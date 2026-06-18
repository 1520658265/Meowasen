from __future__ import annotations

import asyncio
import hashlib
import io

from PIL import Image, ImageDraw

from .protocol import GeneratedImage, GenerateRequest, ProviderCapabilities


class MockImageProvider:
    """Deterministic local provider for pipeline verification."""

    capabilities = ProviderCapabilities(
        supports_reference_images=False,
        supports_image_edit=False,
        supports_custom_size=True,
        supports_seed=True,
    )

    async def generate(self, request: GenerateRequest) -> list[GeneratedImage]:
        return [
            await asyncio.to_thread(self._generate_one, request, index)
            for index in range(request.count)
        ]

    async def edit(
        self,
        image_bytes: bytes,
        prompt: str,
        provider_options: dict | None = None,
    ) -> GeneratedImage:
        return GeneratedImage(index=0, image_bytes=image_bytes, provider_metadata={"backend": "mock"})

    def _generate_one(self, request: GenerateRequest, index: int) -> GeneratedImage:
        rows, cols = request.frame_grid
        cell = request.cell_size
        width = request.sheet_width or cols * cell
        height = request.sheet_height or rows * cell
        background = (255, 0, 255, 255) if "RGB(255,0,255)" in request.prompt or "#FF00FF" in request.prompt else (245, 245, 245, 255)
        image = Image.new("RGBA", (width, height), background)
        draw = ImageDraw.Draw(image)

        digest = hashlib.sha256(f"{request.prompt}|{index}|{request.seed}".encode("utf-8")).digest()
        palette = [
            (40 + digest[0] % 120, 70 + digest[1] % 120, 90 + digest[2] % 120, 255),
            (120 + digest[3] % 100, 60 + digest[4] % 100, 70 + digest[5] % 100, 255),
            (210, 190, 90, 255),
            (35, 35, 40, 255),
        ]

        cell_w = width // cols
        cell_h = height // rows
        for row in range(rows):
            for col in range(cols):
                x0 = col * cell_w
                y0 = row * cell_h
                x1 = x0 + cell_w
                y1 = y0 + cell_h
                cx = (x0 + x1) // 2
                cy = (y0 + y1) // 2
                w = int(cell_w * 0.30)
                h = int(cell_h * 0.36)
                offset = ((row * cols + col) % 4 - 1) * max(2, cell_w // 32)
                draw.ellipse([cx - w // 3, cy - h // 2 + offset, cx + w // 3, cy - h // 8 + offset], fill=palette[0], outline=palette[3], width=3)
                draw.rounded_rectangle([cx - w // 2, cy - h // 8, cx + w // 2, cy + h // 2], radius=8, fill=palette[1], outline=palette[3], width=3)
                draw.rectangle([cx - w // 3, cy + h // 2, cx - w // 8, cy + h // 2 + h // 5], fill=palette[3])
                draw.rectangle([cx + w // 8, cy + h // 2, cx + w // 3, cy + h // 2 + h // 5], fill=palette[3])
                draw.polygon([(cx + w // 2, cy), (cx + w // 2 + w // 4, cy + h // 4), (cx + w // 2, cy + h // 3)], fill=palette[2], outline=palette[3])

        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return GeneratedImage(
            index=index,
            image_bytes=buffer.getvalue(),
            provider_metadata={
                "backend": "mock",
                "model": "mock",
                "reference_applied": False,
                "seed_applied": request.seed is not None,
                "requested_size": [width, height],
            },
        )
