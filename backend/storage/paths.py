from __future__ import annotations

from pathlib import Path


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


TASK_CATEGORY_BY_ASSET_TYPE = {
    "character": "sprites",
    "prop": "props",
    "icon": "icons",
    "tile": "tiles",
}


def task_dir(assets_dir: Path, task_id: str, category: str = "generated") -> Path:
    return ensure_dir(assets_dir / "tasks" / category / task_id)


def category_for_asset_type(asset_type: str) -> str:
    return TASK_CATEGORY_BY_ASSET_TYPE.get(asset_type, "generated")


def resolve_task_dir(assets_dir: Path, task_id: str) -> Path:
    direct = assets_dir / task_id
    if direct.exists():
        return direct
    tasks_root = assets_dir / "tasks"
    if tasks_root.exists():
        for category_dir in tasks_root.iterdir():
            if not category_dir.is_dir():
                continue
            candidate = category_dir / task_id
            if candidate.exists():
                return candidate
    return assets_dir / "tasks" / "generated" / task_id
