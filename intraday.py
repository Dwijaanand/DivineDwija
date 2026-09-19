import os,time,requests,pyotp,pandas as pd,numpy as np
from datetime import datetime,timedelta
from SmartApi import SmartConnect
import pytz

API_KEY=os.getenv("API_KEY");CLIENT_ID=os.getenv("CLIENT_ID");PASSWORD=os.getenv("PASSWORD");TOTP_SECRET=os.getenv("TOTP_SECRET")
TELEGRAM_BOT_TOKEN=os.getenv("TELEGRAM_BOT_TOKEN");TELEGRAM_CHAT_ID=os.getenv("TELEGRAM_CHAT_ID")
MASTER_URL="https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
MIN_PRICE=100;MIN_VOL=200000;TOP_UNIVERSE=60;TOP_SIGNALS=3;MIN_SCORE=70;WATCH_SCORE=50;DELAY=.30;MIN_SL_PCT=.004
WHOLE_BONUS=6;NR7_BONUS=7;OPTIONS_MAX=12;OPTIONS_DAYS=90;OPTIONS_STRIKES=2;OPTION_DELAY=.35
OPTION_CACHE={}
IST=pytz.timezone("Asia/Kolkata")
IPO_BLOCK={"PINELABS-EQ","MEESHO-EQ","LENSKART-EQ","GLASSWALL-EQ","URBANCO-EQ","GROWW-EQ","SKYWAYS-EQ","CUPID-EQ","LUMINO-EQ","PWL-EQ","RIR-EQ","SAMBHV-EQ","EMIL-EQ"}

def tg(x):
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        try: requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",data={"chat_id":TELEGRAM_CHAT_ID,"text":x},timeout=15)
        except: pass

def login():
    s=SmartConnect(api_key=API_KEY)
    r=s.generateSession(CLIENT_ID,PASSWORD,pyotp.TOTP(TOTP_SECRET).now())
    return s

def master():
    x=pd.DataFrame(requests.get(MASTER_URL,timeout=30).json())
    x["token"]=x["token"].astype(str);x["symbol"]=x["symbol"].astype(str);return x

def equity_master(m): return m[(m["exch_seg"]=="NSE")&m["symbol"].str.endswith("-EQ")].copy()

def option_master(m):
    x=m[m["exch_seg"]=="NFO"].copy()
    x["expiry_dt"]=pd.to_datetime(x.get("expiry"),errors="coerce")
    x["strike_num"]=pd.to_numeric(x.get("strike"),errors="coerce")
    x=x[x["symbol"].str.upper().str.contains("CE|PE",regex=True,na=False)].copy()
    return x

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
    x=d.iloc[-1];op=float(x.open);hi=float(x.high);lo=float(x.low)
    if abs(op-round(op))>0.05:return None
    tol=max(op*.001,0.05)
    if abs(op-lo)<=tol:return "BUY"
    if abs(op-hi)<=tol:return "SELL"
    return None

def nr7_strategy(d):
    if d is None or len(d)<10:return None
    x=d.copy();x["range"]=x.high-x.low;cur=x.iloc[-2];prev=x.iloc[-9:-2]["range"]
    if len(prev)<7 or cur["range"]>prev.min():return None
    if cur.close>cur.open:return "BUY"
    if cur.close<cur.open:return "SELL"
    return None

def normalize_underlying(sym): return str(sym).replace("-EQ","").upper()

def find_option_contracts(om,sym,spot):
    if om.empty or not spot:return pd.DataFrame()
    u=normalize_underlying(sym);x=om[om["symbol"].str.upper().str.startswith(u)].copy()
    if x.empty:return x
    today=pd.Timestamp.now().normalize()
    # NEXT 3 MONTH = today se 90 din tak ke expiry
    x=x[(x["expiry_dt"]>=today)&(x["expiry_dt"]<=today+pd.Timedelta(days=90))].copy()
    if x.empty:return x
    exps=sorted(x["expiry_dt"].dropna().unique())[:3] # next 3 expiry only
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
    if contracts.empty:
        return {"score":0,"bias":"NEUTRAL","ce_volume":0,"pe_volume":0,"ratio":0,"contracts":0}
    ce_vol=0.;pe_vol=0.;ce_move=[];pe_move=[];used=0
    for _,c in contracts.iterrows():
        if used>=OPTIONS_MAX:break
        d=candles(s,c["token"],20,"ONE_DAY","NFO");time.sleep(OPTION_DELAY)
        if d is None or len(d)<10:continue
        used+=1;vol=float(pd.to_numeric(d.volume,errors="coerce").fillna(0).sum());first=float(d.iloc[0].close);last=float(d.iloc[-1].close);move=(last-first)/first if first>0 else 0
        if str(c["symbol"]).upper().endswith("CE"):ce_vol+=vol;ce_move.append(move)
        elif str(c["symbol"]).upper().endswith("PE"):pe_vol+=vol;pe_move.append(move)
    if ce_vol<=0 and pe_vol<=0:
        result={"score":0,"bias":"NEUTRAL","ce_volume":0,"pe_volume":0,"ratio":0,"contracts":used};OPTION_CACHE[key]=result;return result
    ratio=ce_vol/(pe_vol if pe_vol else 1);ce_avg=np.mean(ce_move) if ce_move else 0;pe_avg=np.mean(pe_move) if pe_move else 0
    if ratio>=1.35 and ce_avg>=pe_avg:bias="BULLISH";score=OPTIONS_MAX
    elif ratio<=.75 and pe_avg>=ce_avg:bias="BEARISH";score=OPTIONS_MAX
    elif ratio>=1.15:bias="BULLISH";score=7
    elif ratio<=.90:bias="BEARISH";score=7
    else:bias="NEUTRAL";score=0
    result={"score":int(score),"bias":bias,"ce_volume":ce_vol,"pe_volume":pe_vol,"ratio":ratio,"contracts":used};OPTION_CACHE[key]=result;return result

def analyze_tech(sym,tok,s):
    d5=candles(s,tok,10,"FIVE_MINUTE");time.sleep(DELAY);d15=candles(s,tok,20,"FIFTEEN_MINUTE")
    if d5 is None or d15 is None or len(d5)<60 or len(d15)<60:return None,"candle"
    d5=feat(d5);d15=feat(d15);a=d5.iloc[-2];b=d15.iloc[-2]
    if any(pd.isna(a[k]) for k in ["close","atr","rsi","volx","vwap"]):return None,"feature"
    buy=(25 if b.close>b.ema20>b.ema50 else 0)+(20 if a.close>a.ema9>a.ema20 else 0)+(15 if a.close>a.vwap else 0)+(15 if 55<=a.rsi<=75 else 0)+(15 if a.volx>=1.5 else 0)+(10 if a.close>a.prev20h else 0)
    sell=(25 if b.close<b.ema20<b.ema50 else 0)+(20 if a.close<a.ema9<a.ema20 else 0)+(15 if a.close<a.vwap else 0)+(15 if 25<=a.rsi<=45 else 0)+(15 if a.volx>=1.5 else 0)+(10 if a.close<a.prev20l else 0)
    direction="BUY" if buy>=sell else "SELL";tech_score=max(buy,sell);entry=float(a.close);atr=max(float(a.atr),entry*.003)
    if atr/entry<MIN_SL_PCT:return None,"sl_small"
    dd=candles(s,tok,45,"ONE_DAY")
    if dd is None or len(dd)<15:return None,"daily"
    whole=whole_number_strategy(dd);nr7=nr7_strategy(dd);whole_bonus=WHOLE_BONUS if whole==direction else 0;nr7_bonus=NR7_BONUS if nr7==direction else 0
    sl=entry-atr if direction=="BUY" else entry+atr
    return {"symbol":sym,"direction":direction,"tech_score":int(tech_score),"whole":whole or "-","nr7":nr7 or "-","whole_bonus":whole_bonus,"nr7_bonus":nr7_bonus,"entry":entry,"sl":sl,"t1":entry+1.5*atr if direction=="BUY" else entry-1.5*atr,"t2":entry+2.5*atr if direction=="BUY" else entry-2.5*atr,"rsi":float(a.rsi),"volx":float(a.volx),"token":str(tok)},None

def stars(s):
    if s>=90:return "★★★★★"
    if s>=80:return "★★★★☆"
    if s>=70:return "★★★☆☆"
    if s>=60:return "★★☆☆☆"
    return "★☆☆☆☆"

def main():
    print(f"=== AI INTRADAY V7.1 FAST {datetime.now(IST):%d %b %H:%M IST} ===");s=login();m=master();em=equity_master(m);om=option_master(m)
    print(f"NSE-EQ: {len(em)} | NFO: {len(om)}")
    q=quotes(s,em.token.tolist())
    if q.empty:tg("⚠️ V7.1 No quotes");return
    q["symbolToken"]=q["symbolToken"].astype(str);q["ltp"]=pd.to_numeric(q["ltp"],errors="coerce");q["tradeVolume"]=pd.to_numeric(q["tradeVolume"],errors="coerce")
    q=q.dropna(subset=["symbolToken","ltp","tradeVolume"]);q=q[(q.ltp>=MIN_PRICE)&(q.tradeVolume>=MIN_VOL)].sort_values("tradeVolume",ascending=False).head(TOP_UNIVERSE)
    q=q.merge(em[["symbol","token"]].drop_duplicates("token"),left_on="symbolToken",right_on="token",how="left").dropna(subset=["symbol"])
    stat={"candle":0,"feature":0,"daily":0,"ok":0,"ipo_skip":0};res=[]
    for _,r in q.iterrows():
        if r.symbol in IPO_BLOCK:stat["ipo_skip"]+=1;continue
        z,why=analyze_tech(r.symbol,r.symbolToken,s)
        if not z:
            if why in stat:stat[why]+=1
            continue
        stat["ok"]+=1;res.append(z)
        print(f'{z["symbol"]:<18} {z["direction"]:<4} Tech {z["tech_score"]:>3} RSI {z["rsi"]:.1f}')

    res.sort(key=lambda x:x["tech_score"],reverse=True)
    top_for_option=res[:12] # NEXT 3 MONTH OPTION SIRF TOP 12 PE
    print(f"\n--- Checking Next 3 Month Option Flow for Top {len(top_for_option)} ---")
    final_res=[]
    for z in top_for_option:
        opt=option_flow(s,om,z["symbol"],z["entry"])
        opt_bonus=opt["score"] if ((z["direction"]=="BUY" and opt["bias"]=="BULLISH") or (z["direction"]=="SELL" and opt["bias"]=="BEARISH")) else (-min(6,opt["score"]//2) if opt["bias"]!="NEUTRAL" else 0)
        z["score"]=int(min(100,max(0,z["tech_score"]+z["whole_bonus"]+z["nr7_bonus"]+opt_bonus)))
        z["options_bias"]=opt["bias"];z["options_score"]=opt_bonus;z["options_ratio"]=float(opt["ratio"]);z["options_contracts"]=opt["contracts"]
        final_res.append(z)
        print(f'{z["symbol"]} -> Opt {opt["bias"]} ratio {opt["ratio"]:.2f} -> Final {z["score"]}')

    # baaki bache hue bina option ke
    for z in res[12:]:
        z["score"]=z["tech_score"]+z["whole_bonus"]+z["nr7_bonus"];z["options_bias"]="SKIP";z["options_score"]=0;z["options_ratio"]=0;z["options_contracts"]=0
        final_res.append(z)

    final_res.sort(key=lambda x:x["score"],reverse=True)
    sig=[x for x in final_res if x["score"]>=MIN_SCORE][:TOP_SIGNALS]

    msg=[f"⚡ AI INTRADAY V7.1 FAST | {datetime.now(IST):%d-%b %H:%M IST}",f"NSE-EQ: {len(em)} | Liquid: {len(q)} | Analysed: {stat['ok']} | IPO Skip: {stat['ipo_skip']}","","🧠 Stack: 5m/15m + Whole + NR7 + 3M Option(Top12)"]
    if sig:
        msg+=["","🔥 QUALIFYING SETUPS (3M Option Confirmed)"]
        for i,z in enumerate(sig,1):
            msg.append(f'\n#{i} {z["symbol"]} {z["direction"]} {stars(z["score"])} ({z["score"]})\nEntry ₹{z["entry"]:.2f} | SL ₹{z["sl"]:.2f} | T1 ₹{z["t1"]:.2f}\nTech {z["tech_score"]} | RSI {z["rsi"]:.1f} | Vol {z["volx"]:.2f}x\nWhole {z["whole"]} +{z["whole_bonus"]} | NR7 {z["nr7"]} +{z["nr7_bonus"]}\nOptions: {z["options_bias"]} +{z["options_score"]} | CE/PE {z["options_ratio"]:.2f} ({z["options_contracts"]} contracts)')
    else:
        msg+=["","⚠️ NO QUALIFYING SETUP"]
        if final_res:
            msg+=["","👀 WATCHLIST"]+[f'#{i} {z["symbol"]} {z["direction"]} {z["score"]} | Opt {z["options_bias"]}' for i,z in enumerate(final_res[:8],1)]
    msg+=["",f'Diag: candle {stat["candle"]} | feature {stat["feature"]} | daily {stat["daily"]}']
    out="\n".join(msg);print("\n"+out);tg(out)

if __name__=="__main__":
    try:main()
    except Exception as e:tg(f"❌ V7.1 ERROR\n{e}");raise
