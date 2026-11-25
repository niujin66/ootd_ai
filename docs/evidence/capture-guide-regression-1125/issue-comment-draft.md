## B · 11/25–26 任务卡：Xiaomi 17 Pro「3D 采集引导」真机回归

任务卡原文（`docs/first-week.md`，#12 第 2 条）：

> 在 Xiaomi 17 Pro 实测 `main@16d9a50`「3D 采集引导」：预览 `>= 24 FPS`、10 分钟无 OOM、相册录像保存、连后端 `/v1/capture/guide`、直连 DeepSeek 调试与离线降级，修完真机兼容缺陷。

设备：Xiaomi 17 Pro（`25098PN5AC` / `pandora`），Android 17，`fe474c80`
被测包：本地从 `main@de055e1` 构建（`de055e1` 在 `16d9a50` 之后，含队长 11/25 12:01 的背景可靠性修复）
证据目录：[`docs/evidence/capture-guide-regression-1125/`](https://github.com/niujin66/ootd_ai/tree/main/docs/evidence/capture-guide-regression-1125)

### 结果

| 任务卡要求 | 结果 | 实测 |
|---|---|---|
| 预览 ≥ 24 FPS | ✅ | **93–95 FPS**（7 段采样，`dumpsys gfxinfo` 累计帧差分） |
| 10 分钟无 OOM | ✅ | 连续录 **611.59 s / 18,374 帧**，`moov` 完整；PSS 353–374 MB 持平 |
| 相册录像保存 | ✅ | 3 个文件全可解码，avc1 / 30.04 FPS；MediaStore 已登记（`content://…/19530`、`19532`） |
| 连后端 `/v1/capture/guide` | ✅ | `POST /v1/capture/guide 200 OK`，结果页正确渲染三步建议 |
| 离线降级 | ✅ | 断链时自动走 `OfflineCaptureDiagnoser`，文案完整、不崩 |
| 直连 DeepSeek 调试 | ⬜ **未测** | 需真实 API Key，本机未配置 |

补充：室内误报退回（`de055e1` 修的那条）在杂乱卧室背景下 **12/12 扇区、未复现**，且观察到的是 `MANUAL_GUIDE` 路径的 `n/12` 扇区提示，不是旧的 `TOO_CLOSE` 拦截。

### 明确的边界（请 C 验收时注意）

1. **四边留白 `min_margin_px` 不在本证据覆盖范围内。** 按 11/25 12:01 约束，杂乱背景下端侧不再实时拦截留白，只能由 PC 端离线质检给出，判据仍是四边 ≥40 px。需要跑 `analyze_capture.py` 才能验。
2. **后端跑的是 `OOTD_AI_MODE=mock`**，验的是链路通不通，不是 AI 推理质量。
3. **不是「已彻底修复」**，是「单场景单次未复现」。暗光、快速旋转、有人走过镜头、极近距离贴脸都还没测。
4. **包身份已核对**：队长 11/25 私发的 `lead-1125.apk`（SHA `55404df6…`）经 9 个 dex 字节码比对，同样含 `de055e1` 的 `MANUAL_GUIDE`，即状态卡所说的「新包」已经存在，无需再等重打。本地构建与它字节码一致，差异仅在签名与内置资产。

### 复现命令

```bash
# 后端（mock，不设 OOTD_AUTH_TOKEN 即不校验令牌）
cd ootd_ai
OOTD_AI_MODE=mock server/.venv/Scripts/python.exe -m uvicorn app:app --app-dir server --host 127.0.0.1 --port 8000

# 隧道（本机 adb server 会随命令退出而消失，需常驻 + watchdog）
adb reverse tcp:8000 tcp:8000

# 录像容器校验（本机无 ffprobe，用仓库内脚本）
python docs/evidence/capture-guide-regression-1125/mp4probe.py <video.mp4>
```

### 还差什么

- **直连 DeepSeek 调试**：需要一把 API Key 在「开发者调试：AI 指导连接方式」里填一次，录一遍看是否走直连分支而不是后端分支。给我 Key 我就补测，或者你告诉我这一项由 C 那边验。
- PC 端离线质检 `min_margin_px`（不属于真机项，建议并入 C 的整包验收）。
