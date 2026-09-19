"""东财 SNI 阻断规避机制的单测（离线：不真发网络请求）。

要钉住的四件事：
  1. **默认关闭**：不为一个必然失败的请求白加探测/重试开销；
  2. 开启后装的壳**仍是 `requests.Session` 的子类**（`isinstance` 成立、上下文
     管理可用），否则会打断 akshare 等第三方库的内部判断；
  3. 只对 `eastmoney.com` 域名、且只在**命中阻断特征**时才回退 ——
     正常报错（如 404 抛出的 HTTPError）绝不能被当成阻断；
  4. 探测不到可用 IP 时**如实抛出**，不伪造数据。
"""

from __future__ import annotations

import pytest
import requests

from src.core import eastmoney_direct as em


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    """每个用例前后恢复原状（含探测缓存与已安装的壳）。"""
    original = requests.Session
    monkeypatch.setattr(em, "_probe_result", {}, raising=False)
    yield
    requests.Session = original
    em._enabled_flag = False


def test_default_off_and_install_is_noop(monkeypatch) -> None:
    monkeypatch.setattr(em, "_enabled_flag", False)
    assert em.enabled() is False
    assert em.install() is False
    assert requests.Session.__name__ == "Session"
    assert not getattr(requests.Session, "_moss_em_direct", False)


def test_install_is_idempotent_and_subclass(monkeypatch) -> None:
    monkeypatch.setattr(em, "_enabled_flag", True)
    original = requests.Session
    assert em.install() is True
    assert em.install() is False, "重复安装必须幂等"
    session = requests.Session()
    assert isinstance(session, original), "壳必须是原生 Session 的子类"
    assert session.adapters, "原生属性（连接池）要照常可用"
    with requests.Session() as ctx:
        assert ctx is not None


def test_only_eastmoney_hosts(monkeypatch) -> None:
    assert em._is_eastmoney("push2his.eastmoney.com") is True
    assert em._is_eastmoney("push2.eastmoney.com") is True
    assert em._is_eastmoney("d.10jqka.com.cn") is False
    assert em._is_eastmoney("web.ifzq.gtimg.cn") is False
    # 防止"用后缀伪造"绕过域名判断
    assert em._is_eastmoney("eastmoney.com.evil.test") is False


@pytest.mark.parametrize("message,expected", [
    ("ConnectionError: ('Connection aborted.', RemoteDisconnected(...))", True),
    ("SSLError: EOF occurred in violation of protocol", True),
    ("HTTPError: 404 Client Error", False),
    ("JSONDecodeError: Expecting value", False),
    ("KeyError: 'data'", False),
])
def test_block_detection_does_not_swallow_normal_errors(message, expected) -> None:
    class Fake(Exception):
        pass

    assert em._looks_blocked(Fake(message)) is expected


def test_ipv4_addresses_filters_and_dedups(monkeypatch) -> None:
    """只保留 IPv4，且去重、保序（阻断正是发生在域名→IPv6 那条路上，v6 没用）。"""
    class Row:
        def __init__(self, family, address):
            self.family = family
            self.address = address

        def __getitem__(self, index):        # 特殊方法必须在类上定义
            if index == 0:
                return self.family
            if index == 4:
                return (self.address, 443)   # sockaddr：第 [0] 位是 IP
            return None

    rows = [Row(em.socket.AF_INET, "1.1.1.1"), Row(em.socket.AF_INET6, "::1"),
            Row(em.socket.AF_INET, "1.1.1.1"), Row(em.socket.AF_INET, "2.2.2.2")]
    monkeypatch.setattr(em.socket, "getaddrinfo", lambda *a, **k: rows)
    assert em.ipv4_addresses("push2his.eastmoney.com") == ["1.1.1.1", "2.2.2.2"]


def test_no_working_ip_raises_instead_of_faking(monkeypatch) -> None:
    """探测不到可用 IP 时必须抛出（绝不返回空数据或伪造 data）。"""
    monkeypatch.setattr(em, "_enabled_flag", True)
    monkeypatch.setattr(em, "ipv4_addresses", lambda host: ["10.0.0.1"])
    monkeypatch.setattr(em, "_resolve_direct_ip", lambda *a, **k: None)

    class FakeSession(em.EastmoneyDirectSession, requests.Session):
        def request(self, method, url, **kwargs):  # noqa: ANN001, ANN003
            raise requests.exceptions.ConnectionError(
                "('Connection aborted.', RemoteDisconnected('Remote end closed'))")

    session = FakeSession()          # 走真实 __init__，保证 cookies/headers 等齐全
    with pytest.raises(requests.exceptions.ConnectionError):
        session.request("GET", "https://push2his.eastmoney.com/api/qt/x")


def test_probe_report_is_a_copy() -> None:
    em._probe_result.clear()
    em._probe_result["push2.eastmoney.com"] = {
        "ip": "1.2.3.4", "candidates": ["1.2.3.4"], "at": 1.0}
    report = em.probe_report()
    report["push2.eastmoney.com"]["ip"] = "tampered"
    assert em.probe_report()["push2.eastmoney.com"]["ip"] == "1.2.3.4"


def test_install_reads_env_flag(monkeypatch) -> None:
    """环境变量语义：只有显式 1/true/yes/on 才开启（默认 0）。"""
    for raw, expected in (("1", True), ("true", True), ("on", True),
                          ("0", False), ("", False), ("off", False)):
        monkeypatch.setenv("MOSS_EM_DIRECT", raw)
        value = em.os.environ.get("MOSS_EM_DIRECT", "0").strip().lower() in (
            "1", "true", "yes", "on")
        assert value is expected
