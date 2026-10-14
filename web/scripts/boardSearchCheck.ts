/**
 * `boardSearch` 的**行为**自证（与 `check:mainline` / `check:rateguard` 同款）。
 *
 * 为什么不让 pytest 只断言源码结构：结构判据是**形状判据** ——
 * 把 `includes` 写成 `startsWith`、把大小写转换删掉、把"拼音为空"那条
 * 短路去掉，源码里的字串照样在，测试照样绿。这里喂真输入看真输出。
 *
 * 覆盖"写错就静默退化"的情形：
 *   ① 名称子串（含中文中间的片段）  ② 代码子串（带/不带 `.TI`、大小写）
 *   ③ 拼音首字母  ④ 拼音**全拼**（用户 2026-10-14 追加："映射出中文拼音全称字段"）
 *   ⑤ 拼音字段为空**不许**变成"全命中"  ⑥ 多关键词 = AND
 *   ⑦ 空查询 = 不筛（原样返回，且不是同一引用）
 *   ⑧ 联想排序 + ★**候选集必须是筛选结果的子集**（点得到就必须筛得到）
 *
 * 跑法：`npm run check:boardsearch`
 */
import {
  filterBoards, matchBoard, pinyinHint, suggestBoards, type BoardSearchTarget,
} from "../src/boardSearch";

let failed = 0;
function ok(cond: boolean, label: string) {
  if (cond) {
    console.log(`  ✅ ${label}`);
  } else {
    failed += 1;
    console.error(`  ❌ ${label}`);
  }
}

/** 真实形态的数据（名称/代码/拼音都按后端 `/config_list` 的实测值填）。 */
const HUA_GONG: BoardSearchTarget = {
  sector_code: "885959.TI", sector_name: "化工原料",
  pinyin_initials: "HG", pinyin_full: "huagongyuanliao",
};
const BAN_DAO: BoardSearchTarget = {
  sector_code: "885800.TI", sector_name: "半导体材料",
  pinyin_initials: "BDTCL", pinyin_full: "bandaoticailiao",
};
const BAI_JIU: BoardSearchTarget = {
  sector_code: "885530.TI", sector_name: "白酒",
  pinyin_initials: "BJ", pinyin_full: "baijiu",
};
/** 与「化工原料」拼音撞车的干扰项（首字母同为 HG）—— 用来验证"只多看几个候选" */
const HUA_GONG_2: BoardSearchTarget = {
  sector_code: "885960.TI", sector_name: "化工机械",
  pinyin_initials: "HG", pinyin_full: "huagongjixie",
};
const NO_PY: BoardSearchTarget = {
  sector_code: "885001.TI", sector_name: "无拼音板块",
  pinyin_initials: "", pinyin_full: "",
};
const ALL = [HUA_GONG, BAN_DAO, BAI_JIU, HUA_GONG_2, NO_PY];

console.log("板块筛选（boardSearch）行为自证");
console.log("─".repeat(70));

// ① 名称子串
ok(matchBoard(HUA_GONG, "化工"), "① 名称全词命中");
ok(matchBoard(BAN_DAO, "半导"), "① 名称前缀命中");
ok(matchBoard(BAN_DAO, "体材料"), "① 名称**中间**片段也命中（不是 startsWith）");
ok(!matchBoard(BAI_JIU, "化工"), "① 不相关的不会误命中");

// ② 代码
ok(matchBoard(HUA_GONG, "885959"), "② 代码（不带后缀）命中");
ok(matchBoard(HUA_GONG, "885959.ti"), "② 代码（小写带后缀）命中");
ok(matchBoard(HUA_GONG, "885959.TI"), "② 代码（原样）命中");
ok(matchBoard(HUA_GONG, ".ti"), "② 后缀也能当子串用");

// ③ 拼音首字母
ok(matchBoard(HUA_GONG, "hg"), "③ 拼音首字母（小写）命中");
ok(matchBoard(HUA_GONG, "HG"), "③ 拼音首字母（大写）命中");
ok(matchBoard(BAN_DAO, "bdtcl"), "③ 多字首字母命中");
ok(!matchBoard(BAI_JIU, "hg"), "③ 拼音不相关的不误命中");

// ④ ★ 拼音全拼（2026-10-14 用户追加的"拼音池"）
ok(matchBoard(HUA_GONG, "huagong"), "④ 全拼前缀命中（敲一半就行）");
ok(matchBoard(HUA_GONG, "huagongyuanliao"), "④ 全拼整串命中");
ok(matchBoard(HUA_GONG, "yuanliao"), "④ 全拼**中间**片段命中（与名称同款语义）");
ok(matchBoard(BAN_DAO, "ban dao"), "④ 全拼带空格 = 两个片段 AND（无音节分隔也能敲）");
ok(!matchBoard(BAI_JIU, "huagong"), "④ 全拼不相关的不误命中");
ok(!matchBoard(HUA_GONG, "huagongyuanliaoX"), "④ 尾巴多打一个字就不再命中");

// ⑤ ★ 拼音为空时**不许**变成全命中（两个字段都要守）
ok(!matchBoard(NO_PY, "hg"), "⑤ 拼音为空的行不会因为 'hg' 而命中");
ok(!matchBoard(NO_PY, "huagong"), "⑤ 拼音为空的行不会因为全拼而命中");
ok(matchBoard(NO_PY, "无拼音"), "⑤ 拼音为空的行仍可按名称命中");
// 反向：**只有全拼为空**（旧载荷 / 只下发了首字母）时，全拼也不许变成全命中
const ONLY_INITIALS: BoardSearchTarget = {
  sector_code: "885700.TI", sector_name: "军工", pinyin_initials: "JG",
};
ok(!matchBoard(ONLY_INITIALS, "jungong"), "⑤ 只缺全拼时，全拼查询不命中（不是全通过）");
ok(matchBoard(ONLY_INITIALS, "jg"), "⑤ 只缺全拼时，首字母照常命中");

// ⑥ 多关键词 = AND
ok(matchBoard(BAN_DAO, "半导 材料"), "⑥ 两个词都命中 → 通过");
ok(!matchBoard(BAN_DAO, "半导 白酒"), "⑥ 其中一个不命中 → 不通过");
ok(matchBoard(BAN_DAO, "  bdtcl   半导  "), "⑥ 多余空格不影响（AND）");

// ⑦ 空查询
ok(matchBoard(HUA_GONG, ""), "⑦ 空串 = 不筛（全通过）");
ok(matchBoard(HUA_GONG, "   "), "⑦ 纯空格 = 不筛");
const out = filterBoards(ALL, "");
ok(out.length === ALL.length, "⑦ 空查询返回全部");
ok(out !== ALL, "⑦ 返回的是新数组（不改入参）");
ok(filterBoards(ALL, "hg").length === 2, "⑦ 组合：'hg' 筛出 2 个（首字母撞车）");
ok(filterBoards(ALL, "ti").length === 5, "⑦ 组合：五个板块代码都含 .TI");
ok(filterBoards(ALL, "huagong").length === 2, "⑦ 组合：全拼 'huagong' 也筛出 2 个");

// ⑧ 联想：排序 + 不变式
ok(suggestBoards(ALL, "").length === 0, "⑧ 空查询不给候选（不是把 262 个都弹出来）");
ok(suggestBoards(ALL, "   ").length === 0, "⑧ 纯空格同样不给候选");
const byName = suggestBoards(ALL, "化工");
ok(byName.length === 2, "⑧ 名称片段给出 2 个候选");
ok(byName[0].sector_name === "化工原料", "⑧ 候选保持清单顺序（同档内稳定）");
const byInitials = suggestBoards(ALL, "hg");
ok(byInitials.length === 2 && byInitials !== null, "⑧ 首字母撞车时两个都作为候选");
const byFull = suggestBoards(ALL, "baijiu");
ok(byFull.length === 1 && byFull[0].sector_name === "白酒", "⑧ 全拼命中的候选");
ok(suggestBoards(ALL, "不存在的东西").length === 0, "⑧ 无命中时不给候选");
ok(suggestBoards(ALL, "ti", 2).length === 2, "⑧ limit 生效（最多 2 条）");
// 排在最前的应该是"名称完全等于查询"的那一个
const exact: BoardSearchTarget[] = [
  { sector_code: "1.TI", sector_name: "新材料化工", pinyin_full: "xincailiaohuagong" },
  { sector_code: "2.TI", sector_name: "化工", pinyin_full: "huagong" },
];
ok(suggestBoards(exact, "化工")[0].sector_name === "化工",
   "⑧ 名称**完全等于**查询的排最前（不是按清单顺序）");

// ★ 不变式：候选集 ⊆ 筛选结果集（点得到的必须筛得到）
let violations = 0;
const queries = ["化工", "hg", "huagong", "半导", "半导 材料", "ban dao", "ti",
                 "8859", "白酒", "baijiu", "hg 化工", "不存在"];
for (const q of queries) {
  const shown = new Set(filterBoards(ALL, q).map((r) => r.sector_code));
  for (const row of suggestBoards(ALL, q, 99)) {
    if (!shown.has(row.sector_code)) {
      violations += 1;
      console.error(`     候选不在筛选结果里：${q} → ${row.sector_code}`);
    }
  }
}
ok(violations === 0, "★ 不变式：联想候选集 ⊆ 筛选结果集（12 个查询逐一对拍）");

// 拼音提示（下拉右侧那行小字）：缺拼音时返回空串，不能显示成 "[]"
ok(pinyinHint(HUA_GONG) === "HG · huagongyuanliao", "⑧ 拼音提示＝首字母 · 全拼");
ok(pinyinHint(ONLY_INITIALS) === "JG", "⑧ 只有首字母时只显示首字母");
ok(pinyinHint(NO_PY) === "", "⑧ 两个都没有时返回空串（不显示空括号）");

console.log("─".repeat(70));
if (failed > 0) {
  console.error(`❌ ${failed} 条不通过`);
  process.exit(1);
}
console.log("✅ 全部通过");
