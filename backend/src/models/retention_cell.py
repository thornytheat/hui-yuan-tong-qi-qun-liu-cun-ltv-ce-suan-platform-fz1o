"""RetentionCell：留存格（群 × 月序 × 渠道）。

channel='all' 是矩阵用汇总格；四个渠道格给渠道对比用。
右删失 = 快照月尚未走到的月序：active=0，retention 为空，标记 right_censored。

金额口径（储值制）：
- recharge_amount 是充值，只形成待消费余额，不是营收；
- revenue 是净营收 = 消费核销 - 退消费，跨月核销归到真实消费月；
- 退款在退款发生月确认，退未核销充值只减余额、不碰营收；
- pending_balance 只在 all 汇总格维护，是该群截至该月序的待消费余额。
"""
from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Numeric, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.config.settings import settings
from src.models.base import Base, new_uuid, utcnow


class RetentionCell(Base):
    __tablename__ = "retention_cells"
    __table_args__ = (
        UniqueConstraint("cohort_id", "month_index", "channel", name="uq_cell"),
        {"schema": settings.db_schema},
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=new_uuid)
    cohort_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey(f"{settings.db_schema}.cohorts.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    month_index: Mapped[int] = mapped_column(Integer, nullable=False)
    channel: Mapped[str] = mapped_column(String(20), nullable=False, default="all")
    active_members: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sample_size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    retention_rate: Mapped[Decimal | None] = mapped_column(Numeric(8, 6), nullable=True)
    # 净营收 = 消费核销 - 退消费（退款在发生月确认）；充值不计入
    revenue: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=0)
    # 储值充值额（形成待消费余额，不是营收）
    recharge_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=0)
    # 退已核销消费额（正数存储，已在 revenue 中扣减）
    refund_consume_amount: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, default=0
    )
    # 退未核销充值额（正数存储，只减余额，不碰营收）
    refund_recharge_amount: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, default=0
    )
    # 期末待消费余额（仅 channel='all' 汇总格维护，渠道格为 0）
    pending_balance: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=0)
    order_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    right_censored: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    low_sample: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    cohort = relationship("Cohort", lazy="joined")
