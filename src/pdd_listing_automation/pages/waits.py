"""等待列表实际更新，快页面立即继续，慢页面在超时前持续等待。"""
from __future__ import annotations

from playwright.async_api import Page

SIGNATURE_JS = """els => JSON.stringify(els.map(el => [
    el.getAttribute('href'), el.getAttribute('data-row-key'), el.textContent.trim()
]))"""


async def list_signature(page: Page, selector: str) -> str:
    return await page.locator(selector).evaluate_all(SIGNATURE_JS)


async def wait_for_list(page: Page, selector: str, previous: str | None = None) -> None:
    await page.wait_for_function("""({selector, previous}) => {
        const visible = el => el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden';
        if ([...document.querySelectorAll('.ant-spin-spinning')].some(visible)) return false;
        const els = [...document.querySelectorAll(selector)];
        const empty = [...document.querySelectorAll('.ant-empty-description')].some(visible);
        if (!els.length && !empty) return false;
        const signature = JSON.stringify(els.map(el => [el.getAttribute('href'),
            el.getAttribute('data-row-key'), el.textContent.trim()]));
        return previous === null || signature !== previous;
    }""", arg={"selector": selector, "previous": previous}, timeout=20_000, polling=100)
