"""板块指数「当日数据」的校验与补取（2026-09-23 实测缺口）。

## 事故

`ths_daily`（同花顺概念指数）对每个板块都返回**非空**的历史区间，所以
"返回非空 = 成功"这类判断全都通过 —— 而当天数据是**逐板块陆续发布**的：

    19:07 那次同步：138 个概念板块里只有 66 个带上了 20260923 → 作业报 `ok`
    19:19 重跑一次：138/138 全都有了

后果不是"少几个板块"：评分按"窗口内有数据"筛板块（`board_codes_with_bars`
只看区间），缺当天的那 72 个会**拿上一交易日的 K 线**参与当天打分 ——
分数照出、排名照排，界面上看不出异常。

所以 `sync_board_bars` 现在按 `end` 校验一遍，缺的**再取一轮**（有上限），
仍缺的如实写进 `missing` / `message`（status=partial，不是 ok）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.mainline.config import load_config
from src.mainline.datastore import MainlineDataStore
from src.mainline.models import BoardInfo, BoardKind

TODAY = "20260923"
YESTERDAY = "20260922"


class _Client:
    """假 Tushare 客户端：`available_at` 里的板块才"今天已发布"。

    ⚠️ 真实调用是 `self.tushare.client.call(...)` —— `TushareSource` 把裸客户端
    挂在 `.client` 上。少这一层会让所有调用进 `except` 分支（表现为"板块全部
    取不到"，而不是报错），第一版测试就踩了这个。
    """

    def __init__(self, available_at: set[str], *, also_missing: set[str] = frozenset(),
                 publish_after: int = 0) -> None:
        self.available_at = set(available_at)
        self.also_missing = set(also_missing)
        self.publish_after = publish_after
        self.calls: list[str] = []
        self.round = 0
        self.client = self

    def call(self, api: str, **params):  # noqa: ANN003
        import pandas as pd

        code = str(params.get("ts_code") or "")
        seen = self.calls.count(code)       # 这个代码之前被调用过几次
        self.calls.append(code)
        if code in self.also_missing:
            return pd.DataFrame()
        days = [YESTERDAY]
        # `available_at` = 一开始就发布了；`publish_after` = 第 N 次调用之后才发布
        if code in self.available_at or (self.publish_after and seen >= self.publish_after):
            days.append(TODAY)
        return pd.DataFrame([
            {"ts_code": code, "trade_date": day, "open": 1.0, "high": 1.0,
             "low": 1.0, "close": 1.0, "pre_close": 1.0, "vol": 1.0,
             "pct_change": 0.0}
            for day in days])


class _Remote:
    def __init__(self, client: _Client) -> None:
        self.tushare = client
        self.warehouse = None
        self.catalog = None


def _store(tmp_path: Path, client: _Client, codes: list[str]) -> MainlineDataStore:
    config = load_config(force=True)
    config.data.cache_path = str(tmp_path / "cache.db")
    config.universe.source = "catalog"
    config.universe.include_concept = True
    made = MainlineDataStore(config=config, remote=_Remote(client))
    made.ensure_schema()
    for index, code in enumerate(codes):
        made._write(  # noqa: SLF001 测试里直接建名录，绕开网络目录
            "INSERT OR REPLACE INTO ml_board"
            "(code,name,kind,members,source,list_date,updated_at)"
            " VALUES(?,?,?,?,?,?,?)",
            [(code, f"板块{index}", BoardKind.CONCEPT.value, 10, "test", "", "")])
    return made


def test_missing_target_day_is_reported_not_silently_ok(tmp_path, monkeypatch) -> None:
    """缺当天的板块必须报出来：`partial` + 明确的 message（不能报 ok）。"""
    monkeypatch.setattr("src.mainline.datastore.time.sleep", lambda _s: None)
    client = _Client({"885311.TI"})           # 只有一个板块"今天已发布"
    store = _store(tmp_path, client, ["885311.TI", "885362.TI"])

    result = store.sync_board_bars(start=YESTERDAY, end=TODAY)

    assert result.status == "partial", "缺当天数据却报 ok 就是静默"
    assert f"{TODAY}" in result.message and "885362.TI" in " ".join(result.missing)
    assert store.board_codes_with_bar_on(TODAY) == {"885311.TI"}


def test_retry_picks_up_late_published_boards(tmp_path, monkeypatch) -> None:
    """第二轮就发布了的板块要被补进来 —— 这是"再取一轮"的用途。"""
    monkeypatch.setattr("src.mainline.datastore.time.sleep", lambda _s: None)
    client = _Client(set(), publish_after=1)   # 第一轮都缺、重试轮全有
    store = _store(tmp_path, client, ["885311.TI", "885362.TI"])

    result = store.sync_board_bars(start=YESTERDAY, end=TODAY)

    assert result.status == "ok", result.message
    assert store.board_codes_with_bar_on(TODAY) == {"885311.TI", "885362.TI"}


def test_board_without_any_data_is_still_plain_missing(tmp_path, monkeypatch) -> None:
    """区块本身取不到（空表）与"当天没发布"要能区分：前者记代码，后者记 `日期:代码`。"""
    monkeypatch.setattr("src.mainline.datastore.time.sleep", lambda _s: None)
    client = _Client({"885311.TI"}, also_missing={"885362.TI"})
    store = _store(tmp_path, client, ["885311.TI", "885362.TI"])

    result = store.sync_board_bars(start=YESTERDAY, end=TODAY)

    assert "885362.TI" in result.missing
    assert not any(item.startswith(TODAY) for item in result.missing
                   if item.endswith("885362.TI"))


def test_board_codes_with_bar_on_is_exact_day(tmp_path) -> None:
    """`board_codes_with_bar_on` 是**精确到天**的判据（与窗口判据互补）。"""
    client = _Client({"885311.TI"})
    store = _store(tmp_path, client, ["885311.TI"])

    store.sync_board_bars(start=YESTERDAY, end=TODAY)

    assert store.board_codes_with_bar_on(TODAY) == {"885311.TI"}
    assert store.board_codes_with_bar_on("20260919") == set()


@pytest.mark.parametrize("boards", [["885311.TI"], ["885311.TI", "885362.TI"]])
def test_board_info_kind_matches_datastore_kinds(tmp_path, boards) -> None:
    """名录里写的 kind 必须是 datastore 认的概念板块（否则上面几条测不到概念分支）。"""
    client = _Client(set(boards))
    store = _store(tmp_path, client, boards)

    assert {item.kind for item in store.boards()} == {BoardKind.CONCEPT}
    assert all(isinstance(item, BoardInfo) for item in store.boards())
