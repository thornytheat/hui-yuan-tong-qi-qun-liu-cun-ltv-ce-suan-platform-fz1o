<template>
  <section class="panel panel--tight wallet">
    <div class="spread">
      <div class="eyebrow">储值口径 · 待消费余额与净营收</div>
      <span class="tag" :class="wallet.balanced ? 'tag--ok' : 'tag--warn'">
        {{ wallet.balanced ? '对账已对平' : '对账差异' }}
      </span>
    </div>

    <div class="wallet__grid">
      <div class="kpi">
        <span class="label">待消费余额</span>
        <span class="kpi__value mono" data-test="wallet-balance">{{ money(wallet.pending_balance) }}</span>
        <span class="kpi__sub mono">会员负债，未确认营收</span>
      </div>
      <div class="kpi">
        <span class="label">净营收</span>
        <span class="kpi__value mono" data-test="wallet-net-revenue">{{ money(wallet.net_revenue) }}</span>
        <span class="kpi__sub mono">消费核销 - 退消费</span>
      </div>
      <div class="kpi">
        <span class="label">累计充值</span>
        <span class="kpi__value mono">{{ money(wallet.recharge_total) }}</span>
        <span class="kpi__sub mono">不计入营收</span>
      </div>
      <div class="kpi" :class="{ 'kpi--alert': !wallet.balanced }">
        <span class="label">对账差异</span>
        <span class="kpi__value mono" data-test="wallet-diff">{{ money(wallet.reconcile_diff, 2) }}</span>
        <span class="kpi__sub mono">{{ wallet.balanced ? '恒等式残差为 0' : '明细与余额对不上' }}</span>
      </div>
    </div>

    <dl class="wallet__flow">
      <div>
        <dt>累计消费核销</dt>
        <dd class="mono">{{ money(wallet.consume_total) }}</dd>
      </div>
      <div>
        <dt>退已核销消费</dt>
        <dd class="mono">-{{ money(wallet.refund_consume_total) }}</dd>
      </div>
      <div>
        <dt>退未核销充值</dt>
        <dd class="mono">-{{ money(wallet.refund_recharge_total) }}</dd>
      </div>
      <div v-if="wallet.balance_members">
        <dt>台账会员</dt>
        <dd class="mono">{{ integer(wallet.balance_members) }} 人</dd>
      </div>
    </dl>

    <p class="wallet__identity mono">{{ wallet.identity }}</p>
    <p v-if="!wallet.balanced" class="wallet__block" data-test="wallet-block">
      对账差异非零，留存矩阵 / LTV / 渠道回本不得发布，请先修复订单事实后重建矩阵。
    </p>
  </section>
</template>

<script setup lang="ts">
import type { LtvWallet, ReconcileInfo } from '../../types/domain'
import { integer, money } from '../../utils/format'

defineProps<{ wallet: LtvWallet | ReconcileInfo }>()
</script>

<style scoped>
.wallet__grid {
  display: grid; grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 0; margin: 12px 0 8px;
  border-top: 1px dashed var(--rule); border-bottom: 1px dashed var(--rule);
}
.kpi { display: flex; flex-direction: column; gap: 2px; padding: 10px 12px 10px 0; }
.kpi--alert .kpi__value { color: var(--brick); }
.kpi__value { font-size: clamp(1.15rem, 0.95rem + 0.8vw, 1.6rem); }
.kpi__sub { font-size: 10px; color: var(--coffee-3); }
.wallet__flow {
  margin: 6px 0 0; display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 6px 18px;
}
.wallet__flow div { display: flex; justify-content: space-between; gap: 10px; border-bottom: 1px dotted var(--rule-soft); padding-bottom: 4px; }
.wallet__flow dt { font-size: 11.5px; color: var(--coffee-2); }
.wallet__flow dd { margin: 0; font-size: 12px; }
.wallet__identity { margin: 10px 0 0; font-size: 10px; color: var(--coffee-3); }
.wallet__block {
  margin: 8px 0 0; padding: 8px 10px; font-size: 11.5px;
  border: 1px solid var(--brick); background: var(--cream-3); color: var(--brick);
}

@media (max-width: 1199px) {
  .wallet__grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
@media (max-width: 767px) {
  .wallet__grid { grid-template-columns: 1fr; }
}
</style>
