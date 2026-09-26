"""「查看原文」的两个**结构性**约束（源码级，因为 web 没有 JS 测试运行器）。

## 起因：一个把服务端打穿的 bug（2026-09-26 实测）

点一次「查看原文」，`GET /intel/item/{hash}` 被请求了 **2248 次 / 3 秒**，
而界面上永远停在"正在读取全文…"。成因是取全文的 `useEffect` 把**自己会写**的
state（`details`）放进了依赖数组：

    ① effect 跑 → `setDetails(loading)` → 请求发出
    ② `details` 变了 → 而它是依赖 → React 先跑 cleanup
    ③ cleanup 把 `alive = false`（**这次响应注定被丢弃**）并删掉 loading 记录
    ④ 新 effect 看到缓存里有记录 → `return`（不发请求）
    ⑤ cleanup 那次 `setDetails` 生效 → `details` 又变 → 回到 ①

每一轮的响应都被丢掉，同时**每一轮都发一个新请求**。它有两个"看不出来"：
`tsc` 完全通过（类型都对），页面也不报错（只是永远在加载）。

所以这里钉住两件事，两件都不会有任何运行时错误：

  1. 取全文那个 effect 的依赖数组**只允许** `[detailHash, detailTick]` ——
     不许出现 `details`（那是上面那个环的入口）；
  2. 摘要与标题的**去重**必须走 `trimRepeatedHead`（星球帖的标题就是正文前
     60 字，不去重的话同一句话在列表里连着出现两遍）。
"""

from __future__ import annotations

import re
from pathlib import Path

_WEB = Path(__file__).resolve().parents[2] / "web" / "src"
_TSX = _WEB / "components" / "intel" / "IntelFeedTab.tsx"
_API = _WEB / "intelApi.ts"


def _fetch_effect_deps(tsx: str) -> str:
    """取出"按 hash 取全文"那个 effect 的依赖数组原文（不含方括号）。

    定位方式是**找调用点**（`fetchIntelItem(detailHash)`），再往后找第一个
    `}, [` —— 比"数第几个 useEffect"稳：上面还挂着别的 effect，
    顺序一变按序号取就会取错，而取错的表现是这条用例静默失效。
    """
    at = tsx.find("fetchIntelItem(detailHash)")
    assert at != -1, "找不到取全文的调用点（`fetchIntelItem(detailHash)`）"
    tail = tsx[at:]
    m = re.search(r"\n  \}, \[(?P<deps>[^\]]*)\]\);", tail)
    assert m, "取全文的 effect 后面找不到依赖数组"
    return m.group("deps")


def test_fulltext_effect_does_not_depend_on_its_own_state() -> None:
    """★ 依赖数组里不许有 `details`（那会形成每次响应都被丢弃的死循环）。"""
    deps = _fetch_effect_deps(_TSX.read_text(encoding="utf-8"))
    names = [d.strip() for d in deps.split(",") if d.strip()]
    assert "details" not in names, (
        "取全文的 effect 又依赖 `details` 了 —— 它自己会 setDetails，"
        "于是 setDetails → 依赖变 → cleanup 丢弃响应 → 再请求 …… "
        "实测点一次会发 2248 个请求且界面永远停在加载态")
    assert names == ["detailHash", "detailTick"], (
        f"依赖数组变了：{names}。加依赖之前先想清楚"
        "「这个 effect 会不会写它」—— 会写就不能当依赖")


def test_fulltext_effect_guards_by_requested_ref() -> None:
    """同一个 hash 只请求一次：靠 `requested` 这个 ref 记，不靠读 `details`。"""
    tsx = _TSX.read_text(encoding="utf-8")
    assert "requested.current.has(detailHash)" in tsx, (
        "少了「这条取过没有」的判据 —— 每渲染一次都会重新发请求")
    assert "requested.current.add(detailHash)" in tsx


def _fn_body(tsx: str, name: str) -> str:
    """取某个顶层函数声明的整段源码（到下一个 `function` 为止）。

    ⚠️ 不能用 `function X\\(\\{.*?\\n\\}`：函数签名本身是个多行对象类型，
    第一个 `\\n}` 就把签名截断了 —— 于是"函数体里有没有那一句"永远为假，
    用例会以"没接上"的假象失败（第一版就是这么挂的）。
    """
    marks = list(re.finditer(r"\n(?:export )?function ([A-Za-z_]\w*)\(", tsx))
    for i, m in enumerate(marks):
        if m.group(1) != name:
            continue
        end = marks[i + 1].start() if i + 1 < len(marks) else len(tsx)
        return tsx[m.start():end]
    raise AssertionError(f"找不到函数 {name}")


def test_summary_dedup_uses_the_shared_helper() -> None:
    """摘要与标题去重必须走 `trimRepeatedHead`（一处实现，三处调用点共用）。

    星球帖没有标题，服务端取正文前 60 字当标题 —— 不剥的话
    "标题 + 摘要"并排时同一句话出现两遍（列表 / 手机卡片 / 详情三处都会犯）。
    """
    tsx = _TSX.read_text(encoding="utf-8")
    assert "trimRepeatedHead(" in tsx, "摘要去重没接上 trimRepeatedHead"
    api = _API.read_text(encoding="utf-8")
    assert "export function trimRepeatedHead(" in api
    # `SummaryLine` 是所有"标题 + 摘要"并排处的唯一出口，去重必须长在它里面
    assert "trimRepeatedHead(" in _fn_body(tsx, "SummaryLine"), (
        "`SummaryLine` 里没做去重 —— 它的调用点（卡片/详情/组内条目）"
        "都会把标题原文再显示一遍")
