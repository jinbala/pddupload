import argparse

from pdd_listing_automation.app import parse_item_ref, parse_selection, select_arrivals
from pdd_listing_automation.models import ShopNewArrival
from pdd_listing_automation.pages.favorite_shops import parse_shop_card
from pdd_listing_automation.pages.shop_catalog import _normalize_shop_url


def test_parse_shop_card_extracts_shop_and_items() -> None:
    card_text = (
        "MK网购排行 ：第128名主营 ：女休闲裤女牛仔裤地址 ：池尾商圈 综合市场 N档"
        "取消关注进入档口¥45.0010-04上新¥45.0010-04上新"
    )
    items = [
        {
            "id": "167526371",
            "href": "https://cs.17zwd.com/item/167526371?",
            "text": "¥45.0010-04上新",
            "image": "https://img.example.com/a.jpeg",
        },
        {
            "id": "167526318",
            "href": "https://cs.17zwd.com/item/167526318?",
            "text": "¥45.0010-04上新",
            "image": "",
        },
    ]

    arrivals = parse_shop_card(card_text, items)

    assert len(arrivals) == 2
    first = arrivals[0]
    assert first.item_id == "167526371"
    assert first.item_url == "https://cs.17zwd.com/item/167526371?"
    assert first.price_text == "45.00"
    assert first.on_shelf_date_text == "10-04"
    assert first.shop_name == "MK网购"
    assert first.shop_rank == "128"
    assert first.shop_main_category == "女休闲裤女牛仔裤"
    assert first.image_url == "https://img.example.com/a.jpeg"


def test_parse_shop_card_missing_price_and_date() -> None:
    arrivals = parse_shop_card(
        "金公主网购排行 ：第12名主营 ：女休闲裤毛呢外套地址 ：池尾商圈 钟潭 N档取消关注进入档口",
        [{"id": "167531624", "href": "https://cs.17zwd.com/item/167531624?", "text": ""}],
    )
    assert len(arrivals) == 1
    assert arrivals[0].price_text == ""
    assert arrivals[0].on_shelf_date_text == ""
    assert arrivals[0].on_shelf_sort_key == (0, 0)


def test_on_shelf_sort_key_orders_newest_first() -> None:
    def arrival(date_text: str) -> ShopNewArrival:
        return ShopNewArrival(
            item_id="1",
            item_url="https://cs.17zwd.com/item/1",
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
    return ShopNewArrival(item_id=item_id, item_url=f"https://cs.17zwd.com/item/{item_id}")


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


def test_parse_item_ref_accepts_full_url() -> None:
    item_id, item_url = parse_item_ref("https://cs.17zwd.com/item/167526371?")
    assert item_id == "167526371"
    assert item_url == "https://cs.17zwd.com/item/167526371?"


def test_parse_item_ref_accepts_bare_id() -> None:
    item_id, item_url = parse_item_ref("167086511")
    assert item_id == "167086511"
    assert item_url == "167086511"


def test_parse_selection_ranges_and_dedup() -> None:
    assert parse_selection("1,3,5-8", 10) == [0, 2, 4, 5, 6, 7]
    assert parse_selection("1-3", 10) == [0, 1, 2]
    assert parse_selection("5", 10) == [4]
    assert parse_selection("99", 10) == []
    assert parse_selection("1，3", 10) == [0, 2]  # 全角逗号
    assert parse_selection("1,1,2", 10) == [0, 1]  # 去重


def test_normalize_shop_url_adds_search_and_drops_spm() -> None:
    assert _normalize_shop_url(
        "https://cs.17zwd.com/shop/527795.htm?spm=0.42.132.0.0.0&page=1&search=y"
    ) == "https://cs.17zwd.com/shop/527795.htm?page=1&search=y"
    assert _normalize_shop_url("https://cs.17zwd.com/shop/527795.htm") == (
        "https://cs.17zwd.com/shop/527795.htm?page=1&search=y"
    )
