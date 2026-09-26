"""报告持久化：按交易日落盘 JSON + HTML，读侧按日期检索。

目录：项目根 `data/sector_rotation/`：
  - `report_YYYYMMDD.json` —— 结构化数据（API 的 JSON 响应就是它）
  - `report_YYYYMMDD.html` —— 独立网页（report.html 接口直接返回文件内容）

为什么 JSON 也落盘：报告一天只有一份（收盘后不变），落盘后
"进程重启 / 前端首屏"都直接读文件，不为每个请求重付一次取数
（Tushare 截面实测秒级，但 akshare 子进程与腾讯快照都是网络调用）。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from src.core.errors import BRIEF_TIGHT, brief

logger = logging.getLogger(__name__)


def store_dir() -> Path:
    """报告落盘目录（项目根 data/sector_rotation）。"""
    root = Path(__file__).resolve().parents[2]
    target = root / "data" / "sector_rotation"
    target.mkdir(parents=True, exist_ok=True)
    return target


def _norm_date(trade_date: str) -> str:
    """统一成 YYYYMMDD（接受 2026-09-24 / 20260924 两种写法）。"""
    return "".join(ch for ch in str(trade_date) if ch.isdigit())[:8]


def json_path(trade_date: str) -> Path:
    return store_dir() / f"report_{_norm_date(trade_date)}.json"


def html_path(trade_date: str) -> Path:
    return store_dir() / f"report_{_norm_date(trade_date)}.html"


def save(trade_date: str, payload: dict[str, Any], html: str) -> None:
    """落盘 JSON + HTML（失败只记日志，不影响生成方返回结果）。"""
    try:
        json_path(trade_date).write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        html_path(trade_date).write_text(html, encoding="utf-8")
    except Exception as exc:  # noqa: BLE001 落盘失败不该弄丢已生成的报告
        logger.warning("行业轮动报告落盘失败(%s)：%s", trade_date, brief(exc, BRIEF_TIGHT))


def load(trade_date: str) -> dict[str, Any] | None:
    """读指定交易日的报告 JSON（不存在/损坏返回 None）。"""
    path = json_path(trade_date)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 损坏按"没有"处理，触发重新生成
        logger.info("行业轮动报告读取失败(%s)：%s", trade_date, brief(exc, BRIEF_TIGHT))
        return None


def load_html(trade_date: str) -> str | None:
    path = html_path(trade_date)
    if not path.exists():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001
        return None


def history() -> list[str]:
    """已落盘的交易日列表（YYYYMMDD，新的在前）。"""
    dates = []
    for path in store_dir().glob("report_*.json"):
        stamp = path.stem.replace("report_", "")
        if len(stamp) == 8 and stamp.isdigit():
            dates.append(stamp)
    return sorted(dates, reverse=True)


def latest_date() -> str | None:
    """最近一份已落盘报告的交易日。"""
    dates = history()
    return dates[0] if dates else None
