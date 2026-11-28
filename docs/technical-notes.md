# 技术状态与交接入口

2026-11-21 更新。卫衣离线重建已恢复帽形，第 45 轮背面帽缘更平顺。当前候选仍有 74 条帽缝同向边及 92 条接缝单侧边。袖口固定探针、留出帧口径和 GLB 校验值见[实验记录](garment-gaussian-mesh.md)。

> 2026-11-19 服装高斯转网格实验已跑通相机恢复、2DGS、网格提取与 GLB 回读。第 33 轮为实验底版，第 36 轮的袖口补全改善了投影轮廓，仍待修复接缝和局部开口遮挡。见[实验与复核记录](garment-gaussian-mesh.md)。

> 2026-11-18 基线 `main@80de331`。#37–#39 已合入，`fixture-face-03b` 已通过本机正式入口的隔离导入，合并版 APK 的独立真机验收待执行。详见[当前任务卡](first-week.md)与[验收记录](acceptance-record.md)。

更新于 2026-11-16，保留 11/13 核查记录，并补充 `main@0eb2231` 的分支合并、后端复测和头部扫描第十七轮验收。本文件记录可公开的实现和验证情况。个人采集与实验原值保存在本地或团队内部。

## 状态依据

| 项目 | 当前结论 |
| --- | --- |
| 人体网格 | PIXIE 官方样例和本人照片均运行成功；本人确认 B 版体型可作为适配基准 |
| 人物外观 | 面部近照投影着色已生成并回读 GLB。头部经多轮对照，下巴偏差降到 4.3/4.4 px，耳位与颌线仍偏离本人，外观未通过。头发和固定下装仍是近似 |
| 头部贴图（扫描路线） | 第十七轮修正 GLB 的 UV 方向，标准浏览器正侧面验收通过；头顶缺口、五官细节与 App 集成仍待完善，见[头部贴图流水线](head-texture-pipeline.md) |
| 本地三维试衣 | Tk 预览读取同一人物及两件独立合成短袖，旋转和连续切换已验证 |
| 后端 | 四接口、整包导入、文件校验和推荐适配器已实现 |
| 自动化测试 | 2026-11-18 本机在 `80de331` 复跑后端 81 项，80 通过、0 失败、1 跳过 |
| Android | B03 与 #38/#39 已合入。B 在开发分支验证了越界模型拦截与原始实衣包加载。合并版 APK、最后远端包离线重启和 C 独立真机结果待登记 |
| 真实衣物 | 两件均在 `fixture-face-03b` 中，正式隔离导入及三项资产哈希核对通过。`top-001` v2 有历史外观复核记录，胸宽待定，`top-002` 真机贴合待验 |
| 推荐 | C、B 分别复现项目 FastAPI 的匿名 mock 场景；真实模型调用未执行 |

本地测试包含已知输入和合成素材。它提供接口及资产链路的证据，真实衣物、手机端与本人外观分别按[验收表](acceptance-record.md)检查。

## 头部外观实验

早期采集的两轮相机恢复最大模型分别为 23/96 和 28/96 帧。替换为 person-002 采集后，视频全对匹配在同一模型注册 96/96 帧，18,142 个稀疏点，平均重投影误差 0.644 px；加入照片后为 96 帧加 5/6 张照片。两批输入的结果分别记录。

person-002 的本地 PyTorch 简化高斯小样在 84 帧训练，12 帧留出，第二版 42,079 个高斯，留出帧 PSNR 15.40 → 17.55 dB。留出帧参与相机恢复和稀疏初始化，指标包含背景；实现未完整复现原始 3DGS 的自适应增密，不能视为独立几何精度或本人相似度证明。SuGaR 的 Windows 路径兼容性和本机 CUDA 扩展仍待解决。11/15 前以衣物与手机联调为主，头部保留当前实验底稿。

外观验证比较旧版、高斯小样和普通带纹理网格。只有普通网格在正面、左右侧和背面保留可见改善，经本人确认后才拼接头颈；保持 B 版身体和上衣配准，另出目录版本。以下各版均未通过外观确认：

| 版本 | 方法 | 结果 |
| --- | --- | --- |
| head-mesh-04 | 12 视角轮廓体积 + 高斯深度修正 | 轮廓与标注重叠约 0.87；关掉贴图后鼻唇几乎没有起伏 |
| pixie-face-fit-01 | PIXIE 解剖网格 + 两张侧照校准深度 | 侧视有鼻、唇和下颌，颜色来自照片采样的顶点色 |
| uv-portrait-02 | 模板 UV 从原照烘焙 1024 贴图 | 平面色块减少；耳位、颌线、发际仍偏离本人 |
| multiview-clay-02 | 三张照片 + 两帧斜侧面标点约束的局部形变 | 四张留出帧中三张改善（17.11→8.86、22.11→11.50、39.09→24.81 px），一帧几乎不变（21.61→21.57） |
| anatomy-refit-01 | 修正下巴标点 + SMPL-X 身份形状空间约束 | 留出帧下巴 11.62→4.30、11.94→4.41 px；耳部 9.75→13.78 px 变差 |
| mica-face-01 | MICA 中性脸型，单图与三图拟合两版 | 下巴 6.44→5.20、6.83→5.73 px，仍差于上一版 4.31 / 4.41 px；可见耳位 13.73→14.21 px |

表中像素值都是人工标点在 448×202 视频帧（或 320×320 标注图）上的偏差。做法是固定脸型、只用眼鼻嘴估计头姿，下巴与耳朵留作检查点，因此它们反映标住位置的一致性，不构成相似度评分。

**外观结论：未通过。** 当前底稿是 anatomy-refit-01。斜侧面的耳位和颌线偏差没有解决，眼睑与唇形仍带模板特征；提高贴图分辨率或顶点密度都不能替代形状修正。头发、侧后外观和 App 接入不列为已完成。

MICA 权重取自研究所公开文件地址，完整文件 502,647,880 字节并已核对 SHA-256。单次中性脸型推理与网格导出约 0.77 秒，PyTorch 峰值显存 294.5 MiB，计时不含权重加载和图像准备。完整三图拟合会产生局部面翻转，导出版只保留通过法线检查的 40% 调整量。

## 现有内部包

当前两件实衣联调用 `fixture-face-03b`。交付包和兼容导出说明见 [03b 交付目录](../local-data/handoff/T02-candidate-20261117/README.md)，导入复跑见[验收记录](acceptance-record.md)。

历史 `fixture-face-01` 包的队长本机目录为 `ootd_ai/outputs/takeover-tryon/package-face-01/`，包含 manifest 和三件 GLB。人物使用 B 版几何及近照顶点着色，两件上衣标记为 `synthetic-test`。

B 取得完整目录后放进自己工作副本的 `outputs/handoff/person-001/fixture-face-01/`。C 运行资产校验，再以目录版本和 SHA-256 核对接收结果。个人资产只在团队获授权范围内使用，发布前另核查模型及素材的许可。

- `fixture-01` 是早期体型联调包。
- `fixture-02` 是已经确认体型的合成短袖包。
- `fixture-face-01` 在相同几何上增加面部颜色。
- C 的 `fixture-face-02` 联调中第二件仍为同哈希占位，名称编码也待修；不视为两件实衣已交。
- 第一套真实衣物完成后创建新版本，例如 `garments-v1`，并重新验收。

本机另有 `fixture-top001-tex-v1` 候选：首件为 A 的真实贴图版，第二件仍为合成件。技术校验、隔离导入和 mock 验证通过不代表整套实衣完成；C 已判首件外观失败，不能作为正式演示包发布。匿名 `fixture-anonymous-01` 用于公开源码包与跨机接口测试，不代表本人人物。

## 架构检查发现

`main@89eb768` 已有下载与哈希校验，但离线重启恢复、推荐响应绑定、候选模型准备后切换尚有实现缺口。目录字段解析、缓存提交、场景节点变换校验也需加强。具体文件、预期行为和可复现用例见[实施清单](implementation-plan.md)。这些是静态检查结论，尚未修复或真机复测。

## 运行入口

- 后端环境、导入、启动及请求示例见 [server/README.md](../server/README.md)。
- 建模、体型扫描、近照投影和本地预览见 [reconstruction/README.md](../reconstruction/README.md)。
- 手机打开和真机运行见 [android/README.md](../android/README.md)。
- 队长电脑的可双击入口是 `outputs/takeover-tryon/open-preview.cmd`。该文件与 Tcl 运行目录属于本地环境。其他电脑按重建 README 运行 Python 入口。

## 需要如实记录的限制

- 体型经过照片估计、真实身高校准和本人反馈调整。记录每次输入、参数、处理步骤与确认范围。
- 面部颜色从近照投影到网格，眼镜颜色附着于表面。侧后肤色采用近似值，头发几何待制作。
- 头部网格来自照片加统计先验（PIXIE 的 SMPL-X、MICA 的 FLAME 身份空间），不是扫描采集。标点偏差只说明对应点位置，不能当作相似度或尺寸证据。
- 头发仍是人工近似外壳，没有照片参照，不能声称与本人一致。
- 多视角与 MICA 实验脚本读取本机采集和权重，留在 Git 忽略的输出目录，未随仓库公开；仓库只收录整理后的处理工具。
- 测试短袖由人体网格外扩、裁切生成，真实衣物需要根据实物照片和尺寸制作。
- 当前预览采用软件光栅化，Android 渲染性能须在目标手机上实测。
- 库源码、权重和素材分别核查许可。PIXIE、SMPL-X 与 MICA 依赖由使用者按官方条件获取（均限非商业研究用途），本仓库保存项目编写的调用与处理工具。

## 公开与本地记录

公开任务记录编号、代码版本、步骤与结论。内部记录保存照片、模型、测量值、操作录像和输入哈希。参赛的 AI 辅助编程记录需包含提示词要点、采纳内容、人工修改、验证状态和对应提交。

[PIXIE 官方实现](https://github.com/yfeng95/PIXIE)提供模型来源；当前运行使用已配置的本地依赖目录。完整安装与权重获取应遵循官方说明。

---

## 2026-11-28 专项技术深化：端侧视差防扭手腕算法 & Scan-to-Template 多视角纹理迁移

基于 11/23 消融实验与 11/26–27 `top-003` 建模复盘暴露的两处技术缺口，在不碰撞端云对外协议与并行开发分支的前提下，完成了以下两项独立技术优化与闭环验证。

### 1. 端侧「真绕行 vs 原地扭手腕」稀疏光流视差防作弊算法

- **问题根因**：11/23 消融实验证实，仅靠 IMU 偏航角点亮 12 扇区无法区分「原地扭手腕旋转」与「连人带机真正迈步绕行」——前者虽能点亮传感器格数，却会造成真实 3D 相机轨迹 `90°+` 缺口。
- **实现模块**：
  - [OrbitParallaxDetector.kt](../android/app/src/main/java/com/ootd/ai/capture/OrbitParallaxDetector.kt)：纯 Kotlin 无第三方依赖。将 `GrayFrame` 下采样为 `64×64` 灰度网格，在中心前景区（4 个 `8×8` 锚点）与左右背景区（6 个 `8×8` 锚点）剔除低纹理区（`std < 8.0`）后，在 `dx ∈ [-10, +10], dy ∈ [-2, +2]` 窗口执行**零均值绝对差（ZMAD）**块匹配（抗自动曝光漂移）。
  - 物理判据：纯旋转时前景与背景同向等速横移（`parallaxRatio <= 0.35` 且连续 `>= 2` 帧），判为 `WRIST_TWIST_SUSPECTED`；真绕行时目标居中近似静止而背景显著反向平移（`parallaxGapPx >= 1.6`），判为 `TRUE_ORBIT`；低纹理纯色墙面（有效锚点不足）安全降级为 `INSUFFICIENT_TEXTURE` 不误杀。
  - [OrientationSectorTracker.kt](../android/app/src/main/java/com/ootd/ai/capture/OrientationSectorTracker.kt)：新增 `requireOrbitVerification` 门控开关（默认 `false` 兼容历史逻辑）与 `@Synchronized` 线程保护，并提供 `commitVerifiedCurrentSector()` 与 `recordWristTwistRejected()`。
  - [LiveFrameQualityAnalyzer.kt](../android/app/src/main/java/com/ootd/ai/capture/LiveFrameQualityAnalyzer.kt) 与 [CaptureGuideScreen.kt](../android/app/src/main/java/com/ootd/ai/ui/CaptureGuideScreen.kt)：阶段 1 开启视差门控，命中扭手腕时拒绝点亮扇区并触发黄灯纠偏提示「别只扭手腕 · 迈步绕行」。
- **验证结果**：
  - 新增 [OrbitParallaxDetectorTest.kt](../android/app/src/test/java/com/ootd/ai/capture/OrbitParallaxDetectorTest.kt)（7 项测试：纯旋转识别、真绕行识别、白墙安全降级、曝光偏移鲁棒性、状态恢复、扇区门控与分析器联动）。
  - Android 编译与全量单测（13 个测试类共 **75 项**）通过（`OK (75 tests)`），`:app:assembleDebug` 构建成功。

### 2. Scan-to-Template 多视角可见性加权纹理迁移管线

- **问题根因**：`top-003` 扫描几何（V9.1 厚度比 `0.543`）未达交付要求而启用标准模板兜底，但扫描帧中已包含高清真实印花与褶皱纹理，直接沿用原模板 UV 无法承载扫描件外观。
- **实现模块**：[transfer_scan_texture.py](../reconstruction/transfer_scan_texture.py)
  1. **模板单图 UV 重排**：通过 `xatlas` 为干净模板网格生成单张无重叠 UV，保持原始顶点与包围盒尺寸零漂移。
  2. **自动空间配准与正背面消歧**：基于相机平均上方向量 `up_scan` 与正面相机方向先验，在 8 个初始偏航角候选上执行两阶段 Open3D ICP 配准，并以 **多视角 2D 掩膜轮廓 IoU + 归一化倒角距离** 选优。
  3. **多视角自遮挡剔除与两遍抗离群烘焙**：构建 Open3D `RaycastingScene` 检测自遮挡，结合 SegFormer 严格腐蚀掩膜、身体/手臂语义禁区距离加权与高亮帧基准的 Lab 空间光色一致性校准，按 `facing^4.5 × sharpness × spatial_reliability / z^2` 权重完成双遍鲁棒融合。
  4. **干净种子高斯扩散补洞**：对被遮挡/不可见 UV 内区（如内表面及手臂遮挡侧缝）先腐蚀可信种子掩膜剔除边缘阴影噪点，再结合距离衰减平滑扩散至全衣主底色中位数并外扩 UV 边界，确保有效 UV 区全黑洞率 `0.00%`；导出前经 [export_glb_guard.py](../server/tools/export_glb_guard.py) 剥离并验证 0 禁用扩展。
- **验证结果**：
  - 独立合成基准测试 [test_transfer_scan_texture.py](../reconstruction/tests/test_transfer_scan_texture.py) 通过（合成刚体变换对齐轮廓 IoU `> 0.85`、命中率 `> 90%`、黑洞率 `0.0%`、重投影 PSNR `> 22 dB`）。
  - `top-003` V9.1 真实扫描数据（60 视角）迁移至 `top-001_v2` 模板实测（2048px 贴图输出留存于本地隔离输出目录，未入公开仓库）：
    - 几何边界漂移 `0.0 m`，GLB 体积 `3.561 MB`（1024px 版 `1.703 MB`），Filament 禁用扩展 `0` 个；
    - 多视角对齐轮廓 IoU `0.7482`（8 视角重投影评测均值 `0.7711`），有效 UV 命中率 `51.3%`（中位每命中像素观测 `19` 视角），有效 UV 全黑率 `0.00%`；
    - 8 视角重投影相对原模板基准：PSNR 由 `11.408 dB` 提升至 `16.918 dB`（**`+5.510 dB`**；1024px 版 `18.291 dB / +6.943 dB`），Lab 色差 ΔE 由 `57.00` 降至 `29.77`。
  - **v2 姿态对齐与对称织纹升级（方案 B）**：
    - 针对模板 T-pose 平举袖筒与实拍垂臂姿态失配导致的袖筒纹理落空问题，新增纯几何软蒙皮临时下垂采样（`deform_sleeves_soft_lbs`，导出时严格恢复原始 T-pose 几何 `0.0 m` 漂移）、躯干锚定 ICP（`torso_only_icp`）、3D 左右对称纯布料借用（剔除单侧印花防误复制）及无污点高频织纹平铺补全；
    - `top-003` v2 实测（2048px）：自动检出最佳袖筒下垂角 `72.0°`，配准轮廓 IoU 由 `0.7482` 提升至 **`0.8105`**（8 视角重投影均值 **`0.8234`**），90 分位顶点对齐误差由 `0.3578 m` 降至 **`0.2045 m`**，对称借用补齐 `89,388` 个纯布料像素，袖筒外观明显恢复真实白衣褶皱质感，GLB 体积 `3.61 MB`（0 禁用扩展）。
    - 新增 `test_soft_lbs_sleeve_drop_and_symmetry_completion` 单测，验证袖筒下垂位移、躯干静止不变与单侧印花防误镜像；`reconstruction/tests` 2 项测试全部通过。

### 3. 采集质量门控增强与局部缺损“增量补拍”闭环优化

为直接从采集端减少侧缝/袖下遮挡与模糊帧污染，并在模型局部缺块或糊片时支持低门槛“补拍即修”，全链路新增两项协同优化：

1. **Android 端防遮挡引导、曝光质检与定向补拍模式**：
   - [CaptureGuideModels.kt](../android/app/src/main/java/com/ootd/ai/capture/CaptureGuideModels.kt)：新增 `GarmentCapturePosture`（优先推荐 `HANGER` 衣架挂拍防遮挡，支持穿身对比），以及 `ModelDefectRegion` / `ModelDefectKind` 缺损部位映射与 `TargetedReshootPlan`（`CaptureGuideResponse.buildTargetedReshootPlan()` 将 AI 建议缺口/黄格弱区自动映射为定向补拍区，且在 `invalid_retake / canReuseExisting=false` 时严格禁止增量复用）。
   - [LiveFrameQualityAnalyzer.kt](../android/app/src/main/java/com/ootd/ai/capture/LiveFrameQualityAnalyzer.kt)：新增 `ExposureStatus` / `ExposureSnapshot` / `evaluateExposure`，在灰度帧上实时识别过暗、过曝、高对比逆光等曝光异常并连续 2 帧防抖告警，导出 `frameQualityAcceptableForSector` 作为扇区有效点亮门控。
   - [OrientationSectorTracker.kt](../android/app/src/main/java/com/ootd/ai/capture/OrientationSectorTracker.kt)：将 12 扇区升级为三色质量分级 `SectorQuality`（未采集 `UNVISITED` 灰 / 模糊或逆光 `NEEDS_IMPROVEMENT` 黄 / 清晰达标 `GOOD` 绿），并新增 `recordSectorObservation` 阻止低质或扭手腕帧误亮绿灯。
   - [CaptureGuideScreen.kt](../android/app/src/main/java/com/ootd/ai/ui/CaptureGuideScreen.kt)：接入挂拍/穿身筹备选项、局部缺损部位勾选芯片、三色+橙色目标补拍扇区罗盘、逆光/弱区语音与底栏提示；结果页支持根据 `TargetedReshootPlan` 一键按 AI 建议跳转局部定向补拍，录制输出自动加注 `OOTD_patch_` 前缀，全部目标扇区变绿后自动播报完成。
   - **Android 验证**：[CaptureQualityAndReshootTest.kt](../android/app/src/test/java/com/ootd/ai/capture/CaptureQualityAndReshootTest.kt) 含 8 项单测（曝光识别、逆光防抖、三色升级、部位映射与 AI 建议跳转门控），全量 14 个测试类共 **83 项**单测全部通过，`:app:assembleDebug` 构建成功。

2. **重建端缺损自动诊断与无位姿增量补拍纹理融合**：
   - [transfer_scan_texture.py](../reconstruction/transfer_scan_texture.py)：`bake_multiview_to_template_uv(..., return_aux=True)` 新增逐像素质量权重图 `quality_map`、视角命中计数 `obs_count_map` 与光色基准 `lab_target`；`diagnose_model_defects` 按外观法向区域（正面、左右侧缝、背面、领口、双袖筒）自动生成 [defect_report.json] 缺块/弱片诊断及推荐补拍扇区，并落盘增量状态包 `transfer_state.npz`。
   - [refine_texture_with_reshoot.py](../reconstruction/refine_texture_with_reshoot.py)：面向补拍短视频或 `OOTD_patch_` 图片集，支持本地离线 `SegFormer` 自动抠图，基于精确扫描网格射线锚定 + 波前式 BFS 自举 `SuperPoint + LightGlue + PnP RANSAC`（含相机半径异常护栏）恢复无位姿补拍视角，经跨批次 Lab 调色与双遍抗离群融合后执行质量门控更新——缺失洞直接填补、弱区达标提升，原有清晰区零覆盖漂移，并生成 `before_after_4views.png` 与 `patch_diff_map.png`。
   - **重建端验证**：
     - 新增端到端合成测试 [test_refine_texture_with_reshoot.py](../reconstruction/tests/test_refine_texture_with_reshoot.py)，`reconstruction/tests` 3 项单测全部通过；
     - `top-003` 大缺口实测（于隔离输出目录验证，未入公开仓库）：留出背面弧段 `frame-00030..00070`（41 帧）后，`defect_report.json` 自动判别 `back` 为 `missing_patch`（`hit_ratio = 0.3459`，中位 `2.0` 视角）；随后输入 13 张无位姿 `OOTD_patch_` 补拍帧自动抠图并零泄漏融合：**13/13 (100%)** 完成配准（重投影误差 `2.826–4.278 px`），新填补缺失像素 `97,862` 个、质量升级 `51,471` 个，原有清晰区域 `locked_existing_max_drift = 0.0`，`back` 命中率跃升至 **`0.9108`**（中位 `9.0` 视角，状态恢复 `ok`），外表面总命中率由 `0.5775` 提至 `0.8340`，全视角重投影 PSNR 提升 **`+0.241 dB`**（`16.45 -> 16.691 dB`）。


