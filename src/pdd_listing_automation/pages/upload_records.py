from __future__ import annotations

from contextlib import suppress

from playwright.async_api import Locator, Page
from pydantic import ValidationError

from pdd_listing_automation.models import Platform, SourceProduct

UPLOAD_RECORD_URL = "https://i.17zwd.com/user/uploadRecord"


class LoginRequiredError(RuntimeError):
    pass


class UploadRecordsPage:
    def __init__(self, page: Page, login_wait_seconds: int = 300) -> None:
        self.page = page
        self.login_wait_seconds = login_wait_seconds

    async def open(self) -> None:
        await self.page.goto(UPLOAD_RECORD_URL, wait_until="domcontentloaded")
        if await self.is_login_page():
            await self.page.get_by_text("登录", exact=True).first.wait_for()

    async def is_login_page(self) -> bool:
        return "/login" in (self.page.url or "")

    async def wait_until_authenticated(self) -> None:
        if not await self.is_login_page():
            return
        await self.page.wait_for_url(
            "**/user/uploadRecord",
            timeout=self.login_wait_seconds * 1000,
        )
        await self.page.wait_for_load_state("domcontentloaded")

    async def select_platform(self, platform: Platform) -> None:
        tab = self.page.get_by_role("tab", name=platform.upload_record_tab_name)
        if await tab.count() != 1:
            raise RuntimeError(f"平台标签定位失败：{platform.display_name}")
        await tab.click()
        await self.page.wait_for_timeout(500)

    async def collect_current_page(self, platform: Platform) -> list[SourceProduct]:
        rows = self.page.locator("table tbody tr[data-row-key]")
        raw_rows = await rows.evaluate_all(
            """
            (elements, platformValue) => elements.map((row) => {
              const image = row.querySelector("td:first-child img");
              const info = image?.parentElement?.nextElementSibling;
              const infoNodes = info
                ? Array.from(info.children).map((node) => (node.textContent || "").trim())
                : [];
              const cells = Array.from(row.querySelectorAll(":scope > td"));
              const similar = row.querySelector('a[href*="SearchSimilar"]');
              const href = similar?.href || "";
              let goodsId = "";
              try {
                goodsId = new URL(href, window.location.origin).searchParams.get("goods_id") || "";
              } catch {
                goodsId = "";
              }
              return {
                source_goods_id: goodsId,
                source_row_key: row.getAttribute("data-row-key") || "",
                title: image?.alt?.trim() || infoNodes[0] || "",
                source_sku: infoNodes[1] || "",
                price_text: infoNodes[2] || "",
                source_shop: infoNodes[3] || "",
                source_category: (cells[1]?.textContent || "").trim(),
                uploaded_at_text: (cells[2]?.textContent || "").trim(),
                image_url: image?.src || "",
                find_similar_url: href,
                source_platform: platformValue,
              };
            })
            """,
            platform.value,
        )

        products: list[SourceProduct] = []
        for raw in raw_rows:
            try:
                products.append(SourceProduct.model_validate(raw))
            except ValidationError as exc:
                raise RuntimeError(f"上传记录字段解析失败：{raw}") from exc
        return products

    def next_page_button(self) -> Locator:
        return self.page.get_by_role("button", name="right", exact=True)

    async def has_next_page(self) -> bool:
        button = self.next_page_button()
        return await button.count() == 1 and await button.is_enabled()

    async def go_to_next_page(self) -> None:
        if not await self.has_next_page():
            raise RuntimeError("没有可用的下一页")
        # 等加载遮罩消失，避免点击被拦截或按钮在点击过程中变禁用
        with suppress(Exception):
            await self.page.locator(".ant-spin-spinning").first.wait_for(
                state="hidden", timeout=5_000
            )
        if not await self.has_next_page():
            raise RuntimeError("没有可用的下一页")
        try:
            await self.next_page_button().click(timeout=10_000)
        except Exception as exc:
            raise RuntimeError("没有可用的下一页") from exc
        await self.page.wait_for_timeout(700)
