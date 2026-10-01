"""path C（联网搜候选源）的**离线**护栏。

## 这份测试守的是什么

path C 是**唯一花钱**的换源路径（免费包 1000 次**总量**）。所以判据重心在：

1. **搜索挂掉不能拖垮换源**（外部依赖不可靠，且换源本身就是补救路径）；
2. **"没搜到"与"额度闸门关了"必须能分开**（`AGENTS.md`：没量到 ≠ 量到 0）——
   否则运营者会以为"网上真没有这个数据源"，而实际是闸门关了；
3. **线索 ≠ 可用源**：写进审计的必须显式 `actionable=False`，
   否则下一个人会把一个网址当成"已经能用的源"；
4. **`allow_search=False` 时一次都不花**（交互路径/巡检要能真的关掉它）。

全部用**假搜索**，不联网、不花钱。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.infrastructure.catalog import source_reroute as sr
from src.infrastructure.search import bocha


class _Outcome:
    """假 SearchOutcome（只要它暴露那几个字段就够了）。"""

    def __init__(self, ok: bool, hits=None, blocked_by: str = "",
                 spent: bool = False, from_cache: bool = False) -> None:
        self.ok = ok
        self.hits = hits or []
        self.blocked_by = blocked_by
        self.spent = spent
        self.from_cache = from_cache


class _Hit:
    def __init__(self, url: str, title: str = "某数据源") -> None:
        self._d = {"title": title, "url": url, "snippet": "", "site": "example.com"}

    def to_dict(self) -> dict[str, str]:
        return dict(self._d)


def _read_leads(tmp_path: Path) -> list[dict]:
    p = sr._log_path(tmp_path)
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        if rec.get("kind") == "search_candidates":
            out.append(rec)
    return out


@pytest.mark.asyncio
async def test_successful_search_records_leads_as_not_actionable(tmp_path: Path):
    async def fake(indicator, *, context="", limit=5, search=None, root=None):
        pass  # 占位，避免误用

    def search(q, count=5):  # noqa: ANN001
        return _Outcome(True, [_Hit("https://data.stats.gov.cn/a")],
                        spent=True)

    hits, note = await sr.discover_candidate_urls(
        "社融", search=search, root=tmp_path)
    assert [h["url"] for h in hits] == ["https://data.stats.gov.cn/a"]
    assert "spent=True" in note and "blocked_by=-" in note

    leads = _read_leads(tmp_path)
    assert len(leads) == 1
    assert leads[0]["indicator"] == "社融"
    assert leads[0]["actionable"] is False, (
        "线索被标成可执行了 —— 下一个人会把一个网址当成能用的源")
    assert "连接器" in leads[0]["next_step"]


@pytest.mark.asyncio
async def test_budget_blocked_is_not_reported_as_not_found(tmp_path: Path):
    """★ 闸门关了不许说成"网上没有" —— 两者处置完全不同。"""

    def search(q, count=5):  # noqa: ANN001
        return _Outcome(False, [], blocked_by="budget", spent=False)

    hits, note = await sr.discover_candidate_urls("社融", search=search,
                                                  root=tmp_path)
    assert hits == []
    assert "blocked_by=budget" in note, f"闸门状态必须随线索一起留痕：{note}"
    assert "spent=False" in note


@pytest.mark.asyncio
async def test_search_exception_never_breaks_reroute(tmp_path: Path):
    def boom(q, count=5):  # noqa: ANN001
        raise RuntimeError("搜索服务炸了")

    hits, note = await sr.discover_candidate_urls("社融", search=boom,
                                                  root=tmp_path)
    assert hits == []
    assert "搜索调用异常" in note
    #: 异常也要留痕（否则"没搜过"与"搜了但炸了"在审计里长得一样）
    assert len(_read_leads(tmp_path)) == 1


@pytest.mark.asyncio
async def test_reroute_without_candidates_records_a_lead(tmp_path: Path):
    """A 路径全挂 ⇒ 自动去搜线索（这就是 path C 的**接线点**）。"""
    class _Router:
        @staticmethod
        def supports(_ind):  # noqa: ANN001
            return False

    calls: list[str] = []

    def search(q, count=5):  # noqa: ANN001
        calls.append(q)
        return _Outcome(True, [_Hit("https://x.example/data")], spent=True)

    #: ★★ 用**依赖注入**（`search=`），不 monkeypatch。
    #: 本文件第一版是 `search_pkg.web_search = fake`，而 `discover_candidate_urls`
    #: 读的是 `bocha.web_search`（模块属性）—— 替身**没生效**，于是它走了**真实网络**，
    #: 实测把免费额度从 1 花到 2。**单测绝不允许有能力花真钱。**
    pts, why = await sr.reroute("CPI", _Router(), [], root=tmp_path,
                                search=search)

    assert pts == [] and "没有任何连接器认这个指标" in why
    assert "候选源线索" in why, f"应把搜到的线索数写进说明：{why}"
    assert calls, "A 路径全挂却没有触发联网搜索 —— path C 没接上"
    assert len(_read_leads(tmp_path)) == 1


@pytest.mark.asyncio
async def test_injected_search_means_the_real_client_is_never_touched(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """★ **防误花钱的结构判据**：注入替身后，真实客户端一次都不许被调用。

    这条是为上面那次事故立的：判据必须证明"测试跑起来够不到网络"，
    而不是"我记得要打对补丁"。
    """
    from src.infrastructure.search import bocha

    def _forbidden(*_a, **_k):
        raise AssertionError("单测里调用了真实搜索客户端 —— 会花真钱")

    monkeypatch.setattr(bocha, "web_search", _forbidden)

    class _Router:
        @staticmethod
        def supports(_ind):  # noqa: ANN001
            return False

    def fake(q, count=5):  # noqa: ANN001
        return _Outcome(True, [_Hit("https://ok.example/d")], spent=False)

    pts, why = await sr.reroute("CPI", _Router(), [], root=tmp_path,
                                search=fake)
    assert pts == [] and "候选源线索" in why


@pytest.mark.asyncio
async def test_allow_search_false_spends_nothing(tmp_path: Path):
    """★ 关掉时**一次都不许花**（交互路径要有能力真的关掉它）。"""
    class _Router:
        @staticmethod
        def supports(_ind):  # noqa: ANN001
            return False

    calls: list[str] = []

    def search(q, count=5):  # noqa: ANN001
        calls.append(q)
        return _Outcome(True, [], spent=True)

    _pts, why = await sr.reroute("CPI", _Router(), [], root=tmp_path,
                                 allow_search=False, search=search)

    assert calls == [], "allow_search=False 却仍然发了搜索请求（在花钱）"
    assert "未开启联网搜索" in why


def test_module_declares_search_capabilities_consistently():
    """能力面与可用面一致：C 既然有了，模块文档不许还写着"没有搜索引擎能力"。"""
    doc = sr.__doc__ or ""
    assert "没有任何搜索引擎能力" not in doc or "~~" in doc, (
        "模块文档仍声称没有搜索引擎能力 —— 能力面与可用面不一致（R3）")
    assert "discover_candidate_urls" in doc, "文档没提 path C 的入口函数"
    #: 闸门必须真在代码里，不是只在注释里
    assert bocha.MAX_CALLS_TOTAL > 0 and bocha.MAX_CALLS_PER_DAY > 0
    assert bocha.MAX_CALLS_TOTAL < 1000, (
        "总量上限必须**小于**免费包 1000 —— 留余量给人工体检")
