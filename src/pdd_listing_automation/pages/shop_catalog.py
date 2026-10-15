from __future__ import annotations

import re
from urllib.parse import urlparse

from playwright.async_api import Page

from pdd_listing_automation.models import ShopNewArrival
from pdd_listing_automation.pages.waits import list_signature, wait_for_list

# 档口「在售商品」全量列表：每个商品是一个 <a href*='/item/'>，商品卡片是其最近的
# [class*=goodsItem] 祖先；标题在图片 title 属性，价格/上新日期在卡片文本。
_EXTRACT_ITEMS_JS = """
() => Array.from(document.querySelectorAll("a[href*='/item/']")).map((a) => {
  const img = a.querySelector('img');
  const card = a.closest('[class*=goodsItem]');
  const txt = (card?.textContent || '').trim().replace(/\\s+/g, ' ');
  return {
    item_id: (a.href.match(/item\\/(\\d+)/) || [])[1] || '',
    item_url: a.href,
    title: (img?.title || img?.alt || '').trim(),
    price_text: (txt.match(/¥(\\d+(?:\\.\\d{1,2})?)/) || [])[1] || '',
    on_shelf_date_text: (txt.match(/(\\d{1,2}-\\d{1,2})上新/) || [])[1] || '',
    image_url: img?.src || '',
  };
})
"""

# 从档口商品页顶部抓档口名：优先常见选择器，兜底用页面标题（去掉站点后缀）。
_EXTRACT_SHOP_NAME_JS = """
() => {
  const clean = (s) => (s || '').replace(/\\s+/g, ' ').trim();
  for (const sel of [
    '.shop-name', '.shopName', '.shop-name-box', '.seller-name', '.sellerName',
    '.shop-info .name', '.shop-info .shop-name', '.shopHeader .name',
    '.shop-title', '.shop-info h1', 'h1',
  ]) {
    const el = document.querySelector(sel);
    const t = clean(el && el.textContent);
    if (t && t.length <= 40) return t;
  }
  let title = clean(document.title);
  title = title.replace(/[-_|·]?\\s*(17zwd|17\\u7f51|十七\\u7f51|批发|货源).*$/i, '').trim();
  return title;
}
"""


def _normalize_shop_url(url: str) -> str:
    """把任意档口链接规范成「在售商品」分页 URL：/shop/{id}.htm?page=1&search=y。

    关键在 `search=y`——缺它或带过期 spm 时，站点会落到「首页」只显示少量新款。
    """
    parsed = urlparse(url)
    match = re.search(r"/shop/(\d+)", parsed.path)
    if not match:
        raise RuntimeError(f"无法从链接提取档口 ID：{url}")
    return f"https://{parsed.netloc}/shop/{match.group(1)}.htm?page=1&search=y"


class ShopCatalogPage:
    """档口「在售商品」全量列表，自动把链接规范成 page=N&search=y。"""

    def __init__(self, page: Page) -> None:
        self.page = page
        self.base_url = ""
        self.shop_name = ""

    async def open(self, url: str, start_page: int = 1) -> None:
        self.base_url = _normalize_shop_url(url)  # 始终从 page=1 开始
        await self.page.goto(self.base_url, wait_until="domcontentloaded")
        await wait_for_list(self.page, "a[href*='/item/']")
        self.shop_name = await self.page.evaluate(_EXTRACT_SHOP_NAME_JS)
        # 从第 1 页用「下一页」按钮逐页前进到起始页（URL 直接跳转会漏加载）
        for _ in range(max(0, start_page - 1)):
            await self.go_to_next_page()

    async def collect_current_page(self) -> list[ShopNewArrival]:
        raw = await self.page.evaluate(_EXTRACT_ITEMS_JS)
        arrivals = [ShopNewArrival.model_validate(item) for item in raw]
        for arrival in arrivals:
            arrival.shop_name = self.shop_name
        return arrivals

    async def go_to_next_page(self) -> None:
        """点分页「下一页」按钮翻页（URL 直接跳转会漏加载，点按钮才完整渲染）。"""
        pager_next = self.page.locator("li.ant-pagination-next")
        if await pager_next.count() != 1:
            raise RuntimeError("分页组件定位失败")
        if await pager_next.evaluate("el => el.classList.contains('ant-pagination-disabled')"):
            raise RuntimeError("已经是最后一页")
        previous = await list_signature(self.page, "a[href*='/item/']")
        await pager_next.locator("button").click()
        await wait_for_list(self.page, "a[href*='/item/']", previous)

    async def has_next_page(self) -> bool:
        pager_next = self.page.locator("li.ant-pagination-next")
        if await pager_next.count() != 1:
            raise RuntimeError("分页组件定位失败")
        return not await pager_next.evaluate("el => el.classList.contains('ant-pagination-disabled')")

    async def collect(self, pages: int = 1) -> list[ShopNewArrival]:
        arrivals: list[ShopNewArrival] = []
        for page_index in range(max(1, pages)):
            arrivals.extend(await self.collect_current_page())
            if page_index + 1 >= max(1, pages):
                break
            if not await self.has_next_page():
                break
            await self.go_to_next_page()
        return arrivals
