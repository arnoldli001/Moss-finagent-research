"""数据缺口自修复**接回活路径** + `self_heal_pending` **是真标记**的护栏。

（`CHG-0190` ② 幻影标记 / `CHG-0192` ① 图内自修复是死代码 / PRD §46）

## 缺陷本体（本轮实测）

- `_try_self_heal`（LLM 生成连接器 → 沙箱 → 重试 fetch）**唯一**的调用点在
  `_collect_one`，而后者**全仓零引用**（AST：定义 1 处、`Load` 引用 0 处）
  ⇒ 图内自修复**从未发生过一次**；
- 注释却声称"结果写到 `self_heal_pending` 标记，由盘后批量作业回填" ——
  而 `grep self_heal_pending` 全仓**只命中那句注释**，`ResearchState` 无此 channel
  ⇒ 按 `src/core/state.py:54-55` 的教训，它**不可能**生效。

## 本文件的判据（三类，缺一类就是假绿）

1. **可达性（结构）**：从 `collect_node` 出发，调用图必须能走到 `_try_self_heal`；
   且 `_collect_one` **不许再存在**（删掉的死代码不许回来）。
2. **规则（纯函数）**：单轮上限 / 护栏 `guarded` 标记 / **预检不消耗配额**。
3. **消费（行为）**：自修复没成 ⇒ 必须真的进 `gap_queue`（盘后 `gap_drain` 读它），
   且 channel 在真实 `StateGraph` 里是**累加**而不是覆盖。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from langgraph.graph import END, StateGraph

from src.core.state import ResearchState
from src.domain.agents.decision import gap_queue as gap_queue_mod
from src.orchestration import supervisor as sup

_ROOT = Path(__file__).resolve().parents[2]
_SUPERVISOR = _ROOT / "src" / "orchestration" / "supervisor.py"
_UNDECLARED = "_definitely_not_a_declared_channel"


@pytest.fixture(autouse=True)
def _isolate_heal_module_state():
    """隔离自修复的**模块级**状态（配额窗口 / 1h 失败缓存 / 在飞任务集合）。

    ## 为什么必须（本轮实测的一次假红）

    `_self_heal_allowed` 是"**检查即记账**"的闸门，而集成测试会**真的**触发自修复：
    `test_supervisor_graph.py` 跑完采集链后，后台 `_heal_bg` → `_try_self_heal`
    会**消耗配额**并把失败指标写进 `_HEAL_FAIL_CACHE`（TTL 1h）。

    于是本文件单独跑 23 passed，**混在套件里跑就有 2 条假红**：
    "CPI"（集成测试里失败过的指标）被失败缓存挡住 ⇒ `_note_self_heal_candidate`
    返回 `None`。**这不是实现的问题，是判据没有隔离自己的状态。**
    与 `tests/conftest.py` 里那 9 条 autouse 隔离同一个理由：
    逐个用例写 try/finally 是治不住的（下一个人加用例时不会记得）。
    """
    saved_window = list(sup._HEAL_QUOTA_WINDOW)
    saved_fail = dict(sup._HEAL_FAIL_CACHE)
    saved_tasks = set(sup._HEAL_TASKS)
    sup._HEAL_QUOTA_WINDOW.clear()
    sup._HEAL_FAIL_CACHE.clear()
    try:
        yield
    finally:
        sup._HEAL_QUOTA_WINDOW[:] = saved_window
        sup._HEAL_FAIL_CACHE.clear()
        sup._HEAL_FAIL_CACHE.update(saved_fail)
        sup._HEAL_TASKS.clear()
        sup._HEAL_TASKS.update(saved_tasks)


# ======================================================================
# ① 可达性：结构判据（精确调用图：不下钻嵌套函数体）
# ======================================================================


def _own_calls(fn: ast.AST) -> set[str]:
    """函数**自己**的调用集合 —— 刻意不下钻嵌套函数体，否则闭包会把父子关系糊平。"""
    called: set[str] = set()
    stack = list(getattr(fn, "body", []))
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue                     # 嵌套函数：单独成点，不并进来
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name):
                called.add(f.id)
            elif isinstance(f, ast.Attribute):
                called.add(f.attr)
        stack.extend(ast.iter_child_nodes(node))
    return called


def _direct_nested(fn: ast.AST) -> set[str]:
    """**直接**嵌套在里面的函数名（不下钻到孙辈函数体内）。"""
    out: set[str] = set()
    stack = list(getattr(fn, "body", []))
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.add(node.name)
            continue                     # 不下钻：孙辈归它自己的父亲管
        if isinstance(node, ast.ClassDef):
            continue
        stack.extend(ast.iter_child_nodes(node))
    return out


def _module_call_graph(path: Path) -> dict[str, set[str]]:
    """调用图 = **自己发出的调用** ∪ **直接嵌套定义的子函数**。

    ⚠️ 第二条边不能省（本判据第一版就栽在这里）：本项目大量使用**回调**形式
    ——`SmartFetcher.fetch_many(live_fetcher=live_fetch)` 里 `live_fetch` 只是
    **一个名字**，AST 里没有 `Call(func=Name('live_fetch'))`。只认调用边，
    就会把"真正跑的采集路径"判成不可达，得到一条**假红**。
    （实测：漏了这条边时可达集里没有 `_live_fetch_one`。）
    而它**仍然抓得住**原来的缺陷：`_collect_one` 是 `build_research_graph` 的
    兄弟嵌套函数，既不在 `collect_node` 里、也没人调它 ⇒ 不可达。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    graph: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            graph.setdefault(node.name, set()).update(
                _own_calls(node) | _direct_nested(node))
    return graph


def _reachable(graph: dict[str, set[str]], start: str) -> set[str]:
    seen: set[str] = set()
    stack = [start]
    while stack:
        cur = stack.pop()
        for nxt in graph.get(cur, set()):
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return seen


def test_self_heal_is_reachable_from_the_live_collect_path() -> None:
    """★ 从 `collect_node` 出发必须能走到 `_try_self_heal`。

    这是缺陷①的**结构回归判据**：原先唯一的调用点在一个零引用的死函数里，
    所以"接在没人走的路上 = 没接"。删掉 `_schedule_self_heal`/`_heal_bg`
    里的任何一跳，这条立刻红。
    """
    graph = _module_call_graph(_SUPERVISOR)
    seen = _reachable(graph, "collect_node")
    assert "_try_self_heal" in seen, (
        "从 collect_node 走不到 _try_self_heal —— 图内自修复又变回死代码了。"
        f"可达集：{sorted(n for n in seen if n.startswith('_'))}")
    assert "_schedule_self_heal" in seen
    assert "_live_fetch_one" in seen, "采集活路径本身都不可达，判据坏了"


def test_dead_collect_one_stays_deleted() -> None:
    """`_collect_one` 不许回来（它是缺陷①的宿主：零引用 + 幻影注释）。"""
    graph = _module_call_graph(_SUPERVISOR)
    assert "_collect_one" not in graph, (
        "死代码 `_collect_one` 又出现了 —— 先确认它真的被调用，否则就是又接在没人走的路上")
    src = _SUPERVISOR.read_text(encoding="utf-8")
    assert "self_heal_pending\" 标记" not in src, (
        "注释里又出现「结果写到 self_heal_pending 标记」这类**未兑现的声明**"
        " —— 注释里的承诺必须有实现兜着，否则就是下一个幻影标记")


def test_hook_sits_on_the_missing_convergence_point() -> None:
    """★★ 钩子必须挂在 `collect_node` 的 **missing 汇合点**上。

    ## 为什么单独立一条（本轮实测踩过）

    第一版把自修复挂进 `_live_fetch_one` 的「本地与联网均未取到」分支 ——
    看起来是最准的语义点，实测**被调用 0 次**而 13 条缺口照样产生：
    连接器路径下 `live_fetcher` **根本不会被调用**（SmartFetcher 自己取完就返回空）。
    ⇒ 又踩了本项目那句老话：**判据接在没人走的路上 = 没接。**

    真正的汇合点是 `collect_node` 里 `got_empty` + `result.missing` 两个循环。
    这条判据把"必须在汇合点上"钉死：把钩子挪回任何单分支，它立刻红。
    """
    graph = _module_call_graph(_SUPERVISOR)
    assert "_note_self_heal_candidate" in graph.get("collect_node", set()), (
        "`collect_node` 没有直接调用 `_note_self_heal_candidate` —— "
        "自修复钩子又被挪到某个单分支（例如 `_live_fetch_one`）上了。\n"
        "连接器路径下那条分支**不会被执行**（实测 0 次调用），缺口会静默失去自修复。")
    assert "_schedule_self_heal" in graph.get("collect_node", set()), (
        "`collect_node` 没有调度自修复 —— 登记了却不试，与幻影标记没有区别")


def test_schedule_keeps_a_strong_reference() -> None:
    """★ `create_task` 只保留弱引用 ⇒ 必须存强引用，否则任务被 GC 静默取消。

    本项目已在 `src/api/routes/intel.py:419` 记过这条坑。
    """
    src = _SUPERVISOR.read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and n.name == "_schedule_self_heal")
    calls = _own_calls(fn)
    assert "create_task" in calls
    assert "_HEAL_TASKS" in ast.dump(fn), "没把任务存进 _HEAL_TASKS（会被 GC 静默取消）"
    assert "add_done_callback" in calls
    assert isinstance(sup._HEAL_TASKS, set)


# ======================================================================
# ② 规则：模块级纯函数（原先埋在闭包里，除了跑整张图没法验证）
# ======================================================================


def test_candidate_is_registered_and_asks_for_schedule() -> None:
    updates: dict = {}
    entry = sup._note_self_heal_candidate(updates, "CPI", "local", "DataFetchError: x")
    assert entry is not None, "真缺口应当返回「要调度」"
    assert updates["self_heal_pending"] == [entry]
    assert entry["indicator"] == "CPI" and entry["stage"] == "local"
    assert "guarded" not in entry


def test_candidate_respects_round_cap() -> None:
    """单轮上限：第 11 条（默认上限 10）不再登记、也不再调度（防刷屏）。"""
    updates: dict = {}
    for i in range(sup._SELF_HEAL_MAX_PER_ROUND):
        assert sup._note_self_heal_candidate(updates, f"ind_{i}", "local", "e") is not None
    assert sup._note_self_heal_candidate(updates, "ind_over", "local", "e") is None
    assert len(updates["self_heal_pending"]) == sup._SELF_HEAL_MAX_PER_ROUND


def test_candidate_is_guarded_but_still_visible() -> None:
    """被护栏拦下（黑名单）⇒ **登记但不去试**：`guarded=True` 且不调度。

    "没量到"与"量到不值得试"必须分开 —— 否则面板上会出现"这条缺口没人管"的假象。
    """
    blacklisted = sup._HEAL_BLACKLIST[0] + "whatever"
    sup._HEAL_FAIL_CACHE.pop(blacklisted, None)
    updates: dict = {}
    entry = sup._note_self_heal_candidate(updates, blacklisted, "local", "e")
    assert entry is None, "黑名单指标不该被调度（白烧一次 LLM）"
    assert updates["self_heal_pending"][0]["guarded"] is True


def test_precheck_does_not_consume_the_quota() -> None:
    """★★ 陷阱回归：预检**不消耗**配额，真正的尝试才消耗。

    闸门 `_self_heal_allowed` 是"检查即记账"的（`consume=True` 时返回 True 就把
    now 写进 60s 窗口）。若预检也消耗，"先判后调度"会在任务跑起来之前
    把 5 次/60s 的配额吃光 —— 而且是**静默**的。
    """
    saved = list(sup._HEAL_QUOTA_WINDOW)
    try:
        sup._HEAL_QUOTA_WINDOW.clear()
        n = sup._HEAL_QUOTA_MAX + 3
        for i in range(n):
            updates: dict = {}
            assert sup._note_self_heal_candidate(
                updates, f"quota_{i}", "local", "e") is not None, (
                f"第 {i + 1} 次预检被拒 —— 预检在消耗配额")
        assert sup._HEAL_QUOTA_WINDOW == [], "预检竟然改了限频窗口"

        # 对照：真正尝试（consume=True）必须消耗，第 6 次被拦
        for _ in range(sup._HEAL_QUOTA_MAX):
            assert sup._self_heal_allowed("ctl", "", consume=True) is True
        assert sup._self_heal_allowed("ctl", "", consume=True) is False, (
            "限频闸门失效了（判据本身坏了）")
    finally:
        sup._HEAL_QUOTA_WINDOW[:] = saved


# ======================================================================
# ③ 消费：自修复没成 ⇒ 真的进盘后队列
# ======================================================================


def test_heal_failure_enqueues_gap_for_offline_drain(tmp_path) -> None:
    """★ 消费判据（不是"断言字符串存在"）：真的往 `gap_queue` 里写一条。

    这正是那句幻影注释承诺的消费方（盘后 `gap_drain` 读 `pending()`）。
    """
    gap_queue_mod.reset_gap_queue_for_test()
    q = gap_queue_mod.get_gap_queue(tmp_path)      # 隔离到 tmp，绝不碰生产队列
    try:
        queued = sup._enqueue_self_heal_gap(
            "mkt:turnover:total", reason="自修复未成功（boom）", task_id="t1")
        assert queued is True, "自修复失败没有入队 ⇒ 缺口又变成没人管"
        items = q.pending()
        assert items, "队列里没有条目"
        assert any("mkt:turnover" in str(i.indicator) for i in items)
        assert any(str(getattr(i, "source", "")) == "self_heal" for i in items), (
            "source 不是 self_heal ⇒ 事后无法区分「采集侧缺口」与「A17 报的缺口」")
    finally:
        gap_queue_mod.reset_gap_queue_for_test()


# ======================================================================
# channel：声明 + 累加语义 + 对照
# ======================================================================


def test_self_heal_pending_is_declared_in_state_schema() -> None:
    assert "self_heal_pending" in ResearchState.__annotations__


def _roundtrip_two_nodes(second_payload: dict) -> dict:
    graph = StateGraph(ResearchState)
    graph.add_node("a", lambda _s: {"self_heal_pending": [{"indicator": "CPI"}]})
    graph.add_node("b", lambda _s: second_payload)
    graph.set_entry_point("a")
    graph.add_edge("a", "b")
    graph.add_edge("b", END)
    return graph.compile().invoke({
        "task_id": "t", "tenant_id": "x", "user_query": "q",
        "analysis_type": "full", "target": "",
    })


def test_self_heal_pending_accumulates_not_overwrites() -> None:
    """★ 列表通道必须**累加**（并发采集多路写入时，后写的不能把前面的整体覆盖）。

    这正是 `research.py::_ACCUMULATE_LIST_KEYS` 存在的理由 —— 那边也加了本键。
    """
    out = _roundtrip_two_nodes({"self_heal_pending": [{"indicator": "PPI"}]})
    got = [e["indicator"] for e in out.get("self_heal_pending", [])]
    assert got == ["CPI", "PPI"], f"通道被覆盖了：{got}"


def test_undeclared_key_is_dropped_as_control() -> None:
    out = _roundtrip_two_nodes({_UNDECLARED: [1]})
    assert _UNDECLARED not in out


def test_stream_aggregation_whitelist_contains_the_channel() -> None:
    """流式聚合白名单也必须含它（不加 ⇒ 前端看到的 pending 会被整体覆盖）。"""
    from src.api.routes.research import _ACCUMULATE_LIST_KEYS

    assert "self_heal_pending" in _ACCUMULATE_LIST_KEYS
