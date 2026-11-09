"""Command line entry point for whole-package import (task 4).

Usage:
    python import_package.py PACKAGE_DIR [--expected-garments N]
                              [--db-path PATH] [--assets-root DIR]

Config via environment (or same-named flags, which take precedence):
    OOTD_DB_PATH       SQLite file (default <repo>/outputs/ootd.db)
    OOTD_ASSETS_ROOT   published-asset storage (default <repo>/outputs/server-assets)

Exit codes:
    0  published (or same version with identical content, reused)
    1  package invalid, or same version with different content
    2  usage error
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import db
from importer import PackageInvalidError, VersionConflictError, import_package

SERVER_DIR = Path(__file__).resolve().parent
REPO_DIR = SERVER_DIR.parent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="校验并整包导入 OOTD AI 资产包。")
    parser.add_argument("package_dir", help="资产包目录（含 manifest.json 与 GLB）")
    parser.add_argument("--expected-garments", type=int, default=2, help="要求的衣物件数（默认 2）")
    parser.add_argument("--db-path", default=None, help="覆盖 OOTD_DB_PATH")
    parser.add_argument("--assets-root", default=None, help="覆盖 OOTD_ASSETS_ROOT")
    args = parser.parse_args(argv)

    if not Path(args.package_dir).is_dir():
        print(f"错误：资产包目录不存在：{args.package_dir}", file=sys.stderr)
        return 2

    db_path = Path(args.db_path or os.environ.get("OOTD_DB_PATH") or (REPO_DIR / "outputs" / "ootd.db"))
    assets_root = Path(
        args.assets_root or os.environ.get("OOTD_ASSETS_ROOT") or (REPO_DIR / "outputs" / "server-assets")
    )

    conn = db.connect(db_path)
    db.init_db(conn)
    try:
        try:
            result = import_package(conn, args.package_dir, assets_root, expected_garments=args.expected_garments)
        except PackageInvalidError as exc:
            print(f"校验失败，未发布（exit 1）：\n{exc}")
            return 1
        except VersionConflictError as exc:
            print(f"版本冲突，未发布（exit 1）：{exc}")
            return 1
    finally:
        conn.close()

    if result.reused:
        print("版本已存在且内容一致，跳过（exit 0）")
    else:
        print(
            f"已发布：{result.person_id}/{result.catalog_version}"
            f" -> {result.storage_dir}（exit 0）"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())