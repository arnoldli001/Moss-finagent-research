import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
// ⚠️ **顺序有意义，不要调换**：CSS 里同名选择器是**后进 DOM 的赢**。
// `intel.css` 必须排在 `styles.css` 之后，情报中心的规则才能稳定覆盖
// 全局同名类（否则会变成"看打包顺序碰运气"）。
import "./styles.css";
import "./intel.css";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);
