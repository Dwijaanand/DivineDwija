import os,time,requests,pyotp,pandas as pd,numpy as np,logging;from datetime import datetime,timedelta;import pytz;DHAN_CLIENT_ID=os.getenv("DHAN_CLIENT_ID")
DHAN_PASSWORD=os.getenv("DHAN_PASSWORD")
DHAN_TOTP_SECRET=os.getenv("DHAN_TOTP_SECRET")
TELEGRAM_BOT_TOKEN=os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID=os.getenv("TELEGRAM_CHAT_ID")
DHAN_ACCESS_TOKEN=os.getenv("DHAN_ACCESS_TOKEN")
MIN_STOCK_PRICE=100;MIN_STOCK_VOL=100000;STOCK_UNIVERSE=35;TOP_SIGNALS=3;MIN_SCORE=80;MIN_VOLX=1.2;BREAKOUT_VOLX=1.5;MIN_SL_PCT=0.004;MACD_FAST=12;MACD_SLOW=26;MACD_SIGNAL=9
OPTION_ITM_STEPS=1          # 0 = ATM, 1 = 1 strike ITM;OPTION_EXPIRY_MAX_DAYS=14;OPTION_MIN_PRICE=5;OPTION_MAX_SPREAD_PCT=8.0;OPTION_CONFIRM_VOLX=1.2;OPTION_MONTHS=2;OPTION_STRIKE_RANGE=5       # ATM +/- 5 strikes;OPTION_FLOW_MIN_VOLX=1.2;DELAY=0.25;QUOTE_DELAY=1.0;IST=pytz.timezone("Asia/Kolkata");ETF_BLOCK=["BEES","ETF","LIQUID","GOLDBEES"];EXCLUDED={"LTIM"};BASE_URL="https://api.dhan.co/v2";MASTER_URL="https://images.dhan.co/api-data/api-scrip-master.csv"
logging.basicConfig(level=logging.ERROR)
def get_access_token():
    global DHAN_ACCESS_TOKEN
    if DHAN_ACCESS_TOKEN:return DHAN_ACCESS_TOKEN
    try:
        totp=pyotp.TOTP(DHAN_TOTP_SECRET).now()
        r=requests.post("https://auth.dhan.co/app/generateAccessToken",
                        params={"dhanClientId":DHAN_CLIENT_ID,"pin":DHAN_PASSWORD,"totp":totp},
                        timeout=20)
        j=r.json()
        if r.status_code==200 and j.get("accessToken"):
            DHAN_ACCESS_TOKEN=j["accessToken"]
            print("Dhan access token generated:",j.get("expiryTime",""))
            return DHAN_ACCESS_TOKEN
        print("Dhan auth error:",r.status_code,str(j)[:300])
    except Exception as e: print("Dhan auth exception:",e)
    return None

def tg(msg):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(msg)
        return
    try:
        requests.post( f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", data={"chat_id":TELEGRAM_CHAT_ID,"text":msg}, timeout=10 )
    except Exception as e:
        print("Telegram error:",e)
def headers():
    return { "Content-Type":"application/json", "Accept":"application/json", "access-token":DHAN_ACCESS_TOKEN, "client-id":DHAN_CLIENT_ID }
def api_post(endpoint,payload,retries=3):
    for attempt in range(retries):
        try:
            r=requests.post( BASE_URL+endpoint, headers=headers(), json=payload, timeout=20 )
            if r.status_code==200:
                return r.json()
            print("API ERROR",endpoint,r.status_code,r.text[:250])
            if r.status_code in (429,500,502,503,504):
                time.sleep(1.5*(attempt+1))
                continue
            return None
        except Exception as e:
            print("API EXCEPTION",endpoint,e)
            time.sleep(1.5*(attempt+1))
    return None
INSTRUMENTS=None
def load_instruments():
    global INSTRUMENTS
    if INSTRUMENTS is None:
        try:
            INSTRUMENTS=pd.read_csv(MASTER_URL,low_memory=False)
            print("Dhan master:",len(INSTRUMENTS))
        except Exception as e:
            print("Master error:",e)
            return pd.DataFrame()
    return INSTRUMENTS
def clean_symbol(x):
    return str(x).upper().replace("-EQ","").strip()
def blocked(sym):
    s=clean_symbol(sym)
    if s in EXCLUDED:
        return True
    return any(s.endswith(x) or x in s for x in ETF_BLOCK)
def get_quotes_eq(security_ids):
    out={}
    ids=[]
    for x in security_ids:
        try: ids.append(int(float(x)))
        except: pass
    ids=list(dict.fromkeys(ids))
    for start in range(0,len(ids),1000):
        batch=ids[start:start+1000]
        data=api_post("/marketfeed/quote",{"NSE_EQ":batch})
        if data and data.get("status")=="success":
            block=data.get("data",{}).get("NSE_EQ",{})
            for sid,v in block.items():
                try:
                    out[str(sid)]={ "last_price":float(v.get("last_price",0)), "volume":float(v.get("volume",0)) }
                except: pass
        if start+1000<len(ids):
            time.sleep(QUOTE_DELAY)
    return out
def get_option_quotes(security_ids):
    out={}
    ids=[]
    for x in security_ids:
        try: ids.append(int(float(x)))
        except: pass
    ids=list(dict.fromkeys(ids))
    for start in range(0,len(ids),1000):
        batch=ids[start:start+1000]
        data=api_post("/marketfeed/quote",{"NSE_FNO":batch})
        if data and data.get("status")=="success":
            block=data.get("data",{}).get("NSE_FNO",{})
            for sid,v in block.items():
                try:
                    out[str(sid)]={ "last_price":float(v.get("last_price",0)), "volume":float(v.get("volume",0)), "oi":float(v.get("oi",0)), "oi_change":float(v.get("oi_change",0)), "buy_qty":float(v.get("buy_quantity",0)), "sell_qty":float(v.get("sell_quantity",0)) }
                except: pass
        if start+1000<len(ids):
            time.sleep(QUOTE_DELAY)
    return out
def candles(security_id,days,interval):
    try:
        now=datetime.now(IST)
        frm=(now-timedelta(days=days)).strftime("%Y-%m-%d")
        to=now.strftime("%Y-%m-%d")
        if interval=="ONE_DAY":
            payload={ "securityId":str(int(float(security_id))), "exchangeSegment":"NSE_EQ", "instrument":"EQUITY", "expiryCode":0, "oi":False, "fromDate":frm, "toDate":to }
            data=api_post("/charts/historical",payload)
        else:
            mins={"FIVE_MINUTE":5,"FIFTEEN_MINUTE":15}.get(interval,5)
            payload={ "securityId":str(int(float(security_id))), "exchangeSegment":"NSE_EQ", "instrument":"EQUITY", "interval":str(mins), "oi":False, "fromDate":frm, "toDate":to }
            data=api_post("/charts/intraday",payload)
        if not data or data.get("status")=="failure":
            return None
        raw=data.get("data")
        if not raw: return None
        if isinstance(raw,dict) and all(k in raw for k in ["open","high","low","close","volume"]):
            df=pd.DataFrame({ "open":raw["open"], "high":raw["high"], "low":raw["low"], "close":raw["close"], "volume":raw["volume"], "timestamp":raw.get("timestamp",raw.get("start_Time",[])) })
        elif isinstance(raw,(dict,list)):
            df=pd.DataFrame(raw)
        else:
            return None
        if df.empty: return None
        rename={}
        for c in df.columns:
            lc=str(c).lower()
            if lc in ("start_time","starttime","timestamp","time","datetime"):
                rename[c]="timestamp"
            elif lc in ("open","high","low","close","volume"):
                rename[c]=lc
        df.rename(columns=rename,inplace=True)
        if not all(c in df.columns for c in ["open","high","low","close","volume"]):
            return None
        if "timestamp" not in df.columns:
            df["timestamp"]=pd.RangeIndex(len(df))
        else:
            df["timestamp"]=pd.to_datetime(df["timestamp"],errors="coerce")
        for c in ["open","high","low","close","volume"]:
            df[c]=pd.to_numeric(df[c],errors="coerce")
        df=df.dropna(subset=["open","high","low","close","volume"])
        return df.sort_values("timestamp").reset_index(drop=True)
    except Exception as e:
        print("Candle error",security_id,interval,e)
        return None
def rsi(s,n=14):
    d=s.diff()
    u=d.clip(lower=0).ewm(alpha=1/n,adjust=False).mean()
    v=(-d.clip(upper=0)).ewm(alpha=1/n,adjust=False).mean()
    rs=u/v.replace(0,np.nan)
    return 100-100/(1+rs)
def macd(s):
    fast=s.ewm(span=MACD_FAST,adjust=False).mean()
    slow=s.ewm(span=MACD_SLOW,adjust=False).mean()
    line=fast-slow
    signal=line.ewm(span=MACD_SIGNAL,adjust=False).mean()
    hist=line-signal
    return line,signal,hist
def feat(x):
    x=x.copy()
    x["ema9"]=x.close.ewm(span=9,adjust=False).mean()
    x["ema20"]=x.close.ewm(span=20,adjust=False).mean()
    x["ema50"]=x.close.ewm(span=50,adjust=False).mean()
    x["rsi"]=rsi(x.close)
    tr=pd.concat([ x.high-x.low, (x.high-x.close.shift()).abs(), (x.low-x.close.shift()).abs() ],axis=1).max(axis=1)
    x["atr"]=tr.ewm(span=14,adjust=False).mean()
    x["vavg20"]=x.volume.rolling(20).mean()
    x["volx"]=x.volume/x.vavg20.replace(0,np.nan)
    x["prev20h"]=x.high.shift(1).rolling(20).max()
    x["prev20l"]=x.low.shift(1).rolling(20).min()
    tp=(x.high+x.low+x.close)/3
    if isinstance(x["timestamp"].iloc[0],pd.Timestamp):
        day=x.timestamp.dt.date
        x["pv"]=tp*x.volume
        x["cv"]=x.volume.groupby(day).cumsum()
        x["cpv"]=x.pv.groupby(day).cumsum()
        x["vwap"]=x.cpv/x.cv.replace(0,np.nan)
    else:
        x["vwap"]=(tp*x.volume).cumsum()/x.volume.cumsum().replace(0,np.nan)
    x["macd"],x["macd_signal"],x["macd_hist"]=macd(x.close)
    return x
def market_bias():
    for sid in [13,25]:
        try:
            d=candles(sid,120,"ONE_DAY")
            if d is None or len(d)<60: continue
            f=feat(d)
            x=f.iloc[-1]
            if x.close>x.ema20 and x.ema20>x.ema50:
                return "BULLISH",8
            if x.close<x.ema20 and x.ema20<x.ema50:
                return "BEARISH",-8
        except: pass
    return "NEUTRAL",0
def option_underlyings(inst):
    x=inst.copy()
    if x.empty:return set()
    x=x[x["SEM_EXM_EXCH_ID"].astype(str).str.upper()=="NSE"]
    seg=x["SEM_SEGMENT"].astype(str).str.upper()
    x=x[seg.isin(["OPT","D"])]
    x=x[x["SEM_SMST_SECURITY_ID"].notna()]
    s=x["SEM_TRADING_SYMBOL"].astype(str).str.upper()
    x=x[s.str.contains(r"(CE|PE)$",regex=True,na=False)]
    out=set()
    for v in x["SEM_TRADING_SYMBOL"].astype(str):
        m=__import__("re").match(r"^([A-Z0-9&-]+?)(?:\d{2}[A-Z]{3}\d{2,4}|\d{6,8})(?:\d+(?:\.\d+)?)?(CE|PE)$",v.upper())
        if m:out.add(clean_symbol(m.group(1)))
    if not out:
        eq=inst[inst["SEM_SEGMENT"].astype(str).str.upper()=="EQ"].copy()
        eqs=set(eq["SEM_TRADING_SYMBOL"].astype(str).map(clean_symbol))
        for v in x["SEM_TRADING_SYMBOL"].astype(str).str.upper():
            for e in eqs:
                if v.startswith(e) and v.endswith(("CE","PE")):
                    out.add(e)
    return {s for s in out if s and not blocked(s)}
def build_stock_universe():
    inst=load_instruments()
    if inst.empty:return pd.DataFrame()
    opt_syms=option_underlyings(inst)
    if not opt_syms:return pd.DataFrame()
    eq=inst.copy()
    eq=eq[eq["SEM_EXM_EXCH_ID"].astype(str).str.upper()=="NSE"]
    eq=eq[eq["SEM_SEGMENT"].astype(str).str.upper()=="EQ"]
    eq=eq[eq["SEM_SMST_SECURITY_ID"].notna()]
    eq["SYM"]=eq["SEM_TRADING_SYMBOL"].astype(str).map(clean_symbol)
    eq=eq[eq["SYM"].isin(opt_syms)]
    eq=eq[~eq["SYM"].map(blocked)]
    eq=eq[eq["SYM"].str.len()>1].drop_duplicates("SYM")
    quotes=get_quotes_eq(eq["SEM_SMST_SECURITY_ID"].tolist())
    if not quotes:return pd.DataFrame()
    eq["QID"]=eq["SEM_SMST_SECURITY_ID"].map(lambda x:str(int(float(x))))
    eq["LTP"]=eq["QID"].map(lambda x:quotes.get(x,{}).get("last_price",0))
    eq["VOLUME"]=eq["QID"].map(lambda x:quotes.get(x,{}).get("volume",0))
    eq=eq[eq["LTP"]>=MIN_STOCK_PRICE]
    eq=eq[eq["VOLUME"]>=MIN_STOCK_VOL]
    eq=eq.sort_values("VOLUME",ascending=False).head(STOCK_UNIVERSE)
    print("\nOPTION-ELIGIBLE STOCK UNIVERSE:",len(eq))
    for _,r in eq.iterrows():print(r["SYM"],"|",round(r["LTP"],2),"| VOL",int(r["VOLUME"]))
    return eq.reset_index(drop=True)
def analyze_stock(sym,sec_id,live_ltp,mbias):
    try:
        daily=candles(sec_id,220,"ONE_DAY")
        d15=candles(sec_id,7,"FIFTEEN_MINUTE")
        d5=candles(sec_id,5,"FIVE_MINUTE")
        if daily is None or d15 is None or d5 is None:
            return None
        if len(daily)<120 or len(d15)<60 or len(d5)<60:
            return None
        fd=feat(daily)
        f15=feat(d15)
        f5=feat(d5)
        D=fd.iloc[-1]
        M=f15.iloc[-1]
        A=f5.iloc[-1]
        needed=["ema20","ema50"]
        if any(pd.isna(D[x]) for x in needed): return None
        daily_bull=D.close>D.ema20 and D.ema20>D.ema50
        daily_bear=D.close<D.ema20 and D.ema20<D.ema50
        bull15=M.close>M.ema20
        bear15=M.close<M.ema20
        bull5=A.close>A.ema9
        bear5=A.close<A.ema9
        above_vwap=A.close>A.vwap
        below_vwap=A.close<A.vwap
        vol=float(A.volx) if not pd.isna(A.volx) else 0
        rv=float(A.rsi) if not pd.isna(A.rsi) else 50
        macd_buy=A.macd>A.macd_signal and A.macd_hist>0
        macd_sell=A.macd<A.macd_signal and A.macd_hist<0
        breakout_buy=A.close>A.prev20h and vol>=BREAKOUT_VOLX
        breakout_sell=A.close<A.prev20l and vol>=BREAKOUT_VOLX
        buy=sum([ 20 if daily_bull else 0, 20 if bull15 else 0, 15 if bull5 else 0, 10 if above_vwap else 0, 10 if 55<=rv<=75 else 0, 15 if macd_buy else 0, 10 if vol>=BREAKOUT_VOLX else 0 ])
        sell=sum([ 20 if daily_bear else 0, 20 if bear15 else 0, 15 if bear5 else 0, 10 if below_vwap else 0, 10 if 25<=rv<=45 else 0, 15 if macd_sell else 0, 10 if vol>=BREAKOUT_VOLX else 0 ])
        if buy>sell:
            direction="CALL"
            tech=buy
        elif sell>buy:
            direction="PUT"
            tech=sell
        else:
            return None
        if tech<65 or vol<MIN_VOLX:
            return None
        if direction=="CALL" and not (daily_bull and bull15):
            return None
        if direction=="PUT" and not (daily_bear and bear15):
            return None
        entry=float(live_ltp)
        atr=max(float(A.atr),entry*.003)
        if direction=="CALL":
            swing=float(f5.low.iloc[-8:-2].min())
            sl=min(entry-atr,swing-0.15*atr)
            risk=entry-sl
            t1=entry+1.5*risk
            t2=entry+2*risk
            t3=entry+3*risk
        else:
            swing=float(f5.high.iloc[-8:-2].max())
            sl=max(entry+atr,swing+0.15*atr)
            risk=sl-entry
            t1=entry-1.5*risk
            t2=entry-2*risk
            t3=entry-3*risk
        if risk<=0 or risk/entry<MIN_SL_PCT:
            return None
        market_pts=0
        if mbias=="BULLISH" and direction=="CALL": market_pts=8
        elif mbias=="BEARISH" and direction=="PUT": market_pts=8
        elif mbias=="BULLISH" and direction=="PUT": market_pts=-8
        elif mbias=="BEARISH" and direction=="CALL": market_pts=-8
        return { "symbol":clean_symbol(sym), "sec_id":str(sec_id), "direction":direction, "stock_score":int(tech), "market_pts":market_pts, "stock_ltp":entry, "stock_sl":float(sl), "stock_t1":float(t1), "stock_t2":float(t2), "stock_t3":float(t3), "stock_rsi":rv, "stock_volx":vol, "macd":float(A.macd), "macd_signal":float(A.macd_signal), "macd_hist":float(A.macd_hist), "macd_bias":"BULLISH" if macd_buy else "BEARISH" if macd_sell else "NEUTRAL", "daily":"BULLISH" if daily_bull else "BEARISH", "tf15":"BULLISH" if bull15 else "BEARISH", "tf5":"BULLISH" if bull5 else "BEARISH", "vwap":"ABOVE" if above_vwap else "BELOW", "setup":"BREAKOUT" if (breakout_buy if direction=="CALL" else breakout_sell) else "PULLBACK/VWAP", "market":mbias }
    except Exception as e:
        print("Stock analyze error",sym,e)
        return None
def get_next_expiries(opts, months=2):
    today=pd.Timestamp(datetime.now(IST).date())
    exps=sorted(pd.to_datetime(opts["SEM_EXPIRY_DATE"].dropna().unique()))
    out=[]
    for e in exps:
        e=pd.Timestamp(e)
        if e>=today:
            out.append(e)
        if len(out)>=months:
            break
    return out
def option_flow_2months(z):
    try:
        inst=load_instruments()
        if inst.empty:return None
        cols=["SEM_TRADING_SYMBOL","SEM_SEGMENT","SEM_EXM_EXCH_ID","SEM_STRIKE_PRICE","SEM_EXPIRY_DATE","SEM_OPTION_TYPE","SEM_SMST_SECURITY_ID"]
        if not all(c in inst.columns for c in cols):return None
        sym=clean_symbol(z["symbol"])
        o=inst[inst["SEM_TRADING_SYMBOL"].astype(str).str.contains(sym,case=False,na=False)].copy()
        o=o[o["SEM_EXM_EXCH_ID"].astype(str).str.upper()=="NSE"]
        o=o[o["SEM_SEGMENT"].astype(str).str.upper().isin(["OPT","D"])]
        o["SEM_STRIKE_PRICE"]=pd.to_numeric(o["SEM_STRIKE_PRICE"],errors="coerce")
        o["SEM_EXPIRY_DATE"]=pd.to_datetime(o["SEM_EXPIRY_DATE"],errors="coerce")
        o=o.dropna(subset=["SEM_STRIKE_PRICE","SEM_EXPIRY_DATE"])
        today=pd.Timestamp(datetime.now(IST).date());o=o[o["SEM_EXPIRY_DATE"]>=today]
        exps=get_next_expiries(o,OPTION_MONTHS)
        if not exps:return None
        o=o[o["SEM_EXPIRY_DATE"].isin(exps)].copy();spot=float(z["stock_ltp"])
        strikes=sorted(o["SEM_STRIKE_PRICE"].unique())
        if not strikes:return None
        atm=min(strikes,key=lambda x:abs(float(x)-spot))
        near=sorted(strikes,key=lambda x:abs(float(x)-atm))[:OPTION_STRIKE_RANGE*2+1]
        o=o[o["SEM_STRIKE_PRICE"].isin(near)]
        ids=[int(float(x)) for x in o["SEM_SMST_SECURITY_ID"].dropna()]
        q=get_option_quotes(ids)
        cv=pv=co=po=cc=pc=0.;rows=[];best=None
        for exp in exps:
            e=o[o["SEM_EXPIRY_DATE"]==exp];ec=e[e["SEM_OPTION_TYPE"].astype(str).str.upper().str.contains("CE",na=False)];ep=e[e["SEM_OPTION_TYPE"].astype(str).str.upper().str.contains("PE",na=False)]
            ev=pv0=eo=po0=ecg=epg=0.
            for _,r in pd.concat([ec,ep]).iterrows():
                sid=str(int(float(r["SEM_SMST_SECURITY_ID"])));qq=q.get(sid,{})
                v=float(qq.get("volume",0));oi=float(qq.get("oi",0));chg=float(qq.get("oi_change",0))
                if "CE" in str(r["SEM_OPTION_TYPE"]).upper():ev+=v;eo+=oi;ecg+=chg;cc+=v>0
                else:pv0+=v;po0+=oi;epg+=chg;pc+=v>0
            cv+=ev;pv+=pv0;co+=eo;po+=po0
            rows.append({"expiry":pd.Timestamp(exp).strftime("%d-%b"),"ce_vol":ev,"pe_vol":pv0,"ce_oi":eo,"pe_oi":po0,"ce_oichg":ecg,"pe_oichg":epg})
        flow="CALL_HEAVY" if cv>pv*1.2 else "PUT_HEAVY" if pv>cv*1.2 else "BALANCED"
        wanted="CE" if z["direction"]=="CALL" else "PE"
        d=o[o["SEM_OPTION_TYPE"].astype(str).str.upper().str.contains(wanted,na=False)]
        if d.empty:return None
        ranked=[]
        for _,r in d[d["SEM_EXPIRY_DATE"]==exps[0]].iterrows():
            sid=str(int(float(r["SEM_SMST_SECURITY_ID"])));qq=q.get(sid,{})
            lp=float(qq.get("last_price",0));v=float(qq.get("volume",0));oi=float(qq.get("oi",0));chg=float(qq.get("oi_change",0))
            if lp<OPTION_MIN_PRICE:continue
            dist=abs(float(r["SEM_STRIKE_PRICE"])-atm)
            score=0
            score+=max(0,30-dist*2)
            if v>0:score+=15
            if chg>0:score+=10
            if wanted=="CE" and flow=="CALL_HEAVY":score+=15
            if wanted=="PE" and flow=="PUT_HEAVY":score+=15
            score+=min(20,np.log1p(max(v,0))/3)
            ranked.append((score,r,qq))
        if not ranked:return None
        ranked.sort(key=lambda x:x[0],reverse=True)
        _,row,qq=ranked[0];sid=str(int(float(row["SEM_SMST_SECURITY_ID"])))
        return {"security_id":sid,"trading_symbol":str(row["SEM_TRADING_SYMBOL"]),"strike":float(row["SEM_STRIKE_PRICE"]), "expiry":pd.Timestamp(row["SEM_EXPIRY_DATE"]).strftime("%d-%b-%Y"),"expiry_dt":pd.Timestamp(row["SEM_EXPIRY_DATE"]), "type":wanted,"atm":float(atm),"quote":qq,"flow_ce_vol":cv,"flow_pe_vol":pv,"flow_ce_oi":co,"flow_pe_oi":po, "flow_ce_oichg":sum(x["ce_oichg"] for x in rows),"flow_pe_oichg":sum(x["pe_oichg"] for x in rows), "flow_ce_pe_ratio":cv/max(pv,1),"flow_bias":flow,"flow_expiries":rows,"flow_ce_active":cc,"flow_pe_active":pc}
    except Exception as e:
        print("Option flow error:",e);return None
def select_option(z):
    return option_flow_2months(z)
def option_confirm(z,opt,quote):
    try:
        ltp=float(quote.get("last_price",0))
        volume=float(quote.get("volume",0))
        oi=float(quote.get("oi",0))
        oi_change=float(quote.get("oi_change",0))
        if ltp<OPTION_MIN_PRICE:
            return None
        sid=opt["security_id"]
        d5=candles_option(sid,5,"FIVE_MINUTE")
        if d5 is None or len(d5)<40:
            return None
        f=feat(d5)
        a=f.iloc[-1]
        if any(pd.isna(a[x]) for x in [ "ema9","ema20","rsi","vwap", "macd","macd_signal","macd_hist","volx" ]):
            return None
        premium_bull=( a.close>a.ema9 and a.close>a.ema20 and a.close>a.vwap )
        premium_bear=( a.close<a.ema9 and a.close<a.ema20 and a.close<a.vwap )
        macd_bull=a.macd>a.macd_signal and a.macd_hist>0
        macd_bear=a.macd<a.macd_signal and a.macd_hist<0
        volx=float(a.volx) if not pd.isna(a.volx) else 0
        rv=float(a.rsi) if not pd.isna(a.rsi) else 50
        score=0
        if z["direction"]=="CALL":
            if premium_bull: score+=25
            if macd_bull: score+=25
            if volx>=OPTION_CONFIRM_VOLX: score+=20
            if 50<=rv<=75: score+=15
            if oi_change>0: score+=10
            if ltp>float(a.close)*0.995: score+=5
            bias="BULLISH" if premium_bull and macd_bull else "WEAK"
        else:
            if premium_bear: score+=25
            if macd_bear: score+=25
            if volx>=OPTION_CONFIRM_VOLX: score+=20
            if 25<=rv<=50: score+=15
            if oi_change>0: score+=10
            if ltp>float(a.close)*0.995: score+=5
            bias="BEARISH" if premium_bear and macd_bear else "WEAK"
        flow_bonus=0
        if z["direction"]=="CALL" and z.get("flow_bias")=="CALL_HEAVY":
            flow_bonus=15
        elif z["direction"]=="PUT" and z.get("flow_bias")=="PUT_HEAVY":
            flow_bonus=15
        score += flow_bonus
        if score<60:
            return None
        opt_entry=ltp
        opt_atr=max(float(a.atr),opt_entry*0.05)
        if z["direction"]=="CALL":
            opt_sl=max(opt_entry-opt_atr,float(a.low.iloc[-8:-2].min()))
            if opt_sl>=opt_entry:
                opt_sl=opt_entry-opt_atr
            risk=opt_entry-opt_sl
            opt_t1=opt_entry+1.5*risk
            opt_t2=opt_entry+2*risk
            opt_t3=opt_entry+3*risk
        else:
            opt_sl=max(opt_entry-opt_atr,float(a.low.iloc[-8:-2].min()))
            if opt_sl>=opt_entry:
                opt_sl=opt_entry-opt_atr
            risk=opt_entry-opt_sl
            opt_t1=opt_entry+1.5*risk
            opt_t2=opt_entry+2*risk
            opt_t3=opt_entry+3*risk
        if risk<=0: return None
        return { "option_ltp":opt_entry, "option_sl":float(opt_sl), "option_t1":float(opt_t1), "option_t2":float(opt_t2), "option_t3":float(opt_t3), "option_score":int(score), "option_bias":bias, "option_rsi":rv, "option_volx":volx, "option_macd":float(a.macd), "option_macd_signal":float(a.macd_signal), "option_macd_hist":float(a.macd_hist), "option_oi":oi, "option_oi_change":oi_change, "option_volume":volume, "flow_bonus":flow_bonus, "flow_ce_vol":z.get("flow_ce_vol",0), "flow_pe_vol":z.get("flow_pe_vol",0), "flow_ce_oi":z.get("flow_ce_oi",0), "flow_pe_oi":z.get("flow_pe_oi",0), "flow_ce_oichg":z.get("flow_ce_oichg",0), "flow_pe_oichg":z.get("flow_pe_oichg",0), "flow_ratio":z.get("flow_ce_pe_ratio",0), "flow_bias":z.get("flow_bias","BALANCED"), "flow_expiries":z.get("flow_expiries",[]) }
    except Exception as e:
        print("Option confirm error:",opt.get("trading_symbol"),e)
        return None
def candles_option(security_id,days,interval):
    try:
        now=datetime.now(IST)
        frm=(now-timedelta(days=days)).strftime("%Y-%m-%d")
        to=now.strftime("%Y-%m-%d")
        mins=5 if interval=="FIVE_MINUTE" else 15
        payload={ "securityId":str(int(float(security_id))), "exchangeSegment":"NSE_FNO", "instrument":"OPTIDX", "interval":str(mins), "oi":True, "fromDate":frm, "toDate":to }
        data=api_post("/charts/intraday",payload)
        if not data or data.get("status")=="failure":
            payload["instrument"]="OPTSTK"
            data=api_post("/charts/intraday",payload)
        if not data or data.get("status")=="failure":
            return None
        raw=data.get("data")
        if not raw: return None
        if isinstance(raw,dict) and all(k in raw for k in ["open","high","low","close","volume"]):
            df=pd.DataFrame({ "open":raw["open"], "high":raw["high"], "low":raw["low"], "close":raw["close"], "volume":raw["volume"], "timestamp":raw.get("timestamp",raw.get("start_Time",[])) })
        else:
            df=pd.DataFrame(raw)
        if df.empty: return None
        rename={}
        for c in df.columns:
            lc=str(c).lower()
            if lc in ("start_time","starttime","timestamp","time","datetime"):
                rename[c]="timestamp"
            elif lc in ("open","high","low","close","volume"):
                rename[c]=lc
        df.rename(columns=rename,inplace=True)
        if not all(c in df.columns for c in ["open","high","low","close","volume"]):
            return None
        if "timestamp" not in df.columns:
            df["timestamp"]=pd.RangeIndex(len(df))
        else:
            df["timestamp"]=pd.to_datetime(df["timestamp"],errors="coerce")
        for c in ["open","high","low","close","volume"]:
            df[c]=pd.to_numeric(df[c],errors="coerce")
        return df.dropna( subset=["open","high","low","close","volume"] ).sort_values("timestamp").reset_index(drop=True)
    except Exception as e:
        print("Option candle error",security_id,e)
        return None
def fmt_lakh(x):
    try:
        x=float(x)
        if x>=10000000: return f"{x/10000000:.2f}Cr"
        if x>=100000: return f"{x/100000:.2f}L"
        if x>=1000: return f"{x/1000:.1f}K"
        return f"{x:.0f}"
    except: return "0"
def signal_text(z):
    exp_lines=[]
    for e in z.get("flow_expiries",[]):
        exp_lines.append( f"{e['expiry']}: " f"CE Vol {fmt_lakh(e['ce_vol'])} | " f"PE Vol {fmt_lakh(e['pe_vol'])} | " f"CE OI {fmt_lakh(e['ce_oi'])} | " f"PE OI {fmt_lakh(e['pe_oi'])}" )
    return ( f"🔥 DIVINE INTRADAY OPTION SIGNAL\n\n" f"{'🟢 BUY CALL' if z['direction']=='CALL' else '🔴 BUY PUT'}\n" f"{z['option_symbol']}\n\n" f"STOCK: {z['symbol']}\n" f"SETUP: {z['setup']}\n" f"EXPIRY: {z['expiry']}\n" f"STRIKE: {z['strike']:.0f} {z['direction']}\n" f"ATM: {z['atm']:.0f}\n\n" f"OPTION LTP: ₹{z['option_ltp']:.2f}\n" f"SL: ₹{z['option_sl']:.2f}\n" f"T1: ₹{z['option_t1']:.2f}\n" f"T2: ₹{z['option_t2']:.2f}\n" f"T3: ₹{z['option_t3']:.2f}\n\n" f"STOCK LTP: ₹{z['stock_ltp']:.2f}\n" f"STOCK SCORE: {z['stock_score']}/100\n" f"OPTION SCORE: {z['option_score']}/100\n" f"FINAL SCORE: {z['final_score']}\n\n" f"5M: {z['tf5']}\n" f"15M: {z['tf15']}\n" f"DAILY: {z['daily']}\n" f"VWAP: {z['vwap']}\n" f"RSI: {z['stock_rsi']:.1f}\n" f"VOLX: {z['stock_volx']:.2f}x\n" f"MACD: {z['macd_bias']}\n\n" f"📊 NEXT 2 EXPIRIES FLOW\n" + ("\n".join(exp_lines) if exp_lines else "No expiry flow") + f"\n\nCALL TOTAL VOL: {fmt_lakh(z['flow_ce_vol'])}\n" f"PUT TOTAL VOL: {fmt_lakh(z['flow_pe_vol'])}\n" f"CALL/PUT VOL RATIO: {z['flow_ratio']:.2f}\n" f"FLOW BIAS: {z['flow_bias']}\n" f"CALL OI: {fmt_lakh(z['flow_ce_oi'])}\n" f"PUT OI: {fmt_lakh(z['flow_pe_oi'])}\n" f"CALL OI CHG: {fmt_lakh(z['flow_ce_oichg'])}\n" f"PUT OI CHG: {fmt_lakh(z['flow_pe_oichg'])}\n\n" f"OPTION RSI: {z['option_rsi']:.1f}\n" f"OPTION VOLX: {z['option_volx']:.2f}x\n" f"OPTION MACD: {z['option_bias']}\n" f"SELECTED OPT VOL: {fmt_lakh(z['option_volume'])}\n" f"SELECTED OI: {fmt_lakh(z['option_oi'])}\n" f"SELECTED OI CHG: {fmt_lakh(z['option_oi_change'])}\n\n" f"MARKET: {z['market']}\n" f"⏱ {datetime.now(IST):%d-%m-%Y %H:%M IST}\n\n" f"⚠️ SIGNAL ONLY — NO AUTO ORDER" )
def main():
    print( f"\n=== DIVINE INTRADAY OPTION | F&O STOCKS ONLY | ATM +/-5 STRIKE CE/PE | 2-MONTH FLOW | " f"{datetime.now(IST):%d %b %Y %H:%M IST} ===" )
    if not DHAN_CLIENT_ID or not DHAN_PASSWORD or not DHAN_TOTP_SECRET or not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        tg( "⚠️ DIVINE DHAN OPTION\n\n" "NO SETUP\n" "REASON: GitHub Secrets missing (DHAN_CLIENT_ID/DHAN_PASSWORD/DHAN_TOTP_SECRET/TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID)" )
        return
    inst=load_instruments()
    if inst.empty:
        tg( "⚠️ DIVINE DHAN OPTION\n\n" "NO SETUP\n" "REASON: Dhan instrument master unavailable" )
        return
    mbias,mp=market_bias()
    print("MARKET:",mbias)
    universe=build_stock_universe()
    if universe.empty:
        tg( f"⚠️ DIVINE DHAN OPTION\n\n" f"NO SETUP\n" f"MARKET: {mbias}\n" f"REASON: Stock universe unavailable" )
        return
    stock_candidates=[]
    for i,(_,row) in enumerate(universe.iterrows(),1):
        sym=clean_symbol(row["SYM"])
        sec=str(int(float(row["SEM_SMST_SECURITY_ID"])))
        ltp=float(row["LTP"])
        print(f"\n[{i}/{len(universe)}] STOCK:",sym)
        z=analyze_stock(sym,sec,ltp,mbias)
        if z is None:
            print("  rejected")
            continue
        print( "  STOCK:", z["direction"], "score",z["stock_score"], "MACD",z["macd_bias"] )
        opt=select_option(z)
        if opt is None:
            print("  option contract unavailable")
            continue
        print( "  OPTION:", opt["trading_symbol"], "| strike",opt["strike"], "| expiry",opt["expiry"] )
        oq=get_option_quotes([opt["security_id"]])
        quote=oq.get(str(opt["security_id"]),{})
        if not quote:
            print("  option quote unavailable")
            continue
        oc=option_confirm(z,opt,quote)
        if oc is None:
            print("  option confirmation rejected")
            continue
        final_score=( z["stock_score"]+ max(z["market_pts"],0)+ oc["option_score"]*.35 + oc.get("flow_bonus",0) )
        z.update(opt)
        z.update(oc)
        z["option_symbol"]=opt["trading_symbol"]
        z["final_score"]=round(final_score,1)
        print( "  FINAL:", z["final_score"], "| OPTION SCORE", z["option_score"] )
        if z["final_score"]>=MIN_SCORE:
            stock_candidates.append(z)
        time.sleep(DELAY)
    if not stock_candidates:
        tg( "⚠️ DIVINE DHAN OPTION\n\n" "NO SETUP\n\n" f"MARKET: {mbias}\n" f"STOCK UNIVERSE: {len(universe)}\n" f"MIN SCORE: {MIN_SCORE}\n" f"MACD: {MACD_FAST},{MACD_SLOW},{MACD_SIGNAL}\nNEXT 2 EXPIRIES: CE + PE VOLUME/OI\n" "REASON: No stock + option confirmation passed" )
        return
    stock_candidates=sorted( stock_candidates, key=lambda x:( x["final_score"], x["option_score"], x["stock_score"], x["stock_volx"] ), reverse=True )[:TOP_SIGNALS]
    tg( "🔥 DIVINE INTRADAY OPTION\n" f"MARKET: {mbias}\n" f"SIGNALS: {len(stock_candidates)}\n" f"MACD: {MACD_FAST},{MACD_SLOW},{MACD_SIGNAL}\nNEXT 2 EXPIRIES: CE + PE VOLUME/OI\n" "F&O STOCK + DAILY/15M/5M + OPTION CONFIRMATION\n" "⚠️ NO AUTO ORDER" )
    for z in stock_candidates:
        msg=signal_text(z)
        print("\n"+msg)
        tg(msg)
        time.sleep(.5)
if __name__=="__main__":
    try:
        main()
    except Exception as e:
        print("FATAL:",e)
        tg( "⚠️ DIVINE DHAN OPTION\n\n" "NO SETUP\n" f"REASON: Scanner error\n{str(e)[:300]}" )
