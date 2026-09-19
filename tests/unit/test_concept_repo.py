"""概念板块库单测（全部离线：注入假客户端/假资金流，不打网络）。

重点钉住三件容易回退的事：
  1. **质量闸门**：市场级（"同花顺全A"）与量化标签（"百元股""上市首五日"）
     不能出现在关联板块里 —— 它们"很窄"或"很宽"但并不表达题材，
     早期只按成员数排序时正是这些排到了第一位；
  2. **相关性排序**：细分题材要排在宽泛大类前面；
  3. **不打网络**：`_save_membership` 曾在冷启动时触发全量名录拉取（实测 90 秒），
     这里用一个"计数客户端"把它钉住。
"""

from __future__ import annotations

import pandas as pd
import pytest

from src.quant.concept_repo import (
    MAX_MEMBERS,
    MIN_MEMBERS,
    ConceptRepository,
    _age_seconds,
    _is_usable_concept,
    _safe_int,
)


class FakeClient:
    """假 Tushare 客户端：记录调用次数，返回可预设的表。"""

    def __init__(self, index_frame: pd.DataFrame | None = None,
                 member_frame: pd.DataFrame | None = None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._index = index_frame if index_frame is not None else pd.DataFrame()
        self._member = member_frame if member_frame is not None else pd.DataFrame()

    def call(self, api: str, **params: object) -> pd.DataFrame:
        self.calls.append((api, dict(params)))
        if api == "ths_index":
            return self._index
        if api == "ths_member":
            return self._member
        return pd.DataFrame()


class FakeFundFlow:
    """假资金流：给定板块名 → 净额/涨幅。"""

    def __init__(self, snapshot: dict) -> None:
        self._snapshot = snapshot
        self.calls = 0

    def sector_snapshot_sync(self) -> dict:
        self.calls += 1
        return self._snapshot


def _index_frame(rows: list[tuple[str, str, int]]) -> pd.DataFrame:
    return pd.DataFrame({
        "ts_code": [row[0] for row in rows],
        "name": [row[1] for row in rows],
        "count": [row[2] for row in rows],
        "exchange": ["A"] * len(rows),
    })


# ==================== 小工具 ====================


@pytest.mark.parametrize("value,expected", [
    (10, 10), (10.0, 10), ("12", 12), (None, 0), (float("nan"), 0),
    (float("inf"), 0), ("abc", 0),
])
def test_safe_int_never_raises(value, expected) -> None:
    """`count` 列实测带 NaN：`int(nan)` 会抛错，必须兜住。"""
    assert _safe_int(value) == expected


@pytest.mark.parametrize("name,members,usable", [
    ("半导体", 181, True),
    ("存储芯片", 200, True),
    ("航海装备", 10, True),
    ("同花顺全A", 5524, False),      # 太宽 + 命中"全A"
    ("制造业指数", 3826, False),      # 市场级指数
    ("上市首五日", 3, False),         # 太窄（临时标签）
    ("百元股", 213, False),           # 量化标签
    ("昨日资金前十", 10, False),      # 量化标签
    ("2026中报预增", 507, False),     # 业绩横切概念
    ("创历史新高", 4, False),
    ("", 100, False),                 # 名录里没名字
])
def test_quality_gate(name: str, members: int, usable: bool) -> None:
    assert _is_usable_concept(name, members) is usable


def test_member_bounds_are_sane() -> None:
    assert MIN_MEMBERS < MAX_MEMBERS
    assert _is_usable_concept("x" * 3, MIN_MEMBERS)
    assert not _is_usable_concept("x" * 3, MIN_MEMBERS - 1)
    assert _is_usable_concept("x" * 3, MAX_MEMBERS)
    assert not _is_usable_concept("x" * 3, MAX_MEMBERS + 1)


def test_age_seconds_handles_garbage() -> None:
    assert _age_seconds("") == float("inf")
    assert _age_seconds("not-a-date") == float("inf")
    assert _age_seconds("2026-09-17T14:00:00") >= 0


# ==================== 相关性排序 ====================


def _repo(tmp_path, rows, members, snapshot=None) -> ConceptRepository:
    client = FakeClient(_index_frame(rows), pd.DataFrame({"ts_code": members}))
    repo = ConceptRepository(db_path=tmp_path / "concept.db", client=client,
                             fundflow=FakeFundFlow(snapshot or {}))
    return repo


def test_narrow_concept_ranks_above_broad_one(tmp_path) -> None:
    """细分题材必须排在宽泛大类前（早期按绝对净额排时正好相反）。"""
    rows = [("885001.TI", "细分题材", 20), ("885002.TI", "宽泛大类", 600)]
    repo = _repo(tmp_path, rows, ["885001.TI", "885002.TI"])
    boards, _stale = repo.boards_of("600150", limit=5)
    assert [b.name for b in boards] == ["细分题材", "宽泛大类"]


def test_broad_and_noise_concepts_are_filtered_out(tmp_path) -> None:
    rows = [
        ("885001.TI", "半导体", 181),
        ("700001.TI", "同花顺全A", 5524),
        ("883911.TI", "创历史新高", 4),
        ("883901.TI", "昨日资金前十", 10),
        ("885338.TI", "融资融券", 3844),
    ]
    repo = _repo(tmp_path, rows, [row[0] for row in rows])
    boards, _ = repo.boards_of("688825", limit=10)
    assert [b.name for b in boards] == ["半导体"], \
        "市场级/量化标签概念必须被闸门挡掉"


def test_flow_intensity_is_per_member(tmp_path) -> None:
    """资金项按**人均**归一化：大板块的绝对净额大不该让它赢。

    构造：宽泛板块 600 只、净额 600 亿（人均 1 亿）；细分板块 20 只、净额 10 亿
    （人均 0.5 亿）。细分板块仍应排前面（窄度权重更高），但两者的分差
    不应因为"绝对净额差 60 倍"而被拉爆。
    """
    rows = [("885001.TI", "细分题材", 20), ("885002.TI", "宽泛大类", 600)]
    snapshot = {
        "细分题材": {"net": 10e8, "pct_change": 1.0},
        "宽泛大类": {"net": 600e8, "pct_change": 1.0},
    }
    repo = _repo(tmp_path, rows, ["885001.TI", "885002.TI"], snapshot)
    boards, _ = repo.boards_of("600150", limit=5)
    by_name = {b.name: b for b in boards}
    assert boards[0].name == "细分题材"
    assert by_name["宽泛大类"].relevance < by_name["细分题材"].relevance


def test_stale_flag_when_online_fails(tmp_path) -> None:
    """在线取不到但落库里有 → 返回落库结果并标 stale（不假装是最新的）。"""
    rows = [("885001.TI", "半导体", 181)]
    repo = _repo(tmp_path, rows, ["885001.TI"])
    repo.boards_of("688825")                     # 先成功一次，落库
    # 让 ths_member 返回空表（模拟 Tushare 不可用），名录仍可用
    repo._client = FakeClient(_index_frame(rows), pd.DataFrame(columns=["ts_code"]))
    repo._member_cache.clear()                   # 绕开 24h 内存缓存
    boards, stale = repo.boards_of("688825")
    assert stale is True, "在线失败且有落库数据时必须标 stale"
    assert [b.name for b in boards] == ["半导体"]


def test_membership_save_does_not_fetch_full_index(tmp_path) -> None:
    """落库归属时**不能**触发全量名录拉取（冷启动会拖到 90 秒）。

    做法：先让名录进缓存，再把客户端的 ths_index 记录清零，
    然后查一只**新票** —— 期间不应出现任何 ths_index 调用。
    """
    rows = [("885001.TI", "半导体", 181)]
    repo = _repo(tmp_path, rows, ["885001.TI"])
    repo.boards_of("688825")                     # 建缓存
    assert repo._client is not None
    repo._client.calls.clear()                   # 只看后续调用
    repo._client._member = pd.DataFrame({"ts_code": ["885001.TI"]})
    repo.boards_of("300308")                     # 新票 → 走落库路径
    assert not [c for c in repo._client.calls if c[0] == "ths_index"], \
        "落库归属不应触发 ths_index 全量拉取"


# ==================== 联想 ====================


def test_suggest_dedups_and_orders_by_narrowness(tmp_path) -> None:
    rows = [
        ("885001.TI", "存储芯片", 200),
        ("885002.TI", "存储芯片", 180),      # 同名不同 code
        ("885003.TI", "存储设备", 60),
        ("885004.TI", "百元股", 213),        # 噪声，应被过滤
    ]
    repo = _repo(tmp_path, rows, [])
    items = repo.suggest("存储", limit=10)
    names = [item["name"] for item in items]
    assert names == ["存储设备", "存储芯片"], "同名要去重，且窄的排前"
    assert items[1]["members"] == 180, "同名概念应保留成员数更小的那个"


def test_suggest_empty_keyword_returns_nothing(tmp_path) -> None:
    repo = _repo(tmp_path, [("885001.TI", "半导体", 181)], [])
    assert repo.suggest("") == []
    assert repo.suggest("   ") == []
