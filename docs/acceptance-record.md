# 验收记录

## 2026-11-18 任务表更新

远端 main 已到 `7af6fc9`（`#37`/`#38`/`#39` 合入），任务表按 11/18–21 轮次重排，见[当前任务卡](first-week.md)。本轮尚未新增真机、后端或推荐复测；`fixture-face-03b` 目前只有标准库工具的导入记录，正式入口 `server/import_package.py` 导入、两衣外观与独立验收仍待执行，Q05 数量与 Q06 结论不因合入或换包自动更新。

## 2026-11-17 仓库核查

基线 `main@cfdc3d4` 与 GitHub 一致。PR #29/#30/#31 已合入；下文 PR #29“不建议合并”为 11/15 历史意见，E04 坏 GLB 提前登记生效仍按合入后缺陷处理。fixture-face-03 接收与导入记录继续有效，不再写第二件未交付；top-002 外观/真机 fit、Q04-ai 和完整 Q06 仍待验收。11/16 参赛 Word/ZIP 草稿已合入，不等于定稿或解压复验通过。

本轮检查代码、Git 历史和 Issue 评论，未新增真机通过项。下一检查点见 [当前任务卡](first-week.md)，历史测试数和通过范围不自动扩展到当前版本。

维护人 C（uniquebugle-stack）。2026-11-13 根据 #1/#12/#13/#19 最新记录同步状态并重排任务；本次文档整理没有新增真机、后端或推荐复测。开发者真机、C 模拟器、证据复核和 C 独立真机分别登记，检查点见[当前任务卡](first-week.md)。

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

- 设备：OPPO `25098PN5AC`（`fe474c80`），APK 指向 `127.0.0.1:8003`，假后端 `--fixtures outputs/fault-fixtures/corrupt-glb`。
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

