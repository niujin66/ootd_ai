# T02 两件实衣候选包 · v3 兼容性修复（2026-11-17）

`catalog_version = fixture-face-03b`｜`person_id = person-001`｜`pose_id = pose-001`

基于 `T02-candidate-20261113`。**只剥离两个 SceneView 4.67 不支持的材质扩展，几何、UV、贴图、包围盒均未改动。**

`catalog_version` 从 `fixture-face-03` 升到 `fixture-face-03b`：文件字节变了（哈希变了），沿用旧版本号会让 C 那边的 `content_digest` 静默对不上。

---

## 一、资产与哈希（完整 SHA-256）

| 资产 | 文件 | 体积 | SHA-256 | 顶点 / 三角面 |
|---|---|---|---|---|
| person-001 | `person-001.glb` | 419,572 B | `f3dbe4c875dafb73d161d28de311b33dd839e8ec2fb559624c129a3a66012354` | 10,475 / 20,908 |
| top-001（浅色印花短袖） | `top-001_v2.glb` | 981,408 B | `45301ea0feead027300fe1d9b6847dc7a93381638a0d4996496d6d9d745d15b5` | 6,369 / 10,692 |
| top-002（深蓝翻领 POLO） | `top-002_v1.glb` | 671,540 B | `8502d846b4beb443b6acfbd3c91d5ec97369a374dc19352a77657aa3758f7e82` | 6,503 / 12,616 |
| manifest | `manifest.json` | 951 B | `5ee4730134929f159a650db812dd94272965abce9bc4ca33a393dac81113befd` | — |
| 校验报告 | `validation-report.json` | 3,356 B | `acf41c87f7b042d5c4d2940db225adbcfd4f204a37fac5503c20eaf84383a508` | — |

person-001 与 11-13 版**完全没动**（同哈希同体积）。两件衣物的顶点/三角面数与 C 在 11/15 登记的完全一致 —— 剥离扩展不动几何。

## 二、改了什么

| 文件 | 原扩展 | 涉及材质 | 二进制块（BIN） |
|---|---|---|---|
| `top-001_v2.glb` | `KHR_materials_specular` | top001_fabric / front_print / back_print | 974,020 B，sha `e4e157cb799b1579`，**改动前后一致** |
| `top-002_v1.glb` | `KHR_materials_specular` + `KHR_materials_sheen` | polo_button / polo_blue_trim / polo_blue | 665,168 B，sha `044d8ed636ece660`，**改动前后一致** |

- JSON 块分别从 7,640 → 7,360 B、6,900 → 6,344 B，就是去掉扩展声明的体积。
- `pbrMetallicRoughness` 逐字段比对**完全相同**（baseColor / metallic / roughness / 贴图引用）。
- 两个文件都**没有** `extensionsRequired`，扩展本来是可选项。
- 剥离后 `extensionsUsed` 为空。

被丢掉的数值（轻微高光调节，回到 PBR 默认）：

| 材质 | specularFactor | sheenColorFactor | sheenRoughnessFactor |
|---|---|---|---|
| top001_fabric / front_print / back_print | 0.44 | — | — |
| polo_button / polo_blue_trim / polo_blue | 0.5 | [1,1,1] | 0.45 |

## 三、怎么复现（C 可用来自证）

```bash
# 1) 从已验收的 11-13 原包重跑剥离，输出应与本包逐字节一致
python tools/strip_khr_extensions.py <T02-candidate-20261113 目录> <输出目录>
# 期望：top-001_v2.glb sha 45301ea0…、top-002_v1.glb sha 8502d846…

# 2) 扩展自检（有禁用扩展则退出码 1）
python tools/inspect_extensions.py .

# 3) 仓库后端校验
python server/validate_asset_package.py <本包目录>
# 期望 exit 0，三资产 OK
```

本次交付前已在本机跑过 1) 和 3)：1) 输出与包内文件逐字节一致，3) 通过（exit 0）。

## 四、目录内容

```
manifest.json               清单（catalog_version = fixture-face-03b）
person-001.glb              人台
top-001_v2.glb              浅色印花短袖（扩展已剥离）
top-002_v1.glb              深蓝翻领 POLO（扩展已剥离）
validation-report.json      后端校验报告
renders/                    联排预览图（人台六视图 / 白模六视图 / 纯衣四视图）
tools/inspect_extensions.py 扩展自检
tools/strip_khr_extensions.py  剥离工具（带命令行参数，可复现）
tools/measure_chest.py      胸宽量法实测（截面半围 / 投影宽 / 围长）
README.md                   本文件
```

## 五、还没解决的（不在本包范围）

- 胸宽口径（半围 54.3 vs 平铺 53.0）等队长拍板，见 #11。
- 印花照片级投影、背印压侧缝、袖窿拉伸、领圈滚边偏硬 —— 历史交底项，本次未动。
- 本包只解决"App 端加载闪退"，**不代表**外观或真机 fit 通过。
