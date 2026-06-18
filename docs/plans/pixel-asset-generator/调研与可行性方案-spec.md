# Meowa.ai 调研与自用版可行性方案

> 本文是对 [PRD-草稿.md](./PRD-草稿.md) 的补充：聚焦 meowa.ai 已观察到的能力面，以及 PRD MVP 范围之外的进阶能力（多视图、动画、套件）该如何分阶段引入。MVP 范围内的功能详见 PRD 草稿，本文不重复。
>
> **当前约束（2026-06-16）**：本机无独显，PRD 中"本地 ComfyUI + SD1.5 + Pixel LoRA"路线暂时无法启动。计划 3 个月内配 ≥8G 显存独显。整体方案改为**先走全云端 API，独显到位后切本地**，**生成层做抽象接口，业务/UI/后处理/存储代码不动**。多视图、套件、短循环动画在阶段 1 就用「单次 sprite sheet 生成 + 调色板锁定」机制解决，不等独显。

## 1. Meowa.ai 是什么

定位：面向独立游戏开发者的 AI 像素美术工作台。卖点是"风格一致 + 引擎可用"，在一个工作流里把概念到可入引擎的素材这一段闭环掉。

主页域名 `meowa.ai`，`meowart.ai` 是 301 重定向别名。免登录可浏览 `gallery`，约 600 条 UGC（25 页），是真实在产出资产，不是纯营销页。

### 1.1 公开宣传的三块核心能力

| 能力 | 官方描述 | 从 gallery 推断 |
|---|---|---|
| Pixel characters | "readable silhouettes, stronger poses" | 单角色像素立绘、纯/白底，32–128 px |
| Sprite packs | "matching props, tilesets, environment pieces" | 同风格批量产出，调色一致 |
| Animation direction | "key poses and variations" | 透明底循环动画 webp（idle 呼吸、walk cycle） |

### 1.2 从 gallery 推断的隐藏能力

按分类数排序（合计约 600 条）：

- Illustration **305**
- Videos & Storyboards **99** — 有视频/分镜
- Sound Effect **71** — 有音效生成
- Character Design **64**
- Product Design **46**
- UI Layout **15**

URL 里出现过的工具/产物名：
- `character_multi_view_generator` — 角色多视图（3×3 turnaround 网格）
- `sprite_pack_preview.png` — 套件批量预览
- `transparent_backup.webp` — 透明底 idle/walk 循环动画

提示词里常见"能直接导入到 unity 中使用"，说明对接 Unity / Godot / RPG Maker 工作流。

### 1.3 技术背景推断

Meowa 的"风格一致 + 角色 consistency"卖点，与 Nano Banana（Gemini 2.5 Flash Image）的 character consistency 原生能力高度吻合。它的商业模式是付费图像 API 的包装层 + 像素化后处理 + 工作流 UI，并不需要自有大模型。这意味着自用版**走云端 API 路线和 meowa 实际是同类方案**，不是绕远路。

## 2. 自用版各阶段覆盖范围

| meowa 能力 | PRD 是否覆盖 | 优先级 | 对应阶段 |
|---|---|---|---|
| 单角色像素图 | ✅ MVP | P0 | 阶段 1（云端单图） |
| 道具/图标 | ✅ MVP | P0 | 阶段 1（云端单图） |
| 地块参考图 | ✅ MVP | P0 | 阶段 1（云端单图） |
| 调色一致的 sprite pack | 部分 | P0 | 阶段 1（云端 2×2 sprite sheet + 调色板锁定） |
| 角色多视图 turnaround | ❌ | P0 | 阶段 1（云端 3×3 sprite sheet 单次出图） |
| Idle / walk 循环动画 | ❌（PRD 范围外） | P1 | 阶段 1（云端 1×N walk cycle sprite sheet） |
| 长动画 / 复杂打斗动作（>8 帧） | ❌ | P2 | 阶段 3 |
| 视频/分镜、音效、UI | ❌ | P3 | 不做 |

P3 对自用工具性价比低（SFX 用 sfxr/jsfxr；UI 手摆；视频用专门工具），不合并进来。多视图、套件、循环动画依靠"单次扩散过程内的上下文一致性"在阶段 1 就能落地：让 Gemini 一次出一张 N×M 的 sprite sheet，然后用代码切片+锁定调色板量化。

## 3. 阶段 1：全云端 MVP（当前阶段，无独显）

### 3.1 生成后端：中转站 Gemini（Nano Banana 系列）

**为什么选它：**
- 通过个人中转站调用，已验证 `gemini-3.1-flash-image-preview` 可用
- 单张图像默认 1024×1024，足够给后续 N×M sprite sheet 切片留余量
- Character consistency 是官方主打能力，直接对应 meowa 的核心卖点；同一次生成内的多帧上下文也由扩散过程天然保持
- 无需本地 GPU，今天就能开始

**像素化流程（含多帧 sprite sheet 路径）：**

云端只负责出"风格正确的素材或 sprite sheet"，像素化在本地用 Pillow + rembg 做。Pillow 单张 < 100ms，rembg CPU 模式单张 1–3 秒，无需 GPU。

```
用户输入 prompt + 选定 frame_layout（single / walk_cycle / turnaround / sprite_pack ...）
   ↓
PromptBuilder：素材模板 + 帧布局描述（"4-frame walk cycle, horizontal, no overlap"）
   ↓
单次调用 gemini-3.1-flash-image-preview（n=1，多候选用 asyncio.gather 并发）
   ↓
返回 1024×1024 原图（实际是包含 N×M 帧的 sprite sheet）
   ↓
本地切片（按 frame_grid）→ 逐帧后处理：
       rembg 抠图 → 最近邻缩放 → 调色板量化（可锁定调色板）→ 居中裁切
   ↓
输出多张 64×64 透明 PNG，保留原始 sheet 与逐帧 raw
```

`frame_layout=single` 时退化为单图路径，行为与最朴素的 MVP 完全一致。

### 3.2 多帧 sprite sheet + 调色板锁定

这是阶段 1 内做到 sprite pack/turnaround/walk cycle 一致性的关键机制，依赖两个事实：

1. **同一次扩散过程内的帧间一致性是结构性保证的**：N×M sprite sheet 是一次生成的产物，模型在画第 K 帧时"看得见"前面所有帧，身份、服装、调色板天然对齐；这比"分别生成 N 张再用 IPAdapter 拼一致"靠谱得多。
2. **像素质量反而更高**：1024×256 的 1×4 walk sheet，单帧源像素 256×256，缩到 64×64 是 4:1，比单图 1024→64 的 16:1 缩放保留更多细节。

锁定调色板（`style_lock`）解决跨任务一致性：

- 第一张候选完成量化后，由 `StyleLockExtractor` 提取 16 色调色板（PIL `Image.getpalette()`）和色彩直方图
- 派生任务（基于该候选生成 walk cycle / turnaround / 套件）自动带上调色板，后处理量化时强制使用 → 不同任务出来的角色颜色完全一致
- meowa 那种"看起来出自同一个美工"的视觉效果，约 70% 来自调色板一致；剩下的笔触/光影由"同模型 + 相近 prompt"自然保证

衍生任务的参考图通过 Gemini 编辑模式（或参考图 inline）传入，进一步强化角色身份一致性。

### 3.3 生成层抽象接口（关键设计）

独显到位后，从云端切到本地只需换生成层实现，不动其他代码。

```python
# generator/base.py
class ImageGenerator:
    def generate(self, prompt: str, params: GenerateParams) -> list[Image]:
        raise NotImplementedError

# generator/gemini.py
class GeminiGenerator(ImageGenerator):
    def generate(self, prompt, params): ...  # 调 Gemini API

# generator/comfyui.py（独显到位后实现）
class ComfyUIGenerator(ImageGenerator):
    def generate(self, prompt, params): ...  # 调本地 ComfyUI HTTP API
```

配置文件里加一行 `generator: gemini | comfyui` 切换，业务层不感知差异。

### 3.4 像素化后处理（CPU，无需 GPU）

推荐 Pillow + rembg 自己写，不依赖 ComfyUI 节点（因为阶段 1 没有 ComfyUI）。后续引入 ComfyUI 时，可以把后处理搬进节点图，也可以继续跑 Python，两者输出等价。

后处理步骤（多帧任务统一走切片流程，单图 = 1×1 sheet）：

1. `split_sprite_sheet(frame_grid)`：按网格等切原图为 N 张子图；frame_grid=(1,1) 时直通
2. `rembg`（u2net 或 isnet-anime）去背景 → 透明 PNG
3. 找主体 bounding box，加 2px 边距裁切
4. 将主体等比缩放到目标尺寸内，四周补透明（居中）
5. 最近邻缩放（PIL `Image.NEAREST`）到 32/64/128
6. 调色板量化（PIL `Image.quantize(colors=N, palette=style_lock)`），有锁定调色板时强制使用，默认 16 色

如果抠图失败（rembg 置信度低），保留原图并在 meta.json 标记 `bg_removed: false`，不中断任务。单帧失败只标记该帧，同批其他帧继续。

### 3.5 入口形式

建议：**CLI + 本地 Web（FastAPI + 静态 HTML）**

- CLI 用来跑单张验证和批量生成
- Web 页面用来预览候选图、点击保留/废弃/重新生成、对收藏的候选发起派生（walk cycle / turnaround / sprite pack）
- 不做桌面应用（无独显机器做应用分发没意义）

目录结构详见 [设计方案-spec.md](./设计方案-spec.md) §6。

## 4. 阶段 2：本地原生像素生成（独显到位后，约 3 个月后）

独显到位后开启本地 ComfyUI，生成层切 `ComfyUIGenerator`，sprite sheet/调色板锁定/前端/存储/任务编排全部不动。阶段 2 的目标不再是"补齐 meowa 的多视图/套件"——这些在阶段 1 已落地——而是用本地原生像素生成换两件事：**单帧像素质量**（不再是大图缩小）和**运行成本**（电费替代 API 费用）。

### 4.1 单帧质量提升

SD1.5 + Pixel Art LoRA 直接以 64×64 / 128×128 原生分辨率生成，每个像素都是模型有意放置的结果，轮廓硬、颜色块状、网格对齐，质量上限明显高于"1024 大图最近邻缩小"。这是阶段 2 最核心的价值。

阶段 2 的 sprite sheet 生成依然走"单次扩散一次出多帧"路径，由 SD1.5 + ControlNet OpenPose 网格控制每帧姿势，依赖同一次推理的上下文一致性。Pixel Art LoRA + ControlNet + 8G 显存可同时跑通；显存吃紧时打开 `--lowvram`。

### 4.2 Godot/Unity 导出

阶段 2 加 `.aseprite` 导出（用 `aseprite-cli` 或直接写 `.ase` 二进制），基本满足两个引擎的导入需求。阶段 1 只导出 PNG 与 sheet。

## 5. 阶段 3：长动画 / 复杂动作（较晚，独显到位后评估）

阶段 1 的 sprite sheet 路径已经覆盖 idle、walk cycle、turnaround、套件这些"短循环 + 一致性"场景。阶段 3 处理 sprite sheet 单图分辨率不够分的情况：超过 8 帧的长循环、复杂打斗动作、大变形动画。开源方案按落地难度：

1. **AnimateDiff（SD1.5 motion module）**：8G 显存能跑 16 帧 512×512。输出是连续视频质感，需要帧抽取 + 逐帧像素量化 + walk cycle 闭合校验，工程量不小。
2. **逐帧规则化生成**：idle 呼吸 = y 轴 1px 起伏；多帧间用 Pillow 插值。已经在阶段 1 sprite sheet 路径里覆盖到 4–9 帧；阶段 3 用来给阶段 1 的关键帧做帧间补间。
3. **Wan2.2 Animate**：质量好，但 8G 不现实，依赖云端。

动画建议另开 `pixel-animator` 模块，不回塞 MVP 工程。

## 6. 技术栈汇总

```
阶段 1（当前，无独显）
┌──────────────────────────────────────────────────────────┐
│ 前端：单页 HTML + 少量 JS（或 Vite），支持帧布局与派生入口  │
│ 后端：Python FastAPI + 异步任务编排                       │
│ 生成：中转站 OpenAI 兼容端点 → gemini-3.1-flash-image-preview │
│       单次出 1×1 / 1×2 / 1×4 / 2×2 / 3×3 sprite sheet     │
│ 后处理：Pillow + rembg（纯 CPU），含 sprite sheet 切片     │
│         + StyleLockExtractor（调色板提取与锁定量化）       │
│ 存储：本地文件 + SQLite（tasks / candidates / frames 三表）│
└──────────────────────────────────────────────────────────┘

阶段 2（独显到位后，切换生成层）
┌──────────────────────────────────────────────────────┐
│ 生成层换：ComfyUI HTTP API（本地，--listen 模式）      │
│ 模型：SD1.5 + Pixel Art LoRA + ControlNet（多帧网格） │
│ 后处理：可搬进 ComfyUI 节点，或继续用 Pillow           │
│ 其余不动：前端/FastAPI/sprite sheet 路径/存储/抽象接口 │
└──────────────────────────────────────────────────────┘

阶段 3（长动画 / 复杂动作，独显到位后评估）
┌──────────────────────────────────────────────────────┐
│ AnimateDiff（SD1.5）作为 sprite sheet 的补充         │
│ 处理 >8 帧长循环、打斗动作                            │
│ 建议另开 pixel-animator 模块                         │
└──────────────────────────────────────────────────────┘
```

## 7. 成本评估

| 场景 | 单次调用成本（中转站参考价） | 月成本估算（自用） |
|---|---|---|
| 单图任务（1×1 single） | 1 张 API 调用费 | 取决于探索强度 |
| walk cycle / idle / sprite_pack（≤4 帧） | 1 张 API 调用费即得 N 帧 | 与单图任务同价 |
| turnaround 3×3 | 1 张 API 调用费即得 9 视图 | 与单图任务同价 |
| 重新跑后处理（reprocess） | $0 | 不调用 API |
| 派生任务（带 style_lock） | 1 次调用费 | 同上 |
| 阶段 2 本地 SD1.5（独显到位后） | 电费可忽略 | ~$0 |

sprite sheet 路径的关键成本特性：**多帧任务和单图任务花同样的 API 钱**。生成 1 套 walk cycle 4 帧、1 套 9 视图 turnaround、1 套 4 件 sprite pack，都是 1 次调用，这是阶段 1 性价比的最大来源。具体单次费率以中转站当时计费为准。

## 8. 与 PRD 草稿的衔接

PRD §9.3 的 6 个待确认问题，建议：

1. **入口形式**：CLI + 本地 Web（FastAPI + 静态页），不做桌面应用
2. **ComfyUI 安装脚本**：阶段 1 不需要。阶段 2 到位时提供 `setup-comfyui.md`
3. **第三方 Pixel Art LoRA**：允许，只用 license 明确的（CivitAI 上的 PublicPrompts Pixel Art LoRA、All-In-One-Pixel-Model 等），来源写进 `models/README.md`
4. **默认尺寸**：64×64，128×128 可选，地块参考图除外
5. **目录结构**：`assets/{task_id}/`，每任务目录含 `meta.json` + sheet 原图 + 切片帧 raw + 后处理帧（详见 [设计方案-spec.md §6](./设计方案-spec.md)）。素材类型不再作为目录层级，而是作为任务字段，便于跨类型按风格指纹串联派生任务
6. **Godot/Unity 导出**：阶段 1 只导出 PNG（单帧）和 sheet（多帧），阶段 2 加 `.aseprite`

## 9. 风险

| 风险 | 影响 | 缓解 |
|---|---|---|
| 中转站 `gemini-3.1-flash-image-preview` 下线或定价变化 | 阶段 1 成本变化 | 接口已抽象为 GeneratorProtocol，可切到中转站其它图像模型或 fal.ai / Replicate |
| Gemini 不严格按 frame_grid 出帧（合并帧、漏帧、重叠） | 多帧任务出错率 | prompt 强化网格分隔约束；后处理检测每个网格 alpha 占比，过低帧标记 failed；提供"重新生成"按钮 |
| Gemini 不理解动画语义（walk cycle 4 帧实际上是 4 个相似姿势） | 动画连贯性差 | 帧布局模板逐帧描述具体动作；先做 idle (1×2) 验证，再扩到 walk_cycle |
| 大图缩小后像素质量糊 | 单帧质量不达预期 | 多帧 sheet 路径自然降低缩放比（从 16:1 降到 4:1）；调整量化色数（16→32）；阶段 2 切本地 SD 解决 |
| 调色板锁定后派生任务颜色被压缩到不合适的色相 | 派生任务质量下降 | 提供"不锁定"开关；锁定使用感知色彩聚类替代 PIL 默认中位切割（视效果再决定） |
| 独显延迟到位（>3 个月） | 阶段 2 推迟 | 阶段 1 sprite sheet 路径覆盖了多视图、套件、短动画，不阻塞主功能 |
| 8G 显存阶段 2 跑 SD1.5 + ControlNet 多帧网格 OOM | 阶段 2 卡住 | 启用 `--lowvram`；多帧任务降级到分批生成 + 锁定调色板拼合 |
| LoRA license 商用限制 | 商用风险 | PRD 已声明自用；需商用时核查每个 LoRA 的 license |

## 10. 建议的下一步

1. 确认本文 §8 的 6 个待办答案（可以直接口头告诉我）
2. 起一份 `阶段1-实施计划-spec.md`，按依赖顺序拆成可执行单元：
   - GeneratorProtocol + GeminiOpenAICompatibleProvider（中转站连通验证）
   - PromptBuilder + 素材模板 + 帧布局模板（先 single，再 walk_cycle、turnaround、sprite_pack）
   - 后处理 pipeline：rembg + Pillow + SpriteSheetSplitter + StyleLockExtractor
   - SQLite tasks / candidates / frames 三表 + meta.json 同步
   - FastAPI 路由：tasks 创建 / 派生 / reprocess / 详情 / frames 下载
   - 单页 Web 预览：候选+帧网格展示、派生入口、历史记录
3. 先跑通最小闭环：`输入 prompt → 选 single layout → Gemini 出图 → 切片（1×1 退化）→ 本地像素化 → 输出 64×64 PNG` 命令行版本；通过后再加 walk_cycle 帧布局验证多帧路径

## 参考资料

- [Meowa 官网](https://meowa.ai/) — 主推三大功能
- [Meowa Gallery](https://meowa.ai/gallery) — 600 条 UGC，反推实际能力
- [Gemini 2.5 Flash Image API 免费层和定价](https://www.aifreeapi.com/en/posts/gemini-image-generation-free-api) — Nano Banana 计费数据
- [Nano Banana 官方定价](https://vercel.com/ai-gateway/models/gemini-2.5-flash-image)
- [PixelArt-Processing-Nodes-for-ComfyUI](https://github.com/GENKAIx/PixelArt-Processing-Nodes-for-ComfyUI) — 阶段 2 后处理节点
- [Stable Diffusion Art - Consistent Character View Angle](https://stable-diffusion-art.com/consistent-character-view-angle/) — 阶段 2 多视图参考
- [Onodofthenorth/SD_PixelArt_SpriteSheet_Generator](https://huggingface.co/Onodofthenorth/SD_PixelArt_SpriteSheet_Generator) — 阶段 2 SD1.5 像素模型参考
- [Open Source Sprite Generation Guide 2025](http://apatero.com/blog/open-source-sprite-generation-ai-complete-guide-2025) — 开源 sprite 生成方案综述
