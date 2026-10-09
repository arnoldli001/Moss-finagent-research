"""量化数据仓库（多方言）：把 Tushare 全量历史集中入库，供回测/因子快速查询。

## 为什么要有它（三种取数方式对比）

| 取数方式 | 单次全市场截面 | 说明 |
|----------|---------------:|------|
| Tushare API | 66~248 ms | 受公网与积分限流（500 次/分）约束 |
| CSV.gz 分区 | 10~40 ms | 每交易日一个文件，解压 + pandas 解析 |
| **数据库表** | **5~20 ms** | 建好索引后按日期/代码直接命中 |

回测要反复按"日期区间 × 股票池"取数，本地库比 API 快一个数量级且不受限流；
这也是"集中管理 + 去重"的落地方式。

## 方言无关（SQLAlchemy）

同一套代码支持 **MySQL / PostgreSQL / SQLite**，按配置自动选择：

    MOSS_MYSQL_HOST/PORT/USER/PASSWORD/DATABASE   → MySQL（用户指定）
    或 MOSS_DB_URL=mysql+pymysql://...            → 任意方言的完整 URL
    都没有 → 回退项目自带的 SQLite（零配置）

之所以做成多方言：实测本机 MySQL 在跑但凭据未知（其它项目里都是占位符
`your-db-user`），PostgreSQL 用项目默认 DSN 也连不上。写成方言无关后，
凭据一到位改个环境变量就能切换，不用改代码。

## 表设计（去重靠唯一键，不靠"导入前先查一遍"）

- **一个数据集一张表**（`quant_daily`、`quant_daily_basic`、`quant_fina_indicator`…）；
- **主键/唯一键就是去重键**：
  - 日频：`(trade_date, code)` —— 同一只票同一天只可能一行；
  - 财务：`(code, report_period, ann_date)` —— 保留修正公告的多个版本
    （与 PIT 面板口径一致；只留最新版会让"修正公告之前"的截面凭空少一只票）；
- 写入用**方言原生 UPSERT**（MySQL `ON DUPLICATE KEY UPDATE` /
  PostgreSQL `ON CONFLICT DO UPDATE` / SQLite 同 PG），所以**重复导入幂等**，
  "断了重跑"不会产生重复行；
- 索引：日期、代码、`(code, trade_date)` 复合（最常用的三种查询形态）。
"""
from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from src.core.errors import (
    BRIEF_DEFAULT,
    BRIEF_TIGHT,
    brief,
)

logger = logging.getLogger(__name__)

DEFAULT_DATABASE = "moss_quant"
def _rel(name: str, fallback_is_missing: bool = True) -> str:
    """从 registry 取相对路径（`CHG-0069`）。registry 不可用时返回空串。"""
    from src.infrastructure.catalog.data_stores import store_rel

    return store_rel(name)


DEFAULT_ROOT = _rel("tushare_partitions")
DEFAULT_SQLITE_PATH = _rel("warehouse")


def _warehouse_writer() -> str:
    """登记表里行情仓的写者环境（错误消息里要给出**可照抄**的命令）。"""
    try:
        from src.infrastructure.catalog.data_stores import get_store

        return get_store("warehouse").writer
    except Exception:  # noqa: BLE001 登记表不可用不该影响报错本身
        return "main"

# 数据集 → (表名, 去重键, 日期列)
DATASET_TABLES: dict[str, tuple[str, tuple[str, ...], str]] = {
    "daily": ("quant_daily", ("trade_date", "code"), "trade_date"),
    "daily_basic": ("quant_daily_basic", ("trade_date", "code"), "trade_date"),
    "adj_factor": ("quant_adj_factor", ("trade_date", "code"), "trade_date"),
    "moneyflow": ("quant_moneyflow", ("trade_date", "code"), "trade_date"),
    "stk_limit": ("quant_stk_limit", ("trade_date", "code"), "trade_date"),
    "suspend_d": ("quant_suspend_d", ("trade_date", "code"), "trade_date"),
    "bak_daily": ("quant_bak_daily", ("trade_date", "code"), "trade_date"),
    "index_daily": ("quant_index_daily", ("trade_date", "code"), "trade_date"),
    "fina_indicator_vip": ("quant_fina_indicator",
                           ("code", "report_period", "ann_date"),
                           "report_period"),
    "stock_basic": ("quant_stock_basic", ("code",), "list_date"),
}

# 策略档案表：与行情数据集不同，它按**策略**而不是按日期分区，所以单独定义。
#
# 为什么把策略也放数据库（而不是只留 JSON 文件）：
# "哪些策略真的跑赢过基准"是需要**跨策略查询**的问题（按超额排序、按时段筛、
# 按标的找）。JSON 文件做不到，只能每次全量读进内存再排 —— 策略一多就不可用。
# 表结构与行情表一样走同一条方言无关路径，所以换 MySQL 不需要改代码。
STRATEGY_TABLE = "quant_strategy"
_STRATEGY_COLUMNS: tuple[tuple[str, str], ...] = (
    ("id", "VARCHAR(64)"),
    ("name", "VARCHAR(128)"),
    ("code", "VARCHAR(16)"),
    ("entry_condition", "TEXT"),
    ("exit_condition", "TEXT"),
    ("preset", "VARCHAR(64)"),
    ("spec_hash", "VARCHAR(64)"),
    ("created_at", "VARCHAR(32)"),
    ("updated_at", "VARCHAR(32)"),
    ("start_date", "VARCHAR(16)"),
    ("end_date", "VARCHAR(16)"),
    ("trading_days", "FLOAT"),
    ("total_return_pct", "FLOAT"),
    ("benchmark_return_pct", "FLOAT"),
    ("index_return_pct", "FLOAT"),
    ("excess_vs_benchmark_pct", "FLOAT"),
    ("excess_vs_index_pct", "FLOAT"),
    ("recent_return_pct", "FLOAT"),
    ("recent_excess_pct", "FLOAT"),
    ("oos_return_pct", "FLOAT"),
    ("max_drawdown_pct", "FLOAT"),
    ("sharpe", "FLOAT"),
    ("calmar", "FLOAT"),
    ("trade_count", "FLOAT"),
    ("win_rate_pct", "FLOAT"),
    ("verdict_passed", "FLOAT"),
    ("verdict_total", "FLOAT"),
    ("beats_benchmark", "FLOAT"),
    ("low_sample", "FLOAT"),
    ("search_space", "FLOAT"),
    ("scan_median_excess_pct", "FLOAT"),
    ("scan_beat_ratio", "FLOAT"),
    ("recent_trades", "FLOAT"),
    ("auto_saved", "FLOAT"),
    ("source", "VARCHAR(32)"),
    ("payload", "TEXT"),
    ("disclaimer", "TEXT"),
)

# 文本列（其余按 FLOAT 建列；数值列全部可空）
_TEXT_COLUMNS = frozenset({
    "code", "ts_code", "trade_date", "report_period", "ann_date", "end_date",
    "name", "industry", "area", "exchange", "market", "list_date",
    "suspend_type", "suspend_timing", "symbol", "cnspell", "report_type",
    "update_flag", "act_ent_type", "act_name", "fullname", "enname",
    "cnspell2", "curr_type", "list_status", "delist_date", "is_hs",
})


class WarehouseError(RuntimeError):
    """仓库不可用或操作失败。"""


# ==================================================================
# 配置
# ==================================================================


@dataclass
class WarehouseConfig:
    """仓库连接配置（方言无关）。"""

    url: str = ""
    dialect: str = ""
    description: str = ""

    @classmethod
    def from_env(cls, *, prefix: str = "MOSS_MYSQL",
                 root: str | Path = DEFAULT_ROOT) -> WarehouseConfig:
        """按优先级解析：显式 URL → MySQL 环境变量 → 与 `root` 同级的 SQLite。

        `root` 是 CSV 分区缓存根目录（默认 `data/quant/tushare`）。
        缺省 SQLite 定在 `<root>/../warehouse.db`，所以默认情况下就是
        `data/quant/warehouse.db`；**而传入自定义 root 时会跟着走**
        —— 否则 `build_panels(root=别的目录)` 会静默读到默认库的真实数据，
        把调用方自己的 CSV 测试数据遮掉（这是实际踩到过的坑）。
        """
        for key in ("MOSS_DB_URL", "MOSS_QUANT_DB_URL", "QUANT_DB_URL"):
            value = os.environ.get(key)
            if value and value.strip():
                url = value.strip()
                return cls(url=url, dialect=_dialect_of(url),
                           description=f"环境变量 {key}：{_mask(url)}")

        def pick(name: str, default: str = "") -> str:
            for key in (f"{prefix}_{name}", f"MYSQL_{name}", f"DB_{name}"):
                value = os.environ.get(key)
                if value and value.strip():
                    return value.strip()
            return default

        dotenv = _read_dotenv(root)
        host = pick("HOST") or dotenv.get("host", "")
        user = pick("USER") or dotenv.get("user", "")
        password = pick("PASSWORD") or dotenv.get("password", "")
        database = pick("DATABASE") or dotenv.get("database", DEFAULT_DATABASE)
        port = pick("PORT") or dotenv.get("port", "3306")

        note = ""
        if host and user and password:
            if _looks_placeholder(user) or _looks_placeholder(database):
                # 占位符不能当成可用配置，否则报错会出现在第一次查询时；
                # 这里记下原因后继续走 SQLite，并把原因带进 description。
                note = (f"（检测到 MySQL 配置但用户名/库名是占位符："
                        f"user={user}，database={database}，已忽略）")
            else:
                url = (f"mysql+pymysql://{user}:{password}@{host}:{port}/"
                       f"{database}?charset=utf8mb4")
                return cls(url=url, dialect="mysql",
                           description=f"MySQL {host}:{port}/{database}")

        # ⚠️ 这里**只认 quant 专属开关** `MOSS_QUANT_SQLITE`。
        #
        # 曾经还认通用的 `MOSS_SQLITE_PATH`，那是**应用库**（fact_*/告警/做T权重档案）的开关：
        # `manage.py --env dev`（且 `--env` 缺省就是 dev）会把它指到
        # `data/dev/moss_dev.db` 做隔离，防止调试实例写生产数据。
        # 但行情仓是另一份 15~31 GiB 的**只读行情数据**，应用库隔离不该把它一起带跑 ——
        # 实测后果（2026-09-23 用户报障）：dev 实例下股票字典只剩 4 条
        # （`data/dev/moss_dev.db` 里只有按需补录过的那几只），
        # "输入中文名 / 拼音首字母联想"整段失效（汇成真空 301392、大亚圣象 000910 都"识别不了"），
        # 资金流/流通市值/换手率这些同样读行情仓的字段也一起变空。
        #
        # 真要把行情仓指到别处，用 **quant 专属**的 `MOSS_QUANT_SQLITE`（语义明确、
        # 不会与应用库的隔离互相影响）；上面的 `MOSS_QUANT_DB_URL`/`QUANT_DB_URL` 同理。
        quant_sqlite = os.environ.get("MOSS_QUANT_SQLITE")
        if quant_sqlite and quant_sqlite.strip():
            path = Path(quant_sqlite.strip())
            path.parent.mkdir(parents=True, exist_ok=True)
            return cls(url=f"sqlite:///{path.as_posix()}", dialect="sqlite",
                       description=f"SQLite {path}（环境变量 MOSS_QUANT_SQLITE）{note}")

        override = pick("SQLITE")
        if override:
            path = Path(override)
        else:
            root_path = Path(root)
            if root_path.is_absolute() or os.sep in str(root) or "/" in str(root):
                path = root_path.parent / "warehouse.db"
            else:
                path = Path(DEFAULT_SQLITE_PATH)
        path.parent.mkdir(parents=True, exist_ok=True)
        return cls(url=f"sqlite:///{path.as_posix()}", dialect="sqlite",
                   description=(f"SQLite {path}（零配置；配置 MySQL 后自动切换）"
                                f"{note}"))

    def ready(self) -> bool:
        return bool(self.url)


def _looks_placeholder(text: str) -> bool:
    markers = ("your-", "your_", "xxx", "change", "placeholder", "<", "example")
    lowered = (text or "").lower()
    return any(marker in lowered for marker in markers)


def _dialect_of(url: str) -> str:
    return url.split(":", 1)[0].split("+", 1)[0]


def _mask(url: str) -> str:
    """隐藏 URL 里的密码（日志/前端展示用）。"""
    if "@" not in url:
        return url
    head, _, tail = url.partition("@")
    if ":" in head:
        head = head.rpartition(":")[0] + ":***"
    return f"{head}@{tail}"


def _read_dotenv(root: str | Path = ".") -> dict[str, str]:
    """从项目 .env 读取数据库段（键名大小写不敏感，只认 MOSS_MYSQL_/MYSQL_/DB_）。"""
    path = Path(root) / ".env"
    if not path.exists():
        return {}
    result: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, _, raw = stripped.partition("=")
            key = key.strip().upper()
            if key.startswith(("MOSS_MYSQL_", "MYSQL_", "DB_")):
                result[key.split("_")[-1].lower()] = raw.strip().strip("'\"")
    except OSError:
        return {}
    return result


# ==================================================================
# 仓库
# ==================================================================


@dataclass
class IngestResult:
    """一次入库的结果。"""

    dataset: str
    table: str
    partitions: int = 0
    rows: int = 0
    skipped: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    seconds: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {"dataset": self.dataset, "table": self.table,
                "partitions": self.partitions, "rows": self.rows,
                "skipped": len(self.skipped), "failed": len(self.failed),
                "seconds": round(self.seconds, 1),
                "failed_keys": sorted(self.failed.keys())[:5]}


class QuantWarehouse:
    """Tushare 数据仓库（SQLAlchemy，方言无关；建表/入库/查询全部幂等）。"""

    def __init__(self, config: WarehouseConfig | None = None, *,
                 root: str | Path = DEFAULT_ROOT,
                 universe: str = "a_share") -> None:
        # 配置随 root 走：自定义 root 时用同级目录的库，避免静默读到全局默认库
        self.config = config or WarehouseConfig.from_env(root=root)
        self.root = Path(root)
        self.universe = universe
        self._engine: Any = None
        self._tables: dict[str, Any] = {}
        #: 写权限裁决缓存（`write_decision()`）。哨兵值而不是 `None`：
        #: `None` 是"与登记表无关的路径"这个**有效结论**，不能被当成"还没算"。
        self._write_decision: Any = "unset"

    # ---------- 写权限归属（CHG-0087） ----------

    def _registered_store(self) -> str:
        """本实例指向的是哪条**登记过的**存储（`""` = 与登记表无关的路径）。

        ## 为什么必须比对路径，而不是无条件套用生产口径

        `WarehouseConfig.from_env(root=...)` 会**跟着 root 走**（自定义 root 时
        用同级目录的库）—— 测试、回测、脚本都用这条路径造自己的临时仓库。
        如果无条件按"本环境能不能写 `warehouse`"裁决，那些临时库会一起被拒，
        等于用一个生产口径误伤所有隔离场景。

        所以只在**确实指向登记表里那条 `warehouse`** 时才裁决。
        """
        if self.config.dialect != "sqlite":
            # MySQL/PG 走环境变量显式配置，不存在"两个实例共用一个文件"的问题；
            # 那边的写权限由账号本身表达（且本项目的行情仓就是 SQLite）。
            return ""
        prefix = "sqlite:///"
        url = self.config.url or ""
        if not url.startswith(prefix):
            return ""
        try:
            from src.infrastructure.catalog.data_stores import store_path

            mine = Path(url[len(prefix):]).resolve()
            theirs = Path(store_path("warehouse")).resolve()
        except Exception as exc:  # noqa: BLE001 registry 读不到不该让查询失败
            logger.debug("写权限归属判定跳过（登记表不可用）：%s",
                         brief(exc, BRIEF_TIGHT))
            return ""
        return "warehouse" if mine == theirs else ""

    def write_decision(self) -> Any:
        """本实例写这个仓库的裁决（`None` = 与登记表无关的路径）。**结果缓存**。"""
        if getattr(self, "_write_decision", "unset") != "unset":
            return self._write_decision
        name = self._registered_store()
        decision = None
        if name:
            from src.infrastructure.catalog.data_stores import writable_here

            decision = writable_here(name)
        self._write_decision = decision
        return decision

    def writable_here(self) -> bool:
        """本实例**能否写**这个仓库（口径未裁定时按"能"= 保持原行为）。"""
        decision = self.write_decision()
        return True if decision is None else (decision.allowed or not decision.decided)

    def assert_writable(self) -> None:
        """不能写就**立刻拒**，并说清"为什么、谁是写者、去哪改"。

        ⚠️ 这道显式检查的价值是**消息**，不是**拦截力度** ——
        真正的拦截在 `engine()` 里（`PRAGMA query_only=1`，覆盖全部写路径，
        包括 `StrategyArchive` / `StrategyCaseStore` 那些我没逐个加判断的地方）。
        SQLite 自己那句 `attempt to write a readonly database`
        **不会告诉你谁是写者、也不会告诉你去哪改**，那正是本项目最怕的
        "看得见失败、看不出原因"。
        """
        decision = self.write_decision()
        if decision is None or decision.allowed or not decision.decided:
            return
        from src.infrastructure.catalog.data_stores import store_rel

        raise WarehouseError(
            f"本实例没有行情仓（{store_rel('warehouse')}）的写权限，已拒绝写入。"
            f"原因：{decision.reason}。"
            "只读是**有意**的（同一个 SQLite 文件只允许一个写者）；读取不受影响。"
            "确实需要在这里写（手工补数 / 离线脚本）时，让本进程**声明自己是"
            "写者**再跑，例如："
            f"  MOSS_ENV={_warehouse_writer()} uv run python <脚本>"
            "（改长期归属则改 configs/data_stores.yaml 的 warehouse.writer）")

    # ---------- 连接 ----------

    def available(self) -> bool:
        """真实连一次（`SELECT 1`），而不是只看 URL 拼得对不对。

        MySQL/PostgreSQL 下"库还不存在"是可恢复状态：自动建库后再试一次。
        这样首次入库不需要手工 `CREATE DATABASE`。
        """
        from sqlalchemy import text

        try:
            with self.engine().connect() as connection:
                connection.execute(text("SELECT 1"))
            return True
        except Exception as exc:  # noqa: BLE001 不可用是正常状态（未配置）
            logger.debug("数据仓库不可用：%s", brief(exc, BRIEF_DEFAULT))
        if self.config.dialect in ("mysql", "postgresql"):
            try:
                self.ensure_database()
                with self.engine().connect() as connection:
                    connection.execute(text("SELECT 1"))
                return True
            except Exception as exc:  # noqa: BLE001 建库也不行就真不可用
                logger.debug("建库后仍不可用：%s", brief(exc, BRIEF_DEFAULT))
        return False

    def engine(self) -> Any:
        if self._engine is not None:
            return self._engine
        if not self.config.ready():
            raise WarehouseError(self.config.description or "数据仓库未配置")
        try:
            from sqlalchemy import create_engine, event
        except ImportError as exc:  # pragma: no cover
            raise WarehouseError("缺少 SQLAlchemy：请执行 `uv add sqlalchemy`") from exc
        self._engine = create_engine(self.config.url, future=True,
                                     pool_pre_ping=True)
        if self.config.dialect == "sqlite":
            self._tune_sqlite(self._engine, event,
                              read_only=not self.writable_here())
        return self._engine

    @staticmethod
    def _tune_sqlite(engine: Any, event: Any, *, read_only: bool = False) -> None:
        """SQLite 连接调优（实测宽表扫描快 ~12%）。

        - `cache_size=-200000`：200MB 页缓存（默认仅 2MB，全量历史放不下）；
        - `mmap_size=1GB`：内存映射读，省掉缓冲区拷贝；
        - `journal_mode=WAL` + `synchronous=NORMAL`：**允许入库时并发查询**
          （做T/回测在跑而数据在补的场景），代价是断电时可能丢最后一个事务
          —— 数据可从 CSV 分区完整重放，这个代价可接受。
        - `query_only=1`（仅当本实例无写权限，`CHG-0087`）：**连接级只读闸门**。

        ## 为什么把写闸门放在这一层，而不是在 5 个写入口各加一次判断

        `QuantWarehouse` 里开写事务的地方有 5 处（`upsert`、策略档案落库、
        策略档案水位、策略案例 upsert、案例去重清理），而且**以后还会有第 6 处**。
        逐个加判断的失败模式是"新写路径忘了加"—— 而这个失败**不报错**，
        它表现为"dev 又在偷偷写共享行情仓"。

        `query_only` 是 SQLite 自己的开关，对**该连接的一切写操作**生效，
        所以它覆盖我还没枚举到的路径。实测（`scripts/_probe_query_only_pragma.py`）：
        **先调优、后 query_only** 的顺序下 `journal_mode=WAL` 仍能生效
        （`cache_size`/`mmap_size`/`journal_mode` 都不受它影响），
        读取照常、四种写操作全被拒 —— 所以这个组合既挡得住写，也不会把只读实例打死。

        ⚠️ 只对 SQLite 生效。MySQL/PG 的仓库由**账号权限**表达写权限，
        本函数管不到（而本项目的行情仓就是 SQLite）。
        """
        statements = ("PRAGMA cache_size=-200000", "PRAGMA mmap_size=1073741824",
                      "PRAGMA temp_store=MEMORY", "PRAGMA journal_mode=WAL",
                      "PRAGMA synchronous=NORMAL")
        if read_only:
            # 放最后：调优语句里 journal_mode 在库还不是 WAL 时是一次真写，
            # 排在它前面会让连接直接建不起来（连读都读不了）。
            statements = statements + ("PRAGMA query_only=1",)

        @event.listens_for(engine, "connect")
        def _apply(dbapi_connection: Any, _record: Any) -> None:  # pragma: no cover
            cursor = dbapi_connection.cursor()
            try:
                for statement in statements:
                    cursor.execute(statement)
            finally:
                cursor.close()

    def close(self) -> None:
        if self._engine is not None:
            try:
                self._engine.dispose()
            finally:
                self._engine = None

    def ensure_database(self) -> None:
        """MySQL/PostgreSQL 下目标库不存在就建（其它方言跳过）。

        为什么必须用 AUTOCOMMIT：PostgreSQL **不允许在事务块里执行
        `CREATE DATABASE`**，而 SQLAlchemy 在首次 execute 时会隐式开事务。
        两种方言都显式用 AUTOCOMMIT，避免"本机 MySQL 能过、换 PG 就报
        cannot run inside a transaction block"这种只在别人机器上出现的问题。
        """
        if self.config.dialect not in ("mysql", "postgresql"):
            return
        database = self.config.url.rsplit("/", 1)[-1].split("?", 1)[0]
        if not database:
            return
        # 连到"不带库名"的连接（PG 用内置 postgres 维护库）来建目标库
        maintenance = self.engine().url.set(
            database="postgres" if self.config.dialect == "postgresql" else "")
        from sqlalchemy import create_engine, text

        connection = create_engine(maintenance, future=True,
                                   isolation_level="AUTOCOMMIT")
        try:
            with connection.connect() as handle:
                if self.config.dialect == "mysql":
                    handle.execute(text(
                        f"CREATE DATABASE IF NOT EXISTS `{database}` "
                        f"DEFAULT CHARACTER SET utf8mb4"))
                else:
                    exists = handle.execute(text(
                        "SELECT 1 FROM pg_database WHERE datname = :name"),
                        {"name": database}).fetchone()
                    if not exists:
                        handle.execute(text(f'CREATE DATABASE "{database}"'))
        finally:
            connection.dispose()

    # ---------- 表结构 ----------

    def table(self, dataset: str) -> Any:
        """取（必要时创建）数据集对应的 SQLAlchemy Table。"""
        if dataset in self._tables:
            return self._tables[dataset]
        if dataset not in DATASET_TABLES:
            raise WarehouseError(f"数据集 {dataset!r} 未登记仓库表结构")
        from sqlalchemy import Column, MetaData, String, Table

        name, dedup_keys, date_column = DATASET_TABLES[dataset]
        metadata = MetaData()
        columns = [Column(key, String(32), primary_key=True) for key in dedup_keys]
        table = Table(name, metadata, *columns)
        # 索引要等列反射出来后再补（ensure_indexes）
        self._indexes = getattr(self, "_indexes", {})
        self._indexes[dataset] = (date_column, "code" in dedup_keys)
        self._tables[dataset] = table
        return table

    def ensure_columns(self, dataset: str, columns: Sequence[str]) -> Any:
        """把数据里出现的新列补进表（`ALTER TABLE ADD COLUMN`，幂等）。

        每分区都反射一次 `information_schema` 会拖慢 5000 分区的入库，
        所以用 `self._known` 缓存"已确认存在的列"，只有出现新列才真的查库。
        """
        from sqlalchemy import Table, text

        table = self.table(dataset)
        engine = self.engine()
        _name, dedup_keys, _date = DATASET_TABLES[dataset]
        self._known: dict[str, set[str]] = getattr(self, "_known", {})
        requested = {str(column) for column in columns}
        known = self._known.get(dataset)
        if known is not None and requested <= known:
            return table
        existing = self._existing_columns(dataset, table)
        additions = [column for column in sorted(requested)
                     if column not in existing and column not in dedup_keys]
        if additions:
            quote = "`" if self.config.dialect == "mysql" else '"'
            with engine.begin() as connection:
                for column in additions:
                    ddl_type = ("VARCHAR(32)" if column in _TEXT_COLUMNS
                                else "FLOAT")
                    connection.execute(text(
                        f"ALTER TABLE {quote}{table.name}{quote} "
                        f"ADD COLUMN {quote}{column}{quote} {ddl_type}"))
        refreshed = Table(table.name, table.metadata, autoload_with=engine,
                          extend_existing=True)
        self._tables[dataset] = refreshed
        self._known[dataset] = self._existing_columns(dataset, refreshed,
                                                      create=False)
        if additions:
            # 列集合变了 → 让下一次 upsert 重新确认索引（新列可能带日期/代码列）
            getattr(self, "_indexed", set()).discard(dataset)
        return refreshed

    def _existing_columns(self, dataset: str, table: Any,
                          *, create: bool = True) -> set[str]:
        """表当前的列集合；表不存在时先按去重键建出来（只建一次）。"""
        from sqlalchemy import inspect

        engine = self.engine()
        inspector = inspect(engine)
        if table.name not in inspector.get_table_names():
            if not create:
                return set(table.columns.keys())
            table.create(engine, checkfirst=True)
        return {column["name"] for column in inspector.get_columns(table.name)}

    def create_all(self, dataset: str | None = None) -> list[str]:
        """建表（含索引），幂等。"""
        targets = [dataset] if dataset else list(DATASET_TABLES)
        created: list[str] = []
        for name in targets:
            table = self.table(name)
            table.create(self.engine(), checkfirst=True)
            created.append(table.name)
        return created

    def ensure_indexes(self, dataset: str, columns: Sequence[str]) -> list[str]:
        """在列已确定后补建索引（MySQL/SQLite/PG 语法一致，交给 SQLAlchemy）。"""
        from sqlalchemy import Index, inspect

        table = self.table(dataset)
        present = self._existing_columns(dataset, table)
        wanted: list[tuple[str, list[str]]] = []
        date_column, has_code = getattr(self, "_indexes", {}).get(
            dataset, ("", False))
        if date_column and date_column in present:
            wanted.append((f"idx_{table.name}_date", [date_column]))
        if has_code and "code" in present:
            wanted.append((f"idx_{table.name}_code", ["code"]))
        if "code" in present and "trade_date" in present:
            wanted.append((f"idx_{table.name}_code_date", ["code", "trade_date"]))
        existing = {index["name"] for index in inspect(self.engine()).get_indexes(
            table.name)}
        built: list[str] = []
        for name, columns_used in wanted:
            if name in existing:
                continue
            try:
                Index(name, *[table.c[column] for column in columns_used]).create(
                    self.engine())
                built.append(name)
            except Exception as exc:  # noqa: BLE001 索引已存在等
                logger.debug("建索引 %s 失败（可忽略）：%s", name, brief(exc, BRIEF_TIGHT))
        self._indexed = getattr(self, "_indexed", set())
        self._indexed.add(dataset)
        return built

    # ---------- 入库 ----------

    def ingest_dataset(self, dataset: str, *, keys: Sequence[str] | None = None,
                       batch_rows: int = 2000,
                       progress: Any = None) -> IngestResult:
        """把 CSV 分区缓存灌入库（幂等：重复导入靠唯一键去重）。"""
        from src.quant.dataset_store import DatasetStore

        # 先拒再读分区：否则要读完所有分区才在第一次写时失败（十几 GiB 的白读）
        self.assert_writable()
        if dataset not in DATASET_TABLES:
            raise WarehouseError(f"数据集 {dataset!r} 未登记仓库表结构")
        store = DatasetStore(dataset, root=self.root, universe=self.universe)
        targets = list(keys) if keys is not None else store.keys()
        result = IngestResult(dataset=dataset, table=DATASET_TABLES[dataset][0])
        started = time.perf_counter()
        for index, key in enumerate(targets, start=1):
            frame = store.read(key)
            if frame is None or len(frame) == 0:
                result.skipped.append(key)
                continue
            try:
                written = self.upsert(dataset, frame, key=key,
                                      batch_rows=batch_rows)
            except Exception as exc:  # noqa: BLE001 单分区失败不影响整批
                result.failed[key] = f"{type(exc).__name__}: {brief(exc, BRIEF_DEFAULT)}"
                continue
            result.partitions += 1
            result.rows += written
            if progress is not None:
                progress(dataset, index, len(targets), result.rows)
        self.ensure_indexes(dataset, [])
        result.seconds = time.perf_counter() - started
        return result

    def upsert(self, dataset: str, frame: pd.DataFrame, *,
               key: str = "", batch_rows: int = 2000) -> int:
        """方言原生 UPSERT：重复唯一键覆盖 → 重复导入不产生重复行。"""
        from sqlalchemy import text

        # ★ 写权限的**唯一咽喉**（`CHG-0087`）：所有数据集入库都经过这里。
        #   显式拒一次是为了给出"谁是写者、去哪改"的人话理由；
        #   `engine()` 里的 `query_only=1` 是覆盖其余写路径的结构性兜底。
        self.assert_writable()
        table_name, dedup_keys, _date = DATASET_TABLES[dataset]
        data = frame.copy()
        if "code" not in data.columns and "ts_code" in data.columns:
            data["code"] = (data["ts_code"].astype(str)
                            .str.split(".").str[0].str.zfill(6))
        for column in dedup_keys:
            if column not in data.columns:
                data[column] = key
        # 去掉全空列与（Tushare 不同批次可能不带的）非标量列
        usable_source = [str(column) for column in data.columns
                         if not isinstance(data[column].iloc[0]
                                           if len(data) else None, (list, dict))
                         and data[column].notna().any()]
        table = self.ensure_columns(dataset, usable_source)
        if dataset not in getattr(self, "_indexed", set()):
            # 索引是"库比 CSV 快"的前提，必须在第一次写入后立刻建出来
            self.ensure_indexes(dataset, usable_source)
        usable = [column for column in usable_source
                  if column in table.columns and column]
        subset = data[usable]
        payload = (subset.astype(object)
                   .where(pd.notna(subset), None).to_dict("records"))
        if not payload:
            return 0

        statement = text(self._upsert_sql(table_name, usable, dedup_keys))
        written = 0
        with self.engine().begin() as connection:
            for start in range(0, len(payload), batch_rows):
                chunk = payload[start:start + batch_rows]
                connection.execute(statement, chunk)
                written += len(chunk)
        return written

    def _upsert_sql(self, table_name: str, columns: Sequence[str],
                    dedup_keys: Sequence[str]) -> str:
        """按方言生成 UPSERT 语句（"去重"的唯一实现点）。"""
        mysql = self.config.dialect == "mysql"
        quote = (lambda name: f"`{name}`") if mysql else (
            lambda name: f'"{name}"')
        quoted = ", ".join(quote(column) for column in columns)
        params = ", ".join(f":{column}" for column in columns)
        conflicts = ", ".join(quote(key) for key in dedup_keys)
        updates = [column for column in columns if column not in dedup_keys]
        sql = f"INSERT INTO {quote(table_name)} ({quoted}) VALUES ({params})"
        if mysql:
            if updates:
                sql += " ON DUPLICATE KEY UPDATE " + ", ".join(
                    f"{quote(c)}=VALUES({quote(c)})" for c in updates)
            else:
                sql += (f" ON DUPLICATE KEY UPDATE "
                        f"{quote(dedup_keys[0])}={quote(dedup_keys[0])}")
        elif updates:
            prefix = "EXCLUDED" if self.config.dialect == "postgresql" else "excluded"
            sql += (f" ON CONFLICT ({conflicts}) DO UPDATE SET "
                    + ", ".join(f"{quote(c)}={prefix}.{quote(c)}"
                                for c in updates))
        else:
            sql += f" ON CONFLICT ({conflicts}) DO NOTHING"
        return sql

    # ---------- 查询 ----------

    def load(self, dataset: str, *, start: str = "", end: str = "",
             codes: Sequence[str] | None = None,
             columns: Sequence[str] | None = None,
             limit: int = 0, order: bool = True) -> pd.DataFrame:
        """按日期区间 + 股票池查询（走索引）。

        参数与 `DatasetStore.load` 同义，便于两者互换；
        `start`/`end` 是 YYYYMMDD（含端点）。

        **走 DBAPI 游标而不是 SQLAlchemy `text()`**：实测同一条 SQL，
        经 SQLAlchemy `Result` 返回的是逐行 `Row` 对象，pandas 再 `from_records`
        转换，5000 行截面要多花 ~4ms、125 万行区间要多花 ~2.5s。
        这里直接拿 DBAPI 的 tuple 结果交给 pandas，是"库更快"成立的前提。

        `order=False` 跳过 ORDER BY：面板类调用随后要 pivot，顺序无意义，
        而大区间上排序是纯开销（调用方显式声明才能省，不默认替它决定）。
        """
        table_name, _dedup, date_column = DATASET_TABLES[dataset]
        self._ensure_readable(dataset, table_name)
        if not self._table_exists(table_name):
            # 表还没建 = 这个数据集还没入库；直接返回空表，
            # 不建一张只有主键的"壳"表（否则后续 ALTER 出的列集合会误导排查）。
            return pd.DataFrame()
        quote = (lambda name: f"`{name}`") if self.config.dialect == "mysql" \
            else (lambda name: f'"{name}"')
        select = "*" if not columns else ", ".join(quote(c) for c in columns)
        placeholder = "?" if self.config.dialect == "sqlite" else "%s"
        where: list[str] = []
        params: list[Any] = []
        if start:
            where.append(f"{quote(date_column)} >= {placeholder}")
            params.append(str(start))
        if end:
            where.append(f"{quote(date_column)} <= {placeholder}")
            params.append(str(end))
        if codes:
            where.append(f"code IN ({', '.join([placeholder] * len(codes))})")
            params.extend(str(code).split(".")[0].zfill(6) for code in codes)
        sql = f"SELECT {select} FROM {quote(table_name)}"
        if where:
            sql += " WHERE " + " AND ".join(where)
        if order:
            sql += f" ORDER BY {quote(date_column)}, {quote('code')}"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return self._read_sql(sql, params)

    def _read_sql(self, sql: str, params: Sequence[Any]) -> pd.DataFrame:
        """用底层 DBAPI 连接取数（绕过 SQLAlchemy Row 转换），见 load 的说明。

        必须取 `driver_connection`（真正的 `sqlite3.Connection` / pymysql 连接）
        而不是 SQLAlchemy 的 `_ConnectionFairy` 代理：pandas 只对前者走
        它自己测试过的快路径，传代理会退化成通用路径并每个查询告警一次。
        """
        connection = self.engine().raw_connection()
        driver = getattr(connection, "driver_connection", None) or connection
        try:
            return pd.read_sql_query(sql, driver,
                                     params=tuple(params) if params else None)
        finally:
            connection.close()

    def _ensure_readable(self, dataset: str, table_name: str) -> None:
        """MySQL/PG 下库可能还没建（首次查询前应先建库，否则报"库不存在"）。"""
        if self.config.dialect in ("mysql", "postgresql"):
            self.ensure_database()

    def _table_exists(self, table_name: str) -> bool:
        from sqlalchemy import inspect

        return table_name in inspect(self.engine()).get_table_names()

    def columns_of(self, dataset: str) -> list[str]:
        """表里实际存在的列（缓存；表不存在返回空列表）。

        调用方用它来**裁剪要 SELECT 的列**：请求不存在的列会直接 SQL 报错，
        进而整条链路退化成 CSV 回退 —— 那是个很难发现的性能事故。
        """
        table = self.table(dataset)
        self._known = getattr(self, "_known", {})
        known = self._known.get(dataset)
        if known is None:
            known = self._existing_columns(dataset, table, create=False)
            self._known[dataset] = known
        return sorted(known)

    def covers(self, dataset: str, start: str = "", end: str = "") -> bool:
        """库里这个数据集是否**完整覆盖**了请求区间（不只是"有数据"）。

        `has_rows` 只问"区间内有没有一行"，这对"要不要走库"是不够的：
        入库是按分区键**顺序**进行的，所以任何时候库里的数据都是缓存的一个
        **前缀**。如果只用 `has_rows` 判断，一个灌到一半的数据集会被判为
        "走库"，于是回测**静默丢掉后半段数据** —— 比直接报错危险得多
        （净值曲线照样画得出来，只是少了一半样本）。

        因此这里要求：库里该数据集的**最大日期 ≥ 请求结束日**。
        最小日期一侧不需要额外判断：前缀模型保证它不会"中间缺失"；
        而库的起点晚于请求起点（如 moneyflow 天然从 2010 起）时，
        CSV 里同样没有更早的数据，走库不会丢东西。
        """
        table_name, _dedup, date_column = DATASET_TABLES[dataset]
        if not self._table_exists(table_name):
            return False
        quote = (lambda name: f"`{name}`") if self.config.dialect == "mysql" \
            else (lambda name: f'"{name}"')
        sql = (f"SELECT MIN({quote(date_column)}), MAX({quote(date_column)}) "
               f"FROM {quote(table_name)}")
        connection = self.engine().raw_connection()
        driver = getattr(connection, "driver_connection", None) or connection
        try:
            cursor = driver.cursor()
            try:
                cursor.execute(sql, ())
                row = cursor.fetchone()
            finally:
                cursor.close()
        finally:
            connection.close()
        if not row or row[0] is None:
            return False
        highest = str(row[1])
        # 只需检查上界：入库按分区键顺序进行，库里永远是缓存的一个前缀，
        # 所以"中间缺一段"不会发生；而库的起点晚于请求起点（如 moneyflow
        # 天然从 2010 起）时，CSV 里同样没有更早的数据，走库不丢东西。
        if end and highest < str(end):
            return False
        return True

    def has_rows(self, dataset: str, start: str = "", end: str = "") -> bool:
        """该数据集在给定区间内库里有没有数据（**一次探针**，不搬数据）。

        用途：调用方先问一次"这个数据集该走库还是走分区文件"，
        避免每个字段都去试一遍库、失败后再扫一遍 CSV 分区。
        """
        table_name, _dedup, date_column = DATASET_TABLES[dataset]
        if not self._table_exists(table_name):
            return False
        quote = (lambda name: f"`{name}`") if self.config.dialect == "mysql" \
            else (lambda name: f'"{name}"')
        placeholder = "?" if self.config.dialect == "sqlite" else "%s"
        where: list[str] = []
        params: list[Any] = []
        if start:
            where.append(f"{quote(date_column)} >= {placeholder}")
            params.append(str(start))
        if end:
            where.append(f"{quote(date_column)} <= {placeholder}")
            params.append(str(end))
        sql = f"SELECT 1 FROM {quote(table_name)}"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " LIMIT 1"
        connection = self.engine().raw_connection()
        driver = getattr(connection, "driver_connection", None) or connection
        try:
            cursor = driver.cursor()
            try:
                # 始终传元组：sqlite3 不接受 `params=None` 的形式
                cursor.execute(sql, tuple(params))
                return cursor.fetchone() is not None
            finally:
                cursor.close()
        finally:
            connection.close()

    def stats(self, *, count_mode: str = "auto") -> dict[str, Any]:
        """各表行数/时间跨度（前端"数据健康度"与运维用）。

        ## 为什么行数用 MAX(rowid) 而不是 COUNT(*)（2026-09-16 实测）

        库现在 **14.28 GB**、9 张数据表合计 8550 万行，实测：

            COUNT(*)  daily 0.46s | daily_basic 8.52s | adj_factor 8.66s | stk_limit 7.43s
            MAX(rowid) 全部 **0.00s**，且与 COUNT(*) **数值完全相等**

        九张表 `COUNT(*)` 合计 40 秒以上，而且会把 OS 页缓存冲掉（第二次更慢，实测
        42s → 46s）；`/health` 原本把这套同步跑在事件循环里，于是打开「运行指标」页
        会让**整个服务**卡几分钟（实测 `/health` 300 秒超时，做T面板一起卡）。

        ## 为什么在这里 MAX(rowid) 就是精确行数
        写入走 `INSERT ... ON CONFLICT (...) DO UPDATE`（**原地更新，不换 rowid**），
        且仓库数据表**从不 DELETE**（全库唯一的 DELETE 在策略表上）。因此 rowid
        没有空洞，`MAX(rowid) == COUNT(*)`。

        口径写在返回值的 `count_mode` 里（`rowid` / `count`），不让人猜数字怎么来的；
        `count_mode="exact"` 可强制走 COUNT(*)（数据迁移后校验用）。
        """
        from sqlalchemy import inspect, text

        exact = count_mode == "exact" or self.config.dialect != "sqlite"
        output: dict[str, Any] = {"dialect": self.config.dialect,
                                  "description": self.config.description,
                                  "count_mode": "count" if exact else "rowid",
                                  "tables": []}
        existing = set(inspect(self.engine()).get_table_names())
        count_expr = "COUNT(*)" if exact else "MAX(rowid)"
        for dataset, (table_name, _dedup, date_column) in DATASET_TABLES.items():
            if table_name not in existing:
                continue
            with self.engine().connect() as connection:
                row = connection.execute(text(
                    f'SELECT {count_expr}, MIN("{date_column}"), '
                    f'MAX("{date_column}") FROM "{table_name}"')).fetchone()
            output["tables"].append({
                "dataset": dataset, "table": table_name,
                "rows": int(row[0] or 0), "first": str(row[1] or ""),
                "last": str(row[2] or "")})
        output["total_rows"] = sum(item["rows"] for item in output["tables"])
        return output


# ==================================================================
# 策略档案（存数据库；换 MySQL 不需要改代码）
# ==================================================================


@dataclass
class StrategyArchive:
    """策略档案库：把回测出来的策略存进数据库，支持跨策略查询。

    与 `strategy_store.StrategyStore`（JSON 文件）的分工：

    | | JSON 文件 | 数据库（本类） |
    |---|---|---|
    | 用途 | 单机留档、可版本管理、人肉可读 | **跨策略查询**：按超额排序、按时段筛、按标的找 |
    | 优点 | 零依赖、可直接 diff | 一次 SQL 拿到"哪些策略跑赢过基准" |
    | 缺点 | 查"排名"要把全部文件读进内存 | 需要数据库可用 |

    两者**不是替代关系**：数据库是主档案（可查），JSON 是导出格式。
    数据库不可用时退化为"只写文件"，并在返回值里如实标注 —— 静默降级会让人
    以为策略已经入库了。
    """

    warehouse: QuantWarehouse
    disclaimer: str = ""

    # ---------- 建表 ----------

    def ensure_table(self) -> None:
        from sqlalchemy import (
            Column,
            Float,
            MetaData,
            PrimaryKeyConstraint,
            String,
            Table,
            Text,
            UniqueConstraint,
        )

        engine = self.warehouse.engine()
        metadata = MetaData()
        columns = []
        for name, ddl in _STRATEGY_COLUMNS:
            if ddl == "TEXT":
                columns.append(Column(name, Text))
            elif ddl == "FLOAT":
                columns.append(Column(name, Float))
            else:
                length = int(ddl.split("(")[1].rstrip(")"))
                columns.append(Column(name, String(length)))
        # 约束在**建表时**声明。用 `append_constraint` 后补会生成
        # `PRIMARY KEY (id), , CONSTRAINT ...` 这种带空元素的 DDL 而报错
        # （实际踩过）。主键 = 策略 ID；spec_hash 唯一 = 同一套参数去重。
        table = Table(STRATEGY_TABLE, metadata, *columns,
                      PrimaryKeyConstraint("id"),
                      UniqueConstraint("spec_hash", name="uniq_strategy_spec"))
        table.create(engine, checkfirst=True)
        self._add_missing_columns()

    def _add_missing_columns(self) -> None:
        """给已存在的表补上新增的列（幂等）。

        `create(checkfirst=True)` 只判断"表在不在"，**不会**补列 ——
        档案字段是会长大的（"搜索空间""低样本标记"都是后加的），
        没有这一步时新增字段会让 INSERT 引用不存在的列而整条写入失败，
        而且错误信息只提列名、不指向真正的原因（实际踩过：归档 0 条但没报错到用户那）。
        """
        from sqlalchemy import inspect, text

        engine = self.warehouse.engine()
        inspector = inspect(engine)
        if STRATEGY_TABLE not in inspector.get_table_names():
            return
        existing = {column["name"]
                    for column in inspector.get_columns(STRATEGY_TABLE)}
        additions = [(name, ddl) for name, ddl in _STRATEGY_COLUMNS
                     if name not in existing]
        if not additions:
            return
        quote = (lambda name: f"`{name}`") if self.warehouse.config.dialect == "mysql" \
            else (lambda name: f'"{name}"')
        with engine.begin() as connection:
            for name, ddl in additions:
                connection.execute(text(
                    f"ALTER TABLE {quote(STRATEGY_TABLE)} "
                    f"ADD COLUMN {quote(name)} {ddl}"))
        logger.info("策略档案表补齐 %d 个新列：%s", len(additions),
                    ", ".join(name for name, _ in additions))

    # ---------- 写入 ----------

    def save(self, record: dict[str, Any]) -> dict[str, Any]:
        """写入（或按 spec_hash 更新）一条策略，返回落库后的记录。"""
        import json as _json

        from sqlalchemy import text

        self.ensure_table()
        payload = dict(record)
        payload.setdefault("created_at", time.strftime("%Y-%m-%d %H:%M:%S"))
        payload["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        row = {name: self._coerce(name, payload.get(name))
               for name, _ddl in _STRATEGY_COLUMNS}
        row["payload"] = _json.dumps(payload, ensure_ascii=False, default=str)
        row["disclaimer"] = self.disclaimer

        columns = [name for name, _ddl in _STRATEGY_COLUMNS]
        quote = (lambda name: f"`{name}`") if self.warehouse.config.dialect == "mysql" \
            else (lambda name: f'"{name}"')
        placeholders = ", ".join(f":{name}" for name in columns)
        quoted = ", ".join(quote(name) for name in columns)
        updates = [name for name in columns if name not in ("id", "spec_hash")]
        sql = f"INSERT INTO {quote(STRATEGY_TABLE)} ({quoted}) VALUES ({placeholders})"
        if self.warehouse.config.dialect == "mysql":
            sql += " ON DUPLICATE KEY UPDATE " + ", ".join(
                f"{quote(name)}=VALUES({quote(name)})" for name in updates)
        else:
            prefix = ("EXCLUDED" if self.warehouse.config.dialect == "postgresql"
                      else "excluded")
            sql += (f" ON CONFLICT ({quote('spec_hash')}) DO UPDATE SET "
                    + ", ".join(f"{quote(name)}={prefix}.{quote(name)}"
                                for name in updates))
        with self.warehouse.engine().begin() as connection:
            connection.execute(text(sql), row)
        return payload

    @staticmethod
    def _coerce(name: str, value: Any) -> Any:
        if value is None:
            return None
        if name in ("id", "name", "code", "entry_condition", "exit_condition",
                    "preset", "spec_hash", "created_at", "updated_at",
                    "start_date", "end_date", "source"):
            return str(value)[:4000]
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    # ---------- 查询 ----------

    def list(self, *, limit: int = 200, code: str = "",
             only_winners: bool = False, min_excess: float | None = None,
             order_by: str = "recent_excess_pct") -> list[dict[str, Any]]:
        """按条件列出策略。默认按**近一年超额**降序 —— 这正是"哪些策略跑赢过"的问题。"""
        import json as _json

        from sqlalchemy import inspect, text

        if not self.warehouse.available():
            return []
        if STRATEGY_TABLE not in inspect(self.warehouse.engine()).get_table_names():
            return []
        quote = (lambda name: f"`{name}`") if self.warehouse.config.dialect == "mysql" \
            else (lambda name: f'"{name}"')
        allowed = {name for name, _ddl in _STRATEGY_COLUMNS}
        order_column = order_by if order_by in allowed else "recent_excess_pct"
        where, params = [], {}
        if code:
            where.append("code = :code")
            params["code"] = str(code).zfill(6)
        if only_winners:
            where.append("beats_benchmark = 1")
        if min_excess is not None:
            where.append("recent_excess_pct >= :min_excess")
            params["min_excess"] = float(min_excess)
        sql = f"SELECT * FROM {quote(STRATEGY_TABLE)}"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += (f" ORDER BY {quote(order_column)} IS NULL, "
                f"{quote(order_column)} DESC LIMIT {int(limit)}")
        with self.warehouse.engine().connect() as connection:
            rows = connection.execute(text(sql), params).mappings().all()
        out: list[dict[str, Any]] = []
        for row in rows:
            payload = row.get("payload")
            if payload:
                try:
                    out.append(_json.loads(payload))
                    continue
                except _json.JSONDecodeError:
                    pass
            out.append({key: value for key, value in row.items()
                        if key not in ("payload", "disclaimer")})
        return out

    def stats(self) -> dict[str, Any]:
        """档案统计（前端顶部用）：总数、跑赢数、最优近一年超额。"""
        from sqlalchemy import inspect, text

        if not self.warehouse.available():
            return {"available": False, "total": 0, "winners": 0}
        if STRATEGY_TABLE not in inspect(self.warehouse.engine()).get_table_names():
            return {"available": True, "total": 0, "winners": 0}
        quote = (lambda name: f"`{name}`") if self.warehouse.config.dialect == "mysql" \
            else (lambda name: f'"{name}"')
        with self.warehouse.engine().connect() as connection:
            row = connection.execute(text(
                f"SELECT COUNT(*), SUM(CASE WHEN beats_benchmark = 1 THEN 1 ELSE 0 END), "
                f"MAX({quote('recent_excess_pct')}) FROM {quote(STRATEGY_TABLE)}"
            )).fetchone()
        return {"available": True, "dialect": self.warehouse.config.dialect,
                "table": STRATEGY_TABLE, "total": int(row[0] or 0),
                "winners": int(row[1] or 0),
                "best_recent_excess_pct": (None if row[2] is None
                                           else round(float(row[2]), 2))}

    def delete(self, strategy_id: str) -> bool:
        from sqlalchemy import inspect, text

        if not self.warehouse.available():
            return False
        if STRATEGY_TABLE not in inspect(self.warehouse.engine()).get_table_names():
            return False
        quote = (lambda name: f"`{name}`") if self.warehouse.config.dialect == "mysql" \
            else (lambda name: f'"{name}"')
        with self.warehouse.engine().begin() as connection:
            result = connection.execute(
                text(f"DELETE FROM {quote(STRATEGY_TABLE)} WHERE id = :id"),
                {"id": strategy_id})
            return bool(result.rowcount)


def strategy_archive(root: str | Path = DEFAULT_ROOT, *,
                     disclaimer: str = "") -> StrategyArchive:
    return StrategyArchive(QuantWarehouse(root=root), disclaimer=disclaimer)


# ==================================================================
# 开源策略案例（网络公开分享的策略线索，增量抓取）
# ==================================================================

CASE_TABLE = "quant_strategy_case"
_CASE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("id", "VARCHAR(64)"),
    ("theme", "VARCHAR(64)"),
    ("title", "VARCHAR(255)"),
    ("summary", "TEXT"),
    ("source_name", "VARCHAR(64)"),
    ("source_url", "VARCHAR(512)"),
    ("kind", "VARCHAR(32)"),
    ("published_at", "VARCHAR(32)"),
    ("first_seen_at", "VARCHAR(32)"),
    ("fetched_at", "VARCHAR(32)"),
    ("tags", "TEXT"),
    # 永远是 0：本模块不做复现验证，也不假装验证过
    ("verified", "FLOAT"),
    ("payload", "TEXT"),
    ("disclaimer", "TEXT"),
)


@dataclass
class StrategyCaseStore:
    """开源策略案例库（按来源链接去重，增量更新）。

    为什么单独一张表而不是复用策略档案：两者**性质完全不同**。
    `quant_strategy` 里是本项目自己回测出来的（有净值、有自检、可复现）；
    这里是从网上抓的**说法**，未经验证。混在一张表里，
    迟早会有人把"某博客说这个策略年化 50%"当成"我们回测过"。
    """

    warehouse: QuantWarehouse
    disclaimer: str = ""

    def ensure_table(self) -> None:
        from sqlalchemy import (
            Column,
            Float,
            MetaData,
            PrimaryKeyConstraint,
            String,
            Table,
            Text,
        )

        engine = self.warehouse.engine()
        metadata = MetaData()
        columns = []
        for name, ddl in _CASE_COLUMNS:
            if ddl == "TEXT":
                columns.append(Column(name, Text))
            elif ddl == "FLOAT":
                columns.append(Column(name, Float))
            else:
                columns.append(Column(name, String(int(ddl.split("(")[1].rstrip(")")))))
        table = Table(CASE_TABLE, metadata, *columns,
                      PrimaryKeyConstraint("id"))
        table.create(engine, checkfirst=True)
        _add_missing_columns(engine, self.warehouse.config.dialect,
                             CASE_TABLE, _CASE_COLUMNS)

    def upsert(self, cases: list[Any]) -> dict[str, int]:
        """批量写入；已存在的只更新摘要/抓取时间，不新增行。"""
        import json as _json

        from sqlalchemy import text

        if not cases:
            return {"inserted": 0, "total": 0}
        # 数据库不可用时**返回 0 且如实标注**，不抛异常：抓取链路不该因为
        # 存储问题整体崩掉，但调用方必须能看出"没写进去"。
        if not self.warehouse.available():
            return {"inserted": 0, "total": 0, "available": False}
        self.ensure_table()
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        columns = [name for name, _ddl in _CASE_COLUMNS]
        quote = (lambda name: f"`{name}`") if self.warehouse.config.dialect == "mysql" \
            else (lambda name: f'"{name}"')
        sql = (f"INSERT INTO {quote(CASE_TABLE)} "
               f"({', '.join(quote(name) for name in columns)}) VALUES "
               f"({', '.join(':' + name for name in columns)})")
        updates = [name for name in columns if name not in ("id", "first_seen_at")]
        if self.warehouse.config.dialect == "mysql":
            sql += " ON DUPLICATE KEY UPDATE " + ", ".join(
                f"{quote(name)}=VALUES({quote(name)})" for name in updates)
        else:
            prefix = ("EXCLUDED" if self.warehouse.config.dialect == "postgresql"
                      else "excluded")
            sql += (f" ON CONFLICT ({quote('id')}) DO UPDATE SET "
                    + ", ".join(f"{quote(name)}={prefix}.{quote(name)}"
                                for name in updates))
        inserted = 0
        with self.warehouse.engine().begin() as connection:
            for item in cases:
                data = item.as_dict() if hasattr(item, "as_dict") else dict(item)
                row = {
                    "id": data["id"], "theme": data.get("theme", ""),
                    "title": str(data.get("title", ""))[:250],
                    "summary": data.get("summary", ""),
                    "source_name": data.get("source_name", ""),
                    "source_url": str(data.get("source_url", ""))[:500],
                    "kind": data.get("kind", "策略案例"),
                    "published_at": data.get("published_at", ""),
                    "first_seen_at": now, "fetched_at": now,
                    "tags": _json.dumps(data.get("tags", []), ensure_ascii=False),
                    "verified": 0,
                    "payload": _json.dumps(data, ensure_ascii=False, default=str),
                    "disclaimer": self.disclaimer,
                }
                connection.execute(text(sql), row)
                inserted += 1
        return {"inserted": inserted, "total": self.count()}

    def list(self, *, theme: str = "", source: str = "", limit: int = 200,
             order_by: str = "published_at", kind: str = "",
             exclude_kinds: tuple[str, ...] = ()) -> list[dict[str, Any]]:
        import json as _json

        from sqlalchemy import inspect, text

        if not self.warehouse.available():
            return []
        if CASE_TABLE not in inspect(self.warehouse.engine()).get_table_names():
            return []
        quote = (lambda name: f"`{name}`") if self.warehouse.config.dialect == "mysql" \
            else (lambda name: f'"{name}"')
        allowed = {name for name, _ddl in _CASE_COLUMNS}
        column = order_by if order_by in allowed else "published_at"
        where, params = [], {}
        if theme:
            where.append("theme = :theme")
            params["theme"] = theme
        if source:
            where.append("source_name = :source")
            params["source"] = source
        if kind:
            where.append("kind = :kind")
            params["kind"] = kind
        for index, excluded in enumerate(exclude_kinds):
            where.append(f"COALESCE(kind, '') != :exclude{index}")
            params[f"exclude{index}"] = excluded
        sql = f"SELECT * FROM {quote(CASE_TABLE)}"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += (f" ORDER BY {quote(column)} IS NULL, {quote(column)} DESC "
                f"LIMIT {int(limit)}")
        with self.warehouse.engine().connect() as connection:
            rows = connection.execute(text(sql), params).mappings().all()
        out: list[dict[str, Any]] = []
        for row in rows:
            payload = row.get("payload")
            if payload:
                try:
                    item = _json.loads(payload)
                    item["verified"] = 0
                    out.append(item)
                    continue
                except _json.JSONDecodeError:
                    pass
            out.append({key: value for key, value in row.items()
                        if key not in ("payload", "disclaimer")})
        return out

    def count(self) -> int:
        from sqlalchemy import inspect, text

        if not self.warehouse.available():
            return 0
        if CASE_TABLE not in inspect(self.warehouse.engine()).get_table_names():
            return 0
        quote = (lambda name: f"`{name}`") if self.warehouse.config.dialect == "mysql" \
            else (lambda name: f'"{name}"')
        with self.warehouse.engine().connect() as connection:
            return int(connection.execute(
                text(f"SELECT COUNT(*) FROM {quote(CASE_TABLE)}")).scalar() or 0)

    def reclassify(self, *, theme_of: Any = None, kind_of: Any = None) -> int:
        """对表内**全部**记录重跑主题/类型分类（不联网）。

        为什么需要它：抓取只更新"本批返回的条目"，历史条目会保留旧分类。
        关键词表一改（例如新增"回测方法论"这一类），旧记录不会自动跟上 ——
        实测就是这样：新分类只覆盖了当批，表里仍有 40 多条停在"其它"。
        """
        import json as _json

        from sqlalchemy import inspect, text

        if theme_of is None or kind_of is None:
            from src.quant.strategy_cases import classify_kind, classify_theme

            theme_of = theme_of or classify_theme
            kind_of = kind_of or classify_kind
        if not self.warehouse.available():
            return 0
        if CASE_TABLE not in inspect(self.warehouse.engine()).get_table_names():
            return 0
        quote = (lambda name: f"`{name}`") if self.warehouse.config.dialect == "mysql" \
            else (lambda name: f'"{name}"')
        with self.warehouse.engine().begin() as connection:
            rows = connection.execute(text(
                f"SELECT id, title, summary, payload FROM {quote(CASE_TABLE)}"
            )).mappings().all()
            updated = 0
            for row in rows:
                title = row["title"] or ""
                summary = row["summary"] or ""
                theme = theme_of(title, summary)
                kind = kind_of(title, summary)
                payload = row["payload"]
                if payload:
                    try:
                        data = _json.loads(payload)
                        data["theme"], data["kind"] = theme, kind
                        payload = _json.dumps(data, ensure_ascii=False, default=str)
                    except _json.JSONDecodeError:
                        payload = None
                if payload is None:
                    connection.execute(text(
                        f"UPDATE {quote(CASE_TABLE)} SET theme = :theme, "
                        f"kind = :kind WHERE id = :id"),
                        {"theme": theme, "kind": kind, "id": row["id"]})
                else:
                    connection.execute(text(
                        f"UPDATE {quote(CASE_TABLE)} SET theme = :theme, "
                        f"kind = :kind, payload = :payload WHERE id = :id"),
                        {"theme": theme, "kind": kind, "payload": payload,
                         "id": row["id"]})
                updated += 1
        return updated

    def themes(self) -> list[dict[str, Any]]:
        """主题分布（前端做筛选用）。"""
        from sqlalchemy import inspect, text

        if not self.warehouse.available():
            return []
        if CASE_TABLE not in inspect(self.warehouse.engine()).get_table_names():
            return []
        quote = (lambda name: f"`{name}`") if self.warehouse.config.dialect == "mysql" \
            else (lambda name: f'"{name}"')
        with self.warehouse.engine().connect() as connection:
            rows = connection.execute(text(
                f"SELECT theme, COUNT(*) FROM {quote(CASE_TABLE)} "
                f"GROUP BY theme ORDER BY COUNT(*) DESC")).fetchall()
        return [{"theme": str(row[0] or "其它"), "count": int(row[1])}
                for row in rows]

    def kind_stats(self) -> list[dict[str, Any]]:
        """类型分布（策略案例 / 框架工具 / 教程笔记 / 学术论文）。"""
        from sqlalchemy import inspect, text

        if not self.warehouse.available():
            return []
        if CASE_TABLE not in inspect(self.warehouse.engine()).get_table_names():
            return []
        quote = (lambda name: f"`{name}`") if self.warehouse.config.dialect == "mysql" \
            else (lambda name: f'"{name}"')
        with self.warehouse.engine().connect() as connection:
            rows = connection.execute(text(
                f"SELECT COALESCE(kind, '策略案例'), COUNT(*) "
                f"FROM {quote(CASE_TABLE)} GROUP BY 1 ORDER BY 2 DESC")).fetchall()
        return [{"kind": str(row[0]), "count": int(row[1])} for row in rows]


def _add_missing_columns(engine: Any, dialect: str, table: str,
                         columns: tuple[tuple[str, str], ...]) -> None:
    """给已存在的表补新列（幂等）。表结构会长大，不补列会让写入整条失败。"""
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    if table not in inspector.get_table_names():
        return
    existing = {column["name"] for column in inspector.get_columns(table)}
    additions = [(name, ddl) for name, ddl in columns if name not in existing]
    if not additions:
        return
    quote = (lambda name: f"`{name}`") if dialect == "mysql" \
        else (lambda name: f'"{name}"')
    with engine.begin() as connection:
        for name, ddl in additions:
            connection.execute(text(
                f"ALTER TABLE {quote(table)} ADD COLUMN {quote(name)} {ddl}"))


def strategy_case_store(root: str | Path = DEFAULT_ROOT, *,
                        disclaimer: str = "") -> StrategyCaseStore:
    return StrategyCaseStore(QuantWarehouse(root=root), disclaimer=disclaimer)


# ==================================================================
# 统一取数入口（数据库优先，CSV 兜底）
# ==================================================================


# ★★★ 2026-09-27 第八轮：QuantWarehouse 进程级单例（−1.4ms/调用 + 避免 SELECT 1 探测）
# 审计实证：load_dataset 每次新建实例 → 新建 engine → "SELECT 1" 探测 + 表存在性探测 = 1.4 ms/次
# 同一进程 N 次调用就白花 1.4×N ms，且**数据库连接是 OS 级文件描述符**，反复开关有 fd 压力。
# 单例化后只剩一次探测 + 一次连接。
# 为什么不直接用 SQLAlchemy 连接池：现状后端是 SQLite（当前阶段），连接池没收益；
# 换 PostgreSQL 后 `engine.pool` 自带连接池，单例化是切换前置条件。
_WAREHOUSE_CACHE: dict[tuple[str, str], QuantWarehouse] = {}
_WAREHOUSE_LOCK = threading.Lock()


def _get_warehouse_cached(root: str | Path, universe: str) -> QuantWarehouse:
    key = (str(root), str(universe))
    cached = _WAREHOUSE_CACHE.get(key)
    if cached is not None:
        return cached
    with _WAREHOUSE_LOCK:
        cached = _WAREHOUSE_CACHE.get(key)
        if cached is None:
            cached = QuantWarehouse(root=root, universe=universe)
            _WAREHOUSE_CACHE[key] = cached
        return cached


def reset_warehouse_cache() -> None:
    """清空连接单例缓存（仅测试用）。"""
    with _WAREHOUSE_LOCK:
        _WAREHOUSE_CACHE.clear()


def load_dataset(dataset: str, *, start: str = "", end: str = "",
                 codes: Sequence[str] | None = None,
                 columns: Sequence[str] | None = None,
                 root: str | Path = DEFAULT_ROOT,
                 universe: str = "a_share",
                 prefer: str = "db") -> tuple[pd.DataFrame, str]:
    """取一个数据集：优先数据库，失败/无数据则回退 CSV 分区缓存。

    返回 `(DataFrame, 来源说明)` —— **来源必须显式回传**，
    否则"这次到底查的库还是文件"会变成查不出来的问题（回测复现性依赖它）。

    `columns` 是**列裁剪**：实测 250 日区间下只取 3 列比取全列快约 2 倍
    （取数耗时几乎全在搬运多少列上）。CSV 分支做不到下推，只能取完再切。

    ★ 2026-09-27 第八轮：QuantWarehouse 进程级单例（避免每次新建连接）。
    """
    if prefer in ("db", "mysql", "auto"):
        warehouse = _get_warehouse_cached(root, universe)
        if warehouse.available():
            try:
                frame = warehouse.load(dataset, start=start, end=end, codes=codes,
                                       columns=columns, order=False)
                if len(frame):
                    table = DATASET_TABLES[dataset][0]
                    return frame, f"db:{warehouse.config.dialect}.{table}"
            except Exception as exc:  # noqa: BLE001 查询失败回退 CSV
                logger.warning("数据库查询 %s 失败，回退 CSV：%s",
                               dataset, brief(exc, BRIEF_DEFAULT))
            finally:
                warehouse.close()
    from src.quant.dataset_store import DatasetStore

    store = DatasetStore(dataset, root=root, universe=universe)
    keys = [key for key in store.keys()
            if (not start or key >= start) and (not end or key <= end)]
    frame = store.load(keys)
    if codes and len(frame) and "code" in frame.columns:
        frame = frame[frame["code"].isin([str(c).split(".")[0].zfill(6)
                                          for c in codes])]
    if columns and len(frame) and "code" in frame.columns:
        keep = [name for name in columns if name in frame.columns]
        if keep:
            frame = frame[keep]
    return frame, f"csv:{dataset}({len(keys)} 个分区)"


def warehouse_status(root: str | Path = DEFAULT_ROOT) -> dict[str, Any]:
    """仓库状态（未配置/不可用时如实返回原因，不抛异常）。"""
    config = WarehouseConfig.from_env(root=root)
    warehouse = QuantWarehouse(config, root=root)
    try:
        if not warehouse.available():
            return {"available": False, "dialect": config.dialect,
                    "description": config.description,
                    "hint": "设置 MOSS_MYSQL_HOST/USER/PASSWORD（或 MOSS_DB_URL）"
                            "后切换到该数据库"}
        stats = warehouse.stats()
        stats["available"] = True
        return stats
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "dialect": config.dialect,
                "description": config.description,
                "error": f"{type(exc).__name__}: {brief(exc, BRIEF_DEFAULT)}"}
    finally:
        warehouse.close()


def benchmark_sources(dataset: str = "daily_basic", *, days: int = 5,
                      rounds: int = 3) -> dict[str, Any]:
    """对比 API / CSV / 数据库三种取数方式的单截面耗时。"""
    from src.quant.dataset_store import DatasetStore

    store = DatasetStore(dataset)
    keys = store.keys()[-days:]
    report: dict[str, Any] = {"dataset": dataset, "days": days,
                              "partitions": keys, "timings_ms": {}}

    started = time.perf_counter()
    for key in keys:
        store.read(key)
    report["timings_ms"]["csv"] = round(
        (time.perf_counter() - started) / max(len(keys), 1) * 1000, 1)

    warehouse = QuantWarehouse()
    if warehouse.available():
        try:
            started = time.perf_counter()
            for key in keys:
                warehouse.load(dataset, start=key, end=key)
            report["timings_ms"]["db"] = round(
                (time.perf_counter() - started) / max(len(keys), 1) * 1000, 1)
            report["dialect"] = warehouse.config.dialect
        except Exception as exc:  # noqa: BLE001
            report["timings_ms"]["db"] = f"失败：{type(exc).__name__}"
        finally:
            warehouse.close()
    report["timings_ms"]["api"] = "见 docs/DATA_SOURCE_ROUTING.md（66~248 ms/截面）"
    return report


def ingest_all(*, root: str | Path = DEFAULT_ROOT,
               datasets: Sequence[str] | None = None,
               batch_rows: int = 2000,
               progress: Any = None) -> dict[str, Any]:
    """把所有（或指定）数据集从 CSV 分区灌入数据库，返回汇总。

    这是"集中管理"的落地入口：`uv run python scripts/quant_warehouse.py ingest`
    """
    config = WarehouseConfig.from_env(root=root)
    warehouse = QuantWarehouse(config, root=root)
    summary: dict[str, Any] = {"dialect": config.dialect,
                               "description": config.description,
                               "datasets": [], "rows": 0, "seconds": 0.0}
    try:
        if not warehouse.available():
            summary["error"] = "数据库不可用"
            return summary
        warehouse.ensure_database()
        for dataset in (datasets or list(DATASET_TABLES)):
            result = warehouse.ingest_dataset(dataset, batch_rows=batch_rows,
                                              progress=progress)
            summary["datasets"].append(result.as_dict())
            summary["rows"] += result.rows
            summary["seconds"] = round(summary["seconds"] + result.seconds, 1)
        summary.update(warehouse.stats())
        summary["rows"] = summary.pop("total_rows", summary["rows"])
        return summary
    finally:
        warehouse.close()
