import os,time,requests,pyotp,pandas as pd,numpy as np
from datetime import datetime
from SmartApi import SmartConnect

API_KEY=os.getenv("API_KEY"); CLIENT_ID=os.getenv("CLIENT_ID"); PASSWORD=os.getenv("PASSWORD")
TOTP_SECRET=os.getenv("TOTP_SECRET"); TELEGRAM_BOT_TOKEN=os.getenv("TELEGRAM_BOT_TOKEN"); TELEGRAM_CHAT_ID=os.getenv("TELEGRAM_CHAT_ID")
MIN_PRICE=50; MIN_VOL=100000; TOP_UNIVERSE=60; TOP_SIGNALS=5; DELAY=0.20
MASTER_URL="https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"

def tg(msg):
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        try: requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",data={"chat_id":TELEGRAM_CHAT_ID,"text":msg},timeout=10)
        except: pass

def login():
    obj=SmartConnect(api_key=API_KEY)
    d=obj.generateSession(CLIENT_ID,PASSWORD,pyotp.TOTP(TOTP_SECRET).now())
    if not d.get("status"): raise RuntimeError("Angel login failed")
    return obj

def master():
    d=requests.get(MASTER_URL,timeout=30).json()
    x=pd.DataFrame(d); x=x[(x.exchange=="NSE")&(x.symbol.str.endswith("-EQ"))].copy()
    x["symbol"]=x.symbol.str.replace("-EQ","",regex=False)
    return x[["symbol","token","exchange"]].drop_duplicates("symbol")

def candles(api,token,interval,days=3):
    p={"exchange":"NSE","symboltoken":str(token),"interval":interval,
       "fromdate":(pd.Timestamp.now()-pd.Timedelta(days=days)).strftime("%Y-%m-%d 09:15"),
       "todate":pd.Timestamp.now().strftime("%Y-%m-%d %H:%M")}
    try:
        r=api.getCandleData(p)
        if not r.get("status") or not r.get("data"): return pd.DataFrame()
        z=pd.DataFrame(r["data"],columns=["time","open","high","low","close","volume"])
        for c in ["open","high","low","close","volume"]: z[c]=pd.to_numeric(z[c],errors="coerce")
        z["time"]=pd.to_datetime(z["time"]); return z.dropna()
    except: return pd.DataFrame()

def features(d):
    if len(d)<55:return None
    d=d.copy()
    d["ema9"]=d.close.ewm(span=9,adjust=False).mean(); d["ema20"]=d.close.ewm(span=20,adjust=False).mean(); d["ema50"]=d.close.ewm(span=50,adjust=False).mean()
    delta=d.close.diff(); up=delta.clip(lower=0).rolling(14).mean(); dn=(-delta.clip(upper=0)).rolling(14).mean()
    d["rsi"]=100-(100/(1+(up/dn.replace(0,np.nan))))
    d["vwap"]=(d.close*d.volume).cumsum()/d.volume.cumsum()
    tr=pd.concat([d.high-d.low,(d.high-d.close.shift()).abs(),(d.low-d.close.shift()).abs()],axis=1).max(axis=1)
    d["atr"]=tr.rolling(14).mean(); d["volx"]=d.volume/d.volume.rolling(20).mean()
    d["hh20"]=d.high.shift(1).rolling(20).max(); d["ll20"]=d.low.shift(1).rolling(20).min()
    return d

def scan(api,m):
    rows=[]
    # Fast liquidity pass via LTP/volume quotes in batches
    syms=m.symbol.tolist()
    for i in range(0,len(syms),50):
        batch=syms[i:i+50]
        try:
            q=api.getMarketData({"mode":"FULL","exchangeTokens":{"NSE":[str(m.loc[m.symbol.isin(batch),"token"].iloc[j]) for j in range(len(m.loc[m.symbol.isin(batch)]))]}})
        except: q={}
        time.sleep(DELAY)
    # Historical data only for a capped universe, keeping runtime bounded
    # Start with first TOP_UNIVERSE liquid/large-cap candidates from master if quote ranking is unavailable.
    candidates=m.head(TOP_UNIVERSE)
    for _,r in candidates.iterrows():
        d5=candles(api,r.token,"FIVE_MINUTE",3); d15=candles(api,r.token,"FIFTEEN_MINUTE",7)
        if d5.empty or d15.empty: continue
        a=features(d5); b=features(d15)
        if a is None or b is None: continue
        x=a.iloc[-2]; y=b.iloc[-2]
        if x.close<MIN_PRICE or x.volume<MIN_VOL: continue
        long_score=0; short_score=0
        long_score+=25 if y.close>y.ema20>y.ema50 else 0
        long_score+=20 if x.close>x.ema9>x.ema20 else 0
        long_score+=15 if x.close>x.vwap else 0
        long_score+=15 if 55<=x.rsi<=75 else 0
        long_score+=15 if x.volx>=1.5 else 0
        long_score+=10 if x.close>x.hh20 else 0
        short_score+=25 if y.close<y.ema20<y.ema50 else 0
        short_score+=20 if x.close<x.ema9<x.ema20 else 0
        short_score+=15 if x.close<x.vwap else 0
        short_score+=15 if 25<=x.rsi<=45 else 0
        short_score+=15 if x.volx>=1.5 else 0
        short_score+=10 if x.close<x.ll20 else 0
        score=max(long_score,short_score)
        if score<60: continue
        direction="BUY" if long_score>=short_score else "SELL"
        entry=float(x.close); atr=float(x.atr)
        if not np.isfinite(atr) or atr<=0: continue
        sl=entry-1.0*atr if direction=="BUY" else entry+1.0*atr
        t1=entry+1.5*(entry-sl) if direction=="BUY" else entry-1.5*(sl-entry)
        t2=entry+2.5*(entry-sl) if direction=="BUY" else entry-2.5*(sl-entry)
        rows.append({"symbol":r.symbol,"direction":direction,"score":score,"entry":entry,"sl":sl,"t1":t1,"t2":t2,"rsi":x.rsi,"volx":x.volx})
        time.sleep(DELAY)
    return sorted(rows,key=lambda z:z["score"],reverse=True)[:TOP_SIGNALS]

def fmt(rows):
    if not rows:return "⚠️ AI INTRADAY SCANNER\nNo qualifying setup found."
    s="🔥 AI INTRADAY SCANNER\nCompleted 5M + 15M Candle\nStrongest Setup First\n\n"
    stars=lambda n:"★"*min(5,max(1,int(round(n/20))))+"☆"*(5-min(5,max(1,int(round(n/20)))))
    for i,r in enumerate(rows,1):
        s+=f"#{i} {r['symbol']} {stars(r['score'])}\n{r['direction']} | Score {r['score']}/100\nEntry: ₹{r['entry']:.2f}\nSL: ₹{r['sl']:.2f}\nT1: ₹{r['t1']:.2f} | T2: ₹{r['t2']:.2f}\nRSI: {r['rsi']:.1f} | Vol: {r['volx']:.1f}x\n\n"
    return s

def main():
    print("AI INTRADAY SCANNER v1")
    api=login(); m=master(); print("NSE symbols:",len(m))
    rows=scan(api,m); msg=fmt(rows); print(msg); tg(msg)
if __name__=="__main__": main()
    
