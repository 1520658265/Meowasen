from __future__ import annotations

import os

from backend.config import AppConfig, get_api_key

from .gemini import GeminiOpenAICompatibleProvider
from .gemini_native import GeminiNativeProvider
from .mock import MockImageProvider
from .openai_image import OpenAIImageProvider
from .protocol import GeneratorError, GeneratorProtocol


def create_provider(config: AppConfig, backend_override: str | None = None) -> GeneratorProtocol:
    backend = (backend_override or config.generator.backend).lower()
    if backend == "mock":
        return MockImageProvider()
    if backend == "gemini":
        api_key = get_api_key(config)
        if not api_key:
            raise GeneratorError(
                f"Missing API key env var {config.relay.api_key_env}. "
                "Set it in .env or use --backend mock for local pipeline tests."
            )
        return GeminiOpenAICompatibleProvider(api_key=api_key, base_url=config.relay.base_url)
    if backend == "gemini_native":
        api_key = config.generator.provider_options.get("api_key")
        if not isinstance(api_key, str) or not api_key:
            api_key = get_api_key(config)
        if not api_key:
            raise GeneratorError(
                f"Missing API key env var {config.relay.api_key_env}. "
                "Set it in .env or use --backend mock for local pipeline tests."
            )
        base_url = config.generator.provider_options.get(
            "base_url",
            config.relay.base_url,
        )
        return GeminiNativeProvider(api_key=api_key, base_url=str(base_url))
    if backend == "openai_image":
        api_key = os.environ.get("GPT_API_KEY") or get_api_key(config)
        if not api_key:
            raise GeneratorError(
                f"Missing API key env var GPT_API_KEY or {config.relay.api_key_env}. "
                "Set it in .env or use --backend mock for local pipeline tests."
            )
        return OpenAIImageProvider(api_key=api_key, base_url=config.relay.base_url)
    raise GeneratorError(f"Unsupported generator backend: {backend}")
