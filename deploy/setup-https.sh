#!/usr/bin/env bash
#
# 一次性脚本：让服务器上已有的 nginx 以 HTTPS 对外提供 task-backend。
#
# 用法（在服务器上执行，需要 root）：
#   sudo bash setup-https.sh <邮箱>                    # 自动探测公网 IP，用 <IP>.sslip.io
#   sudo bash setup-https.sh <邮箱> <主机名>           # 指定任意域名（DuckDNS / 自有域名）
#   sudo bash setup-https.sh <邮箱> <公网IP>           # 指定 IP，仍拼成 <IP>.sslip.io
#   sudo bash setup-https.sh <邮箱> <公网IP> nip.io    # 换备用 IP 域名（限额被占满时）
#
# 例：
#   sudo bash setup-https.sh me@foo.com myapp.duckdns.org
#   sudo bash setup-https.sh me@foo.com api.mydomain.com
#
# 它做三件事：
#   1. 确定对外主机名（不传域名时用 <公网IP>.sslip.io，无需注册域名）
#   2. 在 /etc/nginx/conf.d/ 新增一个独立站点，反向代理到 127.0.0.1:8000
#   3. 用 certbot 申请 Let's Encrypt 证书，并交给 systemd 定时自动续期
#
# 注意：sslip.io / nip.io 不在 Public Suffix List 上，Let's Encrypt 的限额由
# 全体用户共享，可能签发失败。duckdns.org 等在 PSL 上的域名限额独享，更可靠。
#
# 安全边界：只新增一个站点文件，不改 nginx 主配置，不碰服务器上已有的其他站点。
# 这是一次性的主机层配置，所以没有放进每次部署的 CI 流程里——
# 让 CI 每次 push 都去改宿主机 nginx 配置，风险远大于收益。

set -euo pipefail

EMAIL="${1:-}"
TARGET="${2:-}"
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

is_ipv4() {
  printf '%s' "$1" | grep -qE '^([0-9]{1,3}\.){3}[0-9]{1,3}$' || return 1
  for octet in $(printf '%s' "$1" | tr '.' ' '); do
    [ "$octet" -le 255 ] || return 1
  done
  return 0
}

# 逐段校验主机名。这个值会被拼进 nginx 配置，所以严格限制字符集。
is_hostname() {
  printf '%s' "$1" | grep -qE '^[A-Za-z0-9.-]+$' || return 1
  # 排除开头/结尾的点和连续的点（会产生空标签）
  case "$1" in
    .*|*.|*..*) return 1 ;;
  esac
  # 必须是多段的完整域名
  case "$1" in
    *.*) ;;
    *) return 1 ;;
  esac
  # 每一段都不能以连字符开头或结尾
  for label in $(printf '%s' "$1" | tr '.' ' '); do
    printf '%s' "$label" \
      | grep -qE '^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?$' || return 1
  done
  return 0
}

detect_ip() {
  curl -fsS --max-time 10 https://api.ipify.org 2>/dev/null \
    || curl -fsS --max-time 10 https://ifconfig.me 2>/dev/null \
    || true
}

IP=""
HOST=""
HOST_IS_EXPLICIT=0

if [ -z "$TARGET" ]; then
  # 没传第二个参数：探测公网 IP，拼成 <IP>.sslip.io
  info "探测公网 IP"
  IP="$(detect_ip)"
  [ -n "$IP" ] || die "探测不到公网 IP，请作为第二个参数手动传入 IP 或域名"
  is_ipv4 "$IP" || die "探测到的 '$IP' 不是合法 IPv4，请手动指定"
  HOST="${IP}.${BASE_DOMAIN}"
elif is_ipv4 "$TARGET"; then
  # 传的是 IP：拼成 <IP>.sslip.io
  IP="$TARGET"
  HOST="${IP}.${BASE_DOMAIN}"
else
  # 传的是主机名：直接用（DuckDNS、自有域名等）
  # 纯数字加点但不是合法 IPv4 —— 多半是打错的 IP，明确报错而不是当域名用
  if printf '%s' "$TARGET" | grep -qE '^[0-9.]+$'; then
    die "'$TARGET' 不是合法的 IPv4 地址（每段必须 0-255）"
  fi
  is_hostname "$TARGET" \
    || die "'$TARGET' 不是合法的主机名。需要形如 myapp.duckdns.org"
  HOST="$TARGET"
  HOST_IS_EXPLICIT=1
fi

info "将使用主机名: ${HOST}"

# 确认域名解析回本机，否则 Let's Encrypt 的 HTTP-01 校验必然失败
RESOLVED="$(getent hosts "$HOST" 2>/dev/null | awk '{print $1}' | head -1 || true)"
if [ "$HOST_IS_EXPLICIT" -eq 1 ]; then
  [ -n "$IP" ] || IP="$(detect_ip)"
  if [ -z "$RESOLVED" ]; then
    echo "提示: ${HOST} 当前解析不到任何地址。"
    echo "      请先在域名服务商（或 DuckDNS 后台）把它指向 ${IP:-本机公网 IP}，否则证书申请会失败。"
  elif [ -n "$IP" ] && [ "$RESOLVED" != "$IP" ]; then
    # 不直接退出：域名可能挂在 CDN 后面，这种情况下不一致是正常的
    echo "警告: ${HOST} 解析到 ${RESOLVED}，而本机公网 IP 是 ${IP}。"
    echo "      如果没有套 CDN，证书申请会失败——请先改好 DNS 解析。"
  fi
elif [ -n "$RESOLVED" ] && [ "$RESOLVED" != "$IP" ]; then
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
  echo "证书申请失败。常见原因：" >&2
  echo "  1. 公网无法访问本机 80 端口（安全组/防火墙没放行）" >&2
  echo "  2. ${HOST} 的 DNS 没有正确解析到本机公网 IP" >&2
  if [ "$HOST_IS_EXPLICIT" -eq 0 ]; then
    echo "  3. ${BASE_DOMAIN} 不在 Public Suffix List 上，Let's Encrypt 限额由全体" >&2
    echo "     用户共享，可能已被占满。两个办法：" >&2
    echo "       换备用域名：sudo bash setup-https.sh $EMAIL $IP nip.io" >&2
    echo "       换 PSL 内的免费域名（限额独享），如 duckdns.org 注册一个子域后：" >&2
    echo "       sudo bash setup-https.sh $EMAIL 你的名字.duckdns.org" >&2
  fi
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
