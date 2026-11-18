FROM python:3.12-slim
WORKDIR /srv/app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8000
# 启动前先幂等执行指标水位库迁移（流水线数据库迁移部署），再起服务
CMD ["sh", "-c", "python -m scripts.metrics_admin migrate && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
