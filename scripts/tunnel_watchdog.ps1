# 隧道（cloudflared）运行期值守包装 —— **按需值守**
#
# 由计划任务 `MossTunnelWatchdog` 周期调用（每 5 分钟一次）。
#
# ## ★ 2026-09-27 重写：从"守 CF 备用"改成"主用优先，按需修备用"
#
# ### 改之前错在哪
#
# 旧版无条件探测 `https://moss.wujiaitool.cn`（**Cloudflare 备用**），
# 连续失败就 `Restart-Service Cloudflared`。问题是：
#
#   1. 生产入口早在 2026-09-26 就换成了 **香港 frp**（`hk.wujiaitool.cn`），
#      CF 这条降级为备用（见 `docs/HK_VPS_MIGRATION.md` §9）。
#      于是这个值守**每 5 分钟去修一条没人用的路**。
#   2. CF 链路在本机这条宽带上**结构性劣化**（实测：3 次探测
#      `超时 / 200(5.1s) / 超时`；`cloudflared tunnel info` 连
#      `api.cloudflare.com` 都 `context deadline exceeded`；
#      IPv6 路径建连 1296ms vs IPv4 160~209ms，1400 字节包 100% 丢）。
#      **修不好** —— 这是大陆到 CF 免费版边缘的固有形态，不是配置错误。
#   3. 最坏的是副作用：重启 cloudflared 会**中断所有在途请求**，
#      而它修的那条路当时根本没在服务用户 —— 净效果是自造抖动。
#
# ### 改之后的判据（顺序不能换）
#
#   ① **主用入口 200** → 直接退出，**零副作用**（不探备用、不重启任何东西）。
#      这是绝大多数 tick 的形态，也是本次改动的全部意义。
#   ② 主用不通 + **本机后端也不通** → 不碰隧道，交给 `MossPilotWatchdog`
#      （与 `frp_watchdog.ps1` 同一条纪律：后端不健康时重启隧道是无意义折腾）。
#   ③ 主用不通 + **备用能通** → 什么都不做，只记一行。
#      备用仍算"随时可用"，不该因为主用抖动就去重启它。
#   ④ 主用不通 + **备用也不通** → 这时备用**真的被需要**，重启 cloudflared
#      （`Restart-Service`，让 SCM 管，绝不自己起进程 —— 见下方"两个实例"教训）。
#
#   一句话：**只有"主用挂了、备用也挂了"才动手**。
#
# ### 它仍然不做的事
#
#   · **不改隧道配置**：换线路需要决策与主机，不在自动化范围。
#   · **不动香港那条路**：那是 `MossFrpEnsure` / `frp_watchdog.ps1` 的职责。
#   · **不重启后端**：那是 `MossPilotWatchdog` / `manage.py ensure` 的职责。
#
# ## ★★ 绝不能自己 `taskkill` + `Start-Process`（2026-09-26 实测事故）
#
# 本机 cloudflared 是**以 Windows 服务方式运行**的：
#
#     Cloudflared 服务  state=Running  StartMode=Auto
#     SERVICE_START_NAME = LocalSystem
#     FAILURE_ACTIONS    = RESTART -- Delay = 20000 milliseconds
#
# 也就是说 **SCM 自己会在进程挂掉 20 秒后把它拉起来**。旧版做的是
# "taskkill 全部 cloudflared + 自己 Start-Process 一个"，于是形成死循环：
#
#     我杀掉服务进程 → SCM 20 秒后拉起一个（PID A）
#                    → 我又起一个（PID B）＝ **两个实例**
#
# 两个实例各自向 CF 注册连接器，互相抢同一个隧道 —— 不但没修好，
# 还让"重启"本身成了不稳定源。
#
# **正确做法：只用 `Restart-Service` / `Start-Service`，让 SCM 去管。**
#
# ## 设计约束
#
# 1. **健康时必须完全静默**：绝大多数 tick 都健康，一有输出就把日志刷成噪音。
# 2. **恢复动作记在 `data/run/tunnel_incidents.jsonl`**（含内存现场），
#    不在本日志里重复记。
# 3. 只在**主用与备用同时不可用**（即真的动手了，或该动手却失败）时写一行。
$ErrorActionPreference = 'Continue'
$proj = 'D:\code\Moss-finagent-research'
$logDir = Join-Path $proj 'data\run'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$watchLog = Join-Path $logDir 'tunnel-watchdog.log'
$lockPath = Join-Path $logDir '.tunnel-watchdog.lock'

# ── 两个入口（顺序即优先级：主用在前）──
#
# ★ 主用必须是 https 的**域名**，不能写 IP：VPS 的 80 端口已改成 301 跳 HTTPS，
#   写 http://43.128.5.94 会恒返回 301，每次都记一条假故障（`frp_watchdog.ps1`
#   在 2026-09-27 踩过这个坑，这里直接照抄它的结论）。
$primaryUrl = 'https://hk.wujiaitool.cn/api/v1/health/live'     # 香港 frp（生产）
$backupUrl = 'https://moss.wujiaitool.cn/api/v1/health/live'    # Cloudflare 隧道（备用）
$backendUrl = 'http://127.0.0.1:8110/api/v1/health/live'        # 本机后端（判因用）

function Probe([string]$url) {
    # 4xx 也算"链路通"：能拿到业务错误码说明请求走完了整条路。
    # 与 `manage.py cmd_tunnel_ensure` 的判据保持一致（那里有同样的注释）。
    $code = (curl.exe -s -o NUL --max-time 15 -w "%{http_code}" $url 2>$null)
    if ($code -match '^4\d\d$') { return '200' }
    return $code
}

# 单实例锁：上一轮没跑完（探测 + 重启 + 等建连可能要 40 秒以上）时下一轮直接退出
$lock = $null
try {
    $lock = [IO.File]::Open($lockPath, 'OpenOrCreate', 'ReadWrite', 'None')
} catch {
    exit 0
}

try {
    # ── ① 主用入口健康 → 零副作用退出（本次改动的核心）──
    if ((Probe $primaryUrl) -eq '200') { exit 0 }

    $stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'

    # ── ② 主用不通，但后端自己也不通 → 不碰隧道 ──
    if ((Probe $backendUrl) -ne '200') {
        Add-Content -Path $watchLog -Encoding UTF8 `
            -Value "$stamp 主用不通且本机后端 8110 也不通，跳过隧道处置（归 PilotWatchdog）"
        exit 0
    }

    # ── ③ 主用不通，备用还活着 → 记一行，什么都不做 ──
    if ((Probe $backupUrl) -eq '200') {
        Add-Content -Path $watchLog -Encoding UTF8 `
            -Value "$stamp 主用入口（hk）不通，但 CF 备用可用，未处置（待 FrpEnsure 修主用）"
        exit 0
    }

    # ── ④ 主用与备用同时不通 → 备用真的被需要，重启 cloudflared ──
    $svc = 'Cloudflared'
    $svcState = (Get-Service -Name $svc -ErrorAction SilentlyContinue).Status
    if (-not $svcState) {
        Add-Content -Path $watchLog -Encoding UTF8 `
            -Value "$stamp 主用与备用同时不通，且找不到 $svc 服务，无法恢复"
        exit 0
    }

    if ($svcState.ToString().ToLower() -eq 'running') {
        Restart-Service -Name $svc -Force -ErrorAction SilentlyContinue
    } else {
        Start-Service -Name $svc -ErrorAction SilentlyContinue
    }
    Start-Sleep -Seconds 30   # 等它建连（SCM 恢复策略本身还带 20 秒延迟）

    # ⚠️ 不在字符串插值里用三元运算符 `? :` —— 本机 PowerShell 是 5.1，不支持。
    $verdict = '仍不通'
    if ((Probe $backupUrl) -eq '200') { $verdict = '200，已恢复' }
    Add-Content -Path $watchLog -Encoding UTF8 `
        -Value "$stamp 主用与备用同时不通 → 已重启 $svc（原状态 $svcState）；复检备用 $verdict"
    exit 0
} finally {
    if ($lock) { $lock.Close(); $lock.Dispose() }
}
