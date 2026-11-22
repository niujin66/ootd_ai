# 验收记录

## 2026-11-22 晚：Android 实时采集引导分支构建、单测与鸿蒙初装（`feat/android-capture-guide`）

本轮基于 `main@4dfd825` 分支开发，未改动 `TryOnScreen.kt` 试衣渲染核心与 `RemotePackageRepository.kt`。本地配置 Android SDK（`cmdline-tools 23.0.0`、`platform-tools 37.0.3`、`platforms;android-37.0`、`build-tools 36.0.0 / 37.0.0`）后完成构建与测试：

| 检查 | 实际结果 | 备注 / 未覆盖边界 |
| --- | --- | --- |
| 后端回归复跑 | `.\server\.venv\Scripts\python.exe -m unittest discover -s server/tests -p "test_*.py"` 执行 **102 项**：101 通过、0 失败、1 跳过（`test_symlink_escape_rejected`），耗时 4.707 s | 覆盖推荐 + 采集质检接口 |
| Android Debug 编译打包 | `.\gradlew.bat :app:assembleDebug`（附 `-Pandroid.overridePathCheck=true`）返回 `BUILD SUCCESSFUL`，产出 `app-debug.apk`（`82.06 MB`，含 CameraX 1.4.1 与 ML Kit `libface_detector_v2_jni.so`） | 中文仓库路径下须加 `overridePathCheck` |
| Android 端全量单测 | 12 个测试类共 **66 项**（原有 49 项 + 新增采集引导与直连 DeepSeek 测试 17 项）通过 Gradle 编译产物 + Studio JBR `JUnitCore` 运行：**66 通过、0 失败**（0.225 s） | 中文路径下 Gradle 自带 `TestWorker` `@argfile` 会触发 `ClassNotFoundException`，已用 Gradle 同一份 `.class`/`.jar` 产物在 JVM 全量运行验证 |
| 华为鸿蒙真机初装与界面交互 | 队长在自有华为鸿蒙手机安装首版 `app-debug.apk` 进入采集引导页无闪退；随后据此反馈优化了 3 步进度条、中央虚线框、白话行动提示与手机直连 DeepSeek 调试入口，并重新产出新版 APK | 尚未完成装新版 APK 后的带 Key 实拍直连 DeepSeek 全流程与录影补拍验收 |

## 2026-11-22 T02 四视角真机预检（B 侧，待 A×C 判定）

设备为 B 的 Xiaomi 17 Pro（`25098PN5AC`，adb `fe474c80`，Android 17），被测包由 `c92c65f` 构建，SHA-256 `d4402052545991ed93a3c335532f7b2d77d38700364817d6feaca2bd22ea863a`，47,958,154 B。服务端为 `fixture-face-03b`，pose `pose-001`。

**本节是 B 侧预检，不是验收判定。** 判定权在 A（@VowVole）× C（@uniquebugle-stack），判定栏留空。

### 四条前置判据

| 判据 | 实测 |
| --- | --- |
| 生效指针 `filesDir/ootd-packages/active.json` 指向 `fixture-face-03b` | 通过 |
| 三份 glb 的 sha256 与 manifest 一致 | 通过：`person-001` `f3dbe4c8…2354`（419,572 B）、`top-001` `45301ea0…5d15b5`（981,408 B）、`top-002` `8502d846…5e82`（671,540 B） |
| 顶栏显示 `服务端 · fixture-face-03b · pose-001` | 通过 |
| 底部黄字恢复提示出现 | 通过 |

### 四视角截图（10 张）

`top-001` 浅色印花短袖（圆领）：[正面](evidence/T02-20261122/top-001-正面.webp) · [正面斜角](evidence/T02-20261122/top-001-正面斜角.webp) · [侧面](evidence/T02-20261122/top-001-侧面.webp) · [斜后 34°](evidence/T02-20261122/top-001-斜后34.webp) · [背面](evidence/T02-20261122/top-001-背面.webp)

`top-002` 深蓝翻领 POLO：[正面](evidence/T02-20261122/top-002-正面.webp) · [斜侧 60°](evidence/T02-20261122/top-002-斜侧60.webp) · [侧面 90°](evidence/T02-20261122/top-002-侧面90.webp) · [斜角](evidence/T02-20261122/top-002-斜角.webp) · [背面](evidence/T02-20261122/top-002-c.webp)

### B 侧观察（粗粒度）

- `top-001` 圆领：领圈整圈贴脖根，正面与斜角可见领口上沿压向颈部，偏**紧**。
- `top-002` POLO：领子翻开成 V，与颈部之间留有空隙，偏**松**。
- 两件领口方向相反。
- 袖口贴合、下摆落到大腿中部无悬空、未见明显穿模。

2–4 mm 级别的贴合判定截图无法承载，需 A×C 现场对光复核。

### 判定栏（待 A×C 填写）

| 项 | 结论 |
| --- | --- |
| `top-001` 四视角 | 待判定 |
| `top-002` 四视角（首次独立判定，不继承 top-001） | 待判定 |

## 2026-11-22 后端回归与真实 AI 推荐验证（Q04-ai）

本机基线 `main@9c3451d`，Windows / `server/.venv`（Python 3.12）。服务端代码相对 `80de331` 未改动，所有测试均在隔离临时数据库或默认单测环境执行，凭据仅通过临时环境变量注入，未写入仓库。

| 检查 | 实际结果 |
| --- | --- |
| Q05 后端自动化 | `.\server\.venv\Scripts\python.exe -m unittest discover -s server/tests -p "test_*.py"` 新增采集质检与补拍指导 10 项单测后，共执行 **91 项**，**90 通过、0 失败、1 跳过**（Windows 无符号链接权限跳过 `test_symlink_escape_rejected`），耗时 4.129 s；覆盖既有 81 项（R06/R10/502/503）及新增 `/v1/capture/guide`。 |
| Q04-ai（有效选择 1：`q04-ai-01`） | 隔离导入 `fixture-face-03b` 后经 FastAPI `POST /v1/recommendations` 实测（模型 `deepseek-flash`）：输入「下午上课 / 优先浅色」→ HTTP 200，`status=ok`，`garment_id=top-001`，`mode=ai`，服务端耗时 1.5541 s（281 tokens），理由符合浅色短袖。 |
| Q04-ai（有效选择 2：`q04-ai-02`） | 同环境输入「晚上聚会 / 优先深色」→ HTTP 200，`status=ok`，`garment_id=top-002`，`mode=ai`，服务端耗时 1.9276 s（301 tokens），理由符合深蓝翻领 POLO。 |
| Q04-ai（无匹配：`q04-ai-03`） | 同环境输入「运动 / 优先红色或粉色」→ HTTP 200，`status=no_match`，`garment_id=null`，`mode=ai`，服务端耗时 3.3126 s（408 tokens），理由明确说明无红/粉色运动款衣物。 |
| 采集质检与补拍指导（`POST /v1/capture/guide`） | 运行 `server/tools/demo_capture_guide.py`（`OOTD_AI_MODE=ai`，`deepseek-flash`）：① 头部缺口样例（180° 覆盖、81° 翻滚、单眼 3 px、配准 8.8%）→ HTTP 200（8.455 s），判定 `invalid_retake`、`can_reuse_existing=false`，输出 4 条重拍步骤；② 服装缺口样例（缺 180°–240° 扇区、上下留白 21 px、连续性 0.291）→ HTTP 200（5.394 s），判定 `needs_reshoot`、`can_reuse_existing=true`，输出 4 条补拍步骤。 |
| Q04-ai 端到端边界 | 真实模型服务端路径（推荐 + 补拍指导）均已通过；Android 真机在 `OOTD_AI_MODE=ai` 下的联调驱动换衣待后续真机时段一并复测。 |

## 2026-11-21 卫衣网格与 Android 复核

| 对象与环境 | 已有证据 | 待验项 |
| --- | --- | --- |
| 卫衣离线网格，第 45 轮 | 回读 GLB 得到 203,560 个顶点、396,240 面。帽缝两侧的同向边由 225 降至 74，单侧边仍有 92 条。[实验记录与文件指纹](garment-gaussian-mesh.md) | 衣身侧接缝、下摆原片边界及后续穿着适配 |
| B，Xiaomi 25098PN5AC，PR #41 的 `c92c65f` | 重打包 APK 的 SHA-256 为 `d4402052545991ed93a3c335532f7b2d77d38700364817d6feaca2bd22ea863a`。字号 1.0 与 1.45 均能启动、恢复 `fixture-face-03` 并显示人物与衣物。1.45 下推荐卡片名称被截断。[B 的真机记录](https://github.com/niujin66/ootd_ai/pull/41#issuecomment-7041167205) | 同包 03b 四视角、第二件失败与中断恢复 |
| C，Android 17 / API 37 模拟器，`e5b6423` | 在故障服务上，E04 拒绝坏 GLB 并保留旧衣；X01 强停后断网冷启动，恢复 `fixture-face-03`，两衣可切换。[C 的复测记录](https://github.com/niujin66/ootd_ai/issues/1#issuecomment-7004290573) | C 用 B 的 APK 和正式服务独立跑真机；另在故障服务验证 E01–E03 与 X02 |

## 2026-11-19 卫衣高斯转网格实验

| 检查 | 实际结果 |
| --- | --- |
| Q03-gs-pose 相机对齐 | 第 32 轮单一模型登记 199/199 帧，前、侧、背进入同一重建。稀疏点重投影误差中位 0.977 px。 |
| Q03-gs-export 网格导出 | 第 33 轮与第 36 轮的 GLB 可重新读取。第 33 轮通过同角度对照确认衣摆下方的长条杂面已去除，保留为实验底版。 |
| Q03-gs-cuff 袖口补全 | 第 36 轮同一局部与工作遮罩的 IoU 在两帧分别由 0.697 升至 0.886、由 0.682 升至 0.856。这两帧参与外侧补面构建，分数仅用于对照。独立复核发现面片没有共用接缝边，斜侧有尖片和碎片。腕口中央射线通过，近边缘 8 条中有 3 条被补面挡住。外观和拓扑验收未通过。 |
| Q03-gs-release 交付范围 | 第 36 轮保留为实验候选。帽缘、下摆、贴图、人体适配和 Android 整包验收待验证。 |

同角度对照、数据来源和下一轮判据见[服装高斯转网格记录](garment-gaussian-mesh.md)。03b 的既有验收范围单独记录如下。

## 2026-11-19 03b 尺寸复测与静态预检

| 检查 | 已登记结果 |
| --- | --- |
| Q03-fit 胸宽口径 | 首件平铺胸宽 W_flat 53.0 cm，模型固定 y=1.30 m 截面半围 C_half 54.35 cm，差 +1.35 cm（W_proj 39.48 cm 仅记录不判定）。2026-11-22 队长拍板采用**方案 B（成衣外轮廓现状）**、**成对登记 + 固定 y=1.30 m**、**容差 +1.5 / −0.5 cm**；理由：+1.35 cm 为几何必然项且静态仿真表明改胸宽不解决领圈穿入，保持 03b 现状不挤占真机窗口（数值在容差内，两衣真机外观仍待实际核对）。 |
| Q03-fit 领口静态预检 | 短袖和翻领上衣的领口最大局部穿入分别约 3.7 mm 和 2.3 mm。两衣的合并版 APK 四视角验收仍待登记。 |
| Q03-gltf 导出兼容 | 发布前可用 `server/tools/export_glb_guard.py` 检查并清理材质扩展。Android 最终表现仍按实机验收。 |

## 2026-11-18 兼容包导入与后端回归

本机基线 `main@80de331`，Windows / Python 3.12.13。#37–#39 均已合入，检查点见[11/18–21 任务卡](first-week.md)。

| 检查 | 实际结果 |
| --- | --- |
| 03b 正式入口 | `server/import_package.py` 在独立 SQLite 与资源目录首次导入成功。目录 `fixture-face-03b`、姿势 `pose-001`，数据库登记一个版本、三项资产。相同包第二次导入返回已存在并跳过，两个命令均 exit 0。 |
| 交付文件核对 | 人物、短袖、POLO 的源 GLB、`validation-report.json` 与导入副本 SHA-256 三方一致。`content_digest` 为 `f0c05a0d8b86a8bfbbb5b314692af66cc5468f252c957bae6e6490b4f7388ca0`，与 C 的标准库导入记录一致。 |
| Q05 后端回归 | `python -m unittest discover -s server/tests -p 'test_*.py'` 共执行 81 项，80 通过、0 失败、1 跳过。 |
| Android 与外观 | B 在开发分支的真机记录覆盖越界 GLB 被拒、旧素材保留，以及原始实衣包加载后无闪退。合并版 APK、第二件加载失败、两衣四视角贴合、最后远端包离线恢复和 C 独立真机结果待登记。 |
| Q04-ai | 真实模型调用未测，配置与网络条件见 #26。 |

复跑时给 `server/import_package.py` 显式传 `--db-path` 和 `--assets-root`，将候选库和演示库隔离。包路径为 [`local-data/handoff/T02-candidate-20261117/`](../local-data/handoff/T02-candidate-20261117/)。

证据见 [C 的导入记录](https://github.com/niujin66/ootd_ai/issues/19#issuecomment-6948440976)、[B 的真机记录](https://github.com/niujin66/ootd_ai/issues/12#issuecomment-6975814951)和[模型配置记录](https://github.com/niujin66/ootd_ai/issues/26#issuecomment-6948441584)。

## 2026-11-17 仓库核查

基线 `main@cfdc3d4` 与 GitHub 一致。PR #29/#30/#31 已合入；下文 PR #29“不建议合并”为 11/15 历史意见，E04 坏 GLB 提前登记生效仍按合入后缺陷处理。fixture-face-03 接收与导入记录继续有效，不再写第二件未交付；top-002 外观/真机 fit、Q04-ai 和完整 Q06 仍待验收。11/16 参赛 Word/ZIP 草稿已合入，不等于定稿或解压复验通过。

本轮检查代码、Git 历史和 Issue 评论，未新增真机通过项。下一检查点见 [当前任务卡](first-week.md)，历史测试数和通过范围不自动扩展到当前版本。

## 2026-11-13 验收快照

维护人 C（uniquebugle-stack）。以下为按 #1/#12/#13/#19 留存的历史状态与旧检查点。开发者真机、C 模拟器、证据复核和 C 独立真机分别登记。

| 编号 | 检查项 | 当前证据与结论 | 下一步 |
| --- | --- | --- | --- |
| Q01 | 两件衣物的照片和尺寸 | C 在总表 #1 登记素材验收通过；原图、尺寸与模型包的接收记录分别保留 | 缺少的原图交接由 A/C 明确接收方，不由模型包推断齐全 |
| Q02-1 | Android 首页启动 | B 运行、C 在总表 #1 登记通过 | 保留原记录 |
| Q02-2 | 手机旋转与两衣切换 | B 真机开发记录通过；C 的 Medium Phone 37.0 模拟器复测通过，仅覆盖测试资产交互 | 新实衣包真机独立复测，补失败回滚 |
| Q03-body | 本人体型 | 本人确认 B 版体型基本合适 | 使用固定基准做衣物适配 |
| Q03-look | 面部、头发、固定下装 | 头部完成相机恢复及轮廓体积、PIXIE、UV、多视角拟合和 MICA 对照；留出帧下巴偏差降到 4.3/4.4 px，耳位与颌线仍偏离本人，外观未通过。头发和下装仍是近似 | 11/15 前不新增实验，保留身体基准；之后根据耳位/眼睑对应点与采集缺口另定小样，不重复 MICA 已做实验 |
| Q03-fit | 两件真实衣物贴合 | top-001 v1 失败；v2 于 11/12 经 C 复核四处版型、贴图与穿模通过，仅限首件；胸宽口径仍待定版。top-002 尚无独立通过记录 | A 11/14 12:00 灰模、16:00 贴图；C 19:00 真机两衣分别验，缺件保持未测 |
| Q04-mock | 后端推荐编号 | B/C 复现三场景；B 有手机联调记录，C 11/12 用 fixture-face-02 模拟器复现目录及推荐，已复核 B 证据 | C 11/14 19:00 独立操作真机并使用实衣包；POLO 按实际标签测试 |
| Q04-ai | 真实 AI 推荐 | 未测；#15 mock 完成不等于真实模型验证完成 | C 11/14 12:00 明确配置缺项，11/15 16:00 交真实调用记录或阻塞；本地模拟错误单独登记 |
| Q05 | 后端自动化 | 队长本机 73 项、72 通过、1 跳过。C 于 2026-11-10 13:19 在本机（Windows、Python 3.13）复跑 73 项、72 通过、1 跳过，与基线一致 | 通过，保留 `Ran 73 / OK (skipped=1)` 输出。11/15 晚新增 R06/R10 改动与测试，尚未在 C 的 Python 3.13 环境复跑，复跑后再更新数量 |
| Q06 | 缓存、旧响应、失败恢复 | B 11/12 真机降级、恢复和缓存命中通过，C 复核证据；离线冷启动落回 APK 内置人物。C 独立真机、恢复最后远端包、旧响应、损坏/解码失败与提交中断仍未完成 | 11/15 晚已备好故障注入工具（`server/tools/`），待真机连上后按 E02/E03/E04 复测；其余用例后续另记 |

## 11 月 15 日晚：C 后端改动与故障注入工具

只动了仓库里的代码，没碰 C 盘任何原有文件。改动都还没在真机上跑，等连上真机和 C 的 Python 3.13 环境再复测。

**R06（推荐解析严格）**：`server/recommender.py` 的 `validate_model_output` 原来用 `get` 读 `garment_id`，模型少给这个字段时会当成「没匹配到」。现在改成：字段缺失、类型不是字符串、ID 不在候选里，都直接报协议错误；只有明确给了 `null` 且理由合法，才算「没匹配到」。这样模型输出不合规时 App 收到的是 502，而不是假装没事。

**R10（超时与额度）**：服务端默认超时从 20 秒降到 12 秒，短于 Android 的 20 秒读超时，保证服务端先返回 503、客户端能收到明确提示。额度占用改成先写数据库再返回调用数，超了立刻回退并报错，多个连接之间也能看到同一份占用，不会超用。AI 调用失败（超时、上游出错、输出不对）现在也会记一条日志，带耗时，方便事后排查。

**新增测试**：`server/tests/test_recommender.py` 补了 8 个用例，覆盖缺字段、类型错、空理由、默认超时短于客户端、跨连接额度、以及接口层的 502/503 和失败日志。这些测试本机跑不了（这台机器没装 fastapi），要拿到 C 的 Python 3.13 环境跑。

**故障注入工具**：新增 `server/tools/`，纯 Python 标准库，不用装东西。

- `make_fault_package.py` 生成「正常 / 哈希错 / 坏文件」三份候选包。默认拿 `local-data/handoff/T02-candidate-20261113` 里的真实 GLB 当素材（没有才用占位字节），三份的 `catalog_version` 故意不一样，方便区分。
- `fault_server.py` 把候选包当成假后端跑起来，接口和真后端一致，支持 `--delay` 复现「延迟旧响应被丢弃」、`--token` 鉴权、`--recommend` 指定推荐结果。
- `import_candidate.py` 在没有 trimesh 的机器上做「校验 + 导入」：真解 GLB 头（版本、JSON chunk、accessor 范围、顶点/三角面数），核对体积和 sha256，再写进数据库和 `assets_root`。

这些工具等真机连上后用来跑 E02/E03/E04。

## 11 月 15 日晚：候选包校验导入（fixture-face-03）

A 11/13 交的两件实衣候选包已在这台机器上校验并导入，结果是：

- `catalog_version` = `fixture-face-03`
- `content_digest` = `466d567d6f54089667a359db870cd88fd27c255d17b8dd0cb3ef0ffb8a6d8b77`
- person-001.glb：419572 字节，sha256 `f3dbe4c875dafb73…`，10475 顶点 / 20908 三角面
- top-001_v2.glb：981688 字节，sha256 `64bca93f973f1956…`，6369 顶点 / 10692 三角面
- top-002_v1.glb：672096 字节，sha256 `ceda31dc18e2ee21…`，6503 顶点 / 12616 三角面

顶点和三角面数与 A 的 `validation-report.json` 完全一致，说明包没被改过。导入写进了本机 `outputs/ootd.db`，资源落在 `outputs/server-assets/person-001/fixture-face-03/`。原来那份库备份在 `outputs/ootd.db.bak`。

**还没做的**：这是在本机导入，不是真机；B 那边要用的新 `catalog_version` 和三个文件的哈希还没发到 issue 里；真机两衣切换、fit、Q04/Q06 的独立复测也还没做。

## 11 月 15 日晚：PR #29 复核

B 的 R01–R04 复核完了，结论是**先别合并**：`isValid()` 只看哈希不看能不能解码，哈希对但 GLB 坏的包会被存成生效版本、冷启动还会反复恢复，正好违反 E04。详细问题和复测建议见 [PR #29 复核意见](review-pr29-20261115.md)。

## 11 月 13 日状态同步来源

- [C 11/12 总表](https://github.com/niujin66/ootd_ai/issues/1#issuecomment-6821867569)：首件 v2 通过、B03 基础范围已验收、第二件与独立真机未完成。
- [首件 v2 复核](https://github.com/niujin66/ootd_ai/issues/13#issuecomment-6812670611)：只覆盖 top-001，尺寸口径保留待办。
- [B 真机降级恢复记录](https://github.com/niujin66/ootd_ai/issues/12#issuecomment-6813309921)：缓存命中与回落内置包；未测新版本隔离。
- [C 模拟器与证据复核](https://github.com/niujin66/ootd_ai/issues/19#issuecomment-6821880885)：top-002 同哈希占位、清单名称乱码、独立真机/断网未测。
- [PR #25](https://github.com/niujin66/ootd_ai/pull/25)：降级提示和错误文案已改；其复网后提示消失仍待实际复测。

## 11 月 11 日状态来源

- [C 总表更新](https://github.com/niujin66/ootd_ai/issues/1#issuecomment-6797011638)：模拟器交互结论、首件失败、11/12 检查点。
- [C 首件衣物复核](https://github.com/niujin66/ootd_ai/issues/13#issuecomment-6791071735)：四处版型问题与尺寸疑问。
- [B 项目 FastAPI 复现](https://github.com/niujin66/ootd_ai/issues/15#issuecomment-6798898921)：鉴权、目录、三文件哈希、mock 三场景、72通过/1跳过；明确真机联调与真实模型未完成。
- [B03 PR #24](https://github.com/niujin66/ootd_ai/pull/24) 与 [Android README](../android/README.md)：实现及等价桩服务真机记录。

[方法调研](method-research.md)和[架构 v0.3](architecture.md)是设计产物；[实施清单](implementation-plan.md)中的预算及异常用例尚非测试结果。

## 匿名源码包复测

2026-11-10 从源码 ZIP 解压目录复跑，Windows、Python 3.12。73 项中 72 通过、0 失败、1 项因符号链接权限跳过。匿名包 fixture-anonymous-01 首次导入成功，相同内容重复导入安全跳过。HTTP 健康、鉴权及目录检查通过，三份 GLB 下载长度和 SHA-256 匹配。模拟推荐按浅色、深色、红色偏好依次返回 top-001、top-002、no_match。匿名模型的 Android 真机效果待单独复测。

从匿名源码 ZIP 解压后，使用 JDK 25 和 Android SDK 37 执行 gradlew.bat :app:assembleDebug，构建成功。比对 APK 中的清单和三件 GLB，四份文件均与 ZIP 内对应文件逐字节一致。该结果覆盖构建和打包，匿名样例的真机画面待复测。

## E04 缺口修复（针对 PR #29 复核意见）

2026-11-16，B（Journey668），本机 Windows / JDK 25 / Android SDK 37。

[PR #31](https://github.com/niujin66/ootd_ai/pull/31) 附带的 `docs/review-pr29-20261115.md` 给出的结论是「不建议现在合并」，其中第 1 条是阻塞项：`isValid()` 只查大小与 sha256、不查能不能解码，一个「哈希完全正确、GLB 内部是坏的」包会被当成正常版本存下、标成生效版本、上屏失败，并且下次冷启动还会被再次恢复出来。PR #29 已经合并，这条缺口随 main 保留，本次单独修。

**改动（仅 Android 端，未动服务端）**

| 文件 | 改动 |
|---|---|
| `data/GlbProbe.kt`（新增） | 按 glTF 2.0 容器格式做结构探测：magic/version/声明长度、第一个 chunk 必须是 JSON、JSON 可解析、`asset.version` 是 2.x、至少一个 mesh 且有图元。不通过即抛带中文原因的异常。 |
| `data/RemotePackageRepository.kt` | `isValid()` 在大小与哈希之后加真解码探测；`sync()` 不再写生效指针，新增 `publishActive()` 由上屏成功后调用；`sync` 与 `restoreLast` 用同一把 `Mutex` 互斥。 |
| `data/PersistentCatalogStore.kt` | `versionsOf()` 改按 manifest 的 `saved_at` 倒序（原按目录 mtime，跨目录 rename 后不准），新增 `readSavedAt()`。 |
| `ui/TryOnScreen.kt` | 远端包「人物 Ready」后才 `publishActive`；恢复出来的包解码失败时退回内置素材（只退一次，避免死循环）。 |

**已验证（本机实际执行）**

- `:app:compileDebugKotlin` 构建成功。
- `:app:testDebugUnitTest` 中 `GlbProbeTest` 5 项全部执行、0 跳过、0 失败：真模型通过；改一个字节的同长度坏包被拦；截断包被拦；随机字节被拦；空文件被拦。
- 覆盖的是「结构探测」这一段。它挡住的是结构坏、非模型这两类；它不做几何与 accessor 数值的完整解码，这一点在 `GlbProbe` 的注释里写明。

**真机复测（E04，2026-11-16 晚）**

- 设备：Xiaomi `25098PN5AC`（`fe474c80`），APK 指向 `127.0.0.1:8003`，假后端 `--fixtures outputs/fault-fixtures/corrupt-glb`。
- 第一轮**没有通过**：只把探测挂在 `isValid()` 上，`sync()` 下载完只查了 size 和 sha256。结果坏包一路过：切目录 → 人物（好文件）上屏 → 衣物报 `Failed to parse glTF model from buffer` → 人物一变，`publishActive` 又把生效指针写成了 corrupt 版本。表现是标题变成 `服务端 · fixture-face-03-corrupt`、人物被换掉、衣服没了。**这正是复核意见第 1 条描述的场景，说明只修恢复侧不够。**
- 补 `probeDecodable()`：下载每个资产后立刻探测，坏包在切目录之前就失败。
- 第二轮**通过**，三个检查点逐条：
  1. **冷启动不恢复坏包** —— 上一轮已经把 corrupt 版本写进缓存和生效指针；新包冷启动后顶部是「本地包 · fixture-face-03」，退回内置素材，没恢复坏版本。
  2. **同步阶段就拦住** —— 点「拉取目录」后报「远端同步失败：top-001 解不开（GLB 头不对：magic 不是 glTF），已保留上一套素材」，没有切目录、标题没变。
  3. **旧素材保留** —— 人物和衣物都还是原来那套，画面没有空档。

**未验证**

- X01 离线冷启动恢复两衣、E02 延迟旧响应被丢弃：本次只跑了 E04，这两条还没在真机上跑过。

## 每次检查填写

```text
任务/验收编号
检查人和时间
代码提交、资产目录版本、设备与运行环境
输入编号
操作步骤
预期结果
实际结果
结论（通过 / 失败 / 未测）
内部证据索引
缺项、负责人、下次检查时间
```

一次检查只能登记实际执行的范围。素材已验收、素材已送达、电脑预览通过、Android 通过分别记录。将具体异常交给对应负责人，公开记录用编号定位，个人影像和运行凭据保存在内部。

