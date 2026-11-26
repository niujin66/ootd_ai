# -*- coding: utf-8 -*-
"""fixture-face-04 合包器：校验 4 个 GLB，生成 manifest.json 与 validation-report.json。

用法（在 _work\\ootd_ai_src 下）：
    python ../../pack_fixture04.py --src <资产目录> [--out <输出目录>]

约定：
- 资产目录里放 person-001_v2.glb / top-001_v2.glb / top-002_v1.glb / top-003.glb
- content_digest 与 server/importer.py 的 _content_digest 逐字节一致
- manifest.json 写成 UTF-8 无 BOM（03b 那份 display_name 乱码就是因为这个）
- 任一文件缺 KHR 扩展门禁就整体 exit 1，不产出半成品包
"""
import argparse
import hashlib
import json
import os
import struct
import subprocess
import sys

GUARD = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "ootd_ai_src", "server", "tools", "export_glb_guard.py")
BANNED = ("KHR_materials_specular", "KHR_materials_sheen")

ASSETS = [
    dict(asset_type="person", asset_id="person-001", relative_file="person-001_v2.glb",
         source="v9-head-on-bv2-body", display_name="人物 person-001 v2",
         available=True, tags=None),
    dict(asset_type="garment", asset_id="top-001", relative_file="top-001_v2.glb",
         source="real-garment-reconstruction", display_name="浅色印花短袖",
         available=True, tags=["white", "casual", "print"]),
    dict(asset_type="garment", asset_id="top-002", relative_file="top-002_v1.glb",
         source="real-garment-reconstruction", display_name="深蓝翻领 POLO",
         available=True, tags=["blue", "dark", "polo", "casual"]),
    dict(asset_type="garment", asset_id="top-003", relative_file="top-003.glb",
         source="real-garment-reconstruction", display_name="白色印花短袖",
         available=True, tags=["white", "print", "casual"]),
]


def glb_stats(path):
    data = open(path, "rb").read()
    magic, ver, total = struct.unpack_from("<III", data, 0)
    if magic != 0x46546C67:
        raise SystemExit("不是 GLB：%s" % path)
    jlen, jtype = struct.unpack_from("<II", data, 12)
    js = json.loads(data[20:20 + jlen].decode("utf-8"))
    verts = tris = 0
    for m in js.get("meshes", []):
        for pr in m.get("primitives", []):
            pos = pr.get("attributes", {}).get("POSITION")
            idx = pr.get("indices")
            v = js["accessors"][pos]["count"] if pos is not None else 0
            verts += v
            tris += (js["accessors"][idx]["count"] // 3) if idx is not None else v // 3
    used = js.get("extensionsUsed") or []
    return dict(size=len(data), sha256=hashlib.sha256(data).hexdigest(),
                verts=verts, tris=tris, extensions=list(used),
                images=len(js.get("images", [])), materials=len(js.get("materials", [])))


def content_digest(manifest, records):
    d = hashlib.sha256()
    d.update(json.dumps(manifest, sort_keys=True, ensure_ascii=False).encode("utf-8"))
    for a in sorted(records, key=lambda x: x.get("relative_file") or ""):
        rel, sha = a.get("relative_file"), a.get("sha256")
        if rel and sha:
            d.update(rel.encode("utf-8"))
            d.update(sha.encode("ascii"))
    return d.hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, help="资产目录")
    ap.add_argument("--out", default=None, help="输出目录，默认同 --src")
    ap.add_argument("--version", default="fixture-face-04")
    args = ap.parse_args()
    out = args.out or args.src
    os.makedirs(out, exist_ok=True)

    records = []
    print("== 逐个校验 ==")
    for spec in ASSETS:
        p = os.path.join(args.src, spec["relative_file"])
        if not os.path.isfile(p):
            raise SystemExit("缺文件：%s" % spec["relative_file"])
        st = glb_stats(p)
        bad = [e for e in st["extensions"] if e in BANNED]
        flag = "OK" if not bad else "BLOCKED:%s" % ",".join(bad)
        print("  %-18s %8d B  v=%6d t=%6d  ext=%s  %s" % (
            spec["relative_file"], st["size"], st["verts"], st["tris"],
            st["extensions"] or "无", flag))
        if bad:
            raise SystemExit("门禁未过：%s 含 %s，先跑 export_glb_guard strip" % (p, bad))
        rec = dict(spec)
        rec.update(size_bytes=st["size"], sha256=st["sha256"],
                   vertex_count=st["verts"], triangle_count=st["tris"],
                   extensions_used=st["extensions"])
        records.append(rec)

    manifest = {
        "schema_version": 1,
        "person_id": "person-001",
        "catalog_version": args.version,
        "pose_id": "pose-001",
        "assets": [],
    }
    for r in records:
        entry = {k: r[k] for k in ("asset_type", "asset_id", "relative_file", "source")}
        if r.get("display_name"):
            entry["display_name"] = r["display_name"]
        if r.get("available") is not None:
            entry["available"] = r["available"]
        if r.get("tags"):
            entry["tags"] = r["tags"]
        manifest["assets"].append(entry)

    mpath = os.path.join(out, "manifest.json")
    with open(mpath, "w", encoding="utf-8", newline="\n") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
        f.write("\n")
    raw = open(mpath, "rb").read()
    if raw[:3] == b"\xef\xbb\xbf":
        raise SystemExit("manifest 写了 BOM，03b 的 display_name 乱码就是这样来的")
    json.loads(raw.decode("utf-8"))

    digest = content_digest(manifest, records)
    report = {
        "catalog_version": args.version,
        "person_id": "person-001",
        "pose_id": "pose-001",
        "content_digest": digest,
        "assets": [{k: r[k] for k in ("asset_id", "relative_file", "size_bytes", "sha256",
                                      "vertex_count", "triangle_count", "extensions_used")}
                   for r in records],
    }
    rpath = os.path.join(out, "validation-report.json")
    with open(rpath, "w", encoding="utf-8", newline="\n") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
        f.write("\n")

    print("\n== 产出 ==")
    print("  %s (UTF-8 无 BOM，%d 字节)" % (os.path.basename(mpath), len(raw)))
    print("  %s" % os.path.basename(rpath))
    print("  content_digest = %s" % digest)
    total = sum(r["size_bytes"] for r in records)
    print("  合计 %.2f MB" % (total / 1048576))

    if os.path.isfile(GUARD):
        print("\n== export_glb_guard check ==")
        p = subprocess.run([sys.executable, GUARD, "check", args.src],
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        print("  exit=%d" % p.returncode)
        for line in (p.stdout or p.stderr).strip().splitlines()[:8]:
            print("  " + line)
        if p.returncode != 0:
            raise SystemExit("门禁 check 未过")
    print("\nOK")


main()