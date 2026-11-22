"""Unit and endpoint tests for capture quality inspection & AI reshoot guide."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from app import create_app
from capture_guide import (
    AICaptureGuide,
    analyze_capture_metrics,
    mock_capture_guide,
    validate_guide_output,
)


def _mock_transport(status_code: int, payload: dict | None = None, raise_exc: Exception | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if raise_exc is not None:
            raise raise_exc
        return httpx.Response(status_code=status_code, json=payload)

    return httpx.MockTransport(handler)


class CaptureDiagnosisTests(unittest.TestCase):
    def test_head_half_orbit_roll_and_low_eye_px_is_invalid_retake(self):
        diag = analyze_capture_metrics(
            {
                "target_type": "head",
                "subject_label": "person-002",
                "azimuth_span_deg": 180.0,
                "up_roll_median_deg": 81.0,
                "roi_detail_px": 3.0,
                "frame_count": 1097,
                "registered_frames": 96,
                "has_scale_reference": False,
                "missing_closeups": ["左右耳", "后脑", "头顶"],
            }
        )
        self.assertEqual(diag["readiness"], "invalid_retake")
        codes = {item["code"] for item in diag["issues"]}
        self.assertIn("azimuth_severe_gap", codes)
        self.assertIn("camera_roll_severe", codes)
        self.assertIn("roi_resolution_critical", codes)
        self.assertIn("low_registration_ratio", codes)
        self.assertIn("missing_scale_reference", codes)
        self.assertIn("missing_closeups", codes)
        # Critical issues must come first
        self.assertEqual(diag["issues"][0]["severity"], "critical")

    def test_garment_sector_gap_and_tight_margin_needs_reshoot(self):
        # Cover 10 of 12 sectors (missing 180-240 deg)
        angles = [0, 35, 65, 95, 125, 155, 245, 275, 305, 335]
        diag = analyze_capture_metrics(
            {
                "target_type": "garment",
                "subject_label": "blazer-alt",
                "covered_azimuth_deg": angles,
                "min_margin_px": {"left": 86, "right": 87, "top": 21, "bottom": 21},
                "sharpness_p50": 244.2,
                "orbit_continuity_min": 0.291,
                "has_scale_reference": True,
            }
        )
        self.assertEqual(diag["readiness"], "needs_reshoot")
        self.assertEqual(diag["sector_summary"]["covered_sectors"], 10)
        self.assertEqual(diag["sector_summary"]["missing_arcs"], ["180°-240°"])
        self.assertEqual(diag["sector_summary"]["largest_gap_deg"], 60)
        codes = {item["code"] for item in diag["issues"]}
        self.assertIn("azimuth_partial_gap", codes)
        self.assertIn("edge_tight_margin", codes)
        self.assertIn("orbit_jump", codes)

    def test_clean_capture_passes(self):
        angles = [i * 30 + 10 for i in range(12)]
        diag = analyze_capture_metrics(
            {
                "target_type": "garment",
                "covered_azimuth_deg": angles,
                "up_roll_median_deg": 4.5,
                "min_margin_px": {"left": 100, "right": 100, "top": 120, "bottom": 120},
                "sharpness_p50": 85.0,
                "orbit_continuity_min": 0.65,
                "roi_detail_px": 40.0,
                "has_scale_reference": True,
                "frame_count": 120,
                "registered_frames": 118,
            }
        )
        self.assertEqual(diag["readiness"], "pass")
        self.assertEqual(diag["issue_count"], 0)
        guide = mock_capture_guide(diag)
        self.assertTrue(guide["can_reuse_existing"])
        self.assertEqual(guide["steps"], [])


class GuideValidationTests(unittest.TestCase):
    def setUp(self):
        self.invalid_diag = analyze_capture_metrics(
            {"target_type": "head", "azimuth_span_deg": 180.0, "up_roll_median_deg": 81.0}
        )
        self.reshoot_diag = analyze_capture_metrics(
            {"target_type": "garment", "orbit_continuity_min": 0.25}
        )

    def test_rejects_reuse_true_when_invalid_retake(self):
        raw = json.dumps(
            {
                "summary": "可以继续用",
                "can_reuse_existing": True,
                "steps": [
                    {"priority": 1, "target_area": "背面", "action": "补拍背面", "reason": "缺半圈"}
                ],
            },
            ensure_ascii=False,
        )
        ok, res = validate_guide_output(raw, self.invalid_diag)
        self.assertFalse(ok)
        self.assertIn("invalid_retake", res["error"])

    def test_rejects_empty_steps_when_issues_exist(self):
        raw = json.dumps(
            {"summary": "局部有问题", "can_reuse_existing": True, "steps": []},
            ensure_ascii=False,
        )
        ok, res = validate_guide_output(raw, self.reshoot_diag)
        self.assertFalse(ok)
        self.assertIn("steps", res["error"])

    def test_accepts_valid_structured_output(self):
        raw = json.dumps(
            {
                "summary": "建议放慢移动速度补拍跳帧区间。",
                "can_reuse_existing": True,
                "steps": [
                    {
                        "priority": 1,
                        "target_area": "环绕轨迹连续性",
                        "action": "放慢绕拍速度并在侧背交界停留 0.5 秒。",
                        "reason": "相邻帧最低相似度 0.25 低于 0.35。",
                    }
                ],
            },
            ensure_ascii=False,
        )
        ok, res = validate_guide_output(raw, self.reshoot_diag)
        self.assertTrue(ok)
        self.assertEqual(len(res["steps"]), 1)


class CaptureGuideEndpointTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.db_path = self.tmp / "guide.db"
        self.assets_root = self.tmp / "assets"
        self.log_path = self.tmp / "calls.jsonl"
        self._orig_env = {
            k: os.environ.get(k)
            for k in ("OOTD_AI_MODE", "OOTD_CALL_LOG", "OOTD_AI_MAX_CALLS_PER_DAY")
        }
        os.environ["OOTD_CALL_LOG"] = str(self.log_path)

    def tearDown(self):
        for k, v in self._orig_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._tmp.cleanup()

    def test_mock_mode_returns_deterministic_steps(self):
        os.environ["OOTD_AI_MODE"] = "mock"
        client = TestClient(create_app(db_path=self.db_path, assets_root=self.assets_root))
        resp = client.post(
            "/v1/capture/guide",
            json={
                "request_id": "cap-mock-01",
                "target_type": "head",
                "subject_label": "person-002",
                "azimuth_span_deg": 180.0,
                "up_roll_median_deg": 81.0,
                "roi_detail_px": 3.0,
            },
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["mode"], "mock")
        self.assertEqual(body["readiness"], "invalid_retake")
        self.assertFalse(body["guide"]["can_reuse_existing"])
        self.assertGreaterEqual(len(body["guide"]["steps"]), 3)

    def test_ai_mode_success_logs_and_returns_mode_ai(self):
        os.environ["OOTD_AI_MODE"] = "ai"
        ai_json = {
            "summary": "当前头部素材缺半圈且眼部像素过低，需按步骤重拍。",
            "can_reuse_existing": False,
            "steps": [
                {
                    "priority": 1,
                    "target_area": "环绕方位与姿态",
                    "action": "保持手机水平，距离 1 米匀速绕头完整一圈。",
                    "reason": "仅覆盖 180° 且翻滚偏差 81°。",
                },
                {
                    "priority": 2,
                    "target_area": "眼部近景特写",
                    "action": "靠近至 50 cm 补拍正面与左右斜侧眼部清晰特写。",
                    "reason": "单眼仅 3 px，无法支撑贴图重建。",
                },
            ],
        }
        transport = _mock_transport(
            200,
            {
                "choices": [{"message": {"content": json.dumps(ai_json, ensure_ascii=False)}}],
                "usage": {"total_tokens": 200},
            },
        )
        engine = AICaptureGuide(
            "https://example.invalid",
            "dummy-key",
            "test-model",
            client=httpx.Client(transport=transport),
        )
        client = TestClient(
            create_app(db_path=self.db_path, assets_root=self.assets_root, capture_guide=engine)
        )
        resp = client.post(
            "/v1/capture/guide",
            json={
                "request_id": "cap-ai-01",
                "target_type": "head",
                "azimuth_span_deg": 180.0,
                "up_roll_median_deg": 81.0,
                "roi_detail_px": 3.0,
            },
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["mode"], "ai")
        self.assertEqual(len(body["guide"]["steps"]), 2)
        log_entry = json.loads(self.log_path.read_text(encoding="utf-8").strip())
        self.assertEqual(log_entry["endpoint"], "capture_guide")
        self.assertEqual(log_entry["request_id"], "cap-ai-01")

    def test_ai_mode_invalid_output_returns_502(self):
        os.environ["OOTD_AI_MODE"] = "ai"
        transport = _mock_transport(
            200,
            {"choices": [{"message": {"content": "not json at all"}}]},
        )
        engine = AICaptureGuide(
            "https://example.invalid",
            "dummy-key",
            "test-model",
            client=httpx.Client(transport=transport),
        )
        client = TestClient(
            create_app(db_path=self.db_path, assets_root=self.assets_root, capture_guide=engine)
        )
        resp = client.post(
            "/v1/capture/guide",
            json={"request_id": "cap-bad-01", "target_type": "garment", "orbit_continuity_min": 0.2},
        )
        self.assertEqual(resp.status_code, 502)

    def test_ai_mode_timeout_returns_503(self):
        os.environ["OOTD_AI_MODE"] = "ai"
        transport = _mock_transport(200, raise_exc=httpx.ReadTimeout("timeout"))
        engine = AICaptureGuide(
            "https://example.invalid",
            "dummy-key",
            "test-model",
            client=httpx.Client(transport=transport),
        )
        client = TestClient(
            create_app(db_path=self.db_path, assets_root=self.assets_root, capture_guide=engine)
        )
        resp = client.post(
            "/v1/capture/guide",
            json={"request_id": "cap-to-01", "target_type": "garment", "orbit_continuity_min": 0.2},
        )
        self.assertEqual(resp.status_code, 503)


if __name__ == "__main__":
    unittest.main()
