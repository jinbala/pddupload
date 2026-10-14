from __future__ import annotations

import sqlite3
from collections.abc import Iterable

from pdd_listing_automation.models import ListingStatus, Platform, SourceProduct


class SourceProductRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def listed_goods_ids(self, platform: Platform, target_shop: str = "") -> set[str]:
        """提交中/待核实也阻止重发；未知店铺的历史记录保守去重。"""
        query = "SELECT source_goods_id FROM source_products WHERE source_platform = ?"
        params = [platform.value]
        if target_shop:
            query += " AND (target_shop = ? OR target_shop = '')"
            params.append(target_shop)
        rows = self.connection.execute(
            query, params,
        ).fetchall()
        return {row["source_goods_id"] for row in rows if row["source_goods_id"]}

    def find_listing_record(
        self, platform: Platform, target_shop: str, goods_id: str,
    ) -> sqlite3.Row | None:
        """返回拦截依据，优先匹配目标店铺，其次是店铺未知的历史记录。"""
        return self.connection.execute(
            "SELECT target_shop, listing_status FROM source_products "
            "WHERE source_platform = ? AND source_goods_id = ? "
            "AND (target_shop = ? OR target_shop = '') "
            "ORDER BY (target_shop = ?) DESC LIMIT 1",
            (platform.value, goods_id, target_shop, target_shop),
        ).fetchone()

    def upsert_many(self, products: Iterable[SourceProduct]) -> int:
        rows = [
            (
                product.storage_key,
                product.source_goods_id,
                product.source_row_key,
                product.title,
                product.source_sku,
                product.price_text,
                product.source_shop,
                product.source_category,
                product.image_url,
                product.find_similar_url,
                product.source_platform.value,
                product.uploaded_at_text,
                product.collected_at.isoformat(),
                product.target_shop,
                product.listing_status.value,
            )
            for product in products
        ]
        if not rows:
            return 0
        with self.connection:
            self.connection.executemany(
                """
                INSERT INTO source_products (
                    identity_key,
                    source_goods_id,
                    source_row_key,
                    title,
                    source_sku,
                    price_text,
                    source_shop,
                    source_category,
                    image_url,
                    find_similar_url,
                    source_platform,
                    uploaded_at_text,
                    collected_at, target_shop, listing_status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(identity_key) DO UPDATE SET
                    source_goods_id = excluded.source_goods_id,
                    source_row_key = excluded.source_row_key,
                    title = COALESCE(NULLIF(excluded.title, ''), source_products.title),
                    source_sku = COALESCE(NULLIF(excluded.source_sku, ''), source_products.source_sku),
                    price_text = COALESCE(NULLIF(excluded.price_text, ''), source_products.price_text),
                    source_shop = COALESCE(NULLIF(excluded.source_shop, ''), source_products.source_shop),
                    source_category = COALESCE(NULLIF(excluded.source_category, ''), source_products.source_category),
                    image_url = COALESCE(NULLIF(excluded.image_url, ''), source_products.image_url),
                    find_similar_url = COALESCE(NULLIF(excluded.find_similar_url, ''), source_products.find_similar_url),
                    source_platform = excluded.source_platform,
                    uploaded_at_text = excluded.uploaded_at_text,
                    collected_at = excluded.collected_at,
                    listing_status = excluded.listing_status
                """,
                rows,
            )
        return len(rows)

    def count(self) -> int:
        row = self.connection.execute("SELECT COUNT(*) AS count FROM source_products").fetchone()
        return int(row["count"])

    def resolve_submission(
        self, platform: Platform, target_shop: str, goods_id: str, *, recorded: bool,
        include_recorded: bool = False,
    ) -> bool:
        """用户核对日志后，只处理指定店铺的待核实/已提交记录。"""
        statuses = [ListingStatus.UNCERTAIN.value, ListingStatus.SUBMITTED.value]
        if include_recorded:
            statuses.append(ListingStatus.RECORDED.value)
        condition = (
            "WHERE source_platform = ? AND target_shop = ? AND source_goods_id = ? "
            f"AND listing_status IN ({','.join('?' for _ in statuses)})"
        )
        params = (platform.value, target_shop, goods_id, *statuses)
        with self.connection:
            if recorded:
                cursor = self.connection.execute(
                    "UPDATE source_products SET listing_status = ? " + condition,
                    (ListingStatus.RECORDED.value, *params),
                )
            else:
                cursor = self.connection.execute("DELETE FROM source_products " + condition, params)
        return cursor.rowcount > 0
