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

## 后续

- B03：接 `/v1/catalog` 拉目录、按 `asset_url` 下载并校验 `size_bytes`/`sha256`；
  调 `/v1/recommendations` 把 `garment_id` 接到换衣入口，显示 `reason` 与 `mode`

## 说明

- 仓库是公开的：个人信息、影像素材、密钥不要提交
- `local.properties` 与 `assets/local-demo/` 均已被忽略