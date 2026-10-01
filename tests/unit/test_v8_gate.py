"""V8 串行闸门的护栏（`CHG-0149`）。

## 为什么要这条判据

`py_mini_racer`（V8）**不是线程安全的**，而 akshare 连接器是日线链的公共路径，
`asyncio.to_thread` 会让并发调用变成**并发线程进 V8** ⇒ 进程硬崩
（实测 3/3：退出码 `0x80000003`、无 traceback、原生栈在 `mini_racer.dll`）。

判据分两条，**缺一不可**：
  ① **行为**：并发 4 次 `fetch()` 时，`_fetch_sync` 的**同时进入数必须 == 1**
     （用替身计数，不碰真 V8 —— 快、确定、零网络）；
  ② **自证**：同一套测量在**绕过闸门**时必须能测出重叠（max > 1），
     否则 ① 可能是"根本没并发起来"的假绿（本项目"探针自己会错"的教训）。
"""

from __future__ import annotations

import asyncio
import time

import pytest


def _connector_with_counter(monkeypatch: pytest.MonkeyPatch):
    """造一个 akshare 连接器替身：`_fetch_sync` 只记账与睡一小会儿。"""
    from src.infrastructure.connectors.akshare_connector import AkshareConnector

    connector = AkshareConnector()
    state = {"now": 0, "max": 0, "calls": 0}

    def fake_fetch_sync(*_args, **_kwargs):
        state["now"] += 1
        state["calls"] += 1
        state["max"] = max(state["max"], state["now"])
        time.sleep(0.05)          # 给"重叠"留出窗口
        state["now"] -= 1
        return []

    monkeypatch.setattr(connector, "_fetch_sync", fake_fetch_sync)
    return connector, state


def test_v8_gate_serialises_akshare_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """★ 并发 4 次 `fetch()` ⇒ `_fetch_sync` 同时只有 **1** 个在跑。"""
    connector, state = _connector_with_counter(monkeypatch)

    async def run() -> None:
        await asyncio.gather(*(connector.fetch("stock_close:600036")
                               for _ in range(4)))

    asyncio.run(run())

    assert state["calls"] == 4, "四次调用都必须真的落到 _fetch_sync"
    assert state["max"] == 1, (
        f"_fetch_sync 同时进入了 {state['max']} 个线程 —— V8 不是线程安全的，"
        "这会在真链路上表现为 0x80000003 原生崩溃（无 traceback、服务无痕消失）")


def test_the_measurement_can_detect_overlap(monkeypatch: pytest.MonkeyPatch) -> None:
    """★ 自证：**绕过闸门**时必须测得出重叠，否则上一条是假绿。

    直接 `to_thread(self._fetch_sync)`（不经过 `_fetch_sync_gated`）——
    这正是闸门要防的那种调用形态。
    """
    connector, state = _connector_with_counter(monkeypatch)

    async def run() -> None:
        await asyncio.gather(*(asyncio.to_thread(connector._fetch_sync,  # noqa: SLF001
                                                 "stock_close:600036")
                               for _ in range(4)))

    asyncio.run(run())

    assert state["max"] > 1, (
        "绕过闸门也测不出重叠 ⇒ 测量本身有问题（线程池太窄/替身太快），"
        "那么 test_v8_gate_serialises_akshare_calls 的绿灯不可信")
