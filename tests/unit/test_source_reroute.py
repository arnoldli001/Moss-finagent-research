"""★ 换源（reroute）护栏：联网兜底也找不到时，自动找替代源并**更新数据源地址**。

## 触发它的用户口径（2026-09-30，两条连着的）

> ① 「源停更 → 换源，这个可以在**每次联网找不到数据时**，做个**自动搜索其他网址**，
>    寻找数据源，**找到后就更新数据源地址**」
> ② 「**也可以联网查询获取两个数据源，作为备用**」

## 本文件守的六件事

1. **能力边界照实登记**：仓库**没有**搜索引擎能力（`web_search|serp|bing|duckduckgo`
   在 `src/` 零命中）⇒ 本模块做的是"在**已有连接器**里找替代"（免费、确定性、可离线测），
   LLM/搜索产出的候选走**同一套**校验与落盘；
2. **口径一致性是硬闸**：新源必须与旧序列在重叠期/频率/量级上过得去 ——
   本项目教训是**错口径比缺数据更危险**（"指数当同比"）；
3. **影子期不许偷偷变成"直接切换"**：重叠不足只能 `shadow`，
   而 `preferred_order()` 对 shadow **返回空**（取数顺序不变）；
4. **多源备用**（用户第②条）：一次换源要同时定下**主源 + 最多 2 个备源**，
   路由按"主源 → 备源 → 其余"取数；备源**只往后排、不删除**；
5. **绝不自动改 `indicators.yaml`**（登记表要人工复核 + 改口径要留废止痕迹）：
   只落覆盖层 + 给一行可并入的 `yaml_hint`；
6. **没有候选就什么都不写**（不许留下"看起来换过源"的痕迹）。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

import pytest

from src.infrastructure.catalog import source_reroute as sr


@dataclass
class _P:
    """最小 DataPoint 替身（只用到 `period_date` / `value`）。"""

    period_date: str
    value: float


def _series(periods_values: list[tuple[str, float]]) -> list[_P]:
    return [_P(p, v) for p, v in periods_values]


MONTHLY = _series([(f"2026-{m:02d}-01", 3.0 + m / 10) for m in range(1, 10)])
MONTHLY_SAME = _series([(f"2026-{m:02d}-01", 3.0 + m / 10) for m in range(1, 10)])
INDEX_LEVEL = _series([(f"2026-{m:02d}-01", 337.0 + m) for m in range(1, 10)])
DAILY = _series([(f"2026-09-{d:02d}", 3.0) for d in range(1, 21)])


class FakeConn:
    """最小连接器替身（**类名即 `source_key`** —— 与生产同口径）。"""

    #: 路由用它做失败冷却的键（生产连接器都有这个类属性）
    source_name = "fake"

    def __init__(self, points: list[_P], *, supports: bool = True,
                 boom: bool = False, delay: float = 0.0) -> None:
        self.points, self._supports = points, supports
        self._boom, self._delay = boom, delay
        self.source_name = type(self).__name__

    def supports(self, _indicator: str) -> bool:
        return self._supports

    def get_capabilities(self) -> dict:
        """路由需要它（无支持源时要拼"已注册"提示）。"""
        return {"indicators": [], "source_name": type(self).__name__}

    async def fetch(self, _indicator: str):
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._boom:
            raise RuntimeError("源挂了")
        return self.points


def mk(name: str, points: list[_P], *, supports: bool = True,
       boom: bool = False, delay: float = 0.0):
    """造一个**类名可控**的替身（`type(inst).__name__` 就是 `source_key`）。"""
    cls = type(name, (FakeConn,), {})
    return cls(points, supports=supports, boom=boom, delay=delay)


class _Router:
    """与生产同构：`_routes` 是 `(connector, supports)` 对。"""

    def __init__(self, conns: list[object]) -> None:
        self._routes = [(c, c.supports) for c in conns]  # type: ignore[attr-defined]


def _router(conns: list[object]) -> _Router:
    return _Router(conns)


# ============================================================
# ① 口径一致性：这是"错口径比缺数据更危险"的机器判据
# ============================================================


def test_same_caliber_passes():
    v = sr.check_caliber(MONTHLY, MONTHLY_SAME)
    assert v.ok and "一致" in v.reason
    assert v.checks["overlap_periods"] == 9


def test_index_level_vs_yoy_is_rejected():
    """★ 核心判据：**指数当同比**必须被拒。

    实测现场：`fred:CPILFESL`（指数 337.8）vs `us_core_cpi`（同比 ~3）
    —— 比值 ≈ 110 ≫ 10。数字看着有据、语义是错的，所以宁可留着缺口。
    """
    assert sr.check_caliber(MONTHLY, INDEX_LEVEL).ok is False


def test_daily_vs_monthly_is_rejected():
    """频率差 30 倍 ⇒ 拒（日频序列替不了月频序列）。"""
    assert sr.check_caliber(MONTHLY, DAILY).ok is False


def test_new_source_must_not_be_older():
    older = _series([("2025-12-01", 3.1), ("2026-01-01", 3.2)])
    v = sr.check_caliber(MONTHLY, older)
    assert v.ok is False and "早于旧源" in v.reason


def test_empty_candidate_is_not_a_reroute():
    assert sr.check_caliber(MONTHLY, []).ok is False


def test_all_none_values_is_not_a_reroute():
    """**空结果 ≠ 换源依据**：值全空（NaN 行被跳过的形态）不许当成候选。"""
    assert sr.check_caliber(MONTHLY, [_P("2026-09-01", None)]).ok is False  # type: ignore[arg-type]


def test_thin_overlap_only_gets_shadow():
    """★ 重叠不足 ⇒ 只放行到影子期（弱判据不许直接改取数顺序）。"""
    old = _series([("2026-07-01", 3.0), ("2026-08-01", 3.1), ("2026-09-01", 3.2)])
    new = _series([("2026-09-01", 3.2), ("2026-10-01", 3.3)])   # 重叠仅 1 期
    v = sr.check_caliber(old, new)
    assert v.ok is True and "只能进影子期" in v.reason


# ============================================================
# ② 覆盖层：找到才写；shadow 不改顺序；**主源 + 两个备源**
# ============================================================


def test_promoted_source_is_written_and_read(tmp_path: Path):
    v = sr.check_caliber(MONTHLY, MONTHLY_SAME)
    sr.record_decision("CPI", v, source_key="BaostockConnector",
                       origin="connector", status="promoted", root=tmp_path)
    assert sr.preferred_source("CPI", root=tmp_path) == "BaostockConnector"
    item = sr.load_overrides(tmp_path)["CPI"]
    assert item["status"] == "promoted"
    assert "primary_source" in item["yaml_hint"], "必须给出可直接并入 YAML 的一行"
    assert (tmp_path / "data" / "run" / sr.OVERRIDE_NAME).exists()
    assert (tmp_path / "data" / "run" / sr.DECISION_LOG_NAME).exists()


def test_primary_plus_two_backups_are_kept_in_order(tmp_path: Path):
    """★ 用户第②条：**主源 + 两个备源**，顺序即取数顺序。"""
    v = sr.check_caliber(MONTHLY, MONTHLY_SAME)
    sr.record_decision("CPI", v, source_key="PrimaryConn", status="promoted",
                       backups=["BackupA", "BackupB"], root=tmp_path)
    assert sr.preferred_order("CPI", root=tmp_path) == [
        "PrimaryConn", "BackupA", "BackupB"]
    assert sr.load_overrides(tmp_path)["CPI"]["sources"] == [
        "PrimaryConn", "BackupA", "BackupB"]
    assert "backup" in sr.load_overrides(tmp_path)["CPI"]["yaml_hint"]


def test_backup_count_is_capped(tmp_path: Path):
    """备源**有上限**（默认 2）：不许无界堆积（每个源都是一次真取数）。"""
    v = sr.check_caliber(MONTHLY, MONTHLY_SAME)
    sr.record_decision("CPI", v, source_key="P", status="promoted",
                       backups=["B1", "B2", "B3", "B4"], root=tmp_path)
    assert sr.preferred_order("CPI", root=tmp_path) == ["P", "B1", "B2"]


def test_shadow_does_not_change_fetch_order(tmp_path: Path):
    """★ 影子期**不许**影响取数顺序（纪律 3 的机器判据）。"""
    v = sr.check_caliber(MONTHLY, MONTHLY_SAME)
    sr.record_decision("CPI", v, source_key="BaostockConnector",
                       status="shadow", root=tmp_path)
    assert sr.preferred_order("CPI", root=tmp_path) == [], \
        "shadow 状态不得改取数顺序（否则影子期 = 直接切换）"


def test_rejected_candidate_writes_nothing(tmp_path: Path):
    """★ 口径不过 ⇒ **只写审计、不写覆盖层**（不许留下换过源的痕迹）。"""
    v = sr.check_caliber(MONTHLY, INDEX_LEVEL)
    sr.record_decision("CPI", v, source_key="XConnector", root=tmp_path)
    assert sr.load_overrides(tmp_path) == {}
    assert not (tmp_path / "data" / "run" / sr.OVERRIDE_NAME).exists()
    log = (tmp_path / "data" / "run" / sr.DECISION_LOG_NAME).read_text("utf-8")
    assert "XConnector" in log, "拒绝的理由必须留痕（供人工判断）"


def test_legacy_single_source_format_still_reads(tmp_path: Path):
    """老格式（只有 `source_key`）必须仍然读得出来（不制造静默失效）。"""
    import yaml

    p = tmp_path / "data" / "run" / sr.OVERRIDE_NAME
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump({"version": 1, "overrides": {
        "CPI": {"source_key": "OldConn", "status": "promoted"}}}), encoding="utf-8")
    assert sr.preferred_order("CPI", root=tmp_path) == ["OldConn"]


def test_no_preference_means_original_order(tmp_path: Path):
    assert sr.preferred_source("从未换过源的指标", root=tmp_path) == ""


# ============================================================
# ③ 候选探测：免费路径 A + 上限 + 失败留痕
# ============================================================


@pytest.mark.asyncio
async def test_probe_skips_non_supporting_and_records_errors():
    good = mk("GoodConnector", MONTHLY_SAME)
    bad = mk("BoomConnector", [], boom=True)
    mute = mk("MuteConnector", MONTHLY_SAME, supports=False)
    out = await sr.probe_connectors(_router([mute, bad, good]), "CPI")
    keys = [c.source_key for c in out]
    assert "MuteConnector" not in keys, "不认这个指标的连接器不该被探"
    assert "BoomConnector" in keys and out[0].error, "失败也要留痕"
    assert any(c.points for c in out), "成功的候选必须带点"


@pytest.mark.asyncio
async def test_probe_respects_the_cap():
    conns = [mk(f"C{i}", MONTHLY_SAME) for i in range(6)]
    out = await sr.probe_connectors(_router(conns), "CPI", limit=2)
    assert len(out) <= 2, "单次探测必须有上限（每次都是真取一次）"


@pytest.mark.asyncio
async def test_probe_timeout_is_skipped_not_fatal():
    slow = mk("SlowConnector", MONTHLY_SAME, delay=1.0)
    fast = mk("FastConnector", MONTHLY_SAME)
    out = await sr.probe_connectors(_router([slow, fast]), "CPI", timeout=0.05)
    assert [c.source_key for c in out] == ["SlowConnector", "FastConnector"]
    assert out[0].error, "超时要记成该候选的失败（不是抛出去）"
    assert out[1].points, "慢源超时不许影响后面的候选"


# ============================================================
# ④ 端到端（离线）：探测 → 校验 → 落盘 → 主源+备源
# ============================================================


@pytest.mark.asyncio
async def test_reroute_promotes_primary_plus_backups(tmp_path: Path):
    a = mk("AlterA", MONTHLY_SAME)
    b = mk("AlterB", MONTHLY_SAME)
    pts, why = await sr.reroute("CPI", _router([a, b]), MONTHLY, root=tmp_path)
    assert pts and "成功" in why and "备源" in why
    assert sr.preferred_order("CPI", root=tmp_path) == ["AlterA", "AlterB"]


@pytest.mark.asyncio
async def test_reroute_refuses_wrong_caliber(tmp_path: Path):
    """★ 唯一候选源口径不对 ⇒ 宁可**留着缺口**，也不换。"""
    wrong = mk("LevelConnector", INDEX_LEVEL)
    pts, why = await sr.reroute("CPI", _router([wrong]), MONTHLY, root=tmp_path)
    assert pts == [] and "口径校验未通过" in why
    assert sr.preferred_order("CPI", root=tmp_path) == []


@pytest.mark.asyncio
async def test_reroute_mixes_good_and_bad_candidates(tmp_path: Path):
    """坏的被拒、好的照收 —— 一个坏候选不许把整次换源判死。"""
    wrong = mk("LevelConnector", INDEX_LEVEL)
    right = mk("RightConnector", MONTHLY_SAME)
    pts, why = await sr.reroute("CPI", _router([wrong, right]), MONTHLY,
                                root=tmp_path)
    assert pts and "RightConnector" in why
    assert sr.preferred_order("CPI", root=tmp_path) == ["RightConnector"]


@pytest.mark.asyncio
async def test_reroute_without_candidates_writes_no_override(tmp_path: Path):
    pts, why = await sr.reroute("CPI", _router([]), MONTHLY, root=tmp_path)
    assert pts == [] and "没有任何连接器认这个指标" in why
    assert sr.load_overrides(tmp_path) == {}


@pytest.mark.asyncio
async def test_reroute_can_be_disabled_for_promotion(tmp_path: Path):
    """`allow_promote=False`（如交互路径怕拖慢）⇒ 只进影子期，不改顺序。"""
    good = mk("AlterConnector", MONTHLY_SAME)
    pts, why = await sr.reroute("CPI", _router([good]), MONTHLY, root=tmp_path,
                                allow_promote=False)
    assert pts and "影子期" in why
    assert sr.preferred_order("CPI", root=tmp_path) == []


# ============================================================
# ⑤ 路由侧：偏好只**重排**，不增删（走真实 `_fetch_uncached`）
# ============================================================


@pytest.mark.asyncio
async def test_router_puts_preferred_first_without_dropping_others(monkeypatch):
    """★ ① 主源排第一；② 其它源一个都不能掉。

    走**真实** `_fetch_uncached`（替身的 `fetch` 不联网）——
    "改了判据"与"它生效了"是两件事，这里量的是后者。
    """
    from src.infrastructure.catalog import source_reroute as live
    from src.infrastructure.connectors.router import ConnectorRouter

    calls: list[str] = []

    def rec(name: str):
        inst = mk(name, MONTHLY)

        async def _f(_indicator, _start=None, _end=None, _n=name):
            calls.append(_n)
            return MONTHLY

        inst.fetch = _f  # type: ignore[assignment]
        return inst

    a, b, c = rec("AConn"), rec("BConn"), rec("CConn")
    router = ConnectorRouter([(a, a.supports), (b, b.supports), (c, c.supports)])
    assert [type(x).__name__ for x in router._matched("CPI")] == [
        "AConn", "BConn", "CConn"], "前置：注册顺序即取数顺序"

    await router._fetch_uncached("CPI", None, None)
    assert calls and calls[0] == "AConn", f"基准轮应走注册顺序：{calls}"
    calls.clear()

    monkeypatch.setattr(live, "preferred_source", lambda *_a, **_k: "CConn")
    pts = await router._fetch_uncached("CPI", None, None)

    assert pts, "重排不许把数据弄丢"
    assert calls and calls[0] == "CConn", f"主源应排到第一：{calls}"
    assert sorted(type(x).__name__ for x in router._matched("CPI")) == [
        "AConn", "BConn", "CConn"], "匹配集合不许被覆盖层缩减"


def test_module_documents_the_capability_boundary():
    """★ 诚实边界**必须写在代码里** —— 而且是**当前**的边界。

    本判据 2026-09-30（`CHG-0113`）**被更新过一次**，原因如实记：
    它原来断言"仓库没有任何搜索引擎能力"，而那条在用户提供博查凭据之后
    **不再成立**。留着旧断言会让"能力面与可用面不一致"（R3）：
    文档说没有、实际有 ⇒ 下一个人不会去用它。
    更新后的边界是**更强的**要求：有了搜索也**不许夸大** ——
    它只能给"网址线索"，**不能**直接变成可用源。
    """
    import inspect

    doc = inspect.getdoc(sr) or ""
    #: 旧口径必须留下废止痕迹（本项目纪律：删掉旧口径等于"从没提过"）
    assert "没有任何搜索引擎能力" in doc, "旧口径的废止痕迹被删掉了"
    assert "~~" in doc, "旧口径必须写成删除线形式留痕，而不是直接消失"
    #: 新口径：C 有了，但要带**入口函数名**与**能力边界**
    assert "discover_candidate_urls" in doc, "文档没提 path C 的入口函数"
    assert "搜索引擎只能给出**网址**" in doc, (
        "必须写明 C 只能给线索、不能产点 —— 否则会被当成'已经能自动换源'")
    assert "B 用 LLM 提议新 URL" in doc and "C 真·搜索引擎" in doc
