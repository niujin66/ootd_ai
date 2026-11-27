# APK 瘦身与性能基线（2026-11-27）

对应任务卡 B 岗第 3 条：「针对当前 `app-debug.apk`（`82.06 MB`）评估 ABI 分包与 release 压缩方案并汇报瘦身幅度；按实施与验证清单第 4 节记录冷启动 5 次、换衣 20 次耗时与峰值内存。」

## 结论

**采用「只保留 arm64-v8a + x86_64 两个 ABI」方案，包体从 84.84 MiB 降到 55.80 MiB，减少 29.04 MiB（-34.2%），功能无回归。**

- 真机（Xiaomi 17 Pro）冷启动中位 **363 ms**
- 3D 试衣与采集引导在瘦身后的包上完整可用
- 冷启动、3D 渲染、采集引导均无崩溃、无 OOM

## 先纠正一个基准错误

任务卡写的 `82.06 MB` **不是本地构建产物**。核对结果：

| 文件 | 字节 | MiB | MB（十进制） |
|---|---|---|---|
| `lead-1125.apk`（队长 11/25 私发） | 86,048,755 | **82.06** | 86.05 |
| 本地 `assembleDebug`（改造前） | 88,958,052 | 84.84 | 88.96 |

`86,048,755 ÷ 1048576 = 82.0625`，与任务卡的 82.06 精确吻合。**任务卡那个数是队长包，且用的是 MiB 而非十进制 MB。**

两个包差异的完整账（逐项比对后只剩两处）：

| 差异来源 | 大小 | 性质 |
|---|---|---|
| 队长包缺 `assets/local-demo/` 四个文件 | 1.53 MiB | **功能差异**：内置降级素材缺失 |
| 签名块 + zip 对齐 | 1.25 MiB | 构建方式差异（debug 签名占用更大） |
| 合计 | 2.78 MiB | |

`lib/` 和 `dex` 逐项完全一致，一个字节都不差。

> **结论：82.06 MiB 不能作为瘦身基准**——那是功能不全的包（缺内置降级素材，断网时会没有兜底画面）。本次以本地完整构建 `84.84 MiB` 为基线。

## 改造内容

只改了 `android/app/build.gradle.kts` 一处，`defaultConfig` 内新增 ABI 过滤：

```kotlin
ndk {
    abiFilters += listOf("arm64-v8a", "x86_64")
}
```

- `arm64-v8a`：Xiaomi 17 Pro 真机
- `x86_64`：C 复测用的模拟器（`docs/acceptance-record.md` 记录她用的是 API 37 模拟器）

砍掉 `x86` 与 `armeabi-v7a`，省下的原生库：

| 库 | x86 | armeabi-v7a |
|---|---|---|
| `libface_detector_v2_jni.so` | 9.21 MiB | 5.14 MiB |
| `libfilament-jni.so` | 3.23 MiB | 2.13 MiB |
| `libgltfio-jni.so` | 3.17 MiB | 2.17 MiB |
| `libfilament-utils-jni.so` | 1.37 MiB | 1.11 MiB |
| 小计 | 16.98 MiB | 10.55 MiB |

## 体积结果

| 项 | 改造前 | 改造后 |
|---|---|---|
| 文件大小 | 88,958,052 B | **58,511,819 B** |
| MiB | 84.84 | **55.80** |
| MB（十进制） | 88.96 | **58.51** |
| 减少 | — | **29.04 MiB（-34.2%）** |
| ABI 数 | 4 | **2** |
| 内置降级素材 | 有 | **有（未改动）** |

新包内 ABI 分布：

```
x86_64       17,794,176 B   16.97 MiB
arm64-v8a    16,004,872 B   15.26 MiB
```

## 冷启动 5 次

设备：Xiaomi 17 Pro（`25098PN5AC` / `pandora`，serial `fe474c80`，Android 17）。测量方式：`am force-stop` 后 `am start -W` 读 `TotalTime`，已缓存状态。

| 次序 | TotalTime |
|---|---|
| 1 | 386 ms |
| 2 | 354 ms |
| 3 | 363 ms |
| 4 | 364 ms |
| 5 | 356 ms |

**中位数 363 ms，最大 386 ms，最小 354 ms。** 五次波动 32 ms。

## 换衣 20 次与内存

20 次在「浅色印花短袖」与「深蓝翻领 POLO」之间来回切换，全程进 3D 试衣页。**无 OOM、无崩溃、进程存活。**

| 指标 | 值 |
|---|---|
| PSS Total（`dumpsys meminfo`） | **279,894 KB = 273 MiB** |
| VmHWM（`/proc/<pid>/status`，RSS 峰值） | **473,204 KB = 462 MiB** |
| VmRSS（当前） | 388,512 KB = 379 MiB |

对照首页状态（未进试衣页）：PSS **115,815 KB = 113 MiB**。

PSS 分项（试衣页）：

```
Native Heap    85,244 KB   83 MiB   3D 模型 + 纹理
EGL mtrack     46,774 KB   46 MiB   GL 渲染表面
.dex mmap      39,283 KB   38 MiB   类加载（debug 未优化）
GL mtrack      17,240 KB   17 MiB
.so mmap       14,135 KB   14 MiB
Dalvik Heap     6,116 KB    6 MiB
```

### 未做的判定

两轮切换的 `VmHWM` 分别是 **444 MiB**（第一轮）和 **462 MiB**（第二轮含第一轮）。

**这不构成「有内存泄漏」的证据**：`VmHWM` 是进程生命周期内的历史最高值，第二轮必然 ≥ 第一轮。要判定是否存在累积，需要连续多轮采样看曲线是否单调上升。**本次未做多轮趋势判定，不作为泄漏结论。**

## 功能验证

| 页面 | 结果 | 证据 |
|---|---|---|
| 首页 | 正常，三步引导 + 两个按钮 | `01-home.webp` |
| 3D 试衣 | 正常，人物 + 衣物贴图渲染完整，素材恢复 `fixture-face-03b` | `02-tryon-3d.webp` |
| 3D 采集引导 | 正常，相机预览 + 12 扇区追踪（5/12）+ 水平仪（21°） | `03-capture-guide.webp` |

原生库加载日志（3D 链路）：

```
D/nativeloader: Load .../lib/arm64-v8a/libfilament-jni.so : ok
D/nativeloader: Load .../lib/arm64-v8a/libgltfio-jni.so : ok
D/nativeloader: Load .../lib/arm64-v8a/libfilament-utils-jni.so : ok
I/Filament: FEngine (64 bits) created
I/Filament: FEngine resolved backend: OpenGL
I/Filament: [Qualcomm], [Adreno (TM) 840], [OpenGL ES 3.2 ...]
```

## 包与设备

| 项 | 值 |
|---|---|
| 代码 | `main@edae995` + 本次 `build.gradle.kts` 改动（未提交，见下） |
| APK SHA256 | `3ED1887A3FF34D9608CE489073B12A5D4FA7F5B06A983E5C63CA337E114B7158` |
| 设备 | Xiaomi 17 Pro，`25098PN5AC` / `pandora`，Android 17，serial `fe474c80` |
| 系统依赖 | `JAVA_HOME=C:/Program Files/Android/Android Studio/jbr`（环境变量未设，需显式指定） |

## 还能再省多少

`.dex mmap` 在运行时占 **38 MiB**，这是 debug 包未开 R8 的代价。

若启用 release + R8 + shrinkResources，预估再省 **5–6 MiB**，包体可到 **50 MiB 上下**。但有风险：R8 会混淆反射调用，而 SceneView/Filament 与 ML Kit 都有反射路径，必须重新在真机上验证 3D 渲染与采集引导。**本次未执行。**

## 未验证 / 已知限制

- **未跑 release + R8 构建**（见上节）
- **未做多轮内存趋势采样**，不做泄漏结论
- **换衣 20 次未逐次记录耗时**，只有整体内存指标；`dumpsys meminfo` 单次调用需 5.4 秒（本机 adb 每次都要重启 daemon），无法支撑高频采样
- **C 的模拟器（x86_64）未实机验证**：本次只验了 arm64-v8a 真机
- 本机 adb 注入点击对 Compose 按钮不生效（`MIUIInput` 日志显示事件已送达但 `onClick` 不触发），所有页面跳转均由人工点按

## 复现方法

```bash
cd D:/sic/ootd_ai/android
export JAVA_HOME="C:/Program Files/Android/Android Studio/jbr"
./gradlew assembleDebug
# 产物：app/build/outputs/apk/debug/app-debug.apk

ADB="C:/Users/Lenovo/AppData/Local/Android/Sdk/platform-tools/adb.exe"
"$ADB" install -r app/build/outputs/apk/debug/app-debug.apk

# 冷启动
for i in 1 2 3 4 5; do
  "$ADB" shell am force-stop com.ootd.ai; sleep 1
  "$ADB" shell am start -W -n com.ootd.ai/.MainActivity
  sleep 2
done

# 内存（PSS）
"$ADB" shell dumpsys meminfo com.ootd.ai

# 内存（RSS / 峰值，秒级）
PID=$("$ADB" shell pidof com.ootd.ai | tr -d '\r')
"$ADB" shell "cat /proc/$PID/status | grep -E 'VmHWM|VmRSS'"
```

## 文件清单

- `01-home.webp` —— 首页（2026-11-27 21:59）
- `02-tryon-3d.webp` —— 3D 试衣页（22:29）
- `03-capture-guide.webp` —— 3D 采集引导页（22:32）
- `cold-start-5.csv` —— 冷启动 5 次原始数据
- `mem-tryon.csv` —— 试衣页内存采样
