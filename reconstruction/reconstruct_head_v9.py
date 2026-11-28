"""Zero-hardcode general V9 3D head reconstruction pipeline (local open-source AI-assisted Method 2).

Supports any 360-degree portrait video (--video) or extracted image folder (--images-dir) and
produces a mobile-ready 2048px UV + tangent-space normal-mapped GLB under --output-dir without
relying on external cloud 3D generators or personal hardcoded paths.
"""
from __future__ import annotations

import itertools
import json
import math
import os
import shutil
import sys
import time
from argparse import ArgumentParser, Namespace
from pathlib import Path
from random import randint

import cv2
import numpy as np
import open3d as o3d
import pycolmap
import scipy.sparse as sp
import torch
import torch.nn.functional as F
import transformers
import trimesh
import xatlas
from PIL import Image, ImageDraw, ImageFilter, ImageFont
from scipy.spatial import cKDTree
from tqdm import tqdm
from trimesh.visual.material import PBRMaterial

ALL_STAGES = ("extract", "priors", "sfm", "train", "mesh", "bake", "preview")
SEGFORMER_ID = "mattmdjaga/segformer_b2_clothes"
DA2_ID = "depth-anything/Depth-Anything-V2-Base-hf"
LIGHTGLUE_ID = "ETH-CVG/lightglue_superpoint"
SAPIENS_ID = "facebook/sapiens2-normal-0.4b"

DEFAULT_2DGS_REPO = os.environ.get("OOTD_2DGS_REPO", r"D:\gsscan\vendor\2d-gaussian-splatting")
DEFAULT_EVAL_DIR = os.environ.get("OOTD_GSSCAN_EVAL_DIR", r"D:\gsscan\src\eval")
REPO_ROOT = Path(__file__).resolve().parent.parent
SERVER_TOOLS_DIR = REPO_ROOT / "server" / "tools"
if str(SERVER_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_TOOLS_DIR))
from export_glb_guard import find_extensions, read_glb, strip_file  # noqa: E402


def _inject_external_paths(gs2d_repo: str, eval_dir: str) -> None:
    for p in (gs2d_repo, eval_dir):
        if p and Path(p).exists() and p not in sys.path:
            sys.path.insert(0, p)


def _load_font(size: int):
    for p in (r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\simhei.ttf", r"C:\Windows\Fonts\arial.ttf"):
        if Path(p).exists():
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                pass
    return ImageFont.load_default()


# ---------------------------------------------------------------------------
# Stage 1: Video / Image Extraction & Quality Filtering
# ---------------------------------------------------------------------------
def stage_extract(args, out_dir: Path) -> Path:
    raw_dir = out_dir / "raw_frames"
    raw_dir.mkdir(parents=True, exist_ok=True)
    cand_dir = out_dir / "_cand_frames"
    if cand_dir.exists():
        shutil.rmtree(cand_dir)
    cand_dir.mkdir(parents=True, exist_ok=True)

    candidates: list[Path] = []
    if args.video:
        cap = cv2.VideoCapture(str(args.video))
        if not cap.isOpened():
            raise RuntimeError(f"无法打开输入视频: {args.video}")
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        start_idx = min(max(0, int(args.skip_head_sec * fps)), max(0, total - 1))
        end_idx = max(start_idx + 1, total - int(args.skip_tail_sec * fps))
        n_cand = min(end_idx - start_idx, max(args.target_frames + 25, int(args.target_frames * 1.35)))
        indices = np.linspace(start_idx, end_idx - 1, num=n_cand, dtype=int)
        for k, fidx in enumerate(np.unique(indices)):
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(fidx))
            ok, bgr = cap.read()
            if not ok or bgr is None:
                continue
            p = cand_dir / f"cand-{k:05d}.jpg"
            cv2.imwrite(str(p), bgr, [cv2.IMWRITE_JPEG_QUALITY, 96])
            candidates.append(p)
        cap.release()
    elif args.images_dir:
        src_imgs = sorted(
            [p for p in Path(args.images_dir).iterdir() if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}]
        )
        if not src_imgs:
            raise RuntimeError(f"输入图片目录为空: {args.images_dir}")
        for k, sp in enumerate(src_imgs):
            bgr = cv2.imread(str(sp), cv2.IMREAD_COLOR)
            if bgr is None:
                continue
            p = cand_dir / f"cand-{k:05d}.jpg"
            cv2.imwrite(str(p), bgr, [cv2.IMWRITE_JPEG_QUALITY, 96])
            candidates.append(p)
    else:
        raise ValueError("必须提供 --video 或 --images-dir")

    print(f"[Extract] 评估 {len(candidates)} 帧候选画面（过滤无人头与严重运动模糊帧）...", flush=True)
    seg_proc = transformers.AutoImageProcessor.from_pretrained(SEGFORMER_ID)
    seg_model = transformers.SegformerForSemanticSegmentation.from_pretrained(SEGFORMER_ID).eval().cuda()

    scored = []
    for p in candidates:
        pil = Image.open(p).convert("RGB")
        W, H = pil.size
        inp = seg_proc(images=pil, return_tensors="pt").to("cuda")
        with torch.no_grad():
            logits = seg_model(**inp).logits
        sem = F.interpolate(logits, size=(H, W), mode="bilinear", align_corners=False).argmax(dim=1)[0].cpu().numpy()
        head_m = (sem == 2) | (sem == 11)
        head_ratio = float(head_m.mean())
        sharp = 0.0
        if head_ratio >= 0.015:
            ys, xs = np.nonzero(head_m)
            gray = cv2.cvtColor(cv2.imread(str(p), cv2.IMREAD_COLOR), cv2.COLOR_BGR2GRAY)
            roi = gray[int(ys.min()) : int(ys.max()) + 1, int(xs.min()) : int(xs.max()) + 1]
            sharp = float(cv2.Laplacian(roi, cv2.CV_32F).var())
        scored.append((p, head_ratio, sharp))

    del seg_model
    torch.cuda.empty_cache()

    valid = [item for item in scored if item[1] >= 0.015]
    if not valid:
        raise RuntimeError("未检测到有效人头前景帧，请检查输入视频主体是否在画面内。")
    med_sharp = float(np.median([x[2] for x in valid]))
    crisp = [item for item in valid if item[2] >= 0.25 * med_sharp]
    if len(crisp) < min(30, len(valid)):
        crisp = valid

    if len(crisp) > args.target_frames:
        sel_idx = np.linspace(0, len(crisp) - 1, num=args.target_frames, dtype=int)
        chosen = [crisp[i] for i in sel_idx]
    else:
        chosen = crisp

    for existing in raw_dir.glob("*.jpg"):
        existing.unlink()
    for i, (src_p, _, _) in enumerate(chosen):
        shutil.copy2(src_p, raw_dir / f"frame-{i:05d}.jpg")
    shutil.rmtree(cand_dir, ignore_errors=True)

    meta = {
        "candidate_frames": len(candidates),
        "valid_head_frames": len(valid),
        "selected_frames": len(chosen),
        "median_head_sharpness": round(med_sharp, 2),
    }
    with open(out_dir / "extract_meta.json", "w", encoding="utf-8") as fh:
        json.dump(meta, fh, ensure_ascii=False, indent=2)
    print(f"[Extract] 最终保留 {len(chosen)} 帧清晰主体画面 -> {raw_dir}", flush=True)
    return raw_dir


# ---------------------------------------------------------------------------
# Stage 2: SegFormer + Depth-Anything-V2 + Sapiens2 + Auto Frontal/Eye Anchors
# ---------------------------------------------------------------------------
def _da2_disp_to_opencv_normals(disp: np.ndarray, fx: float, fy: float, cx: float, cy: float) -> np.ndarray:
    H, W = disp.shape
    z = 1.0 / np.maximum(disp.astype(np.float64), 0.05)
    z = cv2.GaussianBlur(z, (0, 0), sigmaX=1.5)
    xs, ys = np.meshgrid(np.arange(W, dtype=np.float64), np.arange(H, dtype=np.float64))
    P = np.stack([(xs - cx) / fx * z, (ys - cy) / fy * z, z], axis=-1)
    dx_v = P[2:, 1:-1] - P[:-2, 1:-1]
    dy_u = P[1:-1, 2:] - P[1:-1, :-2]
    n = np.cross(dx_v, dy_u, axis=-1)
    n /= np.maximum(np.linalg.norm(n, axis=-1, keepdims=True), 1e-8)
    out = np.zeros((3, H, W), dtype=np.float32)
    out[:, 1:-1, 1:-1] = n.transpose(2, 0, 1).astype(np.float32)
    return out


def _predict_sapiens_cropped(img_pil: Image.Image, fg_mask: np.ndarray, proc, model) -> np.ndarray:
    W, H = img_pil.size
    ys, xs = np.nonzero(fg_mask > 127)
    if len(ys) < 100:
        x0, y0, x1, y1 = 0, 0, W, H
    else:
        bx0, bx1 = int(xs.min()), int(xs.max()) + 1
        by0, by1 = int(ys.min()), int(ys.max()) + 1
        pw, ph = int((bx1 - bx0) * 0.15), int((by1 - by0) * 0.15)
        x0, x1 = max(0, bx0 - pw), min(W, bx1 + pw)
        y0, y1 = max(0, by0 - ph), min(H, by1 + ph)
    cw, ch = x1 - x0, y1 - y0
    inp = proc(images=[img_pil.crop((x0, y0, x1, y1))], return_tensors="pt").to("cuda", dtype=torch.bfloat16)
    with torch.no_grad():
        out = model(**inp)
    post = proc.post_process_normal_estimation(out, source_sizes=[(ch, cw)])
    n_crop = F.normalize(post[0]["normals"].float(), p=2, dim=0, eps=1e-8).cpu().numpy()
    canvas = np.zeros((3, H, W), dtype=np.float32)
    canvas[2, :, :] = -1.0
    canvas[:, y0:y1, x0:x1] = n_crop
    return canvas


def auto_detect_frontal_and_eyes(
    images_dir: Path,
    sem_dir: Path,
    sap_normals_dir: Path | None,
) -> dict:
    """Automatically select clearest most-frontal reference frame & locate left/right pupils without extra deps."""
    stems = sorted(p.stem for p in images_dir.glob("*.jpg"))
    candidates = []
    for stem in stems:
        sem = np.asarray(Image.open(sem_dir / f"{stem}.png").convert("L"), dtype=np.uint8)
        face_m = sem == 11
        face_area = int(face_m.sum())
        if face_area < 2000:
            continue
        ys, xs = np.nonzero(face_m)
        x0, x1, y0, y1 = int(xs.min()), int(xs.max()) + 1, int(ys.min()), int(ys.max()) + 1
        fw, fh = max(1, x1 - x0), max(1, y1 - y0)
        cx = 0.5 * (x0 + x1)
        left_px = int((xs < cx).sum())
        right_px = int((xs >= cx).sum())
        sym = 1.0 - abs(left_px - right_px) / max(1, face_area)

        bgr = cv2.imread(str(images_dir / f"{stem}.jpg"), cv2.IMREAD_COLOR)
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        sharp = float(cv2.Laplacian(gray[y0:y1, x0:x1], cv2.CV_32F).var())

        frontal_score = 1.0
        if sap_normals_dir is not None and (sap_normals_dir / f"{stem}.npy").exists():
            n_cam = np.load(sap_normals_dir / f"{stem}.npy").astype(np.float32)
            mean_n = n_cam[:, face_m].mean(axis=1)
            # Facing camera means -n_z is high and |n_x| is near 0
            frontal_score = float(max(0.0, -mean_n[2]) * max(0.0, 1.0 - 2.2 * abs(mean_n[0])))

        # Combined score favors straight frontal symmetry, high Sapiens frontal normal & crisp eyes
        score = frontal_score * (sym ** 2) * math.sqrt(max(sharp, 1.0)) * math.sqrt(face_area)
        candidates.append((score, stem, (x0, y0, x1, y1), sharp, sym, frontal_score))

    if not candidates:
        raise RuntimeError("未在输入帧中检测到足够清晰的正脸区域，无法自动选定锚定帧。")
    candidates.sort(key=lambda x: -x[0])
    _, ref_stem, (x0, y0, x1, y1), ref_sharp, ref_sym, ref_front = candidates[0]
    hero_eye_stems = [c[1] for c in candidates[:2]]

    # Locate viewer-left (EyeR) and viewer-right (EyeL) pupils in the anatomical eye band
    bgr = cv2.imread(str(images_dir / f"{ref_stem}.jpg"), cv2.IMREAD_COLOR)
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY).astype(np.float32)
    sem_ref = np.asarray(Image.open(sem_dir / f"{ref_stem}.png").convert("L"), dtype=np.uint8)
    face_m = sem_ref == 11
    ys_f, xs_f = np.nonzero(face_m)
    fw, fh = float(x1 - x0), float(y1 - y0)
    cx_face = float(np.median(xs_f))

    band_y0 = int(y0 + 0.12 * fh)
    band_y1 = int(y0 + 0.37 * fh)
    # Exclude hair-skin boundary artifacts by eroding Face mask slightly
    face_inner = cv2.erode(face_m.astype(np.uint8), np.ones((9, 9), np.uint8)) > 0
    blur_g = cv2.GaussianBlur(gray, (0, 0), sigmaX=2.5)
    local_bg = cv2.GaussianBlur(gray, (0, 0), sigmaX=14.0)
    # Dark iris stands out against periocular skin while strictly staying inside inner face band above nostrils
    darkness = np.clip(local_bg - blur_g, 0.0, 80.0) * (1.0 - np.clip(blur_g / 180.0, 0.0, 0.7))
    band_mask = np.zeros_like(gray, dtype=bool)
    band_mask[band_y0:band_y1, x0:x1] = True
    band_mask &= face_inner & (sem_ref != 2)

    xs_grid = np.arange(gray.shape[1])[None, :]

    def _pick_eye(side_mask: np.ndarray, fb_u: float, fb_v: float) -> tuple[float, float]:
        m = band_mask & side_mask
        if int(m.sum()) < 40:
            return fb_u, fb_v
        score_map = darkness * m.astype(np.float32)
        vy, vx = np.unravel_index(int(np.argmax(score_map)), score_map.shape)
        if score_map[vy, vx] < 2.5:
            return fb_u, fb_v
        return float(vx), float(vy)

    uR, vR = _pick_eye(
        (xs_grid >= int(cx_face - 0.36 * fw)) & (xs_grid <= int(cx_face - 0.12 * fw)),
        cx_face - 0.21 * fw,
        y0 + 0.25 * fh,
    )
    uL, vL = _pick_eye(
        (xs_grid >= int(cx_face + 0.12 * fw)) & (xs_grid <= int(cx_face + 0.36 * fw)),
        cx_face + 0.21 * fw,
        y0 + 0.25 * fh,
    )

    # Anatomical sanity fallback if brows/shadows skewed one side vertically
    if abs(vR - vL) > 0.10 * fh or not (0.26 * fw <= (uL - uR) <= 0.68 * fw):
        v_mid = float(y0 + 0.25 * fh)
        uR, vR = cx_face - 0.21 * fw, v_mid
        uL, vL = cx_face + 0.21 * fw, v_mid

    rx = max(24.0, 0.19 * fw)
    ry = max(18.0, 0.12 * fh)
    return {
        "ref_stem": ref_stem,
        "hero_eye_stems": hero_eye_stems,
        "face_bbox_xyxy": [x0, y0, x1, y1],
        "pupils": {"EyeR": [round(uR, 1), round(vR, 1)], "EyeL": [round(uL, 1), round(vL, 1)]},
        "eye_radii": [round(rx, 1), round(ry, 1)],
        "ref_sharpness": round(ref_sharp, 2),
        "ref_symmetry": round(ref_sym, 4),
        "ref_frontal_normal_score": round(ref_front, 4),
    }


def stage_priors(args, images_dir: Path, out_dir: Path) -> Path:
    priors_dir = out_dir / "priors"
    sem_dir = priors_dir / "sem"
    fg_dir = priors_dir / "fg_masks"
    disp_dir = priors_dir / "disp"
    sap_dir = priors_dir / "normals_sapiens"
    for d in (sem_dir, fg_dir, disp_dir):
        d.mkdir(parents=True, exist_ok=True)

    stems = sorted(p.stem for p in images_dir.glob("*.jpg"))
    print(f"[Priors 1/3] 运行 SegFormer 语义与前景分割 ({len(stems)} 帧)...", flush=True)
    seg_proc = transformers.AutoImageProcessor.from_pretrained(SEGFORMER_ID)
    seg_model = transformers.SegformerForSemanticSegmentation.from_pretrained(SEGFORMER_ID).eval().cuda()
    for stem in stems:
        pil = Image.open(images_dir / f"{stem}.jpg").convert("RGB")
        W, H = pil.size
        inp = seg_proc(images=pil, return_tensors="pt").to("cuda")
        with torch.no_grad():
            logits = seg_model(**inp).logits
        sem = F.interpolate(logits, size=(H, W), mode="bilinear", align_corners=False).argmax(dim=1)[0].cpu().numpy().astype(np.uint8)
        fg = np.isin(sem, (2, 4, 11)).astype(np.uint8) * 255
        # Keep largest connected foreground component to reject room blobs
        num_cc, labels, stats, _ = cv2.connectedComponentsWithStats(fg, connectivity=8)
        if num_cc > 1:
            largest = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
            fg = np.where(labels == largest, 255, 0).astype(np.uint8)
        Image.fromarray(sem, mode="L").save(sem_dir / f"{stem}.png")
        Image.fromarray(fg, mode="L").save(fg_dir / f"{stem}.png")
    del seg_model
    torch.cuda.empty_cache()

    print(f"[Priors 2/3] 运行 Depth-Anything-V2 单目视差估计 ({len(stems)} 帧)...", flush=True)
    da_proc = transformers.AutoImageProcessor.from_pretrained(DA2_ID)
    da_model = transformers.AutoModelForDepthEstimation.from_pretrained(DA2_ID).eval().cuda()
    for stem in stems:
        pil = Image.open(images_dir / f"{stem}.jpg").convert("RGB")
        W, H = pil.size
        inp = da_proc(images=pil, return_tensors="pt").to("cuda")
        with torch.no_grad():
            pred = da_model(**inp).predicted_depth
        disp = F.interpolate(pred.unsqueeze(1), size=(H, W), mode="bicubic", align_corners=False).squeeze().cpu().numpy()
        d_min, d_max = float(disp.min()), float(disp.max())
        disp_norm = ((disp - d_min) / max(d_max - d_min, 1e-6)).astype(np.float32)
        np.save(disp_dir / f"{stem}.npy", disp_norm)
    del da_model
    torch.cuda.empty_cache()

    use_sap_dir: Path | None = None
    if not args.no_sapiens:
        sap_dir.mkdir(parents=True, exist_ok=True)
        print(f"[Priors 3/3] 运行 Sapiens2-Normal-0.4B 人体表面法线先验 ({len(stems)} 帧)...", flush=True)
        sap_proc = transformers.AutoImageProcessor.from_pretrained(SAPIENS_ID)
        sap_model = transformers.Sapiens2ForNormalEstimation.from_pretrained(SAPIENS_ID, torch_dtype=torch.bfloat16).eval().cuda()

        # Select up to 5 high-face-area frames dynamically for OpenCV axis sign calibration
        face_ranks = []
        for stem in stems:
            sem = np.asarray(Image.open(sem_dir / f"{stem}.png").convert("L"), dtype=np.uint8)
            area = int((sem == 11).sum())
            if area > 2000:
                face_ranks.append((area, stem))
        face_ranks.sort(reverse=True)
        calib_stems = [s for _, s in face_ranks[:5]]
        if not calib_stems:
            raise RuntimeError("未找到足够面积的面部帧以自动对齐 Sapiens2 法线坐标系。")

        sample_im = Image.open(images_dir / f"{stems[0]}.jpg")
        W, H = sample_im.size
        fx_est = fy_est = 1.2 * float(max(W, H))
        cx_est, cy_est = 0.5 * W, 0.5 * H

        calib_pairs = []
        for stem in calib_stems:
            img = Image.open(images_dir / f"{stem}.jpg").convert("RGB")
            fg = np.asarray(Image.open(fg_dir / f"{stem}.png").convert("L"), dtype=np.uint8)
            sem = np.asarray(Image.open(sem_dir / f"{stem}.png").convert("L"), dtype=np.uint8)
            disp = np.load(disp_dir / f"{stem}.npy")
            n_sap = _predict_sapiens_cropped(img, fg, sap_proc, sap_model)
            n_da2 = _da2_disp_to_opencv_normals(disp, fx_est, fy_est, cx_est, cy_est)
            fm = (sem == 11) & (np.linalg.norm(n_da2, axis=0) > 0.5)
            calib_pairs.append((n_sap[:, fm], n_da2[:, fm]))

        best_score = -999.0
        best_perm = (0, 1, 2)
        best_signs = (1, 1, 1)
        for perm in itertools.permutations((0, 1, 2)):
            for signs in itertools.product((-1, 1), repeat=3):
                c_list = []
                for ns, nd in calib_pairs:
                    nt = np.stack([signs[k] * ns[perm[k]] for k in range(3)], axis=0)
                    nt /= np.maximum(np.linalg.norm(nt, axis=0, keepdims=True), 1e-8)
                    c_list.append(float((nt * nd).sum(axis=0).mean()))
                sc = float(np.mean(c_list))
                if sc > best_score:
                    best_score, best_perm, best_signs = sc, perm, signs

        print(f"[Priors 3/3] Sapiens2 坐标轴自动校准: perm={best_perm}, signs={best_signs}, cos={best_score:.4f}", flush=True)
        for stem in stems:
            img = Image.open(images_dir / f"{stem}.jpg").convert("RGB")
            fg = np.asarray(Image.open(fg_dir / f"{stem}.png").convert("L"), dtype=np.uint8)
            n_raw = _predict_sapiens_cropped(img, fg, sap_proc, sap_model)
            n_al = np.stack([best_signs[k] * n_raw[best_perm[k]] for k in range(3)], axis=0)
            n_al /= np.maximum(np.linalg.norm(n_al, axis=0, keepdims=True), 1e-8)
            np.save(sap_dir / f"{stem}.npy", n_al.astype(np.float16))

        del sap_model
        torch.cuda.empty_cache()
        use_sap_dir = sap_dir
        with open(priors_dir / "normals_meta.json", "w", encoding="utf-8") as fh:
            json.dump({"model": SAPIENS_ID, "perm": list(best_perm), "signs": list(best_signs), "mean_face_cos": round(best_score, 4)}, fh, indent=2)

    anchors = auto_detect_frontal_and_eyes(images_dir, sem_dir, use_sap_dir)
    with open(priors_dir / "auto_anchors.json", "w", encoding="utf-8") as fh:
        json.dump(anchors, fh, ensure_ascii=False, indent=2)
    print(f"[Priors] 自动选定锚定帧={anchors['ref_stem']}, 英雄眼帧={anchors['hero_eye_stems']}, 双瞳={anchors['pupils']}", flush=True)
    return priors_dir


# ---------------------------------------------------------------------------
# Stage 3: Foreground-Masked SuperPoint+LightGlue SfM + Auto Undistortion
# ---------------------------------------------------------------------------
def stage_sfm(args, raw_images_dir: Path, priors_dir: Path, out_dir: Path) -> Path:
    data_fg = out_dir / "2dgs_data_fg"
    fg_masks_dir = priors_dir / "fg_masks"
    if data_fg.exists():
        shutil.rmtree(data_fg)
    sfm_imgs_dir = out_dir / "_sfm_masked_imgs"
    if sfm_imgs_dir.exists():
        shutil.rmtree(sfm_imgs_dir)
    sfm_imgs_dir.mkdir(parents=True, exist_ok=True)
    data_fg.mkdir(parents=True, exist_ok=True)

    stems = sorted(p.stem for p in raw_images_dir.glob("*.jpg"))
    kernel_dil3 = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7))
    kp_masks: dict[str, np.ndarray] = {}
    W = H = 0
    for stem in stems:
        bgr = cv2.imread(str(raw_images_dir / f"{stem}.jpg"), cv2.IMREAD_COLOR)
        H, W = bgr.shape[:2]
        m_u8 = np.asarray(Image.open(fg_masks_dir / f"{stem}.png").convert("L"), dtype=np.uint8)
        m_bin = (m_u8 > 127).astype(np.uint8) * 255
        m_dil3 = cv2.dilate(m_bin, kernel_dil3)
        # Keep strict un-bloated mask for photometric image to prevent background rim bleed
        bgr_sfm = np.where(m_dil3[:, :, None] > 127, bgr, 0)
        cv2.imwrite(str(sfm_imgs_dir / f"{stem}.jpg"), bgr_sfm, [cv2.IMWRITE_JPEG_QUALITY, 96])
        kp_masks[stem] = m_dil3 > 127

    db_path = out_dir / "_sfm.db"
    if db_path.exists():
        db_path.unlink()
    reader_opts = pycolmap.ImageReaderOptions()
    pinhole_params = None
    if args.cameras_bin:
        orig_rec = pycolmap.Reconstruction(str(Path(args.cameras_bin).parent))
        orig_cam = next(iter(orig_rec.cameras.values()))
        pinhole_params = [float(x) for x in orig_cam.params]
        reader_opts.camera_model = "PINHOLE"
        reader_opts.camera_params = ",".join(f"{x:.10f}" for x in pinhole_params)
    else:
        reader_opts.camera_model = "SIMPLE_RADIAL"
        reader_opts.camera_params = f"{1.2 * max(W, H):.6f},{0.5 * W:.2f},{0.5 * H:.2f},0.0"

    ext_opts = pycolmap.FeatureExtractionOptions()
    ext_opts.sift.max_num_features = 512
    pycolmap.extract_features(str(db_path), str(sfm_imgs_dir), camera_mode=pycolmap.CameraMode.SINGLE, reader_options=reader_opts, extraction_options=ext_opts)

    lg_model = transformers.LightGlueForKeypointMatching.from_pretrained(LIGHTGLUE_ID).eval().cuda()
    lg_model.depth_confidence = -1.0
    lg_model.width_confidence = -1.0

    kp_cache: dict[str, tuple[torch.Tensor, torch.Tensor]] = {}
    kp_counts: dict[str, int] = {}
    max_kpts = 0
    for stem in stems:
        img = cv2.imread(str(sfm_imgs_dir / f"{stem}.jpg"), cv2.IMREAD_COLOR)[:, :, ::-1].astype(np.float32) / 255.0
        gray = 0.299 * img[:, :, 0] + 0.587 * img[:, :, 1] + 0.114 * img[:, :, 2]
        t_in = torch.from_numpy(gray)[None, None].repeat(1, 3, 1, 1).cuda()
        with torch.no_grad():
            kp_norm, scores, desc, det_mask = lg_model.keypoint_detector(t_in)[:4]
        valid = det_mask[0] > 0
        kpts_xy = kp_norm[0][valid].clone()
        kpts_xy[:, 0] *= float(W)
        kpts_xy[:, 1] *= float(H)
        sc, ds = scores[0][valid], desc[0][valid]
        ui = torch.clamp(torch.round(kpts_xy[:, 0]).long(), 0, W - 1).cpu().numpy()
        vi = torch.clamp(torch.round(kpts_xy[:, 1]).long(), 0, H - 1).cpu().numpy()
        fg_keep = torch.from_numpy(kp_masks[stem][vi, ui]).to(kpts_xy.device)
        kpts_xy, ds, sc = kpts_xy[fg_keep], ds[fg_keep], sc[fg_keep]
        if len(kpts_xy) > 2048:
            top = torch.topk(sc, 2048).indices
            kpts_xy, ds = kpts_xy[top], ds[top]
        kp_cache[stem] = (kpts_xy.contiguous(), ds.contiguous())
        kp_counts[stem] = int(len(kpts_xy))
        max_kpts = max(max_kpts, int(len(kpts_xy)))

    with pycolmap.Database.open(str(db_path)) as db:
        imgs_db = db.read_all_images()
        name_to_id = {Path(im.name).stem: int(im.image_id) for im in imgs_db}
        if pinhole_params is not None:
            for cam in db.read_all_cameras():
                cam.params = np.array(pinhole_params, dtype=np.float64)
                db.update_camera(cam)
        db.clear_keypoints()
        db.clear_descriptors()
        db.clear_matches()
        db.clear_two_view_geometries()
        for stem in stems:
            db.write_keypoints(name_to_id[stem], kp_cache[stem][0].cpu().numpy().astype(np.float32))

    N = len(stems)
    kp_pad = torch.zeros((N, max_kpts, 2), dtype=torch.float32, device="cuda")
    desc_pad = torch.zeros((N, max_kpts, 256), dtype=torch.float32, device="cuda")
    mask_pad = torch.zeros((N, max_kpts), dtype=torch.int64, device="cuda")
    for idx, stem in enumerate(stems):
        kt, dt = kp_cache[stem]
        if len(kt) > 0:
            kp_pad[idx, : len(kt)] = kt
            desc_pad[idx, : len(kt)] = dt
            mask_pad[idx, : len(kt)] = 1

    pairs = [(i, j) for i in range(N) for j in range(i + 1, N)]
    kept_pairs: list[tuple[str, str]] = []
    with pycolmap.Database.open(str(db_path)) as db:
        for start in range(0, len(pairs), 32):
            chunk = pairs[start : start + 32]
            ii = torch.tensor([p[0] for p in chunk], device="cuda", dtype=torch.long)
            jj = torch.tensor([p[1] for p in chunk], device="cuda", dtype=torch.long)
            with torch.no_grad():
                m_out, s_out, _, _, _ = lg_model._match_image_pair(
                    torch.stack([kp_pad[ii], kp_pad[jj]], dim=1),
                    torch.stack([desc_pad[ii], desc_pad[jj]], dim=1),
                    H,
                    W,
                    mask=torch.stack([mask_pad[ii], mask_pad[jj]], dim=1),
                )
            B_c = len(chunk)
            m0 = m_out.reshape(B_c, 2, max_kpts)[:, 0].cpu().numpy()
            sc0 = s_out.reshape(B_c, 2, max_kpts)[:, 0].cpu().numpy()
            for b, (ia, ib) in enumerate(chunk):
                na, nb = kp_counts[stems[ia]], kp_counts[stems[ib]]
                ma, sa = m0[b, :na], sc0[b, :na]
                v = (ma >= 0) & (ma < nb) & (sa >= 0.15)
                if int(v.sum()) >= 15:
                    idx_a = np.nonzero(v)[0].astype(np.uint32)
                    idx_b = ma[v].astype(np.uint32)
                    mu32 = np.stack([idx_a, idx_b], axis=1)
                    ida, idb = name_to_id[stems[ia]], name_to_id[stems[ib]]
                    if ida > idb:
                        ida, idb = idb, ida
                        mu32 = mu32[:, ::-1].copy()
                    db.write_matches(ida, idb, mu32)
                    kept_pairs.append((f"{stems[ia]}.jpg", f"{stems[ib]}.jpg"))

    del lg_model
    torch.cuda.empty_cache()

    pairs_path = out_dir / "_pairs.txt"
    with open(pairs_path, "w", encoding="utf-8") as fh:
        for a, b in kept_pairs:
            fh.write(f"{a} {b}\n")
    pycolmap.verify_matches(str(db_path), str(pairs_path))

    sparse_raw = out_dir / "_sparse_raw"
    if sparse_raw.exists():
        shutil.rmtree(sparse_raw)
    sparse_raw.mkdir(parents=True, exist_ok=True)

    inc_opts = pycolmap.IncrementalPipelineOptions()
    lock_intr = pinhole_params is not None
    inc_opts.ba_refine_focal_length = not lock_intr
    inc_opts.ba_refine_principal_point = False
    inc_opts.ba_refine_extra_params = not lock_intr
    inc_opts.mapper.init_min_num_inliers = 50
    inc_opts.mapper.abs_pose_min_num_inliers = 20

    recs = pycolmap.incremental_mapping(str(db_path), str(sfm_imgs_dir), str(sparse_raw), options=inc_opts)
    if not recs:
        raise RuntimeError("前景 SfM 稀疏重建失败（0 个有效子模型）。")
    best = recs[max(recs, key=lambda k: len(recs[k].images))]
    raw_model_dir = sparse_raw / "best"
    best.write(str(raw_model_dir))

    # Ensure undistorted PINHOLE camera model for 2DGS
    (data_fg / "images").mkdir(parents=True, exist_ok=True)
    (data_fg / "masks").mkdir(parents=True, exist_ok=True)
    (data_fg / "sparse" / "0").mkdir(parents=True, exist_ok=True)

    if lock_intr:
        best.write(str(data_fg / "sparse" / "0"))
        src_for_mask = raw_images_dir
    else:
        undist_tmp = out_dir / "_undist_tmp"
        if undist_tmp.exists():
            shutil.rmtree(undist_tmp)
        pycolmap.undistort_images(str(undist_tmp), str(raw_model_dir), str(raw_images_dir))
        shutil.copytree(undist_tmp / "sparse", data_fg / "sparse" / "0", dirs_exist_ok=True)
        src_for_mask = undist_tmp / "images"

    # Build strict background-masked training images (eroded outside mask -> no crown background bleed)
    for stem in stems:
        im_p = src_for_mask / f"{stem}.jpg"
        if not im_p.exists():
            continue
        bgr = cv2.imread(str(im_p), cv2.IMREAD_COLOR)
        Hi, Wi = bgr.shape[:2]
        m_u8 = np.asarray(Image.open(fg_masks_dir / f"{stem}.png").convert("L"), dtype=np.uint8)
        if m_u8.shape != (Hi, Wi):
            m_u8 = cv2.resize(m_u8, (Wi, Hi), interpolation=cv2.INTER_NEAREST)
        m_bin = (m_u8 > 127).astype(np.uint8) * 255
        # Soft feather inside 1px border instead of dilating outward into dorm/wall background
        soft_m = cv2.GaussianBlur(m_bin.astype(np.float32) / 255.0, (0, 0), sigmaX=1.0)
        soft_m = np.where(m_bin > 127, 1.0, soft_m)
        bgr_clean = np.clip(bgr.astype(np.float32) * soft_m[:, :, None], 0, 255).astype(np.uint8)
        cv2.imwrite(str(data_fg / "images" / f"{stem}.jpg"), bgr_clean, [cv2.IMWRITE_JPEG_QUALITY, 96])
        Image.fromarray(m_bin, mode="L").save(data_fg / "masks" / f"{stem}.png")

    report = {
        "total_frames": len(stems),
        "registered_frames": len(best.images),
        "num_points3D": len(best.points3D),
        "mean_reprojection_error_px": round(float(best.compute_mean_reprojection_error()), 4),
        "kept_match_pairs": len(kept_pairs),
        "intrinsics_mode": "locked_pinhole" if lock_intr else "estimated_simple_radial_undistorted",
    }
    with open(data_fg / "sfm_report.json", "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    print(f"[SfM] 注册 {report['registered_frames']}/{report['total_frames']} 帧, 重投影误差 {report['mean_reprojection_error_px']} px", flush=True)
    return data_fg


# ---------------------------------------------------------------------------
# Stage 4: Pose-Refined Foreground 2DGS Training
# ---------------------------------------------------------------------------
def _axis_angle_to_quat_and_mat(w: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
    theta2 = (w * w).sum()
    theta = torch.sqrt(theta2 + 1e-16)
    half = 0.5 * theta
    k = torch.where(theta < 1e-4, 0.5 - theta2 / 48.0, torch.sin(half) / (theta + 1e-16))
    q = torch.cat([torch.cos(half)[None], w * k], dim=0)
    q = q / torch.sqrt((q * q).sum() + 1e-16)
    r, x, y, z = q[0], q[1], q[2], q[3]
    R = torch.stack(
        [
            torch.stack([1.0 - 2.0 * (y * y + z * z), 2.0 * (x * y - r * z), 2.0 * (x * z + r * y)]),
            torch.stack([2.0 * (x * y + r * z), 1.0 - 2.0 * (x * x + z * z), 2.0 * (y * z - r * x)]),
            torch.stack([2.0 * (x * z - r * y), 2.0 * (y * z + r * x), 1.0 - 2.0 * (x * x + y * y)]),
        ],
        dim=0,
    )
    return q, R


def _quat_mul_batch(q1: torch.Tensor, q2: torch.Tensor) -> torch.Tensor:
    w1, x1, y1, z1 = q1[0], q1[1], q1[2], q1[3]
    w2, x2, y2, z2 = q2[:, 0], q2[:, 1], q2[:, 2], q2[:, 3]
    return torch.stack(
        [
            w1 * w2 - x1 * x2 - y1 * y2 - z1 * z2,
            w1 * x2 + x1 * w2 + y1 * z2 - z1 * y2,
            w1 * y2 - x1 * z2 + y1 * w2 + z1 * x2,
            w1 * z2 + x1 * y2 - y1 * x2 + z1 * w2,
        ],
        dim=-1,
    )


def render_with_pose_delta(viewpoint_camera, pc, pipe, bg_color: torch.Tensor, d_rot: torch.Tensor | None = None, d_trans: torch.Tensor | None = None):
    from diff_surfel_rasterization import GaussianRasterizationSettings, GaussianRasterizer
    from utils.point_utils import depth_to_normal

    screenspace_points = torch.zeros_like(pc.get_xyz, dtype=pc.get_xyz.dtype, requires_grad=True, device="cuda") + 0
    try:
        screenspace_points.retain_grad()
    except Exception:
        pass
    raster_settings = GaussianRasterizationSettings(
        image_height=int(viewpoint_camera.image_height),
        image_width=int(viewpoint_camera.image_width),
        tanfovx=math.tan(viewpoint_camera.FoVx * 0.5),
        tanfovy=math.tan(viewpoint_camera.FoVy * 0.5),
        bg=bg_color,
        scale_modifier=1.0,
        viewmatrix=viewpoint_camera.world_view_transform,
        projmatrix=viewpoint_camera.full_proj_transform,
        sh_degree=pc.active_sh_degree,
        campos=viewpoint_camera.camera_center,
        prefiltered=False,
        debug=False,
    )
    rasterizer = GaussianRasterizer(raster_settings=raster_settings)
    means3D, rotations = pc.get_xyz, pc.get_rotation
    if d_rot is not None and d_trans is not None:
        q_d, R_d = _axis_angle_to_quat_and_mat(d_rot)
        means3D = means3D @ R_d.T + d_trans[None, :]
        rotations = _quat_mul_batch(q_d, F.normalize(rotations, dim=-1))

    rendered_image, radii, allmap = rasterizer(
        means3D=means3D, means2D=screenspace_points, shs=pc.get_features, colors_precomp=None,
        opacities=pc.get_opacity, scales=pc.get_scaling, rotations=rotations, cov3D_precomp=None,
    )
    render_alpha = allmap[1:2]
    rend_normal_cam = allmap[2:5]
    render_normal = (rend_normal_cam.permute(1, 2, 0) @ (viewpoint_camera.world_view_transform[:3, :3].T)).permute(2, 0, 1)
    d_med = torch.nan_to_num(allmap[5:6], 0, 0)
    d_exp = torch.nan_to_num(allmap[0:1] / render_alpha, 0, 0)
    surf_depth = d_exp * (1.0 - pipe.depth_ratio) + pipe.depth_ratio * d_med
    surf_normal = depth_to_normal(viewpoint_camera, surf_depth).permute(2, 0, 1) * render_alpha.detach()
    return {
        "render": rendered_image, "viewspace_points": screenspace_points, "visibility_filter": radii > 0, "radii": radii,
        "rend_alpha": render_alpha, "rend_normal": render_normal, "rend_normal_cam": rend_normal_cam,
        "rend_dist": allmap[6:7], "surf_depth": surf_depth, "surf_normal": surf_normal,
    }


def stage_train(args, data_fg: Path, priors_dir: Path, out_dir: Path) -> Path:
    from arguments import ModelParams, OptimizationParams, PipelineParams
    from scene import GaussianModel, Scene
    from utils.general_utils import safe_state
    from utils.loss_utils import l1_loss, ssim
    import camera_utils as cu

    model_dir = out_dir / "2dgs_out"
    model_dir.mkdir(parents=True, exist_ok=True)
    with open(priors_dir / "auto_anchors.json", "r", encoding="utf-8") as fh:
        anchors = json.load(fh)
    ref_stem = anchors["ref_stem"]

    p = ArgumentParser()
    lp, op, pp = ModelParams(p), OptimizationParams(p), PipelineParams(p)
    iters = 10000 if args.fast and args.iterations == 15000 else args.iterations
    gs_args = p.parse_args(
        ["-s", str(data_fg), "-m", str(model_dir), "--iterations", str(iters), "--depth_ratio", "0.0",
         "--lambda_dist", "0.01", "--lambda_normal", "0.05", "--foreground_only", "--lambda_alpha", "0.05"]
    )
    dataset, opt, pipe = lp.extract(gs_args), op.extract(gs_args), pp.extract(gs_args)
    dataset.foreground_only = True
    dataset.lambda_alpha = 0.05
    safe_state(True)
    with open(model_dir / "cfg_args", "w", encoding="utf-8") as fh:
        fh.write(str(Namespace(**vars(dataset))))

    gaussians = GaussianModel(dataset.sh_degree)
    scene = Scene(dataset, gaussians, shuffle=False)
    gaussians.training_setup(opt)
    cams = scene.getTrainCameras()
    stem_to_idx = {Path(c.image_name).stem: idx for idx, c in enumerate(cams)}
    if ref_stem not in stem_to_idx:
        ref_stem = sorted(stem_to_idx)[0]
    ref_idx = stem_to_idx[ref_stem]

    # Preload CPU priors
    sap_dir = priors_dir / "normals_sapiens"
    use_sap = sap_dir.exists() and not args.no_sapiens
    priors_cache = {}
    for c in cams:
        st = Path(c.image_name).stem
        sp, dp = priors_dir / "sem" / f"{st}.png", priors_dir / "disp" / f"{st}.npy"
        if not sp.exists() or not dp.exists():
            continue
        sem_a = np.asarray(Image.open(sp).convert("L"), dtype=np.uint8).copy()
        disp_a = np.load(dp).astype(np.float32).copy()
        norm_t = torch.from_numpy(np.load(sap_dir / f"{st}.npy").astype(np.float16).copy()) if (use_sap and (sap_dir / f"{st}.npy").exists()) else None
        priors_cache[st] = (torch.from_numpy(sem_a), torch.from_numpy(disp_a), norm_t)

    N_cams = len(cams)
    rot_deltas = torch.nn.Parameter(torch.zeros((N_cams, 3), dtype=torch.float32, device="cuda"))
    trans_deltas = torch.nn.Parameter(torch.zeros((N_cams, 3), dtype=torch.float32, device="cuda"))
    pose_start = int(0.10 * iters)
    pose_end = int(0.73 * iters)
    pose_opt = torch.optim.Adam([rot_deltas, trans_deltas], lr=2e-4, betas=(0.9, 0.99))
    pose_sched = torch.optim.lr_scheduler.CosineAnnealingLR(pose_opt, T_max=max(1, pose_end - pose_start), eta_min=2e-5)
    max_trans_norm = 0.25 * float(scene.cameras_extent)
    background = torch.zeros(3, dtype=torch.float32, device="cuda")

    ai_start = int(0.17 * iters)
    ai_ramp = int(0.17 * iters)
    vp_stack = None
    bar = tqdm(range(0, iters), desc="V9 General 2DGS")
    for iteration in range(1, iters + 1):
        gaussians.update_learning_rate(iteration)
        if iteration % 1000 == 0:
            gaussians.oneupSHdegree()
        if not vp_stack:
            vp_stack = list(range(N_cams))
        cam_idx = vp_stack.pop(randint(0, len(vp_stack) - 1))
        cam = cams[cam_idx]
        stem = Path(cam.image_name).stem

        use_pose = (pose_start <= iteration <= pose_end) and (cam_idx != ref_idx)
        d_r = rot_deltas[cam_idx] if cam_idx != ref_idx else rot_deltas[cam_idx].detach()
        d_t = trans_deltas[cam_idx] if cam_idx != ref_idx else trans_deltas[cam_idx].detach()
        pkg = render_with_pose_delta(cam, gaussians, pipe, background, d_r, d_t)

        image, gt = pkg["render"], cam.original_image.cuda()
        mask = getattr(cam, "gt_alpha_mask", None)
        if mask is not None:
            m = mask.to(image.device)
            Ll1 = l1_loss(image * m, gt * m)
            loss = (1.0 - opt.lambda_dssim) * Ll1 + opt.lambda_dssim * (1.0 - ssim(image * m, gt * m))
            alpha_loss = 0.05 * l1_loss(pkg["rend_alpha"], m)
        else:
            Ll1 = l1_loss(image, gt)
            loss = (1.0 - opt.lambda_dssim) * Ll1 + opt.lambda_dssim * (1.0 - ssim(image, gt))
            alpha_loss = torch.zeros((), device=image.device)

        normal_loss = (opt.lambda_normal if iteration > 7000 else 0.0) * (1.0 - (pkg["rend_normal"] * pkg["surf_normal"]).sum(dim=0)).mean()
        dist_loss = (opt.lambda_dist if iteration > 3000 else 0.0) * pkg["rend_dist"].mean()

        ai_loss = ld = lg = ln_n = torch.zeros((), device=image.device)
        head_m = None
        if stem in priors_cache:
            sem_c, disp_c, sap_c = priors_cache[stem]
            sd = pkg["surf_depth"][0]
            ra = pkg["rend_alpha"][0]
            H, W = sd.shape
            sem = sem_c.to(device=sd.device, non_blocking=True)
            disp_ai = disp_c.to(device=sd.device, dtype=torch.float32, non_blocking=True)
            if sem.shape != (H, W):
                sem = F.interpolate(sem[None, None].float(), size=(H, W), mode="nearest").squeeze(0).squeeze(0).to(torch.uint8)
                disp_ai = F.interpolate(disp_ai[None, None], size=(H, W), mode="bilinear", align_corners=False).squeeze(0).squeeze(0)
            face_m, hair_m = sem == 11, sem == 2
            head_m = face_m | hair_m
            valid = head_m & (ra.detach() > 0.65) & (sd.detach() > 0.3)
            if int(valid.sum()) > 400:
                head_in = (-F.max_pool2d(-head_m[None, None].float(), 5, 1, 2)).squeeze(0).squeeze(0) > 0.5
                with torch.no_grad():
                    x, y = disp_ai[valid], 1.0 / sd.detach()[valid].clamp(min=0.3)
                    xm, ym = x.mean(), y.mean()
                    sc = (((x - xm) * (y - ym)).mean() / ((x - xm) ** 2).mean().clamp(min=1e-6)).clamp(min=1e-4)
                    inv_p = (sc * disp_ai + (ym - sc * xm)).clamp(min=0.05)
                inv_d = 1.0 / sd.clamp(min=0.3)
                wp = torch.where(face_m, 1.0, torch.where(hair_m, 0.75, 0.0)) * valid.float()
                ld = (torch.abs(inv_d - inv_p) * wp).sum() / wp.sum().clamp(min=1.0)
                ix = head_in[:, :-1] & head_in[:, 1:] & valid[:, :-1] & valid[:, 1:]
                iy = head_in[:-1, :] & head_in[1:, :] & valid[:-1, :] & valid[1:, :]
                lg = (
                    (torch.abs((inv_d[:, 1:] - inv_d[:, :-1]) - (inv_p[:, 1:] - inv_p[:, :-1])) * ix.float()).sum() / ix.float().sum().clamp(min=1.0)
                    + (torch.abs((inv_d[1:, :] - inv_d[:-1, :]) - (inv_p[1:, :] - inv_p[:-1, :])) * iy.float()).sum() / iy.float().sum().clamp(min=1.0)
                )
                nm = head_in & (ra.detach() > 0.65)
                if sap_c is not None and int(nm.sum()) > 200:
                    sn = sap_c.to(device=sd.device, dtype=torch.float32, non_blocking=True)
                    if sn.shape[1:] != (H, W):
                        sn = F.normalize(F.interpolate(sn[None], size=(H, W), mode="bilinear", align_corners=False).squeeze(0), dim=0, eps=1e-8)
                    cs = (F.normalize(pkg["rend_normal_cam"], dim=0, eps=1e-8) * F.normalize(sn, dim=0, eps=1e-8)).sum(dim=0).clamp(-1.0, 1.0)
                    wn = torch.where(face_m, 1.0, torch.where(hair_m, 0.75, 0.0)) * nm.float()
                    ln_n = ((1.0 - cs) * wn).sum() / wn.sum().clamp(min=1.0)
            if iteration > ai_start:
                ramp = min(1.0, (iteration - ai_start) / float(max(1, ai_ramp)))
                ai_loss = ramp * (0.06 * ld + 0.12 * lg + 0.08 * ln_n)

        g_r = g_t = None
        if use_pose and head_m is not None and int((head_m > 0.5).sum()) > 500:
            hm3 = head_m[None].float()
            l1_h = (torch.abs(image - gt) * hm3).sum() / (3.0 * hm3.sum().clamp(min=1.0))
            ssim_h = 1.0 - ssim(image * hm3, gt * hm3)
            loss_pose = l1_h + 0.2 * ssim_h + 0.15 * ln_n + 0.08 * ld + 1e-3 * (d_r.pow(2).sum() + d_t.pow(2).sum())
            g_r, g_t = torch.autograd.grad(loss_pose, (rot_deltas, trans_deltas), retain_graph=True)

        (loss + dist_loss + normal_loss + alpha_loss + ai_loss).backward()
        with torch.no_grad():
            if iteration % 10 == 0:
                bar.set_postfix({"Pts": len(gaussians.get_xyz)})
                bar.update(10)
            if iteration in (7000, iters):
                scene.save(iteration)
            if iteration < opt.densify_until_iter:
                gaussians.max_radii2D[pkg["visibility_filter"]] = torch.max(gaussians.max_radii2D[pkg["visibility_filter"]], pkg["radii"][pkg["visibility_filter"]])
                gaussians.add_densification_stats(pkg["viewspace_points"], pkg["visibility_filter"])
                if iteration > opt.densify_from_iter and iteration % opt.densification_interval == 0:
                    gaussians.densify_and_prune(opt.densify_grad_threshold, opt.opacity_cull, scene.cameras_extent, 20 if iteration > opt.opacity_reset_interval else None)
                if iteration % opt.opacity_reset_interval == 0:
                    gaussians.reset_opacity()
            if iteration < iters:
                gaussians.optimizer.step()
                gaussians.optimizer.zero_grad(set_to_none=True)
            if use_pose and g_r is not None and g_t is not None:
                pose_opt.zero_grad(set_to_none=True)
                rot_deltas.grad = g_r.detach().clone()
                trans_deltas.grad = g_t.detach().clone()
                rot_deltas.grad[ref_idx].zero_()
                trans_deltas.grad[ref_idx].zero_()
                pose_opt.step()
                pose_sched.step()
                rot_deltas[ref_idx].zero_()
                trans_deltas[ref_idx].zero_()
                rn = rot_deltas.norm(dim=1, keepdim=True).clamp(min=1e-8)
                rot_deltas.data = torch.where(rn > 0.18, rot_deltas.data * (0.18 / rn), rot_deltas.data)
                tn = trans_deltas.norm(dim=1, keepdim=True).clamp(min=1e-8)
                trans_deltas.data = torch.where(tn > max_trans_norm, trans_deltas.data * (max_trans_norm / tn), trans_deltas.data)
            elif rot_deltas.grad is not None or trans_deltas.grad is not None:
                pose_opt.zero_grad(set_to_none=True)
    bar.close()

    refined = {}
    fg_psnrs = []
    with torch.no_grad():
        for idx, cam in enumerate(cams):
            st = Path(cam.image_name).stem
            _, Rd = _axis_angle_to_quat_and_mat(rot_deltas[idx])
            Rd_np, td_np = Rd.cpu().numpy().astype(np.float64), trans_deltas[idx].cpu().numpy().astype(np.float64)
            Rb, tb = cu.w2c_rt(cam)
            refined[st] = {
                "d_rot": [float(x) for x in rot_deltas[idx].cpu().numpy()],
                "d_trans": [float(x) for x in trans_deltas[idx].cpu().numpy()],
                "d_rot_deg": round(float(np.rad2deg(rot_deltas[idx].norm().item())), 3),
                "R_refined": (Rb @ Rd_np).tolist(),
                "t_refined": (Rb @ td_np + tb).tolist(),
            }
            pkg = render_with_pose_delta(cam, gaussians, pipe, background, rot_deltas[idx], trans_deltas[idx])
            im, gt = torch.clamp(pkg["render"], 0.0, 1.0), torch.clamp(cam.original_image.cuda(), 0.0, 1.0)
            m = getattr(cam, "gt_alpha_mask", None)
            if m is not None:
                fb = m[0] > 0.5
                mse = ((im[:, fb] - gt[:, fb]) ** 2).mean().clamp(min=1e-10)
                fg_psnrs.append(float(-10.0 * torch.log10(mse).item()))
    with open(model_dir / "refined_poses.json", "w", encoding="utf-8") as fh:
        json.dump(refined, fh, indent=2)
    with open(model_dir / "train_stats.json", "w", encoding="utf-8") as fh:
        json.dump({"iterations": iters, "final_points": int(len(gaussians.get_xyz)), "fg_psnr_mean_db": round(float(np.mean(fg_psnrs)), 3)}, fh, indent=2)
    print(f"[Train] 完成 {iters} 步, Gaussians={len(gaussians.get_xyz)}, 前景 PSNR={np.mean(fg_psnrs):.2f} dB", flush=True)
    return model_dir


# ---------------------------------------------------------------------------
# Stage 5 & 6: Mesh Extraction, Crown-Safe Baking & GLB Export
# ---------------------------------------------------------------------------
def _orient_outward(mesh: o3d.geometry.TriangleMesh, cam_centers: np.ndarray) -> tuple[o3d.geometry.TriangleMesh, float]:
    mesh.compute_triangle_normals()
    v, f, fn = np.asarray(mesh.vertices), np.asarray(mesh.triangles), np.asarray(mesh.triangle_normals)
    if len(f) == 0:
        return mesh, 0.0
    fc = v[f].mean(axis=1)
    _, nn = cKDTree(cam_centers).query(fc, k=1)
    to_cam = cam_centers[nn] - fc
    to_cam /= np.maximum(np.linalg.norm(to_cam, axis=1, keepdims=True), 1e-12)
    out_frac = float(((fn * to_cam).sum(axis=1) > 0).mean())
    if out_frac < 0.5:
        mesh.triangles = o3d.utility.Vector3iVector(f[:, ::-1].copy())
        mesh.compute_triangle_normals()
        out_frac = 1.0 - out_frac
    mesh.compute_vertex_normals()
    return mesh, out_frac


def _keep_main_components(o3m: o3d.geometry.TriangleMesh, min_area_ratio: float = 0.08) -> o3d.geometry.TriangleMesh:
    v, f = np.asarray(o3m.vertices, dtype=np.float64), np.asarray(o3m.triangles, dtype=np.int64)
    vc = np.concatenate([(np.clip(np.asarray(o3m.vertex_colors), 0.0, 1.0) * 255.0).astype(np.uint8), np.full((len(v), 1), 255, dtype=np.uint8)], axis=1) if o3m.has_vertex_colors() else None
    tm = trimesh.Trimesh(vertices=v, faces=f, vertex_colors=vc, process=False)
    parts = tm.split(only_watertight=False)
    if len(parts) > 1:
        parts.sort(key=lambda p: -p.area)
        tm = trimesh.util.concatenate([p for p in parts if float(p.area) >= min_area_ratio * float(parts[0].area)] or [parts[0]])
    res = o3d.geometry.TriangleMesh(o3d.utility.Vector3dVector(np.asarray(tm.vertices, dtype=np.float64)), o3d.utility.Vector3iVector(np.asarray(tm.faces, dtype=np.int32)))
    if getattr(tm.visual, "kind", None) == "vertex" and tm.visual.vertex_colors is not None:
        c = np.asarray(tm.visual.vertex_colors)[:, :3].astype(np.float64) / 255.0
        if len(c) == len(tm.vertices):
            res.vertex_colors = o3d.utility.Vector3dVector(c)
    res.remove_duplicated_vertices(); res.remove_duplicated_triangles(); res.remove_degenerate_triangles(); res.remove_unreferenced_vertices()
    return res


def _row_norm_lap(n_verts: int, faces: np.ndarray):
    r = np.concatenate([faces[:, 0], faces[:, 1], faces[:, 1], faces[:, 2], faces[:, 2], faces[:, 0]])
    c = np.concatenate([faces[:, 1], faces[:, 0], faces[:, 2], faces[:, 1], faces[:, 0], faces[:, 2]])
    A = sp.csr_matrix((np.ones(len(r), dtype=np.float64), (r, c)), shape=(n_verts, n_verts))
    A.sum_duplicates(); A.data[:] = 1.0
    return sp.diags(1.0 / np.maximum(np.asarray(A.sum(axis=1)).ravel(), 1.0)) @ A


def stage_mesh(args, data_fg: Path, priors_dir: Path, model_dir: Path, out_dir: Path) -> Path:
    from arguments import ModelParams, PipelineParams
    from scene import GaussianModel, Scene
    from fusion_core import FusionVolume
    import camera_utils as cu
    import mesh_render as mr

    mesh_dir = out_dir / "mesh"
    mesh_dir.mkdir(parents=True, exist_ok=True)
    refined = json.load(open(model_dir / "refined_poses.json", encoding="utf-8"))
    anchors = json.load(open(priors_dir / "auto_anchors.json", encoding="utf-8"))

    p = ArgumentParser(); mp, pp = ModelParams(p, sentinel=True), PipelineParams(p)
    cfg = eval(open(model_dir / "cfg_args", encoding="utf-8").read())  # noqa: S307
    dataset = mp.extract(Namespace(**{**vars(cfg), "model_path": str(model_dir)}))
    pipe0, pipe1 = pp.extract(p.parse_args([])), pp.extract(p.parse_args([]))
    pipe0.depth_ratio, pipe1.depth_ratio = 0.0, 1.0
    gaussians = GaussianModel(dataset.sh_degree)
    load_it = 10000 if args.fast and args.iterations == 15000 else args.iterations
    scene = Scene(dataset, gaussians, load_iteration=load_it, shuffle=False)
    bg = torch.zeros(3, dtype=torch.float32, device="cuda")
    cams = scene.getTrainCameras() + scene.getTestCameras()
    cam_centers_np = np.stack([-np.array(refined[Path(c.image_name).stem]["R_refined"]).T @ np.array(refined[Path(c.image_name).stem]["t_refined"]) for c in cams], axis=0)

    frames, p95s = [], []
    for cam in cams:
        st = Path(cam.image_name).stem
        pose = refined[st]
        dr, dt = torch.tensor(pose["d_rot"], dtype=torch.float32, device="cuda"), torch.tensor(pose["d_trans"], dtype=torch.float32, device="cuda")
        with torch.no_grad():
            pkg0 = render_with_pose_delta(cam, gaussians, pipe0, bg, dr, dt)
            pkg1 = render_with_pose_delta(cam, gaussians, pipe1, bg, dr, dt)
        d0 = pkg0["surf_depth"].squeeze().cpu().numpy().astype(np.float32)
        d1 = cv2.medianBlur(pkg1["surf_depth"].squeeze().cpu().numpy().astype(np.float32), 5)
        d = np.where(np.isfinite(d0) & np.isfinite(d1) & (d1 > 0) & ((d0 - d1) > 0.020), d1, d0)
        rgb = np.clip(pkg0["render"].permute(1, 2, 0).cpu().numpy(), 0.0, 1.0).astype(np.float32)
        alpha = pkg0["rend_alpha"].squeeze().cpu().numpy().astype(np.float32)
        m_im = Image.open(data_fg / "masks" / f"{st}.png").convert("L").filter(ImageFilter.MaxFilter(3))
        mask = np.asarray(m_im) > 127
        sem = np.asarray(Image.open(priors_dir / "sem" / f"{st}.png").convert("L"), dtype=np.uint8)
        fg_ok = np.isfinite(d) & (d > 0) & (alpha > 0.5) & mask
        if fg_ok.sum() > 100:
            med_d = float(np.median(d[fg_ok]))
            p95s.append(float(np.percentile(d[fg_ok], 95)))
            d_f = cv2.bilateralFilter(np.where(fg_ok, d, med_d).astype(np.float32), 5, float(max(0.025, 0.02 * med_d)), 2.0)
            d = np.where(fg_ok, d_f, d)
        frames.append({"name": cam.image_name, "depth": d, "rgb": rgb, "alpha": alpha, "mask": mask, "sem": sem, "K": cu.intrinsics(cam), "R": np.array(pose["R_refined"], dtype=np.float64), "t": np.array(pose["t_refined"], dtype=np.float64)})

    depth_trunc = float(np.median(p95s)) * 1.15
    pts = [cu.backproject(np.stack([xs[ok], ys[ok]], axis=1).astype(np.float64), f["depth"][ys, xs][ok], f["K"], f["R"], f["t"])
           for f in frames
           for ys, xs in [np.meshgrid(np.arange(0, f["depth"].shape[0], 3), np.arange(0, f["depth"].shape[1], 3), indexing="ij")]
           for ok in [np.isfinite(f["depth"][ys, xs]) & (f["depth"][ys, xs] > 0) & (f["depth"][ys, xs] < depth_trunc) & (f["alpha"][ys, xs] > 0.5) & f["mask"][ys, xs]] if ok.sum() >= 30]
    pts_all = np.concatenate(pts, axis=0)
    vol = FusionVolume(np.percentile(pts_all, 1.0, axis=0), np.percentile(pts_all, 99.0, axis=0), resolution=288, sdf_trunc_voxels=3.5)
    for f in frames:
        vol.integrate(f["depth"], f["rgb"], f["K"], f["R"], f["t"], mask=f["mask"], alpha=f["alpha"], max_depth=depth_trunc)

    verts, faces, vcol, _ = vol.extract_mesh(min_weight=0.35, drop_unknown_border=True)
    o3m = o3d.geometry.TriangleMesh(o3d.utility.Vector3dVector(verts.astype(np.float64)), o3d.utility.Vector3iVector(faces.astype(np.int32)))
    o3m.vertex_colors = o3d.utility.Vector3dVector(np.clip(vcol, 0.0, 1.0).astype(np.float64))
    o3m = _keep_main_components(o3m)
    o3m, _ = _orient_outward(o3m, cam_centers_np)

    v_tsdf, f_tsdf = np.asarray(o3m.vertices, dtype=np.float64), np.asarray(o3m.triangles, dtype=np.int64)
    hair_v, face_v, tot_v = np.zeros(len(v_tsdf)), np.zeros(len(v_tsdf)), np.zeros(len(v_tsdf))
    tol = 2.5 * vol.voxel
    for f in frames:
        H, W = f["sem"].shape
        Xc = v_tsdf @ f["R"].T + f["t"]
        z = Xc[:, 2]; ok = z > 0.2
        u = np.where(ok, f["K"][0, 0] * Xc[:, 0] / np.maximum(z, 0.2) + f["K"][0, 2], -1.0)
        v = np.where(ok, f["K"][1, 1] * Xc[:, 1] / np.maximum(z, 0.2) + f["K"][1, 2], -1.0)
        inb = ok & (u >= 0) & (u < W - 1) & (v >= 0) & (v < H - 1)
        idx = np.nonzero(inb)[0]
        ui, vi = np.rint(u[idx]).astype(np.int32), np.rint(v[idx]).astype(np.int32)
        vis = np.isfinite(f["depth"][vi, ui]) & (np.abs(z[idx] - f["depth"][vi, ui]) < tol) & (f["alpha"][vi, ui] > 0.5)
        idx = idx[vis]; cls = f["sem"][vi[vis], ui[vis]]
        hair_v[idx] += (cls == 2); face_v[idx] += (cls == 11); tot_v[idx] += 1.0

    is_hair = (hair_v > 0.42 * np.maximum(tot_v, 1.0)) & (hair_v >= face_v)
    is_face = (face_v > 0.42 * np.maximum(tot_v, 1.0)) & (~is_hair)
    sem_tsdf = np.zeros(len(v_tsdf), dtype=np.uint8)
    sem_tsdf[is_face] = 1; sem_tsdf[is_hair] = 2

    W_t = _row_norm_lap(len(v_tsdf), f_tsdf)
    w_h = np.clip(W_t @ (W_t @ is_hair.astype(np.float64)), 0.0, 1.0)
    o3m_skin = o3d.geometry.TriangleMesh(o3m).filter_smooth_taubin(number_of_iterations=5)
    o3m_hair = o3d.geometry.TriangleMesh(o3m).filter_smooth_taubin(number_of_iterations=2)
    o3m.vertices = o3d.utility.Vector3dVector((1.0 - w_h[:, None]) * np.asarray(o3m_skin.vertices) + w_h[:, None] * np.asarray(o3m_hair.vertices))
    o3m.compute_vertex_normals()

    tsdf_v, tsdf_c, tsdf_n = np.asarray(o3m.vertices), np.asarray(o3m.vertex_colors), np.asarray(o3m.vertex_normals)
    pcd = o3d.geometry.PointCloud()
    pcd.points, pcd.normals, pcd.colors = o3d.utility.Vector3dVector(tsdf_v), o3d.utility.Vector3dVector(tsdf_n), o3d.utility.Vector3dVector(tsdf_c)
    m_p, _ = o3d.geometry.TriangleMesh.create_from_point_cloud_poisson(pcd, depth=9, linear_fit=False)
    tree = cKDTree(tsdf_v)
    dists, _ = tree.query(np.asarray(m_p.vertices), k=1)
    m_p.remove_vertices_by_mask(dists > max(0.20, float(np.percentile(dists, 98)) * 1.6))
    m_p = _keep_main_components(m_p)
    if len(m_p.triangles) > 100000:
        m_p = _keep_main_components(m_p.simplify_quadric_decimation(target_number_of_triangles=100000))
    m_p, _ = _orient_outward(m_p, cam_centers_np)

    # Gentle eye socket fairing anchored by auto-detected pupils
    ref_stem = anchors["ref_stem"]
    ref_cam = next(c for c in cams if Path(c.image_name).stem == ref_stem)
    V, F_m = np.asarray(m_p.vertices, dtype=np.float64).copy(), np.asarray(m_p.triangles, dtype=np.int64)
    Rr, tr = np.array(refined[ref_stem]["R_refined"]), np.array(refined[ref_stem]["t_refined"])
    Cr, Kr = -Rr.T @ tr, cu.intrinsics(ref_cam)
    Wi, Hi = int(ref_cam.image_width), int(ref_cam.image_height)
    sw, sh = 360, 640
    K_sm = Kr.copy(); K_sm[0, :] *= sw / float(Wi); K_sm[1, :] *= sh / float(Hi)
    _, _, zb = mr.render(V, F_m, None, K_sm, Rr, tr, sw, sh, use_vertex_color=False, use_lighting=False, camera_center=Cr)
    Xc = V @ Rr.T + tr; z = Xc[:, 2]; ok_z = z > 0.2
    u = np.where(ok_z, Kr[0, 0] * Xc[:, 0] / np.maximum(z, 0.2) + Kr[0, 2], -1.0)
    v = np.where(ok_z, Kr[1, 1] * Xc[:, 1] / np.maximum(z, 0.2) + Kr[1, 2], -1.0)
    inb = ok_z & (u >= 0) & (u < Wi - 1) & (v >= 0) & (v < Hi - 1)
    uis, vis = np.clip(np.rint(u * (sw / float(Wi))).astype(np.int32), 0, sw - 1), np.clip(np.rint(v * (sh / float(Hi))).astype(np.int32), 0, sh - 1)
    front_shell = inb & np.isfinite(zb[vis, uis]) & (z <= zb[vis, uis] + 0.45)
    uR, vR = anchors["pupils"]["EyeR"]; uL, vL = anchors["pupils"]["EyeL"]; rx, ry = anchors["eye_radii"]
    eye_mask = front_shell & ((np.sqrt(((u - uR) / rx) ** 2 + ((v - vR) / ry) ** 2) < 1.0) | (np.sqrt(((u - uL) / rx) ** 2 + ((v - vL) / ry) ** 2) < 1.0))
    eye_idx = np.nonzero(eye_mask)[0]
    W_p = _row_norm_lap(len(V), F_m)
    if len(eye_idx) > 0:
        for _ in range(2):
            V[eye_idx] = 0.65 * V[eye_idx] + 0.35 * (W_p @ V)[eye_idx]

    m_p.vertices = o3d.utility.Vector3dVector(V)
    _, cidx = tree.query(V, k=1)
    m_p.vertex_colors = o3d.utility.Vector3dVector(np.clip(tsdf_c[cidx], 0.0, 1.0))
    m_p, _ = _orient_outward(m_p, cam_centers_np)
    np.save(mesh_dir / "vertex_sem.npy", sem_tsdf[cidx])
    o3d.io.write_triangle_mesh(str(mesh_dir / "head-clean.ply"), m_p)
    with open(mesh_dir / "mesh_stats.json", "w", encoding="utf-8") as fh:
        json.dump({"voxel_size": round(float(vol.voxel), 6), "final_verts": len(V), "final_faces": len(F_m)}, fh, indent=2)
    print(f"[Mesh] 网格提取完成: {len(F_m)} 面, {len(V)} 顶点", flush=True)
    return mesh_dir


def stage_bake(args, data_fg: Path, priors_dir: Path, model_dir: Path, mesh_dir: Path, out_dir: Path) -> Path:
    from arguments import ModelParams, PipelineParams
    from scene import GaussianModel, Scene
    import camera_utils as cu
    import mesh_render as mr

    final_dir = out_dir / "final"
    final_dir.mkdir(parents=True, exist_ok=True)
    refined = json.load(open(model_dir / "refined_poses.json", encoding="utf-8"))
    anchors = json.load(open(priors_dir / "auto_anchors.json", encoding="utf-8"))
    depth_tol = 6.0 * float(json.load(open(mesh_dir / "mesh_stats.json", encoding="utf-8"))["voxel_size"])
    ref_stem, hero_stems = anchors["ref_stem"], set(anchors["hero_eye_stems"])
    uR, vR = anchors["pupils"]["EyeR"]; uL, vL = anchors["pupils"]["EyeL"]; rx, ry = anchors["eye_radii"]

    p = ArgumentParser(); mp, pp = ModelParams(p, sentinel=True), PipelineParams(p)
    cfg = eval(open(model_dir / "cfg_args", encoding="utf-8").read())  # noqa: S307
    dataset = mp.extract(Namespace(**{**vars(cfg), "model_path": str(model_dir)}))
    pipe0, pipe1 = pp.extract(p.parse_args([])), pp.extract(p.parse_args([]))
    pipe0.depth_ratio, pipe1.depth_ratio = 0.0, 1.0
    gaussians = GaussianModel(dataset.sh_degree)
    load_it = 10000 if args.fast and args.iterations == 15000 else args.iterations
    scene = Scene(dataset, gaussians, load_iteration=load_it, shuffle=False)
    bg = torch.zeros(3, dtype=torch.float32, device="cuda")
    cams = scene.getTrainCameras() + scene.getTestCameras()

    mesh = o3d.io.read_triangle_mesh(str(mesh_dir / "head-clean.ply"))
    mesh.compute_vertex_normals()
    V0, N0, VC0, F0 = np.asarray(mesh.vertices, np.float32), np.asarray(mesh.vertex_normals, np.float32), np.asarray(mesh.vertex_colors, np.float32), np.asarray(mesh.triangles, np.uint32)
    vsem = np.load(mesh_dir / "vertex_sem.npy")
    vmapping, indices, uvs = xatlas.parametrize(V0, F0, N0)
    V_uv, N_uv, VC_uv, VHAIR_uv, VFACE_uv, F_uv, uvs = V0[vmapping], N0[vmapping], VC0[vmapping], (vsem == 2).astype(np.float32)[vmapping], (vsem == 1).astype(np.float32)[vmapping], indices.astype(np.int64), uvs.astype(np.float32)

    # Tangent basis for glTF UV
    T_acc, B_acc = np.zeros_like(V_uv, np.float64), np.zeros_like(V_uv, np.float64)
    p0, p1, p2 = V_uv[F_uv[:, 0]], V_uv[F_uv[:, 1]], V_uv[F_uv[:, 2]]
    u0, u1, u2 = uvs[F_uv[:, 0]], uvs[F_uv[:, 1]], uvs[F_uv[:, 2]]
    du1, dv1, du2, dv2 = u1[:, 0] - u0[:, 0], u1[:, 1] - u0[:, 1], u2[:, 0] - u0[:, 0], u2[:, 1] - u0[:, 1]
    det = du1 * dv2 - du2 * dv1
    inv_d = np.where(np.abs(det) > 1e-12, 1.0 / np.where(np.abs(det) > 1e-12, det, 1.0), 0.0)[:, None]
    for k, tri_vec in ((0, (dv2[:, None] * (p1 - p0) - dv1[:, None] * (p2 - p0)) * inv_d),):
        for j in range(3):
            np.add.at(T_acc, F_uv[:, j], tri_vec)
    for j in range(3):
        np.add.at(B_acc, F_uv[:, j], (-du2[:, None] * (p1 - p0) + du1[:, None] * (p2 - p0)) * inv_d)
    N64 = N_uv.astype(np.float64)
    T_o = T_acc - np.sum(T_acc * N64, axis=1, keepdims=True) * N64
    T_o /= np.maximum(np.linalg.norm(T_o, axis=1, keepdims=True), 1e-8)
    handed = np.where(np.sum(np.cross(N64, T_o) * (B_acc / np.maximum(np.linalg.norm(B_acc, axis=1, keepdims=True), 1e-8)), axis=1) < 0.0, -1.0, 1.0)
    B_o = handed[:, None] * np.cross(N64, T_o)
    B_o /= np.maximum(np.linalg.norm(B_o, axis=1, keepdims=True), 1e-8)

    TEX = 2048
    pos_m, norm_m, tan_m, bitan_m, vcol_m = np.zeros((TEX, TEX, 3), np.float32), np.zeros((TEX, TEX, 3), np.float32), np.zeros((TEX, TEX, 3), np.float32), np.zeros((TEX, TEX, 3), np.float32), np.zeros((TEX, TEX, 3), np.float32)
    hair_m, face_m, valid_m = np.zeros((TEX, TEX), np.float32), np.zeros((TEX, TEX), np.float32), np.zeros((TEX, TEX), bool)
    pxa, pya = uvs[:, 0] * (TEX - 1), (1.0 - uvs[:, 1]) * (TEX - 1)
    for tri in F_uv:
        i0, i1, i2 = int(tri[0]), int(tri[1]), int(tri[2])
        ax, ay, bx, by, cx_, cy_ = float(pxa[i0]), float(pya[i0]), float(pxa[i1]), float(pya[i1]), float(pxa[i2]), float(pya[i2])
        x0, x1, y0, y1 = max(0, int(np.floor(min(ax, bx, cx_)))), min(TEX - 1, int(np.ceil(max(ax, bx, cx_)))), max(0, int(np.floor(min(ay, by, cy_)))), min(TEX - 1, int(np.ceil(max(ay, by, cy_))))
        d = (by - cy_) * (ax - cx_) + (cx_ - bx) * (ay - cy_)
        if x1 < x0 or y1 < y0 or abs(d) < 1e-8:
            continue
        gx, gy = np.meshgrid(np.arange(x0, x1 + 1, dtype=np.float32), np.arange(y0, y1 + 1, dtype=np.float32))
        l1 = ((by - cy_) * (gx - cx_) + (cx_ - bx) * (gy - cy_)) / d
        l2 = ((cy_ - ay) * (gx - cx_) + (ax - cx_) * (gy - cy_)) / d
        l3 = 1.0 - l1 - l2
        ins = (l1 >= -1e-4) & (l2 >= -1e-4) & (l3 >= -1e-4)
        if not ins.any():
            continue
        b1, b2, b3 = l1[ins][:, None], l2[ins][:, None], l3[ins][:, None]
        yy, xx = np.nonzero(ins); ty, tx = y0 + yy, x0 + xx
        pos_m[ty, tx] = b1 * V_uv[i0] + b2 * V_uv[i1] + b3 * V_uv[i2]
        nn = b1 * N_uv[i0] + b2 * N_uv[i1] + b3 * N_uv[i2]; nn /= np.maximum(np.linalg.norm(nn, axis=1, keepdims=True), 1e-8)
        norm_m[ty, tx] = nn
        tt = b1 * T_o[i0] + b2 * T_o[i1] + b3 * T_o[i2]; tt -= np.sum(tt * nn, axis=1, keepdims=True) * nn; tt /= np.maximum(np.linalg.norm(tt, axis=1, keepdims=True), 1e-8)
        tan_m[ty, tx] = tt
        bb = b1 * B_o[i0] + b2 * B_o[i1] + b3 * B_o[i2]; bb -= np.sum(bb * nn, axis=1, keepdims=True) * nn + np.sum(bb * tt, axis=1, keepdims=True) * tt; bb /= np.maximum(np.linalg.norm(bb, axis=1, keepdims=True), 1e-8)
        bitan_m[ty, tx] = bb
        vcol_m[ty, tx] = np.clip(b1 * VC_uv[i0] + b2 * VC_uv[i1] + b3 * VC_uv[i2], 0.0, 1.0)
        hair_m[ty, tx] = b1[:, 0] * VHAIR_uv[i0] + b2[:, 0] * VHAIR_uv[i1] + b3[:, 0] * VHAIR_uv[i2]
        face_m[ty, tx] = b1[:, 0] * VFACE_uv[i0] + b2[:, 0] * VFACE_uv[i1] + b3[:, 0] * VFACE_uv[i2]
        valid_m[ty, tx] = True

    vy, vx = np.nonzero(valid_m)
    P, Nm, Tm, Bm = pos_m[vy, vx].astype(np.float64), norm_m[vy, vx].astype(np.float64), tan_m[vy, vx].astype(np.float64), bitan_m[vy, vx].astype(np.float64)
    tex_hair, tex_face = hair_m[vy, vx] > 0.5, face_m[vy, vx] > 0.5

    def _bilerp(arr, u, v):
        H, W = arr.shape[:2]
        x0 = np.clip(np.floor(u).astype(np.int32), 0, W - 1); y0 = np.clip(np.floor(v).astype(np.int32), 0, H - 1)
        wx, wy = (u - x0)[:, None].astype(np.float32), (v - y0)[:, None].astype(np.float32)
        return ((1 - wy) * ((1 - wx) * arr[y0, x0] + wx * arr[y0, np.clip(x0 + 1, 0, W - 1)]) + wy * ((1 - wx) * arr[np.clip(y0 + 1, 0, H - 1), x0] + wx * arr[np.clip(y0 + 1, 0, H - 1), np.clip(x0 + 1, 0, W - 1)])).astype(np.float32)

    hair_lums = []
    for c in cams:
        st = Path(c.image_name).stem
        bgr = cv2.imread(str(data_fg / "images" / f"{st}.jpg"), cv2.IMREAD_COLOR)
        sem = np.asarray(Image.open(priors_dir / "sem" / f"{st}.png").convert("L"), dtype=np.uint8)
        if int((sem == 2).sum()) > 400:
            hair_lums.append(float(np.mean(cv2.cvtColor(bgr, cv2.COLOR_BGR2LAB)[:, :, 0][sem == 2])))
    target_L = float(np.percentile(hair_lums, 65)) if hair_lums else 38.0

    cam_obs, sharp_vals, hero_data, ref_cam = [], [], [], None
    sap_dir = priors_dir / "normals_sapiens"
    for cam in cams:
        st = Path(cam.image_name).stem
        if st == ref_stem:
            ref_cam = cam
        bgr = cv2.imread(str(data_fg / "images" / f"{st}.jpg"), cv2.IMREAD_COLOR)
        H, W = bgr.shape[:2]
        sem = np.asarray(Image.open(priors_dir / "sem" / f"{st}.png").convert("L"), dtype=np.uint8)
        # Strict slightly-eroded masks for hairline safety against dorm ceiling/wall bleed
        m_fg = np.asarray(Image.open(data_fg / "masks" / f"{st}.png").convert("L").filter(ImageFilter.MinFilter(5))) > 127
        hm = sem == 2
        bgr_lifted = bgr
        if int(hm.sum()) > 400:
            lab = cv2.cvtColor(bgr, cv2.COLOR_BGR2LAB)
            cur_L = float(np.mean(lab[:, :, 0][hm]))
            if cur_L < target_L * 0.90:
                Lc = cv2.createCLAHE(clipLimit=1.8, tileGridSize=(8, 8)).apply(lab[:, :, 0]).astype(np.float32)
                Lb = np.clip((0.55 * lab[:, :, 0] + 0.45 * Lc) * float(np.clip(target_L / max(cur_L, 8.0), 1.0, 1.55)), 0.0, 210.0)
                sh_m = cv2.GaussianBlur(hm.astype(np.float32), (0, 0), sigmaX=3.0)
                lab[:, :, 0] = np.clip((1.0 - sh_m) * lab[:, :, 0] + sh_m * Lb, 0.0, 255.0).astype(np.uint8)
                bgr_lifted = cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)

        rgb_orig, rgb_hair = bgr[:, :, ::-1].astype(np.float32) / 255.0, bgr_lifted[:, :, ::-1].astype(np.float32) / 255.0
        ys, xs = np.nonzero(m_fg)
        sharp = float(cv2.Laplacian(cv2.cvtColor(bgr[int(ys.min()):int(ys.min() + max(16, (ys.max()-ys.min())*0.45)), int(xs.min()):int(xs.max())+1], cv2.COLOR_BGR2GRAY), cv2.CV_32F).var()) if len(ys) > 100 else 1.0
        sharp_vals.append(sharp); s_idx = len(sharp_vals) - 1

        pose = refined[st]
        dr, dt = torch.tensor(pose["d_rot"], dtype=torch.float32, device="cuda"), torch.tensor(pose["d_trans"], dtype=torch.float32, device="cuda")
        with torch.no_grad():
            p0_, p1_ = render_with_pose_delta(cam, gaussians, pipe0, bg, dr, dt), render_with_pose_delta(cam, gaussians, pipe1, bg, dr, dt)
        d0, d1, alpha = p0_["surf_depth"].squeeze().cpu().numpy().astype(np.float32), cv2.medianBlur(p1_["surf_depth"].squeeze().cpu().numpy().astype(np.float32), 5), p0_["rend_alpha"].squeeze().cpu().numpy().astype(np.float32)
        depth = np.where(np.isfinite(d0) & np.isfinite(d1) & (d1 > 0) & ((d0 - d1) > 0.020), d1, d0)

        R, t, K = np.array(pose["R_refined"], np.float64), np.array(pose["t_refined"], np.float64), cu.intrinsics(cam)
        C = -R.T @ t
        sw, sh = W // 3, H // 3
        Ks = K.copy(); Ks[0, :] *= sw / float(W); Ks[1, :] *= sh / float(H)
        _, _, zb = mr.render(V0.astype(np.float64), F0.astype(np.int64), None, Ks, R, t, sw, sh, use_vertex_color=False, use_lighting=False, camera_center=C)
        if st in hero_stems:
            hero_data.append((rgb_orig, m_fg, K, R, t, C, zb, sw, sh, W, H, s_idx))

        sap_w = None
        if not args.no_sapiens and (sap_dir / f"{st}.npy").exists():
            sap_w = np.einsum("ji,jhw->hwi", R.astype(np.float32), np.load(sap_dir / f"{st}.npy").astype(np.float32))
            sap_w /= np.maximum(np.linalg.norm(sap_w, axis=-1, keepdims=True), 1e-8)

        Xc = P @ R.T + t; z = Xc[:, 2]; ok_z = z > 0.2
        u = np.where(ok_z, K[0, 0] * Xc[:, 0] / np.maximum(z, 0.2) + K[0, 2], -1.0)
        v = np.where(ok_z, K[1, 1] * Xc[:, 1] / np.maximum(z, 0.2) + K[1, 2], -1.0)
        inb = ok_z & (u >= 1.0) & (u < W - 2) & (v >= 1.0) & (v < H - 2)
        if not inb.any():
            continue
        idx = np.nonzero(inb)[0]
        ui, vi = np.rint(u[idx]).astype(np.int32), np.rint(v[idx]).astype(np.int32)
        uis, vis_ = np.clip(np.rint(u[idx] * (sw / float(W))).astype(np.int32), 0, sw - 1), np.clip(np.rint(v[idx] * (sh / float(H))).astype(np.int32), 0, sh - 1)
        vis = np.isfinite(depth[vi, ui]) & (np.abs(z[idx] - depth[vi, ui]) < depth_tol) & (alpha[vi, ui] > 0.5) & m_fg[vi, ui] & np.isfinite(zb[vis_, uis]) & (z[idx] <= zb[vis_, uis] + 0.035)
        if not vis.any():
            continue
        idx = idx[vis]
        vd = C[None, :] - P[idx]; vd /= np.maximum(np.linalg.norm(vd, axis=1, keepdims=True), 1e-12)
        fac = (Nm[idx] * vd).sum(axis=1)
        kp = fac > 0.28
        if not kp.any():
            continue
        idx, fac = idx[kp], fac[kp]
        cam_obs.append((idx, _bilerp(rgb_orig, u[idx], v[idx]), _bilerp(rgb_hair, u[idx], v[idx]), _bilerp(sap_w, u[idx], v[idx]) if sap_w is not None else None, fac, np.maximum(z[idx], 0.35), s_idx))

    med_s = float(np.median(sharp_vals)) if sharp_vals else 1.0
    sw_body = [float(np.clip((s / max(med_s, 1e-6)) ** 1.0, 0.45, 1.65)) for s in sharp_vals]
    sw_hair = [float(np.clip((s / max(med_s, 1e-6)) ** 0.75, 0.70, 1.35)) for s in sharp_vals]

    acc1, w1 = np.zeros((len(P), 3), np.float64), np.zeros(len(P), np.float64)
    for idx, co, _, _, fac, zs, si in cam_obs:
        w = ((fac ** 5) / (zs ** 3.5)) * sw_body[si]
        acc1[idx] += w[:, None] * co; w1[idx] += w
    hit1 = w1 > 1e-8; C1 = np.zeros((len(P), 3), np.float32); C1[hit1] = (acc1[hit1] / w1[hit1, None]).astype(np.float32)

    acc2, w2 = np.zeros((len(P), 3), np.float64), np.zeros(len(P), np.float64)
    for idx, co, _, _, fac, zs, si in cam_obs:
        g = np.mean(np.abs(co - C1[idx]), axis=1) <= 0.095
        if g.any():
            w = ((fac[g] ** 5) / (zs[g] ** 3.5)) * sw_body[si]
            acc2[idx[g]] += w[:, None] * co[g]; w2[idx[g]] += w
    hit2 = w2 > 1e-8
    C_base = vcol_m[vy, vx].copy(); C_base[hit1 & (~hit2)] = C1[hit1 & (~hit2)]; C_base[hit2] = np.clip(acc2[hit2] / w2[hit2, None], 0.0, 1.0).astype(np.float32)

    # Hero eye UV blend
    R3, t3 = np.array(refined[ref_stem]["R_refined"], np.float64), np.array(refined[ref_stem]["t_refined"], np.float64)
    K3 = cu.intrinsics(ref_cam); W3, H3 = int(ref_cam.image_width), int(ref_cam.image_height)
    Xc3 = P @ R3.T + t3; z3 = Xc3[:, 2]; ok3 = z3 > 0.2
    u3 = np.where(ok3, K3[0, 0] * Xc3[:, 0] / np.maximum(z3, 0.2) + K3[0, 2], -1.0)
    v3 = np.where(ok3, K3[1, 1] * Xc3[:, 1] / np.maximum(z3, 0.2) + K3[1, 2], -1.0)
    r_eye = np.minimum(np.sqrt(((u3 - uR) / (rx * 1.25)) ** 2 + ((v3 - vR) / (ry * 1.3)) ** 2), np.sqrt(((u3 - uL) / (rx * 1.25)) ** 2 + ((v3 - vL) / (ry * 1.3)) ** 2))
    eye_zone = ok3 & (u3 >= 0) & (u3 < W3 - 1) & (v3 >= 0) & (v3 < H3 - 1) & (r_eye <= 1.30) & (~tex_hair)
    ez_idx = np.nonzero(eye_zone)[0]; P_ez, Nm_ez = P[ez_idx], Nm[ez_idx]
    ea, ew = np.zeros((len(ez_idx), 3), np.float64), np.zeros(len(ez_idx), np.float64)
    for rgb_h, mh, Kh, Rh, th, Ch, zbh, swh, shh, Wh, Hh, si in hero_data:
        Xch = P_ez @ Rh.T + th; zh = Xch[:, 2]; okh = zh > 0.2
        uh = np.where(okh, Kh[0, 0] * Xch[:, 0] / np.maximum(zh, 0.2) + Kh[0, 2], -1.0)
        vh = np.where(okh, Kh[1, 1] * Xch[:, 1] / np.maximum(zh, 0.2) + Kh[1, 2], -1.0)
        sub = np.nonzero(okh & (uh >= 1) & (uh < Wh - 2) & (vh >= 1) & (vh < Hh - 2))[0]
        if len(sub) == 0:
            continue
        zm = zbh[np.clip(np.rint(vh[sub]*(shh/float(Hh))).astype(np.int32), 0, shh-1), np.clip(np.rint(uh[sub]*(swh/float(Wh))).astype(np.int32), 0, swh-1)]
        ov = mh[np.rint(vh[sub]).astype(np.int32), np.rint(uh[sub]).astype(np.int32)] & np.isfinite(zm) & (zh[sub] <= zm + 0.045)
        if not ov.any():
            continue
        sub = sub[ov]; vdh = Ch[None, :] - P_ez[sub]; vdh /= np.maximum(np.linalg.norm(vdh, axis=1, keepdims=True), 1e-12)
        wh = ((np.maximum((Nm_ez[sub] * vdh).sum(axis=1), 0.05) ** 4) / (np.maximum(zh[sub], 0.35) ** 3.0)) * sw_body[si]
        ea[sub] += wh[:, None] * _bilerp(rgb_h, uh[sub], vh[sub]); ew[sub] += wh

    C_merged = C_base.copy()
    if (ew > 1e-8).any():
        ez_hit = ew > 1e-8; hg = ez_idx[ez_hit]
        base_uv = vcol_m.copy(); base_uv[vy, vx] = C_base
        eye_uv, eye_msk = np.zeros((TEX, TEX, 3), np.float32), np.zeros((TEX, TEX), np.float32)
        eye_uv[vy[hg], vx[hg]] = np.clip(ea[ez_hit] / ew[ez_hit, None], 0.0, 1.0); eye_msk[vy[hg], vx[hg]] = 1.0
        den = np.maximum(cv2.GaussianBlur(eye_msk, (0, 0), 24.0), 1e-4)[:, :, None]
        matched = np.clip(eye_uv + (cv2.GaussianBlur(base_uv * eye_msk[:, :, None], (0, 0), 24.0) - cv2.GaussianBlur(eye_uv * eye_msk[:, :, None], (0, 0), 24.0)) / den, 0.0, 1.0)
        rr = np.clip((r_eye[hg] - 0.75) / 0.55, 0.0, 1.0)
        ae = np.where(r_eye[hg] <= 0.75, 1.0, 0.5 * (1.0 + np.cos(np.pi * rr))).astype(np.float32)[:, None]
        C_merged[hg] = (1.0 - ae) * C_base[hg] + ae * matched[vy[hg], vx[hg]]

    # Smooth hair bake + crown background-bleed outlier rejection
    hair_mask = tex_hair & (~eye_zone)
    ha1, hw1 = np.zeros((len(P), 3), np.float64), np.zeros(len(P), np.float64)
    for idx, _, ch, _, fac, zs, si in cam_obs:
        hm_i = hair_mask[idx]
        if not hm_i.any():
            continue
        hi, cs = idx[hm_i], ch[hm_i]
        # Reject bright tan/dorm background bleed along hairline fringes when hair is dark
        not_bleed = ~((cs[:, 0] > 0.34) & ((cs[:, 0] - cs[:, 2]) > 0.08))
        if not not_bleed.any():
            continue
        w = ((fac[hm_i][not_bleed] ** 5) * sw_hair[si]) / (zs[hm_i][not_bleed] ** 3.5)
        ha1[hi[not_bleed]] += w[:, None] * cs[not_bleed]; hw1[hi[not_bleed]] += w

    hh1 = hw1 > 1e-12; H1 = np.zeros((len(P), 3), np.float32); H1[hh1] = (ha1[hh1] / hw1[hh1, None]).astype(np.float32)
    H1_lum = 0.299 * H1[:, 0] + 0.587 * H1[:, 1] + 0.114 * H1[:, 2]
    ha2, hw2 = np.zeros((len(P), 3), np.float64), np.zeros(len(P), np.float64)
    for idx, _, ch, _, fac, zs, si in cam_obs:
        hm_i = hair_mask[idx]
        if not hm_i.any():
            continue
        hi, cs = idx[hm_i], ch[hm_i]
        lum = 0.299 * cs[:, 0] + 0.587 * cs[:, 1] + 0.114 * cs[:, 2]
        g = (lum <= H1_lum[hi] + 0.055) & (lum >= H1_lum[hi] - 0.12) & (~((cs[:, 0] > 0.34) & ((cs[:, 0] - cs[:, 2]) > 0.08)))
        if g.any():
            w = ((fac[hm_i][g] ** 5) * sw_hair[si]) / (zs[hm_i][g] ** 3.5)
            ha2[hi[g]] += w[:, None] * cs[g]; hw2[hi[g]] += w
    C_merged[hh1 & (hw2 <= 1e-12)] = H1[hh1 & (hw2 <= 1e-12)]
    C_merged[hw2 > 1e-12] = np.clip(ha2[hw2 > 1e-12] / hw2[hw2 > 1e-12, None], 0.0, 1.0).astype(np.float32)

    def _dilate(tex, val, iters=8):
        out, fld = tex.copy(), val.copy()
        h, w = fld.shape
        for _ in range(iters):
            if fld.all():
                break
            acc, cnt = np.zeros_like(out), np.zeros((h, w), np.float32)
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    if dy or dx:
                        m = fld[max(0, -dy):h-max(0, dy), max(0, -dx):w-max(0, dx)]
                        acc[max(0, dy):h-max(0, -dy), max(0, dx):w-max(0, -dx)][m] += out[max(0, -dy):h-max(0, dy), max(0, -dx):w-max(0, dx)][m]
                        cnt[max(0, dy):h-max(0, -dy), max(0, dx):w-max(0, -dx)][m] += 1.0
            grow = (~fld) & (cnt > 0)
            out[grow] = acc[grow] / cnt[grow, None]; fld |= grow
        return out

    tex_rgb = vcol_m.copy(); tex_rgb[vy, vx] = np.clip(C_merged, 0.0, 1.0)
    tex_pad = _dilate(tex_rgb, valid_m)
    hair_uv = np.zeros((TEX, TEX), bool); hair_uv[vy[hair_mask], vx[hair_mask]] = True
    tex_final = np.where(hair_uv[:, :, None], np.clip(tex_pad + 0.55 * (tex_pad - cv2.GaussianBlur(tex_pad, (0, 0), 1.1)), 0.0, 1.0), tex_pad)
    albedo_path = final_dir / "albedo_2048.png"
    Image.fromarray((np.clip(tex_final, 0.0, 1.0) * 255.0).astype(np.uint8), mode="RGB").save(albedo_path)

    # Tangent-space normal map from Sapiens2 world normals + hair micro-relief
    na, nw = np.zeros((len(P), 3), np.float64), np.zeros(len(P), np.float64)
    for idx, _, _, ns, fac, zs, si in cam_obs:
        if ns is not None:
            w = ((fac ** 3) * sw_hair[si]) / (zs ** 2.5)
            na[idx] += w[:, None] * ns; nw[idx] += w
    N_sap = Nm.copy()
    nh = nw > 1e-8
    N_sap[nh] = na[nh] / np.maximum(np.linalg.norm(na[nh], axis=1, keepdims=True), 1e-8)
    N_sap = np.where((np.sum(N_sap * Nm, axis=1) > 0.1)[:, None], N_sap, Nm)
    asap = np.zeros(len(P), np.float64); asap[tex_face] = 0.22; asap[eye_zone] = 0.15; asap[hair_mask] = 0.45
    N_bw = (1.0 - asap[:, None]) * Nm + asap[:, None] * N_sap
    N_bw /= np.maximum(np.linalg.norm(N_bw, axis=1, keepdims=True), 1e-8)
    gray_t = (0.299 * tex_final[:, :, 0] + 0.587 * tex_final[:, :, 1] + 0.114 * tex_final[:, :, 2]).astype(np.float32)
    gx, gy = cv2.Sobel(gray_t, cv2.CV_32F, 1, 0, ksize=3) / 4.0, cv2.Sobel(gray_t, cv2.CV_32F, 0, 1, ksize=3) / 4.0
    rel = np.full(len(P), 0.25, np.float32); rel[tex_face] = 0.06; rel[eye_zone] = 0.05; rel[hair_mask] = 1.20
    ntan = np.stack([np.sum(N_bw * Tm, axis=1) - gx[vy, vx] * rel, np.sum(N_bw * Bm, axis=1) + gy[vy, vx] * rel, np.maximum(np.sum(N_bw * Nm, axis=1), 0.25)], axis=1)
    ntan /= np.maximum(np.linalg.norm(ntan, axis=1, keepdims=True), 1e-8)
    n_uv = np.zeros((TEX, TEX, 3), np.float32); n_uv[:, :] = [0.5, 0.5, 1.0]; n_uv[vy, vx] = np.clip(ntan * 0.5 + 0.5, 0.0, 1.0)
    normal_path = final_dir / "normal_2048.png"
    Image.fromarray((np.clip(_dilate(n_uv, valid_m, 6), 0.0, 1.0) * 255.0).astype(np.uint8), mode="RGB").save(normal_path)

    mat = PBRMaterial(name="head_v9_mat", baseColorTexture=Image.open(albedo_path), normalTexture=Image.open(normal_path), baseColorFactor=[1.0, 1.0, 1.0, 1.0], metallicFactor=0.0, roughnessFactor=0.72, doubleSided=True)
    raw_glb = final_dir / "_raw_head.glb"
    glb_out = final_dir / "head-textured.glb"
    trimesh.Trimesh(vertices=V_uv, faces=F_uv, vertex_normals=N_uv, visual=trimesh.visual.TextureVisuals(uv=uvs, material=mat), process=False).export(str(raw_glb))
    strip_file(str(raw_glb), str(glb_out))
    raw_glb.unlink(missing_ok=True)
    if find_extensions(read_glb(str(glb_out))[2]):
        raise RuntimeError("导出 GLB 仍残留 Filament 禁用扩展")
    print(f"[Bake] 导出清洁 GLB: {glb_out} ({glb_out.stat().st_size / 1e6:.2f} MB)", flush=True)
    return final_dir


def stage_preview(priors_dir: Path, model_dir: Path, final_dir: Path, out_dir: Path) -> Path:
    from arguments import ModelParams
    from scene import GaussianModel, Scene
    import camera_utils as cu
    import mesh_render as mr

    vis_dir = out_dir / "visuals"
    vis_dir.mkdir(parents=True, exist_ok=True)
    refined = json.load(open(model_dir / "refined_poses.json", encoding="utf-8"))
    anchors = json.load(open(priors_dir / "auto_anchors.json", encoding="utf-8"))
    tm = trimesh.load(str(final_dir / "head-textured.glb"), force="mesh", process=False)
    v, f, uv = np.asarray(tm.vertices, np.float64), np.asarray(tm.faces, np.int64), np.asarray(tm.visual.uv, np.float64)
    tex = np.asarray(tm.visual.material.baseColorTexture.convert("RGB"), np.float64) / 255.0

    p = ArgumentParser(); mp = ModelParams(p, sentinel=True)
    cfg = eval(open(model_dir / "cfg_args", encoding="utf-8").read())  # noqa: S307
    ds = mp.extract(Namespace(**{**vars(cfg), "model_path": str(model_dir)}))
    preview_it = int(json.load(open(model_dir / "train_stats.json", encoding="utf-8"))["iterations"])
    sc = Scene(ds, GaussianModel(ds.sh_degree), load_iteration=preview_it, shuffle=False)
    all_cams = sc.getTrainCameras() + sc.getTestCameras()
    ups = [ -np.linalg.inv(np.vstack([np.hstack([np.array(refined[Path(c.image_name).stem]["R_refined"]), np.array(refined[Path(c.image_name).stem]["t_refined"])[:, None]]), [0, 0, 0, 1]]))[:3, 1] for c in all_cams]
    up_axis = np.mean(ups, axis=0); up_axis /= np.linalg.norm(up_axis)
    proj_up = v @ up_axis
    center = np.median(v[proj_up >= np.percentile(proj_up, 58)], axis=0)
    head_span = float(np.percentile(proj_up, 99) - np.percentile(proj_up, 62))

    ref_cam = next(c for c in all_cams if Path(c.image_name).stem == anchors["ref_stem"])
    K0 = cu.intrinsics(ref_cam); tw, th = 360, 640
    K_tt = K0.copy(); K_tt[0, :] *= tw / float(ref_cam.image_width); K_tt[1, :] *= th / float(ref_cam.image_height)
    Rr, tr = np.array(refined[anchors["ref_stem"]]["R_refined"]), np.array(refined[anchors["ref_stem"]]["t_refined"])
    C0 = -Rr.T @ tr
    rad = C0 - center - np.dot(C0 - center, up_axis) * up_axis
    dir0 = rad / np.linalg.norm(rad)
    # Use 2.55 factor to frame whole head + nose comfortably without cropping
    radius = max(head_span * (float(K_tt[1, 1]) / float(th)) * 2.55, 2.5)

    # Lightweight textured rasterizer
    from trimesh.visual import color as _col  # noqa: F401
    cw, ch = 240, 426
    sheet = Image.new("RGB", (4 * cw + 5 * 14, 2 * ch + 3 * 14 + 56), (20, 22, 28))
    td = ImageDraw.Draw(sheet)
    f_h, f_c = _load_font(20), _load_font(16)
    td.text((16, 14), f"V9 通用头部重建 8 视角预览（{len(f):,} 面，纯前景+头部 6-DoF 对齐）", fill=(245, 248, 255), font=f_h)

    def _rodrigues(axis, theta):
        axis = axis / np.linalg.norm(axis)
        K = np.array([[0, -axis[2], axis[1]], [axis[2], 0, -axis[0]], [-axis[1], axis[0], 0]], np.float64)
        return np.eye(3) + math.sin(theta) * K + (1.0 - math.cos(theta)) * (K @ K)

    th_tex, tw_tex = tex.shape[:2]
    for k, deg in enumerate((0, 45, 90, 135, 180, 225, 270, 315)):
        cam_pos = center + radius * (_rodrigues(up_axis, math.radians(deg)) @ dir0) + 0.02 * radius * up_axis
        fwd = (center - cam_pos); fwd /= np.linalg.norm(fwd)
        right = np.cross(fwd, up_axis); right /= np.linalg.norm(right)
        down = np.cross(fwd, right)
        R_tt, t_tt = np.stack([right, down, fwd], axis=0), -np.stack([right, down, fwd], axis=0) @ cam_pos
        vc = (R_tt @ v.T).T + t_tt; z = vc[:, 2]; eps = 1e-6; sz = np.where(z > eps, z, eps)
        u, wp = K_tt[0, 0] * vc[:, 0] / sz + K_tt[0, 2], K_tt[1, 1] * vc[:, 1] / sz + K_tt[1, 2]
        fn = mr.face_normals(v, f); ld = cam_pos[None, :] - v[f].mean(axis=1); ld /= np.maximum(np.linalg.norm(ld, axis=1, keepdims=True), 1e-12)
        fn = fn * np.where(np.sign((fn * ld).sum(axis=1)) == 0, 1.0, np.sign((fn * ld).sum(axis=1)))[:, None]
        shade = 0.72 + 0.28 * np.clip((fn * ld).sum(axis=1), 0.0, 1.0)
        rgb, zb = np.full((th, tw, 3), [0.08, 0.09, 0.11], np.float64), np.full((th, tw), np.inf, np.float64)
        u0, u1, u2 = u[f[:, 0]], u[f[:, 1]], u[f[:, 2]]; v0, v1, v2 = wp[f[:, 0]], wp[f[:, 1]], wp[f[:, 2]]; z0, z1, z2 = z[f[:, 0]], z[f[:, 1]], z[f[:, 2]]; uv0, uv1, uv2 = uv[f[:, 0]], uv[f[:, 1]], uv[f[:, 2]]
        vis = (z0 > eps) & (z1 > eps) & (z2 > eps)
        umin, umax, vmin, vmax = np.floor(np.minimum.reduce([u0, u1, u2])).astype(int), np.ceil(np.maximum.reduce([u0, u1, u2])).astype(int), np.floor(np.minimum.reduce([v0, v1, v2])).astype(int), np.ceil(np.maximum.reduce([v0, v1, v2])).astype(int)
        vis &= (umax >= 0) & (umin < tw) & (vmax >= 0) & (vmin < th) & ((umax - umin) < 2048) & ((vmax - vmin) < 2048)
        for i in np.nonzero(vis)[0]:
            x0, x1, y0, y1 = max(int(umin[i]), 0), min(int(umax[i]), tw - 1), max(int(vmin[i]), 0), min(int(vmax[i]), th - 1)
            if x1 < x0 or y1 < y0:
                continue
            d = (v1[i] - v2[i]) * (u0[i] - u2[i]) + (u2[i] - u1[i]) * (v0[i] - v2[i])
            if abs(d) < 1e-12:
                continue
            px, py = np.meshgrid(np.arange(x0, x1 + 1), np.arange(y0, y1 + 1))
            l1, l2 = ((v1[i] - v2[i]) * (px - u2[i]) + (u2[i] - u1[i]) * (py - v2[i])) / d, ((v2[i] - v0[i]) * (px - u2[i]) + (u0[i] - u2[i]) * (py - v2[i])) / d
            ins = (l1 >= -1e-9) & (l2 >= -1e-9) & ((1.0 - l1 - l2) >= -1e-9)
            if not ins.any():
                continue
            w1_, w2_, w3_ = l1 / z0[i], l2 / z1[i], (1.0 - l1 - l2) / z2[i]
            zz = 1.0 / np.maximum(w1_ + w2_ + w3_, 1e-12)
            sub_z = zb[y0:y1+1, x0:x1+1]
            wr = ins & (zz < sub_z)
            if not wr.any():
                continue
            sub_z[wr] = zz[wr]
            ww1, ww2, ww3 = w1_[wr] * zz[wr], w2_[wr] * zz[wr], w3_[wr] * zz[wr]
            tx_ = np.clip(np.rint(np.clip(ww1 * uv0[i, 0] + ww2 * uv1[i, 0] + ww3 * uv2[i, 0], 0.0, 1.0) * (tw_tex - 1)).astype(np.int32), 0, tw_tex - 1)
            ty_ = np.clip(np.rint((1.0 - np.clip(ww1 * uv0[i, 1] + ww2 * uv1[i, 1] + ww3 * uv2[i, 1], 0.0, 1.0)) * (th_tex - 1)).astype(np.int32), 0, th_tex - 1)
            rgb[y0:y1+1, x0:x1+1][wr] = tex[ty_, tx_] * shade[i]

        im_p = Image.fromarray((np.clip(rgb, 0.0, 1.0) * 255.0).astype(np.uint8)).resize((cw, ch), Image.Resampling.LANCZOS)
        r_i, c_i = divmod(k, 4)
        px_c, py_c = 14 + c_i * (cw + 14), 56 + 14 + r_i * (ch + 14)
        sheet.paste(im_p, (px_c, py_c))
        td.rectangle([px_c, py_c, px_c + cw - 1, py_c + ch - 1], outline=(68, 76, 92), width=1)
        td.text((px_c + 8, py_c + 8), f"{deg}°", fill=(140, 245, 180), font=f_c)

    out_png = vis_dir / "head_turntable.png"
    sheet.save(out_png, quality=95)
    print(f"[Preview] 保存 8 视角预览图: {out_png}", flush=True)
    return out_png


def main() -> int:
    parser = ArgumentParser(description="Zero-hardcode V9 3D Head Reconstruction CLI")
    parser.add_argument("--video", type=str, default=None, help="Input 360-degree portrait MP4/MOV video")
    parser.add_argument("--images-dir", type=str, default=None, help="Existing extracted image directory")
    parser.add_argument("--output-dir", type=str, required=True, help="Isolated output directory")
    parser.add_argument("--stages", type=str, default="all", help="Comma-separated stages: extract,priors,sfm,train,mesh,bake,preview")
    parser.add_argument("--target-frames", type=int, default=85)
    parser.add_argument("--iterations", type=int, default=15000)
    parser.add_argument("--fast", action="store_true", help="Fast 10,000-iteration mode")
    parser.add_argument("--no-sapiens", action="store_true", help="Skip Sapiens2-Normal-0.4B (uses DA2 depth/grad only)")
    parser.add_argument("--skip-head-sec", type=float, default=1.5)
    parser.add_argument("--skip-tail-sec", type=float, default=1.0)
    parser.add_argument("--cameras-bin", type=str, default=None, help="Optional pre-calibrated COLMAP cameras.bin")
    parser.add_argument("--gs2d-repo", type=str, default=DEFAULT_2DGS_REPO)
    parser.add_argument("--eval-dir", type=str, default=DEFAULT_EVAL_DIR)
    args = parser.parse_args()

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stages = list(ALL_STAGES) if args.stages.strip().lower() == "all" else [s.strip().lower() for s in args.stages.split(",") if s.strip()]
    for s in stages:
        if s not in ALL_STAGES:
            raise ValueError(f"未知阶段: {s}，可选 {ALL_STAGES}")

    _inject_external_paths(args.gs2d_repo, args.eval_dir)
    t0 = time.time()
    raw_dir = out_dir / "raw_frames"
    priors_dir = out_dir / "priors"
    data_fg = out_dir / "2dgs_data_fg"
    model_dir = out_dir / "2dgs_out"
    mesh_dir = out_dir / "mesh"
    final_dir = out_dir / "final"

    if "extract" in stages:
        raw_dir = stage_extract(args, out_dir)
    if "priors" in stages:
        priors_dir = stage_priors(args, raw_dir, out_dir)
    if "sfm" in stages:
        data_fg = stage_sfm(args, raw_dir, priors_dir, out_dir)
    if "train" in stages:
        model_dir = stage_train(args, data_fg, priors_dir, out_dir)
    if "mesh" in stages:
        mesh_dir = stage_mesh(args, data_fg, priors_dir, model_dir, out_dir)
    if "bake" in stages:
        final_dir = stage_bake(args, data_fg, priors_dir, model_dir, mesh_dir, out_dir)
    if "preview" in stages:
        stage_preview(priors_dir, model_dir, final_dir, out_dir)

    print(f"[Done] 所选阶段 {stages} 全部完成，耗时 {time.time() - t0:.1f}s。", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
