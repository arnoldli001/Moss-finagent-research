"""单票**完整快照**的落盘热加载（重启后首屏毫秒级出图）。

## 这个测试对应的用户诉求（2026-09-24）

> "按你说的优化，首屏做到 1~2 秒级。"

自选概览早就有落盘热加载（`watchlist_snapshot.json`），但用户重启后真正盯的是
**当前这一只票**的四面板完整快照 —— 它要跑完整取数链，冷进程实测 QMT 在时
6.8 秒、QMT 关闭后 11.1 秒，首屏就被这一项卡住。

这里钉住五件事：

1. `hot_cache` 的存取契约：版本 / 过期 / **口径指纹** / 逐代码隔离 / 坏文件；
2. 热加载上来的那份**不伪造新鲜度**：`generated_at` / `trade_date` 原样保留，
   另外在 `health.gaps` 里写明"这是上次运行缓存的快照"；
3. 服务层只在"本进程还没算过这只票"时吃缓存，并且**一定**排一次后台真算；
4. `force_refresh=True`（前端「强制刷新」）与权重预览一律**不许**吃缓存；
5. **降级快照不许落盘** —— 落盘的这份是下次启动首屏的第一帧。
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from src.intraday import hot_cache
from src.intraday.config import IntradayConfig
from src.intraday.models import IntradaySnapshot, Quote, TrendPoint
from src.intraday.service import IntradayService


def asyncio_run(coro):
    return asyncio.run(coro)


def _complete(code: str = "300308",
              generated_at: str = "2026-09-24 14:30:58") -> IntradaySnapshot:
    """一份"算全了"的快照（有分时序列 + 有行情）—— 只有这种才允许落盘。

    判据见 `IntradayService._snapshot_is_worth_caching`：落盘的这份是
    **下次启动首屏的第一帧**，降级快照（数据源全挂时 trend/quote 为空）
    缓存下来就等于"重启后先给用户看一屏空白图"。
    """
    return IntradaySnapshot(
        code=code, name="中际旭创", trade_date="2026-09-24",
        generated_at=generated_at, session_label="交易中",
        quote=Quote(code=code, name="中际旭创", price=100.0),
        trend=[TrendPoint(ts="2026-09-24 14:30:00", price=100.0,
                          avg_price=99.5, volume=1000.0)],
    )


def _payload(code: str = "300308") -> dict:
    return _complete(code).model_dump(mode="json")


# ==================== hot_cache 存取契约 ====================


def test_payload_round_trip(tmp_dir) -> None:
    payload = _payload()
    assert hot_cache.save_snapshot_payload(
        "300308", payload, cache_dir=tmp_dir,
        fingerprint="abc123", trade_date="2026-09-24") is True

    outcome = hot_cache.load_snapshot_payload(
        "300308", cache_dir=tmp_dir, fingerprint="abc123")
    assert outcome.loaded is True
    assert outcome.payload == payload
    assert outcome.trade_date == "2026-09-24"
    assert outcome.fingerprint == "abc123"
    assert outcome.age_seconds < 5


def test_payload_rejected_when_fingerprint_changed(tmp_dir) -> None:
    """权重/阈值/档位/自选绑定改过 → 旧快照的分数不再是当前口径的分数。

    这条最要紧：`configs/intraday.yaml` 是热重载的，用户改完权重不重启。
    若还把旧快照端上去，就是"改了权重但分数没变"那种最像 bug 的现象。
    """
    hot_cache.save_snapshot_payload("300308", _payload(), cache_dir=tmp_dir,
                                    fingerprint="old")
    outcome = hot_cache.load_snapshot_payload(
        "300308", cache_dir=tmp_dir, fingerprint="new")
    assert outcome.loaded is False
    assert "口径已变化" in outcome.reason


def test_payload_rejected_when_too_old(tmp_dir) -> None:
    now = time.time()
    hot_cache.save_snapshot_payload("300308", _payload(), cache_dir=tmp_dir,
                                    fingerprint="f", now=now - 90000)
    outcome = hot_cache.load_snapshot_payload(
        "300308", cache_dir=tmp_dir, fingerprint="f", now=now)
    assert outcome.loaded is False
    assert "过旧" in outcome.reason


def test_payload_rejected_on_version_bump(tmp_dir) -> None:
    path = hot_cache.snapshot_payload_path("300308", tmp_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "version": hot_cache.PAYLOAD_VERSION + 1, "saved_at": time.time(),
        "fingerprint": "f", "payload": _payload()}), encoding="utf-8")
    outcome = hot_cache.load_snapshot_payload(
        "300308", cache_dir=tmp_dir, fingerprint="f")
    assert outcome.loaded is False
    assert "版本不匹配" in outcome.reason


def test_payload_rejected_when_corrupt(tmp_dir) -> None:
    path = hot_cache.snapshot_payload_path("300308", tmp_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{不是 JSON", encoding="utf-8")
    outcome = hot_cache.load_snapshot_payload("300308", cache_dir=tmp_dir)
    assert outcome.loaded is False
    assert "不可读" in outcome.reason


def test_payload_is_per_code(tmp_dir) -> None:
    hot_cache.save_snapshot_payload("300308", _payload("300308"),
                                    cache_dir=tmp_dir, fingerprint="f")
    hot_cache.save_snapshot_payload("600150", _payload("600150"),
                                    cache_dir=tmp_dir, fingerprint="f")
    assert hot_cache.load_snapshot_payload(
        "600150", cache_dir=tmp_dir, fingerprint="f").payload["code"] == "600150"
    assert hot_cache.drop_snapshot_payload("600150", cache_dir=tmp_dir) is True
    assert hot_cache.load_snapshot_payload(
        "600150", cache_dir=tmp_dir, fingerprint="f").loaded is False
    # 删一只不影响另一只
    assert hot_cache.load_snapshot_payload(
        "300308", cache_dir=tmp_dir, fingerprint="f").loaded is True


def test_payload_missing_file_is_not_an_error(tmp_dir) -> None:
    outcome = hot_cache.load_snapshot_payload("300308", cache_dir=tmp_dir)
    assert outcome.loaded is False
    assert "没有这只票的快照缓存" in outcome.reason


def test_payload_path_is_under_the_snapshots_subdir(tmp_dir) -> None:
    """一个代码一个文件、放在 snapshots/ 子目录里（便于按代码清理）。"""
    path = hot_cache.snapshot_payload_path("600150.SH", tmp_dir)
    assert path.parent.name == "snapshots"
    assert path.name == "600150.json"
    assert Path(tmp_dir) in path.parents


# ==================== 服务层 ====================


class _TestService(IntradayService):
    """测试替身：**不重读配置文件**。

    真实现的判断是 `fresh is not self._config`，而用例为了隔离做了深拷贝 ——
    身份永远不等，于是每次 `_reload_config()` 都会重建全部子组件
    （数据源/板块/估值/情绪分析器），把用例注入的替身覆盖回真实实现并**静默联网**
    （实测把一个用例拖到 17 秒）。另一个测试文件踩过同一个坑，处理方式一致。
    """

    def _reload_config(self) -> IntradayConfig:
        return self._config


def _service(tmp_dir) -> IntradayService:
    """真实服务实例，但缓存目录指到临时目录（绝不碰 data/cache/）。"""
    service = _TestService()
    service._snapshot_dir = str(tmp_dir)
    # `load_intraday_config` 按 mtime 缓存并返回同一个对象给所有调用方，
    # 改配置会泄漏给后续用例（其它测试文件的教训），这里深拷贝隔离。
    service._config = service._config.model_copy(deep=True)
    return service


def test_fingerprint_changes_when_weights_change(tmp_dir) -> None:
    service = _service(tmp_dir)
    config = service._config
    before = service._snapshot_fingerprint("300308", config)

    changed = config.model_copy(deep=True)
    changed.weights.vwap = changed.weights.vwap + 1
    assert service._snapshot_fingerprint("300308", changed) != before

    # 同一份配置重复算必须稳定（否则缓存永远命中不了）
    assert service._snapshot_fingerprint("300308", config) == before


def test_remember_then_load_keeps_freshness_fields_and_marks_stale(tmp_dir) -> None:
    service = _service(tmp_dir)
    service._remember_snapshot("300308", _complete(), service._config)
    assert "300308" in service._snapshot_fresh_at

    loaded = service._load_persisted_snapshot("300308", service._config)
    assert loaded is not None
    # 时间戳原样保留 —— 不伪造新鲜度
    assert loaded.generated_at == "2026-09-24 14:30:58"
    assert loaded.trade_date == "2026-09-24"
    # 并且明确写清"这是上次运行缓存的那份"
    assert any("上次运行缓存的快照" in gap for gap in loaded.health.gaps)
    # 返回的是副本：改它不该污染磁盘上的那份
    loaded.health.gaps.append("本地改动")
    again = service._load_persisted_snapshot("300308", service._config)
    assert again is not None
    assert "本地改动" not in again.health.gaps


def test_load_persisted_returns_none_without_cache(tmp_dir) -> None:
    service = _service(tmp_dir)
    assert service._load_persisted_snapshot("300308", service._config) is None


def test_snapshot_serves_persisted_cache_and_schedules_refresh(tmp_dir,
                                                              monkeypatch) -> None:
    """本进程还没算过这只票 → 先给落盘快照（毫秒级），并排一次后台真算。"""
    service = _service(tmp_dir)
    service._remember_snapshot("300308", _complete(), service._config)
    service._snapshot_fresh_at.clear()          # 模拟"刚重启，本进程还没算过"

    scheduled: list[str] = []
    monkeypatch.setattr(service, "_schedule_snapshot_refresh",
                        lambda code: scheduled.append(code))
    # 口径解析换成桩：缓存命中时**根本不该走到取数**
    monkeypatch.setattr(service, "_resolve_override",
                        lambda code, config: _async(None, ""))

    async def _run():
        return await service.snapshot("300308")

    snapshot = asyncio_run(_run())
    assert snapshot.generated_at == "2026-09-24 14:30:58"
    assert any("上次运行缓存的快照" in gap for gap in snapshot.health.gaps)
    assert scheduled == ["300308"]


class _Bomb:
    """一碰就炸的数据层替身。

    为什么要它：下面几个用例只关心"有没有去读落盘缓存"。若让真实 `snapshot()`
    主体跑下去，它会真的去取数（网络 + 子进程），实测把一个用例拖到 17 秒 ——
    `tests/conftest.py` 专门警告过这件事。换成炸弹后，只要缓存门放行了就会
    **立刻**抛错，用例既快又只验证该验证的东西。
    """

    def __getattr__(self, name):
        raise RuntimeError(f"数据层不该被访问（attr={name}）")


def test_snapshot_skips_persisted_cache_once_computed(tmp_dir,
                                                      monkeypatch) -> None:
    """本进程已经算过这只票 → 不再吃落盘缓存（稳态下重复快照只要 ~3 秒）。"""
    service = _service(tmp_dir)
    service._remember_snapshot("300308", _complete(), service._config)

    calls: list[str] = []
    monkeypatch.setattr(service, "_load_persisted_snapshot",
                        lambda code, config: calls.append(code))
    monkeypatch.setattr(service, "_resolve_override",
                        lambda code, config: _async(None, ""))
    monkeypatch.setattr(service, "_data", _Bomb())

    async def _run() -> None:
        try:
            await service.snapshot("300308")
        except Exception:  # noqa: BLE001 这里只关心"没读落盘缓存"
            pass

    asyncio_run(_run())
    assert calls == []


@pytest.mark.parametrize("kwargs", [
    {"force_refresh": True},
    {"light": True},
    {"config_patch": IntradayConfig()},
])
def test_persisted_cache_bypassed_for_force_light_and_preview(tmp_dir,
                                                             monkeypatch,
                                                             kwargs) -> None:
    """强制刷新 / 轻量快照 / 权重预览都**不许**吃完整快照缓存。

    - `force_refresh`：调用方要的就是"现在这一刻"；
    - `light`：自选列表要的是轻量口径，且它有自己那份 `_light_cache`；
    - `config_patch`：预览口径是临时的，落盘会让重启后首帧显示一份
      不属于任何已保存口径的分数。
    """
    service = _service(tmp_dir)
    service._remember_snapshot("300308", _complete(), service._config)

    calls: list[str] = []
    monkeypatch.setattr(service, "_load_persisted_snapshot",
                        lambda code, config: calls.append(code))
    monkeypatch.setattr(service, "_resolve_override",
                        lambda code, config: _async(None, ""))
    monkeypatch.setattr(service, "_data", _Bomb())

    async def _run() -> None:
        try:
            await service.snapshot("300308", **kwargs)
        except Exception:  # noqa: BLE001 数据层是炸弹，异常无所谓
            pass

    asyncio_run(_run())
    assert calls == []


def test_degraded_snapshot_is_not_persisted(tmp_dir, monkeypatch) -> None:
    """降级快照（取数链挂了、trend/quote 为空）**不许落盘**。

    回归（2026-09-24 实测）：落盘的这份是**下次启动首屏的第一帧**。
    把降级快照缓存下来，等价于"重启后先给用户看一屏空白图" ——
    宁可这一轮不缓存（下次启动多等一次真算），也不让空图占住首屏。
    """
    service = _service(tmp_dir)
    saved: list[str] = []
    monkeypatch.setattr(hot_cache, "save_snapshot_payload",
                        lambda *a, **kw: saved.append(str(a[0])) or True)

    # 只有代码、没有分时也没有行情 = 取数链全挂时的样子
    service._remember_snapshot("300308", IntradaySnapshot(code="300308"),
                               service._config)
    assert saved == []
    # 但"本进程算过了"仍要记下（否则每次请求都会去读落盘缓存）
    assert "300308" in service._snapshot_fresh_at

    service._remember_snapshot("300308", _complete(), service._config)
    assert saved == ["300308"]


def test_snapshot_without_quote_is_not_persisted(tmp_dir, monkeypatch) -> None:
    """有分时但没有行情也不算"算全了"（行情是四个面板的前提）。"""
    service = _service(tmp_dir)
    saved: list[str] = []
    monkeypatch.setattr(hot_cache, "save_snapshot_payload",
                        lambda *a, **kw: saved.append(str(a[0])) or True)

    snapshot = _complete()
    snapshot.quote = None
    service._remember_snapshot("300308", snapshot, service._config)
    assert saved == []


def _async(value, second=None):
    async def _coro():
        if second is None:
            return value
        return value, second
    return _coro()


def test_refresh_is_single_flight(tmp_dir, monkeypatch) -> None:
    """同一只票的后台刷新只保留一个在飞任务（WS 首帧 + 轮询会同时打进来）。"""
    service = _service(tmp_dir)
    started: list[str] = []

    async def _fake_snapshot(code, **kwargs):
        started.append(code)
        await asyncio.sleep(0.05)
        return IntradaySnapshot(code=code)

    monkeypatch.setattr(service, "snapshot", _fake_snapshot)

    async def _run():
        for _ in range(3):
            service._schedule_snapshot_refresh("300308")
        await asyncio.sleep(0.15)

    asyncio_run(_run())
    assert started == ["300308"]
    assert service._snapshot_refresh_inflight == set()
