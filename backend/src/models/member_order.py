"""MemberOrder：会员订单事实（seed 生成，重建矩阵的唯一原料）。

订单三分类（`order_type`）：
- recharge 储值充值：金额为正，只形成待消费余额（负债），永不确认营收；
- consume 实际消费：金额为正，核销余额，按消费发生月确认净营收；
- refund 退款：金额恒为负，`refund_order_no` 指向被退的原单，
  退消费在退款发生月冲减净营收，退未核销充值只减待消费余额。

跨月核销：消费按自身 order_month 落月，因此跨月使用余额天然归到真实消费月，
与充值发生月无关。
"""
from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    Numeric,
    String,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.config.settings import settings
from src.models.base import Base, TimestampMixin, new_uuid, utcnow


class MemberOrder(Base, TimestampMixin):
    __tablename__ = "member_orders"
    __table_args__ = (
        Index("ix_member_orders_cohort_month", "cohort_month", "month_index"),
        Index("ix_member_orders_channel", "channel"),
        Index("ix_member_orders_type_month", "order_type", "order_month"),
        Index("ix_member_orders_refund", "refund_order_no"),
        # 金额符号与订单类型绑定：充值/消费为正，退款恒为负（不允许出现正金额退款）
        CheckConstraint(
            "(order_type = 'refund' AND amount < 0) OR "
            "(order_type IN ('recharge', 'consume') AND amount > 0)",
            name="ck_member_orders_amount_sign",
        ),
        # 只有退款单可以关联原单，且退款必须声明退的是充值还是消费
        CheckConstraint(
            "(order_type = 'refund' AND refund_order_no IS NOT NULL "
            "AND refund_type IS NOT NULL) OR "
            "(order_type <> 'refund' AND refund_order_no IS NULL "
            "AND refund_type IS NULL)",
            name="ck_member_orders_refund_link",
        ),
        {"schema": settings.db_schema},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    order_no: Mapped[str] = mapped_column(String(40), unique=True, nullable=False, index=True)
    member_code: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    cohort_month: Mapped[str] = mapped_column(String(7), nullable=False)
    channel: Mapped[str] = mapped_column(String(20), nullable=False)
    store_code: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    order_type: Mapped[str] = mapped_column(String(16), nullable=False, default="consume")
    order_month: Mapped[str] = mapped_column(String(7), nullable=False)
    month_index: Mapped[int] = mapped_column(Integer, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    # 退款单指向被退的原订单号（consume / recharge），非退款单为 NULL
    refund_order_no: Mapped[str | None] = mapped_column(String(40), nullable=True)
    # 退款性质：consume=退已核销消费（冲营收），recharge=退未核销充值（只减余额）
    refund_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    is_first_order: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    ordered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
