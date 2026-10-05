import os,time,requests,pyotp,pandas as pd,numpy as np,re,difflib,logging
from datetime import datetime,timedelta
from SmartApi import SmartConnect
import pytz
logging.getLogger("smartapi.smartConnect").setLevel(logging.ERROR)

# ================= DIVINE INTRADAY | MAHESH ENHANCED =================
# Telegram alerts only. NO AUTO ORDER / NO ORDER API.
API_KEY=os.getenv("API_KEY"); CLIENT_ID=os.getenv("CLIENT_ID")
PASSWORD=os.getenv("PASSWORD"); TOTP_SECRET=os.getenv("TOTP_SECRET")
TELEGRAM_BOT_TOKEN=os.getenv("TELEGRAM_BOT_TOKEN"); TELEGRAM_CHAT_ID=os.getenv("TELEGRAM_CHAT_ID")
MASTER_URL="https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"

MIN_PRICE=100; MIN_VOL=100000; SCAN_POOL=2500; INTRADAY_UNIVERSE=50
TOP_SIGNALS=3; MIN_SCORE=70; MIN_VOLX=1.2; CANDLE_DELAY=1.5; DELAY=.25
MIN_SL_PCT=.004; OPTION_MAX=3; OPTION_DAYS=5; OPTION_STRIKES=2; OPTION_DELAY=.6
CANDLE_RETRIES=3; AB1021_BACKOFF=[8,18,35]
MACD_FAST=12; MACD_SLOW=26; MACD_SIGNAL=9
ONE_DAY_DAYS=220; FIVE_MIN_DAYS=5; FIFTEEN_MIN_DAYS=7
NR_LOOKBACK=7; NR_BONUS=5; CANDLE_BONUS=5; SECTOR_BONUS=5
IST=pytz.timezone("Asia/Kolkata")
UNDERLYING_FIX={"MOTHERSON":"MOTHERSUMI","M_M":"M&M","M&M":"M&M","BAJAJ-AUTO":"BAJAJAUTO","BAJAJ_AUTO":"BAJAJAUTO"}
IPO_BLOCK={"GLASSWALL","SAMBHV","PINELABS","TATATECH","IREDA","MAMA","DOMS","KRN","BLS","BAJAJHFL"}
ETF_BLOCK=["BEES","ETF","LIQUID","GOLDBEES","SILVERBEES","NIFTYBEES","BANKBEES","ITBEES","GOLD","LIQUIDCASE","HANGSENG","NASDAQ","SETFGOLD"]

_LAST_CANDLE_CALL=0.0; _LAST_QUOTE_CALL=0.0; AB1021_COUNT=0; CANDLE_COOLDOWN_UNTIL=0.0; CANDLE_CACHE={}
CANDLE_MIN_GAP=2.0; QUOTE_MIN_GAP=2.0; MAX_CANDLE_CALLS=125

def candle_wait():
    global _LAST_CANDLE_CALL
    gap=time.monotonic()-_LAST_CANDLE_CALL
    wait=max(CANDLE_MIN_GAP-gap,0)
    if wait>0: time.sleep(wait)
    _LAST_CANDLE_CALL=time.monotonic()

def rate_error_response(r):
    if not isinstance(r,dict): return False
    return str(r.get("errorcode","")).upper()=="AB1021" or "too many requests" in str(r.get("message","")).lower() or "exceeding access rate" in str(r.get("message","")).lower()

def tg(x):
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        try: requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",data={"chat_id":TELEGRAM_CHAT_ID,"text":x},timeout=12)
        except: pass

def login():
    s=SmartConnect(api_key=API_KEY)
    r=s.generateSession(CLIENT_ID,PASSWORD,pyotp.TOTP(TOTP_SECRET).now())
    if not r or not r.get("status",True): raise RuntimeError(f"Login failed: {r}")
    return s

def master():
    r=requests.get(MASTER_URL,timeout=30); r.raise_for_status()
    x=pd.DataFrame(r.json()); x["token"]=x["token"].astype(str); x["symbol"]=x["symbol"].astype(str)
    return x

def equity_master(m):
    return m[(m["exch_seg"]=="NSE")&m["symbol"].str.endswith("-EQ")].copy()

def option_master(m):
    x=m[m["exch_seg"]=="NFO"].copy()
    x["expiry_dt"]=pd.to_datetime(x.get("expiry"),errors="coerce",format="mixed")
    x["strike_num"]=pd.to_numeric(x.get("strike"),errors="coerce")
    return x[x["symbol"].str.upper().str.contains("CE|PE",regex=True,na=False)].copy()

def all_underlyings(om):
    u=set()
    for s in om["symbol"].astype(str):
        z=re.match(r'^([A-Z0-9&\-_]+)',s.upper())
        if z and len(z.group(1))>=3: u.add(z.group(1))
    return list(u)

def best_underlying(base,us):
    base=base.upper()
    if base in UNDERLYING_FIX:return UNDERLYING_FIX[base]
    if base in us:return base
    c=[u for u in us if u[:3]==base[:3]]
    z=difflib.get_close_matches(base,c,n=1,cutoff=.82)
    return z[0] if z else base

def quotes_full_market(s,tokens):
    out=[]; total=(len(tokens)+49)//50
    print(f"Fetching quotes {len(tokens)} tokens / {total} batches...",flush=True)
    for i in range(0,len(tokens),50):
        try:
            r=s.getMarketData("FULL",{"NSE":[str(x) for x in tokens[i:i+50]]})
            if isinstance(r,dict):
                out+=(r.get("data",{}).get("fetched",[]) or [])
                print(f"Batch {i//50+1}/{total}",flush=True)
        except Exception as e: print("QUOTE ERROR",e,flush=True)
        time.sleep(max(1.8,QUOTE_MIN_GAP))
    return pd.DataFrame(out) if out else pd.DataFrame()

def live_ltp_map(s,tokens):
    out={}
    for i in range(0,len(tokens),50):
        try:
            r=s.getMarketData("FULL",{"NSE":[str(x) for x in tokens[i:i+50]]})
            for q in r.get("data",{}).get("fetched",[]) or []:
                t=str(q.get("symbolToken") or ""); p=float(q.get("ltp") or q.get("last") or 0)
                if t and p>0: out[t]=p
        except: pass
        time.sleep(1)
    return out

def candles(s,tok,days,interval,exchange="NSE"):
    global _LAST_CANDLE_CALL,AB1021_COUNT,CANDLE_COOLDOWN_UNTIL
    if len(CANDLE_CACHE)>=MAX_CANDLE_CALLS: return None
    key=f"{exchange}_{tok}_{interval}_{days}"
    if key in CANDLE_CACHE:return CANDLE_CACHE[key]
    if time.monotonic()<CANDLE_COOLDOWN_UNTIL:return None
    for attempt in range(CANDLE_RETRIES):
        candle_wait()
        try:
            e=datetime.now(); b=e-timedelta(days=days)
            r=s.getCandleData({"exchange":exchange,"symboltoken":str(tok),"interval":interval,
                               "fromdate":b.strftime("%Y-%m-%d %H:%M"),"todate":e.strftime("%Y-%m-%d %H:%M")})
            if rate_error_response(r):
                AB1021_COUNT+=1; w=AB1021_BACKOFF[min(attempt,2)]
                if AB1021_COUNT>=3:CANDLE_COOLDOWN_UNTIL=time.monotonic()+70
                if attempt<CANDLE_RETRIES-1: time.sleep(w); continue
                return None
            data=r.get("data") if isinstance(r,dict) else None
            if not data:
                if attempt<CANDLE_RETRIES-1:time.sleep(3);continue
                return None
            x=pd.DataFrame(data,columns=["timestamp","open","high","low","close","volume"])
            x["timestamp"]=pd.to_datetime(x["timestamp"],errors="coerce")
            for c in ["open","high","low","close","volume"]:x[c]=pd.to_numeric(x[c],errors="coerce")
            x=x.dropna(subset=["timestamp","close"]).sort_values("timestamp").reset_index(drop=True)
            if x.empty:return None
            AB1021_COUNT=0; CANDLE_CACHE[key]=x; return x
        except Exception as e:
            if "ab1021" in str(e).lower() or "too many" in str(e).lower():
                AB1021_COUNT+=1; time.sleep(AB1021_BACKOFF[min(attempt,2)])
            else: time.sleep(3)
    return None

def rsi(s,n=14):
    d=s.diff(); u=d.clip(lower=0).ewm(alpha=1/n,adjust=False).mean()
    v=(-d.clip(upper=0)).ewm(alpha=1/n,adjust=False).mean()
    return 100-100/(1+u/v.replace(0,np.nan))

def macd_line(s):
    a=s.ewm(span=MACD_FAST,adjust=False).mean(); b=s.ewm(span=MACD_SLOW,adjust=False).mean()
    m=a-b; sg=m.ewm(span=MACD_SIGNAL,adjust=False).mean()
    return m,sg,m-sg

def feat(x):
    x=x.copy()
    x["ema9"]=x.close.ewm(span=9,adjust=False).mean()
    x["ema20"]=x.close.ewm(span=20,adjust=False).mean()
    x["ema50"]=x.close.ewm(span=50,adjust=False).mean()
    x["rsi"]=rsi(x.close)
    tr=pd.concat([x.high-x.low,(x.high-x.close.shift()).abs(),(x.low-x.close.shift()).abs()],axis=1).max(axis=1)
    x["atr"]=tr.ewm(span=14,adjust=False).mean()
    x["vavg20"]=x.volume.rolling(20).mean(); x["volx"]=x.volume/x.vavg20.replace(0,np.nan)
    x["prev20h"]=x.high.shift(1).rolling(20).max(); x["prev20l"]=x.low.shift(1).rolling(20).min()
    tp=(x.high+x.low+x.close)/3; day=x.timestamp.dt.date
    x["pv"]=tp*x.volume; x["cv"]=x.volume.groupby(day).cumsum(); x["cpv"]=x.pv.groupby(day).cumsum()
    x["vwap"]=x.cpv/x.cv.replace(0,np.nan); x["ema20slope"]=x.ema20-x.ema20.shift(3)
    x["macd"],x["macd_sig"],x["macd_hist"]=macd_line(x.close)
    x["range"]=x.high-x.low
    return x

# -------- Mahesh-inspired candlestick + narrow-range confirmations --------
def candle_pattern(x):
    if len(x)<3:return "NONE","NEUTRAL"
    q=x.iloc[-1]; o,h,l,c=map(float,[q.open,q.high,q.low,q.close])
    body=abs(c-o); rng=max(h-l,.0001); up=h-max(o,c); dn=min(o,c)-l
    prev=x.iloc[-2]; po,pc=float(prev.open),float(prev.close)
    bull_eng=c>o and pc<po and c>=po and o<=pc
    bear_eng=c<o and pc>po and c<=po and o>=pc
    hammer=dn>=2*max(body,.001) and up<=body and c>l+rng*.55
    shooting=up>=2*max(body,.001) and dn<=body and c<l+rng*.55
    doji=body<=rng*.12
    if bull_eng:return "BULL_ENGULFING","BULLISH"
    if bear_eng:return "BEAR_ENGULFING","BEARISH"
    if hammer:return "HAMMER","BULLISH"
    if shooting:return "SHOOTING_STAR","BEARISH"
    if doji:return "DOJI","NEUTRAL"
    return "NONE","BULLISH" if c>o else "BEARISH"

def narrow_range(x,n=NR_LOOKBACK):
    if len(x)<n+1:return "NONE"
    q=x.iloc[-1]; recent=x["range"].iloc[-n-1:-1]
    if len(recent)==0:return "NONE"
    if float(q["range"])<=float(recent.min())*1.001:
        return f"NR{n}"
    return "NONE"

def market_bias(s):
    d=candles(s,"26000",10,"FIFTEEN_MINUTE","NSE")
    if d is None or len(d)<30:return "NEUTRAL",0
    x=feat(d).iloc[-2]
    bull=x.close>x.ema20>x.ema50 and x.ema20slope>0 and x.rsi>=50 and x.macd>x.macd_sig
    bear=x.close<x.ema20<x.ema50 and x.ema20slope<0 and x.rsi<=50 and x.macd<x.macd_sig
    return ("BULLISH",1) if bull else ("BEARISH",-1) if bear else ("NEUTRAL",0)

def sector_name(sym):
    b=sym.replace("-EQ","").upper()
    groups={
    "BANKING":set("HDFCBANK ICICIBANK SBIN AXISBANK KOTAKBANK INDUSINDBK BANKBARODA PNB FEDERALBNK CANBK".split()),
    "IT":set("TCS INFY HCLTECH WIPRO TECHM LTIM PERSISTENT COFORGE".split()),
    "AUTO":set("MARUTI TATAMOTORS M&M BAJAJ-AUTO EICHERMOT HEROMOTOCO TVSMOTOR ASHOKLEY".split()),
    "PHARMA":set("SUNPHARMA DRREDDY CIPLA DIVISLAB AUROPHARMA LUPIN APOLLOHOSP".split()),
    "METALS":set("TATASTEEL JSWSTEEL HINDALCO SAIL JINDALSTEL".split()),
    "ENERGY":set("RELIANCE ONGC NTPC POWERGRID COALINDIA".split()),
    "FMCG":set("ITC HINDUNILVR NESTLEIND BRITANNIA DABUR MARICO".split()),
    "REALTY":set("DLF GODREJPROP OBEROIRLT PRESTIGE".split()),
    "CAPITAL_GOODS":set("LT ABB SIEMENS BHEL CGPOWER BEL".split())}
    for k,v in groups.items():
        if b in v:return k
    return "OTHER"

def analyze(sym,tok,s,mbias,sector_strength=None,debug=True):
    clean=sym.replace("-EQ","").upper()
    if clean in IPO_BLOCK:return None
    dd=candles(s,tok,ONE_DAY_DAYS,"ONE_DAY","NSE")
    d5=candles(s,tok,FIVE_MIN_DAYS,"FIVE_MINUTE","NSE")
    if dd is None or len(dd)<120:return None
    if d5 is None:return None
    time.sleep(DELAY)
    d15=candles(s,tok,FIFTEEN_MIN_DAYS,"FIFTEEN_MINUTE","NSE")
    if d15 is None or len(d5)<60 or len(d15)<60:return None
    dd=feat(dd); f5=feat(d5); f15=feat(d15)
    daily=dd.iloc[-2]; a=f5.iloc[-2]; b=f15.iloc[-2]
    entry=float(a.close); atr=max(float(a.atr),entry*.003); vol=float(a.volx)
    daily_bull=daily.close>daily.ema20>daily.ema50 and daily.ema20slope>0
    daily_bear=daily.close<daily.ema20<daily.ema50 and daily.ema20slope<0
    bull15=b.close>b.ema20>b.ema50 and b.ema20slope>0
    bear15=b.close<b.ema20<b.ema50 and b.ema20slope<0
    bull5=a.close>a.ema9>a.ema20 and a.ema20slope>0
    bear5=a.close<a.ema9<a.ema20 and a.ema20slope<0
    above=a.close>a.vwap; below=a.close<a.vwap
    macd_buy=a.macd>a.macd_sig and a.macd_hist>0 and b.macd>b.macd_sig
    macd_sell=a.macd<a.macd_sig and a.macd_hist<0 and b.macd<b.macd_sig
    bo_buy=a.close>a.prev20h and vol>=1.5; bo_sell=a.close<a.prev20l and vol>=1.5
    buy=sum([25 if bull15 else 0,20 if bull5 else 0,15 if above else 0,15 if 55<=a.rsi<=75 else 0,15 if vol>=1.5 else 0,15 if macd_buy else 0,10 if bo_buy else 0])
    sell=sum([25 if bear15 else 0,20 if bear5 else 0,15 if below else 0,15 if 25<=a.rsi<=45 else 0,15 if vol>=1.5 else 0,15 if macd_sell else 0,10 if bo_sell else 0])
    direction="BUY" if buy>sell else "SELL"; tech=max(buy,sell)
    if tech<60 or vol<MIN_VOLX:return None
    if direction=="BUY" and (not macd_buy or not daily_bull):return None
    if direction=="SELL" and (not macd_sell or not daily_bear):return None
    if sector_name(sym)=="OTHER" and tech<85:return None

    cp,cb=candle_pattern(f5.iloc[:-1])
    nr=narrow_range(f5.iloc[:-1])
    extra=0
    if direction=="BUY" and cb=="BULLISH":extra+=CANDLE_BONUS
    if direction=="SELL" and cb=="BEARISH":extra+=CANDLE_BONUS
    if nr!="NONE" and ((direction=="BUY" and a.close>a.open) or (direction=="SELL" and a.close<a.open)):extra+=NR_BONUS

    sec=sector_name(sym)
    sec_score=0
    if sector_strength and sec in sector_strength:
        if direction=="BUY" and sector_strength[sec]>0.15:sec_score=SECTOR_BONUS
        if direction=="SELL" and sector_strength[sec]<-0.15:sec_score=SECTOR_BONUS

    if direction=="BUY":
        sw=float(f5.low.iloc[-8:-2].min()); sl=min(entry-atr,sw-.15*atr)
        sl=entry-atr if sl>=entry else sl
    else:
        sw=float(f5.high.iloc[-8:-2].max()); sl=max(entry+atr,sw+.15*atr)
        sl=entry+atr if sl<=entry else sl
    risk=abs(entry-sl)
    if risk/entry<MIN_SL_PCT:return None
    t1=entry+(1.5*risk if direction=="BUY" else -1.5*risk)
    t2=entry+(2*risk if direction=="BUY" else -2*risk)
    t3=entry+(3*risk if direction=="BUY" else -3*risk)
    setup="BREAKOUT" if (bo_buy if direction=="BUY" else bo_sell) else "PULLBACK/VWAP" if (above if direction=="BUY" else below) else "TREND"
    market_pts=-8 if ((mbias=="BULLISH" and direction=="SELL") or (mbias=="BEARISH" and direction=="BUY")) else (0 if mbias=="NEUTRAL" else 8)
    return {"symbol":sym,"token":tok,"direction":direction,"tech":tech,"extra":extra+sec_score,
            "market_pts":market_pts,"entry":entry,"sl":sl,"t1":t1,"t2":t2,"t3":t3,
            "setup":setup,"sector":sec,"rsi":float(a.rsi),"volx":vol,
            "macd":float(a.macd),"macd_sig":float(a.macd_sig),"macd_bias":"BULLISH" if macd_buy else "BEARISH",
            "tf5":"BULLISH" if bull5 else "BEARISH","tf15":"BULLISH" if bull15 else "BEARISH",
            "daily":"BULLISH" if daily_bull else "BEARISH","candle":cp,"nr":nr,
            "sector_score":sec_score}

def build_sector_strength(top50):
    # Lightweight breadth proxy from the already-fetched liquid universe.
    d={}
    for sym,_,_,vol in top50:
        sec=sector_name(sym)
        if sec=="OTHER":continue
        d.setdefault(sec,[]).append(float(vol))
    # This is only a universe/participation score; no extra API calls.
    out={}
    for sec,vals in d.items():
        med=np.median(vals) if vals else 0
        out[sec]=min(1.0,max(-1.0,(np.mean(vals)-med)/max(med,1)))
    return out

def option_flow(s,om,z,us):
    if AB1021_COUNT>=2:return 0,"NEUTRAL",0
    sym=z["symbol"].replace("-EQ","").upper(); spot=z["entry"]; u=best_underlying(sym,us)
    x=om[om.symbol.str.upper().str.startswith(u,na=False)].copy()
    if x.empty:return 0,"NEUTRAL",0
    today=pd.Timestamp.now().normalize()
    x=x[(x.expiry_dt>=today)&(x.expiry_dt<=today+pd.Timedelta(days=45))]
    if x.empty:return 0,"NEUTRAL",0
    exp=sorted(x.expiry_dt.dropna().unique())[:1]; x=x[x.expiry_dt.isin(exp)]
    strikes=sorted(x.strike_num.dropna().unique())
    if not strikes:return 0,"NEUTRAL",0
    atm=min(strikes,key=lambda q:abs(float(q)-spot))
    gap=min([abs(q-atm) for q in strikes if q!=atm] or [1])
    x=x[x.strike_num.isin([q for q in strikes if abs(q-atm)<=gap*OPTION_STRIKES])]
    ce=pe=0.; cm=[];pm=[];used=0
    for _,c in x.iterrows():
        if used>=OPTION_MAX:break
        d=candles(s,c.token,OPTION_DAYS,"ONE_DAY","NFO"); time.sleep(OPTION_DELAY)
        if d is None or len(d)<3:continue
        used+=1; v=float(d.volume.fillna(0).sum())
        m=(float(d.close.iloc[-1])-float(d.close.iloc[0]))/max(float(d.close.iloc[0]),.01)
        if str(c.symbol).upper().endswith("CE"):ce+=v;cm.append(m)
        else:pe+=v;pm.append(m)
    if ce==0 and pe==0:return 0,"NEUTRAL",0
    ratio=ce/max(pe,1); ca=np.mean(cm) if cm else 0; pa=np.mean(pm) if pm else 0
    if ratio>=1.25 and ca>=pa:return 8,"BULLISH",ratio
    if ratio<=.80 and pa>=ca:return 8,"BEARISH",ratio
    return 0,"NEUTRAL",ratio

def stars(s):
    return "★★★★★" if s>=90 else "★★★★☆" if s>=80 else "★★★☆☆" if s>=70 else "★★☆☆☆"

def main():
    print(f"=== DIVINE INTRADAY ENHANCED | {datetime.now(IST):%d %b %H:%M:%S IST} ===",flush=True)
    s=login(); m=master(); em=equity_master(m)
    rows=[]
    for _,r in em.iterrows():
        sym=r["symbol"]; clean=sym.replace("-EQ","").upper()
        if clean in IPO_BLOCK or len(clean)<=2 or any(x in clean for x in ETF_BLOCK):continue
        rows.append(r)
    em=pd.DataFrame(rows).head(SCAN_POOL)
    print(f"NSE universe: {len(em)}",flush=True)
    om=option_master(m); us=all_underlyings(om)
    mbias,mval=market_bias(s); print(f"MARKET {mbias} {mval}",flush=True)
    q=quotes_full_market(s,em.token.tolist())
    if q.empty:
        tg(f"⚠️ DIVINE INTRADAY\\n\\nNO SETUP\\nREASON: Quotes empty / Market closed")
        return
    symmap={str(r.token):r.symbol for _,r in em.iterrows()}; allv=[]
    for r in q.to_dict("records"):
        try:
            t=str(r.get("symbolToken") or r.get("token") or "")
            p=float(r.get("ltp") or r.get("last") or r.get("close") or 0)
            v=float(r.get("volume") or r.get("tradeVolume") or r.get("vol") or 0)
            if t in symmap and p>=MIN_PRICE and v>=MIN_VOL:allv.append((symmap[t],t,p,v))
        except:pass
    allv=sorted(allv,key=lambda x:x[3],reverse=True); top50=allv[:50]
    print(f"After price/volume: {len(allv)} | TOP50 ranked | TOP{INTRADAY_UNIVERSE} candles",flush=True)
    sector_strength=build_sector_strength(top50)
    results=[]
    for sym,tok,ltp,vol in top50[:INTRADAY_UNIVERSE]:
        z=analyze(sym,tok,s,mbias,sector_strength)
        if z:results.append(z)
        time.sleep(.2)
    if not results:
        tg(f"DIVINE INTRADAY {datetime.now(IST):%d %b %H:%M}\\nNO SETUP\\nMarket:{mbias}\\nTop50 volume universe scanned.")
        return
    results=sorted(results,key=lambda z:z["tech"]+z["extra"]+z["market_pts"],reverse=True)
    finalists=results[:TOP_SIGNALS]
    live=live_ltp_map(s,[z["token"] for z in finalists])
    final=[]
    for z in finalists:
        # Option-flow is deliberately restricted to finalists to protect SmartAPI limits.
        op,ob,ratio=option_flow(s,om,z,us)
        total=z["tech"]+z["extra"]+z["market_pts"]+op
        if total<MIN_SCORE:continue
        lp=live.get(str(z["token"]),z["entry"])
        final.append(f"{stars(total)} {z['symbol']} {z['direction']} @ {lp:.2f}\\n"
                     f"Tech:{z['tech']} Extra:{z['extra']} Mkt:{z['market_pts']} Opt:{op}({ob} R:{ratio:.2f}) Total:{total}\\n"
                     f"Setup:{z['setup']} Sec:{z['sector']} RSI:{z['rsi']:.1f} VolX:{z['volx']:.2f}x\\n"
                     f"MACD:{z['macd_bias']} {z['macd']:.3f}/{z['macd_sig']:.3f} | D:{z['daily']} 15M:{z['tf15']} 5M:{z['tf5']}\\n"
                     f"Candle:{z['candle']} NR:{z['nr']} SectorConfirm:{z['sector_score']}\\n"
                     f"SL:{z['sl']:.2f} T1:{z['t1']:.2f} T2:{z['t2']:.2f} T3:{z['t3']:.2f}")
    if not final:
        tg(f"DIVINE INTRADAY {datetime.now(IST):%d %b %H:%M}\\nNO SETUP\\nMarket:{mbias}\\nReason: Final score < {MIN_SCORE}")
        return
    msg=f"🔥 DIVINE INTRADAY ENHANCED\\nMARKET: {mbias}\\n\\n" + "\\n\\n".join(final)
    print(msg,flush=True); tg(msg)

if __name__=="__main__":
    main()
