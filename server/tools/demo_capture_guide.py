"""CLI demo for Capture Quality Diagnosis + AI Reshoot Guide (/v1/capture/guide).

Runs two realistic scenarios derived directly from project scan experiments
(no private photos or secrets are included):
1. Head scan gap case (based on person-002 baseline + Round 65 findings:
   180° azimuth span, 81° roll deviation, ~3 px single-eye width, 96/1097 frames).
2. Garment scan partial-gap case (based on Round 31 uCO3D samples:
   21 px vertical margin cropping risk, 0.291 orbit continuity dip, missing closeups).

Usage (from repo root, with server/.venv):
    # Mock rule-based mode (default):
    ./server/.venv/Scripts/python.exe server/tools/demo_capture_guide.py

    # Real AI mode (set OOTD_AI_MODE=ai + OOTD_AI_BASE_URL/API_KEY/MODEL in env):
    ./server/.venv/Scripts/python.exe server/tools/demo_capture_guide.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from fastapi.testclient import TestClient

from app import create_app

DEMO_CASES = [
    {
        "request_id": "demo-head-r65",
        "target_type": "head",
        "subject_label": "person-002-r65-baseline",
        "azimuth_span_deg": 180.0,
        "up_roll_median_deg": 81.0,
        "roi_detail_px": 3.0,
        "frame_count": 1097,
        "registered_frames": 96,
        "has_scale_reference": False,
        "missing_closeups": ["左右耳", "后脑", "头顶"],
    },
    {
        "request_id": "demo-garment-r31",
        "target_type": "garment",
        "subject_label": "garment-orbit-sample",
        "covered_azimuth_deg": [0, 30, 60, 90, 120, 150, 240, 270, 300, 330],
        "min_margin_px": {"left": 86, "right": 87, "top": 21, "bottom": 21},
        "sharpness_p50": 22.4,
        "orbit_continuity_min": 0.2907,
        "has_scale_reference": False,
        "missing_closeups": ["领口", "袖口", "腋下"],
    },
]


def main() -> int:
    mode = os.environ.get("OOTD_AI_MODE", "mock")
    print(f"=== OOTD AI 采集质检与补拍指导 Demo (OOTD_AI_MODE={mode}) ===")
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        app = create_app(db_path=tmp_path / "demo.db", assets_root=tmp_path / "assets")
        client = TestClient(app)

        for case in DEMO_CASES:
            t0 = time.perf_counter()
            resp = client.post("/v1/capture/guide", json=case)
            dt = round(time.perf_counter() - t0, 3)
            print(f"\n--- [{case['request_id']}] HTTP {resp.status_code} ({dt}s) ---")
            print(json.dumps(resp.json(), ensure_ascii=False, indent=2))
            if resp.status_code != 200:
                return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
