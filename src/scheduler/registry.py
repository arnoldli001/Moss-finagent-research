"""定时作业注册表（调度单一事实源）。

所有定时任务的crontab只允许在此声明，禁止散落在代码中的sleep循环或硬编码
cron（见 docs/SCHEDULER_DESIGN.md）。Celery Beat与管理API均从本表生成视图。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

JobKind = Literal["graph_snapshot", "run_log_cleanup"]


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
}

# 指数退避重试间隔（秒）：1分钟→5分钟→15分钟，最多3次（设计文档三）
RETRY_COUNTDOWNS = (60, 300, 900)
MAX_RETRIES = 3
# 连续失败此次数后作业自动暂停（设计文档四）
PAUSE_AFTER_CONSECUTIVE_FAILURES = 3


def get_job(name: str) -> JobSpec:
    if name not in JOB_REGISTRY:
        raise KeyError(f"未注册的调度作业: {name}")
    return JOB_REGISTRY[name]
