# 衣序 Android 端

「衣序」的 Android 客户端。当前只包含首页骨架，用于验证工程可构建、可安装、可打开。

对应任务：B01（Issue #3）　负责人：B（@Journey668）

## 目录

```
android/                                             Android 工程根目录
├── app/src/main/java/com/ootd/ai/MainActivity.kt    首页，居中显示「衣序」
├── app/src/main/java/com/ootd/ai/ui/theme/           主题（模板生成）
├── app/build.gradle.kts
├── gradle/libs.versions.toml                        依赖版本目录
├── settings.gradle.kts
└── README.md
```

## 环境

| 项 | 版本 |
| --- | --- |
| Android Studio | AI-262.10968.63.2622.16840690 |
| AGP | 9.5.1 |
| Kotlin | 2.2.10 |
| compileSdk / targetSdk | 37 |
| minSdk | 24 |

## 怎么打开和运行

1. 打开 Android Studio，选 **Open**，选中 `android/` 目录（是 `android/`，不是仓库根目录）
2. 等 Gradle 同步完成。第一次会下载依赖，耗时较长属正常
3. 手机端准备：设置 → 关于手机 → 连点「版本号」7 次开启开发者模式 → 开发者选项 → 打开 **USB 调试**
4. 数据线连接电脑，手机弹出「是否允许 USB 调试」时点允许
5. Android Studio 顶部设备下拉框选到该手机，点绿色三角 **Run**

## 当前实际结果

- 日期：2026-11-09
- 设备：Xiaomi 25098PN5AC，Android 17（API 37），USB 真机
- 结果：**通过**。构建成功，安装成功，首页打开后屏幕正中显示「衣序」，无崩溃
- 首次安装曾报 `INSTALL_FAILED_USER_RESTRICTED`，原因为开发者选项中的「USB 安装」未开启；开启后安装成功。复现时若遇到同样报错，先检查该开关
- 未测项：模拟器未验证（本机只用真机）；深色模式、横屏未验证

## 后续

- B02：接入 SceneView 加载 GLB，手指拖动查看正、侧、背面，在 `top-001` / `top-002` 两个测试资产间切换
- B03：替换为队长交付的人物模型与 A 交付的两件衣物
- 第 2 周：接入 `/v1/recommendations` 推荐结果

## 说明

- 仓库是公开的：个人信息、影像素材、密钥不要提交
- `local.properties` 含本机 SDK 路径，已在根目录 `.gitignore` 中忽略
- 构建产物（`build/`、`.gradle/`）由各层 `.gitignore` 忽略