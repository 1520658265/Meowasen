from __future__ import annotations

import asyncio
import io
import json
import shutil
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from statistics import mean
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageDraw

from backend.config import AppConfig
from backend.generator.factory import create_provider
from backend.generator.protocol import GenerateRequest, GeneratedImage, GeneratorError
from backend.postprocess.alignment import align_to_canvas_center
from backend.postprocess.pipeline import PostProcessor, ProcessOptions, save_png
from backend.postprocess.splitter import SpriteSheetSplitter
from backend.postprocess.style_lock import StyleLockExtractor
from backend.prompt.builder import PromptBuilder
from backend.storage.manifest import find_candidate, read_manifest, write_manifest
from backend.storage.paths import category_for_asset_type, resolve_task_dir, task_dir


@dataclass(frozen=True)
class GenerateParams:
    asset_type: str
    frame_layout: str
    user_prompt: str
    task_id: str | None = None
    count: int | None = None
    output_size: int | None = None
    cell_size: int | None = None
    palette_colors: int | None = None
    canvas_padding: int | None = None
    seed: int | None = None
    backend_override: str | None = None
    model: str | None = None
    style_lock_task: str | None = None
    style_lock_candidate: int | None = None
    reference_images: tuple[Path, ...] = ()
    dry_run: bool = False


@dataclass(frozen=True)
class ReprocessParams:
    task_id: str
    candidate_index: int
    output_size: int | None = None
    palette_colors: int | None = None
    canvas_padding: int | None = None
    use_style_lock: bool = False


@dataclass(frozen=True)
class SelectFrameParams:
    task_id: str
    candidate_index: int
    frame_index: int
    prefix: str = "selected"
    overwrite_primary: bool = False


@dataclass(frozen=True)
class AlignFramesParams:
    task_id: str
    candidate_index: int
    source: str = "processed"
    prefix: str = "aligned"
    anchor: str = "alpha_centroid"
    axis: str = "x"


@dataclass(frozen=True)
class ImportSheetParams:
    sheet_path: Path
    asset_type: str
    frame_layout: str
    user_prompt: str = ""
    task_id: str | None = None
    output_size: int | None = None
    cell_size: int | None = None
    palette_colors: int | None = None
    canvas_padding: int | None = None
    model: str = "imported"
    source_task_id: str | None = None


@dataclass(frozen=True)
class ExportTilesParams:
    task_id: str
    candidate_index: int
    source: str = "processed"
    output_size: int = 64
    palette_colors: int = 0
    canvas_padding: int = 0
    prefix: str = "export"


@dataclass(frozen=True)
class BuildWalk4DirParams:
    source_task_id: str
    task_id: str | None = None
    candidate_index: int = 0
    output_size: int | None = None
    mirror_source_row: int = 1
    row_order: tuple[int, int, int, int] | None = None


@dataclass(frozen=True)
class WalkQcParams:
    task_id: str
    candidate_index: int = 0
    source: str = "processed"
    prefix: str = "qc_loop"
    scale: int = 4
    fps: int = 6


class CoreGenerationService:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.prompt_builder = PromptBuilder(config.paths.templates_dir, config.paths.frame_layouts_dir)
        self.splitter = SpriteSheetSplitter()
        self.processor = PostProcessor()
        self.style_extractor = StyleLockExtractor()

    async def generate(self, params: GenerateParams) -> dict[str, Any]:
        resolved = self._resolve_params(params)
        built_prompt = self.prompt_builder.build(
            asset_type=params.asset_type,
            frame_layout=resolved["frame_layout"],
            user_prompt=params.user_prompt,
        )
        rows, cols = built_prompt.frame_grid
        cell_size = resolved["cell_size"]
        sheet_width = cols * cell_size
        sheet_height = rows * cell_size
        style_lock = self._load_style_lock(params.style_lock_task, params.style_lock_candidate)
        reference_images = self._load_reference_images(params.reference_images)

        task_id = params.task_id or str(uuid.uuid4())
        task_path = task_dir(
            self.config.paths.assets_dir,
            task_id,
            category_for_asset_type(params.asset_type),
        )

        manifest = self._new_manifest(
            task_id=task_id,
            params=params,
            resolved=resolved,
            enhanced_prompt=built_prompt.enhanced_prompt,
            negative_prompt=built_prompt.negative_prompt,
            frame_grid=built_prompt.frame_grid,
            sheet_size=(sheet_width, sheet_height),
            style_lock=style_lock,
        )
        if reference_images:
            manifest["manual_reference_images"] = [
                {"path": str(path), "bytes": size}
                for path, size, _payload in reference_images
            ]
        write_manifest(task_path, manifest)

        if params.dry_run:
            manifest["status"] = "dry_run"
            write_manifest(task_path, manifest)
            return manifest

        provider = create_provider(self.config, params.backend_override)
        request = GenerateRequest(
            prompt=built_prompt.enhanced_prompt,
            negative_prompt=built_prompt.negative_prompt,
            count=resolved["count"],
            model=resolved["model"],
            output_size=resolved["output_size"],
            timeout_seconds=self.config.generation.timeout_seconds,
            seed=params.seed,
            frame_layout=resolved["frame_layout"],
            frame_grid=built_prompt.frame_grid,
            cell_size=cell_size,
            sheet_width=sheet_width,
            sheet_height=sheet_height,
            style_lock=style_lock,
            provider_options=self.config.generator.provider_options,
            reference_images=[
                *(style_lock or {}).get("reference_images", []),
                *[payload for _path, _size, payload in reference_images],
            ],
            debug_dir=str(task_path),
        )

        manifest["status"] = "running"
        write_manifest(task_path, manifest)

        try:
            images = await provider.generate(request)
        except Exception as exc:
            manifest["status"] = "failed"
            manifest["error"] = str(exc)
            write_manifest(task_path, manifest)
            raise

        manifest["provider_capabilities"] = provider.capabilities.__dict__
        await self._process_generated_images(task_path, manifest, images, built_prompt.frame_grid, resolved, style_lock)
        manifest["status"] = self._aggregate_task_status(manifest)
        manifest["error"] = None if manifest["status"] != "failed" else "All candidates failed"
        write_manifest(task_path, manifest)
        return manifest

    def reprocess(self, params: ReprocessParams) -> dict[str, Any]:
        task_path = resolve_task_dir(self.config.paths.assets_dir, params.task_id)
        manifest = read_manifest(task_path)
        candidate = find_candidate(manifest, params.candidate_index)
        frame_grid = tuple(manifest.get("frame_grid") or [1, 1])
        resolved = {
            "output_size": (
                params.output_size
                if params.output_size is not None
                else int(manifest.get("output_size") or self.config.output.default_size)
            ),
            "palette_colors": (
                params.palette_colors
                if params.palette_colors is not None
                else int(manifest.get("palette_colors") or self.config.output.palette_colors)
            ),
            "canvas_padding": (
                params.canvas_padding
                if params.canvas_padding is not None
                else self.config.output.canvas_padding
            ),
        }
        style_palette = candidate.get("style_lock_palette_b64") if params.use_style_lock else None

        sheet_path = task_path / candidate["sheet"]
        sheet = Image.open(sheet_path).convert("RGBA")
        frames = self.splitter.split(
            sheet,
            frame_grid,  # type: ignore[arg-type]
            quality_profile=self._quality_profile(manifest),
        )
        candidate["frames"] = []
        candidate["status"] = "running"
        for frame in frames:
            raw_name = f"frame_{candidate['index']}_{frame.frame_index}.png"
            processed_name = f"processed_{candidate['index']}_{frame.frame_index}.png"
            raw_path = task_path / raw_name
            processed_path = task_path / processed_name
            save_png(frame.image, raw_path)
            result = self.processor.process(
                frame.image,
                ProcessOptions(
                    output_size=resolved["output_size"],
                    palette_colors=resolved["palette_colors"],
                    canvas_padding=resolved["canvas_padding"],
                    style_lock_palette_b64=style_palette,
                    mode=self._process_mode(manifest),
                ),
            )
            save_png(result.image, processed_path)
            self._write_processed_previews(task_path, processed_name)
            frame_status = "failed" if result.status == "failed" or frame.split_quality.status == "failed" else "done"
            candidate["frames"].append(
                {
                    "frame_index": frame.frame_index,
                    "grid_pos": list(frame.grid_pos),
                    "raw": raw_name,
                    "processed": processed_name,
                    "bg_removed": result.bg_removed,
                    "split_quality": frame.split_quality.to_dict(),
                    "postprocess_metrics": result.metrics or {},
                    "status": frame_status,
                    "error": result.error,
                }
            )
        candidate["status"] = self._aggregate_candidate_status(candidate)
        manifest["output_size"] = resolved["output_size"]
        manifest["palette_colors"] = resolved["palette_colors"]
        manifest["status"] = self._aggregate_task_status(manifest)
        write_manifest(task_path, manifest)
        return manifest

    def select_frame(self, params: SelectFrameParams) -> dict[str, Any]:
        task_path = resolve_task_dir(self.config.paths.assets_dir, params.task_id)
        manifest = read_manifest(task_path)
        candidate = find_candidate(manifest, params.candidate_index)
        frames = candidate.get("frames") or []
        frame = next(
            (item for item in frames if int(item.get("frame_index", -1)) == params.frame_index),
            None,
        )
        if frame is None:
            raise ValueError(f"Frame index not found: {params.frame_index}")
        processed = frame.get("processed")
        raw = frame.get("raw")
        if not processed:
            raise ValueError(f"Frame {params.frame_index} has no processed output")

        processed_source = task_path / processed
        if not processed_source.exists():
            raise FileNotFoundError(f"Processed frame not found: {processed_source}")

        selected_processed = f"{params.prefix}_processed.png"
        selected_processed_path = task_path / selected_processed
        if processed_source.resolve() != selected_processed_path.resolve():
            shutil.copyfile(processed_source, selected_processed_path)
            self._write_processed_previews(task_path, selected_processed)

        selected_raw = None
        if raw:
            raw_source = task_path / raw
            if raw_source.exists():
                selected_raw = f"{params.prefix}_raw.png"
                selected_raw_path = task_path / selected_raw
                if raw_source.resolve() != selected_raw_path.resolve():
                    shutil.copyfile(raw_source, selected_raw_path)

        primary_processed = None
        if params.overwrite_primary:
            primary_processed = "processed_0_0.png"
            primary_processed_path = task_path / primary_processed
            if processed_source.resolve() != primary_processed_path.resolve():
                shutil.copyfile(processed_source, primary_processed_path)
            self._write_processed_previews(task_path, primary_processed)

        for item in frames:
            item["selected"] = int(item.get("frame_index", -1)) == params.frame_index
        manifest["selected_frame"] = {
            "candidate_index": params.candidate_index,
            "frame_index": params.frame_index,
            "processed": selected_processed,
            "raw": selected_raw,
            "overwrite_primary": params.overwrite_primary,
            "primary_processed": primary_processed,
            "created_at": datetime.now(UTC).isoformat(),
            "postprocess_metrics": frame.get("postprocess_metrics") or {},
        }
        write_manifest(task_path, manifest)
        return manifest

    def extract_style_lock(self, task_id: str, candidate_index: int, colors: int | None = None) -> dict[str, Any]:
        task_path = resolve_task_dir(self.config.paths.assets_dir, task_id)
        manifest = read_manifest(task_path)
        candidate = find_candidate(manifest, candidate_index)
        image_paths = [
            task_path / frame["processed"]
            for frame in candidate.get("frames", [])
            if frame.get("status") == "done" and frame.get("processed")
        ]
        style_lock = self.style_extractor.extract_from_paths(
            image_paths,
            colors=colors or int(manifest.get("palette_colors") or self.config.output.palette_colors),
        )
        candidate["style_lock_palette_b64"] = style_lock.palette_b64
        candidate["style_lock_histogram_json"] = style_lock.histogram_json
        candidate["favorited"] = True
        write_manifest(task_path, manifest)
        return manifest

    def align_frames(self, params: AlignFramesParams) -> dict[str, Any]:
        if params.source not in {"processed", "raw"}:
            raise ValueError(f"Unsupported align source: {params.source}")
        if params.anchor not in {"alpha_centroid", "bbox_center"}:
            raise ValueError(f"Unsupported align anchor: {params.anchor}")
        if params.axis not in {"x", "y", "xy"}:
            raise ValueError(f"Unsupported align axis: {params.axis}")

        task_path = resolve_task_dir(self.config.paths.assets_dir, params.task_id)
        manifest = read_manifest(task_path)
        candidate = find_candidate(manifest, params.candidate_index)
        for frame in candidate.get("frames", []):
            source_name = frame.get(params.source)
            if not source_name:
                frame["alignment"] = {"status": "skipped", "error": f"Missing {params.source} path"}
                continue
            source_path = task_path / source_name
            if not source_path.exists():
                frame["alignment"] = {"status": "skipped", "error": f"File not found: {source_name}"}
                continue
            image = Image.open(source_path).convert("RGBA")
            result = align_to_canvas_center(image, anchor=params.anchor, axis=params.axis)
            output_name = f"{params.prefix}_{candidate['index']}_{frame['frame_index']}.png"
            save_png(result.image, task_path / output_name)
            measurement = result.measurement
            frame[params.prefix] = output_name
            frame["alignment"] = {
                "status": "done",
                "source": params.source,
                "output": output_name,
                "anchor": params.anchor,
                "axis": params.axis,
                "dx": result.dx,
                "dy": result.dy,
                "target": [round(result.target[0], 3), round(result.target[1], 3)],
                "alpha_centroid": (
                    None
                    if measurement.alpha_centroid is None
                    else [round(measurement.alpha_centroid[0], 3), round(measurement.alpha_centroid[1], 3)]
                ),
                "bbox_center": (
                    None
                    if measurement.bbox_center is None
                    else [round(measurement.bbox_center[0], 3), round(measurement.bbox_center[1], 3)]
                ),
                "bbox": None if measurement.bbox is None else list(measurement.bbox),
            }
        write_manifest(task_path, manifest)
        return manifest

    async def import_sheet(self, params: ImportSheetParams) -> dict[str, Any]:
        if not params.sheet_path.exists():
            raise FileNotFoundError(f"Sheet not found: {params.sheet_path}")
        frame_grid = self.config.frame_layouts.get(params.frame_layout)
        if frame_grid is None:
            built_prompt = self.prompt_builder.build(
                asset_type=params.asset_type,
                frame_layout=params.frame_layout,
                user_prompt=params.user_prompt or "imported sheet",
            )
            frame_grid = built_prompt.frame_grid

        sheet = Image.open(params.sheet_path).convert("RGBA")
        cell_size = (
            params.cell_size
            or self.config.output.default_cell_sizes_by_layout.get(
                params.frame_layout,
                self.config.output.default_cell_size,
            )
        )
        resolved = {
            "frame_layout": params.frame_layout,
            "count": 1,
            "output_size": (
                params.output_size
                if params.output_size is not None
                else (
                    cell_size
                    if params.asset_type == "tile"
                    else self.config.output.default_sizes_by_asset_type.get(
                        params.asset_type,
                        self.config.output.default_size,
                    )
                )
            ),
            "cell_size": cell_size,
            "palette_colors": (
                params.palette_colors
                if params.palette_colors is not None
                else (0 if params.asset_type == "tile" else self.config.output.palette_colors)
            ),
            "canvas_padding": (
                params.canvas_padding
                if params.canvas_padding is not None
                else self.config.output.canvas_padding
            ),
            "model": params.model,
        }
        task_id = params.task_id or str(uuid.uuid4())
        task_path = task_dir(
            self.config.paths.assets_dir,
            task_id,
            category_for_asset_type(params.asset_type),
        )
        manifest = self._new_manifest(
            task_id=task_id,
            params=GenerateParams(
                asset_type=params.asset_type,
                frame_layout=params.frame_layout,
                user_prompt=params.user_prompt or params.sheet_path.name,
                task_id=task_id,
                output_size=resolved["output_size"],
                cell_size=resolved["cell_size"],
                palette_colors=resolved["palette_colors"],
                canvas_padding=resolved["canvas_padding"],
                model=params.model,
            ),
            resolved=resolved,
            enhanced_prompt=params.user_prompt,
            negative_prompt="",
            frame_grid=frame_grid,
            sheet_size=sheet.size,
            style_lock=None,
        )
        manifest["status"] = "running"
        manifest["import_source"] = {
            "sheet_path": str(params.sheet_path),
            "source_task_id": params.source_task_id,
        }
        write_manifest(task_path, manifest)
        generated = GeneratedImage(
            index=0,
            image_bytes=params.sheet_path.read_bytes(),
            mime_type="image/png",
            provider_metadata={
                "backend": "import_sheet",
                "source_path": str(params.sheet_path),
                "source_task_id": params.source_task_id,
            },
        )
        await self._process_generated_images(
            task_path=task_path,
            manifest=manifest,
            images=[generated],
            frame_grid=frame_grid,
            resolved=resolved,
            style_lock=None,
        )
        manifest["status"] = self._aggregate_task_status(manifest)
        manifest["error"] = None if manifest["status"] != "failed" else "All candidates failed"
        write_manifest(task_path, manifest)
        return manifest

    def export_tiles(self, params: ExportTilesParams) -> dict[str, Any]:
        if params.source not in {"processed", "raw"}:
            raise ValueError(f"Unsupported export source: {params.source}")
        if params.output_size <= 0:
            raise ValueError("Export output_size must be greater than 0")
        task_path = resolve_task_dir(self.config.paths.assets_dir, params.task_id)
        manifest = read_manifest(task_path)
        candidate = find_candidate(manifest, params.candidate_index)
        export_key = f"{params.prefix}_{params.output_size}"
        for frame in candidate.get("frames", []):
            source_name = frame.get(params.source)
            if not source_name:
                continue
            source_path = task_path / source_name
            if not source_path.exists():
                continue
            image = Image.open(source_path).convert("RGBA")
            result = self.processor.process(
                image,
                ProcessOptions(
                    output_size=params.output_size,
                    palette_colors=params.palette_colors,
                    canvas_padding=params.canvas_padding,
                    mode=self._process_mode(manifest),
                ),
            )
            output_name = f"{params.prefix}_{params.output_size}_{candidate['index']}_{frame['frame_index']}.png"
            save_png(result.image, task_path / output_name)
            frame[export_key] = output_name
        manifest.setdefault("exports", []).append(
            {
                "candidate_index": params.candidate_index,
                "source": params.source,
                "output_size": params.output_size,
                "palette_colors": params.palette_colors,
                "canvas_padding": params.canvas_padding,
                "prefix": params.prefix,
                "created_at": datetime.now(UTC).isoformat(),
            }
        )
        write_manifest(task_path, manifest)
        return manifest

    def build_walk_4dir(self, params: BuildWalk4DirParams) -> dict[str, Any]:
        source_path = resolve_task_dir(self.config.paths.assets_dir, params.source_task_id)
        source_manifest = read_manifest(source_path)
        source_candidate = find_candidate(source_manifest, params.candidate_index)
        source_grid = tuple(source_manifest.get("frame_grid") or [4, 4])
        if source_grid != (4, 4):
            raise ValueError(f"Source walk task must be a 4x4 sheet, got: {source_grid}")

        output_size = params.output_size or int(source_manifest.get("output_size") or self.config.output.default_size)
        task_id = params.task_id or f"{params.source_task_id}_walk_4dir"
        task_path = task_dir(self.config.paths.assets_dir, task_id, "sprites")
        task_path.mkdir(parents=True, exist_ok=True)

        frames_by_index = {
            int(frame.get("frame_index")): frame
            for frame in source_candidate.get("frames", [])
            if frame.get("processed")
        }
        if len(frames_by_index) < 16:
            raise ValueError(f"Source candidate must contain 16 processed frames, got: {len(frames_by_index)}")

        manifest = self._new_manifest(
            task_id=task_id,
            params=GenerateParams(
                asset_type="character",
                frame_layout="walk_4dir",
                user_prompt=source_manifest.get("user_prompt") or "walk_4dir built from mirrored source",
                task_id=task_id,
                output_size=output_size,
                cell_size=output_size,
                palette_colors=int(source_manifest.get("palette_colors") or 0),
                model="postprocess",
            ),
            resolved={
                "frame_layout": "walk_4dir",
                "count": 1,
                "output_size": output_size,
                "cell_size": output_size,
                "palette_colors": int(source_manifest.get("palette_colors") or 0),
                "canvas_padding": 0,
                "model": "postprocess",
            },
            enhanced_prompt=source_manifest.get("enhanced_prompt") or source_manifest.get("user_prompt") or "",
            negative_prompt=source_manifest.get("negative_prompt") or "",
            frame_grid=(4, 4),
            sheet_size=(output_size * 4, output_size * 4),
            style_lock=None,
        )
        manifest["status"] = "running"
        row_order = params.row_order or self._default_walk_row_order(source_manifest.get("frame_layout"))
        manifest["derived_from"] = {
            "task_id": params.source_task_id,
            "candidate_index": params.candidate_index,
            "operation": "mirror_left_to_right",
            "mirror_source_row": params.mirror_source_row,
            "row_order": list(row_order),
        }
        candidate = {
            "index": 0,
            "sheet": "sheet_0.png",
            "style_lock_palette_b64": None,
            "style_lock_histogram_json": None,
            "provider_metadata": {
                "backend": "postprocess",
                "source_task_id": params.source_task_id,
                "source_candidate_index": params.candidate_index,
            },
            "frames": [],
            "favorited": False,
            "status": "running",
            "error": None,
        }
        manifest["candidates"].append(candidate)

        sheet = Image.new("RGBA", (output_size * 4, output_size * 4), (0, 0, 0, 0))
        for target_row, source_row in enumerate(row_order):
            mirrored = source_row < 0
            actual_source_row = params.mirror_source_row if mirrored else source_row
            for col in range(4):
                source_index = actual_source_row * 4 + col
                source_frame = frames_by_index[source_index]
                source_name = source_frame["processed"]
                image = Image.open(source_path / source_name).convert("RGBA")
                if image.size != (output_size, output_size):
                    image = image.resize((output_size, output_size), Image.Resampling.NEAREST)
                if mirrored:
                    image = image.transpose(Image.Transpose.FLIP_LEFT_RIGHT)

                frame_index = target_row * 4 + col
                processed_name = f"processed_0_{frame_index}.png"
                raw_name = f"frame_0_{frame_index}.png"
                save_png(image, task_path / processed_name)
                save_png(image, task_path / raw_name)
                self._write_processed_previews(task_path, processed_name)
                sheet.alpha_composite(image, (col * output_size, target_row * output_size))
                metrics = self.processor.sprite_quality_metrics(image)
                candidate["frames"].append(
                    {
                        "frame_index": frame_index,
                        "grid_pos": [target_row, col],
                        "raw": raw_name,
                        "processed": processed_name,
                        "bg_removed": True,
                        "split_quality": {"status": "done", "flags": []},
                        "postprocess_metrics": metrics,
                        "status": "done",
                        "error": None,
                        "derived_from": {
                            "frame_index": source_index,
                            "row": actual_source_row,
                            "mirrored": mirrored,
                        },
                    }
                )

        save_png(sheet, task_path / "sheet_0.png")
        candidate["status"] = "done"
        manifest["status"] = "done"
        manifest["sheet_size"] = [sheet.width, sheet.height]
        write_manifest(task_path, manifest)
        return manifest

    def walk_qc(self, params: WalkQcParams) -> dict[str, Any]:
        if params.source not in {"processed", "raw", "aligned"}:
            raise ValueError(f"Unsupported walk QC source: {params.source}")
        if params.scale <= 0:
            raise ValueError("QC scale must be greater than 0")
        if params.fps <= 0:
            raise ValueError("QC fps must be greater than 0")

        task_path = resolve_task_dir(self.config.paths.assets_dir, params.task_id)
        manifest = read_manifest(task_path)
        candidate = find_candidate(manifest, params.candidate_index)
        frame_grid = tuple(manifest.get("frame_grid") or [1, 1])
        frames = sorted(candidate.get("frames", []), key=lambda item: int(item.get("frame_index", 0)))
        frame_data: list[dict[str, Any]] = []
        for frame in frames:
            source_name = frame.get(params.source) or frame.get("processed")
            if not source_name:
                continue
            source_path = task_path / source_name
            if not source_path.exists():
                continue
            image = Image.open(source_path).convert("RGBA")
            frame_data.append(
                {
                    "frame": frame,
                    "image": image,
                    "metrics": self._walk_frame_metrics(image),
                }
            )

        if not frame_data:
            raise ValueError(f"No frames found for walk QC source: {params.source}")

        rows = int(frame_grid[0])
        cols = int(frame_grid[1])
        row_labels = self._walk_row_labels(manifest.get("frame_layout"), rows)
        row_summaries = []
        for row in range(rows):
            row_frames = [
                item
                for item in frame_data
                if int(item["frame"].get("grid_pos", [item["frame"].get("frame_index", 0) // cols, 0])[0]) == row
            ]
            if not row_frames:
                continue
            row_summaries.append(
                self._walk_row_qc(
                    row=row,
                    label=row_labels[row] if row < len(row_labels) else f"row_{row}",
                    frames=row_frames,
                    task_path=task_path,
                    prefix=params.prefix,
                    scale=params.scale,
                    fps=params.fps,
                )
            )
        all_gif = self._write_walk_all_gif(
            frame_data=frame_data,
            rows=rows,
            cols=cols,
            task_path=task_path,
            prefix=params.prefix,
            scale=params.scale,
            fps=params.fps,
        )
        qc = {
            "task_id": params.task_id,
            "candidate_index": params.candidate_index,
            "source": params.source,
            "frame_layout": manifest.get("frame_layout"),
            "frame_grid": list(frame_grid),
            "outputs": {
                "all_directions_gif": all_gif,
                "row_gifs": [item["gif"] for item in row_summaries if item.get("gif")],
            },
            "rows": row_summaries,
            "created_at": datetime.now(UTC).isoformat(),
        }
        (task_path / f"{params.prefix}_metrics.json").write_text(
            json.dumps(qc, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        manifest.setdefault("walk_qc", []).append(qc)
        write_manifest(task_path, manifest)
        return qc

    def _walk_row_qc(
        self,
        row: int,
        label: str,
        frames: list[dict[str, Any]],
        task_path: Path,
        prefix: str,
        scale: int,
        fps: int,
    ) -> dict[str, Any]:
        ordered = sorted(frames, key=lambda item: int(item["frame"].get("frame_index", 0)))
        images = [item["image"] for item in ordered]
        metrics = [item["metrics"] for item in ordered]
        diffs = [
            self._walk_diff_score(images[index], images[(index + 1) % len(images)])
            for index in range(len(images))
        ]
        gif = self._write_walk_gif(
            images=images,
            task_path=task_path,
            output_name=f"{prefix}_{self._safe_name(label)}.gif",
            scale=scale,
            fps=fps,
        )
        bbox_x = [item["bbox_center"][0] for item in metrics if item.get("bbox_center")]
        alpha_x = [item["alpha_centroid"][0] for item in metrics if item.get("alpha_centroid")]
        bottom_y = [item["bottom_y"] for item in metrics if item.get("bottom_y") is not None]
        areas = [int(item["visible_pixels"]) for item in metrics if item.get("visible_pixels") is not None]
        flags = []
        motion_mean = mean(diffs) if diffs else 0.0
        motion_range = (max(diffs) - min(diffs)) if diffs else 0.0
        bbox_x_range = self._number_range(bbox_x)
        alpha_x_range = self._number_range(alpha_x)
        bottom_y_range = self._number_range(bottom_y)
        area_range_pct = self._area_range_pct(areas)
        if bottom_y_range > 1.0:
            flags.append("foot_anchor_drift")
        if bbox_x_range > 3.0 or alpha_x_range > 4.0:
            flags.append("center_drift")
        if motion_mean < 6.0:
            flags.append("low_pose_motion")
        if motion_range > 5.0:
            flags.append("uneven_pose_motion")
        if area_range_pct > 8.0:
            flags.append("scale_or_detail_drift")
        return {
            "row": row,
            "label": label,
            "frame_indices": [int(item["frame"].get("frame_index", 0)) for item in ordered],
            "gif": gif,
            "bbox_x_range": round(bbox_x_range, 3),
            "alpha_x_range": round(alpha_x_range, 3),
            "bottom_y_range": round(bottom_y_range, 3),
            "visible_area_range_pct": round(area_range_pct, 3),
            "sequential_diff_pct": [round(value, 3) for value in diffs],
            "loop_diff_pct": round(diffs[-1], 3) if diffs else 0,
            "motion_mean_pct": round(motion_mean, 3),
            "motion_range_pct": round(motion_range, 3),
            "flags": flags,
        }

    def _write_walk_all_gif(
        self,
        frame_data: list[dict[str, Any]],
        rows: int,
        cols: int,
        task_path: Path,
        prefix: str,
        scale: int,
        fps: int,
    ) -> str:
        if not frame_data:
            return ""
        by_pos: dict[tuple[int, int], Image.Image] = {}
        for item in frame_data:
            frame = item["frame"]
            grid_pos = frame.get("grid_pos") or [int(frame.get("frame_index", 0)) // cols, int(frame.get("frame_index", 0)) % cols]
            by_pos[(int(grid_pos[0]), int(grid_pos[1]))] = item["image"]
        cell_w, cell_h = frame_data[0]["image"].size
        images = []
        for col in range(cols):
            canvas = self._checker_background((cell_w * rows, cell_h))
            for row in range(rows):
                image = by_pos.get((row, col))
                if image is not None:
                    canvas.alpha_composite(image, (row * cell_w, 0))
            images.append(canvas)
        return self._write_walk_gif(
            images=images,
            task_path=task_path,
            output_name=f"{prefix}_all_directions.gif",
            scale=scale,
            fps=fps,
        )

    def _write_walk_gif(
        self,
        images: list[Image.Image],
        task_path: Path,
        output_name: str,
        scale: int,
        fps: int,
    ) -> str:
        if not images:
            return ""
        duration = max(1, round(1000 / fps))
        gif_frames = [self._gif_preview_frame(image, scale=scale) for image in images]
        output_path = task_path / output_name
        gif_frames[0].save(
            output_path,
            save_all=True,
            append_images=gif_frames[1:],
            duration=duration,
            loop=0,
            disposal=2,
        )
        return output_name

    @staticmethod
    def _gif_preview_frame(image: Image.Image, scale: int) -> Image.Image:
        rgba = image.convert("RGBA")
        frame = CoreGenerationService._checker_background(rgba.size)
        frame.alpha_composite(rgba)
        if scale != 1:
            frame = frame.resize((frame.width * scale, frame.height * scale), Image.Resampling.NEAREST)
        return frame.convert("P", palette=Image.Palette.ADAPTIVE)

    @staticmethod
    def _walk_frame_metrics(image: Image.Image) -> dict[str, Any]:
        rgba = image.convert("RGBA")
        alpha = rgba.getchannel("A")
        bbox = alpha.point(lambda value: 255 if value > 8 else 0).getbbox()
        visible_pixels = 0
        total = 0
        sx = 0
        sy = 0
        pixels = alpha.load()
        for y in range(rgba.height):
            for x in range(rgba.width):
                value = pixels[x, y]
                if value > 8:
                    visible_pixels += 1
                if value:
                    total += value
                    sx += x * value
                    sy += y * value
        bbox_center = None
        bottom_y = None
        if bbox:
            bbox_center = ((bbox[0] + bbox[2] - 1) / 2.0, (bbox[1] + bbox[3] - 1) / 2.0)
            bottom_y = bbox[3] - 1
        alpha_centroid = (sx / total, sy / total) if total else None
        return {
            "bbox": None if bbox is None else list(bbox),
            "bbox_center": None if bbox_center is None else [bbox_center[0], bbox_center[1]],
            "alpha_centroid": None if alpha_centroid is None else [alpha_centroid[0], alpha_centroid[1]],
            "bottom_y": bottom_y,
            "visible_pixels": visible_pixels,
        }

    @staticmethod
    def _walk_diff_score(first: Image.Image, second: Image.Image) -> float:
        bg = Image.new("RGBA", first.size, (128, 128, 128, 255))
        first_composite = bg.copy()
        second_composite = bg.copy()
        first_composite.alpha_composite(first.convert("RGBA"))
        second_composite.alpha_composite(second.convert("RGBA"))
        diff = ImageChops.difference(first_composite.convert("RGB"), second_composite.convert("RGB")).convert("L")
        return sum(diff.getdata()) / (diff.width * diff.height * 255) * 100

    @staticmethod
    def _walk_row_labels(frame_layout: str | None, rows: int) -> list[str]:
        if frame_layout in {"walk_4dir", "walk_3dir_4x4"}:
            labels = ["down_front", "left", "right" if frame_layout == "walk_4dir" else "up_back", "up_back" if frame_layout == "walk_4dir" else "helper"]
        elif frame_layout == "walk_3dir_3x4":
            labels = ["down_front", "up_back", "left"]
        else:
            labels = [f"row_{index}" for index in range(rows)]
        if len(labels) < rows:
            labels.extend(f"row_{index}" for index in range(len(labels), rows))
        return labels[:rows]

    @staticmethod
    def _default_walk_row_order(frame_layout: str | None) -> tuple[int, int, int, int]:
        if frame_layout == "walk_3dir_4x4":
            return (0, 1, -1, 2)
        return (0, 1, -1, 3)

    @staticmethod
    def _safe_name(value: str) -> str:
        return "".join(char if char.isalnum() or char in {"-", "_"} else "_" for char in value).strip("_") or "row"

    @staticmethod
    def _number_range(values: list[float | int]) -> float:
        return float(max(values) - min(values)) if values else 0.0

    @staticmethod
    def _area_range_pct(values: list[int]) -> float:
        return (max(values) - min(values)) / mean(values) * 100 if values else 0.0

    async def _process_generated_images(
        self,
        task_path: Path,
        manifest: dict[str, Any],
        images: list[GeneratedImage],
        frame_grid: tuple[int, int],
        resolved: dict[str, Any],
        style_lock: dict[str, Any] | None,
    ) -> None:
        for generated in images:
            candidate = {
                "index": generated.index,
                "sheet": f"sheet_{generated.index}.png",
                "style_lock_palette_b64": None,
                "style_lock_histogram_json": None,
                "provider_metadata": generated.provider_metadata,
                "frames": [],
                "favorited": False,
                "status": "running",
                "error": None,
            }
            manifest["candidates"].append(candidate)
            sheet_path = task_path / candidate["sheet"]
            try:
                sheet = Image.open(io.BytesIO(generated.image_bytes)).convert("RGBA")
                save_png(sheet, sheet_path)
                manifest["sheet_size"] = list(sheet.size)
                frames = self.splitter.split(
                    sheet,
                    frame_grid,
                    quality_profile=self._quality_profile(manifest),
                )
                for frame in frames:
                    raw_name = f"frame_{generated.index}_{frame.frame_index}.png"
                    processed_name = f"processed_{generated.index}_{frame.frame_index}.png"
                    raw_path = task_path / raw_name
                    processed_path = task_path / processed_name
                    save_png(frame.image, raw_path)
                    result = await asyncio.to_thread(
                        self.processor.process,
                        frame.image,
                        ProcessOptions(
                            output_size=resolved["output_size"],
                            palette_colors=resolved["palette_colors"],
                            canvas_padding=resolved["canvas_padding"],
                            style_lock_palette_b64=(style_lock or {}).get("palette_b64"),
                            mode=self._process_mode(manifest),
                        ),
                    )
                    save_png(result.image, processed_path)
                    self._write_processed_previews(task_path, processed_name)
                    frame_status = "failed" if result.status == "failed" or frame.split_quality.status == "failed" else "done"
                    candidate["frames"].append(
                        {
                            "frame_index": frame.frame_index,
                            "grid_pos": list(frame.grid_pos),
                            "raw": raw_name,
                            "processed": processed_name,
                            "bg_removed": result.bg_removed,
                            "split_quality": frame.split_quality.to_dict(),
                            "postprocess_metrics": result.metrics or {},
                            "status": frame_status,
                            "error": result.error,
                        }
                    )
                candidate["status"] = self._aggregate_candidate_status(candidate)
            except Exception as exc:
                candidate["status"] = "failed"
                candidate["error"] = str(exc)
            write_manifest(task_path, manifest)

    def _resolve_params(self, params: GenerateParams) -> dict[str, Any]:
        frame_layout = params.frame_layout or self.config.generation.default_frame_layout
        default_count = self.config.generation.default_counts_by_layout.get(
            frame_layout,
            self.config.generation.default_count,
        )
        cell_size = (
            params.cell_size
            or self.config.output.default_cell_sizes_by_layout.get(
                frame_layout,
                self.config.output.default_cell_size,
            )
        )
        default_size = (
            cell_size
            if params.asset_type == "tile"
            else self.config.output.default_sizes_by_asset_type.get(
                params.asset_type,
                self.config.output.default_size,
            )
        )
        return {
            "frame_layout": frame_layout,
            "count": params.count or default_count,
            "output_size": params.output_size if params.output_size is not None else default_size,
            "cell_size": cell_size,
            "palette_colors": (
                params.palette_colors
                if params.palette_colors is not None
                else (0 if params.asset_type == "tile" else self.config.output.palette_colors)
            ),
            "canvas_padding": (
                params.canvas_padding
                if params.canvas_padding is not None
                else self.config.output.canvas_padding
            ),
            "model": params.model or self.config.generator.model,
        }

    def _load_style_lock(self, task_id: str | None, candidate_index: int | None) -> dict[str, Any] | None:
        if not task_id or candidate_index is None:
            return None
        task_path = resolve_task_dir(self.config.paths.assets_dir, task_id)
        manifest = read_manifest(task_path)
        candidate = find_candidate(manifest, candidate_index)
        palette_b64 = candidate.get("style_lock_palette_b64")
        if not palette_b64:
            manifest = self.extract_style_lock(task_id, candidate_index)
            candidate = find_candidate(manifest, candidate_index)
            palette_b64 = candidate.get("style_lock_palette_b64")
        if not palette_b64:
            return None
        reference_images: list[bytes] = []
        for frame in candidate.get("frames", []):
            processed = frame.get("processed")
            if frame.get("status") == "done" and processed:
                reference_path = task_path / processed
                if reference_path.exists():
                    reference_images.append(reference_path.read_bytes())
                    break
        return {
            "source_task_id": task_id,
            "source_candidate_index": candidate_index,
            "palette_b64": palette_b64,
            "histogram_json": candidate.get("style_lock_histogram_json"),
            "reference_images": reference_images,
        }

    @staticmethod
    def _load_reference_images(paths: tuple[Path, ...]) -> list[tuple[Path, int, bytes]]:
        images: list[tuple[Path, int, bytes]] = []
        for path in paths:
            if not path.exists():
                raise FileNotFoundError(f"Reference image not found: {path}")
            payload = path.read_bytes()
            images.append((path, len(payload), payload))
        return images

    def _new_manifest(
        self,
        task_id: str,
        params: GenerateParams,
        resolved: dict[str, Any],
        enhanced_prompt: str,
        negative_prompt: str,
        frame_grid: tuple[int, int],
        sheet_size: tuple[int, int],
        style_lock: dict[str, Any] | None,
    ) -> dict[str, Any]:
        now = datetime.now(UTC).isoformat()
        return {
            "task_id": task_id,
            "created_at": now,
            "asset_type": params.asset_type,
            "frame_layout": resolved["frame_layout"],
            "frame_grid": list(frame_grid),
            "cell_size": resolved["cell_size"],
            "sheet_size": list(sheet_size),
            "style_lock_source_task": (style_lock or {}).get("source_task_id"),
            "style_lock_source_candidate_index": (style_lock or {}).get("source_candidate_index"),
            "style_lock_applied": bool(style_lock),
            "reference_applied": False,
            "user_prompt": params.user_prompt,
            "enhanced_prompt": enhanced_prompt,
            "negative_prompt": negative_prompt,
            "model": resolved["model"] if (params.backend_override or self.config.generator.backend) != "mock" else "mock",
            "output_size": resolved["output_size"],
            "palette_colors": resolved["palette_colors"],
            "requested_count": resolved["count"],
            "status": "pending",
            "error": None,
            "candidates": [],
        }

    @staticmethod
    def _aggregate_candidate_status(candidate: dict[str, Any]) -> str:
        frames = candidate.get("frames", [])
        if not frames:
            return "failed"
        done = sum(1 for frame in frames if frame.get("status") == "done")
        if done == len(frames):
            return "done"
        if done:
            return "partial_failed"
        return "failed"

    @staticmethod
    def _aggregate_task_status(manifest: dict[str, Any]) -> str:
        candidates = manifest.get("candidates", [])
        if not candidates:
            return "failed"
        statuses = [candidate.get("status") for candidate in candidates]
        requested_count = int(manifest.get("requested_count") or len(candidates))
        missing_candidates = len(candidates) < requested_count
        if all(status == "done" for status in statuses) and not missing_candidates:
            return "done"
        if any(status in {"done", "partial_failed"} for status in statuses):
            return "partial_failed"
        return "failed"

    @staticmethod
    def _quality_profile(manifest: dict[str, Any]) -> str:
        return "tile" if manifest.get("asset_type") == "tile" else "sprite"

    @staticmethod
    def _process_mode(manifest: dict[str, Any]) -> str:
        return "tile" if manifest.get("asset_type") == "tile" else "sprite"

    @staticmethod
    def _write_processed_previews(task_path: Path, processed_name: str) -> None:
        processed_path = task_path / processed_name
        if not processed_path.exists():
            return
        image = Image.open(processed_path).convert("RGBA")
        stem = processed_path.stem

        checker = CoreGenerationService._checker_background(image.size)
        checker.alpha_composite(image)
        save_png(checker, task_path / f"{stem}_checker_preview.png")

        alpha = image.getchannel("A")
        alpha_rgba = Image.merge(
            "RGBA",
            (
                alpha,
                alpha,
                alpha,
                Image.new("L", image.size, 255),
            ),
        )
        save_png(alpha_rgba, task_path / f"{stem}_alpha_mask.png")

    @staticmethod
    def _checker_background(size: tuple[int, int]) -> Image.Image:
        width, height = size
        cell = max(4, min(width, height) // 8)
        image = Image.new("RGBA", size, (230, 230, 230, 255))
        draw = ImageDraw.Draw(image)
        for y in range(0, height, cell):
            for x in range(0, width, cell):
                if ((x // cell) + (y // cell)) % 2:
                    draw.rectangle(
                        [x, y, min(width - 1, x + cell - 1), min(height - 1, y + cell - 1)],
                        fill=(178, 178, 178, 255),
                    )
        return image
