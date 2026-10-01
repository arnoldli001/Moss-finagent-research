"""系统数据能力清单（Capability Catalog）—— 让 Agent 知道"系统有什么"。

## 为什么需要它（2026-09-28 实测暴露的缺陷）

A17 输出的 `data_gaps` 声称缺少：
    「三市分项成交额、北向资金、十月季节性统计与解禁规模」

**核实结果：至少 2 条是错的。**

| A17 说"缺" | 实际 |
|---|---|
| 三市分项成交额 | ❌ **就在 DB**：`mkt:turnover:total` 的 `extra` 含 `sh/sz/cyb/kcb` |
| 限售解禁规模 | ❌ **投资日历能取到**：实测 21 个交易日完整明细，含个股级解禁市值 |
| 北向资金 | ⚠️ 措辞不准：有历史值，**2024-08 起官方停披**（源终止，不是系统缺） |

**根因**：A17 的 prompt 里没有"系统有哪些数据能力"的清单。
它只能看到上游 Agent 传进来的那点数据，于是把**「上游没传给我」**
当成了**「系统没有」**。

**危害不只是措辞**：`2026-10-28` 西安奕材单家解禁 **848 亿**、
`2026-10-21` 中船特气 **818 亿** —— 这是十月行情的重大供给冲击。
A17 把它当"缺失"，等于**漏掉了十月最关键的变量之一**。

## 设计

本模块是**声明式清单**：每条能力写清
    · 能力名（人话）
    · 数据在哪（指标 / API / 表）
    · 怎么取（调用方式）
    · 状态（available / source_terminated / unavailable）

A17 的 prompt 会带上这份清单，于是它能把"缺口"分成三类
（与 AGENTS.md「"没量到"与"量到 0"必须分开」同源）：

    ① `available_not_included` —— 系统里有，本次未纳入 → **可补取**
    ② `source_terminated`      —— 源已停止披露 → **不可补取，如实说明**
    ③ `unavailable`            —— 确实拿不到 → 真缺口

## 维护

新增数据能力时**必须同步登记**。判据：只要系统里能取到而 A17 可能需要的，
就该在这里出现。漏登记 → A17 会把它误报成"缺失"（本轮就是这个教训）。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Capability:
    """一条数据能力。"""

    name: str            # 人话名称（A17 会引用它）
    how_to_get: str      # 怎么取（指标名 / API 路径 / 表名）
    status: str          # available | source_terminated | unavailable
    note: str = ""       # 补充说明（如停披时间）


#: ★ 系统数据能力清单（A17 prompt 注入用）
#:
#: 排序：越靠前越常用。A17 的 prompt 会按此顺序渲染。
CAPABILITIES: tuple[Capability, ...] = (
    # ---------- 行情 / 流动性 ----------
    Capability(
        "两市合计成交额 + **三市分项**",
        "指标 `mkt:turnover:total`，其 `extra` 含 "
        "sh(沪) / sz(深) / cyb(创业板) / kcb(科创板)",
        "available",
        "别再说「仅有两市合计」—— 分项就在 extra 里",
    ),
    Capability(
        "成交额历史序列（60 交易日，用于 MA5/10/50）",
        "指标 `mkt:turnover:hist`", "available"),
    Capability(
        "全A 加权换手率 + 前 5% 成交集中度",
        "指标 `mkt:turnover_rate:all_a`", "available"),
    Capability(
        "两融余额 + 10 日序列",
        "指标 `mkt:margin_balance` / `mkt:margin_balance:hist`", "available"),
    Capability(
        "核心宽基 PE/PB + 1/3/5 年/全历史分位",
        "指标 `idx_val:snapshot:all`（沪深300/中证500/中证1000/"
        "上证50/创业板50/科创50）", "available"),
    Capability(
        "创业板/科创板成交额、板块 PE 分位、涨跌家数宽度",
        "指标 `mkt:cybkcb:turnover:all` / `mkt:cybkcb:val:all` / "
        "`mkt:cybkcb:spot_summary`", "available"),

    # ---------- 投资日历（★ 本轮补充，A17 之前完全不知道）----------
    Capability(
        "★ **限售解禁日程与规模**（含个股级明细）",
        "投资日历 API `GET /api/v1/intel/calendar`，"
        "底层 `src/domain/intel/calendar.py::fetch_unlock_schedule`"
        "（东财主源 + 巨潮备源）",
        "available",
        "含每日解禁家数、合计解禁市值、逐只个股的"
        "解禁市值/占流通市值比/限售股类型。实测可取 30+ 交易日",
    ),
    Capability(
        "财报预约披露日程",
        "投资日历（`stock_yysj_em` 东财 / `stock_report_disclosure` 巨潮）",
        "available"),
    Capability(
        "宏观数据发布日程",
        "投资日历（`configs/calendar_official.yaml` + 百度财经日历）",
        "available"),
    Capability(
        "交易日历",
        "投资日历（`tool_trade_date_hist_sina`，交易所口径）", "available"),

    # ---------- 行业 / 估值 ----------
    Capability(
        "申万一级/二级/三级行业 PE/PB 截面",
        "指标 `ind:sw_first_pe_ttm:all` / `ind:sw_second_pe_ttm:all` / "
        "`ind:sw_third_pe_ttm:all` / `ind:sw_third_pb:all`", "available"),
    Capability(
        "板块成分股、概念映射",
        "表 `sector_member` / `map_stock_concept` / `dim_concept`", "available"),

    # ---------- 宏观 ----------
    Capability(
        "中国宏观（CPI/PPI/M2/社融）与美国宏观（CPI/核心CPI/非农/"
        "失业率/联邦基金利率/PCE）",
        "指标 `CPI` / `PPI` / `M2` / `社融` / `us_cpi_yoy` / "
        "`us_fed_rate` / `us_nonfarm` / `us_unemployment` / `us_pce`",
        "available"),
    Capability(
        "美联储政策利率（目标区间上下限 + 有效联邦基金利率）",
        "指标 `fed:effr`（有效利率，**判方向用它**）/ `fed:target_upper` / "
        "`fed:target_lower`（区间上下限，**成对读**）",
        "available",
        "★ 2026-09-28 第二十三轮：原混合口径 `fed:policy_range` 同一天有"
        "上下限两条点，取『最新一条』会把**区间下限当成政策利率**。"
        "现在拆成三个单值序列；`fed:policy_range` 仍在采集（兼容历史库），"
        "但**不要**拿它取单点。",
    ),

    # ---------- 已知终止 / 不可得（必须如实说明，别当"系统缺陷"）----------
    Capability(
        "北向资金日度净买额",
        "指标 `mkt:north_flow`",
        "source_terminated",
        "⚠️ **2024-08 起官方停止披露**，库里只有停披前的历史值。"
        "这不是系统缺口 —— 应表述为「该指标已停止披露」而非「缺少数据」",
    ),
    Capability(
        "CME FedWatch 加息概率",
        "指标 `fed:rate_prob:next`（CME）",
        "unavailable",
        "⚠️ 本机到 `www.cmegroup.com:443` **确定性不可达**（TCP 预检 2s 失败），"
        "库里 `fed:rate_prob:next` 恒为 0 条。这是**环境限制**，不是系统缺陷。\n"
        "   ★★★ **这【只】影响「各档概率」这一个口径 —— 不要牵连 FRED"
        "（2026-09-28 实测假缺口）**：\n"
        "   · 目标区间与有效利率来自**另一个源（FRED）**，本机 **可达且已入库**："
        "`fed:effr` / `fed:target_upper` / `fed:target_lower`；\n"
        "   · **禁止**在 `data_gaps` 里写「无法访问 CME/FRED」「联邦基金利率不可得」"
        "「目标区间未提供」这类表述 —— 那是**假缺口**；\n"
        "   · 报缺口时 `status` 必须填 `unavailable`（否则会被缺口队列当成"
        "『可补取』入队，让 A19 去做注定失败的尝试）。\n"
        "   实测现场：某轮结论一边引用「目标区间 3.75%~4.00%、有效利率 3.88%」，"
        "一边声明「当前环境无法访问 CME/FRED」—— 自相矛盾，且缺口队列被污染。",
    ),
)


def render_capability_block(*, compact: bool = True) -> str:
    """渲染成 prompt 用的中文清单块。

    `compact=True` 时省略 note（省 token）；缺口相关的 note 仍保留
    （因为它们直接决定 A17 该不该报"缺失"）。
    """
    lines = [
        "## 系统可用数据能力（★ 判断「数据缺口」前必须先看这里）",
        "",
        "以下是本系统**已接入且可取到**的数据。若你的结论需要某项而"
        "上游分析未提供，请先对照本清单：",
        "  · 清单里**有** → 归为 `available_not_included`"
        "（本次未纳入，可补取），**不要**说成「系统缺失」",
        "  · 标注 **source_terminated** → 归为同类状态，"
        "表述为「该指标已停止披露」",
        "  · 标注 **unavailable** → 归为不可得，如实说明原因",
        "  · 清单里**完全没有** → 才是真缺口",
        "",
    ]
    for cap in CAPABILITIES:
        if cap.status == "available":
            mark = "✅"
        elif cap.status == "source_terminated":
            mark = "⛔"
        else:
            mark = "❌"
        line = f"- {mark} **{cap.name}**：{cap.how_to_get}"
        if not compact or cap.status != "available":
            if cap.note:
                line += f"　（{cap.note}）"
        lines.append(line)
    return "\n".join(lines)


__all__ = ["CAPABILITIES", "Capability", "render_capability_block"]
