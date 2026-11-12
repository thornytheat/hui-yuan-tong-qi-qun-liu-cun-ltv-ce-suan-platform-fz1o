"""算法 2：LTV 与回本周期测算（数据库加载 + `ltv_core` 纯函数）。

口径（README「算法口径」与前端口径提示同源）：
- ARPU₀   = 各群 M0 营收 / 各群规模（全渠道加权）
- 拟合     R_m = a·exp(-λm)，对有效（非右删失）格子做加权最小二乘，权重 = 群规模
- 历史 LTV = 观察期内群累计营收 / 群规模，跨群按会员数加权
- 预测 12 月 LTV = ARPU₀ × Σ_{m=0..11} a·exp(-λm)
- 回本周期 = CAC / 月均毛利贡献，月均毛利贡献 = 观察期人均月营收 × 毛利率
- 群或「群×渠道」样本 <30 标 low_sample，不进入渠道排行

数值逻辑全部在 `src/services/ltv_core.py`（无 Session 依赖，可用 pytest 直接测）。
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.config.settings import settings
from src.constants import error_codes as codes
from src.constants.channels import DEFAULT_CAC
from src.constants.enums import OrderType, ReportStatus
from src.models.cohort import Cohort
from src.models.ltv_report import LtvReport
from src.models.member_balance import MemberBalance
from src.models.member_order import MemberOrder
from src.models.retention_cell import RetentionCell
from src.services.ltv_core import CoreError, LtvParams, compute_ltv, sensitivity
from src.utils.errors import AppError


def _load(db: Session) -> list[Cohort]:
    cohorts = db.execute(select(Cohort).order_by(Cohort.month)).scalars().all()
    if not cohorts:
        raise AppError(codes.MATRIX_EMPTY)
    cells = db.execute(select(RetentionCell)).scalars().all()
    if not cells:
        raise AppError(codes.MATRIX_EMPTY, "留存矩阵还没有数据，请先重建矩阵")
    return list(cohorts)


def wallet_snapshot(db: Session) -> dict:
    """从会员余额台账汇总储值口径：待消费余额、净营收与对账差异。

    发布前必须确认 balanced=True；任何残差 > 1 分都视为明细与余额对不上。
    """
    sums = db.execute(
        select(
            func.coalesce(func.sum(MemberBalance.recharge_total), 0),
            func.coalesce(func.sum(MemberBalance.consume_total), 0),
            func.coalesce(func.sum(MemberBalance.refund_consume_total), 0),
            func.coalesce(func.sum(MemberBalance.refund_recharge_total), 0),
            func.coalesce(func.sum(MemberBalance.balance), 0),
            func.coalesce(func.sum(func.abs(MemberBalance.reconcile_diff)), 0),
            func.count(MemberBalance.id),
        )
    ).one()
    (
        recharge,
        consume,
        refund_consume,
        refund_recharge,
        balance,
        abs_diff,
        member_rows,
    ) = (float(v) for v in sums)
    reconcile_diff = round((recharge - consume - refund_recharge) - balance, 2)
    type_counts = dict(
        db.execute(
            select(MemberOrder.order_type, func.count()).group_by(MemberOrder.order_type)
        ).all()
    )
    balanced = abs(reconcile_diff) <= 0.01 and abs_diff <= 0.01 and balance >= 0
    return {
        "balanced": balanced,
        "identity": "充值 - 消费 - 退未核销充值 = 期末待消费余额",
        "recharge_total": round(recharge, 2),
        "consume_total": round(consume, 2),
        "refund_consume_total": round(refund_consume, 2),
        "refund_recharge_total": round(refund_recharge, 2),
        "net_revenue": round(consume - refund_consume, 2),
        "pending_balance": round(balance, 2),
        "reconcile_diff": reconcile_diff,
        "abs_member_diff": round(float(abs_diff), 2),
        "balance_members": int(member_rows),
        "order_counts": {
            OrderType.RECHARGE.value: int(type_counts.get(OrderType.RECHARGE.value, 0)),
            OrderType.CONSUME.value: int(type_counts.get(OrderType.CONSUME.value, 0)),
            OrderType.REFUND.value: int(type_counts.get(OrderType.REFUND.value, 0)),
        },
    }


def compute_results(
    db: Session,
    *,
    observed_months_override: int | None = None,
    lambda_min: float | None = None,
    lambda_max: float | None = None,
    margin_rate: float | None = None,
    channel_cac: dict[str, float] | None = None,
    snapshot_month: str | None = None,
    forecast_months: int | None = None,
) -> dict:
    snapshot = snapshot_month or settings.snapshot_month
    months = settings.matrix_months
    cohorts = _load(db)
    cells = db.execute(select(RetentionCell)).scalars().all()

    params = LtvParams(
        months=months,
        forecast_months=forecast_months or settings.forecast_months,
        margin_rate=settings.gross_margin_rate if margin_rate is None else float(margin_rate),
        lambda_min=lambda_min if lambda_min is not None else settings.fit_lambda_min,
        lambda_max=lambda_max if lambda_max is not None else settings.fit_lambda_max,
        lambda_step=settings.fit_lambda_step,
        low_sample_threshold=settings.low_sample_threshold,
        default_cac=dict(DEFAULT_CAC),
        channel_cac=channel_cac,
        observed_months_override=observed_months_override,
    )
    cohort_inputs = [
        {
            "key": str(cohort.id),
            "month": cohort.month,
            "size": int(cohort.size),
            "channel_mix": cohort.channel_mix or {},
        }
        for cohort in cohorts
    ]
    cell_inputs = [
        {
            "cohort_key": str(cell.cohort_id),
            "month_index": int(cell.month_index),
            "channel": cell.channel,
            "revenue": float(cell.revenue),
            "right_censored": bool(cell.right_censored),
            "sample_size": int(cell.sample_size),
        }
        for cell in cells
    ]

    try:
        results = compute_ltv(cohort_inputs, cell_inputs, snapshot, params)
    except CoreError as exc:
        raise AppError(exc.code, exc.message, exc.detail) from exc

    # 储值口径与对账结论随报告一起冻结：留存矩阵/LTV/渠道回本共用同一份结果
    results["wallet"] = wallet_snapshot(db)
    results["notes"]["wallet"] = (
        "充值只形成待消费余额；净营收 = 消费核销 - 退消费（退款发生月确认）；"
        "对账差异非 0 时禁止发布。"
    )
    return results


def create_report(db: Session, payload: dict, user) -> LtvReport:
    results = compute_results(
        db,
        observed_months_override=payload.get("observed_months"),
        lambda_min=payload.get("lambda_min"),
        lambda_max=payload.get("lambda_max"),
        margin_rate=payload.get("margin_rate"),
        channel_cac=payload.get("channel_cac"),
        snapshot_month=payload.get("snapshot_month"),
        forecast_months=payload.get("forecast_months"),
    )
    report = LtvReport(
        name=payload.get("name") or f"{results['snapshot_month']} 测算口径",
        status=ReportStatus.DRAFT.value,
        params=results["params"],
        results=results,
        note=payload.get("note") or "",
        created_by=user.id if user is not None else None,
    )
    db.add(report)
    db.flush()
    return report


def publish_report(db: Session, report_id, user) -> LtvReport:
    report = db.get(LtvReport, report_id)
    if report is None:
        raise AppError(codes.REPORT_NOT_FOUND)
    if report.status == ReportStatus.PUBLISHED.value:
        raise AppError(codes.REPORT_ALREADY_PUBLISHED)

    # 发布闸门：明细与待消费余额对不上时禁止发布（留存/LTV/渠道回本必须同源且对平）
    wallet = wallet_snapshot(db)
    if not wallet["balanced"]:
        raise AppError(
            codes.RECONCILE_MISMATCH,
            detail=(
                f"reconcile_diff={wallet['reconcile_diff']} "
                f"abs_member_diff={wallet['abs_member_diff']} "
                f"pending_balance={wallet['pending_balance']}"
            ),
        )
    # 报告快照里的净营收也要与当前台账一致，防止用过期矩阵发布
    results = report.results or {}
    snap_wallet = results.get("wallet") or {}
    if snap_wallet and abs(
        float(snap_wallet.get("net_revenue", 0.0)) - wallet["net_revenue"]
    ) > 0.01:
        raise AppError(
            codes.MATRIX_STALE,
            "报告口径与当前净营收台账不一致，请先重建矩阵并重新测算",
        )

    report.status = ReportStatus.PUBLISHED.value
    report.published_at = datetime.now(timezone.utc)
    db.flush()
    return report


def latest_published(db: Session) -> LtvReport | None:
    return db.execute(
        select(LtvReport)
        .where(LtvReport.status == ReportStatus.PUBLISHED.value)
        .order_by(LtvReport.published_at.desc())
    ).scalars().first()


__all__ = [
    "compute_results",
    "create_report",
    "latest_published",
    "publish_report",
    "sensitivity",
    "wallet_snapshot",
]
