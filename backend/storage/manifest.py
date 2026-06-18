from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def read_manifest(task_path: Path) -> dict[str, Any]:
    path = task_path / "meta.json"
    if not path.exists():
        raise FileNotFoundError(f"Manifest not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def write_manifest(task_path: Path, manifest: dict[str, Any]) -> None:
    path = task_path / "meta.json"
    path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def find_candidate(manifest: dict[str, Any], candidate_index: int) -> dict[str, Any]:
    for candidate in manifest.get("candidates", []):
        if int(candidate.get("index", -1)) == candidate_index:
            return candidate
    raise ValueError(f"Candidate index not found: {candidate_index}")
