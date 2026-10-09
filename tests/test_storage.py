from pathlib import Path

from pdd_listing_automation.models import Platform, SourceProduct
from pdd_listing_automation.services.discovery import DiscoveryService
from pdd_listing_automation.storage.database import connect, initialize
from pdd_listing_automation.storage.repositories import SourceProductRepository


def test_discovery_upserts_without_duplicates(tmp_path: Path) -> None:
    database_path = tmp_path / "automation.sqlite3"
    initialize(database_path)

    products = [
        SourceProduct(
            source_goods_id="10001",
            source_row_key="row-1",
            title="商品 A",
            source_platform=Platform.PINDUODUO,
        ),
        SourceProduct(
            source_goods_id="10001",
            source_row_key="row-1",
            title="商品 A 更新",
            source_platform=Platform.PINDUODUO,
        ),
    ]

    with connect(database_path) as connection:
        result = DiscoveryService(connection).store(products)

    assert result.collected == 2
    assert result.unique == 1
    assert result.stored_total == 1


def test_listed_goods_ids_filters_by_platform(tmp_path: Path) -> None:
    database_path = tmp_path / "automation.sqlite3"
    initialize(database_path)
    products = [
        SourceProduct(
            source_goods_id="10001", source_row_key="r1", title="A",
            source_platform=Platform.PINDUODUO,
        ),
        SourceProduct(
            source_goods_id="10002", source_row_key="r2", title="B",
            source_platform=Platform.PINDUODUO,
        ),
        SourceProduct(
            source_goods_id="20001", source_row_key="r3", title="C",
            source_platform=Platform.DOUYIN,
        ),
    ]
    with connect(database_path) as connection:
        SourceProductRepository(connection).upsert_many(products)

    with connect(database_path) as connection:
        repo = SourceProductRepository(connection)
        assert repo.listed_goods_ids(Platform.PINDUODUO) == {"10001", "10002"}
        assert repo.listed_goods_ids(Platform.DOUYIN) == {"20001"}
