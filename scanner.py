import os,time,requests,pyotp,pandas as pd
from datetime import datetime,timedelta
from SmartApi import SmartConnect

API_KEY=os.getenv("API_KEY","")
CLIENT_ID=os.getenv("CLIENT_ID","")
PASSWORD=os.getenv("PASSWORD","")
TOTP_SECRET=os.getenv("TOTP_SECRET","")
TG_TOKEN=os.getenv("TELEGRAM_BOT_TOKEN","")
TG_CHAT=os.getenv("TELEGRAM_CHAT_ID","")

MASTER_URL="https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"

TOP=80
MIN_LTP=50
MIN_VOL=100000
MIN_AVG20=50000
MIN_VOLX=2.0
HISTORY=760

STOCK_DELAY=2.5
RATE_WAIT=60
MAX_RETRY=2
BATCH=50

EXCLUDED={"LTIM","TATAMOTORS"}

print("="*60)
print(" ANGEL ONE SWING SCANNER V3.6.1")
print(" RATE LIMIT SAFE + MTF RANKING")
print(" NO AUTOMATIC ORDERS")
print("="*60)
print("Time:",datetime.now().strftime("%d-%m-%Y %H:%M:%S"),"IST")
print()

def tg(msg):
    if not TG_TOKEN or not TG_CHAT:
        print("Telegram credentials missing.")
        return
    try:
        r=requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            data={"chat_id":TG_CHAT,"text":msg},
            timeout=15
        )
        if r.status_code!=200:
            print("Telegram error:",r.text[:200])
    except Exception as e:
        print("Telegram error:",e)

def login():
    print("Logging into Angel One...")
    for attempt in range(1,3):
        try:
            obj=SmartConnect(api_key=API_KEY)
            totp=pyotp.TOTP(TOTP_SECRET).now()
            data=obj.generateSession(CLIENT_ID,PASSWORD,totp)

            if data and data.get("status"):
                print("Angel One login successful.")
                return obj

            print("Login failed:",data)

        except Exception as e:
            print("Login error:",e)

        if attempt<2:
            time.sleep(5)

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
    print("Loading NSE instrument master...")
    try:
        r=requests.get(MASTER_URL,timeout=30)
        r.raise_for_status()
        data=r.json()
    except Exception as e:
        print("Master download error:",e)
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
            continue

    print("NSE stocks:",len(out))
    return out

def bulk_quotes(api,symbols,tokens):
    result={}
    arr=list(symbols)

    for i in range(0,len(arr),BATCH):
        batch=arr[i:i+BATCH]
        print(f"Bulk quote {min(i+BATCH,len(arr))}/{len(arr)}")

        try:
            q=api.getMarketData(
                "FULL",
                {
                    "exchangeTokens":{
                        "NSE":[tokens[s] for s in batch]
                    }
                }
            )

            data=q.get("data",{}) if isinstance(q,dict) else {}
            fetched=data.get("fetched",[]) if isinstance(data,dict) else []

            for x in fetched:
                ts=str(
                    x.get("tradingSymbol","")
                ).replace("-EQ","")

                if ts:
                    result[ts]=x

        except Exception as e:
            print("Bulk quote error:",e)

        time.sleep(1)

    return result

def hist(api,token,symbol):
    end=datetime.now()
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
                        f"   Rate limit. Waiting {RATE_WAIT}s "
                        f"({attempt}/{MAX_RETRY})"
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
                    f"   Rate limit. Waiting {RATE_WAIT}s "
                    f"({attempt}/{MAX_RETRY})"
                )
                time.sleep(RATE_WAIT)
            else:
                print("   Candle error:",e)
                return None

    print("   Skipped:",symbol,"after rate-limit retries.")
    return None

def clean_daily(rows):
    if not rows:
        return None

    try:
        df=pd.DataFrame(
            rows,
            columns=[
                "date",
                "open",
                "high",
                "low",
                "close",
                "volume"
            ]
        )

        for c in ["open","high","low","close","volume"]:
            df[c]=pd.to_numeric(df[c],errors="coerce")

        df["date"]=pd.to_datetime(
            df["date"],
            errors="coerce"
        )

        df=df.dropna()
        df=df.sort_values("date")
        df=df.drop_duplicates("date")

        now=datetime.now()

        if now.hour<15 or (
            now.hour==15 and now.minute<30
        ):
            if len(df):
                if df.iloc[-1]["date"].date()==now.date():
                    df=df.iloc[:-1]

        return df.reset_index(drop=True)

    except Exception as e:
        print("   Clean error:",e)
        return None

def weekly(df):
    w=df.set_index("date").resample("W-FRI").agg({
        "open":"first",
        "high":"max",
        "low":"min",
        "close":"last",
        "volume":"sum"
    }).dropna()

    now=datetime.now()

    if now.weekday()<4 or (
        now.weekday()==4 and
        (now.hour<15 or (now.hour==15 and now.minute<30))
    ):
        if len(w):
            if w.index[-1].date()>=now.date()-timedelta(days=1):
                w=w.iloc[:-1]

    return w

def monthly(df):
    try:
        m=df.set_index("date").resample("ME").agg({
            "open":"first",
            "high":"max",
            "low":"min",
            "close":"last",
            "volume":"sum"
        }).dropna()
    except:
        m=df.set_index("date").resample("M").agg({
            "open":"first",
            "high":"max",
            "low":"min",
            "close":"last",
            "volume":"sum"
        }).dropna()

    now=datetime.now()

    if len(m):
        if m.index[-1].month==now.month:
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

        volx=float(
            volume.iloc[-1]/avg20
        ) if avg20>0 else 0

        if volx<MIN_VOLX:
            return None

        high52=float(high.tail(252).max())
        ltp=float(close.iloc[-1])

        if high52<=0:
            return None

        near_high=ltp>=high52*0.92
        green=bool(
            close.iloc[-1]>df["open"].iloc[-1]
        )

        w=weekly(df)

        if len(w)<40:
            return None

        wclose=w["close"]
        wsma40=wclose.rolling(40).mean()

        weekly_up=bool(
            wclose.iloc[-1]>wsma40.iloc[-1]
        )

        # =====================================================
        # CORE BUY STRATEGY - LOCKED
        # =====================================================
        if not (
            weekly_up and
            near_high and
            green
        ):
            return None

        m=monthly(df)
        monthly_up=False

        if len(m)>=10:
            msma10=m["close"].rolling(10).mean()

            if (
                pd.notna(msma10.iloc[-1]) and
                len(msma10)>=4
            ):
                monthly_up=bool(
                    m["close"].iloc[-1]>msma10.iloc[-1]
                    and
                    msma10.iloc[-1]>=msma10.iloc[-4]
                )

        day_range=float(
            high.iloc[-1]-low.iloc[-1]
        )

        if day_range>0:
            daily_strength=float(
                (close.iloc[-1]-low.iloc[-1])
                /day_range
            )
        else:
            daily_strength=0

        # MTF RANKING
        proximity=max(
            0,
            min(
                35,
                (ltp/high52)*35
            )
        )

        volume_score=max(
            0,
            min(
                25,
                ((volx-2)/8)*25
            )
        )

        monthly_score=15 if monthly_up else 0
        weekly_score=15 if weekly_up else 0

        daily_score=max(
            0,
            min(
                10,
                daily_strength*10
            )
        )

        score=round(
            proximity+
            volume_score+
            monthly_score+
            weekly_score+
            daily_score,
            1
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
        print("   Analyse error:",e)
        return None

def main():

    if not all([
        API_KEY,
        CLIENT_ID,
        PASSWORD,
        TOTP_SECRET
    ]):
        print("❌ ANGEL ONE SCANNER")
        print("Credentials missing.")
        tg(
            "❌ ANGEL ONE SCANNER\n"
            "Credentials missing."
        )
        return

    api=login()

    if not api:
        tg(
            "❌ ANGEL ONE SCANNER\n"
            "Angel One login failed."
        )
        return

    master=load_master()

    if not master:
        tg(
            "❌ ANGEL ONE SCANNER\n"
            "NSE master loading failed."
        )
        return

    symbols=list(master.keys())

    print()
    print("PHASE 1: BULK QUOTES")
    print()

    quotes=bulk_quotes(
        api,
        symbols,
        master
    )

    candidates=[]

    for sym,q in quotes.items():
        try:
            ltp=float(
                q.get("ltp") or
                q.get("lastTradedPrice") or
                0
            )

            vol=float(
                q.get("tradeVolume") or
                q.get("volume") or
                0
            )

            if (
                ltp>=MIN_LTP and
                vol>=MIN_VOL
            ):
                candidates.append({
                    "symbol":sym,
                    "token":master[sym],
                    "ltp":ltp,
                    "volume":vol
                })

        except:
            continue

    candidates.sort(
        key=lambda x:x["volume"],
        reverse=True
    )

    candidates=candidates[:TOP]

    print()
    print(
        "PHASE 1 DONE:",
        len(candidates),
        "candidates /",
        len(symbols),
        "NSE stocks"
    )

    print(
        "TOP:",
        [x["symbol"] for x in candidates]
    )

    print()
    print(
        "PHASE 2: MONTHLY + WEEKLY + DAILY CHECK..."
    )
    print()

    picks=[]
    skipped=0

    for i,x in enumerate(candidates,1):

        sym=x["symbol"]

        print(
            f"[{i}/{len(candidates)}] {sym}"
        )

        rows=hist(
            api,
            x["token"],
            sym
        )

        if not rows:
            skipped+=1
            print("   Skipped - no history")
            continue

        df=clean_daily(rows)

        if df is None:
            skipped+=1
            print("   Skipped - bad history")
            continue

        r=analyse(sym,df)

        if r:
            picks.append(r)

            print(
                f"   BUY | Score {r['score']} | "
                f"Vol {r['volx']:.2f}x | "
                f"52W {r['high52']:.2f}"
            )
        else:
            print("   No qualifying setup")

        time.sleep(STOCK_DELAY)

    # STRONGEST SETUP FIRST
    picks.sort(
        key=lambda x:(
            x["score"],
            x["volx"],
            x["ltp"]/x["high52"]
        ),
        reverse=True
    )

    print()
    print("="*60)
    print("SCAN COMPLETE")
    print("Candidates:",len(candidates))
    print("Qualified BUY:",len(picks))
    print("Skipped:",skipped)
    print("="*60)

    now=datetime.now().strftime(
        "%d %b %I:%M %p"
    )

    if not picks:
        msg=(
            f"🚀 ANGEL ONE BUY - {now} 🚀\n\n"
            f"Total NSE: {len(symbols)}\n"
            f"Candidates: {len(candidates)}\n"
            f"BUY: 0\n\n"
            f"No qualifying swing setup."
        )

        tg(msg)
        print("No BUY signals.")
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

        lines.append(
            f"#{n} {r['symbol']} {r['stars']}"
        )

        lines.append(
            f"LTP: ₹{r['ltp']:.2f}"
        )

        lines.append(
            f"SL: ₹{r['sl']:.2f}"
        )

        lines.append(
            f"TGT: ₹{r['t1']:.2f} / ₹{r['t2']:.2f}"
        )

        lines.append(
            f"Vol: {r['volx']:.2f}x"
        )

        lines.append(
            f"52W: ₹{r['high52']:.2f}"
        )

        lines.append("")

    tg("\n".join(lines))

    print()
    print("Telegram signal sent.")

if __name__=="__main__":
    try:
        main()
    except Exception as e:
        print("FATAL ERROR:",e)
        tg(
            "❌ ANGEL ONE SCANNER ERROR\n"+
            str(e)
                )
