from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from PIL import Image, ImageDraw

from pixelize_sprite import (
    add_pixel_outline,
    checker_preview,
    clean_light_fringe,
    clear_transparent_rgb,
    contract_alpha,
    estimate_edge_color,
    harden_alpha,
    remove_light_edge_background,
    resize_rgba_premultiplied,
    restore_pixel_highlights,
)


ROOT_DIR = Path(__file__).resolve().parents[1]
DIRECTIONS = ("front", "back", "left", "right")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Postprocess four direction walk videos into RPG sprite sheets.")
    parser.add_argument("--front", required=True)
    parser.add_argument("--back", required=True)
    parser.add_argument("--left", required=True)
    parser.add_argument("--right", required=True)
    parser.add_argument("--output-dir", default="assets/tasks/sprites/gothic_umbrella_walk_4dir_pixel_v1")
    parser.add_argument("--cell-sizes", default="128,160")
    parser.add_argument("--frames-per-dir", type=int, default=4)
    parser.add_argument("--selection-mode", choices=["uniform", "auto-loop"], default="uniform")
    parser.add_argument("--sample-start", type=float, default=0.25)
    parser.add_argument("--sample-end", type=float)
    parser.add_argument("--auto-window-min-seconds", type=float, default=0.9)
    parser.add_argument("--auto-window-max-seconds", type=float, default=1.6)
    parser.add_argument("--auto-feature-size", type=int, default=96)
    parser.add_argument("--palette-colors", type=int, default=64)
    parser.add_argument("--bg-tolerance", type=int, default=52)
    parser.add_argument("--fringe-tolerance", type=int, default=64)
    parser.add_argument("--padding-ratio", type=float, default=0.08)
    parser.add_argument("--foot-margin", type=int, default=6)
    parser.add_argument("--edge-contract", type=int, default=1)
    parser.add_argument("--outline", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--highlight-restore", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--highlight-fraction", type=float, default=0.025)
    parser.add_argument("--highlight-min-luma", type=int, default=136)
    parser.add_argument("--highlight-local-contrast", type=int, default=9)
    parser.add_argument("--floor-shadow-clean", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--floor-shadow-start", type=float, default=0.92)
    parser.add_argument("--floor-shadow-min-luma", type=int, default=70)
    parser.add_argument("--floor-shadow-max-saturation", type=int, default=28)
    parser.add_argument("--floor-shadow-max-dark-ratio", type=float, default=0.08)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    output_dir = resolve_path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    video_paths = {
        "front": resolve_path(args.front),
        "back": resolve_path(args.back),
        "left": resolve_path(args.left),
        "right": resolve_path(args.right),
    }
    cell_sizes = [int(item.strip()) for item in args.cell_sizes.split(",") if item.strip()]
    if not cell_sizes:
        raise ValueError("--cell-sizes must contain at least one size")

    raw_dir = output_dir / "raw_samples"
    cutout_dir = output_dir / "cutouts"
    raw_dir.mkdir(parents=True, exist_ok=True)
    cutout_dir.mkdir(parents=True, exist_ok=True)

    frames: list[FrameItem] = []
    video_meta: dict[str, Any] = {}
    for direction in DIRECTIONS:
        samples, meta = sample_video(
            video_paths[direction],
            count=args.frames_per_dir,
            start=args.sample_start,
            end=args.sample_end,
            args=args,
        )
        video_meta[direction] = meta
        for index, sample in enumerate(samples):
            raw_path = raw_dir / f"{direction}_{index:02d}.png"
            sample.image.save(raw_path)
            cutout = make_cutout(sample.image, args)
            cutout_path = cutout_dir / f"{direction}_{index:02d}.png"
            cutout.save(cutout_path)
            bbox = cutout.getchannel("A").getbbox()
            if bbox is None:
                raise RuntimeError(f"No foreground found in {direction} frame {index}")
            frames.append(
                FrameItem(
                    direction=direction,
                    index=index,
                    timestamp=sample.timestamp,
                    raw_path=raw_path,
                    cutout_path=cutout_path,
                    cutout=cutout,
                    bbox=bbox,
                )
            )

    max_source_width = max(item.bbox[2] - item.bbox[0] for item in frames)
    max_source_height = max(item.bbox[3] - item.bbox[1] for item in frames)
    outputs: dict[str, Any] = {}
    for cell_size in cell_sizes:
        outputs[str(cell_size)] = build_size_outputs(
            frames=frames,
            output_dir=output_dir,
            cell_size=cell_size,
            source_extent=(max_source_width, max_source_height),
            args=args,
        )

    meta = {
        "output_dir": str(output_dir),
        "directions": list(DIRECTIONS),
        "row_order": list(DIRECTIONS),
        "frames_per_dir": args.frames_per_dir,
        "videos": {key: str(value) for key, value in video_paths.items()},
        "video_meta": video_meta,
        "source_extent": {
            "max_width": max_source_width,
            "max_height": max_source_height,
        },
        "settings": {
            "selection_mode": args.selection_mode,
            "cell_sizes": cell_sizes,
            "palette_colors": args.palette_colors,
            "bg_tolerance": args.bg_tolerance,
            "fringe_tolerance": args.fringe_tolerance,
            "padding_ratio": args.padding_ratio,
            "foot_margin": args.foot_margin,
            "edge_contract": args.edge_contract,
            "outline": args.outline,
            "highlight_restore": args.highlight_restore,
            "floor_shadow_clean": args.floor_shadow_clean,
        },
        "frames": [
            {
                "direction": item.direction,
                "index": item.index,
                "timestamp": round(item.timestamp, 4),
                "bbox": list(item.bbox),
                "raw": str(item.raw_path),
                "cutout": str(item.cutout_path),
            }
            for item in frames
        ],
        "outputs": outputs,
    }
    meta_path = output_dir / "postprocess_meta.json"
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output_dir": str(output_dir), "meta": str(meta_path), "outputs": outputs}, ensure_ascii=False, indent=2))
    return 0


class VideoSample:
    def __init__(self, image: Image.Image, timestamp: float) -> None:
        self.image = image
        self.timestamp = timestamp


class FrameItem:
    def __init__(
        self,
        *,
        direction: str,
        index: int,
        timestamp: float,
        raw_path: Path,
        cutout_path: Path,
        cutout: Image.Image,
        bbox: tuple[int, int, int, int],
    ) -> None:
        self.direction = direction
        self.index = index
        self.timestamp = timestamp
        self.raw_path = raw_path
        self.cutout_path = cutout_path
        self.cutout = cutout
        self.bbox = bbox


def resolve_path(path: str) -> Path:
    result = Path(path)
    if result.is_absolute():
        return result
    return ROOT_DIR / result


def sample_video(
    path: Path,
    count: int,
    start: float,
    end: float | None,
    args: argparse.Namespace,
) -> tuple[list[VideoSample], dict[str, Any]]:
    capture = cv2.VideoCapture(str(path))
    if not capture.isOpened():
        raise RuntimeError(f"Could not open video: {path}")

    fps = capture.get(cv2.CAP_PROP_FPS) or 24.0
    frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
    duration = frame_count / fps if frame_count and fps else 0.0
    safe_end = min(end if end is not None else max(0.0, duration - 0.12), max(0.0, duration - 0.04))
    safe_start = min(max(0.0, start), max(0.0, safe_end - 0.01))
    if safe_end <= safe_start:
        safe_start = 0.0
        safe_end = duration

    if args.selection_mode == "auto-loop":
        frames = read_candidate_frames(capture, fps, frame_count, safe_start, safe_end)
        capture.release()
        samples, auto_meta = select_auto_loop_samples(
            frames=frames,
            count=count,
            fps=fps,
            args=args,
        )
        return samples, {
            "path": str(path),
            "fps": fps,
            "frames": frame_count,
            "seconds": duration,
            "size": [width, height],
            "sample_times": [round(item.timestamp, 4) for item in samples],
            "selection": auto_meta,
        }

    span = max(0.01, safe_end - safe_start)
    times = [safe_start + span * (index + 0.5) / count for index in range(count)]

    samples: list[VideoSample] = []
    for timestamp in times:
        capture.set(cv2.CAP_PROP_POS_MSEC, timestamp * 1000.0)
        ok, frame = capture.read()
        if not ok:
            continue
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        samples.append(VideoSample(Image.fromarray(rgb).convert("RGBA"), timestamp))

    capture.release()
    if len(samples) != count:
        raise RuntimeError(f"Expected {count} samples from {path}, got {len(samples)}")
    return samples, {
        "path": str(path),
        "fps": fps,
        "frames": frame_count,
        "seconds": duration,
        "size": [width, height],
        "sample_times": [round(item, 4) for item in times],
        "selection": {"mode": "uniform"},
    }


def read_candidate_frames(
    capture: cv2.VideoCapture,
    fps: float,
    frame_count: int,
    start: float,
    end: float,
) -> list[tuple[int, float, Image.Image]]:
    start_index = max(0, math.floor(start * fps))
    end_index = min(max(0, frame_count - 1), math.ceil(end * fps))
    frames: list[tuple[int, float, Image.Image]] = []
    capture.set(cv2.CAP_PROP_POS_FRAMES, start_index)
    for frame_index in range(start_index, end_index + 1):
        ok, frame = capture.read()
        if not ok:
            break
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        frames.append((frame_index, frame_index / fps, Image.fromarray(rgb).convert("RGBA")))
    if len(frames) < 2:
        raise RuntimeError("Not enough candidate frames for auto selection")
    return frames


def select_auto_loop_samples(
    *,
    frames: list[tuple[int, float, Image.Image]],
    count: int,
    fps: float,
    args: argparse.Namespace,
) -> tuple[list[VideoSample], dict[str, Any]]:
    features = [frame_feature(image, args.auto_feature_size) for _, _, image in frames]
    min_window = max(count, round(args.auto_window_min_seconds * fps))
    max_window = max(min_window, round(args.auto_window_max_seconds * fps))
    max_window = min(max_window, len(frames) - 1)
    if max_window < count:
        raise RuntimeError(f"Not enough frames for auto loop selection: have {len(frames)}, need {count}")

    frame_diffs = [
        feature_distance(features[index], features[index + 1])
        for index in range(len(features) - 1)
    ]
    global_motion = float(np.median(frame_diffs)) if frame_diffs else 0.0
    best: dict[str, Any] | None = None
    for window in range(min_window, max_window + 1):
        if window <= count:
            continue
        for start_index in range(0, len(frames) - window):
            end_index = start_index + window
            sampled_indices = evenly_spaced_indices(start_index, end_index, count)
            if len(set(sampled_indices)) != count:
                continue
            sampled_features = [features[index] for index in sampled_indices]
            adjacent = [
                feature_distance(sampled_features[index], sampled_features[(index + 1) % count])
                for index in range(count)
            ]
            loop_error = feature_distance(sampled_features[0], sampled_features[-1])
            smoothness = float(np.std(adjacent))
            motion = float(np.mean(adjacent))
            center_drift = max_center_drift(sampled_features)
            area_drift = max_area_drift(sampled_features)
            repeat_penalty = float(sum(1 for value in adjacent if value < max(0.003, global_motion * 0.35))) / count
            low_motion_penalty = max(0.0, (global_motion * 0.55 - motion) * 8.0)
            score = (
                loop_error * 1.5
                + smoothness * 1.2
                + center_drift * 0.45
                + area_drift * 0.4
                + repeat_penalty * 0.18
                + low_motion_penalty
            )
            candidate = {
                "score": score,
                "start_index": start_index,
                "end_index": end_index,
                "window": window,
                "sampled_indices": sampled_indices,
                "loop_error": loop_error,
                "smoothness": smoothness,
                "motion": motion,
                "center_drift": center_drift,
                "area_drift": area_drift,
                "repeat_penalty": repeat_penalty,
            }
            if best is None or candidate["score"] < best["score"]:
                best = candidate

    if best is None:
        raise RuntimeError("Could not select auto loop frames")

    samples = [
        VideoSample(frames[index][2], frames[index][1])
        for index in best["sampled_indices"]
    ]
    return samples, {
        "mode": "auto-loop",
        "source_frame_start": frames[best["start_index"]][0],
        "source_frame_end": frames[best["end_index"]][0],
        "source_time_start": round(frames[best["start_index"]][1], 4),
        "source_time_end": round(frames[best["end_index"]][1], 4),
        "window_frames": best["window"],
        "sampled_source_frames": [frames[index][0] for index in best["sampled_indices"]],
        "score": round(float(best["score"]), 6),
        "loop_error": round(float(best["loop_error"]), 6),
        "smoothness": round(float(best["smoothness"]), 6),
        "motion": round(float(best["motion"]), 6),
        "center_drift": round(float(best["center_drift"]), 6),
        "area_drift": round(float(best["area_drift"]), 6),
        "repeat_penalty": round(float(best["repeat_penalty"]), 6),
    }


def evenly_spaced_indices(start: int, end: int, count: int) -> list[int]:
    if count <= 1:
        return [(start + end) // 2]
    return [round(start + (end - start) * index / count) for index in range(count)]


def frame_feature(image: Image.Image, size: int) -> dict[str, Any]:
    rgb = np.array(image.convert("RGB"))
    small = cv2.resize(rgb, (size, size), interpolation=cv2.INTER_AREA)
    edge = estimate_edge_color(image)
    diff = np.max(np.abs(small.astype("int16") - np.array(edge, dtype="int16")), axis=2)
    mask = (diff > 34).astype("uint8")
    kernel = np.ones((3, 3), dtype="uint8")
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    if count > 1:
        largest = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
        mask = (labels == largest).astype("uint8")
    ys, xs = np.where(mask > 0)
    if xs.size == 0:
        center_x = center_y = 0.5
        area = 0.0
        foot = np.zeros((size // 4, size), dtype="float32")
    else:
        center_x = float(xs.mean() / size)
        center_y = float(ys.mean() / size)
        area = float(xs.size / (size * size))
        y0 = max(0, int(np.quantile(ys, 0.72)))
        foot = mask[y0:, :].astype("float32")
        if foot.shape[0] != size // 4:
            foot = cv2.resize(foot, (size, size // 4), interpolation=cv2.INTER_AREA)
    silhouette = cv2.resize(mask.astype("float32"), (48, 48), interpolation=cv2.INTER_AREA)
    return {
        "silhouette": silhouette,
        "foot": foot,
        "center": np.array([center_x, center_y], dtype="float32"),
        "area": area,
    }


def feature_distance(left: dict[str, Any], right: dict[str, Any]) -> float:
    sil = float(np.mean(np.abs(left["silhouette"] - right["silhouette"])))
    foot = float(np.mean(np.abs(left["foot"] - right["foot"])))
    center = float(np.linalg.norm(left["center"] - right["center"]))
    area = abs(float(left["area"]) - float(right["area"]))
    return sil * 0.45 + foot * 0.35 + center * 0.15 + area * 0.05


def max_center_drift(features: list[dict[str, Any]]) -> float:
    centers = np.array([item["center"] for item in features], dtype="float32")
    mean = centers.mean(axis=0)
    return float(np.max(np.linalg.norm(centers - mean, axis=1)))


def max_area_drift(features: list[dict[str, Any]]) -> float:
    areas = np.array([item["area"] for item in features], dtype="float32")
    return float(np.max(np.abs(areas - areas.mean())))


def make_cutout(image: Image.Image, args: argparse.Namespace) -> Image.Image:
    bg_color = estimate_edge_color(image)
    cutout = remove_light_edge_background(image, tolerance=args.bg_tolerance, bg_color=bg_color)
    cutout = clean_light_fringe(cutout, bg_color=bg_color, tolerance=args.fringe_tolerance)
    cutout = remove_near_background_pixels(cutout, bg_color=bg_color, tolerance=max(18, args.bg_tolerance - 10))
    cutout = harden_alpha(cutout)
    if args.floor_shadow_clean:
        cutout = remove_floor_shadow_components(cutout, args)
    return clear_transparent_rgb(cutout)


def remove_near_background_pixels(image: Image.Image, bg_color: tuple[int, int, int], tolerance: int) -> Image.Image:
    rgba = image.convert("RGBA")
    cleaned = []
    for r, g, b, a in rgba.getdata():
        if a == 0:
            cleaned.append((0, 0, 0, 0))
            continue
        distance = max(abs(r - bg_color[0]), abs(g - bg_color[1]), abs(b - bg_color[2]))
        neutral = max(r, g, b) - min(r, g, b) <= 18
        light_background = min(r, g, b) >= 150
        if distance <= tolerance and neutral and light_background:
            cleaned.append((0, 0, 0, 0))
        else:
            cleaned.append((r, g, b, a))
    out = Image.new("RGBA", rgba.size, (0, 0, 0, 0))
    out.putdata(cleaned)
    return out


def remove_floor_shadow_components(image: Image.Image, args: argparse.Namespace) -> Image.Image:
    rgba = image.convert("RGBA")
    data = np.array(rgba)
    alpha_mask = (data[:, :, 3] > 0).astype("uint8")
    bbox = rgba.getchannel("A").getbbox()
    if bbox is None:
        return rgba
    _, top, _, bottom = bbox
    floor_start_y = round(top + (bottom - top) * args.floor_shadow_start)
    yy = np.arange(data.shape[0])[:, None]
    rgb32 = data[:, :, :3].astype("int32")
    max_channel = rgb32.max(axis=2)
    min_channel = rgb32.min(axis=2)
    saturation = max_channel - min_channel
    luma = (rgb32[:, :, 0] * 299 + rgb32[:, :, 1] * 587 + rgb32[:, :, 2] * 114) / 1000.0
    floor_pixel_mask = (
        (alpha_mask > 0)
        & (yy >= floor_start_y)
        & (luma >= args.floor_shadow_min_luma)
        & (saturation <= args.floor_shadow_max_saturation)
    )
    if floor_pixel_mask.any():
        data[floor_pixel_mask, 0:4] = 0
        alpha_mask[floor_pixel_mask] = 0

    count, labels, stats, _ = cv2.connectedComponentsWithStats(alpha_mask, connectivity=8)
    height = data.shape[0]
    rgb = data[:, :, :3].astype("int32")
    removed = np.zeros(alpha_mask.shape, dtype=bool)

    for label in range(1, count):
        x, y, width, comp_height, area = stats[label]
        bottom = y + comp_height
        if bottom < floor_start_y:
            continue
        component = labels == label
        pixels = rgb[component]
        if pixels.size == 0:
            continue
        max_channel = pixels.max(axis=1)
        min_channel = pixels.min(axis=1)
        saturation = max_channel - min_channel
        luma = (pixels[:, 0] * 299 + pixels[:, 1] * 587 + pixels[:, 2] * 114) / 1000.0
        dark_ratio = float((luma < 82).mean())
        mean_luma = float(luma.mean())
        mean_saturation = float(saturation.mean())
        low_detail_component = area < image.width * image.height * 0.035
        neutral_floor_like = (
            mean_luma >= args.floor_shadow_min_luma
            and mean_saturation <= args.floor_shadow_max_saturation
            and dark_ratio <= args.floor_shadow_max_dark_ratio
        )
        if low_detail_component and neutral_floor_like:
            removed |= component

    if removed.any():
        data[removed, 0:4] = 0
    return Image.fromarray(data, mode="RGBA")


def build_size_outputs(
    *,
    frames: list[FrameItem],
    output_dir: Path,
    cell_size: int,
    source_extent: tuple[int, int],
    args: argparse.Namespace,
) -> dict[str, Any]:
    frames_dir = output_dir / f"frames_{cell_size}"
    frames_dir.mkdir(parents=True, exist_ok=True)
    max_width, max_height = source_extent
    max_cell_width = int(cell_size * (1 - args.padding_ratio * 2))
    max_cell_height = int(cell_size * (1 - args.padding_ratio) - args.foot_margin)
    scale = min(max_cell_width / max_width, max_cell_height / max_height)

    pre_frames: dict[tuple[str, int], Image.Image] = {}
    for item in frames:
        sprite = fit_with_shared_scale(
            image=item.cutout,
            bbox=item.bbox,
            cell_size=cell_size,
            scale=scale,
            foot_margin=args.foot_margin,
        )
        if args.edge_contract > 0:
            sprite = contract_alpha(sprite, pixels=args.edge_contract)
        if args.outline:
            sprite = add_pixel_outline(sprite)
        pre_frames[(item.direction, item.index)] = sprite

    pre_sheet = compose_sheet(pre_frames, cell_size, args.frames_per_dir)
    sheet = quantize_rgba_common(pre_sheet, colors=args.palette_colors)
    if args.highlight_restore:
        sheet = restore_pixel_highlights(
            sheet,
            reference=pre_sheet,
            max_fraction=args.highlight_fraction,
            min_luma=args.highlight_min_luma,
            local_contrast=args.highlight_local_contrast,
        )

    sheet_path = output_dir / f"walk_4dir_{cell_size}.png"
    sheet.save(sheet_path)
    preview_path = output_dir / f"walk_4dir_{cell_size}_preview.png"
    checker_preview(sheet, scale=max(2, 512 // cell_size)).save(preview_path)

    frame_paths: dict[str, list[str]] = {}
    gif_paths: dict[str, str] = {}
    for row, direction in enumerate(DIRECTIONS):
        frame_paths[direction] = []
        gif_frames: list[Image.Image] = []
        for index in range(args.frames_per_dir):
            x0 = index * cell_size
            y0 = row * cell_size
            frame = sheet.crop((x0, y0, x0 + cell_size, y0 + cell_size))
            frame_path = frames_dir / f"{direction}_{index:02d}.png"
            frame.save(frame_path)
            frame_paths[direction].append(str(frame_path))
            gif_frames.append(frame_on_checker(frame, scale=max(2, 384 // cell_size)))
        gif_path = output_dir / f"{direction}_{cell_size}_preview.gif"
        gif_frames[0].save(
            gif_path,
            save_all=True,
            append_images=gif_frames[1:],
            duration=180,
            loop=0,
            disposal=2,
        )
        gif_paths[direction] = str(gif_path)

    return {
        "cell_size": cell_size,
        "scale": scale,
        "sheet": str(sheet_path),
        "preview": str(preview_path),
        "frames_dir": str(frames_dir),
        "frames": frame_paths,
        "gifs": gif_paths,
    }


def fit_with_shared_scale(
    *,
    image: Image.Image,
    bbox: tuple[int, int, int, int],
    cell_size: int,
    scale: float,
    foot_margin: int,
) -> Image.Image:
    cropped = image.crop(bbox)
    scaled_size = (max(1, round(cropped.width * scale)), max(1, round(cropped.height * scale)))
    scaled = resize_rgba_premultiplied(cropped, scaled_size)
    scaled_bbox = scaled.getchannel("A").getbbox() or (0, 0, scaled.width, scaled.height)
    x = round((cell_size - scaled.width) / 2)
    y = cell_size - foot_margin - scaled_bbox[3]
    canvas = Image.new("RGBA", (cell_size, cell_size), (0, 0, 0, 0))
    canvas.alpha_composite(scaled, (x, y))
    return clear_transparent_rgb(canvas)


def compose_sheet(frames: dict[tuple[str, int], Image.Image], cell_size: int, frames_per_dir: int) -> Image.Image:
    sheet = Image.new("RGBA", (frames_per_dir * cell_size, len(DIRECTIONS) * cell_size), (0, 0, 0, 0))
    for row, direction in enumerate(DIRECTIONS):
        for index in range(frames_per_dir):
            sheet.alpha_composite(frames[(direction, index)], (index * cell_size, row * cell_size))
    return sheet


def quantize_rgba_common(image: Image.Image, colors: int) -> Image.Image:
    rgba = image.convert("RGBA")
    alpha = rgba.getchannel("A")
    palette = build_palette(rgba, colors=colors)
    rgb = Image.new("RGB", rgba.size, (0, 0, 0))
    rgb.paste(rgba.convert("RGB"), mask=alpha)
    quantized = rgb.quantize(palette=palette, dither=Image.Dither.NONE).convert("RGBA")
    quantized.putalpha(alpha.point(lambda value: 255 if value >= 128 else 0))
    return clear_transparent_rgb(quantized)


def build_palette(image: Image.Image, colors: int) -> Image.Image:
    visible_pixels = [(r, g, b) for r, g, b, a in image.convert("RGBA").getdata() if a >= 128]
    if not visible_pixels:
        visible_pixels = [(0, 0, 0)]
    max_pixels = 220_000
    if len(visible_pixels) > max_pixels:
        step = math.ceil(len(visible_pixels) / max_pixels)
        visible_pixels = visible_pixels[::step]
    side = math.ceil(math.sqrt(len(visible_pixels)))
    swatch = Image.new("RGB", (side, side), (0, 0, 0))
    swatch.putdata(visible_pixels + [(0, 0, 0)] * (side * side - len(visible_pixels)))
    return swatch.quantize(colors=colors, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)


def frame_on_checker(image: Image.Image, scale: int) -> Image.Image:
    rgba = image.convert("RGBA")
    tile = max(4, rgba.width // 8)
    preview = Image.new("RGBA", rgba.size, (238, 238, 238, 255))
    draw = ImageDraw.Draw(preview)
    for y in range(0, rgba.height, tile):
        for x in range(0, rgba.width, tile):
            if ((x // tile) + (y // tile)) % 2:
                draw.rectangle((x, y, x + tile - 1, y + tile - 1), fill=(208, 214, 220, 255))
    preview.alpha_composite(rgba)
    return preview.convert("RGB").resize((rgba.width * scale, rgba.height * scale), Image.Resampling.NEAREST)


if __name__ == "__main__":
    raise SystemExit(main())
