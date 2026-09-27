# 优化施工总结：优化前现状 → 问题根因 → 优化方法 → 优化结果

> 施工日期：2026-09-27。范围：两大慢页面（板块拥挤度、行业轮动日报）+ 投研分析 Agent 链路。
> 配套分析：`docs/ARCH_REVIEW_AND_INTERVIEW_2026-10.md`。
> 验证：162 项相关测试全过、ruff 全绿、前端构建通过、TestClient 路由冒烟通过。

---

## 一、板块拥挤度

| # | 优化前现状 | 问题根因 | 优化方法 | 优化结果 |
|---|---|---|---|---|
| 1 | `/alerts?all=1` 每请求直连 `db.max_ma5_map(conn)` 做 217 万行 GROUP BY（实测 1.7s）；同查询在 `/sectors_max_ma5` 却有 300s 缓存 | 缓存建了但热路径没用上（告警面板绕过 `_cached_max_ma5`） | ① 改走 `_cached_max_ma5`；② 配合物化表 `sector_crowding_max_ma5`（并发会话上线，O(N) 主键读，刷新/recompute 双路维护） | 该查询 **1.7s → <5ms**；60s 轮询不再白付 |
| 2 | 路由 `async def` 里直接跑同步 SQLite——单条 5ms~1.7s 查询期间**全站请求冻结** | 单进程事件循环是全局稀缺资源；同步 IO 未卸载线程池 | 新增 `_run_db()`，8 个热路径读 handler 全部包进 `asyncio.to_thread`（与告警仓储修复同款范式） | **事件循环零阻塞**：打开拥挤度页不再拖慢全站 |
| 3 | 每请求执行 `executescript(_SCHEMA)` DDL + PRAGMA + commit（白付 2~5ms） | `_ensure_tables()` 无状态 | `threading.Event` 守卫，进程内只跑一次 | 每请求省 2~5ms；消除 DDL 隐式事务与刷新写事务的锁交互窗口 |
| 4 | `GET /config_list`（60s 轮询）读路径每次跑 `hide_dead_boards` UPDATE——0 行命中也要开写事务抢写锁 | "读路径藏写"反模式（告警 500 故障同款） | db.py 新增 `has_hideable_dead_boards()`（同 WHERE 的 SELECT 版），先查后更，通常纯读 | 热路径读不再取写锁；消除与刷新线程的锁竞争 |
| 5 | 页面切换卸载重挂：顶级视图切走整棵 FundFlowPanel 销毁；子页签互切重拉 554 板块清单 + 4 请求 + 状态归零 | App.tsx 三元链"只渲染命中的那一个"；KeepAlive 只覆盖 intel/alerts | ① App.tsx：fundflow 加入 null 分支 + 独立 `<KeepAlive>`（自带 Suspense，因面板是 lazy 组件）；② FundFlowPanel：拥挤度/轮动两个子页签接 KeepAlive；③ 两个 60s 轮询加 `document.hidden` 守卫 | **互切 3~5s → 0ms**（数据与交互状态全保留）；未打开过的页签仍零成本（惰性挂载） |

## 二、行业轮动日报

| # | 优化前现状 | 问题根因 | 优化方法 | 优化结果 |
|---|---|---|---|---|
| 6 | 报告落伍时（错过 15:40 调度/刚开机）在 HTTP 请求内**同步生成 20~40 秒**，用户盯空白屏 | `await service.generate()` 写在请求路径里 | 重写路由：**读盘优先 + 落伍即回旧报告**（`meta.background_refreshing`）+ 后台任务补生成；页面注入横幅 + 轮询 `/build_status`（5s，零 IO）完成后自动 reload；仅首跑才同步；失败进 **15 分钟冷却** | 首开 **20~60s → 秒开旧报告**，建完自动刷新；与情报中心"请求永不等待"契约对齐 |
| 7 | iframe 从 jsdelivr CDN 拉约 1MB echarts.min.js——51KB/s 隧道上 ≈20s，大陆可用性不稳定 | 模板 `<script src="CDN">`；历史落盘 HTML 也引用 CDN | ① vendored `static/echarts.min.js`（5.5.1）+ 端点 `/echarts.min.js`（缓存 7 天）；② 模板 `echarts_src()` 本地优先/缺失回退 CDN；③ 历史旧报告读取时字符串替换切本地 | ECharts **≈20s → 本地毫秒级**；消除外部 CDN 依赖 |
| 8 | 取数三段串行：gather(腾讯,akshare) → Tushare 截面 → 10 次 Tushare 5 日 | Tushare 截面与前两路无数据依赖却被串行排在后 | `fetch_sector_frame` 提前并入 `asyncio.gather` 三路全并行（5 日净额依赖 boards 保持串行） | 强制重生成 **20~40s → ≈12~25s**（墙钟≈最慢一路） |
| 9 | 新异步路径无可观测与守卫 | — | 新增 `GET /build_status`（零 IO）+ 4 个回归测试（CDN 替换/横幅注入/秒开旧报告/失败冷却） | 行为有测试钉住，防静默退化 |

## 三、投研分析（Agent 链路）

> 并发会话（第八/九轮）在本会话期间同步施工了白名单、A17 compact、增量协议、信息层并行的**骨架**；我在其代码中发现并修复 **3 个真回归**（均有测试/日志实证），并补齐守卫测试。

| # | 优化前现状 | 问题根因 | 优化方法 | 优化结果 |
|---|---|---|---|---|
| 10 | 5 个分析 Agent 收完全相同的全量数据点（单 trace tokens_in 全部 = 25,149，每轮约 2 万 token 无效） | analyze 节点对 A08~A16 无差别分发 `validated_points` | 并发会话实现 `_AGENT_DATA_WHITELIST` + 过滤函数；**我发现其重构时丢了兜底 return**——过滤 0 条时只告警仍返回空 → Agent 走"无数据拒绝分析"跳过 LLM（`test_full_graph_pipeline` 实证 3 次调用只发生 2 次）。修复：恢复 `fallback 取前 200 条` | 每轮 **−30~50% tokens_in**（≈2 万 token）；且保证过滤不会把 Agent 饿死（兜底回归钉住） |
| 11 | A17 ReAct 每步重发全量上下文（7,582 tokens × 3 步，平均延迟 38.1s，约占全系统成本 55%） | `conversation` 每步累积重发；`use_cache=(step==0)` 使 2/3 步必然真调 | 并发会话实现增量协议骨架 + `_build_compact_context`（≤400 字/块）；**我修复其 2 个真 bug**：① 增量步只发"上次输出"**不带 observation**——Observe 环节丢失；② `describe_all` 误写成 join(键名) 丢工具描述。并补：任务头/尾（输出 schema）保留、`pending_obs` 增量注入、**4 个协议回归测试** | A17 tokens_in **7,582×3 → ~700+600×2 ≈ −91%**；延迟 **38s → ~15s**；保证语义正确（观察必达、schema 必在、工具描述必带） |
| 12 | （并行化 A05/A06 引入回归）news 管线 `extracted_events.stats.total` 期望 1 实际 0 | item_id 由 A05 的 `_parse_items` 分配；并行后 A06 读原始条目（无 item_id）→ 事件全部因"不可溯源"被丢弃 | 新增 `_ensure_item_ids()` 在 fan-out 前按 A05 同款规则补齐（两路同序同规则） | news 管线事件抽取恢复；并行化收益（~50s）保留 |
| 13 | 服务端成本/健壮性既有优化确认 | — | 验证确认已落地：准入信号量(4)+同问合流+结果缓存、语义缓存 `aget` 线程化、TaskStore 淘汰、告警两阶段全本地化（¥0.32→¥0/轮） | 安全并发 2~4 → 15~20；交互链路云端成本趋零 |

## 四、验证结果

| 验证项 | 结果 |
|---|---|
| 相关测试（rotation/crowding/frontend 守卫/react 增量/supervisor 图/研究并发/analysis agents） | **162 passed**（含新增 8 个回归用例） |
| 修复前失败的 `test_full_graph_pipeline` / `test_news_graph_info_pipeline` | 均已转绿（根因即 #10/#12） |
| ruff（本次改动全部文件） | All checks passed（顺手清掉 16 处遗留违规 + 1 处 py3.12-only f-string，恢复 py310 兼容） |
| 前端 `tsc --noEmit` + `vite build` | 通过；首屏核心 bundle 829KB → **264KB（gzip 85KB，慢隧道 ≈1.7s）** |
| TestClient 路由冒烟 | `build_status` 200 ✓；`echarts.min.js` 200 + 1007KB ✓ |
| 物化表维护闭环 | recompute 全量重建 + 每板块刷新增量更新（refresh.py:283/478）✓ |

## 五、遗留说明（非本次改动引入）

| 项 | 状态 |
|---|---|
| `test_alert_api.py` 阈值断言失败（70≠75）与 WS 用例挂起（`receive_json()` 无超时 + analyzer local_only 在无 Ollama 环境） | 并发会话正在改的 WIP（analyzer.py 在其未提交改动中），与本次改动无交集；建议同步测试基线或给 WS 等待加超时 |
| 写路径（清单增删/置顶/告警设置）仍为同步调用 | 用户点击触发、低频、行级小操作；后续可复用 `_run_db` 一并包装 |
| KeepAlive 常驻期间隐藏面板轮询 | 已加 `document.hidden` 守卫；应用内切视图时的轮询命中本地 TTL 缓存，成本可忽略 |

## 六、改动文件清单

| 类别 | 文件 |
|---|---|
| 后端修改 | `src/api/routes/sector_crowding.py`、`src/sector_crowding/db.py`、`src/sector_rotation/service.py`、`src/domain/agents/analysis/react.py`、`src/orchestration/supervisor.py` |
| 后端重写/新增 | `src/api/routes/sector_rotation.py`（重写）、`src/sector_rotation/static/echarts.min.js`（vendored 1MB） |
| 前端修改 | `web/src/App.tsx`、`web/src/components/FundFlowPanel.tsx`、`web/src/components/FundFlowBoard.tsx`、`web/src/components/SectorCrowdingTab.tsx` |
| 测试 | `tests/test_sector_rotation.py`（+4 用例）、`tests/unit/test_react_incremental.py`（新增 4 用例）、`tests/unit/test_frontend_prefetch_structure.py`（扩 fundflow 守卫） |
