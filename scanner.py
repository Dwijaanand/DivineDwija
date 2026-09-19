import os, time, requests, pyotp, pandas as pd
from datetime import datetime, timedelta
from SmartApi import SmartConnect

# ============================================================
# CHANDAN ATH BREAKOUT V2.1 - SWING SCANNER (FAST + STABLE)
# ============================================================

API_KEY = os.getenv("API_KEY")
CLIENT_ID = os.getenv("CLIENT_ID")
PASSWORD = os.getenv("PASSWORD")
TOTP_SECRET = os.getenv("TOTP_SECRET")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

MASTER_URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"

# ========== CORE FILTERS ==========
MIN_PRICE = 100
MIN_AVG_VOL = 300000
VOL_X = 3.0
NEAR_ATH_PERC = 5

# ========== PERFORMANCE ==========
TOP_LIQUID = 60
QUOTE_BATCH = 50
QUOTE_DELAY = 1.05
CANDLE_DELAY = 0.40

# ========== QUALITY ==========
ATR_PERIOD = 14
SWING_LOOKBACK = 10

# ========== BLOCKLIST (IPO / operator) ==========
BLOCKLIST = {"BAJAJHFL","MOTHERSON","TATATECH","JSWINFRA","IREDA"}

def send_telegram(msg):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        # Markdown me * _ ko safe karna
        r = requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"}, timeout=15)
        print("Telegram:", r.status_code, r.text[:200])
    except Exception as e:
        print("Telegram Fail:", e)

def ema(s,p): return s.ewm(span=p, adjust=False).mean()

def rsi(s,p=14):
    d=s.diff(); g=d.clip(lower=0); l=-d.clip(upper=0)
    ag=g.ewm(alpha=1/p, adjust=False).mean()
    al=l.ewm(alpha=1/p, adjust=False).mean()
    rs=ag/al.replace(0, pd.NA)
    return 100-(100/(1+rs))

def atr(df,p=14):
    pc=df["close"].shift(1)
    tr=pd.concat([df["high"]-df["low"], (df["high"]-pc).abs(), (df["low"]-pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1/p, adjust=False).mean()

def get_candles(obj, token, interval):
    try:
        param = {
            "exchange": "NSE",
            "symboltoken": str(token),
            "interval": interval,
            "fromdate": (datetime.now()-timedelta(days=500)).strftime("%Y-%m-%d %H:%M"),
            "todate": datetime.now().strftime("%Y-%m-%d %H:%M")
        }
        res = obj.getCandleData(param)
        if res and res.get("data"):
            df = pd.DataFrame(res["data"], columns=["time","open","high","low","close","volume"])
            for c in ["open","high","low","close","volume"]:
                df[c]=pd.to_numeric(df[c], errors="coerce")
            return df.dropna().reset_index(drop=True)
    except Exception as e:
        print(f"Candle error {token}: {e}")
    return None

def get_quotes(obj, token_list):
    try:
        res = obj.getMarketData("FULL", {"NSE": token_list})
        data = (res or {}).get("data", {})
        if isinstance(data, dict):
            fetched = data.get("fetched", []) or data.get("data", [])
            return fetched
        return data if isinstance(data, list) else []
    except Exception as e:
        print("Quote error:", e)
        return []

# ========== LOGIN ==========
print("\n======================================")
print("CHANDAN ATH BREAKOUT V2.1 FAST")
print("======================================\n")

required = {"API_KEY":API_KEY,"CLIENT_ID":CLIENT_ID,"PASSWORD":PASSWORD,"TOTP_SECRET":TOTP_SECRET,"TELEGRAM_BOT_TOKEN":TELEGRAM_BOT_TOKEN,"TELEGRAM_CHAT_ID":TELEGRAM_CHAT_ID}
missing=[k for k,v in required.items() if not v]
if missing: raise RuntimeError("Missing: "+", ".join(missing))

obj = SmartConnect(api_key=API_KEY)
totp = pyotp.TOTP(TOTP_SECRET).now()
session = obj.generateSession(CLIENT_ID, PASSWORD, totp)
if not session or session.get("status") is False:
    raise RuntimeError(f"Login failed: {session}")
print("Login OK")

# ========== MASTER ==========
print("NSE Master load...")
master = requests.get(MASTER_URL, timeout=30).json()
ALL_TOKENS=[]; seen=set()
for x in master:
    if x.get("exch_seg")=="NSE" and x.get("symbol","").endswith("-EQ"):
        sym=x["symbol"].replace("-EQ","")
        if sym in BLOCKLIST or sym in seen: continue
        seen.add(sym)
        ALL_TOKENS.append({"sym":sym, "token":str(x["token"])})

print(f"Total NSE-EQ: {len(ALL_TOKENS)}")

# ========== FAST LIQUIDITY FILTER ==========
print(f"\nFast filter: {len(ALL_TOKENS)} -> {TOP_LIQUID}")
quote_rows=[]
for start in range(0, len(ALL_TOKENS), QUOTE_BATCH):
    batch = ALL_TOKENS[start:start+QUOTE_BATCH]
    token_list=[x["token"] for x in batch]
    fetched=get_quotes(obj, token_list)
    token_map={x["token"]:x["sym"] for x in batch}
    for q in fetched:
        token=str(q.get("symbolToken",""))
        sym=token_map.get(token, q.get("tradingSymbol","").replace("-EQ",""))
        try:
            ltp=float(q.get("ltp", q.get("close",0)))
            vol=float(q.get("tradeVolume", q.get("volume",0)))
        except: continue
        if ltp>=MIN_PRICE and vol>0:
            quote_rows.append({"sym":sym,"token":token,"ltp":ltp,"volume":vol})
    print(f"Quotes {min(start+QUOTE_BATCH,len(ALL_TOKENS))}/{len(ALL_TOKENS)}")
    time.sleep(QUOTE_DELAY)

if not quote_rows: raise RuntimeError("No quotes")

quote_df=pd.DataFrame(quote_rows)
quote_df=quote_df.sort_values("volume", ascending=False).drop_duplicates("sym")
liquid=quote_df.head(TOP_LIQUID).copy()
print(f"Liquid selected: {len(liquid)}")

# ========== SWING SCAN ==========
picks=[]
for _, row in liquid.iterrows():
    sym=row["sym"]; token=row["token"]
    print(f"Checking {sym}...")

    daily=get_candles(obj, token, "ONE_DAY"); time.sleep(CANDLE_DELAY)
    if daily is None or len(daily)<220: continue
    d=daily.iloc[-2]; hist=daily.iloc[:-1].copy()
    avg_vol=hist["volume"].tail(20).mean()
    if d["close"]<MIN_PRICE or avg_vol<MIN_AVG_VOL: continue

    weekly=get_candles(obj, token, "ONE_WEEK"); time.sleep(CANDLE_DELAY)
    if weekly is None or len(weekly)<110: continue
    w=weekly.iloc[-2]; whist=weekly.iloc[:-1].copy()

    dma200=hist["close"].rolling(200).mean().iloc[-1]
    if pd.isna(dma200): continue
    high_52w=hist["high"].tail(252).max()
    vol_x=float(d["volume"]/avg_vol) if avg_vol>0 else 0

    weekly_uptrend=w["close"]>dma200
    near_ath=d["close"]>=high_52w*(1-NEAR_ATH_PERC/100)
    green=d["close"]>d["open"]
    vol_blast=d["volume"]>(avg_vol*VOL_X)
    breakout=d["close"]>=high_52w*0.995

    if not (weekly_uptrend and near_ath and green and vol_blast and breakout): continue

    ema20=ema(hist["close"],20).iloc[-1]; ema50=ema(hist["close"],50).iloc[-1]; ema200=dma200
    wema10=ema(whist["close"],10).iloc[-1]; wema30=ema(whist["close"],30).iloc[-1]; wema100=ema(whist["close"],100).iloc[-1]
    daily_mtf=d["close"]>ema20>ema50>ema200
    weekly_mtf=w["close"]>wema10>wema30>wema100

    rsi14=rsi(hist["close"],14).iloc[-1]
    if pd.isna(rsi14): continue

    atr14=atr(hist,ATR_PERIOD).iloc[-1]
    if pd.isna(atr14) or atr14<=0: continue
    atr_pct=(atr14/d["close"])*100

    score=50
    if d["close"]>ema20: score+=8
    if ema20>ema50: score+=7
    if ema50>ema200: score+=5
    if w["close"]>wema10: score+=6
    if wema10>wema30: score+=5
    if wema30>wema100: score+=4
    if 55<=rsi14<=80: score+=5
    if 60<=rsi14<=75: score+=3
    if vol_x>=5: score+=4
    elif vol_x>=4: score+=3
    elif vol_x>=3: score+=2
    if d["close"]>=high_52w*0.995: score+=3
    if atr_pct>=1.0: score+=2
    score=min(int(score),100)

    if score>=90: stars="★★★★★"
    elif score>=82: stars="★★★★☆"
    elif score>=74: stars="★★★☆☆"
    elif score>=66: stars="★★☆☆☆"
    else: stars="★☆☆☆☆"

    swing_low=hist["low"].tail(SWING_LOOKBACK).min()
    sl=swing_low-(0.5*atr14)
    if sl>=d["close"]: sl=d["close"]-atr14
    risk=d["close"]-sl
    if risk<=0: continue
    t1=d["close"]+(risk*2); t2=d["close"]+(risk*3)

    picks.append({
        "Stock":sym,"LTP":round(float(d["close"]),2),"52W_High":round(float(high_52w),2),
        "Vol_X":round(vol_x,2),"RSI":round(float(rsi14),1),"ATR_Pct":round(float(atr_pct),2),
        "Score":score,"Stars":stars,"SL":round(float(sl),2),"TGT1":round(float(t1),2),"TGT2":round(float(t2),2),
        "Daily_MTF":"YES" if daily_mtf else "NO","Weekly_MTF":"YES" if weekly_mtf else "NO","Signal":"BUY - BREAKOUT"
    })
    print(f"FOUND {sym} | {stars} Score {score} Vol {vol_x:.2f}x RSI {rsi14:.1f}")

# ========== RESULT ==========
if not picks:
    msg=f"📉 *CHANDAN ATH BREAKOUT*\n🕒 {datetime.now().strftime('%d %b %Y %H:%M')}\n\nNSE: {len(ALL_TOKENS)} | Liquid: {len(liquid)} | Found: 0\n\nNo tight setup today.\n_Core: ATH Break + 3x Vol + Weekly Uptrend_"
    print(msg); send_telegram(msg)
else:
    df=pd.DataFrame(picks).sort_values(by=["Score","Vol_X"], ascending=[False,False]).reset_index(drop=True)
    df["Rank"]=df.index+1
    msg=f"🚀 *CHANDAN ATH BREAKOUT V2.1* 🚀\n🕒 {datetime.now().strftime('%d %b %Y %H:%M')}\n\nNSE: {len(ALL_TOKENS)} | Liquid: {len(liquid)} | Found: {len(df)}\n🔥 *Strongest First*\n\n"
    for _,r in df.iterrows():
        mtf=[]
        if r["Daily_MTF"]=="YES": mtf.append("Daily✓")
        if r["Weekly_MTF"]=="YES": mtf.append("Weekly✓")
        msg+=f"*#{int(r['Rank'])} {r['Stock']} {r['Stars']}*\nLTP: ₹{r['LTP']} | 52W: ₹{r['52W_High']}\nScore: {r['Score']}/100 | Vol: {r['Vol_X']}x\nRSI: {r['RSI']} | ATR: {r['ATR_Pct']}%\nMTF: {' '.join(mtf) or 'Core'}\nSL: ₹{r['SL']}\nTGT: ₹{r['TGT1']} / ₹{r['TGT2']}\n\n"
    msg+="_No auto orders_"
    print("\n"+msg); send_telegram(msg)
    df.to_csv("Chandan_ALL_1800.csv", index=False)
    print("CSV saved Chandan_ALL_1800.csv")

print("\n[Finished]")
