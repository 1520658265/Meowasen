from __future__ import annotations

import asyncio
import base64
import json
from datetime import UTC, datetime
from pathlib import Path
import urllib.error
import urllib.request
from typing import Any

from .protocol import GeneratedImage, GenerateRequest, GeneratorError, ProviderCapabilities
from .streaming_http import post_json_stream


class OpenAIImageProvider:
    """OpenAI-compatible image generation via direct HTTP."""

    capabilities = ProviderCapabilities(
        supports_reference_images=False,
        supports_image_edit=False,
        supports_custom_size=True,
        supports_seed=False,
    )

    def __init__(self, api_key: str, base_url: str) -> None:
        if not api_key:
            raise GeneratorError("Missing API key for OpenAI image provider")
        if not base_url:
            raise GeneratorError("Missing OpenAI image base_url")
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
            raise GeneratorError("; ".join(errors) if errors else "OpenAI image API returned no images")
        if errors:
            images[0].provider_metadata["partial_errors"] = errors
        return images

    async def edit(
        self,
        image_bytes: bytes,
        prompt: str,
        provider_options: dict[str, Any] | None = None,
    ) -> GeneratedImage:
        raise GeneratorError("Image edit is not implemented for OpenAIImageProvider")

    async def _generate_one(self, request: GenerateRequest, index: int) -> GeneratedImage:
        return await asyncio.to_thread(self._generate_one_sync, request, index)

    def _generate_one_sync(self, request: GenerateRequest, index: int) -> GeneratedImage:
        sheet_width = request.sheet_width or request.frame_grid[1] * request.cell_size
        sheet_height = request.sheet_height or request.frame_grid[0] * request.cell_size
        full_prompt = (
            f"{request.prompt}\n"
            f"Create one image only. Target canvas: {sheet_width}x{sheet_height}. "
            f"Frame layout: {request.frame_layout}, grid={request.frame_grid[0]}x{request.frame_grid[1]}. "
            "Strictly follow the grid. Return only the image."
        )
        if request.negative_prompt:
            full_prompt = f"{full_prompt}\navoid: {request.negative_prompt}"
        full_prompt = " ".join(full_prompt.split())

        payload: dict[str, Any] = {
            "model": request.model,
            "prompt": full_prompt,
            "n": 1,
            "size": self._resolve_size(sheet_width, sheet_height, request.provider_options),
            "stream": True,
        }
        response_format = self._resolve_response_format(request.provider_options)
        if response_format:
            payload["response_format"] = response_format
        payload.update(request.provider_options)

        url = f"{self.base_url}/images/generations"
        debug_path = self._write_request_debug(
            request=request,
            index=index,
            url=url,
            payload=payload,
        )
        response = self._post_json(url, payload, request.timeout_seconds, debug_path)
        item = self._first_image_item(response)
        image_bytes = self._image_bytes_from_item(item)
        self._write_response_debug(debug_path, response, item)
        return GeneratedImage(
            index=index,
            image_bytes=image_bytes,
            mime_type="image/png",
            provider_metadata={
                "backend": "openai_image",
                "model": request.model,
                "reference_applied": False,
                "seed_applied": False,
                "requested_size": [sheet_width, sheet_height],
                "api_size": payload.get("size"),
                "response_format": payload.get("response_format"),
                "raw_response_keys": sorted(response.keys()),
            },
        )

    @staticmethod
    def _resolve_response_format(provider_options: dict[str, Any]) -> str | None:
        configured = provider_options.get("response_format")
        if isinstance(configured, str) and configured:
            return configured
        return None

    @staticmethod
    def _resolve_size(
        sheet_width: int,
        sheet_height: int,
        provider_options: dict[str, Any],
    ) -> str:
        configured = provider_options.get("size")
        if isinstance(configured, str) and configured:
            return configured
        if sheet_width == sheet_height:
            if sheet_width <= 512:
                return "512x512"
            return "1024x1024"
        return f"{sheet_width}x{sheet_height}"

    def _post_json(
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
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            timeout_seconds=timeout_seconds,
            error_label="OpenAI image API",
            debug_path=debug_path,
        )
        return result.data

    @staticmethod
    def _first_image_item(response: dict[str, Any]) -> dict[str, Any]:
        try:
            item = response["data"][0]
        except (KeyError, IndexError, TypeError) as exc:
            raise GeneratorError(f"Unexpected OpenAI image API response: {response}") from exc
        if not isinstance(item, dict):
            raise GeneratorError(f"Unexpected OpenAI image item: {item}")
        return item

    @staticmethod
    def _image_bytes_from_item(item: dict[str, Any]) -> bytes:
        if isinstance(item.get("b64_json"), str):
            try:
                return base64.b64decode(item["b64_json"])
            except ValueError as exc:
                raise GeneratorError("OpenAI image b64_json could not be decoded") from exc
        if isinstance(item.get("url"), str):
            try:
                with urllib.request.urlopen(item["url"], timeout=120) as resp:
                    return resp.read()
            except urllib.error.URLError as exc:
                raise GeneratorError(f"OpenAI image URL download failed: {exc.reason}") from exc
        raise GeneratorError(f"OpenAI image response did not include b64_json or url: {item}")

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
        data = {
            "created_at": datetime.now(UTC).isoformat(),
            "provider": "openai_image",
            "url": url,
            "method": "POST",
            "headers": {
                "Authorization": "Bearer <redacted>",
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
            "payload": payload,
        }
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    @staticmethod
    def _write_response_debug(
        debug_path: Path | None,
        response: dict[str, Any],
        item: dict[str, Any],
    ) -> None:
        item_summary = {
            key: (
                f"<base64 length={len(value)}>"
                if key == "b64_json" and isinstance(value, str)
                else value
            )
            for key, value in item.items()
        }
        OpenAIImageProvider._append_debug(
            debug_path,
            {
                "parsed_response": {
                    "top_level_keys": sorted(response.keys()),
                    "data_count": len(response.get("data") or []),
                    "first_item": item_summary,
                }
            },
        )

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
