"""带**父进程死亡联动**的 pytest 运行器（Windows Job Object）。

## 为什么必须有它（2026-09-26 实测孤儿进程）

发现一个 pytest 进程成了孤儿：父进程已退出、CPU = 0 秒、HandleCount = 0、
内存 1 MB —— 从启动起就卡死在初始化，**一天多没动过**，却一直占着资源与
数据库文件句柄。它的父进程是被强杀/超时终止的（AI 工具调用或交互式 shell
退出都会这样）。

根因有两条，缺一不可：

1. **Windows 没有 `PR_SET_PDEATHSIG`**：一个进程**无法**在"父进程死了"时
   被内核自动回收（Linux 上这是默认配套机制，Windows 上不存在）。
2. **pytest 自己也不检测父进程**：它不轮询父 PID，所以父一死它就成了
   无主进程继续挂着 —— 而它卡住的位置在初始化早期，连超时都没有。

## 解法：Job Object + `KILL_ON_JOB_CLOSE`

Windows 的对应机制是 **Job Object**。本脚本：

    1. 建一个 Job，设置 `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`；
    2. 把 pytest **作为该 Job 的子进程**启动；
    3. 自己阻塞等待它结束。

关键在于：**Job 句柄只被本脚本持有**。本脚本一旦消失（正常退出、被强杀、
超时终止、控制台关闭），内核就关闭这个句柄 → Job 关闭 →
`KILL_ON_JOB_CLOSE` 生效 → **pytest 及其全部后代一起被内核终止**。

于是"父进程死了留个孤儿"这件事在机制上不可能发生，
**不依赖任何人记得去清理**。

## 为什么不做成"轮询父 PID"

那种写法（子进程每 N 秒查一次父是否还在）有两个洞：
轮询间隔内父死了它还在跑；以及"父死了但 PID 被复用"会误判。
Job Object 是内核级的，两个洞都不存在。

## 用法

    python scripts/run_tests_in_job.py tests/unit -q
    python scripts/run_tests_in_job.py tests -q -p no:randomly   # 参数原样透传

退出码与 pytest 一致（`sys.exit` 原样传递），所以它能直接替换 `pytest ...`。
"""

# ruff: noqa: N802  Win32 API 的名字就是驼峰，改名反而不好对照文档

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# ── Win32 常量 ──
_JobObjectExtendedLimitInformation = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_TERMINATE = 0x0001
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

if os.name != "nt":  # pragma: no cover - 本项目在 Windows 上跑；非 Windows 直接透传
    def main(argv: list[str]) -> int:
        """非 Windows：直接用 `subprocess` 起（那边的孤儿问题由会话/进程组处理）。"""
        return subprocess.run(
            [sys.executable, "-m", "pytest", *argv], cwd=str(ROOT),
            check=False).returncode
else:
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

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    def _create_kill_on_close_job() -> int:
        """建一个"最后一个句柄关闭时杀掉全部成员"的 Job，返回句柄。"""
        job = _kernel32.CreateJobObjectW(None, None)
        if not job:
            raise OSError(ctypes.get_last_error(), "CreateJobObjectW 失败")
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = (
            _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE)
        ok = _kernel32.SetInformationJobObject(
            wintypes.HANDLE(job), _JobObjectExtendedLimitInformation,
            ctypes.byref(info), ctypes.sizeof(info))
        if not ok:
            err = ctypes.get_last_error()
            _kernel32.CloseHandle(wintypes.HANDLE(job))
            raise OSError(err, "SetInformationJobObject 失败")
        return job

    def main(argv: list[str]) -> int:
        # `--exec <cmd>` 是**自查用**开关：把任意命令放进 Job，
        # 用来验证"父死了子陪葬"这条机制本身真的生效
        # （跑 pytest 时它可能几秒就结束，抓不到窗口）。
        if argv[:1] == ["--exec"]:
            child_cmd = argv[1:]
            if not child_cmd:
                print("--exec 需要一个命令", file=sys.stderr)
                return 2
        else:
            child_cmd = [sys.executable, "-m", "pytest", *argv]

        job = _create_kill_on_close_job()
        # CREATE_NEW_PROCESS_GROUP 让 Ctrl+C 不会误伤本脚本自身；
        # 不加 CREATE_BREAKAWAY_FROM_JOB —— 我们要的正是"它在这个 Job 里"。
        proc = subprocess.Popen(
            child_cmd, cwd=str(ROOT),
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
        # 把 pytest 放进 Job。⚠️ 必须在它跑远之前完成；pytest 初始化要几百毫秒，
        # 这里紧随 Popen 之后，实测足够。真正的保证来自下面那句：
        # 即使 AssignProcessToJobObject 失败（例如进程已自建 Job），
        # 我们也会**终止它**而不是留一个不受管的进程。
        hproc = _kernel32.OpenProcess(
            _PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, proc.pid)
        assigned = False
        if hproc:
            try:
                assigned = bool(_kernel32.AssignProcessToJobObject(
                    wintypes.HANDLE(job), wintypes.HANDLE(hproc)))
            finally:
                _kernel32.CloseHandle(wintypes.HANDLE(hproc))
        if not assigned:
            # 兜底：宁可杀掉，也不留一个"本脚本死了它还在跑"的孤儿 ——
            # 那正是本脚本要消灭的东西。
            print("[run_tests_in_job] ⚠️ 无法把 pytest 放入 Job，"
                  "已终止它以杜绝孤儿进程。", file=sys.stderr)
            proc.kill()
            proc.wait()
            _kernel32.CloseHandle(wintypes.HANDLE(job))
            return 2
        try:
            return proc.wait()
        except KeyboardInterrupt:
            # 用户 Ctrl+C：先请它退，再退而强杀；Job 句柄关闭会兜住一切。
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
            raise
        finally:
            # 正常路径也显式关闭：Job 一关，成员全部被内核终止
            # （此时 pytest 已退出，所以是空操作 —— 但语义要写对）。
            _kernel32.CloseHandle(wintypes.HANDLE(job))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
