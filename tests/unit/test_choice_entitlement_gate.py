"""Choice 能力闸门的离线护栏（`CHG-0119`）—— 不联网、不碰真 SDK。

## 这份测试守的是什么

目标里写着「**在授权到位前不得把它们声明为可用源**」，而这句话此前**只是散文**。
本文件的判据把它变成机器事实：

| 判据 | 防的失效 |
|---|---|
| `no_access` **不可接线** | 把没权限的源接进链 ⇒ 每次调用必失败（"已知必挂的路径"） |
| 六种状态**两两可分** | 把"探针坏了"当成"账号没权限"，排查方向整个跑偏 |
| `assert_wireable` 的**拒绝带出路** | 只说"你没权限"、不说找谁 —— 拒绝不给出路 |
| 空 `ErrorCode` / 未识别码 ⇒ `probe_error` | **不认识就当"没权限"** = 自造假结论 |
| ★ **条件判据**：链上若出现 Choice 连接器 ⇒ 闸门必须是 `ok` | 今天纪律成立（链上没有），但**将来有人接线时这条会红** —— 这是"把纪律变成判据"的关键一条 |

★ 最后那条是**条件式**的：今天它因为"链上没有 Choice"而**空洞通过**，
并在断言里**明写这一点**（空洞通过必须看得见，不能假装它在守什么）。
"""
from __future__ import annotations

import pytest

from src.infrastructure.connectors import choice_gate as G


class _Res:
    def __init__(self, code: str, msg: str = "") -> None:
        self.ErrorCode = code
        self.ErrorMsg = msg


def _importer(c: object):  # noqa: ANN001
    return lambda: c


class _FakeSDK:
    def __init__(self, res: object = None, exc: Exception | None = None) -> None:
        self.res = res
        self.exc = exc
        self.stopped = False
        self.options: list[str] = []

    def start(self, opts: str = ""):  # noqa: ANN001
        self.options.append(opts)
        if self.exc:
            raise self.exc
        return self.res

    def stop(self) -> None:
        self.stopped = True


@pytest.fixture()
def token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """造一个假的令牌文件，并把 SDK_HOME 指过去（**不碰真 SDK**）。"""
    p = tmp_path / "libs" / "windows"
    p.mkdir(parents=True)
    (p / G.TOKEN_NAME).write_text("fake-token", encoding="utf-8")
    monkeypatch.setattr(G, "SDK_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture()
def no_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(G, "SDK_HOME", str(tmp_path / "nope"))
    return tmp_path


# ─────────────────── 状态分类 ───────────────────
def test_ok_when_login_succeeds(token):
    sdk = _FakeSDK(_Res("0"))
    st = G.probe(importer=_importer(sdk), start=sdk.start)
    assert st.state == G.STATE_OK and st.wireable is True
    assert sdk.stopped, "登录成功后应登出（不占着账号的登录位）"


def test_no_access_is_classified_and_not_wireable(token):
    """★ 实测码：`code:160` / `ErrorCode=10001003`（本机真实返回）。"""
    sdk = _FakeSDK(_Res("10001003", "user has no access for this API"))
    st = G.probe(importer=_importer(sdk), start=sdk.start)
    assert st.state == G.STATE_NO_ACCESS
    assert st.wireable is False, "没权限却判成可接线 —— 会造出一条必挂的路径"
    assert st.raw_code == "10001003"


def test_config_missing_when_token_absent(no_token):
    sdk = _FakeSDK(_Res("0"))
    st = G.probe(importer=_importer(sdk), start=sdk.start)
    assert st.state == G.STATE_CONFIG_MISSING and st.wireable is False


def test_sdk_missing_when_dll_load_fails(token):
    """`CDLL('')` ⇒ WinError 87 是 `.pth` 缺失的典型症状（本项目实测）。"""
    err = OSError("参数错误")
    err.winerror = 87  # type: ignore[attr-defined]
    sdk = _FakeSDK(exc=err)
    st = G.probe(importer=_importer(sdk), start=sdk.start)
    assert st.state == G.STATE_SDK_MISSING


def test_import_error_is_sdk_missing(token):
    def boom():
        raise ImportError("No module named 'EmQuantAPI'")

    st = G.probe(importer=boom, start=lambda *_: _Res("0"))
    assert st.state == G.STATE_SDK_MISSING


def test_unreachable_is_not_no_access(token):
    """网络不通 ≠ 账号没权限 —— 两者处置完全不同。"""
    sdk = _FakeSDK(_Res("10001007", "connect server timeout"))
    st = G.probe(importer=_importer(sdk), start=sdk.start)
    assert st.state == G.STATE_UNREACHABLE
    assert st.state != G.STATE_NO_ACCESS


def test_unrecognised_code_is_probe_error_not_no_access(token):
    """★ **不认识就当"没权限"** = 自造假结论。必须落到 probe_error。"""
    sdk = _FakeSDK(_Res("99999", "something totally new"))
    st = G.probe(importer=_importer(sdk), start=sdk.start)
    assert st.state == G.STATE_PROBE_ERROR, (
        f"未识别的失败被当成了别的结论：{st.state} —— 排查方向会被带偏")
    assert st.state != G.STATE_NO_ACCESS


def test_empty_code_is_probe_error(token):
    sdk = _FakeSDK(_Res("", ""))
    st = G.probe(importer=_importer(sdk), start=sdk.start)
    assert st.state == G.STATE_PROBE_ERROR


def test_all_states_are_distinct_and_documented():
    assert len(set(G.STATES)) == len(G.STATES) == 6
    for s in G.STATES:
        assert s in (G.__doc__ or ""), f"状态 {s} 没写进模块文档"


# ─────────────────── 拒绝要给出路 ───────────────────
@pytest.mark.parametrize("state", [G.STATE_NO_ACCESS, G.STATE_CONFIG_MISSING,
                                   G.STATE_SDK_MISSING, G.STATE_UNREACHABLE,
                                   G.STATE_PROBE_ERROR])
def test_assert_wireable_refuses_with_actionable_advice(state):
    st = G.ChoiceStatus(state, "假的原因")
    with pytest.raises(RuntimeError) as ei:
        G.assert_wireable(st)
    msg = str(ei.value)
    assert state in msg, "拒绝消息里必须带状态码（机器可读）"
    assert len(msg) > 40, f"拒绝没有给出路，只说了『不行』：{msg}"


def test_assert_wireable_passes_only_on_ok():
    G.assert_wireable(G.ChoiceStatus(G.STATE_OK, "登录成功"))  # 不抛


def test_only_ok_is_wireable_exhaustively():
    """★ 闸门的**核心承诺**，对六种状态**逐一**断言（非空洞判据）。

    为什么单列这一条：下面那条"链上有没有接 Choice"的判据今天是**条件式**的
    （没接线就 skip），而 skip 不能是这块地方**唯一**的守卫 ——
    否则"把纪律变成判据"就成了一句空话。这条不依赖任何外部条件。
    """
    for s in G.STATES:
        assert G.ChoiceStatus(s, "x").wireable == (s == G.STATE_OK), (
            f"状态 {s} 的 wireable 判错了 —— 只有 ok 才允许接线")


# ─────────────────── ★ 条件判据：把纪律变成判据 ───────────────────
def test_choice_can_only_become_available_after_a_real_permission_check():
    """★ 目标那句「授权到位前不得声明为可用源」的**机器判据**。

    ## 为什么触发条件从「`runtime.py` 里有没有 Choice」改成「口径填没填」

    `CHG-0119` 立这条时，"`runtime.py` 里出现 Choice" ≈ "接线了"。**`CHG-0130`
    之后这个等价关系不成立了**：链上现在**必须**有那一行
    （`build_choice_routes()`），而它在闸门关着时**接出 0 条路由**
    （实测 `runtime.py` 的路由数 29，与接线前**逐字一致**）。
    于是旧触发条件会把"**已接、但被闸门挡住**"误报成违规 ——
    而它**漏掉了真正的门**：Choice 能不能供数，只取决于 `INDICATORS` 有没有填
    （`supports()` 只认它，`build_choice_routes()` 也只认它）。

    所以触发条件换成**那个真正的门**，而且强度**提高**了：

    * `INDICATORS` 为空（今天）⇒ 条件式 `skip`，与旧版一样**明写**"本判据是
      将来的防线"，不假装它在守什么；
    * **一旦有人填了口径** ⇒ 本判据跑**真闸门**（真登录），没权限就直接红。

    ⚠️ 代价要说清：填口径之后，这条会让单测做一次**真网络登录**。
    那是**故意的** —— 声明"Choice 可用"这件事只该发生一次，
    而那一次必须拿真权限换来。
    """
    from src.infrastructure.connectors import choice_connector as C

    if not C.INDICATORS:
        pytest.skip(
            "口径为空 ⇒ Choice 不可能供任何数据（当前纪律成立）—— "
            "本判据是**将来**的防线：谁填了 `INDICATORS`，它就跑真闸门")
    st = G.probe()
    assert st.wireable, (
        f"`INDICATORS` 已登记 {len(C.INDICATORS)} 条口径，但闸门说 Choice 不可用"
        f"（state={st.state}）：{st.detail} —— 这正是目标里"
        "『授权到位前不得声明为可用源』要挡的情况")
