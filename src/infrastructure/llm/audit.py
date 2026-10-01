"""LLM调用哈希审计日志（JSONL追加写，不可变）。

每条记录：时间戳、任务层级、模型、prompt/response哈希、token数、
延迟、缓存命中、降级链——满足"所有LLM调用记录Trace"的架构红线。
"""

from __future__ import annotations

import json
import threading
from collections import deque
from datetime import datetime
from pathlib import Path

from src.infrastructure.llm.models import LLMResponse

#: 尾部统计的默认上限（行）。与访问审计同一个量级：够覆盖一个月的
#: 逐次调用记录，又不至于在监控页首次打开时顶出内存尖峰。
MAX_TAIL_LINES = 200_000


def principal_ids() -> tuple[str, str]:
    """当前上下文里的 `(tenant_id, user_id)`；取不到就返回空串。

    为什么"取不到就空串"而不是编一个默认租户：后台任务/脚本产生的调用
    **确实不属于任何租户**。编一个（比如 `default`）会让这些用量混进那个
    租户的 token 账单里，看起来"某个租户用超了"，而真相是平台自身的开销 ——
    这种错误比少一个字段更难查。空串会归到 `(未归属)`，一眼能看出是平台用量。

    ⚠️ 用 `current_principal()`（返回哨兵）而不是 `require_principal()`
    （抛异常）：审计写入**绝不能**因为没有身份而失败 —— LLM 调用已经发生、
    钱已经花了，这条记录必须落盘。
    """
    try:
        from src.core.tenancy import current_principal, is_authenticated

        # ★ 必须用 `is_authenticated()` 判，而不是"principal 是不是 None"。
        #   未认证时 `current_principal()` 返回的是一个**哨兵对象**，
        #   它的 `user_id` 字面量是 `"__unauthenticated__"` —— 直接读字段
        #   会把这个哨兵当成真实用户名写进审计，于是统计里出现一个
        #   名叫 `__unauthenticated__` 的"用户"，而它其实代表"没有身份"。
        if not is_authenticated():
            return "", ""
        principal = current_principal()
    except Exception:  # noqa: BLE001 审计不能因为环境问题丢记录
        return "", ""
    if principal is None:
        return "", ""
    return (str(getattr(principal, "tenant_id", "") or ""),
            str(getattr(principal, "user_id", "") or ""))


def _resolve_identity(ctx) -> tuple[str, str, str]:
    """定这次调用记在谁名下 → `(tenant_id, user_id, 来源)`。

    ## 优先级：**会话身份 > tenancy Principal**

    `Principal` 那一侧在生产上目前是 `_LOCAL_DEV`（`tenant_id=local`,
    `user_id=local-dev`, `auth_source=dev-bypass`）—— 实测 50764 条访问审计
    **全部**是它。拿它当账本等于给每个租户记到同一个假身份上。

    所以先看会话（`AccountingMiddleware` 绑定的真实登录用户），
    没有再退回 Principal（Bearer 令牌调用方，那些是真身份；dev-bypass 也会
    如实带 `tenant_source="principal"` 落到 `local`，与改动前一致）。
    """
    if getattr(ctx, "has_identity", False):
        return ctx.tenant_id, ctx.user_id, "session"
    tenant_id, user_id = principal_ids()
    if tenant_id or user_id:
        return tenant_id, user_id, "principal"
    return "", "", "none"


class LLMAuditLog:
    """线程安全的JSONL审计追加器。"""

    def __init__(self, audit_dir: str | None = None) -> None:
        # 默认从 registry 取（CHG-0071）；**必须补回落** —— 只把默认值改成
        # `None` 会把 `Path(None)` 抛给所有不传参的调用方（CHG-0069 的教训）。
        if audit_dir is None:
            from src.infrastructure.catalog.data_stores import store_rel

            audit_dir = store_rel("llm_audit")
        self._dir = Path(audit_dir)
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        self._dir.mkdir(parents=True, exist_ok=True)
        return self._dir / "llm_audit.jsonl"

    def record(
        self,
        *,
        trace_id: str,
        agent_id: str,
        task_tier: str,
        response: LLMResponse,
        cached: bool,
        error: str | None = None,
    ) -> dict:
        from src.core import accounting

        ctx = accounting.current()
        tenant_id, user_id, tenant_source = _resolve_identity(ctx)
        entry = {
            "ts": datetime.now().isoformat(),
            "trace_id": trace_id,
            "agent_id": agent_id,
            "task_tier": task_tier,
            # ★ 租户归属：**没有这两个字段，"套餐里的 token 上限"就永远算不出
            #   "还剩多少"**。监控页要按租户显示用量与剩余额度，而用量只能
            #   从这份审计累计（它是唯一逐次记录 tokens_in/tokens_out 的地方）。
            #
            #   `src/infrastructure/security/rls.py` 的 GLOBAL_TABLES 注释里
            #   早就写着 llm_audit "已按租户写入 user_id/tenant_id 字段"——
            #   而代码里一直没写。注释与实现不一致比没注释更糟：读到它的人
            #   会据此判断"这里不用再管租户归属"。
            "tenant_id": tenant_id,
            "user_id": user_id,
            # 这两个字段是"归属是怎么来的"，用于排障：
            #   session   = 会话 Cookie（真实的人，2026-09-26 起）
            #   principal = tenancy Principal（Bearer/开发头；可能是 dev-bypass）
            #   none      = 后台作业/脚本（**不是**"忘了写"）
            # 没有它的话，管理员无法区分"这个 (未归属) 是平台自身开销"
            # 还是"会话桥又断了"。
            "tenant_source": tenant_source,
            # ★ 触发本次调用的 HTTP 路径（`""` = 非请求触发）。
            #   运维页靠它回答"钱花在前端哪个功能上" —— 在此之前这个信息
            #   在审计里**根本不存在**，只能按 agent 名字猜。
            "path": ctx.path,
            "model": response.model_used,
            "provider": response.provider,
            "prompt_hash": response.prompt_hash,
            "response_hash": response.response_hash,
            "tokens_in": response.tokens_in,
            "tokens_out": response.tokens_out,
            # ★ 2026-09-28 第十二轮：思维链 token 占比。
            #   让"延迟归因"可被数据回答：
            #     reasoning_tokens / tokens_out 高 → 降 effort 见效
            #     正文占大头                     → 只能压缩输出长度
            #   0 表示后端未提供该字段（本地 Ollama），不是"没有思考"。
            "reasoning_tokens": response.reasoning_tokens,
            "latency_ms": response.latency_ms,
            "cache_hit": cached,
            "cache_kind": response.cache_kind,
            "fallback_used": response.fallback_used,
            "provider_chain": response.provider_chain,
            "error": error,
        }
        line = json.dumps(entry, ensure_ascii=False)
        with self._lock, self.path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        return entry

    def read_all(self, limit: int | None = None) -> list[dict]:
        """按时间正序读取（limit取最近N条）。"""
        if not self.path.exists():
            return []
        lines = self.path.read_text(encoding="utf-8").splitlines()
        if limit is not None:
            lines = lines[-limit:]
        return [json.loads(line) for line in lines if line.strip()]

    def window_rows(self, *, since: str = "", limit: int = MAX_TAIL_LINES,
                    ) -> tuple[list[dict], bool]:
        """读**尾部若干行**中 `ts >= since` 的记录 → `(rows, 是否被截断)`。

        这是运维页唯一的 LLM 审计入口：token 用量与**费用**都从同一批行算，
        所以必须只读一遍文件 —— 页面上有两个数字就扫两遍文件，会把
        "打开监控页"变成一次几十 MB 的重复 IO（而这一页还是 10 秒自动刷新的）。

        取舍与 `tokens_by_tenant` 完全一致（流式读尾部、只比到秒、
        截断必须让上层看得见），只是把"算 token"换成"交出行"，
        由调用方（`src/domain/platform/llm_cost.py`）决定怎么聚合。
        """
        path = self.path
        if not path.exists():
            return [], False
        tail: deque[str] = deque(maxlen=max(1, limit))
        total = 0
        try:
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if line.strip():
                        total += 1
                        tail.append(line)
        except OSError:
            return [], False
        cutoff = since[:19]
        out: list[dict] = []
        for line in tail:
            try:
                row = json.loads(line)
            except ValueError:
                continue              # 单行损坏只跳过它
            if cutoff and str(row.get("ts") or "")[:19] < cutoff:
                continue
            out.append(row)
        return out, total > len(tail)

    def tokens_by_tenant(
        self, *, since: str = "", limit: int = MAX_TAIL_LINES,
    ) -> tuple[dict[str, int], bool]:
        """按租户累计 token 用量 → `({tenant_id: tokens}, 是否被截断)`。

        用法：监控页要显示"本月 token 还剩多少"，而套餐上限
        （`TierPlan.resources["llm_tokens_per_month"]`）是**月**口径，
        所以调用方传本月起点（`YYYY-MM-01T00:00:00`）。

        ## 三个刻意的取舍

        1. **流式读尾部，不 `read_text()` 整个文件**：文件会一直长
           （配置里按 20 万条估过空间），一次性读进来会在监控页首次打开时
           顶出一个几十上百 MB 的内存尖峰 —— 而这台机器同时在跑行情任务。
        2. **比较时间只取前 19 个字符**（`YYYY-MM-DDTHH:MM:SS`）：
           写入用的是 `datetime.now().isoformat()`（**无时区**），
           而带时区的字符串（`...+08:00`）与无时区字符串直接字典序比较
           没有意义。截到秒既保留了分钟级精度，又让两种写法都能比。
        3. **截断要能被上层看见**：文件超过 `limit` 行时只统计了尾部，
           累计值会**偏低**。返回布尔值让调用方如实标注，而不是把偏小的
           用量当成准确账单（与访问审计同样的处理）。
        """
        rows, truncated = self.window_rows(since=since, limit=limit)
        out: dict[str, int] = {}
        for row in rows:
            tenant = str(row.get("tenant_id") or "") or "(未归属)"
            out[tenant] = out.get(tenant, 0) + int(row.get("tokens_in") or 0) \
                + int(row.get("tokens_out") or 0)
        return out, truncated
