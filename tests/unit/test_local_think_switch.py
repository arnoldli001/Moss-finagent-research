"""本地模型**思维链开关**的护栏（2026-09-28 新增）。

## 为什么这条必须钉住：它是"掷硬币"的根因

`qwen3` / `qwen3.5` 是**思考型**模型，思考 token **计入** `num_predict`。
实测（真实 prompt = `tone.build_prompt` + `tone.extraction_schema()`，
`num_predict=2048`，各 3 次）：

| 模型 | 默认（开思考） | `think=false` |
|---|---|---|
| `qwen3:8b` | p50 **18.3s** · 输出 729 tok · 键齐全 100% | p50 **9.1s** · 321 tok · 键齐全 100% |
| `qwen3.5:4b` | p50 31.4s · **空正文 100%**（2048 全被思考吃掉） | p50 **4.8s** · 184 tok · 键齐全 100% |

上层看到的形态是"**模型返回空内容**"——与随机故障一模一样，所以它被长期
误诊成"8GB 显存不够导致的掷硬币"。**显存不是这个病**：4B 权重只占 ~3.4GB
（比 8B 的 5.6GB 宽裕得多），却比 8B 更容易空返回。

## 本文件钉住五件事

1. 默认**关**（`MOSS_LOCAL_THINK` 未设时）—— 默认值即护栏，安全的一侧做默认
2. `MOSS_LOCAL_THINK` 能开回来（可回退），非法值**打 warning 且不静默**
3. `spec.think` 显式值优先（供 A/B 与个别任务回退）
4. **`think` 必须真的进了 HTTP payload** —— 这是最容易"改了但没生效"的一层：
   单测全绿而 payload 里没这个字段，实测就还是 18s / 空返回
5. 云端 provider **不受影响**（`think` 是 Ollama 的顶层参数，灌给 OpenAI
   兼容端点会被拒或忽略 —— 实测踩过的形态是"参数静默失效"）
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.infrastructure.llm.models import ModelSpec  # noqa: E402
from src.infrastructure.llm.providers import (  # noqa: E402
    ENV_THINK,
    OllamaProvider,
    _resolve_local_think,
)


def _spec(**kw) -> ModelSpec:
    base = dict(name="local_medium", provider="ollama",
                model_name="qwen3.5:4b", base_url="http://localhost:11434")
    base.update(kw)
    return ModelSpec(**base)


# ============================================================
# ① 默认关（默认值即护栏）
# ============================================================


def test_default_is_thinking_off(monkeypatch):
    """★ 未设环境变量时必须**关**思考。

    安全的一侧做默认：开思考在 4B 上是 100% 空返回 —— 而"空返回"会被上层
    记成"本地模型不可用"，进而降级到付费云端（悄悄花钱）。所以默认为关。
    """
    monkeypatch.delenv(ENV_THINK, raising=False)
    assert _resolve_local_think(spec=_spec()) is False


@pytest.mark.parametrize("raw,expected", [
    ("1", True), ("true", True), ("ON", True), ("yes", True),
    ("0", False), ("false", False), ("off", False), ("no", False),
])
def test_env_override_is_honoured(monkeypatch, raw, expected):
    """`MOSS_LOCAL_THINK` 必须能把它开回来（回退路径不能是死的）。"""
    monkeypatch.setenv(ENV_THINK, raw)
    assert _resolve_local_think(spec=_spec()) is expected


def test_invalid_env_value_warns_and_falls_back(monkeypatch, caplog):
    """非法值不许静默：打 warning，并按默认（关）处理。"""
    monkeypatch.setenv(ENV_THINK, "maybe")
    with caplog.at_level("WARNING"):
        assert _resolve_local_think(spec=_spec()) is False
    assert any("无法识别" in r.getMessage() for r in caplog.records), (
        "非法值必须留下可查的 warning（静默回退会让人以为开关生效了）")


def test_explicit_spec_value_wins(monkeypatch):
    """`spec.think` > 环境变量（显式调用方最清楚要什么）。"""
    monkeypatch.setenv(ENV_THINK, "1")
    assert _resolve_local_think(spec=_spec(think=False)) is False
    monkeypatch.setenv(ENV_THINK, "0")
    assert _resolve_local_think(spec=_spec(think=True)) is True
    assert _resolve_local_think(spec=_spec(think=None)) is False


# ============================================================
# ② ★ 接线判据：think 必须真的进 payload
# ============================================================


@pytest.mark.asyncio
async def test_think_reaches_the_http_payload(monkeypatch):
    """★ 最关键的一条：**"我改了"不等于"它生效了"**。

    这一层的失败形态特别隐蔽：`_resolve_local_think` 全绿、`ModelSpec`
    也有字段，但 `chat()` 里忘了塞进 payload —— 于是单测全通过、实测还是
    18 秒或空返回。所以这里拦一次**真实的出站请求**，检查 payload 内容。
    """
    captured: dict = {}

    class _Resp:
        status_code = 200

        @staticmethod
        def raise_for_status() -> None:
            return None

        @staticmethod
        def json() -> dict:
            return {"message": {"content": '{"a":1}'},
                    "prompt_eval_count": 5, "eval_count": 6}

    class _Client:
        async def post(self, url, json=None, **kw):  # noqa: A002 贴合 httpx 签名
            captured["url"] = url
            captured["payload"] = json
            return _Resp()

    monkeypatch.delenv(ENV_THINK, raising=False)
    p = OllamaProvider()
    p._client = _Client()  # noqa: SLF001 注入假 httpx 客户端（等价 FakeProvider）

    await p.chat(_spec(), "sys", "user", json_mode=True)
    assert captured["url"].endswith("/api/chat")
    assert "think" in captured["payload"], (
        "think 没进 payload —— 护栏只改了配置，实际请求仍然开着思考"
        "（实测代价：8B 18.3s→9.1s 拿不到、4B 空返回 100%）")
    assert captured["payload"]["think"] is False, "默认必须是关"

    await p.chat(_spec(think=True), "sys", "user", json_mode=True)
    assert captured["payload"]["think"] is True, "显式 True 必须原样下发"


@pytest.mark.asyncio
async def test_cloud_payload_has_no_think_field(monkeypatch):
    """云端 provider 不受影响：`think` 是 Ollama 的顶层参数。

    灌给 OpenAI 兼容端点要么被 400 拒、要么静默忽略 —— 两种都会把
    "本地实验参数"泄漏进云端请求，属于不必要的风险面。
    """
    captured: dict = {}

    class _Resp:
        status_code = 200

        @staticmethod
        def raise_for_status() -> None:
            return None

        @staticmethod
        def json() -> dict:
            return {"choices": [{"message": {"content": "{}"}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1}}

    class _Ctx:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, **kw):  # noqa: A002
            captured["payload"] = json
            return _Resp()

    import httpx

    from src.infrastructure.llm.providers import OpenAICompatProvider

    monkeypatch.setenv(ENV_THINK, "1")   # 就算开了，也不该漏到云端
    prov = OpenAICompatProvider(name="dashscope", base_url="https://x/v1",
                                api_key_env="X_KEY", api_key="k")
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _Ctx())
    await prov.chat(_spec(provider="dashscope", name="qwen-dashscope-flash"),
                    "sys", "user", json_mode=True)
    assert "think" not in captured["payload"], (
        "云端 payload 里出现了 think —— 这是 Ollama 专有参数")


def test_think_field_defaults_to_auto():
    """`ModelSpec.think` 默认 None（= 按环境变量解析），不是硬编码 False。

    为什么要留 None：将来若要给某个**确实需要思考**的本地任务单独开，
    必须能在不改全局默认的前提下做到。
    """
    assert _spec().think is None
    assert _spec(think=True).think is True
    assert _spec(think=False).think is False


# ============================================================
# ③ 静默截断告警（`_warn_if_prompt_truncated`）
# ============================================================


def test_truncation_warning_fires_on_a_capped_prompt(caplog):
    """★ 提示词被后端截断时必须出声 —— 否则"没抽全"没有任何线索。

    实测依据（`qwen3.5:4b`，`num_ctx=4096`）：12000 字正文（12761 字符 prompt）
    只处理了 **2050 token** 就封顶，而 Ollama **不报错**；
    `prompt_eval_count` 是唯一的证据，而生产链路原先没人读它。
    """
    from src.infrastructure.llm.providers import _warn_if_prompt_truncated

    prompt = "国" * 12000          # 12000 字符
    with caplog.at_level("WARNING"):
        _warn_if_prompt_truncated(_spec(), "", prompt, 2050)
    assert any("截断" in r.getMessage() for r in caplog.records), (
        "12000 字符只处理 2050 token，必须告警")


def test_truncation_warning_is_silent_on_a_normal_prompt(caplog):
    """★ 反面：生产上限（600 字 → 795 token）**不许**告警。

    实测口径：600 字正文 + 骨架 = 1379 字符 → 795 token（≈0.58 token/字符）。
    判据取 0.3 作保守下限，正常路径必须安静 —— 否则天天误报，真报也看不见。
    """
    from src.infrastructure.llm.providers import _warn_if_prompt_truncated

    with caplog.at_level("WARNING"):
        _warn_if_prompt_truncated(_spec(), "", "国" * 1379, 795)
    assert not [r for r in caplog.records if "截断" in r.getMessage()], (
        "生产上限的正常 prompt 不该告警（会变成噪音）")


def test_truncation_warning_needs_both_sides_to_be_measurable(caplog):
    """判不了就不报：「没量到」≠「量到 0」。"""
    from src.infrastructure.llm.providers import _warn_if_prompt_truncated

    with caplog.at_level("WARNING"):
        _warn_if_prompt_truncated(_spec(), "", "", 0)          # 空 prompt
        _warn_if_prompt_truncated(_spec(), "", "国" * 1000, 0)  # 后端没给计数
    assert not caplog.records, "读数缺失时不该告警"
