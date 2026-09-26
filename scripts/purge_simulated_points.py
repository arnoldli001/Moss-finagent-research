"""一次性运维：清掉本地库里**已经落盘**的模拟产业数据行。

## 为什么删连接器还不够（这一步非做不可）

`MockIndustryConnector` 的产出不只活在运行时 —— 它**已经落盘**，而路由的
「本地持久化 DB」档优先级**高于**连接器链（`router._cached_fetch` 的三级短路：
TTL 缓存 → 本地 DB → connector 链）。库里 `source_name = "模拟产业数据(Demo)"`
的行若被判为 fresh，就直接返回，**根本不会走到网络源**。

2026-09-26 实测（本仓库默认库 data/moss_finagent.db）：

    周期行业PE(TTM) 已接中证指数官网真实源 → 路由仍返回 "模拟产业数据(Demo)" 的
    10.1，因为库里躺着 24 个月度模拟点（2024-10 ~ 2026-09），判为 fresh 后短路。

也就是说：只退役连接器，Agent 拿到的仍是模拟值 —— 那才是"真实源明明接好了、
面板还是模拟数据"的真正原因。

## 用法

    # 先看会删什么（默认 dry-run，绝不动数据）
    .venv\\Scripts\\python.exe scripts\\purge_simulated_points.py

    # 确认后真删
    .venv\\Scripts\\python.exe scripts\\purge_simulated_points.py --apply

删除走统一数据层的 `delete_points_by_source`（遵守"禁止直接操作数据库"）；
dry-run 的预览是一次只读 sqlite 查询（`mode=ro`，与 `_audit_purification.py`
同一诊断口径），不写任何东西。
"""

from __future__ import annotations

import argparse
import asyncio
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

#: 待清理的来源（按 source_name 精确匹配）。
#: 这里写死常量而不是 import 连接器 —— 连接器退役后本脚本仍需可用。
DEFAULT_SOURCES: tuple[str, ...] = ("模拟产业数据(Demo)",)
#: 兜底判据：extra_json 里自带 simulated=true 的行（防来源改名后漏删）。
_SIMULATED_JSON_PATTERNS = ('%"simulated": true%', '%"simulated":true%')


def _preview(db_path: str, sources: tuple[str, ...]) -> dict[str, object]:
    """只读预览：按来源/指标统计将被删除的行（sqlite only）。"""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        placeholders = ",".join("?" * len(sources))
        by_source = conn.execute(
            f"SELECT source_name, COUNT(*) FROM fact_data_points "
            f"WHERE source_name IN ({placeholders}) GROUP BY source_name",
            sources,
        ).fetchall()
        by_indicator = conn.execute(
            f"SELECT indicator, COUNT(*) FROM fact_data_points "
            f"WHERE source_name IN ({placeholders}) GROUP BY indicator "
            f"ORDER BY 2 DESC",
            sources,
        ).fetchall()
        extra = 0
        for pat in _SIMULATED_JSON_PATTERNS:
            extra += conn.execute(
                "SELECT COUNT(*) FROM fact_data_points "
                "WHERE extra_json LIKE ? AND source_name NOT IN "
                f"({placeholders})",
                (pat, *sources),
            ).fetchone()[0]
        span = conn.execute(
            f"SELECT MIN(period_date), MAX(period_date) FROM fact_data_points "
            f"WHERE source_name IN ({placeholders})",
            sources,
        ).fetchone()
    finally:
        conn.close()
    return {
        "by_source": by_source,
        "by_indicator": by_indicator,
        "extra_simulated_unmatched": extra,
        "span": span,
    }


async def _purge(sources: tuple[str, ...]) -> dict[str, int]:
    from src.core.config import get_settings
    from src.infrastructure.repositories.repository_factory import build_repository

    settings = get_settings()
    repo = build_repository(settings)
    try:
        out: dict[str, int] = {}
        for name in sources:
            out[name] = await repo.delete_points_by_source(name)
        return out
    finally:
        await repo.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="删除本地库中已落盘的模拟产业数据行（默认 dry-run）")
    parser.add_argument("--apply", action="store_true",
                        help="真正执行删除；不给则只预览")
    parser.add_argument("--source", action="append", default=None,
                        help=f"要清理的来源名，可重复；默认 {DEFAULT_SOURCES[0]}")
    args = parser.parse_args()

    sources = tuple(args.source) if args.source else DEFAULT_SOURCES

    from src.core.config import get_settings

    settings = get_settings()
    db_path = settings.sqlite_path
    print(f"[purge] 后端={settings.data_backend} 库={db_path}")
    print(f"[purge] 目标来源={list(sources)}")

    if settings.data_backend == "sqlite" and Path(db_path).exists():
        info = _preview(db_path, sources)
        print("-" * 72)
        if info["by_source"]:
            for name, n in info["by_source"]:
                print(f"  来源 {name}: {n} 行")
        else:
            print("  该来源在库中没有行（已清理过或从未落盘）")
        if info["span"] and info["span"][0]:
            print(f"  期间跨度: {info['span'][0]} ~ {info['span'][1]}")
        if info["by_indicator"]:
            print("  按指标：")
            for ind, n in info["by_indicator"]:
                print(f"    {ind}: {n}")
        if info["extra_simulated_unmatched"]:
            print(f"  ⚠ 另有 {info['extra_simulated_unmatched']} 行 extra_json 标了 "
                  "simulated=true 但不属于上述来源，请人工确认")
        print("-" * 72)
    elif settings.data_backend != "sqlite":
        print("[purge] 非 sqlite 后端：跳过本地预览，删除直接走仓储层执行")

    if not args.apply:
        print("[purge] dry-run（未删除任何行）。确认无误后加 --apply 执行。")
        return 0

    deleted = asyncio.run(_purge(sources))
    total = sum(deleted.values())
    for name, n in deleted.items():
        print(f"[purge] 已删除 {name}: {n} 行")
    print(f"[purge] 合计删除 {total} 行"
          + ("" if total else "（无匹配行，无需清理）"))
    return 0


if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        if hasattr(_s, "reconfigure"):
            try:
                _s.reconfigure(encoding="utf-8", errors="replace")
            except Exception:  # noqa: BLE001 编码设置失败不影响主流程
                pass
    raise SystemExit(main())
