# -*- coding: utf-8 -*-
"""GLB 材质扩展门禁 —— 资产侧导出后必跑，不依赖客户端兜底。

背景
----
Blender 4.2.9 的 glTF 导出器在材质 ``Specular IOR Level`` / ``Sheen Weight``
偏离默认值时会写出 ``KHR_materials_specular`` / ``KHR_materials_sheen``。
Filament 1.77.3（SceneView 4.67.0 依赖）加载这两类扩展会在
``AssetLoader::createAsset`` 触发 ``PreconditionPanic`` -> SIGABRT，Java 层拦不住。

实测结论（Blender 4.2.9，见 T02-材质扩展修复说明-1119.md 第 3 节）：
* ``export_original_specular`` 无论 True / False **都照样输出**这两个扩展，
  它只改变 specular 值的换算方式，不是"是否输出扩展"的开关。
* 唯一能让导出器不写扩展的办法是把材质节点恢复默认值
  （Specular IOR Level = 0.5、Sheen Weight = 0.0），但那会改动外观参数。
* 因此把「导出后自检 + 剥离」固化为交付门禁：BIN chunk 逐字节不动，
  剥离结果与 App 内置素材 / ``outputs/fault-fixtures-03b/good/`` 基准哈希一致。

用法
----
命令行（CI / 交付前）：
    python export_glb_guard.py check  <dir-or-glb> [...]   # 发现禁用扩展 exit 1
    python export_glb_guard.py strip  <src_dir> <dst_dir> [file1 file2 ...]
    python export_glb_guard.py verify <glb> <reference_glb>  # 逐字节比对

Blender 内（在建模导出脚本里替换 bpy.ops.export_scene.gltf）：
    import sys; sys.path.insert(0, r"<repo>/server/tools")
    from export_glb_guard import export_clean_glb
    export_clean_glb(filepath=out, use_selection=True, export_apply=True,
                     export_yup=True, export_format="GLB")

``export_clean_glb`` 会在导出后立刻自检并剥离，导出件落盘即为干净版本。
任何其它路径产出的 GLB，交付前都要过 ``check``。
"""
import hashlib
import json
import os
import struct
import sys

STRIP = ("KHR_materials_specular", "KHR_materials_sheen")
DEFAULT_FILES = ("top-001_v2.glb", "top-002_v1.glb")
GLB_MAGIC = 0x46546C67
JSON_CHUNK = 0x4E4F534A


def read_glb(path):
    with open(path, "rb") as f:
        data = f.read()
    magic = struct.unpack_from("<I", data, 0)[0]
    if magic != GLB_MAGIC:
        raise ValueError(f"not a GLB: {path}")
    clen, ctype = struct.unpack_from("<II", data, 12)
    if ctype != JSON_CHUNK:
        raise ValueError(f"first chunk is not JSON: {path}")
    return data, clen, json.loads(data[20:20 + clen]), data[20 + clen:]


def find_extensions(js):
    """返回该文件里所有禁用扩展的出处。"""
    hits = []
    for e in js.get("extensionsUsed", []) or []:
        if e in STRIP:
            hits.append(("extensionsUsed", e))
    for m in js.get("materials", []) or []:
        for e in (m.get("extensions") or {}):
            if e in STRIP:
                hits.append((f"material:{m.get('name', '?')}", e))
    return hits


def strip_bytes(data):
    """剥离禁用扩展，BIN chunk 原样保留。与 strip_khr_extensions.py 算法一致。"""
    clen = struct.unpack_from("<I", data, 12)[0]
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
        return data, removed

    jbytes = json.dumps(js, separators=(",", ":")).encode("utf-8")
    jbytes += b" " * ((4 - len(jbytes) % 4) % 4)
    total = 12 + 8 + len(jbytes) + len(bin_chunk)
    out = struct.pack("<III", GLB_MAGIC, 2, total)
    out += struct.pack("<II", len(jbytes), JSON_CHUNK) + jbytes
    out += bin_chunk
    return out, removed


def strip_file(src, dst):
    data = open(src, "rb").read()
    out, removed = strip_bytes(data)
    os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)
    with open(dst, "wb") as f:
        f.write(out)
    sha = hashlib.sha256(out).hexdigest()
    tag = "STRIP" if removed else "SKIP (already clean)"
    print(f"{tag:20} {os.path.basename(src)}  size={len(out)}  sha256={sha[:16]}…")
    for name, key in removed:
        print(f"    - {key} <- {name}")
    # 落盘后复核
    hits = find_extensions(read_glb(dst)[2])
    assert not hits, f"strip failed: {hits}"
    return len(out), sha, removed


def collect(paths):
    out = []
    for p in paths:
        if os.path.isdir(p):
            for f in sorted(os.listdir(p)):
                if f.lower().endswith(".glb"):
                    out.append(os.path.join(p, f))
        elif p.lower().endswith(".glb"):
            out.append(p)
    return out


def cmd_check(paths):
    files = collect(paths)
    if not files:
        print("no .glb found", file=sys.stderr)
        return 2
    bad = 0
    for p in files:
        hits = find_extensions(read_glb(p)[2])
        if hits:
            bad += 1
            print(f"FAIL {p}")
            for where, e in hits:
                print(f"    {e}  @ {where}")
        else:
            print(f"PASS {p}")
    print(f"\n{len(files) - bad}/{len(files)} clean; {bad} blocked")
    return 1 if bad else 0


def cmd_strip(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    src, dst = argv[0], argv[1]
    files = argv[2:] or list(DEFAULT_FILES)
    for name in files:
        strip_file(os.path.join(src, name), os.path.join(dst, name))
    return 0


def cmd_verify(argv):
    if len(argv) != 2:
        print(__doc__)
        return 2
    a, b = argv
    da, db = open(a, "rb").read(), open(b, "rb").read()
    ha, hb = hashlib.sha256(da).hexdigest(), hashlib.sha256(db).hexdigest()
    same = da == db
    print(f"{a}: {len(da)} B  {ha}")
    print(f"{b}: {len(db)} B  {hb}")
    print("IDENTICAL" if same else "DIFFERENT")
    return 0 if same else 1


def export_clean_glb(filepath, **kwargs):
    """Blender 侧导出封装：导出 -> 自检 -> 剥离 -> 复核。

    用法等价于 bpy.ops.export_scene.gltf，额外保证落盘文件不含禁用扩展。
    """
    import bpy  # noqa: F401  (仅在 Blender 内调用)
    kwargs.setdefault("export_format", "GLB")
    kwargs.setdefault("export_materials", "EXPORT")
    bpy.ops.export_scene.gltf(filepath=filepath, **kwargs)
    before = find_extensions(read_glb(filepath)[2])
    if before:
        out, removed = strip_bytes(open(filepath, "rb").read())
        with open(filepath, "wb") as f:
            f.write(out)
        print(f"[guard] stripped {len(removed)} extension entries from {filepath}")
    after = find_extensions(read_glb(filepath)[2])
    assert not after, f"[guard] still contaminated: {after}"
    return filepath


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    mode, rest = argv[1], argv[2:]
    if mode == "check":
        return cmd_check(rest)
    if mode == "strip":
        return cmd_strip(rest)
    if mode == "verify":
        return cmd_verify(rest)
    print(f"unknown mode: {mode}\n")
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
