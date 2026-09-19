import os,time,requests,pyotp,pandas as pd,numpy as np
from datetime import datetime
from SmartApi import SmartConnect

API_KEY=os.getenv("API_KEY"); CLIENT_ID=os.getenv("CLIENT_ID"); PASSWORD=os.getenv("PASSWORD")
TOTP_SECRET=os.getenv("TOTP_SECRET"); TG_BOT=os.getenv("TELEGRAM_BOT_TOKEN"); TG_CHAT=os.getenv("TELEGRAM_CHAT_ID")
MASTER_URL="https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
MIN_PRICE=50; MIN_VOL=100000; TOP_UNIVERSE=45; TOP_SIGNALS=5; DELAY=.15

def tg(msg):
    if TG_BOT and TG_CHAT:
        try: requests.post(f"https://api.telegram.org/bot{TG_BOT}/sendMessage",data={"chat_id":TG_CHAT,"text":msg},timeout=8)
        except: pass

def login():
    if not all([API_KEY,CLIENT_ID,PASSWORD,TOTP_SECRET]): raise RuntimeError("Credentials missing")
    a=SmartConnect(api_key=API_KEY)
    r=a.generateSession(CLIENT_ID,PASSWORD,pyotp.TOTP(TOTP_SECRET).now())
    if not r.get("status"): raise RuntimeError(f"Angel login failed: {r}")
    return a

def master():
    d=requests.get(MASTER_URL,timeout=30).json()
    x=pd.DataFrame(d)
    if "exch_seg" not in x.columns: raise RuntimeError("NSE master format changed: exch_seg missing")
    x=x[(x["exch_seg"]=="NSE")&x["symbol"].astype(str).str.endswith("-EQ")].copy()
    x["symbol"]=x["symbol"].astype(str).str.replace("-EQ","",regex=False)
    x["token"]=x["token"].astype(str)
    return x[["symbol","token"]].drop_duplicates("symbol")

def quotes(a,rows):
    out=[]
    for i in range(0,len(rows),50):
        b=rows.iloc[i:i+50]
        try:
            r=a.getMarketData({"mode":"FULL","exchangeTokens":{"NSE":b.token.tolist()}})
            if r.get("status"):
                for q in r.get("data",{}).get("fetched",[]) or []:
                    out.append({"token":str(q.get("symbolToken")),"ltp":float(q.get("ltp") or 0),
                                "vol":float(q.get("tradeVolume") or 0)})
        except Exception: pass
        time.sleep(DELAY)
    return pd.DataFrame(out)

def candles(a,token,interval,days):
    p={"exchange":"NSE","symboltoken":str(token),"interval":interval,
       "fromdate":(pd.Timestamp.now()-pd.Timedelta(days=days)).strftime("%Y-%m-%d 09:15"),
       "todate":pd.Timestamp.now().strftime("%Y-%m-%d %H:%M")}
    try:
        r=a.getCandleData(p)
        if not r.get("status") or not r.get("data"): return pd.DataFrame()
        d=pd.DataFrame(r["data"],columns=["time","open","high","low","close","volume"])
        for c in ["open","high","low","close","volume"]: d[c]=pd.to_numeric(d[c],errors="coerce")
        return d.dropna()
    except Exception: return pd.DataFrame()

def feat(d):
    if len(d)<55:return None
    d=d.copy(); c=d.close
    d["e9"]=c.ewm(span=9,adjust=False).mean(); d["e20"]=c.ewm(span=20,adjust=False).mean(); d["e50"]=c.ewm(span=50,adjust=False).mean()
    z=c.diff(); up=z.clip(lower=0).rolling(14).mean(); dn=(-z.clip(upper=0)).rolling(14).mean()
    d["rsi"]=100-100/(1+up/dn.replace(0,np.nan))
    d["vwap"]=(d.close*d.volume).cumsum()/d.volume.replace(0,np.nan).cumsum()
    tr=pd.concat([d.high-d.low,(d.high-d.close.shift()).abs(),(d.low-d.close.shift()).abs()],axis=1).max(axis=1)
    d["atr"]=tr.rolling(14).mean(); d["volx"]=d.volume/d.volume.rolling(20).mean()
    d["hh"]=d.high.shift(1).rolling(20).max(); d["ll"]=d.low.shift(1).rolling(20).min()
    return d

def scan(a,m):
    q=quotes(a,m)
    if q.empty: return []
    m=m.merge(q,on="token",how="inner")
    m=m[(m.ltp>=MIN_PRICE)&(m.vol>=MIN_VOL)].sort_values("vol",ascending=False).head(TOP_UNIVERSE)
    print("Liquid candidates:",len(m))
    ans=[]
    for _,r in m.iterrows():
        d5=feat(candles(a,r.token,"FIVE_MINUTE",3)); d15=feat(candles(a,r.token,"FIFTEEN_MINUTE",7))
        if d5 is None or d15 is None: continue
        x=d5.iloc[-2]; y=d15.iloc[-2]
        if not np.isfinite(x.atr) or x.atr<=0: continue
        lb=0; ss=0
        lb+=25 if y.close>y.e20>y.e50 else 0
        lb+=20 if x.close>x.e9>x.e20 else 0
        lb+=15 if x.close>x.vwap else 0
        lb+=15 if 55<=x.rsi<=75 else 0
        lb+=15 if x.volx>=1.5 else 0
        lb+=10 if x.close>x.hh else 0
        ss+=25 if y.close<y.e20<y.e50 else 0
        ss+=20 if x.close<x.e9<x.e20 else 0
        ss+=15 if x.close<x.vwap else 0
        ss+=15 if 25<=x.rsi<=45 else 0
        ss+=15 if x.volx>=1.5 else 0
        ss+=10 if x.close<x.ll else 0
        score=max(lb,ss)
        if score<60: continue
        side="BUY" if lb>=ss else "SELL"; e=float(x.close); atr=float(x.atr)
        sl=e-atr if side=="BUY" else e+atr; risk=abs(e-sl)
        t1=e+1.5*risk if side=="BUY" else e-1.5*risk
        t2=e+2.5*risk if side=="BUY" else e-2.5*risk
        ans.append((score,{"symbol":r["symbol"],"side":side,"score":score,"entry":e,"sl":sl,"t1":t1,"t2":t2,"rsi":float(x.rsi),"volx":float(x.volx)}))
        time.sleep(DELAY)
    return [x[1] for x in sorted(ans,key=lambda z:z[0],reverse=True)[:TOP_SIGNALS]]

def message(rows):
    if not rows:return "⚠️ AI INTRADAY SCANNER\nNo qualifying setup found."
    s=f"🔥 AI INTRADAY SCANNER v1.2\n{datetime.now().strftime('%d-%m-%Y %H:%M')} IST\n15M Trend + 5M Setup\nCompleted Candle Logic\nStrongest Setup First\n\n"
    for i,r in enumerate(rows,1):
        n=max(1,min(5,round(r["score"]/20))); stars="★"*n+"☆"*(5-n)
        s+=f"#{i} {r['symbol']} {stars}\n{r['side']} | Score {r['score']}/100\nEntry ₹{r['entry']:.2f}\nSL ₹{r['sl']:.2f}\nT1 ₹{r['t1']:.2f} | T2 ₹{r['t2']:.2f}\nRSI {r['rsi']:.1f} | Vol {r['volx']:.1f}x\n\n"
    return s

def main():
    print("AI INTRADAY SCANNER v1.2")
    a=login(); m=master(); print("NSE-EQ:",len(m))
    rows=scan(a,m); s=message(rows); print(s); tg(s)

if __name__=="__main__": main()

