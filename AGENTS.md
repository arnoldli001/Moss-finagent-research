# Moss-FinAgent-Research 项目规则

## 项目定位
基于多Agent协作的AI辅助投研分析系统。面向二级市场提供投资分析与决策。

## 环境配置
- 本地运行Ollama（模型以 configs/models.yaml 为准：qwen2.5:1.5b-instruct-q4_K_M、qwen3:8b-q4_K_M）
- 存储默认SQLite零依赖；PostgreSQL/Redis为可选（DATA_BACKEND=postgres / REDIS_CACHE_ENABLED=true）
- 本地行情：**2026-09-23 起 QMT 排在所有链路的「最后一位」且默认关闭**。原因：本机
  QMT 终端失去行情权限、不再运行（`127.0.0.1:58610` 拒连），且**短期内无法恢复** ——
  放在任何位置之前都只会贡献一次必然失败的 xtquant 连接等待（实测 4~5s）。
  - 现行日线链：AkShare → 腾讯 → Tushare → baostock → 本地CSV → **[QMT 开关]（最末）**
  - 现行分钟/分时/快照链：腾讯 → 新浪 → 东财 → **[QMT 开关]（最末）**
  - ⚠️ **顺序与开关表达同一个意图**，两处都要改对：`configs/intraday.yaml` 的
    `data.intraday_sources` 把 `qmt` 写在**最后**、`data.qmt_enabled: false`。
    因为 `health.rank` 在样本 <3 次时**照抄配置顺序**当先验，所以"配置顺序 =
    默认顺序" —— 只改开关不改顺序，QMT 仍会顶到链首。
  - 将来权限恢复：`QMT_ENABLED=1` + `data.qmt_enabled: true` 即可在链尾兜底
  - 全市场全量下载：`scripts/download_market_data.py`（**取代** `scripts/download_qmt_data.py`，
    落盘到项目自己的 `data/quant/prices*/`，不再依赖 QMT 私有目录）
  - 东财可用性依赖 `MOSS_EM_DIRECT`（TLS SNI 阻断规避，可写 `.env`）；
    但该阻断会漂移，**东财只能当链尾备用**，见 `src/core/eastmoney_direct.py`
  - ⚠️ 竞价链的 `DataConfig.fallback` 已从 `"qmt"` 改为 `"none"`：竞价逐秒序列
    **没有免费替代源**（QMT tick 需权限、eltdx 只有盘中实时、Tushare 无竞价过程），
    如实登记"当前无备源"，不要留一个永远失败的 `"qmt"`
- 核心推理调用DeepSeek-V4-Flash API（DEEPSEEK_API_KEY 经环境变量注入）

## 架构规范
- 采用LangGraph StateGraph搭建Supervisor调度架构
- 18个专业Agent分5层：数据层(A01-A04)、信息层(A05-A07)、分析层(A08-A12)、行业层(A13-A16)、决策层(A17)、审计层(A18)
- Agent间通过标准JSON消息通信，基于MCP协议调用工具
- Demo阶段采用单进程分层架构（src layout），Agent以进程内模块注册到Supervisor，接口无状态；保留演进为独立FastAPI微服务的路径

## 编码规范
- Python 3.10+，使用类型注解
- 所有Agent必须实现统一的BaseAgent接口
- 所有数据访问必须通过统一数据层，禁止直接连接数据库
- 禁止硬编码敏感信息（API Key、数据库密码）
- 所有分析结论必须附带数据溯源标签和推理路径记录

## 数据溯源规范
- 每个数据点必须包含source_url、publish_time、fetch_time、raw_content_hash
- 数据处理链路必须记录每一步的Agent ID和操作类型
- LLM推理必须记录prompt_hash和token数
- 最终结论必须引用所有使用的数据ID

## 安全合规规范
- 所有输出附带免责声明
- 多租户数据通过RLS策略自动隔离
- 审计日志独立存储，不可篡改
- 敏感数据操作日志保存不低于3年

## Trae使用规范
- 使用Spec模式进行系统级开发
- 使用Task工具并行派发多个Agent开发任务
- 每个Agent独立开发、独立测试、独立部署

## 前端/接口改动规范
- 改 `web/src`（样式或组件）或配套接口前，**先读** `@.trae/skills/frontend-change-guardrails/SKILL.md`
  —— 它是从 2026-09-26~27 的真实报障里提炼的护栏：顶栏冻结的 16px 渐隐带、
  flex 挤压把一级页签挤折行、视图键三处枚举、删重复提示的正确姿势，
  以及"单请求耗时表会骗人，必须按并发与多宽度验收"的纪律。

## 性能硬约束（改任何会出现在首屏/交互路径上的代码前先读）
- **禁止新增串行往返。** 任何"必须等上一个请求回来才发得出去"的请求都算缺陷。
  门控数据（权限 / 清单 / 配置）一律**并进上一趟响应**，或首帧读本地缓存。
  本项目的基准：这条链路上**一次冷请求的固定开销约 0.85~0.9 秒，且与体积几乎无关**
  （实测 15 KB 与 64 KB 的分块耗时接近）—— 所以要减少的是**次数**，不是字节。
- 首屏启动路径的**串行往返上限 = 1**。新增请求必须同步更新时延预算表
  （见 `@.trae/skills/e2e-latency-budget/SKILL.md` §4.1）。
- 首屏入口 JS **≤ 120 KB(gzip)**。新面板**必须**代码分割，**并且**把它的分块加进预热列表
  —— 只分割不预热会让"首次点击"变慢，体感反而更差（这两件事是一对）。
- 面板数据**必须**复用既有的 cache / prefetch / keepalive 机制，**不得自造一套**。
  预取的续期间隔必须**小于**缓存 TTL；页面隐藏时跳过；失败静默。
- 改动完成后**必须**打真实产物验收：`manage.py build` → `manage.py ship-frontend` → 打公网入口。
  **本地 dev 快不等于线上快**；本项目真实发生过"源码早已优化并构建，但对外实例跑的是
  6 小时前的旧构建"。
- 判据写成**次数与 KB**，不要写毫秒 —— 毫秒换条线路就不成立，次数与 KB 才可 review、可断言。

## 告警/通知硬约束（新建任何通知渠道必读）
- 三层闸门缺一不可：**事件级去重**（同一件事别重复）+ **渠道级速率/合并**（一天很多件事别淹人）
  + **接收者级静默**（该不该在这个时间打扰）。只有第一层就会以"用户抱怨邮件太多"的形式漏水。
- 闸门状态**必须落盘**（放进程内存里的冷却，重启即失效）。
- 阈值有**单一权威默认值**；`.env` 覆盖必须在文档里登记差异。
- suppressed 的原因必须是**人话 + 剩余时间**（"冷却中，还剩 12 分钟"），不是枚举值。
- 详见 `@.trae/skills/alert-noise-and-first-paint/SKILL.md`。

## 红灯纪律（容易违反，但代价最高）
- **不接受"已知的红色基线测试"。** 红灯只有三种合法归宿：修好 / 删除并说明为什么不再需要 /
  显式标记为预期差异**并链接到具体待办**。
- 本项目实测代价：9 条关于告警阈值的测试长期红灯，被当成背景噪音写进文档；
  它们其实是**配置漂移的哨兵**（测试断言 `alert_confidence_min=0.7`，而 `.env` 实际是 `0.6`），
  被无视两天后，同一个漂移以"客户抱怨邮件太多"的形式重新出现。
- 推论：**任何"既能在代码里写默认值、又能被环境变量覆盖"的阈值都必然漂移** ——
  所以要么把生效值纳入测试，要么在启动自检里对比并告警。

## Skills引用
@.trae/skills/ai-dev-rules/SKILL.md
@.trae/skills/dev-standards/SKILL.md
@.trae/skills/dev-standards/references/architecture-naming-spec.md
@.trae/skills/frontend-change-guardrails/SKILL.md
@.trae/skills/e2e-latency-budget/SKILL.md
@.trae/skills/alert-noise-and-first-paint/SKILL.md
