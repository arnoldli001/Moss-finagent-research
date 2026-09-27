# frp 链路（SSH 隧道 + frpc）值守包装
#
# 由计划任务 `MossFrpEnsure` 周期调用（每 5 分钟一次），也可手工运行。
#
# ## 它在整条链路里的位置
#
#     浏览器 → 香港 VPS nginx(443) → frps(18110) → frpc → SSH隧道 → 本机 8110
#
# 本机有**两个**必须同时活着的进程：
#
#     frp_ssh_tunnel.py   SSH 隧道（本机 17000 → VPS 127.0.0.1:7000，走 22 端口）
#     frpc.exe            把本机 8110 暴露成 VPS 18110
#
# 只起一个都不通，而"谁没起来"在用户那侧表现完全一样（打不开）——
# 所以这个包装把两者一起确保。
#
# ## ⚠️ 判据只用「公网入口」，不用「端口是否在听」（2026-09-27 重写）
#
# 旧版先看 `Test-TunnelPort`（17000 是否 LISTEN），那是个**假判据**：
# 隧道进程可以活着、端口也在听，但它的 SSH transport 已经烂掉 ——
# 此时 frpc 能连上 17000、TCP 也能建立，可请求永远等不到响应。
# 外部表现：TCP/TLS 全部正常，**首字节 20 秒不来**（很像后端挂了，其实不是）。
# 端口在听 → 旧版判定"健康" → 什么都不做 → **永远修不好**。
#
# 所以判据改成唯一有外部意义的那个：**公网 HTTPS 入口能否拿到 200**。
#
# ## ⚠️ 为什么是「全杀重建」而不是「找出坏的那个」
#
# 实测踩过两次：**无法从外部判断哪个隧道进程真正绑住了 17000**。
# 启动顺序、重试循环、端口释放时机都会影响结论，我先后按 PID 顺序
# 猜了两次，两次都留下了错的那个，把本来好的链路弄断。
#
# 也无法靠"数进程"判断重复：Windows 上 Python 启动会产生**父子进程对**
# （包装 + 实际工作，与 uvicorn 同理），所以"两个进程"是**正常形态**。
#
# 所以只有两种动作：健康 → 什么都不做；不健康 → 全部回收重建，不猜。
# （重复实例本身已由 frp_ssh_tunnel.py 的 SO_EXCLUSIVEADDRUSE 从源头堵死。）
#
# ## 设计约束
#
# 1. **健康时完全静默且零副作用**：绝大多数 tick 都健康，一有输出就把日志刷成噪音。
# 2. **启动必须脱离本进程**：用 `Win32_Process.Create` 而不是
#    `Start-Process` —— 后者创建的进程与调用者的句柄绑定，
#    调用者的宿主（shell / AI 工具调用）一退出，子进程会被带走。
#    实测：由工具调用启动的隧道在调用结束后立刻消失，表现是"网站 502"。
# 3. **后端自己不健康时不动隧道**：否则每 5 分钟做一次无效重启，纯属折腾。
#    后端归 `MossPilotWatchdog`（manage.py ensure）管。
$ErrorActionPreference = 'Continue'
$proj = 'D:\code\Moss-finagent-research'
$py = Join-Path $proj '.venv\Scripts\python.exe'
$tunnelPy = Join-Path $proj 'scripts\frp_ssh_tunnel.py'
$frpc = Join-Path $proj 'bin\frpc.exe'
$conf = Join-Path $proj 'frpc.toml'
$logDir = Join-Path $proj 'data\run'
$watchLog = Join-Path $logDir 'frp-watchdog.log'

# ★ 必须是 https://hk.wujiaitool.cn，**不能再写 http://43.128.5.94**：
#   2026-09-27 nginx 的 80 端口改成 301 跳 HTTPS 之后，旧地址恒返回 301，
#   于是每一次 tick 都记一条"公网入口返回 301（期望 200）"的**假故障**。
$entry = 'https://hk.wujiaitool.cn/api/v1/health/live'
$backend = 'http://127.0.0.1:8110/api/v1/health/live'

# ── ⚠️ 别再试图用「改运行身份」或「-WindowStyle Hidden」来消灭弹窗 ──
#
# 2026-09-27 两条路都实测过，**都失败**，留此记录免得后人重走：
#
#   ① 任务保持 Interactive，只加 `-WindowStyle Hidden`
#      控制台窗口是操作系统在 CreateProcess 里分配的，PowerShell 要等自己
#      被加载起来之后才有机会去隐藏它。5 分钟被动监视抓到的现场：
#         pid=15604 powershell  标题='C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe'
#      标题还是 exe 全路径 ⇒ 尚在启动瞬间 ⇒ 用户确实看得见。每 5 分钟闪一次。
#
#   ② 把任务改成 `UserId=SYSTEM`（会话 0，无桌面 ⇒ 窗口物理上不可见）
#      窗口确实没了，但代价不可接受：
#        · `Path.home()` 变成 `...\config\systemprofile`，找不到管理员装的
#          SSH 密钥 → 隧道退出 → 入口 502（实测日志：
#          `[tunnel] 退出：没有可用凭据：既没有密钥 C:\Windows\system32\config\systemprofile\.ssh\...`）
#        · `Win32_Process.Create` 的子进程由 WMI 提供者创建，**不继承本进程
#          的环境变量**，所以在脚本里设 `$env:VPS_KEY` 根本传不下去
#        · 该次运行实际卡住，任务 `LastTaskResult=267009 (0x41301 正在运行)`
#
# ★ 现方案：任务保持 Administrator/Interactive，改由
#   `scripts/frp_watchdog_hidden.vbs` 启动本脚本 —— wscript.exe 是 GUI 子系统
#   程序、没有控制台，`Run(cmd, 0, ...)` 在 CreateProcess 阶段就带 SW_HIDE，
#   窗口**从未可见过**（而不是"先建出来再藏起来"）。
#
# 密钥路径的健壮性另在 `frp_ssh_tunnel.py` 的 `_candidate_keys()` 里解决
# （不依赖运行身份，也就不必在这里设环境变量）。

function Test-TunnelPort {
    # ⚠️ 只用于"刚拉起来没有"，**不是健康判据**（理由见文件头）。
    try {
        $c = New-Object System.Net.Sockets.TcpClient
        $t = $c.ConnectAsync('127.0.0.1', 17000)
        $ok = $t.Wait(2000)
        $c.Close()
        return $ok
    } catch { return $false }
}

function Probe([string]$url) {
    return (curl.exe -s -o NUL --max-time 20 -w "%{http_code}" $url)
}

function Start-Detached([string]$cmd, [string]$cwd) {
    # Win32_Process.Create 起的进程不属于本 shell 的作业对象，父进程退出后仍在。
    #
    # ★ 但**必须显式带 CreateFlags**：WMI 的 Create 默认按"新控制台"创建进程，
    #   于是每次重建隧道都会在用户桌面上弹黑窗口（python + frpc 各一个）。
    #   实测（2026-09-27）：不带 flags 时子进程 MainWindowHandle=19270988（可见），
    #   带 flags 后 MainWindowHandle=0（不可见）。
    #
    # 用 CREATE_NO_WINDOW(0x08000000) | CREATE_NEW_PROCESS_GROUP(0x00000200)：
    #   · NO_WINDOW         —— 不分配可见控制台（隧道与 frpc 都自己写文件日志）
    #   · NEW_PROCESS_GROUP —— 保住"父进程退出后仍存活"这一条（本文件存在的理由）
    #
    # ⚠️ 不要用 DETACHED_PROCESS：本仓库
    #    `tests/unit/test_manage_no_console_window.py` 实测过，
    #    DETACHED 会**抵消** NO_WINDOW，照样弹窗。
    try {
        $startup = New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly `
            -Property @{ CreateFlags = [uint32]0x08000200 } -ErrorAction Stop
    } catch {
        $startup = ([wmiclass]'Win32_ProcessStartup').CreateInstance()
        $startup.CreateFlags = [uint32]0x08000200
    }
    $r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
        CommandLine = $cmd; CurrentDirectory = $cwd; ProcessStartupInformation = $startup
    }
    return $r.ReturnValue
}

function Stop-AllTunnels {
    # 只杀"命令行里带 frp_ssh_tunnel.py"的 python.exe。
    #
    # ⚠️ 两个坑：
    #   1. 用 `-Filter "Name = 'python.exe'"` 而不是对全表 -match：
    #      否则**本函数自己的命令行**（排障用的 powershell）也会命中，
    #      把正在诊断的进程一起杀掉。
    #   2. 绝不能碰 frpc —— 那是另一个进程、另一套判据。
    $killed = @()
    Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine.Contains('frp_ssh_tunnel.py') } |
        ForEach-Object {
            try {
                Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop
                $killed += $_.ProcessId
            } catch { }
        }
    return $killed
}

New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$notes = @()

# ── ① 先看公网入口：健康则零副作用退出 ──
$code = Probe $entry
if ($code -eq '200') { exit 0 }

# ── ② 后端自己不健康 → 不碰隧道（否则是无意义的每 5 分钟重启）──
$bcode = Probe $backend
if ($bcode -ne '200') {
    Add-Content -Path $watchLog -Encoding UTF8 -Value ("{0} 本机后端 8110 返回 {1}，跳过隧道处置（归 PilotWatchdog）" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $bcode)
    exit 0
}

$notes += "公网入口返回 $code"

# ── ③ 全量回收：隧道 + frpc 一起重建 ──
$killedTunnel = Stop-AllTunnels
Get-Process frpc -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2

$rc = Start-Detached "`"$py`" `"$tunnelPy`"" $proj
for ($i = 0; $i -lt 10; $i++) {
    Start-Sleep -Seconds 2
    if (Test-TunnelPort) { break }
}
if (-not (Test-TunnelPort)) {
    $notes += "❌ SSH 隧道拉起失败（旧进程 $($killedTunnel.Count) 个，Create=$rc；见 frp_tunnel.log）"
} else {
    $notes += "SSH 隧道已重建（旧进程 $($killedTunnel.Count) 个，Create=$rc）"
    if (Test-Path $frpc) {
        $rc2 = Start-Detached "`"$frpc`" -c `"$conf`"" $proj
        Start-Sleep -Seconds 8
        $tail = Get-Content (Join-Path $logDir 'frpc.log') -Tail 30 -ErrorAction SilentlyContinue
        if ($tail | Select-String -Pattern 'start proxy success' -Quiet) {
            $notes += "frpc 已重建并注册成功（Create=$rc2）"
        } else {
            $notes += '⚠️ frpc 拉起后未见 start proxy success（稍后自动重试）'
        }
    } else {
        $notes += '❌ 找不到 bin\frpc.exe'
    }
}

# ── ④ 复检公网入口：这才是"修好了没有"的唯一答案 ──
#
# ⚠️ 必须有界重试，不能探一次就下结论。frpc 注册成功后，VPS 侧 nginx
#    到 frps 的可用性还有几秒的滞后；只探一次会把"正在恢复"误判成
#    "没修好"，于是下个 tick 又做一次全量回收 —— 变成自造抖动。
$code2 = '000'
for ($i = 0; $i -lt 4; $i++) {
    $code2 = Probe $entry
    if ($code2 -eq '200') { break }
    Start-Sleep -Seconds 5
}
if ($code2 -eq '200') {
    $notes += '复检公网入口 200，已恢复'
} else {
    $notes += "❌ 复检公网入口仍为 $code2（已重试 4 次）"
}

# 只在有事发生时写日志
Add-Content -Path $watchLog -Encoding UTF8 `
    -Value ("{0} {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), ($notes -join '；'))
exit 0
