"""定时作业注册表（调度单一事实源）。

所有定时任务的crontab只允许在此声明，禁止散落在代码中的sleep循环或硬编码
cron（见 docs/SCHEDULER_DESIGN.md）。Celery Beat与管理API均从本表生成视图。
"""

from __future__ import annotations

import logging
import os
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
    "daily_warm",       # 日K快照预热（消除「点开日K先等 4 秒」的冷加载）
    "dynamic_collection",  # 自修复生成的动态指标采集
    "catalog_collection",  # ★ 第十二轮：catalog 驱动的批量采集（按 frequency 聚合）
    "calendar_sync",       # ★ 第十三轮：投资日历落库（解禁/财报/宏观日程）
    "gap_drain",           # ★ 第十四轮：数据缺口补取（A17 报缺口 → A19 生成连接器）
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
    "intel_hot_rank",     # 各平台人气/热搜榜（接口只读落盘结果，不再实时抓）
    "mainline_warm",      # 主线热快照预热（只读；消除 8~23 秒的冷启动）
    "sector_rotation_report",  # 行业轮动日报：收盘后生成热力图+主力流向+规则研判
    "unlock_plan",         # ★ 限售解禁逐股明细表（月频）：连接器查"未来一个月该股解禁"的取数面
    "data_freshness_audit",  # ★ 定期数据维护：扫陈旧/缺失序列 → 报日志 + 落报告 + 入补采队列
    "derived_metrics",       # ★ 派生指标落库（净息差等）：每次计算都记一行 ⇒ 攒趋势
    "source_leads_audit",    # ★ 换源线索上报：把 path C 搜到的候选源网址送到运维日志
]


@dataclass(frozen=True)
class JobSpec:
    name: str
    cron: str  # 标准5字段: 分 时 日 月 周（周0/7=周日）
    kind: JobKind
    description: str
    params: dict
    #: 本作业**会写哪些存储**（`configs/data_stores.yaml` 里的存储名）。
    #:
    #: ## 为什么作业要声明这个（`CHG-0087`）
    #:
    #: 用户裁定「共享行情仓：**dev 读，pilot 写和读**；谁负责更新数据谁有写权限」。
    #: 落地这件事需要两个动作，而它们必须**同源**：
    #:
    #:   ① **写闸门**：`QuantWarehouse.upsert()` 对不可写的实例 fail-closed；
    #:   ② **调度裁剪**：更新作业在只读实例上**根本不该被触发** ——
    #:      否则它每次都会撞闸门失败，把运行台账刷成一片红（而那不是"故障"，
    #:      是"这台机器本来就不负责这件事"）。
    #:
    #: 把"谁写什么"写在这一处，① 与 ② 都从 `data_stores.writable_here()` 派生 ——
    #: 而不是在 `manage.py` 里手写一份任务黑名单（手写的那份必然漂移：
    #: `MOSS_SCHEDULER_DENY` 就是一个**只被注释提到、从未实现**的开关）。
    #:
    #: 留空 = 不写任何被登记的存储（读侧作业、纯计算作业）。
    updates: tuple[str, ...] = ()


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
        # ★ 写共享行情仓（`strategy_case_store(...).upsert()` → 仓库库里的
        # `quant_strategy_case` 表）。写者归属见 `CHG-0087`：
        # 非写者实例上本作业会被**裁掉**，而不是每班撞一次写闸门
        # （撞闸门会在运行台账里刷出一片"失败"—— 那不是故障，是这台机器不负责写）。
        updates=("warehouse",),
    ),
    "strategy_cases_daily": JobSpec(
        name="strategy_cases_daily",
        cron="20 8 * * *",
        kind="strategy_cases_fetch",
        description=(
            "每日08:20抓取开源策略案例，增量入库供单股票页「开源策略库」展示"
        ),
        params={"sources": []},
        updates=("warehouse",),   # 同上：写共享行情仓
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
    # ── 补齐"有分析需求但没有定期作业"的指标（2026-09-26）──────────────
    # 起因：用户问"美国说年内还会再加息一次，请分析…"，而 us_fed_rate
    # **没有任何作业在采** —— 每次分析都要现打网络，这正是"为什么每次都
    # 先从互联网采集"的一个直接原因。`scripts/data_lifecycle.py --check`
    # 可列出全部这类指标。
    #
    # ⚠️ 关于数据新鲜度的实话：AkShare 的 `macro_bank_usa_interest_rate`
    # 实测**最新有效值是 2025-07-31**，2025-09-18 / 2025-10-30 两行是
    # NaN 占位（上游未回填）。也就是说"美联储当前利率"这个指标在本数据源上
    # 是**陈旧**的，作业只能把已发布的部分入库，不能变出新数据。
    # 分析层会按 DataFreshnessEvaluator 的衰减给出低置信标注，A08 的
    # system_prompt 也要求"缺 FedWatch 时声明数据缺口并按已有数据给方向"。
    "us_macro_daily": JobSpec(
        name="us_macro_daily",
        cron="40 8 * * *",
        kind="generic_indicator_snapshot",
        description=(
            "每日08:40预采集美国宏观指标（CPI同比/核心CPI/非农/失业率/PCE/美联储利率），"
            "入库供宏观分析直接命中。注：AkShare 的美联储利率源最新有效值停在 2025-07-31，"
            "该指标会带陈旧标注，作业不掩盖这一点"
        ),
        params={"task_prefix": "us_macro",
                "indicators": ["us_cpi_yoy", "us_core_cpi", "us_nonfarm",
                               "us_unemployment", "us_pce", "us_fed_rate"]},
    ),
    "financial_ratio_weekly": JobSpec(
        name="financial_ratio_weekly",
        cron="50 16 * * 5",
        kind="generic_indicator_snapshot",
        description=(
            "每周五16:50预采集已覆盖标的的财务比率（资产负债率/流动比率），"
            "供 A10 估值与 A11 财务风险分析直接命中。财报为季频，周频足够及时"
        ),
        params={"task_prefix": "fin_ratio", "indicators": []},  # 标的由作业内解析
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
    # ★ 日K快照预热（`CHG-0092`，用户 2026-09-29 口径）：
    #   「理论上只要服务器开着，到了交易时间，就会自动获取数据，而不是冷加载」。
    #
    # 现状（实测，2026-09-29）：`/api/v1/intraday/daily` **冷 4.19 s / 热 0.07 s**，
    # 而缓存 TTL 只有 180 s、**唯一的写入点就是 `daily()` 自己** ——
    # 交易时段每 5 分钟的 `intraday_t_scan` 走的是分钟级链（`service.watchlist()`），
    # `mainline_warm` 预热的是主线快照，**日K一直没有预热作业**。
    # 于是"服务器一直开着"并不等于"日K是热的"：每次点开都要现场取数 + 250 根 bar
    # 量价规则 + 30 根 bar 信号回放，用户看到的是「正在取日线并跑量价规则…」。
    #
    # 为什么是 `*/2`（120 s）而不是 `*/5`：
    #   **预热间隔必须小于缓存 TTL**（本项目硬约束）。TTL = `daily_snapshot_ttl`
    #   = 180 s，取 120 s 才留出 60 s 余量；取 5 分钟 = 300 s > 180 s，
    #   等于每轮都踩在过期线上（前端面板自己按 180 s 轮询，正好卡在边界）。
    #   这条判据有机器护栏：`tests/unit/test_daily_warm.py`
    #   （从 cron 现读间隔、从配置现读 TTL，不写死数字）。
    #
    # 为什么 cron 覆盖 9~11 / 13~14 而作业内还要再判窗口：
    #   与 `quant_select_*` 同一套路 —— cron 粒度粗一点更抗"服务当时没起来"，
    #   真正的 09:15~11:30 / 13:00~15:00 边界由 `intraday.warm.in_warm_window()`
    #   用 `core.trading_session.WATCH_WINDOWS`（全站唯一权威）判，
    #   窗口外直接 skipped，不留下"跑了但没到点"的记录。
    #
    # 只读：不写库、不写盘、不推送（`updates` 留空），因此**任何实例都能跑** ——
    # 与 `mainline_warm` 同理，多实例并存不会互相踩。
    "daily_warm": JobSpec(
        name="daily_warm",
        cron="*/2 9-11,13-14 * * 1-5",
        kind="daily_warm",
        description=(
            "日K快照预热：交易时段每 2 分钟（< 缓存 TTL 180s）把**自选池 ∪ 最近点开过**"
            "的日K快照算进进程内缓存，让「点开日K」命中 0.07s 的热路径而不是 4.19s 的"
            "冷加载。只读：不写库、不落盘、不推送；并发 3（`CHG-0099` 起计算段已移出"
            "事件循环，实测 max 停顿 188ms）、整轮预算 90s（< tick 120s）、目标封顶 80 只，"
            "超出的按顺序留给下一轮"
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
        # ★ 本作业**就是**行情仓库的更新者 —— 与 `configs/data_stores.yaml` 里
        # `warehouse.writer` 是同一件事的两面（`CHG-0087`，用户裁定
        # 「谁负责更新数据谁有写权限」）。
        #
        # 声明它带来两个自动结果，都不需要第二处清单：
        #   ① `MOSS_SCHEDULER` 在**没有写权限**的实例上不会触发本作业
        #      （否则每次撞写闸门失败，台账刷成一片红 —— 而那不是故障，
        #        是"这台机器本来就不负责更新行情"）；
        #   ② 反过来，谁拿到 `warehouse.writer` 谁就自动获得这个作业的执行权。
        # 依据：2026-09-29 实测 dev 与 pilot 的调度器在**同一分钟**（23:30:0x）
        # 往同一个 14.36 GiB 文件里写 —— 两个写者同时 upsert 同一个 SQLite，
        # 且各自以为自己是唯一写者。
        updates=("warehouse",),
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
    # ★ 限售解禁**逐股明细表**（用户 2026-09-29 口径）：
    #   「按投研分析要查的股票、解禁日期、**解禁数量**落入到数据库**单独的表中**，
    #     **每月自动调度一次**……然后 agent 数据连接对接这个单独的表，
    #     查询未来一个月这个股的解禁数据。**直接精准哈希就找到了**。」
    #
    # 为什么是月频而不是日频：解禁日是**交易所规则确定**的（`certainty=rule`，
    #   不会改期），一个月落一次足够；而每次落库要拉一次全市场明细（实测
    #   120 天视野 577 行、一次调用约 1~2s），日频纯属浪费且会天天抢源。
    # 为什么排 08:20（每月 1 日）：不与 08:30 的周频拥挤度、16:40 的仓库同步抢源，
    #   且赶在开盘前把新一个月的解禁注进表里。
    # 视野为什么 400 天（≈13 个月）：用户口径是"未来一个月"，但月频作业若只落
    #   30 天，月初那几天一过就会出现"窗口有交集却没有行"的**假阴性**
    #   （会被答成"无解禁"）。留足一年多，任何"未来一个月"的查询都落在覆盖内。
    # 幂等：主键 `(code, unlock_date, share_type)` + 内容哈希 ⇒ 重复触发不产生新行、
    #   也不刷新 `fetched_at`。
    "unlock_plan_monthly": JobSpec(
        name="unlock_plan_monthly",
        cron="20 8 1 * *",
        kind="unlock_plan",
        description=(
            "限售解禁逐股明细落库（每月 1 日 08:20）：从投资日历同源（东财个股明细）"
            "落 `app_db::unlock_plan`，字段含股票/解禁日期/解禁数量/解禁市值/占流通比/"
            "解禁前后涨跌幅；连接器 `解禁计划:{code}` 用它做索引点查"
        ),
        params={"horizon_days": 400},
        updates=("app_db",),   # 本环境自己的库（per_env + writer: own）⇒ 每个实例都该跑
    ),
    # ★ 2026-09-29 用户要求：「数据采集 agent 要记录任何未能获取到的信息日志…
    #   **定期维护数据**」。本作业回答的是"**拉回来的够不够新**"——
    #   与 `catalog_*` 批采作业互补：批采可能一直在跑，而某个源自己停更了。
    #   实测证据（本条上线当天就跑出来的，见 PRD §19.22）：
    #     · `us_fed_rate`/`us_nonfarm`/`us_unemployment`/`us_core_cpi`/`us_pce`
    #       **停在 2025-07/08**（陈旧 396~425 天）；
    #     · `社融` 停在 2026-04（181 天）；
    #     · `stock_close` 家族 **487 只**停在 2026-09-14（15 天，批采覆盖不全）。
    #   为什么是 07:45：早于 08:20 的解禁作业与 08:30 的周频拥挤度，开盘前出结论；
    #     它**只读事实表 + 入补采队列**（不自己取数、不写事实表），
    #     所以任何实例都能跑，也不会与写者抢 SQLite。
    "data_freshness_audit": JobSpec(
        name="data_freshness_audit",
        cron="45 7 * * *",
        kind="data_freshness_audit",
        description=(
            "定期数据维护（每日 07:45）：扫登记指标的事实序列，判"
            "陈旧/缺失/频率登记错（`freq_mismatch`）；逐条记 `[数据维护]` 日志、"
            "落 `data/run/data_freshness_report.json`、并把缺口入既有补采队列"
            "（由 `drain_gap_queue` 作业补取）"
        ),
        params={"enqueue": True, "write_report": True},
        updates=(),   # 只读事实表 + 写队列/报告文件 ⇒ 不声明存储写入
    ),
    # ★★ 换源线索上报（`CHG-0118`）：给 path C 的产物一个**消费方**。
    #
    # 为什么必须有这个作业：`discover_candidate_urls()` 把联网搜到的候选数据源
    # 网址写进了 `source_reroute_log.jsonl`，但**此前没有任何东西读它** ——
    # 实测 `grep -r "source_reroute_log|search_candidates" src/` **零消费方**，
    # 即 R2 说的"声明式通路"（写了没人看 = 不存在）。
    #
    # cron 排在 08:20：在 `data_freshness_audit`（07:45，它会入缺口队列）与
    # `gap_drain`（盘后）之间 —— 让"这一夜新搜到的线索"在开盘前就进日志。
    #
    # `updates=()`：**只读**审计、只写自己的上报状态文件，任何实例都能跑。
    "source_leads_audit": JobSpec(
        name="source_leads_audit",
        cron="20 8 * * *",
        kind="source_leads_audit",
        description=(
            "换源线索上报（每日 08:20）：读换源审计里 path C 搜到的候选数据源网址，"
            "**每条只报一次**（状态落盘）、单轮最多 10 条，记 `[换源线索]` 后端日志，"
            "供人工或 A19 复核 ⇒ 把「待复核线索」从 JSONL 里捞出来给人看"
        ),
        params={"max_per_run": 10},
        updates=(),   # 只读审计 + 写状态文件 ⇒ 不声明存储写入
    ),
    # ★★ 派生指标落库（用户 2026-09-30 原话）：
    #   「2 的 **净息差的值，每次记录**有利于**统计变化趋势**，来衡量银行的
    #     收益曲线和映射业绩，因为**银行主要赚息差**。」
    # 为什么必须是作业而不是只留 CLI：趋势靠**按期累积**，靠人手跑必然断档。
    # 为什么 cron 是每月 6 号 08:10：
    #   · 银行季报（3/6/9/12 月末）披露后，`derive` 取到的是**最新一期**，
    #     所以月频足够把"新报告出来"这件事在几天内记下来；
    #   · 排在 `catalog_quarterly`（每月 5 日 09:00）之后 ⇒ 输入先到位再算，
    #     否则每次都在"输入还没采到"时算一遍空（那是自己制造缺口）。
    # 只读输入、写事实表 ⇒ `updates=("app_db",)`。
    "derived_metrics_monthly": JobSpec(
        name="derived_metrics_monthly",
        cron="10 8 6 * *",
        kind="derived_metrics",
        description=(
            "派生指标（净息差等）月度落库：对自选池逐标的按注册公式计算并按"
            "**期间**写一行 ⇒ 攒出可统计的变化趋势（银行主要赚息差）"
        ),
        params={"specs": None, "codes": None, "max_codes": 60},
        updates=("app_db",),
    ),
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
    # 主线挖掘·热快照预热（18:10）。
    #
    # 排在 `mainline_daily`（17:30）与 `mainline_etf_flow`（17:50）**之后**：
    # 等当天的数据同步与打分都落地了，再把那份快照预热好。
    #
    # ## 它解决什么（实测）
    #
    # 主线快照的全市场重算要 **8.4~22.6 秒**（随 IO 竞争浮动），而面板
    # 一打开就要它。原来的缓存是进程内 300 秒 TTL：过期有一个人挨一次，
    # 每次重启更是第一个访问者替所有人挨一次（一天重启 5 次就是 5 次）。
    #
    # 现在接口按**数据水位线**（`ml_board_bar` 最新交易日）缓存，并且这份
    # 作业 + `mainline_daily` 都会把算好的快照落盘；服务启动时（lifespan）
    # 直接装回内存。于是"重启"不再等于"冷启动"。
    #
    # ## 为什么有了 `mainline_daily` 还要单独一个
    #
    # 日更作业连续失败 3 次会被自动暂停；数据也可能由**别的实例**同步。
    # 那两种情况下热快照会缺，而面板照样一打开就等 8~23 秒。
    # 本作业是**只读**的：不联网、不写 `data/quant`、不落库、不推送 ——
    # 所以它与别的实例并存是安全的，这也是它能作为兜底的原因。
    "mainline_warm": JobSpec(
        name="mainline_warm",
        cron="10 18 * * 1-5",
        kind="mainline_warm",
        description=(
            "主线挖掘·热快照预热：用当前本地数据重算一次全市场评分并落盘"
            "（data/mainline/warm_snapshot.json），让面板与重启后首个请求免于"
            "一次 8~23 秒的全市场重算。只读：不联网、不写 data/quant、不落库、不推送"
        ),
        params={},
    ),
    # 行业轮动日报：收盘后 15:40 主生成（A 股 15:00 收盘，等东财/Tushare
    # 日频结算留出 40 分钟余量），随后每小时补跑到 23:40 —— 补跑是为
    # "主机关机/服务没在跑而错过 15:40"的场景：开机后下一个整点 40 分
    # 自动补上。补跑先查"落盘交易日 vs 应有交易日"，已最新则跳过取数
    # （幂等，9 次空跑的总成本是 9 次本地日历比较）。
    "sector_rotation_report": JobSpec(
        name="sector_rotation_report",
        cron="40 15-23 * * 1-5",
        kind="sector_rotation_report",
        description=(
            "行业轮动与资金流向监控日报：Tushare 东财板块截面（行业热力图+主力净额）"
            "+ 腾讯指数快照 + 大盘资金流，装配成 JSON/HTML 落盘 data/sector_rotation/；"
            "规则生成研判（不用 LLM，确定性可复现）。15:40 主生成，16:40~23:40 "
            "为关机补跑窗口（已最新则跳过）"
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
    #
    # 2026-09-26 新增第三档：**事件告警按 `alert_expire_days`（默认 3 天）
    # 真正删除**（用户口径："事件告警的信息最多保留三天，超过3天的信息
    # 自动溢出删除"）。与读路径的懒过期（置 `expired`）是两层机制：
    # 懒过期只让列表不显示，行还在库里 —— 只有这一档才真正释放数据。
    "data_retention_daily": JobSpec(
        name="data_retention_daily",
        cron="30 3 * * *",
        kind="data_retention",
        description=(
            "数据保留：每日03:30删除早于保留年限（默认10年）的数据点、"
            "清理超过保留天数（默认30天）的新闻缓存，"
            "并删除超过 alert_expire_days（默认3天）的事件告警及其孤儿事件"
        ),
        params={},
    ),
    # 数据源授权到期提醒（用户口径 2026-09-25）：
    #   "5 天开始提醒，发邮件通知到 your_qq_number@qq.com"
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
            "（收件人 TOKEN_ALERT_EMAIL_TO，默认 your_qq_number@qq.com）；"
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
        #
        # ⚠️ 实测（2026-10-01）这批上限**跟不上快讯的周转**：
        # 试点的 18:20 那一班跑完全程只用了 6.2 秒（说明大部分条目的
        # `content_hash` 已经抽过、被短路跳过），而**默认视图那 60 条里
        # 一条模型判定都没有**（`tone_store` 覆盖 0/60）。
        # 成因是池子的构成：`FILTER_POOL=500` 里绝大多数是快讯（实测
        # 一次真实聚合 321 条、其中 newswire 上百条），它们进得快、
        # 3 天窗口一到就走，而一班只抽 120 条。
        # 所以**方向与摘要不能只靠这个作业** —— `build_feed` 现在会在
        # 请求路径上用规则层给方向兜底（`tone.rule_tone_verdict`），
        # 摘要则在没有模型结果时**标明是原文摘录**（`service._summary_of`）。
        # 要提高模型覆盖率，得调这个 `max_items` 或缩短班次间隔，
        # 那是**成本决策**（本机 8B 实测 ~26.5 秒/次调用），不在本轮改动里。
        params={"max_items": 120},
    ),
    # ⚠️ 这个作业是**补上的**。`hot_job.run_once()` 早就写好了，但从来没有
    # 任何调度任务调用它 —— 只有调试脚本 `scripts/_dbg_hot.py` 跑过。
    # 结果是「热门个股 / 热议事件」在页面上永远是空的或极旧，而空的原因
    # **看起来像"上游没数据"**，完全不像"这条链路没接上"（我因此白查了一轮）。
    #
    # 排在 `intel_tone_extract` 之后 7 分钟：先有逐条倾向与摘要，
    # 再有整批聚合 —— 聚合的输入里用得上前者的产出。
    "intel_hot_topics": JobSpec(
        name="intel_hot_topics",
        cron="27 */2 * * *",
        kind="intel_hot_topics",
        description=(
            "平台热议聚合：把各平台**公开快讯**（财联社/富途/东财/同花顺/新浪）"
            "整批喂本地模型，聚出「讨论最集中的个股与事件」，"
            "落 data/intel/hot_topics.json。"
            "⚠️ 股票名有两道闸：模型侧要求名字必须在原文出现；"
            "另有确定性名录匹配（hot_scan，零模型）兜底，"
            "宁可漏也不认错 —— 编一只票用户是看不出来的"
        ),
        # 160 条 ≈ 4 批 ≈ 80 秒。本地 1.5B 带 JSON Schema 约束后单批约 20 秒。
        params={"max_items": 160},
    ),
    # ⚠️ 这个作业补的是**性能事故**，不是新功能。
    #
    # `hot_rank.fetch_hot_rank()` 原本是**接口每次请求实时调**的
    # （`intel._build_heat`），实测 3.0~3.5 秒 —— 而 `intel.py` 的注释
    # 一直声称"热榜走定时任务落库，这里只读结果"。注释与代码相反，
    # 后果是情报流每打开一次等 5~6 秒（`/feed` 无任何缓存）。
    #
    # 现在改成：本作业每 10 分钟落盘一次，接口读落盘结果（0 ms）；
    # 落盘结果超过 10 分钟才允许接口自己现抓一次 —— 因为对外试点实例
    # `MOSS_SCHEDULER_ENABLED=0`，只读落盘会让客户那边的人气榜永远是空的。
    "intel_hot_rank": JobSpec(
        name="intel_hot_rank",
        cron="*/10 8-22 * * *",
        kind="intel_hot_rank",
        description=(
            "各平台人气/热搜榜（东财千股千评为主源，百度热搜/东财人气榜备源），"
            "落 data/intel/hot_rank.json，情报流的「平台热议 · 人气榜」读它。"
            "⚠️ 主源是 T-1 口径、备源偶发 RemoteDisconnected，"
            "取不到时保留上一次结果并如实标注时间，不把页面清空"
        ),
        params={},
    ),
    # 排在热度聚合之后 3 分钟：先把倾向抽好、把方向判出来，
    # 再决定哪些值得弹窗。
    "intel_signal_alert": JobSpec(
        name="intel_signal_alert",
        cron="30 */2 * * *",
        kind="intel_signal_alert",
        description=(
            "情报信号告警：把情报流里**明显利空/利多**的条目推成事件告警，"
            "走既有告警系统的去重、冷却、等级、每人已读与 WebSocket 弹窗。"
            "闸门（用户口径 2026-09-25）：方向明确 + 可信度≥74 + 引擎判为 high "
            "+ 跨源同文合并成一条；**非交易时段只入库不弹窗**"
        ),
        params={"max_items": 40},
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


# ===========================================================================
# 本实例**不该触发**哪些作业（`CHG-0087`）
# ===========================================================================
#
# 背景（2026-09-29 实测）：三个实例的调度器**都在**跑 `quant_data_sync` ——
# `data/dev/scheduler` 33 条（最后 23:30:03）、`data/pilot/scheduler` 43 条
# （最后 23:30:02）、`data/scheduler` 7 条。也就是 dev 与 pilot 在**同一分钟**
# 往同一个 14.36 GiB 的库里 upsert，而且各自以为自己是唯一写者。
#
# 用户裁定：「共享行情仓，dev 读，pilot 写和读。同时看下更新数据是谁负责的，
# **谁负责更新数据谁有写权限**。」
#
# 落地这件事需要两个动作，它们必须**同源**：
#   ① 写闸门（`QuantWarehouse.upsert()` 层，见 `data_stores.writable_here`）；
#   ② 调度裁剪（本段）—— 更新作业在只读实例上**根本不该被触发**，
#      否则它每一班都撞闸门失败，把运行台账刷成一片红；而那不是"故障"，
#      是"这台机器本来就不负责更新行情"。**假红灯和假绿灯一样有害**：
#      它会让下一次真故障淹没在噪音里。
#
# 所以裁剪清单是**派生**的：读 `JobSpec.updates`（作业声明它会写哪些存储）
# × `data_stores.writable_here()`（本实例能不能写）。没有任何手写黑名单 ——
# 手写的那份必然漂移，本仓库的 `MOSS_SCHEDULER_ENABLED` 就是前车之鉴
# （manage.py 里设了它，**零处读取**，从来没关掉过任何任务）。

#: 手工禁用清单的环境变量名（逗号/空格分隔的**作业名**）。
#:
#: 为什么还需要它（明明有了派生裁剪）：派生裁剪只能回答"这个作业声明的更新目标
#: 我能不能写"，答不了"这台机器上我不想让它跑"（排障、灰度、临时止血）。
#: ⚠️ 它**只允许当临时开关**：长期差异必须落进 `data_stores.yaml` 的 `writer`
#: 或 `JobSpec.updates`，否则两台机器为什么不一样就没人说得清了。
SCHEDULER_DENY_ENV = "MOSS_SCHEDULER_DENY"


def _denied_by_env() -> tuple[str, ...]:
    """`MOSS_SCHEDULER_DENY` 里的作业名（原样返回，含不存在的名字）。"""
    raw = os.environ.get(SCHEDULER_DENY_ENV, "")
    return tuple(part for part in
                 (piece.strip() for piece in raw.replace(";", ",").split(","))
                 if part)


def unknown_denied_names() -> tuple[str, ...]:
    """`MOSS_SCHEDULER_DENY` 里**拼错的**作业名。

    为什么单独暴露：写错一个字母的效果是"什么都没发生"，与"本来就不需要禁"
    在日志里长得一模一样 —— 那正是"设了但不生效的开关"的复发形态。
    调用方（`SchedulerService.start`）对非空结果打 warning 并列出可用名字。
    """
    return tuple(name for name in _denied_by_env() if name not in JOB_REGISTRY)


def job_deny_reason(name: str) -> str:
    """本实例**不该触发** `name` 吗？→ 人话理由（`""` = 该触发）。

    ## 为什么只用 `decided=True` 的裁定来裁剪

    `writable_here()` 有"已裁定"与"待裁定"两态。**待裁定的不能拿来关作业** ——
    那等于让一个没人拍过板的口径悄悄停掉生产任务（`writer: main` 的那批共享
    存储还没有归属裁定，硬拒会让它们当场失去写者）。所以：

      · `allowed=False` **且** `decided=True` → 裁剪（口径已定，就该拒）；
      · `allowed=False` 但 `decided=False` → **照跑**，只在 `/health` 里如实展示
        "它其实不该写"（这正是审计项 A7 的诚实表达）。
    """
    spec = JOB_REGISTRY.get(name)
    if spec is None:
        return ""
    if name in _denied_by_env():
        return f"{SCHEDULER_DENY_ENV} 手工禁用"
    if not spec.updates:
        return ""
    from src.infrastructure.catalog.data_stores import writable_here

    blocked: list[str] = []
    for store in spec.updates:
        try:
            decision = writable_here(store)
        except Exception as exc:  # noqa: BLE001 登记表读不到不该炸调度器
            logger.warning("作业%s的更新目标 %r 写权限判定失败：%s", name, store, exc)
            continue
        if not decision.allowed and decision.decided:
            blocked.append(decision.reason)
    if not blocked:
        return ""
    return (f"本作业更新 {'/'.join(spec.updates)}，而本实例没有写权限："
            + "；".join(blocked))


#: ---------- 进程角色：把"重作业"挪出在线 API 进程（`CHG-0139`）----------
#:
#: 环境变量：`MOSS_SCHEDULER_ROLE` ∈ {`api`, `worker`}；**不设 = 全跑**（既有行为）。
ROLE_ENV = "MOSS_SCHEDULER_ROLE"
ROLE_API = "api"
ROLE_WORKER = "worker"

#: ★ 与在线 API **共用同一个事件循环**、且**实测占用最大**的四个**纯后台**作业。
#:
#: ## 依据（`data/pilot/scheduler/runs.jsonl` 的 1847 条真实运行记录，按累计占用排序）
#:
#: | 作业 | 累计秒 | 轮数 | 中位 | 最大 | 性质 |
#: |---|---|---|---|---|---|
#: | `intel_tone_extract` | **24849** | 49 | **304s** | **2037s** | LLM 批量抽取，**零实时性要求** |
#: | `event_alert_intraday` | 13201 | 104 | 86s | 620s | 告警扫描，时效以分钟计 |
#: | `quant_data_sync` | 5943 | 59 | 94s | 211s | 行情仓同步（批） |
#: | `mainline_daily` | 5894 | 2 | **2947s** | 3086s | 主线快照（**单轮 49 分钟**） |
#:
#: 合计 ≈ **50,000 秒** —— 是 `daily_warm`（8500s，已用硬预算治过）的 **6 倍**。
#:
#: ## 为什么必须挪出（2026-09-30 实测事故）
#:
#: 三小时内用户三次报"前端显示后端不可达"。仪器（`access_audit.latency_ms`）显示
#: 13:54–13:59 请求延迟最高 **64.4 秒**、13:58 那分钟 **23 条里 22 条 >1 秒**，
#: 全部最终 200（**排队，不是报错**）⇒ 事件循环被重活占住 ⇒ 前端 4 秒探针必然超时。
#: 当时只抓到 `daily_warm` 一个；这份清单显示**真正的大头是这四个**。
#:
#: ## 纪律
#:
#: * 这四个必须是**纯后台**（产物落库/落盘，不参与任何同步请求的响应）；
#: * 判据 `tests/unit/test_scheduler_role_split.py` 保证：名字必须在 `JOB_REGISTRY` 里
#:   （改错名 = 该作业**永远不跑**）、`api ∪ worker` == 全表、两者**不相交**；
#: * 角色**未设置时行为与从前逐字一致** —— 不制造"升级即静默丢作业"。
HEAVY_JOBS: tuple[str, ...] = (
    "intel_tone_extract",
    "event_alert_intraday",
    "quant_data_sync",
    "mainline_daily",
)


def process_role() -> str:
    """本进程的调度角色：`api` / `worker` / `""`（未设 = 全跑）。"""
    return (os.environ.get(ROLE_ENV) or "").strip().lower()


def job_out_of_role(name: str) -> str:
    """本进程**角色**是否负责 `name`？→ 人话理由（`""` = 负责）。

    ★ 它必须是一个**独立可调用**的判据，而不是只写在 `schedulable_jobs()` 里 ——
    因为触发作业的路**不止一条**（`CHG-0087` 为同一件事付过一次代价）：

        ① `SchedulerService._tick()`   每分钟的定时路径；
        ② `SchedulerService.trigger()` **手工/启动自检补偿路径**
           （`_check_quant_sync_at_startup` 就是这么补 `quant_data_sync` 的）。

    只把角色过滤写在 ① 里，② 就会**原样绕过去**：API 进程启动自检发现"行情有缺口"
    ⇒ 直接在**在线进程**里把 `quant_data_sync` 跑起来 ⇒ 这次拆分白做，
    而且症状与拆分前**一模一样**（前端又报不可达），排查时却会以为"已经挪出去了"。
    所以两条路径都调**同一个函数**（判据：`test_scheduler_role_split.py` 里
    有一条专门盯 `trigger()`）。
    """
    role = process_role()
    if role == ROLE_API and name in HEAVY_JOBS:
        return (f"本进程角色 role={ROLE_API}（在线 API）⇒ 重作业已移出本进程，"
                f"由独立 worker 执行（`manage.py start-worker`）")
    if role == ROLE_WORKER and name not in HEAVY_JOBS:
        return (f"本进程角色 role={ROLE_WORKER}（调度 worker）⇒ 只执行 "
                f"{len(HEAVY_JOBS)} 个重作业，在线作业由 API 进程执行")
    return ""


def schedulable_jobs() -> dict[str, JobSpec]:
    """本实例**实际可触发**的作业（= 全表 − 派生裁剪 − 手工禁用 − 进程角色外）。

    单一入口：`SchedulerService` 的 `start()` 与 `_tick()` 都从这里取，
    所以"启动时打印的条数"与"每分钟真正遍历的条数"**不可能不一致**
    （原来 `start()` 打 `len(JOB_REGISTRY)`，`_tick()` 自己也遍历全表 ——
    两处各自为政，任何裁剪都只会在其中一处生效）。

    ★ `CHG-0139`：再叠一层**进程角色**过滤 —— API 进程不跑 `HEAVY_JOBS`，
    worker 进程只跑 `HEAVY_JOBS`（未设角色 = 全跑，保持既有行为）。
    角色判据来自 `job_out_of_role()`（**同一个函数**也被 `trigger()` 调用）。
    """
    out: dict[str, JobSpec] = {}
    for name, spec in JOB_REGISTRY.items():
        if job_deny_reason(name):
            continue
        if job_out_of_role(name):
            continue
        out[name] = spec
    return out


def worker_requirement(role: str | None = None) -> dict[str, Any]:
    """本进程**是否需要**一个独立 worker，以及"现在到底有没有"。

    ## 为什么把这两个问题放在同一个函数里

    它们是同一个判断的两半：`role=api` ⇒ 那 4 个重作业被**移出本进程** ⇒
    "有没有 worker"直接决定"它们此刻跑不跑"。分开写就会出现
    「横幅说重作业已移出、但没人检查移出之后谁在跑」——**拆分最典型的静默失效**。

    ★ 三态，不许合并成 bool（`worker_heartbeat` 的 docstring 有表）：
    从来没有过 / 活着 / 起过但停了 —— 处置动作完全不同。
    """
    from src.scheduler.worker_heartbeat import INTERVAL_SEC, read_status

    who = role if role is not None else process_role()
    needed = who == ROLE_API and bool(HEAVY_JOBS)
    hb = read_status()
    #: ★ "活着"与"刚刚还在跳"必须分开（`CHG-0141` 上线实测踩到）：
    #: 陈旧阈值是 90 秒，所以一个**已经死了 86 秒**的 worker 仍在阈值内 ——
    #: 只说 `alive` 就会让横幅印出「**在跑**」这种**假绿结论**。
    #: `fresh` 用 2 个写间隔（40 秒）当"确实在跳"的证据，且**阈值只有一处**
    #: （`INTERVAL_SEC`，不在这里抄数字）。
    fresh = bool(hb["alive"]) and hb["age_sec"] is not None \
        and hb["age_sec"] <= 2 * INTERVAL_SEC
    return {
        "needed": needed,
        "role": who or "all",
        "heavy_jobs": list(HEAVY_JOBS),
        **hb,
        "fresh": fresh,
        # 一句话结论：**只在需要 worker 时才提"无人执行"**，否则 dev 上会天天喊狼
        "ok": (not needed) or bool(hb["alive"]),
        "verdict": (
            "本进程不需要 worker（未设角色或本身是 worker）" if not needed
            else hb["note"]
        ),
    }


def scheduler_scope_report() -> dict[str, Any]:
    """本实例的调度视图（`/health` / 启动日志 / 排障用）。

    返回 `total` / `active` / `pruned`（每项带**人话理由**）/ `out_of_role` /
    `worker`（`CHG-0139`：重作业被移出本进程后，**谁在跑它们**）。
    """
    pruned = []
    for name in JOB_REGISTRY:
        reason = job_deny_reason(name)
        if reason:
            pruned.append({"job": name, "reason": reason})
    role = process_role()
    #: 角色外（不是"被裁"，是"由另一个进程负责"）—— 单独列出，
    #: 否则横幅会把"worker 负责"读成"这台实例裁掉了这几个作业"。
    #: 判据与 `schedulable_jobs()` **同源**（`job_out_of_role`）：两处各写一份
    #: 的话，报告会与真实遍历集不一致 —— 而那正是"报告说没事、实际有作业没跑"。
    out_of_role = [
        name for name in JOB_REGISTRY
        if not job_deny_reason(name) and job_out_of_role(name)
    ]
    return {
        "env": (os.environ.get("MOSS_ENV") or "dev").strip().lower() or "dev",
        "role": role or "all",
        "total": len(JOB_REGISTRY),
        "active": len(JOB_REGISTRY) - len(pruned) - len(out_of_role),
        "pruned": pruned,
        "out_of_role": out_of_role,
        "unknown_denied": list(unknown_denied_names()),
        # ★ 角色外的作业**有没有人在跑**：这是拆分自带的新问题，答案必须在
        #   **同一个报告里**（否则读者要先知道去别处查，就等于没查）。
        "worker": worker_requirement(role),
    }


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
