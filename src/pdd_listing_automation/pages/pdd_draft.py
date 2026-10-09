from __future__ import annotations

from contextlib import suppress

from playwright.async_api import Page

DRAFT_BOX_URL = "https://mms.pinduoduo.com/goods/goods_list?activeKeyNew=key_7"


class PddDraftPage:
    """拼多多商家后台「草稿箱」，完成「改价 + 提交并上架」最后一步。"""

    def __init__(self, page: Page) -> None:
        self.page = page

    async def open_draft_box(self) -> None:
        await self.page.goto(DRAFT_BOX_URL, wait_until="domcontentloaded")
        await self.page.get_by_text("草稿箱", exact=False).first.wait_for(timeout=20_000)
        await self.page.wait_for_timeout(3_000)

    async def collect_skus(self) -> list[str]:
        """收集草稿箱里的商品编码（= 17zwd 商品 ID），按列表顺序去重。"""
        skus: list[str] = await self.page.evaluate(
            """() => {
              const txt = document.body.innerText;
              const out = [];
              const re = /商品编码[：:]\\s*(\\d+)/g;
              let m;
              while ((m = re.exec(txt))) out.push(m[1]);
              return out;
            }"""
        )
        seen: set[str] = set()
        ordered: list[str] = []
        for sku in skus:
            if sku not in seen:
                seen.add(sku)
                ordered.append(sku)
        return ordered

    async def edit_draft(self, sku: str) -> Page | None:
        """点商品编码=sku 的草稿的「编辑」，返回新打开的编辑页标签。"""
        marker = self.page.get_by_text(f"商品编码：{sku}", exact=False).first
        if await marker.count() != 1:
            return None
        row = marker.locator("xpath=ancestor::*[.//a[normalize-space()='编辑']][1]")
        edit = row.locator("a", has_text="编辑").first
        context = self.page.context
        before = set(context.pages)
        await edit.click()
        for _ in range(60):
            for p in context.pages:
                if p not in before and "goods_add" in (p.url or ""):
                    await p.wait_for_load_state("domcontentloaded")
                    return p
            await self.page.wait_for_timeout(250)
        return None

    async def read_price_anchor(self, edit_page: Page) -> tuple[float, float] | None:
        """读第一个规格的拼单价/单买价，返回 (拼单价, 单买价)。"""
        result = await edit_page.evaluate(
            """() => {
              const inputs = Array.from(document.querySelectorAll('input'));
              const prices = inputs.filter(i => /^\\d+\\.\\d+$/.test(i.value));
              if (prices.length < 2) return null;
              return [parseFloat(prices[0].value), parseFloat(prices[1].value)];
            }"""
        )
        if not result:
            return None
        return result[0], result[1]

    async def set_prices(self, edit_page: Page, source_price: float, multiple: float) -> None:
        """改所有规格价格：拼单价=货源价×倍数，单买价保持原差额。"""
        pin, dan = await self.read_price_anchor(edit_page) or (0.0, 0.0)
        gap = round(dan - pin, 2)
        target_pin = round(source_price * multiple, 2)
        target_dan = round(target_pin + gap, 2)
        # 价格输入框的索引（value 为带小数数字的 input 位置，顺序是 拼单价/单买价 交替）
        indices: list[int] = await edit_page.evaluate(
            """() => {
              const inputs = Array.from(document.querySelectorAll('input'));
              return inputs
                .map((el, i) => (/^\\d+\\.\\d+$/.test(el.value) ? i : -1))
                .filter(i => i >= 0);
            }"""
        )
        for pos, idx in enumerate(indices):
            value = target_pin if pos % 2 == 0 else target_dan
            await edit_page.locator("input").nth(idx).fill(f"{value:.2f}")

    async def submit_and_wait(self, edit_page: Page) -> str:
        """点「提交并上架」，处理滑块验证码（人工拖），返回结果状态。"""
        for _ in range(5):  # 最多 5 轮（滑块可能多次出现）
            if await edit_page.get_by_text("请向右滑块完成拼图", exact=False).count():
                print("    ⚠ 请在弹出的浏览器里手动拖滑块完成验证……")
                await self._wait_slider_gone(edit_page)
            try:
                await edit_page.get_by_role("button", name="提交并上架").click(timeout=8_000)
            except Exception:  # noqa: BLE001 — 点击可能被滑块验证码拦截，需重试
                await edit_page.wait_for_timeout(1_000)
                continue
            for _ in range(120):
                if await edit_page.get_by_text("请向右滑块完成拼图", exact=False).count():
                    print("    ⚠ 请在弹出的浏览器里手动拖滑块完成验证……")
                    await self._wait_slider_gone(edit_page)
                    break
                text = await edit_page.locator("body").inner_text()
                if any(k in text for k in ("提交成功", "上架成功", "发布成功", "已提交")):
                    return "success"
                if "goods_list" in edit_page.url:
                    return "redirected"
                await edit_page.wait_for_timeout(1_000)
        return "failed"

    async def _wait_slider_gone(self, edit_page: Page) -> None:
        for _ in range(180):  # 最多等 3 分钟人工拖
            if not await edit_page.get_by_text("请向右滑块完成拼图", exact=False).count():
                await edit_page.wait_for_timeout(1_000)
                return
            await edit_page.wait_for_timeout(1_000)

    async def close_page(self, page: Page) -> None:
        with suppress(Exception):
            await page.close()
