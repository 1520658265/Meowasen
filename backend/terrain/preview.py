from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image

from backend.storage.manifest import find_candidate, read_manifest
from backend.storage.paths import resolve_task_dir


AUTOTILE16 = {
    "center": 0,
    "edge_top": 1,
    "edge_bottom": 2,
    "edge_left": 3,
    "edge_right": 4,
    "outer_tl": 5,
    "outer_tr": 6,
    "outer_bl": 7,
    "outer_br": 8,
    "inner_tl": 9,
    "inner_tr": 10,
    "inner_bl": 11,
    "inner_br": 12,
    "island": 13,
    "horizontal": 14,
    "vertical": 15,
}

BIT_N = 1 << 0
BIT_E = 1 << 1
BIT_S = 1 << 2
BIT_W = 1 << 3
BIT_NE = 1 << 4
BIT_SE = 1 << 5
BIT_SW = 1 << 6
BIT_NW = 1 << 7


def _build_blob47_masks() -> list[int]:
    masks: list[int] = []
    cardinals = [
        ("n", BIT_N),
        ("e", BIT_E),
        ("s", BIT_S),
        ("w", BIT_W),
    ]
    for cardinal_select in range(16):
        mask = 0
        present: set[str] = set()
        for index, (name, bit) in enumerate(cardinals):
            if cardinal_select & (1 << index):
                mask |= bit
                present.add(name)

        allowed: list[int] = []
        if "n" in present and "e" in present:
            allowed.append(BIT_NE)
        if "s" in present and "e" in present:
            allowed.append(BIT_SE)
        if "s" in present and "w" in present:
            allowed.append(BIT_SW)
        if "n" in present and "w" in present:
            allowed.append(BIT_NW)

        for diagonal_select in range(1 << len(allowed)):
            value = mask
            for index, bit in enumerate(allowed):
                if diagonal_select & (1 << index):
                    value |= bit
            masks.append(value)
    return sorted(masks, key=lambda value: (_bit_count(value & 0x0F), _bit_count(value), value))


def _bit_count(value: int) -> int:
    return bin(value).count("1")


BLOB47_MASKS = _build_blob47_masks()
BLOB47_ROLES = [f"blob_{mask:03d}" for mask in BLOB47_MASKS] + ["neutral_base"]
BLOB47 = {role: index for index, role in enumerate(BLOB47_ROLES)}


SHAPES = {
    "blob": [
        "000000000000",
        "000111100000",
        "001111110000",
        "011111111000",
        "011111111100",
        "001111111100",
        "000111111000",
        "000011100000",
        "000000000000",
    ],
    "pond": [
        "000000000000",
        "000011100000",
        "001111111000",
        "011111111100",
        "011111111100",
        "001111111000",
        "000011100000",
        "000000000000",
    ],
    "road": [
        "000000000000",
        "000000000000",
        "111110000000",
        "000111111000",
        "000000111111",
        "000000000000",
    ],
    "donut": [
        "000000000000",
        "000111111000",
        "001111111100",
        "011110011110",
        "011100001110",
        "011110011110",
        "001111111100",
        "000111111000",
        "000000000000",
    ],
    "irregular": [
        "00000000000000",
        "00011100000000",
        "00111111000000",
        "01111111110000",
        "00111111111000",
        "01111111111100",
        "11111101111100",
        "01111100111000",
        "00111111100000",
        "00001111000000",
        "00000000000000",
    ],
}


@dataclass(frozen=True)
class TerrainPreviewParams:
    task_id: str
    candidate_index: int = 0
    shape: str = "blob"
    output_name: str | None = None
    animation_task_id: str | None = None
    animation_candidate_index: int = 0
    animation_row: int = 0
    assets_dir: Path = Path("assets")


def build_preview(params: TerrainPreviewParams) -> dict[str, Any]:
    task_path = resolve_task_dir(params.assets_dir, params.task_id)
    manifest = read_manifest(task_path)
    candidate = find_candidate(manifest, params.candidate_index)
    tile_size = int(manifest.get("output_size") or 64)
    frame_layout = str(manifest.get("frame_layout") or "")
    if frame_layout == "terrain_blob47":
        tile_order = BLOB47
        chooser = choose_blob47_tile
    else:
        tile_order = AUTOTILE16
        chooser = choose_tile
    tiles = _load_candidate_tiles(task_path, candidate, tile_size, tile_order)
    variant_tiles = _load_variant_tiles(task_path, candidate, tile_size, tile_order, manifest)
    mask = _shape_mask(params.shape)

    output_name = params.output_name or f"terrain_preview_{params.shape}.png"
    preview = _compose(mask, tiles, tile_size, chooser, variant_tiles)
    preview_path = task_path / output_name
    preview.save(preview_path)

    animation_outputs: list[str] = []
    if params.animation_task_id:
        animation_outputs = _compose_animation_previews(
            params=params,
            mask=mask,
            tiles=tiles,
            variant_tiles=variant_tiles,
            tile_size=tile_size,
            output_stem=Path(output_name).stem,
        )

    data = {
        "task_id": params.task_id,
        "candidate_index": params.candidate_index,
        "shape": params.shape,
        "tile_size": tile_size,
        "output": output_name,
        "animation_outputs": animation_outputs,
        "variant_sheet": candidate.get("variant_sheet"),
        "tile_order": tile_order,
    }
    sidecar = preview_path.with_suffix(".json")
    sidecar.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data


def choose_tile(mask: list[list[bool]], row: int, col: int) -> str:
    def filled(r: int, c: int) -> bool:
        return 0 <= r < len(mask) and 0 <= c < len(mask[r]) and mask[r][c]

    n = filled(row - 1, col)
    s = filled(row + 1, col)
    w = filled(row, col - 1)
    e = filled(row, col + 1)
    nw = filled(row - 1, col - 1)
    ne = filled(row - 1, col + 1)
    sw = filled(row + 1, col - 1)
    se = filled(row + 1, col + 1)

    if not any([n, s, w, e]):
        return "island"
    if w and e and not n and not s:
        return "horizontal"
    if n and s and not w and not e:
        return "vertical"

    if not n and not w:
        return "outer_tl"
    if not n and not e:
        return "outer_tr"
    if not s and not w:
        return "outer_bl"
    if not s and not e:
        return "outer_br"

    if n and s and w and e:
        if not nw:
            return "inner_tl"
        if not ne:
            return "inner_tr"
        if not sw:
            return "inner_bl"
        if not se:
            return "inner_br"
        return "center"

    if not n:
        return "edge_top"
    if not s:
        return "edge_bottom"
    if not w:
        return "edge_left"
    if not e:
        return "edge_right"
    return "center"


def choose_blob47_tile(mask: list[list[bool]], row: int, col: int) -> str:
    def filled(r: int, c: int) -> bool:
        return 0 <= r < len(mask) and 0 <= c < len(mask[r]) and mask[r][c]

    bits = 0
    if filled(row - 1, col):
        bits |= BIT_N
    if filled(row, col + 1):
        bits |= BIT_E
    if filled(row + 1, col):
        bits |= BIT_S
    if filled(row, col - 1):
        bits |= BIT_W
    if filled(row - 1, col + 1):
        bits |= BIT_NE
    if filled(row + 1, col + 1):
        bits |= BIT_SE
    if filled(row + 1, col - 1):
        bits |= BIT_SW
    if filled(row - 1, col - 1):
        bits |= BIT_NW
    bits = normalize_blob47_mask(bits)
    return f"blob_{bits:03d}"


def normalize_blob47_mask(bits: int) -> int:
    if not (bits & BIT_N and bits & BIT_E):
        bits &= ~BIT_NE
    if not (bits & BIT_S and bits & BIT_E):
        bits &= ~BIT_SE
    if not (bits & BIT_S and bits & BIT_W):
        bits &= ~BIT_SW
    if not (bits & BIT_N and bits & BIT_W):
        bits &= ~BIT_NW
    return bits


def _compose(
    mask: list[list[bool]],
    tiles: dict[str, Image.Image],
    tile_size: int,
    chooser: Any = choose_tile,
    variant_tiles: dict[str, list[Image.Image]] | None = None,
) -> Image.Image:
    height = len(mask)
    width = max(len(row) for row in mask)
    image = Image.new("RGBA", (width * tile_size, height * tile_size), (0, 0, 0, 0))
    for row_index, row in enumerate(mask):
        for col_index, enabled in enumerate(row):
            if not enabled:
                continue
            tile_id = chooser(mask, row_index, col_index)
            tile = _select_variant_tile(tiles, variant_tiles or {}, tile_id, row_index, col_index)
            image.alpha_composite(tile, (col_index * tile_size, row_index * tile_size))
    return image


def _compose_animation_previews(
    params: TerrainPreviewParams,
    mask: list[list[bool]],
    tiles: dict[str, Image.Image],
    variant_tiles: dict[str, list[Image.Image]],
    tile_size: int,
    output_stem: str,
) -> list[str]:
    task_path = resolve_task_dir(params.assets_dir, params.task_id)
    animation_path = resolve_task_dir(params.assets_dir, params.animation_task_id)
    animation_manifest = read_manifest(animation_path)
    animation_candidate = find_candidate(animation_manifest, params.animation_candidate_index)
    frames = [
        frame
        for frame in animation_candidate.get("frames", [])
        if int((frame.get("grid_pos") or [0, 0])[0]) == params.animation_row
    ]
    frames.sort(key=lambda frame: int((frame.get("grid_pos") or [0, 0])[1]))
    outputs: list[str] = []
    for index, frame in enumerate(frames):
        processed = frame.get("processed")
        if not processed:
            continue
        frame_tile = Image.open(animation_path / processed).convert("RGBA")
        if frame_tile.size != (tile_size, tile_size):
            frame_tile = frame_tile.resize((tile_size, tile_size), Image.Resampling.NEAREST)
        animated_tiles = dict(tiles)
        animated_tiles["center"] = frame_tile
        chooser = choose_blob47_tile if "neutral_base" in tiles else choose_tile
        image = _compose(mask, animated_tiles, tile_size, chooser, variant_tiles)
        output = f"{output_stem}_anim_{index}.png"
        image.save(task_path / output)
        outputs.append(output)
    return outputs


def _load_candidate_tiles(
    task_path: Path,
    candidate: dict[str, Any],
    tile_size: int,
    tile_order: dict[str, int],
) -> dict[str, Image.Image]:
    by_index = {
        int(frame["frame_index"]): frame
        for frame in candidate.get("frames", [])
        if frame.get("processed")
    }
    tiles: dict[str, Image.Image] = {}
    for tile_id, frame_index in tile_order.items():
        frame = by_index.get(frame_index)
        if not frame:
            raise ValueError(f"Missing autotile frame {frame_index}: {tile_id}")
        image = Image.open(task_path / frame["processed"]).convert("RGBA")
        if image.size != (tile_size, tile_size):
            image = image.resize((tile_size, tile_size), Image.Resampling.NEAREST)
        tiles[tile_id] = image
    return tiles


def _load_variant_tiles(
    task_path: Path,
    candidate: dict[str, Any],
    tile_size: int,
    tile_order: dict[str, int],
    manifest: dict[str, Any],
) -> dict[str, list[Image.Image]]:
    variant_sheet = candidate.get("variant_sheet")
    if not variant_sheet:
        return {}
    sheet_path = task_path / str(variant_sheet)
    if not sheet_path.exists():
        return {}

    frame_grid = manifest.get("frame_grid") or [4, 4]
    rows = int(frame_grid[0])
    cols = int(frame_grid[1])
    sheet = Image.open(sheet_path).convert("RGBA")
    row_block_count = sheet.height // max(1, rows * tile_size)
    if row_block_count <= 1:
        return {}

    result: dict[str, list[Image.Image]] = {}
    for variant_row in range(1, row_block_count):
        for tile_id, frame_index in tile_order.items():
            col = frame_index % cols
            row = frame_index // cols
            box = (
                col * tile_size,
                (variant_row * rows + row) * tile_size,
                (col + 1) * tile_size,
                (variant_row * rows + row + 1) * tile_size,
            )
            if box[2] > sheet.width or box[3] > sheet.height:
                continue
            result.setdefault(tile_id, []).append(sheet.crop(box))
    return result


def _select_variant_tile(
    tiles: dict[str, Image.Image],
    variant_tiles: dict[str, list[Image.Image]],
    role: str,
    row: int,
    col: int,
) -> Image.Image:
    variants = variant_tiles.get(role) or []
    if not variants:
        return tiles[role]
    role_hash = _stable_role_hash(role)
    value = (row + 1) * 92821 ^ (col + 1) * 68917 ^ role_hash * 31337
    value ^= value >> 16
    value *= 0x7FEB352D
    value ^= value >> 15
    index = value % (len(variants) + 1)
    if index == 0:
        return tiles[role]
    return variants[index - 1]


def _stable_role_hash(role: str) -> int:
    value = 2166136261
    for char in role:
        value ^= ord(char)
        value = (value * 16777619) & 0xFFFFFFFF
    return value


def _shape_mask(name: str) -> list[list[bool]]:
    if name not in SHAPES:
        raise ValueError(f"Unsupported shape: {name}. Available: {', '.join(sorted(SHAPES))}")
    rows = SHAPES[name]
    return [[char == "1" for char in row] for row in rows]
