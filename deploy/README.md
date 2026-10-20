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

只需在服务器上跑一次，之后证书自动续期。

```bash
cd /opt/task-backend

# 方式一：不用域名，直接用 <公网IP>.sslip.io（最快，但见下方限额说明）
sudo bash deploy/setup-https.sh 你的邮箱@example.com

# 方式二：用自己的域名或免费子域（推荐）
sudo bash deploy/setup-https.sh 你的邮箱@example.com myapp.duckdns.org
```

脚本会在 `/etc/nginx/conf.d/task-backend.conf` 新增一个独立站点，然后用
certbot 申请 Let's Encrypt 证书。**不会改动 nginx 主配置或已有站点**，且在
`nginx -t` 校验失败时直接中止，不会 reload 坏配置。

完成后接口基地址即 `https://<你的主机名>`。

### 域名选择：注意 Public Suffix List

Let's Encrypt 按「注册域名」计算签发限额。域名**在 PSL 上**则限额独享，
**不在 PSL 上**则与该域名的全体使用者共享，可能签发或续期失败。

| 域名 | 在 PSL | 说明 |
|---|---|---|
| 自有域名 | ✅ | 最可靠。`.xyz`/`.top` 首年常仅 1-3 美元 |
| `duckdns.org` | ✅ | 免费，GitHub 登录即可建子域，支持 TXT 记录 |
| `is-a.dev` / `js.org` | ✅ | 免费，提 PR 申请，需面向开发者/开源项目 |
| `eu.org` | ✅ | 免费且永久，人工审核需数天 |
| `sslip.io` / `nip.io` | ❌ | 免注册、把 IP 编码在域名里，**限额共享** |

`sslip.io` 适合联调自测。签发失败时脚本会提示换备用域名：

```bash
sudo bash deploy/setup-https.sh 邮箱 <IP> nip.io
```

换成 PSL 内的域名时，把新主机名作为第二个参数重跑即可（记得先把 DNS 指向
服务器公网 IP）。原有的 `/etc/nginx/conf.d/task-backend.conf` 需要先删除或
改掉其中的 `server_name`，否则两个站点会冲突。

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
