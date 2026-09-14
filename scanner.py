import os, time, requests, pyotp, pandas as pd
from datetime import datetime, timedelta
from SmartApi import SmartConnect

API_KEY = os.getenv("API_KEY")
CLIENT_ID = os.getenv("CLIENT_ID")
PASSWORD = os.getenv("PASSWORD")
TOTP_SECRET = os.getenv("TOTP_SECRET")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

MIN_PRICE = 100
MIN_AVG_VOL = 300000
VOL_X = 3.0
NEAR_ATH_PERC = 5

def send_telegram(msg):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"}, timeout=10)
    except:
        pass

print("Login ho raha hai...")
obj = SmartConnect(api_key=API_KEY)
totp = pyotp.TOTP(TOTP_SECRET).now()
obj.generateSession(CLIENT_ID, PASSWORD, totp)
print("Login OK")

print("Total NSE Stocks load ho rahe hain Angel se...")
scrip_master = requests.get("https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json").json()
ALL_TOKENS = []
for i in scrip_master:
    if i['exch_seg'] == 'NSE' and i['symbol'].endswith("-EQ"):
        ALL_TOKENS.append({"sym": i['symbol'].replace("-EQ",""), "token": i['token']})

print(f"Total Stocks: {len(ALL_TOKENS)}")
print("Scanning Start...")

def get_candles(token, interval):
    try:
        param = {
            "exchange": "NSE", "symboltoken": token, "interval": interval,
            "fromdate": (datetime.now() - timedelta(days=400)).strftime("%Y-%m-%d %H:%M"),
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
count = 0
for item in ALL_TOKENS:
    count += 1
    if count % 100 == 0:
        print(f"Checked {count}/{len(ALL_TOKENS)}...")
    sym = item['sym']
    token = item['token']
    daily = get_candles(token, "ONE_DAY")
    if daily is None or len(daily) < 200:
        continue
    last = daily.iloc[-1]
    avg_vol = daily['volume'].tail(20).mean()
    if last['close'] < MIN_PRICE:
        continue
    if avg_vol < MIN_AVG_VOL:
        continue
    weekly = get_candles(token, "ONE_WEEK")
    if weekly is None or len(weekly) < 50:
        continue
    high_52w = daily['high'].max()
    dma200 = daily['close'].rolling(200).mean().iloc[-1]
    if pd.isna(dma200):
        continue
    weekly_uptrend = weekly['close'].iloc[-1] > dma200
    near_ath = last['close'] >= high_52w * (1 - NEAR_ATH_PERC/100)
    green_candle = last['close'] > last['open']
    vol_blast = last['volume'] > (avg_vol * VOL_X)
    if weekly_uptrend and near_ath and green_candle and vol_blast:
        if last['close'] >= high_52w * 0.995:
            picks.append({
                "Stock": sym,
                "LTP": round(float(last['close']), 2),
                "52W_High": round(float(high_52w), 2),
                "Vol_X": round(float(last['volume']/avg_vol), 2),
                "SL": round(float(daily['low'].iloc[-1]), 2)
            })
            print(f"FOUND: {sym}")
    time.sleep(0.2)

if len(picks) == 0:
    msg = f"📉 Chandan Scan - {datetime.now().strftime('%d %b %H:%M')}\nScanned: {len(ALL_TOKENS)} Stocks\n\nAaj koi tight setup nahi mila (ATH + 3x Vol + Weekly Uptrend)\nFilter tight hai isliye 0 aana normal hai."
else:
    df = pd.DataFrame(picks).sort_values(by="Vol_X", ascending=False)
    msg = f"🚀 CHANDAN BUY ALERT - {datetime.now().strftime('%d %b %H:%M')} 🚀\nScanned: {len(ALL_TOKENS)} | Found: {len(df)}\n\n"
    for _, row in df.iterrows():
        t1 = round(row['LTP'] * 1.04, 2)
        t2 = round(row['LTP'] * 1.06, 2)
        msg += f"*{row['Stock']}* - LTP:{row['LTP']} (52W:{row['52W_High']})\nVol: {row['Vol_X']}x | SL: {row['SL']}\nTGT: {t1} / {t2}\n\n"
    msg += "_Logic: Daily ATH Break + 3x Vol + Weekly > 200DMA_"

print(msg)
send_telegram(msg)
print("[Program finished]")
