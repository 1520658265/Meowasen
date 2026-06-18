from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from backend.storage.paths import resolve_task_dir


@dataclass(frozen=True)
class RegisterMaterialParams:
    material_id: str
    name: str
    kit_path: Path
    theme: str
    tags: tuple[str, ...] = ()
    grid_size: int = 4
    description: str = ""
    root_dir: Path = Path("assets/library")


@dataclass(frozen=True)
class RegisterTerrainParams:
    terrain_id: str
    name: str
    task_id: str
    material_id: str
    theme: str
    material_preset: str = "default"
    tags: tuple[str, ...] = ()
    description: str = ""
    assets_dir: Path = Path("assets")
    root_dir: Path = Path("assets/library")


@dataclass(frozen=True)
class ListLibraryParams:
    kind: str = "all"
    theme: str | None = None
    tags: tuple[str, ...] = ()
    root_dir: Path = Path("assets/library")


@dataclass(frozen=True)
class FindLibraryParams:
    kind: str = "terrain"
    theme: str | None = None
    tags: tuple[str, ...] = ()
    limit: int = 5
    root_dir: Path = Path("assets/library")


def register_material(params: RegisterMaterialParams) -> dict[str, Any]:
    _validate_id(params.material_id, "material_id")
    if not params.kit_path.exists():
        raise FileNotFoundError(f"Material kit not found: {params.kit_path}")
    if params.grid_size < 1:
        raise ValueError("grid_size must be positive")

    root = params.root_dir
    item_dir = root / "material_kits" / params.material_id
    item_dir.mkdir(parents=True, exist_ok=True)
    image_name = _copy_file(params.kit_path, item_dir, "source" + params.kit_path.suffix.lower())

    prompt_name = None
    prompt_path = params.kit_path.parent / "prompt.txt"
    if prompt_path.exists():
        prompt_name = _copy_file(prompt_path, item_dir, "prompt.txt")

    now = _now()
    entry = {
        "id": params.material_id,
        "kind": "material_kit",
        "name": params.name,
        "theme": params.theme,
        "tags": sorted(set(params.tags)),
        "description": params.description,
        "grid_size": params.grid_size,
        "paths": {
            "image": f"material_kits/{params.material_id}/{image_name}",
            "prompt": f"material_kits/{params.material_id}/{prompt_name}" if prompt_name else None,
        },
        "source": {
            "original_path": str(params.kit_path),
        },
        "created_at": now,
        "updated_at": now,
    }
    _write_json(item_dir / "material.json", entry)
    index = _read_index(root)
    index.setdefault("materials", {})[params.material_id] = _summary(entry)
    index["updated_at"] = now
    _write_json(root / "index.json", index)
    return entry


def register_terrain(params: RegisterTerrainParams) -> dict[str, Any]:
    _validate_id(params.terrain_id, "terrain_id")
    _validate_id(params.material_id, "material_id")
    task_path = resolve_task_dir(params.assets_dir, params.task_id)
    if not task_path.exists():
        raise FileNotFoundError(f"Terrain task not found: {task_path}")

    manifest_path = task_path / "meta.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"Terrain task manifest not found: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    candidate = (manifest.get("candidates") or [{}])[0]

    root = params.root_dir
    index = _read_index(root)
    if params.material_id not in index.get("materials", {}):
        raise ValueError(f"Material is not registered in library: {params.material_id}")

    item_dir = root / "terrain_sets" / params.terrain_id
    item_dir.mkdir(parents=True, exist_ok=True)

    copied: dict[str, str | None] = {}
    for key, filename in {
        "sheet": candidate.get("sheet") or "sheet_0.png",
        "variant_sheet": candidate.get("variant_sheet"),
        "preview": "terrain_preview_irregular.png",
        "manifest": "meta.json",
    }.items():
        if not filename:
            copied[key] = None
            continue
        source = task_path / str(filename)
        copied[key] = (
            f"terrain_sets/{params.terrain_id}/{_copy_file(source, item_dir, source.name)}"
            if source.exists()
            else None
        )

    now = _now()
    entry = {
        "id": params.terrain_id,
        "kind": "terrain_set",
        "name": params.name,
        "theme": params.theme,
        "material_id": params.material_id,
        "material_preset": params.material_preset,
        "tags": sorted(set(params.tags)),
        "description": params.description,
        "frame_layout": manifest.get("frame_layout"),
        "frame_grid": manifest.get("frame_grid"),
        "tile_size": manifest.get("output_size"),
        "paths": copied,
        "source": {
            "task_id": params.task_id,
            "task_path": str(task_path),
        },
        "created_at": now,
        "updated_at": now,
    }
    _write_json(item_dir / "terrain.json", entry)
    index.setdefault("terrain_sets", {})[params.terrain_id] = _summary(entry)
    index["updated_at"] = now
    _write_json(root / "index.json", index)
    return entry


def list_library(params: ListLibraryParams) -> dict[str, Any]:
    index = _read_index(params.root_dir)
    result = {
        "root": str(params.root_dir),
        "materials": [],
        "terrain_sets": [],
    }
    wanted_tags = set(params.tags)
    if params.kind in {"all", "material", "materials"}:
        result["materials"] = [
            item
            for item in index.get("materials", {}).values()
            if _matches(item, params.theme, wanted_tags)
        ]
    if params.kind in {"all", "terrain", "terrains"}:
        result["terrain_sets"] = [
            item
            for item in index.get("terrain_sets", {}).values()
            if _matches(item, params.theme, wanted_tags)
        ]
    return result


def find_library(params: FindLibraryParams) -> dict[str, Any]:
    if params.limit < 1:
        raise ValueError("limit must be positive")

    index = _read_index(params.root_dir)
    wanted_tags = set(params.tags)
    entries: list[dict[str, Any]] = []
    if params.kind in {"all", "material", "materials"}:
        entries.extend(index.get("materials", {}).values())
    if params.kind in {"all", "terrain", "terrains"}:
        entries.extend(index.get("terrain_sets", {}).values())

    matches = []
    for item in entries:
        if params.theme and item.get("theme") != params.theme:
            continue
        item_tags = set(item.get("tags") or [])
        matched_tags = sorted(wanted_tags & item_tags)
        missing_tags = sorted(wanted_tags - item_tags)
        if wanted_tags and not matched_tags:
            continue
        exact = bool(wanted_tags) and not missing_tags
        score = len(matched_tags) * 10
        if item.get("theme") == params.theme:
            score += 5
        if exact:
            score += 20
        matches.append(
            {
                "score": score,
                "exact": exact,
                "matched_tags": matched_tags,
                "missing_tags": missing_tags,
                "item": item,
            }
        )

    matches.sort(key=lambda match: (-match["score"], match["item"].get("id", "")))
    return {
        "root": str(params.root_dir),
        "query": {
            "kind": params.kind,
            "theme": params.theme,
            "tags": sorted(wanted_tags),
            "limit": params.limit,
        },
        "matches": matches[: params.limit],
    }


def _matches(item: dict[str, Any], theme: str | None, tags: set[str]) -> bool:
    if theme and item.get("theme") != theme:
        return False
    if tags and not tags.issubset(set(item.get("tags") or [])):
        return False
    return True


def _summary(entry: dict[str, Any]) -> dict[str, Any]:
    keep = (
        "id",
        "kind",
        "name",
        "theme",
        "material_id",
        "material_preset",
        "tags",
        "description",
        "grid_size",
        "frame_layout",
        "frame_grid",
        "tile_size",
        "paths",
        "updated_at",
    )
    return {key: entry[key] for key in keep if key in entry}


def _read_index(root: Path) -> dict[str, Any]:
    root.mkdir(parents=True, exist_ok=True)
    path = root / "index.json"
    if not path.exists():
        return {
            "version": 1,
            "materials": {},
            "terrain_sets": {},
            "updated_at": _now(),
        }
    return json.loads(path.read_text(encoding="utf-8"))


def _copy_file(source: Path, target_dir: Path, target_name: str) -> str:
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / target_name
    shutil.copy2(source, target)
    return target.name


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _validate_id(value: str, field: str) -> None:
    if not value or any(char in value for char in "\\/:*?\"<>| "):
        raise ValueError(f"Invalid {field}: {value}")


def _now() -> str:
    return datetime.now(UTC).isoformat()
