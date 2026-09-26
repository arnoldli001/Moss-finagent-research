/**
 * Vite 客户端类型（`import.meta.glob` / `import.meta.env` / `*.css` 等）。
 *
 * 本仓库**必须**保留这个引用：`src/privatePanels.ts` 用 `import.meta.glob`
 * 在构建期解析私有商业版面板（竞价选股/量化选股/擒牛线），
 * 没有这份声明 `ImportMeta.glob` 就不存在，`tsc -b` 会报
 *   error TS2339: Property 'glob' does not exist on type 'ImportMeta'
 */
/// <reference types="vite/client" />
