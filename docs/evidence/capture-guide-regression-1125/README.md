# 「3D 采集引导」真机回归证据（2026-11-25 夜）

B 岗任务卡 11/25–26 项：真机回归 `main@de055e1` 的采集引导，覆盖任务卡第 2 条列出的六项验收点。

- 设备：Xiaomi 17 Pro（`25098PN5AC` / `ro.product.name=pandora`），Android 17，序列号 `fe474c80`
- 被测包：本地从 `main@de055e1` 构建的 debug APK（88,958,052 B）
- 构建环境：JDK `C:\Program Files\Android\Android Studio\jbr`
- 场景：室内卧室，背景为被子、纸巾包、木棍等杂乱堆叠物（刻意制造干扰）

> 说明：`de055e1` 是队长直接改进 B 岗区域的提交，本证据是**复核**它是否生效，不是它的作者自测。

## 结论

| 验收点 | 结果 | 依据 |
|---|---|---|
| ① 不再误报「退回上一步」 | ✅ 未复现 | 杂乱场景 12/12，出现的是 `MANUAL_GUIDE` 的 n/12 扇区提示而非旧 `TOO_CLOSE` 拦截，见下 |
| ② 预览 ≥ 24 FPS | ✅ 93–95 FPS | `dumpsys gfxinfo` 累计帧数分段计算，见下 |
| ③ 连续运行无 OOM | ✅ 8 分钟内存持平 | PSS 353–374 MB、Native Heap 153–156 MB 往复波动 |
| ④ 录像保存到相册 | ✅ 3 个文件全部可解码 | 自写 MP4 容器解析器验证 `moov`/`avc1`，见下 |
| ⑤ 连后端 `/v1/capture/guide` | ✅ 200 OK | 后端访问日志 + 结果页渲染三步建议 |
| ⑤ 附带：离线降级 | ✅ 生效且文案完整 | 断链时自动走 `OfflineCaptureDiagnoser`，App 不崩 |
| ⑥ 直连 DeepSeek 调试 | ✅ 走通并返回真实 AI 建议 | 结果页来源标签「直连 DeepSeek 调试」+ 代码路径唯一，见下 |

**未覆盖**（不算通过，也不该写成通过）：

- 暗光、快速旋转、画面里有人走过、极近距离贴脸
- E01–E04 故障注入场景
- X05 连续切换 20 件衣物
- 真 AI 后端（本次后端以 `OOTD_AI_MODE=mock` 运行，验的是链路不是推理质量）
- **四边留白 `min_margin_px`**（原因见下节）

## 包与约束（11/25 12:01 规则，务必对照）

T02 状态卡把室内误报的验法按包分流，两条约束直接影响本证据的适用范围：

> 室内杂乱背景（门框/家具/地砖强边缘）下，`32d6db8e…`（`main@a57598e`）会误报「太近/往后退」，队长 `de055e1` 已修（新增 `MANUAL_GUIDE`）。老包仍可拍，但该提示一律无视；`min_margin_px` 只能靠 PC 端离线质检复核，判据不变（四边 ≥40 px）。

- **老包** = `32d6db8e…`（1080p 终版）→ 室内误报不能真机验收，只能 PC 端复核
- **新包** = 含 `de055e1` 的构建 → 可做真机验证

本次被测包是**本地从 `main@de055e1` 构建**，代码层面等同状态卡所指的「新包」，因此①可以做真机判定。

### 包身份核对（已用 dex 字节码比对，非猜测）

状态卡写的是「待队长重打私发」，但核对后发现**队长 11/25 已私发的那个包本身就含 `de055e1`**，即状态卡所指的「新包」已经存在。

| 检查项 | 队长私发包 `lead-1125.apk`<br>（86,048,755 B / SHA `55404df6…`） | 本地构建 `app-debug.apk`<br>（88,958,052 B） |
|---|---|---|
| `MANUAL_GUIDE` 枚举 | ✅ `classes5.dex`、`classes6.dex` | ✅ `classes5.dex`、`classes6.dex` |
| 「请目视将目标完整装进中央虚线框」 | ✅ `classes6.dex` | ✅ `classes6.dex` |

两个包 9 个 dex，命中位置完全一致。**结论**：

1. 队长私发包**不是老包**（老包是 `32d6db8e…` / `main@a57598e`），它已含 `de055e1` 的 `MANUAL_GUIDE` 修复。
2. 本地构建与队长包的字节码一致，① 的判定对两者同样成立，**不存在"等新包再验"这一步**。
3. 两者仍有签名与内置资产差异（队长包装不上是因为 `INSTALL_FAILED_UPDATE_INCOMPATIBLE`，签名不匹配，非版本问题）。换装队长原包需先卸载，且会清掉机上 fixture 资产；鉴于 dex 已一致，本次未做。

> 提示 A/C：状态卡里「待队长重打私发」可能已过期。队长包在 11/25 19:04 生成、21:02 已下发，而 A 的状态卡是 22:36 提交的，写卡时可能还不知道包已发出。

### `de055e1` 到底改了什么（决定①的判断依据）

据 `docs/capture-guidance-design.md`：修复后 `SubjectFraming` 增加 `reliable` 标志，杂乱背景返回 `FramingStatus.MANUAL_GUIDE`：

- **不再**用梯度外接框判 `TOO_CLOSE` / `TOO_FAR`
- **不参与**阶段 1 的占比与留白统计
- 改为提示「请目视将目标完整装进中央虚线框（四周留边），迈步 X（已点亮 N/12 格）」
- 阶段 2 细节像素改用保守占比 `0.60` 估算

**所以①的证据不只是"没看到问题"，而是观察到了新逻辑的特征输出**：本次全程出现的是 `👉 向左补几步缺失方向 (n/12)` 这类扇区覆盖提示，正是 `MANUAL_GUIDE` 路径下「已点亮 N/12 格」那条，而不是旧的 `TOO_CLOSE` 拦截。

### 由此产生的边界

杂乱背景上端侧**不再实时拦截留白**，`min_margin_px` 只能由后端离线质检给出。因此本证据**不覆盖**四边留白判据——那需要在电脑端对录像跑离线质检，判据仍是四边 ≥40 px。见「未覆盖」最后一条。

## 逐项明细

### ① 不再误报「退回上一步」

| 项 | 观测 |
|---|---|
| 场景 | 室内卧室，背景含被子堆、纸巾包、木棍等强边缘物 |
| 扇区覆盖 | 12/12，最大缺口 0° |
| 提示类型 | 全程 `👉 向左补几步缺失方向 (n/12)`，属 MANUAL_GUIDE 扇区提示 |
| 是否出现「退回上一步」/ `TOO_CLOSE` 拦截 | **未出现** |
| 阶段跳变 | 未观察到 `②绕圈` 异常回退到 `①对齐` |

判定：在含 `de055e1` 的构建上，杂乱室内背景**未复现**误报退回。注意这是「未复现」，单场景单次，不等于「已彻底修复」。

### ② 预览帧率

采样窗口 21:44:31–21:54:01，用 `dumpsys gfxinfo` 的累计帧数做差分：

| 时间段 | 帧数增量 | 实际帧率 |
|---|---|---|
| 21:44:31 → 21:45:47 | 7,152 | 94.1 FPS |
| 21:45:47 → 21:46:57 | 6,670 | 95.3 FPS |
| 21:46:57 → 21:48:08 | 6,682 | 94.1 FPS |
| 21:48:08 → 21:49:18 | 6,696 | 95.7 FPS |
| 21:49:18 → 21:50:29 | 6,608 | 93.1 FPS |
| 21:50:29 → 21:51:40 | 6,554 | 92.3 FPS |
| 21:51:40 → 21:52:50 | 6,558 | 93.7 FPS |

原始数据见 `monitor-10min-1125.csv`。

补充：因为 CameraX 预览用 Compose `TextureView` 合成进 Activity，没有独立 SurfaceFlinger 图层，所以无法直接读预览层 FPS，这里用 GS 渲染帧数作为代理指标。同一时段还并行跑着 10 分钟录像，是「边录边预览」的真实状态。

### ③ 内存

| 时刻 | PSS (KB) | Native Heap (KB) |
|---|---|---|
| 21:44:31 | 353,225 | 157,817 |
| 21:45:47 | 365,134 | 158,526 |
| 21:46:57 | 368,724 | 158,098 |
| 21:48:08 | 362,354 | 159,678 |
| 21:49:18 | 358,750 | 157,550 |
| 21:50:29 | 368,016 | 157,386 |
| 21:51:40 | 374,320 | 157,349 |
| 21:52:50 | 364,240 | 156,925 |

8 个点来回震荡，没有单调上升趋势。泄漏会表现为每个点都比前一个略高，本例不是。

**关于最后一段**：21:52:50 → 21:54:01 帧数只涨 1,294（折算 18 FPS），PSS 从 364 MB 掉到 273 MB。经确认是系统按 10 分钟默认超时熄屏，相机与渲染同时暂停。该段不计入统计，也不作为异常上报。

### ④ 录像落盘

`/sdcard/Movies/OOTD-Capture/` 下当晚产出 3 个文件，用 `mp4probe.py` 解析容器结构：

| 文件 | 大小 | 时长 | 帧数 | 编码 | 码率 |
|---|---|---|---|---|---|
| `OOTD_head_20261125_214304.mp4` | 731.10 MB | 611.59 s（10 分 19 秒） | 18,374 | avc1 | 10.0 Mbps |
| `OOTD_head_20261125_215804.mp4` | 48.10 MB | 30.47 s | 916 | avc1 | 13.2 Mbps |
| `OOTD_head_20261125_215737.mp4` | 22.44 MB | 13.35 s | 401 | avc1 | 14.1 Mbps |

三个文件都是 `ftyp` → `mdat` → `moov` 的完整结构，`moov` 在尾部（非 faststart，不影响本地播放）。编码帧率分别为 30.04 / 30.07 / 30.04 FPS。

**最大那个是本项最有力的证据**：611.59 秒连续录制、18,374 帧、`moov` 完整写入。OOM 或崩溃会导致录制中断、`moov` 写不进去、文件变砖——这三件事一件都没发生，所以「连续 10 分钟无 OOM」这条比单看内存曲线更充分。

结果页同时显示了 MediaStore URI（`content://media/external/video/media/19530`、`19532`），说明除写文件外还做了系统相册索引登记。

### ⑤ 连后端 / 离线降级

两次抓屏正好覆盖两条路径：

| | 第一次（降级） | 第二次（连上后端） |
|---|---|---|
| 摘要 | 「服务端暂不可用（Failed to connect to /127.0.0.1:8000），端侧离线检测到 1 项问题」 | 「…按步骤正向补拍即可（规则评估）」，**无服务端不可用提示** |
| 建议条数 | 1 条（缺特写） | **3 条**（环绕轨迹连续性 / 尺度参考 / 关键部位近景） |
| 细节 | 端侧自算 | 带阈值：相邻帧最低相似度 **-0.299（<0.35）**、缺 ArUco 标定卡 |

- `result-offline-fallback.png`：降级页，扇区覆盖 12/12 仍正常显示，端侧诊断给出具体缺失角度（正脸特写、左斜 -30°、右斜 +30°），App 不崩不卡。
- `result-backend-guide.png`：后端页，同期 uvicorn 访问日志出现 `POST /v1/capture/guide HTTP/1.1 200 OK`。

联调方式（App 未做任何配置修改）：

```bash
# 后端（mock 模式，不设 OOTD_AUTH_TOKEN 即不校验令牌）
cd D:/sic/ootd_ai
OOTD_AI_MODE=mock server/.venv/Scripts/python.exe -m uvicorn app:app --app-dir server --host 127.0.0.1 --port 8000

# 隧道
adb reverse tcp:8000 tcp:8000
```

App 编译期默认 `OOTD_API_BASE_URL = http://127.0.0.1:8000`、`OOTD_API_TOKEN = ""`（`local.properties` 无 OOTD 配置时），正好与服务端未设令牌的开发模式匹配，所以走 `adb reverse` 即可打通，无需改地址也无需重新编译。

> 踩坑记录：本机 MSYS 环境下 adb server 会随命令进程退出而消失，导致 `adb reverse` 设置完立刻丢失。需用 `adb nodaemon server` 常驻，并额外挂 watchdog 每 5 秒检查 `adb reverse --list`，断了自动重建。第一次联调失败就是隧道中途掉了，与代码无关。

### ⑥ 直连 DeepSeek 调试（11/26 补测）

11/25 夜缺这个 Key，只验了「后端」和「离线降级」两条路；11/26 21:23 用用户自有 Key 补齐。

配置（App 内「开发者调试 → AI 指导连接方式」）：

| 项 | 值 |
|---|---|
| 开关「开启手机直连 DeepSeek API」 | 打开 |
| API Key | 已填（仅存本机，本证据不记录其值） |
| API Base URL | `https://api.deepseek.com/v1` |
| 模型名称 | `deepseek-v4` |

面板状态显示「当前：手机直连 DeepSeek（无需电脑后端）」，说明 `directDeepSeekEnabled` 与 `deepSeekApiKey` 两个前置条件同时满足。

**判定不是靠"看起来像 AI 写的"，而是靠代码路径唯一**。`DirectDeepSeekCaptureGuide.generateGuide()`（`DirectDeepSeekCaptureGuide.kt:47-77`）里 `mode = MODE_DIRECT_AI` 只在一条路径上赋值，且必须四步全过：

1. `apiKey` 去空格后非空（否则抛 `未填写 DeepSeek API Key`）
2. `httpResp.statusCode in 200..299`（否则抛 `DeepSeek 接口返回错误`）
3. `extractChatContent()` 从响应体取到内容
4. `parseAndValidateGuideOutput()` 严格校验通过（含 `invalid_retake` 不得声称 `can_reuse_existing=true` 这类约束）

任一步失败都会抛异常 → `finishAndInspect()` 的 `getOrElse` → `OfflineCaptureDiagnoser.buildFallback()` → `mode = "offline_fallback"`（`CaptureGuideModels.kt:461`），结果页标签就会是别的字样。

**三处独立观测互相印证**：

| 位置 | 观测 |
|---|---|
| 准备页面板 | 「当前：手机直连 DeepSeek（无需电脑后端）」 |
| 分析中状态文案 | 「正在本地整理质检数据，并**直连 DeepSeek** 生成补拍指南…」（`CaptureGuideScreen.kt:312`） |
| 结果页来源标签 | **「直连 DeepSeek 调试」**（`CaptureGuideScreen.kt:1335`，仅在 `mode == "direct_ai"` 时出现） |

截图见 `result-direct-deepseek.jpg`。

附带结论：`deepseek-v4` 这个模型名**是有效的**——若模型名不存在，`/chat/completions` 会返回 404 并抛异常，走不到 `MODE_DIRECT_AI`。

本次返回的四条建议都带 reason code 与实测数值（`edge_cropped`、`camera_roll_warning 翻滚 41.4°`、`orbit_jump 连续度 -0.594`、`missing_closeups`），扇区覆盖 `12/12`（最大缺口 0°）——即模型确实读到了端侧指标，不是套模板。

### 已知问题：录像 Finalize 报 `error=4`，但文件实际完好

同一次补测中，结果页出现了 `录像保存警告（error=4），分析指标仍已保留`。这条**容易让复测者误判成"录像没存上"**，实际不是。

`error=4` 来自 CameraX `VideoRecordEvent.Finalize.error`（`CaptureGuideScreen.kt:234-239`），语义是 `ERROR_SOURCE_INACTIVE`——录制源已失效，典型触发场景是 `stop()` 之后相机已解绑、Finalize 回调才到。**文件此时已经写完。**

实证：拉出对应两次录制的文件，用 `mp4probe.py` 解析容器：

| 文件 | 大小 | 时长 | 帧数 | 编码 | `moov` |
|---|---|---|---|---|---|
| `OOTD_head_20261126_212319.mp4` | 30,790,965 B | 18.70 s | 561 | avc1 | 完整 |
| `OOTD_head_20261126_212424.mp4` | 4,135,067 B | 2.55 s | 77 | avc1 | 完整 |

两个文件结构完整、可解码。所以这是**UI 层的误报**，不是保存失败：`recordError` 一旦非空就整句显示，没有区分「文件已落盘但状态码非零」这种可忽略情形。

**给 C 复测的提示**：看到这行警告先别判失败，去 `/sdcard/Movies/OOTD-Capture/` 看有没有文件、跑一遍 `mp4probe.py` 再下结论。

## 复现方法

```bash
# 1. 装包（需与本机 debug keystore 一致，否则签名冲突）
adb install -r android/app/build/outputs/apk/debug/app-debug.apk

# 2. 打开 App 进入「3D 采集引导」，对环境做环绕 + 推近

# 3. 采样内存与帧数
adb shell dumpsys meminfo com.ootd.ai | grep -E "TOTAL|Native Heap"
adb shell dumpsys gfxinfo com.ootd.ai framestats

# 4. 解析录像容器
python mp4probe.py <video.mp4>
```

## 文件清单

| 文件 | 内容 |
|---|---|
| `result-offline-fallback.png` | 断链时的降级结果页 |
| `result-backend-guide.png` | 连上后端时的结果页（三步建议） |
| `result-direct-deepseek.jpg` | 直连 DeepSeek 的结果页（右上角来源标签为判定依据） |
| `monitor-10min-1125.csv` | 10 分钟内存/帧数采样原始数据 |
| `mp4probe.py` | MP4 容器解析脚本（本机无 ffprobe，故自写） |
