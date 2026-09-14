import os
import sys
import time
import requests
import pandas as pd
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from SmartApi import SmartConnect
import pyotp


# ============================================================
# ANGEL ONE SWING SCANNER - SPEED FINAL
# ============================================================
#
# FULL NSE-EQ UNIVERSE
# BULK CURRENT VOLUME FILTER
# DAILY + WEEKLY CONFIRMATION
# TELEGRAM ALERT
#
# AUTO ORDER = DISABLED
#
# STRATEGY:
#
# 1. All NSE-EQ stocks from Angel instrument master
#
# 2. Fast bulk quote scan
#       Current volume >= 100,000
#
# 3. Rank current volume
#       TOP 80
#
# 4. Daily chart:
#       Price >= Rs.50
#       20D Average Volume >= 50,000
#       Latest Volume >= 2x 20D Average
#       Close > 200 DMA
#       Close >= 92% of 52 Week High
#       Green Daily Candle
#
# 5. Weekly confirmation:
#       Weekly Close > Daily 200 DMA
#       Latest Weekly Close > Previous Weekly Close
#
# 6. Entry:
#       Latest completed daily close
#
# 7. SL:
#       Latest completed daily low
#
# 8. Targets:
#       T1 = 2R
#       T2 = 3R
#
# 9. Telegram
#
# NO AUTOMATIC ORDERS
#
# Excluded:
#       TATAMOTORS
#       LTIM
#
# Added / retained:
#       ADANIPOWER
#
# ============================================================


# ============================================================
# TIMEZONE
# ============================================================

IST = ZoneInfo("Asia/Kolkata")


# ============================================================
# GITHUB SECRETS
# ============================================================

API_KEY = os.getenv("API_KEY", "").strip()
CLIENT_ID = os.getenv("CLIENT_ID", "").strip()
PASSWORD = os.getenv("PASSWORD", "").strip()
TOTP_SECRET = os.getenv("TOTP_SECRET", "").strip()

TELEGRAM_BOT_TOKEN = os.getenv(
    "TELEGRAM_BOT_TOKEN",
    ""
).strip()

TELEGRAM_CHAT_ID = os.getenv(
    "TELEGRAM_CHAT_ID",
    ""
).strip()


# ============================================================
# ANGEL INSTRUMENT MASTER
# ============================================================

INSTRUMENT_URL = (
    "https://margincalculator.angelbroking.com/"
    "OpenAPI_File/files/OpenAPIScripMaster.json"
)


# ============================================================
# STRATEGY SETTINGS
# ============================================================

TOP_N = 80

MIN_PRICE = 50.0

MIN_AVG_VOL_20 = 50000

MIN_VOL_X = 2.0

# Since:
#
# Avg Volume >= 50,000
# Volume >= 2x Avg Volume
#
# Minimum possible latest volume is:
#
# 50,000 x 2 = 100,000
#
# So Phase-1 bulk scan can safely reject anything
# below 100,000 current volume.

MIN_CURRENT_VOLUME = 100000

# Close must be >= 92% of 52-week high
NEAR_52W_PCT = 0.92

# Need enough history for:
# 200 DMA
# 252 trading days ~= 52 weeks
DAILY_LOOKBACK_DAYS = 420


# ============================================================
# SPEED / API SETTINGS
# ============================================================

# Angel bulk quote request
# Keep at or below 50 tokens per request.
QUOTE_BATCH_SIZE = 50

QUOTE_DELAY = 0.40


# Historical candle API pacing.
#
# We deliberately keep this slower than bulk quote requests
# because getCandleData is the endpoint most likely to hit
# access-rate restrictions.
HIST_DELAY = 0.65

HIST_RETRY_DELAY = 8

MAX_HIST_RETRIES = 5

# Cooling pause after historical requests
BATCH_PAUSE_EVERY = 20

BATCH_PAUSE_SECONDS = 3

REQUEST_TIMEOUT = 25


# ============================================================
# EXCLUDED STOCKS
# ============================================================

EXCLUDED_SYMBOLS = {
    "TATAMOTORS",
    "LTIM",
}


# ============================================================
# LOGGING
# ============================================================

def log(message):

    now = datetime.now(IST).strftime(
        "%d-%m-%Y %H:%M:%S IST"
    )

    print(
        f"[{now}] {message}",
        flush=True
    )


# ============================================================
# FATAL ERROR
# ============================================================

def fail(message):

    log("FATAL: " + message)

    sys.exit(1)


# ============================================================
# VALIDATE GITHUB SECRETS
# ============================================================

def validate_config():

    required = {
        "API_KEY": API_KEY,
        "CLIENT_ID": CLIENT_ID,
        "PASSWORD": PASSWORD,
        "TOTP_SECRET": TOTP_SECRET,
        "TELEGRAM_BOT_TOKEN": TELEGRAM_BOT_TOKEN,
        "TELEGRAM_CHAT_ID": TELEGRAM_CHAT_ID,
    }

    missing = [
        key
        for key, value in required.items()
        if not value
    ]

    if missing:

        fail(
            "Missing GitHub Secret(s): "
            + ", ".join(missing)
        )


# ============================================================
# ANGEL LOGIN
# ============================================================

def login():

    log("Logging into Angel One...")

    smart = SmartConnect(
        api_key=API_KEY
    )

    last_error = None

    for attempt in range(1, 4):

        try:

            log(
                f"Generating TOTP... attempt "
                f"{attempt}/3"
            )

            totp = pyotp.TOTP(
                TOTP_SECRET
            ).now()

            session = smart.generateSession(
                CLIENT_ID,
                PASSWORD,
                totp
            )

            if not session:

                raise RuntimeError(
                    "Empty login response"
                )

            if not session.get("status"):

                raise RuntimeError(
                    f"Login failed: {session}"
                )

            # Feed token
            smart.getfeedToken()

            log(
                "Angel One login successful."
            )

            return smart

        except Exception as exc:

            last_error = exc

            log(
                f"Login attempt {attempt}/3 "
                f"failed: {exc}"
            )

            if attempt < 3:

                time.sleep(3)

    raise RuntimeError(
        "Angel login failed after 3 attempts: "
        f"{last_error}"
    )


# ============================================================
# DOWNLOAD INSTRUMENT MASTER
# ============================================================

def download_instruments():

    log(
        "Downloading Angel instrument master..."
    )

    response = requests.get(
        INSTRUMENT_URL,
        timeout=REQUEST_TIMEOUT
    )

    response.raise_for_status()

    instruments = response.json()

    if not isinstance(
        instruments,
        list
    ):

        raise RuntimeError(
            "Instrument master format is invalid."
        )

    return instruments


# ============================================================
# BUILD FULL NSE-EQ UNIVERSE
# ============================================================

def build_nse_universe(instruments):

    universe = []

    seen = set()

    for item in instruments:

        if item.get("exch_seg") != "NSE":
            continue

        symbol = str(
            item.get("name", "")
        ).strip().upper()

        token = str(
            item.get("token", "")
        ).strip()

        trading_symbol = str(
            item.get("symbol", "")
        ).strip().upper()

        if not symbol:
            continue

        if not token:
            continue

        if not trading_symbol:
            continue

        # Only equity
        if not trading_symbol.endswith("-EQ"):
            continue

        # Remove unwanted stocks
        if symbol in EXCLUDED_SYMBOLS:
            continue

        if symbol in seen:
            continue

        seen.add(symbol)

        universe.append({
            "symbol": symbol,
            "token": token,
            "trading_symbol": trading_symbol,
        })

    universe.sort(
        key=lambda x: x["symbol"]
    )

    return universe


# ============================================================
# SAFE FLOAT
# ============================================================

def safe_float(
    value,
    default=0.0
):

    try:

        if value is None:
            return default

        if value == "":
            return default

        return float(value)

    except Exception:

        return default


# ============================================================
# CHUNK LIST
# ============================================================

def chunked(
    items,
    size
):

    for i in range(
        0,
        len(items),
        size
    ):

        yield items[
            i:i + size
        ]


# ============================================================
# EXTRACT BULK QUOTE DATA
# ============================================================

def extract_quote_items(
    response
):

    if not isinstance(
        response,
        dict
    ):

        return []

    data = response.get(
        "data"
    )

    if isinstance(
        data,
        dict
    ):

        fetched = data.get(
            "fetched"
        )

        if isinstance(
            fetched,
            list
        ):

            return fetched

        nested = data.get(
            "data"
        )

        if isinstance(
            nested,
            list
        ):

            return nested

    if isinstance(
        data,
        list
    ):

        return data

    return []


# ============================================================
# GET VOLUME FROM QUOTE
# ============================================================

def extract_volume(item):

    possible_fields = [

        "tradeVolume",

        "tradevolume",

        "volume",

        "totalTradedVolume",

        "totalTradedVolumeToday",

    ]

    for field in possible_fields:

        if field in item:

            value = safe_float(
                item.get(field),
                0
            )

            if value > 0:

                return value

    return 0.0


# ============================================================
# GET LTP FROM QUOTE
# ============================================================

def extract_ltp(item):

    possible_fields = [

        "ltp",

        "LTP",

        "lastTradedPrice",

    ]

    for field in possible_fields:

        if field in item:

            value = safe_float(
                item.get(field),
                0
            )

            if value > 0:

                return value

    return 0.0


# ============================================================
# FAST PHASE 1
#
# BULK CURRENT VOLUME
# ============================================================

def get_current_volume_bulk(
    smart,
    universe
):

    log("")
    log("=" * 70)
    log("PHASE 1 - FAST BULK CURRENT VOLUME SCAN")
    log("=" * 70)

    log(
        f"NSE stocks: {len(universe)}"
    )

    log(
        f"Bulk batch size: "
        f"{QUOTE_BATCH_SIZE}"
    )

    log(
        f"Minimum current volume: "
        f"{MIN_CURRENT_VOLUME:,}"
    )

    volume_rows = []

    total = len(universe)

    processed = 0

    batch_number = 0

    for batch in chunked(
        universe,
        QUOTE_BATCH_SIZE
    ):

        batch_number += 1

        tokens = [
            stock["token"]
            for stock in batch
        ]

        payload = {

            "mode": "FULL",

            "exchangeTokens": {

                "NSE": tokens

            }

        }

        response = None

        # ----------------------------------------------------
        # First attempt
        # ----------------------------------------------------

        try:

            response = smart.getMarketData(
                payload
            )

        except Exception as exc:

            log(
                f"Bulk quote batch "
                f"{batch_number} error: "
                f"{exc}"
            )

        # ----------------------------------------------------
        # Retry once
        # ----------------------------------------------------

        if response is None:

            time.sleep(2)

            try:

                response = smart.getMarketData(
                    payload
                )

            except Exception as exc:

                log(
                    f"Bulk quote retry failed "
                    f"batch {batch_number}: "
                    f"{exc}"
                )

                response = None

        # ----------------------------------------------------
        # Process response
        # ----------------------------------------------------

        items = extract_quote_items(
            response
        )

        by_token = {}

        for item in items:

            token = str(
                item.get(
                    "symbolToken",
                    item.get(
                        "symboltoken",
                        ""
                    )
                )
            ).strip()

            if token:

                by_token[token] = item

        for stock in batch:

            item = by_token.get(
                stock["token"]
            )

            if not item:
                continue

            volume = extract_volume(
                item
            )

            ltp = extract_ltp(
                item
            )

            if volume < MIN_CURRENT_VOLUME:
                continue

            volume_rows.append({

                **stock,

                "current_volume": volume,

                "ltp": ltp,

            })

        processed += len(batch)

        if (
            batch_number % 5 == 0
            or processed >= total
        ):

            log(
                f"Bulk progress: "
                f"{processed}/{total} | "
                f"Candidates: "
                f"{len(volume_rows)}"
            )

        time.sleep(
            QUOTE_DELAY
        )

    # --------------------------------------------------------
    # Remove duplicates
    # --------------------------------------------------------

    unique = {}

    for row in volume_rows:

        unique[
            row["symbol"]
        ] = row

    candidates = list(
        unique.values()
    )

    # --------------------------------------------------------
    # Sort by current volume
    # --------------------------------------------------------

    candidates.sort(
        key=lambda x:
        x["current_volume"],
        reverse=True
    )

    # --------------------------------------------------------
    # TOP 80
    # --------------------------------------------------------

    top_candidates = candidates[
        :TOP_N
    ]

    log("")
    log(
        f"Bulk candidates: "
        f"{len(candidates)}"
    )

    log(
        f"TOP {TOP_N} selected: "
        f"{len(top_candidates)}"
    )

    return (
        candidates,
        top_candidates
    )


# ============================================================
# RATE LIMIT DETECTOR
# ============================================================

def is_rate_limit_error(
    exc
):

    text = str(
        exc
    ).lower()

    keywords = [

        "exceeding access rate",

        "access denied",

        "403",

        "429",

        "rate limit",

        "too many requests",

    ]

    for keyword in keywords:

        if keyword in text:

            return True

    return False


# ============================================================
# GET DAILY CANDLES
# ============================================================

def get_daily_candles(
    smart,
    token
):

    # Current time
    now = datetime.now(
        IST
    )

    # We intentionally stop at yesterday.
    #
    # This prevents today's incomplete candle
    # from becoming a swing signal.

    yesterday = (
        now - timedelta(days=1)
    ).date()

    start_date = (
        yesterday
        - timedelta(
            days=DAILY_LOOKBACK_DAYS
        )
    )

    params = {

        "exchange": "NSE",

        "symboltoken": str(
            token
        ),

        "interval": "ONE_DAY",

        "fromdate":
            f"{start_date.isoformat()} 09:15",

        "todate":
            f"{yesterday.isoformat()} 15:30",

    }

    for attempt in range(
        1,
        MAX_HIST_RETRIES + 1
    ):

        try:

            response = smart.getCandleData(
                params
            )

            if not isinstance(
                response,
                dict
            ):

                raise RuntimeError(
                    f"Unexpected candle response: "
                    f"{response}"
                )

            if not response.get(
                "status"
            ):

                message = response.get(
                    "message",
                    response.get(
                        "errorcode",
                        "Unknown candle error"
                    )
                )

                raise RuntimeError(
                    str(message)
                )

            data = response.get(
                "data"
            ) or []

            if not data:

                return None

            df = pd.DataFrame(

                data,

                columns=[
                    "date",
                    "open",
                    "high",
                    "low",
                    "close",
                    "volume",
                ]

            )

            # ------------------------------------------------
            # Numeric conversion
            # ------------------------------------------------

            numeric_columns = [

                "open",

                "high",

                "low",

                "close",

                "volume",

            ]

            for column in numeric_columns:

                df[column] = pd.to_numeric(

                    df[column],

                    errors="coerce"

                )

            # ------------------------------------------------
            # Date conversion
            # ------------------------------------------------

            df["date"] = pd.to_datetime(

                df["date"],

                errors="coerce"

            )

            # ------------------------------------------------
            # Remove invalid rows
            # ------------------------------------------------

            df = df.dropna(

                subset=[

                    "date",

                    "open",

                    "high",

                    "low",

                    "close",

                    "volume",

                ]

            )

            # ------------------------------------------------
            # Sort
            # ------------------------------------------------

            df = df.sort_values(
                "date"
            )

            df = df.drop_duplicates(
                subset=["date"],
                keep="last"
            )

            df = df.reset_index(
                drop=True
            )

            # ------------------------------------------------
            # Safety:
            # never use today's candle
            # ------------------------------------------------

            today = datetime.now(
                IST
            ).date()

            df = df[
                df["date"].dt.date
                < today
            ].copy()

            # Need at least 200+ candles
            if len(df) < 205:

                return None

            return df

        except Exception as exc:

            # Last attempt
            if attempt >= MAX_HIST_RETRIES:

                log(
                    "Historical API failed after "
                    f"{MAX_HIST_RETRIES} attempts: "
                    f"{exc}"
                )

                return None

            # ----------------------------
