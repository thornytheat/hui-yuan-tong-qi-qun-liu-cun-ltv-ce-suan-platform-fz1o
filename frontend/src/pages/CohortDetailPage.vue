<template>
  <AppShell>
    <PageHeader
      eyebrow="同期群详情"
      :title="detail ? `${detail.month} 同期群` : '同期群详情'"
      :subtitle="detail ? `${detail.size} 人 · 观察 ${detail.observed_months} 个月 · ${detail.note}` : '加载中…'"
    >
      <template #actions>
        <RouterLink to="/matrix" class="btn btn--quiet">返回矩阵</RouterLink>
        <RouterLink to="/ltv" class="btn">用该口径测算 LTV</RouterLink>
      </template>
    </PageHeader>

    <ErrorNotice v-if="error" :message="error" @retry="load" />

    <div v-if="loading" class="skeleton"></div>

    <EmptyState v-else-if="!detail" title="没有这个同期群" :hint="EMPTY_HINTS.cohort">
      <RouterLink to="/matrix" class="btn">回到矩阵</RouterLink>
    </EmptyState>

    <template v-else>
      <section class="band">
        <div class="band__cell">
          <span class="label">群规模</span>
          <span class="band__value mono">{{ detail.size }}</span>
          <span class="band__sub mono" v-if="detail.low_sample">样本 &lt; 30（low_sample）</span>
          <span class="band__sub mono" v-else>样本充足</span>
        </div>
        <div class="band__cell">
          <span class="label">M1 留存</span>
          <span class="band__value mono" data-test="m1">{{ percent(detail.summary.m1) }}</span>
          <span class="band__sub mono">{{ detail.curve[1]?.active ?? 0 }} 人复购</span>
        </div>
        <div class="band__cell">
          <span class="label">M6 留存</span>
          <span class="band__value mono" data-test="m6">{{ percent(detail.summary.m6) }}</span>
          <span class="band__sub mono">储值权益到期台阶</span>
        </div>
        <div class="band__cell">
          <span class="label">M12 留存</span>
          <span class="band__value mono">{{ percent(detail.summary.m12) }}</span>
          <span class="band__sub mono">满 13 格观察</span>
        </div>
        <div class="band__cell band__cell--note">
          <span class="label">人均净营收</span>
          <span class="band__value mono">{{ money(detail.summary.arpu) }}</span>
          <span class="band__sub mono">{{ money(detail.summary.net_revenue) }} / {{ detail.size }} 人</span>
        </div>
      </section>

      <section class="band band--wallet" v-if="detail.wallet">
        <div class="band__cell">
          <span class="label">累计充值（负债）</span>
          <span class="band__value mono">{{ money(detail.wallet.recharge_total) }}</span>
          <span class="band__sub mono">不确认营收</span>
        </div>
        <div class="band__cell">
          <span class="label">消费核销</span>
          <span class="band__value mono">{{ money(detail.wallet.consume_total) }}</span>
          <span class="band__sub mono">按消费发生月</span>
        </div>
        <div class="band__cell">
          <span class="label">退消费 / 退充值</span>
          <span class="band__value mono">{{ money(detail.wallet.refund_consume_total) }}</span>
          <span class="band__sub mono">退充值 {{ money(detail.wallet.refund_recharge_total) }}</span>
        </div>
        <div class="band__cell">
          <span class="label">净营收</span>
          <span class="band__value mono" data-test="detail-net-revenue">{{ money(detail.wallet.net_revenue) }}</span>
          <span class="band__sub mono">消费 - 退消费</span>
        </div>
        <div class="band__cell band__cell--note">
          <span class="label">期末待消费余额</span>
          <span class="band__value mono" data-test="detail-balance">{{ money(detail.wallet.pending_balance) }}</span>
          <span class="band__sub mono">充值 - 消费 - 退充值</span>
        </div>
      </section>

      <div class="layout">
        <div class="stack grow">
          <section class="panel">
            <RetentionCurve
              :points="curvePoints"
              caption="人数留存曲线（M0..M12）"
              :fitted="null"
            />
            <div class="curve-note">
              <span class="tag tag--censor">M6 台阶</span>
              <p>
                M6 出现台阶式下滑，是储值权益到期导致的已知业务事件，不是数据缺口；
                拟合在「LTV 测算」里用 ARPU 留存单独做，不受人数台阶影响。
              </p>
            </div>
          </section>

          <section class="panel">
            <AmountHistogram
              :buckets="detail.amount_distribution.buckets"
              :total-orders="detail.amount_distribution.orders"
              :avg-ticket="detail.amount_distribution.avg_ticket"
            />
            <p class="curve-note-text">
              该群实际消费单总额 {{ money(detail.amount_distribution.total) }}（仅含消费，不含充值/退款），
              客单 ¥{{ detail.amount_distribution.avg_ticket.toFixed(2) }}；
              高金额段来自礼盒与咖啡豆，饮品客单保持在 ¥26-48。
            </p>
          </section>
        </div>

        <aside class="stack">
          <section class="panel panel--tight">
            <div class="eyebrow">渠道构成</div>
            <table class="table">
              <thead>
                <tr>
                  <th>渠道</th>
                  <th class="num">人数</th>
                  <th class="num">M1</th>
                  <th class="num">M6</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="row in detail.channels" :key="row.channel">
                  <td>
                    <span class="ch">{{ row.short }}</span>
                    <span v-if="row.low_sample" class="tag tag--warn">低样本</span>
                  </td>
                  <td class="num">{{ row.members }}</td>
                  <td class="num">{{ percent(row.m1) }}</td>
                  <td class="num">{{ percent(row.m6) }}</td>
                </tr>
              </tbody>
            </table>
            <p class="side__note">渠道留存按「该渠道会员数」为分母，样本 &lt; 30 的格子标低样本。</p>
          </section>

          <section class="panel panel--tight">
            <div class="eyebrow">净营收拆分（消费 - 退消费）</div>
            <ul class="revs">
              <li v-for="row in detail.channels" :key="`rev-${row.channel}`">
                <span class="revs__name">{{ row.short }}</span>
                <span class="revs__bar">
                  <span :style="{ width: `${revWidth(row.revenue)}%` }"></span>
                </span>
                <span class="revs__value mono">{{ money(row.revenue) }}</span>
              </li>
            </ul>
            <p class="side__note">门店自然流以礼盒为主，人均净营收最高；私域以券为主，频次高客单低。充值余额不计入本拆分。</p>
          </section>

          <section class="panel panel--tight">
            <div class="eyebrow">口径</div>
            <p class="side__note">
              活跃分子 = 该月有未被全额退款消费的去重会员；分母 = 群规模 {{ detail.size }}。
              净营收在消费/退款发生月确认，跨月核销归真实消费月；充值只形成待消费余额。
              右删失月（快照 {{ detail.snapshot_month }} 之后）不出现在曲线上。
            </p>
          </section>
        </aside>
      </div>
    </template>
  </AppShell>
</template>

<script setup lang="ts">
import { computed, onMounted, watch } from 'vue'
import { useRoute } from 'vue-router'
import AppShell from '../components/common/AppShell.vue'
import PageHeader from '../components/common/PageHeader.vue'
import EmptyState from '../components/common/EmptyState.vue'
import ErrorNotice from '../components/common/ErrorNotice.vue'
import RetentionCurve from '../components/business/RetentionCurve.vue'
import AmountHistogram from '../components/business/AmountHistogram.vue'
import { fetchCohortDetail } from '../api/cohort'
import { useAsync } from '../hooks/useAsync'
import type { CohortDetail } from '../types/domain'
import { money, percent } from '../utils/format'
import { EMPTY_HINTS } from '../constants/messages'

const route = useRoute()
const { data: detail, loading, error, run } = useAsync<CohortDetail>()

const curvePoints = computed(
  () => detail.value?.curve.map((point) => ({ m: point.m, rate: point.rate })) ?? [],
)
const maxRevenue = computed(() =>
  Math.max(1, ...(detail.value?.channels.map((row) => row.revenue) ?? [1])),
)

function revWidth(value: number): number {
  return Math.max(4, (value / maxRevenue.value) * 100)
}

async function load() {
  const month = String(route.params.month ?? '')
  if (!month) return
  await run(() => fetchCohortDetail(month))
}

watch(() => route.params.month, load)
onMounted(load)
</script>

<style scoped>
.band {
  display: grid; grid-template-columns: repeat(5, minmax(0, 1fr));
  border: var(--hair); background: var(--cream-2);
}
.band__cell { display: flex; flex-direction: column; gap: 2px; padding: 11px 14px; border-right: var(--hair); }
.band__cell:last-child { border-right: 0; }
.band__cell--note { background: var(--cream-3); }
.band--wallet { margin-top: 12px; border-top: 0; }
.band--wallet .band__value { font-size: 17px; }
.band__value { font-size: 20px; }
.band__sub { font-size: 10px; color: var(--coffee-3); }

.layout {
  display: grid; grid-template-columns: minmax(0, 1.9fr) minmax(290px, 1fr);
  gap: 16px; align-items: start; margin-top: 16px;
}
.side__note { margin: 8px 0 0; font-size: 10.5px; color: var(--coffee-3); line-height: 1.55; }
.curve-note { display: flex; gap: 10px; align-items: flex-start; margin-top: 12px; border-top: var(--hair); padding-top: 10px; }
.curve-note p, .curve-note-text { margin: 0; font-size: 11.5px; color: var(--coffee-2); }
.curve-note-text { margin-top: 10px; border-top: var(--hair); padding-top: 10px; }
.ch { margin-right: 4px; }
.revs { list-style: none; margin: 10px 0 0; padding: 0; display: flex; flex-direction: column; gap: 7px; }
.revs li { display: grid; grid-template-columns: 42px 1fr 64px; align-items: center; gap: 8px; }
.revs__name { font-size: 11.5px; }
.revs__bar { display: block; height: 8px; background: var(--cream-3); border: 1px solid var(--rule-soft); }
.revs__bar span { display: block; height: 100%; background: var(--coffee-3); transition: width 600ms var(--ease); }
.revs__value { font-size: 10px; color: var(--coffee-2); text-align: right; }
.skeleton { height: 300px; border: 1px dashed var(--rule); background: var(--cream-2); }

@media (max-width: 1199px) { .layout { grid-template-columns: 1fr; } }
@media (max-width: 899px) {
  .band { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .band__cell { border-bottom: var(--hair); }
  .band__cell--note { display: none; }
}
</style>
