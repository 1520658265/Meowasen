from __future__ import annotations

import argparse
import json
from pathlib import Path

from PIL import Image, ImageChops, ImageFilter


ROOT_DIR = Path(__file__).resolve().parents[1]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Convert a single character image into a pixel-style RPG sprite base.")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--sizes", default="192,128")
    parser.add_argument("--palette-colors", type=int, default=32)
    parser.add_argument("--bg-tolerance", type=int, default=30)
    parser.add_argument("--padding-ratio", type=float, default=0.10)
    parser.add_argument("--foot-margin", type=int, default=5)
    parser.add_argument("--fringe-tolerance", type=int, default=56)
    parser.add_argument("--edge-contract", type=int, default=0)
    parser.add_argument("--highlight-restore", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--highlight-fraction", type=float, default=0.035)
    parser.add_argument("--highlight-min-luma", type=int, default=138)
    parser.add_argument("--highlight-local-contrast", type=int, default=10)
    parser.add_argument("--outline", action=argparse.BooleanOptionalAction, default=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    input_path = _resolve(args.input)
    output_dir = _resolve(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    source = Image.open(input_path).convert("RGBA")
    bg_color = estimate_edge_color(source)
    cutout = remove_light_edge_background(source, tolerance=args.bg_tolerance, bg_color=bg_color)
    cutout = clean_light_fringe(cutout, bg_color=bg_color, tolerance=args.fringe_tolerance)
    cutout = harden_alpha(cutout)
    cutout = clear_transparent_rgb(cutout)
    bbox = cutout.getchannel("A").getbbox()
    if bbox is None:
        raise RuntimeError("No foreground found after background removal")

    cropped = cutout.crop(bbox)
    sizes = [int(item.strip()) for item in args.sizes.split(",") if item.strip()]
    outputs: list[str] = []
    for size in sizes:
        sprite = fit_to_sprite_canvas(
            cropped,
            size=size,
            padding_ratio=args.padding_ratio,
            foot_margin=args.foot_margin,
        )
        if args.edge_contract > 0:
            sprite = contract_alpha(sprite, pixels=args.edge_contract)
        if args.outline:
            sprite = add_pixel_outline(sprite)
        pixel = quantize_rgba(sprite, colors=args.palette_colors)
        if args.highlight_restore:
            pixel = restore_pixel_highlights(
                pixel,
                reference=sprite,
                max_fraction=args.highlight_fraction,
                min_luma=args.highlight_min_luma,
                local_contrast=args.highlight_local_contrast,
            )
        path = output_dir / f"pixel_sprite_{size}.png"
        pixel.save(path)
        outputs.append(str(path))

        checker = checker_preview(pixel, scale=max(2, 512 // size))
        checker_path = output_dir / f"pixel_sprite_{size}_preview.png"
        checker.save(checker_path)
        outputs.append(str(checker_path))

    crop_path = output_dir / "cutout_source.png"
    cutout.crop(bbox).save(crop_path)
    meta = {
        "input": str(input_path),
        "output_dir": str(output_dir),
        "source_size": list(source.size),
        "foreground_bbox": list(bbox),
        "palette_colors": args.palette_colors,
        "sizes": sizes,
        "outputs": outputs,
    }
    (output_dir / "pixelize_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(meta, ensure_ascii=False, indent=2))
    return 0


def _resolve(path: str) -> Path:
    result = Path(path)
    if result.is_absolute():
        return result
    return ROOT_DIR / result


def remove_light_edge_background(
    image: Image.Image,
    tolerance: int,
    bg_color: tuple[int, int, int] | None = None,
) -> Image.Image:
    rgba = image.convert("RGBA")
    bg = bg_color or estimate_edge_color(rgba)
    width, height = rgba.size
    pixels = list(rgba.getdata())
    visited = bytearray(width * height)
    stack: list[int] = []

    def is_bg(index: int) -> bool:
        r, g, b, a = pixels[index]
        if a == 0:
            return True
        return max(abs(r - bg[0]), abs(g - bg[1]), abs(b - bg[2])) <= tolerance

    def seed(index: int) -> None:
        if not visited[index] and is_bg(index):
            visited[index] = 1
            stack.append(index)

    for x in range(width):
        seed(x)
        seed((height - 1) * width + x)
    for y in range(height):
        seed(y * width)
        seed(y * width + width - 1)

    while stack:
        index = stack.pop()
        x = index % width
        neighbors = []
        if x > 0:
            neighbors.append(index - 1)
        if x < width - 1:
            neighbors.append(index + 1)
        if index >= width:
            neighbors.append(index - width)
        if index < width * height - width:
            neighbors.append(index + width)
        for next_index in neighbors:
            if not visited[next_index] and is_bg(next_index):
                visited[next_index] = 1
                stack.append(next_index)

    out = Image.new("RGBA", rgba.size, (0, 0, 0, 0))
    out.putdata([(0, 0, 0, 0) if visited[index] else (r, g, b, a) for index, (r, g, b, a) in enumerate(pixels)])
    return out


def clean_light_fringe(image: Image.Image, bg_color: tuple[int, int, int], tolerance: int) -> Image.Image:
    rgba = image.convert("RGBA")
    alpha = rgba.getchannel("A")
    transparent = alpha.point(lambda value: 255 if value < 8 else 0)
    near_transparent = transparent.filter(ImageFilter.MaxFilter(5))
    pixels = list(rgba.getdata())
    near = list(near_transparent.getdata())
    cleaned = []
    for pixel, edge in zip(pixels, near, strict=True):
        r, g, b, a = pixel
        if a and edge:
            distance = max(abs(r - bg_color[0]), abs(g - bg_color[1]), abs(b - bg_color[2]))
            bright_neutral = min(r, g, b) >= 225 and max(r, g, b) - min(r, g, b) <= 36
            if distance <= tolerance or bright_neutral:
                cleaned.append((0, 0, 0, 0))
                continue
        cleaned.append(pixel)
    out = Image.new("RGBA", rgba.size, (0, 0, 0, 0))
    out.putdata(cleaned)
    return out


def estimate_edge_color(image: Image.Image) -> tuple[int, int, int]:
    rgba = image.convert("RGBA")
    width, height = rgba.size
    samples: list[tuple[int, int, int]] = []
    inset = max(1, min(width, height) // 32)
    for x in (inset, width // 2, width - 1 - inset):
        samples.append(rgba.getpixel((x, inset))[:3])
        samples.append(rgba.getpixel((x, height - 1 - inset))[:3])
    for y in (inset, height // 2, height - 1 - inset):
        samples.append(rgba.getpixel((inset, y))[:3])
        samples.append(rgba.getpixel((width - 1 - inset, y))[:3])
    return tuple(round(sum(color[i] for color in samples) / len(samples)) for i in range(3))


def harden_alpha(image: Image.Image) -> Image.Image:
    rgba = image.convert("RGBA")
    alpha = rgba.getchannel("A")
    alpha = alpha.filter(ImageFilter.MinFilter(3)).filter(ImageFilter.MaxFilter(3))
    alpha = alpha.point(lambda value: 255 if value >= 96 else 0)
    rgba.putalpha(alpha)
    return rgba


def clear_transparent_rgb(image: Image.Image) -> Image.Image:
    rgba = image.convert("RGBA")
    rgba.putdata([(0, 0, 0, 0) if a == 0 else (r, g, b, a) for r, g, b, a in rgba.getdata()])
    return rgba


def contract_alpha(image: Image.Image, pixels: int) -> Image.Image:
    rgba = image.convert("RGBA")
    alpha = rgba.getchannel("A").point(lambda value: 255 if value >= 128 else 0)
    for _ in range(max(0, pixels)):
        alpha = alpha.filter(ImageFilter.MinFilter(3))
    alpha = alpha.filter(ImageFilter.MaxFilter(3))
    rgba.putalpha(alpha)
    return clear_transparent_rgb(rgba)


def fit_to_sprite_canvas(image: Image.Image, size: int, padding_ratio: float, foot_margin: int) -> Image.Image:
    rgba = image.convert("RGBA")
    alpha = rgba.getchannel("A")
    bbox = alpha.getbbox()
    if bbox is None:
        return Image.new("RGBA", (size, size), (0, 0, 0, 0))
    cropped = rgba.crop(bbox)
    max_width = int(size * (1 - padding_ratio * 2))
    max_height = int(size * (1 - padding_ratio) - foot_margin)
    scale = min(max_width / cropped.width, max_height / cropped.height)
    scaled_size = (max(1, round(cropped.width * scale)), max(1, round(cropped.height * scale)))
    scaled = resize_rgba_premultiplied(cropped, scaled_size)

    alpha_scaled = scaled.getchannel("A")
    scaled_bbox = alpha_scaled.getbbox() or (0, 0, scaled.width, scaled.height)
    x = round((size - scaled.width) / 2)
    y = size - foot_margin - scaled_bbox[3]
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    canvas.alpha_composite(scaled, (x, y))
    return canvas


def resize_rgba_premultiplied(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    rgba = clear_transparent_rgb(image.convert("RGBA"))
    premultiplied = []
    for r, g, b, a in rgba.getdata():
        premultiplied.append((round(r * a / 255), round(g * a / 255), round(b * a / 255), a))
    premul = Image.new("RGBA", rgba.size, (0, 0, 0, 0))
    premul.putdata(premultiplied)
    resized = premul.resize(size, Image.Resampling.LANCZOS)
    unpremultiplied = []
    for r, g, b, a in resized.getdata():
        if a <= 0:
            unpremultiplied.append((0, 0, 0, 0))
        else:
            unpremultiplied.append((
                min(255, round(r * 255 / a)),
                min(255, round(g * 255 / a)),
                min(255, round(b * 255 / a)),
                a,
            ))
    out = Image.new("RGBA", size, (0, 0, 0, 0))
    out.putdata(unpremultiplied)
    return out


def add_pixel_outline(image: Image.Image) -> Image.Image:
    rgba = image.convert("RGBA")
    alpha = rgba.getchannel("A").point(lambda value: 255 if value > 0 else 0)
    grown = alpha.filter(ImageFilter.MaxFilter(3))
    outline_alpha = ImageChops.subtract(grown, alpha)
    outline = Image.new("RGBA", rgba.size, (58, 42, 39, 255))
    outline.putalpha(outline_alpha)
    outline.alpha_composite(rgba)
    return outline


def quantize_rgba(image: Image.Image, colors: int) -> Image.Image:
    rgba = image.convert("RGBA")
    alpha = rgba.getchannel("A")
    rgb = Image.new("RGB", rgba.size, (0, 0, 0))
    rgb.paste(rgba.convert("RGB"), mask=alpha)
    quantized = rgb.quantize(colors=colors, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE).convert("RGBA")
    quantized.putalpha(alpha.point(lambda value: 255 if value >= 128 else 0))
    return quantized


def restore_pixel_highlights(
    image: Image.Image,
    reference: Image.Image,
    max_fraction: float,
    min_luma: int,
    local_contrast: int,
) -> Image.Image:
    out = image.convert("RGBA")
    ref = reference.convert("RGBA")
    alpha = ref.getchannel("A").point(lambda value: 255 if value >= 128 else 0)
    luma = ref.convert("L")
    local = luma.filter(ImageFilter.BoxBlur(1.2))
    luma_data = list(luma.getdata())
    local_data = list(local.getdata())
    alpha_data = list(alpha.getdata())
    ref_data = list(ref.getdata())

    candidates: list[tuple[float, int]] = []
    visible = 0
    for index, (light, base, a) in enumerate(zip(luma_data, local_data, alpha_data, strict=True)):
        if not a:
            continue
        visible += 1
        contrast = light - base
        if light >= min_luma and contrast >= local_contrast:
            score = contrast * 2.0 + max(0, light - min_luma) * 0.35
            candidates.append((score, index))
        elif light >= 218 and contrast >= max(4, local_contrast // 2):
            score = contrast + (light - 200) * 0.4
            candidates.append((score, index))

    if not candidates or visible <= 0:
        return out

    keep = max(1, round(visible * max(0.0, min(0.2, max_fraction))))
    chosen = {index for _, index in sorted(candidates, reverse=True)[:keep]}
    pixels = list(out.getdata())
    width = out.width
    for index in chosen:
        r, g, b, a = ref_data[index]
        if a < 128:
            continue
        qr, qg, qb, qa = pixels[index]
        if qa == 0:
            continue
        boosted = (
            min(255, round(max(qr, r) * 1.06)),
            min(255, round(max(qg, g) * 1.06)),
            min(255, round(max(qb, b) * 1.06)),
            qa,
        )
        pixels[index] = boosted

        # Preserve a tiny two-pixel glint only when the neighbor is already bright.
        right = index + 1
        if index % width != width - 1 and right not in chosen and luma_data[right] > min_luma + 20:
            nr, ng, nb, na = pixels[right]
            if na:
                pixels[right] = (
                    min(255, round((nr * 2 + boosted[0]) / 3)),
                    min(255, round((ng * 2 + boosted[1]) / 3)),
                    min(255, round((nb * 2 + boosted[2]) / 3)),
                    na,
                )

    out.putdata(pixels)
    return out


def checker_preview(image: Image.Image, scale: int) -> Image.Image:
    rgba = image.convert("RGBA")
    preview = Image.new("RGBA", rgba.size, (238, 238, 238, 255))
    pixels = preview.load()
    tile = max(2, rgba.width // 16)
    for y in range(rgba.height):
        for x in range(rgba.width):
            if ((x // tile) + (y // tile)) % 2:
                pixels[x, y] = (206, 212, 218, 255)
    preview.alpha_composite(rgba)
    return preview.resize((rgba.width * scale, rgba.height * scale), Image.Resampling.NEAREST)


if __name__ == "__main__":
    raise SystemExit(main())
