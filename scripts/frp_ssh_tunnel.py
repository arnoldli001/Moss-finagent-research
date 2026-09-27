"""临时：搭 frp-over-SSH 隧道（本机 127.0.0.1:17000 → VPS 127.0.0.1:7000）。

## 它解决的问题

腾讯云安全组（`YJ-FIREWALL-INPUT`）只放行了 22 和 80，443/7000 都被挡。
所以 frp 的控制连接**不再直连 7000**，改为走已经通的 22 端口：

    frpc → 127.0.0.1:17000 ──(本地端口转发)──> VPS 127.0.0.1:7000 (frps)

好处：**不需要动腾讯云控制台**，且 7000 不必对公网开放（攻击面更小）。

## 用法

    python scripts/frp_ssh_tunnel.py            # 前台跑（调试）
    # 生产用 scripts/frpc_start.ps1 启动（它会把本进程放后台）
"""

from __future__ import annotations

import os
import select
import socket
import sys
import threading
import time
from pathlib import Path

import paramiko

HOST = os.environ.get("VPS_HOST", "43.128.5.94")
PORT = int(os.environ.get("VPS_PORT", "22"))
USER = os.environ.get("VPS_USER", "ubuntu")

#: 默认密钥路径。**优先用密钥，不用密码** —— 理由见 `_load_credentials`。
DEFAULT_KEY = str(Path.home() / ".ssh" / "moss_hk_tunnel")


def _candidate_keys() -> list[str]:
    """按优先级列出所有可能的密钥位置。

    ## 为什么不能只认 `Path.home()`

    本脚本可能被**以别的账户**拉起（计划任务的身份可以改，进程也可能由
    WMI 创建）。此时 `Path.home()` 指向那个账户的用户目录，找不到管理员
    装的那把密钥，脚本报 `没有可用凭据` 直接退出 —— 而外层看到的只是
    "入口 502"。这会让人先去怀疑 frp、nginx、网络，全都不是。

    实测踩到（2026-09-27）：把计划任务改成 `UserId=SYSTEM` 以消除弹窗后，
    `Path.home()` 变成 `C:\\Windows\\system32\\config\\systemprofile`，
    隧道立即退出，日志里只有一行"没有可用凭据"。

    ## 顺序

        ① 显式 `VPS_KEY`（调用方最清楚密钥在哪）
        ② 当前 home（正常情况）
        ③ 本机其他用户的 `.ssh`（本机自用部署，比硬编码某个用户名更稳）

    ③ 用 glob 而不是写死 `Administrator`：换机器、换用户名都不用改代码。
    """
    out: list[str] = []
    env_key = os.environ.get("VPS_KEY", "").strip()
    if env_key:
        out.append(env_key)
    out.append(DEFAULT_KEY)
    try:
        for home in Path("C:/Users").glob("*"):
            # 只收"真的有 .ssh 目录"的用户目录：`C:/Users` 下还挂着
            # `All Users` / `Default` / `Default User` / `Public` / `desktop.ini`
            # 这些非用户条目，一并塞进候选只会把报错信息刷成噪音。
            ssh_dir = home / ".ssh"
            if ssh_dir.is_dir():
                out.append(str(ssh_dir / "moss_hk_tunnel"))
    except OSError:      # 非 Windows / 无权限：忽略，前两个候选已经够用
        pass
    # 去重且保持顺序：`VPS_KEY` 与当前 home 常常指向同一个路径
    return list(dict.fromkeys(out))


def _load_credentials() -> dict:
    """返回 paramiko 的认证参数。**优先密钥，密码只作兜底。**

    ## 为什么默认必须是密钥

    隧道要**长期稳定**地连着 VPS。用密码意味着每次启动都得有
    `VPS_PASS` 在环境里 —— 换个会话、重启后用计划任务拉起、
    或任何自动化场景下环境一丢，隧道就报
    `No authentication methods available` 并且**循环重连失败**。

    而它的表现是"网站打不开"，与"后端挂了"**长得一模一样** ——
    2026-09-26 实测踩到：手工重启隧道时忘了带密码环境变量，
    链路直接断掉，排查时先怀疑了后端。

    密钥一次安装永久可用，且可以随时从 VPS 上删掉（撤销方便）。

    ## 依赖 `_candidate_keys()` 而不是单个路径

    运行身份是可变的，而"找不到密钥"的表现是入口 502 —— 与网络故障
    无法区分。所以按候选清单逐个找，并把**找过哪些位置**写进报错信息里。
    """
    candidates = _candidate_keys()
    for cand in candidates:
        if cand and Path(cand).exists():
            return {"key_filename": cand}

    looked = "、".join(dict.fromkeys(candidates))
    pwd = os.environ.get("VPS_PASS", "")
    if pwd:
        print("[tunnel] ⚠️ 未找到密钥，回退用密码认证"
              f"（找过：{looked}；建议装密钥，见 scripts/setup_tunnel_key.py）",
              flush=True)
        return {"password": pwd}
    raise SystemExit(
        f"没有可用凭据：下列位置都没有密钥，也没有 VPS_PASS 环境变量。\n"
        f"  找过：{looked}\n"
        f"装密钥：python scripts/setup_tunnel_key.py")



def main() -> int:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    # LOCAL_PORT 等常量在这里读（放在函数里是为了让上面的凭据逻辑先跑，
    # 缺凭据时给出可执行的提示而不是一个 NameError）
    local_port = int(os.environ.get("TUNNEL_LOCAL_PORT", "17000"))
    remote_host = os.environ.get("TUNNEL_REMOTE_HOST", "127.0.0.1")
    remote_port = int(os.environ.get("TUNNEL_REMOTE_PORT", "7000"))

    kw: dict = dict(username=USER, timeout=25, allow_agent=False,
                    look_for_keys=False, **_load_credentials())
    client.connect(HOST, **kw)
    transport = client.get_transport()
    if transport is None:
        print("无法取得 transport", file=sys.stderr)
        return 1

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

    # ★★ Windows 与 Linux 的 SO_REUSEADDR 语义**相反**，这里写错会致命。
    #
    #   · Linux：SO_REUSEADDR 只允许绑定处于 TIME_WAIT 的端口，
    #     对"已有进程正在 LISTEN"的端口仍然报 EADDRINUSE ——
    #     所以下面那段"端口被占就退出"的保护是**有效**的。
    #   · Windows：SO_REUSEADDR **允许重复绑定**一个正在 LISTEN 的端口，
    #     后绑定的进程照样绑定成功 —— 保护**完全失效**。
    #
    # 实测踩到（2026-09-27 00:14）：两个隧道进程同时监听 127.0.0.1:17000，
    # 新连接被 Windows 随机分给其中一个；分到 SSH transport 已失效的那个
    # 就**永久挂住**。外部表现是 HTTPS 请求 TCP/TLS 都正常、但**首字节
    # 20 秒不来** —— 看起来像后端挂了或 frp 坏了，实际两者都无辜。
    # 更坏的是它让"重复实例"永远无法被自愈脚本发现（端口一直在听）。
    #
    # 正确写法：Windows 用 SO_EXCLUSIVEADDRUSE（独占绑定），
    # 绑定一个已被监听的端口会明确失败 —— 保护才真正生效。
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):      # 仅 Windows 存在
        server.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    else:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        server.bind(("127.0.0.1", local_port))
    except OSError as exc:
        # 端口被占 = 已有隧道在跑。**要明确说清并退出**，
        # 否则会陷入"重连循环"，日志刷屏却谁也连不上。
        print(f"[tunnel] 本地端口 {local_port} 已被占用（{exc}）——"
              f"可能已有隧道在运行。本进程退出，不重试。", flush=True)
        return 3
    server.listen(64)
    print(f"[tunnel] 就绪：127.0.0.1:{local_port} → {HOST}:{remote_host}:{remote_port}",
          flush=True)

    def handle(conn: socket.socket) -> None:
        try:
            chan = transport.open_channel(
                "direct-tcpip", (remote_host, remote_port), conn.getpeername())
        except Exception as exc:  # noqa: BLE001
            print(f"[tunnel] 打开通道失败: {exc}", flush=True)
            conn.close()
            return
        if chan is None:
            conn.close()
            return
        while True:
            r, _w, _x = select.select([conn, chan], [], [], 0.5)
            if conn in r:
                data = conn.recv(32768)
                if not data:
                    break
                chan.sendall(data)
            if chan in r:
                data = chan.recv(32768)
                if not data:
                    break
                conn.sendall(data)
        try:
            chan.close()
        except Exception:  # noqa: BLE001
            pass
        conn.close()

    while True:
        try:
            conn, _addr = server.accept()
        except OSError:
            break
        threading.Thread(target=handle, args=(conn,), daemon=True).start()
        if not transport.is_active():
            print("[tunnel] SSH 连接已断开", flush=True)
            return 1


if __name__ == "__main__":
    # ★ 日志自己写，**不靠 shell 重定向**。
    #
    # 为什么：用 `Start-Process -RedirectStandardOutput`（或管道）启动时，
    # 子进程会绑定到父进程持有的那几个句柄上 —— 父进程一退出，
    # 重定向的读端关闭，进程就被连带带走（或卡在写日志上）。
    # 实测：由 AI 工具调用启动的隧道在工具调用结束后**立刻消失**，
    # 而表现是"网站 502"，与"后端挂了"完全一样。
    #
    # 自己打开文件写日志，就没有任何与被启动者的句柄共享。
    _log_path = Path(os.environ.get(
        "TUNNEL_LOG", "data/run/frp_tunnel.log"))
    _log_path.parent.mkdir(parents=True, exist_ok=True)
    _log_fh = _log_path.open("a", encoding="utf-8", buffering=1)

    _orig_print = print

    def print(*args, **kwargs):  # noqa: A001 故意遮蔽內建，统一进日志
        kwargs.setdefault("file", _log_fh)
        _orig_print(*args, **kwargs)

    while True:
        try:
            rc = main()
        except SystemExit as exc:      # 缺凭据之类：明确退出，不重试
            print(f"[tunnel] 退出：{exc}", flush=True)
            break
        except Exception as exc:  # noqa: BLE001
            print(f"[tunnel] 异常: {type(exc).__name__}: {exc}", flush=True)
            rc = 1
        if rc == 3:                    # 端口被占 = 已有隧道，别重试刷屏
            break
        print(f"[tunnel] 退出（{rc}），5 秒后重连…", flush=True)
        time.sleep(5)
