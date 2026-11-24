# `fixture-face-04` 交接单（3 衣候选包）：模板与门禁预演

> 用途：**11/28 晚** 把 `top-003` 并入 `03b` 组成 3 衣候选包交 `@uniquebugle-stack` 隔离导入。
> 本文件 = 交接模板 + 交付门禁的预演结果 + 11/26 止损闸门判定表。**预演已在 11/24 实跑，不是纸面流程。**

## 1｜包组成

| 项 | 内容 | 状态 |
|---|---|---|
| 人物 | `person-001.glb`（`f3dbe4c8…`，419,572 B） | 沿用 `03b`，不动 |
| 衣物 1 | `top-001_v2.glb`（`45301ea0…`，981,408 B） | 沿用 `03b`，不动 |
| 衣物 2 | `top-002_v1.glb`（`8502d846…`，671,540 B） | 沿用 `03b`，不动 |
| 衣物 3 | `top-003.glb` | **待产出**（11/27–28 精修） |
| 清单 | `manifest.json`（`catalog_version = fixture-face-04`） | 待写 |
| 校验报告 | `validation-report.json`（三件 + 新件的哈希与体积） | 待生成 |
| 四视角基准图 | 新件正 / 侧 / 背 / 斜侧各 1 张 | 待生成（走内部渠道） |
| 源工程索引 | `.blend` 路径与导出设置说明（不随包上传） | 待登记 |

**目录约定**：新建 `local-data/handoff/T02-candidate-20261128/`，**不覆盖** `T02-candidate-20261117/`。`03b` 的 `content_digest = f0c05a0d…` 必须保持不变。

## 2｜`manifest.json` 增补条目（模板）

在 `03b` 的 `assets` 数组末尾追加一条，其余字段与既有三件保持一致：

```json
{
  "asset_type": "garment",
  "asset_id": "top-003",
  "relative_file": "top-003.glb",
  "source": "real-garment-reconstruction",
  "display_name": "（中文名，UTF-8 写入）",
  "available": true,
  "tags": ["（颜色）", "（版型）", "casual"]
}
```

> ⚠️ 已知坑：`03b` 的 `manifest.json` 里 `display_name` 字段在本机读取时呈现乱码（`娴呰壊鍗拌姳鐭`）。新包写入时确认文件是 **UTF-8 无 BOM**，写完用 `python -c "import json;json.load(open('manifest.json',encoding='utf-8'))"` 复核，并把该问题在交接里说明。

## 3｜交付门禁：流程与 11/24 预演结果

### 3.1 流程（导出后按序执行，任一不过就停下）

```text
export -> check（必须 exit 0）-> 若 FAIL：strip 到新目录 -> 再 check -> verify 与基准逐字节比对
```

```bash
python server/tools/export_glb_guard.py check  <目录或 glb>
python server/tools/export_glb_guard.py strip  <源目录> <干净输出目录> top-003.glb
python server/tools/export_glb_guard.py verify <剥离后 glb> <基准 glb>
```

### 3.2 预演实测（11/24，在副本上跑，`03b` 原目录零改动）

| 步骤 | 输入 | 结果 |
|---|---|---|
| 1 | `03b` 三件干净副本 | `check` **exit 0**，`3/3 clean; 0 blocked` |
| 2 | 同三件 | `strip` 输出 `SKIP (already clean)`，产物与原文件**逐字节一致** |
| 3 | `T02-candidate-20261113` 的带扩展真实件 | `check` **exit 1**，`top-001_v2.glb` 命中 4 处（`extensionsUsed` + 3 个材质），`top-002_v1.glb` 命中 1 处 |
| 4 | 同两件 `strip` | **exit 0**，981,688 → **981,408 B / `45301ea0…`**，672,096 → **671,540 B / `8502d846…`** |
| 5 | 剥离产物 vs `03b` | **逐字节一致**（两个文件哈希完全相同），`re-check` exit 0 |

结论：**门禁能拦、能剥、剥完回到基准**。`top-003` 导出后照此执行即可，不需要现场试错。

另记一笔边界：人工构造的畸形件（只在 `extensionsUsed` 里登记、材质上没有实际 extension）`check` 会拦（exit 1），但 `strip` 不会改动它。Blender 正常导出不会产生这种形态，仅作记录。

## 4｜11/26 晚止损闸门（判定表）

拿到 2DGS 初模当晚填这张表，**当晚定路线，不拖到 11/27**。

| 观察项 | 记录 | 阈值 / 判断 |
|---|---|---|
| 初模是否闭合 | 破洞数 / 位置 | 仅袖口·领口·下摆局部小洞 → A 路线 |
| 双面粘连范围 | 面积占比 / 部位 | 大面积或躯干粘连 → B 路线 |
| 遮挡缺失 | 缺失部位 | 背部或侧面成片缺失 → B 路线 |
| 修闭预估工时 | 小时 | **> 2 天修不完 → B 路线** |
| 判定 | A / B | A = 扫描直修；B = 扫描网格作比例与贴图参照 + 传统 Blender 模板兜底 |
| 切换原因 | 一句话 | 写进 11/29 对比表与《技术研究报告》 |

> B 路线不是失败路径：它照样 11/28 交 `fixture-face-04`，而且"为什么切、切了之后省/费多少"本身就是比赛要的工程结论。

## 5｜11/28 交付前自检

- [ ] `top-003.glb` 过 `check`（exit 0），无 `KHR_materials_specular` / `KHR_materials_sheen`
- [ ] 四件 GLB 的 SHA-256 与体积已写入 `validation-report.json`
- [ ] `manifest.json` 的 `catalog_version = fixture-face-04`、`top-003` 条目齐全、UTF-8 可解析
- [ ] `03b` 三件哈希与 `content_digest` 未变
- [ ] 四视角基准图已生成（借内部渠道给 C，不进仓库）
- [ ] 导入步骤与失败回滚说明已写清（C 用 `import_candidate.py` 隔离导入）

## 6｜边界与红线

- 不覆盖 `03b`、不改既有三件的几何；新包另起目录。
- `.glb` / `.blend` / 原始影像走内部渠道，**不进公开仓库或 Issue 附件**；Issue 只贴 SHA-256、面数、体积与缺陷说明。
- 门禁是**交付前**的资产侧要求，不能拿客户端 `GlbSanitizer` 兜底来代替。
