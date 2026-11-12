"""同期群读取：矩阵、群详情（留存曲线 + 金额分布 + 渠道拆分 + 储值余额）。

金额口径全部走储值制：
- 矩阵/详情里的 revenue 是净营收（消费核销 - 退消费），不含充值；
- recharge_amount 是待消费余额的来源（负债），单列展示；
- pending_balance 是期末待消费余额，只在 all 汇总格维护；
- 金额分布直方图只统计实际消费单（充值/退款不进客单分布）。
"""
from __future__ import annotations

from collections import defaultdict

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.config.settings import settings
from src.constants import error_codes as codes
from src.constants.channels import CHANNEL_LABELS, CHANNEL_NOTES, CHANNEL_SHORT
from src.constants.enums import COHORT_CHANNELS, OrderType, Channel
from src.models.cohort import Cohort
from src.models.member_balance import MemberBalance
from src.models.member_order import MemberOrder
from src.models.retention_cell import RetentionCell
from src.utils.aggregates import bucket_amounts
from src.utils.errors import AppError, not_found
from src.utils.logger import get_logger
from src.utils.months import observed_months

logger = get_logger("cohortltv.cohort")


def _rate_str(rate: float | None) -> str:
    return "—" if rate is None else f"{rate * 100:.1f}%"


def list_cohorts(db: Session) -> list[dict]:
    cohorts = db.execute(select(Cohort).order_by(Cohort.month)).scalars().all()
    revenue_rows = db.execute(
        select(
            RetentionCell.cohort_id,
            func.sum(RetentionCell.revenue),
            func.sum(RetentionCell.active_members),
            func.sum(RetentionCell.pending_balance),
        )
        .where(RetentionCell.channel == Channel.ALL.value)
        .group_by(RetentionCell.cohort_id)
    ).all()
    # 期末待消费余额取该群最大月序格（快照时点余额）
    latest_balance = {row[0]: (float(row[3] or 0)) for row in revenue_rows}
    agg = {row[0]: (float(row[1] or 0), int(row[2] or 0)) for row in revenue_rows}
    out = []
    for cohort in cohorts:
        total_rev, active_sum = agg.get(cohort.id, (0.0, 0))
        obs = observed_months(cohort.month, settings.snapshot_month, settings.matrix_months)
        out.append(
            {
                "month": cohort.month,
                "size": cohort.size,
                "observed_months": obs,
                "cumulative_revenue": round(total_rev, 2),
                "net_revenue": round(total_rev, 2),
                "pending_balance": round(latest_balance.get(cohort.id, 0.0), 2),
                "arpu": round(total_rev / cohort.size, 2) if cohort.size else 0.0,
                "avg_active_ratio": round(active_sum / cohort.size / max(obs, 1), 4)
                if cohort.size
                else 0.0,
                "low_sample": cohort.size < settings.low_sample_threshold,
                "channel_mix": cohort.channel_mix or {},
            }
        )
    return out


def build_matrix(db: Session) -> dict:
    cohorts = db.execute(select(Cohort).order_by(Cohort.month)).scalars().all()
    if not cohorts:
        raise AppError(codes.MATRIX_EMPTY)
    cells = db.execute(
        select(RetentionCell)
        .where(RetentionCell.channel == Channel.ALL.value)
        .order_by(RetentionCell.month_index)
    ).scalars().all()
    if not cells:
        raise AppError(codes.MATRIX_EMPTY, "留存矩阵还没有数据，请先重建矩阵")

    by_cohort: dict[str, dict[int, RetentionCell]] = defaultdict(dict)
    for cell in cells:
        by_cohort[str(cell.cohort_id)][cell.month_index] = cell

    months = settings.matrix_months
    rows = []
    column_active = [0] * months
    column_base = [0] * months
    censored_total = 0
    observed_total = 0
    net_revenue_total = 0.0
    recharge_total = 0.0
    refund_consume_total = 0.0
    rollforward_balance_total = 0.0

    for cohort in cohorts:
        obs = observed_months(cohort.month, settings.snapshot_month, months)
        cohort_end_balance = 0.0
        cells_out = []
        for m in range(months):
            cell = by_cohort[str(cohort.id)].get(m)
            censored = cell.right_censored if cell else m >= obs
            active = cell.active_members if cell and not censored else 0
            revenue = float(cell.revenue) if cell and not censored else 0.0
            recharge = float(cell.recharge_amount) if cell and not censored else 0.0
            refund_consume = (
                float(cell.refund_consume_amount) if cell and not censored else 0.0
            )
            pending = float(cell.pending_balance) if cell and not censored else 0.0
            rate = cell.retention_rate if cell and not censored else None
            if censored:
                censored_total += 1
            else:
                observed_total += 1
                net_revenue_total += revenue
                recharge_total += recharge
                refund_consume_total += refund_consume
                cohort_end_balance = pending  # 该群最后一个观察格即其快照时点余额
                column_active[m] += active
                column_base[m] += cohort.size
            cells_out.append(
                {
                    "m": m,
                    "active": active,
                    "rate": float(rate) if rate is not None else None,
                    "rate_label": _rate_str(float(rate) if rate is not None else None),
                    "state": "right_censored" if censored else "observed",
                    "revenue": round(revenue, 2),
                    "recharge_amount": round(recharge, 2),
                    "refund_consume_amount": round(refund_consume, 2),
                    "pending_balance": round(pending, 2),
                }
            )
        rows.append(
            {
                "month": cohort.month,
                "size": cohort.size,
                "observed_months": obs,
                "low_sample": cohort.size < settings.low_sample_threshold,
                "cells": cells_out,
            }
        )
        # 格子逐月滚动余额的群期末值汇总（用于与会员台账交叉核对）
        rollforward_balance_total += cohort_end_balance

    columns = [
        {
            "m": m,
            "active": column_active[m],
            "base": column_base[m],
            "rate": round(column_active[m] / column_base[m], 6) if column_base[m] else None,
            "rate_label": _rate_str(
                column_active[m] / column_base[m] if column_base[m] else None
            ),
        }
        for m in range(months)
    ]

    low_sample_cells = int(
        db.execute(
            select(func.count())
            .select_from(RetentionCell)
            .where(RetentionCell.low_sample.is_(True), RetentionCell.right_censored.is_(False))
        ).scalar_one()
    )
    # 订单事实按类型计数
    type_counts = dict(
        db.execute(
            select(MemberOrder.order_type, func.count()).group_by(MemberOrder.order_type)
        ).all()
    )
    member_count = db.execute(
        select(func.count(func.distinct(MemberOrder.member_code)))
    ).scalar_one()
    balance_count = db.execute(select(func.count()).select_from(MemberBalance)).scalar_one()

    # 全局对账：余额台账汇总 vs 恒等式
    balance_sums = db.execute(
        select(
            func.coalesce(func.sum(MemberBalance.recharge_total), 0),
            func.coalesce(func.sum(MemberBalance.consume_total), 0),
            func.coalesce(func.sum(MemberBalance.refund_consume_total), 0),
            func.coalesce(func.sum(MemberBalance.refund_recharge_total), 0),
            func.coalesce(func.sum(MemberBalance.balance), 0),
            func.coalesce(func.sum(func.abs(MemberBalance.reconcile_diff)), 0),
        )
    ).one()
    (
        sum_recharge,
        sum_consume,
        sum_refund_consume,
        sum_refund_recharge,
        sum_balance,
        sum_abs_diff,
    ) = (float(v) for v in balance_sums)
    reconcile_diff = round(
        (sum_recharge - sum_consume - sum_refund_recharge) - sum_balance, 2
    )
    reconcile = {
        "balanced": (
            abs(reconcile_diff) <= 0.01
            and float(sum_abs_diff) <= 0.01
            and sum_balance >= 0
        ),
        "recharge_total": round(sum_recharge, 2),
        "consume_total": round(sum_consume, 2),
        "refund_consume_total": round(sum_refund_consume, 2),
        "refund_recharge_total": round(sum_refund_recharge, 2),
        "net_revenue": round(sum_consume - sum_refund_consume, 2),
        "pending_balance": round(sum_balance, 2),
        "reconcile_diff": reconcile_diff,
        "identity": "充值 - 消费 - 退未核销充值 = 期末待消费余额",
        "balance_members": int(balance_count),
    }
    # 两条独立路径（格子逐月滚动 vs 会员台账）的余额必须对得上，否则说明派生数据漂移
    if abs(rollforward_balance_total - sum_balance) > 0.01:
        logger.error(
            "balance roll-forward drift rollforward=%.2f ledger=%.2f",
            rollforward_balance_total, sum_balance,
        )

    return {
        "snapshot_month": settings.snapshot_month,
        "months": months,
        "cohorts": rows,
        "columns": columns,
        "stats": {
            "cohorts": len(rows),
            "cells_total": len(rows) * months,
            "cells_observed": observed_total,
            "cells_censored": censored_total,
            "low_sample_cells": low_sample_cells,
            "orders": int(type_counts.get(OrderType.CONSUME.value, 0)),
            "recharge_orders": int(type_counts.get(OrderType.RECHARGE.value, 0)),
            "refund_orders": int(type_counts.get(OrderType.REFUND.value, 0)),
            "members": int(member_count),
            "revenue_total": round(net_revenue_total, 2),
            "net_revenue_total": round(net_revenue_total, 2),
            "recharge_total": round(recharge_total, 2),
            "refund_consume_total": round(refund_consume_total, 2),
            # 权威值取会员余额台账；格子滚动余额（各群 pending_balance）是同源的逐月展示
            "pending_balance_total": round(sum_balance, 2),
            "cumulative_arpu": (
                round(net_revenue_total / member_count, 2) if member_count else 0.0
            ),
        },
        "reconcile": reconcile,
        "notes": {
            "matrix": (
                "M0 = 首单月（全员活跃）；Mn 活跃 = 第 n 个月有未被全额退款的消费的会员数 / 群规模；"
                "仅充值或仅退款不算活跃。"
            ),
            "revenue": (
                "净营收在消费/退款发生月确认：净营收 = 消费核销 - 退消费；"
                "跨月核销归真实消费月，充值只形成待消费余额、不计营收。"
            ),
            "balance": "待消费余额 = 累计充值 - 累计消费 - 退未核销充值；退消费不影响余额。",
            "censor": "灰色斜纹格 = 右删失（快照月尚未走到），不进入 λ 拟合与 LTV 测算。",
            "low_sample": f"样本 <{settings.low_sample_threshold} 的群或群×渠道不进入渠道排行。",
        },
    }


def build_detail(db: Session, month: str) -> dict:
    cohort = db.execute(select(Cohort).where(Cohort.month == month)).scalar_one_or_none()
    if cohort is None:
        raise not_found("同期群", month)
    obs = observed_months(cohort.month, settings.snapshot_month, settings.matrix_months)

    cells = db.execute(
        select(RetentionCell)
        .where(RetentionCell.cohort_id == cohort.id)
        .order_by(RetentionCell.month_index)
    ).scalars().all()
    all_cells = sorted(
        [c for c in cells if c.channel == Channel.ALL.value], key=lambda c: c.month_index
    )

    curve = []
    for cell in all_cells:
        if cell.right_censored:
            continue
        curve.append(
            {
                "m": cell.month_index,
                "active": cell.active_members,
                "rate": float(cell.retention_rate) if cell.retention_rate is not None else 0.0,
                "rate_label": _rate_str(
                    float(cell.retention_rate) if cell.retention_rate is not None else None
                ),
                "revenue": float(cell.revenue),
                "recharge_amount": float(cell.recharge_amount),
                "refund_consume_amount": float(cell.refund_consume_amount),
                "pending_balance": float(cell.pending_balance),
            }
        )

    # 订单金额分布：只看实际消费单（充值是负债、退款为负，均不进客单分布）
    amounts = db.execute(
        select(MemberOrder.amount)
        .where(
            MemberOrder.cohort_month == cohort.month,
            MemberOrder.order_type == OrderType.CONSUME.value,
        )
    ).scalars().all()
    amount_distribution = bucket_amounts(amounts)

    # 渠道拆分：观察期内各渠道贡献与 M1/M3/M6 留存
    channel_rows = []
    mix = cohort.channel_mix or {}
    for channel in COHORT_CHANNELS:
        ch_cells = {c.month_index: c for c in cells if c.channel == channel}
        members = int((mix.get(channel) or {}).get("members", 0))
        revenue = sum(float(c.revenue) for c in ch_cells.values() if not c.right_censored)
        recharge = sum(
            float(c.recharge_amount) for c in ch_cells.values() if not c.right_censored
        )
        marks = {}
        for m in (1, 3, 6):
            cell = ch_cells.get(m)
            marks[f"m{m}"] = (
                float(cell.retention_rate)
                if cell is not None and not cell.right_censored and cell.retention_rate is not None
                else None
            )
        channel_rows.append(
            {
                "channel": channel,
                "label": CHANNEL_LABELS.get(channel, channel),
                "short": CHANNEL_SHORT.get(channel, channel),
                "note": CHANNEL_NOTES.get(channel, ""),
                "members": members,
                "sample_size": members,
                "low_sample": members < settings.low_sample_threshold,
                "revenue": round(revenue, 2),
                "recharge_amount": round(recharge, 2),
                "revenue_per_member": round(revenue / members, 2) if members else 0.0,
                "m1": marks["m1"],
                "m3": marks["m3"],
                "m6": marks["m6"],
            }
        )

    cumulative = sum(float(c.revenue) for c in all_cells if not c.right_censored)
    cumulative_recharge = sum(
        float(c.recharge_amount) for c in all_cells if not c.right_censored
    )
    cumulative_refund_consume = sum(
        float(c.refund_consume_amount) for c in all_cells if not c.right_censored
    )
    # 期末余额 = 最后一个观察格的滚动余额
    observed_cells = [c for c in all_cells if not c.right_censored]
    pending_balance = float(observed_cells[-1].pending_balance) if observed_cells else 0.0

    # 该群会员余额台账汇总
    balance_agg = db.execute(
        select(
            func.coalesce(func.sum(MemberBalance.recharge_total), 0),
            func.coalesce(func.sum(MemberBalance.consume_total), 0),
            func.coalesce(func.sum(MemberBalance.refund_consume_total), 0),
            func.coalesce(func.sum(MemberBalance.refund_recharge_total), 0),
            func.coalesce(func.sum(MemberBalance.balance), 0),
        ).where(MemberBalance.cohort_month == cohort.month)
    ).one()
    wallet = {
        "recharge_total": round(float(balance_agg[0]), 2),
        "consume_total": round(float(balance_agg[1]), 2),
        "refund_consume_total": round(float(balance_agg[2]), 2),
        "refund_recharge_total": round(float(balance_agg[3]), 2),
        "pending_balance": round(float(balance_agg[4]), 2),
        "net_revenue": round(float(balance_agg[1]) - float(balance_agg[2]), 2),
    }

    return {
        "month": cohort.month,
        "size": cohort.size,
        "observed_months": obs,
        "snapshot_month": settings.snapshot_month,
        "low_sample": cohort.size < settings.low_sample_threshold,
        "note": cohort.note,
        "curve": curve,
        "amount_distribution": amount_distribution,
        "channels": channel_rows,
        "wallet": wallet,
        "summary": {
            "cumulative_revenue": round(cumulative, 2),
            "net_revenue": round(cumulative, 2),
            "recharge_total": round(cumulative_recharge, 2),
            "refund_consume_total": round(cumulative_refund_consume, 2),
            "pending_balance": round(pending_balance, 2),
            "arpu": round(cumulative / cohort.size, 2) if cohort.size else 0.0,
            "m1": curve[1]["rate"] if len(curve) > 1 else None,
            "m6": curve[6]["rate"] if len(curve) > 6 else None,
            "m12": curve[12]["rate"] if len(curve) > 12 else None,
        },
    }
