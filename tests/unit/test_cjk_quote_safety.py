"""判据：**我们自己写的每个 `.py` 都必须能被 AST 解析**（这一类自伤已发生 5 次）。

## 它抓的是什么（真实事故，2026-09-28~30）

写中文文案时手滑把 `"` 写进了**同样用 `"` 定界**的中文串里，例如：

    "只覆盖了一条路径：另一端仍会留下"API 在跑、重作业没人跑"的静默状态"

后果是 `SyntaxError: invalid character '、'` —— **整个文件导入失败**，
而报错行指向一句看起来毫无问题的中文，`invalid character` 也从不提示
"是引号用错了"。实测同一类错误在三天内犯了 **5 次**
（探针脚本 ×2、单测 ×2、PRD 表格 ×1），每次都是"下一次运行才发现"。

## 为什么最终只留"能不能解析"这一条（这是量出来的，不是偏好）

第一版判据抓的是**危险形状**："紧贴中文字符的 ASCII 引号"。实测扫全库：

| 形状 | 命中 |
|---|---|
| docstring / 中文行文里的语气引号 | **4012** 处（本仓库既有文风） |
| 运行期字符串（多为补丁脚本里嵌的 JS/CSS 片段） | 141 处（其中 `"登录"` 之类在 JS 里**合法**） |

也就是说"零误报"这个前提**是错的** —— 一条 4012 处误报的判据，下场一定是
被人关掉（判据自己 docstring 里那句警告应验了）。而上面那 5 次事故**没有一次**
是"语法合法但引号难看"：它们**全部**是硬语法错误。

所以判据收敛到唯一既精确又完整的那一条：**文件必须 parse 得动**。
它零误报（合法文件永远能解析），且对那 5 次事故 100% 命中。

## 为什么值得单独一条（`compileall` 不就够了吗）

不够，而且差别正好在这次的痛点上：Python 只在**导入那一刻**才编译，
而 `scripts/` 下的探针、`tests/` 里没被选中的用例，可能几天都不会被导入
——事故就发生在这个窗口里。这条判据把"写坏"到"发现"的距离从**下一次运行**
缩短到**下一次跑测试**。

⚠️ 必须按 `utf-8-sig` 读：本仓库有 3 个文件带 UTF-8 BOM
（`scripts/_backfill_extract.py`、`tests/unit/test_generic_industry_agent.py`、
`tests/unit/test_intel_cross_side.py`），BOM 在 Python 源文件里**合法**，
但用 `utf-8` 读出来会多一个 U+FEFF 字符、被 `ast.parse` 判成
`invalid non-printable character` —— 那是判据自己制造的假故障。
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

#: 扫哪些（我们自己写的全部 Python 源）
SCAN_DIRS = ("src", "tests", "scripts", "manage.py")

#: 跳过第三方与生成物
SKIP_PARTS = {".venv", "node_modules", "__pycache__", "dist", "dist-pilot",
              "data", ".git", ".ruff_cache", ".pytest_cache", "build"}


def _iter_py_files() -> list[Path]:
    out: list[Path] = []
    for entry in SCAN_DIRS:
        target = ROOT / entry
        if target.is_file():
            out.append(target)
            continue
        for path in target.rglob("*.py"):
            if any(part in SKIP_PARTS for part in path.parts):
                continue
            out.append(path)
    return sorted(set(out))


def test_every_owned_python_file_parses() -> None:
    files = _iter_py_files()
    # 判据自己也要防"静默变绿"：扫描根写错时 rglob 会安静地返回空
    assert len(files) > 500, f"只扫到 {len(files)} 个文件 —— 扫描根是不是错了？"

    broken: list[str] = []
    for path in files:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
        try:
            ast.parse(text, filename=str(path))
        except SyntaxError as exc:
            line = (text.splitlines() or [""])[max(0, (exc.lineno or 1) - 1)]
            broken.append(f"{path.relative_to(ROOT)}:{exc.lineno} {exc.msg}\n"
                          f"      {line.strip()[:90]}")

    assert not broken, (
        "有文件**根本解析不了** —— 内容不会被导入/执行，而症状只是"
        "「某个脚本没反应」。最常见的原因：中文串里混进了 ASCII 引号"
        "（换成「」即可）。\n  " + "\n  ".join(broken[:20]))


def test_scanner_reads_bom_files_fine() -> None:
    """带 BOM 的文件**必须**被判为合法（否则判据会自己制造假故障）。

    `read_text(encoding="utf-8")` 会把 BOM 留成 U+FEFF 从而解析失败 ——
    第一版就是这么把 3 个文件错报成"语法不合法"的。这条把它钉住。
    """
    bom_files = [p for p in _iter_py_files()
                 if p.read_bytes()[:3] == b"\xef\xbb\xbf"]
    assert bom_files, "本仓库确实有带 BOM 的 .py；若已全部去掉，请删掉这条判据"
    for path in bom_files:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
        ast.parse(text, filename=str(path))     # 不抛即通过


def test_the_real_incident_shape_is_a_syntax_error() -> None:
    """**自证**：判据必须真的抓得到历史上那次真实写法。

    为什么判据自己要带这条：如果"能不能解析"这条路其实抓不到那类错误，
    它就成了永远绿的假护栏 —— 而假护栏比没有护栏更糟（它让人以为挡住了）。
    """
    incident = (
        '    assert src.count("ensure_worker(") >= 2, (\n'
        '        "只覆盖了一条路径：另一端仍会留下"API 在跑、重作业没人跑"的静默状态")'
    )
    try:
        ast.parse(incident)
    except SyntaxError:
        return                      # 命中：正是这条判据要挡下的形状
    raise AssertionError("历史事故写法竟然解析得动 —— 判据抓不到它，需要换判据")


def test_full_width_quotes_are_legal() -> None:
    """合法写法（正确的改法）必须能解析，否则判据会把人逼向别的形状。"""
    fixed = 'ok = "只覆盖了一条路径：仍会留下「API 在跑」的静默状态"'
    ast.parse(fixed)


def test_scan_roots_exist() -> None:
    for entry in SCAN_DIRS:
        assert (ROOT / entry).exists(), f"扫描根不存在：{entry}"
