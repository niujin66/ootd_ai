"""Whole-package transactional import (task 4).

A package must pass automatic validation first.  Publishing is all-or-nothing:
a failed import leaves the previously published versions untouched.  Repeating
the same version with identical content is a no-op; the same version with
different content is rejected and a new version number is required.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import time
from pathlib import Path

import db
from asset_validation import validate_package


class PackageInvalidError(Exception):
    """The package failed automatic validation; nothing was published."""


class VersionConflictError(Exception):
    """The version already exists with different content; publish a new version."""


class ImportResult:
    def __init__(self, published=False, reused=False, person_id=None, catalog_version=None, storage_dir=None):
        self.published = published
        self.reused = reused
        self.person_id = person_id
        self.catalog_version = catalog_version
        self.storage_dir = storage_dir


def _content_digest(report) -> str:
    """Content identity: canonical manifest + per-asset file hashes."""
    digest = hashlib.sha256()
    digest.update(
        json.dumps(report.manifest, sort_keys=True, ensure_ascii=False).encode("utf-8")
    )
    for asset in sorted(report.assets, key=lambda item: item.get("relative_file") or ""):
        relative = asset.get("relative_file")
        sha256 = asset.get("sha256")
        if relative and sha256:
            digest.update(relative.encode("utf-8"))
            digest.update(sha256.encode("ascii"))
    return digest.hexdigest()


def _asset_meta(manifest: dict, record: dict) -> dict:
    """Merge the validation record with the manifest entry for one asset."""
    entry = next(
        (item for item in manifest["assets"] if item.get("asset_id") == record.get("asset_id")),
        {},
    )
    tags = entry.get("tags")
    return {
        "asset_id": record.get("asset_id"),
        "asset_type": record.get("asset_type") or entry.get("asset_type") or "",
        "relative_file": record.get("relative_file") or "",
        "source": entry.get("source") or "",
        "display_name": entry.get("display_name"),
        "tags_json": json.dumps(tags if isinstance(tags, list) else [], ensure_ascii=False),
        "available": 1 if entry.get("available") else 0,
        "size_bytes": record.get("size_bytes") or 0,
        "sha256": record.get("sha256") or "",
    }


def import_package(conn, package_dir, assets_root, expected_garments: int = 2) -> ImportResult:
    """Validate and publish one package.  Raises on failure without side effects."""
    report = validate_package(package_dir, expected_garments=expected_garments)
    if not report.ok:
        raise PackageInvalidError("\n".join(report.errors))

    manifest = report.manifest
    person_id = manifest["person_id"]
    catalog_version = manifest["catalog_version"]
    pose_id = manifest.get("pose_id")
    digest = _content_digest(report)

    root = Path(assets_root).resolve()
    destination = (root / person_id / catalog_version).resolve()
    if not destination.is_relative_to(root) or destination == root:
        raise PackageInvalidError("目标目录必须位于资产存储目录内")
    staging = None
    created_destination = False
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = db.get_version(conn, person_id, catalog_version)
        if existing is not None:
            if existing["content_digest"] != digest:
                raise VersionConflictError(f"版本 {catalog_version} 已发布但内容不同；请使用新版本号")
            conn.execute("COMMIT")
            return ImportResult(reused=True, person_id=person_id, catalog_version=catalog_version)
        if destination.exists():
            raise VersionConflictError("目标目录已存在但尚未登记，请检查后使用新版本号")
        root.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".import-", dir=root)).resolve()
        for record in report.assets:
            if not record.get("sha256") or not record.get("relative_file"):
                continue
            source = Path(package_dir) / record["relative_file"]
            target = staging / record["relative_file"]
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            if hashlib.sha256(target.read_bytes()).hexdigest() != record["sha256"]:
                raise PackageInvalidError("源资产在校验后发生变化，请重新导入")

        destination.parent.mkdir(parents=True, exist_ok=True)
        staging.rename(destination)
        staging = None
        created_destination = True

        published_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        conn.execute(
            "INSERT INTO catalog_versions"
            " (person_id, catalog_version, pose_id, content_digest, storage_dir, published_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (person_id, catalog_version, pose_id, digest, str(destination), published_at),
        )
        for record in report.assets:
            if not record.get("sha256"):
                continue
            meta = _asset_meta(manifest, record)
            conn.execute(
                "INSERT INTO assets"
                " (person_id, catalog_version, asset_id, asset_type, relative_file, source,"
                "  display_name, tags, available, size_bytes, sha256)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    person_id,
                    catalog_version,
                    meta["asset_id"],
                    meta["asset_type"],
                    meta["relative_file"],
                    meta["source"],
                    meta["display_name"],
                    meta["tags_json"],
                    meta["available"],
                    meta["size_bytes"],
                    meta["sha256"],
                ),
            )
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        cleanup = destination if created_destination else staging
        if cleanup is not None and cleanup.resolve().is_relative_to(root) and cleanup.resolve() != root:
            shutil.rmtree(cleanup, ignore_errors=True)
        raise

    return ImportResult(
        published=True,
        person_id=person_id,
        catalog_version=catalog_version,
        storage_dir=str(destination),
    )
