"""主线「热快照」回归测试（`src/mainline/warm.py` + 路由的缓存契约）。

## 这里盯住的是三类**静默**故障

都不会抛异常、界面上也照样出数：

1. **落盘的 key 与接口用的 key 不一致**。落盘按 `|top|alert_limit|limit_boards`、
   接口按 `trade_date|top|alert_limit|limit_boards` —— 少一个竖线就永远命中不了，
   而表现只是"优化没生效"（还是等 8~23 秒），没人会怀疑是键写错了。
   `test_persist_then_load_round_trip`。

2. **水位线不匹配却仍然命中**。那会让用户看到**昨天或上一份数据**的榜，
   而且没有任何报错 —— 比慢几秒糟糕得多。
   `test_payload_for_rejects_other_watermark` / `_other_key` / `_garbage`。

3. **算完之后忘了落盘**。现算那条路（用户点「立即刷新」、或冷启动第一次访问）
   必须顺手把热快照写下来，否则下次重启又是冷的。
   这条由 `_compute_snapshot_payload` 保证，测试里直接验证它的落盘副作用。
"""

from __future__ import annotations

from src.api.routes import mainline as route
from src.mainline import warm


class FakeSnapshot:
    """最小快照替身：只需要 `trade_date` 与 `to_dict`。"""

    trade_date = "20260924"

    def to_dict(self, *, top: int = 60, alert_limit: int = 100) -> dict:
        return {"trade_date": self.trade_date, "top": top,
                "alert_limit": alert_limit, "scores": [{"code": "886042.TI"}],
                "alerts": []}


def _quiet_status(monkeypatch) -> None:
    """把 `_data_status()` 换成假的 —— 真实现要 2.9 秒（11 张表 COUNT）。"""
    monkeypatch.setattr(route, "_data_status",
                        lambda: {"cache_path": "x", "tables": {"ml_board": 1}})
    with route._DATA_STATUS_LOCK:
        route._DATA_STATUS_CACHE.clear()


# ==================================================================
# 键契约：落盘与读取必须逐字一致
# ==================================================================


def test_persist_then_load_round_trip(tmp_path, monkeypatch) -> None:
    """**回归测试**：`warm_persist` 写的 key，`warm_load` 必须能读出来。

    这两个 key 是在两处分别拼的字符串（一个带 `trade_date`、一个不带），
    对不上时不会有任何报错，只是热快照永远不生效。
    """
    _quiet_status(monkeypatch)
    assert route.warm_persist(FakeSnapshot(), root=tmp_path) is True

    # 落盘的是同一个水位线
    data = warm.load(root=tmp_path, force=True)
    assert data["watermark"] == "20260924"
    assert data["data_status"]["tables"] == {"ml_board": 1}

    # `warm_load` 用的 key 与 `warm_persist` 写的一致 → 装得进去
    monkeypatch.setattr(route, "_service",
                        lambda: type("S", (), {"data_watermark": lambda self: "20260924"})())
    with route._SNAPSHOT_LOCK:
        route._SNAPSHOT_CACHE.clear()
    info = route.warm_load(root=tmp_path)
    assert info["loaded"] is True, f"key 对不上：{info}"
    assert info["boards"] == 1
    with route._SNAPSHOT_LOCK:
        assert f"20260924||{route.WARM_TOP}|{route.WARM_ALERT_LIMIT}|0" \
            in route._SNAPSHOT_CACHE
        route._SNAPSHOT_CACHE.clear()


def test_warm_load_skips_when_watermark_moved(tmp_path, monkeypatch) -> None:
    """**回归测试**：数据已经更新（水位线变了）时**不许**装旧快照。

    装了就会让用户看到上一份数据的榜，而且完全不报错。
    """
    _quiet_status(monkeypatch)
    route.warm_persist(FakeSnapshot(), root=tmp_path)
    monkeypatch.setattr(route, "_service",
                        lambda: type("S", (), {"data_watermark": lambda self: "20260925"})())
    with route._SNAPSHOT_LOCK:
        route._SNAPSHOT_CACHE.clear()
    info = route.warm_load(root=tmp_path)
    assert info["loaded"] is False
    assert "水位线" in info["reason"]


# ==================================================================
# `payload_for`：三个条件缺一不可
# ==================================================================


def _made() -> dict:
    return warm.make({}, watermark="20260924", key="|60|100|0",
                     payload={"scores": [1, 2]}, data_status={"tables": {}})


def test_payload_for_accepts_exact_match() -> None:
    out = warm.payload_for(_made(), watermark="20260924", key="|60|100|0")
    assert out == {"scores": [1, 2]}


def test_payload_for_rejects_other_watermark() -> None:
    assert warm.payload_for(_made(), watermark="20260925",
                            key="|60|100|0") is None


def test_payload_for_rejects_other_key() -> None:
    """参数不同（比如前端换了 top）就该回退到现算，而不是拿一份参数不对的。"""
    assert warm.payload_for(_made(), watermark="20260924",
                            key="|20|100|0") is None


def test_payload_for_rejects_garbage() -> None:
    assert warm.payload_for({}, watermark="x", key="y") is None
    assert warm.payload_for({"watermark": "x", "key": "y"},
                            watermark="x", key="y") is None      # 没有 payload
    assert warm.payload_for({"watermark": "x", "key": "y", "payload": []},
                            watermark="x", key="y") is None      # payload 不是 dict


# ==================================================================
# 落盘/读取的健壮性
# ==================================================================


def test_load_missing_or_broken_file_is_empty(tmp_path) -> None:
    """读不到 / 解析失败一律按"没有"处理 —— 缓存坏了不能让面板 500。"""
    assert warm.load(root=tmp_path, force=True) == {}
    p = warm.store_path(tmp_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{不是 JSON", encoding="utf-8")
    assert warm.load(root=tmp_path, force=True) == {}


def test_save_is_best_effort(tmp_path, monkeypatch) -> None:
    """落盘失败**不许抛**（只读文件系统 / 磁盘满时要退化成"这次没热缓存"）。"""
    def boom(*_a, **_kw):
        raise OSError("read-only file system")

    monkeypatch.setattr("pathlib.Path.write_text", boom)
    warm.save({"at": "x"}, root=tmp_path)      # 不该抛
    warm._CACHE.clear()


def test_age_seconds_tolerates_bad_input() -> None:
    assert warm.age_seconds({}) is None
    assert warm.age_seconds({"at": "不是时间"}) is None
