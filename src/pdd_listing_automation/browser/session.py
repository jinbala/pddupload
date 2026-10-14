from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager, suppress
from pathlib import Path

from playwright.async_api import BrowserContext, Page, async_playwright

from pdd_listing_automation.config import Settings


@contextmanager
def profile_lock(profile_dir: Path) -> Iterator[None]:
    """同一浏览器目录仅允许运行一个任务；进程退出时由操作系统释放锁。"""
    lock_path = profile_dir.resolve().with_name(f"{profile_dir.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        if lock_path.stat().st_size == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("该浏览器配置正在被另一个任务使用，请先结束该任务。") from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class BrowserSession:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @asynccontextmanager
    async def open(self) -> AsyncIterator[tuple[BrowserContext, Page]]:
        self.settings.ensure_directories()
        with profile_lock(self.settings.profile_dir):
            async with self._open_browser() as session:
                yield session

    @asynccontextmanager
    async def _open_browser(self) -> AsyncIterator[tuple[BrowserContext, Page]]:
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
