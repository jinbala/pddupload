import argparse
import asyncio
import json
import sqlite3
from datetime import date
from unittest.mock import AsyncMock, MagicMock

import pytest
from playwright.async_api import async_playwright

from pdd_listing_automation import app
from pdd_listing_automation.config import Settings
from pdd_listing_automation.models import (
    ListingStatus,
    Platform,
    ShopNewArrival,
    SourceProduct,
    arrival_sort_key,
)
from pdd_listing_automation.pages.upload_platform import UploadPlatformPage, UploadRejectedError
from pdd_listing_automation.pages.waits import list_signature, wait_for_list
from pdd_listing_automation.services.drafts import load_source_prices
from pdd_listing_automation.storage.batches import BatchJournal
from pdd_listing_automation.storage.database import SCHEMA, connect, initialize
from pdd_listing_automation.storage.drafts import DraftLedger
from pdd_listing_automation.storage.repositories import SourceProductRepository


@pytest.fixture
def settings(tmp_path):
    result = Settings(root_dir=tmp_path, data_dir=tmp_path / "data", profile_dir=tmp_path / "profile",
                      artifacts_dir=tmp_path / "artifacts", database_path=tmp_path / "data/db.sqlite3")
    result.ensure_directories()
    initialize(result.database_path)
    return result


def product(market, price="20"):
    return SourceProduct(source_goods_id="1", source_row_key="r", title="商品",
                         source_platform=Platform.PINDUODUO, target_shop="店铺",
                         source_url=f"https://{market}/item/1", price_text=price,
                         listing_status=ListingStatus.UNCERTAIN)


def test_market_scope_and_exact_resolution(settings):
    with connect(settings.database_path) as db:
        repo = SourceProductRepository(db)
        repo.upsert_many([product("gz.17zwd.com"), product("cs.17zwd.com", "30")])
        assert repo.count() == 2
        assert repo.find_listing_record(Platform.PINDUODUO, "店铺", "1", "other.example.com") is None
        assert repo.listed_product_keys(Platform.PINDUODUO, "店铺") == {("gz.17zwd.com", "1"), ("cs.17zwd.com", "1")}
        with pytest.raises(ValueError, match="多个来源站点"):
            repo.resolve_submission(Platform.PINDUODUO, "店铺", "1", recorded=False)
        assert repo.resolve_submission(Platform.PINDUODUO, "店铺", "1", recorded=False, source_market="cs.17zwd.com")
        assert repo.count() == 1


def test_v1_migration_backups_and_closes_connections(tmp_path):
    path = tmp_path / "old.db"
    old_schema = SCHEMA.replace("    source_market TEXT NOT NULL DEFAULT '',\n", "").replace(
        "    source_url TEXT NOT NULL DEFAULT '',\n", "")
    with sqlite3.connect(path) as db:
        db.executescript(old_schema)
        db.execute("PRAGMA user_version=1")
        db.execute("INSERT INTO source_products(identity_key,source_goods_id,source_row_key,title,source_platform,collected_at) VALUES ('old','1','r','old','pinduoduo','2026-01-01')")
    initialize(path)
    initialize(path)
    assert len(list(tmp_path.glob("old.db.bak-*"))) == 1
    with connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
        repo = SourceProductRepository(db)
        assert repo.find_listing_record(Platform.PINDUODUO, "店铺", "1", "cs.17zwd.com") is not None
    with pytest.raises(sqlite3.ProgrammingError):
        db.execute("SELECT 1")


def test_cross_year_dates_and_invalid_dates():
    today = date(2027, 1, 3)
    assert arrival_sort_key("01-02", today) > arrival_sort_key("12-31", today)
    assert arrival_sort_key("2026-12-31", today) == arrival_sort_key("12-31", today)
    assert arrival_sort_key("02-29", today)[0] == 2024
    assert arrival_sort_key("13-40", today) == (0, 0)


def test_ambiguous_price_is_not_used(settings):
    with connect(settings.database_path) as db:
        SourceProductRepository(db).upsert_many([product("gz.17zwd.com"), product("cs.17zwd.com", "30")])
    assert load_source_prices(settings, "店铺") == {}
    assert load_source_prices(settings, "店铺", "cs.17zwd.com") == {"1": 30}


def test_selection_normalizes_id_and_url_and_rejects_ambiguous_id():
    first = ShopNewArrival(item_id="1", item_url="https://cs.17zwd.com/item/1")
    args = argparse.Namespace(item_ids="1,https://cs.17zwd.com/item/1", limit=0)
    selected, skipped = app.select_arrivals([first], args, set())
    assert selected == [first] and skipped == []
    second = ShopNewArrival(item_id="1", item_url="https://gz.17zwd.com/item/1")
    with pytest.raises(RuntimeError, match="多个来源站点"):
        app.select_arrivals([first, second], args, set())
    args.item_ids = f"{first.item_url},{second.item_url}"
    selected, _ = app.select_arrivals([first, second], args, set())
    assert selected == [first, second]


def test_draft_intent_survives_restart(settings):
    DraftLedger(settings.database_path, "A", "cs.17zwd.com").save("1", "uncertain")
    recovered = DraftLedger(settings.database_path, "A", "cs.17zwd.com")
    assert recovered.status("1") == "uncertain"
    assert DraftLedger(settings.database_path, "B", "cs.17zwd.com").status("1") == ""
    assert recovered.resolve("1", "not-submitted")
    assert recovered.status("1") == ""


def test_exclusive_shop_selection_and_last_second_recheck():
    async def scenario():
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.set_content("<iframe></iframe>")
                frame = page.frames[1]
                await frame.set_content('''<div role="dialog" aria-label="选择上货店铺">
                    <div><span>旧店A</span><input type="checkbox" checked></div>
                    <div><span>旧店B</span><input type="checkbox" checked></div>
                    <div><span>目标店</span><input type="checkbox"></div>
                    <div><span>已签署商家搬家承诺书</span><input type="checkbox">
                    <span class="ant-checkbox-inner" onclick="this.previousElementSibling.click()"></span></div>
                    <button>开始上传</button></div>''')
                upload = UploadPlatformPage(page)
                await upload.select_shop("目标店")
                assert await frame.locator("input:checked").count() == 1
                await upload.accept_commitment()
                await upload.verify_shop_selection()
                await frame.locator("input").first.check()
                with pytest.raises(RuntimeError, match="额外选中"):
                    await upload.start_upload()
            finally:
                await browser.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("text,old,expected", [
    ("提交成功", False, "submitted"),
    ("提交成功", True, "uncertain"),
    ("未提交成功，请重试", False, "rejected"),
    ("提交成功后请查看上货记录", False, "uncertain"),
])
def test_feedback_requires_new_positive_result(text, old, expected):
    async def scenario():
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.set_content("<iframe></iframe>")
                frame = page.frames[1]
                await frame.set_content('<div id="result"></div><button>确认上货</button>')
                await frame.evaluate("""({text, old}) => {
                    const result = document.querySelector('#result');
                    if (old) result.textContent = text;
                    document.querySelector('button').onclick = () => result.textContent = text;
                }""", {"text": text, "old": old})
                page.wait_for_timeout = AsyncMock()
                upload = UploadPlatformPage(page)
                if expected == "rejected":
                    with pytest.raises(UploadRejectedError):
                        await upload.confirm_listing()
                else:
                    assert await upload.confirm_listing() == expected
            finally:
                await browser.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("delay", [30, 950])
def test_pagination_waits_for_new_rows_even_on_slow_page(delay):
    async def scenario():
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.set_content('<a href="/item/1">old</a>')
                previous = await list_signature(page, 'a')
                await page.evaluate("delay => setTimeout(() => document.querySelector('a').href='/item/2', delay)", delay)
                await wait_for_list(page, 'a', previous)
                assert await page.locator('a').get_attribute('href') == '/item/2'
            finally:
                await browser.close()
    asyncio.run(scenario())


def batch_args(**extra):
    return argparse.Namespace(platform="pinduoduo", target_shop="店铺", confirm_publish="",
                              item_ids="1,2", limit=0, delay_seconds=0, hold_open_seconds=0, **extra)


def prepared():
    return {"title": "商品", "sku": "A", "price": "20", "screenshot_path": "test.png",
            "listing_status": "prepared"}, None, False


def test_resume_after_cancellation_only_runs_unfinished_items(settings, monkeypatch):
    arrivals = [ShopNewArrival(item_id=str(i), item_url=f"https://cs.17zwd.com/item/{i}") for i in (1, 2)]
    first = AsyncMock(side_effect=[prepared(), asyncio.CancelledError()])
    monkeypatch.setattr(app, "run_single_upload", first)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(app._process_arrivals(MagicMock(), arrivals, batch_args(), settings,
                                         Platform.PINDUODUO, "test", "商品"))
    batch_id = BatchJournal.recent(settings.database_path)[0]["batch_id"]
    journal = BatchJournal(settings.database_path, batch_id)
    _, items = journal.load()
    assert [item["state"] for item in items] == ["done", "running"]
    second = AsyncMock(return_value=prepared())
    monkeypatch.setattr(app, "run_single_upload", second)
    assert asyncio.run(app._process_arrivals(MagicMock(), arrivals, batch_args(resume_batch=batch_id),
                                            settings, Platform.PINDUODUO, "test", "商品")) == 0
    assert second.await_count == 1
    assert second.call_args.kwargs["item_id"] == "2"
    _, items = journal.load()
    assert [item["state"] for item in items] == ["done", "done"]


def test_preselected_duplicates_are_in_summary(settings, monkeypatch, capsys):
    with connect(settings.database_path) as db:
        SourceProductRepository(db).upsert_many([product("cs.17zwd.com")])
    arrivals = [ShopNewArrival(item_id=str(i), item_url=f"https://cs.17zwd.com/item/{i}") for i in (1, 2)]
    upload = AsyncMock(return_value=prepared())
    monkeypatch.setattr(app, "run_single_upload", upload)
    asyncio.run(app._process_arrivals(MagicMock(), arrivals, batch_args(), settings,
                                     Platform.PINDUODUO, "test", "商品"))
    assert upload.await_count == 1
    assert "本次选择 2 款" in capsys.readouterr().out
    batch_id = BatchJournal.recent(settings.database_path)[0]["batch_id"]
    _, items = BatchJournal(settings.database_path, batch_id).load()
    assert items[0]["result"]["状态"] == "跳过（已有记录）"
    assert json.loads(next(settings.artifacts_dir.glob("test-pinduoduo-*.json")).read_text(encoding="utf-8"))


def fake_upload_pages(monkeypatch, outcome):
    product_page = MagicMock()
    product_page.open = AsyncMock()
    product_page.collect_summary = AsyncMock(return_value={"title": "商品", "sku": "A", "price": "20"})
    target = MagicMock()
    target.screenshot = AsyncMock()
    target.close = AsyncMock()
    product_page.start_upload = AsyncMock(return_value=target)
    platform_page = MagicMock(last_submission_detail="提交成功", last_visible_text="提交成功")
    for name in ("confirm_superboss", "wait_for_store_selection", "select_shop", "accept_service_agreement", "accept_copy_commitment", "accept_commitment", "start_upload"):
        setattr(platform_page, name, AsyncMock())
    platform_page.confirm_listing = AsyncMock(return_value=outcome)
    monkeypatch.setattr(app, "ProductDetailPage", lambda page: product_page)
    monkeypatch.setattr(app, "UploadPlatformPage", lambda page: platform_page)
    monkeypatch.setattr(app, "_save_submission_evidence", AsyncMock(return_value="diagnostic.json"))
    return product_page, platform_page


def test_real_batch_initializes_once_and_writes_two_submission_states(settings, monkeypatch):
    fake_upload_pages(monkeypatch, ListingStatus.SUBMITTED)
    init = MagicMock(wraps=app.initialize)
    monkeypatch.setattr(app, "initialize", init)
    statuses = []
    original = SourceProductRepository.upsert_many

    def track(repo, products):
        products = list(products)
        statuses.extend(p.listing_status for p in products)
        return original(repo, products)

    monkeypatch.setattr(SourceProductRepository, "upsert_many", track)
    arrivals = [ShopNewArrival(item_id=str(i), item_url=f"https://cs.17zwd.com/item/{i}") for i in (1, 2)]
    args = batch_args()
    args.confirm_publish = "PUBLISH"
    assert asyncio.run(app._process_arrivals(MagicMock(), arrivals, args, settings,
                                            Platform.PINDUODUO, "test", "商品")) == 0
    assert init.call_count == 1
    assert statuses == [ListingStatus.UNCERTAIN, ListingStatus.SUBMITTED] * 2


def test_resume_after_interrupted_submit_never_resends(settings, monkeypatch):
    product_page, platform_page = fake_upload_pages(monkeypatch, ListingStatus.SUBMITTED)
    platform_page.confirm_listing.side_effect = asyncio.CancelledError()
    arrival = ShopNewArrival(item_id="1", item_url="https://cs.17zwd.com/item/1")
    args = batch_args()
    args.confirm_publish, args.item_ids = "PUBLISH", "1"
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(app._process_arrivals(MagicMock(), [arrival], args, settings,
                                         Platform.PINDUODUO, "test", "商品"))
    args.resume_batch = BatchJournal.recent(settings.database_path)[0]["batch_id"]
    assert asyncio.run(app._process_arrivals(MagicMock(), [arrival], args, settings,
                                            Platform.PINDUODUO, "test", "商品")) == 1
    assert product_page.open.await_count == 1
    assert platform_page.confirm_listing.await_count == 1
    _, items = BatchJournal(settings.database_path, args.resume_batch).load()
    assert items[0]["result"]["状态"] == "提交结果待核实"
