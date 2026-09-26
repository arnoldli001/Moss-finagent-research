"""交付前的自查：把"我说改了"变成"你可以自己跑一遍"。

用法：``.venv\\Scripts\\python.exe scripts\\verify_cleanup.py``
退出码 0 = 全部通过，1 = 有项不达标（可直接接进 CI）。

## 扫描范围为什么不是"整个仓库"

范围 = **会被 ship 出去的代码**：`src/`、`tests/`、`scripts/`、顶层包。
下面几类**显式排除**并写明理由 —— 排除项必须在代码里看得见，
否则"扫描通过"就变成"我少扫了几个目录"：

| 排除 | 理由 |
|---|---|
| `.venv` / `node_modules` / `__pycache__` / `build` / `dist` | 第三方与构建产物，不是我们的代码 |
| `data/` | 运行时数据目录，还归档了一次性探针（`data/archive/files/root_probe/`），不参与运行 |
| `scripts/_*.py` | `.gitignore` 明列的本地临时探针，用完即弃，不入库 |
| `.git/` | 版本库内部 |
"""

from __future__ import annotations

import ast
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
SKIP_PARTS = (".venv", "node_modules", "__pycache__", ".git", "build", "dist")
#: 运行时数据目录（含归档的一次性探针脚本）
SKIP_DIRS = ("data",)
#: `.gitignore` 里明列的临时探针：`/scripts/_probe_*.py`、`/_tmp_*.py`
SKIP_FILE_PREFIXES = ("_",)

TRUNCATED = re.compile(r"str\(exc\)\[:\s*\d+\s*\]")
ABS_PATH = re.compile(r"D:\\code\\Moss-finagent-research")
SESSION_ARITH = re.compile(r"\b(?:9|11|13|15)\s*\*\s*60\s*(?:\+|\b)")
SESSION_HOME = "core/trading_session.py"

#: `git ls-files` 结果缓存（每个文件一次，避免重复起进程）
_TRACKED_CACHE: dict[str, bool] = {}
_GIT_AVAILABLE: bool | None = None


def _tracked(relative: str) -> bool:
    """文件是否已被 git 跟踪。

    `verify_cleanup` 要回答的是"**会 ship 出去的代码**干不干净" ——
    这正是 CI 能看到的东西（CI 只 checkout 已提交的文件）。
    别人正在写、还没 `git add` 的文件不该让本次检查变红，
    否则每次并发编辑都会把门禁变成噪声，最后没人看它。
    git 不可用时**一律按已跟踪处理**（宁可多报，不可漏报）。
    """
    cached = _TRACKED_CACHE.get(relative)
    if cached is not None:
        return cached
    if _GIT_AVAILABLE is False:
        return True
    try:
        result = subprocess.run(  # noqa: S603
            ["git", "ls-files", "--error-unmatch", relative],  # noqa: S607
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
        )
    except OSError:
        return True
    tracked = result.returncode == 0
    _TRACKED_CACHE[relative] = tracked
    return tracked


def _excluded(path: pathlib.Path) -> bool:
    if any(part in SKIP_PARTS for part in path.parts):
        return True
    if any(part in SKIP_DIRS for part in path.parts):
        return True
    # 只对 `scripts/` 下的临时探针生效，`src/` 里的 `_private.py` 照常检查
    if path.parent.name == "scripts" and path.name.startswith(SKIP_FILE_PREFIXES):
        return True
    relative = str(path.relative_to(ROOT)).replace("\\", "/") if path.is_relative_to(ROOT) else None
    return relative is not None and not _tracked(relative)


def py_files() -> list[pathlib.Path]:
    return [path for path in sorted(ROOT.rglob("*.py")) if not _excluded(path)]


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
    """ruff 的**诊断结果**按 `py_files()` 的范围过滤。

    不直接 `ruff check .`：那会把 `data/` 里归档的一次性探针与
    `scripts/_*.py` 临时脚本也算进来（实测 900+ 条），噪声淹没真信号。
    也不靠 `ruff --exclude`：多级排除的 glob 语义容易与自己的判定不一致，
    干脆让 ruff 全量跑、由本脚本用**同一套 `_excluded()`** 过滤，
    范围只有一个定义处。
    """
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "ruff", "check", ".", "--output-format", "concise"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode == 0:
        return []

    problems = []
    for line in (result.stdout or result.stderr).splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        # ruff 的页脚汇总（`Found N errors` / `[*] N fixable` / `No fixes available`）
        # 说的是**全仓**数量，与过滤后的范围不符，丢掉以免误导。
        if re.match(r"^(Found \d+ errors?|\[\*\] \d+ fixable|No fixes available)", stripped):
            continue
        # 形如 `src\core\errors.py:16:73: E501 Line too long (101 > 100)`
        match = re.match(r"^([^:]+):(\d+):(\d+): (\S+ .*)$", stripped)
        if match is None:
            # 不是诊断行也不是已知页脚 —— 可能是 ruff 自身报错，保留以免吞掉
            problems.append(stripped)
            continue
        raw_path, lineno, _col, message = match.groups()
        path = pathlib.Path(raw_path)
        if not path.is_absolute():
            path = ROOT / path
        if _excluded(path):
            continue
        problems.append(f"{raw_path}:{lineno}: {message}")
    return problems


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


def check_date_bombs() -> list[str]:
    """定时炸弹测试：期望值锚在"运行那一刻"，fixture 却锚在"写代码那天"。

    同一类 bug 在仓库里出现过 **3 次**（详见 `scan_date_bombs.py` docstring），
    而且每次的表现都是"写入当天通过、两天后自己变红"，报错信息看不出与日期有关 ——
    于是被当成"既有无关失败"长期挂着。**一条长期红的用例等于没有 CI。**
    """
    result = subprocess.run(  # noqa: S603
        [sys.executable, str(ROOT / "scripts" / "scan_date_bombs.py")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode == 0:
        return []
    output = result.stdout or ""
    problems = [line.strip() for line in output.splitlines() if line.startswith("      :")]
    return problems or [output.strip().splitlines()[0] if output.strip() else "存在定时炸弹"]


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
        ("定时炸弹测试（写死近期日期的用例）", check_date_bombs()),
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
