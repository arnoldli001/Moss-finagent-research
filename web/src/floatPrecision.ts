/**
 * 下发/落地浮点数的**统一精度口径**（用户 2026-09-30 裁定）。
 *
 * ## 原话
 *
 * > 「**一律 3 位有效数字**，同时作用于**服务端出口**和**前端缓存写入**」
 *
 * ## 为什么两端都要做
 *
 * * **服务端出口** —— 决定**隧道上要传多少字节**。对外试点走公网隧道，
 *   实测 ≈51 KB/s、劣化 ~4.6 KB/s，响应体积直接等于加载时间
 *   （服务端实现见 `src/core/float_precision.py`，实测最省 −61%）。
 * * **前端缓存写入** —— 决定 **localStorage 占多大**。localStorage 配额只有
 *   5 MB/源，而浏览器**不会压缩**写入的字符串：拥挤度一份曲线明文 ~258 KB，
 *   约 19 份就撑爆，撑爆会**连累告警/情报/因子库的缓存一起写不进去**。
 *
 * 两端用**同一条规则**，所以缓存里的数据与服务端刚下发的是同一精度 ——
 * 不会出现"缓存命中时精度更高、联网后反而变粗"的抖动。
 *
 * ## 口径：3 位有效数字（与服务端逐字一致）
 *
 * ```
 * roundSignificant(0.863388)          → 0.863
 * roundSignificant(0.00024339722)     → 0.000243     ← 不归零
 * roundSignificant(3.4567)            → 3.46
 * roundSignificant(1109.28)           → 1110
 * roundSignificant(402367923.3305)    → 402000000
 * ```
 *
 * ### 为什么不是"3 位小数"（实测否决）
 *
 * 占比/胜率/相关系数这类字段量级是 1e-4~1e-9，取 3 位**小数**会归零：
 * 拥挤度 `raw_crowding` 的 96 个不同取值会只剩 1 个，整条曲线变直线。
 * **有效数字自适应量级，天然不归零。**
 */

/** 有效数字位数（与服务端 `src/core/float_precision.py` 同源口径）。 */
export const SIGNIFICANT_DIGITS = 3;

/**
 * 豁免字段名（与服务端 `EXEMPT_FIELDS` 同一份语义）。
 *
 * ⚠️ **只能收"非测量值"** —— epoch 秒压到 3 位有效数字会劣化到 ±12 分钟，
 * 计时字段压了也没有体积收益。不许把"精度不够用的测量值"塞进来。
 */
export const EXEMPT_FIELDS: ReadonlySet<string> = new Set([
  "ts",
  "cached_at",
  "cache_age",
  "cache_age_seconds",
  "age_seconds",
  "elapsed",
  "elapsed_seconds",
  "seconds",
  "waited_seconds",
  "age_sec",
  "remaining_s",
]);

/**
 * 把一个有限数压到 `digits` 位有效数字。
 *
 * 用 `toPrecision` 而不是手算量级：它天然处理了进位与指数形式，
 * 且与 Python 侧 `f"{v:.3g}"` 的结果口径一致。
 *
 * 非有限值（NaN / ±Inf）与 0 **原样返回** —— 前者由服务端出口转 `null`，
 * 这里不越权；后者压了还是 0。
 */
export function roundSignificant(
  value: number,
  digits: number = SIGNIFICANT_DIGITS,
): number {
  if (!Number.isFinite(value) || value === 0) return value;
  return Number(value.toPrecision(digits));
}

/**
 * 递归地把一份载荷压到 3 位有效数字（**返回新对象，不改入参**）。
 *
 * * `boolean` 原样返回（`typeof true !== "number"`，天然安全）；
 * * 整数原样返回 —— 成交量/条数/根数本来就是整数，压了只会改类型；
 * * key 命中 `exempt` 的值原样带走；
 * * 数组/普通对象递归；`null`/`undefined`/字符串原样。
 */
export function roundPayload<T>(value: T, digits: number = SIGNIFICANT_DIGITS): T {
  if (typeof value === "number") {
    // 整数原样返回 —— 成交量/条数/根数本来就是整数，压了只会改类型，
    // 而且与服务端 `round_payload` 的 `isinstance(value, int)` 分支逐字对齐。
    if (Number.isInteger(value)) return value;
    return roundSignificant(value, digits) as unknown as T;
  }
  if (Array.isArray(value)) {
    return value.map((item) => roundPayload(item, digits)) as unknown as T;
  }
  if (value !== null && typeof value === "object") {
    const out: Record<string, unknown> = {};
    for (const [key, item] of Object.entries(value as Record<string, unknown>)) {
      out[key] = EXEMPT_FIELDS.has(key) ? item : roundPayload(item, digits);
    }
    return out as unknown as T;
  }
  return value;
}
