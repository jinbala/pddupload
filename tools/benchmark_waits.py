"""仅测试本地模拟页面，不访问店铺或执行发布。"""
import argparse
import asyncio
import json
from time import perf_counter

from playwright.async_api import async_playwright

from pdd_listing_automation.pages.waits import list_signature, wait_for_list


async def benchmark(pages: int, render_ms: int) -> dict:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            durations = {}
            for strategy in ("fixed_1500ms", "wait_for_changed_rows"):
                await page.set_content('<a href="/item/0">模拟商品</a>')
                start = perf_counter()
                for index in range(1, pages + 1):
                    previous = await list_signature(page, 'a')
                    await page.evaluate("""({index, delay}) => {
                        setTimeout(() => document.querySelector('a').href = '/item/' + index, delay);
                    }""", {"index": index, "delay": render_ms})
                    if strategy == "fixed_1500ms":
                        await page.wait_for_timeout(1500)
                    else:
                        await wait_for_list(page, 'a', previous)
                    assert await page.locator('a').get_attribute('href') == f'/item/{index}'
                durations[strategy] = round(perf_counter() - start, 3)
            return {"pages": pages, "simulated_render_ms": render_ms, **durations,
                    "note": "Local pagination benchmark only; excludes network and publish throttling."}
        finally:
            await browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pages", type=int, default=10)
    parser.add_argument("--render-ms", type=int, default=80)
    args = parser.parse_args()
    if args.pages < 1 or not 0 <= args.render_ms < 1500:
        parser.error("pages >= 1; 0 <= render-ms < 1500")
    print(json.dumps(asyncio.run(benchmark(args.pages, args.render_ms)), indent=2))
