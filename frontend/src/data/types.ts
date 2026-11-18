/** 纯前端数据层的公共类型：与全栈版后端返回的结构保持一致，换回后端时页面不用改。 */

export type EntryRow = {
  id: number
  status: string
  pending: boolean
  abnormal: boolean
  [field: string]: string | number | boolean
}

export type ModuleMeta = {
  key: string
  name: string
  entity: string
  desc: string
  fields: string[]
  statuses: string[]
  actions: string[]
  actionTargets: Record<string, string>
  metrics: string[]
}

export type PageResult = {
  items: EntryRow[]
  total: number
  page: number
  size: number
}

export type ActionResult = {
  ok: boolean
  message: string
}

export type OverviewResult = {
  cards: { label: string; value: number }[]
  modules: { name: string; created: number; pending: number; abnormal: number }[]
}

/** 运行概览增量指标：时间窗指标数据集 */
export type MetricWindow = {
  window_date: string
  total_exec: number
  success_exec: number
  failed_exec: number
  total_batches: number
  missed_batches: number
  collected_rows: number
  avg_duration_ms: number
  availability: number
  metric_version: string
  exec_id_list: number[]
  batch_id_list: number[]
}

/** 指标水位、纳入版本与最近刷新耗时 */
export type WatermarkInfo = {
  watermark: {
    last_exec_id: number
    last_batch_id: number
    metric_version: string
    legacy: boolean
    updated_at: string
  }
  last_exec_id: number
  last_batch_id: number
  metric_version: string
  legacy: boolean
  last_elapsed_ms?: number
  refreshed_at?: string
  last_refresh?: {
    status: string
    elapsed_ms: number
    refreshed_at: string
    [key: string]: unknown
  }
  included_counts: Record<string, number>
  window_count: number
  pending_revisions: number
  storage: string
  db_path: string
  migrations: { seq: number; version: string; applied_at: string }[]
}

export type MetricRefreshResult = {
  status: 'success' | 'noop' | 'conflict'
  mode?: string
  conflict_code?: number
  conflict_terminal?: string
  message?: string
  new_executions?: number
  new_batches?: number
  drift_detected?: number
  revision_windows?: string[]
  rebuilt_windows?: string[]
  refresh_ms?: number
  metric_version?: string
}

export type RevisionRow = {
  revision_id: number
  source: string
  source_id: number
  window_date: string
  reason: string
  status: string
  applied_at: string | null
  created_at: string
}

export type UpgradeRow = {
  upgrade_id: number
  from_version: string
  to_version: string
  strategy: string
  windows_touched: number
  created_at: string
}
