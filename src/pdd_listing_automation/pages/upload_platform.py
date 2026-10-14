from __future__ import annotations

import re
from contextlib import suppress

from playwright.async_api import Locator, Page

from pdd_listing_automation.models import ListingStatus


def _is_closed_error(exc: Exception) -> bool:
    """判断异常是否是「页面/上下文/浏览器被关闭」导致的。"""
    return "closed" in str(exc).lower() or "TargetClosed" in type(exc).__name__


class AlreadyListedError(RuntimeError):
    """商品已存在代销关系（已铺过），应跳过而不是重试。"""


class UploadPlatformPage:
    def __init__(self, page: Page) -> None:
        self.page = page
        self.last_submission_detail = "尚未点击确认上货"
        self.last_visible_text = ""

    def store_dialog(self):
        return self.page.frame_locator("iframe").get_by_role(
            "dialog",
            name="选择上货店铺",
        )

    async def _grab_context_text(self, limit: int = 300) -> str:
        """失败时尽力抓一段可见文字，用于定位真实原因（授权过期/上限/风控提示）。"""
        parts: list[str] = []
        for locator in (
            self.page.frame_locator("iframe").first.locator("body"),
            self.page.locator("body"),
        ):
            with suppress(Exception):
                text = await locator.inner_text(timeout=2_000)
                flat = " ".join(text.split())
                if flat:
                    parts.append(flat[: max(1, limit // 2)])
        return " | ".join(parts)[:limit]

    async def _visible_success_text(self) -> str:
        """直接查找可见反馈，避免长规格表把页尾或外层通知挤出文本截断范围。"""
        pattern = re.compile(r"^(?:提交成功|任务创建成功|上传成功|上货成功)(?:$|[\s！!。，,:：])")
        for root in (self.page.frame_locator("iframe").first, self.page):
            candidates = root.get_by_text(pattern)
            for index in range(await candidates.count()):
                candidate = candidates.nth(index)
                if await candidate.is_visible():
                    return await candidate.inner_text(timeout=1_000)
        return ""

    async def confirm_superboss(self) -> None:
        await self.page.wait_for_load_state("domcontentloaded")
        if "/platform" in (self.page.url or ""):
            superboss = self.page.get_by_text("超级店长", exact=True)
            if await superboss.count() != 1:
                raise RuntimeError("超级店长应用选项定位失败")
            await superboss.click()

            confirm = self.page.get_by_role("button", name="确 认")
            if await confirm.count() != 1:
                raise RuntimeError("上传应用确认按钮定位失败")
            await confirm.click()

        await self.page.wait_for_url(
            re.compile(r".*/main/publish(?:\?.*)?$"),
            timeout=20_000,
        )

    async def wait_for_store_selection(self) -> None:
        dialog = self.store_dialog()
        for attempt in range(2):
            try:
                await dialog.wait_for(timeout=30_000)
                return
            except Exception:
                if attempt == 0:
                    # iframe 可能加载慢/失败，重载当前 t-onekey 页再等一次
                    await self.page.reload(wait_until="domcontentloaded")
                    await self.page.wait_for_timeout(2_000)
                else:
                    raise

    async def select_shop(self, shop_name: str) -> None:
        """按店铺名选店。优先精确匹配；店名带后缀（如「某某店新版 V3.0…」）时退化为包含匹配。

        这样拼多多和抖音即使店名相近也能区分开。
        """
        dialog = self.store_dialog()
        row = await self._find_shop_row(dialog, shop_name, exact=True)
        if row is None:
            row = await self._find_shop_row(dialog, shop_name, exact=False)
        if row is None:
            raise RuntimeError(
                f"目标店铺定位失败：{shop_name}；页面文字：{await self._grab_context_text()}"
            )

        checkbox = row.locator('input[type="checkbox"]')
        if await checkbox.count() != 1:
            raise RuntimeError(f"目标店铺复选框定位失败：{shop_name}")
        if not await checkbox.is_checked():
            await row.locator("span.ant-checkbox-inner").evaluate("(el) => el.click()")
        if not await checkbox.is_checked():
            raise RuntimeError(f"目标店铺选中失败：{shop_name}")

    async def _find_shop_row(self, dialog: Locator, shop_name: str, exact: bool) -> Locator | None:
        """在选店弹窗里找含 shop_name 且带复选框的店铺行；找不到返回 None。"""
        candidates = dialog.get_by_text(shop_name, exact=exact)
        matches: list[Locator] = []
        for index in range(await candidates.count()):
            ancestor = candidates.nth(index).locator(
                "xpath=ancestor::*[.//input[@type='checkbox']][1]"
            )
            if await ancestor.count() == 1:
                matches.append(ancestor)
        if len(matches) > 1:
            raise RuntimeError(f"店铺名称匹配到多条记录：{shop_name}，请使用完整名称")
        return matches[0] if matches else None

    async def accept_commitment(self) -> None:
        """签署「商家搬家承诺书」。若当前平台/店铺没有该承诺书（如抖音）则跳过。"""
        dialog = self.store_dialog()
        commitment = dialog.get_by_text("已签署商家搬家承诺书", exact=False)
        if await commitment.count() != 1:
            # 抖音等平台可能没有该承诺书，直接跳过而不是报错
            return

        row = commitment.locator("xpath=ancestor::*[.//input[@type='checkbox']][1]")
        checkbox = row.locator('input[type="checkbox"]')
        if await checkbox.count() != 1:
            raise RuntimeError("商家搬家承诺书复选框定位失败")
        if not await checkbox.is_checked():
            await row.locator("span.ant-checkbox-inner").evaluate("(el) => el.click()")
        if not await checkbox.is_checked():
            raise RuntimeError("商家搬家承诺书确认失败")

    async def start_upload(self) -> None:
        dialog = self.store_dialog()
        button = dialog.get_by_role("button", name="开始上传")
        if await button.count() != 1:
            raise RuntimeError(
                f"开始上传按钮定位失败；页面文字：{await self._grab_context_text()}"
            )
        await button.click()
        # 点完进入预览页，类目需要 2-5 秒自动匹配；太快点「确认上货」会匹配不到类目导致上货失败
        with suppress(Exception):
            await self.page.wait_for_timeout(5_000)

    async def confirm_listing(self) -> ListingStatus:
        """点「确认上货」提交任务。

        start_upload 已经等了类目自动匹配的时间；这里等「确认上货」出现并点击。
        点「确认上货」之前标签页被关 = 上货未完成（失败）；
        点击后必须看到明确提交提示；关页或没有结果均返回待核实，不冒充上架成功。
        """
        frame = self.page.frame_locator("iframe").first
        confirm = frame.get_by_role("button", name="确认上货")

        for _ in range(60):
            # 已存在代销关系：弹「重复铺货确认」，点取消跳过（弹窗可能稍后才出现，所以每轮都查）
            try:
                duplicate = await frame.get_by_text("重复铺货确认", exact=False).first.is_visible()
            except Exception as exc:
                if _is_closed_error(exc):
                    raise RuntimeError("等待确认上货时页面被关闭，上货未完成")
                raise
            if duplicate:
                for name in ("取 消", "取消", "取消本次操作"):
                    cancel = frame.get_by_role("button", name=name)
                    if await cancel.count():
                        await cancel.first.click()
                        with suppress(Exception):
                            await self.page.wait_for_timeout(1_000)
                        break
                raise AlreadyListedError("上传页面显示可见的「重复铺货确认」，已取消本次操作")

            # 点「确认上货」提交任务
            try:
                if await confirm.count() == 1:
                    try:
                        await confirm.click()
                    except Exception:  # noqa: BLE001 — 提交阶段任何异常均不能盲目重发
                        # 点击过程中断开连接，无法确认是否已发送，禁止直接重试。
                        self.last_submission_detail = "点击确认上货时出现异常，无法确认是否已发送"
                        return ListingStatus.UNCERTAIN
                    self.last_submission_detail = "已点击确认上货，正在等待页面反馈"
                    break
            except Exception as exc:
                if _is_closed_error(exc):
                    raise RuntimeError("等待确认上货时页面被关闭，上货未完成")
                raise

            try:
                await self.page.wait_for_timeout(500)
            except Exception as exc:
                if _is_closed_error(exc):
                    raise RuntimeError("等待确认上货时页面被关闭，上货未完成")
                raise
        else:
            raise RuntimeError(
                f"确认上货按钮定位失败；页面文字：{await self._grab_context_text()}"
            )

        # 点完后检查错误和明确提交提示；仅有页面关闭无法证明已成功。
        error_keywords = (
            "重复铺货确认", "授权已过期", "授权过期", "已过期", "已达上限",
            "达到上限", "操作频繁", "无权限",
        )
        for _ in range(60):  # 最多 30 秒；超级店长异步提交可能超过 5 秒
            try:
                success_text = await self._visible_success_text()
                if success_text:
                    self.last_visible_text = success_text
                    self.last_submission_detail = f"页面反馈：{success_text}；仍需核验目标平台是否上架"
                    return ListingStatus.SUBMITTED
                text = await self._grab_context_text(limit=20_000)
                self.last_visible_text = text
            except Exception as exc:  # noqa: BLE001 — 提交后的读取异常只能视为结果未知
                self.last_submission_detail = f"读取提交反馈时异常：{type(exc).__name__}: {exc}"
                return ListingStatus.UNCERTAIN
            for keyword in error_keywords:
                if keyword in text:
                    raise RuntimeError(f"确认上货后出现异常（「{keyword}」）：{text[:200]}")
            if any(keyword in text for keyword in ("提交成功", "任务创建成功", "上传成功", "上货成功")):
                self.last_submission_detail = "页面显示明确提交成功提示，仍需核验目标平台是否上架"
                return ListingStatus.SUBMITTED
            try:
                await self.page.wait_for_timeout(500)
            except Exception as exc:
                if _is_closed_error(exc):
                    self.last_submission_detail = "点击确认上货后页面关闭，未读取到明确结果；需核对上货日志"
                    return ListingStatus.UNCERTAIN
                raise
        self.last_submission_detail = "点击确认上货后等待 30 秒，未检测到明确提交提示"
        return ListingStatus.UNCERTAIN
