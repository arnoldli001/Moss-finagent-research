"""演示数据预热：提前拉取CPI/PPI并幂等入库，避免演示现场等待网络采集。

运行：$env:PYTHONPATH="."; uv run python scripts/seed_demo_data.py
说明：行业层指标为确定性模拟序列（MockIndustryConnector，运行时即时生成，无需预热）；
     本脚本只预热真实宏观数据，重复执行安全（A04存储为幂等upsert）。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.api.runtime import build_runtime  # noqa: E402

INDICATORS = ("CPI", "PPI")


async def main() -> int:
    runtime = build_runtime()
    before = await runtime.repo.count_by_indicator()
    print(f"[seed] 预热前计数: { {k: before.get(k, 0) for k in INDICATORS} }")

    failed: list[str] = []
    for ind in INDICATORS:
        try:
            print(f"[seed] 拉取 {ind} ...")
            points = await runtime.backend.fetch(ind)
            stats = await runtime.repo.save_points(points, task_id="seed_demo")
            print(f"[seed] {ind}: 拉取{len(points)}点，入库统计 {stats}")
        except Exception as exc:  # noqa: BLE001 预热失败不应中断其他指标
            print(f"[seed] {ind} 拉取失败（演示任务运行时仍会重试）: {exc}")
            failed.append(ind)
        await asyncio.sleep(1)

    after = await runtime.repo.count_by_indicator()
    print(f"[seed] 预热后计数: { {k: after.get(k, 0) for k in INDICATORS} }")
    warmed = all((after.get(k, 0) or 0) > 0 for k in INDICATORS)
    print("[seed]", "OK" if warmed else f"PARTIAL 失败指标={failed}")
    return 0 if warmed else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
