"""板块概念拥挤度：配置加载与共享常量。

## 模块定位

`src/sector_crowding/` 是本功能的实现包（与项目既有 `src/<模块>/` 分层一致：
api/db/refresh 三层，路由挂到 `src/api/routes/sector_crowding.py`）。
`sector_crowding/config.yaml` 是唯一可调参数入口（数据库、窗口、阈值、并发）。

## 口径摘要（详见 docs/SECTOR_CROWDING.md）

    板块每日成交额   = 板块指数当日成交额（ths_daily: vol × avg_price）
    全市场每日成交额 = 全部A股当日成交额之和（本地仓库 quant_daily）
    原始拥挤度       = 板块成交额 / 全市场成交额
    平滑拥挤度       = 原始拥挤度的 MA5（可配置）
    拥挤度水位       = 平滑拥挤度 / 近6年平滑拥挤度最大值 × 100%

分母固定为**近 6 年**（上一轮是 5 年，本轮按要求统一改 6 年）。
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from src.core.errors import (
    BRIEF_DEFAULT,
    BRIEF_TIGHT,
    brief,
)

logger = logging.getLogger(__name__)

#: 仓库根（config.yaml 在 sector_crowding/ 下，根目录是它的上一级）
PROJECT_ROOT = Path(__file__).resolve().parents[2]

#: 配置路径：允许环境变量覆盖（便于测试与多环境）
CONFIG_PATH = Path(os.environ.get(
    "SECTOR_CROWDING_CONFIG", PROJECT_ROOT / "sector_crowding" / "config.yaml"))


@dataclass
class DatabaseConfig:
    path: str = "data/moss_finagent.db"
    warehouse_path: str = "data/quant/warehouse.db"
    #: 主线挖掘缓存库（只读）。拥挤度从这里取**提纯后**的成分股
    #: （`ml_member_pure`），保证两个子系统用的是同一份名单。
    mainline_cache_path: str = "data/mainline_cache.db"


@dataclass
class WindowConfig:
    max_lookback_years: int = 6
    ma_window: int = 5
    alert_threshold: float = 0.8
    high_alert_threshold: float = 0.9
    min_bars_for_water_level: int = 60


@dataclass
class DataConfig:
    board_source: str = "ths_index+dim_concept"
    alert_only_concepts: bool = True
    non_concept_name_patterns: list[str] = field(default_factory=list)
    non_concept_code_prefixes: list[str] = field(default_factory=list)
    fetch_chunk_years: int = 3


@dataclass
class PerformanceConfig:
    worker_threads: int = 8
    retry_times: int = 2
    progress_every: int = 5


@dataclass
class MetricConfig:
    """周频异动指标（前端 4 列）的窗口与口径。

    "近 N 日"一律用**交易日**计数（不是自然日）：`window_calendar` 用来把
    "近 1 个月"换成交易日，改它会同时影响 1 月/2 月两列与资金净流入口径。
    """

    #: "近 1 个月"折算多少个交易日
    month_trading_days: int = 20
    #: 短窗口（近 5 个交易日）
    short_days: int = 5
    #: 2 个月 = 2 × month_trading_days
    long_month_multiplier: int = 2
    #: 算变化率要求基准日水位**不低于**这个值，否则该列写 NULL。
    #:
    #: 为什么需要：水位 = 平滑拥挤度 / 近 6 年最高，冷门板块的基准水位可以低到
    #: 0.04%，于是"从 0.0004 涨到 0.02"就得到 +5000% —— 数学上没错，但它只是
    #: 从"几乎没成交"变成"有一点成交"，不是资金异动，却会在按列降序时把小涨幅
    #: 的真异动板块全挤到后面。实测 2026-W38：基准水位 < 0.001 的有 6 个板块，
    #: 其中 871016.TI 算出 +14472%、875185.TI 算出 +705%，而中位数只有个位数。
    #: 0.005（= 0.5%）是"这个板块确实有过成交热度"的最低门槛。
    min_base_water: float = 0.005
    #: 板块成分股（`ths_member`）并发抓取线程数
    member_workers: int = 6
    #: 成分股抓取的重试次数 / 板块成分股缓存的有效天数
    member_retry_times: int = 2
    member_cache_days: int = 30
    #: 资金净流入的分钟间隔/口径：主力净流入 = 大单+超大单净额（Tushare moneyflow）
    flow_weekday: int = 2      # 每周几自动算（cron 周字段：1=周一 … 6=周六）
    #: 是否优先用主线挖掘的**提纯后**成分股（`ml_member_pure`）。
    #:
    #: 关掉它只是为了做「提纯前后」的对照回测，生产口径应当保持 `True`：
    #: 原始 `ths_member` 名单里大量沾边个股会把主力集中度稀释掉
    #: （见 `members.py` 里的压缩比实测）。
    use_purified_members: bool = True
    #: 提纯后 `relevant=1` 的只数低于这个值就回退原始名单。
    #:
    #: 为什么要有下限：提纯里存在"退化"结果（885699 原始 256 只压到 1 只），
    #: 拿单只股票算资金流，噪声远大于信号。实测全池只有 3 个板块低于 5 只。
    min_purified_members: int = 3


@dataclass
class LoggingConfig:
    level: str = "INFO"
    dir: str = "logs"
    file_name: str = "sector_crowding.log"


@dataclass
class SectorCrowdingConfig:
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    window: WindowConfig = field(default_factory=WindowConfig)
    data: DataConfig = field(default_factory=DataConfig)
    performance: PerformanceConfig = field(default_factory=PerformanceConfig)
    metrics: MetricConfig = field(default_factory=MetricConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)

    @property
    def db_path(self) -> Path:
        return _resolve(self.database.path)

    @property
    def warehouse_path(self) -> Path:
        return _resolve(self.database.warehouse_path)

    @property
    def mainline_cache_path(self) -> Path:
        """主线挖掘缓存库路径（只读；提纯成分股的来源）。"""
        return _resolve(self.database.mainline_cache_path)

    @property
    def log_path(self) -> Path:
        return _resolve(self.logging.dir) / self.logging.file_name

    @property
    def ma_window(self) -> int:
        return max(1, int(self.window.ma_window))

    @property
    def lookback_years(self) -> int:
        return max(1, int(self.window.max_lookback_years))


def _resolve(value: str) -> Path:
    """相对路径按仓库根解析（与项目其它模块一致）。"""
    target = Path(value)
    return target if target.is_absolute() else (PROJECT_ROOT / target)


def _build(raw: dict[str, Any]) -> SectorCrowdingConfig:
    def section(name: str) -> dict[str, Any]:
        value = raw.get(name) or {}
        return value if isinstance(value, dict) else {}

    db = section("database")
    window = section("window")
    data = section("data")
    perf = section("performance")
    metric = section("metrics")
    log = section("logging")
    return SectorCrowdingConfig(
        database=DatabaseConfig(
            path=str(db.get("path", "data/moss_finagent.db")),
            warehouse_path=str(db.get("warehouse_path", "data/quant/warehouse.db")),
            mainline_cache_path=str(
                db.get("mainline_cache_path", "data/mainline_cache.db")),
        ),
        window=WindowConfig(
            max_lookback_years=int(window.get("max_lookback_years", 6)),
            ma_window=int(window.get("ma_window", 5)),
            alert_threshold=float(window.get("alert_threshold", 0.8)),
            high_alert_threshold=float(window.get("high_alert_threshold", 0.9)),
            min_bars_for_water_level=int(window.get("min_bars_for_water_level", 60)),
        ),
        data=DataConfig(
            board_source=str(data.get("board_source", "ths_index+dim_concept")),
            alert_only_concepts=bool(data.get("alert_only_concepts", True)),
            non_concept_name_patterns=[
                str(item) for item in (data.get("non_concept_name_patterns") or [])],
            non_concept_code_prefixes=[
                str(item) for item in (data.get("non_concept_code_prefixes") or [])],
            fetch_chunk_years=int(data.get("fetch_chunk_years", 3)),
        ),
        performance=PerformanceConfig(
            worker_threads=int(perf.get("worker_threads", 8)),
            retry_times=int(perf.get("retry_times", 2)),
            progress_every=int(perf.get("progress_every", 5)),
        ),
        metrics=MetricConfig(
            month_trading_days=int(metric.get("month_trading_days", 20)),
            short_days=int(metric.get("short_days", 5)),
            long_month_multiplier=int(metric.get("long_month_multiplier", 2)),
            min_base_water=float(metric.get("min_base_water", 0.005)),
            member_workers=int(metric.get("member_workers", 6)),
            member_retry_times=int(metric.get("member_retry_times", 2)),
            member_cache_days=int(metric.get("member_cache_days", 30)),
            flow_weekday=int(metric.get("flow_weekday", 2)),
            use_purified_members=bool(metric.get("use_purified_members", True)),
            min_purified_members=int(metric.get("min_purified_members", 3)),
        ),
        logging=LoggingConfig(
            level=str(log.get("level", "INFO")),
            dir=str(log.get("dir", "logs")),
            file_name=str(log.get("file_name", "sector_crowding.log")),
        ),
    )


@lru_cache(maxsize=1)
def load_config() -> SectorCrowdingConfig:
    """加载配置（进程内缓存一次；测试可用 `clear_config_cache()` 重置）。"""
    try:
        import yaml

        raw = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        logger.warning("拥挤度配置不存在（用默认值）：%s", CONFIG_PATH)
        raw = {}
    except Exception as exc:  # noqa: BLE001 配置坏了也要能用默认值跑起来
        logger.warning("拥挤度配置读取失败（用默认值）：%s", brief(exc, BRIEF_DEFAULT))
        raw = {}
    config = _build(raw if isinstance(raw, dict) else {})
    _ensure_logger(config)
    return config


def clear_config_cache() -> None:
    load_config.cache_clear()


def _ensure_logger(config: SectorCrowdingConfig) -> None:
    """给本模块的 logger 挂文件 handler（幂等）。

    为什么要独立日志文件：一键刷新是**长任务**，刷新过程中前端只看到进度数字；
    每个板块的起止日期/新增条数/耗时/失败原因必须落到一个可查的文件里，
    否则"某个板块一直没数据"这类问题无从定位。
    """
    target = config.log_path
    module_logger = logging.getLogger("src.sector_crowding")
    for handler in module_logger.handlers:
        if getattr(handler, "_crowding_file", None) == str(target):
            return
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(target, encoding="utf-8")
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s %(message)s"))
        handler._crowding_file = str(target)  # type: ignore[attr-defined]
        module_logger.addHandler(handler)
        module_logger.setLevel(getattr(logging, config.logging.level.upper(),
                                       logging.INFO))
    except OSError as exc:  # noqa: BLE001 日志写不了不该让功能挂掉
        logger.warning("拥挤度日志文件打开失败（仅控制台）：%s", brief(exc, BRIEF_TIGHT))


#: 同花顺板块 type → 是否"概念题材"。
#:
#: 实测（2026-09-18，2517 个板块）：
#:
#:   I  = 行业（777 个）              → **非概念**。靠名称正则判会漏掉
#:        「半导体产品与设备Ⅲ」「化学制品」「金属与采矿」（8xxxxx 段、名称不带"业指数"），
#:        实测 777 个里有 701 个会被名称规则误判成概念。
#:   R  = 地区（33 个）               → 非概念
#:   S  = 统计口径（110 个）           → 非概念（"昨日涨幅超过10%"）
#:   BB = 宽基/全A口径（75 个）        → 非概念（"同花顺全A(加权)"）
#:   ST = 风格（26 个）               → 非概念（"同花顺小盘/大盘/低估值"）
#:   TH = 同花顺自建组合（250 个）     → 非概念（"同花顺金仓30"）
#:   N  = **指数族**（395 个）        → **可能是概念，不能一刀切**
#:        实测反例：「半导体」= 885800.TI 的 type 就是 N —— 它是同花顺的行业指数族，
#:        里面既有"沪深300样本股"（883xxx，非概念）也有"半导体/光刻胶"（885xxx，是概念）。
#:        所以 N 交回名称/代码规则判，靠 "样本股$" 等模式挡掉样本股。
#:   '' = 未知（本地 dim_concept 独有）→ 交回名称/代码规则判
NON_CONCEPT_BOARD_TYPES = frozenset({"I", "R", "S", "BB", "TH", "ST"})


def is_concept_board(code: str, name: str,
                     config: SectorCrowdingConfig | None = None,
                     *, board_type: str = "") -> bool:
    """是否算"概念板块"（用于告警过滤与前端默认筛选）。

    判据是**两条规则相与**，不是"type 命中就短路"：

    1. `board_type` 若属于 `NON_CONCEPT_BOARD_TYPES` → 直接否（权威分类，不看名字）；
    2. 代码前缀（882 地区 / 864 统计 / **861 行业Ⅲ** / 7001-7003 全A口径）；
    3. 名称正则（"样本股$" / "业指数$" / "市指数$" / …）。

    ## 为什么 2、3 必须在 type 之后**继续判**

    `type='N'` 是"同花顺指数族"，里面既有沪深300样本股（883xxx，非概念）
    也有半导体/光刻胶（885xxx，是概念）—— 所以 N 不能一刀切。
    反过来，N 里的样本股仍需靠名称挡掉。若写成"type 不在黑名单就 return True"，
    样本股会全部漏过（实测：`is_concept_board("883300.TI", "沪深300样本股",
    board_type="N")` 返回 True）。
    """
    config = config or load_config()
    kind = str(board_type or "").strip().upper()
    if kind and kind in NON_CONCEPT_BOARD_TYPES:
        return False

    text = str(code or "").strip()
    label = str(name or "").strip()
    for prefix in config.data.non_concept_code_prefixes:
        if prefix and text.startswith(prefix):
            return False
    for pattern in config.data.non_concept_name_patterns:
        if not pattern:
            continue
        try:
            if re.search(pattern, label):
                return False
        except re.error:      # 配置里写了坏正则：忽略该条，不因此判成非概念
            logger.debug("非概念排除正则无效，已跳过：%r", pattern)
    return True


@lru_cache(maxsize=1)
def _cached_exclusions(mtime: float, path: str) -> frozenset[str]:
    """按 (mtime, path) 缓存 —— 文件没改就不重复解析。"""
    import yaml

    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    out = set()
    for item in (raw.get("entries") or []):
        if isinstance(item, dict) and item.get("code"):
            out.add(str(item["code"]))
    return frozenset(out)


def load_crowding_exclusions(filename: str = "crowding_exclusions.yaml"
                             ) -> frozenset[str] | None:
    """拥挤度功能的剔除清单；`None` 表示**清单不可用**（调用方不过滤）。

    ## 与 `sector_blacklist.yaml` 的分工（两个开关，不要混）

        `sector_blacklist.yaml`  → **主线池级**排除：不进 `ml_board`、不参与打分
        本清单                    → **拥挤度功能**排除：不显示 / 不下载 / 不算指标

    用户 2026-09-22 的指示是「**板块拥挤度功能里**，删除以下概念板块」——
    明确是拥挤度口径。所以两边**故意分开**：混在一起会把板块顺带踢出
    主线打分池，那是用户没要求的副作用。

    ## 为什么返回 `None` 而不是空集

    与 `load_sector_blacklist` 同一约定：`None` 表达"清单读不到"，
    由调用方决定怎么办。三处调用方（`refresh` / `metrics` / `db`）都选择
    **不过滤** —— 把"读不到"当成"排除全部"会让拥挤度一个板块都不算、
    前端一片空白，比"多算几个"严重得多。
    """
    path = PROJECT_ROOT / "configs" / str(filename or "")
    if not path.exists():
        logger.warning("拥挤度剔除清单不存在：%s（本次不剔除任何板块）", path)
        return None
    try:
        return _cached_exclusions(path.stat().st_mtime, str(path))
    except Exception as exc:  # noqa: BLE001 清单坏了不该让整条链路挂掉
        logger.warning("拥挤度剔除清单解析失败：%s（本次不剔除任何板块）",
                       type(exc).__name__)
        return None


__all__ = [
    "CONFIG_PATH",
    "PROJECT_ROOT",
    "DataConfig",
    "DatabaseConfig",
    "LoggingConfig",
    "PerformanceConfig",
    "SectorCrowdingConfig",
    "WindowConfig",
    "clear_config_cache",
    "is_concept_board",
    "load_config",
    "load_crowding_exclusions",
]
