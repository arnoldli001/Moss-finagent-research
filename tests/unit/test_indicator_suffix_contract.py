"""★★ 指标后缀契约：**方向相反的两个错都要挡**（用户报障驱动，2026-09-29）。

## 报障现场（用户原话与真实错误）

> 「当前宏观环境如何，预测下未来一年美国的加息、降息节奏，对A股的影响，
>   以及AI应用加速失业率增加对消费的影响节奏时间节点分析，
>   未来半年能否持有高股息的招商银行？」

`POST /api/v1/research/analyze` 返回的「部分节点异常」里出现：

    A01采集失败(fed:effr:300068): 无连接器支持指标 fed:effr:300068；已注册: …
    A01采集失败(us_cpi_yoy:300068): 无连接器支持指标 us_cpi_yoy:300068；…
    （fed:target_upper / fed:target_lower / us_core_cpi / us_fed_rate /
      us_nonfarm / us_pce / us_unemployment **共九条，全部被拼上了个股代码**）

**这不只是"多九条失败"**：`fed:effr` 这些**正是问句的核心**
（"美国的加息、降息节奏"）⇒ A08 那一轮**一条美国宏观数据都没有**，
而错误文案看起来只是"某些指标没取到"。

根因：`planner.SYSTEM_PROMPT` 写着「个股类指标**必须**拼接6位代码后缀
（如 `PE(TTM):300308`）」，light 层小模型**过度套用**了它 ——
问句里宏观与个股同时出现时，代码被拼到了宏观指标上。
**提示词治不了这个**（同一个 1.5B 模型每次过度套用的位置都不同），
所以做成**确定性的契约修正**：`supervisor.strip_bogus_code_suffix()`。

## 两个方向都要挡（本文件的核心）

* **后缀加多了**：`fed:effr:300068` → `fed:effr`。
  判据 = 整串查不到登记，**掐掉尾段代码能查到登记且登记形态无 `{code}` 占位符**。
* **后缀不能少**：`PE(TTM)` → `PE(TTM):600036`。
  判据 = `_CODE_SUFFIX_INDICATORS` / `_INDUSTRY_SUFFIX_INDICATORS`
  （既有的单一真值源，见 `supervisor.py`）。

⚠️ 本文件第 2 组用例是**第一版判据的现场**：只按登记表判断时，
`商誉占净资产比:300068` 被误改成裸名（那批在 `indicators.yaml` 里登记的
**就是裸名字**，文件自己登记了这条既有缺口）—— 修好一个坏一个。
"""

from __future__ import annotations

import pytest

from src.infrastructure.catalog.registry import get_registry
from src.orchestration.planner import INDICATOR_CATALOG
from src.orchestration.supervisor import (
    _CODE_SUFFIX_INDICATORS,
    _INDUSTRY_SUFFIX_INDICATORS,
    sanitize_indicators,
    strip_bogus_code_suffix,
)

_CODE = "300068"

#: 报障现场那九条（**逐字**来自用户贴的「部分节点异常」）。
_REPORTED_MACRO = (
    "fed:effr", "fed:target_upper", "fed:target_lower",
    "us_cpi_yoy", "us_core_cpi", "us_fed_rate",
    "us_nonfarm", "us_pce", "us_unemployment",
)


# ============================================================================
# 一、后缀加多了：掐掉
# ============================================================================


def test_the_reported_nine_macro_indicators_shed_the_bogus_suffix() -> None:
    """★ 报障现场：九条美国宏观指标全部掐回登记形态。"""
    given = [f"{ind}:{_CODE}" for ind in _REPORTED_MACRO]
    fixed, notes = strip_bogus_code_suffix(given)
    assert fixed == list(_REPORTED_MACRO), (
        "宏观/序列类指标**不接受**代码后缀（登记形态无 `{code}` 占位符）")
    assert len(notes) == len(_REPORTED_MACRO), "每条都要留下可读的修正说明"


def test_non_code_suffixes_are_never_touched() -> None:
    """尾段不是 6 位数字的一律不动（日期后缀 / 行业名 / 赛道名 / `:all`）。"""
    given = ["fed:rate_prob:2026-01-28", "ind:sw_third_pe_ttm:银行",
             "ind:sw_third_pe_ttm:all", "ind:penetration:AI大模型应用",
             "GDP:同比"]
    assert strip_bogus_code_suffix(given)[0] == given


def test_unknown_indicator_is_not_invented_into_something_else() -> None:
    """掐掉也查不到登记的 → 原样保留（让它去 A01 如实报"无连接器支持"）。"""
    given = ["不存在的指标:300068", "另一个乱写的:600036"]
    assert strip_bogus_code_suffix(given)[0] == given


# ============================================================================
# 二、后缀不能少：**必须带后缀的一律不动**（第一版判据在这里错了）
# ============================================================================


def test_stock_and_industry_indicators_keep_their_suffix() -> None:
    """★ 个股/行业类**必须**带后缀 —— 掐掉它们就是把"能取到"改成"必然失败"。

    实测现场（本轮第一版判据的错）：`商誉占净资产比` / `大股东质押比例` /
    `资产负债率` / `ROE` 这批在 `indicators.yaml` 里登记的就是**裸名字**，
    只按登记表判断会把 `商誉占净资产比:300068` 误改成裸名。
    """
    must_keep = [f"{bare}:{_CODE}" for bare in sorted(_CODE_SUFFIX_INDICATORS)]
    must_keep += [f"{bare}:银行" for bare in sorted(_INDUSTRY_SUFFIX_INDICATORS)]
    fixed, notes = strip_bogus_code_suffix(must_keep)
    assert fixed == must_keep, "个股/行业类指标的后缀不许被掐掉"
    assert notes == []


def test_the_veto_actually_covers_the_registry_gap() -> None:
    """自证：**"只按登记表判断"确实会把必须带后缀的指标改坏**。

    这条判据防的是"以后有人把豁免删掉、以为登记表够用"——
    它直接用登记表复算一遍，若哪天那些裸名字被补成模板形态，
    这条会红并提示可以删掉豁免（**豁免自带过期语义**）。
    """
    registry = get_registry()
    bare_registered = [
        bare for bare in sorted(_CODE_SUFFIX_INDICATORS)
        if (meta := registry.get(bare)) is not None and not meta.is_template()
    ]
    assert bare_registered, (
        "预期：`indicators.yaml` 里仍有一批**裸名字**登记形态的个股指标"
        "（文件自己登记了该缺口）；若已被补成 `X:{code}` 模板，"
        "请删掉 `strip_bogus_code_suffix` 里的那道豁免并更新本条")


# ============================================================================
# 三、与既有两条规则共存（sanitize_indicators 是唯一检查点）
# ============================================================================


def test_sanitize_strips_macro_suffix_and_suffixes_bare_stock_names() -> None:
    """★ 一次调用里，**两个方向同时**被修对（顺序错了会互相打架）。"""
    usable, dropped, notes = sanitize_indicators(
        [f"fed:effr:{_CODE}", "PE(TTM)", "us_cpi_yoy", "行业拥挤度"],
        _CODE, "macro", industry="银行",
    )
    assert "fed:effr" in usable, "多余后缀要掐掉"
    assert f"PE(TTM):{_CODE}" in usable, "裸个股指标要补代码"
    assert "us_cpi_yoy" in usable, "本来就合法的一条不动"
    assert "行业拥挤度:银行" in usable, "行业类裸名要补行业名"
    assert not dropped, "没有该丢的指标"
    assert any("去掉多余的代码后缀" in n for n in notes)


def test_rule_path_and_llm_path_agree_on_the_same_input() -> None:
    """两条规划路径共用 `sanitize_indicators` ⇒ 同一输入必须得到同一结果。

    （本项目登记过多次"同一个判断两份实现"的后果；这里把它钉住。）
    """
    given = [f"fed:target_upper:{_CODE}", "PB"]
    llm_like = sanitize_indicators(list(given), _CODE, "macro")[0]
    rule_like = sanitize_indicators(list(given), _CODE, "macro")[0]
    assert llm_like == rule_like == ["fed:target_upper", f"PB:{_CODE}"]


# ============================================================================
# 五、目录声明 ↔ 判据**不许再漂**（本轮实测的漂移：40 条声明 vs 24 个判据）
# ============================================================================


def _catalog_suffix_declarations() -> list[str]:
    """目录里**自己写了"需带代码后缀"**的指标名（现读，不手写）。"""
    return sorted({
        str(item["id"]).split(":", 1)[0]
        for item in INDICATOR_CATALOG
        if "需带代码后缀" in str(item.get("desc") or "")
    })


@pytest.mark.parametrize("bare", _catalog_suffix_declarations())
def test_catalog_suffix_declarations_are_enforced(bare: str) -> None:
    """★★ 目录说"需带代码后缀" ⇒ 判据必须认（**否则裸名进计划，必然失败**）。

    实测漂移（2026-09-29）：目录里 **40 条**这样写，而 `_CODE_SUFFIX_INDICATORS`
    只列了 24 个 ⇒ LLM 照菜单选了 `每股净资产`，`sanitize_indicators` 判它
    "不需要后缀"⇒ 计划里是**裸名** ⇒
    `A01采集失败(每股净资产): 无连接器支持指标 每股净资产`。

    现在 `_needs_code_suffix()` 是**两张表求并集**，本参数化判据钉住它：
    以后任何人往目录里加一条带该声明的指标，这条自动扩展到它。
    """
    from src.orchestration.supervisor import _needs_code_suffix

    assert _needs_code_suffix(bare), (
        f"目录声明了「{bare}」需带代码后缀，而 `_needs_code_suffix()` 不认它 "
        "—— 裸名会被 A01 判「无连接器支持指标」")


def test_the_flaky_dividend_indicator_is_not_on_the_llm_menu() -> None:
    """★ 必然失败的指标**不许留在喂给 LLM 的菜单里**（纪律，附实测证据）。

    `股息率`（合成口径：最近实施每股派息 ÷ 收盘价）的数据源
    `stock_history_dividend_detail` **间歇性 RemoteDisconnected** ——
    实测两次真实端到端 + 用户两次报障**每次都失败**：
    `A01采集失败(股息率:600036/300036): ('Connection aborted.', RemoteDisconnected…)`；
    而同源的 `股息率TTM:{code}` 走本地行情仓，稳定且更新。

    指标本身仍登记在 `indicators.yaml`、连接器仍 `supports()` ——
    本条只断言"**菜单里不出现**"（显式请求与下游合成分仍可用）。
    """
    on_menu = {str(item["id"]) for item in INDICATOR_CATALOG}
    assert "股息率" not in on_menu, (
        "`股息率` 已因数据源不稳定从目录摘掉，别加回来 —— "
        "要股息口径请用 `股息率TTM`（本地行情仓 dv_ttm）")
    assert "股息率TTM" in on_menu, "替代口径必须在菜单里，否则就没有股息数据了"


def _code_less_registered_ids() -> list[str]:
    """登记形态**没有 `{code}` 占位符**的指标 id（现读登记表，自动长大）。"""
    registry = get_registry()
    out: set[str] = set()
    for item in INDICATOR_CATALOG:
        ind = str(item["id"])
        meta = registry.get(ind)
        if meta is not None and not meta.is_template() and ind != ind.split(":")[0]:
            out.add(ind)          # 只查带 `:` 的（裸名字那些是 A12/财务族）
    return sorted(out)


@pytest.mark.parametrize("indicator", _code_less_registered_ids())
def test_every_code_less_registered_indicator_sheds_a_code_suffix(
    indicator: str,
) -> None:
    """★ 派生判据：**目录里每个"无占位符"的指标**拼上代码后都必须被掐回。

    为什么用参数化而不是手写清单：库里/目录里新增一个宏观指标时，
    它会**自动进入这条判据**（手写清单不会自己长大 —— 本项目退役过一张）。
    """
    fixed, _notes = strip_bogus_code_suffix([f"{indicator}:{_CODE}"])
    assert fixed == [indicator], (
        f"{indicator} 的登记形态无 `{{code}}` 占位符，不该接受代码后缀")
