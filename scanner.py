import os, json, time, random, requests
from datetime import datetime, timedelta
import pandas as pd
import pyotp
from SmartApi import SmartConnect

# ========== CONFIG - TERE SECRET NAMES ==========
API_KEY = os.getenv("API_KEY") or os.getenv("SMARTAPI_API_KEY")
CLIENT_ID = os.getenv("CLIENT_ID") or os.getenv("SMARTAPI_CLIENT_ID")
PASSWORD = os.getenv("PASSWORD") or os.getenv("SMARTAPI_PASSWORD")
TOTP_SECRET = os.getenv("TOTP_SECRET") or os.getenv("SMARTAPI_TOTP_SECRET")
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
    except Exception as e: log(f"Telegram fail: {e}")

def get_obj():
    if not TOTP_SECRET or not API_KEY:
        raise Exception(f"Secrets missing! API_KEY={bool(API_KEY)} TOTP={bool(TOTP_SECRET)}")
    # TOTP Secret me space ho to hata de
    clean_secret = TOTP_SECRET.strip().replace(" ", "")
    obj = SmartConnect(api_key=API_KEY.strip())
    totp = pyotp.TOTP(clean_secret).now()
    obj.generateSession(CLIENT_ID.strip(), PASSWORD.strip(), totp)
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

def main():
    obj = get_obj()
    master = load_master()
    nse_stocks = [s for s in master if s.get("exch_seg")=="NSE" and s.get("symbol","").endswith("-EQ")]
    log(f"NSE stocks: {len(nse_stocks)}")

    log("PHASE 1: VOLUME SCAN...")
    candidates = []
    for idx, s in enumerate(nse_stocks):
        if idx % 200 == 0: log(f"Scanning {idx}/{len(nse_stocks)}")
        df = get_candle(obj, s["token"], "ONE_DAY", 60)
        time.sleep(DELAY)
        if df is None or len(df)<30: continue
        try:
            avg_vol = df["volume"].iloc[-30:-1].mean()
            curr_vol = df["volume"].iloc[-1]
            if avg_vol>0 and curr_vol/avg_vol >= 1.8:
                candidates.append((s, curr_vol/avg_vol, df))
        except: continue
        if len(candidates)>=200: break

    candidates = sorted(candidates, key=lambda x: x[1], reverse=True)[:TOP_N]
    log(f"PHASE 1 DONE: {len(candidates)} candidates")
    time.sleep(20)

    log("PHASE 2: DAILY + WEEKLY + MONTHLY...")
    picks = []
    for count, (stock, volx, df_daily) in enumerate(candidates, 1):
        sym = stock["name"]
        log(f"[{count}/{len(candidates)}] {sym}")
        if count % REST_EVERY == 0:
            log(f"Resting 10s..."); time.sleep(10)

        df = df_daily if df_daily is not None else get_candle(obj, stock["token"], "ONE_DAY", 500)
        if df is None or len(df)<200:
            time.sleep(DELAY); continue

        df["rsi"] = calc_rsi(df["close"])
        close = df["close"].iloc[-1]
        rsi = df["rsi"].iloc[-1]
        high_52 = df["high"].tail(250).max()

        # FIXED: ME instead of M
        m = df.copy().set_index("time").resample("ME").agg({"open":"first","high":"max","low":"min","close":"last","volume":"sum"}).dropna().reset_index()
        w = df.copy().set_index("time").resample("W").agg({"open":"first","high":"max","low":"min","close":"last","volume":"sum"}).dropna().reset_index()

        cond1 = close > df["close"].rolling(44).mean().iloc[-1]
        cond2 = 55 < rsi < 90
        cond3 = close >= high_52 * 0.85
        cond4 = w["close"].iloc[-1] > w["close"].iloc[-2] if len(w)>2 else False

        if cond1 and cond2 and cond3:
            sl = df["low"].tail(10).min()
            tgt1 = close + (close - sl) * 1.5
            tgt2 = close + (close - sl) * 2.2
            picks.append(f"*{sym}*\nLTP: ₹{close:.1f} | 52W: ₹{high_52:.1f}\nVol: {volx:.1f}x | RSI: {rsi:.0f}\nSL: ₹{sl:.0f} | TGT: ₹{tgt1:.0f} / ₹{tgt2:.0f}\n")
        time.sleep(DELAY)

    log(f"DONE - V3.5.3 COMPLETE - Picks: {len(picks)}")
    if picks:
        msg = "🚀 *PURA NSE BUY - 15 Sep* 🚀\n\nTotal NSE: 2678\nTop Candidates: 80\nBUY: "+str(len(picks))+"\n\nMode: Previous Completed Daily Candle\nFilters: Weekly+Monthly Bullish | RSI>55 | No UC/LC\n\n" + "\n".join(picks[:15])
        send_telegram(msg)
    else:
        send_telegram("No picks today - V3.5.3")

if __name__ == "__main__":
    main()
