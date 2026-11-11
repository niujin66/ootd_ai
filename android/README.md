# 衣序 Android 端

「衣序」的 Android 客户端。首页显示应用名，进入试衣页后加载同一人物的三维模型和独立上衣，
手指拖动可绕人物旋转，可在两件测试衣物之间切换。

对应任务：B02（Issue #9）　负责人：B（@Journey668）

## 目录

```
android/
├── app/src/main/java/com/ootd/ai/
│   ├── MainActivity.kt                 首页「衣序」+ 进入试衣入口
│   ├── data/
│   │   ├── AssetManifest.kt            解析 manifest.json（字段缺失报可读错误）
│   │   └── LocalAssetRepository.kt     从 assets/local-demo 读取资产包
│   └── ui/
│       ├── TryOnScreen.kt              三维试衣页：加载、旋转、换衣、三态
│       └── theme/                      主题（模板生成）
├── app/src/main/assets/local-demo/     内部资产包，**不入库**（见下）
├── app/build.gradle.kts
├── gradle/libs.versions.toml           依赖版本目录
└── README.md
```

## 环境

| 项 | 版本 |
| --- | --- |
| Android Studio | AI-262.10968.63.2622.16840690 |
| AGP | 9.5.1 |
| Kotlin | 2.4.21（SceneView 4.67 要求） |
| JVM target | 21 |
| compileSdk / targetSdk | 37 |
| minSdk | 24 |
| SceneView | `io.github.sceneview:sceneview:4.67.0` |
| 渲染后端 | Filament（随 SceneView） |

## 本地资产（不入库）

`fixture-face-01` 是团队内部联调包，含本人三维模型，**走团队内部渠道传递，不进仓库**。
放在：

```
android/app/src/main/assets/local-demo/
├── manifest.json
├── person-001.glb
├── top-001.glb
└── top-002.glb
```

该目录已在根目录 `.gitignore` 中忽略。缺文件时 App 会提示「缺少本地资产包」并给出上面的路径，
不会白屏。

清单关键字段：`catalog_version=fixture-face-01`、`pose_id=pose-001`；两件测试衣物
`source=synthetic-test`，与真实衣物区分。

## 怎么打开和运行

1. 打开 Android Studio，选 **Open**，选中 `android/` 目录（是 `android/`，不是仓库根目录）
2. 等 Gradle 同步完成。Kotlin 2.4 首次会下载编译器，耗时较长属正常
3. 手机端准备：设置 → 关于手机 → 连点「版本号」7 次开启开发者模式 → 开发者选项 →
   打开 **USB 调试** 和 **USB 安装**
4. 数据线连接电脑，手机弹出「是否允许 USB 调试」时点允许
5. 顶部设备下拉框选到该手机，点绿色三角 **Run**

命令行构建（等价）：

```bash
cd android
./gradlew :app:assembleDebug
```

## 当前实际结果

### 三维加载与旋转（B02 前半，2026-11-10）

- 设备：Xiaomi 25098PN5AC，Android 17（API 37），USB 真机
- 构建：`BUILD SUCCESSFUL`，APK 44 MB（含 Filament 原生库）
- 资产：APK 内确认含 `assets/local-demo/` 四个文件
- 结果：**通过**。点「进入试衣」后人物正常显示，躯干着 `top-001`，
  手指拖动可绕人物旋转，无崩溃
- 颜色：`person-001.glb` 与两件衣物为顶点色（`COLOR_0`）导出，无贴图材质，
  真机显示正常，深浅两件可区分

### 缺资产提示（2026-11-10 实测）

把 `assets/local-demo/` 移除后重新构建安装，进入试衣页显示：

```
没有找到三维资产
缺少本地资产包：assets/local-demo/manifest.json
放置步骤见 android/README.md「本地资产」一节
[重试] [返回]
```

不白屏、不崩溃，按提示放回资产即可恢复。

### 换衣切换（B02 后半）

- 真机连续切换 `top-001 → top-002`，衣物正常替换，人物与镜头保持：**通过**
- 未测：切到第三件时点不动的情况（只有两件）；切换失败时的回滚提示未构造场景验证

### 已知问题

- 首次安装曾报 `INSTALL_FAILED_USER_RESTRICTED`：开发者选项里的「USB 安装」未开启。
  复现时先查这个开关
- `adb shell input swipe` 无法在小米设备上注入触摸事件（`INJECT_EVENTS` 权限限制），
  手势验证必须真人操作
- 未测项：模拟器未验证；深色模式、横屏未验证；换衣失败回滚未构造场景

## B03：目录下发与推荐接入

### 用到的接口

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/v1/health` | 联调前探活 |
| GET | `/v1/catalog?person_id=person-001` | 取人物与衣物清单（含 `size_bytes`、`sha256`） |
| GET | `/v1/assets/{person_id}/{catalog_version}/{asset_id}` | 下载单个资产 |
| POST | `/v1/recommendations` | 按场合与偏好取推荐结果 |

请求头 `Authorization: Bearer <token>`；`401 / 404 / 409 / 422` 均有对应提示。

### 配置（不进仓库）

在 `android/local.properties` 里加两项，构建时读入 `BuildConfig`：

```properties
ootd.apiBaseUrl=http://127.0.0.1:8000
ootd.apiToken=你的令牌
```

`local.properties` 已被忽略，令牌不要写进代码或提交。

### 真机联调：用 adb reverse，不依赖局域网

```bash
adb reverse tcp:8000 tcp:8000      # 手机的 127.0.0.1:8000 转到电脑
adb reverse --list                 # 确认映射还在（拔插后可能需要重设）
```

这样手机不用连同一个 WiFi，也不用开防火墙。**注意**：debug 构建允许明文 HTTP（`app/src/debug/AndroidManifest.xml`），release 仍强制 HTTPS。

### 校验与切换策略

1. 下载到临时目录，逐个核对 `size_bytes` 与 `sha256`
2. **整版校验通过才切换**；任何一件失败都保留上一套资源和当前画面
3. 下载到的包放在 `cacheDir/ootd/<person_id>/<catalog_version>/`，下次启动可直接用

### 实测记录（Xiaomi 25098PN5AC / Android 17）

| 场景 | 结果 |
|---|---|
| 无令牌点「拉取目录」 | 提示「未授权：缺少或错误的访问令牌（已保留当前资源）」，画面不崩、资源不换 —— **通过** |
| 带令牌拉取目录 | 顶部变为 `服务端 · fixture-face-01 · pose-001`，人物与两件衣物正常渲染 —— **通过** |
| 点「推荐」（上班 / 优先浅色） | 显示【模拟推荐】与理由，衣物切到 `top-001` —— **通过** |
| 先切 `top-002` 再点「推荐」 | 自动切回 `top-001`，说明 `garment_id` 真的驱动了换衣 —— **通过** |

### 已知问题

- SceneView 的 `rememberModelLoadState` 只收字符串，内部按 APK 资产去查；磁盘绝对路径必须用
  `createModelInstance(File)` 直接读，且**必须留在主线程**（Filament 引擎有线程亲和性，
  放后台线程会 native 崩溃）
- 网络调用必须离开主线程，否则 `NetworkOnMainThreadException`（该异常没有 message，
  界面上只会剩一句笼统的失败提示）
- 未测项：`409` 版本冲突、`422` 参数校验、断网中途下载、并发重复拉取

### 说明：这次联调用的是本地等价桩服务

队伍的真实服务（C 的 T03）用 venv + FastAPI 启动，本机装依赖时 PyPI 下载反复卡住，
因此按 `server/app.py` 的契约写了一个**只用标准库的等价桩服务**做端到端联调，
字段与状态码与真实服务一致。**这不代表真实服务已验证**，等 C 的服务可跑通后需要复测一遍。

## 后续

- 等 C 的真实 mock 服务可跑通后复测：`401 / 404 / 409 / 422` 全部分支
- B04：接入真实衣物资产；推荐结果缓存与「换一件」能力

## 说明

- 仓库是公开的：个人信息、影像素材、密钥不要提交
- `local.properties` 与 `assets/local-demo/` 均已被忽略