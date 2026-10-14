/**
 * 「板块」列的**筛选判据**（纯函数，无 React、可单独自证）。
 *
 * ## 为什么单独一个文件
 *
 * 用户 2026-10-14 原话：
 *
 * > 「给高度拥挤告警的数据面板中，"板块"这列加一个检索过滤功能，当前板块太多了，
 * >   想找其中一个板块要翻好久，支持联想或模糊搜索」
 *
 * 清单里 262 个概念板块，靠滚动找一个确实要翻很久。匹配这件事看着简单、
 * 写错却都是**静默的**：大小写、空格、带后缀的代码、没装 pypinyin 时字段为空……
 * 所以把它抽成纯函数放进独立模块，用 `npm run check:boardsearch` 直接喂输入看输出
 * ——而不是靠"源码里有这个字串"。
 *
 * ## 三路匹配（任一命中即可）
 *
 * | 用户输入 | 命中方式 |
 * |---|---|
 * | `化工` | **名称子串**（`化工原料` 也命中） |
 * | `885959` / `885959.ti` | **代码子串**（后端给的是 `885959.TI`，大小写不敏感） |
 * | `hg` | **拼音首字母**（`化工` → `HG`；由后端 `to_initials()` 现算） |
 *
 * ⚠️ 只有**首字母**，没有全拼：板块名没有全拼字典（`pypinyin` 在 Python 侧，
 * 前端没有它的产物）。要全拼得让后端把 `pinyin_full` 一起下发 —— 那是另一件事，
 * 需要时再做（别在前端悄悄造一份拼音表：那就是"同一判断两份实现"）。
 *
 * ## 多关键词 = AND
 *
 * 空格分隔的多个词**全部命中**才算（`半导 材料` 能筛出「半导体材料」，
 * 而 `半导 白酒` 什么都不出）—— 这是"缩小范围"的直觉用法。
 */

/** 一行板块（只需要这三个字段，与 `CrowdingListItem` 结构兼容）。 */
export type BoardSearchTarget = {
  sector_code?: string;
  sector_name?: string;
  /** 后端算好的拼音首字母（大写）；缺 `pypinyin` 时是空串。 */
  pinyin_initials?: string;
};

/** 查询串 → 命中吗？（空查询 = 全通过，即"没在筛"） */
export function matchBoard(target: BoardSearchTarget, query: string): boolean {
  const tokens = String(query ?? "").trim().toLowerCase().split(/\s+/)
    .filter(Boolean);
  if (tokens.length === 0) return true;

  const name = String(target?.sector_name ?? "").toLowerCase();
  const code = String(target?.sector_code ?? "").toLowerCase();
  const initials = String(target?.pinyin_initials ?? "").toLowerCase();

  return tokens.every((token) =>
    name.includes(token)
    || code.includes(token)
    // 拼音字段为空（缺库 / 老载荷）时**不参与**匹配 —— 否则 `includes("")`
    // 恒真，会把"没算拼音"伪装成"全都命中"，那是最糟的一种假绿。
    || (initials !== "" && initials.includes(token)));
}

/** 过滤一个列表；返回新数组（不改入参）。 */
export function filterBoards<T extends BoardSearchTarget>(
  rows: readonly T[], query: string): T[] {
  const text = String(query ?? "").trim();
  if (!text) return [...rows];
  return rows.filter((row) => matchBoard(row, text));
}
