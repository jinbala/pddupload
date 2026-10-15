from __future__ import annotations

import argparse
import math


def nonnegative_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 0:
        raise argparse.ArgumentTypeError("必须是有限的非负数")
    return parsed


def nonnegative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("必须是非负整数")
    return parsed

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pdd-listing")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init-db", help="初始化本地 SQLite 数据库")
    subparsers.add_parser("list-batches", help="查看最近批次和可恢复进度")
    resume = subparsers.add_parser("resume-batch", help="恢复批次中未完成的商品")
    resume.add_argument("--batch-id", required=True)

    scan = subparsers.add_parser("scan-records", help="采集 17zwd 上传记录")
    scan.add_argument(
        "--platform",
        choices=["pinduoduo", "douyin", "拼多多", "抖音"],
        default="pinduoduo",
    )
    scan.add_argument("--pages", type=nonnegative_int, default=1, help="最多采集多少页")
    scan.add_argument(
        "--login-wait-seconds",
        type=nonnegative_int,
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
        type=nonnegative_int,
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
    list_shops.add_argument("--pages", type=nonnegative_int, default=1, help="采集关注档口多少页")
    list_shops.add_argument(
        "--item-ids",
        default="",
        help="要上架的商品 ID，逗号分隔；与 --limit 都不给则只扫描不发布",
    )
    list_shops.add_argument(
        "--limit",
        type=nonnegative_int,
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
    list_shops.add_argument("--delay-seconds", type=nonnegative_float, default=15.0, help="每款之间停顿秒数（防风控，实际随机 1~2 倍）")
    list_shops.add_argument("--hold-open-seconds", type=nonnegative_int, default=0)

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
    list_shop.add_argument("--pages", type=nonnegative_int, default=1, help="扫描多少页（每页约 140 款）")
    list_shop.add_argument("--start-page", type=nonnegative_int, default=1, help="从第几页开始扫")
    list_shop.add_argument("--item-ids", default="", help="要上架的商品 ID，逗号分隔")
    list_shop.add_argument("--limit", type=nonnegative_int, default=0, help="按上新日期倒序取前 N 款上架")
    list_shop.add_argument("--pick", action="store_true", help="扫描后按序号交互选择上架")
    list_shop.add_argument("--target-shop", default="", help="拼多多目标店铺名")
    list_shop.add_argument("--confirm-publish", default="", metavar="PHRASE")
    list_shop.add_argument("--delay-seconds", type=nonnegative_float, default=15.0, help="每款之间停顿秒数（防风控，实际随机 1~2 倍）")
    list_shop.add_argument("--hold-open-seconds", type=nonnegative_int, default=0)

    reconcile = subparsers.add_parser(
        "reconcile-shop",
        help="校对档口商品与已扫描的铺货记录，不核验在售状态",
    )
    reconcile.add_argument("--shop-url", required=True, help="档口商品页 URL（含 page=1&search=y）")
    reconcile.add_argument(
        "--platform",
        choices=["pinduoduo", "douyin", "拼多多", "抖音"],
        default="pinduoduo",
    )
    reconcile.add_argument("--pages", type=nonnegative_int, default=1, help="扫描档口多少页")
    reconcile.add_argument("--start-page", type=nonnegative_int, default=1, help="从第几页开始扫")
    reconcile.add_argument("--record-pages", type=nonnegative_int, default=20, help="扫描铺货记录多少页")
    reconcile.add_argument("--target-shop", default="", help="限定本地记录的目标店铺")

    publish = subparsers.add_parser(
        "publish-drafts",
        help="拼多多后台草稿箱：改价并提交上架（最后一步）",
    )
    publish.add_argument("--multiple", type=float, default=1.5, help="售价=货源价×倍数（可自定义）")
    publish.add_argument("--target-shop", required=True, help="当前拼多多后台的完整店铺名")
    publish.add_argument("--source-market", default="", help="限定成本来源站点，例如 cs.17zwd.com")
    publish.add_argument("--limit", type=nonnegative_int, default=0, help="最多处理多少个草稿，默认全部")
    publish.add_argument("--delay-seconds", type=nonnegative_float, default=10.0, help="每个草稿之间停顿秒数（防风控，实际随机 1~2 倍）")

    subparsers.add_parser("menu", help="交互式菜单（一键启动）")
    draft_review = subparsers.add_parser("resolve-draft", help="人工核对后台后解除或确认本地草稿提交记录")
    draft_review.add_argument("--target-shop", required=True)
    draft_review.add_argument("--source-market", default="")
    draft_review.add_argument("--item-id", required=True)
    draft_review.add_argument("--outcome", choices=["submitted", "not-submitted"], required=True)

    subparsers.add_parser(
        "login",
        help="单独打开浏览器登录 17zwd（强制有头模式，登录态持久化）",
    )

    report = subparsers.add_parser(
        "export-report",
        help="把铺货记录导出成 Excel（含档口/货号/价格/上架时间）",
    )
    report.add_argument(
        "--platform",
        choices=["pinduoduo", "douyin", "拼多多", "抖音", "all"],
        default="all",
        help="只导出某个平台，默认 all（全部）",
    )
    resolve = subparsers.add_parser("resolve-submission", help="人工核对上货日志后处理待核实记录")
    resolve.add_argument("--platform", choices=["pinduoduo", "douyin"], required=True)
    resolve.add_argument("--target-shop", required=True)
    resolve.add_argument("--item-id", required=True)
    resolve.add_argument("--source-market", default=None, help="来源站点，也可传入完整商品链接")
    resolve.add_argument(
        "--outcome", choices=["recorded", "not-submitted"], required=True,
        help="日志确认已铺货选 recorded；确认未提交选 not-submitted，允许重新处理",
    )
    resolve.add_argument(
        "--include-recorded", action="store_true",
        help="核对日志确认历史误标后，允许解除指定店铺 recorded 记录的拦截；不删除平台商品",
    )
    return parser
