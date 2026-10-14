from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from time import time_ns

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
    target_shop TEXT NOT NULL DEFAULT '',
    listing_status TEXT NOT NULL DEFAULT 'recorded',
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
        if connection.execute("PRAGMA user_version").fetchone()[0] < 1:
            # 迁移前备份旧库；历史记录的目标店铺未知，保持为空。
            if connection.execute("SELECT COUNT(*) FROM source_products").fetchone()[0]:
                backup_path = database_path.with_name(f"{database_path.name}.bak-{time_ns()}")
                with sqlite3.connect(backup_path) as backup:
                    connection.backup(backup)
            with connection:
                columns = {r["name"] for r in connection.execute("PRAGMA table_info(source_products)")}
                if "target_shop" not in columns:
                    connection.execute(
                        "ALTER TABLE source_products ADD COLUMN target_shop TEXT NOT NULL DEFAULT ''"
                    )
                if "listing_status" not in columns:
                    connection.execute(
                        "ALTER TABLE source_products ADD COLUMN listing_status "
                        "TEXT NOT NULL DEFAULT 'recorded'"
                    )
                rows = connection.execute(
                    "SELECT identity_key, source_platform, target_shop, source_goods_id, "
                    "source_row_key FROM source_products"
                ).fetchall()
                for row in rows:
                    key = json.dumps(
                        [row["source_platform"], row["target_shop"],
                         row["source_goods_id"] or row["source_row_key"]],
                        ensure_ascii=False, separators=(",", ":"),
                    )
                    connection.execute(
                        "UPDATE source_products SET identity_key = ? WHERE identity_key = ?",
                        (key, row["identity_key"]),
                    )
                connection.execute("PRAGMA user_version = 1")
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_source_products_scope "
            "ON source_products(source_platform, target_shop, source_goods_id)"
        )
