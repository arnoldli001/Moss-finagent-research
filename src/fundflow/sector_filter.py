"""板块资金流榜的板块剔除：把"没有明确行业/概念"的板块挡在榜单之外。

## 挡什么

清单在 `configs/fundflow_sector_exclude.yaml`（**显式冻结清单**，逐个核过名称），
六类：地域性板块 / 海外指数与指数成分 / 资金持仓属性 / 风格规模 /
市场统计情绪事件 / 用户点名的标签板。

## 为什么不能按"882 开头"判（用户口径 vs 数据源口径）

用户口径是"不要地域性板块（882 开头）江苏板块"。但 **882xxx 是同花顺代码**，
而资金流榜的数据源是 Tushare `moneyflow_ind_dc`（**东财**口径），
板块代码形如 `BK0159.DC`：

    江苏板块  BK0159.DC        富时罗素  BK0867.DC
    融资融券  BK0596.DC        华为概念  BK0854.DC

实测两套体系在本项目里**零重叠**：主线挖掘池 `ml_board` 只有 885/886 开头的
161 个代码，一个 882/883 都没有。所以"地域性"只能按**名称**识别 ——
本模块因此同时按代码与名称匹配（代码优先，名称兜底）。

## 为什么"代码 + 名称"两个都要

榜单层（`FlowEntity.code`）用的其实是**板块名**（`code == name`，见
`service._rank_sectors`），而底层截面 `moneyflow_ind_dc` 才带真代码
（形如 `BK1638.DC`）。两者都匹配的好处是：上游改名时，凡是有代码可用的路径
仍能挡住；只有名称可用的路径（已选列表）会漏，这一条写在"已知局限"里。

清单里的 `code` 一律填**东财真代码**（从
`data/cache/fundflow/sector_snapshot.parquet` 逐个核出，不按序号猜 ——
猜错的代码会静默删掉另一个真实板块，见 `verify_codes`）。
名称匹配是**整名相等**（`in frozenset`），不是包含关系：所以剔除「电子」时
「电子车牌」「电子化学品」不受影响，剔除「新材料」时不波及「金属新材料」。

## 调用点（改这里要同步看这三处）

    1. `service._build()`            —— 过滤截面 + 过滤已选板块
       （必须**在 `ensure_defaults` 之前**：默认热门是按当日净额绝对值取的，
        而融资融券/富时罗素/MSCI 这类板块金额极大，不先滤掉就会被写进
        用户的持久化选择列表，之后再滤就只剩"显示了但不参与"的尴尬状态）
    2. `routes/fundflow.py search()` —— 过滤板块搜索项，避免手工再加回来
    3. `pick` 走的是 `board.sector_rank`，由第 1 处自动覆盖

## 读不到配置时：放行全部（fail-open），并写明

文件缺失/损坏 → 空清单 + `source_notes` 里记一条。方向是刻意选的：
放行只是"用户又看到地域板块"（难看但界面完整），
而全部剔除会让榜单空白，用户会以为数据源坏了、往完全错的方向排查。
与 `configs/sector_blacklist.yaml` 的"读不到时回退 `visible=1` 并写明"同一考虑。

## 已知局限

* **已选列表只按名称匹配**（那一层没有代码）。上游把 `江苏板块` 改名后，
  它仍留在用户的选择列表里并继续参与榜单 —— 需要在界面上手工移除一次。
* 本清单是**冻结**的：新出现的统计/风格类板块不会被自动识别。
  东财每季度都会新增报表预告板（如 `2026年报预增`），需要按批补进来。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: 仓库根（本文件在 `src/fundflow/` 下，根目录是上两级）
PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_NAME = "fundflow_sector_exclude.yaml"


@dataclass(frozen=True)
class SectorExclude:
    """剔除清单的解析结果（不可变，可安全跨请求共享）。"""

    codes: frozenset[str] = frozenset()
    names: frozenset[str] = frozenset()
    #: 原始 `{code, name}` 条目（供 `verify_codes` 逐对校验用）
    entries: tuple[dict[str, str], ...] = ()
    #: 清单里登记了多少条（用来在 `source_notes` 里如实报数）
    size: int = 0
    loaded: bool = False
    gap: str = ""
    #: 按类别分组的条目（仅供排查/展示，不参与匹配）
    groups: dict[str, list[str]] = field(default_factory=dict)

    def excluded(self, name: str, code: str = "") -> bool:
        """该板块是否应被剔除（代码优先匹配，其次名称）。"""
        text = str(code or "").strip().upper()
        if text and text in self.codes:
            return True
        return str(name or "").strip() in self.names


def _read_yaml(path: Path) -> dict[str, Any]:
    """读 YAML；任何异常都返回空字典（调用方按"清单为空"处理）。"""
    try:
        import yaml

        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception as exc:  # noqa: BLE001 配置问题不该让资金流页面 500
        logger.warning("板块剔除清单读取失败（%s）：%s", path.name,
                       type(exc).__name__)
        return {}


def load_sector_exclude(path: str | Path | None = None) -> SectorExclude:
    """加载剔除清单；文件缺失/损坏时返回空清单 + `gap`（不抛错）。"""
    target = Path(path) if path else PROJECT_ROOT / "configs" / CONFIG_NAME
    if not target.exists():
        return SectorExclude(
            gap=f"剔除清单不存在：configs/{CONFIG_NAME}（本次不剔除任何板块）")
    raw = _read_yaml(target)
    if not raw:
        return SectorExclude(
            gap=f"剔除清单为空或无法解析：configs/{CONFIG_NAME}"
                "（本次不剔除任何板块）")
    codes: set[str] = set()
    names: set[str] = set()
    entries: list[dict[str, str]] = []
    groups: dict[str, list[str]] = {}
    for item in raw.get("codes") or []:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code") or "").strip().upper()
        name = str(item.get("name") or "").strip()
        if code:
            codes.add(code)
        if name:
            names.add(name)
            groups.setdefault(name[:2], []).append(name)
        if code or name:
            entries.append({"code": code, "name": name})
    out = SectorExclude(codes=frozenset(codes), names=frozenset(names),
                        entries=tuple(entries),
                        size=max(len(codes), len(names)), loaded=bool(codes or names),
                        groups=groups)
    if not out.size:
        return SectorExclude(
            gap=f"剔除清单里没有可用条目：configs/{CONFIG_NAME}"
                "（本次不剔除任何板块）")
    return out


@lru_cache(maxsize=1)
def _cached() -> SectorExclude:
    """进程内缓存（清单是冻结的；改了要重启服务或调 `reset_cache()`）。"""
    return load_sector_exclude()


def reset_cache() -> None:
    """清缓存（测试与"改了清单想立即生效"时用）。"""
    _cached.cache_clear()


def current() -> SectorExclude:
    """当前生效的剔除清单。"""
    return _cached()


def is_excluded(name: str, code: str = "") -> bool:
    """该板块是否在剔除清单里（模块级快捷入口，三个调用点共用）。"""
    return _cached().excluded(name, code)


def filter_payload(payload: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """按剔除清单过滤 `{板块名: {...}}` 截面，返回 `(保留的, 剔除条数)`。

    只做过滤、不改内容：剔掉的板块在返回的字典里**不存在**，
    于是下游（榜单/默认热门/走势）自然都看不到它们。
    """
    config = _cached()
    if not config.loaded or not payload:
        return payload, 0
    kept = {name: info for name, info in payload.items()
            if not config.excluded(name, str((info or {}).get("code") or ""))}
    return kept, len(payload) - len(kept)


def filter_names(names: list[str]) -> tuple[list[str], int]:
    """按剔除清单过滤板块名列表，返回 `(保留的, 剔除条数)`。"""
    config = _cached()
    if not config.loaded:
        return names, 0
    kept = [name for name in names if not config.excluded(name)]
    return kept, len(names) - len(kept)


def verify_codes(payload: dict[str, Any]) -> list[str]:
    """对照真实板块截面校验清单里的 `(code, name)` 配对，返回不一致项说明。

    ## 为什么必须有这道校验（本轮实测踩过）

    按**名称**与按**代码**两条路都匹配，好处是上游改名后代码仍能挡住；
    代价是**代码写错会把另一个真实板块静默删掉**。实测：补录时按序号"猜"的
    几个代码，真实身份分别是

        BK0486.DC → 传媒      BK1622.DC → 镍
        BK0505.DC → 中字头    BK1623.DC → 钼

    而清单里写的是「标准普尔 / 科创板做市商 / 创业板综 / 科创板做市股」——
    名称匹配不上、代码却命中，于是被删的是**镍和钼**，
    榜单上只是"少了两个板块"，不报任何错。这与文件头担心的关键词误伤同源。

    校验口径：对清单里每个 code，若截面里存在该 code 但名称与清单不符 →
    报不一致；若名称存在于截面但 code 与之不符 → 也报。
    两者都为空才说明清单与数据源一致。

    调用点：`service._build()` 每次组装时校验一次（截面已缓存，成本可忽略），
    结果非空会写进 `source_notes` —— 界面上看得见，而不是只躺在日志里。
    """
    config = _cached()
    if not config.loaded or not payload:
        return []
    by_code: dict[str, str] = {}
    by_name: dict[str, str] = {}
    for name, info in payload.items():
        code = str((info or {}).get("code") or "").strip().upper()
        if code:
            by_code[code] = str(name)
            by_name[str(name)] = code
    problems: list[str] = []
    for item in config.entries:
        code = str(item.get("code") or "").strip().upper()
        name = str(item.get("name") or "").strip()
        real = by_code.get(code)
        if real is not None and real != name:
            problems.append(f"{code} 清单写「{name}」实际是「{real}」")
        elif real is None:
            # 代码不在当日截面里：可能是已下架板块（正常），不报
            other = by_name.get(name)
            if other and other != code:
                problems.append(f"「{name}」清单写 {code} 实际是 {other}")
    return problems


def disclosure(dropped: int, *, loaded: bool, gap: str = "") -> str:
    """给 `source_notes` 用的一句话（剔了几个、按哪个清单）。

    口径必须写在界面上：不然"榜单里没有江苏板块"这件事，
    在用户看来和"数据源没给这个板块"完全一样。
    """
    if gap:
        return f"⚠️ 板块剔除未生效：{gap}"
    if not dropped:
        return ""
    return (f"已按 configs/{CONFIG_NAME} 剔除 {dropped} 个非行业/概念板块"
            "（地域 / 指数成分 / 持仓属性 / 风格规模 / 市场统计）")


__all__ = [
    "CONFIG_NAME",
    "PROJECT_ROOT",
    "SectorExclude",
    "current",
    "disclosure",
    "filter_names",
    "filter_payload",
    "is_excluded",
    "load_sector_exclude",
    "reset_cache",
    "verify_codes",
]
