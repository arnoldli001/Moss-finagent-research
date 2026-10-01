"""共享行情仓的写权限归属（`CHG-0087` / PRD §16.11）—— 五条**防复发判据**。

## 触发它的用户原话（2026-09-29）

> 「共享行情仓，dev 读，pilot 写和读。同时看下更新数据是谁负责的，
>   谁负责更新数据谁有写权限。」

## 事故背景（这五条各钉住一个真实失效）

三个实例的调度器**都在**跑 `quant_data_sync`：`data/dev/scheduler` 33 条
（最后 23:30:03）、`data/pilot/scheduler` 43 条（最后 23:30:02）、
`data/scheduler` 7 条 —— dev 与 pilot 在**同一分钟**往同一个 14.36 GiB 的
SQLite 文件里 upsert，而且各自以为自己是唯一写者。

而"它本该被关掉"这件事**只写在两处注释里**：`manage.py` 里那个
`MOSS_SCHEDULER_ENABLED=0`（全仓库**零处读取**，`main.py` 无条件
`CronScheduler.start()`），以及一段声称"定时任务已关闭"的启动横幅。

| 判据 | 失效长什么样（都不会报错） |
|---|---|
| ① 写者点名到**具体环境** | `writer: main` 这类模糊写法 → 没人知道该谁写 |
| ② 作业声明的更新目标**必须登记过** | 名字写错一个字母 → 裁剪静默失效、作业照跑 |
| ③ 非写者实例上更新作业**不触发** | 两个实例同写一个文件（本事故） |
| ④ 写闸门对非写者 **fail-closed** | 只有纪律没有闸门 → 下一个人又来写一次 |
| ⑤ **待裁定**的口径不拿来关作业 | 没人拍过板的策略静默停掉生产任务（假红灯） |

判据全部**从 registry 派生**，不硬编码"pilot"或"quant_data_sync"这种具体值
—— 否则归属一改，测试就变成在测上一版口径。
"""
from __future__ import annotations

import ast
import asyncio
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from src.infrastructure.catalog import data_stores as ds
from src.scheduler import registry as reg
from src.scheduler.registry import JOB_REGISTRY, JobSpec
from src.scheduler.service import CronScheduler

#: 事故里那个"更新行情仓"的作业（从 `updates` 派生，不写死名字）
WAREHOUSE_JOBS = tuple(
    name for name, spec in JOB_REGISTRY.items() if "warehouse" in spec.updates)


def _writer_env() -> str:
    """行情仓登记的写者环境（本测试的全部环境断言都从它推）。"""
    return ds.get_store("warehouse").writer


def _other_env() -> str:
    """一个**不是**写者的隔离环境（用来验证"只读"那一侧）。"""
    return next(e for e in ds.ISOLATED_ENVS if e != _writer_env())


# ======================================================================
# 判据 ① 写者点名到具体环境
# ======================================================================

def test_warehouse_has_a_concrete_writer_env() -> None:
    """`warehouse.writer` 必须是一个**具体环境名**，不能是 `main` 这种模糊说法。

    模糊写法的失效是"没人知道自己该不该写"：`writer: main` 在
    `writable_here()` 里对隔离档只能给出 `decided=False`（"口径待裁定"）,
    于是它**只报告不阻断** —— 而 A7 那条审计项就这么挂了很久。
    """
    store = ds.get_store("warehouse")
    assert store.writer in ds.ISOLATED_ENVS, (
        f"warehouse.writer={store.writer!r} 不是具体环境名；"
        "共享行情仓的写者必须点名到承担更新责任的那个环境")
    assert store.writable is True, "写者自己都写不了，说明声明自相矛盾"


def test_only_the_declared_writer_may_write(monkeypatch) -> None:
    """写者环境可写、**其余每个隔离环境都不可写且已裁定**（两侧都要钉）。"""
    writer = _writer_env()
    monkeypatch.setenv("MOSS_ENV", writer)
    allowed = ds.writable_here("warehouse")
    assert allowed.allowed is True and allowed.decided is True, allowed.reason

    for env in ds.ISOLATED_ENVS:
        if env == writer:
            continue
        monkeypatch.setenv("MOSS_ENV", env)
        denied = ds.writable_here("warehouse")
        assert denied.allowed is False, f"{env} 不该能写行情仓"
        assert denied.decided is True, (
            f"{env} 的拒绝必须**已裁定**（否则只是提示，拦不住写）")
        assert writer in denied.reason, "理由里要点名谁是写者"


# ======================================================================
# 判据 ② 作业声明的更新目标必须登记过 · 且**写共享仓的作业必须声明**
# ======================================================================

def _write_capable_names(module_path: Path) -> set[str]:
    """从模块**源码**推出"哪些函数会写库"（不维护手写清单）。

    判据：函数体里出现 `engine().begin()`（开写事务）、`INSERT INTO`、
    `ingest_dataset(`、`ensure_table(` 之一。
    `query_only` 那道连接级闸门让"写"必然表现为其中之一，所以这个推导
    会**自己长大** —— 新增一个写函数，它的名字自动进集合。
    """
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = ast.dump(node)
        if any(marker in body for marker in
               ("engine().begin", "INSERT INTO", "ingest_dataset",
                "ensure_table")):
            names.add(node.name)
    return names


def test_jobs_writing_shared_warehouse_declare_updates() -> None:
    """★ **写共享行情仓的作业必须声明 `updates=("warehouse",)`**。

    这条防的是我自己修过的那类漏：一开始只给 `quant_data_sync` 声明了
    `updates`，而 `strategy_cases_weekly/daily` 也在往同一个仓库里写
    （`strategy_case_store(...).upsert()` → `quant_strategy_case` 表）。
    漏掉的后果**不是报错**，而是那两个作业在非写者实例上每班撞一次写闸门，
    把运行台账刷成一片"失败"—— 运维看到的是故障，实际是"这台机器不负责写"。

    判据是**派生的**：写能力函数名从 `warehouse.py` / `stock_directory.py`
    的源码推出来（见 `_write_capable_names`），不维护"哪些作业会写"的手写清单。
    """
    root = Path(__file__).resolve().parents[2]
    writers = (_write_capable_names(root / "src" / "quant" / "warehouse.py")
               | _write_capable_names(root / "src" / "quant" / "stock_directory.py"))
    assert {"ingest_dataset", "upsert"} <= writers, (
        f"推导失效（连 ingest_dataset/upsert 都没认出来）：{sorted(writers)}")
    assert "load" not in writers, (
        "推导把只读函数也算成写了 —— 判据必须先自证不误伤")

    jobs_src = (root / "src" / "scheduler" / "jobs.py").read_text(encoding="utf-8")
    called = _called_names(jobs_src)
    offenders = []
    for name, spec in JOB_REGISTRY.items():
        executor = f"_{spec.kind}"
        if executor not in called["functions"]:
            continue          # 该 kind 的执行体不在 jobs.py（例如走工厂）
        touched = called["by_function"].get(executor, set()) & writers
        if touched and "warehouse" not in spec.updates:
            offenders.append((name, sorted(touched)))
    assert not offenders, (
        "这些作业在执行体里调用了会写库的函数，却没有声明 updates=('warehouse',)："
        f"{offenders}。要么补声明，要么说明为什么它不写共享仓 —— "
        "**不要**把这条判据改成豁免名单。")
    # 反向：至少有一个作业真的被判为写共享仓（否则判据可能整体空转）
    assert WAREHOUSE_JOBS, "没有任何作业声明会写 warehouse，判据空转"


def _called_names(source: str) -> dict[str, Any]:
    """按**函数**归集它体内出现的调用名（只做一层，不开闭包）。"""
    tree = ast.parse(source)
    functions: set[str] = set()
    by_function: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        functions.add(node.name)
        found: set[str] = set()
        for inner in ast.walk(node):
            if isinstance(inner, ast.Call):
                target = inner.func
                if isinstance(target, ast.Name):
                    found.add(target.id)
                elif isinstance(target, ast.Attribute):
                    found.add(target.attr)
        by_function[node.name] = found
    return {"functions": functions, "by_function": by_function}


def _modules_calling(source_root: Path, callee: str) -> dict[Path, set[str]]:
    """哪些模块**用到了** `callee`（按 AST，不按正则）。

    判据是"调用 **或** 导入"两种，缺一不可：`scheduler/service.py` 里写的是

        fn = self._execute or execute_job      # 导入后赋值
        return await fn(self._runtime, ...)    # 调用的是 `fn`

    只认 `callee(...)` 形式的调用会**漏掉它**（第一版就这么漏了，
    自证断言当场报"已知路径没被认出来" —— 这正是自证存在的意义）。
    """
    hits: dict[Path, set[str]] = {}
    for path in sorted(source_root.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        found: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                target = node.func
                if isinstance(target, ast.Name) and target.id == callee:
                    found.add(target.id)
                elif isinstance(target, ast.Attribute) and target.attr == callee:
                    found.add(target.attr)
            elif isinstance(node, ast.ImportFrom) and node.module:
                if node.module.endswith("scheduler.jobs"):
                    for alias in node.names:
                        if alias.name == callee:
                            found.add(f"import:{alias.name}")
        if found:
            hits[path] = found
    return hits


def test_every_trigger_path_checks_write_ownership() -> None:
    """★ **所有**能拉起作业的地方都必须过同一道写权限判据。

    本条的由来：`_tick()` 裁掉 `quant_data_sync` 之后，仓库里**还有三条**
    能把它拉起来的路径 —— `main.py` 启动自检的 `scheduler.trigger(...)`、
    管理员端点 `POST /scheduler/jobs/{name}/run`、Celery Worker 的
    `dispatch_job`。任何一条不过判据，"按写权限裁掉更新作业"都能被绕过去：
    作业照跑 → 撞写闸门 → 落一条 `failed`（看起来是故障，实际是
    "这台机器不负责写"）；而 `quant_data_sync` 还会**先白下一遍分区**。

    判据是**派生**的：全仓扫 `execute_job(` 的调用点，凡调它的模块就**必须**
    也调用 `job_deny_reason` / `schedulable_jobs`。新增第五条路径会被自动纳入。
    """
    root = Path(__file__).resolve().parents[2] / "src"
    callers = _modules_calling(root, "execute_job")
    # `jobs.py` 是 `execute_job` 的**定义处**（不是触发路径），排除
    callers.pop(root / "scheduler" / "jobs.py", None)
    assert callers, "没有找到任何 execute_job 调用点 —— 判据推导失效"
    gate_calls = set(_modules_calling(root, "job_deny_reason")) | \
        set(_modules_calling(root, "schedulable_jobs"))
    ungated = [p.relative_to(root).as_posix() for p in callers if p not in gate_calls]
    assert not ungated, (
        f"这些模块能拉起作业，却不过写权限判据（新增路径必须一起接上）：{ungated}")
    # 自证：判据不是恒真 —— 至少要认出已知的三条路径（用 `as_posix`，
    # 否则 Windows 的反斜杠会让断言自己错，本项目已为这类"探针自己错"付过代价）
    known = {p.relative_to(root).as_posix() for p in callers}
    expected = {"scheduler/service.py", "scheduler/celery_app.py",
                "api/routes/scheduler.py"}
    assert expected <= known, f"已知路径没被认出来：缺 {expected - known}；实得 {known}"


def test_called_names_judge_is_self_proving() -> None:
    """**判据自证**：喂一段已知答案的源码，确认它认得出写、且不误伤读。"""
    sample = (
        "def _writes():\n"
        "    store = strategy_case_store()\n"
        "    return store.upsert(cases)\n"
        "\n"
        "def _reads():\n"
        "    return warehouse.load('daily')\n"
    )
    parsed = _called_names(sample)
    assert parsed["by_function"]["_writes"] == {"strategy_case_store", "upsert"}
    assert parsed["by_function"]["_reads"] == {"load"}


def test_update_jobs_only_declare_registered_stores() -> None:
    """`JobSpec.updates` 里的每个名字都必须能在 registry 里解析到。

    这条钉住的是"写错一个字母"：`updates=("warehosue",)` 的后果**不是报错**，
    而是 `writable_here()` 抛 `StoreNotFound` → 裁剪逻辑吞掉 → 作业**照跑**。
    于是"已经声明了写权限归属"变成一句空话，而且没有任何迹象。
    """
    declared = {name: spec.updates for name, spec in JOB_REGISTRY.items()
                if spec.updates}
    assert declared, "没有任何作业声明 updates —— 裁剪机制等于没接（见 CHG-0087）"
    for job, stores in declared.items():
        for store in stores:
            ds.get_store(store)  # 未登记会抛 StoreNotFound
        assert isinstance(stores, tuple), f"{job}.updates 必须是 tuple（frozen）"


def test_stock_directory_cache_fill_is_best_effort(monkeypatch) -> None:
    """读路径上的"顺手缓存"写不进去，**不许**让这次查询失败（`CHG-0087`）。

    共享行情仓只有一个写者（`warehouse.writer`），其余实例连接级只读
    ⇒ `StockDirectory.enrich()` 里的落库**必然被拒**。
    但被拒的是"把补录结果缓存到共享库"，不是这次查询：名字已经从东财取到了。
    让读操作因为缓存写不进去而 500，故障方向就完全错了。

    反向也要钉住：**显式**重建（`build_from_stock_basic`）是"我就是要写"，
    必须把异常抛出去 —— 两边都测，否则"尽力而为"会退化成"静默吞掉一切"。
    """
    from src.quant.stock_directory import StockDirectory, StockEntry

    directory = StockDirectory.__new__(StockDirectory)

    def _boom(_entries):  # noqa: ANN001
        raise RuntimeError("attempt to write a readonly database")

    monkeypatch.setattr(directory, "upsert", _boom)
    # 不抛 = 本次查询照常返回（这就是要钉的行为）
    directory._cache_entries([StockEntry(code="600036", name="招商银行")])

    # 反向：显式写入路径不吞异常（`upsert` 本身没被改）
    monkeypatch.undo()
    real = StockDirectory.__new__(StockDirectory)
    assert real.upsert([]) == 0, "空输入应当直接返回 0，而不是去连库"


# ======================================================================
# 判据 ③ 非写者实例上更新作业不触发（真跑一遍 `_tick`）
# ======================================================================

def test_warehouse_update_job_is_pruned_on_non_writer(monkeypatch) -> None:
    """非写者实例：更新作业被裁掉，且理由是**人话**（不是枚举值）。"""
    assert WAREHOUSE_JOBS, "没有作业声明会写 warehouse，本判据无从谈起"
    monkeypatch.setenv("MOSS_ENV", _other_env())
    for job in WAREHOUSE_JOBS:
        reason = reg.job_deny_reason(job)
        assert reason, f"{job} 在非写者实例上没有被裁"
        assert _writer_env() in reason, "理由里要点名真正的写者"
        assert job not in reg.schedulable_jobs()
    assert reg.scheduler_scope_report()["active"] < len(JOB_REGISTRY)


def test_warehouse_update_job_runs_on_writer(monkeypatch) -> None:
    """写者实例：**不**裁（否则行情永远不会更新，这才是更贵的故障）。"""
    monkeypatch.setenv("MOSS_ENV", _writer_env())
    for job in WAREHOUSE_JOBS:
        assert reg.job_deny_reason(job) == ""
        assert job in reg.schedulable_jobs()


def _first_due(cron: str, start: datetime) -> datetime:
    """从 `start` 起找出**第一个到期时刻**（判据不写死 cron，改 cron 不会弄红它）。

    第一版这里写死了 `spec.cron == "*/30 16-23 * * 1-5"` 与"周一 16:30" ——
    而 `WAREHOUSE_JOBS[0]` 随着我给 `strategy_cases_*` 补上 `updates` 变成了
    `10 8 * * 1`，测试当场红了。**判据不该跟具体 cron 耦合**：到期时刻应当
    由 cron 自己算出来（`cron_due` 是生产用的同一个函数）。
    """
    from src.scheduler.service import cron_due

    for offset in range(60 * 24 * 8):
        moment = start + timedelta(minutes=offset)
        if cron_due(cron, moment):
            return moment
    raise AssertionError(f"cron {cron!r} 在 8 天内从未到期，判据前提不成立")


@pytest.mark.asyncio
async def test_tick_does_not_fire_pruned_job_on_non_writer(monkeypatch) -> None:
    """**真的跑一遍 `_tick`**：被裁的作业不会被触发。

    判据放在"有没有人调它"这道门上 —— 本项目最贵的三个缺陷都卡在这一道：
    判据写得再漂亮，没人调用就什么也没发生。所以这里不看 `job_deny_reason`
    的返回值，而是看**执行器有没有被调用**。
    """
    job = "quant_data_sync"           # 明确的更新作业（行情仓的第一写者）
    spec = JOB_REGISTRY[job]
    assert "warehouse" in spec.updates, f"{job} 必须声明它会写仓库"
    now = _first_due(spec.cron, datetime(2026, 9, 14, 0, 0))  # 周一 00:00 起扫

    async def run_once(env: str) -> list[str]:
        monkeypatch.setenv("MOSS_ENV", env)
        fired: list[str] = []

        async def fake_execute(runtime, name, run_log, trigger):  # noqa: ANN001
            fired.append(name)
            return {"status": "success", "records_processed": 0}

        sched = CronScheduler(object(), _FakeRunLog(), execute_fn=fake_execute,
                              now_fn=lambda: now)
        await sched._tick()
        for _ in range(5):
            await asyncio.sleep(0)
        return fired

    assert job not in await run_once(_other_env()), (
        f"{_other_env()} 实例不该触发 {job}（它没有行情仓的写权限）")
    assert job in await run_once(_writer_env()), (
        f"{_writer_env()} 是写者，{job} 必须照跑")


# ======================================================================
# 判据 ④ 写闸门 fail-closed（真连库、真写一次，但写在临时库上）
# ======================================================================

@pytest.fixture()
def fake_registered_warehouse(tmp_path, monkeypatch):
    """把 registry 里那条 `warehouse` 的路径**指到临时库**。

    为什么不直接对真实行情仓测：那正是"探针不许有把生产弄坏的能力"
    （本会话我真踩过一次：为证明闸门是开的，往 14.36 GiB 生产库写了一行假数据）。
    这里让 `_registered_store()` 认下临时库，于是走的是**同一段生产代码**，
    而最坏后果只是临时目录里多一行。
    """
    real = tmp_path / "warehouse.db"
    conn = sqlite3.connect(real)
    conn.execute("CREATE TABLE quant_daily (trade_date TEXT, code TEXT, "
                 "close FLOAT, PRIMARY KEY (trade_date, code))")
    conn.execute("INSERT INTO quant_daily VALUES ('20260925', '600036', 1.0)")
    conn.commit()
    conn.close()
    monkeypatch.setattr(ds, "store_path", lambda name: str(real))
    return real


def test_write_gate_rejects_non_writer_with_human_reason(
        fake_registered_warehouse, monkeypatch) -> None:
    """非写者：`upsert()` 抛错，理由含"谁是写者 + 怎么改"，**读还读得到**。"""
    import pandas as pd

    from src.quant.warehouse import QuantWarehouse, WarehouseError

    monkeypatch.setenv("MOSS_ENV", _other_env())
    wh = QuantWarehouse(root=fake_registered_warehouse.parent / "tushare")
    # 前提自证：这条实例确实被认成"登记的那条 warehouse"，否则下面的断言是空转
    assert wh._registered_store() == "warehouse"  # noqa: SLF001

    with pytest.raises(WarehouseError) as excinfo:
        wh.upsert("daily", pd.DataFrame(
            {"trade_date": ["20260926"], "code": ["000001"], "close": [2.0]}))
    message = str(excinfo.value)
    assert _writer_env() in message and "只读" in message, message
    assert "MOSS_ENV" in message, "要给出可照抄的命令，不能只说'你没权限'"

    # ★ 反向：读必须照常（只读实例不能被误伤 —— 那会把取数整条链路打死）
    rows = wh.load("daily")
    assert len(rows) == 1, "只读实例读不到数据"

    # ★ SQLite 层的结构性兜底：绕过 `upsert()` 直接写也进不去
    from sqlalchemy import text

    with pytest.raises(Exception) as excinfo2:
        with wh.engine().begin() as conn:
            conn.execute(text("INSERT INTO quant_daily VALUES "
                              "('20260926','000002',3.0)"))
    assert "readonly" in str(excinfo2.value).lower(), str(excinfo2.value)


def test_write_gate_allows_declared_writer(fake_registered_warehouse,
                                           monkeypatch) -> None:
    """写者：`upsert()` 真的写得进去（只钉"拦住"会在写侧坏掉时照样绿）。"""
    import pandas as pd

    from src.quant.warehouse import QuantWarehouse

    monkeypatch.setenv("MOSS_ENV", _writer_env())
    wh = QuantWarehouse(root=fake_registered_warehouse.parent / "tushare")
    assert wh._registered_store() == "warehouse"  # noqa: SLF001
    written = wh.upsert("daily", pd.DataFrame(
        {"trade_date": ["20260926"], "code": ["000001"], "close": [2.0]}))
    assert written == 1
    assert len(wh.load("daily")) == 2


def test_custom_root_warehouse_is_not_gated(tmp_path, monkeypatch) -> None:
    """自定义 `root` 的仓库**不套用**生产写权限（否则测试/回测全被误伤）。"""
    from src.quant.warehouse import QuantWarehouse

    monkeypatch.setenv("MOSS_ENV", _other_env())
    wh = QuantWarehouse(root=tmp_path / "elsewhere" / "tushare")
    assert wh._registered_store() == ""  # noqa: SLF001
    assert wh.writable_here() is True
    wh.assert_writable()  # 不该抛


# ======================================================================
# 判据 ⑤ 待裁定的口径不拿来关作业
# ======================================================================

def test_undecided_denial_does_not_prune_jobs(monkeypatch) -> None:
    """`decided=False`（口径待裁定）**不能**拿来停作业 —— 只能照跑 + 如实展示。

    这条防的是"假红灯"：让一个没人拍过板的口径静默停掉生产任务，
    与"设了但不生效的开关"是同一个错误的两个方向。
    """
    monkeypatch.setenv("MOSS_ENV", _other_env())
    pendings = [s.name for s in ds.all_stores()
                if s.kind == "sqlite" and s.writable and s.writer == "main"]
    if not pendings:
        pytest.skip("当前没有『待裁定』的共享存储，判据前提不成立")
    store = pendings[0]
    decision = ds.writable_here(store)
    assert decision.allowed is False and decision.decided is False, (
        f"{store} 已不再是『待裁定』状态（{decision.reason}）—— "
        "本判据的前提变了，要按新口径重写，而不是让它静默变成空转")

    job = "_test_undecided_updates"
    monkeypatch.setitem(JOB_REGISTRY, job, JobSpec(
        name=job, cron="0 3 * * *", kind="graph_snapshot",
        description="测试用：只写一条待裁定的共享存储", params={},
        updates=(store,)))
    try:
        assert reg.job_deny_reason(job) == "", (
            "口径未裁定就不该关作业 —— 那等于让没人拍板的策略停掉生产任务")
        assert job in reg.schedulable_jobs()
    finally:
        JOB_REGISTRY.pop(job, None)


# ======================================================================
# `MOSS_SCHEDULER_DENY`：那个"只被注释提到"的开关现在必须真的有效
# ======================================================================

def test_scheduler_deny_env_really_denies(monkeypatch) -> None:
    """手工禁用要生效，**并且**拼错的名字要单独报出来。

    只钉"禁用生效"会漏掉另一半失效：名字写错时效果是"什么都没发生"，
    与"本来就不需要禁"在日志里长得一模一样 —— 那正是"设了但不生效的开关"
    的复发形态（`MOSS_SCHEDULER_ENABLED` 就是这么骗了很久的）。
    """
    target = WAREHOUSE_JOBS[0] if WAREHOUSE_JOBS else next(iter(JOB_REGISTRY))
    typo = "_no_such_job_name"
    monkeypatch.setenv(reg.SCHEDULER_DENY_ENV, f" {target} , {typo} ")
    assert reg.job_deny_reason(target) != ""
    assert target not in reg.schedulable_jobs()
    assert reg.unknown_denied_names() == (typo,), (
        "拼错的名字必须被单独列出来（不能静默无效）")
    assert target not in reg.unknown_denied_names()


@pytest.mark.asyncio
async def test_manual_trigger_also_respects_write_ownership(monkeypatch) -> None:
    """**第二条触发路径也要过同一道关**：`trigger()`（启动自检补偿 / 盘中补扫）。

    为什么必须单独钉：`_tick()` 已按写权限裁剪，而 `trigger()` 是**不在
    `_tick()` 里**的另一条路（`src/api/main.py` 的启动自检发现"行情有缺口"时会
    直接 `scheduler.trigger("quant_data_sync")`）。它若不过同一道判据，
    就会在只读实例上把更新作业拉起来撞写闸门、记一条 `failed` ——
    运维看到的是故障，实际是"这台机器不负责写"。
    """
    from src.scheduler.registry import schedulable_jobs

    job = "quant_data_sync"
    assert job not in schedulable_jobs() or _other_env() == _writer_env()

    async def fake_execute(runtime, name, run_log, trigger):  # noqa: ANN001
        raise AssertionError("被裁的作业不该真的被执行")

    monkeypatch.setenv("MOSS_ENV", _other_env())
    sched = CronScheduler(object(), _FakeRunLog(), execute_fn=fake_execute)
    record = await sched.trigger(job, source="startup")
    assert record["status"] == "skipped", record
    assert record["records_processed"] == 0
    assert record["reason"].strip(), "跳过必须给理由（否则与'静默没跑'无法区分）"
    assert _writer_env() in record["reason"], "理由里要点名真正的写者"

    # 反向：写者实例上 `trigger()` 照常执行（只钉"拦住"会连写者也一起拦掉）
    seen: list[str] = []

    async def ok_execute(runtime, name, run_log, trigger):  # noqa: ANN001
        seen.append(name)
        return {"status": "success", "records_processed": 1}

    monkeypatch.setenv("MOSS_ENV", _writer_env())
    sched2 = CronScheduler(object(), _FakeRunLog(), execute_fn=ok_execute)
    record2 = await sched2.trigger(job, source="startup")
    assert record2["status"] == "success" and seen == [job]


def test_scope_report_is_self_consistent(monkeypatch) -> None:
    """`active + pruned == total`，且 `active` 与 `schedulable_jobs()` 同源。"""
    monkeypatch.delenv(reg.SCHEDULER_DENY_ENV, raising=False)
    report = reg.scheduler_scope_report()
    assert report["active"] + len(report["pruned"]) == report["total"]
    assert report["active"] == len(reg.schedulable_jobs())
    assert report["total"] == len(JOB_REGISTRY)
    assert report["env"] == ds.current_env()


class _FakeRunLog:
    def __init__(self, paused: set[str] | None = None):
        self.paused = paused or set()

    def is_paused(self, name: str) -> bool:
        return name in self.paused


# ======================================================================
# `/health` 契约：写权限归属必须**看得见**（用户原话「现在就接」）
# ======================================================================

@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("MOSS_SQLITE_PATH", str(tmp_path / "health.db"))
    monkeypatch.setenv("LLM_AUDIT_DIR", str(tmp_path / "audit"))
    monkeypatch.setenv("MOSS_ENV", "test")
    from fastapi.testclient import TestClient

    from src.api.main import app

    with TestClient(app) as c:
        yield c


def test_health_exposes_store_ownership_contract(client) -> None:
    """`data_sources.local_stores` 必须给出前端/运维渲染所需的字段与类型。

    为什么要有这条契约：`describe()` 曾经**零个生产调用方**，
    于是"dev 与 pilot 同时写同一个 14 GiB 文件"只能靠翻调度日志比对时间戳
    发现。字段名两边各改各的时**不会报错**，只会显示错。
    """
    resp = client.get("/api/v1/health")
    assert resp.status_code == 200, resp.text
    section = resp.json().get("data_sources", {}).get("local_stores")
    assert isinstance(section, dict), (
        "data_sources.local_stores 缺失 —— 写权限归属又变成不可见的了")
    assert section.get("available") is True, section
    for key in ("env", "env_root", "registry", "count", "protected",
                "sizes_measured", "sqlite", "dirs"):
        assert key in section, f"缺字段 {key}"
    assert section["count"] == len(ds.all_stores())
    assert section["sizes_measured"] is False, (
        "`/health` 必须走 want_sizes=False（递归目录实测 3,288.9 ms vs 11.0 ms）")

    # ★ 「没量到」≠「量到 0」：未量体积时必须是 null
    for item in section["sqlite"]:
        assert item["size_mb"] is None, (
            f"{item['name']} 的体积未量到，必须是 null —— 填 0 会被读成『空库』")
        assert "write" in item, f"{item['name']} 缺写权限裁决"
        assert isinstance(item["write"]["write_allowed"], bool)
        assert isinstance(item["write"]["decided"], bool)
        assert item["write"]["reason"].strip(), "写权限裁决必须带人话理由"

    names = {item["name"] for item in section["sqlite"]}
    assert "warehouse" in names, "共享行情仓必须在清单里（它曾按名字被跳过）"
    warehouse = next(i for i in section["sqlite"] if i["name"] == "warehouse")
    assert warehouse["write"]["write_allowed"] is False, (
        "test 档不是行情仓的写者，必须显式不可写")


def test_health_exposes_schedule_scope_contract(client) -> None:
    """`data_sources.schedule` 必须给出"本实例会触发哪些作业"（**唯一可见的面**）。

    为什么必须有这条判据（不是顺手加的）：裁剪理由原先只写在
    `SchedulerService.start()` 的 `logger.info` 里，而**全仓库没有
    `logging.basicConfig()`** ⇒ INFO 被 last-resort handler 丢弃
    ⇒ 逐字节搜 `data/run/*.log` 里的「调度器已启动」= **0 处**（实测）。
    也就是说：**没有这个接口，"作业为什么没跑"在运行中的实例上完全不可见** ——
    而"不可见的护栏"正是本项目最贵的那类缺陷。
    """
    from src.scheduler.registry import (
        JOB_REGISTRY,
        SCHEDULER_DENY_ENV,
        job_deny_reason,
        schedulable_jobs,
    )

    body = client.get("/api/v1/health").json()
    scope = body.get("data_sources", {}).get("schedule")
    assert isinstance(scope, dict), (
        "data_sources.schedule 缺失 —— 被裁的作业又变成不可见的了")
    assert scope.get("available") is True, scope
    # ★ `CHG-0141`：`role` / `out_of_role` / `worker` 三个字段是"重作业移出本进程"
    #   之后**唯一能看见**"它们此刻跑不跑"的面 —— 缺了它们，`/health` 会全绿，
    #   而那 4 个作业可能一直没人执行（前端不会报，因为 worker 没有端口）。
    for key in ("env", "total", "active", "pruned", "unknown_denied", "deny_env",
                "role", "out_of_role", "worker"):
        assert key in scope, f"缺字段 {key}"
    assert scope["env"] == ds.current_env()
    assert scope["deny_env"] == SCHEDULER_DENY_ENV
    assert scope["total"] == len(JOB_REGISTRY)
    # ★ 同源判据：接口里的 active 必须等于 `_tick()` 真跑时遍历的作业数
    assert scope["active"] == len(schedulable_jobs())
    # 恒等式要写成**通用**形式（角色外也算掉一份）—— 原来写的是
    # `active + pruned == total`，那在"未设角色"时才成立；一旦角色拆分生效
    # （role=api）它就会假红，而假红的下场是有人把这条判据删掉。
    assert (scope["active"] + len(scope["pruned"])
            + len(scope["out_of_role"])) == scope["total"]
    # `worker` 段的形状（值随环境变，但字段与路径来源是契约）
    worker = scope["worker"]
    for key in ("needed", "alive", "fresh", "age_sec", "ok", "verdict", "path"):
        assert key in worker, f"worker 段缺字段 {key}"
    assert worker["verdict"].strip(), "worker 段必须带人话结论"
    assert worker["path"].endswith("worker_heartbeat.json"), worker["path"]
    assert str(worker["path"]).startswith(str(ds.store_rel("scheduler_dir"))) or \
        "scheduler" in worker["path"], (
            f"心跳路径必须来自本环境的调度目录（否则会读到别的实例的心跳）："
            f"{worker['path']}")
    # 逐条对账：被点名的作业必须真的被判为"不该触发"，且理由是人话
    assert {i["job"] for i in scope["pruned"]} == {
        name for name in JOB_REGISTRY if job_deny_reason(name)}
    for item in scope["pruned"]:
        assert item["reason"].strip(), f"{item['job']} 被裁却没有理由"
        assert item["job"] not in schedulable_jobs()


def test_health_schedule_scope_degrades_instead_of_500(client, monkeypatch) -> None:
    """registry 读不到时：`available=False` + 原因，而不是 500 或"0 个作业"。"""
    from src.scheduler import registry as _reg

    def _boom(**_kw):  # noqa: ANN003
        raise OSError("模拟调度注册表读不到")

    monkeypatch.setattr(_reg, "scheduler_scope_report", _boom)
    resp = client.get("/api/v1/health")
    assert resp.status_code == 200, resp.text
    scope = resp.json()["data_sources"]["schedule"]
    assert scope["available"] is False
    assert scope.get("error"), "要给出原因，不能只留一个 false"
    assert "total" not in scope, (
        "读不到时不许给 total —— 0 会被读成『这台机器一个作业都没有』（假绿）")


def test_health_store_section_degrades_instead_of_500(client, monkeypatch) -> None:
    """registry 读不到时：`available=False` + 原因，而不是 500 或假绿。"""
    def _boom(**kw):  # noqa: ANN003
        raise OSError("模拟 registry 读不到")

    monkeypatch.setattr(ds, "describe", _boom)
    resp = client.get("/api/v1/health")
    assert resp.status_code == 200, resp.text
    section = resp.json()["data_sources"]["local_stores"]
    assert section["available"] is False
    assert section.get("error"), "要给出原因，不能只留一个 false"
    assert "count" not in section, (
        "读不到时不许给 count —— 0 会被读成『本地一个库都没有』（假绿）")
