"""数据生命周期登记表：指标的「更新周期 / 自动采集作业 / 是否查库」总览。

回答"本地系统有没有根据不同数据的更新周期，定期采集宏观、中观、微观数据
到数据库"这个问题 —— 有的，但**没有任何一处能一眼看全**：
更新周期散在 `core/data_freshness.py` 的 `_FREQ_BY_PREFIX`，
自动采集作业散在 `scheduler/registry.py` 的 `JOB_REGISTRY`，
"要不要查库"散在 `connectors/router.py` 的 `_DB_SKIP_PREFIXES`/
`_DB_QUERY_PREFIXES`，TTL 又在 `_CACHE_TTL_RULES`。

三处口径**互相独立、没有任何一致性校验** —— 于是出现了 2026-09-26 那类故障：
`scheduler` 明明有作业在采、库里明明有数据，但 `router` 把它标成"实时型"
于是每次分析都绕过数据库重新打网络。

本脚本把三处口径**并排打出来**，让"哪个指标没被定期采集/没查库"一眼可见。

用法： python scripts/data_lifecycle.py
      python scripts/data_lifecycle.py --check    # 只报"有作业但分析时仍打网络"的指标
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.core.data_freshness import _DEFAULT_FREQ, _FREQ_BY_PREFIX  # noqa: E402
from src.infrastructure.connectors.router import (  # noqa: E402
    _DB_QUERY_PREFIXES,
    _DB_SKIP_PREFIXES,
    _TTL_BY_PREFIX,
    _should_query_db,
)

#: 分析时实际会请求的指标（来自 plan_run 的各类模板 + 行业路由表）
SAMPLE_INDICATORS: tuple[str, ...] = (
    # 宏观
    "CPI", "PPI", "us_cpi_yoy", "us_fed_rate", "us_nonfarm", "us_pce",
    # 个股（微观）
    "stock_close:300308", "PE(TTM):300308", "PB:300308",
    "资产负债率:300308", "流动比率:300308",
    # 行业（中观）
    "ind:半导体销售额同比", "ind:科技行业PE(TTM)",
    "ind:sw_third_pe_ttm:all", "ind:sw_third_pb:all", "ind:sw_first_pe_ttm:all",
    "ind:penetration:AI大模型应用",
    # 大盘流动性
    "mkt:turnover:total", "mkt:turnover:hist", "mkt:turnover_rate:all_a",
    "mkt:margin_balance", "mkt:margin_balance:hist", "mkt:north_flow",
    "idx_val:snapshot:all",
    # 双创板块
    "mkt:cybkcb:turnover:all", "mkt:cybkcb:val:all", "mkt:cybkcb:spot_summary",
    # 外网
    "fed:rate_prob:next",
)


def _freq(indicator: str) -> tuple[int, int, int]:
    """查 DataFreshnessEvaluator 的口径 → (发布周期天, 滞后天, 过期天)。"""
    for prefixes, cycle, lag, expire in _FREQ_BY_PREFIX:
        for p in prefixes:
            if indicator.startswith(p):
                return cycle, lag, expire
    return _DEFAULT_FREQ


def _freq_label(days: int) -> str:
    if days <= 1:
        return "日频"
    if days <= 7:
        return "周频"
    if days <= 31:
        return "月频"
    if days <= 92:
        return "季频"
    return "年频/长周期"


def _ttl(indicator: str) -> str:
    for prefixes, ttl in _TTL_BY_PREFIX:
        if any(indicator.startswith(p) for p in prefixes):
            return f"{ttl}s" if ttl < 3600 else f"{ttl // 3600}h"
    return "无(不缓存)"


def _db_mode(indicator: str) -> str:
    q = _should_query_db(indicator)
    if q:
        return "查库优先"
    low = indicator.lower()
    for p in _DB_SKIP_PREFIXES:
        if low.startswith(p.lower()):
            return "★跳过库(实时型)"
    return "默认不查库"


#: 作业名 → 它负责的指标前缀（人工对照 scheduler/registry.py 的 description）
JOB_COVERAGE: dict[str, tuple[str, ...]] = {
    "market_intraday_snapshot": ("mkt:turnover:total", "mkt:turnover_rate:all_a"),
    "market_daily_snapshot": ("mkt:turnover:hist", "mkt:margin_balance",
                              "mkt:margin_balance:hist", "mkt:north_flow",
                              "idx_val:"),
    "board_cyb_kcb_intraday": ("mkt:cybkcb:turnover:all",),
    "board_cyb_kcb_daily": ("mkt:cybkcb:val", "mkt:cybkcb:turnover_hist",
                            "mkt:cybkcb:spot_summary"),
    "fedwatch_daily": ("fed:rate_prob",),
    "industry_valuation_snapshot": ("ind:sw_",),
    "tech_industry_daily": ("ind:科技行业PE(TTM)",),
    "tech_industry_monthly": ("ind:半导体销售额同比", "ind:芯片出货量同比"),
    "penetration_rate_update": ("ind:penetration:",),
    "snapshot_macro": ("CPI", "PPI"),
    "quant_data_sync": ("stock_close:", "PE(TTM):", "PB:"),
    "snapshot_industry_watchlist": ("ind:sw_",),
    # 2026-09-26 新增：补齐"有分析需求但没有定期作业"的指标
    "us_macro_daily": ("us_cpi_yoy", "us_core_cpi", "us_nonfarm",
                       "us_unemployment", "us_pce", "us_fed_rate"),
    "financial_ratio_weekly": ("资产负债率:", "流动比率:"),
}


def _jobs_for(indicator: str) -> list[str]:
    out = []
    for job, prefixes in JOB_COVERAGE.items():
        if any(indicator.startswith(p) for p in prefixes):
            out.append(job)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="只列「有定期作业、但分析时仍会打网络」的指标")
    args = ap.parse_args()

    rows = []
    for ind in SAMPLE_INDICATORS:
        cycle, lag, expire = _freq(ind)
        jobs = _jobs_for(ind)
        mode = _db_mode(ind)
        rows.append((ind, _freq_label(cycle), cycle, lag, expire,
                     _ttl(ind), mode, jobs))

    if args.check:
        print("=== 有定期采集作业、但分析时仍会绕过数据库的指标 ===\n")
        bad = [r for r in rows if r[7] and "跳过库" in r[6]]
        if not bad:
            print("  无（全部一致）")
        for ind, fl, _c, _lag, _exp, _ttl_txt, mode, jobs in bad:
            print(f"  {ind:<32} {fl:<6} {mode:<16} 作业={','.join(jobs)}")
        print(f"\n  共 {len(bad)} 个。这些指标**每次分析都要重新打网络**，")
        print("  即使 scheduler 刚采过、库里就是新的。")
        print("  代价实测：mkt:cybkcb:spot_summary 13.4s、fed:rate_prob:next 23.5s。")
        return

    print("=" * 118)
    print("投研指标数据生命周期总览（更新周期 / 缓存TTL / 是否查库 / 自动采集作业）")
    print("=" * 118)
    hdr = (f"{'指标':<32}{'频度':<7}{'周期':>4}{'滞后':>5}{'过期':>5}  "
           f"{'TTL':<8}{'库策略':<18}自动采集作业")
    print(hdr)
    print("-" * 118)
    for ind, fl, c, lag, expire, ttl, mode, jobs in rows:
        jt = ",".join(jobs) if jobs else "**无**"
        print(f"{ind:<32}{fl:<7}{c:>4}{lag:>5}{expire:>5}  "
              f"{ttl:<8}{mode:<18}{jt}")
    print("-" * 118)

    no_job = [r[0] for r in rows if not r[7]]
    skip_db = [r[0] for r in rows if "跳过库" in r[6]]
    print(f"\n  指标数 {len(rows)}｜无定期作业 {len(no_job)}｜分析时跳过数据库 {len(skip_db)}")
    if no_job:
        print(f"\n  ⚠️ 无定期采集作业（每次都靠分析时现拉）：")
        for i in no_job:
            print(f"      {i}")
    if skip_db:
        print(f"\n  ⚠️ 分析时跳过数据库（每次都打网络）：")
        for i in skip_db:
            print(f"      {i}")


if __name__ == "__main__":
    main()
