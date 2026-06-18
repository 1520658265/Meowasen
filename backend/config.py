from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


ROOT_DIR = Path(__file__).resolve().parents[1]


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class RelayConfig:
    base_url: str
    api_key_env: str


@dataclass(frozen=True)
class GeneratorConfig:
    backend: str
    model: str
    provider_options: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class GenerationConfig:
    default_count: int
    default_counts_by_layout: dict[str, int]
    default_frame_layout: str
    timeout_seconds: int
    daily_limit: int | None


@dataclass(frozen=True)
class OutputConfig:
    default_size: int
    default_sizes_by_asset_type: dict[str, int]
    default_cell_size: int
    default_cell_sizes_by_layout: dict[str, int]
    palette_colors: int
    canvas_padding: int
    enforce_style_lock: bool


@dataclass(frozen=True)
class PathsConfig:
    templates_dir: Path
    frame_layouts_dir: Path
    assets_dir: Path


@dataclass(frozen=True)
class AppConfig:
    relay: RelayConfig
    generator: GeneratorConfig
    generation: GenerationConfig
    output: OutputConfig
    frame_layouts: dict[str, tuple[int, int]]
    paths: PathsConfig


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _resolve_path(value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return ROOT_DIR / path


def load_config(config_path: str | Path = "config.yaml") -> AppConfig:
    path = Path(config_path)
    if not path.is_absolute():
        path = ROOT_DIR / path
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")

    load_dotenv(ROOT_DIR / ".env")

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    try:
        relay_raw = raw["relay"]
        generator_raw = raw["generator"]
        generation_raw = raw["generation"]
        output_raw = raw["output"]
        paths_raw = raw["paths"]
    except KeyError as exc:
        raise ConfigError(f"Missing config section: {exc}") from exc

    frame_layouts_raw = raw.get("frame_layouts", {})
    frame_layouts: dict[str, tuple[int, int]] = {}
    for name, grid in frame_layouts_raw.items():
        if not isinstance(grid, list | tuple) or len(grid) != 2:
            raise ConfigError(f"Invalid frame layout grid for {name}: {grid}")
        frame_layouts[name] = (int(grid[0]), int(grid[1]))

    return AppConfig(
        relay=RelayConfig(
            base_url=str(relay_raw.get("base_url", "")).rstrip("/"),
            api_key_env=str(relay_raw.get("api_key_env", "GEMINI_API_KEY")),
        ),
        generator=GeneratorConfig(
            backend=str(generator_raw.get("backend", "gemini")),
            model=str(generator_raw.get("model", "")),
            provider_options=dict(generator_raw.get("provider_options") or {}),
        ),
        generation=GenerationConfig(
            default_count=int(generation_raw.get("default_count", 1)),
            default_counts_by_layout={
                str(k): int(v)
                for k, v in (generation_raw.get("default_counts_by_layout") or {}).items()
            },
            default_frame_layout=str(generation_raw.get("default_frame_layout", "single")),
            timeout_seconds=int(generation_raw.get("timeout_seconds", 30)),
            daily_limit=(
                None
                if generation_raw.get("daily_limit") is None
                else int(generation_raw.get("daily_limit"))
            ),
        ),
        output=OutputConfig(
            default_size=int(output_raw.get("default_size", 64)),
            default_sizes_by_asset_type={
                str(k): int(v)
                for k, v in (output_raw.get("default_sizes_by_asset_type") or {}).items()
            },
            default_cell_size=int(output_raw.get("default_cell_size", 256)),
            default_cell_sizes_by_layout={
                str(k): int(v)
                for k, v in (output_raw.get("default_cell_sizes_by_layout") or {}).items()
            },
            palette_colors=int(output_raw.get("palette_colors", 16)),
            canvas_padding=int(output_raw.get("canvas_padding", 4)),
            enforce_style_lock=bool(output_raw.get("enforce_style_lock", True)),
        ),
        frame_layouts=frame_layouts,
        paths=PathsConfig(
            templates_dir=_resolve_path(str(paths_raw.get("templates_dir"))),
            frame_layouts_dir=_resolve_path(str(paths_raw.get("frame_layouts_dir"))),
            assets_dir=_resolve_path(str(paths_raw.get("assets_dir"))),
        ),
    )


def get_api_key(config: AppConfig) -> str | None:
    value = os.environ.get(config.relay.api_key_env)
    return value if value else None
