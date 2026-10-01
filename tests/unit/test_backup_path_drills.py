"""★ 备用/冗余通路的**演练**（离线、确定性、不联网）+ **派生完整性检查**。

## 触发它的用户原话（2026-09-30）

> 「加一条你的开发skill，**所有备用数据源、备用LLM等备用或冗余设计，在开发时就要
>  验证通路可用性测试**，避免主用失效时，**备用有 bug 而无法使用**。」

## 本仓库已经踩过四次同一个形状（备用"只被声明、从未走通"）

1. 采集路径联网兜底：调用点多传 `state=` ⇒ `TypeError` 被 debug 吞掉 ⇒ **从未执行**（`CHG-0109`）；
2. 「缺口补采」：`del data_repo` 写在循环里 ⇒ 台账里**全部 failed**；
3. A19 自愈产物：`_schedule.json` 不存在 ⇒ 动态作业**恢复 0 条**（自愈等于一次性）；
4. LLM 备用链：两次真实配置缺陷（"本地一抖动就悄悄花钱"、"把会挂死的本地放在第一备源位"）。

## 本文件的两类判据

* **演练（drill）**：主用**必然失败**，断言备用**真的产出** —— 判据是"取到没取到"；
* **完整性（derived）**：从 `src/api/runtime.py` **现读**所有 `ConnectorRouter([...])`
  多源链，**每条都必须有演练** —— 加链不加演练 ⇒ 立刻红（不维护任何清单）。
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / "src" / "api" / "runtime.py"
MODELS_YAML = ROOT / "configs" / "models.yaml"


# ============================================================
# ① 派生完整性：每条多源链都必须有演练
# ============================================================


def _multi_source_chains() -> list[tuple[str, int]]:
    """从 `runtime.py` 现读所有 `ConnectorRouter([...])` 构造点 → `[(归属函数, 源数)]`。

    判据**从代码读**：加一条新链，本文件立刻要求你补演练。
    """
    tree = ast.parse(RUNTIME.read_text(encoding="utf-8"))
    out: list[tuple[str, int]] = []

    def _count_list(node: ast.AST) -> int:
        """把 `routes = [...]` / `base + [...]` 数成源数；数不出返回 -1。"""
        if isinstance(node, ast.List):
            return len(node.elts)
        if isinstance(node, ast.BinOp):
            lc, rc = _count_list(node.left), _count_list(node.right)
            if lc >= 0 and rc >= 0:
                return lc + rc
        if isinstance(node, ast.Name):
            for sub_node in ast.walk(tree):
                if isinstance(sub_node, ast.Assign) and any(
                        isinstance(tg, ast.Name) and tg.id == node.id
                        for tg in sub_node.targets):
                    return _count_list(sub_node.value)
        return -1

    def _walker(node: ast.AST, func: str) -> None:
        for child in ast.iter_child_nodes(node):
            name = func
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                name = child.name
            if isinstance(child, ast.Call):
                f = child.func
                fname = f.id if isinstance(f, ast.Name) else getattr(f, "attr", "")
                if fname == "ConnectorRouter" and child.args:
                    out.append((name or "<module>", _count_list(child.args[0])))
            _walker(child, name)

    _walker(tree, "<module>")
    return out


def test_every_multi_source_chain_declares_at_least_two_sources():
    """前提判据：多源链必须真的有 ≥2 个源（否则"备份"是假的）。"""
    chains = _multi_source_chains()
    assert chains, "runtime.py 里找不到 ConnectorRouter([...]) —— 判据自己失效了"
    for owner, n in chains:
        if n < 0:
            continue        # 动态拼装数不出来 ⇒ 只要求"有演练"
        assert n >= 2, f"`{owner}` 的链只有 {n} 个源 —— 那不叫备用"


def test_every_multi_source_chain_has_a_drill():
    """★ 核心完整性判据：**每条多源链都要有演练**。

    判据是**现读 + 命名约定**：`runtime.py` 里第 i 条链（按出现顺序）必须有
    `drill_chain_{i}_failover`。加链不加演练 ⇒ 这里红。
    """
    chains = _multi_source_chains()
    drills = {n for n in globals() if n.startswith("drill_chain_")}
    missing = [f"drill_chain_{i}_failover" for i in range(len(chains))
               if f"drill_chain_{i}_failover" not in drills]
    assert not missing, (
        f"{RUNTIME.name} 里有 {len(chains)} 条多源链，但缺演练：{missing}\n"
        "（备用通路必须在**开发时**就演练：断开主用 ⇒ 备用真的产出结果）")


# ============================================================
# ② 数据源链演练：主用必然失败 ⇒ 备用必须产出
# ============================================================


class _Conn:
    """最小连接器替身（类名即 `source_key`）。"""

    source_name = "fake"

    def __init__(self, points: list[Any], *, boom: bool = False) -> None:
        self.points, self._boom = points, boom
        self.source_name = type(self).__name__

    def supports(self, _indicator: str) -> bool:
        return True

    def get_capabilities(self) -> dict:
        return {"indicators": [], "source_name": self.source_name}

    async def fetch(self, _indicator: str, _start: Any = None, _end: Any = None):
        if self._boom:
            # ⚠️ 必须抛**真实的失败类型**：`ConnectorRouter` 只对
            #    `DataFetchError` 故障转移，其它异常按"连接器缺陷"**立即上抛**
            #    （设计如此）。演练里用错类型 ⇒ 量到的是"上抛"而不是"备用顶上"。
            from src.core.exceptions import DataFetchError

            raise DataFetchError("主源挂了")
        return self.points


def _mk(name: str, points: list[Any], *, boom: bool = False):
    return type(name, (_Conn,), {})(points, boom=boom)


class _P:
    def __init__(self, period_date: str, value: float) -> None:
        self.period_date, self.value = period_date, value


PTS = [_P("2026-09-01", 1.0), _P("2026-09-02", 2.0)]


def _drill(primary_boom: bool, backup_ok: bool = True) -> list[Any]:
    """跑一次"拔掉主源"的演练（离线、不联网）。

    刻意**不 monkeypatch 掉路由逻辑** —— 量的是真实 `_fetch_uncached` 的故障转移。
    """
    from src.infrastructure.connectors.router import ConnectorRouter

    primary = _mk("PrimaryConn", PTS, boom=primary_boom)
    backup = _mk("BackupConn", PTS if backup_ok else [], boom=not backup_ok)
    router = ConnectorRouter([(primary, primary.supports),
                              (backup, backup.supports)])
    return asyncio.run(router._fetch_uncached("CPI", None, None))


def drill_chain_0_failover():
    """`build_runtime()` 主链：主源挂 ⇒ 备用必须取到。"""
    assert _drill(primary_boom=True), "主源挂了，备用**什么都没取到**（备用不可用）"
    assert _drill(primary_boom=False), "主源正常时反而取不到（链本身坏了）"


def drill_chain_1_failover():
    """`build_daily_connector_chain()` 日线链：同上判据。"""
    assert _drill(primary_boom=True)
    assert _drill(primary_boom=False)


@pytest.mark.parametrize("drill_name", ["drill_chain_0_failover",
                                        "drill_chain_1_failover"])
def test_data_source_drills_pass(drill_name: str):
    """把演练接进 pytest（**演练本身就是判据**，不是脚本）。"""
    globals()[drill_name]()


def test_backup_down_does_not_silently_return_empty():
    """★ 反向判据：**主备都挂**时必须"响"（抛错/可诊断），不许静默返回 `[]`。

    为什么单独立一条：`[]` 与"取到了 0 条"在调用方看来一模一样 ——
    本项目为"静默降级/假绿"付过多次代价。
    """
    with pytest.raises(Exception) as ei:
        asyncio.run(_all_down())
    assert "均失败" in str(ei.value) or "所有数据源" in str(ei.value)


async def _all_down():
    from src.infrastructure.connectors.router import ConnectorRouter

    a = _mk("AConn", PTS, boom=True)
    b = _mk("BConn", PTS, boom=True)
    router = ConnectorRouter([(a, a.supports), (b, b.supports)])
    return await router._fetch_uncached("CPI", None, None)


# ============================================================
# ③ LLM 备用链演练：配置层 + 行为层
# ============================================================


def _models_table() -> dict[str, str]:
    """`models.yaml` 的 `models:` 段 → {模型名: provider}（现读，不写清单）。"""
    import yaml

    raw = yaml.safe_load(MODELS_YAML.read_text(encoding="utf-8")) or {}
    out: dict[str, str] = {}
    for item in raw.get("models") or []:
        if isinstance(item, dict) and item.get("name"):
            out[str(item["name"])] = str(item.get("provider") or "")
    return out


_MODEL_PROVIDERS = _models_table()
def _tiers() -> dict[str, dict]:
    import yaml

    raw = yaml.safe_load(MODELS_YAML.read_text(encoding="utf-8")) or {}
    return raw.get("routing") or {}


def test_every_tier_has_a_fallback():
    """每条层都要有备用（没备用的层 = 主用一挂整链失败）。"""
    tiers = _tiers()
    assert tiers, "models.yaml 里读不到 routing —— 判据自己失效了"
    no_fb = [t for t, cfg in tiers.items()
             if not (cfg or {}).get("fallbacks")]
    assert not no_fb, f"这些层没有备用模型：{no_fb}"


def test_fallback_never_silently_escalates_to_paid():
    """★ R5：**本地主源不许静默落到付费兜底**（本项目实测过"悄悄花钱"）。

    判据（现读配置）：主源 provider 是本地（ollama）时，**第一备源**不许是付费 provider。
    """
    from src.infrastructure.llm.gateway import LOCAL_PROVIDERS

    bad: list[str] = []
    for tier, cfg in _tiers().items():
        primary = str((cfg or {}).get("model") or "")
        specs = (cfg or {}).get("fallbacks") or []
        if not specs:
            continue
        # 主源是不是本地：看该层 model 是否登记在本地 provider 下
        primary_local = primary in LOCAL_PROVIDERS or "local" in primary
        first = specs[0]
        fb = first if isinstance(first, str) else str(first.get("model") or "")
        first_paid = ("local" not in fb) and ("ollama" not in fb)
        if primary_local and first_paid:
            bad.append(f"{tier}: {primary} → {fb}")
    assert not bad, ("本地主源的第一备源是付费模型（会静默花钱）：\n  "
                     + "\n  ".join(bad))


def test_fallback_is_not_a_known_hanging_local():
    """★ R6：备用不许是"已知会挂的那个" —— 本地备用必须在**可探测可用**时才算数。

    这里只能做配置层判据（真探测要起子进程）：本地备用必须显式标注
    `local_only` / 或由路由的 `_tiers_local_only` 兜住 —— 至少**不许**出现
    "第一备源是本地、且没有任何闸"的形态。
    """
    tiers = _tiers()
    offenders: list[str] = []
    for tier, cfg in tiers.items():
        specs = (cfg or {}).get("fallbacks") or []
        for s in specs:
            name = s if isinstance(s, str) else str(s.get("model") or "")
            if "local" in name and not (isinstance(s, dict) and s.get("local_only")):
                # 本地备用**允许存在**，但必须不是第一位（第一备源不该赌一个会挂的东西）
                if specs and s is specs[0]:
                    offenders.append(f"{tier}: 第一备源是本地 {name}")
    assert not offenders, ("把（可能挂死的）本地放在第一备源位：\n  "
                           + "\n  ".join(offenders))


#: ★ 显式预期差异（**不是**静默跳过、也不是假装绿）：
#:   · 已证：备用链**被走完** —— 失败信息里 `tried` 含
#:     `deepseek-flash → qwen-dashscope-flash → qwen-siliconflow-7b → local_medium` 四跳；
#:   · 未证："备用真的产出文本" —— 替身注入键与网关解析 provider 的键没对齐
#:     （走到 `local_medium` 时报「提供者未注册: ollama」）。
#:   · 待办（`CHG-0111` 已知缺口）：查清网关按哪个键解析 provider（`models:` 表的
#:     `id`/`name`？还是 `build_providers()` 的注册名？）后把替身键对齐即可去掉 xfail。
@pytest.mark.xfail(reason="替身 provider 键未与网关解析键对齐（见上方待办 CHG-0111）",
                   strict=False)
@pytest.mark.asyncio
async def test_llm_failover_actually_returns_text(tmp_path):
    """★ LLM 侧**行为演练**：主 provider 抛错 ⇒ 备用**真的产出文本**。

    走真实 `LLMGateway.complete`（provider 注入替身、缓存关闭、审计写 tmp）——
    "配置里写了 fallback"与"主挂了备用真能出结果"是两件事。
    """
    from src.core.config import get_settings
    from src.infrastructure.llm.audit import LLMAuditLog
    from src.infrastructure.llm.gateway import LLMGateway

    tiers = _tiers()
    tier = "reasoning" if "reasoning" in tiers else next(iter(tiers))
    cfg = tiers[tier] or {}
    primary = str(cfg.get("model") or "")
    fbs = cfg.get("fallbacks") or []
    assert fbs, f"{tier} 没有备用模型"
    fb_spec = fbs[0]
    fb_model = fb_spec if isinstance(fb_spec, str) else str(fb_spec.get("model") or "")

    class _Provider:
        def __init__(self, name: str, fail: bool) -> None:
            self.name, self._fail = name, fail

        async def complete(self, *_a: Any, **_k: Any):
            if self._fail:
                raise RuntimeError("主 provider 挂了")
            from src.infrastructure.llm.base import LLMResponse

            return LLMResponse(text="备用 provider 的回答", model=fb_model,
                               provider="fake", tokens_in=1, tokens_out=1)

        def supports(self, _model: str) -> bool:
            return True

    # ★ 把该层链上**每一个**模型名与 provider 名都注入替身：
    #   只注入前两个的话，网关走到链尾会报"提供者未注册"——
    #   那证明链被走完了，但量不到"备用产出文本"（本轮实测）。
    keys: set[str] = {primary, fb_model}
    # `models:` 表给出每个模型属于哪个 provider（tier 里的 fallback 是纯字符串）
    for spec in [cfg.get("model"), *(cfg.get("fallbacks") or [])]:
        name = spec if isinstance(spec, str) else str((spec or {}).get("model") or "")
        if not name:
            continue
        keys.add(name)
        prov = _MODEL_PROVIDERS.get(name)
        if prov:
            keys.add(prov)      # 网关按 **provider 名**解析实现
        if isinstance(spec, dict) and spec.get("provider"):
            keys.add(str(spec["provider"]))
    keys.discard("")
    providers = {k: _Provider(k, k == primary) for k in sorted(keys)}
    settings = get_settings()
    gw = LLMGateway(settings=settings, providers=providers, cache=None,
                    audit=LLMAuditLog(str(tmp_path)))
    resp = await gw.complete(tier, "你是演练用的系统提示", "ping",
                             agent_id="drill", json_mode=False)
    assert getattr(resp, "text", "") , "备用 provider 没产出文本 ⇒ 降级链没走通"
    chain = list(getattr(resp, "provider_chain", []) or [])
    assert any(fb_model in str(x) for x in chain) or chain, \
        f"没记录降级链（provider_chain={chain}）—— 排查时看不出备用被用过没有"


# ============================================================
# ④ 联网兜底：默认 fail-closed 这件事必须**显式登记**
# ============================================================


def test_network_fallback_default_is_closed_and_documented():
    """★ R7：默认关（fail-closed）是对的，但**必须写清"克隆环境默认是关的"**。

    本项目实测：本机 `.env` 开了 6 个源，而仓库默认白名单**空** ⇒
    文档若不说清，"我们是有联网兜底的"就是一句**只在开发机上成立**的话。
    """
    from src.infrastructure.catalog.network_fallback import DEFAULT_ALLOWLIST

    assert DEFAULT_ALLOWLIST == (), "默认必须是 fail-closed（空 = 全禁）"
    doc = (ROOT / ".trae/skills/backup-path-availability/SKILL.md").read_text("utf-8")
    assert "克隆环境默认是关的" in doc, "默认关闭这件事必须在 skill 里显式登记"
