from __future__ import annotations

from collections import Counter

from pdd_listing_automation.models import ListingStatus, Platform

LISTING_LABELS = {
    ListingStatus.RECORDED: "历史铺货记录（未核验在售）",
    ListingStatus.SUBMITTED: "已提交（待核验上架）",
    ListingStatus.UNCERTAIN: "提交结果待核实",
}


def build_batch_summary(rows: list[dict[str, str]], total: int) -> dict[str, int]:
    statuses = Counter(row["状态"] for row in rows)
    return {
        "本次选择": total,
        "已处理": len(rows),
        "提交成功": statuses[LISTING_LABELS[ListingStatus.SUBMITTED]],
        "失败": statuses["失败"],
        "跳过": statuses["跳过（已有记录）"],
        "待核实": statuses[LISTING_LABELS[ListingStatus.UNCERTAIN]],
        "准备完成（未发布）": statuses["准备完成（未发布）"],
        "未处理": max(0, total - len(rows)),
    }


def format_batch_summary(
    rows: list[dict[str, str]], total: int, platform: Platform, target_shop: str,
    stop_reason: str = "",
) -> str:
    counts = build_batch_summary(rows, total)
    lines = [
        "=" * 48,
        "本次铺货总结",
        f"平台：{platform.display_name}  目标店铺：{target_shop or '未指定'}",
        f"本次选择 {total} 款，已处理 {counts['已处理']} 款，未处理 {counts['未处理']} 款",
        (f"提交成功 {counts['提交成功']} 款 | 失败 {counts['失败']} 款 | "
         f"跳过 {counts['跳过']} 款 | 待核实 {counts['待核实']} 款"),
    ]
    if counts["准备完成（未发布）"]:
        lines.append(f"仅准备、未发布：{counts['准备完成（未发布）']} 款")
    lines.append("平台最终上架状态尚未核验；上述成功数量指提交成功。")
    for status, label in (("失败", "失败"), (LISTING_LABELS[ListingStatus.UNCERTAIN], "待核实")):
        matches = [row for row in rows if row["状态"] == status]
        if not matches:
            continue
        lines.append(f"{label}商品 ID：{','.join(row['商品ID'] for row in matches)}")
        reasons = Counter(
            " ".join(row.get("失败原因", "").split()) or "未获得明确结果，详见提交诊断"
            for row in matches
        )
        for reason, count in reasons.items():
            lines.append(f"  {count} 款：{reason[:200]}")
    if stop_reason:
        lines.append(f"提前结束原因：{stop_reason}")
    lines.append("=" * 48)
    return "\n".join(lines)
