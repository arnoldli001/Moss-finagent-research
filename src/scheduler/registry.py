"""定时作业注册表（调度单一事实源）。

所有定时任务的crontab只允许在此声明，禁止散落在代码中的sleep循环或硬编码
cron（见 docs/SCHEDULER_DESIGN.md）。Celery Beat与管理API均从本表生成视图。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

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
    "event_alert_daily": JobSpec(
        name="event_alert_daily",
        cron="30 17 * * 1-5",
        kind="event_alert_scan",
        description=(
            "工作日17:30事件监控扫描：快讯多源+投资日历采集→LLM风险/机会评估"
            "→阈值告警→WebSocket推送与邮件（cron可按需调整）"
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
