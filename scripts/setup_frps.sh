#!/usr/bin/env bash
# ============================================================================
# 香港 VPS 一键部署：frps + nginx（TLS 终止）反向代理
#
# 用途：把 `hk.<你的域名>` 的 HTTPS 请求，经 frp 隧道转发到你本机的 8110。
#       本机项目、模型、数据库**全部不动**；VPS 上只有 frps 与 nginx。
#
# 用法（在香港 VPS 上，root 执行）：
#     bash setup_frps.sh --domain hk.example.com --token '<一个长随机串>'
#
# 之后在你本机跑 frpc（见 scripts/frpc.toml 与 docs/HK_VPS_MIGRATION.md）。
#
# ## 三件必须做对的事（每一件错了都不报错，只是不好用/不安全）
#
# 1. **frps 的 token 必须强**：它是隧道入口。token 泄露 = 别人能把你
#    VPS 上的端口暴露到公网（或直接劫持你的流量）。
# 2. **只暴露 frp 的一个绑定端口，且只绑 127.0.0.1**：frp 默认会把
#    remotePort 绑到 0.0.0.0，那等于给公网开了一个**没有 TLS 的**后门。
#    nginx 才是唯一的公网入口。
# 3. **nginx 必须补 X-Forwarded-For**：否则后端把所有用户看成本机地址，
#    登录限流/图形码判定全部失效（见 src/core/client_ip.py 的说明）。
# ============================================================================
set -euo pipefail

DOMAIN=""
TOKEN=""
FRP_VERSION="0.61.0"
EMAIL=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --domain) DOMAIN="$2"; shift 2 ;;
    --token)  TOKEN="$2";  shift 2 ;;
    --email)  EMAIL="$2";  shift 2 ;;
    --frp-version) FRP_VERSION="$2"; shift 2 ;;
    *) echo "未知参数: $1" >&2; exit 2 ;;
  esac
done

[[ -n "$DOMAIN" ]] || { echo "必须提供 --domain" >&2; exit 2; }
[[ ${#TOKEN} -ge 32 ]] || { echo "❌ --token 至少 32 位（它是隧道入口凭据）" >&2; exit 2; }

log() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }

# ---------------------------------------------------------------------------
log "1/7 系统更新与基础依赖"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq wget curl ufw nginx certbot python3-certbot-nginx \
  >/dev/null

# ---------------------------------------------------------------------------
log "2/7 安装 frps ${FRP_VERSION}"
ARCH="$(uname -m)"
case "$ARCH" in
  x86_64)  FRP_ARCH=amd64 ;;
  aarch64) FRP_ARCH=arm64 ;;
  *) echo "不支持的架构: $ARCH" >&2; exit 1 ;;
esac
TARBALL="frp_${FRP_VERSION}_linux_${FRP_ARCH}.tar.gz"
TMP="$(mktemp -d)"
wget -q -O "$TMP/$TARBALL" \
  "https://github.com/fatedier/frp/releases/download/v${FRP_VERSION}/${TARBALL}"
tar -xzf "$TMP/$TARBALL" -C "$TMP"
install -m 0755 "$TMP/frp_${FRP_VERSION}_linux_${FRP_ARCH}/frps" /usr/local/bin/frps
rm -rf "$TMP"

# ---------------------------------------------------------------------------
log "3/7 写 frps 配置"
# ⚠️ `proxyBindAddr = 127.0.0.1` 是**安全关键**：
#    frp 默认把 remotePort 绑到 0.0.0.0，于是"127.0.0.1:18110 上的明文 HTTP"
#    会同时暴露在公网 IP 的 18110 上 —— 绕过 nginx 的 TLS 直接明文访问。
mkdir -p /etc/frp
cat > /etc/frp/frps.toml <<EOF
# ★ 显式绑 IPv4 全地址（**不要删**）。
#
# 为什么不靠默认值：frps 的默认 bindAddr 会随系统是否启用 IPv6 而变。
# 如果这台机器将来开了 IPv6，而 frpc 仍用 IPv4 连 7000，症状就是
# **隧道连不上、但看不出原因**（地址族不匹配）。显式写死 = 只有一个变量。
#
# 将来真要改走 IPv6：把下面两行换成
#     bindAddr = "::"
# 并同步把这个 IP 加入 frpc.toml 的 serverAddr（用 [ ] 包起来，如 [2408:...]:7000）。
bindAddr = "0.0.0.0"
bindPort = 7000
# 只监听 IPv4 的 7000 供你本机连入；不额外开其它入口
transport.tls.force = true

auth.method = "token"
auth.token = "${TOKEN}"

# ★ 隧道落地的端口只绑本机，公网进不来（唯一入口是 nginx）
proxyBindAddr = "127.0.0.1"

# 允许客户端申请的端口范围（收窄，纵深防御）
allowPorts = [{ start = 18100, end = 18199 }]

log.to = "/var/log/frps.log"
log.level = "info"
log.maxDays = 7
EOF
chmod 600 /etc/frp/frps.toml

cat > /etc/systemd/system/frps.service <<'EOF'
[Unit]
Description=frp server (reverse tunnel entry)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/local/bin/frps -c /etc/frp/frps.toml
Restart=always
RestartSec=3
LimitNOFILE=1048576

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now frps >/dev/null
sleep 1
systemctl is-active --quiet frps || { echo "❌ frps 未起来，看 /var/log/frps.log"; exit 1; }

# ---------------------------------------------------------------------------
log "4/7 写 nginx 反向代理（TLS 终止 + 补 XFF）"
# 说明：先放一个 HTTP 站点，certbot 签完证书后它会自动改成 HTTPS。
cat > /etc/nginx/sites-available/moss-hk <<EOF
server {
    listen 80;
    listen [::]:80;
    server_name ${DOMAIN};

    # certbot 的 ACME 校验走这里
    location /.well-known/acme-challenge/ { root /var/www/html; }

    location / {
        proxy_pass http://127.0.0.1:18110;
        proxy_http_version 1.1;

        # ★★ 这两行是**功能必需**，不是可选项：
        #    · Host 必须保留：后端据此生成 Cookie 域与跳转
        #    · X-Forwarded-For 必须补：否则后端把所有用户看成本机地址，
        #      登录限流与图形码判定全部失效（所有人共用一个计数）
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;

        # WebSocket（告警推送 /ws/alerts 靠它，断掉会让实时通知失效）
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";

        # 情报流首屏可能现拉上游，给足超时（默认 60s 会在慢请求上 504）
        proxy_read_timeout 300s;
        proxy_send_timeout 300s;

        # 关掉缓冲：让响应尽快到达浏览器（我们传的是 JSON，不是大文件）
        proxy_buffering off;
    }
}
EOF
ln -sf /etc/nginx/sites-available/moss-hk /etc/nginx/sites-enabled/moss-hk
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl reload nginx

# ---------------------------------------------------------------------------
log "5/7 申请 Let's Encrypt 证书"
if [[ -n "$EMAIL" ]]; then
  certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos -m "$EMAIL" \
    --redirect >/dev/null 2>&1 || {
      echo "⚠️ 证书申请失败 —— 请确认 $DOMAIN 的 A 记录已指向本机 IP，"
      echo "   然后手动执行：certbot --nginx -d $DOMAIN"
    }
else
  echo "   未提供 --email，跳过证书申请。稍后手动跑："
  echo "   certbot --nginx -d $DOMAIN --agree-tos -m 你的邮箱 --redirect"
fi

# ---------------------------------------------------------------------------
log "6/7 防火墙（只留 SSH / 80 / 443 / frp）"
ufw --force reset >/dev/null
ufw default deny incoming >/dev/null
ufw default allow outgoing >/dev/null
ufw allow 22/tcp   comment 'SSH'        >/dev/null
ufw allow 80/tcp   comment 'HTTP(ACME)'>/dev/null
ufw allow 443/tcp  comment 'HTTPS'      >/dev/null
ufw allow 7000/tcp comment 'frp control'>/dev/null
ufw --force enable >/dev/null

# ---------------------------------------------------------------------------
log "7/7 自检"
echo "  frps      : $(systemctl is-active frps)"
echo "  nginx     : $(systemctl is-active nginx)"
echo "  监听端口  :"
ss -tlnp | grep -E ':(80|443|7000|181[0-9][0-9])\b' | sed 's/^/    /' || true
echo
echo "  ★ 确认 18110 只绑在 127.0.0.1（不能在 0.0.0.0 上）："
ss -tlnp | grep 18110 | sed 's/^/    /' || echo "    （尚未监听 —— 你本机 frpc 连上后才会出现，正常）"

cat <<EOF

============================================================
✅ VPS 侧完成。接下来在你**本机**配置 frpc：

   1) 复制 scripts/frpc.toml 到 D:\code\Moss-finagent-research\
   2) 把 serverAddr 改成这台 VPS 的公网 IP
   3) 把 auth.token 填成你刚才用的那个 token
   4) 用 scripts/frpc_start.ps1 启动

然后访问： https://${DOMAIN}
（DNS 需先把 ${DOMAIN} 的 A 记录指到本机公网 IP）

⚠️ 两件别忘了的安全收尾：
   · 改 SSH 端口 + 只允许密钥登录（见 docs/HK_VPS_MIGRATION.md 的加固清单）
   · 本文件里的 token 不要提交进 git（建议放 .env）
============================================================
EOF
