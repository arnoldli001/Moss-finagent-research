"""记账归属上下文：这次 LLM 调用是**替谁**、**在哪个界面**发生的。

## 为什么需要它（2026-09-26 实测）

运维页要回答三个问题：**花了多少钱 / 花在哪个功能上 / 是哪个租户花的**。
而两份审计里当时都答不出来：

    data/audit/access_audit.jsonl   50764 行，**每一行**都是
                                    tenant_id=local, user_id=local-dev,
                                    auth_source=dev-bypass
    data/audit/llm_audit.jsonl      44145 次调用里带 tenant_id 的只有
                                    621 次，且 tenant 是 `local`

原因很直接：`TenancyMiddleware` 的凭证解析**只认 Bearer 令牌与开发用请求头**
（`resolve_principal`），**不认本项目的会话 Cookie** —— 于是每个浏览器请求都落到
`_LOCAL_DEV` 兜底身份上；而 `LLMAuditLog.record` 的租户归属取自
`core.tenancy` 的当前 Principal，也就跟着变成空或 `local`。

结果就是"按租户监控"这一栏在生产上**只有一个 local 行**，而"钱花在哪个功能"
连字段都没有。

## 这一层与 `Principal` 的分工（刻意不合并）

- `Principal`（`core.tenancy`）认 **Bearer / 开发头**，用于**授权**：
  数据分级、租户隔离、中国墙。改它 = 改"谁能看哪类数据"。
- 本模块认 **会话 Cookie**，只用于**记账归属**（钱、token、调用）。
  打开它只多几个审计字段，不影响任何准入判断。

把会话硬塞进 `Principal` 也能让审计好看，但那会顺带改变授权语义
（`tenant_id` 会从"数据租户"变成"套餐等级"，一旦有人打开
`MOSS_TENANCY_ENFORCE` 就是一次静默的权限变更）。所以这里用**独立的一层**：
只为记账服务，谁都不拦。

## `path` 为什么值钱

"钱花在前端哪个功能"以前无法回答（LLM 审计里根本没有请求信息，访问审计里
也没有 token）。本模块让每次请求把自己的**路径**放进上下文，`LLMAuditLog.record`
随行落盘 —— 于是 `path → 功能` 是一条**精确**的映射，而不是按 agent 名字猜。

⚠️ 归属推断只对**新写入**的行精确：历史行没有 `path` 字段，只能按 agent 名字
归类（`src/domain/platform/llm_cost.py` 里如实标注）。
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from contextvars import ContextVar
from dataclasses import dataclass

__all__ = ["AccountingContext", "current", "use"]


@dataclass(frozen=True)
class AccountingContext:
    """一次请求（或一段作业）的记账归属。三个字段都可为空。"""

    #: 触发本次工作的 HTTP 路径（`""` = 非请求触发：定时作业 / 命令行脚本）
    path: str = ""
    #: 租户 = 套餐等级（`admin` / `vip` / `trial`），与 `platform_tiers.json` 同口径
    tenant_id: str = ""
    user_id: str = ""

    @property
    def has_identity(self) -> bool:
        return bool(self.tenant_id or self.user_id)


_EMPTY = AccountingContext()

#: 当前上下文。默认空（后台任务/脚本），由中间件在请求入口设置。
_current: ContextVar[AccountingContext] = ContextVar("moss_accounting",
                                                     default=_EMPTY)


def current() -> AccountingContext:
    """当前记账上下文；没有绑定时返回空上下文（**不抛异常**）。

    为什么"取不到就空"而不是报错：绝大多数 LLM 调用是后台作业/脚本发起的，
    它们**本来就不属于任何租户**；为空会被如实归到 `(未归属)`，
    而抛异常只会让"钱已经花了"的那条审计写不下去。
    """
    return _current.get()


@contextlib.contextmanager
def use(ctx: AccountingContext) -> Iterator[AccountingContext]:
    """在 `with` 块内绑定记账上下文，退出时恢复。

    ⚠️ 用 `token` 恢复而不是"再 set 一次空值"：后者在嵌套场景下会把
    外层上下文抹掉（中间件套中间件、或作业里再发请求）。
    """
    token = _current.set(ctx)
    try:
        yield ctx
    finally:
        _current.reset(token)
