"""`configs/column_null_policy.yaml` 的**唯一**读取器（列级 NULL 语义登记表）。

## 它回答的问题（用户 2026-10-02 裁定 1）

某列出现 `IS NULL`，是**合法无值**（如从未分红 ⇒ 股息率没有定义），还是**数据洞**
（该入库没入库 ⇒ 真缺口、必须去修）？两者在数据上同形，只能靠**登记 + 证据**分开。

调用方只有一处：`akshare_connector._quant_column_points` 的空结果分流第②支
（表里有这只票的行、但这一列对它**历史上一个非 NULL 都没有**）。登记为
`legitimate` ⇒ 抛 `NoApplicableData(kind="not_applicable")`；登记为 `hole` 或
**未登记** ⇒ 保持普通 `DataFetchError`。

## 为什么放在 `connectors/` 而不是 catalog 侧

① `_quant_column_points` 在 `src/infrastructure/connectors/` 内，读取器与调用方同层
   就不必让 domain 反向依赖基础设施；② 登记表描述的是**这一列的取值语义**，
   而列名→指标的映射（`_QUANT_COLUMN_INDICATORS`）本来就在这个包里 —— 放在一起，
   "取数侧认哪些列"只有一个家；③ `src/infrastructure/catalog/**` 本轮不许改动。

## ★ 失败方向：**读不到 ⇒ 一律按未登记处理**（fail-closed）

`policy_for()` / `null_is_legitimate()` **绝不抛**。以下情形全部退化成"未登记"
（= 调用方保持普通 `DataFetchError`）：

| 情形 | 为什么这样定 |
|---|---|
| 文件不存在 / 读不动 | 登记表是本轮新增的：**没有它的时候行为就是 fail-closed**，读不到必须回到那个行为，不许"读不到 ⇒ 没有约束 ⇒ 都可以豁免" |
| YAML 语法坏 / 顶层不是映射 / 没有 `columns` 映射 | 同上 |
| **同一个列名写了两次** | PyYAML 默认**静默保留最后一个** —— 那正是"同一个 key 写在两处、只改一处"的静默不一致形状（本项目最贵的缺陷）。歧义 ⇒ 整表作废 |
| 某条 `null_is` 取值不在枚举里 / `column` 与映射键不一致 / `why`/`evidence` 为空 / 该条不是映射 | **该条**作废（按未登记）；枚举与字段齐全是判据里的硬判据 |

反向（"读不到 ⇒ 默认豁免"）就是**多豁免**：真缺口会被说成"本来就没有"，
是本项目最危险的方向 —— 而多豁免比多报假阳性难发现得多。

## 为什么不做缓存

① 这个函数只在**空结果**那条罕见路径上被调用（`_quant_column_points` 的
第②支），一次文件读的代价可以忽略；② 人改完登记表立刻生效，不需要重启
（"改了不生效"是本仓库实测过的另一类事故）；③ 缓存会让判据必须处理
"文件改了但缓存没失效"，为一个不需要的性能目标引入一整类新失效。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Final

logger = logging.getLogger(__name__)

#: 合法无值（NULL 是该口径的正确取值，不是缺口）。
LEGITIMATE: Final[str] = "legitimate"
#: 数据洞（NULL 表示该入库没入库 —— 真缺口，必须去修）。
HOLE: Final[str] = "hole"

#: `null_is` 的**全部**合法取值（顺序即文档顺序；枚举是判据里的硬判据）。
NULL_IS_VALUES: Final[tuple[str, str]] = (LEGITIMATE, HOLE)

#: 每条必须具备的字段（缺一条该条作废）。`column` **必须与映射键逐字相同**。
REQUIRED_FIELDS: Final[tuple[str, ...]] = ("column", "null_is", "why", "evidence")

#: 登记表路径：与 `src/core/agent_meta.py:16` 同一套解析方式（`parents[2]` 是 `src/`，
#: 本文件在 `src/infrastructure/connectors/` 下，故仓库根是 `parents[3]`）。
_POLICY_PATH: Final[Path] = (
    Path(__file__).resolve().parents[3] / "configs" / "column_null_policy.yaml"
)


def policy_path() -> Path:
    """登记表文件路径（**只此一份**：判据要能核对它真的指向 `configs/`）。"""
    return _POLICY_PATH


def _duplicate_keys(node: Any) -> list[str]:
    """在 YAML **语法树**上找重复键（不构造任何对象，纯遍历）。

    为什么不用 `yaml.safe_load` 的结果判断：PyYAML 遇到重复键**不报错**，
    后写的静默覆盖先写的（实测：`{a: 1, a: 2}` ⇒ `{'a': 2}`）。于是
    "把 `dv_ttm` 抄成两行、只改其中一行"会得到一个**没人能看出**的登记表，
    而它决定的恰恰是"这条 NULL 豁不豁免"。所以重复键在这里必须被看见。
    """
    import yaml

    dupes: list[str] = []
    root = yaml.compose(node)
    stack = [root]
    while stack:
        cur = stack.pop()
        if isinstance(cur, yaml.MappingNode):
            seen: set[Any] = set()
            for key_node, value_node in cur.value:
                key = getattr(key_node, "value", None)
                if key in seen:
                    dupes.append(str(key))
                seen.add(key)
                stack.append(value_node)
        elif isinstance(cur, yaml.SequenceNode):
            stack.extend(cur.value)
    return dupes


def _entry_or_none(key: Any, raw: Any) -> dict[str, Any] | None:
    """校验一条登记；不合格 ⇒ `None`（= 按未登记处理，fail-closed）。

    `column` 必须与映射键**逐字相同**：这一条防的是复制粘贴错行
    （把 A 列的语义贴到 B 列的键下）—— 那种错**看起来完全正常**，
    却会让另一个真缺口的列被豁免掉。
    """
    if not isinstance(key, str) or not key.strip():
        return None
    if not isinstance(raw, dict):
        return None
    column = raw.get("column")
    if not isinstance(column, str) or column.strip() != key.strip():
        return None
    null_is = raw.get("null_is")
    if null_is not in NULL_IS_VALUES:
        return None
    out: dict[str, Any] = {"column": key.strip(), "null_is": null_is}
    for field in ("why", "evidence"):
        value = raw.get(field)
        if not isinstance(value, str) or not value.strip():
            return None
        out[field] = value
    return out


def _load() -> dict[str, dict[str, Any]]:
    """读**整张**登记表；任何读不到/坏掉的情形 ⇒ 空表（= 全部未登记）。**绝不抛**。

    空表与"表里没这一列"在调用方看来是同一件事 —— 这正是设计意图：
    失败方向只有一个，就是 fail-closed。
    """
    try:
        import yaml

        text = _POLICY_PATH.read_text(encoding="utf-8")
        dupes = _duplicate_keys(text)
        if dupes:
            logger.warning(
                "column_null_policy 有重复列名 %s ⇒ 整表按未登记处理（fail-closed）",
                sorted(set(dupes)))
            return {}
        data = yaml.safe_load(text)
    except Exception as exc:  # noqa: BLE001 读不到/坏了 ⇒ 未登记，绝不抛
        logger.warning("column_null_policy 读不到（按未登记处理）: %s", exc)
        return {}
    if not isinstance(data, dict):
        logger.warning("column_null_policy 顶层不是映射（按未登记处理）")
        return {}
    columns = data.get("columns")
    if not isinstance(columns, dict):
        logger.warning("column_null_policy 没有 columns 映射（按未登记处理）")
        return {}

    out: dict[str, dict[str, Any]] = {}
    for key, raw in columns.items():
        entry = _entry_or_none(key, raw)
        if entry is None:
            logger.warning("column_null_policy 里 %r 这条不合格 ⇒ 按未登记处理", key)
            continue
        out[entry["column"]] = entry
    return out


def policy_for(column: object) -> dict[str, Any] | None:
    """这一列的登记条目；**未登记 / 读不到 / 该条不合格 ⇒ `None`**（绝不抛）。

    返回的 dict 至少含 `column` / `null_is` / `why` / `evidence` 四个键
    （与 YAML 里的字段同名，调用方与判据读的是同一套名字）。
    """
    if not isinstance(column, str) or not column.strip():
        return None
    try:
        return _load().get(column.strip())
    except Exception as exc:  # noqa: BLE001 兜底：本函数对外承诺"绝不抛"
        logger.warning("column_null_policy 读取异常（按未登记处理）: %s", exc)
        return None


def null_is_legitimate(column: object) -> bool:
    """这一列的 NULL 是否**已登记为合法无值**？

    ⚠️ 默认必须是 `False`：**未登记 ⇒ fail-closed**（调用方报普通 `DataFetchError`）。
    写反（未登记 ⇒ True）就是多豁免 —— 真缺口会被说成"本来就没有"。
    """
    entry = policy_for(column)
    return bool(entry) and entry.get("null_is") == LEGITIMATE
