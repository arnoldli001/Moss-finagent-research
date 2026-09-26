/**
 * 管理员 · 资源管控：按等级配置资源上限（调用频次 / token / 自选数量 …）。
 *
 * 对应后端：`GET|PUT /api/v1/admin/platform/tiers`。
 *
 * ## 为什么"资源上限"要单独一个界面，而不并进功能权限
 *
 * 它们回答两个不同的问题：
 *   - **资源管控** = "给他多少"（次数、token、标的数）—— 防滥用、控成本；
 *   - **功能权限** = "他能进哪些页面、每项多少钱" —— 卖什么、卖多少。
 * 运营改价和调额度是两件不同的事、由不同角色做（成本 vs 商务），
 * 并在一屏会让"我只想调个额度"变成一次容易误操作的表单提交。
 *
 * ## 字段清单**由服务端下发**
 *
 * `resources` 的中文名来自 `/tiers` 的 `resources` 字段，前端不硬编码 ——
 * 后端加一个资源项时，前端自动多一行，不会静默漏掉。
 */

import { useCallback, useEffect, useState } from "react";
import { TierConfigPayload, TierPlan, platformApi } from "../api";
import { explain } from "./LoginScreen";

export default function AdminTierPanel() {
  const [data, setData] = useState<TierConfigPayload | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  // 每个等级一份草稿：改完点"保存"才提交（避免每敲一个字符打一次接口）
  const [draft, setDraft] = useState<Record<string, TierPlan>>({});

  const load = useCallback(async () => {
    setError("");
    try {
      const payload = await platformApi.tiers();
      setData(payload);
      setDraft(Object.fromEntries(payload.tiers.map((t) => [t.key, t])));
    } catch (e) {
      setError(explain(e));
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  const patchResource = (tier: string, key: string, value: string) => {
    setDraft((prev) => {
      const plan = prev[tier];
      if (!plan) return prev;
      const n = Number(value);
      return {
        ...prev,
        [tier]: {
          ...plan,
          resources: {
            ...plan.resources,
            [key]: Number.isFinite(n) ? n : 0,
          },
        },
      };
    });
  };

  const patchMeta = (tier: string, field: "label" | "note",
                     value: string) => {
    setDraft((prev) => prev[tier]
      ? { ...prev, [tier]: { ...prev[tier], [field]: value } } : prev);
  };

  const save = async (tier: string) => {
    const plan = draft[tier];
    if (!plan) return;
    setBusy(true); setError(""); setNotice("");
    try {
      const r = await platformApi.updateTier(tier, {
        label: plan.label,
        note: plan.note,
        resources: plan.resources,
      });
      setNotice(r.message);
      await load();
    } catch (e) {
      setError(explain(e));
    } finally {
      setBusy(false);
    }
  };

  const reset = async () => {
    setNotice(""); setError("");
    await load();
    setNotice("已放弃未保存的修改");
  };

  if (!data) {
    return (
      <div className="admin">
        {error
          ? <div className="error-box">{error}</div>
          : <div className="account-dim">加载中…</div>}
      </div>
    );
  }

  return (
    <div className="admin">
      <div className="admin-head">
        <h2>资源管控</h2>
        <span className="admin-stat">
          配置：<code>{data.config_path}</code>
        </span>
        <button className="account-mini" disabled={busy} onClick={() => void reset()}>
          重新载入
        </button>
      </div>

      {error && <div className="error-box">{error}</div>}
      {notice && <div className="info-box">{notice}</div>}

      <p className="auth-hint">
        单位为「每等级」的硬上限：超限的调用会被拒绝（<b>服务端强制</b>，不是界面上藏起来）。
        这些值与套餐配置同源（<code>configs/platform_tiers.json</code>），
        改动会进 Git，可评审、可回滚。
      </p>
      <p className="auth-hint">
        本页只管<b>资源额度</b>（每个等级最多能调多少次、多少 token、多少只票）。
        「能用哪些功能」在「功能权限」页里用开关配置。
        <b>定价不在这里维护</b> —— 权限配置要进 Git，写进售价只会多一处
        需要保密、又没人维护的数字。
      </p>

      {data.tiers.map((tier) => {
        const plan = draft[tier.key] ?? tier;
        const dirty = JSON.stringify(plan.resources) !==
          JSON.stringify(tier.resources)
          || plan.label !== tier.label;
        return (
          <section key={tier.key} className="tier-card">
            <div className="tier-card-head">
              <input className="auth-input tier-label" value={plan.label}
                onChange={(e) => patchMeta(tier.key, "label", e.target.value)} />
              <span className="account-dim mono">{tier.key}</span>
              {!tier.sellable && <span className="account-tier">内部角色</span>}
              <button className={dirty ? "auth-inline-btn" : "account-mini"}
                disabled={busy || !dirty} onClick={() => void save(tier.key)}>
                {dirty ? "保存修改" : "无改动"}
              </button>
            </div>

            <div className="tier-grid">
              {Object.entries(data.resources).map(([key, label]) => (
                <label key={key} className="profile-field">
                  <span className="profile-key" title={key}>{label}</span>
                  <input className="auth-input" type="number" min={0}
                    value={plan.resources[key] ?? 0}
                    onChange={(e) => patchResource(tier.key, key, e.target.value)} />
                </label>
              ))}
            </div>

            {plan.note && <p className="auth-hint">{plan.note}</p>}
          </section>
        );
      })}
    </div>
  );
}
