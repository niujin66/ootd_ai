# -*- coding: utf-8 -*-
"""剥离 GLB 中的 KHR_materials_specular / KHR_materials_sheen 扩展。

等价于 Blender 导出时关闭 Specular / Sheen 输出（材质本体已是
metallic/roughness 工作流，扩展均为可选且未列入 extensionsRequired）。
几何、贴图、节点全部原样保留（BIN chunk 不动，只重写 JSON chunk）。

用法：
    python strip_khr_extensions.py <src_dir> <dst_dir> [文件1 文件2 ...]

不给文件名时默认处理 top-001_v2.glb / top-002_v1.glb。
输入已是干净文件时输出与原文件逐字节一致（幂等）。
"""
import hashlib
import json
import os
import struct
import sys

STRIP = ("KHR_materials_specular", "KHR_materials_sheen")
DEFAULT_FILES = ("top-001_v2.glb", "top-002_v1.glb")


def strip_glb(src, dst):
    with open(src, "rb") as f:
        data = f.read()
    magic, ver, _total = struct.unpack("<III", data[:12])
    assert magic == 0x46546C67, "not a GLB"
    clen, ctype = struct.unpack("<II", data[12:20])
    js = json.loads(data[20:20 + clen])
    bin_chunk = data[20 + clen:]

    removed = []
    for m in js.get("materials", []):
        exts = m.get("extensions")
        if not exts:
            continue
        for key in list(exts):
            if key in STRIP:
                exts.pop(key)
                removed.append((m.get("name", "?"), key))
        if not exts:
            m.pop("extensions")

    used = js.get("extensionsUsed")
    if used is not None:
        left = [e for e in used if e not in STRIP]
        if left:
            js["extensionsUsed"] = left
        else:
            js.pop("extensionsUsed")

    if not removed:
        print(f"SKIP {os.path.basename(src)} 无待剥离扩展，按原样复制")
        with open(dst, "wb") as f:
            f.write(data)
        sha = hashlib.sha256(data).hexdigest()
        return len(data), sha, None, None

    jbytes = json.dumps(js, separators=(",", ":")).encode("utf-8")
    jbytes += b" " * ((4 - len(jbytes) % 4) % 4)
    total = 12 + 8 + len(jbytes) + len(bin_chunk)
    out = struct.pack("<III", 0x46546C67, 2, total)
    out += struct.pack("<II", len(jbytes), 0x4E4F534A) + jbytes
    out += bin_chunk
    with open(dst, "wb") as f:
        f.write(out)

    # 复核：总长、JSON 块类型、扩展已清除
    with open(dst, "rb") as f:
        d2 = f.read()
    _m2, _v2, t2 = struct.unpack("<III", d2[:12])
    c2, ct2 = struct.unpack("<II", d2[12:20])
    assert t2 == len(d2) and ct2 == 0x4E4F534A
    js2 = json.loads(d2[20:20 + c2])
    assert "extensionsUsed" not in js2 or not any(e in STRIP for e in js2["extensionsUsed"])
    for m in js2.get("materials", []):
        assert not any(k in m.get("extensions", {}) for k in STRIP)

    vcount = tcount = 0
    for mesh in js2.get("meshes", []):
        for prim in mesh["primitives"]:
            vcount += js2["accessors"][prim["attributes"]["POSITION"]]["count"]
            if "indices" in prim:
                tcount += js2["accessors"][prim["indices"]]["count"] // 3

    sha = hashlib.sha256(out).hexdigest()
    print(f"OK {os.path.basename(dst)}")
    for name, key in removed:
        print(f"   stripped {key} <- {name}")
    print(f"   size={len(out)} sha256={sha[:16]}…")
    print(f"   meshes={len(js2.get('meshes', []))} verts={vcount} tris={tcount}")
    print(f"   extensionsUsed={js2.get('extensionsUsed')}")
    return len(out), sha, vcount, tcount


def main(argv):
    if len(argv) < 3:
        print(__doc__)
        return 2
    src_dir, dst_dir = argv[1], argv[2]
    files = argv[3:] or list(DEFAULT_FILES)
    os.makedirs(dst_dir, exist_ok=True)
    results = {}
    for name in files:
        results[name] = strip_glb(os.path.join(src_dir, name), os.path.join(dst_dir, name))
    print(json.dumps({k: [v[0], v[1], v[2], v[3]] for k, v in results.items()}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
