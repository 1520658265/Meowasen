from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw


BG = (255, 0, 255, 255)
INK = (18, 20, 24, 255)
TORSO = (30, 118, 210, 255)
LEFT = (224, 72, 72, 255)
RIGHT = (42, 164, 91, 255)
WEAPON = (92, 55, 176, 255)
ANCHOR = (255, 214, 51, 255)
GUIDE = (255, 255, 255, 110)


@dataclass(frozen=True)
class Pose:
    row: int
    col: int
    direction: str
    phase: str
    body_shift: tuple[int, int]
    left_foot: tuple[int, int]
    right_foot: tuple[int, int]
    left_hand: tuple[int, int]
    right_hand: tuple[int, int]
    weapon_tip: tuple[int, int]
    weapon_base: tuple[int, int]
    tail_tip: tuple[int, int]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create a 4x4 RPG walk pose guide sheet.")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--tile-size", type=int, default=256)
    parser.add_argument("--rows", type=int, default=4)
    parser.add_argument("--cols", type=int, default=4)
    parser.add_argument("--prefix", default="pose_walk_3dir_4x4")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    poses = make_poses(args.tile_size)
    sheet = draw_sheet(poses, args.tile_size, args.rows, args.cols, labels=False)
    overlay = draw_sheet(poses, args.tile_size, args.rows, args.cols, labels=True)
    spec = {
        "kind": "rpg_walk_pose_guide",
        "layout": "walk_3dir_4x4",
        "tile_size": args.tile_size,
        "rows": args.rows,
        "cols": args.cols,
        "row_contract": [
            "row 1: down/front walk cycle",
            "row 2: left-facing walk cycle",
            "row 3: up/back walk cycle",
            "row 4: helper duplicate of left-facing walk cycle",
        ],
        "column_contract": [
            "column 1: left foot contact",
            "column 2: passing pose, body up",
            "column 3: right foot contact",
            "column 4: passing pose, body down, loop-ready",
        ],
        "color_contract": {
            "background": "#FF00FF chroma key",
            "black": "skeleton contour and joints",
            "blue": "torso line",
            "red": "left leg and left arm",
            "green": "right leg and right arm",
            "purple": "locked weapon guide",
            "yellow": "feet anchor marker",
        },
        "poses": [pose_to_dict(pose) for pose in poses],
    }
    sheet_path = output_dir / f"{args.prefix}.png"
    overlay_path = output_dir / f"{args.prefix}_overlay_preview.png"
    spec_path = output_dir / f"{args.prefix}_spec.json"
    sheet.save(sheet_path, format="PNG")
    overlay.save(overlay_path, format="PNG")
    spec_path.write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "pose_sheet": str(sheet_path),
                "overlay_preview": str(overlay_path),
                "spec": str(spec_path),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def make_poses(tile: int) -> list[Pose]:
    poses: list[Pose] = []
    phases = [
        ("left_contact", -14, 10, 12, -4, 0, 0),
        ("passing_up", -5, 5, 5, -2, 0, -7),
        ("right_contact", 12, -4, -14, 10, 0, 0),
        ("passing_down", 5, -2, -5, 5, 0, 5),
    ]
    directions = ["down_front", "left", "up_back", "left_helper"]
    for row, direction in enumerate(directions):
        for col, (phase, lf_x, lf_y, rf_x, rf_y, bob_x, bob_y) in enumerate(phases):
            poses.append(make_pose(tile, row, col, direction, phase, (bob_x, bob_y), lf_x, lf_y, rf_x, rf_y))
    return poses


def make_pose(
    tile: int,
    row: int,
    col: int,
    direction: str,
    phase: str,
    body_shift: tuple[int, int],
    left_foot_x: int,
    left_foot_y: int,
    right_foot_x: int,
    right_foot_y: int,
) -> Pose:
    cx = tile // 2 + body_shift[0]
    cy = int(tile * 0.49) + body_shift[1]
    foot_y = int(tile * 0.76)
    if direction in {"left", "left_helper"}:
        # Side view: foot motion is horizontal and should be visible.
        left_foot = (cx - 12 + left_foot_x, foot_y + left_foot_y // 2)
        right_foot = (cx + 12 + right_foot_x, foot_y + right_foot_y // 2)
        left_hand = (cx - 28, cy + 10)
        right_hand = (cx + 14, cy + 8)
        weapon_base = (cx - 34, cy + 52)
        weapon_tip = (cx - 34, cy - 70)
        tail_tip = (cx + 62, cy + 36)
    elif direction == "up_back":
        left_foot = (cx - 16 + left_foot_x // 2, foot_y + left_foot_y)
        right_foot = (cx + 16 + right_foot_x // 2, foot_y + right_foot_y)
        left_hand = (cx - 28, cy + 5)
        right_hand = (cx + 28, cy + 5)
        weapon_base = (cx + 38, cy + 52)
        weapon_tip = (cx + 38, cy - 72)
        tail_tip = (cx - 50, cy + 44)
    else:
        left_foot = (cx - 16 + left_foot_x // 2, foot_y + left_foot_y)
        right_foot = (cx + 16 + right_foot_x // 2, foot_y + right_foot_y)
        left_hand = (cx - 28, cy + 8)
        right_hand = (cx + 28, cy + 8)
        weapon_base = (cx - 42, cy + 54)
        weapon_tip = (cx - 42, cy - 72)
        tail_tip = (cx + 58, cy + 34)
    return Pose(
        row=row,
        col=col,
        direction=direction,
        phase=phase,
        body_shift=body_shift,
        left_foot=left_foot,
        right_foot=right_foot,
        left_hand=left_hand,
        right_hand=right_hand,
        weapon_tip=weapon_tip,
        weapon_base=weapon_base,
        tail_tip=tail_tip,
    )


def draw_sheet(poses: list[Pose], tile: int, rows: int, cols: int, labels: bool) -> Image.Image:
    image = Image.new("RGBA", (cols * tile, rows * tile), BG)
    draw = ImageDraw.Draw(image)
    for pose in poses:
        ox = pose.col * tile
        oy = pose.row * tile
        draw_cell(draw, ox, oy, tile, pose, labels=labels)
    return image


def draw_cell(draw: ImageDraw.ImageDraw, ox: int, oy: int, tile: int, pose: Pose, labels: bool) -> None:
    cx = ox + tile // 2 + pose.body_shift[0]
    cy = oy + int(tile * 0.49) + pose.body_shift[1]
    head = (cx, cy - 54)
    neck = (cx, cy - 28)
    hips = (cx, cy + 30)
    left_foot = offset(pose.left_foot, ox, oy)
    right_foot = offset(pose.right_foot, ox, oy)
    left_hand = offset(pose.left_hand, ox, oy)
    right_hand = offset(pose.right_hand, ox, oy)
    weapon_tip = offset(pose.weapon_tip, ox, oy)
    weapon_base = offset(pose.weapon_base, ox, oy)
    tail_tip = offset(pose.tail_tip, ox, oy)

    if labels:
        draw.rectangle([ox, oy, ox + tile - 1, oy + tile - 1], outline=GUIDE, width=2)
        draw.line([ox + tile // 2, oy + 10, ox + tile // 2, oy + tile - 10], fill=GUIDE, width=1)
        draw.line([ox + 18, oy + int(tile * 0.76), ox + tile - 18, oy + int(tile * 0.76)], fill=GUIDE, width=1)

    draw.line([weapon_base, weapon_tip], fill=WEAPON, width=7)
    draw_joint(draw, weapon_tip, WEAPON, 7)
    draw_joint(draw, weapon_base, WEAPON, 6)

    draw.line([(cx, cy - 30), (cx, cy + 30)], fill=TORSO, width=8)
    draw.line([(cx, cy - 18), left_hand], fill=LEFT, width=6)
    draw.line([(cx, cy - 18), right_hand], fill=RIGHT, width=6)
    draw.line([hips, left_foot], fill=LEFT, width=7)
    draw.line([hips, right_foot], fill=RIGHT, width=7)
    draw.line([hips, tail_tip], fill=INK, width=5)

    draw.ellipse([head[0] - 24, head[1] - 22, head[0] + 24, head[1] + 22], outline=INK, width=5)
    draw.polygon([(head[0] - 18, head[1] - 22), (head[0] - 4, head[1] - 46), (head[0] + 4, head[1] - 20)], outline=INK)
    draw.polygon([(head[0] + 18, head[1] - 22), (head[0] + 4, head[1] - 46), (head[0] - 4, head[1] - 20)], outline=INK)

    for point, color, radius in [
        (neck, TORSO, 6),
        (hips, TORSO, 7),
        (left_hand, LEFT, 6),
        (right_hand, RIGHT, 6),
        (left_foot, LEFT, 8),
        (right_foot, RIGHT, 8),
        (tail_tip, INK, 5),
    ]:
        draw_joint(draw, point, color, radius)
    draw.line([ox + 46, oy + int(tile * 0.82), ox + tile - 46, oy + int(tile * 0.82)], fill=ANCHOR, width=3)


def draw_joint(draw: ImageDraw.ImageDraw, point: tuple[int, int], color: tuple[int, int, int, int], radius: int) -> None:
    x, y = point
    draw.ellipse([x - radius, y - radius, x + radius, y + radius], fill=color, outline=INK, width=2)


def offset(point: tuple[int, int], ox: int, oy: int) -> tuple[int, int]:
    return (point[0] + ox, point[1] + oy)


def pose_to_dict(pose: Pose) -> dict[str, Any]:
    return {
        "row": pose.row,
        "col": pose.col,
        "frame_index": pose.row * 4 + pose.col,
        "direction": pose.direction,
        "phase": pose.phase,
        "body_shift": list(pose.body_shift),
        "left_foot": list(pose.left_foot),
        "right_foot": list(pose.right_foot),
        "left_hand": list(pose.left_hand),
        "right_hand": list(pose.right_hand),
        "weapon_tip": list(pose.weapon_tip),
        "weapon_base": list(pose.weapon_base),
        "tail_tip": list(pose.tail_tip),
    }


if __name__ == "__main__":
    raise SystemExit(main())
