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
# --proxy-headers：信任 nginx 传来的 X-Forwarded-Proto/For，
#   否则应用会以为外部是 http，生成的链接和日志里的客户端 IP 都是错的。
# --forwarded-allow-ips=*：容器看到的来源是 docker 网关而非固定 IP。
#   这里放开是安全的——端口只绑在宿主机 127.0.0.1，公网无法直连，
#   只有本机的 nginx 能把请求送进来。
CMD ["uvicorn", "main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--proxy-headers", \
     "--forwarded-allow-ips", "*"]
