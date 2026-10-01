# 手动触发一次（pilot 已在运行时会安全 no-op，不会重复启动）
schtasks /run /tn "MossPilotAutostart"

# 查看任务状态
schtasks /query /tn "MossPilotAutostart"

# 删除开机自启
schtasks /delete /tn "MossPilotAutostart" /f

请用 --username 指定要重置的账号。
PS D:\code\Moss-finagent-research> .\.venv\Scripts\python.exe scripts\reset_admin_password.py `        
>>     --list --db data/pilot/moss_pilot.db


# ① 只杀 pilot 那棵树（用 manage.py 自己的原语，精确到 PID，不用进程名通杀）
uv run python -c "import manage; manage.kill_pid_tree(11576)"

# ③ 启动 pilot（**不加 --replace**）
uv run python manage.py start --env pilot --port 8110 --daemon

# ④ 验收
uv run python manage.py status        # → 对外试点 8110 运行中