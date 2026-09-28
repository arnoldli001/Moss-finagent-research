"""从「本次改了哪些文件」推出「该跑哪些测试」。

## 为什么需要它（2026-09-28 用户的质问）

> 「为什么你刚刚做了全量测试，是什么规则让你这么做的？加一条护栏规则，
>   只有提交github时才跑全量测试，其他场景只运行本次修改相关的单元测试」

**诚实回答：没有任何规则让我跑全量。** 是我的习惯 —— 根因是
**我不知道哪些测试和改动相关**，全量是"不知道"时唯一能保证不漏的做法。

代价是可量化的：本轮跑了 **8 次全量 × 约 11 分钟 ≈ 90 分钟**纯等待。

而"只跑相关测试"这条规则，**如果不解决"哪些算相关"，就只是把风险转嫁给
下一个人**（他会因为怕漏而继续跑全量）。所以本脚本存在，规则才立得住。

## 判据（三层，都是可判定的）

| 层 | 规则 |
|---|---|
| ① 直接引用 | 测试 import 了这个模块（`ast` 解析，不猜） |
| ② 传递引用 | 有模块 import 了它，而测试 import 了那个模块（有界闭包） |
| ③ 资源引用 | 测试里出现了这个**路径字符串**（如 `configs/models.yaml`） |

## 用法

    uv run python scripts/affected_tests.py                 # 看本次改动影响哪些测试
    uv run python scripts/affected_tests.py --run           # 直接跑它们
    uv run python scripts/affected_tests.py --files a.py b.py   # 指定文件（不依赖 git）
    uv run python scripts/affected_tests.py --explain       # 说明每个测试为什么被选中

⚠️ **什么时候仍然要跑全量**：提交/推送前、改的是**跨模块契约**
（`TaskTier` 字面量、`_AGENT_DATA_WHITELIST`、`NEW_INDICATOR_TOUCHPOINTS`
这类被大量测试引用的东西）时。本脚本会在检测到这类"高扇出文件"时**主动提示**，
而不是让你自己记得。
"""
from __future__ import annotations

import argparse
import ast
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

#: 高扇出文件：改到它们就别省，直接全量。
#: 判据不是"重要性"，而是**被引用的测试数量**（本脚本会实测并提示）。
HIGH_FANOUT_HINT = (
    "src/infrastructure/llm/models.py",      # TaskTier 字面量
    "src/orchestration/supervisor.py",       # 白名单 / 贯通点清单
    "configs/models.yaml",                   # 每层路由
)


def _changed_files() -> list[str]:
    """本次改动（含未跟踪的新文件）。不依赖 git 时返回空。"""
    try:
        out: set[str] = set()
        for args in (("diff", "--name-only", "HEAD"),
                     ("ls-files", "--others", "--exclude-standard")):
            r = subprocess.run(("git", *args), cwd=ROOT, capture_output=True,
                               text=True, timeout=30, check=False)
            if r.returncode == 0:
                out.update(ln.strip() for ln in r.stdout.splitlines() if ln.strip())
        return sorted(out)
    except (OSError, subprocess.SubprocessError):
        return []


def _module_of(rel: str) -> str | None:
    """`src/a/b.py` → `src.a.b`；非 .py 返回 None。"""
    if not rel.endswith(".py"):
        return None
    return rel[:-3].replace("/", ".")


def _imports_in(path: Path) -> set[str]:
    """该文件 import 的**本仓库**模块全名（ast，不猜）。"""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    except (SyntaxError, OSError):
        return set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
            for a in node.names:            # from src.a import b → src.a.b
                names.add(f"{node.module}.{a.name}")
        elif isinstance(node, ast.Import):
            for a in node.names:
                names.add(a.name)
    return names


def _build_index() -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """→ (`{测试文件: 其引用的模块}`，`{测试文件: 其文本里出现的路径}`)。"""
    mods: dict[str, set[str]] = {}
    paths: dict[str, set[str]] = {}
    for p in sorted(ROOT.glob("tests/**/*.py")):
        rel = p.relative_to(ROOT).as_posix()
        mods[rel] = _imports_in(p)
        text = p.read_text(encoding="utf-8-sig", errors="replace")
        # 资源引用：`configs/models.yaml` 这类路径字符串
        paths[rel] = {t for t in (
            "configs/models.yaml", "configs/indicators.yaml",
            "configs/intraday.yaml", "configs/alert_rules.yaml",
        ) if t in text}
    return mods, paths


def _src_importers() -> dict[str, set[str]]:
    """`{被依赖模块: {依赖它的 src 模块}}` —— 用于传递闭包。"""
    graph: dict[str, set[str]] = {}
    for p in sorted(ROOT.glob("src/**/*.py")):
        me = _module_of(p.relative_to(ROOT).as_posix())
        if not me:
            continue
        for dep in _imports_in(p):
            graph.setdefault(dep, set()).add(me)
    return graph


def affected(changed: list[str], *, explain: bool = False) -> list[str]:
    mods, paths = _build_index()
    graph = _src_importers()

    changed_mods = {m for m in (_module_of(f) for f in changed) if m}
    changed_paths = {f for f in changed if not f.endswith(".py")}

    # ② 传递闭包（有界：3 层，足够覆盖本仓库的分层深度）
    closure = set(changed_mods)
    frontier = set(changed_mods)
    for _ in range(3):
        nxt: set[str] = set()
        for m in frontier:
            nxt |= graph.get(m, set())
        nxt -= closure
        if not nxt:
            break
        closure |= nxt
        frontier = nxt

    hit: dict[str, str] = {}
    for test, deps in mods.items():
        for m in closure:
            # 精确匹配 + 包前缀匹配（`from src.a import b` 与 `import src.a.b`）
            if m in deps or any(d == m or d.startswith(m + ".") for d in deps):
                hit[test] = f"imports {m}"
                break
        else:
            for cp in changed_paths:
                if cp in paths.get(test, set()):
                    hit[test] = f"reads {cp}"
                    break
    # 改动本身就是测试文件 → 它自己
    for f in changed:
        if f.startswith("tests/") and f.endswith(".py") and (ROOT / f).exists():
            hit.setdefault(f, "changed itself")

    if explain:
        for t in sorted(hit):
            print(f"  {t:<52} ← {hit[t]}")
    return sorted(hit)


def main() -> int:
    ap = argparse.ArgumentParser(description="本次改动影响哪些测试")
    ap.add_argument("--files", nargs="*", default=None,
                    help="指定改动文件（默认取 git 改动）")
    ap.add_argument("--run", action="store_true", help="直接跑选中的测试")
    ap.add_argument("--explain", action="store_true", help="说明每个测试为何被选中")
    args = ap.parse_args()

    changed = args.files if args.files is not None else _changed_files()
    print("=" * 84)
    print(f"本次改动 {len(changed)} 个文件")
    print("=" * 84)
    for f in changed:
        print(f"  {f}")

    tests = affected(changed, explain=args.explain)
    print(f"\n相关测试 {len(tests)} 个文件：")
    for t in tests:
        print(f"  {t}")

    risky = [f for f in changed if f in HIGH_FANOUT_HINT]
    if risky:
        print("\n⚠️ **高扇出文件**被改动，建议仍然跑全量：")
        for f in risky:
            print(f"  {f}")
        print("   （这类文件（层字面量 / 白名单 / 每层路由）被大量测试引用，"
              "只跑相关测试会漏）")

    if not tests:
        print("\n（没有选中任何测试 —— 若改动的是纯文档/脚本，这是正常的；"
              "否则请检查 --files）")
        return 0

    if args.run:
        cmd = ["python", "-m", "pytest", *tests, "-q", "--no-header",
               "-p", "no:cacheprovider"]
        print(f"\n运行：{' '.join(cmd)}\n")
        return subprocess.call(cmd, cwd=ROOT)
    print("\n（加 --run 直接执行）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
