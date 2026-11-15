# -*- coding: utf-8 -*-
"""在没装 trimesh 的机器上接收候选包用的「标准库校验 + 导入」工具。

正式的导入流程仍然是 server/import_package.py（它用 trimesh 算顶点数、三角面数、
包围盒，并做几何层面的告警）。这个脚本是给「本机装不上 trimesh」的场景兜底的，
做的事情是：

  1. 检查 manifest 基本字段（person_id / catalog_version / assets，至少两件可用上衣）
  2. 逐个资产：文件存在、体积、sha256；如果同目录有 validation-report.json，就核对哈希
  3. 按 GLB 容器结构真正解一遍：magic/version/length、JSON chunk、meshes/accessors/
     bufferViews 的索引和字节范围，确认「能解码」，不是只对哈希
  4. 用和 importer.py 完全相同的 content_digest 算法，把版本和资产写进 SQLite

只写 outputs/ 下的数据库和资产目录，不改候选包本身。重复导入同一个版本：
内容一致就跳过，内容不同就报冲突。

用法：
    python server/tools/import_candidate.py PACKAGE_DIR [--db-path ...] [--assets-root ...] [--dry-run]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import struct
import sys
import time
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parent
REPO_DIR = SERVER_DIR.parents[1]

GLB_MAGIC = b"glTF"
CHUNK_JSON = 0x4E4F534A
CHUNK_BIN = 0x004E4942


class PackageError(Exception):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_glb(data: bytes):
    """按 GLB 容器格式解一遍，返回 (ok, info, error)。"""
    if len(data) < 12:
        return False, None, "文件太短，连 GLB 头都不够 12 字节"
    magic, version, length = struct.unpack_from("<4sII", data, 0)
    if magic != GLB_MAGIC:
        return False, None, f"magic 不是 glTF（拿到 {magic!r}）"
    if version != 2:
        return False, None, f"glTF 版本不是 2（拿到 {version}）"
    if length != len(data):
        return False, None, f"头部写的长度 {length} 和实际文件大小 {len(data)} 对不上"

    offset = 12
    gltf = None
    bin_len = 0
    while offset + 8 <= len(data):
        chunk_len, chunk_type = struct.unpack_from("<II", data, offset)
        offset += 8
        if offset + chunk_len > len(data):
            return False, None, "chunk 长度越界"
        chunk = data[offset:offset + chunk_len]
        offset += chunk_len
        if chunk_type == CHUNK_JSON and gltf is None:
            try:
                gltf = json.loads(chunk.decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as exc:
                return False, None, f"JSON chunk 解析失败：{exc}"
        elif chunk_type == CHUNK_BIN:
            bin_len = chunk_len
    if gltf is None:
        return False, None, "没有 JSON chunk"

    asset = gltf.get("asset") or {}
    if not str(asset.get("version", "")).startswith("2"):
        return False, None, f"asset.version 不是 2.x（{asset.get('version')!r}）"
    meshes = gltf.get("meshes") or []
    if not meshes:
        return False, None, "没有 meshes"
    accessors = gltf.get("accessors") or []
    if not accessors:
        return False, None, "没有 accessors"

    buffers = gltf.get("buffers") or []
    for index, buffer in enumerate(buffers):
        declared = int(buffer.get("byteLength") or 0)
        if index == 0 and declared > bin_len:
            return False, None, f"buffer[0] 声明 {declared} 字节，BIN chunk 只有 {bin_len} 字节"

    vertices = 0
    triangles = 0
    for mesh in meshes:
        primitives = mesh.get("primitives") or []
        if not primitives:
            return False, None, "mesh 里没有 primitive"
        for primitive in primitives:
            attrs = primitive.get("attributes") or {}
            pos_index = attrs.get("POSITION")
            if not isinstance(pos_index, int) or not 0 <= pos_index < len(accessors):
                return False, None, "primitive 缺少有效的 POSITION accessor"
            vertices += int(accessors[pos_index].get("count") or 0)
            idx = primitive.get("indices")
            if isinstance(idx, int) and 0 <= idx < len(accessors):
                triangles += int(accessors[idx].get("count") or 0) // 3
            else:
                triangles += int(accessors[pos_index].get("count") or 0) // 3
    if vertices <= 0:
        return False, None, "解出来的顶点数为 0"
    return True, {"vertices": vertices, "triangles": triangles}, None


def load_manifest(package: Path) -> dict:
    path = package / "manifest.json"
    if not path.is_file():
        raise PackageError(f"缺少 manifest.json：{package}")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PackageError(f"manifest.json 读不了：{exc}") from exc
    if not isinstance(manifest, dict):
        raise PackageError("manifest.json 顶层必须是对象")
    for key in ("person_id", "catalog_version", "assets"):
        if not manifest.get(key):
            raise PackageError(f"manifest 缺少 {key}")
    if not isinstance(manifest["assets"], list) or not manifest["assets"]:
        raise PackageError("manifest.assets 必须是非空数组")
    garments = [
        item for item in manifest["assets"]
        if item.get("asset_type") == "garment" and item.get("available")
    ]
    if len(garments) < 2:
        raise PackageError(f"需要至少 2 件可用上衣，当前 {len(garments)} 件")
    return manifest


def verify_assets(package: Path, manifest: dict):
    """逐个文件算哈希、解 GLB，并核对 validation-report.json（若有）。"""
    report_path = package / "validation-report.json"
    report_assets = {}
    if report_path.is_file():
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
            report_assets = {a.get("relative_file"): a for a in report.get("assets", [])}
        except (OSError, ValueError):
            report_assets = {}

    records = []
    for entry in manifest["assets"]:
        relative = entry.get("relative_file")
        if not relative:
            raise PackageError(f"资产 {entry.get('asset_id')} 缺少 relative_file")
        path = package / relative
        if not path.is_file():
            raise PackageError(f"文件不存在：{relative}")
        data = path.read_bytes()
        sha = sha256_file(path)
        expected = report_assets.get(relative) or {}
        if expected.get("sha256") and expected["sha256"] != sha:
            raise PackageError(
                f"{relative} 的 sha256 和 validation-report 不一致："
                f"{sha} != {expected['sha256']}"
            )
        if expected.get("size_bytes") and expected["size_bytes"] != len(data):
            raise PackageError(
                f"{relative} 的体积和 validation-report 不一致："
                f"{len(data)} != {expected['size_bytes']}"
            )
        if relative.lower().endswith(".glb"):
            ok, info, error = read_glb(data)
            if not ok:
                raise PackageError(f"{relative} 解不了：{error}")
        else:
            info = None
        records.append(
            {
                "asset_id": entry.get("asset_id"),
                "asset_type": entry.get("asset_type"),
                "relative_file": relative,
                "size_bytes": len(data),
                "sha256": sha,
                "vertices": (info or {}).get("vertices"),
                "triangles": (info or {}).get("triangles"),
            }
        )
    return records


def content_digest(manifest: dict, records) -> str:
    """和 server/importer.py 的 _content_digest 完全一致。"""
    digest = hashlib.sha256()
    digest.update(json.dumps(manifest, sort_keys=True, ensure_ascii=False).encode("utf-8"))
    for asset in sorted(records, key=lambda item: item.get("relative_file") or ""):
        digest.update(asset["relative_file"].encode("utf-8"))
        digest.update(asset["sha256"].encode("ascii"))
    return digest.hexdigest()


def import_package(package: Path, db_path: Path, assets_root: Path, dry_run: bool):
    manifest = load_manifest(package)
    records = verify_assets(package, manifest)
    person_id = manifest["person_id"]
    catalog_version = manifest["catalog_version"]
    digest = content_digest(manifest, records)

    assets_root = assets_root.resolve()
    destination = (assets_root / person_id / catalog_version).resolve()
    if not destination.is_relative_to(assets_root) or destination == assets_root:
        raise PackageError("目标目录必须位于资产存储目录内")

    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT content_digest FROM catalog_versions WHERE person_id=? AND catalog_version=?",
            (person_id, catalog_version),
        ).fetchone()
        if existing is not None:
            if existing["content_digest"] != digest:
                conn.execute("ROLLBACK")
                raise PackageError(
                    f"版本 {catalog_version} 已发布但内容不同（digest 不一致），请用新版本号"
                )
            conn.execute("COMMIT")
            return {"reused": True, "person_id": person_id, "catalog_version": catalog_version,
                    "digest": digest, "storage_dir": str(destination), "records": records}

        if destination.exists():
            conn.execute("ROLLBACK")
            raise PackageError(f"目标目录已存在但没登记：{destination}")

        if dry_run:
            conn.execute("ROLLBACK")
            return {"reused": False, "person_id": person_id, "catalog_version": catalog_version,
                    "digest": digest, "storage_dir": str(destination), "records": records}

        if not dry_run:
            staging = Path(str(destination) + ".tmp-import")
            if staging.exists():
                shutil.rmtree(staging)
            staging.mkdir(parents=True)
            for record in records:
                target = staging / record["relative_file"]
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(package / record["relative_file"], target)
                if sha256_file(target) != record["sha256"]:
                    shutil.rmtree(staging, ignore_errors=True)
                    raise PackageError(f"{record['relative_file']} 复制后哈希变了")
            destination.parent.mkdir(parents=True, exist_ok=True)
            staging.rename(destination)

        published_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        conn.execute(
            "INSERT INTO catalog_versions"
            " (person_id, catalog_version, pose_id, content_digest, storage_dir, published_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (person_id, catalog_version, manifest.get("pose_id"), digest, str(destination), published_at),
        )
        by_id = {item.get("asset_id"): item for item in manifest["assets"]}
        for record in records:
            entry = by_id.get(record["asset_id"], {})
            tags = entry.get("tags")
            conn.execute(
                "INSERT INTO assets"
                " (person_id, catalog_version, asset_id, asset_type, relative_file, source,"
                "  display_name, tags, available, size_bytes, sha256)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    person_id,
                    catalog_version,
                    record["asset_id"],
                    record["asset_type"],
                    record["relative_file"],
                    entry.get("source") or "",
                    entry.get("display_name"),
                    json.dumps(tags if isinstance(tags, list) else [], ensure_ascii=False),
                    1 if entry.get("available") else 0,
                    record["size_bytes"],
                    record["sha256"],
                ),
            )
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()

    return {"reused": False, "person_id": person_id, "catalog_version": catalog_version,
            "digest": digest, "storage_dir": str(destination), "records": records}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="校验并导入一个候选包（标准库版，不需要 trimesh）")
    parser.add_argument("package_dir", help="候选包目录（含 manifest.json 和 GLB）")
    parser.add_argument("--db-path", default=None, help="SQLite 路径，默认仓库根 outputs/ootd.db")
    parser.add_argument("--assets-root", default=None, help="资产目录，默认仓库根 outputs/server-assets")
    parser.add_argument("--dry-run", action="store_true", help="只校验不写库不拷文件")
    args = parser.parse_args(argv)

    package = Path(args.package_dir)
    if not package.is_dir():
        print(f"错误：候选包目录不存在：{package}", file=sys.stderr)
        return 2
    db_path = Path(args.db_path or (REPO_DIR / "outputs" / "ootd.db"))
    assets_root = Path(args.assets_root or (REPO_DIR / "outputs" / "server-assets"))

    try:
        result = import_package(package, db_path, assets_root, args.dry_run)
    except PackageError as exc:
        print(f"未导入：{exc}", file=sys.stderr)
        return 1

    status = "版本已存在且内容一致，跳过" if result["reused"] else "已导入"
    if args.dry_run:
        status = "试运行：校验通过，未写库未拷文件"
    print(f"{status}：{result['person_id']}/{result['catalog_version']}")
    print(f"  content_digest = {result['digest']}")
    print(f"  存储目录       = {result['storage_dir']}")
    for record in result["records"]:
        extra = ""
        if record.get("vertices"):
            extra = f" v={record['vertices']} t={record['triangles']}"
        print(f"  {record['asset_id']:12s} {record['size_bytes']:>9d} B  sha256={record['sha256'][:12]}…{extra}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
