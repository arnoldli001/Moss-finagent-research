# 事件监控与自动告警系统 - 产品需求文档（PRD/ Spec）

## Overview

- **Summary**: 在现有多Agent投研系统中新增"事件监控与自动告警"子系统：定时爬取/下载政策事件、板块事件、个股事件与投资日历事件，经标准化、去重、LLM两阶段分析（分类抽取→风险/机会评分与受益个股）、阈值引擎判定后生成告警，落库并通过 WebSocket 实时推送前端弹窗，同时可选经 SMTP 邮件发送到指定邮箱。
- **Purpose**: 参考金融界事件自动报警功能，让用户在不主动发起提问的情况下，自动获知当日重大政策/板块/个股事件中的风险与投资机会；每日盘后定时扫描一次（支持手动立即扫描）。
- **Target Users**: 投研工作台使用者（单租户演示 `tenant_001`，接口预留多租户字段）。

## Goals

- G1: 事件自动采集：政策/板块/个股/日历四类事件，主备多源冗余，单源失败降级不阻断。
- G2: 事件标准化为统一 JSON，携带溯源字段（source_url/publish_time/source_name）。
- G3: 跨源去重与告警冷却，同一事件不重复刷屏。
- G4: 复用统一 LLMGateway 的两阶段分析：轻量/本地模型做分类与实体抽取，推理模型做风险分/机会分/置信度/受益受损个股与影响路径。
- G5: 确定性阈值引擎按配置阈值与置信度门槛生成 high/medium/low 告警；低于阈值的事件仅入库不推送。
- G6: events/alerts 两张业务表，经统一仓储端口访问（禁止分析层直连数据库），幂等写入、active/read/expired 状态机。
- G7: 每工作日 17:30 自动扫描一次（cron 在注册表声明、可改），并支持前端/API 手动触发；运行记录复用 RunLog 与连续失败暂停机制。
- G8: FastAPI WebSocket `/ws/alerts` 实时推送；前端新 Tab"事件告警"：未读红点、Toast 弹窗（风险红/机会金）、高风险声音提示、告警列表筛选、详情（评分/置信度/受益风险个股/事件时间/原文链接/影响路径）、一键已读。
- G9: 邮件通道：QQ 邮箱 SMTP SSL 发送到 `1027312283@qq.com`，授权码仅经环境变量注入；未配置时自动降级并在前端标注"邮件通道未配置"，不影响站内告警。
- G10: 手工事件导入 API（JSON），作为数据源不可达时的兜底与演示手段。
- G11: 合规：每条告警/界面均附"仅供参考，不构成投资建议"；数据源失败、邮件未配置等缺口诚实标注，禁止把缺口解读为多空。

## Non-Goals

- N1: 微博/雪球/股吧等社交媒体爬取（反爬与合规风险，二期评估）。
- N2: 国务院/发改委等政府站点的定向 HTML 爬虫（v1 政策事件由财经快讯中的政策类新闻覆盖；二期按站点适配）。
- N3: 不新增注册 A20+ 编号 Agent，不改动现有18+1 Agent 研究图主链路；告警分析作为 domain 独立服务复用 LLMGateway。
- N4: PostgreSQL 版事件仓储适配（v1 仅 SQLite，即系统默认后端；DATA_BACKEND=postgres 时事件表不可用属已知限制，在文档/健康检查中标注）。
- N5: 用户自定义阈值 UI、自选股/自选行业个性化订阅（v1 全局阈值；配置文件可调）。
- N6: 告警短信/微信/Server酱/钉钉等邮件以外的外发渠道。
- N7: 多用户认证与权限体系（沿用现有单租户演示形态）。

## Background & Context

- 现有调度：`src/scheduler/registry.py` 为 cron 单一事实源，`jobs.py::execute_job` 按 `spec.kind` 分发，进程内 CronScheduler 驱动，`RunLog` 记录 success/partial/failed，连续失败3次自动暂停；手动触发走 `POST /api/v1/scheduler/jobs/{name}/run`。
- 现有新闻能力：`AkshareNewsFetcher.fetch_topic_news`（东财 `stock_info_global_em` 全球快讯关键词过滤）；本机 akshare 1.18.94 另含 `stock_info_global_cls/ths/sina/futu`（财联社/同花顺/新浪/富途，可作主备）、`news_economic_baidu`（宏观经济数据日历）、`news_trade_notify_suspend_baidu`（停复牌）、`news_trade_notify_dividend_baidu`（分红派息）、`news_report_time_baidu`（财报披露时间）、`stock_notice_report`（东财公告）。
- 东财 push2/clist 系接口对本机间歇性阻断；因此采集必须多源主备 + 单源失败不阻断（与既有连接器约定一致）。
- LLM 全部经 `LLMGateway.complete(task_tier, system, prompt, ...)`，tier 路由：light/medium 走本地 Ollama（qwen2.5:1.5b / qwen3:8b），reasoning 走 DeepSeek 主、本地兜底；自动审计 prompt_hash/token/降级链。
- 前端 React+TS+Vite（web/），现有4个 Tab，轮询风格；vite 仅代理 `/api`，需新增 `/ws` 的 WebSocket 代理。
- 经验教训（历史项目）：告警系统必须有"事件key+时间窗"冷却去重防刷屏；前后端字段契约必须先冻结（snake_case 单一包裹结构）；外部凭据缺失时交付表述为"已接入/待配置"。

## Functional Requirements

- **FR-1 事件采集器**：提供三类采集器——①全球财经快讯（东财主，同花顺/财联社/新浪备，关键词预筛政策/板块/个股事件）；②投资日历（百度宏观经济数据日历当日+次日、停复牌、分红派息、财报披露时间）；③手工导入（POST JSON）。每个采集器返回标准化前的原始条目，单源异常被捕获并记录为数据缺口，不中断其余源。
- **FR-2 事件标准化**：原始条目统一转换为 Event 对象：event_id（确定性哈希）、event_type(policy/sector/stock/calendar)、title、content(截断)、source_name、source_url、publish_time、entities{industries[],companies[],regions[]}、raw_data(JSON)、fetch_time、tenant_id。
- **FR-3 去重幂等**：同 `source_name + 规范化标题 + 发布日期` 的事件只入库一次；同一事件对同一告警类型在24小时冷却窗内只产生一条告警（alert_key 唯一约束 + 插入忽略）。
- **FR-4 规则预筛降噪**：快讯先经关键词规则白名单（政策类/行业类/公司类触发词）过滤，再进 LLM；单次扫描进入 LLM 的候选事件有上限（默认30条），超限按时间倒序截断并记录。
- **FR-5 LLM 阶段一（分类抽取）**：medium tier 批量输出每个候选事件的 event_type、情感极性、industries/companies 实体、摘要；无证据字段丢弃，实体经本地白名单规范化（复用 security_resolver 把公司名解析为6位代码，失败保留名称）。
- **FR-6 LLM 阶段二（风险机会评分）**：reasoning tier 批量输出 risk_score/opportunity_score(0-100)、confidence(0-1)、affected_stocks[{code,name,impact(positive/negative/mixed),reason}]、affected_industries[]、impact_path；JSON 解析失败/字段越界由本地代码兜底丢弃该事件评分（该事件留库不告警），不抛出中断批次。
- **FR-7 阈值引擎**：`risk high≥75 / medium≥60 / low≥45`；`opportunity high≥80 / medium≥65 / low≥50`；`confidence_min=0.70`；阈值集中存于配置（dataclass 常量+环境覆盖）；风险优先于机会定级；低于 low 或置信度不足不生成告警，事件仍入库。
- **FR-8 告警状态机**：Alert 含 alert_id、alert_key、event_id、alert_type(risk/opportunity)、alert_level(high/medium/low)、title/description、scores、confidence、affected_stocks/industries(JSON)、trigger_time、expire_time（默认7天）、status(active/read/expired)、tenant_id；支持标记已读、全部已读、查询时自动不物理删除过期项（列表默认隐藏 expired）。
- **FR-9 调度**：注册表新增 `event_alert_daily`（cron `30 17 * * 1-5`，kind=`event_alert_scan`），经 execute_job 执行并落 RunLog；部分源失败为 partial；全部事件源0条且有错误为 failed（沿用暂停策略）；另提供 POST 手动扫描（manual trigger，不等同定时）。
- **FR-10 实时推送**：新告警生成后经 AlertHub 向 `/ws/alerts` 连接的对应租户推送单条告警 JSON；离线客户端不补发（上线后自行拉列表）；连接断开自动清理。
- **FR-11 邮件通知**：仅 high 级告警（风险/机会）触发邮件（可配置 level）；SMTP SSL 465，发件账号/授权码/收件人全部来自环境变量；发送失败记录日志不影响扫描结果；未配置授权码时跳过并在健康/扫描结果中标记 disabled。
- **FR-12 REST API**：`GET /api/v1/alerts`（type/level/status/limit 过滤）、`GET /api/v1/alerts/{id}`、`POST /api/v1/alerts/{id}/read`、`POST /api/v1/alerts/read-all`、`GET /api/v1/events`（分页/类型过滤）、`POST /api/v1/events/import`、`POST /api/v1/alerts/scan`（手动触发，返回 run 概要）、`GET /api/v1/alerts/settings`（阈值+邮件通道配置状态）。
- **FR-13 前端**：新 Tab"事件告警"含——①全局铃铛未读 active 数红点（任何 Tab 可见）；②WS 收到告警弹 Toast（risk 红色 / opportunity 金色，展示标题+评分+个股，10秒自动消失，点击跳详情），high 级用 WebAudio 生成提示音（不依赖外部音频文件）；③告警列表（类型/级别/状态筛选、未读高亮、已读按钮）；④详情面板（评分、置信度、受益/风险个股表、影响路径、事件时间、原文链接、免责声明）；⑤"立即扫描"按钮与最近扫描状态；⑥邮件通道未配置时低调提示。
- **FR-14 溯源与合规**：告警详情可回溯到来源事件与 source_url；所有前端告警视图固定展示"事件告警由AI自动生成，仅供参考，不构成投资建议"；扫描结果含 per-source 成功/失败与 data_gaps 列表。

## Non-Functional Requirements

- **NFR-1 架构合规**：依赖方向 core←domain←infrastructure←api 单向；分析逻辑在 domain/alerts，LLM 只经 LLMGateway，数据库只经 EventRepository 端口；单文件≤300行、函数≤50行（ai-dev-rules）。
- **NFR-2 安全**：SMTP 授权码/账号仅环境变量注入，禁止硬编码与日志输出；SQL 全参数化；导入接口对单条 title/content 长度做限制。
- **NFR-3 可靠性**：所有外部调用（akshare/SMTP/LLM）try-except 包裹，单事件/单源失败不阻断批次；全程异步（阻塞 akshare/SMTP 经 to_thread）；扫描服务与研究图互不影响。
- **NFR-4 成本控制**：规则预筛 + 候选上限30 + 两阶段批量 prompt；单次扫描 LLM 调用 ≤2 次（每阶段1次批量调用）；LLM 走既有语义缓存与 token 预算。
- **NFR-5 可测**：分析器/阈值/去重/标准化为纯函数或可注入假 gateway，单测覆盖阈值边界、去重冷却、JSON 越界兜底；API 用 FastAPI TestClient（含 WS）；单测全绿且 ruff 通过；新增纯函数单测覆盖率≥80%。
- **NFR-6 性能**：手动扫描 API 必须在2秒内返回（后台执行，经 RunLog/扫描记录轮询进度）；列表接口默认 limit≤100 且有索引。
- **NFR-7 幂等**：扫描作业重复执行（Beat+手动重叠）由作业锁拦截；事件与告警均确定性主键，重复执行不产生重复通知邮件（告警已存在时跳过邮件）。

## Constraints

- **Technical**: Python 3.10+/SQLite/React18+Vite；不引入新重依赖（SMTP 用标准库 smtplib；WS 用 FastAPI 内置；不引入 APScheduler/Celery Beat 之外的调度器）。
- **Business**: 输出附免责声明；北向停披等既有数据缺口约定同样适用于事件解读。
- **Dependencies**: akshare 已安装；Ollama 本地模型可选（不可用时 reasoning/medium 经 DeepSeek 云端，需 DEEPSEEK_API_KEY）；QQ SMTP 需用户后续提供授权码。

## Assumptions

- 默认每工作日盘后1次（17:30）满足"调度周期可选每天一次"；后续可通过改注册表 cron 调整为每日/每小时（FR-9 已留手动触发）。
- 用户将在功能交付后自行把 QQ 邮箱 SMTP 授权码写入 `.env`（ALERT_SMTP_AUTH_CODE）；在此之前邮件通道为"已接入、待配置"状态。
- 东财源可能间歇性失败，主备源至少一个成功即视为采集成功（partial）。
- 事件标题中的上市公司名由 security_resolver 尽力解析代码，无法解析时仅展示名称，不阻塞告警。

## Acceptance Criteria

### AC-1: 事件多源采集与降级
- **Type**: `rule`
- **Given**: akshare 东财快讯接口可用/不可用两种情形
- **When**: 扫描服务执行事件采集
- **Then**: 主源失败时自动尝试备源；任一源成功则返回标准化前条目且 per-source 记录成败；全部失败时返回空列表+错误明细且不抛异常
- **Pass Condition**: 单测模拟主源抛异常，采集器返回备源数据且 errors 含主源失败记录；两源皆失败时返回 ([], [错误])
- **Evidence**: tests/unit 新增 event collectors 测试结果

### AC-2: 事件标准化字段完整
- **Type**: `rule`
- **Given**: 各源一条原始快讯/日历条目
- **When**: 调用标准化函数
- **Then**: 输出 Event 含 event_id/type/title/source_name/source_url/publish_time/fetch_time/tenant_id，content 有长度上限
- **Pass Condition**: 纯函数单测断言全部必需字段存在且 event_id 对相同输入确定性一致
- **Evidence**: 单测测试名与断言

### AC-3: 跨源去重与告警冷却
- **Type**: `rule`
- **Given**: 两条同源同标题同日期事件，以及同事件24小时内重复扫描两次
- **When**: 入库/生成告警
- **Then**: 事件只入库一条；同 alert_key 告警第二次被唯一约束/冷却逻辑忽略，不重复推送/发邮件
- **Pass Condition**: 仓储 upsert 计数第二次 inserted=0；阈值引擎/服务层冷却单测通过
- **Evidence**: test_event_repository / test_alert_dedup

### AC-4: LLM分析两阶段与兜底
- **Type**: `rule`
- **Given**: 注入 FakeLLMGateway（阶段一返回分类JSON、阶段二返回评分JSON；另测非法JSON）
- **When**: 分析器处理候选事件
- **Then**: 合法输出产出带风险/机会分与affected_stocks的分析结果；非法JSON时该事件不评分且不中断其余事件；每次扫描对gateway调用≤2次
- **Pass Condition**: 单测覆盖 正常/非法JSON/空候选 三情形
- **Evidence**: tests/unit/test_event_analyzer.py

### AC-5: 阈值定级边界
- **Type**: `rule`
- **Given**: 评分组合：risk=75/conf=0.7；risk=59/conf=0.7；opp=80/conf=0.7；opp=64/conf=0.7；risk=80/conf=0.69；risk=44/conf=0.9
- **When**: AlertEngine.evaluate
- **Then**: 依次得到 risk-high / 无 / opportunity-high / 无 / 无(置信度不足) / 无(低于low)
- **Pass Condition**: 参数化单测6条全过
- **Evidence**: tests/unit/test_alert_thresholds.py

### AC-6: 告警/事件仓储
- **Type**: `rule`
- **Given**: 临时SQLite库
- **When**: 写入事件与告警并按条件查询/标记已读
- **Then**: 幂等写入、列表过滤(type/level/status)、未读计数、read状态流转、expire_time过滤均正确
- **Pass Condition**: 仓储CRUD与幂等单测通过
- **Evidence**: tests/unit/test_event_repository.py

### AC-7: 定时作业接线与运行记录
- **Type**: `rule`
- **Given**: CronScheduler 与注册表
- **When**: execute_job("event_alert_daily") 以 manual 触发
- **Then**: 调用扫描服务，RunLog 落 success/partial/failed 记录；同作业并发被锁拦截；JOB_REGISTRY 中存在该作业且 cron 为 `30 17 * * 1-5`
- **Pass Condition**: 集成测试（Fake扫描服务）断言记录状态；注册表断言
- **Evidence**: tests/integration 或 tests/unit scheduler 测试

### AC-8: WebSocket实时推送
- **Type**: `rule`
- **Given**: TestClient 建立 /ws/alerts 连接
- **When**: 扫描服务产生一条新告警
- **Then**: 连接在2秒内收到该告警JSON且字段与REST详情一致（snake_case包裹）
- **Pass Condition**: websocket 测试收到消息并校验 alert_id/alert_type/alert_level/affected_stocks/disclaimer
- **Evidence**: tests/integration/test_alert_api.py

### AC-9: REST契约
- **Type**: `rule`
- **Given**: 预置事件与告警的测试客户端
- **When**: 调用 FR-12 全部端点
- **Then**: 过滤/详情/已读/全部已读/导入/手动扫描/设置 均返回正确状态码与结构；越权/不存在id返回404
- **Pass Condition**: 每个端点至少1条单测；响应字段全部 snake_case
- **Evidence**: tests/integration/test_alert_api.py

### AC-10: 邮件通道降级
- **Type**: `rule`
- **Given**: ①未配置ALERT_SMTP_AUTH_CODE ②配置但SMTP抛错（注入假smtp）
- **When**: 产生high告警
- **Then**: ①跳过发送并返回 skipped/unconfigured 标记 ②捕获异常记录失败，扫描仍成功，不重复发送
- **Pass Condition**: 邮件通知器单测两种情形通过
- **Evidence**: tests/unit/test_email_notifier.py

### AC-11: 前端告警中心体验
- **Type**: `rubric`
- **Dimension**: 告警中心可用性与信息完整度
- **Scale**: 1-5
- **Anchors**: 1=无界面或看不到告警；3=有列表但缺Toast/详情/筛选其一；5=铃铛红点+实时Toast(风险/机会配色)+声音+列表筛选+详情(评分/个股/原文链接/影响路径/免责声明)+手动扫描全部可用且构建通过
- **Pass Threshold**: >= 4
- **Evidence**: web 构建（npm run build）成功 + 浏览器手动验证截图/检查记录

### AC-12: 端到端闭环
- **Type**: `rubric`
- **Dimension**: 手工导入事件→分析→告警→推送/入库 的闭环质量
- **Scale**: 1-5
- **Anchors**: 1=链路中断；3=能走通但依赖真实LLM或有重复告警；5=不依赖外网（导入事件+Fake/真实LLM均可）完成扫描，产生至多一条告警，REST可查、WS可收、邮件缺凭据时诚实降级，全程有溯源与免责声明
- **Pass Threshold**: >= 4
- **Evidence**: E2E/集成测试 + 手动 API 调用记录

### AC-13: 合规与溯源
- **Type**: `rule`
- **Given**: 任一告警与对应事件
- **When**: 检查存储与API输出
- **Then**: 告警可经 event_id 找到含 source_name/source_url/publish_time 的事件；告警JSON与前端视图均带免责声明；数据源失败在扫描结果 data_gaps 中可见
- **Pass Condition**: 单测断言溯源链与 disclaimer 字段非空
- **Evidence**: 仓储/API测试

## Open Questions

- [ ] QQ邮箱SMTP授权码待用户提供后写入 `.env`（交付前邮件通道状态为"已接入、待配置"）。
- [ ] PG 仓储适配（N4）是否需要在本期补齐——默认不做。
- [ ] 东财公告（stock_notice_report）个股公告源是否纳入本期：v1 作为采集器可选增强（medium优先级），若接口阻断则自动降级，不阻塞核心验收。
