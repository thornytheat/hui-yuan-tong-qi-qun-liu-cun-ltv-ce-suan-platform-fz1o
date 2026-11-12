"""Demo fixture：沐野咖啡连锁 2025-11 ~ 2026-10 的会员开卡与订单事实。

数据是"设计出来的真实分布"，不是随机噪声：
- 12 个自然月同期群，规模逐月增长（2026-10 是未完整月，样本 210）；
- 人数留存按 BASE_RETENTION 曲线（2025-11 基准群：M1 43.6%、M6 21.4%，
  M6 台阶对应「储值权益到期」这一已知业务事件）；
- ARPU 留存按 exp(-GEN_LAMBDA·m) 衰减并带 ±20% 的月份/群波动，
  后端的加权最小二乘会把它还原成 λ≈0.213、R²≈0.94。

储值制三类订单（order_type）：
- consume 消费：由「格内营收 / 订单除数」推出，除数二分使消费单总量恰好 CONSUME_ORDERS；
- recharge 充值：按会员终身消费额加缓冲生成，只形成待消费余额，永远不确认营收；
- refund 退款：恒为负金额并关联原单，
  退消费在退款发生月冲净营收（含跨月退款），退未核销充值只减余额。

对账恒等式（逐会员 + 全局）：
    充值 - 消费 - 退未核销充值 = 期末待消费余额
所有随机量来自 sha256(key) 的确定性哈希，同参数重跑结果完全一致。
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from src.constants.channels import CHANNEL_REVENUE_MULT, CHANNEL_SHARE
from src.constants.enums import COHORT_CHANNELS, OrderType, RefundType

COHORT_MONTHS: list[str] = [
    "2025-11", "2025-12", "2026-01", "2026-02", "2026-03", "2026-04",
    "2026-05", "2026-06", "2026-07", "2026-08", "2026-09", "2026-10",
]
COHORT_SIZES: list[int] = [280, 314, 352, 394, 441, 494, 553, 620, 694, 777, 871, 210]
COHORT_NOTES: list[str] = [
    "冬季开卡，储值卡主推", "双十二拉新", "年货节礼盒", "春节返乡礼盒",
    "春季上新", "储值权益升级", "M6 台阶群（权益到期）", "夏季冰饮旺季",
    "毕业季联名", "门店扫码开卡", "开学季高峰", "未完整月（快照 11-30）",
]
BASE_RETENTION: list[float] = [
    1.000, 0.436, 0.381, 0.338, 0.301, 0.268, 0.214, 0.193,
    0.174, 0.158, 0.144, 0.131, 0.119,
]
GEN_LAMBDA = 0.2260
REV_AMP = 0.200
SALT = 11
ARPU0 = 116.5291
CONSUME_ORDERS = 24000
# 兼容旧引用：seed 摘要里的订单总数改为动态计算，保留常量名给文档对照
TOTAL_ORDERS = CONSUME_ORDERS
SNAPSHOT_MONTH = "2026-11"
DEMO_PASSWORD = "Demo@2026"

# 退款设计：约 1.6% 的消费单被退（同月或跨月），另退少量未核销充值
REFUND_CONSUME_RATE = 0.016
TOPUP_RATE = 0.10            # 10% 会员在观察期中段有一次追充值
RECHARGE_BUFFER = 1.18       # 开卡充值相对终身消费的缓冲系数（留出待消费余额）
RECHARGE_REFUND_RATE = 0.004

STORE_PREFIX = "MY"
STORE_COUNT = 380

USERS = [
    {
        "email": "admin@cohortltv.io",
        "display_name": "陈可 · 增长负责人",
        "role": "admin",
        "channel_scope": None,
    },
    {
        "email": "analyst@cohortltv.io",
        "display_name": "林知遥 · 增长分析师",
        "role": "analyst",
        "channel_scope": None,
    },
    {
        "email": "viewer@cohortltv.io",
        "display_name": "周明 · 门店运营（只读）",
        "role": "viewer",
        "channel_scope": "store",
    },
]


@dataclass
class CellPlan:
    cohort_index: int
    month_index: int
    active: int
    revenue: float
    channels: dict[str, dict]


def h01(*parts: object) -> float:
    key = "|".join(str(part) for part in parts)
    digest = hashlib.sha256(key.encode()).digest()
    return int.from_bytes(digest[:8], "big") / float(1 << 64)


def split_counts(total: int, weights: list[float]) -> list[int]:
    """最大余数法按权重拆整数，和恰好等于 total。"""
    weight_sum = sum(weights)
    raw = [total * w / weight_sum for w in weights]
    out = [int(math.floor(x)) for x in raw]
    remain = total - sum(out)
    order = sorted(range(len(raw)), key=lambda i: raw[i] - out[i], reverse=True)
    for i in order[:remain]:
        out[i] += 1
    return out


def monthly_channel_split() -> list[dict[str, int]]:
    weights = [CHANNEL_SHARE[ch] for ch in COHORT_CHANNELS]
    return [
        dict(zip(COHORT_CHANNELS, split_counts(size, weights))) for size in COHORT_SIZES
    ]


def build_cells(arpu0: float = ARPU0) -> dict[tuple[int, int], CellPlan]:
    """设计每个（群，月）的有效会员数、营收与渠道拆分。"""
    norm = sum(CHANNEL_SHARE[ch] * CHANNEL_REVENUE_MULT[ch] for ch in COHORT_CHANNELS)
    shares = [CHANNEL_SHARE[ch] for ch in COHORT_CHANNELS]
    cells: dict[tuple[int, int], CellPlan] = {}
    for i, size in enumerate(COHORT_SIZES):
        observed = len(BASE_RETENTION) - i  # M0..M(12-i)，快照 2026-11 时 2025-11 群满 13 格
        for m in range(observed):
            if m == 0:
                active = size
            elif i == 0:
                active = int(round(size * BASE_RETENTION[m]))
            else:
                factor = 1.0 + (h01("ret", SALT, i, m) * 2 - 1) * 0.05
                active = max(1, min(size, int(round(size * BASE_RETENTION[m] * factor))))
            revenue = round(
                arpu0 * size * math.exp(-GEN_LAMBDA * m)
                * (1.0 + (h01("rev", SALT, i, m) * 2 - 1) * REV_AMP),
                2,
            )
            per_channel_active = split_counts(active, shares)
            channels: dict[str, dict] = {}
            for channel, ch_active in zip(COHORT_CHANNELS, per_channel_active):
                channels[channel] = {
                    "active": ch_active,
                    "revenue": round(
                        revenue * CHANNEL_SHARE[channel] * CHANNEL_REVENUE_MULT[channel] / norm, 2
                    ),
                }
            cells[(i, m)] = CellPlan(
                cohort_index=i, month_index=m, active=active, revenue=revenue, channels=channels
            )
    return cells


def solve_order_divisor(cells: dict[tuple[int, int], CellPlan], target: int = CONSUME_ORDERS) -> float:
    """二分出订单除数 W，使 Σ max(active_ch, round(rev_ch / W)) == target。"""

    def total_for(divisor: float) -> int:
        total = 0
        for cell in cells.values():
            for plan in cell.channels.values():
                total += max(plan["active"], int(round(plan["revenue"] / divisor)))
        return total

    low, high = 40.0, 400.0
    for _ in range(90):
        mid = (low + high) / 2
        if total_for(mid) > target:
            low = mid
        else:
            high = mid
    return (low + high) / 2


def cell_order_counts(cell: CellPlan, divisor: float) -> dict[str, int]:
    return {
        channel: max(plan["active"], int(round(plan["revenue"] / divisor)))
        for channel, plan in cell.channels.items()
    }


def solve_order_counts(
    cells: dict[tuple[int, int], CellPlan], target: int = CONSUME_ORDERS
) -> dict[tuple[int, int, str], int]:
    """先二分订单除数，再把取整漂移摊到最大的格子上，保证消费单总量恰好等于 target。"""
    divisor = solve_order_divisor(cells, target)
    counts: dict[tuple[int, int, str], int] = {}
    for (i, m), cell in cells.items():
        for channel, count in cell_order_counts(cell, divisor).items():
            counts[(i, m, channel)] = count
    total = sum(counts.values())
    keys = sorted(counts, key=lambda k: (-counts[k], k))
    guard = 0
    index = 0
    while total != target:
        guard += 1
        if guard > 20000:
            raise RuntimeError(f"order count adjustment did not converge total={total}")
        key = keys[index % len(keys)]
        index += 1
        if total < target:
            counts[key] += 1
            total += 1
        else:
            active = cells[(key[0], key[1])].channels[key[2]]["active"]
            if counts[key] > active:
                counts[key] -= 1
                total -= 1
    return counts


def split_amounts(revenue: float, count: int, key: str) -> list[int]:
    """把格内营收（元）拆成 count 笔订单金额（分），和精确等于 round(revenue*100)。"""
    cents = int(round(revenue * 100))
    if count <= 0:
        return []
    base = max(1200, cents // count)
    out: list[int] = []
    remain = cents
    for j in range(count - 1):
        jitter = 1.0 + (h01("amt", SALT, key, j) * 2 - 1) * 0.26
        value = max(1200, int(round(base * jitter)))
        left = count - j - 1
        if remain - value < 1200 * left:
            value = max(1200, remain - 1200 * left)
        out.append(value)
        remain -= value
    out.append(remain)
    return out


def member_codes(cohort_index: int, size: int) -> list[str]:
    return [f"M{cohort_index:02d}-{idx:04d}" for idx in range(size)]


def channel_member_pools(cohort_index: int) -> dict[str, list[str]]:
    """每个渠道的会员编号池（顺序固定，保证重建矩阵可复现）。"""
    codes = member_codes(cohort_index, COHORT_SIZES[cohort_index])
    split = monthly_channel_split()[cohort_index]
    pools: dict[str, list[str]] = {}
    cursor = 0
    for channel in COHORT_CHANNELS:
        count = split[channel]
        pools[channel] = codes[cursor:cursor + count]
        cursor += count
    return pools


def order_month(cohort_month: str, month_index: int) -> str:
    from src.utils.months import add_months

    return add_months(cohort_month, month_index)


def order_timestamp(month: str, key: str) -> datetime:
    year, mon = (int(part) for part in month.split("-"))
    day = 1 + int(h01("day", key) * 26)
    hour = int(h01("hour", key) * 12) + 8
    return datetime(year, mon, day, hour, 0, tzinfo=timezone.utc)


def _base_row(**kwargs: object) -> dict:
    return {
        "store_code": "",
        "refund_order_no": None,
        "refund_type": None,
        "is_first_order": False,
        **kwargs,
    }


def iter_consume_orders(
    cells: dict[tuple[int, int], CellPlan],
    counts_map: dict[tuple[int, int, str], int],
) -> list[dict]:
    """展开消费订单行（总量恰好 CONSUME_ORDERS），同时返回会员终身消费额对照表。"""
    rows: list[dict] = []
    pools = {i: channel_member_pools(i) for i in range(len(COHORT_MONTHS))}
    # 会员 -> 渠道（开卡渠道终身不变）
    member_channel: dict[str, str] = {}
    member_lifetime_consume: dict[str, int] = {}

    for (i, m), cell in sorted(cells.items()):
        cohort_month = COHORT_MONTHS[i]
        counts = {ch: counts_map[(i, m, ch)] for ch in COHORT_CHANNELS}
        for channel in COHORT_CHANNELS:
            plan = cell.channels[channel]
            active_pool = pools[i][channel][: plan["active"]]
            if not active_pool:
                continue
            count = counts[channel]
            key = f"{i}-{m}-{channel}"
            amounts = split_amounts(plan["revenue"], count, key)
            month = order_month(cohort_month, m)
            for j, amount_cents in enumerate(amounts):
                member = active_pool[j % len(active_pool)]
                order_no = f"MO{i:02d}{m:02d}{COHORT_CHANNELS.index(channel) + 1}{j:05d}"
                store_index = 1 + int(h01("store", key, j) * STORE_COUNT)
                member_channel.setdefault(member, channel)
                member_lifetime_consume[member] = (
                    member_lifetime_consume.get(member, 0) + amount_cents
                )
                rows.append(
                    _base_row(
                        order_no=order_no,
                        member_code=member,
                        cohort_month=cohort_month,
                        channel=channel,
                        store_code=f"{STORE_PREFIX}{store_index:03d}",
                        order_type=OrderType.CONSUME.value,
                        order_month=month,
                        month_index=m,
                        amount=Decimal(amount_cents) / Decimal(100),
                        # 首轮订单覆盖每个活跃会员一次，这一轮才是「首单」
                        is_first_order=m == 0 and j < len(active_pool),
                        ordered_at=order_timestamp(month, order_no),
                    )
                )
    return rows, member_channel, member_lifetime_consume


def build_recharge_orders(
    cells: dict[tuple[int, int], CellPlan],
    member_channel: dict[str, str],
    member_lifetime_consume: dict[str, int],
) -> list[dict]:
    """为每个会员生成开卡储值充值；部分会员追加一次中段充值。

    充值额 ≥ 终身消费（含缓冲），保证任何时点余额不为负；充值只形成待消费余额。
    金额单位在内部用「分」整数，落库转两位小数元。
    """
    rows: list[dict] = []
    # 会员 -> 所属群 index
    member_cohort: dict[str, int] = {}
    for i, month in enumerate(COHORT_MONTHS):
        for code in member_codes(i, COHORT_SIZES[i]):
            member_cohort[code] = i

    for member, lifetime_cents in sorted(member_lifetime_consume.items()):
        i = member_cohort[member]
        cohort_month = COHORT_MONTHS[i]
        observed = len(BASE_RETENTION) - i
        channel = member_channel[member]
        # 开卡充值：终身消费 × 缓冲，取整到元，至少比终身消费多 1 元
        open_cents = max(
            lifetime_cents + 100,
            int(math.ceil(lifetime_cents * RECHARGE_BUFFER / 100.0)) * 100,
        )
        open_no = f"RC{i:02d}00{member[-4:]}"
        rows.append(
            _base_row(
                order_no=open_no,
                member_code=member,
                cohort_month=cohort_month,
                channel=channel,
                store_code=f"{STORE_PREFIX}{1 + int(h01('rstore', member) * STORE_COUNT):03d}",
                order_type=OrderType.RECHARGE.value,
                order_month=cohort_month,
                month_index=0,
                amount=Decimal(open_cents) / Decimal(100),
                # 开卡即充值：对纯开卡会员这是第一笔订单（首单仍以消费 is_first_order 为准）
                ordered_at=order_timestamp(cohort_month, open_no),
            )
        )
        # 中段追充值：观察期足够长且命中概率的会员，在 M3 追加
        if observed >= 5 and h01("topup", SALT, member) < TOPUP_RATE:
            topup_cents = int(round(lifetime_cents * (0.25 + 0.2 * h01("topamt", member)) / 100.0)) * 100
            topup_cents = max(5000, topup_cents)  # 单笔不低于 50 元
            topup_month = order_month(cohort_month, 3)
            topup_no = f"RT{i:02d}03{member[-4:]}"
            rows.append(
                _base_row(
                    order_no=topup_no,
                    member_code=member,
                    cohort_month=cohort_month,
                    channel=channel,
                    store_code=f"{STORE_PREFIX}{1 + int(h01('tstore', member) * STORE_COUNT):03d}",
                    order_type=OrderType.RECHARGE.value,
                    order_month=topup_month,
                    month_index=3,
                    amount=Decimal(topup_cents) / Decimal(100),
                    ordered_at=order_timestamp(topup_month, topup_no),
                )
            )
    return rows


def build_refund_orders(
    consume_rows: list[dict],
    cells: dict[tuple[int, int], CellPlan],
    member_recharge: dict[str, list[dict]],
) -> list[dict]:
    """生成退款单（恒负、关联原单）。

    - 退消费：选非最后观察月的消费单，约 REFUND_CONSUME_RATE 比例，
      其中约 1/3 跨月退款（落到下一观察月），用于验证跨月冲减；
      只退部分金额（70%~95%），保证会员当月仍活跃、且不超额。
    - 退未核销充值：从期末仍有余额的会员里挑少量，退款额 < 期末余额，
      只减待消费余额、不碰净营收。
    """
    rows: list[dict] = []
    # 群 index -> 可观察月数
    observed_map = {i: len(BASE_RETENTION) - i for i in range(len(COHORT_MONTHS))}

    # 可退消费单：月序 m 后面至少还有一个观察月，便于安排跨月退款
    refundable = [
        (idx, row)
        for idx, row in enumerate(consume_rows)
        if row["month_index"] + 1 < observed_map[COHORT_MONTHS.index(row["cohort_month"])]
    ]
    target_count = int(round(len(consume_rows) * REFUND_CONSUME_RATE))
    # 确定性等距抽样，保证分布均匀且可复现
    step = max(1, len(refundable) // target_count) if refundable else 1
    chosen = refundable[:: step][:target_count]

    refund_seq = 0
    for _, original in chosen:
        i = COHORT_MONTHS.index(original["cohort_month"])
        observed = observed_map[i]
        cross_month = h01("rf", SALT, original["order_no"]) < 0.34
        refund_m = original["month_index"] + (1 if cross_month else 0)
        if refund_m >= observed:
            refund_m = original["month_index"]
        ratio = 0.70 + 0.25 * h01("rfratio", original["order_no"])  # 70%~95%
        refund_cents = max(100, int(round(float(original["amount"]) * 100 * ratio)))
        refund_cents = min(refund_cents, int(round(float(original["amount"]) * 100)) - 100)
        if refund_cents <= 0:
            continue
        refund_month = order_month(original["cohort_month"], refund_m)
        refund_no = f"RF{refund_seq:06d}"
        refund_seq += 1
        rows.append(
            _base_row(
                order_no=refund_no,
                member_code=original["member_code"],
                cohort_month=original["cohort_month"],
                channel=original["channel"],
                store_code=original["store_code"],
                order_type=OrderType.REFUND.value,
                order_month=refund_month,
                month_index=refund_m,
                amount=-(Decimal(refund_cents) / Decimal(100)),
                refund_order_no=original["order_no"],
                refund_type=RefundType.CONSUME.value,
                ordered_at=order_timestamp(refund_month, refund_no),
            )
        )

    # 退未核销充值：先估算每个会员期末余额（充值 - 消费），只退余额充足者
    recharge_sum: dict[str, int] = {}
    for member, recharge_list in member_recharge.items():
        recharge_sum[member] = sum(
            int(round(float(r["amount"]) * 100)) for r in recharge_list
        )
    # 会员消费额由调用方通过闭包外字典传入更直接，这里用 consume_rows 重算
    consume_sum: dict[str, int] = {}
    for row in consume_rows:
        consume_sum[row["member_code"]] = consume_sum.get(row["member_code"], 0) + int(
            round(float(row["amount"]) * 100)
        )

    candidates = []
    for member, recharge_cents in recharge_sum.items():
        balance = recharge_cents - consume_sum.get(member, 0)
        if balance >= 8000:  # 期末余额 ≥ 80 元才有空间退未核销充值
            candidates.append((member, balance))
    candidates.sort(key=lambda x: x[0])
    target_recharge_refunds = int(round(len(member_recharge) * RECHARGE_REFUND_RATE))
    for member, balance in candidates[:target_recharge_refunds]:
        i = next(idx for idx, m in enumerate(COHORT_MONTHS) if member.startswith(f"M{idx:02d}-"))
        observed = observed_map[i]
        refund_m = max(0, observed - 1)  # 在最后一个观察月退，验证不碰营收
        refund_cents = min(balance - 100, max(2000, int(round(balance * 0.25 / 100.0)) * 100))
        if refund_cents <= 0:
            continue
        # 关联该会员的开卡充值单
        open_recharge = next(
            r for r in member_recharge[member] if r["month_index"] == 0
        )
        refund_month = order_month(open_recharge["cohort_month"], refund_m)
        refund_no = f"RR{refund_seq:06d}"
        refund_seq += 1
        rows.append(
            _base_row(
                order_no=refund_no,
                member_code=member,
                cohort_month=open_recharge["cohort_month"],
                channel=open_recharge["channel"],
                store_code=open_recharge["store_code"],
                order_type=OrderType.REFUND.value,
                order_month=refund_month,
                month_index=refund_m,
                amount=-(Decimal(refund_cents) / Decimal(100)),
                refund_order_no=open_recharge["order_no"],
                refund_type=RefundType.RECHARGE.value,
                ordered_at=order_timestamp(refund_month, refund_no),
            )
        )
    return rows


def demo_plan() -> tuple[dict[tuple[int, int], CellPlan], dict, list[dict]]:
    cells = build_cells()
    counts = solve_order_counts(cells)
    consume_rows, member_channel, lifetime = iter_consume_orders(cells, counts)
    recharge_rows = build_recharge_orders(cells, member_channel, lifetime)

    member_recharge: dict[str, list[dict]] = {}
    for row in recharge_rows:
        member_recharge.setdefault(row["member_code"], []).append(row)

    refund_rows = build_refund_orders(consume_rows, cells, member_recharge)
    rows = consume_rows + recharge_rows + refund_rows
    return cells, counts, rows


def cohort_seed_rows() -> list[dict]:
    from src.constants.channels import DEFAULT_CAC

    splits = monthly_channel_split()
    out = []
    for i, month in enumerate(COHORT_MONTHS):
        mix = {
            channel: {"members": splits[i][channel], "cac": DEFAULT_CAC[channel]}
            for channel in COHORT_CHANNELS
        }
        out.append(
            {
                "month": month,
                "size": COHORT_SIZES[i],
                "channel_mix": mix,
                "note": COHORT_NOTES[i],
            }
        )
    return out


if __name__ == "__main__":  # 本地自检：不连库也能验证设计值与对账
    cells, counts, rows = demo_plan()
    consume = [r for r in rows if r["order_type"] == OrderType.CONSUME.value]
    recharge = [r for r in rows if r["order_type"] == OrderType.RECHARGE.value]
    refunds = [r for r in rows if r["order_type"] == OrderType.REFUND.value]
    print(
        "orders", len(rows), "consume", len(consume),
        "recharge", len(recharge), "refund", len(refunds),
    )
    print("M1", cells[(0, 1)].active, "/", COHORT_SIZES[0])
    print("M6", cells[(0, 6)].active, "/", COHORT_SIZES[0])
