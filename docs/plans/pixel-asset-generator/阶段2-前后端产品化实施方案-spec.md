# Meowasen 阶段 2：前后端产品化实施方案

## 1. 阶段目标

阶段 2 在阶段 1 的核心生成闭环之上，实现本地 Web 应用能力。

阶段 2 不重新实现生成、切片、后处理和 style lock 逻辑，而是把阶段 1 的核心能力封装为：

```text
FastAPI 后端
SQLite 元数据仓库
异步任务状态
Vite 单页前端
历史记录
候选收藏
派生生成
下载与重处理
```

阶段 2 完成后，用户应能在浏览器中完成：

```text
输入 prompt
→ 选择素材类型和布局
→ 发起生成
→ 查看候选和帧
→ 收藏候选
→ 基于候选派生 walk_cycle / sprite_pack / turnaround
→ 下载单帧、sheet 或 zip
→ 查看历史任务
```

---

## 2. 阶段依赖

阶段 2 开始前，阶段 1 必须已经提供以下稳定能力：

1. `core_generation.generate()`
2. `core_generation.reprocess()`
3. `StyleLockExtractor`
4. `PromptBuilder`
5. `GeneratorProtocol`
6. `PostProcessor`
7. `SpriteSheetSplitter`
8. 文件系统产物结构
9. `meta.json`

阶段 2 的后端只负责任务编排、状态管理、查询、下载和调用阶段 1 核心能力。

---

## 3. 范围

### 3.1 范围内

1. FastAPI 应用入口。
2. SQLite schema 与迁移初始化。
3. `TaskRepository`。
4. `TaskService`：
   - 创建任务
   - 后台执行任务
   - 同步阶段 1 产物到 SQLite
   - 状态聚合
   - 派生任务
   - 重处理任务
5. API 接口：
   - `/tasks`
   - `/tasks/{task_id}`
   - `/tasks/{task_id}/regenerate`
   - `/tasks/{task_id}/derive`
   - `/candidates/{id}/reprocess`
   - `/candidates/{id}`
   - `/candidates/{id}/download`
   - `/frames/{id}/download`
6. Vite 单页前端。
7. 任务轮询。
8. 候选/帧预览。
9. 收藏候选。
10. 派生菜单。
11. 历史记录。
12. 单帧下载、sheet 下载、zip 下载。

### 3.2 范围外

1. 账号系统。
2. 云端部署。
3. 多用户协作。
4. 支付和额度系统。
5. 素材商城。
6. 在线编辑器。
7. 手动裁切框 UI。
8. 深度 Unity/Godot 工程导出。

---

## 4. 后端实施

### 4.1 FastAPI 目录结构

```text
backend/
├── main.py
├── api/
│   ├── routes.py
│   └── schemas.py
├── service/
│   ├── core_generation.py      # 阶段 1 复用
│   └── task_service.py
├── repository/
│   ├── models.py
│   ├── migrations.py
│   └── task_repo.py
├── storage/
│   ├── manifest.py
│   └── paths.py
└── ...
```

### 4.2 SQLite 表结构

阶段 2 将阶段 1 的 `meta.json` 映射进 SQLite。SQLite 是查询索引和 UI 状态来源，文件系统仍然是图片产物来源。

**tasks**

| 列 | 类型 | 说明 |
|---|---|---|
| task_id | TEXT PK | UUID |
| created_at | TEXT | ISO8601 |
| updated_at | TEXT | ISO8601 |
| asset_type | TEXT | character/prop/icon/tile |
| frame_layout | TEXT | single/idle/walk_cycle/turnaround/sprite_pack |
| frame_grid_rows | INTEGER | |
| frame_grid_cols | INTEGER | |
| cell_size | INTEGER | |
| sheet_width | INTEGER | |
| sheet_height | INTEGER | |
| style_lock_source_task | TEXT | NULL 表示基底任务 |
| style_lock_source_candidate_id | INTEGER | NULL 表示未使用候选级风格锁 |
| style_lock_applied | INTEGER | 0/1 |
| reference_applied | INTEGER | 0/1 |
| user_prompt | TEXT | |
| enhanced_prompt | TEXT | |
| model | TEXT | |
| output_size | INTEGER | |
| palette_colors | INTEGER | |
| status | TEXT | pending/running/done/partial_failed/failed |
| error | TEXT | |

**candidates**

| 列 | 类型 | 说明 |
|---|---|---|
| id | INTEGER PK | |
| task_id | TEXT FK | |
| index | INTEGER | |
| sheet_path | TEXT | |
| style_lock_palette_b64 | TEXT | |
| style_lock_histogram_json | TEXT | |
| favorited | INTEGER | 0/1 |
| status | TEXT | running/done/partial_failed/failed |
| error | TEXT | |

**frames**

| 列 | 类型 | 说明 |
|---|---|---|
| id | INTEGER PK | |
| candidate_id | INTEGER FK | |
| frame_index | INTEGER | |
| grid_row | INTEGER | |
| grid_col | INTEGER | |
| raw_path | TEXT | |
| processed_path | TEXT | |
| bg_removed | INTEGER | 0/1 |
| split_quality_json | TEXT | |
| status | TEXT | done/failed |
| error | TEXT | |

建议索引：

```sql
CREATE INDEX idx_tasks_created_at ON tasks(created_at);
CREATE INDEX idx_tasks_asset_type ON tasks(asset_type);
CREATE INDEX idx_tasks_frame_layout ON tasks(frame_layout);
CREATE INDEX idx_candidates_task_id ON candidates(task_id);
CREATE INDEX idx_frames_candidate_id ON frames(candidate_id);
```

### 4.3 TaskRepository

职责：

1. 创建任务、候选、帧记录。
2. 更新任务状态。
3. 查询任务详情。
4. 查询历史任务。
5. 收藏/取消收藏候选。
6. 从 `meta.json` 同步任务数据到 SQLite。

阶段 2 启动时应提供一次轻量自检：

```text
SQLite 不存在 → 创建 schema
assets 中有 meta.json 但 SQLite 无记录 → 可选择导入
```

### 4.4 TaskService

`TaskService` 调用阶段 1 的核心能力，不直接操作图像生成细节。

生成流程：

```text
POST /tasks
→ TaskRepository 创建 pending 任务
→ 后台任务更新 running
→ 调用 core_generation.generate()
→ 读取/同步 meta.json 到 SQLite
→ 聚合任务状态
```

派生流程：

```text
POST /tasks/{task_id}/derive
→ 校验 base_candidate_id 属于源任务
→ 若候选无 style_lock，则运行 StyleLockExtractor
→ 根据 ProviderCapabilities 判断是否传参考图
→ 创建新任务
→ 调用 core_generation.generate()
→ 同步结果
```

重处理流程：

```text
POST /candidates/{id}/reprocess
→ 定位候选 sheet
→ 调用 core_generation.reprocess()
→ 更新 frames / candidates / meta.json / SQLite
```

### 4.5 后台执行方式

MVP 可使用 FastAPI `BackgroundTasks` 或应用内 `asyncio.create_task`。不引入 Celery、Redis 或外部队列。

要求：

1. 后端重启后，`running` 超时任务应标记为 `failed` 或 `partial_failed`。
2. 同一时间并发生成任务数受配置限制。
3. API 超时和 Provider 失败不影响已成功候选。

建议配置：

```yaml
server:
  max_concurrent_generation_tasks: 1
  running_task_timeout_minutes: 30
```

---

## 5. API 实施

### 5.1 创建任务

`POST /tasks`

请求：

```json
{
  "asset_type": "character",
  "frame_layout": "single",
  "user_prompt": "蓝色披风的小骑士，俯视角，像素风",
  "count": 2,
  "output_size": 64,
  "palette_colors": 16
}
```

响应：

```json
{
  "task_id": "550e8400-...",
  "status": "pending",
  "poll_url": "/tasks/550e8400-..."
}
```

### 5.2 查询任务

`GET /tasks/{task_id}`

响应必须包含：

1. 任务状态。
2. frame layout。
3. sheet size。
4. 候选列表。
5. 每个候选的帧列表。
6. `split_quality`。
7. 下载 URL。

### 5.3 历史列表

`GET /tasks`

支持过滤：

```text
asset_type
frame_layout
favorited
status
```

分页参数：

```text
limit
offset
```

### 5.4 派生任务

`POST /tasks/{task_id}/derive`

请求：

```json
{
  "frame_layout": "walk_cycle",
  "base_candidate_id": 7,
  "user_prompt_extra": "保持原角色，正面 walk cycle"
}
```

行为：

1. 自动使用 base candidate 的 `style_lock_palette_b64`。
2. Provider 支持参考图时传入参考图。
3. Provider 不支持参考图时记录 `reference_applied=false`。
4. 新任务挂在源任务下。

### 5.5 重处理

`POST /candidates/{id}/reprocess`

请求：

```json
{
  "output_size": 128,
  "palette_colors": 32,
  "canvas_padding": 6,
  "use_style_lock": true
}
```

### 5.6 收藏候选

`PATCH /candidates/{id}`

请求：

```json
{
  "favorited": true
}
```

收藏候选时，如果该候选没有 `style_lock_palette_b64`，后端可立即触发 `StyleLockExtractor`。

### 5.7 下载接口

1. `GET /frames/{id}/download`：下载单帧 processed PNG。
2. `GET /candidates/{id}/download?format=sheet`：下载拼接后的 processed sheet。
3. `GET /candidates/{id}/download?format=zip`：下载所有 processed frames 的 zip。

---

## 6. 前端实施

### 6.1 页面布局

单页三栏：

```text
左栏：生成参数
中栏：候选与帧预览
右栏：历史记录
```

左栏字段：

1. 素材类型：character/prop/icon/tile。
2. 帧布局：single/idle/walk_cycle/turnaround/sprite_pack。
3. prompt 输入。
4. 数量。
5. 输出尺寸。
6. 色数。
7. 生成按钮。

中栏：

1. 候选 sheet 缩略图。
2. 每个候选下的 frames。
3. 单帧失败占位。
4. split quality 警告。
5. 收藏按钮。
6. 下载按钮。
7. 派生菜单。

右栏：

1. 历史任务列表。
2. 任务状态。
3. 任务 frame layout。
4. 派生任务缩进挂在源任务下。

### 6.2 交互流程

生成：

```text
填写参数
→ 点击生成
→ POST /tasks
→ 中栏显示 loading
→ 轮询 GET /tasks/{task_id}
→ done/partial_failed 后展示候选与帧
```

收藏：

```text
点击候选收藏
→ PATCH /candidates/{id}
→ 后端提取 style lock
→ UI 标记为风格基底可用
```

派生：

```text
点击候选派生
→ 选择目标 frame_layout
→ POST /tasks/{task_id}/derive
→ 新任务进入历史记录
→ 轮询新任务
```

重处理：

```text
调整输出尺寸/色数
→ POST /candidates/{id}/reprocess
→ 更新候选帧预览
```

---

## 7. 里程碑

### M1：后端 API 骨架与 SQLite

交付：

1. FastAPI app。
2. SQLite schema 初始化。
3. `TaskRepository` 基础 CRUD。
4. `GET /tasks` 与 `GET /tasks/{task_id}`。

验收：

```text
能把阶段 1 已生成的 meta.json 导入 SQLite，并通过 API 查询。
```

### M2：创建任务与后台生成

交付：

1. `POST /tasks`。
2. 后台调用 `core_generation.generate()`。
3. 状态从 pending → running → done/partial_failed/failed。
4. API 查询返回候选与帧。

验收：

```text
通过 HTTP 创建任务后，无需命令行即可生成素材并查询结果。
```

### M3：前端生成页

交付：

1. Vite 单页。
2. 左栏参数输入。
3. 中栏候选/帧展示。
4. 轮询任务状态。
5. 错误和 partial_failed 展示。

验收：

```text
浏览器中输入 prompt 后，能看到生成出的 processed PNG。
```

### M4：收藏、style lock 与派生任务

交付：

1. 收藏候选。
2. 收藏时提取 style lock。
3. 派生菜单。
4. `/derive` 接口。
5. 派生任务历史挂载。

验收：

```text
用户收藏一个角色候选后，可以基于它派生 walk_cycle 或 sprite_pack。
```

### M5：下载与重处理

交付：

1. 单帧下载。
2. sheet 下载。
3. zip 下载。
4. 候选重处理。

验收：

```text
用户可以下载单帧 PNG、整张 sheet 或全帧 zip，并能调整色数/尺寸重处理。
```

---

## 8. 阶段 2 验收标准

1. 后端启动后可以初始化 SQLite。
2. 前端可以创建 `single` 任务并展示结果。
3. 前端可以创建 `sprite_pack` 任务并展示 2x2 帧。
4. 任务状态能正确显示 `pending/running/done/partial_failed/failed`。
5. 单帧失败不影响同候选其他帧展示。
6. 收藏候选后能提取 style lock。
7. 派生任务能复用源候选调色板。
8. Provider 不支持参考图时，UI 能展示 `reference_applied=false` 或等价提示。
9. 历史记录能按时间列出任务。
10. 派生任务能挂在源任务下。
11. 单帧、sheet、zip 下载可用。
12. 重处理不会重新调用图像生成 API。

---

## 9. 实施原则

1. 阶段 2 不改阶段 1 核心生成语义。
2. SQLite 是索引和 UI 状态层，不是图片真实来源。
3. 文件系统中的 `meta.json` 仍保留，便于脱离 Web 运行和故障恢复。
4. API 返回结构应稳定，前端不直接读取本地文件路径。
5. MVP 只做本地单用户应用，不做公网安全模型。
6. 派生一致性只承诺“尽量一致”，不承诺角色形体完全一致。
