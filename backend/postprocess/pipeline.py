from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageFilter

from .background import (
    connected_background_mask,
    estimate_background_rgb,
    estimate_edge_background_palette,
    foreground_mask_from_background,
    remove_edge_connected_background,
)
from .style_lock import palette_image_from_b64


@dataclass(frozen=True)
class ProcessOptions:
    output_size: int = 128
    palette_colors: int = 16
    canvas_padding: int = 4
    style_lock_palette_b64: str | None = None
    mode: str = "sprite"
    chroma_key_rgb: tuple[int, int, int] | None = (255, 0, 255)
    chroma_key_tolerance: int = 80
    hard_alpha: bool = True
    min_component_area: int = 16
    rebuild_outline: bool = False


@dataclass(frozen=True)
class ProcessResult:
    image: Image.Image
    bg_removed: bool
    status: str
    error: str | None = None
    metrics: dict[str, Any] | None = None


class PostProcessor:
    def process(self, image: Image.Image, options: ProcessOptions) -> ProcessResult:
        try:
            if options.mode == "tile":
                return self.process_tile(image, options)
            if options.mode != "sprite":
                raise ValueError(f"Unsupported process mode: {options.mode}")
            rgba = self.normalize_rgba(image)
            removed, bg_removed, cleanup_metrics = self.remove_background(
                rgba,
                chroma_key_rgb=options.chroma_key_rgb,
                chroma_key_tolerance=options.chroma_key_tolerance,
                min_component_area=options.min_component_area,
                hard_alpha=options.hard_alpha,
            )
            cropped = self.crop_to_subject(removed)
            centered = self.center_on_canvas(cropped, options.output_size, options.canvas_padding)
            if options.chroma_key_rgb is not None:
                centered = self.clean_chroma_fringe(
                    centered,
                    key_rgb=options.chroma_key_rgb,
                    tolerance=options.chroma_key_tolerance,
                )
            if options.hard_alpha:
                centered = self.harden_alpha(centered)
            if options.rebuild_outline:
                centered = self.rebuild_soft_outline(centered)
            final_metrics = self.sprite_quality_metrics(centered, key_rgb=options.chroma_key_rgb)
            quantized = self.quantize_palette(
                centered,
                colors=options.palette_colors,
                style_lock_palette_b64=options.style_lock_palette_b64,
            )
            return ProcessResult(
                image=quantized,
                bg_removed=bg_removed,
                status="done",
                metrics={**cleanup_metrics, **final_metrics},
            )
        except Exception as exc:  # Keep frame-level failures isolated.
            return ProcessResult(image=self.normalize_rgba(image), bg_removed=False, status="failed", error=str(exc))

    def process_tile(self, image: Image.Image, options: ProcessOptions) -> ProcessResult:
        rgba = self.normalize_rgba(image)
        resized = self.resize_tile(rgba, options.output_size)
        quantized = self.quantize_palette(
            resized,
            colors=options.palette_colors,
            style_lock_palette_b64=options.style_lock_palette_b64,
        )
        return ProcessResult(image=quantized, bg_removed=False, status="done")

    @staticmethod
    def normalize_rgba(image: Image.Image) -> Image.Image:
        return image.convert("RGBA")

    @staticmethod
    def remove_background(
        image: Image.Image,
        chroma_key_rgb: tuple[int, int, int] | None = (255, 0, 255),
        chroma_key_tolerance: int = 28,
        min_component_area: int = 16,
        hard_alpha: bool = True,
    ) -> tuple[Image.Image, bool, dict[str, Any]]:
        PostProcessor.load_postprocess_env()
        if chroma_key_rgb is not None:
            keyed = PostProcessor.remove_chroma_key_background(
                image,
                key_rgb=chroma_key_rgb,
                tolerance=chroma_key_tolerance,
                min_component_area=min_component_area,
                hard_alpha=hard_alpha,
            )
            if PostProcessor._has_transparency(keyed):
                return keyed, True, PostProcessor.sprite_quality_metrics(keyed, key_rgb=chroma_key_rgb)
        if os.environ.get("MEOWASEN_ENABLE_REMBG") != "1":
            fallback = PostProcessor.remove_flat_background(image)
            return fallback, PostProcessor._has_transparency(fallback), PostProcessor.sprite_quality_metrics(
                fallback,
                key_rgb=chroma_key_rgb,
            )
        try:
            removed = PostProcessor.remove_with_rembg(image)
            return removed, True, PostProcessor.sprite_quality_metrics(removed, key_rgb=chroma_key_rgb)
        except ModuleNotFoundError:
            fallback = PostProcessor.remove_flat_background(image)
            return fallback, PostProcessor._has_transparency(fallback), PostProcessor.sprite_quality_metrics(
                fallback,
                key_rgb=chroma_key_rgb,
            )
        except Exception:
            fallback = PostProcessor.remove_flat_background(image)
            return fallback, PostProcessor._has_transparency(fallback), PostProcessor.sprite_quality_metrics(
                fallback,
                key_rgb=chroma_key_rgb,
            )

    @staticmethod
    def remove_with_rembg(image: Image.Image) -> Image.Image:
        from rembg import remove  # type: ignore

        removed = remove(image, session=PostProcessor.rembg_session())
        if isinstance(removed, bytes):
            from io import BytesIO

            removed = Image.open(BytesIO(removed))
        return removed.convert("RGBA")

    @staticmethod
    def rembg_session() -> Any:
        global _REMBG_SESSION
        if _REMBG_SESSION is None:
            from rembg import new_session  # type: ignore

            model_name = os.environ.get("MEOWASEN_REMBG_MODEL", "isnet-anime")
            _REMBG_SESSION = new_session(model_name)
        return _REMBG_SESSION

    @staticmethod
    def load_postprocess_env() -> None:
        env_path = Path(__file__).resolve().parents[2] / ".env"
        if not env_path.exists():
            return
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))

    @staticmethod
    def remove_flat_background(image: Image.Image, tolerance: int = 24) -> Image.Image:
        return remove_edge_connected_background(image, tolerance=max(36, tolerance))

    @staticmethod
    def remove_chroma_key_background(
        image: Image.Image,
        key_rgb: tuple[int, int, int] = (255, 0, 255),
        tolerance: int = 80,
        min_component_area: int = 16,
        hard_alpha: bool = True,
    ) -> Image.Image:
        rgba = image.convert("RGBA")
        palette = PostProcessor.chroma_key_palette(rgba, key_rgb=key_rgb)
        mask = connected_background_mask(rgba, tolerance=tolerance, palette=palette)
        if mask.getbbox() is None:
            return rgba

        expanded_mask = PostProcessor.expand_background_mask(mask)
        output = PostProcessor.apply_background_mask(rgba, expanded_mask)
        output = PostProcessor.clean_chroma_fringe(output, key_rgb=key_rgb, tolerance=tolerance)
        output = PostProcessor.keep_foreground_components(output, min_area=min_component_area)
        if hard_alpha:
            output = PostProcessor.harden_alpha(output)
        return PostProcessor.clear_transparent_rgb(output)

    @staticmethod
    def chroma_key_palette(
        image: Image.Image,
        key_rgb: tuple[int, int, int] = (255, 0, 255),
    ) -> list[tuple[int, int, int]]:
        palette = [key_rgb]
        for color in estimate_edge_background_palette(image, max_colors=6, bucket_size=8):
            r, g, b = color
            looks_like_key = r >= 180 and b >= 180 and g <= 120 and abs(r - b) <= 80
            if looks_like_key and color not in palette:
                palette.append(color)
        return palette

    @staticmethod
    def clean_chroma_fringe(
        image: Image.Image,
        key_rgb: tuple[int, int, int] = (255, 0, 255),
        tolerance: int = 80,
    ) -> Image.Image:
        rgba = image.convert("RGBA")
        alpha = rgba.getchannel("A")
        transparent_neighborhood = alpha.filter(ImageFilter.MinFilter(5))
        visible_neighborhood = alpha.filter(ImageFilter.MaxFilter(3))
        output_pixels = []
        for pixel, nearby_alpha, nearby_visible in zip(
            rgba.getdata(),
            transparent_neighborhood.getdata(),
            visible_neighborhood.getdata(),
            strict=True,
        ):
            r, g, b, a = pixel
            if a <= 8:
                output_pixels.append((0, 0, 0, 0))
                continue
            if nearby_visible <= 8:
                output_pixels.append((0, 0, 0, 0))
                continue
            near_key = (
                abs(r - key_rgb[0]) <= tolerance
                and abs(g - key_rgb[1]) <= tolerance
                and abs(b - key_rgb[2]) <= tolerance
            )
            looks_like_key = r >= 170 and b >= 150 and g <= 120 and abs(r - b) <= 90
            # Generated models often darken antialiased key-color pixels into
            # purple/navy fringes. Remove those only near transparent regions so
            # blue-gray armor and internal shadows are not erased.
            hue_purple_fringe = (
                nearby_alpha < 255
                and b > g + 24
                and r > g + 12
                and 45 <= r + b <= 430
                and g <= 140
            )
            if near_key or looks_like_key or hue_purple_fringe:
                output_pixels.append((0, 0, 0, 0))
            else:
                output_pixels.append(pixel)
        output = Image.new("RGBA", rgba.size, (0, 0, 0, 0))
        output.putdata(output_pixels)
        return output

    @staticmethod
    def expand_background_mask(mask: Image.Image) -> Image.Image:
        """Close thin antialiased key-color remnants along the cutout edge."""

        binary = mask.convert("L").point(lambda value: 255 if value > 0 else 0)
        # Dilate one pixel into the fringe, then close pin holes in the mask.
        expanded = binary.filter(ImageFilter.MaxFilter(3))
        return expanded.filter(ImageFilter.MinFilter(3))

    @staticmethod
    def apply_background_mask(image: Image.Image, mask: Image.Image) -> Image.Image:
        rgba = image.convert("RGBA")
        output_pixels = []
        for pixel, background in zip(rgba.getdata(), mask.getdata(), strict=True):
            if background:
                output_pixels.append((0, 0, 0, 0))
            else:
                output_pixels.append(pixel)
        output = Image.new("RGBA", rgba.size, (0, 0, 0, 0))
        output.putdata(output_pixels)
        return output

    @staticmethod
    def harden_alpha(image: Image.Image, threshold: int = 128) -> Image.Image:
        rgba = image.convert("RGBA")
        alpha = rgba.getchannel("A").point(lambda value: 255 if value >= threshold else 0)
        output = rgba.copy()
        output.putalpha(alpha)
        return PostProcessor.clear_transparent_rgb(output)

    @staticmethod
    def keep_foreground_components(image: Image.Image, min_area: int = 16) -> Image.Image:
        rgba = image.convert("RGBA")
        alpha = rgba.getchannel("A").point(lambda value: 255 if value > 8 else 0)
        components = PostProcessor.connected_components(alpha, min_area=max(1, min_area))
        if not components:
            return rgba
        keep = Image.new("L", rgba.size, 0)
        keep_pixels = keep.load()
        alpha_pixels = alpha.load()
        width, height = alpha.size
        allowed_boxes = [(left, top, right, bottom) for left, top, right, bottom, _area in components]
        for left, top, right, bottom in allowed_boxes:
            for y in range(top, bottom):
                for x in range(left, right):
                    if alpha_pixels[x, y] > 0:
                        keep_pixels[x, y] = 255
        output = rgba.copy()
        output.putalpha(keep)
        return PostProcessor.clear_transparent_rgb(output.crop((0, 0, width, height)))

    @staticmethod
    def rebuild_soft_outline(image: Image.Image, color: tuple[int, int, int] = (32, 24, 36)) -> Image.Image:
        rgba = image.convert("RGBA")
        alpha = rgba.getchannel("A").point(lambda value: 255 if value > 8 else 0)
        grown = alpha.filter(ImageFilter.MaxFilter(3))
        outline = ImageChops.subtract(grown, alpha)
        outline_layer = Image.new("RGBA", rgba.size, (*color, 255))
        outline_layer.putalpha(outline)
        outline_layer.alpha_composite(rgba)
        return outline_layer

    @staticmethod
    def connected_components(
        mask: Image.Image,
        min_area: int = 1,
    ) -> list[tuple[int, int, int, int, int]]:
        binary = mask.convert("L")
        width, height = binary.size
        data = binary.load()
        visited = bytearray(width * height)
        components: list[tuple[int, int, int, int, int]] = []
        for y in range(height):
            for x in range(width):
                index = y * width + x
                if visited[index] or data[x, y] <= 0:
                    continue
                visited[index] = 1
                stack = [(x, y)]
                min_x = max_x = x
                min_y = max_y = y
                area = 0
                while stack:
                    current_x, current_y = stack.pop()
                    area += 1
                    min_x = min(min_x, current_x)
                    min_y = min(min_y, current_y)
                    max_x = max(max_x, current_x)
                    max_y = max(max_y, current_y)
                    for next_x, next_y in (
                        (current_x - 1, current_y),
                        (current_x + 1, current_y),
                        (current_x, current_y - 1),
                        (current_x, current_y + 1),
                    ):
                        if next_x < 0 or next_y < 0 or next_x >= width or next_y >= height:
                            continue
                        next_index = next_y * width + next_x
                        if visited[next_index] or data[next_x, next_y] <= 0:
                            continue
                        visited[next_index] = 1
                        stack.append((next_x, next_y))
                if area >= min_area:
                    components.append((min_x, min_y, max_x + 1, max_y + 1, area))
        return components

    @staticmethod
    def sprite_quality_metrics(
        image: Image.Image,
        key_rgb: tuple[int, int, int] | None = (255, 0, 255),
    ) -> dict[str, Any]:
        rgba = image.convert("RGBA")
        alpha = rgba.getchannel("A")
        visible = [pixel for pixel in rgba.getdata() if pixel[3] > 8]
        key_residual = 0
        broad_key_residual = 0
        if key_rgb is not None:
            for r, g, b, _a in visible:
                near_key = (
                    abs(r - key_rgb[0]) <= 36
                    and abs(g - key_rgb[1]) <= 36
                    and abs(b - key_rgb[2]) <= 36
                )
                broad_key = r >= 150 and b >= 130 and g <= 140 and abs(r - b) <= 110
                if near_key:
                    key_residual += 1
                if broad_key:
                    broad_key_residual += 1
        components = PostProcessor.connected_components(
            alpha.point(lambda value: 255 if value > 8 else 0),
            min_area=1,
        )
        tiny_components = sum(1 for component in components if component[4] < 16)
        edge_visible = PostProcessor.edge_visible_pixels(alpha)
        bbox = alpha.point(lambda value: 255 if value > 8 else 0).getbbox()
        return {
            "visible_pixels": len(visible),
            "key_residual_pixels": key_residual,
            "broad_key_residual_pixels": broad_key_residual,
            "component_count": len(components),
            "tiny_component_count": tiny_components,
            "edge_visible_pixels": edge_visible,
            "alpha_bbox": None if bbox is None else list(bbox),
        }

    @staticmethod
    def edge_visible_pixels(alpha: Image.Image) -> int:
        channel = alpha.convert("L")
        width, height = channel.size
        if width <= 0 or height <= 0:
            return 0
        count = 0
        pixels = channel.load()
        for x in range(width):
            if pixels[x, 0] > 8:
                count += 1
            if height > 1 and pixels[x, height - 1] > 8:
                count += 1
        for y in range(1, max(1, height - 1)):
            if pixels[0, y] > 8:
                count += 1
            if width > 1 and pixels[width - 1, y] > 8:
                count += 1
        return count

    @staticmethod
    def _has_transparency(image: Image.Image) -> bool:
        alpha = image.convert("RGBA").getchannel("A")
        extrema = alpha.getextrema()
        return extrema[0] < 255

    @staticmethod
    def crop_to_subject(image: Image.Image) -> Image.Image:
        rgba = image.convert("RGBA")
        mask = foreground_mask_from_background(rgba)
        alpha_bbox = mask.getbbox()
        if alpha_bbox and alpha_bbox != (0, 0, rgba.width, rgba.height):
            return rgba.crop(alpha_bbox)

        bg_rgb = estimate_background_rgb(rgba)
        bg = Image.new("RGBA", rgba.size, (*bg_rgb, 255))
        from PIL import ImageChops

        diff = ImageChops.difference(rgba, bg).convert("L")
        mask = diff.point(lambda value: 255 if value > 18 else 0)
        border = max(2, min(rgba.width, rgba.height) // 48)
        from PIL import ImageDraw

        draw = ImageDraw.Draw(mask)
        draw.rectangle([0, 0, rgba.width - 1, border], fill=0)
        draw.rectangle([0, rgba.height - border - 1, rgba.width - 1, rgba.height - 1], fill=0)
        draw.rectangle([0, 0, border, rgba.height - 1], fill=0)
        draw.rectangle([rgba.width - border - 1, 0, rgba.width - 1, rgba.height - 1], fill=0)
        bbox = mask.getbbox()
        return rgba.crop(bbox) if bbox else rgba

    @staticmethod
    def center_on_canvas(image: Image.Image, output_size: int, padding: int) -> Image.Image:
        rgba = PostProcessor.clear_transparent_rgb(image)
        max_subject = max(1, output_size - padding * 2)
        width, height = rgba.size
        scale = min(max_subject / max(width, 1), max_subject / max(height, 1), 1.0)
        new_size = (max(1, round(width * scale)), max(1, round(height * scale)))
        resample = Image.Resampling.NEAREST
        resized = PostProcessor.clear_transparent_rgb(rgba.resize(new_size, resample))
        canvas = Image.new("RGBA", (output_size, output_size), (0, 0, 0, 0))
        x = (output_size - resized.width) // 2
        y = (output_size - resized.height) // 2
        canvas.alpha_composite(resized, (x, y))
        return canvas

    @staticmethod
    def resize_tile(image: Image.Image, output_size: int) -> Image.Image:
        rgba = image.convert("RGBA")
        if output_size <= 0:
            return PostProcessor.clear_transparent_rgb(rgba)
        if rgba.size == (output_size, output_size):
            return PostProcessor.clear_transparent_rgb(rgba)
        return PostProcessor.clear_transparent_rgb(
            rgba.resize((output_size, output_size), Image.Resampling.NEAREST)
        )

    @staticmethod
    def clear_transparent_rgb(image: Image.Image) -> Image.Image:
        rgba = image.convert("RGBA")
        pixels = [
            (0, 0, 0, 0) if pixel[3] <= 8 else pixel
            for pixel in rgba.getdata()
        ]
        cleaned = Image.new("RGBA", rgba.size, (0, 0, 0, 0))
        cleaned.putdata(pixels)
        return cleaned

    @staticmethod
    def quantize_palette(
        image: Image.Image,
        colors: int,
        style_lock_palette_b64: str | None = None,
    ) -> Image.Image:
        rgba = image.convert("RGBA")
        if colors <= 0:
            return rgba
        alpha = rgba.getchannel("A")
        rgb = Image.new("RGB", rgba.size, (0, 0, 0))
        rgb.paste(rgba.convert("RGB"), mask=alpha)

        if style_lock_palette_b64:
            palette_image = palette_image_from_b64(style_lock_palette_b64)
            quantized = rgb.quantize(palette=palette_image, dither=Image.Dither.NONE).convert("RGB")
        else:
            quantized = rgb.quantize(
                colors=max(2, colors),
                method=Image.Quantize.MEDIANCUT,
                dither=Image.Dither.NONE,
            ).convert("RGB")

        out = quantized.convert("RGBA")
        out.putalpha(alpha)
        return out


def save_png(image: Image.Image, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, format="PNG")


_REMBG_SESSION: Any | None = None
