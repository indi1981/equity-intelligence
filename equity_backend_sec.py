
"""
Equity Intelligence v2 — SEC/XBRL backend
Usage:
  pip install fastapi uvicorn requests
  set SEC_USER_AGENT="Your Name your@email.com"
  uvicorn equity_backend_sec:app --reload --port 8000

The backend deliberately keeps SEC calls server-side because data.sec.gov does not support CORS.
It resolves ticker -> CIK -> submissions -> recent filings -> companyfacts -> normalized metrics.
No market-data or consensus estimates are fabricated here.
"""

import os, re, math, json
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
import requests
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware

APP = FastAPI(title="Equity Intelligence SEC Engine", version="0.3")
app = APP
APP.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

UA = os.getenv("SEC_USER_AGENT", "Equity Intelligence (guets2000@hotmail.com)")
HEADERS = {"User-Agent": UA, "Accept-Encoding": "gzip, deflate"}
TIMEOUT = 30
SEC_MIN_REQUEST_INTERVAL = 0.15

TICKER_URL = "https://www.sec.gov/files/company_tickers.json"
SUB_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"

STANDARD_TAGS = {
    "revenue":["Revenues","RevenueFromContractWithCustomerExcludingAssessedTax","SalesRevenueNet"],
    "gross_profit":["GrossProfit"],
    "operating_income":["OperatingIncomeLoss"],
    "net_income":["NetIncomeLoss"],
    "cfo":["NetCashProvidedByUsedInOperatingActivities"],
    "capex":["PaymentsToAcquirePropertyPlantAndEquipment","PaymentsToAcquireProductiveAssets"],
    "assets":["Assets"],
    "cash":["CashAndCashEquivalentsAtCarryingValue","CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"],
    "equity":["StockholdersEquity","StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"],
    "current_debt":["ShortTermBorrowings","LongTermDebtCurrent","LongTermDebtAndFinanceLeaseObligationsCurrent"],
    "noncurrent_debt":["LongTermDebtNoncurrent","LongTermDebtAndFinanceLeaseObligationsNoncurrent"]
}

def sec_get(url):
    import time
    for attempt in range(4):
        if attempt:
            time.sleep(min(2 ** (attempt - 1), 4))
        r=requests.get(url,headers=HEADERS,timeout=TIMEOUT)
        if r.status_code in (429, 503):
            continue
        r.raise_for_status()
        return r.json()
    r.raise_for_status()
    return r.json()

def to_float(v):
    try:
        return float(v)
    except Exception:
        return None

def pick_unit(units: Dict[str, Any], preferred=("USD","shares","USD/shares")):
    for u in preferred:
        if u in units: return u, units[u]
    if units:
        u=next(iter(units)); return u,units[u]
    return None,[]

def find_concepts(facts, metric):
    out=[]
    taxonomy_order=["us-gaap","ifrs-full"]
    for tax in taxonomy_order:
        group=facts.get("facts",{}).get(tax,{})
        for tag in STANDARD_TAGS[metric]:
            if tag in group:
                out.append((tax,tag,group[tag]))
    return out

def quality_source(source_id, source_name, filing_url=None, accession=None):
    return dict(sourceId=source_id,source_name=source_name,filing_url=filing_url,accession=accession,
                retrieved_at=datetime.now(timezone.utc).isoformat())

def select_duration_fact(arr, forms=("10-K","10-Q"), annual=False):
    candidates=[]
    for x in arr:
        if x.get("form") not in forms: continue
        val=to_float(x.get("val"))
        if val is None or not x.get("end"): continue
        start=x.get("start")
        if not start: continue
        try:
            days=(datetime.fromisoformat(x["end"])-datetime.fromisoformat(start)).days
        except Exception: continue
        if annual:
            if 320<=days<=410 and x.get("form")=="10-K": candidates.append(x)
        else:
            if 60<=days<=110: candidates.append(x)
    candidates.sort(key=lambda x:(x.get("end",""), x.get("filed","")), reverse=True)
    return candidates

def annual_history(facts, metric):
    concepts=find_concepts(facts,metric)
    best={}
    for tax,tag,meta in concepts:
        unit, arr=pick_unit(meta.get("units",{}),preferred=("USD",))
        for x in select_duration_fact(arr,annual=True):
            year=x["end"][:4]
            if year not in best:
                best[year]=(to_float(x["val"]),tax,tag,x)
    return best

def latest_annual(facts,metric):
    h=annual_history(facts,metric)
    if not h:return None
    year=max(h)
    val,tax,tag,x=h[year]
    return dict(value=val,year=year,taxonomy=tax,tag=tag,form=x.get("form"),period_end=x.get("end"),
                filing_date=x.get("filed"),fy=x.get("fy"),fp=x.get("fp"))

def latest_quarter(facts,metric):
    concepts=find_concepts(facts,metric)
    candidates=[]
    for tax,tag,meta in concepts:
        unit,arr=pick_unit(meta.get("units",{}),preferred=("USD",))
        for x in select_duration_fact(arr,forms=("10-Q","10-K"),annual=False):
            val=to_float(x.get("val"))
            if val is not None:candidates.append((x.get("end",""),val,tax,tag,x))
    if not candidates:return None
    candidates.sort(reverse=True,key=lambda z:z[0])
    end,val,tax,tag,x=candidates[0]
    return dict(value=val,taxonomy=tax,tag=tag,form=x.get("form"),period_start=x.get("start"),period_end=end,
                filing_date=x.get("filed"),fy=x.get("fy"),fp=x.get("fp"))

def latest_instant(facts,metric):
    concepts=find_concepts(facts,metric)
    candidates=[]
    for tax,tag,meta in concepts:
        unit,arr=pick_unit(meta.get("units",{}),preferred=("USD",))
        for x in arr:
            if x.get("form") not in ("10-K","10-Q","20-F","40-F"): continue
            v=to_float(x.get("val"))
            if v is not None and x.get("end"):candidates.append((x.get("end",""),v,tax,tag,x))
    if not candidates:return None
    candidates.sort(reverse=True,key=lambda z:z[0])
    end,val,tax,tag,x=candidates[0]
    return dict(value=val,taxonomy=tax,tag=tag,form=x.get("form"),period_end=end,filing_date=x.get("filed"),fy=x.get("fy"),fp=x.get("fp"))

def source_for(cik,filing_meta):
    acc=filing_meta.get("accessionNumber")
    doc=filing_meta.get("primaryDocument")
    url=f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc.replace('-','')}/{doc}" if acc and doc else None
    return quality_source(f"sec:{cik}:{acc or ''}", "SEC EDGAR", url, acc)

def _fact_candidates(facts, metric):
    """Return normalized duration facts for a metric, across US-GAAP/IFRS concepts."""
    out=[]
    for tax,tag,meta in find_concepts(facts,metric):
        units=meta.get("units",{})
        unit_name = "USD" if "USD" in units else ("shares" if "shares" in units else next(iter(units),None))
        if not unit_name:
            continue
        for x in units.get(unit_name,[]):
            if x.get("form") not in ("10-Q","10-K","20-F","40-F"):
                continue
            if not x.get("start") or not x.get("end"):
                continue
            try:
                days=(datetime.fromisoformat(x["end"])-datetime.fromisoformat(x["start"])).days
            except Exception:
                continue
            v=to_float(x.get("val"))
            if v is None:
                continue
            out.append({"value":v,"taxonomy":tax,"tag":tag,"unit":unit_name,"days":days,**x})
    return out

def _best_filing_fact(cands, fy=None, fp=None, min_days=0, max_days=999):
    pool=[x for x in cands if min_days<=x.get("days",0)<=max_days]
    if fy is not None:
        pool=[x for x in pool if x.get("fy") in (fy,str(fy))]
    if fp is not None:
        pool=[x for x in pool if str(x.get("fp","")).upper()==str(fp).upper()]
    if not pool:
        return None
    # Prefer the latest filing, then 10-Q over 10-K for quarterly observations.
    pool.sort(key=lambda x:(x.get("filed","") or "", 1 if x.get("form")=="10-Q" else 0, x.get("end","") or ""), reverse=True)
    return pool[0]

def _fiscal_years_for_metric(facts, metric):
    """Build quarter values without confusing calendar quarters with fiscal quarters.

    Priority:
      1) direct 3-month Q1/Q2/Q3 facts from 10-Q;
      2) YTD facts for Q2/Q3 when a direct quarter is absent;
      3) Q4 = annual FY minus Q1-Q2-Q3.
    """
    cands=_fact_candidates(facts,metric)
    years={}
    # Annual FY facts.
    for x in cands:
        if x.get("form") not in ("10-K","20-F","40-F"):
            continue
        if str(x.get("fp","")).upper()!="FY":
            continue
        if 320<=x.get("days",0)<=410:
            fy=x.get("fy")
            if fy is None: fy=x.get("end","")[:4]
            key=str(fy)
            old=years.setdefault(key,{}).get("annual")
            if old is None or (x.get("filed","") or "")>(old.get("filed","") or ""):
                years[key]["annual"]=x
    # Direct quarterly facts from 10-Q. 70-120 days tolerates 52/53-week calendars.
    for q in ("Q1","Q2","Q3"):
        for x in cands:
            if x.get("form") not in ("10-Q","20-F","40-F"):
                continue
            if str(x.get("fp","")).upper()!=q:
                continue
            if 70<=x.get("days",0)<=120:
                fy=x.get("fy")
                if fy is None: fy=x.get("end","")[:4]
                key=str(fy)
                old=years.setdefault(key,{}).get(q)
                if old is None or (x.get("filed","") or "")>(old.get("filed","") or ""):
                    years[key][q]=x
    # YTD fallbacks: Q2 is H1 minus Q1, Q3 is M9 minus H1.
    for label,lo,hi in (("Q2",150,220),("Q3",230,310)):
        for x in cands:
            if x.get("form") not in ("10-Q","20-F","40-F"):
                continue
            if str(x.get("fp","")).upper()!=label:
                continue
            if lo<=x.get("days",0)<=hi:
                fy=x.get("fy")
                if fy is None: fy=x.get("end","")[:4]
                key=str(fy)
                years.setdefault(key,{}).setdefault("ytd",{})[label]=x
    return years

def _quarterly_metric(facts, metric):
    """Return fiscal quarterly values for one metric, with provenance."""
    years=_fiscal_years_for_metric(facts,metric)
    out=[]
    for fy,rec in years.items():
        qvals={}
        # Q1/Q2/Q3 direct.
        for q in ("Q1","Q2","Q3"):
            if rec.get(q):
                qvals[q]=rec[q]
        # Q2/Q3 from YTD only if direct quarter is unavailable.
        ytd=rec.get("ytd",{})
        if "Q2" not in qvals and ytd.get("Q2") and qvals.get("Q1"):
            qvals["Q2"]={**ytd["Q2"],"value":ytd["Q2"]["value"]-qvals["Q1"]["value"],"method":"YTD 6 mois - Q1"}
        if "Q3" not in qvals and ytd.get("Q3"):
            h1 = ytd.get("Q2")
            if h1 and ytd["Q3"]:
                base_h1 = h1["value"]
                qvals["Q3"]={**ytd["Q3"],"value":ytd["Q3"]["value"]-base_h1,"method":"YTD 9 mois - YTD 6 mois"}
        # Q4 from FY annual less Q1-Q3. This is deliberately used for all duration metrics.
        if rec.get("annual") and all(q in qvals for q in ("Q1","Q2","Q3")):
            a=rec["annual"]
            qvals["Q4"]={**a,"value":a["value"]-qvals["Q1"]["value"]-qvals["Q2"]["value"]-qvals["Q3"]["value"],"method":"FY annuel - Q1 - Q2 - Q3"}
        for q,x in qvals.items():
            if x.get("value") is None: continue
            out.append({"fy":int(fy) if str(fy).isdigit() else fy,"quarter":q,"value":x["value"],
                        "period_start":x.get("start"),"period_end":x.get("end"),"filed":x.get("filed"),
                        "form":x.get("form"),"taxonomy":x.get("taxonomy"),"tag":x.get("tag"),
                        "unit":x.get("unit"),"method":x.get("method","Direct 3 mois XBRL")})
    out.sort(key=lambda z:(z.get("period_end") or "",z.get("quarter") or ""))
    return out

def build_quarterly(facts):
    metrics=["revenue","gross_profit","operating_income","net_income","cfo","capex"]
    by_metric={m:_quarterly_metric(facts,m) for m in metrics}
    keys={}
    for m,rows in by_metric.items():
        for r in rows:
            key=(str(r.get("fy")),r.get("quarter"))
            z=keys.setdefault(key,{"fy":r.get("fy"),"quarter":r.get("quarter"),"period_end":r.get("period_end"),"period_start":r.get("period_start")})
            z[m]=r.get("value")
            z.setdefault("provenance",{})[m]=r
    rows=list(keys.values())
    rows.sort(key=lambda z:(z.get("period_end") or "",str(z.get("quarter"))))
    # Only display periods where revenue exists; other metrics may be missing for some issuers.
    rows=[r for r in rows if r.get("revenue") is not None]
    for i,r in enumerate(rows):
        if r.get("capex") is not None: r["capex"]=abs(r["capex"])
        if r.get("cfo") is not None and r.get("capex") is not None:
            r["fcf"]=r["cfo"]-r["capex"]
        if r.get("gross_profit") is not None and r.get("revenue"):
            r["gross_margin"]=r["gross_profit"]/r["revenue"]
        if r.get("operating_income") is not None and r.get("revenue"):
            r["operating_margin"]=r["operating_income"]/r["revenue"]
        if r.get("net_income") is not None and r.get("revenue"):
            r["net_margin"]=r["net_income"]/r["revenue"]
        if r.get("fcf") is not None and r.get("revenue"):
            r["fcf_margin"]=r["fcf"]/r["revenue"]
        if r.get("fcf") is not None and r.get("net_income") not in (None,0):
            r["fcf_conversion"]=r["fcf"]/r["net_income"]
        if i>0 and rows[i-1].get("revenue") not in (None,0):
            r["revenue_qoq"]=r["revenue"]/rows[i-1]["revenue"]-1
        if i>=4 and rows[i-4].get("revenue") not in (None,0):
            r["revenue_yoy"]=r["revenue"]/rows[i-4]["revenue"]-1
    return rows

def _quarterly_shares(facts):
    # Diluted weighted-average shares are an instant/weighted concept, not a flow to subtract.
    concepts=[]
    for tax in ["us-gaap","ifrs-full"]:
        group=facts.get("facts",{}).get(tax,{})
        for tag in ["WeightedAverageNumberOfDilutedSharesOutstanding","WeightedAverageNumberOfDilutedSharesOutstandingAdjustment"]:
            if tag in group:
                concepts.append((tax,tag,group[tag]))
    out=[]
    for tax,tag,meta in concepts:
        for unit,arr in meta.get("units",{}).items():
            for x in arr:
                if x.get("form") not in ("10-Q","10-K"): continue
                if not x.get("start") or not x.get("end"): continue
                try: days=(datetime.fromisoformat(x["end"])-datetime.fromisoformat(x["start"])).days
                except Exception: continue
                if 70<=days<=410:
                    v=to_float(x.get("val"))
                    if v is not None: out.append({"value":v,"fy":x.get("fy"),"fp":x.get("fp"),"end":x.get("end"),"filed":x.get("filed"),"days":days,"taxonomy":tax,"tag":tag})
    return out

def _quarter_eps(facts):
    tags=["EarningsPerShareDiluted"]
    out=[]
    for tax in ["us-gaap","ifrs-full"]:
        group=facts.get("facts",{}).get(tax,{})
        for tag in tags:
            meta=group.get(tag)
            if not meta: continue
            for unit,arr in meta.get("units",{}).items():
                for x in arr:
                    if x.get("form") not in ("10-Q","10-K"): continue
                    if not x.get("start") or not x.get("end"): continue
                    try: days=(datetime.fromisoformat(x["end"])-datetime.fromisoformat(x["start"])).days
                    except Exception: continue
                    if 70<=days<=120:
                        v=to_float(x.get("val"))
                        if v is not None: out.append({"value":v,"fy":x.get("fy"),"fp":x.get("fp"),"end":x.get("end"),"filed":x.get("filed"),"taxonomy":tax,"tag":tag,"unit":unit})
    out.sort(key=lambda x:(x.get("end") or "",x.get("filed") or ""),reverse=True)
    return out


TWELVEDATA_API_KEY = os.getenv("TWELVEDATA_API_KEY", "").strip()

def twelve_quote(symbol):
    """Optional Twelve Data quote. The API key stays server-side in Render env vars."""
    if not TWELVEDATA_API_KEY:
        return {"available":False,"provider":"Twelve Data","reason":"TWELVEDATA_API_KEY non configurée."}
    try:
        url="https://api.twelvedata.com/quote"
        r=requests.get(url,params={"symbol":symbol,"apikey":TWELVEDATA_API_KEY},timeout=15)
        r.raise_for_status()
        data=r.json()
        if data.get("status")=="error" or data.get("code"):
            return {"available":False,"provider":"Twelve Data","reason":data.get("message") or "Erreur Twelve Data."}
        price=to_float(data.get("close") or data.get("price"))
        prev=to_float(data.get("previous_close"))
        change=to_float(data.get("percent_change"))
        return {"available":price is not None,"provider":"Twelve Data","price":price,"previous_close":prev,"change_pct":change,
                "currency":data.get("currency"),"timestamp":data.get("timestamp"),"datetime":data.get("datetime"),"symbol":data.get("symbol")}
    except Exception as e:
        return {"available":False,"provider":"Twelve Data","reason":str(e)}

def build_company(symbol):
    tickers=sec_get(TICKER_URL)
    rec=None
    for _,v in tickers.items():
        if v.get("ticker","").upper()==symbol.upper(): rec=v; break
    if not rec: raise HTTPException(404,"Ticker not found in SEC company_tickers.json")
    cik=str(rec["cik_str"]).zfill(10)
    sub=sec_get(SUB_URL.format(cik=cik)); facts=sec_get(FACTS_URL.format(cik=cik))
    recent=sub.get("filings",{}).get("recent",{})
    forms=["10-K","10-Q","20-F","40-F","6-K"]
    filings=[]
    for i,form in enumerate(recent.get("form",[])):
        if form not in forms: continue
        fm={"accessionNumber":recent["accessionNumber"][i],"primaryDocument":recent["primaryDocument"][i]}
        filings.append({"filing_date":recent["filingDate"][i],"form":form,"period":recent["reportDate"][i],"accession":recent["accessionNumber"][i],"primary_document":recent["primaryDocument"][i],**source_for(cik,fm)})
        if len(filings)>=20: break
    latest_k=max([f for f in filings if f["form"] in ("10-K","20-F","40-F")],key=lambda f:f["filing_date"],default={})
    base_source=source_for(cik,latest_k)

    quarterly=build_quarterly(facts)
    # Attach diluted EPS where an actual 3-month EPS fact exists; never subtract annual EPS.
    eps=_quarter_eps(facts)
    eps_by_end={}
    for e in eps:
        eps_by_end.setdefault(e["end"],e)
    for r in quarterly:
        e=eps_by_end.get(r.get("period_end"))
        if e:
            r["eps_diluted"]=e["value"]
            r.setdefault("provenance",{})["eps_diluted"]=e
        else:
            # Fallback only for a true quarter when diluted weighted-average shares
            # are available for the same fiscal period. Never derive Q4 by subtracting EPS.
            if r.get("quarter") in ("Q1","Q2","Q3") and r.get("net_income") is not None:
                matches=[x for x in _quarterly_shares(facts) if str(x.get("fy"))==str(r.get("fy")) and str(x.get("fp","")).upper()==r.get("quarter") and 70<=x.get("days",0)<=120]
                if matches:
                    sh=max(matches,key=lambda x:(x.get("filed") or "",x.get("end") or ""))
                    if sh.get("value") not in (None,0):
                        r["eps_diluted"]=r["net_income"]/sh["value"]
                        r.setdefault("provenance",{})["eps_diluted"]={"method":"Net income trimestriel / actions diluées moyennes trimestrielles",**sh}

    # Annual/current metrics for the overview remain direct XBRL facts.
    metrics={}
    for metric in STANDARD_TAGS:
        x=latest_annual(facts,metric) or latest_quarter(facts,metric) or latest_instant(facts,metric)
        if not x: continue
        metrics[metric]={**x,**base_source,"method":"direct XBRL fact","canonical_metric":metric}
    def m(k): return metrics.get(k,{}).get("value")
    revenue=m("revenue"); gp=m("gross_profit"); oi=m("operating_income"); ni=m("net_income"); cfo=m("cfo"); capex=m("capex")
    assets=m("assets"); cash=m("cash"); eq=m("equity"); cd=m("current_debt"); ncd=m("noncurrent_debt")
    if capex is not None: capex=abs(capex)
    fcf=(cfo-capex) if cfo is not None and capex is not None else None
    ratios={}
    def ratio(name,val,method):
        if val is not None: ratios[name]={"value":val,"method":method,**base_source}
    if revenue:
        ratio("gross_margin",gp/revenue if gp is not None else None,"Gross profit / Revenue")
        ratio("operating_margin",oi/revenue if oi is not None else None,"Operating income / Revenue")
        ratio("fcf_margin",fcf/revenue if fcf is not None else None,"(CFO - Capex) / Revenue")
        ratio("cfo_margin",cfo/revenue if cfo is not None else None,"CFO / Revenue")
    if eq and ni: ratio("roe",ni/eq,"Net income / ending equity (simplified; use average equity for production)")
    invested=(eq+cd+ncd-cash) if all(v is not None for v in [eq,cd,ncd,cash]) else None
    if invested and oi: ratio("roic",oi/invested,"Operating income / (Equity + debt - cash), simplified; NOPAT/average invested capital preferred")
    if assets and (cd is not None or ncd is not None): ratio("debt_to_assets",((cd or 0)+(ncd or 0))/assets,"(Current debt + non-current debt) / assets")

    history={}
    for metric in ["revenue","gross_profit","operating_income","net_income","cfo","capex"]:
        for year,(val,tax,tag,x) in annual_history(facts,metric).items():
            history.setdefault(year,{})[metric]=abs(val) if metric=="capex" else val
            history[year]["source"]={**base_source,"taxonomy":tax,"tag":tag,"period_end":x.get("end"),"form":x.get("form")}
    hist=[]
    for year in sorted(history,reverse=True)[:10]:
        z=history[year]; z["year"]=year; z["fcf"]=(z.get("cfo")-z.get("capex")) if z.get("cfo") is not None and z.get("capex") is not None else None; hist.append(z)

    # LTM from the four latest fiscal quarters, not from an arbitrary 90-day fact.
    ltmq=quarterly[-4:] if len(quarterly)>=4 else []
    ltm={}
    if len(ltmq)==4:
        for k in ["revenue","gross_profit","operating_income","net_income","cfo","capex","fcf"]:
            vals=[q.get(k) for q in ltmq]
            if all(v is not None for v in vals): ltm[k]=sum(vals)
        if ltm.get("revenue"):
            ltm["gross_margin"]=ltm.get("gross_profit",0)/ltm["revenue"] if ltm.get("gross_profit") is not None else None
            ltm["operating_margin"]=ltm.get("operating_income",0)/ltm["revenue"] if ltm.get("operating_income") is not None else None
            ltm["net_margin"]=ltm.get("net_income",0)/ltm["revenue"] if ltm.get("net_income") is not None else None
            ltm["fcf_margin"]=ltm.get("fcf",0)/ltm["revenue"] if ltm.get("fcf") is not None else None
        if ltm.get("fcf") is not None and ltm.get("net_income") not in (None,0): ltm["fcf_conversion"]=ltm["fcf"]/ltm["net_income"]

    # Diluted shares: use latest quarterly weighted average as a denominator only; do not subtract.
    share_candidates=_quarterly_shares(facts)
    latest_share=max(share_candidates,key=lambda x:(x.get("end") or "",x.get("filed") or ""),default=None)
    diluted_shares=latest_share.get("value") if latest_share else None
    last4=quarterly[-4:] if len(quarterly)>=4 else []
    eps_values=[q.get("eps_diluted") for q in last4]
    if len(last4)==4 and all(v is not None for v in eps_values):
        annual_eps=sum(eps_values)
    else:
        matched_shares=[]
        for q in last4:
            matches=[x for x in share_candidates if str(x.get("fy"))==str(q.get("fy")) and str(x.get("fp","")).upper()==str(q.get("quarter")) and 70<=x.get("days",0)<=120]
            if matches: matched_shares.append(max(matches,key=lambda x:(x.get("filed") or "",x.get("end") or ""))["value"])
        avg_shares=(sum(matched_shares)/len(matched_shares)) if matched_shares else diluted_shares
        annual_eps=(ltm.get("net_income")/avg_shares) if ltm.get("net_income") is not None and avg_shares else None
    investor={"quarterly_count":len(quarterly),"latest_period":quarterly[-1].get("period_end") if quarterly else None,
              "revenue_ltm":ltm.get("revenue"),"operating_income_ltm":ltm.get("operating_income"),"fcf_ltm":ltm.get("fcf"),"ocf_ltm":ltm.get("cfo"),
              "ebitda_ltm":None,"annual_eps":annual_eps,"diluted_shares":diluted_shares,
              "fcf_per_share":(ltm.get("fcf")/diluted_shares if ltm.get("fcf") is not None and diluted_shares else None),
              "ocf_per_share":(ltm.get("cfo")/diluted_shares if ltm.get("cfo") is not None and diluted_shares else None)}

    # Inflection signal uses comparable YoY quarters and requires several corroborating metrics.
    inf={"label":"Neutre / mixte","reason":"Pas assez de séries comparables pour confirmer une inflexion."}
    if len(quarterly)>=5:
        cur=quarterly[-1]; prev=quarterly[-5]
        rg=cur.get("revenue_yoy"); mg=(cur.get("gross_margin")-prev.get("gross_margin")) if cur.get("gross_margin") is not None and prev.get("gross_margin") is not None else None
        fg=(cur.get("fcf")/prev.get("fcf")-1) if cur.get("fcf") is not None and prev.get("fcf") not in (None,0) else None
        if rg is not None and mg is not None and fg is not None:
            if rg>0 and mg>0 and fg>0: inf={"label":"Inflexion positive","reason":f"CA YoY {rg*100:.1f}%, marge brute +{mg*100:.1f} pt, FCF YoY {fg*100:.1f}%."}
            elif rg>0 and mg<0 and (fg is None or fg<=0): inf={"label":"Inflexion négative","reason":f"CA encore en croissance ({rg*100:.1f}%) mais pression sur marge brute ({mg*100:.1f} pt) et FCF."}
            else: inf={"label":"Mixte","reason":f"CA YoY {rg*100:.1f}%, variation marge brute {mg*100:+.1f} pt, FCF YoY {fg*100:+.1f}%."}
    signals={"inflection":inf}
    if len(hist)>=2 and hist[1].get("revenue"):
        g=(hist[0].get("revenue")/hist[1]["revenue"]-1) if hist[0].get("revenue") is not None else None
        signals["growth"]={"label":f"{g*100:.1f}% YoY" if g is not None else "—","reason":"Revenue annual growth from primary XBRL facts."}
    if "operating_margin" in ratios: signals["margin"]={"label":f"{ratios['operating_margin']['value']*100:.1f}%","reason":"Current operating margin."}
    if "fcf_margin" in ratios: signals["cash"]={"label":f"{ratios['fcf_margin']['value']*100:.1f}%","reason":"FCF margin calculated from CFO and absolute Capex."}
    if "debt_to_assets" in ratios: signals["balance"]={"label":f"{ratios['debt_to_assets']['value']*100:.1f}%","reason":"Debt / assets using identified debt tags."}
    signals["capital"]={"label":"À analyser","reason":"Share count/SBC/buyback pipeline requires additional facts and notes."}
    required=["revenue","operating_income","cfo","capex","assets","cash","equity"]
    coverage=sum(1 for k in required if k in metrics)/len(required); quality=int(round(100*coverage))
    signals["evidence"]={"label":f"{quality}%","reason":"Coverage of required SEC/XBRL metric families."}
    components={"Evidence":{"score":quality,"reason":"Coverage of required primary financial facts."},"Growth":{"score":50,"reason":"Automated engine intentionally avoids converting one growth observation into a business-quality score."},"Margins":{"score":50,"reason":"Needs multi-year trend and peer comparison."},"Balance":{"score":50,"reason":"Needs maturities, leases and net debt context."},"Moat":{"score":None,"reason":"Qualitative evidence must be sourced separately."}}
    numeric=[v["score"] for v in components.values() if isinstance(v.get("score"),(int,float))]; total=round(sum(numeric)/max(1,len(numeric)))

    facts_rows=[]
    for metric in ["revenue","gross_profit","operating_income","net_income","cfo","capex","assets","cash","equity"]:
        x=latest_annual(facts,metric) or latest_quarter(facts,metric) or latest_instant(facts,metric)
        if x: facts_rows.append({**x,"canonical_metric":metric,**base_source})
    audit=[]
    for name,v in metrics.items():
        audit.append({"id":f"metric:{name}","metric":name,"value":v.get("value"),"method":v.get("method"),"source_name":v.get("source_name"),"filing_url":v.get("filing_url"),"retrieved_at":v.get("retrieved_at"),"tag":v.get("tag"),"period_end":v.get("period_end"),"unit":v.get("unit")})
    for name,v in ratios.items(): audit.append({"id":f"ratio:{name}","metric":name,"value":v.get("value"),"formula":v.get("method"),"source_name":v.get("source_name"),"filing_url":v.get("filing_url"),"retrieved_at":v.get("retrieved_at")})

    return {"company":{"ticker":symbol.upper(),"name":sub.get("name"),"cik":cik,"exchange":sub.get("exchanges",[None])[0] if sub.get("exchanges") else None,"jurisdiction":sub.get("stateOfIncorporation"),"form_family":"SEC/US","taxonomy":"US-GAAP / XBRL"},
            "filings":filings,"facts":facts_rows,"metrics":{k:(v|{"kind":"money"}) for k,v in metrics.items()},"ratios":ratios,"history":hist,"quarterly":quarterly,"investor":investor,"ltm":ltm,"signals":signals,"market":twelve_quote(symbol),
            "quality":{"score":quality,"required":required},"score":{"total":total,"components":components,"interpretation":"Score partiel : ne pas interpréter comme une recommandation tant que qualitative, peers et valorisation ne sont pas documentés."},"audit":audit}

@APP.get("/")
def home():
    return FileResponse(os.path.join(os.path.dirname(__file__), "index.html"))

@APP.get("/health")
def health():
    return {"ok":True,"service":"Equity Intelligence SEC Engine","sec_user_agent_configured":bool(UA and "@" in UA),"retrieved_at":datetime.now(timezone.utc).isoformat()}

@APP.get("/health/sec")
def health_sec():
    try:
        data=sec_get(TICKER_URL)
        return {"ok":True,"sec_access":True,"ticker_count":len(data),"user_agent":UA,"retrieved_at":datetime.now(timezone.utc).isoformat()}
    except Exception as e:
        return {"ok":False,"sec_access":False,"error":str(e),"user_agent":UA,"retrieved_at":datetime.now(timezone.utc).isoformat()}

@APP.get("/api/company")
def company(symbol:str):
    symbol=symbol.strip().upper()
    if not re.fullmatch(r"[A-Z0-9.\-]{1,12}",symbol):
        raise HTTPException(400,"Invalid symbol")
    try:
        return build_company(symbol)
    except requests.HTTPError as e:
        raise HTTPException(502,f"SEC request failed: {e}")
    except Exception as e:
        raise HTTPException(500,str(e))
