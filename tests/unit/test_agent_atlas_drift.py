"""`docs/AGENT_COLLABORATION_ATLAS.html` 的**数字漂移判据**。

## 这个文件为什么存在

图谱是**手写 HTML**，里面的数字（Agent 数 / 图节点数 / 边数 / 并行度 / 常量值 /
`文件:行号`）在 2026-09-26 写成后**没有任何东西看着它们**。实测后果（2026-10-15
用户报障「数量不一致了」）：图里写"已注册Agent 19 个 / 依赖图节点 16 个"，
而实际是 **20 个 / 21 个节点 / 33 条边**；`ALL_INSIGHT_AGENTS` 写 9 个（实为 10）；
`INDUSTRY_KEYWORDS` 写 46 词（实为 44）；§8 那张"关键常量与代码锚点"表的
**行号全部漂了**（它写作时 `supervisor.py` 只有 940 行，现在 4965 行）——
最坏的一种错：**锚点看起来最精确，却指到一行无关代码**。

所以这里不检查"文档写得漂不漂亮"，只检查一件事：
**页面上的每个数字与每个锚点，是否还能被代码现算出来**。

## 判据的五层

1. `atlas-facts` 块（页面顶部，机器可读）与代码现算值逐条相等；
2. 四张统计卡片与正文里的关键词串**确实带着**这些数字（防止"facts 改了、正文没改"）；
3. Mermaid 图数量 = 页面里 `class="mermaid"` 的真实块数；
4. 每一个 `class="file">路径:行号` 单元格：文件存在、行号不越界；
5. 一组**人工挑选的高风险锚点**：`(文件, 行号)` 那一行必须仍然含有它的符号名 ——
   这条才是真正抓"行号漂移"的判据（第 4 条抓不住它：漂到文件中间照样不越界）。

★ 反面教材写在判据里：第 5 条的清单**刻意包含** `_STORAGE_FALLBACK_LIMITS`
（它已被删除 ⇒ 页面必须改成现行常量名），因为"文档引用一个不存在的常量"
正是这次审计抓到的原样。
"""
from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ATLAS = ROOT / "docs" / "AGENT_COLLABORATION_ATLAS.html"


def _html() -> str:
    if not ATLAS.exists():  # pragma: no cover - 文件是本判据的主体
        pytest.skip(f"图谱不存在：{ATLAS}（未发布/已移除时应显式跳过）")
    return ATLAS.read_text(encoding="utf-8")


def _facts() -> dict:
    """读页面顶部的 `atlas-facts` JSON 块（**唯一**数字来源）。"""
    m = re.search(
        r'<script type="application/json" id="atlas-facts">(.*?)</script>',
        _html(), re.DOTALL)
    assert m, "页面里找不到 `atlas-facts` 块 —— 数字的唯一来源没了，判据无法工作"
    return json.loads(m.group(1))


# ======================================================================
# ① 代码现算：图与常量（与探针 `scripts/_probe_atlas_truth.py` 同一套口径）
# ======================================================================


def _graph_facts() -> dict:
    from src.orchestration import supervisor as sup

    compiled = sup.build_research_graph({}, chain_path="", llm_audit_path="")
    g = compiled.get_graph()
    nodes = sorted(n for n in g.nodes if n not in ("__start__", "__end__"))
    edges = sorted((e.source, e.target) for e in g.edges)

    adj: dict[str, list[str]] = {}
    for a, b in edges:
        adj.setdefault(a, []).append(b)
    paths: list[list[str]] = []

    def _walk(node: str, acc: list[str]) -> None:
        outs = adj.get(node) or []
        if not outs:
            paths.append(acc + [node])
            return
        for nxt in outs:
            _walk(nxt, acc + [node])

    _walk("__start__", [])

    depth: dict[str, int] = {"__start__": 0}

    def _level(n: str) -> int:
        if n in depth:
            return depth[n]
        parents = [a for a, b in edges if b == n]
        depth[n] = 1 + max((_level(p) for p in parents), default=0)
        return depth[n]

    for n in nodes + ["__end__"]:
        _level(n)
    widths: dict[int, int] = {}
    for n in nodes:
        widths[depth[n]] = widths.get(depth[n], 0) + 1

    return {
        "graph_nodes": len(nodes),
        "graph_edges": len(edges),
        "analyze_nodes": sum(1 for n in nodes if n.startswith("analyze_")),
        "recommend_in_degree": sum(1 for _, t in edges if t == "recommend"),
        "root_to_leaf_paths": len(paths),
        "path_nodes_incl_sentinels": sorted({len(p) for p in paths})[0]
        if len({len(p) for p in paths}) == 1 else -1,
        # ★ 同一件事的**两种数法**（PRD §43.2 用前者、图谱图内用后者）：
        #   含 `__start__`/`__end__` 两个哨兵 ⇒ 11 个节点 / 10 条边；
        #   只数真实节点 ⇒ 9 个 / 8 跳。**两个数都对，混用才是错的** ——
        #   这正是"数量不一致"投诉最常见的一种成因。
        "path_nodes_real": sorted({len(p) for p in paths})[0] - 2
        if len({len(p) for p in paths}) == 1 else -1,
        "path_edges_real": sorted({len(p) for p in paths})[0] - 3
        if len({len(p) for p in paths}) == 1 else -1,
        "max_superstep_width": max(widths.values()),
        "all_insight_agents": len(sup.ALL_INSIGHT_AGENTS),
        "analysis_agents": len(sup.ANALYSIS_AGENTS),
        "industry_agents": len(sup.INDUSTRY_AGENTS),
        "keyworded_industry_agents": len(sup.KEYWORDED_INDUSTRY_AGENTS),
        "info_agents": len(sup.INFO_AGENTS),
        "data_pipeline_agents": len(sup.DATA_PIPELINE_AGENTS),
        "industry_keyword_groups": len(sup.INDUSTRY_KEYWORDS),
        "industry_keywords": sum(len(v) for v in sup.INDUSTRY_KEYWORDS.values()),
        "max_asks_per_agent": sup.MAX_ASKS_PER_AGENT,
        "_paths": paths,
        "_nodes": nodes,
    }


def _registered_agents() -> list[str]:
    """从装配点读"到底注册了几个 Agent"（不依赖 `configs/agents.yaml`）。

    ★ 两者**故意不同**：`agents.yaml` 是 19 条（A01–A18 + A20），
      而运行期装配是 20 个（多一个 `A19_code_engineer`，
      它不挂在投研主图上、由缺口队列单独调用）。页面写的是**运行期**口径，
      所以判据也必须读运行期那一处。
    """
    src = (ROOT / "src" / "api" / "runtime.py").read_text(encoding="utf-8")
    m = re.search(r"agents: dict\[str, Any\] = \{(.*?)\n    \}",
                  src, re.DOTALL)
    assert m, ("`src/api/runtime.py` 里的 `agents: dict[str, Any] = {...}` "
               "装配块找不到 —— 图谱的 Agent 数判据失去依据")
    return re.findall(r'"(A\d{2}_[A-Za-z_]+)":', m.group(1))


def _planner_limits() -> tuple[int, int]:
    src = (ROOT / "src" / "orchestration" / "planner.py").read_text(encoding="utf-8")
    agents = re.search(r"max_agents: int = (\d+)", src)
    inds = re.search(r"isinstance\(i, str\)\]\[:(\d+)\]", src)
    assert agents and inds, "planner.py 里的两个上限找不到（改写了判据要同步）"
    return int(agents.group(1)), int(inds.group(1))


def _react_max_steps() -> int:
    src = (ROOT / "src" / "orchestration" / "supervisor.py").read_text(encoding="utf-8")
    m = re.search(r'MOSS_REACT_MAX_STEPS", "(\d+)"', src)
    assert m, "A17 ReAct 步数的默认值找不到（`MOSS_REACT_MAX_STEPS` 读法变了）"
    return int(m.group(1))


# ======================================================================
# ② 判据
# ======================================================================


def test_facts_block_matches_the_code() -> None:
    """★ 页面声明的每个数字，都要能被代码现算出来。"""
    facts = _facts()
    computed = _graph_facts()

    # 装配点单独算（20 个，含不挂主图的 A19）
    registered = _registered_agents()
    assert len(registered) == facts["agents_registered"], (
        f"页面写已注册 {facts['agents_registered']} 个 Agent，"
        f"而 `runtime.py` 装配了 {len(registered)} 个：{sorted(registered)}")
    assert "A20_generic_industry" in registered, (
        "兜底行业 Agent 从装配点消失了 —— 图谱的行业层叙事要跟着改")
    assert "A19_code_engineer" in registered, (
        "A19 从装配点消失了 —— 图谱说它'不挂主图但在册'的话就不再成立")

    for key, value in computed.items():
        if key.startswith("_"):
            continue
        assert facts.get(key) == value, (
            f"`{key}` 漂了：页面写 {facts.get(key)!r}，代码现算是 {value!r} —— "
            "改完代码请同步 `atlas-facts` 与正文（判据不替你改文档）")

    p_agents, p_inds = _planner_limits()
    assert facts["planner_max_agents"] == p_agents, f"planner max_agents={p_agents}"
    assert facts["planner_max_indicators"] == p_inds, f"planner 截断={p_inds}"

    assert facts["react_max_steps"] == _react_max_steps(), (
        "A17 ReAct 步数变了 —— 图谱里至少 6 处写着它（§2/§4/§7 与时序图）")

    from src.domain.skills.library import DEFAULT_AGENT_DIRS

    assert facts["skills_agent_dirs"] == len(DEFAULT_AGENT_DIRS), (
        f"`DEFAULT_AGENT_DIRS` 现在有 {len(DEFAULT_AGENT_DIRS)} 个键，"
        f"页面写 {facts['skills_agent_dirs']}")


def test_the_two_facts_that_were_wrong_are_the_ones_this_guards() -> None:
    """**自证**：老页面那两个错数，今天必须被判红。

    这不是多余的 —— 它证明"这条判据真的能抓住这次的事故形态"，
    而不是一条永远绿的装饰。若哪天有人把判据放松到"只要 facts 块自洽"，
    这里会立刻变红。
    """
    facts = _facts()
    computed = _graph_facts()
    assert computed["graph_nodes"] != 16, "老页面的 16 个节点已不再成立（这正是事故）"
    assert computed["all_insight_agents"] != 9, "老页面的 9 个已不再成立（这正是事故）"
    assert facts["graph_nodes"] == computed["graph_nodes"]
    assert facts["all_insight_agents"] == computed["all_insight_agents"]


def test_prose_cards_carry_the_facts() -> None:
    """★ facts 块改了、正文没改 ⇒ 这里红。

    页面是给人看的：数字只在 JSON 里对、卡片上还写着旧值，等于没修。
    """
    html = _html()
    facts = _facts()
    must_appear = [
        f"<b>已注册Agent</b><div class=\"v\">{facts['agents_registered']} 个</div>",
        f"<b>依赖图节点</b><div class=\"v\">{facts['graph_nodes']} 个</div>",
        f"<b>最大并行度</b><div class=\"v\">{facts['max_superstep_width']} 路</div>",
        f"build_research_graph {facts['graph_nodes']}节点",
        f"{facts['graph_nodes']} 个 <code>add_node</code>",
        f"{facts['graph_edges']} 条 <code>add_edge</code>",
        f"{facts['all_insight_agents']} 个",
        f"4 组共 <b>{facts['industry_keywords']}</b> 个词",
        f"max_steps={facts['react_max_steps']}",
    ]
    missing = [s for s in must_appear if s not in html]
    assert not missing, (
        "正文里找不到这些字符串（facts 与正文对不上）：\n  "
        + "\n  ".join(missing))


def test_figure_count_matches_the_page() -> None:
    """页脚的"包含 N 张 Mermaid 图"必须等于页面里真实的图块数。"""
    html = _html()
    blocks = html.count('<div class="mermaid">')
    caps = html.count('class="cap"')
    assert blocks == caps, (
        f"{blocks} 个图块但 {caps} 个图注 —— 有一张图没写标题（或反之）")
    assert _facts()["mermaid_figures"] == blocks, (
        f"页面有 {blocks} 张图，`atlas-facts` 声明 {_facts()['mermaid_figures']} 张")
    assert f"包含 <b>{blocks} 张 Mermaid 图</b>" in html, (
        "页脚没写对图的数量（它在正文里是给人看的那个数）")


# ----------------------------------------------------------------------
# ②b 页面结构（手改 HTML 最容易犯的错其实不是写错数字，而是标签没闭合 ——
#     表格/图块被吞掉，页面看着就少一块）
# ----------------------------------------------------------------------

_VOID = {"br", "hr", "img", "meta", "link", "input", "source"}


class _TagBalance(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, int]] = []
        self.problems: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:  # type: ignore[no-untyped-def]
        if tag not in _VOID:
            self.stack.append((tag, self.getpos()[0]))

    def handle_endtag(self, tag: str) -> None:
        if tag in _VOID:
            return
        if not self.stack:
            self.problems.append(f"第 {self.getpos()[0]} 行：多了一个 </{tag}>")
            return
        open_tag, line = self.stack.pop()
        if open_tag != tag:
            self.problems.append(
                f"第 {self.getpos()[0]} 行：</{tag}> 与第 {line} 行的 <{open_tag}> 不匹配")


def test_html_tags_are_balanced() -> None:
    """标签配平 —— 一次手改把 `</table>` 吃掉，页面会静默少一整张表。"""
    p = _TagBalance()
    p.feed(_html())
    leftover = [f"<{t}>（第 {ln} 行）没有闭合" for t, ln in p.stack]
    problems = p.problems + leftover
    assert not problems, "HTML 标签不配平：\n  " + "\n  ".join(problems)


def test_mermaid_subgraphs_are_balanced() -> None:
    """flowchart 里的 `subgraph` 与 `end` 必须配平。

    ⚠️ `sequenceDiagram` **不适用**：那里的 `end` 收的是
    `par` / `loop` / `alt` / `Note over` 块（第一版判据没区分，
    把时序图误判成"多了 6 个 end"—— 判据自己错比漏判更坏）。
    """
    bad: list[str] = []
    for i, body in enumerate(
            re.findall(r'<div class="mermaid">(.*?)</div>', _html(), re.DOTALL), 1):
        lines = [ln.strip() for ln in body.strip().splitlines() if ln.strip()]
        head = lines[0] if lines else "(空)"
        if not head.startswith(("flowchart", "graph")):
            continue
        subs = sum(1 for ln in lines if ln.startswith("subgraph "))
        ends = sum(1 for ln in lines if ln == "end")
        if subs != ends:
            bad.append(f"图 {i}（{head}）：subgraph {subs} 个，end {ends} 个")
    assert not bad, "Mermaid 子图不配平：\n  " + "\n  ".join(bad)


# ----------------------------------------------------------------------
# ③ 代码锚点
# ----------------------------------------------------------------------

_ANCHOR_RX = re.compile(r'class="file">([^<]+)</td>')


def _anchors() -> list[tuple[str, int, str]]:
    """抽出页面里所有 `路径:行号` 锚点 ⇒ [(原文, 行号, 路径)]。"""
    out: list[tuple[str, int, str]] = []
    for cell in _ANCHOR_RX.findall(_html()):
        for part in re.split(r"<br/>|\n", cell):
            part = part.strip()
            m = re.match(r"^([A-Za-z0-9_./]+\.py):(\d+)(?:[-,](\d+))?$", part)
            if m:
                out.append((part, int(m.group(2)), m.group(1)))
    return out


def _resolve(abbrev: str) -> list[Path]:
    """把页面里的**简写路径**（如 `llm/cache.py`、`domain/skills/liquidity_cycle/`）
    解析到仓库里的真实文件/目录。

    简写是刻意的（表格里放全路径太吵），所以这里按后缀匹配；
    匹配到多个就都返回，由断言去要求"至少一个命中"。
    """
    suffix = Path(abbrev.rstrip("/")).as_posix()
    hits = [p for p in ROOT.glob(f"src/**/{suffix}") if p.is_file() or p.is_dir()]
    if abbrev.endswith(".py") and "/" not in abbrev:
        hits += [p for p in ROOT.glob(f"src/**/{abbrev}") if p.is_file()]
    return sorted(set(hits))


def test_every_file_anchor_resolves_and_is_in_range() -> None:
    """第 4 层：锚点指向的文件必须存在，行号不许越界。"""
    anchors = _anchors()
    assert len(anchors) >= 30, (
        f"只解析出 {len(anchors)} 个锚点 —— 表格结构可能被改坏了（判据会假绿）")
    bad: list[str] = []
    for raw, line, abbrev in anchors:
        hits = _resolve(abbrev)
        if not hits:
            bad.append(f"{raw}：仓库里找不到这个文件（简写路径错了？）")
            continue
        if not any(line <= len(p.read_text(encoding="utf-8").splitlines())
                   for p in hits):
            bad.append(f"{raw}：行号超过所有候选文件的总行数")
    assert not bad, "锚点越界/文件不存在：\n  " + "\n  ".join(bad)


def test_every_referenced_file_still_exists() -> None:
    """连**不带行号**的文件引用也要在（§5 那些"本地计算在哪"的单元格）。

    这一类单元格只写符号名不写行号（符号比行号稳定），所以第 5 层管不到它们；
    但"文件被改名/删掉"这种漂移仍然必须报出来。
    """
    missing: list[str] = []
    checked = 0
    for cell in _ANCHOR_RX.findall(_html()):
        head = cell.split("<br/>")[0].split("\n")[0].strip()
        head = re.sub(r":[\d,\-]+$", "", head).strip()
        if not (head.endswith(".py") or head.endswith("/")):
            continue
        checked += 1
        if not _resolve(head):
            missing.append(f"{head}（出现在锚点单元格里，但仓库里找不到）")
    assert checked >= 10, f"只检查到 {checked} 个文件引用 —— 解析规则可能失效了"
    assert not missing, "被引用的文件不存在：\n  " + "\n  ".join(missing)


#: 第 5 层：**人工挑选**的高风险锚点 ⇒ (页面里的原文, 那一行必须含有的片段)
#:
#: 为什么要人工挑：第 4 条判据抓不住"行号漂到文件中间"—— 那种错最常见，
#: 也最坏（看起来最精确，却指着一行无关代码）。这张表把"这一行是什么"也钉住。
#: ⚠️ `_STORAGE_FALLBACK_LIMITS_DEFAULT` 那一条是**反面教材**：
#:    被引用的 `_STORAGE_FALLBACK_LIMITS` 已经删了，页面必须指向现行常量。
_PINNED: list[tuple[str, str, str]] = [
    ("src/orchestration/supervisor.py", "supervisor.py:1326", "_PLANNING"),
    ("src/orchestration/supervisor.py", "supervisor.py:217", "INDUSTRY_KEYWORDS"),
    ("src/orchestration/supervisor.py", "supervisor.py:902", "TOPIC_KEYWORDS"),
    ("src/orchestration/supervisor.py", "supervisor.py:1257", "_MARKET_LIQUIDITY_INDICATORS"),
    ("src/orchestration/supervisor.py", "supervisor.py:1267", "_BOARD_LIQUIDITY_INDICATORS"),
    ("src/orchestration/supervisor.py", "supervisor.py:199", "ANALYSIS_AGENTS"),
    ("src/orchestration/supervisor.py", "supervisor.py:210", "INDUSTRY_AGENTS"),
    ("src/orchestration/supervisor.py", "supervisor.py:227", "INFO_AGENTS"),
    ("src/orchestration/supervisor.py", "supervisor.py:228", "DATA_PIPELINE_AGENTS"),
    ("src/orchestration/supervisor.py", "supervisor.py:230", "ALL_INSIGHT_AGENTS"),
    ("src/orchestration/supervisor.py", "supervisor.py:206", "MAX_ASKS_PER_AGENT"),
    ("src/orchestration/supervisor.py", "supervisor.py:4214",
     "_STORAGE_FALLBACK_LIMITS_DEFAULT"),
    ("src/orchestration/supervisor.py", "supervisor.py:4509", "askable_prefixes"),
    ("src/orchestration/planner.py", "planner.py:402", "max_agents"),
    ("src/orchestration/planner.py", "planner.py:541", "[:20]"),
    ("src/domain/agents/analysis/base.py", "analysis/base.py:747", "payload.events[:15]"),
    ("src/domain/agents/analysis/base.py", "analysis/base.py:749",
     "payload.verified_texts[:5]"),
    ("src/domain/agents/analysis/base.py", "analysis/base.py:808", "repair_prompt"),
    ("src/domain/agents/analysis/macro/agent.py", "analysis/macro/agent.py:131",
     "context_max_periods"),
    ("src/domain/agents/analysis/micro/agent.py", "analysis/micro/agent.py:104",
     "context_max_periods"),
    ("src/domain/agents/analysis/micro/agent.py", "analysis/micro/agent.py:85",
     "_MIN_PERCENTILE_SAMPLE"),
    ("src/domain/agents/analysis/react.py", "analysis/react.py:43", "_OBS_TRUNCATE"),
    ("src/domain/agents/industry/base.py", "industry/base.py:23", "_CONTEXT_PERIODS"),
    ("src/domain/skills/library.py", "skills/library.py:32", "L0_INDEX_TOKEN_LIMIT"),
    ("src/domain/skills/library.py", "skills/library.py:34", "BODY_TOKEN_LIMIT"),
    ("src/domain/skills/library.py", "skills/library.py:232", "max_skills"),
    ("src/domain/skills/library.py", "skills/library.py:38", "DEFAULT_AGENT_DIRS"),
    ("src/api/routes/research.py", "routes/research.py:112", "_RESULT_CACHE_TTL"),
    ("src/infrastructure/llm/cache.py", "llm/cache.py:482", "semantic_threshold"),
    ("src/core/config.py", "config.py:248", "llm_timeout_seconds"),
    ("src/core/config.py", "config.py:300", "llm_cache_ttl_hours"),
    ("src/core/config.py", "config.py:473", "llm_token_budget_per_task"),
    ("src/core/config.py", "config.py:486", "llm_max_tokens_hard_cap"),
    ("src/core/config.py", "config.py:488", "llm_input_char_hard_cap"),
    ("src/infrastructure/llm/gateway.py", "llm/gateway.py:116", "_TIER_REASONING_EFFORT"),
    ("src/infrastructure/llm/gateway.py", "llm/gateway.py:126", "_TIER_OUTPUT_BUDGET"),
    ("src/infrastructure/llm/gateway.py", "llm/gateway.py:159", "_ATTEMPT_BUDGET"),
    ("src/infrastructure/llm/gateway.py", "llm/gateway.py:187", "_OUTPUT_TOKENS_PER_SEC"),
    ("src/infrastructure/llm/providers.py", "llm/providers.py:121", "_CONFIG_STATUS"),
    ("src/infrastructure/llm/circuit_breaker.py", "llm/circuit_breaker.py:328", "deepseek"),
    ("src/infrastructure/llm/circuit_breaker.py", "llm/circuit_breaker.py:334", "ollama"),
    ("src/infrastructure/connectors/code_validator.py",
     "connectors/code_validator.py:59", "SANDBOX_TIMEOUT_SEC"),
    ("src/infrastructure/repositories/audit_chain.py",
     "repositories/audit_chain.py:101", "_record_hash"),
    ("src/domain/agents/audit/verifier/agent.py",
     "audit/verifier/agent.py:187", "_check_completeness"),
    ("src/domain/agents/engineering/code_engineer/agent.py",
     "engineering/code_engineer/agent.py:231", "range(3)"),
    ("src/domain/agents/data_gap_resolver.py", "data_gap_resolver.py:159", "_max_attempts"),
    ("src/domain/agents/decision/recommend/agent.py", "recommend/agent.py:364", "repair"),
]


def _pinned_problems(html: str,
                     pairs: list[tuple[str, str, str]]) -> list[str]:
    """第 5 层的**纯逻辑**：给定 (真实路径, 页面锚点, 必须含有的片段) ⇒ 问题清单。

    抽成纯函数是为了能被**自证**调用（见 `test_the_pinned_check_can_fail`）：
    "这条判据抓不抓得住行号漂移"必须能被证明，而不是靠相信。
    """
    bad: list[str] = []
    for rel, token, needle in pairs:
        if token not in html:
            bad.append(f"{token}：页面里已经没有这个锚点了"
                       f"（若是有意删除，请同步 _PINNED）")
            continue
        path = ROOT / rel
        if not path.exists():
            bad.append(f"{token}：{rel} 不存在（_PINNED 写错了）")
            continue
        lines = path.read_text(encoding="utf-8").splitlines()
        line_no = int(token.rsplit(":", 1)[1])
        if line_no > len(lines):
            bad.append(f"{token}：越界（{rel} 只有 {len(lines)} 行）")
        elif needle not in lines[line_no - 1]:
            bad.append(
                f"{token}：该行现在是 {lines[line_no - 1].strip()[:70]!r}，"
                f"不含 {needle!r} —— 行号漂了")
    return bad


def test_pinned_anchors_still_point_at_their_symbol() -> None:
    """★★ 第 5 层：行号漂了、但文件还在 ⇒ 只有这条能抓住。"""
    bad = _pinned_problems(_html(), _PINNED)
    assert not bad, (
        "代码锚点漂移（这正是 2026-10-15 那次事故的形态）：\n  "
        + "\n  ".join(bad))


def test_the_pinned_check_can_fail() -> None:
    """**自证**：把锚点指到一个"存在但不相干"的行，必须被判红。

    这一条抓的是**最坏的那种错**：文件在、行号不越界、锚点看着最精确，
    但那一行是别的东西（§8 旧版整张表都是这个样子）。
    反例取自真实事故：`ALL_INSIGHT_AGENTS` 曾经被写成 `supervisor.py:46`。
    """
    html = _html()
    # ① 锚点指向同文件里一个**存在但无关**的行（229 = 空行/分隔）。
    #    ⚠️ 这里必须用**合成页面**：真页面里没有 `supervisor.py:229`，
    #    而判据会先判"锚点在不在页面上"——用真页面测到的会是那一条分支。
    #    合成一行"页面里确实写着 229"的最小 HTML，才是在测内容分支本身。
    fake_page = '<td class="file">supervisor.py:229</td>'
    problems = _pinned_problems(
        fake_page, [("src/orchestration/supervisor.py", "supervisor.py:229",
                     "ALL_INSIGHT_AGENTS")])
    assert problems and "不含" in problems[0], (
        f"指向无关行时没报红 —— 第 5 层判据是坏的：{problems}")

    # ② 锚点被搬到文件外（同样用合成页面，隔离出越界那一支）
    fake_page = '<td class="file">supervisor.py:999999</td>'
    problems = _pinned_problems(
        fake_page, [("src/orchestration/supervisor.py", "supervisor.py:999999",
                     "ALL_INSIGHT_AGENTS")])
    assert problems and "越界" in problems[0], f"越界没报红：{problems}"

    # ③ 锚点被从页面上删掉
    problems = _pinned_problems(
        "<html></html>", [("src/orchestration/supervisor.py", "supervisor.py:230",
                           "ALL_INSIGHT_AGENTS")])
    assert problems and "没有这个锚点" in problems[0], f"删锚点没报红：{problems}"

    # ④ 对照：正确的锚点必须**不报**（否则判据是恒红的，同样没用）
    assert _pinned_problems(
        html, [("src/orchestration/supervisor.py", "supervisor.py:230",
                "ALL_INSIGHT_AGENTS")]) == []
