from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from pdd_listing_automation.models import Platform, SourceProduct
from pdd_listing_automation.storage.repositories import SourceProductRepository


@dataclass(frozen=True)
class DiscoveryResult:
    collected: int
    unique: int
    stored_total: int
    products: list[SourceProduct]


class DiscoveryService:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.products = SourceProductRepository(connection)

    def store(self, products: list[SourceProduct]) -> DiscoveryResult:
        deduplicated = {product.identity: product for product in products}
        self.products.upsert_many(deduplicated.values())
        return DiscoveryResult(
            collected=len(products),
            unique=len(deduplicated),
            stored_total=self.products.count(),
            products=list(deduplicated.values()),
        )

    @staticmethod
    def filter_platform(
        products: list[SourceProduct],
        platform: Platform,
    ) -> list[SourceProduct]:
        return [product for product in products if product.source_platform == platform]
