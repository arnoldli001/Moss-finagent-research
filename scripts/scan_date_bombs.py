"""扫描"定时炸弹测试"：同一个测试函数里**同时**锚了"运行那一刻"和"写代码那天"。

## 这个脚本要解决的反复出现的问题

同一条 bug 在本仓库出现过 **3 次**：

| 用例 | 现象 |
|---|---|
| `test_connector_router.py::test_non_ranged_flow_unchanged_uses_fresh_db_without_network` | 写死 `2026-09-16`，两天后自己变红 |
| `test_connector_router.py::test_min_date_ignores_malformed_value` | 写死 `2026-09-22`，同样自己变红 |
| `test_tencent_daily_connector.py::test_forming_bar_requested_uses_open_range_first_page` | 用 `date.today()` 断言，fixture 只到 `2026-09-23` |

**机制**：被测代码用 `date.today()` 判数据新鲜度（`_is_db_fresh` 按指数衰减算
confidence，< 0.4 即判 stale 并继续打网络）。测试把 fixture 日期**写死**，
"距离今天几天"就随运行日历漂移：

| 落后天数 | confidence | 结果 |
|---:|---:|---|
| 0~1 | 0.61~0.999 | ✅ 通过 |
| ≥2 | < 0.4 | ❌ 失败 |

写好的当天是过的，**两天后自己变红**，而报错信息
（`assert ['2026-08-31'] == ['2026-09-16']`）完全看不出跟日期有关 ——
于是很容易被当成"别人改坏了"长期挂着。**一条长期红的用例等于没有 CI。**

## 为什么判定要精确到"函数"而不是"文件"

试过两版更粗的规则，都是噪声：

| 规则 | 命中 | 为什么没用 |
|---|---:|---|
| 全测试目录里"距今天 ±45 天的日期" | **1542** | 整套测试数据都在 2026-09 前后，等于全量 |
| 文件里同时有 `today()` 与"±10 天日期" | **247** | `test_connector_router.py` 有 77 处，但其中只有 **1 个**函数用 `today()`，其余全部走 `start_date/end_date` 区间比较 —— **与运行日历无关，永远稳定** |

精确到函数后剩下个位数，每一条都是真需要看的。

## 判定规则

一个 `test_*` 函数被报出，当且仅当它的函数体里**同时**出现：

1. `date.today()` / `datetime.now()`（期望值锚在运行那一刻）；
2. 距今天 **±N 天**（默认 10 天）的日期字面量（fixture 锚在写代码那天）。

只满足一条是安全的：

- 只有 `today()`、没有写死日期 → 期望值与 fixture 一起漂移，自洽；
- 只有写死日期、没有 `today()` → 与运行日历完全无关（冻结录像带、历史基准）。

用法：``python scripts/scan_date_bombs.py [--days 10] [--all]``，退出码 0/1。
显式豁免：在文件里写一行注释 `date-bomb-exempt: <理由>`。
"""

from __future__ import annotations

import ast
import datetime as dt
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
SKIP_PARTS = (".venv", "node_modules", "__pycache__", ".git", "build", "dist")

DATE_LITERAL = re.compile(r"\b(20\d{2})-(\d{2})-(\d{2})\b")
COMPACT_LITERAL = re.compile(r"\b(20\d{2})(\d{2})(\d{2})\b")
EXEMPT_MARKER = "date-bomb-exempt"


def _date_of(text: str) -> dt.date | None:
    for pattern in (DATE_LITERAL, COMPACT_LITERAL):
        match = pattern.search(text)
        if match is None:
            continue
        year, month, day = (int(g) for g in match.groups()[:3])
        try:
            return dt.date(year, month, day)
        except ValueError:
            continue
    return None


def _anchors_on_clock(node: ast.AST) -> bool:
    """函数体里是否出现 `date.today()` / `datetime.now()`。"""
    for child in ast.walk(node):
        if not isinstance(child, ast.Attribute) or child.attr not in ("today", "now"):
            continue
        value = child.value
        if isinstance(value, ast.Name) and value.id in ("date", "datetime"):
            return True
    return False


def _docstring_nodes(tree: ast.AST) -> set[int]:
    """收集所有 docstring 节点的 `id()`，用于排除。

    为什么必须排除：本脚本自己就鼓励在**修好的**测试里写明
    "原先这里写死 `2026-09-16`"，那句话本身含日期字面量 ——
    不排除的话，修好的用例反而会被自己报出来（实测踩过）。
    """
    skip: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if (isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            skip.add(id(first.value))
    return skip


def _recent_literals(node: ast.AST, source: list[str], today: dt.date,
                     days: int, skip: set[int]) -> list[tuple[int, str, int]]:
    """函数体内距今天 ±days 的日期字面量 → [(行号, 文本, 天数差)]。

    只看**字符串常量**且排除 docstring：日期在测试里一定是字符串形式
    （`"2026-09-16"`），注释里的日期不会被 AST 看到，docstring 里的排除掉。
    """
    found: list[tuple[int, str, int]] = []
    for child in ast.walk(node):
        if not isinstance(child, ast.Constant) or not isinstance(child.value, str):
            continue
        if id(child) in skip:
            continue
        value = _date_of(child.value)
        if value is None:
            continue
        delta = (value - today).days
        if abs(delta) > days:
            continue
        lineno = child.lineno
        text = source[lineno - 1].strip() if 0 < lineno <= len(source) else child.value
        found.append((lineno, text[:100], delta))
    return found


def scan(days: int) -> dict[str, list[tuple[str, int, str, int]]]:
    """→ {文件相对路径: [(函数名, 行号, 源码行, 天数差)]}"""
    today = dt.date.today()
    report: dict[str, list[tuple[str, int, str, int]]] = {}

    tests_dir = ROOT / "tests"
    if not tests_dir.exists():
        return report

    for path in sorted(tests_dir.rglob("*.py")):
        if any(part in SKIP_PARTS for part in path.parts):
            continue
        text = path.read_text(encoding="utf-8-sig", errors="ignore")
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        source = text.splitlines()
        skip = _docstring_nodes(tree)
        exempt = _exempt_lines(source)
        relative = str(path.relative_to(ROOT)).replace("\\", "/")

        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not node.name.startswith("test"):
                continue
            if exempt & set(range(node.lineno, (node.end_lineno or node.lineno) + 1)):
                continue
            if not _anchors_on_clock(node):
                continue
            for lineno, snippet, delta in _recent_literals(node, source, today, days, skip):
                report.setdefault(relative, []).append(
                    (node.name, lineno, snippet, delta)
                )
    return report


def _exempt_lines(source: list[str]) -> set[int]:
    """含 `date-bomb-exempt` 的行号 —— 豁免是**函数级**的，不是文件级。

    文件级豁免会把整个文件的其他用例一起放过，实测太粗：
    `test_quant_freshness.py` 里只有 1 个用例是"reviewed 过、确认安全"的，
    其余 500 行不该被它一起豁免。
    """
    return {number for number, line in enumerate(source, 1) if EXEMPT_MARKER in line}


def scan_recent_fixtures(days: int = 3) -> dict[str, list[str]]:
    """第二类：test 函数里出现距今 ±days 的写死日期（不要求测试自己调时钟）。

    静态上**无法**与"合理的近期 fixture"区分 —— 例如一个纯粹的解析测试
    用今天的日期做输入是完全正常的。所以这一类只做提示，不参与退出码，
    目的是把"时钟可能在被测代码里"的那批用例摊开给人看。
    """
    today = dt.date.today()
    report: dict[str, list[str]] = {}
    tests_dir = ROOT / "tests"
    if not tests_dir.exists():
        return report
    for path in sorted(tests_dir.rglob("*.py")):
        if any(part in SKIP_PARTS for part in path.parts):
            continue
        text = path.read_text(encoding="utf-8-sig", errors="ignore")
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        source = text.splitlines()
        skip = _docstring_nodes(tree)
        exempt = _exempt_lines(source)
        relative = str(path.relative_to(ROOT)).replace("\\", "/")
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not node.name.startswith("test"):
                continue
            if exempt & set(range(node.lineno, (node.end_lineno or node.lineno) + 1)):
                continue
            if _recent_literals(node, source, today, days, skip):
                report.setdefault(relative, []).append(node.name)
    return report


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    days = int(args[0]) if args else 10
    show_all = "--all" in sys.argv

    report = scan(days)
    total = sum(len(v) for v in report.values())
    print(f"扫描范围: tests/  阈值: 距今 ±{days} 天")
    print("判定: **同一个 test 函数**里同时出现 `date.today()`/`datetime.now()` 与近期日期字面量")
    print(f"命中: {total} 处 / {len(report)} 个文件\n")

    if report:
        print("⚠️  期望值锚在'运行那一刻'，fixture 却锚在'写代码那天' —— 会随日历漂移：")
        for relative in sorted(report):
            entries = report[relative]
            print(f"\n  {relative}")
            for name, lineno, snippet, delta in entries[: (99 if show_all else 8)]:
                drift = "今天" if delta == 0 else (
                    f"{delta} 天后" if delta > 0 else f"{-delta} 天前"
                )
                print(f"      :{lineno}  {name}()  ← {drift}")
                print(f"              {snippet}")
            if len(entries) > 8 and not show_all:
                print(f"      … 另有 {len(entries) - 8} 处（--all 看全部）")
        print(
            "\n修法：把 fixture 日期改成**相对今天**推导，与期望值的锚对齐。\n"
            "      若该测试确实与运行日历无关，写一行注释豁免：\n"
            f"          # {EXEMPT_MARKER}: <理由>"
        )
    else:
        print("✅ 未发现定时炸弹（第一类：测试自己锚了时钟）。")
        print("   新增测试请用相对今天推导的日期，与期望值的锚对齐：")
        print("       from datetime import date, timedelta")
        print("       today = date.today().isoformat()")

    # ── 第二类：时钟在被测代码里，测试只写死了近期 fixture 日期 ──
    # 这一类**不能自动判定**（fixture 用近期日期本身是合理的），
    # 所以只做提示、不参与退出码。
    tier2 = scan_recent_fixtures(days=3)
    if tier2:
        print(
            f"\n—— 提示（不计入退出码）：{len(tier2)} 个 test 函数用到了"
            f"距今 ±3 天的写死日期 ——"
        )
        print("   时钟可能在被测代码里（如 `_is_db_fresh` 用 `date.today()` 判新鲜度），")
        print("   这类同样会漂移，但静态上无法区分于'合理的近期 fixture'，需人工看。")
        if show_all:
            for relative in sorted(tier2):
                names = ", ".join(sorted(set(tier2[relative])))
                print(f"     {relative}: {names}")
        else:
            print("   （`--all` 列出全部；本轮已知实例：")
            print("     tests/unit/test_connector_router.py::test_min_date_ignores_malformed_value）")

    return 1 if report else 0


if __name__ == "__main__":
    raise SystemExit(main())
