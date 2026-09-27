# frp 客户端 + SSH 隧道（本机）启动/停止脚本
#
# 把本机 8110 反向暴露到香港 VPS，供 VPS 上的 nginx 转发。
#
# ## 为什么是「两条进程」
#
# 腾讯云安全组（`YJ-FIREWALL-INPUT`）只放行了 **22 和 80**，
# 443/7000 一律超时（实测）。所以 frp 的控制连接**不直连 7000**，
# 而是走已经通的 22 端口：
#
#     frpc → 127.0.0.1:17000 ──(SSH 本地转发)──> VPS 127.0.0.1:7000 (frps)
#
# 于是本机有**两个**必须同时活着的进程：
#
#     frp_ssh_tunnel.py   SSH 隧道（127.0.0.1:17000 → VPS:7000）
#     frpc.exe            frp 客户端（把 8110 暴露成 VPS:18110）
#
# 只起一个都不通，而「谁没起来」在界面上表现完全一样（打不开）——
# 所以本脚本把两者一起管，并在 -Status 里分别报告。
#
# 用法：
#     powershell -NoProfile -ExecutionPolicy Bypass -File scripts\frpc_start.ps1
#     powershell -NoProfile -ExecutionPolicy Bypass -File scripts\frpc_start.ps1 -Stop
#     powershell -NoProfile -ExecutionPolicy Bypass -File scripts\frpc_start.ps1 -Status

param(
    [switch]$Stop,
    [switch]$Status
)

$ErrorActionPreference = 'Continue'
$proj = Split-Path -Parent $PSScriptRoot
$frpc = Join-Path $proj 'bin\frpc.exe'
$conf = Join-Path $proj 'frpc.toml'
$tunnelPy = Join-Path $proj 'scripts\frp_ssh_tunnel.py'
$py = Join-Path $proj '.venv\Scripts\python.exe'
$logDir = Join-Path $proj 'data\run'
$entryUrl = 'http://43.128.5.94/api/v1/health/live'

function Get-FrpcProcs {
    Get-CimInstance Win32_Process -Filter "Name = 'frpc.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -match 'frpc' }
}

function Get-TunnelProcs {
    Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -match 'frp_ssh_tunnel' }
}

function Test-TunnelPort {
    try {
        $c = New-Object System.Net.Sockets.TcpClient
        $t = $c.ConnectAsync('127.0.0.1', 17000)
        $ok = $t.Wait(2000)
        $c.Close()
        return $ok
    } catch { return $false }
}

if ($Status) {
    Write-Host '=== frp 链路状态 ==='
    $tp = Get-TunnelProcs
    if ($tp) { $tp | ForEach-Object { "  SSH 隧道   运行中 PID $($_.ProcessId)" } }
    else { Write-Host '  SSH 隧道   未运行' -ForegroundColor Yellow }
    Write-Host "  本地 17000 : $(if (Test-TunnelPort) { '在监听' } else { '未监听' })"

    $fp = Get-FrpcProcs
    if ($fp) { $fp | ForEach-Object { "  frpc       运行中 PID $($_.ProcessId)" } }
    else { Write-Host '  frpc       未运行' -ForegroundColor Yellow }

    Write-Host ''
    Write-Host '=== 配置 ==='
    if (Test-Path $conf) {
        $confText = Get-Content $conf -Raw
        $addr = [regex]::Match($confText, 'serverAddr\s*=\s*"([^"]+)"').Groups[1].Value
        $sport = [regex]::Match($confText, 'serverPort\s*=\s*(\d+)').Groups[1].Value
        $rport = [regex]::Match($confText, 'remotePort\s*=\s*(\d+)').Groups[1].Value
        Write-Host "  serverAddr:port = $addr`:$sport   remotePort = $rport"
        if ($confText -match 'REPLACE_ME') {
            Write-Host '  仍有 REPLACE_ME 占位符' -ForegroundColor Red
        }
    } else { Write-Host "  缺 $conf" -ForegroundColor Red }

    Write-Host ''
    Write-Host '=== 日志尾部 ==='
    foreach ($n in 'frp_tunnel.log', 'frpc.log') {
        Write-Host "  --- $n ---"
        $l = Join-Path $logDir $n
        if (Test-Path $l) { Get-Content $l -Tail 5 | ForEach-Object { "    $_" } }
        else { Write-Host '    （无）' }
    }

    Write-Host ''
    Write-Host '=== 公网验证 ==='
    $r = curl.exe -s -o NUL --max-time 15 -w "%{http_code} %{time_total}s" $entryUrl
    Write-Host "  $entryUrl  ->  $r"
    exit 0
}

if ($Stop) {
    $any = $false
    foreach ($p in (Get-FrpcProcs)) {
        Write-Host "  停止 frpc PID $($p.ProcessId)"; $any = $true
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
    }
    foreach ($p in (Get-TunnelProcs)) {
        Write-Host "  停止 SSH 隧道 PID $($p.ProcessId)"; $any = $true
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
    }
    if (-not $any) { Write-Host 'frp 链路本来就没在运行' }
    Write-Host '已停止（香港入口不可用；CF 隧道不受影响）'
    exit 0
}

# ---- 启动 ----
foreach ($f in @($py, $tunnelPy, $frpc, $conf)) {
    if (-not (Test-Path $f)) {
        Write-Host "找不到 $f" -ForegroundColor Red
        if ($f -eq $frpc) {
            Write-Host '  从 https://github.com/fatedier/frp/releases 下载 windows_amd64 版，'
            Write-Host '  把 frpc.exe 放到项目 bin\ 目录'
        }
        exit 1
    }
}
if ((Get-Content $conf -Raw) -match 'REPLACE_ME') {
    Write-Host '配置里仍有 REPLACE_ME 占位符，请先填写' -ForegroundColor Red
    exit 1
}

New-Item -ItemType Directory -Force -Path $logDir | Out-Null

# ① SSH 隧道（frpc 依赖它才能连到 VPS 的 7000）
#
# ⚠️ 判据用**端口**而不是"进程在不在"：
#    隧道进程可能因为绑不上 17000 而在重试循环里活着（端口已被占），
#    此时"有进程"却是**不通**的。以端口为准才不会自欺。
if (Test-TunnelPort) {
    Write-Host '① SSH 隧道已就绪（127.0.0.1:17000 在监听）'
    # 清理重复的隧道进程：只留一个，避免多个进程抢 17000 反复重连
    $tps = @(Get-TunnelProcs)
    if ($tps.Count -gt 1) {
        Write-Host "   发现 $($tps.Count) 个隧道进程，清理多余的…"
        $tps | Select-Object -Skip 1 | ForEach-Object {
            Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        }
    }
} else {
    # 端口没就绪 → 先清掉可能残留的重试进程，再起一个干净的
    foreach ($p in (Get-TunnelProcs)) {
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Seconds 1
    $env:PYTHONIOENCODING = 'utf-8'
    Start-Process -FilePath $py -ArgumentList $tunnelPy `
        -WorkingDirectory $proj -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $logDir 'frp_tunnel.log') `
        -RedirectStandardError (Join-Path $logDir 'frp_tunnel.err') | Out-Null
    Start-Sleep -Seconds 6
    if (Test-TunnelPort) {
        Write-Host '① SSH 隧道已启动（127.0.0.1:17000 就绪）'
    } else {
        Write-Host '① SSH 隧道未能就绪，看 data\run\frp_tunnel.log' -ForegroundColor Red
        Get-Content (Join-Path $logDir 'frp_tunnel.log') -Tail 8 -ErrorAction SilentlyContinue |
            ForEach-Object { "     $_" }
        exit 1
    }
}

# ② frpc（把本机 8110 暴露成 VPS:18110）
if (Get-FrpcProcs) {
    Write-Host '② frpc 已在运行'
} else {
    Start-Process -FilePath $frpc -ArgumentList '-c', $conf `
        -WorkingDirectory $proj -WindowStyle Hidden | Out-Null
    Start-Sleep -Seconds 6
    $tail = Get-Content (Join-Path $logDir 'frpc.log') -Tail 25 -ErrorAction SilentlyContinue
    if ($tail | Select-String -Pattern 'start proxy success' -Quiet) {
        Write-Host '② frpc 已启动，代理注册成功'
    } else {
        Write-Host '② 未见 "start proxy success"，日志尾部：' -ForegroundColor Yellow
        $tail | Select-Object -Last 6 | ForEach-Object { "     $_" }
    }
}

Write-Host ''
Write-Host '=== 公网验证 ==='
$r = curl.exe -s -o NUL --max-time 15 -w "%{http_code} %{time_total}s" $entryUrl
Write-Host "  $entryUrl  ->  $r"
Write-Host '  （期望 200；502 = frpc 没连上）'
