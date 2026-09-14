import os, time, requests, pyotp, pandas as pd
from datetime import datetime, timedelta
from SmartApi import SmartConnect

API_KEY = os.getenv("API_KEY")
CLIENT_ID = os.getenv("CLIENT_ID")
PASSWORD = os.getenv("PASSWORD")
TOTP_SECRET = os.getenv("TOTP_SECRET")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

def send_telegram(msg):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"}, timeout=15)
    except Exception as e:
        print(f"Telegram fail: {e}")

def get_nse_high_volume():
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://www.nseindia.com/market-data/volume-gainers",
        "Accept-Language": "en-US,en;q=0.9",
    }
    try:
        print("Step 1: NSE se High Volume list...", flush=True)
        s = requests.Session()
        s.headers.update(headers)
        s.get("https://www.nseindia.com", timeout=15)
        time.sleep(1.5)
        # Try volume-gainers API
        r = s.get("https://www.nseindia.com/api/live-analysis/volume-gainers", timeout=15)
        if r.status_code == 200:
            j = r.json()
            if 'data' in j and len(j['data']) > 10:
                syms = [d['symbol'] for d in j['data'][:50]]
                print(f"NSE OK: {len(syms)} stocks", flush=True)
                return syms
    except Exception as e:
        print(f"NSE fail: {e}", flush=True)
    
    print("NSE blocked in cloud, fallback to Top 200 Nifty scan", flush=True)
    return []

print("Login to Angel...", flush=True)
obj = SmartConnect(api_key=API_KEY)
totp = pyotp.TOTP(TOTP_SECRET).now()
obj.generateSession(CLIENT_ID, PASSWORD, totp)
print("Angel Login OK", flush=True)

print("Token Master load...", flush=True)
master = requests.get("https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json", timeout=20).json()
token_map = {x['symbol'].replace("-EQ",""): x['token'] for x in master if x['exch_seg']=='NSE' and x['symbol'].endswith("-EQ")}
print(f"Total tokens: {len(token_map)}", flush=True)

# STEP 1
nse_list = get_nse_high_volume()

# Fallback - sirf 200 tak, 600 nahi - isse 11 min ki jagah 3 min lagega
if not nse_list:
    nse_list = list(token_map.keys())[:200]

print(f"Step 2: Angel Check {len(nse_list)} stocks - Daily+Weekly", flush=True)

def get_candles(token, interval, days=400):
    try:
        param = {
            "exchange": "NSE", "symboltoken": token, "interval": interval,
            "fromdate": (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M"),
            "todate": datetime.now().strftime("%Y-%m-%d %H:%M")
        }
        res = obj.getCandleData(param)
        if res and res.get('data'):
            df = pd.DataFrame(res['data'], columns=['time','open','high','low','close','volume'])
            return df
    except Exception as e:
        pass
    return None

picks = []
for idx, sym in enumerate(nse_list, 1):
    token = token_map.get(sym)
    if not token:
        continue
    if idx % 20 == 0:
        print(f"Angel Check {idx}/{len(nse_list)}: {sym}", flush=True)

    daily = get_candles(token, "ONE_DAY", 380)
    if daily is None or len(daily) < 200:
        time.sleep(0.2)
        continue
    
    last = daily.iloc[-1]
    if last['close'] < 100:
        time.sleep(0.2)
        continue
    
    avg_vol = daily['volume'].tail(20).mean()
    if avg_vol < 100000:
        time.sleep(0.2)
        continue
    vol_x = float(last['volume'] / avg_vol) if avg_vol>0 else 0
    if vol_x < 2.0:
        time.sleep(0.2)
        continue

    weekly = get_candles(token, "ONE_WEEK", 600)
    if weekly is None or len(weekly) < 40:
        time.sleep(0.2)
        continue

    dma200 = daily['close'].rolling(200).mean().iloc[-1]
    if pd.isna(dma200):
        time.sleep(0.2)
        continue

    high_52w = daily['high'].tail(252).max()
    near_ath = last['close'] >= high_52w * 0.96
    weekly_uptrend = weekly['close'].iloc[-1] > dma200
    green = last['close'] > last['open']

    if weekly_uptrend and near_ath and green and vol_x >= 2.5:
        picks.append({
            "Stock": sym,
            "LTP": round(float(last['close']),2),
            "52W": round(float(high_52w),2),
            "VolX": round(float(vol_x),2),
            "SL": round(float(daily['low'].iloc[-1]),2)
        })
        print(f"FOUND: {sym} {vol_x:.1f}x", flush=True)

    time.sleep(0.25)

if not picks:
    msg = f"📉 *Chandan Scan {datetime.now().strftime('%d %b %I:%M %p')}*\nScanned: {len(nse_list)} (2-Step)\nNo setup today\n\n_Filter: NSE Vol>2x + ATH 96% + Weekly>200DMA + Green_"
else:
    df = pd.DataFrame(picks).sort_values(by="VolX", ascending=False)
    msg = f"🚀 *CHANDAN BUY - {datetime.now().strftime('%d %b %I:%M %p')}* 🚀\nScanned: {len(nse_list)} -> Found: {len(df)}\n\n"
    for _, r in df.head(10).iterrows():
        msg += f"*{r['Stock']}* LTP:{r['LTP']} (52W:{r['52W']})\nVol: {r['VolX']}x | SL: {r['SL']}\nTGT: {round(r['LTP']*1.05,2)} / {round(r['LTP']*1.08,2)}\n\n"
    msg += "_Logic: NSE High Vol + ATH + Weekly Uptrend_"

print(msg, flush=True)
send_telegram(msg)
print("Done", flush=True)
