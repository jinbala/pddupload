from __future__ import annotations

import re
from contextlib import suppress
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from playwright.async_api import Page

from pdd_listing_automation.pages.feedback import SUCCESS, visible_feedback

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

TITLE_FIELDS_JS = r"""() => {
  const controls = Array.from(document.querySelectorAll('input, textarea'));
  const rows = [];
  for (const [index, el] of controls.entries()) {
    if (el.disabled || el.readOnly || !el.getClientRects().length) continue;
    const labels = el.labels ? Array.from(el.labels).map(label => label.textContent || '') : [];
    const item = el.closest('.ant-form-item, [role="group"], label, td, li');
    const nearby = item ? (item.innerText || '').slice(0, 240) : '';
    const identity = [el.placeholder, el.name, el.id, el.getAttribute('aria-label')]
      .filter(Boolean).join(' ');
    const exactLabel = labels.some(label => /^(商品标题|商品名称)$/.test(label.trim()));
    const identityMatch = /(商品标题|商品名称|goods[_-]?name|product[_-]?title)/i.test(identity);
    const nearbyMatch = /商品标题/.test(nearby);
    if (exactLabel || identityMatch || nearbyMatch) {
      rows.push({index, value: el.value || ''});
    }
  }
  return {rows};
}"""


def _compact_title_text(value: str) -> str:
    return re.sub(r"[\s\u3000]+", "", value or "")


def _source_shop_prefixes(source_shop: str, source_title: str = "") -> list[str]:
    """生成来源档口可能出现在商品标题开头的前缀，最长优先。"""
    shop = _compact_title_text(source_shop)
    title = _compact_title_text(source_title)
    if not shop:
        return []
    prefixes = {shop}
    common = []
    for left, right in zip(shop, title):
        if left != right:
            break
        common.append(left)
    if len(common) >= 2:
        prefixes.add("".join(common))
    for suffix in ("旗舰店", "专营店", "专卖店", "供应链", "网购", "网店", "服饰", "服装",
                   "女装", "男装", "商贸", "贸易", "制衣", "针织", "纺织", "店"):
        if shop.endswith(suffix) and len(shop) - len(suffix) >= 2:
            prefixes.add(shop[:-len(suffix)])
    return sorted(prefixes, key=len, reverse=True)


def remove_source_shop_prefix(title: str, source_shop: str, source_title: str = "") -> str:
    """只删除标题开头的来源档口名，不修改标题主体或中间出现的同名词。"""
    original = (title or "").strip()
    for prefix in _source_shop_prefixes(source_shop, source_title):
        pattern = r"^\s*" + r"[\s\u3000]*".join(re.escape(char) for char in prefix)
        match = re.match(pattern, original)
        if not match:
            continue
        cleaned = re.sub(r"^[\s\u3000\-_|:：/]+", "", original[match.end():])
        if cleaned:
            return cleaned
    return original


class PddDraftPage:
    """拼多多商家后台「草稿箱」，完成「改价 + 提交并上架」最后一步。"""

    def __init__(self, page: Page) -> None:
        self.page = page

    async def open_draft_box(self) -> None:
        await self.page.goto(DRAFT_BOX_URL, wait_until="domcontentloaded")
        await self.page.get_by_text("草稿箱", exact=False).first.wait_for(timeout=20_000)
        await self.page.wait_for_function(r"""() => {
            const text = document.body.innerText;
            return /商品编码[：:]\s*\d+|暂无数据|暂无商品|暂无草稿/.test(text)
                && ![...document.querySelectorAll('.ant-spin-spinning')].some(el => el.getClientRects().length);
        }""", timeout=20_000)

    async def verify_shop(self, shop_name: str) -> None:
        matches = self.page.get_by_text(shop_name, exact=True)
        if await matches.count() != 1 or not await matches.is_visible():
            raise RuntimeError("无法唯一核对当前拼多多店铺名称，停止草稿发布")

    async def wait_price_fields(self, edit_page: Page) -> None:
        await edit_page.wait_for_function(
            "() => { const result = (" + PRICE_FIELDS_JS + ")(); return result.error || result.rows.length > 0; }",
            timeout=20_000,
        )

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
        if not re.fullmatch(r"\d+", sku):
            raise ValueError("草稿商品编码必须为数字")
        marker = self.page.get_by_text(re.compile(rf"^\s*商品编码\s*[：:]\s*{re.escape(sku)}\s*$"))
        count = await marker.count()
        if count == 0:
            return None
        if count != 1:
            raise RuntimeError(f"草稿商品编码 {sku} 匹配到多条记录，停止编辑")
        row = marker.locator("xpath=ancestor::*[.//a[normalize-space()='编辑']][1]")
        edit = row.get_by_role("link", name="编辑", exact=True)
        if await edit.count() != 1:
            raise RuntimeError(f"草稿商品编码 {sku} 的编辑入口不唯一，停止编辑")
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

    async def remove_source_shop_prefix(
        self, edit_page: Page, source_shop: str, source_title: str = "",
    ) -> str:
        """清理草稿标题开头的来源档口名；标题控件不唯一时停止提交。"""
        result = await edit_page.evaluate(TITLE_FIELDS_JS)
        rows = result.get("rows", [])
        if len(rows) != 1:
            raise RuntimeError("商品标题输入框无法唯一定位，停止清理标题和提交")
        field = edit_page.locator("input, textarea").nth(rows[0]["index"])
        current = await field.input_value()
        cleaned = remove_source_shop_prefix(current, source_shop, source_title)
        if cleaned == current:
            return current
        await field.fill(cleaned)
        await field.blur()
        if await field.input_value() != cleaned:
            raise RuntimeError("商品标题写入后校验不一致，停止提交")
        return cleaned

    async def set_prices(self, edit_page: Page, source_price: float, multiple: float) -> None:
        """最低拼单价=货源价×倍数，其余规格和单买价保留原来的金额差。"""
        price, factor = Decimal(str(source_price)), Decimal(str(multiple))
        if not all(v.is_finite() and v > 0 for v in (price, factor)):
            raise ValueError("货源价和售价倍数必须是有限正数")
        target_pin = (price * factor).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if target_pin <= 0:
            raise ValueError("计算后的售价必须大于零")
        rows = await self._price_fields(edit_page)
        originals: list[tuple[dict, Decimal, Decimal]] = []
        for row in rows:
            try:
                pin, dan = Decimal(row["pin"]), Decimal(row["dan"])
            except InvalidOperation as exc:
                raise RuntimeError("原规格价格无效，停止改价和提交") from exc
            if not all(v.is_finite() and v > 0 for v in (pin, dan)) or dan < pin:
                raise RuntimeError("原规格价格无效，停止改价和提交")
            originals.append((row, pin, dan))
        anchor = min(pin for _, pin, _ in originals)
        changes: list[tuple[int, str]] = []
        for row, pin, dan in originals:
            new_pin = (target_pin + pin - anchor).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            new_dan = (target_pin + dan - anchor).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            changes.extend([(row["pin_index"], str(new_pin)), (row["dan_index"], str(new_dan))])
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
            if await edit_page.get_by_text("请向右滑块完成拼图", exact=False).first.is_visible():
                print("    ⚠ 请在弹出的浏览器里手动拖滑块完成验证……")
                if not await self._wait_slider_gone(edit_page):
                    return "uncertain"
            before = set(await visible_feedback(edit_page))
            await edit_page.get_by_role("button", name="提交并上架").click(timeout=8_000)
            for _ in range(120):
                if await edit_page.get_by_text("请向右滑块完成拼图", exact=False).first.is_visible():
                    print("    ⚠ 请在弹出的浏览器里手动拖滑块完成验证……")
                    if not await self._wait_slider_gone(edit_page):
                        return "uncertain"
                feedback = set(await visible_feedback(edit_page)) - before
                if any(SUCCESS.fullmatch(text) for text in feedback):
                    return "success"
                if "goods_list" in edit_page.url:
                    return "uncertain"
                await edit_page.wait_for_timeout(1_000)
        except Exception:  # noqa: BLE001 — 提交后异常不能触发自动重试
            return "uncertain"
        return "uncertain"

    async def _wait_slider_gone(self, edit_page: Page) -> bool:
        for _ in range(180):  # 最多等 3 分钟人工拖
            if not await edit_page.get_by_text("请向右滑块完成拼图", exact=False).first.is_visible():
                await edit_page.wait_for_timeout(1_000)
                return True
            await edit_page.wait_for_timeout(1_000)
        return False

    async def close_page(self, page: Page) -> None:
        with suppress(Exception):
            await page.close()
