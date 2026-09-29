import os,time,requests,pyotp,pandas as pd,numpy as np,re,difflib,logging
from datetime import datetime,timedelta
from SmartApi import SmartConnect
import pytz
logging.getLogger("smartapi.smartConnect").setLevel(logging.ERROR)

API_KEY=os.getenv("API_KEY")
CLIENT_ID=os.getenv("CLIENT_ID")
PASSWORD=os.getenv("PASSWORD")
TOTP_SECRET=os.getenv("TOTP_SECRET")
TELEGRAM_BOT_TOKEN=os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID=os.getenv("TELEGRAM_CHAT_ID")
MASTER_URL="https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"

MIN_PRICE=100
MIN_VOL=100000
SCAN_POOL=2500
INTRADAY_UNIVERSE=50
TOP_SIGNALS=3
MIN_SCORE=70
MIN_VOLX=1.2
CANDLE_DELAY=2.0
DELAY=.25
MIN_SL_PCT=.004
OPTION_MAX=3
OPTION_DAYS=5
OPTION_STRIKES=2
OPTION_DELAY=.6
CANDLE_RETRIES=3
AB1021_BACKOFF=[8,18,35]

# FIX: 220 calendar days gives enough trading sessions for 120 daily candles
ONE_DAY_DAYS=220
FIVE_MIN_DAYS=5
FIFTEEN_MIN_DAYS=7

IST=pytz.timezone("Asia/Kolkata")

UNDERLYING_FIX={"MOTHERSON":"MOTHERSUMI","M_M":"M&M","M&M":"M&M","BAJAJ-AUTO":"BAJAJAUTO","BAJAJ_AUTO":"BAJAJAUTO"}
IPO_BLOCK={"GLASSWALL","SAMBHV","PINELABS","TATATECH","IREDA","MAMA","DOMS","KRN","BLS","BAJAJHFL"}
ETF_BLOCK=["BEES","ETF","LIQUID","GOLDBEES","SILVERBEES","NIFTYBEES","BANKBEES","ITBEES","GOLD","LIQUIDCASE","HANGSENG","NASDAQ","SETFGOLD"]

_LAST_CANDLE_CALL=0.0
AB1021_COUNT=0
CANDLE_COOLDOWN_UNTIL=0.0
CANDLE_CACHE={}

def candle_wait():
    global _LAST_CANDLE_CALL
    now=time.monotonic()
    gap=now-_LAST_CANDLE_CALL
    if gap<CANDLE_DELAY: time.sleep(CANDLE_DELAY-gap)
    _LAST_CANDLE_CALL=time.monotonic()

def rate_error_response(r):
    if not isinstance(r,dict): return False
    code=str(r.get("errorcode","")).upper()
    msg=str(r.get("message","")).lower()
    return code=="AB1021" or "too many requests" in msg or "exceeding access rate" in msg

def tg(x):
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        try: requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",data={"chat_id":TELEGRAM_CHAT_ID,"text":x},timeout=12)
        except: pass

def login():
    s=SmartConnect(api_key=API_KEY)
    totp=pyotp.TOTP(TOTP_SECRET).now()
    r=s.generateSession(CLIENT_ID,PASSWORD,totp)
    if not r or not r.get("status",True): raise RuntimeError(f"Login failed: {r}")
    return s

def master():
    r=requests.get(MASTER_URL,timeout=30); r.raise_for_status()
    x=pd.DataFrame(r.json()); x["token"]=x["token"].astype(str); x["symbol"]=x["symbol"].astype(str)
    return x

def equity_master(m):
    return m[(m["exch_seg"]=="NSE") & m["symbol"].str.endswith("-EQ")].copy()

def option_master(m):
    x=m[m["exch_seg"]=="NFO"].copy()
    x["expiry_dt"]=pd.to_datetime(x.get("expiry"),errors="coerce",format="mixed")
    x["strike_num"]=pd.to_numeric(x.get("strike"),errors="coerce")
    return x[x["symbol"].str.upper().str.contains("CE|PE",regex=True,na=False)].copy()

def all_underlyings(om):
    u=set()
    for s in om["symbol"].astype(str):
        mm=re.match(r'^([A-Z0-9&\-\_]+)',s.upper())
        if mm and len(mm.group(1))>=3: u.add(mm.group(1))
    return list(u)

def best_underlying(base,us):
    base=base.upper()
    if base in UNDERLYING_FIX: return UNDERLYING_FIX[base]
    if base in us: return base
    c=[u for u in us if u[:3]==base[:3]]
    z=difflib.get_close_matches(base,c,n=1,cutoff=.82)
    return z[0] if z else base

def quotes_full_market(s,tokens):
    out=[]; total_batches=(len(tokens)+49)//50
    print(f"Fetching quotes for {len(tokens)} tokens in {total_batches} batches...",flush=True)
    for idx in range(0,len(tokens),50):
        batch=tokens[idx:idx+50]; batch_num=idx//50+1
        try:
            r=s.getMarketData("FULL",{"NSE":[str(x) for x in batch]})
            if isinstance(r,dict):
                d=r.get("data",{})
                if isinstance(d,dict):
                    fetched=d.get("fetched",[]) or []
                    out+=fetched
                    print(f"Batch {batch_num}/{total_batches} -> {len(fetched)} quotes",flush=True)
        except Exception as e: print(f"QUOTE ERROR batch {batch_num}: {e}",flush=True)
        time.sleep(1.8)
    return pd.DataFrame(out) if out else pd.DataFrame()

def candles(s,tok,days,interval,exchange="NSE"):
    global _LAST_CANDLE_CALL,AB1021_COUNT,CANDLE_COOLDOWN_UNTIL,CANDLE_CACHE
    key=f"{exchange}_{tok}_{interval}_{days}"
    if key in CANDLE_CACHE: return CANDLE_CACHE[key]
    if time.monotonic()<CANDLE_COOLDOWN_UNTIL: return None
    for attempt in range(CANDLE_RETRIES):
        candle_wait()
        try:
            e=datetime.now(); b=e-timedelta(days=days)
            payload={"exchange":exchange,"symboltoken":str(tok),"interval":interval,"fromdate":b.strftime("%Y-%m-%d %H:%M"),"todate":e.strftime("%Y-%m-%d %H:%M")}
            r=s.getCandleData(payload)
            if rate_error_response(r):
                AB1021_COUNT+=1; wait=AB1021_BACKOFF[min(attempt,len(AB1021_BACKOFF)-1)]
                if AB1021_COUNT>=3: CANDLE_COOLDOWN_UNTIL=time.monotonic()+70
                if attempt>=CANDLE_RETRIES-1: return None
                time.sleep(wait); continue
            if not isinstance(r,dict) or not r.get("data"):
                if attempt==CANDLE_RETRIES-1: return None
                time.sleep(3); continue
            data=r.get("data")
            if not data: return None
            x=pd.DataFrame(data,columns=["timestamp","open","high","low","close","volume"])
            x["timestamp"]=pd.to_datetime(x["timestamp"],errors="coerce")
            for c in ["open","high","low","close","volume"]: x[c]=pd.to_numeric(x[c],errors="coerce")
            x=x.dropna(subset=["timestamp","close"])
            if x.empty: return None
            AB1021_COUNT=0
            res=x.sort_values("timestamp").reset_index(drop=True)
            CANDLE_CACHE[key]=res
            return res
        except Exception as e:
            msg=str(e).lower()
            if "ab1021" in msg or "too many" in msg:
                AB1021_COUNT+=1; wait=AB1021_BACKOFF[min(attempt,len(AB1021_BACKOFF)-1)]
                if AB1021_COUNT>=3: CANDLE_COOLDOWN_UNTIL=time.monotonic()+70
                time.sleep(wait)
            else: time.sleep(3)
            if attempt>=CANDLE_RETRIES-1: return None
    return None

def rsi(s,n=14):
    d=s.diff(); u=d.clip(lower=0).ewm(alpha=1/n,adjust=False).mean(); v=(-d.clip(upper=0)).ewm(alpha=1/n,adjust=False).mean()
    return 100-100/(1+u/v.replace(0,np.nan))

def feat(x):
    x=x.copy()
    x["ema9"]=x.close.ewm(span=9,adjust=False).mean()
    x["ema20"]=x.close.ewm(span=20,adjust=False).mean()
    x["ema50"]=x.close.ewm(span=50,adjust=False).mean()
    x["rsi"]=rsi(x.close)
    tr=pd.concat([x.high-x.low,(x.high-x.close.shift()).abs(),(x.low-x.close.shift()).abs()],axis=1).max(axis=1)
    x["atr"]=tr.ewm(span=14,adjust=False).mean()
    x["vavg20"]=x.volume.rolling(20).mean()
    x["volx"]=x.volume/x.vavg20.replace(0,np.nan)
    x["prev20h"]=x.high.shift(1).rolling(20).max()
    x["prev20l"]=x.low.shift(1).rolling(20).min()
    tp=(x.high+x.low+x.close)/3; day=x.timestamp.dt.date
    x["pv"]=tp*x.volume; x["cv"]=x.volume.groupby(day).cumsum(); x["cpv"]=x.pv.groupby(day).cumsum()
    x["vwap"]=x.cpv/x.cv.replace(0,np.nan)
    x["ema20slope"]=x.ema20-x.ema20.shift(3)
    return x

def market_bias(s):
    vals=[]
    d=candles(s,"99926000",10,"FIFTEEN_MINUTE","NSE")
    if d is None or len(d)<30: return "NEUTRAL",0
    d=feat(d); x=d.iloc[-2]
    bull=(x.close>x.ema20 and x.ema20>x.ema50 and x.ema20slope>0 and x.rsi>=50)
    bear=(x.close<x.ema20 and x.ema20<x.ema50 and x.ema20slope<0 and x.rsi<=50)
    vals.append(1 if bull else -1 if bear else 0)
    if not vals: return "NEUTRAL",0
    z=sum(vals)
    return ("BULLISH",z) if z>0 else ("BEARISH",z) if z<0 else ("NEUTRAL",z)

def sector_name(sym):
    b=sym.replace("-EQ","").upper()
    groups={
        "BANKING":set("HDFCBANK ICICIBANK SBIN AXISBANK KOTAKBANK INDUSINDBK BANKBARODA PNB FEDERALBNK CANBK".split()),
        "IT":set("TCS INFY HCLTECH WIPRO TECHM LTIM PERSISTENT COFORGE".split()),
        "AUTO":set("MARUTI TATAMOTORS M&M BAJAJ-AUTO EICHERMOT HEROMOTOCO TVSMOTOR ASHOKLEY".split()),
        "PHARMA":set("SUNPHARMA DRREDDY CIPLA DIVISLAB AUROPHARMA LUPIN APOLLOHOSP".split()),
        "METALS":set("TATASTEEL JSWSTEEL HINDALCO SAIL JINDALSTEL".split()),
        "ENERGY":set("RELIANCE ONGC NTPC POWERGRID COALINDIA".split())
    }
    for k,v in groups.items():
        if b in v: return k
    return "OTHER"

def analyze(sym,tok,s,mbias,debug=false):
    clean=sym.replace("-EQ","").upper()
    if clean in IPO_BLOCK:
        if debug: print(f" ❌ {sym} REJECT: IPO_BLOCK",flush=True)
        return None

    d_daily=candles(s,tok,ONE_DAY_DAYS,"ONE_DAY","NSE")
    if d_daily is None:
        if debug: print(f" ❌ {sym} REJECT: DAILY API returned empty/failed",flush=True)
        return None
    if len(d_daily)<120:
        if debug: print(f" ❌ {sym} REJECT: DAILY candles={len(d_daily)} <120",flush=True)
        return None

    d5=candles(s,tok,FIVE_MIN_DAYS,"FIVE_MINUTE","NSE")
    if d5 is None:
        if debug: print(f" ❌ {sym} REJECT: 5M data empty",flush=True)
        return None
    time.sleep(DELAY)

    d15=candles(s,tok,FIFTEEN_MIN_DAYS,"FIFTEEN_MINUTE","NSE")
    if d15 is None or len(d5)<60 or len(d15)<60:
        if debug: print(f" ❌ {sym} REJECT: 15M empty or len<60",flush=True)
        return None

    dd=feat(d_daily); d5=feat(d5); d15=feat(d15)
    daily=dd.iloc[-2]; a=d5.iloc[-2]; b=d15.iloc[-2]

    if any(pd.isna(daily[k]) for k in ["close","ema20","ema50","ema20slope","rsi"]):
        if debug: print(f" ❌ {sym} REJECT: DAILY NaN",flush=True)
        return None

    if any(pd.isna(a[k]) for k in ["close","atr","rsi","volx","vwap","prev20h","prev20l"]):
        if debug: print(f" ❌ {sym} REJECT: 5M NaN volx={a.get('volx','?')}",flush=True)
        return None

    entry=float(a.close)
    atr=max(float(a.atr),entry*.003)

    daily_bull=(daily.close>daily.ema20 and daily.ema20>daily.ema50 and daily.ema20slope>0)
    daily_bear=(daily.close<daily.ema20 and daily.ema20<daily.ema50 and daily.ema20slope<0)

    bull15=(b.close>b.ema20>b.ema50 and b.ema20slope>0)
    bear15=(b.close<b.ema20<b.ema50 and b.ema20slope<0)

    bull5=(a.close>a.ema9>a.ema20 and a.ema20slope>0)
    bear5=(a.close<a.ema9<a.ema20 and a.ema20slope<0)

    vol=float(a.volx)
    above_vwap=a.close>a.vwap
    below_vwap=a.close<a.vwap

    breakout_buy=(a.close>a.prev20h and vol>=1.5)
    breakout_sell=(a.close<a.prev20l and vol>=1.5)

    buy=sum([
        25 if bull15 else 0,
        20 if bull5 else 0,
        15 if above_vwap else 0,
        15 if 55<=a.rsi<=75 else 0,
        15 if vol>=1.5 else 0,
        10 if breakout_buy else 0
    ])

    sell=sum([
        25 if bear15 else 0,
        20 if bear5 else 0,
        15 if below_vwap else 0,
        15 if 25<=a.rsi<=45 else 0,
        15 if vol>=1.5 else 0,
        10 if breakout_sell else 0
    ])

    direction="BUY" if buy>sell else "SELL"
    tech=max(buy,sell)

    if tech<60:
        if debug:
            print(f" ❌ {sym} REJECT: tech={tech} <60 dir={direction} buy={buy} sell={sell} 15M bull={bull15} bear={bear15} 5M bull={bull5} bear={bear5} RSI={a.rsi:.1f} volx={vol:.2f}",flush=True)
        return None

    if vol<MIN_VOLX:
        if debug: print(f" ❌ {sym} REJECT: volx={vol:.2f} <{MIN_VOLX} dir={direction} tech={tech}",flush=True)
        return None

    if sector_name(sym)=="OTHER" and tech<85:
        if debug: print(f" ❌ {sym} REJECT: OTHER sector tech={tech} <85",flush=True)
        return None

    if direction=="BUY" and not daily_bull:
        if debug: print(f" ❌ {sym} REJECT: BUY but DAILY not BULL daily={daily.close:.1f} ema20={daily.ema20:.1f} ema50={daily.ema50:.1f} slope={daily.ema20slope:.2f}",flush=True)
        return None

    if direction=="SELL" and not daily_bear:
        if debug: print(f" ❌ {sym} REJECT: SELL but DAILY not BEAR daily={daily.close:.1f} ema20={daily.ema20:.1f} ema50={daily.ema50:.1f} slope={daily.ema20slope:.2f}",flush=True)
        return None

    if direction=="BUY":
        sw=float(d5.low.iloc[-8:-2].min())
        sl=min(entry-atr,sw-0.15*atr)
        sl=entry-atr if sl>=entry else sl
    else:
        sw=float(d5.high.iloc[-8:-2].max())
        sl=max(entry+atr,sw+0.15*atr)
        sl=entry+atr if sl<=entry else sl

    risk=abs(entry-sl)

    if risk/entry<MIN_SL_PCT:
        if debug: print(f" ❌ {sym} REJECT: risk {risk/entry*100:.3f}% < {MIN_SL_PCT*100}%",flush=True)
        return None

    if debug: print(f" ✅ {sym} PASS: {direction} tech={tech} volx={vol:.2f} daily={'BULL' if daily_bull else 'BEAR'}",flush=True)

    t1=entry+(1.5*risk if direction=="BUY" else -1.5*risk)
    t2=entry+(2*risk if direction=="BUY" else -2*risk)
    t3=entry+(3*risk if direction=="BUY" else -3*risk)

    setup="BREAKOUT" if (breakout_buy if direction=="BUY" else breakout_sell) else "PULLBACK/VWAP" if (above_vwap if direction=="BUY" else below_vwap) else "TREND"

    market_pts=(-8 if ((mbias=="BULLISH" and direction=="SELL") or (mbias=="BEARISH" and direction=="BUY")) else (0 if mbias=="NEUTRAL" else 8))

    return {
        "symbol":sym,"direction":direction,"tech":tech,"market_pts":market_pts,
        "entry":entry,"sl":sl,"t1":t1,"t2":t2,"t3":t3,"setup":setup,
        "sector":sector_name(sym),"rsi":float(a.rsi),"volx":vol,
        "tf5":"BULLISH" if bull5 else "BEARISH" if bear5 else "NEUTRAL",
        "tf15":"BULLISH" if bull15 else "BEARISH" if bear15 else "NEUTRAL",
        "daily":"BULLISH" if daily_bull else "BEARISH" if daily_bear else "NEUTRAL"
    }

def option_flow(s,om,z,us):
    if AB1021_COUNT>=2: return 0,"NEUTRAL",0,z["entry"]
    sym=z["symbol"].replace("-EQ","").upper()
    spot=z["entry"]
    u=best_underlying(sym,us)
    x=om[om.symbol.str.upper().str.startswith(u,na=False)].copy()
    if x.empty: return 0,"NEUTRAL",0,u

    today=pd.Timestamp.now().normalize()
    x=x[(x.expiry_dt>=today)&(x.expiry_dt<=today+pd.Timedelta(days=45))].copy()
    if x.empty: return 0,"NEUTRAL",0,u

    exp=sorted(x.expiry_dt.dropna().unique())[:1]; x=x[x.expiry_dt.isin(exp)]
    strikes=sorted(x.strike_num.dropna().unique())
    if not strikes: return 0,"NEUTRAL",0,u

    atm=min(strikes,key=lambda q:abs(float(q)-spot))
    gap=min([abs(q-atm) for q in strikes if q!=atm] or [1])
    allowed=[q for q in strikes if abs(q-atm)<=gap*OPTION_STRIKES]
    x=x[x.strike_num.isin(allowed)]

    ce=pe=0.0; cm=[]; pm=[]; used=0

    for _,c in x.iterrows():
        if used>=OPTION_MAX: break
        d=candles(s,c.token,OPTION_DAYS,"ONE_DAY","NFO")
        time.sleep(OPTION_DELAY)
        if d is None or len(d)<3: continue

        used+=1
        v=float(pd.to_numeric(d.volume,errors="coerce").fillna(0).sum())
        m=(float(d.close.iloc[-1])-float(d.close.iloc[0]))/max(float(d.close.iloc[0]),.01)

        if str(c.symbol).upper().endswith("CE"):
            ce+=v; cm.append(m)
        else:
            pe+=v; pm.append(m)

    if ce==0 and pe==0: return 0,"NEUTRAL",0,u

    ratio=ce/max(pe,1); ca=np.mean(cm) if cm else 0; pa=np.mean(pm) if pm else 0

    if ratio>=1.25 and ca>=pa: return 8,"BULLISH",ratio,u
    if ratio<=.80 and pa>=ca: return 8,"BEARISH",ratio,u
    return 0,"NEUTRAL",ratio,u

def stars(s):
    return "★★★★★" if s>=90 else "★★★★☆" if s>=80 else "★★★☆☆" if s>=70 else "★★☆☆☆"

def main():
    print(f"=== DIVINE INTRADAY 5M + 15M + DAILY FULL MARKET 2500 TOP 50 | {datetime.now(IST):%d %b %H:%M:%S IST} ===",flush=True)

    s=login()
    m=master()
    em_full=equity_master(m)
    print(f"Total NSE EQ in master: {len(em_full)}",flush=True)

    def is_etf(sym):
        u=sym.upper()
        return any(x in u for x in ETF_BLOCK)

    filtered_list=[]
    for _,r in em_full.iterrows():
        sym=r["symbol"]; clean=sym.replace("-EQ","").upper()
        if clean in IPO_BLOCK: continue
        if is_etf(clean): continue
        if len(clean)<=2: continue
        filtered_list.append(r)

    em_full=pd.DataFrame(filtered_list)
    em_full=em_full.head(SCAN_POOL)
    print(f"After ETF/IPO/Penny filter SCAN_POOL: {len(em_full)}",flush=True)

    om=option_master(m); us=all_underlyings(om)
    mbias,mval=market_bias(s)
    print(f"MARKET {mbias} {mval}",flush=True)

    qdf=quotes_full_market(s,em_full["token"].tolist())
    print(f"Total Quotes received: {len(qdf)}",flush=True)

    if qdf.empty:
        tg("⚠️ DIVINE INTRADAY\n\nNO SETUP\nREASON: Quotes empty / Market closed")
        return

    token_map={}; sym_map={}
    for _,row in em_full.iterrows(): sym_map[row["token"]]=row["symbol"]

    for r in qdf.to_dict("records"):
        try:
            tok=str(r.get("symbolToken") or r.get("token") or "")
            ltp=float(r.get("ltp") or r.get("last") or r.get("close") or 0)
            vol=float(r.get("volume") or r.get("tradeVolume") or r.get("vol") or 0)
            token_map[tok]=(ltp,vol)
        except: pass

    all_with_vol=[]
    for tok,(ltp,vol) in token_map.items():
        sym=sym_map.get(tok,"")
        if not sym: continue
        if ltp<MIN_PRICE: continue
        if vol<MIN_VOL: continue
        all_with_vol.append((sym,tok,ltp,vol))

    print(f"After Price>{MIN_PRICE} Vol>{MIN_VOL} filter: {len(all_with_vol)}",flush=True)

    all_with_vol=sorted(all_with_vol,key=lambda x:x[3],reverse=True)
    top50=all_with_vol[:50]

    print(f"\n=== TOP 50 HIGH VOLUME FROM {len(all_with_vol)} STOCKS ===",flush=True)
    for i,(sym,tok,ltp,vol) in enumerate(top50,1):
        print(f"{i:2d}. {sym:<18s} {ltp:8.2f} Vol:{int(vol)}",flush=True)

    if not top50:
        msg=f"No stock after pure filter | Market {mbias}"
        print(msg,flush=True); tg(msg); return

    to_analyze=top50[:INTRADAY_UNIVERSE]
    print(f"\nAnalyzing top {len(to_analyze)} for 5M + 15M + DAILY setup...",flush=True)

    results=[]
    for sym,tok,ltp,vol in to_analyze:
        print(f"Analyzing {sym} LTP {ltp} Vol {vol}",flush=True)
        res=analyze(sym,tok,s,mbias)
        if res: results.append(res)
        time.sleep(.2)

    if not results:
        msg=f"DIVINE INTRADAY {datetime.now(IST):%d %b %H:%M} | No setup in Top 50 Vol | Market {mbias}"
        print(msg,flush=True); tg(msg); return

    results=sorted(results,key=lambda x:x["tech"],reverse=True)[:TOP_SIGNALS]
    final_msgs=[]

    for z in results:
        opt_pts,opt_bias,ratio,_=option_flow(s,om,z,us)
        total=z["tech"]+z["market_pts"]+opt_pts
        if total<MIN_SCORE: continue

        star=stars(total)
        msg=(f"{star} {z['symbol']} {z['direction']} @ {z['entry']:.2f}\n"
             f"Tech:{z['tech']} Mkt:{z['market_pts']} Opt:{opt_pts}({opt_bias} R:{ratio:.2f}) Total:{total}\n"
             f"Setup:{z['setup']} Sec:{z['sector']} RSI:{z['rsi']:.1f} Volx:{z['volx']:.2f}x\n"
             f"TF: D:{z['daily']} 15M:{z['tf15']} 5M:{z['tf5']}\n"
             f"SL:{z['sl']:.2f} T1:{z['t1']:.2f} T2:{z['t2']:.2f} T3:{z['t3']:.2f}\n"
             f"Market:{mbias} | 5M+15M+DAILY | Full Market Top50")
        final_msgs.append(msg)

    if final_msgs:
        text="\n\n".join(final_msgs)
        print(text,flush=True); tg(text)
    else:
        msg=f"No high score in Full Market Top 50 | Market {mbias}"
        print(msg,flush=True); tg(msg)

if __name__=="__main__":
    main()
