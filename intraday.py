import os,time,requests,pyotp,pandas as pd,numpy as np,re,difflib,logging
from datetime import datetime,timedelta
from SmartApi import SmartConnect
import pytz

logging.getLogger("smartapi.smartConnect").setLevel(logging.ERROR)

API_KEY=os.getenv("API_KEY")
CLIENT_ID=os.getenv("CLIENT_ID")
PASSWORD=os.getenv("PASSWORD")
TOTP_SECRET=os.getenv("TOTP_SECRET")

TELEGRAM_BOT_TOKEN=os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID=os.getenv("TELEGRAM_CHAT_ID")

MASTER_URL="https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"

# =========================================================
# CORE SETTINGS — STRATEGY UNCHANGED
# =========================================================
MIN_PRICE=100
MIN_VOL=200000

TOP_UNIVERSE=40
INTRADAY_UNIVERSE=20

TOP_SIGNALS=3
MIN_SCORE=70
MIN_VOLX=1.2

CANDLE_DELAY=1.15
DELAY=.25

MIN_SL_PCT=.004

# OPTIONS
OPTION_MAX=6
OPTION_DAYS=5
OPTION_STRIKES=2
OPTION_DELAY=.25
OPTION_TOP=8

# API RETRY
CANDLE_RETRIES=3
AB1021_BACKOFF=[8,18,35]

# Daily history
ONE_DAY_DAYS=250
FIVE_MIN_DAYS=12
FIFTEEN_MIN_DAYS=25

IST=pytz.timezone("Asia/Kolkata")

OPTION_CACHE={}

# =========================================================
# UNDERLYING FIX
# =========================================================
UNDERLYING_FIX={
    "MOTHERSON":"MOTHERSUMI",
    "M_M":"M&M",
    "M&M":"M&M",
    "BAJAJ-AUTO":"BAJAJAUTO",
    "BAJAJ_AUTO":"BAJAJAUTO"
}

# =========================================================
# IPO / NEW LISTING BLOCK
# =========================================================
IPO_BLOCK={
    "GLASSWALL","SAMBHV","PINELABS","TATATECH",
    "IREDA","MAMA","DOMS","KRN","BLS","BAJAJHFL"
}

# =========================================================
# GLOBAL HISTORICAL API CONTROL
# =========================================================
_LAST_CANDLE_CALL=0.0
AB1021_COUNT=0
CANDLE_COOLDOWN_UNTIL=0.0


def candle_wait():

    global _LAST_CANDLE_CALL

    now=time.monotonic()
    gap=now-_LAST_CANDLE_CALL

    if gap<CANDLE_DELAY:
        time.sleep(CANDLE_DELAY-gap)

    _LAST_CANDLE_CALL=time.monotonic()


def rate_error_response(r):

    if not isinstance(r,dict):
        return False

    code=str(
        r.get("errorcode","")
    ).upper()

    msg=str(
        r.get("message","")
    ).lower()

    return (
        code=="AB1021" or
        "too many requests" in msg or
        "rate limit" in msg or
        "exceeding access rate" in msg
    )


# =========================================================
# TELEGRAM
# =========================================================
def tg(x):

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return

    try:

        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            data={
                "chat_id":TELEGRAM_CHAT_ID,
                "text":x
            },
            timeout=12
        )

    except Exception:
        pass


# =========================================================
# LOGIN
# =========================================================
def login():

    s=SmartConnect(
        api_key=API_KEY
    )

    totp=pyotp.TOTP(
        TOTP_SECRET
    ).now()

    r=s.generateSession(
        CLIENT_ID,
        PASSWORD,
        totp
    )

    if not r or not r.get("status",True):
        raise RuntimeError(
            f"Angel One login failed: {r}"
        )

    return s


# =========================================================
# MASTER
# =========================================================
def master():

    r=requests.get(
        MASTER_URL,
        timeout=30
    )

    r.raise_for_status()

    x=pd.DataFrame(
        r.json()
    )

    x["token"]=x["token"].astype(str)
    x["symbol"]=x["symbol"].astype(str)

    return x


def equity_master(m):

    return m[
        (m["exch_seg"]=="NSE") &
        m["symbol"].str.endswith("-EQ")
    ].copy()


def option_master(m):

    x=m[
        m["exch_seg"]=="NFO"
    ].copy()

    x["expiry_dt"]=pd.to_datetime(
        x.get("expiry"),
        errors="coerce",
        format="mixed"
    )

    x["strike_num"]=pd.to_numeric(
        x.get("strike"),
        errors="coerce"
    )

    return x[
        x["symbol"]
        .str.upper()
        .str.contains(
            "CE|PE",
            regex=True,
            na=False
        )
    ].copy()


def all_underlyings(om):

    u=set()

    for s in om["symbol"].astype(str):

        m=re.match(
            r'^([A-Z0-9&\-\_]+)',
            s.upper()
        )

        if m and len(m.group(1))>=3:
            u.add(m.group(1))

    return list(u)


def best_underlying(base,us):

    base=base.upper()

    if base in UNDERLYING_FIX:
        return UNDERLYING_FIX[base]

    if base in us:
        return base

    c=[
        u for u in us
        if u[:3]==base[:3]
    ]

    z=difflib.get_close_matches(
        base,
        c,
        n=1,
        cutoff=.82
    )

    return z[0] if z else base


# =========================================================
# LIVE QUOTES
# =========================================================
def quotes(s,tokens):

    out=[]

    if not tokens:
        return pd.DataFrame()

    for i in range(
        0,
        len(tokens),
        50
    ):

        batch=tokens[i:i+50]

        try:

            r=s.getMarketData(
                "FULL",
                {
                    "NSE":[
                        str(x)
                        for x in batch
                    ]
                }
            )

            if isinstance(r,dict):

                d=r.get(
                    "data",
                    {}
                )

                if isinstance(d,dict):

                    fetched=d.get(
                        "fetched",
                        []
                    ) or []

                    if fetched:
                        out += fetched

        except Exception as e:

            print(
                f"QUOTE ERROR: {e}",
                flush=True
            )

        # Market-data API safety
        time.sleep(1.2)

    if not out:
        return pd.DataFrame()

    return pd.DataFrame(out)


# =========================================================
# SAFE HISTORICAL CANDLE API
# IMPORTANT:
# Handles AB1021 returned as DICT also.
# =========================================================
def candles(
    s,
    tok,
    days,
    interval,
    exchange="NSE"
):

    global _LAST_CANDLE_CALL
    global AB1021_COUNT
    global CANDLE_COOLDOWN_UNTIL

    # If historical API has entered cooldown
    if time.monotonic()<CANDLE_COOLDOWN_UNTIL:

        remain=int(
            CANDLE_COOLDOWN_UNTIL-
            time.monotonic()
        )

        print(
            f"CANDLE COOLDOWN ACTIVE | "
            f"{remain}s",
            flush=True
        )

        return None

    for attempt in range(
        CANDLE_RETRIES
    ):

        candle_wait()

        try:

            e=datetime.now()

            b=e-timedelta(
                days=days
            )

            payload={
                "exchange":exchange,
                "symboltoken":str(tok),
                "interval":interval,
                "fromdate":b.strftime(
                    "%Y-%m-%d %H:%M"
                ),
                "todate":e.strftime(
                    "%Y-%m-%d %H:%M"
                )
            }

            r=s.getCandleData(
                payload
            )

            # -------------------------------------------------
            # IMPORTANT:
            # SmartAPI can return AB1021 as response dict
            # instead of throwing exception.
            # -------------------------------------------------
            if rate_error_response(r):

                AB1021_COUNT+=1

                wait=AB1021_BACKOFF[
                    min(
                        attempt,
                        len(AB1021_BACKOFF)-1
                    )
                ]

                print(
                    f"AB1021/RATE LIMIT | "
                    f"count={AB1021_COUNT} | "
                    f"attempt={attempt+1} | "
                    f"sleep={wait}s",
                    flush=True
                )

                # After repeated AB1021, stop hammering
                if AB1021_COUNT>=3:

                    CANDLE_COOLDOWN_UNTIL=(
                        time.monotonic()+70
                    )

                    print(
                        "HISTORICAL API COOLDOWN "
                        "70s ACTIVATED",
                        flush=True
                    )

                if attempt>=CANDLE_RETRIES-1:
                    return None

                time.sleep(wait)
                continue

            if not isinstance(r,dict):
                time.sleep(
                    3*(attempt+1)
                )
                continue

            if not r.get("status",True):

                msg=str(
                    r.get("message","")
                ).lower()

                if (
                    "too many" in msg or
                    "rate" in msg or
                    "ab1021" in msg
                ):

                    AB1021_COUNT+=1

                    wait=AB1021_BACKOFF[
                        min(
                            attempt,
                            len(AB1021_BACKOFF)-1
                        )
                    ]

                    print(
                        f"RATE RESPONSE | "
                        f"{msg} | "
                        f"sleep {wait}s",
                        flush=True
                    )

                    if attempt>=CANDLE_RETRIES-1:
                        return None

                    time.sleep(wait)
                    continue

                return None

            data=r.get(
                "data"
            )

            if data is None or len(data)==0:
                return None

            x=pd.DataFrame(
                data,
                columns=[
                    "timestamp",
                    "open",
                    "high",
                    "low",
                    "close",
                    "volume"
                ]
            )

            x["timestamp"]=pd.to_datetime(
                x["timestamp"],
                errors="coerce"
            )

            for c in [
                "open",
                "high",
                "low",
                "close",
                "volume"
            ]:

                x[c]=pd.to_numeric(
                    x[c],
                    errors="coerce"
                )

            x=x.dropna(
                subset=[
                    "timestamp",
                    "close"
                ]
            )

            if x.empty:
                return None

            # Success resets consecutive rate count
            AB1021_COUNT=0

            return (
                x.sort_values(
                    "timestamp"
                )
                .reset_index(drop=True)
            )

        except Exception as e:

            msg=str(e).lower()

            is_rate=(
                "ab1021" in msg or
                "too many requests" in msg or
                "rate" in msg or
                "exceeding access rate" in msg
            )

            is_timeout=(
                "timeout" in msg or
                "timed out" in msg or
                "connection" in msg or
                "connecttimeout" in msg
            )

            if is_rate:

                AB1021_COUNT+=1

                wait=AB1021_BACKOFF[
                    min(
                        attempt,
                        len(AB1021_BACKOFF)-1
                    )
                ]

                print(
                    f"AB1021 EXCEPTION | "
                    f"count={AB1021_COUNT} | "
                    f"attempt={attempt+1} | "
                    f"sleep={wait}s",
                    flush=True
                )

                if AB1021_COUNT>=3:

                    CANDLE_COOLDOWN_UNTIL=(
                        time.monotonic()+70
                    )

                if attempt>=CANDLE_RETRIES-1:
                    return None

                time.sleep(wait)

            elif is_timeout:

                wait=5*(attempt+1)

                print(
                    f"TIMEOUT | "
                    f"attempt={attempt+1} | "
                    f"sleep={wait}s",
                    flush=True
                )

                if attempt>=CANDLE_RETRIES-1:
                    return None

                time.sleep(wait)

            else:

                print(
                    f"CANDLE ERROR | "
                    f"{exchange} {tok} {interval} | "
                    f"{e}",
                    flush=True
                )

                if attempt>=CANDLE_RETRIES-1:
                    return None

                time.sleep(
                    3*(attempt+1)
                )

    return None


# =========================================================
# RSI
# =========================================================
def rsi(s,n=14):

    d=s.diff()

    u=d.clip(
        lower=0
    ).ewm(
        alpha=1/n,
        adjust=False
    ).mean()

    v=(-d.clip(
        upper=0
    )).ewm(
        alpha=1/n,
        adjust=False
    ).mean()

    return (
        100-
        100/(1+
        u/v.replace(
            0,
            np.nan
        ))
    )


# =========================================================
# FEATURES
# =========================================================
def feat(x):

    x=x.copy()

    x["ema9"]=x.close.ewm(
        span=9,
        adjust=False
    ).mean()

    x["ema20"]=x.close.ewm(
        span=20,
        adjust=False
    ).mean()

    x["ema50"]=x.close.ewm(
        span=50,
        adjust=False
    ).mean()

    x["rsi"]=rsi(
        x.close
    )

    tr=pd.concat(
        [
            x.high-x.low,
            (
                x.high-
                x.close.shift()
            ).abs(),
            (
                x.low-
                x.close.shift()
            ).abs()
        ],
        axis=1
    ).max(axis=1)

    x["atr"]=tr.ewm(
        span=14,
        adjust=False
    ).mean()

    x["vavg20"]=x.volume.rolling(
        20
    ).mean()

    x["volx"]=(
        x.volume/
        x.vavg20.replace(
            0,
            np.nan
        )
    )

    x["prev20h"]=(
        x.high.shift(1)
        .rolling(20)
        .max()
    )

    x["prev20l"]=(
        x.low.shift(1)
        .rolling(20)
        .min()
    )

    tp=(
        x.high+
        x.low+
        x.close
    )/3

    day=x.timestamp.dt.date

    x["pv"]=tp*x.volume

    x["cv"]=x.volume.groupby(
        day
    ).cumsum()

    x["cpv"]=x.pv.groupby(
        day
    ).cumsum()

    x["vwap"]=(
        x.cpv/
        x.cv.replace(
            0,
            np.nan
        )
    )

    x["ema20slope"]=(
        x.ema20-
        x.ema20.shift(3)
    )

    return x


# =========================================================
# MARKET BIAS
# =========================================================
def market_bias(s,em):

    vals=[]

    for name,tok,ex in [
        ("NIFTY","99926000","NSE"),
        ("SENSEX","99919000","BSE")
    ]:

        d=candles(
            s,
            tok,
            10,
            "FIFTEEN_MINUTE",
            ex
        )

        if d is None or len(d)<30:
            continue

        d=feat(d)

        x=d.iloc[-2]

        bull=(
            x.close>x.ema20 and
            x.ema20>x.ema50 and
            x.ema20slope>0 and
            x.rsi>=50
        )

        bear=(
            x.close<x.ema20 and
            x.ema20<x.ema50 and
            x.ema20slope<0 and
            x.rsi<=50
        )

        vals.append(
            1 if bull
            else -1 if bear
            else 0
        )

    if not vals:
        return "NEUTRAL",0

    z=sum(vals)

    if z>0:
        return "BULLISH",z

    if z<0:
        return "BEARISH",z

    return "NEUTRAL",z


# =========================================================
# SECTOR
# =========================================================
def sector_name(sym):

    b=sym.replace(
        "-EQ",""
    ).upper()

    groups={

        "BANKING":set(
            "HDFCBANK ICICIBANK SBIN AXISBANK "
            "KOTAKBANK INDUSINDBK BANKBARODA "
            "PNB FEDERALBNK CANBK".split()
        ),

        "IT":set(
            "TCS INFY HCLTECH WIPRO TECHM "
            "LTIM PERSISTENT COFORGE".split()
        ),

        "AUTO":set(
            "MARUTI TATAMOTORS M&M BAJAJ-AUTO "
            "EICHERMOT HEROMOTOCO TVSMOTOR "
            "ASHOKLEY".split()
        ),

        "PHARMA":set(
            "SUNPHARMA DRREDDY CIPLA DIVISLAB "
            "AUROPHARMA LUPIN APOLLOHOSP".split()
        ),

        "METALS":set(
            "TATASTEEL JSWSTEEL HINDALCO "
            "SAIL JINDALSTEL".split()
        ),

        "ENERGY":set(
            "RELIANCE ONGC NTPC POWERGRID "
            "COALINDIA ADANIGREEN ADANIPOWER".split()
        )
    }

    for k,v in groups.items():

        if b in v:
            return k

    return "OTHER"


def sector_strength(res):

    d={}

    for z in res:

        k=z["sector"]

        d.setdefault(
            k,
            []
        ).append(
            z["tech"]
        )

    return {
        k:float(np.mean(v))
        for k,v in d.items()
    }


# =========================================================
# STOCK ANALYSIS
# STRATEGY UNCHANGED
# =========================================================
def analyze(
    sym,
    tok,
    s,
    mbias
):

    clean=sym.replace(
        "-EQ",""
    ).upper()

    # IPO block
    if clean in IPO_BLOCK:
        return None

    # -----------------------------------------------------
    # DAILY
    # -----------------------------------------------------
    d_daily=candles(
        s,
        tok,
        ONE_DAY_DAYS,
        "ONE_DAY",
        "NSE"
    )

    if d_daily is None:
        return None

    if len(d_daily)<120:
        return None

    # -----------------------------------------------------
    # 5 MIN
    # -----------------------------------------------------
    d5=candles(
        s,
        tok,
        FIVE_MIN_DAYS,
        "FIVE_MINUTE",
        "NSE"
    )

    if d5 is None:
        return None

    time.sleep(DELAY)

    # -----------------------------------------------------
    # 15 MIN
    # -----------------------------------------------------
    d15=candles(
        s,
        tok,
        FIFTEEN_MIN_DAYS,
        "FIFTEEN_MINUTE",
        "NSE"
    )

    if d15 is None:
        return None

    if len(d5)<60 or len(d15)<60:
        return None

    d5=feat(d5)
    d15=feat(d15)

    # Last completed candles
    a=d5.iloc[-2]
    b=d15.iloc[-2]

    keys=[
        "close",
        "atr",
        "rsi",
        "volx",
        "vwap",
        "prev20h",
        "prev20l"
    ]

    if any(
        pd.isna(a[k])
        for k in keys
    ):
        return None

    entry=float(
        a.close
    )

    atr=max(
        float(a.atr),
        entry*.003
    )

    # -----------------------------------------------------
    # 15M TREND
    # -----------------------------------------------------
    bull15=(
        b.close>b.ema20>b.ema50 and
        b.ema20slope>0
    )

    bear15=(
        b.close<b.ema20<b.ema50 and
        b.ema20slope<0
    )

    # -----------------------------------------------------
    # 5M TREND
    # -----------------------------------------------------
    bull5=(
        a.close>a.ema9>a.ema20 and
        a.ema20slope>0
    )

    bear5=(
        a.close<a.ema9<a.ema20 and
        a.ema20slope<0
    )

    vol=float(
        a.volx
    )

    above_vwap=(
        a.close>a.vwap
    )

    below_vwap=(
        a.close<a.vwap
    )

    breakout_buy=(
        a.close>a.prev20h and
        vol>=1.5
    )

    breakout_sell=(
        a.close<a.prev20l and
        vol>=1.5
    )

    # -----------------------------------------------------
    # BUY SCORE
    # -----------------------------------------------------
    buy=sum([
        25 if bull15 else 0,
        20 if bull5 else 0,
        15 if above_vwap else 0,
        15 if 55<=a.rsi<=75 else 0,
        15 if vol>=1.5 else 0,
        10 if breakout_buy else 0
    ])

    # -----------------------------------------------------
    # SELL SCORE
    # -----------------------------------------------------
    sell=sum([
        25 if bear15 else 0,
        20 if bear5 else 0,
        15 if below_vwap else 0,
        15 if 25<=a.rsi<=45 else 0,
        15 if vol>=1.5 else 0,
        10 if breakout_sell else 0
    ])

    direction=(
        "BUY"
        if buy>sell
        else "SELL"
    )

    tech=max(
        buy,
        sell
    )

    if tech<60:
        return None

    if vol<MIN_VOLX:
        return None

    sec=sector_name(
        sym
    )

    if (
        sec=="OTHER" and
        tech<85
    ):
        return None

    # -----------------------------------------------------
    # MARKET POINTS
    # -----------------------------------------------------
    if (
        mbias=="BULLISH" and
        direction=="SELL"
    ):

        market_pts=-8

    elif (
        mbias=="BEARISH" and
        direction=="BUY"
    ):

        market_pts=-8

    elif mbias=="NEUTRAL":

        market_pts=0

    else:

        market_pts=8

    # -----------------------------------------------------
    # STOP LOSS
    # -----------------------------------------------------
    if direction=="BUY":

        sw=float(
            d5.low.iloc[-8:-2].min()
        )

        sl=min(
            entry-atr,
            sw-0.15*atr
        )

        if sl>=entry:
            sl=entry-atr

    else:

        sw=float(
            d5.high.iloc[-8:-2].max()
        )

        sl=max(
            entry+atr,
            sw+0.15*atr
        )

        if sl<=entry:
            sl=entry+atr

    risk=abs(
        entry-sl
    )

    if risk/entry<MIN_SL_PCT:
        return None

    # -----------------------------------------------------
    # TARGETS
    # -----------------------------------------------------
    t1=entry+(
        1.5*risk
        if direction=="BUY"
        else -1.5*risk
    )

    t2=entry+(
        2*risk
        if direction=="BUY"
        else -2*risk
    )

    t3=entry+(
        3*risk
        if direction=="BUY"
        else -3*risk
    )

    # -----------------------------------------------------
    # SETUP
    # -----------------------------------------------------
    if (
        breakout_buy
        if direction=="BUY"
        else breakout_sell
    ):

        setup="BREAKOUT"

    elif (
        above_vwap
        if direction=="BUY"
        else below_vwap
    ):

        setup="PULLBACK/VWAP"

    else:

        setup="TREND"

    return {
        "symbol":sym,
        "direction":direction,
        "tech":tech,
        "market_pts":market_pts,
        "entry":entry,
        "sl":sl,
        "t1":t1,
        "t2":t2,
        "t3":t3,
        "rsi":float(a.rsi),
        "volx":vol,
        "setup":setup,
        "sector":sec,
        "live_ltp":entry
    }


# =========================================================
# OPTIONS FLOW
# STRATEGY UNCHANGED
# =========================================================
def option_flow(
    s,
    om,
    z,
    us
):

    sym=z["symbol"].replace(
        "-EQ",""
    ).upper()

    spot=z["live_ltp"]

    u=best_underlying(
        sym,
        us
    )

    x=om[
        om.symbol.str.upper().str.startswith(
            u,
            na=False
        )
    ].copy()

    if x.empty:
        return 0,"NEUTRAL",0,u

    today=pd.Timestamp.now().normalize()

    x=x[
        (x.expiry_dt>=today) &
        (
            x.expiry_dt<=
            today+pd.Timedelta(days=45)
        )
    ].copy()

    if x.empty:
        return 0,"NEUTRAL",0,u

    exp=sorted(
        x.expiry_dt
        .dropna()
        .unique()
    )[:1]

    x=x[
        x.expiry_dt.isin(exp)
    ]

    strikes=sorted(
        x.strike_num
        .dropna()
        .unique()
    )

    if not strikes:
        return 0,"NEUTRAL",0,u

    atm=min(
        strikes,
        key=lambda q:
        abs(float(q)-spot)
    )

    other_gaps=[
        abs(q-atm)
        for q in strikes
        if q!=atm
    ]

    gap=min(
        other_gaps or [1]
    )

    allowed=[
        q for q in strikes
        if abs(q-atm)<=
        gap*OPTION_STRIKES
    ]

    x=x[
        x.strike_num.isin(
            allowed
        )
    ]

    # Nearest strikes first
    x["dist"]=(
        x.strike_num-atm
    ).abs()

    x=x.sort_values(
        ["dist","strike_num"]
    )

    ce=0.0
    pe=0.0

    cm=[]
    pm=[]

    used=0

    for _,c in x.iterrows():

        if used>=OPTION_MAX:
            break

        d=candles(
            s,
            c.token,
            OPTION_DAYS,
            "ONE_DAY",
            "NFO"
        )

        time.sleep(
            OPTION_DELAY
        )

        if d is None or len(d)<3:
            continue

        used+=1

        v=float(
            pd.to_numeric(
                d.volume,
                errors="coerce"
            ).fillna(0).sum()
        )

        first=float(
            d.close.iloc[0]
        )

        last=float(
            d.close.iloc[-1]
        )

        m=(
            last-first
        )/max(
            first,
            .01
        )

        if str(
            c.symbol
        ).upper().endswith("CE"):

            ce+=v
            cm.append(m)

        else:

            pe+=v
            pm.append(m)

    if ce==0 and pe==0:
        return 0,"NEUTRAL",0,u

    ratio=ce/max(
        pe,
        1
    )

    ca=np.mean(cm) if cm else 0
    pa=np.mean(pm) if pm else 0

    if (
        ratio>=1.25 and
        ca>=pa
    ):

        return 8,"BULLISH",ratio,u

    if (
        ratio<=.80 and
        pa>=ca
    ):

        return 8,"BEARISH",ratio,u

    return 0,"NEUTRAL",ratio,u


# =========================================================
# STARS
# =========================================================
def stars(s):

    if s>=90:
        return "★★★★★"

    if s>=80:
        return "★★★★☆"

    if s>=70:
        return "★★★☆☆"

    return "★★☆☆☆"


# =========================================================
# NO SETUP
# =========================================================
def no_setup(
    mbias,
    reason
):

    msg=(
        "⚠️ DIVINE INTRADAY\n\n"
        "NO SETUP\n\n"
        f"MARKET: {mbias}\n"
        f"REASON: {reason}"
    )

    print(
        msg,
        flush=True
    )

    tg(msg)


# =========================================================
# MAIN
# =========================================================
def main():

    global CANDLE_COOLDOWN_UNTIL

    print(
        f"=== DIVINE INTRADAY V8.1 SAFE | "
        f"{datetime.now(IST):%d %b %H:%M:%S IST} ===",
        flush=True
    )

    print(
        "API MODE: RATE-LIMIT SAFE | "
        f"CANDLE GAP {CANDLE_DELAY}s | "
        f"UNIVERSE {TOP_UNIVERSE} | "
        f"INTRADAY {INTRADAY_UNIVERSE}",
        flush=True
    )

    # -----------------------------------------------------
    # LOGIN
    # -----------------------------------------------------
    try:

        s=login()

    except Exception as e:

        msg=(
            "⚠️ DIVINE INTRADAY\n\n"
            "NO SETUP\n\n"
            "REASON: Angel One login failed"
        )

        print(
            f"LOGIN ERROR: {e}",
            flush=True
        )

        tg(msg)
        return

    # -----------------------------------------------------
    # MASTER
    # -----------------------------------------------------
    try:

        m=master()

        em=equity_master(m)

        om=option_master(m)

        us=all_underlyings(
            om
        )

    except Exception as e:

        msg=(
            "⚠️ DIVINE INTRADAY\n\n"
            "NO SETUP\n\n"
            f"REASON: Master data error"
        )

        print(
            f"MASTER ERROR: {e}",
            flush=True
        )

        tg(msg)
        return

    print(
        f"NSE EQUITY: {len(em)} | "
        f"NFO OPTIONS: {len(om)}",
        flush=True
    )

    # -----------------------------------------------------
    # MARKET BIAS
    # -----------------------------------------------------
    mbias,mval=market_bias(
        s,
        em
    )

    print(
        f"MARKET: {mbias} | "
        f"NIFTY/SENSEX score {mval}",
        flush=True
    )

    # -----------------------------------------------------
    # IF HISTORICAL API COOLDOWN
    # -----------------------------------------------------
    if (
        CANDLE_COOLDOWN_UNTIL>
        time.monotonic()
    ):

        no_setup(
            mbias,
            "Angel One historical API AB1021/rate-limit cooldown"
        )

        return

    # -----------------------------------------------------
    # LIVE QUOTES
    # -----------------------------------------------------
    #
    # Strategy/universe logic kept same:
    # TOP_UNIVERSE instruments from master,
    # then live price/volume filtering.
    # -----------------------------------------------------
    top_tokens=em.head(
        TOP_UNIVERSE
    )

    if top_tokens.empty:

        no_setup(
            mbias,
            "No NSE equity universe"
        )

        return

    q=quotes(
        s,
        top_tokens.token.astype(str).tolist()
    )

    if q.empty:

        no_setup(
            mbias,
            "Live quote data unavailable"
        )

        return

    # -----------------------------------------------------
    # NORMALIZE QUOTE DATA
    # -----------------------------------------------------
    q["token"]=q[
        "symbolToken"
    ].astype(str)

    q["ltp"]=pd.to_numeric(
        q.get("ltp"),
        errors="coerce"
    )

    q["tradeVolume"]=pd.to_numeric(
        q.get("tradeVolume"),
        errors="coerce"
    ).fillna(0)

    q=q.dropna(
        subset=["ltp"]
    )

    # -----------------------------------------------------
    # MAP SYMBOL
    # -----------------------------------------------------
    em2=em[
        ["token","symbol"]
    ].copy()

    em2["token"]=em2[
        "token"
    ].astype(str)

    q=q.merge(
        em2,
        on="token",
        how="left"
    )

    q=q.dropna(
        subset=["symbol"]
    )

    # -----------------------------------------------------
    # PRICE + VOLUME FILTER
    # -----------------------------------------------------
    q=q[
        (q.ltp>=MIN_PRICE) &
        (q.tradeVolume>=MIN_VOL)
    ].copy()

    if q.empty:

        no_setup(
            mbias,
            "No stocks passed price/volume filter"
        )

        return

    # Highest live trade volume first
    q=q.sort_values(
        "tradeVolume",
        ascending=False
    )

    q=q.head(
        INTRADAY_UNIVERSE
    )

    print(
        f"INTRADAY CANDIDATES: {len(q)}",
        flush=True
    )

    # -----------------------------------------------------
    # TECHNICAL ANALYSIS
    # -----------------------------------------------------
    results=[]

    for _,row in q.iterrows():

        sym=str(
            row.symbol
        )

        tok=str(
            row.token
        )

        print(
            f"ANALYZE | {sym}",
            flush=True
        )

        try:

            z=analyze(
                sym,
                tok,
                s,
                mbias
            )

            if z is not None:

                # Current live quote
                z["live_ltp"]=float(
                    row.ltp
                )

                results.append(
                    z
                )

        except Exception as e:

            print(
                f"ANALYZE ERROR | "
                f"{sym} | {e}",
                flush=True
            )

        time.sleep(
            DELAY
        )

        # Stop if historical API enters cooldown
        if (
            CANDLE_COOLDOWN_UNTIL>
            time.monotonic()
        ):
            break

    if not results:

        if (
            CANDLE_COOLDOWN_UNTIL>
            time.monotonic()
        ):

            no_setup(
                mbias,
                "Angel One historical API AB1021/rate-limit cooldown"
            )

        else:

            no_setup(
                mbias,
                "No technical setup passed"
            )

        return

    # -----------------------------------------------------
    # TECHNICAL RANK
    # -----------------------------------------------------
    results=sorted(
        results,
        key=lambda z:
        (
            z["tech"]+
            z["market_pts"]
        ),
        reverse=True
    )

    print(
        f"TECH SETUPS: {len(results)}",
        flush=True
    )

    # -----------------------------------------------------
    # OPTIONS ON TOP OPTION_TOP
    # -----------------------------------------------------
    option_candidates=results[
        :OPTION_TOP
    ]

    final=[]

    for z in option_candidates:

        try:

            opt_pts,opt_bias,opt_ratio,u=option_flow(
                s,
                om,
                z,
                us
            )

            z["option_pts"]=opt_pts
            z["option_bias"]=opt_bias
            z["option_ratio"]=opt_ratio
            z["underlying"]=u

            z["score"]=(
                z["tech"]+
                z["market_pts"]+
                z["option_pts"]
            )

            final.append(z)

            print(
                f"OPTION | {z['symbol']} | "
                f"{opt_bias} | "
                f"ratio={opt_ratio:.2f} | "
                f"+{opt_pts}",
                flush=True
            )

        except Exception as e:

            print(
                f"OPTION ERROR | "
                f"{z['symbol']} | {e}",
                flush=True
            )

            z["option_pts"]=0
            z["option_bias"]="NEUTRAL"
            z["option_ratio"]=0
            z["underlying"]=z[
                "symbol"
            ].replace("-EQ","")

            z["score"]=(
                z["tech"]+
                z["market_pts"]
            )

            final.append(z)

        if (
            CANDLE_COOLDOWN_UNTIL>
            time.monotonic()
        ):
            break

    if not final:

        no_setup(
            mbias,
            "Option confirmation unavailable"
        )

        return

    # -----------------------------------------------------
    # FINAL SCORE
    # -----------------------------------------------------
    final=sorted(
        final,
        key=lambda z:
        z["score"],
        reverse=True
    )

    final=[
        z for z in final
        if z["score"]>=MIN_SCORE
    ]

    if not final:

        no_setup(
            mbias,
            "No setup crossed minimum score"
        )

        return

    final=final[
        :TOP_SIGNALS
    ]

    # -----------------------------------------------------
    # SECTOR STRENGTH
    # -----------------------------------------------------
    sec=sector_strength(
        final
    )

    print(
        f"SECTORS: {sec}",
        flush=True
    )

    # -----------------------------------------------------
    # TELEGRAM MESSAGE
    # -----------------------------------------------------
    lines=[]

    lines.append(
        "🚨 DIVINE INTRADAY"
    )

    lines.append(
        ""
    )

    lines.append(
        f"MARKET: {mbias} ({mval:+d})"
    )

    lines.append(
        f"SETUPS: {len(final)}"
    )

    lines.append(
        ""
    )

    for i,z in enumerate(
        final,
        1
    ):

        score=z["score"]

        lines.append(
            f"{i}. {z['symbol']} "
            f"{z['direction']} "
            f"{stars(score)}"
        )

        lines.append(
            f"Score: {score}"
        )

        lines.append(
            f"Setup: {z['setup']}"
        )

        lines.append(
            f"Sector: {z['sector']}"
        )

        lines.append(
            f"LTP: ₹{z['live_ltp']:.2f}"
        )

        lines.append(
            f"Entry: ₹{z['entry']:.2f}"
        )

        lines.append(
            f"SL: ₹{z['sl']:.2f}"
        )

        lines.append(
            f"T1: ₹{z['t1']:.2f}"
        )

        lines.append(
            f"T2: ₹{z['t2']:.2f}"
        )

        lines.append(
            f"T3: ₹{z['t3']:.2f}"
        )

        lines.append(
            f"RSI: {z['rsi']:.1f} | "
            f"VolX: {z['volx']:.2f}x"
        )

        lines.append(
            f"Option: {z['option_bias']} | "
            f"Ratio: {z['option_ratio']:.2f}"
        )

        lines.append(
            ""
        )

    lines.append(
        "⚠️ Paper signal only | No auto-order"
    )

    msg="\n".join(
        lines
    )

    print(
        "\n"+msg,
        flush=True
    )

    tg(msg)


# =========================================================
# RUN
# =========================================================
if __name__=="__main__":

    try:

        main()

    except Exception as e:

        print(
            f"FATAL ERROR: {e}",
            flush=True
        )

        tg(
            "⚠️ DIVINE INTRADAY\n\n"
            "NO SETUP\n\n"
            f"REASON: Scanner error: {e}"
        )
