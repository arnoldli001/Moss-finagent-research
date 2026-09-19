/**
 * 商业版功能占位提示。
 *
 * 竞价选股/量化选股/擒牛线等属私有商业版资产，开源仓库不含其实现；
 * 本组件仅在对应入口给出统一说明，不展示任何公式、参数或打分逻辑。
 */
export default function PrivateFeatureNotice({ feature }: { feature: string }) {
  return (
    <section className="panel private-feature-notice">
      <h2>{feature}</h2>
      <div className="info-box">
        「{feature}」为商业版功能，开源版未包含其实现。
      </div>
      <p className="muted-text">
        开源版保留系统其余全部能力（做T辅助/估值面板/板块拥挤度/资金流/策略回测）。
      </p>
    </section>
  );
}
