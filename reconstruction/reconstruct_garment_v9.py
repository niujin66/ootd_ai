"""Zero-hardcode general V9 3D garment reconstruction pipeline.

Migrates the three highest-ROI capabilities from V9 head reconstruction to garments:
1. SegFormer clothing segmentation + strict non-bloated masks (eliminates trouser/floor bleed)
   with GrabCut fallback for empty-hanger captures;
2. Foreground-masked SuperPoint + LightGlue SfM + automatic SIMPLE_RADIAL undistortion;
3. Backlit shadow lift + smooth facing^5 seamless 2048 UV baking + Filament extension guard.
"""
from __future__ import annotations

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

DEFAULT_2DGS_REPO = os.environ.get("OOTD_2DGS_REPO", r"D:\gsscan\vendor\2d-gaussian-splatting")
DEFAULT_EVAL_DIR = os.environ.get("OOTD_GSSCAN_EVAL_DIR", r"D:\gsscan\src\eval")
REPO_ROOT = Path(__file__).resolve().parent.parent
SERVER_TOOLS_DIR = REPO_ROOT / "server" / "tools"
if str(SERVER_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_TOOLS_DIR))
from export_glb_guard import find_extensions, read_glb, strip_file  # noqa: E402

GARMENT_CLASS_MAP = {
    "upper": (4, 7),          # Upper-clothes (+ Dress side/back close-ups for hoodies/long coats)
    "dress": (7,),            # Dress
    "lower": (5, 6),          # Skirt, Pants
    "all": (4, 5, 6, 7, 17),  # All clothing + Scarf
}
FG_ALL_CLASSES = tuple(range(1, 18))
ARM_SKIN_CLASSES = (14, 15)   # Left-arm, Right-arm
BAKE_BODY_BAN_CLASSES = (6, 9, 10, 11, 12, 13, 14, 15)  # Pants, Shoes, Face/neck, Legs, Arms


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


def _keep_largest_cc(mask_u8: np.ndarray) -> np.ndarray:
    num_cc, labels, stats, _ = cv2.connectedComponentsWithStats((mask_u8 > 127).astype(np.uint8), connectivity=8)
    if num_cc <= 1:
        return (mask_u8 > 127).astype(np.uint8) * 255
    largest = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    return np.where(labels == largest, 255, 0).astype(np.uint8)


def _fill_small_holes(mask_u8: np.ndarray, max_hole_ratio: float = 0.25) -> np.ndarray:
    """Fill enclosed dark interior holes (e.g., hoodie pocket shadows) without touching border gaps."""
    fg = (mask_u8 > 127).astype(np.uint8)
    fg_area = int(fg.sum())
    if fg_area == 0:
        return mask_u8
    H, W = fg.shape
    num_cc, labels, stats, _ = cv2.connectedComponentsWithStats(1 - fg, connectivity=4)
    out = fg.copy()
    for cid in range(1, num_cc):
        x, y, w, h, area = [int(v) for v in stats[cid]]
        if x == 0 or y == 0 or x + w >= W or y + h >= H:
            continue
        if area <= int(max_hole_ratio * fg_area):
            out[labels == cid] = 1
    return (out * 255).astype(np.uint8)


def _grabcut_center_fallback(bgr: np.ndarray) -> np.ndarray:
    """Fallback segmentation for empty-hanger or non-human garment shots without SegFormer hits."""
    H, W = bgr.shape[:2]
    mask = np.zeros((H, W), dtype=np.uint8)
    rect = (int(W * 0.14), int(H * 0.10), int(W * 0.72), int(H * 0.80))
    bgd_model = np.zeros((1, 65), np.float64)
    fgd_model = np.zeros((1, 65), np.float64)
    try:
        cv2.grabCut(bgr, mask, rect, bgd_model, fgd_model, 4, cv2.GC_INIT_WITH_RECT)
        bin_m = np.where((mask == cv2.GC_FGD) | (mask == cv2.GC_PR_FGD), 255, 0).astype(np.uint8)
        return _keep_largest_cc(bin_m)
    except Exception:
        out = np.zeros((H, W), dtype=np.uint8)
        out[rect[1] : rect[1] + rect[3], rect[0] : rect[0] + rect[2]] = 255
        return out


# ---------------------------------------------------------------------------
# Stage 1: Frame Extraction & Blur Filtering
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
        for k, fidx in enumerate(np.unique(np.linspace(start_idx, end_idx - 1, num=n_cand, dtype=int))):
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(fidx))
            ok, bgr = cap.read()
            if ok and bgr is not None:
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
            if bgr is not None:
                p = cand_dir / f"cand-{k:05d}.jpg"
                cv2.imwrite(str(p), bgr, [cv2.IMWRITE_JPEG_QUALITY, 96])
                candidates.append(p)
    else:
        raise ValueError("必须提供 --video 或 --images-dir")

    # Filter severe motion-blur outliers using central garment crop Laplacian variance
    scored = []
    for p in candidates:
        gray = cv2.imread(str(p), cv2.IMREAD_GRAYSCALE)
        H, W = gray.shape
        roi = gray[int(H * 0.15) : int(H * 0.85), int(W * 0.15) : int(W * 0.85)]
        sharp = float(cv2.Laplacian(roi, cv2.CV_32F).var())
        scored.append((p, sharp))

    med_sharp = float(np.median([x[1] for x in scored]))
    crisp = [x for x in scored if x[1] >= 0.25 * med_sharp]
    if len(crisp) < min(30, len(scored)):
        crisp = scored
    if len(crisp) > args.target_frames:
        chosen = [crisp[i] for i in np.linspace(0, len(crisp) - 1, num=args.target_frames, dtype=int)]
    else:
        chosen = crisp

    for existing in raw_dir.glob("*.jpg"):
        existing.unlink()
    for i, (sp, _) in enumerate(chosen):
        shutil.copy2(sp, raw_dir / f"frame-{i:05d}.jpg")
    shutil.rmtree(cand_dir, ignore_errors=True)

    meta = {
        "candidate_frames": len(candidates),
        "selected_frames": len(chosen),
        "median_sharpness": round(med_sharp, 2),
    }
    with open(out_dir / "extract_meta.json", "w", encoding="utf-8") as fh:
        json.dump(meta, fh, ensure_ascii=False, indent=2)
    print(f"[Extract] 保留 {len(chosen)}/{len(candidates)} 帧清晰衣物画面 -> {raw_dir}", flush=True)
    return raw_dir


# ---------------------------------------------------------------------------
# Stage 2: SegFormer Strict Garment Segmentation + Optional DA2 Depth Priors
# ---------------------------------------------------------------------------
def stage_priors(args, images_dir: Path, out_dir: Path) -> Path:
    priors_dir = out_dir / "priors"
    sem_dir = priors_dir / "sem"
    g_mask_dir = priors_dir / "garment_masks"
    all_fg_dir = priors_dir / "all_fg_masks"
    kp_mask_dir = priors_dir / "sfm_kp_masks"
    disp_dir = priors_dir / "disp"
    vis_dir = out_dir / "visuals"
    for d in (sem_dir, g_mask_dir, all_fg_dir, kp_mask_dir, disp_dir, vis_dir):
        d.mkdir(parents=True, exist_ok=True)

    target_cls = GARMENT_CLASS_MAP[args.garment_part]
    stems = sorted(p.stem for p in images_dir.glob("*.jpg"))
    print(f"[Priors 1/2] 运行 SegFormer 精准衣物分割 ({args.garment_part} -> {target_cls}, {len(stems)} 帧)...", flush=True)

    seg_proc = transformers.AutoImageProcessor.from_pretrained(SEGFORMER_ID)
    seg_model = transformers.SegformerForSemanticSegmentation.from_pretrained(SEGFORMER_ID).eval().cuda()
    k_dil5 = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (11, 11))
    k_dil3 = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7))
    k_open = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))

    fallback_frames = 0
    garment_ratios = []
    preview_idxs = set(np.linspace(0, max(0, len(stems) - 1), num=min(6, len(stems)), dtype=int).tolist())
    preview_items = []

    for i, stem in enumerate(stems):
        bgr = cv2.imread(str(images_dir / f"{stem}.jpg"), cv2.IMREAD_COLOR)
        pil = Image.fromarray(bgr[:, :, ::-1], mode="RGB")
        W, H = pil.size
        inp = seg_proc(images=pil, return_tensors="pt").to("cuda")
        with torch.no_grad():
            logits = seg_model(**inp).logits
        sem = F.interpolate(logits, size=(H, W), mode="bilinear", align_corners=False).argmax(dim=1)[0].cpu().numpy().astype(np.uint8)

        g_raw = np.isin(sem, target_cls).astype(np.uint8) * 255
        g_clean = _fill_small_holes(_keep_largest_cc(cv2.morphologyEx(g_raw, cv2.MORPH_OPEN, k_open)))
        ratio = float((g_clean > 127).mean())
        used_fb = False
        if ratio < 0.010:
            g_clean = _grabcut_center_fallback(bgr)
            ratio = float((g_clean > 127).mean())
            fallback_frames += 1
            used_fb = True
        garment_ratios.append(ratio)

        all_fg = np.isin(sem, FG_ALL_CLASSES).astype(np.uint8) * 255
        all_fg = np.maximum(all_fg, g_clean)
        all_fg = cv2.dilate(_keep_largest_cc(all_fg), k_dil3)

        # Zero-dilation strict mask for photometric training/baking; 5px dilated for SfM edges
        kp_m = cv2.dilate(g_clean, k_dil5)
        Image.fromarray(sem, mode="L").save(sem_dir / f"{stem}.png")
        Image.fromarray(g_clean, mode="L").save(g_mask_dir / f"{stem}.png")
        Image.fromarray(all_fg, mode="L").save(all_fg_dir / f"{stem}.png")
        Image.fromarray(kp_m, mode="L").save(kp_mask_dir / f"{stem}.png")

        if i in preview_idxs:
            overlay = bgr[:, :, ::-1].astype(np.float32).copy()
            m_b = g_clean > 127
            overlay[~m_b] *= 0.25
            edge = (cv2.dilate(g_clean, k_dil3) > 127) & (~m_b)
            overlay[edge] = [60, 235, 130]
            preview_items.append((stem, Image.fromarray(np.clip(overlay, 0, 255).astype(np.uint8)), ratio, used_fb))

    del seg_model
    torch.cuda.empty_cache()

    if preview_items:
        cw, ch, pad = 210, 373, 12
        sheet = Image.new("RGB", (len(preview_items) * cw + (len(preview_items) + 1) * pad, ch + 2 * pad + 48), (20, 22, 28))
        draw = ImageDraw.Draw(sheet)
        f_h, f_s = _load_font(18), _load_font(12)
        draw.text((pad, 12), f"SegFormer 衣物分割校验 ({args.garment_part}, 严格零外扩掩码, GrabCut回退={fallback_frames}帧)", fill=(245, 248, 255), font=f_h)
        for col, (st, ov_im, rat, fb) in enumerate(preview_items):
            x, y = pad + col * (cw + pad), 48 + pad
            sheet.paste(ov_im.resize((cw, ch), Image.Resampling.LANCZOS), (x, y))
            tag = f"{st} ({rat*100:.1f}%{' FB' if fb else ''})"
            draw.text((x + 4, y + 4), tag, fill=(140, 245, 180), font=f_s)
        sheet.save(vis_dir / "garment_masks_preview.png", quality=92)

    if args.use_da2:
        print(f"[Priors 2/2] 运行 Depth-Anything-V2 单目视差先验 ({len(stems)} 帧)...", flush=True)
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
            np.save(disp_dir / f"{stem}.npy", ((disp - d_min) / max(d_max - d_min, 1e-6)).astype(np.float32))
        del da_model
        torch.cuda.empty_cache()

    meta = {
        "garment_part": args.garment_part,
        "frames": len(stems),
        "grabcut_fallback_frames": fallback_frames,
        "garment_area_min_p50_max": [round(float(min(garment_ratios)), 4), round(float(np.median(garment_ratios)), 4), round(float(max(garment_ratios)), 4)],
        "use_da2": bool(args.use_da2),
    }
    with open(priors_dir / "garment_priors_meta.json", "w", encoding="utf-8") as fh:
        json.dump(meta, fh, ensure_ascii=False, indent=2)
    print(f"[Priors] 分割完成: median_ratio={meta['garment_area_min_p50_max'][1]*100:.1f}%, fallback={fallback_frames}", flush=True)
    return priors_dir


# ---------------------------------------------------------------------------
# Stage 3: SuperPoint + LightGlue Foreground-Masked Garment SfM
# ---------------------------------------------------------------------------
def stage_sfm(args, raw_images_dir: Path, priors_dir: Path, out_dir: Path) -> Path:
    data_g = out_dir / "2dgs_data_garment"
    g_masks_dir = priors_dir / "garment_masks"
    if data_g.exists():
        shutil.rmtree(data_g)
    data_g.mkdir(parents=True, exist_ok=True)

    stems = sorted(p.stem for p in raw_images_dir.glob("*.jpg"))
    k_er7 = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7))
    g_masks: dict[str, np.ndarray] = {}
    er_masks: dict[str, np.ndarray] = {}
    W = H = 0
    for stem in stems:
        m_g = (np.asarray(Image.open(g_masks_dir / f"{stem}.png").convert("L"), dtype=np.uint8) > 127)
        H, W = m_g.shape
        g_u8 = m_g.astype(np.uint8) * 255
        g_masks[stem] = m_g
        er_masks[stem] = cv2.erode(g_u8, k_er7) > 127

    db_path = out_dir / "_sfm_garment.db"
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
    pycolmap.extract_features(
        str(db_path), str(raw_images_dir),
        camera_mode=pycolmap.CameraMode.SINGLE, reader_options=reader_opts, extraction_options=ext_opts,
    )

    lg_model = transformers.LightGlueForKeypointMatching.from_pretrained(LIGHTGLUE_ID).eval().cuda()
    lg_model.depth_confidence = -1.0
    lg_model.width_confidence = -1.0

    # Pad to multiple of 8 so arbitrary phone resolutions (e.g., 900x1600) run cleanly in SuperPoint
    pad_h, pad_w = (8 - H % 8) % 8, (8 - W % 8) % 8
    H_pad, W_pad = H + pad_h, W + pad_w

    garment_kp_cache: dict[str, tuple[torch.Tensor, torch.Tensor]] = {}
    full_kp_cache: dict[str, tuple[torch.Tensor, torch.Tensor]] = {}
    garment_kp_counts: dict[str, int] = {}
    for stem in stems:
        img = cv2.imread(str(raw_images_dir / f"{stem}.jpg"), cv2.IMREAD_COLOR)[:, :, ::-1].astype(np.float32) / 255.0
        gray = 0.299 * img[:, :, 0] + 0.587 * img[:, :, 1] + 0.114 * img[:, :, 2]
        if pad_h or pad_w:
            gray = np.pad(gray, ((0, pad_h), (0, pad_w)), mode="constant", constant_values=0.0)
        t_in = torch.from_numpy(gray)[None, None].repeat(1, 3, 1, 1).cuda()
        with torch.no_grad():
            kp_norm, scores, desc, det_mask = lg_model.keypoint_detector(t_in)[:4]
        valid = det_mask[0] > 0
        kpts_xy = kp_norm[0][valid].clone()
        kpts_xy[:, 0] *= float(W_pad)
        kpts_xy[:, 1] *= float(H_pad)
        sc, ds = scores[0][valid], desc[0][valid]

        in_img = (kpts_xy[:, 0] >= 0) & (kpts_xy[:, 0] < W) & (kpts_xy[:, 1] >= 0) & (kpts_xy[:, 1] < H)
        kpts_xy, ds, sc = kpts_xy[in_img], ds[in_img], sc[in_img]

        if len(kpts_xy) > 2048:
            top_f = torch.topk(sc, 2048).indices
            full_kp_cache[stem] = (kpts_xy[top_f].contiguous(), ds[top_f].contiguous())
        else:
            full_kp_cache[stem] = (kpts_xy.contiguous(), ds.contiguous())

        ui = torch.clamp(torch.round(kpts_xy[:, 0]).long(), 0, W - 1).cpu().numpy()
        vi = torch.clamp(torch.round(kpts_xy[:, 1]).long(), 0, H - 1).cpu().numpy()
        fg_keep = torch.from_numpy(er_masks[stem][vi, ui]).to(kpts_xy.device)
        if int(fg_keep.sum()) < 80:
            fg_keep = torch.from_numpy(g_masks[stem][vi, ui]).to(kpts_xy.device)
        k_in, d_in, s_in = kpts_xy[fg_keep], ds[fg_keep], sc[fg_keep]
        if len(k_in) > 2048:
            top_g = torch.topk(s_in, 2048).indices
            k_in, d_in = k_in[top_g], d_in[top_g]
        garment_kp_cache[stem] = (k_in.contiguous(), d_in.contiguous())
        garment_kp_counts[stem] = int(len(k_in))

    N = len(stems)
    if N <= 100:
        pairs = [(i, j) for i in range(N) for j in range(i + 1, N)]
    else:
        pair_set = set()
        for i in range(N):
            for d in range(1, 23):
                j = (i + d) % N
                pair_set.add((min(i, j), max(i, j)))
            for j in range(0, N, 4):
                if i != j:
                    pair_set.add((min(i, j), max(i, j)))
        pairs = sorted(pair_set)

    lock_intr = pinhole_params is not None
    inc_opts = pycolmap.IncrementalPipelineOptions()
    inc_opts.ba_refine_focal_length = not lock_intr
    inc_opts.ba_refine_principal_point = False
    inc_opts.ba_refine_extra_params = not lock_intr
    inc_opts.mapper.init_min_num_inliers = 30
    inc_opts.mapper.abs_pose_min_num_inliers = 15

    with pycolmap.Database.open(str(db_path)) as db:
        imgs_db = db.read_all_images()
        name_to_id = {Path(im.name).stem: int(im.image_id) for im in imgs_db}

    def _run_lg_mapping(kp_cache_dict: dict[str, tuple[torch.Tensor, torch.Tensor]], pass_name: str, sparse_dir: Path):
        max_k = max(1, max(int(len(v[0])) for v in kp_cache_dict.values()))
        kp_pad = torch.zeros((N, max_k, 2), dtype=torch.float32, device="cuda")
        desc_pad = torch.zeros((N, max_k, 256), dtype=torch.float32, device="cuda")
        mask_pad = torch.zeros((N, max_k), dtype=torch.int64, device="cuda")
        counts = {}
        for idx, stem in enumerate(stems):
            kt, dt = kp_cache_dict[stem]
            counts[stem] = int(len(kt))
            if len(kt) > 0:
                kp_pad[idx, : len(kt)] = kt
                desc_pad[idx, : len(kt)] = dt
                mask_pad[idx, : len(kt)] = 1

        with pycolmap.Database.open(str(db_path)) as db:
            if pinhole_params is not None:
                for cam in db.read_all_cameras():
                    cam.params = np.array(pinhole_params, dtype=np.float64)
                    db.update_camera(cam)
            db.clear_keypoints()
            db.clear_descriptors()
            db.clear_matches()
            db.clear_two_view_geometries()
            for stem in stems:
                db.write_keypoints(name_to_id[stem], kp_cache_dict[stem][0].cpu().numpy().astype(np.float32))

        kept_pairs: list[tuple[str, str]] = []
        match_counts: list[int] = []
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
                m0 = m_out.reshape(B_c, 2, max_k)[:, 0].cpu().numpy()
                sc0 = s_out.reshape(B_c, 2, max_k)[:, 0].cpu().numpy()
                for b, (ia, ib) in enumerate(chunk):
                    na, nb = counts[stems[ia]], counts[stems[ib]]
                    ma, sa = m0[b, :na], sc0[b, :na]
                    v = (ma >= 0) & (ma < nb) & (sa >= 0.15)
                    nm = int(v.sum())
                    if nm >= 15:
                        mu32 = np.stack([np.nonzero(v)[0].astype(np.uint32), ma[v].astype(np.uint32)], axis=1)
                        ida, idb = name_to_id[stems[ia]], name_to_id[stems[ib]]
                        if ida > idb:
                            ida, idb = idb, ida
                            mu32 = mu32[:, ::-1].copy()
                        db.write_matches(ida, idb, mu32)
                        kept_pairs.append((f"{stems[ia]}.jpg", f"{stems[ib]}.jpg"))
                        match_counts.append(nm)

        pairs_path = out_dir / f"_pairs_{pass_name}.txt"
        with open(pairs_path, "w", encoding="utf-8") as fh:
            for a, b in kept_pairs:
                fh.write(f"{a} {b}\n")
        if kept_pairs:
            pycolmap.verify_matches(str(db_path), str(pairs_path))

        if sparse_dir.exists():
            shutil.rmtree(sparse_dir)
        sparse_dir.mkdir(parents=True, exist_ok=True)
        recs = pycolmap.incremental_mapping(str(db_path), str(raw_images_dir), str(sparse_dir), options=inc_opts)
        if not recs:
            return None, len(kept_pairs), match_counts
        return recs[max(recs, key=lambda k: len(recs[k].images))], len(kept_pairs), match_counts

    sparse_raw = out_dir / "_sparse_raw"
    rec1, n_pairs1, m_cnts1 = _run_lg_mapping(garment_kp_cache, "pass1", sparse_raw / "pass1")
    pass1_reg = len(rec1.images) if rec1 is not None else 0
    best, kept_pairs_cnt, match_counts, sfm_mode = rec1, n_pairs1, m_cnts1, "garment_interior"

    if pass1_reg < int(0.80 * N):
        print(f"[SfM] Pass1 衣物内部关键点注册 {pass1_reg}/{N} (<80%)，启动无遮罩整图 SuperPoint+LightGlue 回退...", flush=True)
        rec2, n_pairs2, m_cnts2 = _run_lg_mapping(full_kp_cache, "pass2", sparse_raw / "pass2")
        if rec2 is not None and len(rec2.images) > pass1_reg:
            best, kept_pairs_cnt, match_counts, sfm_mode = rec2, n_pairs2, m_cnts2, "full_frame_fallback"

    del lg_model
    torch.cuda.empty_cache()

    if best is None:
        raise RuntimeError("衣物 SuperPoint+LightGlue SfM 失败（0 个有效子模型）。")
    raw_best_dir = sparse_raw / "best"
    raw_best_dir.mkdir(parents=True, exist_ok=True)
    best.write(str(raw_best_dir))

    (data_g / "images").mkdir(parents=True, exist_ok=True)
    (data_g / "masks").mkdir(parents=True, exist_ok=True)
    (data_g / "sparse" / "0").mkdir(parents=True, exist_ok=True)

    if lock_intr:
        best.write(str(data_g / "sparse" / "0"))
        src_for_mask = raw_images_dir
    else:
        undist_tmp = out_dir / "_undist_tmp"
        if undist_tmp.exists():
            shutil.rmtree(undist_tmp)
        pycolmap.undistort_images(str(undist_tmp), str(raw_best_dir), str(raw_images_dir))
        shutil.copytree(undist_tmp / "sparse", data_g / "sparse" / "0", dirs_exist_ok=True)
        src_for_mask = undist_tmp / "images"

    for stem in stems:
        im_p = src_for_mask / f"{stem}.jpg"
        if not im_p.exists():
            continue
        bgr = cv2.imread(str(im_p), cv2.IMREAD_COLOR)
        Hi, Wi = bgr.shape[:2]
        m_u8 = np.asarray(Image.open(g_masks_dir / f"{stem}.png").convert("L"), dtype=np.uint8)
        if m_u8.shape != (Hi, Wi):
            m_u8 = cv2.resize(m_u8, (Wi, Hi), interpolation=cv2.INTER_NEAREST)
        m_bin = (m_u8 > 127).astype(np.uint8) * 255
        # Strictly zero outward dilation -> never drags background/trousers onto garment hems
        soft_m = np.where(m_bin > 127, 1.0, cv2.GaussianBlur(m_bin.astype(np.float32) / 255.0, (0, 0), sigmaX=1.0))
        bgr_clean = np.clip(bgr.astype(np.float32) * soft_m[:, :, None], 0, 255).astype(np.uint8)
        cv2.imwrite(str(data_g / "images" / f"{stem}.jpg"), bgr_clean, [cv2.IMWRITE_JPEG_QUALITY, 96])
        Image.fromarray(m_bin, mode="L").save(data_g / "masks" / f"{stem}.png")

    report = {
        "total_frames": N,
        "registered_frames": len(best.images),
        "pass1_registered_frames": pass1_reg,
        "sfm_keypoint_mode": sfm_mode,
        "num_points3D": len(best.points3D),
        "mean_reprojection_error_px": round(float(best.compute_mean_reprojection_error()), 4),
        "mean_track_length": round(float(best.compute_mean_track_length()), 3),
        "kept_match_pairs": kept_pairs_cnt,
        "median_matches_per_pair": int(np.median(match_counts)) if match_counts else 0,
        "min_garment_kp": min(garment_kp_counts.values()),
        "median_garment_kp": int(np.median(list(garment_kp_counts.values()))),
        "intrinsics_mode": "locked_pinhole" if lock_intr else "estimated_simple_radial_undistorted",
    }
    with open(data_g / "sfm_report.json", "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    return data_g


# ---------------------------------------------------------------------------
# Stage 4–7: Pure-Garment 2DGS, Mesh Extraction, Seamless Bake & Preview
# ---------------------------------------------------------------------------
def _axis_angle_to_quat_and_mat(w: torch.Tensor):
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


def render_garment(viewpoint_camera, pc, pipe, bg_color: torch.Tensor, d_rot: torch.Tensor | None = None, d_trans: torch.Tensor | None = None):
    from diff_surfel_rasterization import GaussianRasterizationSettings, GaussianRasterizer
    from utils.point_utils import depth_to_normal

    ssp = torch.zeros_like(pc.get_xyz, dtype=pc.get_xyz.dtype, requires_grad=True, device="cuda") + 0
    try:
        ssp.retain_grad()
    except Exception:
        pass
    rs = GaussianRasterizationSettings(
        image_height=int(viewpoint_camera.image_height), image_width=int(viewpoint_camera.image_width),
        tanfovx=math.tan(viewpoint_camera.FoVx * 0.5), tanfovy=math.tan(viewpoint_camera.FoVy * 0.5),
        bg=bg_color, scale_modifier=1.0, viewmatrix=viewpoint_camera.world_view_transform,
        projmatrix=viewpoint_camera.full_proj_transform, sh_degree=pc.active_sh_degree,
        campos=viewpoint_camera.camera_center, prefiltered=False, debug=False,
    )
    means3D, rotations = pc.get_xyz, pc.get_rotation
    if d_rot is not None and d_trans is not None:
        qd, Rd = _axis_angle_to_quat_and_mat(d_rot)
        means3D = means3D @ Rd.T + d_trans[None, :]
        rotations = _quat_mul_batch(qd, F.normalize(rotations, dim=-1))
    rendered_image, radii, allmap = GaussianRasterizer(raster_settings=rs)(
        means3D=means3D, means2D=ssp, shs=pc.get_features, colors_precomp=None,
        opacities=pc.get_opacity, scales=pc.get_scaling, rotations=rotations, cov3D_precomp=None,
    )
    ra = allmap[1:2]
    rn = (allmap[2:5].permute(1, 2, 0) @ (viewpoint_camera.world_view_transform[:3, :3].T)).permute(2, 0, 1)
    sd = torch.nan_to_num(allmap[0:1] / ra, 0, 0) * (1.0 - pipe.depth_ratio) + pipe.depth_ratio * torch.nan_to_num(allmap[5:6], 0, 0)
    sn = depth_to_normal(viewpoint_camera, sd).permute(2, 0, 1) * ra.detach()
    return {"render": rendered_image, "viewspace_points": ssp, "visibility_filter": radii > 0, "radii": radii,
            "rend_alpha": ra, "rend_normal": rn, "rend_dist": allmap[6:7], "surf_depth": sd, "surf_normal": sn}


def stage_train(args, data_g: Path, priors_dir: Path, out_dir: Path) -> Path:
    from arguments import ModelParams, OptimizationParams, PipelineParams
    from scene import GaussianModel, Scene
    from utils.general_utils import safe_state
    from utils.loss_utils import l1_loss, ssim
    import camera_utils as cu

    model_dir = out_dir / "2dgs_out"
    model_dir.mkdir(parents=True, exist_ok=True)
    iters = 10000 if args.fast and args.iterations == 15000 else args.iterations

    p = ArgumentParser(); lp, op, pp = ModelParams(p), OptimizationParams(p), PipelineParams(p)
    gs_args = p.parse_args(
        ["-s", str(data_g), "-m", str(model_dir), "--iterations", str(iters), "--depth_ratio", "0.0",
         "--lambda_dist", "0.01", "--lambda_normal", "0.05", "--foreground_only", "--lambda_alpha", "0.05"]
    )
    dataset, opt, pipe = lp.extract(gs_args), op.extract(gs_args), pp.extract(gs_args)
    dataset.foreground_only, dataset.lambda_alpha = True, 0.05
    safe_state(True)
    with open(model_dir / "cfg_args", "w", encoding="utf-8") as fh:
        fh.write(str(Namespace(**vars(dataset))))

    gaussians = GaussianModel(dataset.sh_degree)
    scene = Scene(dataset, gaussians, shuffle=False)
    gaussians.training_setup(opt)
    cams = scene.getTrainCameras()
    N_cams = len(cams)

    disp_dir = priors_dir / "disp"
    sem_dir = priors_dir / "sem"
    disp_cache = {}
    valid_sup_cache = {}
    k_arm7 = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7))
    for c in cams:
        st = Path(c.image_name).stem
        if args.use_da2:
            dp = disp_dir / f"{st}.npy"
            if dp.exists():
                disp_cache[st] = torch.from_numpy(np.load(dp).astype(np.float32).copy())
        sp = sem_dir / f"{st}.png"
        m_gt = getattr(c, "gt_alpha_mask", None)
        if sp.exists() and m_gt is not None:
            sem = np.asarray(Image.open(sp), dtype=np.uint8)
            Hc, Wc = int(c.image_height), int(c.image_width)
            if sem.shape != (Hc, Wc):
                sem = cv2.resize(sem, (Wc, Hc), interpolation=cv2.INTER_NEAREST)
            arm_occ = np.isin(sem, ARM_SKIN_CLASSES) & (~np.isin(sem, GARMENT_CLASS_MAP[args.garment_part]))
            arm_dil = cv2.dilate(arm_occ.astype(np.uint8) * 255, k_arm7) > 127
            arm_only = arm_dil & (~(m_gt[0].cpu().numpy() > 0.5))
            valid_sup_cache[st] = torch.from_numpy((1.0 - arm_only.astype(np.float32)))[None].cuda()

    rot_deltas = torch.nn.Parameter(torch.zeros((N_cams, 3), dtype=torch.float32, device="cuda"))
    trans_deltas = torch.nn.Parameter(torch.zeros((N_cams, 3), dtype=torch.float32, device="cuda"))
    pose_opt = torch.optim.Adam([rot_deltas, trans_deltas], lr=1.5e-4)
    max_t = 0.12 * float(scene.cameras_extent)
    bg = torch.zeros(3, dtype=torch.float32, device="cuda")

    vp_stack = None
    bar = tqdm(range(0, iters), desc="V9 Garment 2DGS")
    for iteration in range(1, iters + 1):
        gaussians.update_learning_rate(iteration)
        if iteration % 1000 == 0:
            gaussians.oneupSHdegree()
        if not vp_stack:
            vp_stack = list(range(N_cams))
        cid = vp_stack.pop(randint(0, len(vp_stack) - 1))
        cam = cams[cid]; stem = Path(cam.image_name).stem
        use_pose = bool(args.enable_pose_refine) and (1500 <= iteration <= int(0.7 * iters)) and cid != 0

        pkg = render_garment(cam, gaussians, pipe, bg, rot_deltas[cid] if use_pose else rot_deltas[cid].detach(), trans_deltas[cid] if use_pose else trans_deltas[cid].detach())
        im, gt = pkg["render"], cam.original_image.cuda()
        m = getattr(cam, "gt_alpha_mask", None)
        if m is not None:
            mc = m.to(im.device)
            loss = (1.0 - opt.lambda_dssim) * l1_loss(im * mc, gt * mc) + opt.lambda_dssim * (1.0 - ssim(im * mc, gt * mc))
            if stem in valid_sup_cache:
                alpha_loss = 0.05 * (torch.abs(pkg["rend_alpha"] - mc) * valid_sup_cache[stem]).mean()
            else:
                alpha_loss = 0.05 * l1_loss(pkg["rend_alpha"], mc)
        else:
            loss = (1.0 - opt.lambda_dssim) * l1_loss(im, gt) + opt.lambda_dssim * (1.0 - ssim(im, gt))
            alpha_loss = torch.zeros((), device=im.device)

        normal_w = 0.08 * min(1.0, (iteration - 3000) / 3000.0) if iteration > 3000 else 0.0
        normal_loss = normal_w * (1.0 - (pkg["rend_normal"] * pkg["surf_normal"]).sum(dim=0)).mean()
        dist_loss = (opt.lambda_dist if iteration > 3000 else 0.0) * pkg["rend_dist"].mean()

        ai_loss = torch.zeros((), device=im.device)
        if args.use_da2 and iteration > 2500 and stem in disp_cache and m is not None:
            sd = pkg["surf_depth"][0]; ra = pkg["rend_alpha"][0]; H, W = sd.shape
            disp_ai = disp_cache[stem].to(device=sd.device, dtype=torch.float32, non_blocking=True)
            if disp_ai.shape != (H, W):
                disp_ai = F.interpolate(disp_ai[None, None], size=(H, W), mode="bilinear", align_corners=False).squeeze(0).squeeze(0)
            gm = m[0] > 0.5
            valid = gm & (ra.detach() > 0.65) & (sd.detach() > 0.3)
            if int(valid.sum()) > 400:
                with torch.no_grad():
                    x, y = disp_ai[valid], 1.0 / sd.detach()[valid].clamp(min=0.3)
                    xm, ym = x.mean(), y.mean()
                    sc = (((x - xm) * (y - ym)).mean() / ((x - xm) ** 2).mean().clamp(min=1e-6)).clamp(min=1e-4)
                    inv_p = (sc * disp_ai + (ym - sc * xm)).clamp(min=0.05)
                inv_d = 1.0 / sd.clamp(min=0.3)
                ld = torch.abs(inv_d[valid] - inv_p[valid]).mean()
                vx = valid[:, :-1] & valid[:, 1:]; vy_m = valid[:-1, :] & valid[1:, :]
                lg = (
                    (torch.abs((inv_d[:, 1:] - inv_d[:, :-1]) - (inv_p[:, 1:] - inv_p[:, :-1])) * vx.float()).sum() / vx.float().sum().clamp(min=1.0)
                    + (torch.abs((inv_d[1:, :] - inv_d[:-1, :]) - (inv_p[1:, :] - inv_p[:-1, :])) * vy_m.float()).sum() / vy_m.float().sum().clamp(min=1.0)
                )
                ai_loss = min(1.0, (iteration - 2500) / 2500.0) * (0.08 * ld + 0.18 * lg)

        total = loss + dist_loss + normal_loss + alpha_loss + ai_loss + (1e-3 * (rot_deltas[cid].pow(2).sum() + trans_deltas[cid].pow(2).sum()) if use_pose else 0.0)
        total.backward()

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
                gaussians.optimizer.step(); gaussians.optimizer.zero_grad(set_to_none=True)
            if use_pose:
                if rot_deltas.grad is not None:
                    rot_deltas.grad[0].zero_()
                if trans_deltas.grad is not None:
                    trans_deltas.grad[0].zero_()
                pose_opt.step(); pose_opt.zero_grad(set_to_none=True)
                # Conservative 3.4 deg cap suitable for gentle mannequin/handheld jitter only
                rn = rot_deltas.norm(dim=1, keepdim=True).clamp(min=1e-8)
                rot_deltas.data = torch.where(rn > 0.06, rot_deltas.data * (0.06 / rn), rot_deltas.data)
                tn = trans_deltas.norm(dim=1, keepdim=True).clamp(min=1e-8)
                trans_deltas.data = torch.where(tn > max_t, trans_deltas.data * (max_t / tn), trans_deltas.data)
            else:
                pose_opt.zero_grad(set_to_none=True)
    bar.close()

    refined = {}
    psnrs = []
    with torch.no_grad():
        for idx, cam in enumerate(cams):
            st = Path(cam.image_name).stem
            _, Rd = _axis_angle_to_quat_and_mat(rot_deltas[idx])
            Rb, tb = cu.w2c_rt(cam)
            refined[st] = {
                "d_rot": [float(x) for x in rot_deltas[idx].cpu().numpy()],
                "d_trans": [float(x) for x in trans_deltas[idx].cpu().numpy()],
                "R_refined": (Rb @ Rd.cpu().numpy().astype(np.float64)).tolist(),
                "t_refined": (Rb @ trans_deltas[idx].cpu().numpy().astype(np.float64) + tb).tolist(),
            }
            pkg = render_garment(cam, gaussians, pipe, bg, rot_deltas[idx], trans_deltas[idx])
            m = getattr(cam, "gt_alpha_mask", None)
            if m is not None:
                fb = m[0] > 0.5
                mse = ((torch.clamp(pkg["render"], 0, 1)[:, fb] - torch.clamp(cam.original_image.cuda(), 0, 1)[:, fb]) ** 2).mean().clamp(min=1e-10)
                psnrs.append(float(-10.0 * torch.log10(mse).item()))
    with open(model_dir / "refined_poses.json", "w", encoding="utf-8") as fh:
        json.dump(refined, fh, indent=2)
    with open(model_dir / "train_stats.json", "w", encoding="utf-8") as fh:
        json.dump({"iterations": iters, "final_points": int(len(gaussians.get_xyz)), "fg_psnr_mean_db": round(float(np.mean(psnrs)), 3)}, fh, indent=2)
    print(f"[Train] 纯衣物 2DGS 完成: Pts={len(gaussians.get_xyz)}, PSNR={np.mean(psnrs):.2f} dB", flush=True)
    return model_dir


def stage_mesh(args, data_g: Path, model_dir: Path, out_dir: Path) -> Path:
    from arguments import ModelParams, PipelineParams
    from scene import GaussianModel, Scene
    from fusion_core import FusionVolume
    import camera_utils as cu

    mesh_dir = out_dir / "mesh"
    mesh_dir.mkdir(parents=True, exist_ok=True)
    refined = json.load(open(model_dir / "refined_poses.json", encoding="utf-8"))
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
    cam_centers = np.stack([-np.array(refined[Path(c.image_name).stem]["R_refined"]).T @ np.array(refined[Path(c.image_name).stem]["t_refined"]) for c in cams], axis=0)

    sem_dir = out_dir / "priors" / "sem"
    k_arm7 = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7))
    frames, p95s = [], []
    for cam in cams:
        st = Path(cam.image_name).stem
        pose = refined[st]
        dr, dt = torch.tensor(pose["d_rot"], dtype=torch.float32, device="cuda"), torch.tensor(pose["d_trans"], dtype=torch.float32, device="cuda")
        with torch.no_grad():
            p0_, p1_ = render_garment(cam, gaussians, pipe0, bg, dr, dt), render_garment(cam, gaussians, pipe1, bg, dr, dt)
        d0 = p0_["surf_depth"].squeeze().cpu().numpy().astype(np.float32)
        d1 = cv2.medianBlur(p1_["surf_depth"].squeeze().cpu().numpy().astype(np.float32), 5)
        d = np.where(np.isfinite(d0) & np.isfinite(d1) & (d1 > 0) & ((d0 - d1) > 0.020), d1, d0)
        rgb = np.clip(p0_["render"].permute(1, 2, 0).cpu().numpy(), 0.0, 1.0).astype(np.float32)
        alpha = p0_["rend_alpha"].squeeze().cpu().numpy().astype(np.float32)
        mask = np.asarray(Image.open(data_g / "masks" / f"{st}.png").convert("L")) > 127
        sp = sem_dir / f"{st}.png"
        if sp.exists():
            sem = np.asarray(Image.open(sp), dtype=np.uint8)
            if sem.shape != mask.shape:
                sem = cv2.resize(sem, (mask.shape[1], mask.shape[0]), interpolation=cv2.INTER_NEAREST)
            arm_ban = cv2.dilate((np.isin(sem, ARM_SKIN_CLASSES) & (~np.isin(sem, GARMENT_CLASS_MAP[args.garment_part]))).astype(np.uint8) * 255, k_arm7) > 127
            mask = mask & (~arm_ban)
        fg_ok = np.isfinite(d) & (d > 0) & (alpha > 0.5) & mask
        if fg_ok.sum() > 100:
            med_d = float(np.median(d[fg_ok]))
            p95s.append(float(np.percentile(d[fg_ok], 95)))
            d_f = cv2.bilateralFilter(np.where(fg_ok, d, med_d).astype(np.float32), 5, float(max(0.025, 0.02 * med_d)), 2.0)
            d = np.where(fg_ok, d_f, d)
        frames.append({"depth": d, "rgb": rgb, "alpha": alpha, "mask": mask, "K": cu.intrinsics(cam), "R": np.array(pose["R_refined"]), "t": np.array(pose["t_refined"])})

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
    o3m = o3m.filter_smooth_taubin(number_of_iterations=12)
    o3m.compute_vertex_normals()

    pcd = o3d.geometry.PointCloud()
    pcd.points, pcd.normals, pcd.colors = o3m.vertices, o3m.vertex_normals, o3m.vertex_colors
    m_p, _ = o3d.geometry.TriangleMesh.create_from_point_cloud_poisson(pcd, depth=8, linear_fit=False)
    tree = cKDTree(np.asarray(o3m.vertices))
    dists, _ = tree.query(np.asarray(m_p.vertices), k=1)
    m_p.remove_vertices_by_mask(dists > max(0.20, float(np.percentile(dists, 98)) * 1.6))
    tri_clusters, cluster_n_tris, _ = m_p.cluster_connected_triangles()
    tri_clusters_np = np.asarray(tri_clusters)
    cluster_n_np = np.asarray(cluster_n_tris)
    if len(cluster_n_np):
        min_tris = int(0.05 * int(cluster_n_np.max()))
        drop_tris = np.isin(tri_clusters_np, np.nonzero(cluster_n_np < min_tris)[0])
        if drop_tris.any():
            m_p.remove_triangles_by_mask(drop_tris)
            m_p.remove_unreferenced_vertices()
    m_p = m_p.filter_smooth_taubin(number_of_iterations=3)
    if len(m_p.triangles) > 100000:
        m_p = m_p.simplify_quadric_decimation(target_number_of_triangles=100000)

    m_p.compute_triangle_normals()
    v_f, f_f, fn_f = np.asarray(m_p.vertices), np.asarray(m_p.triangles), np.asarray(m_p.triangle_normals)
    if len(f_f):
        fc = v_f[f_f].mean(axis=1); _, nn = cKDTree(cam_centers).query(fc, k=1)
        to_cam = cam_centers[nn] - fc; to_cam /= np.maximum(np.linalg.norm(to_cam, axis=1, keepdims=True), 1e-12)
        if float(((fn_f * to_cam).sum(axis=1) > 0).mean()) < 0.5:
            m_p.triangles = o3d.utility.Vector3iVector(f_f[:, ::-1].copy())
    m_p.compute_vertex_normals()
    _, cidx = tree.query(np.asarray(m_p.vertices), k=1)
    m_p.vertex_colors = o3d.utility.Vector3dVector(np.clip(np.asarray(o3m.vertex_colors)[cidx], 0.0, 1.0))
    o3d.io.write_triangle_mesh(str(mesh_dir / "garment-clean.ply"), m_p)
    with open(mesh_dir / "mesh_stats.json", "w", encoding="utf-8") as fh:
        json.dump({"voxel_size": round(float(vol.voxel), 6), "final_verts": len(m_p.vertices), "final_faces": len(m_p.triangles)}, fh, indent=2)
    print(f"[Mesh] 衣物网格提取完成: {len(m_p.triangles)} 面", flush=True)
    return mesh_dir


def stage_bake(args, data_g: Path, model_dir: Path, mesh_dir: Path, out_dir: Path) -> Path:
    from arguments import ModelParams, PipelineParams
    from scene import GaussianModel, Scene
    import camera_utils as cu

    final_dir = out_dir / "final"
    final_dir.mkdir(parents=True, exist_ok=True)
    refined = json.load(open(model_dir / "refined_poses.json", encoding="utf-8"))
    depth_tol = 6.0 * float(json.load(open(mesh_dir / "mesh_stats.json", encoding="utf-8"))["voxel_size"])

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

    mesh = o3d.io.read_triangle_mesh(str(mesh_dir / "garment-clean.ply"))
    mesh.compute_vertex_normals()
    V0, N0, VC0, F0 = np.asarray(mesh.vertices, np.float32), np.asarray(mesh.vertex_normals, np.float32), np.asarray(mesh.vertex_colors, np.float32), np.asarray(mesh.triangles, np.uint32)
    ray_scene = o3d.t.geometry.RaycastingScene()
    ray_scene.add_triangles(o3d.core.Tensor(V0, o3d.core.float32), o3d.core.Tensor(F0, o3d.core.uint32))
    vmapping, indices, uvs = xatlas.parametrize(V0, F0, N0)
    V_uv, N_uv, VC_uv, F_uv, uvs = V0[vmapping], N0[vmapping], VC0[vmapping], indices.astype(np.int64), uvs.astype(np.float32)

    TEX = 2048
    pos_m, norm_m, vcol_m, valid_m = np.zeros((TEX, TEX, 3), np.float32), np.zeros((TEX, TEX, 3), np.float32), np.zeros((TEX, TEX, 3), np.float32), np.zeros((TEX, TEX), bool)
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
        ins = (l1 >= -1e-4) & (l2 >= -1e-4) & ((1.0 - l1 - l2) >= -1e-4)
        if not ins.any():
            continue
        b1, b2, b3 = l1[ins][:, None], l2[ins][:, None], (1.0 - l1 - l2)[ins][:, None]
        yy, xx = np.nonzero(ins); ty, tx = y0 + yy, x0 + xx
        pos_m[ty, tx] = b1 * V_uv[i0] + b2 * V_uv[i1] + b3 * V_uv[i2]
        nn = b1 * N_uv[i0] + b2 * N_uv[i1] + b3 * N_uv[i2]; nn /= np.maximum(np.linalg.norm(nn, axis=1, keepdims=True), 1e-8)
        norm_m[ty, tx] = nn
        vcol_m[ty, tx] = np.clip(b1 * VC_uv[i0] + b2 * VC_uv[i1] + b3 * VC_uv[i2], 0.0, 1.0)
        valid_m[ty, tx] = True

    vy, vx = np.nonzero(valid_m)
    P, Nm = pos_m[vy, vx].astype(np.float64), norm_m[vy, vx].astype(np.float64)

    sem_dir = out_dir / "priors" / "sem"
    k_ban19 = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (19, 19))
    bake_masks: dict[str, np.ndarray] = {}
    cloth_lums, cloth_as, cloth_bs = [], [], []
    for cam in cams:
        st = Path(cam.image_name).stem
        bgr = cv2.imread(str(data_g / "images" / f"{st}.jpg"), cv2.IMREAD_COLOR)
        H, W = bgr.shape[:2]
        m_g = np.asarray(Image.open(data_g / "masks" / f"{st}.png").convert("L").filter(ImageFilter.MinFilter(5))) > 127
        sp = sem_dir / f"{st}.png"
        if sp.exists():
            sem = np.asarray(Image.open(sp), dtype=np.uint8)
            if sem.shape != (H, W):
                sem = cv2.resize(sem, (W, H), interpolation=cv2.INTER_NEAREST)
            body_ban = cv2.dilate((np.isin(sem, BAKE_BODY_BAN_CLASSES) & (~np.isin(sem, GARMENT_CLASS_MAP[args.garment_part]))).astype(np.uint8) * 255, k_ban19) > 127
            m_g = m_g & (~body_ban)
        bake_masks[st] = m_g
        if int(m_g.sum()) > 400:
            lab = cv2.cvtColor(bgr, cv2.COLOR_BGR2LAB)
            cloth_lums.append(float(np.mean(lab[:, :, 0][m_g])))
            cloth_as.append(float(np.mean(lab[:, :, 1][m_g])))
            cloth_bs.append(float(np.mean(lab[:, :, 2][m_g])))
    target_L = float(np.percentile(cloth_lums, 65)) if cloth_lums else 110.0
    target_a = float(np.median(cloth_as)) if cloth_as else 128.0
    target_b = float(np.median(cloth_bs)) if cloth_bs else 128.0

    def _bilerp(arr, u, v):
        H, W = arr.shape[:2]
        x0 = np.clip(np.floor(u).astype(np.int32), 0, W - 1); y0 = np.clip(np.floor(v).astype(np.int32), 0, H - 1)
        wx, wy = (u - x0)[:, None].astype(np.float32), (v - y0)[:, None].astype(np.float32)
        return ((1 - wy) * ((1 - wx) * arr[y0, x0] + wx * arr[y0, np.clip(x0 + 1, 0, W - 1)]) + wy * ((1 - wx) * arr[np.clip(y0 + 1, 0, H - 1), x0] + wx * arr[np.clip(y0 + 1, 0, H - 1), np.clip(x0 + 1, 0, W - 1)])).astype(np.float32)

    cam_obs, sharp_vals = [], []
    for cam in cams:
        st = Path(cam.image_name).stem
        bgr = cv2.imread(str(data_g / "images" / f"{st}.jpg"), cv2.IMREAD_COLOR)
        H, W = bgr.shape[:2]
        m_g = bake_masks[st]
        if int(m_g.sum()) > 400:
            lab = cv2.cvtColor(bgr, cv2.COLOR_BGR2LAB)
            cur_L = float(np.mean(lab[:, :, 0][m_g]))
            cur_a = float(np.mean(lab[:, :, 1][m_g]))
            cur_b = float(np.mean(lab[:, :, 2][m_g]))
            sm = cv2.GaussianBlur(m_g.astype(np.float32), (0, 0), sigmaX=3.0)
            if cur_L < target_L * 0.90:
                Lc = cv2.createCLAHE(clipLimit=1.5, tileGridSize=(8, 8)).apply(lab[:, :, 0]).astype(np.float32)
                Lb = np.clip((0.60 * lab[:, :, 0] + 0.40 * Lc) * float(np.clip(target_L / max(cur_L, 12.0), 1.0, 1.45)), 0.0, 240.0)
                lab[:, :, 0] = np.clip((1.0 - sm) * lab[:, :, 0] + sm * Lb, 0.0, 255.0).astype(np.uint8)
            da = 0.55 * float(np.clip(target_a - cur_a, -10.0, 10.0))
            db = 0.55 * float(np.clip(target_b - cur_b, -10.0, 10.0))
            if abs(da) > 0.25 or abs(db) > 0.25:
                lab[:, :, 1] = np.clip(lab[:, :, 1].astype(np.float32) + sm * da, 0.0, 255.0).astype(np.uint8)
                lab[:, :, 2] = np.clip(lab[:, :, 2].astype(np.float32) + sm * db, 0.0, 255.0).astype(np.uint8)
            bgr = cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)
        rgb = bgr[:, :, ::-1].astype(np.float32) / 255.0
        ys, xs = np.nonzero(m_g)
        sharp_vals.append(float(cv2.Laplacian(cv2.cvtColor(bgr[int(ys.min()):int(ys.max())+1, int(xs.min()):int(xs.max())+1], cv2.COLOR_BGR2GRAY), cv2.CV_32F).var()) if len(ys) > 100 else 1.0)
        si = len(sharp_vals) - 1

        pose = refined[st]
        dr, dt = torch.tensor(pose["d_rot"], dtype=torch.float32, device="cuda"), torch.tensor(pose["d_trans"], dtype=torch.float32, device="cuda")
        with torch.no_grad():
            p0_, p1_ = render_garment(cam, gaussians, pipe0, bg, dr, dt), render_garment(cam, gaussians, pipe1, bg, dr, dt)
        d0, d1, alpha = p0_["surf_depth"].squeeze().cpu().numpy().astype(np.float32), cv2.medianBlur(p1_["surf_depth"].squeeze().cpu().numpy().astype(np.float32), 5), p0_["rend_alpha"].squeeze().cpu().numpy().astype(np.float32)
        depth = np.where(np.isfinite(d0) & np.isfinite(d1) & (d1 > 0) & ((d0 - d1) > 0.020), d1, d0)

        R, t, K = np.array(pose["R_refined"], np.float64), np.array(pose["t_refined"], np.float64), cu.intrinsics(cam)
        C = -R.T @ t

        Xc = P @ R.T + t; z = Xc[:, 2]; ok_z = z > 0.2
        u = np.where(ok_z, K[0, 0] * Xc[:, 0] / np.maximum(z, 0.2) + K[0, 2], -1.0)
        v = np.where(ok_z, K[1, 1] * Xc[:, 1] / np.maximum(z, 0.2) + K[1, 2], -1.0)
        inb = ok_z & (u >= 1.0) & (u < W - 2) & (v >= 1.0) & (v < H - 2)
        if not inb.any():
            continue
        idx = np.nonzero(inb)[0]
        ui, vi = np.rint(u[idx]).astype(np.int32), np.rint(v[idx]).astype(np.int32)
        vis = np.isfinite(depth[vi, ui]) & (np.abs(z[idx] - depth[vi, ui]) < depth_tol) & (alpha[vi, ui] > 0.5) & m_g[vi, ui]
        if not vis.any():
            continue
        idx = idx[vis]
        vd = C[None, :] - P[idx]; vd /= np.maximum(np.linalg.norm(vd, axis=1, keepdims=True), 1e-12)
        fac = (Nm[idx] * vd).sum(axis=1); kp = fac > 0.28
        if not kp.any():
            continue
        idx, fac = idx[kp], fac[kp]
        dirs = P[idx] - C[None, :]
        dist = np.linalg.norm(dirs, axis=1)
        unit_dirs = dirs / np.maximum(dist[:, None], 1e-8)
        rays = np.hstack([np.broadcast_to(C[None, :], (len(idx), 3)), unit_dirs]).astype(np.float32)
        t_hit = ray_scene.cast_rays(o3d.core.Tensor(rays, o3d.core.float32))["t_hit"].numpy()
        unocc = np.isfinite(t_hit) & (dist <= t_hit + 0.040)
        if unocc.any():
            idx = idx[unocc]
            cam_obs.append((idx, _bilerp(rgb, u[idx], v[idx]), fac[unocc], np.maximum(z[idx], 0.35), si))

    med_s = float(np.median(sharp_vals)) if sharp_vals else 1.0
    sw_list = [float(np.clip((s / max(med_s, 1e-6)) ** 0.75, 0.70, 1.35)) for s in sharp_vals]
    acc1, w1 = np.zeros((len(P), 3), np.float64), np.zeros(len(P), np.float64)
    for idx, cols, fac, zs, si in cam_obs:
        w = ((fac ** 5) * sw_list[si]) / (zs ** 3.5)
        acc1[idx] += w[:, None] * cols; w1[idx] += w
    h1 = w1 > 1e-8; C1 = np.zeros((len(P), 3), np.float32); C1[h1] = (acc1[h1] / w1[h1, None]).astype(np.float32)

    acc2, w2 = np.zeros((len(P), 3), np.float64), np.zeros(len(P), np.float64)
    for idx, cols, fac, zs, si in cam_obs:
        g = np.mean(np.abs(cols - C1[idx]), axis=1) <= 0.085
        if g.any():
            w = ((fac[g] ** 5) * sw_list[si]) / (zs[g] ** 3.5)
            acc2[idx[g]] += w[:, None] * cols[g]; w2[idx[g]] += w
    h2 = w2 > 1e-8
    C_out = vcol_m[vy, vx].copy(); C_out[h1 & (~h2)] = C1[h1 & (~h2)]; C_out[h2] = np.clip(acc2[h2] / w2[h2, None], 0.0, 1.0).astype(np.float32)

    tex_rgb = vcol_m.copy(); tex_rgb[vy, vx] = C_out
    h, w = valid_m.shape; fld = valid_m.copy()
    for _ in range(8):
        if fld.all():
            break
        acc, cnt = np.zeros_like(tex_rgb), np.zeros((h, w), np.float32)
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                if dy or dx:
                    m = fld[max(0, -dy):h-max(0, dy), max(0, -dx):w-max(0, dx)]
                    acc[max(0, dy):h-max(0, -dy), max(0, dx):w-max(0, -dx)][m] += tex_rgb[max(0, -dy):h-max(0, dy), max(0, -dx):w-max(0, dx)][m]
                    cnt[max(0, dy):h-max(0, -dy), max(0, dx):w-max(0, -dx)][m] += 1.0
        grow = (~fld) & (cnt > 0); tex_rgb[grow] = acc[grow] / cnt[grow, None]; fld |= grow

    albedo_p = final_dir / "albedo_2048.png"
    Image.fromarray((np.clip(tex_rgb, 0.0, 1.0) * 255.0).astype(np.uint8), mode="RGB").save(albedo_p)
    gray_t = (0.299 * tex_rgb[:, :, 0] + 0.587 * tex_rgb[:, :, 1] + 0.114 * tex_rgb[:, :, 2]).astype(np.float32)
    gx, gy = cv2.Sobel(gray_t, cv2.CV_32F, 1, 0, ksize=3) / 4.0, cv2.Sobel(gray_t, cv2.CV_32F, 0, 1, ksize=3) / 4.0
    nx, ny, nz = -gx * 0.45, gy * 0.45, np.ones_like(gx)
    nlen = np.sqrt(nx * nx + ny * ny + nz * nz)
    normal_p = final_dir / "normal_2048.png"
    Image.fromarray((np.clip(np.stack([(nx / nlen) * 0.5 + 0.5, (ny / nlen) * 0.5 + 0.5, (nz / nlen) * 0.5 + 0.5], axis=-1), 0.0, 1.0) * 255.0).astype(np.uint8), mode="RGB").save(normal_p)

    mat = PBRMaterial(name="garment_v9_mat", baseColorTexture=Image.open(albedo_p), normalTexture=Image.open(normal_p), baseColorFactor=[1.0, 1.0, 1.0, 1.0], metallicFactor=0.0, roughnessFactor=0.82, doubleSided=True)
    raw_glb, glb_out = final_dir / "_raw_garment.glb", final_dir / "garment-textured.glb"
    trimesh.Trimesh(vertices=V_uv, faces=F_uv, vertex_normals=N_uv, visual=trimesh.visual.TextureVisuals(uv=uvs, material=mat), process=False).export(str(raw_glb))
    strip_file(str(raw_glb), str(glb_out))
    raw_glb.unlink(missing_ok=True)
    if find_extensions(read_glb(str(glb_out))[2]):
        raise RuntimeError("导出 GLB 仍残留 Filament 禁用扩展")
    print(f"[Bake] 导出清洁衣物 GLB: {glb_out} ({glb_out.stat().st_size / 1e6:.2f} MB)", flush=True)
    return final_dir


def stage_preview(model_dir: Path, final_dir: Path, out_dir: Path) -> Path:
    from arguments import ModelParams
    from scene import GaussianModel, Scene
    import camera_utils as cu
    import mesh_render as mr

    vis_dir = out_dir / "visuals"; vis_dir.mkdir(parents=True, exist_ok=True)
    refined = json.load(open(model_dir / "refined_poses.json", encoding="utf-8"))
    tm = trimesh.load(str(final_dir / "garment-textured.glb"), force="mesh", process=False)
    v, f, uv = np.asarray(tm.vertices, np.float64), np.asarray(tm.faces, np.int64), np.asarray(tm.visual.uv, np.float64)
    tex = np.asarray(tm.visual.material.baseColorTexture.convert("RGB"), np.float64) / 255.0

    p = ArgumentParser(); mp = ModelParams(p, sentinel=True)
    cfg = eval(open(model_dir / "cfg_args", encoding="utf-8").read())  # noqa: S307
    ds = mp.extract(Namespace(**{**vars(cfg), "model_path": str(model_dir)}))
    preview_it = int(json.load(open(model_dir / "train_stats.json", encoding="utf-8"))["iterations"])
    all_cams = Scene(ds, GaussianModel(ds.sh_degree), load_iteration=preview_it, shuffle=False).getTrainCameras()
    ups = [-np.linalg.inv(np.vstack([np.hstack([np.array(refined[Path(c.image_name).stem]["R_refined"]), np.array(refined[Path(c.image_name).stem]["t_refined"])[:, None]]), [0, 0, 0, 1]]))[:3, 1] for c in all_cams]
    up_axis = np.mean(ups, axis=0); up_axis /= np.linalg.norm(up_axis)
    center = np.median(v, axis=0)
    span = float(np.percentile(v @ up_axis, 98) - np.percentile(v @ up_axis, 2))
    c0_cam = all_cams[0]
    K0 = cu.intrinsics(c0_cam); tw, th = 360, 640
    K_tt = K0.copy(); K_tt[0, :] *= tw / float(c0_cam.image_width); K_tt[1, :] *= th / float(c0_cam.image_height)
    Rr, tr = np.array(refined[Path(c0_cam.image_name).stem]["R_refined"]), np.array(refined[Path(c0_cam.image_name).stem]["t_refined"])
    rad = (-Rr.T @ tr) - center - np.dot((-Rr.T @ tr) - center, up_axis) * up_axis
    dir0 = rad / np.linalg.norm(rad)
    radius = max(span * (float(K_tt[1, 1]) / float(th)) * 1.45, 2.2)

    cw, ch = 240, 426
    sheet = Image.new("RGB", (4 * cw + 5 * 14, 2 * ch + 3 * 14 + 56), (20, 22, 28))
    td = ImageDraw.Draw(sheet); f_h, f_c = _load_font(20), _load_font(16)
    td.text((16, 14), f"V9 通用衣物重建 8 视角预览（{len(f):,} 面，SegFormer+LightGlue+facing^5）", fill=(245, 248, 255), font=f_h)
    th_t, tw_t = tex.shape[:2]
    for k, deg in enumerate((0, 45, 90, 135, 180, 225, 270, 315)):
        K_ax = np.array([[0, -up_axis[2], up_axis[1]], [up_axis[2], 0, -up_axis[0]], [-up_axis[1], up_axis[0], 0]])
        rot = np.eye(3) + math.sin(math.radians(deg)) * K_ax + (1.0 - math.cos(math.radians(deg))) * (K_ax @ K_ax)
        cam_pos = center + radius * (rot @ dir0)
        fwd = (center - cam_pos); fwd /= np.linalg.norm(fwd)
        right = np.cross(fwd, up_axis); right /= np.linalg.norm(right)
        R_tt, t_tt = np.stack([right, np.cross(fwd, right), fwd], axis=0), -np.stack([right, np.cross(fwd, right), fwd], axis=0) @ cam_pos
        vc = (R_tt @ v.T).T + t_tt; z = vc[:, 2]; sz = np.where(z > 1e-6, z, 1e-6)
        u, wp = K_tt[0, 0] * vc[:, 0] / sz + K_tt[0, 2], K_tt[1, 1] * vc[:, 1] / sz + K_tt[1, 2]
        fn = mr.face_normals(v, f); ld = cam_pos[None, :] - v[f].mean(axis=1); ld /= np.maximum(np.linalg.norm(ld, axis=1, keepdims=True), 1e-12)
        fn *= np.where(np.sign((fn * ld).sum(axis=1)) == 0, 1.0, np.sign((fn * ld).sum(axis=1)))[:, None]
        shade = 0.72 + 0.28 * np.clip((fn * ld).sum(axis=1), 0.0, 1.0)
        rgb, zb = np.full((th, tw, 3), [0.08, 0.09, 0.11], np.float64), np.full((th, tw), np.inf, np.float64)
        u0, u1, u2 = u[f[:, 0]], u[f[:, 1]], u[f[:, 2]]; v0, v1, v2 = wp[f[:, 0]], wp[f[:, 1]], wp[f[:, 2]]; z0, z1, z2 = z[f[:, 0]], z[f[:, 1]], z[f[:, 2]]; uv0, uv1, uv2 = uv[f[:, 0]], uv[f[:, 1]], uv[f[:, 2]]
        vis = (z0 > 1e-6) & (z1 > 1e-6) & (z2 > 1e-6)
        umin, umax, vmin, vmax = np.floor(np.minimum.reduce([u0, u1, u2])).astype(int), np.ceil(np.maximum.reduce([u0, u1, u2])).astype(int), np.floor(np.minimum.reduce([v0, v1, v2])).astype(int), np.ceil(np.maximum.reduce([v0, v1, v2])).astype(int)
        vis &= (umax >= 0) & (umin < tw) & (vmax >= 0) & (vmin < th) & ((umax - umin) < 2048) & ((vmax - vmin) < 2048)
        for i in np.nonzero(vis)[0]:
            x0, x1, y0, y1 = max(int(umin[i]), 0), min(int(umax[i]), tw - 1), max(int(vmin[i]), 0), min(int(vmax[i]), th - 1)
            d = (v1[i] - v2[i]) * (u0[i] - u2[i]) + (u2[i] - u1[i]) * (v0[i] - v2[i])
            if x1 < x0 or y1 < y0 or abs(d) < 1e-12:
                continue
            px, py = np.meshgrid(np.arange(x0, x1 + 1), np.arange(y0, y1 + 1))
            l1, l2 = ((v1[i] - v2[i]) * (px - u2[i]) + (u2[i] - u1[i]) * (py - v2[i])) / d, ((v2[i] - v0[i]) * (px - u2[i]) + (u0[i] - u2[i]) * (py - v2[i])) / d
            ins = (l1 >= -1e-9) & (l2 >= -1e-9) & ((1.0 - l1 - l2) >= -1e-9)
            if not ins.any():
                continue
            w1_, w2_, w3_ = l1 / z0[i], l2 / z1[i], (1.0 - l1 - l2) / z2[i]
            zz = 1.0 / np.maximum(w1_ + w2_ + w3_, 1e-12); sz_b = zb[y0:y1+1, x0:x1+1]; wr = ins & (zz < sz_b)
            if not wr.any():
                continue
            sz_b[wr] = zz[wr]
            ww1, ww2, ww3 = w1_[wr] * zz[wr], w2_[wr] * zz[wr], w3_[wr] * zz[wr]
            tx_ = np.clip(np.rint(np.clip(ww1 * uv0[i, 0] + ww2 * uv1[i, 0] + ww3 * uv2[i, 0], 0.0, 1.0) * (tw_t - 1)).astype(np.int32), 0, tw_t - 1)
            ty_ = np.clip(np.rint((1.0 - np.clip(ww1 * uv0[i, 1] + ww2 * uv1[i, 1] + ww3 * uv2[i, 1], 0.0, 1.0)) * (th_t - 1)).astype(np.int32), 0, th_t - 1)
            rgb[y0:y1+1, x0:x1+1][wr] = tex[ty_, tx_] * shade[i]
        r_i, c_i = divmod(k, 4); px_c, py_c = 14 + c_i * (cw + 14), 56 + 14 + r_i * (ch + 14)
        sheet.paste(Image.fromarray((np.clip(rgb, 0.0, 1.0) * 255.0).astype(np.uint8)).resize((cw, ch), Image.Resampling.LANCZOS), (px_c, py_c))
        td.rectangle([px_c, py_c, px_c + cw - 1, py_c + ch - 1], outline=(68, 76, 92), width=1)
        td.text((px_c + 8, py_c + 8), f"{deg}°", fill=(140, 245, 180), font=f_c)
    out_png = vis_dir / "garment_turntable.png"; sheet.save(out_png, quality=95)
    print(f"[Preview] 保存 8 视角预览图: {out_png}", flush=True)
    return out_png


def main() -> int:
    parser = ArgumentParser(description="Zero-hardcode V9 3D Garment Reconstruction CLI")
    parser.add_argument("--video", type=str, default=None)
    parser.add_argument("--images-dir", type=str, default=None)
    parser.add_argument("--output-dir", type=str, required=True)
    parser.add_argument("--stages", type=str, default="all", help="extract,priors,sfm,train,mesh,bake,preview")
    parser.add_argument("--garment-part", choices=tuple(GARMENT_CLASS_MAP), default="upper")
    parser.add_argument("--target-frames", type=int, default=90)
    parser.add_argument("--iterations", type=int, default=15000)
    parser.add_argument("--fast", action="store_true")
    parser.add_argument("--use-da2", dest="use_da2", action="store_true", default=False)
    parser.add_argument("--no-da2", dest="use_da2", action="store_false")
    parser.add_argument("--enable-pose-refine", action="store_true", help="Gentle <=3.4deg pose delta for handheld jitter")
    parser.add_argument("--skip-head-sec", type=float, default=1.0)
    parser.add_argument("--skip-tail-sec", type=float, default=1.0)
    parser.add_argument("--cameras-bin", type=str, default=None)
    parser.add_argument("--gs2d-repo", type=str, default=DEFAULT_2DGS_REPO)
    parser.add_argument("--eval-dir", type=str, default=DEFAULT_EVAL_DIR)
    args = parser.parse_args()

    out_dir = Path(args.output_dir); out_dir.mkdir(parents=True, exist_ok=True)
    stages = list(ALL_STAGES) if args.stages.strip().lower() == "all" else [s.strip().lower() for s in args.stages.split(",") if s.strip()]
    for s in stages:
        if s not in ALL_STAGES:
            raise ValueError(f"未知阶段: {s}")
    _inject_external_paths(args.gs2d_repo, args.eval_dir)

    t0 = time.time()
    raw_dir, priors_dir, data_g, model_dir, mesh_dir, final_dir = out_dir / "raw_frames", out_dir / "priors", out_dir / "2dgs_data_garment", out_dir / "2dgs_out", out_dir / "mesh", out_dir / "final"
    if "extract" in stages:
        raw_dir = stage_extract(args, out_dir)
    if "priors" in stages:
        priors_dir = stage_priors(args, raw_dir, out_dir)
    if "sfm" in stages:
        data_g = stage_sfm(args, raw_dir, priors_dir, out_dir)
    if "train" in stages:
        model_dir = stage_train(args, data_g, priors_dir, out_dir)
    if "mesh" in stages:
        mesh_dir = stage_mesh(args, data_g, model_dir, out_dir)
    if "bake" in stages:
        final_dir = stage_bake(args, data_g, model_dir, mesh_dir, out_dir)
    if "preview" in stages:
        stage_preview(model_dir, final_dir, out_dir)
    print(f"[Done] 衣物重建阶段 {stages} 完成 ({time.time() - t0:.1f}s)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
