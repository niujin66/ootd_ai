"""Self-contained synthetic unit tests for transfer_scan_texture.py."""
from __future__ import annotations

import math
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image
import trimesh

RECON_DIR = Path(__file__).resolve().parents[1]
if str(RECON_DIR) not in sys.path:
    sys.path.insert(0, str(RECON_DIR))

from transfer_scan_texture import (  # noqa: E402
    _rasterize_textured_view,
    deform_sleeves_soft_lbs,
    inpaint_and_dilate_uv,
    run_texture_transfer,
)


def _make_synthetic_garment_mesh() -> tuple[trimesh.Trimesh, np.ndarray]:
    """Create a sealed boxy garment-like torso mesh (Y-up, front +Z) with distinct front/back textures."""
    mesh = trimesh.creation.icosphere(subdivisions=3, radius=1.0)
    V = np.asarray(mesh.vertices, dtype=np.float64)
    # Shape into a torso-like ellipsoid: width ~0.50m, height ~0.72m, depth ~0.26m
    V[:, 0] *= 0.25
    V[:, 1] = V[:, 1] * 0.36 + 1.15
    V[:, 2] *= 0.13
    mesh.vertices = V

    # Planar front/back UV & synthetic ground-truth texture (front: blue + red chest badge, back: green stripes)
    tex_w, tex_h = 256, 256
    gt_tex = np.full((tex_h, tex_w, 3), [0.85, 0.85, 0.85], dtype=np.float32)
    # Left half of atlas: front (u in [0, 0.5]), right half: back (u in [0.5, 1.0])
    gt_tex[:, :128] = [0.22, 0.45, 0.88]  # Blue front
    # Red chest circle in front center
    yy, xx = np.ogrid[:tex_h, :128]
    dist_sq = (xx - 64) ** 2 + (yy - 110) ** 2
    gt_tex[:, :128][dist_sq <= 22 ** 2] = [0.92, 0.18, 0.18]
    # Green vertical stripes on back
    gt_tex[:, 128:] = [0.20, 0.72, 0.35]
    for sx in range(136, 256, 24):
        gt_tex[:, sx:sx + 8] = [0.95, 0.88, 0.20]

    # Map each vertex to UV based on Z sign (front vs back)
    u = np.where(
        V[:, 2] >= 0.0,
        0.04 + 0.42 * np.clip((V[:, 0] + 0.25) / 0.50, 0.0, 1.0),
        0.54 + 0.42 * np.clip((V[:, 0] + 0.25) / 0.50, 0.0, 1.0),
    )
    v = 0.08 + 0.84 * np.clip((V[:, 1] - 0.79) / 0.72, 0.0, 1.0)
    uvs = np.column_stack([u, v]).astype(np.float32)
    return mesh, gt_tex, uvs


class TransferScanTextureTest(unittest.TestCase):

    def test_synthetic_end_to_end_alignment_and_texture_transfer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scan_dir = root / "scan"
            out_dir = root / "out"
            (scan_dir / "2dgs_out").mkdir(parents=True)
            (scan_dir / "2dgs_data_garment" / "images").mkdir(parents=True)
            (scan_dir / "2dgs_data_garment" / "masks").mkdir(parents=True)
            (scan_dir / "mesh").mkdir(parents=True)

            # Template mesh (plain neutral gray)
            tmpl_mesh, gt_tex, gt_uvs = _make_synthetic_garment_mesh()
            tmpl_neutral = tmpl_mesh.copy()
            tmpl_neutral.visual = trimesh.visual.ColorVisuals(
                mesh=tmpl_neutral,
                vertex_colors=np.tile(np.array([210, 210, 210, 255], dtype=np.uint8), (len(tmpl_mesh.vertices), 1)),
            )
            tmpl_path = root / "template.glb"
            tmpl_neutral.export(str(tmpl_path))

            # Ground-truth scan space transform: rotate around Y by 55 deg, scale 1.8x, translate
            ang = math.radians(55.0)
            R_gt = np.array(
                [
                    [math.cos(ang), 0.0, math.sin(ang)],
                    [0.0, 1.0, 0.0],
                    [-math.sin(ang), 0.0, math.cos(ang)],
                ],
                dtype=np.float64,
            )
            scale_gt = 1.8
            t_gt = np.array([0.35, -0.40, 0.20], dtype=np.float64)
            V_scan = (np.asarray(tmpl_mesh.vertices) @ (R_gt * scale_gt).T) + t_gt
            F_scan = np.asarray(tmpl_mesh.faces, dtype=np.int64)
            scan_mesh = trimesh.Trimesh(vertices=V_scan, faces=F_scan, process=False)
            scan_mesh.export(str(scan_dir / "mesh" / "garment-clean.ply"))

            # Generate 16 synthetic cameras orbiting the transformed scan mesh
            # Camera 0 looks directly at transformed template +Z front face
            center_scan = V_scan.mean(axis=0)
            front_dir_world = R_gt @ np.array([0.0, 0.0, 1.0])
            W, H = 240, 320
            K = np.array([[360.0, 0.0, W / 2.0], [0.0, 360.0, H / 2.0], [0.0, 0.0, 1.0]], dtype=np.float64)
            up_world = np.array([0.0, 1.0, 0.0], dtype=np.float64)
            cameras_json = []

            n_cams = 16
            for idx in range(n_cams):
                az = (2.0 * math.pi * idx) / n_cams
                dir_world = (
                    math.cos(az) * front_dir_world
                    + math.sin(az) * np.cross(up_world, front_dir_world)
                )
                cam_pos = center_scan + 2.4 * dir_world
                fwd = center_scan - cam_pos
                fwd /= np.linalg.norm(fwd)
                right = np.cross(fwd, up_world)
                right /= np.linalg.norm(right)
                down = np.cross(fwd, right)
                R_w2c = np.stack([right, down, fwd], axis=0)
                t_w2c = -R_w2c @ cam_pos

                rgb, fg = _rasterize_textured_view(
                    V_scan,
                    F_scan,
                    gt_uvs,
                    gt_tex,
                    K,
                    R_w2c,
                    t_w2c,
                    W,
                    H,
                    bg_rgb=(0.05, 0.05, 0.05),
                    shaded=False,
                )
                stem = f"frame-{idx:05d}"
                Image.fromarray((np.clip(rgb, 0.0, 1.0) * 255.0).astype(np.uint8)).save(
                    scan_dir / "2dgs_data_garment" / "images" / f"{stem}.jpg",
                    quality=96,
                )
                Image.fromarray((fg.astype(np.uint8) * 255), mode="L").save(
                    scan_dir / "2dgs_data_garment" / "masks" / f"{stem}.png"
                )
                cameras_json.append(
                    {
                        "id": idx,
                        "img_name": stem,
                        "width": W,
                        "height": H,
                        "position": cam_pos.tolist(),
                        "rotation": R_w2c.T.tolist(),
                        "fx": float(K[0, 0]),
                        "fy": float(K[1, 1]),
                    }
                )

            with open(scan_dir / "2dgs_out" / "cameras.json", "w", encoding="utf-8") as fh:
                import json
                json.dump(cameras_json, fh)

            report = run_texture_transfer(
                scan_dir=scan_dir,
                template_path=tmpl_path,
                output_dir=out_dir,
                tex_size=256,
                max_views=16,
                front_cam_index=0,
                allow_aniso_scale=False,
                exposure_equalize=False,
            )

            self.assertGreater(report["alignment"]["silhouette_iou"], 0.85)
            self.assertGreater(report["bake"]["hit_ratio"], 0.90)
            self.assertEqual(report["fill"]["black_texel_ratio_in_valid_uv"], 0.0)
            self.assertGreater(report["reprojection"]["transferred_psnr_db"], 22.0)
            self.assertGreater(report["reprojection"]["psnr_gain_db"], 8.0)
            self.assertEqual(report["glb_extensions"], [])
            self.assertLess(report["bounds_drift_m"], 1e-4)
            self.assertTrue((out_dir / "garment-transferred.glb").is_file())
            self.assertTrue((out_dir / "comparison_4views.png").is_file())
            self.assertTrue((out_dir / "alignment_overlay.png").is_file())

    def test_soft_lbs_sleeve_drop_and_symmetry_completion(self) -> None:
        # Construct synthetic T-pose garment mesh: torso box + left/right sleeves
        torso = trimesh.creation.box(extents=[0.40, 0.72, 0.22])
        torso.apply_translation([0.0, 1.15, 0.0])
        r_sleeve = trimesh.creation.box(extents=[0.24, 0.14, 0.14])
        r_sleeve.apply_translation([0.30, 1.44, 0.0])
        l_sleeve = trimesh.creation.box(extents=[0.24, 0.14, 0.14])
        l_sleeve.apply_translation([-0.30, 1.44, 0.0])
        tpose = trimesh.util.concatenate([torso, r_sleeve, l_sleeve]).subdivide().subdivide()

        V = np.asarray(tpose.vertices, dtype=np.float64)
        F = np.asarray(tpose.faces, dtype=np.int64)
        V_posed, N_posed, meta = deform_sleeves_soft_lbs(V, F, drop_deg=60.0)
        self.assertTrue(meta["applied"])
        self.assertEqual(V_posed.shape, V.shape)
        self.assertEqual(N_posed.shape, V.shape)

        # Lower torso vertices (y < 1.25) must stay essentially stationary
        low_torso = V[:, 1] < 1.25
        self.assertLess(np.max(np.abs(V_posed[low_torso] - V[low_torso])), 1e-6)

        # Sleeve tips (|x| > 0.36) must drop substantially downward along -Y
        tips = np.abs(V[:, 0]) > 0.36
        self.assertGreater(float(np.mean(V[tips, 1] - V_posed[tips, 1])), 0.10)

        # Test 3D symmetry borrowing: plain white cloth on right side (+x > 0.10), occluded on left side (-x < -0.10),
        # plus an asymmetric red print on right chest that must NOT be mirrored onto the left side.
        H, W = 64, 64
        valid_m = np.ones((H, W), dtype=bool)
        x_coords = np.linspace(-0.20, 0.20, W, dtype=np.float32)
        y_coords = np.linspace(0.90, 1.50, H, dtype=np.float32)
        gx, gy = np.meshgrid(x_coords, y_coords)
        pos_orig_m = np.stack([gx, gy, np.full_like(gx, 0.10)], axis=-1)
        norm_orig_m = np.zeros_like(pos_orig_m)
        norm_orig_m[:, :, 2] = 1.0

        # Right side observed (x >= 0.02); left side mostly missed (x <= -0.04)
        hit_mask = gx >= 0.02
        tex_rgb = np.full((H, W, 3), [0.5, 0.5, 0.5], dtype=np.float32)
        tex_rgb[hit_mask] = [0.92, 0.91, 0.89]
        # Asymmetric bright red chest badge at x in [0.06, 0.12], y in [1.15, 1.25]
        badge = (gx >= 0.06) & (gx <= 0.12) & (gy >= 1.15) & (gy <= 1.25)
        tex_rgb[badge] = [0.95, 0.10, 0.10]

        filled, stats = inpaint_and_dilate_uv(
            tex_rgb=tex_rgb,
            valid_m=valid_m,
            hit_mask=hit_mask,
            pos_orig_m=pos_orig_m,
            norm_orig_m=norm_orig_m,
        )
        self.assertGreater(stats["symmetry_borrowed_texels"], 100)
        self.assertEqual(stats["black_texel_ratio_in_valid_uv"], 0.0)
        # Left mirror of badge (-0.12 <= x <= -0.06, 1.15 <= y <= 1.25) must stay plain white cloth, NOT red!
        mirror_badge = (gx <= -0.06) & (gx >= -0.12) & (gy >= 1.15) & (gy <= 1.25)
        self.assertGreater(float(np.mean(filled[mirror_badge, 1])), 0.80)


if __name__ == "__main__":
    unittest.main()
