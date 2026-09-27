"""客户端真实 IP 的解析与**伪造防护**测试。

## 为什么这些用例值得存在

`client_ip` 是**安全判据**：登录限流计数、图形码触发、审计追溯全靠它。
它原来在**三个文件里各有一份实现**，都只认 `CF-Connecting-IP`：

    src/api/login_gate.py      登录门槛
    src/api/routes/auth.py     登录/注册/找回的限流与审计
    src/api/routes/admin.py    管理操作留痕

两个后果：

1. **三份实现必然分叉**，症状只是"某个接口限流莫名失效" —— 不报错。
2. **切到香港 VPS + nginx 后全盘失效**：那条路没有 `CF-Connecting-IP`，
   所有用户退化成本机地址 = **共用一个限流计数**。

## 钉住的四条

1. CF 形态：`CF-Connecting-IP` 被采信（且**仅公网环境**）。
2. nginx 形态：`X-Forwarded-For` 被采信。
3. ★ **直连方不可信时，任何头部一律无视** —— 否则任何人都能伪造 IP。
4. ★ **不取 XFF 最左段**（那是"客户端自己声称的"，可随意伪造）。
"""

from __future__ import annotations

import pytest
from starlette.requests import Request

from src.core.client_ip import UNKNOWN_IP, client_ip


def _req(peer: str | None, headers: dict[str, str] | None = None) -> Request:
    return Request({
        "type": "http",
        "client": (peer, 12345) if peer else None,
        "headers": [(k.lower().encode(), v.encode())
                    for k, v in (headers or {}).items()],
    })


@pytest.fixture(autouse=True)
def _public_env(monkeypatch: pytest.MonkeyPatch):
    """默认按**公网环境**（pilot/prod）——`CF-Connecting-IP` 只在那里才采信。"""
    from src.core import config as cfg

    real = cfg.get_settings()

    class _S:
        is_public = True
        trusted_proxies = ""

    monkeypatch.setattr(cfg, "get_settings", lambda: _S())
    yield
    monkeypatch.setattr(cfg, "get_settings", real if callable(real) else real)


# ======================================================================
# 形态 ①：Cloudflare Tunnel（现状，保留）
# ======================================================================

def test_cloudflare_header_is_trusted_from_loopback() -> None:
    """cloudflared 从本机发请求 → 采信它覆盖的 `CF-Connecting-IP`。"""
    assert client_ip(_req("127.0.0.1",
                          {"CF-Connecting-IP": "1.2.3.4"})) == "1.2.3.4"


# ======================================================================
# 形态 ②：香港 VPS + frp + nginx（新）
# ======================================================================

def test_xff_is_trusted_from_loopback() -> None:
    """frpc 从本机发请求 → 采信 nginx 添加的 `X-Forwarded-For`。

    这条是"切香港后限流还能正常工作"的全部依据。
    """
    assert client_ip(_req("127.0.0.1",
                          {"X-Forwarded-For": "1.2.3.4"})) == "1.2.3.4"


def test_xff_multi_hop_picks_rightmost_untrusted() -> None:
    """多跳链：`<客户端>, <nginx>` → 取最右的**不可信**地址 = 客户端。

    注意：只有当 nginx 地址在信任清单里时才会跳过它。
    未配置清单时，右侧那一跳本身就是"第一个不可信地址"，返回它是对的
    （保守方向：宁可把它当客户端，也不去信更靠左、可伪造的那一段）。
    """
    got = client_ip(_req("127.0.0.1",
                         {"X-Forwarded-For": "1.2.3.4, 10.0.0.1"}))
    assert got == "10.0.0.1", (
        "未把 10.0.0.1 列入可信代理时，它就是链路端点 —— 返回它"
        "（而不是去信更靠左、可被伪造的 1.2.3.4）")


def test_xff_skips_a_trusted_proxy_hop(monkeypatch: pytest.MonkeyPatch) -> None:
    """把 nginx 加进信任清单后，才跳过它拿到真正的客户端 IP。"""
    from src.core import config as cfg

    class _S:
        is_public = True
        trusted_proxies = "10.0.0.1"

    monkeypatch.setattr(cfg, "get_settings", lambda: _S())
    assert client_ip(_req("127.0.0.1",
                          {"X-Forwarded-For": "1.2.3.4, 10.0.0.1"})) == "1.2.3.4"


# ======================================================================
# ★ 伪造防护（本文件最重要的两条）
# ======================================================================

def test_untrusted_peer_cannot_spoof_via_xff() -> None:
    """★ 直连方不可信 → **无视** XFF，只回直连地址。

    没有这一条，任何人都能发一个 `X-Forwarded-For: 1.2.3.4` 把自己
    "变成"别人 —— 限流按 IP 计数，于是攻击者既能绕过自己的限流，
    又能**把别人打进限流**（拒绝服务）。
    """
    assert client_ip(_req("8.8.8.8",
                          {"X-Forwarded-For": "1.2.3.4"})) == "8.8.8.8"


def test_untrusted_peer_cannot_spoof_via_cf_header() -> None:
    """★ 同理：直连方不可信时，伪造的 `CF-Connecting-IP` 也必须被无视。

    原来的实现**无条件**采信这个头，所以任何能直连服务端口的人
    都能伪造 IP（本地任意程序、以及任何绕过 CF 直连 8110 的路径）。
    """
    assert client_ip(_req("8.8.8.8",
                          {"CF-Connecting-IP": "1.2.3.4"})) == "8.8.8.8"


def test_spoofed_leftmost_segment_is_ignored() -> None:
    """★ 客户端往 XFF 里塞假地址（追加在最左）不能得逞。

    `<伪造>, <真实客户端>` —— 取最右的不可信地址得到的是真实客户端（右侧），
    而不是伪造值。反过来取最左段就等于把伪造权交给攻击者。
    """
    got = client_ip(_req("127.0.0.1",
                         {"X-Forwarded-For": "9.9.9.9, 1.2.3.4"}))
    assert got == "1.2.3.4", f"应取最右段，得到 {got}"


def test_cf_header_only_trusted_in_public_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """★ 本地 dev 直连时**不采信** `CF-Connecting-IP`。

    dev/test 环境没有 CF 在链路上，采信它等于给本机任意程序一个
    伪造 IP 的口子（本地调试时**无限流**，但这个头会进审计日志）。
    """
    from src.core import config as cfg

    class _S:
        is_public = False
        trusted_proxies = ""

    monkeypatch.setattr(cfg, "get_settings", lambda: _S())
    assert client_ip(_req("127.0.0.1",
                          {"CF-Connecting-IP": "1.2.3.4"})) == "127.0.0.1"


# ======================================================================
# 兜底
# ======================================================================

def test_no_headers_falls_back_to_peer() -> None:
    assert client_ip(_req("127.0.0.1")) == "127.0.0.1"


def test_missing_client_does_not_crash() -> None:
    """`request.client` 为 None（某些 ASGI 部署）也不能抛异常。"""
    assert client_ip(_req(None)) == UNKNOWN_IP


def test_three_call_sites_delegate_to_the_shared_implementation() -> None:
    """★ 三个调用点必须**都**委托到同一份实现（不许再各写一份）。

    用源码断言（与 `test_manage_process_lifecycle` 同一手法）：
    三处 `_client_ip` 里都必须出现 `client_ip`，
    且**代码里**不许再直接读 `CF-Connecting-IP`（那意味着又分叉了）。

    ⚠️ 断言只查**代码**、不查 docstring：三处的注释里都提到了
    `CF-Connecting-IP`（那是在解释"为什么不能再自己解析它"），
    把注释一起查会把"解释"误判成"实现"，测试自己先错了。
    """
    import ast
    import pathlib

    for rel in ("src/api/login_gate.py", "src/api/routes/auth.py",
                "src/api/routes/admin.py"):
        source = pathlib.Path(rel).read_text(encoding="utf-8")
        tree = ast.parse(source)
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "_client_ip")
        # 去掉 docstring（第一个语句若是常量表达式），只留可执行代码
        body = fn.body
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)):
            body = body[1:]
        code = "\n".join(ast.unparse(stmt) for stmt in body)
        assert "client_ip(request)" in code, f"{rel} 没有委托到 core.client_ip"
        assert "CF-Connecting-IP" not in code, (
            f"{rel} 的代码里又出现了一份自己的 CF 头解析 —— 那就又分叉了")
