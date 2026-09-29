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
# CORE SETTINGS
# =========================================================

MIN_PRICE=100
MIN_VOL=200000

# Live quote universe
TOP_UNIVERSE=40
INTRADAY_UNIVERSE=20

TOP_SIGNALS=3
MIN_SCORE=70
MIN_VOLX=1.2

# Historical API throttle
CANDLE_DELAY=1.15
CANDLE_RETRIES=3

# Retry backoff
AB1021_BACKOFF=[8,18,35]

# Historical API emergency cooldown
HIST_COOLDOWN=120

# Normal processing delay
DELAY=.25

# Minimum SL distance
MIN_SL_PCT=.004

# Options
OPTION_MAX=6
OPTION_DAYS=5
OPTION_STRIKES=2
OPTION_DELAY=.25
OPTION_TOP=8

# Live quote API
QUOTE_BATCH=50
QUOTE_DELAY=1.2

# Daily history
DAILY_DAYS=250

IST=pytz.timezone("Asia/Kolkata")

# =========================================================
# GLOBALS
# =========================================================

_LAST_CANDLE_CALL=0.0
_CONSECUTIVE_AB1021=0
_HIST_BLOCK_UNTIL=0.0

CANDLE_CACHE={}
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
    "GLASSWALL",
    "SAMBHV",
    "PINELABS",
    "TATATECH",
    "IREDA",
    "MAMA",
    "DOMS",
    "KRN",
    "BLS",
    "BAJAJHFL"
}

# =========================================================
# TELEGRAM
# =========================================================

def tg(x):

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("TELEGRAM SECRETS MISSING",flush=True)
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

    except Exception as e:

        print(
            f"Telegram error: {e}",
            flush=True
        )

# =========================================================
# LOGIN
# =========================================================

def login():

    if not all([
        API_KEY,
        CLIENT_ID,
        PASSWORD,
        TOTP_SECRET
    ]):
        raise RuntimeError(
            "Missing Angel One GitHub Secrets"
        )

    print(
        "LOGIN: starting...",
        flush=True
    )

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

    if not r or r.get("status") is False:

        raise RuntimeError(
            f"LOGIN FAILED: {r}"
        )

    print(
        "LOGIN: SUCCESS",
        flush=True
    )

    return s

# =========================================================
# MASTER
# =========================================================

def master():

    print(
        "MASTER: downloading...",
        flush=True
    )

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

    print(
        f"MASTER: {len(x)} instruments",
        flush=True
    )

    return x


def equity_master(m):

    x=m[
        (m["exch_seg"]=="NSE") &
        m["symbol"].str.endswith("-EQ")
    ].copy()

    return x


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

    x=x[
        x["symbol"]
        .str.upper()
        .str.contains(
            "CE|PE",
            regex=True,
            na=False
        )
    ].copy()

    return x

# =========================================================
# OPTION UNDERLYINGS
# =========================================================

def all_underlyings(om):

    u=set()

    for s in om["symbol"].astype(str):

        m=re.match(
            r'^([A-Z0-9&\-\_]+)',
            s.upper()
        )

        if m:

            z=m.group(1)

            if len(z)>=3:
                u.add(z)

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

    tokens=[
        str(x)
        for x in tokens
    ]

    print(
        f"QUOTE: scanning {len(tokens)} NSE tokens",
        flush=True
    )

    for i in range(
        0,
        len(tokens),
        QUOTE_BATCH
    ):

        batch=tokens[
            i:i+QUOTE_BATCH
        ]

        try:

            r=s.getMarketData(
                "FULL",
                {
                    "NSE":batch
                }
            )

            if isinstance(r,dict):

                if r.get("status") is False:

                    print(
                        f"QUOTE ERROR: {r}",
                        flush=True
                    )

                d=r.get(
                    "data",
                    {}
                )

                if isinstance(d,dict):

                    fetched=d.get(
                        "fetched",
                        []
                    ) or []

                    out.extend(
                        fetched
                    )

        except Exception as e:

            print(
                f"QUOTE ERROR: {e}",
                flush=True
            )

        # Conservative spacing
        time.sleep(
            QUOTE_DELAY
        )

    q=pd.DataFrame(out)

    print(
        f"QUOTE: received {len(q)} rows",
        flush=True
    )

    return q

# =========================================================
# GLOBAL HISTORICAL THROTTLE
# =========================================================

def candle_wait():

    global _LAST_CANDLE_CALL

    now=time.monotonic()

    gap=now-_LAST_CANDLE_CALL

    if gap<CANDLE_DELAY:

        time.sleep(
            CANDLE_DELAY-gap
        )

    _LAST_CANDLE_CALL=time.monotonic()

# =========================================================
# SAFE HISTORICAL CANDLE API
# =========================================================

def candles(
    s,
    tok,
    days,
    interval,
    exchange="NSE"
):

    global _CONSECUTIVE_AB1021
    global _HIST_BLOCK_UNTIL

    cache_key=(
        str(exchange),
        str(tok),
        str(interval),
        int(days)
    )

    # Cache
    if cache_key in CANDLE_CACHE:

        return CANDLE_CACHE[
            cache_key
        ].copy()

    # Emergency block
    if time.monotonic() < _HIST_BLOCK_UNTIL:

        return None

    for attempt in range(
        CANDLE_RETRIES
    ):

        candle_wait()

        try:

            now=datetime.now()

            begin=now-timedelta(
                days=days
            )

            payload={
                "exchange":exchange,
                "symboltoken":str(tok),
                "interval":interval,
                "fromdate":begin.strftime(
                    "%Y-%m-%d %H:%M"
                ),
                "todate":now.strftime(
                    "%Y-%m-%d %H:%M"
                )
            }

            r=s.getCandleData(
                payload
            )

            # -------------------------------------------------
            # IMPORTANT:
            # SmartAPI can return AB1021 as a normal dict
            # instead of raising an exception.
            # -------------------------------------------------

            if isinstance(r,dict):

                code=str(
                    r.get(
                        "errorcode",
                        ""
                    )
                ).upper()

                msg=str(
                    r.get(
                        "message",
                        ""
                    )
                ).lower()

                status=r.get(
                    "status"
                )

                is_rate=(
                    code=="AB1021" or
                    "too many requests" in msg or
                    "exceeding access rate" in msg or
                    "rate limit" in msg
                )

                if is_rate:

                    _CONSECUTIVE_AB1021+=1

                    print(
                        f"AB1021 | "
                        f"{exchange} {tok} {interval} | "
                        f"attempt {attempt+1} | "
                        f"count {_CONSECUTIVE_AB1021}",
                        flush=True
                    )

                    # Repeated rate-limit response
                    if _CONSECUTIVE_AB1021>=3:

                        _HIST_BLOCK_UNTIL=(
                            time.monotonic()+
                            HIST_COOLDOWN
                        )

                        print(
                            f"HISTORICAL API PAUSED "
                            f"FOR {HIST_COOLDOWN}s",
                            flush=True
                        )

                        return None

                    if attempt<CANDLE_RETRIES-1:

                        wait=AB1021_BACKOFF[
                            min(
                                attempt,
                                len(
                                    AB1021_BACKOFF
                                )-1
                            )
                        ]

                        time.sleep(
                            wait
                        )

                        continue

                    return None

                data=r.get(
                    "data"
                )

                if (
                    status is False or
                    data is None
                ):

                    return None

            else:

                data=None

            # -------------------------------------------------
            # VALID DATA
            # -------------------------------------------------

            if data is None:
                continue

            if len(data)==0:
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

            x=(
                x.sort_values(
                    "timestamp"
                )
                .reset_index(
                    drop=True
                )
            )

            # Successful request resets consecutive AB1021
            _CONSECUTIVE_AB1021=0

            CANDLE_CACHE[
                cache_key
            ]=x.copy()

            return x

        except Exception as e:

            msg=str(e).lower()

            is_rate=(
                "ab1021" in msg or
                "too many requests" in msg or
                "exceeding access rate" in msg or
                "rate limit" in msg
            )

            is_timeout=(
                "timeout" in msg or
                "timed out" in msg or
                "connection" in msg or
                "connecttimeout" in msg
            )

            if is_rate:

                _CONSECUTIVE_AB1021+=1

                print(
                    f"AB1021 EXCEPTION | "
                    f"attempt {attempt+1} | "
                    f"count {_CONSECUTIVE_AB1021}",
                    flush=True
                )

                if _CONSECUTIVE_AB1021>=3:

                    _HIST_BLOCK_UNTIL=(
                        time.monotonic()+
                        HIST_COOLDOWN
                    )

                    print(
                        f"HISTORICAL API PAUSED "
                        f"FOR {HIST_COOLDOWN}s",
                        flush=True
                    )

                    return None

                if attempt<CANDLE_RETRIES-1:

                    wait=AB1021_BACKOFF[
                        min(
                            attempt,
                            len(
                                AB1021_BACKOFF
                            )-1
                        )
                    ]

                    time.sleep(
                        wait
                    )

                    continue

            elif is_timeout:

                wait=5*(attempt+1)

                print(
                    f"TIMEOUT | "
                    f"attempt {attempt+1} | "
                    f"sleep {wait}s",
                    flush=True
                )

                time.sleep(
                    wait
                )

                continue

            else:

                print(
                    f"CANDLE ERROR | "
                    f"{exchange} {tok} {interval} | "
                    f"{e}",
                    flush=True
                )

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

    return 100-100/(
        1+
        u/v.replace(
            0,
            np.nan
        )
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
    ).max(
        axis=1
    )

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

def market_bias(s):

    vals=[]

    for name,tok,ex in [
        (
            "NIFTY",
            "99926000",
            "NSE"
        ),
        (
            "SENSEX",
            "99919000",
            "BSE"
        )
    ]:

        d=candles(
            s,
            tok,
            10,
            "FIFTEEN_MINUTE",
            ex
        )

        if d is None:
            continue

        if len(d)<30:
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

# =========================================================
# STOCK ANALYSIS
# =========================================================

def analyze(
    sym,
    tok,
    live_ltp,
    live_vol,
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
        DAILY_DAYS,
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
        12,
        "FIVE_MINUTE",
        "NSE"
    )

    if d5 is None:
        return None

    time.sleep(
        DELAY
    )

    # -----------------------------------------------------
    # 15 MIN
    # -----------------------------------------------------

    d15=candles(
        s,
        tok,
        25,
        "FIFTEEN_MINUTE",
        "NSE"
    )

    if d15 is None:
        return None

    if len(d5)<60:
        return None

    if len(d15)<60:
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
        live_ltp
        if live_ltp>0
        else a.close
    )

    # Use candle ATR
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
    # BUY
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
    # SELL
    # -----------------------------------------------------

    sell=sum([
        25 if bear15 else 0,
        20 if bear5 else 0,
        15 if below_vwap else 0,
        15 if 25<=a.rsi<=45 else 0,
        15 if vol>=1.5 else 0,
        10 if breakout_sell else 0
    ])

    if buy>sell:

        direction="BUY"

    elif sell>buy:

        direction="SELL"

    else:

        return None

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
            sw-.15*atr
        )

        if sl>=entry:
            sl=entry-atr

    else:

        sw=float(
            d5.high.iloc[-8:-2].max()
        )

        sl=max(
            entry+atr,
            sw+.15*atr
        )

        if sl<=entry:
            sl=entry+atr

    risk=abs(
        entry-sl
    )

    if (
        risk/entry
        <
        MIN_SL_PCT
    ):

        return None

    # -----------------------------------------------------
    # TARGETS
    # -----------------------------------------------------

    if direction=="BUY":

        t1=entry+1.5*risk
        t2=entry+2*risk
        t3=entry+3*risk

    else:

        t1=entry-1.5*risk
        t2=entry-2*risk
        t3=entry-3*risk

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

        "live_ltp":entry,
        "live_vol":live_vol
    }

# =========================================================
# OPTIONS FLOW
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

    spot=float(
        z["live_ltp"]
    )

    u=best_underlying(
        sym,
        us
    )

    x=om[
        om.symbol
        .str.upper()
        .str.startswith(
            u,
            na=False
        )
    ].copy()

    if x.empty:

        return (
            0,
            "NEUTRAL",
            0,
            u
        )

    today=pd.Timestamp.now().normalize()

    x=x[
        (x.expiry_dt>=today) &
        (
            x.expiry_dt<=
            today+
            pd.Timedelta(
                days=45
            )
        )
    ].copy()

    if x.empty:

        return (
            0,
            "NEUTRAL",
            0,
            u
        )

    exp=sorted(
        x.expiry_dt
        .dropna()
        .unique()
    )

    if not exp:

        return (
            0,
            "NEUTRAL",
            0,
            u
        )

    x=x[
        x.expiry_dt==exp[0]
    ].copy()

    strikes=sorted(
        x.strike_num
        .dropna()
        .unique()
    )

    if not strikes:

        return (
            0,
            "NEUTRAL",
            0,
            u
        )

    atm=min(
        strikes,
        key=lambda q:
        abs(float(q)-spot)
    )

    gaps=[
        abs(q-atm)
        for q in strikes
        if q!=atm
    ]

    gap=min(
        gaps or [1]
    )

    allowed=[
        q for q in strikes
        if abs(q-atm)
        <=
        gap*OPTION_STRIKES
    ]

    x=x[
        x.strike_num.isin(
            allowed
        )
    ].copy()

    # Closest contracts first
    x["dist"]=(
        x.strike_num-atm
    ).abs()

    x=x.sort_values(
        "dist"
    )

    ce=0.0
    pe=0.0

    cm=[]
    pm=[]

    used=0

    for _,c in x.iterrows():

        if used>=OPTION_MAX:
            break

        key=(
            str(c.token),
            OPTION_DAYS
        )

        if key in OPTION_CACHE:

            d=OPTION_CACHE[key]

        else:

            d=candles(
                s,
                c.token,
                OPTION_DAYS,
                "ONE_DAY",
                "NFO"
            )

            if d is not None:

                OPTION_CACHE[key]=d

            time.sleep(
                OPTION_DELAY
            )

        if d is None:
            continue

        if len(d)<3:
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

        cs=str(
            c.symbol
        ).upper()

        if cs.endswith("CE"):

            ce+=v
            cm.append(m)

        elif cs.endswith("PE"):

            pe+=v
            pm.append(m)

    if ce==0 and pe==0:

        return (
            0,
            "NEUTRAL",
            0,
            u
        )

    ratio=ce/max(
        pe,
        1
    )

    ca=(
        float(np.mean(cm))
        if cm else 0
    )

    pa=(
        float(np.mean(pm))
        if pm else 0
    )

    if (
        ratio>=1.25 and
        ca>=pa
    ):

        return (
            8,
            "BULLISH",
            ratio,
            u
        )

    if (
        ratio<=.80 and
        pa>=ca
    ):

        return (
            8,
            "BEARISH",
            ratio,
            u
        )

    return (
        0,
        "NEUTRAL",
        ratio,
        u
    )

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
# MAIN
# =========================================================

def main():

    start=time.time()

    print(
        "\n"
        "========================================\n"
        "      DIVINE INTRADAY V8.2 SAFE\n"
        f"      {datetime.now(IST):%d %b %Y %H:%M:%S IST}\n"
        "========================================",
        flush=True
    )

    print(
        f"CANDLE GAP: {CANDLE_DELAY}s",
        flush=True
    )

    print(
        f"TOP UNIVERSE: {TOP_UNIVERSE}",
        flush=True
    )

    print(
        f"INTRADAY UNIVERSE: {INTRADAY_UNIVERSE}",
        flush=True
    )

    # =====================================================
    # LOGIN
    # =====================================================

    try:

        s=login()

    except Exception as e:

        print(
            f"LOGIN FAILED: {e}",
            flush=True
        )

        tg(
            "⚠️ DIVINE INTRADAY\n"
            "LOGIN FAILED\n"
            f"{e}"
        )

        return

    # =====================================================
    # MASTER
    # =====================================================

    try:

        m=master()

        em=equity_master(m)

        om=option_master(m)

        us=all_underlyings(
            om
        )

    except Exception as e:

        print(
            f"MASTER FAILED: {e}",
            flush=True
        )

        tg(
            "⚠️ DIVINE INTRADAY\n"
            "MASTER FAILED"
        )

        return

    print(
        f"NSE EQUITY: {len(em)}",
        flush=True
    )

    print(
        f"NFO OPTIONS: {len(om)}",
        flush=True
    )

    # =====================================================
    # MARKET BIAS
    # =====================================================

    mbias,mval=market_bias(
        s
    )

    print(
        f"MARKET: {mbias} | "
        f"NIFTY/SENSEX SCORE {mval}",
        flush=True
    )

    # =====================================================
    # LIVE QUOTES
    # =====================================================

    q=quotes(
        s,
        em.token.tolist()
    )

    if q.empty:

        print(
            "NO LIVE QUOTES RECEIVED",
            flush=True
        )

        tg(
            "⚠️ DIVINE INTRADAY\n"
            "NO LIVE QUOTES RECEIVED"
        )

        return

    # Normalize token
    if "symbolToken" in q.columns:

        q["symbolToken"]=
            q["symbolToken"].astype(str)

    elif "symboltoken" in q.columns:

        q["symbolToken"]=
            q["symboltoken"].astype(str)

    else:

        q["symbolToken"]=
            q.get(
                "token",
                ""
            ).astype(str)

    # Normalize LTP
    if "ltp" not in q.columns:

        q["ltp"]=0

    if "tradeVolume" not in q.columns:

        q["tradeVolume"]=0

    q["ltp"]=pd.to_numeric(
        q["ltp"],
        errors="coerce"
    ).fillna(0)

    q["tradeVolume"]=pd.to_numeric(
        q["tradeVolume"],
        errors="coerce"
    ).fillna(0)

    # -----------------------------------------------------
    # Join symbol master
    # -----------------------------------------------------

    em2=em[
        [
            "token",
            "symbol"
        ]
    ].copy()

    em2["token"]=
        em2["token"].astype(str)

    q=q.merge(
        em2,
        left_on="symbolToken",
        right_on="token",
        how="inner"
    )

    # -----------------------------------------------------
    # Price / volume filter
    # -----------------------------------------------------

    q=q[
        (q.ltp>=MIN_PRICE) &
        (q.tradeVolume>=MIN_VOL)
    ].copy()

    # IPO block
    q["clean"]=(
        q.symbol
        .str.replace(
            "-EQ",
            "",
            regex=False
        )
        .str.upper()
    )

    q=q[
        ~q.clean.isin(
            IPO_BLOCK
        )
    ].copy()

    # Highest live volume first
    q=q.sort_values(
        "tradeVolume",
        ascending=False
    )

    q=q.head(
        TOP_UNIVERSE
    ).copy()

    print(
        f"HIGH-VOLUME FILTER: {len(q)} stocks",
        flush=True
    )

    if q.empty:

        tg(
            "DIVINE INTRADAY\n"
            f"MARKET: {mbias}\n\n"
            "NO LIQUID STOCK FOUND"
        )

        return

    # =====================================================
    # DAILY + INTRADAY ANALYSIS
    # =====================================================

    candidates=[]

    scan=q.head(
        TOP_UNIVERSE
    )

    print(
        "STARTING STOCK ANALYSIS...",
        flush=True
    )

    for n,(_,row) in enumerate(
        scan.iterrows(),
        1
    ):

        sym=str(
            row.symbol
        )

        tok=str(
            row.token
        )

        ltp=float(
            row.ltp
        )

        tv=float(
            row.tradeVolume
        )

        print(
            f"[{n}/{len(scan)}] "
            f"{sym} | LTP {ltp:.2f}",
            flush=True
        )

        try:

            z=analyze(
                sym,
                tok,
                ltp,
                tv,
                s,
                mbias
            )

            if z:

                candidates.append(
                    z
                )

                print(
                    f"  -> {z['direction']} "
                    f"TECH {z['tech']} "
                    f"{z['setup']}",
                    flush=True
                )

        except Exception as e:

            print(
                f"  ANALYSIS ERROR: {e}",
                flush=True
            )

        time.sleep(
            DELAY
        )

        # If historical API gets blocked,
        # don't keep hammering it.
        if time.monotonic() < _HIST_BLOCK_UNTIL:

            print(
                "Historical API cooldown active. "
                "Stopping stock scan.",
                flush=True
            )

            break

    print(
        f"TECH CANDIDATES: {len(candidates)}",
        flush=True
    )

    if not candidates:

        reason=(
            "No stock passed "
            "Daily + 5M + 15M + volume filters"
        )

        if _CONSECUTIVE_AB1021>=3:

            reason=(
                "Angel One historical API "
                "AB1021/rate-limit cooldown"
            )

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

        return

    # =====================================================
    # TOP 8 BEFORE OPTIONS
    # =====================================================

    candidates=sorted(
        candidates,
        key=lambda x:(
            x["tech"]+
            x["market_pts"]
        ),
        reverse=True
    )

    option_candidates=candidates[
        :OPTION_TOP
    ]

    print(
        f"OPTION FLOW: TOP {len(option_candidates)}",
        flush=True
    )

    # =====================================================
    # OPTIONS CONFIRMATION
    # =====================================================

    final=[]

    for z in option_candidates:

        print(
            f"OPTION FLOW -> {z['symbol']}",
            flush=True
        )

        try:

            op,odir,ratio,u=option_flow(
                s,
                om,
                z,
                us
            )

            z["option_pts"]=op
            z["option_dir"]=odir
            z["option_ratio"]=ratio
            z["underlying"]=u

            # Option conflict penalty
            if (
                odir=="BEARISH" and
                z["direction"]=="BUY"
            ):

                z["option_conflict"]=-5

            elif (
                odir=="BULLISH" and
                z["direction"]=="SELL"
            ):

                z["option_conflict"]=-5

            else:

                z["option_conflict"]=0

            total=(
                z["tech"]+
                z["market_pts"]+
                z["option_pts"]+
                z["option_conflict"]
            )

            z["score"]=total

            final.append(
                z
            )

        except Exception as e:

            print(
                f"OPTION ERROR {z['symbol']}: {e}",
                flush=True
            )

            z["option_pts"]=0
            z["option_dir"]="NEUTRAL"
            z["option_ratio"]=0
            z["option_conflict"]=0

            z["score"]=(
                z["tech"]+
                z["market_pts"]
            )

            final.append(
                z
            )

        time.sleep(
            DELAY
        )

    # =====================================================
    # FINAL TOP 3
    # =====================================================

    final=sorted(
        final,
        key=lambda x:x["score"],
        reverse=True
    )

    final=[
        x for x in final
        if x["score"]>=MIN_SCORE
    ]

    final=final[
        :TOP_SIGNALS
    ]

    # =====================================================
    # TELEGRAM
    # =====================================================

    if not final:

        msg=(
            "DIVINE INTRADAY\n\n"
            "NO SETUP\n\n"
            f"MARKET: {mbias}\n"
            f"REASON: Score below {MIN_SCORE}"
        )

        print(
            msg,
            flush=True
        )

        tg(msg)

        return

    lines=[]

    lines.append(
        "🔥 DIVINE INTRADAY"
    )

    lines.append(
        f"MARKET: {mbias} ({mval:+d})"
    )

    lines.append("")

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
            f"Score: {score:.0f} | "
            f"Tech: {z['tech']}"
        )

        lines.append(
            f"LTP: ₹{z['live_ltp']:.2f}"
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
            f"Setup: {z['setup']}"
        )

        lines.append(
            f"Sector: {z['sector']}"
        )

        lines.append(
            f"Options: {z['option_dir']} "
            f"| Ratio {z['option_ratio']:.2f}"
        )

        lines.append(
            ""
        )

    lines.append(
        "⚠️ ALERT ONLY — NO AUTO ORDER"
    )

    msg="\n".join(
        lines
    )

    print(
        "\n"+msg,
        flush=True
    )

    tg(msg)

    elapsed=(
        time.time()-start
    )

    print(
        f"\nSCAN COMPLETE | "
        f"{elapsed:.1f}s",
        flush=True
    )


# =========================================================
# ACTUAL PROGRAM START
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
            "🚨 DIVINE INTRADAY FATAL ERROR\n\n"
            f"{e}"
        )

        raise
