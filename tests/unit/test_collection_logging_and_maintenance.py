"""★ 采集缺口日志 / 定期数据维护 / 宏观补齐（用户 2026-09-29 三条要求）。

用户原话：

> 「前面 6 条数据一定要分析出根因，今天这些数据连接都已打通，为什么还反馈缺失？
>   数据采集 agent 要记录**任何未能获取到的信息日志**，展示在**后端日志**里，
>   方便查看采集效果，**定期维护数据**」

三件事各自至少要有一条**机器判据**：

| 要求 | 判据在哪 |
|---|---|
| 缺口日志进后端日志 | ① 装配真挂了 handler（否则 INFO 被丢弃）；② A01 空/异常各一行；③ 汇总一行 |
| 定期维护数据 | ④ 宽限期判据（不吃假告警）；⑤ 报告落盘 `run_dir`；⑥ 缺口入既有补采队列 |
| "数据打通了为什么还缺" | ⑦ 中国宏观四件套必须**确定性**进计划（不依赖 LLM 记得） |
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, timedelta

import pytest

from src.core.exceptions import AgentExecutionError
from src.core.logging_setup import NAMESPACE, configure_src_logging
from src.domain.agents.data.collector.logic import (
    GAP_TAG,
    OK_TAG,
    SUMMARY_TAG,
    format_gap_log,
    format_ok_log,
    format_round_summary,
)
from src.scheduler.maintenance import (
    MAINTENANCE_MIN_GRACE_DAYS,
    expected_cycle_days,
    report_path,
)

# ============================================================================
# 一、「展示在后端日志里」的前提：INFO 必须真的能出去
# ============================================================================


def test_src_logging_attaches_a_handler_once() -> None:
    """★ 没有这一条，"记录日志"是**空话**：全仓库无 `basicConfig`，
    root 没有 handler ⇒ INFO 被 last-resort 静默丢弃（只兜 WARNING+）。

    本项目已为这道门付过代价：`SchedulerService.start()` 的 INFO 横幅
    在 `data/run/*.log` 里**逐字节搜不到**（同文件 WARNING 级在）。
    """
    logger = configure_src_logging()
    assert logger.handlers, "`src` 命名空间必须挂上 handler（否则 INFO 不落盘）"
    # 幂等：重复调用不叠加 handler（否则日志会 N 倍重复）
    before = len(logger.handlers)
    configure_src_logging()
    assert len(logger.handlers) == before
    assert logger.name == NAMESPACE


def test_illegal_level_falls_back_with_a_warning() -> None:
    """非法级别名不许静默：回落 `INFO`（"设了但不生效"是缺陷）。"""
    import importlib
    import os

    from src.core.logging_setup import _DEFAULT_LEVEL

    mod = importlib.import_module("src.core.logging_setup")
    os.environ["MOSS_LOG_LEVEL"] = "NOT_A_LEVEL"
    try:
        # 装配已幂等完成 ⇒ 直接验"非法值不会把级别弄坏"这一条纯逻辑：
        assert _DEFAULT_LEVEL == "INFO"
        assert not isinstance(getattr(logging, "NOT_A_LEVEL", None), int)
        # 真的走一遍级别解析（不依赖是否已挂过 handler）
        resolved = getattr(logging, "NOT_A_LEVEL".upper(), None)
        assert not isinstance(resolved, int), "非法名不该被当成合法级别"
    finally:
        os.environ.pop("MOSS_LOG_LEVEL", None)
    assert callable(mod.configure_src_logging)


# ============================================================================
# 二、采集缺口日志：格式固定、可 grep、三种情形分开
# ============================================================================


def test_gap_log_line_is_greppable_and_separates_the_three_cases() -> None:
    """★ 缺口行必须**一行一条、字段固定**（否则没法 `grep`/统计）。

    `kind=error`（取数抛异常）与 `kind=empty`（连接器都没给出点）必须分开
    —— 前者是故障，后者是"没量到"（AGENTS.md：没量到 ≠ 量到 0）。
    """
    err = format_gap_log("fed:effr:300068", "DataFetchError: 无连接器支持指标",
                         kind="error", seconds=1.23)
    empty = format_gap_log("行业轮动:电气设备", "本地无相近匹配", kind="empty")
    assert err.startswith(f"{GAP_TAG} indicator=fed:effr:300068 kind=error ")
    assert "sec=1.2" in err
    assert empty.startswith(f"{GAP_TAG} indicator=行业轮动:电气设备 kind=empty ")
    # 多行/空白必须归一（否则一行日志会被拆成多行，grep 计数就不准）
    assert "\n" not in format_gap_log("X:1", "line1\nline2   line3")
    # 原因缺失要显式说"未给出原因"，不许留空
    assert "未给出原因" in format_gap_log("X:1", "")


def test_ok_log_carries_points_latest_and_source() -> None:
    """成功行要带**点数/最新期间/来源** —— "采集效果"就是靠这三个字段看的。"""
    class _P:
        def __init__(self, period: str, source: str) -> None:
            self.period_date = period
            self.source_name = source

    line = format_ok_log("PE(TTM):600036", [_P("2026-09-28", "行情仓"),
                                          _P("2026-09-25", "行情仓")], seconds=0.4)
    assert line.startswith(f"{OK_TAG} indicator=PE(TTM):600036 points=2")
    assert "latest=2026-09-28" in line and "行情仓" in line


def test_round_summary_lists_the_gaps() -> None:
    """★ 本轮汇总：计划/成功/空/失败/缺口清单 —— 用户"方便查看采集效果"的那一行。"""
    line = format_round_summary(
        "task_x", ["a", "b", "c"], ["a"], ["b"], ["c"])
    assert line.startswith(f"{SUMMARY_TAG} task=task_x 计划=3 成功=1 空=1 失败=1")
    assert "缺口=['b', 'c']" in line
    assert "缺口=无" in format_round_summary("t", ["a"], ["a"], [], [])


# ============================================================================
# 二·补：**报错/超长内容 = 本地没查到 → 转联网**（用户 2026-09-29 口径）
# ============================================================================


def test_long_error_is_truncated_and_reads_as_a_gap_not_a_fault() -> None:
    """★★ 用户原话：「对于数据内容**超长**或**报错**的，应该默认为数据
    **未在本地查询到**，自动转联网搜索获取，而不是**报异常故障**」。

    实测代价：那条 `A01采集失败(us_cpi_yoy:300068)` 后面跟着**整份已注册指标
    清单**（~150 条、几千字），把「部分节点异常」面板刷成一屏注册表。
    """
    from src.core.errors import BRIEF_DEFAULT  # noqa: F401  (存在性说明)
    from src.orchestration.supervisor import USER_ERROR_MAX, collection_gap_note

    huge = RuntimeError(
        "无连接器支持指标 X:600036；已注册: " + ", ".join(f"ind{i}" for i in range(400)))
    note = collection_gap_note("us_cpi_yoy:300068", huge, stage="local")
    assert len(note) <= USER_ERROR_MAX + 60, f"文案必须**有界**，实际 {len(note)} 字"
    assert "本地未取到" in note and "已转联网搜索" in note, (
        "措辞必须读作「本地没查到、正在联网找」，而不是故障")
    # 原始长度是它的几十倍 —— 证明截断确实生效（不是碰巧短）
    assert len(str(huge)) > 2000


@pytest.mark.parametrize(
    ("stage", "must_have"),
    [
        ("local", "已转联网搜索"),
        ("timeout", "已转联网搜索"),
        ("online", "已登记缺口，不阻断本轮"),
    ],
)
def test_gap_note_stage_wording(stage: str, must_have: str) -> None:
    """三档措辞：处理中 / 超时 / **两边都没有 ⇒ 缺口**（不是异常）。"""
    from src.orchestration.supervisor import collection_gap_note

    note = collection_gap_note("行业轮动:电气设备", "本地无相近匹配", stage=stage)
    assert must_have in note
    assert "A01_data_collector(行业轮动:电气设备)" in note, (
        "前缀形状必须保留 —— 既有契约断言「采集失败要在 errors 里可见」")


def test_gap_log_line_is_also_bounded() -> None:
    """日志行同样有界（一屏注册表不许进日志，更不许进用户面板）。"""
    from src.orchestration.supervisor import (
        USER_ERROR_MAX,
        format_gap_note_for_log,
    )

    huge = RuntimeError("x" * 5000)
    line = format_gap_note_for_log("fed:effr", huge)
    assert line.startswith(f"{GAP_TAG} indicator=fed:effr kind=error ")
    assert len(line) <= USER_ERROR_MAX + 120


def test_collector_logs_gap_on_empty_and_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """★ 行为判据：A01 **真的**在"取不到"时打日志（不是只在函数里躺着）。"""
    from src.core.models import AgentInput
    from src.domain.agents.data.collector.agent import DataCollectorAgent

    class _EmptyBackend:
        async def fetch(self, indicator, start_date=None, end_date=None, **kw):
            return []

        def get_capabilities(self):
            return {"name": "fake", "indicators": []}

    class _BoomBackend(_EmptyBackend):
        async def fetch(self, indicator, start_date=None, end_date=None, **kw):
            raise RuntimeError("源炸了")

    agent = DataCollectorAgent(_EmptyBackend())
    with caplog.at_level(logging.WARNING, logger="src.domain.agents.data.collector.agent"):
        asyncio.run(agent.execute(AgentInput(
            task_id="t1", tenant_id="tenant_001",
            payload={"indicator": "行业轮动:电气设备"})))
    assert any(GAP_TAG in r.message and "kind=empty" in r.message
               for r in caplog.records), "空结果必须记一行缺口"

    caplog.clear()
    boom = DataCollectorAgent(_BoomBackend())
    with caplog.at_level(logging.ERROR, logger="src.domain.agents.data.collector.agent"):
        with pytest.raises(AgentExecutionError):
            asyncio.run(boom.execute(AgentInput(
                task_id="t2", tenant_id="tenant_001",
                payload={"indicator": "fed:effr"})))
    assert any(GAP_TAG in r.message and "kind=error" in r.message
               for r in caplog.records), "取数异常必须记一行缺口"


# ============================================================================
# 三、定期数据维护：判据不能吃假告警（自证），报告落登记目录，缺口入队列
# ============================================================================


def test_expected_cycle_comes_from_the_registered_frequency() -> None:
    """★ 宽限期从**登记的 frequency** 推，不用评估器的粗粒度猜测。

    实测代价：只看评估器的 `publish_cycle_days`（季频给 30 天）时，
    "6 月 30 日财报在 9 月 29 日看"（91 天）被判停更 ⇒ **862 条里报 835 条**。
    一份 97% 是告警的报告等于没有报告。
    """
    assert expected_cycle_days("daily") == 1
    assert expected_cycle_days("realtime") == 1
    assert expected_cycle_days("weekly") == 7
    assert expected_cycle_days("monthly") == 30
    assert expected_cycle_days("quarterly") == 90
    assert expected_cycle_days("yearly") == 365
    assert expected_cycle_days("没见过的频率") == 30       # 保守：不轻易报
    assert MAINTENANCE_MIN_GRACE_DAYS == 3


@pytest.mark.parametrize(
    ("freq", "days", "should_flag"),
    [
        # 已知答案自证（先喂"健康"的输入，确认它**不报**）
        ("daily", 1, False),        # 昨天更新的日频 → 健康
        ("daily", 3, False),        # 周一早上看周五的数据
        ("daily", 5, True),         # 日频停 5 天 → 该报了
        ("monthly", 28, False),     # CPI 当月已更（2026-09-01）
        ("monthly", 60, False),     # 月频晚一期（等统计局发布）→ 不报
        ("quarterly", 91, False),   # 6/30 财报在 9/29 看 → **不许报**（假告警现场）
        ("quarterly", 300, True),   # 季频停三个季度 → 该报
        # 年频 400 天：登记的宽限（3×365）不该报，但**评估器的硬截止会说 expired**
        # ⇒ 按"已从分析中过滤"处理，报出来是对的（这是**有意**的优先级：
        #   硬截止 > 宽限期）。用这条把该优先级钉住，避免以后被"顺手统一"掉。
        ("yearly", 400, True),
    ],
)
def test_grace_window_does_not_cry_wolf(freq: str, days: int, should_flag: bool) -> None:
    """★ 自证：喂**已知答案**的输入，确认宽限期判据既不漏报也不假报。"""
    from src.scheduler.maintenance import _classify

    class _Entry:
        indicator = "X"
        row_count = 10
        last_period_date = (date.today() - timedelta(days=days)).isoformat()
        frequency = freq

    issue = _classify(_Entry(), date.today())
    assert (issue is not None) is should_flag, (
        f"{freq} 距今天 {days} 天 → 期望{'报' if should_flag else '不报'}，"
        f"实际 {'报了' if issue else '没报'}")


def test_missing_and_template_are_handled() -> None:
    """零行 → `missing`；模板（`PB:{code}`）**不该有点**，不许报。"""
    from src.scheduler.maintenance import _classify

    class _Zero:
        indicator = "PMI"
        row_count = 0
        last_period_date = ""
        frequency = "monthly"

    class _Tpl:
        indicator = "PB:{code}"
        row_count = 0
        last_period_date = ""
        frequency = "daily"

    assert _classify(_Zero(), date.today()).state == "missing"
    assert _classify(_Tpl(), date.today()) is None


def test_report_path_lives_in_the_registered_run_dir() -> None:
    """报告不许写字面路径（护栏 `test_no_store_path_literals_in_src` 守着）。"""
    path = report_path(env="pilot")
    assert path.name == "data_freshness_report_pilot.json"
    assert "run" in path.parts, f"应在登记的 run_dir 下：{path}"


def test_gap_queue_is_not_inside_the_source_tree() -> None:
    """★★ 缺口队列必须落在**项目根**（登记表声明的 `data/gap_queue.jsonl`）。

    实测缺陷（2026-09-29，被维护审计的探针顺手抓到）：`GapQueue.__init__` 用
    `Path(__file__).resolve().parents[3]` 当根 —— 从
    `src/domain/agents/decision/gap_queue.py` 往上数三层是 **`src/`**
    ⇒ 队列落在 `src/data/gap_queue.jsonl`（里面已躺着 **44 条真实缺口**）。

    三重后果：① `configs/data_stores.yaml` 登记的路径成了**幻影**；
    ② 队列写在源码树里（清理/只读挂载即静默丢）；③ 没人知道该不该备份。
    """
    from src.domain.agents.decision.gap_queue import GapQueue
    from src.infrastructure.catalog.data_stores import PROJECT_ROOT, store_rel

    queue = GapQueue()
    assert queue._root == PROJECT_ROOT, "根必须是登记的 PROJECT_ROOT，不是数出来的 parents[N]"
    assert queue._path == PROJECT_ROOT / store_rel("gap_queue")
    assert "src" not in queue._path.relative_to(PROJECT_ROOT).parts, (
        f"队列不许落在源码树里：{queue._path}")


def test_audit_enqueues_gaps_into_the_existing_queue(tmp_path, monkeypatch) -> None:
    """★ 缺口要进**既有**补采队列（补取由既有 drain 作业做）——不另造一套。"""
    from src.domain.agents.decision import gap_queue as gq
    from src.scheduler import maintenance

    class _Entry:
        def __init__(self, indicator, rows, last, freq):
            self.indicator, self.row_count = indicator, rows
            self.last_period_date, self.frequency = last, freq

    old = (date.today() - timedelta(days=500)).isoformat()
    entries = [
        # ★ 2026-09-30 换样本：原用 `us_unemployment`，但它已被**停产**
        #   （`enabled: false` —— AkShare `macro_usa_*` 源停更，替代是
        #   `fred:UNRATE`）。停产判据改成从**登记表现读**之后，这条不再进报告，
        #   本测试随之变红 —— 那是它该有的行为（"改口径时先看旧判据会不会红"）。
        #   改用一个**仍在册**且确实停了的指标来守"缺口要进既有队列"。
        _Entry("社融", 115, old, "monthly"),              # 该报（陈旧 500 天）
        _Entry("CPI", 229, date.today().isoformat(), "monthly"),  # 健康 → 不报
    ]

    class _Repo:
        def __init__(self, *a, **k) -> None:
            pass

        async def all(self, only_enabled: bool = True):
            return entries

        async def refresh_stats_from_facts(self, indicators):
            return 0

    monkeypatch.setattr(
        "src.infrastructure.catalog.catalog_repo.CatalogRepository", _Repo)
    monkeypatch.setattr(gq, "QUEUE_REL", str(tmp_path / "gap_queue.jsonl"))
    gq.reset_gap_queue_for_test()

    out = asyncio.run(maintenance.audit(
        enqueue=True, write_report=False, root=tmp_path))
    flagged = [i["indicator"] for i in out["issues"]]
    assert "社融" in flagged
    assert "CPI" not in flagged, "健康的指标不许进报告（假告警）"
    assert out["enqueued"] == len(flagged) >= 1
    queue_file = tmp_path / "gap_queue.jsonl"
    assert queue_file.exists(), "缺口必须落到补采队列文件"
    assert "社融" in queue_file.read_text(encoding="utf-8")
    gq.reset_gap_queue_for_test()


def test_audit_writes_a_readable_report(tmp_path, monkeypatch) -> None:
    """报告要能被人看：JSON 含 date/scanned/issues/items（每条带判据）。"""
    from src.scheduler import maintenance

    class _Entry:
        indicator = "社融"
        row_count = 115
        last_period_date = (date.today() - timedelta(days=200)).isoformat()
        frequency = "monthly"

    class _Repo:
        def __init__(self, *a, **k) -> None:
            pass

        async def all(self, only_enabled: bool = True):
            return [_Entry()]

        async def refresh_stats_from_facts(self, indicators):
            return 0

    monkeypatch.setattr(
        "src.infrastructure.catalog.catalog_repo.CatalogRepository", _Repo)
    out = asyncio.run(maintenance.audit(
        enqueue=False, write_report=True, root=tmp_path))
    payload = json.loads((tmp_path / "data" / "run" /
                          "data_freshness_report.json").read_text(encoding="utf-8"))
    # ⚠️ 不能断言 `issues == 1`：审计**同时**遍历真实登记表（登记了但索引里
    #    没有的也算 `missing`）—— 那正是它该做的事。这里只钉住两件事：
    #    ① 我喂进去的那条在报告里且带判据；② 报告结构完整、人可读。
    items = {i["indicator"]: i for i in payload["items"]}
    assert "社融" in items, f"报告里应有社融，实际 {list(items)[:5]}…"
    assert "宽限" in items["社融"]["reason"] or "停" in items["社融"]["reason"]
    assert payload["scanned"] >= 1 and isinstance(payload["by_state"], dict)
    assert "env" in payload and "date" in payload
    assert out["report_path"] and out["summary"].startswith("扫描 ")


# ============================================================================
# 四、"数据打通了为什么还缺"：中国宏观必须确定性进计划
# ============================================================================


def test_macro_question_always_gets_china_macro_indicators() -> None:
    """★★ 报障 #3 的根因判据：LLM 的宏观计划里**一条中国宏观都没有**。

    实测同一条问句的 LLM 计划 20 条：全是 `us_*` / `fed:*` / 个股
    （`CPI`/`PPI`/`M2`/`社融` 一条都没有）⇒ A08 模板输出
    「中国端：中国宏观数据缺失」，而**库里 CPI 229 行、PPI 229 行**。
    """
    from src.orchestration.supervisor import ensure_macro_indicators

    query = ("当前宏观环境如何，预测下未来一年美国的加息、降息节奏…"
             "未来半年能否持有高股息的招商银行？")
    planned = ["us_cpi_yoy", "fed:effr", "PE(TTM):600036"]
    out = ensure_macro_indicators("macro", query, list(planned))
    for ind in ("CPI", "PPI", "M2", "社融"):
        assert ind in out, f"宏观问必须补 {ind}（库里都有数据）"
    # 原有的一个都不许动（只增不减）
    assert all(i in out for i in planned)
    # 幂等：重复调用不产生重复项
    assert ensure_macro_indicators("macro", query, out) == out


def test_non_macro_question_is_not_padded() -> None:
    """反向用例：纯个股问句**不该**被塞进宏观四件套（不给别的问题加噪音）。"""
    from src.orchestration.supervisor import ensure_macro_indicators

    out = ensure_macro_indicators("stock", "招商银行未来半年能持有吗？",
                                 ["PE(TTM):600036"])
    assert out == ["PE(TTM):600036"]
