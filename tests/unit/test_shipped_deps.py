"""交付完整性：**会入库的测试，不许依赖不会入库的文件**。

## 为什么需要它（2026-09-28 实测）

`scripts/` 在本仓库**默认不发布**：

    .gitignore:83  # scripts 默认全部不发布，仅白名单以下 16 个公开脚本
    .gitignore:84  /scripts/*

而 `tests/` 是发布的。于是产生了这个**在开发机上完全看不见**的缺陷：

    tests/unit/test_preflight_check.py   ← 会入库
        from scripts import preflight_check   ← 不会入库

**开发机全绿，克隆后必然红灯。** 本地 5212 passed 一条都不会提醒你。

这类问题的共同形状是：**A 引用 B，但 A 发布、B 不发布。** 本测试把
"引用"和"发布"两个事实对齐，而不是逐个手工检查。

## 判据（三条，都可执行）

1. 发布的测试引用的 `scripts.<name>` → 该脚本必须**存在**（否则连开发机都跑不了）
2. 该脚本若**未被 git 跟踪** → 引用它的测试**必须**有 `pytest.skip` 守卫
   （`allow_module_level=True`），即"知名地降级"而不是"静默地红"
3. 仓库引用文档（AGENTS.md / docs）里的 `scripts/...` 命令 → 必须存在

## 为什么判据 2 是"必须有 skip 守卫"而不是"必须入库"

因为**发布与否是政策，不是工程判断**。`scripts/` 不入库是仓库既定政策
（研究/运维/验证脚本不进公开仓库）。工程侧能做的是：**让缺失变得不致命
且可见** —— 而不是偷偷把私有脚本推进公开仓库。

> 顺带：`git check-ignore` 对**已跟踪**文件不作答（tracked 覆盖 ignore），
> 所以判据必须用 `git ls-files` 判"是否跟踪"，不能用 `check-ignore` 判"是否发布"。
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

#: ⚠️ 这里**曾经**用正则解析 import，实测踩了三个坑后改成了 `ast`
#: （见 `parse_script_imports`）：① 捕获组编号写错 → `IndexError`；
#: ② `[\\w,\\s]+` 里的 `\\s` 含换行 → **跨行贪婪吞并**后续 import，
#: 3 条 import 只解析出 1 个名字；③ 文档字符串里的示例代码被当成真引用。
#: **教训：判断"代码里有什么"要用语法树，不要用正则。**

#: 匹配**真实调用**语法：`python scripts/x.py` / `uv run python scripts/x.py`。
#: 只认调用，不认提及 —— 文档里 "**新增** `scripts/x.py`" 是待办提案，
#: 拿它当"不存在的命令"报错就是误报（本轮实测踩过）。
_CALL_RE = re.compile(
    r"(?:uv\s+run\s+)?python\s+(?:-\w+\s+)*scripts[/\\]([A-Za-z_]\w*)\.py")

#: 占位符 / 示例名 —— **不是真命令**，判据 3 必须排除。
#: 实测教训（2026-09-28）：文档里写 `python scripts/x.py` 这种**占位符**
#: 被当成真命令，报出一个不存在的脚本 `x`；于是人们开始**绕道改文案**，
#: 而不是修判据。**判据要适应文档，不能让文档迁就判据。**
_PLACEHOLDER_NAMES: frozenset[str] = frozenset({
    "x", "y", "z", "xxx", "yyy", "zzz", "script", "scripts", "your_script",
    "yourscript", "name", "foo", "bar", "baz", "example", "sample", "path",
    "file", "filename", "some_script", "my_script", "tmp", "placeholder",
})


def _is_placeholder(name: str) -> bool:
    """单字母 或 常见占位词 → 不是真命令。"""
    return len(name) <= 1 or name.lower() in _PLACEHOLDER_NAMES

#: 已知的"会入库的测试引用了不会入库的脚本"——**在飞项，显式登记而非放过**。
#: 红灯纪律：红灯只有三种合法归宿（修好 / 删除并说明 / **显式标记为预期差异并
#: 链接到具体待办**）。这里是第三种。
#: ⚠️ 这些是**并发协作者正在写的文件**（写入时间与检查时间同分钟），
#: 按 AGENTS.md「避开对方正在改的文件」不由我代改。
#: 待办：由作者在对应测试顶部加模块级 skip 守卫（见本文件顶部说明）。
#:
#: 2026-09-28：`prd_sync_check` 已由其作者补上守卫
#: （`tests/unit/test_prd_sync_check.py` 顶部 `pytest.skip(..., allow_module_level=True)`
#: —— 由对方本人改动，非本文件的维护者代改），故条目已删除。
#: 空表是**期望状态**：`test_known_unguarded_entries_are_not_stale` 会保证
#: 这里不会重新长成"永久豁免"。
_KNOWN_UNGUARDED: dict[str, str] = {}

#: 已知的历史悬空引用：文档描述的是**已执行并删除**的一次性脚本。
#: 红灯纪律要求"显式标记为预期差异**并链接到具体待办**"，所以列在这里而不是放过。
#: 待办：这些文档描述的是历史某次分析，重跑无意义；若要复现需重写脚本。
_KNOWN_DANGLING_CALLS: frozenset[str] = frozenset({
    "_dbg_hot", "_diag_562900", "_diag_etf_coverage", "_diag_layer_coverage",
    "_diag_list_date", "_diag_seat_leak", "_diag_window_readiness",
    "mainline_restore_boards",
})


def _git(*args: str) -> subprocess.CompletedProcess[str] | None:
    """跑 git；不在仓库里 / 没装 git 时返回 None（调用方跳过）。"""
    try:
        return subprocess.run(("git", *args), cwd=ROOT, capture_output=True,
                              text=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        return None


def _is_git_repo() -> bool:
    r = _git("rev-parse", "--is-inside-work-tree")
    return bool(r and r.returncode == 0 and "true" in r.stdout)


def _tracked(path: str) -> bool | None:
    """该路径是否被 git 跟踪。None = 判不了。"""
    r = _git("ls-files", "--error-unmatch", path)
    if r is None:
        return None
    return r.returncode == 0


def parse_script_imports(text: str) -> set[str]:
    """从源码里解析出被引用的**脚本模块名**。

    ★ 用 `ast` 而不是正则。正则版本实测踩了三个坑（全部静默少算/误报）：
      ① 捕获组编号写错 → `IndexError`
      ② `[\\w,\\s]+` 里的 `\\s` 含换行 → 正则**跨行贪婪吞并**后续 import，
         3 条 import 只解析出 1 个名字
      ③ 文档字符串里的示例代码被当成真引用（本文件自己的说明文字就被误报成
         `scripts/preflight_check不会入库`）
    `ast` 一次解决全部三个：只认真实语法树，不认注释/字符串/换行。

    语法错误时返回空集（此类文件由其它测试负责，不该在这里二次报错）。
    """
    import ast as _ast

    try:
        tree = _ast.parse(text)
    except SyntaxError:
        return set()

    names: set[str] = set()
    for node in _ast.walk(tree):
        if isinstance(node, _ast.ImportFrom):
            mod = node.module or ""
            if mod == "scripts":                    # from scripts import a, b
                names.update(a.name for a in node.names)
            elif mod.startswith("scripts."):        # from scripts.x import y
                names.add(mod.split(".", 1)[1].split(".")[0])
            elif not mod and node.level:            # from . import x（相对导入）
                continue
        elif isinstance(node, _ast.Import):         # import scripts.x
            for a in node.names:
                if a.name.startswith("scripts."):
                    names.add(a.name.split(".", 1)[1].split(".")[0])
    return {n for n in names if n.isidentifier()}


def _referenced_scripts() -> dict[str, set[str]]:
    """`{脚本名: {引用它的文件}}`，只看**会入库**的测试/文档。"""
    refs: dict[str, set[str]] = {}
    for p in ROOT.glob("tests/**/*.py"):
        rel = p.relative_to(ROOT).as_posix()
        text = p.read_text(encoding="utf-8-sig", errors="replace")
        for n in sorted(parse_script_imports(text)):
            if n and n != "*":
                refs.setdefault(n, set()).add(rel)
    return refs


def _has_skip_guard(test_file: Path) -> bool:
    """该测试是否有模块级 skip 守卫（脚本缺失时不红）。"""
    text = test_file.read_text(encoding="utf-8-sig", errors="replace")
    return bool(re.search(
        r"pytest\.skip\([^)]*allow_module_level\s*=\s*True", text, re.DOTALL))


def _missing_referenced_scripts() -> dict[str, list[str]]:
    """判据 1 的计算：**会发布**却被引用、却又不存在的脚本。

    ⚠️ 例外的理由（2026-09-28 修，CHG-0049）：`.gitignore` 排除的脚本
    （`/scripts/*` 不在白名单内）在**克隆里本就不存在**，按"存在性"判会让
    判据 1 在克隆/CI 里**必红** —— 而判据 2 的 `pytest.skip` 守卫只解了
    "引用方不红"，解不了这一条（判据 2 用 `_tracked` 判，判据 1 当时没有）。

    这条豁免**自带过期语义**：脚本一旦入库，`_tracked()` 变 True，存在性检查
    自动恢复，**不需要维护豁免名单**（名单会腐烂成永久豁免）。
    非 git 环境（`_tracked()` → None）保持原语义：要求存在。
    """
    out: dict[str, list[str]] = {}
    for name, files in _referenced_scripts().items():
        path = f"scripts/{name}.py"
        if (ROOT / path).exists():
            continue
        if _tracked(path) is False:          # 未发布 ⇒ 克隆里本就不存在
            continue
        out[name] = sorted(files)
    return out


def test_referenced_scripts_exist_on_disk() -> None:
    """判据 1：测试引用的**会发布**的脚本必须存在（否则开发机也跑不了）。"""
    missing = _missing_referenced_scripts()
    assert not missing, (
        f"这些测试引用了不存在的**已发布**脚本，开发机也会红：{missing}")


def test_existence_exemption_is_not_a_permanent_green(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """★ 自证：豁免不能把判据 1 变成"永远绿"。

    · 未跟踪脚本缺失 → 豁免（克隆里本就不存在，不该报）
    · 已跟踪脚本缺失 → **必须报**（改名 / 打错字的现场就靠这一条抓）
    """
    monkeypatch.setattr(
        sys.modules[__name__], "_referenced_scripts",
        lambda: {"ghost_script_zzz": {"tests/unit/x.py"}})
    monkeypatch.setattr(sys.modules[__name__], "_tracked", lambda _p: False)
    assert _missing_referenced_scripts() == {}, \
        "未发布脚本不该按存在性报错（否则克隆必红）"

    monkeypatch.setattr(sys.modules[__name__], "_tracked", lambda _p: True)
    assert "ghost_script_zzz" in _missing_referenced_scripts(), \
        "已发布脚本缺失必须报出来 —— 否则判据 1 恒绿（假绿）"


@pytest.mark.skipif(not _is_git_repo(), reason="不在 git 仓库内")
def test_unpublished_script_requires_skip_guard() -> None:
    """★ 判据 2：引用**未发布**脚本的测试必须有 `pytest.skip` 守卫。

    这一条就是本轮那个缺陷的机器复现：
    `test_preflight_check.py` 会入库、`scripts/preflight_check.py` 不会 ——
    没有守卫时，克隆后测试必然红灯，而开发机**永远看不见**。
    """
    unguarded: dict[str, list[str]] = {}
    for name, files in _referenced_scripts().items():
        path = f"scripts/{name}.py"
        if not (ROOT / path).exists():
            continue                      # 判据 1 负责
        if name in _KNOWN_UNGUARDED:
            continue                      # 已登记的在飞项（见常量说明）
        if _tracked(path) is not False:
            continue                      # 已跟踪 = 会发布，无需守卫
        bad = [f for f in files
               if not _has_skip_guard(ROOT / f)]
        if bad:
            unguarded[name] = sorted(bad)
    assert not unguarded, (
        "以下测试会入库，依赖的脚本不会入库，且**没有 pytest.skip 守卫** —— "
        f"克隆后必然红灯：{unguarded}\n"
        "修法：在被引用的测试文件顶部加\n"
        "    if not (ROOT/'scripts'/'<名字>.py').exists():\n"
        "        pytest.skip('...未随仓库发布', allow_module_level=True)")


@pytest.mark.skipif(not _is_git_repo(), reason="不在 git 仓库内")
def test_documented_script_commands_exist() -> None:
    """判据 3：文档里写的**可执行命令**，文件必须存在。

    只认 `python scripts/x.py` 这类**真实调用**，不认普通提及 ——
    文档写「**新增** `scripts/x.py`」是待办提案，不是"不存在的命令"。
    否则文档在教人跑一条跑不通的命令，比没写更糟。
    """
    missing: dict[str, list[str]] = {}
    docs = ["AGENTS.md", *sorted(
        p.relative_to(ROOT).as_posix() for p in ROOT.glob("docs/*.md"))]
    for doc in docs:
        p = ROOT / doc
        if not p.exists():
            continue
        for name in sorted(set(_CALL_RE.findall(
                p.read_text(encoding="utf-8-sig", errors="replace")))):
            if _is_placeholder(name):
                continue                      # 文档里的占位符示例，不是命令
            if name in _KNOWN_DANGLING_CALLS:
                continue                      # 已登记的历史悬空引用
            if not (ROOT / "scripts" / f"{name}.py").exists():
                missing.setdefault(name, []).append(doc)
    assert not missing, (
        f"文档里的**命令**指向不存在的脚本：{missing}\n"
        "修法：改名对齐真实脚本，或加进 _KNOWN_DANGLING_CALLS 并写清原因")


#: `.gitignore` 里 `/scripts/*` 的白名单行（`!/scripts/x.py`）。
#: ⚠️ 必须 `re.MULTILINE` —— 不加时 `^`/`$` 只认**整份文本**的首尾，
#: `findall` 会返回空集，于是判据恒绿（本文件自己踩过一次：空集触发了
#: 下面那条"解析出 0 条"的自证断言，两分钟就抓住了）。
_WHITELIST_RE = re.compile(r"^!/scripts/([A-Za-z_]\w*)\.py\s*$", re.MULTILINE)


def _whitelisted_scripts() -> set[str]:
    """`.gitignore` 明确放行的脚本名（= **政策上承诺要发布**的那些）。"""
    text = (ROOT / ".gitignore").read_text(encoding="utf-8-sig", errors="replace")
    return set(_WHITELIST_RE.findall(text))


def _broken_whitelist_promises() -> dict[str, str]:
    """白名单里承诺发布、却**没有真的入库**的脚本。

    为什么单独立一条判据（2026-09-28 实测，这正是一个**假绿**）：

    `prd_sync_check.py` 被写进 `.gitignore` 白名单（台账 CHG-0047 声称
    "加入发布白名单，这样换开发工具也生效"），但**从来没有 `git add` 过**。
    后果分两层，第二层更贵：

      ① 克隆/CI 里这个文件不存在 → `AGENTS.md` 教的"每轮对账一条命令"跑不了；
      ② CI 的对账门禁因此**每次都静默跳过**（只打一行 `::warning::`）——
         门禁看起来接上了，实际一次都没跑。**绿灯是假的。**

    为什么原有三条判据全都漏掉它：它们报错的**前提**是
    `_tracked(path) is False`（= "这个脚本不发布"）。而白名单里的文件
    恰恰是"**打算发布但目前没入库**" —— 三条判据都把它当成"合法的未发布脚本"
    静默放过。**"政策上要发布"与"技术上已发布"之间的缝，就是这个假绿的来源。**

    判据本身可执行：白名单 = 承诺，`git ls-files` = 事实，两者不一致就报。
    """
    out: dict[str, str] = {}
    for name in sorted(_whitelisted_scripts()):
        path = f"scripts/{name}.py"
        if not (ROOT / path).exists():
            out[name] = "白名单里有、磁盘上都没有（改名/打错字的现场）"
            continue
        if _tracked(path) is False:
            out[name] = "白名单承诺发布，但从未 git add（克隆/CI 里不存在）"
    return out


@pytest.mark.skipif(not _is_git_repo(), reason="不在 git 仓库内")
def test_whitelisted_scripts_are_actually_tracked() -> None:
    """★ 判据 4：`.gitignore` 的白名单不是"免死金牌"，是**必须兑现的承诺**。

    没有这一条时，谁都可以把脚本写进白名单、然后在台账里宣布"已发布"，
    而 CI 门禁因为文件不存在**静默跳过** —— 界面全绿、事实为空。
    """
    broken = _broken_whitelist_promises()
    assert not broken, (
        "以下脚本在 `.gitignore` 白名单里承诺发布，但没有真的入库 —— "
        f"克隆/CI 环境里它们不存在：{broken}\n"
        "修法二选一：① `git add <脚本>` 兑现承诺；"
        "② 从 `.gitignore` 白名单删掉（= 承认它不发布），"
        "并确保引用它的测试有 skip 守卫、文档不再把它当命令教。")
    assert _whitelisted_scripts(), (
        "白名单解析出 0 条 —— 正则或 .gitignore 结构变了，判据 4 会变成假绿")


def test_whitelist_promise_check_is_not_vacuous() -> None:
    """★ 自证：判据 4 必须能报出"承诺未兑现"，不能恒绿。"""
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(sys.modules[__name__], "_whitelisted_scripts",
                            lambda: {"ghost_whitelisted_zzz"})
        monkeypatch.setattr(sys.modules[__name__], "_tracked", lambda _p: False)
        monkeypatch.setattr(Path, "exists", lambda self: True)
        assert "ghost_whitelisted_zzz" in _broken_whitelist_promises(), (
            "白名单里的空头支票必须被报出来 —— 否则判据 4 是假绿")
    finally:
        monkeypatch.undo()


def test_placeholder_names_are_not_treated_as_commands() -> None:
    """★ 自证：文档里的占位符不该被当成真命令。

    实测踩过：`python scripts/x.py` 报出一个不存在的脚本 `x`，
    于是协作者去改文档文案而不是修判据 —— **判据逼着文档变丑**就是判据的错。
    """
    for placeholder in ("x", "y", "script", "your_script", "foo"):
        assert _is_placeholder(placeholder), f"{placeholder} 应判为占位符"
    for real in ("preflight_check", "audit_data_index", "probe_a17_models"):
        assert not _is_placeholder(real), f"{real} 是真实脚本名，不该被判为占位符"
    # 端到端：一条占位符命令 + 一条真命令，只该报真命令
    text = "uv run python scripts/x.py\nuv run python scripts/preflight_check.py\n"
    found = [n for n in _CALL_RE.findall(text) if not _is_placeholder(n)]
    assert found == ["preflight_check"], found


def test_call_regex_matches_real_invocations_only() -> None:
    """自证：调用正则要抓得到真命令、抓不到"待办提案"。"""
    assert _CALL_RE.findall("uv run python scripts/probe_x.py") == ["probe_x"]
    assert _CALL_RE.findall("python scripts/foo.py --a 1") == ["foo"]
    # 提案式提及不是命令
    assert _CALL_RE.findall("新增 `scripts/indicator_latency_report.py`") == []
    # 被引用为前提的 git 跟踪判定也不该把它算进来
    assert "indicator_latency_report" not in _CALL_RE.findall(
        "| 验收判据 | ② 新增 `scripts/indicator_latency_report.py` 输出 P50 |")


def test_the_regex_actually_finds_imports() -> None:
    """自证：正则必须能抓到已知存在的引用关系。

    AGENTS.md 硬约束：「自己的检查脚本必须先自证」——
    只会报 0 的检查等于没有检查。
    """
    found = _referenced_scripts()
    assert "preflight_check" in found, (
        f"正则没抓到已知的引用（tests/unit/test_preflight_check.py → "
        f"scripts/preflight_check.py）；实际抓到：{sorted(found)}")
    assert "tests/unit/test_preflight_check.py" in found["preflight_check"], (
        f"引用方认错了：{sorted(found['preflight_check'])}")
    # 别名必须被剥掉，否则会去找 `scripts/preflight_check as pf.py`
    assert not any(" as " in n for n in found), sorted(found)


def test_import_parser_does_not_confuse_symbols_with_modules() -> None:
    """自证：`from scripts.mod import sym` 里的 `sym` **不是**脚本名。

    本轮实测踩过：把 `from scripts.X import effective_share, variance_share`
    里的两个符号当成脚本名 → 报"引用了不存在的脚本"（误报）。
    还踩过 `as` 别名没剥 → 去找 `scripts/preflight_check as pf.py`。
    """
    src = (
        "from scripts.real_mod import sym_a, sym_b\n"
        "from scripts import one, two as alias\n"
        "import scripts.three\n"
    )
    assert parse_script_imports(src) == {
        "real_mod", "one", "two", "three"}, parse_script_imports(src)


def test_ast_parser_ignores_docstrings_and_comments() -> None:
    """★ 自证：文档字符串/注释里的示例代码**不是**真引用。

    本轮实测踩过：本文件自己的说明文字
    「`from scripts import preflight_check   ← 不会入库`」
    被正则当成真 import，报出一个脚本名叫 `preflight_check不会入库`。
    这正是"检查脚本自己制造假告警"——`ast` 从根上消除它。
    """
    src = (
        '"""说明：\n'
        '    from scripts.ghost import nothing   # 示例，不是真引用\n'
        '    import scripts.phantom\n'
        '"""\n'
        "# from scripts.commented import x\n"
        "import os\n"
    )
    assert parse_script_imports(src) == set(), parse_script_imports(src)


def test_ast_parser_handles_syntax_errors_gracefully() -> None:
    """语法坏掉的文件返回空集，不抛异常（不该在这里二次报错）。"""
    assert parse_script_imports("from scripts.x import (\n") == set()


def test_known_unguarded_entries_are_not_stale() -> None:
    """★ 登记表不许腐烂：已修好的在飞项必须从表里删掉。

    否则"显式登记"会退化成"永久豁免" —— 这正是红灯纪律想防的
    「已知的红色基线测试被当成背景噪音」。
    """
    stale: list[str] = []
    for name in _KNOWN_UNGUARDED:
        path = f"scripts/{name}.py"
        if not (ROOT / path).exists():
            stale.append(f"{name}（脚本已不存在）")
            continue
        if _tracked(path) is True:
            stale.append(f"{name}（脚本已入库，不再需要守卫）")
            continue
        files = _referenced_scripts().get(name, set())
        if files and all(_has_skip_guard(ROOT / f) for f in files):
            stale.append(f"{name}（引用方都已加守卫，应从此表删除）")
    assert not stale, (
        f"以下登记项已过期，请从 _KNOWN_UNGUARDED 删除：{stale}")


def test_annotated_and_relative_imports_are_ignored() -> None:
    """不相关写法不能误报成脚本引用。"""
    assert parse_script_imports("import os\nfrom pathlib import Path\n") == set()
    # 包内模块（非 scripts.）不算
    assert parse_script_imports("from src.core import x\n") == set()


def test_skip_guard_detection_is_reachable() -> None:
    """自证：守卫探测能认出真的守卫（否则判据 2 恒真 = 假绿）。"""
    guarded = ROOT / "tests" / "unit" / "test_preflight_check.py"
    assert guarded.exists(), "自证样本不存在"
    assert _has_skip_guard(guarded), (
        "test_preflight_check.py 明明有模块级 skip 守卫，却没被识别 —— "
        "判据 2 会误报")
