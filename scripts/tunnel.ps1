# 把对外试点实例（8110）通过 Cloudflare 临时隧道开到公网，并打印本次地址。
#
# ## 为什么是"临时隧道"（quick tunnel）
#
# 正式做法是**命名隧道 + 自己的域名 + Cloudflare Access 邮箱白名单**，
# 那样地址固定、且没在白名单里的人**连登录页都看不到**。
# 但它有一个硬前提：Access 策略必须挂在**你自己域名的子域**下。
# 现在还没有域名，所以先用临时隧道把链路跑通 —— 代价必须说清楚：
#
#   · 地址是随机的 `https://xxx.trycloudflare.com`，**每次重启都变**；
#   · **配不了 Access**（域名不是我们的），只有应用自己的登录在保护它 ——
#     拿到 URL 的任何人都能看到登录页并尝试撞库/注册；
#   · Cloudflare 对它**不做可用性承诺**（它启动时会自己声明这一点），
#     实测偶发 502。
#
# ## 为什么 502 多半不是隧道坏了
#
# 8110 的进程重启期间（约 10 秒），cloudflared 连不上 origin 就会返回 502，
# 日志形如：
#   ERR Unable to reach the origin service ... dial tcp 127.0.0.1:8110 ...
# 所以看到 502 先看 `manage.py status` 里 8110 在不在，再看本脚本的日志。
#
# 用法：
#   pwsh -File scripts\tunnel.ps1            # 前台运行，Ctrl+C 停止
#   pwsh -File scripts\tunnel.ps1 -Port 8110

param(
    [int]$Port = 8110,
    [string]$Cloudflared = "$env:LOCALAPPDATA\cloudflared\cloudflared.exe"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot

if (-not (Test-Path $Cloudflared)) {
    Write-Host "未找到 cloudflared：$Cloudflared" -ForegroundColor Red
    Write-Host "下载（免安装单文件）：" -ForegroundColor Yellow
    Write-Host "  Invoke-WebRequest https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe -OutFile `"$Cloudflared`""
    exit 1
}

# 先确认 origin 真的在监听 —— 否则隧道会起来但每个请求都 502，
# 而 502 看起来像"隧道坏了"，排查方向会跑偏。
$listening = (Test-NetConnection -ComputerName 127.0.0.1 -Port $Port `
    -InformationLevel Quiet -WarningAction SilentlyContinue)
if (-not $listening) {
    Write-Host "⚠️ 127.0.0.1:$Port 没有在监听 —— 隧道会返回 502。" -ForegroundColor Yellow
    Write-Host "   先启动：python manage.py start --env pilot --port $Port --daemon" -ForegroundColor Yellow
}

$logDir = Join-Path $root "data\run"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir "cloudflared.log"

Write-Host "▶ 隧道指向 http://127.0.0.1:$Port" -ForegroundColor Cyan
Write-Host "  日志：$log（里面的 trycloudflare.com 就是本次公网地址）" -ForegroundColor Cyan
Write-Host "  ⚠️ 临时隧道：地址每次重启都变，且无法配置 Access 邮箱白名单" -ForegroundColor Yellow

& $Cloudflared tunnel --url "http://127.0.0.1:$Port" --no-autoupdate 2>&1 |
    Tee-Object -FilePath $log -Append
