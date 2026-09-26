import Disclaimer from "./Disclaimer";
import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  api, CharacterProfile, FactorMetaItem, IntradayFactorCatalog, IntradayLevels,
  IntradayMode, MarketCycle, WeightProfile, WeightTemplate,
  IntradayWeightPreview, IntradayWeightProfileDetail, IntradayWeightProfileRequest,
} from "../api";

/**
 * 做T权重自定义编辑面板（权重档案）。
 *
 * 为什么要有这个面板：打分口径原本只写在 `configs/intraday.yaml` 里 —— 改一次要
 * 重启、要对文件名、还只能全局一套。但「该给箱体多少分」本来就跟标的有关：
 * 高波动震荡票该加重箱体/VWAP，趋势票该加重缠论结构，连板题材股该加重筹码与情绪。
 *
 * 三条不可动摇的口径（面板上都要让用户看得见）：
 *   1. **合计必须=100**。总分 = Σ(得分×权重) ∈ [-100,100]，合计一旦变成 120，
 *      ±20/±30 阈值就全部失效，而用户完全看不出来。
 *   2. **保存只提交差集**。档案是「这只票相对全局口径的覆盖」，提交全量会把
 *      全局默认值固化进这只票 —— 以后调全局权重，只有它不跟着变。
 *   3. **预览不落库**。拖滑杆时要的是「立刻看到分数怎么变」，预览走服务端同一条
 *      打分链路（完整快照），因此预览分数与保存后的真实分数必然同源同口径；
 *      代价是它要 1~3 秒，所以按钮必须有 loading。
 *
 * 因子清单、中文名、分组、模板、公式全部来自 `GET /intraday/factors`：
 * 前端不写死任何一项，否则后端加因子时这里必然漂移，而漂移的权重表会让用户
 * 保存出一套后端不认识的口径。
 */

/** 分时档位的中文标签与输入边界（与后端 LevelParams 的校验区间一致）。 */
const LEVEL_FIELDS: {
  key: string; label: string; hint: string;
  min: number; max: number; step: number;
}[] = [
  { key: "min_band_pct", label: "最小档位差 %", step: 0.1, min: 0, max: 10,
    hint: "回踩线~冲高线的最小间距。做T一轮双边成本≈0.2%，档位差太小会被摩擦成本吃光" },
  { key: "max_band_pct", label: "最大档位差 %", step: 0.5, min: 0.1, max: 40,
    hint: "档位差上限。箱体过宽（如20日振幅25%）时防止回踩/冲高线离现价太远而永不触发" },
  { key: "stop_loss_pct", label: "止损下限 %", step: 0.1, min: 0.1, max: 20,
    hint: "回踩线下方止损距离的百分比口径；与 ATR 口径取更宽的那个" },
  { key: "atr_stop_mult", label: "ATR止损倍数", step: 0.1, min: 0, max: 5,
    hint: "止损距离 = max(回踩线×止损%，该倍数×ATR)。高波动票靠它，固定百分比会被噪声打穿；填 0=只用百分比口径" },
  { key: "touch_band_pct", label: "贴线带宽 %", step: 0.1, min: 0.01, max: 5,
    hint: "判定「价格触及档位」的容差：price ≤ 回踩线×(1+该值%)。太小则永远等不到，太大则信号失焦" },
  { key: "dip_fallback_atr", label: "回踩兜底距离（×ATR）", step: 0.1, min: 0, max: 3,
    hint: "箱体下沿/布林下轨都跑到现价上方时（跳空高开、强势股上冲），回踩线按「现价下方该倍数×ATR」给一个真实可触及的位置" },
  { key: "take_profit_buffer_pct", label: "冲高缓冲 %", step: 0.1, min: -10, max: 10,
    hint: "冲高线在此缓冲之上才允许减仓（负值=允许提前一点减）" },
  { key: "vwap_extreme_z", label: "VWAP偏离极值 z", step: 0.1, min: 0.1, max: 6,
    hint: "|z|≥该值时视为「价格触及关键档位」的等效条件（对应需求里的 VWAP 极值）" },
];

const INTRADAY_LEVEL_ORDER = LEVEL_FIELDS.map((item) => item.key);

/**
 * 非数值开关（布尔档位）：本面板只读展示。
 *
 * 刻意不做成可编辑的勾选框：PUT 的 `levels` 声明为 `dict[str, float]`，
 * 布尔值一旦提交会被 pydantic 拒绝（422），而用户只会看到"保存失败"却不知为何。
 * 与其给一个按了会报错的开关，不如老实说明它只能在 YAML 里改。
 */
const BOOLEAN_LEVEL_KEYS = new Set(["blend_boll_bands"]);

type LevelRow = {
  key: string; label: string; hint: string; min: number; max: number; step: number;
};

/** 按 0.1 粒度归一化到合计 100（最大余额法，与后端 `_round_to_total` 同口径）。 */
function normalizeTo100(weights: Record<string, number>): Record<string, number> {
  const keys = Object.keys(weights);
  const total = keys.reduce((acc, key) => acc + (weights[key] || 0), 0);
  if (total <= 0) return { ...weights };
  const scaled: Record<string, number> = {};
  keys.forEach((key) => { scaled[key] = (weights[key] || 0) / total * 100; });
  const shifted: Record<string, number> = {};
  const floors: Record<string, number> = {};
  keys.forEach((key) => {
    shifted[key] = scaled[key] * 10;
    floors[key] = Math.floor(shifted[key]);
  });
  const remainder = Math.round(1000 - keys.reduce((acc, key) => acc + floors[key], 0));
  const order = keys.slice().sort((a, b) => {
    const ra = shifted[a] - floors[a];
    const rb = shifted[b] - floors[b];
    return rb - ra || (a < b ? -1 : 1);
  });
  const step = remainder >= 0 ? 1 : -1;
  for (let index = 0; index < Math.abs(remainder); index += 1) {
    // 刻意保留用户置 0 的因子：把 0 补成 0.1 会让「关掉这个维度」失效
    let cursor = index;
    for (let guard = 0; guard < order.length; guard += 1) {
      const key = order[cursor % order.length];
      if (step > 0 && weights[key] === 0) { cursor += 1; continue; }
      floors[key] += step;
      break;
    }
  }
  const result: Record<string, number> = {};
  keys.forEach((key) => {
    result[key] = Math.round(floors[key]) / 10;
  });
  return result;
}

function round1(value: number): number {
  return Math.round(value * 10) / 10;
}

/** 只保留两位小数，避免 0.30000000000000004 这类浮点尾巴进请求体。 */
function round4(value: number): number {
  return Math.round(value * 10000) / 10000;
}

/** 把 request() 抛出的 `请求失败(400): {"detail":"..."}` 还原成人话。 */
function humanizeError(exc: unknown): string {
  const raw = exc instanceof Error ? exc.message : String(exc);
  const match = raw.match(/\{[\s\S]*\}/);
  if (match) {
    try {
      const parsed = JSON.parse(match[0]) as { detail?: unknown };
      if (typeof parsed.detail === "string") return parsed.detail;
    } catch {
      /* 不是 JSON：原样返回 */
    }
  }
  return raw;
}

function pct(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined) return "—";
  return `${value.toFixed(digits)}%`;
}

function num(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined) return "—";
  return value.toFixed(digits);
}

/** 带符号的百分比（用于「离现价多远」这类有方向的数）。 */
function signedPct(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined) return "—";
  return `${value >= 0 ? "+" : ""}${value.toFixed(digits)}%`;
}

/** 带符号的数（贡献分/影响度）。 */
function signed(value: number | null | undefined, digits: number): string {
  if (value === null || value === undefined) return "—";
  return `${value >= 0 ? "+" : ""}${value.toFixed(digits)}`;
}

/**
 * 把一组档位映射到「相对现价」的 0~100 刻度上（50 = 现价）。
 *
 * 刻度跨度按最远的那个档位定，并留 15% 余量；只用当前档位集合就够 ——
 * 「现在 vs 改动后」两个数值是在同一行文字里对照的，不需要两套标记。
 */
function levelScalePositions(
  levels: { key: string; price: number }[], price: number,
): { key: string; pos: number }[] {
  const values = levels.map((item) => item.price)
    .filter((value) => Number.isFinite(value) && value > 0);
  if (!values.length || !(price > 0)) return [];
  const span = Math.max(2.0, ...values.map((value) => Math.abs(value - price) / price * 100));
  const extent = span * 1.15;
  return levels.map((item) => ({
    key: item.key,
    pos: Math.max(0, Math.min(100,
      50 + (item.price - price) / price * 100 / extent * 50)),
  }));
}

const REGIME_TEXT: Record<string, string> = {
  swing: "震荡回归",
  mixed: "混合",
  trend: "单边趋势",
};

/** 分区中文名（与打分面板 `IntradayScore.ZONE_TEXT` 同一套口径）。 */
const ZONE_TEXT: Record<string, string> = {
  strong_buy_zone: "偏多回踩区",
  buy_zone: "偏多试仓区",
  neutral: "震荡区间",
  sell_zone: "偏空试减区",
  strong_sell_zone: "偏空冲高区",
};

/** 档位刻度上的短标签。 */
const LEVEL_TEXT: Record<string, string> = {
  low_buy: "回踩线",
  high_sell: "冲高线",
  stop_loss: "止损位",
  vwap: "当日均价VWAP",
  boll: "布林轨",
};

export default function WeightProfileEditor({
  code, name, mode, levels: liveLevels, onClose, onSaved,
}: {
  code: string;
  name?: string;
  mode: IntradayMode;
  /**
   * 面板上**此刻**显示的关键价位（回踩/冲高/止损/VWAP）。
   *
   * 传进来的唯一用途是当「改动前」那一列：档位是时刻量（每分钟随 VWAP/布林重算），
   * 预览又要 1~3 秒，若让服务端自己再取一次基准，前后两次的 VWAP 会差 0.1% 量级，
   * 「变动」列里就会凭空出现一行 VWAP 位移 —— 用户会以为是自己改出来的。
   */
  levels?: IntradayLevels | null;
  onClose: () => void;
  onSaved: (info: { code: string; describe: string }) => void;
}) {
  const [tab, setTab] = useState<IntradayMode>(mode);
  const [catalog, setCatalog] = useState<IntradayFactorCatalog | null>(null);
  const [live, setLive] = useState<WeightProfile | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);

  // 表单：weights/thresholds/levels 都只放**当前模式的键**
  const [weights, setWeights] = useState<Record<string, number>>({});
  const [thresholds, setThresholds] = useState({ action: 30, hint: 20 });
  const [levels, setLevels] = useState<Record<string, number | boolean>>({});
  /**
   * 基线：与本票**当前生效**的口径取差集，保证只提交被改过的项。
   *
   * 为什么不能拿全局 `catalog.current_weights` 当基线：已有档案的票，生效权重
   * 本来就和全局不同 —— 那样会把"没动过的项"全部当成改动提交，等于每存一次就把
   * 档案从稀疏差分膨胀成全量快照，以后调全局权重这只票再也不跟着变。
   */
  const [baselineWeights, setBaselineWeights] = useState<Record<string, number>>({});
  const [baselineLevels, setBaselineLevels] = useState<Record<string, number | boolean>>({});
  const [baselineThresholds, setBaselineThresholds] = useState({ action: 30, hint: 20 });

  const [profiles, setProfiles] = useState<WeightProfile[] | null>(null);
  const [scope, setScope] = useState<string>("");
  const [templateKey, setTemplateKey] = useState("");
  const [note, setNote] = useState("");
  const [sourceTag, setSourceTag] = useState<"manual" | "auto_character">("manual");
  const [showFormula, setShowFormula] = useState(false);
  /**
   * 阈值/档位折叠区：用户手动开合过就听用户的，否则按模式给默认。
   *
   * React 没有 `defaultOpen`（那是原生属性），只能自己记住"用户是否动过" ——
   * 直接写 `open={tab==="intraday"}` 会让每次改一个数字就把折叠区弹回默认状态。
   */
  const [levelOpen, setLevelOpen] = useState<boolean | null>(null);

  const [character, setCharacter] = useState<CharacterProfile | null>(null);
  const [characterLoading, setCharacterLoading] = useState(false);
  const [characterError, setCharacterError] = useState<string | null>(null);

  const [preview, setPreview] = useState<IntradayWeightPreview | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewError, setPreviewError] = useState<string | null>(null);
  /** 预览提交时的表单指纹：之后任何一个数字被改动，预览结果就标为"已过期"。 */
  const [previewStamp, setPreviewStamp] = useState("");
  /** 市场情绪周期（做T环境温度）：涨停家数/炸板率/最高连板是**实时**取的。 */
  const [cycle, setCycle] = useState<MarketCycle | null>(null);

  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);

  /** 当前模式的生效口径（档案 > YAML overrides > 全局）。 */
  const viewFor = useCallback((
    payload: IntradayFactorCatalog, profile: WeightProfile | null,
  ) => {
    const useDaily = payload.mode === "daily";
    const dailyStored = profile?.daily_weights ?? {};
    const overridden = Object.keys(dailyStored).length > 0
      || Object.keys(profile?.daily_thresholds ?? {}).length > 0;
    const baseWeights = useDaily
      ? (overridden ? { ...payload.current_weights, ...dailyStored } : payload.current_weights)
      : (payload.effective_weights ?? payload.current_weights);
    const baseLevels = !useDaily && profile
      ? { ...(payload.levels as Record<string, number | boolean>),
          ...(profile.levels ?? {}) }
      : {};
    const baseThresholds = useDaily && profile?.daily_thresholds
      && Object.keys(profile.daily_thresholds).length > 0
      ? { ...payload.thresholds, ...profile.daily_thresholds }
      : payload.thresholds;
    return { weights: baseWeights, thresholds: baseThresholds, levels: baseLevels };
  }, []);

  /** 拉因子目录 + 该票档案，并把表单重置到「当前生效口径」。 */
  useEffect(() => {
    let alive = true;
    setLoading(true);
    setLoadError(null);
    (async () => {
      try {
        const [payload, detail] = await Promise.all([
          api.intradayFactors(tab, code),
          api.intradayWeightProfile(code).catch(() => null),
        ]);
        if (!alive) return;
        const profile = detail?.profile ?? null;
        const view = viewFor(payload, profile);
        setCatalog(payload);
        setLive(profile);
        setWeights({ ...view.weights });
        setBaselineWeights({ ...view.weights });
        setThresholds({ ...view.thresholds });
        setBaselineThresholds({ ...view.thresholds });
        setLevels({ ...view.levels });
        setBaselineLevels({ ...view.levels });
        setNote(profile?.note ?? "");
        setSourceTag(profile?.source === "auto_character" ? "auto_character" : "manual");
        setScope(payload.override_source
          || (profile ? `档案（${profile.describe}）` : "全局口径"));
        setTemplateKey(profile?.template ?? "");
        setCharacter(null);
        setCharacterError(null);
        setPreview(null);
        setPreviewError(null);
        setSaveError(null);
      } catch (exc) {
        if (alive) setLoadError(humanizeError(exc));
      } finally {
        if (alive) setLoading(false);
      }
    })();
    return () => { alive = false; };
  }, [tab, code, viewFor]);

  // 档案库列表（下拉入口）
  useEffect(() => {
    let alive = true;
    api.intradayWeightProfiles()
      .then((data) => { if (alive) setProfiles(data.profiles); })
      .catch(() => { if (alive) setProfiles([]); });
    return () => { alive = false; };
  }, []);

  /**
   * 市场情绪周期：涨停家数 / 炸板率 / 最高连板 → 阶段与做T环境温度。
   *
   * 这三项是**实时**从东财涨停池/炸板池取的（服务端 60 秒 TTL 缓存），不是写死的
   * 常量。把它摆在权重表旁边是有必要的：「市场情绪周期」这个因子的得分完全由
   * 温度映射而来（温度 100→+1、50→0、0→−1），不看到原始计数就无法判断这个分数
   * 是否合理；退潮/冰点期还会一票否决回踩。
   */
  const loadCycle = useCallback(async (force = false) => {
    try {
      setCycle(await api.intradayMarketCycle(force));
    } catch {
      setCycle(null);   // 情绪周期取不到不影响权重编辑主流程
    }
  }, []);

  useEffect(() => {
    if (tab !== "intraday") return;
    void loadCycle();
    const timer = window.setInterval(() => void loadCycle(), 60000);
    return () => window.clearInterval(timer);
  }, [tab, loadCycle]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  // 服务端目录原样使用（不重排、不改名），只做去重兜底
  const factors = useMemo<FactorMetaItem[]>(() => {
    const seen = new Set<string>();
    return (catalog?.factors ?? []).filter((item) => {
      if (seen.has(item.key)) return false;
      seen.add(item.key);
      return true;
    });
  }, [catalog]);

  // 按服务端给的分组顺序渲染（分组名与顺序一律不写死）
  const grouped = useMemo(() => {
    const order = catalog?.groups ?? [];
    const map = new Map<string, { label: string; items: FactorMetaItem[] }>();
    order.forEach((group) => map.set(group.key, { label: group.label, items: [] }));
    factors.forEach((item) => {
      const bucket = map.get(item.group);
      if (bucket) bucket.items.push(item);
      else map.set(item.group, { label: item.group_label, items: [item] });
    });
    return [...map.values()].filter((group) => group.items.length > 0);
  }, [catalog, factors]);

  const levelRows: LevelRow[] = useMemo(() => {
    if (tab !== "intraday") return [];
    return Object.keys(levels)
      .filter((key) => !BOOLEAN_LEVEL_KEYS.has(key))
      .map((key) => {
        const known = LEVEL_FIELDS.find((item) => item.key === key);
        if (known) return known;
        // 服务端多给了数值档位键（例如新增参数）：仍渲染出来，但按通用区间兜住
        return {
          key, label: `${key}（服务端提供的档位项）`, hint: "该档位项由服务端返回，暂无中文说明",
          min: 0, max: 100, step: 0.1,
        };
      })
      .sort((a, b) => {
        const ia = INTRADAY_LEVEL_ORDER.indexOf(a.key);
        const ib = INTRADAY_LEVEL_ORDER.indexOf(b.key);
        return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib) || (a.key < b.key ? -1 : 1);
      });
  }, [levels, tab]);

  /** 只读展示的布尔开关档位（可编辑会让用户点了就 422）。 */
  const booleanLevels = useMemo(
    () => Object.keys(levels).filter((key) => BOOLEAN_LEVEL_KEYS.has(key)),
    [levels]);

  const weightSum = useMemo(() => {
    const total = factors.reduce((acc, item) => acc + (weights[item.key] || 0), 0);
    return round1(total);
  }, [factors, weights]);
  const sumOk = Math.abs(weightSum - 100) < 1e-6;

  const patchWeights = useMemo(() => {
    const diff: Record<string, number> = {};
    factors.forEach((item) => {
      const value = weights[item.key] || 0;
      const base = baselineWeights[item.key] ?? 0;
      if (Math.abs(value - base) > 1e-9) diff[item.key] = round4(value);
    });
    return diff;
  }, [factors, weights, baselineWeights]);

  /** 档位差集：只提交被改过的数值档位键（否则会把全局默认值固化到这只票上）。 */
  const patchLevels = useMemo(() => {
    const diff: Record<string, number> = {};
    Object.keys(levels).forEach((key) => {
      if (BOOLEAN_LEVEL_KEYS.has(key)) return;  // 布尔开关不进数值 diff
      const value = Number(levels[key]);
      const base = Number(baselineLevels[key] ?? 0);
      if (Math.abs(value - base) > 1e-9) diff[key] = round4(value);
    });
    return diff;
  }, [levels, baselineLevels]);

  const patchThresholds = useMemo(() => {
    const diff: Record<string, number> = {};
    (["action", "hint"] as const).forEach((key) => {
      const value = thresholds[key];
      const base = baselineThresholds[key];
      if (Math.abs(value - base) > 1e-9) diff[key] = round4(value);
    });
    return diff;
  }, [thresholds, baselineThresholds]);

  const changedCount = Object.keys(patchWeights).length
    + Object.keys(patchLevels).length + Object.keys(patchThresholds).length;

  /** 贡献提示：沿用本票当前口径时说明「没改」，改了就给出新旧对照。 */
  function contributionHint(item: FactorMetaItem): string {
    const value = weights[item.key] || 0;
    if (Math.abs(value) < 1e-9) return "0 · 该维度不计入总分";
    const base = baselineWeights[item.key] ?? 0;
    const delta = round1(value - base);
    if (delta === 0) return `${value} / 100 分 · 未改动`;
    return `${value} / 100 分 · 原 ${base}（${delta > 0 ? "+" : ""}${delta}）`;
  }

  function updateWeight(key: string, next: number) {
    const value = Number.isFinite(next) ? Math.max(0, Math.min(100, next)) : 0;
    setWeights((prev) => ({ ...prev, [key]: value }));
  }

  function applyTemplate(key: string) {
    setTemplateKey(key);
    const found: WeightTemplate | undefined = catalog?.templates.find(
      (item) => item.key === key);
    if (!found) return;
    setWeights(() => {
      const next: Record<string, number> = {};
      factors.forEach((item) => {
        next[item.key] = found.weights[item.key] ?? 0;
      });
      return next;
    });
    setSaveError(null);
  }

  /**
   * 把某个档案载入表单：以接口给的 **effective（实际生效口径）** 为准。
   *
   * 刻意不用「全局 + 档案稀疏差分」自己拼：档案的稀疏差分只覆盖被改过的键，
   * 一旦全局口径变了（或档案里有已下线的因子键），自己拼出来的值与真正生效的
   * 口径会不一致 —— 而用户看到的就是"载入了但分数对不上"。
   */
  function applyProfileDetail(detail: IntradayWeightProfileDetail) {
    const profile = detail.profile;
    const useDaily = tab === "daily";
    const source = useDaily
      ? detail.effective.daily_weights : detail.effective.weights;
    const next: Record<string, number> = {};
    factors.forEach((item) => { next[item.key] = source[item.key] ?? 0; });
    setWeights(next);
    setBaselineWeights({ ...next });
    setThresholds({ ...detail.effective.thresholds });
    setBaselineThresholds({ ...detail.effective.thresholds });
    if (!useDaily) {
      const nextLevels: Record<string, number | boolean> = {};
      Object.keys(levels).forEach((key) => {
        nextLevels[key] = detail.effective.levels[key] ?? levels[key] ?? 0;
      });
      setLevels({ ...nextLevels, ...detail.effective.levels });
      setBaselineLevels({ ...nextLevels, ...detail.effective.levels });
    }
    setLive(profile);
    setNote(profile?.note ?? "");
    setTemplateKey(profile?.template ?? "");
    setSourceTag(profile?.source === "auto_character" ? "auto_character" : "manual");
    setScope(detail.effective.source);
    setPreview(null);
    setSaveError(null);
  }

  async function loadProfile(code2: string) {
    try {
      const detail = await api.intradayWeightProfile(code2);
      applyProfileDetail(detail);
    } catch (exc) {
      setSaveError(`载入档案失败：${humanizeError(exc)}`);
    }
  }

  async function recommend() {
    setCharacterLoading(true);
    setCharacterError(null);
    try {
      const data = await api.intradayCharacter(code, tab);
      setCharacter(data);
      if (!data.available) {
        setCharacterError(data.gap
          ?? "该票日线样本不足，无法按股性预填（不做任何猜测性填充）");
        return;
      }
      setWeights((prev) => {
        const next = { ...prev };
        Object.keys(data.weights).forEach((key) => {
          if (key in next) next[key] = data.weights[key];
        });
        return next;
      });
      if (tab === "intraday") {
        setLevels((prev) => {
          const next = { ...prev };
          Object.keys(data.levels).forEach((key) => {
            if (key in next) next[key] = data.levels[key];
          });
          return next;
        });
      }
      setTemplateKey(data.template);
      setSourceTag("auto_character");
      setSaveError(null);
    } catch (exc) {
      setCharacterError(`按股性预填失败：${humanizeError(exc)}`);
    } finally {
      setCharacterLoading(false);
    }
  }

  /** 档位表单的**当前**数值（发给服务端的口径；布尔开关不进请求体）。 */
  const numericLevels = useMemo(() => {
    const payload: Record<string, number> = {};
    Object.keys(levels).forEach((key) => {
      if (BOOLEAN_LEVEL_KEYS.has(key)) return;
      payload[key] = Number(levels[key]);
    });
    return payload;
  }, [levels]);

  /** 表单指纹：预览结果与它绑定，改过任何一个数字就说明结果已过期。 */
  const formStamp = useMemo(() => JSON.stringify({
    weights: factors.map((item) => [item.key, weights[item.key] || 0]),
    thresholds: [thresholds.action, thresholds.hint],
    levels: Object.keys(numericLevels).sort().map((key) => [key, numericLevels[key]]),
  }), [factors, weights, thresholds, numericLevels]);

  const previewStale = !!preview && previewStamp !== formStamp;

  async function runPreview() {
    if (!sumOk) {
      setPreviewError(`预览失败：权重合计 ${weightSum} ≠ 100，`
        + "总分刻度依赖它（先点「一键归一化」）");
      return;
    }
    // 预览必须提交**全量权重**（合计恰好 100）：服务端的预览是按「总分=Σ(得分×权重)」
    // 的口径校验的，只发改动项（稀疏差分）会被判成"合计=8"直接 400 ——
    // 那是保存接口的增量语义，预览是"想看看这一套口径算出来多少分"，两者不同。
    const fullWeights: Record<string, number> = {};
    factors.forEach((item) => { fullWeights[item.key] = weights[item.key] || 0; });
    // 「面板此刻」的档位原样回传（服务端拿它当"改动前"那一列）
    const baselineLevels: Record<string, number> = {};
    if (liveLevels) {
      (["low_buy", "high_sell", "stop_loss", "vwap"] as const).forEach((key) => {
        const value = liveLevels[key];
        if (typeof value === "number" && Number.isFinite(value)) {
          baselineLevels[key] = value;
        }
      });
    }
    setPreviewLoading(true);
    setPreviewError(null);
    try {
      const data = await api.intradayPreviewWeights({
        code, mode: tab, weights: fullWeights, thresholds, levels: numericLevels,
        current_levels: baselineLevels,
      });
      setPreview(data);
      setPreviewStamp(formStamp);
    } catch (exc) {
      setPreview(null);
      setPreviewError(`预览失败：${humanizeError(exc)}`);
    } finally {
      setPreviewLoading(false);
    }
  }

  /**
   * 表单一变就自动重算（防抖 800ms）。
   *
   * 为什么不像原来那样只留手动按钮：用户拖滑杆问的是「**现在**回踩线是多少、
   * 还差多少分」，手动预览永远慢一拍 —— 拖完看到的是上一版数字，很容易据此
   * 得出反向结论。代价是每次预览走一遍服务端完整快照（1~3 秒），因此：
   *   - 权重合计不等于 100 时**不自动预览**（那种口径本来就不合法）；
   *   - 首次挂载故意不自动跑（用户可能只是进来看看，不必立刻付这次取数成本）。
   */
  const autoPreviewArmed = useRef(false);
  useEffect(() => {
    if (!sumOk || loading) {
      autoPreviewArmed.current = false;
      return;
    }
    if (!autoPreviewArmed.current) {
      autoPreviewArmed.current = true;
      return;
    }
    const timer = window.setTimeout(() => { void runPreview(); }, 800);
    return () => window.clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [formStamp, sumOk, loading]);

  async function save() {
    if (!sumOk) {
      setSaveError(`保存失败：权重合计 ${weightSum} ≠ 100，`
        + "总分刻度依赖它（先点「一键归一化」）");
      return;
    }
    const anyChange = changedCount > 0;
    if (!anyChange) {
      setSaveError("保存失败：没有任何改动。档案只记录「这只票与全局口径的差异」，"
        + "没有差异就无需保存（否则会把当前全局值固化到这只票上）");
      return;
    }
    setSaving(true);
    setSaveError(null);
    const body: IntradayWeightProfileRequest = {
      name: name ?? "",
      note,
      template: templateKey,
    };
    if (Object.keys(patchWeights).length) {
      if (tab === "intraday") body.weights = patchWeights;
      else body.daily_weights = patchWeights;
    }
    if (Object.keys(patchThresholds).length) {
      if (tab === "intraday") body.thresholds = patchThresholds;
      else body.daily_thresholds = patchThresholds;
    }
    if (tab === "intraday" && Object.keys(patchLevels).length) {
      body.levels = patchLevels;
    }
    /**
     * 未被本次编辑的那一半必须**原样带回**。
     *
     * 后端的保存是整行 upsert（`dim_intraday_profile` 一行的四个 JSON 列
     * 全部按请求体覆盖），所以只提交日线权重会把这票已有的分时权重清空 ——
     * 用户在日线页点一次保存，回到分时页发现口径没了，而且没有任何提示。
     * 这里把库里已有的值读回来原样提交：改动的部分仍是**稀疏差分**，
     * 没改的部分保持它原本的覆盖值，不多写一个键。
     */
    if (tab === "intraday") {
      if (live?.daily_weights && Object.keys(live.daily_weights).length) {
        body.daily_weights = { ...live.daily_weights };
      }
      if (live?.daily_thresholds && Object.keys(live.daily_thresholds).length) {
        body.daily_thresholds = { ...live.daily_thresholds };
      }
    } else {
      if (live?.weights && Object.keys(live.weights).length) {
        body.weights = { ...live.weights };
      }
      if (live?.thresholds && Object.keys(live.thresholds).length) {
        body.thresholds = { ...live.thresholds };
      }
      if (live?.levels && Object.keys(live.levels).length) {
        body.levels = { ...live.levels };
      }
    }
    if (sourceTag === "auto_character") {
      // 档案里存一份「保存那一刻的股性快照」：股性会漂移，
      // 半年后回看这条档案得能知道当时凭什么参数推荐
      body.source = "auto_character";
      body.character_profile = character
        ? {
          code: character.code, name: character.name, grade: character.grade,
          regime: character.regime, t_friendly: character.t_friendly,
          atr_pct: character.atr_pct, template: character.template,
          sampled_days: character.sampled_days,
        }
        : {};
    }
    try {
      const data = await api.intradaySaveWeightProfile(code, body);
      onSaved({ code: data.code, describe: data.describe });
      onClose();
    } catch (exc) {
      setSaveError(`保存失败：${humanizeError(exc)}`);
    } finally {
      setSaving(false);
    }
  }

  async function remove() {
    const label = name ? `${name}(${code})` : code;
    if (!window.confirm(`确认删除 ${label} 的权重档案？\n`
      + "删除后这只票回落到全局口径（YAML overrides 仍生效），该操作不可撤销。")) {
      return;
    }
    setSaving(true);
    setSaveError(null);
    try {
      const data = await api.intradayDeleteWeightProfile(code);
      onSaved({ code: data.code, describe: data.notice });
      onClose();
    } catch (exc) {
      setSaveError(`删除失败：${humanizeError(exc)}`);
    } finally {
      setSaving(false);
    }
  }

  const templates = catalog?.templates ?? [];
  const chosenTemplate = templates.find((item) => item.key === templateKey);
  const previewCard = preview?.scorecard ?? null;
  const previewSum = previewCard
    ? round1(previewCard.factors.reduce((acc, f) => acc + f.contribution, 0))
    : 0;
  const impact = preview?.impact ?? null;
  const levelSet = preview?.level_set ?? null;

  /**
   * 「现在 vs 改动后」逐线对照。
   *
   * 「改动前」那一列由**服务端**给出（`impact.level_deltas`）：它在
   * 「这只票当前生效口径」下把同一条线再算了一遍，因此差异只来自你改的参数与阈值。
   * 前端不自己减 —— 两份档位的口径差异只该由服务端定义一处（见 impact.level_deltas）。
   */
  const levelCompare = useMemo(() => {
    if (!impact || !impact.available) return [];
    const byKey = new Map(impact.level_deltas.map((row) => [row.key, row]));
    return impact.level_rows.map((row) => {
      const delta = byKey.get(row.key);
      return {
        ...row,
        before: delta?.current ?? null,
        delta: delta?.delta ?? null,
        deltaPct: delta?.delta_pct ?? null,
      };
    });
  }, [impact]);

  /** 档位刻度：把三条线（与改动后）画在同一条相对现价的轴上。 */
  const levelScale = useMemo(() => {
    if (!impact || !impact.available || !(impact.price > 0)) return [];
    return levelScalePositions(
      levelCompare
        .filter((row) => ["high_sell", "low_buy", "stop_loss"].includes(row.key))
        .map((row) => ({ key: row.key, price: row.price })),
      impact.price);
  }, [impact, levelCompare]);

  const hitCount = impact ? impact.factor_impact.filter((row) => row.available).length : 0;

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="help-modal weight-editor" onClick={(e) => e.stopPropagation()}>
        <div className="help-head">
          <h2>
            权重编辑 · {name ? `${name} ` : ""}
            <span className="mono">{code}</span>
            <span className="muted-text">（{tab === "intraday" ? "分时" : "日K"}口径）</span>
          </h2>
          <button className="btn-ghost tiny" onClick={onClose}>关闭 (Esc)</button>
        </div>

        <div className="weight-editor-bar">
      <Disclaimer compact />
          <nav className="help-tabs">
            <button className={tab === "intraday" ? "mode-btn active" : "mode-btn"}
                    onClick={() => setTab("intraday")}
                    title="分时做T的14因子混合权重 + ±动手线/提示线 + 回踩冲高档位">
              分时做T（14因子）
            </button>
            <button className={tab === "daily" ? "mode-btn active" : "mode-btn"}
                    onClick={() => setTab("daily")}
                    title="日K级别做T的7因子权重（日线档位由量价体系自算，故无档位表单）">
              日线做T（7因子）
            </button>
          </nav>
          <span className="muted-text">
            当前：{scope || "全局口径（configs/intraday.yaml）"}
          </span>
          <label className="weight-editor-inline">
            载入档案
            <select value=""
                    onChange={(e) => { if (e.target.value) void loadProfile(e.target.value); }}>
              <option value="">
                {profiles === null
                  ? "读取中…"
                  : `选择已保存的档案（${profiles.length}）`}
              </option>
              {(profiles ?? []).map((item) => (
                <option key={item.code} value={item.code}>
                  {item.name || item.code} {item.code} · {item.updated_at || "—"}
                </option>
              ))}
            </select>
          </label>
          <span className="muted-text">
            保存只写入「与全局口径不同的项」，以后改全局这只票会跟着变
          </span>
        </div>

        {loadError && <div className="error-text">口径加载失败：{loadError}</div>}

        <div className="weight-editor-body">
          <section className="weight-editor-col">
            <div className="weight-editor-sum">
              <span className="stat-label">Σ权重</span>
              <b className={`mono weight-editor-total ${sumOk ? "ok" : "bad"}`}>
                {weightSum.toFixed(1)}
              </b>
              <button className="btn-ghost tiny"
                      onClick={() => {
                        const total = factors.reduce(
                          (acc, item) => acc + (weights[item.key] || 0), 0);
                        if (total > 0) setWeights(normalizeTo100(weights));
                      }}
                      title="等比缩放到合计恰好100（最大余额法取整到0.1，保持相对比例，用户刻意置0的项保持0）">
                一键归一化
              </button>
              {!sumOk && (
                <span className="error-text">
                  合计必须=100 才能保存（当前 {weightSum.toFixed(1)}）
                  —— 总分=Σ(得分×权重)，合计120会让 ±动手线/提示线 全部失效
                </span>
              )}
              <button className="btn-ghost tiny"
                      onClick={() => setShowFormula((value) => !value)}>
                {showFormula ? "收起公式" : "显示公式"}
              </button>
            </div>

            {loading && <p className="muted-text">口径加载中…</p>}
            {!loading && !catalog && (
              <p className="muted-text">
                服务端未返回因子目录 —— 拿不到目录就无法编辑（绝不猜测因子名）。
                请先确认后端 /api/v1/intraday/factors 可用。
              </p>
            )}

            {tab === "intraday" && cycle && (
              <div className={cycle.available && !cycle.t_allowed
                ? "warn-box cycle-strip" : "info-box cycle-strip"}>
                {cycle.available ? (
                  <>
                    <b>市场情绪周期：{cycle.stage}</b>
                    <span className="mono">温度 {cycle.temperature}/100</span>
                    <span className="mono">
                      涨停 {cycle.limit_up_count} · 跌停 {cycle.limit_down_count}
                      {" · 炸板 "}{cycle.broken_count}
                      {cycle.broken_rate !== null
                        && `（${(cycle.broken_rate * 100).toFixed(0)}%）`}
                      {" · 最高 "}{cycle.max_streak} 板
                    </span>
                    <span className="muted-text">
                      实时取自涨停池/炸板池（{cycle.fetched_at.slice(11, 19)} 取数，
                      60 秒缓存）——「市场情绪周期」因子的得分就是由温度映射而来
                      （100→+1 / 50→0 / 0→−1），不是写死的常量。
                      {cycle.t_allowed ? "" : "⛔ 当前阶段禁止正式回踩区间提示。"}
                    </span>
                    <button className="btn-ghost tiny" onClick={() => void loadCycle(true)}>
                      刷新
                    </button>
                  </>
                ) : (
                  <span className="muted-text">
                    市场情绪周期不可用：{cycle.gap ?? "数据缺口"}
                    ——该维度会记为缺口并从有效权重中扣除
                  </span>
                )}
              </div>
            )}

            <div className="weight-editor-table-wrap">
              <table className="audit-table compact-table weight-editor-table">
                <thead>
                  <tr>
                    <th>因子维度</th>
                    <th className="num">权重</th>
                    <th>滑杆</th>
                    <th>贡献</th>
                  </tr>
                </thead>
                <tbody>
                  {grouped.map((group) => (
                    <Fragment key={`group-${group.label}`}>
                      <tr className="weight-editor-group">
                        <td colSpan={4}>
                          {group.label}
                          <span className="muted-text">（{group.items.length} 项）</span>
                        </td>
                      </tr>
                      {group.items.map((item) => {
                        const value = weights[item.key] || 0;
                        const base = baselineWeights[item.key] ?? 0;
                        const dimmed = value < (catalog?.min_visible_weight ?? 0.5);
                        return (
                          <tr key={item.key} className={dimmed ? "factor-gap" : ""}>
                            <td className="factor-name">
                              <span className="weight-editor-name">{item.label}</span>
                              {item.from_skill && (
                                <span className="badge-tag"
                                      title={`来自交易技能库：${item.source}`}>
                                  技能
                                </span>
                              )}
                              {item.key === "cycle" && tab === "intraday" && (
                                <span className="badge-tag live-badge"
                                      title={cycle?.available
                                        ? `实时取自东财涨停池/炸板池（${cycle.trade_date}，`
                                          + `60秒缓存）：涨停 ${cycle.limit_up_count} 家 / `
                                          + `炸板 ${cycle.broken_count} 家 / 最高 ${cycle.max_streak} 板 `
                                          + `→ 温度 ${cycle.temperature}/100`
                                        : "实时情绪周期数据当前不可用（该维度会记为缺口）"}>
                                  实时
                                </span>
                              )}
                              <span className="mono muted-text weight-editor-key">
                                {item.key}
                              </span>
                              {Math.abs(base) > 1e-9 && (
                                <span className="muted-text">原 {base}</span>
                              )}
                              {showFormula && (
                                <div className="muted-text weight-editor-formula">
                                  {item.formula}
                                  <br />数据来源：{item.source}
                                </div>
                              )}
                            </td>
                            <td className="num">
                              <input className="weight-editor-num mono" type="number"
                                     step={0.5} min={0} max={100} value={value}
                                     aria-label={`${item.label} 权重`}
                                     onChange={(e) => updateWeight(item.key, Number(e.target.value))} />
                            </td>
                            <td>
                              <input className="weight-editor-range" type="range"
                                     step={0.5} min={0} max={50} value={Math.min(value, 50)}
                                     aria-label={`${item.label} 权重滑杆`}
                                     onChange={(e) => updateWeight(item.key, Number(e.target.value))} />
                            </td>
                            <td className="muted-text">{contributionHint(item)}</td>
                          </tr>
                        );
                      })}
                    </Fragment>
                  ))}
                </tbody>
                <tfoot>
                  <tr>
                    <td>合计</td>
                    <td className={`num mono ${sumOk ? "" : "weight-editor-total bad"}`}>
                      {weightSum.toFixed(1)}
                    </td>
                    <td colSpan={2} className="muted-text">
                      贡献分逐行相加=总分（口径与面板一致）
                    </td>
                  </tr>
                </tfoot>
              </table>
            </div>

            <details className="weight-editor-levels"
                     open={levelOpen ?? tab === "intraday"}
                     onToggle={(e) => setLevelOpen(e.currentTarget.open)}>
              <summary>
                阈值与档位（{tab === "intraday"
                  ? "动手线 / 提示线 / 回踩冲高档位"
                  : "动手线 / 提示线"}）
              </summary>
              <p className="muted-text">
                动手线=正式信号门槛（实心三角），提示线=软提示门槛（空心三角）。
                档位决定「回踩线/冲高线离现价多远」，与权重无关，但同样按票而异。
              </p>
              <div className="weight-editor-levels-grid">
                <label>
                  动手线 action（|总分|≥该值出正式信号）
                  <input className="mono" type="number" step={1} min={1} max={100}
                         value={thresholds.action}
                         onChange={(e) => setThresholds((prev) => ({
                           ...prev, action: Number(e.target.value),
                         }))} />
                </label>
                <label>
                  提示线 hint（|总分|≥该值出软提示）
                  <input className="mono" type="number" step={1} min={1} max={100}
                         value={thresholds.hint}
                         onChange={(e) => setThresholds((prev) => ({
                           ...prev, hint: Number(e.target.value),
                         }))} />
                </label>
                {levelRows.map((row) => (
                  <label key={row.key} title={row.hint}>
                    {row.label}
                    <span className="mono muted-text"> {row.key}</span>
                    <input className="mono" type="number" step={row.step}
                           min={row.min} max={row.max}
                           value={Number(levels[row.key] ?? 0)}
                           onChange={(e) => setLevels((prev) => ({
                             ...prev, [row.key]: Number(e.target.value),
                           }))} />
                  </label>
                ))}
              </div>
              {tab === "intraday" && booleanLevels.length > 0 && (
                <p className="muted-text">
                  该票口径里还有开关型档位{booleanLevels.map(
                    (key) => ` ${key}=${String(levels[key])}`).join("，")}
                  —— 开关只读展示，不能在面板里改（后端 `levels` 是数值字段，
                  提交布尔的开关会被直接拒绝），需要改请编辑
                  <span className="mono"> configs/intraday.yaml </span>。
                </p>
              )}
              {tab === "daily" && (
                <p className="muted-text">
                  日线模式的档位不在本面板：日K的止损/保护线由量价体系
                  （高量柱实顶实底 + 均线）自算，改这里的数字不会生效。
                </p>
              )}
            </details>
          </section>

          <section className="weight-editor-col">
            <div className="weight-editor-block">
              <h3>权重模板（一键套用整套配方）</h3>
              <div className="weight-editor-inline">
                <select value={templateKey}
                        aria-label="权重模板"
                        onChange={(e) => applyTemplate(e.target.value)}>
                  <option value="">不套用模板（保持当前值）</option>
                  {templates.map((item) => (
                    <option key={item.key} value={item.key}>
                      {item.label}（{item.key}）
                    </option>
                  ))}
                </select>
              </div>
              {chosenTemplate
                ? <p className="muted-text">{chosenTemplate.description}</p>
                : <p className="muted-text">
                  模板是**整套权重配方**（已归一化到100），选中即整体覆盖当前表单；
                  之后可以再逐项微调。
                </p>}
            </div>

            <div className="weight-editor-block">
              <h3>按股性预填（用这只票自己的历史定权重）</h3>
              <div className="weight-editor-inline">
                <button className="btn-ghost"
                        disabled={characterLoading || (character !== null && !character.available)}
                        onClick={() => void recommend()}
                        title="按该股近120~250日的振幅/ATR/涨停基因/趋势效率，推荐一套权重与档位">
                  {characterLoading ? "分析中…" : "按股性预填"}
                </button>
                {character && (
                  <button className="btn-ghost tiny"
                          onClick={() => void recommend()}
                          disabled={characterLoading}
                          title="忽略进程内缓存重新取日线重算">
                    重算
                  </button>
                )}
              </div>
              {character && !character.available && (
                <div className="warn-box">
                  该票日线样本不足，无法按股性预填：
                  {character.gap ?? "数据缺口"}（已禁用该按钮的效果，
                  表单未被改动 —— 不做任何猜测性填充）
                </div>
              )}
              {characterError && <div className="error-text">{characterError}</div>}
              {character && character.available && (
                <>
                  <div className="weight-editor-chips">
                    <span className="stat-chip">
                      <em>做T友好度</em>{character.t_friendly}
                    </span>
                    <span className="stat-chip"><em>股性</em>{character.grade}</span>
                    <span className="stat-chip">
                      <em>形态</em>{REGIME_TEXT[character.regime] ?? character.regime}
                    </span>
                    <span className="stat-chip">
                      <em>ATR</em>{pct(character.atr_pct, 2)}
                    </span>
                    <span className="stat-chip">
                      <em>趋势效率</em>{num(character.trend_efficiency, 2)}
                    </span>
                    <span className="stat-chip">
                      <em>日均振幅</em>{pct(character.avg_amplitude_pct, 2)}
                    </span>
                    <span className="stat-chip">
                      <em>样本</em>{character.sampled_days}日
                    </span>
                  </div>
                  {character.template_label && (
                    <p className="muted-text">
                      预填模板「{character.template_label}」：
                      {character.template_description}
                    </p>
                  )}
                  {character.notes.length > 0 && (
                    <ul className="weight-editor-notes">
                      {character.notes.map((item, index) => (
                        <li key={index}>{item}</li>
                      ))}
                    </ul>
                  )}
                  <p className="muted-text">
                    推荐值只是「起点」：它会填进左侧表单，仍要自己确认后点「保存到数据库」。
                  </p>
                </>
              )}
              {!character && !characterLoading && !characterError && (
                <p className="muted-text">
                  股性画像取不到时，这里会显示缺口原因并禁用按钮 ——
                  权重绝不按「猜」的填。
                </p>
              )}
            </div>

            <div className="weight-editor-block">
              <h3>
                实时价格线影响
                <span className="muted-text">
                  （改动会怎么影响回踩/冲高/止损与触发门槛）
                </span>
              </h3>
              <div className="weight-editor-inline">
                <button className="btn-ghost tiny" disabled={previewLoading}
                        onClick={() => void runPreview()}
                        title="用当前表单的权重/阈值/档位走服务端同一条打分链路重算一次（需1~3秒）">
                  {previewLoading ? "重算中…" : "立即重算"}
                </button>
                <span className="muted-text">
                  改完数字会自动重算（防抖 0.8 秒）；这里的结果与「保存后」同源同口径
                </span>
                {previewStale && <span className="badge-tag warn-badge">表单已改动，待重算</span>}
              </div>

              {impact && impact.available ? (
                <>
                  <div className="weight-editor-chips">
                    <span className="stat-chip">
                      <em>现价</em>{num(impact.price, 2)}
                    </span>
                    <span className="stat-chip">
                      <em>总分</em>{signed(impact.total, 1)}
                    </span>
                    <span className="stat-chip">
                      <em>动手线</em>±{num(impact.threshold_action, 0)}
                    </span>
                    <span className="stat-chip">
                      <em>提示线</em>±{num(impact.threshold_hint, 0)}
                    </span>
                    <span className="stat-chip">
                      <em>有效权重</em>{num(impact.available_weight, 0)}/100
                    </span>
                  </div>

                  {levelScale.length > 0 && (
                    <div className="level-scale">
                      <span className="level-scale-label muted-text mono">
                        相对现价 {num(impact.price, 2)}（左=低 右=高）
                      </span>
                      <div className="level-scale-track">
                        <div className="level-scale-zero" />
                        {levelScale.map((item) => (
                          <div key={item.key}
                               className={`level-scale-mark mark-${item.key}`}
                               style={{ left: `${item.pos}%` }}
                               title={`${LEVEL_TEXT[item.key] ?? item.key}`} />
                        ))}
                      </div>
                    </div>
                  )}

                  <table className="audit-table compact-table">
                    <thead>
                      <tr>
                        <th>价格线</th>
                        <th className="num">改动前</th>
                        <th className="num">改动后</th>
                        <th className="num">变动</th>
                        <th className="num">离现价</th>
                        <th className="num">触发价（触及）</th>
                        <th>由谁决定 / 说明</th>
                      </tr>
                    </thead>
                    <tbody>
                      {levelCompare.map((row) => (
                        <tr key={row.key} className={row.key === "stop_loss" ? "row-stop" : ""}>
                          <td className="factor-name">{row.label}</td>
                          <td className="num mono muted-text">
                            {row.before === null ? "—" : num(row.before, 2)}
                          </td>
                          <td className="num mono"><b>{num(row.price, 2)}</b></td>
                          <td className="num mono" style={{
                            color: row.delta === null || row.delta === 0 ? "var(--muted)"
                              : row.delta > 0 ? "var(--high)" : "var(--low)",
                          }}>
                            {row.delta === null ? "—"
                              : Math.abs(row.delta) < 0.005 ? "0.00 未变"
                              : `${signed(row.delta, 2)}`
                                + (row.deltaPct === null ? "" : `（${signedPct(row.deltaPct)}）`)}
                          </td>
                          <td className="num mono" style={{
                            color: row.distance_pct > 0 ? "var(--high)" : "var(--low)",
                          }}>
                            {signedPct(row.distance_pct)}
                          </td>
                          <td className="num mono">
                            {row.trigger_price === null ? "—"
                              : `${num(row.trigger_price, 2)}`}
                          </td>
                          <td className="muted-text">
                            {row.source}
                            <br />{row.note}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>

                  <p className="muted-text">
                    ⚠️ 口径提醒：这三条线是**时刻量**，随 VWAP / 布林 / 现价每分钟重算，
                    因此「改动前」取的是你打开面板那一刻面板上的值；盘中它会随时间小幅漂移，
                    与你的参数改动无关。另：**因子权重不在这条链路里** ——
                    调权重只会改上面的总分与「还差分」，不会移动这三条线。
                  </p>

                  <table className="audit-table compact-table">
                    <thead>
                      <tr>
                        <th>触发门槛</th>
                        <th>可否动手</th>
                        <th className="num">还差分</th>
                        <th className="num">还差价</th>
                        <th>推导</th>
                      </tr>
                    </thead>
                    <tbody>
                      {impact.gates.map((gate) => (
                        <tr key={gate.key}>
                          <td className="factor-name">{gate.label}</td>
                          <td>
                            <span className={gate.ready ? "gate-ok" : "gate-no"}>
                              {gate.ready ? "✓ 条件已满足" : "✗ 未满足"}
                            </span>
                            {gate.blocked_by && (
                              <span className="muted-text"> ⛔{gate.blocked_by}</span>
                            )}
                          </td>
                          <td className="num mono">
                            {gate.score_need > 0 ? gate.score_need.toFixed(1) : "0"}
                          </td>
                          <td className="num mono">
                            {gate.price_need_pct === null ? "—"
                              : signedPct(gate.price_need_pct)}
                          </td>
                          <td className="muted-text">{gate.note}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>

                  <details className="weight-editor-levels" open>
                    <summary>
                      因子影响度（权重 → 总分；{hitCount} 项可用）
                    </summary>
                    <p className="muted-text">
                      「每 +1 分权重」= 该因子得分：得分正 → 加权**推高**总分；
                      得分负 → 加权**拉低**总分（不是所有维度都加分）。
                      「权重置0」= 关掉这一项后总分的变化。
                      这些数字只影响总分，**不会移动价格线**。
                    </p>
                    <table className="audit-table compact-table">
                      <thead>
                        <tr>
                          <th>因子</th>
                          <th className="num">权重</th>
                          <th className="num">得分</th>
                          <th className="num">贡献分</th>
                          <th className="num">每+1分权重</th>
                          <th className="num">权重置0</th>
                          <th className="num">占有效权重</th>
                        </tr>
                      </thead>
                      <tbody>
                        {impact.factor_impact.map((row) => (
                          <tr key={row.key} className={row.available ? "" : "factor-gap"}>
                            <td className="factor-name">
                              {row.label}
                              <span className="muted-text"> {row.note}</span>
                            </td>
                            <td className="num mono">{row.weight}</td>
                            <td className="num mono">{signed(row.score, 2)}</td>
                            <td className="num mono" style={{
                              color: row.contribution > 0 ? "var(--high)"
                                : row.contribution < 0 ? "var(--low)" : "var(--muted)",
                            }}>
                              {signed(row.contribution, 1)}
                            </td>
                            <td className="num mono" style={{
                              color: row.unit_impact > 0 ? "var(--high)"
                                : row.unit_impact < 0 ? "var(--low)" : "var(--muted)",
                            }}>
                              {signed(row.unit_impact, 2)}
                            </td>
                            <td className="num mono">{signed(row.zero_impact, 1)}</td>
                            <td className="num mono">{pct(row.weight_share_pct, 1)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </details>

                  {impact.notes.length > 0 && (
                    <ul className="weight-editor-notes">
                      {impact.notes.map((note, index) => <li key={index}>{note}</li>)}
                    </ul>
                  )}
                  {impact.examples.length > 0 && (
                    <p className="muted-text">
                      {impact.examples.map((item, index) => (
                        <span key={index}>{index > 0 ? " " : ""}{item}</span>
                      ))}
                    </p>
                  )}
                </>
              ) : (
                <p className="muted-text">
                  {impact && !impact.available
                    ? `暂不可用：${impact.reason}`
                    : "改动后回踩/冲高/止损会落在哪个价位、还差多少分多少价才出信号 —— "
                      + "这里会直接算给你看（改完自动重算，也可点「立即重算」）。"}
                </p>
              )}
            </div>

            <div className="weight-editor-block">
              <h3>预览分数（不保存、不落库）</h3>
              <div className="weight-editor-inline">
                <button className="btn-ghost" disabled={previewLoading}
                        onClick={() => void runPreview()}
                        title="用当前表单的权重走服务端同一条打分链路重算一次总分（需1~3秒）">
                  {previewLoading ? "预览中…" : "预览分数"}
                </button>
                <span className="muted-text">
                  预览与保存后同源同口径（服务端完整快照重算），所以要点几秒
                </span>
              </div>
              {previewError && <div className="error-text">{previewError}</div>}
              {preview && previewCard && (
                <>
                  <div className="weight-editor-preview-head">
                    <span className="stat-label">预览总分</span>
                    <b className={`mono weight-editor-preview-total ${
                      previewCard.total >= 0 ? "up" : "down"}`}>
                      {previewCard.total >= 0 ? "+" : ""}{previewCard.total.toFixed(1)}
                    </b>
                    <span className="badge-tag">{previewCard.zone}</span>
                    <span className="muted-text">
                      {ZONE_TEXT[previewCard.zone] ?? previewCard.zone}
                      {" · "}
                      动手线 ±{previewCard.threshold_action} · 提示线 ±{previewCard.threshold_hint}
                    </span>
                  </div>
                  <p className="muted-text">{previewCard.verdict}</p>
                  <p className="muted-text">{preview.notice}</p>
                  {levelSet && (
                    <p className="mono muted-text">
                      本次口径的档位：回踩 {num(levelSet.low_buy, 2)}
                      （触发 {num(levelSet.low_trigger_price, 2)}）· 冲高 {num(levelSet.high_sell, 2)}
                      （触发 {num(levelSet.high_trigger_price, 2)}）· 止损 {num(levelSet.stop_loss, 2)}
                      {levelSet.band_width_pct !== null && levelSet.band_width_pct !== undefined
                        && ` · 档位差 ${num(levelSet.band_width_pct, 2)}%`}
                    </p>
                  )}
                  {previewCard.available_weight < 70 && (
                    <div className="warn-box">
                      有效权重只有 {previewCard.available_weight}/100（&lt;70）：
                      多个维度存在数据缺口，这种口径下已禁止实心正式信号，
                      总分仅供参考。
                    </div>
                  )}
                  <table className="audit-table compact-table">
                    <thead>
                      <tr>
                        <th>因子</th>
                        <th className="num">权重</th>
                        <th className="num">得分</th>
                        <th className="num">贡献分</th>
                      </tr>
                    </thead>
                    <tbody>
                      {previewCard.factors.map((factor) => (
                        <tr key={factor.key} className={factor.available ? "" : "factor-gap"}>
                          <td className="factor-name">{factor.label}</td>
                          <td className="num mono">{factor.weight}</td>
                          <td className="num mono">
                            {factor.available
                              ? `${factor.score >= 0 ? "+" : ""}${factor.score.toFixed(2)}`
                              : "—"}
                          </td>
                          <td className="num mono" style={{
                            color: factor.contribution > 0 ? "var(--high)"
                              : factor.contribution < 0 ? "var(--low)" : "var(--muted)",
                          }}>
                            {factor.available
                              ? `${factor.contribution >= 0 ? "+" : ""}${factor.contribution.toFixed(1)}`
                              : "—"}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                    <tfoot>
                      <tr>
                        <td>合计</td>
                        <td className="num mono">{previewCard.available_weight}</td>
                        <td className="num muted-text">—</td>
                        <td className="num mono">
                          <b>{previewSum >= 0 ? "+" : ""}{previewSum.toFixed(1)}</b>
                        </td>
                      </tr>
                    </tfoot>
                  </table>
                  {previewCard.gaps.length > 0 && (
                    <div className="muted-text gap-box">
                      数据缺口：{previewCard.gaps.join("；")}
                    </div>
                  )}
                </>
              )}
              {!preview && !previewLoading && !previewError && (
                <p className="muted-text">
                  预览走完整快照链路（服务端重算），因此要比对「改动前/改动后」
                  直接看上面的「实时价格线影响」表即可 —— 它已经给出逐线对照。
                </p>
              )}
            </div>

            <div className="weight-editor-block">
              <h3>保存到数据库</h3>
              <div className="weight-editor-inline">
                <input className="weight-editor-note" value={note}
                       placeholder="可选备注（例如：按三季报后的震荡区间调过箱体权重）"
                       onChange={(e) => setNote(e.target.value)} />
              </div>
              <p className="muted-text">
                本次将提交 {changedCount} 项改动
                {changedCount > 0 && (
                  <>
                    ：{Object.keys(patchWeights).length > 0
                      && `权重 ${Object.keys(patchWeights).join("、")}`}
                    {Object.keys(patchThresholds).length > 0
                      && `；阈值 ${Object.keys(patchThresholds).join("、")}`}
                    {Object.keys(patchLevels).length > 0
                      && `；档位 ${Object.keys(patchLevels).join("、")}`}
                  </>
                )}
                。没改的项不提交 —— 否则会把全局默认值固化到这只票上。
              </p>
              {live && (
                <p className="muted-text">
                  该票已有档案：{live.describe}
                  （{live.source === "auto_character" ? "股性自动预填" : "手工调整"}，
                  更新于 {live.updated_at || "—"}）
                </p>
              )}
              <div className="weight-editor-inline">
                <button className="btn-ghost" disabled={saving || !sumOk}
                        onClick={() => void save()}>
                  {saving ? "保存中…" : "保存到数据库"}
                </button>
                <button className="btn-ghost tiny" disabled={saving}
                        onClick={() => void remove()}
                        title="删除后这只票回落到全局口径（幂等）">
                  删除档案
                </button>
              </div>
              {saveError && <div className="error-text">{saveError}</div>}
              <p className="muted-text">
                保存后立即生效：该票的打分与档位改用这份口径，
                其他标的仍走全局口径（下次打开自动复用，无需重启服务）。
              </p>
            </div>
          </section>
        </div>

        <div className="help-foot muted-text">
          权重合计必须=100（总分刻度依赖它）；档案是「这只票相对全局口径的差异」，
          因此只提交改动项；预览不落库。因子清单、中文名、分组与公式均由服务端目录提供，
          前端不写死任何一项。
          <br />
          ⚠️ 两条链路要分开读：**因子权重只影响总分**（够不够格动手），
          **档位参数只影响回踩/冲高/止损三条线**（在哪个价位动手）——
          调权重不会让价格线移动一分钱。操作细节见
          <span className="mono"> docs/INTRADAY_T_OPERATION_GUIDE.md</span>。
        </div>
      </div>
    </div>
  );
}
