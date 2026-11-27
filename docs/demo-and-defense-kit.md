# OOTD AI「写实全身 Demo」录屏分镜、答辩 PPT 大纲与冻结打包清单

> 更新于 2026-11-27（北京时间），供 11/30 真机 Demo 录制及 12/1–12/15 参赛评审交付直接照单执行。所有引用图谱均位于 [`docs/figures/`](figures/)，均为脱敏图表，可直接用于 PPT 与答辩报告。

---

## 一、2 分 30 秒真机写实全身 Demo 录屏分镜与口播台词

- **录制前提**：C 侧共享测试后端开启 `OOTD_AI_MODE=ai` 并已用 `--expected-garments 3` 导入 `fixture-face-04`（含 `person-001_v2.glb` + 3 件上衣）；B 侧已交付瘦身并支持 3 衣横向切换的新版 APK；真机开启系统录屏（1080p）并连接共享后端地址。

| 时段 | 镜头画面操作 | 核心展示点 | 建议口播词（白话直出） |
| --- | --- | --- | --- |
| **0:00–0:20** | 打开 App 首页，点击进入「三维试衣」等待模型加载完成 | 写实全身效果、从零拼装闭环 | 「大家好，这是 OOTD AI 移动端三维试衣原型。屏幕里是真人头部 2DGS 重建后与标准素体长裤鞋拼装出的写实全身数字人，整套模型仅 4.03 MB，在普通安卓手机上毫秒级渲染流畅拖动。」 |
| **0:20–0:45** | 手指拖动数字人转一圈（正→侧→背→斜侧），再依次点击底部 3 件衣服（浅色短袖 / 深蓝 POLO / 长袖兜底版）快速切换 | 4 视角零穿模、3 衣切换防闪烁 | 「支持 360 度实时查看正侧背与领口袖口贴合。底部可以在三件实测上衣间顺畅切换，切换过程中采用双状态护栏，只有新衣解码 Ready 才上屏，绝不闪黑或丢人。」 |
| **0:45–1:25** | 在推荐栏输入 `上班` + `优先浅色` 点击「推荐」展示【AI 推荐】高亮与自动换衣；再输入 `正式西装` 演示 `no_match` 拒答保留当前衣 | `Hy4-flash` 真实大模型、不乱猜拒答能力 | 「接入服务端 Hy4-flash 大模型推荐：输入『上班、优先浅色』，系统返回匹配上衣并展示推荐理由；当要求『正式西装』时，模型触发硬约束拒答并保持原衣，不胡乱硬配。」 |
| **1:25–2:05** | 返回首页打开「3D 采集引导」，对桌面物体慢速绕行展示 12 扇区点亮、近景自动进入第 2 阶段并点击完成触发 AI 质检卡片 | CameraX 1080p、12 扇区、三态补拍指导 | 「针对三维采集难的问题，App 内置两段式采集引导：实时监测 12 扇区覆盖与局部清晰度，录制结束后直接返回结构化 AI 补拍结论（pass / invalid_retake / needs_reshoot），指导用户一次拍齐视角。」 |
| **2:05–2:30** | 切到「故障保护」演练或口播说明（可选展示断网冷启动立刻恢复缓存目录） | 离线冷启动恢复与工程门禁 | 「所有远端包都要经过 SHA-256 校验、材质扩展净化和真解码探测后才切换并写入生效指针。断网冷启动也能秒恢复上次资产，30+20 AI 评测实现 50/50 协议有效、0 违规。」 |

---

## 二、12 页答辩 PPT 逐页框架与配图映射

| 页码 | 幻灯片标题 | 本页核心内容与硬数据（全部有实测依据） | 推荐插入配图 / 素材 |
| --- | --- | --- | --- |
| **P1** | 封面页 | 项目名称「衣序 OOTD AI」——端云协同高写实三维试衣与智能采集系统（团队、赛题） | 主视觉渲染海报（待 11/30 录屏截图补入） |
| **P2** | 痛点与场景定义 | 传统虚拟试衣三大痛点：纸片感 2D 换装不立体、用户乱拍导致 3DGS 失败、端侧大体积 GLB 崩渲染/错乱覆盖 | 对比示意图（文字要点+数据） |
| **P3** | 三端协同总体架构 | Android（采集引导 + Filament）↔ FastAPI（发布门禁 + 双模 AI）↔ 离线 V9（2DGS + 全身拼装） | [`fig1_system_architecture.png`](figures/fig1_system_architecture.png) |
| **P4** | 核心技术 1：V9 多先验融合重建 | SuperPoint+LightGlue 双阶段 SfM、SegFormer+BiRefNet+DA2 先验、2DGS 7k、Open3D 2048px 烘焙与颈裁对齐（head-v9 `3.36 MB`） | [`fig2_v9_reconstruction_pipeline.png`](figures/fig2_v9_reconstruction_pipeline.png) |
| **P5** | 创新实证 1：App 引导 vs 无引导消融 | 同目标实拍 60 帧：`pycolmap` 配准率 `80.0% -> 91.7%`，点云 `+49.7%`，2DGS PSNR `19.710 -> 23.751 dB (+4.041 dB)`，LPIPS 降 `19.9%` | [`fig3_capture_ablation_trajectory.png`](figures/fig3_capture_ablation_trajectory.png) |
| **P6** | 核心技术 2：止损闸门与全身拼装 | `top-003` 穿身扫描诊断（`1021` 碎块 / `0.543` 厚度比）果断切换 B 路线模板改形（`1.08 MB`, `C_half 54.36 cm`）；合成 `fixture-face-04`（`6.69 MB`, 4/4 clean） | [`fig5_modeling_comparison_tradeoff.png`](figures/fig5_modeling_comparison_tradeoff.png) |
| **P7** | 核心技术 3：双模 AI 推荐与补拍指导 | 严格协议校验、每日先占后判配额、12 s 熔断、`invalid_retake` 禁止复用硬约束 | [`fig1_system_architecture.png`](figures/fig1_system_architecture.png)（局部高亮中列） |
| **P8** | 创新实证 2：30+20 真实大模型评测 | C 侧共享后端 `Hy4-flash` 实测：推荐 30/30 + 引导 20/20，协议有效 `100%`、条件违反 `0.0%`、502/503 为 `0`，与 Mock 毫秒基线同屏对照 | [`fig4_ai_benchmark_30_20.png`](figures/fig4_ai_benchmark_30_20.png) |
| **P9** | 工程质量：端云可靠性与发布闭环 | 下载暂存 → SHA 复核 → `GlbSanitizer`（剥离 Filament 致命扩展）→ `GlbProbe` → `PublishGate` → `PackageDirSwap`；后端 105 项 + Android 68 项单测 | [`fig6_reliability_engineering.png`](figures/fig6_reliability_engineering.png) |
| **P10** | 真机实测演示视频 | 现场播放 2 分 30 秒全身 3 衣试穿、AI 推荐与 3D 采集引导实录视频（Xiaomi 真机实录） | 11/30 录制的真机 MP4 视频 |
| **P11** | 测试总结与诚实边界 | 不把 Mock 写为真实 AI、不把模板改形写为扫描直出、遗留领圈 2–4 mm 微贴合与设备性能实测如实披露 | 对照汇总表（引自《项目测试文档》） |
| **P12** | 总结与未来规划 | 总结四大成果与后续迭代路线（分离挂拍免粘连扫衣、多部位换装槽位、云端异步队列重建） | Q&A 致谢页 |

---

## 三、Android 端与资产包适配排雷结论（针对 `fixture-face-04`）

1. **Android 数据层与渲染底层完全兼容 3 衣 + `person-001_v2`**：
   - 核查 [`CatalogModels.kt`](../android/app/src/main/java/com/ootd/ai/data/CatalogModels.kt)、[`RemotePackageRepository.kt`](../android/app/src/main/java/com/ootd/ai/data/RemotePackageRepository.kt#L86-L104) 与 [`ActiveAssets.kt`](../android/app/src/main/java/com/ootd/ai/data/ActiveAssets.kt#L24-L35)：目录解析、按 `catalog.garments` 遍历下载校验净化与 `GlbProbe` 真解码、以及转成 `GarmentOption` 列表均**无 2 件上限硬编码**，天然支持 3 件及以上衣物；`person-001_v2`（4.03 MB）由 `catalog.person` 动态加载，与旧版一致。
2. **UI 层待 B 收口的关键界面点（已写入 B 任务卡）**：
   - 目前 [`TryOnScreen.kt`](../android/app/src/main/java/com/ootd/ai/ui/TryOnScreen.kt#L606-L658) 中衣物按钮与「拉取目录」按钮挤在同一个普通 `Row` 里（每按钮 `weight(1f)`）；当变为 3 件衣物 + 1 个「拉取目录」共 4 个等宽按钮时，小屏或大字号下按钮会被压窄。
   - **B 的收口方案**：按 [`first-week.md`](first-week.md) 任务把「拉取目录」收入右上角菜单，衣物按钮区改为 `LazyRow`（或 `horizontalScroll(rememberScrollState())`）横向切换，即可完美承载 3 件及以上衣物。
3. **资产文案与导入参数提醒（致 A / C）**：
   - `local-data/handoff/tools/pack_fixture04.py` 第 36 行 `top-003` 的 `display_name` 目前写为 `"白色印花短袖"`，而 `#13` 实际交付的 `top-003.glb` 为长袖改形防撞衫版本；建议后续小修订或联调时与文案一致标注（不影响门禁与导入哈希前确认）。
   - C 侧校验与导入 `fixture-face-04` 必须传入 **`--expected-garments 3`**：
     ```powershell
     .\server\.venv\Scripts\python.exe server\validate_asset_package.py <fixture-face-04目录> --expected-garments 3
     .\server\.venv\Scripts\python.exe server\import_package.py <fixture-face-04目录> --expected-garments 3
     ```

---

## 四、12/7 功能冻结 → 12/15 参赛提交核对清单

1. **11/28–30 真机闭环**：C 导入 `fixture-face-04` 并复用 `server/tools/eval_ai_benchmark.py --catalog-version fixture-face-04` 补跑三衣推荐对比；B 交出 `arm64-v8a` 瘦身版 APK（目标 `< 40 MB`）并完成 5 次冷启动 / 20 次换衣 / 峰值内存采样；队长组织真机录屏与截图补进 PPT 和 Word。
2. **12/7 功能冻结前**：
   - 全量文档中保留的真机性能数据、B 侧瘦身幅度及 11/30 Demo 截图补齐后，运行一次 `python competition/scripts/embed_figures_and_sync_docs.py` 确保图表与口径一致；
   - 核对并复制去掉 `待填-` 前缀的最终参赛 `.docx` 交付版（保留规范命名与无隐私泄露）。
3. **重新生成纯净 `作品应用程序源代码.zip`（12/7 冻结后执行）**：
   - 使用 `git archive` 从干净 `HEAD` 打包源码（天然排除 `.git/`、`local.properties`、`.venv/`、构建缓存与任何个人隐私文件）：
     ```powershell
     git -C d:\软创文件\ootd_ai archive --format=zip --output=competition/待填-作品应用程序源代码.zip HEAD android server reconstruction docs README.md
     ```
4. **12/14 提交包最终复验**：确认所有 `.docx` 无「待补 / 未填 / 密钥 / 本地路径敏感信息」，视频、APK、源码 ZIP 逐个可打开且版本号与文档一致，12/15 准时提交。
