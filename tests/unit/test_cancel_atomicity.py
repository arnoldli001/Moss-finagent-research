"""任务终止的**原子性**判据（`CHG-0132`）。

## 为什么要有这个文件

用户问的是：「我刚才手动停止了投研分析，为什么最后结果还是输出了？
没有加 actor 端到端原子化任务终止吗？」

查下来**当次那个结果不是被取消的任务产出的**（被取消的 `7f4446ef` 没有留下
任何采集汇总；结果是 41 秒后**新发起**的 `645def22` 产出的）。但顺着这条问,
在链路里查出了两个**真**缺陷 —— 它们都会造成"点了停止，结果照样出来"的形状：

| # | 缺陷 | 后果 |
|---|---|---|
| ① | `TaskCancelledError` 继承 `Exception` | 图里 18+ 处 `except Exception`（注释都写着"单节点失败不拖垮整图"）会把**取消**降级成**一次节点故障**，图继续走 |
| ② | `cancel_task` 等句柄至多 2 秒后**无条件**写 `cancelled`，且**一行日志都不打** | 取消晚于完成时 → 「状态说已取消、报告已生成并进了结果缓存」；且事后**无从追溯**（824,620 行日志里零取消痕迹） |

本文件把这两条都变成判据。第 ① 条之所以必须改**类型**而不是"多写几个 if"：
Python 自己就是这么定的 —— `asyncio.CancelledError` 从 3.8 起改成 `BaseException`，
理由一模一样。
"""
from __future__ import annotations

import asyncio

import pytest

from src.api.tasks import TaskStore
from src.core.cancel import CancellationToken, TaskCancelledError

_REQUIRED = dict(trace_id="t", tenant_id="tenant", query="q",
                 analysis_type="full", target="")


# ─────────────── ① 取消不许被 `except Exception` 吞掉 ───────────────


def test_cancellation_is_not_an_exception_subclass() -> None:
    """★ 核心判据：`TaskCancelledError` 不是 `Exception`。

    这条红了**不要改判据** —— 它红了意味着"取消"又能被兜底吞掉了
    （`CHG-0132` 修的就是这个）。
    """
    assert not issubclass(TaskCancelledError, Exception), (
        "`TaskCancelledError` 又变回 Exception 了 ⇒ 图里那些 "
        "`except Exception: 单节点失败不拖垮整图` 会把『用户取消』降级成"
        "『一次节点故障』，任务继续跑（见 supervisor.py:2901-2906 的形状）"
    )
    assert issubclass(TaskCancelledError, BaseException), "至少要能被显式捕获"


def test_except_exception_cannot_catch_a_cancellation() -> None:
    """自证：`except Exception` 抓不到它，而 `except BaseException` 抓得到。

    没有这条自证，"不是 Exception" 这个断言可能只是换了个名字而已。
    """
    token = CancellationToken("t")
    token.cancel("user_requested")
    branch = None
    try:
        token.check()
    except Exception:  # noqa: BLE001
        branch = "exception"
    except BaseException:  # noqa: BLE001
        branch = "base"
    assert branch == "base", f"取消信号落到了 {branch} 分支 ⇒ 仍会被兜底吞掉"


def test_supervisor_shaped_node_lets_the_cancellation_out() -> None:
    """★ 照搬 `supervisor.py:2901-2906` 的**真实形状**：取消必须传出节点。

    这条是本轮的关键：那个 `except Exception` 抓的是 `_run_agent`（Agent 内部
    `react.py:181` 也会 `token.check()`）⇒ 修好之前，一次取消会被它变成
    `{'errors': ['意外异常 user_requested']}`，节点"正常返回"，图继续走。
    """

    def supervisor_shaped_node(check) -> dict:  # noqa: ANN001
        try:
            check()                       # ← agent 内部的 token.check()
            return {"ok": True}
        except Exception as exc:          # noqa: BLE001 单节点失败不拖垮整图
            return {"errors": [f"意外异常 {exc}"]}

    token = CancellationToken("t")
    token.cancel("user_requested")
    with pytest.raises(TaskCancelledError):
        supervisor_shaped_node(token.check)


def test_node_entry_check_also_propagates() -> None:
    """节点**入口**的检查（在 try 之外）本来就会传出 —— 保留这条防回归。"""
    token = CancellationToken("t")
    token.cancel()
    with pytest.raises(TaskCancelledError):
        token.check()


# ─────────────── ② 取消必须留痕、且不许覆盖完成态 ───────────────


def test_cancel_of_running_task_marks_cancelled_and_logs(caplog) -> None:
    """在飞任务被取消：状态置 cancelled、句柄被取消、**并且留下一条日志**。"""

    async def _scenario():
        store = TaskStore()
        store.create("t-run", **_REQUIRED)
        store.register_token("t-run", CancellationToken("t-run"))

        async def _forever() -> None:
            await asyncio.sleep(3600)

        handle = asyncio.create_task(_forever())
        store.register_handle("t-run", handle)
        with caplog.at_level("INFO"):
            ok = await store.cancel_task("t-run")
        return ok, store.get("t-run"), handle

    ok, record, handle = asyncio.run(_scenario())
    assert ok is True
    assert record is not None and record.status == "cancelled"
    assert handle.done(), "句柄没被取消 ⇒ 任务还在后台跑"
    assert any("任务已取消" in r.getMessage() for r in caplog.records), (
        "取消没有留痕 —— 原实现一行日志都不打，事后只能靠 access log 反推"
    )
    assert any("t-run" in r.getMessage() for r in caplog.records), (
        "日志里必须有 task_id，否则不知道停的是哪个任务"
    )


def test_cancel_never_overwrites_a_completed_task(caplog) -> None:
    """★ 取消晚于完成：**保留完成态**，并明确记一条"晚了一步"。

    这是"点了停止、结果还是出来了"最可能的形状：报告已经生成、甚至已经进
    结果缓存，而取消把它改写成 `cancelled` —— 用户看到的就是
    「状态说已取消，可结果是完整的」。
    """
    store = TaskStore()
    store.create("t-done", **_REQUIRED)
    store.update("t-done", status="completed", final_report="完整报告")

    with caplog.at_level("WARNING"):
        ok = asyncio.run(store.cancel_task("t-done"))

    record = store.get("t-done")
    assert ok is True
    assert record is not None
    assert record.status == "completed", (
        f"取消覆盖了已完成状态（现在是 {record.status}）⇒ "
        "会出现『状态已取消、报告已进缓存』这种自相矛盾"
    )
    assert record.final_report == "完整报告", "报告不该被取消动过"
    assert any("取消晚了一步" in r.getMessage() for r in caplog.records), (
        "晚到的取消必须留下痕迹，否则运维无法解释『我停了它却出了结果』"
    )


def test_cancel_still_marks_a_pending_task_cancelled() -> None:
    """反向：还没跑完的任务，取消仍然必须把它置成 cancelled（别修过头）。"""
    store = TaskStore()
    store.create("t-pending", **_REQUIRED)
    asyncio.run(store.cancel_task("t-pending"))
    record = store.get("t-pending")
    assert record is not None and record.status == "cancelled"
