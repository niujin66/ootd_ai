# -*- coding: utf-8 -*-
"""A/B/C 三方案几何仿真（只算不写回交付件，不产生新资产）。

按 A04 决策单，三方案的 C_half 目标为 53.0 / 54.35 / 57.4 cm。
本脚本对 03b 衣物网格做 **X/Z 水平等比缩放**（Y 不变，衣长不动），
迭代求解缩放系数使 y=1.30 处最大闭合截面环半围命中目标，
然后对每个仿真网格实测穿模与间隙。人台不缩放。

用途：给队长拍板提供数据，不代替真机结论，不改几何、不发新版本。
依赖：numpy / trimesh / rtree / scipy

用法：
    python T02-三方案几何仿真-1121.py [包目录] [--json 输出.json]
"""
import argparse
import json
import os

import numpy as np
import trimesh
from trimesh.proximity import closest_point
from trimesh.ray.ray_triangle import RayMeshIntersector

DEFAULT_PKG = os.path.join(os.path.dirname(__file__), "T02-candidate-20261117")
ASSETS = ("top-001_v2.glb", "top-002_v1.glb")
Y_CHEST = 1.30
TARGETS = (("A 净样", 53.0), ("B 成衣外轮廓（现状）", 54.35), ("C 净样+松量", 57.4))
REGIONS = (
    ("肩/领 y>1.42", lambda p: p[:, 1] > 1.42),
    ("腋下/胸 1.20-1.42", lambda p: (p[:, 1] >= 1.20) & (p[:, 1] <= 1.42)),
    ("躯干 0.95-1.20", lambda p: (p[:, 1] >= 0.95) & (p[:, 1] < 1.20)),
    ("下摆 y<0.95", lambda p: p[:, 1] < 0.95),
)


def half_girth(mesh, y=Y_CHEST):
    """返回 (截面半围 cm, 最大环 X 投影宽 cm)。"""
    sec = mesh.section(plane_origin=[0, y, 0], plane_normal=[0, 1, 0])
    if sec is None:
        return None
    loops = [np.asarray(p, dtype=float) for p in sec.discrete if len(p) >= 3]
    if not loops:
        return None
    per = max(float(np.linalg.norm(np.diff(np.vstack([p, p[:1]]), axis=0), axis=1).sum()) for p in loops)
    big = max(loops, key=lambda p: np.linalg.norm(np.diff(np.vstack([p, p[:1]]), axis=0), axis=1).sum())
    return per / 2 * 100, float(big[:, 0].max() - big[:, 0].min()) * 100


def scale_xz(base, s):
    """只缩放水平面，绕人体中轴 (x=0,z=0)，Y 不变。"""
    V = np.asarray(base.vertices, dtype=float).copy()
    V[:, 0] *= s
    V[:, 2] *= s
    return trimesh.Trimesh(vertices=V, faces=np.asarray(base.faces).copy(), process=False)


def solve_s(base, target):
    s = 1.0
    for _ in range(6):
        g = half_girth(scale_xz(base, s))
        if not g or g[0] <= 0:
            break
        s *= target / g[0]
    return s


def fit_metrics(person, ray, mesh):
    V = np.asarray(mesh.vertices)
    inside = ray.contains_points(V)
    _cp, dist, _tid = closest_point(person, V)
    clearance, pen = dist[~inside], dist[inside]
    out = {
        "inside_total": int(inside.sum()),
        "penetration_max_mm": round(float(pen.max() * 1000), 2) if len(pen) else 0.0,
        "clearance_min_mm": round(float(clearance.min() * 1000), 2) if len(clearance) else None,
        "critical_lt2mm": int((clearance < 0.002).sum()) if len(clearance) else 0,
        "regions": {},
    }
    for label, fn in REGIONS:
        mask = fn(V)
        if not mask.any():
            continue
        ins, d = inside[mask], dist[mask]
        cl, pn = d[~ins], d[ins]
        out["regions"][label] = {
            "n": int(mask.sum()),
            "inside": int(ins.sum()),
            "penetration_max_mm": round(float(pn.max() * 1000), 2) if len(pn) else 0.0,
            "clearance_min_mm": round(float(cl.min() * 1000), 2) if len(cl) else None,
        }
    return out


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("package_dir", nargs="?", default=DEFAULT_PKG)
    ap.add_argument("--json", default=None)
    a = ap.parse_args(argv)
    pkg = os.path.abspath(a.package_dir)
    person = trimesh.load(os.path.join(pkg, "person-001.glb"), force="scene").to_geometry()
    ray = RayMeshIntersector(person)
    results = []
    for name in ASSETS:
        base = trimesh.load(os.path.join(pkg, name), force="scene").to_geometry()
        for label, target in TARGETS:
            s = solve_s(base, target) if label != "B 成衣外轮廓（现状）" else 1.0
            mesh = scale_xz(base, s)
            g = half_girth(mesh)
            m = fit_metrics(person, ray, mesh)
            row = {
                "file": name, "scheme": label, "target_c_half_cm": target,
                "scale_xz": round(s, 6),
                "c_half_cm": round(g[0], 2) if g else None,
                "w_proj_cm": round(g[1], 2) if g else None,
                **m,
            }
            results.append(row)
            print(f"{name:16} {label:20} s={s:.5f} C_half={g[0]:6.2f} W_proj={g[1]:6.2f} "
                  f"内部={m['inside_total']:>5} 最大穿入={m['penetration_max_mm']:>6}mm "
                  f"最小间隙={m['clearance_min_mm']}mm <2mm={m['critical_lt2mm']}")
    text = json.dumps(results, ensure_ascii=False, indent=2)
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            f.write(text)
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(main(sys.argv[1:]))
