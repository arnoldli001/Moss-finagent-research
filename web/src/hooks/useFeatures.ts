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
 * ## 清单的三级来源（★ 2026-09-28 用户报障后重做）
 *
 * > "首次登录进去，一级目录只显示投资日历、事件告警，而投研分析、策略回测、
 * >   量化交易、主线挖掘、资金流监控、热点&研报小作文都要等 3-5 秒才出来"
 *
 * 根因是两个请求**串行**：`/auth/*` 拿到身份之后，本 hook 才发得出
 * `/me/features`。在这条公网链路上每次冷请求实测约 0.9 秒，页签就只能
 * 一条一条往外冒。现在改成"能拿到就先用，拿不到再等"：
 *
 * | 顺序 | 来源 | 时机 |
 * |---|---|---|
 * | ① | 本次 `/me/features` 的**校验结果** | 挂载后 ~1 次往返（后台，不阻塞渲染） |
 * | ② | `localStorage` 里上一次成功拿到的**整份载荷** | **同步，首帧** |
 * | ③ | 开机探测（`/auth/login`、`/auth/bootstrap`）**顺路带回**的清单 | 同步，首帧 |
 * | ④ | `MINIMAL_VIEWS`（最小集合） | 前三者都没有时 |
 *
 * 于是：**第一次**登录 = 1 次往返（认证那趟就带着清单）；**之后**每次刷新
 * = **0 次**往返，一级目录首帧就完整。细节见 `featuresCache.ts`。
 *
 * ## 失败时的降级方向：只给最小集合，**不是全给**
 *
 * 请求失败且**没有任何缓存**时，若"当作全部可见"，就把"权限关闭"降级成了
 * "权限全开" —— 那是 fail-open，方向错了。这里回退到最小集合（只有做T，
 * 即试用档的可见项），宁可少显示也不能多显示。
 *
 * ⚠️ 有缓存时则**继续用缓存的清单**（而不是缩到最小集合）：那份缓存来自上一次
 * 服务端确认过的答案，把它丢掉会让用户"刷新一下少了半排页签"，比慢一秒更糟。
 * 真正的权限边界在各业务端点上（`require_feature`），不在这里。
 */

import { useEffect, useMemo, useState } from "react";
import { MyFeatures, platformApi } from "../api";
import { readFeaturesCache, writeFeaturesCache } from "../featuresCache";

/** 接口失败、且**没有任何缓存**时的最小可见集合（fail-closed）。 */
const MINIMAL_VIEWS = ["intraday"];

export function useFeatures(enabled: boolean, userId?: string) {
  // ★ 同步读缓存（②③）：`userId` 一变就重读，换人登录不会拿到上一个人的清单。
  const cached = useMemo(() => readFeaturesCache(userId), [userId]);

  /** 本次校验（①）拿到的整份载荷。 */
  const [fetched, setFetched] = useState<MyFeatures | null>(null);
  const [loading, setLoading] = useState(enabled);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    if (!enabled) return;
    let alive = true;
    setLoading(true);
    platformApi.myFeatures()
      .then((data) => {
        if (!alive) return;
        setFetched(data);
        setFailed(false);
        // 落盘：下一次刷新就直接命中 ②，连这一趟都可以不等。
        writeFeaturesCache(data);
      })
      .catch(() => { if (alive) setFailed(true); })
      .finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  }, [enabled]);

  // ① 本次校验 > ② 缓存里的整份载荷。
  const effective = fetched ?? cached?.data ?? null;
  const seed = cached?.seedViews && cached.seedViews.length > 0
    ? cached.seedViews : null;
  const visible: string[] = effective?.visible_views ?? seed ?? MINIMAL_VIEWS;

  // 已经能渲染出完整清单时不算"加载中"：别给用户看骨架屏。
  const hasFallback = Boolean(effective || seed);

  return {
    features: effective,
    loading: loading && !hasFallback,
    /** 读取失败（界面应给出提示，而不是静默少显示页签）。 */
    failed,
    visible,
    isVisible: (view: string) => visible.includes(view),
    /** 量化交易页内的子功能是否可用（做T/量化选股/竞价选股）。 */
    quantEnabled: (key: string) => Boolean(effective?.quant_views?.[key]),
  };
}
