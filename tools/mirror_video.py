from __future__ import annotations

import argparse
from pathlib import Path

import cv2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Mirror a video horizontally with OpenCV.")
    parser.add_argument("--input", required=True, help="Input video path.")
    parser.add_argument("--output", required=True, help="Output video path.")
    parser.add_argument("--fourcc", default="mp4v", help="OpenCV fourcc code, default: mp4v.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    input_path = Path(args.input)
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    capture = cv2.VideoCapture(str(input_path))
    if not capture.isOpened():
        raise RuntimeError(f"Could not open input video: {input_path}")

    fps = capture.get(cv2.CAP_PROP_FPS) or 24.0
    width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
    if width <= 0 or height <= 0:
        raise RuntimeError(f"Could not read video dimensions: {input_path}")

    writer = cv2.VideoWriter(
        str(output_path),
        cv2.VideoWriter_fourcc(*args.fourcc),
        fps,
        (width, height),
    )
    if not writer.isOpened():
        capture.release()
        raise RuntimeError(f"Could not open output video: {output_path}")

    frames = 0
    while True:
        ok, frame = capture.read()
        if not ok:
            break
        writer.write(cv2.flip(frame, 1))
        frames += 1

    capture.release()
    writer.release()
    print(f"mirrored {frames} frames -> {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
