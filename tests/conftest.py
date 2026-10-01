"""共享fixtures。

不用pytest内置tmp_path：本机环境下其目录清理机制单次耗时30-60s
（已实测任意测试+tmp_path必现，与pytest版本/目录位置无关），
改用tempfile.mkdtemp自管理，实测<0.01s。
"""

import os
import pathlib
import shutil
import tempfile
from collections.abc import Generator

import pytest


@pytest.fixture
def tmp_dir() -> Generator[str, None, None]:
    """快速临时目录（str路径），测试结束自动清理。"""
    d = tempfile.mkdtemp(prefix="moss_finagent_test_")
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def repo(tmp_dir):
    from src.infrastructure.repositories.macro_repo import MacroRepository

    return MacroRepository(db_path=os.path.join(tmp_dir, "test.db"))


@pytest.fixture(autouse=True)
def _isolate_health_caches(tmp_dir):
    """把数据健康度的**落盘缓存**重定向到临时目录（autouse，所有测试生效）。

    ## 为什么必须是 autouse（2026-09-23 实测事故）

    `test_data_health_survives_broken_warehouse` 会：
    1. 把 `MOSS_DB_URL` 设成一个假的不可达 MySQL（`nobody@127.0.0.1:1/none`）；
    2. `invalidate_cache(include_disk=True)` —— **删掉生产的**
       `data/quant/warehouse_stats.json`；
    3. `build_data_health(force=True)` —— 把"仓库不可用(mysql)"这个结论
       **写进同一个生产路径**。

    后果不是"测试脏了"而是**正在运行的服务被带坏**：跑一次测试，
    服务在接下来 10 分钟里把本地 14GB 的 SQLite 仓库报成"MySQL 不可用"，
    选股、健康度面板、同步缺口判定全部据此降级 —— 静默，且看起来像环境问题
    （我是先看到 `sync_gap` 里 8 个数据集全变 `unknown` 才顺藤摸到这里的）。

    做成 autouse 而不是只修那一个用例：**将来任何新增测试**只要碰
    `build_data_health(force=True)` 都会写这个文件，靠"记得自己清理"是防不住的。
    """
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("MOSS_HEALTH_CACHE_DIR", tmp_dir)
    # 模块级常量在 import 时求值，所以还要把已导入的那份改掉
    import importlib

    try:
        module = importlib.import_module("src.api.data_health")
    except ImportError:  # 少数测试环境可能没装 api 依赖
        module = None
    if module is not None:
        monkeypatch.setattr(module, "_HEALTH_CACHE_DIR", tmp_dir, raising=False)
        module.invalidate_cache()
    yield
    monkeypatch.undo()


@pytest.fixture(autouse=True)
def _isolate_intel_item_store(tmp_dir, monkeypatch):
    """把**券商作文留存**（`item_store`）重定向到临时目录（autouse，所有测试生效）。

    ## 为什么必须 autouse（2026-09-26 实测）

    `build_feed` 现在每轮都会 `item_store.persist(本轮取到的)`，并且把留存里的
    内容**并回**这一页的结果。于是出现了这样一条链路：

        `test_intel_vocab` 里某个用例直接调真实的 `build_feed`
        → 知识星球那条路真发了一次请求
        → 我的留存把**几十条真实研报**写进了仓库里的
          `data/intel/zsxq_items.jsonl`
        → 之后**每一个**调 `build_feed` 的用例都会把这些真实条目并进来
        → 8 条与这次改动完全无关的用例集体失败，
          失败信息是"多出来一批真实标题"（`test_intel_feed_window_group`
          的窗口断言、`test_intel_vocab` 的高亮断言……）

    这不是"测试脏了"，而是**测试之间通过一个生产文件互相串数据**：
    失败信息指向的是一堆与用例无关的研报标题，排查成本极高。

    做成 autouse 而不是只修那几个用例：**将来任何新增测试**只要碰
    `build_feed` 就会读写这个文件，靠"记得自己清理"防不住 ——
    与 `_isolate_health_caches` 是同一类问题、同一类修法。
    """
    from src.domain.intel import item_store

    # ⚠️ 只改**默认根**（`root=None`，也就是 `build_feed` 走的那条）：
    # 显式传 `root=` 的调用方（存储层自己的用例）仍然按它指定的路径走 ——
    # 否则"测试指定了 root 却写到别处"会让那些用例失去意义。
    _real = item_store.store_path
    monkeypatch.setattr(
        item_store, "store_path",
        lambda root=None: (pathlib.Path(tmp_dir) / "zsxq_items.jsonl"
                           if root is None else _real(root)))
    item_store.reset_cache()
    yield
    item_store.reset_cache()


@pytest.fixture(autouse=True)
def _no_background_refresh(monkeypatch):
    """默认关掉"后台刷新"：它会在测试进程里**真发网络请求 / 起 akshare 子进程**。

    2026-09-16 实测踩到的坑：`test_light_snapshot_skips_slow_subsystems` 用真实
    `IntradayService` 跑冷启动快照，而板块冷取那时被改成了"放后台拉"，
    于是测试里起了真实子进程；用例结束时 `asyncio.run` 取消任务，子进程的
    stdio 管道却没被收掉，整个 pytest 卡在 53% 十几分钟。
    （同时暴露了"取消路径不 kill 子进程"的生产隐患，已在
    `subproc.run_json_subprocess` 里补上 CancelledError 分支。）

    需要验证后台刷新本身的用例，在自己的用例里 monkeypatch 回来即可。
    """
    from src.intraday import board as board_module
    from src.intraday import service as service_module

    monkeypatch.setattr(
        board_module.BoardContextProvider, "_schedule_refresh",
        lambda self, name, kind="concept": None)
    monkeypatch.setattr(
        service_module.IntradayService, "_schedule_watchlist_refresh",
        lambda self: None)
    yield


@pytest.fixture(autouse=True)
def _isolate_intraday_disk_cache(tmp_dir, monkeypatch):
    """把做T的**落盘热缓存**重定向到临时目录（autouse，所有测试生效）。

    ## 为什么必须是 autouse（2026-09-24 实测踩到两次）

    做T模块有两份"重启后热加载"的落盘缓存，都会被**使用真实服务的用例**写到
    仓库的 `data/cache/intraday/` 里：

    1. **自选概览**（`watchlist(force=True)` → `_persist_watch_snapshot`）：
       热加载按**代码交集**校验，替身返回的 600000/600001/600002 一写进真实
       文件，下次启动就因"与当前自选池无交集"整份丢弃。
       现象：跑完单测重启服务，前端自选分数一直空着（要等 200 秒整表重算）。
    2. **单票完整快照**（`snapshot()` → `_remember_snapshot`）：它是**下次启动
       首屏的第一帧**，用真实服务跑降级快照的用例会留下一份空白图缓存。

    与 `_isolate_health_caches` 同一套路、同一理由：靠"记得自己清理"防不住，
    所以提到根 conftest 做成 autouse。改 `hot_cache.DEFAULT_CACHE_DIR` 即可 ——
    `IntradayService.__init__` 正是从这个模块常量取 `_snapshot_dir` 的
    （构造发生在 patch 之后）。自己指定了缓存目录的用例（`tmp_dir`）不受影响。
    """
    from src.intraday import hot_cache

    monkeypatch.setattr(hot_cache, "DEFAULT_CACHE_DIR", tmp_dir)
    yield


@pytest.fixture(autouse=True)
def _isolate_intel_source_cache(tmp_path):
    """清理情报流的**进程级缓存**并隔离它的**落盘文件**（autouse）。

    ## ① 为什么必须清 `source_cache`

    `source_cache` 是进程级缓存，键是 `fetch_all|<watch>|<date>` 与
    `zsxq_incremental`。而情报流的测试普遍用 `monkeypatch.setattr` 把
    `intel_sources.fetch_all` / `zsxq_incremental.fetch_incremental` 换成
    返回**固定假数据**的替身：

        monkeypatch.setattr(
            "src.infrastructure.connectors.intel_sources.fetch_all", _fake)

    缓存一留着，第二个用例就拿不到自己的假数据 —— 它命中上一个用例写下的
    那份（**替身已经被撤销**，数据却还在），于是断言全部对不上。
    实测：不加这条夹具时 intel 相关用例 **25 个失败**，且失败信息全是
    "内容/条数不对"，看起来像业务逻辑坏了，实际只是缓存串了用例。

    ## ② 为什么还要清 `_FEED_CACHE` 并隔离两个落盘文件

    ★ 实测事故（2026-09-26）：`test_intel_feed_nonblocking` 用假数据替换
    `build_feed`，而 `_build_feed_bg` **成功重建后会落盘一份 payload**
    （给下次启动热加载）。于是那次运行的**假条目真被写进了生产文件**
    `data/cache/intel/feed_payload.json`，而服务下次启动把它当"已有数据"
    加载了 —— 用户会在页面上看到"假条目 / 假摘要"。

    只隔离 `source_cache` 挡不住这条链（它走 `_FEED_CACHE` → 落盘）。
    所以三样一起处理：清内存缓存 + 两个落盘路径都指到 `tmp_path`。
    要验证落盘本身的用例自己传 `path=`（见 `test_intel_prewarm`）。

    ## ③ 慢聚合缓存（日历/热度）也必须隔离

    同一类事故的第二个入口：`/intel/calendar` 与 `/intel/heat` 现在也落盘
    （`prewarm.save_slow`，小时级 TTL）。用例只要用假数据打这两个端点，
    就会把假日历/假热榜写进 `data/cache/intel/slow_*.json`，下次**服务启动**
    直接把它当真实日程读出来 —— 与上面 `feed_payload.json` 完全同型的
    生产污染。所以 `DEFAULT_SLOW_DIR` 一并指到 `tmp_path`。

    另外要把 `_SLOW_BUILDING` / `_SLOW_TASKS` 清掉：它们藏着上一用例
    起的后台续期任务，任务体里持有假数据的闭包，跨用例存活会写脏缓存。
    """
    from src.api.routes import intel as intel_route
    from src.domain.intel import prewarm, source_cache

    def _clear_feed_cache() -> None:
        with intel_route._FEED_LOCK:            # noqa: SLF001
            intel_route._FEED_CACHE.clear()     # noqa: SLF001
            intel_route._FEED_BUILDING.clear()  # noqa: SLF001
            intel_route._SLOW_BUILDING.clear()  # noqa: SLF001
            intel_route._SLOW_TASKS.clear()     # noqa: SLF001

    source_cache.reset()
    _clear_feed_cache()
    orig_state = prewarm.DEFAULT_STATE_PATH
    orig_payload = prewarm.DEFAULT_PAYLOAD_PATH
    orig_slow_dir = prewarm.DEFAULT_SLOW_DIR
    prewarm.DEFAULT_STATE_PATH = tmp_path / "prewarm_state.json"
    prewarm.DEFAULT_PAYLOAD_PATH = tmp_path / "feed_payload.json"
    prewarm.DEFAULT_SLOW_DIR = tmp_path / "slow"
    try:
        yield
    finally:
        prewarm.DEFAULT_STATE_PATH = orig_state
        prewarm.DEFAULT_PAYLOAD_PATH = orig_payload
        prewarm.DEFAULT_SLOW_DIR = orig_slow_dir
        source_cache.reset()
        _clear_feed_cache()


@pytest.fixture(autouse=True)
def _isolate_intraday_notify_throttle(tmp_dir, monkeypatch):
    """把做T的**推送节流记录**重定向到临时目录（autouse，所有测试生效）。

    ## 为什么必须是 autouse（与上面两条同一套路、同一理由）

    `notify_dedup` 把"今天哪只票已经通知过几次"落在
    `data/cache/intraday/notified_signals.json`。任何构造真实 `SignalNotifier`
    并走到推送判定的用例，都会读到/写到**生产实例正在用的同一个文件**。

    后果比"测试脏了"严重：跑一次测试就写入
    "2026-09-24 688668 已通知 4 次" —— 而生产实例按同一份记录判上限，
    于是用户当天**真的收不到通知**了，且看不出任何异常
    （和 `_isolate_health_caches` 那次的"把生产仓库报成 MySQL 不可用"同类）。

    注意必须同时改 `DEFAULT_PATH` 与清进程内缓存：前者是模块级常量
    （import 时求值），后者是 `_state` 里的 memo，只改一个都不够。
    """
    from src.intraday import notify_dedup

    monkeypatch.setattr(notify_dedup, "DEFAULT_PATH",
                        pathlib.Path(tmp_dir) / "notified_signals.json")
    notify_dedup.reset()
    yield
    notify_dedup.reset()


@pytest.fixture(autouse=True)
def _reset_settings_cache():
    """每个用例前后清掉 `get_settings()` 的**进程级缓存**（autouse，全局）。

    ## 为什么必须是 autouse（2026-09-30 实测：一个用例把后面的用例全弄红）

    `get_settings()` 是 `@lru_cache` 的**进程内单例**。于是任何用例只要在
    "别人的环境变量"下构建过一次 `Settings`，那份**属于另一个环境**的配置
    就会留在进程里，直到有人手工 `cache_clear()` —— 而以前只有**部分文件**
    在自己的 fixture 里记得清（实测 14 个文件有，其余没有）。

    现场：`test_scheduler_role_split.py` 的夹具把 `MOSS_ENV` 设成 `pilot`（那是
    它必须锚定的环境），而 `worker_heartbeat.heartbeat_path()` 在没有
    `SCHEDULER_DIR` 时会回落到 `get_settings()` ⇒ 一份 **pilot** 的 `Settings`
    被缓存住 ⇒ 之后 `test_warehouse_write_ownership.py` 的四个 `/health` 用例
    被当成**公网实例**（`LoginGateMiddleware` 按 `is_public` 强制登录）
    ⇒ 全部返回 **401**、四条判据同时红，而它们**单独跑全绿**。
    复现命令（一跑就红，修好后必须绿）：

        python -m pytest tests/unit/test_scheduler_role_split.py \
                         tests/unit/test_warehouse_write_ownership.py -q

    ## 为什么修在 conftest 而不是只修那个夹具

    "记得清缓存"与"记得 patch 生产进程"是**同一类**纪律（见
    `_forbid_real_process_signals`）：写在文档里会被忽略，写成判据的不会。
    做成 autouse 之后，**任何**用例（含以后新增的）都不可能把环境泄漏给下一个。

    判据：`tests/unit/test_test_harness_isolation.py`。
    """
    from src.core.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _isolate_llm_spend_ledger(tmp_path_factory, monkeypatch):
    """把**跨进程 LLM 花费账本**指到临时目录（autouse，全局）。

    ## 为什么必须 autouse（2026-10-01，与 `_reset_settings_cache` 同一类）

    生产单例 `get_budget()` 会打开 `data/run/llm_spend-YYYY-MM-DD.jsonl`
    作为"当日全局花费"的事实源（`DaySpendLedger`）。测试若不去隔离：

      · 跑测试的机器上**真实花过的钱**会进到 `remaining` / 硬闸判断里
        ⇒ 判据的通过与否取决于"今天线上花了多少"，那是**不可复现**的红/绿；
      · 反向也危险：测试写进真实账本 ⇒ 污染线上运维页的当日花费。

    所以每个用例都给一个**独立临时目录**。判据
    `tests/unit/test_test_harness_isolation.py`（与 settings 缓存同一条纪律：
    "记得隔离"写在文档里会被忽略，写成 autouse 的不会）。

    显式设成目录（而不是空串）：空串在实现里表示"关闭账本"，
    而这里要的是"换一个地方"，两者语义不同 —— 混用会让
    `ledger_enabled` 这类可见性字段在测试里失去意义。
    """
    from src.core import budget as budget_mod

    monkeypatch.setenv(
        budget_mod.LEDGER_DIR_ENV,
        str(tmp_path_factory.mktemp("llm_spend_ledger")))
    yield


@pytest.fixture(autouse=True)
def _isolate_circuit_registry():
    """每个用例给一份**全新的熔断注册表**（autouse，全局）。

    ## 为什么必须 autouse（2026-10-01，与 `_reset_settings_cache` 同一类）

    注册表是进程级单例、承载**可变熔断状态**。于是"先打满阈值、再断言被拒"
    这类判据的**前提**依赖"这个桶此刻是 CLOSED 初始态" —— 一旦同进程里
    别的用例或应用后台线程碰过同一个桶，那条断言测的就是**用例执行顺序**：

        现场：`test_fallback_wiring.py::test_circuit_breaker_open_refuses`
              合并跑时红过一次（断言「熔断器没有打开 —— 测试前提不成立」），
              单独跑 6 次 + 同命令重跑 5 次全绿。

    这种"合并偶发红"最容易被当成抖动忽略，而它掩盖的是**共享可变单例**
    这一类真问题。做法与账本/设置缓存那两条一致：**结构性隔离，不靠多跑几次**。

    判据：`tests/unit/test_test_harness_isolation.py`。
    """
    from src.infrastructure.llm import circuit_breaker as cb_mod

    cb_mod.reset_circuit_registry_for_test()
    yield
    cb_mod.reset_circuit_registry_for_test()


@pytest.fixture(autouse=True)
def _forbid_real_process_signals(monkeypatch):
    """测试**绝不允许**向活着的进程发信号（autouse，所有测试生效）。

    ## 为什么必须是 autouse（2026-09-30 实测，差一步停掉线上 worker）

    `stop_backend_processes` 为 `CHG-0141` 加上了"并上 worker 枚举"之后，
    四条**只 patch 了后端枚举**的既有用例，在本机真有一个 worker 在跑时
    把 **PID 18880（线上那个 worker）** 当成了停止目标：
    断言先红了，但代码路径**已经走到** `request_graceful_stop(18880)`。

    也就是说：**跑一次单测就可能把线上的重作业进程停掉** —— 而 worker
    没有端口、前端不会报不可达，症状要等到"某个数据不再更新"才浮现
    （正是 `CHG-0141` 加心跳要消灭的那类静默失效）。
    这与本项目反复记的"测试与生产跑在同一台机器上"是同一个根：
    **不能靠"记得 patch"，要靠机器拦。**

    ## 为什么只拦"真的活着"的 PID

    有些用例**故意**要验证真实实现（例如 `request_graceful_stop` 对不存在的
    PID 必须返回 False 而不是抛异常），它们传的是假 PID ⇒ 应当继续走真实分支。
    一律拦住会把那些判据变成"测一个替身"，看起来绿、实际什么都没证明。
    """
    import manage

    orig_kill = manage.kill_pid_tree
    orig_grace = manage.request_graceful_stop

    def _guard(name, orig):
        def _wrapped(pid, *args, **kwargs):
            try:
                alive = manage._pid_alive(int(pid))
            except Exception:  # noqa: BLE001 判不出来就按"危险"处理
                alive = True
            if alive:
                print(f"[测试安全闸] 拒绝对活着的进程发信号：{name}({pid})"
                      " —— 用例应当 monkeypatch 掉这个函数，"
                      "而不是对真实进程下手")
                return False
            return orig(pid, *args, **kwargs)

        return _wrapped

    monkeypatch.setattr(manage, "kill_pid_tree", _guard("kill_pid_tree", orig_kill))
    monkeypatch.setattr(manage, "request_graceful_stop",
                        _guard("request_graceful_stop", orig_grace))
    yield


@pytest.fixture(autouse=True)
def _reset_auth_singletons():
    """每个用例前后重置认证相关的**进程级单例**：图形码 + IP 限流。

    ## 为什么必须是 autouse（2026-09-23 实测，同一个坑踩了第三次）

    `IpRateLimiter` 是按 IP 计数的模块级单例，默认 **300 秒窗口 / 60 次请求**。
    各个测试文件如果只在"自己记得"的夹具里重置它，就会出这样的事：

      · 单跑某个文件 → 全绿；
      · 和另一个登录密集的文件一起跑 → 窗口内累计超过 60 次 →
        后面所有用例的**登录一律 429**，而错误位置在"登录"这一步，
        看起来像密码/会话问题，完全指不到限流器。

    已经因为这一条修过 `test_my_pools_api` / `test_my_profiles_api`，
    这次是 `test_admin_routes`（加了 6 个监控用例、登录次数上去之后就翻了）。
    **逐个文件修是治不住的** —— 任何新增的、会登录的测试都可能再次触发，
    所以提到根 conftest 做成 autouse：所有测试一律从"干净的限制器"开始。

    重置它们没有副作用：两者都只是进程内缓存，用例之间本就不该共享状态。
    """
    from src.domain.auth.human_check import reset_challenge_service
    from src.domain.auth.ratelimit import reset_ip_rate_limiter

    reset_challenge_service()
    reset_ip_rate_limiter()
    yield
    reset_challenge_service()
    reset_ip_rate_limiter()


@pytest.fixture(autouse=True)
def _isolate_collection_anomalies(tmp_dir, monkeypatch):
    """把**采集异常库**重定向到临时目录（autouse，所有测试生效）。

    ## 为什么必须是 autouse（2026-09-30 实测）

    管理员界面「运行指标 → 数据采集异常」区读的就是
    `<run_dir>/collection_anomalies.jsonl`，而 `run_dir` 在
    `configs/data_stores.yaml` 里登记为 **shared**（dev / pilot / test 共用同一个
    目录）。于是**集成测试里故意抛的假错误会被写进生产文件**：

        tests/integration/test_supervisor_graph.py:262
            raise RuntimeError("网络炸了")

    实测后果：那个文件当时 **34 行里有 32 行**是这句假错误产生的 ——
    也就是说**这个面板 94% 是假数据**，而它存在的意义恰恰是"方便维护"。
    (`AGENTS.md`：宁可不显示，也不显示假绿 —— 这里是更糟的一种：
    显示的是**测试的幻觉**，而且看不出是假的。)

    做成 autouse 而不是去修那一个用例：**将来任何新增测试**只要经过
    Supervisor 的采集链就会再污染一次，而它**不报错**（与上面几条 autouse
    同一个理由 —— 逐个文件修是治不住的）。

    ⚠️ 同时清掉进程内去重表 `_SEEN`（`DEDUP_WINDOW_S=1800`）：
    不清的话，用例 A 记过的 `(kind, indicator, reason[:60])`
    会让用例 B 的同一条**静默不写** —— 表现为"我明明记了却没落盘"，
    同一进程跑整个套件时最容易出现。
    """
    import pathlib as _pathlib

    from src.core import collection_anomalies as _ca

    monkeypatch.setattr(_ca, "_path",
                        lambda root=None: _pathlib.Path(tmp_dir) / _ca.FILE_NAME)
    monkeypatch.setattr(_ca, "_SEEN", {})
    monkeypatch.setattr(_ca, "_WARNED_UNKNOWN_KINDS", set())
