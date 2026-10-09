"""增量保留清理：认证/告警/通知/行情台账表的分批过期清理。

## 为什么新增这个模块（2026-09-25 数据库审计）

审计发现全项目**只有** `retention_service.py` 在做清理，而它只覆盖
`fact_data_points` 与 `news_cache` 两张表；其余约 40 张表**没有任何清理**。
其中一批是"天然随时间单调增长"的流水/令牌/台账表，而且它们的"过期"与
"撤销"**全是 UPDATE 状态 + 查询时过滤，从不删行** —— 也就是说：

    过期 ≠ 被清理。行永远留在库里。

实测证据（审计时）：
- `fact_session` dev 96 行中 **34 行已撤销、39 行已过期**，全在库里；
- `fact_alerts` 主库 7 行**全部是 `expired` 状态**，一行没删；
- `fact_notify_log` 有一条**不受月配额约束**的写入路径
  （`auth/service.py:450` 登录时账号不存在就写一行），单 IP 可达万行/天；
- `sector_crowding_daily` 218 万行 / `auction_snapshot` 每行 15.7KB。

## 设计约束（对齐本仓既有工程范式）

1. **不新开清理服务**：本模块由 `retention_service.run_retention()` 调用，
   仍然只有"一个清理入口"这件事成立（`scheduler` 侧只有一个
   `data_retention_daily` 作业）。
2. **分批删除是硬要求**：一次性 `DELETE` 几百万行会长时间持有写锁，
   把做T/资金流的写请求全顶成 "database is locked"。
   `event_sqlite_base.py` 里记录了同类教训（"清理写把线程池打满比 500 更糟"）。
   所以每批 `retention_batch_size` 行、批间 `await asyncio.sleep(0)` 让出事件循环。
3. **逐表隔离失败**：一张表删失败不影响其它表（保留是增强能力，不能拖垮主链路）；
   每张表的结果都回报给调用方，便于在作业记录里看到。
4. **不可逆的表默认关闭**：`sector_crowding_daily` / `auction_*` /
   `fact_quant_selection` 这类**历史行情数据**原本是"有意永久保留"的
   （`sector_crowding/refresh.py` 明确写着"不删历史 —— 删历史不可逆，
   停止更新可逆"），所以它们的保留期默认 `0 = 不清理`，由使用者显式开启。
5. **不删审计链**：`fact_registration_review` 是哈希链（删行会断链，且
   AGENTS.md 要求敏感操作日志留存 ≥3 年），**刻意不纳入本模块**。

## 时间列的两种口径

- 认证/告警类：比较 ISO 时间戳（`expires_at` / `at` / `created_at`）。
  会话与令牌额外要求**同时**满足"已过期"或"已撤销"，避免误删活跃凭证。
- 行情台账类：比较 `trade_date`（`YYYYMMDD`）或 `compute_week`（`YYYY-Www`），
  格式不同，所以每张表自带`cutoff`构造方式（见 `_cutoff_for`）。
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from src.core.config import Settings

logger = logging.getLogger(__name__)

#: 打开连接时的忙等上限（秒）。与 `connect_sqlite` 的默认一致：
#: 清理是**后台低优先级**任务，宁可在锁上多等一会儿，也不要因为它
#: 立刻失败而让保留策略长期不生效。
_BUSY_TIMEOUT_MS = 15_000

#: 单条 DELETE 的兜底超时（秒）—— 防止某次删除卡死整个保留作业。
_STATEMENT_TIMEOUT_S = 30.0


# ======================================================================
# 测试进程防护（**结构保证，不靠"记得隔离"**）
# ======================================================================
#
# 本模块做的是**删数据**，而它会被应用启动钩子自动触发
# （`api/main.py` 的 `_run_retention_on_startup`）。两个因素叠加成真实风险：
#
#   1. `TestClient(app)` 作为上下文管理器会跑 lifespan → 启动钩子被触发；
#   2. `main.py:38` 的 `settings` 是**模块导入时**求值的，测试里的
#      `monkeypatch.setenv("MOSS_SQLITE_PATH", ...)` 对它**无效**
#      —— 清理会指向真实库。
#
# 这个仓库已因同一个陷阱出过一次事故：测试夹具的 `clear_all()` 长期在清
# **生产库**的认证表（`core/config.py:83` 有完整记录，表现被伪装成
# "登录莫名 401""接口偶发 429"，极难归因）。靠"每个测试记得隔离"防不住
# （同一个坑已经踩了两次），所以做成结构性检查。
#
# ## 判据为什么是"是否在测试进程里"，而不是"路径像不像临时目录"
#
# ⚠️ 这里必须写清楚，因为我第一版就写反了：若判据是"只有临时库才允许清理"，
# 那**生产库永远不清理** —— 把保留功能整个废掉，而且静默（没人会发现
# 清理没跑）。反过来"只在测试进程里拒绝"才是正确方向：
# 生产照常清理，测试一律不碰。
#
# 识别测试进程用 `PYTEST_CURRENT_TEST` —— pytest 在每个测试运行期间设置的
# 标准环境变量（不是猜测的自定义开关）。测试若要**验证清理逻辑本身**，
# 显式设 `MOSS_RETENTION_IN_TEST=1` 放行（那时库本来就是临时库）。

#: 放行标志：测试里要验证清理逻辑本身时显式打开。
ALLOW_IN_TEST_ENV = "MOSS_RETENTION_IN_TEST"

#: pytest 在每个测试用例运行期间设置的环境变量（标准行为）。
_PYTEST_ENV = "PYTEST_CURRENT_TEST"


def in_test_process() -> bool:
    """当前是否运行在 pytest 进程里（含被测试触发的启动钩子）。"""
    return bool(str(os.environ.get(_PYTEST_ENV, "")).strip())


def destructive_allowed(settings: Settings) -> tuple[bool, str]:
    """现在允许执行**破坏性**清理吗？→ (是否允许, 拒绝原因)。

    这是"保护生产库"的单点判据；`prune_table` 入口统一检查，
    所以启动钩子 / 定时作业 / 直接调用三条路径都被覆盖。
    """
    if str(os.environ.get(ALLOW_IN_TEST_ENV, "")).strip().lower() in (
            "1", "true", "yes"):
        return True, ""
    if in_test_process():
        return False, (
            f"检测到 pytest 进程（{_PYTEST_ENV}）—— 拒绝执行破坏性清理，"
            f"以免测试删掉非隔离库的数据；要测清理本身请设 "
            f"{ALLOW_IN_TEST_ENV}=1")
    return True, ""


@dataclass(frozen=True)
class Cascade:
    """级联删除：父表删了，子表里引用它的行必须一起删，否则留孤儿。

    `child_condition` 里用 `{ids}` 占位符接收父表本批删掉的 id 列表。
    为什么必须显式做级联：本项目 SQLite **默认不开外键**
    （`user_pool_sqlite_repo` 里有"级联删条目"的手写实现，同一个理由），
    所以数据库不会替我们清孤儿行。
    """

    table: str
    child_key: str          # 子表里引用父表的列
    parent_key: str         # 父表的主键列
    child_condition: str = ""   # 额外的子表过滤（可选）


@dataclass(frozen=True)
class RetentionPass:
    """一张表的清理口径。"""

    name: str                       # 表名
    time_column: str                # 用于比较的时间列
    unit: str                       # days | years | weeks
    setting: str                    # `Settings` 上的保留期字段名
    condition: str = ""             # 额外的 WHERE 片段（如"已过期或已撤销"）
    order_by: str = ""              # 删除顺序（默认按 rowid）
    cascades: tuple[Cascade, ...] = field(default_factory=tuple)
    #: 该表是否属于"历史行情数据"（不可逆）。仅用于文案与排序，不改变行为。
    historical: bool = False
    #: 需要对父表**引用它的其它表**做解引用（置空）而不是级联删除。
    #: 例：删 `fact_events` 前要把 `fact_alerts.event_id` 置空，
    #: 否则告警会指向一个不存在的事件。
    dereference: tuple[tuple[str, str], ...] = field(default_factory=tuple)


#: 全部清理口径。
#:
#: ⚠️ 新增一张表时请一并确认三件事：
#:   ① 时间列存在且格式与 `unit` 匹配；② 有没有子表引用它（`cascades` /
#:   `dereference`）；③ 删了会不会断审计链（是的话**不要**加进来）。
PASSES: tuple[RetentionPass, ...] = (
    # ---------- 认证/通知流水（默认开启） ----------
    RetentionPass(
        name="fact_notify_log", time_column="at", unit="days",
        setting="retention_notify_log_days",
    ),
    RetentionPass(
        # 会话：**只有"已过期或已撤销"且已过保留期**的才删。
        # 少了这个额外条件就会删掉活跃会话（把用户踢下线）——
        # 所以它不用 `condition`，而是在 `_where_for` 里专门处理
        # （要比较两列，`condition` 的单一片段表达不了）。
        name="fact_session", time_column="absolute_expires_at", unit="days",
        setting="retention_session_days",
    ),
    RetentionPass(
        # 令牌：撤销行必须留到过期之后 —— 它是重放检测与"撤销整条 family"
        # 的依据（`auth_sqlite_repo` 的 revoke_remember_family）。
        name="fact_remember_token", time_column="expires_at", unit="days",
        setting="retention_remember_token_days",
    ),
    RetentionPass(
        name="fact_verification_code", time_column="expires_at", unit="days",
        setting="retention_verification_code_days",
    ),
    RetentionPass(
        name="fact_password_reset", time_column="expires_at", unit="days",
        setting="retention_password_reset_days",
    ),
    # ---------- 事件告警（默认开启；级联 + 解引用必须成对） ----------
    RetentionPass(
        # 只删**已过期**的告警：`status='expired'` 是阈值引擎按
        # `alert_expire_days` 打的状态，未过期的（含 unread 的）不能碰。
        name="fact_alerts", time_column="expire_time", unit="days",
        setting="retention_alert_days",
        condition="status = 'expired'",
        cascades=(
            # 删告警必须级联删"已读"记录，否则留下引用不存在告警的孤儿行。
            Cascade(table="user_alert_read", child_key="alert_id",
                    parent_key="alert_id"),
        ),
    ),
    RetentionPass(
        # 删事件前先把告警里的引用**置空** —— 告警本身是有效业务记录，
        # 不能因为它挂的事件过期了就一起删掉（那是删业务数据，不是清理）。
        name="fact_events", time_column="created_at", unit="days",
        setting="retention_event_days",
        dereference=(("fact_alerts", "event_id"),),
    ),
    # ---------- 历史行情台账（默认 0 = 不清理；开启即不可恢复） ----------
    RetentionPass(
        name="sector_crowding_daily", time_column="trade_date", unit="years",
        setting="retention_crowding_daily_years", historical=True,
    ),
    RetentionPass(
        name="sector_crowding_metric", time_column="compute_week", unit="weeks",
        setting="retention_crowding_metric_weeks", historical=True,
    ),
    RetentionPass(
        name="auction_snapshot", time_column="trade_date", unit="days",
        setting="retention_auction_days", historical=True,
    ),
    RetentionPass(
        name="auction_feature", time_column="trade_date", unit="days",
        setting="retention_auction_days", historical=True,
    ),
    RetentionPass(
        name="auction_pick", time_column="trade_date", unit="days",
        setting="retention_auction_days", historical=True,
    ),
    RetentionPass(
        # 运行台账：子表按 run_id 级联（`fact_quant_selection_item`
        # 没有外键，不级联就是一堆永远查不到的孤儿行）。
        name="fact_quant_selection", time_column="trade_date", unit="days",
        setting="retention_quant_selection_days", historical=True,
        cascades=(
            Cascade(table="fact_quant_selection_item", child_key="run_id",
                    parent_key="id"),
        ),
    ),
)


def _cutoff_for(pass_: RetentionPass, value: int) -> str:
    """保留期 → 截止值（格式随表的时间列而定）。

    - `days` / `years`：按该表时间列的实际格式输出（`trade_date` 为
      `YYYYMMDD`，ISO 时间列为 `YYYY-MM-DD`），见 `_format_like`。
    - `weeks`：`YYYY-Www`（ISO 周，与 `compute_week` 的实际格式一致，
      实测样例 `2026-W38`）。
    """
    today = date.today()
    if pass_.unit == "years":
        target = _shift_years(today, value)
    elif pass_.unit == "weeks":
        target = today - timedelta(weeks=max(1, value))
        iso = target.isocalendar()
        return f"{iso.year}-W{iso.week:02d}"
    else:
        target = today - timedelta(days=max(1, value))
    return _format_like(target, pass_.time_column)


def _shift_years(base: date, years: int) -> date:
    """往前推 N 年（2/29 → 2/28）。"""
    n = max(1, years)
    try:
        return base.replace(year=base.year - n)
    except ValueError:
        return base.replace(month=2, day=28, year=base.year - n)


def points_cutoff_for_years(years: int) -> str:
    """保留 N 年 → 截止日（`YYYY-MM-DD`），`fact_data_points` 专用。

    ★ 这个值有**两个**消费方，必须同源：

    1. `retention_service` 的 `prune_before(cutoff)` —— 删除早于它的行；
    2. 数据源路由的**回填写入前过滤** —— 不要写注定马上被删的点。

    此前只有第 1 个消费方，于是出现了一个自我抵消的循环：回填照写 10 年
    历史，而保留窗口也是 10 年，**刚写进去的最老那几天立刻被下一次清理删掉**。
    实测该表 180 万行个股日收盘、单次回填 60 万行，主库 6.32GB 里 3.96GB
    是这么产生的空闲页（`auto_vacuum=0`，文件只涨不缩）。
    """
    return _shift_years(date.today(), int(years or 0)).isoformat()


def points_cutoff_date(settings: Settings) -> str:
    """按 `settings.data_retention_years` 取 `fact_data_points` 的截止日。"""
    years = int(getattr(settings, "data_retention_years", 10) or 10)
    return points_cutoff_for_years(years)


def _format_like(target: date, column: str) -> str:
    """按列的实际格式输出截止值。

    `trade_date` 在本项目是 `YYYYMMDD`（无横线，实测 `20260923`），
    而 `expires_at` / `at` / `created_at` 是 ISO（含 `T`）。
    两种混用会让字符串比较得出完全错误的结论（`"2026-09-24" > "20260923"`
    恒为真），所以**必须分开格式化**，不能一律 `isoformat()`。
    """
    if column == "trade_date":
        return target.strftime("%Y%m%d")
    return target.isoformat()


def _existing_tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {str(r[0]) for r in rows}


def _where_for(pass_: RetentionPass) -> str:
    """该表的 WHERE 片段（含条件与时间比较）。"""
    if pass_.name == "fact_session":
        # 会话的特殊口径：**两种"不再有效"各自独立成立**即可删。
        #
        #   ① 绝对到期已过（且过保留期）；
        #   ② 已撤销、且撤销时间已过保留期。
        #
        # ⚠️ 这两个条件必须是 **OR 而不是 AND** —— 实测踩过：
        # 撤销**不重算** `absolute_expires_at`，所以"已撤销"的会话其到期时间
        # 可能还在未来。写成 `到期已过 AND 已撤销` 的话这类行**永远删不掉**
        # （两个条件互斥），被撤销的会话会永久堆积 —— 恰是本模块要消灭的形态。
        #
        # 反之 `revoked_at` 为空的行（活跃会话）两个条件都不成立，安全保留。
        # 保留期各自从**对应时间点**算起，语义正确：撤销后再留 N 天。
        return (
            f"(({pass_.time_column} IS NOT NULL AND {pass_.time_column} != '' "
            f"  AND {pass_.time_column} < ?) "
            f" OR (revoked_at IS NOT NULL AND revoked_at != '' "
            f"     AND revoked_at < ?))"
        )
    parts = [f"{pass_.time_column} IS NOT NULL",
             f"{pass_.time_column} != ''",
             f"{pass_.time_column} < ?"]
    if pass_.condition:
        parts.append(pass_.condition)
    return " AND ".join(parts)


def _params_for(pass_: RetentionPass, cutoff: str) -> list[Any]:
    return [cutoff, cutoff] if pass_.name == "fact_session" else [cutoff]


def _delete_batch(conn: sqlite3.Connection, pass_: RetentionPass,
                  where: str, params: list[Any], batch: int,
                  ) -> tuple[int, list[Any]]:
    """删一批，返回 (删除行数, 本批删掉的**单一父键**值列表)。

    用 `rowid IN (SELECT rowid ... LIMIT n)` 而不是 `DELETE ... LIMIT n`：
    后者在 SQLite 上**默认不编译**（需要 `SQLITE_ENABLE_UPDATE_DELETE_LIMIT`），
    实测不可移植。子查询取 rowid 是标准且高效的做法。

    ⚠️ 只在**恰有一个**父键需要时返回键值 —— 本项目每个 `RetentionPass`
    最多带一种级联（`fact_quant_selection.run_id`、`fact_alerts.alert_id`），
    所以取一个键就够。写死"多父键"支持会引入"哪一列对哪张子表"的歧义，
    而那种歧义一旦搞错就是删错子表。真需要多键时请拆成两个 pass。
    """
    select_ids = (f"SELECT rowid FROM {pass_.name} WHERE {where} "
                  f"LIMIT {int(batch)}")
    ids = [r[0] for r in conn.execute(select_ids, params).fetchall()]
    if not ids:
        return 0, []
    # 先取父键值：级联要用，删掉之后就查不到了。
    parent_vals: list[Any] = []
    if pass_.cascades:
        key = pass_.cascades[0].parent_key
        placeholders = ",".join("?" for _ in ids)
        parent_vals = [
            r[0] for r in conn.execute(
                f"SELECT {key} FROM {pass_.name} "
                f"WHERE rowid IN ({placeholders})", ids).fetchall()]
    placeholders = ",".join("?" for _ in ids)
    cursor = conn.execute(
        f"DELETE FROM {pass_.name} WHERE rowid IN ({placeholders})", ids)
    return int(cursor.rowcount or 0), parent_vals


def _chunked(values: list[Any], size: int = 500) -> list[list[Any]]:
    """`IN (...)` 参数过多会超 SQLite 变量上限（默认 999），切成小批。"""
    return [values[i:i + size] for i in range(0, len(values), size)]


def _run_cascades(conn: sqlite3.Connection, pass_: RetentionPass,
                  parent_vals: list[Any], tables: set[str]) -> int:
    """按父键值删子表行（若有）。返回级联删除行数。

    `tables` 由调用方**传入**而不是在这里查：它每批都会被调用一次，
    而 `_existing_tables` 是一次 `sqlite_master` 扫描 —— 放进循环里
    在几十万行的清理中会被白查几百遍。
    """
    if not parent_vals:
        return 0
    removed = 0
    for cascade in pass_.cascades:
        if cascade.table not in tables:
            # 主库还没有该表（实测 `user_alert_read` 在生产库尚不存在）——
            # 这不是错误，跳过即可，不能因此让整张表的清理失败。
            continue
        extra = f" AND {cascade.child_condition}" if cascade.child_condition else ""
        for chunk in _chunked(parent_vals):
            placeholders = ",".join("?" for _ in chunk)
            cursor = conn.execute(
                f"DELETE FROM {cascade.table} "
                f"WHERE {cascade.child_key} IN ({placeholders}){extra}", chunk)
            removed += int(cursor.rowcount or 0)
    return removed


def _run_dereference(conn: sqlite3.Connection, pass_: RetentionPass,
                     cutoff: str) -> int:
    """把引用本表的列置空（在删除**之前**执行），返回受影响行数。

    为什么是"置空"而不是"级联删除"：告警本身是有效业务记录，
    不能因为它挂的那个事件到期了就把告警一起删掉 —— 那是删业务数据，
    不是清理。所以只摘掉悬空引用。

    ⚠️ `cutoff` 必须由调用方传入**同一个**值（而不是在这里重算），
    否则"置空的范围"与"即将删除的范围"会因跨零点而错开一行。
    """
def _dereference_where(conn: sqlite3.Connection, pass_: RetentionPass,
                       table: str, column: str, cutoff: str,
                       ) -> tuple[str, list[Any]] | None:
    """解引用 UPDATE 的 WHERE 片段与参数（`None` = 该表/列不适用）。

    抽出来的理由：**计数与执行必须用同一个 WHERE** —— 复制一份必然漂移，
    而漂移的症状是"dry-run 说 0 行、真跑却改了 3 行"（先看后做就失去意义）。

    `cutoff` 显式传参（不用模块级变量）：本模块跑在 `asyncio.to_thread`
    的线程池里，用可变全局会被并发的另一张表污染，且症状是"偶发多改一行"。
    """
    if not pass_.dereference:
        return None
    key = _primary_key(conn, pass_.name)
    if not key:
        return None
    if table not in _existing_tables(conn):
        return None
    cols = {str(r[1]) for r in conn.execute(f'PRAGMA table_info("{table}")')}
    if column not in cols:
        return None
    sub = f"SELECT {key} FROM {pass_.name} WHERE {_where_for(pass_)}"
    where = (f"{column} IS NOT NULL AND {column} != '' "
             f"AND {column} IN ({sub})")
    return where, _params_for(pass_, cutoff)


def _count_candidates(conn: sqlite3.Connection, pass_: RetentionPass,
                      cutoff: str, *, limit: int) -> tuple[int, int]:
    """`(待删行数, 待解引用行数)` —— **只读**，不修改任何数据。

    `limit` 与真跑的单轮批量上限一致：dry-run 报的是"**这一轮会动多少**"，
    而不是"理论上总共多少"。两者混用会让人以为一次能清完。
    """
    where = _where_for(pass_)
    params = _params_for(pass_, cutoff)
    row = conn.execute(
        f"SELECT COUNT(*) FROM (SELECT rowid FROM {pass_.name} "
        f"WHERE {where} LIMIT {int(limit)})", params).fetchone()
    pending = int(row[0] or 0) if row else 0

    touched = 0
    for table, column in (pass_.dereference or ()):
        spec = _dereference_where(conn, pass_, table, column, cutoff)
        if spec is None:
            continue
        dwhere, dparams = spec
        r = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {dwhere}",
                         dparams).fetchone()
        touched += int(r[0] or 0) if r else 0
    return pending, touched


def _run_dereference(conn: sqlite3.Connection, pass_: RetentionPass,
                     cutoff: str, *, count_only: bool = False) -> int:
    """按 parent 主键把子表/同表的悬空引用置空。返回受影响行数。

    `count_only=True`（dry-run）：**只数不改**。

    ## 为什么要"置空"而不是"级联删除"

    这些引用指向的是**仍然有效的业务记录**（如告警指向的事件）。清理时
    不能因为它挂的那个事件到期了就把告警一起删掉 —— 那是删业务数据，
    不是清理。所以只摘掉悬空引用。

    ⚠️ `cutoff` 必须由调用方传入**同一个**值（而不是在这里重算），
    否则"置空的范围"与"即将删除的范围"会因跨零点而错开一行。
    """
    if not pass_.dereference:
        return 0
    touched = 0
    for table, column in pass_.dereference:
        spec = _dereference_where(conn, pass_, table, column, cutoff)
        if spec is None:
            continue
        dwhere, dparams = spec
        if count_only:
            row = conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE {dwhere}",
                dparams).fetchone()
            touched += int(row[0] or 0) if row else 0
            continue
        cursor = conn.execute(f"UPDATE {table} SET {column} = '' WHERE {dwhere}",
                              dparams)
        touched += int(cursor.rowcount or 0)
    return touched


def _primary_key(conn: sqlite3.Connection, table: str) -> str:
    """取表的首列主键名（本项目的主键都是单列，见各 `_SCHEMA`）。"""
    for row in conn.execute(f'PRAGMA table_info("{table}")'):
        if int(row[5] or 0) > 0:        # pk 标志
            return str(row[1])
    return ""


def _prune_sync(settings: Settings, pass_: RetentionPass, *,
                dry_run: bool = False) -> dict[str, Any]:
    """同步清理一张表（由 `prune_table` 在线程池里调）。

    `dry_run=True`：**只统计不修改**，用于"先看后做"。
    """
    value = int(getattr(settings, pass_.setting, 0) or 0)
    out: dict[str, Any] = {"table": pass_.name, "deleted": 0,
                           "cascaded": 0, "cutoff": "", "skipped": ""}
    if dry_run:
        out["dry_run"] = True
    if value <= 0:
        out["skipped"] = "保留期未开启（0 = 不清理）"
        return out
    db_path = str(getattr(settings, "sqlite_path", "") or "")
    if not db_path:
        out["skipped"] = "未配置 sqlite_path"
        return out

    cutoff = _cutoff_for(pass_, value)
    out["cutoff"] = cutoff
    batch = max(1, int(getattr(settings, "retention_batch_size", 2000) or 2000))
    max_batches = max(1, int(
        getattr(settings, "retention_max_batches_per_table", 500) or 500))

    conn = sqlite3.connect(db_path, timeout=_BUSY_TIMEOUT_MS / 1000)
    try:
        conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        if pass_.name not in _existing_tables(conn):
            out["skipped"] = "表不存在"
            return out
        tables = _existing_tables(conn)      # 查一次，供级联判断复用
        if dry_run:
            # ★ "先看后做"：**只数不改**。三个动作（解引用 / 删主表 / 级联删子表）
            #   全部走计数分支，且计数与真跑共用同一个 WHERE（见 `_dereference_where`）。
            #   报的是"**这一轮**会动多少"（受 max_batches × batch 限制），
            #   不是"理论上总共多少" —— 两者混用会让人以为一次能清完。
            pending, deref = _count_candidates(
                conn, pass_, cutoff, limit=batch * max_batches)
            out["dereferenced"] = deref
            out["deleted"] = pending
            if pending >= batch * max_batches:
                out["truncated"] = True
            return out
        if pass_.dereference:
            out["dereferenced"] = _run_dereference(conn, pass_, cutoff)
        where = _where_for(pass_)
        params = _params_for(pass_, cutoff)
        for _ in range(max_batches):
            # 每批一个事务：提交后写锁立刻释放，等锁的写请求能插进来。
            with conn:
                deleted, parent_vals = _delete_batch(
                    conn, pass_, where, params, batch)
                if deleted:
                    out["cascaded"] += _run_cascades(
                        conn, pass_, parent_vals, tables)
            # ⚠️ 累加必须在 `break` **之前**：删完的最后一批（往往是不满的一批）
            # 正是行数要被计进去的那批。写在 break 之后等于"最后一批白删"——
            # 实测表现为 `deleted: 0` 而数据确实少了，报告与事实不符。
            out["deleted"] += deleted
            if deleted < batch:
                break
        else:
            out["truncated"] = True     # 达到单轮上限，下一轮继续
        return out
    finally:
        conn.close()


async def prune_table(settings: Settings, pass_: RetentionPass, *,
                      dry_run: bool = False) -> dict[str, Any]:
    """异步清理一张表（逐表隔离失败：抛错也不影响其它表）。

    `dry_run=True`：只报告"这一轮会删/会改多少"，**不动任何数据**。
    """
    allowed, why = destructive_allowed(settings)
    if not allowed and not dry_run:
        # 不是错误、也不抛：被防护挡住时如实回报原因，便于观测
        # （"清理没跑"必须看得见，否则会静默失效）。
        logger.info("保留清理被跳过：%s", why)
        return {"table": pass_.name, "deleted": 0, "cascaded": 0,
                "cutoff": "", "skipped": why}
    try:
        return await asyncio.to_thread(_prune_sync, settings, pass_,
                                       dry_run=dry_run)
    except Exception as exc:  # noqa: BLE001 保留是增强能力，不能拖垮主链路
        logger.warning("保留清理失败 table=%s: %s", pass_.name, exc)
        return {"table": pass_.name, "deleted": 0, "cascaded": 0,
                "cutoff": "", "error": type(exc).__name__}


async def run_passes(settings: Settings, *,
                     dry_run: bool = False) -> list[dict[str, Any]]:
    """跑全部启用的清理口径，返回逐表结果。

    ⚠️ `dry_run=True` 时**刻意绕过 `destructive_allowed` 的 pytest 防护**：
    防护的目的是"别让测试把真库删了"，而 dry-run 本来就不写 ——
    若它也被挡，测试就**无法验证 dry-run 本身**（判据会变成假绿）。
    写路径的防护一点没放松。
    """
    results: list[dict[str, Any]] = []
    for pass_ in PASSES:
        value = int(getattr(settings, pass_.setting, 0) or 0)
        if value <= 0:
            # 未开启的表**不进结果**：否则每次保留作业都返回十几条
            # "skipped"，真正删了东西的信息会被淹没。
            continue
        result = await prune_table(settings, pass_, dry_run=dry_run)
        results.append(result)
        if result.get("deleted") or result.get("cascaded"):
            logger.info(
                "保留清理%s %s：删除 %s 行（级联 %s），截止 %s",
                "（dry-run）" if dry_run else "",
                result["table"], result["deleted"], result.get("cascaded", 0),
                result.get("cutoff", ""))
        # 批间让出事件循环，别让清理把 WS/HTTP 饿死。
        await asyncio.sleep(0)
    return results


__all__ = ["PASSES", "Cascade", "RetentionPass", "prune_table", "run_passes"]
