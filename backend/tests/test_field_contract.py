"""字段契约测试：保证纯函数输出与 ORM 落库字段、前端响应字段严格对齐。

即使本地没有 SQLAlchemy，也用字段名集合锁定契约，防止改模型 / 改纯函数时漂移：
- retention_core 的格子 dict 必须覆盖 RetentionCell 落库所需字段；
- billing_core 的会员台账 dict 必须覆盖 MemberBalance 落库字段；
- 储值三栏金额在右删失格必须全部归零。
"""
from __future__ import annotations

from src.constants.enums import OrderType, RefundType
from src.services.billing_core import build_wallet_ledger
from src.services.retention_core import CohortInput, build_retention_cells

SNAPSHOT = "2026-11"

# 与 src/models/retention_cell.py 的 RetentionCell 落库参数对齐（除外键/时间戳）
RETENTION_CELL_FIELDS = {
    "month_index",
    "channel",
    "active_members",
    "sample_size",
    "retention_rate",
    "revenue",
    "recharge_amount",
    "refund_consume_amount",
    "refund_recharge_amount",
    "pending_balance",
    "order_count",
    "right_censored",
    "low_sample",
}

# 与 src/models/member_balance.py 的 MemberBalance 落库参数对齐（除主键/时间戳）
MEMBER_BALANCE_FIELDS = {
    "member_code",
    "cohort_month",
    "recharge_total",
    "consume_total",
    "refund_consume_total",
    "refund_recharge_total",
    "balance",
    "reconcile_diff",
}


def _orders():
    return [
        {
            "order_no": "R1", "member_code": "u1", "cohort_month": "2026-10",
            "channel": "private", "order_type": OrderType.RECHARGE.value,
            "order_month": "2026-10", "month_index": 0, "amount": 100.0,
            "refund_order_no": None, "refund_type": None,
        },
        {
            "order_no": "C1", "member_code": "u1", "cohort_month": "2026-10",
            "channel": "private", "order_type": OrderType.CONSUME.value,
            "order_month": "2026-10", "month_index": 0, "amount": 40.0,
            "refund_order_no": None, "refund_type": None,
        },
        {
            "order_no": "RF1", "member_code": "u1", "cohort_month": "2026-10",
            "channel": "private", "order_type": OrderType.REFUND.value,
            "order_month": "2026-11", "month_index": 1, "amount": -8.0,
            "refund_order_no": "C1", "refund_type": RefundType.CONSUME.value,
        },
    ]


def test_retention_cell_row_contract() -> None:
    build = build_retention_cells(
        [CohortInput(key="c1", month="2026-10", size=1)],
        _orders(),
        [("2026-10", "private", 1)],
        SNAPSHOT,
        3,
        30,
    )
    assert build.rows, "应至少产出格子"
    for row in build.rows:
        assert RETENTION_CELL_FIELDS.issubset(row.keys()), (
            f"缺少落库字段: {RETENTION_CELL_FIELDS - set(row.keys())}"
        )


def test_member_balance_row_contract() -> None:
    wallet = build_wallet_ledger(_orders(), ["2026-10"], SNAPSHOT, 3)
    assert len(wallet.members) == 1
    for row in wallet.members:
        assert MEMBER_BALANCE_FIELDS.issubset(row.keys()), (
            f"缺少台账字段: {MEMBER_BALANCE_FIELDS - set(row.keys())}"
        )


def test_censored_cells_zero_out_all_money_fields() -> None:
    build = build_retention_cells(
        [CohortInput(key="c1", month="2026-10", size=1)],
        _orders(),
        [("2026-10", "private", 1)],
        SNAPSHOT,
        3,
        30,
    )
    # 2026-10 群在快照 2026-11 只有 M0/M1 可观察，M2 右删失
    censored = [r for r in build.rows if r["right_censored"]]
    assert censored, "应存在右删失格"
    for row in censored:
        assert row["active_members"] == 0
        assert row["revenue"] == 0.0
        assert row["recharge_amount"] == 0.0
        assert row["refund_consume_amount"] == 0.0
        assert row["refund_recharge_amount"] == 0.0
        assert row["pending_balance"] == 0.0
        assert row["order_count"] == 0
        assert row["retention_rate"] is None


def test_pending_balance_only_on_all_channel() -> None:
    build = build_retention_cells(
        [CohortInput(key="c1", month="2026-10", size=1)],
        _orders(),
        [("2026-10", "private", 1)],
        SNAPSHOT,
        2,
        30,
    )
    for row in build.rows:
        if row["channel"] != "all":
            assert row["pending_balance"] == 0.0
    all_rows = [r for r in build.rows if r["channel"] == "all"]
    assert any(r["pending_balance"] > 0 for r in all_rows)
