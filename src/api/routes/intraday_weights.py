"""做T因子目录 / 个股股性 / 权重档案 API。

接口一览（前缀 `/api/v1`）：
  GET    /intraday/factors                       因子目录 + 权重模板（前端权重编辑器用）
  GET    /intraday/character?code=300308         个股股性画像 + 预填权重/档位
  GET    /intraday/market-cycle                  市场情绪周期（涨停家数/炸板率/连板高度）
  GET    /intraday/weight-profiles               档案列表
  GET    /intraday/weight-profiles/{code}        单只票的档案（含当前生效口径）
  PUT    /intraday/weight-profiles/{code}        保存/更新档案（**写数据库**）
  DELETE /intraday/weight-profiles/{code}        删除档案（幂等）
  POST   /intraday/weight-profiles/preview       用给定权重重算一次总分（保存前预览）

设计取舍：
- 权重**必须合计=100** 才允许入库。前端滑杆会实时显示合计，这里仍做二次校验：
  总分刻度依赖权重合计，一旦存进一个合计=120 的档案，
  这只票的 ±20/±30 阈值就永久失效，而用户完全看不出来。
- `preview` 不落库、不动缓存：用户拖滑杆时要的是"立刻看到分数怎么变"，
  不该因此改动任何持久状态。
"""

from __future__ import annotations

import logging
import math
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from src.core.errors import (
    BRIEF_DEFAULT,
    brief,
)
from src.domain.intraday.models import (
    SOURCE_AUTO_CHARACTER,
    SOURCE_MANUAL,
    IntradayProfile,
)
from src.intraday.config import DAILY_FACTOR_LABELS, FACTOR_LABELS
from src.intraday.features import build_intraday_features
from src.intraday.impact import annotate_level_basis, build_trigger_impact
from src.intraday.level_fit import FEATURE_KEYS
from src.intraday.service import IntradayService
from src.intraday.weight_profiles import (
    FACTOR_GROUPS,
    factor_keys,
    meta_for,
    templates,
    weights_sum,
)
from src.intraday.weight_profiles import (
    template as get_template,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["intraday-weights"])

Mode = Literal["intraday", "daily"]


def _service(request: Request) -> IntradayService:
    service = getattr(request.app.state.runtime, "intraday", None)
    if service is None:
        raise HTTPException(
            status_code=503, detail="做T辅助模块未装配（intraday service unavailable）")
    return service


def _profile_repo(request: Request) -> Any:
    repo = getattr(request.app.state.runtime, "intraday_profile_repo", None)
    if repo is None:
        raise HTTPException(
            status_code=503,
            detail="做T权重档案仓储未装配（intraday profile repository unavailable）")
    return repo


def _validate_code(code: str) -> str:
    value = (code or "").strip()
    if not (value.isdigit() and len(value) == 6):
        raise HTTPException(status_code=400, detail="证券代码应为6位数字")
    if value.startswith(("8", "4", "920")):
        raise HTTPException(
            status_code=400, detail=f"北交所标的暂不支持（数据源未覆盖）: {value}")
    return value


def _factor_catalog(mode: Mode) -> list[dict[str, Any]]:
    labels = FACTOR_LABELS if mode == "intraday" else DAILY_FACTOR_LABELS
    items: list[dict[str, Any]] = []
    for key in factor_keys(mode):
        meta = meta_for(key, mode)
        items.append({
            "key": key,
            "label": labels.get(key, meta.label),
            "group": meta.group,
            "group_label": FACTOR_GROUPS.get(meta.group, meta.group),
            "default_weight": meta.default_weight,
            "formula": meta.formula,
            "source": meta.source,
            "from_skill": meta.from_skill,
        })
    return items


# ==================== 因子目录 ====================


@router.get("/intraday/factors")
async def factors(
    request: Request,
    mode: Annotated[Mode, Query(description="intraday=分时做T / daily=日线做T")] = "intraday",
    code: Annotated[str, Query(description="可选：带上代码则同时返回该票当前生效权重")] = "",
) -> dict:
    """因子目录 + 权重模板（前端权重编辑器唯一的数据来源）。

    目录是**服务端单一事实来源**：前端不写死任何因子名或中文标签，
    这样以后加因子只需要改 `weight_profiles.py` 一处。
    """
    service = _service(request)
    config = service.config
    payload: dict[str, Any] = {
        "mode": mode,
        "factors": _factor_catalog(mode),
        "groups": [{"key": key, "label": label}
                   for key, label in FACTOR_GROUPS.items()],
        "templates": [
            {"key": item.key, "label": item.label, "description": item.description,
             "weights": item.normalized()}
            for item in templates(mode)
        ],
    }
    if mode == "intraday":
        payload["current_weights"] = config.weights.as_dict()
        payload["thresholds"] = {"action": config.thresholds.action,
                                 "hint": config.thresholds.hint}
        payload["levels"] = config.levels.model_dump()
    else:
        payload["current_weights"] = config.daily_weights.as_dict()
        payload["thresholds"] = {"action": config.daily_thresholds.action,
                                 "hint": config.daily_thresholds.hint}
        payload["levels"] = {}
    payload["weights_sum"] = weights_sum(payload["current_weights"], mode)
    payload["min_visible_weight"] = 0.5
    if code:
        target = _validate_code(code)
        override, source = await service._resolve_override(target, config)  # noqa: SLF001
        if override is not None and not override.is_empty():
            scoped = config.with_override(target, override)
            payload["effective_weights"] = (
                scoped.weights.as_dict() if mode == "intraday"
                else scoped.daily_weights.as_dict())
            payload["override_source"] = source
            payload["override"] = {
                "weights": dict(override.weights),
                "daily_weights": dict(override.daily_weights),
                "thresholds": dict(override.thresholds),
                "levels": dict(override.levels),
                "describe": override.describe(),
            }
        else:
            payload["effective_weights"] = payload["current_weights"]
            payload["override_source"] = ""
            payload["override"] = None
        payload["code"] = target
    return payload


# ==================== 个股股性 ====================


@router.get("/intraday/character")
async def character(
    request: Request,
    code: Annotated[str, Query(description="6位证券代码")] = "300308",
    mode: Annotated[Mode, Query()] = "intraday",
    refresh: Annotated[bool, Query(description="true=忽略进程内缓存重算")] = False,
) -> dict:
    """个股股性画像：做T友好度 + 预填权重模板 + 预填档位。

    这是「根据个股股性自定义权重」的默认值来源：
    高波动震荡票加重箱体/VWAP/布林，趋势票加重缠论/MACD，
    连板题材股加重筹码/情绪周期/板块排行。
    """
    service = _service(request)
    target = _validate_code(code)
    try:
        profile = await service.character(target, mode=mode, refresh=refresh)
    except Exception as exc:  # noqa: BLE001 取数失败转502
        logger.warning("股性画像失败(%s): %s", target, brief(exc, BRIEF_DEFAULT))
        raise HTTPException(
            status_code=502, detail=f"股性画像失败：{brief(exc, BRIEF_DEFAULT)}") from exc
    payload = profile.to_dict()
    payload["mode"] = mode
    # 预填权重的模板说明（前端展示"为什么推荐这套"）
    chosen = get_template(profile.template, mode)
    payload["template_label"] = "" if chosen is None else chosen.label
    payload["template_description"] = "" if chosen is None else chosen.description
    payload["templates"] = [
        {"key": item.key, "label": item.label, "description": item.description,
         "weights": item.normalized()}
        for item in templates(mode)
    ]
    return payload


@router.get("/intraday/market-cycle")
async def market_cycle(request: Request,
                       force: bool = Query(default=False)) -> dict:
    """市场情绪周期：涨停家数 / 炸板率 / 最高连板高度 → 阶段与做T环境温度。"""
    service = _service(request)
    cycle = await service.market_cycle(force=force)
    payload = cycle.to_dict()
    payload["verdict"] = (
        f"周期阶段「{cycle.stage}」，做T环境温度 {cycle.temperature}/100，"
        + ("允许做T" if cycle.t_allowed else "禁止回踩区间提示（退潮/冰点）")
        if cycle.available else (cycle.gap or "不可用"))
    return payload


# ==================== 权重档案（数据库） ====================


class PreviewRequest(BaseModel):
    """权重预览入参（不落库、不动缓存）。"""

    code: str = Field(default="300308")
    mode: Mode = Field(default="intraday")
    weights: dict[str, float] = Field(default_factory=dict)
    thresholds: dict[str, float] = Field(default_factory=dict)
    levels: dict[str, float] = Field(default_factory=dict)
    # 「改动前」那一列的价格线：前端把**面板上此刻**的档位传回来。
    #
    # 为什么不让服务端自己再算一遍基准：预览本身要 1~3 秒，而回踩/冲高/止损是
    # 时刻量（随 VWAP/布林每分钟重算）。服务端自己再取一次，前后两次的 VWAP 会差
    # 0.1% 量级，于是在"变动"列里凭空冒出一行 `VWAP −0.93（−0.10%）` ——
    # 用户会以为调某个参数动到了 VWAP，而那根本不是他改出来的。
    # 用前端当下看到的那份做基准，才能保证「变动」只反映参数改动本身。
    current_levels: dict[str, float] = Field(
        default_factory=dict,
        description="可选：面板当前档位 low_buy/high_sell/stop_loss/vwap")


class ProfileRequest(BaseModel):
    """保存档案入参（**部分更新**：只有出现在请求体里的字段才会被写入）。

    为什么是"部分更新"而不是"整行覆盖"：分时权重与日线权重是两套因子集合，
    用户常常只想改其中一半。若 PUT 按整行覆盖，在日线页保存会把这只票
    **已有的分时权重整体清空**，且没有任何提示 —— 这是会真实丢数据的操作。
    实现上用 pydantic 的 `model_fields_set` 判断"哪些字段真的被提交了"，
    未提交的字段沿用库里的原值。
    """

    name: str | None = Field(default=None, description="证券简称（不传则沿用已有）")
    weights: dict[str, float] | None = Field(
        default=None, description="分时权重覆盖；传全量即按该票口径打分")
    daily_weights: dict[str, float] | None = None
    thresholds: dict[str, float] | None = None
    levels: dict[str, float] | None = Field(
        default=None, description="回踩/冲高/止损档位覆盖")
    daily_thresholds: dict[str, float] | None = None
    template: str | None = None
    source: str | None = Field(
        default=None, description="manual | auto_character | import")
    note: str | None = None
    character_profile: dict[str, Any] | None = None
    # 传 true 时，服务端用该票股性画像自动补全未给出的权重/档位，
    # 前端「按股性一键推荐」按钮走这条路
    apply_character: bool = Field(default=False)



def _baseline_levels(
    payload: dict[str, float], level_set: Any,
) -> Any:
    """把前端回传的「面板此刻档位」拼成对照用的基准 LevelSet。

    只取四个价格字段（回踩/冲高/止损/VWAP）：它们是用户眼睛看到的、也是"变动"
    这一列需要比较的量。基准的来源说明沿用**该票当前生效口径**的标注
    （即用 `baseline` 那一份的参数去 annotate 同一批价格），因此"由谁决定"
    这一列讲的仍是"改动前这条线是怎么来的"，不会张冠李戴。

    四个键一个都没给（或全不是数字）→ 返回 None，让调用方走服务端兜底基准。
    """
    picked = {
        key: float(value) for key, value in payload.items()
        if key in ("low_buy", "high_sell", "stop_loss", "vwap")
        and isinstance(value, (int, float)) and math.isfinite(float(value))
    }
    if not picked:
        return None
    return level_set.model_copy(update=picked)


@router.get("/intraday/level-fit")
async def level_fit(
    request: Request,
    code: Annotated[str, Query(description="6位证券代码")] = "300308",
    refresh: Annotated[bool, Query(description="true=忽略缓存重新拟合")] = False,
) -> dict:
    """关键价位**神经网络拟合**：用 7 个客观维度拟合回踩/冲高/止损三条线。

    返回三个必须一起看的东西（少一个都会误导）：

      1. `metrics.in_sample_rate` —— **过去 N 个交易日**的成功率（用户口径）；
      2. `metrics.walk_forward_rate` —— **留一天**交叉验证的成功率（能不能信它）；
      3. `metrics.gate_passed` —— 是否允许启用拟合档位（不达标就回退规则口径）。

    拟合口径与「成功」的定义见 `src/intraday/level_fit.py` 模块 docstring：
    触及回踩后、在 H 根 bar 内**先**到冲高且不破止损 = 成功；只统计"触及过"的样本。
    """
    service = _service(request)
    target = _validate_code(code)
    config = service.config
    # 数据直接取 5 分钟 bars（多日）自己走一遍特征工程。
    #
    # 为什么不用"轻量快照"里的 bars：快照的 `bars` 字段只有**当日**（前端画图用），
    # 而拟合要的是过去 N 个交易日 —— 实测那样只会拿到 1 天 / 48 根，
    # 拟合会一直报"样本不足"。多日 bars 在快照内部是有的（`features`），
    # 但那条路只在快照调用栈里可见，独立接口只能自己取一次。
    try:
        bars, bars_source, _attempts = await service.data_provider.fetch_bars(
            target, days=service._intraday_days)  # noqa: SLF001
        # ⚠️ `IntradayDataProvider.fetch_quote` 返回的是 **3 元组**
        #    `(Quote, 源名, 尝试记录)`（多源容灾链），**不是 Quote 本身** ——
        #    和上面那行 `fetch_bars` 一样要解包。
        #
        #    这里原来漏了解包，直接把元组当 Quote 往下传，于是
        #    `service._fit_levels_sync` 里的 `quote.price` 抛
        #    `AttributeError: 'tuple' object has no attribute 'price'`；
        #    而 `level_fit` 把这个异常吞成 `None` → 本路由报 503
        #    「档位拟合结果不可用」（2026-09-23 用户实测「重新拟合」必现）。
        #
        #    为什么只有 `refresh=true` 会犯：`refresh=false` 时 `level_fit`
        #    会**先命中当日缓存**直接返回，根本走不到那个同步拟合函数 ——
        #    缓存是快照路径用**正确解包过的** Quote 写进去的。
        #    所以「不点重新拟合就正常、一点就 503」正是这个漏解包的指纹。
        quote, _quote_source, _quote_attempts = \
            await service.data_provider.fetch_quote(target)
        daily = await service._fetch_daily_bars(target)  # noqa: SLF001
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=502,
            detail=("拟合所需数据获取失败（数据源不可用时如实报错，不猜测）："
                    + brief(exc, BRIEF_DEFAULT))
        ) from exc
    daily_bars = daily[0] if isinstance(daily, tuple) else daily
    if bars is None or len(bars) == 0:
        raise HTTPException(
            status_code=502,
            detail="拟合需要多日 5 分钟K线，但数据源未返回（缺口时不拟合、不猜测）")
    try:
        features = build_intraday_features(bars, config=config)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=502, detail=f"分钟特征构建失败：{brief(exc, BRIEF_DEFAULT)}") from exc
    fit = await service.level_fit(
        target, refresh=refresh, features=features,
        daily_bars=daily_bars, quote=quote)
    if fit is None:
        raise HTTPException(status_code=503, detail="档位拟合结果不可用")
    payload = fit.to_dict()
    payload["bars_input"] = int(len(bars))
    payload["bars_source"] = bars_source
    payload["feature_keys"] = list(FEATURE_KEYS)
    payload["config"] = {
        "sessions": config.factors.level_fit.sessions,
        "horizon_bars": config.factors.level_fit.horizon_bars,
        "target_hit_rate": config.factors.level_fit.target_hit_rate,
        "min_touch_samples": config.factors.level_fit.min_touch_samples,
        "round_trip_cost_pct": config.factors.level_fit.round_trip_cost_pct,
    }
    payload["notice"] = (
        f"拟合线：回踩 −{_mix_pct(fit, 'low'):.2f}% / 冲高 +{_mix_pct(fit, 'high'):.2f}% "
        f"/ 止损 −{_mix_pct(fit, 'stop'):.2f}%（相对当日均价）"
        if fit.metrics.available else f"拟合不可用：{fit.metrics.reason}")
    return payload


def _mix_pct(fit: Any, key: str) -> float:
    mix = getattr(fit, f"{key}_mix", None) or []
    anchors = getattr(fit, f"{key}_anchors", None) or []
    if not mix or not anchors or len(mix) != len(anchors):
        return 0.0
    return float(anchors[max(range(len(mix)), key=lambda i: mix[i])])


def _check_weight_sum(weights: dict[str, float], mode: Mode) -> float:
    """权重合计必须=100（分时14因子 / 日线7因子按各自目录校验）。

    只校验**非空**的覆盖项：稀疏差分（例如只写 `{boll: 8}`）无法判断合计，
    那种情况是"覆盖某一项"，不触发合计校验 —— 与 `CodeOverride` 的既有语义一致。
    """
    if not weights:
        return 0.0
    keys = set(factor_keys(mode))
    unknown = set(weights) - keys
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"含未知因子 {sorted(unknown)}；可用因子：{sorted(keys)}")
    negative = [key for key, value in weights.items() if float(value) < 0]
    if negative:
        raise HTTPException(status_code=400, detail=f"权重不得为负：{sorted(negative)}")
    total = weights_sum(weights, mode)
    if abs(total - 100.0) > 1e-6:
        raise HTTPException(
            status_code=400,
            detail=(f"权重合计必须为100，当前={total:g}。"
                    "（只想覆盖某几项时请只提交那几项；"
                    "提交全量则必须恰好合计100 —— 总分刻度依赖它）"))
    return total


def _profile_payload(profile: IntradayProfile, *, effective: dict[str, Any] | None = None,
                     mode: Mode = "intraday") -> dict:
    payload = profile.as_dict()
    payload["describe"] = profile.describe()
    payload["weights_sum"] = weights_sum(profile.weights, "intraday")
    payload["daily_weights_sum"] = weights_sum(profile.daily_weights, "daily")
    if effective is not None:
        payload["effective_weights"] = effective.get("weights", {})
        payload["effective_daily_weights"] = effective.get("daily_weights", {})
        payload["effective_thresholds"] = effective.get("thresholds", {})
        payload["effective_levels"] = effective.get("levels", {})
        payload["effective_source"] = effective.get("source", "")
    _ = mode
    return payload


@router.get("/intraday/weight-profiles")
async def list_profiles(request: Request,
                        limit: int = Query(default=200, ge=1, le=1000)) -> dict:
    """列出全部权重档案（按更新时间倒序）。"""
    repo = _profile_repo(request)
    try:
        items = await repo.list(limit=limit)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=500, detail=f"读取权重档案失败：{brief(exc, BRIEF_DEFAULT)}") from exc
    return {
        "count": len(items),
        "profiles": [_profile_payload(item) for item in items],
        "source": f"db:sqlite.{'dim_intraday_profile'}",
    }


@router.get("/intraday/weight-profiles/{code}")
async def get_profile(code: str, request: Request) -> dict:
    """取单只票的档案 + 当前实际生效的口径（档案 > YAML overrides > 全局）。"""
    service = _service(request)
    target = _validate_code(code)
    repo = _profile_repo(request)
    try:
        profile = await repo.get(target)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=500, detail=f"读取权重档案失败：{brief(exc, BRIEF_DEFAULT)}") from exc
    config = service.config
    override, source = await service._resolve_override(target, config)  # noqa: SLF001
    if override is not None and not override.is_empty():
        scoped = config.with_override(target, override)
        effective = {
            "weights": scoped.weights.as_dict(),
            "daily_weights": scoped.daily_weights.as_dict(),
            "thresholds": {"action": scoped.thresholds.action,
                           "hint": scoped.thresholds.hint},
            "levels": scoped.levels.model_dump(),
            "source": source,
        }
    else:
        effective = {
            "weights": config.weights.as_dict(),
            "daily_weights": config.daily_weights.as_dict(),
            "thresholds": {"action": config.thresholds.action,
                           "hint": config.thresholds.hint},
            "levels": config.levels.model_dump(),
            "source": "全局口径（configs/intraday.yaml）",
        }
    return {
        "code": target,
        "profile": None if profile is None else _profile_payload(profile),
        "effective": effective,
    }


@router.put("/intraday/weight-profiles/{code}")
async def save_profile(code: str, body: ProfileRequest, request: Request) -> dict:
    """保存/更新权重档案（写数据库，保存后立即生效）。

    **部分更新语义**：只有出现在请求体里的字段会被写入，未提交的字段沿用库里的原值。
    这样"在日线页只改日线权重"不会把这只票已有的分时权重清空。
    """
    service = _service(request)
    repo = _profile_repo(request)
    target = _validate_code(code)
    provided = body.model_fields_set
    weights = {key: float(value) for key, value in (body.weights or {}).items()}
    daily_weights = {
        key: float(value) for key, value in (body.daily_weights or {}).items()}
    _check_weight_sum(weights, "intraday")
    _check_weight_sum(daily_weights, "daily")
    levels = {key: float(value) for key, value in (body.levels or {}).items()}
    thresholds = {key: float(value) for key, value in (body.thresholds or {}).items()}
    daily_thresholds = {
        key: float(value) for key, value in (body.daily_thresholds or {}).items()}
    source = body.source
    character_profile = dict(body.character_profile or {})
    name = (body.name or "").strip()
    try:
        existing = await repo.get(target)
    except Exception:  # noqa: BLE001 读旧档失败不阻断保存（只是拿不到原值）
        existing = None
    if body.apply_character:
        # 「按股性一键推荐」：用该票画像补全未显式给出的权重与档位。
        try:
            character = await service.character(target)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=502,
                detail=("取股性画像失败（无法按股性预填）："
                        + brief(exc, BRIEF_DEFAULT)),
            ) from exc
        if not character.available:
            raise HTTPException(
                status_code=400,
                detail=f"该票股性画像不可用，无法按股性预填权重：{character.gap}")
        if "weights" not in provided:
            weights = dict(character.weights)
            provided = provided | {"weights"}
        if "levels" not in provided and character.levels:
            levels = dict(character.levels)
            provided = provided | {"levels"}
        source = source or SOURCE_AUTO_CHARACTER
        character_profile = character_profile or character.to_dict()
        name = name or character.name

    def _pick(field: str, incoming: Any, previous: Any) -> Any:
        """提交了就覆盖，没提交就沿用库里的原值（部分更新的核心）。"""
        if field in provided:
            return incoming
        return previous if previous is not None else incoming

    profile_model = IntradayProfile(
        code=target,
        name=_pick("name", name, existing.name if existing else ""),
        weights=_pick("weights", weights, existing.weights if existing else {}),
        daily_weights=_pick("daily_weights", daily_weights,
                            existing.daily_weights if existing else {}),
        thresholds=_pick("thresholds", thresholds,
                         existing.thresholds if existing else {}),
        daily_thresholds=_pick("daily_thresholds", daily_thresholds,
                               existing.daily_thresholds if existing else {}),
        levels=_pick("levels", levels, existing.levels if existing else {}),
        character_profile=_pick("character_profile", character_profile,
                                existing.character_profile if existing else {}),
        template=_pick("template", body.template or "",
                       existing.template if existing else ""),
        source=_pick("source", source or SOURCE_MANUAL,
                     existing.source if existing else SOURCE_MANUAL),
        note=_pick("note", body.note or "", existing.note if existing else ""),
    )
    if profile_model.is_empty():
        raise HTTPException(
            status_code=400,
            detail="档案为空：至少要给出权重/日线权重/阈值/档位中的一项，"
                   "否则保存下来不会有任何效果")
    try:
        saved = await service.save_profile(profile_model)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=400, detail=f"保存权重档案失败：{brief(exc, BRIEF_DEFAULT)}") from exc
    return {
        "ok": True, "code": target,
        "profile": _profile_payload(saved),
        "describe": saved.describe(),
        "notice": (f"已保存权重档案 {target}（{saved.describe()}）——"
                   "该票的打分/档位与全局口径不同，下次打开自动复用"),
    }


@router.delete("/intraday/weight-profiles/{code}")
async def delete_profile(code: str, request: Request) -> dict:
    """删除权重档案（幂等）；删除后该票回落到 YAML overrides / 全局口径。"""
    service = _service(request)
    target = _validate_code(code)
    try:
        removed = await service.delete_profile(target)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=400, detail=f"删除权重档案失败：{brief(exc, BRIEF_DEFAULT)}") from exc
    return {
        "ok": True, "code": target, "removed": removed,
        "notice": (f"已删除 {target} 的权重档案，该票回到全局口径"
                   if removed else f"{target} 本来就没有权重档案（幂等成功）"),
    }


@router.post("/intraday/weight-profiles/preview")
async def preview(body: PreviewRequest, request: Request) -> dict:
    """用给定权重重算一次总分（**不落库、不动缓存**），供前端拖滑杆时实时预览。

    实现方式：把这组权重作为 `config_patch` 传给服务器端的同一条打分链路，
    因此预览分数与保存后的真实分数**必然同源同口径**（不是前端另算一遍）。

    刻意用完整快照（`light=False`）而不是轻量快照：轻量模式会跳过
    消息面/指数量能/板块排行/海外映射，预览出来的总分与面板上的分数不可比，
    反而会误导用户。
    """
    service = _service(request)
    target = _validate_code(body.code)
    weights = {key: float(value) for key, value in body.weights.items()}
    total = _check_weight_sum(weights, body.mode) if weights else 0.0
    thresholds = {key: float(value) for key, value in body.thresholds.items()}
    levels = {key: float(value) for key, value in body.levels.items()}
    if not weights and not thresholds and not levels:
        raise HTTPException(status_code=400, detail="预览至少要给出一项改动（权重/阈值/档位）")

    base = service.config
    # 「改动前」的基准口径：这只票**当前实际生效**的那一套（档案 > YAML 覆盖 > 全局）。
    # 只用来给对照表提供"改动前"那一列，任何失败都不阻断预览
    # （那时对照列留空，面板只显示"改动后"，绝不编造基准）。
    try:
        baseline_config = await service.effective_config(target)
    except Exception as exc:  # noqa: BLE001
        logger.info("预览基准口径获取失败(%s): %s", target, brief(exc, BRIEF_DEFAULT))
        baseline_config = None
    try:
        # 预览以**全局口径**为基准（不叠加该票已有档案）：
        # 否则用户看到的是"档案 + 我的改动"的叠加结果，看不出改动本身的效果。
        scoped = base.with_score_patch(
            body.mode, weights=weights, thresholds=thresholds, levels=levels)
    except Exception as exc:  # noqa: BLE001 参数非法转400
        raise HTTPException(
            status_code=400, detail=f"预览参数非法：{brief(exc, BRIEF_DEFAULT)}") from exc

    try:
        if body.mode == "intraday":
            snapshot = await service.snapshot(
                target, force_refresh=False, light=False, config_patch=scoped)
            card = snapshot.scorecard
            # 「改动后到底会变成什么样」——档位线、触发价、还差多少分/多少价。
            # 这里返回的档位是**这次预览口径**下的真实档位（含档位参数改动的影响），
            # 与面板上那条线同源，因此可以直接用来做「改动前/改动后」对照。
            #
            # 先 annotate 再算 impact：`impact.level_rows` 用的就是这一份带来源
            # 标注的档位，前面板"改动前/改动后"两列与"由谁决定"那一列才会一致 ——
            # 若这里仍返回未标注的 snapshot.levels，前端拿到的来源会是空的。
            level_set = (None if snapshot.levels is None
                         else annotate_level_basis(snapshot.levels, scoped))
            # 「改动前」那一列：
            #   1) 优先用前端回传的「面板此刻档位」—— 它与用户眼睛看到的一致，
            #      "变动"列就只反映参数改动（见 PreviewRequest.current_levels 说明）；
            #   2) 没传就退回服务端自己取的当前口径档位（可能已漂移几百毫秒）。
            baseline = dict(body.current_levels)
            current_levels = None
            if level_set is not None:
                current_levels = _baseline_levels(baseline, level_set)
            if current_levels is None:
                raw_current = None
                try:
                    raw_current = await service.current_levels(target)
                except Exception as exc:  # noqa: BLE001
                    logger.info("预览基准档位获取失败(%s): %s", target, brief(exc, BRIEF_DEFAULT))
                current_levels = (None if raw_current is None
                                  else annotate_level_basis(
                                      raw_current, baseline_config or base))
            impact = build_trigger_impact(
                scorecard=card, levels=level_set, config=scoped,
                current_levels=current_levels)
            effective = {
                "weights": scoped.weights.as_dict(),
                "thresholds": {"action": scoped.thresholds.action,
                               "hint": scoped.thresholds.hint},
                "levels": scoped.levels.model_dump(),
                "level_set": None if level_set is None else level_set.model_dump(),
                "impact": impact.model_dump(),
                "signal": None if snapshot.signal is None
                else snapshot.signal.model_dump(),
            }
        else:
            daily = await service.daily(target, config_patch=scoped)
            card = daily.scorecard
            effective = {
                "weights": scoped.daily_weights.as_dict(),
                "thresholds": {"action": scoped.daily_thresholds.action,
                               "hint": scoped.daily_thresholds.hint},
                "levels": {},
                "level_set": None,
                # 日K模式没有「回踩档位」这套线（止损/保护线由量价体系自算），
                # 只给权重影响度，别硬套分时的解释。
                "impact": build_trigger_impact(
                    scorecard=card, levels=None, config=scoped).model_dump(),
                "signal": None,
            }
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("权重预览失败(%s): %s", target, brief(exc, BRIEF_DEFAULT))
        raise HTTPException(
            status_code=502, detail=f"权重预览失败：{brief(exc, BRIEF_DEFAULT)}") from exc
    if card is None:
        raise HTTPException(status_code=502, detail="预览失败：该标的当天打分卡未生成")
    return {
        "code": target, "mode": body.mode,
        "weights_sum": total or weights_sum(effective["weights"], body.mode),
        "scorecard": card.model_dump(),
        "notice": f"预览总分 {card.total:+.1f}（{card.verdict}）——预览不落库，"
                  "点「保存」才会写入数据库并在下次打开时复用",
        **effective,
    }
