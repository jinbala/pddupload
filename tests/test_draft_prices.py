import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from playwright.async_api import async_playwright

from pdd_listing_automation.pages.pdd_draft import PddDraftPage


def test_price_editing_in_local_browser():
    async def scenario():
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page()
            try:
                await page.set_content("""
                    <input id="unrelated" value="123.45">
                    <table><thead><tr><th>规格</th><th>单买价</th><th>拼单价</th><th>库存</th></tr></thead>
                    <tbody>
                    <tr><td>红色</td><td><input value="25"></td><td><input value="20"></td><td><input value="100.00"></td></tr>
                    <tr><td>蓝色</td><td><input value="28.50"></td><td><input value="21.50"></td><td><input value="200"></td></tr>
                    </tbody></table>
                """)
                draft = PddDraftPage(page)
                await draft.set_prices(page, 20.01, 1.5)
                values = await page.locator("input").evaluate_all("els => els.map(el => el.value)")
                assert values == ["123.45", "35.02", "30.02", "100.00", "37.02", "30.02", "200"]

                # 缺少明确表头时拒绝改价，不把任意数字输入框当成价格。
                await page.set_content('<input value="10.00"><input value="20.00">')
                with pytest.raises(RuntimeError, match="未识别"):
                    await draft.set_prices(page, 20, 1.5)
                assert await page.locator("input").first.input_value() == "10.00"

                # 全部规格先校验：后面的无效规格不能造成前面规格已被部分修改。
                await page.set_content("""
                    <table><thead><tr><th>拼单价</th><th>单买价</th></tr></thead><tbody>
                    <tr><td><input value="20"></td><td><input value="25"></td></tr>
                    <tr><td><input value="30"></td><td><input value="10"></td></tr>
                    </tbody></table>
                """)
                with pytest.raises(RuntimeError, match="原规格价格无效"):
                    await draft.set_prices(page, 40, 1.5)
                assert await page.locator("input").first.input_value() == "20"
                for factor in (0, -1, float("inf"), float("nan")):
                    with pytest.raises(ValueError, match="有限正数"):
                        await draft.set_prices(page, 20, factor)
            finally:
                await browser.close()
    asyncio.run(scenario())


def test_draft_submit_does_not_retry_when_result_is_unknown():
    page = MagicMock()
    page.get_by_text.return_value.count = AsyncMock(return_value=0)
    page.get_by_role.return_value.click = AsyncMock()
    page.locator.return_value.inner_text = AsyncMock(return_value="处理中")
    page.wait_for_timeout = AsyncMock()
    page.url = "https://mms.pinduoduo.com/goods/goods_add"
    assert asyncio.run(PddDraftPage(page).submit_and_wait(page)) == "uncertain"
    assert page.get_by_role.return_value.click.await_count == 1


def test_draft_submit_requires_explicit_success():
    page = MagicMock()
    page.get_by_text.return_value.count = AsyncMock(return_value=0)
    page.get_by_role.return_value.click = AsyncMock()
    page.locator.return_value.inner_text = AsyncMock(return_value="发布成功")
    assert asyncio.run(PddDraftPage(page).submit_and_wait(page)) == "success"
