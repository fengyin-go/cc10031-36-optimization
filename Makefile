.PHONY: install backend frontend migrate-metrics metrics-info metrics-reset

install:
	cd backend && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
	cd frontend && npm ci

backend:
	cd backend && ./run.sh

frontend:
	cd frontend && npm run dev

# 流水线数据库迁移部署：幂等执行指标水位库迁移，重复执行不会重复建表/累计
migrate-metrics:
	cd backend && .venv/bin/python -m scripts.metrics_admin migrate

# 查看指标水位、纳入版本、最近刷新耗时与迁移状态
metrics-info:
	cd backend && .venv/bin/python -m scripts.metrics_admin info

# 本地开发环境切换：清空指标数据并重置水位（保留表结构与迁移记录）
metrics-reset:
	cd backend && .venv/bin/python -m scripts.metrics_admin reset
