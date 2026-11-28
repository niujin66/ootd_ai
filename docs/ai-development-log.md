# AI 辅助开发记录（工作底稿）

此 Markdown 用于日常追踪，提交比赛时再整理到附件7的 doc/docx/pdf；不直接作为参赛文档提交。

## 2026-11-11：方法调研与架构设计

- 任务：重新检查当前项目的方法与整体框架，给出可执行的优化设计。
- 提示词要点：结合已有仓库、3DGS 转建模想法、Gemini 方案及队员交付情况；队长正修电脑，保留 A/B/C 工作分工。
- 上下文：审阅 `main@89eb768` 的 Android/后端代码、首件实衣失败记录、mock 服务复现记录、既有头部相机结果；核对官方 COLMAP、3DGS、SuGaR、2DGS、Blender、Android 与 glTF 资料。
- 采纳：固定 B 版身体，离线改进头部和衣物；保留当前技术栈与 v1 协议；设计候选模型准备后换衣、持久包恢复、请求与版本校验、资产质量门槛。
- 未采纳/修正：零穿模保证、压缩后必达60帧、单张照片直接恢复真实法线、为外观问题更换整个客户端技术栈。研究路线不标记为已实现。
- 人工职责：A 制作与实物核对，B 实现 Android 改动，C 独立测试及模型实验，队长仅确认相似度/外观与必要决策。
- 本轮产物：方法调研、架构 v0.3、实施与测试清单、进度/验收记录校正。
- 验证范围：文档相对链接、JSON 示例、代码引用、Git diff；未运行新的重建、真实 AI 请求或真机测试，未宣称 R01–R10 已修复。
- 对应提交：包含本条记录的 Git 提交；由 Git 历史定位，避免写入自引用哈希。

后续记录应补齐采纳代码、人工修改、实际运行结果与提交号；公开记录不包含身份资料、原图、模型资产或凭据。

## 2026-11-13：按实际进度重排冲刺任务

- 任务：依据本地和远端最新进度加快开发，向队员分配可交付工作，队长保留少量实际开发。
- 输入：`main@765ba2a`、#1/#12/#13/#19 的最新证据、Android/后端现有文件和本地头部结果摘要。
- 采纳：A 主攻 POLO 与资产；B 先 R01/R02 再 R03/R04；C 编写异常输入、后端输出校验和真实 AI 验证并独立联调；队长完成首页引导。设定 11/14、11/15 两轮目标，11/16 更新参赛快照。
- 修正：首件 v2 已通过；B 的降级/恢复证据成立，但尚不覆盖恢复最后远端目录；区分早期相机结果与 person-002 的 96/96 注册。C 本轮退出头部实验，保留未通过结论。
- 人工职责：各负责人回报可完成的节点、编写并解释代码、提交交付证据；C 复测并维护实际状态。预计工时和排期不当作承诺或完成结果。
- 验证：核对任务负责人、依赖、文件入口、链接和 Git diff；本轮没有新增真机、自动化、模型训练或真实 AI 调用测试。
- 对应提交：包含本条记录的提交；任务指派与文档 PR 在 GitHub 可查。

## 2026-11-13：头部外观重建对照与文档更新

- 任务：上一轮外观被判定不写实后，重新对比可行的头部重建路线，并把结论写回公开文档。
- 提示词要点：先用多视角标点约束修形状，再谈贴图和头发；不要用贴图掩盖几何偏差；每轮都要能与原照同角度比较。
- 上下文：`main@82b957d` 的文档与 `reconstruction/` 工具；本机已配置的 PIXIE、SMPL-X 权重与头部采集、COLMAP 相机结果；官方 PIXIE、SMPL-X、MICA 说明及其许可条款。
- 采纳：多视角标点约束的局部形变；修正下巴标点后改用 SMPL-X 身份形状空间约束；用 MICA 中性脸型做对照版；每次导出后回读 GLB，检查面法线方向与边长比例。
- 未采纳/修正：为每张照片单独拉扯五官的旧做法；靠更高分辨率贴图掩盖形状偏差；把单图 MICA 结果直接当作最终脸型；声称头发或侧后外观已与本人一致。
- 人工职责：本人照片与标点由本人在本地提供，实验脚本在本机运行；外观是否通过由本人判断，文档只记录实测数值和失败结论。
- 本轮产物：`technical-notes.md` 的头部外观实验表、`acceptance-record.md` 的 Q03-look、`README.md` 进度、`reconstruction/README.md`，以及新增的 `reconstruction/build_person_look.py`。
- 验证范围：每轮都用同一相机重渲染并标注偏差；未新增真机、后端或真实 AI 推荐测试；外观结论为未通过。
- 对应提交：包含本条记录的 Git 提交；由 Git 历史定位，避免写入自引用哈希。

## 2026-11-19 卫衣网格与辅助补全

- 任务要求从单条环绕视频恢复衣物外形，输出可旋转的 GLB，并把实拍恢复的表面和衣物内侧的推测补面分开标记。
- DeepSeek 在本地补抽转角帧，将相机登记从多子模型推进到 199 帧同一模型。随后完成 2DGS 训练、网格提取和袖口补面实验。袖口内侧采用参考袖口的形状与厚度构造几何，报告保留估计假设。
- Codex 检查原片、训练与留出帧的划分，重新渲染 GLB，复现了遮罩填洞误收背景的问题，并验证了第 33 轮清理长条杂面的效果。第 36 轮另外复核了面片索引与腕口射线。
- 复核发现轮廓分数上升，补面与底版仍缺少共用边，斜侧可见尖片，腕口近边缘有遮挡。保留第 33 轮底版，第 36 轮登记为待修的实验结果。
- 完整过程见[服装高斯转网格记录](garment-gaussian-mesh.md)。

## 2026-11-21 卫衣重建结果复核

根据第 37–45 轮报告对照最终 GLB、接缝计数和轮廓指标。第 39 轮的过渡带保有共享边，但导出文件丢失袖筒壁，后续验收增加面组回读。第 45 轮的上缘平滑改善了局部抖动，背面轮廓相对原片的第 90 百分位距离仍由 74 增至 76 像素。两个指标各自登记。留出帧 01170 仅用于目视对照，独立实衣包的真机结论由 A/C 另行记录。[实验记录](garment-gaussian-mesh.md)

## 2026-11-22 晚：Android 两段式实时采集引导、SDK 环境配通与直连调试

- 任务：在合并 PR #45 后，按 `docs/capture-guidance-design.md` 第 4 节落地 Android 端两段式实时采集引导，并响应队长真机试用反馈（“引导不明确、不知所措” + “暂无云服务器时直连 DeepSeek API 调试”）。
- 提示词与设计决策：
  - 将「只远拍眼部模糊」与「只近拍丢失环绕空间位置」的矛盾拆为单镜不中断的 **阶段 1（1.2–1.5 m 远景绕 12 扇区）→ 阶段 2（被拍人静止、镜头慢推近 40 cm 补正/左右眼部像素 ≥20 px）**；
  - 针对室内磁力计干扰与首次点击录像时机，在 `RotationSensorBridge.kt` 优先使用 `TYPE_GAME_ROTATION_VECTOR` 再回退 `TYPE_ROTATION_VECTOR`，并补全 CameraX 绑定就绪后的 `pendingRecordStart` 延时起录；
  - 依据华为鸿蒙真机初测反馈，把遮挡视野的中央 220 dp 大圆环移至右上角紧凑罗盘，中央改用透明虚线安全框 + 顶部 3 步进度条 + 白话下一步指令徽章；
  - 新增 `DirectDeepSeekCaptureGuide.kt` 与本机私密 `CaptureDebugPrefs.kt`：沿用「端侧先做确定性几何/清晰度诊断，再让模型翻译成 1–4 条中文补拍步骤并严格拦截矛盾输出」的安全边界，Key 仅由用户在设备本地配置，不写入仓库或硬编码。
- 环境与人工配合：本机缺 Android SDK，配置官方 `cmdline-tools 23.0.0`、`platform-tools 37.0.3`、`platforms;android-37.0`、`build-tools 36.0.0/37.0.0` 并通过 SHA-1 校验，打通 `:app:assembleDebug`；队长在华为鸿蒙手机实装首版 APK 验证不崩溃，并指出引导提示和调试连接方式的改进需求。
- 本轮产物：`capture/`（7 个 Kotlin 文件）、`ui/CaptureGuideScreen.kt`、`MainActivity.kt`、`AndroidManifest.xml`、`app/build.gradle.kts` 及 `test/.../capture/`（4 个单测文件）。
- 验证范围：后端 102 项（101 通过、1 跳过）；Android `assembleDebug` 编译打包通过（`82.06 MB`）；Android 全量 12 个测试类 66 项通过 `JUnitCore` 运行全过；真机直连 DeepSeek 端到端实拍待下一步复测。

## 2026-11-23 晚：真实头部消融实验验证、短板定位与 1080p 录像升级

- 任务：使用华为鸿蒙真机实拍「无引导对照组」与「App 两段式实时引导组」头部对比视频，在本地 GPU 环境开展全流程 `COLMAP + DeepLabV3 + 2DGS（7,000 步）` 消融对比，定位真实工程瓶颈并立即反哺 Android 客户端。
- 实验发现与定位：
  - **三维重建质量显著提升**：App 引导组在 COLMAP 配准率（`91.7%` vs `80.0%`）、稀疏点数（`6,093` vs `4,071`，`+49.7%`）、阶段 1 远景构图（峰值占比 `65.2%` vs `85.8%`，裁边帧率 `50.0%` vs `90.2%`）以及 `2DGS` 7k 步留出视角渲染质量（`PSNR 23.751 dB` vs `19.710 dB`，`SSIM 0.8665` vs `0.8413`，`LPIPS 0.1601` vs `0.1999`）上全面占优，且阶段 2 单眼有效像素达 `42.6 px`（远超第 65 轮 `2–3 px` 瓶颈）。
  - **定位出的 3 项真实改进点**：① App 录像原用 `Quality.HD`（720p）导致清晰度中位数 `84.79` 低于原生相机 `141.27`；② 起录第 0–1 秒对焦/拿稳前容易出现首帧连续度跳变（`-0.1132`）；③ COLMAP 3D 空间轨迹交叉核验显示引导组仍存在 `90°` 空间空缺，印证「手机陀螺仪朝向 ≠ 3D 空间位置绕行」风险。
- 采纳改动：在 `CaptureGuideScreen.kt` 将 `QualitySelector` 升级为优先 `Quality.FHD`（1080p）并安全回退、加入 `1200 ms` 自动对焦稳像缓冲后才启动 `MediaStore` 录像，并在引导文案强调「迈步绕行、勿只原地扭手腕」。
- 验证范围：新版 `app-debug.apk`（`82.06 MB`）编译打包通过，12 类 66 项端侧单测全过（`0.24 s`）；COLMAP + `DeepLabV3` 分段质检 + `2DGS` 7k 步评估脚本全部在隔离目录运行完成；升级 1080p 后的下一轮真机复拍待测。所有个人原视频与抽帧均留存本地隔离目录，不入库。

## 2026-11-25：V9 通用本地 AI 辅助头部重建、采集话术升级与衣物迁移评估

- 任务：在不借助外部闭源云端 3D 生成器的前提下，解决 V8 头部正脸扭曲/眼窝高低不平与后脑勺 `270°` 断崖问题；封装无硬编码通用脚本 `reconstruction/reconstruct_head_v9.py`；优化端云 AI 采集引导话术并评估衣物管线升级。
- 诊断与方案设计：
  - 通过将正面锚定帧 3D 双瞳反投影回其他正面帧，定位到此前锁定宿舍背景的 COLMAP 存在 `12–18 px` 活体歪头偏移；后脑勺 `270°` 断崖来自阳台强逆光（清晰度 `28.1` vs 侧后 `275.4`）与 `facing^16` 极端权重。
  - 采用全本地开源栈组合：`SuperPoint + LightGlue` 纯前景 SfM（`85/85` 全注册、重投影 `1.4267 px`）+ `Meta Sapiens2-Normal-0.4B` 法线先验（面部校准余弦 `0.8697`）+ 2DGS `Face|Hair` 解耦 6-DoF 可微位姿联合微调 + `Lab` 逆光暗部拉回与 `facing^5` 切线法线融合。
- 本轮落地改动：
  - 新增 [`reconstruction/reconstruct_head_v9.py`](../reconstruction/reconstruct_head_v9.py)：自动滤糊抽帧、`SIMPLE_RADIAL` 自校准与去畸变、纯图像/法线联合正脸锚定与上三分之一面部解剖带双瞳定位、严格前景掩码防头顶沾边与 `export_glb_guard.py` 扩展剥离；
  - 升级 [`server/capture_guide.py`](../server/capture_guide.py)、[`DirectDeepSeekCaptureGuide.kt`](../android/app/src/main/java/com/ootd/ai/capture/DirectDeepSeekCaptureGuide.kt) 与 [`CaptureGuideScreen.kt`](../android/app/src/main/java/com/ootd/ai/ui/CaptureGuideScreen.kt)：补充摘眼镜/闭口/控时 25–40 秒/避逆光提示词，并修复阶段 2 衣物模式误播报人脸提示的问题；
  - 更新 `docs/head-texture-pipeline.md`、`docs/garment-gaussian-mesh.md`、`docs/capture-guidance-design.md`、`reconstruction/README.md` 与 `README.md`。
- 实测与验证范围：V9 局部对照实测双瞳漂移由 `12–18 px` 降至 `2.62 px`、前景 PSNR `25.95 dB`、`num_boundary_loops = 2`、消除歪脸与 `270°` 死线；后端 Python 自动化 `102` 项测试 `101` 通过 `1` 跳过、Android 12 类 `68` 项单测与 `:app:compileDebugKotlin` 通过、`reconstruct_head_v9.py` 正脸/双瞳锚点自检通过。
- 补充落地（V9 3 项高收益能力迁入衣物管线）：
  - 新增 [`reconstruction/reconstruct_garment_v9.py`](../reconstruction/reconstruct_garment_v9.py)：迁移 `SegFormer` 严格零外扩衣物掩码（`upper=(4, 7)` 兼顾长款侧背视角 + `max_hole_ratio=0.25` 闭合兜阴影暗孔 + 空衣架 `GrabCut` 回退）、无裁剪原图 `SuperPoint + LightGlue` 两级配准与 `SIMPLE_RADIAL` 去畸变、`Lab` 逆光暗部拉回 + `facing^5` 无缝 2048 UV 烘焙与 `export_glb_guard.py` Filament 扩展剥离；默认 `--no-da2` 且关闭位姿微调，按需可启用 `--use-da2` / `--enable-pose-refine`。
  - 隔离冒烟实测（实拍卫衣 `80` 帧选 `76` 清晰帧，仅跑 `extract,priors,sfm`）：`GrabCut` 回退 `0` 帧、面积分布 `[0.3592, 0.5386, 0.8183]`、裤腿/背景零渗漏；Pass 1 纯衣内角点注册 `42/76` 触发自动全图 Pass 2 回退并达 **`76/76 (100%)`** 注册、`6851` 三维点、重投影误差 **`1.3132 px`**。同步更新 `docs/garment-gaussian-mesh.md`、`docs/a-scan-modeling-trial-guide.md`、`reconstruction/README.md` 与 `README.md`。

## 2026-11-26：`top-003` V9.1 实拍重建与「写实全身 Demo」合龙收口

- 任务：完成 A 岗交付 `top-003` 实拍白色短袖印花 T 恤的 V9/V9.1 全流程重建，预裁剪并对齐 V9 真人写实头部供 `person-001_v2` 拼接全身，并将团队分工收敛为 11/30 手机端可演示 Demo。
- 诊断与方案设计：
  - 首轮 V9（`--no-da2`）实现 `90/90` Pass 1 一次性全注册（`1.359 px`、`analyze_capture.py --sparse` 12/12 扇区 100% 覆盖、`32.543 dB`），但暴露出纯白前腹低纹理凹凸坑与两侧垂放手臂遮挡带入的棕色竖条。
  - 升级 V9.1：在 `stage_train` 加入手臂语义 `ignore_mask` 防止侧腰被抠凹并提前法线平滑、`stage_mesh` 采用 `Taubin 12 + Poisson depth=8` 去微碎片、`stage_bake` 屏蔽 `19 px` 膨胀肢体区域并改用 `Open3D RaycastingScene` 加速射线遮挡测试。
- 本轮落地改动：
  - 完善 [`reconstruction/reconstruct_garment_v9.py`](../reconstruction/reconstruct_garment_v9.py)（V9.1 遮挡保护、Lab 色彩平衡与 Open3D 射线烘焙提速）并同步修复 [`reconstruction/reconstruct_head_v9.py`](../reconstruction/reconstruct_head_v9.py) 训练/预览与扩边切片细节；
  - 更新 [`docs/first-week.md`](first-week.md) 与 [`README.md`](../README.md) 明确 11/27–12/7「拼装写实全身（V9 真人头 + 固定裤鞋 + 3 独立上衣 `fixture-face-04`）+ Demo UI 化 + 共享后端」全员分工。
- 实测与验证范围：`top-003` V9.1 隔离实测在前腹消坑与色温一致性上显著改善（`7.91 MB`、`100,000` 面、`32.439 dB`、`export_glb_guard.py check` 通过）；V9 头部去毛衣裁剪并按 `frame-03130` 对齐 `person-001` 尺度后为 `3.36 MB`（`41,357` 面、四视角朝向准确、零禁用扩展）；代码与文档全量回归后同步推送 `origin/main`。
