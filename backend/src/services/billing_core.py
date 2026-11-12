"""储值制核算的纯函数核心：订单事实 → 会员余额台账、净营收格子与对账结果。

口径（与 README「算法口径」、前端口径提示同源）：

1. 订单三分类
   - recharge 储值充值：金额为正，只形成待消费余额（负债），永不确认营收；
   - consume 实际消费：金额为正，核销余额，按「消费发生月」确认营收；
   - refund 退款：金额恒为负，refund_order_no 指向原单，refund_type 区分
     退消费（consume）/ 退未核销充值（recharge）。

2. 净营收确认
   - 净营收只在消费、退款发生月确认；充值月不确认；
   - 跨月核销：消费按自身 order_month 落月，因此跨月使用余额天然归到真实消费月；
   - 退消费在「退款发生月」冲减净营收（负数金额），不回改原消费月；
   - 退未核销充值只减待消费余额，不碰营收，因此不会留下负营收。

3. 活跃口径
   - 某会员在某月活跃 = 当月有「未被全额退款的消费」；
   - 只有充值、只有退款的月份一律不算活跃；
   - 被全额退款的消费不计入活跃（部分退款仍计活跃）。

4. 对账恒等式（逐会员 + 全局）
   - balance = recharge_total - consume_total - refund_recharge_total
   - net_revenue = consume_total - refund_consume_total
   - reconcile_diff = (recharge - consume - refund_recharge) - balance，正常恒为 0；
   - 任意会员或全局差异超过容差（1 分）即判定对不上，禁止发布。

本模块只吃普通 dict / list，不做数据库读写；持久化由 retention_service 负责，
因此核算逻辑可以脱离 PostgreSQL 做单元测试。金额统一用 Decimal（分）计算，
输出给前端时再转 float（元）。
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from src.constants.enums import OrderType, RefundType
from src.utils.months import observed_months

# 对账容差：金额单位为元，1 分以内的舍入残差视为对平
RECONCILE_TOLERANCE = Decimal("0.01")
_CENTS = Decimal("0.01")


def _money(value: object) -> Decimal:
    """任何输入都归一成两位小数 Decimal（负数原样保留符号）。"""
    return Decimal(str(value or 0)).quantize(_CENTS)


@dataclass(frozen=True)
class OrderFact:
    """一条订单事实（与 MemberOrder 字段同构，纯数据）。"""

    order_no: str
    member_code: str
    cohort_month: str
    channel: str
    order_type: str
    order_month: str
    month_index: int
    amount: Decimal
    refund_order_no: str | None = None
    refund_type: str | None = None


@dataclass
class MemberLedger:
    """单会员的累计发生额与期末余额。"""

    member_code: str
    cohort_month: str
    recharge_total: Decimal = Decimal("0.00")
    consume_total: Decimal = Decimal("0.00")
    refund_consume_total: Decimal = Decimal("0.00")   # 正数存储
    refund_recharge_total: Decimal = Decimal("0.00")  # 正数存储

    @property
    def balance(self) -> Decimal:
        # 待消费余额 = 充值 - 消费 - 退未核销充值（退消费不动余额）
        return (
            self.recharge_total
            - self.consume_total
            - self.refund_recharge_total
        ).quantize(_CENTS)

    @property
    def net_revenue(self) -> Decimal:
        return (self.consume_total - self.refund_consume_total).quantize(_CENTS)

    def as_row(self) -> dict:
        balance = self.balance
        # 恒等式残差：左侧发生额推导余额 - 实际余额（同一公式，正常为 0，防御性保留）
        derived = (
            self.recharge_total - self.consume_total - self.refund_recharge_total
        ).quantize(_CENTS)
        return {
            "member_code": self.member_code,
            "cohort_month": self.cohort_month,
            "recharge_total": float(self.recharge_total),
            "consume_total": float(self.consume_total),
            "refund_consume_total": float(self.refund_consume_total),
            "refund_recharge_total": float(self.refund_recharge_total),
            "balance": float(balance),
            "reconcile_diff": float((derived - balance).quantize(_CENTS)),
        }


@dataclass
class WalletLedger:
    """整盘核算结果：会员台账、格子金额、月度滚动余额、对账结论。"""

    members: list[dict]
    # (cohort_month, month_index, channel) -> 金额/计数聚合
    cells: dict[tuple[str, int, str], dict]
    # 每个群逐月滚动的期末待消费余额：(cohort_month, month_index) -> balance
    cohort_month_balance: dict[tuple[str, int], Decimal]
    # (cohort_month, month_index) -> 当月有有效消费的去重会员
    active_members: dict[tuple[str, int], set[str]]
    totals: dict
    reconcile: dict
    member_diffs: list[dict] = field(default_factory=list)


def _normalize_orders(rows: Iterable[dict | OrderFact]) -> list[OrderFact]:
    facts: list[OrderFact] = []
    for row in rows:
        if isinstance(row, OrderFact):
            facts.append(
                OrderFact(
                    order_no=row.order_no,
                    member_code=row.member_code,
                    cohort_month=row.cohort_month,
                    channel=row.channel,
                    order_type=row.order_type,
                    order_month=row.order_month,
                    month_index=int(row.month_index),
                    amount=_money(row.amount),
                    refund_order_no=row.refund_order_no,
                    refund_type=row.refund_type,
                )
            )
        else:
            facts.append(
                OrderFact(
                    order_no=str(row["order_no"]),
                    member_code=str(row["member_code"]),
                    cohort_month=str(row["cohort_month"]),
                    channel=str(row["channel"]),
                    order_type=str(row["order_type"]),
                    order_month=str(row["order_month"]),
                    month_index=int(row["month_index"]),
                    amount=_money(row["amount"]),
                    refund_order_no=row.get("refund_order_no"),
                    refund_type=row.get("refund_type"),
                )
            )
    return facts


def _empty_cell() -> dict:
    return {
        "recharge": Decimal("0.00"),
        "consume": Decimal("0.00"),
        "refund_consume": Decimal("0.00"),   # 正数
        "refund_recharge": Decimal("0.00"),  # 正数
        "consume_orders": 0,
        "refund_orders": 0,
        "recharge_orders": 0,
        # 当月被全额退款的消费额（用于活跃判定），key=member -> 金额
        "consume_gross_by_member": defaultdict(lambda: Decimal("0.00")),
        "consume_refunded_by_member": defaultdict(lambda: Decimal("0.00")),
    }


def build_wallet_ledger(
    orders: Sequence[dict | OrderFact],
    cohort_months: Sequence[str],
    snapshot_month: str,
    months: int,
) -> WalletLedger:
    """纯函数：订单事实 → 会员余额台账 + 净营收格子 + 对账结论。

    - 只统计观察窗口内（month_index < 该群 observed_months）的订单；
    - 充值进 recharge 栏与余额；消费进 consume 栏与净营收；
    - 退款按 refund_type 分栏，退消费冲净营收，退充值减余额；
    - 活跃只看「未被全额退款的消费」。
    """
    facts = _normalize_orders(orders)
    observed = {
        month: observed_months(month, snapshot_month, months) for month in cohort_months
    }

    ledgers: dict[str, MemberLedger] = {}
    cells: dict[tuple[str, int, str], dict] = defaultdict(_empty_cell)
    # 群维度发生额（用于逐月滚动余额）
    cohort_flow: dict[tuple[str, int], dict[str, Decimal]] = defaultdict(
        lambda: {
            "recharge": Decimal("0.00"),
            "consume": Decimal("0.00"),
            "refund_recharge": Decimal("0.00"),
        }
    )
    active_members: dict[tuple[str, int], set[str]] = defaultdict(set)

    # 订单号 -> 订单事实，用于校验退款归属与超额
    order_index: dict[str, OrderFact] = {fact.order_no: fact for fact in facts}
    # 每个原单累计已退金额（正数），防止退款超过原单金额
    refunded_by_original: dict[str, Decimal] = defaultdict(lambda: Decimal("0.00"))

    for fact in facts:
        window = observed.get(fact.cohort_month)
        if window is None or fact.month_index < 0 or fact.month_index >= window:
            # 观察窗口外（含右删失月、开卡前充值的负月序落库前已折算）不计入本期
            continue

        ledger = ledgers.get(fact.member_code)
        if ledger is None:
            ledger = MemberLedger(
                member_code=fact.member_code, cohort_month=fact.cohort_month
            )
            ledgers[fact.member_code] = ledger

        cell = cells[(fact.cohort_month, fact.month_index, fact.channel)]
        flow = cohort_flow[(fact.cohort_month, fact.month_index)]
        amount = fact.amount  # 退款为负

        if fact.order_type == OrderType.RECHARGE:
            positive = abs(amount)
            ledger.recharge_total += positive
            cell["recharge"] += positive
            cell["recharge_orders"] += 1
            flow["recharge"] += positive

        elif fact.order_type == OrderType.CONSUME:
            positive = abs(amount)
            ledger.consume_total += positive
            cell["consume"] += positive
            cell["consume_orders"] += 1
            cell["consume_gross_by_member"][fact.member_code] += positive
            flow["consume"] += positive

        elif fact.order_type == OrderType.REFUND:
            refund_abs = abs(amount)
            original = order_index.get(fact.refund_order_no or "")
            if original is None:
                raise ValueError(
                    f"refund {fact.order_no!r} 指向不存在的原单 {fact.refund_order_no!r}"
                )
            if original.member_code != fact.member_code:
                raise ValueError(
                    f"refund {fact.order_no!r} 与原单 {fact.refund_order_no!r} 会员不一致"
                )
            already = refunded_by_original[fact.refund_order_no]  # type: ignore[index]
            if already + refund_abs > abs(original.amount) + RECONCILE_TOLERANCE:
                raise ValueError(
                    f"refund {fact.order_no!r} 累计退款超过原单 {fact.refund_order_no!r} 金额"
                )
            refunded_by_original[fact.refund_order_no] = already + refund_abs  # type: ignore[index]
            cell["refund_orders"] += 1
            if fact.refund_type == RefundType.CONSUME:
                ledger.refund_consume_total += refund_abs
                cell["refund_consume"] += refund_abs
                cell["consume_refunded_by_member"][fact.member_code] += refund_abs
                # 退消费冲净营收但不动余额、不动 flow 余额项
            elif fact.refund_type == RefundType.RECHARGE:
                ledger.refund_recharge_total += refund_abs
                cell["refund_recharge"] += refund_abs
                flow["refund_recharge"] += refund_abs
            else:  # pragma: no cover - 未知退款类型防御
                raise ValueError(f"unknown refund_type: {fact.refund_type!r}")
        else:  # pragma: no cover - 未知订单类型防御
            raise ValueError(f"unknown order_type: {fact.order_type!r}")

    # 活跃判定：当月消费毛额 > 被退消费额（即未被全额退款）的会员
    for (cohort_month, m_idx, _channel), cell in cells.items():
        key = (cohort_month, m_idx)
        gross = cell["consume_gross_by_member"]
        refunded = cell["consume_refunded_by_member"]
        for member_code, gross_amt in gross.items():
            if gross_amt > refunded.get(member_code, Decimal("0.00")):
                active_members[key].add(member_code)

    # 群逐月滚动待消费余额
    cohort_month_balance: dict[tuple[str, int], Decimal] = {}
    for cohort_month in cohort_months:
        running = Decimal("0.00")
        for m_idx in range(observed.get(cohort_month, 0)):
            flow = cohort_flow.get((cohort_month, m_idx))
            if flow is not None:
                running += flow["recharge"] - flow["consume"] - flow["refund_recharge"]
            cohort_month_balance[(cohort_month, m_idx)] = running.quantize(_CENTS)

    # 汇总 + 对账
    total_recharge = sum((lg.recharge_total for lg in ledgers.values()), Decimal("0.00"))
    total_consume = sum((lg.consume_total for lg in ledgers.values()), Decimal("0.00"))
    total_refund_consume = sum(
        (lg.refund_consume_total for lg in ledgers.values()), Decimal("0.00")
    )
    total_refund_recharge = sum(
        (lg.refund_recharge_total for lg in ledgers.values()), Decimal("0.00")
    )
    total_balance = sum((lg.balance for lg in ledgers.values()), Decimal("0.00"))
    net_revenue = total_consume - total_refund_consume

    # 全局恒等式：充值 - 消费 - 退充值 == 期末余额
    identity_left = (total_recharge - total_consume - total_refund_recharge).quantize(_CENTS)
    global_diff = (identity_left - total_balance).quantize(_CENTS)

    member_diffs = [
        lg.as_row() for lg in ledgers.values() if abs(Decimal(lg.as_row()["reconcile_diff"])) > 0
    ]
    # 负余额也是对不上（透支：消费超过可用余额）
    negative_balance = [
        lg.as_row() for lg in ledgers.values() if lg.balance < Decimal("0.00")
    ]

    # 跨渠道格子金额汇总核对：cell 维度净营收之和应等于会员维度净营收
    cell_net_revenue = Decimal("0.00")
    cell_recharge = Decimal("0.00")
    for cell in cells.values():
        cell_net_revenue += cell["consume"] - cell["refund_consume"]
        cell_recharge += cell["recharge"]
    cell_diff = (cell_net_revenue - net_revenue).quantize(_CENTS)
    recharge_diff = (cell_recharge - total_recharge).quantize(_CENTS)

    balanced = (
        abs(global_diff) <= RECONCILE_TOLERANCE
        and abs(cell_diff) <= RECONCILE_TOLERANCE
        and abs(recharge_diff) <= RECONCILE_TOLERANCE
        and not negative_balance
        and not member_diffs
    )

    totals = {
        "members": len(ledgers),
        "recharge_total": float(total_recharge.quantize(_CENTS)),
        "consume_total": float(total_consume.quantize(_CENTS)),
        "refund_consume_total": float(total_refund_consume.quantize(_CENTS)),
        "refund_recharge_total": float(total_refund_recharge.quantize(_CENTS)),
        "net_revenue": float(net_revenue.quantize(_CENTS)),
        "pending_balance": float(total_balance.quantize(_CENTS)),
        "recharge_orders": sum(c["recharge_orders"] for c in cells.values()),
        "consume_orders": sum(c["consume_orders"] for c in cells.values()),
        "refund_orders": sum(c["refund_orders"] for c in cells.values()),
    }
    reconcile = {
        "balanced": balanced,
        "tolerance": float(RECONCILE_TOLERANCE),
        "identity": "充值 - 消费 - 退未核销充值 = 期末待消费余额",
        "reconcile_diff": float(global_diff),
        "cell_revenue_diff": float(cell_diff),
        "cell_recharge_diff": float(recharge_diff),
        "negative_balance_members": len(negative_balance),
        "member_diff_count": len(member_diffs),
        "member_diffs": member_diffs[:50],
        "negative_balance_samples": negative_balance[:50],
    }

    return WalletLedger(
        members=[lg.as_row() for lg in sorted(ledgers.values(), key=lambda x: x.member_code)],
        cells=cells,
        cohort_month_balance=cohort_month_balance,
        active_members=active_members,
        totals=totals,
        reconcile=reconcile,
        member_diffs=member_diffs,
    )


def cell_amounts(cell: dict) -> dict:
    """把 defaultdict 格子转成可落库 / 可序列化的普通 dict（金额转 float 元）。"""
    return {
        "recharge_amount": float(cell["recharge"].quantize(_CENTS)),
        "revenue": float((cell["consume"] - cell["refund_consume"]).quantize(_CENTS)),
        "refund_consume_amount": float(cell["refund_consume"].quantize(_CENTS)),
        "refund_recharge_amount": float(cell["refund_recharge"].quantize(_CENTS)),
        "consume_orders": int(cell["consume_orders"]),
        "refund_orders": int(cell["refund_orders"]),
        "recharge_orders": int(cell["recharge_orders"]),
    }


__all__ = [
    "OrderFact",
    "MemberLedger",
    "WalletLedger",
    "RECONCILE_TOLERANCE",
    "build_wallet_ledger",
    "cell_amounts",
]
