"""`scripts/*.ps1` 的编码 / 语法护栏 —— **防止"脚本静默死亡"再次发生**。

## 为什么需要这个测试（2026-09-27 实测事故）

本项目的 `.ps1` 值守脚本全部含中文注释。而 **Windows PowerShell 5.1 在文件
不带 UTF-8 BOM 时会按系统 ANSI 代码页（本机 GBK）读取源文件** —— 中文注释被
错解后可能与相邻字符错位，引发**解析错误**，脚本在入口处直接死掉。

它的症状极其隐蔽，三条都成立：

1. 计划任务 `LastTaskResult = 1`（看起来像"业务失败"）；
2. 脚本**一行日志都不写**（因为死在写日志之前）；
3. 健康时的设计是"静默"，所以**和"一切正常"长得一模一样**。

实测代价：`scripts/tunnel_watchdog.ps1` 一直是这个状态 ——
`MossTunnelWatchdog` 每 5 分钟返回 1，**Cloudflare 那条链路实际上长期没有值守**，
而外部表现只是"偶尔打不开、要等很久"。

## 为什么会反复发生

BOM 是**文件的隐藏属性，不是内容**。任何"读进来再写回去"的工具
（编辑器、AI 改写、脚本批处理）都可能把它丢掉，而 diff 里**看不出来**。
所以必须有自动检查，不能靠人记住。

## 判据

* 含非 ASCII 的 `.ps1` **必须有 UTF-8 BOM**；
* 所有 `.ps1` **必须能被 PowerShell 解析器零错误解析**。

第二条比第一条更强：即便将来换成别的编码问题，只要它导致解析失败就会被拦下。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = ROOT / "scripts"

#: 收集时排序，保证测试 ID 稳定（pytest-randomly 下也一致）
PS1_FILES = sorted(SCRIPTS_DIR.glob("*.ps1")) if SCRIPTS_DIR.is_dir() else []

#: `CREATE_NO_WINDOW`：跑子进程时不要在用户桌面上弹黑窗口
_NO_CONSOLE = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="PowerShell 解析检查仅在 Windows 上有意义"
)

_IDS = [p.name for p in PS1_FILES] or ["<no-ps1-found>"]


def test_scripts_dir_has_ps1_files() -> None:
    """护栏本身要能被触发 —— 一个都没收集到说明路径写错了。"""
    assert PS1_FILES, f"{SCRIPTS_DIR} 下没有找到任何 .ps1，护栏形同虚设"


@pytest.mark.parametrize("path", PS1_FILES or [SCRIPTS_DIR / "missing.ps1"], ids=_IDS)
def test_ps1_with_non_ascii_has_utf8_bom(path: Path) -> None:
    """含中文的 `.ps1` 必须带 UTF-8 BOM（否则 PS 5.1 按 GBK 读，解析即死）。"""
    if not path.exists():
        pytest.skip("占位参数（没有收集到 .ps1）")
    raw = path.read_bytes()
    try:
        raw.decode("ascii")
    except UnicodeDecodeError:
        pass  # 含非 ASCII → 必须带 BOM
    else:
        pytest.skip("纯 ASCII 文件，PS 5.1 按 ANSI 读也不会错位")

    assert raw.startswith(b"\xef\xbb\xbf"), (
        f"{path.name} 含非 ASCII 却没有 UTF-8 BOM。\n"
        "Windows PowerShell 5.1 会按系统 ANSI(GBK) 读取它，中文注释错位后引发\n"
        "解析错误 —— 症状是计划任务 LastTaskResult=1 且**一行日志都不写**，\n"
        "与'健康时的静默'无法区分。\n"
        "修法：以 UTF-8 with BOM 重新保存该文件。"
    )


@pytest.mark.parametrize("path", PS1_FILES or [SCRIPTS_DIR / "missing.ps1"], ids=_IDS)
def test_ps1_parses_without_error(path: Path) -> None:
    """所有 `.ps1` 必须能被 PowerShell 解析器零错误解析。"""
    if not path.exists():
        pytest.skip("占位参数（没有收集到 .ps1）")
    # 路径里的单引号按 PowerShell 规则转义（Windows 路径通常没有，稳妥起见）
    literal = str(path).replace("'", "''")
    # 先钉住控制台输出编码为 UTF-8：否则 PowerShell 的报错按系统 ANSI(GBK) 写出，
    # 断言信息会变成乱码，看到失败的人根本读不懂原因。
    script = (
        "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); "
        "$e = $null; "
        f"$null = [System.Management.Automation.Language.Parser]::ParseFile('{literal}', "
        "[ref]$null, [ref]$e); "
        "if ($e) { $e | ForEach-Object { $_.Message }; exit 1 } else { exit 0 }"
    )
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        timeout=60,
        creationflags=_NO_CONSOLE,
    )
    out = (completed.stdout or b"").decode("utf-8", errors="replace").strip()
    assert completed.returncode == 0, (
        f"{path.name} 存在 PowerShell 语法/编码错误：\n{out}\n"
        "最常见原因就是缺 UTF-8 BOM（见本文件顶部说明）。"
    )


def test_vbs_launchers_stay_ascii_only() -> None:
    """`.vbs` 启动器必须保持纯 ASCII。

    VBScript 与 `.ps1` 有**同一个编码陷阱**：文件不带 BOM 时，非 ASCII 字节
    会按系统 ANSI 代码页被误解码，可能破坏引号匹配、让脚本静默失效。

    `scripts/frp_watchdog_hidden.vbs` 是"隐藏窗口启动 PowerShell"的关键
    （wscript 无控制台 + `Run(cmd, 0, ...)` 在 CreateProcess 阶段就带 SW_HIDE），
    它一旦失效，每 5 分钟一次的弹窗就会回来。所以这里不要求 BOM，
    而是**从源头要求纯 ASCII**：更简单，也更难被后来的编辑破坏。
    """
    vbs_files = sorted(SCRIPTS_DIR.glob("*.vbs"))
    if not vbs_files:
        pytest.skip("scripts 下没有 .vbs 启动器")
    for path in vbs_files:
        try:
            path.read_bytes().decode("ascii")
        except UnicodeDecodeError as exc:
            raise AssertionError(
                f"{path.name} 含非 ASCII 字节（{exc}）。\n"
                "VBScript 文件一旦失去 BOM，非 ASCII 会被按 ANSI 误解码，"
                "可能破坏引号匹配而使脚本静默失效。\n"
                "请把该文件的注释改为英文，保持纯 ASCII。"
            ) from exc
