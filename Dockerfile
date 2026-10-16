# 采用轻量且受官方维护的 Python 运行时
FROM python:3.11-slim

# 禁止缓冲，保证终端实时输出报错与访问日志
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# 利用 Docker 分层缓存机制加速构建
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制代码至容器
COPY . .

# 暴露端口并以生产模式启动
EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
