# Tools

This directory currently contains both active pipeline tools and legacy experiment helpers.

## Active Video-To-Sprite Pipeline

Use these for the current reference-image -> I2V -> sprite-sheet validation route:

```text
wlai_video_generate.py      call bobdong.cn /v1/videos for Seedance I2V
mirror_video.py             mirror a side-view video to create the opposite direction
video_contact_sheet.py      sample videos into a quick visual contact sheet
postprocess_walk_videos.py  auto-select loop frames and build pixel sprite sheets
pixelize_sprite.py          shared pixelization/cutout helpers used by postprocess_walk_videos.py
```

## Image Provider Debug Bridges

These are still useful while ComfyUI reference generation is being integrated:

```text
imagehub_generate.py
gpt_image_generate.py
gemini_image_generate.py
```

## Legacy / Candidate Archive

These are historical helpers and should not be treated as current pipeline entry points:

```text
animation_preview.html      older standalone preview page
extract_video_frames.py     manual video frame extraction, mostly replaced by auto-loop selection
make_walk_pose_guide.py     old pose-guide sheet generator
```

See `docs/plans/pixel-asset-generator/项目整理审计-2026-06-30.md` before deleting or moving legacy files.

Already removed as obsolete one-off helpers:

```text
../gpt-test.py
splice_walk_row.py
_row2_inspect.py
```
