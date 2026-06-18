# Meowasen 阶段 1：核心生成闭环实施方案

## 1. 阶段目标

阶段 1 只实现从用户提供 prompt 到产出可用像素素材文件的核心能力，不实现 Web 前端、FastAPI 接口、异步任务队列和 SQLite 历史库。

本阶段完成后，应能通过命令行输入：

```text
asset_type + frame_layout + user_prompt + 输出尺寸/色数
```

系统完成：

```text
Prompt 模板增强
→ 调用图像生成 Provider
→ 保存原始 sheet
→ 切片
→ 抠图/裁切/居中/缩放/量化
→ 输出 processed PNG
→ 写入 meta.json
```

阶段 1 的核心验收是：用户不需要打开 Web 页面，只通过命令行即可生成最终 PNG 素材，并能在任务目录中找到原图、切片、处理图和元数据。

---

## 2. 范围

### 2.1 范围内

1. 配置文件与环境变量读取。
2. Prompt 模板与 frame layout 模板。
3. `GeneratorProtocol` 与首个云端 Provider。
4. Provider 能力声明：参考图、图片编辑、自定义尺寸、seed。
5. 命令行生成入口。
6. 原始 sheet 保存。
7. `SpriteSheetSplitter` 按网格切片。
8. 后处理 pipeline：
   - `normalize_rgba`
   - `remove_background`
   - `crop_to_subject`
   - `center_on_canvas`
   - `nearest_neighbor_scale`
   - `quantize_palette`
9. `split_quality` 检测与记录。
10. 候选级 `style_lock` 提取与锁定量化。
11. 本地文件系统保存产物。
12. `meta.json` 记录完整生成参数和结果状态。
13. 本地重处理命令：基于已有 sheet/frames 按新参数重跑后处理。

### 2.2 范围外

1. FastAPI 服务。
2. Vite 前端。
3. SQLite 表结构和查询。
4. 前端收藏、历史、派生菜单。
5. 后台异步任务队列。
6. zip 下载接口。
7. Web 轮询状态。
8. 多用户、账号、权限、计费。
9. ComfyUI Provider。

---

## 3. 目录结构

阶段 1 建议先按最终设计中的核心模块落地，但不启用 `api/`、`repository/SQLite` 和 `frontend/`：

```text
meowasen/
├── config.yaml
├── .env
├── backend/
│   ├── cli.py
│   ├── generator/
│   │   ├── protocol.py
│   │   └── gemini.py
│   ├── prompt/
│   │   ├── builder.py
│   │   ├── templates/
│   │   │   ├── character.yaml
│   │   │   ├── prop.yaml
│   │   │   ├── icon.yaml
│   │   │   └── tile.yaml
│   │   └── frame_layouts/
│   │       ├── single.yaml
│   │       ├── idle.yaml
│   │       ├── walk_cycle.yaml
│   │       ├── turnaround.yaml
│   │       └── sprite_pack.yaml
│   ├── postprocess/
│   │   ├── pipeline.py
│   │   ├── splitter.py
│   │   └── style_lock.py
│   ├── storage/
│   │   ├── manifest.py
│   │   └── paths.py
│   └── service/
│       └── core_generation.py
└── assets/
```

`backend/service/core_generation.py` 是阶段 1 的编排入口，阶段 2 的 `TaskService` 应复用它，而不是重新写一套生成逻辑。

---

## 4. 命令行入口

### 4.1 生成命令

```bash
python -m backend.cli generate \
  --asset-type character \
  --frame-layout single \
  --prompt "蓝色披风的小骑士，俯视角，像素风" \
  --count 2 \
  --output-size 64 \
  --palette-colors 16
```

多帧示例：

```bash
python -m backend.cli generate \
  --asset-type prop \
  --frame-layout sprite_pack \
  --prompt "宝箱、药水、钥匙、金币，同一套 RPG 背包图标" \
  --count 2 \
  --output-size 64 \
  --palette-colors 16
```

### 4.2 重处理命令

```bash
python -m backend.cli reprocess \
  --task-id 550e8400-e29b-41d4-a716-446655440000 \
  --candidate-index 0 \
  --output-size 128 \
  --palette-colors 32
```

### 4.3 提取风格锁命令

```bash
python -m backend.cli extract-style-lock \
  --task-id 550e8400-e29b-41d4-a716-446655440000 \
  --candidate-index 0
```

### 4.4 使用风格锁生成

```bash
python -m backend.cli generate \
  --asset-type character \
  --frame-layout walk_cycle \
  --prompt "保持原角色，正面 4 帧 walk cycle" \
  --style-lock-task 550e8400-e29b-41d4-a716-446655440000 \
  --style-lock-candidate 0 \
  --count 1
```

---

## 5. 核心模块实施

### 5.1 ConfigLoader

职责：

1. 读取 `config.yaml`。
2. 读取 `.env` 中的 API Key。
3. 合并 CLI 参数、配置默认值、frame layout 默认值。
4. 根据 `frame_layout` 推导：
   - `frame_grid`
   - `cell_size`
   - `sheet_width`
   - `sheet_height`
   - 默认 `count`

配置优先级：

```text
CLI 参数 > config.yaml > 内置默认值
```

### 5.2 PromptBuilder

输入：

```text
asset_type
frame_layout
user_prompt
negative_extra
```

输出：

```text
enhanced_prompt
negative_prompt
frame_grid
```

要求：

1. `asset_type` 必须是 `character/prop/icon/tile`。
2. `frame_layout` 必须能在 `prompt/frame_layouts/` 中找到。
3. 最终 prompt 拼装顺序保持：

```text
素材模板 prefix
→ frame layout description
→ 用户 prompt
→ frame_prompts
→ avoid: negative + negative_extra
```

### 5.3 GeneratorProtocol

阶段 1 实现设计方案里的：

```python
ProviderCapabilities
GenerateRequest
GeneratedImage
GeneratorProtocol
```

Provider 必须显式声明能力：

```text
supports_reference_images
supports_image_edit
supports_custom_size
supports_seed
```

不支持的能力不能静默伪装。例如不支持参考图时，只记录 `reference_applied=false`，不把图片 base64 拼进 prompt。

### 5.4 GeminiOpenAICompatibleProvider

阶段 1 默认只实现一个云端 Provider。

要求：

1. 通过 OpenAI-compatible endpoint 调用中转站。
2. `count` 使用多次 `n=1` 请求完成。
3. 每张返回图保存为 `sheet_N.png`。
4. 记录 provider metadata：
   - `model`
   - `request_id`，若 Provider 有返回
   - `reference_applied`
   - `seed_applied`
   - `requested_size`
   - `actual_size`
5. API 失败时保留已成功候选。

### 5.5 SpriteSheetSplitter

输入：

```text
sheet image
frame_grid rows/cols
```

输出：

```text
frame_N_M.png
split_quality
```

`split_quality` 格式：

```json
{
  "status": "ok",
  "flags": []
}
```

必须支持的 flags：

```text
empty_frame
low_alpha_area
bbox_touches_edge
subject_overflow
grid_merge_suspected
```

阶段 1 的切片策略先采用等分网格。复杂的人工裁切框调整留到阶段 2 前端做。

### 5.6 PostProcessor

处理顺序：

```text
normalize_rgba
→ remove_background
→ crop_to_subject
→ center_on_canvas
→ nearest_neighbor_scale
→ quantize_palette
```

实现要求：

1. 原始 sheet 必须保留。
2. 切片后的 raw frame 必须保留。
3. processed PNG 必须使用透明背景。
4. 最近邻缩放必须使用 nearest neighbor。
5. 调色板量化必须保留 alpha。
6. rembg 失败时不能中断候选，只标记 `bg_removed=false`。
7. 单帧失败不影响同候选其他帧。

### 5.7 StyleLockExtractor

职责：

1. 从用户选定候选的 processed frames 提取 16 色调色板。
2. 生成 `style_lock_palette_b64`。
3. 生成 `style_lock_histogram_json`。
4. 写回该候选的 `meta.json`。

规则：

1. 不自动使用第 0 个候选作为风格锁。
2. 必须由 CLI 参数或阶段 2 的 UI 操作显式指定候选。
3. 派生生成时，如果调色板缺失，先尝试提取；仍失败则记录 `style_lock_applied=false`。

### 5.8 ManifestStorage

阶段 1 不引入 SQLite，只写文件和 `meta.json`。

任务目录结构：

```text
assets/
└── {task_id}/
    ├── meta.json
    ├── sheet_0.png
    ├── frame_0_0.png
    ├── processed_0_0.png
    └── ...
```

`meta.json` 必须包含：

1. 任务级信息：
   - `task_id`
   - `created_at`
   - `asset_type`
   - `frame_layout`
   - `frame_grid`
   - `cell_size`
   - `sheet_size`
   - `user_prompt`
   - `enhanced_prompt`
   - `model`
   - `output_size`
   - `palette_colors`
   - `status`
   - `error`
2. Provider 信息：
   - `reference_applied`
   - `seed_applied`
   - `provider_metadata`
3. 候选信息：
   - `index`
   - `sheet`
   - `style_lock_palette_b64`
   - `style_lock_histogram_json`
   - `status`
4. 帧信息：
   - `frame_index`
   - `grid_pos`
   - `raw`
   - `processed`
   - `bg_removed`
   - `split_quality`
   - `status`
   - `error`

---

## 6. 里程碑

### M1：配置、模板与 CLI 骨架

交付：

1. `config.yaml` 示例。
2. `.env` 读取。
3. `PromptBuilder` 可拼出增强 prompt。
4. `python -m backend.cli generate --dry-run` 可打印请求参数。

验收：

```text
输入 asset_type + frame_layout + prompt，能输出 enhanced_prompt、frame_grid、sheet_size。
```

### M2：Provider 出图并保存 sheet

交付：

1. `GeneratorProtocol`。
2. `GeminiOpenAICompatibleProvider`。
3. 生成后保存 `sheet_N.png`。
4. 写入基础 `meta.json`。

验收：

```text
命令行输入 prompt 后，assets/{task_id}/sheet_0.png 存在，meta.json 记录模型和 prompt。
```

### M3：单图后处理闭环

交付：

1. `single` layout 支持。
2. 1x1 sheet 切片。
3. 透明 PNG 后处理。
4. 失败兜底。

验收：

```text
角色、道具、图标三类至少各生成 1 张 processed PNG。
```

### M4：sprite sheet 切片与多帧处理

交付：

1. `sprite_pack` layout。
2. `idle/walk_cycle/turnaround` 作为实验 layout 可跑通。
3. `split_quality` 记录。
4. 单帧失败不影响其他帧。

验收：

```text
sprite_pack 生成 2x2 sheet 后，能输出 4 张 processed PNG。
```

### M5：style lock 与重处理

交付：

1. `extract-style-lock` 命令。
2. 使用锁定调色板重处理。
3. 使用锁定调色板生成派生任务。

验收：

```text
基于候选 A 提取调色板后，候选 B 的量化输出使用相同颜色集合。
```

---

## 7. 阶段 1 验收标准

1. 未启动任何 Web 服务时，命令行可以完成一次素材生成。
2. `single` 模式可以输出至少 1 张 processed PNG。
3. `sprite_pack` 模式可以从 2x2 sheet 输出 4 张 processed PNG。
4. 每个任务目录都有 `meta.json`。
5. `meta.json` 能追溯 prompt、模型、Provider metadata、sheet、frames、processed 文件。
6. Provider 不支持参考图时，任务不会失败，记录 `reference_applied=false`。
7. rembg 或单帧后处理失败时，不影响其他候选/其他帧继续处理。
8. 可以基于已有任务重跑后处理，不重新调用图像生成 API。
9. 可以从用户指定候选提取 style lock。
10. 使用 style lock 后，输出素材的量化调色板一致。

---

## 8. 阶段 1 完成后移交给阶段 2 的能力

阶段 1 完成后，阶段 2 可以直接复用：

1. `core_generation.generate()`
2. `core_generation.reprocess()`
3. `StyleLockExtractor`
4. `PromptBuilder`
5. `GeneratorProtocol`
6. `PostProcessor`
7. `meta.json` 结构

阶段 2 不应重写生成逻辑，只应在这些核心能力外增加：

```text
FastAPI API
SQLite Repository
异步任务状态
Web UI
历史记录
收藏与派生交互
下载接口
```
