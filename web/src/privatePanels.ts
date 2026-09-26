/**
 * 私有商业版前端资产的**本地解析器**（公开仓库里这些文件不存在）。
 *
 * ## 为什么不能直接 `import`
 *
 * 竞价选股 / 量化选股 / 擒牛线的实现属私有资产：只留本地、不进公开仓库
 * （见 `.gitignore` 的「私有核心资产」一节）。但**引用它们的组件是要入库的**，
 * 所以不能写成普通的静态导入：
 *
 *     import AuctionSelectPanel from "./components/AuctionSelectPanel";  // ✗ 别这么写
 *
 * 公开仓库 checkout 里没有这个文件，`tsc -b` 会直接报
 *     error TS2307: Cannot find module './components/AuctionSelectPanel'
 * 于是本地跑得好好的，一 push 上去**整个前端构建失败**（CI 的 frontend job 变红）。
 * 更麻烦的是报的是"找不到模块"，很难第一时间联想到是私有裁剪造成的 ——
 * 本文件就是为了把这个坑一次填掉。
 *
 * ## 所以改用 `import.meta.glob`
 *
 * `import.meta.glob` 由 Vite 在**构建期对文件系统**求值，不走 git：
 *
 *   - 本地完整版（文件在）→ 命中，拿到真实组件 → 功能正常可用；
 *   - 公开仓库（文件不在）→ **零匹配返回空对象，且不报任何错**
 *     （不像静态 import 那样抛 TS2307）→ 调用方渲染 PrivateFeatureNotice 占位。
 *
 * 结果是**同一份入库源码两边都能构建**：本地有完整功能，公开仓库只有占位提示，
 * 而且公开仓库里根本不含私有实现。这比"把私有源码加密后入库"更彻底，
 * 也不用管密钥（加密入库仍要解密才能构建，密钥一旦泄漏等于没加密）。
 *
 * ## 新增一个私有面板时
 *
 * ① `.gitignore` 里加上该文件（放在「私有核心资产」一节）；
 * ② 在下面加一条 glob + 一个导出；
 * ③ 调用方写成 `X !== null ? <X … /> : <PrivateFeatureNotice feature="…" />`。
 *
 * 注意两点：
 *   - glob 的路径必须是**字面量字符串**，Vite 要在编译期静态分析它，传变量会构建报错；
 *   - 这里的 props 类型是**手写的契约**。私有组件的 props 一旦变了，
 *     本地 `tsc` 会立刻报错（这正是在本地暴露漂移、而不是等 push 之后才炸）。
 */
import type { ComponentType } from "react";
import type { IntradayDailySnapshot } from "./api";

/**
 * 竞价选股面板的 props（契约，见 `components/AuctionSelectPanel.tsx`）。
 *
 * 用户口径 2026-09-23：面板**不再收任何 props** —— 原来那个 `onBack`
 * 只服务于「← 返回量化交易」按钮，按钮删掉后 prop 也随之移除
 * （`noUnusedParameters` 开着，留着它就构建不过）。
 * 用 `Record<string, never>` 而不是 `{}`：前者"传任何 prop 都报错"，
 * 后者等于"随便传"，契约就白写了。
 */
type AuctionSelectProps = Record<string, never>;

/** 量化选股面板的 props（契约，见 `components/QuantSelectPanel.tsx`）。 */
type QuantSelectProps = {
  onAdded?: (codes: string[]) => void;
  /** 面板往板块里加完票后回报，让左侧板块下拉立刻重读（板块管理已搬到左侧）。 */
  onSectorsChanged?: () => void;
};

/** 擒牛线主图的 props（契约，见 `components/NiuLineChart.tsx`）。 */
type NiuLineProps = { snapshot: IntradayDailySnapshot };

/**
 * 竞价选股（组合级页签）。文件不在时是 `null`，调用方退化成占位提示。
 *
 * 标注了显式类型：`Record` 的索引访问在 TS 里被当成"必定存在"，
 * 不标就丢掉 `| null`，调用处的 `!== null` 判断会看着像多余代码。
 */
export const AuctionSelectPanel: ComponentType<AuctionSelectProps> | null =
  import.meta.glob<{ default: ComponentType<AuctionSelectProps> }>(
    "./components/AuctionSelectPanel.tsx",
    { eager: true },
  )["./components/AuctionSelectPanel.tsx"]?.default ?? null;

/** 量化选股（日内分时/日K 之外的第三个「周期」口径）。 */
export const QuantSelectPanel: ComponentType<QuantSelectProps> | null =
  import.meta.glob<{ default: ComponentType<QuantSelectProps> }>(
    "./components/QuantSelectPanel.tsx",
    { eager: true },
  )["./components/QuantSelectPanel.tsx"]?.default ?? null;

/** 擒牛线主图档位线（NML/QRL/CBX20/CBX60/SMX）。 */
export const NiuLineChart: ComponentType<NiuLineProps> | null =
  import.meta.glob<{ default: ComponentType<NiuLineProps> }>(
    "./components/NiuLineChart.tsx",
    { eager: true },
  )["./components/NiuLineChart.tsx"]?.default ?? null;
