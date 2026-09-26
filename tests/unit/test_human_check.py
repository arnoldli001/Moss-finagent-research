"""真人检测（图形验证码）测试。

对应设计：§8.6.7 第 3 条。

## 为什么这个文件几乎全是"攻击视角"的用例

图形码是**安全组件**：它"能显示"不代表"防得住"。所以这里不测
"图能不能生成"（那是最容易的），而是逐条测**已知的绕过手法**：

| 用例 | 对应攻击 |
|---|---|
| 令牌必须是服务端签发 | 前端自己造"已通过"令牌 |
| 答案以哈希保存 | 内存/库被读走就拿到答案 |
| 一次性 | 抓到一个令牌后无限复用 |
| 绑定客户端 | 打码平台代答 / 令牌转卖 |
| TTL 生效 | 提前批量囤积令牌 |
| 尝试次数上限 | 暴力试答案 |
| 领取限流 | 脚本狂领图来训练/刷爆存储 |
| 答案不回显 | 响应里直接带答案（等于没验证） |
"""

from __future__ import annotations

import base64
import random
import re

import pytest

from src.domain.auth.human_check import (
    ALPHABET,
    ANSWER_LENGTH,
    IMAGE_MIME,
    ChallengeRateLimited,
    HumanChallengeService,
    png_data_uri,
    render_challenge_image,
)

#: JPEG 的魔数（SOI 标记）。用 JPEG 而非 PNG 的理由见
#: `render_challenge_image` 里的三次实测对照：同样的抗 OCR 强度下
#: JPEG 只有 PNG 的 40% 体积（噪点图 PNG 压不动）。
JPEG_MAGIC = b"\xff\xd8\xff"


@pytest.fixture()
def svc() -> HumanChallengeService:
    # 固定随机种子：答案可预测 → 断言更直接（生产用真随机）
    return HumanChallengeService(
        ttl_seconds=180, max_attempts=3, issue_limit=5, issue_window=60,
        rng=random.Random(20260923))


def issue(svc: HumanChallengeService, *, ip: str = "10.0.0.1"):
    """领一个挑战，并**无条件**拿到答案。

    用 `expose_answer=True` 而不是 `debug=True`：后者只在 `MOSS_ENV` 为
    dev/test 时生效，而测试进程未必设了这个变量 —— 依赖环境变量的测试
    会在换台机器时莫名其妙失败。前者是给测试/本地脚本的显式开关。
    """
    return svc.issue(ip=ip, ua="pytest", expose_answer=True)


# ======================================================================
# 一、签发
# ======================================================================

def test_issue_returns_image_and_token(svc) -> None:
    view = issue(svc)
    assert view.token and len(view.token) >= 20
    assert view.image_png.startswith(JPEG_MAGIC), "不是合法 JPEG"
    assert view.expires_in > 0
    assert view.meta["length"] == ANSWER_LENGTH


def test_debug_answer_is_off_in_production(monkeypatch) -> None:
    """★★ 生产环境**绝不**回显答案 —— 回显等于没有验证码。

    "调试开关忘了关"是这类组件最典型的事故：开发时看得见答案，上线后照旧。
    所以这条用 `MOSS_ENV=prod` 直接验证。

    ⚠️ 注意这里必须传 `debug=True`（HTTP 层用的那个开关），
    而**不是** `expose_answer` —— 后者是给测试/本地脚本的无条件开关，
    它本来就该生效。混用会让这条测试失去意义（我第一版就写错成
    `expose_answer=True`，于是"生产不回显"根本没被验证）。
    """
    monkeypatch.setenv("MOSS_ENV", "prod")
    svc = HumanChallengeService(rng=random.Random(1))
    assert svc.issue(ip="10.0.0.1", debug=True).debug_answer == "", (
        "生产环境回显了答案 —— 等于没有验证码")

    # dev 环境下 debug 才生效（这是它存在的意义：本地调试不用看图）
    monkeypatch.setenv("MOSS_ENV", "dev")
    assert svc.issue(ip="10.0.0.2", debug=True).debug_answer != "", (
        "dev 环境下 debug 反而没生效，本地调试会很不方便")

    # 不传 debug 时，任何环境都不回显
    for env in ("dev", "test", "prod"):
        monkeypatch.setenv("MOSS_ENV", env)
        assert svc.issue(ip=f"10.0.1.{env.__len__()}").debug_answer == "", env

    # `expose_answer` 是无条件开关：它的存在就是为了测试与本地脚本，
    # 因此**不应该**再受环境影响（否则测试要依赖环境变量，换机器就红）。
    monkeypatch.setenv("MOSS_ENV", "prod")
    assert svc.issue(ip="10.0.2.1", expose_answer=True).debug_answer != "", (
        "expose_answer 应当无条件生效")


def test_answer_uses_unambiguous_alphabet(svc) -> None:
    """★ 字符集不含易混字符（0/O、1/I/L）。

    用户分不清"这是 0 还是 O"时会反复重试最后放弃注册 ——
    这是安全性与可用性的经典取舍，这里明确选可用性。
    """
    for ch in "01OILlo":
        assert ch not in ALPHABET, f"{ch!r} 易与其它字符混淆"
    for i in range(30):
        view = svc.issue(ip=f"10.1.0.{i % 200}", expose_answer=True)
        assert len(view.debug_answer) == ANSWER_LENGTH
        assert all(c in ALPHABET for c in view.debug_answer)


def test_meta_tells_user_about_alphabet(svc) -> None:
    """提示要**告诉用户**字符集里没有易混字符，否则他会一直纠结。"""
    view = issue(svc)
    assert "0" in view.meta["alphabet_note"]
    assert view.meta["hint"]


def test_image_is_reasonably_small(svc) -> None:
    """图片要小（内联进 JSON），否则登录页首屏被拖慢。

    实测（20 张中位）：模糊+PNG 11.9 KB → 密噪点+PNG 9.9 KB →
    **密噪点+JPEG 3.9 KB**。上限设 8 KB 是为了在"抗 OCR 强度"
    与"体积"之间留出余量：如果有人往图里加更多干扰，只要不破 8 KB
    就不用担心首屏；破了说明该重新权衡。
    """
    sizes = [len(issue(svc).image_png) for _ in range(5)]
    assert max(sizes) < 8000, f"验证码图过大：{sizes}"


def test_data_uri_is_valid_image(svc) -> None:
    uri = png_data_uri(issue(svc).image_png)
    assert uri.startswith(f"data:{IMAGE_MIME};base64,"), uri[:40]
    raw = base64.b64decode(uri.split(",", 1)[1])
    assert raw.startswith(JPEG_MAGIC)


# ======================================================================
# 二、答案不以明文保存
# ======================================================================

def test_answer_is_not_stored_in_plaintext(svc) -> None:
    """★★ 存储里翻不到答案明文。

    内存转储/调试接口/日志把 `_store` 打出来时，不能直接暴露答案。
    """
    view = issue(svc)
    answer = view.debug_answer
    assert answer
    blob = repr(svc._store)          # noqa: SLF001 刻意翻内部存储
    assert answer not in blob, "答案以明文存在记录里"
    record = svc._store[view.token]  # noqa: SLF001
    assert record.answer_hash != answer
    assert len(record.answer_hash) == 64, "应是 sha256 十六进制"


def test_hash_is_bound_to_token() -> None:
    """★ 答案哈希必须**绑定 token**。

    不绑的话，两张不同的图若答案相同，哈希就相同 ——
    攻击者能据此推断"这两张图答案一样"，缩小要试的组合空间。
    """
    from src.domain.auth.human_check import _hash_answer

    assert _hash_answer("token-a", "ABCD") != _hash_answer("token-b", "ABCD")


# ======================================================================
# 三、校验：正确路径
# ======================================================================

def test_verify_accepts_correct_answer_case_insensitive(svc) -> None:
    view = issue(svc)
    assert svc.verify(token=view.token, answer=view.debug_answer.lower(),
                      ip="10.0.0.1")
    # 也容忍空格（用户复制粘贴常带空格）
    view2 = issue(svc)
    spaced = " ".join(view2.debug_answer)
    assert svc.verify(token=view2.token, answer=spaced, ip="10.0.0.1")


def test_verify_rejects_wrong_answer(svc) -> None:
    view = issue(svc)
    assert svc.verify(token=view.token, answer="ZZZZ", ip="10.0.0.1") is False


def test_verify_rejects_unknown_token(svc) -> None:
    assert svc.verify(token="not-a-real-token", answer="ABCD") is False


def test_verify_rejects_empty_input(svc) -> None:
    view = issue(svc)
    assert svc.verify(token=view.token, answer="") is False
    assert svc.verify(token="", answer=view.debug_answer) is False


# ======================================================================
# 四、一次性（防复用）
# ======================================================================

def test_token_is_single_use(svc) -> None:
    """★★ 校验成功后令牌**立即作废**。

    否则攻击者只要成功过一次（或从流量里抓到一个），就能无限复用。
    """
    view = issue(svc)
    assert svc.verify(token=view.token, answer=view.debug_answer,
                      ip="10.0.0.1")
    assert svc.verify(token=view.token, answer=view.debug_answer,
                      ip="10.0.0.1") is False, "令牌被复用了"
    assert view.token not in svc._store  # noqa: SLF001


# ======================================================================
# 五、绑定客户端（防代答/转卖）
# ======================================================================

def test_token_is_bound_to_client_ip(svc) -> None:
    """★★ 在 A 处领的令牌不能拿到 B 处用。

    这拦的是**打码平台/代答**：攻击者把图转给别人解，再拿答案回来提交。
    如果令牌与 IP 无关，这种"人工代答"就完全绕过了图形码。
    """
    view = issue(svc, ip="10.0.0.1")
    assert svc.verify(token=view.token, answer=view.debug_answer,
                      ip="10.0.0.2") is False, "令牌可以在别的 IP 上使用"
    # 原 IP 仍然可用
    assert svc.verify(token=view.token, answer=view.debug_answer,
                      ip="10.0.0.1") is True


def test_bind_client_can_be_disabled_for_special_cases(svc) -> None:
    """手机网络切换 IP 时不该被误杀（`bind_client=False` 由调用方决定）。"""
    view = issue(svc, ip="10.0.0.1")
    assert svc.verify(token=view.token, answer=view.debug_answer,
                      ip="10.9.9.9", bind_client=False) is True


# ======================================================================
# 六、TTL 与尝试次数
# ======================================================================

def test_expired_token_is_rejected(svc) -> None:
    """★ 过期即失效（防"提前批量囤积"）。"""
    svc._ttl = 0.01                 # noqa: SLF001 直接改短 TTL
    view = issue(svc, ip="10.2.0.1")
    import time
    time.sleep(0.05)
    assert svc.verify(token=view.token, answer=view.debug_answer,
                      ip="10.2.0.1") is False


def test_attempts_are_capped(svc) -> None:
    """★★ 连续试错达到上限后**作废挑战**，不给继续试的机会。

    4 位字符集 30 ⇒ 约 81 万组合。给 3 次机会，暴力命中率 < 4e-6。
    """
    view = issue(svc)
    for _ in range(2):
        assert svc.verify(token=view.token, answer="ZZZZ",
                          ip="10.0.0.1") is False
    # 第 3 次错 → 达到上限、作废
    assert svc.verify(token=view.token, answer="ZZZZ", ip="10.0.0.1") is False
    # 此时**即使答对也不算**（挑战已作废）
    assert svc.verify(token=view.token, answer=view.debug_answer,
                      ip="10.0.0.1") is False, "试满上限后仍可用正确答案通过"
    assert view.token not in svc._store  # noqa: SLF001


# ======================================================================
# 七、领取限流（防刷图/刷存储）
# ======================================================================

def test_issue_is_rate_limited_per_ip(svc) -> None:
    """★ 单 IP 领取次数受限：否则脚本能狂领图（训练 OCR / 刷爆内存）。"""
    for _ in range(5):
        svc.issue(ip="10.3.0.1", expose_answer=True)
    with pytest.raises(ChallengeRateLimited, match="过于频繁"):
        svc.issue(ip="10.3.0.1", expose_answer=True)
    # 别的 IP 不受影响（限流必须按 IP 隔离）
    assert svc.issue(ip="10.3.0.2", expose_answer=True).token


def test_issue_without_ip_is_not_limited(svc) -> None:
    """拿不到 IP 时不限流 —— 否则会把所有正常用户一起挡掉。"""
    for _ in range(10):
        assert svc.issue(ip="", expose_answer=True).token


def test_issue_window_slides(svc) -> None:
    """窗口滑动：过窗口后重新可领（不是永久封禁）。"""
    svc._issue_window = 0.05        # noqa: SLF001
    for _ in range(5):
        svc.issue(ip="10.4.0.1", expose_answer=True)
    with pytest.raises(ChallengeRateLimited):
        svc.issue(ip="10.4.0.1", expose_answer=True)
    import time
    time.sleep(0.08)
    assert svc.issue(ip="10.4.0.1", expose_answer=True).token


def test_prune_keeps_memory_bounded(svc) -> None:
    """★ 过期挑战必须被清掉，否则存储随攻击流量无界增长。"""
    svc._ttl = 0.01                 # noqa: SLF001
    svc._issue_limit = 1000         # noqa: SLF001 绕开限流，专测清理
    for i in range(50):
        svc.issue(ip=f"10.5.0.{i % 250}", expose_answer=True)
    import time
    time.sleep(0.05)
    stats = svc.stats()             # stats 内部会 prune
    assert stats["pending"] == 0, f"过期挑战没被清掉：{stats}"


def test_stats_reports_configuration(svc) -> None:
    stats = svc.stats()
    assert stats["ttl_seconds"] == 180
    assert stats["max_attempts"] == 3
    assert stats["issue_limit"] == 5


# ======================================================================
# 八、渲染（图必须真的画了东西，而不是一张空白）
# ======================================================================

def test_image_differs_between_challenges(svc) -> None:
    """★ 每张图必须不同 —— 相同的图意味着模板匹配一次就通杀。"""
    a = render_challenge_image("ABCD", rng=random.Random(1))
    b = render_challenge_image("ABCD", rng=random.Random(2))
    assert a != b, "同答案不同种子生成的图完全相同"


def test_image_is_not_blank() -> None:
    """图的像素要有变化（纯色图 = 渲染失败但接口仍返回 200）。"""
    import io

    from PIL import Image

    png = render_challenge_image("A7K2", rng=random.Random(3))
    img = Image.open(io.BytesIO(png)).convert("L")
    colors = img.getcolors(maxcolors=100000) or []
    assert len(colors) > 20, f"图几乎是纯色（只有 {len(colors)} 种灰阶）"
    # 尺寸固定（随机尺寸会被用来做特征识别）
    assert img.size == (168, 52)


def test_no_obvious_answer_leak_in_png_metadata() -> None:
    """★ PNG 里不能带可提取的答案（例如写在注释块里）。

    有些实现图省事把答案塞进 tEXt 元数据 —— 那脚本一读就拿到了。
    """
    answer = "Q4W9"
    png = render_challenge_image(answer, rng=random.Random(4))
    assert answer.encode() not in png, "答案明文出现在 PNG 字节里"
    assert not re.search(rb"tEXt|iTXt|zTXt", png), "PNG 里带了文本元数据块"
