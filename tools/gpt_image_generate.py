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


DEFAULT_BASE_URL = "https://bobdong.cn/v1"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Direct gpt-image-2 image generation through /images/generations.")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--api-key")
    parser.add_argument("--api-key-env", default="GPT_API_KEY")
    parser.add_argument("--asset-type", default="character")
    parser.add_argument("--frame-layout", default="single")
    parser.add_argument("--prompt")
    parser.add_argument("--prompt-file")
    parser.add_argument("--full-prompt")
    parser.add_argument("--task-id")
    parser.add_argument("--output-dir")
    parser.add_argument("--model", default="gpt-image-2")
    parser.add_argument("--size", default="1024x1024")
    parser.add_argument("--count", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=600.0)
    parser.add_argument("--trust-env", choices=["auto", "true", "false"], default="auto")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _load_dotenv(ROOT_DIR / ".env")
    config = load_config(args.config)
    task_id = args.task_id or str(uuid.uuid4())
    output_dir = Path(args.output_dir) if args.output_dir else task_dir(config.paths.assets_dir, task_id, "imports")
    output_dir.mkdir(parents=True, exist_ok=True)

    api_key = args.api_key or os.environ.get(args.api_key_env) or os.environ.get(config.relay.api_key_env)
    if not api_key:
        print(f"ERROR: missing API key. Set {args.api_key_env} or pass --api-key.", file=sys.stderr)
        return 1

    full_prompt = args.full_prompt or _build_project_prompt(args, config)
    payload = {
        "model": args.model,
        "prompt": " ".join(full_prompt.split()),
        "size": args.size,
        "n": args.count,
        "stream": True,
    }
    debug = {
        "created_at": datetime.now(UTC).isoformat(),
        "task_id": task_id,
        "url": f"{args.base_url.rstrip('/')}/images/generations",
        "method": "POST",
        "headers": {"Authorization": "Bearer <redacted>", "Content-Type": "application/json"},
        "transport": {
            "httpx_trust_env": _resolve_trust_env(args.trust_env),
            "proxy_env": _proxy_env_snapshot(),
        },
        "payload": payload,
    }
    _write_json(output_dir / "gpt_image_request_debug.json", debug)
    (output_dir / "prompt.txt").write_text(payload["prompt"], encoding="utf-8")

    if args.dry_run:
        print(
            json.dumps(
                {
                    "task_id": task_id,
                    "status": "dry_run",
                    "output_dir": str(output_dir),
                    "request_debug": str(output_dir / "gpt_image_request_debug.json"),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    timeout = httpx.Timeout(connect=30.0, read=float(args.timeout), write=30.0, pool=30.0)
    chunks: list[bytes] = []
    url = f"{args.base_url.rstrip('/')}/images/generations"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    response_headers: dict[str, str] = {}
    try:
        with httpx.Client(timeout=timeout, trust_env=_resolve_trust_env(args.trust_env)) as client:
            with client.stream("POST", url, headers=headers, json=payload) as response:
                response_headers = dict(response.headers.items())
                if response.status_code != 200:
                    body = response.read()
                    (output_dir / "gpt_image_http_error.txt").write_bytes(body)
                    raise RuntimeError(f"GPT image HTTP {response.status_code}: {output_dir / 'gpt_image_http_error.txt'}")
                for chunk in response.iter_bytes():
                    if chunk:
                        chunks.append(chunk)
    except httpx.HTTPError as exc:
        partial = b"".join(chunks)
        if partial:
            (output_dir / "gpt_image_response_partial.txt").write_bytes(partial)
        _write_json(
            output_dir / "gpt_image_error_debug.json",
            {
                "created_at": datetime.now(UTC).isoformat(),
                "error": type(exc).__name__,
                "message": str(exc),
                "headers": response_headers,
                "bytes_read": len(partial),
            },
        )
        raise

    raw = b"".join(chunks)
    (output_dir / "gpt_image_response_raw.json").write_bytes(raw)
    data = json.loads(raw.decode("utf-8"))
    image_paths = _save_images(output_dir, data)
    _write_json(
        output_dir / "gpt_image_response_debug.json",
        {
            "created_at": datetime.now(UTC).isoformat(),
            "task_id": task_id,
            "headers": response_headers,
            "raw_response_keys": sorted(data.keys()),
            "image_count": len(image_paths),
        },
    )
    print(
        json.dumps(
            {
                "task_id": task_id,
                "status": "succeeded",
                "output_dir": str(output_dir),
                "images": image_paths,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def _build_project_prompt(args: argparse.Namespace, config: Any) -> str:
    if args.prompt_file:
        user_prompt = Path(args.prompt_file).read_text(encoding="utf-8").strip()
    else:
        user_prompt = (args.prompt or "").strip()
    if not user_prompt:
        raise ValueError("Either --prompt or --prompt-file is required")
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
    return full_prompt


def _save_images(output_dir: Path, response: dict[str, Any]) -> list[str]:
    items = response.get("data")
    if not isinstance(items, list) or not items:
        raise RuntimeError(f"GPT image response did not include data: {response}")
    paths: list[str] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise RuntimeError(f"Unexpected image item: {item}")
        if isinstance(item.get("b64_json"), str):
            payload = base64.b64decode(item["b64_json"])
        elif isinstance(item.get("url"), str):
            with httpx.Client(timeout=120.0) as client:
                payload = client.get(item["url"]).content
        else:
            raise RuntimeError(f"Image item did not include b64_json or url: {item}")
        ext = _detect_extension(payload)
        path = output_dir / f"gpt_image_sheet_{index}.{ext}"
        path.write_bytes(payload)
        paths.append(str(path))
    return paths


def _detect_extension(payload: bytes) -> str:
    try:
        with Image.open(io.BytesIO(payload)) as image:
            image_format = (image.format or "").upper()
    except OSError:
        return "bin"
    if image_format == "JPEG":
        return "jpg"
    if image_format == "PNG":
        return "png"
    if image_format == "WEBP":
        return "webp"
    return image_format.lower() or "bin"


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


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
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")
    )


def _proxy_env_snapshot() -> dict[str, str]:
    keys = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "all_proxy", "no_proxy")
    return {key: _redact_proxy_value(value) for key in keys if (value := os.environ.get(key))}


def _redact_proxy_value(value: str) -> str:
    if "@" not in value:
        return value
    prefix, suffix = value.rsplit("@", 1)
    scheme = prefix.split("://", 1)[0] if "://" in prefix else "proxy"
    return f"{scheme}://<redacted>@{suffix}"


if __name__ == "__main__":
    raise SystemExit(main())
