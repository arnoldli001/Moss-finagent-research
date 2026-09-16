"""统一管理 CLI（manage.py）单测：只测纯函数与临时 socket，不启动真实服务。"""

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


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
