"""Scan-to-Template Multi-View Visibility-Weighted Texture Transfer Pipeline.

Transfers real garment appearance captured by multi-view SfM / 2DGS onto a clean,
deliverable template garment GLB (such as top-001_v2 / top-003) without altering
template geometry, and verifies Filament compatibility via export_glb_guard.
"""
from __future__ import annotations

import json
import math
import sys
from argparse import ArgumentParser
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import open3d as o3d
import trimesh
import xatlas
from PIL import Image, ImageDraw, ImageFont
from scipy.ndimage import distance_transform_edt
from scipy.spatial import cKDTree
from trimesh.visual.material import PBRMaterial

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVER_TOOLS_DIR = REPO_ROOT / "server" / "tools"
if str(SERVER_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_TOOLS_DIR))
from export_glb_guard import find_extensions, read_glb, strip_file  # noqa: E402

GARMENT_CLASS_UPPER = (4, 7)
BAKE_BODY_BAN_CLASSES = (6, 9, 10, 11, 12, 13, 14, 15)


def _load_font(size: int):
    for p in (r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\simhei.ttf", r"C:\Windows\Fonts\arial.ttf"):
        if Path(p).exists():
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                pass
    return ImageFont.load_default()


def load_template_mesh(path: Path) -> tuple[trimesh.Trimesh, Image.Image | None]:
    """Load template GLB/OBJ/PLY as a single merged Trimesh and optional base texture."""
    loaded = trimesh.load(str(path), process=False)
    base_tex: Image.Image | None = None

    if isinstance(loaded, trimesh.Scene):
        geoms = loaded.dump(concatenate=False)
        meshes = [g for g in geoms if isinstance(g, trimesh.Trimesh) and len(g.faces) > 0]
        if not meshes:
            raise ValueError(f"模板场景中没有有效网格: {path}")
        for m in meshes:
            mat = getattr(m.visual, "material", None)
            tex = getattr(mat, "baseColorTexture", None) if mat is not None else None
            if tex is not None and base_tex is None:
                base_tex = tex.convert("RGB")
        # Bake existing per-part colors into vertex colors for before/after comparison
        baked_meshes: list[trimesh.Trimesh] = []
        for m in meshes:
            mc = m.copy()
            vcols = _sample_mesh_base_colors(mc)
            mc.visual = trimesh.visual.ColorVisuals(mesh=mc, vertex_colors=vcols)
            baked_meshes.append(mc)
        merged = trimesh.util.concatenate(baked_meshes)
    elif isinstance(loaded, trimesh.Trimesh):
        merged = loaded.copy()
        mat = getattr(merged.visual, "material", None)
        tex = getattr(mat, "baseColorTexture", None) if mat is not None else None
        if tex is not None:
            base_tex = tex.convert("RGB")
        vcols = _sample_mesh_base_colors(merged)
        merged.visual = trimesh.visual.ColorVisuals(mesh=merged, vertex_colors=vcols)
    else:
        raise ValueError(f"不支持的模板类型: {type(loaded).__name__}")

    merged.fix_normals()
    return merged, base_tex


def _sample_mesh_base_colors(mesh: trimesh.Trimesh) -> np.ndarray:
    """Extract per-vertex RGBA uint8 colors from material texture, color factor, or vertex colors."""
    n = len(mesh.vertices)
    if hasattr(mesh.visual, "vertex_colors") and mesh.visual.vertex_colors is not None:
        vc = np.asarray(mesh.visual.vertex_colors)
        if len(vc) == n and vc.shape[1] >= 3:
            if vc.shape[1] == 3:
                return np.column_stack([vc[:, :3], np.full(n, 255, dtype=np.uint8)])
            return vc[:, :4].astype(np.uint8)

    mat = getattr(mesh.visual, "material", None)
    tex = getattr(mat, "baseColorTexture", None) if mat is not None else None
    uv = getattr(mesh.visual, "uv", None)
    if tex is not None and uv is not None and len(uv) == n:
        arr = np.asarray(tex.convert("RGB"), dtype=np.uint8)
        h, w = arr.shape[:2]
        u = np.clip(np.mod(uv[:, 0], 1.0), 0.0, 1.0)
        v = np.clip(1.0 - np.mod(uv[:, 1], 1.0), 0.0, 1.0)
        ix = np.clip(np.rint(u * (w - 1)).astype(np.int32), 0, w - 1)
        iy = np.clip(np.rint(v * (h - 1)).astype(np.int32), 0, h - 1)
        rgb = arr[iy, ix]
        return np.column_stack([rgb, np.full(n, 255, dtype=np.uint8)])

    factor = getattr(mat, "baseColorFactor", None) if mat is not None else None
    if factor is not None and len(factor) >= 3:
        rgba = np.clip(np.rint(np.asarray(factor[:4], dtype=np.float32) * 255.0), 0, 255).astype(np.uint8)
        return np.tile(rgba, (n, 1))

    return np.tile(np.array([210, 210, 210, 255], dtype=np.uint8), (n, 1))


def prepare_template_uv(
    mesh: trimesh.Trimesh,
    uv_mode: str = "xatlas",
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Return (V_uv, F_uv, N_uv, VC_uv, uvs) for single-atlas baking."""
    V0 = np.asarray(mesh.vertices, dtype=np.float32)
    F0 = np.asarray(mesh.faces, dtype=np.uint32)
    mesh.compute_vertex_normals() if hasattr(mesh, "compute_vertex_normals") else None
    N0 = np.asarray(mesh.vertex_normals, dtype=np.float32)
    VC0 = np.asarray(mesh.visual.vertex_colors[:, :3], dtype=np.float32) / 255.0

    if uv_mode == "keep" and hasattr(mesh.visual, "uv") and mesh.visual.uv is not None and len(mesh.visual.uv) == len(V0):
        uvs = np.asarray(mesh.visual.uv, dtype=np.float32)
        return V0, F0.astype(np.int64), N0, VC0, uvs

    vmapping, indices, uvs = xatlas.parametrize(V0, F0, N0)
    V_uv = V0[vmapping]
    N_uv = N0[vmapping]
    VC_uv = VC0[vmapping]
    F_uv = indices.astype(np.int64)
    return V_uv, F_uv, N_uv, VC_uv, uvs.astype(np.float32)


def load_scan_views(scan_dir: Path, max_views: int = 60) -> tuple[list[dict[str, Any]], np.ndarray]:
    """Load camera poses, intrinsics, RGB images, and garment masks from a V9 scan directory."""
    cams_path = scan_dir / "2dgs_out" / "cameras.json"
    refined_path = scan_dir / "2dgs_out" / "refined_poses.json"
    data_dir = scan_dir / "2dgs_data_garment"
    if not cams_path.exists():
        raise FileNotFoundError(f"未找到扫描相机文件: {cams_path}")

    with open(cams_path, encoding="utf-8") as fh:
        cams_raw = json.load(fh)
    if refined_path.exists():
        with open(refined_path, encoding="utf-8") as fh:
            refined = json.load(fh)
    else:
        refined = {}

    all_views: list[dict[str, Any]] = []
    ups: list[np.ndarray] = []
    for c in cams_raw:
        stem = c["img_name"]
        img_path = data_dir / "images" / f"{stem}.jpg"
        mask_path = data_dir / "masks" / f"{stem}.png"
        if not img_path.exists() or not mask_path.exists():
            continue

        if stem in refined:
            R = np.array(refined[stem]["R_refined"], dtype=np.float64)
            t = np.array(refined[stem]["t_refined"], dtype=np.float64)
        else:
            C_pos = np.array(c["position"], dtype=np.float64)
            R_c2w = np.array(c["rotation"], dtype=np.float64)
            R = R_c2w.T
            t = -R @ C_pos

        W = int(c["width"])
        H = int(c["height"])
        fx = float(c["fx"])
        fy = float(c["fy"])
        cx = float(c.get("cx", W / 2.0))
        cy = float(c.get("cy", H / 2.0))
        K = np.array([[fx, 0.0, cx], [0.0, fy, cy], [0.0, 0.0, 1.0]], dtype=np.float64)
        C = -R.T @ t
        ups.append(-R[1, :])

        all_views.append(
            {
                "stem": stem,
                "img_path": img_path,
                "mask_path": mask_path,
                "sem_path": scan_dir / "priors" / "sem" / f"{stem}.png",
                "W": W,
                "H": H,
                "K": K,
                "R": R,
                "t": t,
                "C": C,
            }
        )

    if not all_views:
        raise RuntimeError(f"扫描目录中没有可用图像视角: {scan_dir}")

    up_scan = np.mean(np.stack(ups, axis=0), axis=0)
    up_scan /= max(float(np.linalg.norm(up_scan)), 1e-8)

    if len(all_views) > max_views:
        idxs = np.linspace(0, len(all_views) - 1, num=max_views, dtype=int)
        views = [all_views[int(i)] for i in idxs]
    else:
        views = all_views
    return views, up_scan


def load_scan_reference_geometry(scan_dir: Path, scan_mesh_override: Path | None = None) -> tuple[np.ndarray, np.ndarray | None, np.ndarray | None]:
    """Load scan target points (and optional normals/colors) from garment-clean.ply or sparse points3D."""
    mesh_p = scan_mesh_override or (scan_dir / "mesh" / "garment-clean.ply")
    if not mesh_p.exists():
        mesh_p = scan_dir / "final" / "garment-textured.glb"
    if mesh_p.exists():
        m = trimesh.load(str(mesh_p), force="mesh", process=False)
        pts = np.asarray(m.vertices, dtype=np.float64)
        m.compute_vertex_normals() if hasattr(m, "compute_vertex_normals") else None
        norms = np.asarray(m.vertex_normals, dtype=np.float64)
        cols = None
        if hasattr(m.visual, "vertex_colors") and m.visual.vertex_colors is not None and len(m.visual.vertex_colors) == len(pts):
            cols = np.asarray(m.visual.vertex_colors[:, :3], dtype=np.float32) / 255.0
        return pts, norms, cols

    ply_p = scan_dir / "2dgs_data_garment" / "sparse" / "0" / "points3D.ply"
    if ply_p.exists():
        pcd = o3d.io.read_point_cloud(str(ply_p))
        return np.asarray(pcd.points, dtype=np.float64), None, None
    raise FileNotFoundError(f"找不到扫描点云或网格用于对齐: {scan_dir}")


def _rotation_between_vectors(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    a_u = a / max(float(np.linalg.norm(a)), 1e-12)
    b_u = b / max(float(np.linalg.norm(b)), 1e-12)
    v = np.cross(a_u, b_u)
    c = float(np.dot(a_u, b_u))
    if np.linalg.norm(v) < 1e-10:
        if c > 0:
            return np.eye(3, dtype=np.float64)
        ortho = np.array([1.0, 0.0, 0.0]) if abs(a_u[0]) < 0.9 else np.array([0.0, 1.0, 0.0])
        axis = np.cross(a_u, ortho)
        axis /= np.linalg.norm(axis)
        return -np.eye(3, dtype=np.float64) + 2.0 * np.outer(axis, axis)
    s = float(np.linalg.norm(v))
    vx = np.array([[0.0, -v[2], v[1]], [v[2], 0.0, -v[0]], [-v[1], v[0], 0.0]], dtype=np.float64)
    return np.eye(3, dtype=np.float64) + vx + (vx @ vx) * ((1.0 - c) / (s * s))


def _rot_y(deg: float) -> np.ndarray:
    rad = math.radians(deg)
    c, s = math.cos(rad), math.sin(rad)
    return np.array([[c, 0.0, s], [0.0, 1.0, 0.0], [-s, 0.0, c]], dtype=np.float64)


def _compute_silhouette_iou(
    verts_scan: np.ndarray,
    faces: np.ndarray,
    views: list[dict[str, Any]],
    sample_indices: list[int],
    ds: int = 4,
) -> float:
    """Compute average 2D mask IoU across selected camera views by fast triangle-centroid + vertex splatting."""
    tri_centers = verts_scan[faces].mean(axis=1)
    sample_pts = np.vstack([verts_scan, tri_centers])
    ious: list[float] = []
    k_close = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7))

    for vi in sample_indices:
        vw = views[vi]
        W, H = vw["W"], vw["H"]
        ws, hs = max(32, W // ds), max(32, H // ds)
        K = vw["K"]
        R, t = vw["R"], vw["t"]
        Xc = sample_pts @ R.T + t
        ok = Xc[:, 2] > 0.2
        if not ok.any():
            ious.append(0.0)
            continue
        u = (K[0, 0] * Xc[ok, 0] / Xc[ok, 2] + K[0, 2]) / ds
        v = (K[1, 1] * Xc[ok, 1] / Xc[ok, 2] + K[1, 2]) / ds
        inb = (u >= 0) & (u < ws) & (v >= 0) & (v < hs)
        pred = np.zeros((hs, ws), dtype=np.uint8)
        if inb.any():
            ui = np.clip(np.rint(u[inb]).astype(np.int32), 0, ws - 1)
            vi_px = np.clip(np.rint(v[inb]).astype(np.int32), 0, hs - 1)
            pred[vi_px, ui] = 255
            pred = cv2.morphologyEx(pred, cv2.MORPH_CLOSE, k_close)
        gt = cv2.resize(
            np.asarray(Image.open(vw["mask_path"]).convert("L")),
            (ws, hs),
            interpolation=cv2.INTER_NEAREST,
        ) > 127
        pb = pred > 127
        inter = int((pb & gt).sum())
        union = int((pb | gt).sum())
        ious.append(inter / max(1, union))
    return float(np.mean(ious)) if ious else 0.0


def _smoothstep(edge0: float, edge1: float, x: np.ndarray) -> np.ndarray:
    t = np.clip((x - edge0) / max(edge1 - edge0, 1e-8), 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def _compute_torso_half_width(V: np.ndarray) -> tuple[float, float, bool]:
    """Estimate template X symmetry center, torso half-width, and whether extended sleeves exist."""
    x_mid = 0.5 * float(V[:, 0].min() + V[:, 0].max())
    y_min, y_max = float(V[:, 1].min()), float(V[:, 1].max())
    h = max(y_max - y_min, 1e-4)
    torso_band = (V[:, 1] >= y_min + 0.15 * h) & (V[:, 1] <= y_min + 0.65 * h)
    if int(torso_band.sum()) < 4:
        torso_half_x = float(np.percentile(np.abs(V[:, 0] - x_mid), 80))
    else:
        torso_half_x = float(np.percentile(np.abs(V[torso_band, 0] - x_mid), 95))
    max_abs_x = float(np.max(np.abs(V[:, 0] - x_mid)))
    has_sleeves = max_abs_x > torso_half_x * 1.25
    return x_mid, torso_half_x, has_sleeves


def deform_sleeves_soft_lbs(
    V: np.ndarray,
    F: np.ndarray,
    drop_deg: float,
) -> tuple[np.ndarray, np.ndarray, dict[str, Any]]:
    """Temporarily lower T-pose template sleeves via smooth LBS to match arm-down captures."""
    V_in = np.asarray(V, dtype=np.float64)
    F_in = np.asarray(F, dtype=np.int64)
    x_mid, torso_half_x, has_sleeves = _compute_torso_half_width(V_in)

    if abs(drop_deg) < 1e-3 or not has_sleeves:
        m = trimesh.Trimesh(vertices=V_in, faces=F_in, process=False)
        N_out = np.asarray(m.vertex_normals, dtype=np.float64)
        return V_in.copy(), N_out, {"applied": False, "drop_deg": 0.0, "has_sleeves": has_sleeves}

    abs_dx = np.abs(V_in[:, 0] - x_mid)
    sleeve_mask = abs_dx > torso_half_x * 1.15
    y_sl_lo = float(V_in[sleeve_mask, 1].min())
    y_sl_hi = float(V_in[sleeve_mask, 1].max())
    z_sl = float(V_in[sleeve_mask, 2].mean())

    pivot_offset_x = torso_half_x * 0.96
    pivot_y = y_sl_lo + 0.55 * max(y_sl_hi - y_sl_lo, 1e-4)
    pivot_z = z_sl

    wx = _smoothstep(torso_half_x * 0.88, torso_half_x * 1.22, abs_dx)
    wy = _smoothstep(y_sl_lo - 0.06, y_sl_lo + 0.02, V_in[:, 1])
    w = wx * wy

    V_out = V_in.copy()
    right = V_in[:, 0] >= x_mid
    left = ~right

    for side_mask, sign in ((right, -1.0), (left, 1.0)):
        if not side_mask.any():
            continue
        px = x_mid + (-sign) * pivot_offset_x
        dx = V_in[side_mask, 0] - px
        dy = V_in[side_mask, 1] - pivot_y
        theta = np.deg2rad(sign * drop_deg * w[side_mask])
        ct = np.cos(theta)
        st = np.sin(theta)
        V_out[side_mask, 0] = px + dx * ct - dy * st
        V_out[side_mask, 1] = pivot_y + dx * st + dy * ct

    m_posed = trimesh.Trimesh(vertices=V_out, faces=F_in, process=False)
    N_out = np.asarray(m_posed.vertex_normals, dtype=np.float64)
    return V_out, N_out, {
        "applied": True,
        "drop_deg": float(drop_deg),
        "has_sleeves": True,
        "torso_half_x": round(torso_half_x, 4),
        "sleeve_verts": int((w > 0.05).sum()),
    }


def align_template_to_scan(
    tmpl_mesh: trimesh.Trimesh,
    scan_pts: np.ndarray,
    scan_norms: np.ndarray | None,
    views: list[dict[str, Any]],
    up_scan: np.ndarray,
    front_cam_index: int = 0,
    allow_aniso_scale: bool = True,
    sleeve_drop_mode: str = "auto",
) -> tuple[np.ndarray, dict[str, Any]]:
    """Find 4x4 affine transform T_tmpl_to_scan and optimal temporary sleeve drop angle."""
    V_t = np.asarray(tmpl_mesh.vertices, dtype=np.float64)
    F_t = np.asarray(tmpl_mesh.faces, dtype=np.int64)
    x_mid, torso_half_x, has_sleeves = _compute_torso_half_width(V_t)

    # Canonical scan frame: Y_canonical = up_scan
    R_y_to_up = _rotation_between_vectors(np.array([0.0, 1.0, 0.0]), up_scan)
    scan_pts_upright = scan_pts @ R_y_to_up  # i.e. R_y_to_up.T @ p

    # Robust center & scale using quantiles along upright axes
    s_lo = np.percentile(scan_pts_upright, 2.0, axis=0)
    s_hi = np.percentile(scan_pts_upright, 98.0, axis=0)
    s_span = np.maximum(s_hi - s_lo, 1e-4)
    s_center_upright = 0.5 * (s_lo + s_hi)

    t_lo = np.percentile(V_t, 1.0, axis=0)
    t_hi = np.percentile(V_t, 99.0, axis=0)
    t_span = np.maximum(t_hi - t_lo, 1e-4)
    t_center = 0.5 * (t_lo + t_hi)

    # Height ratio gives a reliable initial isotropic scale
    iso_scale_init = float(s_span[1] / t_span[1])

    # Determine front camera direction in upright space for front/back disambiguation
    front_dir_upright: np.ndarray | None = None
    if 0 <= front_cam_index < len(views):
        scan_center_world = R_y_to_up @ s_center_upright
        cam_vec = views[front_cam_index]["C"] - scan_center_world
        cam_upright = R_y_to_up.T @ cam_vec
        cam_upright[1] = 0.0
        nrm = float(np.linalg.norm(cam_upright))
        if nrm > 1e-6:
            front_dir_upright = cam_upright / nrm

    pcd_scan = o3d.geometry.PointCloud()
    pcd_scan.points = o3d.utility.Vector3dVector(np.array(scan_pts, dtype=np.float64, copy=True))
    if scan_norms is not None and len(scan_norms) == len(scan_pts):
        pcd_scan.normals = o3d.utility.Vector3dVector(np.array(scan_norms, dtype=np.float64, copy=True))
    else:
        pcd_scan.estimate_normals(search_param=o3d.geometry.KDTreeSearchParamHybrid(radius=0.15 * s_span[1], max_nn=30))

    # Subsample template surface points uniformly; anchor ICP on torso core when T-pose sleeves exist
    tmpl_sample_pts, tmpl_face_idx = tmpl_mesh.sample(4500, return_index=True)
    tmpl_sample_norms = tmpl_mesh.face_normals[tmpl_face_idx]
    torso_only_icp = False
    if has_sleeves:
        torso_sel = np.abs(tmpl_sample_pts[:, 0] - x_mid) <= torso_half_x * 1.12
        if int(torso_sel.sum()) >= 1200:
            tmpl_sample_pts = tmpl_sample_pts[torso_sel]
            tmpl_sample_norms = tmpl_sample_norms[torso_sel]
            torso_only_icp = True

    # During yaw search, pre-drop sleeves slightly if auto mode so silhouette IoU matches arm-down captures
    init_drop_for_yaw = 50.0 if (has_sleeves and sleeve_drop_mode == "auto") else (
        0.0 if sleeve_drop_mode == "auto" else float(sleeve_drop_mode)
    )
    V_yaw_eval, _, _ = deform_sleeves_soft_lbs(V_t, F_t, init_drop_for_yaw)
    eval_views = np.linspace(0, len(views) - 1, num=min(8, len(views)), dtype=int).tolist()

    best_score = -1e9
    best_T = np.eye(4, dtype=np.float64)
    best_info: dict[str, Any] = {}

    candidate_yaws = [float(k * 45.0) for k in range(8)]
    if front_dir_upright is not None:
        prior_yaw = math.degrees(math.atan2(front_dir_upright[0], front_dir_upright[2]))
        candidate_yaws.append(prior_yaw)

    for yaw in candidate_yaws:
        Ry = _rot_y(yaw)
        T_init = np.eye(4, dtype=np.float64)
        R_init = R_y_to_up @ Ry
        T_init[:3, :3] = R_init * iso_scale_init
        T_init[:3, 3] = (R_y_to_up @ s_center_upright) - T_init[:3, :3] @ t_center

        pcd_src = o3d.geometry.PointCloud()
        pts_0 = (tmpl_sample_pts @ T_init[:3, :3].T) + T_init[:3, 3]
        nrm_0 = tmpl_sample_norms @ R_init.T
        pcd_src.points = o3d.utility.Vector3dVector(np.array(pts_0, dtype=np.float64, copy=True))
        pcd_src.normals = o3d.utility.Vector3dVector(np.array(nrm_0, dtype=np.float64, copy=True))

        reg1 = o3d.pipelines.registration.registration_icp(
            pcd_src,
            pcd_scan,
            max_correspondence_distance=0.22 * s_span[1],
            init=np.eye(4, dtype=np.float64),
            estimation_method=o3d.pipelines.registration.TransformationEstimationPointToPoint(with_scaling=True),
            criteria=o3d.pipelines.registration.ICPConvergenceCriteria(max_iteration=40),
        )
        T_mid = reg1.transformation @ T_init

        pts_1 = (tmpl_sample_pts @ T_mid[:3, :3].T) + T_mid[:3, 3]
        pcd_src.points = o3d.utility.Vector3dVector(np.array(pts_1, dtype=np.float64, copy=True))
        reg2 = o3d.pipelines.registration.registration_icp(
            pcd_src,
            pcd_scan,
            max_correspondence_distance=0.09 * s_span[1],
            init=np.eye(4, dtype=np.float64),
            estimation_method=o3d.pipelines.registration.TransformationEstimationPointToPoint(with_scaling=True),
            criteria=o3d.pipelines.registration.ICPConvergenceCriteria(max_iteration=40),
        )
        T_cand = reg2.transformation @ T_mid

        V_cand = (V_yaw_eval @ T_cand[:3, :3].T) + T_cand[:3, 3]
        iou = _compute_silhouette_iou(V_cand, F_t, views, eval_views, ds=4)

        front_bonus = 0.0
        if front_dir_upright is not None:
            tmpl_front_world = T_cand[:3, :3] @ np.array([0.0, 0.0, 1.0])
            tmpl_front_upright = R_y_to_up.T @ tmpl_front_world
            tmpl_front_upright[1] = 0.0
            fn = float(np.linalg.norm(tmpl_front_upright))
            if fn > 1e-6:
                cos_f = float(np.dot(tmpl_front_upright / fn, front_dir_upright))
                front_bonus = 0.12 * cos_f

        d_t2s = np.asarray(pcd_src.compute_point_cloud_distance(pcd_scan))
        chamfer_med = float(np.median(d_t2s)) / max(s_span[1], 1e-4)
        score = iou + front_bonus - 0.35 * chamfer_med

        if score > best_score:
            best_score = score
            best_T = T_cand
            best_info = {
                "chosen_yaw_deg": round(yaw % 360.0, 2),
                "silhouette_iou": round(iou, 4),
                "chamfer_norm_median": round(chamfer_med, 4),
                "front_bonus": round(front_bonus, 4),
                "torso_only_icp": torso_only_icp,
            }

    # Select optimal sleeve drop angle against multi-view masks
    if not has_sleeves:
        best_drop_deg = 0.0
    elif sleeve_drop_mode == "auto":
        best_drop_deg = init_drop_for_yaw
        best_drop_iou = -1.0
        for deg in (0.0, 25.0, 40.0, 52.0, 62.0, 72.0, 80.0):
            V_d, _, _ = deform_sleeves_soft_lbs(V_t, F_t, deg)
            iou_d = _compute_silhouette_iou((V_d @ best_T[:3, :3].T) + best_T[:3, 3], F_t, views, eval_views, ds=4)
            if iou_d > best_drop_iou:
                best_drop_iou = iou_d
                best_drop_deg = deg
        best_info["silhouette_iou"] = round(best_drop_iou, 4)
    else:
        best_drop_deg = float(sleeve_drop_mode)
        V_d, _, _ = deform_sleeves_soft_lbs(V_t, F_t, best_drop_deg)
        best_info["silhouette_iou"] = round(
            _compute_silhouette_iou((V_d @ best_T[:3, :3].T) + best_T[:3, 3], F_t, views, eval_views, ds=4), 4
        )
    best_info["sleeve_drop_deg"] = float(best_drop_deg)

    # Full posed mesh fine ICP refinement (accept only if silhouette IoU does not degrade)
    V_posed, _, _ = deform_sleeves_soft_lbs(V_t, F_t, best_drop_deg)
    if has_sleeves:
        posed_mesh = trimesh.Trimesh(vertices=V_posed, faces=F_t, process=False)
        posed_pts, _ = posed_mesh.sample(4000, return_index=True)
        pcd_posed = o3d.geometry.PointCloud()
        pcd_posed.points = o3d.utility.Vector3dVector(np.array((posed_pts @ best_T[:3, :3].T) + best_T[:3, 3], dtype=np.float64, copy=True))
        reg_fine = o3d.pipelines.registration.registration_icp(
            pcd_posed,
            pcd_scan,
            max_correspondence_distance=0.07 * s_span[1],
            init=np.eye(4, dtype=np.float64),
            estimation_method=o3d.pipelines.registration.TransformationEstimationPointToPoint(with_scaling=True),
            criteria=o3d.pipelines.registration.ICPConvergenceCriteria(max_iteration=40),
        )
        T_fine = reg_fine.transformation @ best_T
        iou_fine = _compute_silhouette_iou((V_posed @ T_fine[:3, :3].T) + T_fine[:3, 3], F_t, views, eval_views, ds=4)
        if iou_fine >= best_info["silhouette_iou"] - 0.002:
            best_T = T_fine
            best_info["silhouette_iou"] = round(iou_fine, 4)

    # Optional mild anisotropic refinement
    if allow_aniso_scale:
        T_inv = np.linalg.inv(best_T)
        scan_in_tmpl = (scan_pts @ T_inv[:3, :3].T) + T_inv[:3, 3]
        p_lo = np.percentile(V_posed, 1.0, axis=0)
        p_hi = np.percentile(V_posed, 99.0, axis=0)
        p_span = np.maximum(p_hi - p_lo, 1e-4)
        p_center = 0.5 * (p_lo + p_hi)
        margin = 0.25 * p_span
        inside = np.all((scan_in_tmpl >= p_lo - margin) & (scan_in_tmpl <= p_hi + margin), axis=1)
        if int(inside.sum()) >= 200:
            s_in_lo = np.percentile(scan_in_tmpl[inside], 3.0, axis=0)
            s_in_hi = np.percentile(scan_in_tmpl[inside], 97.0, axis=0)
            ratio = np.clip((s_in_hi - s_in_lo) / np.maximum(p_hi - p_lo, 1e-4), 0.85, 1.18)
            s_mid = 0.5 * (s_in_lo + s_in_hi)
            A = np.eye(4, dtype=np.float64)
            A[:3, :3] = np.diag(ratio)
            A[:3, 3] = s_mid - ratio * p_center
            T_aniso = best_T @ A
            iou_before = best_info["silhouette_iou"]
            iou_after = _compute_silhouette_iou((V_posed @ T_aniso[:3, :3].T) + T_aniso[:3, 3], F_t, views, eval_views, ds=4)
            if iou_after >= iou_before - 0.005:
                best_T = T_aniso
                best_info["silhouette_iou"] = round(iou_after, 4)
                best_info["aniso_ratio"] = [round(float(x), 4) for x in ratio]
            else:
                best_info["aniso_ratio"] = [1.0, 1.0, 1.0]

    V_aligned = (V_posed @ best_T[:3, :3].T) + best_T[:3, 3]
    dists_v, _ = cKDTree(scan_pts).query(V_aligned, k=1)
    best_info["vertex_to_scan_median_m"] = round(float(np.median(dists_v)), 4)
    best_info["vertex_to_scan_p90_m"] = round(float(np.percentile(dists_v, 90)), 4)
    return best_T, best_info


def transform_points_and_normals(
    pts: np.ndarray,
    norms: np.ndarray,
    T: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """Transform 3D positions and unit surface normals by a 4x4 affine matrix T."""
    M = T[:3, :3]
    P_out = (pts @ M.T) + T[:3, 3]
    inv_M_T = np.linalg.inv(M).T
    N_out = norms @ inv_M_T.T
    N_out /= np.maximum(np.linalg.norm(N_out, axis=1, keepdims=True), 1e-12)
    return P_out, N_out


def rasterize_template_uv_atlas(
    V_uv: np.ndarray,
    N_uv: np.ndarray,
    VC_uv: np.ndarray,
    F_uv: np.ndarray,
    uvs: np.ndarray,
    tex_size: int,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Rasterize template triangles in UV space to obtain 3D position/normal/baseColor texel buffers."""
    pos_m = np.zeros((tex_size, tex_size, 3), dtype=np.float32)
    norm_m = np.zeros((tex_size, tex_size, 3), dtype=np.float32)
    vcol_m = np.zeros((tex_size, tex_size, 3), dtype=np.float32)
    valid_m = np.zeros((tex_size, tex_size), dtype=bool)

    pxa = np.mod(uvs[:, 0], 1.0) * (tex_size - 1)
    pya = (1.0 - np.mod(uvs[:, 1], 1.0)) * (tex_size - 1)

    for tri in F_uv:
        i0, i1, i2 = int(tri[0]), int(tri[1]), int(tri[2])
        ax, ay = float(pxa[i0]), float(pya[i0])
        bx, by = float(pxa[i1]), float(pya[i1])
        cx_, cy_ = float(pxa[i2]), float(pya[i2])
        x0 = max(0, int(math.floor(min(ax, bx, cx_))))
        x1 = min(tex_size - 1, int(math.ceil(max(ax, bx, cx_))))
        y0 = max(0, int(math.floor(min(ay, by, cy_))))
        y1 = min(tex_size - 1, int(math.ceil(max(ay, by, cy_))))
        d = (by - cy_) * (ax - cx_) + (cx_ - bx) * (ay - cy_)
        if x1 < x0 or y1 < y0 or abs(d) < 1e-8:
            continue
        gx, gy = np.meshgrid(np.arange(x0, x1 + 1, dtype=np.float32), np.arange(y0, y1 + 1, dtype=np.float32))
        l1 = ((by - cy_) * (gx - cx_) + (cx_ - bx) * (gy - cy_)) / d
        l2 = ((cy_ - ay) * (gx - cx_) + (ax - cx_) * (gy - cy_)) / d
        ins = (l1 >= -1e-4) & (l2 >= -1e-4) & ((1.0 - l1 - l2) >= -1e-4)
        if not ins.any():
            continue
        b1 = l1[ins][:, None]
        b2 = l2[ins][:, None]
        b3 = (1.0 - l1 - l2)[ins][:, None]
        yy, xx = np.nonzero(ins)
        ty, tx = y0 + yy, x0 + xx
        pos_m[ty, tx] = b1 * V_uv[i0] + b2 * V_uv[i1] + b3 * V_uv[i2]
        nn = b1 * N_uv[i0] + b2 * N_uv[i1] + b3 * N_uv[i2]
        nn /= np.maximum(np.linalg.norm(nn, axis=1, keepdims=True), 1e-8)
        norm_m[ty, tx] = nn
        vcol_m[ty, tx] = np.clip(b1 * VC_uv[i0] + b2 * VC_uv[i1] + b3 * VC_uv[i2], 0.0, 1.0)
        valid_m[ty, tx] = True

    return pos_m, norm_m, vcol_m, valid_m


def _bilerp_rgb(arr: np.ndarray, u: np.ndarray, v: np.ndarray) -> np.ndarray:
    H, W = arr.shape[:2]
    x0 = np.clip(np.floor(u).astype(np.int32), 0, W - 1)
    y0 = np.clip(np.floor(v).astype(np.int32), 0, H - 1)
    x1 = np.clip(x0 + 1, 0, W - 1)
    y1 = np.clip(y0 + 1, 0, H - 1)
    wx = (u - x0)[:, None].astype(np.float32)
    wy = (v - y0)[:, None].astype(np.float32)
    c00 = arr[y0, x0]
    c10 = arr[y0, x1]
    c01 = arr[y1, x0]
    c11 = arr[y1, x1]
    return ((1.0 - wy) * ((1.0 - wx) * c00 + wx * c10) + wy * ((1.0 - wx) * c01 + wx * c11)).astype(np.float32)


def _load_clean_bake_mask(vw: dict[str, Any], erode_px: int = 7) -> tuple[np.ndarray, np.ndarray]:
    """Return (clean_garment_mask, spatial_reliability_weight in [0,1])."""
    m = np.asarray(Image.open(vw["mask_path"]).convert("L")) > 127
    if erode_px >= 3:
        k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (erode_px, erode_px))
        m = cv2.erode(m.astype(np.uint8) * 255, k) > 127

    rel_weight = np.ones(m.shape, dtype=np.float32)
    # Down-weight pixels very close to the outer garment boundary to avoid background bleed
    dist_fg = cv2.distanceTransform(m.astype(np.uint8), cv2.DIST_L2, 3)
    rel_weight *= np.clip(dist_fg / 8.0, 0.15, 1.0)

    sem_path = vw.get("sem_path")
    if sem_path is not None and sem_path.exists():
        sem = np.asarray(Image.open(sem_path), dtype=np.uint8)
        H, W = m.shape
        if sem.shape != (H, W):
            sem = cv2.resize(sem, (W, H), interpolation=cv2.INTER_NEAREST)
        k_ban = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (25, 25))
        ban_raw = (np.isin(sem, BAKE_BODY_BAN_CLASSES) & (~np.isin(sem, GARMENT_CLASS_UPPER))).astype(np.uint8) * 255
        ban = cv2.dilate(ban_raw, k_ban) > 127
        m = m & (~ban)
        dist_ban = cv2.distanceTransform((~ban).astype(np.uint8), cv2.DIST_L2, 3)
        rel_weight *= np.clip(dist_ban / 20.0, 0.05, 1.0)
    return m, rel_weight


def bake_multiview_to_template_uv(
    V_uv: np.ndarray,
    F_uv: np.ndarray,
    pos_m: np.ndarray,
    norm_m: np.ndarray,
    vcol_m: np.ndarray,
    valid_m: np.ndarray,
    T_tmpl_to_scan: np.ndarray,
    views: list[dict[str, Any]],
    facing_min: float = 0.20,
    exposure_equalize: bool = True,
    return_aux: bool = False,
) -> Any:
    """Back-project multi-view RGB images onto template UV texels with ray occlusion & two-pass blending."""
    vy, vx = np.nonzero(valid_m)
    P_t = pos_m[vy, vx].astype(np.float64)
    N_t = norm_m[vy, vx].astype(np.float64)
    P_s, N_s = transform_points_and_normals(P_t, N_t, T_tmpl_to_scan)

    V_s = (V_uv.astype(np.float64) @ T_tmpl_to_scan[:3, :3].T) + T_tmpl_to_scan[:3, 3]
    ray_scene = o3d.t.geometry.RaycastingScene()
    ray_scene.add_triangles(
        o3d.core.Tensor(V_s.astype(np.float32), o3d.core.float32),
        o3d.core.Tensor(F_uv.astype(np.uint32), o3d.core.uint32),
    )

    # Compute scene scale for ray self-occlusion tolerance
    bbox_diag = float(np.linalg.norm(V_s.max(axis=0) - V_s.min(axis=0)))
    occ_tol = max(0.015, 0.025 * bbox_diag)

    # First pass over masks for cross-view Lab lighting normalization (anchored on well-lit views)
    masks: list[np.ndarray] = []
    rel_weights: list[np.ndarray] = []
    lums, a_vals, b_vals = [], [], []
    for vw in views:
        m, rw = _load_clean_bake_mask(vw, erode_px=7)
        masks.append(m)
        rel_weights.append(rw)
        if exposure_equalize and int(m.sum()) > 300:
            bgr = cv2.imread(str(vw["img_path"]), cv2.IMREAD_COLOR)
            lab = cv2.cvtColor(bgr, cv2.COLOR_BGR2LAB)
            lums.append(float(np.mean(lab[:, :, 0][m])))
            a_vals.append(float(np.mean(lab[:, :, 1][m])))
            b_vals.append(float(np.mean(lab[:, :, 2][m])))

    if lums:
        l_arr = np.asarray(lums, dtype=np.float64)
        top_idx = l_arr >= np.percentile(l_arr, 70)
        target_L = float(np.percentile(l_arr, 75))
        target_a = float(np.median(np.asarray(a_vals)[top_idx]))
        target_b = float(np.median(np.asarray(b_vals)[top_idx]))
    else:
        target_L, target_a, target_b = 120.0, 128.0, 128.0

    cam_obs: list[tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray, int]] = []
    sharp_vals: list[float] = []

    for vi, vw in enumerate(views):
        bgr = cv2.imread(str(vw["img_path"]), cv2.IMREAD_COLOR)
        H, W = bgr.shape[:2]
        m_g = masks[vi]
        rw_map = rel_weights[vi]
        if int(m_g.sum()) < 50:
            sharp_vals.append(1.0)
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
        sharp_vals.append(max(1.0, float(cv2.Laplacian(roi_gray, cv2.CV_32F).var())))
        si = len(sharp_vals) - 1

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
        cam_obs.append((idx, _bilerp_rgb(rgb, u[idx], v[idx]), fac[unocc], np.maximum(z[idx], 0.35), rw_obs, si))

    med_s = float(np.median(sharp_vals)) if sharp_vals else 1.0
    sw_list = [float(np.clip((s / max(med_s, 1e-6)) ** 0.75, 0.70, 1.35)) for s in sharp_vals]

    acc1 = np.zeros((len(P_s), 3), dtype=np.float64)
    w1 = np.zeros(len(P_s), dtype=np.float64)
    obs_count = np.zeros(len(P_s), dtype=np.int32)
    for idx, cols, fac, zs, rw_obs, si in cam_obs:
        w = ((fac ** 4.5) * sw_list[si] * rw_obs) / (zs ** 2.0)
        acc1[idx] += w[:, None] * cols
        w1[idx] += w
        obs_count[idx] += 1

    h1 = w1 > 1e-8
    C1 = np.zeros((len(P_s), 3), dtype=np.float32)
    C1[h1] = (acc1[h1] / w1[h1, None]).astype(np.float32)

    # Pass 2: outlier-rejected blending against C1
    acc2 = np.zeros((len(P_s), 3), dtype=np.float64)
    w2 = np.zeros(len(P_s), dtype=np.float64)
    for idx, cols, fac, zs, rw_obs, si in cam_obs:
        g = np.mean(np.abs(cols - C1[idx]), axis=1) <= 0.10
        if g.any():
            w = ((fac[g] ** 4.5) * sw_list[si] * rw_obs[g]) / (zs[g] ** 2.0)
            acc2[idx[g]] += w[:, None] * cols[g]
            w2[idx[g]] += w

    h2 = w2 > 1e-8
    hit_texels = h1 | h2
    C_out = vcol_m[vy, vx].copy()
    C_out[h1 & (~h2)] = C1[h1 & (~h2)]
    C_out[h2] = np.clip(acc2[h2] / w2[h2, None], 0.0, 1.0).astype(np.float32)

    tex_rgb = vcol_m.copy()
    tex_rgb[vy, vx] = C_out
    hit_mask = np.zeros(valid_m.shape, dtype=bool)
    hit_mask[vy[hit_texels], vx[hit_texels]] = True

    quality_vals = np.where(h2, w2, w1).astype(np.float32)
    quality_map = np.zeros(valid_m.shape, dtype=np.float32)
    quality_map[vy, vx] = quality_vals
    obs_count_map = np.zeros(valid_m.shape, dtype=np.int32)
    obs_count_map[vy, vx] = obs_count

    hit_ratio = float(hit_texels.mean()) if len(hit_texels) else 0.0
    med_views = float(np.median(obs_count[hit_texels])) if hit_texels.any() else 0.0
    stats = {
        "valid_uv_texels": int(valid_m.sum()),
        "hit_uv_texels": int(hit_texels.sum()),
        "hit_ratio": round(hit_ratio, 4),
        "median_views_per_hit_texel": round(med_views, 2),
        "views_used": len(views),
        "views_with_observations": len(cam_obs),
    }
    if return_aux:
        return tex_rgb, hit_mask, stats, {
            "quality_map": quality_map,
            "obs_count_map": obs_count_map,
            "lab_target": np.array([target_L, target_a, target_b], dtype=np.float32),
        }
    return tex_rgb, hit_mask, stats


def _find_plain_fabric_detail_tile(
    tex_rgb: np.ndarray,
    seed_mask: np.ndarray,
    median_rgb: np.ndarray,
) -> tuple[np.ndarray | None, float]:
    """Find a spotless plain fabric patch in UV space and build a 2x2 mirrored tileable micro-weave map."""
    H, W = tex_rgb.shape[:2]
    patch_sz = max(16, min(64, H // 8))
    stride = max(8, patch_sz // 2)
    if H < patch_sz or W < patch_sz:
        return None, 0.0

    best_score = -1e9
    best_patch: np.ndarray | None = None
    for y0 in range(0, H - patch_sz + 1, stride):
        for x0 in range(0, W - patch_sz + 1, stride):
            sm = seed_mask[y0:y0 + patch_sz, x0:x0 + patch_sz]
            if float(sm.mean()) < 0.995:
                continue
            patch = tex_rgb[y0:y0 + patch_sz, x0:x0 + patch_sz]
            patch_med = np.median(patch, axis=(0, 1))
            local_dev = np.max(np.abs(patch - patch_med[None, None, :]), axis=2)
            # Reject any patch containing spots, print fragments, or dark seam edges
            if float(np.max(local_dev)) > 0.07:
                continue
            color_dev = float(np.mean(np.abs(patch - median_rgb[None, None, :])))
            if color_dev > 0.08:
                continue
            gray = 0.299 * patch[:, :, 0] + 0.587 * patch[:, :, 1] + 0.114 * patch[:, :, 2]
            hf = gray - cv2.GaussianBlur(gray, (0, 0), sigmaX=4.0)
            hf_std = float(np.std(hf))
            if hf_std < 0.0015 or hf_std > 0.016:
                continue
            # Prefer clean, uniform fabric weave near target std ~0.007 rather than high-contrast outliers
            score = -color_dev - 3.0 * abs(hf_std - 0.007) - 0.5 * float(np.max(local_dev))
            if score > best_score:
                best_score = score
                best_patch = patch

    if best_patch is None:
        return None, 0.0

    low_freq = cv2.GaussianBlur(best_patch, (0, 0), sigmaX=4.5)
    hf_patch = np.clip(best_patch - low_freq, -0.035, 0.035)
    # 2x2 mirror-tiled patch eliminates seam discontinuity when tiled over occluded UV zones
    top = np.concatenate([hf_patch, hf_patch[:, ::-1, :]], axis=1)
    tile_2x2 = np.concatenate([top, top[::-1, :, :]], axis=0)
    th, tw = tile_2x2.shape[:2]
    reps_y = int(math.ceil(H / th))
    reps_x = int(math.ceil(W / tw))
    tiled = np.tile(tile_2x2, (reps_y, reps_x, 1))[:H, :W, :]
    return tiled.astype(np.float32), round(float(np.std(hf_patch)), 5)


def inpaint_and_dilate_uv(
    tex_rgb: np.ndarray,
    valid_m: np.ndarray,
    hit_mask: np.ndarray,
    pos_orig_m: np.ndarray | None = None,
    norm_orig_m: np.ndarray | None = None,
    gutter_passes: int = 12,
) -> tuple[np.ndarray, dict[str, Any]]:
    """Complete occluded UV texels via 3D left/right symmetry borrowing + clean-seed diffusion + micro-fabric detail."""
    miss_inside = valid_m & (~hit_mask)
    sym_borrowed = 0
    fabric_detail_std = 0.0
    working_rgb = tex_rgb.copy()
    working_hit = hit_mask.copy()

    if working_hit.any():
        eroded = cv2.erode(working_hit.astype(np.uint8) * 255, np.ones((5, 5), np.uint8)) > 127
        seed_mask = eroded if int(eroded.sum()) >= max(64, int(0.25 * int(working_hit.sum()))) else working_hit
        median_rgb = np.median(working_rgb[seed_mask], axis=0).astype(np.float32)

        # Step A: 3D left/right symmetry borrowing for plain cloth areas (never copy single-side prints)
        if pos_orig_m is not None and norm_orig_m is not None and miss_inside.any() and int(seed_mask.sum()) >= 64:
            sy, sx = np.nonzero(seed_mask)
            # Subsample seed KDTree if huge so 2048x2048 runs in <0.5s
            step = max(1, len(sy) // 220000)
            sy_s, sx_s = sy[::step], sx[::step]
            P_seed = pos_orig_m[sy_s, sx_s].astype(np.float64)
            N_seed = norm_orig_m[sy_s, sx_s].astype(np.float64)
            C_seed = working_rgb[sy_s, sx_s]

            x_mid = 0.5 * float(pos_orig_m[valid_m, 0].min() + pos_orig_m[valid_m, 0].max())
            my, mx = np.nonzero(miss_inside)
            P_miss = pos_orig_m[my, mx].astype(np.float64)
            N_miss = norm_orig_m[my, mx].astype(np.float64)
            off_center = np.abs(P_miss[:, 0] - x_mid) > 0.035

            if off_center.any():
                P_q = P_miss[off_center].copy()
                P_q[:, 0] = 2.0 * x_mid - P_q[:, 0]
                N_q = N_miss[off_center].copy()
                N_q[:, 0] = -N_q[:, 0]

                dists, nn_idx = cKDTree(P_seed).query(P_q, k=1)
                n_dot = np.sum(N_q * N_seed[nn_idx], axis=1)
                col_cand = C_seed[nn_idx]
                plain_ok = np.mean(np.abs(col_cand - median_rgb[None, :]), axis=1) <= 0.14
                accept = (dists <= 0.022) & (n_dot >= 0.72) & plain_ok
                if accept.any():
                    ay = my[off_center][accept]
                    ax = mx[off_center][accept]
                    working_rgb[ay, ax] = col_cand[accept]
                    working_hit[ay, ax] = True
                    sym_borrowed = int(accept.sum())

        # Recompute clean seed mask & remaining missed mask after symmetry borrowing
        miss_after_sym = valid_m & (~working_hit)
        eroded2 = cv2.erode(working_hit.astype(np.uint8) * 255, np.ones((5, 5), np.uint8)) > 127
        seed_mask2 = eroded2 if int(eroded2.sum()) >= max(64, int(0.25 * int(working_hit.sum()))) else working_hit
        median_rgb = np.median(working_rgb[seed_mask2], axis=0).astype(np.float32)

        dist_to_seed, nearest_idx = distance_transform_edt(~seed_mask2, return_indices=True)
        nn_seeded = working_rgb[nearest_idx[0], nearest_idx[1]].copy()
        nn_seeded[working_hit] = working_rgb[working_hit]

        if miss_after_sym.any():
            smooth_field = cv2.GaussianBlur(nn_seeded, (0, 0), sigmaX=12.0)
            alpha = np.exp(-dist_to_seed / 18.0).astype(np.float32)[..., None]
            blended_fill = alpha * smooth_field + (1.0 - alpha) * median_rgb[None, None, :]

            detail_tile, fabric_detail_std = _find_plain_fabric_detail_tile(working_rgb, seed_mask2, median_rgb)
            if detail_tile is not None:
                detail_w = (0.70 * (1.0 - np.exp(-dist_to_seed / 6.0))).astype(np.float32)[..., None]
                blended_fill = blended_fill + detail_w * detail_tile

            filled = working_rgb.copy()
            filled[miss_after_sym] = blended_fill[miss_after_sym]
            filled[~valid_m] = smooth_field[~valid_m]
        else:
            filled = nn_seeded
    else:
        filled = working_rgb.copy()

    # Dilate valid island out into empty UV gutters to prevent mipmap black seams
    h, w = valid_m.shape
    fld = valid_m.copy()
    for _ in range(gutter_passes):
        if fld.all():
            break
        acc = np.zeros_like(filled)
        cnt = np.zeros((h, w), dtype=np.float32)
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                if dy == 0 and dx == 0:
                    continue
                ys_src = slice(max(0, -dy), h - max(0, dy))
                xs_src = slice(max(0, -dx), w - max(0, dx))
                ys_dst = slice(max(0, dy), h - max(0, -dy))
                xs_dst = slice(max(0, dx), w - max(0, -dx))
                m = fld[ys_src, xs_src]
                acc[ys_dst, xs_dst][m] += filled[ys_src, xs_src][m]
                cnt[ys_dst, xs_dst][m] += 1.0
        grow = (~fld) & (cnt > 0)
        if not grow.any():
            break
        filled[grow] = acc[grow] / cnt[grow, None]
        fld |= grow

    valid_lum = (0.299 * filled[:, :, 0] + 0.587 * filled[:, :, 1] + 0.114 * filled[:, :, 2])[valid_m]
    black_ratio = float((valid_lum < (12.0 / 255.0)).mean()) if len(valid_lum) else 0.0
    return np.clip(filled, 0.0, 1.0), {
        "missed_inside_texels_inpainted": int(miss_inside.sum()),
        "symmetry_borrowed_texels": sym_borrowed,
        "fabric_detail_synthesized": fabric_detail_std > 0.0,
        "fabric_detail_std": fabric_detail_std,
        "black_texel_ratio_in_valid_uv": round(black_ratio, 6),
    }


def _rasterize_textured_view(
    V: np.ndarray,
    F: np.ndarray,
    uvs: np.ndarray,
    tex_rgb: np.ndarray,
    K: np.ndarray,
    R: np.ndarray,
    t: np.ndarray,
    W: int,
    H: int,
    bg_rgb: tuple[float, float, float] = (0.08, 0.09, 0.11),
    shaded: bool = True,
) -> tuple[np.ndarray, np.ndarray]:
    """Software perspective rasterizer returning (rgb float32 HxWx3, fg_mask bool HxW)."""
    vc = (V @ R.T) + t
    z = vc[:, 2]
    sz = np.where(z > 1e-4, z, 1e-4)
    u = K[0, 0] * vc[:, 0] / sz + K[0, 2]
    wp = K[1, 1] * vc[:, 1] / sz + K[1, 2]

    cam_pos = -R.T @ t
    v0_tri = V[F[:, 0]]
    fn = np.cross(V[F[:, 1]] - v0_tri, V[F[:, 2]] - v0_tri)
    fn /= np.maximum(np.linalg.norm(fn, axis=1, keepdims=True), 1e-12)
    ld = cam_pos[None, :] - V[F].mean(axis=1)
    ld /= np.maximum(np.linalg.norm(ld, axis=1, keepdims=True), 1e-12)
    fn *= np.where((fn * ld).sum(axis=1) >= 0, 1.0, -1.0)[:, None]
    shade = (0.76 + 0.24 * np.clip((fn * ld).sum(axis=1), 0.0, 1.0)) if shaded else np.ones(len(F), dtype=np.float32)

    rgb = np.full((H, W, 3), bg_rgb, dtype=np.float32)
    zb = np.full((H, W), np.inf, dtype=np.float64)
    fg = np.zeros((H, W), dtype=bool)
    th_t, tw_t = tex_rgb.shape[:2]

    u0, u1, u2 = u[F[:, 0]], u[F[:, 1]], u[F[:, 2]]
    v0, v1, v2 = wp[F[:, 0]], wp[F[:, 1]], wp[F[:, 2]]
    z0, z1, z2 = z[F[:, 0]], z[F[:, 1]], z[F[:, 2]]
    uv0, uv1, uv2 = uvs[F[:, 0]], uvs[F[:, 1]], uvs[F[:, 2]]

    vis = (z0 > 0.05) & (z1 > 0.05) & (z2 > 0.05)
    umin = np.floor(np.minimum.reduce([u0, u1, u2])).astype(int)
    umax = np.ceil(np.maximum.reduce([u0, u1, u2])).astype(int)
    vmin = np.floor(np.minimum.reduce([v0, v1, v2])).astype(int)
    vmax = np.ceil(np.maximum.reduce([v0, v1, v2])).astype(int)
    vis &= (umax >= 0) & (umin < W) & (vmax >= 0) & (vmin < H) & ((umax - umin) < W) & ((vmax - vmin) < H)

    for i in np.nonzero(vis)[0]:
        x0, x1 = max(int(umin[i]), 0), min(int(umax[i]), W - 1)
        y0, y1 = max(int(vmin[i]), 0), min(int(vmax[i]), H - 1)
        d = (v1[i] - v2[i]) * (u0[i] - u2[i]) + (u2[i] - u1[i]) * (v0[i] - v2[i])
        if x1 < x0 or y1 < y0 or abs(d) < 1e-10:
            continue
        px, py = np.meshgrid(np.arange(x0, x1 + 1), np.arange(y0, y1 + 1))
        l1 = ((v1[i] - v2[i]) * (px - u2[i]) + (u2[i] - u1[i]) * (py - v2[i])) / d
        l2 = ((v2[i] - v0[i]) * (px - u2[i]) + (u0[i] - u2[i]) * (py - v2[i])) / d
        ins = (l1 >= -1e-6) & (l2 >= -1e-6) & ((1.0 - l1 - l2) >= -1e-6)
        if not ins.any():
            continue
        w1_ = l1 / z0[i]
        w2_ = l2 / z1[i]
        w3_ = (1.0 - l1 - l2) / z2[i]
        zz = 1.0 / np.maximum(w1_ + w2_ + w3_, 1e-12)
        sz_b = zb[y0:y1 + 1, x0:x1 + 1]
        wr = ins & (zz < sz_b)
        if not wr.any():
            continue
        sz_b[wr] = zz[wr]
        fg[y0:y1 + 1, x0:x1 + 1][wr] = True
        ww1, ww2, ww3 = w1_[wr] * zz[wr], w2_[wr] * zz[wr], w3_[wr] * zz[wr]
        tu = np.mod(ww1 * uv0[i, 0] + ww2 * uv1[i, 0] + ww3 * uv2[i, 0], 1.0)
        tv = np.mod(ww1 * uv0[i, 1] + ww2 * uv1[i, 1] + ww3 * uv2[i, 1], 1.0)
        tx_ = np.clip(np.rint(tu * (tw_t - 1)).astype(np.int32), 0, tw_t - 1)
        ty_ = np.clip(np.rint((1.0 - tv) * (th_t - 1)).astype(np.int32), 0, th_t - 1)
        rgb[y0:y1 + 1, x0:x1 + 1][wr] = tex_rgb[ty_, tx_] * shade[i]

    return rgb, fg


def evaluate_reprojection_metrics(
    V_uv: np.ndarray,
    F_uv: np.ndarray,
    uvs: np.ndarray,
    T_tmpl_to_scan: np.ndarray,
    tex_transferred: np.ndarray,
    tex_baseline: np.ndarray,
    views: list[dict[str, Any]],
    eval_ds: int = 2,
) -> dict[str, Any]:
    """Compare transferred texture vs baseline template by reprojecting onto scan views."""
    V_s = (V_uv.astype(np.float64) @ T_tmpl_to_scan[:3, :3].T) + T_tmpl_to_scan[:3, 3]
    idxs = np.linspace(0, len(views) - 1, num=min(8, len(views)), dtype=int)

    psnr_trans, psnr_base = [], []
    de_trans, de_base = [], []
    ious = []

    for vi in idxs:
        vw = views[int(vi)]
        W, H = max(64, vw["W"] // eval_ds), max(64, vw["H"] // eval_ds)
        K = vw["K"].copy()
        K[0, :] /= eval_ds
        K[1, :] /= eval_ds

        gt_rgb = np.asarray(Image.open(vw["img_path"]).convert("RGB").resize((W, H), Image.Resampling.BILINEAR), dtype=np.float32) / 255.0
        gt_mask = np.asarray(Image.open(vw["mask_path"]).convert("L").resize((W, H), Image.Resampling.NEAREST)) > 127

        pred_t, fg_t = _rasterize_textured_view(V_s, F_uv, uvs, tex_transferred, K, vw["R"], vw["t"], W, H, shaded=False)
        pred_b, _ = _rasterize_textured_view(V_s, F_uv, uvs, tex_baseline, K, vw["R"], vw["t"], W, H, shaded=False)

        ious.append(float((fg_t & gt_mask).sum()) / max(1, int((fg_t | gt_mask).sum())))
        eval_region = fg_t & gt_mask
        if int(eval_region.sum()) < 100:
            continue

        mse_t = float(np.mean((pred_t[eval_region] - gt_rgb[eval_region]) ** 2))
        mse_b = float(np.mean((pred_b[eval_region] - gt_rgb[eval_region]) ** 2))
        psnr_trans.append(-10.0 * math.log10(max(mse_t, 1e-10)))
        psnr_base.append(-10.0 * math.log10(max(mse_b, 1e-10)))

        gt_lab = cv2.cvtColor((gt_rgb * 255).astype(np.uint8), cv2.COLOR_RGB2LAB).astype(np.float32)
        pt_lab = cv2.cvtColor((np.clip(pred_t, 0, 1) * 255).astype(np.uint8), cv2.COLOR_RGB2LAB).astype(np.float32)
        pb_lab = cv2.cvtColor((np.clip(pred_b, 0, 1) * 255).astype(np.uint8), cv2.COLOR_RGB2LAB).astype(np.float32)
        de_trans.append(float(np.mean(np.linalg.norm(pt_lab[eval_region] - gt_lab[eval_region], axis=1))))
        de_base.append(float(np.mean(np.linalg.norm(pb_lab[eval_region] - gt_lab[eval_region], axis=1))))

    return {
        "eval_views": len(idxs),
        "reproj_silhouette_iou_mean": round(float(np.mean(ious)) if ious else 0.0, 4),
        "baseline_psnr_db": round(float(np.mean(psnr_base)) if psnr_base else 0.0, 3),
        "transferred_psnr_db": round(float(np.mean(psnr_trans)) if psnr_trans else 0.0, 3),
        "psnr_gain_db": round((float(np.mean(psnr_trans)) - float(np.mean(psnr_base))) if psnr_trans else 0.0, 3),
        "baseline_lab_deltaE": round(float(np.mean(de_base)) if de_base else 0.0, 2),
        "transferred_lab_deltaE": round(float(np.mean(de_trans)) if de_trans else 0.0, 2),
    }


def render_alignment_overlay(
    V_uv: np.ndarray,
    F_uv: np.ndarray,
    uvs: np.ndarray,
    tex_transferred: np.ndarray,
    T_tmpl_to_scan: np.ndarray,
    views: list[dict[str, Any]],
    out_path: Path,
) -> None:
    """Save 4-view overlay comparing real captured frames against aligned template projection."""
    V_s = (V_uv.astype(np.float64) @ T_tmpl_to_scan[:3, :3].T) + T_tmpl_to_scan[:3, 3]
    idxs = np.linspace(0, len(views) - 1, num=min(4, len(views)), dtype=int)
    cw, ch, pad = 270, 480, 12
    sheet = Image.new("RGB", (len(idxs) * cw + (len(idxs) + 1) * pad, 2 * ch + 3 * pad + 44), (18, 20, 26))
    draw = ImageDraw.Draw(sheet)
    fh, fc = _load_font(18), _load_font(13)
    draw.text((pad, 12), "扫描图 vs 模板配准叠加与迁移重投影对照 (上: 真实图+绿框轮廓 / 下: 迁移模板重投影)", fill=(240, 245, 255), font=fh)

    for col, vi in enumerate(idxs):
        vw = views[int(vi)]
        W, H = max(64, vw["W"] // 2), max(64, vw["H"] // 2)
        K = vw["K"].copy()
        K[0, :] /= 2.0
        K[1, :] /= 2.0
        gt = np.asarray(Image.open(vw["img_path"]).convert("RGB").resize((W, H), Image.Resampling.BILINEAR), dtype=np.float32) / 255.0
        pred, fg = _rasterize_textured_view(V_s, F_uv, uvs, tex_transferred, K, vw["R"], vw["t"], W, H, shaded=True)

        fg_u8 = fg.astype(np.uint8) * 255
        edge = (cv2.dilate(fg_u8, np.ones((5, 5), np.uint8)) > 127) & (~fg)
        ov = gt.copy()
        ov[fg] = 0.78 * ov[fg] + 0.22 * pred[fg]
        ov[edge] = [0.25, 0.95, 0.50]

        x = pad + col * (cw + pad)
        y1 = 44 + pad
        y2 = 44 + 2 * pad + ch
        sheet.paste(Image.fromarray((np.clip(ov, 0, 1) * 255).astype(np.uint8)).resize((cw, ch), Image.Resampling.LANCZOS), (x, y1))
        sheet.paste(Image.fromarray((np.clip(pred, 0, 1) * 255).astype(np.uint8)).resize((cw, ch), Image.Resampling.LANCZOS), (x, y2))
        draw.text((x + 6, y1 + 6), f"{vw['stem']} 原图+对齐轮廓", fill=(120, 250, 170), font=fc)
        draw.text((x + 6, y2 + 6), f"{vw['stem']} 迁移模型渲染", fill=(140, 215, 255), font=fc)

    sheet.save(out_path, quality=94)


def render_comparison_4views(
    V_uv: np.ndarray,
    F_uv: np.ndarray,
    uvs: np.ndarray,
    tex_baseline: np.ndarray,
    tex_transferred: np.ndarray,
    out_path: Path,
) -> None:
    """Render 4 canonical angles (0, 45, 90, 180 deg) comparing baseline template vs transferred texture."""
    V = V_uv.astype(np.float64)
    center = V.mean(axis=0)
    span = float(np.max(V.max(axis=0) - V.min(axis=0)))
    tw, th = 360, 440
    K = np.array([[560.0, 0.0, tw / 2.0], [0.0, 560.0, th / 2.0], [0.0, 0.0, 1.0]], dtype=np.float64)
    radius = max(1.65 * span, 1.2)

    cw, ch, pad = 280, 342, 12
    sheet = Image.new("RGB", (4 * cw + 5 * pad, 2 * ch + 3 * pad + 48), (18, 20, 26))
    draw = ImageDraw.Draw(sheet)
    fh, fc = _load_font(18), _load_font(14)
    draw.text((pad, 12), "四视角外观对照: 上排 原模板基准 vs 下排 Scan-to-Template 多视角纹理迁移", fill=(240, 245, 255), font=fh)

    angles = [(0, "正面 0°"), (45, "斜前 45°"), (90, "侧面 90°"), (180, "背面 180°")]
    up_axis = np.array([0.0, 1.0, 0.0], dtype=np.float64)
    for row_idx, (row_name, tex) in enumerate([("原模板", tex_baseline), ("迁移版", tex_transferred)]):
        for col_idx, (deg, lbl) in enumerate(angles):
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
            im = Image.fromarray((np.clip(rgb, 0, 1) * 255).astype(np.uint8)).resize((cw, ch), Image.Resampling.LANCZOS)
            x = pad + col_idx * (cw + pad)
            y = 48 + pad + row_idx * (ch + pad)
            sheet.paste(im, (x, y))
            draw.rectangle([x, y, x + cw - 1, y + ch - 1], outline=(70, 80, 96), width=1)
            draw.text((x + 8, y + 8), f"{row_name} · {lbl}", fill=(140, 245, 180) if row_idx == 1 else (220, 225, 235), font=fc)

    sheet.save(out_path, quality=95)


def export_clean_glb(
    V_uv: np.ndarray,
    F_uv: np.ndarray,
    N_uv: np.ndarray,
    uvs: np.ndarray,
    albedo_rgb: np.ndarray,
    output_dir: Path,
    glb_name: str = "garment-transferred.glb",
) -> tuple[Path, Path, Path]:
    """Export textured GLB, strip Filament-banned extensions, and verify clean pass."""
    albedo_p = output_dir / "albedo_transferred.png"
    Image.fromarray((np.clip(albedo_rgb, 0.0, 1.0) * 255.0).astype(np.uint8), mode="RGB").save(albedo_p, optimize=True)

    gray_t = (0.299 * albedo_rgb[:, :, 0] + 0.587 * albedo_rgb[:, :, 1] + 0.114 * albedo_rgb[:, :, 2]).astype(np.float32)
    gx = cv2.Sobel(gray_t, cv2.CV_32F, 1, 0, ksize=3) / 4.0
    gy = cv2.Sobel(gray_t, cv2.CV_32F, 0, 1, ksize=3) / 4.0
    nx, ny, nz = -gx * 0.35, gy * 0.35, np.ones_like(gx)
    nlen = np.sqrt(nx * nx + ny * ny + nz * nz)
    normal_p = output_dir / "normal_transferred.png"
    Image.fromarray(
        (np.clip(np.stack([(nx / nlen) * 0.5 + 0.5, (ny / nlen) * 0.5 + 0.5, (nz / nlen) * 0.5 + 0.5], axis=-1), 0.0, 1.0) * 255.0).astype(np.uint8),
        mode="RGB",
    ).save(normal_p, optimize=True)

    mat = PBRMaterial(
        name="garment_transferred_mat",
        baseColorTexture=Image.open(albedo_p),
        normalTexture=Image.open(normal_p),
        baseColorFactor=[1.0, 1.0, 1.0, 1.0],
        metallicFactor=0.0,
        roughnessFactor=0.84,
        doubleSided=True,
    )
    raw_glb = output_dir / "_raw_transferred.glb"
    out_glb = output_dir / glb_name
    mesh = trimesh.Trimesh(
        vertices=V_uv,
        faces=F_uv,
        vertex_normals=N_uv,
        visual=trimesh.visual.TextureVisuals(uv=uvs, material=mat),
        process=False,
    )
    mesh.export(str(raw_glb))
    strip_file(str(raw_glb), str(out_glb))
    raw_glb.unlink(missing_ok=True)
    exts = find_extensions(read_glb(str(out_glb))[2])
    if exts:
        raise RuntimeError(f"导出 GLB 仍残留禁用扩展: {exts}")
    return out_glb, albedo_p, normal_p


def diagnose_model_defects(
    V_orig: np.ndarray,
    pos_orig_m: np.ndarray,
    norm_orig_m: np.ndarray,
    valid_m: np.ndarray,
    hit_mask: np.ndarray,
    quality_map: np.ndarray,
    obs_count_map: np.ndarray,
) -> dict[str, Any]:
    """Automatically diagnose per-region UV coverage & quality gaps and recommend targeted reshoot sectors."""
    x_mid, torso_half_x, has_sleeves = _compute_torso_half_width(np.asarray(V_orig, dtype=np.float64))
    vy, vx = np.nonzero(valid_m)
    P = pos_orig_m[vy, vx]
    N = norm_orig_m[vy, vx]
    H_hit = hit_mask[vy, vx]
    Q = quality_map[vy, vx]
    O = obs_count_map[vy, vx]

    y_min, y_max = float(P[:, 1].min()), float(P[:, 1].max())
    height = max(y_max - y_min, 1e-4)
    z_mid = float(np.median(P[:, 2]))

    # Focus on outward-facing garment surface (exclude inner lining faces pointing inward)
    rad_x = P[:, 0] - x_mid
    rad_z = P[:, 2] - z_mid
    outward = (rad_x * N[:, 0] + rad_z * N[:, 2]) >= -0.02

    dx = P[:, 0] - x_mid
    region_specs: list[tuple[str, str, np.ndarray, list[int], str]] = [
        (
            "front",
            "正面 / 胸口",
            outward & (np.abs(dx) <= torso_half_x * 1.10) & (N[:, 2] >= 0.35),
            [0, 1, 11],
            "正对前胸 0° 附近慢速移动并保持清晰对焦",
        ),
        (
            "right_seam",
            "右侧缝 / 腋下",
            outward & (dx >= torso_half_x * 0.45) & (np.abs(dx) <= torso_half_x * 1.15) & (N[:, 0] >= 0.35),
            [2, 3, 4],
            "抬起右臂或挂拍露出右侧缝，慢拍右侧 60°–150°",
        ),
        (
            "back",
            "背面 / 印花",
            outward & (np.abs(dx) <= torso_half_x * 1.10) & (N[:, 2] <= -0.35),
            [5, 6, 7],
            "绕到正后方 150°–240° 均匀采光慢拍",
        ),
        (
            "left_seam",
            "左侧缝 / 腋下",
            outward & (dx <= -torso_half_x * 0.45) & (np.abs(dx) <= torso_half_x * 1.15) & (N[:, 0] <= -0.35),
            [8, 9, 10],
            "抬起左臂或挂拍露出左侧缝，慢拍左侧 240°–330°",
        ),
        (
            "collar",
            "领口 / 肩线",
            outward & (P[:, 1] >= y_max - 0.14 * height) & (np.abs(dx) <= torso_half_x * 0.65),
            [0, 1, 11],
            "略俯拍 15° 并推近领口与肩线，拍清走线细节",
        ),
    ]
    if has_sleeves:
        region_specs.append(
            (
                "sleeves",
                "双袖筒 / 袖口",
                outward & (np.abs(dx) > torso_half_x * 1.15),
                [2, 3, 9, 10],
                "撑开或挂平袖筒，重点补拍左右 60°–120° 与 240°–300°",
            )
        )

    pos_q = Q[H_hit]
    low_q_thr = float(np.percentile(pos_q, 20)) if len(pos_q) >= 20 else 0.0

    regions_out: dict[str, Any] = {}
    defects: list[dict[str, Any]] = []
    rec_sectors: set[int] = set()
    for code, label, mask_r, sectors, hint in region_specs:
        n_tot = int(mask_r.sum())
        if n_tot < 20:
            continue
        hit_r = H_hit[mask_r]
        o_r = O[mask_r]
        q_r = Q[mask_r]
        hit_ratio = float(hit_r.mean())
        med_obs = float(np.median(o_r[hit_r])) if hit_r.any() else 0.0
        low_q_ratio = float((q_r[hit_r] <= low_q_thr).mean()) if hit_r.any() else 1.0

        status = "ok"
        if hit_ratio < 0.72:
            status = "missing_patch"
        elif med_obs < 4.0 or low_q_ratio > 0.40:
            status = "blurry_or_weak"

        info = {
            "label": label,
            "outer_texels": n_tot,
            "hit_ratio": round(hit_ratio, 4),
            "median_obs_views": round(med_obs, 2),
            "low_quality_ratio": round(low_q_ratio, 4),
            "status": status,
            "target_sectors": sectors,
        }
        regions_out[code] = info
        if status != "ok":
            rec_sectors.update(sectors)
            defects.append(
                {
                    "region": code,
                    "label": label,
                    "issue": status,
                    "hit_ratio": round(hit_ratio, 4),
                    "median_obs_views": round(med_obs, 2),
                    "target_sectors": sectors,
                    "reshoot_hint": hint,
                }
            )

    return {
        "overall_outer_hit_ratio": round(float(H_hit[outward].mean()) if outward.any() else float(H_hit.mean()), 4),
        "needs_reshoot": len(defects) > 0,
        "recommended_target_sectors": sorted(rec_sectors),
        "defects": defects,
        "regions": regions_out,
    }


def run_texture_transfer(
    scan_dir: Path,
    template_path: Path,
    output_dir: Path,
    scan_mesh_override: Path | None = None,
    tex_size: int = 1024,
    uv_mode: str = "xatlas",
    max_views: int = 60,
    front_cam_index: int = 0,
    allow_aniso_scale: bool = True,
    exposure_equalize: bool = True,
    facing_min: float = 0.20,
    sleeve_drop_mode: str = "auto",
    exclude_stems: set[str] | None = None,
    save_state: bool = True,
    glb_name: str = "garment-transferred.glb",
) -> dict[str, Any]:
    """Run full Scan-to-Template texture transfer pipeline and return structured report."""
    output_dir.mkdir(parents=True, exist_ok=True)
    tmpl_mesh, _ = load_template_mesh(template_path)
    orig_bounds = np.asarray(tmpl_mesh.bounds, dtype=np.float64)

    V_uv, F_uv, N_uv, VC_uv, uvs = prepare_template_uv(tmpl_mesh, uv_mode=uv_mode)
    all_views, up_scan = load_scan_views(scan_dir, max_views=max_views)
    views = [v for v in all_views if not exclude_stems or v["stem"] not in exclude_stems]
    if not views:
        raise RuntimeError("过滤 exclude_stems 后没有可用相机视角")
    scan_pts, scan_norms, _ = load_scan_reference_geometry(scan_dir, scan_mesh_override=scan_mesh_override)

    T_tmpl_to_scan, align_info = align_template_to_scan(
        tmpl_mesh=tmpl_mesh,
        scan_pts=scan_pts,
        scan_norms=scan_norms,
        views=views,
        up_scan=up_scan,
        front_cam_index=front_cam_index,
        allow_aniso_scale=allow_aniso_scale,
        sleeve_drop_mode=sleeve_drop_mode,
    )

    drop_deg = float(align_info.get("sleeve_drop_deg", 0.0))
    V_uv_posed, N_uv_posed, sleeve_meta = deform_sleeves_soft_lbs(V_uv, F_uv, drop_deg)

    # Original template UV positions/normals for 3D left/right symmetry completion & defect diagnosis
    pos_orig_m, norm_orig_m, vcol_m, valid_m = rasterize_template_uv_atlas(V_uv, N_uv, VC_uv, F_uv, uvs, tex_size=tex_size)
    # Temporarily posed UV positions/normals for multi-view projection & ray occlusion
    if sleeve_meta["applied"]:
        pos_sample_m, norm_sample_m, _, _ = rasterize_template_uv_atlas(
            V_uv_posed.astype(np.float32),
            N_uv_posed.astype(np.float32),
            VC_uv,
            F_uv,
            uvs,
            tex_size=tex_size,
        )
    else:
        pos_sample_m, norm_sample_m = pos_orig_m, norm_orig_m

    tex_raw, hit_mask, bake_stats, aux_maps = bake_multiview_to_template_uv(
        V_uv=V_uv_posed.astype(np.float32),
        F_uv=F_uv,
        pos_m=pos_sample_m,
        norm_m=norm_sample_m,
        vcol_m=vcol_m,
        valid_m=valid_m,
        T_tmpl_to_scan=T_tmpl_to_scan,
        views=views,
        facing_min=facing_min,
        exposure_equalize=exposure_equalize,
        return_aux=True,
    )
    defect_report = diagnose_model_defects(
        V_orig=V_uv,
        pos_orig_m=pos_orig_m,
        norm_orig_m=norm_orig_m,
        valid_m=valid_m,
        hit_mask=hit_mask,
        quality_map=aux_maps["quality_map"],
        obs_count_map=aux_maps["obs_count_map"],
    )
    with open(output_dir / "defect_report.json", "w", encoding="utf-8") as fh:
        json.dump(defect_report, fh, ensure_ascii=False, indent=2)

    tex_final, fill_stats = inpaint_and_dilate_uv(
        tex_raw,
        valid_m=valid_m,
        hit_mask=hit_mask,
        pos_orig_m=pos_orig_m,
        norm_orig_m=norm_orig_m,
    )
    # Export strictly with original T-pose V_uv & N_uv so garment fits person-001 with 0 geometry drift
    out_glb, albedo_p, normal_p = export_clean_glb(V_uv, F_uv, N_uv, uvs, tex_final, output_dir, glb_name=glb_name)

    if save_state:
        np.savez_compressed(
            output_dir / "transfer_state.npz",
            V_uv=V_uv.astype(np.float32),
            F_uv=F_uv.astype(np.int64),
            N_uv=N_uv.astype(np.float32),
            V_uv_posed=V_uv_posed.astype(np.float32),
            N_uv_posed=N_uv_posed.astype(np.float32),
            VC_uv=VC_uv.astype(np.float32),
            uvs=uvs.astype(np.float32),
            T_tmpl_to_scan=T_tmpl_to_scan.astype(np.float64),
            tex_observed_rgb=tex_raw.astype(np.float32),
            tex_final_rgb=tex_final.astype(np.float32),
            hit_mask=hit_mask.astype(bool),
            quality_map=aux_maps["quality_map"].astype(np.float32),
            obs_count_map=aux_maps["obs_count_map"].astype(np.int32),
            valid_m=valid_m.astype(bool),
            pos_orig_m=pos_orig_m.astype(np.float32),
            norm_orig_m=norm_orig_m.astype(np.float32),
            pos_sample_m=pos_sample_m.astype(np.float32),
            norm_sample_m=norm_sample_m.astype(np.float32),
            vcol_m=vcol_m.astype(np.float32),
            lab_target=aux_maps["lab_target"].astype(np.float32),
            sleeve_drop_deg=np.array([drop_deg], dtype=np.float32),
        )

    reproj_metrics = evaluate_reprojection_metrics(
        V_uv=V_uv_posed.astype(np.float32),
        F_uv=F_uv,
        uvs=uvs,
        T_tmpl_to_scan=T_tmpl_to_scan,
        tex_transferred=tex_final,
        tex_baseline=vcol_m,
        views=views,
    )
    render_alignment_overlay(V_uv_posed.astype(np.float32), F_uv, uvs, tex_final, T_tmpl_to_scan, views, output_dir / "alignment_overlay.png")
    render_comparison_4views(V_uv, F_uv, uvs, vcol_m, tex_final, output_dir / "comparison_4views.png")

    # Verify exported GLB geometry matches template bounds
    exported_mesh = trimesh.load(str(out_glb), force="mesh", process=False)
    bounds_diff = float(np.max(np.abs(np.asarray(exported_mesh.bounds) - orig_bounds)))

    report = {
        "template_vertices_uv": int(len(V_uv)),
        "template_faces": int(len(F_uv)),
        "tex_size": int(tex_size),
        "uv_mode": uv_mode,
        "bounds_drift_m": round(bounds_diff, 6),
        "alignment": align_info,
        "bake": bake_stats,
        "fill": fill_stats,
        "defects": defect_report,
        "reprojection": reproj_metrics,
        "glb_size_mb": round(out_glb.stat().st_size / 1048576.0, 3),
        "glb_extensions": find_extensions(read_glb(str(out_glb))[2]),
        "outputs": {
            "glb": str(out_glb),
            "albedo": str(albedo_p),
            "normal": str(normal_p),
            "defect_report": str(output_dir / "defect_report.json"),
            "transfer_state": str(output_dir / "transfer_state.npz") if save_state else None,
            "comparison_4views": str(output_dir / "comparison_4views.png"),
            "alignment_overlay": str(output_dir / "alignment_overlay.png"),
        },
    }
    with open(output_dir / "transfer_report.json", "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
    return report


def main() -> int:
    parser = ArgumentParser(description="Multi-view visibility-weighted Scan-to-Template texture transfer")
    parser.add_argument("--scan-dir", type=Path, required=True, help="V9 scan output directory")
    parser.add_argument("--template", type=Path, required=True, help="Template garment GLB/PLY/OBJ")
    parser.add_argument("--output-dir", type=Path, required=True, help="Output directory")
    parser.add_argument("--scan-mesh", type=Path, default=None, help="Optional scan mesh override")
    parser.add_argument("--tex-size", type=int, default=1024)
    parser.add_argument("--uv-mode", choices=("xatlas", "keep"), default="xatlas")
    parser.add_argument("--max-views", type=int, default=60)
    parser.add_argument("--front-cam-index", type=int, default=0)
    parser.add_argument("--no-aniso-scale", action="store_true")
    parser.add_argument("--no-exposure-eq", action="store_true")
    parser.add_argument("--facing-min", type=float, default=0.20)
    parser.add_argument("--sleeve-drop-deg", type=str, default="auto", help="'auto' or numeric degrees (e.g. 0, 60)")
    parser.add_argument("--glb-name", type=str, default="garment-transferred.glb")
    args = parser.parse_args()

    report = run_texture_transfer(
        scan_dir=args.scan_dir,
        template_path=args.template,
        output_dir=args.output_dir,
        scan_mesh_override=args.scan_mesh,
        tex_size=args.tex_size,
        uv_mode=args.uv_mode,
        max_views=args.max_views,
        front_cam_index=args.front_cam_index,
        allow_aniso_scale=not args.no_aniso_scale,
        exposure_equalize=not args.no_exposure_eq,
        facing_min=args.facing_min,
        sleeve_drop_mode=args.sleeve_drop_deg,
        glb_name=args.glb_name,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
