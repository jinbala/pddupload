import argparse
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from playwright.async_api import async_playwright

from pdd_listing_automation import app
from pdd_listing_automation.browser.session import profile_lock
from pdd_listing_automation.config import Settings
from pdd_listing_automation.models import ListingStatus, Platform, ShopNewArrival
from pdd_listing_automation.pages.upload_platform import (
    AlreadyListedError,
    UploadPlatformPage,
    UploadRejectedError,
)
from pdd_listing_automation.storage.database import connect


@pytest.fixture
def settings(tmp_path):
    return Settings(
        root_dir=tmp_path, data_dir=tmp_path / "data", profile_dir=tmp_path / "profile",
        artifacts_dir=tmp_path / "artifacts", database_path=tmp_path / "data/db.sqlite3",
    )


@pytest.mark.parametrize("status", ["prepared", "submitted", "uncertain"])
def test_batch_records_submission_states_and_continues_on_uncertain(settings, monkeypatch, capsys, status):
    arrivals = [
        ShopNewArrival(item_id=str(i), item_url=f"https://market.example.com/item/{i}",
                       shop_name="来源档口", shop_main_category="女装", price_text="20")
        for i in (1, 2)
    ]
    args = argparse.Namespace(
        target_shop="目标店", confirm_publish="" if status == "prepared" else "PUBLISH",
        item_ids="1,2", limit=0, delay_seconds=0, hold_open_seconds=0,
    )
    async def fake_upload(page, **kwargs):
        result = {"title": "详情标题", "sku": "ABC", "price": "22", "screenshot_path": "test.png",
                  "listing_status": status}
        # 单品服务负责提交记录；批次层只记录进度，不再重复写同一条记录。
        if status != "prepared":
            app._mark_listed(settings, kwargs["source_arrival"], Platform.PINDUODUO,
                             kwargs["target_shop"], result, ListingStatus(status))
        return result, None, status != "prepared"
    upload = AsyncMock(side_effect=fake_upload)
    monkeypatch.setattr(app, "run_single_upload", upload)
    exports = []
    monkeypatch.setattr(app, "export_workbook_xlsx", lambda sheets, *args: exports.extend(sheets))
    code = asyncio.run(app._process_arrivals(
        MagicMock(), arrivals, args, settings, Platform.PINDUODUO, "test", "商品",
    ))
    with connect(settings.database_path) as db:
        rows = db.execute("SELECT * FROM source_products").fetchall()
    sheets = dict(exports)
    if status == "prepared":
        assert rows == []
        assert sheets["上架结果"][0]["状态"] == "准备完成（未发布）"
    else:
        assert len(rows) == 2
        assert rows[0]["source_shop"] == "来源档口"
        assert rows[0]["source_sku"] == "ABC"
        assert rows[0]["price_text"] == "22"
        assert rows[0]["target_shop"] == "目标店"
        assert rows[0]["listing_status"] == status
    assert code == (1 if status == "uncertain" else 0)
    assert upload.await_count == 2
    assert len(sheets["上架结果"]) == 2
    if status == "uncertain":
        assert all(row["状态"] == "提交结果待核实" for row in sheets["上架结果"])
        assert "待核实商品 ID：1,2" in capsys.readouterr().out


@pytest.mark.parametrize("text,expected", [
    ("正在处理", ListingStatus.UNCERTAIN),
    ("提交成功", ListingStatus.SUBMITTED),
])
def test_confirmation_requires_positive_evidence(text, expected):
    page = MagicMock()
    frame = page.frame_locator.return_value.first
    frame.get_by_text.return_value.first.is_visible = AsyncMock(return_value=False)
    frame.get_by_role.return_value.count = AsyncMock(return_value=1)
    frame.get_by_role.return_value.click = AsyncMock()
    page.wait_for_timeout = AsyncMock()
    platform = UploadPlatformPage(page)
    platform._grab_context_text = AsyncMock(return_value=text)
    platform._visible_success_text = AsyncMock(return_value=text if text == "提交成功" else "")
    assert asyncio.run(platform.confirm_listing()) == expected


def test_closed_page_during_final_click_is_uncertain():
    page = MagicMock()
    frame = page.frame_locator.return_value.first
    frame.get_by_text.return_value.first.is_visible = AsyncMock(return_value=False)
    frame.get_by_role.return_value.count = AsyncMock(return_value=1)
    frame.get_by_role.return_value.click = AsyncMock(side_effect=RuntimeError("TargetClosed"))
    assert asyncio.run(UploadPlatformPage(page).confirm_listing()) == ListingStatus.UNCERTAIN


def test_profile_lock_rejects_concurrent_task_and_releases(tmp_path):
    profile = tmp_path / "browser-profile"
    with profile_lock(profile):
        with pytest.raises(RuntimeError, match="另一个任务"), profile_lock(profile):
            pytest.fail("second task acquired lock")
        with profile_lock(tmp_path / "other-profile"):
            pass
    with profile_lock(profile):
        pass


@pytest.mark.parametrize("rejected", [False, True])
def test_interrupted_submission_blocks_retry_but_explicit_rejection_does_not(settings, monkeypatch, rejected):
    product = MagicMock()
    product.open = AsyncMock()
    product.collect_summary = AsyncMock(return_value={"title": "test", "sku": "ABC", "price": "20"})
    target = MagicMock()
    target.screenshot = AsyncMock()
    product.start_upload = AsyncMock(return_value=target)
    monkeypatch.setattr(app, "ProductDetailPage", lambda page: product)
    platform = MagicMock()
    for name in ("confirm_superboss", "wait_for_store_selection", "select_shop",
                 "accept_service_agreement", "accept_copy_commitment", "accept_commitment", "start_upload"):
        setattr(platform, name, AsyncMock())
    platform.confirm_listing = AsyncMock(side_effect=(
        UploadRejectedError("授权已过期") if rejected else RuntimeError("interrupted")
    ))
    monkeypatch.setattr(app, "UploadPlatformPage", lambda page: platform)
    arrival = ShopNewArrival(item_id="1", item_url="https://market.example.com/item/1", shop_name="档口")
    kwargs = {
        "item_id": "1", "item_url": arrival.item_url, "platform": Platform.PINDUODUO,
        "target_shop": "目标店", "confirm_publish": "PUBLISH", "screenshot_stem": "test",
        "settings": settings, "source_arrival": arrival,
    }
    if rejected:
        with pytest.raises(UploadRejectedError):
            asyncio.run(app.run_single_upload(MagicMock(), **kwargs))
        with connect(settings.database_path) as db:
            assert db.execute("SELECT COUNT(*) FROM source_products").fetchone()[0] == 0
        return
    result, _, attempted = asyncio.run(app.run_single_upload(MagicMock(), **kwargs))
    assert attempted
    assert result["listing_status"] == ListingStatus.UNCERTAIN
    with connect(settings.database_path) as db:
        row = db.execute("SELECT * FROM source_products").fetchone()
        assert row["listing_status"] == ListingStatus.UNCERTAIN
        assert row["source_shop"] == "档口"
    with pytest.raises(AlreadyListedError):
        asyncio.run(app.run_single_upload(MagicMock(), **kwargs))
    assert product.open.await_count == 1


def test_hidden_duplicate_modal_does_not_skip_but_visible_modal_does():
    async def scenario():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.set_content("<iframe></iframe>")
                frame = page.frames[1]
                await frame.set_content("""
                    <div id="duplicate" style="display:none">重复铺货确认<button>取消</button></div>
                    <button onclick="document.querySelector('#result').textContent='提交成功'">确认上货</button>
                    <div id="result"></div>
                """)
                assert await frame.get_by_text("重复铺货确认", exact=False).count() == 1
                assert await UploadPlatformPage(page).confirm_listing() == ListingStatus.SUBMITTED
                await frame.evaluate("document.querySelector('#duplicate').style.display='block'")
                await frame.get_by_role("button", name="取消").evaluate(
                    "el => el.onclick = () => document.querySelector('#duplicate').style.display='none'"
                )
                with pytest.raises(AlreadyListedError, match="上传页面显示可见"):
                    await UploadPlatformPage(page).confirm_listing()
            finally:
                await browser.close()
    asyncio.run(scenario())


def test_submission_can_finish_after_original_five_second_window():
    page = MagicMock()
    frame = page.frame_locator.return_value.first
    frame.get_by_text.return_value.first.is_visible = AsyncMock(return_value=False)
    frame.get_by_role.return_value.count = AsyncMock(return_value=1)
    frame.get_by_role.return_value.click = AsyncMock()
    page.wait_for_timeout = AsyncMock()
    platform = UploadPlatformPage(page)
    platform._visible_success_text = AsyncMock(side_effect=[""] * 12 + ["提交成功"])
    assert asyncio.run(platform.confirm_listing()) == ListingStatus.SUBMITTED
    assert frame.get_by_role.return_value.click.await_count == 1
    assert platform._visible_success_text.await_count == 13


def test_visible_outer_feedback_is_detected_beyond_long_iframe_form():
    async def scenario():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.set_content('<div id="result"></div><iframe></iframe>')
                frame = page.frames[1]
                await frame.set_content(
                    '<div>' + '规格内容' * 6000 + '</div>'
                    '<p style="display:none">提交成功</p>'
                    '<button onclick="parent.document.querySelector(\'#result\').textContent=\'提交成功\'">'
                    '确认上货</button>'
                )
                platform = UploadPlatformPage(page)
                assert await platform.confirm_listing() == ListingStatus.SUBMITTED
                assert "页面反馈" in platform.last_submission_detail
            finally:
                await browser.close()
    asyncio.run(scenario())


def test_closed_page_still_produces_submission_diagnostics(settings):
    target = MagicMock()
    target.screenshot = AsyncMock(side_effect=RuntimeError("TargetClosed"))
    path = asyncio.run(app._save_submission_evidence(
        target, settings, "test", "1", Platform.PINDUODUO, "目标店",
        ListingStatus.UNCERTAIN, "提交后页面关闭", "",
    ))
    import json
    from pathlib import Path

    evidence = json.loads(Path(path).read_text(encoding="utf-8"))
    assert evidence["detail"] == "提交后页面关闭"
    assert "TargetClosed" in evidence["screenshot_error"]
