"""开工检查脚本的护栏 —— 防「假绿」。

## 为什么必须有这个测试

`scripts/preflight_check.py --prefix cal:` 现在输出 **9/9 已教**。
但"全绿"有两种可能：

  ① 真的都教了；
  ② **检查逻辑本身失效**（比如 prompt 压根没读到、关键词判断写错），
     于是无条件返回 OK。

②就是「假绿」—— 比没有检查更危险，因为它会让人放心。

## 本测试证明什么

| 测试 | 证明 |
|---|---|
| `test_preflight_reports_ok_for_cal_prefix` | 当前状态确实全绿（不是靠失效逻辑蒙的）|
| `test_preflight_detects_missing_teaching` | **判据关键词换成不存在的词 → 必须报 MISS**。<br/>这一条就是当时 A12–A16 漏教的复现 |
| `test_preflight_missing_is_reachable_and_counted` | MISS 会被计入统计（不是打印了但不算）|
| `test_preflight_self_check_guards_scope` | 扫描范围没被改坏（自证逻辑真的在跑）|
| `test_self_check_fails_loudly_on_empty_scope` | 扫描范围坏掉时**必须抛错**，不能静默全绿 |
| `test_every_whitelisted_agent_has_prompt_mapping` | 白名单新增 Agent 但没登记类映射时，<br/>脚本要能说出来（不能静默当"已教"）|
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

#: ⚠️ `scripts/` 在本仓库**默认不发布**（`.gitignore` 第 83 行：
#: 「scripts 默认全部不发布 …… 研究/运维/验证脚本不入库」）。
#: 所以本测试文件会入库，而它依赖的脚本**可能不会** ——
#: 不处理的话，**别人克隆下来测试必然红灯**（违反红灯纪律）。
#: 这里显式跳过而不是假装通过（"没量到" ≠ "量到 0"）。
_SCRIPT = ROOT / "scripts" / "preflight_check.py"
if not _SCRIPT.exists():
    pytest.skip(
        "scripts/preflight_check.py 未随仓库发布（scripts/ 默认不入库）；"
        "该护栏只在开发机生效。发布方式见 tests/unit/test_shipped_deps.py",
        allow_module_level=True)

from scripts import preflight_check as pf  # noqa: E402


def _cal_agents() -> list[str]:
    """白名单里放行 `cal:` 的 Agent —— **现读**，不写死。

    ★ 2026-09-29 修正：这里原先写死 `"9 个 Agent"` / `"已教 9/9"`，
    新增兜底行业 Agent（A20）后**立刻红**，而真因只是"多了一个 Agent"。
    这正是本项目反复登记过的模式（**判据要表达意图，不要表达某个时刻的数量**）：
    写死 9 会让每次新增 Agent 都误报成"贯通点坏了"，把真问题淹掉。
    现在数字从 `_AGENT_DATA_WHITELIST` 现算 —— 白名单加人，这里自动跟上。
    """
    from src.orchestration.supervisor import _AGENT_DATA_WHITELIST

    return [a for a, kws in _AGENT_DATA_WHITELIST.items() if "cal:" in kws]


def test_preflight_reports_ok_for_cal_prefix() -> None:
    """现状：cal: 前缀的贯通点全绿。"""
    rows = pf.check_touchpoints("cal:")
    assert rows, "check_touchpoints 返回空 —— 检查逻辑没跑"
    by_name = {name: (status, ev) for name, status, ev in rows}

    cal_agents = _cal_agents()
    assert cal_agents, "没有任何 Agent 的白名单含 cal:（本测试失去意义）"
    n = len(cal_agents)

    wl_status, wl_ev = by_name["_AGENT_DATA_WHITELIST"]
    assert wl_status == "OK", wl_ev
    assert f"{n} 个 Agent" in wl_ev, (
        f"白名单放行 Agent 数应为 {n}（现读自 _AGENT_DATA_WHITELIST），实际：{wl_ev}"
    )

    taught_status, taught_ev = by_name["Agent system_prompt（贯通点⑩）"]
    assert taught_status == "OK", taught_ev
    assert f"已教 {n}/{n}" in taught_ev, taught_ev


def test_preflight_detects_missing_teaching(monkeypatch: pytest.MonkeyPatch) -> None:
    """★ 核心自证：判据关键词换成不存在的词 → 必须报 MISS。

    这就是 2026-09-28 那个 bug 的机器复现：
    白名单放行了 N 个 Agent，但 prompt 里没教 —— 检查必须抓到。
    """
    monkeypatch.setattr(pf, "_PREFIX_KW",
                        {"cal:": ("绝不存在的关键词-xyzzy",)})
    rows = pf.check_touchpoints("cal:")
    by_name = {name: (status, ev) for name, status, ev in rows}
    status, ev = by_name["Agent system_prompt（贯通点⑩）"]
    n = len(_cal_agents())
    assert status == "MISS", f"漏教却报 OK —— 假绿！证据：{ev}"
    assert "漏教" in ev, ev
    assert f"已教 0/{n}" in ev, ev


def test_preflight_missing_is_reachable_and_counted(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """MISS 会真的被计入 miss 数（main 里用它决定打印 ❌ 还是 ✅）。"""
    monkeypatch.setattr(pf, "_PREFIX_KW",
                        {"cal:": ("绝不存在的关键词-xyzzy",)})
    rows = pf.check_touchpoints("cal:")
    miss = sum(1 for _, s, _ in rows if s == "MISS")
    assert miss >= 1, f"漏教没被计入 miss：{rows}"


def test_preflight_self_check_guards_scope() -> None:
    """自证逻辑真的在跑，且扫描范围覆盖 src/。"""
    assert pf._iter_files(), "扫描范围为空"
    hits = pf.search("_AGENT_DATA_WHITELIST")
    assert hits, "已知词搜不到 —— 扫描范围坏了"
    assert any(h.path.startswith("src/") for h in hits), \
        f"src/ 不在扫描范围内：{[h.path for h in hits]}"
    pf._self_check()  # 不该抛


def test_search_never_truncates_by_default() -> None:
    """★ 默认不截断 —— 否则"同类现场"清单会静默缺层。

    实测事故：文件按字母序排时 `.trae/`、`AGENTS.md` 顶到最前，
    带 `max_hits` 上限的搜索**整个 `src/` 层一条都不返回**，
    而使用者会以为"同类现场已看全"。
    """
    capped = pf.search("_AGENT_DATA_WHITELIST", max_hits=2)
    assert any(h.path == "(截断)" for h in capped), \
        "截断时必须显式留标记，不能静默丢结果"

    full = pf.search("_AGENT_DATA_WHITELIST")
    assert not any(h.path == "(截断)" for h in full), "默认搜索不该截断"


def test_search_orders_src_layer_first() -> None:
    """排序必须让 `src/` 先出现（顺序即重要性）。"""
    files = [p.relative_to(pf.ROOT).as_posix() for p in pf._iter_files()]
    src_pos = files.index(next(f for f in files if f.startswith("src/")))
    skill_pos = files.index(
        next(f for f in files if f.startswith(".trae/")))
    assert src_pos < skill_pos, (
        f"src/(#{src_pos}) 排在 .trae/(#{skill_pos}) 之后 —— "
        "任何截断都会先丢掉代码层")


def test_keyword_scan_covers_all_major_layers() -> None:
    """同一个概念散落的多层必须**全部**出现在结果里。

    这就是「问 1：这是一个点还是一类」的机器判据 ——
    清单少了任何一层，开工者就会漏掉那一层的改动。

    `_AGENT_DATA_WHITELIST` 这个词的真实散布（4 层）：
    定义在编排层，被测试断言，写进 skill 和 AGENTS.md，还有脚本引用它。
    """
    hits = pf.search("_AGENT_DATA_WHITELIST")
    layers = {h.layer for h in hits}
    for required in ("③ 代码-编排/调度", "⑤ 测试", "⑥ 文档/Skill", "⑦ 脚本"):
        assert required in layers, f"缺少层 {required}；实际={sorted(layers)}"


def test_every_scanned_file_is_classified() -> None:
    """★ 扫描范围内的**每个**文件都必须能归类，不许掉进「⑨ 未分类」黑箱。

    实测事故：`src/domain/intel/` 整个目录没被任何层规则覆盖 →
    掉进「其它」。而"未分类"在输出里看起来像"不重要"，
    开工者会直接跳过它 —— 这正是漏改的来源。
    """
    unclassified = [
        p.relative_to(pf.ROOT).as_posix() for p in pf._iter_files()
        if pf.Hit(path=p.relative_to(pf.ROOT).as_posix(),
                  line=1, text="").layer == "⑨ 未分类"
    ]
    assert not unclassified, (
        f"{len(unclassified)} 个文件无法归类，会在输出里被当'其它'跳过；"
        f"请给 LAYER_RULES 补规则：{unclassified[:10]}")


def test_layer_rules_cover_src_exhaustively() -> None:
    """`src/` 下不允许出现「⑨ 未分类」。"""
    bad = [p.relative_to(pf.ROOT).as_posix() for p in pf._iter_files()
           if p.relative_to(pf.ROOT).as_posix().startswith("src/")
           and pf.Hit(path=p.relative_to(pf.ROOT).as_posix(),
                      line=1, text="").layer == "⑨ 未分类"]
    assert not bad, f"src/ 下有未归类文件：{bad}"


def test_self_check_fails_loudly_on_empty_scope(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """扫描范围坏掉时 _self_check 必须抛错，不能静默全绿。"""
    monkeypatch.setattr(pf, "_iter_files", lambda: [])
    with pytest.raises(AssertionError, match="自证失败"):
        pf._self_check()


def test_every_whitelisted_agent_has_prompt_mapping() -> None:
    """白名单里的 Agent 若没在 _AGENT_MODS 登记，会被报成"漏教"而非静默放过。"""
    from src.orchestration.supervisor import _AGENT_DATA_WHITELIST

    wl_agents = [a for a, kws in _AGENT_DATA_WHITELIST.items()
                 if any(str(k) == "cal:" for k in kws)]
    unmapped = [a for a in wl_agents if a not in pf._AGENT_MODS]
    assert not unmapped, (
        f"这些 Agent 放行了 cal: 但 preflight 没有类映射，"
        f"检查会静默失效：{unmapped}。"
        f"请把它们加进 scripts/preflight_check.py::_AGENT_MODS")


def test_unregistered_prefix_reports_todo_not_ok(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """★ 未登记 prompt 判据的前缀必须报 TODO，**不许报 OK**。

    「没量到」≠「量到 0」（AGENTS.md 硬约束）：
    · 报 OK → 假绿，人以为 Agent 已经会用了；
    · 报 MISS → 误报，会训练人忽略警告。
    所以单列 TODO，且不计入"通过"。
    """
    monkeypatch.setattr(pf, "_PREFIX_KW", {})
    rows = pf.check_touchpoints("cal:")
    by_name = {name: (s, e) for name, s, e in rows}
    status, ev = by_name["Agent system_prompt（贯通点⑩）"]
    assert status == "TODO", f"未登记判据却报了 {status} —— 假绿或误报：{ev}"
    assert "未登记" in ev, ev


def test_prompt_taught_never_claims_ok_without_criteria() -> None:
    """`_prompt_taught` 在无判据时不能把任何 Agent 算作"已教"。"""
    taught, missing = pf._prompt_taught(["A11_fin_risk"], "完全没登记的前缀:")
    assert taught == [], f"无判据却报已教：{taught}"
    assert missing == ["A11_fin_risk"], missing


def test_prefix_with_no_whitelisted_agent_is_todo() -> None:
    """无人放行的前缀要**出声**（TODO），不能静默当通过。

    空心白名单有两种可能：给分析层的数据漏改了白名单（真缺陷），
    或本就是内部数据（可忽略）—— 机器判不了，必须让人看到。
    """
    rows = pf.check_touchpoints("nonexistent_prefix_zzz:")
    by_name = {name: (s, e) for name, s, e in rows}
    assert "_AGENT_DATA_WHITELIST" in by_name, "白名单这一行整个没打印"
    status, ev = by_name["_AGENT_DATA_WHITELIST"]
    assert status == "TODO", f"无人放行却报 {status}：{ev}"
    assert "人工判断" in ev, ev
    # prompt 那一行也必须出现（不能因为 wl 空就省略）
    assert "Agent system_prompt（贯通点⑩）" in by_name


def test_layer_classification_covers_all_hits() -> None:
    """每条命中都要能归类，否则分组输出会漏。"""
    for h in pf.search("_AGENT_DATA_WHITELIST"):
        assert h.layer, f"未归类：{h.path}"
        assert h.layer != "⑨ 未分类", f"未归类：{h.path}（层规则有缺口）"
