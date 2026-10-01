"""判据：**启动段不许被重活挡住**（就绪 = 能应答，不是"端口在监听"）（`CHG-0147`）。

## 现场（2026-09-30 实测）

`lifespan` 里两段重活被 `await` 着：

    await cat.rebuild_all()          # 指标索引重建：日志里 16 秒的间隔就是它
    await asset_cat.scan_and_store() # 数据资产扫描

按日志时间线（17:03:12.4 → 17:03:28.5 → 17:03:32.6）：

    17:03:12.4  catalog 作业已注册
    17:03:28.5  频率推断：修正 1038 个自动登记指标     ← 中间 **16 秒**
    17:03:29.3  资产扫描…

后果不是"慢"，是**整站不可用**：uvicorn 先绑端口再跑 lifespan ⇒
**端口在监听、请求却排在启动完成之后** ⇒ 前端 4 秒探针必然超时
⇒ 用户看到「后端服务当前不可达」（而这与"进程死了"在界面上**长得一样**）。

★ 更值得记的是：`loop_lag` **永远看不见这一段** —— 监控是在它之后才启动的
（`[循环延迟] 监控已启动` 出现在 17:04:04）。所以"卡顿表里没有它"≠"它不卡"，
这也是 B 类（启动/后台）必须单独立判据的原因。

## 两条判据各自守什么

1. **AST**：`lifespan` 里不许 `await` `rebuild_all` / `scan_and_store`，
   且必须有把它们交给后台任务的 `create_task`。**按语法树判**，不看文本 ——
   lifespan 里那段注释**逐字写着**这两个名字（解释为什么挪走），
   文本判据会被自己的说明命中而假红。
2. **行为**：把 `rebuild_all` 换成"睡 2.5 秒"的替身，断言**首个请求 2 秒内返回**
   （证明启动没等它），并且它**确实被执行了**（证明不是被静默丢掉）。
   只测前者会奖励"直接删掉这一步"，只测后者会奖励"改回 await"。
"""
from __future__ import annotations

import ast
import asyncio
import pathlib
import time

MAIN = pathlib.Path(__file__).resolve().parents[2] / "src" / "api" / "main.py"

#: 必须"不挡就绪"的两步（`CHG-0147`）
DEFERRED_CALLS = ("rebuild_all", "scan_and_store")
#: 承载它们的后台任务（名字变了要同步改这里）
BACKGROUND_TASK = "_rebuild_catalog_and_assets_later"


def _lifespan_tree() -> ast.AsyncFunctionDef:
    tree = ast.parse(MAIN.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "lifespan":
            return node
    raise AssertionError("`src/api/main.py` 里找不到 `lifespan` —— 判据失去目标")


def test_lifespan_does_not_await_the_heavy_index_work() -> None:
    """★ `lifespan` 里**不许** `await` 指标索引重建 / 资产扫描。"""
    node = _lifespan_tree()
    awaited_heavy: list[str] = []
    for child in ast.walk(node):
        if not isinstance(child, ast.Await):
            continue
        call = child.value
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute) \
                and call.func.attr in DEFERRED_CALLS:
            awaited_heavy.append(f"{call.func.attr}@line{call.lineno}")
    assert not awaited_heavy, (
        "启动段又在等这两步了 ⇒ **端口在监听、请求却排在启动完成之后**，"
        "重启期间前端探针必然超时（实测 16~25 秒不可用）："
        + "、".join(awaited_heavy)
        + f"；正确做法：`asyncio.create_task({BACKGROUND_TASK}(settings), …)`")


def test_the_work_is_handed_to_a_named_background_task() -> None:
    """★ 而且要**确实交出去了**（不是删掉了）：必须有一个具名后台任务承载它。

    ⚠️ 形态允两种：`_bg_run("<名字>", <coro>)`（现行，同时登记进在飞簿）
    或裸 `asyncio.create_task(<coro>, name=...)`（曾经的写法）。
    判据盯的是"**有没有交出去**"，不是"用哪种写法"——
    写死一种写法会让"换了个更安全的封装"变成假红。
    """
    node = _lifespan_tree()
    scheduled: list[str] = []
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        func = child.func
        is_create = isinstance(func, ast.Attribute) and func.attr == "create_task"
        is_bg_run = isinstance(func, ast.Name) and func.id == "_bg_run"
        if not (is_create or is_bg_run):
            continue
        for sub in ast.walk(child):
            if isinstance(sub, ast.Name) and sub.id == BACKGROUND_TASK:
                scheduled.append(f"line{child.lineno}")
    assert scheduled, (
        f"`lifespan` 里没有把重活交给 `{BACKGROUND_TASK}` —— 要么被删了"
        "（索引会永远陈旧 ⇒ SmartFetcher 每次都多联网），要么又改回 `await`")

    # 承载函数必须真的存在
    tree = ast.parse(MAIN.read_text(encoding="utf-8"))
    names = {n.name for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert BACKGROUND_TASK in names, f"找不到 {BACKGROUND_TASK} 的定义"


def test_app_serves_before_the_index_rebuild_finishes(monkeypatch) -> None:
    """★★ **行为**判据：把重建换成"睡 2.5 秒"，首个请求必须 2 秒内返回。

    这是本判据的**真身**：AST 判据只证明"没有 await"，行为判据证明
    "服务真的能先应答"。两者缺一：只做 AST，将来有人把重活搬进别的
    被 await 的启动步骤（比如 runtime 装配里）就没人拦。

    同时断言它**确实被执行**（`called["n"] >= 1`）—— 否则"直接删掉重建"
    也能让这条判据变绿，而那是拿"索引永远陈旧、每次多联网 13 秒"换来的。
    """
    from fastapi.testclient import TestClient

    from src.infrastructure.catalog.catalog_repo import CatalogRepository

    called = {"n": 0}

    async def _slow_rebuild(self, **_kw):  # noqa: ANN001
        called["n"] += 1
        await asyncio.sleep(2.5)
        return {"metas_synced": 1, "indicators_backfilled": 1}

    monkeypatch.setattr(CatalogRepository, "rebuild_all", _slow_rebuild)

    from src.api.main import app

    t0 = time.perf_counter()
    with TestClient(app) as client:
        ready_s = time.perf_counter() - t0
        resp = client.get("/api/v1/health/live")
    assert resp.status_code == 200, resp.text
    assert ready_s < 2.0, (
        f"首个请求等了 {ready_s:.1f} 秒才返回（替身的重建要 2.5 秒）⇒ "
        "启动仍在等重活；重启期间整站不可用，前端 4 秒探针必然超时")
    assert called["n"] >= 1, (
        "重建一次都没执行 ⇒ 不是「挪到后台」，而是被删了（索引会永远陈旧）")


# ─────────────── 第二半：后台任务必须**有名**（否则卡顿点名不了）───────────────


def test_every_lifespan_background_task_is_named_and_registered() -> None:
    """★ `lifespan` 里**不许**出现裸 `asyncio.create_task`（必须走 `_bg_run`）。

    ## 为什么（2026-09-30 实测）

    把索引重建挪到后台后，就绪只要 0.6 秒了，但紧接着仍有 **1.2 s / 3.6 s** 的
    循环卡顿，而卡顿行印的是：

        [循环延迟] 本次 3578ms（…）—— 卡顿时刻在飞：（无在飞请求）

    对**请求路径**而言那句话是对的，但它读起来像"没人干活"——
    真相是**后台任务在干**，只是它们**没登记** ⇒ 仪器**点名不了**。
    于是"修完一个已知的"之后，剩下的仍然只能靠猜 —— 这条判据就是为拦住这个：
    **每个后台任务都要在登记簿里有名字**，下一刀才有靶子。

    `_bg_run` 同时解决两件事：起任务 + 登记 `task:<名字>`（结束自动注销）。
    """
    node = _lifespan_tree()
    raw_calls: list[int] = []
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        func = child.func
        if isinstance(func, ast.Attribute) and func.attr == "create_task":
            raw_calls.append(child.lineno)
    assert not raw_calls, (
        "`lifespan` 里还有裸 `asyncio.create_task`（行 "
        + "、".join(str(x) for x in raw_calls)
        + "）⇒ 该后台任务不会登记进在飞簿，卡顿时刻点不出它的名字。"
        "改法：`_bg_run(\"<名字>\", <coro>)`。")

    tree = ast.parse(MAIN.read_text(encoding="utf-8"))
    names = {n.name for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert "_bg_run" in names, "`_bg_run` 不见了 —— 判据失去目标"


async def test_bg_run_registers_while_running_then_unregisters() -> None:
    """★ 行为判据：`_bg_run` 起的任务在**跑的时候**登记、跑完注销。

    只断言"调用了 `_bg_run`"是形状判据 —— 而本节已经栽过一次形状判据
    （在飞依赖挂在 `api_router.dependencies` 上却不生效）。所以这里直接跑它。
    """
    from src.api.main import _bg_run
    from src.core import inflight

    seen: list[str] = []

    async def _work() -> None:
        await asyncio.sleep(0.05)
        seen.extend(r["label"] for r in inflight.snapshot())

    inflight.reset()
    task = _bg_run("unit-test-bg", _work())
    await task
    assert any("unit-test-bg" in s for s in seen), (
        f"后台任务运行期间登记簿里看不到它 ⇒ 卡顿点名不了：{seen}")
    assert inflight.inflight_count() == 0, "任务结束后没有注销 ⇒ 登记簿只增不减"
