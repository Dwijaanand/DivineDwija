import os
import time
import json
import requests
import pyotp
import pandas as pd

from datetime import datetime, timedelta
from SmartApi import SmartConnect


# ============================================================
# CHANDAN PURA NSE HIGH-VOLUME SWING SCANNER
# GitHub Actions + Angel One + Telegram
#
# FLOW:
# 1. Load complete NSE-EQ universe from Angel instrument master
# 2. Read DAILY candles from Angel One for NSE stocks
# 3. Find high-volume stocks
# 4. Rank and keep Top 80
# 5. Read DAILY + WEEKLY candles for Top 80
# 6. Check swing setup
# 7. Send final signals to Telegram
#
# NO AUTO ORDERS
# ============================================================


# -----------------------------
# ENVIRONMENT / SECRETS
# -----------------------------

API_KEY = os.getenv("API_KEY")
CLIENT_ID = os.getenv("CLIENT_ID")
PASSWORD = os.getenv("PASSWORD")
TOTP_SECRET = os.getenv("TOTP_SECRET")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")


# -----------------------------
# SETTINGS
# -----------------------------

INSTRUMENT_URL = (
    "https://margincalculator.angelbroking.com/"
    "OpenAPI_File/files/OpenAPIScripMaster.json"
)

TOP_VOLUME_COUNT = 80

MIN_PRICE = 50.0
MIN_AVG_VOLUME = 50000

# High-volume condition
MIN_VOLUME_X = 2.0

# High-volume scan uses enough history for volume average
DAILY_LOOKBACK_DAYS = 45

# Final daily chart history
DAILY_HISTORY_DAYS = 420

# Weekly chart history
WEEKLY_HISTORY_DAYS = 900

# Conservative API pacing.
# Do NOT use 15 parallel candle requests.
REQUEST_DELAY = 0.45

# Extra pause after a group of requests
BATCH_SIZE = 40
BATCH_PAUSE = 5.0

# Retry configuration
MAX_RETRIES = 4
RETRY_BASE_SECONDS = 8

# Final setup
NEAR_52W_PERCENT = 0.92

# GitHub Actions should run after market data is sufficiently formed.
# Workflow itself is set for 3 PM IST.
USE_COMPLETED_DAILY_CANDLE = True


# -----------------------------
# LOGGING
# -----------------------------

def log(msg):
    print(
        f"[{datetime.now().strftime('%d-%m-%Y %H:%M:%S')}] "
        f"{msg}",
        flush=True
    )


# -----------------------------
# TELEGRAM
# -----------------------------

def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log("Telegram secrets missing.")
        return False

    try:
        url = (
            f"https://api.telegram.org/bot"
            f"{TELEGRAM_BOT_TOKEN}/sendMessage"
        )

        response = requests.post(
            url,
            data={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
            },
            timeout=20,
        )

        if response.ok:
            log("Telegram sent successfully.")
            return True

        log(
            f"Telegram failed: HTTP {response.status_code} "
            f"{response.text[:300]}"
        )

    except Exception as e:
        log(f"Telegram error: {e}")

    return False


# -----------------------------
# VALIDATE SECRETS
# -----------------------------

def validate_secrets():
    required = {
        "API_KEY": API_KEY,
        "CLIENT_ID": CLIENT_ID,
        "PASSWORD": PASSWORD,
        "TOTP_SECRET": TOTP_SECRET,
        "TELEGRAM_BOT_TOKEN": TELEGRAM_BOT_TOKEN,
        "TELEGRAM_CHAT_ID": TELEGRAM_CHAT_ID,
    }

    missing = [
        name for name, value in required.items()
        if not value
    ]

    if missing:
        raise RuntimeError(
            "Missing GitHub Secrets: " + ", ".join(missing)
        )


# -----------------------------
# ANGEL LOGIN
# -----------------------------

def login_angel():
    log("Login to Angel One...")

    obj = SmartConnect(api_key=API_KEY)

    totp = pyotp.TOTP(TOTP_SECRET).now()

    data = obj.generateSession(
        CLIENT_ID,
        PASSWORD,
        totp
    )

    if not data:
        raise RuntimeError("Angel login returned empty response.")

    if data.get("status") is False:
        raise RuntimeError(
            f"Angel login failed: {data}"
        )

    log("Angel One login successful.")

    return obj


# -----------------------------
# LOAD NSE INSTRUMENT MASTER
# -----------------------------

def load_nse_universe():
    log("Downloading Angel One instrument master...")

    response = requests.get(
        INSTRUMENT_URL,
        timeout=30
    )

    response.raise_for_status()

    master = response.json()

    token_map = {}

    for item in master:

        if item.get("exch_seg") != "NSE":
            continue

        symbol = item.get("symbol", "")
        token = item.get("token")

        if not symbol.endswith("-EQ"):
            continue

        if not token:
            continue

        clean_symbol = symbol[:-3]

        token_map[clean_symbol] = str(token)

    universe = sorted(
        token_map.items(),
        key=lambda x: x[0]
    )

    log(
        f"Total NSE-EQ stocks found: {len(universe)}"
    )

    return universe


# -----------------------------
# DATAFRAME CONVERTER
# -----------------------------

def candles_to_df(data):

    if not data:
        return None

    try:
        df = pd.DataFrame(
            data,
            columns=[
                "time",
                "open",
                "high",
                "low",
                "close",
                "volume",
            ]
        )

        numeric_columns = [
            "open",
            "high",
            "low",
            "close",
            "volume",
        ]

        for col in numeric_columns:
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
                "volume",
            ]
        )

        if df.empty:
            return None

        df = df.reset_index(drop=True)

        return df

    except Exception as e:
        log(f"DataFrame conversion error: {e}")
        return None


# -----------------------------
# REMOVE INCOMPLETE DAILY CANDLE
# -----------------------------

def remove_incomplete_daily(df):

    if df is None or df.empty:
        return df

    if not USE_COMPLETED_DAILY_CANDLE:
        return df

    try:
        # Angel timestamps generally contain +05:30.
        # At 3 PM the current candle can still be incomplete.
        # Therefore use previous completed daily candle.
        if len(df) >= 2:
            return df.iloc[:-1].reset_index(drop=True)

    except Exception:
        pass

    return df


# -----------------------------
# ANGEL CANDLE REQUEST
# -----------------------------

def get_candles(
    obj,
    token,
    interval,
    from_date,
    to_date,
    label=""
):

    params = {
        "exchange": "NSE",
        "symboltoken": str(token),
        "interval": interval,
        "fromdate": from_date,
        "todate": to_date,
    }

    for attempt in range(1, MAX_RETRIES + 1):

        try:

            response = obj.getCandleData(params)

            if response and response.get("data"):
                return response["data"]

            # Explicit Angel error
            message = ""

            if response:
                message = str(
                    response.get("message", "")
                )

            if (
                "access" in message.lower()
                or "rate" in message.lower()
                or "too many" in message.lower()
            ):
                wait = RETRY_BASE_SECONDS * attempt

                log(
                    f"Rate limit [{label}] "
                    f"attempt {attempt}/{MAX_RETRIES}. "
                    f"Waiting {wait}s..."
                )

                time.sleep(wait)
                continue

            return None

        except Exception as e:

            error_text = str(e).lower()

            rate_error = (
                "403" in error_text
                or "rate" in error_text
                or "access denied" in error_text
                or "too many requests" in error_text
            )

            if rate_error:

                wait = RETRY_BASE_SECONDS * attempt

                log(
                    f"Rate limit exception [{label}] "
                    f"attempt {attempt}/{MAX_RETRIES}. "
                    f"Waiting {wait}s..."
                )

                time.sleep(wait)

            else:

                log(
                    f"Candle error [{label}] "
                    f"attempt {attempt}: {e}"
                )

                if attempt < MAX_RETRIES:
                    time.sleep(2 * attempt)
                else:
                    return None

    return None


# -----------------------------
# DAILY VOLUME SCAN
# -----------------------------

def scan_volume_stock(obj, symbol, token):

    from_date = (
        datetime.now() -
        timedelta(days=DAILY_LOOKBACK_DAYS)
    ).strftime("%Y-%m-%d %H:%M")

    to_date = datetime.now().strftime(
        "%Y-%m-%d %H:%M"
    )

    data = get_candles(
        obj,
        token,
        "ONE_DAY",
        from_date,
        to_date,
        label=f"{symbol} DAILY"
    )

    if not data:
        return None

    df = candles_to_df(data)

    if df is None or len(df) < 21:
        return None

    df = remove_incomplete_daily(df)

    if df is None or len(df) < 21:
        return None

    last = df.iloc[-1]

    close = float(last["close"])

    if close < MIN_PRICE:
        return None

    avg_volume = (
        df["volume"]
        .tail(20)
        .mean()
    )

    if avg_volume <= 0:
        return None

    if avg_volume < MIN_AVG_VOLUME:
        return None

    volume_x = (
        float(last["volume"]) /
        float(avg_volume)
    )

    if volume_x < MIN_VOLUME_X:
        return None

    return {
        "symbol": symbol,
        "token": token,
        "volume_x": volume_x,
        "daily": df,
    }


# -----------------------------
# PHASE 1
# -----------------------------

def phase_1_high_volume(obj, universe):

    log("=" * 55)
    log("PHASE 1: COMPLETE NSE HIGH-VOLUME SCAN")
    log("=" * 55)

    high_volume = []

    total = len(universe)

    for index, (symbol, token) in enumerate(
        universe,
        start=1
    ):

        result = scan_volume_stock(
            obj,
            symbol,
            token
        )

        if result:
            high_volume.append(result)

            log(
                f"HIGH VOLUME: {symbol} "
                f"{result['volume_x']:.2f}x"
            )

        if index % 50 == 0 or index == total:
            log(
                f"Phase 1 progress: "
                f"{index}/{total} | "
                f"High Volume: {len(high_volume)}"
            )

        # Conservative pacing
        time.sleep(REQUEST_DELAY)

        if index % BATCH_SIZE == 0:
            log(
                f"Batch pause after {index} stocks..."
            )
            time.sleep(BATCH_PAUSE)

    high_volume.sort(
        key=lambda x: x["volume_x"],
        reverse=True
    )

    top_list = high_volume[:TOP_VOLUME_COUNT]

    log(
        f"PHASE 1 COMPLETE: "
        f"{len(high_volume)} high-volume stocks"
    )

    log(
        f"Top {TOP_VOLUME_COUNT}: "
        f"{len(top_list)} stocks"
    )

    return top_list


# -----------------------------
# GET FULL DAILY
# -----------------------------

def get_full_daily(obj, token, symbol):

    from_date = (
        datetime.now() -
        timedelta(days=DAILY_HISTORY_DAYS)
    ).strftime("%Y-%m-%d %H:%M")

    to_date = datetime.now().strftime(
        "%Y-%m-%d %H:%M"
    )

    data = get_candles(
        obj,
        token,
        "ONE_DAY",
        from_date,
        to_date,
        label=f"{symbol} FULL DAILY"
    )

    if not data:
        return None

    df = candles_to_df(data)

    if df is None:
        return None

    df = remove_incomplete_daily(df)

    return df


# -----------------------------
# GET WEEKLY
# -----------------------------

def get_weekly(obj, token, symbol):

    from_date = (
        datetime.now() -
        timedelta(days=WEEKLY_HISTORY_DAYS)
    ).strftime("%Y-%m-%d %H:%M")

    to_date = datetime.now().strftime(
        "%Y-%m-%d %H:%M"
    )

    data = get_candles(
        obj,
        token,
        "ONE_WEEK",
        from_date,
        to_date,
        label=f"{symbol} WEEKLY"
    )

    if not data:
        return None

    df = candles_to_df(data)

    if df is None:
        return None

    return df


# -----------------------------
# PHASE 2
# DAILY + WEEKLY SETUP
# -----------------------------

def phase_2_setup_scan(obj, top_volume_list):

    log("=" * 55)
    log("PHASE 2: DAILY + WEEKLY ANGEL API CHECK")
    log("=" * 55)

    picks = []

    total = len(top_volume_list)

    for index, item in enumerate(
        top_volume_list,
        start=1
    ):

        symbol = item["symbol"]
        token = item["token"]

        log(
            f"[{index}/{total}] "
            f"Reading Daily + Weekly: {symbol}"
        )

        # -------------------------
        # FULL DAILY
        # -------------------------

        daily = get_full_daily(
            obj,
            token,
            symbol
        )

        time.sleep(REQUEST_DELAY)

        if daily is None or len(daily) < 200:
            log(
                f"{symbol}: insufficient daily data"
            )
            continue

        # -------------------------
        # WEEKLY
        # -------------------------

        weekly = get_weekly(
            obj,
            token,
            symbol
        )

        time.sleep(REQUEST_DELAY)

        if weekly is None or len(weekly) < 20:
            log(
                f"{symbol}: insufficient weekly data"
            )
            continue

        # -------------------------
        # DAILY INDICATORS
        # -------------------------

        last = daily.iloc[-1]

        close = float(last["close"])
        open_price = float(last["open"])
        low = float(last["low"])

        dma200 = (
            daily["close"]
            .rolling(200)
            .mean()
            .iloc[-1]
        )

        if pd.isna(dma200):
            log(
                f"{symbol}: 200 DMA unavailable"
            )
            continue

        # -------------------------
        # 52 WEEK HIGH
        # -------------------------

        last_252 = daily.tail(252)

        if len(last_252) == 0:
            continue

        high_52w = float(
            last_252["high"].max()
        )

        # -------------------------
        # WEEKLY TREND
        # -------------------------

        weekly_close = float(
            weekly["close"].iloc[-1]
        )

        weekly_up = (
            weekly_close > float(dma200)
        )

        # -------------------------
        # NEAR 52W HIGH
        # -------------------------

        near_52w_high = (
            close >=
            high_52w * NEAR_52W_PERCENT
        )

        # -------------------------
        # GREEN DAILY CANDLE
        # -------------------------

        green = close > open_price

        # -------------------------
        # FINAL SETUP
        # -------------------------

        if (
            weekly_up
            and near_52w_high
            and green
        ):

            target_1 = close * 1.05
            target_2 = close * 1.08

            picks.append(
                {
                    "Stock": symbol,
                    "LTP": round(close, 2),
                    "52W": round(high_52w, 2),
                    "VolX": round(
                        float(item["volume_x"]),
                        2
                    ),
                    "SL": round(low, 2),
                    "TGT1": round(
                        target_1,
                        2
                    ),
                    "TGT2": round(
                        target_2,
                        2
                    ),
                    "DMA200": round(
                        float(dma200),
                        2
                    ),
                }
            )

            log(
                f"BUY FOUND: {symbol} | "
                f"Vol {item['volume_x']:.2f}x | "
                f"LTP {close:.2f}"
            )

        else:

            log(
                f"{symbol}: No setup"
            )

        # Additional batch pause
        if index % BATCH_SIZE == 0:
            log(
                f"Phase 2 batch pause..."
            )
            time.sleep(BATCH_PAUSE)

    return picks


# -----------------------------
# TELEGRAM MESSAGE
# -----------------------------

def build_message(
    total_nse,
    high_volume_count,
    picks
):

    timestamp = datetime.now().strftime(
        "%d %b %Y %I:%M %p"
    )

    if not picks:

        return (
            f"📉 PURA NSE SWING SCAN\n\n"
            f"Time: {timestamp}\n"
            f"Total NSE Stocks: {total_nse}\n"
            f"High Volume 2x+: {high_volume_count}\n"
            f"Final Setup: 0\n\n"
            f"Daily + Weekly confirmation ke baad "
            f"aaj koi setup nahi mila."
        )

    df = pd.DataFrame(picks)

    df = df.sort_values(
        by="VolX",
        ascending=False
    )

    message = (
        f"🚀 PURA NSE SWING BUY\n\n"
        f"Time: {timestamp}\n"
        f"Total NSE Stocks: {total_nse}\n"
        f"High Volume 2x+: {high_volume_count}\n"
        f"BUY Setup: {len(df)}\n\n"
    )

    for _, row in df.iterrows():

        message += (
            f"📌 {row['Stock']}\n"
            f"LTP: ₹{row['LTP']}\n"
            f"52W High: ₹{row['52W']}\n"
            f"Volume: {row['VolX']}x\n"
            f"200 DMA: ₹{row['DMA200']}\n"
            f"SL: ₹{row['SL']}\n"
            f"T1: ₹{row['TGT1']}\n"
            f"T2: ₹{row['TGT2']}\n\n"
        )

    message += (
        "⚠️ Scanner signal only. "
        "No automatic order placement."
    )

    return message


# -----------------------------
# MAIN
# -----------------------------

def main():

    start_time = time.time()

    log("=" * 60)
    log("CHANDAN PURA NSE HIGH-VOLUME SWING SCANNER")
    log("=" * 60)

    validate_secrets()

    # -------------------------
    # LOGIN
    # -------------------------

    obj = login_angel()

    # -------------------------
    # NSE UNIVERSE
    # -------------------------

    universe = load_nse_universe()

    if not universe:
        raise RuntimeError(
            "NSE universe is empty."
        )

    # -------------------------
    # PHASE 1
    # -------------------------

    top_volume_list = phase_1_high_volume(
        obj,
        universe
    )

    if not top_volume_list:

        message = (
            f"📉 PURA NSE SCAN\n\n"
            f"Total NSE Stocks: {len(universe)}\n"
            f"High Volume 2x+: 0\n"
            f"Final Setup: 0\n\n"
            f"Aaj koi stock 2x volume condition "
            f"pass nahi hua."
        )

        print(message, flush=True)
        send_telegram(message)
        return

    log(
        "Top volume stocks: "
        + ", ".join(
            x["symbol"]
            for x in top_volume_list[:20]
        )
    )

    # -------------------------
    # PHASE 2
    # -------------------------

    picks = phase_2_setup_scan(
        obj,
        top_volume_list
    )

    # -------------------------
    # TELEGRAM
    # -------------------------

    message = build_message(
        total_nse=len(universe),
        high_volume_count=len(top_volume_list),
        picks=picks
    )

    print("\n" + message, flush=True)

    send_telegram(message)

    elapsed = (
        time.time() -
        start_time
    )

    log(
        f"DONE - Scan completed in "
        f"{elapsed / 60:.1f} minutes"
    )


if __name__ == "__main__":
    main()
