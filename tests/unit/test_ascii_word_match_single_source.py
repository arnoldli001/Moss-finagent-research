"""「ASCII 全词匹配」只有**一处实现** —— 判据 + 两处历史回归（`CHG-0155`）。

## 现场（实测，不是推断）

同一条判据曾经有**两份实现、两套规则、两个字符串域**：

| 位置 | 规则 | 分歧样本 |
|---|---|---|
| `synonym_dict`（旧 `_ascii_token_aligned`） | 两侧都要非字母数字 | `fed:` ⊄ `fed:policy_range` |
| `supervisor._kw_hit` | 只在关键词该端是字母数字时才要求边界 | `fed:` ⊂ `fed:policy_range` |

`scripts/_audit_matching_layer.py` part ② 实测：11 对样本里 **5 对不一致**
（`fed:` / `cal:` / `ind:` / `mkt:` / `roe`）—— 同一个别名
**一条路径认得出、另一条认不出**。两边各自都是为修一个真实事故才长成那样：

* 严的那版为挡 `pe` ⊂ `fedtargetupper`（曾拿"市盈率"的问句去取**美联储利率**）；
* 松的那版为救 `fed:` 这类**以分隔符结尾的前缀关键词**（无条件要求右边界会让
  三个 Agent 立刻不可达）。

## 本文件钉两件事

1. **行为**：上面两类历史回归**同时**成立（严的那条不许复活，松的那条不许退化）。
2. **结构**：两条调用路径必须**调同一个函数** —— 用探针替换实现、看两处是否都被点到。
   ⚠️ 只比对两边返回值是**不够的**：值相等但两份拷贝并存，正是这次缺陷的成因
   （本项目纪律：一条不会红的判据等于没有判据）。

## 自证

`test_the_old_strict_rule_would_fail_the_table` 把**旧的严规则**就地重现，
断言它在同一张回归表上**必然给出不同答案** —— 证明这张表有鉴别力，不是恒绿。
"""
from __future__ import annotations

import pytest

from src.infrastructure.catalog import synonym_dict as sd
from src.infrastructure.catalog.synonym_dict import ascii_full_word

#: 回归表：`(needle, haystack, 期望)`。左半是"必须挡住的过匹配"，
#: 右半是"必须放行的前缀式/同族别名"——两类都在真实事故里出现过。
CASES: tuple[tuple[str, str, bool], ...] = (
    # ---- 必须挡住：纯字母别名被更长的英文单词吞掉（历史事故：取错指标）----
    ("pe", "ind:penetration:ai大模型应用", False),
    ("pe", "fedtargetupper", False),
    ("pe", "fedpolicyrange", False),
    ("pb", "pboc:rate", False),
    ("ps", "eps_ttm", False),
    # ---- 必须放行：正常命中 ----
    ("pe", "pe:600036", True),
    ("pe", "pe(ttm):600036", True),
    ("pb", "pb:600036", True),
    ("dv_ratio", "dv_ratio:600036", True),
    ("stock_close", "stock_close:600036", True),
    # ---- 必须放行：**以分隔符结尾的前缀式关键词**（第一版就错在这里）----
    ("fed:", "fed:policy_range", True),
    ("cal:", "cal:cpi", True),
    ("ind:", "ind:sw_third_pe_ttm", True),
    ("mkt:", "mkt:turnover:total", True),
    ("sw_", "sw_third_pb:all", True),
    # ---- 中文走子串语义（不经本函数，此处只钉"不经它"这一事实）----
)


@pytest.mark.parametrize("needle,haystack,expected", CASES)
def test_ascii_word_boundary_table(needle: str, haystack: str,
                                   expected: bool) -> None:
    """★ 行为判据：两类历史回归必须**同时**成立。"""
    assert ascii_full_word(haystack, needle) is expected, (
        f"{needle!r} 在 {haystack!r} 里的判定错了 —— "
        f"严的那版会漏掉前缀式关键词，松的那版会让 pe 吞掉 penetration")


def test_the_old_strict_rule_would_fail_the_table() -> None:
    """★ 自证：旧的严规则在同一张表上**必然**给出不同答案。

    没有这一条，"回归表全绿"可能只是因为这张表没有鉴别力。
    """
    def old_strict(container: str, part: str) -> bool:
        """旧的 `_ascii_token_aligned`：两侧都要非字母数字（不管 part 两端）。"""
        start = container.find(part)
        while start != -1:
            end = start + len(part)
            left_ok = start == 0 or not container[start - 1].isalnum()
            right_ok = end == len(container) or not container[end].isalnum()
            if left_ok and right_ok:
                return True
            start = container.find(part, start + 1)
        return False

    disagreements = [(n, h, exp) for n, h, exp in CASES
                     if old_strict(h, n) is not exp]
    assert disagreements, (
        "旧规则竟然与回归表完全一致 ⇒ 这张表证明不了这次收敛的必要性")
    # 至少要能抓住前缀式那一类（`fed:` 是最有代表性的现场）
    assert any(n.endswith(":") for n, _h, _e in disagreements), (
        "旧规则应当在前缀式关键词上出错（fed:/cal:/ind:/mkt:），实际没有")


def test_both_call_paths_hit_the_same_implementation(monkeypatch) -> None:
    """★★ 结构判据：两条路径必须**调同一个函数**（不是"两边值恰好相等"）。

    做法：用探针替换唯一实现，记录调用来源，然后两处各触发一次。
    值相等但两份拷贝并存 = 本次缺陷的成因，必须能被这条判据挡住。
    """
    from src.orchestration import supervisor

    seen: list[tuple[str, str]] = []
    real = sd.ascii_full_word

    def probe(haystack: str, needle: str) -> bool:
        seen.append((haystack, needle))
        return real(haystack, needle)

    monkeypatch.setattr(sd, "ascii_full_word", probe)
    # ① 白名单侧（supervisor 在函数体内 import ⇒ 也从 sd 取，探针能拦住）
    assert supervisor._kw_hit("pb:600036", "pb") is True     # noqa: SLF001
    # ② 同义词侧（走 _match_span 的 ASCII 分支）
    assert sd._match_span("pettm", "pe") is not None         # noqa: SLF001
    assert len(seen) >= 2, (
        f"两条路径没有都走到唯一实现（只看到 {seen}）⇒ 有一处仍在用自己那份拷贝")


def test_chinese_keywords_do_not_go_through_the_ascii_rule(monkeypatch) -> None:
    """中文关键词走子串语义，**不**该被 ASCII 词边界规则处理（否则语义会变）。"""
    from src.orchestration import supervisor

    calls: list[tuple[str, str]] = []
    real = sd.ascii_full_word

    def probe(haystack: str, needle: str) -> bool:
        calls.append((haystack, needle))
        return real(haystack, needle)

    monkeypatch.setattr(sd, "ascii_full_word", probe)
    assert supervisor._kw_hit("股息率ttm:600036", "股息率") is True   # noqa: SLF001
    assert calls == [], "中文关键词不该经过 ASCII 词边界规则"


def test_normalized_domain_is_conservative() -> None:
    """归一化域（分隔符已被剥掉）里必须**保守**：边界信息没了就不许猜。

    `_match_span` 收到的是 `normalize_alias` 过的串，所以 `pe` ⊂ `pettm`
    在**没有边界信息**的域里应当被否决 —— 这是刻意的，不是缺陷。
    """
    assert sd._match_span("pettm", "pe") == 0        # noqa: SLF001
    assert sd._match_span("fedpolicyrange", "fed") == 0   # noqa: SLF001
