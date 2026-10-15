from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import re
from contextlib import suppress
from datetime import UTC, datetime

from playwright.async_api import Page

from pdd_listing_automation import __version__
from pdd_listing_automation.browser.session import BrowserSession
from pdd_listing_automation.cli import build_parser
from pdd_listing_automation.config import Settings
from pdd_listing_automation.models import (
    ListingStatus,
    Platform,
    ShopNewArrival,
    SourceProduct,
    has_listing,
    market_from_url,
)
from pdd_listing_automation.pages.favorite_shops import FavoriteShopsPage
from pdd_listing_automation.pages.product_detail import ProductDetailPage
from pdd_listing_automation.pages.shop_catalog import ShopCatalogPage
from pdd_listing_automation.pages.upload_platform import (
    AlreadyListedError,
    UploadPlatformPage,
    UploadRejectedError,
)
from pdd_listing_automation.pages.upload_records import UploadRecordsPage
from pdd_listing_automation.reports.batch_summary import (
    LISTING_LABELS,
    build_batch_summary,
    format_batch_summary,
)
from pdd_listing_automation.reports.exporter import export_models, export_workbook_xlsx
from pdd_listing_automation.services.discovery import DiscoveryService
from pdd_listing_automation.services.drafts import publish_drafts, resolve_draft
from pdd_listing_automation.services.evidence import (
    _record_failure_evidence,
    _save_submission_evidence,
)
from pdd_listing_automation.storage.batches import BatchJournal
from pdd_listing_automation.storage.database import connect, initialize
from pdd_listing_automation.storage.repositories import SourceProductRepository


async def _ensure_authenticated(page_obj, settings: Settings) -> None:
    """检测登录态。无头模式下登录失效无法人工扫码，直接报错并提示去登录；有头模式提示人工登录。"""
    if await page_obj.is_login_page():
        if settings.headless:
            raise RuntimeError(
                "登录已失效：当前是后台无头模式，无法弹出浏览器登录。"
                "请先双击「登录账号.bat」完成登录，再重新运行。"
            )
        print("请在打开的浏览器中完成 17zwd 登录。")
    await page_obj.wait_until_authenticated()


async def login(settings: Settings) -> int:
    """单独登录：强制有头模式，打开浏览器让用户完成 17zwd 登录，登录态写入本地 profile。"""
    settings.headless = False  # 登录必须可见（扫码 / 验证码）
    async with BrowserSession(settings).open() as (_, page):
        await page.goto(
            "https://i.17zwd.com/user/favouriteShops",
            wait_until="domcontentloaded",
        )
        if "/login" not in (page.url or ""):
            print("已登录，无需重新登录。")
            return 0
        print("请在打开的浏览器中完成 17zwd 登录（扫码 / 账号密码）。")
        for _ in range(settings.login_wait_seconds):
            await page.wait_for_timeout(1_000)
            if "/login" not in (page.url or ""):
                break
        else:
            print(f"登录等待超时（{settings.login_wait_seconds} 秒），未检测到登录成功，请重试。")
            return 1
        print("登录成功，登录态已保存到本地浏览器配置。")
        return 0


def export_report(settings: Settings, platform: Platform | None = None) -> int:
    """把数据库里的铺货记录导出成 Excel（明细 + 按档口汇总），用于对账「我上的分别是哪家的」。"""
    initialize(settings.database_path)
    with connect(settings.database_path) as connection:
        query = (
            "SELECT source_goods_id, title, source_sku, price_text, source_shop, "
            "source_category, source_platform, uploaded_at_text, target_shop, listing_status, "
            "source_market, source_url "
            "FROM source_products"
        )
        params: tuple = ()
        if platform is not None:
            query += " WHERE source_platform = ?"
            params = (platform.value,)
        query += " ORDER BY source_platform, source_shop, uploaded_at_text DESC"
        rows = connection.execute(query, params).fetchall()

    if not rows:
        print("暂无铺货记录。请先运行菜单「刷新铺货记录」（或 scan-records）再导出。")
        return 0

    detail = [
        {
            "商品ID": r["source_goods_id"],
            "来源站点": r["source_market"] or "未知（历史记录）",
            "商品链接": r["source_url"],
            "标题": r["title"],
            "档口": r["source_shop"],
            "货号": r["source_sku"],
            "货源价": r["price_text"],
            "类目": r["source_category"],
            "平台": Platform(r["source_platform"]).display_name,
            "目标店铺": r["target_shop"] or "未知（历史记录）",
            "状态": LISTING_LABELS[ListingStatus(r["listing_status"])],
            "记录时间": r["uploaded_at_text"],
        }
        for r in rows
    ]

    by_shop: dict[tuple[str, str, str], int] = {}
    for r in rows:
        key = (r["source_shop"], r["source_platform"], r["listing_status"])
        by_shop[key] = by_shop.get(key, 0) + 1
    summary = [
        {"档口": shop, "平台": Platform(plat).display_name,
         "状态": LISTING_LABELS[ListingStatus(status)], "记录数": cnt}
        for (shop, plat, status), cnt in sorted(by_shop.items(), key=lambda kv: (-kv[1], kv[0][0]))
    ]

    timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    stem = f"上架清单-{platform.value if platform else 'all'}-{timestamp}"
    path = export_workbook_xlsx(
        [("上架清单", detail), ("按档口汇总", summary)],
        settings.artifacts_dir,
        stem,
    )

    print(f"共 {len(detail)} 条铺货记录，来自 {len({r['source_shop'] for r in rows})} 个档口。")
    print(f"Excel：{path}")
    return 0


def resolve_submission(args: argparse.Namespace, settings: Settings) -> int:
    if not args.target_shop.strip():
        raise ValueError("必须指定完整目标店铺名，不能处理未知店铺的历史记录")
    initialize(settings.database_path)
    item_id, item_url = parse_item_ref(args.item_id)
    with connect(settings.database_path) as connection:
        updated = SourceProductRepository(connection).resolve_submission(
            Platform.parse(args.platform), args.target_shop.strip(), item_id,
            recorded=args.outcome == "recorded",
            include_recorded=getattr(args, "include_recorded", False),
            source_market=getattr(args, "source_market", None) or market_from_url(item_url) or None,
        )
    if not updated:
        print("未找到该平台、店铺、商品对应的待核实/已提交记录。")
        return 1
    print("已标记为有铺货记录。" if args.outcome == "recorded" else "已解除该商品的重发拦截。")
    return 0


async def scan_records(args: argparse.Namespace, settings: Settings) -> int:
    platform = Platform.parse(args.platform)
    if args.login_wait_seconds is not None:
        settings.login_wait_seconds = args.login_wait_seconds

    async with BrowserSession(settings).open() as (_, page):
        upload_records = UploadRecordsPage(page, settings.login_wait_seconds)
        await upload_records.open()
        await _ensure_authenticated(upload_records, settings)
        await upload_records.select_platform(platform)

        products = []
        for page_index in range(max(1, args.pages)):
            products.extend(await upload_records.collect_current_page(platform))
            if page_index + 1 >= max(1, args.pages):
                break
            if not await upload_records.has_next_page():
                break
            try:
                await upload_records.go_to_next_page()
            except RuntimeError:
                break

    settings.ensure_directories()
    initialize(settings.database_path)
    with connect(settings.database_path) as connection:
        result = DiscoveryService(connection).store(products)

    timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    stem = f"upload-records-{platform.value}-{timestamp}"
    json_path, csv_path = export_models(result.products, settings.artifacts_dir, stem)

    print(f"平台：{platform.display_name}")
    print(f"采集：{result.collected}")
    print(f"去重后：{result.unique}")
    print(f"数据库商品总数：{result.stored_total}")
    print(f"JSON：{json_path}")
    print(f"CSV：{csv_path}")
    return 0


def parse_item_ref(value: str) -> tuple[str, str]:
    """把 --item-id 解析成 (数字ID, 完整URL)：支持纯 ID 或完整商品链接。"""
    value = value.strip()
    if value.startswith(("http://", "https://")):
        match = re.search(r"/item/(\d+)", value)
        if not match:
            raise RuntimeError(f"无法从链接提取商品 ID：{value}")
        return match.group(1), value
    return value, value


MAX_CONSECUTIVE_FAILURES = 5


async def _close_leaked_upload_pages(page: Page) -> None:
    """关闭除主页面外的 t-onekey 上传页，避免下一条 start_upload 命中旧标签。"""
    for target in list(page.context.pages):
        if target is page:
            continue
        if "t-onekey.17zwd.com" in (target.url or ""):
            with suppress(Exception):
                await target.close()


async def run_single_upload(
    page: Page,
    *,
    item_id: str,
    item_url: str,
    platform: Platform,
    target_shop: str,
    confirm_publish: str,
    screenshot_stem: str,
    settings: Settings,
    source_arrival: ShopNewArrival | None = None,
    database_initialized: bool = False,
) -> tuple[dict[str, str], Page | None, bool]:
    """单个商品的完整上传流程，停在“选择上货店铺”；确认短语为 PUBLISH 时才点开始上传。"""
    if confirm_publish:
        if confirm_publish != "PUBLISH":
            raise RuntimeError("发布确认短语不正确，必须为 PUBLISH")
        if not target_shop:
            raise RuntimeError("执行发布时必须指定目标店铺")
        if not database_initialized:
            initialize(settings.database_path)
        with connect(settings.database_path) as connection:
            record = SourceProductRepository(connection).find_listing_record(
                platform, target_shop, item_id, market_from_url(item_url) or "gz.17zwd.com",
            )
            if record is not None:
                scope = record["target_shop"] or "未知店铺（历史记录）"
                label = LISTING_LABELS[ListingStatus(record["listing_status"])]
                raise AlreadyListedError(
                    f"本地记录拦截：{scope}，{label}；请先核对上货日志",
                    ListingStatus(record["listing_status"]),
                )
    product_page = ProductDetailPage(page)
    await product_page.open(item_url)
    summary = await product_page.collect_summary(item_id)
    platform_page_target = await product_page.start_upload(platform)

    platform_page = UploadPlatformPage(platform_page_target)
    await platform_page.confirm_superboss()
    await platform_page.wait_for_store_selection()

    if confirm_publish:
        await platform_page.accept_service_agreement()
        await platform_page.accept_copy_commitment()
    if target_shop:
        await platform_page.select_shop(target_shop)

    # 注意：不调用 bring_to_front，避免浏览器窗口抢焦点弹到前台；Playwright 截图无需前台
    screenshot_path = settings.artifacts_dir / f"{screenshot_stem}.png"
    await platform_page_target.screenshot(path=str(screenshot_path), full_page=False)

    published = False
    listing_status = ListingStatus.UNCERTAIN
    submission_detail = ""
    evidence_path = ""
    if confirm_publish:
        await platform_page.accept_commitment()
        # 先落盘再点最终提交：即使进程在点击后退出，下次也不会盲目重发。
        arrival = source_arrival or ShopNewArrival(item_id=item_id, item_url=item_url)
        _mark_listed(settings, arrival, platform, target_shop, summary, listing_status)
        try:
            await platform_page.start_upload()
            listing_status = await platform_page.confirm_listing()
        except AlreadyListedError:
            _mark_listed(settings, arrival, platform, target_shop, summary, ListingStatus.RECORDED)
            raise
        except UploadRejectedError:
            # 明确拒绝提交的商品按失败统计；解除本次预写的待核实拦截。
            with connect(settings.database_path) as connection:
                SourceProductRepository(connection).resolve_submission(
                    platform, target_shop, item_id, recorded=False,
                    source_market=arrival.source_market or "gz.17zwd.com",
                )
            raise
        except Exception as exc:  # noqa: BLE001 — 进入提交阶段后出错必须保留待核实状态
            submission_detail = f"提交阶段异常：{type(exc).__name__}: {exc}"
            await _record_failure_evidence(page, settings, item_id, exc)
        else:
            submission_detail = str(platform_page.last_submission_detail)
        _mark_listed(settings, arrival, platform, target_shop, summary, listing_status)
        evidence_path = await _save_submission_evidence(
            platform_page_target, settings, screenshot_stem, item_id, platform, target_shop,
            listing_status, submission_detail, str(platform_page.last_visible_text),
        )
        published = True

    result = {**summary, "screenshot_path": str(screenshot_path),
              "listing_status": listing_status.value if published else "prepared",
              "submission_detail": submission_detail, "evidence_path": evidence_path}
    return result, platform_page_target, published


async def prepare_upload(args: argparse.Namespace, settings: Settings) -> int:
    platform = Platform.parse(args.platform)
    item_id, item_url = parse_item_ref(args.item_id)
    async with BrowserSession(settings).open() as (_, page):
        result, _, published = await run_single_upload(
            page,
            item_id=item_id,
            item_url=item_url,
            platform=platform,
            target_shop=args.shop_name,
            confirm_publish=args.confirm_publish,
            screenshot_stem=f"prepare-upload-{platform.value}-{item_id}",
            settings=settings,
        )

        print(f"商品：{result['title']}")
        print(f"商品 ID：{result['item_id']}")
        print(f"货号：{result['sku']}")
        print(f"货源价格：{result['price']}")
        print(f"目标平台：{platform.display_name}")
        if args.shop_name:
            print(f"目标店铺：{args.shop_name}")
        print(
            f"状态：{LISTING_LABELS[ListingStatus(result['listing_status'])]}，请核对上货日志。"
            if published
            else "状态：已到“选择上货店铺”，尚未开始上传。"
        )
        print(f"截图：{result['screenshot_path']}")
        if result.get("submission_detail"):
            print(f"提交反馈：{result['submission_detail']}")
        if result.get("evidence_path"):
            print(f"提交诊断：{result['evidence_path']}")

        if args.hold_open_seconds > 0:
            print(f"浏览器将保持打开 {args.hold_open_seconds} 秒，可人工检查。")
            await page.wait_for_timeout(args.hold_open_seconds * 1000)
    return 1 if published and result["listing_status"] == ListingStatus.UNCERTAIN else 0


def print_arrivals(arrivals: list[ShopNewArrival], listed_ids: set, label: str = "商品") -> None:
    unlisted = [a for a in arrivals if not has_listing(a, listed_ids)]
    listed = [a for a in arrivals if has_listing(a, listed_ids)]
    print(f"{label}共 {len(arrivals)} 款：可处理 {len(unlisted)} 款 / 已有记录 {len(listed)} 款（按上新日期倒序）")
    print(f"—— 未上架 {len(unlisted)} 款 ——")
    for index, arrival in enumerate(unlisted, 1):
        price = f"¥{arrival.price_text}" if arrival.price_text else "价格未知"
        date = f"{arrival.on_shelf_date_text}上新" if arrival.on_shelf_date_text else "日期未知"
        name = arrival.title or arrival.shop_name or "无标题"
        print(f"{index:>4}. {name} {price} {date}  id={arrival.item_id}")
    if listed:
        print(f"—— 已有铺货或待核实记录 {len(listed)} 款（跳过，避免重复提交）——")
        for arrival in listed:
            name = arrival.title or arrival.shop_name or "无标题"
            print(f"  ⚠ {name} id={arrival.item_id}")


def select_arrivals(
    arrivals: list[ShopNewArrival],
    args: argparse.Namespace,
    listed_ids: set,
) -> tuple[list[ShopNewArrival], list[ShopNewArrival]]:
    """按 --item-ids / --limit 选款，返回 (要上架的, 已上过被跳过的)。"""
    if args.item_ids:
        wanted = list(dict.fromkeys(
            part.strip() for part in args.item_ids.replace("，", ",").split(",") if part.strip()
        ))
        by_id: dict[str, list[ShopNewArrival]] = {}
        by_key = {arrival.product_key: arrival for arrival in arrivals}
        for arrival in arrivals:
            by_id.setdefault(arrival.item_id, []).append(arrival)
        selected: list[ShopNewArrival] = []
        skipped: list[ShopNewArrival] = []
        chosen_keys: set[tuple[str, str]] = set()
        for ref in wanted:
            item_id, url = parse_item_ref(ref)
            market = market_from_url(url)
            if market:
                arrival = by_key.get((market, item_id))
            else:
                matches = by_id.get(item_id, [])
                if len(matches) > 1:
                    raise RuntimeError(f"商品 ID {item_id} 存在多个来源站点，请传入完整商品链接")
                arrival = matches[0] if matches else None
            if arrival is None:
                raise RuntimeError(
                    f"商品 ID {item_id} 不在本次扫描结果中，请核对 ID 或增加 --pages"
                )
            if arrival.product_key in chosen_keys:
                continue
            chosen_keys.add(arrival.product_key)
            if has_listing(arrival, listed_ids):
                skipped.append(arrival)
            else:
                selected.append(arrival)
        return selected, skipped
    if args.limit and args.limit > 0:
        selected = []
        skipped = []
        for arrival in arrivals:
            if len(selected) >= args.limit:
                break
            if has_listing(arrival, listed_ids):
                skipped.append(arrival)
            else:
                selected.append(arrival)
        return selected, skipped
    return [], []


def parse_selection(text: str, total: int) -> list[int]:
    """把「1,3,5-8」解析成 0 起下标（去重、忽略越界）。"""
    result: list[int] = []
    for part in text.replace("，", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start_text, _, end_text = part.partition("-")
            try:
                start, end = int(start_text), int(end_text)
            except ValueError:
                continue
            result.extend(i - 1 for i in range(start, end + 1) if 1 <= i <= total)
        else:
            try:
                i = int(part)
            except ValueError:
                continue
            if 1 <= i <= total:
                result.append(i - 1)
    seen: set[int] = set()
    ordered: list[int] = []
    for i in result:
        if i not in seen:
            seen.add(i)
            ordered.append(i)
    return ordered


def _mark_listed(
    settings: Settings, arrival: ShopNewArrival, platform: Platform,
    target_shop: str, summary: dict[str, str], listing_status: ListingStatus,
) -> None:
    """记录提交状态和完整货源信息，阻止未经核实的重复提交。"""
    with connect(settings.database_path) as connection:
        SourceProductRepository(connection).upsert_many(
            [
                SourceProduct(
                    source_goods_id=arrival.item_id,
                    source_row_key=arrival.item_id,
                    title=summary.get("title") or arrival.title,
                    source_sku=summary.get("sku", ""),
                    price_text=summary.get("price") or arrival.price_text,
                    source_shop=arrival.shop_name,
                    source_category=arrival.shop_main_category,
                    image_url=arrival.image_url,
                    source_platform=platform,
                    source_market=arrival.source_market or "gz.17zwd.com",
                    source_url=arrival.item_url if market_from_url(arrival.item_url) else f"https://gz.17zwd.com/item/{arrival.item_id}",
                    target_shop=target_shop,
                    listing_status=listing_status,
                    uploaded_at_text=datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S"),
                )
            ]
        )


async def _process_arrivals(
    page: Page,
    arrivals: list[ShopNewArrival],
    args: argparse.Namespace,
    settings: Settings,
    platform: Platform,
    stem_prefix: str,
    label: str,
) -> int:
    # 去重：同一商品可能出现在多页，只保留一份
    seen: set[tuple[str, str]] = set()
    deduped: list[ShopNewArrival] = []
    for arrival in arrivals:
        if arrival.product_key not in seen:
            seen.add(arrival.product_key)
            deduped.append(arrival)
    arrivals = deduped

    arrivals = sorted(
        arrivals,
        key=lambda arrival: (arrival.on_shelf_sort_key, arrival.item_id),
        reverse=True,
    )

    settings.ensure_directories()
    initialize(settings.database_path)
    with connect(settings.database_path) as connection:
        listed_ids = SourceProductRepository(connection).listed_product_keys(platform, args.target_shop)

    print_arrivals(arrivals, listed_ids, label)

    timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    stem = f"{stem_prefix}-{platform.value}-{timestamp}"
    json_path, csv_path = export_models(arrivals, settings.artifacts_dir, stem)
    print(f"JSON：{json_path}")
    print(f"CSV：{csv_path}")

    unlisted = [a for a in arrivals if not has_listing(a, listed_ids)]

    resume_id = getattr(args, "resume_batch", "")
    skipped: list[ShopNewArrival] = []
    saved_items: list[dict] = []
    if resume_id:
        journal = BatchJournal(settings.database_path, resume_id)
        _, saved_items = journal.load()
        selected = [item["arrival"] for item in saved_items if item["state"] != "done"]
    elif getattr(args, "pick", False) and not args.item_ids and not args.limit:
        text = input("上架哪几款？输入 n 上最新 n 款；输入 1,3,5 或 1-5 按「未上架」序号选；回车只看货：").strip()
        if not text:
            print("未选择，仅完成扫描（未上架）。")
            return 0
        if text.isdigit():
            selected = unlisted[: int(text)]
        else:
            selected = [unlisted[i] for i in parse_selection(text, len(unlisted))]
    else:
        selected, skipped = select_arrivals(arrivals, args, listed_ids)

    if not selected and not skipped and not resume_id:
        print("没有可上架的商品（可能都已上过，或未选择）。")
        return 0

    # 发布前一次性校验，避免批量循环里每款都报同一个错、白跑几百次
    if args.confirm_publish:
        if args.confirm_publish != "PUBLISH":
            raise RuntimeError("发布确认短语不正确，必须为 PUBLISH")
        if not args.target_shop:
            raise RuntimeError(
                "执行发布必须指定目标店铺名（--target-shop）。"
                "菜单里请先填写店铺名；命令行请加 --target-shop \"你的店铺名\"。"
            )

    if not resume_id:
        journal = BatchJournal.create(settings.database_path, {
            "platform": platform.value, "target_shop": args.target_shop,
            "confirm_publish": args.confirm_publish, "delay_seconds": getattr(args, "delay_seconds", 15.0),
            "hold_open_seconds": args.hold_open_seconds, "stem_prefix": stem_prefix, "label": label,
        }, skipped + selected)
        for position, arrival in enumerate(skipped):
            journal.record(position, {
                "商品ID": arrival.item_id, "来源站点": arrival.source_market,
                "商品链接": arrival.item_url, "标题": arrival.title, "档口": arrival.shop_name,
                "目标店铺": args.target_shop, "状态": "跳过（已有记录）",
                "失败原因": "本地已有铺货或待核实记录；未重新提交",
                "处理时间": datetime.now(UTC).isoformat(),
            })
        _, saved_items = journal.load()
    total = len(saved_items)
    positions = [item["position"] for item in saved_items if item["state"] != "done"]
    stem = f"{stem_prefix}-{platform.value}-{journal.batch_id}"
    print(f"批次 ID：{journal.batch_id}；本次待处理 {len(selected)} 款。")
    print(f"程序版本：{__version__}")
    print(f"中断后可运行：pdd-listing resume-batch --batch-id {journal.batch_id}")
    consecutive_failures = 0
    stop_reason = ""
    delay_seconds = float(getattr(args, "delay_seconds", 15.0))
    report_rows: list[dict[str, str]] = [item["result"] for item in saved_items if item["result"]]
    for index, arrival in enumerate(selected, 1):
        name = arrival.title or arrival.shop_name or arrival.item_id
        print(f"[{index}/{len(selected)}] id={arrival.item_id}（{name}）")
        result = None
        published = False
        skip = False
        error: Exception | None = None

        # 上架不做自动重试：一旦点了「确认上货」可能已部分提交，重试会导致重复上架
        platform_page_target: Page | None = None
        journal.start(positions[index - 1])
        try:
            result, platform_page_target, published = await run_single_upload(
                page,
                item_id=arrival.item_id,
                item_url=arrival.item_url,
                platform=platform,
                target_shop=args.target_shop,
                confirm_publish=args.confirm_publish,
                screenshot_stem=f"{stem}-{arrival.source_market}-{arrival.item_id}",
                settings=settings,
                source_arrival=arrival,
                database_initialized=True,
            )
            if not published and args.hold_open_seconds > 0:
                print(f"  准备完成，保留选店页面 {args.hold_open_seconds} 秒供人工检查。")
                await page.wait_for_timeout(args.hold_open_seconds * 1000)
            error = None
        except AlreadyListedError as exc:
            error = exc
            skip = True
            await _close_leaked_upload_pages(page)
        except Exception as exc:  # noqa: BLE001 — 批量上架需捕获任意错误
            error = exc
            # 无论什么错误都写日志（含堆栈），便于定位；浏览器没关闭时才清理泄漏的上传页
            await _record_failure_evidence(page, settings, arrival.item_id, exc)
            if not ("closed" in str(exc).lower() or "TargetClosed" in type(exc).__name__):
                await _close_leaked_upload_pages(page)
        finally:
            # 关闭本次打开的上传页，避免下一次 start_upload 命中旧的 t-onekey 标签
            if platform_page_target is not None and platform_page_target is not page:
                with suppress(Exception):
                    await platform_page_target.close()

        if error is None:
            consecutive_failures = 0
            if published:
                status = ListingStatus(result["listing_status"])
                status_label = LISTING_LABELS[status]
            else:
                status_label = "准备完成（未发布）"
            print(f"  商品：{result['title']}")
            print(f"  货号：{result['sku']}  货源价：¥{result['price']}")
            print(f"  状态：{status_label}")
            if result.get("submission_detail"):
                print(f"  提交反馈：{result['submission_detail']}")
            if result.get("evidence_path"):
                print(f"  提交诊断：{result['evidence_path']}")
            print(f"  截图：{result['screenshot_path']}")
            report_rows.append({
                "商品ID": arrival.item_id,
                "标题": result["title"],
                "档口": arrival.shop_name,
                "档口主营": arrival.shop_main_category,
                "货号": result["sku"],
                "货源价": result["price"],
                "目标店铺": args.target_shop,
                "状态": status_label,
                "失败原因": result.get("submission_detail", "") if published and status == ListingStatus.UNCERTAIN else "",
                "处理时间": datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S"),
            })
            if published and status == ListingStatus.UNCERTAIN:
                print("  已保留待核实记录，不重发本款，继续处理下一款。")
        elif skip:
            consecutive_failures = 0
            print(f"  ⏭ 跳过：{error}")
            recovered_status = getattr(error, "listing_status", None) if resume_id else None
            report_rows.append({
                "商品ID": arrival.item_id,
                "标题": arrival.title,
                "档口": arrival.shop_name,
                "档口主营": arrival.shop_main_category,
                "货号": "",
                "货源价": arrival.price_text,
                "目标店铺": args.target_shop,
                "状态": LISTING_LABELS[recovered_status] if recovered_status in (
                    ListingStatus.UNCERTAIN, ListingStatus.SUBMITTED,
                ) else "跳过（已有记录）",
                "失败原因": str(error),
                "处理时间": datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S"),
            })
        else:
            consecutive_failures += 1
            print(f"  ❌ 失败：{type(error).__name__}: {error}")
            report_rows.append({
                "商品ID": arrival.item_id,
                "标题": arrival.title,
                "档口": arrival.shop_name,
                "档口主营": arrival.shop_main_category,
                "货号": "",
                "货源价": arrival.price_text,
                "目标店铺": args.target_shop,
                "状态": "失败",
                "失败原因": str(error),
                "处理时间": datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S"),
            })
            if "closed" in str(error).lower() or "TargetClosed" in type(error).__name__:
                stop_reason = "浏览器已关闭"
                print("  （浏览器已关闭，停止后续上架）")
            elif consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                stop_reason = f"连续失败 {consecutive_failures} 次"
                print(
                    f"  （连续失败 {consecutive_failures} 次，自动暂停上架，"
                    "先排查原因再继续，详见 artifacts/failures.log）"
                )

        report_rows[-1].update({"来源站点": arrival.source_market, "商品链接": arrival.item_url})
        journal.record(positions[index - 1], report_rows[-1])
        if stop_reason:
            break

        # 每款之间随机停顿，降低风控/浏览器崩溃概率
        if delay_seconds > 0 and index < len(selected):
            try:
                await page.wait_for_timeout(delay_seconds * random.uniform(1.0, 2.0) * 1000)
            except Exception as exc:
                if "closed" in str(exc).lower() or "TargetClosed" in type(exc).__name__:
                    stop_reason = "浏览器在款间等待时关闭"
                    print("  （浏览器已关闭，停止后续上架）")
                    break
                raise

    journal.finish()
    _, saved_items = journal.load()
    report_rows = [item["result"] for item in saved_items if item["result"]]
    counts = build_batch_summary(report_rows, total)
    summary = format_batch_summary(report_rows, total, platform, args.target_shop, stop_reason)
    summary = f"批次 ID：{journal.batch_id}\n" + summary
    summary_rows = [
        {"项目": "平台", "结果": platform.display_name},
        {"项目": "目标店铺", "结果": args.target_shop or "未指定"},
        *({"项目": key, "结果": value} for key, value in counts.items()),
        {"项目": "成功口径", "结果": "提交成功，尚未核验平台最终上架结果"},
        {"项目": "提前结束原因", "结果": stop_reason or "无"},
    ]
    failed_rows = [row for row in report_rows if row["状态"] == "失败"]
    uncertain_rows = [row for row in report_rows if row["状态"] == LISTING_LABELS[ListingStatus.UNCERTAIN]]
    unprocessed_rows = [
        {"商品ID": arrival.item_id, "标题": arrival.title, "档口": arrival.shop_name,
         "状态": "未处理", "原因": stop_reason or "批次提前结束"}
        for arrival in [item["arrival"] for item in saved_items if item["state"] != "done"]
    ]
    sheets = [("本次汇总", summary_rows), ("上架结果", report_rows)]
    if failed_rows:
        sheets.append(("失败清单", failed_rows))
    if uncertain_rows:
        sheets.append(("待核实清单", uncertain_rows))
    if unprocessed_rows:
        sheets.append(("未处理清单", unprocessed_rows))
    report_error = False
    try:
        report_path = export_workbook_xlsx(sheets, settings.artifacts_dir, f"{stem}-上架结果")
        summary += f"\n结果 Excel：{report_path}"
    except Exception as exc:  # noqa: BLE001 — 报表失败也必须展示本次执行总结
        report_error = True
        summary += f"\nExcel 保存失败：{exc}"
    print(summary)
    try:
        summary_path = settings.artifacts_dir / f"{stem}-批次总结.txt"
        summary_path.write_text(summary, encoding="utf-8")
        print(f"总结文件：{summary_path}")
    except OSError as exc:
        report_error = True
        print(f"总结文件保存失败：{exc}")

    if selected and args.confirm_publish and args.hold_open_seconds > 0:
        print(f"浏览器将保持打开 {args.hold_open_seconds} 秒，可人工检查。")
        await page.wait_for_timeout(args.hold_open_seconds * 1000)
    return 1 if counts["失败"] or counts["待核实"] or counts["未处理"] or report_error else 0


async def list_from_shops(args: argparse.Namespace, settings: Settings) -> int:
    platform = Platform.parse(args.platform)
    async with BrowserSession(settings).open() as (_, page):
        shops_page = FavoriteShopsPage(page, settings.login_wait_seconds)
        await shops_page.open()
        await _ensure_authenticated(shops_page, settings)

        arrivals = await shops_page.collect_new_arrivals(args.pages)
        return await _process_arrivals(
            page, arrivals, args, settings, platform, "favorite-shops", "关注档口上新",
        )


async def resume_batch(args: argparse.Namespace, settings: Settings) -> int:
    initialize(settings.database_path)
    options, items = BatchJournal(settings.database_path, args.batch_id).load()
    print(f"恢复原批次：{options['platform']} / {options['target_shop']}；"
          + ("继续已确认的发布任务。" if options["confirm_publish"] else "仅准备，不自动提交。"))
    saved_args = argparse.Namespace(**options, resume_batch=args.batch_id, item_ids="", limit=0)
    arrivals = [item["arrival"] for item in items]
    platform = Platform.parse(options["platform"])
    if all(item["state"] == "done" for item in items):
        return await _process_arrivals(
            None, arrivals, saved_args, settings, platform, options["stem_prefix"], options["label"],
        )
    async with BrowserSession(settings).open() as (_, page):
        return await _process_arrivals(
            page, arrivals, saved_args, settings, platform, options["stem_prefix"], options["label"],
        )


def list_batches(settings: Settings) -> int:
    initialize(settings.database_path)
    for batch in BatchJournal.recent(settings.database_path):
        options = json.loads(batch["options_json"])
        print(f"{batch['batch_id']}  {options['platform']} / {options['target_shop']}  "
              f"已处理 {batch['processed']}/{batch['total']}  {batch['state']}")
    return 0


async def list_from_shop(args: argparse.Namespace, settings: Settings) -> int:
    platform = Platform.parse(args.platform)
    async with BrowserSession(settings).open() as (_, page):
        catalog_page = ShopCatalogPage(page)
        await catalog_page.open(args.shop_url, getattr(args, "start_page", 1))
        arrivals = await catalog_page.collect(args.pages)
        return await _process_arrivals(
            page, arrivals, args, settings, platform, "shop-catalog", "档口商品",
        )


async def reconcile_shop(args: argparse.Namespace, settings: Settings) -> int:
    """对比有限扫描范围的铺货记录；不把存在记录等同于已在售。"""
    platform = Platform.parse(args.platform)
    async with BrowserSession(settings).open() as (_, page):
        catalog_page = ShopCatalogPage(page)
        await catalog_page.open(args.shop_url, getattr(args, "start_page", 1))
        arrivals = await catalog_page.collect(args.pages)

        upload_records = UploadRecordsPage(page, settings.login_wait_seconds)
        await upload_records.open()
        await _ensure_authenticated(upload_records, settings)
        await upload_records.select_platform(platform)
        listed_products: list[SourceProduct] = []
        for page_index in range(max(1, args.record_pages)):
            listed_products.extend(await upload_records.collect_current_page(platform))
            if page_index + 1 >= max(1, args.record_pages):
                break
            if not await upload_records.has_next_page():
                break
            try:
                await upload_records.go_to_next_page()
            except RuntimeError:
                break

    initialize(settings.database_path)
    with connect(settings.database_path) as connection:
        listed_ids = SourceProductRepository(connection).listed_product_keys(
            platform, getattr(args, "target_shop", ""),
        )
    # 线上记录没有可靠的目标店铺信息，不能用它证明指定店铺已铺货。
    unknown_ids = {(p.source_market, p.source_goods_id) for p in listed_products}
    if not getattr(args, "target_shop", ""):
        listed_ids |= unknown_ids

    # 去重后对比
    seen: set[tuple[str, str]] = set()
    deduped: list[ShopNewArrival] = []
    for arrival in arrivals:
        if arrival.product_key not in seen:
            seen.add(arrival.product_key)
            deduped.append(arrival)

    listed = [a for a in deduped if has_listing(a, listed_ids)]
    missing = [a for a in deduped if not has_listing(a, listed_ids)]

    print(f"档口商品共 {len(deduped)} 款：有铺货或提交记录 {len(listed)} 款，本次未查到 {len(missing)} 款")
    print(f"仅核对本地记录及最多 {args.record_pages} 页线上记录，未核验平台在售状态。")
    if getattr(args, "target_shop", ""):
        print("线上记录的目标店铺未知，不作为指定店铺的铺货证明；本地未知店铺记录仍保守提示。")
    if listed:
        print(f"—— 有铺货或提交记录 {len(listed)} 款 ——")
        for a in listed:
            print(f"  ✅ {a.title} id={a.item_id}")
    print(f"—— 本次未查到记录 {len(missing)} 款 ——")
    for a in missing:
        price = f" ¥{a.price_text}" if a.price_text else ""
        date = f" {a.on_shelf_date_text}上新" if a.on_shelf_date_text else ""
        print(f"  ❌ {a.title}{price}{date} id={a.item_id}")

    settings.ensure_directories()
    timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    _, listed_csv = export_models(
        listed, settings.artifacts_dir, f"reconcile-listed-{platform.value}-{timestamp}",
    )
    _, missing_csv = export_models(
        missing, settings.artifacts_dir, f"reconcile-missing-{platform.value}-{timestamp}",
    )
    print(f"有记录 CSV：{listed_csv}")
    print(f"本次未查到 CSV：{missing_csv}")
    return 0


def _run(coro):
    """在菜单里运行一个任务，出错时打印原因并继续留在菜单，不让整程序崩溃。"""
    try:
        return asyncio.run(coro)
    except Exception as exc:  # noqa: BLE001 — 菜单需展示任务错误并允许继续操作
        print(f"\n[失败] {exc}\n")
        return None


def run_menu(settings: Settings) -> int:
    """交互式菜单，供「一键启动」双击调用。"""
    shops = {
        "pinduoduo": os.getenv("PDD_AUTOMATION_TARGET_SHOP", "").strip(),
        "douyin": os.getenv("PDD_AUTOMATION_DOUYIN_SHOP", "").strip(),
    }
    platform = "pinduoduo"
    try:
        delay = float(os.getenv("PDD_AUTOMATION_DELAY_SECONDS", "15.0"))
    except ValueError:
        delay = 15.0
    while True:
        display = Platform.parse(platform).display_name
        if not shops[platform]:
            shops[platform] = input(
                f"请输入 {display} 目标店铺名（超级店长弹窗里的精确名字，可回车跳过）："
            ).strip()
        shop = shops[platform]
        mode = "后台（不弹浏览器）" if settings.headless else "前台（可见浏览器）"
        print()
        print("=" * 42)
        print("  17zwd 铺货助手（一键启动）")
        print("=" * 42)
        print(f"  程序版本：{__version__}")
        print(f"  平台：{display}    目标店铺：{shop}")
        print(f"  运行模式：{mode}")
        print(f"  每款停顿：{delay} 秒起（随机 {delay}~{delay * 2:.0f} 秒）")
        print("-" * 42)
        print("  1. 看货（扫描关注档口，不上架）")
        print("  2. 刷新铺货记录（去重）")
        print("  3. 上架最新 N 款（真实发布）")
        print("  4. 上架指定商品 ID（真实发布）")
        print("  5. 上架档口老品（贴档口商品页链接）")
        print("  6. 校对档口铺货记录（不等同于在售）")
        print("  7. 导出上架清单（Excel，含档口）")
        print("  8. 查看批次 / 继续未完成项")
        print("  9. 切换平台（拼多多/抖音）")
        print(" 10. 选择运行模式（前台/后台）")
        print("  0. 退出")
        print("=" * 42)

        choice = input("请选择 (0-10)：").strip()
        if choice == "0":
            print("退出。")
            return 0
        if choice == "9":
            platform = "douyin" if platform == "pinduoduo" else "pinduoduo"
            continue
        if choice == "10":
            print(f"当前运行模式：{mode}")
            print("  1. 前台（可见浏览器，便于登录和验证码）")
            print("  2. 后台（不弹浏览器窗口）")
            while True:
                mode_choice = input("选择运行模式 (1/2，回车保持当前模式)：").strip()
                if not mode_choice:
                    break
                if mode_choice in ("1", "2"):
                    settings.headless = mode_choice == "2"
                    selected_mode = "后台（不弹浏览器）" if settings.headless else "前台（可见浏览器）"
                    print(f"已选择{selected_mode}，后续任务使用此模式。")
                    break
                print("请输入 1 或 2，或回车保持当前模式。")
            continue
        if choice == "1":
            args = argparse.Namespace(
                platform=platform, pages=1, item_ids="", limit=0,
                target_shop="", confirm_publish="", hold_open_seconds=0,
            )
            _run(list_from_shops(args, settings))
        elif choice == "2":
            args = argparse.Namespace(platform=platform, pages=1, login_wait_seconds=None)
            _run(scan_records(args, settings))
        elif choice == "3":
            if not shop:
                print("发布上架需要先填写目标店铺名（不能跳过）。")
                continue
            value = input("要上架最新几款？").strip()
            if not value.isdigit():
                print("请输入数字。")
                continue
            args = argparse.Namespace(
                platform=platform, pages=1, item_ids="", limit=int(value),
                target_shop=shop, confirm_publish="PUBLISH", delay_seconds=delay, hold_open_seconds=0,
            )
            _run(list_from_shops(args, settings))
        elif choice == "4":
            if not shop:
                print("发布上架需要先填写目标店铺名（不能跳过）。")
                continue
            ids = input("要上架的商品 ID（逗号分隔）？").strip()
            args = argparse.Namespace(
                platform=platform, pages=1, item_ids=ids, limit=0,
                target_shop=shop, confirm_publish="PUBLISH", delay_seconds=delay, hold_open_seconds=0,
            )
            _run(list_from_shops(args, settings))
        elif choice == "5":
            if not shop:
                print("发布上架需要先填写目标店铺名（不能跳过）。")
                continue
            url = input("档口商品页链接（含 page=1&search=y）：").strip()
            start = input("从第几页开始？（默认 1）：").strip() or "1"
            pages = input("扫描几页？（默认 1）：").strip() or "1"
            try:
                start_page = int(start)
            except ValueError:
                start_page = 1
            try:
                page_count = int(pages)
            except ValueError:
                page_count = 1
            args = argparse.Namespace(
                shop_url=url, platform=platform, pages=page_count, start_page=start_page,
                item_ids="", limit=0, pick=True, target_shop=shop, confirm_publish="PUBLISH",
                delay_seconds=delay, hold_open_seconds=0,
            )
            _run(list_from_shop(args, settings))
        elif choice == "6":
            url = input("档口商品页链接（含 page=1&search=y）：").strip()
            start = input("从第几页开始？（默认 1）：").strip() or "1"
            pages = input("扫描档口几页？（默认 1）：").strip() or "1"
            try:
                start_page = int(start)
            except ValueError:
                start_page = 1
            try:
                page_count = int(pages)
            except ValueError:
                page_count = 1
            args = argparse.Namespace(
                shop_url=url, platform=platform, pages=page_count, start_page=start_page, record_pages=20,
                target_shop=shop,
            )
            _run(reconcile_shop(args, settings))
        elif choice == "7":
            try:
                export_report(settings, Platform.parse(platform))
            except Exception as exc:  # noqa: BLE001 — 导出失败保留菜单
                print(f"\n[失败] {exc}\n")
        elif choice == "8":
            list_batches(settings)
            batch_id = input("输入要继续的批次 ID（回车返回）：").strip()
            if batch_id:
                _run(resume_batch(argparse.Namespace(batch_id=batch_id), settings))
        else:
            print("无效选择，请重新输入。")


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    settings = Settings.from_environment()
    settings.ensure_directories()

    if args.command == "init-db":
        initialize(settings.database_path)
        print(f"数据库已初始化：{settings.database_path}")
        return 0
    if args.command == "list-batches":
        return list_batches(settings)
    if args.command == "resume-batch":
        return asyncio.run(resume_batch(args, settings))
    if args.command == "login":
        return asyncio.run(login(settings))
    if args.command == "export-report":
        platform = None if args.platform == "all" else Platform.parse(args.platform)
        return export_report(settings, platform)
    if args.command == "resolve-submission":
        return resolve_submission(args, settings)
    if args.command == "scan-records":
        return asyncio.run(scan_records(args, settings))
    if args.command == "prepare-upload":
        return asyncio.run(prepare_upload(args, settings))
    if args.command == "list-from-shops":
        return asyncio.run(list_from_shops(args, settings))
    if args.command == "list-from-shop":
        return asyncio.run(list_from_shop(args, settings))
    if args.command == "reconcile-shop":
        return asyncio.run(reconcile_shop(args, settings))
    if args.command == "publish-drafts":
        return asyncio.run(publish_drafts(args, settings))
    if args.command == "resolve-draft":
        return resolve_draft(args, settings)
    if args.command == "menu":
        return run_menu(settings)

    parser.error(f"未知命令：{args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
