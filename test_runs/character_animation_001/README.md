# Character Animation Test 001

Goal: validate API-based RPG character animation generation after the static
cat warrior test passed.

Reference static test:

- `../character_static_001/assets/tasks/sprites/cat_warrior_candidates_gpt_wifi_001/selected_preview_384.png`
- The current GPT image endpoint is text-to-image only in this project, so the
  reference is used as manual identity guidance unless a future image-reference
  API is added.

Primary API route:

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

Fallback idea if 16-frame identity consistency is poor:

- Add a square 4x4 layout that generates down/front, up/back, and left-facing
  rows, then derives the right-facing row by mirroring in code.
- Avoid a raw 3x4 API canvas for now because the paid image endpoint is most
  predictable with `1024x1024`.

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
