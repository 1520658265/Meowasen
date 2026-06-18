from __future__ import annotations

import base64
import io
import json
from dataclasses import dataclass
from pathlib import Path

from PIL import Image


@dataclass(frozen=True)
class StyleLock:
    palette_b64: str
    histogram_json: str
    colors: int

    def to_dict(self) -> dict:
        return {
            "palette_b64": self.palette_b64,
            "histogram_json": self.histogram_json,
            "colors": self.colors,
        }


def palette_image_from_b64(palette_b64: str) -> Image.Image:
    data = base64.b64decode(palette_b64)
    return Image.open(io.BytesIO(data)).convert("P")


class StyleLockExtractor:
    def extract_from_paths(self, image_paths: list[Path], colors: int = 16) -> StyleLock:
        images = [Image.open(path).convert("RGBA") for path in image_paths if path.exists()]
        if not images:
            raise ValueError("No images available for style lock extraction")
        swatch = self._build_swatch(images)
        quantized = swatch.convert("RGB").quantize(colors=colors, method=Image.Quantize.MEDIANCUT)
        palette_image = Image.new("P", (1, 1))
        palette = quantized.getpalette() or []
        palette_image.putpalette((palette + [0] * 768)[:768])

        buffer = io.BytesIO()
        palette_image.save(buffer, format="PNG")
        histogram = self._histogram(quantized.convert("RGB"), colors)
        return StyleLock(
            palette_b64=base64.b64encode(buffer.getvalue()).decode("ascii"),
            histogram_json=json.dumps(histogram, ensure_ascii=False),
            colors=colors,
        )

    @staticmethod
    def _build_swatch(images: list[Image.Image]) -> Image.Image:
        visible_pixels: list[tuple[int, int, int]] = []
        for image in images:
            rgba = image.copy()
            rgba.thumbnail((64, 64), Image.Resampling.NEAREST)
            visible_pixels.extend(pixel[:3] for pixel in rgba.getdata() if pixel[3] > 0)
        if not visible_pixels:
            raise ValueError("No visible pixels available for style lock extraction")
        swatch = Image.new("RGB", (len(visible_pixels), 1))
        swatch.putdata(visible_pixels)
        return swatch

    @staticmethod
    def _histogram(image: Image.Image, colors: int) -> dict[str, int]:
        small = image.copy()
        small.thumbnail((128, 128), Image.Resampling.NEAREST)
        quantized = small.quantize(colors=colors, method=Image.Quantize.MEDIANCUT).convert("RGB")
        counts: dict[str, int] = {}
        for rgb in quantized.getdata():
            key = "#%02x%02x%02x" % rgb
            counts[key] = counts.get(key, 0) + 1
        return dict(sorted(counts.items(), key=lambda item: item[1], reverse=True))
