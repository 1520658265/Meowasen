# ComfyUI 参考图生成接入方案

> 日期：2026-06-30
> 目标：把 RPG 像素风角色动画流水线中的“参考图生成”步骤，从云端生图 API 切换到本地 ComfyUI；视频生成、抽帧、像素化后处理暂时沿用当前已验证流程。

## 1. 当前结论

当前最稳的路线不是直接在 ComfyUI 里生成最终像素动画，而是：

```text
ComfyUI 生成干净 2D 角色参考图
 -> I2V 生成短动作视频
 -> 自动选帧
 -> 本地后处理像素化
 -> sprite sheet
 -> HTML 动画预览
```

原因：

- I2V 模型更容易保持“输入图的大体角色身份”，但会把硬像素图理解成低清/马赛克，因此输入给视频模型的参考图不应该是强像素图。
- 像素风应该主要由后处理保证，包括抠图、边缘收缩、描边、调色板量化、高光保护、透明背景和统一 cell 对齐。
- ComfyUI 更适合解决“角色参考图可控、可复现、可本地批量出图”的问题，不应该先承担视频一致性问题。
- 12GB 显存可以跑 SDXL 动漫系模型生成 1024 或 832 级别参考图，但不适合一开始上 Flux、Qwen Image、SD3.5 Large 或复杂多模型视频工作流。

因此，第一阶段只替换这一段：

```text
原方案：云端 API 生成 reference image
新方案：本地 ComfyUI 生成 reference image
```

后面的 `seedance -> postprocess_walk_videos.py -> preview.html` 先不动。

## 2. 角色风格策略

参考图建议使用“干净的 2D / Q 版 / RPG 角色设定图”，而不是直接生成硬像素图。

推荐风格：

```text
chibi, full body, front view, clean silhouette,
flat 2d game character design, RPG character reference,
simple neutral background
```

避免风格：

```text
pixel art, 16-bit, 8-bit, low resolution, mosaic, blocky
```

原因是视频模型会放大输入图中的块状结构，把像素风误读成“低质量画面”或“马赛克纹理”。我们最终需要的是“像素动画输出”，不是“像素图作为视频输入”。

## 3. 推荐模型栈

### 3.1 必装：主 checkpoint

首选：

```text
模型：animagine-xl-4.0-opt.safetensors
来源：https://huggingface.co/cagliostrolab/animagine-xl-4.0
文件：https://huggingface.co/cagliostrolab/animagine-xl-4.0/blob/main/animagine-xl-4.0-opt.safetensors
大小：约 6.94GB
放置：ComfyUI/models/checkpoints/animagine-xl-4.0-opt.safetensors
```

选择理由：

- SDXL 体系，ComfyUI 支持成熟。
- 动漫/Q 版角色质量稳定，适合生成角色设定图。
- 相比 Flux、Qwen Image、SD3.5 Large，更适合 12GB 显存做本地验证。
- 官方推荐参数接近我们的需求：`1024x1024`、`28 steps`、`CFG 5`、`Euler a`。

### 3.2 可选：IPAdapter，一致性增强

用于从一张正面参考图派生背面、左侧等方向图。

```text
模型：ip-adapter-plus_sdxl_vit-h.safetensors
来源：https://huggingface.co/h94/IP-Adapter/tree/main/sdxl_models
文件：https://huggingface.co/h94/IP-Adapter/blob/main/sdxl_models/ip-adapter-plus_sdxl_vit-h.safetensors
大小：约 848MB
放置：ComfyUI/models/ipadapter/ip-adapter-plus_sdxl_vit-h.safetensors
```

还需要 CLIP Vision 编码器：

```text
模型：model.safetensors
来源：https://huggingface.co/h94/IP-Adapter/tree/main/models/image_encoder
大小：约 2.53GB
建议重命名：CLIP-ViT-H-14-laion2B-s32B-b79K.safetensors
放置：ComfyUI/models/clip_vision/CLIP-ViT-H-14-laion2B-s32B-b79K.safetensors
```

对应 ComfyUI 节点：

```text
插件：ComfyUI_IPAdapter_plus
地址：https://github.com/cubiq/ComfyUI_IPAdapter_plus
放置：ComfyUI/custom_nodes/ComfyUI_IPAdapter_plus/
```

注意：

- 这个插件仓库目前处于维护模式，但仍可用于验证。
- IPAdapter 权重不要过高。建议先从 `0.45 - 0.65` 试。
- 权重太高，模型会拒绝转向，背面/侧面变不出来。
- 权重太低，服装、发型、伞、配色会漂。

### 3.3 可选：OpenPose ControlNet，姿态/方向增强

当背面、侧面或站姿控制不稳定时再加，不作为第一轮必需项。

```text
模型：diffusion_pytorch_model.safetensors
来源：https://huggingface.co/xinsir/controlnet-openpose-sdxl-1.0
文件：https://huggingface.co/xinsir/controlnet-openpose-sdxl-1.0/tree/main
大小：约 2.5GB
放置：ComfyUI/models/controlnet/diffusion_pytorch_model.safetensors
```

OpenPose 预处理节点：

```text
插件：comfyui_controlnet_aux
地址：https://github.com/Fannovel16/comfyui_controlnet_aux
放置：ComfyUI/custom_nodes/comfyui_controlnet_aux/
```

优先级：

```text
第一轮：不用 ControlNet，只用文字 prompt 出正面参考图
第二轮：用 IPAdapter 生成背面/左侧参考图
第三轮：如果方向不稳，再加 OpenPose ControlNet
```

## 4. 不建议第一阶段使用的模型

### Flux / Qwen Image / SD3.5 Large

不建议原因：

- 显存压力大。
- 工作流更复杂。
- 本阶段目标只是生成干净角色参考图，SDXL 已够用。

### MV-Adapter / 多视图模型

不建议第一阶段使用。

原因：

- 多视图模型确实理论上适合 front/back/side，但显存和节点依赖更重。
- 已知部分 SDXL 多视图工作流在 12GB 显存上容易接近或超过可用上限。
- 当前我们已有“左侧生成 + 右侧镜像”的省成本方案，不需要一开始上多视图模型。

### Pixel LoRA

不建议作为视频前置参考图使用。

原因：

- 容易生成看似像素、实际是马赛克/低清块的图。
- I2V 会继承这些块状纹理，生成视频后更难后处理。
- 像素化应放在视频抽帧之后做。

可以后续单独验证 Pixel LoRA，用途限定为“静态像素头像/道具”，不用于 I2V 输入。

## 5. 工作流 1：单张正面参考图

用途：生成可供 I2V 使用的正面角色参考图。

### 节点结构

```text
CheckpointLoaderSimple
 -> CLIP Text Encode Positive
 -> CLIP Text Encode Negative
 -> Empty Latent Image
 -> KSampler
 -> VAE Decode
 -> Save Image
```

### 推荐参数

```text
checkpoint: animagine-xl-4.0-opt.safetensors
width: 1024
height: 1024
batch_size: 1
steps: 28
cfg: 5
sampler: euler_ancestral / Euler a
scheduler: normal
denoise: 1.0
seed: 固定 seed，方便复现
```

如果 12GB 显存 OOM：

```text
width: 832
height: 832
batch_size: 1
steps: 24 - 28
不启用 refiner
不启用 hires fix
```

### 正向 prompt 模板

示例主题：打着伞的暗黑 Q 版女孩。

```text
1girl, chibi, young gothic girl, full body, front view, standing pose,
holding a black lace umbrella, black bob haircut, burgundy bow,
black and burgundy gothic dress, stockings, boots, moon charms,
simple neutral gray background, clean silhouette, centered,
flat 2d game character design, RPG character reference, safe,
masterpiece, high score, great score, absurdres
```

说明：

- 使用 `young gothic girl`、`chibi`、`safe`，只做完整着装的可爱 RPG 角色。
- `front view` 和 `full body` 必须保留。
- `simple neutral gray background` 有利于后续抠图。
- `flat 2d game character design` 用于压住 3D/写实倾向。

### 负向 prompt 模板

```text
lowres, bad anatomy, bad hands, text, logo, watermark, cropped,
extra limbs, missing fingers, deformed face, blurry, realistic photo,
3d render, heavy shadow, complex background, nsfw
```

## 6. 工作流 2：方向参考图

用途：从正面参考图派生背面、左侧参考图；右侧优先由左侧镜像得到。

### 节点结构

```text
Load Image
 -> CLIP Vision Encode
 -> IPAdapter Apply / IPAdapter Advanced
 -> CheckpointLoaderSimple
 -> CLIP Text Encode Positive
 -> CLIP Text Encode Negative
 -> Empty Latent Image
 -> KSampler
 -> VAE Decode
 -> Save Image
```

### 推荐参数

```text
checkpoint: animagine-xl-4.0-opt.safetensors
ipadapter: ip-adapter-plus_sdxl_vit-h.safetensors
clip_vision: CLIP-ViT-H-14-laion2B-s32B-b79K.safetensors
ipadapter_weight: 0.45 - 0.65
steps: 28
cfg: 5
size: 1024x1024，OOM 则 832x832
```

### 背面 prompt

```text
1girl, chibi, same young gothic girl, full body, back view,
standing pose, holding a black lace umbrella,
same black bob haircut, same burgundy bow, same black and burgundy gothic dress,
stockings, boots, moon charms, clean silhouette, centered,
simple neutral gray background, flat 2d game character design,
RPG character reference, safe,
masterpiece, high score, great score, absurdres
```

### 左侧 prompt

```text
1girl, chibi, same young gothic girl, full body, left-facing side view,
standing pose, holding a black lace umbrella,
same black bob haircut, same burgundy bow, same black and burgundy gothic dress,
stockings, boots, moon charms, clean silhouette, centered,
simple neutral gray background, flat 2d game character design,
RPG character reference, safe,
masterpiece, high score, great score, absurdres
```

右侧图不建议单独生成，第一阶段直接镜像左侧：

```text
left reference -> horizontal mirror -> right reference
```

原因：

- 节省一次生成。
- 侧面角色细节最容易漂，镜像比再生成更一致。
- 当前我们的视频生成和后处理已经支持右侧镜像视频。

## 7. 工作流 3：方向不稳时加 OpenPose

如果 IPAdapter 生成背面或侧面时仍出现以下问题：

- 背面仍露出脸。
- 侧面变成 3/4 视角。
- 伞、头发、裙摆结构漂移明显。
- 人物姿态不居中。

再引入 OpenPose ControlNet。

### 节点结构

```text
Load Image reference
 -> CLIP Vision Encode
 -> IPAdapter Apply

Load OpenPose Guide Image
 -> OpenPose Preprocessor 或直接使用姿态图
 -> Apply ControlNet

CheckpointLoaderSimple
 -> CLIP Text Encode
 -> KSampler
 -> VAE Decode
 -> Save Image
```

建议先准备三张简单姿态引导图：

```text
front_stand_pose.png
back_stand_pose.png
left_stand_pose.png
```

第一阶段不需要画复杂走路姿态。方向参考图只要稳定站姿即可，动作交给 I2V。

## 8. 与当前视频流水线衔接

ComfyUI 输出方向参考图后，继续使用已验证的视频与后处理流程：

```text
front_ref.png -> seedance front walk video
back_ref.png  -> seedance back walk video
left_ref.png  -> seedance left walk video
left video mirror -> right walk video

front/back/left/right videos
 -> tools/postprocess_walk_videos.py
 -> auto-loop 12 frames per direction
 -> 4dir sprite sheet
 -> preview.html
```

当前已验证的后处理命令形态：

```powershell
python tools\postprocess_walk_videos.py `
  --front assets\tasks\videos\seedance_gothic_umbrella_walk_front_v1\video.mp4 `
  --back assets\tasks\videos\seedance_gothic_umbrella_walk_back_v1\video.mp4 `
  --left assets\tasks\videos\seedance_gothic_umbrella_walk_left_v1\video.mp4 `
  --right assets\tasks\videos\seedance_gothic_umbrella_walk_right_mirror_v1\video.mp4 `
  --output-dir assets\tasks\sprites\gothic_umbrella_walk_4dir_pixel_v5_auto12 `
  --cell-sizes 128 `
  --frames-per-dir 12 `
  --selection-mode auto-loop `
  --palette-colors 64 `
  --bg-tolerance 52 `
  --fringe-tolerance 64 `
  --edge-contract 1 `
  --outline `
  --highlight-restore `
  --floor-shadow-clean
```

## 9. 生成视频前的参考图检查标准

ComfyUI 出图后，不要马上送 I2V。先人工筛掉不合格参考图。

必须满足：

- 全身完整，没有裁切。
- 背景简单，最好是纯色或接近纯色。
- 角色轮廓干净，没有大面积光晕。
- 伞、头发、裙摆、鞋子等关键识别点清楚。
- 正面/背面/侧面的体型比例接近。
- 没有明显 3D 渲染感。
- 没有复杂投影、强体积光、背景装饰。

如果参考图已经有光晕、复杂高光或半透明边缘，后处理会更难，视频阶段也会放大问题。

## 10. 12GB 显存建议

推荐运行策略：

```text
batch_size: 1
分辨率优先 1024x1024，OOM 则 832x832
不启用 refiner
不启用 hires fix
一次只加载必要插件
IPAdapter 和 ControlNet 不同时作为第一轮默认项
```

优先级：

```text
先跑通基础 SDXL 文生图
再加 IPAdapter
最后才加 ControlNet
```

如果出现 OOM：

```text
1. 降到 832x832
2. 关闭其它占显存程序
3. batch_size 保持 1
4. 不同时启用 IPAdapter + ControlNet
5. 重启 ComfyUI 清理显存
```

## 11. 后续可落地任务

下一步可以补三个文件：

```text
docs/workflows/comfyui/front_reference_sdxl_animagine.json
docs/workflows/comfyui/direction_reference_sdxl_ipadapter.json
docs/workflows/comfyui/direction_reference_sdxl_ipadapter_openpose.json
```

还可以新增一个本地调用脚本：

```text
tools/comfyui_generate_reference.py
```

脚本职责：

```text
1. 连接 http://127.0.0.1:8188
2. 提交 ComfyUI workflow 到 /prompt
3. 轮询 /history/{prompt_id}
4. 下载 /view 返回的图片
5. 输出到 assets/tasks/imports/comfyui_xxx/
```

这样后续流水线可以变成：

```text
python tools/comfyui_generate_reference.py --workflow front_reference_sdxl_animagine.json
python tools/wlai_video_generate.py --image front_ref.png ...
python tools/postprocess_walk_videos.py ...
```

## 12. 本阶段验收标准

第一阶段只验证 ComfyUI 替换参考图生成是否成立。

验收项：

- 能在 12GB 显存上稳定生成 1 张正面参考图。
- 能用 IPAdapter 从正面图派生背面、左侧图。
- 右侧镜像后视觉可接受。
- 三个方向参考图送入 I2V 后，角色身份比云端随机生图更稳定。
- 后处理后的 `4dir x 12 frames` sprite sheet 仍能在 HTML 预览中顺畅循环。

不在第一阶段解决：

- ComfyUI 本地视频生成。
- 完整多动作 sprite atlas。
- 战斗、攻击、施法等复杂动作。
- Pixel LoRA 直接生成视频输入图。

## 13. 来源链接

- Animagine XL 4.0：https://huggingface.co/cagliostrolab/animagine-xl-4.0
- Animagine XL 4.0 Opt 文件：https://huggingface.co/cagliostrolab/animagine-xl-4.0/blob/main/animagine-xl-4.0-opt.safetensors
- IP-Adapter：https://huggingface.co/h94/IP-Adapter
- IPAdapter SDXL models：https://huggingface.co/h94/IP-Adapter/tree/main/sdxl_models
- IPAdapter Plus SDXL ViT-H 文件：https://huggingface.co/h94/IP-Adapter/blob/main/sdxl_models/ip-adapter-plus_sdxl_vit-h.safetensors
- IPAdapter CLIP Vision encoder：https://huggingface.co/h94/IP-Adapter/tree/main/models/image_encoder
- ComfyUI IPAdapter Plus 节点：https://github.com/cubiq/ComfyUI_IPAdapter_plus
- ControlNet OpenPose SDXL：https://huggingface.co/xinsir/controlnet-openpose-sdxl-1.0
- ComfyUI ControlNet Aux：https://github.com/Fannovel16/comfyui_controlnet_aux
