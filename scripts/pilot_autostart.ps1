# 开机自启包装脚本：对外试点后端 pilot (127.0.0.1:8110)
#
# 为什么需要这个包装：任务以 Local System 身份在开机时（无需登录）运行，
# 而 DEEPSEEK_API_KEY / ALERT_SMTP_* 等配置不在系统级环境变量里（DEEPSEEK
# 是 Administrator 用户级变量、SMTP 凭据只在 .env 中），config.py 又不自动
# 加载 .env，因此先由本脚本把 .env 注入当前进程环境，再调 manage.py
# 完成"环境自检 → 端口占用检查 → 守护进程 detach"全过程。
$ErrorActionPreference = 'Stop'

$proj = 'D:\code\Moss-finagent-research'
$logDir = Join-Path $proj 'data\run'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$bootLog = Join-Path $logDir 'pilot-autostart.log'

Add-Content -Path $bootLog -Encoding UTF8 `
    -Value ("{0} 开机自启脚本开始" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'))

# 1) 把 .env 注入进程环境（去注释/空行，去掉值两端可选的包裹引号）
Get-Content (Join-Path $proj '.env') | ForEach-Object {
    $line = $_.Trim()
    if (-not $line -or $line.StartsWith('#')) { return }
    $eq = $line.IndexOf('=')
    if ($eq -lt 1) { return }
    $key = $line.Substring(0, $eq).Trim()
    $val = $line.Substring($eq + 1)
    if ($val.Length -ge 2 -and
        (($val[0] -eq '"' -and $val[-1] -eq '"') -or
         ($val[0] -eq "'" -and $val[-1] -eq "'"))) {
        $val = $val.Substring(1, $val.Length - 2)
    }
    Set-Item -Path ("Env:\" + $key) -Value $val
}

# 2) 启动 pilot 守护进程（manage.py 内部跑环境/依赖自检，已在运行则 no-op）
# 注意：manage.py 的状态横幅按设计写到 stderr，调用期间不能用 Stop 策略，
# 否则 PowerShell 会把这些正常输出当成本机命令致命错误 (NativeCommandError)。
Push-Location $proj
try {
    $ErrorActionPreference = 'Continue'
    # 统一原生命令管道编码：python 输出 UTF-8，PowerShell 也按 UTF-8 解码，
    # 否则 SYSTEM 非交互会话下中文横幅会变成乱码。
    $env:PYTHONIOENCODING = 'utf-8'
    [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
    $output = & (Join-Path $proj '.venv\Scripts\python.exe') `
        (Join-Path $proj 'manage.py') start --env pilot --port 8110 --daemon 2>&1
    $code = $LASTEXITCODE
    $output | ForEach-Object {
        $text = ($_ | Out-String).TrimEnd()
        if ($text) { Add-Content -Path $bootLog -Encoding UTF8 -Value $text }
    }
} finally {
    Pop-Location
    $ErrorActionPreference = 'Stop'
}
Add-Content -Path $bootLog -Encoding UTF8 -Value `
    ("{0} manage.py 退出码 {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $code)
exit $code
