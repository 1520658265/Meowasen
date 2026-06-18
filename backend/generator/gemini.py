from __future__ import annotations

import asyncio
import base64
import re
import urllib.error
import urllib.request
from typing import Any

from .protocol import GeneratedImage, GenerateRequest, GeneratorError, ProviderCapabilities
from .streaming_http import post_json_stream


class GeminiOpenAICompatibleProvider:
    """OpenAI-compatible Gemini image generation via direct HTTP.

    This intentionally avoids the OpenAI Python SDK. Some relays expose Gemini
    image models through `/chat/completions` rather than `/images/generations`;
    this provider supports both and falls back to chat when the image endpoint
    rejects the model.
    """

    capabilities = ProviderCapabilities(
        supports_reference_images=False,
        supports_image_edit=False,
        supports_custom_size=True,
        supports_seed=False,
    )

    def __init__(self, api_key: str, base_url: str) -> None:
        if not api_key:
            raise GeneratorError("Missing API key for Gemini relay")
        if not base_url:
            raise GeneratorError("Missing relay base_url")
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
            raise GeneratorError("; ".join(errors) if errors else "Image API returned no images")
        if errors:
            images[0].provider_metadata["partial_errors"] = errors
        return images

    async def edit(
        self,
        image_bytes: bytes,
        prompt: str,
        provider_options: dict[str, Any] | None = None,
    ) -> GeneratedImage:
        raise GeneratorError("Image edit is not supported by GeminiOpenAICompatibleProvider")

    async def _generate_one(self, request: GenerateRequest, index: int) -> GeneratedImage:
        return await asyncio.to_thread(self._generate_one_sync, request, index)

    def _generate_one_sync(self, request: GenerateRequest, index: int) -> GeneratedImage:
        try:
            return self._generate_one_images_endpoint(request, index)
        except GeneratorError as exc:
            if "not supported model for image generation" not in str(exc):
                raise
            return self._generate_one_chat_endpoint(request, index, fallback_reason=str(exc))

    def _generate_one_images_endpoint(self, request: GenerateRequest, index: int) -> GeneratedImage:
        full_prompt = request.prompt
        if request.negative_prompt:
            full_prompt = f"{full_prompt}\navoid: {request.negative_prompt}"

        payload: dict[str, Any] = {
            "model": request.model,
            "prompt": full_prompt,
            "n": 1,
            "stream": True,
        }
        response_format = request.provider_options.get("response_format")
        if isinstance(response_format, str) and response_format:
            payload["response_format"] = response_format

        sheet_width = request.sheet_width or request.frame_grid[1] * request.cell_size
        sheet_height = request.sheet_height or request.frame_grid[0] * request.cell_size
        # Many OpenAI-compatible relays accept size as WIDTHxHEIGHT. If a relay
        # ignores it, downstream splitter records the actual image size.
        payload["size"] = f"{sheet_width}x{sheet_height}"
        payload.update(request.provider_options)

        url = f"{self.base_url}/images/generations"
        response = self._post_json(url, payload, request.timeout_seconds)

        try:
            item = response["data"][0]
            image_bytes = self._image_bytes_from_item(item)
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise GeneratorError(f"Unexpected image API response: {response}") from exc

        return GeneratedImage(
            index=index,
            image_bytes=image_bytes,
            mime_type="image/png",
            provider_metadata={
                "backend": "gemini",
                "model": request.model,
                "reference_applied": False,
                "seed_applied": False,
                "requested_size": [sheet_width, sheet_height],
                "raw_response_keys": sorted(response.keys()),
            },
        )

    def _generate_one_chat_endpoint(
        self,
        request: GenerateRequest,
        index: int,
        fallback_reason: str | None = None,
    ) -> GeneratedImage:
        sheet_width = request.sheet_width or request.frame_grid[1] * request.cell_size
        sheet_height = request.sheet_height or request.frame_grid[0] * request.cell_size
        full_prompt = (
            f"{request.prompt}\n"
            f"Create one image only. Target canvas: {sheet_width}x{sheet_height}. "
            f"Frame layout: {request.frame_layout}, grid={request.frame_grid[0]}x{request.frame_grid[1]}. "
            "Return the generated image as an inline image."
        )
        if request.negative_prompt:
            full_prompt = f"{full_prompt}\navoid: {request.negative_prompt}"

        payload: dict[str, Any] = {
            "model": request.model,
            "messages": [{"role": "user", "content": full_prompt}],
        }
        payload.update(request.provider_options)

        url = f"{self.base_url}/chat/completions"
        response = self._post_json(url, payload, request.timeout_seconds)
        content = self._extract_message_content(response)
        mime_type, image_bytes = self._extract_inline_image(content)
        return GeneratedImage(
            index=index,
            image_bytes=image_bytes,
            mime_type=mime_type,
            provider_metadata={
                "backend": "gemini",
                "endpoint": "chat/completions",
                "model": request.model,
                "reference_applied": False,
                "seed_applied": False,
                "requested_size": [sheet_width, sheet_height],
                "fallback_reason": fallback_reason,
                "raw_response_keys": sorted(response.keys()),
            },
        )

    def _post_json(self, url: str, payload: dict[str, Any], timeout_seconds: int) -> dict[str, Any]:
        result = post_json_stream(
            url=url,
            payload=payload,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            timeout_seconds=timeout_seconds,
            error_label="Image API",
        )
        return result.data

    @staticmethod
    def _image_bytes_from_item(item: dict[str, Any]) -> bytes:
        if isinstance(item.get("b64_json"), str):
            try:
                return base64.b64decode(item["b64_json"])
            except ValueError as exc:
                raise GeneratorError("Image API b64_json could not be decoded") from exc
        if isinstance(item.get("url"), str):
            try:
                with urllib.request.urlopen(item["url"], timeout=120) as resp:
                    return resp.read()
            except urllib.error.URLError as exc:
                raise GeneratorError(f"Image API URL download failed: {exc.reason}") from exc
        raise GeneratorError(f"Image API response did not include b64_json or url: {item}")

    @staticmethod
    def _extract_message_content(response: dict[str, Any]) -> str:
        try:
            content = response["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise GeneratorError(f"Unexpected chat completion response: {response}") from exc
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, dict):
                    if isinstance(item.get("text"), str):
                        parts.append(item["text"])
                    image_url = item.get("image_url")
                    if isinstance(image_url, dict) and isinstance(image_url.get("url"), str):
                        parts.append(image_url["url"])
                elif isinstance(item, str):
                    parts.append(item)
            return "\n".join(parts)
        raise GeneratorError(f"Unsupported chat completion content type: {type(content).__name__}")

    @staticmethod
    def _extract_inline_image(content: str) -> tuple[str, bytes]:
        match = re.search(r"data:(image/[a-zA-Z0-9.+-]+);base64,([A-Za-z0-9+/=\r\n]+)", content)
        if not match:
            raise GeneratorError("Chat completion did not include an inline base64 image")
        mime_type = match.group(1)
        b64_data = re.sub(r"\s+", "", match.group(2))
        try:
            return mime_type, base64.b64decode(b64_data)
        except ValueError as exc:
            raise GeneratorError("Inline image base64 could not be decoded") from exc
