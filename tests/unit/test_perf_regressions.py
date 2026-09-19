"""性能相关的**回归断言**：这些分支改错了不会报错，只会悄悄变慢。

## 为什么要单独一组测试

这一轮修掉的几个"卡"的问题，根因都不是功能错误，而是**热路径退化**：

| 改动 | 若被改回去 | 症状 | 会不会有测试失败 |
|---|---|---|---|
| 删除自选后**精确剔除缓存** | 改回 `invalidate_watchlist_cache()` | 删一只等 100+ 秒 | ❌ |
| 资金流**滑窗缓存** | 缓存格式一变 | 静默走全量（慢 1~3 秒/次） | ❌ 同上 |
| 置顶后**不重建组件** | 改回 `_reset_components()` | 取消置顶等 13 秒 | ❌ 同上 |
| 报价**共享批量缓存** | 去掉缓存 | 每只票一次 HTTP（2.1 秒） | ❌ 同上 |

所以本文件的断言全部是"**行为不该退化**"型的：不检查具体耗时（那会 flaky），
只检查"该走缓存的分支确实走了缓存 / 该保留的缓存确实没被清空"。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pandas as pd
import pytest

from src.intraday.config import WatchConfig

# ============================================================================
# 1. 删除自选：精确剔除缓存，而不是整表作废
# ============================================================================


class _FakeRepo:
    """最小的自选仓储替身（只实现这些用例需要的方法）。"""

    def __init__(self, items: list[WatchConfig]) -> None:
        self._items = list(items)

    async def list(self, kind: str = "sector") -> list[WatchConfig]:
        return list(self._items)


def _service_with_config(monkeypatch, tmp_path: Path, items: list[WatchConfig],
                         remaining_config: list[WatchConfig]):
    """造一个 IntradayService：写盘与组件重建都换成无害替身。

    刻意**不碰真实配置文件**：这些用例只验证"缓存怎么变"，
    真实写盘逻辑由 `test_intraday_watchlist_refresh` 等既有用例覆盖。
    """
    from src.intraday import service as service_module
    from src.intraday.config import IntradayConfig

    service = service_module.IntradayService.__new__(service_module.IntradayService)
    service._config_path = tmp_path / "intraday.yaml"
    service._config = IntradayConfig(watchlist=list(items))
    service._watch_cache = (time.monotonic(), list(items))
    service._watch_generation = 0
    service._watch_lock = asyncio.Lock()
    service._watch_refresh_inflight = False
    service._refresh_state = {}
    # `_reset_components` 会去建真实 provider（起子进程/连 QMT）——换成空操作
    monkeypatch.setattr(service, "_reset_components", lambda config: None)
    monkeypatch.setattr(
        service, "_reload_config",
        lambda: IntradayConfig(watchlist=list(remaining_config)))
    return service


def test_remove_watch_keeps_other_items_in_cache(monkeypatch, tmp_path) -> None:
    """删除一只票后：缓存**不能**被清空，其余条目必须原样保留。

    这是"删除要等 100 秒"的根因断言 —— 一旦有人把精确剔除改回
    `invalidate_watchlist_cache()`，`_watch_cache` 会变成 None，
    下一次 `/watchlist` 就要整表重算。这里直接把它钉住。
    """
    items = [WatchConfig(code="600150"), WatchConfig(code="300308"),
             WatchConfig(code="002463")]
    remaining = [items[0], items[2]]

    from src.intraday import config as config_module

    monkeypatch.setattr(config_module, "remove_watch",
                        lambda code, path: type(
                            "C", (), {"watchlist": remaining})())
    service = _service_with_config(monkeypatch, tmp_path, items, remaining)

    service.remove_watch("300308")

    assert service._watch_cache is not None, \
        "删除后缓存被清空了 —— 下一次读取会整表重算（实测 100+ 秒）"
    codes = [item.code for item in service._watch_cache[1]]
    assert codes == ["600150", "002463"], f"其余条目应保留且顺序不变，实际 {codes}"
    assert service._watch_generation > 0, "代次应递增，前端才知道列表变了"


def test_remove_watch_of_absent_code_keeps_cache_untouched(monkeypatch, tmp_path) -> None:
    """删一个不在列表里的代码（幂等路径）不该动缓存，也不该递增代次。"""
    items = [WatchConfig(code="600150"), WatchConfig(code="300308")]

    from src.intraday import config as config_module

    monkeypatch.setattr(config_module, "remove_watch",
                        lambda code, path: type("C", (), {"watchlist": items})())
    service = _service_with_config(monkeypatch, tmp_path, items, items)
    before = service._watch_cache[1]

    service.remove_watch("999999")

    assert service._watch_cache[1] is before, "没删掉东西就不该动缓存对象"
    assert service._watch_generation == 0


# ============================================================================
# 2. 置顶：只改一条 + 不重建组件（否则取消置顶要 13 秒）
# ============================================================================


def test_pin_updates_one_entry_without_resetting_components(monkeypatch, tmp_path) -> None:
    """置顶只应改缓存里的那一条并重排，**不能**触发组件重建。"""
    items = [WatchConfig(code="600150"), WatchConfig(code="300308")]
    service = _service_with_config(monkeypatch, tmp_path, items, items)
    called: list[bool] = []
    # 若实现里又调了 `_reset_components`，说明缓存会被置空 → 取消置顶变慢
    monkeypatch.setattr(service, "_reset_components",
                        lambda config: called.append(True))

    from src.intraday import config as config_module

    monkeypatch.setattr(config_module, "upsert_watch",
                        lambda item, path: type(
                            "C", (), {"watchlist": items})()

    )
    service.set_watch_pinned("300308", True)

    assert not called, ("置顶不该重建组件（会连带清空概览缓存，"
                        "实测让取消置顶从 0.03s 变成 13.31s）")
    assert service._watch_cache is not None
    cached = service._watch_cache[1]
    assert cached[0].code == "300308", "置顶项应排到最前"
    assert cached[0].pinned is True
    assert cached[1].pinned is False


# ============================================================================
# 3. 资金流滑窗缓存：命中 / 增量 / 损坏回落
# ============================================================================


def _frame(dates: list[str], codes: list[str]) -> pd.DataFrame:
    """造一份与 `stock_frame` 同结构的最小面板。"""
    rows = []
    for date in dates:
        for code in codes:
            rows.append({"code": code, "date": date, "trade_date": date, "net": 1e7,
                         "buy_lg": 0.0, "sell_lg": 0.0, "buy_elg": 0.0,
                         "sell_elg": 0.0, "close": 10.0, "circ_mv": 1e10})
    return pd.DataFrame(rows)


def test_frame_cache_roundtrip(tmp_path, monkeypatch) -> None:
    """写盘 → 读回：行数、最大日期、市值列都要在（滑窗增量的前提）。"""
    from src.fundflow.provider import FundFlowProvider

    provider = FundFlowProvider()
    monkeypatch.setattr(FundFlowProvider, "_cache_dir",
                        staticmethod(lambda: tmp_path))
    frame = _frame(["20260915", "20260916"], ["600150", "300308"])
    caps = pd.DataFrame({"code": ["600150", "300308"], "circ_mv": [1e10, 2e10]})

    provider._save_frame_cache(frame, caps, 10)
    loaded = provider._load_frame_cache(10)

    assert loaded is not None, "落盘后应能读回（否则每次都走全量）"
    got_frame, got_caps, cached_max = loaded
    assert len(got_frame) == len(frame)
    assert cached_max == "20260916", "最大日期是增量补数的锚点，必须正确"
    assert len(got_caps) == 2
    assert float(got_caps["circ_mv"].max()) == pytest.approx(2e10)


def test_frame_cache_missing_returns_none(tmp_path, monkeypatch) -> None:
    """没有缓存文件 → 返回 None（调用方走全量，而不是当成空缓存用）。"""
    from src.fundflow.provider import FundFlowProvider

    provider = FundFlowProvider()
    monkeypatch.setattr(FundFlowProvider, "_cache_dir",
                        staticmethod(lambda: tmp_path))
    assert provider._load_frame_cache(10) is None


def test_frame_cache_corrupt_file_falls_back(tmp_path, monkeypatch) -> None:
    """缓存文件损坏 → 返回 None 走全量，**不能抛错**（否则接口直接 500）。"""
    from src.fundflow.provider import FundFlowProvider

    provider = FundFlowProvider()
    monkeypatch.setattr(FundFlowProvider, "_cache_dir",
                        staticmethod(lambda: tmp_path))
    frame_path, caps_path = provider._cache_files(10)
    frame_path.write_bytes(b"this is not parquet")
    caps_path.write_bytes(b"nor is this")

    assert provider._load_frame_cache(10) is None, "损坏的缓存应静默回落到全量"


def test_frame_cache_too_old_is_ignored(tmp_path, monkeypatch) -> None:
    """缓存超过 30 天 → 视为过期（增量补数不划算），返回 None 走全量。"""
    from src.fundflow import provider as provider_module
    from src.fundflow.provider import FundFlowProvider

    provider = FundFlowProvider()
    monkeypatch.setattr(FundFlowProvider, "_cache_dir",
                        staticmethod(lambda: tmp_path))
    frame = _frame(["20250101"], ["600150"])          # 很久以前
    caps = pd.DataFrame({"code": ["600150"], "circ_mv": [1e10]})
    provider._save_frame_cache(frame, caps, 10)

    assert provider._load_frame_cache(10) is None, "过期缓存应被忽略"
    assert provider_module is not None                 # 保持导入被使用


def test_incremental_only_reads_new_days(tmp_path, monkeypatch) -> None:
    """增量补数**只读新增的那几天**，不能把整个窗口重读一遍。

    断言方式：记录仓库 `load` 收到的 start/end，验证它就是那几天。
    """
    from src.fundflow.provider import FundFlowProvider

    provider = FundFlowProvider()
    seen: list[tuple[str, str]] = []

    class _Client:
        def load(self, dataset: str, start: str = "", end: str = "", **kw):
            seen.append((dataset, start, end))
            return _frame(["20260917"], ["600150"])

    monkeypatch.setattr(provider, "_warehouse_client", lambda: _Client())
    out = provider._load_incremental(["20260917"], 10)

    assert out is not None and len(out) > 0
    assert seen, "增量路径必须真的去读仓库"
    assert all(start == end == "20260917" for _ds, start, end in seen), \
        f"增量只该读新增日，实际读了 {seen}"
    assert {ds for ds, _s, _e in seen} >= {"moneyflow"}, "至少要取资金流表"


def test_sector_cache_roundtrip_and_old_ignored(tmp_path, monkeypatch) -> None:
    """板块截面缓存：写盘 → 读回；超过 5 天视为过期（否则会用上个月的截面）。"""
    import os

    from src.fundflow.provider import FundFlowProvider

    provider = FundFlowProvider()
    monkeypatch.setattr(FundFlowProvider, "_cache_dir",
                        staticmethod(lambda: tmp_path))
    payload = {"半导体": {"code": "BK1036.DC", "net": 1.36e10, "rank": 1.0}}
    provider._save_sector_cache(payload)

    loaded = provider._load_sector_cache()
    assert "半导体" in loaded and loaded["半导体"]["rank"] == 1.0

    # 把文件时间改成 6 天前 → 应被判定过期
    path = provider._sector_cache_path()
    old = time.time() - 6 * 86400
    os.utime(path, (old, old))
    assert provider._load_sector_cache() == {}, "超过 5 天的板块截面应被忽略"


def test_quote_batch_cache_reuses_result() -> None:
    """腾讯批量快照：同一批（或子集）在缓存窗口内**只打一次 HTTP**。

    这是"每只票 2.1 秒"的根因断言 —— 去掉缓存就会退回到
    "26 只票 = 26 次 HTTP"。
    """
    from src.intraday.sources import TencentSource

    calls: list[str] = []

    class _Resp:
        encoding = "gbk"
        text = ("v_sh600150=\"1~中国船舶~600150~38.95~37.80~38.10~123~456~789~"
                "10.0~11.0~12.0~13.0~14.0~15.0~16.0~17.0~18.0~19.0~20.0~"
                "21.0~22.0~23.0~24.0~25.0~26.0~27.0~28.0~29.0~30.0~31.0~"
                "3.07~32.0~33.0~34.0~35.0~36.0~37.0~38.0~39.0~40.0~41.0~"
                "2931.2~3400.0~42.0~43.0~44.0~45.0~46.0~47.0~\";")

        def raise_for_status(self) -> None:
            return None

    class _Client:
        async def get(self, url: str, **kwargs):
            calls.append(url)
            return _Resp()

    provider = TencentSource(_Client())            # type: ignore[arg-type]

    async def run() -> None:
        await provider._raw_quotes(["sh600150"])       # type: ignore[attr-defined]
        await provider._raw_quotes(["sh600150"])       # type: ignore[attr-defined]
        await provider._raw_quotes(["sh600150"])       # type: ignore[attr-defined]

    asyncio.run(run())
    assert len(calls) == 1, (
        f"同一代码在缓存窗口内应只请求 1 次，实际 {len(calls)} 次 —— "
        "缓存被去掉会让自选池刷新退化成 N 次 HTTP（每次约 2.1 秒）")
