"""导出：渠道对比 CSV（4 行）+ 测算说明 MD。"""
from __future__ import annotations

import csv
import io

CSV_HEADER = [
    "渠道",
    "渠道代码",
    "CAC(元)",
    "样本会员数",
    "观察月数",
    "人均累计净营收(元)",
    "月均净营收(元)",
    "月均毛利贡献(元)",
    "回本周期(月)",
    "历史LTV(元)",
    "预测12月LTV(元)",
]


def channels_csv(results: dict) -> tuple[str, int]:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(CSV_HEADER)
    for row in results["channels"]:
        writer.writerow(
            [
                row["label"],
                row["channel"],
                f"{row['cac']:.2f}",
                row["members"],
                f"{row['observed_months']:.2f}",
                f"{row['cum_per_member']:.2f}",
                f"{row['monthly_revenue']:.2f}",
                f"{row['monthly_margin']:.2f}",
                f"{row['payback_months']:.1f}",
                f"{row['ltv_hist']:.2f}",
                f"{row['ltv_pred12']:.2f}",
            ]
        )
    return buffer.getvalue(), len(results["channels"])


def report_md(results: dict, report_name: str, published_at: str | None = None) -> str:
    fit = results["fit"]
    params = results["params"]
    lines = [
        f"# {report_name}",
        "",
        f"- 快照月：{results['snapshot_month']}（M0..M12），预测窗 {params['forecast_months']} 个月",
        f"- 同期群：{results['totals']['cohorts']} 个，会员 {results['totals']['members']:,} 人，"
        f"有效格 {results['totals']['observed_cells']} / 右删失 {results['totals']['censored_cells']}",
        f"- 拟合：{fit['model']}，λ = {fit['lambda']:.3f}，a = {fit['a']:.4f}，R² = {fit['r2']:.2f}",
        f"- ARPU₀ = ¥{results['arpu0']:.2f}；历史 LTV = ¥{results['ltv_hist']:,}；"
        f"预测 12 月 LTV = ¥{results['ltv_pred12']:,}",
        f"- 毛利率 {params['margin_rate'] * 100:.0f}%（回本周期按毛利口径）",
        "",
        "| 排名 | 渠道 | CAC | 样本 | 月均毛利贡献 | 回本周期 | 历史 LTV | 预测 12 月 LTV |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in results["channels"]:
        lines.append(
            f"| {row['rank']} | {row['label']} | ¥{row['cac']:.0f} | {row['members']} | "
            f"¥{row['monthly_margin']:.2f} | {row['payback_months']:.1f} 月 | "
            f"¥{row['ltv_hist']:.0f} | ¥{row['ltv_pred12']:.0f} |"
        )
    low = results["low_sample"]
    if low["channel_cells"]:
        lines += ["", "低样本剔除（<30 人，不参与排行）："]
        for cell in low["channel_cells"]:
            lines.append(f"- {cell['month']} × {cell['channel']}：{cell['members']} 人")
    wallet = results.get("wallet") or {}
    if wallet:
        lines += [
            "",
            "## 储值口径与对账",
            "",
            f"- 累计充值（待消费余额来源）：¥{wallet.get('recharge_total', 0):,.2f}",
            f"- 累计消费核销：¥{wallet.get('consume_total', 0):,.2f}",
            f"- 退已核销消费：¥{wallet.get('refund_consume_total', 0):,.2f}"
            "（退款发生月冲净营收）",
            f"- 退未核销充值：¥{wallet.get('refund_recharge_total', 0):,.2f}（只减余额）",
            f"- 净营收（消费 - 退消费）：¥{wallet.get('net_revenue', 0):,.2f}",
            f"- 期末待消费余额：¥{wallet.get('pending_balance', 0):,.2f}",
            f"- 对账差异：¥{wallet.get('reconcile_diff', 0):,.2f}"
            f"（{'已对平，允许发布' if wallet.get('balanced') else '未对平，禁止发布'}）",
            f"- 恒等式：{wallet.get('identity', '充值 - 消费 - 退未核销充值 = 期末待消费余额')}",
        ]
    lines += [
        "",
        f"- 口径：{results['notes']['fit']}",
        f"- 口径：{results['notes']['payback']}",
        "",
        f"生成时间：{published_at or '—'}",
    ]
    return "\n".join(lines) + "\n"
