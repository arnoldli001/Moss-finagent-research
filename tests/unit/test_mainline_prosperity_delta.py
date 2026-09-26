"""景气度「增速变化率」口径（V3）的单元测试。

## V3 是什么

景气度**低于** `six_dim.prosperity_delta_below`（默认 30 分）的板块，
改用 `Δg = g(t) − g(t−W)` 的横截面分位来算景气度；其余板块仍用水平值 `g`。

## 为什么必须限定范围（这批测试的由来）

全局替换在留出窗口把误报率从 6.30 抬到 **8.44**，而只换低景气组的 29 个板块
是 **6.20**。候选池每天固定 64 个名额，作用范围越大、被挤掉的真信号越多。
所以"只在低景气时启用"这件事**是功能的一部分**，必须被测试钉住 ——
一个不小心把它改成全局，指标会静默变差。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from src.mainline.config import load_config
from src.mainline.models import BoardInfo, BoardKind
from src.mainline.six_dim import BoardInput, score_six_dim


def _board(index: int, *, roe: float, profit: float, revenue: float
           ) -> BoardInput:
    code = f"6000{index:02d}"
    stats = {code: {"roe_yoy": roe, "netprofit_yoy": profit,
                    "or_yoy": revenue, "circ_mv": 5.0e9}}
    return BoardInput(
        info=BoardInfo(code=f"8859{index:02d}.TI", name=f"板块{index}",
                       kind=BoardKind.CONCEPT),
        members=[code], member_stats=stats, circ_mv=5.0e9)


def _mixed_boards() -> list[BoardInput]:
    """40 个板块、**连续分布**的景气度：从很差到很好。

    ⚠️ 不能用"20 个一样的差 + 20 个一样的好"：分位是 `≤` 占比，
    并列值会全部落到 50%，于是"低于 30 分"一个都不成立，测试变成空转
    （第一版就是这么写的，白白失败了一次）。
    """
    boards = []
    for index in range(40):
        # 0..19 → 越来越差（-120 到 -6）；20..39 → 越来越好（6 到 120）
        value = -120.0 + index * 6.0 if index < 20 else (index - 20) * 6.0 + 6.0
        boards.append(_board(index, roe=value, profit=value, revenue=value))
    return boards


def _improving_history() -> dict[str, dict[str, float]]:
    """差板块 60 天前更差（Δg 为正），好板块 60 天前更好（Δg 为负）。

    ⚠️ 键必须是**板块代码**（`score_six_dim` 按 `item.code` 查，`service`
    也是按 `board_code` 传的）。第一版这里写成了股票代码 `6000xx`，
    于是每个 Δg 都是 None、一个板块都没换 —— 而**没有任何报错**。
    这正是本项目反复遇到的"静默失效"，所以实现侧也加了一条告警探测。
    """
    out = {}
    for index in range(40):
        board = f"8859{index:02d}.TI"
        if index < 20:
            out[board] = {"roe_yoy": -95.0, "profit_yoy": -95.0,
                          "or_yoy": -95.0}
        else:
            out[board] = {"roe_yoy": 60.0, "profit_yoy": 60.0, "or_yoy": 60.0}
    return out


def _prosperity(payload_boards: list[BoardInput], *, history
                ) -> dict[str, float]:
    config = load_config(force=True)
    scored = score_six_dim(payload_boards, config=config, history_raws=history)
    out = {}
    for code, (layer, _raw) in scored.items():
        dim = next(d for d in layer.dimensions if d.key == "prosperity")
        out[code] = dim.score
    return out


def test_history_is_strictly_point_in_time(tmp_path: Path) -> None:
    """`prosperity_raw_history_sync` 必须取 `t−W` 那一天，且**不含当日**。

    ⚠️ 与突破触发那次同一个坑：方法必须放在**结果库**仓储上，
    放到 `MainlineDataStore`（cache 库、没有 `mainline_score` 表）会抛
    "no such table"，而调用方把它吞成"历史为空" → 功能静默失效。
    """
    from src.mainline.storage import MainlineRepository

    db = tmp_path / "results.db"
    connection = sqlite3.connect(db)
    connection.execute(
        "CREATE TABLE mainline_score"
        "(trade_date TEXT, board_code TEXT, payload TEXT)")
    rows = []
    for index in range(8):
        payload = json.dumps({"layers": [{"key": "six_dim", "dimensions": [
            {"key": "prosperity", "available": True,
             "raw": {"roe_yoy": float(index), "profit_yoy": float(index * 2),
                     "revenue_yoy": float(index * 3)}}]}]})
        rows.append((f"2026010{index + 1}", "A", payload))
    connection.executemany("INSERT INTO mainline_score VALUES(?,?,?)", rows)
    connection.commit()
    connection.close()

    repo = MainlineRepository(path=str(db))
    # 当日 = 20260108（index=7）；W=3 → 应取 20260105（index=4）
    history = repo.prosperity_raw_history_sync("20260108", lookback_days=3)
    assert history["A"]["roe_yoy"] == pytest.approx(4.0)
    assert history["A"]["profit_yoy"] == pytest.approx(8.0)
    # 历史不足 → 空字典（调用方回退水平值），不能抛
    assert repo.prosperity_raw_history_sync("20260102", lookback_days=30) == {}


def test_low_prosperity_boards_switch_to_delta() -> None:
    """低景气板块必须切到 Δg 口径，且在 note 里留痕。"""
    boards = _mixed_boards()
    config = load_config(force=True)
    scored = score_six_dim(boards, config=config,
                           history_raws=_improving_history())
    switched = {code for code, (layer, _raw) in scored.items()
                if any(d.key == "prosperity" and "增速变化率" in d.note
                       for d in layer.dimensions)}
    assert switched, "没有任何板块切到变化率口径 —— 开关没生效"
    # 换过去的必须是**当前景气度最低**的那批（阈值 30 分）
    worst = {"885900.TI", "885901.TI", "885902.TI", "885903.TI"}
    assert worst <= switched, f"最差的几个没换：{worst - switched}"


def test_high_prosperity_boards_keep_the_level() -> None:
    """景气度高的板块**不能**被换 —— 换了就是全局替换，留出会变差。"""
    boards = _mixed_boards()
    config = load_config(force=True)
    scored = score_six_dim(boards, config=config,
                           history_raws=_improving_history())
    switched = {code for code, (layer, _raw) in scored.items()
                if any(d.key == "prosperity" and "增速变化率" in d.note
                       for d in layer.dimensions)}
    # 好板块（index>=20）一个都不能在里面
    good = {f"8859{index:02d}.TI" for index in range(20, 40)}
    assert not (switched & good), f"高景气板块被误换：{switched & good}"
    # 差板块应当被换（否则这条测试也是空转）
    bad = {f"8859{index:02d}.TI" for index in range(20)}
    assert switched & bad, "低景气板块没有被换"


def test_disabled_when_threshold_is_zero() -> None:
    """`prosperity_delta_below <= 0` = 关闭；此时给不给历史都不该换。"""
    boards = _mixed_boards()
    config = load_config(force=True)
    config.six_dim.prosperity_delta_below = 0.0
    scored = score_six_dim(boards, config=config,
                           history_raws=_improving_history())
    notes = [d.note for _code, (layer, _raw) in scored.items()
             for d in layer.dimensions if d.key == "prosperity"]
    assert all("增速变化率" not in note for note in notes)


def test_no_history_falls_back_to_level() -> None:
    """没有历史（重打分刚开始那几十天）必须安静回退，不能报错也不能换。"""
    boards = _mixed_boards()
    config = load_config(force=True)
    scored = score_six_dim(boards, config=config, history_raws=None)
    notes = [d.note for _code, (layer, _raw) in scored.items()
             for d in layer.dimensions if d.key == "prosperity"]
    assert all("增速变化率" not in note for note in notes)
    assert len(notes) == 40


def test_config_defaults_are_wired_from_yaml() -> None:
    """`prosperity_delta_below` / `_window` 必须真的从 YAML 读到。

    ⚠️ 配置加载里 `SixDimConfig(...)` 是**整体替换**，漏传的字段会静默
    退回 dataclass 默认值（`reverse_dims` 就这样被忽略过，只是默认值恰好
    与 YAML 相同才没暴露）。
    """
    config = load_config(force=True)
    assert config.six_dim.prosperity_delta_below == 30.0
    assert config.six_dim.prosperity_delta_window == 60
    assert config.six_dim.reverse_dims == ("trading", "technical")
