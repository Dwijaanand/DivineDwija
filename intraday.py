import os,time,requests,pyotp,pandas as pd,numpy as np
from datetime import datetime,timedelta
from SmartApi import SmartConnect
import pytz
API_KEY=os.getenv("API_KEY");CLIENT_ID=os.getenv("CLIENT_ID");PASSWORD=os.getenv("PASSWORD");TOTP_SECRET=os.getenv("TOTP_SECRET")
TELEGRAM_BOT_TOKEN=os.getenv("TELEGRAM_BOT_TOKEN");TELEGRAM_CHAT_ID=os.getenv("TELEGRAM_CHAT_ID")
MASTER_URL="https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
MIN_PRICE=100;MIN_VOL=200000;TOP_UNIVERSE=60;TOP_SIGNALS=3;MIN_SCORE=70;WATCH_SCORE=50;DELAY=.25;MIN_SL_PCT=0.004
ENABLE_OPTION_FILTER=True;MIN_PCR_BUY=0.7;MAX_PCR_SELL=1.4
IST=pytz.timezone('Asia/Kolkata')
IPO_BLOCK={"PINELABS-EQ","MEESHO-EQ","LENSKART-EQ","GLASSWALL-EQ","URBANCO-EQ","GROWW-EQ","SKYWAYS-EQ","CUPID-EQ","LUMINO-EQ","PWL-EQ","RIR-EQ","SAMBHV-EQ","EMIL-EQ"}
def tg(x):
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        try: requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",data={"chat_id":TELEGRAM_CHAT_ID,"text":x},timeout=15)
        except: pass
def login():
    s=SmartConnect(api_key=API_KEY);r=s.generateSession(CLIENT_ID,PASSWORD,pyotp.TOTP(TOTP_SECRET).now())
    return s
def master():
    x=pd.DataFrame(requests.get(MASTER_URL,timeout=30).json())
    return x
def get_nfo_master(df):
    return df[(df["exch_seg"]=="NFO") & (df["instrumenttype"].isin(["OPTSTK","OPTIDX"]))].copy()
def quotes(s,tokens):
    out=[]
    for i in range(0,len(tokens),50):
        try:
            r=s.getMarketData("FULL",{"NSE":[str(x) for x in tokens[i:i+50]]});d=(r or {}).get("data",{})
            out+=(d.get("fetched",[]) if isinstance(d,dict) else [])
        except: pass
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
def is_old_enough(sym): return sym not in IPO_BLOCK
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
    if any(pd.isna(a[k]) for k in ["close","atr","rsi","volx","vwap"]):return None,"feature"
    buy=(25 if b.close>b.ema20>b.ema50 else 0)+(20 if a.close>a.ema9>a.ema20 else 0)+(15 if a.close>a.vwap else 0)+(15 if 55<=a.rsi<=75 else 0)+(15 if a.volx>=1.5 else 0)+(10 if a.close>a.prev20h else 0)
    sell=(25 if b.close<b.ema20<b.ema50 else 0)+(20 if a.close<a.ema9<a.ema20 else 0)+(15 if a.close<a.vwap else 0)+(15 if 25<=a.rsi<=45 else 0)+(15 if a.volx>=1.5 else 0)+(10 if a.close<a.prev20l else 0)
    direction="BUY" if buy>=sell else "SELL";sc=max(buy,sell);entry=float(a.close);atr=max(float(a.atr),entry*.003)
    if atr/entry < MIN_SL_PCT: return None,"sl_small"
    sl=entry-atr if direction=="BUY" else entry+atr
    return {"symbol":sym,"direction":direction,"score":int(sc),"entry":entry,"sl":sl,"t1":entry+(1.5*atr if direction=="BUY" else -1.5*atr),"t2":entry+(2.5*atr if direction=="BUY" else -2.5*atr),"rsi":float(a.rsi),"volx":float(a.volx),"token":str(tok),"ltp":entry},None

def option_bias(s, underlying, direction, nfo_df):
    # returns (pcr, bias_ok, msg)
    try:
        base=underlying.replace("-EQ","")
        df=nfo_df[nfo_df["name"]==base].copy()
        if df.empty: return 1.0, True, "No F&O"
        df["expiry"]=pd.to_datetime(df["expiry"],errors="coerce")
        exps=sorted(df["expiry"].dropna().unique())[:3]
        if len(exps)==0: return 1.0, True, "No expiry"
        df=df[df["expiry"].isin(exps)]
        # take ATM around underlying LTP - we will use quote for underlying already, so just take all strikes for 3 months
        # get OI from market data
        tokens=df["token"].astype(str).tolist()[:80] # limit for speed
        q=quotes(s, tokens)
        if q.empty: return 1.0, True, "No option quotes"
        q["symbolToken"]=q["symbolToken"].astype(str)
        df=df.merge(q[["symbolToken","openInterest","ltp"]],left_on="token",right_on="symbolToken",how="left")
        ce_oi=df[df["symbol"].str.endswith("CE")]["openInterest"].fillna(0).sum()
        pe_oi=df[df["symbol"].str.endswith("PE")]["openInterest"].fillna(0).sum()
        pcr = pe_oi / ce_oi if ce_oi>0 else 1.0
        if direction=="BUY":
            ok = pcr >= MIN_PCR_BUY # bullish when PE OI building
        else:
            ok = pcr <= MAX_PCR_SELL
        return round(pcr,2), ok, f"3M PCR {pcr:.2f}"
    except Exception as e:
        return 1.0, True, f"Opt err {e}"

def stars(s): return "★★★★★" if s>=85 else "★★★★☆" if s>=75 else "★★★☆☆" if s>=65 else "★★☆☆☆" if s>=55 else "★☆☆☆☆"
def main():
    print(f"=== AI INTRADAY V6.6 OPTION {datetime.now(IST):%d %b %H:%M IST} ===")
    s=login();df=master();m=df[(df["exch_seg"]=="NSE")&df["symbol"].astype(str).str.endswith("-EQ")].copy()
    nfo=get_nfo_master(df);m["token"]=m["token"].astype(str)
    print("NSE-EQ:",len(m),"NFO:",len(nfo))
    q=quotes(s,m.token.tolist())
    q["symbolToken"]=q["symbolToken"].astype(str);q["ltp"]=pd.to_numeric(q["ltp"],errors="coerce");q["tradeVolume"]=pd.to_numeric(q["tradeVolume"],errors="coerce")
    q=q.dropna(subset=["symbolToken","ltp","tradeVolume"]);q=q[(q.ltp>=MIN_PRICE)&(q.tradeVolume>=MIN_VOL)].sort_values("tradeVolume",ascending=False).head(TOP_UNIVERSE)
    q=q.merge(m[["symbol","token"]].drop_duplicates("token"),left_on="symbolToken",right_on="token",how="left").dropna(subset=["symbol"])
    stat={"candle":0,"feature":0,"ok":0,"score":0,"sl_small":0,"ipo_skip":0,"opt_skip":0};res=[]
    for _,r in q.iterrows():
        z,why=analyze(r.symbol,r.symbolToken,s)
        if not z:stat[why]+=1;continue
        stat["ok"]+=1
        if z["score"]>=MIN_SCORE:stat["score"]+=1
        if z["score"]>=WATCH_SCORE:res.append(z)
        print(f'{z["symbol"]:<18} {z["direction"]:<4} {z["score"]:>3} RSI {z["rsi"]:>5.1f}')
    res.sort(key=lambda x:x["score"],reverse=True)
    final_res=[]
    for z in res[:30]:
        if not is_old_enough(z["symbol"]):
            stat["ipo_skip"]+=1; continue
        # OPTION FILTER for next 3 months
        if ENABLE_OPTION_FILTER:
            pcr, ok, msg = option_bias(s, z["symbol"], z["direction"], nfo)
            z["pcr"]=pcr; z["opt_msg"]=msg
            print(f' -> {z["symbol"]} {msg} -> {"PASS" if ok else "FAIL"}')
            if not ok:
                stat["opt_skip"]+=1; continue
        else:
            z["pcr"]=0; z["opt_msg"]=""
        final_res.append(z); time.sleep(0.2)
    final_res.sort(key=lambda x:x["score"],reverse=True)
    sig=[x for x in final_res if x["score"]>=MIN_SCORE][:TOP_SIGNALS]
    msg=[f"⚡ AI INTRADAY V6.6 OPTION | {datetime.now(IST):%d-%b %H:%M IST}",f"NSE-EQ: {len(m)} | Liquid: {len(q)} | Analysed: {stat['ok']} | IPO-Skip: {stat['ipo_skip']} | Opt-Skip: {stat['opt_skip']}"]
    if sig:
        msg+=["","🔥 QUALIFYING SETUPS (Option Confirmed 3M)"]
        for i,z in enumerate(sig,1):msg.append(f"\n#{i} {z['symbol']} {z['direction']} {stars(z['score'])} ({z['score']})\nEntry ₹{z['entry']:.2f} | SL ₹{z['sl']:.2f} | T1 ₹{z['t1']:.2f}\nRSI {z['rsi']:.1f} | {z['opt_msg']}")
    else:
        msg+=["","⚠️ NO QUALIFYING SETUP (after option filter)"]
        if final_res:
            msg+=["","👀 WATCHLIST"]+[f"#{i} {z['symbol']} {z['direction']} {z['score']} | {z['opt_msg']}" for i,z in enumerate(final_res[:8],1)]
    msg+=["",f"Diag: candle {stat['candle']} | sl_small {stat['sl_small']}","Alert-only"]
    out="\n".join(msg);print("\n"+out);tg(out)
if __name__=="__main__":
    try: main()
    except Exception as e: tg(f"❌ ERROR\n{e}");raise
