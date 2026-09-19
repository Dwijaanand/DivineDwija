import os,json,time,random,requests
from datetime import datetime,timedelta
import pandas as pd
import pyotp
from SmartApi import SmartConnect

# ========== ANGEL ONE NSE V4.0 TURBO - INTRADAY QUALITY (2-3 Signals) ==========
# Merged: Your V3.5.9 + arshadakl intraday bot logic

API_KEY=os.getenv("API_KEY") or os.getenv("SMARTAPI_API_KEY")
CLIENT_ID=os.getenv("CLIENT_ID") or os.getenv("SMARTAPI_CLIENT_ID")
PASSWORD=os.getenv("PASSWORD") or os.getenv("SMARTAPI_PASSWORD")
TOTP_SECRET=os.getenv("TOTP_SECRET") or os.getenv("SMARTAPI_TOTP_SECRET")
TELE_BOT=os.getenv("TELEGRAM_BOT_TOKEN")
TELE_CHAT=os.getenv("TELEGRAM_CHAT_ID")

DELAY=0.35
TOP_N=80 # 50 se 80 kiya - jyada pool
REST_EVERY=25
VOL_THRESHOLD=1.5 # 1.8 se 1.5 kiya

# --- INTRADAY QUALITY PARAMS (repo se) ---
OHL_BUFFER_PCT = 0.06
VWAP_CONSOLIDATION_PCT = 0.2
NIFTY_THRESHOLD = 0.1

def log(m): print(m,flush=True)

def send_telegram(msg):
    if not TELE_BOT or not TELE_CHAT: return
    try:
        url=f"https://api.telegram.org/bot{TELE_BOT}/sendMessage"
        requests.post(url,json={"chat_id":TELE_CHAT,"text":msg,"parse_mode":"Markdown"},timeout=10)
    except Exception as e: log(f"Telegram fail: {e}")

def get_obj():
    clean_secret=TOTP_SECRET.strip().replace(" ","")
    obj=SmartConnect(api_key=API_KEY.strip())
    for attempt in range(1,3):
        try:
            totp=pyotp.TOTP(clean_secret).now()
            data=obj.generateSession(CLIENT_ID.strip(),PASSWORD.strip(),totp)
            if data and data.get("status"):
                log("Angel Login OK - V4.0 QUALITY")
                return obj
        except Exception as e:
            log(f"Login fail {e}")
            time.sleep(3)
    raise Exception("Login failed")

def load_master():
    local="OpenAPIScripMaster.json"
    if os.path.exists(local):
        try:
            with open(local,"r") as f:
                data=json.load(f)
            if len(data)>1000: return data
        except: pass
    url="https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
    r=requests.get(url,timeout=60)
    data=r.json()
    with open(local,"w") as f: json.dump(data,f)
    return data

def get_candle(obj,token,interval="ONE_DAY",days=500):
    to_date=datetime.now()
    from_date=to_date-timedelta(days=days)
    params={"exchange":"NSE","symboltoken":str(token),"interval":interval,"fromdate":from_date.strftime("%Y-%m-%d %H:%M"),"todate":to_date.strftime("%Y-%m-%d %H:%M")}
    for attempt in range(5):
        try:
            res=obj.getCandleData(params)
            if res and res.get("status") and res.get("data"):
                df=pd.DataFrame(res["data"],columns=["time","open","high","low","close","volume"])
                df["time"]=pd.to_datetime(df["time"])
                for c in ["open","high","low","close","volume"]: df[c]=pd.to_numeric(df[c],errors="coerce")
                return df.dropna().sort_values("time").reset_index(drop=True)
            if "rate" in str(res).lower(): time.sleep(8*(attempt+1))
            else: return None
        except Exception:
            time.sleep(2)
            return None
    return None

def get_intraday_1min(obj, token):
    # Aaj ka 1-min data
    return get_candle(obj, token, "ONE_MINUTE", 2)

def calc_rsi(series,period=14):
    delta=series.diff()
    gain=delta.where(delta>0,0).rolling(period).mean()
    loss=(-delta.where(delta<0,0)).rolling(period).mean()
    rs=gain/loss
    return 100-(100/(1+rs))

def calculate_quality_indicators(df):
    if len(df)<20: return df
    typical = (df['high'] + df['low'] + df['close']) / 3
    df['vwap'] = (typical * df['volume']).cumsum() / df['volume'].cumsum()
    df['rsi'] = calc_rsi(df['close'])
    df['avg_vol'] = df['volume'].rolling(20).mean()
    df['tr'] = pd.concat([df['high']-df['low'], abs(df['high']-df['close'].shift()), abs(df['low']-df['close'].shift())], axis=1).max(axis=1)
    df['atr_pct'] = df['tr'].rolling(14).mean() / df['close'] * 100
    return df

def main():
    log("="*70)
    log(" ANGEL ONE NSE V4.0 TURBO - QUALITY 2-3 SIGNALS")
    log("="*70)
    obj=get_obj()
    master=load_master()
    nse_stocks=[s for s in master if s.get("exch_seg")=="NSE" and str(s.get("symbol","")).endswith("-EQ")]
    log(f"NSE Total: {len(nse_stocks)}")

    # STEP 0: Nifty Trend (repo ka main filter)
    log("\nSTEP 0: Nifty Trend Check...")
    nifty_token = "99926000" # Nifty 50 token
    # Nifty ke liye master se token dhoondo
    nifty_stock = next((s for s in master if s.get("symbol")=="Nifty 50" or s.get("name")=="NIFTY"), None)
    nifty_trend = "UP"
    try:
        nifty_df = get_intraday_1min(obj, nifty_stock['token'] if nifty_stock else nifty_token)
        if nifty_df is not None and len(nifty_df)>5:
            nifty_open = nifty_df.iloc[0]['open']
            nifty_now = nifty_df.iloc[-1]['close']
            change = (nifty_now - nifty_open)/nifty_open*100
            if change > NIFTY_THRESHOLD: nifty_trend = "UP"
            elif change < -NIFTY_THRESHOLD: nifty_trend = "DOWN"
            else: nifty_trend = "FLAT"
            log(f"Nifty Trend: {nifty_trend} ({change:.2f}%)")
    except Exception as e:
        log(f"Nifty fetch fail, default UP: {e}")

    log("\nPHASE 1: VOLUME SCAN...")
    candidates=[]
    for idx,s in enumerate(nse_stocks):
        if idx%400==0: log(f"Scanning {idx}/{len(nse_stocks)} | Found: {len(candidates)}")
        df=get_candle(obj,s["token"],"ONE_DAY",40)
        time.sleep(DELAY)
        if df is None or len(df)<25: continue
        try:
            avg_vol=df["volume"].iloc[-20:-1].mean()
            curr_vol=df["volume"].iloc[-1]
            if avg_vol>0 and curr_vol/avg_vol >= VOL_THRESHOLD:
                candidates.append((s,curr_vol/avg_vol))
        except: continue
    candidates=sorted(candidates,key=lambda x:x[1],reverse=True)[:TOP_N]
    log(f"PHASE 1 DONE: {len(candidates)} candidates")

    log("\nPHASE 2: INTRADAY QUALITY FILTER (OHL + VWAP + RSI)...\n")
    quality_picks=[]

    for count,(stock,volx) in enumerate(candidates,1):
        sym=stock.get("name") or stock.get("symbol","UNKNOWN")
        log(f"[{count}/{len(candidates)}] {sym} {volx:.1f}x")
        if count%REST_EVERY==0: time.sleep(5)

        # 1. Daily data for swing confirm + 52W
        df_daily=get_candle(obj,stock["token"],"ONE_DAY",300)
        time.sleep(DELAY)
        if df_daily is None or len(df_daily)<50: continue

        # 2. Intraday 1-min data - Isi se quality ayegi
        df_1min=get_intraday_1min(obj, stock["token"])
        time.sleep(DELAY)
        if df_1min is None or len(df_1min)<20:
            log(" No intraday data - skip")
            continue

        try:
            df_1min = calculate_quality_indicators(df_1min)
            last = df_1min.iloc[-1]
            prev = df_1min.iloc[-2]

            # REPO FILTER 1: OHL with 0.06% buffer
            ohl_buffer_val = last['open'] * OHL_BUFFER_PCT / 100
            is_open_low = abs(last['open'] - last['low']) <= ohl_buffer_val
            is_open_high = abs(last['open'] - last['high']) <= ohl_buffer_val
            if not (is_open_low or is_open_high):
                log(" No OHL pattern")
                continue

            # REPO FILTER 2: ATR% 1.5-4%
            if not (1.5 <= last['atr_pct'] <= 4.0):
                log(f" ATR fail {last['atr_pct']:.2f}%")
                continue

            # REPO FILTER 3: Consolidation Check (VWAP hugging)
            distance_vwap = abs(last['close'] - last['vwap']) / last['vwap'] * 100
            if distance_vwap < VWAP_CONSOLIDATION_PCT:
                log(" Consolidation zone - skip")
                continue

            # REPO FILTER 4: VWAP Crossover
            vwap_cross_up = prev['close'] < prev['vwap'] and last['close'] > last['vwap']
            vwap_cross_down = prev['close'] > prev['vwap'] and last['close'] < last['vwap']

            # REPO FILTER 5: RSI 40-70
            rsi_ok = 40 < last['rsi'] < 70

            # REPO FILTER 6: Nifty Confirmation (MOST IMPORTANT FOR QUALITY)
            signal_type = None
            if is_open_low and nifty_trend=="UP" and (vwap_cross_up or last['close']>last['vwap']) and rsi_ok:
                signal_type = "LONG"
            elif is_open_high and nifty_trend=="DOWN" and (vwap_cross_down or last['close']<last['vwap']):
                signal_type = "SHORT"

            if not signal_type:
                log(" Nifty/RSI/VWAP mismatch")
                continue

            # REPO FILTER 7: Volume Surge 1.5x
            if last['volume'] < last['avg_vol'] * 1.5:
                log(" No volume surge")
                continue

            # FINAL QUALITY SCORE
            score = 0
            if is_open_low or is_open_high: score+=30
            if nifty_trend in ["UP","DOWN"]: score+=25
            if vwap_cross_up or vwap_cross_down: score+=20
            if rsi_ok: score+=10
            score += min(volx*3, 15)

            # Swing confirm from your old logic
            close_daily = float(df_daily["close"].iloc[-1])
            high_52 = float(df_daily["high"].tail(250).max())
            near_high = (close_daily/high_52*100) if high_52>0 else 0

            quality_picks.append({
                "sym": sym,
                "type": signal_type,
                "close": last['close'],
                "vwap": last['vwap'],
                "rsi": last['rsi'],
                "score": score,
                "volx": volx,
                "gap_pattern": "O=L" if is_open_low else "O=H",
                "nifty": nifty_trend,
                "high_52": high_52,
                "near_high": near_high
            })
            log(f" ✅ QUALITY {signal_type} | Score {score} | {sym}")

        except Exception as e:
            log(f" Error {sym}: {e}")
            continue

    # TOP 3 ONLY
    quality_picks = sorted(quality_picks, key=lambda x: x['score'], reverse=True)[:3]
    log(f"\nDONE - Final Quality Picks: {len(quality_picks)}")

    if quality_picks:
        ranked_text=[]
        for i,p in enumerate(quality_picks,1):
            stars = "⭐⭐⭐⭐⭐" if p['score']>=80 else "⭐⭐⭐⭐" if p['score']>=65 else "⭐⭐⭐"
            text=(f"*#{i} {p['sym']} {p['type']} {stars}*\n"
                  f"Score: {p['score']}/100 | Vol: {p['volx']:.1f}x\n"
                  f"LTP: ₹{p['close']:.1f} | VWAP: ₹{p['vwap']:.1f} | RSI: {p['rsi']:.0f}\n"
                  f"Pattern: {p['gap_pattern']} | Nifty: {p['nifty']} | 52W Near: {p['near_high']:.0f}%\n")
            ranked_text.append(text)

        msg=(f"🎯 *NSE TURBO V4.0 - QUALITY 2-3 SIGNALS* 🎯\n"
             f"Date: {datetime.now().strftime('%d %b %H:%M')}\n"
             f"Nifty Trend: {nifty_trend}\n"
             f"Scanned: {len(nse_stocks)} -> {len(candidates)} -> {len(quality_picks)}\n\n"
             f"🔥 *TOP QUALITY SETUP (OHL+VWAP+RSI)*\n\n" + "\n".join(ranked_text) +
             f"\n_Logic: OHL 0.06% + Nifty Filter + VWAP Cross + Vol 1.5x + RSI_")
        send_telegram(msg)
    else:
        send_telegram(f"⚠️ *V4.0 Quality Scan - No Picks*\nNSE: {len(nse_stocks)}\nCandidates: {len(candidates)}\nNifty: {nifty_trend}\nNo stock passed OHL+VWAP+Nifty filter - Market in consolidation.")

if __name__=="__main__":
    main()
