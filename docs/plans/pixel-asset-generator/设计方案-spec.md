# Meowasen 设计方案

## 1. 系统架构

```
Web UI (Vite + 轻量 UI)
        ↓ HTTP
FastAPI Backend
    ├── PromptBuilder         — 拼接素材模板 + 帧布局模板 + 用户输入
    ├── ImageGeneratorProvider — 通过 Provider 适配中转站 Gemini API / 未来 ComfyUI
    ├── PostProcessor         — 切片 → 抠图 → 裁切 → 缩放 → 量化（支持锁定调色板）
    ├── StyleLockExtractor    — 从基底候选提取调色板/直方图，作为派生生成的风格指纹
    ├── TaskService           — 异步任务编排 + 状态推进 + 派生任务
    └── TaskRepository        — SQLite 元数据 + 本地文件
```

生成层通过 `GeneratorProtocol` 接口隔离，`config.yaml` 里 `generator.backend` 字段决定走哪个 Provider 实现；任务编排、后处理、存储和 UI 不感知具体生成后端。默认 Provider 仍为中转站 Gemini，默认模型固定为中转站已验证存在的 `gemini-3.1-flash-image-preview`。

多帧素材（walk cycle、turnaround、套件）优先采用「单次 API 调用生成 sprite sheet 大图，再由后处理切片」的方式，依靠同一次生成上下文提高帧间一致性；衍生生成时把用户选定候选的风格指纹（`style_lock`）回传给 Provider 与后处理器，在后处理层锁定调色板并尽量对齐整体色彩分布。`style_lock` 只能强制颜色和量化规则一致，不能强制角色轮廓、比例、光影和细节完全一致。

---

## 2. 项目目录结构

```
meowasen/
├── config.yaml
├── .env                        # GEMINI_API_KEY
├── backend/
│   ├── main.py                 # FastAPI app 入口
│   ├── generator/
│   │   ├── protocol.py         # GeneratorProtocol / GenerateRequest / GeneratedImage
│   │   └── gemini.py           # GeminiOpenAICompatibleProvider
│   ├── prompt/
│   │   ├── builder.py
│   │   ├── templates/
│   │   │   ├── character.yaml
│   │   │   ├── prop.yaml
│   │   │   ├── icon.yaml
│   │   │   └── tile.yaml
│   │   └── frame_layouts/
│   │       ├── single.yaml          # 1×1 单图
│   │       ├── walk_cycle.yaml      # 1×4 walk cycle
│   │       ├── idle.yaml            # 1×2 idle 呼吸
│   │       ├── turnaround.yaml      # 3×3 八方向 + 中心
│   │       └── sprite_pack.yaml     # 2×2 同套件批量
│   ├── postprocess/
│   │   ├── pipeline.py
│   │   ├── splitter.py              # sprite sheet 切片
│   │   └── style_lock.py            # 调色板提取 + 锁定量化
│   ├── service/
│   │   └── task_service.py          # 创建任务、调用 Provider、推进状态、派生任务
│   ├── repository/
│   │   ├── models.py           # SQLite schema
│   │   └── task_repo.py
│   └── api/
│       └── routes.py
├── frontend/
│   ├── index.html
│   └── src/
│       └── main.js
├── assets/                     # 生成产物（git ignore）
└── data/
    └── meowasen.db
```

---

## 3. 生成层

### GeneratorProtocol

生成层不直接暴露某一家 API 的参数。业务层统一传入 `GenerateRequest`，Provider 返回标准化后的 `GeneratedImage`，后续切换到 ComfyUI、fal.ai 或 Replicate 时只新增 Provider 实现。

```python
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class ProviderCapabilities:
    supports_reference_images: bool = False
    supports_image_edit: bool = False
    supports_custom_size: bool = False
    supports_seed: bool = False


@dataclass
class GenerateRequest:
    prompt: str
    negative_prompt: str
    count: int
    model: str
    output_size: int                         # 单帧最终输出尺寸，如 64
    timeout_seconds: int
    seed: int | None = None
    output_mime_type: str = "image/png"
    frame_layout: str = "single"
    frame_grid: tuple[int, int] = (1, 1)      # (rows, cols)，如 (1,4) 为 4 帧横排
    cell_size: int = 256                      # 生成 sheet 中每个格子的目标像素尺寸
    sheet_width: int | None = None            # 未传时由 cols * cell_size 推导
    sheet_height: int | None = None           # 未传时由 rows * cell_size 推导
    style_lock: dict[str, Any] | None = None   # 含 palette bytes + histogram，锁定量化
    provider_options: dict[str, Any] = field(default_factory=dict)
    reference_images: list[bytes] = field(default_factory=list)


@dataclass
class GeneratedImage:
    index: int
    image_bytes: bytes
    mime_type: str = "image/png"
    provider_metadata: dict[str, Any] = field(default_factory=dict)


class GeneratorProtocol(Protocol):
    capabilities: ProviderCapabilities

    async def generate(self, request: GenerateRequest) -> list[GeneratedImage]: ...

    async def edit(
        self,
        image_bytes: bytes,
        prompt: str,
        provider_options: dict[str, Any] | None = None,
    ) -> GeneratedImage: ...
```

### GeminiOpenAICompatibleProvider

使用 `openai.AsyncOpenAI`，通过中转站的 OpenAI 兼容端点调用：

```python
client = AsyncOpenAI(
    api_key=api_key,
    base_url=config.relay.base_url,   # https://your-relay.example.com/v1
)

response = await client.images.generate(
    model=request.model,    # 默认 gemini-3.1-flash-image-preview
    prompt=full_prompt,     # prefix + user_input，负向 prompt 拼入末尾："avoid: ..."
    n=1,
    response_format="b64_json",
    **request.provider_options,
)
image_bytes = base64.b64decode(response.data[0].b64_json)
```

- `count` 张图用 `asyncio.gather` 并发发起 `n=1` 的请求，避免部分中转站不支持 `n>1`
- `negative_prompt` 以 `"avoid: {negative}"` 形式追加到 prompt 末尾（OpenAI 图像 API 无独立 negative 字段）
- `frame_grid != (1,1)` 时，`PromptBuilder` 把帧布局描述拼入 prompt（如 "4-frame walk cycle, frames laid out horizontally, no overlap"），Provider 仍发起单次生成，返回包含多帧的大图。Provider 需要知道 `frame_layout`、`frame_grid`、`cell_size`、`sheet_width`、`sheet_height`，用于选择画布比例或透传尺寸参数；帧切分仍由后处理的 `SpriteSheetSplitter` 完成
- `reference_images` 非空时先检查 `capabilities.supports_reference_images` 或 `capabilities.supports_image_edit`。支持时走 Provider 的图片输入/编辑能力，保持同一角色的派生生成；不支持时不把图片 base64 拼进 prompt，而是明确降级为文本派生，并在 `provider_metadata.reference_applied=false`、`meta.json.reference_applied=false` 中记录
- 超时由 `config.generation.timeout_seconds` 控制，透传给 OpenAI SDK 的底层 HTTP 客户端
- 默认模型保持 `gemini-3.1-flash-image-preview`，这是当前中转站已验证可用且唯一可用的图像模型
- Provider 返回 `list[GeneratedImage]`，调用方不直接依赖 OpenAI SDK 响应结构

Provider 初始化时必须声明能力边界。业务层只根据 `ProviderCapabilities` 决定是否启用参考图、图片编辑、自定义 sheet 尺寸和 seed；不支持的能力不做静默假装，统一写入任务元数据，方便用户判断派生任务为何一致性下降。

---

## 4. Prompt 模板

每个素材类型对应 `templates/{type}.yaml`，格式：

```yaml
# templates/character.yaml
prefix: >
  pixel art style, game sprite, single character,
  centered composition, transparent background,
  clear readable silhouette, strong pose,
  limited color palette, no background details,
  front-facing view, 64x64 pixel game asset
negative: >
  realistic, photorealistic, blurry, noisy,
  background clutter, multiple characters, text, watermark
```

`PromptBuilder` 把 `prefix + " " + user_input` 作为正向 prompt，`negative` 作为负向 prompt。

四种素材类型的模板差异点：

| 类型 | 额外约束 |
|---|---|
| character | front-facing view, single subject, centered |
| prop | isolated object, no shadow, clear outline |
| icon | square composition, ui-friendly, bold shapes |
| tile | top-down view, tile reference texture, seamless-ready；MVP 只作为地块参考图，不承诺自动无缝 |

### 帧布局模板

`prompt/frame_layouts/{layout}.yaml` 描述帧数和每帧动作，`PromptBuilder` 把帧布局描述拼到素材模板之后：

```yaml
# frame_layouts/walk_cycle.yaml
grid: [1, 4]
description: >
  4-frame walk cycle sprite sheet, frames laid out horizontally,
  evenly spaced, identical character in each cell, no overlap between cells
frame_prompts:
  - "frame 1: contact pose, left foot forward, right arm forward"
  - "frame 2: passing pose, weight on right leg, body slight up"
  - "frame 3: contact pose, right foot forward, left arm forward"
  - "frame 4: passing pose, weight on left leg, body slight up"
negative_extra: "merged figures, overlapping sprites, missing cell separation"
```

预设布局：

| layout | grid | 用途 |
|---|---|---|
| single | (1, 1) | 默认，单图生成 |
| idle | (1, 2) | idle 呼吸 2 帧 |
| walk_cycle | (1, 4) | 4 帧步行循环 |
| turnaround | (3, 3) | 八方向 + 中心，角色多视图 |
| sprite_pack | (2, 2) | 同风格批量道具/图标 |

素材类型与 frame layout 正交组合：例如 `character + walk_cycle`、`prop + sprite_pack`、`character + turnaround`。`single` 是默认值，请求体不传 `frame_layout` 时按 `single` 处理，行为与单图生成完全一致。

最终 prompt 拼装顺序：素材模板 prefix + frame layout description + 用户输入 + 帧逐帧描述（如有） + `"avoid: " + negative + negative_extra`。

---

## 5. 后处理管线

```
API raw bytes (sprite sheet 大图)
    → normalize_rgba()                  统一转 RGBA，保留原始 sheet PNG
    → split_sprite_sheet(frame_grid)    SpriteSheetSplitter 按网格均切，frame_grid=(1,1) 时直通
    → for each frame:
        → remove_background()           rembg，u2net-anime 模型
        → crop_to_subject()             优先 alpha bounding box；无 alpha 时走非透明兜底裁切
        → center_on_canvas()            居中放到目标尺寸画布
        → nearest_neighbor_scale()      PIL.NEAREST，不做平滑插值
        → quantize_palette()            只量化 RGB，alpha 通道单独保留后合回；
                                        有 style_lock.palette 时使用锁定调色板
    → list[PNG bytes]，长度 = rows × cols
```

`split_sprite_sheet` 按 `frame_grid` 等分原图，单帧主体未在网格中央时，`crop_to_subject` 兜底从 alpha bounding box 重新定位。切片时为每帧计算 `split_quality`，格式为 `{"status":"ok|warning|failed","flags":[]}`，flags 至少支持 `empty_frame`、`low_alpha_area`、`bbox_touches_edge`、`subject_overflow`、`grid_merge_suspected` 五类标记，供候选状态和前端提示使用。rembg 步骤失败时跳过，保存原始输出，候选记录中标注 `bg_removed: false`，后续步骤继续执行。透明图量化时必须保留 alpha 通道，避免调色板量化把透明边缘写成脏色。若单帧后处理失败，只标记该帧 `failed`，同批其他帧继续处理。

`style_lock.palette` 存在时，`quantize_palette` 强制使用锁定调色板，确保同一角色在不同派生任务（walk cycle、turnaround、sprite pack）里颜色集合完全一致。`StyleLockExtractor` 从用户收藏或显式选定的候选结果提取 16 色调色板和色彩直方图，写入 `candidates.style_lock_palette_b64` 与 `meta.json`，作为后续派生任务的输入。若用户尚未选定候选，系统不自动把第 0 个候选作为风格基底；派生入口应提示先选择一个候选。

同一张 raw 图允许后续按不同 `output_size`、`palette_colors`、`canvas_padding`、甚至切换 `style_lock` 重跑后处理，避免为了调整像素化参数重复调用付费 API。

---

## 6. 存储结构

### 文件系统

```
assets/
└── {task_id}/
    ├── meta.json
    ├── sheet_0.png             # 第 0 个候选的原始 sprite sheet（单图任务亦视为 1×1 sheet）
    ├── sheet_1.png
    ├── sheet_2.png
    ├── sheet_3.png
    ├── frame_0_0.png           # sheet 0 切片后第 0 帧的 raw（rembg 之前）
    ├── frame_0_1.png           # sheet 0 第 1 帧；单图任务时只有 frame_N_0.png
    ├── ...
    ├── processed_0_0.png       # sheet 0 第 0 帧后处理结果
    ├── processed_0_1.png
    └── ...
```

候选 = 一次 API 调用产物（一张 sheet）；帧 = 切片后的子图。`frame_grid=(1,1)` 时每个候选只有一帧，目录里 `sheet_N.png` 与 `frame_N_0.png` 内容相同（实现上可硬链接或符号链接节省空间）。

### meta.json

```json
{
  "task_id": "550e8400-e29b-41d4-a716-446655440000",
  "created_at": "2026-06-16T10:00:00Z",
  "asset_type": "character",
  "frame_layout": "walk_cycle",
  "frame_grid": [1, 4],
  "cell_size": 256,
  "sheet_size": [1024, 256],
  "style_lock_source_task": "5a4fc2b1-...",
  "style_lock_source_candidate_id": 7,
  "style_lock_applied": true,
  "reference_applied": true,
  "user_prompt": "蓝色披风骑士，RPG 俯视角",
  "enhanced_prompt": "pixel art style, game sprite ... 4-frame walk cycle ... 蓝色披风骑士",
  "model": "gemini-3.1-flash-image-preview",
  "output_size": 64,
  "palette_colors": 16,
  "status": "done",
  "error": null,
  "candidates": [
    {
      "id": 7,
      "index": 0,
      "sheet": "sheet_0.png",
      "style_lock_palette_b64": "iVBORw0KGgo...",
      "frames": [
        {"frame_index": 0, "grid_pos": [0, 0], "raw": "frame_0_0.png", "processed": "processed_0_0.png", "bg_removed": true, "split_quality": {"status": "ok", "flags": []}, "status": "done", "error": null},
        {"frame_index": 1, "grid_pos": [0, 1], "raw": "frame_0_1.png", "processed": "processed_0_1.png", "bg_removed": true, "split_quality": {"status": "ok", "flags": []}, "status": "done", "error": null},
        {"frame_index": 2, "grid_pos": [0, 2], "raw": "frame_0_2.png", "processed": "processed_0_2.png", "bg_removed": true, "split_quality": {"status": "ok", "flags": []}, "status": "done", "error": null},
        {"frame_index": 3, "grid_pos": [0, 3], "raw": "frame_0_3.png", "processed": "processed_0_3.png", "bg_removed": true, "split_quality": {"status": "ok", "flags": []}, "status": "done", "error": null}
      ],
      "favorited": false,
      "status": "done",
      "error": null
    }
  ]
}
```

`style_lock_source_task` 为空表示这是基底任务；非空指向用作风格来源的上游任务 ID。`style_lock_source_candidate_id` 指向用户选定的风格候选。`style_lock_palette_b64` 存在于候选层，是 PIL 调色板字节的 base64，可直接喂给 `Image.quantize(palette=...)`。

### 写入顺序与一致性

任务写入采用"先任务、后文件、再候选/帧、最后状态"的顺序：

1. 创建任务目录，写入 `tasks` 记录，状态为 `pending`，记录 `frame_grid`、`cell_size`、`sheet_width`、`sheet_height`、`style_lock_source_task` 与 `style_lock_source_candidate_id`。
2. 任务开始执行时更新为 `running`。
3. 每个候选先保存 `sheet_N.png`，立刻写入 `candidates` 记录（状态 `running`）。
4. 后处理对该 sheet 切片，逐帧执行 pipeline，每帧成功后写 `frame_N_M.png` / `processed_N_M.png` 并 upsert `frames` 记录，同时记录 `split_quality`。
5. 当前候选所有帧处理结束后，按帧成功率把候选状态置为 `done` / `partial_failed` / `failed`。
6. 全部候选完成后按候选状态聚合任务状态：所有候选 `done` → `done`；任意候选有成功帧但非全部 `done` → `partial_failed`；全部失败 → `failed`。
7. 用户收藏或显式选择某个候选作为风格基底时，由 `StyleLockExtractor` 提取调色板，更新该候选的 `style_lock_palette_b64` 与 `style_lock_histogram_json`。
8. `meta.json` 作为文件侧快照，在每次帧/候选/任务状态变化后重写，内容以 SQLite 当前记录为准。

### SQLite 表结构

**tasks**

| 列 | 类型 | 说明 |
|---|---|---|
| task_id | TEXT PK | UUID |
| created_at | TEXT | ISO8601 |
| asset_type | TEXT | character/prop/icon/tile |
| frame_layout | TEXT | single/idle/walk_cycle/turnaround/sprite_pack/... |
| frame_grid_rows | INTEGER | |
| frame_grid_cols | INTEGER | |
| cell_size | INTEGER | 生成 sheet 中每个格子的目标像素尺寸 |
| sheet_width | INTEGER | Provider 实际请求或期望的 sheet 宽度 |
| sheet_height | INTEGER | Provider 实际请求或期望的 sheet 高度 |
| style_lock_source_task | TEXT | NULL 表示基底任务 |
| style_lock_source_candidate_id | INTEGER | NULL 表示未使用候选级风格锁 |
| style_lock_applied | INTEGER | 0/1，后处理是否实际使用锁定调色板 |
| reference_applied | INTEGER | 0/1，Provider 是否实际使用参考图 |
| user_prompt | TEXT | |
| enhanced_prompt | TEXT | |
| model | TEXT | |
| status | TEXT | pending/running/done/partial_failed/failed |
| error | TEXT | 失败原因，成功时为空 |

**candidates**

| 列 | 类型 | 说明 |
|---|---|---|
| id | INTEGER PK | |
| task_id | TEXT FK | |
| index | INTEGER | 候选序号，0–(count-1) |
| sheet_path | TEXT | 原始 sprite sheet 文件路径 |
| style_lock_palette_b64 | TEXT | 从该候选提取的 16 色调色板，供派生任务复用 |
| style_lock_histogram_json | TEXT | 从该候选提取的色彩直方图，用于分析和后续扩展 |
| favorited | INTEGER | 0/1 |
| status | TEXT | running/done/partial_failed/failed |
| error | TEXT | 候选级失败原因 |

**frames**

| 列 | 类型 | 说明 |
|---|---|---|
| id | INTEGER PK | |
| candidate_id | INTEGER FK | |
| frame_index | INTEGER | 0–(rows×cols-1) |
| grid_row | INTEGER | |
| grid_col | INTEGER | |
| raw_path | TEXT | 切片后未后处理的帧 |
| processed_path | TEXT | 后处理结果 |
| bg_removed | INTEGER | 0/1 |
| split_quality_json | TEXT | JSON，格式为 `{"status":"ok|warning|failed","flags":[]}` |
| status | TEXT | done/failed |
| error | TEXT | 单帧失败原因 |

---

## 7. API 接口

| Method | Path | 说明 |
|---|---|---|
| `POST` | `/tasks` | 创建任务，返回 `pending` 任务并后台发起生成 |
| `GET` | `/tasks` | 列出历史任务，支持 `asset_type` / `frame_layout` / `favorited` 过滤 |
| `GET` | `/tasks/{task_id}` | 获取任务详情 + 候选 + 帧列表 |
| `POST` | `/tasks/{task_id}/regenerate` | 保留历史结果，创建一次新的重新生成任务（沿用原 frame_layout） |
| `POST` | `/tasks/{task_id}/derive` | 基于用户选定候选的 `style_lock_palette_b64` + base 候选参考图，新建派生任务（指定新的 frame_layout，如 walk_cycle/turnaround/sprite_pack） |
| `POST` | `/candidates/{id}/reprocess` | 基于已有 sheet 与帧切片按新后处理参数重跑，不重新调用生成 API |
| `PATCH` | `/candidates/{id}` | 更新 `favorited` |
| `GET` | `/candidates/{id}/download` | 下载该候选所有帧的处理结果（zip）或拼接 sheet |
| `GET` | `/frames/{id}/download` | 下载单帧 processed PNG |

**POST /tasks 请求体**

```json
{
  "asset_type": "character",
  "frame_layout": "walk_cycle",
  "user_prompt": "蓝色披风骑士，RPG 俯视角",
  "count": 1,
  "output_size": 64,
  "cell_size": 256,
  "palette_colors": 16,
  "style_lock_source_task": null,
  "style_lock_source_candidate_id": null
}
```

`frame_layout` 省略时按 `single` 处理。`cell_size` 省略时按配置默认值处理，`sheet_width/sheet_height` 由 `frame_grid × cell_size` 推导。`style_lock_source_candidate_id` 非空时后端从对应候选读取调色板；若 Provider 支持参考图，则同时把该候选 sheet 或首帧作为参考图，否则只启用锁定量化并记录 `reference_applied=false`。

**POST /tasks 响应**

```json
{
  "task_id": "550e8400-...",
  "status": "pending",
  "poll_url": "/tasks/550e8400-..."
}
```

**POST /tasks/{task_id}/derive 请求体**

```json
{
  "frame_layout": "walk_cycle",
  "base_candidate_id": 7,
  "user_prompt_extra": "保持原角色，正面 walk cycle"
}
```

后端读取 `base_candidate_id` 对应候选的 `style_lock_palette_b64`、源任务用户原 prompt、候选 sheet 或代表帧作为参考图，组合成新任务请求。派生任务的 `style_lock_source_task` 自动指向源任务，`style_lock_source_candidate_id` 指向 `base_candidate_id`。如果该候选尚未提取调色板，后端先运行 `StyleLockExtractor`；提取失败时派生任务退化为不锁定量化，并记录 `style_lock_applied=false`。

生成完成后，前端通过 `GET /tasks/{task_id}` 获取候选与帧列表：

```json
{
  "task_id": "550e8400-...",
  "status": "done",
  "frame_layout": "walk_cycle",
  "frame_grid": [1, 4],
  "sheet_size": [1024, 256],
  "candidates": [
    {
      "id": 1,
      "index": 0,
      "status": "done",
      "sheet_url": "/candidates/1/download?format=sheet",
      "frames": [
        { "id": 11, "frame_index": 0, "grid_pos": [0, 0], "split_quality": {"status": "ok", "flags": []}, "status": "done", "processed_url": "/frames/11/download" },
        { "id": 12, "frame_index": 1, "grid_pos": [0, 1], "split_quality": {"status": "ok", "flags": []}, "status": "done", "processed_url": "/frames/12/download" },
        { "id": 13, "frame_index": 2, "grid_pos": [0, 2], "split_quality": {"status": "ok", "flags": []}, "status": "done", "processed_url": "/frames/13/download" },
        { "id": 14, "frame_index": 3, "grid_pos": [0, 3], "split_quality": {"status": "ok", "flags": []}, "status": "done", "processed_url": "/frames/14/download" }
      ]
    }
  ]
}
```

MVP 如果先采用同步实现，接口响应也应保持上述任务状态结构，避免后续改异步时破坏前端契约。

---

## 8. 前端页面

单页三栏布局：

```
┌─────────────────┬──────────────────────────┬────────────┐
│  左栏            │  候选区域                 │  历史记录  │
│                  │                          │            │
│  素材类型        │  候选 0 (sheet 缩略图)    │  2026-06   │
│  ○ 角色          │  ┌──┬──┬──┬──┐           │  > 骑士    │
│  ○ 道具          │  │f0│f1│f2│f3│ 收藏 ♡   │   walk_cycle│
│  ● 图标          │  └──┴──┴──┴──┘           │  > 宝箱    │
│  ○ 地块          │  下载 sheet ↓ / 全帧 zip ↓│   sprite_pk│
│                  │                          │  > 草地    │
│  布局            │  候选 1                   │            │
│  ● single        │  ┌──┬──┬──┬──┐           │            │
│  ○ idle          │  │f0│f1│f2│f3│           │            │
│  ○ walk_cycle    │  └──┴──┴──┴──┘           │            │
│  ○ turnaround    │                          │            │
│  ○ sprite_pack   │  候选 2 / 候选 3...       │            │
│                  │                          │            │
│  描述            │  ─────────────────       │            │
│  ┌────────────┐  │  ▸ 派生菜单（基于本任务） │            │
│  │            │  │   - walk_cycle           │            │
│  └────────────┘  │   - turnaround           │            │
│                  │   - sprite_pack          │            │
│  数量  [按布局默认]│                          │            │
│  尺寸  [64]      │                          │            │
│  色数  [16]      │                          │            │
│                  │                          │            │
│  [ 生 成 ]       │                          │            │
└─────────────────┴──────────────────────────┴────────────┘
```

- 左栏选择 `frame_layout` 决定本次生成的帧布局；候选区按候选展示，每个候选下方平铺该候选的所有帧
- 单帧失败时该帧位置展示失败占位，不影响其他帧的展示与下载
- 候选下方"派生"按钮基于当前候选作为风格基底创建派生任务（自动带 `style_lock_source_task`、`style_lock_source_candidate_id` 与 `base_candidate_id`）
- 点击"生成"后按钮变 loading 状态，候选区显示骨架占位
- 前端轮询 `GET /tasks/{task_id}`，状态为 `done` 或 `partial_failed` 时填充已完成候选与帧
- 历史记录按任务列出，点击展开候选与帧；派生任务在历史记录里以缩进形式挂在源任务下

---

## 9. 配置文件

```yaml
# config.yaml
relay:
  base_url: "https://your-relay.example.com/v1"
  api_key_env: "GEMINI_API_KEY"   # 从 .env 读取

generator:
  backend: gemini                  # 后续改为 comfyui 时只改这里
  model: "gemini-3.1-flash-image-preview"

generation:
  default_count: 2
  default_counts_by_layout:        # 多帧任务默认少生成，避免一次任务产生过多帧和 API 成本
    single: 2
    idle: 1
    walk_cycle: 1
    turnaround: 1
    sprite_pack: 2
  default_frame_layout: single     # single/idle/walk_cycle/turnaround/sprite_pack
  timeout_seconds: 30
  daily_limit: 100              # 本地自用成本保护，可按需关闭

output:
  default_size: 64                 # 支持 32 / 64 / 128
  default_cell_size: 256           # 生成 sheet 中每个格子的目标尺寸，sheet 尺寸由 grid × cell_size 推导
  palette_colors: 16
  canvas_padding: 4                # 抠图居中时主体四周留白像素数
  enforce_style_lock: true         # 派生任务强制使用源候选调色板量化；只保证颜色集合，不保证形体一致

frame_layouts:                      # 帧布局尺寸预设，配合 prompt/frame_layouts/*.yaml 使用
  single: [1, 1]
  idle: [1, 2]
  walk_cycle: [1, 4]
  turnaround: [3, 3]
  sprite_pack: [2, 2]

paths:
  templates_dir: "backend/prompt/templates"
  frame_layouts_dir: "backend/prompt/frame_layouts"
  assets_dir: "assets"
  db_path: "data/meowasen.db"
```

---

## 10. 错误处理

| 情况 | 处理方式 |
|---|---|
| 用户输入为空 | 前端阻止提交；后端 422 校验 |
| 用户输入超 500 字符 | 前端截断提示；后端截断拼 prompt |
| API 超时 / 网络失败 | 若无候选成功则任务标记 `failed`；若已有候选成功则标记 `partial_failed`，错误写入任务和候选记录 |
| Provider 不支持参考图 | 派生任务继续执行文本生成 + 锁定调色板量化，记录 `reference_applied=false`，不把图片 base64 拼进 prompt |
| Provider 不支持自定义尺寸 | 使用 Provider 默认尺寸或最近支持尺寸，记录实际 `sheet_width/sheet_height`；切片失败时候选 `partial_failed` |
| Provider 不支持 seed | 忽略 seed 并记录 `provider_metadata.seed_applied=false` |
| sprite sheet 内主体跨越网格分隔 | 切片仍按 `frame_grid` 等分，依赖 `crop_to_subject` 的 alpha bbox 兜底重新定位；裁切结果异常时该帧标 `failed`，其他帧继续 |
| 实际帧数不符合 `frame_grid`（AI 出现合并或缺帧） | 检测每个网格 alpha 占比和 bbox 边界，写入 `split_quality`；异常帧标记 `failed`，候选状态置 `partial_failed` |
| `style_lock_source_candidate_id` 调色板缺失 | 先尝试从该候选提取；仍失败时派生任务退化为不锁定量化，并在 `meta.json` 标 `style_lock_applied: false` |
| rembg 失败 | 跳过抠图步骤，保存原始 raw PNG，`bg_removed: false` |
| 单帧后处理其他步骤失败 | 保存 raw 帧，帧标记 `failed`，错误写入 SQLite 与 meta.json；其他帧继续处理 |
| ComfyUI 未启动（未来本地模式） | 启动时检测 `/system_stats` 端点，不可达时给明确提示 |
