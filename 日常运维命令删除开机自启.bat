# 手动触发一次（pilot 已在运行时会安全 no-op，不会重复启动）
schtasks /run /tn "MossPilotAutostart"

# 查看任务状态
schtasks /query /tn "MossPilotAutostart"

# 删除开机自启
schtasks /delete /tn "MossPilotAutostart" /f

请用 --username 指定要重置的账号。
PS D:\code\Moss-finagent-research> .\.venv\Scripts\python.exe scripts\reset_admin_password.py `        
>>     --list --db data/pilot/moss_pilot.db