from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from backend.config import ConfigError, load_config
from backend.generator.protocol import GeneratorError
from backend.service.core_generation import (
    AlignFramesParams,
    CoreGenerationService,
    ExportTilesParams,
    GenerateParams,
    ImportSheetParams,
    ReprocessParams,
    SelectFrameParams,
)
from backend.terrain.pack import (
    RegisterTerrainSetParams,
    TerrainPackParams,
    init_pack,
    register_from_task,
)
from backend.terrain.material_kit import (
    TerrainMaterialBuildParams,
    TerrainMaterialDemoParams,
    build_material_demo,
    build_material_from_kit,
)
from backend.terrain.preview import TerrainPreviewParams, build_preview
from backend.terrain.scene_tileset import SceneTerrainBuildParams, build_scene_terrain_tileset
from backend.resource_library import (
    FindLibraryParams,
    ListLibraryParams,
    RegisterMaterialParams,
    RegisterTerrainParams,
    find_library,
    list_library,
    register_material,
    register_terrain,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="meowasen", description="Meowasen phase 1 CLI")
    parser.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser("generate", help="Generate assets")
    generate.add_argument("--asset-type", required=True, choices=["character", "prop", "icon", "tile"])
    generate.add_argument("--frame-layout", default=None)
    generate.add_argument("--prompt")
    generate.add_argument("--prompt-file", help="Read UTF-8 prompt text from a file")
    generate.add_argument("--task-id", help="Reuse a fixed task directory instead of creating a new UUID")
    generate.add_argument("--count", type=int)
    generate.add_argument("--output-size", type=int)
    generate.add_argument("--cell-size", type=int)
    generate.add_argument("--palette-colors", type=int)
    generate.add_argument("--canvas-padding", type=int)
    generate.add_argument("--seed", type=int)
    generate.add_argument("--backend", dest="backend_override", help="Override generator backend, e.g. mock")
    generate.add_argument("--model", help="Override generator model for this request")
    generate.add_argument("--style-lock-task")
    generate.add_argument("--style-lock-candidate", type=int)
    generate.add_argument("--dry-run", action="store_true")

    reprocess = subparsers.add_parser("reprocess", help="Reprocess an existing candidate")
    reprocess.add_argument("--task-id", required=True)
    reprocess.add_argument("--candidate-index", required=True, type=int)
    reprocess.add_argument("--output-size", type=int)
    reprocess.add_argument("--palette-colors", type=int)
    reprocess.add_argument("--canvas-padding", type=int)
    reprocess.add_argument("--use-style-lock", action="store_true")

    select_frame = subparsers.add_parser("select-frame", help="Select one processed frame as the final sprite")
    select_frame.add_argument("--task-id", required=True)
    select_frame.add_argument("--candidate-index", required=True, type=int)
    select_frame.add_argument("--frame-index", required=True, type=int)
    select_frame.add_argument("--prefix", default="selected")
    select_frame.add_argument(
        "--overwrite-primary",
        action="store_true",
        help="Also overwrite processed_0_0.png for the current logical test output",
    )

    style = subparsers.add_parser("extract-style-lock", help="Extract palette from a candidate")
    style.add_argument("--task-id", required=True)
    style.add_argument("--candidate-index", required=True, type=int)
    style.add_argument("--colors", type=int)

    align = subparsers.add_parser("align-frames", help="Align frame anchors on a transparent canvas")
    align.add_argument("--task-id", required=True)
    align.add_argument("--candidate-index", required=True, type=int)
    align.add_argument("--source", choices=["processed", "raw"], default="processed")
    align.add_argument("--prefix", default="aligned")
    align.add_argument("--anchor", choices=["alpha_centroid", "bbox_center"], default="alpha_centroid")
    align.add_argument("--axis", choices=["x", "y", "xy"], default="x")

    import_sheet = subparsers.add_parser("import-sheet", help="Import an existing generated sheet")
    import_sheet.add_argument("--sheet-path", required=True)
    import_sheet.add_argument("--asset-type", required=True, choices=["character", "prop", "icon", "tile"])
    import_sheet.add_argument("--frame-layout", required=True)
    import_sheet.add_argument("--prompt", default="")
    import_sheet.add_argument("--prompt-file", help="Read UTF-8 prompt text from a file")
    import_sheet.add_argument("--task-id", help="Reuse a fixed task directory instead of creating a new UUID")
    import_sheet.add_argument("--output-size", type=int)
    import_sheet.add_argument("--cell-size", type=int)
    import_sheet.add_argument("--palette-colors", type=int)
    import_sheet.add_argument("--canvas-padding", type=int)
    import_sheet.add_argument("--model", default="imported")
    import_sheet.add_argument("--source-task-id")

    export_tiles = subparsers.add_parser("export-tiles", help="Export tile frames at a target size")
    export_tiles.add_argument("--task-id", required=True)
    export_tiles.add_argument("--candidate-index", type=int, default=0)
    export_tiles.add_argument("--source", choices=["processed", "raw"], default="processed")
    export_tiles.add_argument("--output-size", type=int, default=64)
    export_tiles.add_argument("--palette-colors", type=int, default=0)
    export_tiles.add_argument("--canvas-padding", type=int, default=0)
    export_tiles.add_argument("--prefix", default="export")

    terrain_init = subparsers.add_parser("terrain-init", help="Create a terrain pack manifest")
    terrain_init.add_argument("--pack-id", required=True)
    terrain_init.add_argument("--name", required=True)
    terrain_init.add_argument("--tile-size", type=int, default=64)

    terrain_register = subparsers.add_parser("terrain-register", help="Register a generated candidate as a terrain set")
    terrain_register.add_argument("--pack-id", required=True)
    terrain_register.add_argument("--terrain-id", required=True)
    terrain_register.add_argument("--set-id", required=True)
    terrain_register.add_argument("--set-type", required=True, choices=["base", "blob47", "overlay", "animated"])
    terrain_register.add_argument("--task-id", required=True)
    terrain_register.add_argument("--candidate-index", type=int, default=0)
    terrain_register.add_argument("--material")
    terrain_register.add_argument("--priority", type=int, default=10)
    terrain_register.add_argument("--rows")
    terrain_register.add_argument("--animation-fps", type=int)
    terrain_register.add_argument("--animation-frames", type=int)

    terrain_preview = subparsers.add_parser("terrain-preview", help="Compose test shapes from an autotile task")
    terrain_preview.add_argument("--task-id", required=True)
    terrain_preview.add_argument("--candidate-index", type=int, default=0)
    terrain_preview.add_argument("--shape", choices=["blob", "pond", "road", "donut", "irregular"], default="blob")
    terrain_preview.add_argument("--output-name")
    terrain_preview.add_argument("--animation-task-id")
    terrain_preview.add_argument("--animation-candidate-index", type=int, default=0)
    terrain_preview.add_argument("--animation-row", type=int, default=0)

    terrain_material_demo = subparsers.add_parser(
        "terrain-material-demo",
        help="Build a procedural material-kit autotile validation task",
    )
    terrain_material_demo.add_argument("--task-id")
    terrain_material_demo.add_argument("--tile-size", type=int, default=64)
    terrain_material_demo.add_argument("--animation-frames", type=int, default=4)
    terrain_material_demo.add_argument("--seed", type=int, default=17)
    terrain_material_demo.add_argument("--mode", choices=["autotile16", "blob47"], default="blob47")
    terrain_material_demo.add_argument("--variants", type=int, default=1)

    terrain_material_build = subparsers.add_parser(
        "terrain-material-build",
        help="Expand a generated material-kit sheet into a rule-based autotile task",
    )
    terrain_material_build.add_argument("--kit-path", required=True)
    terrain_material_build.add_argument("--task-id")
    terrain_material_build.add_argument("--tile-size", type=int, default=128)
    terrain_material_build.add_argument("--grid-size", type=int, default=4)
    terrain_material_build.add_argument("--animation-frames", type=int, default=4)
    terrain_material_build.add_argument("--seed", type=int, default=17)
    terrain_material_build.add_argument("--mode", choices=["autotile16", "blob47"], default="blob47")
    terrain_material_build.add_argument("--cell-margin-ratio", type=float, default=0.07)
    terrain_material_build.add_argument("--variants", type=int, default=1)
    terrain_material_build.add_argument("--theme", choices=["volcano", "campus"], default="volcano")
    terrain_material_build.add_argument(
        "--material-preset",
        choices=["default", "campus_path", "campus_brick", "campus_track", "campus_sand", "campus_dirt"],
        default="default",
    )

    library_register_material = subparsers.add_parser(
        "library-register-material",
        help="Copy a reusable material kit into the public asset library",
    )
    library_register_material.add_argument("--material-id", required=True)
    library_register_material.add_argument("--name", required=True)
    library_register_material.add_argument("--kit-path", required=True)
    library_register_material.add_argument("--theme", required=True)
    library_register_material.add_argument("--tags", nargs="*", default=[])
    library_register_material.add_argument("--grid-size", type=int, default=4)
    library_register_material.add_argument("--description", default="")

    library_register_terrain = subparsers.add_parser(
        "library-register-terrain",
        help="Copy a reusable terrain set into the public asset library",
    )
    library_register_terrain.add_argument("--terrain-id", required=True)
    library_register_terrain.add_argument("--name", required=True)
    library_register_terrain.add_argument("--task-id", required=True)
    library_register_terrain.add_argument("--material-id", required=True)
    library_register_terrain.add_argument("--theme", required=True)
    library_register_terrain.add_argument("--material-preset", default="default")
    library_register_terrain.add_argument("--tags", nargs="*", default=[])
    library_register_terrain.add_argument("--description", default="")

    library_list = subparsers.add_parser("library-list", help="List reusable assets in the public library")
    library_list.add_argument(
        "--kind",
        choices=["all", "material", "materials", "terrain", "terrains"],
        default="all",
    )
    library_list.add_argument("--theme")
    library_list.add_argument("--tags", nargs="*", default=[])

    library_find = subparsers.add_parser(
        "library-find",
        help="Find the best reusable library assets for a dynamic scene request",
    )
    library_find.add_argument(
        "--kind",
        choices=["all", "material", "materials", "terrain", "terrains"],
        default="terrain",
    )
    library_find.add_argument("--theme")
    library_find.add_argument("--tags", nargs="*", default=[])
    library_find.add_argument("--limit", type=int, default=5)

    scene_terrain_build = subparsers.add_parser(
        "scene-terrain-build",
        help="Build a final reusable terrain tileset for a scene from library terrain families",
    )
    scene_terrain_build.add_argument("--scene", required=True)
    scene_terrain_build.add_argument("--task-id")
    scene_terrain_build.add_argument("--theme")
    scene_terrain_build.add_argument("--tile-size", type=int, default=64)
    scene_terrain_build.add_argument("--art-style", choices=["source", "rpg"], default="rpg")
    scene_terrain_build.add_argument("--profile-path")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        service = CoreGenerationService(config)
        if args.command == "generate":
            prompt = _resolve_prompt(args.prompt, args.prompt_file)
            frame_layout = args.frame_layout or config.generation.default_frame_layout
            manifest = asyncio.run(
                service.generate(
                    GenerateParams(
                        asset_type=args.asset_type,
                        frame_layout=frame_layout,
                        user_prompt=prompt,
                        task_id=args.task_id,
                        count=args.count,
                        output_size=args.output_size,
                        cell_size=args.cell_size,
                        palette_colors=args.palette_colors,
                        canvas_padding=args.canvas_padding,
                        seed=args.seed,
                        backend_override=args.backend_override,
                        model=args.model,
                        style_lock_task=args.style_lock_task,
                        style_lock_candidate=args.style_lock_candidate,
                        dry_run=args.dry_run,
                    )
                )
            )
        elif args.command == "reprocess":
            manifest = service.reprocess(
                ReprocessParams(
                    task_id=args.task_id,
                    candidate_index=args.candidate_index,
                    output_size=args.output_size,
                    palette_colors=args.palette_colors,
                    canvas_padding=args.canvas_padding,
                    use_style_lock=args.use_style_lock,
                )
            )
        elif args.command == "select-frame":
            manifest = service.select_frame(
                SelectFrameParams(
                    task_id=args.task_id,
                    candidate_index=args.candidate_index,
                    frame_index=args.frame_index,
                    prefix=args.prefix,
                    overwrite_primary=args.overwrite_primary,
                )
            )
        elif args.command == "extract-style-lock":
            manifest = service.extract_style_lock(
                task_id=args.task_id,
                candidate_index=args.candidate_index,
                colors=args.colors,
            )
        elif args.command == "align-frames":
            manifest = service.align_frames(
                AlignFramesParams(
                    task_id=args.task_id,
                    candidate_index=args.candidate_index,
                    source=args.source,
                    prefix=args.prefix,
                    anchor=args.anchor,
                    axis=args.axis,
                )
            )
        elif args.command == "import-sheet":
            manifest = asyncio.run(
                service.import_sheet(
                    ImportSheetParams(
                        sheet_path=Path(args.sheet_path),
                        asset_type=args.asset_type,
                        frame_layout=args.frame_layout,
                        user_prompt=_resolve_prompt(args.prompt, args.prompt_file, allow_empty=True),
                        task_id=args.task_id,
                        output_size=args.output_size,
                        cell_size=args.cell_size,
                        palette_colors=args.palette_colors,
                        canvas_padding=args.canvas_padding,
                        model=args.model,
                        source_task_id=args.source_task_id,
                    )
                )
            )
        elif args.command == "export-tiles":
            manifest = service.export_tiles(
                ExportTilesParams(
                    task_id=args.task_id,
                    candidate_index=args.candidate_index,
                    source=args.source,
                    output_size=args.output_size,
                    palette_colors=args.palette_colors,
                    canvas_padding=args.canvas_padding,
                    prefix=args.prefix,
                )
            )
        elif args.command == "terrain-init":
            manifest = init_pack(
                TerrainPackParams(
                    pack_id=args.pack_id,
                    name=args.name,
                    tile_size=args.tile_size,
                    root_dir=config.paths.assets_dir / "terrain",
                )
            )
        elif args.command == "terrain-register":
            manifest = register_from_task(
                RegisterTerrainSetParams(
                    pack_id=args.pack_id,
                    terrain_id=args.terrain_id,
                    set_id=args.set_id,
                    set_type=args.set_type,
                    task_id=args.task_id,
                    candidate_index=args.candidate_index,
                    material=args.material,
                    priority=args.priority,
                    rows=args.rows,
                    animation_fps=args.animation_fps,
                    animation_frames=args.animation_frames,
                    root_dir=config.paths.assets_dir / "terrain",
                    assets_dir=config.paths.assets_dir,
                )
            )
        elif args.command == "terrain-preview":
            manifest = build_preview(
                TerrainPreviewParams(
                    task_id=args.task_id,
                    candidate_index=args.candidate_index,
                    shape=args.shape,
                    output_name=args.output_name,
                    animation_task_id=args.animation_task_id,
                    animation_candidate_index=args.animation_candidate_index,
                    animation_row=args.animation_row,
                    assets_dir=config.paths.assets_dir,
                )
            )
        elif args.command == "terrain-material-demo":
            manifest = build_material_demo(
                TerrainMaterialDemoParams(
                    task_id=args.task_id,
                    tile_size=args.tile_size,
                    animation_frames=args.animation_frames,
                    seed=args.seed,
                    mode=args.mode,
                    variants=args.variants,
                    assets_dir=config.paths.assets_dir,
                )
            )
        elif args.command == "terrain-material-build":
            manifest = build_material_from_kit(
                TerrainMaterialBuildParams(
                    kit_path=Path(args.kit_path),
                    task_id=args.task_id,
                    tile_size=args.tile_size,
                    grid_size=args.grid_size,
                    animation_frames=args.animation_frames,
                    seed=args.seed,
                    mode=args.mode,
                    cell_margin_ratio=args.cell_margin_ratio,
                    variants=args.variants,
                    theme=args.theme,
                    material_preset=args.material_preset,
                    assets_dir=config.paths.assets_dir,
                )
            )
        elif args.command == "library-register-material":
            manifest = register_material(
                RegisterMaterialParams(
                    material_id=args.material_id,
                    name=args.name,
                    kit_path=Path(args.kit_path),
                    theme=args.theme,
                    tags=tuple(args.tags or ()),
                    grid_size=args.grid_size,
                    description=args.description,
                    root_dir=config.paths.assets_dir / "library",
                )
            )
        elif args.command == "library-register-terrain":
            manifest = register_terrain(
                RegisterTerrainParams(
                    terrain_id=args.terrain_id,
                    name=args.name,
                    task_id=args.task_id,
                    material_id=args.material_id,
                    theme=args.theme,
                    material_preset=args.material_preset,
                    tags=tuple(args.tags or ()),
                    description=args.description,
                    assets_dir=config.paths.assets_dir,
                    root_dir=config.paths.assets_dir / "library",
                )
            )
        elif args.command == "library-list":
            manifest = list_library(
                ListLibraryParams(
                    kind=args.kind,
                    theme=args.theme,
                    tags=tuple(args.tags or ()),
                    root_dir=config.paths.assets_dir / "library",
                )
            )
        elif args.command == "library-find":
            manifest = find_library(
                FindLibraryParams(
                    kind=args.kind,
                    theme=args.theme,
                    tags=tuple(args.tags or ()),
                    limit=args.limit,
                    root_dir=config.paths.assets_dir / "library",
                )
            )
        elif args.command == "scene-terrain-build":
            manifest = build_scene_terrain_tileset(
                SceneTerrainBuildParams(
                    scene=args.scene,
                    task_id=args.task_id,
                    theme=args.theme,
                    tile_size=args.tile_size,
                    art_style=args.art_style,
                    profile_path=Path(args.profile_path) if args.profile_path else None,
                    library_dir=config.paths.assets_dir / "library",
                    assets_dir=config.paths.assets_dir,
                )
            )
        else:
            parser.error(f"Unknown command: {args.command}")
            return 2
    except (ConfigError, GeneratorError, ValueError, FileNotFoundError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(_summary(manifest), ensure_ascii=False, indent=2))
    return 0


def _summary(manifest: dict) -> dict:
    if manifest.get("kind") in {"material_kit", "terrain_set"}:
        return manifest
    if manifest.get("kind") == "scene_terrain_tileset":
        return {
            "kind": manifest.get("kind"),
            "task_id": manifest.get("task_id"),
            "status": manifest.get("status"),
            "theme": manifest.get("theme"),
            "tile_size": manifest.get("tile_size"),
            "art_style": manifest.get("art_style"),
            "view": manifest.get("view"),
            "outputs": manifest.get("outputs"),
            "requirements": [
                {
                    "feature_id": item.get("feature_id"),
                    "status": item.get("status"),
                    "terrain_kind": item.get("terrain_kind"),
                    "shape": item.get("shape"),
                    "terrain_id": ((item.get("library_match") or {}).get("item") or {}).get("id"),
                    "missing_tags": item.get("missing_tags"),
                }
                for item in manifest.get("requirements", [])
            ],
            "tileset_sections": [
                {
                    "feature_id": item.get("feature_id"),
                    "terrain_id": item.get("terrain_id"),
                    "family_kind": item.get("family_kind"),
                    "tile_size": item.get("tile_size"),
                    "frame_grid": item.get("frame_grid"),
                    "start_row": item.get("start_row"),
                    "row_count": item.get("row_count"),
                }
                for item in manifest.get("tileset_sections", [])
            ],
            "missing_requirements": manifest.get("missing_requirements", []),
        }
    if "root" in manifest and "materials" in manifest and "terrain_sets" in manifest:
        return manifest
    if "root" in manifest and "matches" in manifest:
        return manifest
    if "tile_order" in manifest:
        return manifest
    if "pack_id" in manifest:
        return {
            "pack_id": manifest.get("pack_id"),
            "name": manifest.get("name"),
            "tile_size": manifest.get("tile_size"),
            "terrains": manifest.get("terrains", []),
            "sets": [
                {
                    "id": item.get("id"),
                    "terrain": item.get("terrain"),
                    "type": item.get("type"),
                    "priority": item.get("priority"),
                    "atlas": item.get("atlas"),
                    "processed_atlas": item.get("processed_atlas"),
                    "animation": item.get("animation"),
                }
                for item in manifest.get("sets", [])
            ],
        }
    return {
        "task_id": manifest.get("task_id"),
        "status": manifest.get("status"),
        "asset_type": manifest.get("asset_type"),
        "frame_layout": manifest.get("frame_layout"),
        "frame_grid": manifest.get("frame_grid"),
        "sheet_size": manifest.get("sheet_size"),
        "output_size": manifest.get("output_size"),
        "palette_colors": manifest.get("palette_colors"),
        "selected_frame": manifest.get("selected_frame"),
        "candidates": [
            {
                "index": candidate.get("index"),
                "status": candidate.get("status"),
                "sheet": candidate.get("sheet"),
                "frames": [
                    {
                        "frame_index": frame.get("frame_index"),
                        "status": frame.get("status"),
                        "processed": frame.get("processed"),
                        "aligned": frame.get("aligned"),
                        "split_quality": frame.get("split_quality"),
                        "postprocess_metrics": frame.get("postprocess_metrics"),
                        "alignment": frame.get("alignment"),
                    }
                    for frame in candidate.get("frames", [])
                ],
                "has_style_lock": bool(candidate.get("style_lock_palette_b64")),
            }
            for candidate in manifest.get("candidates", [])
        ],
    }


def _resolve_prompt(prompt: str | None, prompt_file: str | None, allow_empty: bool = False) -> str:
    if prompt and prompt_file:
        raise ValueError("Use either --prompt or --prompt-file, not both")
    if prompt_file:
        return Path(prompt_file).read_text(encoding="utf-8").strip()
    if prompt:
        return prompt
    if allow_empty:
        return ""
    raise ValueError("Either --prompt or --prompt-file is required")


if __name__ == "__main__":
    raise SystemExit(main())
