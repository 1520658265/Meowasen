from __future__ import annotations

import argparse
import json
import math
import subprocess
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Extract candidate PNG frames from a generated video with ffmpeg.")
    parser.add_argument("--video", required=True)
    parser.add_argument("--output-dir")
    parser.add_argument("--start", type=float, default=0.5)
    parser.add_argument("--end", type=float, default=4.5)
    parser.add_argument("--fps", type=float, default=8.0)
    parser.add_argument("--width", type=int, default=512)
    parser.add_argument("--prefix", default="candidate")
    parser.add_argument("--ffmpeg", default="ffmpeg")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    video_path = Path(args.video)
    if not video_path.is_absolute():
        video_path = ROOT_DIR / video_path
    if not video_path.exists():
        raise FileNotFoundError(f"Video not found: {video_path}")

    output_dir = Path(args.output_dir) if args.output_dir else video_path.parent / "frames"
    if not output_dir.is_absolute():
        output_dir = ROOT_DIR / output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    duration = max(0.0, args.end - args.start)
    output_pattern = output_dir / f"{args.prefix}_%04d.png"
    vf = f"fps={args.fps},scale={args.width}:-2"
    command = [
        args.ffmpeg,
        "-y",
        "-ss",
        str(args.start),
        "-t",
        str(duration),
        "-i",
        str(video_path),
        "-vf",
        vf,
        str(output_pattern),
    ]
    try:
        subprocess.run(command, check=True)
    except FileNotFoundError:
        extract_with_cv2(
            video_path=video_path,
            output_dir=output_dir,
            prefix=args.prefix,
            start=args.start,
            end=args.end,
            fps=args.fps,
            width=args.width,
        )

    frames = sorted(output_dir.glob(f"{args.prefix}_*.png"))
    meta = {
        "video": str(video_path),
        "output_dir": str(output_dir),
        "start": args.start,
        "end": args.end,
        "fps": args.fps,
        "width": args.width,
        "expected_frame_count": math.ceil(duration * args.fps),
        "frames": [str(path) for path in frames],
    }
    (output_dir / "extract_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output_dir": str(output_dir), "frames": len(frames)}, ensure_ascii=False, indent=2))
    return 0


def extract_with_cv2(
    *,
    video_path: Path,
    output_dir: Path,
    prefix: str,
    start: float,
    end: float,
    fps: float,
    width: int,
) -> None:
    import cv2

    capture = cv2.VideoCapture(str(video_path))
    if not capture.isOpened():
        raise RuntimeError(f"OpenCV could not open video: {video_path}")
    source_fps = capture.get(cv2.CAP_PROP_FPS) or 24.0
    total_frames = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    source_duration = total_frames / source_fps if source_fps else max(end, 0.0)
    end = min(end, source_duration) if source_duration > 0 else end
    if end <= start:
        end = source_duration
    frame_times = []
    current = start
    while current < end - 1e-6:
        frame_times.append(current)
        current += 1.0 / fps

    for index, timestamp in enumerate(frame_times, start=1):
        capture.set(cv2.CAP_PROP_POS_MSEC, timestamp * 1000.0)
        ok, frame = capture.read()
        if not ok:
            continue
        height, src_width = frame.shape[:2]
        target_height = max(2, round(height * (width / src_width)))
        resized = cv2.resize(frame, (width, target_height), interpolation=cv2.INTER_AREA)
        out_path = output_dir / f"{prefix}_{index:04d}.png"
        cv2.imwrite(str(out_path), resized)
    capture.release()


if __name__ == "__main__":
    raise SystemExit(main())
