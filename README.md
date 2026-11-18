# RPA流程自动化中心

面向企业数据采集场景，提供RPA流程编排、元素定位、执行调度和结果校验的自动化运营平台

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/WorkbenchView.vue|tsx  全部模块共享工作台
│   ├── src/data/modules.ts              18个业务模块配置
│   ├── src/api/              统一请求封装
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/resource.py  通用查询/导出/登记/动作接口
│   ├── app/services/<key>.py    每个模块独立领域逻辑
│   ├── app/services/base.py     共享服务骨架
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 流程定义 | `bot_definition` | RPA流程 | 流程名称、目标系统、触发方式 |
| 步骤编排 | `step_editor` | 流程步骤 | 步骤序号、步骤名称、操作类型 |
| 元素定位 | `element_selector` | 定位规则 | 规则名称、选择器类型、定位表达式 |
| 触发配置 | `trigger_config` | 触发器 | 触发器名称、触发类型、调度表达式 |
| 执行记录 | `execution_log` | 执行日志 | 执行编号、所属流程、开始时间 |
| 数据提取 | `data_extract` | 提取规则 | 规则名称、源页面结构、提取字段 |
| 凭据管理 | `credential_store` | 系统凭据 | 凭据名称、目标系统、认证方式 |
| 环境配置 | `env_profile` | 执行环境 | 环境名称、浏览器版本、操作系统 |
| 反封锁 | `anti_block` | 防护策略 | 策略名称、目标站点、请求间隔 |
| 结果校验 | `result_verify` | 校验规则 | 规则名称、校验字段、校验条件 |
| 批次追踪 | `batch_tracker` | 采集批次 | 批次编号、触发流程、采集时间 |
| 页面建模 | `page_model` | 页面模型 | 模型名称、目标站点、页面结构 |
| 数据补全 | `data_enrich` | 补全规则 | 规则名称、目标字段、补全来源 |
| 代理路由 | `proxy_router` | 代理节点 | 节点名称、IP地址、出口地域 |
| 频控策略 | `rate_limiter` | 频控规则 | 规则名称、目标站点、每秒上限 |
| 输出格式 | `output_format` | 格式模板 | 模板名称、输出类型、字段映射 |
| 异常日志 | `anomaly_log` | 异常事件 | 事件编号、关联流程、异常类型 |
| 调度计划 | `scheduler` | 采集计划 | 计划名称、绑定流程、cron表达式 |
| 运行概览 | `report_dashboard` | 运行数据 | 统计日期、总执行数、成功数 |
| 任务队列 | `task_queue` | 排队任务 | 任务编号、流程名称、提交时间 |

## 约定

- 全部模块共享`frontend/src/views/WorkbenchView.vue|tsx`和一套资源路由。
- 每个模块仍保留独立`backend/app/services/<模块>.py`；纯前端保留
  `frontend/src/domain/<模块>.ts`，用于承载真实业务差异并维持36题位容量。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 运行概览：增量指标水位

运行概览（`report_dashboard`）不再每次刷新都全量重算执行日志与采集批次，
改由 `backend/app/metrics/` 下的**指标水位引擎**驱动，状态持久化在 SQLite。

### 核心机制

- **水位与版本同时持久化**：`metric_meta` 保存执行/批次已扫描到的最大 id（处理水位）
  与已纳入指标流程版本（`metric-flow-v1` / `metric-flow-v2`），重启后继续处理，不重算历史。
- **终态才纳入、台账去重**：`metric_processed` 以 `(source, source_id)` 为主键，
  同一条执行/批次无论刷新多少次只纳入一次，升级指标流程后不会重复累计历史执行。
- **修订只重算受影响窗口**：`metric_revisions` 登记修订记录及统计日期，
  刷新时仅重建这些时间窗；执行记录/批次状态流转会自动登记修订边界。
- **升级双策略**：
  - `continue`：沿用旧水位与历史窗口结果，只让新增数据按新版本累计；
  - `rebuild`：对受影响历史时间窗按新版本整体重建。
    两种策略都靠台账去重，不重复累计同一执行。
- **并发首胜**：同一时刻只接受首个成功刷新，其余并发请求返回
  `36号处理终态：并发冲突`（业务码 36），不改变已累计指标。

### 指标接口（前缀 `/api/report-dashboard`）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/watermark` | 指标水位、纳入版本、最近刷新耗时、迁移状态 |
| GET | `/metrics?start=&end=` | 按日期范围查询时间窗指标数据集 |
| GET | `/revisions` | 修订记录及重算状态 |
| GET | `/upgrades` | 指标流程升级审计 |
| POST | `/refresh?force=` | 增量刷新（并发冲突返回 36 号终态） |
| POST | `/revisions` | 登记修订（仅终态记录，触发受影响窗口重算） |
| POST | `/rebuild` | 对指定时间窗重建全部指标 |
| POST | `/upgrade` | 升级指标流程（`continue` / `rebuild`） |
| POST | `/legacy-boundary` | 把当前聚合标记为旧版本边界（兼容升级前场景） |

### 数据库迁移与本地切换

- 迁移脚本在 `backend/app/metrics/migrations.py`，幂等建表并登记
  `metric_migrations`，流水线重复部署不会重建表或重复累计；容器启动时自动执行迁移。
- 指标库路径由环境变量 `METRICS_DB` 控制（见 `.env.example`）：
  默认 `backend/var/metric_watermark.sqlite3`，测试/隔离调试可用 `:memory:`。
- 运维命令：

```bash
make migrate-metrics   # 流水线数据库迁移部署（幂等）
make metrics-info      # 查看水位、纳入版本、最近刷新耗时
make metrics-reset     # 本地开发环境切换：清空指标并重置水位（保留表结构）
```

领域：数据工程
子方向：数据采集、爬虫与RPA
