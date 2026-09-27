# 对外实例（pilot @ 8110）运行期值守包装 —— **全项目唯一的值守入口**
#
# 由计划任务 `MossPilotWatchdog` 周期调用（**每 1 分钟一次**，`PT1M`）。
#
# ## 它解决什么
#
# 2026-09-26 实测：8110 **被静默终止**过一次 —— 端口无监听、进程全无、
# `backend.log` 只到最后一个 `200 OK`，Windows 事件日志也没有崩溃/关机记录。
# 强杀（`taskkill /F`、Job Object 关闭之类）**不走 lifespan**，所以什么都
# 不会留下；后果是"客户先发现打不开，我们才发现进程没了"，且事后查不到原因。
#
# 有了它，同一件事变成：**一个周期内自动恢复 + 现场入流水**。
#
# ## 与 pilot_autostart.ps1 的分工（不是两个值守）
#
#   pilot_autostart.ps1  开机自启（一次性，开机时跑 `manage.py start`）
#   pilot_watchdog.ps1   运行期值守（周期性；本文件）—— 唯一的运行期值守
#
# ## 合并说明（2026-09-26）
#
# 本文件原来是"探测失败两次 → 调 pilot_autostart.ps1 拉起"。
# 那份实现与 `manage.py ensure` 是**同一个功能的两套代码**，按要求只留一套：
# 统一走 `manage.py ensure`（判据可测、现场入 JSONL 流水、与启动逻辑共用
# `cmd_start` 不重复拼 uvicorn 命令）。计划任务与文件路径都没变。
#
# ## 设计约束（改这个文件前先读）
#
# 1. **健康时必须完全静默**：健康是绝大多数 tick 的形态，一有输出就会把
#    日志刷成噪音，真正的事故记录反而被淹没。所以所有输出一律丢弃，
#    只在**异常退出**（退出码非 0 且非 2）时写一行。
# 2. **恢复动作记在 `backend_incidents.jsonl`**，不在本日志里重复记。
#    那份流水是结构化的（JSONL，含端口 / 环境 / 上次活动时刻），更有用。
# 3. **不注入 .env**：不需要 —— `src/core/config.py` 用 pydantic-settings 的
#    `env_file=".env"`，只要 cwd 是项目根就会自己读到（实测无环境变量也能
#    成功重启 pilot，SMTP 自检通过）。非交互会话下手工注入反而是编码/身份坑。
# ## ★ 编辑本文件后**必须**重新补 BOM（2026-09-27 实测把生产值守弄坏）
#
# 事故经过：只改了两行**注释**（把"每 5 分钟"更正为"每 1 分钟"），
# 用文本编辑工具落盘后 **UTF-8 BOM 被丢掉**，于是：
#
#     PowerShell 5.1 按 GBK 读 → 中文串吞掉引号 →
#     行 56 解析失败并级联报 行 37/55/64 的
#     "Missing closing '}'" / "Try statement is missing its Catch or Finally"
#
# 也就是说**脚本整体失效**，而症状是计划任务 `LastTaskResult=1` + **一行日志都不写**
# —— 与"健康时的静默"完全无法区分（这正是 `tests/unit/test_ps1_encoding.py`
# 存在的理由，它是唯一的自动拦截）。
#
# ⚠️ 教训：**改注释也会破坏脚本**。BOM 是文件的隐藏属性，diff 里看不出来，
#    任何"读进来再写回去"的工具都可能丢掉它。
#    所以每次编辑本文件后跑一次：
#
#         python scripts/fix_ps1_bom.py scripts/pilot_watchdog.ps1
#         python -m pytest tests/unit/test_ps1_encoding.py -q
#
# 4. **绝不 `--replace`**：它按命令行枚举本项目**全部**后端进程，会把
#    正在跑的另一个实例（dev 8100）一起停掉。`ensure` 内部已显式关掉它。
# 5. **单实例锁**：上一轮还没跑完时下一轮直接退出。
#
#    ⚠️ 2026-09-27 更正：真实周期是 **1 分钟**（`PT1M`），不是本文档原先写的
#    "默认 5 分钟周期"。这个数字错了会让下面这句论证失去前提 ——
#    1 分钟的间隔**小于** `cmd_start` 的最坏耗时（`ensure` 含最多 ~10 秒
#    就绪等待，加上环境自检可能更久），所以"正常不会重叠"是**不成立**的：
#    靠的不是时间差宽裕，而完全是单实例锁 + 任务侧的 `IgnoreNew`。
#    换句话说，这个锁不是"多一层保险"，而是**唯一**防止重叠的机制。
$ErrorActionPreference = 'Continue'
$proj = 'D:\code\Moss-finagent-research'
$logDir = Join-Path $proj 'data\run'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$watchLog = Join-Path $logDir 'pilot-watchdog.log'
$lockPath = Join-Path $logDir '.pilot-watchdog.lock'

# ── 单实例锁（独占打开；拿不到说明上一轮还在跑，直接退出）──
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
        # 所有输出丢弃（含 stderr）：健康路径本来就静默，重启路径已进事件流水
        $null = & (Join-Path $proj '.venv\Scripts\python.exe') `
            (Join-Path $proj 'manage.py') ensure --env pilot --port 8110 *>&1
        $code = $LASTEXITCODE
    } finally {
        Pop-Location
    }

    # 退出码语义（见 manage.py cmd_ensure）：
    #   0 = 健康（静默）或重启成功
    #   2 = 端口被**其他程序**占用 → 拒绝启动，**这是需要人来看的事**
    #   其它 = 重启失败 / python 起不来（缺 .venv、脚本损坏等）
    #
    # ⚠️ 这里**不吞掉**非 0：原来是 `exit 0` 一律成功，导致"值守坏了"和
    #    "值守没事"在计划任务里长得一模一样（`LastTaskResult` 都是 0）——
    #    而那正是最难发现的静默失效。
    if ($code -ne 0) {
        $stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
        $why = if ($code -eq 2) { '端口被其他程序占用（值守拒绝启动）' }
               else { '重启失败或无法执行 manage.py' }
        Add-Content -Path $watchLog -Encoding UTF8 `
            -Value "$stamp 值守退出码 $code：$why；详见 backend_incidents.jsonl"
    }
    exit $code
} finally {
    if ($lock) { $lock.Close(); $lock.Dispose() }
}
