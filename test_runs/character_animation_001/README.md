# Character Animation Test 001

Goal: validate API-based RPG character animation generation after the static
cat warrior test passed.

Reference static test:

- `../character_static_001/assets/tasks/sprites/cat_warrior_candidates_gpt_wifi_001/selected_preview_384.png`
- The current GPT image endpoint is text-to-image only in this project, so the
  reference is used as manual identity guidance unless a future image-reference
  API is added.

Baseline route already tested:

Use the direct GPT image script for the paid model call, then import the raw
sheet into the local post-processing pipeline. This keeps the API call path the
same as the static character test and writes all artifacts under this test
folder.

```bash
python -B tools/gpt_image_generate.py --config test_runs/character_animation_001/config.yaml --asset-type character --frame-layout walk_4dir --prompt-file test_runs/character_animation_001/cat_warrior_walk_prompt.txt --task-id cat_warrior_walk_4dir_001_raw --size 1024x1024 --count 1

python -B -m backend.cli --config test_runs/character_animation_001/config.yaml import-sheet --sheet-path test_runs/character_animation_001/assets/tasks/imports/cat_warrior_walk_4dir_001_raw/gpt_image_sheet_0.png --asset-type character --frame-layout walk_4dir --prompt-file test_runs/character_animation_001/cat_warrior_walk_prompt.txt --task-id cat_warrior_walk_4dir_001 --output-size 128 --palette-colors 0 --source-task-id cat_warrior_walk_4dir_001_raw
```

Dry-run the GPT request without spending a generation:

```bash
python -B tools/gpt_image_generate.py --config test_runs/character_animation_001/config.yaml --asset-type character --frame-layout walk_4dir --prompt-file test_runs/character_animation_001/cat_warrior_walk_prompt.txt --task-id cat_warrior_walk_4dir_001_raw --size 1024x1024 --count 1 --dry-run
```

Current preferred route for the next paid run:

Generate a square 4x4 source sheet with only three authored directions. The
right-facing direction is derived by mirroring the left-facing row in code.
This keeps the paid image endpoint on predictable `1024x1024` output while
reducing direction inconsistency.

```bash
python -B tools/gpt_image_generate.py --config test_runs/character_animation_001/config.yaml --asset-type character --frame-layout walk_3dir_4x4 --prompt-file test_runs/character_animation_001/cat_warrior_walk_3dir_prompt.txt --task-id cat_warrior_walk_3dir_4x4_001_raw --size 1024x1024 --count 1

python -B -m backend.cli --config test_runs/character_animation_001/config.yaml import-sheet --sheet-path test_runs/character_animation_001/assets/tasks/imports/cat_warrior_walk_3dir_4x4_001_raw/gpt_image_sheet_0.png --asset-type character --frame-layout walk_3dir_4x4 --prompt-file test_runs/character_animation_001/cat_warrior_walk_3dir_prompt.txt --task-id cat_warrior_walk_3dir_4x4_001 --output-size 128 --palette-colors 0 --source-task-id cat_warrior_walk_3dir_4x4_001_raw

python -B -m backend.cli --config test_runs/character_animation_001/config.yaml build-walk-4dir --source-task-id cat_warrior_walk_3dir_4x4_001 --task-id cat_warrior_walk_4dir_mirrored_001 --candidate-index 0 --output-size 128

python -B -m backend.cli --config test_runs/character_animation_001/config.yaml walk-qc --task-id cat_warrior_walk_4dir_mirrored_001 --candidate-index 0 --source processed --prefix qc_auto --scale 4 --fps 6
```

Pose-guide preparation:

```bash
python -B tools/make_walk_pose_guide.py --output-dir test_runs/character_animation_001/pose_guides --tile-size 256 --prefix pose_walk_3dir_4x4
```

This writes:

- `pose_guides/pose_walk_3dir_4x4.png`
- `pose_guides/pose_walk_3dir_4x4_overlay_preview.png`
- `pose_guides/pose_walk_3dir_4x4_spec.json`

Pose-guided ImageHub dry-run/request shape:

```bash
python -B tools/imagehub_generate.py --config test_runs/character_animation_001/config.yaml --asset-type character --frame-layout walk_3dir_4x4 --prompt-file test_runs/character_animation_001/cat_warrior_walk_pose_guided_prompt.txt --task-id cat_warrior_walk_pose_guided_001_raw --size 1024x1024 --count 1 --reference-image test_runs/character_static_001/assets/tasks/sprites/cat_warrior_candidates_gpt_wifi_001/selected_preview_384.png --reference-image test_runs/character_animation_001/pose_guides/pose_walk_3dir_4x4.png --dry-run
```

Pose-guided Gemini native route:

```bash
python -B tools/gemini_image_generate.py --config test_runs/character_animation_001/config.yaml --asset-type character --frame-layout walk_3dir_4x4 --prompt-file test_runs/character_animation_001/cat_warrior_walk_pose_guided_prompt.txt --task-id cat_warrior_walk_gemini_pose_guided_001_raw --model gemini-3.1-flash-image-preview --size 1024x1024 --count 1 --reference-image test_runs/character_static_001/assets/tasks/sprites/cat_warrior_candidates_gpt_wifi_001/selected_preview_384.png --reference-image test_runs/character_animation_001/pose_guides/pose_walk_3dir_4x4.png

python -B -m backend.cli --config test_runs/character_animation_001/config.yaml import-sheet --sheet-path test_runs/character_animation_001/assets/tasks/imports/cat_warrior_walk_gemini_pose_guided_001_raw/gemini_image_sheet_0.png --asset-type character --frame-layout walk_3dir_4x4 --prompt-file test_runs/character_animation_001/cat_warrior_walk_pose_guided_prompt.txt --task-id cat_warrior_walk_gemini_pose_guided_001 --output-size 128 --palette-colors 0 --source-task-id cat_warrior_walk_gemini_pose_guided_001_raw

python -B -m backend.cli --config test_runs/character_animation_001/config.yaml build-walk-4dir --source-task-id cat_warrior_walk_gemini_pose_guided_001 --task-id cat_warrior_walk_gemini_pose_guided_4dir_001 --candidate-index 0 --output-size 128

python -B -m backend.cli --config test_runs/character_animation_001/config.yaml walk-qc --task-id cat_warrior_walk_gemini_pose_guided_4dir_001 --candidate-index 0 --source processed --prefix qc_auto --scale 4 --fps 6
```

## Gemini Pose-Guided Attempt 001

Status: request payload prepared, but the real call did not reach Gemini.

Attempted command:

- `tools/gemini_image_generate.py`
- model: `gemini-3.1-flash-image-preview`
- references: approved static cat warrior + `pose_walk_3dir_4x4.png`
- task id: `cat_warrior_walk_gemini_pose_guided_001_raw`

Result:

- Both normal trust-env mode and `--trust-env false` failed with TCP connect
  timeout to `generativelanguage.googleapis.com`.
- No image was generated, so there is no sheet to import.
- This is a network reachability issue before the model/API response layer, not
  a prompt, payload, or reference-image rejection.

Retained debug files:

- `assets/tasks/imports/cat_warrior_walk_gemini_pose_guided_001_raw/gemini_request_debug.json`
- `assets/tasks/imports/cat_warrior_walk_gemini_pose_guided_001_raw/prompt.txt`

Review checklist:

- Each frame is a top-down/overworld RPG sprite, not portrait or side-scroller art.
- One complete cat warrior per cell, no merged bodies or cropped spear.
- Identity and spear silhouette remain consistent.
- Feet anchor and body center do not drift across the cycle.
- Magenta background is removed cleanly in processed outputs.
- Use `tools/animation_preview.html` for manual frame playback.

## Real Run 001

Status: generated and imported successfully.

Raw API output:

- `assets/tasks/imports/cat_warrior_walk_4dir_001_raw/gpt_image_sheet_0.png`

Processed task:

- `assets/tasks/sprites/cat_warrior_walk_4dir_001`
- `sheet_0.png`
- `processed_0_0.png` through `processed_0_15.png`
- `processed_0_*_checker_preview.png`
- `processed_0_*_alpha_mask.png`

QC summary:

- 16/16 frames imported successfully.
- Exact chroma-key residual pixels: `0` for every frame.
- Edge visible pixels: `0` for every frame.
- Split quality warning: every frame has `subject_overflow`, meaning the
  character occupies too much of the 128x128 runtime canvas. For the next paid
  generation, ask for a smaller sprite with more safe margin inside each cell.
- `walk-qc` was added after this run. It flags the current up/back row as
  `low_pose_motion`, matching the visual issue that the animation is stable but
  not very walk-like.

## Real Run 002

Status: optimized three-direction route generated, imported, mirrored, and QC'd
successfully.

Raw API output:

- `assets/tasks/imports/cat_warrior_walk_3dir_4x4_001_raw/gpt_image_sheet_0.png`

Three-direction processed source:

- `assets/tasks/sprites/cat_warrior_walk_3dir_4x4_001`

Final mirrored four-direction task:

- `assets/tasks/sprites/cat_warrior_walk_4dir_mirrored_001`
- `sheet_0.png`
- `processed_0_0.png` through `processed_0_15.png`
- `qc_auto_all_directions.gif`
- `qc_auto_down_front.gif`
- `qc_auto_left.gif`
- `qc_auto_right.gif`
- `qc_auto_up_back.gif`
- `qc_auto_metrics.json`

QC summary:

- 16/16 final frames built successfully.
- Exact chroma-key residual pixels: `0` for every source frame.
- Edge visible pixels: `0` for every source frame.
- Right-facing row is now deterministic because it is mirrored from the
  left-facing row.
- Remaining issue: left/right rows are flagged `low_pose_motion`, so the side
  walk still reads like a slide rather than a strong walk cycle.
- Down/front and up/back rows have stronger motion, but QC flags `center_drift`.
- The image model still ignored the requested smaller 70% occupancy; source
  import reported `subject_overflow` on all frames.

## Real Run 003

Status: pose-guided Gemini route via the bobdong relay generated, imported,
mirrored, and QC'd successfully.

Route:

- Generation through the relay-backed Gemini call (the direct
  `generativelanguage.googleapis.com` route was unreachable in Run 001).
- Inputs: approved static cat warrior + `pose_walk_3dir_4x4.png` as the
  pose/layout guide.
- Frame layout: `walk_3dir_4x4`, sheet 1024x1024 JPG.

Raw API output:

- `assets/tasks/imports/cat_warrior_walk_gemini_relay_pose_guided_001_raw/gemini_image_sheet_0.jpg`

Three-direction processed source:

- `assets/tasks/sprites/cat_warrior_walk_gemini_relay_pose_guided_001`

Final mirrored four-direction task:

- `assets/tasks/sprites/cat_warrior_walk_gemini_relay_pose_guided_4dir_001`
- `sheet_0.png`
- `processed_0_0.png` through `processed_0_15.png`
- `qc_auto_all_directions.gif`
- `qc_auto_down_front.gif`
- `qc_auto_left.gif`
- `qc_auto_right.gif`
- `qc_auto_up_back.gif`
- `qc_auto_metrics.json`

QC summary:

- 16/16 final frames built successfully.
- Exact chroma-key residual pixels: `0` for every source frame.
- Edge visible pixels: `0` for every source frame.
- Pipeline is deterministic: re-running `import-sheet` + `build-walk-4dir` +
  `walk-qc` from the same raw JPG reproduces the committed PNG bytes and metric
  values; only the embedded `created_at` timestamps change.
- Per-row motion metrics (% pixels):
  - down/front: motion_mean 5.655, motion_range 1.826, loop_diff 5.746,
    flags `low_pose_motion`.
  - left:       motion_mean 5.542, motion_range 4.140, loop_diff 6.421,
    flags `low_pose_motion`, `scale_or_detail_drift`.
  - right:      identical to left because it is mirrored in code.
  - up/back:    motion_mean 5.925, motion_range 5.649, loop_diff 8.316,
    flags `low_pose_motion`, `uneven_pose_motion`, `scale_or_detail_drift`.
- Compared with Real Run 002 (`cat_warrior_walk_4dir_mirrored_001`), all four
  rows still trip `low_pose_motion`, and the side rows additionally trip
  `scale_or_detail_drift`. The pose guide reduced direction inconsistency but
  did not yet produce the strong walk cycle target.
- All 16 source frames still reported `subject_overflow` during import; the
  model continues to fill the 70% safe area.

Reanchor protocol:

- The 32 PNG files modified between b8a446d and the next run were the result
  of an out-of-pipeline edit (sheet 1024 -> 512 plus bottom-row pixel changes)
  that left `meta.json` describing a 1024x1024 sheet with 256 cell size while
  the actual sheet was 512x512. The fix is to re-run `import-sheet` against the
  unchanged raw JPG; the deterministic pipeline overwrites the strays back to
  the b8a446d byte state.
