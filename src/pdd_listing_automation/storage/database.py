from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS source_products (
    identity_key TEXT PRIMARY KEY,
    source_goods_id TEXT NOT NULL,
    source_row_key TEXT NOT NULL,
    title TEXT NOT NULL,
    source_sku TEXT NOT NULL DEFAULT '',
    price_text TEXT NOT NULL DEFAULT '',
    source_shop TEXT NOT NULL DEFAULT '',
    source_category TEXT NOT NULL DEFAULT '',
    image_url TEXT NOT NULL DEFAULT '',
    find_similar_url TEXT NOT NULL DEFAULT '',
    source_platform TEXT NOT NULL,
    uploaded_at_text TEXT NOT NULL DEFAULT '',
    collected_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS shops (
    platform TEXT NOT NULL,
    shop_id TEXT NOT NULL,
    shop_name TEXT NOT NULL,
    authorization_status TEXT NOT NULL DEFAULT '',
    authorization_expires_text TEXT NOT NULL DEFAULT '',
    checked_at TEXT NOT NULL,
    PRIMARY KEY (platform, shop_id)
);

CREATE TABLE IF NOT EXISTS publish_tasks (
    task_id TEXT PRIMARY KEY,
    unique_key TEXT NOT NULL UNIQUE,
    source_goods_id TEXT NOT NULL,
    target_platform TEXT NOT NULL,
    target_shop_id TEXT NOT NULL,
    template_id TEXT NOT NULL DEFAULT '',
    mode TEXT NOT NULL DEFAULT 'edit',
    status TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    message TEXT NOT NULL DEFAULT '',
    artifact_path TEXT NOT NULL DEFAULT ''
);
"""


def connect(database_path: Path) -> sqlite3.Connection:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def initialize(database_path: Path) -> None:
    with connect(database_path) as connection:
        connection.executescript(SCHEMA)
