from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from backend.storage.manifest import find_candidate, read_manifest
from backend.storage.paths import resolve_task_dir


@dataclass(frozen=True)
class TerrainPackParams:
    pack_id: str
    name: str
    tile_size: int = 64
    root_dir: Path = Path("assets/terrain")


@dataclass(frozen=True)
class RegisterTerrainSetParams:
    pack_id: str
    terrain_id: str
    set_id: str
    set_type: str
    task_id: str
    candidate_index: int = 0
    material: str | None = None
    priority: int = 10
    rows: str | None = None
    animation_fps: int | None = None
    animation_frames: int | None = None
    root_dir: Path = Path("assets/terrain")
    assets_dir: Path = Path("assets")


def init_pack(params: TerrainPackParams) -> dict[str, Any]:
    pack_path = _pack_path(params.root_dir, params.pack_id)
    pack_path.mkdir(parents=True, exist_ok=True)
    manifest_path = pack_path / "terrain_manifest.json"
    if manifest_path.exists():
        return _read_json(manifest_path)

    now = datetime.now(UTC).isoformat()
    manifest = {
        "schema_version": 1,
        "pack_id": params.pack_id,
        "name": params.name,
        "created_at": now,
        "updated_at": now,
        "tile_size": params.tile_size,
        "terrains": [],
        "sets": [],
        "rules": {
            "composition": "base + ordered overlays + optional animated overlays",
            "autotile": {
                "preferred": "blob47",
                "manual_extension": "append new terrain sets without reordering existing set ids",
            },
        },
    }
    _write_json(manifest_path, manifest)
    return manifest


def register_from_task(params: RegisterTerrainSetParams) -> dict[str, Any]:
    pack_path = _pack_path(params.root_dir, params.pack_id)
    manifest_path = pack_path / "terrain_manifest.json"
    if not manifest_path.exists():
        init_pack(
            TerrainPackParams(
                pack_id=params.pack_id,
                name=params.pack_id,
                root_dir=params.root_dir,
            )
        )
    manifest = _read_json(manifest_path)

    task_path = resolve_task_dir(params.assets_dir, params.task_id)
    task_manifest = read_manifest(task_path)
    candidate = find_candidate(task_manifest, params.candidate_index)
    tile_size = int(task_manifest.get("output_size") or manifest.get("tile_size") or 64)

    terrain = _find_or_add_terrain(
        manifest,
        terrain_id=params.terrain_id,
        material=params.material or params.terrain_id,
    )
    terrain["material"] = params.material or terrain.get("material") or params.terrain_id

    terrain_dir = pack_path / params.terrain_id
    terrain_dir.mkdir(parents=True, exist_ok=True)

    atlas_source = task_path / candidate["sheet"]
    if not atlas_source.exists():
        raise FileNotFoundError(f"Candidate sheet not found: {atlas_source}")

    atlas_name = f"{params.set_id}_{atlas_source.name}"
    atlas_target = terrain_dir / atlas_name
    shutil.copy2(atlas_source, atlas_target)

    processed_atlas = _compose_processed_atlas(
        task_path=task_path,
        candidate=candidate,
        tile_size=tile_size,
        frame_grid=task_manifest.get("frame_grid") or [1, 1],
    )
    processed_name = f"{params.set_id}_processed.png"
    processed_atlas.save(terrain_dir / processed_name)

    terrain_set = {
        "id": params.set_id,
        "terrain": params.terrain_id,
        "type": params.set_type,
        "priority": params.priority,
        "tile_size": tile_size,
        "source_task_id": params.task_id,
        "source_candidate_index": params.candidate_index,
        "atlas": _relative_path(atlas_target, pack_path),
        "processed_atlas": f"{params.terrain_id}/{processed_name}",
        "frame_grid": list(task_manifest.get("frame_grid") or [1, 1]),
        "rows": _parse_rows(params.rows),
        "animation": None,
    }
    if params.set_type == "animated" or params.animation_frames or params.animation_fps:
        terrain_set["animation"] = {
            "fps": params.animation_fps or 6,
            "frames": params.animation_frames,
            "loop": "forward",
        }

    sets = [item for item in manifest.get("sets", []) if item.get("id") != params.set_id]
    sets.append(terrain_set)
    sets.sort(key=lambda item: (int(item.get("priority") or 0), str(item.get("id") or "")))
    manifest["sets"] = sets
    manifest["tile_size"] = tile_size
    manifest["updated_at"] = datetime.now(UTC).isoformat()
    _write_json(manifest_path, manifest)
    return manifest


def _pack_path(root_dir: Path, pack_id: str) -> Path:
    if not pack_id or any(char in pack_id for char in "\\/:*?\"<>|"):
        raise ValueError(f"Invalid pack_id: {pack_id}")
    return root_dir / pack_id


def _find_or_add_terrain(manifest: dict[str, Any], terrain_id: str, material: str) -> dict[str, Any]:
    if not terrain_id or any(char in terrain_id for char in "\\/:*?\"<>|"):
        raise ValueError(f"Invalid terrain_id: {terrain_id}")
    terrains = manifest.setdefault("terrains", [])
    for terrain in terrains:
        if terrain.get("id") == terrain_id:
            return terrain
    terrain = {
        "id": terrain_id,
        "material": material,
        "collision": "walkable",
        "tags": [],
    }
    terrains.append(terrain)
    terrains.sort(key=lambda item: str(item.get("id") or ""))
    return terrain


def _compose_processed_atlas(
    task_path: Path,
    candidate: dict[str, Any],
    tile_size: int,
    frame_grid: list[int] | tuple[int, int],
) -> Any:
    from PIL import Image

    rows, cols = int(frame_grid[0]), int(frame_grid[1])
    atlas = Image.new("RGBA", (cols * tile_size, rows * tile_size), (0, 0, 0, 0))
    for frame in candidate.get("frames", []):
        processed = frame.get("processed")
        grid_pos = frame.get("grid_pos") or [0, int(frame.get("frame_index") or 0)]
        if not processed:
            continue
        path = task_path / processed
        if not path.exists():
            continue
        tile = Image.open(path).convert("RGBA")
        if tile.size != (tile_size, tile_size):
            tile = tile.resize((tile_size, tile_size), Image.Resampling.NEAREST)
        row, col = int(grid_pos[0]), int(grid_pos[1])
        atlas.alpha_composite(tile, (col * tile_size, row * tile_size))
    return atlas


def _parse_rows(value: str | None) -> list[int] | None:
    if not value:
        return None
    rows: list[int] = []
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start, end = part.split("-", 1)
            rows.extend(range(int(start), int(end) + 1))
        else:
            rows.append(int(part))
    return rows


def _relative_path(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
