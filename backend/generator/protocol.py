from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class ProviderCapabilities:
    supports_reference_images: bool = False
    supports_image_edit: bool = False
    supports_custom_size: bool = False
    supports_seed: bool = False


@dataclass
class GenerateRequest:
    prompt: str
    negative_prompt: str
    count: int
    model: str
    output_size: int
    timeout_seconds: int
    seed: int | None = None
    output_mime_type: str = "image/png"
    frame_layout: str = "single"
    frame_grid: tuple[int, int] = (1, 1)
    cell_size: int = 256
    sheet_width: int | None = None
    sheet_height: int | None = None
    style_lock: dict[str, Any] | None = None
    provider_options: dict[str, Any] = field(default_factory=dict)
    reference_images: list[bytes] = field(default_factory=list)
    debug_dir: str | None = None


@dataclass
class GeneratedImage:
    index: int
    image_bytes: bytes
    mime_type: str = "image/png"
    provider_metadata: dict[str, Any] = field(default_factory=dict)


class GeneratorProtocol(Protocol):
    capabilities: ProviderCapabilities

    async def generate(self, request: GenerateRequest) -> list[GeneratedImage]:
        ...

    async def edit(
        self,
        image_bytes: bytes,
        prompt: str,
        provider_options: dict[str, Any] | None = None,
    ) -> GeneratedImage:
        ...


class GeneratorError(RuntimeError):
    pass
