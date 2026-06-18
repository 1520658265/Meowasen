from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


class PromptError(ValueError):
    pass


@dataclass(frozen=True)
class BuiltPrompt:
    enhanced_prompt: str
    negative_prompt: str
    frame_grid: tuple[int, int]
    frame_layout: str


PROJECT_RPG_POSITIVE = (
    "Project-wide style contract: RPG game art first, cute colorful pixel-art "
    "map readability, limited cohesive palette, clean silhouettes, low-noise "
    "symbolic detail, crisp hand-placed pixels."
)

PROJECT_RPG_NEGATIVE = (
    "non-RPG style, photorealistic material texture, raw texture scan, generic "
    "texture sample, 3d render, noisy gritty surface, cinematic scene painting"
)

CHROMA_KEY_ASSET_TYPES = {"character", "prop", "icon"}
CHROMA_KEY_POSITIVE = (
    "Generation background contract: use a flat solid chroma-key background "
    "with exact RGB(255,0,255), hex #FF00FF, filling every cell behind the "
    "subject. This is not final transparency; alpha transparency is created "
    "only by post-processing. Keep RGB(255,0,255) / #FF00FF out of the "
    "character, object, weapon, outfit, highlights, and effects."
)
CHROMA_KEY_NEGATIVE = (
    "transparent background, checkerboard transparency grid, fake transparency, "
    "gradient background, textured background, scenery, floor plane, cast shadow, "
    "contact shadow, white background, black background, using RGB(255,0,255) "
    "or #FF00FF on the subject"
)


class PromptBuilder:
    def __init__(self, templates_dir: Path, frame_layouts_dir: Path) -> None:
        self.templates_dir = templates_dir
        self.frame_layouts_dir = frame_layouts_dir

    def build(self, asset_type: str, frame_layout: str, user_prompt: str) -> BuiltPrompt:
        user_prompt = user_prompt.strip()
        if not user_prompt:
            raise PromptError("User prompt cannot be empty")

        template = self._load_yaml(self.templates_dir / f"{asset_type}.yaml")
        layout = self._load_yaml(self.frame_layouts_dir / f"{frame_layout}.yaml")

        prefix = str(template.get("prefix", "")).strip()
        negative = str(template.get("negative", "")).strip()
        description = str(layout.get("description", "")).strip()
        frame_prompts = [str(item).strip() for item in layout.get("frame_prompts") or []]
        negative_extra = str(layout.get("negative_extra", "")).strip()
        grid_raw = layout.get("grid") or [1, 1]
        if not isinstance(grid_raw, list | tuple) or len(grid_raw) != 2:
            raise PromptError(f"Invalid frame layout grid: {frame_layout}")
        frame_grid = (int(grid_raw[0]), int(grid_raw[1]))

        chroma_positive = CHROMA_KEY_POSITIVE if asset_type in CHROMA_KEY_ASSET_TYPES else ""
        chroma_negative = CHROMA_KEY_NEGATIVE if asset_type in CHROMA_KEY_ASSET_TYPES else ""

        positive_parts = [PROJECT_RPG_POSITIVE, prefix, chroma_positive, description, user_prompt, *frame_prompts]
        enhanced_prompt = " ".join(part for part in positive_parts if part)
        negative_prompt = ", ".join(
            part for part in [PROJECT_RPG_NEGATIVE, negative, chroma_negative, negative_extra] if part
        )
        return BuiltPrompt(
            enhanced_prompt=enhanced_prompt,
            negative_prompt=negative_prompt,
            frame_grid=frame_grid,
            frame_layout=frame_layout,
        )

    @staticmethod
    def _load_yaml(path: Path) -> dict:
        if not path.exists():
            raise PromptError(f"Template not found: {path}")
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            raise PromptError(f"Invalid template: {path}")
        return data
