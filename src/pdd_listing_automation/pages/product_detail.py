from __future__ import annotations

import re

from playwright.async_api import Page

from pdd_listing_automation.models import Platform

DEFAULT_MARKET_HOST = "gz.17zwd.com"


class ProductDetailPage:
    def __init__(self, page: Page) -> None:
        self.page = page

    async def open(self, item_id: str, host: str | None = None) -> None:
        # 商品 ID 按市场站点隔离（如池尾站 cs.17zwd.com）。传入完整 URL 时直接打开，
        # 否则按 host（缺省广州站）拼接。
        if item_id.startswith(("http://", "https://")):
            url = item_id
        else:
            url = f"https://{host or DEFAULT_MARKET_HOST}/item/{item_id}"
        await self.page.goto(url, wait_until="domcontentloaded")

    async def collect_summary(self, item_id: str) -> dict[str, str]:
        title = (await self.page.title()).removesuffix(" - 17网").strip()
        page_text = await self.page.locator("body").inner_text()
        price_match = re.search(r"批发\s*¥([\d.]+)", page_text)
        sku_match = re.search(r"货号\s*\n\s*([^\n]+)", page_text)
        return {
            "item_id": item_id,
            "title": title,
            "price": price_match.group(1) if price_match else "",
            "sku": sku_match.group(1).strip() if sku_match else "",
        }

    async def start_upload(self, platform: Platform) -> Page:
        side_bar = self.page.locator('div[class*="silderBarContainer-"]')
        if await side_bar.count():
            await side_bar.evaluate_all("(elements) => elements.forEach((el) => el.style.display = 'none')")

        upload = self.page.get_by_text("一键上传", exact=True)
        if await upload.count() != 1:
            raise RuntimeError("商品详情页的一键上传按钮定位失败")
        await upload.click()

        await self.page.get_by_text("请选择要上传的平台", exact=True).wait_for(timeout=10_000)
        platform_choice = self.page.get_by_text(platform.display_name, exact=True)
        try:
            await platform_choice.first.wait_for(state="visible", timeout=10_000)
        except Exception as exc:
            raise RuntimeError(f"{platform.display_name}平台选项未显示") from exc
        context = self.page.context
        await platform_choice.first.click()

        for _ in range(50):
            # 点击平台后可能直接进 /main/publish（已记住默认应用），也可能先进 /platform
            # 选择应用；两者都在 t-onekey.17zwd.com 上，统一在这里兜住。
            for candidate in context.pages:
                if "t-onekey.17zwd.com" in (candidate.url or ""):
                    await candidate.wait_for_load_state("domcontentloaded")
                    return candidate
            if "t-onekey.17zwd.com" in (self.page.url or ""):
                return self.page
            await self.page.wait_for_timeout(200)

        raise RuntimeError(f"未进入{platform.display_name}上传应用页面")
