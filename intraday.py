import os, json, time, requests
from datetime import datetime, timedelta
import pandas as pd
import pyotp
from SmartApi import SmartConnect
import pytz

# ========== INTRADAY NEW V5.0 - 9:15 to 9:35 QUALITY (2-3 Signals) ==========
# Logic: Nifty 2600 -> Vol Top 80 -> OHL 0.06% + VWAP + Nifty + Vol 1.5x

API_KEY = os.getenv("API_KEY") or os.getenv("SMARTAPI_API_KEY")
CLIENT_ID = os.getenv("CLIENT_ID") or os.getenv("SMARTAPI_CLIENT_ID")
PASSWORD = os.getenv("PASSWORD") or os.getenv("SMARTAPI_PASSWORD")
TOTP_SECRET = os.getenv("TOTP_SECRET") or os.getenv("SMARTAPI_TOTP_SECRET")
TELE_BOT = os.getenv("TELEGRAM_BOT_TOKEN")
TELE_CHAT = os.getenv("TELEGRAM_CHAT_ID")

IST = pytz.timezone('Asia/Kolkata')
DELAY = 0.3
TOP_N = 80
VOL_THRESHOLD = 1.5
OHL_BUFFER_PCT = 0.06

def log(m): print(m, flush=True)

def send_telegram(msg):
    if not TELE_BOT or not TELE_CHAT: return
    try:
        url = f"https://api.telegram.org/bot{TELE_BOT}/sendMessage"
        requests.post(url, json={"chat_id": TELE_CHAT, "text": msg, "parse_mode": "Markdown"}, timeout=10)
    except Exception as e:
        log(f"Telegram fail: {e}")

def get_obj():
    if not all([API_KEY, CLIENT_ID, PASSWORD, TOTP_SECRET]):
        raise Exception("Secrets missing!")
    clean_secret = TOTP_SECRET.strip().replace(" ", "")
    obj = SmartConnect(api_key=API_KEY.strip())
    totp = pyotp.TOTP(clean_secret).now()
    data = obj.generateSession(CLIENT_ID.strip(), PASSWORD.strip(), totp)
    if data and data.get("status"):
        log(f"Login OK - {datetime.now(IST).strftime('%H:%M:%S IST')}")
        return obj
    raise Exception(f"Login failed: {data}")

def load_master():
    local = "OpenAPIScripMaster.json"
    if os.path.exists(local):
        try:
            with open(local, "r") as f:
                data = json.load(f)
            if len(data) > 1000: return data
        except: pass
    url = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
    r = requests.get(url, timeout=60)
    data = r.json()
    with open(local, "w") as f: json.dump(data, f)
    return data

def get_candle(obj, token, interval, days):
    to_date = datetime.now(IST)
    from_date = to_date - timedelta(days=days)
    params = {
        "exchange": "NSE",
        "symboltoken": str(token),
        "interval": interval,
        "fromdate": from_date.strftime("%Y-%m-%d %H:%M"),
        "todate": to_date.strftime("%Y-%m-%d %H:%M")
    }
    for _ in range(3):
        try:
            res = obj.getCandleData(params)
            if res and res.get("status") and res.get("data"):
                df = pd.DataFrame(res["data"], columns=["time", "open", "high", "low", "close", "volume"])
                df["time"] = pd.to_datetime(df["time"], utc=True).dt.tz_convert(None)
                for c in ["open", "high", "low", "close", "volume"]:
                    df[c] = pd.to_numeric(df[c], errors="coerce")
                return df.dropna().sort_values("time").reset_index(drop=True)
            return None
        except:
            time.sleep(1)
            continue
    return None

def calc_rsi(series, period=14):
    delta = series.diff()
    gain = delta.where(delta > 0, 0).rolling(period).mean()
    loss = -delta.where(delta < 0, 0).rolling(period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def main():
    log("="*60)
    log(f"INTRADAY NEW V5.0 START - {datetime.now(IST).strftime('%d %b %H:%M:%S IST')}")
    log("="*60)

    obj = get_obj()
    master = load_master()
    nse_stocks = [s for s in master if s.get("exch_seg") == "NSE" and str(s.get("symbol","")).endswith("-EQ")]
    log(f"Total NSE EQ: {len(nse_stocks)}")

    # STEP 0: Nifty Trend
    log("\nSTEP 0: Nifty Trend...")
    nifty_trend = "UP"
    nifty_change = 0
    try:
        # Nifty 50 token
        nifty_df = get_candle(obj, "99926000", "ONE_MINUTE", 1)
        if nifty_df is not None and len(nifty_df) > 5:
            n_open = nifty_df.iloc[0]['open']
            n_now = nifty_df.iloc[-1]['close']
            nifty_change = (n_now - n_open) / n_open * 100
            if nifty_change > 0.1: nifty_trend = "UP"
            elif nifty_change < -0.1: nifty_trend = "DOWN"
            else: nifty_trend = "FLAT"
            log(f"Nifty Trend: {nifty_trend} ({nifty_change:.2f}%)")
        else:
            log("Nifty data not found, default UP")
    except Exception as e:
        log(f"Nifty error: {e}, default UP")

    # PHASE 1: FAST VOLUME SCAN (Daily)
    log(f"\nPHASE 1: Fast Volume Scan {VOL_THRESHOLD}x...")
    candidates = []
    for idx, s in enumerate(nse_stocks):
        if idx % 600 == 0:
            log(f" Scanning {idx}/{len(nse_stocks)} | Found: {len(candidates)}")
        df = get_candle(obj, s["token"], "ONE_DAY", 30)
        time.sleep(0.12) # Fast for phase 1
        if df is None or len(df) < 22: continue
        try:
            avg_vol = df["volume"].iloc[-21:-1].mean()
            curr_vol = df["volume"].iloc[-1]
            if avg_vol > 0 and curr_vol / avg_vol >= VOL_THRESHOLD:
                candidates.append((s, curr_vol / avg_vol))
        except: continue

    candidates = sorted(candidates, key=lambda x: x[1], reverse=True)[:TOP_N]
    log(f"PHASE 1 DONE: Top {len(candidates)} candidates (out of {len(nse_stocks)})")
    if not candidates:
        send_telegram(f"⚠️ INTRADAY V5 - No volume candidates\nNSE: {len(nse_stocks)}\nTime: {datetime.now(IST).strftime('%H:%M IST')}")
        return

    # PHASE 2: INTRADAY QUALITY (1-min)
    log(f"\nPHASE 2: Intraday Quality Check (OHL 0.06% + VWAP + RSI)...")
    picks = []

    for count, (stock, volx) in enumerate(candidates, 1):
        sym = stock.get("name") or stock.get("symbol","").replace("-EQ","")
        df1 = get_candle(obj, stock["token"], "ONE_MINUTE", 1)
        time.sleep(DELAY)
        if df1 is None or len(df1) < 15:
            # log(f"[{count}] {sym} No 1-min data")
            continue
        try:
            # Indicators
            typical = (df1['high'] + df1['low'] + df1['close']) / 3
            df1['vwap'] = (typical * df1['volume']).cumsum() / df1['volume'].cumsum()
            df1['rsi'] = calc_rsi(df1['close'])
            df1['avg_vol'] = df1['volume'].rolling(20).mean()
            df1['tr'] = pd.concat([df1['high']-df1['low'], abs(df1['high']-df1['close'].shift()), abs(df1['low']-df1['close'].shift())], axis=1).max(axis=1)
            df1['atr_pct'] = df1['tr'].rolling(14).mean() / df1['close'] * 100

            last = df1.iloc[-1]
            prev = df1.iloc[-2]

            # FILTER 1: OHL with 0.06% buffer
            buffer_val = last['open'] * OHL_BUFFER_PCT / 100
            is_open_low = abs(last['open'] - last['low']) <= buffer_val
            is_open_high = abs(last['open'] - last['high']) <= buffer_val
            if not (is_open_low or is_open_high): continue

            # FILTER 2: ATR 1.5% - 4%
            if pd.isna(last['atr_pct']) or not (1.5 <= last['atr_pct'] <= 4.0): continue

            # FILTER 3: No consolidation at VWAP
            dist_vwap = abs(last['close'] - last['vwap']) / last['vwap'] * 100
            if dist_vwap < 0.2: continue

            # FILTER 4: Volume Surge 1.5x
            if pd.isna(last['avg_vol']) or last['volume'] < last['avg_vol'] * 1.5: continue

            # FILTER 5: VWAP Crossover + RSI + Nifty
            vwap_cross_up = prev['close'] < prev['vwap'] and last['close'] > last['vwap']
            vwap_cross_down = prev['close'] > prev['vwap'] and last['close'] < last['vwap']

            signal = None
            if is_open_low and last['close'] > last['vwap'] and 40 < last['rsi'] < 70:
                if nifty_trend in ["UP", "FLAT"]: signal = "LONG"
            elif is_open_high and last['close'] < last['vwap']:
                if nifty_trend == "DOWN": signal = "SHORT"

            if not signal: continue

            # Quality Score
            score = 30 # OHL
            if vwap_cross_up or vwap_cross_down: score += 25
            else: score += 15
            if nifty_trend == signal.replace("LONG","UP").replace("SHORT","DOWN"): score += 20
            score += min(volx * 4, 15)
            if 55 < last['rsi'] < 65: score += 10

            picks.append({
                "sym": sym,
                "type": signal,
                "close": float(last['close']),
                "vwap": float(last['vwap']),
                "rsi": float(last['rsi']),
                "volx": float(volx),
                "score": float(score),
                "pattern": "O=L" if is_open_low else "O=H",
                "atr": float(last['atr_pct'])
            })
            log(f"[{count}] ✅ {sym} {signal} Score:{score:.0f} {('O=L' if is_open_low else 'O=H')} Vol:{volx:.1f}x RSI:{last['rsi']:.0f}")

        except Exception as e:
            log(f"[{count}] {sym} Error: {e}")
            continue

    picks = sorted(picks, key=lambda x: x['score'], reverse=True)[:3]
    log(f"\nFINAL PICKS: {len(picks)}")

    if picks:
        lines = []
        for i, p in enumerate(picks, 1):
            stars = "⭐⭐⭐⭐⭐" if p['score'] >= 85 else "⭐⭐⭐⭐" if p['score'] >= 70 else "⭐⭐⭐"
            lines.append(f"*#{i} {p['sym']} {p['type']} {stars}*\nLTP: ₹{p['close']:.1f} VWAP: ₹{p['vwap']:.1f} | RSI: {p['rsi']:.0f}\nVol: {p['volx']:.1f}x | {p['pattern']} | ATR: {p['atr']:.1f}% | Score: {p['score']:.0f}/100\n")

        msg = f"🎯 *INTRADAY V5.0 - {datetime.now(IST).strftime('%d %b %H:%M IST')}* 🎯\nNifty: {nifty_trend} ({nifty_change:.2f}%)\nScanned: {len(nse_stocks)} → Top {len(candidates)} → Final {len(picks)}\n\n" + "\n".join(lines) + "\n_Logic: OHL 0.06% + VWAP Cross + Vol 1.5x + Nifty_"
        send_telegram(msg)
        log("Telegram sent")
    else:
        msg = f"⚠️ *INTRADAY V5 - NO QUALITY PICKS*\nTime: {datetime.now(IST).strftime('%H:%M IST')}\nNifty: {nifty_trend} ({nifty_change:.2f}%)\nScanned: {len(nse_stocks)} → Top {len(candidates)}\nAll failed OHL+VWAP+Nifty filter"
        send_telegram(msg)

if __name__ == "__main__":
    main()
