import os, pyotp, time, datetime
import requests
import pandas as pd
from SmartApi import SmartConnect

API_KEY = os.getenv("API_KEY")
CLIENT_ID = os.getenv("CLIENT_ID")
PASSWORD = os.getenv("PASSWORD")
TOTP_SECRET = os.getenv("TOTP_SECRET")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

def login():
    totp = pyotp.TOTP(TOTP_SECRET).now()
    obj = SmartConnect(api_key=API_KEY)
    data = obj.generateSession(CLIENT_ID, PASSWORD, totp)
    return obj

def send_telegram(msg):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    requests.post(url, data={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"}, timeout=15)

# --- Yaha apni list daal de ---
STOCKS = {
    "RELIANCE-EQ": "2885",
    "TCS-EQ": "11536",
    "INFY-EQ": "1594",
    "HDFCBANK-EQ": "1333",
    "ICICIBANK-EQ": "4963"
    # tu apni puri Nifty 200 list yaha add kar sakta hai
}

def get_52w_data(smart, token):
    try:
        to_date = datetime.datetime.now()
        from_date = to_date - datetime.timedelta(days=365)
        params = {
            "exchange": "NSE",
            "symboltoken": token,
            "interval": "ONE_DAY",
            "fromdate": from_date.strftime("%Y-%m-%d %H:%M"),
            "todate": to_date.strftime("%Y-%m-%d %H:%M")
        }
        candles = smart.getCandleData(params)
        if candles and candles.get('data'):
            df = pd.DataFrame(candles['data'], columns=['date','open','high','low','close','volume'])
            high_52w = df['high'].max()
            low_52w = df['low'].min()
            ltp = df['close'].iloc[-1]
            return ltp, high_52w, low_52w
    except:
        pass
    return None, None, None

def main():
    smart = login()
    print("Login OK")
    results = []
    for name, token in STOCKS.items():
        ltp, h52, l52 = get_52w_data(smart, token)
        if not ltp: continue
        # Check breakout within 2%
        if ltp >= h52 * 0.98:
            entry = ltp
            sl = round(entry * 0.97, 2)
            tgt = round(entry * 1.08, 2)
            results.append(f"*{name}* 🔥 52W HIGH ke paas\nLTP: {entry} | 52W High: {h52}\nEntry: {entry}\nSL: {sl}\nTGT: {tgt}")
        elif ltp <= l52 * 1.02:
            entry = ltp
            sl = round(entry * 1.03, 2)
            tgt = round(entry * 0.92, 2)
            results.append(f"*{name}* 💧 52W LOW ke paas\nLTP: {entry} | 52W Low: {l52}\nEntry: {entry}\nSL: {sl}\nTGT: {tgt}")
        time.sleep(0.3)

    if results:
        msg = f"🚀 *52 Week High Low - {datetime.datetime.now().strftime('%d-%m-%Y')}*\n\n" + "\n\n".join(results[:10])
    else:
        msg = f"🚀 *52 Week Scanner - {datetime.datetime.now().strftime('%d-%m-%Y')}*\nNo breakout today"
    
    send_telegram(msg)
    print(msg)

if __name__=="__main__":
    main()
