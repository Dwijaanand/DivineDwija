import os,time,requests,signal
import pandas as pd
from datetime import datetime,timedelta
from zoneinfo import ZoneInfo
import pyotp
from SmartApi import SmartConnect

IST=ZoneInfo("Asia/Kolkata")
MASTER_URLS=[
"https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json",
"https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
]

TOP=80
MIN_LTP=50
MIN_VOL=100000
MIN_AVG20=50000
MIN_VOLX=2.0
HISTORY=760
STOCK_DELAY=2.0
RATE_WAIT=45
MAX_RETRY=1
BATCH=50

EXCLUDED={"LTIM","TATAMOTORS"}

API_KEY=os.getenv("API_KEY","").strip()
CLIENT_ID=os.getenv("CLIENT_ID","").strip()
PASSWORD=os.getenv("PASSWORD","").strip()
TOTP_SECRET=os.getenv("TOTP_SECRET","").strip()
TG_TOKEN=os.getenv("TELEGRAM_BOT_TOKEN","").strip()
TG_CHAT=os.getenv("TELEGRAM_CHAT_ID","").strip()

def log(x):
    print(x,flush=True)

def fail(msg):
    log("❌ "+msg)
    send_tg("❌ ANGEL ONE SCANNER\n\n"+msg)
    raise SystemExit

def send_tg(msg):
    if not TG_TOKEN or not TG_CHAT:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            data={"chat_id":TG_CHAT,"text":msg},
            timeout=15
        )
    except Exception as e:
        log("Telegram error: "+str(e))

def login():
    if not all([API_KEY,CLIENT_ID,PASSWORD,TOTP_SECRET]):
        fail("Credentials missing. Check GitHub Secrets.")
    for attempt in range(1,3):
        try:
            log(f"Login attempt {attempt}/2...")
            api=SmartConnect(api_key=API_KEY)
            totp=pyotp.TOTP(TOTP_SECRET).now()
            if hasattr(signal,"SIGALRM"):
                signal.alarm(30)
            try:
                r=api.generateSession(CLIENT_ID,PASSWORD,totp)
            finally:
                if hasattr(signal,"SIGALRM"):
                    signal.alarm(0)
            if r and r.get("status"):
                log("✅ Angel One login successful.")
                return api
            log("Login failed: "+str(r))
        except Exception as e:
            log("Login error: "+str(e))
        time.sleep(5)
    fail("Angel One login failed.")

def load_master():
    log("Loading NSE master...")
    data=None
    for url in MASTER_URLS:
        for attempt in range(1,4):
            try:
                log(f"Master download {attempt}/3...")
                r=requests.get(
                    url,
                    timeout=(15,90),
                    headers={"User-Agent":"Mozilla/5.0"}
                )
                r.raise_for_status()
                j=r.json()
                if isinstance(j,list) and len(j)>1000:
                    data=j
                    log(f"Master downloaded: {len(j)} instruments")
                    break
            except Exception as e:
                log("Master error: "+str(e))
                time.sleep(5)
        if data:
            break

    if not data:
        fail("NSE master download failed.")

    out={}
    for x in data:
        try:
            seg=str(x.get("exch_seg","")).lower().strip()
            if seg not in ("nse","nse_cm"):
                continue

            raw=str(x.get("symbol","")).strip()
            if not raw.upper().endswith("-EQ"):
                continue

            sym=raw[:-3].strip().upper()
            if not sym or sym in EXCLUDED:
                continue

            token=str(x.get("token","")).strip()
            if token:
                out[sym]=token
        except:
            continue

    log(f"✅ NSE-EQ stocks loaded: {len(out)}")

    if len(out)<100:
        fail("NSE-EQ symbols not found in master.")

    return out

def bulk_quotes(api,tokens):
    result=[]
    for i in range(0,len(tokens),BATCH):
        batch=tokens[i:i+BATCH]
        for attempt in range(1,3):
            try:
                r=api.getMarketData(
                    "FULL",
                    {"exchangeTokens":{"NSE":batch}}
                )
                if r and r.get("status"):
                    rows=r.get("data",{}).get("fetched",[])
                    result.extend(rows)
                    break
                log("Quote error: "+str(r))
            except Exception as e:
                log("Quote exception: "+str(e))
            time.sleep(5)
    return result

def phase1(api,master):
    log("\nPHASE 1: NSE liquidity scan...")
    reverse={v:k for k,v in master.items()}
    tokens=list(master.values())
    rows=bulk_quotes(api,tokens)

    candidates=[]
    for q in rows:
        try:
            token=str(q.get("symbolToken",""))
            sym=reverse.get(token,"")
            if not sym:
                continue
            ltp=float(q.get("ltp",0) or 0)
            vol=float(q.get("tradeVolume",q.get("volume",0)) or 0)
            if ltp<MIN_LTP or vol<MIN_VOL:
                continue
            candidates.append({
                "symbol":sym,
                "token":token,
                "ltp":ltp,
                "volume":vol
            })
        except:
            continue

    candidates.sort(key=lambda x:x["volume"],reverse=True)
    candidates=candidates[:TOP]

    log(f"Candidates: {len(candidates)}")
    return candidates

def candle(api,token):
    to_dt=datetime.now(IST).replace(tzinfo=None)
    from_dt=to_dt-timedelta(days=HISTORY)

    params={
        "exchange":"NSE",
        "symboltoken":str(token),
        "interval":"ONE_DAY",
        "fromdate":from_dt.strftime("%Y-%m-%d %H:%M"),
        "todate":to_dt.strftime("%Y-%m-%d %H:%M")
    }

    for attempt in range(MAX_RETRY+1):
        try:
            r=api.getCandleData(params)

            if r and r.get("status"):
                data=r.get("data")
                if data:
                    df=pd.DataFrame(
                        data,
                        columns=["date","open","high","low","close","volume"]
                    )
                    for c in ["open","high","low","close","volume"]:
                        df[c]=pd.to_numeric(df[c],errors="coerce")
                    df["date"]=pd.to_datetime(df["date"],errors="coerce")
                    df=df.dropna().sort_values("date").reset_index(drop=True)

                    if len(df)>2:
                        today=datetime.now(IST).date()
                        df=df[df["date"].dt.date<today].copy()

                    return df

            txt=str(r)
            if "rate" in txt.lower() or "access denied" in txt.lower():
                log(f"Rate limit. Waiting {RATE_WAIT}s...")
                time.sleep(RATE_WAIT)
            else:
                log("Candle error: "+txt[:250])
                time.sleep(5)

        except Exception as e:
            txt=str(e)
            if "rate" in txt.lower() or "access denied" in txt.lower():
                log(f"Rate limit. Waiting {RATE_WAIT}s...")
                time.sleep(RATE_WAIT)
            else:
                log("Candle exception: "+txt[:250])
                time.sleep(5)

    return None

def analyze(sym,df,ltp):
    if df is None or len(df)<200:
        return None

    df=df.copy()

    df["sma20v"]=df["volume"].rolling(20).mean()
    df["ema21"]=df["close"].ewm(span=21,adjust=False).mean()
    df["ema50"]=df["close"].ewm(span=50,adjust=False).mean()
    df["ema200"]=df["close"].ewm(span=200,adjust=False).mean()

    prev=df.iloc[:-1]
    cur=df.iloc[-1]

    avg20=float(prev["volume"].tail(20).mean())
    if avg20<MIN_AVG20:
        return None

    volx=float(cur["volume"])/avg20 if avg20 else 0
    if volx<MIN_VOLX:
        return None

    high52=float(prev["high"].tail(252).max())
    if high52<=0:
        return None

    close=float(cur["close"])
    op=float(cur["open"])
    high=float(cur["high"])
    low=float(cur["low"])

    near_high=close>=high52*0.92
    green=close>op

    weekly=df.set_index("date")["close"].resample("W-FRI").last().dropna()
    weekly_sma40=weekly.rolling(40).mean()
    weekly_up=False
    if len(weekly)>=40:
        weekly_up=float(weekly.iloc[-1])>float(weekly_sma40.iloc[-1])

    # CORE BUY STRATEGY - UNCHANGED
    buy=weekly_up and near_high and green
    if not buy:
        return None

    monthly=df.set_index("date")["close"].resample("ME").last().dropna()
    monthly_sma10=monthly.rolling(10).mean()

    monthly_up=False
    monthly_slope=False

    if len(monthly)>=10:
        monthly_up=float(monthly.iloc[-1])>float(monthly_sma10.iloc[-1])
    if len(monthly)>=13:
        monthly_slope=float(monthly_sma10.iloc[-1])>=float(monthly_sma10.iloc[-4])

    dist=max(0,min(100,(1-(high52-close)/high52/0.08)*100))
    prox_score=min(35,max(0,dist*0.35))

    vol_score=min(25,max(0,((volx-2)/8)*25))
    month_score=15 if monthly_up and monthly_slope else (10 if monthly_up else 0)
    week_score=15 if weekly_up else 0

    rng=high-low
    body=abs(close-op)
    candle_strength=(body/rng) if rng>0 else 0
    daily_score=min(10,max(0,candle_strength*10))

    score=round(prox_score+vol_score+month_score+week_score+daily_score)

    if score>=85:
        stars="★★★★★"
    elif score>=72:
        stars="★★★★☆"
    elif score>=60:
        stars="★★★☆☆"
    elif score>=48:
        stars="★★☆☆☆"
    else:
        stars="★☆☆☆☆"

    sl=low
    t1=close*1.05
    t2=close*1.08
    risk=((close-sl)/close)*100 if close else 0

    return {
        "symbol":sym,
        "ltp":ltp,
        "close":close,
        "sl":sl,
        "t1":t1,
        "t2":t2,
        "volx":volx,
        "high52":high52,
        "score":score,
        "stars":stars,
        "monthly":monthly_up and monthly_slope,
        "weekly":weekly_up,
        "risk":risk
    }

def phase2(api,candidates):
    log("\nPHASE 2: Daily/Weekly/Monthly setup scan...")
    results=[]

    for i,c in enumerate(candidates,1):
        sym=c["symbol"]
        log(f"[{i}/{len(candidates)}] Scanning {sym}...")

        df=candle(api,c["token"])

        if df is None:
            log("  Skipped - candle data unavailable")
            continue

        try:
            x=analyze(sym,df,c["ltp"])
            if x:
                results.append(x)
                log(f"  BUY setup | Score {x['score']} | Vol {x['volx']:.2f}x")
            else:
                log("  No qualifying setup")
        except Exception as e:
            log("  Analysis error: "+str(e))

        time.sleep(STOCK_DELAY)

    results.sort(key=lambda x:(x["score"],x["volx"]),reverse=True)
    return results

def telegram(results):
    now=datetime.now(IST).strftime("%d-%b-%Y %I:%M %p")

    if not results:
        msg=(
            "❌ ANGEL ONE SCANNER\n\n"
            f"Time: {now} IST\n"
            "No BUY setup found.\n\n"
            "Core strategy unchanged."
        )
        send_tg(msg)
        return

    top=results[:10]

    lines=[
        "🚀 ANGEL ONE SWING BUY",
        f"Time: {now} IST",
        f"BUY: {len(results)}",
        ""
    ]

    for i,x in enumerate(top,1):
        lines += [
            f"#{i} {x['symbol']} {x['stars']}  Score:{x['score']}",
            f"LTP: ₹{x['ltp']:.2f}",
            f"SL: ₹{x['sl']:.2f}",
            f"TGT: ₹{x['t1']:.2f} / ₹{x['t2']:.2f}",
            f"Vol: {x['volx']:.2f}x | 52W: ₹{x['high52']:.2f}",
            f"Risk: {x['risk']:.1f}%",
            f"MTF: {'M/W OK' if x['monthly'] else 'W/D OK'}",
            ""
        ]

    lines.append("⚠️ Signal only | No automatic orders")
    send_tg("\n".join(lines))

def main():
    log("="*60)
    log(" ANGEL ONE SWING SCANNER V3.6.3 FINAL")
    log(" MTF + RATE LIMIT SAFE + MASTER FIX")
    log("="*60)
    log("Time: "+datetime.now(IST).strftime("%d-%m-%Y %H:%M:%S IST"))
    log("Auto orders: DISABLED")

    if not all([API_KEY,CLIENT_ID,PASSWORD,TOTP_SECRET]):
        fail("Credentials missing. Check GitHub Secrets.")

    api=login()
    master=load_master()
    candidates=phase1(api,master)

    if not candidates:
        fail("No liquid NSE candidates found.")

    results=phase2(api,candidates)

    log("\n"+"="*60)
    log(f"FINAL BUY SETUPS: {len(results)}")
    log("="*60)

    for i,x in enumerate(results[:10],1):
        log(
            f"#{i} {x['symbol']} | "
            f"Score {x['score']} | "
            f"Vol {x['volx']:.2f}x | "
            f"LTP {x['ltp']:.2f}"
        )

    telegram(results)
    log("\n✅ Scanner completed.")

if __name__=="__main__":
    main()
