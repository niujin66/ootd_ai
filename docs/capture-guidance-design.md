# 手机端「拍摄实时引导」与端到端质检闭环技术设计

更新于 2026-11-22。本文面向 11/22–23 03b 真机验收收口之后的功能扩展阶段（**12/7 功能冻结前不改变当前验收基准 APK `d4402052…` 的代码与哈希**），定义 **方向 1（后端视频/帧/COLMAP 自动质检提取）** 与 **方向 2（Android 实时采集引导原型）** 的架构、开源参考依据与接口对接方案。

---

## 1. 问题来源与设计原则

在本地 2DGS / 3DGS 实验（[补拍清单](../../ootd_gsscan/docs/补拍清单.md) 与第 31–65 轮报告）中，我们定位到后期重建失真的两类源头缺陷：
1. **头部第 65 轮**：单眼有效像素仅约 2–3 px，且早期采样存在大角度视角缺失，导致重烘焙眼部贴图只会放大亮度与高频噪点，无法恢复真实眼形。
2. **服装第 31–36 轮**：部分候选存在画面上下裁切（边距仅 21 px）、相邻帧相似度低至 0.291（移动过快/跳帧）、侧面及袖口/领口近景缺失。

因此，本系统拒绝“拍完后让生成式 AI 脑补本人五官或衣物背面结构”的幻觉路线，转而采用 **「确定性视觉/传感器质检 + 大模型交互式补拍指导」** 闭环：
- **端侧与服务端程序负责算**：毫秒级量化方位扇区、相机水平翻滚、清晰度、帧间重叠度、边缘留白与关键局部像素；
- **DeepSeek（`POST /v1/capture/guide`）负责教用户怎么改**：将客观缺口 JSON 转译为按优先级排序、具体到距离/角度/动作的中文补拍步骤，并在服务端通过 `validate_guide_output()` 拦截矛盾输出（如 `invalid_retake` 不得标记可复用）。

---

## 2. GitHub 参考方案对照

| 模块 / 能力 | GitHub 开源参考 | 核心机制 | 衣序本地选型与改进 |
| --- | --- | --- | --- |
| **实时帧间重叠与防跳帧** | [`nsicart/MosaicEarth-Capture-Android`](https://github.com/nsicart/MosaicEarth-Capture-Android) | CameraX `ImageAnalysis` 对降采样亮度网格做归一化互相关（NCC），不依赖 ARCore 即可提示移动过快 | 端侧复用降采样灰度块 NCC；后端在 `server/capture_analyzer.py` 实现 64×64 直方图均衡余弦相似度 `compute_orbit_continuity()` |
| **头部姿态与眼部像素检查** | [`ZijianOu/DMS_App`](https://github.com/ZijianOu/DMS_App)、[`RajaNimit27/CameraMLKitFaceDetection`](https://github.com/RajaNimit27/CameraMLKitFaceDetection) | Jetpack Compose + CameraX + Google ML Kit Face Detection，提取人脸框、欧拉角与双眼坐标 | 头部采集模式下，用双眼眼距/眼框像素实时拦截 `< 8 px` 低分辨率源数据，从源头避免第 65 轮困境 |
| **主体留白与防裁切** | [`google-ai-edge/mediapipe-samples`](https://github.com/google-ai-edge/mediapipe-samples)（`examples/image_segmentation/android`） | CameraX + MediaPipe `ImageSegmenter` 实时提取前景掩膜并计算四边边距 | 端侧可用轻量分割或中心框投影估算 `min_margin_px`；电脑端 `capture_analyzer.py` 采用四角+侧边调色板与中心最大连通域提取，并支持外部 `--masks-dir` |
| **清晰度筛选** | [`SharkWipf/nerf_dataset_preprocessing_helper`](https://github.com/SharkWipf/nerf_dataset_preprocessing_helper)、[`cansik/sharp-frame-extractor`](https://github.com/cansik/sharp-frame-extractor) | 拉普拉斯方差（Laplacian Variance）/ Tenengrad 评估视频帧清晰度 | 端侧在 `ImageAnalysis` 抽样计算 Y 通道 3×3 拉普拉斯方差；后端 `compute_laplacian_sharpness()` 给出中位数 `sharpness_p50` |
| **空间追踪与 NBV 对照** | [`faizfahmi77/ScanDot`](https://github.com/faizfahmi77/ScanDot)、[`JiangWenPL/FisherRF`](https://github.com/JiangWenPL/FisherRF) | ARCore 6DoF 位姿 / 3DGS Fisher 信息不确定性下一最佳视角（NBV） | 手机端首版优先采用通用传感器 `TYPE_ROTATION_VECTOR`（兼容全部 Android 机型，不强绑 ARCore）；`FisherRF` 需预训练高斯场，作为技术报告理论对照 |

---

## 3. 方向 1（已落地）：后端视频/帧/COLMAP 自动质检入口

已实现模块：
- **`server/capture_analyzer.py`**：零额外重型依赖（在 `server/.venv` 下仅用标准库 + NumPy 即可运行，自动兼容 `PIL` / `OpenCV` / `ffmpeg`）：
  1. 图像/视频采样（`load_frames()`）：内置纯 NumPy 8-bit PNG 解码，并在 Windows 下通过原生 Media Foundation（`ctypes`）直接读取 `.mp4`/`.mov` 视频与 `.NET System.Drawing` 批量读取 `.jpg`/`.bmp`；
  2. 清晰度方差（`compute_laplacian_sharpness()`）；
  3. 环绕连续性（`compute_orbit_continuity()`）；
  4. 四边留白像素（`estimate_foreground_margins()`）：支持外部掩膜，无掩膜时采用抗光照阴影的 RGB+色度周边调色板与中心主体似然（GrabCut-lite）+ 形态学开运算与中心最大连通域；
  5. 关键局部像素自动估算（`estimate_head_eye_detail_px()`）：`head` 模式下基于 YCbCr 肤色区域估算单眼有效像素宽度；`garment` 模式下按服装前景宽度估算结构尺度；
  6. COLMAP 稀疏模型解析（`analyze_colmap_sparse()`，同时支持 `images.bin` / `images.txt`，用光轴最小二乘交点估计目标中心、SVD 估计环绕平面法向，并自动适配横竖屏相机主轴计算 `up_roll_median_deg`）。
- **`server/tools/analyze_capture.py`**：一键提取指标并可选通过 `--call-guide` 调用 `POST /v1/capture/guide`（支持 `mock` 与 `ai` 双模式）。

---

## 4. 方向 2（待排期）：Android 客户端实时采集引导设计

> 实施约束：**必须等 11/22–23 真机验收基准闭合且 `#44` 定案后再合入 `android/`**，避免提前改动当前验收 APK 哈希。

### 4.1 依赖与模块划分

在 `android/app/build.gradle.kts` 后续增量引入（`minSdk = 24`、`compileSdk = 37` 均兼容）：
- `androidx.camera:camera-core / camera2 / lifecycle / view / video`（CameraX 预览、录像与帧分析）
- `com.google.mlkit:face-detection`（仅 `head` 模式下用于实时眼距像素与头部欧拉角检查，纯端侧离线模型）
- 可选：`com.google.mediapipe:tasks-vision`（用于更精确的衣物四边留白分割；若需控制 APK 体积 ≤ 30 MiB，首版可用 YUV 中心高亮/边缘方差投影替代）

建议新增包内文件（不触碰现有 `TryOnScreen.kt` 渲染核心与 `RemotePackageRepository.kt`）：

```text
android/app/src/main/java/com/ootd/ai/
  capture/
    CaptureGuideModels.kt      # 请求/响应 DTO（对齐 POST /v1/capture/guide）
    CaptureGuideApi.kt         # HTTP 客户端（复用 CatalogApi 超时/鉴权与错误体解析）
    OrientationSectorTracker.kt# TYPE_ROTATION_VECTOR -> 12 个 30° 扇区 + up 翻滚中位数
    LiveFrameQualityAnalyzer.kt# CameraX ImageAnalysis：Y 通道清晰度 + 64×64 NCC 连续性 + 人脸眼部 px
  ui/
    CaptureGuideScreen.kt      # 预览叠加：12 扇区进度环 + 水平仪条 + 实时语音/文字提示 + AI 补拍清单卡片
```

### 4.2 端侧实时流水线（边拍边检）

1. **姿态与扇区覆盖（`OrientationSectorTracker`）**：
   - 注册 `Sensor.TYPE_ROTATION_VECTOR`（`SENSOR_DELAY_GAME`）。
   - 以开始录制时刻的方位为 `0°` 基准，计算绕重力轴的相对偏航角 `azimuth ∈ [0, 360)`，点亮 12 个 `30°` 扇区进度环。
   - 实时计算手机横滚角 `rollDeg`：`|rollDeg| > 20°` 提示“请保持手机端平”，录完取中位数写入 `up_roll_median_deg`。
2. **帧质量分析（`LiveFrameQualityAnalyzer`，限流 `3–4 FPS`）**：
   - **清晰度**：直接读取 `YUV_420_888` 的 Y 平面（降采样至 `320×240`），整数算子计算 3×3 拉普拉斯方差；连续 3 帧 `< 20` 立即弹出“画面模糊，请放慢移动”。
   - **帧间连续性**：缩放到 `64×64` 灰度均值归一化向量，与上一分析帧做点积；记录全程最小值 `orbit_continuity_min`，低于 `0.35` 实时提示“移动过快”。
   - **头部近景/眼部像素（`target_type = "head"`）**：通过 ML Kit `FaceDetection` 取左右眼关键点距离 `eyeDistPx`（按原图分辨率换算），实时提示“请再靠近些，保证眼部清晰”，并将最小/中位眼部像素估算传入 `roi_detail_px`。
   - **四边留白（`min_margin_px`）**：在检测框（人脸/躯干）或前景轮廓四周计算最小像素边距，低于 `40 px` 提示“请略微后退，避免裁切边缘”。
3. **录制结束 → 调用服务端 `POST /v1/capture/guide`**：
   - 停止录制后汇总 `covered_azimuth_deg`、`up_roll_median_deg`、`sharpness_p50`、`orbit_continuity_min`、`min_margin_px`、`roi_detail_px` 等字段发送至后端。
   - 若后端返回 `readiness = "pass"`：提示“采集达标，可导出上传重建”。
   - 若返回 `needs_reshoot` / `invalid_retake`：在结果卡片展示 AI 返回的 `summary` 与按 `priority` 排序的 1–4 条可执行补拍步骤，并支持 Android `TextToSpeech` 语音播报与一键“针对缺口角度重新拍摄”。

### 4.3 接口契约对齐（已与服务端联调一致）

端侧 `POST /v1/capture/guide` 请求与当前服务端 [app.py](../server/app.py) / [capture_guide.py](../server/capture_guide.py) 完全一致：

```json
{
  "request_id": "android-cap-001",
  "target_type": "head",
  "subject_label": "person-002",
  "covered_azimuth_deg": [0, 30, 60, 90, 120, 150],
  "up_roll_median_deg": 12.4,
  "min_margin_px": {"left": 96, "right": 102, "top": 140, "bottom": 180},
  "sharpness_p50": 48.2,
  "orbit_continuity_min": 0.51,
  "roi_detail_px": 5.5,
  "has_scale_reference": false,
  "missing_closeups": ["左右耳", "后脑"]
}
```

### 4.4 验收标准（后续合入 Android 时执行）

1. **权限与兼容性**：拒授相机权限时友好提示且不崩溃；真机无 ARCore 时传感器引导照常工作。
2. **性能开销**：`ImageAnalysis` 开启后预览稳定 `≥ 24 FPS`，10 分钟无内存泄漏或 OOM。
3. **协议与离线降级**：服务端离线或超时（503）时，端侧可展示基于本地 `covered_azimuth_deg` 与 `sharpness_p50` 的基础缺失提示，不丢失已录视频。
4. **打包体积与既有回归**：新增依赖后 APK 体积增量与既有 49 项单测、试衣渲染页零回归。
