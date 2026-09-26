"""两个主线接口共用一个筛选口径的契约测试。

## 这里盯住的是"同一个数在两个页签里不一样"

`/alert-returns`（回测报告 → 回测收益展示）与 `/board-win-rates`
（告警流水 → 隐藏不达标板块的强/中告警）都按 **20 日胜率**决定显示什么。

如果哪个页签自己再算一遍，两个地方就会各有一份"20 日胜率"：口径一旦漂移，
界面上照样出数、不会报错，只是同一句话在两个页签里给出不同的板块 ——
这类故障只有测试能守住。所以这里直接钉住两件事：

1. 两个接口**命中同一把缓存键**（第二次调用不该再算一遍）；
2. `board-win-rates` 只做投影：一条行数据都不带出去。

用替身而不是真实仓储：这条契约关于**路由层怎么拼参数**，
跑真实数据只会让测试变慢、并在没有本地数据仓的机器上失败。
"""

from __future__ import annotations

import asyncio

from src.api.routes import mainline as route
from src.mainline import alert_returns


class FakeRepo:
    """只实现路由用到的那一个方法。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def load_alerts(self, **kwargs) -> list[dict]:
        self.calls.append(kwargs)
        return [{"trade_date": "20260105", "board_code": "886042.TI",
                 "board_name": "存储芯片", "level": "strong", "score": 80.0,
                 "entry_close": 100.0,
                 "payload": {"level_label": "🔴 强信号"}}]


def run(coro):
    return asyncio.run(coro)


def test_two_endpoints_share_one_cache_key(monkeypatch) -> None:
    """**回归测试**：`board-win-rates` 与 `alert-returns` 必须共用一份结果。

    两个处理函数的默认参数只要有一项对不上（例如一边写 `split="20260825"`、
    另一边用模块常量拼出别的值），缓存键就会分叉：不报错，只是每个页签各算一遍，
    而且**两边的口径从此可以各自演化**。所以断言"只算了一次"。
    """
    builds: list[dict] = []

    def fake_build(alerts, store, **kwargs):
        builds.append(kwargs)
        return {
            "as_of": "20260105", "generated_at": "2026-01-05T18:00:00+08:00",
            "min_win_rate": kwargs.get("min_win_rate", 0.5),
            "boards": [
                {"board_code": "886042.TI", "board_name": "存储芯片",
                 "signals": 3, "done_20d": 3, "win_rate_20d": 0.667,
                 "gate": "pass", "passed": True, "avg_ret_20d": 8.0},
                {"board_code": "885908.TI", "board_name": "创新药",
                 "signals": 4, "done_20d": 4, "win_rate_20d": 0.25,
                 "gate": "hidden", "passed": False, "avg_ret_20d": 1.2},
            ],
            "rows": [], "stats": {}, "gaps": [],
        }

    repo = FakeRepo()
    monkeypatch.setattr(route, "_repo", lambda: repo)
    monkeypatch.setattr(route, "_store", lambda: object())
    monkeypatch.setattr(alert_returns, "build", fake_build)
    alert_returns.cache_clear()

    gate = run(route.board_win_rates(min_win_rate=0.5, refresh=False))
    assert gate["hidden"] == 1 and "rows" not in gate
    # 面板的默认参数 = 前端 `alertReturns({leaders:false})` 实际发出去的那一组
    panel = run(route.alert_returns_panel(
        start="", end="", levels="strong,medium", split="20260825", dedup_days=10,
        horizons="7,20,60", leaders=False, stock_window=20, top_leaders=3,
        min_win_rate=0.5, history_only=False, limit=0, refresh=False))

    assert len(builds) == 1, (
        "两个接口没有命中同一把缓存键：默认参数分叉了 —— 口径会从这里开始各走一路")
    assert panel.get("cached") is True
    alert_returns.cache_clear()


def test_board_win_rates_projects_only_the_gate(monkeypatch) -> None:
    """`board-win-rates` 是投影：只给"隐不隐藏"，不把行数据带出去。"""
    def fake_build(alerts, store, **kwargs):
        return {
            "as_of": "20260105", "generated_at": "2026-01-05T18:00:00+08:00",
            "boards": [
                {"board_code": "886042.TI", "board_name": "存储芯片",
                 "signals": 3, "done_20d": 3, "win_rate_20d": 0.667,
                 "gate": "pass", "passed": True, "avg_ret_20d": 8.0,
                 "leaders": [{"code": "000001"}]},
                {"board_code": "885908.TI", "board_name": "创新药",
                 "signals": 4, "done_20d": 4, "win_rate_20d": 0.25,
                 "gate": "hidden", "passed": False, "avg_ret_20d": 1.2},
                {"board_code": "886100.TI", "board_name": "刚出信号的板块",
                 "signals": 1, "done_20d": 0, "win_rate_20d": None,
                 "gate": "pending", "passed": True, "avg_ret_20d": None},
            ],
            "rows": [{"board_code": "886042.TI"}], "stats": {},
            # "没请求领涨股"这条缺口与本接口无关，必须滤掉
            "gaps": ["领涨成分股列为空是因为本次没有请求它（`leaders=true` 才会计算）",
                     "1 个板块在本地没有指数日线"],
        }

    monkeypatch.setattr(route, "_repo", lambda: FakeRepo())
    monkeypatch.setattr(route, "_store", lambda: object())
    monkeypatch.setattr(alert_returns, "build", fake_build)
    alert_returns.cache_clear()

    payload = run(route.board_win_rates(min_win_rate=0.5, refresh=True))
    alert_returns.cache_clear()

    assert payload["hidden"] == 1
    assert payload["pending"] == 1          # 判不出来的单独计数，前端要写明"不隐藏"
    assert payload["min_win_rate"] == 0.5
    assert payload["levels"] == ["strong", "medium"]
    assert [item["board_code"] for item in payload["boards"]] == \
        ["886042.TI", "885908.TI", "886100.TI"]
    # 投影后的每一项都必须是标量：带出行/领涨股会让这个接口悄悄变成第二个面板接口
    for item in payload["boards"]:
        assert set(item) == {"board_code", "board_name", "signals", "done_20d",
                             "win_rate_20d", "gate", "hidden"}
    assert payload["gaps"] == ["1 个板块在本地没有指数日线"]
