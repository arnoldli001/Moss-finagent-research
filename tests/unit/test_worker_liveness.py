"""调度 worker 的**存活信号**与**单实例锁**的判据（`CHG-0139`）。

这组判据针对的是拆分**自带的两类新静默失效**（详见 `src/scheduler/worker.py`
的 docstring 表）：

    ① 重作业停摆而无人知道  → `worker_heartbeat` 的三态必须可区分；
    ② 起两次 ⇒ 重作业双跑    → `worker_lock` 必须真的互斥；
    ③ worker 里卡了多久没人知道 → `loop_lag` 必须在启动序列里**真的被启动**（带 worker 标签）；
    ④ 无痕死亡（原生崩溃不留 traceback） → 「上一次是怎么结束的」必须留下痕迹。

判据不测"代码长什么样"，测**行为**：写进去的心跳读出来是什么、陈旧了报什么、
两个锁能不能同时拿到、启动序列跑完之后监控与取证记录**实际**变成了什么。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import time
from pathlib import Path

import pytest

from src.scheduler import worker_heartbeat as hb
from src.scheduler import worker_lock as wl


@pytest.fixture()
def sched_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把调度目录指到临时目录 —— 判据绝不碰真库/真调度目录。"""
    monkeypatch.setenv("SCHEDULER_DIR", str(tmp_path))
    return tmp_path


# ─────────────── ① 三态必须可区分（合并成 bool 就丢掉了处置动作）───────────────


def test_missing_heartbeat_says_never_started(sched_dir: Path) -> None:
    """**没有文件** ≠ 死了：处置是"去起一个"，不是"去看日志"。"""
    st = hb.read_status(path=sched_dir / hb.HEARTBEAT_FILE)
    assert st["present"] is False
    assert st["alive"] is False
    assert "从未起过" in st["note"], st["note"]


def test_fresh_heartbeat_is_alive(sched_dir: Path) -> None:
    path = sched_dir / hb.HEARTBEAT_FILE
    hb.write_once(role="worker", jobs=["a", "b"], pid=4321, path=path)
    st = hb.read_status(path=path)
    assert st["present"] and st["alive"]
    assert st["pid"] == 4321 and st["jobs"] == ["a", "b"]
    assert st["age_sec"] is not None and st["age_sec"] < hb.STALE_AFTER_SEC


def test_stale_heartbeat_is_not_alive_but_still_present(sched_dir: Path) -> None:
    """陈旧：**起过、现在没了**。`present` 与 `alive` 必须分开报。

    只给一个 bool 的话，调用方无法区分"从来没起"与"起来又死了" ——
    而这两种情况的排查动作完全不同（去起 vs 去看日志尾部）。
    """
    path = sched_dir / hb.HEARTBEAT_FILE
    hb.write_once(role="worker", jobs=["a"], pid=1, path=path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["beat_at"] = time.time() - (hb.STALE_AFTER_SEC + 30)
    path.write_text(json.dumps(raw), encoding="utf-8")

    st = hb.read_status(path=path)
    assert st["present"] is True
    assert st["alive"] is False
    assert st["age_sec"] > hb.STALE_AFTER_SEC
    assert "无人执行" in st["note"], st["note"]


def test_corrupt_heartbeat_is_reported_as_unreadable_not_dead(sched_dir: Path) -> None:
    """半写/损坏 ⇒ 说"读不懂"，**不**说成"进程死了"（两者的动作不同）。"""
    path = sched_dir / hb.HEARTBEAT_FILE
    path.write_text('{"pid": 1, "beat_at":', encoding="utf-8")
    st = hb.read_status(path=path)
    assert st["present"] is True
    assert st["alive"] is False
    assert "无法解析" in st["note"]


def test_threshold_is_a_multiple_of_interval() -> None:
    """阈值必须**大于若干个**写间隔 —— 否则一次写失败就被读成"死了"。

    这是取值纪律（与隧道值守"连续 4 次失败才重启"同源）：
    **宁可不报，也不要误报**。写死在这里，防止有人把两个数字调反。
    """
    assert hb.STALE_AFTER_SEC >= 3 * hb.INTERVAL_SEC


def test_heartbeat_is_written_by_a_dedicated_thread(sched_dir: Path) -> None:
    """★ 心跳必须由**独立线程**写，且进程活着时**立刻**就有第一拍。

    为什么这条是判据而不是注释：写成 `asyncio.Task` 一样能通过"文件里有心跳"
    这种测试，但重作业一跑（实测中位 2947 秒）心跳就停 —— 于是**正在干活**的
    worker 会被判成**死的**，进而被重启。把忙判成死比不监控更糟。
    """
    path = sched_dir / hb.HEARTBEAT_FILE
    beat = hb.WorkerHeartbeat(role="worker", jobs=["x"], interval=0.05, path=path)
    beat.start()
    try:
        # 第一拍必须是**立刻**（不能等一个 interval）：worker 刚起来就崩时，
        # 值守看到的是"从未起过"还是"起来过"，取决于这一拍。
        deadline = time.time() + 2.0
        while time.time() < deadline and not path.exists():
            time.sleep(0.01)
        assert path.exists(), "start() 后 2 秒内没有写出第一拍心跳"
        assert beat._thread is not None and beat._thread.daemon
        # 让它至少跳两次，确认它是**持续**在写，不是只写一次
        first = json.loads(path.read_text(encoding="utf-8"))["beats"]
        deadline = time.time() + 3.0
        while time.time() < deadline:
            now = json.loads(path.read_text(encoding="utf-8"))["beats"]
            if now > first:
                break
            time.sleep(0.02)
        assert json.loads(path.read_text(encoding="utf-8"))["beats"] > first
    finally:
        beat.stop()


# ─────────────── ② 单实例锁必须真的互斥（否则重作业双跑）───────────────


def test_second_lock_cannot_be_acquired(sched_dir: Path) -> None:
    """★ 第二次 `acquire()` 必须失败，并且**报出持有者是谁**。

    "能不能拿到锁"是唯一判据；`holder` 只是为了让报错能指出是谁 ——
    只说"已在运行"等于让运维去猜（本项目为"报错里没有现场"付过代价）。
    """
    first = wl.WorkerLock(sched_dir / wl.LOCK_FILE)
    second = wl.WorkerLock(sched_dir / wl.LOCK_FILE)
    assert first.acquire(env="test") is True
    try:
        assert second.acquire(env="test") is False
        assert second.holder.get("pid"), f"拿不到锁时必须能说出持有者：{second.holder}"
    finally:
        first.release()


def test_lock_is_released_and_reacquirable(sched_dir: Path) -> None:
    """释放后必须能再拿到 —— 否则一次正常重启就会**永久**起不来 worker。"""
    path = sched_dir / wl.LOCK_FILE
    first = wl.WorkerLock(path)
    assert first.acquire(env="test")
    first.release()
    second = wl.WorkerLock(path)
    assert second.acquire(env="test")
    second.release()


# ─────────────── ③ 停止握手：不依赖控制台的那条通道（`CHG-0141`）───────────────


@pytest.mark.asyncio
async def test_stop_file_newer_than_process_triggers_shutdown(sched_dir: Path) -> None:
    """★ 停止文件**比本进程新** ⇒ 优雅关停。

    为什么需要这条通道（2026-09-30 实测）：`request_graceful_stop` 的
    CTRL_BREAK 要求**调用方与目标共享控制台**，而本项目所有守护进程都是
    `CREATE_NO_WINDOW` 起的、值守又跑在计划任务里 ⇒ 实测它**返回 False**
    ⇒ worker 只能被硬杀，而硬杀**不走关停收尾**（不 checkpoint）。
    """
    import asyncio

    from src.scheduler.worker import watch_stop_file

    path = sched_dir / "worker.stop"
    stop = asyncio.Event()
    started = time.time()
    path.write_text("stop\n", encoding="utf-8")
    os.utime(path, (time.time() + 1, time.time() + 1))   # 明确晚于启动时刻
    task = asyncio.create_task(
        watch_stop_file(stop, path=path, started_at=started, interval=0.05))
    await asyncio.wait_for(task, timeout=2.0)
    assert stop.is_set(), "比本进程新的停止文件必须触发关停"


@pytest.mark.asyncio
async def test_stale_stop_file_never_kills_a_fresh_worker(sched_dir: Path) -> None:
    """★★ **旧文件不许杀新进程**：陈旧的停止文件必须被忽略并删掉。

    没有这条不变量的后果很隐蔽：上次停止留下的文件会让**下一次**启动的 worker
    在第一轮轮询时立刻自杀 —— 而现象看起来是"worker 起不来"，
    排查方向（配置？依赖？锁？）与真因（一个残留文件）完全无关。
    """
    import asyncio

    from src.scheduler.worker import watch_stop_file

    path = sched_dir / "worker.stop"
    path.write_text("stale\n", encoding="utf-8")
    old = time.time() - 3600
    os.utime(path, (old, old))                 # 早于本进程启动时刻
    stop = asyncio.Event()
    task = asyncio.create_task(
        watch_stop_file(stop, path=path, started_at=time.time(), interval=0.05))
    await asyncio.sleep(0.3)                   # 至少跑过一轮
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not stop.is_set(), "陈旧停止文件竟然杀掉了新进程"
    assert not path.exists(), "陈旧停止文件应当被顺手清掉（否则每轮都要重判一次）"


def test_stop_file_lives_next_to_the_lock_and_heartbeat(sched_dir: Path) -> None:
    """三者必须在**同一个目录**：`manage.py stop` 与 worker 要对"哪个文件"同解。"""
    from src.scheduler import worker as wk

    assert wk.stop_file_path() == sched_dir / wk.STOP_FILE
    assert wl.worker_lock_path().parent == wk.stop_file_path().parent
    assert hb.heartbeat_path().parent == wk.stop_file_path().parent


def test_lock_records_where_it_lives(sched_dir: Path) -> None:
    """锁文件必须落在 `SCHEDULER_DIR` 下（**同一环境的 worker 才互斥**）。

    落错目录的后果是"互斥失效"：dev 的 worker 与 pilot 的 worker 各自拿到
    自己的锁，然后**同时**跑 `quant_data_sync` 往同一个行情仓写 ——
    正是这次拆分要防的事故形状。
    """
    lock = wl.WorkerLock()
    assert lock.path == sched_dir / wl.LOCK_FILE
    assert hb.heartbeat_path() == sched_dir / hb.HEARTBEAT_FILE


def test_lock_byte_sits_outside_the_content_region() -> None:
    """★ 锁的字节不能压在内容上：Windows 的字节范围锁**连读一起拒**。

    实测（2026-09-30）：锁在偏移 0 时，`acquire()` 失败的进程读不出持有者是谁
    （`OSError: Permission denied`）—— 而"说清是谁在跑"正是那段代码的全部意义。
    所以锁字节与 JSON 内容必须分区，且内容要短到不会碰到锁字节。
    """
    assert wl.LOCK_OFFSET >= 64, (
        "锁字节离文件头太近：内容（JSON，约 100 字节）会压到锁区上，"
        "于是别的进程既读不到持有者、也写不进自己的信息")
    sample = json.dumps({"pid": 999999, "env": "pilot",
                         "started_at": 1790750000.0,
                         "started_at_iso": "2026-09-30 14:34:13"},
                        ensure_ascii=False).encode("utf-8")
    assert len(sample) < wl.LOCK_OFFSET, (
        f"内容长度 {len(sample)} 已越过锁字节 {wl.LOCK_OFFSET} —— 调大 LOCK_OFFSET")


@pytest.mark.skipif(os.name != "nt", reason="字节范围锁的拒写行为是 Windows 特有的")
def test_acquire_survives_a_denied_content_write(sched_dir: Path) -> None:
    """★ **内容写不进去，锁仍然算拿到**（否则一个旧版 worker 就能让新版起不来）。

    实测事故（2026-09-30）：一个**旧版** worker 锁在偏移 0，新代码于是在写
    自己被拒（`PermissionError: [Errno 13]`）时崩掉 —— 而它**明明拿到了锁**。
    锁管"能不能跑"（判据），内容只管"事后看得出是谁"（线索）：
    线索拿不到时不能让判据失效。
    """
    import msvcrt

    path = sched_dir / wl.LOCK_FILE
    blocker = path.open("w+b")          # 模拟旧版：占住偏移 0
    try:
        msvcrt.locking(blocker.fileno(), msvcrt.LK_NBLCK, 1)
        lock = wl.WorkerLock(path)
        assert lock.acquire(env="test") is True, "被拒写内容不该让启动失败"
        assert lock.holder.get("pid") == os.getpid()
        lock.release()
    finally:
        try:
            blocker.seek(0)
            msvcrt.locking(blocker.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        blocker.close()


# ───────── ④ worker 侧补的两件仪器：循环延迟监控 + 崩溃取证（跑真启动序列）─────────


class _FakeRuntime:
    """够 `_shutdown()` 遍历的假 runtime（四个仓储都是 None ⇒ 各句直接跳过）。"""

    event_repo = None
    intraday_profile_repo = None
    fundflow_repo = None
    repo = None


class _FakeScheduler:
    """假调度器：真调度器会起一个每分钟 tick 的循环，而判据不需要它。"""

    def __init__(self, *_a: object, **_kw: object) -> None:
        pass

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None


async def _stop_immediately(stop: asyncio.Event, **_kw: object) -> None:
    """替身版停止握手：判据只关心"起来 → 收尾"，不需要等 5 秒的文件轮询。"""
    stop.set()


def _no_heartbeat() -> dict:
    """心跳读数的替身：从来没有过（三路存活证据里这一路缺席）。"""
    return {"present": False, "alive": False, "age_sec": None, "pid": None,
            "role": None, "jobs": [], "beats": None, "started_at_iso": None,
            "path": "x", "note": ""}


@pytest.fixture()
def worker_startup(sched_dir: Path, monkeypatch: pytest.MonkeyPatch):
    """★ 把 `_run()` 的启动序列要碰的**一切**换成替身或临时目录。

    ## 为什么必须跑**真序列**（而不是断言"源码里有 `loop_lag.start` 这一行"）

    "监控到底起没起""记录到底写没写"这两件事**只有跑一遍**才证明得了：
    断言源码里有一行，只证明那行字还在（本项目为"假护栏"付过代价：
    看起来绿、实际什么都没证明）。但真跑一遍要挡住三件**不该发生**的事：

      · `build_runtime()` 会装配 LLM 网关与几十个连接器（判据不需要，且慢）；
      · 真调度器会起一分钟 tick 的循环；
      · ★ `checkpoint_and_release_all()` 会去 checkpoint **生产 SQLite**
        —— 单测里绝不能发生（`tests/conftest.py` 的 `_isolate_*` 系列
        为"单测真去碰生产文件"写过一串血案）。

    另外 `_run()` 会在主线程注册**真实信号处理器**，用例结束必须还原，
    否则后面所有用例的 SIGINT 都被这一个用例接管了。
    """
    import src.api.runtime as runtime_mod
    import src.core.executors as executors_mod
    import src.core.loop_lag as loop_lag
    import src.core.sqlite_recovery as recovery_mod
    import src.scheduler.service as service_mod
    from src.scheduler import worker as wk

    monkeypatch.setenv("MOSS_SCHEDULER_ROLE", "worker")
    monkeypatch.setattr(runtime_mod, "build_runtime", lambda: _FakeRuntime())
    monkeypatch.setattr(service_mod, "CronScheduler", _FakeScheduler)
    monkeypatch.setattr(recovery_mod, "checkpoint_and_release_all", lambda: (0, 0))
    monkeypatch.setattr(executors_mod, "shutdown_infra_executors", lambda: None)
    monkeypatch.setattr(wk, "watch_stop_file", _stop_immediately)

    # 循环延迟监控是**进程级单例**：清掉上一次用例可能留下的任务与统计，
    # 并在收尾把**标签**也放回默认 —— 实测过它的代价（见下面 test_loop_lag_* 的断言）：
    tag_before = loop_lag.process_tag()
    loop_lag._TASK = None
    loop_lag.stats().reset()

    saved = {sig: signal.getsignal(sig)
             for sig in (signal.SIGINT, signal.SIGTERM,
                         getattr(signal, "SIGBREAK", None))
             if sig is not None}
    try:
        yield wk
    finally:
        for sig, handler in saved.items():
            signal.signal(sig, handler)
        loop_lag._TASK = None
        loop_lag.stats().reset()
        loop_lag._TAG = tag_before


def _run_worker_once() -> int:
    """跑一次**真的**启动序列（重活由 `worker_startup` 夹具换成替身）。"""
    from src.scheduler import worker as wk

    return asyncio.run(wk._run())


def test_run_record_lives_next_to_the_lock_and_heartbeat(sched_dir: Path) -> None:
    """取证记录必须与锁/心跳/停止文件落在**同一个目录**。

    这条防的是本项目记过的那类事故：路径推导被写第二遍 ⇒ 数 `parents[N]`
    数错一层 ⇒ 文件写歪而**没有任何报错**（"记录说上次死了"说的可能是
    另一个环境的 worker，于是两个事故被读成同一个）。
    """
    from src.scheduler import worker as wk

    assert wk.run_record_path() == sched_dir / wk.RUN_RECORD_FILE
    assert wk.run_record_path().parent == hb.heartbeat_path().parent
    assert wk.run_record_path().parent == wl.worker_lock_path().parent


def test_normal_exit_is_not_reported_as_abnormal_at_next_start(
    worker_startup: object, caplog: pytest.LogCaptureFixture,
) -> None:
    """★ 判据 ①：**正常退出后，下一次启动不许报「上次异常终止」**。

    为什么要与"伪造一条未标记记录"那条**成对**看：单看这一条，
    "记录根本没写出来"也能通过（`state=never` 同样不报警）⇒
    所以这里同时钉住"记录确实写了、而且记的是**本次**运行的 pid"。
    """
    from src.scheduler import worker as wk

    caplog.set_level(logging.WARNING, logger="src.scheduler.worker")
    assert _run_worker_once() == 0
    # 按**存活报告的同一种读法**读（锁空着 ⇒ 那次运行确已结束，见 `run_ended`）
    rep = wk.last_run_report(run_ended=True)
    assert rep["state"] == "clean", f"正常退出却被判成 {rep['state']}：{rep['note']}"
    assert rep["needs_attention"] is False
    assert rep["pid"] == os.getpid(), "记录里的 pid 不是本进程 ⇒ 记录根本没写对"
    # ⚠️ 不带外部证据的读法（`run_ended=None`）只报"最近一次**确知已结束**的运行"，
    #    因此它**不**用来报警 —— 报警必须由锁/启动这两个确知时刻之一来判
    #    （否则运行途中每一次读都会报假警，见判据 ④）。

    # ── 第二次启动：它在启动序列里读到的"上一次"必须是正常结束 ──
    caplog.clear()
    assert _run_worker_once() == 0
    frozen = json.loads(wk.run_record_path().read_text(encoding="utf-8"))["previous"]
    assert frozen["state"] == "clean", f"下一次启动读成了 {frozen['state']}：{frozen['note']}"
    assert frozen["needs_attention"] is False
    assert "异常" not in caplog.text, f"正常退出之后的启动竟然报了异常：{caplog.text}"


def test_hard_killed_previous_run_is_reported_with_pid_and_time(
    worker_startup: object, sched_dir: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """★ 判据 ②：模拟"上次被强杀" ⇒ 下一次启动必须报**异常终止** + 上次的 pid/时刻。

    ⚠️ **只写一条未标记的记录，绝不对任何进程发信号**：这条判据要证明的是
    **判读逻辑**，而"进程被强杀"与"记录停在未标记"在磁盘上**完全同形**
    （原生崩溃不执行 `finally`/`atexit`，留下的就是这样一个文件）。
    `tests/conftest.py::_forbid_real_process_signals` 正是为"单测给真 PID 发信号"
    那次事故（差一步停掉线上 worker）设的 —— 本判据不需要、也不许碰真进程。
    """
    from src.scheduler import worker as wk

    wk.run_record_path().write_text(json.dumps({
        "schema": 1, "pid": 4242, "role": "worker", "env": "pilot",
        "started_at_iso": "2026-09-30 14:03:11",
        "clean_exit": False, "exit_code": None, "finished_at": None,
    }, ensure_ascii=False), encoding="utf-8")
    # 心跳停在同一 pid 上：它是"死在什么时候"唯一的时间锚点（原生崩溃不写别的）
    hb.write_once(role="worker", jobs=[], pid=4242, path=sched_dir / hb.HEARTBEAT_FILE)

    rep = wk.last_run_report(run_ended=True)       # 锁空着 ⇒ 写记录的进程已经不在了
    assert rep["state"] == "vanished", rep
    assert rep["needs_attention"] is True
    assert rep["pid"] == 4242
    assert rep["started_at_iso"] == "2026-09-30 14:03:11"
    assert rep["exit_code"] is None, "原生崩溃拿不到退出码 —— 不许编一个出来"
    assert rep["last_beat_at_iso"], "心跳属于同一个 pid 时必须给出最后心跳时刻"

    # ── "下一次启动"：启动日志里必须出现这句结论（值守就是从日志看到的）──
    caplog.set_level(logging.WARNING, logger="src.scheduler.worker")
    assert _run_worker_once() == 0
    assert "异常终止" in caplog.text, f"启动了却没有报告上次异常终止：{caplog.text}"
    assert "4242" in caplog.text and "2026-09-30 14:03:11" in caplog.text, caplog.text


def test_a_corrupt_record_is_not_reported_as_a_crash(sched_dir: Path) -> None:
    """★★ 判据 ⑥（**防误报**）：记录读不懂 ⇒ 说「无法判定」，**不说**"异常终止"。

    与心跳那条同因（`test_corrupt_heartbeat_is_reported_as_unreadable_not_dead`）：
    半写/被手工改过的文件**没有**携带"进程是怎么结束的"这个信息 ——
    把它读成崩溃就是凭空造一条警报，而"无法判定"让人去看文件，方向完全不同。
    """
    from src.scheduler import worker as wk

    wk.run_record_path().write_text('{"pid": 4242, "clean_exit":', encoding="utf-8")
    rep = wk.last_run_report(run_ended=True)
    assert rep["state"] == "unreadable", rep
    assert rep["needs_attention"] is False, "读不懂被判成了崩溃 ⇒ 凭空一条假警报"
    assert "无法判定" in rep["note"], rep["note"]


def test_loop_lag_monitor_is_really_started_in_the_worker_startup_sequence(
    worker_startup: object, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★ 判据 ③：worker 启动序列里 `loop_lag` **真的被启动**，且带 worker 标签。

    这是**行为**判据，不是"源码里有这一行"：
      · `start` 用替身记下标签，再委托**真实现**（真建一个 1 Hz 采样任务）；
      · 收尾时 `stop()` **只在那个任务真的存在时**才 `flush(force=True)`
        （`stop()` 第一句就是 `if _TASK is None: return`）⇒ 断言"收尾真的 flush 了"
        等价于断言"启动真的把任务建起来了"，只写一行而没生效是过不了的。
    """
    from src.core import loop_lag

    started: list[str] = []
    flushed: list[bool] = []
    real_start, real_flush = loop_lag.start, loop_lag.flush

    def _spy_start(*, tag: str = loop_lag.TAG_API) -> bool:
        started.append(tag)
        return real_start(tag=tag)

    def _spy_flush(*, force: bool = False) -> None:
        flushed.append(force)
        real_flush(force=force)

    monkeypatch.setattr(loop_lag, "start", _spy_start)
    monkeypatch.setattr(loop_lag, "flush", _spy_flush)

    assert _run_worker_once() == 0
    assert started == [loop_lag.TAG_WORKER], (
        f"worker 启动序列没有（或没有按 worker 标签）启动循环延迟监控：{started}")
    assert True in flushed, (
        "关停时没有 flush ⇒ 监控任务根本没起来（`stop()` 在 `_TASK is None` 时直接返回）")
    # ★ 标签是**进程级单例**，不许活过监控本身：实测过它的代价 —— 留着 worker 标签
    # 会让**同一进程里后面的**（根本没有 worker 的）代码按 worker 版理由落盘，
    # 于是一条"卡顿理由必须点名在飞处理器"的判据毫无道理地变红。
    assert loop_lag.process_tag() == loop_lag.TAG_API, (
        "监控已经停了，进程标签却还留在 worker 上（会污染同进程里后续的落盘与判据）")


def test_selfproof_removing_the_clean_mark_turns_criterion_one_red(
    worker_startup: object, monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """★★ **自证**：把"标记正常结束"这一步去掉，判据 ① 必须**变红**。

    为什么判据自己要带这一条：标记法只有两半 —— "启动时写未标记记录"与
    "正常收尾时划掉"。如果只有前半也照样绿（例如判读时把"没有标记"读成了"正常"），
    这套取证就是一件**永远不响的仪器**（假护栏比没有护栏更糟）。
    这里让两半在**同一段启动+收尾**下成对出现，唯一差别就是那一句标记：
      · 标记在位 ⇒ `clean`（判据 ① 的绿）；
      · 标记被去掉 ⇒ `vanished`（判据 ① 的红）—— 而"标记没写成"正是原生崩溃的磁盘形状。
    """
    from src.scheduler import worker as wk

    # ① 标记在位：正常路径
    assert _run_worker_once() == 0
    assert wk.last_run_report(run_ended=True)["state"] == "clean"

    # ② 把"标记正常结束"这一步去掉（崩溃进程根本走不到它）
    def _no_mark(**_kw: object) -> bool:
        return False

    monkeypatch.setattr(wk, "finish_run", _no_mark)
    assert _run_worker_once() == 0
    raw = json.loads(wk.run_record_path().read_text(encoding="utf-8"))
    assert raw["clean_exit"] is False and raw["finished_at"] is None, (
        "标记竟然还是写进去了 —— 这条自证就没在测它想测的东西")
    assert wk.last_run_report(run_ended=True)["state"] == "vanished", (
        "少了标记却被读成正常 ⇒ 判据 ① 永远不会变红，取证等于没装")

    # ③ 紧接着的一次启动：序列里读到的上一条必须是**异常终止**（判据 ① 翻红）
    caplog.set_level(logging.WARNING, logger="src.scheduler.worker")
    caplog.clear()
    assert _run_worker_once() == 0
    assert "异常终止" in caplog.text, f"少了标记却没报异常：{caplog.text}"


def test_a_running_worker_with_an_unmarked_record_is_not_reported_as_crashed(
    worker_startup: object, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★★ 判据 ④（**防误报**）：worker 正在跑时，未标记的记录**不是**崩溃。

    这一条防的是最难发现的一种错：记录在进程活着的**全程**都是未标记的
    （标记只在收尾那一刻写）。若判读只看"文件里没有标记"，那么**每一次正常运行**
    都会被报成「上次异常终止」—— 天天喊狼的告警，下场一定是被人关掉
    （本项目的取值纪律：**宁可不报，也不要误报**）。
    所以判据必须从外面来：**锁被持着** ⇒ 那次运行还在跑 ⇒ 不报。
    """
    import manage
    from src.scheduler import worker as wk

    class _HeldLock:
        """锁被持着（操作系统级证据：有人真的在跑）。"""

        holder = {"pid": 4242}

        def __init__(self, *_a: object, **_kw: object) -> None:
            pass

        def acquire(self, **_kw: object) -> bool:
            return False

        def release(self) -> None:
            pass

    monkeypatch.setattr(manage, "list_our_worker_pids", lambda: [])
    monkeypatch.setattr(hb, "read_status", lambda **_kw: _no_heartbeat())
    monkeypatch.setattr(wl, "WorkerLock", _HeldLock)
    wk.run_record_path().write_text(json.dumps({
        "schema": 1, "pid": 4242, "role": "worker", "env": "pilot",
        "started_at_iso": "2026-09-30 14:03:11",
        "clean_exit": False, "exit_code": None, "finished_at": None,
    }, ensure_ascii=False), encoding="utf-8")

    present, why = manage.worker_present()
    assert present is True and "锁" in why, why
    assert "异常终止" not in why, f"正在跑的 worker 被判成了崩溃（假警报）：{why}"

    # 只读判读在"不知道它结束没结束"时**不许猜**（`run_ended=None`）
    rep = wk.last_run_report()
    assert rep["needs_attention"] is False, rep
    assert rep["in_progress"] is True, "本次还在跑却没有标出来"


def test_worker_present_carries_the_verdict_and_is_silent_when_it_was_clean(
    worker_startup: object, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★ 判据 ⑤：结论必须接到 `manage.py` 的存活报告里，且正常时**一个字都不多说**。

    为什么"正常时不说话"也是判据的一半：附注一旦常驻（每次 `status`/值守都印），
    它就从"事故线索"退化成"背景噪音"，而噪音里的真警报没人会看
    —— 与"陈旧停止文件必须被忽略"是同一类纪律。
    """
    import manage
    from src.scheduler import worker as wk

    class _FreeLock:
        """锁空着（真实现会在 `acquire()` 里写持有者信息，判据不需要）。"""

        holder: dict = {}

        def __init__(self, *_a: object, **_kw: object) -> None:
            pass

        def acquire(self, **_kw: object) -> bool:
            return True

        def release(self) -> None:
            pass

    monkeypatch.setattr(manage, "list_our_worker_pids", lambda: [])
    monkeypatch.setattr(hb, "read_status", lambda **_kw: _no_heartbeat())
    monkeypatch.setattr(wl, "WorkerLock", _FreeLock)
    path = wk.run_record_path()

    # ① 上次异常终止 + 现在确实没人跑 ⇒ 判「不在」，**并把那句结论带出来**
    path.write_text(json.dumps({
        "schema": 1, "pid": 4242, "role": "worker", "env": "pilot",
        "started_at_iso": "2026-09-30 14:03:11",
        "clean_exit": False, "exit_code": None, "finished_at": None,
    }, ensure_ascii=False), encoding="utf-8")
    present, why = manage.worker_present()
    assert present is False, "锁空着、心跳没有、枚举为空 ⇒ 必须判不在（否则永远不去拉起）"
    assert "异常终止" in why and "4242" in why, why

    # ② 上次正常结束 ⇒ 附注**必须为空**（否则每次都喊一次狼，判据会被关掉）
    path.write_text(json.dumps({
        "schema": 1, "pid": 4242, "role": "worker", "env": "pilot",
        "started_at_iso": "2026-09-30 14:03:11",
        "clean_exit": True, "exit_code": 0, "finished_at": 1.0,
    }, ensure_ascii=False), encoding="utf-8")
    present, why = manage.worker_present()
    assert present is False and why == "", f"正常结束却带了附注：{why!r}"
