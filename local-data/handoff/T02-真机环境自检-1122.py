# -*- coding: utf-8 -*-
"""T02 真机环境一键自检（C / A 11/22 用，自包含，无需仓库其他模块）。

用途：把 #13 回执三条里「能自动取到的实际值」一次跑出来，避免现场靠嘴报、
也避免把 APK 内置素材目录标签 `fixture-face-03` 误判成 11-13 的带扩展旧包。

检查项：
  1. APK SHA-256 是否等于 B 的 `c92c65f` 重打包值
  2. assets 目录三份 GLB 的实算哈希/体积是否等于 03b 基准
  3. 目录标签（basename / active.json / manifest.json）与 catalog_version
  4. content_digest：按 server/importer.py::_content_digest 算法，用**实算文件哈希**
     重算，并与报告声明值、基准值三方比对
  5. 三份 GLB 是否残留 KHR_materials_specular / KHR_materials_sheen

用法：
    python T02-真机环境自检-1122.py --apk path/to/app-debug.apk --assets path/to/pkg
    python T02-真机环境自检-1122.py --assets path/to/pkg --baseline path/to/03b
    python T02-真机环境自检-1122.py --assets path/to/pkg --issue-text out.md

退出码：0 = 全部一致；1 = 有不一致（明细见输出）；2 = 参数/文件缺失
只用标准库。
"""
import argparse
import hashlib
import json
import os
import struct
import sys

EXPECTED_APK = "d4402052545991ed93a3c335532f7b2d77d38700364817d6feaca2bd22ea863a"
EXPECTED_DIGEST = "f0c05a0d8b86a8bfbbb5b314692af66cc5468f252c957bae6e6490b4f7388ca0"
EXPECTED_CATALOG = "fixture-face-03b"
EXPECTED_ASSETS = {
    "person-001.glb": (419572, "f3dbe4c875dafb73d161d28de311b33dd839e8ec2fb559624c129a3a66012354"),
    "top-001_v2.glb": (981408, "45301ea0feead027300fe1d9b6847dc7a93381638a0d4996496d6d9d745d15b5"),
    "top-002_v1.glb": (671540, "8502d846b4beb443b6acfbd3c91d5ec97369a374dc19352a77657aa3758f7e82"),
}
STRIP_EXTS = ("KHR_materials_specular", "KHR_materials_sheen")


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def glb_json(path):
    with open(path, "rb") as f:
        head = f.read(20)
    magic = struct.unpack_from("<I", head, 0)[0]
    if magic != 0x46546C67:
        return None
    clen, ctype = struct.unpack_from("<II", head, 12)
    if ctype != 0x4E4F534A:
        return None
    with open(path, "rb") as f:
        f.seek(20)
        return json.loads(f.read(clen))


def bad_exts(path):
    js = glb_json(path)
    if js is None:
        return ["<不是合法 GLB / JSON chunk 异常>"]
    hits = []
    for e in js.get("extensionsUsed", []) or []:
        if e in STRIP_EXTS:
            hits.append("extensionsUsed:" + e)
    for m in js.get("materials", []) or []:
        for e in (m.get("extensions") or {}):
            if e in STRIP_EXTS:
                hits.append("material:" + e)
    return hits


def content_digest(manifest, assets):
    """与 server/importer.py::_content_digest 完全一致；assets 的 sha256 可由调用方替换。"""
    d = hashlib.sha256()
    d.update(json.dumps(manifest, sort_keys=True, ensure_ascii=False).encode("utf-8"))
    for a in sorted(assets, key=lambda x: x.get("relative_file") or ""):
        rel, sh = a.get("relative_file"), a.get("sha256")
        if rel and sh:
            d.update(rel.encode("utf-8"))
            d.update(sh.encode("ascii"))
    return d.hexdigest()


def try_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def find_glb(root, name):
    """按文件名找，容忍子目录与 top-001.glb / top-001_v2.glb 两种命名。"""
    direct = os.path.join(root, name)
    if os.path.isfile(direct):
        return direct
    stem = name.replace("_v2", "").replace("_v1", "")
    for cand in (stem, name):
        p = os.path.join(root, cand)
        if os.path.isfile(p):
            return p
    for base, _dirs, files in os.walk(root):
        for f in files:
            if f.lower().endswith(".glb") and (f == name or f.replace("_v2", "").replace("_v1", "") == stem):
                return os.path.join(base, f)
    return None


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--apk", default=None, help="APK 文件路径")
    ap.add_argument("--assets", default=None, help="待检资产目录")
    ap.add_argument("--baseline", default=None, help="03b 基准目录（默认用内置期望值）")
    ap.add_argument("--issue-text", default=None, help="把可贴 Issue 的回执写到文件")
    a = ap.parse_args(argv)

    problems = []
    lines = []
    print("=" * 68)

    # 1. APK
    if a.apk:
        if not os.path.isfile(a.apk):
            print(f"[APK] 文件不存在: {a.apk}")
            problems.append("apk_missing")
        else:
            h = sha256(a.apk)
            ok = h == EXPECTED_APK
            print(f"[APK] {os.path.basename(a.apk)}  {os.path.getsize(a.apk)} B")
            print(f"      sha256 = {h}")
            print(f"      与 B 的 c92c65f 重打包值一致: {'YES' if ok else 'NO'}")
            if not ok:
                problems.append("apk_mismatch")
            lines.append(f"- APK SHA-256：`{h}`（{'与 B 交包一致' if ok else '**不一致，停下核对**'}），{os.path.getsize(a.apk)} B")
    else:
        print("[APK] 未提供 --apk，跳过（现场请人工核对 d4402052545991ed…）")

    # 2/3/4/5 资产目录
    if not a.assets:
        print("[ASSETS] 未提供 --assets，只完成 APK 检查")
    elif not os.path.isdir(a.assets):
        print(f"[ASSETS] 目录不存在: {a.assets}")
        problems.append("assets_missing")
    else:
        root = os.path.abspath(a.assets)
        print(f"[ASSETS] {root}")
        print(f"[LABEL] 目录名 = {os.path.basename(root)}")
        for fn in ("active.json", "manifest.json"):
            j = try_json(os.path.join(root, fn))
            if j:
                cv = j.get("catalog_version") if isinstance(j, dict) else None
                print(f"[LABEL] {fn}: catalog_version = {cv}")
                if cv and cv != EXPECTED_CATALOG:
                    print(f"      ⚠ 注意：目录标签为 {cv}，与 03b 不同；是否内置素材回退请按哈希判断")
        print("[LABEL] 提醒：fixture-face-03 同时是 APK 内置素材目录标签和 11-13 带扩展旧包名，按哈希认，不按标签认")

        report = try_json(os.path.join(root, "validation-report.json"))
        manifest = try_json(os.path.join(root, "manifest.json"))
        if manifest is None and report:
            manifest = report.get("manifest")

        actual = {}
        print("[FILES]")
        for name, (esize, esha) in EXPECTED_ASSETS.items():
            p = find_glb(root, name)
            if not p:
                print(f"  MISSING {name}")
                problems.append("missing_" + name)
                continue
            h = sha256(p)
            sz = os.path.getsize(p)
            ok = (h == esha and sz == esize)
            print(f"  {'OK    ' if ok else 'DIFFER '} {os.path.basename(p):20} {sz:>8} B  {h[:16]}…")
            if not ok:
                problems.append("hash_" + name)
            ex = bad_exts(p)
            print(f"        材质扩展: {', '.join(ex) if ex else '无（干净）'}")
            if ex:
                problems.append("ext_" + name)
            actual[name] = (sz, h)

        # 4. content_digest —— 用实算哈希替换报告声明值
        if report and manifest:
            declared = content_digest(manifest, report.get("assets", []))
            recomputed_assets = []
            for item in report.get("assets", []):
                rel = item.get("relative_file")
                p = os.path.join(root, rel) if rel else None
                if p and os.path.isfile(p):
                    recomputed_assets.append({"relative_file": rel, "sha256": sha256(p)})
                else:
                    recomputed_assets.append({"relative_file": rel, "sha256": item.get("sha256")})
            recomputed = content_digest(manifest, recomputed_assets)
            print(f"[DIGEST] 报告声明 = {declared}")
            print(f"[DIGEST] 实算重跑 = {recomputed}")
            print(f"[DIGEST] 基准 03b = {EXPECTED_DIGEST}")
            for tag, v in (("declared", declared), ("recomputed", recomputed)):
                if v != EXPECTED_DIGEST:
                    problems.append("digest_" + tag)
            if declared != recomputed:
                problems.append("digest_report_vs_file")
            lines.append(f"- content_digest：报告声明 `{declared[:16]}…`，实算重跑 `{recomputed[:16]}…`，基准 `{EXPECTED_DIGEST[:16]}…`")
        else:
            print("[DIGEST] 目录内缺 validation-report.json / manifest.json，跳过（候选库目录正常现象）")

        names = [f"{k} `{v[1][:16]}…`" for k, v in actual.items()]
        lines.append("- 实际文件哈希：" + "、".join(names) + ("（与 03b 基准一致）" if not any(p.startswith("hash_") for p in problems) else "（**有不一致**）"))
        lines.append(f"- 生效目录标签：`{os.path.basename(root)}` + manifest 的 catalog_version（两栏都报）")

    # 生成可贴文本
    text = "## 11/22 环境自检（脚本自动生成，人工补设备/时段）\n\n"
    text += "- 设备 / 系统：____（人工填）\n"
    text += "- 真机时段：____（人工填）\n"
    for ln in lines:
        text += ln + "\n"
    text += "- 材质扩展：三份 GLB 均无 `KHR_materials_specular` / `KHR_materials_sheen`\n" if not any(p.startswith("ext_") for p in problems) else "- 材质扩展：**检出残留，需处理**\n"
    text += "\n判定：脚本只核对文件与哈希，**不代替真机四视角外观结论**；两衣分别给通过/不通过/未测。\n"

    print("=" * 68)
    print(text)
    if a.issue_text:
        with open(a.issue_text, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"[OUT] 回执文本已写: {a.issue_text}")

    if problems:
        print("[RESULT] 不一致项: " + ", ".join(problems))
        return 1
    print("[RESULT] 全部一致（未提供 --apk/--assets 的项按跳过计）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
