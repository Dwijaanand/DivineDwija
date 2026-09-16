import os, time, requests, pyotp, pandas as pd, random
from datetime import datetime,timedelta
from SmartApi import SmartConnect

API_KEY=os.getenv("API_KEY")
CLIENT_ID=os.getenv("CLIENT_ID")
PASSWORD=os.getenv("PASSWORD")
TOTP_SECRET=os.getenv("TOTP_SECRET")
TELEGRAM_BOT_TOKEN=os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID=os.getenv("TELEGRAM_CHAT_ID")

MIN_PRICE=20.0
MIN_SCORE=70
MAX_SIGNALS=15
DELAY=2.5
REST_EVERY=10

MASTER_URL="https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
HIGH_URL="https://www.nseindia.com/api/live-analysis-data-52weekhighstock"
LOW_URL="https://www.nseindia.com/api/live-analysis-data-52weeklowstock"

NSE=requests.Session()
NSE.headers.update({"User-Agent":"Mozilla/5.0 Chrome/124.0.0.0","Referer":"https://www.nseindia.com/market-data/52-week-high-equity-market"})

def log(m): print(f"[{datetime.now().strftime('%H:%M:%S')}] {m}",flush=True)

def get_52w():
    NSE.get("https://www.nseindia.com",timeout=15)
    h=NSE.get(HIGH_URL,timeout=25).json(); time.sleep(2); l=NSE.get(LOW_URL,timeout=25).json()
    def ext(o):
        s=set()
        def w(x):
            if isinstance(x,dict):
                for k,v in x.items():
                    if str(k).lower() in ("symbol","symbols"):
                        if isinstance(v,str): s.add(v.upper())
                        elif isinstance(v,list):
                            for i in v:
                                if isinstance(i,str): s.add(i.upper())
                                elif isinstance(i,dict) and "symbol" in i: s.add(str(i["symbol"]).upper())
                for v in x.values(): w(v)
            elif isinstance(x,list):
                for i in x: w(i)
        w(o); return {x for x in s if x.isalnum() and len(x)<20}
    return ext(h), ext(l)

def load_master():
    r=requests.get(MASTER_URL,timeout=60).json()
    return {str(x.get("symbol")).replace("-EQ",""):str(x.get("token")) for x in r if str(x.get("exch_seg"))=="NSE" and str(x.get("symbol","")).endswith("-EQ")}

def login():
    obj=SmartConnect(api_key=API_KEY.strip())
    res=obj.generateSession(CLIENT_ID.strip(),PASSWORD.strip(),pyotp.TOTP(TOTP_SECRET.strip().replace(" ","")).now())
    if res.get("status"): log("Login OK"); return obj
    raise Exception(f"Login Fail {res}")

# ===== YAHI FIX HAI - RATE LIMIT HANDLE =====
def daily(obj,token):
    now=datetime.now(); frm=now-timedelta(days=750)
    q={"exchange":"NSE","symboltoken":str(token),"interval":"ONE_DAY","fromdate":frm.strftime("%Y-%m-%d 09:15"),"todate":now.strftime("%Y-%m-%d 15:30")}
    for attempt in range(5):
        try:
            res=obj.getCandleData(q)
            if not res or not res.get("data"):
                msg=str(res).lower()
                if "rate" in msg or "access" in msg or "exceed" in msg:
                    wait=30+attempt*15+random.randint(1,5)
                    log(f" Rate limit (response) wait {wait}s")
                    time.sleep(wait)
                    continue
                return None
            df=pd.DataFrame(res["data"],columns=["time","open","high","low","close","volume"])
            df["time"]=pd.to_datetime(df["time"])
            for c in ["open","high","low","close","volume"]: df[c]=pd.to_numeric(df[c],errors="coerce")
            df=df.dropna().sort_values("time").set_index("time")
            df=df[df.index.date < datetime.now().date()]
            return df if len(df)>=250 else None
        except Exception as e:
            em=str(e).lower()
            if "access denied" in em or "exceeding" in em or "rate" in em or "couldn't parse" in em:
                wait=60+attempt*15+random.randint(5,10)
                log(f" Rate limit (exception) wait {wait}s -> {e}")
                time.sleep(wait)
                continue
            else:
                log(f" Candle error {e}")
                return None
    return None
# =============================================

def ema(s,n): return s.ewm(span=n,adjust=False).mean()
def rsi(s,p=14):
    d=s.diff(); g=d.clip(lower=0); l=-d.clip(upper=0)
    return 100-(100/(1+g.ewm(alpha=1/p,adjust=False).mean()/l.ewm(alpha=1/p,adjust=False).mean()))
def add_ind(df):
    df["ema21"]=ema(df["close"],21); df["ema50"]=ema(df["close"],50); df["ema200"]=ema(df["close"],200)
    df["rsi"]=rsi(df["close"]); df["vol20"]=df["volume"].rolling(20).mean()
    df["hh20"]=df["high"].shift(1).rolling(20).max(); df["ll20"]=df["low"].shift(1).rolling(20).min()
    tr=pd.concat([df["high"]-df["low"],(df["high"]-df["close"].shift()).abs(),(df["low"]-df["close"].shift()).abs()],axis=1).max(axis=1)
    df["atr"]=tr.rolling(14).mean(); return df
def tf(df,rule):
    x=df.resample(rule).agg({"open":"first","high":"max","low":"min","close":"last","volume":"sum"}).dropna()
    return add_ind(x)

def analyze(df,sym,side):
    d=add_ind(df.copy()); w=tf(df,"W-FRI"); m=tf(df,"ME")
    if len(w)<50 or len(m)<20: return None
    dc=d.iloc[-1]; d1=d.iloc[-2]; wc=w.iloc[-1]; wc1=w.iloc[-2]; mc=m.iloc[-1]; mc1=m.iloc[-2]
    close=float(dc["close"]); atr=float(dc["atr"]); volx=float(dc["volume"]/dc["vol20"]) if dc["vol20"]>0 else 0
    if close<MIN_PRICE or atr<=0: return None
    score=0; why=[]; setup=None
    if side=="BUY":
        monthly_up=mc["close"]>mc["ema21"] and mc["close"]>mc1["close"]
        weekly_up=wc["close"]>wc["ema21"] and wc["close"]>wc["ema50"]
        if close>dc["hh20"]: setup="20D Breakout (52W High)"; score+=30; why.append("Daily 20D High Break")
        elif close>d1["high"] and close>dc["ema21"]: setup="Base Breakout"; score+=25; why.append(f"Prev High {d1['high']:.1f} Break")
        else: return None
        if monthly_up: score+=20; why.append(f"Monthly UP {mc1['close']:.0f}->{mc['close']:.0f}")
        if weekly_up: score+=20; why.append(f"Weekly UP {wc1['close']:.0f}->{wc['close']:.0f}")
        if close>dc["ema200"]: score+=10; why.append("200EMA Upar")
        if volx>=1.5: score+=15; why.append(f"Vol {volx:.1f}x")
        if 50<=dc["rsi"]<=75: score+=10; why.append(f"RSI {dc['rsi']:.0f}")
        if score<MIN_SCORE: return None
        sl=max(float(d["low"].tail(10).min()), close-1.5*atr, float(dc["ema21"])*0.985)
        risk=close-sl
        if risk<=0 or not 2<=risk/close*100<=8: return None
        return {"side":"BUY","symbol":sym,"setup":setup,"score":score,"price":close,"sl":sl,"t1":close+1.8*risk,"t2":close+3*risk,"risk":risk/close*100,"rsi":float(dc["rsi"]),"vol":volx,"why":why,"logic":"Monthly UP + Weekly UP + Daily Breakout"}
    else:
        monthly_down=mc["close"]<mc["ema21"] and mc["close"]<mc1["close"]
        weekly_down=wc["close"]<wc["ema21"] and wc["close"]<wc["ema50"]
        if close<dc["ll20"]: setup="20D Breakdown (52W Low)"; score+=30; why.append("Daily 20D Low Break")
        elif close<d1["low"] and close<dc["ema21"]: setup="Base Breakdown"; score+=25; why.append(f"Prev Low {d1['low']:.1f} Break")
        else: return None
        if monthly_down: score+=20; why.append(f"Monthly DOWN {mc1['close']:.0f}->{mc['close']:.0f}")
        if weekly_down: score+=20; why.append(f"Weekly DOWN {wc1['close']:.0f}->{wc['close']:.0f}")
        if close<dc["ema200"]: score+=10; why.append("200EMA Neeche")
        if volx>=1.2: score+=10; why.append(f"Vol {volx:.1f}x")
        if score<MIN_SCORE: return None
        cands=[x for x in [float(d["high"].tail(10).max()), close+1.5*atr, float(dc["ema21"])*1.015] if x>close]
        if not cands: return None
        sl=min(cands); risk=sl-close
        if risk<=0 or not 2<=risk/close*100<=8: return None
        return {"side":"SELL","symbol":sym,"setup":setup,"score":score,"price":close,"sl":sl,"t1":close-1.8*risk,"t2":close-3*risk,"risk":risk/close*100,"rsi":float(dc["rsi"]),"vol":volx,"why":why,"logic":"Monthly DOWN + Weekly DOWN + Daily Breakdown"}

def tg(txt):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID: return
    try:
        for i in range(0, len(txt), 3800):
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",json={"chat_id":TELEGRAM_CHAT_ID,"text":txt[i:i+3800],"parse_mode":"Markdown"},timeout=15)
            time.sleep(0.5)
    except Exception as e: log(f"TG {e}")

def fmt(s):
    icon="🟢" if s["side"]=="BUY" else "🔴"
    title="BUY" if s["side"]=="BUY" else "SELL/SHORT"
    why_txt="\n- ".join(s["why"])
    return f"""{icon} *{title} {s['symbol']}* | Score {s['score']}\n*Setup:* {s['setup']}\n*Entry:* ₹{s['price']:.2f}\n*SL:* ₹{s['sl']:.2f} ({s['risk']:.1f}% Risk)\n*TGT1:* ₹{s['t1']:.2f} | *TGT2:* ₹{s['t2']:.2f}\nRSI {s['rsi']:.0f} | Vol {s['vol']:.1f}x\n*Chart Read:*\n- {why_txt}\n"""

def main():
    log("=== NSE 52W V3.1 FIXED ===")
    high,low=get_52w(); master=load_master(); obj=login()
    ht={s:master[s] for s in high if s in master}
    lt={s:master[s] for s in low if s in master}
    log(f"BUY:{len(ht)} SELL:{len(lt)}")
    sigs=[]
    count=0
    for i,(sym,tok) in enumerate(ht.items(),1):
        count+=1
        if count%REST_EVERY==0: log(f"Resting 15s..."); time.sleep(15)
        log(f"[BUY {i}/{len(ht)}] {sym}"); df=daily(obj,tok); time.sleep(DELAY)
        if df is None: continue
        r=analyze(df,sym,"BUY")
        if r: sigs.append(r)
    for i,(sym,tok) in enumerate(lt.items(),1):
        count+=1
        if count%REST_EVERY==0: log(f"Resting 15s..."); time.sleep(15)
        log(f"[SELL {i}/{len(lt)}] {sym}"); df=daily(obj,tok); time.sleep(DELAY)
        if df is None: continue
        r=analyze(df,sym,"SELL")
        if r: sigs.append(r)
    sigs=sorted(sigs,key=lambda x:x["score"],reverse=True)[:MAX_SIGNALS]
    buys=[x for x in sigs if x["side"]=="BUY"]; sells=[x for x in sigs if x["side"]=="SELL"]
    log(f"DONE BUY:{len(buys)} SELL:{len(sells)}")
    if sigs:
        head=f"🚀 *NSE 52W {datetime.now().strftime('%d %b %Y')}* | High:{len(high)} Low:{len(low)} | BUY:{len(buys)} SELL:{len(sells)}\n\n"
        body="\n".join(fmt(x) for x in sigs)
        tg(head+body)
    else:
        tg(f"No Setup Today High:{len(high)} Low:{len(low)}")

if __name__=="__main__": main()
