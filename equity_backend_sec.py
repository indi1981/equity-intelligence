"""Equity Intelligence V6 — SEC/XBRL + Twelve Data backend.

Primary accounting data: SEC EDGAR/companyfacts, server-side.
Market price: Twelve Data when TWELVEDATA_API_KEY is configured.
No analyst consensus or valuation estimate is fabricated.
"""
import os, re, time, math
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
import requests
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware

APP = FastAPI(title="Equity Intelligence Investor Engine", version="0.6")
app = APP
APP.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

UA = os.getenv("SEC_USER_AGENT", "Equity Intelligence (guets2000@hotmail.com)")
HEADERS = {"User-Agent": UA, "Accept-Encoding": "gzip, deflate"}
TIMEOUT = 30
TICKER_URL = "https://www.sec.gov/files/company_tickers.json"
SUB_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
TD_URL = "https://api.twelvedata.com/quote"

STANDARD_TAGS = {
    "revenue": ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet"],
    "gross_profit": ["GrossProfit"],
    "operating_income": ["OperatingIncomeLoss"],
    "net_income": ["NetIncomeLoss", "ProfitLoss"],
    "cfo": ["NetCashProvidedByUsedInOperatingActivities"],
    "capex": ["PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets"],
    "assets": ["Assets"],
    "cash": ["CashAndCashEquivalentsAtCarryingValue", "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"],
    "equity": ["StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"],
    "current_debt": ["ShortTermBorrowings", "LongTermDebtCurrent", "LongTermDebtAndFinanceLeaseObligationsCurrent"],
    "noncurrent_debt": ["LongTermDebtNoncurrent", "LongTermDebtAndFinanceLeaseObligationsNoncurrent"],
    "d_and_a": ["DepreciationDepletionAndAmortization", "DepreciationDepletionAndAmortizationPropertyPlantAndEquipment", "DepreciationAmortizationAndAccretionNet"],
    "diluted_shares": ["WeightedAverageNumberOfDilutedSharesOutstanding"],
    "diluted_eps": ["EarningsPerShareDiluted"],
    "basic_shares": ["WeightedAverageNumberOfSharesOutstandingBasic"],
}

DURATION_METRICS = {"revenue","gross_profit","operating_income","net_income","cfo","capex","d_and_a","diluted_shares","diluted_eps","basic_shares"}
INSTANT_METRICS = {"assets","cash","equity","current_debt","noncurrent_debt"}


def sec_get(url):
    last = None
    for attempt in range(5):
        if attempt: time.sleep(min(1.5 * attempt, 5))
        try:
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            last = r
            if r.status_code in (429, 503): continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException:
            if attempt == 4: raise
    if last is not None: last.raise_for_status()
    raise RuntimeError("SEC request failed")


def to_float(v):
    try: return float(v)
    except Exception: return None


def find_concepts(facts, metric):
    out=[]
    for tax in ["us-gaap", "ifrs-full"]:
        group=facts.get("facts",{}).get(tax,{})
        for tag in STANDARD_TAGS.get(metric,[]):
            if tag in group: out.append((tax,tag,group[tag]))
    return out


def all_fact_rows(facts, metric):
    rows=[]
    for tax,tag,meta in find_concepts(facts,metric):
        for unit, arr in meta.get("units",{}).items():
            for x in arr:
                if x.get("form") not in ("10-K","10-Q","20-F","40-F","6-K"): continue
                v=to_float(x.get("val"))
                if v is None or not x.get("end"): continue
                y=dict(x)
                y.update({"value":v,"taxonomy":tax,"tag":tag,"unit":unit})
                rows.append(y)
    # latest filing wins for duplicate fact periods
    rows.sort(key=lambda x:(x.get("end",""),x.get("filed","")), reverse=True)
    return rows


def source_url(cik, accession, doc=None):
    if not accession: return None
    clean=str(accession).replace("-","")
    base=f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{clean}/"
    return base + str(doc) if doc else base

def source_meta(cik, accession=None, doc=None, taxonomy=None, tag=None, period_end=None, method=None):
    return {
        "sourceId": f"sec:{cik}:{accession or ''}", "source_name":"SEC EDGAR",
        "filing_url":source_url(cik,accession,doc), "accession":accession,
        "taxonomy":taxonomy, "tag":tag, "period_end":period_end,
        "method":method, "retrieved_at":datetime.now(timezone.utc).isoformat()
    }


def latest_duration(facts, metric, annual=False):
    rows=all_fact_rows(facts,metric)
    candidates=[]
    for x in rows:
        if not x.get("start"): continue
        try: days=(datetime.fromisoformat(x["end"])-datetime.fromisoformat(x["start"])).days
        except Exception: continue
        if annual and 320<=days<=410 and x.get("form") in ("10-K","20-F","40-F"): candidates.append(x)
        elif not annual and 60<=days<=120: candidates.append(x)
    return candidates[0] if candidates else None


def latest_instant(facts, metric):
    rows=all_fact_rows(facts,metric)
    return rows[0] if rows else None


def row_obj(cik,x,method="direct XBRL fact"):
    return {"value":x["value"],"taxonomy":x.get("taxonomy"),"tag":x.get("tag"),"unit":x.get("unit"),"form":x.get("form"),
            "period_start":x.get("start"),"period_end":x.get("end"),"filing_date":x.get("filed"),"fy":x.get("fy"),"fp":x.get("fp"),
            **source_meta(cik,x.get("accn"),x.get("doc") or x.get("primaryDocument"),x.get("taxonomy"),x.get("tag"),x.get("end"),method)}


def annual_history(facts, metric, cik):
    rows=all_fact_rows(facts,metric); best={}
    for x in rows:
        if not x.get("start") or x.get("form") not in ("10-K","20-F","40-F"): continue
        try: days=(datetime.fromisoformat(x["end"])-datetime.fromisoformat(x["start"])).days
        except Exception: continue
        if not 320<=days<=410: continue
        year=x["end"][:4]
        if year not in best: best[year]=row_obj(cik,x)
    return best


def duration_candidates_by_period(facts, metric):
    # Keyed by (fiscal year, end, approximate duration class), newest filing first.
    out={}
    for x in all_fact_rows(facts,metric):
        if not x.get("start"): continue
        try: days=(datetime.fromisoformat(x["end"])-datetime.fromisoformat(x["start"])).days
        except Exception: continue
        if not 50<=days<=410: continue
        fy=str(x.get("fy") or x["end"][:4])
        key=(fy,x["end"],days//15)
        if key not in out: out[key]=x
    return list(out.values())


def fiscal_quarter_records(facts, metric, cik):
    """Reconstruct quarterly duration facts.
    Direct ~3-month facts are preferred. For YTD cash-flow facts:
    Q2=6M-Q1, Q3=9M-6M, Q4=FY-Q1-Q2-Q3.
    """
    rows=duration_candidates_by_period(facts,metric)
    # Weighted-average shares are not additive YTD values. Never derive Q2/Q3/Q4 by subtraction.
    if metric in ("diluted_shares","basic_shares"):
        direct_only={}
        for x in rows:
            try: days=(datetime.fromisoformat(x["end"])-datetime.fromisoformat(x["start"])).days
            except Exception: continue
            if 60<=days<=120:
                fy=str(x.get("fy") or x["end"][:4]); fp=str(x.get("fp") or "").upper()
                q=int(fp[1]) if re.match(r"Q[1-4]",fp) else None
                if q in (1,2,3,4): direct_only[(fy,q)]=x
        return {k:dict(value=v["value"],q=k[1],fy=k[0],period_end=v.get("end"),period_start=v.get("start"),source=row_obj(cik,v,"direct quarterly XBRL fact"),derived=False) for k,v in direct_only.items()}
    groups={}
    for x in rows:
        fy=str(x.get("fy") or x["end"][:4]); groups.setdefault(fy,[]).append(x)
    result={}
    for fy, arr in groups.items():
        # keep latest fiscal-year facts; derive quarter identity from fp when available, otherwise duration
        direct=[]; cum=[]; annual=[]
        for x in arr:
            try: days=(datetime.fromisoformat(x["end"])-datetime.fromisoformat(x["start"])).days
            except Exception: continue
            if 60<=days<=120: direct.append(x)
            elif 120<days<=210: cum.append((2,x))
            elif 210<days<=310: cum.append((3,x))
            elif 320<=days<=410: annual.append(x)
        # direct facts: fp Q1/Q2/Q3 if explicit, otherwise assign by month/end ordering
        byq={}
        for x in direct:
            fp=str(x.get("fp") or "").upper()
            q=int(fp[1]) if re.match(r"Q[1-4]",fp) else None
            if q in (1,2,3,4): byq[q]=x
        if not byq:
            for i,x in enumerate(sorted(direct,key=lambda z:z["end"])):
                if i<4: byq[i+1]=x
        # cumulative facts
        cum2=next((x for q,x in cum if q==2),None)
        cum3=next((x for q,x in cum if q==3),None)
        ann=sorted(annual,key=lambda z:z["end"],reverse=True)[0] if annual else None
        vals={}
        for q,x in byq.items(): vals[q]=(x["value"],x,"direct quarterly XBRL fact")
        if 2 not in vals and cum2 and 1 in vals:
            vals[2]=(cum2["value"]-vals[1][0],cum2,"derived: 6M YTD minus Q1")
        if 3 not in vals and cum3:
            six=cum2["value"] if cum2 else (vals[1][0]+vals[2][0] if 1 in vals and 2 in vals else None)
            if six is not None: vals[3]=(cum3["value"]-six,cum3,"derived: 9M YTD minus 6M YTD")
        if 4 not in vals and ann:
            qsum=sum(vals[q][0] for q in (1,2,3) if q in vals)
            if len([q for q in (1,2,3) if q in vals])==3: vals[4]=(ann["value"]-qsum,ann,"derived: FY annual minus Q1-Q3")
        for q,(value,src,method) in vals.items():
            # use period end from direct/cumulative/annual source; fiscal year from source if available
            result[(fy,q)]=dict(value=value, q=q, fy=fy, period_end=src.get("end"), period_start=src.get("start"),
                source=row_obj(cik,src,method), derived=(method.startswith("derived")))
    # If fy grouping was unreliable, duplicate fiscal years can happen; keep chronological unique periods.
    return result


def build_quarterly(facts,cik):
    metrics=["revenue","gross_profit","operating_income","net_income","cfo","capex","d_and_a","diluted_shares","diluted_eps"]
    maps={m:fiscal_quarter_records(facts,m,cik) for m in metrics}
    keys=set()
    for mp in maps.values(): keys.update(mp.keys())
    rows=[]
    for fy,q in keys:
        z={"fy":fy,"quarter":q}
        sources=[]
        for m in metrics:
            r=maps[m].get((fy,q))
            if r:
                val=r["value"]
                if m=="capex": val=abs(val)
                z[m]=val; sources.append(r["source"])
        if z.get("revenue") is None: continue
        # EPS: prefer reported quarterly EPS. If absent, calculate from NI / diluted shares for that quarter.
        if z.get("diluted_eps") is None and z.get("net_income") is not None and z.get("diluted_shares") not in (None,0):
            z["diluted_eps"]=z["net_income"]/z["diluted_shares"]; z["eps_method"]="derived: net income / diluted weighted-average shares"
        elif z.get("diluted_eps") is not None: z["eps_method"]="reported diluted EPS"
        z["fcf"]=(z["cfo"]-z["capex"]) if z.get("cfo") is not None and z.get("capex") is not None else None
        if z.get("gross_profit") is not None: z["gross_margin"]=z["gross_profit"]/z["revenue"]
        if z.get("operating_income") is not None: z["operating_margin"]=z["operating_income"]/z["revenue"]
        if z.get("net_income") is not None: z["net_margin"]=z["net_income"]/z["revenue"]
        if z.get("fcf") is not None: z["fcf_margin"]=z["fcf"]/z["revenue"]
        if z.get("fcf") is not None and z.get("net_income") not in (None,0): z["fcf_conversion"]=z["fcf"]/z["net_income"]
        z["period_end"]=max([s.get("period_end") for s in sources if s.get("period_end")], default=None)
        z["source_count"]=len(sources)
        z["derived_fields"]=sorted(set(s.get("method") for s in sources if s.get("method"," ").startswith("derived")))
        rows.append(z)
    rows.sort(key=lambda x:(x.get("period_end") or "", int(x.get("quarter",0))))
    # QoQ and YoY use chronological quarters. We deliberately don't invent missing prior periods.
    for i,z in enumerate(rows):
        if i>0 and rows[i-1].get("revenue") not in (None,0): z["revenue_qoq"]=z["revenue"]/rows[i-1]["revenue"]-1
        if i>=4 and rows[i-4].get("revenue") not in (None,0): z["revenue_yoy"]=z["revenue"]/rows[i-4]["revenue"]-1
    return rows[-20:]


def instant_latest(facts,metric,cik):
    x=latest_instant(facts,metric)
    return row_obj(cik,x) if x else None


def market_quote(symbol):
    key=os.getenv("TWELVEDATA_API_KEY")
    if not key:
        return {"available":False,"provider":"Twelve Data","reason":"TWELVEDATA_API_KEY non configurée sur Render."}
    try:
        r=requests.get(TD_URL,params={"symbol":symbol,"apikey":key},timeout=15)
        r.raise_for_status(); j=r.json()
        if j.get("status")=="error" or j.get("code"):
            return {"available":False,"provider":"Twelve Data","reason":j.get("message") or "Twelve Data a retourné une erreur."}
        price=to_float(j.get("close"))
        if price is None: price=to_float(j.get("price"))
        return {"available":price is not None,"provider":"Twelve Data","price":price,
                "previous_close":to_float(j.get("previous_close")),"change_pct":to_float(j.get("percent_change")),
                "change":to_float(j.get("change")),"currency":j.get("currency"),"exchange":j.get("exchange"),
                "timestamp":j.get("timestamp"),"datetime":j.get("datetime"),"is_extended_hours":j.get("is_extended_hours",False)}
    except Exception as e:
        return {"available":False,"provider":"Twelve Data","reason":f"Erreur Twelve Data: {e}"}


def build_company(symbol):
    tickers=sec_get(TICKER_URL); rec=next((v for v in tickers.values() if v.get("ticker","").upper()==symbol.upper()),None)
    if not rec: raise HTTPException(404,"Ticker not found in SEC company_tickers.json")
    cik=str(rec["cik_str"]).zfill(10); sub=sec_get(SUB_URL.format(cik=cik)); facts=sec_get(FACTS_URL.format(cik=cik))
    recent=sub.get("filings",{}).get("recent",{}); filings=[]
    allowed={"10-K","10-Q","20-F","40-F","6-K"}
    for i,form in enumerate(recent.get("form",[])):
        if form not in allowed: continue
        acc=recent["accessionNumber"][i]; doc=recent["primaryDocument"][i]
        filings.append({"filing_date":recent["filingDate"][i],"form":form,"period":recent["reportDate"][i],"accession":acc,"primary_document":doc,
                        **source_meta(cik,acc,doc)})
        if len(filings)>=30: break
    latest_k=next((f for f in filings if f["form"] in ("10-K","20-F","40-F")),filings[0] if filings else {})
    base_source=source_meta(cik,latest_k.get("accession"),latest_k.get("primary_document"))

    metrics={}
    for metric in STANDARD_TAGS:
        x=latest_duration(facts,metric,annual=True) if metric in DURATION_METRICS else latest_instant(facts,metric)
        if not x: x=latest_duration(facts,metric,annual=False) if metric in DURATION_METRICS else None
        if x: metrics[metric]=row_obj(cik,x,"latest annual/instant XBRL fact")

    q=build_quarterly(facts,cik)
    last4=q[-4:]
    def sumq(k):
        vals=[x.get(k) for x in last4 if x.get(k) is not None]
        return sum(vals) if len(vals)==len(last4) else None
    rev_ltm=sumq("revenue"); gp_ltm=sumq("gross_profit"); oi_ltm=sumq("operating_income"); ni_ltm=sumq("net_income"); cfo_ltm=sumq("cfo"); capex_ltm=sumq("capex"); fcf_ltm=sumq("fcf"); da_ltm=sumq("d_and_a")
    annual_share_fact=latest_duration(facts,"diluted_shares",annual=True)
    diluted_shares=annual_share_fact.get("value") if annual_share_fact else next((x.get("diluted_shares") for x in reversed(last4) if x.get("diluted_shares") is not None),None)
    eps_vals=[x.get("diluted_eps") for x in last4 if x.get("diluted_eps") is not None]
    annual_eps=sum(eps_vals) if len(eps_vals)==4 else ((ni_ltm/diluted_shares) if ni_ltm is not None and diluted_shares not in (None,0) else None)
    investor={"quarterly_count":len(q),"latest_period":last4[-1].get("period_end") if last4 else None,"revenue_ltm":rev_ltm,"gross_profit_ltm":gp_ltm,
              "operating_income_ltm":oi_ltm,"net_income_ltm":ni_ltm,"fcf_ltm":fcf_ltm,"ocf_ltm":cfo_ltm,"ebitda_ltm":(oi_ltm+da_ltm if oi_ltm is not None and da_ltm is not None else None),
              "annual_eps":annual_eps,"diluted_shares":diluted_shares,"fcf_per_share":(fcf_ltm/diluted_shares if fcf_ltm is not None and diluted_shares else None),
              "ocf_per_share":(cfo_ltm/diluted_shares if cfo_ltm is not None and diluted_shares else None),
              "eps_note":"Somme des EPS trimestriels rapportés/calculés; aucun EPS annuel n'est soustrait pour fabriquer Q4."}

    ratios={}
    def put(k,v,formula):
        if v is not None and math.isfinite(v): ratios[k]={"value":v,"method":formula,**base_source}
    put("gross_margin_ltm",gp_ltm/rev_ltm if gp_ltm is not None and rev_ltm else None,"Gross profit LTM / Revenue LTM")
    put("operating_margin_ltm",oi_ltm/rev_ltm if oi_ltm is not None and rev_ltm else None,"Operating income LTM / Revenue LTM")
    put("net_margin_ltm",ni_ltm/rev_ltm if ni_ltm is not None and rev_ltm else None,"Net income LTM / Revenue LTM")
    put("fcf_margin_ltm",fcf_ltm/rev_ltm if fcf_ltm is not None and rev_ltm else None,"FCF LTM / Revenue LTM")
    put("fcf_conversion_ltm",fcf_ltm/ni_ltm if fcf_ltm is not None and ni_ltm not in (None,0) else None,"FCF LTM / Net income LTM")
    put("cfo_margin_ltm",cfo_ltm/rev_ltm if cfo_ltm is not None and rev_ltm else None,"CFO LTM / Revenue LTM")
    # Instant balance-sheet data
    for k in ["assets","cash","equity","current_debt","noncurrent_debt"]:
        x=latest_instant(facts,k)
        if x: metrics[k]=row_obj(cik,x,"latest balance-sheet XBRL fact")
    assets=metrics.get("assets",{}).get("value"); cash=metrics.get("cash",{}).get("value"); eq=metrics.get("equity",{}).get("value"); cd=metrics.get("current_debt",{}).get("value"); ncd=metrics.get("noncurrent_debt",{}).get("value")
    debt=(cd or 0)+(ncd or 0) if cd is not None or ncd is not None else None
    put("debt_to_assets",debt/assets if debt is not None and assets else None,"(Current + non-current debt) / Assets")
    invested=(eq+debt-cash) if eq is not None and debt is not None and cash is not None else None
    put("roic_simplified",oi_ltm/invested if oi_ltm is not None and invested else None,"Operating income LTM / (Equity + debt - cash), simplified")

    market=market_quote(symbol)
    if market.get("available") and annual_eps not in (None,0): market["pe_ltm"]=market["price"]/annual_eps
    if market.get("available") and investor.get("fcf_per_share") not in (None,0): market["p_fcf"]=market["price"]/investor["fcf_per_share"]
    if market.get("available") and investor.get("ocf_per_share") not in (None,0): market["p_ocf"]=market["price"]/investor["ocf_per_share"]

    # Annual history
    history={}
    for metric in ["revenue","gross_profit","operating_income","net_income","cfo","capex"]:
        for year,x in annual_history(facts,metric,cik).items(): history.setdefault(year,{})[metric]=x["value"]; history[year]["source"]=x
    hist=[]
    for year in sorted(history,reverse=True)[:10]:
        z=history[year].copy(); z["year"]=year; z["capex"]=abs(z["capex"]) if z.get("capex") is not None else None; z["fcf"]=(z["cfo"]-z["capex"]) if z.get("cfo") is not None and z.get("capex") is not None else None; hist.append(z)

    # Inflection logic: transparent and intentionally conservative.
    inf={"status":"Neutre","label":"Pas d'inflexion quantitative claire","reasons":[]}
    if len(q)>=5:
        a,b=q[-1],q[-5]; yoy=a.get("revenue_yoy"); prev=q[-2].get("revenue_yoy")
        gm=a.get("gross_margin"); pg=q[-5].get("gross_margin"); fcf=a.get("fcf"); pfcf=q[-5].get("fcf")
        if yoy is not None and prev is not None and yoy>prev and gm is not None and pg is not None and gm>pg and fcf is not None and pfcf is not None and fcf>pfcf:
            inf={"status":"Positif","label":"Inflexion positive","reasons":["croissance YoY accélère","marge brute progresse","FCF supérieur à la période comparable"]}
        elif yoy is not None and prev is not None and yoy<prev and gm is not None and pg is not None and gm<pg:
            inf={"status":"Négatif","label":"Inflexion négative","reasons":["croissance YoY ralentit","marge brute recule"]}
        else: inf["reasons"]=["signaux mixtes ou évolution insuffisante pour conclure"]

    signals={"growth":{"label":(f"{q[-1].get('revenue_yoy')*100:.1f}% YoY" if q and q[-1].get('revenue_yoy') is not None else "—"),"reason":"Croissance trimestrielle YoY calculée à partir des facts XBRL."},
             "margin":{"label":(f"{q[-1].get('operating_margin')*100:.1f}%" if q and q[-1].get('operating_margin') is not None else "—"),"reason":"Marge opérationnelle du dernier trimestre disponible."},
             "cash":{"label":(f"{q[-1].get('fcf_margin')*100:.1f}%" if q and q[-1].get('fcf_margin') is not None else "—"),"reason":"FCF = CFO - Capex absolu."},
             "balance":{"label":(f"{ratios['debt_to_assets']['value']*100:.1f}%" if 'debt_to_assets' in ratios else "—"),"reason":"Dette identifiée / actifs."},
             "capital":{"label":(f"{diluted_shares:,.0f}" if diluted_shares else "—"),"reason":"Dernière moyenne annuelle d’actions diluées disponible; dilution à analyser sur plusieurs périodes."},
             "evidence":{"label":f"{min(100,int(100*len([x for x in q if x.get('revenue') is not None])/12))}%","reason":"Couverture indicative des 12 derniers trimestres demandés."},
             "inflection":inf}

    # Facts rows for the traceability tab
    facts_rows=[]
    for metric,x in metrics.items(): facts_rows.append({**x,"canonical_metric":metric})
    audit=[]
    for metric,x in metrics.items(): audit.append({"id":f"metric:{metric}","metric":metric,"value":x.get("value"),"method":x.get("method"),"source_name":x.get("source_name"),"filing_url":x.get("filing_url"),"accession":x.get("accession"),"tag":x.get("tag"),"period_end":x.get("period_end")})
    for k,v in ratios.items(): audit.append({"id":f"ratio:{k}","metric":k,"value":v["value"],"formula":v["method"],"source_name":v.get("source_name"),"filing_url":v.get("filing_url")})

    required=["revenue","operating_income","cfo","capex","assets","cash","equity"]; coverage=sum(1 for k in required if k in metrics)/len(required); quality=round(coverage*100)
    components={"Evidence":{"score":quality,"reason":"Couverture des familles de données SEC requises."},"Growth":{"score":50,"reason":"Score qualitatif non fabriqué; utiliser les tendances trimestrielles."},"Margins":{"score":50,"reason":"Score qualitatif non fabriqué; utiliser les marges et leur évolution."},"Balance":{"score":50,"reason":"Lecture simplifiée; maturités/leases nécessitent les notes du filing."},"Moat":{"score":None,"reason":"À documenter à partir des filings, IR et sources sectorielles."}}
    nums=[v["score"] for v in components.values() if isinstance(v.get("score"),(int,float))]; total=round(sum(nums)/len(nums)) if nums else None
    return {"company":{"ticker":symbol.upper(),"name":sub.get("name"),"cik":cik,"exchange":sub.get("exchanges",[None])[0] if sub.get("exchanges") else None,"jurisdiction":sub.get("stateOfIncorporation"),"form_family":"SEC/US","taxonomy":"US-GAAP / XBRL"},
            "filings":filings,"facts":facts_rows,"metrics":metrics,"ratios":ratios,"history":hist,"quarterly":q,"investor":investor,"market":market,"signals":signals,"quality":{"score":quality,"required":required},"score":{"total":total,"components":components,"interpretation":"Score partiel : la qualité économique et le moat ne sont jamais déduits de quelques ratios."},"audit":audit}


@APP.get("/")
def home(): return FileResponse(os.path.join(os.path.dirname(__file__),"index.html"))

@APP.get("/health")
def health(): return {"ok":True,"service":"Equity Intelligence Investor Engine","sec_user_agent_configured":bool(UA and "@" in UA),"twelve_data_configured":bool(os.getenv("TWELVEDATA_API_KEY")),"version":"0.6","retrieved_at":datetime.now(timezone.utc).isoformat()}

@APP.get("/health/sec")
def health_sec():
    try:
        data=sec_get(TICKER_URL); return {"ok":True,"sec_access":True,"ticker_count":len(data),"user_agent":UA,"retrieved_at":datetime.now(timezone.utc).isoformat()}
    except Exception as e: return {"ok":False,"sec_access":False,"error":str(e),"user_agent":UA,"retrieved_at":datetime.now(timezone.utc).isoformat()}

@APP.get("/health/market")
def health_market(symbol:str="AAPL"):
    return market_quote(symbol.upper())

@APP.get("/api/company")
def company(symbol:str):
    symbol=symbol.strip().upper()
    if not re.fullmatch(r"[A-Z0-9.\-]{1,12}",symbol): raise HTTPException(400,"Invalid symbol")
    try: return build_company(symbol)
    except requests.HTTPError as e: raise HTTPException(502,f"SEC request failed: {e}")
    except HTTPException: raise
    except Exception as e: raise HTTPException(500,str(e))
