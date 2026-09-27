"""报告 HTML 渲染：数据 JSON 内嵌 + 静态 JS 骨架（ECharts 图表 / DOM 热力图）。

设计约束（继承项目"括号失配即整页全废"的教训）：
  - **JS 是静态的**：模板里只有一处动态内容 —— `window.__REPORT__ = <json>`；
    所有图表 option 都是写死的骨架，数据在 JS 里从 `__REPORT__` 读取组装，
    不存在"Python 拼 JS 代码"的注入面；
  - 模板交付前过 `node --check`（见 tests/test_sector_rotation.py 的回归测试）；
  - 红涨绿跌（A 股口径）、浅底深字研报风，与工作区产物一致；
  - 页内显式标注：交易日以数据为准、各数据源口径、规则生成声明、固定免责声明。

## ★ ECharts 本地化（2026-09-27 性能修复）

原模板从 jsdelivr CDN 拉 echarts@5（约 1MB）：对外隧道实测 ≈51KB/s，
仅这一项就要 ≈20 秒，且 jsdelivr 在大陆可用性不稳定 —— 这是"行业轮动日报
打开慢"的第二大根因（第一大是落伍报告同步生成 20~40s，见 routes 侧修复）。

现在优先用**同前缀的本地静态文件** `src/sector_rotation/static/echarts.min.js`
（vendored，版本 5.5.1，与 CDN 版本一致），由
`GET /api/v1/sector_rotation/echarts.min.js` 提供；本地文件缺失时才回退 CDN
（保底可用性）。历史上已落盘的旧报告 HTML 引用的仍是 CDN 链接，
路由侧在**读取时**做一次字符串替换切换为本地地址，无需重新生成。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

#: 本地 vendored ECharts（缺失时回退 CDN）。
_STATIC_DIR = Path(__file__).resolve().parent / "static"
_ECHARTS_LOCAL = _STATIC_DIR / "echarts.min.js"
_ECHARTS_CDN = "https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js"


def echarts_src() -> str:
    """报告页用的 ECharts 地址：本地 vendored 优先，缺失回退 CDN。"""
    return "echarts.min.js" if _ECHARTS_LOCAL.exists() else _ECHARTS_CDN


#: 免责声明（金融场景一票否决条款：禁止改写、缩减或省略）
DISCLAIMER = (
    "免责声明：以上内容基于公开数据和量化分析，仅供参考，不构成投资建议。"
    "市场有风险，投资需谨慎。任何投资决策应结合个人风险承受能力、资金状况和投资目标"
    "独立判断，必要时咨询持牌专业机构。过往表现不预示未来收益。"
)

_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>行业轮动与资金流向监控</title>
<script src="__ECHARTS_SRC__"></script>
<style>
  :root{
    --bg:#f4f6f9; --card:#ffffff; --ink:#1c2333; --sub:#6b7280; --line:#e6e9f0;
    --up:#d93026; --down:#1a9e54; --accent:#2456c8;
  }
  *{margin:0;padding:0;box-sizing:border-box;}
  body{background:var(--bg);color:var(--ink);
       font-family:"PingFang SC","Microsoft YaHei",system-ui,sans-serif;
       padding:24px 16px 48px;}
  .wrap{max-width:1180px;margin:0 auto;}
  header{display:flex;flex-wrap:wrap;align-items:baseline;gap:10px;margin-bottom:6px;}
  header h1{font-size:22px;letter-spacing:.5px;}
  .meta{font-size:12.5px;color:var(--sub);}
  .badge{display:inline-block;font-size:11px;padding:2px 8px;border-radius:10px;
         background:#eef2ff;color:var(--accent);border:1px solid #dbe4ff;}
  .badge.warn{background:#fdf3e7;color:#b45309;border-color:#f5ddba;}
  .grid-idx{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));
            gap:12px;margin:16px 0;}
  .idx-card{background:var(--card);border:1px solid var(--line);border-radius:12px;
            padding:14px 16px;}
  .idx-card .name{font-size:12.5px;color:var(--sub);}
  .idx-card .val{font-size:22px;font-weight:700;margin-top:4px;
                 font-variant-numeric:tabular-nums;}
  .idx-card .chg{font-size:13px;margin-top:2px;font-weight:600;
                 font-variant-numeric:tabular-nums;}
  .up{color:var(--up);} .down{color:var(--down);}
  .card{background:var(--card);border:1px solid var(--line);border-radius:12px;
        padding:18px 20px;margin-bottom:16px;}
  .card h2{font-size:16px;margin-bottom:4px;display:flex;align-items:center;gap:8px;}
  .card h2::before{content:"";width:4px;height:16px;border-radius:2px;background:var(--accent);}
  .src{font-size:11.5px;color:#9aa1ad;margin-top:10px;line-height:1.6;}
  .headline{font-size:15.5px;font-weight:700;margin-top:8px;color:var(--accent);}
  .thesis{font-size:14px;line-height:1.9;margin-top:8px;}
  .heat{display:grid;grid-template-columns:repeat(auto-fill,minmax(112px,1fr));
        gap:10px;margin-top:12px;}
  .tile{border-radius:10px;padding:10px 10px 8px;color:#fff;position:relative;min-height:72px;}
  .tile .t-name{font-size:12.5px;font-weight:600;}
  .tile .t-pct{font-size:17px;font-weight:800;margin-top:3px;font-variant-numeric:tabular-nums;}
  .tile .t-net{font-size:10.5px;opacity:.85;margin-top:2px;}
  .tile .t-tag{position:absolute;top:6px;right:8px;font-size:10px;opacity:.75;}
  .legend{display:flex;align-items:center;gap:8px;font-size:11.5px;color:var(--sub);margin-top:10px;}
  .legend .bar{flex:1;max-width:220px;height:8px;border-radius:4px;
               background:linear-gradient(90deg,#1a9e54,#cfe3d6,#f3d7d4,#d93026);}
  .chart{width:100%;height:330px;}
  .two-col{display:grid;grid-template-columns:1fr 1fr;gap:16px;}
  @media(max-width:860px){.two-col{grid-template-columns:1fr;}}
  .view-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));
             gap:14px;margin-top:10px;}
  .view{border:1px solid var(--line);border-radius:10px;padding:14px 16px;background:#fbfbfd;}
  .view h3{font-size:14px;margin-bottom:6px;display:flex;align-items:center;gap:8px;}
  .view p{font-size:13px;line-height:1.85;color:#37404f;}
  .lvl{font-size:11px;font-weight:700;padding:2px 9px;border-radius:9px;white-space:nowrap;}
  .lvl.in{background:rgba(217,48,38,.10);color:var(--up);}
  .lvl.out{background:rgba(26,158,84,.10);color:var(--down);}
  .lvl.watch{background:#fdf3e7;color:#b45309;}
  .note{font-size:12px;color:var(--sub);background:#f7f8fb;border:1px dashed var(--line);
        border-radius:8px;padding:10px 12px;margin-top:10px;line-height:1.7;}
  .disc{font-size:12px;color:#8a93a3;line-height:1.8;margin-top:14px;
        border-top:1px solid var(--line);padding-top:12px;}
  table{width:100%;border-collapse:collapse;font-size:13px;margin-top:8px;}
  th,td{padding:8px 10px;border-bottom:1px solid var(--line);text-align:right;
        font-variant-numeric:tabular-nums;}
  th:first-child,td:first-child{text-align:left;}
  thead th{color:var(--sub);font-weight:600;font-size:12px;}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>行业轮动与资金流向监控</h1>
    <span class="badge">每日自动生成</span>
    <span class="badge warn" id="intraday-badge" style="display:none;"
          >盘中：指数为实时，行业为上一交易日</span>
    <span class="meta" id="meta-line"></span>
  </header>

  <div class="grid-idx" id="idx-cards"></div>

  <div class="card">
    <h2>今日主线</h2>
    <p class="headline" id="narrative-headline"></p>
    <p class="thesis" id="narrative-body"></p>
    <div class="note" id="narrative-note"></div>
  </div>

  <div class="card">
    <h2>行业轮动热力图 · 当日涨跌幅</h2>
    <div class="heat" id="heat"></div>
    <div class="legend"><span>跌</span><div class="bar"></div><span>涨</span>
      <span style="margin-left:14px;">红涨绿跌 · 标注为东财行业层级
        · 每格下方为主力净额</span></div>
    <div class="src" id="heat-src"></div>
  </div>

  <div class="two-col">
    <div class="card">
      <h2>主力资金 · 行业净流入排行（当日）</h2>
      <div id="flow1d" class="chart"></div>
      <div class="src">单位 ¥亿 · 东财行业板块口径（Tushare moneyflow_ind_dc）
        · 红=流入 / 绿=流出</div>
    </div>
    <div class="card">
      <h2>榜单板块 · 近 5 日累计主力净额</h2>
      <div id="flow5d" class="chart"></div>
      <div class="src">单位 ¥亿 · 当日流入/流出榜各前 5 的近 5 日合计，
        用于验证单日信号的连续性</div>
    </div>
  </div>

  <div class="two-col">
    <div class="card">
      <h2>大盘主力资金流 · 近 20 日</h2>
      <div id="mflow" class="chart"></div>
      <div class="src">单位 ¥亿 · 全市场主力净流入（东财口径，akshare）· 正值为净流入</div>
    </div>
    <div class="card">
      <h2>风格轮动 · 大盘价值 vs 成长（当日）</h2>
      <div id="style1d" class="chart"></div>
      <div class="src">当日涨跌幅 % · 上证50 代表大盘价值，创业板指代表成长；
        差值 ≥1.5pct 视为风格显著</div>
    </div>
  </div>

  <div class="card">
    <h2>轮动研判 · 规则生成</h2>
    <div class="view-grid" id="views"></div>
    <div class="note">以上方向卡片由规则引擎从本页数据直接推导（narrative_engine=rule-based-v1），
      不使用大模型，结论与数字必然一致；不构成任何买卖建议。</div>
  </div>

  <div class="card">
    <h2>行业板块全表（东财行业口径）</h2>
    <table id="ind-table">
      <thead><tr><th>板块</th><th>层级</th><th>涨跌幅</th><th>主力净额(亿)</th></tr></thead>
      <tbody></tbody>
    </table>
    <div class="src" id="table-src"></div>
  </div>

  <div class="disc" id="disc"></div>
</div>

<script>
window.__REPORT__ = __REPORT_JSON__;
</script>
<script>
(function(){
  var R = window.__REPORT__ || {};
  var UP = '#d93026', DOWN = '#1a9e54';
  function fmt(v, digits){
    return (v === null || v === undefined) ? '—'
      : Number(v).toFixed(digits === undefined ? 2 : digits);
  }
  function cls(v){ return (v || 0) >= 0 ? 'up' : 'down'; }
  function sign(v){ return (v || 0) >= 0 ? '+' : ''; }

  // ---- 头部元信息 ----
  var meta = R.meta || {};
  document.getElementById('meta-line').textContent =
    '数据交易日 ' + (meta.trade_date || '—') + '（以数据为准）'
      + '· 生成于 ' + (meta.generated_at || '—');
  if (meta.intraday) document.getElementById('intraday-badge').style.display = '';

  // ---- 指数卡片 ----
  var cards = (R.indices || []).map(function(c){
    return '<div class="idx-card"><div class="name">' + c.name + '</div>' +
      '<div class="val">' + fmt(c.price) + '</div>' +
      '<div class="chg ' + cls(c.change_pct) + '">' + sign(c.change_pct)
        + fmt(c.change_pct) + '%</div></div>';
  });
  var m = R.market || {};
  if (m.turnover_yi) {
    var delta = m.turnover_delta_yi;
    cards.push('<div class="idx-card"><div class="name">两市成交额</div>' +
      '<div class="val">¥' + (m.turnover_yi / 10000).toFixed(2) + '万亿</div>' +
      '<div class="chg ' + (delta > 0 ? 'up' : 'down') + '">' +
      (delta === null || delta === undefined ? '环比暂缺' :
        '较上日 ' + (delta > 0 ? '放量' : '缩量') + ' ' + Math.abs(delta).toFixed(0) + '亿') +
      '</div></div>');
  }
  if (m.up !== null && m.up !== undefined) {
    cards.push('<div class="idx-card"><div class="name">上涨 / 下跌（涨停）</div>' +
      '<div class="val"><span class="up">' + m.up + '</span>'
        + ' / <span class="down">' + m.down + '</span></div>' +
      '<div class="chg up">涨停 ' + (m.limit_up || 0) + ' 家</div></div>');
  }
  document.getElementById('idx-cards').innerHTML = cards.join('');

  // ---- 主线研判 ----
  var n = R.narrative || {};
  document.getElementById('narrative-headline').textContent = n.headline || '';
  document.getElementById('narrative-body').textContent = n.body || '';
  document.getElementById('narrative-note').textContent = n.note || '';

  // ---- 热力图 ----
  function tileColor(p){
    var a = Math.min(Math.abs(p || 0) / 4.0, 1);
    return (p || 0) >= 0
      ? 'rgba(217,48,38,' + (0.35 + 0.6 * a).toFixed(2) + ')'
      : 'rgba(26,158,84,' + (0.30 + 0.55 * a).toFixed(2) + ')';
  }
  document.getElementById('heat').innerHTML = (R.heat || []).map(function(t){
    return '<div class="tile" style="background:' + tileColor(t.pct) + '">' +
      '<span class="t-tag">L' + t.level + '</span>' +
      '<div class="t-name">' + t.name + '</div>' +
      '<div class="t-pct">' + sign(t.pct) + fmt(t.pct) + '%</div>' +
      '<div class="t-net">主力 ' + sign(t.net_yi) + fmt(t.net_yi, 1) + '亿</div></div>';
  }).join('') || '<span class="meta">行业板块数据暂缺</span>';
  document.getElementById('heat-src').textContent =
    '来源：' + ((meta.sources || [])[1] || 'Tushare moneyflow_ind_dc（东财板块口径）');

  // ---- 图1：当日行业主力净流入排行 ----
  var inflow = ((R.flow_1d || {}).inflow || []).slice().reverse();
  var outflow = ((R.flow_1d || {}).outflow || []).slice();
  var f1cats = outflow.map(function(b){ return b.name; })
    .concat(inflow.map(function(b){ return b.name; }));
  var f1data = outflow.concat(inflow).map(function(b){
    return { value: b.net_yi, itemStyle: { color: (b.net_yi || 0) >= 0 ? UP : DOWN,
             borderRadius: [0, 4, 4, 0] } };
  });
  echarts.init(document.getElementById('flow1d')).setOption({
    tooltip: { trigger: 'axis', valueFormatter: function(v){ return v + ' 亿'; } },
    grid: { left: 90, right: 44, top: 20, bottom: 30 },
    xAxis: { type: 'value', name: '¥亿' },
    yAxis: { type: 'category', data: f1cats },
    series: [{ name: '主力净额', type: 'bar', barWidth: 14,
      label: { show: true, position: 'right', formatter: '{c}' }, data: f1data }]
  });

  // ---- 图2：榜单板块近 5 日累计净额 ----
  var f5rows = (R.flow_5d || []).slice().sort(
    function(a, b){ return (a.net5_yi || 0) - (b.net5_yi || 0); });
  echarts.init(document.getElementById('flow5d')).setOption({
    tooltip: { trigger: 'axis', valueFormatter: function(v){ return v + ' 亿'; } },
    grid: { left: 90, right: 44, top: 20, bottom: 30 },
    xAxis: { type: 'value', name: '¥亿' },
    yAxis: { type: 'category', data: f5rows.map(function(r){ return r.name; }) },
    series: [{ name: '近5日累计', type: 'bar', barWidth: 14,
      label: { show: true, position: 'right',
        formatter: function(p){ return f5rows[p.dataIndex].net5_yi === null ? '暂缺' : p.value; } },
      data: f5rows.map(function(r){
        return { value: r.net5_yi, itemStyle: { color: (r.net5_yi || 0) >= 0 ? UP : DOWN,
                 borderRadius: [0, 4, 4, 0] } };
      }) }]
  });

  // ---- 图3：大盘主力资金流近 20 日 ----
  var series = ((R.market_flow || {}).series) || [];
  echarts.init(document.getElementById('mflow')).setOption({
    tooltip: { trigger: 'axis', valueFormatter: function(v){ return v + ' 亿'; } },
    grid: { left: 60, right: 20, top: 20, bottom: 44 },
    xAxis: { type: 'category', data: series.map(function(r){ return (r.date || '').slice(5); }),
      axisLabel: { interval: 3, rotate: 30 } },
    yAxis: { type: 'value', name: '¥亿' },
    series: [{ name: '主力净流入', type: 'bar', barWidth: 10,
      data: series.map(function(r){
        return { value: r.net_yi, itemStyle: { color: (r.net_yi || 0) >= 0 ? UP : DOWN } };
      }) }]
  });

  // ---- 图4：风格轮动（上证50 vs 创业板指）----
  var pmap = {};
  (R.indices || []).forEach(function(c){ pmap[c.name] = c.change_pct; });
  var styleRows = [
    { name: '上证50(价值)', pct: pmap['上证50'] },
    { name: '沪深300', pct: pmap['沪深300'] },
    { name: '上证指数', pct: pmap['上证指数'] },
    { name: '科创50', pct: pmap['科创50'] },
    { name: '创业板指(成长)', pct: pmap['创业板指'] }
  ].filter(function(r){ return r.pct !== null && r.pct !== undefined; });
  echarts.init(document.getElementById('style1d')).setOption({
    tooltip: { trigger: 'axis', valueFormatter: function(v){ return v + ' %'; } },
    grid: { left: 60, right: 20, top: 20, bottom: 30 },
    xAxis: { type: 'category', data: styleRows.map(function(r){ return r.name; }) },
    yAxis: { type: 'value', name: '%' },
    series: [{ name: '当日涨跌幅', type: 'bar', barWidth: 26,
      label: { show: true, position: 'top', formatter: '{c}%', fontSize: 11 },
      data: styleRows.map(function(r){
        return { value: r.pct, itemStyle: { color: r.pct >= 0 ? UP : DOWN,
                 borderRadius: r.pct >= 0 ? [4, 4, 0, 0] : [0, 0, 4, 4] } };
      }) }]
  });

  // ---- 研判方向卡片 ----
  var lvlText = { 'in': '流入', 'out': '流出', 'watch': '观察' };
  document.getElementById('views').innerHTML = (n.views || []).map(function(v){
    return '<div class="view"><h3><span class="lvl ' + v.level + '">' +
      (lvlText[v.level] || v.level) + '</span>' + v.title + '</h3><p>' + v.text + '</p></div>';
  }).join('');

  // ---- 行业全表 ----
  document.querySelector('#ind-table tbody').innerHTML = (R.industries || []).map(function(b){
    return '<tr><td>' + b.name + '</td><td>L' + b.level + '</td>' +
      '<td class="' + cls(b.pct) + '">' + sign(b.pct) + fmt(b.pct) + '%</td>' +
      '<td class="' + cls(b.net_yi) + '">' + sign(b.net_yi) + fmt(b.net_yi) + '</td></tr>';
  }).join('');
  document.getElementById('table-src').textContent =
    '共 ' + ((R.industries || []).length) + ' 个行业板块 · 按涨跌幅降序'
      + ' · 嵌套子行业已按（涨跌幅,净额）去重';

  // ---- 免责与口径 ----
  document.getElementById('disc').innerHTML =
    '<b>数据口径</b>：' + (meta.sources || []).join('；') +
    '。主力资金流为大单统计估算，非交易所官方口径；'
      + '东财行业板块口径与申万行业口径数值不可直接对应。<br><br>' +
    '<b>__DISCLAIMER__</b>';
})();
</script>
</body>
</html>
"""


def render_html(payload: dict[str, Any]) -> str:
    """报告 JSON → 独立 HTML（动态点：内嵌 JSON + 免责声明 + ECharts 地址）。"""
    blob = json.dumps(payload, ensure_ascii=False)
    # 防止 JSON 里的 "</script>" 提前闭合脚本块（板块名理论上不含，但不赌）
    blob = blob.replace("</", "<\\/")
    return (_PAGE
            .replace("__REPORT_JSON__", blob)
            .replace("__ECHARTS_SRC__", echarts_src())
            .replace("__DISCLAIMER__", DISCLAIMER))
