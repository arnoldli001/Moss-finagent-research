# 迁到香港 VPS（免备案）+ frp：完整实操

> **目标**：把公网入口从"Cloudflare 隧道（走 LAX，1.1~11.5 秒、约 10% 请求挂死）"
> 换成"香港 VPS 中转（实测到腾讯云香港 **6 ms**）"。
>
> **原则**：**CF 隧道完整保留**，两条路并存，切回来只改一条 DNS 记录、零停机。

---

## 0. 为什么是香港 · 为什么是腾讯云（先看数据，别看结论）

2026-09-26 的实测，同一台机器、同一时段：

| 路径 | 结果 |
|---|---|
| 后端本机 `127.0.0.1:8110` | **1.9 ~ 3.4 毫秒**，5/5 成功 |
| 经 CF 隧道（公网） | **1.14 ~ 11.5 秒**，9/10 成功（1 次直接挂死） |
| **到腾讯云香港 `43.145.12.111`** | **min 5ms / 中位 6ms / max 9ms，5/5 成功** |
| CF 香港 POP | 161 ~ 173 ms（CF 的 anycast **不**给你走香港） |

**读法**：问题从来不在后端（2 毫秒级），全在链路。而你这台机器到腾讯云香港
只有 **6 毫秒** —— 这不是"普通香港线路 70~120ms"，是有直连的线路。

⚠️ **诚实说明测量的局限**：其他家（阿里云香港、搬瓦工、Vultr、AWS 香港）
的测试 IP **全部不通**。这**很可能**是它们默认丢弃无关探测包，
**不代表线路差** —— 所以上表只能证明"腾讯云这条路极快"，
不能证明别人慢。若你想换家，买之前无法预知，只能买来实测。

**免备案**：备案只管**中国大陆境内**的服务器。香港属于境外节点，
域名解析过去**无需 ICP 备案**。

---

## 1. 买机器（腾讯云香港）

### 1.1 注册

| 步骤 | 操作 |
|---|---|
| 1 | 打开 **https://cloud.tencent.com** |
| 2 | 右上角「注册」→ 手机号 + 短信验证码 |
| 3 | 控制台 → 账号信息 → **实名认证** → 选「个人认证」→ **微信/支付宝扫码，1 分钟**（**不需要营业执照**） |
| 4 | 费用中心 → 充值 **¥50**（支持微信/支付宝） |

### 1.2 买轻量应用服务器

| 步骤 | 操作 |
|---|---|
| 1 | 打开 **https://cloud.tencent.com/product/lighthouse** |
| 2 | 点「立即选购」 |
| 3 | **⚠️ 地域选「中国香港」** —— 买完**不能改**，这一步错了只能退款重买 |
| 4 | 镜像选 **Ubuntu 22.04 LTS**（**不要**选应用镜像，我们脚本自己装） |
| 5 | 套餐选**最低档**（2核1G 或 2核2G）—— 我们峰值只要 ~3 Mbps，最低档绰绰有余 |
| 6 | 时长选 **1 个月**（约 ¥24~30）—— **先验证效果，别年付** |
| 7 | 付款 → 微信/支付宝 |

> 想更便宜可以看活动页 **https://cloud.tencent.com/act**（新客常有折扣）。
> **但注意续费价**：促销价往往只首年有效。

### 1.3 拿凭据

| 步骤 | 操作 |
|---|---|
| 1 | 控制台 → 轻量应用服务器 → 点进实例 → 记下**公网 IP** |
| 2 | 「重置密码」→ 设 root 密码（或上传 SSH 公钥） |
| 3 | 防火墙 → 添加规则，放行 **22**（SSH） |

> 443 端口**现在不用放行** —— 等 nginx 装好、证书签好再放。

---

## 2. 域名（可选但推荐）

**方案 A（推荐）**：加一个子域名，两条路并存

| 记录 | 指向 | 用途 |
|---|---|---|
| `moss.wujiaitool.cn` | **保持不动**（CF 隧道） | 现状，随时可回退 |
| `hk.wujiaitool.cn` | **VPS 公网 IP** | 新路径，测试用 |

**方案 B**：暂时不用域名，直接 `https://<VPS_IP>:8443` 访问
（证书是自签的，浏览器会警告；仅用于内部验证）

> ⚠️ DNS 生效一般 1~10 分钟。**先加记录再签证书**，
> 否则 Let's Encrypt 的校验会失败。

---

## 3. 部署 VPS 侧（一条命令）

> **✅ 已部署完成（2026-09-26）**。实际落地时遇到一个原计划没有的情况，
> 已解决，见 §3.1。下面是当时的原始步骤，保留作参考。

把 `scripts/setup_frps.sh` 传到 VPS，然后：

```bash
# 生成一个强 token（隧道入口凭据，务必长且随机）
TOKEN=$(head -c 32 /dev/urandom | base64 | tr -d '/+=' | head -c 40)

bash setup_frps.sh \
  --domain hk.wujiaitool.cn \
  --token "$TOKEN" \
  --email 你的邮箱@example.com

# ★ 把 token 记下来，本机 frpc 要用同一个
echo "TOKEN=$TOKEN"
```

### 3.1 ★ 实际卡点：腾讯云安全组只放行 22 和 80

**原计划是 frpc 直连 VPS 的 7000**，但实测**连不上**：

| 端口 | 本机 → VPS |
|---|---|
| 22 | 通 |
| **80** | 通 |
| 443 | **超时** |
| 7000 | **超时** |

根因在 VPS 的 `iptables INPUT` 第一条：

```
Chain INPUT (policy DROP)
1  YJ-FIREWALL-INPUT  all  --  0.0.0.0/0  0.0.0.0/0
```

**`YJ-FIREWALL` 是腾讯云自己的安全组**，排在 ufw 规则**之前** ——
所以**在 VPS 里怎么配 ufw 都决定不了公网可达性**，必须去控制台加规则。

**采用的方案：frp 走 SSH 隧道，完全不碰控制台**

    浏览器 → VPS nginx(80) → frps(18110) → frpc → SSH隧道(22) → 本机 8110

```
frpc → 127.0.0.1:17000 ──(SSH 本地端口转发)──> VPS 127.0.0.1:7000 (frps)
```

- **不需要去腾讯云控制台加任何规则**（22 已经开着）
- **更安全**：7000 不必对公网开放
- 认证用 **SSH 密钥**（不是密码），见 §3.2

### 3.2 为什么隧道认证必须用密钥而不是密码

隧道进程要**长期稳定**地连 VPS。用密码意味着每次启动都得有
`VPS_PASS` 在环境里 —— 换个会话、或用计划任务自动拉起时环境一丢，
隧道就报 `No authentication methods available` 并**循环重连失败**，
而它的表现是"网站打不开"，与"后端挂了"**长得一模一样**（实测踩到）。

所以装了密钥（`~/.ssh/moss_hk_tunnel`，公钥追加到 VPS 的
`authorized_keys`，带 `moss-hk-tunnel` 标记便于撤销）。
`scripts/frp_ssh_tunnel.py` 默认用它，密码只作兜底。

### 3.3 进程必须「脱离父进程」启动

**`Start-Process` 不够**：它创建的进程与调用者的句柄绑定，
调用者的宿主（shell / AI 工具调用）一退出，子进程就被带走
（实测：隧道在工具调用结束后立刻消失，公网 502，而后端好着）。

正确做法是 `Win32_Process.Create` —— 实测父进程会变成 `WmiPrvSE.exe`，
即由 WMI 服务持有，与调用者完全解耦，可长期存活。

### 3.4 ⚠️ 别用「进程数」判断隧道是否正常

`frp_ssh_tunnel.py` 在 Windows 上会表现为**两个进程**（父 + 子），
与 uvicorn 同理。所以"有两个进程 = 重复了"是**错的**，
按数量清理等于定期误杀正在工作的进程（实测踩到，把链路打断）。

也**无法**从外部判断"哪个进程才是好的"（按 PID 顺序两种猜法都错）。

**唯一可靠判据 = `127.0.0.1:17000` 通不通**：
通 → 什么都不做；不通 → 全杀重启，不猜。
`scripts/frp_watchdog.ps1` 就是这么写的。

脚本会做：装 frps + nginx + certbot → 写配置 → 申请证书 →
配防火墙 → 自检并打印监听状态。

**它做对的三件关键事**（每件错了都不报错，只是不好用或不安全）：

1. `proxyBindAddr = "127.0.0.1"` —— frp 默认把落地端口绑 `0.0.0.0`，
   那等于开了一个**没有 TLS 的明文后门**。绑本机后，唯一入口是 nginx。
2. nginx 补 `X-Forwarded-For` —— 否则后端把所有用户看成本机地址，
   **登录限流与图形码判定全部失效**（见 `src/core/client_ip.py`）。
3. `transport.tls.force = true` —— 隧道本身也加密。

---

## 4. 部署本机侧（frpc）

### 4.1 下载 frpc

从 **https://github.com/fatedier/frp/releases** 下载
`frp_<版本>_windows_amd64.zip`，解压出 **`frpc.exe`**，放到项目 `bin\` 目录：

```
D:\code\Moss-finagent-research\bin\frpc.exe
```

### 4.2 配 frpc.toml

```powershell
cd D:\code\Moss-finagent-research
Copy-Item scripts\frpc.toml.example frpc.toml
notepad frpc.toml      # 改两处：serverAddr = VPS公网IP；auth.token = 第3步那个
```

> `frpc.toml` 已加进 `.gitignore`（里面是 token，**绝不能提交**）。

### 4.3 启动与自检

```powershell
# 启动
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\frpc_start.ps1

# 查状态（frpc 在不在、配置对不对、日志尾部）
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\frpc_start.ps1 -Status

# 停止（香港入口断开，CF 隧道不受影响）
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\frpc_start.ps1 -Stop
```

启动成功的标志：VPS 上 `ss -tlnp | grep 18110` 能看到
`127.0.0.1:18110` 在监听。

---

## 5. 验证

```powershell
# 本机后端（基准）
curl.exe -s -o NUL -w "%{http_code} %{time_total}s`n" http://127.0.0.1:8110/api/v1/health/live

# 新路径（香港）
curl.exe -s -o NUL -w "%{http_code} %{time_total}s`n" https://hk.wujiaitool.cn/api/v1/health/live

# 旧路径（CF，保留）
curl.exe -s -o NUL -w "%{http_code} %{time_total}s`n" https://moss.wujiaitool.cn/api/v1/health/live
```

**期望**：香港那条应该明显更快、且**不出现 000**。

### A/B 对比脚本（各打 20 次，看成功率与延迟分布）

```powershell
foreach ($u in @('https://moss.wujiaitool.cn','https://hk.wujiaitool.cn')) {
  $ok=0; $fail=0; $t=@()
  1..20 | ForEach-Object {
    $r = curl.exe -s -o NUL --max-time 15 -w "%{http_code}|%{time_total}" "$u/api/v1/health/live"
    $p = $r -split '\|'
    if ($p[0] -eq '200') { $ok++; $t += [double]$p[1] } else { $fail++ }
  }
  $s = $t | Sort-Object
  "{0,-32} 成功 {1}/20  中位 {2}s  最大 {3}s" -f $u, $ok, $s[[int]($s.Count/2)], $s[-1]
}
```

---

## 6. 切正式流量（确认新路径没问题之后）

改 `moss.wujiaitool.cn` 的 DNS：从 CF 代理（橙云）**改成 A 记录指向 VPS IP**。

> ⚠️ **改之前先确认**：`hk.wujiaitool.cn` 已经稳定跑够时间（建议 ≥1 天），
> 且你的用户分布验证过。

**回滚**：把 DNS 改回 CF 代理即可，**秒级生效**，CF 隧道一直没动过。

---

## 7. 安全加固清单（**别跳过**）

| 项 | 做法 | 为什么 |
|---|---|---|
| **SSH 换端口 + 密钥登录** | 改 `/etc/ssh/sshd_config`：`Port 2222`、`PasswordAuthentication no`、`PermitRootLogin prohibit-password` | 公网 22 端口每天被爆破 |
| **防火墙** | 脚本已配：只留 22 / 80 / 443 / 7000 | 最小暴露面 |
| **frp token** | ≥32 位随机，放 `.env` 或 `frpc.toml`（已 gitignore） | token 泄露 = 别人能暴露你 VPS 的端口 |
| **关掉 nginx 版本号** | `server_tokens off;` | 减少指纹 |
| **定期更新** | `apt-get update && apt-get upgrade -y`（或开自动安全更新） | |
| **快照备份** | 云控制台开自动快照 | VPS 故障时可快速重建（**注意：VPS 上没有你的业务数据**，只有配置，所以代价很低） |

### 「代码会不会泄露」——结论

**不会**。整条链路里 VPS 只做转发：

```
浏览器 → VPS nginx(终止 TLS) → frps → frpc(你的机器) → 127.0.0.1:8110
```

- 你的**代码、Ollama 模型、SQLite 数据库**全部在你自己这台机器上，**不上传**
- VPS 上只有 `frps` + `nginx` 两个程序与配置文件
- VPS 能看到的**流量内容**，与今天 Cloudflare 能看到的是**同一批**
  （TLS 都在边缘终止）—— 暴露面**没有增加**

想连 VPS 也看不到内容，可以把 TLS 终止放回你本机
（nginx 只做 TCP 转发），代价是拿不到 Let's Encrypt 证书、
且本机要开 443 对外。**本项目不需要做到这一步。**

---

## 8. 常见问题

### 要不要在 VPS 上开 IPv6？——**不要**（实测依据）

2026-09-26 实测本机到 CF 边缘（同一时段、同样 5 次）：

| 地址族 | TCP 连接延迟 | 成功率 |
|---|---|---|
| **IPv4** | min 0.208s / 中位 **0.211s** / max 0.219s | **5/5** |
| **IPv6** | min 0.300s / 中位 **0.309s** / max 0.311s | 4/5 |

本机**已有**全局 IPv6（`2408:820c:752e:b310:...`，路由器通告下发），
但这条 IPv6 路径**比 IPv4 慢约 100ms 且丢过一次**。所以：

- **不开**：反向代理的延迟由"你到 VPS"决定，**与 VPS 的地址族无关**；
  而多一个地址族就多一类"连不上但看不出原因"的故障（frps 绑了 v6、
  frpc 走 v4 就是不匹配）。
- **保持单栈 IPv4 = 只有一个变量**，符合"先拿到确定性结论"的目标。
- 腾讯云的 IPv6 是**免费**的，随时可以开 —— 等香港方案验证完、
  有明确需求时再说。

> `scripts/setup_frps.sh` 里已显式写了 `bindAddr = "0.0.0.0"`，
> 就是为了**防止将来开 IPv6 后默认绑定行为变化导致隧道静默连不上**。

### 其他常见问题

| 现象 | 原因与处置 |
|---|---|
| `frpc_start.ps1` 说缺 frpc.exe | 没下载，或放错目录（要 `bin\frpc.exe`） |
| frpc 起来了但 VPS 上 18110 没监听 | token 不一致 / VPS frps 没起（`systemctl status frps`） |
| 502 Bad Gateway | nginx 到了但 frpc 没连上 → 查 `data\run\frpc.log` |
| 证书申请失败 | DNS 还没生效，或 80 端口没放行（certbot 走 HTTP 校验） |
| 页面能开但登录后一直转 | nginx 少了 `X-Forwarded-For` → 后端 IP 判定异常 |
| WebSocket 不推通知 | nginx 少了 `Upgrade`/`Connection` 头（脚本已带） |
| **所有人共用一个限流计数** | 同上，`X-Forwarded-For` 没补 |

---

## 9. 与 CF 隧道的关系（重要）

**两条路并存，互不干扰**：

| | CF 隧道 | 香港 frp |
|---|---|---|
| 进程 | `cloudflared`（**SCM 服务**，非裸进程） | `frpc.exe` + `frp_ssh_tunnel.py` |
| 值守 | `MossTunnelWatchdog`（每 5 分钟，**按需**，见下） | `MossFrpEnsure`（每 5 分钟，判据=公网入口 200） |
| 入口 | `moss.wujiaitool.cn` | `hk.wujiaitool.cn` |
| 状态 | **降级为备用，不做常规值守** | **生产入口** |

两条同时活着意味着**双倍入口**，但**不冲突**：它们各自连各自的
服务端，最终都回到本机 8110。回滚成本为零。

### 9.1 ★ 2026-09-27 修正：`MossTunnelWatchdog` 改成「按需值守」

**上面那张表原来写的是"CF 隧道 = `MossTunnelWatchdog` 每 5 分钟值守 /
香港 frp = 暂无值守"，正是这个错配造成了实测故障。**

切换生产入口后，`MossTunnelWatchdog` 仍然每 5 分钟去探测**已降级的
CF 备用**，连续失败就 `Restart-Service Cloudflared`。三个问题叠在一起：

1. 它在**修一条没人用的路**，而真正在服务用户的 `hk` 入口**当时无人值守**。
2. CF 这条路在本机宽带上**结构性劣化且修不好**（实测 3 次探测
   `超时 / 200(5.1s) / 超时`；`cloudflared tunnel info` 连
   `api.cloudflare.com` 都 `context deadline exceeded`；IPv6 建连 1296ms
   vs IPv4 160~209ms，1400 字节包 100% 丢）。于是它**每 5 分钟就要重启一次**。
3. 重启 cloudflared 会**中断所有在途请求** —— 净效果是自造抖动。

**现行判据（顺序不能换，见 `scripts/tunnel_watchdog.ps1` 头部）：**

| 条件 | 动作 |
|---|---|
| ① 主用 `hk` 入口 200 | **直接退出，零副作用**（绝大多数 tick） |
| ② 主用不通 + 本机后端也不通 | 不碰隧道（归 `MossPilotWatchdog`） |
| ③ 主用不通 + 备用能通 | 记一行，**不处置**（备用仍算可用，主用归 `MossFrpEnsure`） |
| ④ 主用与备用**同时**不通 | 这时备用真的被需要 → `Restart-Service Cloudflared` |

一句话：**只有"主用挂了、备用也挂了"才动手。**

> ⚠️ 判据顺序换了就会退回旧故障：若把"备用是否健康"放到第一步，
> 就会重新变成"每 5 分钟修一条没人用的路"。

> ⚠️ **重启必须走 `Restart-Service`，绝不能自己 `taskkill` + `Start-Process`**：
> cloudflared 由 SCM 托管且带 `FAILURE_ACTIONS = RESTART`，自己起进程会和
> SCM 的自动恢复打架，造出**两个实例**互相抢同一个隧道（2026-09-26 实测事故）。
