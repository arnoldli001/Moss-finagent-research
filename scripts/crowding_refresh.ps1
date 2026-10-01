# 板块拥挤度：**自动定时刷新**的计划任务包装
#
# 由计划任务 `MossCrowdingRefresh` 调用。触发时刻（**每周两次**）：
#
#     每周二 11:00
#     每周六 11:00
#
# ## ★ 为什么是两次 —— 用户定的成本上限（2026-09-30）
#
# 用户原话：「拥挤度会花费 tokens 和搜索用量，**设置一周最大调用 2 次**」。
#
# 实测口径更正：**拥挤度刷新完全不调用 LLM**（`src/sector_crowding/` 下
# `llm|openai|deepseek|embedding` 零命中），所以**不花 token**。
# 真实成本是 **Tushare 外部配额**：
#
#     sources.py:149  ths_index   （板块列表，1 次）
#     sources.py:233  ths_daily   （每板块 1 次 —— 大头）
#     sources.py:274  ths_member  （成分股，按需）
#
# 上限在**注册入口**是硬约束（`scripts/setup_crowding_refresh_task.py` 的
# `_enforce_weekly_budget()`），不是注释里的提醒。
#
# > 曾考虑"每天 21:00"（理由：晚上机器大概率开着），**已被这条上限否掉** ——
# > `EveryDay` 折算成周频是 **7 次**。
# > 若机器在 11:00 常关着，正确做法是**把这两次挪到机器开着的时段**
# > （例如周六 11:00 + 周六 21:00），**不是**增加次数。
#
# ## 它解决什么
#
# `sector_crowding_daily` 此前**没有任何调度作业更新它** —— 实测数据停在
# 2026-09-24、最后写入 2026-09-27T18:52，而仓库里最新交易日是 2026-09-29。
# 也就是说「一键刷新」**只能靠人手动点**，没人点就一直是旧的。
#
# ## 为什么是「不依赖服务启动」的 OS 级计划任务
#
# 用户口径：「不依赖服务是否启动」。服务内调度器（`src/scheduler/`）做不到
# 这一点 —— 进程不在就什么都不跑；而拥挤度参考数据的写者是 **dev 实例**
# （`crowding_shared.writer: dev`），dev 并不常驻。
# 所以走 Windows 计划任务：**开机后一直在**，与后端进程无关。
#
# ## 时间选择的理由
#
# * **避开 09:00~10:30**（用户点名）；11:00 在午间休市窗口内；
# * **周六 11:00 完全空闲**（那天唯一作业是 20:00 的 `model_retrain_weekly`）；
# * 周二 11:00 只有每 2~5 分钟的盘中轻活（走腾讯/东财，与 Tushare 不同源）；
# * **不落在 16:00~23:30 的 `quant_data_sync` 窗口**里 —— 它每 30 分钟一次，
#   与拥挤度**共用同一个 Tushare token**（限流器是**每进程**的，
#   挡不住跨进程叠加）。11:00 避开这个窗口是刻意的。
#
# ## 只刷「关注板块池」，与前端按钮同口径
#
# `manage.py crowding-refresh` 默认 `pool_only=True`（不带 `--full`），
# 与前端「一键刷新」按钮完全一致。全量 2517 个板块要几分钟且会撞限流
# （实测 8 个板块因 `ths_daily` 500 次/分钟 超限而失败），日常增量不必。
#
# ## 幂等 / 并发安全
#
# 单实例锁：拿不到锁说明上一轮还在跑，**直接退出 0**（不是失败）。
# 刷新本身幂等（`UNIQUE(trade_date, sector_code)` UPSERT），重复触发不产生重复行。
#
# ## ★ 编辑本文件后**必须**重新补 BOM
#
# Windows PowerShell 5.1 读脚本时**没有 BOM 就按系统 ANSI(GBK) 解码**，
# 中文注释的字节边界会吞掉引号 → 解析阶段就失败。症状是
# 计划任务 `LastTaskResult=1` 且**一行日志都不写**，与"健康时的静默"无法区分。
# 本项目实测栽过（`scripts/tunnel_watchdog.ps1` 曾长期失效）。
# 所以改完必须跑：
#
#     python scripts/fix_ps1_bom.py scripts/crowding_refresh.ps1
#     python -m pytest tests/unit/test_ps1_encoding.py -q
#

$ErrorActionPreference = 'Continue'
$proj = 'D:\code\Moss-finagent-research'
$logDir = Join-Path $proj 'data\run'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$jobLog = Join-Path $logDir 'crowding-refresh.log'
$lockPath = Join-Path $logDir '.crowding-refresh.lock'

$stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'

# ── 单实例锁（独占打开；拿不到说明上一轮还在跑）──
$lock = $null
try {
    $lock = [IO.File]::Open($lockPath, 'OpenOrCreate', 'ReadWrite', 'None')
} catch {
    Add-Content -Path $jobLog -Encoding UTF8 `
        -Value "$stamp 跳过：上一轮仍在执行（拿不到单实例锁）"
    exit 0
}

try {
    Push-Location $proj
    try {
        $env:PYTHONIOENCODING = 'utf-8'
        [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)

        # ★ **只收 stdout，stderr 单独走 `2>&1` 的临时文件**（2026-09-30 实测修正）。
        #
        # 第一版写的是 `*>&1`（stdout+stderr 一起收）。实测把 **stderr 横幅**
        # 也写进了台账 —— `manage.py` 会往 stderr 打环境横幅
        # （「🔧 dev 隔离实例：数据目录 …」「定时任务（env=dev）：46/49…」），
        # 于是日志里混进三行与本次刷新无关的噪声，还带着 PowerShell 的
        # `NativeCommandError` 包装。台账要的是**结论**，不是启动横幅。
        #
        # 但**失败原因必须留住**（数据源限流 / 无 token / 库锁都在 stderr），
        # 所以 stderr 不丢，只是**只在非 0 退出码时才写进台账**。
        $errFile = [IO.Path]::GetTempFileName()
        try {
            $output = & (Join-Path $proj '.venv\Scripts\python.exe') `
                (Join-Path $proj 'manage.py') crowding-refresh --env dev `
                2>$errFile
            $code = $LASTEXITCODE
            $text = ($output | Out-String).Trim()
            $errText = (Get-Content $errFile -Raw -ErrorAction SilentlyContinue)
            if ($errText) { $errText = $errText.Trim() }
        } finally {
            Remove-Item $errFile -Force -ErrorAction SilentlyContinue
        }
    } finally {
        Pop-Location
    }

    # 退出码语义（见 manage.py cmd_crowding_refresh）：
    #   0 = 刷新成功（或前一轮还在跑的跳过）
    #   1 = 刷新抛异常
    #   2 = 本环境不是写者 → 计划任务配错了环境，**需要人来看**
    #   3 = 未预期异常
    if ($code -eq 0) {
        # 成功也记一行：这是"每周两次"的量级，一行运行台账远不至于淹没日志，
        # 而它正是回答"上次到底刷没刷、什么时候刷的"的唯一凭据。
        Add-Content -Path $jobLog -Encoding UTF8 -Value "$stamp $text"
    } else {
        $why = if ($code -eq 2) { '本环境不是 crowding_shared 的写者（计划任务配错环境？）' }
               else { '刷新失败' }
        # 失败时才把 stderr 一起留档 —— 那里面才是真正的原因
        $detail = if ($errText) { "$text`n--- stderr ---`n$errText" } else { $text }
        Add-Content -Path $jobLog -Encoding UTF8 `
            -Value "$stamp ❌ 退出码 $code：$why`n$detail"
    }
    exit $code
} finally {
    if ($lock) { $lock.Close(); $lock.Dispose() }
}
