"""End-to-end synthetic tests for defect diagnosis and incremental reshoot texture refinement."""
from __future__ import annotations

import json
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

from refine_texture_with_reshoot import refine_texture_with_reshoot  # noqa: E402
from transfer_scan_texture import _rasterize_textured_view, run_texture_transfer  # noqa: E402


def _make_garment_with_front_back_right_patterns() -> tuple[trimesh.Trimesh, np.ndarray, np.ndarray]:
    mesh = trimesh.creation.icosphere(subdivisions=3, radius=1.0)
    V = np.asarray(mesh.vertices, dtype=np.float64)
    V[:, 0] *= 0.25
    V[:, 1] = V[:, 1] * 0.36 + 1.15
    V[:, 2] *= 0.13
    mesh.vertices = V

    tex_w, tex_h = 256, 256
    gt_tex = np.full((tex_h, tex_w, 3), [0.85, 0.86, 0.88], dtype=np.float32)
    gt_tex[:, :128] = [0.20, 0.45, 0.90]
    yy, xx = np.ogrid[:tex_h, :128]
    gt_tex[:, :128][((xx - 64) ** 2 + (yy - 110) ** 2) <= 22 ** 2] = [0.95, 0.16, 0.16]
    gt_tex[:, 128:] = [0.22, 0.72, 0.38]
    for sx in range(136, 256, 20):
        gt_tex[:, sx:sx + 7] = [0.95, 0.85, 0.18]

    u = np.where(
        V[:, 2] >= 0.0,
        0.04 + 0.42 * np.clip((V[:, 0] + 0.25) / 0.50, 0.0, 1.0),
        0.54 + 0.42 * np.clip((V[:, 0] + 0.25) / 0.50, 0.0, 1.0),
    )
    v = 0.08 + 0.84 * np.clip((V[:, 1] - 0.79) / 0.72, 0.0, 1.0)
    uvs = np.column_stack([u, v]).astype(np.float32)
    return mesh, gt_tex, uvs


class RefineTextureWithReshootTest(unittest.TestCase):

    def test_defect_detection_and_incremental_reshoot_refinement(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scan_dir = root / "scan"
            state_dir = root / "state"
            reshoot_img_dir = root / "reshoot_imgs"
            reshoot_msk_dir = root / "reshoot_masks"
            out_dir = root / "refined"
            for d in (
                scan_dir / "2dgs_out",
                scan_dir / "2dgs_data_garment" / "images",
                scan_dir / "2dgs_data_garment" / "masks",
                scan_dir / "mesh",
                reshoot_img_dir,
                reshoot_msk_dir,
            ):
                d.mkdir(parents=True, exist_ok=True)

            tmpl_mesh, gt_tex, gt_uvs = _make_garment_with_front_back_right_patterns()
            tmpl_neutral = tmpl_mesh.copy()
            tmpl_neutral.visual = trimesh.visual.ColorVisuals(
                mesh=tmpl_neutral,
                vertex_colors=np.tile(np.array([210, 210, 210, 255], dtype=np.uint8), (len(tmpl_mesh.vertices), 1)),
            )
            tmpl_path = root / "template.glb"
            tmpl_neutral.export(str(tmpl_path))

            ang = math.radians(30.0)
            R_gt = np.array(
                [
                    [math.cos(ang), 0.0, math.sin(ang)],
                    [0.0, 1.0, 0.0],
                    [-math.sin(ang), 0.0, math.cos(ang)],
                ],
                dtype=np.float64,
            )
            scale_gt = 1.5
            t_gt = np.array([0.2, -0.3, 0.1], dtype=np.float64)
            V_scan = (np.asarray(tmpl_mesh.vertices) @ (R_gt * scale_gt).T) + t_gt
            F_scan = np.asarray(tmpl_mesh.faces, dtype=np.int64)
            trimesh.Trimesh(vertices=V_scan, faces=F_scan, process=False).export(str(scan_dir / "mesh" / "garment-clean.ply"))

            center_scan = V_scan.mean(axis=0)
            front_dir_world = R_gt @ np.array([0.0, 0.0, 1.0])
            up_world = np.array([0.0, 1.0, 0.0], dtype=np.float64)
            W, H = 240, 320
            K = np.array([[360.0, 0.0, W / 2.0], [0.0, 360.0, H / 2.0], [0.0, 0.0, 1.0]], dtype=np.float64)
            cameras_json = []
            reshoot_poses = {}
            back_stems: set[str] = set()

            n_cams = 16
            for idx in range(n_cams):
                az = (2.0 * math.pi * idx) / n_cams
                dir_world = math.cos(az) * front_dir_world + math.sin(az) * np.cross(up_world, front_dir_world)
                cam_pos = center_scan + 2.4 * dir_world
                fwd = center_scan - cam_pos
                fwd /= np.linalg.norm(fwd)
                right = np.cross(fwd, up_world)
                right /= np.linalg.norm(right)
                down = np.cross(fwd, right)
                R_w2c = np.stack([right, down, fwd], axis=0)
                t_w2c = -R_w2c @ cam_pos
                rgb, fg = _rasterize_textured_view(
                    V_scan, F_scan, gt_uvs, gt_tex, K, R_w2c, t_w2c, W, H, bg_rgb=(0.05, 0.05, 0.05), shaded=False
                )
                stem = f"frame-{idx:05d}"
                Image.fromarray((np.clip(rgb, 0.0, 1.0) * 255.0).astype(np.uint8)).save(
                    scan_dir / "2dgs_data_garment" / "images" / f"{stem}.jpg", quality=96
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
                # Hold out back views (indices 5..11) as targeted reshoot set
                if 5 <= idx <= 11:
                    back_stems.add(stem)
                    Image.fromarray((np.clip(rgb, 0.0, 1.0) * 255.0).astype(np.uint8)).save(
                        reshoot_img_dir / f"{stem}.jpg", quality=96
                    )
                    Image.fromarray((fg.astype(np.uint8) * 255), mode="L").save(
                        reshoot_msk_dir / f"{stem}.png"
                    )
                    reshoot_poses[stem] = {
                        "width": W,
                        "height": H,
                        "fx": float(K[0, 0]),
                        "fy": float(K[1, 1]),
                        "R": R_w2c.tolist(),
                        "t": t_w2c.tolist(),
                    }

            with open(scan_dir / "2dgs_out" / "cameras.json", "w", encoding="utf-8") as fh:
                json.dump(cameras_json, fh)
            poses_path = root / "reshoot_poses.json"
            with open(poses_path, "w", encoding="utf-8") as fh:
                json.dump(reshoot_poses, fh)

            # 1. Initial incomplete transfer missing the back arc
            init_report = run_texture_transfer(
                scan_dir=scan_dir,
                template_path=tmpl_path,
                output_dir=state_dir,
                tex_size=256,
                max_views=16,
                front_cam_index=0,
                allow_aniso_scale=False,
                exposure_equalize=False,
                exclude_stems=back_stems,
                save_state=True,
            )
            self.assertTrue(init_report["defects"]["needs_reshoot"])
            back_info_before = init_report["defects"]["regions"]["back"]
            self.assertEqual(back_info_before["status"], "missing_patch")
            self.assertLess(back_info_before["hit_ratio"], 0.65)

            # 2. Incremental reshoot fusion
            refine_report = refine_texture_with_reshoot(
                state_dir=state_dir,
                output_dir=out_dir,
                reshoot_images_dir=reshoot_img_dir,
                reshoot_masks_dir=reshoot_msk_dir,
                reshoot_poses_path=poses_path,
                auto_segformer=False,
                exposure_equalize=False,
                eval_scan_dir=scan_dir,
            )

            cov = refine_report["coverage"]
            self.assertGreater(cov["newly_filled_texels"], 3000)
            self.assertGreater(cov["hit_ratio_after"], cov["hit_ratio_before"] + 0.15)
            self.assertEqual(cov["locked_existing_max_drift"], 0.0)

            back_info_after = refine_report["defects_after"]["regions"]["back"]
            self.assertGreater(back_info_after["hit_ratio"], 0.90)
            self.assertEqual(back_info_after["status"], "ok")
            self.assertGreater(refine_report["reprojection_comparison"]["psnr_delta_db"], 2.0)
            self.assertTrue((out_dir / "garment-refined.glb").is_file())
            self.assertTrue((out_dir / "before_after_4views.png").is_file())
            self.assertTrue((out_dir / "patch_diff_map.png").is_file())


if __name__ == "__main__":
    unittest.main()
