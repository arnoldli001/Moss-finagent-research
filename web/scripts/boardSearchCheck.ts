/**
 * `boardSearch` 的**行为**自证（与 `check:mainline` / `check:rateguard` 同款）。
 *
 * 为什么不让 pytest 只断言源码结构：结构判据是**形状判据** ——
 * 把 `includes` 写成 `startsWith`、把大小写转换删掉、把"拼音为空"那条
 * 短路去掉，源码里的字串照样在，测试照样绿。这里喂真输入看真输出。
 *
 * 覆盖"写错就静默退化"的六种情形：
 *   ① 名称子串（含中文中间的片段）  ② 代码子串（带/不带 `.TI`、大小写）
 *   ③ 拼音首字母（大写输入 / 小写输入）  ④ 拼音字段为空**不许**变成"全命中"
 *   ⑤ 多关键词 = AND              ⑥ 空查询 = 不筛（原样返回，且不是同一引用）
 *
 * 跑法：`npm run check:boardsearch`
 */
import { filterBoards, matchBoard, type BoardSearchTarget } from "../src/boardSearch";

let failed = 0;
function ok(cond: boolean, label: string) {
  if (cond) {
    console.log(`  ✅ ${label}`);
  } else {
    failed += 1;
    console.error(`  ❌ ${label}`);
  }
}

/** 三条真实形态的数据（名称/代码/拼音都来自后端 `/config_list`）。 */
const HUA_GONG: BoardSearchTarget = {
  sector_code: "885959.TI", sector_name: "化工原料", pinyin_initials: "HG",
};
const BAN_DAO: BoardSearchTarget = {
  sector_code: "885800.TI", sector_name: "半导体材料", pinyin_initials: "BDTCL",
};
const BAI_JIU: BoardSearchTarget = {
  sector_code: "885530.TI", sector_name: "白酒", pinyin_initials: "BJ",
};
const NO_PY: BoardSearchTarget = {
  sector_code: "885001.TI", sector_name: "无拼音板块", pinyin_initials: "",
};
const ALL = [HUA_GONG, BAN_DAO, BAI_JIU, NO_PY];

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

// ④ ★ 拼音为空时**不许**变成全命中
ok(!matchBoard(NO_PY, "hg"), "④ 拼音为空的行不会因为 'hg' 而命中");
ok(matchBoard(NO_PY, "无拼音"), "④ 拼音为空的行仍可按名称命中");

// ⑤ 多关键词 = AND
ok(matchBoard(BAN_DAO, "半导 材料"), "⑤ 两个词都命中 → 通过");
ok(!matchBoard(BAN_DAO, "半导 白酒"), "⑤ 其中一个不命中 → 不通过");
ok(matchBoard(BAN_DAO, "  bdtcl   半导  "), "⑤ 多余空格不影响（AND）");

// ⑥ 空查询
ok(matchBoard(HUA_GONG, ""), "⑥ 空串 = 不筛（全通过）");
ok(matchBoard(HUA_GONG, "   "), "⑥ 纯空格 = 不筛");
const out = filterBoards(ALL, "");
ok(out.length === ALL.length, "⑥ 空查询返回全部");
ok(out !== ALL, "⑥ 返回的是新数组（不改入参）");
ok(filterBoards(ALL, "hg").length === 1, "⑥ 组合：'hg' 只筛出 1 个");
ok(filterBoards(ALL, "ti").length === 4, "⑥ 组合：四个板块代码都含 .TI");

console.log("─".repeat(70));
if (failed > 0) {
  console.error(`❌ ${failed} 条不通过`);
  process.exit(1);
}
console.log("✅ 全部通过");
