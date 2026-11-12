"""储值制钱包核算专项测试（billing_core 纯函数）。

覆盖：订单三分类、净营收确认月份、跨月核销、退款分栏、活跃口径、对账恒等式、
负余额与超额退款防护。全部手算期望值。
"""
from __future__ import annotations

import pytest

from src.constants.enums import OrderType, RefundType
from src.services.billing_core import (
    RECONCILE_TOLERANCE,
    MemberLedger,
    build_wallet_ledger,
    cell_amounts,
)

SNAPSHOT = "2026-11"
COHORT = "2026-09"


def _o(order_no, member, m_idx, otype, amount, *, channel="private",
       refund_order_no=None, refund_type=None, month=None):
    return {
        "order_no": order_no,
        "member_code": member,
        "cohort_month": COHORT,
        "channel": channel,
        "order_type": otype,
        "order_month": month or ("2026-09" if m_idx == 0 else "2026-10"),
        "month_index": m_idx,
        "amount": amount,
        "refund_order_no": refund_order_no,
        "refund_type": refund_type,
    }


def test_member_ledger_identity() -> None:
    """单会员恒等式：余额 = 充值 - 消费 - 退未核销充值；净营收 = 消费 - 退消费。"""
    ledger = MemberLedger(member_code="m1", cohort_month=COHORT)
    ledger.recharge_total += 100
    ledger.consume_total += 70
    ledger.refund_consume_total += 10
    ledger.refund_recharge_total += 5
    assert ledger.balance == pytest.approx(25.0)       # 100 - 70 - 5
    assert ledger.net_revenue == pytest.approx(60.0)   # 70 - 10
    row = ledger.as_row()
    assert row["reconcile_diff"] == 0.0


def test_full_wallet_identity_and_monthly_rollforward() -> None:
    orders = [
        _o("R1", "m1", 0, OrderType.RECHARGE.value, 100),
        _o("C1", "m1", 0, OrderType.CONSUME.value, 30),
        _o("C2", "m1", 1, OrderType.CONSUME.value, 50, month="2026-10"),
        _o("RF1", "m1", 1, OrderType.REFUND.value, -10,
           refund_order_no="C1", refund_type=RefundType.CONSUME.value, month="2026-10"),
        _o("RR1", "m1", 1, OrderType.REFUND.value, -20,
           refund_order_no="R1", refund_type=RefundType.RECHARGE.value, month="2026-10"),
    ]
    wallet = build_wallet_ledger(orders, [COHORT], SNAPSHOT, 13)
    assert wallet.totals["recharge_total"] == 100.0
    assert wallet.totals["consume_total"] == 80.0
    assert wallet.totals["refund_consume_total"] == 10.0
    assert wallet.totals["refund_recharge_total"] == 20.0
    assert wallet.totals["net_revenue"] == 70.0
    assert wallet.totals["pending_balance"] == 0.0
    assert wallet.reconcile["balanced"] is True
    # 逐月滚动：M0 = 100-30=70；M1 = 70-50-20=0
    assert wallet.cohort_month_balance[(COHORT, 0)] == 70.0
    assert wallet.cohort_month_balance[(COHORT, 1)] == 0.0


def test_revenue_recognition_month() -> None:
    """净营收只在消费/退款发生月确认，充值月无营收。"""
    orders = [
        _o("R1", "m1", 0, OrderType.RECHARGE.value, 200),
        _o("C1", "m1", 1, OrderType.CONSUME.value, 60, month="2026-10"),
        _o("RF1", "m1", 2, OrderType.REFUND.value, -15,
           refund_order_no="C1", refund_type=RefundType.CONSUME.value, month="2026-11"),
    ]
    wallet = build_wallet_ledger(orders, [COHORT], SNAPSHOT, 13)
    m0 = cell_amounts(wallet.cells[(COHORT, 0, "private")])
    m1 = cell_amounts(wallet.cells[(COHORT, 1, "private")])
    m2 = cell_amounts(wallet.cells[(COHORT, 2, "private")])
    assert m0["revenue"] == 0.0 and m0["recharge_amount"] == 200.0
    assert m1["revenue"] == 60.0
    assert m2["revenue"] == -15.0 and m2["refund_consume_amount"] == 15.0
    # 全局净营收 60-15=45，余额 = 200-60=140（退消费不动余额）
    assert wallet.totals["net_revenue"] == 45.0
    assert wallet.totals["pending_balance"] == 140.0


def test_active_rules() -> None:
    # 只充值不活跃
    w1 = build_wallet_ledger(
        [_o("R1", "a", 0, OrderType.RECHARGE.value, 50)], [COHORT], SNAPSHOT, 13
    )
    assert not w1.active_members

    # 只退款（退充值）不活跃
    w2 = build_wallet_ledger(
        [
            _o("R1", "b", 0, OrderType.RECHARGE.value, 50),
            _o("RR1", "b", 0, OrderType.REFUND.value, -10,
               refund_order_no="R1", refund_type=RefundType.RECHARGE.value),
        ],
        [COHORT], SNAPSHOT, 13,
    )
    assert not w2.active_members

    # 全额退消费不活跃
    w3 = build_wallet_ledger(
        [
            _o("R1", "c", 0, OrderType.RECHARGE.value, 50),
            _o("C1", "c", 0, OrderType.CONSUME.value, 40),
            _o("RF1", "c", 0, OrderType.REFUND.value, -40,
               refund_order_no="C1", refund_type=RefundType.CONSUME.value),
        ],
        [COHORT], SNAPSHOT, 13,
    )
    assert (COHORT, 0) not in w3.active_members or "c" not in w3.active_members[(COHORT, 0)]

    # 部分退款仍活跃
    w4 = build_wallet_ledger(
        [
            _o("R1", "d", 0, OrderType.RECHARGE.value, 50),
            _o("C1", "d", 0, OrderType.CONSUME.value, 40),
            _o("RF1", "d", 0, OrderType.REFUND.value, -10,
               refund_order_no="C1", refund_type=RefundType.CONSUME.value),
        ],
        [COHORT], SNAPSHOT, 13,
    )
    assert "d" in w4.active_members[(COHORT, 0)]


def test_negative_balance_is_mismatch() -> None:
    """透支（消费超过可用余额）→ 对账不平。"""
    orders = [
        _o("R1", "m1", 0, OrderType.RECHARGE.value, 50),
        _o("C1", "m1", 0, OrderType.CONSUME.value, 80),
    ]
    wallet = build_wallet_ledger(orders, [COHORT], SNAPSHOT, 13)
    assert wallet.reconcile["balanced"] is False
    assert wallet.reconcile["negative_balance_members"] == 1


def test_over_refund_rejected() -> None:
    orders = [
        _o("C1", "m1", 0, OrderType.CONSUME.value, 30),
        _o("RF1", "m1", 0, OrderType.REFUND.value, -31,
           refund_order_no="C1", refund_type=RefundType.CONSUME.value),
    ]
    with pytest.raises(ValueError, match="超过原单"):
        build_wallet_ledger(orders, [COHORT], SNAPSHOT, 13)


def test_dangling_refund_rejected() -> None:
    orders = [
        _o("RF1", "m1", 0, OrderType.REFUND.value, -10,
           refund_order_no="NOPE", refund_type=RefundType.CONSUME.value),
    ]
    with pytest.raises(ValueError, match="不存在的原单"):
        build_wallet_ledger(orders, [COHORT], SNAPSHOT, 13)


def test_censored_orders_excluded() -> None:
    """右删失月（超出观察窗口）的订单不计入本期核算。"""
    orders = [
        _o("C1", "m1", 5, OrderType.CONSUME.value, 30, month="2027-02"),
    ]
    # 2026-10 群在快照 2026-11 只有 2 个观察月（M0/M1）
    wallet = build_wallet_ledger(orders, ["2026-10"], SNAPSHOT, 13)
    assert wallet.totals["consume_total"] == 0.0
    assert wallet.totals["members"] == 0


def test_multi_channel_revenue_splits_and_totals() -> None:
    orders = [
        _o("R1", "m1", 0, OrderType.RECHARGE.value, 100, channel="private"),
        _o("C1", "m1", 0, OrderType.CONSUME.value, 40, channel="private"),
        _o("C2", "m1", 0, OrderType.CONSUME.value, 30, channel="store"),
    ]
    wallet = build_wallet_ledger(orders, [COHORT], SNAPSHOT, 13)
    priv = cell_amounts(wallet.cells[(COHORT, 0, "private")])
    store = cell_amounts(wallet.cells[(COHORT, 0, "store")])
    assert priv["revenue"] == 40.0
    assert store["revenue"] == 30.0
    assert wallet.totals["net_revenue"] == 70.0
    assert wallet.reconcile["cell_revenue_diff"] == 0.0
    assert RECONCILE_TOLERANCE == pytest.approx(0.01)
