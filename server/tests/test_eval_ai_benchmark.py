"""Tests for the 30+20 AI benchmark evaluation tool (server/tools/eval_ai_benchmark.py)."""

import sys
import unittest
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from eval_ai_benchmark import (  # noqa: E402
    CAPTURE_GUIDE_CASES,
    RECOMMEND_CASES,
    _check_recommend_result,
    format_markdown,
    run_benchmark,
)


class EvalBenchmarkTests(unittest.TestCase):
    def test_case_counts_and_unique_ids(self):
        self.assertEqual(len(RECOMMEND_CASES), 30)
        self.assertEqual(len(CAPTURE_GUIDE_CASES), 20)
        rec_ids = [c["case_id"] for c in RECOMMEND_CASES]
        cap_ids = [c["payload"]["request_id"] for c in CAPTURE_GUIDE_CASES]
        self.assertEqual(len(set(rec_ids)), 30)
        self.assertEqual(len(set(cap_ids)), 20)

    def test_mock_benchmark_end_to_end_and_markdown(self):
        report = run_benchmark()
        rs = report["recommendations_summary"]
        cs = report["capture_guide_summary"]
        self.assertEqual(rs["total"], 30)
        self.assertEqual(rs["http_200_count"], 30)
        self.assertEqual(rs["valid_return_rate"], 1.0)
        self.assertEqual(rs["condition_violation_rate"], 0.0)
        self.assertEqual(cs["total"], 20)
        self.assertEqual(cs["http_200_count"], 20)
        self.assertEqual(cs["valid_return_rate"], 1.0)
        self.assertEqual(cs["condition_violation_rate"], 0.0)

        md = format_markdown(report)
        self.assertIn("30/30 (100.0%)", md)
        self.assertIn("20/20 (100.0%)", md)

    def test_violation_detection_flags_unknown_id_and_wrong_choice(self):
        valid_ids = {"top-001", "top-002", "top-003"}
        case = {"expected_status": "ok", "allowed_ids": ["top-002"]}
        p_ok, c_ok, _ = _check_recommend_result(case, {"status": "ok", "garment_id": "top-999", "reason": "bad"}, valid_ids, "ai")
        self.assertFalse(p_ok)
        self.assertFalse(c_ok)
        p_ok2, c_ok2, _ = _check_recommend_result(case, {"status": "ok", "garment_id": "top-001", "reason": "wrong tag"}, valid_ids, "ai")
        self.assertTrue(p_ok2)
        self.assertFalse(c_ok2)


if __name__ == "__main__":
    unittest.main()
