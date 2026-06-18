from __future__ import annotations

import asyncio
import io
import shutil
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

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
            reference_images=(style_lock or {}).get("reference_images", []),
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
