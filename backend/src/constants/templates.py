"""审计动作名、口径说明文案（前端只展示后端回传的文案，不另写一份）。"""
from __future__ import annotations

ACTION_LOGIN = "auth.login"
ACTION_LOGIN_FAILED = "auth.login_failed"
ACTION_REBUILD = "matrix.rebuild"
ACTION_LTV_RUN = "ltv.run"
ACTION_LTV_PUBLISH = "ltv.publish"
ACTION_EXPORT = "export.download"
ACTION_FORBIDDEN = "auth.forbidden"

METRIC_NOTES: dict[str, str] = {
    "matrix": (
        "按首单月分群；Mn 活跃 = 第 n 个月有未被全额退款的消费的会员数 / 群规模；"
        "仅充值或仅退款不算活跃；观察期未到的格子标右删失，不参与拟合。"
    ),
    "wallet": (
        "储值制：充值只形成待消费余额（负债），不确认营收；净营收 = 消费核销 - 退消费，"
        "在消费/退款发生月确认，跨月核销归真实消费月。"
    ),
    "reconcile": "对账恒等式：充值 - 消费 - 退未核销充值 = 期末待消费余额；差异非 0 禁止发布。",
    "fit": "ARPU 留存 R_m = a·exp(-λm)；λ 在 [0.02, 0.60] 上 0.001 步长网格搜索，权重 = 群规模，a 为给定 λ 的闭式解。",
    "ltv_hist": "历史 LTV = 群内观察期累计净营收 / 群规模，跨群按会员数加权（不含待消费余额）。",
    "ltv_pred": "预测 12 月 LTV = ARPU₀ × Σ_{m=0..11} a·exp(-λm)，ARPU₀ 取 M0 净营收口径。",
    "payback": "回本周期 = CAC / 月均毛利贡献；月均毛利贡献 = 观察期人均月净营收 × 毛利率（默认 50%）。",
    "low_sample": "群或「群×渠道」样本 < 30 标 low_sample，不进入渠道排行。",
}
