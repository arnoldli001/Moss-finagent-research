"""A股流动性周期研判（纯函数，阈值来自.trae/skills流动性周期skill）。

消费已采集/校验的数据点（dict或DataPoint均可），输出结构化"本地计算参考"：
- 两市成交额分层（>3万亿活跃/2.5-3万亿震荡/2-2.5万亿存量/<2万亿地量）
- 量能信号：MA5/MA10/MA50、MA10上穿MA50、单日脉冲放量、牛熊警戒线1.4万亿
- 中观三市分项：上证/创业板/科创板（科创综指全板口径）成交额与占比→风格偏向
- 双创市场宽度：涨跌家数/上涨占比/涨跌幅中位数（spot_summary）
- 双创板块PE历史分位（创业板/科创板，乐咕全历史）
- 全A换手率与成交集中度（前5%个股成交额占比，45%为牛熊转换警戒）
- 两融余额及5日变化、北向资金（含2024-08停披缺口）
- 核心宽基PE/PB历史分位（>80%偏热/<20%偏冷）
- CME FedWatch降息/不变/加息概率（不可用时诚实标注缺口）
- 建议仓位区间（地量20-30%/存量40-50%/震荡50-70%/活跃70-80%）

LLM只负责结合标的与产业逻辑解读这些数字，不负责计算。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

# 成交额分层阈值（亿元）
T_ACTIVE = 30000
T_OSCILLATING = 25000
T_RANGE = 20000
T_BULL_BEAR_GUARD = 14000  # 牛熊转换警戒线
PULSE_RATIO = 1.5          # 单日成交额≥1.5倍MA5视为脉冲放量
SHRINK_RATIO = 0.6         # 缩量40%以上（比值≤0.6）
CONCENTRATION_GUARD = 42.0  # 前5%成交集中度接近45%即预警
VAL_HOT_PCT = 80.0
VAL_COLD_PCT = 20.0
# 双创（创业板+科创板全板）成交额占两市比重→风格偏向阈值
GROWTH_ACTIVE_PCT = 40.0   # 双创占比≥40%：成长风格活跃
DEFENSE_WEIGHT_PCT = 25.0  # 双创占比≤25%：资金偏沪市权重/红利
# 双创市场宽度情绪阈值
BREADTH_STRONG = 60.0      # 上涨占比≥60%且中位数为正：普涨健康
BREADTH_FREEZE = 30.0      # 上涨占比≤30%：情绪冰点

_PHASE_RULES = (
    ("活跃市（增量资金，主线可持续）", T_ACTIVE, (70, 80)),
    ("震荡市（温和宽松，回踩为主）", T_OSCILLATING, (50, 70)),
    ("存量博弈（哑铃/跷跷板轮动）", T_RANGE, (40, 50)),
    ("地量冷却（轻仓等放量确认）", 0, (20, 30)),
)


def _get(p: Any, key: str, default: Any = None) -> Any:
    if isinstance(p, Mapping):
        return p.get(key, default)
    return getattr(p, key, default)


def _series(points: Sequence[Any], indicator: str) -> list[tuple[str, float]]:
    """取某指标按期别升序的(date, value)序列，跳过缺失值。"""
    rows = [
        (str(_get(p, "period_date") or ""), float(_get(p, "value")))
        for p in points
        if str(_get(p, "indicator")) == indicator and _get(p, "value") is not None
    ]
    return sorted(rows, key=lambda r: r[0])


def _ma(series: list[tuple[str, float]], n: int) -> float | None:
    if len(series) < n:
        return None
    return round(sum(v for _, v in series[-n:]) / n, 1)


def _latest_point(points: Sequence[Any], indicator: str) -> Any:
    matched = [p for p in points if str(_get(p, "indicator")) == indicator]
    if not matched:
        return None
    return max(matched, key=lambda p: str(_get(p, "period_date") or ""))


def assess_liquidity(points: Sequence[Any]) -> dict[str, Any]:
    """从数据点集合计算流动性周期研判结果；缺失数据进入data_gaps而非杜撰。"""
    result: dict[str, Any] = {
        "liquidity_phase": None,
        "suggested_position_pct": None,
        "turnover": {},
        "board_turnover": {},
        "market_breadth": {},
        "board_valuation": [],
        "turnover_rate": {},
        "margin": {},
        "northbound": {},
        "index_valuation": [],
        "fedwatch": {},
        "signals": [],
        "risk_alerts": [],
        "data_gaps": [],
        "summary_text": "",
    }

    # ---- 1. 成交额分层与量能信号 ----
    total_p = _latest_point(points, "mkt:turnover:total")
    hist = _series(points, "mkt:turnover:hist")
    if total_p is not None:
        total = float(_get(total_p, "value"))
        ma5, ma10, ma50 = _ma(hist, 5), _ma(hist, 10), _ma(hist, 50)
        prev_ma10 = _ma(hist[:-1], 10) if len(hist) >= 11 else None
        prev_ma50 = _ma(hist[:-1], 50) if len(hist) >= 51 else None
        label, position_band = _phase(total)
        result["liquidity_phase"] = label
        result["suggested_position_pct"] = list(position_band)
        block = {
            "total_yi": round(total, 1),
            "as_of": _get(total_p, "period_date"),
            "ma5_yi": ma5, "ma10_yi": ma10, "ma50_yi": ma50,
            "vs_ma5_pct": round(total / ma5 * 100 - 100, 1) if ma5 else None,
        }
        block.update(dict(_get(total_p, "extra") or {}))
        result["turnover"] = block
        # 信号：MA10上穿MA50（右侧温和修复，中线布局信号）
        if ma10 is not None and ma50 is not None and ma10 > ma50:
            if prev_ma10 is not None and prev_ma50 is not None and prev_ma10 <= prev_ma50:
                result["signals"].append("成交额MA10上穿MA50，温和放量修复（右侧中线信号）")
            else:
                result["signals"].append("成交额MA10位于MA50上方，量能中期趋势偏强")
        elif ma10 is not None and ma50 is not None:
            result["signals"].append("成交额MA10低于MA50，量能中期趋势偏弱")
        # 单日脉冲放量 / 明显缩量
        if ma5 and total >= PULSE_RATIO * ma5:
            result["signals"].append(
                f"单日成交额为MA5的{round(total / ma5, 2)}倍，脉冲放量（高赔率但需次日确认）")
        elif ma5 and total / ma5 <= SHRINK_RATIO:
            result["signals"].append("成交额较MA5缩量超40%，牛市缩量回调区间历史胜率较高")
        # 牛熊警戒线
        if total < T_BULL_BEAR_GUARD:
            result["risk_alerts"].append(
                f"两市成交额{total:.0f}亿跌破1.4万亿牛熊转换警戒线，谨慎为主")
    else:
        result["data_gaps"].append("两市成交额（腾讯/东财接口均不可用）")

    # ---- 1b. 中观三市分项：上证/创业板/科创板成交额与占比（定风格方向） ----
    # 科创板全板用科创综指(kcb_all)口径；科创50仅50只成分股只作权重观察
    total_v = float(_get(total_p, "value")) if total_p is not None else None
    cyb_p = _latest_point(points, "mkt:cybkcb:turnover:cyb")
    kcb_p = _latest_point(points, "mkt:cybkcb:turnover:kcb")
    kcb_all_p = _latest_point(points, "mkt:cybkcb:turnover:kcb_all")
    total_extra = dict(_get(total_p, "extra") or {}) if total_p is not None else {}
    sh_v = _num(total_extra.get("sh"))
    cyb_v = _num(_get(cyb_p, "value")) if cyb_p is not None else _num(total_extra.get("cyb"))
    kcb_v = _num(_get(kcb_p, "value")) if kcb_p is not None else _num(total_extra.get("kcb"))
    kcb_all_v = _num(_get(kcb_all_p, "value")) if kcb_all_p is not None else None
    if total_v is not None and (sh_v is not None or cyb_v is not None):
        growth_total = (cyb_v or 0.0) + (kcb_all_v or 0.0)
        growth_share = round(growth_total / total_v * 100, 1)
        if growth_share >= GROWTH_ACTIVE_PCT:
            style_bias = (
                f"双创（创业板+科创板全板）成交占比{growth_share}%≥40%，"
                "成长风格活跃、风险偏好回升")
        elif growth_share <= DEFENSE_WEIGHT_PCT:
            style_bias = (
                f"双创成交占比仅{growth_share}%≤25%，资金偏向沪市权重/红利防御")
        else:
            style_bias = f"双创成交占比{growth_share}%，成长与权重风格相对均衡"
        result["board_turnover"] = {
            "as_of": _get(total_p, "period_date"),
            "shanghai_yi": sh_v,
            "chinext_yi": cyb_v,
            "star50_yi": kcb_v,
            "star_all_yi": kcb_all_v,
            "chinext_share_pct": (
                round(cyb_v / total_v * 100, 1) if cyb_v is not None else None),
            "star_share_pct": (
                round(kcb_all_v / total_v * 100, 1) if kcb_all_v is not None else None),
            "growth_share_pct": growth_share,
            "style_bias": style_bias,
        }
        result["signals"].append(style_bias)

    # ---- 1c. 双创市场宽度（spot_summary：涨跌家数/上涨占比/中位涨幅） ----
    breadth_p = _latest_point(points, "mkt:cybkcb:spot_summary")
    if breadth_p is not None:
        bex = _get(breadth_p, "extra") or {}
        up_ratio = _num(_get(breadth_p, "value"))
        median_chg = _num(bex.get("median_chg_pct"))
        result["market_breadth"] = {
            "as_of": _get(breadth_p, "period_date"),
            "scope": "创业板+科创板",
            "stock_count": bex.get("stock_count"),
            "up_count": bex.get("up_count"),
            "down_count": bex.get("down_count"),
            "flat_count": bex.get("flat_count"),
            "up_ratio_pct": up_ratio,
            "median_chg_pct": median_chg,
            "total_turnover_yi": _num(bex.get("total_turnover_yi")),
        }
        if up_ratio is not None:
            if up_ratio >= BREADTH_STRONG and (median_chg or 0) > 0:
                result["signals"].append(
                    f"双创{int(up_ratio)}%个股上涨、中位涨幅{median_chg}%，普涨结构健康")
            elif up_ratio <= BREADTH_FREEZE:
                result["signals"].append(
                    f"双创仅{int(up_ratio)}%个股上涨，情绪接近冰点，结合地量规律等待放量反转")
            elif up_ratio < 40 and (median_chg or 0) < 0:
                result["risk_alerts"].append(
                    f"双创上涨占比{up_ratio}%且中位涨幅{median_chg}%，"
                    "权重拉指数而个股赚钱效应差，警惕虚涨")

    # ---- 1d. 双创板块PE及历史分位（乐咕；备源为现值口径无分位） ----
    for ind, board in (("mkt:cybkcb:val:cyb_pe", "创业板"),
                       ("mkt:cybkcb:val:kcb_pe", "科创板")):
        vp = _latest_point(points, ind)
        if vp is None:
            continue
        vex = _get(vp, "extra") or {}
        pe_val = _num(_get(vp, "value"))
        row = {
            "board": board,
            "pe": round(pe_val, 2) if pe_val is not None else None,
            "pct_1y": vex.get("pct_1y"),
            "pct_3y": vex.get("pct_3y"),
            "pct_5y": vex.get("pct_5y"),
            "pct_all": vex.get("pct_all"),
            "as_of": _get(vp, "period_date"),
            "source": vex.get("source"),
            "note": vex.get("note"),
        }
        result["board_valuation"].append(row)
        pct5 = _num(vex.get("pct_5y"))
        if pct5 is not None:
            if pct5 >= VAL_HOT_PCT:
                result["risk_alerts"].append(
                    f"{board}板块PE {row['pe']}倍处近5年{pct5:.0f}%分位，估值偏热，追高风险大")
            elif pct5 <= VAL_COLD_PCT:
                result["signals"].append(
                    f"{board}板块PE {row['pe']}倍处近5年{pct5:.0f}%分位，估值偏冷，安全边际较高")

    # ---- 2. 全A换手率与成交集中度 ----
    rate_p = _latest_point(points, "mkt:turnover_rate:all_a")
    if rate_p is not None:
        extra = _get(rate_p, "extra") or {}
        result["turnover_rate"] = {
            "all_a_weighted_pct": float(_get(rate_p, "value")),
            "as_of": _get(rate_p, "period_date"),
            "top5pct_concentration_pct": extra.get("top5pct_concentration_pct"),
        }
        conc = extra.get("top5pct_concentration_pct")
        if conc is not None and float(conc) >= CONCENTRATION_GUARD:
            result["risk_alerts"].append(
                f"成交集中度（前5%个股占比）{float(conc):.1f}%，逼近45%牛熊转换警戒线，"
                "警惕抱团交易拥挤")
    else:
        result["data_gaps"].append("全A加权换手率（东财clist聚合不可用）")

    # ---- 3. 两融余额与杠杆趋势 ----
    margin_p = _latest_point(points, "mkt:margin_balance")
    margin_hist = _series(points, "mkt:margin_balance:hist")
    if margin_p is not None:
        val = float(_get(margin_p, "value"))
        chg5 = None
        if len(margin_hist) >= 6:
            ref = margin_hist[-6][1]
            chg5 = round((val / ref - 1) * 100, 2) if ref else None
        result["margin"] = {
            "balance_yi": round(val, 1),
            "as_of": _get(margin_p, "period_date"),
            "change_5d_pct": chg5,
            "coverage": (_get(margin_p, "extra") or {}).get("coverage"),
        }
        if chg5 is not None and chg5 <= -2:
            result["risk_alerts"].append(f"两融余额5日下降{abs(chg5)}%，杠杆资金快速撤退")
    else:
        result["data_gaps"].append("两融余额（交易所接口不可用）")

    # ---- 4. 北向资金（停披缺口诚实标注） ----
    north_p = _latest_point(points, "mkt:north_flow")
    if north_p is not None:
        extra = _get(north_p, "extra") or {}
        if extra.get("status") == "disclosure_halted":
            result["northbound"] = {
                "status": "disclosure_halted",
                "halted_since": extra.get("halted_since"),
                "last_net_buy_yi": _get(north_p, "value"),
                "last_date": _get(north_p, "period_date"),
                "note": "北向日度净买额已停止披露，无法判断当前外资实时流向，不得臆测",
            }
            result["data_gaps"].append(
                f"北向资金日度净买额（{extra.get('halted_since')}起交易所停止披露）")
        else:
            result["northbound"] = {
                "status": "normal", "net_buy_yi": _get(north_p, "value"),
                "as_of": _get(north_p, "period_date"),
            }
    else:
        result["data_gaps"].append("北向资金（AKShare接口不可用）")

    # ---- 5. 核心宽基估值分位 ----
    val_points = [p for p in points
                  if str(_get(p, "indicator")) == "idx_val:snapshot:all"
                  and _get(p, "value") is not None]
    for p in sorted(val_points, key=lambda x: str(_get(x, "period_date") or ""),
                    reverse=True):
        extra = _get(p, "extra") or {}
        seen = {v.get("index_name") for v in result["index_valuation"]}
        name = extra.get("index_name")
        if not name or name in seen:
            continue
        row = {
            "index_name": name, "pe_ttm": round(float(_get(p, "value")), 2),
            "pb": _round(extra.get("pb"), 2),
            "pe_pct_5y": extra.get("pe_pct_5y"),
            "pb_pct_5y": extra.get("pb_pct_5y"),
            "as_of": extra.get("as_of"),
        }
        result["index_valuation"].append(row)
        pct = extra.get("pe_pct_5y")
        if pct is not None:
            if float(pct) >= VAL_HOT_PCT:
                result["risk_alerts"].append(
                    f"{name} PE-TTM处近5年{float(pct):.0f}%分位，估值偏热")
            elif float(pct) <= VAL_COLD_PCT:
                result["signals"].append(
                    f"{name} PE-TTM处近5年{float(pct):.0f}%分位，估值偏冷（安全边际较高）")
    if not val_points:
        result["data_gaps"].append("核心指数PE/PB估值分位（乐咕接口不可用）")

    # ---- 6. CME FedWatch（外网不可达时降级） ----
    fed_points = [p for p in points
                  if str(_get(p, "indicator")) == "fed:rate_prob:next"]
    if fed_points:
        extra = _get(fed_points[0], "extra") or {}
        result["fedwatch"] = {
            "meeting_date": extra.get("meeting_date"),
            "current_target": extra.get("current_target"),
            "cut_prob_pct": extra.get("cut_prob"),
            "hold_prob_pct": extra.get("hold_prob"),
            "hike_prob_pct": extra.get("hike_prob"),
            "dominant_range": extra.get("dominant_range"),
            "probabilities": {
                str(_get(p, "extra", {}).get("rate_range")): float(_get(p, "value"))
                for p in fed_points
            },
        }
    else:
        # ★ 2026-09-30（`CHG-0135`）：这里原来往 `data_gaps` 里写
        #   「CME FedWatch利率概率（当前环境无法访问CME/FRED，仅能依据…）」——
        #   两个问题，都会直接误导客户：
        #   ① **FRED 是可达的**：`fedwatch_connector` 自己就写着
        #      「CME FedWatch 主机 TCP 预检不可达，跳过调用（省去 ~21s 超时等待）；
        #        政策利率请用 fed:policy_range（FRED 源，**实测可达**）」，
        #      直连 `api.stlouisfed.org` 也有响应（400 = 缺 key，主机可达）。
        #      把"CME 不可达"写成"CME/FRED 都不可达"，是**把两个源混成一句**。
        #   ② 它正面违反 `decision/capabilities.py` 的明令：
        #      **禁止**在 `data_gaps` 里写「无法访问 CME/FRED」。
        #   现在：**不写进 data_gaps**（CME 不可达是环境限制、补不到，
        #   进缺口队列只会让 A19 白跑一次），只在 `fedwatch` 字段里如实说，
        #   并指向真正可用的替代口径 —— 不再声明 FRED 不可达。
        result["fedwatch"] = {
            # ⚠️ `status` 保持 `"unavailable"`：**这个状态是真的**
            #   （我们确实没有 FedWatch 概率），它是既有契约
            #   （`test_liquidity_cycle_skill.py::test_empty_points_only_gaps` 断言它）。
            #   错的只是下面那句 `data_gaps` 文案 —— 别把状态一起改掉。
            "status": "unavailable",
            "note": "CME FedWatch 主机本机不可达（已按 TCP 预检跳过调用，"
                    "省去约 21s 超时等待）；利率路径请用 FRED 源的"
                    "联邦基金目标区间（`fed:policy_range`，实测可达）",
            "alternative": "fed:policy_range",
        }

    result["summary_text"] = render_liquidity_hint(result)
    return result


def _phase(total_yi: float) -> tuple[str, tuple[int, int]]:
    for label, threshold, band in _PHASE_RULES:
        if total_yi >= threshold:
            return label, band
    return _PHASE_RULES[-1][0], _PHASE_RULES[-1][2]


def _round(v: Any, ndigits: int) -> float | None:
    try:
        return round(float(v), ndigits)
    except (TypeError, ValueError):
        return None


def _num(v: Any) -> float | None:
    """宽松转浮点：None/空串/非法值→None（连接器extra数值可能为字符串）。"""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def render_liquidity_hint(a: dict[str, Any]) -> str:
    """把研判结果渲染为给LLM的紧凑中文参考文本（A17/分析层prompt用）。"""
    lines: list[str] = []
    if a.get("liquidity_phase"):
        lo, hi = a["suggested_position_pct"]
        lines.append(f"流动性阶段：{a['liquidity_phase']}，对应建议总仓位区间{lo}-{hi}%。")
    t = a.get("turnover") or {}
    if t:
        lines.append(
            f"两市成交额{t.get('total_yi')}亿（{t.get('as_of')}），"
            f"MA5={t.get('ma5_yi')}亿/MA10={t.get('ma10_yi')}亿/MA50={t.get('ma50_yi')}亿，"
            f"相对MA5 {_signed(t.get('vs_ma5_pct'))}%。")
    # 中观三市分项：总量之后立即给出上证/创业板/科创板成交额与占比（中观优先）
    bt = a.get("board_turnover") or {}
    if bt:
        parts = []
        if bt.get("shanghai_yi") is not None:
            parts.append(f"沪市{bt['shanghai_yi']}亿")
        if bt.get("chinext_yi") is not None:
            parts.append(
                f"创业板{bt['chinext_yi']}亿(占比{bt.get('chinext_share_pct')}%)")
        if bt.get("star_all_yi") is not None:
            parts.append(
                f"科创板全板{bt['star_all_yi']}亿(占比{bt.get('star_share_pct')}%，"
                f"科创50成分{bt.get('star50_yi')}亿)")
        if parts:
            lines.append("三市分项成交额：" + "、".join(parts) + "。")
            lines.append(f"[风格] {bt.get('style_bias')}。")
    # 双创市场宽度（情绪温度/赚钱效应）
    mb = a.get("market_breadth") or {}
    if mb and mb.get("up_ratio_pct") is not None:
        lines.append(
            f"双创市场宽度（{mb.get('as_of')}，{mb.get('stock_count')}只）："
            f"{mb.get('up_count')}涨/{mb.get('down_count')}跌/{mb.get('flat_count')}平，"
            f"上涨占比{mb.get('up_ratio_pct')}%，涨跌幅中位数{_signed(mb.get('median_chg_pct'))}%。")
    r = a.get("turnover_rate") or {}
    if r:
        conc = r.get("top5pct_concentration_pct")
        lines.append(
            f"全A加权换手率{r.get('all_a_weighted_pct')}%"
            + (f"，前5%个股成交集中度{conc}%（45%为警戒）。" if conc is not None else "。"))
    m = a.get("margin") or {}
    if m:
        lines.append(
            f"两融余额{m.get('balance_yi')}亿（{m.get('coverage') or '沪深'}，"
            f"{m.get('as_of')}），5日变化{_signed(m.get('change_5d_pct'))}%。")
    n = a.get("northbound") or {}
    if n.get("status") == "disclosure_halted":
        lines.append(
            f"北向资金日度净买额自{n.get('halted_since')}起停止披露，"
            f"停披前最后值{n.get('last_net_buy_yi')}亿（{n.get('last_date')}），"
            "当前外资实时流向不可得，禁止杜撰。")
    elif n.get("status") == "normal":
        lines.append(f"北向资金当日净买额{_signed(n.get('net_buy_yi'))}亿。")
    vals = a.get("index_valuation") or []
    if vals:
        seg = "；".join(
            f"{v['index_name']} PE {v['pe_pct_5y']}%分位"
            for v in vals[:8] if v.get("pe_pct_5y") is not None)
        if seg:
            lines.append(f"核心宽基近5年PE分位：{seg}。")
    bvs = a.get("board_valuation") or []
    if bvs:
        def _bv_text(v: dict) -> str:
            if v.get("pct_5y") is not None:
                return f"{v['board']}PE {v['pe']}倍，近5年{v.get('pct_5y')}%分位"
            basis = v.get("note") or v.get("source")
            return f"{v['board']}PE {v['pe']}倍（{basis}现值口径，无历史分位）"

        seg = "；".join(_bv_text(v) for v in bvs)
        if seg:
            lines.append(f"双创板块估值：{seg}。")
    f = a.get("fedwatch") or {}
    if f.get("status") != "unavailable" and f:
        lines.append(
            f"CME FedWatch下次会议（{f.get('meeting_date')}）："
            f"降息概率{f.get('cut_prob_pct')}%/不变{f.get('hold_prob_pct')}%"
            f"/加息{f.get('hike_prob_pct')}%，当前目标区间{f.get('current_target')}。")
    for s in a.get("signals", []):
        lines.append(f"[信号] {s}")
    for w in a.get("risk_alerts", []):
        lines.append(f"[风险] {w}")
    for g in a.get("data_gaps", []):
        lines.append(f"[数据缺口] {g}")
    return "\n".join(lines)


def _signed(v: Any) -> str:
    try:
        return f"{float(v):+.1f}"
    except (TypeError, ValueError):
        return "N/A"
