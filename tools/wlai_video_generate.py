from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import re
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import httpx


ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_BASE_URL = "https://bobdong.cn"
DEFAULT_ENDPOINT = "/v1/videos"
DEFAULT_STATUS_ENDPOINT_TEMPLATE = "/v1/videos/{video_id}"
DEFAULT_MODEL = "seedance-2.0-720p"
DEFAULT_IMAGE = "assets/tasks/imports/ref_girl_toy_poodle_v1/gpt_image_sheet_0.png"
DEFAULT_PROMPT_FILE = "assets/tasks/videos/girl_toy_poodle_idle_step_v1/video_request.json"
VIDEO_EXTENSIONS = (".mp4", ".webm", ".mov", ".m4v")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate an image-to-video clip through bobdong.cn /v1/videos openai-video endpoint."
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--status-endpoint-template", default=DEFAULT_STATUS_ENDPOINT_TEMPLATE)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--api-key")
    parser.add_argument("--api-key-env", default="GPT_API_KEY")
    parser.add_argument("--image", default=DEFAULT_IMAGE)
    parser.add_argument("--image-url", help="Use a public image URL instead of embedding --image as a data URL.")
    parser.add_argument(
        "--image-field",
        default="image_url",
        choices=["image_url", "image", "input_image", "first_frame_image", "img_url"],
    )
    parser.add_argument("--prompt")
    parser.add_argument("--prompt-file", default=DEFAULT_PROMPT_FILE)
    parser.add_argument("--negative-prompt")
    parser.add_argument("--duration", type=int)
    parser.add_argument("--size", default="")
    parser.add_argument("--resolution", default="")
    parser.add_argument("--ratio", default="1:1")
    parser.add_argument("--fps", type=int)
    parser.add_argument("--task-id", default="girl_toy_poodle_seedance_idle_step_v1")
    parser.add_argument("--output-dir")
    parser.add_argument(
        "--extra",
        action="append",
        default=[],
        help='Extra JSON object merged into the request, for example --extra \'{"quality":"standard"}\'.',
    )
    parser.add_argument("--timeout", type=float, default=600.0)
    parser.add_argument("--poll", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--poll-interval", type=float, default=8.0)
    parser.add_argument("--poll-timeout", type=float, default=900.0)
    parser.add_argument("--download", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--trust-env", choices=["auto", "true", "false"], default="auto")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _load_dotenv(ROOT_DIR / ".env")

    task_id = args.task_id or str(uuid.uuid4())
    output_dir = Path(args.output_dir) if args.output_dir else ROOT_DIR / "assets" / "tasks" / "videos" / task_id
    output_dir.mkdir(parents=True, exist_ok=True)

    api_key = args.api_key or os.environ.get(args.api_key_env)
    if not api_key:
        print(f"ERROR: missing API key. Set {args.api_key_env} in .env or pass --api-key.", file=sys.stderr)
        return 1

    prompt, negative_prompt, prompt_meta = _resolve_prompt(args.prompt, args.prompt_file, args.negative_prompt)
    image_ref = args.image_url or _image_data_url(_resolve_image_path(args.image))
    duration = args.duration if args.duration is not None else int(prompt_meta.get("duration_seconds") or 3)

    payload = _build_payload(
        args=args,
        prompt=prompt,
        negative_prompt=negative_prompt,
        image_ref=image_ref,
        duration=duration,
    )

    request_debug = {
        "created_at": datetime.now(UTC).isoformat(),
        "task_id": task_id,
        "url": _join_url(args.base_url, args.endpoint),
        "method": "POST",
        "headers": {"Authorization": "Bearer <redacted>", "Content-Type": "application/json"},
        "transport": {
            "httpx_trust_env": _resolve_trust_env(args.trust_env),
            "proxy_env": _proxy_env_snapshot(),
        },
        "input_image": None if args.image_url else str(_resolve_image_path(args.image)),
        "input_image_url": args.image_url,
        "payload": _redact_payload(payload),
    }
    _write_json(output_dir / "video_request_debug.json", request_debug)
    (output_dir / "prompt.txt").write_text(prompt, encoding="utf-8")
    if negative_prompt:
        (output_dir / "negative_prompt.txt").write_text(negative_prompt, encoding="utf-8")

    if args.dry_run:
        print(
            json.dumps(
                {
                    "task_id": task_id,
                    "status": "dry_run",
                    "output_dir": str(output_dir),
                    "request_debug": str(output_dir / "video_request_debug.json"),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    timeout = httpx.Timeout(connect=30.0, read=float(args.timeout), write=60.0, pool=30.0)
    with httpx.Client(timeout=timeout, trust_env=_resolve_trust_env(args.trust_env), follow_redirects=True) as client:
        initial = _post_json(
            client=client,
            url=_join_url(args.base_url, args.endpoint),
            headers=headers,
            payload=payload,
            output_dir=output_dir,
        )
        _write_json(output_dir / "video_response_initial.json", initial)

        final_response = initial
        video_id = _extract_video_id(initial)
        if args.poll and video_id and not _is_terminal(initial) and not _extract_video_ref(initial):
            final_response = _poll_video(
                client=client,
                base_url=args.base_url,
                status_endpoint_template=args.status_endpoint_template,
                headers=headers,
                output_dir=output_dir,
                video_id=video_id,
                interval=args.poll_interval,
                timeout_seconds=args.poll_timeout,
            )

        _write_json(output_dir / "video_response_final.json", final_response)
        video_path = None
        if args.download:
            video_ref = _extract_video_ref(final_response) or _extract_video_ref(initial)
            if video_ref:
                video_path = _download_video(client, video_ref, args.base_url, headers, output_dir)

    result = {
        "task_id": task_id,
        "remote_video_id": video_id,
        "status": _extract_status(final_response),
        "output_dir": str(output_dir),
        "video": str(video_path) if video_path else None,
        "final_response": str(output_dir / "video_response_final.json"),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not video_path and not _is_success(final_response):
        print("Video was not downloaded. Check video_response_initial.json/final.json.", file=sys.stderr)
    return 0


def _build_payload(
    *,
    args: argparse.Namespace,
    prompt: str,
    negative_prompt: str,
    image_ref: str,
    duration: int,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": args.model,
        "prompt": prompt,
        args.image_field: image_ref,
        "duration": duration,
        "ratio": args.ratio,
    }
    if negative_prompt:
        payload["negative_prompt"] = negative_prompt
    if args.size:
        payload["size"] = args.size
    if args.resolution:
        payload["resolution"] = args.resolution
    if args.fps is not None:
        payload["fps"] = args.fps
    for raw_extra in args.extra:
        extra = json.loads(raw_extra)
        if not isinstance(extra, dict):
            raise ValueError("--extra must be a JSON object")
        _deep_update(payload, extra)
    return payload


def _resolve_prompt(
    prompt: str | None,
    prompt_file: str | None,
    negative_prompt: str | None,
) -> tuple[str, str, dict[str, Any]]:
    if prompt:
        return prompt.strip(), (negative_prompt or "").strip(), {}
    if not prompt_file:
        raise ValueError("Either --prompt or --prompt-file is required")
    path = Path(prompt_file)
    if not path.is_absolute():
        path = ROOT_DIR / path
    text = path.read_text(encoding="utf-8").strip()
    meta: dict[str, Any] = {}
    if path.suffix.lower() == ".json":
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError(f"Prompt JSON must be an object: {path}")
        meta = data
        resolved_prompt = str(data.get("prompt") or "").strip()
        resolved_negative = str(data.get("negative_prompt") or "").strip()
    else:
        resolved_prompt = text
        resolved_negative = ""
    if negative_prompt:
        resolved_negative = negative_prompt.strip()
    if not resolved_prompt:
        raise ValueError(f"Prompt file did not contain prompt text: {path}")
    return resolved_prompt, resolved_negative, meta


def _resolve_image_path(raw_path: str) -> Path:
    path = Path(raw_path)
    if not path.is_absolute():
        path = ROOT_DIR / path
    if not path.exists():
        raise FileNotFoundError(f"Input image not found: {path}")
    return path


def _post_json(
    *,
    client: httpx.Client,
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    output_dir: Path,
) -> dict[str, Any]:
    response = client.post(url, headers=headers, json=payload)
    body = response.content
    (output_dir / "video_response_initial_raw.bin").write_bytes(body)
    if response.status_code >= 400:
        (output_dir / "video_http_error.txt").write_bytes(body)
        raise RuntimeError(f"Video HTTP {response.status_code}: {output_dir / 'video_http_error.txt'}")
    return _loads_json(body, output_dir / "video_json_parse_error.txt")


def _poll_video(
    *,
    client: httpx.Client,
    base_url: str,
    status_endpoint_template: str,
    headers: dict[str, str],
    output_dir: Path,
    video_id: str,
    interval: float,
    timeout_seconds: float,
) -> dict[str, Any]:
    started = time.monotonic()
    last_data: dict[str, Any] = {}
    attempt = 0
    urls = _poll_urls(base_url, status_endpoint_template, video_id)
    while time.monotonic() - started <= timeout_seconds:
        attempt += 1
        for url in urls:
            response = client.get(url, headers=headers)
            if response.status_code == 404:
                continue
            raw_path = output_dir / f"video_poll_{attempt:03d}.json"
            raw_path.write_bytes(response.content)
            if response.status_code >= 400:
                continue
            if _looks_like_video_bytes(response.content, response.headers.get("content-type", "")):
                video_path = output_dir / "video.mp4"
                video_path.write_bytes(response.content)
                return {
                    "id": video_id,
                    "status": "succeeded",
                    "video_url": str(video_path),
                    "content_type": response.headers.get("content-type", ""),
                    "bytes": len(response.content),
                }
            data = _loads_json(response.content, output_dir / f"video_poll_parse_error_{attempt:03d}.txt")
            last_data = data
            status = _extract_status(data)
            print(f"poll {attempt}: {status or 'unknown'}")
            if _is_terminal(data) or _extract_video_ref(data):
                return data
            break
        time.sleep(interval)
    if last_data:
        return last_data
    raise TimeoutError(f"Video task did not finish within {timeout_seconds} seconds: {video_id}")


def _poll_urls(base_url: str, status_endpoint_template: str, video_id: str) -> list[str]:
    candidates = [
        status_endpoint_template.replace("{video_id}", video_id).replace("{task_id}", video_id),
        f"/v1/videos/{video_id}",
        f"/v1/videos/{video_id}/content",
    ]
    urls: list[str] = []
    seen: set[str] = set()
    for item in candidates:
        url = _join_url(base_url, item)
        if url not in seen:
            urls.append(url)
            seen.add(url)
    return urls


def _extract_video_id(data: Any) -> str | None:
    for key in ("id", "video_id", "videoId", "task_id", "taskId", "request_id", "requestId"):
        value = _find_key(data, key)
        if isinstance(value, (str, int)) and str(value).strip():
            return str(value).strip()
    return None


def _extract_status(data: Any) -> str:
    for key in ("status", "state", "task_status", "taskStatus"):
        value = _find_key(data, key)
        if isinstance(value, str):
            return value
    return ""


def _is_terminal(data: Any) -> bool:
    status = _extract_status(data).lower()
    return status in {
        "succeeded",
        "succeed",
        "success",
        "successful",
        "completed",
        "complete",
        "finished",
        "done",
        "failed",
        "error",
        "cancelled",
        "canceled",
        "expired",
    }


def _is_success(data: Any) -> bool:
    status = _extract_status(data).lower()
    return status in {"succeeded", "succeed", "success", "successful", "completed", "complete", "finished", "done"} or bool(
        _extract_video_ref(data)
    )


def _extract_video_ref(data: Any) -> str | None:
    if isinstance(data, str):
        if data.startswith("data:video/") or _looks_like_video_url(data):
            return data
        local_path = Path(data)
        if local_path.exists() and local_path.suffix.lower() in VIDEO_EXTENSIONS:
            return data
        return None
    if isinstance(data, dict):
        for key in (
            "video_url",
            "videoUrl",
            "download_url",
            "downloadUrl",
            "output_url",
            "outputUrl",
            "file_url",
            "fileUrl",
            "url",
            "video",
            "content",
        ):
            value = data.get(key)
            if isinstance(value, str) and (value.startswith("data:video/") or _looks_like_video_url(value)):
                return value
        for value in data.values():
            found = _extract_video_ref(value)
            if found:
                return found
    if isinstance(data, list):
        for value in data:
            found = _extract_video_ref(value)
            if found:
                return found
    return None


def _find_key(data: Any, wanted: str) -> Any:
    if isinstance(data, dict):
        if wanted in data:
            return data[wanted]
        for value in data.values():
            found = _find_key(value, wanted)
            if found is not None:
                return found
    elif isinstance(data, list):
        for value in data:
            found = _find_key(value, wanted)
            if found is not None:
                return found
    return None


def _download_video(
    client: httpx.Client,
    video_ref: str,
    base_url: str,
    headers: dict[str, str],
    output_dir: Path,
) -> Path:
    if video_ref.startswith("data:video/"):
        mime_type, payload = _parse_data_url(video_ref)
        ext = _extension_for_mime(mime_type)
        path = output_dir / f"video.{ext}"
        path.write_bytes(payload)
        return path

    local_path = Path(video_ref)
    if not local_path.is_absolute():
        local_path = ROOT_DIR / local_path
    if local_path.exists():
        return local_path

    url = video_ref if video_ref.startswith(("http://", "https://")) else urljoin(base_url.rstrip("/") + "/", video_ref)
    response = client.get(url, headers=headers)
    if response.status_code in {401, 403}:
        response = client.get(url)
    response.raise_for_status()
    ext = _extension_from_url_or_type(url, response.headers.get("content-type", ""))
    path = output_dir / f"video{ext}"
    path.write_bytes(response.content)
    return path


def _image_data_url(path: Path) -> str:
    mime_type = mimetypes.guess_type(path.name)[0] or "image/png"
    payload = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{payload}"


def _parse_data_url(value: str) -> tuple[str, bytes]:
    match = re.match(r"^data:([^;,]+);base64,(.*)$", value, flags=re.DOTALL)
    if not match:
        raise ValueError("Invalid data URL")
    return match.group(1), base64.b64decode(re.sub(r"\s+", "", match.group(2)))


def _looks_like_video_bytes(payload: bytes, content_type: str) -> bool:
    if content_type.lower().startswith("video/"):
        return True
    if len(payload) >= 12 and payload[4:8] == b"ftyp":
        return True
    return False


def _looks_like_video_url(value: str) -> bool:
    lower = value.lower().split("?", 1)[0]
    return lower.startswith(("http://", "https://", "/")) and lower.endswith(VIDEO_EXTENSIONS)


def _extension_for_mime(mime_type: str) -> str:
    if mime_type == "video/webm":
        return "webm"
    if mime_type == "video/quicktime":
        return "mov"
    return "mp4"


def _extension_from_url_or_type(url: str, content_type: str) -> str:
    lower = url.lower().split("?", 1)[0]
    for ext in VIDEO_EXTENSIONS:
        if lower.endswith(ext):
            return ext
    if "webm" in content_type:
        return ".webm"
    if "quicktime" in content_type:
        return ".mov"
    return ".mp4"


def _loads_json(payload: bytes, error_path: Path) -> dict[str, Any]:
    try:
        data = json.loads(payload.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        error_path.write_bytes(payload[:20000])
        raise RuntimeError(f"Response was not valid JSON: {error_path}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"Response JSON was not an object: {type(data).__name__}")
    return data


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _deep_update(target: dict[str, Any], patch: dict[str, Any]) -> None:
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_update(target[key], value)
        else:
            target[key] = value


def _join_url(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _redact_payload(payload: dict[str, Any]) -> dict[str, Any]:
    redacted = json.loads(json.dumps(payload, ensure_ascii=False))
    for key in ("image", "image_url", "input_image", "img_url", "first_frame_image"):
        value = redacted.get(key)
        if isinstance(value, str) and value.startswith("data:"):
            redacted[key] = _summarize_data_url(value)
    return redacted


def _summarize_data_url(value: str) -> str:
    match = re.match(r"^data:([^;,]+);base64,(.*)$", value, flags=re.DOTALL)
    if not match:
        return f"<data-url length={len(value)}>"
    return f"data:{match.group(1)};base64,<length={len(match.group(2))}>"


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
