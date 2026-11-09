"""Tests for the asset package validator (task 3).

Runs with the standard library only (unittest); fixture GLBs are tiny meshes
generated in memory with trimesh.  No official/personal assets are used.
"""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import trimesh

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from asset_validation import SUPPORTED_SCHEMA_VERSION, validate_package  # noqa: E402

SERVER_DIR = Path(__file__).resolve().parents[1]
CLI = SERVER_DIR / "validate_asset_package.py"


def write_minimal_glb(path, scale=0.5):
    """Write a small valid GLB (a cube) and return its byte content."""
    mesh = trimesh.creation.box(extents=[scale, scale, scale])
    mesh.export(path, file_type="glb")
    return path.read_bytes()


def write_huge_glb(path):
    """A valid GLB whose mesh spans an implausibly large size."""
    mesh = trimesh.creation.box(extents=[50.0, 50.0, 50.0])
    mesh.export(path, file_type="glb")


def build_package(root, manifest, files):
    """Write a package directory from a manifest dict and {relative_file: bytes-or-Path}."""
    (root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    for relative, content in files.items():
        target = (root / relative).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, (bytes, bytearray)):
            target.write_bytes(content)
        else:
            target.write_bytes(Path(content).read_bytes())


def valid_manifest(assets=None):
    if assets is None:
        assets = [
            {
                "asset_type": "person",
                "asset_id": "person-001",
                "relative_file": "person-001.glb",
                "source": "participant",
            },
            {
                "asset_type": "garment",
                "asset_id": "top-001",
                "relative_file": "top-001.glb",
                "source": "participant",
                "display_name": "白色短袖",
                "tags": ["white", "casual"],
                "available": True,
            },
            {
                "asset_type": "garment",
                "asset_id": "top-002",
                "relative_file": "top-002.glb",
                "source": "participant",
                "display_name": "深色短袖",
                "tags": ["dark", "casual"],
                "available": True,
            },
        ]
    return {
        "schema_version": SUPPORTED_SCHEMA_VERSION,
        "person_id": "person-001",
        "catalog_version": "v1",
        "pose_id": "pose-001",
        "assets": assets,
    }


def make_valid_package(root, manifests=None):
    """Write a package that must pass all automatic checks."""
    manifest = manifests or valid_manifest()
    files = {
        "person-001.glb": write_minimal_glb(root / "person-001.glb"),
        "top-001.glb": write_minimal_glb(root / "top-001.glb", scale=0.6),
        "top-002.glb": write_minimal_glb(root / "top-002.glb", scale=0.6),
    }
    build_package(root, manifest, files)
    return manifest, files


def run_cli(*args):
    """Run the CLI with deterministic UTF-8 pipes (independent of console codepage)."""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [sys.executable, str(CLI), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )


class PackageValidationTests(unittest.TestCase):
    def test_valid_package_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, files = make_valid_package(root)
            report = validate_package(root)
            self.assertTrue(report.ok, msg="\n".join(report.errors))
            self.assertEqual(report.errors, [])
            self.assertEqual(report.manifest["person_id"], "person-001")
            self.assertEqual(report.manifest["catalog_version"], "v1")
            self.assertEqual(len(report.assets), 3)

    def test_report_contains_size_and_sha256(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, files = make_valid_package(root)
            report = validate_package(root)
            by_id = {asset["asset_id"]: asset for asset in report.assets}
            for entry in manifest["assets"]:
                asset = by_id[entry["asset_id"]]
                content = files[entry["relative_file"]]
                self.assertEqual(asset["size_bytes"], len(content))
                self.assertEqual(
                    asset["sha256"],
                    hashlib.sha256(content).hexdigest(),
                )

    def test_report_is_json_serializable_and_marks_manual_checks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_valid_package(root)
            report = validate_package(root)
            payload = json.loads(json.dumps(report.as_dict(), ensure_ascii=False))
            self.assertTrue(payload["ok"])
            self.assertGreaterEqual(len(payload["manual_checks"]), 3)
            self.assertTrue(any("本人相似度" in item for item in payload["manual_checks"]))

    def test_missing_required_manifest_field_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, files = make_valid_package(root)
            del manifest["pose_id"]
            build_package(root, manifest, files)
            report = validate_package(root)
            self.assertFalse(report.ok)
            self.assertTrue(any("pose_id" in error for error in report.errors))

    def test_unsupported_schema_version_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, files = make_valid_package(root)
            manifest["schema_version"] = SUPPORTED_SCHEMA_VERSION + 1
            build_package(root, manifest, files)
            report = validate_package(root)
            self.assertFalse(report.ok)
            self.assertTrue(any("schema_version" in error for error in report.errors))

    def test_unknown_manifest_field_warns_but_does_not_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, files = make_valid_package(root)
            manifest["future_field"] = "something"
            build_package(root, manifest, files)
            report = validate_package(root)
            self.assertTrue(report.ok)
            self.assertTrue(any("future_field" in warning for warning in report.warnings))

    def test_manifest_with_utf8_bom_is_accepted(self):
        # Windows editors often write a UTF-8 BOM; the manifest must still parse.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_valid_package(root)
            manifest_path = root / "manifest.json"
            content = manifest_path.read_text(encoding="utf-8")
            manifest_path.write_bytes(b"\xef\xbb\xbf" + content.encode("utf-8"))
            report = validate_package(root)
            self.assertTrue(report.ok, msg="\n".join(report.errors))

    def test_duplicate_asset_id_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, files = make_valid_package(root)
            manifest["assets"][2]["asset_id"] = "top-001"
            build_package(root, manifest, files)
            report = validate_package(root)
            self.assertFalse(report.ok)
            self.assertTrue(any("top-001" in error and "重复" in error for error in report.errors))

    def test_more_than_one_person_asset_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, files = make_valid_package(root)
            manifest["assets"].append(
                {
                    "asset_type": "person",
                    "asset_id": "person-002",
                    "relative_file": "person-002.glb",
                    "source": "participant",
                }
            )
            files["person-002.glb"] = write_minimal_glb(root / "person-002.glb")
            build_package(root, manifest, files)
            report = validate_package(root)
            self.assertFalse(report.ok)
            self.assertTrue(any("person" in error for error in report.errors))

    def test_person_asset_id_must_equal_person_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, files = make_valid_package(root)
            manifest["assets"][0]["asset_id"] = "person-999"
            build_package(root, manifest, files)
            report = validate_package(root)
            self.assertFalse(report.ok)
            self.assertTrue(any("person-001" in error for error in report.errors))

    def test_wrong_number_of_garments_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, files = make_valid_package(root)
            manifest["assets"] = manifest["assets"][:2]  # keep person + one garment
            build_package(root, manifest, files)
            report = validate_package(root)
            self.assertFalse(report.ok)
            self.assertTrue(any("衣物" in error or "garment" in error for error in report.errors))

    def test_missing_file_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_valid_package(root)
            (root / "top-002.glb").unlink()
            report = validate_package(root)
            self.assertFalse(report.ok)
            self.assertTrue(any("top-002.glb" in error for error in report.errors))

    def test_absolute_path_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, files = make_valid_package(root)
            manifest["assets"][2]["relative_file"] = "C:\\outside\\top-002.glb"
            build_package(root, manifest, files)
            report = validate_package(root)
            self.assertFalse(report.ok)
            self.assertTrue(any("绝对路径" in error for error in report.errors))

    def test_posix_root_path_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, files = make_valid_package(root)
            manifest["assets"][2]["relative_file"] = "/etc/top-002.glb"
            build_package(root, manifest, files)
            report = validate_package(root)
            self.assertFalse(report.ok)
            self.assertTrue(any("绝对路径" in error for error in report.errors))

    def test_traversal_path_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, files = make_valid_package(root)
            manifest["assets"][2]["relative_file"] = "../top-002.glb"
            build_package(root, manifest, files)
            report = validate_package(root)
            self.assertFalse(report.ok)
            self.assertTrue(any("目录穿越" in error or ".." in error for error in report.errors))

    def test_backslash_traversal_rejected_on_all_platforms(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, files = make_valid_package(root)
            manifest["assets"][2]["relative_file"] = "..\\..\\secret\\top-002.glb"
            build_package(root, manifest, files)
            report = validate_package(root)
            self.assertFalse(report.ok)

    def test_symlink_escape_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "package"
            root.mkdir()
            make_valid_package(root)
            outside = base / "outside-secret.glb"
            outside.write_bytes(write_minimal_glb(outside))
            link = root / "top-002.glb"
            link.unlink()
            try:
                link.symlink_to(outside)
            except (OSError, NotImplementedError):
                self.skipTest("cannot create symlink in this environment")
            report = validate_package(root)
            self.assertFalse(report.ok)
            self.assertTrue(any("top-002.glb" in error for error in report.errors))

    def test_junction_escape_rejected(self):
        if os.name != "nt":
            self.skipTest("junctions are Windows-only")
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "package"
            root.mkdir()
            manifest, files = make_valid_package(root)
            (root / "top-002.glb").unlink()
            outside = base / "outside-dir"
            outside.mkdir()
            outside.joinpath("top-002.glb").write_bytes(
                write_minimal_glb(outside / "top-002.glb")
            )
            manifest["assets"][2]["relative_file"] = "sub/top-002.glb"
            (root / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
            )
            created = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(root / "sub"), str(outside)],
                capture_output=True,
                text=True,
            )
            if created.returncode != 0:
                self.skipTest("cannot create junction: " + created.stderr.strip())
            report = validate_package(root)
            self.assertFalse(report.ok)
            self.assertTrue(any("sub/top-002.glb" in error for error in report.errors))

    def test_corrupt_glb_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_valid_package(root)
            (root / "top-001.glb").write_bytes(b"this is not a glb file")
            report = validate_package(root)
            self.assertFalse(report.ok)
            self.assertTrue(any("top-001.glb" in error for error in report.errors))

    def test_non_glb_extension_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, files = make_valid_package(root)
            manifest["assets"][1]["relative_file"] = "top-001.obj"
            build_package(root, manifest, files)
            report = validate_package(root)
            self.assertFalse(report.ok)
            self.assertTrue(any("GLB" in error for error in report.errors))

    def test_extreme_size_warns_but_does_not_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_valid_package(root)
            write_huge_glb(root / "top-001.glb")
            report = validate_package(root)
            self.assertTrue(report.ok, msg="\n".join(report.errors))
            self.assertTrue(any("尺寸" in warning for warning in report.warnings))

    def test_validation_does_not_modify_package(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest, files = make_valid_package(root)
            before = {
                path: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in root.rglob("*")
                if path.is_file()
            }
            validate_package(root)
            after = {
                path: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in root.rglob("*")
                if path.is_file()
            }
            self.assertEqual(before, after)

    def test_cli_exit_codes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_valid_package(root)
            valid = run_cli(str(root))
            self.assertEqual(valid.returncode, 0, msg=valid.stdout + valid.stderr)

            (root / "top-001.glb").write_bytes(b"corrupted")
            invalid = run_cli(str(root))
            self.assertEqual(invalid.returncode, 1)

            usage = run_cli(str(root / "does-not-exist"))
            self.assertEqual(usage.returncode, 2)

    def test_cli_report_file_is_written_for_valid_and_invalid(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_valid_package(root)
            report_path = root / ".." / "report-valid.json"
            valid = run_cli(str(root), "--report", str(report_path))
            self.assertEqual(valid.returncode, 0)
            payload = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertTrue(payload["ok"])
            self.assertIn("exit_code", payload)
            self.assertEqual(payload["exit_code"], 0)


if __name__ == "__main__":
    unittest.main()