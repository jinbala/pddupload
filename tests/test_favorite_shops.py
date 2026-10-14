import argparse
import asyncio
from unittest.mock import AsyncMock, MagicMock

from pdd_listing_automation.app import parse_item_ref, parse_selection, select_arrivals
from pdd_listing_automation.models import ShopNewArrival
from pdd_listing_automation.pages.favorite_shops import parse_shop_card
from pdd_listing_automation.pages.shop_catalog import ShopCatalogPage, _normalize_shop_url


def test_parse_shop_card_extracts_shop_and_items() -> None:
    card_text = (
        "模拟档口A排行 ：第1名主营 ：示例品类A地址 ：示例区域 示例市场A区"
        "取消关注进入档口¥10.0010-04上新¥10.0010-04上新"
    )
    items = [
        {
            "id": "10001",
            "href": "https://market.example.com/item/10001?",
            "text": "¥10.0010-04上新",
            "image": "https://img.example.com/a.jpeg",
        },
        {
            "id": "10002",
            "href": "https://market.example.com/item/10002?",
            "text": "¥10.0010-04上新",
            "image": "",
        },
    ]

    arrivals = parse_shop_card(card_text, items)

    assert len(arrivals) == 2
    first = arrivals[0]
    assert first.item_id == "10001"
    assert first.item_url == "https://market.example.com/item/10001?"
    assert first.price_text == "10.00"
    assert first.on_shelf_date_text == "10-04"
    assert first.shop_name == "模拟档口A"
    assert first.shop_rank == "1"
    assert first.shop_main_category == "示例品类A"
    assert first.image_url == "https://img.example.com/a.jpeg"


def test_parse_shop_card_missing_price_and_date() -> None:
    arrivals = parse_shop_card(
        "模拟档口B排行 ：第2名主营 ：示例品类B地址 ：示例区域 示例市场B区取消关注进入档口",
        [{"id": "10003", "href": "https://market.example.com/item/10003?", "text": ""}],
    )
    assert len(arrivals) == 1
    assert arrivals[0].price_text == ""
    assert arrivals[0].on_shelf_date_text == ""
    assert arrivals[0].on_shelf_sort_key == (0, 0)


def test_on_shelf_sort_key_orders_newest_first() -> None:
    def arrival(date_text: str) -> ShopNewArrival:
        return ShopNewArrival(
            item_id="1",
            item_url="https://market.example.com/item/1",
            on_shelf_date_text=date_text,
        )

    dates = ["09-08", "10-05", "10-04", "08-06"]
    ordered = sorted(
        (arrival(d) for d in dates),
        key=lambda a: (a.on_shelf_sort_key, a.item_id),
        reverse=True,
    )
    assert [a.on_shelf_date_text for a in ordered] == ["10-05", "10-04", "09-08", "08-06"]


def _arrival(item_id: str) -> ShopNewArrival:
    return ShopNewArrival(item_id=item_id, item_url=f"https://market.example.com/item/{item_id}")


def test_select_arrivals_by_ids_skips_listed() -> None:
    arrivals = [_arrival("1"), _arrival("2"), _arrival("3")]
    args = argparse.Namespace(item_ids="1,2,3", limit=0)
    selected, skipped = select_arrivals(arrivals, args, {"2"})
    assert [a.item_id for a in selected] == ["1", "3"]
    assert [a.item_id for a in skipped] == ["2"]


def test_select_arrivals_by_limit_skips_listed() -> None:
    arrivals = [_arrival("1"), _arrival("2"), _arrival("3"), _arrival("4")]
    args = argparse.Namespace(item_ids="", limit=2)
    selected, skipped = select_arrivals(arrivals, args, {"1", "3"})
    assert [a.item_id for a in selected] == ["2", "4"]
    assert [a.item_id for a in skipped] == ["1", "3"]


def test_select_arrivals_deduplicates_repeated_ids():
    selected, skipped = select_arrivals(
        [_arrival("1"), _arrival("2")], argparse.Namespace(item_ids="1，1,2", limit=0), set(),
    )
    assert [arrival.item_id for arrival in selected] == ["1", "2"]
    assert skipped == []


def test_parse_item_ref_accepts_full_url() -> None:
    item_id, item_url = parse_item_ref("https://market.example.com/item/10001?")
    assert item_id == "10001"
    assert item_url == "https://market.example.com/item/10001?"


def test_parse_item_ref_accepts_bare_id() -> None:
    item_id, item_url = parse_item_ref("10004")
    assert item_id == "10004"
    assert item_url == "10004"


def test_parse_selection_ranges_and_dedup() -> None:
    assert parse_selection("1,3,5-8", 10) == [0, 2, 4, 5, 6, 7]
    assert parse_selection("1-3", 10) == [0, 1, 2]
    assert parse_selection("5", 10) == [4]
    assert parse_selection("99", 10) == []
    assert parse_selection("1，3", 10) == [0, 2]  # 全角逗号
    assert parse_selection("1,1,2", 10) == [0, 1]  # 去重


def test_normalize_shop_url_adds_search_and_drops_spm() -> None:
    assert _normalize_shop_url(
        "https://market.example.com/shop/20001.htm?spm=0.42.132.0.0.0&page=1&search=y"
    ) == "https://market.example.com/shop/20001.htm?page=1&search=y"
    assert _normalize_shop_url("https://market.example.com/shop/20001.htm") == (
        "https://market.example.com/shop/20001.htm?page=1&search=y"
    )


def test_shop_catalog_stops_at_last_page_without_losing_collected_items():
    catalog = ShopCatalogPage(MagicMock())
    catalog.collect_current_page = AsyncMock(return_value=[_arrival("1")])
    catalog.has_next_page = AsyncMock(return_value=False)
    catalog.go_to_next_page = AsyncMock()
    assert [a.item_id for a in asyncio.run(catalog.collect(20))] == ["1"]
    catalog.go_to_next_page.assert_not_awaited()
