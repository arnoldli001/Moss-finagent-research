"""科技行业真实产业数据下载脚本（替代模拟产业数据的三个指标）。

免费公开数据源：
- ind:半导体销售额同比  WSTS Historical Billings Report（全球月度销售额，自算同比）
- ind:芯片出货量同比    国家统计局 集成电路产量当月同比（akshare封装）
- ind:科技行业PE(TTM)   中证指数官网 全指半导体(H30184) PE-TTM（近20个交易日）

用法（项目 .venv）：
    .venv\\Scripts\\python.exe scripts\\download_industry_data.py
        # 下载全部
    .venv\\Scripts\\python.exe scripts\\download_industry_data.py --store
        # 下载并经A01-A04入库
    .venv\\Scripts\\python.exe scripts\\download_industry_data.py \
        --indicator ind:芯片出货量同比

原始快照同时落盘 data/industry_metrics/{wsts,nbs,csindex}/YYYY-MM-DD.json，
网络失败时连接器自动回退最近快照（输出标记 storage_fallback）。
可重复执行，入库按 (指标,期别,内容哈希) 幂等去重。
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.core.models import AgentInput  # noqa: E402
from src.infrastructure.connectors.real_industry_connector import (  # noqa: E402
    RealTechIndustryConnector,
)

INDICATORS = [
    "ind:半导体销售额同比", "ind:芯片出货量同比", "ind:科技行业PE(TTM)",
]


def _print_points(indicator: str, points: list) -> None:
    if not points:
        print(f"[缺口] {indicator}: 无数据（数据源失败且无本地快照）")
        return
    fallback = any(p.extra.get("storage_fallback") for p in points)
    tag = "（本地快照回退）" if fallback else ""
    print(f"\n=== {indicator}{tag} ===")
    print(f"来源: {points[-1].source_name}  可信度: {points[-1].confidence}")
    print(f"期数: {len(points)}  范围: {points[0].period_date} ~ {points[-1].period_date}")
    print("最近6期:")
    for p in points[-6:]:
        print(f"  {p.period_date}: {p.value:>10.2f}")


async def _store_through_pipeline(runtime, indicator: str, points: list) -> int:
    agents = runtime.agents
    task_id = f"inddl_{int(time.time())}_{indicator[-4:]}"
    raw = [p.model_dump(mode="json") for p in points]
    cleaned = await agents["A02_data_cleaner"].execute(AgentInput(
        task_id=task_id, tenant_id="tenant_001",
        payload={"data_points": raw}))
    validated = await agents["A03_data_validator"].execute(AgentInput(
        task_id=task_id, tenant_id="tenant_001",
        payload={"data_points": cleaned.result.get("data_points", [])}))
    stored = await agents["A04_data_storage"].execute(AgentInput(
        task_id=task_id, tenant_id="tenant_001",
        payload={"data_points": validated.result.get("data_points", [])}))
    return int((stored.result.get("storage_stats") or {}).get("total", 0))


async def main(indicators: list[str], store: bool) -> int:
    connector = RealTechIndustryConnector()
    runtime = None
    if store:
        from src.api.runtime import build_runtime

        runtime = build_runtime()

    failed = 0
    for indicator in indicators:
        try:
            points = await connector.fetch(indicator)
        except Exception as exc:  # noqa: BLE001 单指标失败不阻断其余
            print(f"[失败] {indicator}: {exc}")
            failed += 1
            continue
        _print_points(indicator, points)
        if store and points:
            saved = await _store_through_pipeline(runtime, indicator, points)
            print(f"  入库数据点: {saved}")
    return 1 if failed == len(indicators) else 0


def cli() -> None:
    parser = argparse.ArgumentParser(description="科技行业真实产业数据下载")
    parser.add_argument("--indicator", choices=INDICATORS,
                        help="只下载指定指标（默认全部）")
    parser.add_argument("--store", action="store_true",
                        help="经 A01→A02→A03→A04 清洗校验后入库（幂等）")
    args = parser.parse_args()
    indicators = [args.indicator] if args.indicator else INDICATORS
    raise SystemExit(asyncio.run(main(indicators, args.store)))


if __name__ == "__main__":
    cli()
