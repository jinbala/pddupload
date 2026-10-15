from __future__ import annotations

import json
import re
import traceback
from contextlib import suppress
from datetime import UTC, datetime

from playwright.async_api import Page

from pdd_listing_automation.config import Settings
from pdd_listing_automation.models import ListingStatus, Platform


async def _record_failure_evidence(
    page: Page, settings: Settings, item_id: str, error: Exception
) -> None:
    """失败时把浏览器各页截图落盘，并把错误与堆栈写入 failures.log，便于排查。"""
    timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    # 截图失败（如页面/浏览器已关闭）不影响写日志
    with suppress(Exception):
        for index, target in enumerate(page.context.pages):
            tag = "main" if target is page else f"tab{index}"
            url_part = re.sub(r"[^0-9A-Za-z.-]+", "_", target.url or "about:blank")[:40]
            path = settings.artifacts_dir / f"failure-{item_id}-{timestamp}-{tag}-{url_part}.png"
            await target.screenshot(path=str(path), full_page=False)
    # 只保存页面控件结构，不读取 Cookie、登录存储或输入框内容。
    controls = []
    with suppress(Exception):
        for target in page.context.pages:
            for frame in target.frames:
                with suppress(Exception):
                    snapshot = await frame.locator("body").evaluate("""() => ({
                        checkboxes: [...document.querySelectorAll('input[type="checkbox"]')].slice(0, 40).map(el => {
                            const row = el.closest('tr, [role="row"], label') || el.parentElement;
                            return {checked: el.checked, disabled: el.disabled,
                                inputClass: el.className, rowTag: row?.tagName,
                                rowClass: row?.className,
                                rowText: (row?.innerText || '').trim().slice(0, 180)};
                        }),
                        textareas: [...document.querySelectorAll('textarea')].slice(0, 10).map(el => ({
                            placeholder: el.getAttribute('placeholder'), visible: !!el.getClientRects().length
                        }))
                    })""", timeout=2_000)
                    controls.append({"page": target.url.split("?")[0], "controls": snapshot})
        diagnostic_path = settings.artifacts_dir / f"failure-{item_id}-{timestamp}-controls.json"
        diagnostic_path.write_text(json.dumps(controls, ensure_ascii=False, indent=2), encoding="utf-8")
    log_path = settings.artifacts_dir / "failures.log"
    with suppress(OSError), log_path.open("a", encoding="utf-8") as handle:
        handle.write(f"[{timestamp}] id={item_id} {type(error).__name__}: {error}\n")
        handle.write("".join(traceback.format_exception(error)))
        handle.write("\n")


async def _save_submission_evidence(
    target: Page, settings: Settings, stem: str, item_id: str,
    platform: Platform, target_shop: str, status: ListingStatus, detail: str, visible_text: str,
) -> str:
    """在关闭上传页之前记录最终反馈；关闭的页面也要留下关页原因。"""
    settings.ensure_directories()
    timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")
    evidence_path = settings.artifacts_dir / f"{stem}-result-{timestamp}.json"
    screenshot_path = evidence_path.with_suffix(".png")
    evidence = {
        "item_id": item_id, "platform": platform.value, "target_shop": target_shop,
        "status": status.value, "detail": detail, "visible_text": visible_text,
        "observed_at": datetime.now(UTC).isoformat(), "screenshot_path": "",
    }
    try:
        await target.screenshot(path=str(screenshot_path), full_page=False, timeout=5_000)
        evidence["screenshot_path"] = str(screenshot_path)
    except Exception as exc:  # noqa: BLE001 — 页面已关闭也必须保留文字诊断
        evidence["screenshot_error"] = f"{type(exc).__name__}: {exc}"
    try:
        evidence_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        print(f"  提交诊断保存失败：{exc}")
        return ""
    return str(evidence_path)
