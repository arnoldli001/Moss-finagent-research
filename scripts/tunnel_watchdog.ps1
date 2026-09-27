# 隧道（cloudflared）运行期值守包装
#
# 由计划任务 `MossTunnelWatchdog` 周期调用（每 5 分钟一次）。
#
# ## 为什么需要它
#
# 实测（2026-09-26，同一时刻对照）：
#
#     后端本机   1.9~3.4 毫秒、5/5 成功
#     走公网     1.14~11.5 秒、9/10 成功（1 次直接挂死）
#
# 后端是 2 毫秒级的健康服务，**问题全在隧道**。用户看到的是"后台挂了"。
#
# 而且实测到**重启隧道能恢复**：
#
#     | | 重启前 | 重启后 |
#     |---|---|---|
#     | 成功率 | 9/10（1 次挂死） | 10/10 |
#     | 中位延迟 | 3.34 s | 1.60 s |
#
# 这条链路会随时间劣化（连续跑 35 小时后劣化；23 分钟后也复现同样形态）。
# 本包装 + 计划任务把它变成自动恢复。
#
# ## 判据（在 `manage.py tunnel-ensure` 里）
#
# **只有"连不上 / 超时 / 5xx"才算失败；4xx 一律算成功** ——
# 能拿到业务错误码，说明请求走完了 cloudflared → 后端 → 回来的整条路。
# 踩过的坑：第一版把任何非 200 当失败，于是探测路径被登录门槛拦下时
# 误判为故障，**白白重启了一次隧道**（重启会中断所有在途请求）。
#
# ## ★★ 第二个坑（2026-09-26 实测事故）：重启方式会造出两个实例
#
# 原来 `manage.py tunnel-ensure` 做的是"taskkill 全部 cloudflared +
# 自己 Start-Process 一个"。而这台机器上 cloudflared 是**以服务方式运行**的：
#
#     Cloudflared 服务  StartMode=Auto  SERVICE_START_NAME=LocalSystem
#     FAILURE_ACTIONS   RESTART -- Delay = 20000 milliseconds
#
# 也就是说 **SCM 自己会在进程挂掉 20 秒后把它拉起来**。于是：
#
#     杀掉服务进程 → SCM 20 秒后拉起（PID A）→ 我们又起一个（PID B）
#     ＝ **两个实例**，各自向 CF 注册连接器（实测两个 connector 相差 16 秒）
#
# 两个实例互相抢同一个隧道，不但没修好，**重启本身成了不稳定源**。
#
# 现在改为走 **Windows 服务**（`Restart-Service` / `Start-Service`），
# 由 SCM 保证"永远只有一个实例"。所以这个包装**不需要动进程**。
#
# ## 设计约束
#
# 1. **健康时必须完全静默**：绝大多数 tick 都健康，一有输出就把日志刷成噪音。
# 2. **恢复动作记在 `data/run/tunnel_incidents.jsonl`**（含内存现场），
#    不在本日志里重复记。
# 3. 只在**退出码非 0**（探测失败且重启后仍不通）时写一行。
# 4. **绝不自起 cloudflared 进程**：那是服务的事（见上）。
$ErrorActionPreference = 'Continue'
$proj = 'D:\code\Moss-finagent-research'
$logDir = Join-Path $proj 'data\run'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$watchLog = Join-Path $logDir 'tunnel-watchdog.log'
$lockPath = Join-Path $logDir '.tunnel-watchdog.lock'

# 单实例锁：上一轮没跑完（探测 3 次 + 重启 + 等建连可能要 40 秒）时下一轮直接退出
$lock = $null
try {
    $lock = [IO.File]::Open($lockPath, 'OpenOrCreate', 'ReadWrite', 'None')
} catch {
    exit 0
}

try {
    Push-Location $proj
    try {
        $env:PYTHONIOENCODING = 'utf-8'
        [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
        $null = & (Join-Path $proj '.venv\Scripts\python.exe') `
            (Join-Path $proj 'manage.py') tunnel-ensure *>&1
        $code = $LASTEXITCODE
    } finally {
        Pop-Location
    }
    if ($code -ne 0) {
        $stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
        Add-Content -Path $watchLog -Encoding UTF8 `
            -Value "$stamp 隧道值守退出码 $code（探测失败，重启后仍不通；详见 tunnel_incidents.jsonl）"
    }
    exit $code
} finally {
    if ($lock) { $lock.Close(); $lock.Dispose() }
}
