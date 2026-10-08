"""`CHG-0240`：信息层（`A05`/`A06`/`A07`）的参与判据 —— **两条规划路径共用一份**。

## 这个文件在防什么

真实端到端跑出来的形态（两次同问句，`task_20261008_09a27591` / `task_20261008_b3b2adc2`）：

  · 提交响应的 `plan` 里**有** A05/A06/A07；
  · dev 审计里该 trace **只有 8 次调用**（planner/A08/A10/A11/A12/A20/A09/A17）⇒ 信息层零调用；
  · 而 `_fetch_news_per_code` 单独实测**返回 20 条**（每只 10 条、代码归属正确）
    ⇒ **不是"没有新闻"，是没人去处理新闻**。

根因是"同一判断两份实现"：规则路径 `plan_run()` 确定性追加信息层，
LLM 路径的 `state["plan"]` 直接来自 `llm_plan["agents"]`（**模型选的**）。
本文件的判据钉住"**确定性的那一份**"，并钉住"两条路径都调它"。

跑法：
    uv run python -m pytest tests/unit/test_info_agents_judge.py -q
"""
from __future__ import annotations

import ast
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from src.orchestration.supervisor import (  # noqa: E402
    INFO_AGENTS,
    ensure_info_agents,
    plan_run,
)

QUERY = (
    "当前宏观环境如何，预测下未来一年美国的加息预期下，基于当前板块拥挤度和能源重点项目"
    "与新业态投资20万亿的政策，未来半年能否持有高股息的宁波银行和中国神华？"
)


def test_stock_target_gets_the_info_layer() -> None:
    """个股问法（`target` 是 6 位码 + `full`/`stock`）⇒ 必须挂信息层。"""
    got = ensure_info_agents(["A10_micro"], info_items=None,
                             analysis_type="full", target="601088", query="看下这只票")
    assert [a for a in INFO_AGENTS if a in got] == list(INFO_AGENTS)


def test_focus_codes_also_trigger_it() -> None:
    """★ 问句点名了个股但 `target` 不是代码（只填了板块）时**也要**挂。

    这正是报障问句的另一半：两只票都点了名，新闻却只按 `target` 取。
    """
    got = ensure_info_agents(["A08_macro"], info_items=None, analysis_type="full",
                             target="煤炭开采", query="宁波银行和中国神华能否持有",
                             focus_stock_codes=("002142", "601088"))
    assert all(a in got for a in INFO_AGENTS)


def test_pure_macro_query_is_not_polluted() -> None:
    """★★ 反事实护栏：**既没个股、也没主题词**的宏观问法不许被挂上信息层。

    判据若写成"无条件追加"，这条必红（白跑 3 次本地模型调用）。

    ⚠️ 我第一次把 query 写成"美国加息路径怎么看" —— 那条**会**被挂上，
    因为 `extract_topic_keywords()` 能从中提出主题词，而"有主题词就拉全球财经快讯"
    是**既有设计**（不是缺陷）。所以这条判据的输入必须真的什么信号都没有。
    """
    got = ensure_info_agents(["A08_macro"], info_items=None, analysis_type="macro",
                             target="", query="")
    assert not [a for a in INFO_AGENTS if a in got], got


def test_macro_query_with_topic_keywords_does_get_them() -> None:
    """反过来钉住"有主题词 ⇒ 挂"这一支（免得上面那条被"改宽"成永远不挂）。"""
    got = ensure_info_agents(["A08_macro"], info_items=None, analysis_type="macro",
                             target="", query="美国加息路径怎么看")
    assert all(a in got for a in INFO_AGENTS), got


def test_explicit_info_items_always_win() -> None:
    got = ensure_info_agents(["A08_macro"], info_items=[{"title": "x"}],
                             analysis_type="macro", target="", query="")
    assert all(a in got for a in INFO_AGENTS)


def test_helper_is_idempotent_and_order_preserving() -> None:
    """重复调用不重复插入，且**不改变已有顺序**（计划顺序会影响前端展示）。"""
    once = ensure_info_agents(["A10_micro", "A09_meso"], info_items=None,
                              analysis_type="full", target="601088", query="")
    twice = ensure_info_agents(list(once), info_items=None,
                               analysis_type="full", target="601088", query="")
    assert twice == once
    assert once[:2] == ["A10_micro", "A09_meso"], once


def test_rule_path_still_includes_them() -> None:
    """规则路径（`plan_run`）的既有行为**不许退化** —— 它本来就是对的。"""
    plan = plan_run("full", "601088", None, query=QUERY)
    assert all(a in plan["agents"] for a in INFO_AGENTS), plan["agents"]


def test_both_paths_call_the_shared_judge() -> None:
    """★★ 接线判据：**两条**规划路径都必须调用 `ensure_info_agents`。

    `ast` 判据（与 `tests/unit/test_per_item_runtime_guard.py` 的接线判据同款手法）：
    只测 `plan_run` 会漏掉 LLM 路径 —— 而**这次出问题的正是 LLM 路径**。
    """
    tree = ast.parse((ROOT / "src" / "orchestration" / "supervisor.py").read_text("utf-8"))
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call)
             and getattr(n.func, "id", None) == "ensure_info_agents"]
    assert len(calls) >= 2, f"只有 {len(calls)} 处调用 —— 另一条路径没接上"
    # `plan_run` 里的那一处：模块级函数、且传了 auto_topic
    assert sum(1 for c in calls if any(k.arg == "auto_topic" for k in c.keywords)) >= 1
