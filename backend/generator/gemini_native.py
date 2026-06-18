from __future__ import annotations

import asyncio
import base64
import json
from datetime import UTC, datetime
from pathlib import Path
import urllib.parse
from typing import Any

from .protocol import GeneratedImage, GenerateRequest, GeneratorError, ProviderCapabilities
from .streaming_http import post_json_stream


class GeminiNativeProvider:
    """Native Gemini image generation via direct REST HTTP."""

    capabilities = ProviderCapabilities(
        supports_reference_images=True,
        supports_image_edit=False,
        supports_custom_size=False,
        supports_seed=False,
    )

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://generativelanguage.googleapis.com/v1",
    ) -> None:
        if not api_key:
            raise GeneratorError("Missing API key for native Gemini API")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")

    async def generate(self, request: GenerateRequest) -> list[GeneratedImage]:
        tasks = [self._generate_one(request, index) for index in range(request.count)]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        images: list[GeneratedImage] = []
        errors: list[str] = []
        for result in results:
            if isinstance(result, Exception):
                errors.append(str(result))
            else:
                images.append(result)
        if not images:
            raise GeneratorError("; ".join(errors) if errors else "Gemini API returned no images")
        if errors:
            images[0].provider_metadata["partial_errors"] = errors
        return images

    async def edit(
        self,
        image_bytes: bytes,
        prompt: str,
        provider_options: dict[str, Any] | None = None,
    ) -> GeneratedImage:
        raise GeneratorError("Image edit is not supported by GeminiNativeProvider")

    async def _generate_one(self, request: GenerateRequest, index: int) -> GeneratedImage:
        return await asyncio.to_thread(self._generate_one_sync, request, index)

    def _generate_one_sync(self, request: GenerateRequest, index: int) -> GeneratedImage:
        sheet_width = request.sheet_width or request.frame_grid[1] * request.cell_size
        sheet_height = request.sheet_height or request.frame_grid[0] * request.cell_size
        full_prompt = (
            f"{request.prompt}\n"
            f"Create one image only. Target canvas: {sheet_width}x{sheet_height}. "
            f"Frame layout: {request.frame_layout}, grid={request.frame_grid[0]}x{request.frame_grid[1]}. "
            "Return an image result."
        )
        if request.negative_prompt:
            full_prompt = f"{full_prompt}\navoid: {request.negative_prompt}"
        if request.reference_images:
            full_prompt = (
                f"{full_prompt}\nUse the provided reference image to preserve the same character identity, "
                "palette, outfit, silhouette, and pixel-art style."
            )

        parts: list[dict[str, Any]] = [{"text": full_prompt}]
        for image_bytes in request.reference_images:
            parts.append(
                {
                    "inlineData": {
                        "mimeType": "image/png",
                        "data": base64.b64encode(image_bytes).decode("ascii"),
                    }
                }
            )

        payload: dict[str, Any] = {
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]},
        }
        payload = self._merge_provider_options(payload, request.provider_options)

        url = self._generate_content_url(request.model)
        debug_path = self._write_request_debug(request, index, url, payload)
        response = self._post_generate_content(url, payload, request.timeout_seconds, debug_path)
        mime_type, image_bytes = self._extract_inline_image(response)
        self._append_debug(
            debug_path,
            {
                "parsed_response": {
                    "top_level_keys": sorted(response.keys()),
                    "candidates_count": len(response.get("candidates") or []),
                }
            },
        )
        return GeneratedImage(
            index=index,
            image_bytes=image_bytes,
            mime_type=mime_type,
            provider_metadata={
                "backend": "gemini_native",
                "model": request.model,
                "reference_applied": bool(request.reference_images),
                "seed_applied": False,
                "requested_size": [sheet_width, sheet_height],
                "raw_response_keys": sorted(response.keys()),
            },
        )

    def _post_generate_content(
        self,
        url: str,
        payload: dict[str, Any],
        timeout_seconds: int,
        debug_path: Path | None = None,
    ) -> dict[str, Any]:
        result = post_json_stream(
            url=url,
            payload=payload,
            headers={
                "x-goog-api-key": self.api_key,
                "Content-Type": "application/json",
            },
            timeout_seconds=timeout_seconds,
            error_label="Gemini native API",
            debug_path=debug_path,
        )
        return result.data

    @staticmethod
    def _extract_inline_image(response: dict[str, Any]) -> tuple[str, bytes]:
        texts: list[str] = []
        for candidate in response.get("candidates") or []:
            content = candidate.get("content") or {}
            for part in content.get("parts") or []:
                if isinstance(part.get("text"), str):
                    texts.append(part["text"])
                inline_data = part.get("inlineData") or part.get("inline_data")
                if isinstance(inline_data, dict) and isinstance(inline_data.get("data"), str):
                    mime_type = inline_data.get("mimeType") or inline_data.get("mime_type") or "image/png"
                    try:
                        return str(mime_type), base64.b64decode(inline_data["data"])
                    except ValueError as exc:
                        raise GeneratorError("Gemini native inline image base64 could not be decoded") from exc
        text_hint = " ".join(texts).strip()
        if text_hint:
            raise GeneratorError(f"Gemini native response did not include an image. Text: {text_hint}")
        raise GeneratorError(f"Gemini native response did not include an image: {response}")

    @staticmethod
    def _merge_provider_options(
        payload: dict[str, Any],
        provider_options: dict[str, Any],
    ) -> dict[str, Any]:
        merged = dict(payload)
        for key, value in provider_options.items():
            if key == "generationConfig" and isinstance(value, dict):
                generation_config = dict(merged.get("generationConfig") or {})
                generation_config.update(value)
                merged["generationConfig"] = generation_config
            elif key != "base_url":
                merged[key] = value
        return merged

    def _generate_content_url(self, model: str) -> str:
        encoded_model = urllib.parse.quote(model, safe="")
        return f"{self.base_url}/models/{encoded_model}:generateContent"

    @staticmethod
    def _write_request_debug(
        request: GenerateRequest,
        index: int,
        url: str,
        payload: dict[str, Any],
    ) -> Path | None:
        if not request.debug_dir:
            return None
        debug_dir = Path(request.debug_dir)
        debug_dir.mkdir(parents=True, exist_ok=True)
        path = debug_dir / f"request_debug_{index}.json"
        debug_payload = json.loads(json.dumps(payload))
        for content in debug_payload.get("contents") or []:
            for part in content.get("parts") or []:
                inline_data = part.get("inlineData")
                if isinstance(inline_data, dict) and isinstance(inline_data.get("data"), str):
                    inline_data["data"] = f"<base64 length={len(inline_data['data'])}>"
        data = {
            "created_at": datetime.now(UTC).isoformat(),
            "provider": "gemini_native",
            "url": url,
            "method": "POST",
            "headers": {
                "x-goog-api-key": "<redacted>",
                "Content-Type": "application/json",
            },
            "timeout_seconds": request.timeout_seconds,
            "request": {
                "frame_layout": request.frame_layout,
                "frame_grid": list(request.frame_grid),
                "cell_size": request.cell_size,
                "sheet_width": request.sheet_width,
                "sheet_height": request.sheet_height,
                "output_size": request.output_size,
                "reference_images_count": len(request.reference_images),
            },
            "payload": debug_payload,
        }
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    @staticmethod
    def _append_debug(path: Path | None, update: dict[str, Any]) -> None:
        if path is None:
            return
        try:
            current = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except json.JSONDecodeError:
            current = {}
        current.update(update)
        path.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
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
