"""Tests for the minimal FastAPI + SQLite server (task 4).

Covered: health, catalog, asset download, mock recommendations, the unified
error body, import semantics (idempotent same-content, conflict on same
version with different content, invalid packages rejected), and bearer-token
auth.  Uses TestClient with temporary DB and asset storage.
"""

import json
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # server dir
sys.path.insert(0, str(Path(__file__).resolve().parent))  # tests dir

from test_asset_validation import make_valid_package, valid_manifest, write_minimal_glb  # noqa: E402

import db  # noqa: E402
from app import create_app  # noqa: E402
from importer import PackageInvalidError, VersionConflictError, import_package  # noqa: E402


class ServerTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db_path = self.root / "test.db"
        self.assets_root = self.root / "assets"
        self.app = create_app(
            db_path=self.db_path, assets_root=self.assets_root, token=None
        )
        self.client = TestClient(self.app)
        self.conn = db.connect(self.db_path)
        db.init_db(self.conn)

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def publish(self, version="v1"):
        package = self.root / f"package-{version}"
        package.mkdir()
        manifest = valid_manifest()
        manifest["catalog_version"] = version
        manifest, files = make_valid_package(package, manifests=manifest)
        result = import_package(self.conn, package, self.assets_root)
        return package, manifest, files, result

    def recommend(self, preference, version="v1", person="person-001", **extra):
        payload = {
            "request_id": "request-test-1",
            "person_id": person,
            "catalog_version": version,
            "scene": "下午上课",
            "preference": preference,
        }
        payload.update(extra)
        return self.client.post("/v1/recommendations", json=payload)


class HealthAndCatalogTests(ServerTestCase):
    def test_health_ok(self):
        response = self.client.get("/v1/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")

    def test_catalog_unknown_person_returns_404_unified(self):
        response = self.client.get("/v1/catalog", params={"person_id": "nobody"})
        self.assertEqual(response.status_code, 404)
        body = response.json()
        self.assertEqual(body["error"]["code"], "not_found")
        self.assertIn("nobody", body["error"]["message"])
        self.assertEqual(body["error"]["request_id"], None)

    def test_catalog_without_person_id_returns_422_unified(self):
        response = self.client.get("/v1/catalog")
        self.assertEqual(response.status_code, 422)
        body = response.json()
        self.assertEqual(body["error"]["code"], "validation_error")

    def test_published_catalog_lists_assets_with_url_size_sha(self):
        package, manifest, files, result = self.publish("v1")
        response = self.client.get("/v1/catalog", params={"person_id": "person-001"})
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["person_id"], "person-001")
        self.assertEqual(payload["catalog_version"], "v1")
        self.assertEqual(payload["pose_id"], "pose-001")
        self.assertEqual(len(payload["assets"]), 3)
        by_id = {asset["asset_id"]: asset for asset in payload["assets"]}
        top_001 = by_id["top-001"]
        self.assertEqual(top_001["asset_url"], "/v1/assets/person-001/v1/top-001")
        self.assertEqual(top_001["size_bytes"], len(files["top-001.glb"]))
        self.assertIsInstance(top_001["sha256"], str)
        self.assertEqual(top_001["display_name"], "白色短袖")
        self.assertTrue(top_001["available"])
        self.assertIn("white", top_001["tags"])

    def test_catalog_returns_latest_version(self):
        self.publish("v1")
        self.publish("v2")
        response = self.client.get("/v1/catalog", params={"person_id": "person-001"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["catalog_version"], "v2")

    def test_asset_download_returns_exact_glb_bytes(self):
        package, manifest, files, result = self.publish("v1")
        response = self.client.get("/v1/assets/person-001/v1/top-001")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, files["top-001.glb"])
        self.assertEqual(response.headers["content-type"], "model/gltf-binary")

    def test_asset_unknown_id_returns_404(self):
        self.publish("v1")
        response = self.client.get("/v1/assets/person-001/v1/does-not-exist")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "not_found")

    def test_asset_from_unpublished_version_returns_404(self):
        self.publish("v1")
        response = self.client.get("/v1/assets/person-001/v9/top-001")
        self.assertEqual(response.status_code, 404)


class RecommendationTests(ServerTestCase):
    def setUp(self):
        super().setUp()
        self.publish("v1")

    def test_recommendation_ok_echoes_and_returns_garment(self):
        response = self.recommend("简洁，优先浅色")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["request_id"], "request-test-1")
        self.assertEqual(payload["person_id"], "person-001")
        self.assertEqual(payload["catalog_version"], "v1")
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["garment_id"], "top-001")
        self.assertEqual(payload["mode"], "mock")
        self.assertTrue(payload["reason"])

    def test_recommendation_dark_preference_selects_dark_garment(self):
        response = self.recommend("深色")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["garment_id"], "top-002")

    def test_recommendation_no_match_returns_200_null(self):
        response = self.recommend("要红色上衣")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "no_match")
        self.assertIsNone(payload["garment_id"])
        self.assertTrue(payload["reason"])

    def test_recommendation_missing_fields_returns_422(self):
        response = self.client.post(
            "/v1/recommendations",
            json={"request_id": "r", "person_id": "person-001", "catalog_version": "v1"},
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"]["code"], "validation_error")

    def test_recommendation_unknown_person_returns_404(self):
        response = self.recommend("浅色", person="nobody")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "not_found")

    def test_recommendation_stale_version_returns_409(self):
        self.publish("v2")
        response = self.recommend("浅色", version="v1")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "version_conflict")
        fresh = self.recommend("浅色", version="v2")
        self.assertEqual(fresh.status_code, 200)
        self.assertEqual(fresh.json()["garment_id"], "top-001")

    def test_disabled_garment_is_never_recommended(self):
        manifest = valid_manifest()
        manifest["catalog_version"] = "v2"
        manifest["assets"][2]["available"] = False
        package = self.root / "limited"
        package.mkdir()
        make_valid_package(package, manifests=manifest)
        import_package(self.conn, package, self.assets_root)
        dark = self.recommend("深色", version="v2")
        self.assertEqual(dark.status_code, 200)
        self.assertEqual(dark.json()["status"], "no_match")
        light = self.recommend("浅色", version="v2")
        self.assertEqual(light.status_code, 200)
        self.assertEqual(light.json()["garment_id"], "top-001")


class AuthTests(ServerTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.assets_root = self.root / "assets"
        self.app = create_app(
            db_path=self.root / "test.db",
            assets_root=self.assets_root,
            token="secret-token",
        )
        self.client = TestClient(self.app)
        self.conn = db.connect(self.root / "test.db")
        db.init_db(self.conn)

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def test_unauthorized_request_rejected(self):
        response = self.client.get("/v1/catalog", params={"person_id": "person-001"})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "unauthorized")

    def test_valid_token_accepted(self):
        self.publish("v1")
        response = self.client.get(
            "/v1/catalog",
            params={"person_id": "person-001"},
            headers={"Authorization": "Bearer secret-token"},
        )
        self.assertEqual(response.status_code, 200)

    def test_wrong_token_rejected(self):
        self.publish("v1")
        response = self.client.get(
            "/v1/catalog",
            params={"person_id": "person-001"},
            headers={"Authorization": "Bearer wrong"},
        )
        self.assertEqual(response.status_code, 401)


class ImportSemanticsTests(ServerTestCase):
    def test_invalid_directory_ids_are_rejected(self):
        for index, field in enumerate(("person_id", "catalog_version", "pose_id")):
            with self.subTest(field=field):
                package = self.root / f"bad-id-{index}"
                package.mkdir()
                manifest = valid_manifest()
                manifest[field] = "../outside"
                if field == "person_id":
                    manifest["assets"][0]["asset_id"] = manifest[field]
                make_valid_package(package, manifests=manifest)
                with self.assertRaises(PackageInvalidError):
                    import_package(self.conn, package, self.assets_root)
        self.assertFalse((self.root / "outside").exists())

    def test_invalid_asset_id_type_is_reported(self):
        package = self.root / "bad-asset-id"
        package.mkdir()
        manifest = valid_manifest()
        manifest["assets"][1]["asset_id"] = ["unexpected"]
        make_valid_package(package, manifests=manifest)
        with self.assertRaises(PackageInvalidError):
            import_package(self.conn, package, self.assets_root)

    def test_existing_unregistered_directory_is_preserved(self):
        package = self.root / "existing-target"
        package.mkdir()
        make_valid_package(package)
        destination = self.assets_root / "person-001" / "v1"
        destination.mkdir(parents=True)
        marker = destination / "keep.txt"
        marker.write_text("existing data")
        with self.assertRaises(VersionConflictError):
            import_package(self.conn, package, self.assets_root)
        self.assertEqual(marker.read_text(), "existing data")

    def test_copy_failure_preserves_published_assets(self):
        self.publish("v1")
        first = self.assets_root / "person-001" / "v1" / "person-001.glb"
        original = first.read_bytes()
        package = self.root / "copy-failure"
        package.mkdir()
        manifest = valid_manifest()
        manifest["catalog_version"] = "v2"
        make_valid_package(package, manifests=manifest)
        with patch("importer.shutil.copy2", side_effect=OSError("copy failed")):
            with self.assertRaises(OSError):
                import_package(self.conn, package, self.assets_root)
        self.assertEqual(first.read_bytes(), original)
        self.assertIsNone(db.get_version(self.conn, "person-001", "v2"))
        self.assertFalse(list(self.assets_root.glob(".import-*")))

    def test_changed_source_during_copy_is_rejected(self):
        package = self.root / "changed-source"
        package.mkdir()
        make_valid_package(package)
        def corrupt_copy(source, target):
            Path(target).write_bytes(b"changed after validation")
        with patch("importer.shutil.copy2", side_effect=corrupt_copy):
            with self.assertRaises(PackageInvalidError):
                import_package(self.conn, package, self.assets_root)
        self.assertIsNone(db.get_version(self.conn, "person-001", "v1"))
        self.assertFalse((self.assets_root / "person-001" / "v1").exists())

    def test_concurrent_imports_reuse_published_version(self):
        package = self.root / "concurrent"
        package.mkdir()
        make_valid_package(package)
        def publish(_):
            connection = db.connect(self.root / "test.db")
            try:
                return import_package(connection, package, self.assets_root)
            finally:
                connection.close()
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(publish, range(2)))
        self.assertEqual(sum(result.published for result in results), 1)
        self.assertEqual(sum(result.reused for result in results), 1)
        self.assertTrue((self.assets_root / "person-001" / "v1" / "person-001.glb").is_file())

    def test_same_version_same_content_is_idempotent(self):
        package, manifest, files, first = self.publish("v1")
        self.assertTrue(first.published)
        second = import_package(self.conn, package, self.assets_root)
        self.assertTrue(second.reused)
        rows = self.conn.execute("SELECT COUNT(*) FROM catalog_versions").fetchone()[0]
        self.assertEqual(rows, 1)

    def test_same_version_different_content_is_rejected_without_changes(self):
        package, manifest, files, first = self.publish("v1")
        (package / "top-001.glb").write_bytes(write_minimal_glb(package / "top-001.glb", scale=0.9))
        before = self.conn.execute(
            "SELECT asset_id FROM assets WHERE person_id=? AND catalog_version=? ORDER BY asset_id",
            ("person-001", "v1"),
        ).fetchall()
        with self.assertRaises(VersionConflictError):
            import_package(self.conn, package, self.assets_root)
        after = self.conn.execute(
            "SELECT asset_id FROM assets WHERE person_id=? AND catalog_version=? ORDER BY asset_id",
            ("person-001", "v1"),
        ).fetchall()
        self.assertEqual(before, after)

    def test_invalid_package_leaves_nothing_published(self):
        package = self.root / "broken"
        package.mkdir()
        manifest = valid_manifest()
        make_valid_package(package, manifests=manifest)
        (package / "top-002.glb").unlink()
        with self.assertRaises(PackageInvalidError):
            import_package(self.conn, package, self.assets_root)
        response = self.client.get("/v1/catalog", params={"person_id": "person-001"})
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
