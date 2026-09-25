"""日志与审计的**脱敏** —— 把"禁止泄漏"从文档条款变成代码约束。

## 为什么需要它

`docs/SECURITY_COMPLIANCE.md` 的安全红线写着：

> 禁止在日志中输出用户隐私数据、完整 Prompt 内容、模型原始响应。

但仓库里此前**没有任何脱敏实现** —— 而 `AGENTS.md` 同时要求
"LLM 推理必须记录 prompt_hash"，两件事放在一起就会出现
"为了合规而把完整 prompt 写进日志"的荒诞结果。

本模块提供三个入口，全部是**白名单式**的（不在白名单就按规则脱敏）：

- `redact(text)` —— 文本脱敏：API Key / 证件号 / 手机号 / 邮箱 / 账号
- `redact_prompt(text, *, limit)` —— Prompt 脱敏：只留前缀指纹 + 长度
- `safe_extra(mapping)` —— 结构化字段脱敏：命中敏感键名的值一律打码

## 设计取舍

**脱敏在写入侧，不在读取侧。** 读侧过滤意味着敏感数据已经落盘了，
一次 `cat` 就泄漏。写侧过滤的代价是"事后想分析原文时没有了" ——
所以 `redact_prompt` 保留**前缀**（便于按开头聚类）与**长度**（便于排查异常大 prompt），
只丢弃中间正文。
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Final

#: 值命中这些键名时一律打码（大小写不敏感、子串匹配）
SENSITIVE_KEYS: Final[frozenset[str]] = frozenset({
    "password", "passwd", "pwd", "secret", "token", "api_key", "apikey",
    "authorization", "auth", "credential", "private_key", "access_key",
    "session", "cookie", "id_card", "idcard", "phone", "mobile",
    "email", "bank_card", "account", "position", "holding", "持仓",
    "密码", "令牌", "密钥", "身份证", "手机号", "银行卡",
    # ── 数据源标识（agent 平台核心资产，泄漏即可被复制）──
    # 加这些键名是因为：来源本身是可复制资产 —— 对手拿到 group_id / 公众号名
    # 就能自己去订阅，几百块复制掉整条采集层。详见 docs/INTEL_PERMISSION_DESIGN.md §5。
    "group_id", "groupid", "gid", "chat_id", "channel_id",
    "author_id", "user_id", "uid", "member_id",
    "source_url", "source_id", "feed_id", "webhook", "bot_id",
    "数据源", "群组", "星球", "公众号", "作者",
})

#: 掩码文案。数据源场景给**可追溯的假名**，便于"同源去重"又不暴露是谁。
_MASK: Final = "***REDACTED***"

#: 明显的占位符 —— 出现这些词就不判为敏感内容。
#: 否则 `.env.example` 里的 `sk-REPLACE_ME`、`your-key-here` 会被门禁误报，
#: 逼得贡献者要么关掉门禁、要么写得更隐晦，两种结果都更糟。
_PLACEHOLDER = re.compile(
    r"(?i)(replace[_-]?me|change[_-]?me|your[_-]?|xxx+|example|placeholder|"
    r"dummy|fake|test[_-]?key|<[^>]+>|\{[^}]+\}|你的)")

#: 文本级脱敏规则（顺序敏感：先长后短，避免部分匹配）
_PATTERNS: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}\b")),
    ("bearer", re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._\-]{12,}")),
    ("aws_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    # ── 知识星球：域名 / 接口路径 / 群组与用户 ID ──
    # 这几条要放在最前（先长后短）：一旦 URL 被部分规则先切碎，
    # 后面的域名与 ID 规则就匹配不到了。
    ("zsxq_url", re.compile(
        r"https?://(?:api|wx)\.zsxq\.com[^\s\"'<>）)]*")),
    ("zsxq_group", re.compile(
        r"(?i)(?:/groups?/|group_id[\"'\s:=]+|gid[\"'\s:=]+)(\d{8,})")),
    ("zsxq_topic", re.compile(r"(?i)(?:/topics?/|topic_id[\"'\s:=]+)(\d{6,})")),
    ("zsxq_token", re.compile(
        # 键名锚定，不假设 token 具体格式：实测 zsxq_access_token 是
        # 8-4-4-4-12 的 GUID 变体，但格式可能变；绑在键名上更稳。
        r"(?i)\b(zsxq[_\-]?access[_\-]?token|x-access-token)"
        r"[\"'\s:=]+[A-Za-z0-9\-_.~+/=]{16,}")),
    ("id_card", re.compile(r"\b[1-9]\d{5}(19|20)\d{2}(0[1-9]|1[0-2])"
                           r"(0[1-9]|[12]\d|3[01])\d{3}[0-9Xx]\b")),
    ("phone", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
    ("bank_card", re.compile(r"(?<!\d)\d{16,19}(?!\d)")),
    ("email", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]{2,}\b")),
    ("kv_secret", re.compile(
        r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key)\s*[=:]\s*\S+")),
)


def _is_placeholder(text: str) -> bool:
    return bool(_PLACEHOLDER.search(text))


def _fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()[:12]


def redact(text: object, *, keep: int = 0) -> str:
    """文本脱敏。`keep>0` 时保留前 `keep` 个字符（便于按开头聚类）。"""
    raw = "" if text is None else str(text)
    if not raw:
        return ""
    for _name, pattern in _PATTERNS:
        if pattern.groups:
            raw = pattern.sub(
                lambda m: (f"{m.group(1)}={_MASK}"
                           if m.lastindex and m.group(1).lower() in
                           {"password", "passwd", "pwd", "secret", "token",
                            "api_key", "apikey", "api-key"}
                           else _MASK),
                raw)
        else:
            raw = pattern.sub(_MASK, raw)
    if keep and len(raw) > keep:
        return f"{raw[:keep]}…（已脱敏）"
    return raw


def redact_prompt(text: object, *, prefix: int = 80,
                  limit: int = 120) -> str:
    """Prompt 专用脱敏：**只保留前缀指纹 + 长度 + 内容哈希**，不落正文。

    这是"要审计 LLM 调用、又不能把 prompt 写进日志"的折中：
    - 前缀（默认 80 字）足以按开头聚类、认出是哪个 Agent 的哪类任务；
    - 哈希足以判断"两次调用是不是同一 prompt"（缓存归因要用）；
    - 长度足以发现"prompt 异常膨胀"这类故障。
    """
    raw = "" if text is None else str(text)
    if not raw:
        return ""
    head = redact(raw[:prefix])
    return (f"[len={len(raw)} hash={_fingerprint(raw)}] "
            f"{head}{'…' if len(raw) > limit else ''}")


def source_pseudonym(source_id: object, *, prefix: str = "src") -> str:
    """数据源标识 → **稳定假名**（`src-3f9a2c1b`）。

    这是数据源保密的核心工具：内部要用 `group_id` 做去重、限流、增量判断，
    但**接口响应里绝不能出现真实 ID**。用哈希假名同时满足两件事：

    - **稳定**：同一来源永远得到同一个假名 → 去重、限流、按源统计都成立；
    - **不可逆**：只暴露 8 位哈希，无法反推是哪个星球/公众号。

    ⚠️ 为什么不是"打码成 ***"：那样所有来源会变成同一个值，
    去重与限流直接失效（表现为"所有来源被当成同一个源"，
    是一类很难查的静默故障）。假名保留区分度、去掉可识别性。
    """
    raw = "" if source_id is None else str(source_id).strip()
    if not raw:
        return ""
    return f"{prefix}-{hashlib.sha256(raw.encode('utf-8', 'ignore')).hexdigest()[:8]}"


def safe_extra(mapping: dict[str, Any] | None) -> dict[str, Any]:
    """结构化字段脱敏：命中 `SENSITIVE_KEYS` 的值打码，其余原样。"""
    if not mapping:
        return {}
    out: dict[str, Any] = {}
    for key, value in mapping.items():
        lowered = str(key).lower()
        if any(token in lowered for token in SENSITIVE_KEYS):
            out[key] = _MASK
            continue
        out[key] = redact(value) if isinstance(value, str) else value
    return out


__all__ = [
    "SENSITIVE_KEYS",
    "assert_no_secret",
    "redact",
    "redact_prompt",
    "safe_extra",
    "source_pseudonym",
]


def assert_no_secret(text: str, *, context: str = "") -> None:
    """**门禁用**：文本里出现未脱敏的密钥形态时抛错。

    CI 的合规门禁会拿它扫 `.env.example` 与日志样本；也让
    "我不小心打了个 key" 在测试期就暴露。

    明显占位符（`REPLACE_ME` / `your-key` / `<TOKEN>` / `{env}`）不判违规 ——
    否则会把贡献者逼到"关掉门禁"或"写得更隐晦"，两种结果都更糟。
    """
    for name, pattern in _PATTERNS:
        for match in pattern.finditer(text):
            if _is_placeholder(match.group(0)):
                continue
            where = f"（{context}）" if context else ""
            raise AssertionError(
                f"检测到未脱敏的敏感内容 {name}{where}: {match.group(0)[:40]!r}")


__all__ = [
    "SENSITIVE_KEYS",
    "assert_no_secret",
    "redact",
    "redact_prompt",
    "safe_extra",
    "source_pseudonym",
]
