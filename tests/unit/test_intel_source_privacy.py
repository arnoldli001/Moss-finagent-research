"""数据源防泄漏 —— **面向 F12 的回归测试**。

## 威胁模型

用户打开 F12 → Network，看接口响应；或 F12 → Sources，看前端 bundle。
目标是：**从浏览器侧无论如何都拿不到数据源是谁**。

这跟日志脱敏（`test_redaction` 那类）是**两件事**：
日志脱敏挡的是运维/审计侧，本文件挡的是**用户侧**。
响应体是直接发给浏览器的，日志脱敏对它完全无效。

## 为什么要有这个文件

"某天有人给 `IntelItem` 加了个字段，顺手带出了 URL" —— 这类回归
靠代码评审是拦不住的（字段名太像无害的）。用测试把契约钉死：

  1. `to_public()` 的输出键集合是**白名单**，多一个就失败；
  2. 递归扫描输出值，出现 URL / ID 形态就失败；
  3. 断言 `FORBIDDEN_PUBLIC_KEYS` 一个都没出现。

对"泄漏即失去壁垒"的资产，测试比文档可靠。
"""

from __future__ import annotations

import json
import re

import pytest

from src.infrastructure.connectors.intel_sources import (
    FORBIDDEN_PUBLIC_KEYS,
    IntelItem,
    health,
)

#: 允许出现在响应里的键。**新增字段必须显式加到这里**，
#: 否则测试失败 —— 这是刻意的摩擦，逼人就"这个字段能不能给用户看"做一次决定。
ALLOWED_PUBLIC_KEYS: frozenset[str] = frozenset({
    "kind", "kind_label", "title", "summary", "published_at",
    "source_alias", "codes", "industry", "rating_origin", "agency",
    "content_hash", "extra",
})

#: 值层面的泄漏形态
_URL_RE = re.compile(r"https?://", re.I)
#: 知识星球群组/主题 ID 形态（8 位以上纯数字）
_BIG_ID_RE = re.compile(r"(?<!\d)\d{8,}(?!\d)")


def _item() -> IntelItem:
    """一条**故意塞满敏感信息**的样本。"""
    return IntelItem(
        kind="broker_report",
        title="某公司中报点评",
        summary="太平洋：某公司中报点评",
        published_at="2026-08-25",
        source_alias="broker-太平洋",
        codes=["000001"],
        industry="银行Ⅱ",
        rating_origin="买入",
        agency="太平洋",
        content_hash="abc123def456",
        extra={
            # 这些是**内部字段**，绝不能出接口
            "report_url": "https://pdf.dfcfw.com/pdf/H3_xxx.pdf",
            "url": "https://finance.eastmoney.com/a/xxx.html",
            # 这个是安全的
            "forecast": {"2026": {"eps": 2.15, "pe": 5.17}},
        },
    )


def test_public_keys_are_exactly_allowlisted() -> None:
    """输出键必须**恰好**等于白名单 —— 多一个少一个都失败。"""
    pub = _item().to_public()
    got = set(pub)
    extra = set(pub.get("extra") or {})
    assert got == ALLOWED_PUBLIC_KEYS, (
        f"响应键集合变了。多出={got - ALLOWED_PUBLIC_KEYS} "
        f"缺失={ALLOWED_PUBLIC_KEYS - got}\n"
        "新增字段必须先确认它能不能给用户看，再加进 ALLOWED_PUBLIC_KEYS。")
    assert extra <= {"forecast", "rating_change", "period"}, (
        f"extra 里出现了未白名单的键：{extra}")


def test_no_forbidden_key_anywhere() -> None:
    """递归扫描：任何层级都不能出现禁用键。"""
    pub = _item().to_public()
    found: list[str] = []

    def walk(node: object, path: str = "") -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                if str(k).lower() in FORBIDDEN_PUBLIC_KEYS:
                    found.append(f"{path}.{k}")
                walk(v, f"{path}.{k}")
        elif isinstance(node, (list, tuple)):
            for i, v in enumerate(node):
                walk(v, f"{path}[{i}]")

    walk(pub)
    assert not found, f"响应里出现禁用键：{found}"


def test_no_url_in_response_values() -> None:
    """值层面：序列化后全文不能有 URL。

    这是最后一道 —— 即使有人把 URL 塞进某个**允许的键**里（比如 summary），
    也会被这条抓住。
    """
    blob = json.dumps(_item().to_public(), ensure_ascii=False)
    assert not _URL_RE.search(blob), f"响应里出现 URL：{blob[:200]}"
    assert "dfcfw" not in blob and "eastmoney" not in blob


def test_extra_url_is_stripped() -> None:
    """`extra` 白名单确实生效（首版这里漏过，是真实缺陷）。"""
    pub = _item().to_public()
    assert "report_url" not in pub["extra"]
    assert "url" not in pub["extra"]
    assert "forecast" in pub["extra"], "安全键不该被误删"


def test_health_has_no_source_identifier() -> None:
    """健康度接口同样不能泄漏来源。

    `/intel/health` 是前端会轮询的接口 —— 如果它返回
    `{"zsxq_48848484411448": {...}}`，用户一眼就看到了群组 ID。
    """
    blob = json.dumps(health(), ensure_ascii=False)
    assert not _BIG_ID_RE.search(blob), f"健康度里出现长数字 ID：{blob}"
    assert not _URL_RE.search(blob)


def test_alias_carries_no_raw_id() -> None:
    """来源假名不含真实 ID，且长度可控。"""
    from src.core.redaction import source_pseudonym

    gid = "48848484411448"
    alias = source_pseudonym(gid)
    assert gid not in alias
    assert alias.startswith("src-")
    assert len(alias) <= 14


#: 构造点写过的所有**语义明文**别名（这些绝不能出现在接口响应里）。
#:
#: 首版 `to_public` 直接透传 `source_alias`，于是一按 F12 就能看到
#: `newswire-em` / `policy-cctv` —— 等于把"我用了哪几个免费渠道"
#: 直接交出去。那正是本项目的壁垒。
#: ⚠️ 原来的隐私测试**只测了 `source_pseudonym()` 这个 helper 本身**，
#: 没测 `to_public()` 的实际输出，所以这个泄漏一直存在而测试全绿。
_LEAKY_ALIASES = (
    "newswire-em", "newswire-ths", "newswire-sina", "policy-cctv",
    "broker", "broker-太平洋", "zsxq",
)


def test_to_public_pseudonymises_source_alias() -> None:
    """**核心回归**：`to_public()` 输出里的 `source_alias` 必须是假名。

    测的是**实际输出**，不是 helper —— 泄漏就发生在"helper 写好了但
    没人调用"这个缝隙里。
    """
    from src.infrastructure.connectors.intel_sources import IntelItem

    for raw in _LEAKY_ALIASES:
        pub = IntelItem(kind="newswire", title="t", summary="s",
                     published_at="2026-01-01", source_alias=raw).to_public()
        alias = pub["source_alias"]
        assert alias.startswith("src-"), (
            f"source_alias 未过假名：{raw!r} → {alias!r}")
        assert raw not in alias, f"假名里仍含明文：{alias!r}"
        for token in ("em", "ths", "sina", "cctv", "zsxq", "太平洋"):
            assert token not in alias, (
                f"假名泄漏渠道线索 {token!r}：{raw!r} → {alias!r}")


def test_source_alias_pseudonym_is_stable() -> None:
    """假名要**稳定**：同一来源恒等，否则前端按源去重/统计会失效。"""
    from src.infrastructure.connectors.intel_sources import IntelItem

    a = IntelItem(kind="newswire", title="t1", summary="s",
                  published_at="2026-01-01",
                  source_alias="newswire-em").to_public()["source_alias"]
    b = IntelItem(kind="newswire", title="t2", summary="s",
                  published_at="2026-01-01",
                  source_alias="newswire-em").to_public()["source_alias"]
    c = IntelItem(kind="newswire", title="t3", summary="s",
                  published_at="2026-01-01",
                  source_alias="newswire-sina").to_public()["source_alias"]
    assert a == b, "同一来源的假名必须稳定"
    assert a != c, "不同来源必须得到不同假名（打码成 *** 会让去重失效）"


def test_summary_is_clipped_per_kind() -> None:
    """摘要必须截断 —— 源文本最长 800+ 字，直接下发会把移动端撑爆。"""
    from src.infrastructure.connectors.intel_sources import (
        SUMMARY_MAX_BY_KIND, SUMMARY_MAX_CHARS, IntelItem,
    )

    long_text = "测" * 2000
    for kind in ("newswire", "policy", "broker_report", "research_note",
                 "other"):
        pub = IntelItem(kind=kind, title="t", summary=long_text,
                         published_at="2026-01-01").to_public()
        limit = SUMMARY_MAX_BY_KIND.get(kind, SUMMARY_MAX_CHARS)
        assert len(pub["summary"]) <= limit + 1, (
            f"{kind} 摘要未截断：{len(pub['summary'])} > {limit}")
        assert pub["summary"].endswith("…"), f"{kind} 截断后应有省略号"

    # 短文本不能被改
    short = IntelItem(kind="newswire", title="t", summary="很短",
                         published_at="2026-01-01").to_public()
    assert short["summary"] == "很短"


def test_agency_only_exposed_for_broker_reports() -> None:
    """`agency` 只对研报公开署名；快讯不应从 `agency` 漏出渠道。"""
    from src.infrastructure.connectors.intel_sources import IntelItem

    rpt = IntelItem(kind="broker_report", title="t", summary="s",
                    published_at="2026-01-01",
                    agency="某某证券").to_public()
    assert rpt["agency"] == "某某证券", "研报署名是公开信息，应保留"

    wire = IntelItem(kind="newswire", title="t", summary="s",
                     published_at="2026-01-01",
                     agency="某渠道").to_public()
    assert wire["agency"] == "", "快讯不应通过 agency 泄漏渠道名"


def test_group_id_redacted_in_text() -> None:
    """日志/异常侧：group_id 与接口 URL 都要脱敏。"""
    from src.core.redaction import redact

    gid = "48848484411448"
    for raw in (
        f"/v2/groups/{gid}/topics",
        f"https://api.zsxq.com/v2/groups/{gid}/topics?scope=all",
        f'{{"group_id":"{gid}"}}',
    ):
        out = redact(raw)
        assert gid not in out, f"未脱敏：{raw!r} → {out!r}"


def test_safe_extra_masks_source_keys() -> None:
    """结构化字段：来源类键名一律打码。"""
    from src.core.redaction import safe_extra

    masked = safe_extra({
        "group_id": "48848484411448",
        "author_id": "111855414254442",
        "source_url": "https://api.zsxq.com/x",
        "kind": "newswire",
    })
    assert masked["group_id"] == "***REDACTED***"
    assert masked["author_id"] == "***REDACTED***"
    assert masked["source_url"] == "***REDACTED***"
    assert masked["kind"] == "newswire"


# ======================================================================
# 渗透视角：主动构造失败，试图从错误文本里问出数据源
# ======================================================================

def test_sanitize_error_drops_upstream_url() -> None:
    """**核心防渗透断言**：带 URL 的上游异常不得泄漏地址。

    真实形态（httpx 原样）：
        Client error '403 Forbidden' for url
        'https://api.zsxq.com/v2/groups/48848484411448/topics'
    渗透工具只需触发一次上游失败，就能通过错误回显拿到数据源。
    """
    from src.core.redaction import sanitize_error

    class _UpstreamHTTPError(Exception):
        """类名不含 "http" 子串，避免把类名误判成 URL 泄漏。"""
        pass

    exc = _UpstreamHTTPError(
        "Client error '403 Forbidden' for url "
        "'https://api.zsxq.com/v2/groups/48848484411448/topics?scope=all'")
    out = sanitize_error(exc)

    # 判据要精确，否则误报会让人关掉这道测试：
    #   · 查 "://"（URL 的确定特征）而不是裸 "http" —— 类名里的 HTTP 会误伤
    #   · 查域名与 ID 本体
    for needle in ("://", "zsxq", "48848484411448", "for url"):
        assert needle not in out.lower(), f"错误文本泄漏 {needle!r}：{out}"
    assert "拒绝访问" in out, f"应保留可读原因：{out}"


def test_sanitize_error_handles_common_failures() -> None:
    """超时/连接/限流都要有可读原因，且都不含地址。"""
    from src.core.redaction import sanitize_error

    cases = {
        "ReadTimeout: HTTPSConnectionPool(host='api.zsxq.com', port=443)":
            "超时",
        "ConnectionError: Max retries exceeded with url: /v2/groups/1/topics":
            "连接失败",
        "HTTPError: 429 Too Many Requests":
            "限流",
    }
    for raw, expect in cases.items():
        out = sanitize_error(ValueError(raw))
        assert expect in out, f"{raw!r} → {out!r} 缺 {expect}"
        assert "zsxq" not in out.lower() and "://" not in out, out


def test_sanitize_error_unknown_type_leaks_nothing() -> None:
    """白名单未命中时**只给类型名** —— 宁可少一句线索，也不冒险。"""
    from src.core.redaction import sanitize_error

    out = sanitize_error(RuntimeError(
        "unexpected internal failure at /opt/secret/path/config.yaml"))
    assert "未归类失败" in out
    assert "secret" not in out.lower() and "/opt" not in out


def test_connector_uses_sanitize_error() -> None:
    """连接器失败路径必须走 sanitize_error（防止有人改回 str(exc)）。"""
    import inspect

    from src.infrastructure.connectors import intel_sources

    src = inspect.getsource(intel_sources._run_one)
    assert "sanitize_error" in src, (
        "_run_one 必须用 sanitize_error()；改回 str(exc)/redact() 会让"
        "上游失败文本带出数据源 URL（渗透工具的主要突破口）")


def test_no_upstream_domain_in_code_strings() -> None:
    """连接器模块的**代码字符串**里不该有上游域名。

    只扫 string literal（含 docstring）之外的**可执行代码**常量 ——
    文档字符串里举"泄漏长什么样"的例子是必要的，不该被判违规。
    这里用 AST 取出所有字符串常量，再排除纯注释/文档场景：
    实际上更实用的判据是"域名字符串没有被赋给任何变量或用于请求"，
    但那需要数据流分析 —— 折中做法是**只允许它出现在 docstring 里**。
    """
    import ast
    from pathlib import Path

    from src.infrastructure.connectors import intel_sources

    path = Path(intel_sources.__file__)
    tree = ast.parse(path.read_text(encoding="utf-8"))

    # 收集所有 docstring 节点（模块/类/函数的第一个 Expr 常量）
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef,
                             ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None) or []
            if body and isinstance(body[0], ast.Expr) and \
                    isinstance(body[0].value, ast.Constant) and \
                    isinstance(body[0].value.value, str):
                docstrings.add(id(body[0].value))

    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in docstrings:
                continue
            low = node.value.lower()
            if any(d in low for d in ("zsxq.com", "api.zsxq", "wx.zsxq")):
                offenders.append(f"L{node.lineno}: {node.value[:60]}")

    assert not offenders, (
        "连接器**代码字符串**里出现上游域名（docstring 除外）：\n  "
        + "\n  ".join(offenders))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
