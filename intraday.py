import os, time, requests, pandas as pd, numpy as np, logging, pyotp, re
from datetime import datetime, timedelta
import pytz

# ========== AUTO TOKEN GENERATION (No DHAN_ACCESS_TOKEN needed) ==========
def auto_generate_token():
    client_id = os.getenv("DHAN_CLIENT_ID")
    password = os.getenv("DHAN_PASSWORD")
    totp_secret = os.getenv("DHAN_TOTP_SECRET")
    old_token = os.getenv("DHAN_ACCESS_TOKEN")

    if not client_id or not password:
        print("DHAN_CLIENT_ID / DHAN_PASSWORD not set, using fallback token")
        return old_token

    try:
        payload = {"clientId": client_id, "password": password}
        if totp_secret:
            totp = pyotp.TOTP(totp_secret)
            payload["totp"] = totp.now()
            print(f"Generated TOTP for {client_id}")

        r = requests.post("https://api.dhan.co/v2/login", json=payload, timeout=20)
        try:
            data = r.json()
        except:
            data = {}

        token = None
        if isinstance(data, dict):
            if isinstance(data.get("data"), dict):
                token = data.get("data", {}).get("jwtToken") or data.get("data", {}).get("accessToken")
            token = token or data.get("jwtToken") or data.get("accessToken")

        if token and len(token) > 100:
            print("✅ Auto Token Generated Successfully")
            return token
        else:
            print(f"Auto Login response: {str(data)[:300]} - using fallback token")
            return old_token
    except Exception as e:
        print(f"Auto token error: {e} - using fallback")
        return old_token

DHAN_CLIENT_ID=os.getenv("DHAN_CLIENT_ID")
DHAN_ACCESS_TOKEN=auto_generate_token()
TELEGRAM_BOT_TOKEN=os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID=os.getenv("TELEGRAM_CHAT_ID")

MIN_STOCK_PRICE=100;MIN_STOCK_VOL=100000;STOCK_UNIVERSE=35;TOP_SIGNALS=3;MIN_SCORE=80;MIN_VOLX=1.2;BREAKOUT_VOLX=1.5;MIN_SL_PCT=0.004;MACD_FAST=12;MACD_SLOW=26;MACD_SIGNAL=9
OPTION_ITM_STEPS=1;OPTION_EXPIRY_MAX_DAYS=14;OPTION_MIN_PRICE=5;OPTION_MAX_SPREAD_PCT=8.0;OPTION_CONFIRM_VOLX=1.2;OPTION_MONTHS=2;OPTION_STRIKE_RANGE=5;OPTION_FLOW_MIN_VOLX=1.2;DELAY=0.25;QUOTE_DELAY=1.0;IST=pytz.timezone("Asia/Kolkata");ETF_BLOCK=["BEES","ETF","LIQUID","GOLDBEES"];EXCLUDED={"LTIM"};BASE_URL="https://api.dhan.co/v2";MASTER_URL="https://images.dhan.co/api-data/api-scrip-master.csv"

logging.basicConfig(level=logging.ERROR)
def tg(msg):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(msg);return
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", data={"chat_id":TELEGRAM_CHAT_ID,"text":msg}, timeout=10)
    except Exception as e:
        print("Telegram error:",e)

def headers():
    return {"Content-Type":"application/json","Accept":"application/json","access-token":DHAN_ACCESS_TOKEN,"client-id":DHAN_CLIENT_ID}

def api_post(endpoint,payload,retries=3):
    for attempt in range(retries):
        try:
            r=requests.post(BASE_URL+endpoint, headers=headers(), json=payload, timeout=20)
            if r.status_code==200:
                return r.json()
            print("API ERROR",endpoint,r.status_code,r.text[:250])
            if r.status_code in (429,500,502,503,504):
                time.sleep(1.5*(attempt+1));continue
            return None
        except Exception as e:
            print("API EXCEPTION",endpoint,e);time.sleep(1.5*(attempt+1))
    return None

INSTRUMENTS=None
def load_instruments():
    global INSTRUMENTS
    if INSTRUMENTS is None:
        try:
            INSTRUMENTS=pd.read_csv(MASTER_URL,low_memory=False)
            print("Dhan master:",len(INSTRUMENTS))
        except Exception as e:
            print("Master error:",e);return pd.DataFrame()
    return INSTRUMENTS

def clean_symbol(x):return str(x).upper().replace("-EQ","").strip()
def blocked(sym):
    s=clean_symbol(sym)
    if s in EXCLUDED:return True
    return any(s.endswith(x) or x in s for x in ETF_BLOCK)

def get_quotes_eq(security_ids):
    out={};ids=[]
    for x in security_ids:
        try:ids.append(int(float(x)))
        except:pass
    ids=list(dict.fromkeys(ids))
    for start in range(0,len(ids),1000):
        batch=ids[start:start+1000]
        data=api_post("/marketfeed/quote",{"NSE_EQ":batch})
        if data and data.get("status")=="success":
            block=data.get("data",{}).get("NSE_EQ",{})
            for sid,v in block.items():
                try:out[str(sid)]={"last_price":float(v.get("last_price",0)),"volume":float(v.get("volume",0))}
                except:pass
        if start+1000<len(ids):time.sleep(QUOTE_DELAY)
    return out

def get_option_quotes(security_ids):
    out={};ids=[]
    for x in security_ids:
        try:ids.append(int(float(x)))
        except:pass
    ids=list(dict.fromkeys(ids))
    for start in range(0,len(ids),1000):
        batch=ids[start:start+1000]
        data=api_post("/marketfeed/quote",{"NSE_FNO":batch})
        if data and data.get("status")=="success":
            block=data.get("data",{}).get("NSE_FNO",{})
            for sid,v in block.items():
                try:out[str(sid)]={"last_price":float(v.get("last_price",0)),"volume":float(v.get("volume",0)),"oi":float(v.get("oi",0)),"oi_change":float(v.get("oi_change",0)),"buy_qty":float(v.get("buy_quantity",0)),"sell_qty":float(v.get("sell_quantity",0))}
                except:pass
        if start+1000<len(ids):time.sleep(QUOTE_DELAY)
    return out

def candles(security_id,days,interval):
    try:
        now=datetime.now(IST);frm=(now-timedelta(days=days)).strftime("%Y-%m-%d");to=now.strftime("%Y-%m-%d")
        if interval=="ONE_DAY":
            payload={"securityId":str(int(float(security_id))),"exchangeSegment":"NSE_EQ","instrument":"EQUITY","expiryCode":0,"oi":False,"fromDate":frm,"toDate":to}
            data=api_post("/charts/historical",payload)
        else:
            mins={"FIVE_MINUTE":5,"FIFTEEN_MINUTE":15}.get(interval,5)
            payload={"securityId":str(int(float(security_id))),"exchangeSegment":"NSE_EQ","instrument":"EQUITY","interval":str(mins),"oi":False,"fromDate":frm,"toDate":to}
            data=api_post("/charts/intraday",payload)
        if not data or data.get("status")=="failure":return None
        raw=data.get("data")
        if not raw:return None
        if isinstance(raw,dict) and all(k in raw for k in ["open","high","low","close","volume"]):
            df=pd.DataFrame({"open":raw["open"],"high":raw["high"],"low":raw["low"],"close":raw["close"],"volume":raw["volume"],"timestamp":raw.get("timestamp",raw.get("start_Time",[]))})
        elif isinstance(raw,(dict,list)):df=pd.DataFrame(raw)
        else:return None
        if df.empty:return None
        rename={}
        for c in df.columns:
            lc=str(c).lower()
            if lc in ("start_time","starttime","timestamp","time","datetime"):rename[c]="timestamp"
            elif lc in ("open","high","low","close","volume"):rename[c]=lc
        df.rename(columns=rename,inplace=True)
        if not all(c in df.columns for c in ["open","high","low","close","volume"]):return None
        if "timestamp" not in df.columns:df["timestamp"]=pd.RangeIndex(len(df))
        else:df["timestamp"]=pd.to_datetime(df["timestamp"],errors="coerce")
        for c in ["open","high","low","close","volume"]:df[c]=pd.to_numeric(df[c],errors="coerce")
        df=df.dropna(subset=["open","high","low","close","volume"])
        return df.sort_values("timestamp").reset_index(drop=True)
    except Exception as e:
        print("Candle error",security_id,interval,e);return None

def rsi(s,n=14):
    d=s.diff();u=d.clip(lower=0).ewm(alpha=1/n,adjust=False).mean();v=(-d.clip(upper=0)).ewm(alpha=1/n,adjust=False).mean();rs=u/v.replace(0,np.nan);return 100-100/(1+rs)
def macd(s):
    fast=s.ewm(span=MACD_FAST,adjust=False).mean();slow=s.ewm(span=MACD_SLOW,adjust=False).mean();line=fast-slow;signal=line.ewm(span=MACD_SIGNAL,adjust=False).mean();hist=line-signal;return line,signal,hist
def feat(x):
    x=x.copy();x["ema9"]=x.close.ewm(span=9,adjust=False).mean();x["ema20"]=x.close.ewm(span=20,adjust=False).mean();x["ema50"]=x.close.ewm(span=50,adjust=False).mean();x["rsi"]=rsi(x.close);tr=pd.concat([x.high-x.low,(x.high-x.close.shift()).abs(),(x.low-x.close.shift()).abs()],axis=1).max(axis=1);x["atr"]=tr.ewm(span=14,adjust=False).mean();x["vavg20"]=x.volume.rolling(20).mean();x["volx"]=x.volume/x.vavg20.replace(0,np.nan);x["prev20h"]=x.high.shift(1).rolling(20).max();x["prev20l"]=x.low.shift(1).rolling(20).min();tp=(x.high+x.low+x.close)/3
    if isinstance(x["timestamp"].iloc[0],pd.Timestamp):
        day=x.timestamp.dt.date;x["pv"]=tp*x.volume;x["cv"]=x.volume.groupby(day).cumsum();x["cpv"]=x.pv.groupby(day).cumsum();x["vwap"]=x.cpv/x.cv.replace(0,np.nan)
    else:x["vwap"]=(tp*x.volume).cumsum()/x.volume.cumsum().replace(0,np.nan)
    x["macd"],x["macd_signal"],x["macd_hist"]=macd(x.close);return x

def market_bias():
    for sid in [13,25]:
        try:
            d=candles(sid,120,"ONE_DAY")
            if d is None or len(d)<60:continue
            f=feat(d);x=f.iloc[-1]
            if x.close>x.ema20 and x.ema20>x.ema50:return "BULLISH",8
            if x.close<x.ema20 and x.ema20<x.ema50:return "BEARISH",-8
        except:pass
    return "NEUTRAL",0

def option_underlyings(inst):
    x=inst.copy()
    if x.empty:return set()
    x=x[x["SEM_EXM_EXCH_ID"].astype(str).str.upper()=="NSE"];seg=x["SEM_SEGMENT"].astype(str).str.upper();x=x[seg.isin(["OPT","D"])];x=x[x["SEM_SMST_SECURITY_ID"].notna()];s=x["SEM_TRADING_SYMBOL"].astype(str).str.upper();x=x[s.str.contains(r"(CE|PE)$",regex=True,na=False)];out=set()
    for v in x["SEM_TRADING_SYMBOL"].astype(str):
        m=re.match(r"^([A-Z0-9&-]+?)(?:\d{2}[A-Z]{3}\d{2,4}|\d{6,8})(?:\d+(?:\.\d+)?)?(CE|PE)$",v.upper())
        if m:out.add(clean_symbol(m.group(1)))
    return {s for s in out if s and not blocked(s)}

def build_stock_universe():
    inst=load_instruments()
    if inst.empty:return pd.DataFrame()
    opt_syms=option_underlyings(inst)
    if not opt_syms:return pd.DataFrame()
    eq=inst.copy();eq=eq[eq["SEM_EXM_EXCH_ID"].astype(str).str.upper()=="NSE"];eq=eq[eq["SEM_SEGMENT"].astype(str).str.upper()=="EQ"];eq=eq[eq["SEM_SMST_SECURITY_ID"].notna()];eq["SYM"]=eq["SEM_TRADING_SYMBOL"].astype(str).map(clean_symbol);eq=eq[eq["SYM"].isin(opt_syms)];eq=eq[~eq["SYM"].map(blocked)];eq=eq[eq["SYM"].str.len()>1].drop_duplicates("SYM")
    quotes=get_quotes_eq(eq["SEM_SMST_SECURITY_ID"].tolist())
    if not quotes:return pd.DataFrame()
    eq["QID"]=eq["SEM_SMST_SECURITY_ID"].map(lambda x:str(int(float(x))));eq["LTP"]=eq["QID"].map(lambda x:quotes.get(x,{}).get("last_price",0));eq["VOLUME"]=eq["QID"].map(lambda x:quotes.get(x,{}).get("volume",0))
    eq=eq[eq["LTP"]>=MIN_STOCK_PRICE];eq=eq[eq["VOLUME"]>=MIN_STOCK_VOL];eq=eq.sort_values("VOLUME",ascending=False).head(STOCK_UNIVERSE)
    print("\nOPTION-ELIGIBLE STOCK UNIVERSE:",len(eq))
    return eq.reset_index(drop=True)

def analyze_stock(sym,sec_id,live_ltp,mbias):
    try:
        daily=candles(sec_id,220,"ONE_DAY");d15=candles(sec_id,7,"FIFTEEN_MINUTE");d5=candles(sec_id,5,"FIVE_MINUTE")
        if daily is None or d15 is None or d5 is None:return None
        if len(daily)<120 or len(d15)<60 or len(d5)<60:return None
        fd=feat(daily);f15=feat(d15);f5=feat(d5);D=fd.iloc[-1];M=f15.iloc[-1];A=f5.iloc[-1]
        daily_bull=D.close>D.ema20 and D.ema20>D.ema50;daily_bear=D.close<D.ema20 and D.ema20<D.ema50
        bull15=M.close>M.ema20;bear15=M.close<M.ema20;bull5=A.close>A.ema9;bear5=A.close<A.ema9;above_vwap=A.close>A.vwap;below_vwap=A.close<A.vwap
        vol=float(A.volx) if not pd.isna(A.volx) else 0;rv=float(A.rsi) if not pd.isna(A.rsi) else 50
        macd_buy=A.macd>A.macd_signal and A.macd_hist>0;macd_sell=A.macd<A.macd_signal and A.macd_hist<0
        breakout_buy=A.close>A.prev20h and vol>=BREAKOUT_VOLX;breakout_sell=A.close<A.prev20l and vol>=BREAKOUT_VOLX
        buy=sum([20 if daily_bull else 0,20 if bull15 else 0,15 if bull5 else 0,10 if above_vwap else 0,10 if 55<=rv<=75 else 0,15 if macd_buy else 0,10 if vol>=BREAKOUT_VOLX else 0])
        sell=sum([20 if daily_bear else 0,20 if bear15 else 0,15 if bear5 else 0,10 if below_vwap else 0,10 if 25<=rv<=45 else 0,15 if macd_sell else 0,10 if vol>=BREAKOUT_VOLX else 0])
        if buy>sell:direction="CALL";tech=buy
        elif sell>buy:direction="PUT";tech=sell
        else:return None
        if tech<65 or vol<MIN_VOLX:return None
        if direction=="CALL" and not (daily_bull and bull15):return None
        if direction=="PUT" and not (daily_bear and bear15):return None
        entry=float(live_ltp);atr=max(float(A.atr),entry*.003)
        if direction=="CALL":
            swing=float(f5.low.iloc[-8:-2].min());sl=min(entry-atr,swing-0.15*atr);risk=entry-sl;t1=entry+1.5*risk;t2=entry+2*risk;t3=entry+3*risk
        else:
            swing=float(f5.high.iloc[-8:-2].max());sl=max(entry+atr,swing+0.15*atr);risk=sl-entry;t1=entry-1.5*risk;t2=entry-2*risk;t3=entry-3*risk
        if risk<=0 or risk/entry<MIN_SL_PCT:return None
        market_pts=8 if (mbias=="BULLISH" and direction=="CALL") or (mbias=="BEARISH" and direction=="PUT") else -8 if mbias!="NEUTRAL" else 0
        return {"symbol":clean_symbol(sym),"sec_id":str(sec_id),"direction":direction,"stock_score":int(tech),"market_pts":market_pts,"stock_ltp":entry,"stock_sl":float(sl),"stock_t1":float(t1),"stock_t2":float(t2),"stock_t3":float(t3),"stock_rsi":rv,"stock_volx":vol,"macd":float(A.macd),"macd_signal":float(A.macd_signal),"macd_hist":float(A.macd_hist),"macd_bias":"BULLISH" if macd_buy else "BEARISH" if macd_sell else "NEUTRAL","daily":"BULLISH" if daily_bull else "BEARISH","tf15":"BULLISH" if bull15 else "BEARISH","tf5":"BULLISH" if bull5 else "BEARISH","vwap":"ABOVE" if above_vwap else "BELOW","setup":"BREAKOUT" if (breakout_buy if direction=="CALL" else breakout_sell) else "PULLBACK/VWAP","market":mbias}
    except Exception as e:
        print("Stock analyze error",sym,e);return None

def get_next_expiries(opts, months=2):
    today=pd.Timestamp(datetime.now(IST).date());exps=sorted(pd.to_datetime(opts["SEM_EXPIRY_DATE"].dropna().unique()));out=[]
    for e in exps:
        e=pd.Timestamp(e)
        if e>=today:out.append(e)
        if len(out)>=months:break
    return out

def option_flow_2months(z):
    try:
        inst=load_instruments()
        if inst.empty:return None
        cols=["SEM_TRADING_SYMBOL","SEM_SEGMENT","SEM_EXM_EXCH_ID","SEM_STRIKE_PRICE","SEM_EXPIRY_DATE","SEM_OPTION_TYPE","SEM_SMST_SECURITY_ID"]
        if not all(c in inst.columns for c in cols):return None
        sym=clean_symbol(z["symbol"])
        o=inst[inst["SEM_TRADING_SYMBOL"].astype(str).str.contains(sym,case=False,na=False)].copy()
        o=o[o["SEM_EXM_EXCH_ID"].astype(str).str.upper()=="NSE"];o=o[o["SEM_SEGMENT"].astype(str).str.upper().isin(["OPT","D"])]
        o["SEM_STRIKE_PRICE"]=pd.to_numeric(o["SEM_STRIKE_PRICE"],errors="coerce");o["SEM_EXPIRY_DATE"]=pd.to_datetime(o["SEM_EXPIRY_DATE"],errors="coerce")
        o=o.dropna(subset=["SEM_STRIKE_PRICE","SEM_EXPIRY_DATE"]);today=pd.Timestamp(datetime.now(IST).date());o=o[o["SEM_EXPIRY_DATE"]>=today]
        exps=get_next_expiries(o,OPTION_MONTHS)
        if not exps:return None
        o=o[o["SEM_EXPIRY_DATE"].isin(exps)].copy();spot=float(z["stock_ltp"])
        strikes=sorted(o["SEM_STRIKE_PRICE"].unique())
        if not strikes:return None
        atm=min(strikes,key=lambda x:abs(float(x)-spot));near=sorted(strikes,key=lambda x:abs(float(x)-atm))[:OPTION_STRIKE_RANGE*2+1]
        o=o[o["SEM_STRIKE_PRICE"].isin(near)];ids=[int(float(x)) for x in o["SEM_SMST_SECURITY_ID"].dropna()];q=get_option_quotes(ids)
        cv=pv=co=po=0.;rows=[]
        for exp in exps:
            e=o[o["SEM_EXPIRY_DATE"]==exp];ec=e[e["SEM_OPTION_TYPE"].astype(str).str.upper().str.contains("CE",na=False)];ep=e[e["SEM_OPTION_TYPE"].astype(str).str.upper().str.contains("PE",na=False)]
            ev=pv0=eo=po0=0.
            for _,r in pd.concat([ec,ep]).iterrows():
                sid=str(int(float(r["SEM_SMST_SECURITY_ID"])));qq=q.get(sid,{});v=float(qq.get("volume",0));oi=float(qq.get("oi",0))
                if "CE" in str(r["SEM_OPTION_TYPE"]).upper():ev+=v;eo+=oi
                else:pv0+=v;po0+=oi
            cv+=ev;pv+=pv0;co+=eo;po+=po0
            rows.append({"expiry":pd.Timestamp(exp).strftime("%d-%b"),"ce_vol":ev,"pe_vol":pv0})
        wanted="CE" if z["direction"]=="CALL" else "PE"
        d=o[o["SEM_OPTION_TYPE"].astype(str).str.upper().str.contains(wanted,na=False)]
        if d.empty:return None
        ranked=[]
        for _,r in d[d["SEM_EXPIRY_DATE"]==exps[0]].iterrows():
            sid=str(int(float(r["SEM_SMST_SECURITY_ID"])));qq=q.get(sid,{});lp=float(qq.get("last_price",0));v=float(qq.get("volume",0))
            if lp<OPTION_MIN_PRICE:continue
            dist=abs(float(r["SEM_STRIKE_PRICE"])-atm);score=max(0,30-dist*2)
            if v>0:score+=15
            ranked.append((score,r,qq))
        if not ranked:return None
        ranked.sort(key=lambda x:x[0],reverse=True)
        _,row,qq=ranked[0];sid=str(int(float(row["SEM_SMST_SECURITY_ID"])))
        return {"security_id":sid,"trading_symbol":str(row["SEM_TRADING_SYMBOL"]),"strike":float(row["SEM_STRIKE_PRICE"]),"expiry":pd.Timestamp(row["SEM_EXPIRY_DATE"]).strftime("%d-%b-%Y"),"type":wanted,"atm":float(atm),"quote":qq,"flow_bias":"CALL_HEAVY" if cv>pv*1.2 else "PUT_HEAVY" if pv>cv*1.2 else "BALANCED"}
    except Exception as e:
        print("Option flow error:",e);return None

def main():
    print(f"Starting Bot Client: {DHAN_CLIENT_ID}")
    mbias,_=market_bias()
    print("Market Bias:",mbias)
    universe=build_stock_universe()
    if universe.empty:
        tg("❌ Universe empty");return
    quotes=get_quotes_eq(universe["SEM_SMST_SECURITY_ID"].tolist())
    signals=[]
    for _,row in universe.iterrows():
        qid=str(int(float(row["SEM_SMST_SECURITY_ID"])));live=quotes.get(qid,{}).get("last_price",0)
        if live<MIN_STOCK_PRICE:continue
        res=analyze_stock(row["SYM"],row["SEM_SMST_SECURITY_ID"],live,mbias)
        if res:
            res["option"]=option_flow_2months(res)
            signals.append(res)
        time.sleep(DELAY)
    signals=sorted(signals,key=lambda x:x["stock_score"],reverse=True)[:TOP_SIGNALS]
    if not signals:
        tg(f"No signals | Bias {mbias}");return
    msg=f"🚀 Intraday Signals | Bias {mbias}\n\n"
    for s in signals:
        opt=s.get("option")
        opt_str=f"{opt['trading_symbol']} @ {opt['quote'].get('last_price',0)}" if opt else "No Option"
        msg+=f"{s['symbol']} {s['direction']} | Score {s['stock_score']} | LTP {s['stock_ltp']:.1f} SL {s['stock_sl']:.1f} T1 {s['stock_t1']:.1f}\n{opt_str}\nSetup {s['setup']} Volx {s['stock_volx']:.2f}\n\n"
    tg(msg)
    print(msg)

if __name__=="__main__":
    main()
