from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta, timezone
from enum import StrEnum
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator, model_validator


def market_from_url(url: str) -> str:
    """只从商品详情链接识别市场，搜索/图片域名不能代表货源站点。"""
    parsed = urlparse(url)
    return (parsed.hostname or "").lower() if parsed.path.startswith("/item/") else ""


def arrival_sort_key(text: str, today: date | None = None) -> tuple[int, int]:
    today = today or datetime.now(timezone(timedelta(hours=8))).date()
    try:
        if len(text.split("-")) == 3:
            value = date.fromisoformat(text)
        else:
            month, day = map(int, text.split("-"))
            # 包含闰日；取不晚于今天的最近一次日期。
            value = None
            for year in range(today.year, today.year - 5, -1):
                try:
                    candidate = date(year, month, day)
                except ValueError:
                    continue
                if candidate <= today:
                    value = candidate
                    break
            if value is None:
                return (0, 0)
        return (value.year, value.toordinal())
    except (ValueError, TypeError):
        return (0, 0)


class Platform(StrEnum):
    PINDUODUO = "pinduoduo"
    DOUYIN = "douyin"

    @classmethod
    def parse(cls, value: str) -> Platform:
        normalized = value.strip().lower()
        aliases = {
            "pdd": cls.PINDUODUO,
            "pinduoduo": cls.PINDUODUO,
            "拼多多": cls.PINDUODUO,
            "dy": cls.DOUYIN,
            "douyin": cls.DOUYIN,
            "抖音": cls.DOUYIN,
        }
        try:
            return aliases[normalized]
        except KeyError as exc:
            raise ValueError(f"Unsupported platform: {value}") from exc

    @property
    def display_name(self) -> str:
        return {
            Platform.PINDUODUO: "拼多多",
            Platform.DOUYIN: "抖音",
        }[self]

    @property
    def upload_record_tab_name(self) -> str:
        name = self.display_name
        return f"{name} {name}"


class TaskStatus(StrEnum):
    DISCOVERED = "discovered"
    READY = "ready"
    NEEDS_HUMAN = "needs_human"
    RUNNING = "running"
    PAUSED_CONFIRM = "paused_confirm"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


class ListingStatus(StrEnum):
    RECORDED = "recorded"
    SUBMITTED = "submitted"
    UNCERTAIN = "uncertain"


class SourceProduct(BaseModel):
    source_goods_id: str
    source_row_key: str
    title: str
    source_sku: str = ""
    price_text: str = ""
    source_shop: str = ""
    source_category: str = ""
    image_url: str = ""
    find_similar_url: str = ""
    source_market: str = ""
    source_url: str = ""
    source_platform: Platform
    target_shop: str = ""
    listing_status: ListingStatus = ListingStatus.RECORDED
    uploaded_at_text: str = ""
    collected_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator(
        "source_goods_id",
        "source_row_key",
        "title",
        "source_sku",
        "price_text",
        "source_shop",
        "source_category",
        "image_url",
        "find_similar_url",
        "uploaded_at_text",
        "target_shop",
        "source_market",
        "source_url",
        mode="before",
    )
    @classmethod
    def normalize_text(cls, value: object) -> str:
        return "" if value is None else str(value).strip()

    @model_validator(mode="after")
    def normalize_market(self):
        self.source_market = self.source_market.lower() or market_from_url(self.source_url)
        return self

    @property
    def identity(self) -> str:
        return self.source_goods_id or self.source_row_key

    @property
    def storage_key(self) -> str:
        return json.dumps(
            [self.source_platform.value, self.target_shop, self.source_market, self.identity],
            ensure_ascii=False, separators=(",", ":"),
        )


class PublishTask(BaseModel):
    task_id: str
    source_goods_id: str
    target_platform: Platform
    target_shop_id: str
    template_id: str = ""
    mode: str = "edit"
    status: TaskStatus = TaskStatus.DISCOVERED
    attempt_count: int = 0
    last_error: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def unique_key(self) -> str:
        return f"{self.source_goods_id}:{self.target_platform}:{self.target_shop_id}"


class ShopBinding(BaseModel):
    platform: Platform
    shop_name: str
    shop_id: str = ""
    authorization_status: str = ""
    authorization_expires_text: str = ""


class ShopNewArrival(BaseModel):
    """档口页每个商品卡片（关注档口上新预览 / 档口全量商品）。"""

    item_id: str
    item_url: str
    title: str = ""
    price_text: str = ""
    on_shelf_date_text: str = ""
    image_url: str = ""
    shop_name: str = ""
    shop_rank: str = ""
    shop_main_category: str = ""

    @field_validator(
        "item_id",
        "item_url",
        "title",
        "price_text",
        "on_shelf_date_text",
        "image_url",
        "shop_name",
        "shop_rank",
        "shop_main_category",
        mode="before",
    )
    @classmethod
    def normalize_text(cls, value: object) -> str:
        return "" if value is None else str(value).strip()

    @property
    def source_market(self) -> str:
        return market_from_url(self.item_url)

    @property
    def product_key(self) -> tuple[str, str]:
        return (self.source_market, self.item_id)

    @property
    def on_shelf_sort_key(self) -> tuple[int, int]:
        return arrival_sort_key(self.on_shelf_date_text)


def has_listing(arrival: ShopNewArrival, keys: set) -> bool:
    # 未知站点的历史数据保持保守拦截；兼容旧调用传入的纯 ID 集合。
    return (arrival.product_key in keys or ("", arrival.item_id) in keys
            or arrival.item_id in keys)
