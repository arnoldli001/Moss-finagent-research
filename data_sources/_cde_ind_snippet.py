# 15-line drop-in: real 创新药 IND 申报数量(个) from CDE 受理品种信息 (no auth, no key)
import requests
from collections import defaultdict
API="https://www.cde.org.cn/main/xxgk/getMenuListHc"; H={"User-Agent":"Mozilla/5.0","X-Requested-With":"XMLHttpRequest","Content-Type":"application/x-www-form-urlencoded; charset=UTF-8"}
def _1(rk):  # CDE 注册分类 -> 1类(含1.1~1.4), 排除 '原1'
    f=str(rk or "").strip()
    return f and not f.startswith("原") and (f.split(";")[0].strip() in("1","1类") or f.split(";")[0].strip()[:2]=="1.")
def periods(years=(2025,2026)):
    s=requests.Session(); s.headers.update(H); s.get("https://www.cde.org.cn/",timeout=30)  # 必做: 取反爬cookie
    b=defaultdict(int)
    for y in years:
        for dt in("hy","swzp","zy"):                     # 化药 / 生物制品 / 中药
            for pn in range(1,999):
                p={"statenow":"","year":str(y),"drugtype":dt,"applytype":"xy","acceptid":"","drugname":"","company":"","pageSize":"50","pageNum":str(pn)}
                d=s.post(API,data=p,timeout=45).json().get("data") or {}
                rs=d.get("records") or []
                for r in rs:
                    if r["acceptid"][:4] in("CXHL","CXSL","CXZL") and _1(r.get("registerkind")):
                        b[r["createdate"][:7]]+=1
                if pn*50>=(d.get("total") or 0): break
    return [(f"{m}-01",float(b[m])) for m in sorted(b)]

if __name__ == "__main__":
    print(periods((2026,)))   # -> [('2026-01-01', 265.0), ('2026-02-01', 187.0), ... ('2026-09-01', 240.0)]
