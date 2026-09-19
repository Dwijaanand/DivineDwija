import os,time,requests,pyotp,pandas as pd,numpy as np
from datetime import datetime,timedelta
from SmartApi import SmartConnect
import pytz

API_KEY=os.getenv("API_KEY");CLIENT_ID=os.getenv("CLIENT_ID");PASSWORD=os.getenv("PASSWORD");TOTP_SECRET=os.getenv("TOTP_SECRET")
TELEGRAM_BOT_TOKEN=os.getenv("TELEGRAM_BOT_TOKEN");TELEGRAM_CHAT_ID=os.getenv("TELEGRAM_CHAT_ID")
MASTER_URL="https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
MIN_PRICE=100;MIN_VOL=200000;TOP_UNIVERSE=60;TOP_SIGNALS=3;MIN_SCORE=70;WATCH_SCORE=50;DELAY=.25;MIN_SL_PCT=0.004
IST=pytz.timezone('Asia/Kolkata')

def tg(x):
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        try: requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",data={"chat_id":TELEGRAM_CHAT_ID,"text":x},timeout=15)
        except: pass

def login():
    if not all([API_KEY,CLIENT_ID,PASSWORD,TOTP_SECRET]): raise RuntimeError("Credentials missing")
    s=SmartConnect(api_key=API_KEY);r=s.generateSession(CLIENT_ID,PASSWORD,pyotp.TOTP(TOTP_SECRET).now())
    if not r or not r.get("status"): raise RuntimeError(f"Login failed: {r}")
    return s

def master():
    x=pd.DataFrame(requests.get(MASTER_URL,timeout=30).json())
    x=x[(x["exch_seg"]=="NSE")&x["symbol"].astype(str).str.endswith("-EQ")].copy()
    x["token"]=x["token"].astype(str);x["symbol"]=x["symbol"].astype(str);return x

def quotes(s,tokens):
    out=[]
    for i in range(0,len(tokens),50):
        try:
            r=s.getMarketData("FULL",{"NSE":[str(x) for x in tokens[i:i+50]]});d=(r or {}).get("data",{})
            out+=(d.get("fetched",[]) if isinstance(d,dict) else [])
        except Exception as e: print("Quote error:",e)
        time.sleep(1.05)
    return pd.DataFrame(out)

def candles(s,tok,days,interval):
    try:
        e=datetime.now();b=e-timedelta(days=days)
        r=s.getCandleData({"exchange":"NSE","symboltoken":str(tok),"interval":interval,"fromdate":b.strftime("%Y-%m-%d %H:%M"),"todate":e.strftime("%Y-%m-%d %H:%M")})
        d=(r or {}).get("data")
        if not d:return None
        x=pd.DataFrame(d,columns=["timestamp","open","high","low","close","volume"])
        x["timestamp"]=pd.to_datetime(x.timestamp,errors="coerce")
        for c in ["open","high","low","close","volume"]:x[c]=pd.to_numeric(x[c],errors="coerce")
        return x.dropna(subset=["timestamp","close"]).sort_values("timestamp").reset_index(drop=True)
    except: return None

def is_old_enough(tok,s):
    try:
        d=candles(s,tok,70,"ONE_DAY")
        return d is not None and len(d)>=60
    except:
        return False

def rsi(s,n=14):
    d=s.diff();u=d.clip(lower=0).ewm(alpha=1/n,adjust=False).mean();v=(-d.clip(upper=0)).ewm(alpha=1/n,adjust=False).mean()
    return 100-100/(1+u/v.replace(0,np.nan))

def feat(x):
    x=x.copy();x["ema9"]=x.close.ewm(span=9,adjust=False).mean();x["ema20"]=x.close.ewm(span=20,adjust=False).mean();x["ema50"]=x.close.ewm(span=50,adjust=False).mean();x["rsi"]=rsi(x.close)
    tr=pd.concat([x.high-x.low,(x.high-x.close.shift()).abs(),(x.low-x.close.shift()).abs()],axis=1).max(axis=1);x["atr"]=tr.ewm(span=14,adjust=False).mean();x["vavg20"]=x.volume.rolling(20).mean();x["volx"]=x.volume/x.vavg20.replace(0,np.nan)
    x["prev20h"]=x.high.shift(1).rolling(20).max();x["prev20l"]=x.low.shift(1).rolling(20).min()
    tp=(x.high+x.low+x.close)/3;day=x.timestamp.dt.date;x["pv"]=tp*x.volume;x["cv"]=x.volume.groupby(day).cumsum();x["cpv"]=x.pv.groupby(day).cumsum();x["vwap"]=x.cpv/x.cv.replace(0,np.nan)
    return x

def analyze(sym,tok,s):
    d5=candles(s,tok,10,"FIVE_MINUTE");time.sleep(DELAY);d15=candles(s,tok,20,"FIFTEEN_MINUTE")
    if d5 is None or d15 is None or len(d5)<60 or len(d15)<60:return None,"candle"
    d5=feat(d5);d15=feat(d15);a=d5.iloc[-2];b=d15.iloc[-2]
    if any(pd.isna(a[k]) for k in ["close","atr","rsi","volx","vwap"]) or pd.isna(a.prev20h) or pd.isna(a.prev20l):return None,"feature"
    buy=(25 if b.close>b.ema20>b.ema50 else 0)+(20 if a.close>a.ema9>a.ema20 else 0)+(15 if a.close>a.vwap else 0)+(15 if 55<=a.rsi<=75 else 0)+(15 if a.volx>=1.5 else 0)+(10 if a.close>a.prev20h else 0)
    sell=(25 if b.close<b.ema20<b.ema50 else 0)+(20 if a.close<a.ema9<a.ema20 else 0)+(15 if a.close<a.vwap else 0)+(15 if 25<=a.rsi<=45 else 0)+(15 if a.volx>=1.5 else 0)+(10 if a.close<a.prev20l else 0)
    direction="BUY" if buy>=sell else "SELL";sc=max(buy,sell);entry=float(a.close);atr=max(float(a.atr),entry*.003)
    if atr/entry < MIN_SL_PCT: return None,"sl_small"
    sl=entry-atr if direction=="BUY" else entry+atr
    return {"symbol":sym,"direction":direction,"score":int(sc),"entry":entry,"sl":sl,"t1":entry+(1.5*atr if direction=="BUY" else -1.5*atr),"t2":entry+(2.5*atr if direction=="BUY" else -2.5*atr),"rsi":float(a.rsi),"volx":float(a.volx),"token":str(tok)},None

def stars(s): return "★★★★★" if s>=85 else "★★★★☆" if s>=75 else "★★★☆☆" if s>=65 else "★★☆☆☆" if s>=55 else "★☆☆☆☆"

def main():
    print(f"=== AI INTRADAY V6.2 FAST {datetime.now(IST):%d %b %H:%M IST} ===");s=login();m=master();print("NSE-EQ:",len(m));q=quotes(s,m.token.tolist())
    if q.empty: tg("⚠️ AI INTRADAY V6.2\nQuote API returned no data.");return
    q["symbolToken"]=q["symbolToken"].astype(str);q["ltp"]=pd.to_numeric(q["ltp"],errors="coerce");q["tradeVolume"]=pd.to_numeric(q["tradeVolume"],errors="coerce")
    q=q.dropna(subset=["symbolToken","ltp","tradeVolume"]);q=q[(q.ltp>=MIN_PRICE)&(q.tradeVolume>=MIN_VOL)].sort_values("tradeVolume",ascending=False).head(TOP_UNIVERSE)
    q=q.merge(m[["symbol","token"]].drop_duplicates("token"),left_on="symbolToken",right_on="token",how="left").dropna(subset=["symbol"]);print("Liquid:",len(q))
    stat={"candle":0,"feature":0,"ok":0,"score":0,"sl_small":0,"ipo_skip":0};res=[]
    # PHASE 1: Fast scan without daily calls
    for _,r in q.iterrows():
        z,why=analyze(r.symbol,r.symbolToken,s)
        if not z:stat[why]+=1;continue
        stat["ok"]+=1
        if z["score"]>=MIN_SCORE:stat["score"]+=1
        if z["score"]>=WATCH_SCORE:res.append(z)
        print(f'{z["symbol"]:<18} {z["direction"]:<4} {z["score"]:>3} RSI {z["rsi"]:>5.1f} VolX {z["volx"]:>5.2f}')
    res.sort(key=lambda x:x["score"],reverse=True)
    # PHASE 2: IPO check only on top candidates
    final_res=[]
    for z in res[:15]:
        if not is_old_enough(z["token"], s):
            print(f"{z['symbol']} skip - IPO/new"); stat["ipo_skip"]+=1; time.sleep(0.3); continue
        final_res.append(z)
    final_res.sort(key=lambda x:x["score"],reverse=True)
    sig=[x for x in final_res if x["score"]>=MIN_SCORE][:TOP_SIGNALS]
    msg=[f"⚡ AI INTRADAY V6.2 | {datetime.now(IST):%d-%b %H:%M IST}",f"NSE-EQ: {len(m)} | Liquid: {len(q)} | Analysed: {stat['ok']} | IPO-Skip: {stat['ipo_skip']}",f"Score ≥{MIN_SCORE}: {stat['score']} | Alert limit: {TOP_SIGNALS}"]
    if sig:
        msg+=["","🔥 QUALIFYING SETUPS"]
        for i,z in enumerate(sig,1):msg.append(f"\n#{i} {z['symbol']} {z['direction']} {stars(z['score'])} ({z['score']})\nEntry ₹{z['entry']:.2f} | SL ₹{z['sl']:.2f} | T1 ₹{z['t1']:.2f} | T2 ₹{z['t2']:.2f}\nRSI {z['rsi']:.1f} | Vol {z['volx']:.2f}x")
    else:
        msg+=["","⚠️ NO QUALIFYING SETUP"]
        if final_res:
            msg+=["","👀 NEAR-MISS WATCHLIST"]+[f"#{i} {z['symbol']} {z['direction']} {stars(z['score'])} {z['score']} | RSI {z['rsi']:.1f} | Vol {z['volx']:.2f}x" for i,z in enumerate(final_res[:8],1)]
        else:msg+=["No stock reached the watch score."]
    msg+=["",f"Diag: candle {stat['candle']} | feat {stat['feature']} | sl_small {stat['sl_small']}","Alert-only • No auto orders"]
    out="\n".join(msg);print("\n"+out);tg(out)

if __name__=="__main__":
    try: main()
    except Exception as e: tg(f"❌ AI INTRADAY ERROR\n{e}");raise
