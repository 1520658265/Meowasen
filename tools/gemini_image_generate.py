from __future__ import annotations

import argparse
import base64
import io
import json
import os
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


DEFAULT_NATIVE_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Native Gemini image generation with optional reference images.")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument(
        "--api-mode",
        choices=["openai-chat", "native"],
        default="openai-chat",
        help="openai-chat uses the configured relay /chat/completions; native uses Gemini generateContent.",
    )
    parser.add_argument("--base-url")
    parser.add_argument("--api-key")
    parser.add_argument("--api-key-env", default="GEMINI_API_KEY")
    parser.add_argument("--asset-type", default="character")
    parser.add_argument("--frame-layout", default="single")
    parser.add_argument("--prompt")
    parser.add_argument("--prompt-file")
    parser.add_argument("--full-prompt")
    parser.add_argument("--task-id")
    parser.add_argument("--output-dir")
    parser.add_argument("--model", default="gemini-3.1-flash-image-preview")
    parser.add_argument("--size", default="1024x1024")
    parser.add_argument("--count", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=600.0)
    parser.add_argument("--reference-image", action="append", default=[])
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
    references = _load_reference_images(args.reference_image)
    base_url = (args.base_url or _default_base_url(args.api_mode, config)).rstrip("/")
    payload = _build_payload(args.api_mode, args.model, full_prompt, references)
    url = _request_url(args.api_mode, base_url, args.model)
    headers = _request_headers(args.api_mode, api_key)
    debug = {
        "created_at": datetime.now(UTC).isoformat(),
        "task_id": task_id,
        "url": url,
        "method": "POST",
        "api_mode": args.api_mode,
        "headers": _redact_headers(headers),
        "transport": {
            "httpx_trust_env": _resolve_trust_env(args.trust_env),
            "proxy_env": _proxy_env_snapshot(),
        },
        "model": args.model,
        "size": args.size,
        "reference_images": [
            {"path": str(item["path"]), "mime_type": item["mime_type"], "bytes": item["bytes"]}
            for item in references
        ],
        "payload": _redact_payload(payload),
    }
    _write_json(output_dir / "gemini_request_debug.json", debug)
    (output_dir / "prompt.txt").write_text(" ".join(full_prompt.split()), encoding="utf-8")

    if args.dry_run:
        print(
            json.dumps(
                {
                    "task_id": task_id,
                    "status": "dry_run",
                    "output_dir": str(output_dir),
                    "request_debug": str(output_dir / "gemini_request_debug.json"),
                    "reference_images": len(references),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    timeout = httpx.Timeout(connect=30.0, read=float(args.timeout), write=30.0, pool=30.0)
    image_paths: list[str] = []
    response_summaries: list[dict[str, Any]] = []
    with httpx.Client(timeout=timeout, trust_env=_resolve_trust_env(args.trust_env)) as client:
        for index in range(args.count):
            status_code, raw_body = _post_json_stream(
                client=client,
                url=url,
                headers=headers,
                payload=payload,
                output_dir=output_dir,
                index=index,
            )
            if status_code != 200:
                error_path = output_dir / f"gemini_http_error_{index}.txt"
                error_path.write_bytes(raw_body)
                raise RuntimeError(f"Gemini HTTP {status_code}: {error_path}")
            data = _loads_json_response(raw_body, output_dir, index)
            (output_dir / f"gemini_response_raw_{index}.json").write_text(
                json.dumps(data, ensure_ascii=False),
                encoding="utf-8",
            )
            response_summaries.append(_summarize_response(data))
            image_paths.append(str(_save_image(output_dir, index, data)))

    _write_json(
        output_dir / "gemini_response_debug.json",
        {
            "created_at": datetime.now(UTC).isoformat(),
            "task_id": task_id,
            "responses": response_summaries,
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


def _post_json_stream(
    *,
    client: httpx.Client,
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    output_dir: Path,
    index: int,
) -> tuple[int, bytes]:
    chunks: list[bytes] = []
    status_code = 0
    response_headers: dict[str, str] = {}
    try:
        with client.stream("POST", url, headers=headers, json=payload) as response:
            status_code = response.status_code
            response_headers = dict(response.headers)
            for chunk in response.iter_bytes():
                if chunk:
                    chunks.append(chunk)
    except httpx.RemoteProtocolError as exc:
        raw_body = b"".join(chunks)
        partial_path = output_dir / f"gemini_stream_partial_{index}.bin"
        partial_path.write_bytes(raw_body)
        _write_json(
            output_dir / f"gemini_stream_error_{index}.json",
            {
                "status_code": status_code,
                "headers": response_headers,
                "error": repr(exc),
                "partial_bytes": len(raw_body),
                "partial_path": str(partial_path),
            },
        )
        if raw_body and _looks_like_complete_json(raw_body):
            return status_code, raw_body
        raise RuntimeError(
            f"Gemini stream ended before a complete JSON body was received: {partial_path}"
        ) from exc
    return status_code, b"".join(chunks)


def _loads_json_response(raw_body: bytes, output_dir: Path, index: int) -> dict[str, Any]:
    try:
        data = json.loads(raw_body.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        bad_path = output_dir / f"gemini_json_parse_error_{index}.txt"
        bad_path.write_bytes(raw_body[:20000])
        raise RuntimeError(f"Gemini response was not valid JSON: {bad_path}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"Gemini response JSON was not an object: {type(data).__name__}")
    return data


def _looks_like_complete_json(raw_body: bytes) -> bool:
    try:
        json.loads(raw_body.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    return True


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
    return " ".join(full_prompt.split())


def _default_base_url(api_mode: str, config: Any) -> str:
    if api_mode == "native":
        return DEFAULT_NATIVE_BASE_URL
    return str(config.relay.base_url)


def _request_url(api_mode: str, base_url: str, model: str) -> str:
    if api_mode == "native":
        return f"{base_url}/models/{model}:generateContent"
    return f"{base_url}/chat/completions"


def _request_headers(api_mode: str, api_key: str) -> dict[str, str]:
    if api_mode == "native":
        return {"x-goog-api-key": api_key, "Content-Type": "application/json"}
    return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}


def _redact_headers(headers: dict[str, str]) -> dict[str, str]:
    redacted = dict(headers)
    if "x-goog-api-key" in redacted:
        redacted["x-goog-api-key"] = "<redacted>"
    if "Authorization" in redacted:
        redacted["Authorization"] = "Bearer <redacted>"
    return redacted


def _build_payload(api_mode: str, model: str, prompt: str, references: list[dict[str, Any]]) -> dict[str, Any]:
    prompt = " ".join(prompt.split())
    if api_mode == "native":
        parts: list[dict[str, Any]] = [{"text": prompt}]
        for reference in references:
            parts.append(
                {
                    "inlineData": {
                        "mimeType": reference["mime_type"],
                        "data": reference["base64"],
                    }
                }
            )
        return {
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]},
        }

    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    for reference in references:
        content.append({"type": "image_url", "image_url": {"url": reference["data_url"]}})
    return {
        "model": model,
        "messages": [{"role": "user", "content": content}],
    }


def _load_reference_images(paths: list[str]) -> list[dict[str, Any]]:
    references: list[dict[str, Any]] = []
    for raw_path in paths:
        path = Path(raw_path)
        if not path.exists():
            raise FileNotFoundError(f"Reference image not found: {path}")
        payload = path.read_bytes()
        references.append(
            {
                "path": path,
                "mime_type": _mime_type_for_reference(path, payload),
                "bytes": len(payload),
                "base64": base64.b64encode(payload).decode("ascii"),
                "data_url": f"data:{_mime_type_for_reference(path, payload)};base64,{base64.b64encode(payload).decode('ascii')}",
            }
        )
    return references


def _save_image(output_dir: Path, index: int, response: dict[str, Any]) -> Path:
    mime_type, payload = _extract_inline_image(response)
    ext = _extension_for_mime(mime_type, payload)
    path = output_dir / f"gemini_image_sheet_{index}.{ext}"
    path.write_bytes(payload)
    return path


def _extract_inline_image(response: dict[str, Any]) -> tuple[str, bytes]:
    text_parts: list[str] = []
    for choice in response.get("choices") or []:
        message = choice.get("message") or {}
        content = message.get("content")
        for item in _iter_chat_content_items(content):
            if isinstance(item.get("text"), str):
                text_parts.append(item["text"])
                image = _extract_data_url_from_text(item["text"])
                if image is not None:
                    return image
            image_url = item.get("image_url")
            if isinstance(image_url, dict) and isinstance(image_url.get("url"), str):
                image = _decode_data_url_or_download(image_url["url"])
                if image is not None:
                    return image
        if isinstance(content, str):
            image = _extract_data_url_from_text(content)
            if image is not None:
                return image
            text_parts.append(content)
    for candidate in response.get("candidates") or []:
        content = candidate.get("content") or {}
        for part in content.get("parts") or []:
            if isinstance(part.get("text"), str):
                text_parts.append(part["text"])
            inline_data = part.get("inlineData") or part.get("inline_data")
            if isinstance(inline_data, dict) and isinstance(inline_data.get("data"), str):
                mime_type = inline_data.get("mimeType") or inline_data.get("mime_type") or "image/png"
                return str(mime_type), base64.b64decode(inline_data["data"])
    text = " ".join(text_parts).strip()
    if text:
        raise RuntimeError(f"Gemini response did not include an image. Text: {text}")
    raise RuntimeError(f"Gemini response did not include an image: {response}")


def _iter_chat_content_items(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, list):
        return [item for item in content if isinstance(item, dict)]
    if isinstance(content, dict):
        return [content]
    return []


def _extract_data_url_from_text(text: str) -> tuple[str, bytes] | None:
    import re

    match = re.search(r"data:(image/[a-zA-Z0-9.+-]+);base64,([A-Za-z0-9+/=\r\n]+)", text)
    if not match:
        return None
    return match.group(1), base64.b64decode(re.sub(r"\s+", "", match.group(2)))


def _decode_data_url_or_download(value: str) -> tuple[str, bytes] | None:
    image = _extract_data_url_from_text(value)
    if image is not None:
        return image
    if value.startswith("http://") or value.startswith("https://"):
        with httpx.Client(timeout=120.0) as client:
            response = client.get(value)
            response.raise_for_status()
            mime_type = response.headers.get("content-type", "image/png").split(";", 1)[0]
            return mime_type, response.content
    return None


def _summarize_response(response: dict[str, Any]) -> dict[str, Any]:
    summary = {
        "top_level_keys": sorted(response.keys()),
        "candidates_count": len(response.get("candidates") or []),
        "choices_count": len(response.get("choices") or []),
    }
    parts_summary = []
    for candidate in response.get("candidates") or []:
        content = candidate.get("content") or {}
        for part in content.get("parts") or []:
            if isinstance(part.get("text"), str):
                parts_summary.append({"text_length": len(part["text"])})
            inline_data = part.get("inlineData") or part.get("inline_data")
            if isinstance(inline_data, dict):
                data = inline_data.get("data")
                parts_summary.append(
                    {
                        "mime_type": inline_data.get("mimeType") or inline_data.get("mime_type"),
                        "base64_length": len(data) if isinstance(data, str) else 0,
                    }
                )
    summary["parts"] = parts_summary
    return summary


def _redact_payload(payload: dict[str, Any]) -> dict[str, Any]:
    redacted = json.loads(json.dumps(payload, ensure_ascii=False))
    for content in redacted.get("contents") or []:
        for part in content.get("parts") or []:
            inline_data = part.get("inlineData")
            if isinstance(inline_data, dict) and isinstance(inline_data.get("data"), str):
                inline_data["data"] = f"<base64 length={len(inline_data['data'])}>"
    for message in redacted.get("messages") or []:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            image_url = part.get("image_url")
            if isinstance(image_url, dict) and isinstance(image_url.get("url"), str):
                image_url["url"] = _summarize_data_url(image_url["url"])
    return redacted


def _summarize_data_url(value: str) -> str:
    import re

    match = re.match(r"^data:([^;,]+);base64,(.*)$", value, flags=re.DOTALL)
    if not match:
        return f"<url length={len(value)}>"
    return f"data:{match.group(1)};base64,<length={len(match.group(2))}>"


def _mime_type_for_reference(path: Path, payload: bytes) -> str:
    try:
        with Image.open(io.BytesIO(payload)) as image:
            image_format = (image.format or "").upper()
    except OSError:
        image_format = ""
    if image_format == "JPEG":
        return "image/jpeg"
    if image_format == "PNG":
        return "image/png"
    if image_format == "WEBP":
        return "image/webp"
    suffix = path.suffix.lower()
    if suffix in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if suffix == ".webp":
        return "image/webp"
    return "image/png"


def _extension_for_mime(mime_type: str, payload: bytes) -> str:
    try:
        with Image.open(io.BytesIO(payload)) as image:
            image_format = (image.format or "").upper()
    except OSError:
        image_format = ""
    if image_format == "JPEG" or mime_type == "image/jpeg":
        return "jpg"
    if image_format == "WEBP" or mime_type == "image/webp":
        return "webp"
    return "png"


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
