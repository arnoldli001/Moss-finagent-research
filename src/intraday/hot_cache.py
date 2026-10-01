"""做T辅助：**上次关停前的缓存热加载**（重启后前端立刻有数据）。

## 问题（2026-09-17 用户报障）

"每次重启服务时，前端加载界面数据都很慢。"

实测（干净进程，先前的库故障已修）：

| 环节 | 耗时 |
|---|---|
| 进程启动 → 端口可连 | 3.2s |
| 首个 `/api/v1/health` | 7.6s |
| 首个 `/api/v1/intraday/watchlist` | **59.8s** |
| 第二个 `/api/v1/intraday/watchlist` | 0.00s |

即"慢"不在启动，而在**首批业务请求**：自选 26~39 只票要为每只各取一次
快照（报价 + 分钟线 + 板块 + 情绪周期），冷启动时全部要打数据源，
进程内缓存又是空的 —— 前端在拿到这份列表之前只能转圈。

## 做法：把上一轮的概览落盘，启动时"热加载 + 过期即后台重算"

- **写**：每次 `_watch_cache` 更新后，把列表（`WatchItem` 是 pydantic 模型，
  纯叶子字段）序列化成 JSON，原子替换落盘（临时文件 + `os.replace`）；
- **读**：启动时载入并**打上"已过期"时间戳**塞进 `_watch_cache`。这样
  `watchlist()` 的既有分支（`not force and cached is not None` → 立即返回旧值
  并 `_schedule_watchlist_refresh()`）自动生效 —— **首请求 0 秒出数据，
  新鲜度由后台重算补上**，不需要为热加载新写一条返回路径；
- **校验**：自选池改动过（增删票）就丢弃失效条目，只保留仍然存在的代码，
  避免"显示已经删掉的票"；
- **过期**：默认超过 24 小时或交易日跨度太大就不加载（宁可等一次重算，
  也不要展示昨天的价格当作今天的）。

## 为什么不做成"启动时就同步重算一遍"

那样只是把 59.8 秒从"首个请求"搬到"启动阶段"，用户看到的还是一样的等待。
热加载的关键是**先给旧值**，重算放到后台（stale-while-revalidate）。

## 与"诚实数据"原则的关系

旧值一定带时间戳：`WatchItem.quote_ts` 是那一轮取数时刻，前端照常按它显示
新鲜度；后端返回的 `auto_refresh` 等字段不变。**不伪造新鲜度**。
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.core.errors import (
    BRIEF_DEFAULT,
    BRIEF_TIGHT,
    brief,
)

logger = logging.getLogger(__name__)

from src.infrastructure.catalog.data_stores import store_rel  # noqa: E402

#: 快照格式版本：字段语义变化时 +1，旧文件直接丢弃（不做迁移）
SNAPSHOT_VERSION = 1

#: 默认缓存目录（与资金流的 `data/cache/fundflow/` 同级，便于统一清理）
DEFAULT_CACHE_DIR = store_rel("cache_root") + "/intraday"

#: 默认最长可用时长：超过就不加载热缓存（宁可等一次重算，也不展示隔夜旧值）
DEFAULT_MAX_AGE_SECONDS = 24 * 3600


def snapshot_path(cache_dir: str | Path = DEFAULT_CACHE_DIR) -> Path:
    return Path(cache_dir) / "watchlist_snapshot.json"


@dataclass
class SnapshotLoad:
    """热加载结果（供日志/接口/测试断言）。"""

    loaded: bool = False
    items: list[dict[str, Any]] = field(default_factory=list)
    saved_at: float = 0.0
    age_seconds: float = 0.0
    reason: str = ""
    dropped_codes: list[str] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.items)


def _normalize_code(raw: Any) -> str:
    """代码归一：去掉 `.SH/.SZ/.BJ` 等后缀后补足 6 位。

    为什么必须去后缀：配置里写的是 `600150`，而快照/接口偶尔会带
    `600150.SH` —— 直接 `zfill(6)` 对 `600150.SH` 是空操作，于是一只票都
    匹配不上，热加载会被误判成"自选池改动较大"而整体丢弃。
    """
    text = str(raw or "").strip()
    if "." in text:
        text = text.split(".", 1)[0]
    return text.zfill(6)


def save_snapshot(
    items: list[Any], *, cache_dir: str | Path = DEFAULT_CACHE_DIR,
    now: float | None = None,
) -> bool:
    """把自选概览落盘（原子替换）。**任何异常都只记日志，不影响主链路。**

    `items` 是 `WatchItem` 列表或等价的 dict 列表；这里只取叶子字段，
    不带任何句柄/DataFrame（那些不可 JSON 化，也不该跨进程共享）。
    """
    path = snapshot_path(cache_dir)
    payload = {
        "version": SNAPSHOT_VERSION,
        "saved_at": float(now if now is not None else time.time()),
        "count": len(items),
        "items": [_to_plain(item) for item in items],
    }
    tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)          # 原子替换：读方永远看到完整文件
        logger.debug("自选概览快照已落盘：%d 条 → %s", len(items), path)
        return True
    except Exception as exc:  # noqa: BLE001 落盘失败绝不影响请求
        logger.info("自选概览快照落盘失败（不影响服务）：%s", brief(exc, BRIEF_DEFAULT))
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False


def load_snapshot(
    *, cache_dir: str | Path = DEFAULT_CACHE_DIR,
    known_codes: list[str] | None = None,
    max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
    now: float | None = None,
) -> SnapshotLoad:
    """读取热加载快照；不可用时返回 `loaded=False` 并给出原因（不抛异常）。

    Args:
        known_codes: 当前配置里的自选代码。给了就只保留仍然存在的条目
            （自选增删过 → 旧快照里被删掉的票不能继续显示）。
        max_age_seconds: 超过这个时长视为不可用。
    """
    path = snapshot_path(cache_dir)
    current = float(now if now is not None else time.time())
    if not path.exists():
        return SnapshotLoad(reason="没有上次的快照文件")

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 文件坏了就当没有
        return SnapshotLoad(reason=f"快照文件不可读：{brief(exc, BRIEF_TIGHT)}")

    if not isinstance(raw, dict) or raw.get("version") != SNAPSHOT_VERSION:
        return SnapshotLoad(
            reason=f"快照版本不匹配（文件 {raw.get('version') if isinstance(raw, dict) else '?'}"
                   f" ≠ 当前 {SNAPSHOT_VERSION}）")

    saved_at = float(raw.get("saved_at") or 0.0)
    age = max(0.0, current - saved_at) if saved_at else float("inf")
    if max_age_seconds > 0 and age > max_age_seconds:
        return SnapshotLoad(
            saved_at=saved_at, age_seconds=age,
            reason=f"快照过旧（{age / 3600:.1f} 小时 > {max_age_seconds / 3600:.1f} 小时）")

    items = [item for item in (raw.get("items") or []) if isinstance(item, dict)]
    if not items:
        return SnapshotLoad(saved_at=saved_at, age_seconds=age, reason="快照里没有条目")

    dropped: list[str] = []
    if known_codes:
        wanted = {_normalize_code(code) for code in known_codes}
        kept: list[dict[str, Any]] = []
        for item in items:
            code = _normalize_code(item.get("code"))
            if code in wanted:
                kept.append(item)
            else:
                dropped.append(code)
        items = kept
        if not items:
            return SnapshotLoad(saved_at=saved_at, age_seconds=age,
                                dropped_codes=dropped,
                                reason="快照条目与当前自选池无交集（配置改动较大）")

    return SnapshotLoad(
        loaded=True, items=items, saved_at=saved_at, age_seconds=age,
        dropped_codes=dropped,
        reason=f"已热加载 {len(items)} 条（{age / 60:.1f} 分钟前）")


def drop_snapshot(*, cache_dir: str | Path = DEFAULT_CACHE_DIR) -> bool:
    """删除快照（自选池大改时调用，避免下次加载到无关条目）。"""
    path = snapshot_path(cache_dir)
    try:
        path.unlink(missing_ok=True)
        return True
    except OSError:
        return False


# ======================================================================
# 单票**完整快照**的落盘热加载（重启后首屏毫秒级出图）
# ======================================================================
#
# 上面那份是"自选概览"（几十条行摘要）。用户重启后真正盯的是**当前这一只票**
# 的四面板完整快照 —— 它要跑完整取数链（分钟线/分时/日线/板块/估值/消息面），
# 冷进程实测 QMT 在时 6.8 秒、QMT 关闭后 11.1 秒。冷启动首屏就被这一项卡住。
#
# 做法与自选概览同源：把每次算好的完整快照按代码落盘，重启后**第一次**请求
# 先返回这份旧快照（带明确时间戳 + 面板上写明"重启前缓存，正在刷新"），
# 同时后台真算一遍覆盖 —— 首屏从 11 秒降到毫秒级，新鲜度由随后的
# WS 推送/轮询补齐。
#
# 与自选概览的差别（为什么不能照抄字段级校验）：
# - 快照是**深度嵌套**结构（趋势/逐bar档位/打分卡/板块/情绪），不做字段白名单，
#   整份 payload 原样存取；版本号变了就整份丢弃（不做迁移）。
# - 多一道**口径指纹**：权重/阈值/档位/因子参数/自选绑定任一变化，
#   旧快照的分数就不再是当前口径下的分数 —— 必须丢弃，否则用户会看到
#   "改了权重但分数没变"（`configs/intraday.yaml` 是热重载的，很容易踩）。

#: 单票完整快照的缓存格式版本；字段语义变化时 +1，旧文件直接丢弃。
PAYLOAD_VERSION = 1


@dataclass
class PayloadLoad:
    """单票快照热加载结果（供日志/测试断言）。"""

    loaded: bool = False
    payload: dict[str, Any] = field(default_factory=dict)
    saved_at: float = 0.0
    age_seconds: float = 0.0
    trade_date: str = ""
    fingerprint: str = ""
    reason: str = ""


def snapshot_payload_dir(cache_dir: str | Path = DEFAULT_CACHE_DIR) -> Path:
    """单票快照的存放目录（一个代码一个文件，便于按代码失效/清理）。"""
    return Path(cache_dir) / "snapshots"


def snapshot_payload_path(code: Any,
                          cache_dir: str | Path = DEFAULT_CACHE_DIR) -> Path:
    return snapshot_payload_dir(cache_dir) / f"{_normalize_code(code)}.json"


def save_snapshot_payload(
    code: Any, payload: dict[str, Any], *,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
    fingerprint: str = "", trade_date: str = "", now: float | None = None,
) -> bool:
    """把一只票的完整快照落盘（原子替换）。失败只记日志，绝不影响请求。"""
    if not isinstance(payload, dict) or not payload:
        return False
    path = snapshot_payload_path(code, cache_dir)
    envelope = {
        "version": PAYLOAD_VERSION,
        "code": _normalize_code(code),
        "saved_at": float(now if now is not None else time.time()),
        "trade_date": str(trade_date or ""),
        "fingerprint": str(fingerprint or ""),
        "payload": payload,
    }
    tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(envelope, ensure_ascii=False),
                       encoding="utf-8")
        os.replace(tmp, path)          # 原子替换：读方永远看到完整文件
        logger.debug("单票快照已落盘：%s → %s", envelope["code"], path)
        return True
    except Exception as exc:  # noqa: BLE001 落盘失败绝不影响请求
        logger.info("单票快照落盘失败（不影响服务）：%s", brief(exc, BRIEF_DEFAULT))
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False


def load_snapshot_payload(
    code: Any, *,
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
    fingerprint: str | None = None,
    max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
    now: float | None = None,
) -> PayloadLoad:
    """读一只票的完整快照；不可用时 `loaded=False` 并给出原因（不抛异常）。

    Args:
        fingerprint: 当前**口径指纹**。给了就必须与文件里的一致 ——
            不一致说明权重/阈值/档位/自选绑定被改过，旧快照的分数已经
            不是当前口径下的分数，必须丢弃（宁可多等一次真算）。
        max_age_seconds: 超过这个时长视为不可用（默认 24 小时，与自选概览同口径）。
    """
    path = snapshot_payload_path(code, cache_dir)
    current = float(now if now is not None else time.time())
    if not path.exists():
        return PayloadLoad(reason="没有这只票的快照缓存")

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 文件坏了就当没有
        return PayloadLoad(reason=f"快照文件不可读：{brief(exc, BRIEF_TIGHT)}")

    if not isinstance(raw, dict) or raw.get("version") != PAYLOAD_VERSION:
        return PayloadLoad(
            reason=f"快照版本不匹配（文件 "
                   f"{raw.get('version') if isinstance(raw, dict) else '?'}"
                   f" ≠ 当前 {PAYLOAD_VERSION}）")

    saved_at = float(raw.get("saved_at") or 0.0)
    age = max(0.0, current - saved_at) if saved_at else float("inf")
    if max_age_seconds > 0 and age > max_age_seconds:
        return PayloadLoad(
            saved_at=saved_at, age_seconds=age,
            reason=f"快照过旧（{age / 3600:.1f} 小时 > "
                   f"{max_age_seconds / 3600:.1f} 小时）")

    stored = str(raw.get("fingerprint") or "")
    if fingerprint is not None and stored != str(fingerprint):
        return PayloadLoad(
            saved_at=saved_at, age_seconds=age, fingerprint=stored,
            reason="打分口径已变化（权重/阈值/档位/自选绑定被改过），旧快照作废")

    payload = raw.get("payload")
    if not isinstance(payload, dict) or not payload:
        return PayloadLoad(saved_at=saved_at, age_seconds=age,
                           reason="快照里没有内容")

    return PayloadLoad(
        loaded=True, payload=payload, saved_at=saved_at, age_seconds=age,
        trade_date=str(raw.get("trade_date") or ""), fingerprint=stored,
        reason=f"已热加载 {age / 60:.1f} 分钟前的快照")


def drop_snapshot_payload(code: Any, *,
                          cache_dir: str | Path = DEFAULT_CACHE_DIR) -> bool:
    """删除一只票的快照缓存（口径变化/人工清理时用）。"""
    try:
        snapshot_payload_path(code, cache_dir).unlink(missing_ok=True)
        return True
    except OSError:
        return False


def _to_plain(item: Any) -> dict[str, Any]:
    """`WatchItem`（pydantic）或 dict → 纯叶子字段 dict。"""
    if isinstance(item, dict):
        data = item
    elif hasattr(item, "model_dump"):
        data = item.model_dump()
    elif hasattr(item, "dict"):
        data = item.dict()
    else:
        fields = ("code", "name", "boards", "total_score", "signal_strength",
                  "signal_kind", "price", "change_pct", "quote_ts", "pinned",
                  "valuation_label", "valuation_bucket")
        data = {name: getattr(item, name, None) for name in fields}
    # 只留叶子类型：list[str]/str/float/bool/None
    plain: dict[str, Any] = {}
    for key, value in data.items():
        if isinstance(value, (str, int, float, bool)) or value is None:
            plain[key] = value
        elif isinstance(value, (list, tuple)):
            plain[key] = [str(v) for v in value]
    return plain


__all__ = [
    "DEFAULT_CACHE_DIR",
    "DEFAULT_MAX_AGE_SECONDS",
    "PAYLOAD_VERSION",
    "SNAPSHOT_VERSION",
    "PayloadLoad",
    "SnapshotLoad",
    "drop_snapshot",
    "drop_snapshot_payload",
    "load_snapshot",
    "load_snapshot_payload",
    "save_snapshot",
    "save_snapshot_payload",
    "snapshot_path",
    "snapshot_payload_dir",
    "snapshot_payload_path",
]
