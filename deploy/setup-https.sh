#!/usr/bin/env bash
#
# 一次性脚本：让服务器上已有的 nginx 以 HTTPS 对外提供 task-backend。
#
# 用法（在服务器上执行，需要 root）：
#   sudo bash setup-https.sh <你的邮箱>              # 自动探测公网 IP
#   sudo bash setup-https.sh <你的邮箱> <公网IP>     # 手动指定 IP
#   sudo bash setup-https.sh <你的邮箱> <公网IP> nip.io   # 换用 nip.io（限额被占满时的备选）
#
# 它做三件事：
#   1. 用 <公网IP>.sslip.io 当主机名（无需注册域名，该服务把 IP 直接编码在域名里）
#   2. 在 /etc/nginx/conf.d/ 新增一个独立站点，反向代理到 127.0.0.1:8000
#   3. 用 certbot 申请 Let's Encrypt 证书，并交给 systemd 定时自动续期
#
# 安全边界：只新增一个站点文件，不改 nginx 主配置，不碰服务器上已有的其他站点。
# 这是一次性的主机层配置，所以没有放进每次部署的 CI 流程里——
# 让 CI 每次 push 都去改宿主机 nginx 配置，风险远大于收益。

set -euo pipefail

EMAIL="${1:-}"
IP_ARG="${2:-}"
BASE_DOMAIN="${3:-sslip.io}"

SITE="task-backend"
CONF="/etc/nginx/conf.d/${SITE}.conf"
UPSTREAM="127.0.0.1:8000"

die() { echo "错误: $*" >&2; exit 1; }
info() { echo "==> $*"; }

# ---------- 前置检查 ----------

[ "$(id -u)" -eq 0 ] || die "需要 root 权限，请用 sudo 执行"

[ -n "$EMAIL" ] || die "请提供邮箱（Let's Encrypt 用它发送证书到期提醒）
用法: sudo bash setup-https.sh you@example.com"

case "$EMAIL" in
  *@*.*) ;;
  *) die "邮箱格式看起来不对: $EMAIL" ;;
esac

command -v nginx >/dev/null || die "没有找到 nginx。本脚本假设 nginx 已装在宿主机上"

# 确认后端确实在监听，否则配好代理也是 502
info "检查后端是否在 ${UPSTREAM} 上响应"
if ! curl -fsS --max-time 5 "http://${UPSTREAM}/health" >/dev/null; then
  die "后端没有响应。请先确认容器在跑：docker compose ps"
fi

# ---------- 确定主机名 ----------

if [ -n "$IP_ARG" ]; then
  IP="$IP_ARG"
else
  info "探测公网 IP"
  IP="$(curl -fsS --max-time 10 https://api.ipify.org 2>/dev/null \
     || curl -fsS --max-time 10 https://ifconfig.me 2>/dev/null \
     || true)"
  [ -n "$IP" ] || die "探测不到公网 IP，请作为第二个参数手动传入"
fi

# 严格校验 IPv4，避免把垃圾字符串拼进域名
if ! printf '%s' "$IP" | grep -qE '^([0-9]{1,3}\.){3}[0-9]{1,3}$'; then
  die "'$IP' 不是合法的 IPv4 地址"
fi
for octet in $(printf '%s' "$IP" | tr '.' ' '); do
  [ "$octet" -le 255 ] || die "'$IP' 不是合法的 IPv4 地址"
done

HOST="${IP}.${BASE_DOMAIN}"
info "将使用主机名: ${HOST}"

# 确认该域名真的解析回本机 IP，否则 HTTP-01 校验必然失败
RESOLVED="$(getent hosts "$HOST" 2>/dev/null | awk '{print $1}' | head -1 || true)"
if [ -n "$RESOLVED" ] && [ "$RESOLVED" != "$IP" ]; then
  die "${HOST} 解析到 ${RESOLVED}，与预期的 ${IP} 不一致"
fi

# 80 端口必须能从公网访问，Let's Encrypt 要走 HTTP-01 校验
if command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q "Status: active"; then
  ufw status | grep -qE '(^|[^0-9])80(/tcp)?\s' \
    || echo "提示: ufw 已启用但似乎没放行 80 端口，证书校验可能失败（sudo ufw allow 80,443/tcp）"
fi

# ---------- 写入站点配置 ----------

if [ -f "$CONF" ]; then
  info "$CONF 已存在，保留现有内容（certbot 可能已经改过它）"
else
  info "写入 $CONF"
  cat > "$CONF" <<NGINX_CONF
# task-backend 反向代理。由 deploy/setup-https.sh 生成。
# certbot 会在本文件中追加 443 的 ssl 配置。
server {
    listen 80;
    listen [::]:80;
    server_name ${HOST};

    # 只匹配上面这个 server_name，不影响本机其他站点
    location / {
        proxy_pass http://${UPSTREAM};
        proxy_http_version 1.1;

        proxy_set_header Host              \$host;
        proxy_set_header X-Real-IP         \$remote_addr;
        proxy_set_header X-Forwarded-For   \$proxy_add_x_forwarded_for;
        # 让后端知道外部是 https，生成的链接才不会退化成 http
        proxy_set_header X-Forwarded-Proto \$scheme;

        # 透传 WebSocket 升级头，便于以后接实时推送
        proxy_set_header Upgrade    \$http_upgrade;
        proxy_set_header Connection \$http_connection;

        proxy_read_timeout 60s;
    }
}
NGINX_CONF
fi

info "校验 nginx 配置"
nginx -t || die "nginx 配置校验失败，已停止。$CONF 请人工检查"

info "重载 nginx"
systemctl reload nginx 2>/dev/null || nginx -s reload

# ---------- 申请证书 ----------

if ! command -v certbot >/dev/null; then
  info "安装 certbot"
  if command -v apt-get >/dev/null; then
    apt-get update -qq
    apt-get install -y -qq certbot python3-certbot-nginx
  elif command -v dnf >/dev/null; then
    dnf install -y -q certbot python3-certbot-nginx
  elif command -v yum >/dev/null; then
    yum install -y -q certbot python3-certbot-nginx
  else
    die "无法自动安装 certbot，请手动安装后重跑本脚本"
  fi
fi

info "申请 Let's Encrypt 证书（--redirect 会自动把 http 跳转到 https）"
if ! certbot --nginx \
      -d "$HOST" \
      --non-interactive \
      --agree-tos \
      --email "$EMAIL" \
      --redirect; then
  echo >&2
  echo "证书申请失败。最常见的两个原因：" >&2
  echo "  1. 公网无法访问本机 80 端口（安全组/防火墙没放行）" >&2
  echo "  2. ${BASE_DOMAIN} 的 Let's Encrypt 共享限额被占满" >&2
  echo "     —— 换另一个域名重试：sudo bash setup-https.sh $EMAIL $IP nip.io" >&2
  exit 1
fi

# certbot 安装时会自带定时续期，这里确认一下
info "检查自动续期"
systemctl list-timers 2>/dev/null | grep -q certbot \
  && echo "    certbot 定时续期已启用" \
  || echo "    提示: 没检测到续期定时器，请确认 certbot.timer 或 cron 任务存在"

# ---------- 验证 ----------

info "验证 HTTPS"
curl -fsS --max-time 10 "https://${HOST}/health" || die "HTTPS 访问失败"
echo

cat <<DONE

====================================================
HTTPS 配置完成

  接口基地址: https://${HOST}
  探活:       https://${HOST}/health
  接口文档:   https://${HOST}/docs

证书会自动续期，无需人工处理。
====================================================
DONE
