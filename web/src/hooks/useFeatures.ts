/**
 * 当前用户可见的功能页签（由套餐权限决定）。
 *
 * 对应后端：`GET /api/v1/me/features`。
 *
 * ## 为什么页签必须由服务端下发
 *
 * 权限是**管理员可配置**的：同一个 VIP 租户，今天开着"策略回测"、
 * 明天可能被关掉。前端若自己硬编码页签，会出现两种坏情况：
 *   - 配了权限但页面还在 → 用户点进去全是 403，像系统坏了；
 *   - 取消了权限但前端没改 → 等于权限没生效。
 *
 * 所以这里只做一件事：把服务端的 `visible_views` 拿来当页签清单。
 *
 * ## 失败时的降级：只给最小集合，**不是全给**
 *
 * 请求失败时若"当作全部可见"，就把"权限关闭"降级成了"权限全开" ——
 * 那是 fail-open，方向错了。这里回退到最小集合（只有做T，即试用档的可见项），
 * 宁可少显示也不能多显示。
 */

import { useEffect, useState } from "react";
import { MyFeatures, platformApi } from "../api";

/** 接口失败时的最小可见集合（fail-closed）。 */
const MINIMAL_VIEWS = ["intraday"];

export function useFeatures(enabled: boolean) {
  const [features, setFeatures] = useState<MyFeatures | null>(null);
  const [loading, setLoading] = useState(enabled);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    if (!enabled) return;
    let alive = true;
    setLoading(true);
    platformApi.myFeatures()
      .then((data) => { if (alive) { setFeatures(data); setFailed(false); } })
      .catch(() => { if (alive) { setFeatures(null); setFailed(true); } })
      .finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  }, [enabled]);

  const visible: string[] = features?.visible_views ?? MINIMAL_VIEWS;

  return {
    features,
    loading,
    /** 读取失败（界面应给出提示，而不是静默少显示页签） */
    failed,
    visible,
    isVisible: (view: string) => visible.includes(view),
    /** 量化交易页内的子功能是否可用（做T/量化选股/竞价选股） */
    quantEnabled: (key: string) => Boolean(features?.quant_views?.[key]),
  };
}
