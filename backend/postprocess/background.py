from __future__ import annotations

from collections import Counter
from collections.abc import Iterable

from PIL import Image


def estimate_background_rgb(image: Image.Image, inset: int | None = None) -> tuple[int, int, int]:
    """Estimate a flat background color from edge/corner samples.

    Generated sprite sheets often draw separators on the outermost pixel, so
    using only (0, 0) is fragile. Sampling slightly inside the corners and edges
    gives a better default for simple white/solid backgrounds.
    """

    rgba = image.convert("RGBA")
    width, height = rgba.size
    if inset is None:
        inset = max(2, min(width, height) // 32)
    inset = min(inset, max(0, width - 1), max(0, height - 1))
    xs = sorted({inset, width // 2, max(0, width - 1 - inset)})
    ys = sorted({inset, height // 2, max(0, height - 1 - inset)})

    samples: list[tuple[int, int, int]] = []
    for x in xs:
        samples.append(rgba.getpixel((x, inset))[:3])
        samples.append(rgba.getpixel((x, max(0, height - 1 - inset)))[:3])
    for y in ys:
        samples.append(rgba.getpixel((inset, y))[:3])
        samples.append(rgba.getpixel((max(0, width - 1 - inset), y))[:3])

    if not samples:
        return rgba.getpixel((0, 0))[:3]
    return Counter(samples).most_common(1)[0][0]


def estimate_edge_background_palette(
    image: Image.Image,
    max_colors: int = 8,
    bucket_size: int = 16,
) -> list[tuple[int, int, int]]:
    """Find likely background colors from the outer image edge.

    Image models often render a fake transparency checkerboard. A single
    background color estimate only removes one half of that pattern; using the
    dominant edge colors lets us remove both checker colors while still keeping
    the operation local to the edge-connected background.
    """

    rgba = image.convert("RGBA")
    width, height = rgba.size
    if width <= 0 or height <= 0:
        return []

    sums: dict[tuple[int, int, int], list[int]] = {}
    counts: Counter[tuple[int, int, int]] = Counter()
    for rgb in _edge_rgb_samples(rgba):
        key = tuple(channel // bucket_size for channel in rgb)
        counts[key] += 1
        bucket = sums.setdefault(key, [0, 0, 0])
        bucket[0] += rgb[0]
        bucket[1] += rgb[1]
        bucket[2] += rgb[2]

    total = sum(counts.values())
    if not total:
        return [estimate_background_rgb(rgba)]

    min_count = max(4, total // 200)
    palette: list[tuple[int, int, int]] = []
    for key, count in counts.most_common():
        if count < min_count and len(palette) >= 2:
            break
        rgb_sum = sums[key]
        color = (
            round(rgb_sum[0] / count),
            round(rgb_sum[1] / count),
            round(rgb_sum[2] / count),
        )
        if not any(_max_channel_distance(color, existing) <= bucket_size for existing in palette):
            palette.append(color)
        if len(palette) >= max_colors:
            break

    base = estimate_background_rgb(rgba)
    if not any(_max_channel_distance(base, color) <= bucket_size for color in palette):
        palette.insert(0, base)
    return palette[:max_colors]


def connected_background_mask(
    image: Image.Image,
    tolerance: int = 36,
    palette: list[tuple[int, int, int]] | None = None,
) -> Image.Image:
    """Return an L mask where edge-connected background pixels are 255.

    Matching is limited to pixels connected to the image edge. This makes the
    cleanup safer than deleting every similar color globally, because bright
    armor highlights or white icons inside the subject are not removed unless
    they are also connected to the outer background.
    """

    rgba = image.convert("RGBA")
    width, height = rgba.size
    total = width * height
    if total == 0:
        return Image.new("L", rgba.size, 0)

    data = list(rgba.getdata())
    colors = palette or estimate_edge_background_palette(rgba)
    if not colors:
        return Image.new("L", rgba.size, 0)

    visited = bytearray(total)
    stack: list[int] = []

    def is_background(index: int) -> bool:
        pixel = data[index]
        if pixel[3] == 0:
            return True
        rgb = pixel[:3]
        return any(_max_channel_distance(rgb, color) <= tolerance for color in colors)

    def seed(index: int) -> None:
        if not visited[index] and is_background(index):
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
        if x > 0:
            next_index = index - 1
            if not visited[next_index] and is_background(next_index):
                visited[next_index] = 1
                stack.append(next_index)
        if x < width - 1:
            next_index = index + 1
            if not visited[next_index] and is_background(next_index):
                visited[next_index] = 1
                stack.append(next_index)
        if index >= width:
            next_index = index - width
            if not visited[next_index] and is_background(next_index):
                visited[next_index] = 1
                stack.append(next_index)
        if index < total - width:
            next_index = index + width
            if not visited[next_index] and is_background(next_index):
                visited[next_index] = 1
                stack.append(next_index)

    mask = Image.new("L", rgba.size, 0)
    mask.putdata([255 if value else 0 for value in visited])
    return mask


def remove_edge_connected_background(
    image: Image.Image,
    tolerance: int = 36,
) -> Image.Image:
    rgba = image.convert("RGBA")
    mask = connected_background_mask(rgba, tolerance=tolerance)
    output_pixels = []
    for pixel, background in zip(rgba.getdata(), mask.getdata(), strict=True):
        if background:
            output_pixels.append((pixel[0], pixel[1], pixel[2], 0))
        else:
            output_pixels.append(pixel)
    output = Image.new("RGBA", rgba.size, (0, 0, 0, 0))
    output.putdata(output_pixels)
    return output


def foreground_mask_from_background(
    image: Image.Image,
    tolerance: int = 36,
) -> Image.Image:
    rgba = image.convert("RGBA")
    alpha = rgba.getchannel("A")
    alpha_bbox = alpha.getbbox()
    if alpha_bbox and alpha_bbox != (0, 0, rgba.width, rgba.height):
        return alpha.point(lambda value: 255 if value > 8 else 0)

    background = connected_background_mask(rgba, tolerance=tolerance)
    return background.point(lambda value: 0 if value else 255)


def foreground_mask_from_palette(
    image: Image.Image,
    tolerance: int = 36,
    palette: list[tuple[int, int, int]] | None = None,
) -> Image.Image:
    rgba = image.convert("RGBA")
    colors = palette or estimate_edge_background_palette(rgba)
    background = connected_background_mask(rgba, tolerance=tolerance, palette=colors)
    return background.point(lambda value: 0 if value else 255)


def _edge_rgb_samples(image: Image.Image) -> Iterable[tuple[int, int, int]]:
    width, height = image.size
    for x in range(width):
        yield image.getpixel((x, 0))[:3]
        yield image.getpixel((x, height - 1))[:3]
    for y in range(height):
        yield image.getpixel((0, y))[:3]
        yield image.getpixel((width - 1, y))[:3]


def _max_channel_distance(left: tuple[int, int, int], right: tuple[int, int, int]) -> int:
    return max(abs(left[index] - right[index]) for index in range(3))
