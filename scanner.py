import os,time,pytz,requests,pyotp
import pandas as pd
from datetime import datetime
from SmartApi import SmartConnect

# ================= CONFIG =================
LIQUID_COUNT=30
LIQUID_POOL=100
MIN_PRICE=20
VOL_MULT=3.0
MAX_RANGE=18.0
MIN_HISTORY=220
IST=pytz.timezone("Asia/Kolkata")

API_KEY=os.getenv("API_KEY")
CLIENT_ID=os.getenv("CLIENT_ID")
PASSWORD=os.getenv("PASSWORD")
TOTP_SECRET=os.getenv("TOTP_SECRET")
BOT_TOKEN=os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID=os.getenv("TELEGRAM_CHAT_ID")

MASTER_URL="https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"

# ================= TELEGRAM =================
def send_telegram(msg):
    if not BOT_TOKEN or not CHAT_ID:
        print("Telegram credentials missing")
        return
    try:
        url=f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        r=requests.post(
            url,
            json={
                "chat_id":CHAT_ID,
                "text":msg,
                "parse_mode":"Markdown"
            },
            timeout=10
        )
        print(f"Telegram: {r.status_code}")
    except Exception as e:
        print(f"Telegram err: {e}")

# ================= LOGIN =================
def login():
    if not all([API_KEY,CLIENT_ID,PASSWORD,TOTP_SECRET]):
        raise Exception("Angel One credentials missing")

    totp=pyotp.TOTP(TOTP_SECRET).now()
    obj=SmartConnect(api_key=API_KEY)
    session=obj.generateSession(CLIENT_ID,PASSWORD,totp)

    if not session or not session.get("data"):
        raise Exception(f"Login failed: {session}")

    print("Login: SUCCESS")
    return obj

# ================= CANDLE RETRY =================
def get_candles_with_retry(obj,params):
    for i in range(5):
        try:
            resp=obj.getCandleData(params)
            if resp and resp.get("data"):
                return resp["data"]
            if resp:
                print(f"Candle response: {resp.get('message','')}")
        except Exception as e:
            print(f"Retry {i+1}: {e}")

        time.sleep(0.8+i*0.5)

    print(f"Candle error token={params['symboltoken']}")
    return None

# ================= DATAFRAME =================
def candle_df(candles):
    if not candles:
        return None

    df=pd.DataFrame(
        candles,
        columns=["time","open","high","low","close","volume"]
    )

    for c in ["open","high","low","close","volume"]:
        df[c]=pd.to_numeric(df[c],errors="coerce")

    df=df.dropna().reset_index(drop=True)
    return df

# ================= WEEKLY =================
def weekly_confirmation(dfc):
    x=dfc.copy()

    x["time"]=pd.to_datetime(x["time"],errors="coerce")

    x=x.set_index("time")

    weekly=x.resample("W-FRI").agg({
        "open":"first",
        "high":"max",
        "low":"min",
        "close":"last",
        "volume":"sum"
    }).dropna()

    if len(weekly)<30:
        return False,None

    weekly["ema10"]=weekly["close"].ewm(span=10,adjust=False).mean()
    weekly["ema30"]=weekly["close"].ewm(span=30,adjust=False).mean()

    w=weekly.iloc[-1]
    wp=weekly.iloc[-2]

    # Weekly uptrend
    condition=(
        w["close"]>w["ema10"] and
        w["ema10"]>w["ema30"] and
        w["ema10"]>wp["ema10"] and
        w["close"]>wp["close"]
    )

    return bool(condition),weekly

# ================= STOCK ANALYSIS =================
def analyze_stock(obj,sym,tok):

    param={
        "exchange":"NSE",
        "symboltoken":str(tok),
        "interval":"ONE_DAY",
        "fromdate":"2023-01-01 09:15",
        "todate":datetime.now(IST).strftime("%Y-%m-%d 15:30")
    }

    candles=get_candles_with_retry(obj,param)

    if not candles or len(candles)<MIN_HISTORY:
        return None

    df=candle_df(candles)

    if df is None or len(df)<MIN_HISTORY:
        return None

    # Ignore incomplete/latest candle if market candle is not completed
    df["time"]=pd.to_datetime(df["time"],errors="coerce")

    now=datetime.now(IST)

    # If current/latest candle is today's candle and before close,
    # use previous completed candle.
    if len(df):
        last_date=df["time"].iloc[-1].date()
        if last_date==now.date() and now.hour<15:
            df=df.iloc[:-1].copy()

    if len(df)<MIN_HISTORY:
        return None

    # ---------------- BASIC ----------------
    close=float(df["close"].iloc[-1])

    if close<MIN_PRICE:
        return None

    # ---------------- 200 DMA ----------------
    df["dma200"]=df["close"].rolling(200).mean()

    dma200=float(df["dma200"].iloc[-1])

    if pd.isna(dma200):
        return None

    if close<=dma200:
        return None

    # ---------------- ATH ----------------
    ath=float(df["high"].max())

    # Previous ATH excluding current completed candle
    previous_ath=float(df["high"].iloc[:-1].max())

    # Current candle
    today=df.iloc[-1]

    # Actual ATH breakout:
    # current close near/above previous lifetime high
    ath_break=(
        close>=previous_ath*0.995 and
        float(today["high"])>=previous_ath
    )

    if not ath_break:
        return None

    # ---------------- VOLUME ----------------
    if len(df)<21:
        return None

    avg20=float(df["volume"].iloc[-21:-1].mean())
    today_vol=float(today["volume"])

    if avg20<=0:
        return None

    vol_x=today_vol/avg20

    if vol_x<VOL_MULT:
        return None

    # ---------------- TIGHT RANGE ----------------
    last20=df.tail(20)

    range_high=float(last20["high"].max())
    range_low=float(last20["low"].min())

    if range_low<=0:
        return None

    range_pct=(range_high-range_low)/range_low*100

    if range_pct>MAX_RANGE:
        return None

    # ---------------- DAILY CANDLE QUALITY ----------------
    op=float(today["open"])
    hi=float(today["high"])
    lo=float(today["low"])

    candle_range=hi-lo

    if candle_range<=0:
        return None

    green=close>op

    if not green:
        return None

    close_position=(close-lo)/candle_range

    # Close should be in upper part of breakout candle
    if close_position<0.65:
        return None

    # ---------------- WEEKLY ----------------
    weekly_ok,weekly=weekly_confirmation(df)

    if not weekly_ok:
        return None

    # ---------------- SCORE ----------------
    score=0

    # ATH breakout
    score+=30

    # Volume
    if vol_x>=5:
        score+=25
    elif vol_x>=4:
        score+=22
    elif vol_x>=3:
        score+=18

    # 200 DMA distance
    dma_distance=(close/dma200-1)*100

    if dma_distance>=20:
        score+=15
    elif dma_distance>=10:
        score+=12
    elif dma_distance>=5:
        score+=9
    else:
        score+=6

    # Tightness
    if range_pct<=8:
        score+=15
    elif range_pct<=12:
        score+=12
    elif range_pct<=15:
        score+=9
    else:
        score+=6

    # Candle strength
    if close_position>=0.85:
        score+=10
    elif close_position>=0.75:
        score+=8
    else:
        score+=5

    # Weekly confirmation
    score+=5

    # ---------------- SL / TARGETS ----------------
    recent_low=float(df["low"].tail(10).min())

    # ATR14
    prev_close=df["close"].shift(1)

    tr=pd.concat([
        df["high"]-df["low"],
        (df["high"]-prev_close).abs(),
        (df["low"]-prev_close).abs()
    ],axis=1).max(axis=1)

    atr=float(tr.rolling(14).mean().iloc[-1])

    if pd.isna(atr) or atr<=0:
        atr=close*0.03

    sl=max(recent_low-0.5*atr,close*0.85)

    risk=close-sl

    if risk<=0:
        return None

    target1=close+risk*2
    target2=close+risk*3

    # ---------------- STARS ----------------
    if score>=90:
        stars="★★★★★"
    elif score>=82:
        stars="★★★★☆"
    elif score>=74:
        stars="★★★☆☆"
    elif score>=66:
        stars="★★☆☆☆"
    else:
        stars="★☆☆☆☆"

    return {
        "symbol":sym.replace("-EQ",""),
        "close":close,
        "ath":ath,
        "previous_ath":previous_ath,
        "volume":today_vol,
        "avg20":avg20,
        "vol_x":vol_x,
        "range_pct":range_pct,
        "dma200":dma200,
        "dma_distance":dma_distance,
        "score":score,
        "stars":stars,
        "sl":sl,
        "target1":target1,
        "target2":target2
    }

# ================= MAIN =================
def main():

    obj=None

    try:
        obj=login()

        print("Fetching NSE master...")

        try:
            master=pd.read_json(MASTER_URL)

            nse=master[
                (master["exch_seg"]=="NSE") &
                (master["symbol"].astype(str).str.endswith("-EQ"))
            ].copy()

            nse["token"]=nse["token"].astype(str)

            print(f"NSE stocks: {len(nse)}")

        except Exception as e:
            print(f"Master fetch failed: {e}")
            return

        # =================================================
        # STEP 1:
        # First 100 candidates from master
        # =================================================

        pool=nse.head(LIQUID_POOL).copy()

        print(f"Liquidity pool: {len(pool)}")

        # =================================================
        # STEP 2:
        # Actual liquidity selection using 20D volume
        # =================================================

        liquidity=[]

        for _,row in pool.iterrows():

            sym=row["symbol"]
            tok=row["token"]

            print(f"Liquidity: {sym}")

            param={
                "exchange":"NSE",
                "symboltoken":str(tok),
                "interval":"ONE_DAY",
                "fromdate":"2026-07-01 09:15",
                "todate":datetime.now(IST).strftime("%Y-%m-%d 15:30")
            }

            candles=get_candles_with_retry(obj,param)

            if candles and len(candles)>=20:

                d=candle_df(candles)

                if d is not None and len(d)>=20:

                    avg_volume=float(d["volume"].tail(20).mean())

                    last_close=float(d["close"].iloc[-1])

                    if last_close>=MIN_PRICE:
                        liquidity.append({
                            "symbol":sym,
                            "token":tok,
                            "avg_volume":avg_volume
                        })

            time.sleep(0.25)

        liquidity=sorted(
            liquidity,
            key=lambda x:x["avg_volume"],
            reverse=True
        )[:LIQUID_COUNT]

        print(f"Liquid selected: {len(liquidity)}")

        # =================================================
        # STEP 3:
        # ATH BREAKOUT SCAN
        # =================================================

        found=[]

        for item in liquidity:

            sym=item["symbol"]
            tok=item["token"]

            print(f"Scanning {sym}...")

            try:
                result=analyze_stock(obj,sym,tok)

                if result:
                    found.append(result)
                    print(
                        f"FOUND {sym} | "
                        f"Score={result['score']} | "
                        f"Vol={result['vol_x']:.2f}x"
                    )

            except Exception as e:
                print(f"{sym} error: {e}")

            time.sleep(0.4)

        # =================================================
        # STEP 4:
        # STRONGEST SETUP FIRST
        # =================================================

        found=sorted(
            found,
            key=lambda x:(
                x["score"],
                x["vol_x"],
                -x["range_pct"]
            ),
            reverse=True
        )

        # =================================================
        # TELEGRAM
        # =================================================

        now=datetime.now(IST).strftime("%d %b %Y %H:%M")

        msg=(
            f"*CHANDAN ATH BREAKOUT*\n"
            f"⏰ {now}\n\n"
            f"NSE Stocks: {len(nse)}\n"
            f"Liquidity Pool: {LIQUID_POOL}\n"
            f"Liquid Selected: {len(liquidity)}\n"
            f"BUY Setups: {len(found)}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
        )

        if found:

            for i,r in enumerate(found[:10],1):

                msg+=(
                    f"\n*#{i} {r['symbol']} {r['stars']}*\n"
                    f"💰 LTP: ₹{r['close']:.2f}\n"
                    f"📊 Score: {r['score']}/100\n"
                    f"🔥 Volume: {r['vol_x']:.2f}x\n"
                    f"📈 ATH: ₹{r['previous_ath']:.2f}\n"
                    f"📐 200DMA: ₹{r['dma200']:.2f}\n"
                    f"📦 20D Range: {r['range_pct']:.1f}%\n"
                    f"🛡 SL: ₹{r['sl']:.2f}\n"
                    f"🎯 T1: ₹{r['target1']:.2f}\n"
                    f"🎯 T2: ₹{r['target2']:.2f}\n"
                )

        else:

            msg+=(
                "\nNo setup today.\n\n"
                "_Core: ATH Break + 3x Volume + "
                "200 DMA + Weekly Uptrend + Tight Range_"
            )

        print("\n"+msg)

        send_telegram(msg)

    except Exception as e:

        print(f"MAIN ERROR: {e}")

        send_telegram(
            f"*CHANDAN ATH BREAKOUT ERROR*\n\n"
            f"`{str(e)[:500]}`"
        )

    finally:

        if obj:
            try:
                obj.terminateSession(CLIENT_ID)
                print("Session terminated")
            except:
                pass


if __name__=="__main__":
    main()
