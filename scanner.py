import os,json,time,random,requests
from datetime import datetime,timedelta
import pandas as pd
import pyotp
from SmartApi import SmartConnect

# ========== ANGEL ONE NSE BUY SCANNER V3.5.9 TURBO ==========
# SAME STRATEGY - 4X FASTER - 2600 STOCKS IN 30 MIN

API_KEY=os.getenv("API_KEY") or os.getenv("SMARTAPI_API_KEY")
CLIENT_ID=os.getenv("CLIENT_ID") or os.getenv("SMARTAPI_CLIENT_ID")
PASSWORD=os.getenv("PASSWORD") or os.getenv("SMARTAPI_PASSWORD")
TOTP_SECRET=os.getenv("TOTP_SECRET") or os.getenv("SMARTAPI_TOTP_SECRET")
TELE_BOT=os.getenv("TELEGRAM_BOT_TOKEN")
TELE_CHAT=os.getenv("TELEGRAM_CHAT_ID")

DELAY=0.35 # TURBO MODE
TOP_N=50
REST_EVERY=25
VOL_THRESHOLD=1.8

def log(m):
    print(m,flush=True)

def send_telegram(msg):
    if not TELE_BOT or not TELE_CHAT:
        return
    try:
        url=f"https://api.telegram.org/bot{TELE_BOT}/sendMessage"
        requests.post(url,json={"chat_id":TELE_CHAT,"text":msg,"parse_mode":"Markdown"},timeout=10)
    except Exception as e:
        log(f"Telegram fail: {e}")

def get_obj():
    clean_secret=TOTP_SECRET.strip().replace(" ","")
    obj=SmartConnect(api_key=API_KEY.strip())
    for attempt in range(1,3):
        try:
            totp=pyotp.TOTP(clean_secret).now()
            data=obj.generateSession(CLIENT_ID.strip(),PASSWORD.strip(),totp)
            if data and data.get("status"):
                log("Angel Login OK - V3.5.9 TURBO")
                return obj
        except Exception as e:
            log(f"Login attempt {attempt} failed: {e}")
            time.sleep(2)
    raise Exception("Angel One login failed")

def load_master():
    local="OpenAPIScripMaster.json"
    if os.path.exists(local):
        try:
            with open(local,"r") as f:
                data=json.load(f)
            if isinstance(data,list) and len(data)>1000:
                log(f"Local master loaded: {len(data)} records")
                return data
        except: pass
    url="https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
    r=requests.get(url,timeout=60)
    data=r.json()
    with open(local,"w") as f:
        json.dump(data,f)
    log(f"Master downloaded: {len(data)} records")
    return data

def get_candle(obj,token,interval="ONE_DAY",days=500):
    to_date=datetime.now()
    from_date=to_date-timedelta(days=days)
    params={
        "exchange":"NSE","symboltoken":str(token),"interval":interval,
        "fromdate":from_date.strftime("%Y-%m-%d %H:%M"),
        "todate":to_date.strftime("%Y-%m-%d %H:%M")
    }
    for attempt in range(5):
        try:
            res=obj.getCandleData(params)
            if res and res.get("status") and res.get("data"):
                df=pd.DataFrame(res["data"],columns=["time","open","high","low","close","volume"])
                df["time"]=pd.to_datetime(df["time"])
                for c in ["open","high","low","close","volume"]:
                    df[c]=pd.to_numeric(df[c],errors="coerce")
                return df.dropna().sort_values("time").reset_index(drop=True)
            msg=str(res).lower()
            if "rate" in msg or "access" in msg or "exceed" in msg:
                wait=6*(attempt+1)
                log(f" Rate limit. Waiting {wait}s")
                time.sleep(wait)
                continue
            return None
        except Exception as e:
            if "rate" in str(e).lower():
                time.sleep(6)
            else:
                return None
    return None

def calc_rsi(series,period=14):
    delta=series.diff()
    gain=delta.where(delta>0,0).rolling(period).mean()
    loss=(-delta.where(delta<0,0)).rolling(period).mean()
    rs=gain/loss
    return 100-(100/(1+rs))

def make_monthly(df):
    return df.set_index("time").resample("ME").agg({"open":"first","high":"max","low":"min","close":"last","volume":"sum"}).dropna().reset_index()

def make_weekly(df):
    return df.set_index("time").resample("W-FRI").agg({"open":"first","high":"max","low":"min","close":"last","volume":"sum"}).dropna().reset_index()

def get_completed_week_rows(w,scan_date):
    w=w.copy()
    scan_day=pd.to_datetime(scan_date).normalize()
    w["period_end"]=pd.to_datetime(w["time"]).dt.normalize()
    completed=w[w["period_end"] < scan_day]
    if len(completed)>=2:
        return completed.iloc[-1],completed.iloc[-2]
    return None,None

def get_completed_month_rows(m,scan_date):
    m=m.copy()
    scan_day=pd.to_datetime(scan_date).normalize()
    m["period_end"]=pd.to_datetime(m["time"]).dt.normalize()
    completed=m[m["period_end"] < scan_day]
    if len(completed)>=2:
        return completed.iloc[-1],completed.iloc[-2]
    return None,None

def get_star_score(volx,rsi,close,high_52,wl,wp,ml,mp,risk_pct):
    score=0
    score+= 25 if volx>=4 else 22 if volx>=3 else 19 if volx>=2.5 else 16 if volx>=2 else 13
    score+= 20 if 60<=rsi<=75 else 16 if (55<=rsi<60 or 75<rsi<=80) else 12 if 80<rsi<=85 else 8
    nh=(close/high_52)*100 if high_52>0 else 0
    score+= 20 if nh>=97 else 18 if nh>=94 else 16 if nh>=90 else 14 if nh>=87 else 11
    wc=((wl/wp)-1)*100 if wp>0 else 0
    score+= 15 if wc>=5 else 13 if wc>=3 else 11 if wc>=1.5 else 9
    mc=((ml/mp)-1)*100 if mp>0 else 0
    score+= 15 if mc>=8 else 13 if mc>=5 else 11 if mc>=2 else 9
    score+= 5 if risk_pct<=4 else 4 if risk_pct<=6 else 3 if risk_pct<=8 else 1
    return score

def get_stars(s):
    return "⭐⭐⭐⭐⭐" if s>=90 else "⭐⭐⭐⭐" if s>=82 else "⭐⭐⭐" if s>=74 else "⭐⭐" if s>=66 else "⭐"

def main():
    log("="*70)
    log(" ANGEL ONE NSE BUY SCANNER V3.5.9 TURBO - 30 MIN")
    log("="*70)
    obj=get_obj()
    master=load_master()
    nse_stocks=[s for s in master if s.get("exch_seg")=="NSE" and str(s.get("symbol","")).endswith("-EQ")]
    log(f"NSE stocks: {len(nse_stocks)}")
    log("\nPHASE 1: TURBO VOLUME SCAN (40-day)...\n")
    candidates=[]
    for idx,s in enumerate(nse_stocks):
        if idx%300==0:
            log(f"Scanning {idx}/{len(nse_stocks)} | Found: {len(candidates)}")
        df=get_candle(obj,s["token"],"ONE_DAY",40) # 60 ki jagah 40 - fast
        time.sleep(DELAY)
        if df is None or len(df)<25:
            continue
        try:
            avg_vol=df["volume"].iloc[-20:-1].mean()
            curr_vol=df["volume"].iloc[-1]
            if avg_vol>0 and (curr_vol/avg_vol)>=VOL_THRESHOLD:
                candidates.append((s,curr_vol/avg_vol,None)) # df daily ko phase2 me lenge
        except: continue
        if len(candidates)>=180:
            # Jaldi 180 mil gaye to 2600 pura scan karne ki zarurat nahi
            if idx>1200:
                break
    candidates=sorted(candidates,key=lambda x:x[1],reverse=True)[:TOP_N]
    log(f"\nPHASE 1 DONE: {len(candidates)} candidates - Time saved!\n")
    if not candidates:
        send_telegram(f"No picks today - V3.5.9 TURBO\nNSE: {len(nse_stocks)}")
        return
    time.sleep(5)
    log("PHASE 2: DAILY + WEEKLY + MONTHLY\n")
    picks=[]
    scan_date=datetime.now()
    for count,(stock,volx,_) in enumerate(candidates,1):
        sym=stock.get("name") or stock.get("symbol","UNKNOWN")
        log(f"[{count}/{len(candidates)}] {sym} - {volx:.1f}x")
        if count%REST_EVERY==0:
            log("Resting 5s...")
            time.sleep(5)
        df=get_candle(obj,stock["token"],"ONE_DAY",500)
        time.sleep(DELAY)
        if df is None or len(df)<200:
            continue
        try:
            df["rsi"]=calc_rsi(df["close"])
            close=float(df["close"].iloc[-1])
            rsi=float(df["rsi"].iloc[-1])
            high_52=float(df["high"].tail(250).max())
            w=make_weekly(df); m=make_monthly(df)
            if len(w)<3 or len(m)<3: continue
            lw,pw=get_completed_week_rows(w,scan_date)
            lm,pm=get_completed_month_rows(m,scan_date)
            if lw is None or pw is None or lm is None or pm is None: continue
            cond1=close>df["close"].rolling(44).mean().iloc[-1]
            cond2=55<rsi<90
            cond3=close>=high_52*0.85
            cond4=float(lw["close"])>float(pw["close"])
            cond5=float(lm["close"])>float(pm["close"])
            if cond1 and cond2 and cond3 and cond4 and cond5:
                sl=float(df["low"].tail(10).min())
                risk=close-sl
                if risk<=0: continue
                tgt1=close+(risk*1.5); tgt2=close+(risk*2.2)
                risk_pct=(risk/close)*100
                strength=get_star_score(volx,rsi,close,high_52,float(lw["close"]),float(pw["close"]),float(lm["close"]),float(pm["close"]),risk_pct)
                picks.append({"sym":sym,"close":close,"high_52":high_52,"volx":volx,"rsi":rsi,"wl":float(lw["close"]),"wp":float(pw["close"]),"ml":float(lm["close"]),"mp":float(pm["close"]),"sl":sl,"tgt1":tgt1,"tgt2":tgt2,"risk_pct":risk_pct,"strength":strength,"stars":get_stars(strength)})
                log(f" BUY ✅ {sym} | {strength}/100 {get_stars(strength)}")
        except Exception as e:
            log(f" Calc error {sym}: {e}")
    picks=sorted(picks,key=lambda x:x["strength"],reverse=True)
    log(f"\nDONE - V3.5.9 TURBO - Picks: {len(picks)}")
    if picks:
        ranked_text=[]
        for i,p in enumerate(picks[:5],1):
            ranked_text.append(f"*#{i} {p['sym']} {p['stars']}*\nStrength: {p['strength']}/100\nLTP: ₹{p['close']:.1f} | 52W: ₹{p['high_52']:.1f}\nVol: {p['volx']:.1f}x | RSI: {p['rsi']:.0f}\nW: {p['wl']:.0f}>{p['wp']:.0f} M: {p['ml']:.0f}>{p['mp']:.0f}\nRisk: {p['risk_pct']:.1f}%\nSL: ₹{p['sl']:.0f} | TGT: ₹{p['tgt1']:.0f} / ₹{p['tgt2']:.0f}\n")
        msg=(f"🚀 *PURA NSE BUY - {datetime.now().strftime('%d %b')} - V3.5.9 TURBO* 🚀\n\nTotal NSE: {len(nse_stocks)}\nTop Candidates: {len(candidates)}\nBUY: {len(picks)}\nScan Time: ~30 Min\n\n🏆 *TOP 5 SETUP RANKING*\n\n*SWING MODE* | Completed Candle ✅\n\n"+"\n".join(ranked_text))
        send_telegram(msg)
    else:
        send_telegram(f"No picks today - V3.5.9 TURBO\nNSE: {len(nse_stocks)}\nCandidates: {len(candidates)}")

if __name__=="__main__":
    try:
        main()
    except Exception as e:
        log(f"❌ FATAL ERROR: {e}")
        send_telegram(f"❌ SCANNER V3.5.9 TURBO ERROR\n\n{e}")
