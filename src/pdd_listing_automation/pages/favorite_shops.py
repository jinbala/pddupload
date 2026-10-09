from __future__ import annotations

import re

from playwright.async_api import Locator, Page

from pdd_listing_automation.models import ShopNewArrival

FAVORITE_SHOPS_URL = "https://i.17zwd.com/user/favouriteShops"

# 从关注档口页提取每个档口卡片的原始文本和其上新款链接。档口卡片 = 同时包含
# “进入档口”链接（a[href*='/shop/']）和若干商品链接（a[href*='/item/']）的最小祖先。
_EXTRACT_CARDS_JS = """
() => {
  const out = [];
  for (const enter of Array.from(document.querySelectorAll("a[href*='/shop/']"))) {
    let card = enter;
    for (let i = 0; i < 10 && card; i++) {
      card = card.parentElement;
      if (card && card.querySelectorAll("a[href*='/item/']").length > 0) break;
    }
    if (!card) continue;
    const card_text = (card.textContent || '').trim().replace(/\\s+/g, ' ');
    const items = Array.from(card.querySelectorAll("a[href*='/item/']")).map((a) => {
      const img = a.querySelector('img');
      return {
        id: (a.href.match(/item\\/(\\d+)/) || [])[1] || '',
        href: a.href,
        text: (a.textContent || '').trim().replace(/\\s+/g, ' '),
        image: img ? img.src : '',
      };
    });
    out.push({ card_text, items });
  }
  return out;
}
"""


def parse_shop_card(card_text: str, items: list[dict]) -> list[ShopNewArrival]:
    """把单个档口卡片的文本和商品链接解析成 ShopNewArrival 列表（纯函数，便于单测）。"""
    shop_name = card_text.split("排行")[0].strip()
    rank_match = re.search(r"排行\s*：\s*第(\d+)名", card_text)
    main_match = re.search(r"主营\s*：\s*(.*?)\s*地址", card_text)

    arrivals: list[ShopNewArrival] = []
    for item in items:
        text = item.get("text", "")
        price_match = re.search(r"¥(\d+(?:\.\d{1,2})?)", text)
        date_match = re.search(r"(\d{1,2}-\d{1,2})\s*上新", text)
        arrivals.append(
            ShopNewArrival(
                item_id=item.get("id", ""),
                item_url=item.get("href", ""),
                price_text=price_match.group(1) if price_match else "",
                on_shelf_date_text=date_match.group(1) if date_match else "",
                image_url=item.get("image", ""),
                shop_name=shop_name,
                shop_rank=rank_match.group(1) if rank_match else "",
                shop_main_category=main_match.group(1).strip() if main_match else "",
            )
        )
    return arrivals


class FavoriteShopsPage:
    def __init__(self, page: Page, login_wait_seconds: int = 300) -> None:
        self.page = page
        self.login_wait_seconds = login_wait_seconds

    async def open(self) -> None:
        await self.page.goto(FAVORITE_SHOPS_URL, wait_until="domcontentloaded")
        if await self.is_login_page():
            await self.page.get_by_text("登录", exact=True).first.wait_for()

    async def is_login_page(self) -> bool:
        return "/login" in (self.page.url or "")

    async def wait_until_authenticated(self) -> None:
        if not await self.is_login_page():
            return
        await self.page.wait_for_url(
            "**/user/favouriteShops",
            timeout=self.login_wait_seconds * 1000,
        )
        await self.page.wait_for_load_state("domcontentloaded")

    async def collect_current_page(self) -> list[ShopNewArrival]:
        # 页面内容由 JS 异步渲染，先等第一个上新款链接出现再采集。
        await self.page.locator("a[href*='/item/']").first.wait_for(timeout=15_000)
        raw_cards = await self.page.evaluate(_EXTRACT_CARDS_JS)
        arrivals: list[ShopNewArrival] = []
        for raw in raw_cards:
            arrivals.extend(parse_shop_card(raw["card_text"], raw["items"]))
        return arrivals

    async def collect_new_arrivals(self, pages: int = 1) -> list[ShopNewArrival]:
        arrivals: list[ShopNewArrival] = []
        for page_index in range(max(1, pages)):
            arrivals.extend(await self.collect_current_page())
            if page_index + 1 >= max(1, pages):
                break
            if not await self.has_next_page():
                break
            await self.go_to_next_page()
        return arrivals

    def next_page_locator(self) -> Locator:
        return self.page.locator("li.ant-pagination-next")

    async def has_next_page(self) -> bool:
        locator = self.next_page_locator()
        if await locator.count() != 1:
            return False
        return not await locator.evaluate(
            "(el) => el.classList.contains('ant-pagination-disabled')"
        )

    async def go_to_next_page(self) -> None:
        if not await self.has_next_page():
            raise RuntimeError("没有可用的下一页")
        await self.next_page_locator().locator("button").click()
        await self.page.wait_for_timeout(700)
