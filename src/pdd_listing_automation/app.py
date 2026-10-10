from __future__ import annotations

import argparse
import asyncio
import os
import random
import re
import traceback
from contextlib import suppress
from datetime import UTC, datetime

from playwright.async_api import Page

from pdd_listing_automation.browser.session import BrowserSession
from pdd_listing_automation.config import Settings
from pdd_listing_automation.models import Platform, ShopNewArrival, SourceProduct
from pdd_listing_automation.pages.favorite_shops import FavoriteShopsPage
from pdd_listing_automation.pages.pdd_draft import PddDraftPage
from pdd_listing_automation.pages.product_detail import ProductDetailPage
from pdd_listing_automation.pages.shop_catalog import ShopCatalogPage
from pdd_listing_automation.pages.upload_platform import AlreadyListedError, UploadPlatformPage
from pdd_listing_automation.pages.upload_records import UploadRecordsPage
from pdd_listing_automation.reports.exporter import export_models
from pdd_listing_automation.services.discovery import DiscoveryService
from pdd_listing_automation.storage.database import connect, initialize
from pdd_listing_automation.storage.repositories import SourceProductRepository


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pdd-listing")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init-db", help="初始化本地 SQLite 数据库")

    scan = subparsers.add_parser("scan-records", help="采集 17zwd 上传记录")
    scan.add_argument(
        "--platform",
        choices=["pinduoduo", "douyin", "拼多多", "抖音"],
        default="pinduoduo",
    )
    scan.add_argument("--pages", type=int, default=1, help="最多采集多少页")
    scan.add_argument(
        "--login-wait-seconds",
        type=int,
        default=None,
        help="等待人工完成登录的秒数",
    )

    prepare = subparsers.add_parser(
        "prepare-upload",
        help="从 17zwd 新品商品详情准备一次上传",
    )
    prepare.add_argument("--item-id", required=True, help="17zwd 商品 ID")
    prepare.add_argument(
        "--platform",
        choices=["pinduoduo", "douyin", "拼多多", "抖音"],
        required=True,
    )
    prepare.add_argument(
        "--hold-open-seconds",
        type=int,
        default=0,
        help="到达店铺选择后保持浏览器打开的秒数",
    )
    prepare.add_argument("--shop-name", default="", help="要选择的上货店铺名称")
    prepare.add_argument(
        "--confirm-publish",
        default="",
        metavar="PHRASE",
        help="需要完整执行发布时传入确认短语 PUBLISH",
    )

    list_shops = subparsers.add_parser(
        "list-from-shops",
        help="从关注档口采集上新款，按选择一键上传到拼多多",
    )
    list_shops.add_argument(
        "--platform",
        choices=["pinduoduo", "douyin", "拼多多", "抖音"],
        default="pinduoduo",
    )
    list_shops.add_argument("--pages", type=int, default=1, help="采集关注档口多少页")
    list_shops.add_argument(
        "--item-ids",
        default="",
        help="要上架的商品 ID，逗号分隔；与 --limit 都不给则只扫描不发布",
    )
    list_shops.add_argument(
        "--limit",
        type=int,
        default=0,
        help="按上新日期倒序取前 N 款上架",
    )
    list_shops.add_argument(
        "--target-shop",
        default="",
        help="拼多多目标店铺名（默认空=停在选店窗口人工选）",
    )
    list_shops.add_argument(
        "--confirm-publish",
        default="",
        metavar="PHRASE",
        help="需要完整执行发布时传入确认短语 PUBLISH",
    )
    list_shops.add_argument("--delay-seconds", type=float, default=15.0, help="每款之间停顿秒数（防风控，实际随机 1~2 倍）")
    list_shops.add_argument("--hold-open-seconds", type=int, default=0)

    list_shop = subparsers.add_parser(
        "list-from-shop",
        help="扫描档口全量商品（含老品），按选择一键上传到拼多多",
    )
    list_shop.add_argument("--shop-url", required=True, help="档口商品页 URL（含 page=1&search=y）")
    list_shop.add_argument(
        "--platform",
        choices=["pinduoduo", "douyin", "拼多多", "抖音"],
        default="pinduoduo",
    )
    list_shop.add_argument("--pages", type=int, default=1, help="扫描多少页（每页约 140 款）")
    list_shop.add_argument("--start-page", type=int, default=1, help="从第几页开始扫")
    list_shop.add_argument("--item-ids", default="", help="要上架的商品 ID，逗号分隔")
    list_shop.add_argument("--limit", type=int, default=0, help="按上新日期倒序取前 N 款上架")
    list_shop.add_argument("--pick", action="store_true", help="扫描后按序号交互选择上架")
    list_shop.add_argument("--target-shop", default="", help="拼多多目标店铺名")
    list_shop.add_argument("--confirm-publish", default="", metavar="PHRASE")
    list_shop.add_argument("--delay-seconds", type=float, default=15.0, help="每款之间停顿秒数（防风控，实际随机 1~2 倍）")
    list_shop.add_argument("--hold-open-seconds", type=int, default=0)

    reconcile = subparsers.add_parser(
        "reconcile-shop",
        help="校对档口商品：对比铺货记录，列出已上架和漏上的",
    )
    reconcile.add_argument("--shop-url", required=True, help="档口商品页 URL（含 page=1&search=y）")
    reconcile.add_argument(
        "--platform",
        choices=["pinduoduo", "douyin", "拼多多", "抖音"],
        default="pinduoduo",
    )
    reconcile.add_argument("--pages", type=int, default=1, help="扫描档口多少页")
    reconcile.add_argument("--start-page", type=int, default=1, help="从第几页开始扫")
    reconcile.add_argument("--record-pages", type=int, default=20, help="扫描铺货记录多少页")

    publish = subparsers.add_parser(
        "publish-drafts",
        help="拼多多后台草稿箱：改价并提交上架（最后一步）",
    )
    publish.add_argument("--multiple", type=float, default=1.5, help="售价=货源价×倍数（可自定义）")
    publish.add_argument("--limit", type=int, default=0, help="最多处理多少个草稿，默认全部")
    publish.add_argument("--delay-seconds", type=float, default=10.0, help="每个草稿之间停顿秒数（防风控，实际随机 1~2 倍）")

    subparsers.add_parser("menu", help="交互式菜单（一键启动）")

    subparsers.add_parser(
        "login",
        help="单独打开浏览器登录 17zwd（强制有头模式，登录态持久化）",
    )
    return parser


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
    log_path = settings.artifacts_dir / "failures.log"
    with suppress(OSError), log_path.open("a", encoding="utf-8") as handle:
        handle.write(f"[{timestamp}] id={item_id} {type(error).__name__}: {error}\n")
        handle.write("".join(traceback.format_exception(error)))
        handle.write("\n")


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
) -> tuple[dict[str, str], Page | None, bool]:
    """单个商品的完整上传流程，停在“选择上货店铺”；确认短语为 PUBLISH 时才点开始上传。"""
    product_page = ProductDetailPage(page)
    await product_page.open(item_url)
    summary = await product_page.collect_summary(item_id)
    platform_page_target = await product_page.start_upload(platform)

    platform_page = UploadPlatformPage(platform_page_target)
    await platform_page.confirm_superboss()
    await platform_page.wait_for_store_selection()

    if target_shop:
        await platform_page.select_shop(target_shop)

    # 注意：不调用 bring_to_front，避免浏览器窗口抢焦点弹到前台；Playwright 截图无需前台
    screenshot_path = settings.artifacts_dir / f"{screenshot_stem}.png"
    await platform_page_target.screenshot(path=str(screenshot_path), full_page=False)

    published = False
    if confirm_publish:
        if confirm_publish != "PUBLISH":
            raise RuntimeError("发布确认短语不正确，必须为 PUBLISH")
        if not target_shop:
            raise RuntimeError("执行发布时必须指定 --target-shop")
        await platform_page.accept_commitment()
        await platform_page.start_upload()
        await platform_page.confirm_listing()
        published = True

    result = {**summary, "screenshot_path": str(screenshot_path)}
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
            "状态：已提交上架任务，请在超级店长页面检查结果。"
            if published
            else "状态：已到“选择上货店铺”，尚未开始上传。"
        )
        print(f"截图：{result['screenshot_path']}")

        if args.hold_open_seconds > 0:
            print(f"浏览器将保持打开 {args.hold_open_seconds} 秒，可人工检查。")
            await page.wait_for_timeout(args.hold_open_seconds * 1000)
    return 0


def print_arrivals(arrivals: list[ShopNewArrival], listed_ids: set[str], label: str = "商品") -> None:
    unlisted = [a for a in arrivals if a.item_id not in listed_ids]
    listed = [a for a in arrivals if a.item_id in listed_ids]
    print(f"{label}共 {len(arrivals)} 款：未上架 {len(unlisted)} 款 / 已上架 {len(listed)} 款（按上新日期倒序）")
    print(f"—— 未上架 {len(unlisted)} 款 ——")
    for index, arrival in enumerate(unlisted, 1):
        price = f"¥{arrival.price_text}" if arrival.price_text else "价格未知"
        date = f"{arrival.on_shelf_date_text}上新" if arrival.on_shelf_date_text else "日期未知"
        name = arrival.title or arrival.shop_name or "无标题"
        print(f"{index:>4}. {name} {price} {date}  id={arrival.item_id}")
    if listed:
        print(f"—— 已上架 {len(listed)} 款（跳过，不再上架）——")
        for arrival in listed:
            name = arrival.title or arrival.shop_name or "无标题"
            print(f"  ⚠ {name} id={arrival.item_id}")


def select_arrivals(
    arrivals: list[ShopNewArrival],
    args: argparse.Namespace,
    listed_ids: set[str],
) -> tuple[list[ShopNewArrival], list[ShopNewArrival]]:
    """按 --item-ids / --limit 选款，返回 (要上架的, 已上过被跳过的)。"""
    if args.item_ids:
        wanted = [part.strip() for part in args.item_ids.split(",") if part.strip()]
        by_id = {arrival.item_id: arrival for arrival in arrivals}
        selected: list[ShopNewArrival] = []
        skipped: list[ShopNewArrival] = []
        for item_id in wanted:
            arrival = by_id.get(item_id)
            if arrival is None:
                raise RuntimeError(
                    f"商品 ID {item_id} 不在本次扫描结果中，请核对 ID 或增加 --pages"
                )
            if arrival.item_id in listed_ids:
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
            if arrival.item_id in listed_ids:
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


def _mark_listed(settings: Settings, arrival: ShopNewArrival, platform: Platform) -> None:
    """把上架成功的商品记入本地库，便于断点续跑时跳过已上架的。"""
    initialize(settings.database_path)
    with connect(settings.database_path) as connection:
        SourceProductRepository(connection).upsert_many(
            [
                SourceProduct(
                    source_goods_id=arrival.item_id,
                    source_row_key=arrival.item_id,
                    title=arrival.title,
                    price_text=arrival.price_text,
                    source_platform=platform,
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
    seen: set[str] = set()
    deduped: list[ShopNewArrival] = []
    for arrival in arrivals:
        if arrival.item_id not in seen:
            seen.add(arrival.item_id)
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
        listed_ids = SourceProductRepository(connection).listed_goods_ids(platform)

    print_arrivals(arrivals, listed_ids, label)

    timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    stem = f"{stem_prefix}-{platform.value}-{timestamp}"
    json_path, csv_path = export_models(arrivals, settings.artifacts_dir, stem)
    print(f"JSON：{json_path}")
    print(f"CSV：{csv_path}")

    unlisted = [a for a in arrivals if a.item_id not in listed_ids]

    if getattr(args, "pick", False) and not args.item_ids and not args.limit:
        text = input("上架哪几款？输入 n 上最新 n 款；输入 1,3,5 或 1-5 按「未上架」序号选；回车只看货：").strip()
        if not text:
            print("未选择，仅完成扫描（未上架）。")
            return 0
        if text.isdigit():
            selected = unlisted[: int(text)]
        else:
            selected = [unlisted[i] for i in parse_selection(text, len(unlisted))]
    else:
        selected, _ = select_arrivals(arrivals, args, listed_ids)

    if not selected:
        print("没有可上架的商品（可能都已上过，或未选择）。")
        return 0

    print(f"本次将处理 {len(selected)} 款。")
    successes = 0
    skipped: list[str] = []
    failures: list[str] = []
    consecutive_failures = 0
    delay_seconds = float(getattr(args, "delay_seconds", 15.0))
    for index, arrival in enumerate(selected, 1):
        name = arrival.title or arrival.shop_name or arrival.item_id
        print(f"[{index}/{len(selected)}] id={arrival.item_id}（{name}）")
        result = None
        published = False
        skip = False
        error: Exception | None = None

        # 上架不做自动重试：一旦点了「确认上货」可能已部分提交，重试会导致重复上架
        platform_page_target: Page | None = None
        try:
            result, platform_page_target, published = await run_single_upload(
                page,
                item_id=arrival.item_id,
                item_url=arrival.item_url,
                platform=platform,
                target_shop=args.target_shop,
                confirm_publish=args.confirm_publish,
                screenshot_stem=f"{stem_prefix}-{platform.value}-{arrival.item_id}",
                settings=settings,
            )
            error = None
        except AlreadyListedError as exc:
            error = exc
            skip = True
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
            successes += 1
            consecutive_failures = 0
            _mark_listed(settings, arrival, platform)
            print(f"  商品：{result['title']}")
            print(f"  货号：{result['sku']}  货源价：¥{result['price']}")
            print("  状态：已提交上架任务。" if published else "  状态：已到“选择上货店铺”，尚未开始上传。")
            print(f"  截图：{result['screenshot_path']}")
        elif skip:
            consecutive_failures = 0
            skipped.append(arrival.item_id)
            print("  ⏭ 跳过（已上架）")
        else:
            failures.append(arrival.item_id)
            consecutive_failures += 1
            print(f"  ❌ 失败：{type(error).__name__}: {error}")
            if "closed" in str(error).lower() or "TargetClosed" in type(error).__name__:
                print("  （浏览器已关闭，停止后续上架）")
                break
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                print(
                    f"  （连续失败 {consecutive_failures} 次，自动暂停上架，"
                    "先排查原因再继续，详见 artifacts/failures.log）"
                )
                break

        # 每款之间随机停顿，降低风控/浏览器崩溃概率
        if delay_seconds > 0 and index < len(selected):
            try:
                await page.wait_for_timeout(delay_seconds * random.uniform(1.0, 2.0) * 1000)
            except Exception as exc:
                if "closed" in str(exc).lower() or "TargetClosed" in type(exc).__name__:
                    print("  （浏览器已关闭，停止后续上架）")
                    break
                raise

    summary = f"完成：成功 {successes} 款"
    if skipped:
        summary += f"，跳过 {len(skipped)} 款（已上架）"
    if failures:
        summary += f"，失败 {len(failures)} 款（失败 ID：{failures}）"
    print(summary)

    if args.hold_open_seconds > 0:
        print(f"浏览器将保持打开 {args.hold_open_seconds} 秒，可人工检查。")
        await page.wait_for_timeout(args.hold_open_seconds * 1000)
    return 0


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
    """校对：对比档口商品与铺货记录，列出已上架和漏上的。"""
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

    listed_ids = {p.source_goods_id for p in listed_products}

    # 去重后对比
    seen: set[str] = set()
    deduped: list[ShopNewArrival] = []
    for arrival in arrivals:
        if arrival.item_id not in seen:
            seen.add(arrival.item_id)
            deduped.append(arrival)

    listed = [a for a in deduped if a.item_id in listed_ids]
    missing = [a for a in deduped if a.item_id not in listed_ids]

    print(f"档口商品共 {len(deduped)} 款：已上架 {len(listed)} 款，漏上 {len(missing)} 款")
    if listed:
        print(f"—— 已上架 {len(listed)} 款 ——")
        for a in listed:
            print(f"  ✅ {a.title} id={a.item_id}")
    print(f"—— 漏上 {len(missing)} 款 ——")
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
    print(f"已上架 CSV：{listed_csv}")
    print(f"漏上 CSV：{missing_csv}")
    return 0


def load_source_prices(settings: Settings) -> dict[str, float]:
    """从本地库读货源价：{17zwd 商品 ID: 货源价}。"""
    initialize(settings.database_path)
    with connect(settings.database_path) as connection:
        rows = connection.execute(
            "SELECT source_goods_id, price_text FROM source_products"
        ).fetchall()
    result: dict[str, float] = {}
    for row in rows:
        text = (row["price_text"] or "").replace("¥", "").replace("￥", "").strip()
        try:
            result[row["source_goods_id"]] = float(text)
        except (TypeError, ValueError):
            continue
    return result


async def publish_drafts(args: argparse.Namespace, settings: Settings) -> int:
    """拼多多后台草稿箱：改价（货源价×倍数）并提交上架，完成最后一步。"""
    multiple = float(args.multiple)
    source_prices = load_source_prices(settings)
    async with BrowserSession(settings).open() as (_, page):
        draft_page = PddDraftPage(page)
        await draft_page.open_draft_box()
        skus = await draft_page.collect_skus()
        print(f"草稿箱共 {len(skus)} 个草稿，售价 = 货源价 × {multiple}")
        if args.limit:
            skus = skus[: args.limit]

        success = 0
        delay = float(getattr(args, "delay_seconds", 10.0))
        for index, sku in enumerate(skus, 1):
            source_price = source_prices.get(sku)
            if source_price is None:
                print(f"[{index}/{len(skus)}] {sku} 未找到货源价，跳过")
                continue
            target = round(source_price * multiple, 2)
            print(f"[{index}/{len(skus)}] {sku} 货源¥{source_price} → ¥{target}")
            edit_page = await draft_page.edit_draft(sku)
            if edit_page is None:
                print("  ❌ 找不到编辑入口，跳过")
                continue
            await edit_page.wait_for_timeout(5_000)
            await draft_page.set_prices(edit_page, source_price, multiple)
            status = await draft_page.submit_and_wait(edit_page)
            if status == "success":
                success += 1
            print(f"  结果：{status}")
            await draft_page.close_page(edit_page)
            # 每个草稿之间随机停顿，降低触发风控的概率
            if delay > 0 and index < len(skus):
                wait = delay * random.uniform(1.0, 2.0)
                print(f"    停顿 {wait:.1f} 秒防风控……")
                await page.wait_for_timeout(wait * 1000)

        print(f"完成：成功 {success} / 共 {len(skus)}")
    return 0


def _run(coro):
    """在菜单里运行一个任务，出错时打印原因并继续留在菜单，不让整程序崩溃。"""
    try:
        return asyncio.run(coro)
    except Exception as exc:
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
        print(f"  平台：{display}    目标店铺：{shop}")
        print(f"  运行模式：{mode}")
        print(f"  每款停顿：{delay} 秒起（随机 {delay}~{delay * 2:.0f} 秒）")
        print("-" * 42)
        print("  1. 看货（扫描关注档口，不上架）")
        print("  2. 刷新铺货记录（去重）")
        print("  3. 上架最新 N 款（真实发布）")
        print("  4. 上架指定商品 ID（真实发布）")
        print("  5. 上架档口老品（贴档口商品页链接）")
        print("  6. 校对档口（已上架/漏上）")
        print("  0. 切换平台（拼多多/抖音）")
        print("  7. 退出")
        print("=" * 42)

        choice = input("请选择 (0-7)：").strip()
        if choice == "0":
            platform = "douyin" if platform == "pinduoduo" else "pinduoduo"
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
            ids = input("要上架的商品 ID（逗号分隔）？").strip()
            args = argparse.Namespace(
                platform=platform, pages=1, item_ids=ids, limit=0,
                target_shop=shop, confirm_publish="PUBLISH", delay_seconds=delay, hold_open_seconds=0,
            )
            _run(list_from_shops(args, settings))
        elif choice == "5":
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
            )
            _run(reconcile_shop(args, settings))
        elif choice == "7":
            print("退出。")
            return 0
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
    if args.command == "login":
        return asyncio.run(login(settings))
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
    if args.command == "menu":
        return run_menu(settings)

    parser.error(f"未知命令：{args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
