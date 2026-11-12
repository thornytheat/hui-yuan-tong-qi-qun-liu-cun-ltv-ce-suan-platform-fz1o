"""算法 A（同期群留存矩阵 + 储值钱包，右删失）单元测试。

只测纯函数 `retention_core.build_retention_cells`（内部调用 billing_core）：
输入订单事实（充值/消费/退款）与群规模，输出钱包台账与「群 × 月序 × 渠道」留存格。
手算期望值 + 真实 seed 切片。
"""
from __future__ import annotations

import pytest

from scripts import fixtures
from src.constants.enums import COHORT_CHANNELS, OrderType, RefundType
from src.services.retention_core import CohortInput, build_retention_cells

SNAPSHOT = "2026-11"


def _order(
    order_no,
    member,
    month,
    m_idx,
    channel,
    otype,
    amount,
    *,
    refund_order_no=None,
    refund_type=None,
    cohort_month="2026-10",
):
    return {
        "order_no": order_no,
        "member_code": member,
        "cohort_month": cohort_month,
        "channel": channel,
        "order_type": otype,
        "order_month": month,
        "month_index": m_idx,
        "amount": amount,
        "refund_order_no": refund_order_no,
        "refund_type": refund_type,
    }


# ---------------------------------------------------------------- 正常路径


def test_recharge_is_liability_not_revenue() -> None:
    """充值只形成待消费余额，不确认营收、不算活跃。"""
    orders = [
        _order("R1", "u1", "2026-10", 0, "private", OrderType.RECHARGE.value, 100.0),
        _order("R2", "u2", "2026-10", 0, "store", OrderType.RECHARGE.value, 80.0),
    ]
    build = build_retention_cells(
        [CohortInput(key="c1", month="2026-10", size=2)],
        orders,
        [("2026-10", "private", 1), ("2026-10", "store", 1)],
        SNAPSHOT,
        2,
        5,
    )
    by_key = {(r["month_index"], r["channel"]): r for r in build.rows}
    all_m0 = by_key[(0, "all")]
    assert all_m0["active_members"] == 0            # 只充值不活跃
    assert all_m0["revenue"] == 0.0                 # 充值不是营收
    assert all_m0["recharge_amount"] == pytest.approx(180.0)
    assert all_m0["pending_balance"] == pytest.approx(180.0)
    assert build.wallet.totals["net_revenue"] == 0.0
    assert build.wallet.totals["pending_balance"] == pytest.approx(180.0)
    assert build.summary["reconcile"]["balanced"] is True


def test_consume_recognizes_revenue_and_active() -> None:
    """消费核销余额，消费发生月确认净营收并计活跃。"""
    orders = [
        _order("R1", "u1", "2026-10", 0, "private", OrderType.RECHARGE.value, 100.0),
        _order("C1", "u1", "2026-10", 0, "private", OrderType.CONSUME.value, 30.0),
    ]
    build = build_retention_cells(
        [CohortInput(key="c1", month="2026-10", size=1)],
        orders,
        [("2026-10", "private", 1)],
        SNAPSHOT,
        2,
        5,
    )
    by_key = {(r["month_index"], r["channel"]): r for r in build.rows}
    all_m0 = by_key[(0, "all")]
    assert all_m0["active_members"] == 1
    assert all_m0["revenue"] == pytest.approx(30.0)
    assert all_m0["pending_balance"] == pytest.approx(70.0)   # 100 - 30


def test_cross_month_writeoff_lands_on_consume_month() -> None:
    """跨月核销：M0 充值，M1 才消费，营收落在真实消费月 M1。"""
    orders = [
        _order("R1", "u1", "2026-10", 0, "private", OrderType.RECHARGE.value, 100.0),
        _order("C1", "u1", "2026-11", 1, "private", OrderType.CONSUME.value, 40.0),
    ]
    build = build_retention_cells(
        [CohortInput(key="c1", month="2026-10", size=1)],
        orders,
        [("2026-10", "private", 1)],
        SNAPSHOT,
        2,
        5,
    )
    by_key = {(r["month_index"], r["channel"]): r for r in build.rows}
    assert by_key[(0, "all")]["revenue"] == 0.0
    assert by_key[(0, "all")]["pending_balance"] == pytest.approx(100.0)
    assert by_key[(1, "all")]["revenue"] == pytest.approx(40.0)
    assert by_key[(1, "all")]["active_members"] == 1
    assert by_key[(1, "all")]["pending_balance"] == pytest.approx(60.0)


def test_consume_refund_books_in_refund_month() -> None:
    """退消费：跨月退款在退款月冲净营收（负数），原消费月不改写。"""
    orders = [
        _order("R1", "u1", "2026-10", 0, "private", OrderType.RECHARGE.value, 100.0),
        _order("C1", "u1", "2026-10", 0, "private", OrderType.CONSUME.value, 50.0),
        _order("RF1", "u1", "2026-11", 1, "private", OrderType.REFUND.value, -10.0,
               refund_order_no="C1", refund_type=RefundType.CONSUME.value),
    ]
    build = build_retention_cells(
        [CohortInput(key="c1", month="2026-10", size=1)],
        orders,
        [("2026-10", "private", 1)],
        SNAPSHOT,
        2,
        5,
    )
    by_key = {(r["month_index"], r["channel"]): r for r in build.rows}
    # 原消费月 M0 营收保持 50，不回改
    assert by_key[(0, "all")]["revenue"] == pytest.approx(50.0)
    # 退款月 M1 净营收 = 0 - 10 = -10（允许负的月度净营收，但余额不变）
    assert by_key[(1, "all")]["revenue"] == pytest.approx(-10.0)
    assert by_key[(1, "all")]["refund_consume_amount"] == pytest.approx(10.0)
    # 退消费不动余额：余额仍是 100 - 50 = 50
    assert by_key[(1, "all")]["pending_balance"] == pytest.approx(50.0)
    # 全局净营收 = 50 - 10 = 40
    assert build.wallet.totals["net_revenue"] == pytest.approx(40.0)
    assert build.summary["reconcile"]["balanced"] is True


def test_recharge_refund_only_reduces_balance() -> None:
    """退未核销充值：只减待消费余额，净营收不受影响、不产生负营收。"""
    orders = [
        _order("R1", "u1", "2026-10", 0, "private", OrderType.RECHARGE.value, 100.0),
        _order("C1", "u1", "2026-10", 0, "private", OrderType.CONSUME.value, 30.0),
        _order("RR1", "u1", "2026-11", 1, "private", OrderType.REFUND.value, -20.0,
               refund_order_no="R1", refund_type=RefundType.RECHARGE.value),
    ]
    build = build_retention_cells(
        [CohortInput(key="c1", month="2026-10", size=1)],
        orders,
        [("2026-10", "private", 1)],
        SNAPSHOT,
        2,
        5,
    )
    by_key = {(r["month_index"], r["channel"]): r for r in build.rows}
    assert by_key[(1, "all")]["revenue"] == 0.0          # 不碰营收
    assert by_key[(1, "all")]["refund_recharge_amount"] == pytest.approx(20.0)
    # 余额 = 100 - 30 - 20 = 50
    assert by_key[(1, "all")]["pending_balance"] == pytest.approx(50.0)
    assert build.wallet.totals["net_revenue"] == pytest.approx(30.0)


def test_fully_refunded_consume_not_active() -> None:
    """当月消费被全额退款：会员该月不算活跃，净营收为 0。"""
    orders = [
        _order("R1", "u1", "2026-10", 0, "private", OrderType.RECHARGE.value, 100.0),
        _order("C1", "u1", "2026-10", 0, "private", OrderType.CONSUME.value, 40.0),
        _order("RF1", "u1", "2026-10", 0, "private", OrderType.REFUND.value, -40.0,
               refund_order_no="C1", refund_type=RefundType.CONSUME.value),
    ]
    build = build_retention_cells(
        [CohortInput(key="c1", month="2026-10", size=1)],
        orders,
        [("2026-10", "private", 1)],
        SNAPSHOT,
        1,
        5,
    )
    all_m0 = next(r for r in build.rows if r["channel"] == "all")
    assert all_m0["active_members"] == 0
    assert all_m0["revenue"] == pytest.approx(0.0)


def test_partial_refund_keeps_active() -> None:
    """部分退款（未全额）会员仍算活跃。"""
    orders = [
        _order("R1", "u1", "2026-10", 0, "private", OrderType.RECHARGE.value, 100.0),
        _order("C1", "u1", "2026-10", 0, "private", OrderType.CONSUME.value, 40.0),
        _order("RF1", "u1", "2026-10", 0, "private", OrderType.REFUND.value, -15.0,
               refund_order_no="C1", refund_type=RefundType.CONSUME.value),
    ]
    build = build_retention_cells(
        [CohortInput(key="c1", month="2026-10", size=1)],
        orders,
        [("2026-10", "private", 1)],
        SNAPSHOT,
        1,
        5,
    )
    all_m0 = next(r for r in build.rows if r["channel"] == "all")
    assert all_m0["active_members"] == 1
    assert all_m0["revenue"] == pytest.approx(25.0)


def test_censoring_windows_per_cohort() -> None:
    """不同首单月在同一快照下的观察窗口：13 / 7 / 2，删失 0 / 6 / 11。"""
    build = build_retention_cells(
        [
            CohortInput(key="a", month="2025-11", size=100),
            CohortInput(key="b", month="2026-05", size=100),
            CohortInput(key="c", month="2026-10", size=100),
        ],
        [],
        [],
        SNAPSHOT,
        13,
        30,
    )
    assert build.summary["cells_observed"] == 13 + 7 + 2
    assert build.summary["cells_censored"] == 0 + 6 + 11
    assert len(build.rows) == 3 * 13 * 5
    by_key = {(r["cohort_key"], r["month_index"], r["channel"]): r for r in build.rows}
    assert by_key[("b", 6, "all")]["right_censored"] is False
    assert by_key[("b", 7, "all")]["right_censored"] is True
    assert by_key[("b", 7, "all")]["active_members"] == 0
    assert by_key[("b", 7, "all")]["pending_balance"] == 0.0


def test_real_seed_reconciles_and_keeps_retention() -> None:
    """真实 seed：对账全平，且 2025-11 群 M1=122、M6=60 的设计留存不被退款改变。"""
    cells_plan, _, rows = fixtures.demo_plan()
    splits = fixtures.monthly_channel_split()
    member_aggs = [
        (fixtures.COHORT_MONTHS[i], channel, splits[i][channel])
        for i in range(len(fixtures.COHORT_MONTHS))
        for channel in COHORT_CHANNELS
    ]
    build = build_retention_cells(
        [
            CohortInput(key=f"c{i}", month=month, size=fixtures.COHORT_SIZES[i])
            for i, month in enumerate(fixtures.COHORT_MONTHS)
        ],
        rows,
        member_aggs,
        fixtures.SNAPSHOT_MONTH,
        len(fixtures.BASE_RETENTION),
        30,
    )
    assert build.summary["reconcile"]["balanced"] is True
    all_cells = {
        r["month_index"]: r
        for r in build.rows
        if r["cohort_month"] == "2025-11" and r["channel"] == "all"
    }
    assert all_cells[0]["active_members"] == 280
    assert all_cells[1]["active_members"] == 122
    assert all_cells[6]["active_members"] == 60
    # 净营收严格小于消费总额（有退消费），但大于 0
    wallet = build.wallet.totals
    assert 0 < wallet["net_revenue"] < wallet["consume_total"]
    assert wallet["pending_balance"] > 0


# ---------------------------------------------------------------- 边界 / 降级


def test_empty_cohorts_returns_empty_matrix() -> None:
    build = build_retention_cells([], [], [], SNAPSHOT, 13, 30)
    assert build.rows == []
    assert build.channel_members == {}
    assert build.summary["cells"] == 0
    assert build.wallet.totals["members"] == 0


def test_no_orders_degrades_to_zero_rates() -> None:
    """有群无订单：all 行 rate=0.0（分母存在），渠道行 rate=None（无分母）。"""
    build = build_retention_cells(
        [CohortInput(key="c1", month="2026-10", size=100)],
        [],
        [],
        SNAPSHOT,
        2,
        30,
    )
    by_key = {(r["month_index"], r["channel"]): r for r in build.rows}
    for m in (0, 1):
        assert by_key[(m, "all")]["active_members"] == 0
        assert by_key[(m, "all")]["retention_rate"] == pytest.approx(0.0)
        assert by_key[(m, "all")]["sample_size"] == 100
        for channel in COHORT_CHANNELS:
            assert by_key[(m, channel)]["retention_rate"] is None
            assert by_key[(m, channel)]["low_sample"] is True


def test_low_sample_marks_without_dropping_rows() -> None:
    """样本 <30 只打标、不删行；渠道分母取「群×渠道」真实会员数。"""
    orders = [
        _order("R1", "u1", "2026-10", 0, "private", OrderType.RECHARGE.value, 50.0),
        _order("C1", "u1", "2026-10", 0, "private", OrderType.CONSUME.value, 20.0),
    ]
    build = build_retention_cells(
        [CohortInput(key="c1", month="2026-10", size=20)],
        orders,
        [("2026-10", "private", 100)],
        SNAPSHOT,
        2,
        30,
    )
    by_key = {(r["month_index"], r["channel"]): r for r in build.rows}
    assert by_key[(0, "all")]["low_sample"] is True       # 群规模 20 < 30
    assert by_key[(0, "private")]["low_sample"] is False  # 渠道样本 100
    assert by_key[(0, "private")]["sample_size"] == 100
    assert by_key[(0, "private")]["retention_rate"] == pytest.approx(0.01)


def test_over_refund_raises() -> None:
    """退款累计超过原单金额必须显式报错，不允许悄悄对不平。"""
    orders = [
        _order("C1", "u1", "2026-10", 0, "private", OrderType.CONSUME.value, 30.0),
        _order("RF1", "u1", "2026-10", 0, "private", OrderType.REFUND.value, -31.0,
               refund_order_no="C1", refund_type=RefundType.CONSUME.value),
    ]
    with pytest.raises(ValueError, match="超过原单"):
        build_retention_cells(
            [CohortInput(key="c1", month="2026-10", size=1)],
            orders,
            [("2026-10", "private", 1)],
            SNAPSHOT,
            1,
            30,
        )
