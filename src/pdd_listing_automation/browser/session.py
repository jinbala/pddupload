from __future__ import annotations

import subprocess
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from playwright.async_api import BrowserContext, Page, async_playwright

from pdd_listing_automation.config import Settings


def kill_stale_chrome(profile_dir: str) -> None:
    """杀掉残留的使用该 profile 的 Chrome 进程，避免启动时被锁。"""
    with suppress(Exception):
        subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                (
                    "Get-CimInstance Win32_Process | Where-Object { "
                    "$_.CommandLine -like '*browser-profile*' -and $_.Name -eq 'chrome.exe' "
                    "} | ForEach-Object { Stop-Process -Id $_.ProcessId -Force "
                    "-ErrorAction SilentlyContinue }"
                ),
            ],
            capture_output=True,
            timeout=60,
            check=False,
        )


class BrowserSession:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @asynccontextmanager
    async def open(self) -> AsyncIterator[tuple[BrowserContext, Page]]:
        self.settings.ensure_directories()
        kill_stale_chrome(str(self.settings.profile_dir))
        async with async_playwright() as playwright:
            context = await playwright.chromium.launch_persistent_context(
                user_data_dir=str(self.settings.profile_dir),
                headless=self.settings.headless,
                slow_mo=self.settings.slow_mo_ms,
                viewport={"width": 1440, "height": 960},
                accept_downloads=True,
            )
            try:
                # 持久化浏览器会恢复上次的标签页；关掉旧标签，只留一个干净页面，
                # 避免用户看到历史残留的档口首页等旧页面。
                old_pages = list(context.pages)
                page = await context.new_page()
                for old in old_pages:
                    with suppress(Exception):
                        await old.close()
                yield context, page
            finally:
                await context.close()
