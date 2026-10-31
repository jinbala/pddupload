import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import AsyncMock, MagicMock

import pytest
from playwright.async_api import async_playwright

from pdd_listing_automation import app
from pdd_listing_automation.config import Settings
from pdd_listing_automation.models import ListingStatus, Platform, ShopNewArrival, SourceProduct
from pdd_listing_automation.pages.pdd_draft import PddDraftPage
from pdd_listing_automation.pages.product_detail import ProductDetailPage
from pdd_listing_automation.pages.upload_platform import AlreadyListedError, UploadPlatformPage
from pdd_listing_automation.storage.database import connect, initialize
from pdd_listing_automation.storage.drafts import DraftLedger
from pdd_listing_automation.storage.repositories import SourceProductRepository


@pytest.fixture
def settings(tmp_path):
    result = Settings(
        root_dir=tmp_path, data_dir=tmp_path / "data", profile_dir=tmp_path / "profile",
        artifacts_dir=tmp_path / "artifacts", database_path=tmp_path / "data/db.sqlite3",
    )
    result.ensure_directories()
    initialize(result.database_path)
    return result


def product(market="cs.17zwd.com", shop="目标店"):
    return SourceProduct(
        source_goods_id="10001", source_row_key="10001", title="商品",
        source_url=f"https://{market}/item/10001" if market else "",
        source_platform=Platform.PINDUODUO, target_shop=shop,
        listing_status=ListingStatus.UNCERTAIN,
    )


def test_concurrent_submission_reservation_allows_only_one_writer(settings):
    barrier = Barrier(2)

    def reserve():
        with connect(settings.database_path) as db:
            repo = SourceProductRepository(db)
            # 两个任务都在旧的预检阶段看到没有记录。
            assert repo.find_listing_record(Platform.PINDUODUO, "目标店", "10001", "cs.17zwd.com") is None
            barrier.wait(timeout=5)
            return repo.reserve_submission(product()) is None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: reserve(), range(2)))
    assert sorted(results) == [False, True]
    with connect(settings.database_path) as db:
        assert SourceProductRepository(db).count() == 1


def test_reservation_blocks_legacy_unknown_scope_but_preserves_other_shops(settings):
    with connect(settings.database_path) as db:
        repo = SourceProductRepository(db)
        repo.upsert_many([product("", "")])
        assert repo.reserve_submission(product()) is not None
        assert repo.count() == 1
    other_path = settings.data_dir / "other.db"
    initialize(other_path)
    with connect(other_path) as db:
        repo = SourceProductRepository(db)
        assert repo.reserve_submission(product()) is None
        assert repo.reserve_submission(product(shop="另一个店")) is None
        assert repo.reserve_submission(product(market="gz.17zwd.com")) is None
        assert repo.count() == 3


def test_draft_reservation_blocks_unknown_market_and_concurrent_writers(settings):
    barrier = Barrier(2)

    def reserve():
        ledger = DraftLedger(settings.database_path, " 目标店 ", "")
        barrier.wait(timeout=5)
        return ledger.reserve("10001")

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: reserve(), range(2)))
    assert sorted(results) == ["", "uncertain"]
    assert DraftLedger(settings.database_path, "目标店", "cs.17zwd.com").reserve("10001") == "uncertain"
    assert DraftLedger(settings.database_path, "另一个店", "").reserve("10001") == ""


def test_whitespace_shop_does_not_bypass_selection_dedup(settings, monkeypatch):
    with connect(settings.database_path) as db:
        SourceProductRepository(db).upsert_many([product()])
    upload = AsyncMock()
    monkeypatch.setattr(app, "run_single_upload", upload)
    args = argparse.Namespace(
        target_shop=" 目标店 ", confirm_publish="PUBLISH", item_ids="10001",
        limit=0, delay_seconds=0, hold_open_seconds=0,
    )
    arrival = ShopNewArrival(item_id="10001", item_url="https://cs.17zwd.com/item/10001")
    assert asyncio.run(app._process_arrivals(
        MagicMock(), [arrival], args, settings, Platform.PINDUODUO, "test", "商品",
    )) == 0
    upload.assert_not_awaited()


def test_new_record_after_precheck_stops_before_submit(settings, monkeypatch):
    detail = MagicMock()
    detail.open = AsyncMock()
    detail.collect_summary = AsyncMock(return_value={"title": "商品", "sku": "A", "price": "20"})
    target = MagicMock()
    target.screenshot = AsyncMock()
    detail.start_upload = AsyncMock(return_value=target)
    platform = MagicMock()
    for name in ("confirm_superboss", "wait_for_store_selection", "select_shop",
                 "accept_service_agreement", "accept_copy_commitment", "start_upload"):
        setattr(platform, name, AsyncMock())

    async def other_task_submits():
        with connect(settings.database_path) as db:
            SourceProductRepository(db).upsert_many([
                product().model_copy(update={"listing_status": ListingStatus.SUBMITTED})
            ])

    platform.accept_commitment = AsyncMock(side_effect=other_task_submits)
    monkeypatch.setattr(app, "ProductDetailPage", lambda _: detail)
    monkeypatch.setattr(app, "UploadPlatformPage", lambda _: platform)
    with pytest.raises(AlreadyListedError, match="提交前防重"):
        asyncio.run(app.run_single_upload(
            MagicMock(), item_id="10001", item_url="https://cs.17zwd.com/item/10001",
            platform=Platform.PINDUODUO, target_shop="目标店", confirm_publish="PUBLISH",
            screenshot_stem="test", settings=settings,
        ))
    platform.start_upload.assert_not_awaited()
    with connect(settings.database_path) as db:
        assert db.execute("SELECT listing_status FROM source_products").fetchone()[0] == "submitted"


@pytest.mark.parametrize("outer", [False, True])
def test_duplicate_modal_after_final_click_is_cancelled(outer):
    async def scenario():
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.set_content('<div id="outer"></div><iframe></iframe>')
                frame = page.frames[1]
                modal = '''<div><span>重复铺货确认</span>
                    <button onclick="this.parentElement.remove()">取消</button>
                    <button onclick="window.resent=true">确认重复铺货</button></div>'''
                await frame.set_content('''<div style="display:none">重复铺货确认</div>
                    <button id="submit">确认上货</button><div id="inner"></div>''')
                await frame.evaluate("""({outer, modal}) => {
                    document.querySelector('#submit').onclick = () => {
                        window.clicks = (window.clicks || 0) + 1;
                        const target = outer ? parent.document.querySelector('#outer') : document.querySelector('#inner');
                        target.innerHTML = modal;
                    };
                }""", {"outer": outer, "modal": modal})
                with pytest.raises(AlreadyListedError, match="已取消"):
                    await UploadPlatformPage(page).confirm_listing()
                assert await frame.evaluate("window.clicks") == 1
                assert not await frame.evaluate("Boolean(window.resent)")
                assert not await page.evaluate("Boolean(window.resent)")
                assert await page.locator('#outer button').count() == 0
                assert await frame.locator('#inner button').count() == 0
            finally:
                await browser.close()
    asyncio.run(scenario())


def test_draft_edit_matches_complete_id_and_refuses_duplicates():
    async def scenario():
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.set_content('''
                    <div><span>商品编码：100010</span><a href="#" onclick="window.wrong=true">编辑</a></div>
                    <div><span>商品编码: 10001</span><a href="#" onclick="window.right=true">编辑</a></div>
                ''')
                page.wait_for_timeout = AsyncMock()
                assert await PddDraftPage(page).edit_draft("10001") is None
                assert await page.evaluate("window.right")
                assert not await page.evaluate("Boolean(window.wrong)")
                await page.set_content('''
                    <div><span>商品编码：10001</span><a href="#">编辑</a></div>
                    <div><span>商品编码：10001</span><a href="#">编辑</a></div>
                ''')
                with pytest.raises(RuntimeError, match="多条记录"):
                    await PddDraftPage(page).edit_draft("10001")
            finally:
                await browser.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["new", "old", "text-only", "hidden", "unrelated-controls"])
def test_task_started_requires_new_visible_complete_modal(mode):
    async def scenario():
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.set_content('<iframe></iframe>')
                frame = page.frames[1]
                await frame.set_content('<div id="result"></div><button>确认上货</button>')
                modal = ('<div><p>开始上货任务</p><button>继续上货</button>'
                         '<button>查看上货记录</button></div>')
                if mode == "text-only":
                    modal = '<p>开始上货任务</p>'
                elif mode == "hidden":
                    modal = '<div style="display:none">' + modal + '</div>'
                elif mode == "unrelated-controls":
                    await frame.locator('body').evaluate(
                        "el => el.insertAdjacentHTML('beforeend', '<button>继续上货</button><button>查看上货记录</button>')"
                    )
                    modal = '<p>开始上货任务</p>'
                await frame.evaluate("""({modal, old}) => {
                    const result = document.querySelector('#result');
                    if (old) result.innerHTML = modal;
                    document.querySelector('button').onclick = () => result.innerHTML = modal;
                }""", {"modal": modal, "old": mode == "old"})
                page.wait_for_timeout = AsyncMock()
                status = await UploadPlatformPage(page).confirm_listing()
                assert status == (ListingStatus.SUBMITTED if mode == "new" else ListingStatus.UNCERTAIN)
            finally:
                await browser.close()
    asyncio.run(scenario())


def test_mismatched_product_link_stops_before_browser_actions(settings, monkeypatch):
    detail = MagicMock()
    monkeypatch.setattr(app, "ProductDetailPage", detail)
    with pytest.raises(ValueError, match="不一致"):
        asyncio.run(app.run_single_upload(
            MagicMock(), item_id="10001", item_url="https://cs.17zwd.com/item/100010",
            platform=Platform.PINDUODUO, target_shop="目标店", confirm_publish="PUBLISH",
            screenshot_stem="test", settings=settings,
        ))
    detail.assert_not_called()


def test_product_upload_ignores_old_upload_tabs():
    page = MagicMock()
    page.locator.return_value.count = AsyncMock(return_value=0)
    upload = page.get_by_text.return_value
    upload.count = AsyncMock(return_value=1)
    upload.click = AsyncMock()
    upload.wait_for = AsyncMock()
    upload.first.wait_for = AsyncMock()
    old, new = MagicMock(), MagicMock()
    old.url = new.url = "https://t-onekey.17zwd.com/main/publish"
    new.wait_for_load_state = AsyncMock()
    page.context.pages = [page, old]

    async def open_new_tab():
        page.context.pages.append(new)

    upload.first.click = AsyncMock(side_effect=open_new_tab)
    assert asyncio.run(ProductDetailPage(page).start_upload(Platform.PINDUODUO)) is new
