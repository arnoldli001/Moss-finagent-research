"""创新药IND连接器测试（全离线：构造CDE响应 + 打桩Session）。

重点钉死两个真实踩过的坑：
1. **CDE 超页会绕回**：请求大于末页的 pageNum 不返回空，而是返回第1页内容。
   若靠"翻到空页为止"或固定页数上限，多出来的页会把最新月份重复计数
   （实测把 2026-09 从 240 抬到 795）。
2. **窗口外月份只被部分统计**：为判断"已越过窗口"必然多读一页，
   那一页里早于窗口的月份只统计到一部分，必须丢弃。
"""

from __future__ import annotations

from datetime import date

import pytest

from src.core.exceptions import DataFetchError
from src.infrastructure.connectors import pharma_ind_connector as ind_mod
from src.infrastructure.connectors.pharma_ind_connector import (
    INDICATOR,
    PharmaIndConnector,
    is_class1_ind,
    parse_ind_records,
)

# ---------------- 纯函数：判定与归月 ----------------

@pytest.mark.parametrize("acceptid,kind,expected", [
    ("CXHL2601201", "1类", True),          # 化药1类 IND
    ("CXSL2601001", "1.1", True),          # 生物制品1类亚型（易被漏掉）
    ("CXZL2601001", "1.4", True),
    ("CXHL2601201", "1", True),
    ("CXHL2601201", "2.2", False),         # 2类不是1类创新药
    ("CXHL2601201", "原9", False),         # 原分类不算
    ("CXHS2601201", "1类", False),         # 上市申请(NDA)不是IND
    ("JXHL2600001", "1类", False),         # 进口IND不计入境内口径
    ("CXHL2601201", "1类;2.2", True),      # 多值取首个token
])
def test_is_class1_ind_matrix(acceptid, kind, expected):
    assert is_class1_ind({"acceptid": acceptid, "registerkind": kind}) is expected


def test_parse_records_aggregates_by_month_and_skips_noise():
    records = [
        {"acceptid": "CXHL2601201", "registerkind": "1类", "createdate": "2026-09-24"},
        {"acceptid": "CXSL2601002", "registerkind": "1.1", "createdate": "2026-09-02"},
        {"acceptid": "CXHS2601003", "registerkind": "1类", "createdate": "2026-09-02"},
        {"acceptid": "CXHL2601004", "registerkind": "2.2", "createdate": "2026-09-02"},
        {"acceptid": "CXHL2601005", "registerkind": "1类", "createdate": "2026-08-11"},
    ]
    assert parse_ind_records(records) == {"2026-09": 2, "2026-08": 1}


# ---------------- 翻页：绕回必须止步 ----------------

class _FakeResponse:
    def __init__(self, body: dict):
        self.status_code = 200
        self.headers = {"content-type": "application/json;charset=utf-8"}
        self.content = __import__("json").dumps(body).encode("utf-8")


class _WrapSession:
    """模拟 CDE：页数超过末页时**绕回第1页**（真实行为，2026-09-26 实测）。"""

    def __init__(self, total: int, per_page: int = 50):
        self.headers: dict = {}
        self.total = total
        self.per_page = per_page
        self.requests: list[int] = []

    def get(self, *a, **k):
        return _FakeResponse({"code": 200, "data": {}})

    def post(self, url, data=None, timeout=None):
        page = int(data["pageNum"])
        self.requests.append(page)
        last_real = -(-self.total // self.per_page)
        # 绕回：超出末页时返回第1页内容
        effective = page if page <= last_real else 1
        start = (effective - 1) * self.per_page
        n = min(self.per_page, max(0, self.total - start))
        records = [
            {"acceptid": f"CXHL26{start + i:05d}", "registerkind": "1类",
             "createdate": "2026-09-10"}
            for i in range(n)
        ]
        return _FakeResponse({"code": 200, "data": {"total": self.total,
                                                   "records": records}})

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_pagination_stops_at_last_page_and_ignores_wraparound(monkeypatch):
    """末页之后必须止步：绕回页会让同一批最新记录被重复计数。"""
    total = 120           # 3 页（50/50/20）
    sess = _WrapSession(total)
    monkeypatch.setattr(ind_mod.requests, "Session", lambda: sess)

    counts: dict[str, int] = {}
    conn = PharmaIndConnector()
    conn._sweep(sess, 2026, "hy", "xy", "2026-01-01", counts, "")

    # 翻页只到第 3 页就停（不再请求 4、5…），且总数恰好等于 total
    assert sess.requests == [1, 2, 3]
    assert sum(counts.values()) == total


def test_wraparound_guard_stops_even_without_total(monkeypatch):
    """即便服务端 total 缺失，整页重复出现也必须立即停止（防御性）。"""
    class _NoTotalSession(_WrapSession):
        def post(self, url, data=None, timeout=None):
            self.requests.append(int(data["pageNum"]))
            # 永远返回同一页，且不带 total
            records = [{"acceptid": f"CXHL26{i:05d}", "registerkind": "1类",
                        "createdate": "2026-09-10"} for i in range(50)]
            return _FakeResponse({"code": 200, "data": {"records": records}})

    sess = _NoTotalSession(200)
    monkeypatch.setattr(ind_mod.requests, "Session", lambda: sess)
    counts: dict[str, int] = {}
    PharmaIndConnector()._sweep(sess, 2026, "hy", "xy", "2026-01-01", counts, "")

    assert sess.requests == [1, 2]           # 第2页发现整页重复 → 停
    assert sum(counts.values()) == 50        # 没有把同一页数两遍


def test_out_of_window_months_are_dropped(monkeypatch):
    """窗口之前只被部分统计的月份必须丢弃（宁可缺月，不给"半个月"）。"""
    records = [
        {"acceptid": "CXHL2601001", "registerkind": "1类", "createdate": "2026-03-05"},
        {"acceptid": "CXHL2601002", "registerkind": "1类", "createdate": "2026-02-20"},
    ]

    class _S(_WrapSession):
        def post(self, url, data=None, timeout=None):
            # 三个过滤组合各自返回不同记录；这里只让化药组有数据，
            # 否则同一批记录会被 3 个组合各数一遍（真实接口不会这样）
            if data["drugtype"] != "hy" or int(data["pageNum"]) != 1:
                return _FakeResponse({"code": 200, "data": {"total": 0,
                                                           "records": []}})
            return _FakeResponse({"code": 200, "data": {"total": 2,
                                                       "records": records}})

    monkeypatch.setattr(ind_mod.requests, "Session", lambda: _S(2))
    counts, _ = PharmaIndConnector()._fetch_counts("2026-03-01")
    assert counts == {"2026-03": 1}          # 2026-02 在窗口外 → 丢弃


# ---------------- fetch 组装 ----------------

async def _run_fetch(monkeypatch, counts, covered="2026-09-24"):
    monkeypatch.setattr(PharmaIndConnector, "_fetch_counts",
                        lambda self, cutoff: (counts, covered))
    monkeypatch.setattr(ind_mod, "_write_snapshot", lambda *a, **k: None)
    monkeypatch.setattr(ind_mod, "_latest_snapshot", lambda source: None)
    return await PharmaIndConnector().fetch(INDICATOR)


def test_supports_and_capabilities_declare_real_data():
    conn = PharmaIndConnector()
    assert conn.supports(INDICATOR) is True
    assert conn.supports("ind:不存在") is False
    caps = conn.get_capabilities()
    assert caps["simulated"] is False
    assert caps["indicators"] == [INDICATOR]
    assert "件数" in caps["notes"]


async def test_fetch_marks_current_month_partial(monkeypatch):
    """当月必然不满月（T-2 延迟 + 月末回填）→ 必须打 partial 标注。"""
    current = date.today().strftime("%Y-%m")
    points = await _run_fetch(monkeypatch, {current: 240, "2026-01": 265})

    by_month = {p.period_date[:7]: p for p in points}
    assert by_month[current].value == 240.0
    assert by_month[current].extra["partial"] is True
    assert current in by_month[current].extra["partial_note"] or \
        "已覆盖至" in by_month[current].extra["partial_note"]
    # 往月不得被打上 partial
    assert "partial" not in by_month["2026-01"].extra


async def test_fetch_point_metadata(monkeypatch):
    points = await _run_fetch(monkeypatch, {"2026-08": 215})
    p = points[0]
    assert p.period_date == "2026-08-01"
    assert p.unit == "个"
    assert p.confidence == 0.85 and p.verified is True
    assert p.extra["simulated"] is False
    assert p.extra["frequency"] == "monthly"
    assert p.extra["count_basis"] == "acceptance_number"
    assert "受理号件数" in p.extra["count_note"]
    # ★ 口径与局限必须出现在 **source_name** 上（LLM 上下文只拼 source_name、
    # 不展开 extra）：既要说明是"受理号件数"，也要提示"当月不满月"。
    assert "CDE" in p.source_name
    assert "受理号件数" in p.source_name
    assert "不满月" in p.source_name


async def test_snapshot_fallback_when_api_fails(monkeypatch):
    def boom(self, cutoff):
        raise DataFetchError("反爬")

    monkeypatch.setattr(PharmaIndConnector, "_fetch_counts", boom)
    monkeypatch.setattr(ind_mod, "_latest_snapshot", lambda source: {
        "counts": {"2026-07": 251, "2026-08": 215},
    })
    points = await PharmaIndConnector().fetch(INDICATOR)
    assert [int(p.value) for p in points] == [251, 215]
    assert points[0].extra["storage_fallback"] is True
    assert points[0].extra["simulated"] is False


async def test_api_failure_without_snapshot_raises(monkeypatch):
    def boom(self, cutoff):
        raise DataFetchError("反爬")

    monkeypatch.setattr(PharmaIndConnector, "_fetch_counts", boom)
    monkeypatch.setattr(ind_mod, "_latest_snapshot", lambda source: None)
    with pytest.raises(DataFetchError):
        await PharmaIndConnector().fetch(INDICATOR)


async def test_unsupported_indicator_raises():
    with pytest.raises(DataFetchError):
        await PharmaIndConnector().fetch("ind:不存在")
