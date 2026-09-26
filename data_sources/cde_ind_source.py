"""
cde_ind_source.py — 创新药 IND 申报数量 (个) from CDE 受理品种信息 (real, free, no auth).

Source  : 国家药监局药品审评中心 (CDE) 信息公开 -> 受理品种信息
Page    : https://www.cde.org.cn/main/xxgk/listpage/9f9c74c73e0f8f56a8bfbc646055026d
API     : POST https://www.cde.org.cn/main/xxgk/getMenuListHc   (JSON)

Verified by real requests from this machine on 2026-09-26 (local, UTC+8):
  * GET https://www.cde.org.cn/ then POST the API inside the SAME requests.Session
    -> HTTP 200 application/json. Without the warm-up GET -> HTTP 202 JS challenge.
  * Full unfiltered sweep 2024+2025+2026 = 1,034 pages / 51,594 rows, 0 page errors.
  * Recency: newest createdate observed = 2026-09-24 (T-2 days from 2026-09-26).
  * Server-side pre-filtering (drugtype x applytype x {hy,swzp,zy}) reproduces the
    unfiltered 1类 IND set EXACTLY (1,942 vs 1,942 acceptances in 2026) in ~29 s/year
    instead of ~104 s/year.

Definition used here (deterministic, documented):
  1类创新药 IND = 受理号 first 4 chars in {CXHL, CXSL, CXZL}
                    (domestic clinical-trial applications; CXH*=化药, CXS*=生物制品,
                     CXZ*=中药; trailing L = 临床试验 = IND, S = 上市 = NDA)
              AND registerkind normalises to 1 / 1类 / 1.x
                    (CDE 注册分类; 1.x = 生物制品 1.1–1.4, still 1类创新药)
  周期 = createdate  (受理日期) month.
  include_imported=True additionally counts JXHL/JXSL (境外已上市药品的临床试验申请).

Yearly totals produced by this definition (受理号件数):
  2024 = 1,978 ; 2025 = 2,216 ; 2026 (to 09-24) = 1,942

NOTE on a widely-quoted alternative: the CDE 年度药品审评报告 uses per-application
category wording (e.g. "创新药和改良新药 IND 申请 1,947 件（化药）" for 2025). Those
published figures are ANNUAL and use a broader 创新药+改良新药 basket, so they are NOT
directly comparable to the 1类-IND series here. This module returns the 1类-only,
monthly series; treat the annual report as a cross-check of order of magnitude only.
"""

from __future__ import annotations

import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

API = "https://www.cde.org.cn/main/xxgk/getMenuListHc"
HOME = "https://www.cde.org.cn/"
PAGE = ("https://www.cde.org.cn/main/xxgk/listpage/"
        "9f9c74c73e0f8f56a8bfbc646055026d")

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "X-Requested-With": "XMLHttpRequest",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "Referer": PAGE,
    "Origin": "https://www.cde.org.cn",
}

PAGE_SIZE = 50            # API whitelist: 10 / 20 / 30 / 50 only
IND_DOMESTIC = ("CXHL", "CXSL", "CXZL")
IND_IMPORTED = ("JXHL", "JXSL", "JXZL")
# 受理品种信息 的 drugtype 代码 -> 该类别下含 IND 的"新药"篮子
_FILTERS = (("hy", "xy"), ("swzp", "xy"), ("zy", "xy"))


def _is_class1(rk) -> bool:
    """CDE 注册分类 -> True for 化学药品/生物制品/中药 1类 (incl. 1.1..1.4).
    '原1' (旧分类) is deliberately NOT treated as 1类."""
    if not rk:
        return False
    first = str(rk).strip()
    if first.startswith("原"):
        return False
    first = first.split(";")[0].strip()
    if first in ("1", "1类"):
        return True
    if first.startswith("1."):
        return first[2:].replace(".", "").isdigit()
    return False


def _new_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    s.get(HOME, timeout=30)          # required: sets the FSSBBIl1UgzbN7N80S/T cookie
    return s


def _post(session, payload, tries=4):
    for attempt in range(tries):
        try:
            r = session.post(API, data=payload, timeout=45)
            if r.text.lstrip().startswith("{"):
                j = r.json()
                if j.get("code") == 200:
                    d = j.get("data") or {}
                    return d.get("records") or [], d.get("total")
        except Exception:
            pass
        time.sleep(1.5 * (attempt + 1))
        try:
            session.get(HOME, timeout=20)      # refresh the anti-bot cookie
        except Exception:
            pass
    raise RuntimeError(f"CDE API failed: {payload}")


def _fetch(session, year, drugtype, applytype, max_workers=3):
    base = {"statenow": "", "year": str(year), "drugtype": drugtype,
            "applytype": applytype, "acceptid": "", "drugname": "", "company": "",
            "pageSize": str(PAGE_SIZE)}
    first, total = _post(session, dict(base, pageNum="1"))
    pages = ((total or 0) + PAGE_SIZE - 1) // PAGE_SIZE
    recs = list(first)
    if pages > 1:
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futs = {}
            for pn in range(2, pages + 1):
                futs[ex.submit(_post, session, dict(base, pageNum=str(pn)))] = pn
                time.sleep(0.1)
            for f in as_completed(futs):
                r, _ = f.result()
                recs.extend(r)
    return recs


def monthly_class1_ind(years=(2025, 2026), include_imported: bool = False,
                       max_workers: int = 3):
    """
    Returns [(period_date: str, value: float)] ascending, monthly.

    period_date = "YYYY-MM-01" (month of CDE 受理日期 / createdate).
    value       = 该月"1类创新药 IND 受理号件数".
    Raises RuntimeError if the CDE API cannot be read (never silently fabricates).
    """
    prefixes = IND_DOMESTIC + (IND_IMPORTED if include_imported else ())
    session = _new_session()

    buckets: dict[str, int] = defaultdict(int)
    for year in years:
        for drugtype, applytype in _FILTERS:
            for r in _fetch(session, year, drugtype, applytype, max_workers):
                aid = r.get("acceptid") or ""
                if aid[:4] not in prefixes:
                    continue
                if not _is_class1(r.get("registerkind")):
                    continue
                cd = r.get("createdate") or ""
                if len(cd) >= 7:
                    buckets[cd[:7]] += 1
        try:
            session.get(HOME, timeout=20)      # keep the cookie fresh per year
        except Exception:
            pass

    return [(f"{m}-01", float(buckets[m])) for m in sorted(buckets)]


if __name__ == "__main__":
    import datetime
    t0 = time.time()
    print("now:", datetime.datetime.now().isoformat(timespec="seconds"))
    series = monthly_class1_ind(years=(2025, 2026), include_imported=False)
    for d, v in series:
        print(d, int(v))
    print(f"rows={len(series)} elapsed={time.time() - t0:.1f}s")
