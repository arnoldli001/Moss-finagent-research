"""本地存储的**单点解析**：`configs/data_stores.yaml` 是唯一事实源。

## 为什么必须有这个模块（一次真实报障的根因）

用户报障「pilot 投研取数查不到，数据分散到多个库里」。实测根因不是"数据分散"
本身，而是**没有任何一层认全部**：

| 层 | 原先的枚举方式 | 漏了什么 |
|---|---|---|
| `assets.DataAssetCatalog` | 只扫 `settings.sqlite_path` **一个库** | 行情仓、主线缓存、遗留主库、所有文件目录 |
| `column_index.ColumnIndex` | 递归 `data/**.db`，**按名字跳过 `warehouse.db`** *且*受 8 GiB 上限（行情仓 14.36 GiB） | 行情仓（**双重跳过**） |
| `assets.TABLE_FREQUENCY` / `DIR_FREQUENCY` | **手写** | 无 `quant_daily*` |

同时有 3 套库路径解析器（`settings.sqlite_path` / `WarehouseConfig.from_env()` /
模块自带 `config.yaml` 硬编码），`src/` 里散着 67 处路径字面量。

**本模块把"库在哪、谁能写、能不能删"收敛成一个答案。**

## 三条硬约束

1. **不许再按名字或体积跳过存储。** 行情仓被 `SKIP_DB_PATTERNS`（名字）与
   `max_db_bytes=8GiB`（体积）**双重跳过**，于是"库里有 1,542 万行，Agent 一条看不到"。
   要不要扫由 registry 的 `kind`/`role` 决定，不由启发式决定。
2. **`protected: true` 的存储不得被任何清理口径覆盖。**
   用户 2026-09-28 明确声明：「`data/quant/warehouse.db` 14.36 GiB
   这是我行情数据库 **不能随便删，前端要用的**」。
3. **写者归属是声明式的、可见的，而且是强制的。**
   `writable_here()` 返回**决定 + 理由**；`writer: <env>` 这一支
   `decided=True` ⇒ 写闸门 fail-closed（`CHG-0087`：用户裁定
   「共享行情仓，dev 读，pilot 写和读；谁负责更新数据谁有写权限」）。
   仅剩 `writer: main` + 本实例为隔离档这一支仍是"只报告不阻断"
   （审计项 A7 的剩余部分，见 PRD §18.4）。

## 环境感知

`isolation: per_env` 的存储按**该环境自己的数据根**解析。数据根从应用库路径
反推（`{env_root}/moss_{env}.db` 的父目录），与 `manage.py` 的三档隔离
（`dev_isolation_env` / `test_isolation_env` / `pilot_isolation_env`）**同源** ——
所以不会出现"隔离环境改了、registry 没改"的漂移。
"""
from __future__ import annotations

import logging
import os
import sqlite3
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: 仓库根（`src/infrastructure/catalog/data_stores.py` → 上溯 3 级）
PROJECT_ROOT = Path(__file__).resolve().parents[3]

#: 清单文件（**唯一事实源**）
REGISTRY_PATH = PROJECT_ROOT / "configs" / "data_stores.yaml"

#: 三档隔离环境（`manage.py` 里各有一个 `*_isolation_env`）
ISOLATED_ENVS: tuple[str, ...] = ("dev", "test", "pilot")


class StoreNotFound(KeyError):
    """引用了未登记的存储 —— **不许静默回退**成某个默认路径。"""


@dataclass(frozen=True)
class Store:
    """一条存储登记。"""

    name: str
    kind: str                 # sqlite | dir | file
    path: str
    isolation: str = "shared"  # shared | per_env
    writer: str = "main"       # main | own | none
    writable: bool = True
    protected: bool = False
    role: str = "source"       # source | derived | dim | ops
    time_column: str = ""
    note: str = ""
    #: ★ `isolation: per_env` 的存储，在**主实例**上的路径（`CHG-0071`）。
    #:
    #: ## 为什么需要它（这条把 31 处字面量从代码里挪进 registry）
    #:
    #: 项目里实际存在**两套布局**：
    #:
    #: | 谁 | 应用库 | 审计 | 缓存 |
    #: |---|---|---|---|
    #: | 三档隔离实例（`manage.py --env dev/test/pilot`） | `data/<env>/moss_<env>.db` | `data/<env>/audit` | `data/<env>/llm_cache` |
    #: | **主实例 / 离线脚本**（不注入 `MOSS_SQLITE_PATH`） | `data/moss_finagent.db` | `data/audit` | `data/llm_cache` |
    #:
    #: registry 原先只能表达第一套，于是第二套只能**留在代码里当字面量**
    #: （`core/config.py` 5 处 + 审计/缓存/运行目录 12 处）。
    #: 它不是"合并成一个模板"就能解决的 —— 把主实例的审计目录改到
    #: `data/dev/audit` 会**把已有的哈希链搬走、链校验直接断**。
    #: 所以正确做法是**让 registry 同时表达两套**，代码里一处都不写。
    main_path: str = ""

    def resolved(self, env: str | None = None) -> Path:
        """这条存储的真实路径。

        `env` 给定时解析**别的环境**的 per_env 存储（用于覆盖判据的展开）；
        不给定时按**本进程**判定走隔离布局还是主实例布局。
        """
        raw = self.path
        if self.isolation == "per_env":
            if env is None and self.main_path and is_main_instance():
                raw = self.main_path
            else:
                target = env or current_env()
                raw = raw.replace("{env}", target)
                raw = raw.replace("{env_root}", _env_root_for(target).as_posix())
        p = Path(raw)
        return p if p.is_absolute() else (PROJECT_ROOT / p)

    @property
    def exists(self) -> bool:
        return self.resolved().exists()

    def size_bytes(self) -> int:
        p = self.resolved()
        if not p.exists():
            return 0
        if p.is_file():
            return p.stat().st_size
        total = 0
        for f in p.rglob("*"):
            if f.is_file():
                try:
                    total += f.stat().st_size
                except OSError:
                    pass
        return total

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "kind": self.kind, "isolation": self.isolation,
            "writer": self.writer, "writable": self.writable,
            "protected": self.protected, "role": self.role,
            "time_column": self.time_column,
            "path": self.resolved().relative_to(PROJECT_ROOT).as_posix()
            if self.resolved().is_relative_to(PROJECT_ROOT) else str(self.resolved()),
            "exists": self.exists, "note": self.note,
        }


def current_env() -> str:
    """当前进程的环境名（`MOSS_ENV`，缺省 dev —— 与 `Settings.env` 同口径）。"""
    return (os.environ.get("MOSS_ENV") or "dev").strip().lower() or "dev"


def _legacy_main_basename() -> str:
    """遗留主库的文件名（从 registry 现读，**不写字面量**）。

    刻意只比文件名而不是全路径：调用方可能写成 `data\\moss_finagent.db`
    或绝对路径，比全路径会因分隔符/大小写误判。
    """
    for s in _load():
        if s.name == "legacy_main":
            return Path(s.path).name.lower()
    return ""


def is_main_instance() -> bool:
    """本进程是不是**主实例**（= 未走三档隔离）。

    判据（二选一，都机器可读）：
      ① 没注入 `MOSS_SQLITE_PATH` —— 离线脚本与"直接 uvicorn"的主实例都是这样；
      ② 注入了，但它指向**遗留主库**（`legacy_main`）。

    ## 为什么必须有这个谓词

    项目里存在两套布局（见 `Store.main_path` 的说明）。没有这个谓词，
    registry 只能表达隔离档那一套，主实例那一套就只能留在代码里当字面量 ——
    而那正是 `CHG-0070` 收敛到 31 处时卡住的地方。
    """
    app = os.environ.get("MOSS_SQLITE_PATH", "").strip()
    if not app:
        return True
    return Path(app).name.lower() == _legacy_main_basename()


@lru_cache(maxsize=1)
def _load() -> tuple[Store, ...]:
    import yaml

    raw = yaml.safe_load(REGISTRY_PATH.read_text(encoding="utf-8")) or {}
    stores = []
    for item in raw.get("stores", []):
        stores.append(Store(
            name=str(item["name"]), kind=str(item.get("kind", "dir")),
            path=str(item["path"]),
            isolation=str(item.get("isolation", "shared")),
            writer=str(item.get("writer", "main")),
            writable=bool(item.get("writable", True)),
            protected=bool(item.get("protected", False)),
            role=str(item.get("role", "source")),
            time_column=str(item.get("time_column", "")),
            note=str(item.get("note", "")).strip(),
            main_path=str(item.get("main_path", "")),
        ))
    names = [s.name for s in stores]
    dup = {n for n in names if names.count(n) > 1}
    if dup:
        raise ValueError(f"data_stores.yaml 有重名存储：{sorted(dup)}")
    return tuple(stores)


def all_stores() -> tuple[Store, ...]:
    return _load()


def registry_path() -> Path:
    return REGISTRY_PATH


def reset_cache() -> None:
    """测试用：清单改动后重新加载。"""
    _load.cache_clear()
    env_root.cache_clear()


@lru_cache(maxsize=1)
def env_root() -> Path:
    """本环境的数据根目录。

    ## 为什么从应用库路径反推，而不是硬编码 `data/<env>`

    `manage.py` 的三档隔离把应用库放在 `{root}/moss_{env}.db`，而 `{root}`
    是各档自己定的（`DEV_ROOT` / `TEST_ROOT` / `PILOT_ROOT`）。若这里硬编码
    `data/dev` 之类，就会出现"隔离环境改了、registry 没改"的漂移 ——
    正是本模块要消灭的那类缺陷。

    所以：**应用库在哪，数据根就在哪**。拿不到就退回 `data/<env>`。
    """
    return _env_root_for(current_env())


def _env_root_for(env: str) -> Path:
    """指定环境的数据根。当前环境优先从 `MOSS_SQLITE_PATH` 反推，其余按约定。"""
    if env == current_env():
        app = os.environ.get("MOSS_SQLITE_PATH", "").strip()
        if app:
            parent = Path(app).parent
            if parent.name.lower() == env:
                return parent if parent.is_absolute() else (PROJECT_ROOT / parent)
    return PROJECT_ROOT / "data" / env


def declared_sqlite_paths() -> set[Path]:
    """清单声明的**全部** SQLite 文件路径（per_env 的按三档展开）。

    为什么要展开：`app_db` 一条登记要覆盖 `data/dev/moss_dev.db`、
    `data/test/moss_test.db`、`data/pilot/moss_pilot.db` 三个文件 ——
    只按当前环境解析会让另外两个看起来"未登记"（判据假红）。
    """
    out: set[Path] = set()
    for s in sqlite_stores():
        if s.isolation == "per_env":
            out.update(s.resolved(env) for env in ISOLATED_ENVS)
        else:
            out.add(s.resolved())
    return out


def declared_dir_roots() -> tuple[Path, ...]:
    """清单声明的目录型存储根（落在其下的文件视为"已被覆盖"）。"""
    return tuple(s.resolved() for s in all_stores() if s.kind == "dir")


def unregistered_databases(scan_root: Path | None = None) -> list[Path]:
    """扫描 `data/**.db`，返回**清单没覆盖**的文件。

    覆盖判据（二者之一即算覆盖）：
      ① 它就是某个 sqlite 存储声明的路径（per_env 按三档展开）；
      ② 它落在某个**已声明的目录型存储**之下（如 `data/backups/`、`data/archive/`）。

    ★ 这份判据**只有这一处实现** —— `tests/unit/test_store_registry.py` 与审计
    脚本都调它，否则会出现"测试绿、脚本红"的双份口径。
    """
    root = (scan_root or (PROJECT_ROOT / "data")).resolve()
    declared = {p.resolve() for p in declared_sqlite_paths()}
    covered_dirs = [d.resolve() for d in declared_dir_roots()]
    out: list[Path] = []
    for f in sorted(root.rglob("*.db")):
        if not f.is_file():
            continue
        rp = f.resolve()
        if rp in declared:
            continue
        if any(rp.is_relative_to(d) for d in covered_dirs):
            continue
        out.append(f)
    return out


def get_store(name: str) -> Store:
    for s in all_stores():
        if s.name == name:
            return s
    raise StoreNotFound(
        f"未登记的存储 {name!r} —— 请在 configs/data_stores.yaml 里登记。"
        f"已登记：{[s.name for s in all_stores()]}")


def resolve_store(name: str) -> Path:
    """存储的真实路径（**唯一的解析入口**）。"""
    return get_store(name).resolved()


def store_path(name: str, *parts: str) -> Path:
    """存储下的子路径：`store_path("intel_cache", "prewarm_state.json")`。"""
    return resolve_store(name).joinpath(*parts)


def store_rel(name: str) -> str:
    """存储路径（**相对仓库根**）—— 供"模块自带 config.yaml"的默认值使用。

    ## 为什么要有它（`CHG-0069`）

    一批模块在自己的 `config.yaml` / 数据类里**各写一份路径默认值**
    （`sector_crowding` 3 处、`auction_select` 1 处、`concept_repo` 1 处、
    7 个仓储的构造默认值）。同一个 key 写在 N 处，而其中**没有一处**
    受 `--env` 隔离管 —— 于是 dev/test/pilot 三个实例读写**同一个库**，
    且没有任何地方声明过这件事。

    ## 不在仓库根下时返回**绝对路径**（不是空串）

    单测会把 `MOSS_SQLITE_PATH` 指到 `tmp_path`（仓库外），此时
    `env_root()` 推出来的路径自然也在仓库外。两种处理都不对：

      · 抛异常 → 整个 `Settings()` 起不来（实测：`test_login_gate.py` 12 个 ERROR）；
      · 返回空串 → **静默降级**成"路径是空的"，比抛错更难查。

    返回绝对路径才对 —— 而且**比旧的"写死相对路径"更安全**：
    旧写法下，"把库隔离到 tmp"的测试仍然会把审计/缓存写进**真实仓库**。

    registry 本身读不到时才返回空串（那种情况调用方必须自己报错，
    不能把"读不到清单"伪装成"路径是空的"）。
    """
    try:
        p = resolve_store(name)
    except Exception:  # noqa: BLE001 registry 不可用 → 交由调用方报错
        return ""
    try:
        return p.relative_to(PROJECT_ROOT).as_posix()
    except ValueError:      # 不在仓库根下（如单测的 tmp 目录）→ 只能给绝对路径
        return p.as_posix()


def default_app_db(env: str | None = None) -> str:
    """**本环境**的应用库路径 —— 仓库里"应用库在哪"的唯一默认值。

    ## 为什么默认必须是"本环境的库"而不是遗留主库

    AGENTS.md 的纪律：**默认值即护栏，安全的一侧做成默认**。
    构造仓储时若不传 `db_path`，得到 `data/moss_finagent.db`（三档隔离共用的
    遗留主库）是**危险的一侧**：一次漏传就让隔离档写到共享库上。
    所以默认取 `settings.sqlite_path`（= `MOSS_SQLITE_PATH`，
    正是 `manage.py` 三档隔离注入的那个），拿不到才退回 registry 的
    `legacy_main`（**与改动前的行为一致**，见 `CHG-0069`）。
    """
    try:
        from src.core.config import get_settings

        s = get_settings()
        path = str(getattr(s, "sqlite_path", "") or "").strip()
        if path and (env is None or str(getattr(s, "env", "")) == env):
            return path
    except Exception:  # noqa: BLE001 拿不到配置时退回 legacy_main（= 旧行为）
        pass
    return store_rel("legacy_main")


def sqlite_stores() -> tuple[Store, ...]:
    return tuple(s for s in all_stores() if s.kind == "sqlite")


def protected_stores() -> tuple[Store, ...]:
    """**不得删除**的存储（清理口径必须排除）。"""
    return tuple(s for s in all_stores() if s.protected)


@dataclass(frozen=True)
class WriteDecision:
    """本实例对某存储的写权限决定 + 理由（给人看的）。"""

    store: str
    allowed: bool
    reason: str
    decided: bool = True     # False = 口径待用户裁定，当前**只报告不阻断**

    def to_dict(self) -> dict[str, Any]:
        return {"store": self.store, "write_allowed": self.allowed,
                "reason": self.reason, "decided": self.decided}


def writable_here(name: str) -> WriteDecision:
    """本实例能写这条存储吗？→ **决定 + 人话理由**。

    ## 这条判据现在是**强制**的（`CHG-0087`）

    用户 2026-09-29 裁定：

    > 「共享行情仓，dev 读，pilot 写和读。同时看下更新数据是谁负责的，
    >   谁负责更新数据谁有写权限。」

    于是 `writer: <env>`（点名到具体环境）这一支 `decided=True` ——
    它**必须被拒**，不只是被打印：`QuantWarehouse.upsert()`（唯一的写咽喉）
    在真正写之前调本函数，`allowed=False` 就 fail-closed 抛错。

    仍然**只报告不阻断**的只剩一支：`writer: main` 但本实例是隔离档
    （`decided=False`，§18.4 审计项 A7 的剩余部分）—— 那批共享存储
    （`legacy_main` 等）还没有归属裁定，硬拒会让它们当场失去写者。
    **`warehouse` 已经不属于这一支了**（它的 `writer` 是 `pilot`，已裁定）。

    ## 调用方（写完这条要能答"谁调它、拿掉它哪条测试会红"）

      · `src/quant/warehouse.py::QuantWarehouse.upsert()` —— 写闸门；
      · `src/scheduler/service.py::_tick()` —— 从 `JobSpec.updates` 派生
        "本实例不该触发的作业"（更新作业在只读实例上不触发）；
      · `describe(want_sizes=False)` —— `/health` 里的写权限报告。
    """
    s = get_store(name)
    if not s.writable or s.writer == "none":
        return WriteDecision(s.name, False, "登记为只读（writable: false）")
    env = current_env()
    if s.isolation == "per_env" or s.writer == "own":
        return WriteDecision(s.name, True, "本环境私有，写者=本实例")
    if s.writer in ISOLATED_ENVS:
        #: ★ **点名到某个具体环境**（`CHG-0087`，用户 2026-09-29 裁定）：
        #: 「共享行情仓 —— dev 读，pilot 写和读；**谁负责更新数据谁有写权限**」。
        #: 所以写者不是"主实例"这种模糊说法，而是**承担更新责任的那个环境**。
        if env == s.writer:
            return WriteDecision(
                s.name, True,
                f"登记的写者={s.writer}（**更新数据的责任在它**），本实例就是它")
        return WriteDecision(
            s.name, False,
            f"登记的写者={s.writer}（更新数据的责任在它），"
            f"本实例是 {env!r} → **只读**；"
            f"要改归属就改 configs/data_stores.yaml 的 writer 字段")
    if s.writer == "main":
        env = current_env()
        app = os.environ.get("MOSS_SQLITE_PATH", "")
        is_main = env not in ISOLATED_ENVS or "moss_finagent" in app.lower()
        if is_main:
            return WriteDecision(s.name, True, "共享存储，写者=主实例，本实例即主实例")
        return WriteDecision(
            s.name, False,
            f"共享存储，登记的写者是**主实例**，而本实例是 {env!r} 隔离档 —— "
            "按 §18.4（审计项 A7）应只读；该口径**待裁定**，故此处只报告不阻断",
            decided=False)
    return WriteDecision(s.name, False, f"未知写者声明 {s.writer!r}")


def open_readonly(*names: str) -> tuple[sqlite3.Connection, dict[str, str]]:
    """**一份只读连接**，用 `ATTACH` 把多个 SQLite 存储并到一起。

    返回 `(conn, {存储名: 库别名})`。别名就是存储名，所以可以写
    `SELECT ... FROM warehouse.quant_daily d JOIN app_db.map_quant_sector_stock m ...`
    —— **跨库联查今天做不到的事**（板块映射 × 行情仓估值）由此成立。

    ## 实测（2026-09-28，pilot 环境）

    · `ATTACH` 耗时 **4.6 ms**，零字节复制
    · 跨库 `JOIN` **10.0 ms**（返回真实数据）
    · 三处写入提议**全部被拒**：`attempt to write a readonly database`

    ## 为什么写必须走别的路（而不是"只读连接顺手也能写"）

    SQLite **没有跨库事务** —— 一次写两个库只能半提交。而本项目的溯源规范
    要求每个数据点可归属到来源与操作者，写路径必须**恰好一个库**。
    所以：**读统一（本函数）、写归属（`writable_here`）**。
    """
    if not names:
        raise ValueError("open_readonly() 至少要给一个存储名")
    stores = [get_store(n) for n in names]
    for s in stores:
        if s.kind != "sqlite":
            raise ValueError(f"{s.name} 不是 sqlite（kind={s.kind}），不能 ATTACH")
        if not s.exists:
            raise FileNotFoundError(f"{s.name} 不存在：{s.resolved()}")
    head, rest = stores[0], stores[1:]
    conn = sqlite3.connect(f"file:{head.resolved().as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    aliases = {head.name: "main"}
    try:
        for s in rest:
            conn.execute(
                f"ATTACH DATABASE 'file:{s.resolved().as_posix()}?mode=ro' AS {s.name}")
            aliases[s.name] = s.name
        # 双保险：即便某个库的 mode=ro 被绕过，也不许写。
        conn.execute("PRAGMA query_only=1")
    except Exception:
        conn.close()
        raise
    return conn, aliases


def describe(*, want_sizes: bool = True) -> dict[str, Any]:
    """给 `/health` 与审计脚本的自省视图（含写权限报告）。

    ## 为什么要有 `want_sizes`（本项目的延迟硬约束）

    `Store.size_bytes()` 会**递归整个目录** —— `tushare_partitions` 有 3.5 万个文件，
    实测秒级。把它放进 `/health` 会让每次轮询都去 walk 一遍磁盘，
    正是本项目「一次冷请求固定开销 0.85~0.9s，要减少的是**次数**」要防的东西。

    所以 `/health` 走 `want_sizes=False`（只给名字/路径/写者归属，全走缓存，~0 I/O），
    体积只交给**离线审计脚本**算。
    """
    stores = []
    for s in all_stores():
        d = s.to_dict()
        if want_sizes:
            d["size_mb"] = round(s.size_bytes() / 2**20, 2)
        else:
            d["size_mb"] = None      # 「未量到」≠「量到 0」：不填 0 假装量过
        if s.kind == "sqlite":
            d["write"] = writable_here(s.name).to_dict()
        stores.append(d)
    return {
        "env": current_env(),
        "env_root": env_root().as_posix(),
        "registry": REGISTRY_PATH.relative_to(PROJECT_ROOT).as_posix(),
        "count": len(stores),
        "protected": [s.name for s in protected_stores()],
        "sizes_measured": bool(want_sizes),
        "stores": stores,
    }


__all__ = [
    "PROJECT_ROOT", "REGISTRY_PATH", "ISOLATED_ENVS", "Store", "StoreNotFound",
    "WriteDecision", "all_stores", "current_env", "declared_dir_roots",
    "declared_sqlite_paths", "default_app_db", "describe", "env_root",
    "get_store", "open_readonly", "protected_stores", "registry_path",
    "reset_cache", "resolve_store", "sqlite_stores", "store_path", "store_rel",
    "unregistered_databases", "writable_here",
]
