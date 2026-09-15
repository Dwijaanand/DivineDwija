import os, json, time, random, requests
from datetime import datetime, timedelta
import pandas as pd
import pyotp
from SmartApi import SmartConnect

# ========== CONFIG ==========
API_KEY = os.getenv("SMARTAPI_API_KEY")
CLIENT_ID = os.getenv("SMARTAPI_CLIENT_ID")
PASSWORD = os.getenv("SMARTAPI_PASSWORD")
TOTP_SECRET = os.getenv("SMARTAPI_TOTP_SECRET")
TELE_BOT = os.getenv("TELEGRAM_BOT_TOKEN")
TELE_CHAT = os.getenv("TELEGRAM_CHAT_ID")

DELAY = 1.5
TOP_N = 80
REST_EVERY = 15

def log(m): print(m, flush=True)

def send_telegram(msg):
    if not TELE_BOT or not TELE_CHAT: return
    try:
        url = f"https://api.telegram.org/bot{TELE_BOT}/sendMessage"
        requests.post(url, json={"chat_id": TELE_CHAT, "text": msg, "parse_mode": "Markdown"}, timeout=10)
    except: pass

def get_obj():
    obj = SmartConnect(api_key=API_KEY)
    totp = pyotp.TOTP(TOTP_SECRET).now()
    obj.generateSession(CLIENT_ID, PASSWORD, totp)
    log("Angel Login OK")
    return obj

def load_master():
    if os.path.exists("OpenAPIScripMaster.json"):
        log("Trying local file OpenAPIScripMaster.json...")
        with open("OpenAPIScripMaster.json") as f:
            data = json.load(f)
        log(f"Local master loaded: {len(data)} records")
        return data
    url = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
    for i in range(3):
        try:
            log(f"Downloading master Attempt {i+1}/3...")
            r = requests.get(url, timeout=60)
            data = r.json()
            log(f"Master downloaded: {len(data)} records")
            with open("OpenAPIScripMaster.json","w") as f: json.dump(data,f)
            return data
        except Exception as e:
            log(f"Attempt {i+1} fail: {e}"); time.sleep(5)
    raise Exception("Master failed")

def get_candle(obj, token, interval="ONE_DAY", days=500):
    to_date = datetime.now()
    from_date = to_date - timedelta(days=days)
    params = {
        "exchange": "NSE", "symboltoken": token, "interval": interval,
        "fromdate": from_date.strftime("%Y-%m-%d %H:%M"),
        "todate": to_date.strftime("%Y-%m-%d %H:%M")
    }
    for attempt in range(5):
        try:
            res = obj.getCandleData(params)
            if res and res.get("status") and res.get("data"):
                df = pd.DataFrame(res["data"], columns=["time","open","high","low","close","volume"])
                df["time"] = pd.to_datetime(df["time"])
                return df
            msg = str(res).lower()
            if "rate" in msg or "access" in msg or "exceed" in msg:
                wait = 8 * (attempt+1) + random.randint(1,4)
                log(f" Rate limit. Waiting {wait}s ({attempt+1}/4)")
                time.sleep(wait)
                continue
            return None
        except Exception as e:
            if "rate" in str(e).lower():
                wait = 8 * (attempt+1)
                log(f" Rate limit. Waiting {wait}s ({attempt+1}/4)")
                time.sleep(wait)
            else:
                time.sleep(1)
                return None
    return None

def calc_rsi(series, period=14):
    delta = series.diff()
    gain = (delta.where(delta>0,0)).rolling(period).mean()
    loss = (-delta.where(delta<0,0)).rolling(period).mean()
    rs = gain/loss
    return 100 - (100/(1+rs))

# ========== MAIN ==========
def main():
    obj = get_obj()
    master = load_master()
    nse_stocks = [s for s in master if s.get("exch_seg")=="NSE" and s.get("symbol","").endswith("-EQ")]
    log(f"NSE stocks: {len(nse_stocks)}")

    # PHASE 1 - BULK VOLUME SCAN
    log("PHASE 1: BULK VOLUME SCAN...")
    vol_data = []
    batch_size = 50
    for i in range(0, len(nse_stocks), batch_size):
        batch = nse_stocks[i:i+batch_size]
        tokens = [s["token"] for s in batch]
        try:
            # ltpData bulk
            params = {"exchange":"NSE","tradingsymbol": [s["symbol"] for s in batch],"symboltoken": tokens}
            res = obj.getMarketData(params)
            # fallback to ltp if needed - simplified volume check using candle
            log(f"Bulk quote {min(i+batch_size, len(nse_stocks))}/{len(nse_stocks)}")
        except Exception as e:
            log(f"Bulk fail {e}")
        time.sleep(1)

    # Simplified - Use daily candle for volume spike to avoid bulk limit
    candidates = []
    for idx, s in enumerate(nse_stocks):
        if idx % 200 == 0: log(f"Scanning {idx}/{len(nse_stocks)}")
        df = get_candle(obj, s["token"], "ONE_DAY", 60)
        time.sleep(DELAY)
        if df is None or len(df)<30: continue
        try:
            avg_vol = df["volume"].iloc[-30:-1].mean()
            curr_vol = df["volume"].iloc[-1]
            if avg_vol>0 and curr_vol/avg_vol >= 1.8: # 1.8x volume
                candidates.append((s, curr_vol/avg_vol, df))
        except: continue
        if len(candidates)>=200: break # speed

    candidates = sorted(candidates, key=lambda x: x[1], reverse=True)[:TOP_N]
    log(f"PHASE 1 DONE: {len(candidates)} candidates / {len(nse_stocks)}")
    log(f"TOP: { [c[0]['name'] for c in candidates[:10]] }")

    time.sleep(20) # Gap before Phase 2

    # PHASE 2
    log("PHASE 2: DAILY + WEEKLY + MONTHLY + MOMENTUM CHECK...")
    picks = []
    for count, (stock, volx, df_daily) in enumerate(candidates, 1):
        sym = stock["name"]
        log(f"[{count}/{len(candidates)}] {sym}")

        if count % REST_EVERY == 0:
            log(f"Resting 10s to avoid rate limit...")
            time.sleep(10)

        df = df_daily if df_daily is not None else get_candle(obj, stock["token"], "ONE_DAY", 500)
        if df is None or len(df)<200:
            time.sleep(DELAY)
            continue

        # Daily
        df["rsi"] = calc_rsi(df["close"])
        close = df["close"].iloc[-1]
        rsi = df["rsi"].iloc[-1]
        high_52 = df["high"].tail(250).max()

        # Weekly from daily
        w = df.copy().set_index("time").resample("W").agg({"open":"first","high":"max","low":"min","close":"last","volume":"sum"}).dropna().reset_index()
        # Monthly - FIXED ME
        m = df.copy().set_index("time").resample("ME").agg({"open":"first","high":"max","low":"min","close":"last","volume":"sum"}).dropna().reset_index()

        # Momentum Filters (as per your logic)
        cond1 = close > df["close"].rolling(44).mean().iloc[-1] # above 44 DMA
        cond2 = 55 < rsi < 75
        cond3 = close >= high_52 * 0.85 # near 52W high
        cond4 = w["close"].iloc[-1] > w["close"].iloc[-2] if len(w)>2 else False

        if cond1 and cond2 and cond3:
            # SL / TGT
            sl = df["low"].tail(10).min()
            tgt1 = close + (close - sl) * 1.5
            tgt2 = close + (close - sl) * 2.2
            picks.append(f"*{sym}*\nLTP: ₹{close:.1f} | 52W: ₹{high_52:.1f}\nVol: {volx:.1f}x | RSI: {rsi:.0f}\nSL: ₹{sl:.0f} | TGT: ₹{tgt1:.0f} / ₹{tgt2:.0f}\n")

        time.sleep(DELAY)

    log(f"DONE - V3.5.3 FILTERED SCAN COMPLETE - Picks: {len(picks)}")
    if picks:
        msg = "🔥 *Divine Dwija - Final Picks* 🔥\n\n" + "\n".join(picks[:15])
        send_telegram(msg)
        log(msg)
    else:
        send_telegram("No picks today - V3.5.3")
        log("No picks")

if __name__ == "__main__":
    main()
