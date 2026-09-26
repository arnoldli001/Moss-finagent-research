"""告警表 SQLite CRUD 与过期状态机（fact_alerts，EventSqliteRepository混入）。

过期处理（FR-8/AC-6）：读路径懒迁移——list/count/已读前把
expire_time<now 的 active/read 告警统一置为 expired；默认列表隐藏
expired，status='expired' 可显式查询，未读红点到期自动消退。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from src.core.errors import (
    BRIEF_TIGHT,
    brief,
)
from src.domain.alerts.models import DEFAULT_TENANT, Alert
from src.domain.alerts.normalize import now_iso
from src.infrastructure.repositories.event_sqlite_base import (
    _ALERT_COLS,
    EXPIRE_TIMEOUT_S,
    alert_to_row,
    connect_sqlite,
    insert_sql,
    is_locked_error,
    retry_on_locked,
    row_to_alert,
)

logger = logging.getLogger(__name__)


class AlertSqliteMixin:
    """告警CRUD（依赖 EventSqliteStore 的 _connect/_sync_once）。"""

    def _upsert_alerts_sync(self, alerts: list[Alert]) -> dict[str, int]:
        self._sync_once()
        sql = insert_sql("fact_alerts", _ALERT_COLS)

        def _write() -> int:
            inserted = 0
            with self._connect() as conn:
                for alert in alerts:
                    inserted += conn.execute(sql, alert_to_row(alert)).rowcount
            return inserted

        # 单条告警入库失败会丢掉一次推送/邮件，这里对锁竞争做兜底重试。
        inserted = retry_on_locked(_write)
        return {"inserted": inserted, "skipped": len(alerts) - inserted}

    async def upsert_alerts(self, alerts: list[Alert]) -> dict[str, int]:
        return await asyncio.to_thread(self._upsert_alerts_sync, alerts)

    def _delete_alerts_sync(
        self, alert_key_prefix: str, tenant_id: str,
    ) -> dict[str, int]:
        """按 `alert_key` 前缀删除告警 + 对应事件（试验数据回滚专用）。

        ## 为什么用 `instr(alert_key, ?) = 1` 而不是 `LIKE ?`

        `LIKE` 里 `%` 是"任意长度"、`_` 是"任意单字符"。前缀来自调用方，
        一旦它含有这两个字符，`LIKE` 会**多删**：``LIKE 'intel_copy:%'``
        连 `intelXcopy:risk` 一起删掉（`_` 匹配了 `X`）。要让它按字面走就得
        让每个调用点自己记得写 `ESCAPE` 子句 —— 等于把一条安全性质
        交给调用方去记（迟早有人漏）。`instr(x, p) = 1` 是"p 出现在 x 的
        第 1 个字符处"，纯字面比较，`%` / `_` 只是普通字符。

        ⚠️ **不用 `substr(alert_key, 1, ?) = ?`**（虽然它也不是通配符）：
        第二个参数要传"前缀长度"，而 `len()` 数的是 Python 码点、
        SQLite 的 `substr` 数的是**字符（码点）**，对 BMP 之外的字符
        （emoji 等 surrogate pair）两者**不一致** —— 长度传大了截出来的串
        就不等于前缀，表现是**静默地一条都删不掉**（返回 `{"alerts": 0}`，
        脚本还会报"清理完成"）。`instr` 不依赖任何长度计算，没有这个坑。

        ## 为什么事件也一起删（而不是留孤儿事件）

        告警的 `event_id` 指向 `fact_events.event_id`，事件行本身只被这条告警
        引用。只删告警会留下 `intelcopy:` 的孤儿事件：它不再对应任何告警，
        却还留在采集/评估链路里（`list_unanalyzed_events` 会把它当"未评估的
        积压"再捞出来）—— 于是"清干净了"只是看起来干净。
        事件侧按 `event_key` 判前缀（`event_key = f"intelcopy:{h}"`，与
        `alert_key = f"{event_key}:{type}"` 同源，见 `dedup.alert_dedup_key`）。

        ## 两条删除在**同一个事务**里

        `sqlite3` 的隐式事务把两条 `DELETE` 绑成一次原子提交（`with` 退出时
        `commit`）。中途抛锁错误则整体回滚，不会出现"事件删了、告警还在"的
        半清状态 —— 那种状态既占着唯一键（`event_key` UNIQUE），用户重新
        导一次又会被 `INSERT OR IGNORE` 静默跳过。

        写锁重试沿用既有的 `retry_on_locked`（绝不新开一套 sleep 重试：
        项目其它仓储的锁等待分级都在这一个函数里）。
        """
        if not alert_key_prefix:
            # 空前缀 = 删光该租户的全部告警。这是回滚脚本绝不该发生的事，
            # 宁可报错也不执行（`delete_alerts` 的端口契约也这么写）。
            raise ValueError("alert_key_prefix 不能为空（等于删光该租户全部告警）")
        self._sync_once()

        def _purge() -> dict[str, int]:
            with self._connect() as conn:
                n_alerts = conn.execute(
                    "DELETE FROM fact_alerts "
                    "WHERE tenant_id = ? AND instr(alert_key, ?) = 1",
                    (tenant_id, alert_key_prefix),
                ).rowcount
                n_events = conn.execute(
                    "DELETE FROM fact_events "
                    "WHERE tenant_id = ? AND instr(event_key, ?) = 1",
                    (tenant_id, alert_key_prefix),
                ).rowcount
            return {"alerts": int(n_alerts), "events": int(n_events)}

        # 试验回滚是**一次性**动作，撞上主库写锁时排队重试即可
        # （`retry_on_locked` 内部只对 locked/busy 重试，其它错误立即上抛）。
        return retry_on_locked(_purge)

    async def delete_alerts(
        self, alert_key_prefix: str, tenant_id: str = DEFAULT_TENANT,
    ) -> dict[str, int]:
        return await asyncio.to_thread(
            self._delete_alerts_sync, alert_key_prefix, tenant_id)

    # ---------- 保留期清理（"溢出删除"） ----------

    def _prune_alerts_before_sync(
        self, cutoff_text: str, tenant_id: str | None = None,
        *, delete_orphan_events: bool = True,
    ) -> dict[str, int]:
        """删除**触发时间早于** `cutoff_text` 的告警（可按租户限定）。

        与 `_expire_due` 的区别（这是本次新增的能力，两者都要）：
          · `_expire_due` 把到期告警 `status` 置成 `expired` —— **读路径的
            懒迁移**，只是让列表不再显示，**行还在库里**；
          · 本函数真正 `DELETE` —— 用户口径"最多保留三天，超过3天的信息
            自动溢出删除"。只做前者的话 `fact_alerts` 会无限增长。

        ## 时间字段选 `trigger_time` 而不是 `expire_time`

        `expire_time = trigger_time + alert_expire_days`，两者只差一个常量。
        但用 `trigger_time` 的语义更直接："这条告警是什么时候产生的"，
        而保留期说的是"产生超过 N 天的就删"。用 `expire_time` 会让
        "改短 `alert_expire_days`"这个动作**同时**改变保留窗口
        （历史行的 expire_time 是按旧配置算的，改配置后它们的新旧关系
        会突然变化）—— 那是个很难查的耦合。

        ## 孤儿事件也一起删

        与 `_delete_alerts_sync` 同一理由（见那里的长注释）：`fact_events`
        的行只被告警引用，`event_id` 指过去。只删告警会留下孤儿事件，
        而 `list_unanalyzed_events` 会把它们当"未评估的积压"反复捞出来 ——
        "清干净了"只是看起来干净。

        ⚠️ **孤儿事件的"新旧"要按事件自己的日期判，而且不依赖两份时间戳的
        格式一致性**。踩过的两个坑：

        1. `fact_events.created_at` 是**入库时刻**（`'YYYY-MM-DD HH:MM:SS'`，
           无 `T`），而调用方传进来的 `cutoff_text` 是 ISO 带 `T` 的格式 ——
           两者**字符串直接比较会错**，结果是"孤儿事件一条都删不掉"
           且**不报任何错**。
        2. 更根本的是：**`created_at` 不该用来判"事件有多旧"**。补采/回填
           一条两个月前的新闻时 `created_at` 是今天 —— 用入库时间判，它会被
           当成"新鲜事件"永远留着。所以用 `publish_time`（事件发生时间）。

        判据：取 `publish_time` 与 `created_at` 中**较早**的那个日期前缀
        （`[:10]`，两种格式的日期部分一致）。取较早是**保守**方向 ——
        只有"按任何口径都算旧"的事件才会被删。两者都取不到时**保留**
        （宁可留一行，也不误删）。

        ## 两条 DELETE 在同一事务

        `with self._connect()` 退出时一次原子提交。中途抛锁错误则整体回滚，
        不会留下"事件删了、告警还在"的一半状态。
        """
        if not cutoff_text:
            # 空截止日 = 删光全表。这是绝不该发生的事，宁可报错也不执行。
            raise ValueError("cutoff_text 不能为空（等于删光全部告警）")
        self._sync_once()
        tenant_clause = " AND tenant_id = ?" if tenant_id else ""
        tenant_args: tuple[Any, ...] = (tenant_id,) if tenant_id else ()
        # 只比到"天"。两种时间戳格式（ISO 带 T / 空格分隔）的**日期部分**
        # 都是前 10 个字符，所以截断后比较与格式无关。
        cutoff_day = str(cutoff_text)[:10]

        def _purge() -> dict[str, int]:
            with self._connect() as conn:
                n_alerts = conn.execute(
                    "DELETE FROM fact_alerts "
                    f"WHERE trigger_time != '' AND trigger_time < ?{tenant_clause}",
                    (cutoff_text, *tenant_args),
                ).rowcount
                n_events = 0
                if delete_orphan_events:
                    orphans = conn.execute(
                        "SELECT event_id, created_at, publish_time FROM fact_events "
                        "WHERE event_id NOT IN "
                        "(SELECT event_id FROM fact_alerts WHERE event_id != '')"
                        f"{tenant_clause}",
                        tenant_args,
                    ).fetchall()
                    doomed = []
                    for r in orphans:
                        # 取 publish_time（事件发生时间）与 created_at（入库时间）
                        # 中**较早**的日期前缀。取较早是保守方向：只有按任何
                        # 口径都算旧的事件才会被删。两者都取不到 → 保留。
                        days = sorted(
                            str(v)[:10] for v in
                            (r["publish_time"], r["created_at"])
                            if v and len(str(v)) >= 10
                        )
                        if days and days[0] < cutoff_day:
                            doomed.append(r["event_id"])
                    for start in range(0, len(doomed), 500):
                        chunk = doomed[start:start + 500]
                        placeholders = ",".join("?" for _ in chunk)
                        n_events += conn.execute(
                            f"DELETE FROM fact_events WHERE event_id IN ({placeholders})",
                            chunk,
                        ).rowcount
            return {"alerts": int(n_alerts), "events": int(n_events)}

        # 保留清理是后台作业的一次性动作，撞上主库写锁时排队重试即可
        # （`retry_on_locked` 只对 locked/busy 重试，其它错误立即上抛）。
        return retry_on_locked(_purge)

    async def prune_alerts_before(
        self, cutoff_text: str, tenant_id: str | None = None,
        *, delete_orphan_events: bool = True,
    ) -> dict[str, int]:
        """删除触发时间早于 `cutoff_text` 的告警（及因此变成孤儿的事件）。"""
        return await asyncio.to_thread(
            self._prune_alerts_before_sync, cutoff_text, tenant_id,
            delete_orphan_events=delete_orphan_events)

    def _expire_due(self, tenant_id: str, now_text: str) -> bool:
        """懒过期：到期的活动/已读告警统一置expired（参数化SQL）。

        这是**读路径里唯一的一次写**，也是告警列表 500 的直接触发点：
        主库被做T/量化写入者占用时 `UPDATE` 抛
        `sqlite3.OperationalError: database is locked`，整个
        GET /api/v1/alerts 就 500（2026-09-18 backend.log traceback）。

        两条纪律，缺一不可：
        1. 过期是"懒迁移+幂等"的清理动作，不是读结果的一部分 —— 抢不到
           写锁就跳过并返回 False，让列表照常返回；
        2. 抢锁用**独立短超时连接**（EXPIRE_TIMEOUT_S）且**只试一次**，
           绝不复用读连接的长排队参数、也不在这里重试：服务是单事件循环 +
           to_thread 默认线程池，一次 20s 的清理等待会占死一个线程，主库
           繁忙时把线程池打满（实测占锁 12s 时连续 4 次请求全部超时），
           比 500 更糟。实测：占锁时单次 list_alerts 总耗时 ≈0.5s
           （全部是这一次抢锁），SELECT 本身不受 WAL 写事务影响。
        """
        try:
            with connect_sqlite(
                    self._db_path, timeout_s=EXPIRE_TIMEOUT_S,
                    busy_timeout_ms=int(EXPIRE_TIMEOUT_S * 1000)) as conn:
                conn.execute(
                    "UPDATE fact_alerts SET status = 'expired' "
                    "WHERE tenant_id = ? AND status IN ('active', 'read') "
                    "AND expire_time != '' AND expire_time < ?",
                    (tenant_id, now_text),
                )
            return True
        except Exception as exc:  # noqa: BLE001 库繁忙/只读等一律降级
            if is_locked_error(exc):
                logger.warning(
                    "告警懒过期跳过（主库写锁繁忙，不影响本次读取）: %s",
                    brief(exc, BRIEF_TIGHT))
                return False
            raise

    async def list_alerts(
        self, alert_type: str | None = None, alert_level: str | None = None,
        status: str | None = None, limit: int = 100,
        include_expired: bool = False, tenant_id: str = DEFAULT_TENANT,
        *, user_id: str = "",
    ) -> list[Alert]:
        return await asyncio.to_thread(
            self._list_alerts_sync, alert_type, alert_level, status,
            limit, include_expired, tenant_id, user_id)

    def _list_alerts_sync(
        self, alert_type: str | None, alert_level: str | None,
        status: str | None, limit: int, include_expired: bool,
        tenant_id: str, user_id: str = "",
    ) -> list[Alert]:
        """列告警。

        ## `status` 的语义在传了 `user_id` 时会变（这是刻意的）

        原来的 `status` 是 `fact_alerts` 上的单值（`active`/`read`/`expired`），
        而"已读"是按用户的概念 —— 两者混在一个字段里就必然出错
        （实测：一个人点已读全员清零）。所以：

            传了 user_id：
                status="active" → **该用户未读**的告警
                status="read"   → **该用户已读**的告警
                status="expired"→ 生命周期已过期（与用户无关）
            没传 user_id：保留旧的全局语义（内部调用方与既有测试）

        这个"同名不同义"有点危险，所以**只有这一个地方做转换**，
        而且注释写在这里 —— 换个地方再解释一遍就会漂移。
        """
        self._sync_once()
        sql = "SELECT * FROM fact_alerts WHERE tenant_id = ?"
        params: list[Any] = [tenant_id]
        # 读路径一律短超时：读本身在 WAL 下几乎不阻塞，但不该为写锁排队 20s。
        with self._connect_fast() as conn:
            self._expire_due(tenant_id, now_iso())
            if user_id:
                # `NOT EXISTS` 比 LEFT JOIN 更直白，且走 user_alert_read
                # 的主键索引（user_id, alert_id）
                unread = ("NOT EXISTS (SELECT 1 FROM user_alert_read r "
                          "WHERE r.user_id = ? AND r.alert_id = a.alert_id)")
                read = ("EXISTS (SELECT 1 FROM user_alert_read r "
                        "WHERE r.user_id = ? AND r.alert_id = a.alert_id)")
                sql = ("SELECT a.* FROM fact_alerts a "
                       "WHERE a.tenant_id = ?")
                # ⚠️ 条件是 `IN ('active','read')`：旧行为在行上留下过
                # 全局 `read`（某个人点过已读），那些行在新语义下对**每个人**
                # 都是未读，除非他自己标记过 —— 所以不能用 `= 'active'` 排除。
                if status == "active":
                    sql += f" AND a.status IN ('active','read') AND {unread}"
                    params.append(user_id)
                elif status == "read":
                    sql += f" AND a.status IN ('active','read') AND {read}"
                    params.append(user_id)
                elif status == "expired":
                    sql += " AND a.status = 'expired'"
                elif not include_expired:
                    # 默认隐藏过期；未读/已读都算"没过期"
                    sql += " AND a.status <> 'expired'"
            elif status:
                sql += " AND status = ?"
                params.append(status)
            elif not include_expired:
                sql += " AND status <> 'expired'"  # 默认隐藏过期
            for column, value in (
                ("alert_type", alert_type), ("alert_level", alert_level),
            ):
                if value:
                    sql += f" AND {column} = ?"
                    params.append(value)
            order = "a.alert_id" if user_id else "alert_id"
            trig = "a.trigger_time" if user_id else "trigger_time"
            sql += f" ORDER BY {trig} DESC, {order} DESC LIMIT ?"
            params.append(int(limit))
            rows = conn.execute(sql, params).fetchall()
            # `row_to_alert` 只读 `fact_alerts`，所以它给出的 `status` 是
            # **全局**值（`active`/`read`/`expired`）—— 而"已读"是按用户的。
            # 传了 `user_id` 时必须在同一个连接里把该用户的已读集合查出来，
            # 再逐条覆盖 `status`，否则前端会拿到别人的已读状态。
            read_ids: set[str] = set()
            if user_id and rows:
                read_ids = {
                    str(r["alert_id"]) for r in conn.execute(
                        "SELECT alert_id FROM user_alert_read WHERE user_id = ?",
                        (user_id,)).fetchall()
                }
        out: list[Alert] = []
        for r in rows:
            alert = row_to_alert(r)
            if user_id:
                # 生命周期以 `fact_alerts` 为准；只有 `active` 才可能"已读"
                if str(alert.status) != "expired":
                    alert.status = ("read" if str(alert.alert_id) in read_ids
                                    else "active")
            out.append(alert)
        return out

    async def get_alert(
        self, alert_id: str, tenant_id: str = DEFAULT_TENANT,
    ) -> Alert | None:
        def _query() -> Alert | None:
            self._sync_once()
            with self._connect_fast() as conn:
                self._expire_due(tenant_id, now_iso())
                row = conn.execute(
                    "SELECT * FROM fact_alerts WHERE alert_id = ? AND tenant_id = ?",
                    (alert_id, tenant_id),
                ).fetchone()
            return row_to_alert(row) if row else None
        return await asyncio.to_thread(_query)

    async def mark_read(
        self, alert_id: str, tenant_id: str = DEFAULT_TENANT,
        *, user_id: str = "",
    ) -> bool:
        """标记一条为**该用户**已读。

        ## ⚠️ 为什么需要 `user_id`（实测发现的漏洞）

        原来只有 `UPDATE fact_alerts SET status='read'` —— 而告警行是按
        `tenant_id` 共享的。实测：pilot 库 47 条告警**全部**是
        `tenant_id='tenant_001'`，而系统里有 13 个 vip 用户。
        于是**任何一个用户点已读，全体 13 个人的未读角标一起清零** ——
        每个用户的已读状态根本不存在。

        现在改为写 `user_alert_read`（按 `user_id`+`alert_id`）。
        `fact_alerts.status` 保留，但只表示**告警自身的生命周期**。

        ⚠️ `user_id` 为空时**退回旧的全局行为** —— 那是为了不改动
        内部调用方与既有测试；接口层必须传 `user_id`。
        """
        def _update() -> bool:
            self._sync_once()
            # 用户点击的接口用短超时连接：宁可快速失败让前端重试，
            # 也不要把一个请求钉在写锁上 20s。
            with self._connect_fast() as conn:
                self._expire_due(tenant_id, now_iso())
                if user_id:
                    # 先确认这条告警在该租户下存在且**未过期** ——
                    # 否则会替一个不存在的 alert_id 写读记录（脏数据）。
                    #
                    # ⚠️ 条件是 `status IN ('active','read')` 而**不是**
                    # `= 'active'`。库里有旧行为留下的 8 条全局 `read`
                    # （某个人点过"已读"，把行改成了 read）—— 只认 active
                    # 的话那 8 条**谁也标不动**，接口会报"告警不存在或已读"
                    # 而用户看不出为什么。在新语义下它们对每个人都应该是
                    # **未读**（除非他自己标记过），所以必须允许写读记录。
                    row = conn.execute(
                        "SELECT 1 FROM fact_alerts "
                        "WHERE alert_id = ? AND tenant_id = ? "
                        "AND status IN ('active', 'read')",
                        (alert_id, tenant_id)).fetchone()
                    if not row:
                        return False
                    conn.execute(
                        "INSERT OR IGNORE INTO user_alert_read "
                        "(user_id, alert_id, read_at) VALUES (?, ?, ?)",
                        (user_id, alert_id, now_iso()))
                    # ⚠️ 幂等：重复标记也要返回 True。
                    # 用 `INSERT OR IGNORE` 时重复的 rowcount 是 0 ——
                    # 若按它判断，接口会报 404"告警不存在或已读"，
                    # 而用户只是又点了一次。所以这里不判 rowcount。
                    return True
                cur = conn.execute(
                    "UPDATE fact_alerts SET status = 'read' "
                    "WHERE alert_id = ? AND tenant_id = ? AND status = 'active'",
                    (alert_id, tenant_id),
                )
                return cur.rowcount > 0
        return await asyncio.to_thread(_update)

    async def mark_all_read(self, tenant_id: str = DEFAULT_TENANT,
                            *, user_id: str = "") -> int:
        """把该租户**当前所有 active 告警**标记为**该用户**已读。

        返回"新标记了几条"（已读过的重复标记不计），前端据此提示。
        """
        def _update() -> int:
            self._sync_once()
            with self._connect_fast() as conn:
                self._expire_due(tenant_id, now_iso())
                if user_id:
                    # `INSERT OR IGNORE ... SELECT`：一条 SQL 把"该租户所有
                    # 未过期告警"逐个写进读表，重复的自动跳过。
                    # 用 `SELECT changes()` 取真实插入数 ——
                    # `cur.rowcount` 在 `INSERT OR IGNORE` 下不反映跳过的行。
                    conn.execute(
                        "INSERT OR IGNORE INTO user_alert_read "
                        "(user_id, alert_id, read_at) "
                        "SELECT ?, alert_id, ? FROM fact_alerts "
                        "WHERE tenant_id = ? AND status IN ('active','read')",
                        (user_id, now_iso(), tenant_id))
                    return int(conn.execute(
                        "SELECT changes() AS n").fetchone()["n"])
                cur = conn.execute(
                    "UPDATE fact_alerts SET status = 'read' "
                    "WHERE tenant_id = ? AND status = 'active'", (tenant_id,))
                return cur.rowcount
        return await asyncio.to_thread(_update)

    async def count_unread(self, tenant_id: str = DEFAULT_TENANT,
                           *, user_id: str = "") -> int:
        """该用户**未读**的 active 告警数。

        未读 = 该租户 `active` 的告警 − 该用户在读表里的记录。
        """
        def _count() -> int:
            self._sync_once()
            with self._connect_fast() as conn:
                self._expire_due(tenant_id, now_iso())
                if user_id:
                    row = conn.execute(
                        "SELECT COUNT(*) AS n FROM fact_alerts a "
                        "WHERE a.tenant_id = ? "
                        "AND a.status IN ('active','read') "
                        "AND NOT EXISTS (SELECT 1 FROM user_alert_read r "
                        "  WHERE r.user_id = ? AND r.alert_id = a.alert_id)",
                        (tenant_id, user_id)).fetchone()
                else:
                    row = conn.execute(
                        "SELECT COUNT(*) AS n FROM fact_alerts "
                        "WHERE tenant_id = ? AND status = 'active'",
                        (tenant_id,)).fetchone()
            return int(row["n"])
        return await asyncio.to_thread(_count)

    async def last_alert_time(
        self, alert_key: str, tenant_id: str = DEFAULT_TENANT,
    ) -> str | None:
        def _query() -> str | None:
            self._sync_once()
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT trigger_time FROM fact_alerts "
                    "WHERE tenant_id = ? AND alert_key = ?",
                    (tenant_id, alert_key),
                ).fetchone()
            return row["trigger_time"] if row else None
        return await asyncio.to_thread(_query)

    async def last_content_alert_time(
        self, content_key: str, tenant_id: str = DEFAULT_TENANT,
    ) -> str | None:
        """跨源同文抑制：查该租户同content_key最近一次告警时间。"""
        if not content_key:
            return None

        def _query() -> str | None:
            self._sync_once()
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT trigger_time FROM fact_alerts "
                    "WHERE tenant_id = ? AND content_key = ? "
                    "ORDER BY trigger_time DESC LIMIT 1",
                    (tenant_id, content_key),
                ).fetchone()
            return row["trigger_time"] if row else None
        return await asyncio.to_thread(_query)
