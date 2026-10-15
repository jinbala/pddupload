from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
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
    source_market TEXT NOT NULL DEFAULT '',
    source_url TEXT NOT NULL DEFAULT '',
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

CREATE TABLE IF NOT EXISTS batches (
    batch_id TEXT PRIMARY KEY,
    options_json TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'running',
    created_at TEXT NOT NULL,
    finished_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS batch_items (
    batch_id TEXT NOT NULL REFERENCES batches(batch_id),
    position INTEGER NOT NULL,
    arrival_json TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',
    result_json TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (batch_id, position)
);
CREATE TABLE IF NOT EXISTS draft_submissions (
    target_shop TEXT NOT NULL,
    source_market TEXT NOT NULL,
    goods_id TEXT NOT NULL,
    status TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (target_shop, source_market, goods_id)
);
"""


@contextmanager
def connect(database_path: Path) -> Iterator[sqlite3.Connection]:
    """事务结束后显式关闭连接，长批次不累积 SQLite 文件句柄。"""
    database_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(database_path, timeout=15)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        with connection:
            yield connection


def initialize(database_path: Path) -> None:
    with connect(database_path) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version >= 2:
            return
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='source_products'"
        ).fetchone()
        # 在任何结构修改之前备份，含空库和既有批次数据。
        if exists:
            backup_path = database_path.with_name(f"{database_path.name}.bak-{time_ns()}")
            with closing(sqlite3.connect(backup_path)) as backup:
                connection.backup(backup)
        connection.executescript(SCHEMA)
        with connection:
            columns = {r["name"] for r in connection.execute("PRAGMA table_info(source_products)")}
            for name, default in (("target_shop", ""), ("listing_status", "recorded"),
                                  ("source_market", ""), ("source_url", "")):
                if name not in columns:
                    connection.execute(
                        f"ALTER TABLE source_products ADD COLUMN {name} TEXT NOT NULL DEFAULT '{default}'"
                    )
            rows = connection.execute(
                "SELECT identity_key, source_platform, target_shop, source_market, source_goods_id, "
                "source_row_key FROM source_products"
            ).fetchall()
            for row in rows:
                key = json.dumps(
                    [row["source_platform"], row["target_shop"], row["source_market"],
                     row["source_goods_id"] or row["source_row_key"]],
                    ensure_ascii=False, separators=(",", ":"),
                )
                connection.execute(
                    "UPDATE source_products SET identity_key = ? WHERE identity_key = ?",
                    (key, row["identity_key"]),
                )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_source_products_scope "
                "ON source_products(source_platform, target_shop, source_goods_id, source_market)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_source_products_lookup "
                "ON source_products(source_platform, source_goods_id, target_shop, source_market)"
            )
            connection.execute("PRAGMA user_version = 2")
