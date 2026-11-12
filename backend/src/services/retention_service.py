"""算法 1：同期群留存矩阵重建（订单事实 → 钱包台账 + 留存格）。

口径见 `retention_core` / `billing_core`（纯函数）；本模块只负责读订单、整表重建与落库。

储值制核算链路：
- 充值（recharge）只形成待消费余额，不进营收；
- 消费（consume）按消费发生月确认净营收，跨月核销归真实消费月；
- 退款（refund）在退款发生月冲减：退消费冲净营收，退未核销充值只减余额；
- 活跃只认「未被全额退款的消费」，仅充值/退款不算活跃；
- 对账恒等式不平（差异 > 1 分、负余额、退款超额）直接抛错，不提交脏结果。
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import delete, distinct, func, select
from sqlalchemy.orm import Session

from src.config.settings import settings
from src.constants import error_codes as codes
from src.constants.enums import COHORT_CHANNELS
from src.models.cohort import Cohort
from src.models.member_balance import MemberBalance
from src.models.member_order import MemberOrder
from src.models.retention_cell import RetentionCell
from src.services.retention_core import CohortInput, build_retention_cells
from src.utils.errors import AppError
from src.utils.logger import get_logger

logger = get_logger("cohortltv.retention")


def _load_orders(db: Session) -> list[dict]:
    """读全部订单事实（重建的唯一原料），按时间稳定排序。"""
    orders = db.execute(
        select(MemberOrder).order_by(
            MemberOrder.cohort_month, MemberOrder.month_index, MemberOrder.order_no
        )
    ).scalars().all()
    return [
        {
            "order_no": o.order_no,
            "member_code": o.member_code,
            "cohort_month": o.cohort_month,
            "channel": o.channel,
            "order_type": o.order_type,
            "order_month": o.order_month,
            "month_index": o.month_index,
            "amount": float(o.amount),
            "refund_order_no": o.refund_order_no,
            "refund_type": o.refund_type,
        }
        for o in orders
    ]


def rebuild_matrix(
    db: Session,
    snapshot_month: str | None = None,
    months: int | None = None,
) -> dict:
    snapshot = snapshot_month or settings.snapshot_month
    months = months or settings.matrix_months
    cohorts = db.execute(select(Cohort).order_by(Cohort.month)).scalars().all()
    if not cohorts:
        raise AppError(codes.MATRIX_EMPTY, "还没有同期群数据，无法重建矩阵")

    orders = _load_orders(db)
    if not orders:
        raise AppError(codes.MATRIX_EMPTY, "还没有订单事实，无法重建矩阵")

    # 渠道分母：该「群 × 渠道」去重会员数（含只充值未消费的会员，保证分母不丢）
    member_aggs = db.execute(
        select(
            MemberOrder.cohort_month,
            MemberOrder.channel,
            func.count(distinct(MemberOrder.member_code)),
        ).group_by(MemberOrder.cohort_month, MemberOrder.channel)
    ).all()

    build = build_retention_cells(
        [
            CohortInput(
                key=str(cohort.id),
                month=cohort.month,
                size=int(cohort.size or 0),
                channel_mix=cohort.channel_mix or {},
            )
            for cohort in cohorts
        ],
        orders,
        member_aggs,
        snapshot,
        months,
        settings.low_sample_threshold,
    )

    reconcile = build.wallet.reconcile
    if not reconcile["balanced"]:
        # 明细与余额对不上：禁止落库发布口径，把差异原因抛给调用方
        logger.error("reconcile mismatch: %s", reconcile)
        raise AppError(
            codes.RECONCILE_MISMATCH,
            detail=(
                f"global_diff={reconcile['reconcile_diff']} "
                f"cell_revenue_diff={reconcile['cell_revenue_diff']} "
                f"negative_balance={reconcile['negative_balance_members']} "
                f"member_diff={reconcile['member_diff_count']}"
            ),
        )

    # 矩阵与余额台账都是纯派生数据：整表重建（幂等）
    db.execute(delete(RetentionCell))
    db.execute(delete(MemberBalance))
    now = datetime.now(timezone.utc)
    cohort_ids = {str(cohort.id): cohort.id for cohort in cohorts}
    rows = [
        RetentionCell(
            cohort_id=cohort_ids[row["cohort_key"]],
            month_index=row["month_index"],
            channel=row["channel"],
            active_members=row["active_members"],
            sample_size=row["sample_size"],
            retention_rate=row["retention_rate"],
            revenue=row["revenue"],
            recharge_amount=row["recharge_amount"],
            refund_consume_amount=row["refund_consume_amount"],
            refund_recharge_amount=row["refund_recharge_amount"],
            pending_balance=row["pending_balance"],
            order_count=row["order_count"],
            right_censored=row["right_censored"],
            low_sample=row["low_sample"],
            updated_at=now,
        )
        for row in build.rows
    ]
    db.bulk_save_objects(rows)

    balance_rows = [
        MemberBalance(
            member_code=member["member_code"],
            cohort_month=member["cohort_month"],
            recharge_total=member["recharge_total"],
            consume_total=member["consume_total"],
            refund_consume_total=member["refund_consume_total"],
            refund_recharge_total=member["refund_recharge_total"],
            balance=member["balance"],
            reconcile_diff=member["reconcile_diff"],
            updated_at=now,
        )
        for member in build.wallet.members
    ]
    db.bulk_save_objects(balance_rows)
    db.flush()

    for cohort in cohorts:
        cohort.rebuilt_at = now
        if cohort.channel_mix:
            mix = dict(cohort.channel_mix)
            for channel in COHORT_CHANNELS:
                entry = mix.get(channel) or {}
                entry["members"] = build.channel_members.get((cohort.month, channel), 0)
                mix[channel] = entry
            cohort.channel_mix = mix

    db.commit()
    summary = {**build.summary, "rebuilt_at": now.isoformat()}
    logger.info("matrix rebuilt %s", summary)
    return summary


def cell_state(cell: RetentionCell) -> str:
    from src.constants.enums import CellState

    return CellState.RIGHT_CENSORED if cell.right_censored else CellState.OBSERVED
