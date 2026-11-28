# -*- coding: utf-8 -*-
"""组装 person-001_v2.glb：V9 头部 + 03b 躯干 + 长裤/鞋（顶点色分段）

修正版：用 bpy.data.objects 差集精确抓取新导入对象，避免把躯干当成头部。
"""
import os

import bmesh
import bpy
import numpy as np

BODY = r"D:\Desktop\软创\_work\ootd_ai_src\local-data\handoff\T02-candidate-20261117\person-001.glb"
HEAD = r"D:\Desktop\软创\_work\.garment-src\head-v9-neckcut-aligned-1024.glb"
OUT = r"D:\Desktop\软创\_work\.person-build"
os.makedirs(OUT, exist_ok=True)
NECK_Z = 1.5181
WAIST_Z = 1.010
HEM_Z = 0.115
SKIN = (0.79, 0.46, 0.31)
PANTS = (0.013, 0.017, 0.027)
SHOES = (0.006, 0.006, 0.008)


def import_new(path):
    before = set(bpy.data.objects)
    bpy.ops.import_scene.gltf(filepath=path)
    new = [o for o in bpy.data.objects if o not in before and o.type == "MESH"]
    return new


def join_all(objs, name):
    for o in bpy.context.view_layer.objects:
        o.select_set(False)
    for o in objs:
        o.select_set(True)
    bpy.context.view_layer.objects.active = objs[0]
    if len(objs) > 1:
        bpy.ops.object.join()
    ob = bpy.context.view_layer.objects.active
    ob.name = name
    return ob


def co_of(ob):
    n = len(ob.data.vertices)
    a = np.empty(n * 3)
    ob.data.vertices.foreach_get("co", a)
    return a.reshape(n, 3)


def stats(ob):
    bm = bmesh.new()
    bm.from_mesh(ob.data)
    bnd = sum(1 for e in bm.edges if len(e.link_faces) == 1)
    nonman = sum(1 for e in bm.edges if len(e.link_faces) > 2)
    bm.free()
    return bnd, nonman


def cut_above(ob, z):
    bm = bmesh.new()
    bm.from_mesh(ob.data)
    bm.verts.ensure_lookup_table()
    drop = [v for v in bm.verts if v.co.z > z]
    n0 = len(bm.verts)
    if drop:
        bmesh.ops.delete(bm, geom=drop, context="VERTS")
    edges = [e for e in bm.edges if len(e.link_faces) == 1]
    filled = 0
    if edges:
        r = bmesh.ops.holes_fill(bm, edges=edges, sides=0)
        filled = len(r.get("faces", []))
    bm.to_mesh(ob.data)
    bm.free()
    ob.data.update()
    print("   切掉 %d 顶点（%d -> %d），封口面 %d" % (
        n0 - len(ob.data.vertices), n0, len(ob.data.vertices), filled))


def recolor(ob):
    me = ob.data
    ca = me.color_attributes[0]
    z = co_of(ob)[:, 2]
    print("   domain=%s type=%s" % (ca.domain, ca.data_type))
    if ca.domain == "POINT":
        n = len(me.vertices)
        arr = np.empty(n * 4)
        ca.data.foreach_get("color", arr)
        arr = arr.reshape(n, 4)
        for i in range(n):
            zi = z[i]
            arr[i, :3] = SHOES if zi <= HEM_Z else PANTS if zi <= WAIST_Z else (
                SKIN if zi <= NECK_Z else arr[i, :3])
        ca.data.foreach_set("color", arr.reshape(-1))
    else:
        nloops = len(me.loops)
        vidx = np.empty(nloops, dtype=np.int64)
        me.loops.foreach_get("vertex_index", vidx)
        arr = np.empty(nloops * 4)
        ca.data.foreach_get("color", arr)
        arr = arr.reshape(nloops, 4)
        for i in range(nloops):
            zi = z[vidx[i]]
            arr[i, :3] = SHOES if zi <= HEM_Z else PANTS if zi <= WAIST_Z else (
                SKIN if zi <= NECK_Z else arr[i, :3])
        ca.data.foreach_set("color", arr.reshape(-1))
    me.update()
    print("   上色：鞋 %d / 裤 %d / 肤 %d" % (
        int((z <= HEM_Z).sum()), int(((z > HEM_Z) & (z <= WAIST_Z)).sum()),
        int(((z > WAIST_Z) & (z <= NECK_Z)).sum())))


def main():
    bpy.ops.wm.read_factory_settings(use_empty=True)

    body = join_all(import_new(BODY), "body-03b")
    bnd, nm = stats(body)
    print("== 躯干 ==")
    print("   verts=%d polys=%d 边界边=%d 非流形=%d" % (
        len(body.data.vertices), len(body.data.polygons), bnd, nm))
    cut_above(body, NECK_Z)
    recolor(body)
    bnd, nm = stats(body)
    print("   切后 边界边=%d 非流形=%d" % (bnd, nm))

    head = join_all(import_new(HEAD), "head-v9")
    hc = co_of(head)
    print("== 头部 ==")
    print("   verts=%d polys=%d 材质=%d UV=%s" % (
        len(head.data.vertices), len(head.data.polygons),
        len(head.data.materials), [a.name for a in head.data.uv_layers]))
    print("   bbox %s .. %s" % (np.round(hc.min(axis=0), 4), np.round(hc.max(axis=0), 4)))
    bnd, nm = stats(head)
    print("   边界边=%d 非流形=%d" % (bnd, nm))
    #  头部底口封口
    bm = bmesh.new()
    bm.from_mesh(head.data)
    edges = [e for e in bm.edges if len(e.link_faces) == 1]
    if 0 < len(edges) < 4000:
        r = bmesh.ops.holes_fill(bm, edges=edges, sides=0)
        print("   封口面 %d（边界边 %d）" % (len(r.get("faces", [])), len(edges)))
        bm.to_mesh(head.data)
        head.data.update()
    else:
        print("   边界边 %d，跳过封口" % len(edges))
    bm.free()

    print("== 导出 ==")
    for o in bpy.context.view_layer.objects:
        o.select_set(False)
    body.select_set(True)
    head.select_set(True)
    bpy.context.view_layer.objects.active = body
    out = os.path.join(OUT, "person-001_v2.glb")
    bpy.ops.export_scene.gltf(
        filepath=out, export_format="GLB", export_yup=True, use_selection=True,
        export_apply=True, export_image_format="AUTO", export_all_vertex_colors=True,
    )
    print("   %s %.2f MB（限 5 MB）" % (os.path.basename(out), os.path.getsize(out) / 1048576))
    total_v = len(body.data.vertices) + len(head.data.vertices)
    total_p = len(body.data.polygons) + len(head.data.polygons)
    print("   合计 verts=%d polys=%d" % (total_v, total_p))
    bpy.ops.wm.save_as_mainfile(filepath=os.path.join(OUT, "person-001_v2.blend"))
    print("DONE")


main()