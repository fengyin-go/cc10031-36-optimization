import { request } from './client'
import { moduleMeta } from '@/data/modules'
import type { ActionResult, EntryRow, OverviewResult, PageResult } from '@/data/types'

export { moduleMeta }

export async function listEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<PageResult> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}?${query}`)
  if (!response.ok) throw new Error('业务数据读取失败')
  return await response.json()
}

export async function createEntry(
  key: string,
  values: Record<string, string>,
): Promise<ActionResult> {
  const response = await request(`/api/${key}`, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  return await response.json()
}

export async function runAction(
  key: string,
  id: number,
  action: string,
): Promise<ActionResult> {
  const response = await request(`/api/${key}/${id}/actions`, {
    method: 'POST',
    body: JSON.stringify({ action }),
  })
  return await response.json()
}

export async function downloadEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<void> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}/export?${query}`)
  if (!response.ok) throw new Error('业务数据导出失败')
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${key}.csv`
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}

export async function loadOverview(): Promise<OverviewResult> {
  const response = await request('/api/overview')
  if (!response.ok) throw new Error('运营概览读取失败')
  return await response.json()
}

/** 跨模块数据流：获取全平台领域快照 */
export async function loadNodeStats(module?: string): Promise<Record<string, unknown>> {
  const query = module ? `?module=${module}` : ''
  const response = await request(`/api/nodestats${query}`)
  if (!response.ok) throw new Error('节点统计读取失败')
  return await response.json()
}

/** 跨模块数据流：获取指定 topic 的通道消息 */
export async function loadChannelSnapshot(topic: string): Promise<Record<string, unknown>> {
  const response = await request(`/api/channel/${topic}`)
  if (!response.ok) throw new Error('通道快照读取失败')
  return await response.json()
}

/** 跨模块数据流：查询脏数据隔离区 */
export async function loadDirtyVault(source?: string): Promise<Record<string, unknown>> {
  const query = source ? `?source=${source}` : ''
  const response = await request(`/api/dirty-vault${query}`)
  if (!response.ok) throw new Error('脏数据查询失败')
  return await response.json()
}

/** 跨模块数据流：查询采集记录 */
export async function loadCollectRecords(batchRef?: string): Promise<Record<string, unknown>> {
  const query = batchRef ? `?batch_ref=${batchRef}` : ''
  const response = await request(`/api/collect-records${query}`)
  if (!response.ok) throw new Error('采集记录查询失败')
  return await response.json()
}

/** 跨模块数据流：执行RPA全生命周期管道 */
export async function triggerPipeline(lifecycle: string): Promise<Record<string, unknown>> {
  const response = await request(`/api/pipeline/${lifecycle}`, {
    method: 'POST',
  })
  if (!response.ok) throw new Error('管道执行失败')
  return await response.json()
}

/** 跨模块数据流：获取失败恢复状态 */
export async function loadRecoveryStats(): Promise<Record<string, unknown>> {
  const response = await request('/api/recovery')
  if (!response.ok) throw new Error('恢复状态查询失败')
  return await response.json()
}

// ---------------------------------------------------------------------------
// 运行概览：增量指标水位
// ---------------------------------------------------------------------------

const METRICS_BASE = '/api/report-dashboard'

/** 查询指标水位、纳入版本与最近刷新耗时 */
export async function loadMetricWatermark(): Promise<import('@/data/types').WatermarkInfo> {
  const response = await request(`${METRICS_BASE}/watermark`)
  if (!response.ok) throw new Error('指标水位读取失败')
  return await response.json()
}

/** 按日期范围查询时间窗运行指标数据集 */
export async function loadMetricWindows(
  start?: string,
  end?: string,
): Promise<{ items: import('@/data/types').MetricWindow[]; total: number }> {
  const params = new URLSearchParams()
  if (start) params.set('start', start)
  if (end) params.set('end', end)
  const suffix = params.toString() ? `?${params.toString()}` : ''
  const response = await request(`${METRICS_BASE}/metrics${suffix}`)
  if (!response.ok) throw new Error('运行指标读取失败')
  return await response.json()
}

/** 增量刷新运行指标；并发时后端返回 36 号冲突终态 */
export async function refreshMetricOverview(
  force = false,
): Promise<import('@/data/types').MetricRefreshResult> {
  const response = await request(`${METRICS_BASE}/refresh?force=${force}`, { method: 'POST' })
  if (!response.ok) throw new Error('指标刷新失败')
  return await response.json()
}

/** 查询修订记录 */
export async function loadMetricRevisions(
  status?: string,
): Promise<{ items: import('@/data/types').RevisionRow[]; total: number }> {
  const suffix = status ? `?status=${encodeURIComponent(status)}` : ''
  const response = await request(`${METRICS_BASE}/revisions${suffix}`)
  if (!response.ok) throw new Error('修订记录读取失败')
  return await response.json()
}

/** 查询指标流程升级审计 */
export async function loadMetricUpgrades(): Promise<{
  items: import('@/data/types').UpgradeRow[]
  total: number
}> {
  const response = await request(`${METRICS_BASE}/upgrades`)
  if (!response.ok) throw new Error('升级记录读取失败')
  return await response.json()
}

/** 登记修订记录（只重算受影响时间窗） */
export async function registerMetricRevision(
  source: string,
  sourceId: number,
  reason: string,
): Promise<{ ok: boolean; revision?: import('@/data/types').RevisionRow; detail?: string }> {
  const response = await request(`${METRICS_BASE}/revisions`, {
    method: 'POST',
    body: JSON.stringify({ source, source_id: sourceId, reason }),
  })
  return await response.json()
}

/** 对受影响时间窗重建全部指标 */
export async function rebuildMetricWindows(
  windowDates: string[],
): Promise<Record<string, unknown>> {
  const response = await request(`${METRICS_BASE}/rebuild`, {
    method: 'POST',
    body: JSON.stringify({ window_dates: windowDates }),
  })
  if (!response.ok) throw new Error('窗口重建失败')
  return await response.json()
}

/** 升级指标流程：continue 沿用旧水位 / rebuild 重建历史窗口 */
export async function upgradeMetricFlow(
  strategy: 'continue' | 'rebuild',
): Promise<Record<string, unknown>> {
  const response = await request(`${METRICS_BASE}/upgrade`, {
    method: 'POST',
    body: JSON.stringify({ strategy }),
  })
  if (!response.ok) throw new Error('指标流程升级失败')
  return await response.json()
}
