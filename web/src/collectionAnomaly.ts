/**
 * ★ 采集异常的用户侧过滤（2026-09-30 用户口径）。
 *
 * > 「前端显示"部分节点异常、查询超过 10 秒防撞钟"，这类信息异常信息，
 * >  **不要显示在用户界面**。要记录并显示到管理员界面的"运行指标"里，
 * >  可以加一块**数据采集异常展示区**。」
 *
 * ## 为什么要有这个"唯一判定处"
 *
 * "哪些属于内部技术信息"这件事原先**散在两个展示点**（`AgentTimeline` 的
 * 「部分节点异常」列表与 `App` 的任务错误块）。同一件事写在 N 处 ⇒ 必然漂移
 * （本项目实测过：功能 key 写三处、只改一处 → 情报 5 个端点对所有人 403）。
 * 所以判定只在这一个文件里，两处都 import 它。
 *
 * ## 判据（机器可读的固定前缀，**不认自由文案**）
 *
 * 后端产生这些行的地方都带固定标记：
 *   · `A01_data_collector(...)` —— 采集缺口（`collection_gap_note` 的前缀）
 *   · `⏱️` / `[防撞钟]` —— 防撞钟终止
 *   · `无连接器支持指标` / `已注册:` —— 连接器注册表 dump
 *   · `超时未取到` / `该实体无值` —— 取数侧结论
 * 命中任一条 ⇒ 属于**运维信息**，用户界面不显示、管理员界面显示。
 */

/** 技术性/运维性异常行的固定标记（**只认机器可读的标识**）。 */
const TECHNICAL_MARKERS: readonly string[] = [
  "A01_data_collector(",
  "[采集缺口]",
  "[防撞钟]",
  "⏱️",
  "防撞钟",
  "无连接器支持指标",
  "已注册:",
  "超时未取到",
  "该实体无值",
  "该口径对本主体不适用",
];

/** 这一行是不是"内部技术信息"（用户不该看到）。 */
export function isTechnicalAnomaly(text: string): boolean {
  const t = String(text ?? "");
  return TECHNICAL_MARKERS.some((m) => t.includes(m));
}

/**
 * 用户界面**可见**的异常行。
 *
 * 刻意返回数组（而不是在调用点 `.filter(...)` 各写一次）：调用点复制判据
 * 就是"同一判断两份实现"的开始。
 */
export function visibleAnomalies(errors: readonly string[]): string[] {
  return (errors ?? []).filter((e) => !isTechnicalAnomaly(e));
}

/** 被折叠掉的条数（给"已在后台记录"那句话用，不泄露具体内容）。 */
export function hiddenAnomalyCount(errors: readonly string[]): number {
  return (errors ?? []).filter((e) => isTechnicalAnomaly(e)).length;
}
