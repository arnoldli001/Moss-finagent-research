"""交付前的自查：把"我说改了"变成"你可以自己跑一遍"。

用法：``.venv\\Scripts\\python.exe scripts\\verify_cleanup.py``
退出码 0 = 全部通过，1 = 有项不达标（可直接接进 CI）。
"""

from __future__ import annotations

import ast
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
SKIP_PARTS = (".venv", "node_modules", "__pycache__", ".git", "build", "dist")

TRUNCATED = re.compile(r"str\(exc\)\[:\s*\d+\s*\]")
ABS_PATH = re.compile(r"D:\\code\\Moss-finagent-research")
SESSION_ARITH = re.compile(r"\b(?:9|11|13|15)\s*\*\s*60\s*(?:\+|\b)")
SESSION_HOME = "core/trading_session.py"


def py_files() -> list[pathlib.Path]:
    return [
        path
        for path in sorted(ROOT.rglob("*.py"))
        if not any(part in SKIP_PARTS for part in path.parts)
    ]


def check_syntax() -> list[str]:
    problems = []
    for path in py_files():
        try:
            ast.parse(path.read_text(encoding="utf-8-sig"))
        except SyntaxError as exc:
            problems.append(f"{path.relative_to(ROOT)}:{exc.lineno} {exc.msg}")
    return problems


def grep(pattern: re.Pattern[str], *, allow: tuple[str, ...] = ()) -> list[str]:
    hits = []
    for path in py_files():
        relative = str(path.relative_to(ROOT)).replace("\\", "/")
        if any(relative.endswith(suffix) for suffix in allow):
            continue
        text = path.read_text(encoding="utf-8-sig", errors="ignore")
        for number, line in enumerate(text.splitlines(), 1):
            if pattern.search(line):
                hits.append(f"{relative}:{number}")
    return hits


def check_ruff() -> list[str]:
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "ruff", "check", "."],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode == 0:
        return []
    return (result.stdout or result.stderr).strip().splitlines()


def check_same_name_conflicts() -> list[str]:
    result = subprocess.run(  # noqa: S603
        [sys.executable, str(ROOT / "scripts" / "scan_same_name_conflicts.py"), "src"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    output = result.stdout or ""
    marker = re.search(r"待处理\s*(\d+)", output)
    if marker is None:
        return [f"扫描脚本输出无法解析：{output.strip()[:120]}"]
    count = int(marker.group(1))
    if count == 0:
        return []
    return [line.strip() for line in output.splitlines() if line.startswith("⚠️")]


def main() -> int:
    checks: list[tuple[str, list[str]]] = [
        ("Python 语法", check_syntax()),
        ("残留 str(exc)[:N]", grep(TRUNCATED)),
        ("残留硬编码本机绝对路径", grep(ABS_PATH, allow=(
            # 测试断言与文档字符串里出现的路径不是"配置"，允许保留
            "tests/unit/test_manage_no_console_window.py",
            "src/quant/quant_select_service.py",
        ))),
        ("交易时段算术字面量只应出现在权威模块", grep(SESSION_ARITH, allow=(
            SESSION_HOME,
            "core/market_constants.py",
            # 测试用例本身要用分钟数构造时刻，那里的 `hour * 60 + minute` 是数据不是口径
            "tests/unit/test_trading_session.py",
        ))),
        ("ruff", check_ruff()),
        ("同名不同值常量", check_same_name_conflicts()),
    ]

    failed = 0
    for title, problems in checks:
        if problems:
            failed += 1
            print(f"[FAIL] {title} —— {len(problems)} 项")
            for item in problems[:10]:
                print(f"        {item}")
            if len(problems) > 10:
                print(f"        … 另有 {len(problems) - 10} 项")
        else:
            print(f"[ OK ] {title}")

    print()
    print("全部通过 ✅" if not failed else f"{failed} 项不达标 ❌")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
