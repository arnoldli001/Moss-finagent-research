"""需求↔PRD 对账护栏的测试 —— 防「假绿」，并把漏账变成会红的断言。

## 为什么必须有这个测试

`prd_sync_check.py --ledger` 现在输出 ✅。但"全绿"有两种可能：

  ① 台账真的完整；
  ② **校验逻辑本身失效**（表没解析到、枚举没校验、证据路径没查），
     于是无条件返回"无问题"。

②就是**假绿** —— 比没有护栏更危险，因为它会让人放心地不再对账。

## 本测试证明什么

| 测试 | 证明 |
|---|---|
| `test_self_test_passes` | 检查器自证通过（四类坏输入都能报出来） |
| `test_real_ledger_has_no_errors` | 当前台账真的达标，且**条目确实被解析到了**（不是空表蒙过） |
| `test_real_ledger_sections_match_prd` | 台账章节 == PRD 现读章节（PRD 加一节而忘登记 → 立刻红） |
| `test_missing_prd_entry_is_detected_end_to_end` | ★ 机器复现「仓库做了、PRD 查不到」→ 必须报 MISS_PRD |
| `test_keyword_absent_everywhere_is_todo_not_ok` | 「没量到」不许报 OK |
| `test_new_requirement_must_land_in_prd_to_close` | ★ 只登台账不改 PRD 却标已闭环 → 必须 ERROR |
| `test_claiming_prd_updated_without_trace_is_error` | ★ 声称改了 PRD 但 PRD 里没有 CHG 号 → 必须 ERROR（"我改了≠它生效了"） |
| `test_fenced_template_block_is_not_a_section` | PRD §10.1 的 ```markdown 模板标题不许被骗成章节 |
| `test_zero_sections_is_an_error` | 解析器坏掉时不许"0 章节 = 全覆盖" |
| 模块级 `skip` 守卫 | `scripts/` 不随仓库发布 → **知名降级**，不在克隆环境制造红灯 |
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

#: ⚠️ `scripts/` 与 `.trae/` 在本仓库**默认不发布**（`.gitignore:84 /scripts/*`、
#: `:116 /.trae/`），所以本测试会入库、而它依赖的脚本**可能不会** ——
#: 不处理的话**别人克隆下来必然红灯**（违反红灯纪律）。
#: 按 AGENTS.md「交付完整性硬约束」显式降级，而不是假装通过。
#: 发布与否是政策；工程侧只负责"让缺失不致命且可见"。
#:
#: 2026-09-28（CHG-0047）：脚本与 skill 已加入 `.gitignore` 白名单 → 会发布。
#: 但**台账与审计文档是否公开尚未裁定**（见 `docs/REQUIREMENT_CHANGELOG.md` §5 C17：
#: 两者含内部数字），而本测试要读台账、台账的证据又要指向审计 —— 三者是一套。
#: 所以守卫按"一整套"判定：**任一缺失即显式 skip**，不制造红灯、也不静默绿灯。
_NEEDED = (
    ROOT / "scripts" / "prd_sync_check.py",
    ROOT / "docs" / "PRD.md",
    ROOT / "docs" / "REQUIREMENT_CHANGELOG.md",
    ROOT / "docs" / "PRD_ALIGNMENT_AUDIT_20260928.md",
)
_missing = [p.relative_to(ROOT).as_posix() for p in _NEEDED if not p.exists()]
if _missing:
    pytest.skip(
        "对账护栏的组成文件未随仓库发布（政策见 REQUIREMENT_CHANGELOG §5 C17）："
        f"{_missing}；该护栏只在具备整套文件的开发机生效。"
        "发布方式与降级说明见 tests/unit/test_shipped_deps.py",
        allow_module_level=True)

from scripts import prd_sync_check as ps  # noqa: E402


# ============================================================
# 自证 + 现状（回归哨兵）
# ============================================================


def test_self_test_passes() -> None:
    """检查器自证：已知缺失 / 幻影章节 / 失效证据 / 未落 PRD 的闭环，都要能报。"""
    fails = ps.self_test()
    assert not fails, "自证失败（护栏本身不可信）：\n" + "\n".join(fails)


def test_ledger_has_no_nul_bytes() -> None:
    """★★ 台账里**一个 NUL 字节都不许有**（2026-10-01 实测踩到，`CHG-0153` ⑫）。

    现场：`CHG-0149` 那一行把 `0x80000003` 写成了 `\\x00x80000003` ——
    一个**字面 NUL 字节**。后果不是"那一行显示难看"，而是
    **整份 `REQUIREMENT_CHANGELOG.md` 对所有基于文本的工具降级成二进制**：

      · 通用编辑工具直接拒绝读写它（本轮实测："binary file"）；
      · GitHub / GitLab 的 PR 视图显示「Binary file not shown」
        ⇒ **这份中央台账在评审里整体消失**；
      · 一些按内容检索的工具默认跳过二进制文件 ⇒ 台账"搜不到"。

    修复成本是 **1 个字节**，漏掉的成本是**整份台账不可评审**。
    所以判据必须落在字节层，而不是"看渲染出来对不对"——
    渲染时 NUL 是不可见的，**肉眼永远看不出这一条**。

    同时钉住"UTF-8 可解码"：坏编码会让同一件事以另一种形式复发。
    """
    for rel in (ps.LEDGER_PATH, ps.PRD_PATH):
        path = ROOT / rel
        raw = path.read_bytes()
        nul = raw.count(b"\x00")
        assert nul == 0, (
            f"{rel} 里有 {nul} 个 NUL 字节 ⇒ 文本工具会把它当二进制文件"
            f"（PR 视图显示 Binary file not shown、编辑工具拒绝读写）")
        try:
            raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise AssertionError(
                f"{rel} 不是合法 UTF-8：{exc} —— 同一条判据的另一半") from exc


def test_real_prd_and_ledger_exist() -> None:
    """需求基线 + 台账必须都在 —— 缺一个就不是"没有问题"，是"没法对账"。"""
    assert (ROOT / ps.PRD_PATH).exists(), f"缺少需求基线 {ps.PRD_PATH}"
    assert (ROOT / ps.LEDGER_PATH).exists(), f"缺少变更台账 {ps.LEDGER_PATH}"


def test_real_ledger_has_no_errors() -> None:
    """现状：台账通过校验。"""
    prd = (ROOT / ps.PRD_PATH).read_text(encoding="utf-8-sig")
    ledger = (ROOT / ps.LEDGER_PATH).read_text(encoding="utf-8-sig")
    issues = ps.validate_ledger(prd, ledger)
    errors = [i for i in issues if i.level == "ERROR"]
    assert not errors, "台账有未达标项：\n" + "\n".join(
        f"  {i.where}：{i.detail}" for i in errors)


def test_real_ledger_entries_are_actually_parsed() -> None:
    """★ 防"空表蒙过"：条目必须真被解析出来，否则校验等于没跑。

    实测过的假绿形态：表头列名一改，`_table_rows` 返回空 →
    所有逐条校验静默跳过 → 脚本报 ✅。
    """
    ledger = (ROOT / ps.LEDGER_PATH).read_text(encoding="utf-8-sig")
    _, ent_rows = ps._table_rows(ledger, ps.LEDGER_ENTRY_TABLE_MARK)
    assert ent_rows, (
        f"没解析到任何变更条目 —— 表头必须含 `变更 ID` 列，"
        f"且标题里含「{ps.LEDGER_ENTRY_TABLE_MARK}」")
    for r in ent_rows:
        assert ps._cell(r, "变更 ID").startswith("CHG-"), (
            f"列名没对上，解析出来的行是：{r}")


def test_real_ledger_sections_match_prd() -> None:
    """台账登记的章节集合 == PRD 现读的章节集合（两向都查）。"""
    prd = (ROOT / ps.PRD_PATH).read_text(encoding="utf-8-sig")
    ledger = (ROOT / ps.LEDGER_PATH).read_text(encoding="utf-8-sig")
    prd_ids = {s.id for s in ps.parse_prd_sections(prd)}
    _, sec_rows = ps._table_rows(ledger, ps.LEDGER_SECTION_TABLE_MARK)
    reg = {ps._cell(r, "章节 ID") for r in sec_rows}
    reg.discard("")
    assert prd_ids - reg == set(), (
        f"PRD 有但台账没登记：{sorted(prd_ids - reg)}"
        "（新增章节后必须同步台账；用 --list-sections 现读）")
    assert reg - prd_ids == set(), (
        f"台账引用了 PRD 里不存在的章节：{sorted(reg - prd_ids)}"
        "（PRD 改过编号 → 幻影引用，比漏登记更隐蔽）")


# ============================================================
# ★ 机器复现：仓库做了、PRD 查不到
# ============================================================


def _make_repo(tmp: Path, *, prd: str, ledger: str,
               code: str = "") -> Path:
    (tmp / "docs").mkdir(parents=True, exist_ok=True)
    (tmp / "src").mkdir(parents=True, exist_ok=True)
    (tmp / "docs" / "PRD.md").write_text(prd, encoding="utf-8")
    (tmp / "docs" / "REQUIREMENT_CHANGELOG.md").write_text(ledger, encoding="utf-8")
    if code:
        (tmp / "src" / "mod.py").write_text(code, encoding="utf-8")
    return tmp


def test_missing_prd_entry_is_detected_end_to_end(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """★ 这就是用户要防的那一幕：仓库做了（`qmt_enabled: false`），
    PRD 里一个字都没有 → 必须报 `MISS_PRD`，逼着补进 PRD。

    （用 tmp 仓库而不是真仓库：真仓库的 PRD 一旦补上 QMT，这条断言就会失真。）
    """
    _make_repo(
        tmp_path,
        prd="# 需求\n\n## 一、概述\n\n- 本地行情走 QMT\n",
        ledger="## 1. 章节登记表\n\n| 章节 ID | PRD 标题 | 登记状态 |\n|---|---|---|\n"
               "| PRD-1 | 一、概述 | 已登记 |\n",
        code="qmt_enabled = False  # 已降位到链尾\n")
    monkeypatch.setattr(ps, "ROOT", tmp_path)

    rep = ps.scan_keyword("qmt_enabled")          # PRD 里没有这个词
    assert rep.prd == 0, "PRD 面不该命中"
    assert rep.in_repo, f"实现面该命中：{rep.code}"
    assert rep.verdict == "MISS_PRD", (
        f"仓库做了、PRD 查不到，却报 {rep.verdict} —— 护栏失效")


def test_keyword_absent_everywhere_is_todo_not_ok(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """「没量到」≠「量到 0」：三面都查不到时不许报 OK（假绿）。"""
    _make_repo(tmp_path, prd="# 需求\n\n## 一、概述\n",
               ledger="## 1. 章节登记表\n\n| 章节 ID | PRD 标题 | 登记状态 |\n"
                      "|---|---|---|\n| PRD-1 | 一、概述 | 已登记 |\n")
    monkeypatch.setattr(ps, "ROOT", tmp_path)
    rep = ps.scan_keyword("完全不存在的关键词_zzz")
    assert rep.verdict == "TODO", f"没量到却报 {rep.verdict}"
    assert rep.verdict != "OK"


def test_prd_and_ledger_hit_is_ok() -> None:
    """PRD 有 + 台账有 → OK（存在性通过；口径一致性仍需人工看）。"""
    rep = ps.KeywordReport(keyword="x", prd=2, ledger=1)
    assert rep.verdict == "OK"


def test_prd_without_ledger_entry_is_miss_ledger() -> None:
    """PRD 有、台账无 → 需求史断档（老需求没登记 / 本轮没落账）。"""
    rep = ps.KeywordReport(keyword="x", prd=2, ledger=0)
    assert rep.verdict == "MISS_LEDGER"


def test_rule_plane_alone_still_counts_as_repo_having_it() -> None:
    """只写进 AGENTS.md / skill 的要求也算"仓库有" —— 它同样是 PRD 里该有的需求。"""
    rep = ps.KeywordReport(keyword="x", rule={"AGENTS.md": 3})
    assert rep.in_repo
    assert rep.verdict == "MISS_PRD"


# ============================================================
# 台账校验：核心判据
# ============================================================


_PRD_FIXTURE = (
    "## 一、概述\n\n内容（CHG-0001：示例落点）\n\n### 1.1 子节\n\n内容\n\n## 二、架构\n\n内容\n"
)

_LEDGER_FIXTURE = (
    "## 1. 章节登记表\n\n"
    "| 章节 ID | PRD 标题 | 层级 | 章节登记状态 | 备注 |\n"
    "|---|---|---|---|---|\n"
    "| PRD-1 | 一、概述 | H2 | 已登记 | docs/PRD.md:1 |\n"
    "| PRD-1.1 | 1.1 子节 | H3 | 无差异 | docs/PRD.md:5 |\n"
    "| PRD-2 | 二、架构 | H2 | 待确认 | docs/PRD.md:9 |\n\n"
    "## 3. 变更台账\n\n"
    "| 变更 ID | 日期 | 类型 | PRD 章节 | 需求原话 | 处置 | PRD 已更新 | 状态 | 证据 |\n"
    "|---|---|---|---|---|---|---|---|---|\n"
)


def _entry(**kw: str) -> str:
    """拼一条台账行；缺省值是**合规**的，"新增已闭环"那条用来做反向测试。"""
    row = {
        "变更 ID": "CHG-0001", "日期": "2026-09-28", "类型": "细化",
        "PRD 章节": "PRD-1", "需求原话": "用户原话 A",
        "处置": "补写PRD", "PRD 已更新": "是", "状态": "已闭环",
        "证据": f"{ps.PRD_PATH}:1",
    }
    row.update(kw)
    return "| " + " | ".join(row.values()) + " |\n"


def test_conflict_can_land_by_deprecation_marker() -> None:
    """冲突类可以通过**标注废止**落 PRD（把旧口径标成废止并留 CHG 号）。

    防"一刀切"：若只认 补写/改写，现实里"整节废止"就只能被迫写成假的"改写PRD"。
    """
    ledger = _LEDGER_FIXTURE + _entry(类型="冲突", 处置="标注废止")
    issues = ps.validate_ledger(_PRD_FIXTURE, ledger)
    assert not [i for i in issues if "没有落到" in i.detail], \
        f"合法的废止标记被误报：{[i.detail for i in issues]}"


def test_claiming_prd_updated_without_trace_is_error() -> None:
    """★ 核心判据：「PRD 已更新=是」必须**在 PRD 里看得见 CHG 号**。

    这是本项目最贵的教训「我改了 ≠ 它生效了」的机器判据：
    台账宣布改了 PRD，而 PRD 里搜不到这条 CHG —— 那条"改动"根本不存在。
    """
    ledger = _LEDGER_FIXTURE + _entry(**{"变更 ID": "CHG-0777"})
    issues = ps.validate_ledger(_PRD_FIXTURE, ledger)
    assert any("改了 ≠ 生效了" in i.detail for i in issues), \
        f"假闭环没被报出来：{[i.detail for i in issues]}"


def test_new_requirement_must_land_in_prd_to_close() -> None:
    """★ 核心判据：`新增` + `已闭环` 却不补/不改 PRD → ERROR。

    用户原话：「没有的补充进去，有差异的改动」——
    "只登台账"两件都没做，所以必须报错，而不是让人自己看着办。
    """
    ledger = _LEDGER_FIXTURE + _entry(
        类型="新增", 处置="仅登记台账", **{"PRD 已更新": "否"})
    issues = ps.validate_ledger(_PRD_FIXTURE, ledger)
    hits = [i for i in issues if "没有落到" in i.detail]
    assert hits, f"未落 PRD 却标已闭环 —— 没报错：{[i.detail for i in issues]}"


def test_conflict_requires_rewrite_not_just_logging() -> None:
    """口径冲突同样必须改写 PRD（旧口径标注废止），不能只登记。"""
    ledger = _LEDGER_FIXTURE + _entry(
        类型="冲突", 处置="仅登记台账", **{"PRD 已更新": "否"})
    issues = ps.validate_ledger(_PRD_FIXTURE, ledger)
    assert any("没有落到" in i.detail for i in issues), \
        f"冲突未落 PRD 没报错：{[i.detail for i in issues]}"


def test_pending_entry_is_allowed_to_skip_prd() -> None:
    """落不进 PRD 的条目**是允许存在的** —— 但状态必须停在待办。

    防"一刀切"：如果连待办都报错，人就会去改状态而不是改需求（指标失真）。
    """
    ledger = _LEDGER_FIXTURE + _entry(
        类型="冲突", 处置="待办", **{"PRD 已更新": "否", "状态": "待办"})
    issues = ps.validate_ledger(_PRD_FIXTURE, ledger)
    assert not [i for i in issues if "没有落到" in i.detail], \
        f"待办条目被误报：{[i.detail for i in issues]}"


def test_landing_action_requires_updated_flag() -> None:
    """处置=补写PRD 但「PRD 已更新」=否 → 自相矛盾，必须报。"""
    ledger = _LEDGER_FIXTURE + _entry(处置="补写PRD", **{"PRD 已更新": "否"})
    issues = ps.validate_ledger(_PRD_FIXTURE, ledger)
    assert any("PRD 已更新" in i.detail and "否" in i.detail for i in issues), \
        f"矛盾没被报出来：{[i.detail for i in issues]}"


def test_status_todo_requires_todo_action() -> None:
    """状态=待办 而 处置=补写PRD → 状态与处置打架。"""
    ledger = _LEDGER_FIXTURE + _entry(**{"状态": "待办", "处置": "补写PRD"})
    issues = ps.validate_ledger(_PRD_FIXTURE, ledger)
    assert any("状态=待办" in i.detail for i in issues), \
        f"没报出来：{[i.detail for i in issues]}"


def test_closed_entry_cannot_have_todo_action() -> None:
    """状态=已闭环 而 处置=待办 → 闭环必须写明怎么落地的。"""
    ledger = _LEDGER_FIXTURE + _entry(处置="待办", **{"状态": "已闭环"})
    issues = ps.validate_ledger(_PRD_FIXTURE, ledger)
    assert any("状态=已闭环" in i.detail and "处置=待办" in i.detail
               for i in issues), \
        f"没报出来：{[i.detail for i in issues]}"


def test_free_text_enums_are_rejected() -> None:
    """枚举只认机器可读的值：写"改好了"这种文案必须报错。"""
    ledger = _LEDGER_FIXTURE + _entry(处置="改好了", **{"状态": "差不多"})
    issues = ps.validate_ledger(_PRD_FIXTURE, ledger)
    details = " ".join(i.detail for i in issues)
    assert "处置" in details and "状态" in details, \
        f"自由文本没被枚举校验拦住：{details}"


def test_missing_section_registration_is_error() -> None:
    """PRD 有、台账不登记 → ERROR（而不是"没写就是没有差异"）。"""
    ledger = _LEDGER_FIXTURE.replace(
        "| PRD-2 | 二、架构 | H2 | 待确认 | docs/PRD.md:9 |\n", "")
    issues = ps.validate_ledger(_PRD_FIXTURE, ledger)
    assert any("PRD-2" in i.detail and "未登记" in i.detail for i in issues), \
        f"漏登记没被报出来：{[i.detail for i in issues]}"


def test_phantom_section_id_is_error() -> None:
    """台账引用 PRD 里不存在的章节 → ERROR（PRD 重编号后的错位）。"""
    ledger = _LEDGER_FIXTURE + "| PRD-9.9 | 幻影 | H3 | 无差异 | - |\n"
    issues = ps.validate_ledger(_PRD_FIXTURE, ledger)
    assert any("PRD-9.9" in i.detail for i in issues), \
        f"幻影章节没被报出来：{[i.detail for i in issues]}"


def test_unresolvable_evidence_is_error() -> None:
    """证据必须是现存路径 —— 「我改了」不算证据。"""
    ledger = _LEDGER_FIXTURE + _entry(证据="docs/不存在_zzz.md:1")
    issues = ps.validate_ledger(_PRD_FIXTURE, ledger)
    assert any("一个都不存在" in i.detail for i in issues), \
        f"失效证据没被报出来：{[i.detail for i in issues]}"


def test_evidence_without_any_path_is_error() -> None:
    """证据里连路径都没有（"已确认"）→ ERROR。"""
    ledger = _LEDGER_FIXTURE + _entry(证据="已确认")
    issues = ps.validate_ledger(_PRD_FIXTURE, ledger)
    assert any("没有任何文件路径" in i.detail for i in issues), \
        f"没报出来：{[i.detail for i in issues]}"


def test_missing_entry_table_is_error() -> None:
    """台账里没有「变更台账」表 → ERROR，不许静默当"暂无条目=合规"。"""
    issues = ps.validate_ledger(_PRD_FIXTURE, "## 1. 章节登记表\n")
    assert any("变更台账" in i.detail for i in issues), \
        f"缺表没报错：{[i.detail for i in issues]}"


def test_zero_sections_is_an_error() -> None:
    """解析器坏掉时**必须报错**：0 章节会自动变成"全部覆盖"（假绿）。"""
    issues = ps.validate_ledger("# 没有章节的 PRD", _LEDGER_FIXTURE)
    assert any("0 个章节" in i.detail for i in issues), \
        f"0 章节没报错：{[i.detail for i in issues]}"


def test_duplicate_change_id_is_error() -> None:
    """同一个 CHG 号出现两次 → 变更史无法引用。"""
    ledger = _LEDGER_FIXTURE + _entry() + _entry(**{"需求原话": "另一条"})
    issues = ps.validate_ledger(_PRD_FIXTURE, ledger)
    assert any("ID 重复" in i.detail for i in issues), \
        f"重复 ID 没报出来：{[i.detail for i in issues]}"


def test_empty_quote_is_error() -> None:
    """需求原话为空 → 这条台账无法复核（用户输入才是台账的输入）。"""
    ledger = _LEDGER_FIXTURE + _entry(**{"需求原话": ""})
    issues = ps.validate_ledger(_PRD_FIXTURE, ledger)
    assert any("需求原话" in i.detail for i in issues), \
        f"空原话没报出来：{[i.detail for i in issues]}"


# ============================================================
# PRD 解析：围栏代码块 / 真实文件
# ============================================================


def test_fenced_template_block_is_not_a_section() -> None:
    """PRD §10.1 嵌了一整段 ```markdown 的 AGENTS.md 模板，里面的 `## xxx` 不是章节。

    不跳围栏 → 台账会被要求登记一堆幻影章节，而且**不报错**。
    """
    text = ("## 一、真章节\n\n```markdown\n## 项目定位\n## 环境配置\n"
            "### 架构规范\n```\n\n### 1.1 真子节\n")
    titles = [s.title for s in ps.parse_prd_sections(text)]
    assert "一、真章节" in titles and "1.1 真子节" in titles
    for ghost in ("项目定位", "环境配置", "架构规范"):
        assert ghost not in titles, f"代码块里的 `{ghost}` 被骗成章节：{titles}"


def test_real_prd_template_block_titles_are_not_sections() -> None:
    """对真实 `docs/PRD.md` 的同一断言（那是模板块真实存在的地方）。"""
    prd = (ROOT / ps.PRD_PATH).read_text(encoding="utf-8-sig")
    titles = {s.title for s in ps.parse_prd_sections(prd)}
    leaked = titles & {"项目定位", "环境配置", "架构规范", "编码规范",
                       "数据溯源规范", "安全合规规范", "Trae使用规范"}
    assert not leaked, f"PRD 模板块里的标题被当成章节：{sorted(leaked)}"


def test_section_ids_are_unique_and_ordered() -> None:
    """ID 必须唯一且按出现顺序 —— 台账靠它定位，重复或乱序会让引用错位。"""
    prd = (ROOT / ps.PRD_PATH).read_text(encoding="utf-8-sig")
    secs = ps.parse_prd_sections(prd)
    ids = [s.id for s in secs]
    assert len(ids) == len(set(ids)), "章节 ID 重复"
    assert ids == sorted(ids, key=lambda s: (int(s.split("-")[1].split(".")[0]),
                                             int(s.split(".")[1]) if "." in s else -1)), \
        f"章节 ID 不是按出现顺序：{ids[:12]}"
    assert sum(1 for s in secs if s.level == 2) >= 10, "H2 章节数异常偏少，解析可能坏了"
