"""MemberBalance：会员待消费余额台账（矩阵重建时由订单事实整表重算）。

每个会员一行，记录快照时点的待消费余额与三笔累计发生额。
对账恒等式（金额单位：元）：

    累计充值 - 累计消费 - 累计退款 = 期末待消费余额

其中退款按性质拆两栏：
- refund_consume：退已核销消费（冲减净营收，同时把钱退回，余额不变）；
- refund_recharge：退未核销充值（只减待消费余额，不碰营收）。

因此余额恒等式展开为：

    balance = recharge_total - consume_total - refund_recharge_total

而净营收口径为：

    net_revenue = consume_total - refund_consume_total   （按消费/退款发生月确认）

`reconcile_diff` 恒为 0 才允许发布；非 0 表示明细与余额对不上，重建会直接报错。
"""
from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, Numeric, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.config.settings import settings
from src.models.base import Base, new_uuid, utcnow


class MemberBalance(Base):
    __tablename__ = "member_balances"
    __table_args__ = ({"schema": settings.db_schema},)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    member_code: Mapped[str] = mapped_column(String(24), unique=True, nullable=False, index=True)
    cohort_month: Mapped[str] = mapped_column(String(7), nullable=False, index=True)
    recharge_total: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=0)
    consume_total: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=0)
    refund_consume_total: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, default=0
    )
    refund_recharge_total: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, default=0
    )
    balance: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=0)
    # 恒等式残差，正常为 0.00；非 0 阻断重建提交与报告发布
    reconcile_diff: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )
