import os, re, time
from datetime import datetime, timezone
from typing import Any, Dict
import requests
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware

APP = FastAPI(title="Equity Intelligence SEC Engine", version="0.7")
app = APP
APP.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
UA = os.getenv("SEC_USER_AGENT", "Equity Intelligence/1.0 (guets2000@hotmail.com)")
HEADERS = {"User-Agent": UA, "Accept-Encoding": "gzip, deflate"}
TIMEOUT = 30
TICKER_URL = "https://www.sec.gov/files/company_tickers.json"
SUB_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"

STANDARD_TAGS = {
    "revenue":["Revenues","RevenueFromContractWithCustomerExcludingAssessedTax","SalesRevenueNet"],
    "gross_profit":["GrossProfit"], "operating_income":["OperatingIncomeLoss"], "net_income":["NetIncomeLoss"],
    "cfo":["NetCashProvidedByUsedInOperatingActivities"],
    "capex":["PaymentsToAcquirePropertyPlantAndEquipment","PaymentsToAcquireProductiveAssets"],
    "assets":["Assets"], "cash":["CashAndCashEquivalentsAtCarryingValue","CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"],
    "equity":["StockholdersEquity","StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"],
    "current_debt":["ShortTermBorrowings","LongTermDebtCurrent","LongTermDebtAndFinanceLeaseObligationsCurrent"],
    "noncurrent_debt":["LongTermDebtNoncurrent","LongTermDebtAndFinanceLeaseObligationsNoncurrent"],
    "d_and_a":["DepreciationDepletionAndAmortization","DepreciationDepletionAndAmortizationPropertyPlantAndEquipment","DepreciationAmortizationAndAccretionNet"],
    "shares_diluted":["WeightedAverageNumberOfDilutedSharesOutstanding"],
    "eps_diluted":["EarningsPerShareDiluted"]
}

session=requests.Session(); session.headers.update(HEADERS)
_last_sec=0.0

def sec_get(url):
    global _last_sec
    delay=0.25-max(0,time.time()-_last_sec)
    if delay>0: time.sleep(delay)
    for attempt in range(5):
        r=session.get(url,timeout=TIMEOUT); _last_sec=time.time()
        if r.status_code in (429,503):
            time.sleep(min(2**attempt,8)); continue
        r.raise_for_status(); return r.json()
    r.raise_for_status()

def public_get(url, timeout=15):
    r=requests.get(url,timeout=timeout,headers={"User-Agent":"Equity Intelligence market-data client"}); r.raise_for_status(); return r.json()

def val(v):
    try:return float(v)
    except:return None

def concepts(facts,metric):
    out=[]
    for tax in ("us-gaap","ifrs-full"):
        for tag in STANDARD_TAGS.get(metric,[]):
            meta=facts.get("facts",{}).get(tax,{}).get(tag)
            if meta: out.append((tax,tag,meta))
    return out

def duration_rows(facts,metric):
    out=[]
    for tax,tag,meta in concepts(facts,metric):
        units=meta.get("units",{}); arr=units.get("USD") or units.get("shares") or units.get("USD/shares") or (next(iter(units.values())) if units else [])
        for x in arr:
            if x.get("form") not in ("10-Q","10-K","20-F","40-F"): continue
            if not x.get("start") or not x.get("end"): continue
            try: days=(datetime.fromisoformat(x["end"])-datetime.fromisoformat(x["start"])).days
            except: continue
            v=val(x.get("val"))
            if v is None: continue
            y=(x.get("end") or "")[:4]
            out.append({**x,"value":v,"days":days,"taxonomy":tax,"tag":tag,"metric":metric,"year":y})
    return out

def instant_rows(facts,metric):
    out=[]
    for tax,tag,meta in concepts(facts,metric):
        units=meta.get("units",{}); arr=units.get("USD") or units.get("shares") or (next(iter(units.values())) if units else [])
        for x in arr:
            if x.get("form") not in ("10-Q","10-K","20-F","40-F"): continue
            v=val(x.get("val")); end=x.get("end")
            if v is not None and end: out.append({**x,"value":v,"taxonomy":tax,"tag":tag,"metric":metric})
    return out

def filing_source(cik, f):
    acc=f.get("accessionNumber"); doc=f.get("primaryDocument")
    url=f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc.replace('-','')}/{doc}" if acc and doc else None
    return {"source_name":"SEC EDGAR","filing_url":url,"accession":acc,"retrieved_at":datetime.now(timezone.utc).isoformat()}

def annual_history(facts,metric):
    best={}
    for x in duration_rows(facts,metric):
        if x["form"]!="10-K" or not (320<=x["days"]<=410): continue
        y=x["end"][:4]
        if y not in best or str(x.get("filed",""))>str(best[y].get("filed","")): best[y]=x
    return best

def quarter_series(facts,metric):
    rows=duration_rows(facts,metric)
    by_end={}
    # Prefer direct quarter observations (10-Q and ~3 month durations). For cash flow, filings often expose only cumulative values.
    for x in rows:
        if 70<=x["days"]<=110:
            by_end[x["end"]]=max([y for y in rows if y["end"]==x["end"] and 70<=y["days"]<=110],key=lambda y:str(y.get("filed","")))
    # Cumulative 6m/9m to reconstruct Q2/Q3.
    all_by_fy={}
    for x in rows:
        if x["form"] in ("10-Q","10-K"):
            all_by_fy.setdefault(str(x.get("fy") or x["end"][:4]),[]).append(x)
    derived={}
    for fy,arr in all_by_fy.items():
        buckets={}
        for x in arr:
            if 150<=x["days"]<=205: buckets[180]=max([buckets[180],x],key=lambda z:str(z.get("filed",""))) if buckets.get(180) else x
            elif 235<=x["days"]<=305: buckets[270]=max([z for z in arr if 235<=z["days"]<=305],key=lambda z:str(z.get("filed","")))
            elif 70<=x["days"]<=110: buckets[90]=max([z for z in arr if 70<=z["days"]<=110],key=lambda z:str(z.get("filed","")))
        # Use latest 10-Q cumulative facts by period_end.
        q1=buckets.get(90); h1=buckets.get(180); m9=buckets.get(270)
        if q1: derived[q1["end"]]=q1
        if h1 and q1: derived[h1["end"]]={"value":h1["value"]-q1["value"],"period_end":h1["end"],"fy":fy,"fp":"Q2","form":h1["form"],"filed":h1.get("filed"),"taxonomy":h1["taxonomy"],"tag":h1["tag"],"metric":metric,"method":"6M cumulative - Q1"}
        if m9 and h1: derived[m9["end"]]={"value":m9["value"]-h1["value"],"period_end":m9["end"],"fy":fy,"fp":"Q3","form":m9["form"],"filed":m9.get("filed"),"taxonomy":m9["taxonomy"],"tag":m9["tag"],"metric":metric,"method":"9M cumulative - 6M cumulative"}
        # Q4 from FY if we have the annual fact and three preceding quarters.
        annual=annual_history(facts,metric).get(fy)
        if annual:
            prior=[derived[e]["value"] for e in sorted(derived) if str(derived[e].get("fy"))==fy][-3:]
            if len(prior)==3:
                end=annual["end"]; derived[end]={"value":annual["value"]-sum(prior),"period_end":end,"fy":fy,"fp":"Q4","form":"10-K","filed":annual.get("filed"),"taxonomy":annual["taxonomy"],"tag":annual["tag"],"metric":metric,"method":"FY - Q1 - Q2 - Q3"}
    # Direct facts override reconstructions at same end.
    merged={**derived,**{k:{"value":v["value"],**v,"period_end":k,"method":"direct ~quarter fact"} for k,v in by_end.items()}}
    return sorted(merged.values(),key=lambda x:x["period_end"])[-16:]

def build_quarterly(facts):
    ms=("revenue","gross_profit","operating_income","net_income","cfo","capex","shares_diluted","eps_diluted","d_and_a")
    q={}
    for metric in ms:
        for x in quarter_series(facts,metric):
            end=x["period_end"]; q.setdefault(end,{})[metric]=x["value"]; q[end].setdefault("_meta",{})[metric]=x
    rows=[]; dates=sorted(q)
    for i,end in enumerate(dates):
        r={k:v for k,v in q[end].items() if k!="_meta"}; r["period_end"]=end
        meta=next(iter(q[end].get("_meta",{}).values()),{})
        r["fy"]=meta.get("fy") or end[:4]; fp=meta.get("fp")
        r["quarter"]="Q4" if fp=="FY" else (fp if fp in ("Q1","Q2","Q3") else None)
        if r.get("capex") is not None: r["capex_outflow"]=abs(r["capex"])
        if r.get("cfo") is not None and r.get("capex") is not None: r["fcf"]=r["cfo"]-abs(r["capex"])
        rev=r.get("revenue");
        if rev:
            for num,key in (("gross_profit","gross_margin"),("operating_income","operating_margin"),("net_income","net_margin"),("fcf","fcf_margin")): r[key]=r.get(num)/rev if r.get(num) is not None else None
        r["fcf_conversion"]=r.get("fcf")/r.get("net_income") if r.get("fcf") is not None and r.get("net_income") not in (None,0) else None
        if i: r["revenue_qoq"]=r.get("revenue")/rows[-1].get("revenue")-1 if r.get("revenue") is not None and rows[-1].get("revenue") else None
        if i>=4: r["revenue_yoy"]=r.get("revenue")/rows[i-4].get("revenue")-1 if r.get("revenue") is not None and rows[i-4].get("revenue") else None
        # Diluted EPS: prefer reported XBRL; otherwise calculate net income / diluted shares.
        if r.get("eps_diluted") is None and r.get("net_income") is not None and r.get("shares_diluted") not in (None,0): r["eps_diluted"]=r["net_income"]/r["shares_diluted"]
        rows.append(r)
    return rows[-16:]

def market_quote(symbol):
    # Twelve Data is preferred when a personal API key is supplied; otherwise Yahoo quote endpoint is used as fallback.
    key=os.getenv("TWELVEDATA_API_KEY")
    if key:
        try:
            d=public_get(f"https://api.twelvedata.com/quote?symbol={symbol.upper()}&apikey={key}")
            if d.get("close"):
                px=float(d["close"]); prev=float(d.get("previous_close") or px)
                return {"available":True,"provider":"Twelve Data","price":px,"previous_close":prev,"change_pct":(px/prev-1) if prev else None,"currency":d.get("currency"),"timestamp":d.get("timestamp"),"disclaimer":"Donnée de marché fournie par Twelve Data."}
        except Exception as e: pass
    try:
        d=public_get(f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol.upper()}?range=1d&interval=1m&includePrePost=false")
        r=(d.get("chart",{}).get("result") or [None])[0]; meta=(r or {}).get("meta",{}); px=meta.get("regularMarketPrice"); prev=meta.get("previousClose")
        if px is not None: return {"available":True,"provider":"Yahoo Finance","price":px,"previous_close":prev,"change_pct":(px/prev-1) if prev else None,"currency":meta.get("currency"),"timestamp":meta.get("regularMarketTime"),"disclaimer":"Cours indicatif : selon le fournisseur et la place, la donnée peut être différée. Pour une vraie diffusion temps réel, connecter Twelve Data via TWELVEDATA_API_KEY."}
    except Exception as e: return {"available":False,"provider":"Yahoo Finance","reason":str(e)}
    return {"available":False,"provider":"Yahoo Finance","reason":"Cours indisponible"}

def build_company(symbol):
    tickers=sec_get(TICKER_URL); rec=next((v for v in tickers.values() if v.get("ticker","").upper()==symbol.upper()),None)
    if not rec: raise HTTPException(404,"Ticker not found in SEC company_tickers.json")
    cik=str(rec["cik_str"]).zfill(10); sub=sec_get(SUB_URL.format(cik=cik)); facts=sec_get(FACTS_URL.format(cik=cik)); recent=sub.get("filings",{}).get("recent",{})
    filings=[]
    for i,form in enumerate(recent.get("form",[])):
        if form not in ("10-K","10-Q","20-F","40-F","6-K"): continue
        f={"filing_date":recent["filingDate"][i],"form":form,"period":recent["reportDate"][i],"accession":recent["accessionNumber"][i],"primary_document":recent["primaryDocument"][i]}
        f.update(filing_source(cik,{"accessionNumber":f["accession"],"primaryDocument":f["primary_document"]})); filings.append(f)
        if len(filings)>=20: break
    latest_k=max([f for f in filings if f["form"] in ("10-K","20-F","40-F")],key=lambda x:x["filing_date"],default={}); base=filing_source(cik,{"accessionNumber":latest_k.get("accession"),"primaryDocument":latest_k.get("primary_document")})
    quarterly=build_quarterly(facts)
    history={}
    for metric in ("revenue","gross_profit","operating_income","net_income","cfo","capex"):
        for y,x in annual_history(facts,metric).items(): history.setdefault(y,{})[metric]=x["value"]; history[y]["source"]={**base,"taxonomy":x["taxonomy"],"tag":x["tag"],"period_end":x["end"],"form":x["form"]}
    hist=[]
    for y in sorted(history,reverse=True)[:10]:
        z=history[y]; z["year"]=y; z["fcf"]=z.get("cfo")-abs(z.get("capex")) if z.get("cfo") is not None and z.get("capex") is not None else None; hist.append(z)
    # Annual/LTM investor values
    last4=quarterly[-4:] if len(quarterly)>=4 else []
    def s4(k):
        vals=[x.get(k) for x in last4]; return sum(vals) if len(vals)==4 and all(v is not None for v in vals) else None
    eps=last4[-1].get("eps_diluted") if last4 else None
    shares=sum(x.get("shares_diluted") for x in last4 if x.get("shares_diluted") is not None)/len([x for x in last4 if x.get("shares_diluted") is not None]) if last4 and any(x.get("shares_diluted") is not None for x in last4) else None
    fcf_ltm=s4("fcf"); cfo_ltm=s4("cfo")
    investor={"quarterly_count":len(quarterly),"latest_period":quarterly[-1]["period_end"] if quarterly else None,"annual_eps":((s4("net_income")/shares) if s4("net_income") is not None and shares else eps),"fcf_per_share":(fcf_ltm/shares if fcf_ltm is not None and shares else None),"ocf_per_share":(cfo_ltm/shares if cfo_ltm is not None and shares else None),"diluted_shares":shares,"fcf_ltm":fcf_ltm,"ocf_ltm":cfo_ltm,"revenue_ltm":s4("revenue"),"operating_income_ltm":s4("operating_income"),"ebitda_ltm":(s4("operating_income") + s4("d_and_a") if s4("operating_income") is not None and s4("d_and_a") is not None else None)}
    metrics={}
    for m in ("revenue","gross_profit","operating_income","net_income","cfo","capex","assets","cash","equity","current_debt","noncurrent_debt"):
        rr=quarter_series(facts,m) if m in ("revenue","gross_profit","operating_income","net_income","cfo","capex") else instant_rows(facts,m)
        x=rr[-1] if rr else None
        if x: metrics[m]={"value":x["value"],"year":x.get("end",x.get("period_end"))[:4],"taxonomy":x.get("taxonomy"),"tag":x.get("tag"),"form":x.get("form"),"period_end":x.get("end",x.get("period_end")),**base,"method":"XBRL fact / latest available"}
    # Use LTM metrics for headline display.
    if investor["revenue_ltm"] is not None: metrics["revenue_ltm"]={"value":investor["revenue_ltm"],**base,"method":"sum of latest 4 reported/reconstructed quarters"}
    ratios={}; rev=investor["revenue_ltm"]; oi=investor["operating_income_ltm"]; fcf=investor["fcf_ltm"]; ni=s4("net_income"); gp=s4("gross_profit"); cfo=cfo_ltm
    if rev:
        for name,v,formula in [("gross_margin",gp,"gross profit LTM / revenue LTM"),("operating_margin",oi,"operating income LTM / revenue LTM"),("net_margin",ni,"net income LTM / revenue LTM"),("fcf_margin",fcf,"FCF LTM / revenue LTM"),("cfo_margin",cfo,"CFO LTM / revenue LTM")]:
            if v is not None: ratios[name]={"value":v/rev,"method":formula,**base}
    eq=metrics.get("equity",{}).get("value");
    if eq and ni: ratios["roe"]={"value":ni/eq,"method":"Net income LTM / ending equity (simplified)",**base}
    cd=metrics.get("current_debt",{}).get("value"); ncd=metrics.get("noncurrent_debt",{}).get("value"); cash=metrics.get("cash",{}).get("value");
    if eq is not None and cd is not None and ncd is not None and cash is not None and oi: ratios["roic"]={"value":oi/(eq+cd+ncd-cash),"method":"Operating income LTM / (equity + debt - cash), simplified",**base}
    if cd is not None or ncd is not None:
        assets=metrics.get("assets",{}).get("value"); debt=(cd or 0)+(ncd or 0)
        if assets: ratios["debt_to_assets"]={"value":debt/assets,"method":"Debt / assets",**base}
    market=market_quote(symbol)
    if market.get("available") and investor.get("diluted_shares"):
        px=market["price"]; sh=investor["diluted_shares"]
        if investor.get("annual_eps") not in (None,0): ratios["pe"]={"value":px/investor["annual_eps"],"method":"Price / latest diluted EPS",**market}
        if investor.get("fcf_per_share") not in (None,0): ratios["p_fcf"]={"value":px/investor["fcf_per_share"],"method":"Price / FCF per share LTM",**market}
        if investor.get("ocf_per_share") not in (None,0): ratios["p_ocf"]={"value":px/investor["ocf_per_share"],"method":"Price / OCF per share LTM",**market}
    inf={"label":"Données trimestrielles insuffisantes","reason":"Il faut au moins 5 trimestres pour une détection d’inflexion fiable."}
    if len(quarterly)>=5:
        a,b=quarterly[-2],quarterly[-1]; revacc=a.get("revenue_yoy") is not None and b.get("revenue_yoy") is not None and b["revenue_yoy"]>a["revenue_yoy"]; gmup=a.get("gross_margin") is not None and b.get("gross_margin") is not None and b["gross_margin"]>a["gross_margin"]; fcfacc=a.get("fcf") is not None and b.get("fcf") is not None and b["fcf"]>a["fcf"]
        if revacc and gmup and fcfacc: inf={"label":"Signal positif : qualité de croissance en amélioration","reason":"CA YoY accélère + marge brute augmente + FCF augmente."}
        elif b.get("revenue_yoy",0)>0 and a.get("gross_margin") is not None and b.get("gross_margin") is not None and b["gross_margin"]<a["gross_margin"] and a.get("fcf") not in (None,0) and b.get("fcf") is not None and abs(b["fcf"]/a["fcf"]-1)<0.05: inf={"label":"Signal de dégradation : qualité de croissance en baisse","reason":"CA progresse mais marge brute baisse et FCF stagne."}
    signals={"inflection":inf,"growth":{"label":f"{(hist[0]['revenue']/hist[1]['revenue']-1)*100:.1f}% YoY" if len(hist)>1 and hist[0].get("revenue") and hist[1].get("revenue") else "—","reason":"Croissance annuelle issue de XBRL."}}
    facts_rows=[]
    for m in ("revenue","gross_profit","operating_income","net_income","cfo","capex","assets","cash","equity"):
        rr=quarter_series(facts,m) if m in ("revenue","gross_profit","operating_income","net_income","cfo","capex") else instant_rows(facts,m)
        if rr: facts_rows.append({**rr[-1],"canonical_metric":m,**base})
    quality=int(round(100*sum(k in metrics for k in ("revenue","operating_income","cfo","capex","assets","cash","equity"))/7))
    audit=[{"id":f"metric:{k}","metric":k,"value":v.get("value"),"method":v.get("method"),"source_name":v.get("source_name"),"filing_url":v.get("filing_url"),"period_end":v.get("period_end"),"tag":v.get("tag")} for k,v in metrics.items()]
    return {"company":{"ticker":symbol.upper(),"name":sub.get("name"),"cik":cik,"exchange":(sub.get("exchanges") or [None])[0],"jurisdiction":sub.get("stateOfIncorporation"),"form_family":"SEC/US","taxonomy":"US-GAAP / XBRL"},"filings":filings,"facts":facts_rows,"metrics":metrics,"ratios":ratios,"history":hist,"quarterly":quarterly,"investor":investor,"market":market,"signals":signals,"quality":{"score":quality},"score":{"total":quality,"components":{},"interpretation":"Score de qualité des données, pas une recommandation."},"audit":audit}

@APP.get("/")
def home(): return FileResponse(os.path.join(os.path.dirname(__file__),"index.html"))
@APP.get("/health")
def health(): return {"ok":True,"service":"Equity Intelligence SEC Engine","sec_user_agent_configured":bool(UA and "@" in UA),"retrieved_at":datetime.now(timezone.utc).isoformat()}
@APP.get("/health/sec")
def health_sec():
    try: d=sec_get(TICKER_URL); return {"ok":True,"sec_access":True,"ticker_count":len(d),"retrieved_at":datetime.now(timezone.utc).isoformat()}
    except Exception as e: return {"ok":False,"sec_access":False,"error":str(e)}
@APP.get("/api/company")
def company(symbol:str):
    symbol=symbol.strip().upper()
    if not re.fullmatch(r"[A-Z0-9.\-]{1,12}",symbol): raise HTTPException(400,"Invalid symbol")
    try:return build_company(symbol)
    except requests.HTTPError as e: raise HTTPException(502,f"SEC/market request failed: {e}")
    except Exception as e: raise HTTPException(500,str(e))
