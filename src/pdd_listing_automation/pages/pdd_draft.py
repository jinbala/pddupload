from __future__ import annotations

from contextlib import suppress
from decimal import ROUND_HALF_UP, Decimal

from playwright.async_api import Page

DRAFT_BOX_URL = "https://mms.pinduoduo.com/goods/goods_list?activeKeyNew=key_7"

# 只识别规格表格中的价格列，不按页面上数字输入框的排列猜测字段。
PRICE_FIELDS_JS = r"""() => {
  const inputs = Array.from(document.querySelectorAll('input'));
  const out = [];
  for (const table of document.querySelectorAll('table')) {
    if (!table.getClientRects().length) continue;
    const headers = Array.from(table.querySelectorAll('thead th'));
    const pin = headers.map((h, i) => /拼单价|团购价/.test(h.textContent) ? i : -1).filter(i => i >= 0);
    const dan = headers.map((h, i) => /单买价/.test(h.textContent) ? i : -1).filter(i => i >= 0);
    if (!pin.length && !dan.length) continue;
    if (pin.length !== 1 || dan.length !== 1 || pin[0] === dan[0] ||
        headers.some(h => h.colSpan !== 1 || h.rowSpan !== 1))
      return {error: '规格价格表头不明确'};
    for (const row of table.querySelectorAll('tbody tr')) {
      const cells = Array.from(row.children);
      if (!row.querySelector('input')) continue;
      if (cells.length !== headers.length || cells.some(c => c.colSpan !== 1 || c.rowSpan !== 1))
        return {error: '规格价格表含合并单元格，无法可靠定位'};
      const group = cells[pin[0]].querySelectorAll('input');
      const single = cells[dan[0]].querySelectorAll('input');
      if (group.length !== 1 || single.length !== 1 ||
          [group[0], single[0]].some(el => el.disabled || el.readOnly))
        return {error: '规格价格输入框不明确或不可编辑'};
      out.push({pin_index: inputs.indexOf(group[0]), dan_index: inputs.indexOf(single[0]),
                pin: group[0].value, dan: single[0].value});
    }
  }
  return {rows: out};
}"""


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
        rows = await self._price_fields(edit_page)
        return float(rows[0]["pin"]), float(rows[0]["dan"])

    async def _price_fields(self, edit_page: Page) -> list[dict]:
        result = await edit_page.evaluate(PRICE_FIELDS_JS)
        if result.get("error") or not result.get("rows"):
            raise RuntimeError(result.get("error") or "未识别到规格价格表，停止改价和提交")
        return result["rows"]

    async def set_prices(self, edit_page: Page, source_price: float, multiple: float) -> None:
        """改所有规格价格：拼单价=货源价×倍数，单买价保持原差额。"""
        price, factor = Decimal(str(source_price)), Decimal(str(multiple))
        if not all(v.is_finite() and v > 0 for v in (price, factor)):
            raise ValueError("货源价和售价倍数必须是有限正数")
        target_pin = (price * factor).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if target_pin <= 0:
            raise ValueError("计算后的售价必须大于零")
        rows = await self._price_fields(edit_page)
        changes: list[tuple[int, str]] = []
        for row in rows:
            pin, dan = Decimal(row["pin"]), Decimal(row["dan"])
            if not all(v.is_finite() and v > 0 for v in (pin, dan)) or dan < pin:
                raise RuntimeError("原规格价格无效，停止改价和提交")
            target_dan = (target_pin + dan - pin).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            changes.extend([(row["pin_index"], str(target_pin)), (row["dan_index"], str(target_dan))])
        # 先验证全部规格，再写入；逐项回读，发现控件拒绝修改时不提交。
        for index, value in changes:
            field = edit_page.locator("input").nth(index)
            await field.fill(value)
            await field.blur()
            if Decimal(await field.input_value()) != Decimal(value):
                raise RuntimeError("价格写入后校验不一致，停止提交")

    async def submit_and_wait(self, edit_page: Page) -> str:
        """只提交一次；超时、跳转或验证码后无明确结果时返回待核实。"""
        try:
            if await edit_page.get_by_text("请向右滑块完成拼图", exact=False).count():
                print("    ⚠ 请在弹出的浏览器里手动拖滑块完成验证……")
                if not await self._wait_slider_gone(edit_page):
                    return "uncertain"
            await edit_page.get_by_role("button", name="提交并上架").click(timeout=8_000)
            for _ in range(120):
                if await edit_page.get_by_text("请向右滑块完成拼图", exact=False).count():
                    print("    ⚠ 请在弹出的浏览器里手动拖滑块完成验证……")
                    if not await self._wait_slider_gone(edit_page):
                        return "uncertain"
                text = await edit_page.locator("body").inner_text()
                if any(k in text for k in ("提交成功", "上架成功", "发布成功", "已提交")):
                    return "success"
                if "goods_list" in edit_page.url:
                    return "uncertain"
                await edit_page.wait_for_timeout(1_000)
        except Exception:  # noqa: BLE001 — 提交后异常不能触发自动重试
            return "uncertain"
        return "uncertain"

    async def _wait_slider_gone(self, edit_page: Page) -> bool:
        for _ in range(180):  # 最多等 3 分钟人工拖
            if not await edit_page.get_by_text("请向右滑块完成拼图", exact=False).count():
                await edit_page.wait_for_timeout(1_000)
                return True
            await edit_page.wait_for_timeout(1_000)
        return False

    async def close_page(self, page: Page) -> None:
        with suppress(Exception):
            await page.close()
