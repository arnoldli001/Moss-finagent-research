# 2026 年中国大陆便宜云端大模型选型调研（多 Agent 投研系统）

> **调研日期：2026-09-28**（所有价格均为当日抓取）
> **调研方法**：web_search + web_fetch 抓取官方定价文档；无法抓取到的明确标注"未找到官方来源"
> **纪律声明**：本文每个价格数字都附 URL。区分「官方标称」与「第三方实测」。存在矛盾的数字全部列出，不做单一裁定。

---

## 0. 全局前提与本次调研的关键发现

### 0.1 汇率口径（重要）

DeepSeek 与 MiniMax 官方**只公布美元价**，百炼的国际化页面也以美元计价。本文按 **1 USD ≈ 6.7 CNY** 换算（2026-09 人民币处于三年多高位，见 [香港经济日报 2026-09 报道](https://invest.hket.com/article/4190611/) 报道即期 6.7095）。**换算值仅供参考**，实际以账单为准。

### 0.2 三条最重要的结论（先看这个）

1. **A17 的 32.7s 有明确归因：不是排队，是「思考模式下默认 high effort + 3168 output tokens 逐字生成」的固有耗时。** DeepSeek 官方文档确认思考模式**默认打开且 effort 默认 high**（[官方文档](https://api-docs.deepseek.com/zh-cn/guides/thinking_mode)）。第三方实测显示 high 档首字延迟 >30s、单轮总耗时 >100s，而 none 档 <0.1s / ~11s（[CSDN 实测](https://deepseek.csdn.net/6a7f065010ee7a33f29afe52.html)）。
2. **有一个 0 元且明确支持结构化输出 + 128K 输出的模型：智谱 `glm-4.7-flash`。** 官方定价表标「免费」，模型页明确列出「结构化输出：支持 JSON 等结构化格式输出」「Function Calling」「思考模式」（[定价页](https://docs.bigmodel.cn/cn/guide/start/pricing) / [模型页](https://docs.bigmodel.cn/cn/guide/models/free/glm-4.7-flash)）。30B 级、200K 上下文。**这是本次调研中性价比最高的单项发现。**
3. **A17 换模型的收益主要在延迟而非成本。** 单次调用成本本来就只有约 0.03 元量级（见 §2.3 测算），远低于 20 元/日预算。真正的痛点是 32.7s。

---

## 1. 云端便宜模型对比表

### 1.1 DeepSeek（官方 api-docs.deepseek.com）

抓取日期 **2026-09-28**，来源：<https://api-docs.deepseek.com/quick_start/pricing>

**官方标称价（美元 / 百万 tokens）**。注意 DeepSeek 采用**峰谷定价**：高峰时段为 UTC 周一至周五 01:00–04:00 与 06:00–10:00（对应北京时间 09:00–12:00 与 14:00–18:00），其余时间（含周末与中国法定节假日全天）为**低谷价，价格是高峰的一半**。

| 模型 | 输入(缓存命中) 谷/峰 | 输入(缓存未命中) 谷/峰 | 输出 谷/峰 | 上下文 | JSON | FC | 官方页 |
|---|---|---|---|---|---|---|---|
| `deepseek-flash`（版本 DeepSeek-V4.1-Flash） | $0.003 / $0.006 | $0.15 / $0.30 | $0.60 / $1.20 | 1M | ✓ | ✓ | [pricing](https://api-docs.deepseek.com/quick_start/pricing) |
| `deepseek-v4-pro`（版本 DeepSeek-V4-Pro-0813） | $0.022 / $0.044 | $0.66 / $1.32 | $1.98 / $3.96 | 1M | ✓ | ✓ | 同上 |

**换算为元（×6.7，估算值）**：

| 模型 | 输入命中(谷) | 输入未命中(谷) | 输出(谷) | 输入未命中(峰) | 输出(峰) |
|---|---|---|---|---|---|
| `deepseek-flash` | ≈¥0.020 | ≈¥1.01 | ≈¥4.02 | ≈¥2.01 | ≈¥8.04 |
| `deepseek-v4-pro` | ≈¥0.147 | ≈¥4.42 | ≈¥13.27 | ≈¥8.84 | ≈¥26.53 |

**缓存折扣比例（实测于官方定价页）**：
- `deepseek-flash`：缓存命中 $0.003 vs 未命中 $0.15 → **1/50**（谷）；峰值 $0.006 vs $0.30 → 同为 **1/50**。
- `deepseek-v4-pro`：$0.022 vs $0.66 → **1/30**。

**其他关键规格（官方）**：
- 最大输出 **384K**；并发上限 `deepseek-flash` 2500、`deepseek-v4-pro` 500。
- 思考模式开关（OpenAI 格式）：`{"thinking": {"type": "enabled/disabled"}}`；强度控制 `{"reasoning_effort": "low/high/max"}`。
- **effort 映射表（官方）**：`minimal→low`、`low→low`、`medium→high`、`high→high`、`xhigh→high`、`max→max`。
- `deepseek-flash` 支持 Vision，`deepseek-v4-pro` **不支持** Vision。
- 旧模型名 `deepseek-v4-flash` / `deepseek-v4-flash-vision-exp` 已下线，请求由 V4.1-Flash 承接并按 Flash 价格计费。

**DeepSeek 新用户免费额度**：官方定价页与快速开始页**均未提及赠送额度**（抓取 2026-09-28）。→ **未找到官方来源**，不要假设有。

### 1.2 阿里云百炼 Qwen（官方 help.aliyun.com）

抓取日期 **2026-09-28**，来源：<https://help.aliyun.com/zh/model-studio/model-pricing>，免费额度规则见 <https://help.aliyun.com/zh/model-studio/new-free-quota>

**元 / 百万 tokens，华北2（北京），官方「原价」**（页面明示：仅展示原价，活动优惠需看控制台）：

| 模型 ID | 输入 | 输出 | 缓存命中输入 | 上下文 | 结构化输出 | FC | 免费额度 | 官方页 |
|---|---|---|---|---|---|---|---|---|
| `qwen3.8-max` | 12 | 36 | 未在 CN 页显式给出（另见 note） | 1M | 支持 | 支持 | 100万 Token | [model-pricing](https://help.aliyun.com/zh/model-studio/model-pricing) |
| `qwen3.7-max` | 12 | 36 | — | 1M | 支持 | 支持 | 100万 Token | 同上 |
| `qwen3.6-max-preview` | 9（≤128K）/ 15（>128K） | 54 / 90 | — | 256K | 支持 | 支持 | 100万 Token | 同上 |
| `qwen3-max` | 2.5（≤32K）/ 4（≤128K）/ 7（≤256K） | 10 / 16 / 28 | 阶梯 | 256K | 支持 | 支持 | 100万 Token | 同上 |
| `qwen-plus`（= qwen-plus-2025-12-01） | 0.8（≤128K）/ 2.4（≤256K）/ 4.8（≤1M） | 2 / 20 / 48 | 阶梯 | 1M | 支持 | 支持 | 100万 Token | 同上 |
| `qwen3.5-plus-2026-02-15` | 0.8 / 2 / 4 | 4.8 / 12 / 24 | 阶梯 | 1M | 支持 | 支持 | 100万 Token | 同上 |

**关键：百炼的缓存折扣（官方 note 原文）** —— 「显式缓存创建按标准输入单价的 **125%** 计费、命中按 **10%** 计费」。即**缓存命中 = 输入价 1/10**。来源同上页顶部 Note。

**`qwen3.8-flash` 的具体价格**：百炼 CN 定价页对我**抓取时被截断**（4372 行文档只取到前 53KB），因此下表数字来自**阿里云开发者社区文章**（2026-09-08，作者为实名注册用户，**属第三方而非官方定价页**），但与官方口径自洽，请标注为「第三方转述的官方价」：

| 项目 | 价格（元/百万 tokens） |
|---|---|
| 输入（标准） | **0.8** |
| 输入（缓存命中） | **0.1** |
| 输入（Batch File） | 0.4 |
| 显式缓存创建 | 1.25 |
| 显式缓存命中 | 0.1 |
| 输出（标准） | **2.7** |
| 输出（Batch File） | 1.35 |
| 最大输入 / 输出 | 991K / 128K；思考模式最大思维链 256K |
| 免费额度 | 100 万 Token（华北2北京，开通/发布起 90 天） |

来源：<https://developer.aliyun.com/article/1761526>（第三方文章，2026-09-08）。文中亦载：2026-08-27 起北京地域输入由 1.00→0.8、输出由 3.00→2.7。

**`qwen3.7-flash` 的官方价（美元）**：来源 <https://www.alibabacloud.com/help/ja/model-studio/qwen3-7-flash>（Alibaba Cloud 官方文档，最后更新 2026-08-26），中国（北京）阶梯：

| 输入档 | 输入 | 输出 | 输入(隐式缓存) | 输入(Batch File) | 输出(Batch File) |
|---|---|---|---|---|---|
| ≤32k | $0.028 | $0.11 | $0.006 | $0.014 | $0.055 |
| 32k–256k | $0.083 | $0.33 | $0.017 | $0.041 | $0.165 |
| 256k–1M | $0.165 | $0.66 | $0.033 | $0.083 | $0.33 |

→ 换算（×6.7）：≤32k 档 输入 **≈¥0.19** / 输出 **≈¥0.74**，隐式缓存命中 **≈¥0.04**。
**注意这是国际化站点以美元标价的 CN 北京价**，与 CN 站点人民币刊例价可能存在口径差异（见 §5 矛盾清单）。上下文 1M，最大输出 131072，RPM 30000 / TPM 5,000,000。

**百炼新人免费额度规则（官方）** —— 来源 <https://help.aliyun.com/zh/model-studio/new-free-quota>：
- **每个模型独立的免费额度，通常 100 万 Token**，不可跨模型合并。
- 有效期 **90 天**，自「开通百炼 / 模型发布 / 申请通过」较晚者起算。
- **仅华北2（北京）地域**有免费额度，其他地域无。
- 免费额度**输入输出共用**同一池子。
- 未实名认证用户免费额度用完后**无法继续调用**（强制「用完即停」）。
- **快照版本与最新版视为两个独立模型**（如 `qwen-max` 与 `qwen-max-2026-05-17` 各有额度）。
- OAuth 认证另有独立免费额度：**每天 2000 次调用**。
- 用完即停报错码：`AllocationQuota.FreeTierOnly`（HTTP 403）。

### 1.3 智谱 GLM（官方 docs.bigmodel.cn）

抓取日期 **2026-09-28**，来源：<https://docs.bigmodel.cn/cn/guide/start/pricing>

**元 / 百万 tokens**（官方，完整表）：

| 模型 | 上下文 | 输入 | 输出 | 缓存命中 | 免费? |
|---|---|---|---|---|---|
| **GLM-4.7-Flash** | 200K | **免费** | **免费** | 免费 | ✅ **完全免费** |
| **GLM-4-Flash-250414** | 128K | **免费** | **免费** | 不支持 | ✅ 完全免费 |
| **GLM-Z1-Flash** | 128K | **免费** | **免费** | 不支持 | ✅ 完全免费 |
| GLM-4.5-Flash | 128K | 免费 | 免费 | — | ✅ 完全免费 |
| GLM-4-FlashX-250414 | 128K | 0.1 | 0.1 | 0.05 | ❌ |
| GLM-Z1-FlashX | 128K | 0.1 | 0.1 | 不支持 | ❌ |
| GLM-4.7-FlashX | 200K | 0.5 | 3 | 0.1 | ❌ |
| GLM-4.5-Air | 128K | 0.8（≤32K）/ 1.2（32K–128K） | 2（输出<0.2K）/ **6**（输出≥0.2K）/ 8 | 0.16 / 0.24 | ❌ |
| GLM-Z1-Air | 128K | 0.5 | 0.5 | 不支持 | ❌ |
| GLM-4-Long | 1M | 1 | 1 | 0.5 | ❌ |
| GLM-4-Air-250414 | 128K | 0.5 | 0.5 | 0.25 | ❌ |
| GLM-4.7 | 200K | 2（≤32K,输出<0.2K）/ 3（≤32K,输出≥0.2K）/ 4（32K–200K） | 8 / 14 / 16 | 0.4 / 0.6 / 0.8 | ❌ |
| GLM-4.6（私有实例价） | 200K | 175 元/算力单元/天 | — | — | ❌ |
| GLM-5 | 200K | 4（≤32K）/ 6（≥32K） | 18 / 22 | 1 / 1.5 | ❌ |
| GLM-5-Turbo | 200K | 5 / 7 | 22 / 26 | 1.2 / 1.8 | ❌ |
| GLM-5.1 | 200K | 6 / 8 | 24 / 28 | 1.3 / 2 | ❌ |
| GLM-5.2 | 1M | 8 | 28 | 2 | ❌ |
| **GLM-5.3** | 1M | **8** | **28** | **2** | ❌ |
| **GLM-5.3-Flash** | 1M | **0.8** | **2.8** | **0.23** | ❌ |
| **GLM-5.3-FlashX** | 1M | **2** | **7** | **0.57** | ❌ |

**其他官方要点**：
- 缓存存储（元/百万 Tokens/小时）：当前**限时免费**。
- **Batch API = 标准价 5 折**。
- GLM-4.5-Air 的阶梯**按输出长度分档**这点很反直觉：同样 ≤32K 输入，输出 <0.2K 时输出价 2 元，输出 ≥0.2K 时跳到 **6 元**（3 倍）。→ 对本系统（A17 输出 3168 tokens）适用的是 **6 元**档，不是 2 元。
- **GLM-5.3 强制开启思考，不支持关闭**，`thinking.type` 仅支持 `enabled`，`reasoning_effort` 默认 **max**（可选 low/high/max）。迁移提示：若原来用 `disabled`，必须改成 `enabled` + `low`，否则请求失败。来源：<https://docs.bigmodel.cn/cn/guide/models/text/glm-5.3>
- **GLM-4.7-Flash 能力清单（官方模型页）**：30B 级、200K 上下文、**最大输出 128K**、思考模式 ✓、流式 ✓、**Function Calling ✓**、**结构化输出 ✓**（「支持 JSON 等结构化格式输出，便于系统集成」）、上下文缓存 ✓、MCP ✓；官方称在 SWE-bench Verified、τ²-Bench 上取得同尺寸开源 SOTA。来源：<https://docs.bigmodel.cn/cn/guide/models/free/glm-4.7-flash>
- GLM-4-9B / ChatGLM3-6B 已开放**免费商用授权**（需申请授权证书）。来源：定价页底部。

### 1.4 月之暗面 Kimi（官方 platform.kimi.com）

抓取日期 **2026-09-28**，来源：<https://platform.kimi.com/docs/pricing/chat>（`.md` 端点拿到完整表）

**元 / 百万 tokens**：

| 模型 | 缓存写入(TTL 5min) | 缓存写入(TTL 1h) | 输入(缓存命中) | 输入(未命中) | 输出 | 上下文 |
|---|---|---|---|---|---|---|
| `kimi-k3` | ¥20.00 | ¥40.00 | ¥2.00 | **¥20.00** | **¥100.00** | 1,048,576 |
| `kimi-k2.7-code` | — | — | ¥1.30 | ¥6.50 | ¥27.00 | 262,144 |
| `kimi-k2.7-code-highspeed` | — | — | ¥2.60 | ¥13.00 | ¥54.00 | 262,144 |
| `kimi-k2.6` | — | — | ¥1.10 | **¥6.50** | **¥27.00** | 262,144 |

**重要**：Kimi 官方定价页**只有 K3 与 K2 系列**。**`moonshot-v1-8k/32k/128k` 与 `kimi-k2.5` 已不在官方定价页上** —— 它们目前只能通过百炼等第三方平台调用（百炼三方模型表里可见 `kimi-k2.5`、`kimi-k2-thinking`、`Moonshot-Kimi-K2-Instruct`）。→ **Kimi 自营 API 未有「便宜档」**，最低是 k2.6 的 ¥6.5/¥27。**不含税**（官方明示价格不含适用税费）。

**Kimi 免费额度**：定价页与充值页**均未提及赠送额度**；官方明示「代金券不计入累计充值总额」。→ **未找到官方免费额度来源**。

### 1.5 MiniMax（官方 platform.minimax.io）

抓取日期 **2026-09-28**，来源：<https://platform.minimax.io/docs/guides/pricing-paygo>（`.md` 端点）

| 模型 | 输入 | 输出 | 缓存命中读取 | 缓存写入 |
|---|---|---|---|---|
| **MiniMax-M3**（≤512k 输入） | $0.30（原价$0.60，**永久5折**） | $1.20（原价$2.40） | $0.06 | — |
| MiniMax-M3（>512k 输入） | $0.60 | $2.40 | $0.12 | — |
| MiniMax-M2.7 | $0.30 | $1.20 | $0.06 | $0.375 |
| MiniMax-M2.7-highspeed | $0.60 | $2.40 | $0.06 | $0.375 |

→ 换算（×6.7）：M3 标准档 输入 **≈¥2.01** / 输出 **≈¥8.04**，缓存命中 **≈¥0.40**。
- Priority 服务层价格为标准 1.5×（`service_tier: "priority"`）。
- **abab 系列已不在价目表上**（现为 Legacy/已下线）。→ 用户问的「abab 系列」官方页查无此项，**未找到官方来源**。
- MiniMax **未提及** 新用户免费额度。
- 注意：这是 `.io`（国际化）站点，中国大陆站 `platform.minimaxi.com` 的价格**未在本次抓取中确认**，可能存在人民币刊例价差异。

### 1.6 硅基流动 SiliconFlow（官方 siliconflow.cn/pricing）

抓取日期 **2026-09-28**，来源：<https://siliconflow.cn/pricing>

**对话模型（元 / 百万 tokens）**，含**完全免费**档：

| 模型 | 输入 | 输出 | 缓存 | 免费? |
|---|---|---|---|---|
| **tencent/Hunyuan-MT-7B** | **免费** | **免费** | — | ✅ |
| **XingChenAGI/Xing4.0-29B**（中国电信） | **免费** | **免费** | — | ✅ |
| **PaddlePaddle/PaddleOCR-VL-1.5** | **免费** | **免费** | — | ✅ |
| `Qwen/Qwen3.5-35B-A3B` | 0.40（≤128k）/ 1.60 | 3.20 / 12.80 | — | ❌ |
| `Qwen/Qwen3.5-27B` | 0.60（≤128k）/ 1.80 | 4.80 / 14.40 | — | ❌ |
| `Qwen/Qwen3.5-122B-A10B` | 0.80（≤128k）/ 2.00 | 6.40 / 16.00 | — | ❌ |
| `zai-org/GLM-4.5-Air` | 1.00 | 6.00 | — | ❌ |
| `deepseek-ai/DeepSeek-V3.2` | 4.00 | 6.00 | 0.40 | ❌ |
| `deepseek-ai/DeepSeek-V4-Flash` | 3.00（0–2点/8–24点）/ **1.50**（2–8点） | 9.00 / 4.50 | 0.30 / 0.15 | ❌ |
| `deepseek-ai/DeepSeek-V4-Pro` | 12.00 | 24.00 | 1.00 | ❌ |
| `Qwen/Qwen3.8-27B` | 3.00 | 12.00 | — | ❌ |
| `zai-org/GLM-5.3` | 8.00 | 28.00 | 2.00 | ❌ |
| `moonshotai/Kimi-K2.7-Code` | 6.50 | 27.00 | 1.30 | ❌ |

**注意**：聚合平台的 DeepSeek-V4-Flash 报价 ¥3.00/¥9.00 约等于官方价（¥2.01/¥8.04 峰值）的 1.1–1.5 倍；DeepSeek-V4-Pro ¥12/¥24 约等于官方峰值价（¥8.84/¥26.53）的 0.9–1.4 倍 —— **聚合平台不总是更便宜，也可能更贵**。必须在选型时逐项比对。

### 1.7 字节豆包 Doubao / 火山方舟

- 官方定价页：<https://console.volcengine.com/ark/region:cn-beijing/docs/ark/model-pricing?lang=zh> 与 <https://docs.volcengine.com/docs/82379/1099320>、<https://docs.volcengine.com/docs/ark/bytedance-seed-1-8?lang=zh>
- **本次抓取结果：全部返回空白/仅标题。** 火山方舟文档站为 JS 渲染，web_fetch 无法取得价格表内容（尝试了 4 个 URL）。
- → **未找到可引用的豆包官方价格数字。不编造。**
- 可确认的旁证（非价格）：豆包大模型 1.8 有官方文档页存在（`docs.volcengine.com/docs/ark/bytedance-seed-1-8`）；火山方舟有「安心体验模式（free-tokens-only-mode）」文档页存在，说明**存在免费 token 模式机制**，但**具体额度未取到**。

### 1.8 百度文心 ERNIE / 千帆

- 官方页：<https://cloud.baidu.com/doc/qianfan-docs/s/Jm8r1826a>（价格）、<https://cloud.baidu.com/doc/qianfan/s/wmh4sv6ya>（模型服务计费）
- **本次抓取结果：页面正文为 JS 渲染，仅返回导航壳。** → **未取到 2026 年 ERNIE 现行价格。**
- **可确认的官方公告（有时间戳，需注意时效）**：<https://cloud.baidu.com/news/notice_c7a145b9-5c99-4870-939f-e09ba506aab3>（2024-05-21）宣布 **ERNIE-Speed-8K / ERNIE-Speed-128K / ERNIE-Speed-AppBuilder专用版 / ERNIE-Lite-8K / ERNIE-Lite-8K-0922 / ERNIE-Lite-128K / ERNIE-Tiny 共 7 款模型的预置服务免费开放**，所有完成实名认证的客户通过按量调用即可免费，官方称「计划**长期**开放」。
  - ⚠️ **这是一份 2024 年的公告，距今约 2 年 4 个月**。公告中明确「未来上线的新模型，还可以继续免费使用吗？A：以后续官网通知及展示为准。」→ **该免费政策在 2026 年是否仍有效，我无法从官方现行定价页证实。** 这类免费政策在 2024 年已被多轮收紧（同期 Google/OpenAI 均如此），**上线前必须在千帆控制台实测确认**。
  - 该公告同时说明：ERNIE-Speed/ERNIE-Lite/ERNIE-Tiny 的免费 TPM/RPM 配额「以控制台展示的额度为准」。

### 1.9 其他性价比突出的平台

| 平台/模型 | 价格 | 来源 | 备注 |
|---|---|---|---|
| 腾讯云智能体开发平台 DeepSeek V4 Flash | 曾**限时免费公测**，**2026-05-07 10:00 起转正式商用**按量计费 | [腾讯云公告](https://cloud.tencent.com/announce/detail/2278)（官方） | **免费期已结束**，2026-09-28 起需付费。具体单价需查腾讯云计费文档 |
| UCloud ModelVerse（GLM 类） | 输入<32K：4元/输出18元；32K–200K：6元/22元 | [UCloud 文档 PDF](https://docs.ucloud.cn/modelverse/mdToPdf/modelverse.pdf) | 第三方云厂商转售，价高于智谱官方（8/28 对比 4/18） |

### 1.10 免费 & 新用户额度汇总

| 类型 | 内容 | 官方来源 |
|---|---|---|
| **完全免费模型** | GLM-4.7-Flash（200K/128K输出/结构化输出✓）<br>GLM-4-Flash-250414（128K）<br>GLM-Z1-Flash（128K）<br>GLM-4.5-Flash（128K）<br>GLM-4V-Flash / GLM-4.6V-Flash / GLM-4.1V-Thinking-Flash<br>CogView-3-Flash / CogVideoX-Flash | [智谱定价页](https://docs.bigmodel.cn/cn/guide/start/pricing) |
| **完全免费模型（SiliconFlow）** | tencent/Hunyuan-MT-7B、XingChenAGI/Xing4.0-29B、PaddlePaddle/PaddleOCR-VL-1.5 | [SiliconFlow 价格页](https://siliconflow.cn/pricing) |
| **完全免费（百度，2024 公告）** | ERNIE-Speed-8K/128K、ERNIE-Lite-8K/128K、ERNIE-Tiny（预置服务） | [百度公告 2024-05-21](https://cloud.baidu.com/news/notice_c7a145b9-5c99-4870-939f-e09ba506aab3) ⚠️时效性存疑 |
| **新用户免费额度** | 百炼：每模型通常 100 万 Token，**独立不互通**，有效期 90 天，仅北京地域 | [百炼免费额度](https://help.aliyun.com/zh/model-studio/new-free-quota) |
| | 百炼 OAuth：每天 2000 次调用（独立体系） | 同上 |
| **未找到官方免费额度** | DeepSeek、Kimi、MiniMax | 官方定价页均未提及 |
| **曾有现已结束** | 腾讯云智能体平台 DeepSeek V4 Flash（2026-05-07 结束免费） | [腾讯公告](https://cloud.tencent.com/announce/detail/2278) |

---

## 2. A17「综合决策」任务胜任度评估

### 2.1 A17 的真实需求拆解

A17 要：综合 5~9 个上游结论 → **冲突仲裁** → **三档情景 + 证伪信号** → **仓位建议** → 回答开放式投资问题。翻译成模型能力要求：

| 能力维度 | 要求强度 | 为什么 |
|---|---|---|
| 中文长文综合（多源整合） | ★★★★★ | 5~9 份中文分析要在一个上下文里对齐口径 |
| 冲突消解 / 取舍判断 | ★★★★★ | 上游结论互相矛盾时必须选边并给理由 |
| 指令遵循（严格 JSON + 固定字段） | ★★★★★ | 三档情景 + 证伪信号是固定结构，字段缺一即下游解析失败 |
| 反事实推理（证伪信号） | ★★★★☆ | 需要「什么情况下该结论失效」的假设推理 |
| 数值/仓位推理 | ★★★☆☆ | 仓位建议不是精确计算，容错较高 |
| 长输出 | ★★★★☆ | 实测 3168 output tokens，需要有足够 max_output 空间 |

### 2.2 有哪些公开证据能支撑「便宜模型接近 v4-pro」

**能拿到的（证据等级：第三方榜单）**

1. **SuperCLUE 2026 年 3 月测评**（22 款国内外主流模型）：
   - 海外前三：Anthropic、Google、OpenAI。
   - **国内第一：字节豆包，71.53 分**，进入全球第一梯队，**与 GPT-5.4 总分差距仅 0.95 分**；豆包在**智能体任务规划维度跻身全球前五**。
   - 小米 MiMo-V2-Pro（闭源）表现突出；MiMo-V2-Flash 在代码生成有潜力。
   - 国产开源模型**包揽开源榜前三名**。
   - 来源：<https://www.c114.net.cn/ainews/71776.html>（2026-03-30，转载 SuperCLUE 结果，**第三方媒体转述，非 SuperCLUE 官网原文**）
   - ⚠️ 该报道**未给出 DeepSeek V4-Pro/Flash、Qwen3.8、GLM-5.3 的具体分数**，因此**无法据此直接判断「哪个便宜模型接近 v4-pro」**。

2. **AIMultiple LLM Latency Benchmark**（2026-08-12 更新，1320 次请求，11 个模型）—— 这是**延迟**榜而非能力榜，但对 Q4 极有价值：
   - 短答（Q&A）首字延迟：非推理模型 claude-opus-4-8 0.75s / gpt-5-2 0.8s / claude-haiku-4-5 0.96s；**推理模型组：mimo-v2-5 1.2s、minimax-m3 2.4s、`deepseek-v4-flash` 3.6s**、gemini-3-1-pro-preview 9s、hy3-preview 14.6s。
   - 长答（代码生成，输出上限 1024 tokens）：非推理组 5.5–12.7s；**推理组：gemini-3-1-pro 18.5s、hy3-preview 23.1s、`deepseek-v4-flash` 24.3s、minimax-m3 28.1s、mimo-v2-5 35s**。
   - **p90 长尾**：「推理模型的尾部是**成倍放大**而非整体平移」。MiniMax 在代码任务上中位 ~13s、**p90 ~42s**；混元 ~16s → **~46s**。
   - **max_tokens 与推理模型的冲突（与本系统踩过的坑完全一致）**：官方原文——「When thinking fills the whole budget, no tokens are left for the answer and the response comes back empty. This hit MiniMax hardest on coding, where the first pass completed fewer than half of its requests.」
   - 来源：<https://aimultiple.com/llm-latency-benchmark>

**拿不到的（必须如实标注）**

- **LMSYS Chatbot Arena 中文榜**：本次未取得 2026-09-28 的快照数据 → **未找到可引用数字**。
- **C-Eval / CMMLU 官方 leaderboard**：本次未取得 2026 年数据 → **未找到可引用数字**。
- **「便宜模型 vs 贵模型」在长文本综合任务上的公开对比（LongBench / ∞Bench / RULER / Artificial Analysis）**：本次未取得可引用分数 → **未找到公开证据**。
- **BFCL / JSONSchemaBench 等结构化输出成功率榜**：本次未取得 → **未找到公开证据**（这对本项目最关键，见 §5 不确定项）。
- **FinEval / CFBenchmark 等中文金融榜单**：本次未取得 → **未找到公开证据**。

### 2.3 单次调用成本测算（A17 口径：input 1504 / output 3168 tokens）

按官方价计算（DeepSeek/MiniMax/Qwen国际化站换算 ×6.7，估算）：

| 模型 | 单价（元/百万）输入/输出 | 单次成本 | 相对 v4-pro 官方价 | 备注 |
|---|---|---|---|---|
| **GLM-4.7-Flash** | 0 / 0 | **¥0.0000** | **0%** | 完全免费，200K/128K，结构化输出✓ |
| **GLM-4.5-Flash** | 0 / 0 | **¥0.0000** | 0% | 免费，128K |
| `glm-5.3-flash` | 0.8 / 2.8 | **¥0.0101** | 7.8% | 1M 上下文 |
| `qwen3.8-flash` | 0.8 / 2.7 | **¥0.0098** | 7.5% | 官方 CN 价（第三方转述） |
| `qwen3.7-flash` | ≈0.19 / ≈0.74 | **¥0.0026** | 2.0% | **最便宜的付费档**（需确认 CN 人民币刊例口径） |
| `deepseek-flash`（谷，全未命中） | ≈1.01 / ≈4.02 | **¥0.0142** | 11% | 1M，JSON✓ |
| `deepseek-flash`（谷，input 全命中） | ≈0.020 / ≈4.02 | **¥0.0128** | 10% | 缓存对短 input 影响很小 |
| `deepseek-v4-pro`（谷） | ≈4.42 / ≈13.27 | **¥0.0487** | 38% | 当前 A17 primary（非高峰） |
| `deepseek-v4-pro`（峰） | ≈8.84 / ≈26.53 | **¥0.0973** | 75% | 当前 A17 primary（高峰） |
| `glm-4.5-air` | 0.8 / **6**（输出≥0.2K 档） | **¥0.0202** | 16% | 注意输出档位陷阱 |
| `glm-5.3` | 8 / 28 | **¥0.1007** | 78% | 强制思考，默认 max effort |
| `MiniMax-M2.7` | ≈2.01 / ≈8.04 | **¥0.0285** | 22% | |
| `MiniMax-M3` | ≈2.01 / ≈8.04 | **¥0.0285** | 22% | 192K 上下文 |
| `kimi-k2.6` | 6.5 / 27 | **¥0.0953** | 74% | |
| `kimi-k3` | 20 / 100 | **¥0.3469** | **268%** | **比 v4-pro 贵 2.7 倍，不适合** |

**关键结论**：A17 单次调用的成本区间是 **¥0.0000 ~ ¥0.35**。用户报告的「单次分析成本中位 0.05 / p90 0.22 元」与 v4-pro 单次 ¥0.049–0.097 完全吻合 —— 说明**成本从来不是本系统的瓶颈，延迟才是**。换成 glm-5.3-flash 或 qwen3.8-flash 可把 A17 成本降到 1/10，但绝对节省额只有 ¥0.04/次，**不值得为省这点钱牺牲仲裁质量**；真正值得换的理由是**延迟**。

### 2.4 分层替换建议（我的判断）

| 层 | 当前 | 建议 | 依据 |
|---|---|---|---|
| `light`（分类/抽取/情感） | 本地 qwen2.5:1.5b | **可换 `glm-4.7-flash`（免费）** 或保持本地 | 免费且能力远超 1.5B；但引入网络往返 |
| `medium`（新闻去伪/事件抽取） | 本地 qwen3:8b + 付费 fallback | **`glm-4.7-flash`（免费）或 `qwen3.8-flash`（¥0.8/¥2.7）** | 结构化输出是硬需求，两者官方均标支持 |
| `reasoning`（宏观/中观/微观/财务风险） | primary=deepseek-flash，fallback=本地 | **保持 `deepseek-flash`，但显式关闭或降档思考** | 1M 上下文 + JSON✓ + 便宜；32.7s 问题与它无关 |
| **`decision`（A17）** | primary=**deepseek-v4-pro**（32.7s） | **见下** | |

**A17 的候选方案（按我的推荐度排序）**：

1. **`deepseek-flash` + 关闭思考（或 effort=low）** —— 最优性价比。理由：同为 DeepSeek 家族，`response_format={'type':'json_object'}` 行为一致；1M 上下文足够装 5~9 份上游结论；价格是 v4-pro 的 **1/3.3**（谷）到 1/2.4（峰）；**关闭思考后延迟量级性下降**（第三方实测 none 档 ~11s vs high 档 >100s）。⚠️ 前提：必须先做 A/B 验证仲裁质量没有掉。
2. **`glm-5.3-flash`（¥0.8/¥2.8）** —— 单次 ¥0.0101，是 v4-pro 的 7.8%。智谱官方称其为「普惠的全球前沿模型」，1M 上下文。**风险**：GLM-5.3 系列强制思考（GLM-5.3 明确不支持关闭；Flash 版本的文档我未逐项确认），需实测延迟。
3. **`glm-4.7-flash`（免费）** —— **强烈建议作为 A17 的 benchmark 对照组与降级备选**。免费、200K、128K 输出、结构化输出✓、官方称 SWE-bench/τ²-Bench 同尺寸 SOTA。**唯一风险**是免费档的**并发/速率限制与稳定性未在定价页说明**（见 §5）。
4. **`qwen3.8-flash`（¥0.8/¥2.7）** —— 单次 ¥0.0098，1M 上下文，结构化输出✓、FC✓、缓存命中 ¥0.1（输入 1/8）。百炼控制台可看余量、有「用完即停」保护。
5. **`MiniMax-M3`** —— 单次 ¥0.0285，**但百炼文档标注 `MiniMax-M3` 结构化输出「不支持」**（见百炼文本生成页三方模型表）→ **对本系统是硬伤**（除非走 MiniMax 官方 API，其 JSON 支持未确认）。

**明确不建议用于 A17 的**：

- **`kimi-k3`**：单次 ¥0.347，**比现有 v4-pro 贵 2.7 倍**，且上下文 1M 主要是为长文档设计，对本任务无增益。
- **`glm-4.5-air`**：输出 ≥0.2K 时输出价从 2 元跳到 **6 元**，而 A17 输出必然 ≥0.2K，实际成本 ¥0.0202 —— 性价比不如 glm-5.3-flash。
- **1.5B / 3B 级本地模型做 A17**：见 §3，能力与上下文均不足。
- **任何 thinking 默认开启且 effort 不可降到 none 的模型做 A17 primary**：延迟不可控（见 §4）。

---

## 3. 本地 Ollama 替代建议（RTX 4060 8GB + 32GB RAM）

### 3.1 8GB 显存下的显存账（第三方实测/估算）

来源：<https://willitrunai.com/blog/qwen-3-gpu-requirements>（2026-09-06 更新，**第三方估算站，其自述「All estimates are approximations based on mathematical models」**，非实测）

| 模型 | Q4_K_M | Q5_K_M | Q6_K | Q8_0 | F16 |
|---|---|---|---|---|---|
| Qwen3 0.6B | 0.5 GB | 0.6 | 0.7 | 0.9 | 1.3 |
| Qwen3 1.7B | 1.1 GB | 1.3 | 1.5 | 1.9 | 3.4 |
| **Qwen3 4B** | **2.5 GB** | 3.0 | 3.6 | **4.6** | 8.0 |
| **Qwen3 8B** | **4.6 GB** | 5.6 | 6.6 | **8.5** | 16.1 |
| Qwen3 14B | 8.3 GB | 10.2 | 12.0 | 15.7 | 28.0 |
| Qwen3 30B-A3B（MoE，激活3B） | 16.8 GB | 20.6 | 24.2 | 31.6 | 60.0 |
| Qwen3 32B | 19.1 GB | 23.5 | 27.6 | 36.1 | 64.4 |
| Qwen3.5 9B | **~5.7 GB** | — | — | — | — |
| Qwen3.5 27B | ~16.5 GB | — | — | — | — |

> 该站明确注明：「Add ~1-2 GB for KV cache and runtime overhead at default context lengths.」

**直接回答你的问题**：

- **8B 模型 Q8_0 在 8GB 里放得下吗？** → **放不下（含 KV cache）**。官方估算 8B Q8_0 = **8.5 GB** 权重本身已超 8GB 显存，再加 1–2GB KV cache/运行时 = 9.5–10.5GB。**必然触发 partial offload 到 CPU**。
- **那能开到多少量化 + 多少上下文？** → 8B 的 **Q5_K_M（5.6GB）是 8GB 卡的实际上限**，留 2.4GB 给 KV cache/运行时；**Q4_K_M（4.6GB）留 3.4GB**，可以开更长上下文。而**你实测 qwen3:8b 常驻 5.2GB** —— 与 Q4_K_M 的 4.6GB + 少量开销吻合，说明你现在跑的就是 Q4_K_M 档。
- **8GB 能跑的「更强」选项**：`Qwen3.5 9B` Q4_K_M **~5.7GB** —— 比 Qwen3 8B 的 4.6GB 多 1.1GB，**在 8GB 内可行但 KV cache 余量被压缩到 ~2GB**。这是 8GB 卡上「同显存换更强模型」最现实的一步。

**该站给出的 Qwen3 8B 在 RTX 4060 8GB 上的速度估算：~50 tok/s**（Q4_K_M）。对照你实测 **44 t/s**，方向一致（你的实测略低，可能受上下文长度与 KV cache 影响）。→ 可认为 **44 t/s 就是这个档位的真实水平，没有明显优化空间**。

**该站的量化选择建议（原文翻译）**：
| 显存预算 | 推荐量化 | 说明 |
|---|---|---|
| 极紧（≤4GB） | Q4_K_M | 可用，余量最小 |
| 正常（4–12GB） | **Q5_K_M** | 质量/体积平衡最好 |
| 充裕（12–24GB） | Q6_K | 多数任务近乎无损 |
| 极充裕（24GB+） | Q8_0 | 实际等同 F16 |

> 对 8GB 卡的推论：**Q5_K_M 是理论最优，但你没有 12GB。** 在 8GB 下 Q5_K_M（5.6GB）会挤掉 KV cache 空间 → 长 input（你实测 1.8k tokens）会导致上下文吃紧。**结论：Q4_K_M 仍是你 8GB 下的正确选择**，除非把上下文限制得很短。

### 3.2 2026 年 8GB 内的小模型候选（按「结构化抽取 + 中文推理」排序）

| 模型 | 参数 | Q4_K_M 显存 | 8GB 可行性 | 结构化输出支持 | 证据 |
|---|---|---|---|---|---|
| **GLM-4.7-Flash** | 30B 级 | — | ❌ **本地不可行** | 云端支持 | [智谱模型页](https://docs.bigmodel.cn/cn/guide/models/free/glm-4.7-flash) |
| **Qwen3.5 9B** | 9B dense | ~5.7 GB | ⚠️ 可行但 KV 余量小 | Ollama `format` 支持（见 3.3） | [willitrunai](https://willitrunai.com/blog/qwen-3-gpu-requirements) |
| **Qwen3 8B** | 8B dense | 4.6 GB | ✅（你已在跑） | Ollama `format` | 同上 |
| **Qwen3 4B** | 4B dense | 2.5 GB | ✅ 宽裕，可开长上下文 | Ollama `format` | 同上 |
| **Qwen3 1.7B / 0.6B** | 1.7B/0.6B | 1.1 / 0.5 GB | ✅ | Ollama `format` | 同上 |
| GLM-4-9B / GLM-4-9B-Chat | 9B | ~5.5–6 GB（未取到官方量化表） | ⚠️ | 未确认 | [智谱定价页](https://docs.bigmodel.cn/cn/guide/start/pricing) 提到 GLM-4-9B 已开放**免费商用授权**，并有 8K-int8 私有实例价 |
| `qwen3:14b` | 14B | 8.3 GB | ❌ **超 8GB** | — | willitrunai |
| Qwen3 30B-A3B（MoE） | 30B/激活3B | 16.8 GB | ❌ **超 8GB 一倍** | — | willitrunai |
| Llama-3.2-3B / Gemma-3 4B / Phi-4-mini | 3–4B | 2–3 GB | ✅ | 未确认 | **本次未取得可引用的中文能力证据** |

**⚠️ 关于 MoE 的一个重要更正机会**：Qwen3 30B-A3B 推理速度等同 3B 模型，**但显存要装下全部 30B 参数（Q4 需 ~17GB）**。8GB 卡**不可能**跑 30B-A3B。任何「MoE 省显存」的说法对 8GB 卡都不成立。

### 3.3 Ollama 结构化输出（JSON Schema 受约束解码）

**官方事实**：
- Ollama 自 **2024-12-06** 起支持 `format` 参数传 **JSON Schema**，做**受约束解码**。官方原文：「Ollama now supports structured outputs making it possible to constrain a model's output to a specific format defined by a JSON schema.」
- 也支持 `format: "json"`（仅保证合法 JSON，不约束 schema）。
- 支持 cURL / Python（Pydantic `model_json_schema()` 推荐）/ JavaScript（Zod）/ **OpenAI 兼容端点**（`client.beta.chat.completions.parse(..., response_format=PydanticModel)`）。
- 官方「最佳实践」三条：① 用 Pydantic/Zod 定义 schema；② **prompt 里也写「return as JSON」**；③ **temperature 设 0**。
- 官方「What's next」列明：**「Performance and accuracy improvements for structured outputs」** → 暗示当前仍有性能/准确率改进空间。
- 来源：<https://ollama.com/blog/structured-outputs>（2024-12-06 博客）与 <https://docs.ollama.com/capabilities/structured-outputs>（现行文档，2026-09-28 抓取）
- **重要限制（官方现行文档首行）**：「**Ollama's Cloud currently does not support structured outputs.**」→ 只有**本地** Ollama 支持，Ollama Cloud 不支持。

**哪些模型对结构化输出支持最好？** → **官方文档未给出模型兼容性清单**。本次搜索**未找到**「不同模型在受约束 JSON 解码下的成功率/速度损失」的公开实测。→ **未找到公开证据**（见 §5）。可确认的是：官方示例用的是 `llama3.1`、`llama3.2-vision`、`gpt-oss`，**未使用 Qwen 系列作示例** —— 这不代表 Qwen 不支持（`format` 是服务端 grammar 层实现，与模型无关），但官方示例的模型选择可供参考。

### 3.4 本地 vs 云端延迟对比

**可推算的（用你自己的实测数据 + 官方/第三方参数）**

你已知：qwen3:8b 完成 **input 1.8k / output 1.6k** 用 **45.7s**，生成速度 **44 t/s**。

拆解：
- **纯生成时间** = 1600 / 44 ≈ **36.4s**
- → **剩余 ≈ 9.3s 是 prefill（1.8k input）+ 调度/加载开销**
- → 反推 **prefill 吞吐 ≈ 1800 / 9.3 ≈ 195 tok/s**（若 9.3s 全算 prefill，这是下限；实际 prefill 吞吐更高，因为含调度开销）

**外推到 A17 口径（input 1504 / output 3168）**：
- 生成 = 3168 / 44 ≈ **72s**
- prefill ≈ 1504 / 195 ≈ **7.7s**
- **合计 ≈ 80s**（对照组：云端 v4-pro 实测 32.7s）

**结论：本地跑 A17 约 80s，是云端 32.7s 的 2.4 倍。** 而且这还没算：
1. **Ollama 只有 1 个计算槽位，请求严格串行** → 18 个 Agent 的任何并发都会排队，**端到端墙钟时间不是 80s 而是所有本地请求之和**。
2. 8GB 显存下 qwen3:8b 的 Q4_K_M 已是极限，**长 input（1.5k–3.5k）会持续挤占 KV cache**，可能出现比线性更差的劣化。

**关于「partial offload 到 CPU 速度断崖」**：本次**未找到可引用的实测数据**（willitrunai 只给了显存占用估算，未给 offload 后的速度）。→ **未找到公开证据**，但可以用你自己的机器验证：把 `qwen3:14b`（Q4 需 8.3GB，超 8GB）拉下来跑同一个 1.8k/1.6k 任务，对比 44 t/s —— 这是**一次实验就能拿到的本地证据**，成本只有一次 ollama pull。

### 3.5 本地能替代哪些层（结论）

| 层 | 能否本地替代 | 理由 |
|---|---|---|
| `light`（分类/抽取/情感） | ✅ **能** | 短输入短输出，1.5B–4B 足够；串行不构成瓶颈 |
| `medium`（新闻去伪/事件抽取） | ✅ **能，但边际** | qwen3:8b 已够；换 Qwen3.5 9B 可小幅提升，但 KV 余量变紧 |
| `reasoning`（宏观/中观/微观/财务风险） | ❌ **不能作为 primary** | 输出 200–3200 tokens，本地单次 20–80s，且串行排队 |
| **`decision`（A17）** | ❌ **明确不能** | 实测 3168 output → 本地 ≈80s；且单槽位导致与其他 Agent 抢资源 |

**推荐的本地/云端分工（不增加串行往返的前提下）**：
- 本地保留 `light` 层（qwen2.5:1.5b 或换 qwen3:4b，2.5GB，可给 KV cache 留更多空间）。
- **`medium` 层的付费 fallback 改为 `glm-4.7-flash`（免费）** —— 这样 fallback 不再花钱，且结构化输出官方标支持。
- `reasoning` 保持 `deepseek-flash` + **显式关闭思考**（见 §4）。
- `A17` 见 §2.4。

---

## 4. A17 的 32.7s 延迟归因与优化路径

### 4.1 归因：先把数算清楚

已知：**input 1504 tokens、output 3168 tokens、总耗时 32.7s、模型 deepseek-v4-pro**。

**第一步：这不是排队，是生成。**
- 若按 3168 output / 32.7s 计算，**等效生成速度 ≈ 97 tok/s**。这对一个前沿推理模型是正常吞吐，**说明 32.7s 里几乎没有排队等待**。
- 佐证：官方并发上限 `deepseek-v4-pro` 为 500（[定价页](https://api-docs.deepseek.com/quick_start/pricing)），单用户单请求不可能撞到这个上限。

**第二步：思考模式默认开着，且默认 effort=high。**
- 官方原文（[思考模式文档](https://api-docs.deepseek.com/zh-cn/guides/thinking_mode)）：「思考模式**默认打开**，且 effort 默认为 `high`」。
- 官方映射表：`medium→high`、`high→high`、`xhigh→high` —— **即默认状态下你拿到的是 high 档**。
- **你的 3168 output tokens 里，很可能有相当一部分是 `reasoning_content`，而不是正文。** 这是最需要立刻验证的一点（见 4.2 第一条）。

**第三步：output 长度本身是最大成本项。**
- 即使完全关闭思考，3168 tokens 的正文在 ~100 tok/s 下也需要 **~32s**。**换句话说：把思考关掉，最乐观也只降到接近 32s 的下限，除非同时缩短正文。**
- 这个推论很重要 —— 它意味着**只调 `reasoning_effort` 可能收益有限**，必须同时压缩 output。

### 4.2 优化路径（按收益排序，每条带证据）

#### 【收益最高｜证据：官方文档 + 第三方实测】1. 显式关闭或降档思考模式

**官方依据**：
- 开关：`{"thinking": {"type": "disabled"}}`（OpenAI 格式，需放进 `extra_body`）
- 强度：`reasoning_effort` 支持 `minimal/low/medium/high/max/xhigh/ultra`，映射为 `low/high/max`
- **注意**：官方文档把 `none` 写作 Anthropic 格式 `{"reasoning": {"effort": "none"}}` 的取值（`none` 表示关闭思考模式），OpenAI 格式下官方表格里**没有列 `none`**

**第三方实测（⚠️ 重要限定：该实测跑在自建 vLLM serving 上，不是 api.deepseek.com，作者本人明确警告「apply เฉพาะ vLLM local serving」「official DeepSeek API อาจ behave ต่างกันโดยสิ้นเชิง」）**，来源 <https://blog.2my.xyz/2026/08/17/deepseek-v4-flash-reasoning-effort-test/>：

| 测试 | 结果 |
|---|---|
| Test #1 `reasoning_effort` × `thinking=enabled` | `low`/`medium`/`high` **输出逐字节相同**（148 tokens）；`xhigh`/`max` 相同（273 tokens） |
| **Test #3 完全不传 `reasoning_effort`**（只留 `thinking: disabled`） | reasoning_chars = **0**；**延迟从 4.34s → 0.68s（快 6.4 倍）** |
| Test #2 `thinking=disabled` + `reasoning_effort=max` | **reasoning 仍然产生**（1404 chars），**content = 0（空正文）**，总耗时 17.00s |
| 结论 | **`reasoning_effort` 会覆盖 `thinking.type`** —— 只要 payload 里有 `reasoning_effort`，`thinking: disabled` 就无效 |

**另一份第三方实测（CSDN，2026-08-14，来源 <https://deepseek.csdn.net/6a7f065010ee7a33f29afe52.html>）**，四档思考模式对比：

| 思考模式 | 平均首字延迟 | 单轮总耗时 | Reasoning Token 占比 | 适用场景（原文建议） |
|---|---|---|---|---|
| **none** | **< 0.1s** | **~11s** | ~0% | 简单问答、**对延迟敏感的场景** |
| low | ~7s | ~22s | ~30% | 轻度推理、润色、总结 |
| high | **> 30s** | **> 100s** | ~90%+（易耗尽） | 复杂数学、深度代码调试、多步规划 |
| max | > 40s | > 150s | ~95%+（极易耗尽） | 极高难度科研、全栈架构 |

**⚠️ 这份 CSDN 数据的可信度警告**：
- 它引用的价格（「输出 $0.28 / 1M」「缓存命中 $0.0028 vs 未命中 $0.14」「相差整整 50 倍」）**与官方现行价目表不符**（官方：输出 $0.60/$1.20、缓存命中 $0.003、未命中 $0.15）。→ 该文的价格段落**不可引用**，但其延迟表格可作为**方向性**参考。
- 它测的是 `deepseek-v4-flash`，**不是 v4-pro**。v4-pro 的思考更重，延迟只会更长。
- 未说明样本量（「12 轮」对 none/low，但 high/max 是「首轮即失败」）。

**这条路径的收益估计（保守）**：把 effort 从 high 降到 low/关闭，**首字延迟从 >30s 降到 <0.1s（感知延迟，配合流式）**，总耗时取决于正文长度。

#### 【收益高｜证据：官方文档】2. 让你先看到东西 —— 流式输出

**关于 stream=True 对首 token 延迟的影响**：
- 严格说，**流式不降低「模型开始生成」的时间**，但它让**用户/下游在看到第一个 token 时就能开始处理**，把「感知延迟」从「总耗时」变成「TTFT」。
- 官方文档对 `reasoning_content` 与 `content` 的流式分离有明确说明（[思考模式文档](https://api-docs.deepseek.com/zh-cn/guides/thinking_mode) 的「流式」示例），**思考阶段的内容走 `delta.reasoning_content`，正文走 `delta.content`** —— 这意味着**流式下你可以把「思考中」渲染成进度提示，避免 32.7s 的白屏**。
- 第三方数据支撑 TTFT 与总时长的差距：AIMultiple 测 `deepseek-v4-flash` 短答首字 3.6s、长答总时 24.3s（[来源](https://aimultiple.com/llm-latency-benchmark)）→ **TTFT 与总时长可以差 7 倍**。
- **对总时长有没有影响？** 官方文档**未说明**流式会改变总耗时。第三方实测（blog.2my.xyz）**全程使用流式**测量，其 TTFT/total 两列数值不同，说明流式不改变 total。→ **结论：流式改善感知延迟，不改善总时长。**
- **⚠️ 但有一个反直觉的副作用**：官方文档明确「在非流式请求后」用户拿到完整响应，而**流式请求下 `reasoning_content` 会被拼接进上下文**（若携带 `tools` 参数，历史轮次的 `reasoning_content` 必须完整回传，否则 400 报错）。→ 对多轮 Agent 场景，**流式 + tools 会让上下文膨胀**，需要实测 token 增长。

#### 【收益中｜证据：官方 JSON 文档的警告】3. max_tokens 的正确设法与「空正文」陷阱

**你的系统踩过的坑有官方文档背书**（[DeepSeek JSON Output 官方文档](https://api-docs.deepseek.com/zh-cn/guides/json_mode) 注意事项原文）：
1. 需设 `response_format` 为 `{'type': 'json_object'}`
2. prompt 里**必须含 `json` 字样**并给出样例
3. **「需要合理设置 `max_tokens` 参数，防止 JSON 字符串被中途截断」**
4. **「在使用 JSON Output 功能时，API 有概率会返回空的 content。我们正在积极优化该问题」** ← **官方承认这个 bug 存在且尚未完全修复**

**两个独立的第三方来源都记录了「思考吃光 max_tokens 导致正文为空」**：
- CSDN：8K 上限下 high 档「输出的 8191 个 Token 全部是 `reasoning_tokens`，留给正文的空间为 0」；建议「必须将 `max_tokens` 提升至 **32K 甚至更高**」
- AIMultiple：「When thinking fills the whole budget, no tokens are left for the answer and the response comes back empty.」（MiniMax 在代码任务上首轮成功率不到一半）

**⚠️ 对「调小 max_tokens 能否降延迟」的直接回答**：
- **调小 max_tokens 只有在它能阻止模型继续生成时才会降延迟**。但**对推理模型，调小 max_tokens 是高危操作**：思考会先吃预算，正文可能为空 → **你花了一样的时间（甚至更多，见 Test #2 的 17.00s）却拿到空结果**。
- 所以：**「调小 max_tokens 降延迟」在本系统不成立，除非你先关闭思考**。正确顺序是 **① 先关思考 → ② 再按正文实际长度设 max_tokens**。
- 监控字段：CSDN 建议监控 `usage.output_tokens_details.reasoning_tokens`；官方 `usage` 里缓存相关字段是 `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens`（[官方缓存文档](https://api-docs.deepseek.com/zh-cn/guides/kv_cache)）。

#### 【收益中｜证据：官方定价页】4. 缓存命中（降成本，不降延迟）

- `deepseek-flash`：缓存命中 **1/50**；`deepseek-v4-pro`：**1/30**（[定价页](https://api-docs.deepseek.com/quick_start/pricing)）
- 官方缓存规则（[缓存文档](https://api-docs.deepseek.com/zh-cn/guides/kv_cache)）：**请求结束位置落盘**（用户输入结束 + 模型输出结束各产生一个缓存前缀单元）+ **公共前缀检测** + **按固定 token 间隔落盘**。命中要求**完整匹配缓存前缀单元**。
- **对 A17 的具体含义**：你的 system prompt + 上游 5~9 份结论是稳定前缀 → **把它们放在 messages 最前面、且不要插入时间戳/随机数**，第二次及以后的请求就能命中。
- **实测参考**（CSDN，第三方）：Agent 工具回调场景「单次请求可命中 500+ tokens」；长文连续创作若每轮重发全文命中率低至 1.6%，改用滑动窗口追加可提升到 78%+。
- **但对 A17 的收益有限**：input 只有 1504 tokens，即使全命中，省下的是 ¥0.00x 量级。→ **缓存是省钱手段，不是降延迟手段。**

#### 【收益中｜证据：本系统架构】5. 拆掉 A17 的超长输出

这是**唯一能真正把 32.7s 压下来的结构性手段**：
- 32.7s 的物理下限由 3168 output tokens ÷ ~97 tok/s ≈ 32s 决定。**要降总时长，必须降 output tokens。**
- 可行的拆分：把 A17 的 3168 tokens 拆成 **2 次串行调用各 ~1500 tokens**，虽然总 tokens 不变，但**每次调用的墙钟时间减半**，且第一次的结果可以先渲染给用户。代价：增加一次串行往返（与 AGENTS.md 的「禁止新增串行往返」冲突，需要权衡；但这是 **LLM 调用**而非首屏 HTTP 往返，性质不同）。
- 或者：**结构化字段与自然语言解释分离** —— 三档情景/证伪信号/仓位建议用严格 JSON（短），把「回答用户开放式问题」放到第二个可选调用（可流式、可流式到用户面前）。

#### 【收益低但便宜｜证据：官方定价页】6. 避开高峰时段

- 高峰：UTC 周一至周五 01:00–04:00 与 06:00–10:00（**北京时间 09:00–12:00 / 14:00–18:00**），高峰价为低谷 **2 倍**
- **恰好覆盖 A 股交易时段** → 你的日间投研请求全部落在高峰价
- 来源：[DeepSeek 定价页](https://api-docs.deepseek.com/quick_start/pricing)
- ⚠️ 官方**未说明**峰谷是否影响延迟/吞吐。→ **未找到公开证据**，不要假设低谷更快。

#### 【不要做】把 A17 换成 kimi-k3 / 加大 effort

- `kimi-k3` 单次 ¥0.347，是现在 v4-pro 官方价的 2.7–3.6 倍
- `reasoning_effort` 调到 `max`：第三方实测 max 档首字 >40s、总耗时 >150s

### 4.3 非推理型模型的延迟对比（回答「换 deepseek-chat 或 qwen-flash 会怎样」）

**⚠️ 前提澄清**：2026 年的 DeepSeek **已经没有 `deepseek-chat` / `deepseek-reasoner` 这两个模型名**了。官方现行模型只有 `deepseek-flash` 与 `deepseek-v4-pro`，**两者都是「默认开启思考的混合模型」**（[定价页](https://api-docs.deepseek.com/quick_start/pricing)：「THINKING MODE: Supports both non-thinking and thinking (default) modes」）。→ **「换非推理模型」在 DeepSeek 内部的等价操作是 `thinking: disabled`。**

**可用的对比数据**：

| 对比 | 数据 | 来源 | 性质 |
|---|---|---|---|
| `deepseek-v4-flash` 短答首字 | 3.6s（推理开启，默认态） | AIMultiple | 第三方实测（1320 请求） |
| `deepseek-v4-flash` 长答总时 | 24.3s（output 上限 1024） | AIMultiple | 第三方实测 |
| 非推理模型短答首字 | claude-haiku-4-5 **0.96s**、gpt-5-2 **0.8s** | AIMultiple | 第三方实测 |
| 非推理模型长答总时 | haiku-4-5 **~5.5s**（~180 tok/s 流式） | AIMultiple | 第三方实测 |
| 推理 vs 非推理的总时长差距 | 非推理组 5.5–12.7s vs 推理组 **18.5–35s** | AIMultiple | 第三方实测 |
| 关闭思考前后（同一模型同一任务） | 4.34s → **0.68s**（6.4×） | blog.2my.xyz | 第三方实测（**自建 vLLM，非官方 API**） |
| none vs high 档（四档对比） | ~11s vs **>100s** | CSDN | 第三方实测（**价格段落已证伪，仅供方向参考**） |

**对 `qwen-flash` 类模型**：本次**未找到** qwen-flash/qwen3.8-flash 在同类任务上的公开延迟实测。→ **未找到公开证据**。但百炼文档确认 qwen3.8-flash 支持 `enable_thinking` 参数，**默认态未在文档中明确**，需要实测。

**结论**：换非推理/关思考在延迟上的收益是**量级性的**（6–10 倍 TTFT 改善），但**代价是仲裁质量未知**。→ **必须做 A/B 实测**，不能凭 benchmark 拍板。

---

## 5. 不确定的地方（明确标注证据不足）

### 5.1 价格类的不确定

| 项 | 状态 |
|---|---|
| **豆包 Doubao 全部价格** | ❌ **未找到官方来源**。火山方舟定价页（4 个 URL 尝试）均为 JS 渲染，web_fetch 返回空白。**报告中没有豆包任何价格数字。** |
| **百度 ERNIE 2026 现行价格** | ❌ **未找到官方来源**。千帆价格页正文为 JS 渲染。唯一可引用的免费政策是 **2024-05-21 的公告**，距今 2 年 4 个月，**是否仍有效无法证实**。 |
| **qwen3.8-flash / qwen3.7-flash 的人民币刊例价** | ⚠️ **证据不足**。百炼 CN 定价页抓取被截断（4372 行只取到前 53KB，flash 章节在截断之后）。`qwen3.8-flash` 的 0.8/2.7 来自**阿里云开发者社区用户文章**（第三方）；`qwen3.7-flash` 的 $0.028/$0.11 来自**国际化站点的美元标价**。两者**都已标注性质**，但**上线前必须在百炼控制台核对人民币刊例价**。 |
| **`qwen-flash` / `qwen-turbo` 的现行价格** | ❌ **未取到**。百炼文档把这两个列为「旧版Qwen」，未在可抓取片段中给出单价。 |
| **MiniMax 中国大陆站（platform.minimaxi.com）价格** | ⚠️ 本次只抓到 `.io` 国际化站。中国大陆站可能存在人民币刊例价差异。 |
| **DeepSeek 新用户免费额度** | ❌ **未找到官方来源**。定价页与快速开始页均未提及。 |
| **Kimi 新用户免费额度** | ❌ **未找到官方来源**。 |
| **`moonshot-v1-8k/32k/128k` 与 `kimi-k2.5` 的官方价格** | ❌ **已从 Kimi 官方定价页消失**。只能在百炼等第三方平台调用。 |
| **MiniMax abab 系列价格** | ❌ **已下线**，官方价目表无此项。 |

### 5.2 价格矛盾清单（不裁定，两个来源都列）

| 模型 | 来源 A | 来源 B | 矛盾点 |
|---|---|---|---|
| **`deepseek-v4-pro`** | 官方 [api-docs](https://api-docs.deepseek.com/quick_start/pricing)（2026-09-28）：$0.66 输入 / $1.98 输出（低谷，≈¥4.4/¥13.3） | [SiliconFlow 价格页](https://siliconflow.cn/pricing)（2026-09-28）：**¥12 输入 / ¥24 输出** | **SiliconFlow 报价 ≈ 官方低谷价的 2.7×/1.8×，≈ 官方高峰价的 1.4×/0.9×**。聚合平台在此模型上**不比官方便宜**。需按你的实际调用时段核算。 |
| **`deepseek-v4-flash`** | 官方：$0.15/$0.60（低谷，≈¥1.0/¥4.0） | SiliconFlow：¥3.00/¥9.00（0–2点/8–24点）、**¥1.50/¥4.50（2–8点）** | SiliconFlow 非优惠时段 ≈ 官方 **3×**；优惠时段 ≈ 官方 1.5×。**2–8 点档是 SiliconFlow 自己设的谷时**，与 DeepSeek 官方峰谷定义（工作日 UTC 01–04/06–10）**不一致**。 |
| **`deepseek-v4-flash` 输出价** | 官方现行：**$0.60/$1.20** | [CSDN 2026-08-14 文章](https://deepseek.csdn.net/6a7f065010ee7a33f29afe52.html)：**「$0.28 / 1M tokens」**；缓存命中「$0.0028」、未命中「$0.14」 | **该文价格整体偏低约 2 倍**。**我已核验该文价格段落不可引用**（官方缓存比是 1/50 而该文说 1/50 但绝对数全错），但其延迟数据仍作为方向性参考保留。**这是本次调研发现的最明确的一处来源污染。** |
| **`glm-4.5-air` 输出价** | [智谱定价页](https://docs.bigmodel.cn/cn/guide/start/pricing)：输出 <0.2K 时 **2 元**，≥0.2K 时 **6 元** | 同一页：[SiliconFlow](https://siliconflow.cn/pricing)：**¥6.00** 无阶梯 | **不矛盾但极易误读**：智谱的 2 元档只在输出 <200 tokens 时适用。A17（3168 tokens）适用 **6 元**。若按 2 元估算会**低估 3 倍**。 |
| **GLM-5.3** | 智谱官方：**8/28** | [UCloud ModelVerse](https://docs.ucloud.cn/modelverse/mdToPdf/modelverse.pdf)：输入<32K **4元** / 输出 **18元** | UCloud 报价更便宜（4/18 vs 8/28）。**是转售补贴还是文档过期无法判断**。→ 需实测确认可用性与是否为同版本权重。 |
| **百炼定价页自相矛盾提示** | 百炼 [model-pricing](https://help.aliyun.com/zh/model-studio/model-pricing) 页顶 Note：「显式缓存创建按标准输入单价的 **125%** 计费、命中按 **10%** 计费」 | [阿里云开发者社区文章](https://developer.aliyun.com/article/1761526) 称 qwen3.8-flash「缓存命中价格仅为标准输入价的**八分之一**」 | **1/10 vs 1/8**。Note 是通用规则，文章是模型专属说法。**未裁定**，以控制台账单为准。 |
| **SuperCLUE 数据** | [C114 转载](https://www.c114.net.cn/ainews/71776.html)（2026-03-30）：豆包 71.53 分国内第一 | 我**未能访问 SuperCLUE 官网原文**核对 | **该分数来自媒体转载而非榜单原文**。且报道**未给出 v4-pro/Qwen3.8/GLM-5.3 的分数**，**无法支撑「哪个便宜模型接近 v4-pro」这一核心问题**。 |

### 5.3 能力/证据类的不确定（对本项目最关键）

1. **❌ 没有任何「便宜模型 vs deepseek-v4-pro 在中文多源综合/冲突仲裁任务上的对比实测」。** 这是本报告**最大的证据缺口**。我找到的只有：一份媒体转载的 SuperCLUE 总榜（未含 v4-pro 分数）、一份延迟榜（不含能力）。→ **「哪个便宜模型能胜任 A17」目前只能靠推断，不能靠证据。** 唯一的可靠路径是**用你自己的历史 A17 输入做 A/B**（注意 AGENTS.md 已记录的坑：**LLM 缓存 scope 不含 provider/model，"换模型对比"三个模型输出曾逐字节相同** —— 做对比前必须先清测量路径）。
2. **❌ 没有「长文本综合」任务的 cheap-vs-expensive 公开对比**（LongBench / ∞Bench / RULER / Artificial Analysis 均未取到可引用数字）。
3. **❌ 没有结构化输出成功率榜**（BFCL / JSONSchemaBench 未取到）。本系统 18 个 Agent 全部依赖严格 JSON，**这是最该有数据却最缺数据的一项**。
4. **❌ 没有中文金融领域评测数据**（FinEval / CFBenchmark / FinanceBench 中文版未取到）。
5. **⚠️ Ollama 受约束解码的模型兼容性：官方文档未给清单。** 官方只说「支持 `format` 传 JSON Schema」，未说明哪些模型效果最好，也未给速度损失数据。官方 roadmap 里「Performance and accuracy improvements for structured outputs」暗示**当前不完美**。
6. **⚠️ `glm-4.7-flash` 免费档的并发/速率限制未在定价页说明。** 对 18-Agent 系统，「免费」是否伴随低 QPS 是**上线前必须实测**的问题。同理，免费档的**稳定性/SLA** 无任何承诺。
7. **⚠️ `reasoning_effort` 的官方语义与第三方实测不符。** 官方映射表说 `low→low`、`high→high`（不同档），但 vLLM 自建实测显示 `low`/`medium`/`high` **输出逐字节相同**。**这可能是自建 serving 的 bug，也可能是官方 API 的真实行为。** → **必须在 api.deepseek.com 上自己复现一次**（成本：3 次调用）。
8. **⚠️ 流式对 `reasoning_content` 上下文膨胀的影响未量化。** 官方要求带 `tools` 时必须完整回传历史 `reasoning_content`，否则 400。对多轮 Agent 这是**上下文成本放大器**，但**未找到实测数据**。
9. **⚠️ 8GB 显存 partial offload 的速度断崖无公开实测。** 需要你自己跑一次 `qwen3:14b` 对比 44 t/s 才能得到本机数据。
10. **⚠️ 峰谷时段是否影响延迟/吞吐：官方未说明。** 只确认影响价格（2×）。

### 5.4 我明确没有做的事（避免误读）

- **我没有访问豆包、ERNIE、混元、阶跃星辰的官方定价页并成功取到数字** → 报告中这些厂商**没有价格数字**，不是「便宜」也不是「贵」，是**未知**。
- **我没有做任何实测**（本次是纯文献调研，无 API 调用、无本地推理）。
- **我没有核对 SuperCLUE 官网原文**，只用了媒体转载。
- **所有 ×6.7 的汇率换算都是我做的**，不是官方数字，请标注为估算。

---

## 6. 可立即执行的验证清单（按性价比排序）

| # | 动作 | 成本 | 能回答什么 |
|---|---|---|---|
| 1 | 在 A17 加一行日志，记录 `usage.output_tokens_details.reasoning_tokens` 与 `prompt_cache_hit_tokens` | 1 行代码 | **3168 output 里有多少是思考？** 这决定 §4 所有优化能拿多少收益 |
| 2 | 同一 A17 输入，跑 3 组：`thinking=enabled+high`（现状）/ `enabled+low` / `disabled`，记录 TTFT + total + reasoning_tokens | 3 次调用，几分钱 | 官方 vs 第三方关于 effort 的**矛盾结论**在你这条链路上哪边成立 |
| 3 | 把 A17 primary 换成 `glm-4.7-flash`（免费）做影子跑（只记录不采纳） | **0 元** | 免费模型在**你的真实任务**上的仲裁质量。**这是全清单里最高价值的一步** |
| 4 | 同一输入跑 `deepseek-flash`（关思考）vs `deepseek-v4-pro`，**先清 LLM 缓存并确认 scope 含 model** | 几分钱 | 换模型到 Flash 的质量损失 |
| 5 | 实测 `glm-4.7-flash` 的并发上限与 p95 延迟 | 0 元 + 时间 | 免费档能否承载 18-Agent 的并发 |
| 6 | `ollama pull qwen3:14b`，跑你那个 1.8k/1.6k 任务 | 一次下载 | **本机** partial offload 的速度断崖实测数据（公开无此数据） |
| 7 | 把 A17 的 system prompt + 稳定前缀固定在最前，去掉时间戳 | 少量改动 | 缓存命中率（省钱，不省时间） |

---

## 7. 主要来源清单（全部为 2026-09-28 抓取）

**官方定价/文档**
- DeepSeek 模型与定价：<https://api-docs.deepseek.com/quick_start/pricing>
- DeepSeek 思考模式：<https://api-docs.deepseek.com/zh-cn/guides/thinking_mode>
- DeepSeek JSON Output：<https://api-docs.deepseek.com/zh-cn/guides/json_mode>
- DeepSeek 上下文硬盘缓存：<https://api-docs.deepseek.com/zh-cn/guides/kv_cache>
- 阿里云百炼模型调用价格：<https://help.aliyun.com/zh/model-studio/model-pricing>
- 阿里云百炼新人免费额度：<https://help.aliyun.com/zh/model-studio/new-free-quota>
- 阿里云百炼文本生成模型：<https://help.aliyun.com/zh/model-studio/text-generation-model>
- qwen3.7-flash 官方模型页（国际化站）：<https://www.alibabacloud.com/help/ja/model-studio/qwen3-7-flash>
- 智谱 API 定价：<https://docs.bigmodel.cn/cn/guide/start/pricing>
- 智谱 GLM-4.7-Flash：<https://docs.bigmodel.cn/cn/guide/models/free/glm-4.7-flash>
- 智谱 GLM-5.3：<https://docs.bigmodel.cn/cn/guide/models/text/glm-5.3>
- 智谱模型概览：<https://docs.bigmodel.cn/cn/guide/start/model-overview>
- Kimi 模型推理价格：<https://platform.kimi.com/docs/pricing/chat>
- MiniMax Pay as You Go：<https://platform.minimax.io/docs/guides/pricing-paygo>
- 硅基流动价格：<https://siliconflow.cn/pricing>
- 百度千帆 ERNIE 免费公告（2024-05-21）：<https://cloud.baidu.com/news/notice_c7a145b9-5c99-4870-939f-e09ba506aab3>
- 腾讯云 DeepSeek V4 Flash 商业化公告：<https://cloud.tencent.com/announce/detail/2278>
- Ollama 结构化输出博客：<https://ollama.com/blog/structured-outputs>
- Ollama 结构化输出文档：<https://docs.ollama.com/capabilities/structured-outputs>

**第三方实测/榜单**
- DeepSeek V4 Flash 四档思考模式实测（CSDN，2026-08-14，⚠️价格段落已证伪）：<https://deepseek.csdn.net/6a7f065010ee7a33f29afe52.html>
- DeepSeek-V4-Flash-0731 reasoning_effort 7 轮实测（自建 vLLM，2026-08-17）：<https://blog.2my.xyz/2026/08/17/deepseek-v4-flash-reasoning-effort-test/>
- AIMultiple LLM 延迟基准（2026-08-12，1320 请求）：<https://aimultiple.com/llm-latency-benchmark>
- SuperCLUE 2026-03 结果（C114 转载，2026-03-30）：<https://www.c114.net.cn/ainews/71776.html>
- Qwen3/3.5 显存需求（willitrunai，2026-09-06，第三方估算）：<https://willitrunai.com/blog/qwen-3-gpu-requirements>
- qwen3.8-flash 价格介绍（阿里云开发者社区用户文章，2026-09-08）：<https://developer.aliyun.com/article/1761526>
- 人民币汇率参考（香港经济日报）：<https://invest.hket.com/article/4190611/>
