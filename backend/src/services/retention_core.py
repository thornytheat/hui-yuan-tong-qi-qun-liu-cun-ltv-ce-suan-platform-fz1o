"""算法 1 的纯函数核心：订单聚合 → 储值钱包台账 + 同期群留存格（无 ORM 依赖）。

口径（与 README / 前端「口径提示」同源）：
- 按首单月分群（cohort.month = 会员首个消费月），群规模 N；
- 订单三分类：recharge 充值只形成待消费余额；consume 消费确认营收；refund 退款冲减；
- 净营收在消费 / 退款发生月确认，跨月核销归到真实消费月（按订单自身 order_month）；
- Mn 活跃 = 第 n 个月有「未被全额退款的消费」的去重会员数 / N；
  仅充值、仅退款、被全额退款的消费都不算活跃；
- 快照月尚未走到的月序标 right_censored，金额与活跃归零，retention 为空，不参与拟合；
- 每个格子按渠道拆一份（channel=all 是矩阵汇总），待消费余额只在 all 格维护。

本模块只吃普通 dict / tuple，不做任何数据库读写；持久化由 retention_service 负责，
因此算法可以脱离 PostgreSQL 做单元测试。
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from src.constants.enums import COHORT_CHANNELS, Channel
from src.services.billing_core import (
    WalletLedger,
    build_wallet_ledger,
    cell_amounts,
)
from src.utils.months import observed_months


@dataclass(frozen=True)
class CohortInput:
    """一个同期群的输入事实（来自 cohorts 表）。"""

    key: str
    month: str
    size: int
    channel_mix: Mapping[str, object] = field(default_factory=dict)


@dataclass
class RetentionBuild:
    """重建结果：待落库的格子 + 汇总 + 各渠道真实会员数 + 钱包台账与对账。"""

    rows: list[dict]
    summary: dict
    channel_members: dict[tuple[str, str], int]
    wallet: WalletLedger


def build_retention_cells(
    cohorts: Sequence[CohortInput],
    orders: Iterable[Mapping],
    member_aggs: Iterable[Sequence],
    snapshot_month: str,
    months: int,
    low_sample_threshold: int = 30,
) -> RetentionBuild:
    """纯函数：订单事实 + 同期群规模 → 钱包台账与「群 × 月序 × 渠道」留存格。

    参数：
    - orders：订单事实行（dict，含 order_type/amount/refund_* 等 MemberOrder 字段）；
    - member_aggs：(cohort_month, channel, 去重会员数)，渠道行的分母来源；
    - 右删失格：活跃/金额/订单数归零，retention_rate 为 None；
    - 低样本：样本数 < low_sample_threshold 标 low_sample（不剔除，只标记）。
    """
    cohort_months = [str(c.month) for c in cohorts]
    wallet = build_wallet_ledger(orders, cohort_months, snapshot_month, months)

    channel_members: dict[tuple[str, str], int] = {
        (str(row[0]), str(row[1])): int(row[2]) for row in member_aggs
    }

    rows: list[dict] = []
    censored = observed = 0
    low_sample_cells = 0

    for cohort in cohorts:
        obs = observed_months(cohort.month, snapshot_month, months)
        size = int(cohort.size or 0)
        for m in range(months):
            is_censored = m >= obs
            if is_censored:
                censored += 1
            else:
                observed += 1

            channel_values = []
            for channel in COHORT_CHANNELS:
                cell = wallet.cells.get((cohort.month, m, channel))
                if cell is None:
                    amounts = {
                        "recharge_amount": 0.0,
                        "revenue": 0.0,
                        "refund_consume_amount": 0.0,
                        "refund_recharge_amount": 0.0,
                        "consume_orders": 0,
                        "refund_orders": 0,
                        "recharge_orders": 0,
                    }
                    active = 0
                else:
                    amounts = cell_amounts(cell)
                    active = len(
                        {
                            member
                            for member in cell["consume_gross_by_member"]
                            if cell["consume_gross_by_member"][member]
                            > cell["consume_refunded_by_member"].get(member, 0)
                        }
                    )
                channel_values.append((channel, active, amounts))
                sample = channel_members.get((cohort.month, channel), 0)
                low = sample < low_sample_threshold
                if low:
                    low_sample_cells += 1
                rate = None
                if not is_censored and sample > 0:
                    rate = round(active / sample, 6)
                if is_censored:
                    active = 0
                rows.append(
                    {
                        "cohort_key": cohort.key,
                        "cohort_month": cohort.month,
                        "month_index": m,
                        "channel": channel,
                        "active_members": int(active),
                        "sample_size": int(sample),
                        "retention_rate": rate,
                        "revenue": 0.0 if is_censored else round(amounts["revenue"], 2),
                        "recharge_amount": (
                            0.0 if is_censored else round(amounts["recharge_amount"], 2)
                        ),
                        "refund_consume_amount": (
                            0.0 if is_censored else round(amounts["refund_consume_amount"], 2)
                        ),
                        "refund_recharge_amount": (
                            0.0 if is_censored else round(amounts["refund_recharge_amount"], 2)
                        ),
                        "pending_balance": 0.0,
                        # 营收事件订单数 = 消费 + 退款（充值不是营收事件，单列）
                        "order_count": (
                            0
                            if is_censored
                            else amounts["consume_orders"] + amounts["refund_orders"]
                        ),
                        "recharge_order_count": (
                            0 if is_censored else amounts["recharge_orders"]
                        ),
                        "right_censored": is_censored,
                        "low_sample": low,
                    }
                )

            # all 汇总格：渠道加总，余额取群逐月滚动值
            all_active_set = wallet.active_members.get((cohort.month, m), set())
            all_active = len(all_active_set)
            all_amounts = {
                key: round(sum(v[2][key] for v in channel_values), 2)
                for key in (
                    "recharge_amount",
                    "revenue",
                    "refund_consume_amount",
                    "refund_recharge_amount",
                )
            }
            all_order_count = sum(
                v[2]["consume_orders"] + v[2]["refund_orders"] for v in channel_values
            )
            all_recharge_orders = sum(v[2]["recharge_orders"] for v in channel_values)
            pending = float(wallet.cohort_month_balance.get((cohort.month, m), 0.0))
            low = size < low_sample_threshold
            if low:
                low_sample_cells += 1
            rate = None
            if not is_censored and size > 0:
                rate = round(all_active / size, 6)
            if is_censored:
                all_active = 0
                all_amounts = {key: 0.0 for key in all_amounts}
                all_order_count = 0
                all_recharge_orders = 0
                pending = 0.0
            rows.append(
                {
                    "cohort_key": cohort.key,
                    "cohort_month": cohort.month,
                    "month_index": m,
                    "channel": Channel.ALL.value,
                    "active_members": int(all_active),
                    "sample_size": int(size),
                    "retention_rate": rate,
                    "revenue": all_amounts["revenue"],
                    "recharge_amount": all_amounts["recharge_amount"],
                    "refund_consume_amount": all_amounts["refund_consume_amount"],
                    "refund_recharge_amount": all_amounts["refund_recharge_amount"],
                    "pending_balance": round(pending, 2),
                    "order_count": int(all_order_count),
                    "recharge_order_count": int(all_recharge_orders),
                    "right_censored": is_censored,
                    "low_sample": low,
                }
            )

    summary = {
        "snapshot_month": snapshot_month,
        "cohorts": len(cohorts),
        "months": months,
        "cells": len(rows),
        "cells_observed": observed,
        "cells_censored": censored,
        "low_sample_cells": low_sample_cells,
        "wallet": wallet.totals,
        "reconcile": wallet.reconcile,
    }
    return RetentionBuild(
        rows=rows,
        summary=summary,
        channel_members=channel_members,
        wallet=wallet,
    )


__all__ = ["CohortInput", "RetentionBuild", "build_retention_cells"]
