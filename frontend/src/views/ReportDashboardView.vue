<template>
  <section class="page" data-module="report_dashboard">
    <header class="page-head">
      <div>
        <h2>运行概览（增量指标）</h2>
        <p class="page-desc">
          持久化处理水位与已纳入版本，刷新只处理新增终态，修订仅重算受影响时间窗。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="metric-refresh" type="button" :disabled="busy" @click="onRefresh(false)">
          {{ busy ? '刷新中…' : '增量刷新概览' }}
        </button>
        <button class="btn" data-testid="metric-force-refresh" type="button" :disabled="busy" @click="onRefresh(true)">
          强制对账
        </button>
      </div>
    </header>

    <!-- 水位与纳入版本 -->
    <div class="stat-row" data-testid="watermark-cards">
      <article class="stat-card">
        <span class="stat-label">执行水位（最大ID）</span>
        <strong class="stat-value">{{ watermark?.last_exec_id ?? '—' }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">批次水位（最大ID）</span>
        <strong class="stat-value">{{ watermark?.last_batch_id ?? '—' }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">已纳入指标版本</span>
        <strong class="stat-value version">{{ watermark?.metric_version ?? '—' }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">最近刷新耗时</span>
        <strong class="stat-value">{{ watermark?.last_elapsed_ms ?? '—' }} ms</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">待重算修订</span>
        <strong class="stat-value" :class="{ warn: (watermark?.pending_revisions ?? 0) > 0 }">
          {{ watermark?.pending_revisions ?? 0 }}
        </strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">指标窗口数 / 存储</span>
        <strong class="stat-value">{{ watermark?.window_count ?? 0 }} · {{ watermark?.storage }}</strong>
      </article>
    </div>

    <p class="status-legend" data-testid="watermark-meta">
      <span class="legend-item">最近刷新：{{ watermark?.refreshed_at ?? '尚未刷新' }}</span>
      <span class="legend-item">已纳入执行：{{ watermark?.included_counts.execution_log ?? 0 }}</span>
      <span class="legend-item">已纳入批次：{{ watermark?.included_counts.batch_tracker ?? 0 }}</span>
      <span class="legend-item">历史版本边界：{{ watermark?.legacy ? '兼容旧版本' : '当前版本' }}</span>
      <span class="legend-item">库：{{ watermark?.db_path ?? '—' }}</span>
    </p>

    <!-- 日期范围查询 -->
    <form class="filter-bar" @submit.prevent="loadAll">
      <label class="filter-item">
        <span>开始日期</span>
        <input v-model="startDate" data-testid="metric-start" type="date" />
      </label>
      <label class="filter-item">
        <span>结束日期</span>
        <input v-model="endDate" data-testid="metric-end" type="date" />
      </label>
      <button class="btn" data-testid="metric-query" type="submit">查询指标窗口</button>
      <button class="btn ghost" type="button" @click="resetRange">重置范围</button>
    </form>

    <!-- 时间窗指标数据集 -->
    <table class="data-table" data-testid="metric-windows">
      <thead>
        <tr>
          <th>统计日期</th><th>总执行数</th><th>成功数</th><th>失败数</th>
          <th>批次数</th><th>遗漏批次</th><th>采集总量</th><th>平均耗时(ms)</th>
          <th>可用率</th><th>纳入版本</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in windows" :key="row.window_date" :data-testid="`window-${row.window_date}`">
          <td>{{ row.window_date }}</td>
          <td>{{ row.total_exec }}</td>
          <td>{{ row.success_exec }}</td>
          <td>{{ row.failed_exec }}</td>
          <td>{{ row.total_batches }}</td>
          <td>{{ row.missed_batches }}</td>
          <td>{{ row.collected_rows }}</td>
          <td>{{ row.avg_duration_ms }}</td>
          <td>{{ formatRate(row.availability) }}</td>
          <td>{{ row.metric_version }}</td>
        </tr>
        <tr v-if="!windows.length">
          <td colspan="10" class="empty-state">暂无指标窗口，请先点击「增量刷新概览」</td>
        </tr>
      </tbody>
    </table>

    <!-- 升级与修订操作 -->
    <div class="ops-grid">
      <section class="ops-card">
        <h3>指标流程升级（不重复累计历史执行）</h3>
        <p class="hint">
          沿用旧水位只处理新增数据（continue），或对受影响历史时间窗重建全部指标（rebuild）。
        </p>
        <div class="page-actions">
          <button class="btn" data-testid="upgrade-continue" type="button" :disabled="busy" @click="onUpgrade('continue')">
            沿用旧水位继续（continue）
          </button>
          <button class="btn primary" data-testid="upgrade-rebuild" type="button" :disabled="busy" @click="onUpgrade('rebuild')">
            重建历史窗口（rebuild）
          </button>
        </div>
        <table class="data-table sub-table" data-testid="upgrade-history">
          <thead><tr><th>升级ID</th><th>原版本</th><th>目标版本</th><th>策略</th><th>重建窗口</th><th>时间</th></tr></thead>
          <tbody>
            <tr v-for="up in upgrades" :key="up.upgrade_id">
              <td>{{ up.upgrade_id }}</td><td>{{ up.from_version }}</td><td>{{ up.to_version }}</td>
              <td>{{ up.strategy }}</td><td>{{ up.windows_touched }}</td><td>{{ up.created_at }}</td>
            </tr>
            <tr v-if="!upgrades.length"><td colspan="6" class="empty-state">暂无升级记录</td></tr>
          </tbody>
        </table>
      </section>

      <section class="ops-card">
        <h3>修订记录（仅重算受影响窗口）</h3>
        <form class="revision-form" @submit.prevent="onRegisterRevision">
          <label class="filter-item">
            <span>来源</span>
            <select v-model="revisionSource" data-testid="revision-source">
              <option value="execution_log">执行记录</option>
              <option value="batch_tracker">采集批次</option>
            </select>
          </label>
          <label class="filter-item">
            <span>记录ID</span>
            <input v-model.number="revisionId" data-testid="revision-id" type="number" min="1" required />
          </label>
          <button class="btn" data-testid="revision-submit" type="button" @click="onRegisterRevision">登记修订</button>
        </form>
        <table class="data-table sub-table" data-testid="revision-list">
          <thead><tr><th>ID</th><th>来源</th><th>记录</th><th>时间窗</th><th>原因</th><th>状态</th></tr></thead>
          <tbody>
            <tr v-for="rev in revisions" :key="rev.revision_id">
              <td>{{ rev.revision_id }}</td><td>{{ rev.source }}</td><td>{{ rev.source_id }}</td>
              <td>{{ rev.window_date }}</td><td>{{ rev.reason }}</td><td>{{ rev.status }}</td>
            </tr>
            <tr v-if="!revisions.length"><td colspan="6" class="empty-state">暂无修订记录</td></tr>
          </tbody>
        </table>
      </section>
    </div>

    <footer class="page-foot">
      <span data-testid="metric-message" :class="{ 'error-text': isError, 'conflict-text': isConflict }">
        {{ message }}
      </span>
      <span>增量指标由水位引擎驱动，刷新不重算历史窗口</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import {
  loadMetricRevisions,
  loadMetricUpgrades,
  loadMetricWatermark,
  loadMetricWindows,
  refreshMetricOverview,
  registerMetricRevision,
  upgradeMetricFlow,
} from '@/api/backend-service'
import type {
  MetricRefreshResult,
  MetricWindow,
  RevisionRow,
  UpgradeRow,
  WatermarkInfo,
} from '@/data/types'

const watermark = ref<WatermarkInfo | null>(null)
const windows = ref<MetricWindow[]>([])
const revisions = ref<RevisionRow[]>([])
const upgrades = ref<UpgradeRow[]>([])
const startDate = ref('')
const endDate = ref('')
const busy = ref(false)
const message = ref('就绪')
const isError = ref(false)
const isConflict = ref(false)

const revisionSource = ref<'execution_log' | 'batch_tracker'>('execution_log')
const revisionId = ref<number | null>(null)

function setMessage(text: string, conflict = false, error = false) {
  message.value = text
  isConflict.value = conflict
  isError.value = error
}

function formatRate(value: number): string {
  return `${(value * 100).toFixed(2)}%`
}

async function loadAll() {
  try {
    const [wm, metricResult, revisionResult, upgradeResult] = await Promise.all([
      loadMetricWatermark(),
      loadMetricWindows(startDate.value || undefined, endDate.value || undefined),
      loadMetricRevisions(),
      loadMetricUpgrades(),
    ])
    watermark.value = wm
    windows.value = metricResult.items
    revisions.value = revisionResult.items
    upgrades.value = upgradeResult.items
  } catch (error) {
    setMessage(error instanceof Error ? error.message : '运行概览数据加载失败', false, true)
  }
}

function resetRange() {
  startDate.value = ''
  endDate.value = ''
  void loadAll()
}

async function onRefresh(force: boolean) {
  busy.value = true
  try {
    const result: MetricRefreshResult = await refreshMetricOverview(force)
    if (result.status === 'conflict') {
      setMessage(
        `${result.conflict_terminal ?? '冲突'}：${result.message ?? '并发刷新只接受首个成功结果'}`,
        true,
        false,
      )
    } else {
      setMessage(
        `增量刷新完成：${result.status}，新增执行 ${result.new_executions ?? 0}、` +
          `新增批次 ${result.new_batches ?? 0}、重算窗口 ${(result.rebuilt_windows ?? []).length} 个、` +
          `耗时 ${result.refresh_ms ?? 0}ms`,
      )
    }
    await loadAll()
  } catch (error) {
    setMessage(error instanceof Error ? error.message : '指标刷新失败', false, true)
  } finally {
    busy.value = false
  }
}

async function onUpgrade(strategy: 'continue' | 'rebuild') {
  busy.value = true
  try {
    const result = await upgradeMetricFlow(strategy)
    setMessage(
      `指标流程已升级（${strategy}），重建窗口 ${String(result.windows_touched ?? 0)} 个，历史执行未重复累计`,
    )
    await loadAll()
  } catch (error) {
    setMessage(error instanceof Error ? error.message : '指标流程升级失败', false, true)
  } finally {
    busy.value = false
  }
}

async function onRegisterRevision() {
  if (!revisionId.value) {
    setMessage('请填写需要登记修订的记录ID', false, true)
    return
  }
  try {
    const result = await registerMetricRevision(
      revisionSource.value,
      revisionId.value,
      '前端登记修订',
    )
    if (!result.ok) {
      setMessage(result.detail ?? '修订登记失败：仅终态记录可登记', false, true)
    } else {
      setMessage('修订已登记，下次增量刷新只会重算受影响时间窗')
    }
    await loadAll()
  } catch (error) {
    setMessage(error instanceof Error ? error.message : '修订登记失败', false, true)
  }
}

onMounted(loadAll)
</script>

<style scoped>
.page-actions {
  display: flex;
  gap: 8px;
}
.status-legend {
  display: flex;
  flex-wrap: wrap;
  gap: 16px;
  color: var(--muted);
  font-size: 12px;
  margin: 4px 0 12px;
}
.version {
  font-size: 15px;
  color: var(--brand);
}
.warn {
  color: #b54708;
}
.ops-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
  margin-top: 18px;
}
.ops-card {
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 14px;
}
.ops-card h3 {
  margin: 0 0 6px;
  font-size: 14px;
}
.hint {
  color: var(--muted);
  font-size: 12px;
  margin: 0 0 10px;
}
.sub-table {
  margin-top: 12px;
}
.revision-form {
  display: flex;
  gap: 10px;
  align-items: flex-end;
  flex-wrap: wrap;
}
.revision-form select,
.revision-form input {
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 7px;
}
.conflict-text {
  color: #b54708;
  font-weight: 600;
}
@media (max-width: 980px) {
  .ops-grid {
    grid-template-columns: 1fr;
  }
}
</style>
