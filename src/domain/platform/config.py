"""套餐配置：资源上限 + 功能权限 + 定价（**单一事实来源**）。

对应设计：`docs/PLATFORM_MULTI_TENANCY_DESIGN.md` §4.6（tiers 表）、
§7.2.6①（三重配额）、§8.6.10（管理员控制台）。

## 为什么把"资源上限"和"功能权限"放在同一个文件里

它们是**同一个商业决策的两面**：给某个等级开多少资源、能进哪些页面、
每项多少钱 —— 运营改价时是一起改的。分成两处存，迟早出现
"页面开了但资源没给"或"涨了价但权限没动"这种自相矛盾的套餐。

## 为什么是文件（JSON）而不是数据库表

三个理由：
  1. **要能进 Git**：套餐改动应当可评审、可回滚、可 diff ——
     它不是"用户数据"，而是**部署配置**；
  2. 读取频率低（每次鉴权一次，且进程内缓存），不值得为它建表；
  3. 出事故时 `git checkout` 就能回到上一版套餐。

数据库里只存**用户属于哪个等级**（`dim_user.applied_tier`）。

## 与 `src/domain/quota/service.py` 的关系（不能有两份额度表）

`service.py` 里的 `TIER_QUOTAS` 是**自选池**的额度（池数/单池/总数），
本文件的 `resources` 是**平台级**资源（调用频次/token/标的数）。
两者**刻意分开**：一个是"自选池能放多少票"，一个是"这套套餐一共能调多少次"。
但为避免"两处都写着 watchlist 上限"，本文件的自选相关字段
以 `watchlist_stocks` 为**平台口径**，而 `TIER_QUOTAS` 是池结构约束 ——
`tests/unit/test_platform_config.py` 会校验两者不矛盾。
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: 配置文件路径（相对项目根；`MOSS_TIER_CONFIG` 可覆盖，测试用）
DEFAULT_CONFIG_PATH = "configs/platform_tiers.json"

#: 等级顺序：**权限从大到小**。前端的下拉、权限继承、默认值都按它。
TIER_ORDER: tuple[str, ...] = ("admin", "vip", "trial")

#: 可配置的功能（key → 中文名）。
#:
#: ★ key 必须与前端页签一一对应，否则"配了权限但页面还是进得去"。
#: 子功能（竞价选股/量化选股/辅助做T）是**量化交易**页内的模块，
#: 单独列出来是因为它们可以分别售卖。
#:
#: ## ⚠️ 这里**只放可售卖的项**，运维面能力一律不放
#:
#: 用户口径（2026-09-25）：
#:
#:   "功能权限 里，默认只有管理员有运行指标、调度管理的权限，
#:    不用加在功能权限设置的选项里"
#:   "可以不删除，但是不用显示或者写死默认管理员有这两个权限，
#:    其他用户都没这个权限且不可选择。"
#:
#: 所以 `scheduler`（调度管理）与 `metrics`（运行指标）**不在本表**：
#: 它们不进权限矩阵、不可勾选，由 `my_features.ADMIN_ONLY_VIEWS`
#: **写死**给管理员、其他人一律没有（那是规则层，不是可改的默认值）。
#: `configs/platform_tiers.json` 里仍留着这两个键 —— 按用户口径
#: **不删**（删了反而要在多处补"为什么没有"），但它们已不被任何判据读取。
FEATURES: dict[str, str] = {
    "research": "投研分析",
    "backtest": "策略回测",
    "mainline": "主线挖掘",
    "fundflow": "资金流监控",
    "quant.auction": "量化交易 · 竞价选股",
    "quant.select": "量化交易 · 量化选股",
    "quant.intraday": "量化交易 · 辅助做T",
    # ── 舆情情报 ──
    # `intel.hot` 一个售卖项对应**两个**顶级页签
    # （「热点&研报小作文」与「投资日历」）—— 它们共用同一份公开信息，
    # 只是两种看法（"现在在说什么" vs "接下来会发生什么"），
    # 拆成两个售卖项只会让矩阵变长而没有区分度。
    #
    # ⚠️ 改这里之后**必须同步** `src/api/routes/my_features.py` 的
    # `VIEW_FEATURE` **以及** `src/api/routes/intel.py` 的 `FEATURE_*` 常量，
    # 否则出现"矩阵里能勾、用户侧 403"或"配了权限但页面进不去"的静默故障。
    # 有单测断言这几处是包含关系。
    "intel.hot": "舆情情报 · 热点&研报小作文",
    "intel.alerts": "舆情情报 · 事件告警中心",
}

#: 资源上限的字段与中文名（前端表单、监控页共用一份口径）。
RESOURCE_FIELDS: dict[str, str] = {
    "api_calls_per_minute": "接口调用 / 分钟",
    "api_calls_per_day": "接口调用 / 天",
    "llm_tokens_per_month": "大模型 token / 月",
    "watchlist_stocks": "自选标的上限（去重）",
    "watchlist_pools": "自选池数量上限",
    "concurrent_tasks": "并发分析任务数",
    "max_backtest_days": "回测区间上限（天）",
}


class TierConfigError(ValueError):
    """配置非法（**拒绝保存**，而不是存进去等运行期出错）。"""


@dataclass
class TierPlan:
    """一个等级的完整套餐。"""

    key: str
    label: str
    #: 是否可选售（管理员档通常不售，只是内部角色）
    sellable: bool = True
    #: 资源上限（键见 `RESOURCE_FIELDS`）
    resources: dict[str, int] = field(default_factory=dict)
    #: 功能权限：feature key → 是否开启
    features: dict[str, bool] = field(default_factory=dict)
    #: 每项功能的**定价**（元/月）。0 表示包含在套餐里不单独收费。
    #:
    #: ⚠️ 与 `monthly_price`（套餐月费）的区别，以及后者为什么被删掉：
    #: **本字段仍在使用** —— 权限矩阵的每个格子会带上它（`/permissions-matrix`
    #: 的 `price`），管理员据此看到"这项单卖多少钱"。
    #: 而"套餐月费"是一个**在界面上没有任何用途的数字**（用户口径 2026-09-23：
    #: 功能权限页只留开关，不留价格），留着它只会让配置文件里多一个
    #: 没人维护、却看起来像"售价"的字段 —— 于是整个字段连同它的读写
    #: 一起删除，而不是留在那里当摆设。
    pricing: dict[str, float] = field(default_factory=dict)
    note: str = ""


def _default_config() -> dict[str, Any]:
    """出厂默认套餐（与设计 §4.6 的口径一致）。

    ## ⚠️ 这里只覆盖 `FEATURES`（可售卖项），不含管理员专属项

    `scheduler` / `metrics` **不在**本函数产出的 features 里 ——
    它们已从 `FEATURES` 移出（管理员专属，不进权限矩阵）。
    `tests/unit/test_platform_config.py::test_defaults_cover_all_tiers_and_features`
    断言"每档的 features 键集合恰好等于 FEATURES"，所以这里多写一个键
    就会失败 —— 那条断言正是防止"出厂默认与售卖口径漂移"的。
    """
    all_features = dict.fromkeys(FEATURES, True)
    # 试用：只给"看"的能力，不给重资源功能
    trial_features = dict.fromkeys(FEATURES, False)
    trial_features.update({
        "research": True,
        "quant.intraday": True,   # 做T是核心卖点，试用必须能体验
        "quant.auction": False,   # 竞价选股最贵，试用不开
        "quant.select": False,
        "backtest": False,        # 回测吃 CPU，试用不放
        # 舆情情报：`intel.hot`（热点&研报小作文 + 投资日历）是**每日价值锚点**，
        # 试用要能体验；告警是**执行抓手**（不是展示品），试用只读 ——
        # 不给自己配置告警的能力。
        "intel.hot": True,
        "intel.alerts": False,
    })

    vip_features = dict.fromkeys(FEATURES, True)
    vip_features["quant.auction"] = False   # 竞价选股按项加购

    return {
        "version": 1,
        "plans": {
            "admin": {
                "label": "管理员",
                "sellable": False,
                "resources": {
                    "api_calls_per_minute": 600,
                    "api_calls_per_day": 200000,
                    "llm_tokens_per_month": 20000000,
                    "watchlist_stocks": 100,
                    "watchlist_pools": 5,
                    "concurrent_tasks": 8,
                    "max_backtest_days": 3650,
                },
                "features": all_features,
                "pricing": dict.fromkeys(FEATURES, 0.0),
                "note": "内部角色，不售；含全部功能与最高资源上限",
            },
            "vip": {
                "label": "VIP",
                "sellable": True,
                "resources": {
                    "api_calls_per_minute": 300,
                    "api_calls_per_day": 50000,
                    "llm_tokens_per_month": 5000000,
                    "watchlist_stocks": 100,
                    "watchlist_pools": 5,
                    "concurrent_tasks": 4,
                    "max_backtest_days": 1825,
                },
                "features": vip_features,
                # ⚠️ **不要**在这里手写字面量键名 —— 加功能时必漏。
                # admin/trial 用 `dict.fromkeys(FEATURES, 0.0)` 自动跟随，
                # VIP 因为要给 `quant.auction` 单独定价，才写成了展开式：
                # 先按 FEATURES 铺满 0.0，再覆盖需要收费的那一项。
                # 首版是纯字面量，于是加了 3 个 intel 功能后 VIP 缺这 3 个
                # 定价键 —— 测试报 "vip 缺定价键"，而 features 却齐全，
                # 因为 features 走的是 `dict.fromkeys(FEATURES, True)`。
                "pricing": {
                    **dict.fromkeys(FEATURES, 0.0),
                    "quant.auction": 199.0,      # 单独加购
                },
                "note": "含主要业务功能；竞价选股需单独加购",
            },
            "trial": {
                "label": "试用",
                "sellable": True,
                "resources": {
                    "api_calls_per_minute": 60,
                    "api_calls_per_day": 3000,
                    "llm_tokens_per_month": 300000,
                    "watchlist_stocks": 25,
                    "watchlist_pools": 5,
                    "concurrent_tasks": 1,
                    "max_backtest_days": 0,      # 0 = 未开放
                },
                "features": trial_features,
                "pricing": dict.fromkeys(FEATURES, 0.0),
                "note": "只读+做T体验；回测与选股类不开放",
            },
        },
    }


class PlatformConfigStore:
    """套餐配置的读写（进程内缓存 + 写时校验 + 原子落盘）。

    ## 为什么写入要"先校验全部、再落盘"

    套餐是**全局生效**的：写坏一个字段，所有该等级用户立刻受影响。
    所以保存时先把整份配置构造出来、逐项校验（等级存在、功能键已知、
    数值范围合法），全通过才写文件 —— 不允许"写一半发现不对"。
    """

    def __init__(self, path: str | Path | None = None) -> None:
        import os

        raw = str(path or os.environ.get("MOSS_TIER_CONFIG")
                  or DEFAULT_CONFIG_PATH)
        p = Path(raw)
        # 相对路径按项目根解析（与 `intraday.yaml` 同一约定：
        # 服务不在仓库根启动时也不能找不到配置）
        if not p.is_absolute():
            p = Path(__file__).resolve().parents[3] / p
        self._path = p
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path

    # ---------------- 读 ----------------

    def load(self) -> dict[str, Any]:
        """读配置；文件不存在或损坏时**回退出厂默认**并告警。

        为什么回退而不是抛错：套餐配置缺失不该让整个平台起不来 ——
        回退到默认套餐是"受限但可用"，而 500 是"谁都别用"。

        ## ★ 必须是**深合并**（实测踩到的缺口）

        第一版只做了浅合并：文件里写了 `vip.resources` 就把该等级的
        整个 `resources` 换掉 —— 于是运维手写一份"只调一个字段"的配置时，
        其余字段**全部消失**（例如 `watchlist_stocks` 不见了，
        界面显示 0、用户加票即被拒）。

        所以这里逐层合并：`plans → 每个等级 → resources/features/pricing`。
        效果是"文件里只写要改的，其余保留出厂默认"——
        这正是运维手改配置时的自然预期。
        """
        if not self._path.exists():
            logger.warning("套餐配置不存在（%s），使用出厂默认", self._path)
            return _default_config()
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.error("套餐配置无法解析（%s）：%s，使用出厂默认",
                         self._path, exc)
            return _default_config()

        merged = _default_config()
        merged["version"] = data.get("version", 1)
        for tier, incoming in (data.get("plans") or {}).items():
            if not isinstance(incoming, dict):
                continue
            base = merged["plans"].setdefault(
                tier, {"label": tier, "resources": {}, "features": {},
                       "pricing": {}})
            for field_name in ("resources", "features", "pricing"):
                block = incoming.get(field_name)
                if isinstance(block, dict):
                    base.setdefault(field_name, {}).update(block)
            for scalar in ("label", "sellable", "note"):
                if scalar in incoming:
                    base[scalar] = incoming[scalar]
        return merged

    def plans(self) -> dict[str, TierPlan]:
        raw = self.load().get("plans") or {}
        out: dict[str, TierPlan] = {}
        for key, item in raw.items():
            out[key] = TierPlan(
                key=key,
                label=str(item.get("label") or key),
                sellable=bool(item.get("sellable", True)),
                resources={k: int(v) for k, v in
                           (item.get("resources") or {}).items()},
                features={k: bool(v) for k, v in
                          (item.get("features") or {}).items()},
                pricing={k: float(v) for k, v in
                         (item.get("pricing") or {}).items()},
                note=str(item.get("note") or ""),
            )
        return out

    def plan(self, tier: str) -> TierPlan:
        """取某个等级的套餐；未知等级回退 `trial`（**fail-closed**）。"""
        plans = self.plans()
        return plans.get(str(tier or "").strip().lower()
                         or "trial") or plans["trial"]

    def feature_enabled(self, tier: str, feature: str) -> bool:
        return bool(self.plan(tier).features.get(feature, False))

    # ---------------- 写 ----------------

    def save_plan(self, tier: str, patch: dict[str, Any]) -> TierPlan:
        """改一个等级的套餐（**整体校验后原子落盘**）。"""
        key = str(tier or "").strip().lower()
        if key not in TIER_ORDER:
            raise TierConfigError(f"未知等级：{tier!r}（可选：{'/'.join(TIER_ORDER)}）")
        plans = self.plans()
        plan = plans[key]

        if "label" in patch and patch["label"] is not None:
            label = str(patch["label"]).strip()
            if not label:
                raise TierConfigError("等级名称不能为空")
            plan.label = label
        # 已下线字段：**忽略并留日志，不报错**。
        #
        # 为什么不是 400：移除的是一个**产品决策上的**字段，不是配置完整性
        # 约束。而"报错"会把一个**还没刷新页面的旧前端**（它仍会带上这个键）
        # 变成"资源管控页保存一律失败" —— 用户会以为整个面板坏了。
        # 相比"记一条日志"，那个代价更大，而且排查方向完全错。
        # 所以这里接住它、丢掉它，并把"有人还在发这个字段"留在日志里。
        if patch.get("monthly_price") not in (None, 0, 0.0):
            logger.warning(
                "收到已下线的字段 monthly_price=%r（套餐月费已移除，已忽略）。"
                "若看到这条日志，说明还有旧版前端在发送它，请硬刷新页面。",
                patch["monthly_price"])
        if "sellable" in patch and patch["sellable"] is not None:
            plan.sellable = bool(patch["sellable"])
        if "note" in patch and patch["note"] is not None:
            plan.note = str(patch["note"])

        for src, dst, label_cn in (
            (patch.get("resources"), plan.resources, "资源上限"),
            (patch.get("features"), plan.features, "功能权限"),
            (patch.get("pricing"), plan.pricing, "功能定价"),
        ):
            if not src:
                continue
            if not isinstance(src, dict):
                raise TierConfigError(f"{label_cn}必须是对象")
            for fkey, value in src.items():
                if dst is plan.resources:
                    if fkey not in RESOURCE_FIELDS:
                        raise TierConfigError(
                            f"未知资源项：{fkey!r}（可选："
                            f"{'/'.join(RESOURCE_FIELDS)}）")
                    dst[fkey] = _non_negative_int(value, f"{label_cn}.{fkey}")
                elif dst is plan.features:
                    if fkey not in FEATURES:
                        raise TierConfigError(
                            f"未知功能：{fkey!r}（可选：{'/'.join(FEATURES)}）")
                    dst[fkey] = bool(value)
                else:
                    if fkey not in FEATURES:
                        raise TierConfigError(f"未知功能定价：{fkey!r}")
                    dst[fkey] = _money(value, f"定价.{fkey}")

        self._write({**plans, key: plan})
        return plan

    def _write(self, plans: dict[str, TierPlan]) -> None:
        """原子写：先写临时文件再 `replace`。

        直接覆盖写有个真实风险：写到一半进程被杀 → 文件半截 →
        下次启动**所有等级回退出厂默认**（用户突然被降级）。
        临时文件 + 原子替换保证"要么旧的、要么新的"。
        """
        payload = {
            "version": 1,
            "_comment": "由管理员控制台「资源管控 / 功能权限」维护；"
                        "可进 Git 评审与回滚。",
            "plans": {
                k: {
                    "label": p.label,
                    "sellable": p.sellable,
                    "resources": p.resources,
                    "features": p.features,
                    "pricing": p.pricing,
                    "note": p.note,
                }
                for k, p in sorted(plans.items(),
                                   key=lambda kv: _tier_rank(kv[0]))
            },
        }
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2)
                           + "\n", encoding="utf-8")
            tmp.replace(self._path)
        logger.warning("套餐配置已更新：%s", self._path)


def _tier_rank(tier: str) -> int:
    try:
        return TIER_ORDER.index(tier)
    except ValueError:
        return len(TIER_ORDER)


def _non_negative_int(value: Any, label: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise TierConfigError(f"{label} 必须是整数：{value!r}") from exc
    if number < 0:
        raise TierConfigError(f"{label} 不能为负：{number}")
    if number > 10**12:
        raise TierConfigError(f"{label} 过大：{number}")
    return number


def _money(value: Any, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise TierConfigError(f"{label} 必须是数字：{value!r}") from exc
    if number != number or number in (float("inf"), float("-inf")):
        raise TierConfigError(f"{label} 不是有限数字")
    if number < 0:
        raise TierConfigError(f"{label} 不能为负：{number}")
    return round(number, 2)


def describe_features() -> dict[str, str]:
    """功能清单（前端选择用；含中文名）。"""
    return dict(FEATURES)


def describe_resources() -> dict[str, str]:
    return dict(RESOURCE_FIELDS)


def plan_to_json(plan: TierPlan) -> dict[str, Any]:
    data = asdict(plan)
    # 补齐缺省键：前端表格按 `FEATURES` 全量渲染，缺键会显示成"—"难以分辨
    data["features"] = {k: bool(plan.features.get(k, False))
                        for k in FEATURES}
    data["pricing"] = {k: float(plan.pricing.get(k, 0.0)) for k in FEATURES}
    data["resources"] = {k: int(plan.resources.get(k, 0))
                         for k in RESOURCE_FIELDS}
    return data


#: 进程内单例（配置读取频繁，且要保证同一进程内视图一致）
_STORE: PlatformConfigStore | None = None
_STORE_PATH: str = ""


def get_platform_config() -> PlatformConfigStore:
    """取配置存储（惰性单例；路径变化时重建 —— 与自选池 provider 同一约定）。"""
    global _STORE, _STORE_PATH  # noqa: PLW0603
    import os

    path = str(os.environ.get("MOSS_TIER_CONFIG") or DEFAULT_CONFIG_PATH)
    if _STORE is None or _STORE_PATH != path:
        _STORE = PlatformConfigStore(path)
        _STORE_PATH = path
    return _STORE


def reset_platform_config() -> None:
    """清掉单例（**仅测试用**）。"""
    global _STORE, _STORE_PATH  # noqa: PLW0603
    _STORE = None
    _STORE_PATH = ""


__all__ = [
    "DEFAULT_CONFIG_PATH",
    "FEATURES",
    "RESOURCE_FIELDS",
    "TIER_ORDER",
    "PlatformConfigStore",
    "TierConfigError",
    "TierPlan",
    "describe_features",
    "describe_resources",
    "get_platform_config",
    "plan_to_json",
    "reset_platform_config",
]
