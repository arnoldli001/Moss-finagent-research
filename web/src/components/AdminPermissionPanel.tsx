/**
 * 管理员 · 功能权限：功能 × 等级 的**开关矩阵**。
 *
 * 对应后端：`GET /api/v1/admin/platform/permissions-matrix`、
 * `PUT /api/v1/admin/platform/tiers/{tier}`。
 *
 * ## 为什么这里**只有开关、没有定价**（用户口径 2026-09-23）
 *
 * 用户的判断是对的，而且理由比"界面复杂"更实在：
 *
 *   1. **定价是"租户等级"的属性，不是"每项功能"的属性。**
 *      现实里的报价是"VIP 999/月，含 A/B/C；竞价选股 +199/月"，
 *      而不是"投研分析这一项值 10 元" —— 按功能逐项定价，
 *      最后没人能说清一个等级到底该收多少。
 *   2. **两个维度混在一屏，会让"我只想开关一个功能"变成一次容易误操作的提交。**
 *      开关是**即时生效的权限变更**，定价是**商务决策**，节奏完全不同。
 *   3. 套餐月费留在「资源管控」里改（与"给多少资源"一起）；
 *      这里只回答一个问题：**这个等级能不能用这个功能**。
 *
 * 后端仍保留 `pricing` 字段（历史数据与其它调用方可能需要），
 * 但本界面不再读写它 —— 所以保存请求**只发 `features`**，
 * 不会顺带把定价覆盖掉。
 *
 * ## 矩阵而非"每个等级一个表单"
 *
 * 管理员的真实任务是**横向比较**："竞价选股这一项，试用/VIP/管理员各是什么状态？"
 * 按等级分表单会把同一项功能拆到三处，比较要来回翻。
 */

import { useCallback, useEffect, useState } from "react";
import { platformApi } from "../api";
import { explain } from "./LoginScreen";

type Row = {
  feature: string;
  label: string;
  tiers: Record<string, { enabled: boolean; price: number }>;
};
type TierMeta = {
  key: string; label: string; sellable: boolean;
};

/**
 * **管理员专属功能**：不对 VIP / 试用开放，这一列勾了也无效。
 *
 * ## 现在它是一个防御性兜底，正常情况下根本不会显示
 *
 * 用户口径（2026-09-25）："默认只有管理员有运行指标、调度管理的权限，
 * 不用加在功能权限设置的选项里"、"其他用户都没这个权限且不可选择"。
 *
 * 做法是**从后端 `FEATURES` 里删掉**这两项 —— 本面板的行来自
 * `GET /api/v1/me/features` 下发的 `feature_labels`（即 `FEATURES`），
 * 所以它们连格子都不会出现，"不可选择"是**结构上**保证的，
 * 而不是靠这里锁住。保留这个集合是为了：
 *   · 万一将来有人把某项加回 `FEATURES`（比如想单独售卖），
 *     这一层能立刻把它锁住，而不是静默地对 VIP 开放；
 *   · `metrics` 曾经不在这个集合里 —— 那正是"运维面能力被当套餐项"
 *     的隐患本身，加进来比漏掉便宜。
 *
 * （对应的路由本身也挂了 `require_admin`，见 `src/api/routes/scheduler.py`。）
 */
const ADMIN_ONLY_FEATURES = new Set(["scheduler", "metrics"]);

/** 该格子是否被"管理员专属"锁住（管理员那一列不锁，本来就是管理员的）。 */
function isLocked(tier: string, feature: string): boolean {
  return tier !== "admin" && ADMIN_ONLY_FEATURES.has(feature);
}

export default function AdminPermissionPanel() {
  const [rows, setRows] = useState<Row[]>([]);
  const [tiers, setTiers] = useState<TierMeta[]>([]);
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  /** 每个等级一份"待保存的开关改动"（勾了但还没点保存）。 */
  const [pending, setPending] =
    useState<Record<string, Record<string, boolean>>>({});

  const load = useCallback(async () => {
    setError("");
    setLoading(true);
    try {
      const m = await platformApi.matrix();
      setRows(m.rows);
      setTiers(m.tiers);
      setPending({});
    } catch (e) {
      setError(explain(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  const toggle = (tier: string, feature: string, next: boolean) => {
    // 乐观更新：开关类操作若等一个来回才动，用户会以为没点上而连点。
    // 保存失败时由 `load()` 拉回服务端真实状态。
    setRows((prev) => prev.map((r) => r.feature === feature
      ? { ...r, tiers: { ...r.tiers,
          [tier]: { ...r.tiers[tier], enabled: next } } }
      : r));
    setPending((p) => ({
      ...p,
      [tier]: { ...(p[tier] ?? {}), [feature]: next },
    }));
  };

  const setAll = (tier: string, enabled: boolean) => {
    // 只动**可开关**的行：把「全开」写给管理员专属项会产生一个
    // "存进去了但用户侧永远看不到"的假配置。
    const target = rows.filter((r) => !isLocked(tier, r.feature));
    setRows((prev) => prev.map((r) => (isLocked(tier, r.feature) ? r : {
      ...r,
      tiers: { ...r.tiers, [tier]: { ...r.tiers[tier], enabled } },
    })));
    setPending((p) => ({
      ...p,
      [tier]: Object.fromEntries(target.map((r) => [r.feature, enabled])),
    }));
  };

  const save = async (tier: string) => {
    const features = pending[tier];
    if (!features) return;
    setBusy(true); setError(""); setNotice("");
    try {
      // ★ 只发 features：不带 pricing，避免把定价顺带覆盖成 0
      const r = await platformApi.updateTier(tier, { features });
      setNotice(r.message);
      await load();
    } catch (e) {
      setError(explain(e));
      // 保存失败 → 拉回真实状态，避免界面停在"看起来已生效"的假象
      await load();
    } finally {
      setBusy(false);
    }
  };

  const dirty = (tier: string) => Boolean(pending[tier]);
  /** 该等级**真正可开关**的功能行：管理员专属的行对非管理员档不参与计数。
   *
   * 否则分母里含一个永远勾不上的格子（例如 VIP 显示"已开 8/12"），
   * 管理员会以为"还差 4 项没配"，而其中 1 项本来就不该给他。
   */
  const assignableRows = (tier: string) =>
    rows.filter((r) => !isLocked(tier, r.feature));
  const enabledCount = (tier: string) =>
    assignableRows(tier).filter((r) => r.tiers[tier]?.enabled).length;

  return (
    <div className="admin">
      <div className="admin-head">
        <h2>功能权限</h2>
        <button className="account-mini" disabled={busy || loading}
          onClick={() => void load()}>
          {loading ? "读取中…" : "重新载入"}
        </button>
      </div>

      {error && <div className="error-box">{error}</div>}
      {notice && <div className="info-box">{notice}</div>}

      <p className="auth-hint">
        每一行是一个功能，每一列是一个租户等级，<b>勾选即为该等级开启</b>。
        开关决定对应用户能否看到那个页签（页签清单由服务端下发，
        前端不再自己判断）。保存后**只影响该等级**，且是「整体校验后原子写入」，
        不会出现"改了一半"；费用与资源上限在「资源管控」里配。
        <br />
        标了<b>「管理员专属」</b>的行不对 VIP / 试用开放（如「调度管理」：
        它能手动触发内部作业，属运维面），那两列不可勾选。
      </p>

      {loading && rows.length === 0 ? (
        <div className="account-dim">加载中…</div>
      ) : (
        <table className="admin-table matrix-table">
          <thead>
            <tr>
              <th className="matrix-feature">功能</th>
              {tiers.map((t) => (
                <th key={t.key}>
                  <div>{t.label}</div>
                  <div className="account-dim">
                    已开 {enabledCount(t.key)}/{assignableRows(t.key).length}
                  </div>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.feature}>
                <td className="matrix-feature">
                  <div>{r.label}</div>
                  <div className="account-dim mono">{r.feature}</div>
                </td>
                {tiers.map((t) => {
                  const locked = isLocked(t.key, r.feature);
                  // 锁住的格子一律显示为「未开启」：矩阵里可能残留一个
                  // 早先勾上的 true（后端会忽略它），照着显示 true 会让人
                  // 以为"VIP 真的开着调度管理"。
                  const on = !locked && (r.tiers[t.key]?.enabled ?? false);
                  return (
                    <td key={t.key}>
                      <label className={on
                        ? "matrix-switch on" : "matrix-switch"}
                        title={locked
                          ? `${r.label}：管理员专属，不对 ${t.label} 开放`
                          : `${t.label} · ${r.label}`}>
                        <input type="checkbox" checked={on}
                          disabled={busy || locked}
                          onChange={(e) => toggle(t.key, r.feature,
                                                  e.target.checked)} />
                        <span className="matrix-switch-text">
                          {locked ? "管理员专属" : on ? "已开启" : "未开启"}
                        </span>
                      </label>
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
          <tfoot>
            <tr>
              <td className="matrix-feature">
                <div className="account-dim">批量 / 保存</div>
                <div className="matrix-bulk">
                  {tiers.map((t) => (
                    <span key={t.key} className="matrix-bulk-group">
                      <button className="account-mini" disabled={busy}
                        onClick={() => setAll(t.key, true)}>
                        {t.label} 全开
                      </button>
                      <button className="account-mini" disabled={busy}
                        onClick={() => setAll(t.key, false)}>
                        全关
                      </button>
                    </span>
                  ))}
                </div>
              </td>
              {tiers.map((t) => (
                <td key={t.key}>
                  <button
                    className={dirty(t.key) ? "auth-inline-btn" : "account-mini"}
                    disabled={busy || !dirty(t.key)}
                    onClick={() => void save(t.key)}>
                    {dirty(t.key) ? `保存「${t.label}」` : "无改动"}
                  </button>
                </td>
              ))}
            </tr>
          </tfoot>
        </table>
      )}

      <h3 className="admin-section">当前套餐概览</h3>
      <table className="admin-table">
        <thead>
          <tr><th>等级</th><th>名称</th>
            <th>是否可售</th><th>已开功能</th></tr>
        </thead>
        <tbody>
          {tiers.map((t) => (
            <tr key={t.key}>
              <td className="mono">{t.key}</td>
              <td>{t.label}</td>
              <td>{t.sellable ? "可售" : "内部角色"}</td>
              <td>{enabledCount(t.key)}/{assignableRows(t.key).length}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="auth-hint">
        本页只决定<b>功能开关</b>（客户能看到/使用哪些页签）；
        每个等级的<b>资源额度</b>在「资源管控」里配置。
        <b>定价不在这里维护</b>（用户口径 2026-09-23）——
        权限配置进 Git，价格另行走报价单。
      </p>
    </div>
  );
}
