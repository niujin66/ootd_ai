# -*- coding: utf-8 -*-
"""静态几何预检：衣物网格与人台的穿模/间隙。不是真机结论。

用法：
    python T02-穿模静态预检-1119.py [包目录] [--json 输出.json]

口径：
- 人台用射线奇偶判定内外（人台网格非 watertight，signed_distance 的符号不可靠，弃用）
- 间隙 = 衣物顶点到人台表面的最近距离（trimesh.closest_point）
- 人台内部顶点 = 穿模，其最近距离近似为穿入深度
- 分区按高度：肩/领 >1.42，腋下/胸 1.20-1.42，躯干 0.95-1.20，下摆 <0.95

依赖：numpy / trimesh / rtree / scipy
"""
import argparse
import json
import os
import sys

import numpy as np
import trimesh
from trimesh.proximity import closest_point
from trimesh.ray.ray_triangle import RayMeshIntersector

DEFAULT_PKG = os.path.join(os.path.dirname(__file__), "T02-candidate-20261117")
ASSETS = ("top-001_v2.glb", "top-002_v1.glb")
REGIONS = (
    ("肩/领 (y>1.42)", lambda p: p[:, 1] > 1.42),
    ("腋下/胸 (1.20-1.42)", lambda p: (p[:, 1] >= 1.20) & (p[:, 1] <= 1.42)),
    ("躯干 (0.95-1.20)", lambda p: (p[:, 1] >= 0.95) & (p[:, 1] < 1.20)),
    ("下摆 (y<0.95)", lambda p: p[:, 1] < 0.95),
)


def probe(pkg, name, person, ray):
    garment = trimesh.load(os.path.join(pkg, name), force="scene").to_geometry()
    V = np.asarray(garment.vertices)
    inside = ray.contains_points(V)
    _cp, dist, _tid = closest_point(person, V)
    result = {"file": name, "vertices": int(len(V)), "inside_total": int(inside.sum()), "regions": {}, "inside_points": {}}
    for label, fn in REGIONS:
        mask = fn(V)
        if not mask.any():
            continue
        ins, d = inside[mask], dist[mask]
        clearance, pen = d[~ins], d[ins]
        result["regions"][label] = {
            "n": int(mask.sum()),
            "inside": int(ins.sum()),
            "penetration_max_mm": round(float(pen.max() * 1000), 2) if len(pen) else 0.0,
            "clearance_min_mm": round(float(clearance.min() * 1000), 2) if len(clearance) else None,
            "clearance_median_mm": round(float(np.median(clearance) * 1000), 2) if len(clearance) else None,
            "critical_lt2mm": int((clearance < 0.002).sum()) if len(clearance) else 0,
        }
    P = V[inside]
    if len(P):
        result["inside_points"] = {
            "x_range_m": [round(float(P[:, 0].min()), 3), round(float(P[:, 0].max()), 3)],
            "y_range_m": [round(float(P[:, 1].min()), 3), round(float(P[:, 1].max()), 3)],
            "z_range_m": [round(float(P[:, 2].min()), 3), round(float(P[:, 2].max()), 3)],
            "within_neck_center_absx_lt_0p25": int((np.abs(P[:, 0]) < 0.25).sum()),
        }
    return result


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("package_dir", nargs="?", default=DEFAULT_PKG)
    ap.add_argument("--json", default=None)
    a = ap.parse_args(argv)
    pkg = os.path.abspath(a.package_dir)
    person = trimesh.load(os.path.join(pkg, "person-001.glb"), force="scene").to_geometry()
    ray = RayMeshIntersector(person)
    results = [probe(pkg, n, person, ray) for n in ASSETS]
    text = json.dumps(results, ensure_ascii=False, indent=2)
    print(text)
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            f.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
