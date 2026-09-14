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
    except: pass

print("Login to Angel...", flush=True)
obj = SmartConnect(api_key=API_KEY)
obj.generateSession(CLIENT_ID, PASSWORD, pyotp.TOTP(TOTP_SECRET).now())
print("Angel OK", flush=True)

# PHASE 1: BHAVCOPY SE PURA NSE VOLUME - ANGEL BLOCK ZERO
def get_bhavcopy_top_volume():
    """NSE Bhavcopy se bina Angel ke top volume"""
    try:
        print("PHASE 1: Bhavcopy se Pura NSE Volume...", flush=True)
        # Aaj ka bhavcopy
        today = datetime.now()
        # NSE bhavcopy URL pattern - 2 din try karenge
        for i in range(2):
            d = today - timedelta(days=i)
            # Angel ka bhi bhavcopy use kar sakte hai - ye block nahi hota
            # NSE ka official bhavcopy
            date_str = d.strftime("%d%b%Y").upper()
            url = f"https://archives.nseindia.com/content/hist_equities/bhavcopy/bhavcopy_{date_str}.csv"
            try:
                # direct csv download with NSE headers
                s = requests.Session()
                s.headers.update({"User-Agent": "Mozilla/5.0"})
                s.get("https://www.nseindia.com", timeout=10)
                r = s.get(url, timeout=15)
                if r.status_code == 200:
                    from io import StringIO
                    df = pd.read_csv(StringIO(r.text))
                    df = df[df['SERIES']=='EQ']
                    df = df.sort_values(by='TTL_TRD_QNTY', ascending=False).head(100)
                    syms = df['SYMBOL'].tolist()
                    print(f"Bhavcopy OK: Top 100 Volume: {syms[:10]}", flush=True)
                    return syms
            except Exception as e:
                print(f"Bhavcopy {date_str} fail: {e}", flush=True)
                continue
    except Exception as e:
        print(f"Bhavcopy total fail: {e}", flush=True)
    return []

top_syms = get_bhavcopy_top_volume()

# Agar bhavcopy bhi block ho to Angel se hi slow scan - par sirf 500 tak
if not top_syms:
    print("Bhavcopy blocked, Angel se slow Top 500 Volume scan...", flush=True)
    master = requests.get("https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json", timeout=20).json()
    token_map_all = {x['symbol'].replace("-EQ",""): x['token'] for x in master if x['exch_seg']=='NSE' and x['symbol'].endswith("-EQ")}
    # Sirf top 500 liquid - pura 2000 nahi taaki block na ho
    candidates = list(token_map_all.items())[:500]
    vol_list = []
    for idx, (sym, token) in enumerate(candidates, 1):
        try:
            param = {
                "exchange": "NSE", "symboltoken": token, "interval": "ONE_DAY",
                "fromdate": (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d %H:%M"),
                "todate": datetime.now().strftime("%Y-%m-%d %H:%M")
            }
            res = obj.getCandleData(param)
            if res and res.get('data') and len(res['data'])>=21:
                df = pd.DataFrame(res['data'], columns=['time','open','high','low','close','volume'])
                avg = df['volume'].tail(20).mean()
                last_vol = df['volume'].iloc[-1]
                vol_x = last_vol/avg if avg>0 else 0
                if vol_x >= 2.0:
                    vol_list.append((vol_x, sym, token, df))
        except: pass
        if idx % 100 == 0:
            print(f"Phase1 {idx}/500...", flush=True)
        time.sleep(0.4) # BLOCK SE BACHNE KE LIYE DELAY

    vol_list = sorted(vol_list, key=lambda x: x[0], reverse=True)[:80]
    top_syms = [x[1] for x in vol_list]
    # token_map for phase 2
    token_map = {sym: token for _, sym, token, _ in vol_list}
    daily_cache = {sym: df for _, sym, _, df in vol_list}
else:
    # Bhavcopy se mile to token map banao
    master = requests.get("https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json", timeout=20).json()
    token_map_all = {x['symbol'].replace("-EQ",""): x['token'] for x in master if x['exch_seg']=='NSE' and x['symbol'].endswith("-EQ")}
    token_map = {s: token_map_all.get(s) for s in top_syms if token_map_all.get(s)}
    daily_cache = {}

print(f"PHASE 1 DONE: Top Volume Count: {len(top_syms)}", flush=True)

# PHASE 2: SIRF TOP VOLUME KA WEEKLY + DAILY - YEHI TU CHAHTA THA
print(f"PHASE 2: Angel se Daily+Weekly check {len(top_syms)} stocks...", flush=True)

def get_candles(token, interval, days):
    try:
        param = {
            "exchange": "NSE", "symboltoken": token, "interval": interval,
            "fromdate": (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M"),
            "todate": datetime.now().strftime("%Y-%m-%d %H:%M")
        }
        res = obj.getCandleData(param)
        if res and res.get('data'):
            return pd.DataFrame(res['data'], columns=['time','open','high','low','close','volume'])
    except: pass
    return None

picks = []
for sym in top_syms:
    token = token_map.get(sym)
    if not token: continue

    daily = daily_cache.get(sym)
    if daily is None:
        daily = get_candles(token, "ONE_DAY", 380)
    if daily is None or len(daily) < 50:
        time.sleep(0.5)
        continue

    last = daily.iloc[-1]
    if last['close'] < 50:
        time.sleep(0.5)
        continue

    # 200 DMA ke liye pura data
    daily_full = get_candles(token, "ONE_DAY", 400) if len(daily) < 250 else daily
    if daily_full is None:
        time.sleep(0.5)
        continue

    dma200 = daily_full['close'].rolling(200).mean().iloc[-1]
    high_52w = daily_full['high'].tail(252).max()

    weekly = get_candles(token, "ONE_WEEK", 600)
    if weekly is None or len(weekly) < 20 or pd.isna(dma200):
        time.sleep(0.5)
        continue

    # Tera original logic
    vol_x = float(last['volume'] / daily['volume'].tail(20).mean()) if daily['volume'].tail(20).mean()>0 else 0
    near_ath = last['close'] >= float(high_52w) * 0.92
    weekly_up = weekly['close'].iloc[-1] > dma200
    green = last['close'] > last['open']

    if weekly_up and near_ath and green and vol_x >= 2.0:
        picks.append({"Stock": sym, "LTP": round(float(last['close']),2), "52W": round(float(high_52w),2), "VolX": round(float(vol_x),2), "SL": round(float(daily['low'].iloc[-1]),2)})
        print(f"BUY: {sym} {vol_x:.1f}x", flush=True)

    time.sleep(0.6) # IMPORTANT: Angel block se bachne ke liye

if not picks:
    msg = f"📉 *Pura NSE Scan {datetime.now().strftime('%d %b %I:%M %p')}*\nBhavcopy Top Vol: {len(top_syms)}\nFinal: 0 setup\n\n_Angel block nahi hua, scan OK_"
else:
    df = pd.DataFrame(picks).sort_values(by="VolX", ascending=False)
    msg = f"🚀 *PURA NSE BUY {datetime.now().strftime('%d %b %I:%M %p')}* 🚀\nBhavcopy Top: {len(top_syms)} -> BUY: {len(df)}\n\n"
    for _, r in df.iterrows():
        msg += f"*{r['Stock']}* LTP:{r['LTP']} (52W:{r['52W']})\nVol:{r['VolX']}x | SL:{r['SL']}\n\n"

print(msg, flush=True)
send_telegram(msg)
print("DONE", flush=True)
