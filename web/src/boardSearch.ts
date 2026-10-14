/**
 * 「板块」列的**检索判据 + 联想排序**（纯函数，无 React、可单独自证）。
 *
 * ## 为什么单独一个文件
 *
 * 用户 2026-10-14 原话：
 *
 * > 「给高度拥挤告警的数据面板中，"板块"这列加一个检索过滤功能，当前板块太多了，
 * >   想找其中一个板块要翻好久，支持联想或模糊搜索」
 *
 * 随后追加（同一天）：
 *
 * > 「1.没有全拼，你就把当前拥挤度数据清单里，所有板块字段的板块名称，都映射出
 * >   中文拼音全称字段，然后不就有了搜索联想拼音池了吗」
 *
 * 清单里 262 个概念板块，靠滚动找一个确实要翻很久。匹配这件事看着简单、
 * 写错却都是**静默的**：大小写、空格、带后缀的代码、没装 pypinyin 时字段为空……
 * 所以把它抽成纯函数放进独立模块，用 `npm run check:boardsearch` 直接喂输入看输出
 * ——而不是靠"源码里有这个字串"。
 *
 * ## 四路匹配（任一命中即可）
 *
 * | 用户输入 | 命中方式 |
 * |---|---|
 * | `化工` | **名称子串**（`化工原料` 也命中） |
 * | `885959` / `885959.ti` | **代码子串**（后端给的是 `885959.TI`，大小写不敏感） |
 * | `hg` | **拼音首字母**（`化工` → `HG`；后端 `to_initials()` 现算） |
 * | `huagong` | **拼音全拼**（`化工原料` → `huagongyuanliao`；后端 `to_full_pinyin()`） |
 *
 * 全拼是**无分隔**的连写，所以 `bandao chanpin` 这种带空格的输入也能靠
 * 下面的"空格 = AND"命中（两个片段各自都是子串）。
 *
 * ⚠️ 全拼与首字母**都只是字符串包含**：`hg` 会同时命中「化工」和「海关」。
 * 这对**筛选器**是可接受的（多出来的是可读候选，用户一眼能挑），
 * 但**不可以**直接拿去当"自动归属"的判据 —— 仓库里
 * `src/infrastructure/catalog/synonym_dict.py:142` 就明确拒绝把拼音首字母
 * 收进同义词表（5568 个名称里 `zsyh` 撞车招商银行/招商证券…）。
 * **同一个字符串匹配，代价取决于谁消费。**
 *
 * ## 多关键词 = AND
 *
 * 空格分隔的多个词**全部命中**才算（`半导 材料` 能筛出「半导体材料」，
 * 而 `半导 白酒` 什么都不出）—— 这是"缩小范围"的直觉用法。
 */

/** 一行板块（只需要这几个字段，与 `CrowdingListItem` 结构兼容）。 */
export type BoardSearchTarget = {
  sector_code?: string;
  sector_name?: string;
  /** 后端算好的拼音首字母（大写）；缺 `pypinyin` 时是空串。 */
  pinyin_initials?: string;
  /** 后端算好的拼音全拼（小写连写）；缺 `pypinyin` 时是空串。 */
  pinyin_full?: string;
};

/** 一行里可以被检索的四个字段（小写化 + 去空白后的形态）。 */
type Fields = {
  name: string;
  code: string;
  initials: string;
  full: string;
};

/** 去空白：让 `hua gong` 与 `huagong`、`半导 材料` 与 `半导体材料` 等价。 */
function strip(text: unknown): string {
  return String(text ?? "").replace(/\s+/g, "").toLowerCase();
}

function fieldsOf(target: BoardSearchTarget): Fields {
  return {
    name: strip(target?.sector_name),
    code: strip(target?.sector_code),
    initials: strip(target?.pinyin_initials),
    full: strip(target?.pinyin_full),
  };
}

/** 查询串 → 词列表（空查询 = 空数组 = "没在筛"）。 */
function tokensOf(query: string): string[] {
  return String(query ?? "").trim().toLowerCase().split(/\s+/).filter(Boolean);
}

/**
 * 字段命中吗？
 *
 * ⚠️ **空字段不参与匹配**：`"".includes("")` 恒真（`includes("")` 对任何字符串
 * 都是 `true`），于是"没算拼音 / 没写名称"会被伪装成"全部命中" —— 敲 `hg`
 * 一个都不筛掉，用户只会以为筛选坏了。这是本模块最需要防的**假绿**，
 * 所以每个字段都先判非空。
 */
function hit(field: string, token: string): boolean {
  return field !== "" && field.includes(token);
}

/** 查询串 → 命中吗？（空查询 = 全通过，即"没在筛"） */
export function matchBoard(target: BoardSearchTarget, query: string): boolean {
  const tokens = tokensOf(query);
  if (tokens.length === 0) return true;
  const f = fieldsOf(target);
  return tokens.every((token) =>
    hit(f.name, token) || hit(f.code, token)
    || hit(f.initials, token) || hit(f.full, token));
}

/** 过滤一个列表；返回新数组（不改入参）。 */
export function filterBoards<T extends BoardSearchTarget>(
  rows: readonly T[], query: string): T[] {
  const text = String(query ?? "").trim();
  if (!text) return [...rows];
  return rows.filter((row) => matchBoard(row, text));
}

/**
 * 联想排序的**档位**（数字越小越靠前）。
 *
 * 顺序按"用户最可能想要的那一个"排：**名称**优先于**代码**优先于**拼音**
 * —— 因为敲中文名片段的人多半想直接找到那个板块，而敲拼音的人是在
 * "记不住全名"的情况下试探，两者命中同一个板块时应当一致。
 * 同一档内**保持调用方给的顺序**（`Array.prototype.sort` 是稳定的），
 * 而调用方给的是面板当前的排序顺序（默认按拥挤度水位），
 * 于是"拼音撞车"时先看到的是更拥挤的那个 —— 正是这个面板的语境。
 */
function rankOf(f: Fields, probe: string): number {
  // ⚠️ 这里**不能**写"查询串等于它自己"那种判断（`raw === probe`）——
  //    它对每一行都成立，于是所有候选并列第 0 档，排序退化成"清单顺序"。
  //    第一版就是这么写的，`check:boardsearch` 里"完全等于查询的排最前"
  //    那条当场把它抓出来了。要判的是**字段**等于查询，不是一个常量。
  if (f.name === probe) return 0;
  if (f.code === probe) return 0;              // 直接敲了完整代码
  if (f.name.startsWith(probe)) return 1;
  if (f.name.includes(probe)) return 2;
  if (f.initials === probe) return 3;
  if (f.initials.startsWith(probe)) return 4;
  if (f.full.startsWith(probe)) return 5;
  if (f.code.startsWith(probe)) return 6;
  if (f.full.includes(probe)) return 7;
  if (f.initials.includes(probe)) return 8;
  if (f.code.includes(probe)) return 9;
  return 10;                                    // 只在多词场景走到这里
}

/**
 * 联想候选：名称 / 代码 / 拼音四路命中，按 `rankOf` 排序，最多 `limit` 条。
 *
 * ★ **不变式：候选集必须是筛选结果的子集。**
 * 否则会出现"下拉里点了它，列表里却没有它"——比没有联想更糟。
 * 所以这里**先**用 `matchBoard` 过滤，**再**排序；排序只重排、不新增。
 * `npm run check:boardsearch` 里有一条专门盯着这个不变式（含多词场景）。
 *
 * 空查询返回**空数组**（不是全部）：没输入时弹一个 262 项的下拉是噪音，
 * 而筛选框空着本来就等于"没筛"。
 */
export function suggestBoards<T extends BoardSearchTarget>(
  rows: readonly T[], query: string, limit = 8): T[] {
  const text = String(query ?? "").trim();
  if (!text) return [];
  const probe = strip(text);
  const scored: { row: T; rank: number; index: number }[] = [];
  rows.forEach((row, index) => {
    if (!matchBoard(row, text)) return;
    scored.push({ row, index, rank: rankOf(fieldsOf(row), probe) });
  });
  scored.sort((a, b) => (a.rank - b.rank) || (a.index - b.index));
  const top = Math.max(0, Math.floor(limit));
  return scored.slice(0, top).map((item) => item.row);
}

/**
 * 候选行右侧显示的"为什么命中"提示（拼音池是给人看的，不然没人知道能敲拼音）。
 *
 * 返回空串 = 这个板块没有拼音（缺 `pypinyin`），调用方不要显示空的 `[]`。
 */
export function pinyinHint(target: BoardSearchTarget): string {
  const initials = String(target?.pinyin_initials ?? "");
  const full = String(target?.pinyin_full ?? "");
  if (!initials && !full) return "";
  if (!full) return initials;
  if (!initials) return full;
  return `${initials} · ${full}`;
}
