from __future__ import annotations

import argparse
import math
import random
from datetime import UTC, datetime

from pdd_listing_automation.browser.session import BrowserSession
from pdd_listing_automation.config import Settings
from pdd_listing_automation.models import ListingStatus, Platform
from pdd_listing_automation.pages.pdd_draft import PddDraftPage
from pdd_listing_automation.reports.exporter import export_workbook_xlsx
from pdd_listing_automation.services.evidence import _save_submission_evidence
from pdd_listing_automation.storage.database import connect, initialize
from pdd_listing_automation.storage.drafts import DraftLedger


def load_source_prices(settings: Settings, target_shop: str = "", source_market: str = "") -> dict[str, float]:
    """同号跨站点或未知来源与已知价格冲突时跳过，避免拿另一款的成本改价。"""
    initialize(settings.database_path)
    query = ("SELECT source_goods_id, source_market, price_text FROM source_products "
             "WHERE source_platform='pinduoduo' AND price_text!=''")
    params = []
    if target_shop:
        query += " AND (target_shop=? OR target_shop='')"
        params.append(target_shop)
    if source_market:
        query += " AND (source_market=? OR source_market='')"
        params.append(source_market)
    query += " ORDER BY collected_at DESC"
    with connect(settings.database_path) as db:
        rows = db.execute(query, params).fetchall()
    prices: dict[str, dict[str, float]] = {}
    for row in rows:
        try:
            price = float(row["price_text"].replace("¥", "").replace("￥", "").strip())
        except ValueError:
            continue
        if math.isfinite(price) and price > 0:
            prices.setdefault(row["source_goods_id"], {}).setdefault(row["source_market"], price)
    result = {}
    for goods_id, markets in prices.items():
        known = set(markets) - {""}
        if len(known) > 1 or ("" in markets and len(set(markets.values())) > 1):
            continue
        result[goods_id] = next(iter(markets.values()))
    return result


async def publish_drafts(args: argparse.Namespace, settings: Settings) -> int:
    multiple = float(args.multiple)
    if not math.isfinite(multiple) or multiple <= 0:
        raise ValueError("售价倍数必须是有限正数")
    if settings.headless:
        raise RuntimeError("草稿箱需要处理人工验证码，请设置 PDD_AUTOMATION_HEADLESS=false")
    if not args.target_shop.strip():
        raise ValueError("草稿发布必须指定 --target-shop，以核对店铺及保存发布记录")
    prices = load_source_prices(settings, args.target_shop, args.source_market)
    ledger = DraftLedger(settings.database_path, args.target_shop, args.source_market)
    stem = "drafts-" + datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")
    rows = []
    failures = 0
    try:
        async with BrowserSession(settings).open() as (_, page):
            draft = PddDraftPage(page)
            await draft.open_draft_box()
            await draft.verify_shop(args.target_shop)
            skus = await draft.collect_skus()
            if args.limit:
                skus = skus[:args.limit]
            print(f"当前页待检查 {len(skus)} 个草稿；目标店铺：{args.target_shop}")
            for index, sku in enumerate(skus, 1):
                row = {"商品ID": sku, "目标店铺": args.target_shop, "来源站点": args.source_market,
                       "状态": "", "原因": "", "提交诊断": ""}
                previous = ledger.status(sku)
                if previous:
                    row.update(状态="跳过", 原因=f"已有草稿提交记录：{previous}，请先核对")
                    failures += previous == "uncertain"
                    rows.append(row)
                    continue
                if sku not in prices:
                    row.update(状态="跳过", 原因="缺少可靠货源价，或多个来源站点价格不明确")
                    failures += 1
                    rows.append(row)
                    continue
                edit_page = None
                attempted = False
                status = "uncertain"
                try:
                    edit_page = await draft.edit_draft(sku)
                    if edit_page is None:
                        raise RuntimeError("找不到草稿编辑入口")
                    await draft.wait_price_fields(edit_page)
                    await draft.set_prices(edit_page, prices[sku], multiple)
                    await draft.verify_shop(args.target_shop)
                    # 写入提交意图后才允许点击；异常退出也会拦截下次重发。
                    ledger.save(sku, "uncertain", "已进入草稿提交阶段")
                    attempted = True
                    status = await draft.submit_and_wait(edit_page)
                    ledger.save(sku, "submitted" if status == "success" else "uncertain")
                    row["状态"] = "已提交（待核验在售）" if status == "success" else "待核实"
                except Exception as exc:  # noqa: BLE001 — 提交阶段任意异常均需保留检查点
                    row.update(状态="待核实" if attempted else "失败", 原因=str(exc))
                finally:
                    if edit_page is not None:
                        row["提交诊断"] = await _save_submission_evidence(
                            edit_page, settings, f"{stem}-{sku}", sku, Platform.PINDUODUO,
                            args.target_shop, ListingStatus.SUBMITTED if status == "success" else ListingStatus.UNCERTAIN,
                            row["原因"] or row["状态"], "",
                        )
                        await draft.close_page(edit_page)
                    rows.append(row)
                print(f"[{index}/{len(skus)}] {sku}：{row['状态']} {row['原因']}")
                if status != "success":
                    failures += 1
                if attempted and status != "success":
                    print("提交结果待核实，已落库并暂停；请核对后台后处理 resolve-draft。")
                    break
                if args.delay_seconds > 0 and index < len(skus):
                    await page.wait_for_timeout(args.delay_seconds * random.uniform(1, 2) * 1000)
    finally:
        if rows:
            path = export_workbook_xlsx([("草稿发布结果", rows)], settings.artifacts_dir, stem)
            print(f"草稿结果：{path}")
    return 1 if failures else 0


def resolve_draft(args: argparse.Namespace, settings: Settings) -> int:
    initialize(settings.database_path)
    ledger = DraftLedger(settings.database_path, args.target_shop, args.source_market)
    changed = ledger.resolve(args.item_id, args.outcome)
    print("已更新本地草稿记录。" if changed else "未找到指定店铺、站点、商品的草稿记录。")
    return 0 if changed else 1
