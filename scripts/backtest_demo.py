"""回测演示：PPI同比动量规则 vs 周期股月度收益（真实AkShare优先，合成数据回退）。

运行（项目根目录）：
    uv run python -u scripts/backtest_demo.py
    uv run python -u scripts/backtest_demo.py --synthetic          # 强制合成数据
    uv run python -u scripts/backtest_demo.py --code 600519        # 换标的（默认601088中国神华）

规则（src/backtest，纯本地计算，无LLM、无未来函数）：
    PPI同比序列环比上行>1% → 次月持有标的；下行>1% → 空仓回避；区间内持平 → 空仓。
评估：1/3/6个月方向命中率、平均前瞻收益；多头策略净值 vs 买入持有。

⚠️ 本脚本仅验证信号框架与回测引擎，历史回测不代表未来收益，不构成投资建议。
   投资有风险，入市需谨慎，盈亏自负。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import re
import sys
import zlib
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.backtest.engine import result_to_dict, run_backtest  # noqa: E402
from src.backtest.signals import Bar, TrendPEConfig  # noqa: E402

DISCLAIMER = (
    "⚠️ 历史回测不代表未来收益，以上结果仅供框架验证，不构成投资建议。"
    "投资有风险，入市需谨慎，盈亏自负。"
)
_MONTH_RE = re.compile(r"(\d{4})\D{0,2}(\d{1,2})")


def _month_key(text: str | None) -> str | None:
    if not text:
        return None
    m = _MONTH_RE.search(str(text))
    if not m:
        return None
    year, month = int(m.group(1)), int(m.group(2))
    if not 1 <= month <= 12:
        return None
    return f"{year:04d}-{month:02d}"


async def _fetch_with_retry(conn, indicator: str, retries: int = 1):
    """顺序请求+一次重试：东财接口偶发RemoteDisconnected，并发更易触发。"""
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            return await conn.fetch(indicator)
        except Exception as exc:  # noqa: BLE001 演示脚本重试后才回退
            last_exc = exc
            if attempt < retries:
                await asyncio.sleep(3)
    raise last_exc  # type: ignore[misc]


async def _load_real_bars(code: str) -> list[Bar]:
    """AkShare真实数据：PPI月度同比 + 个股月末收盘，按月内连接（顺序请求）。"""
    from src.infrastructure.connectors.akshare_connector import AkshareConnector

    conn = AkshareConnector()
    ppi_points = await _fetch_with_retry(conn, "PPI")
    await asyncio.sleep(1)
    price_points = await _fetch_with_retry(conn, f"stock_close:{code}")

    indicator_by_month: dict[str, float] = {}
    for p in ppi_points:
        # period_date是发布日（如2025-07-09发布6月PPI）：按发布月对齐，
        # 信号在发布月才可知，天然保证回测无未来函数。
        key = _month_key(p.period_date)
        if key and isinstance(p.value, (int, float)):
            indicator_by_month[key] = float(p.value)

    # 日频收盘→月末收盘（同月取日期最大者）
    last_by_month: dict[str, tuple[str, float]] = {}
    for p in price_points:
        key = _month_key(p.period_date)
        if key and isinstance(p.value, (int, float)):
            day = str(p.period_date)
            if key not in last_by_month or day > last_by_month[key][0]:
                last_by_month[key] = (day, float(p.value))

    months = sorted(set(indicator_by_month) & set(last_by_month))
    if len(months) < 8:
        raise RuntimeError(f"真实数据对齐月份不足（{len(months)}），无法回测")
    return [
        Bar(period=m, price=last_by_month[m][1],
            indicators={"PPI": indicator_by_month[m]})
        for m in months
    ]


def _synthetic_bars(months: int = 120) -> list[Bar]:
    """确定性合成10年月度数据：下月收益=本月PPI环比方向的含噪映射。

    仅用于验证回测引擎机制——刻意构造了指标对价格的领先关系，
    因此合成数据上的"好结果"不具有任何市场含义。
    """
    bars: list[Bar] = []
    ppi = 100.0
    price = 3000.0
    start_year = datetime.now().year - months // 12
    for i in range(months):
        key = f"{start_year + i // 12:04d}-{i % 12 + 1:02d}"
        if i > 0:
            prev_ppi = bars[-1].indicators["PPI"]
            change_pct = (ppi - prev_ppi) / abs(prev_ppi) * 100
            noise = (zlib.crc32(key.encode()) % 1000) / 1000.0 - 0.5  # ±0.5%
            monthly_return = max(-0.06, min(0.06, change_pct / 100 * 0.8 + noise / 10))
            price *= 1 + monthly_return
        bars.append(Bar(period=key, price=round(price, 2),
                        indicators={"PPI": round(ppi, 3)}))
        ppi *= 1 + 0.012 * _cycle(i)
    return bars


def _cycle(i: int) -> float:
    # 36个月一轮景气周期（sin），叠小确定性扰动
    return math.sin(i / 36 * 2 * math.pi) + 0.3 * math.sin(i / 7 * 2 * math.pi)


async def main(force_synthetic: bool, code: str) -> int:
    asset = f"{code} A股"
    simulated = force_synthetic
    if force_synthetic:
        bars = _synthetic_bars()
        asset = "合成指数(无市场含义)"
    else:
        try:
            bars = await _load_real_bars(code)
        except Exception as exc:  # noqa: BLE001 演示脚本：任何真实数据问题都回退
            print(f"[warn] 真实数据加载失败，回退合成数据：{exc}")
            bars = _synthetic_bars()
            simulated = True
            asset = "合成指数(无市场含义)"

    print(f"[info] 数据源={'合成数据(仅验证引擎)' if simulated else 'AkShare真实数据'}"
          f" 标的={asset}  区间={bars[0].period}~{bars[-1].period}  月度样本={len(bars)}")

    result = run_backtest(bars, TrendPEConfig("PPI", eps_pct=1.0))
    payload = {
        "generated_at": datetime.now().astimezone().isoformat(),
        "simulated": simulated,
        "asset": asset,
        **result_to_dict(result),
        "disclaimer": DISCLAIMER,
    }

    out_dir = Path("data/backtest")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "ppi_csi300_backtest.json"
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    s = result.strategy
    print(f"[signals] 看多{result.signals['long']} / 中性{result.signals['neutral']}"
          f" / 回避{result.signals['avoid']}")
    for h, block in result.directional.items():
        long_b, base = block["long"], block["always_long_baseline"]
        print(f"[{h}] 看多n={long_b['n']} 命中率={long_b['hit_rate']} "
              f"平均前瞻={long_b['avg_forward_return']} 基准(全程持有)={base}")
    print(f"[strategy] 累计={s['cumulative_return']:.2%} 年化={s['cagr']} "
          f"最大回撤={s['max_drawdown']:.2%} 夏普(rf0)={s['sharpe_rf0']}")
    bh = s["buy_and_hold"]
    print(f"[buyhold ] 累计={bh['cumulative_return']:.2%} 年化={bh['cagr']} "
          f"最大回撤={bh['max_drawdown']:.2%} 超额={s['excess_cumulative_return']:.2%}")
    print(f"[output] {out_path}")
    print(DISCLAIMER)
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--synthetic", action="store_true", help="强制使用合成数据")
    parser.add_argument("--code", default="601088", help="A股代码（默认601088中国神华）")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.synthetic, args.code)))
