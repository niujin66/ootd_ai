# -*- coding: utf-8 -*-
"""胸宽量法实测脚本：同一份 GLB 上同时给出「截面半围」与「正视投影宽」。

口径：
    截面半围 = 指定高度水平截面，最大闭合环的周长 ÷ 2   （与平铺半围可直接比）
    投影宽   = 该环在 X 方向的跨度                        （只作参考，不可与平铺宽比）

用法：
    python measure_chest.py [包目录] [人台 glb]
    默认：脚本所在包的目录、包内 person-001.glb

依赖：numpy、trimesh
"""
import os
import sys

import numpy as np
import trimesh

HEIGHTS = [1.34, 1.32, 1.30, 1.28, 1.25, 1.20, 1.10, 0.95, 0.92]


def loops_at(mesh, y):
    sec = mesh.section(plane_origin=[0, y, 0], plane_normal=[0, 1, 0])
    if sec is None:
        return []
    out = []
    for poly in sec.discrete:
        p = np.asarray(poly)
        if len(p) < 3:
            continue
        out.append((np.linalg.norm(np.diff(p, axis=0), axis=1).sum(), p))
    return out


def report(path, label, heights):
    sc = trimesh.load(path, force="scene")
    m = sc.to_geometry()
    b = m.bounds
    print(f"== {label} | {os.path.basename(path)}")
    print(f"   包围盒 {b[1][0]-b[0][0]:.4f} x {b[1][1]-b[0][1]:.4f} x {b[1][2]-b[0][2]:.4f} m | y {b[0][1]:.3f}..{b[1][1]:.3f}")
    for y in heights:
        ls = loops_at(m, y)
        if not ls:
            print(f"   y={y:.3f} 无截面")
            continue
        ls.sort(key=lambda t: -t[0])
        L, p = ls[0]
        w = p[:, 0].max() - p[:, 0].min()
        d = p[:, 2].max() - p[:, 2].min()
        print(f"   y={y:.3f} 环数{len(ls)} 围长{L*100:6.2f} 半围{L/2*100:6.2f} 投影宽{w*100:6.2f} 厚{d*100:6.2f}")


def main(argv):
    here = os.path.dirname(os.path.abspath(__file__))
    default_pkg = os.path.dirname(here)
    pkg = argv[1] if len(argv) > 1 else default_pkg
    person = argv[2] if len(argv) > 2 else os.path.join(pkg, "person-001.glb")
    for name, label in (("top-001_v2.glb", "top-001"), ("top-002_v1.glb", "top-002")):
        p = os.path.join(pkg, name)
        if os.path.exists(p):
            report(p, label, HEIGHTS)
    if os.path.exists(person):
        report(person, "person-001 人体", [1.30, 1.20])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
