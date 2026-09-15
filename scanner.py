import os,time,requests,pyotp,pandas as pd,signal
from datetime import datetime,timedelta
from zoneinfo import ZoneInfo
from SmartApi import SmartConnect

API_KEY=os.getenv("API_KEY","")
CLIENT_ID=os.getenv("CLIENT_ID","")
PASSWORD=os.getenv("PASSWORD","")
TOTP_SECRET=os.getenv("TOTP_SECRET","")
TG_TOKEN=os.getenv("TELEGRAM_BOT_TOKEN","")
TG_CHAT=os.getenv("TELEGRAM_CHAT_ID","")

MASTER_URL="https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
IST=ZoneInfo("Asia/Kolkata")

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

print("="*55,flush=True)
print(" ANGEL ONE SWING SCANNER V3.6.2",flush=True)
print(" LOGIN + RATE LIMIT FIX",flush=True)
print(" MONTHLY + WEEKLY + DAILY RANKING",flush=True)
print(" NO AUTOMATIC ORDERS",flush=True)
print("="*55,flush=True)
print("Time:",datetime.now(IST).strftime("%d-%m-%Y %H:%M:%S IST"),flush=True)

def tg(msg):
    if not TG_TOKEN or not TG_CHAT:
        print("Telegram credentials missing.",flush=True)
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            data={"chat_id":TG_CHAT,"text":msg},
            timeout=15
        )
        print("Telegram sent.",flush=True)
    except Exception as e:
        print("Telegram error:",e,flush=True)

class LoginTimeout(Exception):
    pass

def timeout_handler(signum,frame):
    raise LoginTimeout("Angel login timeout")

def login():
    print("Logging into Angel One...",flush=True)
    if not API_KEY or not CLIENT_ID or not PASSWORD or not TOTP_SECRET:
        print("Credentials missing.",flush=True)
        return None

    old_handler=signal.signal(signal.SIGALRM,timeout_handler)

    for attempt in range(1,3):
        try:
            print("Login attempt",attempt,flush=True)
            signal.alarm(30)
            obj=SmartConnect(api_key=API_KEY)
            totp=pyotp.TOTP(TOTP_SECRET).now()
            print("Sending login request...",flush=True)
            data=obj.generateSession(CLIENT_ID,PASSWORD,totp)
            signal.alarm(0)

            if data and data.get("status"):
                print("Angel One login successful.",flush=True)
                signal.signal(signal.SIGALRM,old_handler)
                return obj

            print("Login failed:",data,flush=True)

        except LoginTimeout:
            signal.alarm(0)
            print("LOGIN TIMEOUT - Angel connection hung.",flush=True)

        except Exception as e:
            signal.alarm(0)
            print("Login error:",e,flush=True)

        if attempt<2:
            print("Retrying login in 5 sec...",flush=True)
            time.sleep(5)

    signal.alarm(0)
    signal.signal(signal.SIGALRM,old_handler)
    return None

def rate_error(x):
    s=str(x).lower()
    return any(k in s for k in [
        "access denied",
        "exceeding access rate",
        "rate limit",
        "too many requests",
        "429"
    ])

def load_master():
    print("Loading NSE master...",flush=True)
    try:
        r=requests.get(MASTER_URL,timeout=30)
        r.raise_for_status()
        data=r.json()
    except Exception as e:
        print("Master error:",e,flush=True)
        return {}

    out={}
    for x in data:
        try:
            if x.get("exch_seg")!="NSE":
                continue
            raw=x.get("symbol","")
            if not raw.endswith("-EQ"):
                continue
            sym=raw[:-3]
            if sym in EXCLUDED:
                continue
            out[sym]=str(x["token"])
        except:
            pass

    print("NSE stocks:",len(out),flush=True)
    return out

def bulk_quotes(api,symbols,tokens):
    result={}
    arr=list(symbols)

    for i in range(0,len(arr),BATCH):
        batch=arr[i:i+BATCH]
        print(
            f"Bulk quote {min(i+BATCH,len(arr))}/{len(arr)}",
            flush=True
        )
        try:
            q=api.getMarketData(
                "FULL",
                {"exchangeTokens":{"NSE":[tokens[s] for s in batch]}}
            )
            data=q.get("data",{}) if isinstance(q,dict) else {}
            fetched=data.get("fetched",[]) if isinstance(data,dict) else []

            for x in fetched:
                sym=str(x.get("tradingSymbol","")).replace("-EQ","")
                if sym:
                    result[sym]=x

        except Exception as e:
            print("Bulk quote error:",e,flush=True)

        time.sleep(1)

    return result

def hist(api,token,symbol):
    end=datetime.now(IST).replace(tzinfo=None)
    start=end-timedelta(days=HISTORY+30)

    params={
        "exchange":"NSE",
        "symboltoken":str(token),
        "interval":"ONE_DAY",
        "fromdate":start.strftime("%Y-%m-%d 09:15"),
        "todate":end.strftime("%Y-%m-%d 15:30")
    }

    for attempt in range(1,MAX_RETRY+1):
        try:
            d=api.getCandleData(params)

            if isinstance(d,dict):
                message=str(d.get("message",""))

                if rate_error(message):
                    print(
                        f"   Rate limit. Waiting {RATE_WAIT}s...",
                        flush=True
                    )
                    time.sleep(RATE_WAIT)
                    continue

                rows=d.get("data")
                if rows:
                    return rows

            return None

        except Exception as e:
            if rate_error(e):
                print(
                    f"   Rate limit. Waiting {RATE_WAIT}s...",
                    flush=True
                )
                time.sleep(RATE_WAIT)
            else:
                print("   Candle error:",e,flush=True)
                return None

    print("   Skipped:",symbol,flush=True)
    return None

def clean_daily(rows):
    if not rows:
        return None
    try:
        df=pd.DataFrame(
            rows,
            columns=["date","open","high","low","close","volume"]
        )
        for c in ["open","high","low","close","volume"]:
            df[c]=pd.to_numeric(df[c],errors="coerce")
        df["date"]=pd.to_datetime(df["date"],errors="coerce")
        df=df.dropna().sort_values("date").drop_duplicates("date")

        now=datetime.now(IST)

        if now.hour<15 or (now.hour==15 and now.minute<30):
            if len(df) and df.iloc[-1]["date"].date()==now.date():
                df=df.iloc[:-1]

        return df.reset_index(drop=True)
    except Exception as e:
        print("   Clean error:",e,flush=True)
        return None

def weekly(df):
    w=df.set_index("date").resample("W-FRI").agg({
        "open":"first","high":"max","low":"min",
        "close":"last","volume":"sum"
    }).dropna()

    now=datetime.now(IST)

    if now.weekday()<4 or (
        now.weekday()==4 and
        (now.hour<15 or (now.hour==15 and now.minute<30))
    ):
        if len(w):
            w=w.iloc[:-1]

    return w

def monthly(df):
    try:
        m=df.set_index("date").resample("ME").agg({
            "open":"first","high":"max","low":"min",
            "close":"last","volume":"sum"
        }).dropna()
    except:
        m=df.set_index("date").resample("M").agg({
            "open":"first","high":"max","low":"min",
            "close":"last","volume":"sum"
        }).dropna()

    now=datetime.now(IST)

    if len(m) and m.index[-1].month==now.month:
        m=m.iloc[:-1]

    return m

def analyse(symbol,df):
    if df is None or len(df)<200:
        return None

    try:
        close=df["close"]
        high=df["high"]
        low=df["low"]
        volume=df["volume"]

        avg20=volume.iloc[-21:-1].mean()
        if avg20<MIN_AVG20:
            return None

        volx=float(volume.iloc[-1]/avg20) if avg20>0 else 0
        if volx<MIN_VOLX:
            return None

        ltp=float(close.iloc[-1])
        high52=float(high.tail(252).max())

        if high52<=0:
            return None

        near_high=ltp>=high52*0.92
        green=bool(close.iloc[-1]>df["open"].iloc[-1])

        w=weekly(df)
        if len(w)<40:
            return None

        wclose=w["close"]
        wsma40=wclose.rolling(40).mean()
        weekly_up=bool(wclose.iloc[-1]>wsma40.iloc[-1])

        # CORE BUY STRATEGY LOCKED
        if not (weekly_up and near_high and green):
            return None

        m=monthly(df)
        monthly_up=False

        if len(m)>=10:
            msma10=m["close"].rolling(10).mean()
            if pd.notna(msma10.iloc[-1]):
                monthly_up=bool(
                    m["close"].iloc[-1]>msma10.iloc[-1]
                    and msma10.iloc[-1]>=msma10.iloc[-4]
                )

        day_range=float(high.iloc[-1]-low.iloc[-1])
        daily_strength=(
            float((close.iloc[-1]-low.iloc[-1])/day_range)
            if day_range>0 else 0
        )

        proximity=max(0,min(35,(ltp/high52)*35))
        volume_score=max(0,min(25,((volx-2)/8)*25))
        monthly_score=15 if monthly_up else 0
        weekly_score=15 if weekly_up else 0
        daily_score=max(0,min(10,daily_strength*10))

        score=round(
            proximity+volume_score+
            monthly_score+weekly_score+
            daily_score,1
        )

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

        sl=float(low.iloc[-1])
        t1=ltp*1.05
        t2=ltp*1.08

        return {
            "symbol":symbol,
            "ltp":ltp,
            "sl":sl,
            "t1":t1,
            "t2":t2,
            "volx":volx,
            "high52":high52,
            "score":score,
            "stars":stars
        }

    except Exception as e:
        print("   Analyse error:",e,flush=True)
        return None

def main():
    if not all([API_KEY,CLIENT_ID,PASSWORD,TOTP_SECRET]):
        print("❌ ANGEL ONE SCANNER",flush=True)
        print("Credentials missing.",flush=True)
        tg("❌ ANGEL ONE SCANNER\nCredentials missing.")
        return

    api=login()

    if not api:
        print("❌ Angel login failed.",flush=True)
        tg("❌ ANGEL ONE SCANNER\nAngel One login failed.")
        return

    master=load_master()

    if not master:
        tg("❌ ANGEL ONE SCANNER\nNSE master loading failed.")
        return

    symbols=list(master.keys())

    print("\nPHASE 1: BULK QUOTES\n",flush=True)

    quotes=bulk_quotes(api,symbols,master)

    candidates=[]

    for sym,q in quotes.items():
        try:
            ltp=float(
                q.get("ltp") or
                q.get("lastTradedPrice") or 0
            )
            vol=float(
                q.get("tradeVolume") or
                q.get("volume") or 0
            )

            if ltp>=MIN_LTP and vol>=MIN_VOL:
                candidates.append({
                    "symbol":sym,
                    "token":master[sym],
                    "volume":vol
                })
        except:
            pass

    candidates.sort(
        key=lambda x:x["volume"],
        reverse=True
    )
    candidates=candidates[:TOP]

    print(
        f"\nPHASE 1 DONE: {len(candidates)} candidates / "
        f"{len(symbols)} NSE stocks",
        flush=True
    )

    print(
        "PHASE 2: MONTHLY + WEEKLY + DAILY CHECK...\n",
        flush=True
    )

    picks=[]
    skipped=0

    for i,x in enumerate(candidates,1):
        sym=x["symbol"]
        print(f"[{i}/{len(candidates)}] {sym}",flush=True)

        rows=hist(api,x["token"],sym)

        if not rows:
            skipped+=1
            print("   Skipped",flush=True)
            continue

        df=clean_daily(rows)

        if df is None:
            skipped+=1
            print("   Bad history",flush=True)
            continue

        r=analyse(sym,df)

        if r:
            picks.append(r)
            print(
                f"   BUY | Score {r['score']} | "
                f"Vol {r['volx']:.2f}x | "
                f"52W {r['high52']:.2f}",
                flush=True
            )
        else:
            print("   No qualifying setup",flush=True)

        time.sleep(STOCK_DELAY)

    picks.sort(
        key=lambda x:(
            x["score"],
            x["volx"],
            x["ltp"]/x["high52"]
        ),
        reverse=True
    )

    print("\n"+"="*55,flush=True)
    print("SCAN COMPLETE",flush=True)
    print("Candidates:",len(candidates),flush=True)
    print("Qualified BUY:",len(picks),flush=True)
    print("Skipped:",skipped,flush=True)
    print("="*55,flush=True)

    now=datetime.now(IST).strftime("%d %b %I:%M %p")

    if not picks:
        tg(
            f"🚀 ANGEL ONE BUY - {now} 🚀\n\n"
            f"Total NSE: {len(symbols)}\n"
            f"Candidates: {len(candidates)}\n"
            f"BUY: 0\n\n"
            f"No qualifying swing setup."
        )
        return

    lines=[
        f"🚀 ANGEL ONE BUY - {now} 🚀",
        "",
        f"Total NSE: {len(symbols)}",
        f"Candidates: {len(candidates)}",
        f"BUY: {len(picks)}",
        ""
    ]

    for n,r in enumerate(picks,1):
        lines+= [
            f"#{n} {r['symbol']} {r['stars']}",
            f"LTP: ₹{r['ltp']:.2f}",
            f"SL: ₹{r['sl']:.2f}",
            f"TGT: ₹{r['t1']:.2f} / ₹{r['t2']:.2f}",
            f"Vol: {r['volx']:.2f}x",
            f"52W: ₹{r['high52']:.2f}",
            ""
        ]

    tg("\n".join(lines))
    print("Telegram signal sent.",flush=True)

if __name__=="__main__":
    try:
        main()
    except Exception as e:
        print("FATAL ERROR:",e,flush=True)
        tg(f"❌ ANGEL ONE SCANNER ERROR\n{e}")
