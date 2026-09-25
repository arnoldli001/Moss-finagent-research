"""定时作业注册表（调度单一事实源）。

所有定时任务的crontab只允许在此声明，禁止散落在代码中的sleep循环或硬编码
cron（见 docs/SCHEDULER_DESIGN.md）。Celery Beat与管理API均从本表生成视图。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

#: 同步作业该覆盖的日频数据集，与 `sync_gap.DAILY_DATASETS` 同源 ——
#: 避免"面板要读的"和"作业会同步的"两张清单各写一份、慢慢漂开。
from src.quant.sync_gap import DAILY_DATASETS

logger = logging.getLogger(__name__)

JobKind = Literal[
    "graph_snapshot", "run_log_cleanup",
    "industry_valuation_snapshot", "penetration_rate_update",
    "market_intraday_snapshot", "market_daily_snapshot", "fedwatch_daily",
    "board_intraday_snapshot", "board_daily_snapshot",
    "event_alert_scan", "tech_industry_snapshot",
    "intraday_t_scan",  # 做T辅助：自选标的盘中扫描 + 正式信号推送
    "dynamic_collection",  # 自修复生成的动态指标采集
    "strategy_cases_fetch",  # 开源策略案例抓取（多因子页按周 / 单股票页按日）
    "quant_select",      # 量化选股：3 档模型在开盘/尾盘窗口自动选股
    "model_retrain",     # 模型重训：每周非交易时段重训 3 档模型 + 回归报告
    "quant_data_sync",   # 行情仓库同步（量化选股/周频指标的前置条件）
    "crowding_metrics",  # 板块拥挤度周频异动指标（前端 4 列）
    "auction_tick_capture",  # 涨停股竞价过程采集（防过期，每天必跑）
    "data_retention",  # 数据保留：清理超N年数据点与超N天新闻缓存
    "intel_token_alert",  # 数据源授权到期提醒（邮件通知管理员，只发管理员不发用户）
    "intel_zsxq_collect",  # 知识星球增量采集（水位线，2 小时一次，少量多次）
    "intel_tone_extract",   # 原文倾向抽取（本地模型，排在采集之后）
]


@dataclass(frozen=True)
class JobSpec:
    name: str
    cron: str  # 标准5字段: 分 时 日 月 周（周0/7=周日）
    kind: JobKind
    description: str
    params: dict


# 演示环境作业集（付费产业接口接入后，fetch_industry的模拟源自动替换为真实源）
JOB_REGISTRY: dict[str, JobSpec] = {
    "snapshot_macro": JobSpec(
        name="snapshot_macro",
        cron="30 18 * * *",
        kind="graph_snapshot",
        description="每日收盘后跑宏观快照（CPI/PPI采集入库+A08分析），存储幂等",
        params={"analysis_type": "macro", "target": ""},
    ),
    "snapshot_industry_watchlist": JobSpec(
        name="snapshot_industry_watchlist",
        cron="0 17 * * 1-5",
        kind="graph_snapshot",
        description="工作日17:00依次刷新行业观察清单（半导体/煤炭/创新药/白酒）",
        params={"analysis_type": "industry",
                "targets": ["半导体", "煤炭", "创新药", "白酒"]},
    ),
    "run_log_cleanup": JobSpec(
        name="run_log_cleanup",
        cron="0 3 1 * *",
        kind="run_log_cleanup",
        description="每月1日03:00清理超过保留期的运行记录",
        params={},
    ),
    "industry_valuation_snapshot": JobSpec(
        name="industry_valuation_snapshot",
        cron="0 16 * * 1-5",
        kind="industry_valuation_snapshot",
        description="工作日16:00采集申万一/二/三级行业PE/PB截面估值，入库积累历史分位",
        params={},
    ),
    "penetration_rate_update": JobSpec(
        name="penetration_rate_update",
        cron="0 9 1 * *",
        kind="penetration_rate_update",
        description="每月1日09:00更新核心赛道渗透率数据（静态基准+新闻提取+研报搜索）",
        params={},
    ),
    "market_intraday_snapshot": JobSpec(
        name="market_intraday_snapshot",
        cron="*/5 9-11,13-14 * * 1-5",
        kind="market_intraday_snapshot",
        description=(
            "工作日交易时段每5分钟采集两市成交额与全A换手率快照"
            "（腾讯/东财实时源，非交易时段自动取最近交易日）"
        ),
        params={},
    ),
    # 开源策略案例：多因子页「最新开源回测策略」按周抓，单股票页「开源策略库」按日抓。
    # 拆成两个作业是因为两页关注的主题不同（前者偏因子/方法论，后者偏择时/套利），
    # 分开跑的代价只是多几次搜索请求（GitHub 免鉴权限 60 次/小时，够用）。
    "strategy_cases_weekly": JobSpec(
        name="strategy_cases_weekly",
        cron="10 8 * * 1",
        kind="strategy_cases_fetch",
        description=(
            "每周一08:10抓取开源回测策略案例（GitHub/arXiv/CSDN），"
            "增量入库供多因子页「最新开源回测策略」展示"
        ),
        params={"sources": ["GitHub", "arXiv q-fin", "CSDN"]},
    ),
    "strategy_cases_daily": JobSpec(
        name="strategy_cases_daily",
        cron="20 8 * * *",
        kind="strategy_cases_fetch",
        description=(
            "每日08:20抓取开源策略案例，增量入库供单股票页「开源策略库」展示"
        ),
        params={"sources": []},
    ),
    "market_daily_snapshot": JobSpec(
        name="market_daily_snapshot",
        cron="10 16 * * 1-5",
        kind="market_daily_snapshot",
        description=(
            "工作日16:10采集大盘流动性日频数据：成交额60日序列/换手率历史/"
            "两融余额/北向/核心宽基估值分位，入库积累MA与分位序列"
        ),
        params={},
    ),
    "fedwatch_daily": JobSpec(
        name="fedwatch_daily",
        cron="30 9 * * *",
        kind="fedwatch_daily",
        description=(
            "每日09:30采集CME FedWatch下次FOMC利率概率"
            "（外网不可达自动降级为数据缺口，不阻断作业）"
        ),
        params={},
    ),
    "board_cyb_kcb_intraday": JobSpec(
        name="board_cyb_kcb_intraday",
        cron="*/5 9-11,13-14 * * 1-5",
        kind="board_intraday_snapshot",
        description=(
            "工作日交易时段每5分钟采集创业板指/科创50/科创综指实时成交额"
            "（腾讯→东财主备，与大盘流动性盘中作业并行不冲突）"
        ),
        params={},
    ),
    "board_cyb_kcb_daily": JobSpec(
        name="board_cyb_kcb_daily",
        cron="20 16 * * 1-5",
        kind="board_daily_snapshot",
        description=(
            "工作日16:20采集科创板/创业板日频数据：三指数成交额60日序列/"
            "板块整体PE/PB及历史分位/两板个股截面统计（全主备冗余），入库积累序列"
        ),
        params={},
    ),
    # ── 事件告警扫描：三个作业共用同一个 kind（`event_alert_scan`）──────
    # 口径（用户 2026-09-24）：**盘中也要出告警**，开盘、午盘各一次，收盘后再补
    # 一次全量；再加"服务一启动就自动跑一次"（见 `main._run_event_alert_on_startup`）。
    #
    # 为什么按"点位"而不是"每 30 分钟"：一次扫描要打多路快讯源 + 逐条过 LLM
    # （有时间与 token 成本），盘中只在信息增量最大的几个点跑就够：
    #   开盘 09:30（隔夜/竞价消息落地）→ 10:30（早盘发酵）→ 午盘 12:00
    #   （午休时段消化上午盘面，13:00 开盘前告警已到位）→ 13:30（午后开盘）
    #   → 14:30（尾盘前的决策窗口）→ 17:30（收盘后全量复盘）
    # 三个作业共用同一把扫描锁（`ScanInProgressError` → 记 skipped 不记失败），
    # 启动补扫与之撞上也只是跳过，不会双跑。
    "event_alert_intraday": JobSpec(
        name="event_alert_intraday",
        cron="30 9,10,13,14 * * 1-5",
        kind="event_alert_scan",
        description=(
            "工作日盘中事件监控扫描（开盘09:30/10:30/13:30/14:30）："
            "快讯多源+投资日历采集→LLM风险/机会评估→阈值告警"
            "→WebSocket推送与邮件"
        ),
        params={},
    ),
    "event_alert_noon": JobSpec(
        name="event_alert_noon",
        cron="0 12 * * 1-5",
        kind="event_alert_scan",
        description=(
            "工作日午盘12:00事件监控扫描（午休时段跑，13:00开盘前推送到位）："
            "快讯多源+投资日历采集→LLM风险/机会评估→阈值告警"
        ),
        params={},
    ),
    "event_alert_daily": JobSpec(
        name="event_alert_daily",
        cron="30 17 * * 1-5",
        kind="event_alert_scan",
        description=(
            "工作日17:30收盘后事件监控扫描（全量复盘）：快讯多源+投资日历采集"
            "→LLM风险/机会评估→阈值告警→WebSocket推送与邮件"
        ),
        params={},
    ),
    "tech_industry_daily": JobSpec(
        name="tech_industry_daily",
        cron="30 16 * * 1-5",
        kind="tech_industry_snapshot",
        description=(
            "工作日16:30采集科技行业PE-TTM（中证全指半导体H30184，"
            "日频快照积累历史序列），走完整清洗校验入库链路"
        ),
        params={"indicators": ["ind:科技行业PE(TTM)"]},
    ),
    "tech_industry_monthly": JobSpec(
        name="tech_industry_monthly",
        cron="30 9 20 * *",
        kind="tech_industry_snapshot",
        description=(
            "每月20日09:30采集科技行业月度产业数据：WSTS全球半导体销售额同比、"
            "国家统计局集成电路产量同比（月频，等待双方中旬数据发布后取数）"
        ),
        params={
            "indicators": [
                "ind:半导体销售额同比", "ind:芯片出货量同比",
            ]
        },
    ),
    "intraday_t_scan": JobSpec(
        name="intraday_t_scan",
        cron="*/5 9-11,13-14 * * 1-5",
        kind="intraday_t_scan",
        description=(
            "做T辅助：工作日交易时段每5分钟扫描自选标的（configs/intraday.yaml watchlist），"
            "重算七因子总分与做T信号；触发实心三角/止损时经飞书·钉钉·企业微信机器人"
            "（未配置则回落邮件）推送到手机，同标的同方向30分钟冷却去重"
        ),
        params={},
    ),
    # 量化选股（用户口径 2026-09-18）：每个 A 股开市日 09:25–09:45 与 14:45
    # 各跑一轮，用那 3 个按流通市值分档的已训练模型选出标的，结果进前端
    # 「量化选股」模块（**不**自动写自选池，由用户一键加自选）。
    #
    # 为什么拆成两个作业而不是一个 */5 全时段循环：
    #   - 一轮要几十秒~几分钟（全市场 800 只 × 26 因子），全天跑既没人看又抢数据源；
    #   - 两个窗口的**语义不同** —— 开盘那次是"今天做谁"，尾盘那次是"隔夜留谁"；
    #   - 作业级 run_log/暂停/重试直接复用调度器既有能力，不用自己写状态机。
    # 分钟粒度取 */5 + 作业内判窗口：cron 写死单分钟（如 25 9）会因
    # "服务当时没起来/上一轮还没跑完"直接漏掉整个窗口。
    "quant_select_open": JobSpec(
        name="quant_select_open",
        cron="*/5 9 * * 1-5",
        kind="quant_select",
        description=(
            "量化选股·开盘：开市日 09:25~09:45 用 3 档模型选股，结果进「量化选股」模块"
            "（窗口内只跑一轮；不自动写自选池）"
        ),
        params={"window": "open", "start": "09:25", "end": "09:45"},
    ),
    "quant_select_close": JobSpec(
        name="quant_select_close",
        cron="*/5 14 * * 1-5",
        kind="quant_select",
        description=(
            "量化选股·尾盘：开市日 14:45 用 3 档模型选股，结果进「量化选股」模块"
            "（窗口内只跑一轮；不自动写自选池）"
        ),
        params={"window": "close", "start": "14:45", "end": "15:00"},
    ),
    # 行情仓库同步（**量化选股的前置条件**）。
    #
    # 为什么必须有它：量化选股读的是本地 Tushare 仓库，而仓库不会自己更新。
    # 实测事故：2026-09-18 09:10 手动选股，选出来的日期是 20260915 ——
    # 因为仓库里最新就到 0915（0916/0917 没人同步过）。选股逻辑没错，
    # 它忠实地用了"手上最新的一天"，但那是两天前的市场，界面上完全看不出来。
    #
    # 时点：**工作日 16:00~23:30 每 30 分钟一次**（2026-09-23 起，原来是 16:40 一天一次）。
    #
    # 为什么改成重试节奏：
    #   - Tushare 的 EOD 数据 15:00~16:00 才入库，且**各数据集发布时刻不同** ——
    #     `moneyflow` 实测比 `daily` 晚约 2 个交易日才全。一天只跑一次，
    #     那次撞上"还没发布"就只能等**明天**，中间用户在页面上看到的是
    #     「数据滞后」而不是自动自愈；
    #   - 本作业**幂等**：没有缺口时只做几次水位查询就返回（不下载、不写库），
    #     所以提高频率的代价很小；
    #   - ⚠️ 16:00 之前的轮次基本是空转：`latest_complete_trade_date()` 的
    #     发布线是 16:30（`freshness.EOD_RELEASE`），早于它"应该有的最新一天"
    #     还是昨天。所以区间从 16 点起，而不是更早。
    "quant_data_sync": JobSpec(
        name="quant_data_sync",
        cron="*/30 16-23 * * 1-5",
        kind="quant_data_sync",
        description=(
            "行情仓库同步：工作日 16:00~23:30 每 30 分钟把日频数据集补齐到最近一个"
            "已收盘交易日（先补下载缺失分区 → 再按**每档自己的水位**灌库）。"
            "缺了它，次日 09:25 的量化选股会用几天前的旧行情，板块拥挤度的"
            "资金净流入口径也会停在上周"
        ),
        # ⚠️ 这张清单必须覆盖 `src/quant/panels.py` **实际会读**的全部日频数据集。
        # 原来只有 daily/daily_basic/stk_limit/moneyflow 四个，漏了
        # adj_factor（复权因子）、index_daily（相对强度基准）、suspend_d（停牌）、
        # bak_daily（内盘外盘等特色字段）。漏同步的后果是**静默降级**而不是报错：
        # `adj_factor` 缺数据时价格不复权，面板照算不误，只在除权日出现假跳空。
        params={"datasets": list(DAILY_DATASETS)},
    ),
    # 板块拥挤度周频异动指标（前端告警面板的 4 列）。
    #
    # 为什么按周、为什么是周三 08:30：
    #   - 第 4 列要按板块抓成分股（`ths_member`，900+ 板块全量约 4 分钟）再回仓库
    #     聚合近 20 日净流入，**每天算既慢又几乎是同一个数**；
    #   - 它依赖的 `quant_moneyflow` 实测比 `quant_daily` 晚约 2 个交易日才落仓，
    #     周二上午算的时候最新资金流通常还停在上一周（周二才齐周五的），
    #     所以排周三 08:30 —— 既不与 16:40 的仓库同步抢数据源，又赶在开盘前。
    # 幂等：同一 ISO 周已算过就跳过（`force=False`），重复触发不会白跑一遍。
    "crowding_metrics_weekly": JobSpec(
        name="crowding_metrics_weekly",
        cron="30 8 * * 3",
        kind="crowding_metrics",
        description=(
            "板块拥挤度·周频异动指标：每周三 08:30 计算并落库"
            "「近5日/近1月/近2月拥挤度变化 + 近1月资金净流入占流通市值比」4 列，"
            "供资金流监控→板块拥挤度面板排序查看（同一周已算过则跳过）"
        ),
        params={"force": False},
    ),
    # 涨停股竞价过程采集（用户口径 2026-09-22）：**防过期**，每个交易日必跑。
    #
    # 为什么是 09:40 而不是收盘后：竞价过程（09:15~09:25 每 3 秒）在 QMT 服务器上
    # 只保留约 1 个月，而 09:25 那一刻的完整序列**只有当天能取**——生产自己也是
    # 靠盘中实时推、当天落盘（录像带 `auction_snapshot`）才留下的。
    # 09:40 跑：当天竞价刚结束（09:31 之后 QMT 才归档），既能把**今天的**采到，
    # 又能把"最近 5 个交易日里还缺的"补齐（机器没开/服务没跑都不怕）。
    # 仓库 `daily` 的同步在 16:30，所以当天盘中拿不到当天涨停名单 ——
    # 作业按"仓库里已有的最近交易日"逐个回补，天然对上"上一个交易日的涨停股"。
    "auction_tick_capture": JobSpec(
        name="auction_tick_capture",
        cron="40 9 * * 1-5",
        kind="auction_tick_capture",
        description=(
            "涨停股竞价过程采集（防过期）：工作日 09:40 采集当日涨停股的"
            "09:15~09:25 逐 3 秒竞价过程，并回补最近 5 个交易日缺口；"
            "落盘 data/auction_hist/tick_auction_<日期>.parquet"
        ),
        params={"lookback": 5, "scope": "candidates"},
    ),
    # 模型重训（用户口径 2026-09-18）：每周**非交易时段**重训 3 档模型 +
    # 回归测试报告 + 过闸门才替换。周六 20:00 —— "晚上6点后或早上9点前"都满足，
    # 且周六天然是非交易日；作业内还会再判一次交易日（节假日调休时兜底）。
    "model_retrain_weekly": JobSpec(
        name="model_retrain_weekly",
        cron="0 20 * * 6",
        kind="model_retrain",
        description=(
            "模型重训：每周六 20:00（非交易时段）重训 20-150/150-500/500亿+ 三档模型，"
            "逐档对比 AUC/RankIC 回归测试，过闸门才原子替换，报告落 "
            "moss_selector/data/retrain_report_latest.json"
        ),
        params={"dry_run": False},
    ),
    # 主线挖掘·每日盘后（需求 7.3 的"每日运行流程"）。
    #
    # 为什么是 17:30 而不是需求写的 15:30：
    #   15:30 时点只能拿到行情，而本模块第二层三维依赖的
    #   `margin_detail`（两融）与 `top_inst`（龙虎榜）实测要到傍晚才发布；
    #   15:30 跑会得到一份"两融与龙虎榜全空"的评分，第二层三维里两维直接
    #   缺席（`available=False`），当日告警质量显著低于次日回看。
    #   宁可晚两小时拿到完整数据，也不要准点产出一份残缺的告警。
    # 幂等：同一交易日重复触发只会 upsert 覆盖，不会产生重复告警行。
    "mainline_daily": JobSpec(
        name="mainline_daily",
        cron="30 17 * * 1-5",
        kind="mainline_daily",
        description=(
            "主线挖掘·每日盘后：17:30 同步 tushare 日频（板块指数/资金流/两融/"
            "北向/龙虎榜/宏观/期货）→ 三层漏斗打分 → 生成告警 → 推送 → 落库"
        ),
        params={"sync_days": 5, "notify": True},
    ),
    # ETF 份额监控·每日盘后（17:50，排在主线日更之后）。
    #
    # 为什么不是 15:30：`fund_share` 的份额数据次日 8:30 左右才更新，
    # 盘后立刻拉拿到的仍是**上一交易日**的份额。放在 17:50 只是为了让面板
    # 早上打开就有数据，并不意味着当天份额已经可得。
    # 因此本作业产出的信号本质是 T+1 确认，**不适合日内做 T**。
    #
    # 幂等：`alert_id` = 日期+代码+类型，重复触发只 upsert 覆盖。
    "mainline_etf_flow": JobSpec(
        name="mainline_etf_flow",
        cron="50 17 * * 1-5",
        kind="mainline_etf_flow",
        description=(
            "ETF 份额监控·每日盘后：读宽基 ETF 份额与指数分位 → 判定市场环境"
            "（近120日收益）→ 生成机会/风险/行业反转信号 → 落库"
            "（机会信号仅在熊市放行，其余环境降级为观察）"
        ),
        params={},
    ),
    # 期货映射表季度校准（需求 5.3 / 8.11）：每季度末月首个周六 09:00。
    # 放在非交易日是为了不与盘后链路抢 Tushare 频次。
    "mainline_futures_calibrate": JobSpec(        name="mainline_futures_calibrate",
        cron="0 9 1-7 1,4,7,10 6",
        kind="mainline_calibrate",
        description=(
            "主线挖掘·期货映射校准：每季度（1/4/7/10 月首个周六 09:00）"
            "用过去 250 个交易日的滚动相关系数重算品种↔板块强度，"
            "结果写 calibrated_strength 而**不覆盖**人工设定的 strength"
        ),
        params={},
    ),
    # 数据保留（用户口径：最多保留最近 10 年）：每日 03:30 低峰清理。
    # 作业幂等：无超期数据时删除 0 行；启动钩子另跑一次，保证长期没开定时
    # 任务的环境重启时也会收敛。
    "data_retention_daily": JobSpec(
        name="data_retention_daily",
        cron="30 3 * * *",
        kind="data_retention",
        description=(
            "数据保留：每日03:30删除早于保留年限（默认10年）的数据点，"
            "并清理超过保留天数（默认30天）的新闻缓存"
        ),
        params={},
    ),
    # 数据源授权到期提醒（用户口径 2026-09-25）：
    #   "5 天开始提醒，发邮件通知到 1027312283@qq.com"
    #
    # 为什么必须提前提醒而不是失效后报警：token 是 opaque（无 exp 可读），
    # 实测有效期 7–14 天。失效后才发现 = 采集中断 —— 历史上就这样
    # **静默停了 10 天**（2026-09-15 之后无产出也无人知）。
    #
    # 07:30 起跑：早于全部盘前任务（08:05 起），到期信息在开盘前进邮箱。
    # 分层可见：邮件只发管理员；用户侧始终只显示"该来源暂无更新"。
    "intel_token_alert": JobSpec(
        name="intel_token_alert",
        cron="30 7 * * *",
        kind="intel_token_alert",
        description=(
            "数据源授权到期检查：满 5 天起邮件提醒管理员重新授权"
            "（收件人 TOKEN_ALERT_EMAIL_TO，默认 1027312283@qq.com）；"
            "同一级别只发一次，重新授权后自动重置。"
            "只发管理员，用户侧无感"
        ),
        # 阈值由 	oken_alerts 模块自己的常量决定（WARN_AFTER=5/STALE=7），
        # **不在这里传** —— 两处各写一套必然漂移，且第一版按参数名传时
        # 与 check_and_notify 的真实签名不符（它只有 root/dry_run/refresh_cmd/force）。
        params={},
    ),
    # 知识星球**增量**采集（用户口径 2026-09-25）：
    #   "上次获取到 A 时间，这次就从当前时间到 A 时间获取；
    #    太多信息会让本地大模型分析很慢，要设上限或缩短周期，少量多次，建议 2 小时一次"
    #
    # 为什么是 2 小时而不是更频繁：
    #   · 单次送本地 qwen3:8b 的条数必须可控（实测 ~30s/批）；
    #     一次灌几百条会把分析阶段拖到超时，**卡住之后整批都拿不到结果**。
    #   · 2 小时一批 → 行情密集时段每批约 30 条，正好在上限内。
    #
    # 为什么用"偶数点"（*/2）而不是交易时段：
    #   知识星球是研报/小作文流，**盘后与夜间同样有内容**，
    #   只在盘中跑会漏掉大量次日盘前要用的材料。
    #
    # ★ 关机恢复：增量靠**水位线**（data/intel/zsxq_cursor.json）驱动 ——
    #   开机后第一次运行会自动"从上次位置追到现在"，无需专门补扫逻辑。
    #   追不完时按 MAX_PAGES 截断并记 `truncated`，界面提示"数据不完整"，
    #   下次继续追（**不静默丢**）。
    "intel_zsxq_collect": JobSpec(
        name="intel_zsxq_collect",
        cron="5 */2 * * *",
        kind="intel_zsxq_collect",
        description=(
            "知识星球增量采集：每 2 小时一次，按水位线只取新内容"
            "（单次上限 30 条 / 回填最多 5 页）；"
            "开机后首次运行自动回填停机期间的内容"
        ),
        params={"max_fetch": 30, "max_pages": 5, "max_analyze": 20},
    ),
    # 原文倾向抽取（P1 第二步）。**排在采集之后 15 分钟** ——
    # 采集刚写完水位线，这边才有一批新条目可抽。
    #
    # 为什么单独一个任务而不是塞进采集里：本地模型单条 ~770ms，
    # 40 条就是 31 秒。混在一起会让采集这个任务的耗时失去意义
    # （它本该是秒级的），也让失败归因变模糊 —— 采集失败与模型失败
    # 是两件事，一个要重试、一个只要下一批补上。
    "intel_tone_extract": JobSpec(
        name="intel_tone_extract",
        cron="20 */2 * * *",
        kind="intel_tone_extract",
        description=(
            "原文倾向抽取：只抽可信度 ≥50 的条目（低可信不做倾向分析，"
            "用户口径 2026-09-25）；结果落 data/intel/tone_results.jsonl，"
            "接口只读不算 —— 否则一页 60 条会给请求加 46 秒。"
            "判定与规则层交叉验证，不一致给「未定」；抽取的股票代码必须"
            "在原文里真实出现（幻觉拦截）"
        ),
        # 120 条 ≈ 92 秒（本地 770ms/条）。为什么不是 40：
        # 池子约 260 条高可信条目，而**默认视图看的是最近 60 条** ——
        # 40 条/2 小时要 13 小时才铺一遍，期间用户看到的多是尚未抽取。
        # 120 条两三批就能把存量铺满，之后每批只处理新增的那几十条。
        params={"max_items": 120},
    ),
}

# ========== 动态作业注册（自修复生成的连接器自动加入） ==========
# 存储在 data/dynamic_connectors/_schedule.json，进程重启后自动恢复

_DYNAMIC_SCHEDULE_FILE = None  # 延迟初始化（避免循环导入）


def _get_dynamic_schedule_path() -> Path:
    return Path(__file__).resolve().parents[2] / "data" / "dynamic_connectors" / "_schedule.json"


def register_dynamic_job(
    name: str, cron: str, indicator: str,
    connector_path: str | None = None,
) -> None:
    """注册一个动态采集作业（自修复成功后调用）。

    同时写入 JOB_REGISTRY（当前进程生效）和 _schedule.json（重启后恢复）。
    """
    import json
    spec = JobSpec(
        name=name, cron=cron, kind="dynamic_collection",
        description=f"动态采集（自修复生成）: {indicator}",
        params={"indicators": [indicator], "connector_path": connector_path},
    )
    JOB_REGISTRY[name] = spec

    # 持久化到 _schedule.json
    try:
        path = _get_dynamic_schedule_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        schedules = {}
        if path.exists():
            schedules = json.loads(path.read_text(encoding="utf-8"))
        schedules[name] = {
            "cron": cron, "indicator": indicator,
            "connector_path": connector_path,
        }
        path.write_text(json.dumps(schedules, ensure_ascii=False, indent=2),
                        encoding="utf-8")
        logger.info("动态作业已注册: %s (%s)", name, cron)
    except Exception as exc:  # noqa: BLE001
        logger.warning("动态作业持久化失败(%s): %s", name, exc)


def load_dynamic_jobs() -> None:
    """进程启动时从 _schedule.json 恢复动态作业注册。"""
    import json
    try:
        path = _get_dynamic_schedule_path()
        if not path.exists():
            return
        schedules = json.loads(path.read_text(encoding="utf-8"))
        for name, cfg in schedules.items():
            spec = JobSpec(
                name=name, cron=cfg["cron"], kind="dynamic_collection",
                description=f"动态采集（自修复生成）: {cfg['indicator']}",
                params={"indicators": [cfg["indicator"]],
                        "connector_path": cfg.get("connector_path")},
            )
            JOB_REGISTRY[name] = spec
        logger.info("从 _schedule.json 恢复 %d 个动态作业", len(schedules))
    except Exception as exc:  # noqa: BLE001
        logger.warning("动态作业恢复失败: %s", exc)

# 指数退避重试间隔（秒）：1分钟→5分钟→15分钟，最多3次（设计文档三）
RETRY_COUNTDOWNS = (60, 300, 900)
MAX_RETRIES = 3
# 连续失败此次数后作业自动暂停（设计文档四）
PAUSE_AFTER_CONSECUTIVE_FAILURES = 3


def get_job(name: str) -> JobSpec:
    if name not in JOB_REGISTRY:
        raise KeyError(f"未注册的调度作业: {name}")
    return JOB_REGISTRY[name]


def _expand_cron_field(field: str, lo: int, hi: int) -> list[int]:
    """把 cron 的单个字段展开成取值列表。

    只支持本注册表实际用到的写法：`*`、`*/n`、`a`、`a,b,c`、`a-b`、`a-b/n`。
    解析不出来就当空（调用方只用于"展示时刻表"，不该因为它把接口打挂）。
    """
    values: set[int] = set()
    for raw in field.split(","):
        part = raw.strip()
        if not part:
            continue
        step = 1
        if "/" in part:
            part, _, raw_step = part.partition("/")
            try:
                step = max(1, int(raw_step))
            except ValueError:
                step = 1
        try:
            if part in ("", "*"):
                values.update(range(lo, hi + 1, step))
            elif "-" in part:
                start, end = (int(x) for x in part.split("-", 1))
                values.update(range(start, end + 1, step))
            else:
                values.add(int(part))
        except ValueError:
            continue
    return sorted(v for v in values if lo <= v <= hi)


def alert_scan_schedule() -> dict[str, Any]:
    """事件告警扫描的时刻表（`/alerts/settings` 与前端展示用）。

    单一事实源仍然是本表里 `kind == "event_alert_scan"` 的 cron —— 这里把
    「分 时」两段展开成 `["09:30", "10:30", ...]`，**就是为了改了 cron 不用再
    改文案**（"改了注册表忘了改提示"是本项目最容易漂的一类 bug）。
    """
    slots: set[str] = set()
    jobs: list[dict[str, str]] = []
    weekday_only = True
    for name, spec in JOB_REGISTRY.items():
        if spec.kind != "event_alert_scan":
            continue
        jobs.append({"job": name, "cron": spec.cron,
                     "description": spec.description})
        fields = spec.cron.split()
        if len(fields) != 5:
            continue
        minute, hour, _dom, _month, dow = fields
        if dow.strip() not in ("1-5", "*"):
            weekday_only = False
        for hour_v in _expand_cron_field(hour, 0, 23):
            for minute_v in _expand_cron_field(minute, 0, 59):
                slots.add(f"{hour_v:02d}:{minute_v:02d}")
    return {
        "jobs": jobs,
        "slots": sorted(slots),
        "weekday_only": weekday_only,
        # 启动补扫与定时班次是两条路径，前端提示里要一起讲清楚
        "startup_scan": True,
    }
