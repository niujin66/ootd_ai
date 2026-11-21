# -*- coding: utf-8 -*-
"""11/23 候选输入：模型侧尺寸实测（几何回归值，不是平铺实测）。

口径声明
--------
模型侧是「穿着状态的 3D 几何跨度」，实物侧 measurements.csv 是「平铺后的
直线距离」。两者端点与状态不同，**不可直接相减算误差**，只做成对登记。
本脚本只输出可复现的几何量，不判定尺寸合格、不产生新资产、不改 GLB。

用法：
    python T02-尺寸量法实测-1121.py [包目录] [--csv 实物尺寸.csv] [--json 输出.json]
依赖：numpy / trimesh
"""
import argparse
import csv
import json
import os
import sys

import numpy as np
import trimesh

DEFAULT_PKG = os.path.join(os.path.dirname(__file__), "T02-candidate-20261117")
DEFAULT_CSV = os.path.join(os.path.dirname(__file__), "..", "garments", "measurements.csv")
ASSETS = ("top-001_v2.glb", "top-002_v1.glb")
Y_CHEST = 1.30


def loop_at(mesh, y):
    sec = mesh.section(plane_origin=[0, y, 0], plane_normal=[0, 1, 0])
    if sec is None:
        return None
    loops = [np.asarray(p, dtype=float) for p in sec.discrete if len(p) >= 3]
    if not loops:
        return None
    return max(loops, key=lambda p: np.linalg.norm(np.diff(np.vstack([p, p[:1]]), axis=0), axis=1).sum())


def dims(path):
    mesh = trimesh.load(path, force="scene").to_geometry()
    V = np.asarray(mesh.vertices, dtype=float)
    ymin, ymax = float(V[:, 1].min()), float(V[:, 1].max())
    h = ymax - ymin

    top = V[V[:, 1] >= ymax - 0.05 * h]
    bottom = V[V[:, 1] <= ymin + 0.05 * h]
    neck = V[(V[:, 1] >= 1.48) & (V[:, 1] <= 1.58)]

    ring = loop_at(mesh, Y_CHEST)
    ring_half = float(np.linalg.norm(np.diff(np.vstack([ring, ring[:1]]), axis=0), axis=1).sum()) / 2 * 100 if ring is not None else None
    ring_proj = float(ring[:, 0].max() - ring[:, 0].min()) * 100 if ring is not None else None
    outer_x = float(np.abs(V[:, 0]).max()) * 100

    # 肩宽 / 领口宽不做自动数值：当前 GLB 无端点标记，且领口高度(y≈1.48–1.58)
    # 的截面碎成多个小环，「取全区 X 跨度」会把袖子与肩部混入（top-001 50.5 cm
    # vs top-002 22.0 cm 即此误差）。标为需人工标点，见配套 md 第 5 节。
    return {
        "y_min_m": round(ymin, 4), "y_max_m": round(ymax, 4),
        "length_y_cm": round(h * 100, 2),
        "hem_span_x_cm": round(float(bottom[:, 0].max() - bottom[:, 0].min()) * 100, 2),
        "chest_loop_half_cm": round(ring_half, 2) if ring_half else None,
        "chest_proj_x_cm": round(ring_proj, 2) if ring_proj else None,
        "sleeve_outer_x_cm": round(outer_x, 2),
        "sleeve_lateral_cm": round(outer_x - ring_proj / 2, 2) if ring_proj else None,
        "shoulder_span_x_cm": None,
        "neck_span_x_cm": None,
        "manual_marker_required": ["shoulder_span_x_cm", "neck_span_x_cm"],
        "vertices": int(len(V)),
    }


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("package_dir", nargs="?", default=DEFAULT_PKG)
    ap.add_argument("--csv", default=DEFAULT_CSV)
    ap.add_argument("--json", default=None)
    a = ap.parse_args(argv)
    pkg = os.path.abspath(a.package_dir)

    real = {}
    csv_path = os.path.abspath(a.csv)
    if os.path.exists(csv_path):
        with open(csv_path, encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                real[row["garment_id"]] = {k: v for k, v in row.items() if k != "garment_id"}

    out = {"package": pkg, "csv": csv_path if os.path.exists(csv_path) else None, "garments": {}}
    for name in ASSETS:
        gid = name.split("_")[0]
        d = dims(os.path.join(pkg, name))
        d["real_flat"] = real.get(gid)
        out["garments"][gid] = d
        print(f"== {gid}  {name}")
        for k in ("length_y_cm", "hem_span_x_cm", "chest_loop_half_cm",
                  "chest_proj_x_cm", "sleeve_outer_x_cm", "sleeve_lateral_cm"):
            print(f"   {k:24} {d[k]}")
        print(f"   shoulder_span_x_cm / neck_span_x_cm: 需人工标点（见 md 第 5 节）")
        print(f"   实物平铺 CSV: {d['real_flat']}")
    text = json.dumps(out, ensure_ascii=False, indent=2)
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            f.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
