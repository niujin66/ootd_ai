"""SQLite persistence for the OOTD AI minimal backend (task 4).

Stores published catalog versions and their assets.  Asset files themselves
live under the configured assets root; the database records the storage
directory and per-asset SHA-256/size so file serving never joins arbitrary
user paths.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS catalog_versions (
    person_id       TEXT NOT NULL,
    catalog_version TEXT NOT NULL,
    pose_id         TEXT NOT NULL,
    content_digest  TEXT NOT NULL,
    storage_dir     TEXT NOT NULL,
    published_at    TEXT NOT NULL,
    PRIMARY KEY (person_id, catalog_version)
);
CREATE TABLE IF NOT EXISTS assets (
    person_id       TEXT NOT NULL,
    catalog_version TEXT NOT NULL,
    asset_id        TEXT NOT NULL,
    asset_type      TEXT NOT NULL,
    relative_file   TEXT NOT NULL,
    source          TEXT NOT NULL,
    display_name    TEXT,
    tags            TEXT NOT NULL DEFAULT '[]',
    available       INTEGER NOT NULL DEFAULT 0,
    size_bytes      INTEGER NOT NULL,
    sha256          TEXT NOT NULL,
    PRIMARY KEY (person_id, catalog_version, asset_id),
    FOREIGN KEY (person_id, catalog_version)
        REFERENCES catalog_versions(person_id, catalog_version) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS recommender_usage (
    day   TEXT PRIMARY KEY,
    calls INTEGER NOT NULL DEFAULT 0
);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open a connection with manual transaction control (isolation_level=None)."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def get_version(conn: sqlite3.Connection, person_id: str, catalog_version: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM catalog_versions WHERE person_id=? AND catalog_version=?",
        (person_id, catalog_version),
    ).fetchone()


def get_current_version(conn: sqlite3.Connection, person_id: str) -> sqlite3.Row | None:
    """The most recently published version for a person."""
    return conn.execute(
        "SELECT * FROM catalog_versions WHERE person_id=? "
        "ORDER BY published_at DESC, rowid DESC LIMIT 1",
        (person_id,),
    ).fetchone()


def get_assets(conn: sqlite3.Connection, person_id: str, catalog_version: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM assets WHERE person_id=? AND catalog_version=? ORDER BY asset_id",
        (person_id, catalog_version),
    ).fetchall()


def get_asset(
    conn: sqlite3.Connection, person_id: str, catalog_version: str, asset_id: str
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM assets WHERE person_id=? AND catalog_version=? AND asset_id=?",
        (person_id, catalog_version, asset_id),
    ).fetchone()