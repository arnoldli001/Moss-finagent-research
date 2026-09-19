"""做T辅助「重启热加载」单测：快照落盘/加载/过期/与服务缓存的衔接。

背景（2026-09-17 用户报障）：重启后前端要等几十秒才出数据。实测干净进程
启动只要 3.2s，但**首个** `/api/v1/intraday/watchlist` 要 59.8s（26~39 只票
冷启动全部打数据源），第二个 0.00s。热加载把上一轮的概览先给出去，
重算交给后台（stale-while-revalidate）。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from src.intraday import hot_cache
from src.intraday.models import WatchItem


def _item(code: str, name: str = "", score: float | None = 50.0,
          price: float | None = 10.0, **kwargs) -> WatchItem:
    return WatchItem(code=code, name=name or f"票{code}", total_score=score,
                     price=price, **kwargs)


# ---------------------------------------------------------------- 落盘

def test_save_then_load_roundtrip(tmp_path: Path) -> None:
    items = [_item("600150", "中国船舶", 62.5, 35.2, boards=["军工"],
                   signal_strength="solid", signal_kind="low_buy",
                   quote_ts="2026-09-17 15:00:00", pinned=True),
             _item("300308", "中际旭创", None, None)]
    assert hot_cache.save_snapshot(items, cache_dir=tmp_path) is True

    loaded = hot_cache.load_snapshot(cache_dir=tmp_path)
    assert loaded.loaded is True
    assert loaded.count == 2
    first = loaded.items[0]
    assert first["code"] == "600150"
    assert first["name"] == "中国船舶"
    assert first["boards"] == ["军工"]
    assert first["total_score"] == 62.5
    assert first["signal_strength"] == "solid"
    assert first["signal_kind"] == "low_buy"
    assert first["quote_ts"] == "2026-09-17 15:00:00"
    assert first["pinned"] is True
    # None 必须原样保留（不能变成 0，否则前端会显示"0 分"而不是"暂无"）
    assert loaded.items[1]["total_score"] is None
    assert loaded.items[1]["price"] is None


def test_saved_file_is_plain_json(tmp_path: Path) -> None:
    """落盘内容必须是纯 JSON 叶子字段（不带句柄/DataFrame）。"""
    hot_cache.save_snapshot([_item("600150")], cache_dir=tmp_path)
    raw = json.loads((tmp_path / "watchlist_snapshot.json").read_text(encoding="utf-8"))
    assert raw["version"] == hot_cache.SNAPSHOT_VERSION
    assert raw["count"] == 1
    assert isinstance(raw["saved_at"], float)
    assert set(raw["items"][0]) <= {
        "code", "name", "boards", "total_score", "signal_strength",
        "signal_kind", "price", "change_pct", "quote_ts", "pinned"}


def test_save_is_atomic_no_tmp_left_behind(tmp_path: Path) -> None:
    hot_cache.save_snapshot([_item("600150")], cache_dir=tmp_path)
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == ["watchlist_snapshot.json"]


def test_save_empty_list_is_allowed_but_load_reports_empty(tmp_path: Path) -> None:
    assert hot_cache.save_snapshot([], cache_dir=tmp_path) is True
    loaded = hot_cache.load_snapshot(cache_dir=tmp_path)
    assert loaded.loaded is False
    assert "没有条目" in loaded.reason


def test_save_failure_is_swallowed(tmp_path: Path) -> None:
    """落盘失败绝不能抛异常（它在请求路径上）。"""
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory", encoding="utf-8")
    assert hot_cache.save_snapshot([_item("600150")], cache_dir=blocker) is False


def test_save_overwrites_previous_snapshot(tmp_path: Path) -> None:
    hot_cache.save_snapshot([_item("600150"), _item("300308")], cache_dir=tmp_path)
    hot_cache.save_snapshot([_item("600150")], cache_dir=tmp_path)
    loaded = hot_cache.load_snapshot(cache_dir=tmp_path)
    assert loaded.count == 1
    assert loaded.items[0]["code"] == "600150"


# ---------------------------------------------------------------- 加载与降级

def test_load_missing_file_reports_reason(tmp_path: Path) -> None:
    loaded = hot_cache.load_snapshot(cache_dir=tmp_path)
    assert loaded.loaded is False
    assert "没有上次的快照" in loaded.reason


def test_load_corrupt_file_is_not_fatal(tmp_path: Path) -> None:
    (tmp_path / "watchlist_snapshot.json").write_text("{not json", encoding="utf-8")
    loaded = hot_cache.load_snapshot(cache_dir=tmp_path)
    assert loaded.loaded is False
    assert "不可读" in loaded.reason


def test_load_rejects_version_mismatch(tmp_path: Path) -> None:
    (tmp_path / "watchlist_snapshot.json").write_text(json.dumps({
        "version": hot_cache.SNAPSHOT_VERSION + 99, "saved_at": time.time(),
        "items": [{"code": "600150"}]}), encoding="utf-8")
    loaded = hot_cache.load_snapshot(cache_dir=tmp_path)
    assert loaded.loaded is False
    assert "版本不匹配" in loaded.reason


def test_load_rejects_too_old_snapshot(tmp_path: Path) -> None:
    """过旧就不加载：宁可等一次重算，也不把昨天的价格当今天的展示。"""
    now = time.time()
    hot_cache.save_snapshot([_item("600150")], cache_dir=tmp_path, now=now - 7200)
    ok = hot_cache.load_snapshot(cache_dir=tmp_path, max_age_seconds=3600, now=now)
    assert ok.loaded is False
    assert "过旧" in ok.reason

    fresh = hot_cache.load_snapshot(cache_dir=tmp_path, max_age_seconds=10800, now=now)
    assert fresh.loaded is True
    assert fresh.age_seconds == pytest.approx(7200, abs=5)


def test_load_drops_codes_no_longer_in_watchlist(tmp_path: Path) -> None:
    """自选删过票 → 旧快照里被删掉的代码不能继续显示。"""
    hot_cache.save_snapshot([_item("600150"), _item("300308")], cache_dir=tmp_path)
    loaded = hot_cache.load_snapshot(cache_dir=tmp_path, known_codes=["600150"])
    assert loaded.loaded is True
    assert [i["code"] for i in loaded.items] == ["600150"]
    assert loaded.dropped_codes == ["300308"]


def test_load_returns_not_loaded_when_no_overlap(tmp_path: Path) -> None:
    hot_cache.save_snapshot([_item("600150")], cache_dir=tmp_path)
    loaded = hot_cache.load_snapshot(cache_dir=tmp_path, known_codes=["000001"])
    assert loaded.loaded is False
    assert "无交集" in loaded.reason


def test_load_normalises_code_with_suffix(tmp_path: Path) -> None:
    """带交易所后缀的代码也要能对上（zfill 后比较）。"""
    hot_cache.save_snapshot([_item("600150")], cache_dir=tmp_path)
    loaded = hot_cache.load_snapshot(cache_dir=tmp_path, known_codes=["600150.SH"])
    assert loaded.loaded is True


def test_drop_snapshot_removes_file(tmp_path: Path) -> None:
    hot_cache.save_snapshot([_item("600150")], cache_dir=tmp_path)
    assert hot_cache.drop_snapshot(cache_dir=tmp_path) is True
    assert not (tmp_path / "watchlist_snapshot.json").exists()
    assert hot_cache.drop_snapshot(cache_dir=tmp_path) is True   # 幂等


def test_snapshot_path_is_under_cache_dir() -> None:
    assert hot_cache.snapshot_path("data/cache/intraday").name == \
        "watchlist_snapshot.json"


# ---------------------------------------------------------------- 与服务衔接

class _StubConfig:
    def __init__(self, codes: list[str]) -> None:
        self.watchlist = [type("W", (), {"code": c})() for c in codes]


def _bare_service(tmp_path: Path, codes: list[str]):
    """构造一个只带热加载所需字段的 IntradayService（跳过数据源装配）。"""
    from src.intraday.service import IntradayService

    svc = IntradayService.__new__(IntradayService)
    svc._snapshot_dir = str(tmp_path)
    svc._snapshot_loaded = False
    svc._snapshot_state = {}
    svc._watch_cache = None
    svc._watch_generation = 0
    svc._reload_config = lambda: _StubConfig(codes)      # type: ignore[method-assign]
    return svc


def test_service_hot_load_sets_cache_and_marks_expired(tmp_path: Path) -> None:
    """关键：热加载后的缓存时间戳必须是"很久以前" —— 这样既有分支会自动
    先返回旧值、再触发后台重算（stale-while-revalidate）。"""
    hot_cache.save_snapshot([_item("600150", "中国船舶", 62.5, 35.2),
                             _item("300308", "中际旭创", 50.0, 20.0)],
                            cache_dir=tmp_path)
    svc = _bare_service(tmp_path, codes=["600150", "300308"])

    state = svc.load_cached_watchlist()

    assert state["loaded"] is True
    assert state["count"] == 2
    assert svc._watch_cache is not None
    stamp, items = svc._watch_cache
    assert len(items) == 2
    assert time.monotonic() - stamp > 1000, "时间戳必须被设成很久以前（视为过期）"
    assert svc._watch_generation == 1


def test_service_hot_load_is_idempotent(tmp_path: Path) -> None:
    hot_cache.save_snapshot([_item("600150")], cache_dir=tmp_path)
    svc = _bare_service(tmp_path, codes=["600150"])
    first = svc.load_cached_watchlist()
    svc._watch_cache = None                    # 人为清空，验证不会二次读盘
    second = svc.load_cached_watchlist()
    assert first["loaded"] is True
    assert second == first
    assert svc._watch_cache is None


def test_service_hot_load_without_snapshot_keeps_cache_empty(tmp_path: Path) -> None:
    svc = _bare_service(tmp_path, codes=["600150"])
    state = svc.load_cached_watchlist()
    assert state["loaded"] is False
    assert svc._watch_cache is None            # 没有旧数据 → 老实走一次重算


def test_service_hot_load_drops_removed_codes(tmp_path: Path) -> None:
    hot_cache.save_snapshot([_item("600150"), _item("300308")], cache_dir=tmp_path)
    svc = _bare_service(tmp_path, codes=["600150"])
    state = svc.load_cached_watchlist()
    assert state["loaded"] is True
    assert state["count"] == 1
    assert state["dropped"] == ["300308"]


def test_service_persist_watch_snapshot_writes_file(tmp_path: Path) -> None:
    svc = _bare_service(tmp_path, codes=["600150"])
    svc._persist_watch_snapshot([_item("600150")])
    assert (tmp_path / "watchlist_snapshot.json").exists()
    loaded = hot_cache.load_snapshot(cache_dir=tmp_path)
    assert loaded.count == 1


def test_service_persist_ignores_empty_list(tmp_path: Path) -> None:
    """空列表不落盘：否则下次启动会把"空"当成有效热加载数据。"""
    svc = _bare_service(tmp_path, codes=["600150"])
    svc._persist_watch_snapshot([])
    assert not (tmp_path / "watchlist_snapshot.json").exists()


def test_service_hot_load_ignores_unparsable_items(tmp_path: Path) -> None:
    """快照里混入坏条目时只丢那一条，其余照常热加载。"""
    (tmp_path / "watchlist_snapshot.json").write_text(json.dumps({
        "version": hot_cache.SNAPSHOT_VERSION, "saved_at": time.time(),
        "items": [{"code": "600150", "name": "好条目", "total_score": 50.0},
                  {"code": "300308", "signal_strength": "不是合法枚举值"}]}),
        encoding="utf-8")
    svc = _bare_service(tmp_path, codes=["600150", "300308"])
    state = svc.load_cached_watchlist()
    assert state["loaded"] is True
    assert state["count"] == 1
