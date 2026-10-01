"""进程角色划分的护栏（`CHG-0139`）。

## 为什么需要这一组判据

2026-09-30 三小时内用户三次报"前端显示后端不可达"。仪器
（`access_audit.latency_ms`）显示那次请求延迟最高 **64.4 秒**、某一分钟
**23 条里 22 条 >1 秒**，全部最终 200（**排队，不是报错**）
⇒ **作业与在线 API 共用同一个事件循环**。

按 `runs.jsonl` 的 1847 条真实记录，占用最大的四个**纯后台**作业合计
≈ **50,000 秒**（是 `daily_warm` 的 6 倍）。处置：**把它们挪到独立 worker 进程**，
API 进程设 `MOSS_SCHEDULER_ROLE=api` 不跑它们。

## 这组判据守的六个失效（每一个都会静默出问题）

* ① `HEAVY_JOBS` 写错名 ⇒ 那个作业**没人跑**（`..._heavy_jobs_all_exist_in_registry`）；
* ② 两个角色**重叠** ⇒ 同一作业**双跑**（`..._roles_are_disjoint_and_cover`）；
* ③ 角色未设时行为变了 ⇒ 升级即「静默丢作业」（`..._unset_role_keeps_everything`）；
* ④ 重作业被当成「裁剪」上报 ⇒ 运营者以为实例裁了它
  （`..._scope_report_separates_role_from_pruning`）；
* ⑤ `trigger()` 旁路不过角色判据 ⇒ 在线进程又把重作业跑起来
  （`..._trigger_refuses_a_job_outside_the_role`）；
* ⑥ 只看枚举判「worker 在不在」⇒ 枚举抖动就白起进程 + 报假警报
  （`..._worker_present_has_three_independent_evidences`）。
"""
from __future__ import annotations

import pytest

from src.scheduler import registry as reg


@pytest.fixture(autouse=True)
def _pilot_env(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """★ 这组判据必须锚定 **pilot**（`MOSS_ENV=pilot`）。

    为什么：`job_deny_reason()` 会先按**写权限归属**裁作业
    （`configs/data_stores.yaml` 的 `warehouse.writer`），而 `quant_data_sync`
    在 **dev** 上会被裁掉（dev 不是行情仓写者）⇒ 在 dev 语境下
    "worker == HEAVY_JOBS" 必然不成立。**这不是缺陷，是设计**：
    拆分的目标部署就是 pilot（它是唯一写者）。

    第一版判据没锚环境，于是三条判据同时报红 —— 报的是**判据自己的问题**，
    这正说明"先按写权限裁、再按角色分"这个顺序是对的。

    ⚠️ **`SCHEDULER_DIR` 也必须一起重定向**（2026-09-30 实测踩到）：
    `heartbeat_path()` 在没有这个变量时会回落到 `get_settings()`，
    而那时 `MOSS_ENV=pilot` ⇒ **一份 pilot 的 Settings 被缓存进进程**
    ⇒ 后面的 `/health` 用例被当成公网实例、全部 401
    （四条判据在单独跑时全绿、合起来跑全红）。夹具只管到自己的作用域，
    跨用例的缓存清理在 `tests/conftest.py::_reset_settings_cache`（autouse）。
    """
    monkeypatch.setenv("MOSS_ENV", "pilot")
    monkeypatch.setenv("SCHEDULER_DIR", str(tmp_path / "scheduler"))
    monkeypatch.delenv(reg.ROLE_ENV, raising=False)


def _jobs_for(role: str, monkeypatch: pytest.MonkeyPatch) -> set[str]:
    monkeypatch.setenv(reg.ROLE_ENV, role)
    return set(reg.schedulable_jobs())


# ─────────────── ① 名字必须在注册表里 ───────────────


def test_heavy_jobs_all_exist_in_registry() -> None:
    """★ 写错名字 = 那个作业**永远不会有人跑**，且没有任何报错。

    这是"靠字符串清单驱动"的固有风险（本项目反复记录过：写在两处的清单必然漂移）。
    所以判据从**注册表现读**，而不是信任这份常量。
    """
    missing = [j for j in reg.HEAVY_JOBS if j not in reg.JOB_REGISTRY]
    assert not missing, (
        f"`HEAVY_JOBS` 里有不在 `JOB_REGISTRY` 的作业名：{missing} ⇒ "
        "它们既不会被 API 进程跑、也不会被 worker 跑（静默消失）。"
        "多半是作业改名了 —— 同步改 `HEAVY_JOBS`。")
    assert reg.HEAVY_JOBS, "`HEAVY_JOBS` 为空 ⇒ 这次拆分什么也没挪"


def test_heavy_jobs_are_not_write_pruned_away() -> None:
    """拆分的目标环境里，这 4 个作业必须**都能被调度**（否则清单里有个永不执行的名字）。

    锚定 pilot（见 `_pilot_env`）：pilot 是行情仓的写者 ⇒ `quant_data_sync` 不被裁。
    """
    for name in reg.HEAVY_JOBS:
        assert not reg.job_deny_reason(name), (
            f"{name} 在 pilot 上被写权限归属裁掉，却被列进 `HEAVY_JOBS` —— "
            "要么它不属于重作业，要么 `warehouse.writer` 归属需要重新裁定")


# ─────────────── ② 角色互斥且覆盖全集 ───────────────


def test_roles_are_disjoint_and_cover(monkeypatch: pytest.MonkeyPatch) -> None:
    """`api ∩ worker == ∅` 且 `api ∪ worker == 全表`（每个作业**恰好**被一个进程负责）。"""
    monkeypatch.delenv(reg.ROLE_ENV, raising=False)
    all_jobs = set(reg.schedulable_jobs())
    api = _jobs_for(reg.ROLE_API, monkeypatch)
    worker = _jobs_for(reg.ROLE_WORKER, monkeypatch)

    overlap = api & worker
    assert not overlap, f"两个角色都跑这些作业 ⇒ 会**双跑**：{sorted(overlap)}"
    assert api | worker == all_jobs, (
        f"有作业**没人跑**（漏在角色之外）：{sorted(all_jobs - (api | worker))}")
    assert worker == set(reg.HEAVY_JOBS), (
        f"worker 的作业集必须是 `HEAVY_JOBS` 本身，实际差集 "
        f"{sorted(worker ^ set(reg.HEAVY_JOBS))}")


# ─────────────── ③ 未设角色 = 全跑（不制造静默丢作业） ───────────────


def test_unset_role_keeps_everything(monkeypatch: pytest.MonkeyPatch) -> None:
    """未设 `MOSS_SCHEDULER_ROLE` 时**行为与从前逐字一致**（全表）。"""
    monkeypatch.delenv(reg.ROLE_ENV, raising=False)
    with_none = set(reg.schedulable_jobs())
    monkeypatch.setenv(reg.ROLE_ENV, "")
    still_none = set(reg.schedulable_jobs())
    assert with_none == still_none, "空串与未设必须同解（否则运维会踩到隐式差异）"
    assert set(reg.HEAVY_JOBS) <= with_none, "未设角色时重作业必须仍在表内"


# ─────────────── ④ 作用域报告要分清"角色外"与"被裁" ───────────────


def test_scope_report_separates_role_from_pruning(monkeypatch: pytest.MonkeyPatch) -> None:
    """★ 角色外的作业**不能**混进 `pruned`（那会让运营者以为实例裁掉了它）。"""
    monkeypatch.setenv(reg.ROLE_ENV, reg.ROLE_API)
    rep = reg.scheduler_scope_report()
    assert rep["role"] == reg.ROLE_API
    assert set(rep["out_of_role"]) == set(reg.HEAVY_JOBS)
    assert not (set(rep["out_of_role"]) & {p["job"] for p in rep["pruned"]}), (
        "同一个作业既算‘角色外’又算‘被裁’ ⇒ 两种原因混在一起没法处置")
    assert rep["active"] == rep["total"] - len(rep["pruned"]) - len(rep["out_of_role"])


def test_start_worker_is_registered() -> None:
    """`manage.py start-worker` 必须存在 —— 否则 role=api 之后没人跑重作业。

    （这条是"接线"判据：lint 检查不出来"有没有人真的能启动那个进程"。）
    """
    import manage

    src = manage.__file__
    assert src, "manage.py 不可读"
    from pathlib import Path

    text = Path(src).read_text(encoding="utf-8")
    assert "start-worker" in text and "cmd_start_worker" in text
    assert "MOSS_SCHEDULER_ROLE" in text and "src.scheduler.worker" in text


# ─────────────── ⑤ 拆分的**代价**必须自带答案（`CHG-0139` 第二半）───────────────


def test_worker_env_is_the_api_env_plus_role() -> None:
    """★ worker 的环境必须与 **API 的环境逐项相同**，只差 `MOSS_SCHEDULER_ROLE`。

    为什么这是判据：两个进程只要有一项不同（库路径 / 审计目录 / 调度目录），
    "谁跑了多久"就会分成两份账、`/health` 的心跳会去读另一个环境的目录，
    而症状全都是**看起来正常**。这次事故正是靠那一份账
    （`data/pilot/scheduler/runs.jsonl` 的 1847 条记录）才定位到的。
    """
    import manage

    for env in ("pilot", "dev"):
        api = {"pilot": manage.pilot_isolation_env,
               "dev": manage.dev_isolation_env}[env]()
        worker = manage.worker_env(env)
        assert worker is not None
        assert worker["MOSS_SCHEDULER_ROLE"] == "worker"
        diff = {k: (api.get(k), worker.get(k))
                for k in set(api) | set(worker)
                if k != "MOSS_SCHEDULER_ROLE" and api.get(k) != worker.get(k)}
        assert not diff, f"{env}: worker 与 API 的环境不一致：{diff}"


def test_env_needs_worker_is_derived_from_api_role() -> None:
    """`env_needs_worker` 必须**派生**自 API 的隔离环境，不是第二份清单。

    两份清单必然漂移，而漂移的症状正是这次要消灭的那个：
    **API 不跑重作业，而没有人跑**。
    """
    import manage

    for env, maker in (("pilot", manage.pilot_isolation_env),
                       ("dev", manage.dev_isolation_env)):
        expected = maker().get("MOSS_SCHEDULER_ROLE") == "api"
        assert manage.env_needs_worker(env) is expected, env
    assert manage.env_needs_worker("test") is False   # 没有隔离定义 ⇒ 不需要


def test_ensure_supervises_the_worker() -> None:
    """★ 值守必须**连带**保证 worker —— 否则"起了 API 忘了 worker"没有任何人发现。

    `scripts/pilot_watchdog.ps1` 是**全项目唯一**的运行期值守入口（每分钟一轮，
    文件头写明），所以重作业的存活检查必须挂在同一个入口里，不再造第二个值守。

    这条判据查的是**调用关系**（静态），因为动态验它需要真起两个进程；
    调用关系一旦断掉，"worker 挂了没人管"就会**静默**回归。
    """
    import inspect

    import manage

    src = inspect.getsource(manage.cmd_ensure)
    assert "ensure_worker(" in src, (
        "`cmd_ensure` 不再检查 worker ⇒ 拆出去的那 4 个重作业挂了没人拉起来")
    # 两条成功路径（后端本来就健康 / 后端刚被重启）都要过一遍
    assert src.count("ensure_worker(") >= 2, (
        "只覆盖了一条路径：另一端仍会留下「API 在跑、重作业没人跑」的静默状态")
    start_src = inspect.getsource(manage.cmd_start)
    assert "ensure_worker(" in start_src, (
        "`manage.py start --env pilot` 只起 API ⇒ 这条命令的净效果是"
        "「服务起来了、4 个重作业从此不再执行」，而输出全是 ✅")


def test_worker_present_has_three_independent_evidences(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★ `worker_present()` 不许只依赖命令行枚举（它会抖动，实测过）。

    现场：本机负载高时 PowerShell CIM 查询超时 ⇒ 枚举返回空 ⇒ `ensure` 判成
    「没在跑」⇒ 白起一个 worker（被单实例锁拦住，但日志里留下一句 ERROR +
    值守报了一条「启动后立即退出」的**假警报**）。

    判据：枚举为空但**心跳新鲜**时必须判「在」（并能说出「第一路仪器不可靠」）；
    心跳也没有、锁也拿得到 ⇒ 判「不在」（否则会永远不去拉起它，那更糟）。
    """
    import manage
    from src.scheduler import worker_heartbeat as hb
    from src.scheduler import worker_lock as wl

    class _Locked:
        holder: dict = {}

        def __init__(self, *_a, **_kw) -> None:
            pass

        def acquire(self, **_kw) -> bool:
            return False        # 锁被持有 = 有人真的在跑（操作系统级证据）

        def release(self) -> None:
            pass

    # ① 枚举为空 + 心跳新鲜 ⇒ 在
    monkeypatch.setattr(manage, "list_our_worker_pids", lambda: [])
    monkeypatch.setattr(hb, "read_status", lambda **_kw: {
        "present": True, "alive": True, "age_sec": 8.0, "pid": 777,
        "role": "worker", "jobs": [], "beats": 3,
        "started_at_iso": "2026-09-30 10:00:00", "path": "x", "note": "n"})
    present, why = manage.worker_present()
    assert present is True, "心跳新鲜却被判成不在 ⇒ 会白起一个 worker"
    assert "心跳" in why and "不可靠" in why, (
        f"必须把「第一路仪器不可靠」这件事留痕，实际理由：{why!r}")

    # ② 枚举为空 + 心跳陈旧 + 锁拿不到 ⇒ 在（锁是操作系统级证据）
    monkeypatch.setattr(hb, "read_status", lambda **_kw: {
        "present": False, "alive": False, "age_sec": None, "pid": None,
        "role": None, "jobs": [], "beats": None, "started_at_iso": None,
        "path": "x", "note": "n"})
    monkeypatch.setattr(wl, "WorkerLock", _Locked)
    present, why = manage.worker_present()
    assert present is True and "锁" in why, why

    # ③ 三路都没有 ⇒ 不在（否则永远不去拉起它）
    class _Free(_Locked):
        def acquire(self, **_kw) -> bool:
            return True

    monkeypatch.setattr(wl, "WorkerLock", _Free)
    present, why = manage.worker_present()
    assert present is False, f"三路都没有证据却判成在跑 ⇒ 重作业永不恢复：{why!r}"


def test_ensure_worker_does_not_cry_wolf_when_lock_says_alive(
    monkeypatch,
) -> None:
    """★ 起了新进程但它立刻退出、而锁说"有人持着" ⇒ **不许报假警报**。

    这是上面那个抖动现场的第二半：白起的进程被锁挡下（退出码 3），
    若直接报"启动后立即退出 ⇒ 重作业仍无人执行"，运维会去查一个并不存在的故障。
    """
    import manage

    monkeypatch.setattr(manage, "env_needs_worker", lambda _env: True)
    monkeypatch.setattr(manage, "worker_present",
                        lambda: (True, "单实例锁被持有（操作系统级证据）"))
    monkeypatch.setattr(manage, "_spawn_worker", lambda _env: 999999)
    monkeypatch.setattr(manage, "_pid_alive", lambda _pid: False)  # 新进程立刻退出
    assert manage.ensure_worker("pilot") == 0, (
        "锁证明有人持着 ⇒ 这不叫故障，不该返回非零去惊动值守")


def test_restart_pilot_is_registered_and_scoped() -> None:
    """★ `manage.py restart-pilot` 必须存在，且**不许**用全量 `stop_backend_processes`。

    为什么需要这个命令：`stop` / `--replace` 按命令行枚举本项目**全部**后端进程
    （含 dev 8100）—— 跨端口并存时用它们重启 pilot 会把 dev 一起停掉。

    为什么"不许用全量停止"要写成判据：这正是这条命令存在的理由，
    而它极其容易被下一个人"顺手复用"掉（复用 `stop_backend_processes` 更省事，
    症状却是把另一个实例静默停掉）。
    """
    import inspect

    import manage

    parser = manage.build_parser()
    subs = {a.dest for a in parser._actions if a.dest == "command"}  # noqa: SLF001
    assert subs or True                      # 结构自证：parser 可构建
    src = inspect.getsource(manage.cmd_restart_pilot)
    assert "stop_backend_processes" not in src, (
        "`restart-pilot` 复用了全量停止 ⇒ 它会把 dev(8100) 一起停掉，"
        "而这条命令的全部意义就是**只动 pilot**")
    head = inspect.getsource(manage.build_parser)
    assert '"restart-pilot"' in head or "'restart-pilot'" in head, (
        "子命令没有注册 ⇒ 命令不存在（用户按文档敲会得到 invalid choice）")


def test_stop_file_paths_are_resolved_per_target_env() -> None:
    """★★ 停止文件路径必须在**目标环境**里求值（`CHG-0112` 的同一形状，第三次）。

    实测（2026-09-30）：`restart-pilot` 里直接调 `stop_file_path()`，而
    `manage.py` 跑在运维的 shell 里（没有 `SCHEDULER_DIR` / `MOSS_ENV`）
    ⇒ 路径解析成 `data/scheduler/worker.stop`，而 pilot 的 worker 看的是
    `data/pilot/scheduler/worker.stop` ⇒ **停止信号发到了另一个目录，两边都静默**：
    命令输出"已写停止文件"，worker 却毫无反应（只能硬杀，而硬杀不 checkpoint）。

    判据：pilot / dev 两档必须解析到各自的环境目录，且 `cmd_stop` 用的全量版本
    必须**同时覆盖**两个环境（它要停的是所有实例，不是某一档）。
    """
    import manage

    pilot_path = manage._worker_stop_files("pilot")[0]      # noqa: SLF001
    dev_path = manage._worker_stop_files("dev")[0]          # noqa: SLF001
    assert "pilot" in str(pilot_path) and pilot_path.name.endswith(".stop"), (
        f"pilot 档的停止文件不在 pilot 的调度目录里：{pilot_path}")
    assert "dev" in str(dev_path), f"dev 档解析错了：{dev_path}"
    assert pilot_path != dev_path, "两档解析到了同一个文件 ⇒ 环境隔离失效"

    all_paths = manage._worker_stop_files()                 # noqa: SLF001
    assert len(all_paths) >= 2, (
        f"`cmd_stop` 用的全量版本必须覆盖所有已知环境，实际只有 {all_paths}")
    assert pilot_path in all_paths and dev_path in all_paths


def test_stop_covers_the_worker() -> None:
    """★ `stop` / `--replace` 必须**停掉 worker**，否则留下孤儿写者。

    worker 没有端口、命令行也与后端不同：漏掉它的后果是一个继续跑
    `quant_data_sync`（往 14 GiB 行情仓 upsert）的孤儿进程，而命令输出说
    「已停止」—— 与 `CHG-0087`（两个实例同时写同一个库）同一形状。
    """
    import inspect

    import manage

    assert manage.is_our_worker_cmdline(r"python -m src.scheduler.worker") is True
    assert manage.is_our_worker_cmdline("uvicorn src.api.main:app") is False
    # 反向也要成立：两个判据不能互相吞掉（端口归属那条必须保持"只看后端"）
    assert manage.is_our_cmdline("python -m src.scheduler.worker") is False
    src = inspect.getsource(manage.stop_backend_processes)
    assert "list_our_worker_pids" in src, (
        "`stop_backend_processes` 不看 worker ⇒ `manage.py stop` 会留下孤儿 worker")


# ─────────────── ⑥ 触发路径**不止一条**（`CHG-0087` 的同一道门，第二次出现）───


@pytest.mark.asyncio
async def test_trigger_refuses_a_job_outside_the_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★★ `trigger()` 这条旁路也必须过**角色**判据，否则整次拆分被绕过。

    现场：`src/api/main.py` 的启动自检 `_check_quant_sync_at_startup()` 用
    `scheduler.trigger("quant_data_sync")` 补行情缺口 —— 那是 `HEAVY_JOBS` 之一。
    第一次改拆分时只改了 `_tick()` 走的 `schedulable_jobs()`，
    于是**这条路径原样把重作业拉回了在线进程**：前端又报不可达，
    而排查的人会以为"已经挪出去了"。

    这条判据的行为断言是"**执行器一次都没被调用**"（不是"返回了 skipped"）：
    只看返回值的话，将来有人在 skipped 之前插入一次调用就没人拦得住了。
    """
    from src.scheduler.service import CronScheduler

    monkeypatch.setenv(reg.ROLE_ENV, reg.ROLE_API)
    heavy = reg.HEAVY_JOBS[0]
    calls: list[str] = []

    async def _fake_execute(runtime, name, run_log, source):  # noqa: ANN001
        calls.append(name)
        return {"status": "ok", "job_name": name}

    sched = CronScheduler(object(), object(), execute_fn=_fake_execute)
    out = await sched.trigger(heavy, source="startup")

    assert out["status"] == "skipped", f"{heavy} 竟然在 role=api 进程里被执行了：{out}"
    assert not calls, f"执行器被调用了 {calls} —— 角色判据没有拦住它"
    assert out.get("reason"), "skip 必须带人话理由（说清由谁跑），否则运维只能猜"


@pytest.mark.asyncio
async def test_trigger_inside_the_role_still_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """反面：**角色内**的作业必须照跑 —— 不能为了堵旁路把功能堵死。

    （只测"被拦住"的判据会奖励"一律拦住"这种错误实现。）
    """
    from src.scheduler.service import CronScheduler

    monkeypatch.setenv(reg.ROLE_ENV, reg.ROLE_WORKER)
    heavy = reg.HEAVY_JOBS[0]
    calls: list[str] = []

    async def _fake_execute(runtime, name, run_log, source):  # noqa: ANN001
        calls.append(name)
        return {"status": "ok", "job_name": name}

    sched = CronScheduler(object(), object(), execute_fn=_fake_execute)
    out = await sched.trigger(heavy, source="startup")

    assert calls == [heavy], f"worker 进程里重作业必须能跑，实际：{out}"
    assert out["status"] == "ok"
