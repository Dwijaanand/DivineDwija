import os,time,requests,pyotp,pandas as pd,json
from datetime import datetime,timedelta
from SmartApi import SmartConnect

API=os.getenv("API_KEY"); CID=os.getenv("CLIENT_ID")
PWD=os.getenv("PASSWORD"); TOTP=os.getenv("TOTP_SECRET")
TG=os.getenv("TELEGRAM_BOT_TOKEN"); CHAT=os.getenv("TELEGRAM_CHAT_ID")

MASTER="https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"

TOP=80
MIN_LTP=50
MIN_VOL=100000
MIN_AVG20=50000
MIN_VOLX=2.0
HISTORY=380
DELAY=.7
RETRIES=4
WAIT=8
EXCLUDED={"LTIM","TATAMOTORS"}

obj=None

def telegram(msg):
    if not TG or not CHAT:return
    try:
        requests.post(f"https://api.telegram.org/bot{TG}/sendMessage",
                      data={"chat_id":CHAT,"text":msg,"parse_mode":"Markdown"},
                      timeout=15)
    except Exception as e:
        print("Telegram error:",e,flush=True)

def rate_error(e):
    s=str(e).lower()
    return any(x in s for x in ["access rate","exceeding access rate","rate limit","too many requests","access denied"])

def market_closed():
    n=datetime.now()
    return n.hour>15 or (n.hour==15 and n.minute>=30)

def rsi(series, period=14):
    try:
        delta = series.diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
        rs = gain / loss
        return 100 - (100 / (1 + rs))
    except:
        return None

def hist(token):
    for a in range(1,RETRIES+1):
        try:
            p={
                "exchange":"NSE",
                "symboltoken":str(token),
                "interval":"ONE_DAY",
                "fromdate":(datetime.now()-timedelta(days=HISTORY)).strftime("%Y-%m-%d %H:%M"),
                "todate":datetime.now().strftime("%Y-%m-%d %H:%M")
            }
            r=obj.getCandleData(p)
            if r and r.get("data"):
                return pd.DataFrame(r["data"],columns=["time","open","high","low","close","volume"])
            return None
        except Exception as e:
            if rate_error(e):
                w=WAIT*a
                print(f" Rate limit. Waiting {w}s ({a}/{RETRIES})",flush=True)
                time.sleep(w)
            else:
                print(" Candle error:",e,flush=True)
                return None
    return None

def clean_daily(df):
    if df is None or df.empty:return None
    try:
        df=df.copy()
        df["time"]=pd.to_datetime(df["time"],errors="coerce")
        for c in ["open","high","low","close","volume"]:
            df[c]=pd.to_numeric(df[c],errors="coerce")
        df=df.dropna(subset=["time","open","high","low","close","volume"])
        df=df.sort_values("time").drop_duplicates("time",keep="last")
        if not market_closed() and len(df):
            if df["time"].iloc[-1].date()==datetime.now().date():
                df=df.iloc[:-1]
        return df.reset_index(drop=True)
    except Exception as e:
        print(" Clean error:",e,flush=True)
        return None

def weekly(df):
    try:
        w=df.copy().set_index("time").resample("W-FRI").agg({
            "open":"first","high":"max","low":"min","close":"last","volume":"sum"
        }).dropna()
        if not market_closed() and len(w):
            today=datetime.now()
            friday=today+timedelta(days=4-today.weekday())
            if w.index[-1].date()>=friday.date():
                w=w.iloc[:-1]
        return w
    except:
        return None

def monthly(df):
    try:
        m=df.copy().set_index("time").resample("M").agg({
            "open":"first","high":"max","low":"min","close":"last","volume":"sum"
        }).dropna()
        if len(m)>1:
            m=m.iloc[:-1]
        return m
    except:
        return None

def bulk_quotes(tokens):
    out={}
    for i in range(0,len(tokens),50):
        batch=tokens[i:i+50]
        ok=False
        for a in range(1,RETRIES+1):
            try:
                r=obj.getMarketData("FULL",{"NSE":batch})
                data=(r or {}).get("data",{})
                rows=[]
                if isinstance(data,dict):
                    rows=data.get("fetched",[]) or data.get("data",[])
                elif isinstance(data,list):
                    rows=data
                for q in rows:
                    if isinstance(q,dict):
                        t=str(q.get("symbolToken",""))
                        if t:out[t]=q
                ok=True
                break
            except Exception as e:
                print("Bulk error:",e,flush=True)
                time.sleep(WAIT*a if rate_error(e) else 3*a)
        if not ok:print("Bulk batch failed",flush=True)
        time.sleep(1.1)
        print(f"Bulk quote {min(i+50,len(tokens))}/{len(tokens)}",flush=True)
    return out

print("\n======================================")
print(" ANGEL ONE SWING SCANNER V3.5.1 FIXED")
print("======================================",flush=True)
print("Core strategy: LOCKED",flush=True)
print("Filters: Monthly+Weekly Bullish | RSI>55 | No UC/LC",flush=True)
print("Auto orders: DISABLED",flush=True)
print("Mode: FINAL DAILY CANDLE" if market_closed() else "Mode: PRE-CLOSE - TODAY CANDLE PROTECTED",flush=True)

if not API or not CID or not PWD or not TOTP:
    print("ERROR: Missing Angel credentials",flush=True)
    raise SystemExit

print("\nLogin...",flush=True)
try:
    obj=SmartConnect(api_key=API)
    sess=obj.generateSession(CID,PWD,pyotp.TOTP(TOTP).now())
    if not sess:raise Exception("Login failed")
    print("Angel OK",flush=True)
except Exception as e:
    print("LOGIN ERROR:",e,flush=True)
    raise SystemExit

# --- NSE MASTER WITH FALLBACK FIX ---
print("\nLoading NSE master...",flush=True)
master=None
for attempt in range(1,4):
    try:
        print(f" Attempt {attempt}/3...",flush=True)
        r=requests.get(MASTER,timeout=20,headers={"User-Agent":"Mozilla/5.0"})
        master=r.json()
        print(f" Master downloaded: {len(master)} records",flush=True)
        break
    except Exception as e:
        print(f" Master error {attempt}: {e}",flush=True)
        time.sleep(5)

if master is None:
    print(" Trying local file OpenAPIScripMaster.json...",flush=True)
    try:
        with open("OpenAPIScripMaster.json","r") as f:
            master=json.load(f)
        print(f" Local master loaded: {len(master)} records",flush=True)
    except Exception as e:
        print(f"Master failed completely: {e}",flush=True)
        raise SystemExit

tokens={}
for x in master:
    try:
        seg=str(x.get("exch_seg","")).lower()
        sym=str(x.get("symbol","")).upper()
        if seg in ("nse","nse_cm") and sym.endswith("-EQ"):
            s=sym[:-3]
            if s not in EXCLUDED and x.get("token"):
                tokens[s]=str(x["token"])
    except:
        pass

stocks=list(tokens.items())
print(f"NSE stocks: {len(stocks)}",flush=True)

print("\nPHASE 1: BULK VOLUME SCAN...",flush=True)
quotes=bulk_quotes([t for _,t in stocks])
candidates=[]

for sym,token in stocks:
    x=quotes.get(token,{})
    try:
        ltp=float(x.get("ltp",0) or 0)
        vol=float(x.get("tradeVolume",0) or 0)
        if ltp>=MIN_LTP and vol>=MIN_VOL:
            candidates.append({"sym":sym,"token":token,"ltp":ltp,"volume":vol})
    except:
        pass

candidates.sort(key=lambda x:x["volume"],reverse=True)
candidates=candidates[:TOP]

print(f"PHASE 1 DONE: {len(candidates)} candidates / {len(stocks)} NSE stocks",flush=True)
print("TOP:",[x["sym"] for x in candidates[:10]],flush=True)

print("\nPHASE 2: DAILY + WEEKLY + MONTHLY + MOMENTUM CHECK...",flush=True)
picks=[]

for n,x in enumerate(candidates,1):
    print(f"[{n}/{len(candidates)}] {x['sym']}",flush=True)

    df=clean_daily(hist(x["token"]))
    if df is None or len(df)<200:
        continue

    last=df.iloc[-1]
    avg20=df["volume"].iloc[-21:-1].mean()
    if not avg20 or avg20<MIN_AVG20:
        continue

    volx=float(last["volume"])/float(avg20)
    if volx<MIN_VOLX:
        continue

    dma200=df["close"].rolling(200).mean().iloc[-1]
    high52=df["high"].tail(252).max()
    if pd.isna(dma200) or pd.isna(high52):
        continue

    close=float(last["close"])
    openp=float(last["open"])
    low=float(last["low"])

    # CORE - LOCKED
    near_high=close>=float(high52)*.92
    green=close>openp

    # WEEKLY
    w=weekly(df)
    if w is None or len(w)<40:
        continue
    w["sma40"]=w["close"].rolling(40).mean()
    weekly_close=float(w["close"].iloc[-1])
    weekly_sma=float(w["sma40"].iloc[-1])
    if pd.isna(weekly_sma):
        continue
    weekly_up=weekly_close>weekly_sma

    # MONTHLY
    m=monthly(df)
    if m is None or len(m)<12:
        continue
    m["sma10"]=m["close"].rolling(10).mean()
    if pd.isna(m["sma10"].iloc[-1]):
        continue
    monthly_up=float(m["close"].iloc[-1])>float(m["sma10"].iloc[-1])

    # MOMENTUM
    r = rsi(df["close"],14)
    if r is None or pd.isna(r.iloc[-1]):
        continue
    rsi_val=float(r.iloc[-1])
    mom=rsi_val>55

    # UC/LC - Last 90 days me 15%+ wale din
    last90=df.tail(90)
    circuits=0
    for _,row in last90.iterrows():
        try:
            if float(row["open"])!=0:
                rng=abs(float(row["close"])-float(row["open"]))/float(row["open"])*100
                if rng>15: circuits+=1
        except:
            pass
    no_operator=circuits<=5

    if weekly_up and monthly_up and near_high and green and mom and no_operator:
        t1=close*1.05
        t2=close*1.08
        sl=max(low*0.99, close*0.965)
        picks.append({
            "Stock":x["sym"],
            "LTP":close,
            "52W":float(high52),
            "VolX":volx,
            "RSI":rsi_val,
            "Circuits":circuits,
            "SL":sl,
            "T1":t1,
            "T2":t2
        })
        print(f" BUY FOUND: {x['sym']} Vol {volx:.2f}x RSI {rsi_val:.0f}",flush=True)

    time.sleep(DELAY)

picks.sort(key=lambda x:x["VolX"],reverse=True)

now=datetime.now().strftime("%d %b %I:%M %p")
mode="Completed Daily Candle" if market_closed() else "Previous Completed Daily Candle"

if not picks:
    msg=(f"📉 *PURA NSE SCAN - {now}*\n\n"
         f"Total NSE: {len(stocks)}\n"
         f"Top Candidates: {len(candidates)}\n"
         f"Final BUY: 0\n\n"
         f"Mode: {mode}\n"
         f"Filters: Weekly+Monthly Bullish | RSI>55 | No UC/LC\n\n"
         f"_Aaj qualifying setup nahi mila._")
else:
    msg=(f"🚀 *PURA NSE BUY - {now}* 🚀\n\n"
         f"Total NSE: {len(stocks)}\n"
         f"Top Candidates: {len(candidates)}\n"
         f"BUY: {len(picks)}\n\n"
         f"Mode: {mode}\n"
         f"Filters: Weekly+Monthly Bullish | RSI>55 | No UC/LC\n\n")
    for r in picks:
        msg+=(f"*{r['Stock']}*\n"
              f"LTP: ₹{r['LTP']:.2f} | 52W: ₹{r['52W']:.2f}\n"
              f"Vol: {r['VolX']:.2f}x | RSI: {r['RSI']:.0f} | UC/LC(90d): {r['Circuits']}\n"
              f"SL: ₹{r['SL']:.2f} | TGT: ₹{r['T1']:.2f} / ₹{r['T2']:.2f}\n\n")

print("\n"+msg,flush=True)
telegram(msg)
print("\nDONE - V3.5.1 FILTERED SCAN COMPLETE",flush=True)
