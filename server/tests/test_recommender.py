"""Tests for the replaceable AI recommender adapter (task 5).

Covers prompt building, strict output validation, provider call handling
(success / no_match / timeout / invalid JSON / unknown ID), endpoint
integration with mode=ai, rate/cost limits, and the call log.  All provider
interactions use a fake OpenAI-compatible server (httpx.MockTransport):
these are *simulated* model results, never real model outcomes.
"""

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # server dir
sys.path.insert(0, str(Path(__file__).resolve().parent))  # tests dir

from test_asset_validation import valid_manifest, write_minimal_glb  # noqa: E402
from test_server import ServerTestCase  # noqa: E402
from test_asset_validation import make_valid_package  # noqa: E402

import db  # noqa: E402
from app import create_app  # noqa: E402
from importer import import_package  # noqa: E402
from recommender import (  # noqa: E402
    AIModelRecommender,
    InvalidModelOutputError,
    RecommenderTimeoutError,
    build_prompt,
    check_daily_cap,
    check_minute_cap,
    validate_model_output,
)

CANDIDATES = [
    {"asset_id": "top-001", "display_name": "白色短袖", "tags": ["white", "casual"]},
    {"asset_id": "top-002", "display_name": "深色短袖", "tags": ["dark", "casual"]},
]
IDS = {c["asset_id"] for c in CANDIDATES}


def fake_transport(content="") -> httpx.MockTransport:
    """A fake OpenAI-compatible /chat/completions server for tests."""
    payload = content if isinstance(content, dict) else {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 12, "completion_tokens": 6, "total_tokens": 18},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    return httpx.MockTransport(handler)


def make_recommender(content="", transport=None) -> AIModelRecommender:
    if transport is None:
        transport = fake_transport(content)
    return AIModelRecommender(
        base_url="http://fake-model.invalid",
        api_key="test-key",
        model="test-model",
        client=httpx.Client(transport=transport),
    )


class PromptAndValidationTests(unittest.TestCase):
    def test_prompt_carries_only_ids_tags_and_io(self):
        messages = build_prompt(CANDIDATES, "下午上课", "简洁，优先浅色")
        system = messages[0]["content"]
        user = messages[1]["content"]
        self.assertIn("下午上课", user)
        self.assertIn("简洁，优先浅色", user)
        self.assertIn("top-001", user)
        self.assertIn("白色短袖", user)
        self.assertIn("garment_id", system)
        # no photos / person data may enter the model input
        for banned in ("photo", "image", "photo/", "person_id", "scene/", "bytes"):
            self.assertNotIn(banned, user.lower())
        # candidates must not be extensible by the model
        self.assertIn("清单", system)

    def test_validate_model_output_ok(self):
        ok, result = validate_model_output(
            json.dumps({"garment_id": "top-001", "reason": "浅色合适"}), IDS
        )
        self.assertTrue(ok)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["garment_id"], "top-001")

    def test_validate_model_output_no_match(self):
        ok, result = validate_model_output(json.dumps({"garment_id": None, "reason": "没有合适"}), IDS)
        self.assertTrue(ok)
        self.assertEqual(result["status"], "no_match")
        self.assertIsNone(result["garment_id"])

    def test_validate_unknown_id_fails(self):
        ok, result = validate_model_output(json.dumps({"garment_id": "top-999", "reason": "x"}), IDS)
        self.assertFalse(ok)
        self.assertIn("top-999", result["error"])

    def test_validate_invalid_json_fails(self):
        ok, result = validate_model_output("not json at all", IDS)
        self.assertFalse(ok)

    def test_validate_missing_reason_fails(self):
        ok, result = validate_model_output(json.dumps({"garment_id": "top-001"}), IDS)
        self.assertFalse(ok)

    def test_validate_missing_garment_id_key_fails(self):
        # R06：缺 garment_id 键是协议错误，不能当成 no_match。
        ok, result = validate_model_output(json.dumps({"reason": "没有 ID 键"}), IDS)
        self.assertFalse(ok)
        self.assertIn("garment_id", result["error"])

    def test_validate_wrong_type_garment_id_fails(self):
        ok, result = validate_model_output(json.dumps({"garment_id": 123, "reason": "数字 ID"}), IDS)
        self.assertFalse(ok)
        self.assertIn("类型", result["error"])

    def test_validate_null_with_empty_reason_fails(self):
        ok, result = validate_model_output(json.dumps({"garment_id": None, "reason": "   "}), IDS)
        self.assertFalse(ok)

    def test_validate_extra_keys_tolerated(self):
        ok, result = validate_model_output(
            json.dumps({"garment_id": "top-002", "reason": "深色", "confidence": 0.9}), IDS
        )
        self.assertTrue(ok)

    def test_daily_cap_counts_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = db.connect(Path(tmp) / "t.db")
            db.init_db(conn)
            try:
                check_daily_cap(conn, "2026-11-09", cap=2)
                check_daily_cap(conn, "2026-11-09", cap=2)
                with self.assertRaises(Exception):
                    check_daily_cap(conn, "2026-11-09", cap=2)
            finally:
                conn.close()

    def test_minute_cap_sliding_window(self):
        stamps: list[float] = []
        check_minute_cap(stamps, time.time(), cap=2)
        check_minute_cap(stamps, time.time(), cap=2)
        with self.assertRaises(Exception):
            check_minute_cap(stamps, time.time(), cap=2)
        # old stamps fall out of the window
        stamps[:] = [time.time() - 120]
        check_minute_cap(stamps, time.time(), cap=2)

    def test_default_timeout_is_shorter_than_client(self):
        # R10：服务端默认 12s，必须短于 Android 的 20s 读超时。
        recommender = AIModelRecommender(base_url="http://x", api_key="k", model="m")
        self.assertEqual(recommender.timeout_seconds, 12.0)
        self.assertLess(recommender.timeout_seconds, 20.0)

    def test_daily_cap_reservation_visible_across_connections(self):
        # R10：占用额度写进数据库，另一个连接立刻能看到，临界额度不超用。
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.db"
            conn1 = db.connect(path)
            db.init_db(conn1)
            conn2 = db.connect(path)
            db.init_db(conn2)
            try:
                check_daily_cap(conn1, "2026-11-09", cap=1)
                with self.assertRaises(Exception):
                    check_daily_cap(conn2, "2026-11-09", cap=1)
            finally:
                conn1.close()
                conn2.close()


class ModelCallTests(unittest.TestCase):
    def test_ok_call_captures_usage_and_latency(self):
        recommender = make_recommender(
            content=json.dumps({"garment_id": "top-001", "reason": "浅色合适"})
        )
        call = recommender.recommend(CANDIDATES, "下午上课", "浅色")
        self.assertEqual(call.status, "ok")
        self.assertEqual(call.garment_id, "top-001")
        self.assertEqual(call.usage["prompt_tokens"], 12)
        self.assertGreater(call.latency_seconds, 0)

    def test_no_match_call(self):
        recommender = make_recommender(
            content=json.dumps({"garment_id": None, "reason": "没有"})
        )
        call = recommender.recommend(CANDIDATES, "晚会", "红色")
        self.assertEqual(call.status, "no_match")
        self.assertIsNone(call.garment_id)

    def test_timeout_raises(self):
        def timeout_handler(request):
            raise httpx.ConnectTimeout("timed out")

        recommender = make_recommender(transport=httpx.MockTransport(timeout_handler))
        with self.assertRaises(RecommenderTimeoutError):
            recommender.recommend(CANDIDATES, "x", "x")

    def test_invalid_json_raises(self):
        recommender = make_recommender(content="not json at all")
        with self.assertRaises(InvalidModelOutputError):
            recommender.recommend(CANDIDATES, "x", "x")

    def test_unknown_id_raises(self):
        recommender = make_recommender(
            content=json.dumps({"garment_id": "top-999", "reason": "x"})
        )
        with self.assertRaises(InvalidModelOutputError):
            recommender.recommend(CANDIDATES, "x", "x")


class AiEndpointTests(ServerTestCase):
    def setUp(self):
        super().setUp()  # db/app/client/conn ready; mode not set -> mock by default
        self.publish("v1")
        self.recommender = make_recommender(
            content=json.dumps({"garment_id": "top-001", "reason": "浅色合适"})
        )
        self.call_log = self.root / "calls.jsonl"
        os.environ["OOTD_AI_MODE"] = "ai"
        os.environ["OOTD_CALL_LOG"] = str(self.call_log)
        os.environ.pop("OOTD_AI_BASE_URL", None)
        os.environ.pop("OOTD_AI_API_KEY", None)
        os.environ.pop("OOTD_AI_MODEL", None)
        self.app = create_app(
            db_path=self.db_path,
            assets_root=self.assets_root,
            recommender=self.recommender,
        )
        self.client = TestClient(self.app)

    def tearDown(self):
        super().tearDown()
        for key in ("OOTD_AI_MODE", "OOTD_CALL_LOG",
                    "OOTD_AI_MAX_CALLS_PER_DAY", "OOTD_AI_MAX_CALLS_PER_MINUTE"):
            os.environ.pop(key, None)

    def test_ai_success_returns_mode_ai(self):
        response = self.recommend("浅色")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["mode"], "ai")
        self.assertEqual(payload["garment_id"], "top-001")
        self.assertEqual(payload["status"], "ok")

    def test_ai_no_match_returns_200_null(self):
        self.recommender = make_recommender(
            content=json.dumps({"garment_id": None, "reason": "没有"})
        )
        self.app = create_app(
            db_path=self.db_path, assets_root=self.assets_root, recommender=self.recommender
        )
        self.client = TestClient(self.app)
        response = self.recommend("红色")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "no_match")
        self.assertIsNone(response.json()["garment_id"])
        self.assertEqual(response.json()["mode"], "ai")

    def test_ai_timeout_returns_503(self):
        def timeout_handler(request):
            raise httpx.ConnectTimeout("timed out")

        self.recommender = make_recommender(transport=httpx.MockTransport(timeout_handler))
        self.app = create_app(
            db_path=self.db_path, assets_root=self.assets_root, recommender=self.recommender
        )
        self.client = TestClient(self.app)
        response = self.recommend("浅色")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "service_unavailable")

    def test_ai_invalid_output_returns_502(self):
        self.recommender = make_recommender(content="not json at all")
        self.app = create_app(
            db_path=self.db_path, assets_root=self.assets_root, recommender=self.recommender
        )
        self.client = TestClient(self.app)
        response = self.recommend("浅色")
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["error"]["code"], "bad_gateway")

    def test_ai_unknown_id_returns_502(self):
        self.recommender = make_recommender(
            content=json.dumps({"garment_id": "top-999", "reason": "x"})
        )
        self.app = create_app(
            db_path=self.db_path, assets_root=self.assets_root, recommender=self.recommender
        )
        self.client = TestClient(self.app)
        response = self.recommend("浅色")
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["error"]["code"], "bad_gateway")

    def test_ai_missing_garment_id_key_returns_502(self):
        # R06：模型输出缺 garment_id 键必须报协议错误（502），不是 no_match。
        self.recommender = make_recommender(
            content=json.dumps({"reason": "忘了给 ID"})
        )
        self.app = create_app(
            db_path=self.db_path, assets_root=self.assets_root, recommender=self.recommender
        )
        self.client = TestClient(self.app)
        response = self.recommend("浅色")
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["error"]["code"], "bad_gateway")

    def test_ai_wrong_type_garment_id_returns_502(self):
        self.recommender = make_recommender(
            content=json.dumps({"garment_id": 123, "reason": "数字 ID"})
        )
        self.app = create_app(
            db_path=self.db_path, assets_root=self.assets_root, recommender=self.recommender
        )
        self.client = TestClient(self.app)
        response = self.recommend("浅色")
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["error"]["code"], "bad_gateway")

    def test_ai_timeout_logs_failure_with_latency(self):
        # R10：超时也要落一条日志，带 status=timeout 和耗时。
        def timeout_handler(request):
            raise httpx.ConnectTimeout("timed out")

        self.recommender = make_recommender(transport=httpx.MockTransport(timeout_handler))
        self.app = create_app(
            db_path=self.db_path, assets_root=self.assets_root, recommender=self.recommender
        )
        self.client = TestClient(self.app)
        response = self.recommend("浅色")
        self.assertEqual(response.status_code, 503)
        lines = self.call_log.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 1)
        entry = json.loads(lines[0])
        self.assertEqual(entry["status"], "timeout")
        self.assertIsNone(entry["garment_id"])
        self.assertIn("latency_seconds", entry)

    def test_ai_disabled_garment_is_rejected_502(self):
        manifest = valid_manifest()
        manifest["catalog_version"] = "v2"
        manifest["assets"][2]["available"] = False
        package = self.root / "limited"
        package.mkdir()
        make_valid_package(package, manifests=manifest)
        import_package(self.conn, package, self.assets_root)
        self.recommender = make_recommender(
            content=json.dumps({"garment_id": "top-002", "reason": "x"})
        )
        self.app = create_app(
            db_path=self.db_path, assets_root=self.assets_root, recommender=self.recommender
        )
        self.client = TestClient(self.app)
        response = self.recommend("深色", version="v2")
        self.assertEqual(response.status_code, 502)

    def test_ai_missing_config_returns_503(self):
        self.app = create_app(db_path=self.db_path, assets_root=self.assets_root, recommender=None)
        self.client = TestClient(self.app)
        response = self.recommend("浅色")
        self.assertEqual(response.status_code, 503)
        self.assertIn("未配置", response.json()["error"]["message"])

    def test_daily_cap_blocks_and_call_log_records(self):
        os.environ["OOTD_AI_MAX_CALLS_PER_DAY"] = "1"
        first = self.recommend("浅色")
        self.assertEqual(first.status_code, 200)
        second = self.recommend("深色")
        self.assertEqual(second.status_code, 503)
        self.assertEqual(second.json()["error"]["code"], "service_unavailable")
        lines = self.call_log.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 1, msg="blocked calls must not enter the model log")
        entry = json.loads(lines[0])
        self.assertEqual(entry["mode"], "ai")
        self.assertEqual(entry["garment_id"], "top-001")
        self.assertIn("latency_seconds", entry)
        self.assertIn("usage", entry)
        self.assertIn("request_id", entry)


if __name__ == "__main__":
    unittest.main()