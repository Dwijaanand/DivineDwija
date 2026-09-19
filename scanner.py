import os,json,time,random,requests
from datetime import datetime,timedelta
import pandas as pd
import pyotp
from SmartApi import SmartConnect

# ========== ANGEL ONE NSE BUY SCANNER V3.5.9 TURBO FIXED-2 ==========
# FINAL FIX for UTC+05:30 error

API_KEY=os.getenv("API_KEY") or os.getenv("SMARTAPI_API_KEY")
CLIENT_ID=os.getenv("CLIENT_ID") or os.getenv("SMARTAPI_CLIENT_ID")
PASSWORD=os.getenv("PASSWORD") or os.getenv("SMARTAPI_PASSWORD")
TOTP_SECRET=os.getenv("TOTP_SECRET") or os.getenv("SMARTAPI_TOTP_SECRET")
TELE_BOT=os.getenv("TELEGRAM_BOT_TOKEN")
TELE_CHAT=os.getenv("TELEGRAM_CHAT_ID")

DELAY=0.35
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
    if not API_KEY or not CLIENT_ID or not PASSWORD or not TOTP_SECRET:
        raise Exception(f"Secrets missing!")
    clean_secret=TOTP_SECRET.strip().replace(" ","")
    obj=SmartConnect(api_key=API_KEY.strip())
    for attempt in range(1,3):
        try:
            log(f"Generating TOTP... attempt {attempt}/2")
            totp=pyotp.TOTP(clean_secret).now()
            data=obj.generateSession(CLIENT_ID.strip(),PASSWORD.strip(),totp)
            if data and data.get("status"):
                log("Angel Login OK - V3.5.9 TURBO FIXED-2")
                return obj
            log(f"Login response: {data}")
        except Exception as e:
            log(f"Login attempt {attempt} failed: {e}")
            time.sleep(3)
    raise Exception("Angel One login failed")

def load_master():
    local="OpenAPIScripMaster.json"
    if os.path.exists(local):
        try:
            log("Trying local OpenAPIScripMaster.json...")
            with open(local,"r") as f:
                data=json.load(f)
            if isinstance(data,list) and len(data)>1000:
                log(f"Local master loaded: {len(data)} records")
                return data
        except Exception as e:
            log(f"Local master error: {e}")
    url="https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
    for i in range(3):
        try:
            log(f"Downloading master Attempt {i+1}/3...")
            r=requests.get(url,timeout=60)
            r.raise_for_status()
            data=r.json()
            if not isinstance(data,list) or len(data)<1000:
                raise Exception("Master data incomplete")
            with open(local,"w") as f:
                json.dump(data,f)
            log(f"Master downloaded: {len(data)} records")
            return data
        except Exception as e:
            log(f"Attempt {i+1} failed: {e}")
            time.sleep(5)
    raise Exception("NSE master loading failed")

def get_candle(obj,token,interval="ONE_DAY",days=500):
    to_date=datetime.now()
    from_date=to_date-timedelta(days=days)
    params={
        "exchange":"NSE",
        "symboltoken":str(token),
        "interval":interval,
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
                df=df.dropna().sort_values("time").reset_index(drop=True)
                return df
            msg=str(res).lower()
            if "rate" in msg or "access" in msg or "exceed" in msg or "too many" in msg:
                wait=8*(attempt+1)+random.randint(1,4)
                log(f" Rate limit. Waiting {wait}s ({attempt+1}/5)")
                time.sleep(wait)
                continue
            return None
        except Exception as e:
            em=str(e).lower()
            if "rate" in em or "access" in em or "exceed" in em:
                wait=8*(attempt+1)+random.randint(1,4)
                log(f" Rate limit. Waiting {wait}s ({attempt+1}/5)")
                time.sleep(wait)
            else:
                time.sleep(1)
                return None
    return None

def calc_rsi(series,period=14):
    delta=series.diff()
    gain=delta.where(delta>0,0).rolling(period).mean()
    loss=(-delta.where(delta<0,0)).rolling(period).mean()
    rs=gain/loss
    return 100-(100/(1+rs))

def make_monthly(df):
    x=df.copy()
    x["time"]=pd.to_datetime(x["time"], utc=True).dt.tz_convert(None)
    x=x.set_index("time")
    m=x.resample("ME").agg({"open":"first","high":"max","low":"min","close":"last","volume":"sum"}).dropna().reset_index()
    return m

def make_weekly(df):
    x=df.copy()
    x["time"]=pd.to_datetime(x["time"], utc=True).dt.tz_convert(None)
    x=x.set_index("time")
    w=x.resample("W-FRI").agg({"open":"first","high":"max","low":"min","close":"last","volume":"sum"}).dropna().reset_index()
    return w

# ===== FINAL FIX - TIMEZONE ERROR SOLVED =====
def get_completed_week_rows(w,scan_date):
    w=w.copy()
    w["time"]=pd.to_datetime(w["time"], utc=True).dt.tz_convert(None)
    scan_day=pd.to_datetime(scan_date).tz_localize(None).normalize()
    w["period_end"]=w["time"].dt.normalize()
    completed=w[w["period_end"] < scan_day].copy()
    if len(completed)>=2:
        return completed.iloc[-1],completed.iloc[-2]
    return None,None

def get_completed_month_rows(m,scan_date):
    m=m.copy()
    m["time"]=pd.to_datetime(m["time"], utc=True).dt.tz_convert(None)
    scan_day=pd.to_datetime(scan_date).tz_localize(None).normalize()
    m["period_end"]=m["time"].dt.normalize()
    completed=m[m["period_end"] < scan_day].copy()
    if len(completed)>=2:
        return completed.iloc[-1],completed.iloc[-2]
    return None,None

def get_star_score(volx,rsi,close,high_52,week_latest,week_previous,month_latest,month_previous,risk_pct):
    score=0
    if volx>=4: score+=25
    elif volx>=3: score+=22
    elif volx>=2.5: score+=19
    elif volx>=2: score+=16
    else: score+=13
    if 60<=rsi<=75: score+=20
    elif 55<=rsi<60 or 75<rsi<=80: score+=16
    elif 80<rsi<=85: score+=12
    else: score+=8
    near_high=(close/high_52)*100 if high_52>0 else 0
    if near_high>=97: score+=20
    elif near_high>=94: score+=18
    elif near_high>=90: score+=16
    elif near_high>=87: score+=14
    else: score+=11
    week_change=((week_latest/week_previous)-1)*100 if week_previous>0 else 0
    if week_change>=5: score+=15
    elif week_change>=3: score+=13
    elif week_change>=1.5: score+=11
    else: score+=9
    month_change=((month_latest/month_previous)-1)*100 if month_previous>0 else 0
    if month_change>=8: score+=15
    elif month_change>=5: score+=13
    elif month_change>=2: score+=11
    else: score+=9
    if risk_pct<=4: score+=5
    elif risk_pct<=6: score+=4
    elif risk_pct<=8: score+=3
    else: score+=1
    return score

def get_stars(score):
    if score>=90: return "⭐⭐⭐⭐⭐"
    elif score>=82: return "⭐⭐⭐⭐"
    elif score>=74: return "⭐⭐⭐"
    elif score>=66: return "⭐⭐"
    return "⭐"

def main():
    log("="*70)
    log(" ANGEL ONE NSE BUY SCANNER V3.5.9 TURBO FIXED-2")
    log("="*70)
    log(f"Time: {datetime.now().strftime('%d-%m-%Y %H:%M:%S')} IST")
    obj=get_obj()
    master=load_master()
    nse_stocks=[s for s in master if s.get("exch_seg")=="NSE" and str(s.get("symbol","")).endswith("-EQ")]
    log(f"NSE stocks: {len(nse_stocks)}")
    log("\nPHASE 1: TURBO VOLUME SCAN (40-day)...\n")
    candidates=[]
    for idx,s in enumerate(nse_stocks):
        if idx%300==0:
            log(f"Scanning {idx}/{len(nse_stocks)} | Found: {len(candidates)}")
        df=get_candle(obj,s["token"],"ONE_DAY",40)
        time.sleep(DELAY)
        if df is None or len(df)<25:
            continue
        try:
            avg_vol=df["volume"].iloc[-20:-1].mean()
            curr_vol=df["volume"].iloc[-1]
            if avg_vol>0:
                volx=curr_vol/avg_vol
                if volx>=VOL_THRESHOLD:
                    candidates.append((s,volx))
        except Exception:
            continue
    candidates=sorted(candidates,key=lambda x:x[1],reverse=True)[:TOP_N]
    log(f"\nPHASE 1 DONE: {len(candidates)} candidates - Time saved!\n")
    if not candidates:
        send_telegram(f"No picks today - V3.5.9 FIXED-2\nNSE: {len(nse_stocks)}\nVolume candidates: 0")
        return
    time.sleep(5)
    log("PHASE 2: DAILY + WEEKLY + MONTHLY\n")
    picks=[]
    scan_date=datetime.now()
    for count,(stock,volx) in enumerate(candidates,1):
        sym=stock.get("name") or stock.get("symbol","UNKNOWN")
        log(f"[{count}/{len(candidates)}] {sym} - {volx:.1f}x")
        if count%REST_EVERY==0:
            log("Resting 5s...")
            time.sleep(5)
        df=get_candle(obj,stock["token"],"ONE_DAY",500)
        time.sleep(DELAY)
        if df is None or len(df)<200:
            log(" Not enough daily data - skip")
            continue
        try:
            df=df.sort_values("time").reset_index(drop=True)
            df["rsi"]=calc_rsi(df["close"])
            close=float(df["close"].iloc[-1])
            rsi=float(df["rsi"].iloc[-1])
            high_52=float(df["high"].tail(250).max())
            w=make_weekly(df)
            m=make_monthly(df)
            if len(w)<3 or len(m)<3:
                log(" Weekly/Monthly data insufficient - skip")
                continue
            latest_week,previous_week=get_completed_week_rows(w,scan_date)
            if latest_week is None or previous_week is None:
                log(" Completed weekly data insufficient - skip")
                continue
            latest_month,previous_month=get_completed_month_rows(m,scan_date)
            if latest_month is None or previous_month is None:
                log(" Completed monthly data insufficient - skip")
                continue
            daily_avg=df["close"].rolling(44).mean().iloc[-1]
            cond1=(close>daily_avg)
            cond2=(55<rsi<90)
            cond3=(close>=high_52*0.85)
            week_latest_close=float(latest_week["close"])
            week_previous_close=float(previous_week["close"])
            cond4=(week_latest_close>week_previous_close)
            month_latest_close=float(latest_month["close"])
            month_previous_close=float(previous_month["close"])
            cond5=(month_latest_close>month_previous_close)
            if cond1 and cond2 and cond3 and cond4 and cond5:
                sl=float(df["low"].tail(10).min())
                risk=close-sl
                if risk<=0:
                    log(" Invalid SL - skip")
                    continue
                tgt1=close+(risk*1.5)
                tgt2=close+(risk*2.2)
                risk_pct=(risk/close)*100
                strength=get_star_score(volx,rsi,close,high_52,week_latest_close,week_previous_close,month_latest_close,month_previous_close,risk_pct)
                stars=get_stars(strength)
                picks.append({"sym":sym,"close":close,"high_52":high_52,"volx":volx,"rsi":rsi,"week_latest":week_latest_close,"week_previous":week_previous_close,"month_latest":month_latest_close,"month_previous":month_previous_close,"sl":sl,"tgt1":tgt1,"tgt2":tgt2,"risk_pct":risk_pct,"strength":strength,"stars":stars})
                log(f" BUY ✅ | Strength {strength}/100 | {stars}")
            else:
                log(" No qualifying setup")
        except Exception as e:
            log(f" Calculation error {sym}: {e}")
        time.sleep(DELAY)
    picks=sorted(picks,key=lambda x:x["strength"],reverse=True)
    log("\n"+"="*70)
    log(f"DONE - V3.5.9 TURBO FIXED-2 COMPLETE - Picks: {len(picks)}")
    log("="*70)
    if picks:
        log("\n🏆 FINAL RANKING\n")
        for i,p in enumerate(picks[:5],1):
            log(f"#{i} {p['sym']} {p['stars']} Strength {p['strength']}/100")
        ranked_text=[]
        for i,p in enumerate(picks[:5],1):
            text=(f"*#{i} {p['sym']} {p['stars']}*\nStrength: {p['strength']}/100\nLTP: ₹{p['close']:.1f} | 52W: ₹{p['high_52']:.1f}\nVol: {p['volx']:.1f}x | RSI: {p['rsi']:.0f}\nW: {p['week_latest']:.0f}>{p['week_previous']:.0f} M: {p['month_latest']:.0f}>{p['month_previous']:.0f}\nRisk: {p['risk_pct']:.1f}%\nSL: ₹{p['sl']:.0f} | TGT: ₹{p['tgt1']:.0f} / ₹{p['tgt2']:.0f}\n")
            ranked_text.append(text)
        msg=(f"🚀 *PURA NSE BUY - {datetime.now().strftime('%d %b')} - V3.5.9 TURBO FIXED-2* 🚀\n\nTotal NSE: {len(nse_stocks)}\nTop Candidates: {len(candidates)}\nBUY: {len(picks)}\nScan Time: ~30 Min\n\n🏆 *TOP 5 SETUP RANKING*\n\nRanking = Setup Strength, NOT guaranteed probability.\n\n*SWING MODE*\nDaily: Swing Setup ✅\nWeekly: Trend Confirmation ✅\nMonthly: Quality Filter ✅\nCompleted Candle: ✅\n\n"+"\n".join(ranked_text))
        send_telegram(msg)
        log("Telegram ranked BUY alert sent.")
    else:
        msg=(f"No picks today - V3.5.9 TURBO FIXED-2\nNSE: {len(nse_stocks)}\nCandidates: {len(candidates)}\n\nDaily + Weekly + Monthly filters: No BUY - Market weak")
        send_telegram(msg)
        log("No BUY today. Telegram status sent.")

if __name__=="__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Stopped by user.")
    except Exception as e:
        log(f"\n❌ FATAL ERROR: {e}")
        send_telegram(f"❌ ANGEL ONE SCANNER V3.5.9 FIXED-2 ERROR\n\n{e}")
