"""共享枚举（后端）：渠道 / 留存格状态 / 报告状态 / 任务状态 / 衰减模型。

前端同名枚举在 frontend/src/constants/enums.ts，README「枚举出现位置」列出全部落点。
"""
from __future__ import annotations

from enum import StrEnum


class Channel(StrEnum):
    PRIVATE = "private"        # 私域社群
    PAID_FEED = "paid_feed"    # 信息流投放
    STORE = "store"            # 门店自然流
    PARTNER = "partner"        # 异业合作
    ALL = "all"                # 汇总行（仅 RetentionCell 使用）


class OrderType(StrEnum):
    RECHARGE = "recharge"      # 储值充值：只形成待消费余额（负债），不确认营收
    CONSUME = "consume"        # 实际消费：核销余额，消费发生月确认营收
    REFUND = "refund"          # 退款：负数金额，关联原单，退款发生月冲减


class RefundType(StrEnum):
    CONSUME = "consume"        # 退已核销消费：冲减退款月净营收
    RECHARGE = "recharge"      # 退未核销充值：只减待消费余额，不碰营收


class CellState(StrEnum):
    OBSERVED = "observed"
    RIGHT_CENSORED = "right_censored"


class ReportStatus(StrEnum):
    DRAFT = "draft"
    PUBLISHED = "published"
    ARCHIVED = "archived"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class DecayModel(StrEnum):
    EXPONENTIAL = "exponential"   # R_m = a * exp(-lambda * m)


CHANNEL_VALUES: tuple[str, ...] = (
    Channel.PRIVATE, Channel.PAID_FEED, Channel.STORE, Channel.PARTNER, Channel.ALL,
)
COHORT_CHANNELS: tuple[str, ...] = (
    Channel.PRIVATE, Channel.PAID_FEED, Channel.STORE, Channel.PARTNER,
)
