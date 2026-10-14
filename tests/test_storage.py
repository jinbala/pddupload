import sqlite3
from pathlib import Path

from pdd_listing_automation.models import ListingStatus, Platform, SourceProduct
from pdd_listing_automation.services.discovery import DiscoveryService
from pdd_listing_automation.storage.database import SCHEMA, connect, initialize
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


def test_same_goods_on_different_platforms_and_shops_remain_independent(tmp_path):
    path = tmp_path / "db.sqlite3"
    initialize(path)
    products = [
        SourceProduct(source_goods_id="1", source_row_key="r", title="test",
                      source_platform=platform, target_shop=shop)
        for platform, shop in [(Platform.PINDUODUO, "A"), (Platform.PINDUODUO, "B"),
                               (Platform.DOUYIN, "A")]
    ]
    with connect(path) as db:
        result = DiscoveryService(db).store(products)
        assert result.unique == result.stored_total == 3
        repo = SourceProductRepository(db)
        assert repo.listed_goods_ids(Platform.PINDUODUO, "A") == {"1"}
        assert repo.listed_goods_ids(Platform.PINDUODUO, "C") == set()
        assert repo.listed_goods_ids(Platform.DOUYIN, "A") == {"1"}


def test_partial_refresh_preserves_known_metadata(tmp_path):
    path = tmp_path / "db.sqlite3"
    initialize(path)
    product = SourceProduct(source_goods_id="1", source_row_key="r", title="test",
                            source_platform=Platform.PINDUODUO, source_shop="档口", source_sku="货号")
    with connect(path) as db:
        repo = SourceProductRepository(db)
        repo.upsert_many([product])
        repo.upsert_many([product.model_copy(update={"source_shop": "", "source_sku": ""})])
        row = db.execute("SELECT * FROM source_products").fetchone()
        assert row["source_shop"] == "档口"
        assert row["source_sku"] == "货号"


def test_legacy_database_is_backed_up_and_migrated_once(tmp_path):
    path = tmp_path / "db.sqlite3"
    old_schema = SCHEMA.replace("    target_shop TEXT NOT NULL DEFAULT '',\n", "").replace(
        "    listing_status TEXT NOT NULL DEFAULT 'recorded',\n", "",
    )
    with sqlite3.connect(path) as db:
        db.executescript(old_schema)
        db.execute(
            "INSERT INTO source_products(identity_key, source_goods_id, source_row_key, title, "
            "source_platform, collected_at) VALUES ('1', '1', 'r', 'old', 'pinduoduo', '2026-01-01')"
        )
    initialize(path)
    initialize(path)
    backups = list(tmp_path.glob("db.sqlite3.bak-*"))
    assert len(backups) == 1
    with connect(path) as db:
        repo = SourceProductRepository(db)
        assert repo.listed_goods_ids(Platform.PINDUODUO, "任意目标店") == {"1"}
        repo.upsert_many([SourceProduct(source_goods_id="1", source_row_key="r", title="updated",
                                       source_platform=Platform.PINDUODUO)])
        assert repo.count() == 1
        assert db.execute("SELECT title FROM source_products").fetchone()[0] == "updated"
    with sqlite3.connect(backups[0]) as db:
        assert db.execute("SELECT identity_key FROM source_products").fetchone()[0] == "1"


def test_review_submission_only_changes_exact_pending_scope(tmp_path):
    path = tmp_path / "db.sqlite3"
    initialize(path)
    with connect(path) as db:
        repo = SourceProductRepository(db)
        products = [
            SourceProduct(source_goods_id="1", source_row_key="r", title="test",
                          source_platform=Platform.PINDUODUO, target_shop=shop, listing_status=status)
            for shop, status in [("A", ListingStatus.UNCERTAIN), ("B", ListingStatus.SUBMITTED),
                                 ("C", ListingStatus.RECORDED)]
        ]
        repo.upsert_many(products)
        assert repo.resolve_submission(Platform.PINDUODUO, "A", "1", recorded=False)
        assert repo.listed_goods_ids(Platform.PINDUODUO, "A") == set()
        assert repo.resolve_submission(Platform.PINDUODUO, "B", "1", recorded=True)
        assert not repo.resolve_submission(Platform.PINDUODUO, "B", "1", recorded=False)
        assert not repo.resolve_submission(Platform.PINDUODUO, "C", "1", recorded=False)
        assert repo.count() == 2


def test_recorded_review_requires_explicit_opt_in_and_preserves_other_shop(tmp_path):
    path = tmp_path / "db.sqlite3"
    initialize(path)
    with connect(path) as db:
        repo = SourceProductRepository(db)
        repo.upsert_many([
            SourceProduct(source_goods_id="1", source_row_key="r", title="test",
                          source_platform=Platform.PINDUODUO, target_shop=shop)
            for shop in ("A", "B")
        ])
        assert not repo.resolve_submission(Platform.PINDUODUO, "A", "1", recorded=False)
        assert repo.resolve_submission(
            Platform.PINDUODUO, "A", "1", recorded=False, include_recorded=True,
        )
        assert repo.listed_goods_ids(Platform.PINDUODUO, "A") == set()
        assert repo.listed_goods_ids(Platform.PINDUODUO, "B") == {"1"}
