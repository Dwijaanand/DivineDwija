import os,time,requests,pyotp,pandas as pd,numpy as np,re,difflib,logging
from datetime import datetime,timedelta
from SmartApi import SmartConnect
import pytz
logging.getLogger("smartapi.smartConnect").setLevel(logging.ERROR)

API_KEY=os.getenv("API_KEY");CLIENT_ID=os.getenv("CLIENT_ID");PASSWORD=os.getenv("PASSWORD");TOTP_SECRET=os.getenv("TOTP_SECRET")
TELEGRAM_BOT_TOKEN=os.getenv("TELEGRAM_BOT_TOKEN");TELEGRAM_CHAT_ID=os.getenv("TELEGRAM_CHAT_ID")
MASTER_URL="https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
MIN_PRICE=100;MIN_VOL=200000;TOP_UNIVERSE=80;TOP_SIGNALS=3;MIN_SCORE=70
MIN_VOLX=1.2;DELAY=.25;MIN_SL_PCT=.004
OPTION_MAX=6;OPTION_DAYS=5;OPTION_STRIKES=2;OPTION_DELAY=.2
IST=pytz.timezone("Asia/Kolkata");OPTION_CACHE={}
UNDERLYING_FIX={"MOTHERSON":"MOTHERSUMI","M_M":"M&M","M&M":"M&M","BAJAJ-AUTO":"BAJAJAUTO","BAJAJ_AUTO":"BAJAJAUTO"}

# IPO / New listing block list
IPO_BLOCK = {"GLASSWALL","SAMBHV","PINELABS","TATATECH","IREDA","MAMA","DOMS","KRN","BLS","BAJAJHFL"}

def tg(x):
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        try:requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",data={"chat_id":TELEGRAM_CHAT_ID,"text":x},timeout=12)
        except:pass

def login():
    s=SmartConnect(api_key=API_KEY)
    s.generateSession(CLIENT_ID,PASSWORD,pyotp.TOTP(TOTP_SECRET).now());return s

def master():
    x=pd.DataFrame(requests.get(MASTER_URL,timeout=30).json())
    x["token"]=x["token"].astype(str);x["symbol"]=x["symbol"].astype(str);return x

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
        m=re.match(r'^([A-Z&\-\_]+)',s.upper())
        if m and len(m.group(1))>=3:u.add(m.group(1))
    return list(u)

def best_underlying(base,us):
    base=base.upper()
    if base in UNDERLYING_FIX:return UNDERLYING_FIX[base]
    if base in us:return base
    c=[u for u in us if u[:3]==base[:3]]
    z=difflib.get_close_matches(base,c,n=1,cutoff=.82)
    return z[0] if z else base

def quotes(s,tokens):
    out=[]
    for i in range(0,len(tokens),50):
        try:
            r=s.getMarketData("FULL",{"NSE":[str(x) for x in tokens[i:i+50]]});d=(r or {}).get("data",{})
            out+=(d.get("fetched",[]) if isinstance(d,dict) else [])
        except:pass
        time.sleep(1.02)
    return pd.DataFrame(out)

def candles(s,tok,days,interval,exchange="NSE"):
    try:
        e=datetime.now();b=e-timedelta(days=days)
        r=s.getCandleData({"exchange":exchange,"symboltoken":str(tok),"interval":interval,
          "fromdate":b.strftime("%Y-%m-%d %H:%M"),"todate":e.strftime("%Y-%m-%d %H:%M")})
        d=(r or {}).get("data")
        if not d:return None
        x=pd.DataFrame(d,columns=["timestamp","open","high","low","close","volume"])
        x["timestamp"]=pd.to_datetime(x.timestamp,errors="coerce")
        for c in ["open","high","low","close","volume"]:x[c]=pd.to_numeric(x[c],errors="coerce")
        return x.dropna(subset=["timestamp","close"]).sort_values("timestamp").reset_index(drop=True)
    except:return None

def rsi(s,n=14):
    d=s.diff();u=d.clip(lower=0).ewm(alpha=1/n,adjust=False).mean();v=(-d.clip(upper=0)).ewm(alpha=1/n,adjust=False).mean()
    return 100-100/(1+u/v.replace(0,np.nan))

def feat(x):
    x=x.copy()
    x["ema9"]=x.close.ewm(span=9,adjust=False).mean();x["ema20"]=x.close.ewm(span=20,adjust=False).mean();x["ema50"]=x.close.ewm(span=50,adjust=False).mean()
    x["rsi"]=rsi(x.close)
    tr=pd.concat([x.high-x.low,(x.high-x.close.shift()).abs(),(x.low-x.close.shift()).abs()],axis=1).max(axis=1)
    x["atr"]=tr.ewm(span=14,adjust=False).mean();x["vavg20"]=x.volume.rolling(20).mean();x["volx"]=x.volume/x.vavg20.replace(0,np.nan)
    x["prev20h"]=x.high.shift(1).rolling(20).max();x["prev20l"]=x.low.shift(1).rolling(20).min()
    tp=(x.high+x.low+x.close)/3;day=x.timestamp.dt.date
    x["pv"]=tp*x.volume;x["cv"]=x.volume.groupby(day).cumsum();x["cpv"]=x.pv.groupby(day).cumsum();x["vwap"]=x.cpv/x.cv.replace(0,np.nan)
    x["ema20slope"]=x.ema20-x.ema20.shift(3)
    return x

def market_bias(s,em):
    idx={"NIFTY":"99926000","SENSEX":"99919000"}
    vals=[]
    for name,tok,ex in [("NIFTY","99926000","NSE"),("SENSEX","99919000","BSE")]:
        d=candles(s,tok,10,"FIFTEEN_MINUTE",ex)
        if d is None or len(d)<30:continue
        d=feat(d);x=d.iloc[-2]
        bull=x.close>x.ema20 and x.ema20>x.ema50 and x.ema20slope>0 and x.rsi>=50
        bear=x.close<x.ema20 and x.ema20<x.ema50 and x.ema20slope<0 and x.rsi<=50
        vals.append(1 if bull else -1 if bear else 0)
    if not vals:return "NEUTRAL",0
    z=sum(vals)
    return ("BULLISH" if z>0 else "BEARISH" if z<0 else "NEUTRAL"),z

def sector_name(sym):
    b=sym.replace("-EQ","").upper()
    groups={
      "BANKING":set("HDFCBANK ICICIBANK SBIN AXISBANK KOTAKBANK INDUSINDBK BANKBARODA PNB FEDERALBNK CANBK".split()),
      "IT":set("TCS INFY HCLTECH WIPRO TECHM LTIM PERSISTENT COFORGE".split()),
      "AUTO":set("MARUTI TATAMOTORS M&M BAJAJ-AUTO EICHERMOT HEROMOTOCO TVSMOTOR ASHOKLEY".split()),
      "PHARMA":set("SUNPHARMA DRREDDY CIPLA DIVISLAB AUROPHARMA LUPIN APOLLOHOSP".split()),
      "METALS":set("TATASTEEL JSWSTEEL HINDALCO SAIL JINDALSTEL".split()),
      "ENERGY":set("RELIANCE ONGC NTPC POWERGRID COALINDIA ADANIGREEN ADANIPOWER".split()),
    }
    for k,v in groups.items():
        if b in v:return k
    return "OTHER"

def sector_strength(res):
    d={}
    for z in res:
        k=z["sector"];d.setdefault(k,[]).append(z["tech"])
    return {k:float(np.mean(v)) for k,v in d.items()}

def analyze(sym,tok,s,mbias):
    # IPO name block
    clean = sym.replace("-EQ","").upper()
    if clean in IPO_BLOCK:
        return None

    # IPO history filter - at least 120 daily candles required
    d_daily = candles(s,tok,250,"ONE_DAY","NSE")
    if d_daily is None or len(d_daily) < 120:
        return None

    d5=candles(s,tok,12,"FIVE_MINUTE");time.sleep(DELAY);d15=candles(s,tok,25,"FIFTEEN_MINUTE")
    if d5 is None or d15 is None or len(d5)<60 or len(d15)<60:return None
    d5=feat(d5);d15=feat(d15);a=d5.iloc[-2];b=d15.iloc[-2]
    keys=["close","atr","rsi","volx","vwap","prev20h","prev20l"]
    if any(pd.isna(a[k]) for k in keys):return None
    entry=float(a.close);atr=max(float(a.atr),entry*.003)
    bull15=b.close>b.ema20>b.ema50 and b.ema20slope>0
    bear15=b.close<b.ema20<b.ema50 and b.ema20slope<0
    bull5=a.close>a.ema9>a.ema20 and a.ema20slope>0
    bear5=a.close<a.ema9<a.ema20 and a.ema20slope<0
    vol=float(a.volx);above_vwap=a.close>a.vwap
    below_vwap=a.close<a.vwap
    breakout_buy=a.close>a.prev20h and vol>=1.5
    breakout_sell=a.close<a.prev20l and vol>=1.5
    buy=sum([25 if bull15 else 0,20 if bull5 else 0,15 if above_vwap else 0,15 if 55<=a.rsi<=75 else 0,15 if vol>=1.5 else 0,10 if breakout_buy else 0])
    sell=sum([25 if bear15 else 0,20 if bear5 else 0,15 if below_vwap else 0,15 if 25<=a.rsi<=45 else 0,15 if vol>=1.5 else 0,10 if breakout_sell else 0])
    direction="BUY" if buy>sell else "SELL";tech=max(buy,sell)
    if tech<60 or vol<MIN_VOLX:return None
    # Skip OTHER sector low quality to avoid random smallcaps
    sec = sector_name(sym)
    if sec == "OTHER" and tech < 85:
        return None
    if mbias=="BULLISH" and direction=="SELL":market_pts=-8
    elif mbias=="BEARISH" and direction=="BUY":market_pts=-8
    elif mbias=="NEUTRAL":market_pts=0
    else:market_pts=8
    if direction=="BUY":
        sw=float(d5.low.iloc[-8:-2].min());sl=min(entry-atr,sw-0.15*atr)
        if sl>=entry:sl=entry-atr
    else:
        sw=float(d5.high.iloc[-8:-2].max());sl=max(entry+atr,sw+0.15*atr)
        if sl<=entry:sl=entry+atr
    risk=abs(entry-sl)
    if risk/entry<MIN_SL_PCT:return None
    t1=entry+(1.5*risk if direction=="BUY" else -1.5*risk)
    t2=entry+(2*risk if direction=="BUY" else -2*risk)
    t3=entry+(3*risk if direction=="BUY" else -3*risk)
    setup="BREAKOUT" if (breakout_buy if direction=="BUY" else breakout_sell) else ("PULLBACK/VWAP" if (above_vwap if direction=="BUY" else below_vwap) else "TREND")
    return {"symbol":sym,"direction":direction,"tech":tech,"market_pts":market_pts,"entry":entry,"sl":sl,"t1":t1,"t2":t2,"t3":t3,"rsi":float(a.rsi),"volx":vol,"setup":setup,"sector":sec,"live_ltp":entry}

def option_flow(s,om,z,us):
    sym=z["symbol"].replace("-EQ","").upper();spot=z["live_ltp"];u=best_underlying(sym,us)
    x=om[om.symbol.str.upper().str.startswith(u,na=False)].copy()
    if x.empty:return 0,"NEUTRAL",0,u
    today=pd.Timestamp.now().normalize()
    x=x[(x.expiry_dt>=today)&(x.expiry_dt<=today+pd.Timedelta(days=45))].copy()
    if x.empty:return 0,"NEUTRAL",0,u
    exp=sorted(x.expiry_dt.dropna().unique())[:1]
    x=x[x.expiry_dt.isin(exp)]
    strikes=sorted(x.strike_num.dropna().unique())
    if not strikes:return 0,"NEUTRAL",0,u
    atm=min(strikes,key=lambda q:abs(float(q)-spot));gap=min([abs(q-atm) for q in strikes if q!=atm] or [1])
    allowed=[q for q in strikes if abs(q-atm)<=gap*OPTION_STRIKES]
    x=x[x.strike_num.isin(allowed)]
    ce=pe=0.;cm=[];pm=[];used=0
    for _,c in x.iterrows():
        if used>=OPTION_MAX:break
        d=candles(s,c.token,OPTION_DAYS,"ONE_DAY","NFO");time.sleep(OPTION_DELAY)
        if d is None or len(d)<3:continue
        used+=1;v=float(pd.to_numeric(d.volume,errors="coerce").fillna(0).sum());m=(float(d.close.iloc[-1])-float(d.close.iloc[0]))/max(float(d.close.iloc[0]),.01)
        if str(c.symbol).upper().endswith("CE"):ce+=v;cm.append(m)
        else:pe+=v;pm.append(m)
    if ce==0 and pe==0:return 0,"NEUTRAL",0,u
    ratio=ce/max(pe,1);ca=np.mean(cm) if cm else 0;pa=np.mean(pm) if pm else 0
    if ratio>=1.25 and ca>=pa:return 8,"BULLISH",ratio,u
    if ratio<=.80 and pa>=ca:return 8,"BEARISH",ratio,u
    return 0,"NEUTRAL",ratio,u

def stars(s):
    return "★★★★★" if s>=90 else "★★★★☆" if s>=80 else "★★★☆☆" if s>=70 else "★★☆☆☆"

def main():
    print(f"=== DIVINE INTRADAY V8 LIVE | {datetime.now(IST):%d %b %H:%M:%S IST} ===",flush=True)
    s=login();m=master();em=equity_master(m);om=option_master(m);us=all_underlyings(om)
    mbias,mval=market_bias(s,em)
    print(f"MARKET: {mbias} | NIFTY/SENSEX score {mval}",flush=True)
    q=quotes(s,em.token.tolist())
    if q.empty:tg("⚠️ V8 No live quotes");return
    q["symbolToken"]=q.symbolToken.astype(str);q["ltp"]=pd.to_numeric(q.ltp,errors="coerce");q["tradeVolume"]=pd.to_numeric(q.tradeVolume,errors="coerce")
    q=q.dropna(subset=["symbolToken","ltp","tradeVolume"])
    q=q[(q.ltp>=MIN_PRICE)&(q.tradeVolume>=MIN_VOL)].sort_values("tradeVolume",ascending=False).head(TOP_UNIVERSE)
    q=q.merge(em[["symbol","token"]].drop_duplicates("token"),left_on="symbolToken",right_on="token",how="left").dropna(subset=["symbol"])
    raw=[]
    for _,r in q.iterrows():
        z=analyze(r.symbol,r.symbolToken,s,mbias)
        if z:
            z["live_ltp"]=float(r.ltp)
            old_entry=z["entry"];delta=z["live_ltp"]-old_entry
            z["entry"]=z["live_ltp"]
            z["sl"]+=delta;z["t1"]+=delta;z["t2"]+=delta;z["t3"]+=delta
            raw.append(z)
    if not raw:
        out=f"⚡ DIVINE INTRADAY V8 LIVE\n{datetime.now(IST):%d-%b %H:%M IST}\nMarket: {mbias}\n\n⚠️ NO QUALIFYING SETUP"
        print(out);tg(out);return
    ss=sector_strength(raw)
    for z in raw:z["sector_pts"]=min(7,int(max(0,ss.get(z["sector"],0)-60)/4))
    raw.sort(key=lambda z:z["tech"]+z["market_pts"]+z["sector_pts"],reverse=True)
    shortlist=raw[:8]
    for z in shortlist:
        ob,obias,ratio,mapped=option_flow(s,om,z,us)
        aligned=(z["direction"]=="BUY" and obias=="BULLISH") or (z["direction"]=="SELL" and obias=="BEARISH")
        z["opt_bias"]=obias;z["opt_ratio"]=ratio;z["opt_bonus"]=ob if aligned else (-4 if obias!="NEUTRAL" else 0);z["mapped"]=mapped
        z["score"]=max(0,min(100,z["tech"]+z["market_pts"]+z["sector_pts"]+z["opt_bonus"]))
    shortlist.sort(key=lambda z:z["score"],reverse=True)
    sig=[z for z in shortlist if z["score"]>=MIN_SCORE][:TOP_SIGNALS]
    msg=[f"⚡ DIVINE INTRADAY V8 LIVE | {datetime.now(IST):%d-%b %H:%M IST}",f"Market: {mbias} | Liquid: {len(q)} | Analysed: {len(raw)}","🟢 LTP = LIVE Angel quote at scan time",""]
    if sig:
        msg.append("🔥 TOP 3 LIVE SETUPS")
        for i,z in enumerate(sig,1):
            msg.append(f'\n#{i} {z["symbol"]} {z["direction"]} {stars(z["score"])} {z["score"]}/100\nSetup: {z["setup"]} | Sector: {z["sector"]}\nLTP/Entry ₹{z["entry"]:.2f}\nSL ₹{z["sl"]:.2f} | T1 ₹{z["t1"]:.2f} | T2 ₹{z["t2"]:.2f} | T3 ₹{z["t3"]:.2f}\nTech {z["tech"]} | Market {z["market_pts"]:+d} | Sector {z["sector_pts"]:+d}\nRSI {z["rsi"]:.1f} | Vol {z["volx"]:.2f}x\nOptions {z["opt_bias"]} {z["opt_bonus"]:+d} | CE/PE {z["opt_ratio"]:.2f}')
    else:
        msg+=["⚠️ NO QUALIFYING LIVE SETUP","Watchlist:"]+[f'#{i} {z["symbol"]} {z["direction"]} {z["score"]} | LTP ₹{z["entry"]:.2f} | {z["setup"]}' for i,z in enumerate(shortlist[:5],1)]
    out="\n".join(msg);print(out,flush=True);tg(out)

if __name__=="__main__":
    try:main()
    except Exception as e:
        tg(f"❌ V8 ERROR\n{e}");raise
