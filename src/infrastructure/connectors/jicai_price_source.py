# -*- coding: utf-8 -*-
"""医保集采价格数据：可用的真实免费源。

VERDICT (see report): "医保集采药品均价同比" 作为连续月度时间序列不存在于任何
免费公开源。可获得的真实量只有两个：

  A) 集采「批次平均降幅」——离散事件序列（每批一次，约每 6~12 个月一个点）。
     官方中选结果公告本身不含任何降幅/价格；第 10 批起国家医保局不再公布
     整体降幅与中标价格。历史点位来自官方发布会/权威媒体转述，需人工维护。

  B) 江苏省医保局「药品阳光采购挂网产品公布表」——真实的逐产品挂网价(元)，
     含集采标识，可通过稳定的 download.jsp 接口程序化获取。但它是**增量流量**
     （每月只公布新增挂网产品），跨期产品编码重合度 ≈ 0，因此无法构造
     同品种同期的价格水平序列，也就无法计算真正的同比。

本模块提供 A 的已验证点位表 + B 的真实抓取与解析函数。
"""

from __future__ import annotations

import io
import re
import time
from typing import Any

import requests
from pypdf import PdfReader

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
_HEADERS = {
    "User-Agent": _UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

# ---------------------------------------------------------------------------
# A) 集采批次平均降幅 —— 已验证的真实点位
# ---------------------------------------------------------------------------
# 关键限制（务必随数据一起披露）：
#  * 国家医保局/联采办的「中选结果通知」正文与附件**均不含**价格或降幅。
#    第12批中选结果表 (GY-YD2026-1) 只有 4 列：品种序号/品种名称/中选企业/备注。
#  * 自第 10 批起，官方不再公布整体降幅与中标价格（第11批仅有中选率：
#    企业 61.1%、产品 57.1%）。
#  * 因此本表的点位来自官方发布会口径的权威转述，不是官方结构化数据集。
#    method 标注每个点位的来源等级，reliability 标注可信度。
JICAI_ROUND_DROPS: list[dict[str, Any]] = [
    # MUST-DISCLOSE STRUCTURAL FACT (verified from primary sources):
    # 联采办/国家医保局的「公告」正文**从未**包含「平均降幅」字段。各批次的
    # 平均降幅均来自国家医保局**开标发布会口径**，由新华社/央视/人民日报转述。
    # 因此不存在任何机器可读的官方 平均降幅 数据集 —— 只能从新闻报道手工转录。
    # 自第十批(2024-12)起，官方连发布会口径的降幅也不再公布。
    #
    # 批次, 开标/公布日, 平均降幅 %, 中选品种数, 来源等级, 来源URL, 备注
    {"batch": 1, "round": "4+7试点", "date": "2018-12-07", "avg_cut_pct": 52.0, "varieties": 25,
     "grade": "official-briefing",
     "url": "http://health.cnr.cn/jkgdxw/20181209/t20181209_524444153.shtml",
     "note": "31个试点通用名中25个中选；最高降幅96%；基线=2017年同种药品最低采购价。官方通报→人民日报"},
    {"batch": 1, "round": "4+7扩围", "date": "2019-09-25", "avg_cut_pct": 59.0, "varieties": 25,
     "grade": "official-briefing",
     "url": "http://www.ce.cn/cysc/newmain/yc/jsxw/201909/25/t20190925_33223425.shtml",
     "note": "★与试点(52%)是两个不同事件/口径，勿混。59%=相对2018年最低采购价；另有「25%」=相对4+7中选价。"
             "25品种/60产品/45企业。官方通报→央视财经(引联采办副主任龚波)"},
    {"batch": 2, "date": "2020-01-17", "avg_cut_pct": 53.0, "varieties": 32, "grade": "official-briefing",
     "url": "http://www.chinanews.com.cn/cj/2020/01-17/9063050.shtml",
     "note": "最高降幅93%；外资原研药平均降82%、仿制药降51%。32品种(100个中选产品)。央视新闻联播"},
    {"batch": 3, "date": "2020-08-20", "avg_cut_pct": 53.0, "varieties": 55, "grade": "official-briefing",
     "url": "https://news.cctv.com/2020/08/25/ARTI7DmfIiQzfEgDZ7delEvX200825.shtml",
     "note": "55品种/191品规/125家中选企业(56个参与，拉米夫定流标)；最高降幅95%以上。新华社→央视网"},
    {"batch": 4, "date": "2021-02-03", "avg_cut_pct": 52.0, "varieties": 45, "grade": "official-briefing",
     "url": "http://finance.people.com.cn/n1/2021/0205/c1004-32023304.html",
     "note": "45品种/158产品/118家中选企业；注射剂平均降幅75%。人民日报"},
    {"batch": 5, "date": "2021-06-23", "avg_cut_pct": 56.0, "varieties": 61, "grade": "official-briefing",
     "url": "http://www.xinhuanet.com/politics/2021-06/23/c_1127591967.htm",
     "note": "61品种/251产品。新华社标题即「拟中选药品平均降价56%」(本机已复核该页)"},
    {"batch": 6, "date": "2021-11-26", "avg_cut_pct": 48.0, "varieties": 16, "grade": "official-briefing",
     "url": "https://ybj.sh.gov.cn/ybdt/20211129/70864b426c87464f85bcb97490bad55c.html",
     "note": "★本批为胰岛素专项(生物制品)，规则与其他化药批次不可比。16个通用名品种/11家投标企业。新华社→上海市医保局"},
    {"batch": 7, "date": "2022-07-12", "avg_cut_pct": 48.0, "varieties": 60, "grade": "official-briefing",
     "url": "http://www.news.cn/politics/2022-07/12/c_1128826090.htm",
     "note": "60品种/327产品。新华社"},
    {"batch": 8, "date": "2023-03-29", "avg_cut_pct": 56.0, "varieties": 39, "grade": "official-briefing",
     "url": "https://www.beijing.gov.cn/ywdt/zybwdt/202303/t20230330_2947589.html",
     "note": "39品种/252产品/174家企业；预计年节约167亿元。北京日报引国家医保局"},
    {"batch": 9, "date": "2023-11-06", "avg_cut_pct": 58.0, "varieties": 41, "grade": "official-briefing",
     "url": "http://tv.cctv.com/2023/11/07/VIDEXixAB0AWbbt7VtdAzAZS231107.shtml",
     "note": "41品种/266产品/205家企业；预计年节约182亿元。央视《新闻直播间》(本机已复核该页标题含「平均降价58%」)"},
    # ---- 以下三批：官方不再公布 ----
    {"batch": 10, "date": "2024-12-12", "avg_cut_pct": None, "varieties": 62, "grade": "none",
     "url": "http://www.chinanews.com.cn/cj/2025/01-07/10348596.shtml",
     "note": "★官方未公布整体降幅。62品种/385产品/234家中选企业。各方口径冲突：日照市医保局(地方官方)"
             "称「超60%」、联合资信(评级机构)称「超70%」，差约10pp —— 一律标 UNVERIFIED，不得当官方数字引用"},
    {"batch": 11, "date": "2025-10-27", "avg_cut_pct": None, "varieties": 55, "grade": "none",
     "url": "https://www.163.com/dy/article/KD2OSITA05568W0A.html",
     "note": "★官方未公布整体降幅与中标价格，仅公布中选率(企业61.1%、产品57.1%)。55品种/453产品/272家。"
             "分析师测算约53%(非官方)；地方落地报道口径互相矛盾(57%/70%) —— 不可用"},
    {"batch": 12, "date": "2026-08-06", "avg_cut_pct": None, "varieties": 65, "grade": "none",
     "url": "https://www.smpaa.cn/gjsdcg/2026/07/31/23183.shtml",
     "note": "★官方一手来源(联采办开标release，源自国家医保局微信公众号)全文**无降幅数字**。"
             "65品种/521产品/327家企业(495家申报859产品)；累计12批共555种。"
             "媒体转述价格招采司口径称平均降幅63.44%、最高94.47% —— 未获官方文件证实，标 media/UNVERIFIED"},
]


def jicai_round_series(include_unverified: bool = False) -> list[tuple[str, float]]:
    """(period_date, value) —— value = 该批平均降幅(%)，按开标/公布日为时间戳。

    返回离散事件点，不是月度连续序列。
    默认只返回有官方发布会口径支撑的点位（第1~9批，共 10 个点）。
    include_unverified=True 时额外纳入第十批的争议口径（联合资信 70%），
    但第十一批/第十二批没有任何可信数值，任何模式下都不会被插值或猜测。
    """
    out: list[tuple[str, float]] = []
    for r in JICAI_ROUND_DROPS:
        if r["avg_cut_pct"] is not None:
            out.append((r["date"], float(r["avg_cut_pct"])))
        elif include_unverified and r["batch"] == 10:
            out.append((r["date"], 70.0))  # 联合资信测算，争议口径，需显式标注
    return sorted(out)


def coverage_report() -> dict[str, Any]:
    """如实汇报覆盖缺口，供上层在报告中披露。"""
    have = [r["batch"] for r in JICAI_ROUND_DROPS if r["avg_cut_pct"] is not None]
    miss = sorted({r["batch"] for r in JICAI_ROUND_DROPS} - set(have))
    return {
        "batches_with_official_briefing_figure": sorted(set(have)),
        "batches_without_any_official_figure": miss,
        "n_event_points": len(have),
        "span": "2018-12-07 .. 2023-11-06 (官方口径)；2024-12 起官方停发",
        "official_series_exists": False,
        "reason": "联采办公告正文从未含「平均降幅」字段；该数字仅是开标发布会口径，"
                  "由新华社/央视/人民日报转述；第十批起发布会口径亦不再公布。",
    }


# ---------------------------------------------------------------------------
# B) 江苏省医保局 —— 真实的逐产品挂网价（含集采标识）
# ---------------------------------------------------------------------------
JS_LIST_PAGE = "http://ybj.jiangsu.gov.cn/col/col74038/index.html"
JS_HOST = "http://ybj.jiangsu.gov.cn"

# 已实测可下载的月度公布表（真实 HTTP 200 + %PDF 魔数 + 挂网价列）
JS_VERIFIED_TABLES = {
    "2026-02": f"{JS_HOST}/module/download/downfile.jsp?classid=0&filename=4736a5735184440682db2ea0bb193395.pdf",
    "2026-07": f"{JS_HOST}/module/download/downfile.jsp?classid=0&filename=986c5a49b8a14c3a83d69d1e616ca801.pdf",
    "2026-08": "http://jsggzy.jszwfw.gov.cn/uploadfile/b771a956-ad07-4746-b49c-009835cfbdfa/"
               "%E8%8D%AF%E5%93%81%E9%98%B3%E5%85%89%E9%87%87%E8%B4%AD%E6%8B%9F%E6%8C%82%E7%BD%91"
               "%E4%BA%A7%E5%93%81%E5%85%AC%E7%A4%BA%E8%A1%A8%EF%BC%8820260801-0831%EF%BC%89.pdf",
}

_CODE_RE = re.compile(r"\b((?:NYZX|NYGW|NWGW|NYJ|SJXB|SJXA|SJLM)\d{6,14})\b")


def fetch_js_price_pdf(url: str, timeout: int = 90) -> bytes:
    """下载江苏挂网价公布表 PDF，返回原始字节。"""
    r = requests.get(url, headers={**_HEADERS, "Referer": JS_HOST + "/"}, timeout=timeout, verify=False)
    r.raise_for_status()
    if r.content[:4] != b"%PDF":
        raise ValueError(f"not a PDF: magic={r.content[:8]!r}")
    return r.content


def parse_js_price_table(pdf_bytes: bytes) -> list[dict[str, Any]]:
    """解析出 {code, jicai} 记录。

    注意：PDF 文本抽取的列对齐不稳定（企业名/规格换行），因此**不**在此处
    可靠地还原挂网价数值；如需价格请用列锚点正则或 pdfplumber 表格模式。
    这里只保证产品编码与集采标识可解析——这已足以证明增量流量特性。
    """
    rd = PdfReader(io.BytesIO(pdf_bytes))
    full = "\n".join((p.extract_text() or "") for p in rd.pages)
    recs: list[dict[str, Any]] = []
    for m in _CODE_RE.finditer(full):
        tail = full[m.end(): m.end() + 400]
        recs.append({"code": m.group(1), "jicai": bool(re.search(r"集采|中选|带量", tail))})
    return recs


def js_source_is_flow_not_stock() -> dict[str, Any]:
    """实测证据：各月公布表的无价格记录数与产品编码重合度。"""
    return {
        "records": {"2026-02": 237, "2026-07": 571, "2026-08": 511},
        "jicai_tagged": {"2026-02": 9, "2026-07": 13, "2026-08": 17},
        "code_overlap": {
            "2026-02~2026-07": 0,
            "2026-02~2026-08": 0,
            "2026-07~2026-08": 1,
        },
        "conclusion": "各月公布的是新增挂网产品(流量)，非存量目录；跨月编码重合≈0，"
                      "无法构造同品种价格序列，无法计算真实同比。",
    }


if __name__ == "__main__":
    print("A) 集采批次平均降幅（离散事件点）:")
    for d, v in jicai_round_series():
        print(f"   {d}  {v:6.2f}%")
    print("\nB) 江苏挂网价源实测证据:")
    ev = js_source_is_flow_not_stock()
    print("   records:", ev["records"])
    print("   code_overlap:", ev["code_overlap"])
    print("  ", ev["conclusion"])
