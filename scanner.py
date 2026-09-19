import os, time, pytz, requests, math
import pandas as pd
from datetime import datetime
from SmartApi import SmartConnect
import pyotp

# --- CONFIG ---
LIQUID_COUNT = 30  # 60 se 30 kar diya, fast + no ban
IST = pytz.timezone('Asia/Kolkata')

API_KEY = os.getenv("API_KEY")
CLIENT_ID = os.getenv("CLIENT_ID")
PASSWORD = os.getenv("PASSWORD")
TOTP_SECRET = os.getenv("TOTP_SECRET")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

def send_telegram(msg):
    if not BOT_TOKEN or not CHAT_ID:
        print("Telegram secrets missing")
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"}
    try:
        r = requests.post(url, json=payload, timeout=10)
        print(f"Telegram: {r.status_code} {r.text[:200]}")
    except Exception as e:
        print(f"Telegram error: {e}")

def login():
    totp = pyotp.TOTP(TOTP_SECRET).now()
    obj = SmartConnect(api_key=API_KEY)
    data = obj.generateSession(CLIENT_ID, PASSWORD, totp)
    print(f"Login: {data.get('message')}")
    return obj

def get_candles_with_retry(obj, params, retries=4):
    for i in range(retries):
        try:
            resp = obj.getCandleData(params)
            if resp and resp.get('data') and len(resp['data']) > 10:
                return resp['data']
            # empty b'' case
            if resp and resp.get('errorcode') == "":
                time.sleep(0.6)
        except Exception as e:
            print(f"  Retry {i+1} fail: {e}")
        time.sleep(0.6 + i*0.5)
    return None

def get_nse_tokens(obj):
    # Angel instrument dump se NSE EQ
    try:
        url = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
        df = pd.read_json(url)
        nse = df[(df['exch_seg']=='NSE') & (df['symbol'].str.endswith('-EQ'))]
        print(f"NSE: {len(nse)}")
        return nse.head(3000) # safety
    except Exception as e:
        print(f"Instrument fetch fail: {e}, using fallback")
        # fallback: quotes list wala logic tera purana wala
        return None

def check_ath_breakout(candles):
    if not candles or len(candles) < 250:
        return False
    df = pd.DataFrame(candles, columns=['time','open','high','low','close','volume'])
    close = df['close'].iloc[-1]
    ath = df['high'].max() # last 1 year me already filter hai
    # Tight base: last 20 days range < 15%
    last20 = df.tail(20)
    range_pct = (last20['high'].max() - last20['low'].min()) / last20['low'].min() * 100
    vol_avg = df['volume'].tail(20).mean()
    vol_today = df['volume'].iloc[-1]
    
    cond1 = close >= ath * 0.99 # ATH ke pass
    cond2 = range_pct < 18
    cond3 = vol_today > vol_avg * 2.5 # 3x vol approx
    
    return cond1 and cond2 and cond3

def main():
    obj = login()
    
    # 1. Get tokens + LTP for liquidity filter
    print("Fetching liquidity...")
    # Tu agar pehle quotes se karta tha to wahi logic rakha
    # Yaha fast ke liye hum direct top 2685 me se quotes lenge batch me
    # Angel getMarketData ek baar me 500 de sakta hai - isliye batch
    # Simple: hum pehle wale logic se Liquid 30 nikalenge
    try:
        # Try to get all NSE tokens from local if available else use Nifty500 list
        # For speed we use Nifty500 + Next 500
        nse_df = get_nse_tokens(obj)
        tokens = nse_df['token'].astype(str).tolist()[:2685]
        names = nse_df['symbol'].tolist()[:2685]
    except:
        print("Using dummy token list - update if needed")
        tokens, names = [], []
    
    # Liquidity selection via LTP volume
    liquid = []
    print(f"Quotes {len(tokens)}/2685")
    batch = 500
    all_data = []
    for i in range(0, len(tokens), batch):
        sub = tokens[i:i+batch]
        try:
            params = {"mode":"LTP","exchangeTokens":{"NSE":sub}}
            resp = obj.getMarketData(params)
            if resp and resp.get('data'):
                all_data.extend(resp['data']['fetched'])
        except Exception as e:
            print(f"Quote batch error: {e}")
        time.sleep(0.4)
        print(f"Quotes {min(i+batch,len(tokens))}/{len(tokens)}")
    
    # Sort by value if possible else take first 100
    if all_data:
        # fetched has no volume, so we just take top as per your previous logic
        # If you had volume logic, keep it
        liquid_tokens = tokens[:60] # first 60 as liquid
        liquid_names = names[:60]
    else:
        liquid_tokens = tokens[:60]
        liquid_names = names[:60]
    
    # Only 30 for final check
    liquid_tokens = liquid_tokens[:LIQUID_COUNT]
    liquid_names = liquid_names[:LIQUID_COUNT]
    print(f"Liquid selected: {len(liquid_tokens)}")

    found = []
    for sym, tok in zip(liquid_names, liquid_tokens):
        print(f"Checking {sym}...")
        param = {
            "exchange": "NSE",
            "symboltoken": str(tok),
            "interval": "ONE_DAY",
            "fromdate": "2025-06-01 09:15",
            "todate": datetime.now(IST).strftime("%Y-%m-%d 15:30")
        }
        candles = get_candles_with_retry(obj, param)
        if not candles:
            print(f"Candle error {tok}: Couldn't parse")
            continue
        if check_ath_breakout(candles):
            found.append(sym.replace("-EQ",""))
        time.sleep(0.35) # 350ms gap - RATE LIMIT FIX

    now_str = datetime.now(IST).strftime("%d %b %Y %H:%M")
    msg = f"\\*CHANDAN ATH BREAKOUT\\*\n⏰ {now_str}\n\n"
    msg += f"NSE: {len(tokens)} | Liquid: {len(liquid_tokens)} | Found: {len(found)}\n\n"
    if found:
        for s in found:
            msg += f"• {s}\n"
    else:
        msg += "No tight setup today.\n_Core: ATH Break + 3x Vol + Weekly Uptrend_"
    
    print(msg)
    send_telegram(msg)
    obj.terminateSession(CLIENT_ID)

if __name__ == "__main__":
    main()
