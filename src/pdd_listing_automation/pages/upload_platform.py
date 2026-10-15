from __future__ import annotations

import re
from contextlib import suppress
from time import monotonic

from playwright.async_api import Locator, Page, expect

from pdd_listing_automation.models import ListingStatus
from pdd_listing_automation.pages.feedback import REJECTION, SUCCESS, visible_feedback


def _is_closed_error(exc: Exception) -> bool:
    """判断异常是否是「页面/上下文/浏览器被关闭」导致的。"""
    return "closed" in str(exc).lower() or "TargetClosed" in type(exc).__name__


class AlreadyListedError(RuntimeError):
    """商品已存在代销关系（已铺过），应跳过而不是重试。"""

    def __init__(self, message: str, listing_status: ListingStatus | None = None):
        super().__init__(message)
        self.listing_status = listing_status


class UploadRejectedError(RuntimeError):
    """页面明确拒绝提交，区别于网络中断等结果未知的情况。"""


class UploadPlatformPage:
    def __init__(self, page: Page) -> None:
        self.page = page
        self.last_submission_detail = "尚未点击确认上货"
        self.last_visible_text = ""
        self._selected_checkbox: Locator | None = None
        self._selected_shop_row: Locator | None = None
        self._feedback_before: set[str] = set()

    async def _feedback(self) -> list[str]:
        result: list[str] = []
        # 一个 frame 一次浏览器调用，避免按节点往返读取。
        for frame in self.page.frames:
            result.extend(await visible_feedback(frame))
        return result

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
        feedback = set(await self._feedback()) - self._feedback_before
        self.last_visible_text = " | ".join(sorted(feedback))
        for text in feedback:
            if REJECTION.match(text):
                raise UploadRejectedError(f"页面拒绝提交：{text}")
        return next((text for text in feedback if SUCCESS.fullmatch(text)), "")

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
        self._selected_checkbox = checkbox
        self._selected_shop_row = row
        # 只操作与目标店铺相同列表的店铺行，不遍历整个弹窗的复选框。
        target_handle = await checkbox.element_handle()
        for field in await self._shop_checkboxes():
            if await field.evaluate("(el, target) => el === target", target_handle):
                continue
            if await field.is_checked():
                await self._set_checked(field, False)
        await self._set_checked(checkbox, True)
        self._selected_checkbox = checkbox
        await self.verify_shop_selection()

    async def _shop_checkboxes(self) -> list[Locator]:
        if self._selected_shop_row is None:
            raise RuntimeError("尚未定位店铺列表")
        body = self._selected_shop_row.locator("xpath=ancestor-or-self::tbody[1]")
        if await body.count():
            rows = body.locator(":scope > tr")
        else:
            # 兼容没有 table 的店铺卡片/label 列表；只看同级行。
            rows = self._selected_shop_row.locator("xpath=..").locator(":scope > *")
        fields = []
        target = await self._selected_checkbox.element_handle()
        for row in await rows.all():
            checkbox = row.locator('input[type="checkbox"]')
            if await checkbox.count() != 1:
                continue
            text = " ".join((await row.inner_text()).split())
            # 目标店铺名称即便包含这些词，也必须保留用于最终校验。
            if re.search(r"承诺|协议|签署|隐私|已阅读|已知晓|我已|全选", text) and not await checkbox.evaluate(
                "(el, target) => el === target", target,
            ):
                continue
            if not text:
                continue
            fields.append(checkbox)
        if not fields:
            raise RuntimeError("未识别到可靠的店铺列表，停止选店")
        return fields

    async def _set_checked(self, field: Locator, checked: bool) -> None:
        if await field.is_checked() == checked:
            return
        if not await field.is_enabled():
            raise RuntimeError("复选框不可修改，停止提交")
        if await field.is_visible():
            await field.click(timeout=5_000)
        else:
            # 部分版本隐藏原生 input，仅让 Ant Checkbox 的外观接收点击。
            visual = field.locator("xpath=..").locator("span.ant-checkbox-inner")
            if await visual.count() != 1:
                raise RuntimeError("复选框没有唯一可见点击入口，停止提交")
            await visual.click(timeout=5_000)
        await expect(field).to_be_checked(checked=checked, timeout=5_000)

    async def _commitment_checkbox(self) -> Locator | None:
        commitment = self.store_dialog().get_by_text("已签署商家搬家承诺书", exact=False)
        if await commitment.count() == 0:
            return None
        if await commitment.count() != 1:
            raise RuntimeError("商家搬家承诺书定位不唯一，停止提交")
        row = commitment.locator("xpath=ancestor::*[.//input[@type='checkbox']][1]")
        checkbox = row.locator('input[type="checkbox"]')
        if await checkbox.count() != 1:
            raise RuntimeError("商家搬家承诺书复选框定位失败")
        return checkbox

    async def verify_shop_selection(self) -> None:
        if self._selected_checkbox is None or not await self._selected_checkbox.is_checked():
            raise RuntimeError("未确认唯一目标店铺，停止提交")
        target = await self._selected_checkbox.element_handle()
        for field in await self._shop_checkboxes():
            if await field.is_checked() and not await field.evaluate("(el, target) => el === target", target):
                raise RuntimeError("检测到额外选中的店铺，停止提交")

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

    async def accept_service_agreement(self) -> bool:
        """仅在已确认发布的流程中，处理已识别的超级店长协议弹窗。"""
        handled = False
        for frame in self.page.frames:
            titles = frame.get_by_text("超级店长用户服务协议和隐私政策", exact=True)
            for title in await titles.all():
                if not await title.is_visible():
                    continue
                # 按已知标题及同一弹窗的按钮限定作用域，不点击其他“同意”按钮。
                modal = title.locator(
                    "xpath=ancestor::*[.//*[self::button or @role='button']"
                    "[contains(., '同意并继续')]][1]"
                )
                button = modal.get_by_role("button", name="同意并继续", exact=True)
                if await button.count() != 1:
                    raise RuntimeError("超级店长协议弹窗确认按钮不唯一，停止提交")
                await button.click(timeout=5_000)
                await expect(title).to_be_hidden(timeout=10_000)
                handled = True
        return handled

    async def accept_commitment(self) -> None:
        """协议弹窗和承诺书分开处理；等待受控复选框的异步状态更新。"""
        await self.accept_service_agreement()
        await self.accept_copy_commitment()
        checkbox = await self._commitment_checkbox()
        if checkbox is None or await checkbox.is_checked():
            return
        if not await checkbox.is_enabled():
            raise RuntimeError("商家搬家承诺书不可勾选，停止提交")
        # 正常点击遵守遮罩和可见性，不再用 JS 穿透协议弹窗点击底层。
        if await checkbox.is_visible():
            await checkbox.click(timeout=5_000)
        else:
            visual = checkbox.locator("xpath=..").locator("span.ant-checkbox-inner")
            if await visual.count() != 1:
                raise RuntimeError("商家搬家承诺书没有唯一可见点击入口")
            await visual.click(timeout=5_000)
        deadline = monotonic() + 10
        while monotonic() < deadline:
            await self.accept_service_agreement()
            await self.accept_copy_commitment()
            if await checkbox.is_checked():
                return
            await self.page.wait_for_timeout(100)
        raise RuntimeError("商家搬家承诺书确认失败：协议处理后仍未勾选，请查看失败截图")

    async def accept_copy_commitment(self) -> bool:
        """处理已识别的商品复制承诺书，内容只取当前弹窗显示的模板。"""
        handled = False
        for frame in self.page.frames:
            for title in await frame.get_by_text("商品复制承诺书", exact=True).all():
                if not await title.is_visible():
                    continue
                modal = title.locator(
                    "xpath=ancestor::*[.//textarea[@placeholder='请输入承诺内容']][1]"
                )
                field = modal.get_by_placeholder("请输入承诺内容", exact=True)
                button = modal.get_by_role("button", name="确认签署并继续", exact=True)
                if await field.count() != 1 or await button.count() != 1:
                    raise RuntimeError("商品复制承诺书结构不明确，停止提交")
                text = await modal.inner_text()
                match = re.search(
                    r"本店承诺在进行商品复制时[\s\S]*?本店将自行承担相应责任[。.]?", text,
                )
                if match is None:
                    raise RuntimeError("未识别到页面展示的承诺内容，请人工核对商品复制承诺书")
                template = match.group(0).strip()
                current = await field.input_value()
                if re.sub(r"\s+", "", current) != re.sub(r"\s+", "", template):
                    await field.fill(template)
                await expect(button).to_be_enabled(timeout=5_000)
                await button.click(timeout=5_000)
                await expect(title).to_be_hidden(timeout=10_000)
                handled = True
        return handled

    async def start_upload(self) -> None:
        await self.accept_service_agreement()
        await self.accept_copy_commitment()
        await self.verify_shop_selection()
        commitment = await self._commitment_checkbox()
        if commitment is not None and not await commitment.is_checked():
            raise RuntimeError("商家搬家承诺书尚未确认，停止提交")
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
                    self._feedback_before = set(await self._feedback())
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
        for _ in range(60):  # 最多 30 秒；超级店长异步提交可能超过 5 秒
            try:
                success_text = await self._visible_success_text()
                if success_text:
                    self.last_visible_text = success_text
                    self.last_submission_detail = f"页面反馈：{success_text}；仍需核验目标平台是否上架"
                    return ListingStatus.SUBMITTED
            except UploadRejectedError:
                raise
            except Exception as exc:  # noqa: BLE001 — 提交后的读取异常只能视为结果未知
                self.last_submission_detail = f"读取提交反馈时异常：{type(exc).__name__}: {exc}"
                return ListingStatus.UNCERTAIN
            try:
                await self.page.wait_for_timeout(500)
            except Exception as exc:
                if _is_closed_error(exc):
                    self.last_submission_detail = "点击确认上货后页面关闭，未读取到明确结果；需核对上货日志"
                    return ListingStatus.UNCERTAIN
                raise
        self.last_submission_detail = "点击确认上货后等待 30 秒，未检测到明确提交提示"
        self.last_visible_text = await self._grab_context_text(limit=2_000)
        return ListingStatus.UNCERTAIN
