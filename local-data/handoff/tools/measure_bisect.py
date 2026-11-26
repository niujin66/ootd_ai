# -*- coding: utf-8 -*-
"""用 bmesh.bisect_plane 精确求水平截面环长（替代不可靠的三角形穿越法）。

口径同 measure_chest.py：截面最大闭合环周长 ÷ 2 = 半围。
"""
import bpy
import bmesh
import numpy as np

TARGETS = [
    (r"D:\Desktop\软创\_work\ootd_ai_src\local-data\handoff\T02-candidate-20261117\top-001_v2.glb",
     "top-001 模板（校验：y=1.30 应为 54.35）"),
    (r"D:\Desktop\软创\_work\.top003-build\top-003.glb", "top-003 长袖兜底版"),
]
HEIGHTS = [1.38, 1.36, 1.34, 1.32, 1.30, 1.28, 1.25, 1.20, 1.10]


def load(path):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.import_scene.gltf(filepath=path)
    objs = [o for o in bpy.context.scene.objects if o.type == "MESH"]
    for o in objs:
        o.select_set(True)
    bpy.context.view_layer.objects.active = objs[0]
    if len(objs) > 1:
        bpy.ops.object.join()
    ob = bpy.context.view_layer.objects.active
    bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
    bm = bmesh.new()
    bm.from_mesh(ob.data)
    bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=1e-5)
    bm.to_mesh(ob.data)
    bm.free()
    ob.data.update()
    return ob


def section_loops(me, y):
    bm = bmesh.new()
    bm.from_mesh(me)
    geom = list(bm.verts) + list(bm.edges) + list(bm.faces)
    res = bmesh.ops.bisect_plane(
        bm, geom=geom, dist=1e-6,
        plane_co=(0.0, 0.0, y), plane_no=(0.0, 0.0, 1.0),
        clear_inner=False, clear_outer=False,
    )
    cut_edges = [g for g in res["geom_cut"] if isinstance(g, bmesh.types.BMEdge)]
    #  只取真正落在平面上的边
    cut_edges = [e for e in cut_edges if abs(e.verts[0].co.z - y) < 1e-4 and abs(e.verts[1].co.z - y) < 1e-4]
    if not cut_edges:
        bm.free()
        return []
    adj = {}
    elen = {}
    for e in cut_edges:
        a, b = e.verts[0].index, e.verts[1].index
        adj.setdefault(a, []).append(b)
        adj.setdefault(b, []).append(a)
        elen[(min(a, b), max(a, b))] = e.calc_length()
    seen, loops = set(), []
    for st in list(adj):
        if st in seen:
            continue
        comp = [st]
        seen.add(st)
        stack = [st]
        while stack:
            c = stack.pop()
            for n in adj[c]:
                if n not in seen:
                    seen.add(n)
                    comp.append(n)
                    stack.append(n)
        #  该连通分量里所有边长度之和（闭合成环时即周长）
        tot = 0.0
        edge_set = set()
        for v in comp:
            for n in adj[v]:
                k = (min(v, n), max(v, n))
                if k in edge_set:
                    continue
                edge_set.add(k)
                tot += elen[k]
        deg2 = all(len(adj[v]) == 2 for v in comp)
        if deg2 and len(comp) >= 6:
            loops.append(tot)
    verts_on_plane = [v for v in bm.verts if abs(v.co.z - y) < 1e-4]
    xs = np.array([v.co.x for v in verts_on_plane])
    zs = np.array([v.co.y for v in verts_on_plane])
    bm.free()
    loops.sort(reverse=True)
    return loops, xs, zs


for path, label in TARGETS:
    ob = load(path)
    me = ob.data
    co = np.array([v.co[:] for v in me.vertices])
    print("\n== %s ==" % label)
    print("   bbox x %.4f..%.4f  y %.4f..%.4f  z %.4f..%.4f" % (
        co[:, 0].min(), co[:, 0].max(), co[:, 1].min(), co[:, 1].max(),
        co[:, 2].min(), co[:, 2].max()))
    print("   衣长 %.2f cm（曲线 z），袖展 %.2f cm，verts=%d tris=%d" % (
        (co[:, 2].ptp()) * 100, co[:, 0].ptp() * 100, len(co), len(me.polygons)))
    for y in HEIGHTS:
        r = section_loops(me, y)
        if not r or not r[0]:
            print("   y=%.2f 无闭合环" % y)
            continue
        loops, xs, zs = r
        print("   y=%.2f 闭合环=%d 最大周长=%6.2f 半围=%6.2f 投影宽=%6.2f 厚=%5.2f" % (
            y, len(loops), loops[0] * 100, loops[0] / 2 * 100,
            (xs.max() - xs.min()) * 100, (zs.max() - zs.min()) * 100))