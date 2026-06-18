# Character Static Test 001

Role prompt: ornate cat warrior carrying a long spear, RPG overworld sprite.

Retained real-generation task:

- `assets/tasks/sprites/cat_warrior_candidates_gpt_wifi_001`

Current final outputs in that task:

- `sheet_0.png`: raw GPT 2x2 candidate sheet on `#FF00FF` background
- `processed_0_0.png`: selected transparent runtime sprite, overwritten in place
- `selected_processed.png`: explicit copy of the selected frame
- `processed_0_0_checker_preview.png`: checkerboard review preview
- `processed_0_0_alpha_mask.png`: alpha-mask review preview
- `selected_preview_256.png`: clearer selected review/source sprite
- `selected_preview_384.png`: high-resolution selected review/source sprite

Workflow update:

- Use `character_candidates_2x2` for future static character tests when spending a real model call.
- This asks the model for four alternatives of the same RPG character in one sheet.
- Review the four processed frames, then run `select-frame --overwrite-primary` to promote one frame to `processed_0_0.png`.
- Keep one logical test in one task directory by passing a fixed `--task-id` when needed.

Latest cleanup metrics for `processed_0_0.png`:

- exact key residual pixels: `0`
- broad magenta/purple residual pixels: `2`
- connected foreground components: `1`
- visible edge pixels: `0`

Clarity note:

- `processed_0_0.png` is a 128x128 runtime sprite and is expected to lose detail.
- Use `selected_preview_256.png` or `selected_preview_384.png` for human review,
  source retention, and any later engine import that can scale with nearest-neighbor filtering.
