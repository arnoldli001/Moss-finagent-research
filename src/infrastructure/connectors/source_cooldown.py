"""外部数据源的失败冷却（negative cache）。

## 为什么需要它（2026-09-26 实测）

投研分析里最慢的一个指标是 ``fed:rate_prob:next``（CME FedWatch）：

    指标                       结果      耗时
    fed:rate_prob:next         0 点     23.47 s   <== 每次必然失败，却每次都等满
    mkt:cybkcb:spot_summary    1 点     13.41 s   <== 东财 clist 分页 ~16 次请求

`fed:rate_prob:next` 依赖 CME/FRED **外网**，本机/内网不可达时
``FedWatchConnector.fetch`` 会 ``asyncio.wait_for(..., timeout=25)``：
**既然它必然失败，那 23.5 秒是纯浪费** —— 而且因为 A01 用 ``asyncio.gather``
并发采集，整条数据采集阶段的墙钟时间就被这一个指标拖到 23.5 秒。

更糟的是它**没有记忆**：同一次分析里被问一次、下一个用户再问一次，
每次都重新等满 23.5 秒。

## 做法

对"已知不可用"的源记一个**冷却窗口**：窗口内直接返回空（不发起网络请求），
窗口结束放行一次探测；探测成功立即忘掉冷却（源恢复了就马上用它）。

与 ``intraday/source_health.py`` 的分级冷却同一思路，但更轻量：
这里只需要"记住它坏了多久"，不需要成功率统计与健康度面板
（那套服务的指标是盘中逐秒行情，这里的指标是低频投研数据）。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

#: 首次失败后的冷却时长（秒）。60 秒 ≈ 同一次分析/同一位用户连续追问不会重复付代价，
#: 又短到"源刚恢复"不会被冷落太久。
DEFAULT_COOLDOWN_SEC = 60.0
#: 连续失败时的退避上限（秒）：避免长期不通的源被无限探测。
MAX_COOLDOWN_SEC = 900.0


class SourceCooldown:
    """按 key 记失败冷却（进程内、线程安全）。

    key 约定用 ``"{source}:{indicator}"``：同一个源的**不同指标**可能一个通一个不通
    （例如腾讯有日线但外网 FedWatch 不通），按指标隔离比按源隔离更准确。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        #: key → (冷却截止 monotonic, 连续失败次数)
        self._state: dict[str, tuple[float, int]] = {}

    def is_cooling(self, key: str) -> bool:
        """是否处于冷却中（True = 不要再发请求）。"""
        with self._lock:
            entry = self._state.get(key)
            if entry is None:
                return False
            until, _fails = entry
            if time.monotonic() >= until:
                # 冷却到期：放行一次探测，但保留失败计数用于退避
                return False
            return True

    def remaining(self, key: str) -> float:
        """冷却剩余秒数（未在冷却返回 0）。"""
        with self._lock:
            entry = self._state.get(key)
            if entry is None:
                return 0.0
            return max(0.0, entry[0] - time.monotonic())

    def record_failure(self, key: str, *, cooldown: float = DEFAULT_COOLDOWN_SEC,
                       reason: str = "") -> float:
        """记一次失败，返回本次冷却时长（指数退避，封顶 MAX_COOLDOWN_SEC）。"""
        with self._lock:
            _until, fails = self._state.get(key, (0.0, 0))
            fails += 1
            wait = min(cooldown * fails, MAX_COOLDOWN_SEC)
            self._state[key] = (time.monotonic() + wait, fails)
        logger.info(
            "数据源失败冷却 %s：连续失败 %d 次，冷却 %.0fs%s",
            key, fails, wait, f"（{reason}）" if reason else "")
        return wait

    def record_success(self, key: str) -> bool:
        """记一次成功；如果此前在冷却中则清除。返回是否清除了冷却。"""
        with self._lock:
            had = key in self._state
            self._state.pop(key, None)
        if had:
            logger.info("数据源恢复 %s：已清除失败冷却", key)
        return had

    def snapshot(self) -> dict[str, Any]:
        """当前冷却状态（供 /health 观测）。"""
        with self._lock:
            now = time.monotonic()
            return {
                k: {"remaining_s": round(max(0.0, until - now), 1), "failures": fails}
                for k, (until, fails) in sorted(self._state.items())
                if until > now
            }

    def clear(self) -> None:
        with self._lock:
            self._state.clear()


#: 进程单例
_cooldown: SourceCooldown | None = None
_cooldown_lock = threading.Lock()


def get_cooldown() -> SourceCooldown:
    global _cooldown
    if _cooldown is None:
        with _cooldown_lock:
            if _cooldown is None:
                _cooldown = SourceCooldown()
    return _cooldown


__all__ = [
    "DEFAULT_COOLDOWN_SEC",
    "MAX_COOLDOWN_SEC",
    "SourceCooldown",
    "get_cooldown",
]
