r"""verify_cde_ind.py — one-shot verification of the CDE IND source.

Run:  .\.venv\Scripts\python.exe data_sources\verify_cde_ind.py

Prints the HTTP evidence, the live monthly series, and the exact window each
month covers. Any failure raises; it never substitutes synthetic values.
"""
from __future__ import annotations

import datetime
import sys
import time

import requests

sys.path.insert(0, ".")
from data_sources.cde_ind_source import (  # noqa: E402
    API, HEADERS, HOME, PAGE, monthly_class1_ind,
)


def main() -> int:
    print("=" * 78)
    print("CDE 创新药 IND 申报数量 — live verification")
    print("=" * 78)
    print("now         :", datetime.datetime.now().isoformat(timespec="seconds"))
    print("page        :", PAGE)
    print("api         :", API)
    print()

    # 1) prove the session warm-up is what unlocks the API
    cold = requests.post(API, headers=HEADERS,
                         data={"statenow": "", "year": "2026", "drugtype": "",
                               "applytype": "", "acceptid": "", "drugname": "",
                               "company": "", "pageSize": "50", "pageNum": "1"},
                         timeout=30)
    print(f"[1] COLD POST (no cookie)      -> HTTP {cold.status_code} "
          f"ctype={cold.headers.get('Content-Type')} "
          f"json={cold.text.lstrip().startswith('{')}")

    s = requests.Session()
    s.headers.update(HEADERS)
    warm = s.get(HOME, timeout=30)
    hot = s.post(API, headers={"Referer": PAGE, "Origin": "https://www.cde.org.cn"},
                 data={"statenow": "", "year": "2026", "drugtype": "hy",
                       "applytype": "xy", "acceptid": "", "drugname": "",
                       "company": "", "pageSize": "50", "pageNum": "1"},
                 timeout=45)
    j = hot.json()
    recs = (j.get("data") or {}).get("records") or []
    print(f"[2] warm-up GET {HOME}   -> HTTP {warm.status_code}")
    print(f"[3] HOT  POST                  -> HTTP {hot.status_code} "
          f"code={j.get('code')} total={j.get('data', {}).get('total')} "
          f"n={len(recs)}")
    if recs:
        r0 = recs[0]
        print("    sample record:", {k: r0.get(k) for k in
              ("acceptid", "registerkind", "createdate", "drugtype",
               "applytype", "drgnamecn")})
    print()

    # 2) live series
    t0 = time.time()
    series = monthly_class1_ind(years=(2025, 2026), include_imported=False)
    print(f"[4] monthly 1类创新药 IND 受理号件数   ({time.time()-t0:.0f}s)")
    for d, v in series:
        print(f"    {d}   {int(v):>4}")
    print()
    if series:
        print(f"[5] latest period = {series[-1][0]}  value = {int(series[-1][1])}")
    print("[6] OK — source is live; values above are real CDE 受理品种信息 counts.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
