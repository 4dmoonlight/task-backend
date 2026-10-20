# 部署说明

## 架构

```
前端 (Web / iOS)
   │ HTTPS
   ▼
nginx (宿主机, 80/443)          ← TLS 在这里终结
   │ HTTP, 仅 127.0.0.1
   ▼
容器 task-backend (:8000)  ──▶  容器 task-backend-db (Postgres)
                                      └─ 数据卷 pgdata
```

应用容器的端口**只绑在 `127.0.0.1:8000`**，公网无法直连，必须经宿主机 nginx
转发。数据库不映射任何宿主端口，只在 compose 内部网络可达。

## 日常部署

push 到 `main` 即自动部署，无需手工操作。流程见
[.github/workflows/deploy.yml](../.github/workflows/deploy.yml)：

1. 用真实 Postgres 跑一遍集成测试，失败则不部署
2. 构建 Docker 镜像验证可打包
3. 上传源码到 `/opt/task-backend`
4. `docker compose up --wait`，等健康检查通过
5. 宿主机侧再探活一次，失败则打印容器日志并让部署失败

## 一次性配置：HTTPS

只需在服务器上跑一次，之后证书自动续期：

```bash
cd /opt/task-backend
sudo bash deploy/setup-https.sh 你的邮箱@example.com
```

脚本会用 `<公网IP>.sslip.io` 作为主机名（免注册域名），在
`/etc/nginx/conf.d/task-backend.conf` 新增一个独立站点，然后用 certbot
申请 Let's Encrypt 证书。**不会改动 nginx 主配置或已有站点。**

完成后接口基地址是 `https://<公网IP>.sslip.io`。

### 注意

- `sslip.io` / `nip.io` **不在 Public Suffix List 中**，Let's Encrypt 的
  每域名限额由全体用户共享。签发失败时换另一个域名重试：
  `sudo bash deploy/setup-https.sh 邮箱 <IP> nip.io`
- 正式上线建议自备域名，把 `server_name` 换成自己的域名后重跑 certbot，
  不受共享限额影响。

## 环境变量

服务器上的 `/opt/task-backend/.env`（不进版本库）：

| 变量 | 说明 |
|---|---|
| `POSTGRES_PASSWORD` | 首次部署由 CI 随机生成并固化，**不要手改** |
| `POSTGRES_DB` | 可选，默认 `taskdb` |
| `POSTGRES_USER` | 可选，默认 `taskuser` |

`DATABASE_URL` 由 [compose.yml](../compose.yml) 自动拼装，不用手配。

密码在 Postgres 数据卷初始化时写入数据库，改 `.env` 不会同步改掉数据库里的
密码，只会导致连不上。确实需要换密码时要同时重置数据卷（会丢数据）。

## 接口文档

服务自带 Swagger UI，可直接在浏览器调试、也可拿 schema 生成客户端模型：

- `https://<域名>/docs` — 交互式文档
- `https://<域名>/openapi.json` — 机器可读 schema

## 已知待办

- **表结构变更没有迁移工具**：启动时 `create_all()` 只建表、不改表。后续增删
  字段需要接入 Alembic。
- **CORS 为 `allow_origins=["*"]` 且 `allow_credentials=True`**：这个组合下
  浏览器会拒绝带凭证的请求。加鉴权前需收敛为具体域名（iOS 原生请求不受影响）。
