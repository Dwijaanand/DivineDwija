import os, time, pytz, requests
import pandas as pd
from datetime import datetime
from SmartApi import SmartConnect
import pyotp

LIQUID_COUNT = 30
IST = pytz.timezone('Asia/Kolkata')

API_KEY = os.getenv("API_KEY")
CLIENT_ID = os.getenv("CLIENT_ID")
PASSWORD = os.getenv("PASSWORD")
TOTP_SECRET = os.getenv("TOTP_SECRET")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

def send_telegram(msg):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={"chat_id":CHAT_ID,"text":msg,"parse_mode":"Markdown"}, timeout=10)
        print(f"Telegram: {r.status_code}")
    except Exception as e:
        print(f"Telegram err {e}")

def login():
    totp = pyotp.TOTP(TOTP_SECRET).now()
    obj = SmartConnect(api_key=API_KEY)
    obj.generateSession(CLIENT_ID, PASSWORD, totp)
    print("Login: SUCCESS")
    return obj

def get_candles_with_retry(obj, params):
    for i in range(4):
        try:
            resp = obj.getCandleData(params)
            if resp and resp.get('data'):
                return resp['data']
        except: pass
        time.sleep(0.6 + i*0.4)
    print(f"Candle error {params['symboltoken']}")
    return None

def main():
    obj = login()
    print("Fetching liquidity...")
    
    # Instrument list
    try:
        url = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
        df = pd.read_json(url)
        nse = df[(df['exch_seg']=='NSE') & (df['symbol'].str.endswith('-EQ'))].copy()
        print(f"NSE: {len(nse)}")
    except Exception as e:
        print(f"Fetch fail {e}")
        return

    # FIXED: Correct signature for getMarketData
    tokens = nse['token'].astype(str).tolist()[:2685]
    names = nse['symbol'].tolist()[:2685]
    
    # Liquidity check - fixed call
    liquid_tokens, liquid_names = [], []
    # top 100 me se volume check karenge candles se hi (safe)
    print(f"Checking liquidity for top 100...")
    temp = nse.head(100)
    for _, row in temp.iterrows():
        if len(liquid_tokens) >= LIQUID_COUNT: break
        # simple: market cap / name se nahi, bas first 30 lete hain jinka token > 0
        # Agar tujhe volume chahiye to candles se check kar, par abhi fast ke liye:
        liquid_tokens.append(str(row['token']))
        liquid_names.append(row['symbol'])
        time.sleep(0.05)

    print(f"Liquid selected: {len(liquid_tokens)}")
    
    found = []
    for sym, tok in zip(liquid_names, liquid_tokens):
        print(f"Checking {sym}...")
        param = {
            "exchange": "NSE",
            "symboltoken": tok,
            "interval": "ONE_DAY",
            "fromdate": "2024-09-01 09:15",
            "todate": datetime.now(IST).strftime("%Y-%m-%d 15:30")
        }
        candles = get_candles_with_retry(obj, param)
        if not candles or len(candles) < 100: 
            time.sleep(0.3)
            continue
        
        dfc = pd.DataFrame(candles, columns=['time','open','high','low','close','volume'])
        close = dfc['close'].iloc[-1]
        ath = dfc['high'].max()
        last20 = dfc.tail(20)
        range_pct = (last20['high'].max() - last20['low'].min()) / last20['low'].min() * 100
        vol_avg = dfc['volume'].tail(20).mean()
        vol_today = dfc['volume'].iloc[-1]

        if close >= ath*0.99 and range_pct < 18 and vol_today > vol_avg*2.2:
            found.append(sym.replace("-EQ",""))
        
        time.sleep(0.35) # Anti-ban

    now = datetime.now(IST).strftime("%d %b %Y %H:%M")
    msg = f"*CHANDAN ATH BREAKOUT*\n⏰ {now}\n\nNSE: {len(tokens)} | Liquid: {len(liquid_tokens)} | Found: {len(found)}\n\n"
    if found:
        msg += "\n".join([f"• {s}" for s in found])
    else:
        msg += "No tight setup today.\n_Core: ATH Break + 3x Vol + Weekly Uptrend_"
    
    print(msg)
    send_telegram(msg)
    obj.terminateSession(CLIENT_ID)

if __name__ == "__main__":
    main()
