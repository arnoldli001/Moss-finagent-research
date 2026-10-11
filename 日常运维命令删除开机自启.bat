@echo off
chcp 65001 >nul
REM ============================================================================
REM 日常运维命令（开机自启 / 值守相关）—— 本文件**只做安全动作**
REM
REM ★★ 2026-10-11 实测事故（本文件存在的理由）：
REM    三个任务 MossPilotAutostart / MossPilotWatchdog / MossAutostartGuard
REM    在 10:22:10 被删除（任务计划程序事件 ID 141，操作者 Administrator），
REM    10:36:51 机器重启 ⇒ **没有任何东西拉起 8110** ⇒ 公网 hk.wujiaitool.cn
REM    全站 502；唯一活着的 MossFrpEnsure 按设计"本机后端没起来就放手
REM    （归 PilotWatchdog）"⇒ 日志里写着一句看起来完全正常的话，实际无人负责。
REM
REM    ⇒ **不要删除这些任务**。要临时关掉用 `/change /disable`（可恢复）。
REM    ⇒ 恢复注册：uv run python scripts\moss_autostart.py --install
REM ============================================================================

echo [1/3] 手动触发一次开机拉起（pilot 已在运行时是安全 no-op，不会重复启动）
schtasks /run /tn "MossPilotAutostart"

echo.
echo [2/3] 值守任务状态（期望四个都是 Ready / Enabled）
schtasks /query /fo TABLE | findstr /I "Moss"

echo.
echo [3/3] 仓库自带判据（退出码 0=OK，1=有缺失/停用，2=读不到）
uv run python scripts\moss_autostart.py --check

echo.
echo 其余命令见文件末尾注释（**不要**执行删除）。
pause

REM ----------------------------------------------------------------------------
REM 临时停用（可恢复）：
REM   schtasks /change /tn "MossPilotWatchdog" /disable
REM   schtasks /change /tn "MossPilotWatchdog" /enable
REM
REM ⛔ 禁止使用（会让"重启后自动拉起 8110"这件事彻底失效）：
REM   schtasks /delete /tn "MossPilotAutostart" /f        ← 2026-10-11 事故的直接原因
REM   schtasks /delete /tn "MossPilotWatchdog" /f
REM   schtasks /delete /tn "MossAutostartGuard" /f
REM
REM 起后端 / 验收：
REM   uv run python manage.py start --env pilot --port 8110 --daemon
REM   uv run python manage.py status
REM 只杀 pilot 那棵树（精确到 PID，不用进程名通杀）：
REM   uv run python -c "import manage; manage.kill_pid_tree(<PID>)"
REM ----------------------------------------------------------------------------
