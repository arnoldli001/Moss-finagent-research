' ============================================================================
'  frp_watchdog_hidden.vbs  --  launch a PowerShell watchdog with NO window
' ============================================================================
'
'  WHY THIS FILE EXISTS (measured, 2026-09-27)
' ----------------------------------------------------------------------------
'  The scheduled task used to run:
'
'      powershell.exe -NoProfile -NonInteractive -WindowStyle Hidden -File ...frp_watchdog.ps1
'
'  That STILL flashes a black window every 5 minutes. A 5-minute passive monitor
'  caught it red-handed:
'
'      pid=15604 powershell  title='C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe'
'      parent = svchost.exe (Task Scheduler)
'
'  The title being the full exe path proves we caught the STARTUP INSTANT:
'  the console window is allocated by the OS inside CreateProcess, *before*
'  PowerShell is even loaded. `-WindowStyle Hidden` is applied only afterwards,
'  so the window is genuinely visible for a short moment.
'
'  wscript.exe is a GUI-subsystem program: it owns no console. When it starts
'  the child with WshShell.Run(cmd, 0, ...), the SW_HIDE flag is part of
'  CreateProcess itself -- the console window is never shown at all.
'
'  Do NOT "simplify" this back to powershell.exe -WindowStyle Hidden.
'
'  NOTE: comments are intentionally ASCII-only. VBScript files have the same
'  encoding trap as .ps1: a non-ASCII byte in a file without a BOM can be
'  mis-decoded and corrupt the script. ASCII keeps this file immune.
'
'  Exit code is propagated (bWaitOnReturn = True), so Task Scheduler's
'  LastTaskResult still reflects the real outcome instead of always being 0.
' ============================================================================

Option Explicit

Dim fso, sh, baseDir, ps1, cmd, rc

If WScript.Arguments.Count >= 1 Then
    ps1 = WScript.Arguments(0)
Else
    ' Self-locating: resolve the sibling .ps1 next to this script, so the task
    ' action never has to hardcode a project path.
    Set fso = CreateObject("Scripting.FileSystemObject")
    baseDir = fso.GetParentFolderName(WScript.ScriptFullName)
    ps1 = fso.BuildPath(baseDir, "frp_watchdog.ps1")
End If

cmd = "powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File """ & ps1 & """"

Set sh = CreateObject("WScript.Shell")
' 0 = hidden window, True = wait and return the child's exit code
rc = sh.Run(cmd, 0, True)

WScript.Quit rc
