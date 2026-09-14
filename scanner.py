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
    """Step 1: Direct NSE se volume gainers"""
    try:
        print("Step 1: NSE se High Volume list le raha hu...", flush=True)
        session = requests.Session()
        session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
        })
        # Cookie lene ke liye pehle NSE homepage
        session.get("https://www.nseindia.com", timeout=10)
        time.sleep(1)
        # Volume gainers API
        r = session.get("https://www.nseindia.com/api/live-analysis/volume-gainers", timeout=10)
        if r.status_code == 200:
            data = r.json().get('data', [])
            symbols = [d['symbol'] for d in data[:60]] # Top 60 high volume
            print(f"NSE se mile: {len(symbols)} High Volume stocks: {symbols[:10]}...", flush=True)
            return symbols
    except Exception as e:
        print(f"NSE API fail: {e}, fallback use karunga", flush=True)
    return []

print("Login to Angel...", flush=True)
obj = SmartConnect(api_key=API_KEY)
obj.generateSession(CLIENT_ID, PASSWORD, pyotp.TOTP(TOTP_SECRET).now())
print("Angel Login OK", flush=True)

print("Angel Token Master load ho raha hai...", flush=True)
master = requests.get("https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json").json()
token_map = {x['symbol'].replace("-EQ",""): x['token'] for x in master if x['exch_seg']=='NSE' and x['symbol'].endswith("-EQ")}

# STEP 1: NSE se list
nse_list = get_nse_high_volume()

# Agar NSE fail ho jaye to fallback - last day ke top 500 scan
if not nse_list:
    print("NSE fail, fallback: Top 600 NSE scan", flush=True)
    nse_list = list(token_map.keys())[:600]

print(f"Step 2: Angel se Daily+Weekly chart check - {len(nse_list)} stocks", flush=True)

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
    except:
        pass
    return None

picks = []
for idx, sym in enumerate(nse_list, 1):
    token = token_map.get(sym)
    if not token:
        continue
    if idx % 20 == 0:
        print(f"Angel Check {idx}/{len(nse_list)}: {sym}", flush=True)
    
    daily = get_candles(token, "ONE_DAY")
    if daily is None or len(daily) < 200:
        time.sleep(0.2)
        continue
    
    last = daily.iloc[-1]
    if last['close'] < 100:
        time.sleep(0.2)
        continue
    
    avg_vol = daily['volume'].tail(20).mean()
    vol_x = last['volume'] / avg_vol if avg_vol>0 else 0
    
    if vol_x < 2.0: # NSE se already high vol hai, par fir bhi 2x filter
        time.sleep(0.2)
        continue

    weekly = get_candles(token, "ONE_WEEK", 500)
    if weekly is None or len(weekly) < 30:
        time.sleep(0.2)
        continue
    
    dma200 = daily['close'].rolling(200).mean().iloc[-1]
    high_52w = daily['high'].tail(252).max()
    
    if pd.isna(dma200):
        time.sleep(0.2)
        continue
    
    # Final Logic
    weekly_uptrend = weekly['close'].iloc[-1] > dma200
    near_ath = last['close'] >= high_52w * 0.96  # 52W High ke 4% andar
    green = last['close'] > last['open']
    
    if weekly_uptrend and near_ath and green and vol_x >= 2.5:
        picks.append({
            "Stock": sym, "LTP": round(float(last['close']),2),
            "52W": round(float(high_52w),2), "VolX": round(float(vol_x),2),
            "SL": round(float(daily['low'].iloc[-1]),2)
        })
        print(f"FOUND BUY: {sym} {vol_x:.1f}x Vol", flush=True)
    
    time.sleep(0.25)

if not picks:
    msg = f"📉 *Chandan Scan {datetime.now().strftime('%d %b %H:%M')}*\nStep1 NSE Vol Gainers: {len(nse_list)}\nStep2 Angel Weekly/Daily: No setup\n\n_NSE se high vol aaye par Weekly>200DMA + ATH ke paas koi nahi mila_"
else:
    df = pd.DataFrame(picks).sort_values(by="VolX", ascending=False)
    msg = f"🚀 *CHANDAN 2-STEP BUY - {datetime.now().strftime('%d %b %H:%M')}* 🚀\nNSE High Vol: {len(nse_list)} -> Final: {len(df)}\n\n"
    for _, r in df.iterrows():
        msg += f"*{r['Stock']}* LTP:{r['LTP']} (52W:{r['52W']})\nVol: {r['VolX']}x | SL: {r['SL']}\nTGT: {round(r['LTP']*1.05,2)} / {round(r['LTP']*1.08,2)}\n\n"
    msg += "_Logic: NSE Volume Gainer + Daily ATH + Weekly>200DMA + 2.5x Vol_"

print(msg, flush=True)
send_telegram(msg)
print("Done - Telegram sent", flush=True)
