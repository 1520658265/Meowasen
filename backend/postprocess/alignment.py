from __future__ import annotations

from dataclasses import dataclass

from PIL import Image


@dataclass(frozen=True)
class AlignmentMeasurement:
    bbox: tuple[int, int, int, int] | None
    alpha_centroid: tuple[float, float] | None
    bbox_center: tuple[float, float] | None


@dataclass(frozen=True)
class AlignmentResult:
    image: Image.Image
    dx: int
    dy: int
    measurement: AlignmentMeasurement
    target: tuple[float, float]


def measure_alignment(image: Image.Image) -> AlignmentMeasurement:
    rgba = image.convert("RGBA")
    alpha = rgba.getchannel("A")
    bbox = alpha.getbbox()
    bbox_center = None
    if bbox:
        bbox_center = ((bbox[0] + bbox[2] - 1) / 2.0, (bbox[1] + bbox[3] - 1) / 2.0)

    pixels = alpha.load()
    total = 0
    sx = 0
    sy = 0
    for y in range(rgba.height):
        for x in range(rgba.width):
            value = pixels[x, y]
            if value:
                total += value
                sx += x * value
                sy += y * value

    alpha_centroid = None
    if total:
        alpha_centroid = (sx / total, sy / total)

    return AlignmentMeasurement(
        bbox=bbox,
        alpha_centroid=alpha_centroid,
        bbox_center=bbox_center,
    )


def align_to_canvas_center(
    image: Image.Image,
    anchor: str = "alpha_centroid",
    axis: str = "x",
) -> AlignmentResult:
    rgba = image.convert("RGBA")
    measurement = measure_alignment(rgba)
    anchor_point = getattr(measurement, anchor, None)
    target = ((rgba.width - 1) / 2.0, (rgba.height - 1) / 2.0)
    if anchor_point is None:
        return AlignmentResult(image=rgba, dx=0, dy=0, measurement=measurement, target=target)

    dx = round(target[0] - anchor_point[0]) if "x" in axis else 0
    dy = round(target[1] - anchor_point[1]) if "y" in axis else 0
    if dx == 0 and dy == 0:
        return AlignmentResult(image=rgba, dx=0, dy=0, measurement=measurement, target=target)

    shifted = Image.new("RGBA", rgba.size, (0, 0, 0, 0))
    shifted.alpha_composite(rgba, (dx, dy))
    return AlignmentResult(image=shifted, dx=dx, dy=dy, measurement=measurement, target=target)
