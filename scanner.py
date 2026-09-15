import os,time,requests,pyotp,pandas as pd
from datetime import datetime,timedelta
from SmartApi import SmartConnect

API=os.getenv("API_KEY"); CID=os.getenv("CLIENT_ID")
PWD=os.getenv("PASSWORD"); TOTP=os.getenv("TOTP_SECRET")
TG=os.getenv("TELEGRAM_BOT_TOKEN"); CHAT=os.getenv("TELEGRAM_CHAT_ID")
MASTER="https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"

TOP=80
MIN_LTP=50
MIN_VOL=100000
MIN_AVG20=50000
MIN_VOLX=2.0
HISTORY=380
DELAY=.7
RETRIES=4
WAIT=8
EXCLUDED={"LTIM","TATAMOTORS"}
obj=None

def telegram(msg):
    if not TG or not CHAT:return
    try: requests.post(f"https://api.telegram.org/bot{TG}/sendMessage",data={"chat_id":CHAT,"text":msg,"parse_mode":"Markdown"},timeout=15)
    except Exception as e: print("Telegram error:",e,flush=True)

def rate_error(e):
    s=str(e).lower()
    return any(x in s for x in ["access rate","exceeding access rate","rate limit","too many requests","access denied"])

def market_closed():
    n=datetime.now(); return n.hour>15 or (n.hour==15 and n.minute>=30)

def rsi(series, period=14):
    try:
        delta = series.diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
        rs = gain / loss
        return 100 - (100 / (1 + rs))
    except: return None

def hist(token):
    for a in range(1,RETRIES+1):
        try:
            p={"exchange":"NSE","symboltoken":str(token),"interval":"ONE_DAY","fromdate":(datetime.now()-timedelta(days=HISTORY)).strftime("%Y-%m-%d %H:%M"),"todate":datetime.now().strftime("%Y-%m-%d %H:%M")}
            r=obj.getCandleData(p)
            if r and r.get("data"): return pd.DataFrame(r["data"],columns=["time","open","high","low","close","volume"])
            return None
        except Exception as e:
            if rate_error(e): time.sleep(WAIT*a)
            else: return None
    return None

def clean_daily(df):
    if df is None or df.empty:return None
    try:
        df=df.copy()
        df["time"]=pd.to_datetime(df["time"],errors="coerce")
        for c in ["open","high","low","close","volume"]: df[c]=pd.to_numeric(df[c],errors="coerce")
        df=df.dropna(subset=["time","open","high","low","close","volume"])
        df=df.sort_values("time").drop_duplicates("time",keep="last")
        if not market_closed() and len(df):
            if df["time"].iloc[-1].date()==datetime.now().date(): df=df.iloc[:-1]
        return df.reset_index(drop=True)
    except: return None

def weekly(df):
    try:
        w=df.copy().set_index("time").resample("W-FRI").agg({"open":"first","high":"max","low":"min","close":"last","volume":"sum"}).dropna()
        if not market_closed() and len(w):
            today=datetime.now(); friday=today+timedelta(days=4-today.weekday())
            if w.index[-1].date()>=friday.date(): w=w.iloc[:-1]
        return w
    except: return None

def monthly(df):
    try:
        m=df.copy().set_index("time").resample("M").agg({"open":"first","high":"max","low":"min","close":"last","volume":"sum"}).dropna()
        if len(m)>1: m=m.iloc[:-1] # current month remove for safety
        return m
    except: return None

def bulk_quotes(tokens):
    out={}
    for i in range(0,len(tokens),50):
        batch=tokens[i:i+50]
        for a in range(1,RETRIES+1):
            try:
                r=obj.getMarketData("FULL",{"NSE":batch}); data=(r or {}).get("data",{}); rows=[]
                if isinstance(data,dict): rows=data.get("fetched",[]) or data.get("data",[])
                elif isinstance(data,list): rows=data
                for q in rows:
                    if isinstance(q,dict):
                        t=str(q.get("symbolToken",""))
                        if t:out[t]=q
                break
            except: time.sleep(WAIT*a if True else 3*a)
        time.sleep(1.1)
    return out

# --- LOGIN ---
print("\n ANGEL ONE SWING SCANNER V3.5.0 FILTERED",flush=True)
obj=SmartConnect(api_key=API)
sess=obj.generateSession(CID,PWD,pyotp.TOTP(TOTP).now())

print("Loading NSE master...",flush=True)
master=requests.get(MASTER,timeout=30).json()
tokens={}
for x in master:
    try:
        seg=str(x.get("exch_seg","")).lower(); sym=str(x.get("symbol","")).upper()
        if seg in ("nse","nse_cm") and sym.endswith("-EQ"):
            s=sym[:-3]
            if s not in EXCLUDED and x.get("token"): tokens[s]=str(x["token"])
    except: pass
stocks=list(tokens.items())
quotes=bulk_quotes([t for _,t in stocks])
candidates=[]
for sym,token in stocks:
    x=quotes.get(token,{})
    try:
        ltp=float(x.get("ltp",0) or 0); vol=float(x.get("tradeVolume",0) or 0)
        if ltp>=MIN_LTP and vol>=MIN_VOL: candidates.append({"sym":sym,"token":token,"ltp":ltp,"volume":vol})
    except: pass
candidates.sort(key=lambda x:x["volume"],reverse=True)
candidates=candidates[:TOP]
print(f"TOP {len(candidates)} selected",flush=True)

# --- PHASE 2 ---
picks=[]
for n,x in enumerate(candidates,1):
    print(f"[{n}/{len(candidates)}] {x['sym']}",flush=True)
    df=clean_daily(hist(x["token"]))
    if df is None or len(df)<200: continue
    last=df.iloc[-1]
    avg20=df["volume"].iloc[-21:-1].mean()
    if not avg20 or avg20<MIN_AVG20: continue
    volx=float(last["volume"])/float(avg20)
    if volx<MIN_VOLX: continue

    dma200=df["close"].rolling(200).mean().iloc[-1]
    high52=df["high"].tail(252).max()
    if pd.isna(dma200) or pd.isna(high52): continue
    close=float(last["close"]); openp=float(last["open"]); low=float(last["low"])

    # CORE STRATEGY - LOCKED
    near_high=close>=float(high52)*.92
    green=close>openp

    # WEEKLY
    w=weekly(df)
    if w is None or len(w)<40: continue
    w["sma40"]=w["close"].rolling(40).mean()
    weekly_up=float(w["close"].iloc[-1])>float(w["sma40"].iloc[-1])

    # --- NEW FILTERS (WITHOUT CHANGING CORE) ---
    # 1. Monthly bullish
    m=monthly(df)
    if m is None or len(m)<12: continue
    m["sma10"]=m["close"].rolling(10).mean()
    monthly_up=float(m["close"].iloc[-1])>float(m["sma10"].iloc[-1])

    # 2. Momentum RSI > 55
    r = rsi(df["close"],14)
    if r is None or pd.isna(r.iloc[-1]): continue
    mom = float(r.iloc[-1])>55

    # 3. UC/LC filter - last 90 days me 5+ circuit days nahi hone chahiye
    last90=df.tail(90)
    circuits=0
    for _,row in last90.iterrows():
        try:
            rng=abs(float(row["close"])-float(row["open"]))/float(row["open"])*100 if float(row["open"])!=0 else 0
            if rng>15: circuits+=1
        except: pass
    no_operator = circuits<=5

    if weekly_up and monthly_up and near_high and green and mom and no_operator:
        t1=close*1.05; t2=close*1.08
        sl = max(low*0.99, close*0.965) # 3.5% max SL - improved
        picks.append({"Stock":x['sym'],"LTP":close,"52W":float(high52),"VolX":volx,"SL":sl,"T1":t1,"T2":t2,"RSI":float(r.iloc[-1]),"Circuits":circuits})
        print(f" BUY FOUND: {x['sym']} Vol {volx:.2f}x RSI {float(r.iloc[-1]):.0f}",flush=True)
    time.sleep(DELAY)

picks.sort(key=lambda x:x["VolX"],reverse=True)
now=datetime.now().strftime("%d %b %I:%M %p")
mode="Completed Daily Candle" if market_closed() else "Previous Completed Daily Candle"

if not picks:
    msg=(f"📉 *PURA NSE SCAN - {now}*\nTotal NSE: {len(stocks)}\nTop: {len(candidates)}\nFinal BUY: 0\nMode: {mode}\n\n_Aaj filter me koi pass nahi hua._")
else:
    msg=(f"🚀 *PURA NSE BUY - {now}* 🚀\nTotal NSE: {len(stocks)}\nTop Candidates: {len(candidates)}\nBUY: {len(picks)}\nMode: {mode}\nFilters: Weekly+Monthly Bullish | RSI>55 | No UC/LC\n\n")
    for r in picks:
        msg+=(f"*{r['Stock']}*\nLTP: ₹{r['LTP']:.2f} | 52W: ₹{r['52W']:.2f}\nVol: {r['VolX']:.2f}x | RSI: {r['RSI']:.0f} | Circuits(90d): {r['Circuits']}\nSL: ₹{r['SL']:.2f} | TGT: ₹{r['T1']:.2f} / ₹{r['T2']:.2f}\n\n")

print("\n"+msg,flush=True)
telegram(msg)
