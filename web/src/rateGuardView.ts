/**
 * 免费档限流熔断一行的**展示口径**（纯函数，便于独立验收）。
 *
 * 为什么把它单独抽出来：这一行是「siliconflow 被静默限流」**唯一**的可见面 ——
 * `light` 层（占 82% 调用量）首位是免费档，它被限流时降级链会自动前进到
 * 下一跳。没有这一行，整个过程只能从"延迟上升 + 下一跳配额被多吃"上后知后觉。
 *
 * 抽成纯函数的第二个理由：组件里没法用单测钉住文案口径，而**这一行的
 * 三种状态必须能被断言**（见下方三条口径）。
 *
 * 三条口径（都来自 AGENTS.md 的 AI 首轮硬约束）：
 *  ① **「没量到」与「量到 0」必须分开**：`available === false` → "未量到"，
 *     绝不用「0 次限流」糊过去（那是假绿）；
 *  ② 锁定中的原因必须是**人话 + 剩余时间**（"还剩 9 分 12 秒"），
 *     不是枚举值、也不是裸秒数；
 *  ③ **零样本 ≠ 无异常**：连一条记录都没有时显示"无记录"，不显示"正常"。
 */
import type { RateLimitGuard } from "./api";

export type RateGuardView = {
  /** badge 的样式类（run-success | run-failed | run-skipped） */
  tone: string;
  /** badge 文案（≤4 字） */
  text: string;
  /** 说明列（人话 + 剩余时间 + 计数口径） */
  detail: string;
};

/** 秒 → "9 分 12 秒" / "42 秒"。 */
export function humanRemaining(secs: number): string {
  const mins = Math.floor(secs / 60);
  const rest = Math.round(secs - mins * 60);
  return mins > 0 ? `${mins} 分 ${rest} 秒` : `${rest} 秒`;
}

export function rateGuardView(g?: RateLimitGuard): RateGuardView {
  if (!g) {
    return {
      tone: "run-skipped",
      text: "未上报",
      detail: "后端未返回该字段（旧版本？）—— 不能视为「没有限流」",
    };
  }
  if (!g.available) {
    return {
      tone: "run-skipped",
      text: "未量到",
      detail: `读不到状态文件 —— 不能当作「没有限流」${g.error ? `：${g.error}` : ""}`,
    };
  }
  const models = Object.entries(g.models ?? {});
  const locked = models.filter(([, v]) => v.locked);
  const totalHits = models.reduce((n, [, v]) => n + v.total_429, 0);

  if (locked.length > 0) {
    const [name, v] = locked[0];
    const more = locked.length > 1 ? `（另有 ${locked.length - 1} 个也在锁定中）` : "";
    return {
      tone: "run-failed",
      text: "已锁定",
      detail: `${name} 连续 ${g.threshold ?? "?"} 次限流 → 跳过该跳，`
        + `还剩 ${humanRemaining(v.remaining_s ?? 0)}${more}`
        + `（期间调用直接走下一跳，不再白撞 429）`,
    };
  }
  if (models.length === 0) {
    return {
      tone: "run-skipped",
      text: "无记录",
      detail: "尚未有免费档调用（不是「没有限流」，是还没有样本）",
    };
  }
  return {
    tone: "run-success",
    text: "正常",
    detail: `无模型处于锁定；累计命中共 ${totalHits} 次`
      + `（连续 ${g.threshold ?? "?"} 次即锁 ${g.lock_minutes ?? "?"} 分钟）`,
  };
}
