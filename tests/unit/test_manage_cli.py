"""统一管理 CLI（manage.py）单测：只测纯函数与临时 socket，不启动真实服务。"""

import re
import socket
import threading
from pathlib import Path

import pytest

import manage


class TestCmdlineRecognition:
    def test_ours_uvicorn_cmdline(self) -> None:
        assert manage.is_our_cmdline(
            r".venv\Scripts\python.exe -m uvicorn src.api.main:app --port 8100"
        )

    def test_ours_backslash_path(self) -> None:
        assert manage.is_our_cmdline("python src\\api\\main")

    def test_rejects_unrelated(self) -> None:
        assert not manage.is_our_cmdline(r"C:\Program Files\node.exe vite")
        assert not manage.is_our_cmdline(None)
        assert not manage.is_our_cmdline("")


class TestTestEnvIsolation:
    def test_env_redirects_all_data_paths(self, tmp_path: Path) -> None:
        env = manage.build_test_env(str(tmp_path))
        assert env["MOSS_ENV"] == "test"
        assert env["SQLITE_PATH"].startswith(str(tmp_path))
        assert env["LLM_AUDIT_DIR"].startswith(str(tmp_path))
        assert env["SCHEDULER_DIR"].startswith(str(tmp_path))
        assert env["LLM_CACHE_ENABLED"] == "false"
        assert env["REDIS_CACHE_ENABLED"] == "false"

    def test_env_has_no_production_data_dir(self, tmp_path: Path) -> None:
        env = manage.build_test_env(str(tmp_path))
        # 所有路径型值必须位于临时目录下，绝不指向生产 data/
        for key in ("SQLITE_PATH", "LLM_AUDIT_DIR", "SCHEDULER_DIR"):
            assert Path(env[key]).parent == tmp_path
        assert not env["SQLITE_PATH"].endswith("moss_finagent.db")


class TestPidFile:
    def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        assert manage.read_pid_file(tmp_path / "nope.pid") is None

    def test_invalid_content_returns_none(self, tmp_path: Path) -> None:
        p = tmp_path / "bad.pid"
        p.write_text("not-a-pid", encoding="utf-8")
        assert manage.read_pid_file(p) is None

    def test_dead_pid_returns_none(self, tmp_path: Path) -> None:
        p = tmp_path / "dead.pid"
        p.write_text("999999", encoding="utf-8")
        assert manage.read_pid_file(p) is None

    def test_current_process_pid(self, tmp_path: Path) -> None:
        p = tmp_path / "live.pid"
        p.write_text(str(__import__("os").getpid()), encoding="utf-8")
        assert manage.read_pid_file(p) == __import__("os").getpid()


class TestPortProbe:
    def test_closed_port(self) -> None:
        free = manage.find_free_port(40000)
        assert manage.port_open("127.0.0.1", free) is False

    def test_listening_port_detected(self) -> None:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", 0))
        port = server.getsockname()[1]
        server.listen(1)
        ready = threading.Event()

        def _accept() -> None:
            ready.set()
            try:
                conn, _ = server.accept()
                conn.close()
            except OSError:
                pass

        t = threading.Thread(target=_accept, daemon=True)
        t.start()
        ready.wait(1)
        try:
            assert manage.port_open("127.0.0.1", port, timeout=1.0) is True
        finally:
            server.close()

    def test_find_free_port_advances(self) -> None:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        port = server.getsockname()[1]
        server.listen(1)
        try:
            free = manage.find_free_port(port)
            assert free > port
        finally:
            server.close()


class TestParser:
    def test_start_defaults(self) -> None:
        args = manage.build_parser().parse_args(["start"])
        assert args.port == 8100
        assert args.replace is False
        assert args.daemon is False

    def test_test_args_passthrough_via_main_parser(self) -> None:
        args, extras = manage.build_parser().parse_known_args(
            ["test", "-q", "tests/unit", "-k", "circuit"]
        )
        assert args.command == "test"
        assert extras == ["-q", "tests/unit", "-k", "circuit"]

    def test_stop_and_status(self) -> None:
        assert manage.build_parser().parse_args(["stop"]).command == "stop"
        assert manage.build_parser().parse_args(["status"]).command == "status"

    def test_worker_and_logs_parsers(self) -> None:
        args = manage.build_parser().parse_args(
            ["worker", "--daemon", "--replace", "--loglevel", "warning"]
        )
        assert args.command == "worker"
        assert args.daemon is True
        assert args.replace is True
        assert args.loglevel == "warning"

        logs = manage.build_parser().parse_args(
            ["logs", "worker", "--lines", "50", "-f"]
        )
        assert logs.command == "logs"
        assert logs.name == "worker"
        assert logs.lines == 50
        assert logs.follow is True


class TestDiagnoseSafeOnClosedPort:
    def test_diagnose_unoccupied(self) -> None:
        free = manage.find_free_port(40000)
        info = manage.diagnose_port(free)
        assert info["occupied"] is False
        assert info["is_ours"] is False
        assert info["pid"] is None


class TestSchedulerScopeLineEvaluatesUnderTargetEnv:
    """启动横幅的调度作用域必须在**目标环境**下求值（`CHG-0112`）。

    ## 它守的是什么（2026-09-30 实测）

    `--env pilot` 只是一个**命令行参数**，父进程里并没有 `MOSS_ENV`。
    横幅若直接 `scheduler_scope_report()`，读到的就是父进程的环境 ——
    `current_env()` 退化成 `'dev'`，于是对 pilot 印出**与事实相反**的一行：

        · 定时任务：39/42 个会触发；因**写权限归属**被裁：…quant_data_sync

    而 pilot **恰恰是行情仓唯一的写者**，这条作业在它上面照跑
    （同一子进程随后自己在日志里印「48/48，env=pilot」，全库裁剪记录 0 条）。
    运营者照这行去"修"，就会把 `warehouse.writer` 改回去 ——
    把 dev 与 pilot 重新变回**两个写者**，正是 `CHG-0087` 要防的事故。

    为什么三个实例里只有 pilot 中招：dev / prod 的"目标环境名"与
    "父进程退化值"算出来**恰好同解**（对 `warehouse` 都只读）。
    所以下面那条 **dev 的反向断言和 pilot 的正向断言一样重要** ——
    修这个 bug 时最容易的错法是"把裁剪整个关掉"。
    """

    def test_pilot_scope_is_not_pruned_and_says_pilot(self) -> None:
        """pilot 是行情仓写者 ⇒ 42/42，且这行字必须自带 `env=pilot`。"""
        line = manage._scheduler_scope_line(manage.pilot_isolation_env())
        assert "env=pilot" in line, f"横幅必须自证档位，实际：{line!r}"
        assert "quant_data_sync" not in line, (
            "pilot 是 warehouse 的唯一写者（data_stores.yaml: warehouse.writer=pilot），"
            f"它的 quant_data_sync 不该被裁 —— 实际印出：{line!r}"
        )
        assert "没有作业因写权限归属被裁" in line

    def test_dev_scope_still_prunes_quant_data_sync(self) -> None:
        """★ 反向判据：dev 按裁定是只读，修完必须**仍然**被裁。

        没有这一条，"修复"可以退化成"把裁剪关掉"，而那样测试照样全绿 ——
        然后 dev 与 pilot 又会在同一分钟同写一个 14.36 GiB 的库。
        """
        line = manage._scheduler_scope_line(manage.dev_isolation_env())
        assert "env=dev" in line
        assert "quant_data_sync" in line, (
            f"dev 不是写者，quant_data_sync 必须被裁 —— 实际印出：{line!r}"
        )

    def test_no_overlay_reproduces_the_parent_env_bug(self) -> None:
        """空 overlay 复现旧的无参行为 ⇒ 证明那个参数**真的在起作用**。

        若哪天有人把 `extra_env` 改成可选并传空，这条会红；
        它同时也是"父进程没有 MOSS_ENV"这一前提的活文档。
        """
        line = manage._scheduler_scope_line({})
        assert "env=dev" in line, "父进程无 MOSS_ENV ⇒ current_env() 退化成 dev"
        assert "quant_data_sync" in line, "这正是那条与事实相反的旧结论"

    def test_scope_line_callers_pass_the_target_env(self, monkeypatch) -> None:
        """★ 收尾判据：`_prepare_environment` 里**每一处**调用都传了目标环境。

        这是本缺陷的真实形态 —— 判据函数本身是对的，错的是**调用点**
        （"我改了" → "它生效了"之间的第二道门：有没有人正确地调它）。
        所以按语法树取调用实参，而不是搜字符串。
        """
        import ast
        import inspect
        import textwrap

        src = inspect.getsource(manage._prepare_environment)
        tree = ast.parse(textwrap.dedent(src))
        calls = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_scheduler_scope_line"
        ]
        assert calls, "`_prepare_environment` 里应当有调度作用域横幅"
        for call in calls:
            assert len(call.args) == 1, (
                f"第 {call.lineno} 行的 `_scheduler_scope_line()` 没传目标环境 —— "
                "它会退回读父进程的 os.environ，于是 --env pilot 印出 dev 的结论"
            )
            assert isinstance(call.args[0], ast.Name), (
                f"第 {call.lineno} 行传的不是一个环境字典变量"
            )


class TestSchedulerScopeLineCountsRuntimeRegistry:
    """横幅的分母必须是**运行时**的作业数，不是静态 import 到的那个数（`CHG-0129`）。

    ## 它守的是什么（2026-09-30 实测，重启 pilot 时当场量到）

    静态 `JOB_REGISTRY` = **43**；应用启动时 `install_catalog_jobs()` 再动态注册
    **6** 个（`catalog_daily/weekly/monthly/quarterly/calendar` + `gap_drain`）
    ⇒ 调度器实际跑 **49** 个。同一分钟里两份自述是**打架**的：

    | 谁在说 | 说的什么 |
    |---|---|
    | `manage.py start` 横幅 | `定时任务（env=pilot）：43/43 个会触发（没有作业因写权限归属被裁）` |
    | 那个子进程自己的日志 | `进程内Cron调度器已启动（49/49个作业，环境=pilot）` |

    横幅那行**读起来是一句完整的健康结论** —— 分子分母相等、还声明"没有作业被裁"，
    所以没人会去怀疑它；而它**少报了 6 个作业**，其中就包括 `gap_drain`
    （我两次把它误判成"死分支"，正是因为静态视图里看不见它：`CHG-0125`、`CHG-0128`）。

    ★ 这与 `CHG-0112` 是**同一个形状**，只是换了一层：
    那次的错是"在**父进程的环境**里求值"，这次的错是"在**静态的注册表**里求值"。
    两次都拿到一个看起来很确定的错答案。
    """

    def test_scope_line_counts_dynamically_registered_jobs(self) -> None:
        """横幅的数必须等于"装完动态作业之后"的注册表大小。

        ## 为什么先把动态作业**摘掉**再测（这段是判据能不能证伪的关键）

        本进程里 `JOB_REGISTRY` 是**全局**的，而本文件前面几条判据（还有别的测试）
        也会调用 `_scheduler_scope_line` ⇒ 轮到这条时它**可能已经是 49 了**。
        那样 `total == len(JOB_REGISTRY)` 会因为两边都已是 49 而**恒真** ——
        判据看起来绿，却什么也没证明。
        所以先摘掉、要求横幅**自己把 6 个装回来**：这样"没装"必然露馅。
        """
        from src.scheduler import registry as reg
        from src.scheduler.catalog_jobs import (
            CALENDAR_JOB_NAME,
            GAP_DRAIN_JOB_NAME,
            install_catalog_jobs,
        )

        saved = dict(reg.JOB_REGISTRY)
        try:
            # 先问出"哪些是动态注册的" —— **用 `dry_run=True`**：
            # 它只返回计划、**不写注册表**，所以无论本进程之前有没有人调用过
            # 横幅（那些调用已经把 6 个装进去了），这里都能拿到确定的名单。
            # （我第一版没用 dry_run，于是"新增集合"是空的 —— 判据自己先红了，
            #   这正是它该做的事：非空自证挡住的第一个错误是判据自己的。）
            dynamic_names = {p.name for p in install_catalog_jobs(dry_run=True)}
            dynamic_names |= {CALENDAR_JOB_NAME, GAP_DRAIN_JOB_NAME}
            assert dynamic_names, (
                "`plan_jobs()` 没给出任何作业 ⇒ 这条判据失去意义"
                "（它要防的正是「横幅看不见动态注册的作业」）"
            )
            assert GAP_DRAIN_JOB_NAME in dynamic_names, (
                f"`gap_drain` 应当在动态名单里，实际 {sorted(dynamic_names)}"
            )

            # 摘掉它们：模拟"横幅只看静态注册表"的旧行为
            for name in dynamic_names:
                reg.JOB_REGISTRY.pop(name, None)
            static_total = len(reg.JOB_REGISTRY)

            line = manage._scheduler_scope_line(manage.pilot_isolation_env())
            runtime_total = len(reg.JOB_REGISTRY)

            missing = sorted(dynamic_names - set(reg.JOB_REGISTRY))
            assert not missing, (
                f"横幅没有把动态注册的作业装回来：缺 {missing} —— "
                "它读的是静态 `JOB_REGISTRY`，于是分母少报，"
                "而那行字看起来仍是一句完整的健康结论"
            )
            assert runtime_total == static_total + len(dynamic_names), (
                f"静态 {static_total} + 动态 {len(dynamic_names)} ≠ 实际 {runtime_total}"
            )
            m = re.search(r"：(\d+)/(\d+) ", line)
            assert m, f"横幅这行的格式变了，判据取不到分子分母：{line!r}"
            active, total = int(m.group(1)), int(m.group(2))
            assert total == runtime_total, (
                f"横幅的分母 {total} ≠ 运行时作业数 {runtime_total} —— "
                f"少报的正是动态注册的那些（含 `gap_drain`）。实际印出：{line!r}"
            )
            assert active == total, "pilot 是 warehouse 的唯一写者，不该有作业被裁"
        finally:
            reg.JOB_REGISTRY.clear()
            reg.JOB_REGISTRY.update(saved)

    def test_scope_line_never_exceeds_runtime_registry(self) -> None:
        """反向：横幅不许报一个**比真实注册表还大**的数（分母只能来自注册表）。"""
        from src.scheduler import registry as reg

        line = manage._scheduler_scope_line(manage.dev_isolation_env())
        m = re.search(r"：(\d+)/(\d+) ", line)
        assert m, line
        total = int(m.group(2))
        assert total == len(reg.JOB_REGISTRY), (
            f"横幅分母 {total} ≠ 注册表 {len(reg.JOB_REGISTRY)}：{line!r}"
        )
        assert "env=dev" in line


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
