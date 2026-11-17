# -*- coding: utf-8 -*-
"""自查：打印 GLB 的 extensionsUsed / extensionsRequired 与各材质的扩展。

用法：
    python tools/inspect_extensions.py                 # 检查当前目录所有 glb
    python tools/inspect_extensions.py a.glb b.glb     # 检查指定文件
退出码：0 = 无禁用扩展；1 = 存在 KHR_materials_specular / KHR_materials_sheen
"""
import struct, json, os, sys, glob

STRIP = ("KHR_materials_specular", "KHR_materials_sheen")

def inspect(path):
    with open(path, "rb") as f:
        data = f.read()
    magic, ver, total = struct.unpack("<III", data[:12])
    if magic != 0x46546C67:
        print(f"{path}: 不是 GLB 文件")
        return True
    clen, _ = struct.unpack("<II", data[12:20])
    js = json.loads(data[20:20 + clen])
    used = js.get("extensionsUsed", [])
    req = js.get("extensionsRequired", [])
    bad = [e for e in used if e in STRIP] + [e for e in req if e in STRIP]
    print(f"=== {os.path.basename(path)} ({total} B) ===")
    print(f"  extensionsUsed     : {used or '无'}")
    print(f"  extensionsRequired : {req or '无'}")
    for i, m in enumerate(js.get("materials", [])):
        exts = list(m.get("extensions", {}).keys())
        pbr = m.get("pbrMetallicRoughness", {})
        print(f"  material[{i}] {m.get('name')}: pbr={list(pbr.keys()) or '无'} extensions={exts or '无'}")
    print(f"  => {'❌ 含 SceneView 4.67 不支持的扩展' if bad else 'OK 基础 PBR，无禁用扩展'}")
    return bool(bad)

if __name__ == "__main__":
    files = sys.argv[1:] or sorted(glob.glob("*.glb"))
    if not files:
        print("当前目录没有 glb 文件")
        sys.exit(0)
    bad = any(inspect(f) for f in files)
    sys.exit(1 if bad else 0)
