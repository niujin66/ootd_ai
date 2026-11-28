# -*- coding: utf-8 -*-
"""从 garment-textured.glb 抽出 baseColor，修补全黑 texel，产出可当兜底贴图用的 albedo_2048.png。

用途：队长 11/26 晚方案里「卡壳超半天直接切模板套 albedo_2048.png 兜底」的那张图。
只做两件确定性的事：① 抽出原始贴图留档；② 把无照片命中的全黑区用邻域填充补上。
不做主观调色（棕带涂白需要人眼确认，另议）。
"""
import hashlib
import io
import json
import os
import struct

import numpy as np
from PIL import Image

GLB = r"D:\Desktop\软创\_work\.garment-src\garment-textured.glb"
OUT = r"D:\Desktop\软创\_work\.garment-build"
os.makedirs(OUT, exist_ok=True)


def load_glb(path):
    data = open(path, "rb").read()
    jlen, = struct.unpack_from("<I", data, 12)
    js = json.loads(data[20:20 + jlen].decode("utf-8"))
    off = 20 + jlen
    blen, _t = struct.unpack_from("<II", data, off)
    return js, data[off + 8:off + 8 + blen]


def img_bytes(js, binc, idx):
    bv = js["bufferViews"][js["images"][idx]["bufferView"]]
    o = bv.get("byteOffset", 0)
    return binc[o:o + bv["byteLength"]]


def inpaint_black(rgb, thr=12, passes=64):
    """把亮度低于 thr 的像素用邻域均值逐圈填上。返回 (新图, 掩膜, 迭代次数)。"""
    a = rgb.astype(np.float32)
    lum = a.mean(axis=2)
    hole = lum < thr
    filled = 0
    img = a.copy()
    mask = hole.copy()
    for it in range(passes):
        if not mask.any():
            break
        # 每一轮用上下左右四个邻居里“已知”的像素求均值
        acc = np.zeros_like(img)
        cnt = np.zeros(mask.shape, dtype=np.float32)
        for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            shifted = np.roll(img, (dy, dx), axis=(0, 1))
            smask = np.roll(~mask, (dy, dx), axis=(0, 1))
            acc += shifted * smask[..., None]
            cnt += smask.astype(np.float32)
        upd = mask & (cnt > 0)
        if upd.any():
            img[upd] = acc[upd] / cnt[upd][..., None]
            mask &= ~upd
            filled += int(upd.sum())
    return np.clip(img, 0, 255).astype(np.uint8), hole, filled


def main():
    js, binc = load_glb(GLB)
    print("== 材质贴图 ==")
    for i, im in enumerate(js.get("images", [])):
        raw = img_bytes(js, binc, i)
        p = Image.open(io.BytesIO(raw))
        print("  image[%d] %s %s %dx%d %.2f MB" % (
            i, im.get("mimeType"), im.get("name"), p.width, p.height, len(raw) / 1048576))

    base_raw = img_bytes(js, binc, 0)
    base = Image.open(io.BytesIO(base_raw)).convert("RGB")
    arr = np.asarray(base)
    print("\n== baseColor 现状 ==")
    lum = arr.astype(np.float32).mean(axis=2)
    black = lum < 12
    print("  尺寸 %dx%d 格式 %s" % (base.width, base.height, base.mode))
    print("  全黑像素 %d / %d = %.2f%%" % (black.sum(), black.size, 100.0 * black.mean()))
    print("  亮度分位 1/5/50/95 = %s" % np.round(np.percentile(lum, [1, 5, 50, 95]), 1))

    raw_path = os.path.join(OUT, "albedo_2048_raw.png")
    base.save(raw_path)
    print("  原始留档 -> %s" % os.path.basename(raw_path))

    fixed, hole, filled = inpaint_black(arr)
    lum2 = fixed.astype(np.float32).mean(axis=2)
    print("\n== 修补后 ==")
    print("  填补像素 %d，残留全黑 %d (%.3f%%)" % (filled, (lum2 < 12).sum(), 100.0 * (lum2 < 12).mean()))
    print("  亮度分位 1/5/50/95 = %s" % np.round(np.percentile(lum2, [1, 5, 50, 95]), 1))
    print("  均值颜色 RGB = %s" % np.round(fixed.reshape(-1, 3).mean(axis=0), 1))

    out_path = os.path.join(OUT, "albedo_2048.png")
    Image.fromarray(fixed).save(out_path, optimize=True)
    print("  修补版 -> %s (%.2f MB)" % (os.path.basename(out_path), os.path.getsize(out_path) / 1048576))

    # 也导一份法线贴图备用
    nrm = Image.open(io.BytesIO(img_bytes(js, binc, 1)))
    npath = os.path.join(OUT, "normal_2048.png")
    nrm.save(npath)
    print("  法线 -> %s (%.2f MB, %dx%d)" % (os.path.basename(npath),
                                          os.path.getsize(npath) / 1048576, nrm.width, nrm.height))

    rep = {
        "source": os.path.basename(GLB),
        "source_sha256": hashlib.sha256(open(GLB, "rb").read()).hexdigest(),
        "albedo_sha256": hashlib.sha256(open(out_path, "rb").read()).hexdigest(),
        "size": [base.width, base.height],
        "black_ratio_before": round(100.0 * black.mean(), 2),
        "black_ratio_after": round(100.0 * (lum2 < 12).mean(), 3),
        "filled_pixels": filled,
        "mean_rgb_after": [round(float(v), 1) for v in fixed.reshape(-1, 3).mean(axis=0)],
    }
    with open(os.path.join(OUT, "albedo-report.json"), "w", encoding="utf-8") as f:
        json.dump(rep, f, ensure_ascii=False, indent=2)
    print("\nDONE")
    print(json.dumps(rep, ensure_ascii=False, indent=2))


main()