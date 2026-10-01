"""外部联网源**真实通路**体检：博查搜索 + 东方财富 Choice EMQuantAPI。

## 为什么要有这条命令

本项目已经**四次**踩同一形状的坑：备用/新源只被"声明"过、从未被"走通"过
（见 `.trae/skills/backup-path-availability/SKILL.md`）。所以任何外部源在
**接进架构之前**、以及**接进去之后每次排查**，都用这一条命令回答：

    这个源现在到底能不能用？不能用是**卡在哪一层**？下一步谁去做什么？

判据是**取到没取到**，不是"看着像不像"。**不打印任何密钥明文。**

## 退出码

    0 = 全部可用        3 = 至少一个被服务端拒绝（含原因与下一步）
    1 = 用法/环境错误   2 = 探针自己坏了（不要把它当成"源不可用"）

用法：

    uv run python scripts/check_external_sources.py
    uv run python scripts/check_external_sources.py --choice-only
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

#: ★ 本机脚本固定坑：`uv run python scripts\x.py` 的 `sys.path[0]` 是 **scripts/**，
#: 不是仓库根 ⇒ 不插这一下，下面 `from src...` 会 ModuleNotFoundError
#: （看起来像"代码坏了"，实际是脚本自己没设好路径）。
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

#: Choice SDK 的**规范安装位置**（单一真值源；`.pth` 由 SDK 自带 installer 写入）。
#: ⚠️ 这个 `.pth` 在 **venv 里**（不在仓库里）⇒ **重建 venv 会静默失效**，
#: 症状是 `CDLL('')` → `WinError 87 参数错误`，看起来像"SDK 坏了"。
CHOICE_HOME = r"C:\EMQuantAPI_Python\python3"

BOCHA_URL = "https://api.bochaai.com/v1/web-search"

OK, BLOCKED, BROKEN = "可用", "被拒", "探针异常"


def _mask(v: str | None) -> str:
    if not v:
        return "(未设置)"
    return f"{v[:4]}****  (长度 {len(v)})"


def _read_env_any_scope(name: str) -> tuple[str | None, str]:
    """按 进程 → 用户 → 机器 三档找环境变量，返回 (值, 找到的作用域)。

    ## 为什么要读注册表，而不是只看 `os.environ`

    Windows 上 `setx`/安装器写的是**用户或机器**作用域，**已经在跑的进程不会**
    看到它（本项目实测：key 在 User+Machine 里都有，而 harness 与已在运行的
    pilot 进程读不到）。

    只看 `os.environ` 会把这种情况报成"**未配置**" —— 而它与"真的没配"、
    与"配了但没额度"是**三件不同的事**，处置完全不同。
    `AGENTS.md`：「没量到」≠「量到 0」。所以这里把三档**分别**标出来。
    """
    v = os.environ.get(name)
    if v:
        return v, "进程"
    try:
        import winreg
    except ImportError:
        return None, "非 Windows"
    for hive, path, label in (
        (winreg.HKEY_CURRENT_USER, r"Environment", "用户"),
        (winreg.HKEY_LOCAL_MACHINE,
         r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment", "机器"),
    ):
        try:
            with winreg.OpenKey(hive, path) as k:
                val, _ = winreg.QueryValueEx(k, name)
                if val:
                    return str(val), label
        except OSError:
            continue
    return None, "未配置"


# ────────────────────────── 博查搜索 ──────────────────────────
def check_bocha() -> tuple[str, str, str]:
    """返回 (状态, 详情, 下一步)。"""
    key, scope = _read_env_any_scope("bocha_search")
    if not key:
        return (BLOCKED, "三档作用域都找不到 bocha_search（进程/用户/机器）",
                "设置环境变量或写进 .env；设置后**已运行的实例要重启**才看得到")
    inherited = scope == "进程"
    note = "" if inherited else f"（在**{scope}**作用域，本进程未继承 → 需重启才生效）"
    body = json.dumps({"query": "招商银行 600036", "count": 1,
                       "summary": True, "freshness": "noLimit"}).encode()
    req = urllib.request.Request(
        BOCHA_URL, data=body, method="POST",
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            payload = json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            msg = json.loads(raw).get("message", raw[:120])
        except Exception:  # noqa: BLE001
            msg = raw[:120]
        if e.code in (401, 403) and "quota" in msg.lower():
            return (BLOCKED, f"HTTP {e.code}：{msg}{note}",
                    "账号**额度/套餐不足** → 到博查控制台充值或开通套餐；"
                    "在此之前**不得**把它当作可用搜索源接进链路")
        if e.code in (401, 403):
            return (BLOCKED, f"HTTP {e.code}：{msg}{note}",
                    "密钥无效或权限不足 → 核对 bocha_search 是否为该账号的 key")
        return (BLOCKED, f"HTTP {e.code}：{msg}{note}", "按返回体排查")
    except Exception as exc:  # noqa: BLE001
        return (BROKEN, f"{type(exc).__name__}: {exc}",
                "网络/探针问题，**不要**据此判定源不可用")

    pages = (((payload.get("data") or {}).get("webPages") or {})
             .get("value") or [])
    if not pages:
        return (BLOCKED, f"HTTP 200 但 0 条结果（可能是 query 太窄）{note}",
                "换一个更宽的 query 复测")
    return (OK, f"HTTP 200，返回 {len(pages)} 条；"
                f"首条={pages[0].get('name', '')[:40]}{note}",
            "可用于换源检索（path C）")


def check_baidu() -> tuple[str, str, str]:
    """百度千帆「智能搜索」—— 搜索源的**备用**（`CHG-0117`）。

    走**生产代码路径**（`infrastructure/search/baidu.py`），不是另写一遍 HTTP：
    否则"体检通过"与"生产能跑"是两回事。

    ⚠️ 用**固定 query** ⇒ 24h 内重复执行都是**缓存命中、不花钱**；
    每天第一次执行才真花 1 次（日配额 100，闸门 60）。
    """
    try:
        from src.infrastructure.search import baidu
    except Exception as exc:  # noqa: BLE001
        return (BROKEN, f"导入失败：{type(exc).__name__}: {exc}",
                "先修导入（多半是脚本 sys.path 或包结构变了）")

    key, scope = _read_env_any_scope("baidusearch")
    note = "" if scope == "进程" else f"（在**{scope}**作用域，本进程未继承）"
    out = baidu.web_search("中国 社会融资规模存量 月度 官方数据",
                          count=3, use_cache=True)
    st = baidu.budget_state()
    gate = (f"日闸门 {st['used_today']}/{st['limit_today']}"
            + ("（命中缓存，本次未花钱）" if out.from_cache else ""))
    if out.ok:
        return (OK, f"{out.reason}；{gate}{note}",
                "作为搜索备用源可用（主源失效时自动顶上）")
    if out.blocked_by == "no_key":
        return (BLOCKED, f"未配置 baidusearch{note}",
                "写进项目根 `.env`（与 bocha_search 同一理由："
                "机器级环境变量，已在运行的进程继承不到）")
    return (BLOCKED, f"{out.reason}；{gate}{note}", "按 blocked_by 排查")


# ────────────────────────── Choice ──────────────────────────
def check_choice() -> tuple[str, str, str]:
    """委托给 **`connectors/choice_gate.py`**（**单一实现**，本脚本不再自己判一次）。

    为什么改成委托：此前这里有一份独立的错误码分类（`code == "10001003"` …），
    而闸门模块里也有同样一份 —— **同一判断两份实现必然漂移**
    （`AGENTS.md`《AI 首轮编码硬约束》）。现在分类只在一处，
    本函数只负责把状态码翻译成"体检报告"的三元组。

    登录成功时额外做一次**真取数**（`css`）—— 体检要证到"能取到数"，
    而不是只证到"能登录"（判据是取到没取到）。
    """
    try:
        from src.infrastructure.connectors import choice_gate as G
    except Exception as exc:  # noqa: BLE001
        return (BROKEN, f"导入闸门失败：{type(exc).__name__}: {exc}",
                "先修导入（脚本 sys.path 或包结构变了）")

    st = G.probe()
    if st.state != G.STATE_OK:
        advice = {
            G.STATE_NO_ACCESS: "**账号未开通「量化接口(EMQuantAPI)」权限** → 找东财 "
                               "Choice 客户经理/服务群开通该权限；设备已激活、令牌有效，"
                               "**唯一缺的就是这个权限**。开通后无需改代码",
            G.STATE_CONFIG_MISSING: f"跑 `LoginActivator.exe` 完成设备激活（生成 "
                                    f"{G.TOKEN_NAME}）",
            G.STATE_SDK_MISSING: f"跑 `uv run python {G.SDK_HOME}\\installEmQuantAPI.py`"
                                 "（SDK 自带 installer 写 `.pth`，别自己造路径注入）",
            G.STATE_UNREACHABLE: "查网络/代理后重试（**这不是账号没权限**）",
            G.STATE_PROBE_ERROR: "**先修探针**，不要据此判定账号没权限",
        }.get(st.state, "按状态码排查")
        status = BROKEN if st.state == G.STATE_PROBE_ERROR else BLOCKED
        return (status, f"[{st.state}] {st.detail}", advice)

    #: 登录成功 ⇒ 再证一步"真的能取到数"
    try:
        from EmQuantAPI import c  # type: ignore

        d = c.css("600036.SH", "CLOSE", "2026-09-22", "2026-09-29")
        ok = str(getattr(d, "ErrorCode", "")) == "0"
        detail = f"登录成功；css 取数{'成功' if ok else '失败'}：{getattr(d, 'Data', None)}"
        try:
            c.stop()
        except Exception:  # noqa: BLE001
            pass
    except Exception as exc:  # noqa: BLE001
        return (BLOCKED, f"登录成功但取数异常：{type(exc).__name__}: {exc}",
                "登录过了 ⇒ 查具体接口权限（不同指标可能分权限）")
    return (OK if ok else BLOCKED, detail[:200], "可接成数据连接器")


def main() -> int:
    only = "--choice-only" in sys.argv
    rows: list[tuple[str, str, str, str]] = []
    if not only:
        rows.append(("博查搜索 Bocha（主）", *check_bocha()))
        rows.append(("百度千帆搜索（备）", *check_baidu()))
    rows.append(("东财 Choice", *check_choice()))

    print("=" * 100)
    print("外部联网源 · 真实通路体检")
    print("=" * 100)
    for name, status, detail, nxt in rows:
        mark = "✅" if status == OK else ("⛔" if status == BLOCKED else "⚠️")
        print(f"\n{mark} {name}：{status}")
        print(f"    现象：{detail}")
        print(f"    下一步：{nxt}")
    if not only:
        print("\n  搜索源路由顺序：", " → ".join(
            __import__("src.infrastructure.search.router", fromlist=["x"])
            .PROVIDER_ORDER))
        _k, _s = _read_env_any_scope("bocha_search")
        print(f"  博查密钥：{_mask(_k)}  作用域={_s}")
        _k2, _s2 = _read_env_any_scope("baidusearch")
        print(f"  百度密钥：{_mask(_k2)}  作用域={_s2}")
        if (_k and _s != "进程") or (_k2 and _s2 != "进程"):
            print("    ⚠️ 有密钥在本进程未继承 ⇒ **已在运行的实例重启后才生效**")
    print(f"  Choice SDK：{CHOICE_HOME}")
    print("\n" + "=" * 100)
    if any(r[1] == BROKEN for r in rows):
        print("⚠️ 有探针自身异常 —— 先修探针，不要据此判定源不可用")
        return 2
    if all(r[1] == OK for r in rows):
        print("✅ 全部可用")
        return 0
    print("⛔ 至少一个源被服务端拒绝 —— **不要**把它接进链路（会变成永远失败的路径）")
    return 3


if __name__ == "__main__":
    sys.exit(main())
