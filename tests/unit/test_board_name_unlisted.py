"""板块名解析的"不可用"路径单测。

现场（2026-09-17，服务启动日志每次都有）：

    同花顺概念快照(覆铜板) 子进程内报错: IndexError: index out of bounds
    同花顺概念快照(印制电路板) 子进程内报错: IndexError: index out of bounds
    同花顺概念快照(锂电铜箔) 子进程内报错: IndexError: index out of bounds
    同花顺概念快照(电子铜箔) 子进程内报错: IndexError: index out of bounds

根因在 akshare 里（`C:\\veighna_studio\\Lib\\site-packages\\akshare\\stock_feature\\
stock_board_concept_ths.py:101`）：

    symbol_code = stock_board_ths_map_df[stock_board_ths_map_df["name"] == symbol][
        "code"].values[0]

名字不在官方列表里时 `.values[0]` 直接下标越界。实测这四个名字**确实不在**
同花顺 375 个概念板块中（含"铜箔"的只有 `PET铜箔`，含"PCB"的只有 `PCB概念`）。
原实现把用户原名交给 akshare，于是每个这样的板块都要白起一个注定崩溃的子进程、
等它崩、再退到新浪，并且每次启动都刷 4 条 error 日志。
"""

from __future__ import annotations

import pytest

from src.intraday.board import BoardContextProvider, match_board_name

#: 同花顺官方概念名（真实子集）
THS_NAMES = ["PCB概念", "PET铜箔", "共封装光学(CPO)", "先进封装", "AI PC",
             "光伏概念", "锂电池", "存储芯片"]


@pytest.mark.parametrize("query", ["覆铜板", "印制电路板", "锂电铜箔", "电子铜箔"])
def test_unlisted_concept_is_not_matched(query: str) -> None:
    """这四个名字在同花顺官方列表里都不存在 —— 匹配必须是 None。"""
    assert match_board_name(query, THS_NAMES) is None


@pytest.mark.parametrize("query", ["覆铜板", "印制电路板"])
def test_resolve_returns_empty_for_unlisted_concept(query: str, monkeypatch) -> None:
    """解析不到要返回空串，调用方据此跳过（而不是把原名交给 akshare）。"""
    provider = _bare_provider()
    monkeypatch.setattr(provider, "_ths_concept_names", _async_return(THS_NAMES))
    import asyncio

    assert asyncio.run(provider._resolve_concept_name(query)) == ""  # noqa: SLF001


def test_resolve_falls_back_to_original_when_name_list_unavailable(monkeypatch) -> None:
    """官方列表拿不到（冷却/网络失败）时回落原名 —— 不能把所有板块都跳掉。

    这是刻意的保守取舍：一次列表拉取失败不该让全部板块在 300 秒内失去同花顺
    数据（那比多起一个子进程严重得多）。
    """
    provider = _bare_provider()
    monkeypatch.setattr(provider, "_ths_concept_names", _async_return([]))
    import asyncio

    assert asyncio.run(provider._resolve_concept_name("覆铜板")) == "覆铜板"  # noqa: SLF001


def test_unlisted_concept_does_not_spawn_subprocess(monkeypatch) -> None:
    """关键：列表里没有的名字**不能**进批量目标（否则会崩掉整个批量子进程，

    连带把同批其它正常板块的结果一起丢掉 —— 批量版是"一个子进程查 N 个板块"）。
    """
    provider = _bare_provider()
    monkeypatch.setattr(provider, "_ths_concept_names",
                        _async_return(["PCB概念", "PET铜箔"]))
    calls: list[str] = []

    async def _fake_subproc(body: str, **_kwargs):
        calls.append(body)
        return {"frames": {}}

    import src.intraday.subproc as subproc
    monkeypatch.setattr(subproc, "run_json_subprocess", _fake_subproc)
    import asyncio

    # 只给"列表里没有"的名字 → 根本不该起子进程
    assert asyncio.run(provider.warm_concept_snapshots(["覆铜板"])) == {}
    assert calls == []

    # 混合：只有官方名进目标列表
    asyncio.run(provider.warm_concept_snapshots(["覆铜板", "PCB概念"]))
    assert len(calls) == 1
    assert "PCB概念" in calls[0]
    assert "覆铜板" not in calls[0]


def test_resolve_caches_result(monkeypatch) -> None:
    provider = _bare_provider()
    provider._ths_names = list(THS_NAMES)          # 直接给定列表，避免重复拉取
    import asyncio

    first = asyncio.run(provider._resolve_concept_name("PCB"))       # noqa: SLF001
    second = asyncio.run(provider._resolve_concept_name("PCB"))      # noqa: SLF001
    assert first == "PCB概念"
    assert second == "PCB概念"


# ---------------------------------------------------------------- 工具

def _bare_provider() -> BoardContextProvider:
    """只带解析所需字段的实例（跳过配置/数据源装配）。"""
    provider = BoardContextProvider.__new__(BoardContextProvider)
    provider._concept_name_cache = {}
    provider._ths_names = None
    provider._ths_names_cooldown_until = 0.0
    return provider


def _async_return(value):
    async def _inner(*_args, **_kwargs):
        return value
    return _inner
