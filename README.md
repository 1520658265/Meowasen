# Meowasen

Phase 1 implements the local core generation pipeline:

```text
prompt -> provider -> raw sheet -> split frames -> postprocess -> PNG + meta.json
```

Project-wide art rule: every final asset is for an RPG game. Prompts,
post-processing, terrain packs, scene tilesets, sprites, and props must default
to RPG pixel-art readability: limited cohesive palettes, clear silhouettes,
low-noise symbolic texture, and crisp tile-scale details. Photorealistic
materials, raw texture scans, 3D renders, and generic texture samples are only
allowed as temporary references, not as final assets.

## Setup

```bash
pip install -r requirements.txt
copy .env.example .env
```

For real image generation, prefer the stable ImageHub bridge script and set `GPT_API_KEY` in `.env`.
The configured backend in `config.yaml` is still available for provider debugging, but the direct
OpenAI-compatible image endpoint can be interrupted by large chunked responses on some networks.

For local pipeline verification without network or API keys, use `--backend mock`.

Background cleanup uses the fast edge-connected remover by default. `rembg` is optional for higher quality refinement; keep `U2NET_HOME=D:\Soft\Rembg` and `MEOWASEN_REMBG_MODEL=isnet-anime` in `.env`, then set `MEOWASEN_ENABLE_REMBG=1` only when you want to run the local model.

Character, prop, and icon prompts automatically request a flat solid
chroma-key background with exact `RGB(255,0,255)` / `#FF00FF`. The image model
is not expected to produce real alpha transparency. Post-processing removes
the edge-connected chroma-key background and writes transparent `processed_*.png`
outputs.

## Commands

Dry-run prompt assembly:

```bash
python -B -m backend.cli generate --asset-type character --frame-layout walk_cycle --prompt "blue cloak knight" --backend mock --dry-run
```

Generate a single sprite:

```bash
python -B -m backend.cli generate --asset-type character --frame-layout single --prompt "blue cloak knight" --backend mock
```

Generate four alternatives for the same RPG character in one model call:

```bash
python -B -m backend.cli generate --asset-type character --frame-layout character_candidates_2x2 --prompt "blue cloak knight" --count 1
```

This writes one `sheet_0.png`, splits it into four processed candidate frames,
and records cleanup metrics for each frame in `meta.json`. After manual review,
promote one frame to the logical final output:

```bash
python -B -m backend.cli select-frame --task-id <task_id> --candidate-index 0 --frame-index 2 --overwrite-primary
```

Use `--task-id <fixed_id>` on `generate` or `import-sheet` when a test should
reuse one task directory instead of creating a new UUID.

For four-direction RPG walk animation, prefer the optimized three-direction
API route: ask the model for a square `walk_3dir_4x4` source sheet, then mirror
the left-facing row into the right-facing row with code and run automatic walk
QC. This keeps the paid API call at stable `1024x1024` while reducing direction
inconsistency.

```bash
python -B tools/gpt_image_generate.py --asset-type character --frame-layout walk_3dir_4x4 --prompt-file <prompt.txt> --task-id <raw_task> --size 1024x1024 --count 1
python -B -m backend.cli import-sheet --sheet-path assets/tasks/imports/<raw_task>/gpt_image_sheet_0.png --asset-type character --frame-layout walk_3dir_4x4 --prompt-file <prompt.txt> --task-id <source_task> --output-size 128 --palette-colors 0 --source-task-id <raw_task>
python -B -m backend.cli build-walk-4dir --source-task-id <source_task> --task-id <walk_task> --output-size 128
python -B -m backend.cli walk-qc --task-id <walk_task> --source processed --prefix qc_auto --scale 4 --fps 6
```

For pose-guided Gemini tests, use `tools/gemini_image_generate.py` with repeated
`--reference-image` arguments. This script calls Gemini native `generateContent`
and can pass both the approved static character and a local pose guide sheet.

For the stable ImageHub path, generate one 2x2 candidate sheet and import it
into a fixed sprite task:

```bash
python -B tools/imagehub_generate.py --asset-type character --frame-layout character_candidates_2x2 --prompt "blue cloak knight" --task-id character_candidates_raw
python -B -m backend.cli import-sheet --sheet-path assets/tasks/imports/character_candidates_raw/imagehub_sheet_0_0.jpg --asset-type character --frame-layout character_candidates_2x2 --task-id character_candidates_review --output-size 128 --palette-colors 0
```

Generate a 2x2 sprite pack:

```bash
python -B -m backend.cli generate --asset-type prop --frame-layout sprite_pack --prompt "treasure chest potion key gold coin" --backend mock --count 1
```

Generate through the stable ImageHub bridge:

```bash
python -B tools/imagehub_generate.py
```

By default this creates a continuous RPG `terrain_source_8x8` sheet: one
1024x1024 terrain image planned for an invisible 8x8 crop grid. Import it with
`import-sheet` to preserve 128x128 source tiles, then export smaller runtime
tiles only when needed:

```bash
python -B -m backend.cli import-sheet --sheet-path assets/<imagehub_task>/imagehub_sheet_0_0.png --asset-type tile --frame-layout terrain_source_8x8
python -B -m backend.cli export-tiles --task-id <import_task> --candidate-index 0 --output-size 64 --palette-colors 0
```

For terrain packs, the more stable route is to generate an RPG terrain source
kit and let code expand it into autotile pieces. A real ImageHub kit can be
expanded with:

```bash
python -B -m backend.cli terrain-material-build --kit-path assets/<imagehub_task>/imagehub_sheet_0_0.png --tile-size 128 --grid-size 4 --animation-frames 4 --mode blob47 --variants 3
```

For a campus grass-to-path terrain set, use the campus theme so the local
post-processing keeps grass, concrete, dirt, and curb colors instead of the
volcanic lava palette:

```bash
python -B -m backend.cli terrain-material-build --kit-path assets/<imagehub_task>/imagehub_sheet_0_0.png --tile-size 128 --grid-size 4 --animation-frames 2 --mode blob47 --variants 3 --theme campus
```

Campus kits can be expanded into several terrain families from the same source
sheet without another model call:

```bash
python -B -m backend.cli terrain-material-build --kit-path assets/<imagehub_task>/imagehub_sheet_0_0.png --task-id campus_path_blob47 --tile-size 128 --grid-size 4 --animation-frames 1 --mode blob47 --variants 3 --theme campus --material-preset campus_path
python -B -m backend.cli terrain-material-build --kit-path assets/<imagehub_task>/imagehub_sheet_0_0.png --task-id campus_brick_blob47 --tile-size 128 --grid-size 4 --animation-frames 1 --mode blob47 --variants 3 --theme campus --material-preset campus_brick
python -B -m backend.cli terrain-material-build --kit-path assets/<imagehub_task>/imagehub_sheet_0_0.png --task-id campus_track_blob47 --tile-size 128 --grid-size 4 --animation-frames 1 --mode blob47 --variants 3 --theme campus --material-preset campus_track
python -B -m backend.cli terrain-material-build --kit-path assets/<imagehub_task>/imagehub_sheet_0_0.png --task-id campus_sand_blob47 --tile-size 128 --grid-size 4 --animation-frames 1 --mode blob47 --variants 3 --theme campus --material-preset campus_sand
```

Reusable material kits and terrain sets should be registered in the public
library under `assets/library`. Scene-specific builders should query this
library before spending another image generation call. The fixed contract is
`theme + tags + material/terrain kind`; the actual scene semantics remain
dynamic and are expressed as tags such as `campus track`, `volcano lava`, or
`town cobblestone`.

```bash
python -B -m backend.cli library-register-material --material-id campus_ground_001 --name "Campus Ground Material Kit" --kit-path assets/campus_material_kit_001/imagehub_sheet_0_0.png --theme campus --tags campus grass concrete brick track sand --grid-size 4 --description "Shared campus ground material kit generated once through ImageHub."

python -B -m backend.cli library-register-terrain --terrain-id campus_path_blob47 --name "Campus Concrete Path Blob47" --task-id campus_path_blob47_v2 --material-id campus_ground_001 --theme campus --material-preset campus_path --tags campus path concrete grass
python -B -m backend.cli library-register-terrain --terrain-id campus_brick_blob47 --name "Campus Brick Plaza Blob47" --task-id campus_brick_blob47_v1 --material-id campus_ground_001 --theme campus --material-preset campus_brick --tags campus brick plaza grass
python -B -m backend.cli library-register-terrain --terrain-id campus_track_blob47 --name "Campus Track Rubber Blob47" --task-id campus_track_blob47_v1 --material-id campus_ground_001 --theme campus --material-preset campus_track --tags campus track rubber grass
python -B -m backend.cli library-register-terrain --terrain-id campus_sand_blob47 --name "Campus Sand Ground Blob47" --task-id campus_sand_blob47_v1 --material-id campus_ground_001 --theme campus --material-preset campus_sand --tags campus sand playground grass
```

For manual browsing:

```bash
python -B -m backend.cli library-list --theme campus
```

For dynamic scene planning, prefer `library-find`. If it returns a useful
match, reuse the returned `paths.sheet` or `paths.variant_sheet`; if it returns
no match, generate a new material kit, expand it, then register both results.

```bash
python -B -m backend.cli library-find --kind terrain --theme campus --tags campus track grass
```

The main scene-facing command is `scene-terrain-build`. It turns a user scene
description into terrain requirements, resolves them from the public library,
and writes a final scene tileset plus a semantic assembly preview:

```bash
python -B -m backend.cli scene-terrain-build --task-id campus_scene_tileset_v1 --theme campus --tile-size 64 --art-style rpg --scene "校园场景：有椭圆跑道和草地操场，教学楼前有水泥道路，入口附近有砖地广场，旁边有沙地活动区"
```

The public library may keep higher-resolution RPG source tiles, such as 128x128
terrain sheets, while `scene-terrain-build --tile-size 64` emits RPG-runtime
tilesets at 64x64 per tile. The default `--art-style rpg` pass reduces noisy
material texture, snaps colors toward a small RPG-oriented palette, and adds
simple symbolic detail. Use `--art-style source` only to inspect library inputs
while debugging; it is not the final project style.

Outputs are written to `assets/<task_id>/`:

- `scene_terrain_plan.json`: inferred terrain requirements, library matches,
  section offsets, used tile ids, and missing requirements.
- `scene_tileset.png`: all resolved terrain families stacked into one final
  tileset sheet.
- `scene_tileset_variants.png`: optional stacked variant sheet when source
  terrain families have variants.
- `scene_tileset_used.png`: compact sheet containing only the tiles used by the
  generated semantic preview.
- `scene_preview.png`: a human-checkable preview assembled from the same tiles.

Default scene profiles live in `backend/terrain/scene_profiles.json`. Keep the
code path generic and add new scene/domain knowledge there or pass
`--profile-path <json>` for project-specific extension data. If a requirement
cannot be resolved from `assets/library`, the command finishes with
`status: needs_generation`; generate/register the missing material or terrain
family, then run the same scene command again.

Scene terrain requirements support two terrain kinds:

- `blob`: arbitrary organic terrain boundaries, such as sand patches, lava
  pools, plazas, dirt areas, ponds, or irregular paths. These reuse blob47
  terrain families from the public library.
- `composite`: semantic geometry with a fixed real-world structure, such as an
  oval running track. These are generated as a larger tile grid from the matched
  material family, then sliced into reusable tiles. For example, the campus
  `track` feature emits a `6x10` top-down orthographic oval-track tile section
  with straightaways, half-circle turns, an infield, and lane lines.

For a no-network mechanism check:

```bash
python -B -m backend.cli terrain-material-demo --tile-size 64 --animation-frames 4 --mode blob47
```

`terrain-material-build` accepts `--grid-size 2`, `3`, or `4`. The generated
material samples are cleaned, made more tileable, color-normalized by material
family, then expanded with rule-based masks. `--mode blob47` emits a 6x8 atlas
with the 47 valid blob autotile states plus one neutral fill tile; use
`--mode autotile16` only for the older compact validation path. `--variants`
adds deterministic same-role tile variants and writes `sheet_variants_0.png`;
generated previews use those variants to reduce visible repetition. Use
`terrain-preview --shape irregular` for a less symmetric manual check shape.

Dry-run the ImageHub request without spending a generation:

```bash
python -B tools/imagehub_generate.py --dry-run
```

Generate through the configured API backend directly, mainly for provider debugging:

```bash
python -B -m backend.cli generate --asset-type character --frame-layout single --prompt "blue cloak knight, top-down RPG pixel art" --count 1 --output-size 64 --palette-colors 16
```

Verify multi-asset splitting through the configured API backend:

```bash
python -B -m backend.cli generate --asset-type prop --frame-layout sprite_pack --prompt "treasure chest, red potion, brass key, gold coin, same RPG pixel art icon set" --count 1 --output-size 64 --palette-colors 16
```

Extract a style lock:

```bash
python -B -m backend.cli extract-style-lock --task-id <task_id> --candidate-index 0
```

Reprocess an existing candidate:

```bash
python -B -m backend.cli reprocess --task-id <task_id> --candidate-index 0 --output-size 128 --palette-colors 32 --use-style-lock
```

Generate with an existing style lock:

```bash
python -B -m backend.cli generate --asset-type prop --frame-layout sprite_pack --prompt "style locked item set" --count 1 --style-lock-task <task_id> --style-lock-candidate 0
```

Refine an existing candidate with local `rembg`:

```bash
set MEOWASEN_ENABLE_REMBG=1
python -B -m backend.cli reprocess --task-id <task_id> --candidate-index 0 --output-size 128 --palette-colors 32
```

Generated files are written under `assets/tasks/<category>/<task_id>/`.
Current categories are `imports`, `tiles`, `terrain`, `sprites`, `props`,
`icons`, `scenes`, and `generated`. The reusable public library remains under
`assets/library/`.

Default processed sizes are asset-aware: `character` uses `128x128`, while `prop`, `icon`, and `tile` use `64x64`. Use `--output-size` to override this per run.
