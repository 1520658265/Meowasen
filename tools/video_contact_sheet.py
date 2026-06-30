from __future__ import annotations

import argparse
from pathlib import Path

import cv2
from PIL import Image, ImageDraw, ImageFont


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create a contact sheet by sampling video frames.")
    parser.add_argument("--video", action="append", required=True, help="Video path. Can be repeated.")
    parser.add_argument("--label", action="append", help="Label for each video. Can be repeated.")
    parser.add_argument("--output", required=True, help="Output PNG path.")
    parser.add_argument("--samples", type=int, default=8, help="Frames to sample from each video.")
    parser.add_argument("--thumb-width", type=int, default=180, help="Thumbnail width.")
    parser.add_argument("--start", type=float, default=0.2, help="Start time in seconds.")
    parser.add_argument("--end", type=float, help="End time in seconds. Defaults to video duration.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    videos = [Path(item) for item in args.video]
    labels = args.label or [path.parent.name for path in videos]
    if len(labels) != len(videos):
        raise ValueError("--label count must match --video count")

    rows: list[list[Image.Image]] = []
    for path in videos:
        rows.append(sample_video(path, args.samples, args.thumb_width, args.start, args.end))

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    sheet = compose_sheet(rows, labels, args.thumb_width)
    sheet.save(output_path)
    print(f"contact sheet -> {output_path}")
    return 0


def sample_video(
    path: Path,
    samples: int,
    thumb_width: int,
    start: float,
    end: float | None,
) -> list[Image.Image]:
    capture = cv2.VideoCapture(str(path))
    if not capture.isOpened():
        raise RuntimeError(f"Could not open video: {path}")

    fps = capture.get(cv2.CAP_PROP_FPS) or 24.0
    frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    duration = frame_count / fps if frame_count and fps else 0.0
    sample_end = min(end if end is not None else duration, duration)
    sample_start = min(max(0.0, start), max(0.0, sample_end - 0.01))
    if samples <= 1:
        times = [(sample_start + sample_end) / 2.0]
    else:
        span = max(0.01, sample_end - sample_start)
        times = [sample_start + span * index / (samples - 1) for index in range(samples)]

    frames: list[Image.Image] = []
    for timestamp in times:
        capture.set(cv2.CAP_PROP_POS_MSEC, timestamp * 1000.0)
        ok, frame = capture.read()
        if not ok:
            continue
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        image = Image.fromarray(rgb)
        target_height = max(1, round(image.height * (thumb_width / image.width)))
        frames.append(image.resize((thumb_width, target_height), Image.Resampling.LANCZOS))

    capture.release()
    if not frames:
        raise RuntimeError(f"No frames sampled from video: {path}")
    return frames


def compose_sheet(rows: list[list[Image.Image]], labels: list[str], thumb_width: int) -> Image.Image:
    gap = 10
    label_width = 110
    label_height = 28
    font = ImageFont.load_default()
    thumb_height = max(frame.height for row in rows for frame in row)
    row_height = max(label_height, thumb_height)
    cols = max(len(row) for row in rows)
    width = label_width + cols * thumb_width + (cols + 1) * gap
    height = len(rows) * row_height + (len(rows) + 1) * gap
    sheet = Image.new("RGB", (width, height), (245, 245, 245))
    draw = ImageDraw.Draw(sheet)

    for row_index, row in enumerate(rows):
        y = gap + row_index * (row_height + gap)
        draw.text((gap, y + 8), labels[row_index], fill=(20, 20, 20), font=font)
        for col_index, frame in enumerate(row):
            x = label_width + gap + col_index * (thumb_width + gap)
            frame_y = y + (row_height - frame.height) // 2
            sheet.paste(frame, (x, frame_y))
    return sheet


if __name__ == "__main__":
    raise SystemExit(main())
