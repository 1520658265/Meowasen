from __future__ import annotations

import json
import math
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageOps

from backend.resource_library import FindLibraryParams, find_library
from backend.storage.paths import task_dir
from backend.terrain.preview import BLOB47, SHAPES, choose_blob47_tile


@dataclass(frozen=True)
class SceneTerrainBuildParams:
    scene: str
    task_id: str | None = None
    theme: str | None = None
    tile_size: int | None = 64
    art_style: str = "rpg"
    profile_path: Path | None = None
    library_dir: Path = Path("assets/library")
    assets_dir: Path = Path("assets")


DEFAULT_PROFILE_DATA: dict[str, Any] = {
    "themes": {}
}


def build_scene_terrain_tileset(params: SceneTerrainBuildParams) -> dict[str, Any]:
    if not params.scene.strip():
        raise ValueError("scene must not be empty")
    if params.tile_size is not None and params.tile_size < 16:
        raise ValueError("tile_size must be at least 16")
    if params.art_style not in {"source", "rpg"}:
        raise ValueError("art_style must be source or rpg")

    profile_data = _load_profile_data(params.profile_path)
    theme_id = _select_theme(params.scene, params.theme, profile_data)
    requirements = _plan_requirements(params.scene, theme_id, profile_data)
    if not requirements:
        raise ValueError(f"No terrain requirements inferred for scene: {params.scene}")

    task_id = params.task_id or f"scene_terrain_{uuid.uuid4()}"
    task_path = task_dir(params.assets_dir, task_id, "scenes")

    resolved: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    for requirement in requirements:
        match = _find_best_terrain(params.library_dir, theme_id, requirement["tags"])
        requirement_result = dict(requirement)
        if not match:
            requirement_result["status"] = "missing"
            missing.append(requirement_result)
            continue
        requirement_result["status"] = "resolved"
        requirement_result["library_match"] = match
        resolved.append(requirement_result)

    outputs: dict[str, str | None] = {
        "scene_tileset": None,
        "scene_tileset_variants": None,
        "scene_tileset_used": None,
        "scene_preview": None,
    }
    sections: list[dict[str, Any]] = []
    used_index: list[dict[str, Any]] = []
    layout: list[dict[str, Any]] = []

    if resolved:
        loaded = [_load_family(params.library_dir, item, params.tile_size, params.art_style) for item in resolved]
        tileset, sections = _compose_full_tileset(loaded)
        tileset.save(task_path / "scene_tileset.png")
        outputs["scene_tileset"] = "scene_tileset.png"

        variant_sheet, variant_sections = _compose_variant_tileset(loaded)
        if variant_sheet:
            variant_sheet.save(task_path / "scene_tileset_variants.png")
            outputs["scene_tileset_variants"] = "scene_tileset_variants.png"
            variant_by_feature = {item["feature_id"]: item for item in variant_sections}
            for section in sections:
                variant_section = variant_by_feature.get(section["feature_id"])
                if not variant_section:
                    continue
                section["variant_start_row"] = variant_section["start_row"]
                section["variant_row_count"] = variant_section["row_count"]

        preview, used_roles, layout = _compose_scene_preview(loaded)
        preview.save(task_path / "scene_preview.png")
        outputs["scene_preview"] = "scene_preview.png"

        used_tileset, used_index = _compose_used_tileset(loaded, used_roles)
        used_tileset.save(task_path / "scene_tileset_used.png")
        outputs["scene_tileset_used"] = "scene_tileset_used.png"

    now = datetime.now(UTC).isoformat()
    manifest = {
        "schema_version": 1,
        "kind": "scene_terrain_tileset",
        "task_id": task_id,
        "status": "done" if not missing else "needs_generation",
        "created_at": now,
        "updated_at": now,
        "scene": params.scene,
        "theme": theme_id,
        "tile_size": params.tile_size,
        "art_style": params.art_style,
        "style_contract": (
            "RPG game art is the project default. source style is only for "
            "debug inspection of library inputs, not final scene output."
        ),
        "view": "top_down_orthographic_ground",
        "requirements": resolved + missing,
        "missing_requirements": missing,
        "library_root": str(params.library_dir),
        "outputs": outputs,
        "tileset_sections": sections,
        "used_tiles": used_index,
        "preview_layout": layout,
        "rule": (
            "scene text -> profile-driven terrain requirements -> library match -> "
            "stacked reusable terrain tileset and semantic assembly preview"
        ),
    }
    (task_path / "scene_terrain_plan.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return manifest


def _load_profile_data(profile_path: Path | None) -> dict[str, Any]:
    default_path = Path(__file__).with_name("scene_profiles.json")
    if default_path.exists():
        data = json.loads(default_path.read_text(encoding="utf-8"))
    else:
        data = json.loads(json.dumps(DEFAULT_PROFILE_DATA, ensure_ascii=False))
    if not profile_path:
        return data
    if not profile_path.exists():
        raise FileNotFoundError(f"Scene terrain profile not found: {profile_path}")
    extra = json.loads(profile_path.read_text(encoding="utf-8"))
    for theme_id, theme_data in (extra.get("themes") or {}).items():
        if theme_id not in data["themes"]:
            data["themes"][theme_id] = theme_data
            continue
        current = data["themes"][theme_id]
        current["keywords"] = _merge_list(current.get("keywords"), theme_data.get("keywords"))
        current["default_features"] = _merge_list(
            current.get("default_features"),
            theme_data.get("default_features"),
        )
        existing_features = {item["id"]: item for item in current.get("features", [])}
        for feature in theme_data.get("features", []):
            existing_features[feature["id"]] = feature
        current["features"] = list(existing_features.values())
    return data


def _merge_list(left: Any, right: Any) -> list[Any]:
    result = []
    for item in list(left or []) + list(right or []):
        if item not in result:
            result.append(item)
    return result


def _select_theme(scene: str, explicit_theme: str | None, profile_data: dict[str, Any]) -> str:
    themes = profile_data.get("themes") or {}
    if explicit_theme:
        if explicit_theme not in themes:
            raise ValueError(f"Unknown terrain scene theme: {explicit_theme}")
        return explicit_theme

    scene_text = scene.lower()
    best_theme = None
    best_score = 0
    for theme_id, theme_data in themes.items():
        score = sum(1 for item in theme_data.get("keywords", []) if str(item).lower() in scene_text)
        if score > best_score:
            best_score = score
            best_theme = theme_id
    if best_theme:
        return best_theme
    raise ValueError("Could not infer scene terrain theme. Pass --theme or add a profile.")


def _plan_requirements(scene: str, theme_id: str, profile_data: dict[str, Any]) -> list[dict[str, Any]]:
    theme_data = (profile_data.get("themes") or {}).get(theme_id)
    if not theme_data:
        raise ValueError(f"Unknown terrain scene theme: {theme_id}")

    scene_text = scene.lower()
    defaults = set(theme_data.get("default_features") or [])
    planned: list[dict[str, Any]] = []
    for feature in theme_data.get("features", []):
        feature_id = str(feature["id"])
        aliases = [str(item).lower() for item in feature.get("aliases", [])]
        mentioned = any(alias and alias in scene_text for alias in aliases)
        if feature_id not in defaults and not mentioned:
            continue
        planned.append(
            {
                "feature_id": feature_id,
                "label": feature.get("label") or feature_id,
                "tags": list(feature.get("tags") or []),
                "terrain_kind": feature.get("terrain_kind") or "blob",
                "shape": feature.get("shape") or "irregular",
                "composite": feature.get("composite"),
                "position": list(feature.get("position") or [0, 0]),
                "priority": int(feature.get("priority") or 100),
            }
        )
    planned.sort(key=lambda item: (item["priority"], item["feature_id"]))
    return planned


def _find_best_terrain(library_dir: Path, theme: str, tags: list[str]) -> dict[str, Any] | None:
    result = find_library(
        FindLibraryParams(
            kind="terrain",
            theme=theme,
            tags=tuple(tags),
            limit=1,
            root_dir=library_dir,
        )
    )
    matches = result.get("matches") or []
    if not matches:
        return None
    match = matches[0]
    if match.get("missing_tags"):
        return None
    return {
        "score": match.get("score"),
        "matched_tags": match.get("matched_tags"),
        "item": match.get("item"),
    }


def _load_family(
    library_dir: Path,
    requirement: dict[str, Any],
    target_tile_size: int | None,
    art_style: str,
) -> dict[str, Any]:
    item = requirement["library_match"]["item"]
    manifest_path = library_dir / str(item["paths"]["manifest"])
    if not manifest_path.exists():
        raise FileNotFoundError(f"Terrain manifest not found: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    source_tile_size = int(item.get("tile_size") or manifest.get("output_size") or 64)
    tile_size = int(target_tile_size or source_tile_size)
    frame_grid = item.get("frame_grid") or manifest.get("frame_grid") or [6, 8]
    rows, cols = int(frame_grid[0]), int(frame_grid[1])
    tile_order = _tile_order_from_manifest(manifest)
    sheet_path = library_dir / str(item["paths"]["sheet"])
    tiles = _slice_sheet(sheet_path, source_tile_size, rows, cols, tile_order, tile_size)
    variant_tiles = _load_variant_tiles(
        library_dir=library_dir,
        item=item,
        source_tile_size=source_tile_size,
        target_tile_size=tile_size,
        rows=rows,
        cols=cols,
        tile_order=tile_order,
    )
    if art_style == "rpg":
        material_preset = str(item.get("material_preset") or "")
        tiles = _stylize_tile_set_for_rpg(tiles, material_preset)
        variant_tiles = {
            role: [_stylize_rpg_tile(tile, material_preset, role) for tile in variants]
            for role, variants in variant_tiles.items()
        }
    family = {
        "requirement": requirement,
        "item": item,
        "manifest": manifest,
        "art_style": art_style,
        "source_tile_size": source_tile_size,
        "tile_size": tile_size,
        "frame_grid": [rows, cols],
        "tile_order": tile_order,
        "tiles": tiles,
        "variant_tiles": variant_tiles,
        "sheet_path": sheet_path,
    }
    if requirement.get("terrain_kind") == "composite":
        return _build_composite_family(family)
    return family


def _tile_order_from_manifest(manifest: dict[str, Any]) -> dict[str, int]:
    candidate = (manifest.get("candidates") or [{}])[0]
    frames = candidate.get("frames") or []
    result = {
        str(frame.get("role")): int(frame.get("frame_index"))
        for frame in frames
        if frame.get("role") and frame.get("frame_index") is not None
    }
    return result or dict(BLOB47)


def _slice_sheet(
    sheet_path: Path,
    source_tile_size: int,
    rows: int,
    cols: int,
    tile_order: dict[str, int],
    target_tile_size: int | None = None,
) -> dict[str, Image.Image]:
    if not sheet_path.exists():
        raise FileNotFoundError(f"Terrain sheet not found: {sheet_path}")
    sheet = Image.open(sheet_path).convert("RGBA")
    target_size = int(target_tile_size or source_tile_size)
    tiles: dict[str, Image.Image] = {}
    for role, index in tile_order.items():
        col = index % cols
        row = index // cols
        if row >= rows:
            continue
        box = (
            col * source_tile_size,
            row * source_tile_size,
            (col + 1) * source_tile_size,
            (row + 1) * source_tile_size,
        )
        tiles[role] = _resize_tile(sheet.crop(box), target_size)
    return tiles


def _load_variant_tiles(
    library_dir: Path,
    item: dict[str, Any],
    source_tile_size: int,
    target_tile_size: int,
    rows: int,
    cols: int,
    tile_order: dict[str, int],
) -> dict[str, list[Image.Image]]:
    variant_path_value = (item.get("paths") or {}).get("variant_sheet")
    if not variant_path_value:
        return {}
    variant_path = library_dir / str(variant_path_value)
    if not variant_path.exists():
        return {}
    sheet = Image.open(variant_path).convert("RGBA")
    row_block_count = sheet.height // max(1, rows * source_tile_size)
    if row_block_count <= 1:
        return {}
    result: dict[str, list[Image.Image]] = {}
    for variant_row in range(1, row_block_count):
        for role, index in tile_order.items():
            col = index % cols
            row = index // cols
            box = (
                col * source_tile_size,
                (variant_row * rows + row) * source_tile_size,
                (col + 1) * source_tile_size,
                (variant_row * rows + row + 1) * source_tile_size,
            )
            if box[2] > sheet.width or box[3] > sheet.height:
                continue
            result.setdefault(role, []).append(_resize_tile(sheet.crop(box), target_tile_size))
    return result


def _build_composite_family(family: dict[str, Any]) -> dict[str, Any]:
    requirement = family["requirement"]
    composite = requirement.get("composite") or {}
    if composite.get("type") != "oval_track":
        return family

    tile_size = family["tile_size"]
    grid_rows, grid_cols = [int(value) for value in composite.get("grid", [6, 10])]
    if grid_rows < 6 or grid_cols < 8:
        raise ValueError("oval_track composite grid must be at least 6x8 tiles")

    canvas = _draw_oval_track_composite(
        family=family,
        tile_size=tile_size,
        rows=grid_rows,
        cols=grid_cols,
        lane_count=int(composite.get("lane_count") or 4),
        track_width_tiles=float(composite.get("track_width_tiles") or 1.35),
    )
    tile_order: dict[str, int] = {}
    tiles: dict[str, Image.Image] = {}
    for row in range(grid_rows):
        for col in range(grid_cols):
            index = row * grid_cols + col
            role = f"composite_r{row:02d}_c{col:02d}"
            tile_order[role] = index
            box = (col * tile_size, row * tile_size, (col + 1) * tile_size, (row + 1) * tile_size)
            tiles[role] = canvas.crop(box)

    return family | {
        "family_kind": "composite",
        "frame_grid": [grid_rows, grid_cols],
        "tile_order": tile_order,
        "tiles": tiles,
        "variant_tiles": {},
        "sheet_image": canvas,
        "sheet_path": None,
        "composite": {
            "type": "oval_track",
            "grid": [grid_rows, grid_cols],
            "lane_count": int(composite.get("lane_count") or 4),
            "track_width_tiles": float(composite.get("track_width_tiles") or 1.35),
            "view": composite.get("view") or "top_down_orthographic_ground",
        },
    }


def _draw_oval_track_composite(
    family: dict[str, Any],
    tile_size: int,
    rows: int,
    cols: int,
    lane_count: int,
    track_width_tiles: float,
) -> Image.Image:
    width = cols * tile_size
    height = rows * tile_size
    grass_tile = family["tiles"].get("neutral_base") or next(iter(family["tiles"].values()))
    image = _tile_fill(grass_tile, width, height)
    fill_tile = family["tiles"].get("blob_255") or next(iter(family["tiles"].values()))
    track_surface = _tile_fill(fill_tile, width, height)
    mask, geometry = _smooth_oval_track_mask(
        width=width,
        height=height,
        tile_size=tile_size,
        track_width_tiles=track_width_tiles,
    )
    image.paste(track_surface, (0, 0), mask)
    _draw_ground_track_marks(image, geometry, tile_size, max(2, lane_count))
    return image


def _smooth_oval_track_mask(
    width: int,
    height: int,
    tile_size: int,
    track_width_tiles: float,
) -> tuple[Image.Image, dict[str, int]]:
    scale = 4
    margin_x = int(tile_size * 0.82)
    margin_y = int(tile_size * 0.58)
    box = (margin_x, margin_y, width - margin_x, height - margin_y)
    track_width = max(int(tile_size * 0.92), int(tile_size * track_width_tiles))
    high = Image.new("L", (width * scale, height * scale), 0)
    draw = ImageDraw.Draw(high)
    scaled_box = tuple(value * scale for value in box)
    draw.rounded_rectangle(
        scaled_box,
        radius=max(1, (scaled_box[3] - scaled_box[1]) // 2),
        outline=255,
        width=track_width * scale,
    )
    mask = high.resize((width, height), Image.Resampling.LANCZOS)
    mask = mask.point(lambda value: 255 if value > 96 else 0)
    return mask, {
        "left": box[0],
        "top": box[1],
        "right": box[2],
        "bottom": box[3],
        "track_width": track_width,
        "radius": (box[3] - box[1]) // 2,
    }


def _draw_ground_track_marks(
    image: Image.Image,
    geometry: dict[str, int],
    tile_size: int,
    lane_count: int,
) -> None:
    draw = ImageDraw.Draw(image, "RGBA")
    line_color = (246, 238, 214, 132)
    dark_line = (113, 42, 35, 58)
    line_width = max(1, tile_size // 44)
    left = geometry["left"]
    right = geometry["right"]
    top = geometry["top"]
    bottom = geometry["bottom"]
    radius = geometry["radius"]
    track_width = geometry["track_width"]
    straight_left = left + radius
    straight_right = right - radius
    if straight_right <= straight_left:
        return

    def dashed_line(y: int, inset: int = 0) -> None:
        dash = max(10, tile_size // 3)
        gap = max(8, tile_size // 5)
        x = straight_left + inset
        while x < straight_right - inset:
            draw.line(
                (x, y, min(straight_right - inset, x + dash), y),
                fill=line_color,
                width=line_width,
            )
            x += dash + gap

    for lane in range(1, lane_count):
        offset = int(track_width * lane / lane_count)
        dashed_line(top + offset, inset=tile_size // 8)
        dashed_line(bottom - offset, inset=tile_size // 8)

    start_x = straight_right - tile_size // 2
    for y0, y1 in (
        (top + tile_size // 8, top + track_width - tile_size // 8),
        (bottom - track_width + tile_size // 8, bottom - tile_size // 8),
    ):
        draw.line((start_x, y0, start_x, y1), fill=(246, 238, 214, 166), width=max(2, tile_size // 32))
        draw.line((start_x + 2, y0, start_x + 2, y1), fill=dark_line, width=1)


def _tile_fill(tile: Image.Image, width: int, height: int) -> Image.Image:
    tile = tile.convert("RGBA")
    out = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    for y in range(0, height, tile.height):
        for x in range(0, width, tile.width):
            out.alpha_composite(tile, (x, y))
    return out


def _dominant_color(tile: Image.Image) -> tuple[int, int, int, int]:
    image = tile.convert("RGBA").resize((1, 1), Image.Resampling.BILINEAR)
    return image.getpixel((0, 0))


def _nudge_color(
    color: tuple[int, int, int, int],
    red: int = 0,
    green: int = 0,
    blue: int = 0,
    alpha: int | None = None,
) -> tuple[int, int, int, int]:
    return (
        max(0, min(255, color[0] + red)),
        max(0, min(255, color[1] + green)),
        max(0, min(255, color[2] + blue)),
        color[3] if alpha is None else alpha,
    )


def _stylize_tile_set_for_rpg(
    tiles: dict[str, Image.Image],
    material_preset: str,
) -> dict[str, Image.Image]:
    return {
        role: _stylize_rpg_tile(tile, material_preset, role)
        for role, tile in tiles.items()
    }


def _stylize_rpg_tile(tile: Image.Image, material_preset: str, role: str) -> Image.Image:
    tile = tile.convert("RGBA")
    alpha = tile.getchannel("A")
    rgb = tile.convert("RGB")
    rgb = rgb.filter(ImageFilter.MedianFilter(3))
    rgb = ImageEnhance.Color(rgb).enhance(1.12)
    rgb = ImageEnhance.Contrast(rgb).enhance(1.06)
    rgb = ImageOps.posterize(rgb, 5)
    rgba = rgb.convert("RGBA")
    rgba.putalpha(alpha)
    rgba = _snap_tile_to_rpg_palette(rgba, material_preset)
    rgba = _add_rpg_micro_detail(rgba, material_preset, role)
    return rgba


def _snap_tile_to_rpg_palette(tile: Image.Image, material_preset: str) -> Image.Image:
    palette = _rpg_palette_for_material(material_preset)
    rgba = tile.convert("RGBA")
    pixels = list(rgba.getdata())
    snapped = []
    for red, green, blue, alpha in pixels:
        if alpha == 0:
            snapped.append((red, green, blue, alpha))
            continue
        best = min(
            palette,
            key=lambda color: (red - color[0]) ** 2 + (green - color[1]) ** 2 + (blue - color[2]) ** 2,
        )
        mix = 0.68
        snapped.append(
            (
                int(red * (1 - mix) + best[0] * mix),
                int(green * (1 - mix) + best[1] * mix),
                int(blue * (1 - mix) + best[2] * mix),
                alpha,
            )
        )
    out = Image.new("RGBA", rgba.size)
    out.putdata(snapped)
    return out


def _rpg_palette_for_material(material_preset: str) -> list[tuple[int, int, int]]:
    palettes = {
        "campus_path": [
            (178, 172, 154),
            (198, 191, 171),
            (142, 137, 124),
            (94, 146, 70),
            (120, 174, 82),
        ],
        "campus_brick": [
            (176, 84, 48),
            (207, 105, 60),
            (135, 63, 44),
            (104, 144, 68),
            (128, 176, 84),
        ],
        "campus_track": [
            (189, 65, 48),
            (220, 83, 55),
            (148, 48, 42),
            (105, 158, 68),
            (128, 185, 82),
        ],
        "campus_sand": [
            (219, 185, 122),
            (238, 207, 145),
            (174, 137, 88),
            (104, 154, 70),
            (131, 185, 84),
        ],
    }
    return palettes.get(
        material_preset,
        [
            (92, 145, 68),
            (119, 178, 80),
            (153, 199, 94),
            (70, 113, 58),
            (201, 187, 136),
        ],
    )


def _add_rpg_micro_detail(tile: Image.Image, material_preset: str, role: str) -> Image.Image:
    if tile.width < 24 or tile.height < 24:
        return tile
    out = tile.copy()
    draw = ImageDraw.Draw(out, "RGBA")
    seed = _stable_role_hash(f"{material_preset}:{role}") & 0xFFFF
    detail_count = max(2, tile.width // 18)
    base_points = [
        (
            (seed * (index + 3) * 17 + index * 19) % tile.width,
            (seed * (index + 5) * 23 + index * 11) % tile.height,
        )
        for index in range(detail_count)
    ]
    if material_preset == "campus_brick":
        return _add_brick_rpg_detail(out, role)
    if material_preset == "campus_track":
        for x, y in base_points[: max(2, detail_count // 2)]:
            draw.point((x, y), fill=(255, 188, 151, 72))
        return out
    if material_preset == "campus_path":
        for x, y in base_points:
            color = (224, 218, 196, 72) if (x + y) % 2 else (108, 102, 91, 54)
            draw.rectangle((x, y, min(tile.width - 1, x + 1), y), fill=color)
        return out
    if material_preset == "campus_sand":
        for x, y in base_points:
            draw.point((x, y), fill=(255, 229, 169, 82))
            if x + 1 < tile.width:
                draw.point((x + 1, y), fill=(163, 124, 79, 58))
        return out

    for x, y in base_points:
        color = (56, 112, 48, 84) if (x + y) % 3 else (168, 211, 98, 78)
        draw.line((x, y, min(tile.width - 1, x + 2), y), fill=color, width=1)
    return out


def _add_brick_rpg_detail(tile: Image.Image, role: str) -> Image.Image:
    out = tile.copy()
    draw = ImageDraw.Draw(out, "RGBA")
    brick_h = max(5, tile.height // 10)
    mortar = (111, 65, 52, 96)
    light = (229, 127, 76, 44)
    offset_seed = _stable_role_hash(role) % max(1, brick_h)
    for y in range(offset_seed, tile.height, brick_h):
        draw.line((0, y, tile.width, y), fill=mortar, width=1)
        if y + 1 < tile.height:
            draw.line((0, y + 1, tile.width, y + 1), fill=light, width=1)
        brick_w = max(12, tile.width // 4)
        offset = 0 if (y // brick_h) % 2 == 0 else brick_w // 2
        for x in range(offset, tile.width, brick_w):
            draw.line((x, y, x, min(tile.height - 1, y + brick_h - 1)), fill=mortar, width=1)
    return out


def _compose_full_tileset(loaded: list[dict[str, Any]]) -> tuple[Image.Image, list[dict[str, Any]]]:
    width = max(item["frame_grid"][1] * item["tile_size"] for item in loaded)
    height = sum(item["frame_grid"][0] * item["tile_size"] for item in loaded)
    out = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    sections = []
    y = 0
    for family in loaded:
        rows, cols = family["frame_grid"]
        tile_size = family["tile_size"]
        section = family.get("sheet_image") or _compose_family_sheet(family)
        out.alpha_composite(section, (0, y))
        sections.append(
            {
                "feature_id": family["requirement"]["feature_id"],
                "terrain_id": family["item"]["id"],
                "family_kind": family.get("family_kind") or "blob",
                "material_id": family["item"].get("material_id"),
                "tile_size": tile_size,
                "frame_grid": [rows, cols],
                "start_row": y // tile_size,
                "row_count": rows,
                "tile_order": family["tile_order"],
                "composite": family.get("composite"),
            }
        )
        y += rows * tile_size
    return out, sections


def _compose_family_sheet(family: dict[str, Any]) -> Image.Image:
    rows, cols = family["frame_grid"]
    tile_size = family["tile_size"]
    out = Image.new("RGBA", (cols * tile_size, rows * tile_size), (0, 0, 0, 0))
    for role, index in family["tile_order"].items():
        tile = family["tiles"].get(role)
        if tile is None:
            continue
        col = index % cols
        row = index // cols
        if row >= rows:
            continue
        out.alpha_composite(_resize_tile(tile, tile_size), (col * tile_size, row * tile_size))
    return out


def _compose_variant_tileset(loaded: list[dict[str, Any]]) -> tuple[Image.Image | None, list[dict[str, Any]]]:
    sheets = []
    for family in loaded:
        if family.get("family_kind") == "composite":
            continue
        sheet = _compose_family_variant_sheet(family)
        if sheet:
            sheets.append((family, sheet))
    if not sheets:
        return None, []

    width = max(sheet.width for _, sheet in sheets)
    height = sum(sheet.height for _, sheet in sheets)
    out = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    sections = []
    y = 0
    for family, sheet in sheets:
        tile_size = family["tile_size"]
        out.alpha_composite(sheet, (0, y))
        sections.append(
            {
                "feature_id": family["requirement"]["feature_id"],
                "terrain_id": family["item"]["id"],
                "start_row": y // tile_size,
                "row_count": sheet.height // tile_size,
            }
        )
        y += sheet.height
    return out, sections


def _compose_family_variant_sheet(family: dict[str, Any]) -> Image.Image | None:
    if not family.get("variant_tiles"):
        return None
    rows, cols = family["frame_grid"]
    tile_size = family["tile_size"]
    max_variants = max((len(value) for value in family["variant_tiles"].values()), default=0)
    if max_variants <= 0:
        return None

    out = Image.new("RGBA", (cols * tile_size, rows * tile_size * (max_variants + 1)), (0, 0, 0, 0))
    for block_index in range(max_variants + 1):
        y_offset = block_index * rows * tile_size
        for role, index in family["tile_order"].items():
            col = index % cols
            row = index // cols
            if row >= rows:
                continue
            tile = family["tiles"].get(role)
            if block_index:
                variants = family["variant_tiles"].get(role) or []
                if block_index <= len(variants):
                    tile = variants[block_index - 1]
            if tile is None:
                continue
            out.alpha_composite(_resize_tile(tile, tile_size), (col * tile_size, y_offset + row * tile_size))
    return out


def _compose_scene_preview(
    loaded: list[dict[str, Any]],
) -> tuple[Image.Image, dict[str, set[str]], list[dict[str, Any]]]:
    tile_size = max(item["tile_size"] for item in loaded)
    shapes = []
    for family in loaded:
        if family.get("family_kind") == "composite":
            rows, cols = family["frame_grid"]
            mask = [[True for _ in range(cols)] for _ in range(rows)]
        else:
            mask = _shape_mask(family["requirement"]["shape"])
        x, y = [int(value) for value in family["requirement"].get("position", [0, 0])]
        shapes.append((family, mask, x, y))

    width_tiles = max(x + max(len(row) for row in mask) for _, mask, x, _ in shapes) + 2
    height_tiles = max(y + len(mask) for _, mask, _, y in shapes) + 2
    base_family = loaded[0]
    base_tile = _resize_tile(base_family["tiles"].get("neutral_base") or next(iter(base_family["tiles"].values())), tile_size)
    preview = Image.new("RGBA", (width_tiles * tile_size, height_tiles * tile_size), (0, 0, 0, 0))
    for row in range(height_tiles):
        for col in range(width_tiles):
            preview.alpha_composite(base_tile, (col * tile_size, row * tile_size))

    used_roles: dict[str, set[str]] = {}
    for family in loaded:
        terrain_id = family["item"]["id"]
        used_roles[terrain_id] = set()
        if "neutral_base" in family["tile_order"]:
            used_roles[terrain_id].add("neutral_base")
    layout = []
    for family, mask, offset_x, offset_y in shapes:
        terrain_id = family["item"]["id"]
        used_roles.setdefault(terrain_id, set())
        if family.get("family_kind") == "composite":
            image = family["sheet_image"]
            if image.size != (len(mask[0]) * tile_size, len(mask) * tile_size):
                image = image.resize((len(mask[0]) * tile_size, len(mask) * tile_size), Image.Resampling.NEAREST)
            preview.alpha_composite(image, (offset_x * tile_size, offset_y * tile_size))
            used_roles[terrain_id].update(family["tile_order"].keys())
            layout.append(
                {
                    "feature_id": family["requirement"]["feature_id"],
                    "terrain_id": terrain_id,
                    "family_kind": "composite",
                    "shape": family["requirement"]["shape"],
                    "position": [offset_x, offset_y],
                    "size": [len(mask[0]), len(mask)],
                    "composite": family.get("composite"),
                }
            )
            continue
        for row_index, row in enumerate(mask):
            for col_index, enabled in enumerate(row):
                if not enabled:
                    continue
                role = choose_blob47_tile(mask, row_index, col_index)
                tile = _select_tile(family, role, row_index + offset_y, col_index + offset_x, tile_size)
                preview.alpha_composite(tile, ((offset_x + col_index) * tile_size, (offset_y + row_index) * tile_size))
                used_roles[terrain_id].add(role)
        layout.append(
            {
                "feature_id": family["requirement"]["feature_id"],
                "terrain_id": terrain_id,
                "family_kind": family.get("family_kind") or "blob",
                "shape": family["requirement"]["shape"],
                "position": [offset_x, offset_y],
                "size": [max(len(row) for row in mask), len(mask)],
            }
        )
    return preview, used_roles, layout


def _compose_used_tileset(
    loaded: list[dict[str, Any]],
    used_roles: dict[str, set[str]],
) -> tuple[Image.Image, list[dict[str, Any]]]:
    tile_size = max(item["tile_size"] for item in loaded)
    cols = 8
    rows_per_family = []
    for family in loaded:
        roles = _sorted_roles(family["tile_order"], used_roles.get(family["item"]["id"], set()))
        rows_per_family.append((family, roles, max(1, math.ceil(len(roles) / cols))))
    width = cols * tile_size
    height = sum(row_count * tile_size for _, _, row_count in rows_per_family)
    out = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    index: list[dict[str, Any]] = []
    y_row = 0
    for family, roles, row_count in rows_per_family:
        terrain_id = family["item"]["id"]
        for role_index, role in enumerate(roles):
            tile = _select_tile(family, role, y_row + role_index // cols, role_index % cols, tile_size)
            x = (role_index % cols) * tile_size
            y = (y_row + role_index // cols) * tile_size
            out.alpha_composite(tile, (x, y))
            index.append(
                {
                    "terrain_id": terrain_id,
                    "feature_id": family["requirement"]["feature_id"],
                    "family_kind": family.get("family_kind") or "blob",
                    "role": role,
                    "tile": [role_index % cols, y_row + role_index // cols],
                    "source_index": family["tile_order"].get(role),
                }
            )
        y_row += row_count
    return out, index


def _sorted_roles(tile_order: dict[str, int], roles: set[str]) -> list[str]:
    valid = [role for role in roles if role in tile_order]
    return sorted(valid, key=lambda role: tile_order[role])


def _select_tile(family: dict[str, Any], role: str, row: int, col: int, target_size: int) -> Image.Image:
    variants = family["variant_tiles"].get(role) or []
    if variants:
        role_hash = _stable_role_hash(role)
        value = (row + 1) * 92821 ^ (col + 1) * 68917 ^ role_hash * 31337
        value ^= value >> 16
        index = value % (len(variants) + 1)
        if index:
            return _resize_tile(variants[index - 1], target_size)
    return _resize_tile(family["tiles"][role], target_size)


def _resize_tile(tile: Image.Image, target_size: int) -> Image.Image:
    tile = tile.convert("RGBA")
    if tile.size == (target_size, target_size):
        return tile
    if target_size < min(tile.size):
        return _resize_tile_crisp_down(tile, target_size)
    return tile.resize((target_size, target_size), Image.Resampling.NEAREST)


def _resize_tile_crisp_down(tile: Image.Image, target_size: int) -> Image.Image:
    alpha = tile.getchannel("A").resize((target_size, target_size), Image.Resampling.BOX)
    rgb = tile.convert("RGB").resize((target_size, target_size), Image.Resampling.BOX)
    rgb = rgb.filter(ImageFilter.UnsharpMask(radius=0.65, percent=160, threshold=3))
    rgb = ImageEnhance.Contrast(rgb).enhance(1.08)
    rgb = ImageEnhance.Sharpness(rgb).enhance(1.22)
    rgb = ImageOps.posterize(rgb, 6)
    out = rgb.convert("RGBA")
    out.putalpha(alpha)
    return out


def _stable_role_hash(role: str) -> int:
    value = 2166136261
    for char in role:
        value ^= ord(char)
        value = (value * 16777619) & 0xFFFFFFFF
    return value


def _shape_mask(name: str) -> list[list[bool]]:
    if name == "oval_track":
        rows = [
            "000001111100000",
            "000111111111000",
            "001111000111100",
            "011100000001110",
            "011100000001110",
            "001111000111100",
            "000111111111000",
            "000001111100000",
        ]
    elif name == "winding_path":
        rows = [
            "000000000000000",
            "111110000000000",
            "000111111000000",
            "000000111111100",
            "000000000011111",
        ]
    elif name == "plaza":
        rows = [
            "01111110",
            "11111111",
            "11111111",
            "11111110",
            "01111100",
        ]
    elif name == "sand_blob":
        rows = [
            "000111000",
            "011111110",
            "111111111",
            "111111110",
            "001111000",
        ]
    elif name in SHAPES:
        rows = SHAPES[name]
    else:
        rows = SHAPES["irregular"]
    return [[char == "1" for char in row] for row in rows]
