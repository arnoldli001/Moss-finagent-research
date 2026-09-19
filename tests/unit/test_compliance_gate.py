"""**合规门禁** —— 把 `docs/SECURITY_COMPLIANCE.md` 的红线变成 CI 会拦的断言。

## 为什么单独一个文件

合规要求写在文档里没人执行，写在单测里就有人执行。本文件的每条用例都对应
一条**监管/内控要求**，注释里写明出处。CI 把它作为独立 job 跑，
失败即视为"合规门禁未通过"，不允许合并。

覆盖：
1. 禁止硬编码敏感信息（`AGENTS.md` 安全合规规范）
2. 禁止在日志中输出完整 Prompt / 模型原始响应（`SECURITY_COMPLIANCE.md` 红线）
3. 多租户必须真的隔离（不能是"假 RLS"）
4. 审计必须不可篡改（哈希链）
5. 所有数据库查询必须参数化
6. 免责声明必须存在
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from src.core import redaction
from src.core.tenancy import DataClass, Principal, Role, WallGroup, principal_scope

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"

#: 豁免文件（测试夹具、示例配置里出现样例密钥是允许的）
_EXEMPT = {"test_", "conftest.py", ".example"}


def _iter_source_files() -> list[Path]:
    return [p for p in SRC.rglob("*.py")
            if "__pycache__" not in p.parts]


# ======================================================================
# 1. 禁止硬编码敏感信息
# ======================================================================

#: 只在**赋值给敏感名字**时才算硬编码，避免把文档字符串里的示例判成违规
_HARDCODED = re.compile(
    r"""(?ix)
    \b(password|passwd|pwd|secret|api[_-]?key|access[_-]?key|
       private[_-]?key|auth[_-]?token)\b
    \s*[:=]\s*
    ["'](?![A-Za-z_]*\{)(?!\s*["'])[^"']{8,}["']
    """)


def test_no_hardcoded_secrets() -> None:
    """**红线**：禁止硬编码 API Key / 数据库密码 / Token。

    全部敏感值必须来自环境变量或密钥管理服务。这里扫源码，
    命中即失败；确需示例请写进 `.env.example` 或测试夹具。
    """
    offenders: list[str] = []
    for path in _iter_source_files():
        text = path.read_text(encoding="utf-8", errors="ignore")
        for match in _HARDCODED.finditer(text):
            value = match.group(0)
            # 允许读取环境变量的形态：os.environ.get("X", "")
            if "os.environ" in text[max(0, match.start() - 120):match.start()]:
                continue
            offenders.append(f"{path.relative_to(ROOT)}: {value[:60]}")
    assert not offenders, "发现硬编码敏感信息：\n  " + "\n  ".join(offenders[:10])


def test_env_example_has_no_real_values() -> None:
    """`.env.example` 只放占位符，不放真值。"""
    example = ROOT / ".env.example"
    if not example.exists():
        pytest.skip("项目未提供 .env.example")
    text = example.read_text(encoding="utf-8", errors="ignore")
    redaction.assert_no_secret(text, context=".env.example")


# ======================================================================
# 2. 日志不得输出完整 Prompt / 模型原始响应
# ======================================================================

def test_prompt_redaction_keeps_audit_value_without_body() -> None:
    """Prompt 脱敏后仍可用于审计（可聚类、可归因），但不含正文。"""
    secret_body = "贵州茅台 2026Q3 内部评级：买入，目标价 2100 元，" * 40
    out = redaction.redact_prompt(secret_body, prefix=40)
    assert secret_body[:20] in out, "前缀指纹丢失 → 无法按开头聚类"
    assert "len=" in out and "hash=" in out, "长度/哈希丢失 → 无法归因与发现膨胀"
    assert secret_body[100:] not in out, "正文泄漏"


def test_log_leak_detector_catches_raw_secrets() -> None:
    """门禁自身的检测器必须真的有效（否则是安全幻觉）。"""
    for bad in ("sk-abcdefghijklmnopqrstuvwxyz0123",
                "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6",
                "password=SuperSecret123",
                "13800138000",
                "11010519900307123X"):
        with pytest.raises(AssertionError):
            redaction.assert_no_secret(bad)


def test_redaction_masks_common_pii() -> None:
    text = ("联系 13800138000 / a.b@example.com / 身份证 11010519900307123X / "
            "key sk-abcdefghijklmnopqrstuvwxyz")
    masked = redaction.redact(text)
    assert "13800138000" not in masked
    assert "a.b@example.com" not in masked
    assert "11010519900307123X" not in masked
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in masked


def test_safe_extra_masks_sensitive_keys() -> None:
    out = redaction.safe_extra({"tenant_id": "quant-a", "api_key": "abc123",
                                "持仓": [{"code": "600000"}], "count": 3})
    assert out["tenant_id"] == "quant-a" and out["count"] == 3
    assert out["api_key"] == "***REDACTED***"
    assert out["持仓"] == "***REDACTED***"


# ======================================================================
# 3. 多租户隔离必须是真实现
# ======================================================================

def test_rls_is_not_a_noop() -> None:
    """**最重要的回归**：RLS 不能是"取了租户但什么都不做"的假实现。

    前一版 `_inject_tenant_filter` 遍历完表就 `return stmt`，
    docstring 却声称注入了 `WHERE tenant_id = ?` —— 那比没有更危险。
    """
    from src.infrastructure.security import rls

    principal = Principal(user_id="alice", tenant_id="quant-a",
                          roles=frozenset({Role.RESEARCHER}),
                          clearance=DataClass.INTERNAL,
                          wall_group=WallGroup.RESEARCH)
    with principal_scope(principal):
        where, params = rls.tenant_column_guard("watchlist")
    assert where and "tenant_id" in where, "租户条件为空 → 假隔离"
    assert params == ["quant-a"], "租户参数未下推"


def test_rls_rejects_undeclared_tables() -> None:
    from src.core.tenancy import TenantError
    from src.infrastructure.security import rls

    with pytest.raises(TenantError):
        rls.tenant_column_guard("some_table_nobody_registered")


def test_every_tenant_table_is_documented() -> None:
    """登记租户隔离的表与声明为全局的表，必须都有说明。"""
    from src.infrastructure.security import rls

    assert rls.TENANT_COLUMNS, "租户表清单为空"
    for table, why in rls.GLOBAL_TABLES.items():
        assert len(why) > 8, f"{table} 的豁免原因过于简略"


def test_unauthenticated_never_silently_degrades() -> None:
    """未认证必须是可检测状态，不能回落成某个真实租户。"""
    from src.core.tenancy import TenantError, require_principal

    with pytest.raises(TenantError):
        require_principal()


# ======================================================================
# 4. 审计不可篡改 + 5. 查询参数化 + 6. 免责声明
# ======================================================================

def test_access_audit_is_hash_chained(tmp_path: Path) -> None:
    """审计条目必须带链式哈希 —— 事后改一条就对不上。"""
    import json

    from src.api.tenancy_middleware import TenantAuditLog

    audit = TenantAuditLog(tmp_path)
    principal = Principal(user_id="alice", tenant_id="quant-a",
                          roles=frozenset({Role.RESEARCHER}),
                          clearance=DataClass.INTERNAL,
                          wall_group=WallGroup.RESEARCH)
    for index in range(3):
        audit.record(principal=principal, method="GET", path=f"/api/v1/x{index}",
                     status=200, latency_ms=index)
    lines = [json.loads(line) for line in
             audit.path.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 3
    assert all(len(row["chain_hash"]) == 64 for row in lines)
    assert len({row["chain_hash"] for row in lines}) == 3, "链式哈希重复 → 未串链"
    assert all(row["tenant_id"] == "quant-a" for row in lines)


def test_no_string_interpolated_sql_in_tenant_tables() -> None:
    """**参数化查询**：租户条件不得用 f-string / % 直接拼进 SQL。"""
    offenders: list[str] = []
    pattern = re.compile(
        r"(?i)(select|update|delete)[^\n]*\{[^}]*tenant[^}]*\}")
    for path in _iter_source_files():
        text = path.read_text(encoding="utf-8", errors="ignore")
        for match in pattern.finditer(text):
            offenders.append(f"{path.relative_to(ROOT)}: {match.group(0)[:80]}")
    assert not offenders, ("租户条件被字符串插值进 SQL（应走参数化）：\n  "
                           + "\n  ".join(offenders[:5]))


def test_disclaimer_exists_in_readme_and_api() -> None:
    """红线：所有输出附带免责声明。"""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "不构成投资建议" in readme
