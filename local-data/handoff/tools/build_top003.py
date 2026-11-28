# -*- coding: utf-8 -*-
"""top-003：以 top-001 干净拓扑为模板，把短袖挤出为长袖，衣长按实物 74cm 重定标。

- 袖口环：边界边且两端 |x| > 0.35（top-001 实测 36 顶点，中心 ±0.398, y0.099, z1.414，半径 5.23 cm）
- 手臂走向：躯干实测 x 0.30–0.90 段 z≈1.41、y≈0.10–0.13 水平，腕部在 x≈0.72
- 目标袖口：x≈0.70，半径≈3.6 cm（袖子比手臂松一圈）
- 贴图：新面继承源环 UV，袖筒保持原袖料颜色，不破坏原有印花 UV
"""
import os

import bmesh
import bpy
import numpy as np
from mathutils import Vector

SRC = r"D:\Desktop\软创\_work\ootd_ai_src\local-data\handoff\T02-candidate-20261117\top-001_v2.glb"
OUT = r"D:\Desktop\软创\_work\.top003-build"
OUT_GLB = os.path.join(OUT, "top-003.glb")
os.makedirs(OUT, exist_ok=True)

TARGET_LEN = 0.74
CUFF_X = 0.700
CUFF_R = 0.036
STEPS = 8


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
    return ob


def build_sleeve(bm, sign, uv_layer):
    es = [e for e in bm.edges
          if len(e.link_faces) == 1 and all(v.co.x * sign > 0.35 for v in e.verts)]
    if not es:
        print("   [%+d] 找不到袖口环" % sign)
        return 0
    verts = {v for e in es for v in e.verts}
    cen = Vector((0, 0, 0))
    for v in verts:
        cen += v.co
    cen /= len(verts)
    start_x = cen.x
    step_len = (CUFF_X - abs(start_x)) / STEPS
    if step_len <= 0:
        print("   [%+d] 袖口已在 %.3f，无需延长" % (sign, start_x))
        return 0
    d = Vector((sign * 0.302, 0.012, -0.004)).normalized()
    taper = (CUFF_R / 0.0523) ** (1.0 / STEPS)

    uvmap = {}
    for v in verts:
        acc = Vector((0, 0))
        n = 0
        for f in v.link_faces:
            for l in f.loops:
                if l.vert is v:
                    acc += l[uv_layer].uv
                    n += 1
        uvmap[v] = acc / max(1, n)

    cur_edges = es
    nf_count = 0
    filled = 0
    for i in range(STEPS):
        res = bmesh.ops.extrude_edge_only(bm, edges=cur_edges)
        nv = [g for g in res["geom"] if isinstance(g, bmesh.types.BMVert)]
        ne = [g for g in res["geom"] if isinstance(g, bmesh.types.BMEdge)]
        nf = [g for g in res["geom"] if isinstance(g, bmesh.types.BMFace)]
        src = {}
        for v in nv:
            for e in v.link_edges:
                o = e.other_vert(v)
                if o in verts:
                    src[v] = o
                    break
        for v, sv in src.items():
            p = sv.co + d * step_len
            v.co = cen + (p - cen) * taper
            uvmap[v] = uvmap[sv]
        for f in nf:
            for l in f.loops:
                if l.vert in uvmap:
                    l[uv_layer].uv = uvmap[l.vert]
        nf_count = len(nf)
        verts = set(nv)
        c = Vector((0, 0, 0))
        for v in verts:
            c += v.co
        cen = c / len(verts)
        cur_edges = [e for e in ne if len(e.link_faces) == 1]

    #  封袖口
    if cur_edges:
        r = bmesh.ops.holes_fill(bm, edges=cur_edges, sides=0)
        filled = len(r.get("faces", []))
        for f in r.get("faces", []):
            for l in f.loops:
                l[uv_layer].uv = Vector((0.5, 0.5))
    print("   [%+d] 袖筒延长 %.3f m，%d 段，新增面 %d，袖口封 %d 面" % (
        sign, step_len * STEPS, STEPS, nf_count, filled))
    return nf_count


def main():
    ob = load(SRC)
    me = ob.data
    print("== 模板 ==")
    print("   verts=%d polys=%d 材质=%d" % (len(me.vertices), len(me.polygons), len(me.materials)))
    co = np.array([v.co[:] for v in me.vertices])
    print("   bbox z %.4f..%.4f（衣长 %.2f cm）" % (co[:, 2].min(), co[:, 2].max(),
                                                (co[:, 2].ptp()) * 100))

    bm = bmesh.new()
    bm.from_mesh(me)
    uv_layer = bm.loops.layers.uv.active
    print("== 挤长袖 ==")
    build_sleeve(bm, 1, uv_layer)
    build_sleeve(bm, -1, uv_layer)
    bm.to_mesh(me)
    bm.free()
    me.update()

    #  衣长归到 74 cm（以顶部为基准压 Z）
    co = np.array([v.co[:] for v in me.vertices])
    zlo, zhi = co[:, 2].min(), co[:, 2].max()
    cur = zhi - zlo
    k = TARGET_LEN / cur
    print("== 衣长归位 ==")
    print("   当前 %.2f cm -> 目标 %.0f cm，Z 缩放 %.5f" % (cur * 100, TARGET_LEN * 100, k))
    for v in me.vertices:
        v.co.z = zhi - (zhi - v.co.z) * k
    me.update()

    co = np.array([v.co[:] for v in me.vertices])
    print("   新 bbox x %.4f..%.4f y %.4f..%.4f z %.4f..%.4f" % (
        co[:, 0].min(), co[:, 0].max(), co[:, 1].min(), co[:, 1].max(),
        co[:, 2].min(), co[:, 2].max()))
    print("   衣长 %.2f cm，袖展 %.2f cm" % ((co[:, 2].ptp()) * 100, co[:, 0].ptp() * 100))

    bm = bmesh.new()
    bm.from_mesh(me)
    bnd = sum(1 for e in bm.edges if len(e.link_faces) == 1)
    nm = sum(1 for e in bm.edges if len(e.link_faces) > 2)
    print("   边界边=%d 非流形=%d verts=%d polys=%d" % (bnd, nm, len(me.vertices), len(me.polygons)))
    bm.free()

    bpy.ops.export_scene.gltf(filepath=OUT_GLB, export_format="GLB", export_yup=True,
                              use_selection=True, export_apply=True, export_image_format="AUTO")
    print("== 导出 ==")
    print("   %s %.2f MB" % (os.path.basename(OUT_GLB), os.path.getsize(OUT_GLB) / 1048576))
    bpy.ops.wm.save_as_mainfile(filepath=os.path.join(OUT, "top-003.blend"))
    print("DONE")


main()