# 140 池重打分完成后的分析链（等打分结束 → 落库核对 → 重出三份报告）
#
# ⚠️ 本文件**必须带 UTF-8 BOM**（2026-09-22 踩过）
#
# Windows PowerShell 5.1 读 `.ps1` 时，**没有 BOM 就按本地代码页（中文系统 = GBK）
# 解码**，于是脚本里的中文字面量在解析阶段就已经是乱码：
# 「等待 140 池重打分结束」写进日志会变成「绛夊緟 140 姹犻噸鎵撳垎缁撴潫」。
# 逻辑不受影响（路径与命令都是 ASCII），但审计日志不可读。
# 本项目已多次记录 PowerShell + 非 ASCII 的坑，这是又一例。
#
# 修正方法：用 Python 以 `utf-8-sig` 写回；不要用会丢 BOM 的编辑器。
#
# 用法：  & .\scripts\run_chain_140.ps1      （本机没有 pwsh，只有 powershell 5.1）
$ErrorActionPreference = 'Stop'
$env:PYTHONIOENCODING = 'utf-8'
Set-Location 'D:\code\Moss-finagent-research'

$py = '.\.venv\Scripts\python.exe'
$log = 'scripts\_chain_scope140.log'
"== 等待 140 池重打分结束 $(Get-Date -Format o) ==" | Out-File $log -Encoding utf8

& $py scripts\wait_rescore.py --min 135 --stable 4 --timeout 170 *>> $log
if ($LASTEXITCODE -ne 0) {
    "X 等重打分超时/未达标，中止分析链（不引用半截口径）" | Out-File $log -Append -Encoding utf8
    Get-Content $log -Encoding utf8 | Select-Object -Last 8
    exit 2
}
"== 重打分完成 $(Get-Date -Format o) ==" | Out-File $log -Append -Encoding utf8

"== 范围闸门复核（应为 0 行待处理：闸门在打分中已原生生效）==" | Out-File $log -Append -Encoding utf8
& $py scripts\apply_alert_scope.py --dry-run *>> $log

"== 池清单 ==" | Out-File $log -Append -Encoding utf8
& $py scripts\export_pool_list.py *>> $log

"== 误报榜（线上表）==" | Out-File $log -Append -Encoding utf8
& $py scripts\board_false_positive_report.py --alert-table mainline_alert `
    --out docs/MAINLINE_FALSE_POSITIVE_BOARDS.md `
    --excel docs/MAINLINE_CONCEPT_FP_LIST.xlsx *>> $log

"== 前后对照（池规模已变，需 --allow-pool-change）==" | Out-File $log -Append -Encoding utf8
& $py scripts\compare_floor_rescore.py --allow-pool-change `
    --out docs/MAINLINE_FLOOR_RESCORE.md *>> $log

"ALL_DONE $(Get-Date -Format o)" | Out-File $log -Append -Encoding utf8
Get-Content $log -Encoding utf8 | Select-Object -Last 45
