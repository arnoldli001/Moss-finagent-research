"""题材准入（`src.mainline.theme_gate`）单元测试。

## 这里盯住的是三类**静默**故障

它们都不会抛异常，界面上也照样出数：

1. **把"判不出"当成"不达标"**。一条 20 日窗口都没走满的新题材、或只走过
   1~2 个窗口的题材，胜率是抛硬币级别的噪声。若一起剔掉，系统**永远看不到
   刚冒头的新题材**，而"抓新主线"正是这个模块存在的理由。
   `test_small_sample_goes_to_watching` / `test_no_history_goes_to_watching`。
2. **边界方向写反**。判据是"胜率 ≤ 门槛才剔"，而保留侧是"**严格大于**门槛"。
   写成 `<` 会让恰好等于门槛的题材漏回来，而且不报错。
   `test_threshold_boundary`。
3. **台账文件写坏**。这份文件是 `boards()` 唯一读的东西，一行 YAML 写错
   （比如题材名里带引号）会让**整个剔除清单静默失效** —— 加载器会当成
   "读不到"从而不剔除任何题材。`test_rendered_file_round_trips`。
"""

from __future__ import annotations

import yaml

from src.mainline import theme_gate


def board(name: str = "创新药", *, rate: float | None = 0.2, done: int = 5,
          code: str = "886015.TI", signals: int = 5, alerts: int = 8,
          avg_ret: float | None = -0.5) -> dict:
    """造一条 `summarize_boards()` 形状的汇总。"""
    return {"board_code": code, "board_name": name, "win_rate_20d": rate,
            "done_20d": done, "signals": signals, "alert_total": alerts,
            "avg_ret_20d": avg_ret}


# ==================================================================
# 判据
# ==================================================================


def test_bad_theme_with_enough_samples_is_excluded() -> None:
    excluded, watching = theme_gate.select([board()])
    assert [item["board_name"] for item in excluded] == ["创新药"]
    assert watching == []


def test_good_theme_is_neither() -> None:
    excluded, watching = theme_gate.select([board(rate=0.55, done=10)])
    assert excluded == [] and watching == []


def test_small_sample_goes_to_watching() -> None:
    """**回归测试**：样本不足的题材留观察，不剔除。

    只走过 1~2 个窗口时胜率是噪声（本机那 8 个这样的题材加起来只有
    11 个信号周期 / 14 条告警，几乎不产生噪音），剔掉它们等于用抛硬币的结果
    永久关掉一个题材。
    """
    excluded, watching = theme_gate.select([board(rate=0.0, done=2)])
    assert excluded == []
    assert len(watching) == 1
    assert "少于 3 个" in theme_gate.watch_reason(watching[0], 3)


def test_no_history_goes_to_watching() -> None:
    """**回归测试**：一条 20 日窗口都没走满的新题材不能剔。"""
    excluded, watching = theme_gate.select(
        [board(name="AI智能体", rate=None, done=0, signals=0, alerts=0)])
    assert excluded == []
    assert "判不出来" in theme_gate.watch_reason(watching[0], 3)


def test_threshold_boundary() -> None:
    """**回归测试**：恰好等于门槛 → 剔除；比门槛高一点点 → 保留。"""
    at, _ = theme_gate.select([board(rate=0.4, done=9)])
    assert len(at) == 1, "恰好等于门槛算不达标"
    over, _ = theme_gate.select([board(rate=0.41, done=9)])
    assert over == []


def test_min_samples_is_configurable() -> None:
    excluded, watching = theme_gate.select([board(rate=0.1, done=5)],
                                           min_samples=8)
    assert excluded == [] and len(watching) == 1
    excluded2, _ = theme_gate.select([board(rate=0.1, done=8)], min_samples=8)
    assert len(excluded2) == 1


def test_excluded_sorted_worst_first() -> None:
    """台账要让人先看到最该剔的：按胜率升序、同胜率按窗口数降序。"""
    excluded, _ = theme_gate.select([
        board(name="A", rate=0.38, done=8),
        board(name="B", rate=0.0, done=6),
        board(name="C", rate=0.38, done=13),
    ])
    assert [item["board_name"] for item in excluded] == ["B", "C", "A"]


# ==================================================================
# 台账文件：必须能被加载器读回去
# ==================================================================


def test_rendered_file_round_trips() -> None:
    """**回归测试**：生成的文件必须能被 YAML 解析、且 `codes` 恰好是那批代码。

    这份文件是 `boards()` 唯一读的东西。写坏了（比如题材名里带引号没转义）
    会让解析整体失败 → 加载器当成"清单不可用" → **一个题材都不剔**，
    而生成脚本照样打印"✅ 已写入"。
    """
    excluded, watching = theme_gate.select([
        board(name='带"引号"的题材', rate=0.1, done=5, code="886999.TI"),
        board(name="正常题材", rate=0.2, done=4, code="886998.TI"),
        board(name="样本不足", rate=0.0, done=1, code="886997.TI"),
    ])
    text = theme_gate.render(excluded, watching, threshold=0.4, min_samples=3,
                             pool_size=138, as_of="20260924",
                             levels=theme_gate.DEFAULT_LEVELS,
                             now="2026-09-25T21:00:00+08:00")
    raw = yaml.safe_load(text)
    assert raw["version"] == theme_gate.FORMAT_VERSION
    assert raw["rule"]["threshold"] == 0.4
    assert raw["rule"]["min_samples"] == 3
    assert raw["counts"] == {"pool": 138, "excluded": 2, "kept": 136,
                             "watching": 1}
    codes = [item["code"] for item in raw["codes"]]
    assert codes == ["886999.TI", "886998.TI"]        # 胜率升序
    assert raw["codes"][0]["name"] == '带"引号"的题材'  # 引号被正确转义
    assert [item["code"] for item in raw["watching"]] == ["886997.TI"]


def test_render_with_nothing_excluded_is_valid_yaml() -> None:
    """一个题材都没被剔时也必须写出合法 YAML（`codes: []`，不是半截键）。"""
    text = theme_gate.render([], [board()], threshold=0.4, min_samples=3,
                             pool_size=138, as_of="20260924",
                             levels=theme_gate.DEFAULT_LEVELS,
                             now="2026-09-25T21:00:00+08:00")
    raw = yaml.safe_load(text)
    assert raw["codes"] == []
    assert raw["counts"]["excluded"] == 0
    assert len(raw["watching"]) == 1


def test_default_levels_cover_all_three() -> None:
    """池级取舍看**全部档位**，不只面板的 strong/medium。

    面板只追踪强/中信号是"展示范围"的选择；而"要不要继续挖这个题材"要看它
    产生过的全部信号（含弱信号）—— 只会反复报弱信号且从不兑现的题材同样占位置。
    改这里等于改口径，必须显式。
    """
    assert theme_gate.DEFAULT_LEVELS == ("strong", "medium", "weak")
    assert theme_gate.DEFAULT_THRESHOLD == 0.4
    assert theme_gate.DEFAULT_MIN_SAMPLES == 3
