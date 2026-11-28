# `fixture-face-04` 交付证据（A 侧，11/27 出件 / 11/28 复核）

> 一句话：三衣全身包 `fixture-face-04` 已按 `0fa25e7` 的合龙方案产出，`export_glb_guard.py check` **4/4 clean、0 blocked、exit 0**；`content_digest` 与 11/27 在 [`#13`](https://github.com/niujin66/ootd_ai/issues/13) 登记的 `92036d7ac113…` 一致。本页只放**指标与哈希**，`.glb`、`.blend`、渲染图与原始影像走内部渠道，**不进仓库、不进 Issue 附件**。

## 1｜包身份（逐件复算，11/28）

| 文件 | 体积 | SHA-256 | 顶点 / 三角面 | `extensionsUsed` |
|---|---|---|---|---|
| `person-001_v2.glb` | 4,230,124 B | `4881ded182c1992de23a0ed227a938ba016a735316c325de9ce2643c02784d37` | 59,518 / 52,357 | 空 |
| `top-001_v2.glb` | 981,408 B | `45301ea0feead027300fe1d9b6847dc7a93381638a0d4996496d6d9d745d15b5` | 6,369 / 10,692 | 空 |
| `top-002_v1.glb` | 671,540 B | `8502d846b4beb443b6acfbd3c91d5ec97369a374dc19352a77657aa3758f7e82` | 6,503 / 12,616 | 空 |
| `top-003.glb` | 1,132,692 B | `292dae5a5e4c923c614155f10dd942b5ba5ffbc7d450ab65b1280e9de3a1e500` | 7,735 / 13,132 | 空 |

- 四件合计 **6.691 MB**（`person-001_v2.glb` 在 5 MB 上限内）。
- `catalog_version = fixture-face-04`，`person_id = person-001`，`pose_id = pose-001`，`manifest.json` 为 UTF-8 无 BOM、可复解析。
- `content_digest`：**`92036d7ac113f7e9ed118a34f1ecb36318c542b16495ecdef611168b0d3c5095`**
  - 复算方式与 `server/importer.py` 的 `_content_digest` 逐字节一致：`sha256(json.dumps(manifest, sort_keys=True, ensure_ascii=False)` + 按 `relative_file` 排序的 `(relative_file, sha256)` 串接`)`。
  - 四件的 SHA-256 与体积**逐件复算命中** `validation-report.json`，无一例外。

## 2｜交付门禁

```text
export_glb_guard.py check <包目录>
→ PASS ×4，4/4 clean; 0 blocked，exit 0
```

- 四件 `extensionsUsed` 均为空 → 不含 `KHR_materials_specular` / `KHR_materials_sheen`，Filament 不会因材质扩展闪退。
- `03b` 三件的哈希与体积**未变**（`top-001_v2` / `top-002_v1` 与 03b 登记值逐位相同），只新增 `person-001_v2.glb` 与 `top-003.glb`。
- **未跑** `server/validate_asset_package.py`：本机缺 `trimesh`（与 11/26 诊断记录同一环境限制）。正式校验请 C 用 `--expected-garments 3` 执行，不要用默认的 2 件口径。

## 3｜四视角基准图（16 张，内部渠道）

`renders/` 下按 `<资产>_<视角>.png` 命名，1024×1024，相机/灯光参数与 03b 六视图同源（`_work/render_4views_fixture04.py`）：

- 人物：`person-001_v2_{front,side,back,threequarter}.png`
- 上衣：`top-001_*`、`top-002_*`、`top-003_*` 各 4 视角

> 用途是给 C 做四视角核对与真机对照，**不替代真机结论**；图片不进仓库。

## 4｜静态贴合预检（几何，不是真机）

方法：Blender BVHTree 最近面 + 面法线判内外，分区按高度。**先用 03b 校准**：同法复现 11/19 记录的「最小间隙 7.12 mm、领圈 `top-001 ≤3.70 mm` / `top-002 ≤2.30 mm`」，说明量尺没漂。

| 分区 | `top-003` 内侧顶点 | 最大穿入 | 最小间隙 | 间隙中位 |
|---|---|---|---|---|
| 肩/领（`z>1.42`） | 64 | 28.58 mm | 3.98 mm | 17.08 mm |
| 腋下/胸（`1.20–1.42`） | **72** | **3.08 mm** | **0.08 mm** | 14.86 mm |
| 躯干（`0.95–1.20`） | 0 | 0 | 12.05 mm | 31.05 mm |
| 下摆（`z<0.95`） | 0 | 0 | 7.64 mm | 20.16 mm |

- **肩/领区不可比**：`person-001_v2` 换成 V9 头部后，领口附近最近面来自头部体积（同法对未改动的 `top-001_v2` 也从 24 个内侧顶点变成 53 个），这一行不能当穿模结论。
- **可比区间是腋下/胸及以下**：新袖筒在腋下贴住手臂（最小间隙 `0.08 mm`、最大穿入 `3.08 mm`），是模板兜底改形的已知待验点，请 C 在四视角里重点看；若真机可见，按「改几何 → 新版本新哈希 → 重验收」处理。

## 5｜请 C 执行的导入步骤

```bash
# 1) 隔离导入（务必显式 3 件，默认值是 2）
python server/import_package.py <包目录> --expected-garments 3
# 或先只校验不出库
python server/validate_asset_package.py <包目录> --expected-garments 3
# 2) 核对 content_digest 是否等于 92036d7ac113…（见 §1）
# 3) 真机看颈部接缝（躯干 `z=1.5181` 平切，切口 1 个封口面、边界边 0、非流形 0）
# 4) 三件上衣逐件四视角，重点 `top-003` 袖口封边与腋下（§4）
```

## 6｜边界与未做项

- 资产不入库：`.glb` / `.blend` / 渲染图 / 原始影像走内部渠道；本页只放哈希、体积、计数与几何指标。
- `top-003` 是**模板兜底**（模板 `top-001_v2` 改形，长袖为防撞衫的人为设计，实物是短袖），不写「扫描重建」；口径与理由见 [`modeling-comparison-top003.md`](../../modeling-comparison-top003.md) §2.3–§2.4。
- 未做：`validate_asset_package.py`（缺 `trimesh`）、COLMAP `sparse/0` 轨迹核验（本机无 `colmap`/`pycolmap`）、真机四视角结论（C 侧）。