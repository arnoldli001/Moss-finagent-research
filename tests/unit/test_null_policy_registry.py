"""判据：列级 NULL 语义**登记表**（`configs/column_null_policy.yaml`）与它的读取器。

## 它守的是什么（用户 2026-10-02 裁定 1）

`akshare_connector._quant_column_points` 的第②支（表里有这只票的行、但这一列对它
**历史上一个非 NULL 都没有**）原先**分不开**"合法无值"（从未分红 ⇒ 股息率没有定义）
与"数据洞"（该入库没入库 ⇒ 真缺口），只能一律 fail-closed。裁定 1 要求建登记表
把这件事从**猜语义**变成**登记 + 证据**。

## 这张表**自己**也必须被守（否则它是一张无人核对的豁免名单）

| # | 判据（函数名） | 防的是 |
|---|---|---|
| 1 | `test_registry_is_self_consistent` | 枚举写错 / 字段缺失 / 列名重复 / 证据凭空写 |
| 2 | `test_registry_path_is_a_single_fact_source` | 表被挪走或复制成第二份（改了不生效） |
| 3 | `test_missing_file_is_treated_as_unregistered` | 读不到 ⇒ 默认豁免（多豁免，最危险方向） |
| 4 | `test_corrupt_yaml_is_treated_as_unregistered` | 坏文件被静默当成空表以外的东西 |
| 5 | `test_duplicate_column_key_invalidates_the_whole_registry` | PyYAML 静默保留最后一个 ⇒ 抄两行只改一行没人发现 |
| 6 | `test_bad_entry_is_dropped_not_the_whole_table` | 一条写坏就把整张表废掉（会让人不敢改表）；或反过来：坏条目照旧生效 |
| 7 | `test_reader_never_raises_on_garbage` | 读取器在异常输入上抛 ⇒ 取数链上多出一个没人预期的失败点 |

逐条说清楚各自防什么：

* 1 ：`null_is` 用了第三个取值（如 `"ok"`/`"tolerable"`）、`why`/`evidence` 空着、
       `column` 与映射键不一致、或同一个列名写两次 —— 这张表是**豁免名单**，
       写错一个字的后果是"某个真缺口从此不再进缺陷清单"，必须当场红；
       另外**证据必须是可复现的**（`文件:行` 或一条查询），`legitimate` 条目
       拿不出这两样就是**凭印象**；
* 2 ：表在 `configs/column_null_policy.yaml`（本仓库配置的唯一家）——
       换个位置/抄第二份 ⇒ 改了不生效；
* 3/4/5 ：**失败方向**。表读不到时必须回到"没有这张表"的行为（fail-closed），
       绝不能变成"没有约束 ⇒ 都可以豁免"；
* 6 ：坏条目**只废它自己**（未登记 = fail-closed），不废整表 —— 否则一次笔误
       会把所有已登记的合法无值重新变成缺陷（人人自危 ⇒ 很快会有人把判据关掉）；
* 7 ：读取器承诺"绝不抛"（调用点在取数链上，抛出去就是一个新的失败形状）。

## 纪律

* 枚举与字段名一律从 `null_policy` **现取**（`NULL_IS_VALUES` / `REQUIRED_FIELDS`），
  判据里不抄字面量 —— 抄一份就必然漂移；
* 造坏文件一律写进 `tmp_dir`（既有 conftest 夹具），**不碰**真的 `configs/`；
  只 monkeypatch 读取器的路径常量（表本身仍走真实解析与真实文件）。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from src.infrastructure.connectors import null_policy

#: 真的登记表（判据 1/2 读它；造坏文件的那几条**不读它**）。
REGISTRY = Path(__file__).resolve().parents[2] / "configs" / "column_null_policy.yaml"


class _StrictLoader(yaml.SafeLoader):
    """遇到**重复键**就抛的 loader（PyYAML 默认静默保留最后一个）。

    判据不复用读取器里的重复键检测：那是被测对象自己的一部分，
    用它来证明"表里没有重复键"等于让被测者给自己打分（实测：`yaml.safe_load`
    对 `{a: 1, a: 2}` 返回 `{'a': 2}`，**不报错**）。
    """

    def construct_mapping(self, node: Any, deep: bool = False) -> Any:
        seen: set[Any] = set()
        for key_node, _value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if isinstance(key, str) and key in seen:
                raise ValueError(f"登记表里有重复列名：{key!r}")
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


def _load_registry() -> dict[str, Any]:
    return yaml.load(REGISTRY.read_text(encoding="utf-8"), Loader=_StrictLoader)


def _write(monkeypatch: pytest.MonkeyPatch, tmp_dir: str, text: str) -> Path:
    """把一段 YAML 写到 `tmp_dir` 并把它设成读取器眼里的登记表。"""
    path = Path(tmp_dir) / "column_null_policy.yaml"
    path.write_text(text, encoding="utf-8")
    monkeypatch.setattr(null_policy, "_POLICY_PATH", path)
    return path


def _entry(key: str, **overrides: str) -> str:
    """**一条**合法登记的 YAML 片段（各判据只覆盖自己关心的那个字段）。

    `key` 是映射键（判据要造出"键与 `column` 字段不一致"的形状，所以两者分开传）；
    为什么按"条目片段"拼而不是整份文件：一份文件只能有一个顶层 `columns:`
    —— 拼两份整文件会撞出重复顶层键（那正好会被判据 5 的机制废掉整表，
    于是判据 6 就永远红，而不是在测它自己那件事）。
    """
    fields: dict[str, str] = {
        "column": key,
        "null_is": "legitimate",
        "why": "没分红 ⇒ 股息率没有定义",
        "evidence": "src/infrastructure/connectors/akshare_connector.py:465-466",
    }
    fields.update(overrides)
    body = "\n".join(f"    {k}: {v}" for k, v in fields.items())
    return f"  {key}:\n{body}\n"


def _registry(*entries: str) -> str:
    """把若干条目拼成一份完整的登记表文本。"""
    return "version: 1\ncolumns:\n" + "".join(entries)


# ============================================================
# ① 登记表自洽（枚举与字段齐全是**硬判据**）
# ============================================================


def test_registry_is_self_consistent() -> None:
    """★ 每一条都必须：列名与键一致、`null_is` 在枚举内、`why`/`evidence` 非空、
    且**证据可复现**（`文件:行` 或一条查询）。列名不许重复。

    为什么这些是硬判据而不是"最好有"：这张表是**豁免名单** ——
    写错一个字的后果是某个真缺口从此不再进缺陷清单，而缺陷清单里少一条
    **没有任何症状**（不会有人发现）。所以宁可在这里红。
    """
    data = _load_registry()
    assert isinstance(data, dict), "登记表顶层必须是映射"
    columns = data.get("columns")
    assert isinstance(columns, dict) and columns, (
        "登记表里必须真的有条目（空表 = 这套机制没落地，判据会静默恒绿）")

    for key, entry in columns.items():
        assert isinstance(key, str) and key.strip(), f"列名不合法：{key!r}"
        assert isinstance(entry, dict), f"{key}: 条目必须是映射"
        for field in null_policy.REQUIRED_FIELDS:
            assert field in entry, f"{key}: 缺 {field}（四个字段缺一不可）"
        assert entry["column"] == key, (
            f"{key}: `column` 字段与映射键不一致（{entry['column']!r}）——"
            "复制粘贴错行会让**另一个真缺口**的列被豁免掉")
        assert entry["null_is"] in null_policy.NULL_IS_VALUES, (
            f"{key}: null_is={entry['null_is']!r} 不在枚举 "
            f"{null_policy.NULL_IS_VALUES} 内")
        for field in ("why", "evidence"):
            value = entry[field]
            assert isinstance(value, str) and value.strip(), f"{key}: {field} 为空"
        # 证据必须**可复现**：要么指到源码行，要么给一条能跑的查询
        evidence = entry["evidence"]
        assert ("src/" in evidence and ":" in evidence) or "SELECT" in evidence, (
            f"{key}: evidence 既没有 `文件:行` 也没有可跑查询 —— 那就是凭印象写的")

    names = [str(e["column"]) for e in columns.values()]
    assert len(names) == len(set(names)), f"有重复列名：{names}"

    # 两个枚举取值都必须在表里出现（否则判据 4「hole ⇒ fail-closed」没有真实靶子）
    kinds = {str(e["null_is"]) for e in columns.values()}
    assert null_policy.LEGITIMATE in kinds, "没有任何 legitimate 条目 ⇒ 本轮的机制是空转"
    assert null_policy.HOLE in kinds, "没有任何 hole 条目 ⇒ 判据没有真实靶子"


def test_registry_path_is_a_single_fact_source() -> None:
    """★ 表的**位置**只有一份：`null_policy.policy_path()` 就是 `configs/` 下那个文件。

    防的是"表挪走了/抄了第二份" —— 本地实测过同一 key 写在 3 处只改 1 处的形状；
    那类缺陷的症状是**改了不生效**（没有任何报错）。
    """
    path = null_policy.policy_path()
    assert path.exists(), f"登记表不存在：{path}"
    assert path.resolve() == REGISTRY.resolve(), (
        f"读取器读的（{path}）与判据核对的（{REGISTRY}）不是同一个文件")
    assert path.parent.name == "configs", f"登记表必须住在 configs/ 下：{path}"


# ============================================================
# ② 失败方向：读不到 ⇒ 未登记（fail-closed），绝不抛
# ============================================================


def test_missing_file_is_treated_as_unregistered(
    tmp_dir: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★★ 文件不存在 ⇒ 按未登记（返回 None / False），**不许**按"没约束 ⇒ 豁免"。"""
    missing = Path(tmp_dir) / "not_there.yaml"
    monkeypatch.setattr(null_policy, "_POLICY_PATH", missing)

    assert null_policy.policy_for("dv_ttm") is None
    assert null_policy.null_is_legitimate("dv_ttm") is False


def test_corrupt_yaml_is_treated_as_unregistered(
    tmp_dir: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★★ 坏 YAML / 顶层不是映射 / 没有 columns / 条目不是映射 ⇒ 一律未登记，不抛。

    四种坏形状都要过一遍：它们各自的"合理误读"方向都不同
    （坏 YAML 可能被当成空串、`columns: []` 可能被当成"没有列"而放过）。
    """
    bad = [
        "columns: [",                                  # 语法坏
        "- 1\n- 2\n",                                  # 顶层是序列
        "version: 1\n",                                # 没有 columns
        "columns: 3\n",                                # columns 不是映射
        "columns:\n  dv_ttm: legitimate\n",            # 条目不是映射（值是标量）
        "\t tabs are not yaml\n  dv_ttm: x\n",         # 制表符 ⇒ 解析器直接抛
    ]
    for i, text in enumerate(bad):
        _write(monkeypatch, tmp_dir, text)
        assert null_policy.policy_for("dv_ttm") is None, f"第 {i} 种坏文件被当成了有效登记"
        assert null_policy.null_is_legitimate("dv_ttm") is False, f"第 {i} 种坏文件"


def test_duplicate_column_key_invalidates_the_whole_registry(
    tmp_dir: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★★ 同一个列名写两次 ⇒ **整表**作废（不是"后写的赢"）。

    为什么必须这么狠：PyYAML 遇到重复键**静默保留最后一个**。于是
    "把 `dv_ttm` 抄成两行、只改了其中一行"会得到一个**没人能看出**的登记表，
    而它决定的恰恰是"这条 NULL 豁不豁免" —— 正是本仓库最贵的缺陷形状
    （同一个 key 写在两处，只改一处 ⇒ 静默不一致）。
    """
    text = (
        "version: 1\ncolumns:\n"
        "  dv_ttm:\n    column: dv_ttm\n    null_is: hole\n"
        "    why: 数据洞\n    evidence: SELECT COUNT(*) FROM x\n"
        "  dv_ttm:\n    column: dv_ttm\n    null_is: legitimate\n"
        "    why: 合法无值\n    evidence: src/a.py:1\n"
    )
    _write(monkeypatch, tmp_dir, text)

    assert null_policy.policy_for("dv_ttm") is None, "重复键被静默取舍了（歧义必须整表作废）"
    assert null_policy.null_is_legitimate("dv_ttm") is False


@pytest.mark.parametrize("broken", [
    {"null_is": "ok"},                      # 第三个枚举取值
    {"null_is": "LEGITIMATE"},              # 大小写漂移
    {"why": "''"},                          # 理由空着
    {"evidence": "''"},                     # 证据空着
    {"evidence": "123"},                    # 证据不是文本（类型错）
    {"column": "dv_ratio"},                 # column 与键不一致（粘贴错行）
])
def test_bad_entry_is_dropped_not_the_whole_table(
    tmp_dir: str, monkeypatch: pytest.MonkeyPatch, broken: dict[str, str],
) -> None:
    """★ 一条写坏 ⇒ **只废它自己**（未登记 = fail-closed），别的条目照旧生效。

    两个方向都要防：
      · 写得再坏也照旧生效 ⇒ 枚举/字段判据形同虚设（**多豁免**）；
      · 一条坏就废整表 ⇒ 一次笔误把所有已登记的合法无值重新变成缺陷，
        很快会有人把这张表删掉（**护栏被关**）。

    ⚠️ "证据是不是**可复现**"（`文件:行` / 一条查询）**故意不在这里判**：
    那要靠文本启发式，误杀一个写法正常的条目 ⇒ 条目明明在文件里却**不生效**
    （"改了不生效"是本仓库实测过的另一类事故）。它由判据 1 在**真的登记表**上守，
    红了人看得见；运行期只判**无歧义**的那几条（枚举/非空/键与 column 一致/类型）。
    """
    good = _entry("dv_ratio", evidence="src/a.py:1")
    bad = _entry("dv_ttm", **broken)
    _write(monkeypatch, tmp_dir, _registry(good, bad))

    assert null_policy.policy_for("dv_ratio") is not None, "好条目被坏条目连坐废掉了"
    assert null_policy.null_is_legitimate("dv_ratio") is True
    assert null_policy.policy_for("dv_ttm") is None, (
        f"坏条目照旧生效了（{broken}）—— 枚举/字段判据是摆设")
    assert null_policy.null_is_legitimate("dv_ttm") is False


def test_reader_never_raises_on_garbage(
    tmp_dir: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★★ 读取器对外承诺**绝不抛**：路径指向目录、二进制垃圾、空文件、超长行都不许炸。

    为什么：调用点在取数链上（`_quant_column_points` 的第②支）。这里抛出去
    就是一个**没人预期的新失败形状**，而且会把"这一列到底豁不豁免"变成
    "登记表能不能读"的运气问题。
    """
    path = Path(tmp_dir) / "dir_not_file"
    path.mkdir()
    monkeypatch.setattr(null_policy, "_POLICY_PATH", path)
    assert null_policy.null_is_legitimate("dv_ttm") is False

    for i, blob in enumerate((b"", b"\x00\x01\x02\xff\xfe", b"columns: {a: 1}\n" * 500)):
        (Path(tmp_dir) / f"blob{i}.yaml").write_bytes(blob)
        monkeypatch.setattr(null_policy, "_POLICY_PATH", Path(tmp_dir) / f"blob{i}.yaml")
        assert null_policy.policy_for("dv_ttm") is None, f"第 {i} 种垃圾输入"

    # 非字符串的列名也不许炸（类型注解不是运行期保证）
    for weird in (None, 3, b"dv_ttm", ["dv_ttm"], ""):
        assert null_policy.policy_for(weird) is None
        assert null_policy.null_is_legitimate(weird) is False
