from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from .protocol import GeneratorError


@dataclass(frozen=True)
class JsonStreamResponse:
    data: dict[str, Any]
    raw_bytes: bytes
    status_code: int
    headers: dict[str, str]


def post_json_stream(
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    timeout_seconds: int,
    error_label: str,
    debug_path: Path | None = None,
    trust_env: bool | None = None,
) -> JsonStreamResponse:
    chunks: list[bytes] = []
    response_headers: dict[str, str] = {}
    read_timeout = float(max(timeout_seconds, 1))
    timeout = httpx.Timeout(connect=30.0, read=read_timeout, write=30.0, pool=30.0)
    resolved_trust_env = _resolve_trust_env(trust_env)
    _append_debug(
        debug_path,
        {
            "transport": {
                "httpx_trust_env": resolved_trust_env,
                "proxy_env": _proxy_env_snapshot(),
            }
        },
    )
    try:
        with httpx.Client(timeout=timeout, trust_env=resolved_trust_env) as client:
            with client.stream("POST", url, headers=headers, json=payload) as resp:
                response_headers = dict(resp.headers.items())
                if resp.status_code != 200:
                    body_bytes = resp.read()
                    raw_path = _write_raw_debug_file(debug_path, "response_http_error.txt", body_bytes)
                    body = body_bytes.decode("utf-8", errors="replace")
                    _append_debug(
                        debug_path,
                        {
                            "error": {
                                "type": "HTTPStatusError",
                                "code": resp.status_code,
                                "headers": response_headers,
                                "raw_file": raw_path.name if raw_path else None,
                                "body": body,
                            }
                        },
                    )
                    raise GeneratorError(f"{error_label} HTTP {resp.status_code}: {body}")

                for chunk in resp.iter_bytes():
                    if chunk:
                        chunks.append(chunk)
    except GeneratorError:
        raise
    except httpx.TimeoutException as exc:
        partial = b"".join(chunks)
        raw_path = _write_raw_debug_file(debug_path, "response_partial.txt", partial) if partial else None
        _append_debug(
            debug_path,
            {
                "error": {
                    "type": type(exc).__name__,
                    "message": str(exc),
                    "bytes_read": len(partial),
                    "raw_file": raw_path.name if raw_path else None,
                }
            },
        )
        raise GeneratorError(f"{error_label} request timed out: {exc}") from exc
    except httpx.HTTPError as exc:
        partial = b"".join(chunks)
        raw_path = _write_raw_debug_file(debug_path, "response_partial.txt", partial) if partial else None
        partial_text = partial.decode("utf-8", errors="replace")
        _append_debug(
            debug_path,
            {
                "error": {
                    "type": type(exc).__name__,
                    "message": str(exc),
                    "bytes_read": len(partial),
                    "headers": response_headers,
                    "request_id": response_headers.get("x-oneapi-request-id"),
                    "raw_file": raw_path.name if raw_path else None,
                    "partial_head": partial_text[:4000],
                    "partial_tail": partial_text[-4000:],
                }
            },
        )
        raise GeneratorError(f"{error_label} request failed: {exc}") from exc

    raw_bytes = b"".join(chunks)
    raw_path = _write_raw_debug_file(debug_path, "response_raw.json", raw_bytes)
    _append_debug(
        debug_path,
        {
            "response": {
                "status": 200,
                "headers": response_headers,
                "bytes_read": len(raw_bytes),
                "raw_file": raw_path.name if raw_path else None,
            }
        },
    )
    try:
        raw = raw_bytes.decode("utf-8")
        data = json.loads(raw)
    except UnicodeDecodeError as exc:
        _append_debug(debug_path, {"error": {"type": "UnicodeDecodeError", "message": str(exc)}})
        raise GeneratorError(f"{error_label} returned non-UTF8 JSON: {exc}") from exc
    except json.JSONDecodeError as exc:
        _append_debug(debug_path, {"error": {"type": "JSONDecodeError", "message": str(exc)}})
        raise GeneratorError(f"{error_label} returned invalid JSON: {exc}") from exc

    return JsonStreamResponse(
        data=data,
        raw_bytes=raw_bytes,
        status_code=200,
        headers=response_headers,
    )


def _append_debug(path: Path | None, update: dict[str, Any]) -> None:
    if path is None:
        return
    try:
        current = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except json.JSONDecodeError:
        current = {}
    current.update(update)
    path.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_raw_debug_file(
    debug_path: Path | None,
    filename: str,
    data: bytes,
) -> Path | None:
    if debug_path is None:
        return None
    path = debug_path.with_name(filename)
    path.write_bytes(data)
    return path


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


def _resolve_trust_env(configured: bool | None) -> bool:
    if configured is not None:
        return configured
    return not _has_blocking_proxy_env()


def _has_blocking_proxy_env() -> bool:
    return any(
        os.environ.get(key, "").strip().lower() == "http://127.0.0.1:9"
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")
    )
