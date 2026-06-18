from __future__ import annotations

import argparse
import base64
import io
import json
import os
import re
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from PIL import Image

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from backend.config import load_config
from backend.prompt.builder import PromptBuilder
from backend.storage.paths import task_dir


DEFAULT_ENDPOINT = "https://imagehub.taijiai.online/api/images/generate"
DEFAULT_BASE_URL = "https://www.taijiai.online/"
DEFAULT_CLIENT_ID = "7ece16f1-4ec4-4a0a-976a-257e8e2a1901"
PROJECT_RPG_STYLE_CONTRACT = (
    "Project-wide requirement: every generated asset must be RPG game art first. "
    "Use cute colorful top-down RPG pixel-art readability, clean silhouettes, "
    "limited cohesive palette, low-noise symbolic detail, and crisp hand-placed "
    "pixels. Do not create photorealistic material textures, raw texture scans, "
    "generic texture samples, 3D renders, or scene paintings unless explicitly "
    "requested for a non-final reference."
)
DEFAULT_PROMPT = (
    "volcanic terrain RPG tileset source sheet: one complete continuous "
    "top-down RPG map ground field with dark cracked basalt, charcoal ash, "
    "clean glowing lava cracks, small molten pools, and readable scorched rock "
    "edges. Use broad color clusters and sparse symbolic pixel details that "
    "remain clear after slicing into reusable tiles."
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate one image through the ImageHub bridge endpoint."
    )
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--api-key")
    parser.add_argument("--api-key-env", default="GPT_API_KEY")
    parser.add_argument(
        "--client-id",
        default=os.environ.get("IMAGEHUB_CLIENT_ID", DEFAULT_CLIENT_ID),
    )
    parser.add_argument("--asset-type", default="tile")
    parser.add_argument("--frame-layout", default="terrain_source_8x8")
    parser.add_argument("--prompt")
    parser.add_argument("--prompt-file", help="Read UTF-8 prompt text from a file")
    parser.add_argument(
        "--full-prompt",
        help="Use this prompt as-is instead of building from project templates.",
    )
    parser.add_argument("--task-id", help="Reuse a fixed imports task directory instead of creating a new UUID")
    parser.add_argument("--model")
    parser.add_argument("--protocol", default="custom-openai")
    parser.add_argument("--size", default="1024x1024")
    parser.add_argument("--aspect-ratio", default="1:1")
    parser.add_argument("--resolution", default="1K")
    parser.add_argument("--quality", default="auto")
    parser.add_argument("--output-format", default="jpeg")
    parser.add_argument("--seed", default="")
    parser.add_argument("--negative-prompt", default="")
    parser.add_argument(
        "--reference-image",
        action="append",
        default=[],
        help="Attach a local reference image as a data URL. Can be provided multiple times.",
    )
    parser.add_argument("--count", type=int, default=1)
    parser.add_argument("--batch-id")
    parser.add_argument("--timeout", type=float, default=600.0)
    parser.add_argument(
        "--trust-env",
        choices=["auto", "true", "false"],
        default="auto",
        help="Whether httpx should use proxy env vars. auto ignores 127.0.0.1:9.",
    )
    parser.add_argument("--output-dir")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _load_dotenv(ROOT_DIR / ".env")
    config = load_config(args.config)
    task_id = args.task_id or str(uuid.uuid4())
    output_dir = Path(args.output_dir) if args.output_dir else task_dir(config.paths.assets_dir, task_id, "imports")
    output_dir.mkdir(parents=True, exist_ok=True)

    api_key = _resolve_api_key(args, config)
    if not api_key:
        print(
            f"ERROR: missing API key. Set {args.api_key_env} or pass --api-key.",
            file=sys.stderr,
        )
        return 1

    batch_id = args.batch_id or str(uuid.uuid4())
    full_prompt = _with_project_style_contract(args.full_prompt or _build_project_prompt(args, config))
    model = args.model or config.generator.model
    reference_images = _load_reference_images(args.reference_image)
    request_bodies = [
        _build_body(
            args=args,
            api_key=api_key,
            batch_id=batch_id,
            index=index,
            total=args.count,
            model=model,
            prompt=full_prompt,
            reference_images=reference_images,
        )
        for index in range(1, args.count + 1)
    ]

    _write_json(
        output_dir / "imagehub_request_debug.json",
        {
            "created_at": datetime.now(UTC).isoformat(),
            "endpoint": args.endpoint,
            "task_id": task_id,
            "dry_run": args.dry_run,
            "transport": {
                "httpx_trust_env": _resolve_trust_env(args.trust_env),
                "proxy_env": _proxy_env_snapshot(),
            },
            "requests": [_redact_body(body) for body in request_bodies],
            "reference_images": [
                {
                    "path": str(item["path"]),
                    "mime_type": item["mime_type"],
                    "bytes": item["bytes"],
                }
                for item in reference_images
            ],
        },
    )
    (output_dir / "prompt.txt").write_text(full_prompt, encoding="utf-8")

    if args.dry_run:
        print(
            json.dumps(
                {
                    "task_id": task_id,
                    "status": "dry_run",
                    "output_dir": str(output_dir),
                    "count": args.count,
                    "model": model,
                    "size": args.size,
                    "request_debug": str(output_dir / "imagehub_request_debug.json"),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    client_timeout = httpx.Timeout(
        connect=30.0,
        read=float(args.timeout),
        write=30.0,
        pool=30.0,
    )
    response_summaries: list[dict[str, Any]] = []
    image_paths: list[str] = []
    with httpx.Client(timeout=client_timeout, trust_env=_resolve_trust_env(args.trust_env)) as client:
        for item_index, body in enumerate(request_bodies):
            response = client.post(args.endpoint, json=body)
            if response.status_code != 200:
                error_path = output_dir / f"imagehub_http_error_{item_index}.txt"
                error_path.write_text(response.text, encoding="utf-8", errors="replace")
                raise RuntimeError(f"ImageHub HTTP {response.status_code}: {error_path}")

            data = response.json()
            response_summaries.append(_summarize_response(data))
            image_paths.extend(_save_images(output_dir, item_index, data))

    _write_json(
        output_dir / "imagehub_response_debug.json",
        {
            "created_at": datetime.now(UTC).isoformat(),
            "task_id": task_id,
            "responses": response_summaries,
        },
    )
    print(
        json.dumps(
            {
                "task_id": task_id,
                "status": "succeeded",
                "output_dir": str(output_dir),
                "images": image_paths,
                "request_ids": [
                    item.get("requestId")
                    for item in response_summaries
                    if item.get("requestId")
                ],
                "response_debug": str(output_dir / "imagehub_response_debug.json"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def _resolve_api_key(args: argparse.Namespace, config: Any) -> str | None:
    return (
        args.api_key
        or os.environ.get(args.api_key_env)
        or os.environ.get("GPT_API_KEY")
        or os.environ.get(config.relay.api_key_env)
    )


def _build_project_prompt(args: argparse.Namespace, config: Any) -> str:
    user_prompt = _resolve_prompt(args.prompt, args.prompt_file)
    built = PromptBuilder(config.paths.templates_dir, config.paths.frame_layouts_dir).build(
        asset_type=args.asset_type,
        frame_layout=args.frame_layout,
        user_prompt=user_prompt,
    )
    rows, cols = built.frame_grid
    full_prompt = (
        f"{built.enhanced_prompt} "
        f"Create one image only. Target canvas: {args.size}. "
        f"Frame layout: {args.frame_layout}, grid={rows}x{cols}. "
        "Strictly follow the grid. Return only the image."
    )
    if built.negative_prompt:
        full_prompt = f"{full_prompt} avoid: {built.negative_prompt}"
    return " ".join(full_prompt.split())


def _resolve_prompt(prompt: str | None, prompt_file: str | None) -> str:
    if prompt and prompt_file:
        raise ValueError("Use either --prompt or --prompt-file, not both")
    if prompt_file:
        return Path(prompt_file).read_text(encoding="utf-8").strip()
    return (prompt or DEFAULT_PROMPT).strip()


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _with_project_style_contract(prompt: str) -> str:
    if "Project-wide style contract:" in prompt or PROJECT_RPG_STYLE_CONTRACT in prompt:
        return " ".join(prompt.split())
    return " ".join(f"{PROJECT_RPG_STYLE_CONTRACT} {prompt}".split())


def _build_body(
    args: argparse.Namespace,
    api_key: str,
    batch_id: str,
    index: int,
    total: int,
    model: str,
    prompt: str,
    reference_images: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "baseUrl": args.base_url,
        "apiKey": api_key,
        "clientId": args.client_id,
        "request": {
            "batchId": batch_id,
            "index": index,
            "total": total,
            "protocol": args.protocol,
            "model": model,
            "prompt": prompt,
            "aspectRatio": args.aspect_ratio,
            "size": args.size,
            "resolution": args.resolution,
            "quality": args.quality,
            "outputFormat": args.output_format,
            "seed": args.seed,
            "negativePrompt": args.negative_prompt,
            "referenceImages": [item["data_url"] for item in reference_images],
        },
    }


def _load_reference_images(paths: list[str]) -> list[dict[str, Any]]:
    references: list[dict[str, Any]] = []
    for raw_path in paths:
        path = Path(raw_path)
        if not path.exists():
            raise FileNotFoundError(f"Reference image not found: {path}")
        payload = path.read_bytes()
        mime_type = _mime_type_for_reference(path, payload)
        references.append(
            {
                "path": path,
                "mime_type": mime_type,
                "bytes": len(payload),
                "data_url": f"data:{mime_type};base64,{base64.b64encode(payload).decode('ascii')}",
            }
        )
    return references


def _mime_type_for_reference(path: Path, payload: bytes) -> str:
    detected_format = _detect_image_format(payload)
    detected = (detected_format or "").upper()
    if detected == "JPEG":
        return "image/jpeg"
    if detected == "PNG":
        return "image/png"
    if detected == "WEBP":
        return "image/webp"
    suffix = path.suffix.lower()
    if suffix in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if suffix == ".webp":
        return "image/webp"
    return "image/png"


def _save_images(output_dir: Path, request_index: int, response: dict[str, Any]) -> list[str]:
    if not response.get("ok"):
        raise RuntimeError(f"ImageHub returned ok=false: {response}")
    images = response.get("images")
    if not isinstance(images, list) or not images:
        raise RuntimeError(f"ImageHub response did not include images: {response}")

    paths: list[str] = []
    for image_index, image in enumerate(images):
        if not isinstance(image, dict):
            raise RuntimeError(f"Unexpected image item: {image}")
        declared_mime_type, payload = _parse_data_url(str(image.get("dataUrl", "")))
        detected_format = _detect_image_format(payload)
        ext = _extension_for_image(declared_mime_type, detected_format)
        path = output_dir / f"imagehub_sheet_{request_index}_{image_index}.{ext}"
        path.write_bytes(payload)
        paths.append(str(path))
    return paths


def _parse_data_url(value: str) -> tuple[str, bytes]:
    match = re.match(r"^data:([^;,]+);base64,(.*)$", value, flags=re.DOTALL)
    if not match:
        raise RuntimeError("Image dataUrl is missing or is not base64 encoded")
    mime_type = match.group(1).lower()
    try:
        payload = base64.b64decode(match.group(2), validate=True)
    except ValueError as exc:
        raise RuntimeError("Image dataUrl base64 could not be decoded") from exc
    return mime_type, payload


def _detect_image_format(payload: bytes) -> str | None:
    try:
        with Image.open(io.BytesIO(payload)) as image:
            return image.format
    except OSError:
        return None


def _extension_for_image(declared_mime_type: str, detected_format: str | None) -> str:
    detected = (detected_format or "").upper()
    if detected == "JPEG":
        return "jpg"
    if detected == "PNG":
        return "png"
    if detected == "WEBP":
        return "webp"
    return {
        "image/jpeg": "jpg",
        "image/jpg": "jpg",
        "image/png": "png",
        "image/webp": "webp",
    }.get(declared_mime_type, "bin")


def _summarize_response(response: dict[str, Any]) -> dict[str, Any]:
    summary = dict(response)
    images = []
    for item in response.get("images") or []:
        if not isinstance(item, dict):
            images.append(item)
            continue
        image_summary = dict(item)
        data_url = image_summary.pop("dataUrl", "")
        image_summary["dataUrl_mime"] = _data_url_mime(data_url)
        image_summary["dataUrl_length"] = len(data_url) if isinstance(data_url, str) else 0
        images.append(image_summary)
    summary["images"] = images
    return summary


def _data_url_mime(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    match = re.match(r"^data:([^;,]+)", value)
    return match.group(1) if match else None


def _redact_body(body: dict[str, Any]) -> dict[str, Any]:
    redacted = json.loads(json.dumps(body, ensure_ascii=False))
    if redacted.get("apiKey"):
        redacted["apiKey"] = "<redacted>"
    request = redacted.get("request")
    if isinstance(request, dict) and isinstance(request.get("referenceImages"), list):
        request["referenceImages"] = [
            _summarize_reference_data_url(value)
            for value in request["referenceImages"]
        ]
    return redacted


def _summarize_reference_data_url(value: Any) -> dict[str, Any] | Any:
    if not isinstance(value, str):
        return value
    match = re.match(r"^data:([^;,]+);base64,(.*)$", value, flags=re.DOTALL)
    if not match:
        return {"mime_type": None, "data_url_length": len(value)}
    return {
        "mime_type": match.group(1),
        "base64_length": len(match.group(2)),
    }


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _resolve_trust_env(value: str) -> bool:
    if value == "true":
        return True
    if value == "false":
        return False
    return not _has_blocking_proxy_env()


def _has_blocking_proxy_env() -> bool:
    return any(
        os.environ.get(key, "").strip().lower() == "http://127.0.0.1:9"
        for key in (
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "http_proxy",
            "https_proxy",
            "all_proxy",
        )
    )


def _proxy_env_snapshot() -> dict[str, str]:
    keys = (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
    )
    return {
        key: _redact_proxy_value(value)
        for key in keys
        if (value := os.environ.get(key))
    }


def _redact_proxy_value(value: str) -> str:
    if "@" not in value:
        return value
    prefix, suffix = value.rsplit("@", 1)
    scheme = prefix.split("://", 1)[0] if "://" in prefix else "proxy"
    return f"{scheme}://<redacted>@{suffix}"


if __name__ == "__main__":
    raise SystemExit(main())
