
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

def build_company(symbol):
    tickers=sec_get(TICKER_URL)
    rec=None
    for _,v in tickers.items():
        if v.get("ticker","").upper()==symbol.upper():
            rec=v;break
    if not rec: raise HTTPException(404,"Ticker not found in SEC company_tickers.json")
    cik=str(rec["cik_str"]).zfill(10)
    sub=sec_get(SUB_URL.format(cik=cik))
    facts=sec_get(FACTS_URL.format(cik=cik))
    recent=sub.get("filings",{}).get("recent",{})
    forms=["10-K","10-Q","20-F","40-F","6-K"]
    filings=[]
    for i,form in enumerate(recent.get("form",[])):
        if form not in forms: continue
        filings.append({
            "filing_date":recent["filingDate"][i],"form":form,"period":recent["reportDate"][i],
            "accession":recent["accessionNumber"][i],"primary_document":recent["primaryDocument"][i],
            **source_for(cik,{
                "accessionNumber":recent["accessionNumber"][i],
                "primaryDocument":recent["primaryDocument"][i]
            })
        })
        if len(filings)>=20:break

    latest_k = max([f for f in filings if f["form"] in ("10-K","20-F","40-F")],key=lambda f:f["filing_date"],default={})
    base_source=source_for(cik,latest_k)

    metrics={}
    for metric in STANDARD_TAGS:
        x=latest_annual(facts,metric) or latest_quarter(facts,metric) or latest_instant(facts,metric)
        if not x: continue
        metrics[metric] = {**x, **base_source, "method":"direct XBRL fact", "canonical_metric":metric}

    def m(k):return metrics.get(k,{}).get("value")
    revenue=m("revenue"); gp=m("gross_profit"); oi=m("operating_income"); ni=m("net_income"); cfo=m("cfo"); capex=m("capex")
    assets=m("assets"); cash=m("cash"); eq=m("equity"); cd=m("current_debt"); ncd=m("noncurrent_debt")
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
    if invested and oi:
        # Simplified ROIC intentionally disclosed; production version should adjust tax/NOPAT and average invested capital.
        ratio("roic",oi/invested,"Operating income / (Equity + debt - cash), simplified; NOPAT/average invested capital preferred")
    if assets and (cd is not None or ncd is not None):
        debt=(cd or 0)+(ncd or 0);ratio("debt_to_assets",debt/assets,"(Current debt + non-current debt) / assets")

    history={}
    for metric in ["revenue","gross_profit","operating_income","net_income","cfo","capex"]:
        for year,(val,tax,tag,x) in annual_history(facts,metric).items():
            history.setdefault(year,{})[metric]=val
            history[year]["source"]={**base_source,"taxonomy":tax,"tag":tag,"period_end":x.get("end"),"form":x.get("form")}
    hist=[]
    for year in sorted(history,reverse=True)[:10]:
        z=history[year];z["year"]=year;z["fcf"]=(z.get("cfo")-z.get("capex")) if z.get("cfo") is not None and z.get("capex") is not None else None;hist.append(z)

    # Facts: selected canonical facts only, with full provenance.
    facts_rows=[]
    for metric in ["revenue","gross_profit","operating_income","net_income","cfo","capex","assets","cash","equity"]:
        x=latest_annual(facts,metric) or latest_quarter(facts,metric) or latest_instant(facts,metric)
        if x:
            facts_rows.append({**x,"canonical_metric":metric,**base_source})

    # Quality is evidence coverage, not investment quality.
    required=["revenue","operating_income","cfo","capex","assets","cash","equity"]
    coverage=sum(1 for k in required if k in metrics)/len(required)
    quality=int(round(100*coverage))
    signals={}
    if len(hist)>=2 and hist[1].get("revenue"):
        g=(hist[0].get("revenue")/hist[1]["revenue"]-1) if hist[0].get("revenue") is not None else None
        signals["growth"]={"label":f"{g*100:.1f}% YoY" if g is not None else "—","reason":"Revenue annual growth from primary XBRL facts."}
    if "operating_margin" in ratios: signals["margin"]={"label":f"{ratios['operating_margin']['value']*100:.1f}%","reason":"Current operating margin."}
    if "fcf_margin" in ratios: signals["cash"]={"label":f"{ratios['fcf_margin']['value']*100:.1f}%","reason":"FCF margin calculated from CFO and Capex."}
    if "debt_to_assets" in ratios: signals["balance"]={"label":f"{ratios['debt_to_assets']['value']*100:.1f}%","reason":"Debt / assets using identified debt tags."}
    signals["capital"]={"label":"À analyser","reason":"Share count/SBC/buyback pipeline requires additional facts and notes."}
    signals["evidence"]={"label":f"{quality}%","reason":"Coverage of required SEC/XBRL metric families."}

    components={}
    components["Evidence"]= {"score":quality,"reason":"Coverage of required primary financial facts."}
    components["Growth"]= {"score":50,"reason":"Automated engine intentionally avoids converting one growth observation into a business-quality score."}
    components["Margins"]= {"score":50,"reason":"Needs multi-year trend and peer comparison."}
    components["Balance"]= {"score":50,"reason":"Needs maturities, leases and net debt context."}
    components["Moat"]= {"score":None,"reason":"Qualitative evidence must be sourced separately."}
    total=round(sum(v["score"] for v in components.values() if isinstance(v["score"],(int,float)))/max(1,sum(1 for v in components.values() if isinstance(v["score"],(int,float)))))
    audit=[]
    for name,v in metrics.items():
        audit.append({"id":f"metric:{name}","metric":name,"value":v.get("value"),"method":v.get("method"),"source_name":v.get("source_name"),"filing_url":v.get("filing_url"),"retrieved_at":v.get("retrieved_at"),"tag":v.get("tag"),"period_end":v.get("period_end"),"unit":v.get("unit")})
    for name,v in ratios.items():
        audit.append({"id":f"ratio:{name}","metric":name,"value":v.get("value"),"formula":v.get("method"),"source_name":v.get("source_name"),"filing_url":v.get("filing_url"),"retrieved_at":v.get("retrieved_at")})

    return {
        "company":{"ticker":symbol.upper(),"name":sub.get("name"),"cik":cik,"exchange":sub.get("exchanges",[None])[0] if sub.get("exchanges") else None,
                   "jurisdiction":sub.get("stateOfIncorporation"),"form_family":"SEC/US","taxonomy":"US-GAAP / XBRL"},
        "filings":filings,"facts":facts_rows,"metrics":{k:(v|{"kind":"money"}) for k,v in metrics.items()},
        "ratios":ratios,"history":hist,"signals":signals,"quality":{"score":quality,"required":required},
        "score":{"total":total,"components":components,"interpretation":"Score partiel : ne pas interpréter comme une recommandation tant que qualitative, peers et valorisation ne sont pas documentés."},
        "audit":audit
    }

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
