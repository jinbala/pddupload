from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator


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
    source_platform: Platform
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
        mode="before",
    )
    @classmethod
    def normalize_text(cls, value: object) -> str:
        return "" if value is None else str(value).strip()

    @property
    def identity(self) -> str:
        return self.source_goods_id or self.source_row_key


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
    def on_shelf_sort_key(self) -> tuple[int, int]:
        """把“MM-DD”上新日期转成可排序的 (月, 日)。跨年场景按简单启发处理。"""
        if "-" in self.on_shelf_date_text:
            month, _, day = self.on_shelf_date_text.partition("-")
            if month.isdigit() and day.isdigit():
                return (int(month), int(day))
        return (0, 0)
