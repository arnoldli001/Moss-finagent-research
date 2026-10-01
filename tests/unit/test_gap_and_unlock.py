"""第十四轮三项能力测试：缺口队列 / A11 解禁信号 / GLM 并发上限。

## 守的三件事（用户 2026-09-28 提出）

1. **A17 发现缺口 → A19 补取**（跨轮次）
2. **教 A11 的 prompt 怎么用解禁数据**
3. **GLM-4.7-flash 免费档上限实测**（结论写进代码）

每一项都有对应的**反向测试**（构造坏输入必须失败）——
防止"假绿"（本会话最贵的教训）。
"""

from __future__ import annotations

import pytest

from src.domain.agents.decision.gap_queue import (
    DEDUP_TTL_S,
    MAX_ATTEMPTS,
    NO_ENQUEUE_STATUS,
    GapQueue,
    reset_gap_queue_for_test,
)


@pytest.fixture
def queue(tmp_path, monkeypatch):
    """隔离的缺口队列（写 tmp_path，不碰生产 data/）。"""
    reset_gap_queue_for_test()
    q = GapQueue(root=tmp_path)
    yield q
    reset_gap_queue_for_test()


# ============================================================
# ① 缺口队列（跨轮次补取）
# ============================================================


def test_enqueue_and_pending(queue):
    """入队后应出现在 pending 里。"""
    assert queue.enqueue("ind:test_a", reason="A17 报缺口") is True
    items = queue.pending()
    assert len(items) == 1
    assert items[0].indicator == "ind:test_a"
    assert items[0].status == "pending"


def test_enqueue_dedup_within_ttl(queue):
    """★ 同一缺口在 DEDUP_TTL 内不重复入队（否则队列被淹没）。"""
    assert queue.enqueue("ind:dup", reason="r") is True
    assert queue.enqueue("ind:dup", reason="r") is False, "去重失效"
    assert len(queue.pending()) == 1


def test_enqueue_rejects_terminated_source(queue):
    """★ 反向测试：`source_terminated` / `unavailable` 必须**不入队**。

    理由：源已停披（如北向资金 2024-08 起停披）或环境不可达（CME），
    补也补不到 —— 入队只会浪费 A19 的 LLM 调用。
    """
    for status in NO_ENQUEUE_STATUS:
        assert queue.enqueue("x", reason="r", status=status) is False, (
            f"status={status} 不该入队")
    assert queue.pending() == []


def test_enqueue_rejects_empty_indicator(queue):
    """空指标名不入队。"""
    assert queue.enqueue("") is False
    assert queue.enqueue("   ") is False
    assert queue.pending() == []


def test_resolved_not_reenqueued(queue):
    """已解决的缺口不再入队（除非数据又被清了）。"""
    queue.enqueue("ind:done", reason="r")
    e = queue.pending()[0]
    queue.mark(e, "resolved", result="ok")
    assert queue.enqueue("ind:done", reason="r") is False


def test_failed_retries_then_skipped(queue):
    """★ 失败达 MAX_ATTEMPTS 后转 skipped（已知不可达的不该每次重试）。"""
    queue.enqueue("ind:bad", reason="r")
    e = queue.pending()[0]
    for _ in range(MAX_ATTEMPTS):
        # 每次失败后要能重新进 pending（模拟 TTL 已过）
        e.last_try_ms = 0
        queue.mark(e, "failed", result="err")
    assert queue.stats()["skipped"] == 1, "达上限后应转 skipped"
    assert queue.pending() == [], "skipped 不该再出现在 pending"


def test_persistence_across_instances(tmp_path):
    """★ 落盘：换一个实例仍能读到（进程内存队列重启即失效）。"""
    reset_gap_queue_for_test()
    q1 = GapQueue(root=tmp_path)
    q1.enqueue("ind:persist", reason="r")
    q2 = GapQueue(root=tmp_path)      # 新实例，同一目录
    assert len(q2.pending()) == 1
    assert q2.pending()[0].indicator == "ind:persist"


def test_stats_shape(queue):
    """stats() 给出各状态计数（可观测）。"""
    queue.enqueue("a", reason="r")
    queue.enqueue("b", reason="r")
    st = queue.stats()
    assert st["pending"] == 2
    assert st["total"] == 2
    for k in ("resolved", "failed", "skipped", "resolving"):
        assert k in st


def test_dedup_ttl_positive():
    """DEDUP_TTL 必须是正数（0 会导致每次都入队）。"""
    assert DEDUP_TTL_S > 0


# ============================================================
# ② A11 解禁信号
# ============================================================


def _unlock_points(market_cap=None, count=None, top_cap=None):
    pts = []
    if market_cap is not None:
        pts.append({"indicator": "cal:unlock:market_cap",
                    "value": market_cap, "period_date": "2026-10-28",
                    "extra": {"top_stocks": [
                        {"name": "西安奕材", "market_cap": 84850495969.77,
                         "pct_of_float": 1054.99}]}})
    if count is not None:
        pts.append({"indicator": "cal:unlock:company_count",
                    "value": count, "period_date": "2026-10-28"})
    if top_cap is not None:
        pts.append({"indicator": "cal:unlock:top_stock_cap",
                    "value": top_cap, "period_date": "2026-10-28"})
    return pts


def test_a11_flags_large_unlock():
    """★ 大额解禁必须触发旗标（2026-10-28 西安奕材 848 亿是真实案例）。"""
    from src.domain.agents.analysis.base import AnalysisPayload
    from src.domain.agents.analysis.risk.agent import _unlock_flags

    p = AnalysisPayload(data_points=_unlock_points(
        market_cap=108078427192.0, count=9, top_cap=84850495969.77))
    flags = _unlock_flags(p)
    assert len(flags) >= 2, f"大额解禁应触发 ≥2 个旗标，实际 {flags}"
    joined = " ".join(flags)
    assert "1081" in joined or "1080" in joined, "应报单日合计解禁规模"
    assert "849" in joined or "848" in joined, "应报最大单只解禁规模"


def test_a11_no_flags_for_small_unlock():
    """★ 反向测试：小额解禁**不该**触发旗标（防误报）。"""
    from src.domain.agents.analysis.base import AnalysisPayload
    from src.domain.agents.analysis.risk.agent import _unlock_flags

    p = AnalysisPayload(data_points=_unlock_points(
        market_cap=5e8, count=3, top_cap=2e8))
    assert _unlock_flags(p) == [], "小额解禁不该报旗标"


def test_a11_summary_marks_no_data():
    """★ 反向测试：无解禁数据时 `has_data=False`（供 prompt 声明缺口）。

    AGENTS.md：不得用训练记忆里的数字填补缺口。
    """
    from src.domain.agents.analysis.base import AnalysisPayload
    from src.domain.agents.analysis.risk.agent import (
        _unlock_flags,
        _unlock_summary,
    )

    p = AnalysisPayload(data_points=[{"indicator": "PE", "value": 20}])
    assert _unlock_summary(p)["has_data"] is False
    assert _unlock_flags(p) == []


def test_a11_prompt_teaches_unlock():
    """★ prompt 里必须**教**怎么用解禁数据（不只放行数据）。

    这是 `requirement-closure-and-impact` skill 的贯通点第 ⑩ 层：
    「Agent system_prompt 有没有教它怎么用这个指标」。
    漏了它的症状：数据在上下文里但模型不知道那是风险信号。
    """
    from src.domain.agents.analysis.risk.agent import RiskAnalysisAgent

    sp = RiskAnalysisAgent.system_prompt
    assert "cal:unlock" in sp, "system_prompt 未提及解禁指标名"
    assert "解禁" in sp
    # 必须教它"解禁≠必跌"（防止模型直接断言涨跌）
    assert "必跌" in sp or "潜在减持压力" in sp


def test_a11_requirements_require_unlock_field():
    """`_requirements` 必须把 `unlock_assessment` 列为输出字段。"""
    from src.domain.agents.analysis.base import AnalysisPayload
    from src.domain.agents.analysis.risk.agent import RiskAnalysisAgent

    a = RiskAnalysisAgent.__new__(RiskAnalysisAgent)
    a.agent_id = "A11_fin_risk"
    # 有数据
    p1 = AnalysisPayload(data_points=_unlock_points(
        market_cap=1e11, count=9))
    p1.hint["unlock_summary"] = {"has_data": True}
    assert "unlock_assessment" in a._requirements(p1)
    # 无数据
    p2 = AnalysisPayload(data_points=[])
    p2.hint["unlock_summary"] = {"has_data": False}
    req2 = a._requirements(p2)
    assert "unlock_assessment" in req2
    assert "未获取到解禁数据" in req2, "无数据时应要求如实声明缺口"


def test_a11_thresholds_are_in_code():
    """阈值必须**写进代码**而非注释（AGENTS.md 硬约束）。"""
    from src.domain.agents.analysis import risk

    for name in ("UNLOCK_CAP_MARKET_WARN", "UNLOCK_CAP_SINGLE_WARN",
                 "UNLOCK_COUNT_WARN"):
        assert hasattr(risk.agent, name), f"{name} 未定义为常量"
        assert getattr(risk.agent, name) > 0


# ============================================================
# ③ GLM 并发上限（结论写进配置）
# ============================================================


def test_glm_concurrency_cap_in_config():
    """★ GLM 的实测并发上限必须写进 `models.yaml`（不能只留文档）。

    实测（scripts/probe_glm_concurrency.py，2026-09-28）：
        并发 1 → 429 率 17%
        并发 2 → 429 率 100%

    ══════════════════════════════════════════════════════════════════
    ⚠️ 2026-09-28 第十七轮**语义变更**（用户裁定，不是放宽判据）

    原判据：`glm-4.7-flash` **不得出现在任何 routing 链里**。
    新判据：**不得出现为主模型，也不得占据第一备源位**；
            允许作为**非首位备源**（多个云端厂商互为备份）。

    用户原话：「跨厂商 不应该是两个云端模型吗 —— deepseek-flash、glm，
    本地 ollama 最后备用？」

    变更理由：原判据把"不可靠"等同于"不能用"，于是 deepseek 熔断时
    没有任何**云端**备源 —— 第一备源位直接落到会挂死的本地。
    新语义把 glm 放在**第二跳**：常态不花钱、不承载主流量，
    只在 deepseek 故障时接一次；它的 17% 429 由第三跳（本地）兜住。

    废止痕迹（AGENTS.md 要求变更史可查）：
        ~~glm 不得进入任何 routing 链~~
        → glm 不得为主模型、不得为第一备源（第十七轮）
    ══════════════════════════════════════════════════════════════════
    """
    from pathlib import Path

    import yaml

    cfg = Path(__file__).resolve().parents[2] / "configs" / "models.yaml"
    raw = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    glm = (raw.get("models") or {}).get("glm-4.7-flash")
    assert glm is not None, "glm-4.7-flash 未登记"
    assert glm.get("max_concurrency") == 1, (
        "并发上限必须写进配置（实测并发 2 为 100% 失败）"
    )

    def _chain(item: dict) -> list[str]:
        out = [item.get("primary")]
        fb = item.get("fallbacks")
        if isinstance(fb, (list, tuple)):
            out.extend(fb)
        elif item.get("fallback"):
            out.append(item["fallback"])
        return [x for x in out if x]

    #: 允许 glm 作**主模型**的层 —— 必须是**低频**层，且由用户显式裁定。
    #: 2026-09-28 用户原话：「规划层 优先glm-4.7-flash，其次备选deepseek-flash」
    #: 依据：规划 1 次/请求，暴露面小；glm p50 1106ms 且免费。
    #: 高频层（light/medium 挂清洗与信息层）**不允许** —— 17% 429 会按调用量放大。
    _GLM_PRIMARY_OK = {"planning"}

    for tier, item in (raw.get("routing") or {}).items():
        chain = _chain(item)
        if chain[0] == "glm-4.7-flash":
            assert tier in _GLM_PRIMARY_OK, (
                f"{tier} 层把 glm-4.7-flash 当**主模型** —— 实测 17% 429，"
                f"高频层不能承载主流量（允许的层：{sorted(_GLM_PRIMARY_OK)}）")
        if "glm-4.7-flash" in chain:
            # 无论主备，**不许在链尾**：17% 429 的模型后面必须有兜底。
            assert chain.index("glm-4.7-flash") < len(chain) - 1, (
                f"{tier} 层把 glm-4.7-flash 放在**链尾**（链={chain}）"
                "—— 它 17% 429，后面必须有兜底")
        # 无论 glm 在不在，链上都必须有本地兜底（云端全挂时不整链失败）
        assert any("local" in m for m in chain), (
            f"{tier} 层链上没有本地兜底（链={chain}）—— 云端全挂即整链失败")


def test_gap_drain_job_registered():
    """缺口补取作业必须注册（且盘后跑）。"""
    from src.scheduler.catalog_jobs import (
        GAP_DRAIN_JOB_CRON,
        GAP_DRAIN_JOB_NAME,
        install_catalog_jobs,
    )
    from src.scheduler.registry import JOB_REGISTRY

    snapshot = dict(JOB_REGISTRY)
    try:
        install_catalog_jobs()
        assert GAP_DRAIN_JOB_NAME in JOB_REGISTRY
        assert JOB_REGISTRY[GAP_DRAIN_JOB_NAME].kind == "gap_drain"
        hour = int(GAP_DRAIN_JOB_CRON.split()[1])
        assert hour >= 16, f"缺口补取定在 {hour} 点，早于收盘"
    finally:
        JOB_REGISTRY.clear()
        JOB_REGISTRY.update(snapshot)


def test_gap_drain_has_executor_branch():
    """`gap_drain` 必须在 jobs.py 有分发分支（本项目踩过"声明了没分支"）。"""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[2]
           / "src" / "scheduler" / "jobs.py").read_text(encoding="utf-8")
    assert 'spec.kind == "gap_drain"' in src
    assert "_drain_gap_queue" in src


def test_enqueue_after_a17_gaps_wired():
    """A17 的缺口入队必须接在 recommend_node 里（否则队列永远是空的）。"""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[2]
           / "src" / "orchestration" / "supervisor.py").read_text(encoding="utf-8")
    assert "_enqueue_data_gaps" in src, "recommend_node 未接入缺口入队"
    assert "data_gaps" in src
