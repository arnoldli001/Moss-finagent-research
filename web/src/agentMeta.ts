/**
 * Agent 展示元数据：agent_id → 中文名，置信度 high/medium/low → 高/中/低。
 *
 * 单一事实源在后端 configs/agents.yaml，经 /api/v1/agents/meta 下发；
 * 此处的 FALLBACK 表与 yaml 同步，保证接口未就绪/历史数据也能正确中文展示。
 * agent_id 仍是内部路由标识，本模块只做展示层转换，不参与任何请求参数。
 */

const FALLBACK_AGENTS: Record<string, { name: string; layer: string }> = {
  A01_data_collector: { name: "数据采集Agent", layer: "data" },
  A02_data_cleaner: { name: "数据清洗Agent", layer: "data" },
  A03_data_validator: { name: "数据校验Agent", layer: "data" },
  A04_data_storage: { name: "数据存储Agent", layer: "data" },
  A05_verifier: { name: "信息核验Agent", layer: "info" },
  A06_extractor: { name: "事件提取Agent", layer: "info" },
  A07_sentiment: { name: "舆情分析Agent", layer: "info" },
  A08_macro: { name: "宏观分析Agent", layer: "analysis" },
  A09_meso: { name: "中观分析Agent", layer: "analysis" },
  A10_micro: { name: "微观分析Agent", layer: "analysis" },
  A11_fin_risk: { name: "财务风险Agent", layer: "analysis" },
  A12_compliance: { name: "合规风险Agent", layer: "analysis" },
  A13_tech: { name: "科技行业Agent", layer: "industry" },
  A14_consumer: { name: "消费行业Agent", layer: "industry" },
  A15_cyclical: { name: "周期行业Agent", layer: "industry" },
  A16_pharma: { name: "医药行业Agent", layer: "industry" },
  A17_recommend: { name: "投研建议Agent", layer: "decision" },
  A18_audit: { name: "逻辑审计Agent", layer: "audit" },
};

const FALLBACK_CONFIDENCE: Record<string, string> = {
  high: "高",
  medium: "中",
  low: "低",
};

let agentTable: Record<string, { name: string; layer: string }> = {
  ...FALLBACK_AGENTS,
};
let confidenceTable: Record<string, string> = { ...FALLBACK_CONFIDENCE };
let inflight: Promise<void> | null = null;

/** 拉取后端元数据表（带内存缓存，失败静默用兜底表）。 */
export function loadAgentMeta(): Promise<void> {
  if (inflight) return inflight;
  inflight = fetch("/api/v1/agents/meta")
    .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
    .then((data: {
      agents?: Record<string, { name: string; layer: string }>;
      confidence_zh?: Record<string, string>;
    }) => {
      if (data.agents) {
        // 后端表优先，兜底表补齐后端缺失的 id
        agentTable = { ...FALLBACK_AGENTS, ...data.agents };
      }
      if (data.confidence_zh) {
        confidenceTable = { ...FALLBACK_CONFIDENCE, ...data.confidence_zh };
      }
    })
    .catch(() => {
      /* 元数据接口不可用时保留兜底表，不阻断页面 */
    })
    .finally(() => {
      inflight = null;
    });
  return inflight;
}

/** agent_id → 中文名；未知 id 原样返回（保留可排查信息）。 */
export function agentLabel(agentId: string | null | undefined): string {
  if (!agentId) return "未知Agent";
  return agentTable[agentId]?.name ?? agentId;
}

/** high/medium/low → 高/中/低；未知值原样返回。 */
export function confidenceLabel(level: string | null | undefined): string {
  if (!level) return "未知";
  return confidenceTable[level.toLowerCase()] ?? level;
}
