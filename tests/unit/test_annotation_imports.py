"""注解里用了**未导入**的名字 ⇒ 导入即 `NameError`（判据）。

## 这条判据是怎么来的（`CHG-0192`）

我新增 `PROVIDER_DECLARATIONS: Final[...]` 时**忘了 `from typing import Final`**。
模块级变量注解在**导入时求值** ⇒ `import providers` 直接 `NameError`
⇒ 整个 LLM 层起不来。

当时是 `test_no_undefined_names`（走 `ruff F821`）抓到的。但那条判据是
**全库扫描 + 依赖 ruff**：万一某类形态 ruff 不报（注解里的某些写法），
就没人管了。所以这里补一条**自带 AST 实现**的判据 —— 不依赖外部 linter，
且专门盯"注解"这个最容易漏的形态。

## 与 `test_no_undefined_names` 的分工

| 判据 | 手段 | 盯什么 |
|---|---|---|
| `test_no_undefined_names` | `ruff --select F821` | **任意位置**的未定义名（更广） |
| 本文件 | 自带 AST | **注解**里的未导入名（更专，且不依赖 ruff 存在） |

两条都在，是因为"更广的那条依赖外部工具" —— 工具缺失时它只能跳过，
而注解这个形态**必然**在导入期炸、值得有一条不依赖工具的兜底。

## 判据强度

`test_scanner_catches_a_missing_import` —— **自证**：拿一段"注解用了
未导入名"的合成源码喂给扫描函数，必须被报出来。删掉扫描逻辑 ⇒ 立刻红。
"""

from __future__ import annotations

import ast
import builtins
import textwrap
from pathlib import Path

BUILTINS = set(dir(builtins))

ROOTS = (Path("src"), Path("tests"))


# ======================================================================
# 扫描实现（本文件自带，不依赖 ruff）
# ======================================================================

def _imported_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                names.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                names.add(a.asname or a.name)
    return names


def _defined_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
    return names


def _annotation_names(ann: ast.AST | None) -> set[str]:
    out: set[str] = set()
    if ann is None:
        return out
    for node in ast.walk(ann):
        if isinstance(node, ast.Name):
            out.add(node.id)
        elif isinstance(node, ast.Attribute):
            cur: ast.AST = node
            while isinstance(cur, ast.Attribute):
                cur = cur.value
            if isinstance(cur, ast.Name):
                out.add(cur.id)
    return out


def scan_source(source: str, label: str = "<src>") -> list[str]:
    """返回问题清单（空 = 通过）。"""
    tree = ast.parse(source)
    have = _imported_names(tree) | _defined_names(tree) | BUILTINS
    problems: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign):
            for n in sorted(_annotation_names(node.annotation) - have):
                problems.append(f"{label}:{node.lineno}: 注解用了未导入的 {n}")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for a in list(node.args.args) + list(node.args.kwonlyargs):
                for n in sorted(_annotation_names(a.annotation) - have):
                    problems.append(f"{label}:{node.lineno}: 参数注解用了未导入的 {n}")
            for n in sorted(_annotation_names(node.returns) - have):
                problems.append(f"{label}:{node.lineno}: 返回注解用了未导入的 {n}")
    return problems


# ======================================================================
# ★ 自证
# ======================================================================

def test_scanner_catches_a_missing_import():
    """★ 自证：注解用了未导入名 ⇒ 必须被报出来（否则本判据是装饰）。"""
    bad = textwrap.dedent("""
        DECL: Final[dict[str, int]] = {}          # Final 没导入

        def f(x: MissingType) -> AlsoMissing:     # 两个都没导入
            return x
    """)
    found = scan_source(bad, "probe")
    assert any("Final" in p for p in found), f"漏报变量注解：{found}"
    assert any("MissingType" in p for p in found), f"漏报参数注解：{found}"
    assert any("AlsoMissing" in p for p in found), f"漏报返回注解：{found}"


def test_scanner_does_not_flag_correct_code():
    """反向：正确导入了就不许报（否则判据会逼人加无用 import）。"""
    good = textwrap.dedent("""
        from typing import Final
        from src.core.schemas import DataPoint

        DECL: Final[dict[str, int]] = {}

        def f(x: DataPoint, y: "Final[int]" = None) -> str:
            return ""
    """)
    assert scan_source(good, "probe") == []


def test_scanner_accepts_locally_defined_names():
    """本模块定义的类/变量也算"有来源"。"""
    local = textwrap.dedent("""
        class Thing: ...

        INSTANCE: Thing | None = None
    """)
    assert scan_source(local, "probe") == []


# ======================================================================
# 全库
# ======================================================================

def test_no_missing_type_imports_in_repo():
    """★ 全库扫描：`src/` 与 `tests/` 下不许有"注解用了未导入名"。

    ⚠️ 用 `utf-8-sig` 读：本仓有带 BOM 的文件（BOM 是文件隐藏属性，
    肉眼与 diff 都看不见），`ast.parse` 直接吃带 BOM 的字符串会报
    `invalid non-printable character U+FEFF` —— 那是**误报**，不是缺陷。
    """
    problems: list[str] = []
    scanned = 0
    for root in ROOTS:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.py")):
            if "__pycache__" in str(path):
                continue
            text = path.read_text(encoding="utf-8-sig")
            try:
                problems += scan_source(text, str(path))
            except SyntaxError as exc:            # 语法错由别的判据负责
                problems.append(f"{path}: 语法错误 {exc}")
            scanned += 1
    assert scanned > 100, f"只扫到 {scanned} 个文件 —— 扫描面疑似失效"
    assert not problems, (
        "以下位置在**导入时**就会 `NameError`（模块级注解在导入期求值）：\n  "
        + "\n  ".join(problems[:20])
        + "\n修法：把名字导入上，或删掉那行注解。**不许**用 `# type: ignore` 压掉。"
    )
