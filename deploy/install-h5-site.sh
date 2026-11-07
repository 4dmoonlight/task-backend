#!/usr/bin/env bash
#
# install-h5-site.sh —— 把仓库里的 deploy/h5.conf 装到 nginx，**只装一次**。
#
# 用法：
#   sudo bash deploy/install-h5-site.sh            # 安装 / 重新安装
#   sudo bash deploy/install-h5-site.sh --remove   # 卸载
#   sudo bash deploy/install-h5-site.sh --help
#
# 这是唯一需要 root 的动作。装完之后：
#   - 改页面内容只需要更新 /opt/task-backend/web/，不需要 root、不需要重跑本脚本
#   - 只有改域名 / 证书路径 / 缓存与安全头策略时，才需要改 deploy/h5.conf 并重跑
#
# 本脚本明确不做的事：
#   - 不签发证书、不动 DNS、不猜证书位置（路径都写在 h5.conf 里）
#   - 不引入 staged 文件、不写 sudoers、不安装任何常驻的 root 入口
#
# 为什么必须 root：私钥 /etc/nginx/ssl/api.hahahahaha.party.key 是 root:root 600，
# deploy 用户读不到；而 nginx 要校验配置能否加载、能否完成 TLS 握手，这一步只能
# 在 root 上下文里发生。所以证书相关的验证天然属于这次一次性安装。

set -euo pipefail

SITE_NAME="h5"
CONF_DIR="/etc/nginx/conf.d"
CONF="${CONF_DIR}/${SITE_NAME}.conf"
# 用脚本自身所在目录定位源文件，不依赖调用者的 cwd。
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SRC="${SCRIPT_DIR}/h5.conf"

ROOT_DOMAIN="hahahahaha.party"
API_DOMAIN="api.hahahahaha.party"
API_HEALTH_PATH="/health"

REMOVE=0
BACKUP=""
BODY_FILE=""
HEAD_FILE=""
RESP_CODE="000"
RESP_TYPE=""

# 自测轮询参数：reload 是异步的，见 wait_for 的注释。
POLL_INTERVAL="0.5"
POLL_ATTEMPTS=20

die()  { echo "错误: $*" >&2; exit 1; }
info() { echo "==> $*"; }
ok()   { echo "  ✓ $*"; }
warn() { echo "警告: $*" >&2; }
step() { echo; echo "---- $* ----"; }

usage() {
  # 打印文件头部的注释块：跳过 shebang，遇到第一条不以 # 开头的行即停止。
  awk 'NR == 1 { next }
       !started { if ($0 == "#") next; started = 1 }
       started { if ($0 !~ /^#/) exit; sub(/^# ?/, ""); print }' "$0"
}

# 清理备份。trap 在下面创建完探测用的临时文件后再统一注册，
# 会把临时文件与这个 cleanup 一起串上。
cleanup() {
  [ -z "$BACKUP" ] || rm -f "$BACKUP"
}

# ---------- 参数 ----------

while [ $# -gt 0 ]; do
  case "$1" in
    --remove) REMOVE=1 ;;
    -h|--help) usage; exit 0 ;;
    *) die "不接受参数：$1（只有 --remove / --help）" ;;
  esac
  shift
done

# ---------- 前置检查 ----------

[ "$(id -u)" -eq 0 ] || die "需要 root。请用：sudo bash deploy/install-h5-site.sh"

command -v nginx   >/dev/null || die "没有找到 nginx"
command -v curl    >/dev/null || die "没有找到 curl（自测需要）"
command -v systemctl >/dev/null || die "没有找到 systemctl"
# 没有 openssl 时，下面的「证书与私钥是否配对」会拿到两个空输出并误判为通过，
# 所以必须先确认它存在。
command -v openssl >/dev/null || die "没有找到 openssl（校验证书与私钥配对需要）"

info "站点文件: ${CONF}"
info "静态目录: /opt/task-backend/web"

# 探测用的临时文件：安装与卸载两条路径都要用，所以在这里统一创建一次。
BODY_FILE="$(mktemp /tmp/h5-probe-body.XXXXXX)"
HEAD_FILE="$(mktemp /tmp/h5-probe-head.XXXXXX)"
trap 'rm -f "$BODY_FILE" "$HEAD_FILE"; cleanup' EXIT

# 一次探测：把状态码写进 RESP_CODE、content-type 写进 RESP_TYPE，
# 响应头与响应体分别落到给定文件。
# curl 失败时它自己也会输出 000，所以不能再 `|| echo 000` 追加（会拼成
# "000000"）；万一什么都没输出，把空值归一成 000。
probe() {
  local host="$1" path="$2" headers="$3" body="$4" raw
  raw="$(curl -skS -o "$body" -D "$headers" -w '%{http_code}' --max-time 10 \
          --resolve "${host}:443:127.0.0.1" "https://${host}${path}" 2>/dev/null || true)"
  RESP_CODE="${raw//[!0-9]/}"
  RESP_CODE="${RESP_CODE: -3}"
  [ -n "$RESP_CODE" ] || RESP_CODE="000"

  # 取第一条 Content-Type；用 awk 打印后立即退出，避免 sed|head 在
  # pipefail 下出现 SIGPIPE。
  RESP_TYPE="$(awk 'BEGIN { IGNORECASE = 1 }
                    /^Content-Type:/ {
                      sub(/^[^:]*:[[:space:]]*/, "")
                      sub(/\r$/, "")
                      sub(/[[:space:]]+$/, "")
                      print
                      exit
                    }' "$headers" 2>/dev/null || true)"
}

# 根域判定：200 且响应体含 <html（不区分大小写）。
check_root_page() {
  [ "$RESP_CODE" = "200" ] || return 1
  grep -qi '<html' "$BODY_FILE"
}

# api 判定：/health 返回 200。
check_api_health() {
  [ "$RESP_CODE" = "200" ]
}

# 在超时内反复探测，直到判定函数返回真；每次尝试都重新取状态码与响应体，
# 不复用上一次的结果。超时返回 1。
#
# 为什么必须轮询：nginx.service 是 Type=forking，ExecReload 走
# `nginx -s reload`，这条命令发完 HUP 就立刻返回，并不等待新配置生效。
# 若在 reload 返回后立刻自测，此刻旧 worker 仍在按旧配置处理连接：
# 根域匹配不到任何 server_name，会落到缺省站点 api.conf 被反代到 FastAPI，
# 拿到 JSON 404。轮询等到新 worker 接手后再判定，才是对配置本身的检验。
wait_for() {
  local host="$1" path="$2" check="$3" i=1
  while [ "$i" -le "$POLL_ATTEMPTS" ]; do
    probe "$host" "$path" "$HEAD_FILE" "$BODY_FILE"
    if "$check"; then
      [ "$i" -le 1 ] || info "第 ${i} 次尝试通过（新配置已生效）"
      return 0
    fi
    if [ "$i" -lt "$POLL_ATTEMPTS" ]; then
      sleep "$POLL_INTERVAL"
    fi
    i=$((i + 1))
  done
  return 1
}

# 失败时的诊断：把最后一次响应的特征打出来，让人一眼看出请求落到了哪个站点。
# 只在失败路径调用。
print_diagnosis() {
  local label="$1" host="$2" path="$3"
  local status_line snippet

  status_line="$(awk 'NR == 1 { sub(/\r$/, ""); print; exit }' "$HEAD_FILE" 2>/dev/null || true)"
  [ -n "$status_line" ] || status_line="（无响应）"
  # 单行化 + 截断，避免刷屏。
  snippet="$(tr '\r\n\t' '   ' < "$BODY_FILE" 2>/dev/null | cut -c1-160 || true)"

  warn "  ── 自测失败诊断：${label} ──"
  warn "  URL:           https://${host}${path}"
  warn "  状态行:        ${status_line}"
  warn "  content-type:  ${RESP_TYPE:-（无）}"
  warn "  响应体前160字: ${snippet:-（空）}"

  case "${RESP_TYPE:-}" in
    application/json*)
      warn "  解读: content-type 是 application/json → 请求落到了 API 缺省站点"
      warn "        （h5.conf 未被加载或尚未生效），不是静态文件的问题。"
      ;;
    text/html*)
      if [ "$RESP_CODE" = "404" ]; then
        warn "  解读: 返回 html 404 → 确实找不到文件，检查 root 与 index.html 是否可读。"
      elif [ "$RESP_CODE" = "200" ]; then
        warn "  解读: 状态 200 但内容不含 <html → 检查 root 指向的 index.html 是否被替换。"
      fi
      ;;
  esac

  if [ "$RESP_CODE" = "000" ]; then
    warn "  解读: 状态 000 → 连不上，检查是否监听 443、防火墙、nginx 是否在运行。"
  fi
}

reload_nginx() {
  systemctl reload nginx
}

# 回滚：有备份就还原，没有就删掉新装的站点文件；然后重新加载旧配置。
rollback_and_reload() {
  if [ -n "$BACKUP" ] && [ -f "$BACKUP" ]; then
    mv -f "$BACKUP" "$CONF"
    BACKUP=""   # 已归还，trap 不要再删
    info "已恢复原有 ${CONF}"
  else
    rm -f "$CONF"
    info "已删除新建的 ${CONF}"
  fi
  reload_nginx
}

# 安装后失败：回滚 + reload 旧配置，再退出 1。
fail_after_install() {
  local stage="$1" msg="$2"
  warn "[${stage}] ${msg}"
  warn "开始回滚，恢复安装前的状态"
  if rollback_and_reload; then
    warn "[${stage}] 已回滚并重新加载旧配置，线上恢复为安装前的行为"
  else
    warn "[${stage}] 回滚后 reload 仍失败，请立即人工处理："
    warn "         nginx -t && systemctl status nginx"
  fi
  exit 1
}

# ---------- --remove ----------

if [ "$REMOVE" -eq 1 ]; then
  step "卸载 H5 站点"
  if [ ! -f "$CONF" ]; then
    info "${CONF} 不存在，无需卸载"
    exit 0
  fi

  BACKUP="${CONF}.bak.$(date +%Y%m%d%H%M%S)"
  cp -p "$CONF" "$BACKUP"
  ok "已备份到 ${BACKUP}"

  rm -f "$CONF"
  ok "已删除 ${CONF}"

  step "校验 nginx 配置"
  if ! nginx -t; then
    warn "删除后配置校验失败"
    if rollback_and_reload; then
      warn "已恢复 ${CONF} 并重新加载"
    else
      warn "已恢复 ${CONF}，但 reload 失败，请人工检查"
    fi
    exit 1
  fi
  ok "配置校验通过"

  step "重载 nginx"
  if ! reload_nginx; then
    fail_after_install "卸载" "reload 失败"
  fi
  ok "已重载"

  step "回归自测：api 站点不受影响"
  # 卸载同样触发 reload，同样存在生效时序问题，所以复用同一个轮询逻辑。
  if ! wait_for "$API_DOMAIN" "$API_HEALTH_PATH" check_api_health; then
    warn "https://${API_DOMAIN}${API_HEALTH_PATH} 在 ${POLL_ATTEMPTS} 次尝试内未返回 200，最后一次结果："
    print_diagnosis "api 健康检查" "$API_DOMAIN" "$API_HEALTH_PATH"
    fail_after_install "卸载" "api 回归自测超时未通过"
  fi
  ok "api ${API_HEALTH_PATH} → 200"

  rm -f "$BACKUP"
  BACKUP=""

  cat <<DONE

====================================================
H5 站点已卸载

  已删除 ${CONF}
  api 站点未受影响（${API_DOMAIN}${API_HEALTH_PATH} → 200）
  静态文件 /opt/task-backend/web 仍在，未被删除。
====================================================
DONE
  exit 0
fi

# ---------- 安装 ----------

step "检查源配置"
[ -e "$SRC" ] || die "源文件不存在：${SRC}"
# 用 if 而不是 `[ -L ] && die`：后者在条件不成立时返回 1，
# set -e 下会让脚本直接退出。
if [ -L "$SRC" ]; then
  die "源文件是符号链接，拒绝安装：${SRC}"
fi
[ -f "$SRC" ] || die "源文件不是普通文件：${SRC}"
[ -r "$SRC" ] || die "源文件不可读：${SRC}"
[ -s "$SRC" ] || die "源文件是空文件：${SRC}"
ok "${SRC}"

# 确认它确实是本项目的 H5 站点配置，而不是被替换成了别的东西。
grep -q 'hahahahaha.party' "$SRC" \
  || die "源文件里没有出现 hahahahaha.party，疑似不是 H5 站点配置"
ok "内容标识正确"

step "检查静态目录"
[ -d /opt/task-backend/web ] \
  || die "静态目录 /opt/task-backend/web 不存在（先让 CI 同步 web/）"
[ -f /opt/task-backend/web/index.html ] \
  || die "/opt/task-backend/web/index.html 不存在，根域没有入口页"
ok "index.html 就位"

step "校验证书与私钥（root 权限，deploy 用户做不到这一步）"
# 路径从 deploy/h5.conf 里解析，而不是写死在脚本里 —— 否则改了 h5.conf 的证书
# 路径再重跑安装，这里校验的仍是旧证书。自测用 curl -k 跳过了证书链，也发现不了
# 这种不一致，所以解析必须与源配置同源。
#
# 解析规则：剥掉 # 注释，匹配首词完全等于指令名（避免 ssl_certificate 吃掉
# ssl_certificate_key），取第二个字段，去掉行尾分号和引号，容忍前导空白与多空格。
parse_directive() {
  local file="$1" want="$2"
  sed -e 's/#.*$//' "$file" \
    | awk -v n="$want" '
        { sub(/^[[:space:]]+/, ""); sub(/[[:space:]]+$/, "") }
        $0 == "" { next }
        {
          if (index($0, n) != 1) next
          rest = substr($0, length(n) + 1)
          if (rest !~ /^[[:space:]]/) next
          sub(/^[[:space:]]+/, "", rest)
          sub(/;.*$/, "", rest)
          gsub(/["\047]/, "", rest)
          sub(/[[:space:]]+$/, "", rest)
          print rest
          exit
        }'
}

CERT="$(parse_directive "$SRC" ssl_certificate)"
KEY="$(parse_directive "$SRC" ssl_certificate_key)"

[ -n "$CERT" ] || die "h5.conf 里缺少 ssl_certificate：${SRC}"
[ -n "$KEY" ]  || die "h5.conf 里缺少 ssl_certificate_key：${SRC}"
info "证书: ${CERT}"
info "私钥: ${KEY}"

[ -f "$CERT" ] || die "证书不存在：${CERT}"
[ -r "$CERT" ] || die "证书不可读：${CERT}"
[ -f "$KEY" ]  || die "私钥不存在：${KEY}"
[ -r "$KEY" ]  || die "私钥不可读：${KEY}"

if ! diff -q <(openssl x509 -in "$CERT" -noout -pubkey 2>/dev/null) \
             <(openssl pkey -in "$KEY" -pubout 2>/dev/null) >/dev/null 2>&1; then
  die "证书与私钥不配对：${CERT} / ${KEY}"
fi
ok "证书与私钥配对正常"

step "备份并安装"
if [ -f "$CONF" ]; then
  BACKUP="${CONF}.bak.$(date +%Y%m%d%H%M%S)"
  cp -p "$CONF" "$BACKUP"
  ok "已备份到 ${BACKUP}"
else
  info "${CONF} 尚不存在，首次安装"
fi

install -m 644 -o root -g root "$SRC" "$CONF"
ok "已安装 ${CONF}"

step "校验 nginx 配置"
if ! nginx -t; then
  # 还没 reload，线上不受影响；恢复后直接退出，不 reload。
  warn "配置校验失败，nginx 未重载"
  if [ -n "$BACKUP" ]; then
    mv -f "$BACKUP" "$CONF"
    BACKUP=""
    warn "已恢复原有 ${CONF}"
  else
    rm -f "$CONF"
    warn "已删除新建的 ${CONF}"
  fi
  warn "请检查上面的 nginx 报错；常见原因：证书路径写错、端口被占用、server_name 冲突"
  exit 1
fi
ok "配置校验通过"

step "重载 nginx"
if ! reload_nginx; then
  fail_after_install "安装" "reload 失败，新配置未生效"
fi
ok "已重载"

step "本机自测（--resolve 钉到 127.0.0.1，绕过 DNS 与 Cloudflare）"
# reload 是异步的（systemd Type=forking + nginx -s reload 发完 HUP 即返回），
# 所以这里轮询等待新配置真正生效，而不是 reload 一返回就判定失败。
info "轮询等待新配置生效（最多 $((POLL_ATTEMPTS)) 次 × ${POLL_INTERVAL}s ≈ 10 秒）"

if ! wait_for "$ROOT_DOMAIN" "/" check_root_page; then
  warn "https://${ROOT_DOMAIN}/ 在 ${POLL_ATTEMPTS} 次尝试内未达到预期（200 且含 <html），最后一次结果："
  print_diagnosis "根域" "$ROOT_DOMAIN" "/"
  fail_after_install "安装" "根域自测超时未通过"
fi
ok "根域 ${ROOT_DOMAIN} → 200 且含 <html"

if ! wait_for "$API_DOMAIN" "$API_HEALTH_PATH" check_api_health; then
  warn "https://${API_DOMAIN}${API_HEALTH_PATH} 在 ${POLL_ATTEMPTS} 次尝试内未返回 200，最后一次结果："
  print_diagnosis "api 健康检查" "$API_DOMAIN" "$API_HEALTH_PATH"
  fail_after_install "安装" "api 自测超时未通过（api 站点可能受影响）"
fi
ok "api ${API_DOMAIN}${API_HEALTH_PATH} → 200"

# ---- 全部通过：删掉备份，并顺手清理历史残留 ----
if [ -n "$BACKUP" ]; then
  rm -f "$BACKUP"
  BACKUP=""
  info "自测通过，已删除本次备份"
fi

# 清理 7 天前的同类残留备份（历史上异常中断可能留下的）。
find "$CONF_DIR" -maxdepth 1 -name "${SITE_NAME}.conf.bak.*" -type f -mtime +7 -delete 2>/dev/null || true

cat <<DONE

====================================================
页面已上线

  https://${ROOT_DOMAIN}/
  配置:     ${CONF}
  静态目录: /opt/task-backend/web
  api 站点: ${API_DOMAIN}${API_HEALTH_PATH} → 200（未受影响）

以后只 push 即可
--------------------------------------------
改 web/ 里的内容 → commit/push，CI 会把 web/ 同步到
/opt/task-backend/web，nginx 直接读文件。不需要再登录服务器，
也不需要 root。

只有在改动下面这些时才需要改 deploy/h5.conf 并重跑一次：
  - 域名（server_name）
  - 证书 / 私钥路径
  - 缓存策略、安全头、监听端口

卸载：sudo bash deploy/install-h5-site.sh --remove
====================================================
DONE
