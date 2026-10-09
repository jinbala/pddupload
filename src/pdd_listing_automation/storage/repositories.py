from __future__ import annotations

import sqlite3
from collections.abc import Iterable

from pdd_listing_automation.models import Platform, SourceProduct


class SourceProductRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def listed_goods_ids(self, platform: Platform) -> set[str]:
        """返回某平台已铺货过的来源商品 ID 集合，用于上架前去重。"""
        rows = self.connection.execute(
            "SELECT source_goods_id FROM source_products WHERE source_platform = ?",
            (platform.value,),
        ).fetchall()
        return {row["source_goods_id"] for row in rows}

    def upsert_many(self, products: Iterable[SourceProduct]) -> int:
        rows = [
            (
                product.identity,
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
                    collected_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(identity_key) DO UPDATE SET
                    source_goods_id = excluded.source_goods_id,
                    source_row_key = excluded.source_row_key,
                    title = excluded.title,
                    source_sku = excluded.source_sku,
                    price_text = excluded.price_text,
                    source_shop = excluded.source_shop,
                    source_category = excluded.source_category,
                    image_url = excluded.image_url,
                    find_similar_url = excluded.find_similar_url,
                    source_platform = excluded.source_platform,
                    uploaded_at_text = excluded.uploaded_at_text,
                    collected_at = excluded.collected_at
                """,
                rows,
            )
        return len(rows)

    def count(self) -> int:
        row = self.connection.execute("SELECT COUNT(*) AS count FROM source_products").fetchone()
        return int(row["count"])
