"""主线挖掘「热快照」：把当日那份评分快照落盘，让**重启与首次访问都不用重算**。

## 它解决什么问题（实测数字）

    全市场重算（`score_date_sync`，122 个板块）   8.4 ~ 22.6 秒（随 IO 竞争浮动）
    `_data_status()`（11 张表逐张 COUNT）        2.9 秒 ← 每次请求都要挂一回
    快照 JSON 大小                               0.10 MB
    `MAX(trade_date)` 水位线探针                 0 ms

原来的缓存是**进程内 + 300 秒 TTL**，于是有两个洞：

1. **缓存过期就要有人挨一次 8~23 秒**。而这份快照一天只变一次（板块日线、
   资金流、两融、北向、龙虎榜、宏观都是日频）—— 每 5 分钟让第一个访问者
   白烧一次全市场重算，纯浪费。
2. **每次重启缓存清零**，第一个打开面板的人替所有人挨那一下。
   2026-09-25 一天为了发布重启了 5 次，就是 5 次。

## 缓存键 = **数据水位线**，不是时间

水位线取 `ml_board_bar` 的 `MAX(trade_date)`（走索引，0 ms）。它变了才说明
"该重算了"；没变时算一百遍结果都一样。所以这份缓存**不需要 TTL** ——
时间流逝本身不改变任何输入。

## 与 `data/intel/` 那两份的约定一致

定时任务写、接口读；读不到/解析失败一律按"没有"处理（**不能让缓存坏了
变成页面 500**），落盘失败也只记警告（缓存不是业务数据）。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

logger = logging.getLogger(__name__)

#: 落盘位置。与 `data/intel/hot_topics.json`、`data/intel/hot_rank.json` 同一约定。
_STORE_DIR: Final = Path("data") / "mainline"
_STORE_FILE: Final = "warm_snapshot.json"

#: 进程内缓存（键固定 `"latest"`）—— 落盘文件只有一份，就是"最近一次热快照"。
_CACHE: dict[str, Any] = {}


def store_path(root: Path | None = None) -> Path:
    return (root or Path.cwd()) / _STORE_DIR / _STORE_FILE


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def save(data: dict[str, Any], *, root: Path | None = None) -> None:
    """原子落盘（先写 `.tmp` 再 replace）。**失败只记警告**。

    这份文件是缓存：只读文件系统 / 磁盘满 / 并发写都该退化成"这次没热缓存"，
    而不是让打分链路或接口失败。
    """
    try:
        p = store_path(root)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(p)
    except OSError as exc:
        logger.warning("主线热快照落盘失败（不影响本次结果）：%s",
                       type(exc).__name__)
    _CACHE["latest"] = data


def load(*, root: Path | None = None, force: bool = False) -> dict[str, Any]:
    """读最近一次热快照。读不到返回空 dict（调用方回退到现算）。"""
    if _CACHE.get("latest") is not None and not force:
        return _CACHE["latest"]  # type: ignore[return-value]
    p = store_path(root)
    data: dict[str, Any] = {}
    if p.exists():
        try:
            obj = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(obj, dict):
                data = obj
        except (OSError, ValueError):
            logger.warning("主线热快照读取失败（按没有处理）")
    _CACHE["latest"] = data
    return data


def payload_for(data: dict[str, Any], *, watermark: str,
                key: str) -> dict[str, Any] | None:
    """从热快照里取出**与当前水位线和参数完全匹配**的那份载荷。

    三个条件缺一不可，任何一个不匹配都必须回退到现算 ——
    返回一份"数据已经过期"或"参数不是这次要的"的快照，比慢几秒糟糕得多：
    它**不会报错**，只会让用户看到昨天的榜。
    """
    if not data:
        return None
    if str(data.get("watermark") or "") != str(watermark or ""):
        return None
    if str(data.get("key") or "") != str(key or ""):
        return None
    payload = data.get("payload")
    return payload if isinstance(payload, dict) else None


def age_seconds(data: dict[str, Any]) -> float | None:
    """落盘时间距今多少秒（**只用于展示/日志**，不参与缓存是否有效的判断）。"""
    raw = str(data.get("at") or "")
    if not raw:
        return None
    try:
        stamp = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - stamp.astimezone(timezone.utc)
            ).total_seconds()


def make(data: dict[str, Any], *, watermark: str, key: str,
         payload: dict[str, Any], data_status: dict[str, Any] | None = None
         ) -> dict[str, Any]:
    """组装要落盘的那份 dict（调用方负责在算完之后调 `save`）。"""
    return {
        "at": _now(),
        "watermark": str(watermark or ""),
        "key": str(key or ""),
        "payload": payload,
        # `data_status` 一起落盘：它实测 **2.9 秒**，而且同样一天只变一次 ——
        # 只热快照、不热它的话，重启后第一个请求还是要等 2.9 秒。
        "data_status": data_status or {},
    }


__all__ = ["age_seconds", "load", "make", "payload_for", "save", "store_path"]
