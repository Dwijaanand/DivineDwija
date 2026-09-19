import os,time,pytz,requests,pyotp
import pandas as pd
from datetime import datetime
from SmartApi import SmartConnect

# ================= CONFIG =================
LIQUID_COUNT=100
MIN_PRICE=20.0
VOL_MULT=3.0
MAX_RANGE=18.0
MIN_HISTORY=220
API_DELAY=1.05
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
def get_candles(obj,params):
    for i in range(5):
        try:
            r=obj.getCandleData(params)

            if r and r.get("data"):
                return r["data"]

            print(f"Candle retry {i+1}: {r}")

        except Exception as e:
            print(f"Candle retry {i+1}: {e}")

        time.sleep(1+i*0.7)

    return None

# ================= DATAFRAME =================
def make_df(candles):
    if not candles:
        return None

    df=pd.DataFrame(
        candles,
        columns=[
            "time",
            "open",
            "high",
            "low",
            "close",
            "volume"
        ]
    )

    df["time"]=pd.to_datetime(
        df["time"],
        errors="coerce"
    )

    for c in [
        "open",
        "high",
        "low",
        "close",
        "volume"
    ]:
        df[c]=pd.to_numeric(
            df[c],
            errors="coerce"
        )

    df=df.dropna()
    df=df.sort_values("time")
    df=df.reset_index(drop=True)

    return df

# ================= BULK MARKET DATA =================
def get_bulk_market_data(obj,nse):

    print(
        "Fetching actual NSE liquidity "
        "using bulk market data..."
    )

    result=[]

    rows=nse[
        ["symbol","token"]
    ].copy()

    rows["token"]=rows["token"].astype(str)

    tokens=rows["token"].tolist()

    # Angel One bulk market data:
    # maximum 50 tokens per request
    for start in range(
        0,
        len(tokens),
        50
    ):

        batch=tokens[
            start:start+50
        ]

        batch_set=set(batch)

        print(
            f"Market data batch "
            f"{start+1}-"
            f"{min(start+50,len(tokens))} / "
            f"{len(tokens)}"
        )

        try:

            data=obj.getMarketData(
                "FULL",
                {
                    "NSE":batch
                }
            )

            if data and data.get("data"):

                fetched=data[
                    "data"
                ].get(
                    "fetched",
                    []
                )

                for x in fetched:

                    tok=str(
                        x.get(
                            "symbolToken",
                            x.get(
                                "symboltoken",
                                ""
                            )
                        )
                    )

                    if tok not in batch_set:
                        continue

                    volume=x.get(
                        "tradeVolume",
                        0
                    )

                    try:
                        volume=float(
                            volume or 0
                        )
                    except:
                        volume=0

                    ltp=x.get(
                        "ltp",
                        0
                    )

                    try:
                        ltp=float(
                            ltp or 0
                        )
                    except:
                        ltp=0

                    result.append({
                        "token":tok,
                        "volume":volume,
                        "ltp":ltp
                    })

            else:
                print(
                    f"Market data error: "
                    f"{data}"
                )

        except Exception as e:
            print(
                f"Market data exception: "
                f"{e}"
            )

        if start+50<len(tokens):
            time.sleep(API_DELAY)

    if not result:
        return pd.DataFrame()

    vol=pd.DataFrame(result)

    vol=vol.drop_duplicates(
        "token"
    )

    merged=nse.merge(
        vol,
        on="token",
        how="inner"
    )

    merged=merged[
        (merged["ltp"]>=MIN_PRICE) &
        (merged["volume"]>0)
    ]

    merged=merged.sort_values(
        "volume",
        ascending=False
    )

    merged=merged.head(
        LIQUID_COUNT
    )

    return merged.reset_index(
        drop=True
    )

# ================= WEEKLY CONFIRMATION =================
def weekly_confirmation(df):

    x=df.copy()

    x=x.set_index("time")

    weekly=x.resample(
        "W-FRI"
    ).agg({
        "open":"first",
        "high":"max",
        "low":"min",
        "close":"last",
        "volume":"sum"
    }).dropna()

    if len(weekly)<30:
        return False

    weekly["ema10"]=weekly[
        "close"
    ].ewm(
        span=10,
        adjust=False
    ).mean()

    weekly["ema30"]=weekly[
        "close"
    ].ewm(
        span=30,
        adjust=False
    ).mean()

    w=weekly.iloc[-1]
    p=weekly.iloc[-2]

    return bool(
        w["close"]>w["ema10"] and
        w["ema10"]>w["ema30"] and
        w["ema10"]>p["ema10"] and
        w["close"]>p["close"]
    )

# ================= STOCK SCANNER =================
def scan_stock(obj,sym,tok):

    params={
        "exchange":"NSE",
        "symboltoken":str(tok),
        "interval":"ONE_DAY",
        "fromdate":"2021-03-01 09:15",
        "todate":datetime.now(
            IST
        ).strftime(
            "%Y-%m-%d 15:30"
        )
    }

    candles=get_candles(
        obj,
        params
    )

    if not candles:
        return None

    df=make_df(candles)

    if df is None:
        return None

    if len(df)<MIN_HISTORY:
        return None

    # ================= COMPLETED CANDLE =================
    now=datetime.now(IST)

    if (
        len(df)>0 and
        df["time"].iloc[-1].date()==now.date()
    ):
        df=df.iloc[:-1].copy()

    if len(df)<MIN_HISTORY:
        return None

    close=float(
        df["close"].iloc[-1]
    )

    # ================= PRICE =================
    if close<MIN_PRICE:
        return None

    # ================= 200 DMA =================
    df["dma200"]=df[
        "close"
    ].rolling(200).mean()

    dma200=float(
        df["dma200"].iloc[-1]
    )

    if pd.isna(dma200):
        return None

    if close<=dma200:
        return None

    # ================= ATH BREAK =================
    previous_ath=float(
        df["high"].iloc[:-1].max()
    )

    candle=df.iloc[-1]

    high=float(candle["high"])
    low=float(candle["low"])
    op=float(candle["open"])

    # Today's high must break previous ATH
    if high<previous_ath:
        return None

    # Close must remain near ATH
    if close<previous_ath*0.995:
        return None

    # ================= VOLUME =================
    avg20=float(
        df["volume"].iloc[-21:-1].mean()
    )

    today_vol=float(
        candle["volume"]
    )

    if avg20<=0:
        return None

    vol_x=today_vol/avg20

    if vol_x<VOL_MULT:
        return None

    # ================= TIGHT RANGE =================
    last20=df.tail(20)

    range_high=float(
        last20["high"].max()
    )

    range_low=float(
        last20["low"].min()
    )

    if range_low<=0:
        return None

    range_pct=(
        (range_high-range_low)/
        range_low
    )*100

    if range_pct>MAX_RANGE:
        return None

    # ================= CANDLE STRENGTH =================
    candle_range=high-low

    if candle_range<=0:
        return None

    # Green breakout candle
    if close<=op:
        return None

    close_position=(
        (close-low)/
        candle_range
    )

    # Close in upper 35%
    if close_position<0.65:
        return None

    # ================= WEEKLY =================
    if not weekly_confirmation(df):
        return None

    # ================= SCORE =================
    score=30

    # Volume score
    if vol_x>=5:
        score+=25
    elif vol_x>=4:
        score+=22
    elif vol_x>=3:
        score+=18

    # 200 DMA distance
    dma_distance=(
        close/dma200-1
    )*100

    if dma_distance>=20:
        score+=15
    elif dma_distance>=10:
        score+=12
    elif dma_distance>=5:
        score+=9
    else:
        score+=6

    # Tight range
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

    # ================= ATR =================
    prev_close=df[
        "close"
    ].shift(1)

    tr=pd.concat([
        df["high"]-df["low"],
        (
            df["high"]-
            prev_close
        ).abs(),
        (
            df["low"]-
            prev_close
        ).abs()
    ],axis=1).max(axis=1)

    atr=float(
        tr.rolling(14).mean().iloc[-1]
    )

    if pd.isna(atr) or atr<=0:
        atr=close*0.03

    # ================= SL =================
    swing_low=float(
        df["low"].tail(10).min()
    )

    sl=max(
        swing_low-0.5*atr,
        close*0.85
    )

    risk=close-sl

    if risk<=0:
        return None

    # ================= TARGETS =================
    t1=close+2*risk
    t2=close+3*risk

    # ================= STARS =================
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
        "symbol":sym.replace(
            "-EQ",
            ""
        ),
        "close":close,
        "ath":previous_ath,
        "vol_x":vol_x,
        "range_pct":range_pct,
        "dma200":dma200,
        "score":score,
        "stars":stars,
        "sl":sl,
        "t1":t1,
        "t2":t2
    }

# ================= MAIN =================
def main():

    obj=None

    try:

        # ================= LOGIN =================
        obj=login()

        # ================= NSE MASTER =================
        print(
            "Fetching NSE master..."
        )

        master=pd.read_json(
            MASTER_URL
        )

        nse=master[
            (master["exch_seg"]=="NSE") &
            (
                master["symbol"]
                .astype(str)
                .str.endswith("-EQ")
            )
        ].copy()

        nse["token"]=nse[
            "token"
        ].astype(str)

        nse=nse.drop_duplicates(
            "token"
        )

        print(
            f"NSE Stocks: {len(nse)}"
        )

        # ================= REAL LIQUIDITY =================
        liquid=get_bulk_market_data(
            obj,
            nse
        )

        if liquid.empty:
            raise Exception(
                "Could not fetch bulk NSE market data"
            )

        print(
            f"Actual liquid stocks selected: "
            f"{len(liquid)}"
        )

        print(
            "\nTop liquid stocks:"
        )

        for i,(_,r) in enumerate(
            liquid.head(20).iterrows(),
            1
        ):

            print(
                f"{i}. {r['symbol']} "
                f"Volume={int(r['volume']):,}"
            )

        # ================= ATH SCAN =================
        found=[]

        for i,(_,row) in enumerate(
            liquid.iterrows(),
            1
        ):

            sym=row["symbol"]
            tok=row["token"]

            print(
                f"Scanning "
                f"{i}/{len(liquid)} "
                f"{sym}..."
            )

            try:

                result=scan_stock(
                    obj,
                    sym,
                    tok
                )

                if result:

                    found.append(
                        result
                    )

                    print(
                        f"FOUND {sym} | "
                        f"Score="
                        f"{result['score']} | "
                        f"Vol="
                        f"{result['vol_x']:.2f}x"
                    )

            except Exception as e:

                print(
                    f"{sym} error: {e}"
                )

            time.sleep(
                API_DELAY
            )

        # ================= RANK =================
        found=sorted(
            found,
            key=lambda x:(
                x["score"],
                x["vol_x"],
                -x["range_pct"]
            ),
            reverse=True
        )

        # ================= TELEGRAM =================
        now=datetime.now(
            IST
        ).strftime(
            "%d %b %Y %H:%M"
        )

        msg=(
            f"*DIVINE DWIJA ATH BREAKOUT*\n"
            f"⏰ {now}\n\n"
            f"NSE Stocks: {len(nse)}\n"
            f"Actual Liquid Top: "
            f"{len(liquid)}\n"
            f"BUY Setups: {len(found)}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
        )

        if found:

            for i,r in enumerate(
                found[:10],
                1
            ):

                msg+=(
                    f"\n*#{i} "
                    f"{r['symbol']} "
                    f"{r['stars']}*\n"
                    f"💰 LTP: "
                    f"₹{r['close']:.2f}\n"
                    f"📊 Score: "
                    f"{r['score']}/100\n"
                    f"🔥 Volume: "
                    f"{r['vol_x']:.2f}x\n"
                    f"🚀 ATH: "
                    f"₹{r['ath']:.2f}\n"
                    f"📈 200DMA: "
                    f"₹{r['dma200']:.2f}\n"
                    f"📦 20D Range: "
                    f"{r['range_pct']:.1f}%\n"
                    f"🛡 SL: "
                    f"₹{r['sl']:.2f}\n"
                    f"🎯 T1: "
                    f"₹{r['t1']:.2f}\n"
                    f"🎯 T2: "
                    f"₹{r['t2']:.2f}\n"
                )

        else:

            msg+=(
                "\nNo setup today.\n\n"
                "_Core: ATH Break + 3x Volume + "
                "200 DMA + Weekly Uptrend + "
                "Tight Range_"
            )

        print(
            "\n"+msg
        )

        send_telegram(
            msg
        )

    except Exception as e:

        print(
            f"MAIN ERROR: {e}"
        )

        send_telegram(
            "*DIVINE DWIJA ATH BREAKOUT ERROR*\n\n"
            f"`{str(e)[:500]}`"
        )

    finally:

        if obj:

            try:

                obj.terminateSession(
                    CLIENT_ID
                )

                print(
                    "Session terminated"
                )

            except:
                pass


if __name__=="__main__":
    main()
