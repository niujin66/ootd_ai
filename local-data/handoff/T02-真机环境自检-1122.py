# -*- coding: utf-8 -*-
"""T02 真机环境一键自检（C / A 11/22 用，自包含，无需仓库其他模块，只用标准库）。

用途：把 #13 回执里「能自动取到的实际值」一次跑出来，避免现场靠嘴报、
也避免把 `fixture-face-03` 目录标签误判成 03b。

检查项：
  1. APK SHA-256 是否等于 B 的 `c92c65f` 重打包值
  2. assets 目录三份 GLB 的实算哈希/体积是否等于 03b 基准
  3. 目录标签（basename / active.json / manifest.json）与 catalog_version
  4. content_digest：按 server/importer.py::_content_digest 算法，用**实算文件哈希**
     重算，并与报告声明值、基准值三方比对
  5. 三份 GLB 是否残留 KHR_materials_specular / KHR_materials_sheen
  6. **（11/22 新增）--adb-serial 分支**：把 @Journey668 #13 11:40 的现场提醒与
     #12 11:34 的同步四判据脚本化：
       a. `adb reverse --list` 是否含 tcp:8000（同步失败≠代码 bug）
       b. `active.json` 是否在 files/ootd-packages/ 根目录且 catalog_version = 03b
       c. 设备内 `sha256sum *.glb` 是否命中 03b 三文件
       d. 明确识别 11-18 早期设备版（981,412 / 671,544）并报「不是 03b，需先拉取目录」
       e. UI dump 抓到的版本字样只作人工确认提示，不自动判失败
     adb 不在 PATH / 设备未连时该项 SKIP，不影响其余检查。

用法：
    # PC 侧文件核对
    python T02-真机环境自检-1122.py --apk path/to/app-debug.apk --assets path/to/pkg
    python T02-真机环境自检-1122.py --assets path/to/pkg --issue-text out.md
    # 真机四判据（在现场、adb 已连上设备时）
    python T02-真机环境自检-1122.py --adb-serial fe474c80 --assets path/to/pkg --issue-text 回执.md

退出码：0 = 全部一致；1 = 有不一致（明细见输出）；2 = 参数缺失
"""
import argparse
import hashlib
import json
import os
import re
import struct
import subprocess
import sys

EXPECTED_APK = "d4402052545991ed93a3c335532f7b2d77d38700364817d6feaca2bd22ea863a"
EXPECTED_DIGEST = "f0c05a0d8b86a8bfbbb5b314692af66cc5468f252c957bae6e6490b4f7388ca0"
EXPECTED_CATALOG = "fixture-face-03b"
EXPECTED_ASSETS = {
    "person-001.glb": (419572, "f3dbe4c875dafb73d161d28de311b33dd839e8ec2fb559624c129a3a66012354"),
    "top-001_v2.glb": (981408, "45301ea0feead027300fe1d9b6847dc7a93381638a0d4996496d6d9d745d15b5"),
    "top-002_v1.glb": (671540, "8502d846b4beb443b6acfbd3c91d5ec97369a374dc19352a77657aa3758f7e82"),
}
# 设备上的文件名（无 _v2 / _v1 后缀）→ 期望 (size, sha256)
DEVICE_EXPECT = {
    "person-001.glb": (419572, EXPECTED_ASSETS["person-001.glb"][1]),
    "top-001.glb": (981408, EXPECTED_ASSETS["top-001_v2.glb"][1]),
    "top-002.glb": (671540, EXPECTED_ASSETS["top-002_v1.glb"][1]),
}
# B #12 10:34 实测：设备 filesDir 里曾存在的 11-18 早期远端版本（只公布哈希前 8 位）
EARLY_DEVICE = {
    "top-001.glb": (981412, "503172eb"),
    "top-002.glb": (671544, "ad07321c"),
}
# 11-13 带材质扩展的交付包（哈希前 8 位）
LEGACY_1113 = {
    "top-001.glb": (981688, "64bca93f"),
    "top-002.glb": (672096, "ceda31dc"),
}
STRIP_EXTS = ("KHR_materials_specular", "KHR_materials_sheen")
DEVICE_DIR = "files/ootd-packages/person-001/fixture-face-03b"
ACTIVE_JSON = "files/ootd-packages/active.json"


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


def run_cmd(args, timeout=20):
    """返回 (rc, stdout, stderr)；rc=None 表示命令不可用/超时。"""
    try:
        p = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=timeout, text=True, encoding="utf-8", errors="replace")
        return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()
    except FileNotFoundError:
        return None, "", "可执行文件不在 PATH（" + str(args[0]) + "）"
    except subprocess.TimeoutExpired:
        return None, "", "命令超时"
    except Exception as e:
        return None, "", type(e).__name__ + ": " + str(e)


def adb(serial, *tail, timeout=20):
    return run_cmd(["adb", "-s", serial] + list(tail), timeout=timeout)


def device_unavailable(err):
    e = (err or "").lower()
    return any(k in e for k in ("device not found", "no devices", "device offline",
                                "unauthorized", "more than one device", "device '(null)'"))


def check_adb(serial, problems, lines):
    """B #13 11:40 / #12 11:34 的四判据。返回是否实际执行了 adb 检查。"""
    print("-" * 68)
    print(f"[ADB] 设备 serial = {serial}")

    # a. adb reverse
    rc, out, err = adb(serial, "reverse", "--list")
    if rc is None or device_unavailable(err):
        why = err or "adb 不可用"
        print(f"[ADB] SKIP：{why}（其余检查照常）")
        lines.append(f"- adb 四判据：**SKIP**（{why}），现场请人工按执行单第 0 步核对")
        return False
    if rc != 0 and not out.strip():
        print(f"[ADB] SKIP：命令未成功（{err or '无输出'}），疑似设备未连（其余检查照常）")
        lines.append(f"- adb 四判据：**SKIP**（{err or '命令未成功'}），现场请人工按执行单第 0 步核对")
        return False
    has_rev = "tcp:8000" in (out or "")
    print(f"[ADB] reverse --list: {out or '(空)'}")
    print(f"      含 tcp:8000: {'YES' if has_rev else 'NO'}")
    if not has_rev:
        problems.append("adb_reverse_missing")
        print("      ⚠ 先 `adb reverse tcp:8000 tcp:8000` 然后再查一次；"
              "报 Failed to connect to /127.0.0.1:8000 不是代码 bug")
    lines.append(f"- `adb reverse --list`：{'含 `tcp:8000`（OK）' if has_rev else '**不含 tcp:8000，需重设再查**'}；原始输出 `{out or '(空)'}`")

    # b. active.json（注意：在 files/ootd-packages/ 根目录，不在 person-001/ 下）
    rc, out, err = adb(serial, "shell", f"run-as com.ootd.ai cat {ACTIVE_JSON}")
    active = None
    if rc == 0 and out.strip().startswith("{"):
        try:
            active = json.loads(out.strip().splitlines()[-1])
        except Exception:
            active = None
    cat_ok = bool(active) and active.get("catalog_version") == EXPECTED_CATALOG
    print(f"[ADB] active.json: {out.strip()[:160] or '(读不到)'}")
    print(f"      catalog_version = {EXPECTED_CATALOG}: {'YES' if cat_ok else 'NO'}")
    if not cat_ok:
        problems.append("adb_active_json")
        print("      ⚠ 指针未落到 03b：先点「拉取目录」同步；同步动作本身不写指针，包上屏后才写")
    lines.append(f"- `active.json`：{'`' + EXPECTED_CATALOG + '`（OK）' if cat_ok else '**未落到 03b / 读不到**'}；原文 `{out.strip()[:160] or '(空)'}`")

    # c. 设备内 sha256sum
    cmd = f"run-as com.ootd.ai sh -c 'cd {DEVICE_DIR} && sha256sum *.glb'"
    rc, out, err = adb(serial, "shell", cmd)
    parsed = {}
    for ln in (out or "").splitlines():
        m = re.match(r"^\s*([0-9a-fA-F]{64})\s+(\S+\.glb)\s*$", ln)
        if m:
            parsed[os.path.basename(m.group(2))] = m.group(1).lower()
    print("[ADB] sha256sum *.glb:")
    early_hit, legacy_hit = [], []
    for fn, (esize, esha) in DEVICE_EXPECT.items():
        h = parsed.get(fn)
        ok = (h == esha)
        print(f"  {'OK    ' if ok else 'DIFFER '} {fn:20} {h[:16] + '…' if h else '缺失/读不到'}")
        if h and not ok:
            if h.startswith(EARLY_DEVICE.get(fn, ("", "xxxx"))[1]):
                early_hit.append(fn)
            if h.startswith(LEGACY_1113.get(fn, ("", "xxxx"))[1]):
                legacy_hit.append(fn)
        if not ok:
            problems.append("adb_hash_" + fn)
    if early_hit:
        print("      ⚠ 命中 11-18 早期设备版（981,412 / 671,544）：**不是 03b，需先点「拉取目录」同步**")
        lines.append("- 设备文件：**命中 11-18 早期远端版本** `503172eb…` / `ad07321c…`，不是 03b，需先同步")
    if legacy_hit:
        print("      ⚠ 命中 11-13 带扩展旧包：不是 03b")
        lines.append("- 设备文件：**命中 11-13 带材质扩展旧包**，不是 03b")
    if not parsed:
        print(f"      （读不到哈希输出：{err[:120] or '目录可能不存在'}）")
    lines.append("- 设备三文件 sha256sum：" + (
        "、".join(f"`{fn}` `{parsed.get(fn, '缺失')[:16]}…`" for fn in DEVICE_EXPECT)
        + ("（与 03b 基准一致）" if not any(p.startswith("adb_hash_") for p in problems) else "（**有不一致**）")))

    # d. UI dump（仅信息项，不自动判失败）
    rc, out, err = adb(serial, "shell", "uiautomator dump /sdcard/ootd_window.xml", timeout=25)
    ui_text = ""
    if rc == 0:
        rc2, xml, _ = adb(serial, "shell", "cat /sdcard/ootd_window.xml", timeout=25)
        if rc2 == 0:
            # uiautomator 的文案在 text="..." 属性里，不能简单剥标签（会把内容一起删掉）
            texts = re.findall(r'text="([^"]*)"', xml or "")
            ui_text = " ".join(texts) if texts else (xml or "")
    hits = sorted(set(re.findall(r"fixture-face-03b?", ui_text)))
    print(f"[ADB] UI 版本字样: {', '.join(hits) if hits else '(未抓到)'}"
          + "  ← 人工确认顶栏与底部黄字两处都是 03b")
    lines.append(f"- UI 版本字样（信息项，请人工确认顶栏 `服务端 · fixture-face-03b · pose-001` 与底部黄字）：`{', '.join(hits) if hits else '未抓到'}`")
    if hits and "fixture-face-03b" not in hits:
        lines.append("- ⚠ UI 只见 `fixture-face-03`（少个 b）：**同步没生效，别继续核对**（若尚未进入试衣页，先进入再重跑本脚本）")
        problems.append("adb_ui_not_03b")
        print("      ⚠ UI 只见 fixture-face-03：按 B 的判据视为同步未生效，已计入不一致项")
    return True


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--apk", default=None, help="APK 文件路径")
    ap.add_argument("--assets", default=None, help="待检资产目录")
    ap.add_argument("--baseline", default=None, help="03b 基准目录（默认用内置期望值）")
    ap.add_argument("--adb-serial", default=None, help="真机 serial；提供后跑 adb 四判据")
    ap.add_argument("--issue-text", default=None, help="把可贴 Issue 的回执写到文件")
    a = ap.parse_args(argv)

    if not any((a.apk, a.assets, a.adb_serial)):
        ap.print_help()
        return 2

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
        print("[ASSETS] 未提供 --assets，只完成 APK / adb 检查")
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
                    print(f"      ⚠ 目录标签为 {cv}，与 03b 不同；请按哈希判断是内置素材 / 11-13 旧包 / 11-18 早期版")
        print("[LABEL] 提醒：fixture-face-03 标签下出现过三套内容，按哈希认，不按标签认")

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
                if h.startswith("503172eb") or h.startswith("ad07321c"):
                    print("        ⚠ 这是 11-18 早期设备版，不是 03b")
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

    # 6. adb 真机四判据
    if a.adb_serial:
        check_adb(a.adb_serial, problems, lines)

    # 生成可贴文本
    text = "## 11/22 环境自检（脚本自动生成，人工补设备/时段）\n\n"
    text += "- 设备 / 系统：____（人工填）\n"
    text += "- 真机时段：____（人工填）\n"
    for ln in lines:
        text += ln + "\n"
    text += "- 材质扩展：三份 GLB 均无 `KHR_materials_specular` / `KHR_materials_sheen`\n" if not any(p.startswith("ext_") for p in problems) else "- 材质扩展：**检出残留，需处理**\n"
    text += "\n判定：脚本只核对环境、文件与哈希，**不代替真机四视角外观结论**；两衣分别给通过/不通过/未测。\n"
    text += "同步四判据未全过时**先别开始核对**：`adb reverse --list` → `active.json` → `sha256sum` → 顶栏/黄字两处 `03b`。\n"

    print("=" * 68)
    print(text)
    if a.issue_text:
        with open(a.issue_text, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"[OUT] 回执文本已写: {a.issue_text}")

    if problems:
        print("[RESULT] 不一致项: " + ", ".join(problems))
        return 1
    print("[RESULT] 全部一致（未提供 --apk/--assets/--adb-serial 的项按跳过计）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
