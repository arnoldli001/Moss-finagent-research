---
name: finagent-lessons
description: |
  多 Agent 投研/AI 应用开发踩坑清单与举一反三规则。
  当用户构建 LLM Agent 编排、Agent 工具链、数据管道、成本/性能优化时调用本 skill。
  来源：Moss-FinAgent-Research 项目第八轮 + 第九轮优化后的经验汇总。
triggers:
  - LLM Agent 编排
  - 多 Agent 协作
  - Agent 成本优化
  - LLM 网关
  - Token 优化
  - LLM 缓存
  - 投研分析
  - 知识库问答
  - RAG / 检索增强
  - ReAct / Function Calling
version: 1.0
source_project: Moss-FinAgent-Research
source_date: 2026-09-28
---

# Moss-FinAgent-Research AI 编程教训 skill

> 这是从 Moss-FinAgent-Research（多 Agent AI 投研系统，19 Agent 编排，13 万行代码，
> 2500+ 测试，250+ 轮优化）开发过程中提取出的**通用 AI 编程反模式与举一反三规则**。
>
> 使用方式：AI 助手在写 LLM Agent 相关代码前，**必读本文 §1-§3 的"AI 不足点"**；
> 写完后，**必跑 §5 "自检清单"**——任一项未做 = 必须补做。

> **分工**（三个 skill 别用错）：
> * 本文 = **写新代码**时我的**设计**有没有系统性偏好；
> * `skills/pipeline-redundancy-audit/` = 某段计算/接口/表**该不该存在**；
> * `skills/ai-dev-loop-discipline/` = **改一个正在跑的系统**时的取证/半径/完成/交付纪律
>   （"我以为我知道系统现在是什么样"是那一类最常见的错）。

---

## §1 · AI 编程的 12 个"系统性偏好"（最大的反模式）

> AI 不是不会写代码，而是**有偏好**。这些偏好与"工程正确"经常冲突。

### 1.1 AI 喜欢"全量"——能精简必精简

| 现象 | 反例（投研项目里出现） | 正确做法 |
|---|---|---|
| 给 5 个 Agent 喂同一份全量数据 | A08-A12 tokens_in=25149 **完全相同** | `payload_fn(agent_id)` 按 Agent 范围裁剪——A10 只需 PE/PB、A11 只需负债率、A12 只需 events |

**规则**：
- 写多 Agent 协作代码前，**先列出"每个 Agent 真正需要什么"**
- 写一个 `_AGENT_DATA_WHITELIST` 字典（agent_id → keyword 列表）
- 默认行为是"按 agent_id 过滤"，而非"全量广播"

### 1.2 AI 喜欢"默认安全"——能严必严

| 现象 | 反例 | 正确做法 |
|---|---|---|
| `max_steps=3` 写死、scope 用空串、fallback 默认放付费云端 | light/medium 层 primary 是本地，fallback 是 deepseek-flash（**本地抖动悄悄花钱**） | 在 `configs/*.yaml` 里 `local_only: true` 默认值严；fallback 默认是"更便宜的选项" |

**规则**：
- 任何**默认行为**都应该是"更难出错"的
- 任何**"fallback"**都应该是"更便宜的"或"更安全的"
- 写配置项时问："如果某个调用方忘了传这个参数，会发生什么？"——默认应该是"安全"的那边

### 1.3 AI 喜欢"循环重发"——能增量必增量

| 现象 | 反例 | 正确做法 |
|---|---|---|
| ReAct 每轮重发同一份 payload | A17 prompt = 7582 tokens × 3 步 = 22746 tokens_in | 增量协议：第 1 步发全量，第 2+ 步只发"上次输出摘要 + 新 observation" |

**规则**：
- 任何 `while` / `for` 循环里**重发同一数据**的协议都要改增量
- 写"增量协议"模板：`base_context` (第一次全发) + `delta` (后续只发增量)
- observation 也要截断（如 `_OBS_TRUNCATE = 800`）

### 1.4 AI 不会"测量"——先测后改

| 现象 | 反例 | 正确做法 |
|---|---|---|
| 拍脑袋说"优化 LLM tokens"，但实测瓶颈在 IO/串行/缓存 | 集合竞价选股"先优化规则"，但实测规则只占 0.66%、生态序列占 99.3% | **cProfile / 端到端计时先于改代码**——优化对象判断比优化本身重要 |

**规则**：
- 改任何热点代码前，**先跑 cProfile / 端到端计时**
- 写 "黄金回归"（bit-by-bit diff）防止纯重构引入回归
- "我感觉这里慢" ≠ "这里真的慢"——必须有数据

### 1.5 AI 不会"防自己"——fallback 默认值要严

| 现象 | 反例 | 正确做法 |
|---|---|---|
| "贴心的 fallback"自动启用 | light 层 fallback 是 deepseek-flash（本地抖动时**悄悄花钱**） | 配置项默认 `local_only: true`；新调用方默认就是安全的 |

**规则**：
- 任何 AI 写的 `try/except` 默认 fallback 都要问："这个 fallback 是更安全还是更花钱？"
- 真要花钱的少数场景，显式传 `local_only=False`（"显式退出"原则）

### 1.6 AI 不会"写在文档里"——防御代码必须有测试覆盖

| 现象 | 反例 | 正确做法 |
|---|---|---|
| docstring 写"已实现"，实际是 `return stmt` 原样返回 | RLS 假实现：docstring 说"注入 WHERE tenant_id=?"，实际什么都没做 | **写防御代码必写测试覆盖"是否真的过滤了"** |

**规则**：
- 任何"权限/审计/合规"相关代码必须有"未授权 → 失败"的测试
- CI 门禁要"红线即失败"（与普通测试失败的处置不同）

### 1.7 AI 不会"哈希链"——审计必 LLM-free

| 现象 | 反例 | 正确做法 |
|---|---|---|
| "审计也用 LLM 打分"是常见反模式 | A18 调 LLM 做完整性校验（**审计被攻陷就全完**）| **A18 零 LLM**——纯本地完整性校验 + 哈希链封存 |

**规则**：
- 任何"审计/合规/告警"路径**必 LLM-free**
- 审计必带 `prompt_hash/response_hash/tokens/错误/降级链`

### 1.8 AI 不会"折钱"——优化若不能折成钱就无法验收

| 现象 | 反例 | 正确做法 |
|---|---|---|
| token 统计 ≠ 钱 | `deepseek-v4-flash` 跑了 309 次算成 0 元（已改名未登记价格）| `LLMResponse.cost_yuan` 网关写入；`call_cost_cny` 单一计价 |

**规则**：
- 任何"优化"都要能折成钱——`call_cost_cny` 单一函数（避免账本/监控分叉）
- 日预算要有"预扣 + 结算"机制（防 TOCTOU）
- 模型名与 `models.yaml` 配置名**必须**一致——不一致 → 静默算成 0 元

### 1.9 AI 不会"白名单"——黑名单会漏下一个新字段

| 现象 | 反例 | 正确做法 |
|---|---|---|
| 黑名单式"剔除某字段" | `raw_data.source_tag = "sina"` 原样出接口 → 渠道名泄漏 | `raw_data` 走**白名单**（`frozenset({"importance"})`） |

**规则**：
- 任何"敏感数据出接口"用**白名单**（允许的字段），不用黑名单（禁的字段）
- "新字段默认不出"是更安全的默认

### 1.10 AI 不会"假实现"——假实现比没有更危险

| 现象 | 反例 | 正确做法 |
|---|---|---|
| docstring 写"已实现" | `_inject_tenant_filter` 取了租户、遍历了表，然后 `return stmt` 原样返回 | 写代码必**自测**：防御代码"未授权时抛错"——让假实现显形 |

**规则**：
- 写完任何"防御/安全/合规"代码后，**主动构造一个"未授权"的输入验证它真的拒绝**
- 如果代码只是"看起来做了"而实际"什么都不做"——测试会失败，假实现就被抓

### 1.11 AI 不会"end-to-end"——单节点优化不等于端到端优化

| 现象 | 反例 | 正确做法 |
|---|---|---|
| 优化某个节点，但端到端时间不变 | A17 优化 −55% cost，但端到端时间只省 5-10s | **优化要从端到端测量入手**——单节点优化 ≠ 端到端优化 |

**规则**：
- 任何优化前**先画端到端时间分布**（哪些阶段、各占多少）
- 优化目标是**缩短端到端**（不仅是单节点）

### 1.12 AI 不会"带宽"——后端快 ≠ 用户快

| 现象 | 反例 | 正确做法 |
|---|---|---|
| "DB 快 = 用户快"是错的 | `/alerts` 列表 100KB 响应 → 51 KB/s 隧道下首屏 5s | **带宽比 DB 慢**——列表要瘦身（白名单 + 缓存 + 字段裁剪） |

**规则**：
- 列表接口必须按"用户真用"裁剪字段
- 大响应要 `for_list=True` 单独路径
- 缓存命中要"零往返"——`/bootstrap` 一次往返合并多接口

---

## §2 · 落地的"必做动作"清单（按优先级）

### 2.1 P0 · 第一天就要做（不做会后悔）

1. **LLM 网关统一入口**：所有 LLM 调用必须过网关，禁止 Agent 直连模型 API
2. **模型分层路由**：按任务难度分 light/medium/reasoning/decision 四层
3. **熔断 + 降级链**：三层状态机（CLOSED/OPEN/HALF_OPEN）+ 链式降级
4. **配置类错误不计入熔断**：401/402/403 是确定性错误，重试不愈
5. **缓存键覆盖全部影响参数**：tier + json_mode + scope + schema
6. **空响应不命中 + 不写缓存**：否则"永久性失败"
7. **取消令牌在 4 类检查点**：节点入口 / 规划前 / ReAct 每步 / provider 调用前
8. **白名单脱敏**：敏感数据出接口用白名单，不用黑名单
9. **审计必 LLM-free**：审计/合规/告警路径不能调 LLM
10. **成本核算到分**：任何 token 优化都要能折成钱

### 2.2 P1 · 第一周要做（性能明显）

1. **按 Agent 范围裁剪 payload**：`_AGENT_DATA_WHITELIST` 字典
2. **A17 ReAct 增量协议**：第 2+ 步只发"上次输出摘要 + 新 observation"
3. **结果缓存 + 同问合流**：相同 query 直接复用，不重跑管线
4. **并发闸门 + 队列超时**：N 个并发用户不会击穿 provider 熔断
5. **预扣 + 结算**：日预算闸门要"预扣"（防 TOCTOU 集体超额）
6. **小模型传 json_schema**：1.5B 必须受约束解码，否则输出坏 JSON
7. **A12 合规走纯规则**：当本地规则给出"无"时跳过 LLM，节省云端
8. **三级流量治理**：结果缓存 + 同问合流 + 日预算 + 并发闸门
9. **A08-A12 全量广播改为白名单**：省 ~2万 tokens_in/轮
10. **死代码与魔鬼数字清理**：CI 跑 `scan_same_name_conflicts.py`

### 2.3 P2 · 第一个月要做（架构可维护性）

1. **物化表替代 GROUP BY**：max_ma5、缓存索引等
2. **React.lazy 路由级 code splitting**：主 chunk −30%
3. **IntersectionObserver 懒加载 iframe**：避免白屏
4. **多租户 → tenant + user 双层维度**：机构内部标配
5. **合规门禁 = CI 红线**：门禁上线当天抓到 3 个真问题
6. **黄金回归防纯重构**：47 只票 × 12 维逐位 diff
7. **定时炸弹测试检测器**：`scripts/scan_date_bombs.py`
8. **A19 数据缺口自修复**：白名单 + 频率上限 + 沙箱 + AST 四件套

---

## §3 · 30 条具体坑（**带避坑建议**）

> 来自 `docs/DEV_EXPERIENCE_TABLE.md` §三，已交叉验证。

### 缓存类
1. **缓存键缺参数** → 切档位拿到上一档结果——必列"什么会让结果不同"
2. **空响应写缓存** → 永久性失败——必加 `hit.content.strip()` 防护
3. **trace_id 用时间戳** → cache miss——必用 `sha1(stable_input)[:16]`
4. **同步 IO 在 async 服务** → 9ms 阻塞全并发——必走线程池
5. **同问合流不释放** → 永久占位——finally 收尾

### 性能类
6. **"全量广播"** → A08-A12 tokens_in=25149 完全相同——按 Agent 白名单过滤
7. **ReAct 每轮重发** → 7582 tokens × 3 步 = 22746 tokens_in——增量协议
8. **fallback 顺手花钱** → 本地抖动悄悄花云端——配置项默认 `local_only: true`
9. **Ollama 单 slot 串行** → 9 路并发 max(单 agent)——多实例 LB（需硬件）
10. **"_best_achievable" Python 双层循环** → 1.27M 调用——numpy 向量化

### 安全类
11. **假实现** → RLS 假实现比没有更危险——自测"未授权时是否真的拒绝"
12. **黑名单脱敏** → 新字段会泄漏——白名单
13. **个人信息进版本库** → 真实邮箱/默认密码——CI 红线 + 假得明显的占位符
14. **跨租户共享自选池** → 多用户串号——`tenant_id + user_id` 双层
15. **审计用 LLM 打分** → 审计被攻陷就全完——审计必 LLM-free

### 稳定性类
16. **401/402/403 计入熔断** → 3 次后熔断 → 病因被淹没——`count_as_failure=False`
17. **本地抖动悄悄花钱** → fallback 默认放云端——配置默认 `local_only: true`
18. **A19 每轮稳定触发** → 50 次调用 162k tokens 输出（23%）——黑名单 + 频率上限
19. **空缓存固化失败** → cache miss 后用空值重试——空响应不写
20. **取消令牌放得不够密** → 用户点了没反应——4 类检查点（节点入口/规划前/ReAct每步/provider前）

### 架构类
21. **A17 撞 4096 上限** → 98 次输出撞墙——`max_tokens=8192`，按调用覆盖
22. **fastapi 短连接** → 9 处 `conn.close()`——连接池
23. **config CWD 相对** → 服务不在仓库根启动 → 配置静默失效——绝对路径 + env 覆盖
24. **同名不同值常量** → A 股年化用 252 天高估 4%——CI 扫
25. **魔鬼数字 7 种截断并存** → 消息长度不一致——三档单一权威

### 性能 + 用户体验
26. **列表响应 100KB** → 51 KB/s 隧道下首屏 5s——白名单 + 缓存 + 字段裁剪
27. **iframe 16s 白屏** → 800KB HTML + ECharts + 51 KB/s 隧道——IntersectionObserver + 骨架屏 + 超时
28. **while True 无上界** → LLM 无限刷工具不收敛——`max_steps` + 兜底返回
29. **大文件上帝对象** → `intraday/service.py` 2526 行——按职责拆包
30. **定时炸弹测试** → fixture 锚"写代码那天"——`scan_date_bombs.py`

---

## §4 · 举一反三的 5 条核心原则

> 任何优化/重构前，**先问这 5 条**——任一不满足，先停下来。

1. **"测了再改 > 先改再测"** — cProfile、端到端计时先于拍脑袋改代码。
2. **"可被钱验证 > 不可验证"** — 任何优化若不能折成钱（cost_yuan），就是空口。
3. **"白名单 > 黑名单"** — 系统设计里"必须做"比"必须不做"更可靠；`local_only: true` 写在配置里比每个调用点记得传更可靠。
4. **"增量 > 全量"** — 任何循环协议都该"第 2+ 步只发增量"（ReAct、A17、缓存键、采集链路）。
5. **"硬上界 > 软保险"** — `max_steps`/TTL/并发数都该有上限 + 体面退出，而不是"尽量保险"。

---

## §5 · 自检清单（写完代码必跑）

> **本节是"必做动作"清单**。写完任何 LLM Agent 相关代码，**逐项打勾**——
> 任一项未做 = 必须补做。

### 5.1 网关层（必须做）

- [ ] 所有 LLM 调用是否都过统一网关（不是 `openai.chat` 直连）？
- [ ] 网关是否有熔断器（CLOSED/OPEN/HALF_OPEN 三态）？
- [ ] 熔断器是否区分"配置错误（401/402/403）不计入"与"瞬时故障计入"？
- [ ] 网关是否有降级链（model1 → model2 → ...）而不是原地重试？
- [ ] 网关是否有输入截断（保头保尾，非切尾部 schema）？
- [ ] 网关是否有 `cost_yuan` 字段写入响应？
- [ ] 网关是否区分"缓存命中"与"穿透"（cache_kind=exact/semantic/none）？

### 5.2 缓存层（必须做）

- [ ] 缓存键是否覆盖全部影响参数（`tier + json_mode + scope + schema`）？
- [ ] 缓存是否区分 `(agent_id, scope)` 分桶（防止跨 Agent 串台）？
- [ ] 缓存是否"空响应不命中 + 不写缓存"（两处对称判断）？
- [ ] 缓存是否有 TTL + 容量双阈值淘汰策略（防长跑进程单调增长）？
- [ ] 缓存索引是否在进程内（避免每次未命中扫盘）？

### 5.3 模型路由（必须做）

- [ ] 是否按任务难度分层（light/medium/reasoning/decision）？
- [ ] `local_only` 是否写在配置里（而非每个调用点记得传）？
- [ ] `trace_id` 是否稳定（用 sha1 而不是时间戳）？
- [ ] `use_cache=False` 是否有明确理由（否则一律走缓存）？
- [ ] 模型名与 `models.yaml` 配置名是否一致（不一致会算成 0 元）？

### 5.4 Agent 编排（必须做）

- [ ] 每个 Agent 是否只看到它**真正需要的数据**（不"全量广播"）？
- [ ] Agent 输出是否有 `agent_id + data_refs + trace_id` 必填字段？
- [ ] Agent 是否区分"本地规则计算的数字"与"LLM 解读"（前者确定性强）？
- [ ] Agent 是否有"体面退出"路径（不抛异常把整链路打断）？
- [ ] Agent 之间的消息是否有 schema（避免 LLM 自由格式）？

### 5.5 ReAct / 工具调用（必须做）

- [ ] 是否有 `max_steps` 硬上界 + 兜底返回？
- [ ] 第 2+ 步是否"只发增量"（不发全量 payload）？
- [ ] 工具调用是否有 4 类闸门（receiver 在白名单 / 同问题幂等 / 单 Agent 追问数 / 工具存在）？
- [ ] 闸门是否在 provider 调用之前（不是事后检查）？
- [ ] LLM 输出 schema 是否包含 `final_answer` 与 `action` 二选一？

### 5.6 成本与可观测性（必须做）

- [ ] `call_cost_cny` 是否唯一计价实现（账本与监控共用）？
- [ ] 日预算是否有"预扣 + 结算"机制（防 TOCTOU）？
- [ ] 高额脚本是否有"入口拒绝 + 跑到一半主动中止"双护栏？
- [ ] LLM 审计是否记录 `prompt_hash/response_hash/tokens/错误/降级链`？
- [ ] 审计是否 LLM-free（`prompt_hash + SHA256 链` 而非"AI 给 AI 打分"）？
- [ ] 审计是否带 `cost_yuan` 字段？

### 5.7 多租户 / 安全（必须做）

- [ ] RLS 是否**真实现**（不是 `return stmt`）——自测"未授权时是否真的拒绝"？
- [ ] 多租户是否到 `tenant_id + user_id` 双层？
- [ ] 信息隔离墙是否三维正交（RBAC × ABAC × Chinese Wall）？
- [ ] 跨墙调用是否**必须显式传** `WallCrossing(approver, reason)` 必填对象？
- [ ] 敏感数据脱敏是否**白名单**（不是黑名单）？
- [ ] `.env.example` 占位符是否"假得很明显"——CI 红线抓？

### 5.8 前端 / 用户体验（必须做）

- [ ] 列表接口响应是否瘦身（白名单 + `for_list=True` 单独路径）？
- [ ] iframe / 大组件是否 `IntersectionObserver` 懒加载？
- [ ] 首屏是否有骨架屏（不是空白 + 转圈）？
- [ ] React.lazy 是否按路由拆分（主 chunk −30%）？
- [ ] 失败有降级（不是"等加载中"——要"显示旧数据 + 刷新"）？

### 5.9 测试 / CI（必须做）

- [ ] 任何"防御/安全/合规"代码是否有测试覆盖（"未授权 → 失败"）？
- [ ] 任何"纯重构"是否有黄金回归（bit-by-bit diff）？
- [ ] CI 是否有合规门禁（与普通测试失败处置不同——红线即不允许合并）？
- [ ] 是否有 `scan_same_name_conflicts.py`（扫魔鬼常量）？
- [ ] 是否有 `scan_date_bombs.py`（扫定时炸弹测试）？
- [ ] 测试是否真的在 CI 跑（不是"本地能过"）？

### 5.10 数据安全 / 故障转移（必须做）

- [ ] 不可达源是否置末尾 + 默认关闭（如 QMT 失联）？
- [ ] 真实源失败是否**绝不回退 simulated**（宁报缺口）？
- [ ] 配置路径是否绝对路径 + env 可覆盖（不是 CWD 相对）？
- [ ] 已知故障源是否有显式黑名单（A19 自修复不会反复触发）？

---

## §6 · 关键术语对照表

| 术语 | 含义 | 项目里的具体实现 |
|---|---|---|
| 网关 | LLM 统一入口 | `src/infrastructure/llm/gateway.py` |
| 熔断器 | 防止 provider 故障雪崩 | `src/infrastructure/llm/circuit_breaker.py` |
| 降级链 | 失败时自动跳到下一个模型 | `routing[tier] = [primary, fallback]` |
| 缓存键 | `sha256(normalize(system) + normalize(prompt) + scope)` | `src/infrastructure/llm/cache.py` |
| trace_id | 任务级 token 累计键 | 用 `sha1(stable_input)` 而非时间戳 |
| cost_yuan | 单次调用真实费用（元） | `LLMResponse.cost_yuan` |
| call_cost_cny | 唯一计价实现 | `src/core/budget.py:152` |
| payload_fn | Agent 的输入装配函数 | 按 Agent 白名单过滤 |
| ReAct | 推理 → 动作 → 观察 → 推理循环 | `src/domain/agents/analysis/react.py` |
| HallucinationGuard | 数字/代码 ground 校验 | `src/infrastructure/llm/hallucination_guard.py` |
| AuditChain | SHA256 前后链接 | `src/infrastructure/repositories/audit_chain.py` |
| WallCrossing | 跨信息隔离墙的显式授权对象 | `src/core/policy.py` |
| Chinese Wall | 信息隔离墙（机构内部合规） | `src/core/policy.py` |

---

## §7 · 落地清单（工程师视角）

> 当用户要做"多 Agent 投研/AI 应用"时，**按本节清单逐项做**——把 30 条坑预防在写代码前。

### 第一天（基础设施）

1. LLM 网关（统一入口 + 熔断 + 降级链 + 截断 + cost_yuan）
2. 缓存层（精确 + 语义 + 空响应防护 + TTL + 容量淘汰）
3. 模型分层路由（light/medium/reasoning/decision）
4. 配置类错误不计入熔断（401/402/403）
5. `cost_yuan` 单一计价实现 + 日预算预扣
6. 取消令牌 4 类检查点
7. 白名单脱敏 + 审计 LLM-free
8. CI 合规门禁（红线即失败）

### 第一周（性能与成本）

1. Agent 白名单过滤 payload
2. ReAct 增量协议（第 2+ 步只发增量）
3. 结果缓存 + 同问合流 + 并发闸门
4. 预扣 + 结算
5. 小模型传 json_schema
6. A12 合规走纯规则
7. 三级流量治理

### 第一个月（架构）

1. 物化表替代 GROUP BY
2. React.lazy 路由级 code splitting
3. IntersectionObserver 懒加载
4. tenant + user 双层维度
5. 合规门禁 = CI 红线
6. 黄金回归防纯重构
7. A19 数据缺口自修复（四件套）

---

> **使用本 skill 的方式**：
> 1. **写代码前**：读 §1 的 12 个系统性偏好，**避免 AI 写代码时的本能错误**
> 2. **写代码后**：跑 §5 的 10 类自检清单，**逐项打勾**
> 3. **踩坑后**：查 §3 的 30 条具体坑 + §1 的反模式，**找到对应反模式**
> 4. **举一反三**：用 §4 的 5 条核心原则，**评估当前架构是否违反**
>
> 文件位置：`skills/finagent-lessons/SKILL.md`
> 来源项目：Moss-FinAgent-Research（19 Agent 多 Agent 投研系统）
> 来源时间：2026-09-28（第九轮优化完成后）
> 字数：约 700 行 / ~25 KB
> 引用：`docs/DEV_EXPERIENCE_TABLE.md`（详细经验表）+ `docs/END_TO_END_OPTIMIZATION_2026-09-28.md`（端到端优化方案）