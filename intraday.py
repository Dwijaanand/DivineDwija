import os,time,requests,pyotp,pandas as pd,numpy as np,re,difflib
from datetime import datetime,timedelta
from SmartApi import SmartConnect
import pytz, logging
logging.getLogger("smartapi.smartConnect").setLevel(logging.ERROR)

API_KEY=os.getenv("API_KEY");CLIENT_ID=os.getenv("CLIENT_ID");PASSWORD=os.getenv("PASSWORD");TOTP_SECRET=os.getenv("TOTP_SECRET")
TELEGRAM_BOT_TOKEN=os.getenv("TELEGRAM_BOT_TOKEN");TELEGRAM_CHAT_ID=os.getenv("TELEGRAM_CHAT_ID")
MASTER_URL="https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
MIN_PRICE=100;MIN_VOL=200000;TOP_UNIVERSE=60;TOP_SIGNALS=3;MIN_SCORE=70;MIN_VOLX=0.5;DELAY=.30;MIN_SL_PCT=.004
WHOLE_BONUS=6;NR7_BONUS=7;OPTIONS_MAX=10;OPTIONS_DAYS=20;OPTIONS_STRIKES=2;OPTION_DELAY=.30
OPTION_CACHE={}; ALL_UNDERLYINGS=None
IST=pytz.timezone("Asia/Kolkata")
IPO_BLOCK={"PINELABS-EQ","MEESHO-EQ","LENSKART-EQ","GLASSWALL-EQ","URBANCO-EQ","GROWW-EQ","SKYWAYS-EQ","CUPID-EQ","LUMINO-EQ","PWL-EQ","RIR-EQ","SAMBHV-EQ","EMIL-EQ"}

def tg(x):
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        try: requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",data={"chat_id":TELEGRAM_CHAT_ID,"text":x},timeout=15)
        except: pass

def login():
    s=SmartConnect(api_key=API_KEY)
    s.generateSession(CLIENT_ID,PASSWORD,pyotp.TOTP(TOTP_SECRET).now())
    return s

def master():
    x=pd.DataFrame(requests.get(MASTER_URL,timeout=30).json())
    x["token"]=x["token"].astype(str);x["symbol"]=x["symbol"].astype(str);return x

def equity_master(m): return m[(m["exch_seg"]=="NSE")&m["symbol"].str.endswith("-EQ")].copy()

def option_master(m):
    x=m[m["exch_seg"]=="NFO"].copy()
    x["expiry_dt"]=pd.to_datetime(x.get("expiry"), errors="coerce", format="mixed")
    x["strike_num"]=pd.to_numeric(x.get("strike"),errors="coerce")
    x=x[x["symbol"].str.upper().str.contains("CE|PE",regex=True,na=False)].copy()
    return x

def get_all_underlyings(om):
    underlyings=set()
    for s in om["symbol"].astype(str):
        m=re.match(r'^([A-Z&\-\_]+)', s.upper())
        if m:
            u=m.group(1).replace("-EQ","")
            if len(u)>=3: underlyings.add(u)
    return list(underlyings)

def find_best_underlying(equity_base):
    global ALL_UNDERLYINGS
    if not ALL_UNDERLYINGS: return equity_base
    for u in ALL_UNDERLYINGS:
        if u.startswith(equity_base) or equity_base.startswith(u): return u
    match=difflib.get_close_matches(equity_base, ALL_UNDERLYINGS, n=1, cutoff=0.6)
    return match[0] if match else equity_base

def quotes(s,tokens):
    out=[]
    for i in range(0,len(tokens),50):
        try:
            r=s.getMarketData("FULL",{"NSE":[str(x) for x in tokens[i:i+50]]});d=(r or {}).get("data",{})
            out+=(d.get("fetched",[]) if isinstance(d,dict) else [])
        except: pass
        time.sleep(1.05)
    return pd.DataFrame(out)

def candles(s,tok,days,interval,exchange="NSE"):
    try:
        e=datetime.now();b=e-timedelta(days=days)
        r=s.getCandleData({"exchange":exchange,"symboltoken":str(tok),"interval":interval,"fromdate":b.strftime("%Y-%m-%d %H:%M"),"todate":e.strftime("%Y-%m-%d %H:%M")})
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
    x=x.copy();x["ema9"]=x.close.ewm(span=9,adjust=False).mean();x["ema20"]=x.close.ewm(span=20,adjust=False).mean();x["ema50"]=x.close.ewm(span=50,adjust=False).mean();x["rsi"]=rsi(x.close)
    tr=pd.concat([x.high-x.low,(x.high-x.close.shift()).abs(),(x.low-x.close.shift()).abs()],axis=1).max(axis=1);x["atr"]=tr.ewm(span=14,adjust=False).mean();x["vavg20"]=x.volume.rolling(20).mean();x["volx"]=x.volume/x.vavg20.replace(0,np.nan)
    x["prev20h"]=x.high.shift(1).rolling(20).max();x["prev20l"]=x.low.shift(1).rolling(20).min()
    tp=(x.high+x.low+x.close)/3;day=x.timestamp.dt.date;x["pv"]=tp*x.volume;x["cv"]=x.volume.groupby(day).cumsum();x["cpv"]=x.pv.groupby(day).cumsum();x["vwap"]=x.cpv/x.cv.replace(0,np.nan)
    return x

def whole_number_strategy(d):
    if d is None or len(d)<2:return None
    x=d.iloc[-1];op=float(x.open)
    if abs(op-round(op))>0.05:return None
    tol=max(op*.001,0.05)
    if abs(op-float(x.low))<=tol:return "BUY"
    if abs(op-float(x.high))<=tol:return "SELL"
    return None

def nr7_strategy(d):
    if d is None or len(d)<10:return None
    x=d.copy();x["range"]=x.high-x.low;cur=x.iloc[-2];prev=x.iloc[-9:-2]["range"]
    if len(prev)<7 or cur["range"]>prev.min():return None
    if cur.close>cur.open:return "BUY"
    if cur.close<cur.open:return "SELL"
    return None

def find_option_contracts(om,sym,spot):
    global ALL_UNDERLYINGS
    if om.empty or not spot:return pd.DataFrame()
    if ALL_UNDERLYINGS is None: ALL_UNDERLYINGS=get_all_underlyings(om)
    base=str(sym).replace("-EQ","").upper()
    best_u=find_best_underlying(base)
    x=om[om["symbol"].str.upper().str.startswith(best_u, na=False)].copy()
    if x.empty: x=om[om["symbol"].str.upper().str.contains(base[:4], na=False)].copy()
    if x.empty:return x
    today=pd.Timestamp.now().normalize()
    x=x[(x["expiry_dt"]>=today)&(x["expiry_dt"]<=today+pd.Timedelta(days=90))].copy()
    if x.empty:return x
    exps=sorted(x["expiry_dt"].dropna().unique())[:3]
    x=x[x["expiry_dt"].isin(exps)].copy()
    selected=[]
    for exp in exps:
        e=x[x["expiry_dt"]==exp].copy()
        strikes=sorted(e["strike_num"].dropna().unique())
        if not strikes:continue
        atm=min(strikes,key=lambda z:abs(float(z)-float(spot)))
        gap=min([abs(float(z)-float(atm)) for z in strikes if z!=atm] or [1])
        allowed=[z for z in strikes if abs(float(z)-float(atm))<=gap*OPTIONS_STRIKES]
        selected.append(e[e["strike_num"].isin(allowed)])
    return pd.concat(selected,ignore_index=True) if selected else pd.DataFrame()

def option_flow(s,om,sym,spot):
    key=f"{sym}_{round(float(spot),2)}"
    if key in OPTION_CACHE:return OPTION_CACHE[key]
    contracts=find_option_contracts(om,sym,spot)
    if contracts.empty: return {"score":0,"bias":"NEUTRAL","ratio":0,"contracts":0}
    ce_vol=0.;pe_vol=0.;ce_move=[];pe_move=[];used=0
    for _,c in contracts.iterrows():
        if used>=OPTIONS_MAX:break
        d=candles(s,c["token"],OPTIONS_DAYS,"ONE_DAY","NFO");time.sleep(OPTION_DELAY)
        if d is None or len(d)<10:continue
        used+=1;vol=float(pd.to_numeric(d.volume,errors="coerce").fillna(0).sum())
        first=float(d.iloc[0].close);last=float(d.iloc[-1].close);move=(last-first)/first if first>0 else 0
        if str(c["symbol"]).upper().endswith("CE"):ce_vol+=vol;ce_move.append(move)
        else:pe_vol+=vol;pe_move.append(move)
    if ce_vol<=0 and pe_vol<=0:
        res={"score":0,"bias":"NEUTRAL","ratio":0,"contracts":used};OPTION_CACHE[key]=res;return res
    ratio=ce_vol/(pe_vol if pe_vol else 1);ce_avg=np.mean(ce_move) if ce_move else 0;pe_avg=np.mean(pe_move) if pe_move else 0
    if ratio>=1.35 and ce_avg>=pe_avg:bias="BULLISH";score=OPTIONS_MAX
    elif ratio<=.75 and pe_avg>=ce_avg:bias="BEARISH";score=OPTIONS_MAX
    elif ratio>=1.15:bias="BULLISH";score=7
    elif ratio<=.90:bias="BEARISH";score=7
    else:bias="NEUTRAL";score=0
    res={"score":int(score),"bias":bias,"ratio":ratio,"contracts":used};OPTION_CACHE[key]=res;return res

def analyze_tech(sym,tok,s):
    d5=candles(s,tok,10,"FIVE_MINUTE");time.sleep(DELAY);d15=candles(s,tok,20,"FIFTEEN_MINUTE")
    if d5 is None or d15 is None or len(d5)<60 or len(d15)<60:return None,"candle"
    d5=feat(d5);d15=feat(d15);a=d5.iloc[-2];b=d15.iloc[-2]
    if any(pd.isna(a[k]) for k in ["close","atr","rsi","volx","vwap"]):return None,"feature"
    if float(a.volx) < MIN_VOLX: return None,"vol_low"
    buy=(25 if b.close>b.ema20>b.ema50 else 0)+(20 if a.close>a.ema9>a.ema20 else 0)+(15 if a.close>a.vwap else 0)+(15 if 55<=a.rsi<=75 else 0)+(15 if a.volx>=1.5 else 0)+(10 if a.close>a.prev20h else 0)
    sell=(25 if b.close<b.ema20<b.ema50 else 0)+(20 if a.close<a.ema9<a.ema20 else 0)+(15 if a.close<a.vwap else 0)+(15 if 25<=a.rsi<=45 else 0)+(15 if a.volx>=1.5 else 0)+(10 if a.close<a.prev20l else 0)
    direction="BUY" if buy>=sell else "SELL";tech_score=max(buy,sell);entry=float(a.close);atr=max(float(a.atr),entry*.003)
    if atr/entry<MIN_SL_PCT:return None,"sl_small"
    dd=candles(s,tok,45,"ONE_DAY")
    if dd is None or len(dd)<15:return None,"daily"
    whole=whole_number_strategy(dd);nr7=nr7_strategy(dd)
    sl=entry-atr if direction=="BUY" else entry+atr
    return {"symbol":sym,"direction":direction,"tech_score":int(tech_score),"whole":whole or "-","nr7":nr7 or "-","whole_bonus":WHOLE_BONUS if whole==direction else 0,"nr7_bonus":NR7_BONUS if nr7==direction else 0,"entry":entry,"sl":sl,"t1":entry+1.5*atr if direction=="BUY" else entry-1.5*atr,"rsi":float(a.rsi),"volx":float(a.volx),"token":str(tok)},None

def stars(s):
    if s>=90:return "★★★★★"
    if s>=80:return "★★★★☆"
    if s>=70:return "★★★☆☆"
    if s>=60:return "★★☆☆☆"
    return "★☆☆☆☆"

def main():
    print(f"=== AI INTRADAY V7.4 AUTO {datetime.now(IST):%d %b %H:%M IST} ===", flush=True)
    s=login();m=master();em=equity_master(m);om=option_master(m)
    global ALL_UNDERLYINGS; ALL_UNDERLYINGS=get_all_underlyings(om)
    print(f"NSE-EQ:{len(em)} NFO:{len(om)} Underlyings:{len(ALL_UNDERLYINGS)}", flush=True)
    q=quotes(s,em.token.tolist())
    if q.empty:tg("⚠️ V7.4 No quotes");return
    q["symbolToken"]=q["symbolToken"].astype(str);q["ltp"]=pd.to_numeric(q["ltp"],errors="coerce");q["tradeVolume"]=pd.to_numeric(q["tradeVolume"],errors="coerce")
    q=q.dropna(subset=["symbolToken","ltp","tradeVolume"]);q=q[(q.ltp>=MIN_PRICE)&(q.tradeVolume>=MIN_VOL)].sort_values("tradeVolume",ascending=False).head(TOP_UNIVERSE)
    q=q.merge(em[["symbol","token"]].drop_duplicates("token"),left_on="symbolToken",right_on="token",how="left").dropna(subset=["symbol"])
    stat={"candle":0,"feature":0,"daily":0,"ok":0,"ipo_skip":0,"vol_low":0,"sl_small":0};res=[]
    for _,r in q.iterrows():
        if r.symbol in IPO_BLOCK:stat["ipo_skip"]+=1;continue
        z,why=analyze_tech(r.symbol,r.symbolToken,s)
        if not z:
            if why in stat:stat[why]+=1
            continue
        stat["ok"]+=1;res.append(z)
        print(f'{z["symbol"]:<18} {z["direction"]:<4} Tech {z["tech_score"]:>3} VolX {z["volx"]:.2f}', flush=True)
    res.sort(key=lambda x:x["tech_score"],reverse=True)
    top=res[:12]
    print(f"\n--- 3M Option Auto-Match Top {len(top)} ---", flush=True)
    final=[]
    for z in top:
        opt=option_flow(s,om,z["symbol"],z["entry"])
        bonus=opt["score"] if ((z["direction"]=="BUY" and opt["bias"]=="BULLISH") or (z["direction"]=="SELL" and opt["bias"]=="BEARISH")) else (-min(6,opt["score"]//2) if opt["bias"]!="NEUTRAL" else 0)
        z["score"]=int(min(100,max(0,z["tech_score"]+z["whole_bonus"]+z["nr7_bonus"]+bonus)))
        z["opt_bias"]=opt["bias"];z["opt_bonus"]=bonus;z["opt_ratio"]=float(opt["ratio"]);z["opt_c"]=opt["contracts"]
        final.append(z)
        print(f'{z["symbol"]} -> {find_best_underlying(z["symbol"].replace("-EQ",""))} {opt["bias"]} {opt["ratio"]:.2f} -> {z["score"]}', flush=True)
    for z in res[12:]:
        z["score"]=z["tech_score"]+z["whole_bonus"]+z["nr7_bonus"];z["opt_bias"]="SKIP";z["opt_bonus"]=0;z["opt_ratio"]=0;z["opt_c"]=0;final.append(z)
    final.sort(key=lambda x:x["score"],reverse=True)
    sig=[x for x in final if x["score"]>=MIN_SCORE][:TOP_SIGNALS]
    msg=[f"⚡ AI INTRADAY V7.4 AUTO | {datetime.now(IST):%d-%b %H:%M IST}",f"NSE:{len(em)} Liquid:{len(q)} Analysed:{stat['ok']} VolLow:{stat['vol_low']} IPO:{stat['ipo_skip']}","","🧠 5m/15m+Whole+NR7+3M Option Auto-Match"]
    if sig:
        msg+=["","🔥 QUALIFYING SETUPS"]
        for i,z in enumerate(sig,1):
            msg.append(f'\n#{i} {z["symbol"]} {z["direction"]} {stars(z["score"])} ({z["score"]})\nEntry ₹{z["entry"]:.2f} SL ₹{z["sl"]:.2f} T1 ₹{z["t1"]:.2f}\nTech {z["tech_score"]} RSI {z["rsi"]:.1f} Vol {z["volx"]:.2f}x | Whole {z["whole"]} NR7 {z["nr7"]}\nOpt {z["opt_bias"]} +{z["opt_bonus"]} CE/PE {z["opt_ratio"]:.2f} ({z["opt_c"]}c)')
    else:
        msg+=["","⚠️ NO SETUP", "👀 WATCHLIST"]+[f'#{i} {z["symbol"]} {z["direction"]} {z["score"]} VolX {z["volx"]:.2f} Opt {z["opt_bias"]}' for i,z in enumerate(final[:8],1)]
    msg+=["",f'Diag: candle {stat["candle"]} vol_low {stat["vol_low"]} daily {stat["daily"]}']
    out="\n".join(msg);print("\n"+out, flush=True);tg(out)

if __name__=="__main__":
    try:main()
    except Exception as e:tg(f"❌ V7.4 ERROR\n{e}");raise
