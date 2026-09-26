# 把对外试点实例（8110）挂到**自己的域名**上：命名隧道 + Cloudflare Access 邮箱白名单。
#
# ## 与 `tunnel.ps1`（临时隧道）的区别
#
# | | 临时隧道 `tunnel.ps1` | 命名隧道（本脚本） |
# |---|---|---|
# | 地址 | `https://xxx.trycloudflare.com`，**每次重启都变** | 固定 `https://moss.wujiaitool.cn` |
# | Access 白名单 | **配不了**（域名不是我们的） | 能配：没在白名单的人**连登录页都看不到** |
# | 可用性 | Cloudflare 不承诺，实测偶发 502 | 同样不承诺，但入口固定、可做边缘拦截 |
#
# ## 前置条件（缺一不可，缺了脚本会直接告诉你）
#
# 1. `wujiaitool.cn` **已托管到 Cloudflare**：在 Cloudflare 面板 Add a site，
#    拿到两个 NS 后到**阿里云域名控制台 → DNS 修改 → 自定义 DNS** 填进去，
#    等 Cloudflare 把域名标成 **Active**。
#    ⚠️ 只把域名注册好没用：实测 `wujiaitool.cn` 的 NS 还是
#    `dns17/dns18.hichina.com`（阿里云），Cloudflare 隧道**挂不上去** ——
#    `cfargotunnel.com` 只在自己托管的 zone 里生效。
# 2. `cloudflared tunnel login` 已执行过（会在 `~/.cloudflared/cert.pem` 留凭据）。
#    没执行过：`cloudflared tunnel login`，浏览器里选 wujiaitool.cn 并授权。
#
# ## Access 白名单（这步在面板上做，脚本只能提醒）
# Zero Trust → Access → Applications → Add an application → Self-hosted：
#   · Application domain: `moss.wujiaitool.cn`
#   · Policy: Action=Allow, Include=Emails, 填客户邮箱（按需加）
# 这样没在白名单里的访问者在**边缘**就被挡掉，连应用的登录页都拿不到。
#
# 用法：
#   pwsh -File scripts\tunnel-named.ps1                    # 首次会自动创建隧道并配 DNS
#   pwsh -File scripts\tunnel-named.ps1 -Hostname moss.wujiaitool.cn

param(
    [string]$Hostname = "moss.wujiaitool.cn",
    [string]$TunnelName = "moss-pilot",
    [int]$Port = 8110,
    [string]$Cloudflared = "$env:LOCALAPPDATA\cloudflared\cloudflared.exe"
)

$ErrorActionPreference = "Stop"
$cfDir = Join-Path $env:USERPROFILE ".cloudflared"
$cert = Join-Path $cfDir "cert.pem"
$root = Split-Path -Parent $PSScriptRoot

if (-not (Test-Path $Cloudflared)) {
    Write-Host "未找到 cloudflared：$Cloudflared" -ForegroundColor Red
    Write-Host "下载（免安装单文件）：" -ForegroundColor Yellow
    Write-Host "  Invoke-WebRequest https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe -OutFile `"$Cloudflared`""
    exit 1
}

if (-not (Test-Path $cert)) {
    Write-Host "还没有 Cloudflare 凭据（$cert）。先执行：" -ForegroundColor Red
    Write-Host "  `"$Cloudflared`" tunnel login" -ForegroundColor Yellow
    Write-Host "浏览器里选中 wujiaitool.cn 并授权，再重跑本脚本。" -ForegroundColor Yellow
    exit 1
}

# origin 不在监听时，隧道会起来但每个请求都 502（看起来像"隧道坏了"，排查会跑偏）
$listening = (Test-NetConnection -ComputerName 127.0.0.1 -Port $Port `
    -InformationLevel Quiet -WarningAction SilentlyContinue)
if (-not $listening) {
    Write-Host "⚠️ 127.0.0.1:$Port 没有在监听 —— 隧道会返回 502。" -ForegroundColor Yellow
    Write-Host "   先启动：python manage.py start --env pilot --port $Port --daemon" -ForegroundColor Yellow
}

# 1) 隧道存在就复用，不存在就创建（幂等：重复执行不会造出第二条）
$existing = & $Cloudflared tunnel list --output json 2>$null | ConvertFrom-Json
$tunnel = $existing | Where-Object { $_.name -eq $TunnelName } | Select-Object -First 1
if (-not $tunnel) {
    Write-Host "▶ 创建隧道 $TunnelName" -ForegroundColor Cyan
    & $Cloudflared tunnel create $TunnelName
    $existing = & $Cloudflared tunnel list --output json 2>$null | ConvertFrom-Json
    $tunnel = $existing | Where-Object { $_.name -eq $TunnelName } | Select-Object -First 1
}
if (-not $tunnel) { Write-Host "隧道创建失败" -ForegroundColor Red; exit 1 }
$tunnelId = $tunnel.id
Write-Host "  隧道 $TunnelName = $tunnelId" -ForegroundColor Cyan

# 2) 把域名指到隧道上（已存在时 cloudflared 会报"record already exists"，可忽略）
Write-Host "▶ 路由 DNS：$Hostname → $tunnelId" -ForegroundColor Cyan
& $Cloudflared tunnel route dns $TunnelName $Hostname

# 3) 写 config.yml（幂等覆盖，内容是"一份 ingress 清单"）
$config = Join-Path $cfDir "config.yml"
@"
# 由 scripts/tunnel-named.ps1 生成：对外试点实例（8110）→ $Hostname
tunnel: $tunnelId
credentials-file: $(Join-Path $cfDir "$tunnelId.json")

ingress:
  - hostname: $Hostname
    service: http://127.0.0.1:$Port
  - service: http_status:404
"@ | Set-Content -Path $config -Encoding UTF8
Write-Host "  写入 $config" -ForegroundColor Cyan

Write-Host ""
Write-Host "▶ 启动隧道（前台运行，Ctrl+C 停止）" -ForegroundColor Cyan
Write-Host "  地址：https://$Hostname" -ForegroundColor Green
Write-Host "  ⚠️ 别忘了 Access 白名单：Zero Trust → Access → Applications →" -ForegroundColor Yellow
Write-Host "     Self-hosted，域名填 $Hostname，Policy=Allow + Emails（客户邮箱）" -ForegroundColor Yellow

& $Cloudflared tunnel run $TunnelName
