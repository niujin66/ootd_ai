"""Incremental Reshoot Texture Refinement Pipeline.

Allows users to fix localized missing patches or blurry/dark regions on an
already-generated garment model by capturing a short targeted reshoot clip or
image set, registering those reshoot frames into the existing 3D model frame
via SuperPoint + LightGlue + PnP (or known poses), and performing quality-gated
incremental UV texture fusion without degrading already-crisp prints.
"""
from __future__ import annotations

import json
import math
import shutil
import sys
from argparse import ArgumentParser
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import open3d as o3d
import torch
import trimesh
from PIL import Image, ImageDraw

RECON_DIR = Path(__file__).resolve().parent
if str(RECON_DIR) not in sys.path:
    sys.path.insert(0, str(RECON_DIR))

from transfer_scan_texture import (  # noqa: E402
    BAKE_BODY_BAN_CLASSES,
    GARMENT_CLASS_UPPER,
    _bilerp_rgb,
    _load_clean_bake_mask,
    _load_font,
    _rasterize_textured_view,
    diagnose_model_defects,
    evaluate_reprojection_metrics,
    export_clean_glb,
    inpaint_and_dilate_uv,
    load_scan_views,
    transform_points_and_normals,
)

LIGHTGLUE_ID = "ETH-CVG/lightglue_superpoint"
SEGFORMER_ID = "mattmdjaga/segformer_b2_clothes"


def _extract_reshoot_frames_from_video(video_path: Path, out_dir: Path, max_frames: int = 12) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise RuntimeError(f"无法打开补拍视频: {video_path}")
    total = max(1, int(cap.get(cv2.CAP_PROP_FRAME_COUNT)))
    n_cand = min(total, max(max_frames * 3, 24))
    scored: list[tuple[float, int, np.ndarray]] = []
    for fidx in np.unique(np.linspace(0, total - 1, num=n_cand, dtype=int)):
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(fidx))
        ok, bgr = cap.read()
        if not ok or bgr is None:
            continue
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        sharp = float(cv2.Laplacian(gray, cv2.CV_32F).var())
        scored.append((sharp, int(fidx), bgr))
    cap.release()
    if not scored:
        raise RuntimeError(f"补拍视频未读取到有效帧: {video_path}")

    med_s = float(np.median([s[0] for s in scored]))
    crisp = [s for s in scored if s[0] >= 0.4 * med_s]
    if len(crisp) < min(4, len(scored)):
        crisp = scored
    crisp.sort(key=lambda x: x[1])
    if len(crisp) > max_frames:
        chosen = [crisp[i] for i in np.linspace(0, len(crisp) - 1, num=max_frames, dtype=int)]
    else:
        chosen = crisp

    paths: list[Path] = []
    for k, (_, _, bgr) in enumerate(chosen):
        p = out_dir / f"reshoot-{k:05d}.jpg"
        Image.fromarray(bgr[:, :, ::-1], mode="RGB").save(p, quality=96)
        paths.append(p)
    return paths


def _generate_segformer_masks(image_paths: list[Path], mask_dir: Path, sem_dir: Path) -> None:
    import torch.nn.functional as F
    import transformers

    mask_dir.mkdir(parents=True, exist_ok=True)
    sem_dir.mkdir(parents=True, exist_ok=True)
    device = "cuda" if torch.cuda.is_available() else "cpu"
    try:
        proc = transformers.AutoImageProcessor.from_pretrained(SEGFORMER_ID, local_files_only=True)
        model = transformers.SegformerForSemanticSegmentation.from_pretrained(SEGFORMER_ID, local_files_only=True).eval().to(device)
    except Exception:
        proc = transformers.AutoImageProcessor.from_pretrained(SEGFORMER_ID)
        model = transformers.SegformerForSemanticSegmentation.from_pretrained(SEGFORMER_ID).eval().to(device)

    k_open = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    for p in image_paths:
        pil = Image.open(p).convert("RGB")
        W, H = pil.size
        inp = proc(images=pil, return_tensors="pt").to(device)
        with torch.no_grad():
            logits = model(**inp).logits
        sem = F.interpolate(logits, size=(H, W), mode="bilinear", align_corners=False).argmax(dim=1)[0].cpu().numpy().astype(np.uint8)
        g_raw = np.isin(sem, GARMENT_CLASS_UPPER).astype(np.uint8) * 255
        g_clean = cv2.morphologyEx(g_raw, cv2.MORPH_OPEN, k_open)
        Image.fromarray(g_clean, mode="L").save(mask_dir / f"{p.stem}.png")
        Image.fromarray(sem, mode="L").save(sem_dir / f"{p.stem}.png")

    del model
    if device == "cuda":
        torch.cuda.empty_cache()


def _load_lightglue():
    import transformers

    device = "cuda" if torch.cuda.is_available() else "cpu"
    try:
        lg = transformers.LightGlueForKeypointMatching.from_pretrained(LIGHTGLUE_ID, local_files_only=True).eval().to(device)
    except Exception:
        lg = transformers.LightGlueForKeypointMatching.from_pretrained(LIGHTGLUE_ID).eval().to(device)
    lg.depth_confidence = -1.0
    lg.width_confidence = -1.0
    return lg, device


def _read_bgr_unicode(img_path: Path) -> np.ndarray:
    """Read image in BGR uint8 safely on Windows even when path contains non-ASCII characters."""
    buf = np.fromfile(str(img_path), dtype=np.uint8)
    bgr = cv2.imdecode(buf, cv2.IMREAD_COLOR)
    if bgr is None:
        bgr = np.asarray(Image.open(img_path).convert("RGB"))[:, :, ::-1].copy()
    return bgr


def _extract_superpoint_kpts(lg, device: str, img_path: Path, mask: np.ndarray | None = None, max_kp: int = 2048):
    bgr = _read_bgr_unicode(img_path)
    rgb = bgr[:, :, ::-1].astype(np.float32) / 255.0
    H, W = rgb.shape[:2]
    gray = 0.299 * rgb[:, :, 0] + 0.587 * rgb[:, :, 1] + 0.114 * rgb[:, :, 2]
    pad_h, pad_w = (8 - H % 8) % 8, (8 - W % 8) % 8
    H_pad, W_pad = H + pad_h, W + pad_w
    if pad_h or pad_w:
        gray = np.pad(gray, ((0, pad_h), (0, pad_w)), mode="constant", constant_values=0.0)
    t_in = torch.from_numpy(gray)[None, None].repeat(1, 3, 1, 1).to(device)
    with torch.no_grad():
        kp_norm, scores, desc, det_mask = lg.keypoint_detector(t_in)[:4]
    valid = det_mask[0] > 0
    kpts_xy = kp_norm[0][valid].clone()
    kpts_xy[:, 0] *= float(W_pad)
    kpts_xy[:, 1] *= float(H_pad)
    sc, ds = scores[0][valid], desc[0][valid]
    in_img = (kpts_xy[:, 0] >= 0) & (kpts_xy[:, 0] < W) & (kpts_xy[:, 1] >= 0) & (kpts_xy[:, 1] < H)
    kpts_xy, sc, ds = kpts_xy[in_img], sc[in_img], ds[in_img]

    if mask is not None:
        ui = torch.clamp(torch.round(kpts_xy[:, 0]).long(), 0, W - 1).cpu().numpy()
        vi = torch.clamp(torch.round(kpts_xy[:, 1]).long(), 0, H - 1).cpu().numpy()
        fg = torch.from_numpy(mask[vi, ui]).to(kpts_xy.device)
        if int(fg.sum()) >= 30:
            kpts_xy, sc, ds = kpts_xy[fg], sc[fg], ds[fg]

    if len(kpts_xy) > max_kp:
        top = torch.topk(sc, max_kp).indices
        kpts_xy, ds = kpts_xy[top], ds[top]
    return kpts_xy.contiguous(), ds.contiguous(), (W, H)


def _raycast_ref_keypoints_3d(
    ray_scene: o3d.t.geometry.RaycastingScene,
    kpts_xy: np.ndarray,
    K: np.ndarray,
    R: np.ndarray,
    t: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """Cast rays from reference camera through 2D keypoints onto aligned garment mesh to obtain 3D coordinates."""
    if len(kpts_xy) == 0:
        return np.zeros((0, 3), dtype=np.float64), np.zeros(0, dtype=bool)
    C = -R.T @ t
    x_cam = (kpts_xy[:, 0] - K[0, 2]) / K[0, 0]
    y_cam = (kpts_xy[:, 1] - K[1, 2]) / K[1, 1]
    dirs_cam = np.stack([x_cam, y_cam, np.ones_like(x_cam)], axis=1)
    dirs_world = dirs_cam @ R
    dirs_world /= np.maximum(np.linalg.norm(dirs_world, axis=1, keepdims=True), 1e-12)
    rays = np.hstack([np.broadcast_to(C[None, :], (len(dirs_world), 3)), dirs_world]).astype(np.float32)
    t_hit = ray_scene.cast_rays(o3d.core.Tensor(rays, o3d.core.float32))["t_hit"].numpy()
    ok = np.isfinite(t_hit) & (t_hit > 0.1) & (t_hit < 50.0)
    P_3d = np.zeros((len(kpts_xy), 3), dtype=np.float64)
    P_3d[ok] = C[None, :] + dirs_world[ok] * t_hit[ok, None]
    return P_3d, ok


def register_reshoot_poses_lightglue_pnp(
    reshoot_items: list[dict[str, Any]],
    ref_views: list[dict[str, Any]],
    V_scan: np.ndarray,
    F_uv: np.ndarray,
    max_ref_views: int = 36,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Register pose-free reshoot images into scan space using SuperPoint + LightGlue + PnP RANSAC."""
    lg, device = _load_lightglue()
    ray_scene = o3d.t.geometry.RaycastingScene()
    ray_scene.add_triangles(
        o3d.core.Tensor(V_scan.astype(np.float32), o3d.core.float32),
        o3d.core.Tensor(F_uv.astype(np.uint32), o3d.core.uint32),
    )

    if len(ref_views) > max_ref_views:
        idxs = np.linspace(0, len(ref_views) - 1, num=max_ref_views, dtype=int)
        refs = [ref_views[int(i)] for i in idxs]
    else:
        refs = ref_views

    # Reference camera intrinsic baseline + radial distance guardrail around mesh center
    ref_K0 = refs[0]["K"]
    ref_W0, ref_H0 = refs[0]["W"], refs[0]["H"]
    mesh_center = V_scan.mean(axis=0)
    ref_radii = [float(np.linalg.norm(rv["C"] - mesh_center)) for rv in refs]
    med_rad = float(np.median(ref_radii)) if ref_radii else 0.0

    ref_cache: list[dict[str, Any]] = []
    for rv in refs:
        m_ref = np.asarray(Image.open(rv["mask_path"]).convert("L")) > 127
        kp, ds, (rw, rh) = _extract_superpoint_kpts(lg, device, rv["img_path"], mask=m_ref, max_kp=1536)
        P_3d, ok_hit = _raycast_ref_keypoints_3d(ray_scene, kp.cpu().numpy(), rv["K"], rv["R"], rv["t"])
        if int(ok_hit.sum()) < 15:
            continue
        ok_t = torch.from_numpy(ok_hit).to(kp.device)
        ref_cache.append(
            {
                "stem": rv["stem"],
                "kp": kp[ok_t],
                "ds": ds[ok_t],
                "P3d": P_3d[ok_hit],
                "W": rw,
                "H": rh,
            }
        )

    if not ref_cache:
        raise RuntimeError("参考视角未能投影到网格表面生成 3D 特征点")

    # Pre-extract query keypoints once per reshoot frame, then run up to 4 bootstrapping passes
    query_cache: list[dict[str, Any] | None] = []
    for item in reshoot_items:
        kp_q, ds_q, (W, H) = _extract_superpoint_kpts(lg, device, item["img_path"], mask=item["raw_mask"], max_kp=1536)
        query_cache.append({"kp": kp_q, "ds": ds_q, "W": W, "H": H})

    registered: list[dict[str, Any]] = []
    reg_details_by_idx: dict[int, dict[str, Any]] = {}
    pending_idxs = list(range(len(reshoot_items)))

    for pass_idx in range(5):
        if not pending_idxs:
            break
        next_pending: list[int] = []
        round_new_refs: list[dict[str, Any]] = []
        for idx in pending_idxs:
            item = reshoot_items[idx]
            qc = query_cache[idx]
            assert qc is not None
            kp_q, ds_q, W, H = qc["kp"], qc["ds"], qc["W"], qc["H"]
            if len(kp_q) < 15:
                reg_details_by_idx[idx] = {"stem": item["stem"], "registered": False, "reason": "insufficient_keypoints"}
                continue

            cand_matches: list[tuple[int, np.ndarray, np.ndarray, str]] = []
            nq = len(kp_q)
            for rc in ref_cache:
                nr = len(rc["kp"])
                max_k = max(nq, nr)
                kp_pair = torch.zeros((1, 2, max_k, 2), dtype=torch.float32, device=device)
                ds_pair = torch.zeros((1, 2, max_k, 256), dtype=torch.float32, device=device)
                mk_pair = torch.zeros((1, 2, max_k), dtype=torch.int64, device=device)
                kp_pair[0, 0, :nq], kp_pair[0, 1, :nr] = kp_q, rc["kp"]
                ds_pair[0, 0, :nq], ds_pair[0, 1, :nr] = ds_q, rc["ds"]
                mk_pair[0, 0, :nq], mk_pair[0, 1, :nr] = 1, 1

                with torch.no_grad():
                    m_out, s_out, _, _, _ = lg._match_image_pair(
                        kp_pair,
                        ds_pair,
                        max(H, rc["H"]),
                        max(W, rc["W"]),
                        mask=mk_pair,
                    )
                m0 = m_out.reshape(1, 2, max_k)[0, 0, :nq].cpu().numpy()
                sc0 = s_out.reshape(1, 2, max_k)[0, 0, :nq].cpu().numpy()
                valid_m = (m0 >= 0) & (m0 < nr) & (sc0 >= 0.15)
                n_m = int(valid_m.sum())
                if n_m >= 8:
                    q_idx = np.nonzero(valid_m)[0]
                    r_idx = m0[valid_m]
                    pts2d = kp_q.cpu().numpy()[q_idx]
                    pts3d = rc["P3d"][r_idx]
                    cand_matches.append((n_m, pts2d, pts3d, rc["stem"]))

            if not cand_matches:
                reg_details_by_idx[idx] = {"stem": item["stem"], "registered": False, "reason": "no_lightglue_matches"}
                next_pending.append(idx)
                continue

            cand_matches.sort(key=lambda x: x[0], reverse=True)
            top_refs = cand_matches[:6]
            all_2d = np.concatenate([x[1] for x in top_refs], axis=0).astype(np.float64)
            all_3d = np.concatenate([x[2] for x in top_refs], axis=0).astype(np.float64)
            if len(all_2d) < 10:
                reg_details_by_idx[idx] = {"stem": item["stem"], "registered": False, "reason": "too_few_2d3d_pairs"}
                next_pending.append(idx)
                continue

            scale_w = W / float(ref_W0)
            scale_h = H / float(ref_H0)
            K_new = np.array(
                [
                    [ref_K0[0, 0] * scale_w, 0.0, W / 2.0],
                    [0.0, ref_K0[1, 1] * scale_h, H / 2.0],
                    [0.0, 0.0, 1.0],
                ],
                dtype=np.float64,
            )

            ok_pnp, rvec, tvec, inliers = cv2.solvePnPRansac(
                all_3d,
                all_2d,
                K_new,
                None,
                iterationsCount=2000,
                reprojectionError=8.0,
                confidence=0.995,
                flags=cv2.SOLVEPNP_EPNP,
            )
            n_inl = int(len(inliers)) if (ok_pnp and inliers is not None) else 0
            if not ok_pnp or n_inl < 10:
                reg_details_by_idx[idx] = {"stem": item["stem"], "registered": False, "inliers": n_inl, "reason": "pnp_failed"}
                next_pending.append(idx)
                continue

            rvec, tvec = cv2.solvePnPRefineLM(
                all_3d[inliers[:, 0]],
                all_2d[inliers[:, 0]],
                K_new,
                None,
                rvec,
                tvec,
            )
            R_est, _ = cv2.Rodrigues(rvec)
            t_est = tvec.reshape(3)
            C_est = -R_est.T @ t_est

            dist_est = float(np.linalg.norm(C_est - mesh_center))
            if med_rad > 1e-4 and not (0.35 * med_rad <= dist_est <= 2.5 * med_rad):
                reg_details_by_idx[idx] = {
                    "stem": item["stem"],
                    "registered": False,
                    "inliers": n_inl,
                    "reason": f"camera_radius_outlier({dist_est:.2f} vs med {med_rad:.2f})",
                }
                next_pending.append(idx)
                continue

            proj, _ = cv2.projectPoints(all_3d[inliers[:, 0]], rvec, tvec, K_new, None)
            reproj_err = float(np.mean(np.linalg.norm(proj.reshape(-1, 2) - all_2d[inliers[:, 0]], axis=1)))

            registered.append(
                {
                    **item,
                    "W": W,
                    "H": H,
                    "K": K_new,
                    "R": R_est,
                    "t": t_est,
                    "C": C_est,
                }
            )
            reg_details_by_idx[idx] = {
                "stem": item["stem"],
                "registered": True,
                "pass": pass_idx + 1,
                "matched_refs": [x[3] for x in top_refs],
                "total_2d3d_pairs": int(len(all_2d)),
                "pnp_inliers": n_inl,
                "inlier_reproj_err_px": round(reproj_err, 3),
            }
            if n_inl >= 15 and reproj_err <= 5.0:
                P_new3d, ok_hit = _raycast_ref_keypoints_3d(ray_scene, kp_q.cpu().numpy(), K_new, R_est, t_est)
                if int(ok_hit.sum()) >= 15:
                    ok_t = torch.from_numpy(ok_hit).to(kp_q.device)
                    round_new_refs.append(
                        {
                            "stem": item["stem"],
                            "kp": kp_q[ok_t],
                            "ds": ds_q[ok_t],
                            "P3d": P_new3d[ok_hit],
                            "W": W,
                            "H": H,
                        }
                    )

        pending_idxs = next_pending
        if not round_new_refs:
            break
        # Batch-append after full BFS wave so both sides of a gap converge inward symmetrically
        ref_cache.extend(round_new_refs)

    reg_details = [reg_details_by_idx[i] for i in range(len(reshoot_items))]

    del lg
    if device == "cuda":
        torch.cuda.empty_cache()
    return registered, {
        "total_reshoot_frames": len(reshoot_items),
        "registered_frames": len(registered),
        "frames": reg_details,
    }


def _render_patch_diff_map(
    V_uv: np.ndarray,
    F_uv: np.ndarray,
    uvs: np.ndarray,
    valid_m: np.ndarray,
    old_hit: np.ndarray,
    fill_mask: np.ndarray,
    upgrade_mask: np.ndarray,
    out_path: Path,
) -> None:
    """Visualize newly filled (cyan) and quality-upgraded (amber) regions on UV and front/back views."""
    H, W = valid_m.shape
    diff_tex = np.full((H, W, 3), [0.16, 0.18, 0.22], dtype=np.float32)
    diff_tex[valid_m] = [0.42, 0.45, 0.50]
    diff_tex[old_hit] = [0.78, 0.80, 0.84]
    diff_tex[upgrade_mask] = [1.0, 0.72, 0.15]  # Amber: quality upgraded
    diff_tex[fill_mask] = [0.10, 0.90, 0.85]     # Cyan: newly filled missing patch

    V = V_uv.astype(np.float64)
    center = V.mean(axis=0)
    span = float(np.max(V.max(axis=0) - V.min(axis=0)))
    tw, th = 360, 440
    K = np.array([[560.0, 0.0, tw / 2.0], [0.0, 560.0, th / 2.0], [0.0, 0.0, 1.0]], dtype=np.float64)
    radius = max(1.65 * span, 1.2)
    up_axis = np.array([0.0, 1.0, 0.0], dtype=np.float64)

    cw, ch, pad = 280, 342, 12
    sheet = Image.new("RGB", (4 * cw + 5 * pad, ch + 2 * pad + 52), (18, 20, 26))
    draw = ImageDraw.Draw(sheet)
    fh, fc = _load_font(17), _load_font(13)
    draw.text(
        (pad, 14),
        "增量补拍生效热力分布（浅灰=原清晰锁定区 | 青色=补拍填补缺块 | 橙黄=质量升级区）",
        fill=(240, 245, 255),
        font=fh,
    )

    uv_pil = Image.fromarray((diff_tex * 255).astype(np.uint8)).resize((cw, ch), Image.Resampling.NEAREST)
    sheet.paste(uv_pil, (pad, 52 + pad))
    draw.text((pad + 8, 52 + pad + 8), "UV 空间增量分布", fill=(120, 245, 240), font=fc)

    for col_idx, (deg, lbl) in enumerate([(0, "正面 0°"), (90, "侧面 90°"), (180, "背面 180°")], start=1):
        rad = math.radians(deg)
        cam_pos = center + radius * np.array([math.sin(rad), 0.05, math.cos(rad)], dtype=np.float64)
        fwd = center - cam_pos
        fwd /= np.linalg.norm(fwd)
        right = np.cross(fwd, up_axis)
        right /= np.linalg.norm(right)
        down = np.cross(fwd, right)
        R = np.stack([right, down, fwd], axis=0)
        t = -R @ cam_pos
        rgb, _ = _rasterize_textured_view(V, F_uv, uvs, diff_tex, K, R, t, tw, th, shaded=True)
        x = pad + col_idx * (cw + pad)
        y = 52 + pad
        sheet.paste(Image.fromarray((np.clip(rgb, 0, 1) * 255).astype(np.uint8)).resize((cw, ch), Image.Resampling.LANCZOS), (x, y))
        draw.rectangle([x, y, x + cw - 1, y + ch - 1], outline=(70, 80, 96), width=1)
        draw.text((x + 8, y + 8), lbl, fill=(235, 240, 248), font=fc)

    sheet.save(out_path, quality=94)


def _render_before_after_4views(
    V_uv: np.ndarray,
    F_uv: np.ndarray,
    uvs: np.ndarray,
    tex_before: np.ndarray,
    tex_after: np.ndarray,
    out_path: Path,
) -> None:
    V = V_uv.astype(np.float64)
    center = V.mean(axis=0)
    span = float(np.max(V.max(axis=0) - V.min(axis=0)))
    tw, th = 360, 440
    K = np.array([[560.0, 0.0, tw / 2.0], [0.0, 560.0, th / 2.0], [0.0, 0.0, 1.0]], dtype=np.float64)
    radius = max(1.65 * span, 1.2)
    up_axis = np.array([0.0, 1.0, 0.0], dtype=np.float64)

    cw, ch, pad = 280, 342, 12
    sheet = Image.new("RGB", (4 * cw + 5 * pad, 2 * ch + 3 * pad + 48), (18, 20, 26))
    draw = ImageDraw.Draw(sheet)
    fh, fc = _load_font(18), _load_font(14)
    draw.text((pad, 12), "增量补拍优化对照: 上排 补拍前缺陷模型 vs 下排 定向补拍增量融合后", fill=(240, 245, 255), font=fh)

    for row_idx, (row_name, tex) in enumerate([("补拍前", tex_before), ("补拍优化后", tex_after)]):
        for col_idx, (deg, lbl) in enumerate([(0, "正面 0°"), (45, "斜前 45°"), (90, "侧面 90°"), (180, "背面 180°")]):
            rad = math.radians(deg)
            cam_pos = center + radius * np.array([math.sin(rad), 0.05, math.cos(rad)], dtype=np.float64)
            fwd = center - cam_pos
            fwd /= np.linalg.norm(fwd)
            right = np.cross(fwd, up_axis)
            right /= np.linalg.norm(right)
            down = np.cross(fwd, right)
            R = np.stack([right, down, fwd], axis=0)
            t = -R @ cam_pos
            rgb, _ = _rasterize_textured_view(V, F_uv, uvs, tex, K, R, t, tw, th, shaded=True)
            x = pad + col_idx * (cw + pad)
            y = 48 + pad + row_idx * (ch + pad)
            sheet.paste(Image.fromarray((np.clip(rgb, 0, 1) * 255).astype(np.uint8)).resize((cw, ch), Image.Resampling.LANCZOS), (x, y))
            draw.rectangle([x, y, x + cw - 1, y + ch - 1], outline=(70, 80, 96), width=1)
            draw.text((x + 8, y + 8), f"{row_name} · {lbl}", fill=(140, 245, 180) if row_idx == 1 else (255, 210, 125), font=fc)

    sheet.save(out_path, quality=95)


def refine_texture_with_reshoot(
    state_dir: Path,
    output_dir: Path,
    reshoot_images_dir: Path | None = None,
    reshoot_video: Path | None = None,
    reshoot_masks_dir: Path | None = None,
    reshoot_poses_path: Path | None = None,
    ref_scan_dir: Path | None = None,
    ref_exclude_stems: set[str] | None = None,
    auto_segformer: bool = True,
    max_reshoot_frames: int = 12,
    facing_min: float = 0.20,
    upgrade_ratio: float = 1.25,
    exposure_equalize: bool = True,
    eval_scan_dir: Path | None = None,
    glb_name: str = "garment-refined.glb",
) -> dict[str, Any]:
    """Incrementally refine an existing transferred garment model with targeted reshoot observations."""
    output_dir.mkdir(parents=True, exist_ok=True)
    state_path = state_dir / "transfer_state.npz"
    if not state_path.exists():
        raise FileNotFoundError(f"未找到增量状态包: {state_path}")
    with np.load(state_path) as st:
        V_uv = st["V_uv"]
        F_uv = st["F_uv"]
        N_uv = st["N_uv"]
        uvs = st["uvs"]
        V_uv_posed = st["V_uv_posed"]
        T_tmpl_to_scan = st["T_tmpl_to_scan"]
        old_obs_rgb = st["tex_observed_rgb"].copy()
        tex_before_final = st["tex_final_rgb"].copy()
        old_hit = st["hit_mask"].astype(bool)
        old_quality = st["quality_map"].astype(np.float32)
        old_obs_count = st["obs_count_map"].astype(np.int32)
        valid_m = st["valid_m"].astype(bool)
        pos_orig_m = st["pos_orig_m"]
        norm_orig_m = st["norm_orig_m"]
        pos_sample_m = st["pos_sample_m"]
        norm_sample_m = st["norm_sample_m"]
        vcol_m = st["vcol_m"]
        lab_target = st["lab_target"]
        target_L, target_a, target_b = float(lab_target[0]), float(lab_target[1]), float(lab_target[2])

    # 1. Gather reshoot image paths
    if reshoot_video is not None:
        img_paths = _extract_reshoot_frames_from_video(reshoot_video, output_dir / "_reshoot_frames", max_frames=max_reshoot_frames)
    elif reshoot_images_dir is not None:
        img_paths = sorted(
            p for p in reshoot_images_dir.iterdir() if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
        )
    else:
        raise ValueError("必须提供 --reshoot-images-dir 或 --reshoot-video")
    if not img_paths:
        raise RuntimeError("未找到有效补拍图像")

    # 2. Load or generate masks
    mask_dir = reshoot_masks_dir
    sem_dir = output_dir / "_reshoot_sem"
    if mask_dir is None:
        mask_dir = output_dir / "_reshoot_masks"
        if auto_segformer:
            _generate_segformer_masks(img_paths, mask_dir, sem_dir)
        else:
            mask_dir.mkdir(parents=True, exist_ok=True)
            for p in img_paths:
                im = Image.open(p)
                Image.new("L", im.size, 255).save(mask_dir / f"{p.stem}.png")

    reshoot_items: list[dict[str, Any]] = []
    for p in img_paths:
        mp = mask_dir / f"{p.stem}.png"
        if not mp.exists():
            raise FileNotFoundError(f"补拍图像缺少对应掩膜: {mp}")
        sp = sem_dir / f"{p.stem}.png"
        raw_mask = np.asarray(Image.open(mp).convert("L")) > 127
        reshoot_items.append(
            {
                "stem": p.stem,
                "img_path": p,
                "mask_path": mp,
                "sem_path": sp if sp.exists() else None,
                "raw_mask": raw_mask,
            }
        )

    # 3. Resolve camera poses (explicit JSON or LightGlue+PnP against reference scan views)
    V_scan = (V_uv_posed.astype(np.float64) @ T_tmpl_to_scan[:3, :3].T) + T_tmpl_to_scan[:3, 3]
    if reshoot_poses_path is not None:
        with open(reshoot_poses_path, encoding="utf-8") as fh:
            poses_data = json.load(fh)
        registered_views: list[dict[str, Any]] = []
        for item in reshoot_items:
            if item["stem"] not in poses_data:
                continue
            pd = poses_data[item["stem"]]
            W, H = int(pd["width"]), int(pd["height"])
            K = np.array(
                [
                    [float(pd["fx"]), 0.0, float(pd.get("cx", W / 2.0))],
                    [0.0, float(pd["fy"]), float(pd.get("cy", H / 2.0))],
                    [0.0, 0.0, 1.0],
                ],
                dtype=np.float64,
            )
            R = np.array(pd["R"], dtype=np.float64)
            t = np.array(pd["t"], dtype=np.float64)
            registered_views.append({**item, "W": W, "H": H, "K": K, "R": R, "t": t, "C": -R.T @ t})
        reg_report = {
            "mode": "explicit_poses",
            "total_reshoot_frames": len(reshoot_items),
            "registered_frames": len(registered_views),
        }
    else:
        if ref_scan_dir is None:
            raise ValueError("未提供 --reshoot-poses 时必须指定 --ref-scan-dir 以执行 LightGlue+PnP 增量注册")
        ref_views, _ = load_scan_views(ref_scan_dir, max_views=90)
        if ref_exclude_stems:
            ref_views = [v for v in ref_views if v["stem"] not in ref_exclude_stems]
        ref_ply = ref_scan_dir / "mesh" / "garment-clean.ply"
        if ref_ply.is_file():
            ref_mesh = trimesh.load(str(ref_ply), force="mesh", process=False)
            V_ref = np.asarray(ref_mesh.vertices, dtype=np.float64)
            F_ref = np.asarray(ref_mesh.faces, dtype=np.int64)
        else:
            V_ref, F_ref = V_scan, F_uv
        registered_views, reg_report = register_reshoot_poses_lightglue_pnp(
            reshoot_items=reshoot_items,
            ref_views=ref_views,
            V_scan=V_ref,
            F_uv=F_ref,
            max_ref_views=36,
        )
        reg_report["mode"] = "superpoint_lightglue_pnp"

    if not registered_views:
        raise RuntimeError(f"补拍帧全部注册失败: {reg_report}")

    # 4. Back-project registered reshoot views onto template UV
    vy, vx = np.nonzero(valid_m)
    P_s, N_s = transform_points_and_normals(
        pos_sample_m[vy, vx].astype(np.float64),
        norm_sample_m[vy, vx].astype(np.float64),
        T_tmpl_to_scan,
    )
    ray_scene = o3d.t.geometry.RaycastingScene()
    ray_scene.add_triangles(
        o3d.core.Tensor(V_scan.astype(np.float32), o3d.core.float32),
        o3d.core.Tensor(F_uv.astype(np.uint32), o3d.core.uint32),
    )
    bbox_diag = float(np.linalg.norm(V_scan.max(axis=0) - V_scan.min(axis=0)))
    occ_tol = max(0.015, 0.025 * bbox_diag)

    cam_obs: list[tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray, float]] = []
    sharp_vals: list[float] = []
    for vw in registered_views:
        bgr = _read_bgr_unicode(vw["img_path"])
        H, W = bgr.shape[:2]
        m_g, rw_map = _load_clean_bake_mask(vw, erode_px=6)
        if int(m_g.sum()) < 50:
            continue

        if exposure_equalize and int(m_g.sum()) > 300:
            lab = cv2.cvtColor(bgr, cv2.COLOR_BGR2LAB)
            cur_L = float(np.mean(lab[:, :, 0][m_g]))
            cur_a = float(np.mean(lab[:, :, 1][m_g]))
            cur_b = float(np.mean(lab[:, :, 2][m_g]))
            sm = cv2.GaussianBlur(m_g.astype(np.float32), (0, 0), sigmaX=3.0)
            if cur_L < target_L * 0.92:
                Lc = cv2.createCLAHE(clipLimit=1.4, tileGridSize=(8, 8)).apply(lab[:, :, 0]).astype(np.float32)
                Lb = np.clip((0.65 * lab[:, :, 0] + 0.35 * Lc) * float(np.clip(target_L / max(cur_L, 12.0), 1.0, 1.38)), 0.0, 242.0)
                lab[:, :, 0] = np.clip((1.0 - sm) * lab[:, :, 0] + sm * Lb, 0.0, 255.0).astype(np.uint8)
            da = 0.65 * float(np.clip(target_a - cur_a, -12.0, 12.0))
            db = 0.65 * float(np.clip(target_b - cur_b, -12.0, 12.0))
            if abs(da) > 0.25 or abs(db) > 0.25:
                lab[:, :, 1] = np.clip(lab[:, :, 1].astype(np.float32) + sm * da, 0.0, 255.0).astype(np.uint8)
                lab[:, :, 2] = np.clip(lab[:, :, 2].astype(np.float32) + sm * db, 0.0, 255.0).astype(np.uint8)
            bgr = cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)

        rgb = bgr[:, :, ::-1].astype(np.float32) / 255.0
        ys, xs = np.nonzero(m_g)
        roi_gray = cv2.cvtColor(bgr[int(ys.min()):int(ys.max()) + 1, int(xs.min()):int(xs.max()) + 1], cv2.COLOR_BGR2GRAY)
        sharp = max(1.0, float(cv2.Laplacian(roi_gray, cv2.CV_32F).var()))
        sharp_vals.append(sharp)

        R, t, K, C = vw["R"], vw["t"], vw["K"], vw["C"]
        Xc = P_s @ R.T + t
        z = Xc[:, 2]
        ok_z = z > 0.2
        u = np.where(ok_z, K[0, 0] * Xc[:, 0] / np.maximum(z, 0.2) + K[0, 2], -1.0)
        v = np.where(ok_z, K[1, 1] * Xc[:, 1] / np.maximum(z, 0.2) + K[1, 2], -1.0)
        inb = ok_z & (u >= 1.0) & (u < W - 2) & (v >= 1.0) & (v < H - 2)
        if not inb.any():
            continue
        idx = np.nonzero(inb)[0]
        ui = np.rint(u[idx]).astype(np.int32)
        vi_px = np.rint(v[idx]).astype(np.int32)
        in_mask = m_g[vi_px, ui]
        if not in_mask.any():
            continue
        idx = idx[in_mask]

        view_dir = C[None, :] - P_s[idx]
        view_dir /= np.maximum(np.linalg.norm(view_dir, axis=1, keepdims=True), 1e-12)
        fac = (N_s[idx] * view_dir).sum(axis=1)
        kp = fac > facing_min
        if not kp.any():
            continue
        idx, fac = idx[kp], fac[kp]

        dirs = P_s[idx] - C[None, :]
        dist = np.linalg.norm(dirs, axis=1)
        unit_dirs = dirs / np.maximum(dist[:, None], 1e-8)
        rays = np.hstack([np.broadcast_to(C[None, :], (len(idx), 3)), unit_dirs]).astype(np.float32)
        t_hit = ray_scene.cast_rays(o3d.core.Tensor(rays, o3d.core.float32))["t_hit"].numpy()
        unocc = np.isfinite(t_hit) & (dist <= t_hit + occ_tol)
        if not unocc.any():
            continue

        idx = idx[unocc]
        ui_u = np.clip(np.rint(u[idx]).astype(np.int32), 0, W - 1)
        vi_u = np.clip(np.rint(v[idx]).astype(np.int32), 0, H - 1)
        rw_obs = rw_map[vi_u, ui_u]
        cam_obs.append((idx, _bilerp_rgb(rgb, u[idx], v[idx]), fac[unocc], np.maximum(z[idx], 0.35), rw_obs, sharp))

    med_s = float(np.median(sharp_vals)) if sharp_vals else 1.0
    sw_list = [float(np.clip((s / max(med_s, 1e-6)) ** 0.75, 0.70, 1.35)) for s in sharp_vals]
    acc1 = np.zeros((len(P_s), 3), dtype=np.float64)
    w1 = np.zeros(len(P_s), dtype=np.float64)
    new_cnt = np.zeros(len(P_s), dtype=np.int32)
    for obs_i, (idx, cols, fac, zs, rw_obs, _sharp) in enumerate(cam_obs):
        w = ((fac ** 4.5) * sw_list[obs_i] * rw_obs) / (zs ** 2.0)
        acc1[idx] += w[:, None] * cols
        w1[idx] += w
        new_cnt[idx] += 1

    h1 = w1 > 1e-8
    C1 = np.zeros((len(P_s), 3), dtype=np.float32)
    C1[h1] = (acc1[h1] / w1[h1, None]).astype(np.float32)

    # Pass 2: reject pose/silhouette outliers (> 0.10 L1) so back prints stay crisp without ghosting
    acc2 = np.zeros((len(P_s), 3), dtype=np.float64)
    w2 = np.zeros(len(P_s), dtype=np.float64)
    for obs_i, (idx, cols, fac, zs, rw_obs, _sharp) in enumerate(cam_obs):
        g = np.mean(np.abs(cols - C1[idx]), axis=1) <= 0.10
        if g.any():
            w = ((fac[g] ** 4.5) * sw_list[obs_i] * rw_obs[g]) / (zs[g] ** 2.0)
            acc2[idx[g]] += w[:, None] * cols[g]
            w2[idx[g]] += w

    h2 = w2 > 1e-8
    new_hit_flat = h1 | h2
    C_new = np.zeros((len(P_s), 3), dtype=np.float32)
    C_new[h1 & (~h2)] = C1[h1 & (~h2)]
    C_new[h2] = np.clip(acc2[h2] / w2[h2, None], 0.0, 1.0).astype(np.float32)

    new_rgb = np.zeros_like(old_obs_rgb)
    new_rgb[vy[new_hit_flat], vx[new_hit_flat]] = C_new[new_hit_flat]
    new_hit = np.zeros(valid_m.shape, dtype=bool)
    new_hit[vy[new_hit_flat], vx[new_hit_flat]] = True
    new_quality = np.zeros(valid_m.shape, dtype=np.float32)
    new_quality[vy, vx] = np.where(h2, w2, w1).astype(np.float32)
    new_obs_count = np.zeros(valid_m.shape, dtype=np.int32)
    new_obs_count[vy, vx] = new_cnt

    # 5. Cross-batch Lab color harmonization on overlapping pixels
    overlap = old_hit & new_hit
    if exposure_equalize and int(overlap.sum()) >= 150:
        old_u8 = (np.clip(old_obs_rgb, 0, 1) * 255).astype(np.uint8)
        new_u8 = (np.clip(new_rgb, 0, 1) * 255).astype(np.uint8)
        old_lab = cv2.cvtColor(old_u8, cv2.COLOR_RGB2LAB).astype(np.float32)
        new_lab = cv2.cvtColor(new_u8, cv2.COLOR_RGB2LAB).astype(np.float32)
        d_lab = np.median(old_lab[overlap] - new_lab[overlap], axis=0)
        d_lab = np.clip(d_lab, -14.0, 14.0)
        new_lab[new_hit] = np.clip(new_lab[new_hit] + 0.75 * d_lab[None, :], 0.0, 255.0)
        new_rgb = cv2.cvtColor(new_lab.astype(np.uint8), cv2.COLOR_LAB2RGB).astype(np.float32) / 255.0

    # 6. Quality-gated incremental fusion (lock crisp existing pixels, fill gaps, upgrade weak pixels)
    fill_mask = new_hit & (~old_hit)
    upgrade_mask = new_hit & old_hit & (new_quality > old_quality * upgrade_ratio)
    strong_override = upgrade_mask & (new_quality > old_quality * 2.5)
    soft_upgrade = upgrade_mask & (~strong_override)

    merged_obs_rgb = old_obs_rgb.copy()
    merged_obs_rgb[fill_mask] = new_rgb[fill_mask]
    merged_obs_rgb[strong_override] = new_rgb[strong_override]
    if soft_upgrade.any():
        w_old_u = old_quality[soft_upgrade][..., None]
        w_new_u = new_quality[soft_upgrade][..., None]
        merged_obs_rgb[soft_upgrade] = (
            (w_old_u * old_obs_rgb[soft_upgrade] + w_new_u * new_rgb[soft_upgrade])
            / np.maximum(w_old_u + w_new_u, 1e-8)
        )

    merged_hit = old_hit | new_hit
    merged_quality = np.maximum(old_quality, new_quality)
    merged_obs_count = old_obs_count + new_obs_count

    defect_before = diagnose_model_defects(V_uv, pos_orig_m, norm_orig_m, valid_m, old_hit, old_quality, old_obs_count)
    defect_after = diagnose_model_defects(V_uv, pos_orig_m, norm_orig_m, valid_m, merged_hit, merged_quality, merged_obs_count)
    with open(output_dir / "defect_report_after.json", "w", encoding="utf-8") as fh:
        json.dump(defect_after, fh, ensure_ascii=False, indent=2)

    tex_refined, fill_stats = inpaint_and_dilate_uv(
        merged_obs_rgb,
        valid_m=valid_m,
        hit_mask=merged_hit,
        pos_orig_m=pos_orig_m,
        norm_orig_m=norm_orig_m,
    )
    out_glb, albedo_p, normal_p = export_clean_glb(V_uv, F_uv, N_uv, uvs, tex_refined, output_dir, glb_name=glb_name)

    _render_before_after_4views(V_uv, F_uv, uvs, tex_before_final, tex_refined, output_dir / "before_after_4views.png")
    _render_patch_diff_map(V_uv, F_uv, uvs, valid_m, old_hit, fill_mask, upgrade_mask, output_dir / "patch_diff_map.png")

    reproj_cmp = None
    eval_src = eval_scan_dir or ref_scan_dir
    if eval_src is not None:
        eval_views, _ = load_scan_views(eval_src, max_views=24)
        m_before = evaluate_reprojection_metrics(
            V_uv_posed.astype(np.float32), F_uv, uvs, T_tmpl_to_scan, tex_before_final, vcol_m, eval_views
        )
        m_after = evaluate_reprojection_metrics(
            V_uv_posed.astype(np.float32), F_uv, uvs, T_tmpl_to_scan, tex_refined, vcol_m, eval_views
        )
        reproj_cmp = {
            "before_psnr_db": m_before["transferred_psnr_db"],
            "after_psnr_db": m_after["transferred_psnr_db"],
            "psnr_delta_db": round(m_after["transferred_psnr_db"] - m_before["transferred_psnr_db"], 3),
            "before_lab_deltaE": m_before["transferred_lab_deltaE"],
            "after_lab_deltaE": m_after["transferred_lab_deltaE"],
        }

    locked_ok = old_hit & (~upgrade_mask)
    locked_drift = float(np.max(np.abs(merged_obs_rgb[locked_ok] - old_obs_rgb[locked_ok]))) if locked_ok.any() else 0.0

    report = {
        "registration": reg_report,
        "coverage": {
            "valid_uv_texels": int(valid_m.sum()),
            "hit_before": int(old_hit.sum()),
            "hit_after": int(merged_hit.sum()),
            "newly_filled_texels": int(fill_mask.sum()),
            "quality_upgraded_texels": int(upgrade_mask.sum()),
            "hit_ratio_before": round(float(old_hit[valid_m].mean()), 4),
            "hit_ratio_after": round(float(merged_hit[valid_m].mean()), 4),
            "outer_hit_ratio_before": defect_before["overall_outer_hit_ratio"],
            "outer_hit_ratio_after": defect_after["overall_outer_hit_ratio"],
            "locked_existing_max_drift": round(locked_drift, 6),
        },
        "defects_before": defect_before,
        "defects_after": defect_after,
        "fill": fill_stats,
        "reprojection_comparison": reproj_cmp,
        "glb_size_mb": round(out_glb.stat().st_size / 1048576.0, 3),
        "outputs": {
            "glb": str(out_glb),
            "albedo": str(albedo_p),
            "normal": str(normal_p),
            "before_after_4views": str(output_dir / "before_after_4views.png"),
            "patch_diff_map": str(output_dir / "patch_diff_map.png"),
            "defect_report_after": str(output_dir / "defect_report_after.json"),
        },
    }
    with open(output_dir / "refine_report.json", "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    return report


def main() -> int:
    parser = ArgumentParser(description="Incremental texture refinement using targeted reshoot capture")
    parser.add_argument("--state-dir", type=Path, required=True, help="Directory containing transfer_state.npz")
    parser.add_argument("--output-dir", type=Path, required=True, help="Output directory for refined model")
    parser.add_argument("--reshoot-images-dir", type=Path, default=None)
    parser.add_argument("--reshoot-video", type=Path, default=None)
    parser.add_argument("--reshoot-masks-dir", type=Path, default=None)
    parser.add_argument("--reshoot-poses", type=Path, default=None, help="Optional explicit camera poses JSON")
    parser.add_argument("--ref-scan-dir", type=Path, default=None, help="Reference scan dir for LightGlue+PnP registration")
    parser.add_argument("--no-auto-segformer", action="store_true")
    parser.add_argument("--max-reshoot-frames", type=int, default=12)
    parser.add_argument("--facing-min", type=float, default=0.20)
    parser.add_argument("--upgrade-ratio", type=float, default=1.15)
    parser.add_argument("--no-exposure-eq", action="store_true")
    parser.add_argument("--eval-scan-dir", type=Path, default=None)
    parser.add_argument("--glb-name", type=str, default="garment-refined.glb")
    args = parser.parse_args()

    report = refine_texture_with_reshoot(
        state_dir=args.state_dir,
        output_dir=args.output_dir,
        reshoot_images_dir=args.reshoot_images_dir,
        reshoot_video=args.reshoot_video,
        reshoot_masks_dir=args.reshoot_masks_dir,
        reshoot_poses_path=args.reshoot_poses,
        ref_scan_dir=args.ref_scan_dir,
        auto_segformer=not args.no_auto_segformer,
        max_reshoot_frames=args.max_reshoot_frames,
        facing_min=args.facing_min,
        upgrade_ratio=args.upgrade_ratio,
        exposure_equalize=not args.no_exposure_eq,
        eval_scan_dir=args.eval_scan_dir,
        glb_name=args.glb_name,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
