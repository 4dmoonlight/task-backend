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

## H5 介绍页

根域 `hahahahaha.party`（含 `www`）提供的是一个**纯静态**的产品介绍页，与
`api.hahahahaha.party` 的接口服务分开：两者是 `/etc/nginx/conf.d/` 下两个独立站点
文件，各自的 `server_name`、`root` / `proxy_pass` 互不引用。

```
hahahahaha.party, www.*   ──▶  nginx ──▶ 静态文件 /opt/task-backend/web/index.html
api.hahahahaha.party      ──▶  nginx ──▶ 127.0.0.1:8000 (FastAPI)
```

### 两个文件，两件事

| 项 | 位置 | 谁负责 |
|---|---|---|
| 站点配置（版本化） | 仓库 `deploy/h5.conf` → 服务器 `/etc/nginx/conf.d/h5.conf` | 改域名/证书/缓存策略时才动；改完重跑一次安装 |
| 页面内容 | 仓库 `web/` → 服务器 `/opt/task-backend/web/` | 日常改文案，push 即可 |

`deploy/h5.conf` 是**版本化的静态配置**，不是模板——里面没有任何占位符，路径、
域名、安全头全是字面量。看一眼文件就知道线上是什么样。

### 一次性安装（需要 root，只做一次）

在服务器上，工作目录是 `/opt/task-backend`（**不是** `deploy/` 子目录）：

```bash
cd /opt/task-backend
sudo bash deploy/install-h5-site.sh
```

脚本会依次：校验源文件与静态目录 → 以 root 校验证书与私钥配对 →
备份现有 `h5.conf`（若有）→ `install -m 644 root:root` → `nginx -t` →
`systemctl reload nginx` → 本机自测 → 全部通过后删除备份。

自测用 `curl -sk --resolve` 把域名钉到 `127.0.0.1`，绕过 DNS 与 Cloudflare，
验证的是这段 nginx 配置本身：

- `https://hahahahaha.party/` → 200，且响应体里含 `<html`
- `https://api.hahahahaha.party/health` → 200（确认没有把 api 站点带下线）

> **reload 是异步的**：`nginx.service` 是 `Type=forking`，`nginx -s reload` 发完
> HUP 就立刻返回，并不等待新配置生效。所以脚本不会在 reload 一返回就判定，而是
> 每 0.5 秒轮询一次、最多 20 次（约 10 秒），确认新配置真正接管请求后才算成功。
> 这段时间里短暂返回旧内容（例如根域返回 API 的 JSON 404）属正常。超时才算失败，
> 并会打印当次的状态行、content-type 与响应体片段，帮助判断请求落到了哪个站点。

任何一步失败都会回滚到安装前的状态并重新 reload 旧配置；`nginx -t` 失败时
根本不会 reload，线上不受影响。

卸载（同样在 `/opt/task-backend` 下执行）：

```bash
cd /opt/task-backend
sudo bash deploy/install-h5-site.sh --remove
```

卸载同样会备份、删除、`nginx -t`、reload，并回归验证 api `/health` 仍为 200。

### 日常更新：只 push，不碰服务器

改 `web/index.html`（或新增静态资源）→ commit / push。CI 把 `web/` 同步到
`/opt/task-backend/web/`，nginx 直接读文件，**不需要登录服务器，也不需要 root**。

> CI 的 `scp-action` 白名单里写的是目录名 `web`（不是 `web/*`）。传目录时会**保留
> 目录名**，落到 `/opt/task-backend/web/`；若改成 `web/*`，文件会被摊平到
> `/opt/task-backend/` 根下，`root` 就指不到 `index.html` 了。`deploy/` 同理保留
> `deploy/` 这一层。

### 什么时候才需要重新执行安装

只有站点配置本身变了才需要：

- 域名（`server_name`）
- 证书 / 私钥路径
- 缓存策略、安全头、监听端口

改这些 → 编辑 `deploy/h5.conf` → push（CI 同步到服务器）→ 在服务器上重跑一次：

```bash
cd /opt/task-backend
sudo bash deploy/install-h5-site.sh
```

重复执行等价于重新安装 + 重新校验，幂等。

### 为什么不保留长期的 root 入口

之前一版设计引入过「常驻的 root 助手 + sudoers 白名单」，让 `deploy` 用户能随时
把配置写进 `/etc/nginx`。这个能力与它的代价不成比例：

**能写 nginx 配置，等价于能让 root 代为部署任意站点。** 一份配置里可以声明
`root /;`、`alias`、`proxy_pass` 到内网服务，把不该公开的文件树暴露出去，或者
把流量引到别处。`nginx -t` 只检查语法是否合法，检查不了「这个站点该不该存在」。

现在的折中是：

- **安装是一次性的人工动作**，由人用 root 执行、看清输出
- **不安装任何常驻脚本、不写任何 sudoers 条目**，日常部署（改页面）完全不需要 root
- 站点配置有版本、有 diff、随时可以回滚

### 关于缺省站点（未命中 server_name 的请求）

`nginx.conf` 里是 `include /etc/nginx/conf.d/*.conf;`，**按文件名排序**加载。
`api.conf` 排在 `h5.conf` 之前，所以 **`api.conf` 是 `:80` 与 `:443` 的缺省站点**：

- `https://api.hahahahaha.party` 按 `server_name` 精确匹配，照常反代到 `127.0.0.1:8000`
- **未命中任何 `server_name` 的请求**（例如直接用 IP 访问）**仍然落到 API**，
  与安装 `h5.conf` 之前的行为完全一致

`h5.conf` 刻意**不声明 `default_server`**：一是不改变既有行为，二是避免与
`api.conf` 将来可能添加的 `default_server` 冲突（同一 `listen` 上重复声明会让
`nginx -t` 直接失败）。

另外，这台机器没有全局 IPv6 地址，`api.conf` 也只监听 IPv4，因此 `h5.conf` 同样
不写 `listen [::]:80` / `listen [::]:443`。

### 证书

`h5.conf` 复用 api 站点那张 Cloudflare Origin CA 证书：

```
ssl_certificate     /etc/nginx/ssl/api.hahahahaha.party.pem;
ssl_certificate_key /etc/nginx/ssl/api.hahahahaha.party.key;
```

SAN 覆盖 `*.hahahahaha.party` 与 `hahahahaha.party`，根域和 `www` 都在内，不需要
另签证书。

私钥是 `root:root 600`，**`deploy` 用户读不到**。这就是为什么证书与私钥的配对
校验只能发生在 `install-h5-site.sh` 里（它以 root 运行），而不是在日常部署流程里。

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

> 注意：当前这台服务器的 api 站点文件叫 `api.conf`，证书是 Cloudflare Origin CA
> （`/etc/nginx/ssl/api.hahahahaha.party.pem` + `.key`），属于既有配置。本节描述的是
> **全新机器首次接入**的流程；在这台机器上**不要**再跑 `setup-https.sh`，否则会多出
> 一个 server_name 相同的 `task-backend.conf`，触发 `conflicting server name` 告警。

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
| `API_KEY` | 必需的共享 API 密钥，缺失时服务拒绝启动；**只放在服务器本地**，详见下节 |
| `ENABLE_DOCS` | 可选；设为 `1` 才启用 `/docs` 与 `/openapi.json`，生产不要设 |

`DATABASE_URL` 由 [compose.yml](../compose.yml) 自动拼装，不用手配。

密码在 Postgres 数据卷初始化时写入数据库，改 `.env` 不会同步改掉数据库里的
密码，只会导致连不上。确实需要换密码时要同时重置数据卷（会丢数据）。

## API 访问控制

所有业务接口（`/api/v1/*`）都需要在请求头里带 `X-API-Key`。缺失或错误一律返回
`401 Unauthorized`，不区分两种情况，避免泄露任何提示。

### 密钥放在哪

- 只存在于服务器的 `/opt/task-backend/.env`（`chmod 600`，属主 `deploy`），**不进 git**
- `compose.yml` 里 api 服务已有 `env_file: .env`，变量自动注入容器，无需改 compose
- CI 的 `.env` 处理逻辑只在缺少 `POSTGRES_PASSWORD` 时追加该行，**从不覆盖已有行**，
  所以 `API_KEY` 不会被部署流程冲掉
- **本仓库是公开仓库，任何密钥都不得写进代码、测试、workflow 或文档**

### 怎么生成与轮换

生成一个新密钥：

```bash
openssl rand -hex 32
```

轮换步骤（**服务端与双端必须同步换**，否则客户端会立刻全部 401）：

1. 在服务器 `/opt/task-backend/.env` 里改 `API_KEY=` 那一行
2. `cd /opt/task-backend && docker compose up -d`（只重启 api 容器即可，数据库不动）
3. 同步更新 iOS（`APIConfig.swift`）与 Android（`TodoApi.kt` 的 `APIConfig`）里的常量
4. 重新构建并分发两个客户端

注意顺序：先改客户端还是先改服务端都会有一段窗口期，期间旧客户端会 401。
如果在意，可以先用新密钥发布客户端、再切服务端。

### `/health` 保持公开

容器健康检查用 `python urllib` 直接请求 `http://127.0.0.1:8000/health`，不带任何头，
因此 `/health` **刻意不加鉴权**。它只返回 `{"status": "healthy"}`，不含任何业务数据。

### 接口文档默认关闭

生产环境下 `/docs`、`/redoc`、`/openapi.json` 全部关闭（对应 FastAPI 的
`docs_url/redoc_url/openapi_url = None`）。本地开发需要调试时：

```bash
ENABLE_DOCS=1 uvicorn main:app --reload
```

此时 `/docs` 与 `/openapi.json` 可用。注意业务接口仍然要求 `X-API-Key`。

### 这个模型能挡什么、挡不住什么

`X-API-Key` 是**共享密钥**模型：

- **能挡**：互联网上的自动化扫描器、随手访问、误打误撞的爬虫
- **挡不住**：任何认真的人。密钥被打包进 App 二进制，逆向即可提取
  （iOS 字符串搜索、Android 反编译都很快）；也无法区分具体用户、无法单独吊销

两个客户端仓库（ReminderTest、TodoTest）目前**都在本地、未公开**，所以密钥暂时
放在源码常量里。**一旦要把任一客户端推送到公开仓库，必须先把它改成从不受版本管理
的本地配置读取**（iOS 用 xcconfig 或本地 plist，Android 用 `local.properties`
经 BuildConfig 注入），绝不能随源码公开。

要真正做用户级鉴权，需要引入账号体系与令牌（JWT/OAuth），那是另一套设计。

## 已知待办

- **表结构变更没有迁移工具**：启动时 `create_all()` 只建表、不改表。后续增删
  字段需要接入 Alembic。
- **`X-API-Key` 是共享密钥**：能挡扫描，挡不住逆向；无用户区分、无法单独吊销。
  要做用户级鉴权需引入账号体系与令牌，见上文「API 访问控制」。
