# 故障排查指南

## 一、Ollama相关问题

### 问题：显存不足
**现象**：Ollama报错“CUDA out of memory”
**解决方案**：
- 使用Q4量化模型：`ollama pull qwen3.5:4b-q4_0`
- 降低max_tokens：在configs/models.yaml中设置max_tokens=1024
- 关闭不必要的后台服务

### 问题：模型响应慢
**现象**：本地模型响应超过10秒
**解决方案**：
- 检查GPU利用率：`nvidia-smi`
- 减少并发请求数
- 使用更小的模型（如qwen3.5:1.5b）

## 二、数据库相关问题

### 问题：`disk I/O error` —— SQLite 打开立刻失败，重启后前端几十秒没数据

**现象**（2026-09-17 用户报障"每次重启服务前端加载都很慢"）：

```
路由DB查询异常，穿透网络: stock_close:300750 -> disk I/O error
```

主库本身完好（729MB），但**任何**普通打开方式都在 0.000s 内失败：

| 打开方式 | 结果 |
|---|---|
| `sqlite3.connect("data/moss_finagent.db")` | ❌ `disk I/O error` |
| `sqlite3.connect("file:...?mode=ro", uri=True)` | ❌ `disk I/O error` |
| `sqlite3.connect("file:...?mode=rw", uri=True)` | ❌ `disk I/O error` |
| `sqlite3.connect("file:...?immutable=1", uri=True)` | ✅ 104 万行 / 0.04s |
| 把主库**单独**复制一份（不带伴生文件）再打开 | ✅ 数据完整 |

**根因**：`-wal` 伴生文件被截断成 **0 字节**、`-shm` 停在上一轮进程留下的
**64KB 陈旧 WAL 索引**（`-shm` 与当前 WAL 不匹配）。主库没坏，坏的是伴生文件。

**为什么会出现**：`manage.py` 的守护进程用 `taskkill /T /F` **硬杀**，进程没有
机会在退出前 checkpoint / 关闭连接；叠加 `--replace` 原先只按"端口监听者"找
旧实例，端口之外的历史实例会变成**孤儿进程**一直占着 `-wal`/`-shm` 的文件句柄
（现场实测：一个 12:28 启动的实例活到 16:51，CPU 累计 0.0s，纯占位；连
`Remove-Item` 都因文件被占用而失败）。

**为什么表现为"前端很慢"**：`MacroRepository._connect()` 每次都新建连接，
于是**每一个**走该库的请求都再失败一次；仓储按设计 fail-open 穿透到网络链，
一次请求几十秒（实测重启后首个 `intraday/watchlist` **182.9s**，第二个 0.0s）。

**处理**：

```bash
python manage.py doctor     # 体检 + 自愈（把陈旧伴生文件改名备份到 data/recovery/）
python manage.py stop       # 端口外孤儿进程会被一并优雅停止
python manage.py start --daemon --replace
```

- 启动时 lifespan 会自动做同一件事（`src/core/sqlite_recovery.py`），
  正常库只多一次 `sqlite_master` 计数（0.00s 量级）；
- 关停时会 checkpoint 并把 WAL 收干，下次启动拿到的是干净的库；
- 自愈**只改名备份、从不直接删**：万一 WAL 里有未 checkpoint 的事务，可从
  `data/recovery/<库名>-<时间戳>/` 找回；主库文件永不改动；
- 关掉自愈：`MOSS_SQLITE_RECOVERY=0`。

| 现象 | 结论 | 动作 |
|---|---|---|
| `doctor` 显示"伴生文件挪不动：仍有进程占用" | 还有孤儿实例活着 | `python manage.py stop` 后重试 |
| `doctor` 显示"已自愈" | 陈旧 `-wal`/`-shm` 已备份 | 无需处理，重启服务即可 |
| 主库也读不出来（`immutable=1` 也失败） | 真损坏，不是伴生文件问题 | 从备份恢复，别自动处理 |

### 问题：PostgreSQL连接失败
**现象**：`connection refused`
**解决方案**：
- 检查PostgreSQL是否启动：`docker ps`
- 检查.env中的连接字符串
- 检查端口是否被占用：`netstat -ano | findstr 5432`

### 问题：Redis连接超时
**现象**：`Redis timeout`
**解决方案**：
- 检查Redis是否启动：`redis-cli ping`
- 检查防火墙设置
- 增加连接超时时间

## 三、API相关问题

### 问题：DeepSeek API返回429
**现象**：`Rate limit exceeded`
**解决方案**：
- 降低调用频率
- 使用指数退避重试
- 切换到备用模型

### 问题：API Key无效
**现象**：`401 Unauthorized`
**解决方案**：
- 检查.env中的API Key
- 确认API Key是否过期
- 确认账户余额充足

## 四、Agent相关问题

### 问题：Agent超时
**现象**：Agent执行超过预设超时时间
**解决方案**：
- 检查数据源是否可用
- 检查LLM调用是否超时
- 增加超时时间配置

### 问题：Agent输出格式错误
**现象**：JSON解析失败
**解决方案**：
- 检查Prompt中的格式约束
- 增加输出验证和重试机制
- 使用Pydantic模型强制校验

## 五、缓存相关问题

### 问题：缓存命中率低
**现象**：hit_rate < 50%
**解决方案**：
- 检查缓存键设计是否合理
- 增加TTL时间
- 检查数据更新频率是否过高

### 问题：缓存不一致
**现象**：缓存数据与数据库不一致
**解决方案**：
- 检查写缓存失效逻辑
- 检查发布-订阅通知是否正常
- 强制刷新缓存

## 六、日志与监控

### 查看Agent执行日志
```bash
tail -f logs/agents/A08_macro.log
```

### 查看API调用日志
```bash
tail -f logs/api/requests.log
```

### 查看审计日志
```bash
tail -f logs/audit/trace.log
```

## 七、数据源：东方财富 `push2*` 被 SNI 阻断（本机网络实测 2026-09-17）

### 现象

所有走东财 `push2.eastmoney.com` / `push2his.eastmoney.com` 的 akshare 接口
**全部失败**，错误都是：

```
ConnectionError: ('Connection aborted.', RemoteDisconnected('Remote end closed
connection without response'))
```

涉及接口（实测）：`stock_individual_fund_flow`、`stock_individual_fund_flow_rank`、
`stock_sector_fund_flow_hist`、`stock_concept_fund_flow_hist`、
`stock_sector_fund_flow_rank`、`stock_board_concept_cons_em`、
`stock_zh_a_hist_min_em`、`stock_zh_a_hist`。
而同花顺（`d.10jqka.com.cn`）、腾讯（`web.ifzq.gtimg.cn`）、
`datacenter-web.eastmoney.com`、`quote.eastmoney.com` **一直正常**。

### 排查结论（照抄可复现）

| 假设 | 实测 |
|---|---|
| DNS 解析失败 | ❌ 解析正常（多个 CDN IPv4 + IPv6） |
| TCP 443 不通 | ❌ 全部 IP 都能建连 |
| 反爬（UA/Referer） | ❌ 换浏览器 UA、加 Referer、加 `ut` token、JSONP 全无效 |
| 限频 | ❌ 同一 IP 连续多次请求都 200 |
| IPv6 优先（urllib3 默认） | ❌ 强制 IPv4-only 依然断 |
| **按 TLS SNI 阻断** | ✅ **URL 写 IP → 200；URL 写域名（SNI=域名）→ 立刻断**；同 IP、同参数、同 UA，只改 URL 里写域名还是写 IP |

**结论**：链路中间设备按 **TLS SNI** 匹配 `push2*.eastmoney.com` 并断开连接
（证书本身正常，SAN 里含 `*.push2his.eastmoney.com`）。TCP 与 DNS 都通，
所以不是"网络不通"，也不是程序问题：**任何实现（浏览器/curl/akshare）走域名都同样失败**。

⚠️ 这层阻断**按时间窗反复**：实测同一 IP 在几分钟内从"直连 200"变成"直连也断"，
因此**不存在稳定绕过**。

### 影响与既有对策（无需处理）

项目主链路不依赖东财：
- 资金流（板块 / 个股）→ **Tushare**（`moneyflow_ind_dc` / `moneyflow`，已实现）；
- 分钟K线 / 分时 / 实时价 → QMT 或 **腾讯**；
- 板块情绪 / 涨停池 → **同花顺**；
- 龙虎榜等东财独有数据 → 暂时不可用（`gap` 会如实标注）。

### 想让东财链路也恢复时的两个办法

1. **换网络 / 换出口**（手机热点、代理、其它宽带）——最直接；
2. 打开内置规避开关（默认关闭）：

```powershell
$env:MOSS_EM_DIRECT="1"    # 开启"直连 IP + Host 头"回退
python manage.py start --port 8100 --replace
```

开启后：请求先按原样走域名；**只有**命中阻断特征才改为
`https://<IP><路径>` + `Host: 真实域名` + `verify=False`（证书与正确 SNI 二选一，
这是被阻断条件下的唯一组合）。探测不到可用 IP 时**照常报错**，不会伪造数据。
排障时可看 `src.core.eastmoney_direct.probe_report()` 得知试过哪些 IP。

> 为什么默认关闭：阻断按时间窗反复，给一个必然失败的请求白加"探测 + 重试"
> 只会让失败更慢。开关交给使用者按当时网络情况决定。
