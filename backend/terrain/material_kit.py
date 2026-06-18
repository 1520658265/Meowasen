from __future__ import annotations

import json
import math
import random
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageDraw, ImageFilter

from backend.storage.paths import task_dir
from backend.terrain.preview import (
    AUTOTILE16,
    BLOB47,
    BLOB47_MASKS,
    BIT_E,
    BIT_N,
    BIT_NE,
    BIT_NW,
    BIT_S,
    BIT_SE,
    BIT_SW,
    BIT_W,
    SHAPES,
    choose_blob47_tile,
    choose_tile,
)


ROLE_BY_INDEX = {index: role for role, index in AUTOTILE16.items()}


@dataclass(frozen=True)
class TerrainMaterialDemoParams:
    task_id: str | None = None
    tile_size: int = 64
    animation_frames: int = 4
    seed: int = 17
    mode: str = "blob47"
    variants: int = 1
    assets_dir: Path = Path("assets")


@dataclass(frozen=True)
class TerrainMaterialBuildParams:
    kit_path: Path
    task_id: str | None = None
    tile_size: int = 128
    grid_size: int = 4
    animation_frames: int = 4
    seed: int = 17
    mode: str = "blob47"
    cell_margin_ratio: float = 0.07
    variants: int = 1
    theme: str = "volcano"
    material_preset: str = "default"
    assets_dir: Path = Path("assets")


def build_material_demo(params: TerrainMaterialDemoParams) -> dict[str, Any]:
    if params.tile_size < 16:
        raise ValueError("tile_size must be at least 16")
    if params.animation_frames < 0:
        raise ValueError("animation_frames must not be negative")
    _validate_mode(params.mode)
    _validate_variants(params.variants)

    task_id = params.task_id or str(uuid.uuid4())
    task_path = task_dir(params.assets_dir, task_id, "terrain")

    textures = _build_textures(params.tile_size, params.seed, lava_phase=0)
    _save_material_kit(task_path, textures, params.tile_size)

    tiles = _build_tiles(params.tile_size, params.seed, lava_phase=0, mode=params.mode)
    variant_tiles = _build_variant_tiles(params.tile_size, params.seed, textures, params.mode, params.variants)
    frame_layout = _frame_layout_for_mode(params.mode)
    frame_grid = _frame_grid_for_mode(params.mode)
    _save_standard_task(
        task_path=task_path,
        task_id=task_id,
        tile_size=params.tile_size,
        tiles=tiles,
        seed=params.seed,
        animation_frames=params.animation_frames,
        frame_layout=frame_layout,
        frame_grid=frame_grid,
        variant_tiles=variant_tiles,
        variants=params.variants,
    )

    preview_outputs = _save_shape_previews(
        task_path=task_path,
        tile_size=params.tile_size,
        tiles=tiles,
        variant_tiles=variant_tiles,
        prefix="terrain_preview",
    )

    animation_outputs: list[str] = []
    for frame_index in range(params.animation_frames):
        phase = frame_index / max(1, params.animation_frames)
        animated_tiles = _build_tiles(params.tile_size, params.seed, lava_phase=phase, mode=params.mode)
        atlas_name = f"animation_sheet_{frame_index}.png"
        _compose_atlas(animated_tiles, params.tile_size, frame_grid).save(task_path / atlas_name)
        animation_outputs.append(atlas_name)
        animation_outputs.extend(
            _save_shape_previews(
                task_path=task_path,
                tile_size=params.tile_size,
                tiles=animated_tiles,
                variant_tiles=variant_tiles,
                prefix=f"terrain_preview_anim_{frame_index}",
                shapes=("pond",),
            )
        )

    summary = {
        "task_id": task_id,
        "status": "done",
        "asset_type": "tile",
        "frame_layout": frame_layout,
        "frame_grid": list(frame_grid),
        "sheet_size": [params.tile_size * frame_grid[1], params.tile_size * frame_grid[0]],
        "output_size": params.tile_size,
        "palette_colors": 0,
        "material_kit": "material_kit.png",
        "previews": preview_outputs,
        "animation_outputs": animation_outputs,
        "tile_order": _tile_order_for_mode(params.mode),
        "variants": params.variants,
    }
    (task_path / "terrain_material_demo.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return _read_json(task_path / "meta.json") | {
        "material_kit": summary["material_kit"],
        "previews": preview_outputs,
        "animation_outputs": animation_outputs,
    }


def build_material_from_kit(params: TerrainMaterialBuildParams) -> dict[str, Any]:
    if not params.kit_path.exists():
        raise FileNotFoundError(f"Material kit not found: {params.kit_path}")
    if params.grid_size < 2:
        raise ValueError("grid_size must be at least 2")
    if params.tile_size < 16:
        raise ValueError("tile_size must be at least 16")
    _validate_mode(params.mode)
    _validate_variants(params.variants)

    task_id = params.task_id or str(uuid.uuid4())
    task_path = task_dir(params.assets_dir, task_id, "terrain")

    source = Image.open(params.kit_path).convert("RGBA")
    normalized, cells = _extract_kit_cells(source, params.grid_size, params.cell_margin_ratio)
    normalized.save(task_path / "material_kit_source.png")

    _validate_theme(params.theme)
    material_preset = _canonical_material_preset(params.theme, params.material_preset)
    textures = _textures_from_cells(
        cells,
        params.tile_size,
        theme=params.theme,
        material_preset=material_preset,
    )
    _save_extracted_materials(task_path, textures, params.tile_size)

    tiles = _build_tiles_from_textures(
        tile_size=params.tile_size,
        seed=params.seed,
        textures=textures,
        lava_phase=0,
        mode=params.mode,
        theme=params.theme,
    )
    variant_tiles = _build_variant_tiles(
        params.tile_size,
        params.seed,
        textures,
        params.mode,
        params.variants,
        theme=params.theme,
        material_preset=material_preset,
    )
    frame_layout = _frame_layout_for_mode(params.mode)
    frame_grid = _frame_grid_for_mode(params.mode)
    _save_standard_task(
        task_path=task_path,
        task_id=task_id,
        tile_size=params.tile_size,
        tiles=tiles,
        seed=params.seed,
        animation_frames=params.animation_frames,
        model="imagehub_material_kit_expansion",
        user_prompt=f"material kit expanded from {params.kit_path}",
        frame_layout=frame_layout,
        frame_grid=frame_grid,
        variant_tiles=variant_tiles,
        variants=params.variants,
        source={
            "kit_path": str(params.kit_path),
            "grid_size": params.grid_size,
            "mode": params.mode,
            "cell_margin_ratio": params.cell_margin_ratio,
            "variants": params.variants,
            "theme": params.theme,
            "material_preset": material_preset,
            "normalized_source": "material_kit_source.png",
            "extracted_materials": "material_kit.png",
        },
    )
    preview_outputs = _save_shape_previews(
        task_path=task_path,
        tile_size=params.tile_size,
        tiles=tiles,
        variant_tiles=variant_tiles,
        prefix="terrain_preview",
    )

    animation_outputs: list[str] = []
    for frame_index in range(params.animation_frames):
        phase = frame_index / max(1, params.animation_frames)
        animated_tiles = _build_tiles_from_textures(
            tile_size=params.tile_size,
            seed=params.seed,
            textures=textures,
            lava_phase=phase,
            mode=params.mode,
            theme=params.theme,
        )
        atlas_name = f"animation_sheet_{frame_index}.png"
        _compose_atlas(animated_tiles, params.tile_size, frame_grid).save(task_path / atlas_name)
        animation_outputs.append(atlas_name)
        animation_outputs.extend(
            _save_shape_previews(
                task_path=task_path,
                tile_size=params.tile_size,
                tiles=animated_tiles,
                variant_tiles=variant_tiles,
                prefix=f"terrain_preview_anim_{frame_index}",
                shapes=("pond",),
            )
        )

    summary = {
        "task_id": task_id,
        "status": "done",
        "source_kit": str(params.kit_path),
        "normalized_source": "material_kit_source.png",
        "material_kit": "material_kit.png",
        "previews": preview_outputs,
        "animation_outputs": animation_outputs,
        "tile_order": _tile_order_for_mode(params.mode),
        "variants": params.variants,
        "theme": params.theme,
        "material_preset": material_preset,
    }
    (task_path / "terrain_material_build.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return _read_json(task_path / "meta.json") | {
        "material_kit": summary["material_kit"],
        "source_kit": summary["source_kit"],
        "previews": preview_outputs,
        "animation_outputs": animation_outputs,
    }


def _build_autotile_set(tile_size: int, seed: int, lava_phase: float) -> dict[str, Image.Image]:
    textures = _build_textures(tile_size, seed, lava_phase=lava_phase)
    return _build_tiles_from_textures(tile_size, seed, textures, lava_phase, mode="autotile16")


def _build_tiles(
    tile_size: int,
    seed: int,
    lava_phase: float,
    mode: str,
) -> dict[str, Image.Image]:
    textures = _build_textures(tile_size, seed, lava_phase=lava_phase)
    return _build_tiles_from_textures(tile_size, seed, textures, lava_phase, mode=mode)


def _build_tiles_from_textures(
    tile_size: int,
    seed: int,
    textures: dict[str, Image.Image],
    lava_phase: float,
    mode: str,
    shape_seed: int = 0,
    theme: str = "volcano",
) -> dict[str, Image.Image]:
    _validate_mode(mode)
    _validate_theme(theme)
    prepared = dict(textures)
    if lava_phase:
        prepared["lava"] = _animate_texture_for_theme(prepared["lava"], lava_phase, seed + 404, theme)
        prepared["glow"] = _animate_texture_for_theme(prepared["glow"], lava_phase, seed + 505, theme)
    tiles: dict[str, Image.Image] = {}
    if mode == "autotile16":
        for index in range(16):
            role = ROLE_BY_INDEX[index]
            mask = _mask_for_role(role, tile_size)
            tiles[role] = _compose_tile(
                base=prepared["base"],
                lava=prepared["lava"],
                crust=prepared["crust"],
                glow=prepared["glow"],
                mask=mask,
                seed=seed + index,
                theme=theme,
            )
        return tiles

    for mask_bits in BLOB47_MASKS:
        role = f"blob_{mask_bits:03d}"
        mask_variant_seed = shape_seed + mask_bits * 17 if shape_seed else 0
        mask = _mask_for_blob47(mask_bits, tile_size, variant_seed=mask_variant_seed)
        tiles[role] = _compose_tile(
            base=prepared["base"],
            lava=prepared["lava"],
            crust=prepared["crust"],
            glow=prepared["glow"],
            mask=mask,
            seed=seed + mask_bits,
            theme=theme,
        )
    tiles["neutral_base"] = prepared["base"]
    return tiles


def _build_variant_tiles(
    tile_size: int,
    seed: int,
    textures: dict[str, Image.Image],
    mode: str,
    variants: int,
    theme: str = "volcano",
    material_preset: str = "default",
) -> dict[str, list[Image.Image]]:
    if variants <= 1:
        return {}
    result: dict[str, list[Image.Image]] = {}
    for variant_index in range(1, variants):
        variant_seed = seed + variant_index * 1009
        variant_textures = _variant_textures(
            textures,
            variant_seed,
            variant_index,
            theme,
            material_preset,
        )
        tiles = _build_tiles_from_textures(
            tile_size=tile_size,
            seed=variant_seed,
            textures=variant_textures,
            lava_phase=0,
            mode=mode,
            shape_seed=variant_seed if mode == "blob47" else 0,
            theme=theme,
        )
        for role, image in tiles.items():
            result.setdefault(role, []).append(image)
    return result


def _variant_textures(
    textures: dict[str, Image.Image],
    seed: int,
    variant_index: int,
    theme: str = "volcano",
    material_preset: str = "default",
) -> dict[str, Image.Image]:
    out: dict[str, Image.Image] = {}
    for name, image in textures.items():
        rgba = image.convert("RGBA")
        shift_scale = 0.19 if theme == "volcano" and name in {"lava", "glow"} else 0.12
        shifted = ImageChops.offset(
            rgba,
            round(math.sin(seed + variant_index) * rgba.width * shift_scale),
            round(math.cos(seed - variant_index) * rgba.height * shift_scale * 0.82),
        )
        shifted = _blend_texture_edges(shifted, rgba, edge_ratio=0.16)
        if theme == "volcano" and name in {"lava", "glow"}:
            shifted = _add_variant_heat_patches(shifted, seed + variant_index * 31)
        overlay_color = _theme_target_color(theme, name, material_preset)
        overlay = Image.new("RGBA", shifted.size, (*overlay_color, 255))
        blend_amount = 0.035 + variant_index * (0.01 if theme == "volcano" and name in {"lava", "glow"} else 0.015)
        out[name] = Image.blend(shifted, overlay, blend_amount)
    return out


def _blend_texture_edges(image: Image.Image, reference: Image.Image, edge_ratio: float) -> Image.Image:
    width, height = image.size
    edge = max(2, round(min(width, height) * edge_ratio))
    mask = Image.new("L", image.size, 0)
    pixels = mask.load()
    for y in range(height):
        for x in range(width):
            distance = min(x, y, width - 1 - x, height - 1 - y)
            if distance < edge:
                pixels[x, y] = int(255 * (1 - distance / edge))
    mask = mask.filter(ImageFilter.GaussianBlur(radius=max(1.0, edge / 3)))
    out = image.copy()
    out.paste(reference.convert("RGBA"), (0, 0), mask)
    return out


def _add_variant_heat_patches(image: Image.Image, seed: int) -> Image.Image:
    rng = random.Random(seed)
    width, height = image.size
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    for _ in range(max(3, min(width, height) // 24)):
        cx = rng.randint(width // 5, width - width // 5)
        cy = rng.randint(height // 5, height - height // 5)
        rx = rng.randint(max(3, width // 18), max(4, width // 8))
        ry = rng.randint(max(3, height // 22), max(4, height // 9))
        color = rng.choice([(255, 150, 32, 34), (255, 206, 62, 28), (140, 34, 10, 30)])
        draw.ellipse((cx - rx, cy - ry, cx + rx, cy + ry), fill=color)
    overlay = overlay.filter(ImageFilter.GaussianBlur(radius=max(1.0, min(width, height) / 48)))
    return Image.alpha_composite(image.convert("RGBA"), overlay)


def _extract_kit_cells(
    source: Image.Image,
    grid_size: int,
    cell_margin_ratio: float = 0.07,
) -> tuple[Image.Image, list[Image.Image]]:
    side = min(source.size)
    side = (side // grid_size) * grid_size
    left = max(0, (source.width - side) // 2)
    top = max(0, (source.height - side) // 2)
    normalized = source.crop((left, top, left + side, top + side))
    cell_size = side // grid_size
    margin = max(0, min(cell_size // 4, round(cell_size * cell_margin_ratio)))
    cells: list[Image.Image] = []
    for row in range(grid_size):
        for col in range(grid_size):
            cell_left = col * cell_size
            cell_top = row * cell_size
            cell_right = (col + 1) * cell_size
            cell_bottom = (row + 1) * cell_size
            cells.append(
                normalized.crop(
                    (
                        cell_left + margin,
                        cell_top + margin,
                        cell_right - margin,
                        cell_bottom - margin,
                    )
                )
            )
    return normalized, cells


def _textures_from_cells(
    cells: list[Image.Image],
    tile_size: int,
    theme: str = "volcano",
    material_preset: str = "default",
) -> dict[str, Image.Image]:
    if len(cells) < 4:
        raise ValueError("Material kit must contain at least 4 cells")
    preset = _canonical_material_preset(theme, material_preset)
    cell_groups = _material_cell_groups(theme, preset)
    base = _blend_cell_group(cells, tile_size, "base", theme, preset, cell_groups["base"])
    lava = _blend_cell_group(cells, tile_size, "lava", theme, preset, cell_groups["lava"])
    crust = _blend_cell_group(cells, tile_size, "crust", theme, preset, cell_groups["crust"])
    glow = _blend_cell_group(cells, tile_size, "glow", theme, preset, cell_groups["glow"])
    return {
        "base": _normalize_material(base.convert("RGBA"), "base", theme=theme, material_preset=preset),
        "lava": _normalize_material(lava.convert("RGBA"), "lava", theme=theme, material_preset=preset),
        "crust": _normalize_material(crust.convert("RGBA"), "crust", theme=theme, material_preset=preset),
        "glow": _normalize_material(glow.convert("RGBA"), "glow", theme=theme, material_preset=preset),
    }


def _blend_cell_group(
    cells: list[Image.Image],
    tile_size: int,
    family: str,
    theme: str,
    material_preset: str,
    indexes: tuple[int, ...],
) -> Image.Image:
    available = [index for index in indexes if index < len(cells)]
    if not available:
        raise ValueError(f"Material kit does not contain cells for {material_preset}:{family}")
    texture = _prepare_texture(
        cells[available[0]],
        tile_size,
        family=family,
        theme=theme,
        material_preset=material_preset,
    )
    for offset, index in enumerate(available[1:], start=1):
        sample = _prepare_texture(
            cells[index],
            tile_size,
            family=family,
            theme=theme,
            material_preset=material_preset,
        )
        texture = Image.blend(texture, sample, min(0.28, 0.16 + offset * 0.04))
    return texture


def _material_cell_groups(theme: str, material_preset: str) -> dict[str, tuple[int, ...]]:
    if theme != "campus":
        return {
            "base": (0, 4),
            "lava": (1, 5),
            "crust": (2, 6),
            "glow": (3, 7),
        }
    presets = {
        "campus_path": {
            "base": (0, 4, 15),
            "lava": (1, 5),
            "crust": (2, 6, 13),
            "glow": (3, 7),
        },
        "campus_brick": {
            "base": (0, 4, 15),
            "lava": (8,),
            "crust": (2, 6, 13),
            "glow": (3, 1),
        },
        "campus_track": {
            "base": (0, 4, 15),
            "lava": (10,),
            "crust": (9, 11, 6),
            "glow": (3, 7),
        },
        "campus_sand": {
            "base": (0, 4, 15),
            "lava": (11, 9),
            "crust": (2, 6, 13),
            "glow": (7, 3),
        },
        "campus_dirt": {
            "base": (0, 4, 15),
            "lava": (9, 6, 13),
            "crust": (2, 13),
            "glow": (11, 7),
        },
    }
    return presets[material_preset]


def _prepare_texture(
    image: Image.Image,
    tile_size: int,
    family: str,
    theme: str = "volcano",
    material_preset: str = "default",
) -> Image.Image:
    rgba = image.convert("RGBA")
    if rgba.size != (tile_size, tile_size):
        rgba = rgba.resize((tile_size, tile_size), Image.Resampling.LANCZOS)
    rgba = _tileable_texture(rgba)
    rgba = _normalize_material(rgba, family, theme=theme, material_preset=material_preset)
    return rgba.filter(ImageFilter.UnsharpMask(radius=1.1, percent=140, threshold=3))


def _tileable_texture(image: Image.Image) -> Image.Image:
    rgba = image.convert("RGBA")
    shifted = ImageChops.offset(rgba, rgba.width // 2, rgba.height // 2)
    blend = Image.blend(rgba, shifted, 0.18)
    edge = max(4, min(rgba.size) // 12)
    mask = Image.new("L", rgba.size, 0)
    draw = ImageDraw.Draw(mask)
    draw.rectangle((0, 0, rgba.width, edge), fill=180)
    draw.rectangle((0, rgba.height - edge, rgba.width, rgba.height), fill=180)
    draw.rectangle((0, 0, edge, rgba.height), fill=180)
    draw.rectangle((rgba.width - edge, 0, rgba.width, rgba.height), fill=180)
    mask = mask.filter(ImageFilter.GaussianBlur(radius=edge / 2))
    out = rgba.copy()
    out.paste(blend, (0, 0), mask)
    return out


def _normalize_material(
    image: Image.Image,
    family: str,
    theme: str = "volcano",
    material_preset: str = "default",
) -> Image.Image:
    rgba = image.convert("RGBA")
    target = _theme_target_color(theme, family, material_preset)
    strength = _theme_normalize_strength(theme, family)
    overlay = Image.new("RGBA", rgba.size, (*target, 255))
    mixed = Image.blend(rgba, overlay, strength)
    return mixed.filter(ImageFilter.UnsharpMask(radius=0.8, percent=110, threshold=2))


def _theme_target_color(
    theme: str,
    family: str,
    material_preset: str = "default",
) -> tuple[int, int, int]:
    material_preset = _canonical_material_preset(theme, material_preset)
    palettes = {
        "volcano": {
            "base": (40, 40, 45),
            "lava": (205, 73, 15),
            "crust": (54, 43, 36),
            "glow": (245, 126, 24),
        },
        "campus": {
            "base": (68, 132, 63),
            "lava": (159, 139, 112),
            "crust": (102, 93, 63),
            "glow": (202, 190, 157),
        },
    }
    campus_overlay = {
        "campus_path": {
            "lava": (159, 139, 112),
            "crust": (104, 92, 66),
            "glow": (202, 190, 157),
        },
        "campus_brick": {
            "lava": (165, 85, 58),
            "crust": (106, 83, 59),
            "glow": (190, 166, 134),
        },
        "campus_track": {
            "lava": (172, 66, 57),
            "crust": (139, 103, 71),
            "glow": (215, 191, 157),
        },
        "campus_sand": {
            "lava": (201, 174, 114),
            "crust": (128, 103, 68),
            "glow": (224, 206, 156),
        },
        "campus_dirt": {
            "lava": (128, 93, 60),
            "crust": (102, 82, 59),
            "glow": (203, 174, 114),
        },
    }
    if theme == "campus" and family in campus_overlay.get(material_preset, {}):
        return campus_overlay[material_preset][family]
    return palettes[theme][family]


def _theme_normalize_strength(theme: str, family: str) -> float:
    strengths = {
        "volcano": {
            "base": 0.28,
            "lava": 0.22,
            "crust": 0.30,
            "glow": 0.24,
        },
        "campus": {
            "base": 0.20,
            "lava": 0.18,
            "crust": 0.18,
            "glow": 0.14,
        },
    }
    return strengths[theme][family]


def _save_extracted_materials(task_path: Path, textures: dict[str, Image.Image], tile_size: int) -> None:
    samples = [
        textures["base"],
        textures["lava"],
        textures["crust"],
        textures["glow"],
        _mask_preview(_mask_for_role("edge_top", tile_size)),
        _mask_preview(_mask_for_role("edge_left", tile_size)),
        _mask_preview(_mask_for_role("outer_tl", tile_size)),
        _mask_preview(_mask_for_role("inner_tl", tile_size)),
        _mask_preview(_mask_for_role("island", tile_size)),
        _mask_preview(_mask_for_role("horizontal", tile_size)),
        _mask_preview(_mask_for_role("vertical", tile_size)),
        textures["base"],
        textures["lava"],
        textures["glow"],
        textures["crust"],
        textures["base"],
    ]
    atlas = Image.new("RGBA", (tile_size * 4, tile_size * 4), (0, 0, 0, 0))
    for index, sample in enumerate(samples):
        atlas.alpha_composite(sample.convert("RGBA"), ((index % 4) * tile_size, (index // 4) * tile_size))
    atlas.save(task_path / "material_kit.png")


def _animate_texture(image: Image.Image, phase: float, seed: int) -> Image.Image:
    rng = random.Random(seed)
    offset_x = round(math.sin(phase * math.tau + rng.random()) * image.width * 0.045)
    offset_y = round(math.cos(phase * math.tau + rng.random()) * image.height * 0.035)
    shifted = ImageChops.offset(image.convert("RGBA"), offset_x, offset_y)
    pulse = 0.92 + 0.12 * math.sin(phase * math.tau)
    overlay = Image.new("RGBA", shifted.size, (255, 112, 28, 255))
    return Image.blend(shifted, overlay, max(0.0, min(0.16, pulse - 0.88)))


def _animate_texture_for_theme(image: Image.Image, phase: float, seed: int, theme: str) -> Image.Image:
    if theme == "volcano":
        return _animate_texture(image, phase, seed)
    rng = random.Random(seed)
    offset_x = round(math.sin(phase * math.tau + rng.random()) * image.width * 0.012)
    offset_y = round(math.cos(phase * math.tau + rng.random()) * image.height * 0.010)
    shifted = ImageChops.offset(image.convert("RGBA"), offset_x, offset_y)
    pulse = 0.04 * math.sin(phase * math.tau)
    overlay = Image.new("RGBA", shifted.size, (226, 221, 190, 255))
    return Image.blend(shifted, overlay, max(0.0, pulse))


def _build_textures(tile_size: int, seed: int, lava_phase: float) -> dict[str, Image.Image]:
    return {
        "base": _base_texture(tile_size, seed),
        "lava": _lava_texture(tile_size, seed + 101, lava_phase),
        "crust": _crust_texture(tile_size, seed + 202),
        "glow": _glow_texture(tile_size, seed + 303, lava_phase),
    }


def _base_texture(size: int, seed: int) -> Image.Image:
    rng = random.Random(seed)
    phases = [rng.random() * math.tau for _ in range(6)]
    image = Image.new("RGBA", (size, size))
    pixels = []
    for y in range(size):
        for x in range(size):
            nx = x / size
            ny = y / size
            value = (
                math.sin(math.tau * (2 * nx + phases[0]))
                + math.sin(math.tau * (3 * ny + phases[1]))
                + math.sin(math.tau * (2 * nx + 2 * ny + phases[2]))
                + math.sin(math.tau * (5 * nx - 3 * ny + phases[3])) * 0.45
            )
            crack = abs(math.sin(math.tau * (4 * nx + 3 * ny + phases[4])))
            shade = int(34 + value * 5)
            if crack < 0.055:
                shade -= 13
            if abs(math.sin(math.tau * (7 * nx - 5 * ny + phases[5]))) < 0.035:
                shade += 8
            shade = _clamp(shade, 18, 58)
            pixels.append((shade, shade + 2, shade + 4, 255))
    image.putdata(pixels)
    return image


def _lava_texture(size: int, seed: int, phase: float) -> Image.Image:
    rng = random.Random(seed)
    phases = [rng.random() * math.tau + phase * math.tau for _ in range(5)]
    image = Image.new("RGBA", (size, size))
    pixels = []
    for y in range(size):
        for x in range(size):
            nx = x / size
            ny = y / size
            flow = (
                math.sin(math.tau * (2 * nx + 0.75 * ny) + phases[0])
                + math.sin(math.tau * (-1.5 * nx + 2.5 * ny) + phases[1])
                + math.sin(math.tau * (4 * nx - 2 * ny) + phases[2]) * 0.55
            )
            vein = abs(math.sin(math.tau * (6 * nx + 2 * ny) + phases[3]))
            hot = max(0.0, 1.0 - vein * 10.0)
            r = int(170 + flow * 18 + hot * 70)
            g = int(48 + flow * 9 + hot * 120)
            b = int(10 + hot * 22)
            if abs(math.sin(math.tau * (3 * nx - 5 * ny) + phases[4])) < 0.06:
                r, g, b = 255, 201, 52
            pixels.append((_clamp(r, 120, 255), _clamp(g, 28, 224), _clamp(b, 0, 64), 255))
    image.putdata(pixels)
    return image


def _crust_texture(size: int, seed: int) -> Image.Image:
    base = _base_texture(size, seed)
    overlay = Image.new("RGBA", (size, size), (14, 12, 12, 255))
    return Image.blend(base, overlay, 0.42)


def _glow_texture(size: int, seed: int, phase: float) -> Image.Image:
    lava = _lava_texture(size, seed, phase)
    glow = Image.new("RGBA", (size, size), (255, 110, 24, 255))
    return Image.blend(lava, glow, 0.38)


def _mask_for_role(role: str, size: int) -> Image.Image:
    if role == "center":
        return Image.new("L", (size, size), 255)
    if role == "edge_top":
        return _edge_mask(size, "top")
    if role == "edge_bottom":
        return _edge_mask(size, "bottom")
    if role == "edge_left":
        return _edge_mask(size, "left")
    if role == "edge_right":
        return _edge_mask(size, "right")
    if role == "outer_tl":
        return _outer_corner_mask(size, "tl")
    if role == "outer_tr":
        return _outer_corner_mask(size, "tr")
    if role == "outer_bl":
        return _outer_corner_mask(size, "bl")
    if role == "outer_br":
        return _outer_corner_mask(size, "br")
    if role == "inner_tl":
        return _inner_corner_mask(size, "tl")
    if role == "inner_tr":
        return _inner_corner_mask(size, "tr")
    if role == "inner_bl":
        return _inner_corner_mask(size, "bl")
    if role == "inner_br":
        return _inner_corner_mask(size, "br")
    if role == "island":
        return _island_mask(size)
    if role == "horizontal":
        return _strip_mask(size, "horizontal")
    if role == "vertical":
        return _strip_mask(size, "vertical")
    raise ValueError(f"Unsupported role: {role}")


def _mask_for_blob47(bits: int, size: int, variant_seed: int = 0) -> Image.Image:
    if variant_seed == 0:
        return _cached_blob47_mask(bits, size).copy()
    return _build_blob47_mask(bits, size, variant_seed)


@lru_cache(maxsize=256)
def _cached_blob47_mask(bits: int, size: int) -> Image.Image:
    return _build_blob47_mask(bits, size, 0)


def _build_blob47_mask(bits: int, size: int, variant_seed: int) -> Image.Image:
    scale = 3
    high_size = size * scale
    mask = Image.new("L", (high_size, high_size), 0)
    draw = ImageDraw.Draw(mask)
    q = high_size // 4
    center = (q, q, high_size - q, high_size - q)
    radius = max(3, high_size // 9)
    draw.rounded_rectangle(center, radius=radius, fill=255)
    if bits & BIT_N:
        draw.rectangle((q, 0, high_size - q, high_size // 2), fill=255)
    if bits & BIT_S:
        draw.rectangle((q, high_size // 2, high_size - q, high_size), fill=255)
    if bits & BIT_W:
        draw.rectangle((0, q, high_size // 2, high_size - q), fill=255)
    if bits & BIT_E:
        draw.rectangle((high_size // 2, q, high_size, high_size - q), fill=255)

    corner_radius = high_size // 3
    if bits & BIT_N and bits & BIT_W:
        box = (-corner_radius // 2, -corner_radius // 2, corner_radius, corner_radius)
        if bits & BIT_NW:
            draw.pieslice(box, 0, 360, fill=255)
    if bits & BIT_N and bits & BIT_E:
        box = (high_size - corner_radius, -corner_radius // 2, high_size + corner_radius // 2, corner_radius)
        if bits & BIT_NE:
            draw.pieslice(box, 0, 360, fill=255)
    if bits & BIT_S and bits & BIT_W:
        box = (-corner_radius // 2, high_size - corner_radius, corner_radius, high_size + corner_radius // 2)
        if bits & BIT_SW:
            draw.pieslice(box, 0, 360, fill=255)
    if bits & BIT_S and bits & BIT_E:
        box = (high_size - corner_radius, high_size - corner_radius, high_size + corner_radius // 2, high_size + corner_radius // 2)
        if bits & BIT_SE:
            draw.pieslice(box, 0, 360, fill=255)

    amplitude = max(1, high_size // (36 if variant_seed else 48))
    mask = _distort_mask(mask, amplitude=amplitude, seed=variant_seed)
    if variant_seed:
        _lock_blob47_connectors(mask, bits, high_size)
    mask = mask.filter(ImageFilter.GaussianBlur(radius=max(0.4, high_size / 240)))
    return mask.resize((size, size), Image.Resampling.LANCZOS)


def _lock_blob47_connectors(mask: Image.Image, bits: int, high_size: int) -> None:
    draw = ImageDraw.Draw(mask)
    q = high_size // 4
    lock_depth = max(4, high_size // 18)
    if bits & BIT_N:
        draw.rectangle((q, 0, high_size - q, lock_depth), fill=255)
    if bits & BIT_S:
        draw.rectangle((q, high_size - lock_depth, high_size - q, high_size), fill=255)
    if bits & BIT_W:
        draw.rectangle((0, q, lock_depth, high_size - q), fill=255)
    if bits & BIT_E:
        draw.rectangle((high_size - lock_depth, q, high_size, high_size - q), fill=255)


def _distort_mask(mask: Image.Image, amplitude: int, seed: int = 0) -> Image.Image:
    if amplitude <= 0:
        return mask
    width, height = mask.size
    src = mask.load()
    out = Image.new("L", mask.size, 0)
    dst = out.load()
    phase_a = (seed % 997) * 0.017
    phase_b = (seed % 577) * 0.023
    for y in range(height):
        for x in range(width):
            dx = round(math.sin(y * 0.075 + phase_a) * amplitude + math.sin((x + y) * 0.031 + phase_b) * amplitude * 0.45)
            dy = round(math.sin(x * 0.068 + phase_b) * amplitude + math.cos((x - y) * 0.029 + phase_a) * amplitude * 0.45)
            sx = max(0, min(width - 1, x + dx))
            sy = max(0, min(height - 1, y + dy))
            dst[x, y] = src[sx, sy]
    return out


def _edge_mask(size: int, side: str) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    pixels = mask.load()
    edge = int(size * 0.28)
    amp = max(2, size // 18)
    for y in range(size):
        for x in range(size):
            wave_x = int(math.sin(math.tau * x / size) * amp + math.sin(math.tau * 3 * x / size) * amp * 0.45)
            wave_y = int(math.sin(math.tau * y / size) * amp + math.sin(math.tau * 3 * y / size) * amp * 0.45)
            if side == "top":
                inside = y >= edge + wave_x
            elif side == "bottom":
                inside = y <= size - edge + wave_x
            elif side == "left":
                inside = x >= edge + wave_y
            else:
                inside = x <= size - edge + wave_y
            pixels[x, y] = 255 if inside else 0
    return mask.filter(ImageFilter.GaussianBlur(radius=max(0.5, size / 96)))


def _outer_corner_mask(size: int, corner: str) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    pixels = mask.load()
    cx = size * (0.32 if "l" in corner else 0.68)
    cy = size * (0.32 if "t" in corner else 0.68)
    radius = size * 0.56
    for y in range(size):
        for x in range(size):
            dx = x - cx
            dy = y - cy
            dist = math.sqrt(dx * dx + dy * dy)
            wave = math.sin(math.atan2(dy, dx) * 5.0) * size * 0.035
            inside = dist >= radius + wave
            if corner == "tl":
                inside = x > size * 0.18 and y > size * 0.18 and inside
            elif corner == "tr":
                inside = x < size * 0.82 and y > size * 0.18 and inside
            elif corner == "bl":
                inside = x > size * 0.18 and y < size * 0.82 and inside
            else:
                inside = x < size * 0.82 and y < size * 0.82 and inside
            pixels[x, y] = 255 if inside else 0
    return mask.filter(ImageFilter.GaussianBlur(radius=max(0.5, size / 96)))


def _inner_corner_mask(size: int, corner: str) -> Image.Image:
    mask = Image.new("L", (size, size), 255)
    cutout = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(cutout)
    radius = int(size * 0.62)
    if corner == "tl":
        box = (-radius // 2, -radius // 2, radius, radius)
    elif corner == "tr":
        box = (size - radius, -radius // 2, size + radius // 2, radius)
    elif corner == "bl":
        box = (-radius // 2, size - radius, radius, size + radius // 2)
    else:
        box = (size - radius, size - radius, size + radius // 2, size + radius // 2)
    draw.ellipse(box, fill=255)
    cutout = cutout.filter(ImageFilter.GaussianBlur(radius=max(0.5, size / 96)))
    return ImageChops.subtract(mask, cutout)


def _island_mask(size: int) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(mask)
    margin = int(size * 0.23)
    draw.ellipse((margin, margin, size - margin, size - margin), fill=255)
    return mask.filter(ImageFilter.GaussianBlur(radius=max(0.75, size / 64)))


def _strip_mask(size: int, orientation: str) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(mask)
    margin = int(size * 0.28)
    if orientation == "horizontal":
        draw.rounded_rectangle((0, margin, size, size - margin), radius=max(2, size // 8), fill=255)
    else:
        draw.rounded_rectangle((margin, 0, size - margin, size), radius=max(2, size // 8), fill=255)
    return mask.filter(ImageFilter.GaussianBlur(radius=max(0.5, size / 96)))


def _compose_tile(
    base: Image.Image,
    lava: Image.Image,
    crust: Image.Image,
    glow: Image.Image,
    mask: Image.Image,
    seed: int = 0,
    theme: str = "volcano",
) -> Image.Image:
    if theme == "campus":
        return _compose_campus_tile(
            base=base,
            path=lava,
            dirt=crust,
            highlight=glow,
            mask=mask,
            seed=seed,
        )
    out = base.copy()
    lava = _vary_lava_interior(lava.convert("RGBA"), mask, seed)
    out.paste(lava, (0, 0), mask)

    ring_size = max(3, (min(mask.size) // 24) | 1)
    inner_size = max(3, (min(mask.size) // 32) | 1)
    dilated = mask.filter(ImageFilter.MaxFilter(ring_size))
    eroded = mask.filter(ImageFilter.MinFilter(ring_size))
    crust_ring = ImageChops.subtract(dilated, eroded)
    glow_eroded = mask.filter(ImageFilter.MinFilter(inner_size))
    glow_ring = ImageChops.subtract(mask, glow_eroded)
    crust_ring = _breakup_alpha(crust_ring, seed + 1, keep=0.58)
    crust_ring = crust_ring.point(lambda value: min(150, int(value * 0.62)))
    glow_ring = _breakup_alpha(glow_ring, seed + 2, keep=0.86)
    out.paste(crust, (0, 0), crust_ring)
    out.paste(glow, (0, 0), glow_ring.point(lambda value: min(190, value * 2)))
    out = _add_edge_sparks(out, mask, seed + 3)
    return out.convert("RGBA")


def _compose_campus_tile(
    base: Image.Image,
    path: Image.Image,
    dirt: Image.Image,
    highlight: Image.Image,
    mask: Image.Image,
    seed: int = 0,
) -> Image.Image:
    out = base.copy()
    path = _vary_campus_path_interior(path.convert("RGBA"), mask, seed)
    out.paste(path, (0, 0), mask)

    ring_size = max(3, (min(mask.size) // 20) | 1)
    inner_size = max(3, (min(mask.size) // 28) | 1)
    dilated = mask.filter(ImageFilter.MaxFilter(ring_size))
    eroded = mask.filter(ImageFilter.MinFilter(ring_size))
    soft_edge = ImageChops.subtract(dilated, eroded)
    inner_edge = ImageChops.subtract(mask, mask.filter(ImageFilter.MinFilter(inner_size)))
    soft_edge = _breakup_alpha(soft_edge, seed + 1, keep=0.72)
    soft_edge = soft_edge.point(lambda value: min(118, int(value * 0.48)))
    inner_edge = _breakup_alpha(inner_edge, seed + 2, keep=0.64)
    out.paste(dirt, (0, 0), soft_edge)
    out.paste(highlight, (0, 0), inner_edge.point(lambda value: min(90, int(value * 0.42))))
    out = _add_campus_edge_detail(out, mask, seed + 3)
    return out.convert("RGBA")


def _vary_campus_path_interior(path: Image.Image, mask: Image.Image, seed: int) -> Image.Image:
    rng = random.Random(seed)
    width, height = path.size
    interior = mask.convert("L").filter(ImageFilter.MinFilter(max(3, (min(width, height) // 8) | 1)))
    if interior.getbbox() is None:
        return path
    overlay = Image.new("RGBA", path.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    for _ in range(max(2, min(width, height) // 32)):
        cx = rng.randint(width // 7, width - width // 7)
        cy = rng.randint(height // 7, height - height // 7)
        rx = rng.randint(max(4, width // 20), max(5, width // 9))
        ry = rng.randint(max(4, height // 24), max(5, height // 10))
        color = rng.choice([(226, 214, 181, 18), (114, 102, 83, 16), (84, 132, 70, 12)])
        draw.ellipse((cx - rx, cy - ry, cx + rx, cy + ry), fill=color)
    overlay_alpha = ImageChops.multiply(overlay.getchannel("A"), interior.point(lambda value: int(value * 0.65)))
    overlay.putalpha(overlay_alpha)
    overlay = overlay.filter(ImageFilter.GaussianBlur(radius=max(1.0, min(width, height) / 52)))
    return Image.alpha_composite(path, overlay)


def _add_campus_edge_detail(image: Image.Image, mask: Image.Image, seed: int) -> Image.Image:
    rng = random.Random(seed)
    out = image.copy()
    draw = ImageDraw.Draw(out)
    edge = ImageChops.subtract(mask.filter(ImageFilter.MaxFilter(5)), mask.filter(ImageFilter.MinFilter(5)))
    width, height = out.size
    candidates = [
        (x, y)
        for y in range(0, height, max(2, height // 30))
        for x in range(0, width, max(2, width // 30))
        if edge.getpixel((x, y)) > 36
    ]
    rng.shuffle(candidates)
    for x, y in candidates[: max(3, min(width, height) // 10)]:
        color = rng.choice([(77, 129, 61, 130), (132, 112, 76, 112), (220, 207, 164, 88)])
        draw.point((x, y), fill=color)
        if rng.random() < 0.22 and x + 1 < width:
            draw.point((x + 1, y), fill=color)
    return out


def _vary_lava_interior(lava: Image.Image, mask: Image.Image, seed: int) -> Image.Image:
    rng = random.Random(seed)
    width, height = lava.size
    interior = mask.convert("L").filter(ImageFilter.MinFilter(max(3, (min(width, height) // 9) | 1)))
    if interior.getbbox() is None:
        return lava
    overlay = Image.new("RGBA", lava.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    count = max(2, min(width, height) // 28)
    for _ in range(count):
        cx = rng.randint(width // 6, width - width // 6)
        cy = rng.randint(height // 6, height - height // 6)
        rx = rng.randint(max(4, width // 18), max(5, width // 7))
        ry = rng.randint(max(4, height // 18), max(5, height // 7))
        color = rng.choice([(255, 176, 38, 22), (255, 92, 16, 20), (122, 28, 8, 24)])
        draw.ellipse((cx - rx, cy - ry, cx + rx, cy + ry), fill=color)
    overlay_alpha = ImageChops.multiply(overlay.getchannel("A"), interior.point(lambda value: int(value * 0.7)))
    overlay.putalpha(overlay_alpha)
    overlay = overlay.filter(ImageFilter.GaussianBlur(radius=max(1.0, min(width, height) / 52)))
    return Image.alpha_composite(lava, overlay)


def _breakup_alpha(alpha: Image.Image, seed: int, keep: float) -> Image.Image:
    rng = random.Random(seed)
    data = []
    for index, value in enumerate(alpha.convert("L").getdata()):
        if value <= 0:
            data.append(0)
            continue
        jitter = rng.random()
        factor = 1.0 if jitter < keep else 0.35
        data.append(int(value * factor))
    out = Image.new("L", alpha.size, 0)
    out.putdata(data)
    return out.filter(ImageFilter.GaussianBlur(radius=0.35))


def _add_edge_sparks(image: Image.Image, mask: Image.Image, seed: int) -> Image.Image:
    rng = random.Random(seed)
    out = image.copy()
    draw = ImageDraw.Draw(out)
    edge = ImageChops.subtract(mask.filter(ImageFilter.MaxFilter(5)), mask.filter(ImageFilter.MinFilter(5)))
    width, height = out.size
    candidates = [
        (x, y)
        for y in range(0, height, max(2, height // 32))
        for x in range(0, width, max(2, width // 32))
        if edge.getpixel((x, y)) > 32
    ]
    rng.shuffle(candidates)
    for x, y in candidates[: max(2, min(width, height) // 12)]:
        color = rng.choice([(255, 150, 35, 160), (255, 208, 70, 145), (110, 31, 18, 150)])
        draw.point((x, y), fill=color)
        if rng.random() < 0.35 and x + 1 < width:
            draw.point((x + 1, y), fill=color)
    return out


def _save_material_kit(task_path: Path, textures: dict[str, Image.Image], tile_size: int) -> None:
    samples = [
        textures["base"],
        textures["lava"],
        textures["crust"],
        textures["glow"],
        _mask_preview(_mask_for_role("edge_top", tile_size)),
        _mask_preview(_mask_for_role("edge_left", tile_size)),
        _mask_preview(_mask_for_role("outer_tl", tile_size)),
        _mask_preview(_mask_for_role("inner_tl", tile_size)),
        _mask_preview(_mask_for_role("island", tile_size)),
        _mask_preview(_mask_for_role("horizontal", tile_size)),
        _mask_preview(_mask_for_role("vertical", tile_size)),
        _base_texture(tile_size, 909),
        _lava_texture(tile_size, 808, 0.25),
        _lava_texture(tile_size, 808, 0.5),
        _lava_texture(tile_size, 808, 0.75),
        textures["base"],
    ]
    atlas = Image.new("RGBA", (tile_size * 4, tile_size * 4), (0, 0, 0, 0))
    for index, sample in enumerate(samples):
        atlas.alpha_composite(sample.convert("RGBA"), ((index % 4) * tile_size, (index // 4) * tile_size))
    atlas.save(task_path / "material_kit.png")


def _mask_preview(mask: Image.Image) -> Image.Image:
    black = Image.new("RGBA", mask.size, (28, 27, 29, 255))
    orange = Image.new("RGBA", mask.size, (221, 78, 20, 255))
    black.paste(orange, (0, 0), mask)
    return black


def _save_standard_task(
    task_path: Path,
    task_id: str,
    tile_size: int,
    tiles: dict[str, Image.Image],
    seed: int,
    animation_frames: int,
    model: str = "procedural_material_kit_demo",
    user_prompt: str = "procedural volcanic material kit autotile demo",
    source: dict[str, Any] | None = None,
    frame_layout: str = "terrain_autotile16",
    frame_grid: tuple[int, int] = (4, 4),
    variant_tiles: dict[str, list[Image.Image]] | None = None,
    variants: int = 1,
) -> None:
    atlas = _compose_atlas(tiles, tile_size, frame_grid)
    atlas.save(task_path / "sheet_0.png")
    if variant_tiles:
        _compose_variant_atlas(
            tiles=tiles,
            variant_tiles=variant_tiles,
            tile_size=tile_size,
            frame_grid=frame_grid,
        ).save(task_path / "sheet_variants_0.png")

    frames = []
    tile_order = _tile_order_for_layout(frame_layout)
    for role, index in tile_order.items():
        image = tiles[role]
        raw_name = f"frame_0_{index}.png"
        processed_name = f"processed_0_{index}.png"
        image.save(task_path / raw_name)
        image.save(task_path / processed_name)
        frames.append(
            {
                "frame_index": index,
                "grid_pos": [index // 4, index % 4],
                "raw": raw_name,
                "processed": processed_name,
                "role": role,
                "bg_removed": False,
                "split_quality": {"status": "ok", "flags": []},
                "status": "done",
                "error": None,
            }
        )

    now = datetime.now(UTC).isoformat()
    manifest = {
        "task_id": task_id,
        "created_at": now,
        "asset_type": "tile",
        "frame_layout": frame_layout,
        "frame_grid": list(frame_grid),
        "cell_size": tile_size,
        "sheet_size": [tile_size * frame_grid[1], tile_size * frame_grid[0]],
        "style_lock_source_task": None,
        "style_lock_source_candidate_index": None,
        "style_lock_applied": False,
        "reference_applied": False,
        "user_prompt": user_prompt,
        "enhanced_prompt": "procedural local validation for material kit to autotile expansion",
        "negative_prompt": "",
        "model": model,
        "output_size": tile_size,
        "palette_colors": 0,
        "requested_count": 1,
        "status": "done",
        "error": None,
        "material_kit_mode": {
            "source": "procedural_demo",
            "seed": seed,
            "animation_frames": animation_frames,
            "variants": variants,
            "rule": "base/lava/crust/glow material samples expanded through fixed autotile masks",
            "input": source,
        },
        "candidates": [
            {
                "index": 0,
                "sheet": "sheet_0.png",
                "style_lock_palette_b64": None,
                "style_lock_histogram_json": None,
                "provider_metadata": {"backend": "procedural_material_kit_demo"},
                "variant_sheet": "sheet_variants_0.png" if variant_tiles else None,
                "frames": frames,
                "favorited": False,
                "status": "done",
                "error": None,
            }
        ],
    }
    (task_path / "meta.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


def _compose_atlas(
    tiles: dict[str, Image.Image],
    tile_size: int,
    frame_grid: tuple[int, int] = (4, 4),
) -> Image.Image:
    rows, cols = frame_grid
    atlas = Image.new("RGBA", (tile_size * cols, tile_size * rows), (0, 0, 0, 0))
    tile_order = _tile_order_for_grid(frame_grid)
    for role, index in tile_order.items():
        atlas.alpha_composite(tiles[role], ((index % cols) * tile_size, (index // cols) * tile_size))
    return atlas


def _compose_variant_atlas(
    tiles: dict[str, Image.Image],
    variant_tiles: dict[str, list[Image.Image]],
    tile_size: int,
    frame_grid: tuple[int, int],
) -> Image.Image:
    tile_order = _tile_order_for_grid(frame_grid)
    max_variants = max((len(items) for items in variant_tiles.values()), default=0)
    rows, cols = frame_grid
    atlas = Image.new("RGBA", (tile_size * cols, tile_size * rows * (max_variants + 1)), (0, 0, 0, 0))
    for variant_row in range(max_variants + 1):
        for role, index in tile_order.items():
            role_variants = variant_tiles.get(role) or []
            if variant_row == 0 or not role_variants:
                image = tiles[role]
            else:
                image = role_variants[min(variant_row - 1, len(role_variants) - 1)]
            x = (index % cols) * tile_size
            y = (variant_row * rows + index // cols) * tile_size
            atlas.alpha_composite(image, (x, y))
    return atlas


def _save_shape_previews(
    task_path: Path,
    tile_size: int,
    tiles: dict[str, Image.Image],
    variant_tiles: dict[str, list[Image.Image]] | None,
    prefix: str,
    shapes: tuple[str, ...] = ("blob", "pond", "road", "donut", "irregular"),
) -> list[str]:
    outputs: list[str] = []
    for shape in shapes:
        mask = [[char == "1" for char in row] for row in SHAPES[shape]]
        width = max(len(row) for row in mask)
        height = len(mask)
        image = Image.new("RGBA", (width * tile_size, height * tile_size), (0, 0, 0, 0))
        for row_index, row in enumerate(mask):
            for col_index, enabled in enumerate(row):
                if not enabled:
                    continue
                role = choose_blob47_tile(mask, row_index, col_index) if "neutral_base" in tiles else choose_tile(mask, row_index, col_index)
                tile = _select_variant_tile(tiles, variant_tiles or {}, role, row_index, col_index)
                image.alpha_composite(tile, (col_index * tile_size, row_index * tile_size))
        output = f"{prefix}_{shape}.png"
        image.save(task_path / output)
        outputs.append(output)
    return outputs


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


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def _validate_mode(mode: str) -> None:
    if mode not in {"autotile16", "blob47"}:
        raise ValueError(f"Unsupported terrain material mode: {mode}")


def _validate_theme(theme: str) -> None:
    if theme not in {"volcano", "campus"}:
        raise ValueError(f"Unsupported terrain material theme: {theme}")


def _canonical_material_preset(theme: str, material_preset: str) -> str:
    if material_preset == "default":
        return "campus_path" if theme == "campus" else "default"
    _validate_material_preset(theme, material_preset)
    return material_preset


def _validate_material_preset(theme: str, material_preset: str) -> None:
    allowed = {
        "volcano": {"default"},
        "campus": {"default", "campus_path", "campus_brick", "campus_track", "campus_sand", "campus_dirt"},
    }[theme]
    if material_preset not in allowed:
        raise ValueError(
            f"Unsupported material preset {material_preset!r} for theme {theme!r}. "
            f"Available: {', '.join(sorted(allowed))}"
        )


def _validate_variants(variants: int) -> None:
    if variants < 1 or variants > 4:
        raise ValueError("variants must be between 1 and 4")


def _frame_layout_for_mode(mode: str) -> str:
    return "terrain_blob47" if mode == "blob47" else "terrain_autotile16"


def _frame_grid_for_mode(mode: str) -> tuple[int, int]:
    return (6, 8) if mode == "blob47" else (4, 4)


def _tile_order_for_mode(mode: str) -> dict[str, int]:
    return BLOB47 if mode == "blob47" else AUTOTILE16


def _tile_order_for_layout(frame_layout: str) -> dict[str, int]:
    return BLOB47 if frame_layout == "terrain_blob47" else AUTOTILE16


def _tile_order_for_grid(frame_grid: tuple[int, int]) -> dict[str, int]:
    return BLOB47 if frame_grid == (6, 8) else AUTOTILE16
