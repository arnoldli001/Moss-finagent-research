"""做T信号的**当日通知节流**：冷却窗口 + 每票每日上限。

## 为什么必须单独做这件事（2026-09-25 用户报障）

> "昨天盘中的邮件提醒也是做T辅助重复刷屏，要修改成个股股价触及当日冲高线
>   或回踩线才通知，不是一直循环刷信息。"

引擎的"触及"判定是一个**状态**，不是**事件**（`engine.decide_signal`）：

    价格 ≤ 回踩线×(1+触及带宽)  →  triggered=True
    价格 ≥ 冲高线×(1-触及带宽)  →  triggered=True

于是只要价格**停在带内**，自选池每分钟重算一次就重新判一次 `triggered=True`。
原代码只有 `cooldown_minutes`（30 分钟）这一个节流阀，而且它**只活在进程内存里**
（`SignalNotifier._last_push`）—— 进程一重启就整个清空，于是重启后又刷一轮。

## 用户最终口径（2026-09-25，第二次收敛）

> "可以设置成发出一次通知就静默半小时，半小时后再检测，
>   一天最多单个票发4次。"

所以是**两道闸门**，不是"一天一次"：

| 闸门 | 作用 | 默认 |
|---|---|---|
| 冷却窗口 | 通知后静默这么久，期间再怎么重算都不发 | 30 分钟 |
| 每日上限 | **每只票**一天最多通知几次（所有方向合计） | 4 次 |

外加一条**重新武装**（防止"趴在线上一整天，每 30 分钟机械地发一封"）：
价格没离开过触发行 `rearm_pct` 以上，就不再重复通知 —— 因为那不是新的触及，
只是同一波行情还在原地。默认 `rearm_pct=0`（关闭），这样就严格等于
"冷却 30 分钟 + 一天 4 次"，与用户口径逐字一致。

## ⚠️ 每日上限为什么按**票**计而不是按（票, 方向）计

若按方向分桶计数，一只票"回踩 4 次 + 冲高 4 次"就是 **8 封**，
而用户说的是"单个票一天最多 4 次"。所以计数维度是 `date|code`，
上限也按它判 —— 回踩与冲高**共享**这 4 次配额。

## 为什么落盘而不是只放内存

内存节流在进程重启后失效（实测正是刷屏的成因之一），而"今天已经发过几次"
必须跨重启成立。按交易日分桶，只保留最近几天（`keep_days`），文件不会无限增长。
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from src.core.errors import BRIEF_TIGHT, brief
from src.intraday import hot_cache

logger = logging.getLogger(__name__)

#: 落盘位置。与自选池热缓存同目录（`data/cache/intraday/`），
#: 因为它的键就是"自选池里某只票今天通知过几次"，同生命周期。
#:
#: ⚠️ 这里锚定 `hot_cache.DEFAULT_CACHE_DIR` 而不是自己写一遍路径：
#: 该目录若改道，两份缓存必须一起走，否则会出现"快照换了目录、
#: 节流记录还留在老地方"的分家（表现为重启后又刷一轮）。
DEFAULT_PATH = Path(hot_cache.DEFAULT_CACHE_DIR) / "notified_signals.json"

#: 保留几个交易日。多留几天是为了跨周末后仍能正确判"这是新的一天"，
#: 同时不让文件无限长。
DEFAULT_KEEP_DAYS = 5

#: 重新武装阈值（%）的**兜底**值。`0` = 不重新武装。
#:
#: 实际值取自 `levels.notify_rearm_pct`（见 `SignalNotifier._rearm_pct`）。
#: 默认 0 的含义：价格只要还在触发行附近，就不算"新的触及"，
#: 于是行为严格等于"冷却 30 分钟 + 一天最多 4 次"。
#:
#: ⚠️ 若用户把它调大，必须**大于** `levels.touch_band_pct`（默认 0.3%）——
#: 那个定义"多近算触及"，重新武装阈值比它还小的话，价格刚出带就被重新武装，
#: 等于没有去重，贴着线抖动照样刷屏。
DEFAULT_REARM_PCT = 0.0

#: 每只票每日通知上限的**兜底**值（配置项 `notify.daily_max_per_code`）。
DEFAULT_DAILY_MAX = 4

_SCHEMA_VERSION = 2

_lock = threading.Lock()
_state: dict[str, Any] = {"path": None, "days": {}}


@dataclass(frozen=True)
class Decision:
    """能不能通知 → (是否允许, 人话原因)。

    `reason` 为空串表示允许。文案会原样进 `NotifyResult.detail`，
    所以必须是用户看得懂的话（"冷却中，还有 12 分钟"而不是 `cooldown_hit`）。
    """

    allowed: bool
    reason: str = ""

    def __bool__(self) -> bool:      # 允许写成 `if decision:`
        return self.allowed


def _path_of(path: str | Path | None) -> Path:
    """归一成 `Path`。

    ⚠️ 必须容错 `str`：所有 `path=` 入参的公开签名都写成 `str | Path`，
    而下游 `_save` 要用 `Path.with_suffix`。实测踩过 —— 传字符串时
    报 `'str' object has no attribute 'with_suffix'`，而那是在**落盘**时才炸，
    表现为"通知发出去了但节流记录没写上"，很难定位。
    """
    return Path(path) if path is not None else DEFAULT_PATH


def _load(path: Path) -> dict[str, dict[str, Any]]:
    """读落盘状态。**任何读取失败都退化成"没有记录"**（fail-open）。

    为什么 fail-open 而不是 fail-closed：读不到记录时若判成"已通知过"，
    用户会**永久收不到**任何通知（比多收一封严重得多）。多收一封只是噪音，
    收不到是功能失效。
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as exc:  # noqa: BLE001 文件损坏/权限问题都不该让推送挂掉
        logger.warning("当日通知节流记录不可读（按无记录处理）：%s",
                       brief(exc, BRIEF_TIGHT))
        return {}
    days = raw.get("days") if isinstance(raw, dict) else None
    return days if isinstance(days, dict) else {}


def _save(path: Path, days: dict[str, dict[str, Any]],
          *, keep_days: int = DEFAULT_KEEP_DAYS) -> None:
    """原子落盘（临时文件 + `os.replace`），并裁掉过期的日子。"""
    keep = sorted(days.keys())[-max(1, keep_days):]
    trimmed = {day: days[day] for day in keep}
    payload = {"version": _SCHEMA_VERSION, "days": trimmed}
    tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)          # 原子替换：读方永远看到完整文件
    except Exception as exc:  # noqa: BLE001 落盘失败不该影响推送本身
        logger.warning("当日通知节流记录落盘失败：%s", brief(exc, BRIEF_TIGHT))
        tmp.unlink(missing_ok=True)


def _days(path: str | Path | None = None) -> dict[str, dict[str, Any]]:
    """进程内缓存的当日记录（首次访问时从磁盘载入）。"""
    resolved = _path_of(path)
    if _state["path"] != str(resolved):
        _state["path"] = str(resolved)
        _state["days"] = _load(resolved)
    return _state["days"]


def _record(days: dict[str, dict[str, Any]], trade_date: str, code: str
            ) -> dict[str, Any] | None:
    """取某票某日的记录（不存在返回 None）。

    键是 `date|code` —— **不含方向**，因为每日上限按票计（见模块 docstring）。
    """
    day = days.get(trade_date) or {}
    entry = day.get(code)
    return entry if isinstance(entry, dict) else None


def _count_of(entry: dict[str, Any] | None) -> int:
    if not entry:
        return 0
    try:
        return max(0, int(entry.get("count") or 0))
    except (TypeError, ValueError):
        return 0


def reset(path: str | Path | None = None) -> None:
    """清空进程内缓存（测试用；不动磁盘）。"""
    with _lock:
        _state["path"] = None
        _state["days"] = {}


def check(
    *, trade_date: str, code: str, kind: str, price: float | None = None,
    cooldown_minutes: float = 30.0, daily_max: int = DEFAULT_DAILY_MAX,
    rearm_pct: float = DEFAULT_REARM_PCT, path: str | Path | None = None,
) -> Decision:
    """这道触及能不能通知？按"最硬到最软"的顺序判：

    1. **每日上限**（按票）—— 到了就直接静默到明天，冷却再久也没用；
    2. **冷却窗口** —— 距上次通知不足 `cooldown_minutes` 就不发；
    3. **重新武装**（`rearm_pct > 0` 时才生效）—— 价格没离开触发行足够远，
       说明还是同一波行情，不重复通知。

    判据不全（没有交易日/代码）时放行 —— 拦错了等于功能静默失效。
    """
    if not trade_date or not code:
        return Decision(True)
    with _lock:
        entry = _record(_days(path), trade_date, code)
        count = _count_of(entry)
        if count:
            if daily_max > 0 and count >= int(daily_max):
                return Decision(False, (
                    f"今日已通知 {count} 次（上限 {int(daily_max)} 次）—— "
                    "该票今日不再推送"))
            if cooldown_minutes and cooldown_minutes > 0:
                remain = _cooldown_left(entry, float(cooldown_minutes))
                if remain is not None:
                    return Decision(False, (
                        f"冷却中（距上次通知不足 {cooldown_minutes:g} 分钟，"
                        f"还需约 {remain:.0f} 分钟）"))
            if rearm_pct > 0:
                last = entry.get("last_price")
                if isinstance(last, (int, float)) and last:
                    try:
                        drift = abs(float(price) - float(last)) / float(last) * 100.0
                    except (TypeError, ValueError, ZeroDivisionError):
                        drift = None
                    if drift is not None and drift < float(rearm_pct):
                        return Decision(False, (
                            f"价格仍在同一档位区间内（距上次通知价仅 "
                            f"{drift:.2f}%，未达重新武装 {rearm_pct:g}%）"))
    return Decision(True)


def _cooldown_left(entry: dict[str, Any], cooldown_minutes: float) -> float | None:
    """距上次通知还差多少分钟；已过冷却期或读不到时间戳时返回 None。"""
    stamp = str(entry.get("last_at") or "")
    if not stamp:
        return None
    try:
        last = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    elapsed = (datetime.now() - last).total_seconds() / 60.0
    return None if elapsed >= cooldown_minutes else cooldown_minutes - elapsed


def mark_notified(
    *, trade_date: str, code: str, kind: str, price: float | None = None,
    path: str | Path | None = None,
) -> None:
    """记一次通知：计数 +1、刷新时间戳与价格，并立即落盘。

    同时记录 `kinds`（今天通知过哪些方向）—— 它不参与判定，
    只为了让"今天为什么只发了 3 次"这类问题在文件里可查。
    """
    if not trade_date or not code:
        return
    with _lock:
        days = _days(path)
        day = days.setdefault(trade_date, {})
        entry = day.get(code)
        if not isinstance(entry, dict):
            entry = {"count": 0, "kinds": []}
            day[code] = entry
        entry["count"] = _count_of(entry) + 1
        entry["last_at"] = _now()
        entry["last_price"] = None if price is None else round(float(price), 4)
        entry["last_kind"] = str(kind or "")
        kinds = entry.get("kinds")
        if not isinstance(kinds, list):
            kinds = []
            entry["kinds"] = kinds
        if kind and kind not in kinds:
            kinds.append(str(kind))
        _save(_path_of(path), days)


def record_of(*, trade_date: str, code: str,
              path: str | Path | None = None) -> dict[str, Any]:
    """某票某日的节流记录（供状态接口/排查用）；没有则返回空计数。"""
    with _lock:
        entry = _record(_days(path), trade_date, code)
        if not entry:
            return {"count": 0, "last_at": "", "last_price": None, "kinds": []}
        return {
            "count": _count_of(entry),
            "last_at": str(entry.get("last_at") or ""),
            "last_price": entry.get("last_price"),
            "kinds": list(entry.get("kinds") or []),
        }


def notified_count(*, trade_date: str, path: str | Path | None = None) -> int:
    """某交易日**有通知记录的票数**（不是通知总次数）。"""
    with _lock:
        return len(_days(path).get(trade_date) or {})


def _now() -> str:
    """记录时间戳用的"现在"。

    ★ 单独抽成函数是为了**可注入**：冷却窗口是时间相关的行为，
    测试里既要能造出"31 分钟前通知过"，又不能真的 sleep 半小时。
    测试通过 monkeypatch 本函数（或 `datetime`）来控制时间。
    """
    return datetime.now().isoformat(timespec="seconds")


__all__ = [
    "DEFAULT_DAILY_MAX",
    "DEFAULT_KEEP_DAYS",
    "DEFAULT_PATH",
    "DEFAULT_REARM_PCT",
    "Decision",
    "check",
    "mark_notified",
    "notified_count",
    "record_of",
    "reset",
]
