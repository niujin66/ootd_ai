"""Unit and integration tests for capture_analyzer (PNG, CV metrics, COLMAP, end-to-end)."""

from __future__ import annotations

import math
import os
import struct
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

import numpy as np
from fastapi.testclient import TestClient

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from app import create_app
from capture_analyzer import (
    analyze_capture_source,
    analyze_colmap_sparse,
    analyze_frames,
    compute_laplacian_sharpness,
    compute_orbit_continuity,
    decode_png_bytes,
    estimate_foreground_margins,
    estimate_head_eye_detail_px,
    extract_head_margins_from_mask,
)


def _encode_png(rgb: np.ndarray, filter_type: int = 0) -> bytes:
    """Minimal pure-Python 8-bit RGB PNG encoder supporting filter types 0/1/2."""
    h, w, c = rgb.shape
    assert c == 3
    raw = bytearray()
    for y in range(h):
        raw.append(filter_type)
        row = rgb[y].reshape(-1)
        if filter_type == 0:
            raw.extend(row.tobytes())
        elif filter_type == 1:
            filt = bytearray(len(row))
            for x in range(len(row)):
                left = int(row[x - 3]) if x >= 3 else 0
                filt[x] = (int(row[x]) - left) & 0xFF
            raw.extend(filt)
        elif filter_type == 2:
            up = rgb[y - 1].reshape(-1) if y > 0 else np.zeros_like(row)
            filt = ((row.astype(np.int32) - up.astype(np.int32)) & 0xFF).astype(np.uint8)
            raw.extend(filt.tobytes())
        else:
            raise ValueError(filter_type)

    def chunk(tag: bytes, payload: bytes) -> bytes:
        crc = zlib.crc32(tag + payload) & 0xFFFFFFFF
        return struct.pack(">I", len(payload)) + tag + payload + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    idat = zlib.compress(bytes(raw))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


def _rotmat_to_qvec(R: np.ndarray) -> np.ndarray:
    """Convert 3x3 rotation matrix to COLMAP (qw, qx, qy, qz)."""
    tr = float(np.trace(R))
    if tr > 0:
        s = math.sqrt(tr + 1.0) * 2.0
        qw = 0.25 * s
        qx = (R[2, 1] - R[1, 2]) / s
        qy = (R[0, 2] - R[2, 0]) / s
        qz = (R[1, 0] - R[0, 1]) / s
    else:
        idx = int(np.argmax([R[0, 0], R[1, 1], R[2, 2]]))
        if idx == 0:
            s = math.sqrt(1.0 + R[0, 0] - R[1, 1] - R[2, 2]) * 2.0
            qw = (R[2, 1] - R[1, 2]) / s
            qx = 0.25 * s
            qy = (R[0, 1] + R[1, 0]) / s
            qz = (R[0, 2] + R[2, 0]) / s
        elif idx == 1:
            s = math.sqrt(1.0 + R[1, 1] - R[0, 0] - R[2, 2]) * 2.0
            qw = (R[0, 2] - R[2, 0]) / s
            qx = (R[0, 1] + R[1, 0]) / s
            qy = 0.25 * s
            qz = (R[1, 2] + R[2, 1]) / s
        else:
            s = math.sqrt(1.0 + R[2, 2] - R[0, 0] - R[1, 1]) * 2.0
            qw = (R[1, 0] - R[0, 1]) / s
            qx = (R[0, 2] + R[2, 0]) / s
            qy = (R[1, 2] + R[2, 1]) / s
            qz = 0.25 * s
    return np.array([qw, qx, qy, qz], dtype=np.float64)


def _look_at_pose(cam_pos: np.ndarray, target: np.ndarray, up_hint: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Return COLMAP (qvec, tvec) where camera +Z points from cam_pos to target."""
    z_c = target - cam_pos
    z_c = z_c / np.linalg.norm(z_c)
    x_c = np.cross(z_c, up_hint)
    x_c = x_c / np.linalg.norm(x_c)
    y_c = np.cross(z_c, x_c)  # points down in image plane
    R_c2w = np.column_stack([x_c, y_c, z_c])
    R_w2c = R_c2w.T
    tvec = -R_w2c @ cam_pos
    return _rotmat_to_qvec(R_w2c), tvec


def _write_colmap_images_txt(path: Path, poses: list[tuple[int, np.ndarray, np.ndarray, str]]) -> None:
    lines = ["# Image list with two lines of data per image"]
    for iid, qvec, tvec, name in poses:
        q_str = " ".join(f"{v:.8f}" for v in qvec)
        t_str = " ".join(f"{v:.8f}" for v in tvec)
        lines.append(f"{iid} {q_str} {t_str} 1 {name}")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


class PngAndMetricsTests(unittest.TestCase):
    def test_png_roundtrip_across_filters(self):
        rng = np.random.default_rng(7)
        rgb = rng.integers(0, 256, size=(24, 32, 3), dtype=np.uint8)
        for ftype in (0, 1, 2):
            decoded = decode_png_bytes(_encode_png(rgb, filter_type=ftype))
            np.testing.assert_array_equal(decoded, rgb)

    def test_sharp_checkerboard_beats_smooth_frame(self):
        smooth = np.full((64, 64, 3), 128, dtype=np.uint8)
        sharp = np.zeros((64, 64, 3), dtype=np.uint8)
        sharp[::2, ::2] = 255
        sharp[1::2, 1::2] = 255
        self.assertLess(compute_laplacian_sharpness(smooth), 1.0)
        self.assertGreater(compute_laplacian_sharpness(sharp), 5000.0)

    def test_foreground_margin_detects_centered_vs_cropped_subject(self):
        # Centered subject on dark gray background
        centered = np.full((120, 100, 3), 30, dtype=np.uint8)
        centered[25:95, 20:80] = [220, 210, 200]
        res_ok = estimate_foreground_margins(centered)
        self.assertIsNotNone(res_ok["margins"])
        self.assertGreaterEqual(res_ok["margins"]["left"], 19)
        self.assertGreaterEqual(res_ok["margins"]["top"], 24)

        # Cropped subject touching top and bottom edges
        cropped = np.full((120, 100, 3), 30, dtype=np.uint8)
        cropped[0:120, 20:80] = [220, 210, 200]
        res_crop = estimate_foreground_margins(cropped)
        self.assertEqual(res_crop["margins"]["top"], 0)
        self.assertEqual(res_crop["margins"]["bottom"], 0)

    def test_orbit_continuity_detects_jump_frame(self):
        rng = np.random.default_rng(42)
        base = rng.integers(40, 220, size=(64, 64, 3), dtype=np.uint8)
        frame1 = base.copy()
        frame2 = np.roll(base, shift=1, axis=1)
        frame3 = 255 - base  # abrupt jump
        cont = compute_orbit_continuity([frame1, frame2, frame3])
        self.assertLess(cont["min"], 0.0)

    def test_illumination_shadow_at_border_does_not_leak_margins(self):
        # Neutral gray room with darker shadow band at left edge (same hue, lower brightness)
        img = np.full((160, 160, 3), [140, 140, 140], dtype=np.uint8)
        img[:, :28] = [95, 95, 95]  # border shadow on left wall
        img[35:125, 45:115] = [40, 75, 160]  # distinct centered blue garment
        res = estimate_foreground_margins(img)
        self.assertIsNotNone(res["margins"])
        self.assertGreaterEqual(res["margins"]["left"], 40)
        self.assertGreaterEqual(res["margins"]["right"], 40)

    def test_head_eye_detail_px_distinguishes_far_vs_closeup(self):
        # Distant small face (~16 px wide -> single eye ~3.4 px < 8 px)
        far_img = np.full((200, 200, 3), [30, 30, 40], dtype=np.uint8)
        far_img[60:84, 92:108] = [215, 165, 135]  # YCbCr skin tone
        far_eye = estimate_head_eye_detail_px(far_img)
        self.assertIsNotNone(far_eye)
        self.assertLess(far_eye, 8.0)

        # Close-up face (~100 px wide -> single eye ~21 px >= 16 px)
        close_img = np.full((200, 200, 3), [30, 30, 40], dtype=np.uint8)
        close_img[30:170, 50:150] = [215, 165, 135]
        close_eye = estimate_head_eye_detail_px(close_img)
        self.assertIsNotNone(close_eye)
        self.assertGreaterEqual(close_eye, 16.0)

    def test_extract_head_margins_ignores_wide_shoulders_at_bottom(self):
        # 240x200 portrait bust mask: head centered [40:120, 65:135] (margins >= 64px),
        # narrow neck [120:145, 85:115], wide shoulders [145:240, 0:200] hitting L/R/B=0!
        mask = np.zeros((240, 200), dtype=bool)
        mask[40:120, 65:135] = True
        mask[120:145, 85:115] = True
        mask[145:240, 0:200] = True
        res = extract_head_margins_from_mask(mask, orig_h=240, orig_w=200)
        self.assertIsNotNone(res["margins"])
        self.assertGreaterEqual(res["margins"]["left"], 60)
        self.assertGreaterEqual(res["margins"]["right"], 60)
        self.assertGreaterEqual(res["margins"]["top"], 38)


class ColmapAndEndToEndTests(unittest.TestCase):
    def test_colmap_half_orbit_with_rolled_cameras(self):
        with tempfile.TemporaryDirectory() as tmp:
            sparse_dir = Path(tmp) / "sparse" / "0"
            sparse_dir.mkdir(parents=True)
            target = np.array([0.0, 1.0, 0.0])
            up = np.array([0.0, 1.0, 0.0])
            poses = []
            # Only 0..150 deg (6/12 sectors) + heavy ~60 deg camera roll
            roll_rad = math.radians(60.0)
            rolled_up = np.array([math.sin(roll_rad), math.cos(roll_rad), 0.0])
            for idx, deg in enumerate(range(0, 160, 25), start=1):
                rad = math.radians(deg)
                cam_pos = np.array([2.0 * math.cos(rad), 1.0, 2.0 * math.sin(rad)])
                qvec, tvec = _look_at_pose(cam_pos, target, rolled_up)
                poses.append((idx, qvec, tvec, f"frame-{idx:03d}.png"))
            _write_colmap_images_txt(sparse_dir / "images.txt", poses)

            stats = analyze_colmap_sparse(sparse_dir, total_frames=70)
            self.assertEqual(stats["registered_frames"], len(poses))
            self.assertEqual(stats["frame_count"], 70)
            covered_sectors = {int(a // 30) for a in stats["covered_azimuth_deg"]}
            self.assertLessEqual(len(covered_sectors), 7)
            self.assertGreater(stats["up_roll_median_deg"], 45.0)

    def test_end_to_end_frames_plus_colmap_calls_guide_endpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            frames_dir = tmp_path / "frames"
            sparse_dir = tmp_path / "sparse" / "0"
            frames_dir.mkdir()
            sparse_dir.mkdir(parents=True)

            # Create 6 sharp, centered, smoothly-panning PNG frames (margin >= 50px, garment width 200px -> roi >= 16px)
            rng = np.random.default_rng(11)
            base_patch = rng.integers(140, 240, size=(200, 200, 3), dtype=np.uint8)
            for idx in range(6):
                img = np.full((320, 320, 3), 25, dtype=np.uint8)
                x0 = 55 + idx
                img[60:260, x0 : x0 + 200] = base_patch
                (frames_dir / f"frame-{idx:03d}.png").write_bytes(_encode_png(img, filter_type=1))

            # Full 360 orbit upright poses -> should pass if margins & continuity are good
            target = np.array([0.0, 0.0, 0.0])
            up = np.array([0.0, 1.0, 0.0])
            poses = []
            for idx in range(12):
                rad = math.radians(idx * 30 + 15)
                cam_pos = np.array([2.0 * math.cos(rad), 0.0, 2.0 * math.sin(rad)])
                qvec, tvec = _look_at_pose(cam_pos, target, up)
                poses.append((idx + 1, qvec, tvec, f"frame-{idx:03d}.png"))
            _write_colmap_images_txt(sparse_dir / "images.txt", poses)

            payload = analyze_capture_source(
                request_id="e2e-test-01",
                target_type="garment",
                subject_label="synthetic-clean",
                input_path=frames_dir,
                sparse_dir=sparse_dir,
                has_scale_reference=True,
            )

            orig_mode = os.environ.get("OOTD_AI_MODE")
            os.environ["OOTD_AI_MODE"] = "mock"
            try:
                client = TestClient(
                    create_app(db_path=tmp_path / "test.db", assets_root=tmp_path / "assets")
                )
                resp = client.post("/v1/capture/guide", json=payload)
            finally:
                if orig_mode is None:
                    os.environ.pop("OOTD_AI_MODE", None)
                else:
                    os.environ["OOTD_AI_MODE"] = orig_mode

            self.assertEqual(resp.status_code, 200)
            body = resp.json()
            self.assertEqual(body["readiness"], "pass")
            self.assertTrue(body["guide"]["can_reuse_existing"])


if __name__ == "__main__":
    unittest.main()
