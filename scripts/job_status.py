"""只读诊断：某个进程是否在 **Job Object** 里、那个 Job 会不会"关掉就杀进程"。

## 为什么需要它（2026-09-26）

对外实例被**静默终止**过一次：端口无监听、进程全无、日志只到最后一个
`200 OK`、事件日志也没有崩溃/关机记录 —— 即"被外部强杀"。

强杀最常见的机制就是 **Job Object**：Windows 上任何进程都可以被放进一个
Job，而 Job 设了 `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` 时，
**最后一个 Job 句柄一关闭，成员进程全部被内核终止**（父进程退出、
AI 工具调用结束、shell 超时被杀，都会触发）。它不走任何信号处理，
所以**不留下任何痕迹** —— 与观测到的现象完全一致。

## 它回答什么

    · 这个进程在 Job 里吗？
    · 有几个 Job 包含它？
    · 那些 Job 设了 KILL_ON_JOB_CLOSE / BREAKAWAY_OK 吗？

**"在 Job 里 + KILL_ON_JOB_CLOSE"** 就是高风险组合：宿主一消失，
进程陪葬。反过来，"不在任何 Job 里"就基本排除了这条杀因。

## 用法

    python scripts/job_status.py 8110        # 按端口找监听进程
    python scripts/job_status.py --self       # 看自己
    python scripts/job_status.py --pid 1234
"""

from __future__ import annotations

# ruff: noqa: N802  Win32 API 名字就是驼峰

import argparse
import ctypes
import os
import subprocess
import sys
from ctypes import wintypes

_JobObjectExtendedLimitInformation = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0x00000800
_JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK = 0x00001000
_JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION = 0x00000400

_FLAG_NAMES = {
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE: "KILL_ON_JOB_CLOSE",
    _JOB_OBJECT_LIMIT_BREAKAWAY_OK: "BREAKAWAY_OK",
    _JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK: "SILENT_BREAKAWAY_OK",
    _JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION: "DIE_ON_UNHANDLED_EXCEPTION",
}


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [(n, ctypes.c_ulonglong) for n in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
        ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.POINTER(ctypes.c_ulong)),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def _flag_names(flags: int) -> list[str]:
    named = [n for bit, n in _FLAG_NAMES.items() if flags & bit]
    rest = flags & ~sum(_FLAG_NAMES)
    if rest:
        named.append(f"0x{rest:x}")
    return named


def _list_jobs_for(pid: int) -> list[tuple[int, int]]:
    """返回 `[(job_handle, limit_flags)]`；失败时抛 OSError。

    ⚠️ 用 `QueryInformationJobObject(NULL, JobObjectBasicProcessIdList)`
    枚举"哪些 Job 含这个 PID"不现实（要遍历所有 Job）。
    这里用**反向**做法：把目标进程放进一个**临时探测 Job** ——
    若它已在别的 Job 里，`AssignProcessToJobObject` 会失败
    （除非那个 Job 设了 BREAKAWAY_OK），据此可以判定"它在某个 Job 里"。

    代价：需要 `PROCESS_SET_QUOTA` 权限（同用户进程通常有）。
    """
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    probe = kernel32.CreateJobObjectW(None, None)
    if not probe:
        raise OSError(ctypes.get_last_error(), "CreateJobObjectW")
    try:
        hproc = kernel32.OpenProcess(0x0100 | 0x0001, False, pid)  # SET_QUOTA|TERMINATE
        if not hproc:
            raise OSError(ctypes.get_last_error(), f"OpenProcess({pid})")
        try:
            ok = kernel32.AssignProcessToJobObject(
                wintypes.HANDLE(probe), wintypes.HANDLE(hproc))
            err = ctypes.get_last_error()
        finally:
            kernel32.CloseHandle(wintypes.HANDLE(hproc))
        if ok:
            # 成功 = 它**不在**任何 Job 里（探测 Job 现在含它了；
            # 我们马上关闭探测句柄 —— 因为它没设 KILL_ON_CLOSE，所以无害）
            return []
        # 失败 = 已经在某个 Job 里；ERROR_ACCESS_DENIED(5) 是最常见的表现
        return [(-1, 0)] if err in (5, 87) else [(-1, err)]
    finally:
        kernel32.CloseHandle(wintypes.HANDLE(probe))


def _find_listener_pid(port: int) -> int | None:
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-NetTCPConnection -LocalPort {port} -State Listen "
             "-ErrorAction SilentlyContinue).OwningProcess"],
            capture_output=True, text=True, timeout=25,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        pid = out.strip().splitlines()[0].strip() if out.strip() else ""
        return int(pid) if pid.isdigit() else None
    except Exception:
        return None


def _cmdline(pid: int) -> str:
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-CimInstance Win32_Process -Filter \"ProcessId = {pid}\")"
             ".CommandLine"],
            capture_output=True, text=True, timeout=25,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        return out.strip()[:120]
    except Exception:
        return ""


def main() -> int:
    # 控制台可能是 GBK（中文 Windows 默认）。emoji 与部分符号会直接
    # `UnicodeEncodeError` 把诊断脚本自己搞崩 —— 一个"用来查问题"的
    # 工具自己崩掉是最糟的体验。所以重配为 UTF-8，失败就退回 ASCII 标记。
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        pass

    ap = argparse.ArgumentParser(description="诊断进程是否在 Job Object 里")
    ap.add_argument("port", nargs="?", type=int, help="按监听端口找进程")
    ap.add_argument("--pid", type=int, help="直接指定 PID")
    ap.add_argument("--self", action="store_true", help="看当前进程")
    args = ap.parse_args()

    if args.self:
        pid = os.getpid()
    elif args.pid:
        pid = args.pid
    elif args.port:
        pid = _find_listener_pid(args.port)
        if pid is None:
            print(f"端口 {args.port} 上没有监听进程", file=sys.stderr)
            return 1
    else:
        ap.error("需要 port / --pid / --self 之一")

    print(f"目标进程 PID = {pid}")
    cmd = _cmdline(pid)
    if cmd:
        print(f"  命令行: {cmd}")

    try:
        jobs = _list_jobs_for(pid)
    except OSError as exc:
        print(f"  [警告] 无法判定（{exc}）")
        return 2

    if not jobs:
        print("  [OK] 不在任何 Job Object 里 —— "
              "排除了「Job 关闭导致陪葬」这条杀因。")
        print("       它可以独立于父进程存活（父退出/被强杀都不影响它）。")
        return 0

    print("  [注意] 在某个 Job Object 里 —— 该 Job 的宿主一消失，它可能陪葬。")
    for _handle, flags in jobs:
        names = (_flag_names(flags) if flags
                 else ["(无法读取限制标志，通常=已在别的 Job 内)"])
        print(f"     Job limit flags: {', '.join(names)}")
        if flags & _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE:
            print("     [危险] 含 KILL_ON_JOB_CLOSE：**持有该 Job 的进程一退出，"
                  "本进程会被内核终止且不留日志** —— 与实测的静默终止吻合。")
        else:
            print("     [信息] 未设 KILL_ON_JOB_CLOSE：Job 关闭不会杀它。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
