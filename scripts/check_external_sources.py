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

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

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


# ────────────────────────── Choice ──────────────────────────
def check_choice() -> tuple[str, str, str]:
    try:
        from EmQuantAPI import c  # type: ignore
        import EmQuantAPI  # type: ignore
    except Exception as exc:  # noqa: BLE001
        return (BLOCKED,
                f"导入失败：{type(exc).__name__}: {exc}",
                f"多半是 `EmQuantAPI.pth` 缺失（重建 venv 会丢）→ 跑 "
                f"`uv run python {CHOICE_HOME}\\installEmQuantAPI.py`")

    where = getattr(EmQuantAPI, "__file__", "?")
    if "EMQuantAPI_Python" not in str(where):
        return (BROKEN, f"导入自非预期位置：{where}",
                f"期望位于 {CHOICE_HOME}")

    try:
        res = c.start("ForceLogin=1")
    except Exception as exc:  # noqa: BLE001
        return (BLOCKED, f"c.start() 抛异常：{type(exc).__name__}: {exc}",
                "看 DLL 是否加载成功（WinError 87 = .pth 缺失）")
    code = str(getattr(res, "ErrorCode", ""))
    msg = str(getattr(res, "ErrorMsg", ""))
    if code == "0":
        try:
            d = c.css("600036.SH", "CLOSE", "2026-09-22", "2026-09-29")
            ok = str(getattr(d, "ErrorCode", "")) == "0"
            detail = f"登录成功；css 取数{'成功' if ok else '失败'}：{getattr(d,'Data',None)}"
        finally:
            try:
                c.stop()
            except Exception:  # noqa: BLE001
                pass
        return (OK if ok else BLOCKED, detail[:200], "可接成数据连接器")

    #: 实测（2026-09-30）：code:160 / 10001003 = 账号**没有该 API 的权限**
    if code == "10001003" or "no access" in msg.lower():
        return (BLOCKED, f"登录被拒：ErrorCode={code} {msg}（服务端 code:160）",
                "**账号未开通「量化接口(EMQuantAPI)」权限** → 找东财 Choice "
                "客户经理/服务群开通该权限；设备激活已成功、令牌有效，"
                "**唯一缺的就是这个权限**。开通后无需改代码")
    return (BLOCKED, f"登录被拒：ErrorCode={code} {msg}", "按错误码排查")


def main() -> int:
    only = "--choice-only" in sys.argv
    rows: list[tuple[str, str, str, str]] = []
    if not only:
        rows.append(("博查搜索 Bocha", *check_bocha()))
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
        _k, _s = _read_env_any_scope("bocha_search")
        print(f"\n  博查密钥：{_mask(_k)}  作用域={_s}")
        if _k and _s != "进程":
            print("    ⚠️ 本进程未继承它 ⇒ **已在运行的实例重启后才生效**")
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
