"""★ 配置**真的生效**了吗：`.env` → Settings → 消费者（2026-09-29 实测的"改了个寂寞"）。

## 现场

用户口径变更要开启"联网兜底"，我在 `.env` 写了
`MOSS_NETWORK_FALLBACK_ALLOWLIST=...`（6 个免费源），可是端到端日志里仍然是：

```
联网兜底已装配：白名单=（空 → fail-closed，不允许任何兜底）
采集路径联网兜底被护栏拒绝(个股告警:600036)：[ALLOWLIST_EMPTY]
```

**看起来像护栏在正常工作，其实是开关压根没接上。** 根因：
`pydantic-settings` 只把 `.env` 读进 **Settings 对象**，**不写回 `os.environ`**，
而 `allowlist_from_env()` 读的是 `os.environ`。

## 本文件守的三件事

1. `os.environ` 缺值时**回落到 Settings**（`.env` 能生效）；
2. 显式传入的 `env` 字典**优先**（测试/预检用，不受 Settings 影响）；
3. 两边都没有 → **fail-closed**（空 = 全拒），不因为读不到配置而"默认放行"。
"""

from __future__ import annotations

import pytest

from src.core.config import get_settings
from src.core.intel_limits import QUERY_DEADLINE_SEC, query_deadline_sec
from src.infrastructure.catalog.network_fallback import (
    ALLOWLIST_ENV,
    DEFAULT_ALLOWLIST,
    allowlist_from_env,
)


def test_allowlist_falls_back_to_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★ `os.environ` 没有 → 从 Settings 读（`.env` 那一份）。"""
    monkeypatch.delenv(ALLOWLIST_ENV, raising=False)
    monkeypatch.setattr(
        get_settings(), "network_fallback_allowlist",
        "AkshareConnector,TencentDailyConnector", raising=False)
    assert allowlist_from_env() == ("AkshareConnector", "TencentDailyConnector")


def test_explicit_env_argument_still_wins() -> None:
    """显式传字典时不看 Settings（预检/测试要能完全控制输入）。"""
    assert allowlist_from_env({ALLOWLIST_ENV: "OnlyThisOne"}) == ("OnlyThisOne",)
    assert allowlist_from_env({}) == DEFAULT_ALLOWLIST


def test_missing_everywhere_is_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """两边都没有 → **空白名单 = 全拒**（绝不因为读不到配置就放行）。"""
    monkeypatch.delenv(ALLOWLIST_ENV, raising=False)
    monkeypatch.setattr(get_settings(), "network_fallback_allowlist", "",
                        raising=False)
    assert allowlist_from_env() == ()


def test_non_ascii_entries_are_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """白名单只认机器可读标识（中文标签会随文案漂移，绑上去等于把花钱判据绑在文案上）。"""
    assert allowlist_from_env(
        {ALLOWLIST_ENV: "AkshareConnector,腾讯财经"}) == ("AkshareConnector",)


def test_deadline_reads_from_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★ 防撞钟同理：`.env` 里的 `MOSS_QUERY_DEADLINE_SEC` 必须真的生效。"""
    monkeypatch.delenv("MOSS_QUERY_DEADLINE_SEC", raising=False)
    monkeypatch.setattr(get_settings(), "query_deadline_sec", "7.5", raising=False)
    assert query_deadline_sec() == 7.5


def test_deadline_falls_back_to_default_on_garbage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """非法值（空/非数字/≤0）→ 模块默认，不抛。"""
    for bad in ("", "abc", "0", "-3"):
        monkeypatch.setattr(get_settings(), "query_deadline_sec", bad,
                            raising=False)
        assert query_deadline_sec() == QUERY_DEADLINE_SEC, bad
