import os
import time
import requests
import pyotp
import pandas as pd

from datetime import datetime, timedelta
from SmartApi import SmartConnect


# ============================================================
# ANGEL ONE SWING SCANNER V3.4.9 FIXED
# ============================================================
# Core strategy LOCKED
#
# PHASE 1:
#   - NSE equity universe
#   - LTP >= 50
#   - Current volume >= 100000
#   - Top 80 by volume
#
# PHASE 2:
#   - Completed daily candle only
#   - Avg20 volume >= 50000
#   - Daily volume >= 2x Avg20
#   - Close within 8% of 52W high
#   - Green daily candle
#   - Proper weekly trend:
#       Weekly close > 40-week SMA
#
# TARGET:
#   T1 = +5%
#   T2 = +8%
#
# SL:
#   Last completed daily candle low
#
# NO AUTOMATIC ORDERS
# ============================================================


# ============================================================
# CONFIG
# ============================================================

API = os.getenv("API_KEY")
CID = os.getenv("CLIENT_ID")
PWD = os.getenv("PASSWORD")
TOTP = os.getenv("TOTP_SECRET")

TG = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT = os.getenv("TELEGRAM_CHAT_ID")

MASTER = (
    "https://margincalculator.angelbroking.com/"
    "OpenAPI_File/files/OpenAPIScripMaster.json"
)

MARKET_OPEN_HOUR = 9
MARKET_OPEN_MINUTE = 15

MARKET_CLOSE_HOUR = 15
MARKET_CLOSE_MINUTE = 30

TOP_CANDIDATES = 80

MIN_LTP = 50
MIN_CURRENT_VOLUME = 100000

MIN_AVG20_VOLUME = 50000
MIN_VOLUME_MULTIPLE = 2.0

NEAR_HIGH_PERCENT = 0.92

T1_PERCENT = 0.05
T2_PERCENT = 0.08

HISTORY_DAYS = 380

STOCK_DELAY = 0.7

# Rate limit settings
RATE_LIMIT_RETRIES = 4
RATE_LIMIT_BASE_WAIT = 8

# Excluded stocks
EXCLUDED_SYMBOLS = {
    "LTIM",
    "TATAMOTORS"
}


# ============================================================
# GLOBAL
# ============================================================

obj = None


# ============================================================
# TIME HELPERS
# ============================================================

def now_local():
    return datetime.now()


def market_closed():
    now = now_local()

    close_time = now.replace(
        hour=MARKET_CLOSE_HOUR,
        minute=MARKET_CLOSE_MINUTE,
        second=0,
        microsecond=0
    )

    return now >= close_time


def market_open():
    now = now_local()

    open_time = now.replace(
        hour=MARKET_OPEN_HOUR,
        minute=MARKET_OPEN_MINUTE,
        second=0,
        microsecond=0
    )

    close_time = now.replace(
        hour=MARKET_CLOSE_HOUR,
        minute=MARKET_CLOSE_MINUTE,
        second=0,
        microsecond=0
    )

    return open_time <= now < close_time


# ============================================================
# TELEGRAM
# ============================================================

def telegram(msg):

    if not TG or not CHAT:
        print("Telegram credentials not configured.", flush=True)
        return

    try:

        r = requests.post(
            f"https://api.telegram.org/bot{TG}/sendMessage",
            data={
                "chat_id": CHAT,
                "text": msg,
                "parse_mode": "Markdown"
            },
            timeout=15
        )

        if not r.ok:
            print(
                "Telegram error:",
                r.text[:300],
                flush=True
            )

    except Exception as e:

        print(
            "Telegram send failed:",
            e,
            flush=True
        )


# ============================================================
# RATE LIMIT DETECTION
# ============================================================

def is_rate_limit_error(error):

    text = str(error).lower()

    keywords = [
        "access rate",
        "exceeding access rate",
        "rate limit",
        "too many requests",
        "access denied"
    ]

    return any(k in text for k in keywords)


# ============================================================
# HISTORICAL DAILY DATA
# ============================================================

def hist(token, days=HISTORY_DAYS, interval="ONE_DAY"):

    for attempt in range(1, RATE_LIMIT_RETRIES + 1):

        try:

            p = {
                "exchange": "NSE",
                "symboltoken": str(token),
                "interval": interval,
                "fromdate": (
                    datetime.now() - timedelta(days=days)
                ).strftime("%Y-%m-%d %H:%M"),
                "todate": datetime.now().strftime(
                    "%Y-%m-%d %H:%M"
                )
            }

            r = obj.getCandleData(p)

            if r and r.get("data"):

                df = pd.DataFrame(
                    r["data"],
                    columns=[
                        "time",
                        "open",
                        "high",
                        "low",
                        "close",
                        "volume"
                    ]
                )

                return df

            # API returned no data
            return None

        except Exception as e:

            if is_rate_limit_error(e):

                wait = RATE_LIMIT_BASE_WAIT * attempt

                print(
                    f"   Rate limit detected. "
                    f"Waiting {wait}s "
                    f"(attempt {attempt}/{RATE_LIMIT_RETRIES})",
                    flush=True
                )

                time.sleep(wait)

                continue

            print(
                f"   Candle error: {e}",
                flush=True
            )

            return None

    print(
        "   Candle failed after rate-limit retries.",
        flush=True
    )

    return None


# ============================================================
# CLEAN DAILY DATA
# ============================================================

def prepare_daily_data(df):

    if df is None or df.empty:
        return None

    try:

        df = df.copy()

        df["time"] = pd.to_datetime(
            df["time"],
            errors="coerce"
        )

        df = df.dropna(subset=["time"])

        for col in [
            "open",
            "high",
            "low",
            "close",
            "volume"
        ]:

            df[col] = pd.to_numeric(
                df[col],
                errors="coerce"
            )

        df = df.dropna(
            subset=[
                "open",
                "high",
                "low",
                "close",
                "volume"
            ]
        )

        df = df.sort_values("time")

        df = df.drop_duplicates(
            subset=["time"],
            keep="last"
        )

        # ----------------------------------------------------
        # IMPORTANT FIX:
        #
        # If today's daily candle is still incomplete,
        # remove it.
        #
        # Therefore 2:56 PM scan will NOT use today's
        # incomplete candle.
        #
        # After 3:30 PM today's candle is accepted as
        # completed.
        # ----------------------------------------------------

        today = datetime.now().date()

        if not market_closed():

            if len(df) > 0:

                last_date = df["time"].iloc[-1].date()

                if last_date == today:

                    df = df.iloc[:-1]

        return df.reset_index(drop=True)

    except Exception as e:

        print(
            "   Data preparation error:",
            e,
            flush=True
        )

        return None


# ============================================================
# WEEKLY DATA
# ============================================================

def make_weekly_data(df):

    try:

        w = df.copy()

        w["time"] = pd.to_datetime(
            w["time"],
            errors="coerce"
        )

        w = w.dropna(subset=["time"])

        w = w.set_index("time")

        w = w.resample("W-FRI").agg({
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum"
        }).dropna()

        # ----------------------------------------------------
        # IMPORTANT:
        #
        # If current Friday week is incomplete, remove it.
        #
        # After market close Friday, it remains.
        # ----------------------------------------------------

        today = datetime.now()

        if not market_closed():

            current_week_end = (
                today
                + timedelta(
                    days=(4 - today.weekday())
                )
            ).date()

            if len(w) > 0:

                last_week_date = w.index[-1].date()

                if last_week_date >= current_week_end:

                    w = w.iloc[:-1]

        return w

    except Exception as e:

        print(
            "   Weekly data error:",
            e,
            flush=True
        )

        return None


# ============================================================
# BULK MARKET DATA
# ============================================================

def bulk_quotes(token_list):

    out = {}

    if not token_list:
        return out

    for i in range(0, len(token_list), 50):

        batch = token_list[i:i + 50]

        success = False

        for attempt in range(
            1,
            RATE_LIMIT_RETRIES + 1
        ):

            try:

                r = obj.getMarketData(
                    "FULL",
                    {
                        "NSE": batch
                    }
                )

                data = (r or {}).get(
                    "data",
                    {}
                )

                rows = []

                if isinstance(data, dict):

                    rows = (
                        data.get("fetched", [])
                        or data.get("data", [])
                    )

                elif isinstance(data, list):

                    rows = data

                for q in rows:

                    if not isinstance(q, dict):
                        continue

                    token = str(
                        q.get(
                            "symbolToken",
                            ""
                        )
                    )

                    if token:
                        out[token] = q

                success = True
                break

            except Exception as e:

                print(
                    "Bulk quote error:",
                    e,
                    flush=True
                )

                if is_rate_limit_error(e):

                    wait = RATE_LIMIT_BASE_WAIT * attempt

                    print(
                        f"Bulk rate limit. "
                        f"Waiting {wait}s...",
                        flush=True
                    )

                    time.sleep(wait)

                else:

                    time.sleep(
                        3 * attempt
                    )

        if not success:

            print(
                "Bulk batch failed:",
                batch[:3],
                "...",
                flush=True
            )

        # Endpoint safety delay
        time.sleep(1.1)

        print(
            f"Bulk quote "
            f"{min(i + 50, len(token_list))}/"
            f"{len(token_list)}",
            flush=True
        )

    return out


# ============================================================
# MAIN
# ============================================================

print(
    "\n======================================"
)

print(
    " ANGEL ONE SWING SCANNER V3.4.9 FIXED"
)

print(
    "======================================",
    flush=True
)

print(
    "Core strategy: LOCKED",
    flush=True
)

print(
    "Auto orders: DISABLED",
    flush=True
)

if market_closed():

    print(
        "Mode: FINAL / COMPLETED DAILY CANDLE",
        flush=True
    )

else:

    print(
        "Mode: PRE-CLOSE / INCOMPLETE CANDLE PROTECTED",
        flush=True
    )


# ============================================================
# LOGIN
# ============================================================

print(
    "\nLogin...",
    flush=True
)

if not API or not CID or not PWD or not TOTP:

    print(
        "ERROR: Missing Angel One credentials.",
        flush=True
    )

    raise SystemExit


try:

    obj = SmartConnect(
        api_key=API
    )

    totp_code = pyotp.TOTP(TOTP).now()

    sess = obj.generateSession(
        CID,
        PWD,
        totp_code
    )

    if not sess:

        print(
            "Login failed.",
            flush=True
        )

        raise SystemExit

    print(
        "Angel OK",
        flush=True
    )

except Exception as e:

    print(
        "LOGIN ERROR:",
        e,
        flush=True
    )

    raise SystemExit


# ============================================================
# LOAD NSE MASTER
# ============================================================

print(
    "\nLoading NSE master...",
    flush=True
)

try:

    master_response = requests.get(
        MASTER,
        timeout=30
    )

    master_response.raise_for_status()

    master = master_response.json()

except Exception as e:

    print(
        "Master loading failed:",
        e,
        flush=True
    )

    raise SystemExit


tokens = {}

for item in master:

    try:

        seg = str(
            item.get(
                "exch_seg",
                ""
            )
        ).lower()

        sym = str(
            item.get(
                "symbol",
                ""
            )
        ).upper()

        if seg in (
            "nse",
            "nse_cm"
        ) and sym.endswith("-EQ"):

            s = sym[:-3]

            if s in EXCLUDED_SYMBOLS:
                continue

            token = item.get("token")

            if token:

                tokens[s] = str(token)

    except Exception:
        continue


stocks = list(
    tokens.items()
)

print(
    f"NSE stocks: {len(stocks)}",
    flush=True
)

print(
    "Excluded:",
    ", ".join(sorted(EXCLUDED_SYMBOLS)),
    flush=True
)


# ============================================================
# PHASE 1
# ============================================================

print(
    "\nPHASE 1: BULK VOLUME SCAN...",
    flush=True
)

q = bulk_quotes(
    [token for _, token in stocks]
)

candidates = []


for sym, token in stocks:

    x = q.get(
        token,
        {}
    )

    try:

        ltp = float(
            x.get(
                "ltp",
                0
            ) or 0
        )

        # Angel field
        vol_raw = x.get(
            "tradeVolume",
            0
        )

        vol = float(
            vol_raw or 0
        )

        if (
            ltp >= MIN_LTP
            and vol >= MIN_CURRENT_VOLUME
        ):

            candidates.append({
                "sym": sym,
                "token": token,
                "ltp": ltp,
                "volume": vol
            })

    except Exception:
        continue


# Highest volume first
candidates.sort(
    key=lambda x: x["volume"],
    reverse=True
)

candidates = candidates[
    :TOP_CANDIDATES
]


print(
    f"PHASE 1 DONE: "
    f"{len(candidates)} candidates / "
    f"{len(stocks)} NSE stocks",
    flush=True
)

print(
    "TOP:",
    [
        x["sym"]
        for x in candidates[:10]
    ],
    flush=True
)


# ============================================================
# PHASE 2
# ============================================================

print(
    "\nPHASE 2: DAILY + WEEKLY CHECK...",
    flush=True
)

picks = []


for n, x in enumerate(
    candidates,
    1
):

    print(
        f"[{n}/{len(candidates)}] "
        f"{x['sym']}",
        flush=True
    )

    # --------------------------------------------------------
    # HISTORICAL DATA
    # --------------------------------------------------------

    raw_df = hist(
        x["token"],
        HISTORY_DAYS
    )

    if raw_df is None:

        print(
            "   No candle data.",
            flush=True
        )

        continue

    # --------------------------------------------------------
    # PREPARE DATA
    # --------------------------------------------------------

    df = prepare_daily_data(
        raw_df
    )

    if df is None or len(df) < 200:

        print(
            "   Insufficient completed daily candles.",
            flush=True
        )

        continue

    # --------------------------------------------------------
    # LAST COMPLETED DAILY CANDLE
    # --------------------------------------------------------

    last = df.iloc[-1]

    # --------------------------------------------------------
    # SAME VOLUME RULE
    # --------------------------------------------------------

    if len(df) < 21:

        continue

    avg20 = (
        df["volume"]
        .iloc[-21:-1]
        .mean()
    )

    if not avg20 or avg20 <= 0:

        continue

    volx = (
        float(last["volume"])
        / float(avg20)
    )

    if (
        avg20 < MIN_AVG20_VOLUME
        or volx < MIN_VOLUME_MULTIPLE
    ):

        continue

    # --------------------------------------------------------
    # SAME DAILY CONDITIONS
    # --------------------------------------------------------

    dma200 = (
        df["close"]
        .rolling(200)
        .mean()
        .iloc[-1]
    )

    high52 = (
        df["high"]
        .tail(252)
        .max()
    )

    if pd.isna(dma200) or pd.isna(high52):

        continue

    close_price = float(
        last["close"]
    )

    open_price = float(
        last["open"]
    )

    low_price = float(
        last["low"]
    )

    near_high = (
        close_price
        >= float(high52)
        * NEAR_HIGH_PERCENT
    )

    green = (
        close_price
        > open_price
    )

    # --------------------------------------------------------
    # WEEKLY DATA
    # --------------------------------------------------------

    w = make_weekly_data(
        df
    )

    if w is None or len(w) < 40:

        print(
            "   Insufficient weekly history.",
            flush=True
        )

        continue

    # --------------------------------------------------------
    # PROPER WEEKLY TREND
    #
    # 40-week SMA is approximately the weekly equivalent
    # of 200 trading days.
    # --------------------------------------------------------

    w["wma40"] = (
        w["close"]
        .rolling(40)
        .mean()
    )

    weekly_close = float(
        w["close"].iloc[-1]
    )

    weekly_ma40 = float(
        w["wma40"].iloc[-1]
    )

    if pd.isna(weekly_ma40):

        continue

    weekly_up = (
        weekly_close
        > weekly_ma40
    )

    # --------------------------------------------------------
    # FINAL BUY CONDITION
    #
    # SAME CORE CONDITIONS:
    #   weekly up
    #   near 52W high
    #   green candle
    # --------------------------------------------------------

    if (
        weekly_up
        and near_high
        and green
    ):

        entry = close_price
        sl = low_price

        target1 = (
            entry
            * (1 + T1_PERCENT)
        )

        target2 = (
            entry
            * (1 + T2_PERCENT)
        )

        picks.append({

            "Stock": x["sym"],

            "LTP": entry,

            "52W": float(high52),

            "VolX": volx,

            "SL": sl,

            "T1": target1,

            "T2": target2,

            "WeeklyMA": weekly_ma40

        })

        print(
            f"   BUY FOUND: "
            f"{x['sym']} "
            f"Vol {volx:.2f}x",
            flush=True
        )

    time.sleep(
        STOCK_DELAY
    )


# ============================================================
# SORT RESULTS
# ============================================================

picks.sort(
    key=lambda x: x["VolX"],
    reverse=True
)


# ====================================
