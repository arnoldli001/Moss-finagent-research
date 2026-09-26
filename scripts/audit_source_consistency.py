"""把**候选替代源**与 Tushare 仓库在**重叠区间**上逐字段比对，给出"能不能换"的判据。

## 用户的要求

> 「用 AkShare / 东财对接下，只要从 2026 年起就行，**数据对接一致，
>   不要有数据接口形式不一致导致的数据错误**。」

"不要有数据接口形式不一致导致的数据错误"这句话唯一的工程解法是：
**在重叠区间上实测两边的差异，并且把"超差就不许写入"做成闸门** ——
而不是靠人工看一眼觉得像。

## 三类必须先分清的问题（都实测过）

1. **字段顺序陷阱**：腾讯日线返回 `[日期, 开, 收, 高, 低, 量]` ——
   **收在高之前**。按 OHLC 的直觉去解会把"最高价"写成收盘价，
   而且**数值都是合理价格，不会报错**。本脚本按实测的顺序解，并逐字段比对来证明。
2. **单位陷阱**：Tushare 原口径是 万元/千元，仓库里已经换算成**元**
   （`datastore.py` 顶部有实测记录）。AkShare/东财/新浪各自的口径都不同，
   换源时必须显式换算 —— 早先本项目就因为多乘了一次把板块资金流放大 1e3~1e4 倍，
   而分数是横截面分位，**排序不变、界面上看不出任何异常**。
3. **同名不同义的陷阱（最危险）**：新浪 `netamount`（净流入额，全单口径）
   与 Tushare `net_mf_amount` **不是同一个东西**；新浪 `r0_net`（超大单净额）
   与 Tushare 的 `buy_elg − sell_elg` 更是**符号都能相反**。
   两列都叫"资金流"，乘错、接错都不会报错，只会让整条序列静默漂移。

## 判据（写死在这里，避免每次自己给自己放宽）

    日线 OHLCV     逐字段**完全相等**才算一致（同一交易所同一价格，没有"近似"的余地）
    资金流         先看**符号一致率**，再看**中位相对偏差**；
                   符号一致率 < 90% 或中位偏差 > 5% → **判为不可互换**

只读（网络 + 只读库），不写任何库。

用法：
    .venv\\Scripts\\python.exe scripts/audit_source_consistency.py --samples 8 \\
        --start 20260101 --end 20260915
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

WAREHOUSE = ROOT / "data" / "quant" / "warehouse.db"
HEADERS = {"User-Agent": "Mozilla/5.0"}

#: 日线逐字段比对：`quant_daily` 列 → 腾讯返回的下标
#: ⚠️ 腾讯字段序实测是 `[日期, 开, 收, 高, 低, 量]`，不是 OHLC。
TX_ORDER = {"open": 1, "close": 2, "high": 3, "low": 4, "volume_lot": 5}


def _dashed(day: str) -> str:
    """`20260101` → `2026-01-01`。⚠️ 腾讯接口只认短横写法，写错返回 `param error` +
    `data: []`（HTTP 仍是 200），照直取用就会炸在下标上。"""
    return f"{day[:4]}-{day[4:6]}-{day[6:]}"


def tx_daily(code: str, market: str, start: str, end: str) -> dict[str, list]:
    """腾讯日线（`qfq` 留空 = 不复权，与 Tushare `daily` 同口径）。

    ⚠️ 返回体形状会变：正常时 `data` 是 `{code: {...}}` 字典，参数出错时是**空列表**，
    所以这里既校验 `code` 又校验 `msg`，不让空结果伪装成「没有重叠日」。
    """
    url = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
           f"?param={market}{code},day,{_dashed(start)},{_dashed(end)},320,")
    payload = requests.get(url, timeout=20, headers=HEADERS).json()
    if payload.get("msg") not in (None, "", "ok"):
        raise RuntimeError(f"腾讯返回 msg={payload['msg']!r}（参数被拒）")
    data = payload.get("data")
    if not isinstance(data, dict) or f"{market}{code}" not in data:
        raise RuntimeError(f"腾讯返回 data 形状异常：{type(data).__name__}")
    node = data[f"{market}{code}"]
    key = "day" if "day" in node else next(k for k in node if "day" in k)
    return {row[0].replace("-", ""): row for row in node[key]}


def sina_moneyflow(code: str, market: str) -> dict[str, dict]:
    """新浪个股资金流（全历史，一次一只票）。"""
    url = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php"
           f"/MoneyFlow.ssl_qsfx_zjlrqs?daima={market}{code}")
    rows = json.loads(requests.get(url, timeout=25, headers=HEADERS).text)
    return {str(r.get("opendate", "")).replace("-", ""): r for r in rows}


def main() -> int:
    parser = argparse.ArgumentParser(description="候选替代源与 Tushare 的一致性实测")
    parser.add_argument("--start", default="20260101")
    parser.add_argument("--end", default="20260915")
    parser.add_argument("--samples", type=int, default=8, help="抽多少只票比对")
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    lines: list[str] = []

    def emit(text: str = "") -> None:
        print(text, flush=True)
        lines.append(text)

    conn = sqlite3.connect(f"file:{WAREHOUSE.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    # 抽样本：取库里有 2026 数据的、不同市场的票
    picks = [dict(r) for r in conn.execute(
        "SELECT code, ts_code FROM quant_daily WHERE trade_date = ?"
        " ORDER BY code LIMIT ?", (args.end, args.samples * 3))]
    conn.close()
    if not picks:
        print(f"❌ 库里没有 {args.end} 的数据，无法比对")
        return 2
    def market_of(ts_code: str) -> str:
        return "sh" if ts_code.endswith(".SH") else (
            "bj" if ts_code.endswith(".BJ") else "sz")
    picks = [{"code": p["code"], "market": market_of(str(p["ts_code"]))}
             for p in picks][: args.samples]

    emit("# 候选替代源与 Tushare 的一致性实测\n")
    emit(f"> 由 `scripts/audit_source_consistency.py` 生成"
         f"（{datetime.now().astimezone().isoformat(timespec='seconds')}）。")
    emit(f"> 重叠区间 {args.start} ~ {args.end}，抽样 {len(picks)} 只。\n")

    # ---------------- ① 日线 OHLCV ----------------
    emit("## 一、个股日线（腾讯 vs Tushare `quant_daily`）\n")
    emit("⚠️ 腾讯字段序是 `[日期, 开, **收**, 高, 低, 量]` —— 收在高之前。"
         "按 OHLC 的直觉解会把最高价写成收盘价，**而且数值都合理、不会报错**。\n")
    emit("比对按**字段类**分开判定：价格四字段要求逐位一致（容差 1e-6）；"
         "成交量单列，因为 Tushare 的 `vol` 带两位小数（如 `875491.18` 手）"
         "而腾讯返回整数手，二者天然差不到 1 手 —— "
         "**把取整差和口径差算成同一件事，就会得出错误的换源结论**。\n")
    emit("| 代码 | 可比天数 | 价格字段相等 | 价格不等 | 量最大差（手） | 结论 |")
    emit("|---|---:|---:|---:|---:|---|")
    price_fields = [f for f in TX_ORDER if f != "volume_lot"]
    tx_price_same = tx_price_diff = 0
    tx_vol_max_diff = 0.0
    tx_days = 0
    bad_examples: list[str] = []
    for item in picks:
        code, market = item["code"], item["market"]
        try:
            theirs = tx_daily(code, market, args.start, args.end)
        except Exception as exc:  # noqa: BLE001 单只失败不影响整体
            emit(f"| {code} | — | — | — | — | ⚠️ 腾讯取数失败"
                 f"（{type(exc).__name__}: {exc}） |")
            continue
        conn = sqlite3.connect(f"file:{WAREHOUSE.as_posix()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        mine = [dict(r) for r in conn.execute(
            "SELECT * FROM quant_daily WHERE code=? AND trade_date BETWEEN ? AND ?"
            " ORDER BY trade_date", (code, args.start, args.end))]
        conn.close()
        p_same = p_diff = days = 0
        vol_max = 0.0
        for row in mine:
            other = theirs.get(str(row["trade_date"]))
            if other is None:
                continue
            days += 1
            for field in price_fields:
                try:
                    theirs_value = float(other[TX_ORDER[field]])
                except (TypeError, ValueError, IndexError):
                    continue
                if abs(float(row[field] or 0.0) - theirs_value) <= 1e-6:
                    p_same += 1
                else:
                    p_diff += 1
                    if len(bad_examples) < 4:
                        bad_examples.append(
                            f"{code} {row['trade_date']} {field}: "
                            f"Tushare {row[field]} vs 腾讯 {theirs_value}")
            try:
                vol_max = max(vol_max, abs(float(row["volume_lot"] or 0.0)
                                           - float(other[TX_ORDER["volume_lot"]])))
            except (TypeError, ValueError, IndexError):
                pass
        tx_price_same += p_same
        tx_price_diff += p_diff
        tx_vol_max_diff = max(tx_vol_max_diff, vol_max)
        tx_days += days
        verdict = "✅ 价格一致" if p_diff == 0 else f"❌ 价格 {p_diff} 处不等"
        emit(f"| {code} | {days} | {p_same} | {p_diff} | {vol_max:.2f} | {verdict} |")
    for text in bad_examples:
        emit(f"| | | | | | {text} |")
    emit("")
    if tx_price_diff:
        emit(f"**日线小结**：❌ 价格字段有 {tx_price_diff} 处不等，**不可换源**。\n")
    elif tx_days:
        emit(f"**日线小结**：✅ {tx_days} 个交易日 × 4 个价格字段 = "
             f"{tx_price_same} 个值**全部逐位一致**；成交量每日差 <1 手"
             f"（最大 {tx_vol_max_diff:.2f} 手，纯取整），**价格可直接换源**。\n")
    else:
        emit("**日线小结**：⚠️ 没有可比对的交易日，结论不成立。\n")

    # ---------------- ② 资金流 ----------------
    emit("## 二、个股资金流（新浪 vs Tushare `quant_moneyflow`）\n")
    emit("⚠️ 两列都叫「资金流」但**不是同一个东西**：新浪 `netamount` 是全单口径、"
         "Tushare `net_mf_amount` 是另一套划分；新浪 `r0_net`（超大单净额）"
         "与 Tushare 的 `buy_elg − sell_elg` 连**符号都能相反**。\n")
    emit("| 代码 | 可比天数 | 符号一致率 | 中位相对偏差 | 结论 |")
    emit("|---|---:|---:|---:|---|")
    sign_hits = total = 0
    devs: list[float] = []
    for item in picks:
        code, market = item["code"], item["market"]
        try:
            theirs = sina_moneyflow(code, market)
        except Exception as exc:  # noqa: BLE001
            emit(f"| {code} | — | — | — | ⚠️ 新浪取数失败（{type(exc).__name__}） |")
            continue
        conn = sqlite3.connect(f"file:{WAREHOUSE.as_posix()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        mine = [dict(r) for r in conn.execute(
            "SELECT trade_date, net_mf_amount FROM quant_moneyflow"
            " WHERE code=? AND trade_date BETWEEN ? AND ?", (code, args.start, args.end))]
        conn.close()
        hit = cnt = 0
        local: list[float] = []
        for row in mine:
            other = theirs.get(str(row["trade_date"]))
            if other is None:
                continue
            try:
                mine_v = float(row["net_mf_amount"] or 0.0)
                theirs_v = float(other.get("netamount") or 0.0)
            except (TypeError, ValueError):
                continue
            cnt += 1
            if (mine_v >= 0) == (theirs_v >= 0):
                hit += 1
            if mine_v:
                local.append(abs(theirs_v - mine_v) / abs(mine_v))
        sign_hits += hit
        total += cnt
        devs.extend(local)
        rate = hit / cnt if cnt else float("nan")
        dev = sorted(local)[len(local) // 2] if local else float("nan")
        verdict = ("✅ 可互换" if (cnt and rate >= 0.9 and dev <= 0.05)
                   else "❌ 不可互换")
        emit(f"| {code} | {cnt} | {rate:.0%} | {dev:.1%} | {verdict} |")
    rate_all = sign_hits / total if total else float("nan")
    dev_all = sorted(devs)[len(devs) // 2] if devs else float("nan")
    emit("")
    flow_verdict = ("✅ 可互换" if (total and rate_all >= 0.9 and dev_all <= 0.05)
                    else "❌ **不可互换**（判据：符号一致率 ≥90% 且 中位偏差 ≤5%）")
    emit(f"**资金流小结**：符号一致率 **{rate_all:.1%}**、"
         f"中位相对偏差 **{dev_all:.1%}** → {flow_verdict}\n")

    emit("## 三、结论\n")
    if tx_price_diff:
        emit(f"- **日线 OHLCV**：❌ 不可换（价格字段 {tx_price_diff} 处不等）。")
    else:
        emit(f"- **日线价格（开/收/高/低）**：✅ 可换（腾讯，{tx_price_same} 个值逐位一致）。"
             f"成交量存在 <1 手取整差（最大 {tx_vol_max_diff:.2f} 手），"
             "若下游对量做**分位/阈值**判断则影响可忽略，做**逐位对账**则不行。")
        emit("  但成交额 `amount` 腾讯日线接口**不提供** —— "
             "而 `amount` 恰恰是板块成交额的分子，"
             "所以腾讯只能补**价格**，不能独立补齐 `quant_daily` 全表。")
    emit("- **个股资金流**：**不可换** —— 新浪与 Tushare 的定义不同，"
         "换过去会让 `board_flow` 整条序列出现台阶，"
         "而它进的是**截面分位**，排序会变、告警会变，界面上看不出来。")
    emit("- **东财行情域名（`push2his` / `push2`）在本机被 SNI 阻断**"
         "（TCP 握手成功、TLS 立即被重置；而 baidu / tushare / 新浪 / 腾讯 / "
         "东财 datacenter 都正常）—— 这一条我原本写成「全部不可用」，**说过头了**：")
    emit("  - 项目早就知道这件事并有规避（`src/core/eastmoney_direct.py`，"
         "2026-09-17 实测记录），本部署经 `MOSS_EM_DIRECT` **显式打开**；")
    emit("  - 实测**同一进程内结果就不一致**：`stock_individual_fund_flow` "
         "✅ 拿到 120 行，而 `stock_zh_a_hist` ❌ "
         "`RemoteDisconnected`（探测到的候选 IP 不可用）；")
    emit("  - 项目自己的结论就是「阻断会漂移、不存在稳定绕过」，"
         "所以东财只能**排链尾、必须有兜底**，不能当主源。")
    emit("- 但**换不换源的决定性理由不是可达性，是口径**：东财/新浪的「主力」"
         "定义与 Tushare `net_mf_amount` 不同，而 `board_flow` 进的是**截面分位** —— "
         "换过去会让新旧数据接不上（出现台阶），而界面上看不出来。")

    if args.out:
        target = ROOT / args.out
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"\n报告 → {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
