import os,time,requests,pyotp,pandas as pd
from datetime import datetime,timedelta
from SmartApi import SmartConnect
from SmartApi.smartWebSocketV2 import SmartWebSocketV2

API=os.getenv("API_KEY"); CID=os.getenv("CLIENT_ID")
PWD=os.getenv("PASSWORD"); TOTP=os.getenv("TOTP_SECRET")
TG=os.getenv("TELEGRAM_BOT_TOKEN"); CHAT=os.getenv("TELEGRAM_CHAT_ID")

MASTER="https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"

def telegram(msg):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG}/sendMessage",
            data={"chat_id":CHAT,"text":msg,"parse_mode":"Markdown"},
            timeout=15
        )
    except: pass

def hist(token,days=380,interval="ONE_DAY"):
    try:
        p={
            "exchange":"NSE","symboltoken":token,"interval":interval,
            "fromdate":(datetime.now()-timedelta(days=days)).strftime("%Y-%m-%d %H:%M"),
            "todate":datetime.now().strftime("%Y-%m-%d %H:%M")
        }
        r=obj.getCandleData(p)
        if r and r.get("data"):
            return pd.DataFrame(
                r["data"],
                columns=["time","open","high","low","close","volume"]
            )
    except Exception as e:
        if "access rate" in str(e).lower():
            time.sleep(3)
    return None

print("\n======================================")
print(" ANGEL ONE SWING SCANNER V3.4.8 FAST")
print("======================================",flush=True)

# LOGIN
print("Login...",flush=True)
obj=SmartConnect(api_key=API)
sess=obj.generateSession(
    CID,PWD,pyotp.TOTP(TOTP).now()
)
print("Angel OK",flush=True)

# MASTER
print("Loading NSE master...",flush=True)
master=requests.get(MASTER,timeout=30).json()

tokens={}
for x in master:
    seg=str(x.get("exch_seg","")).lower()
    sym=str(x.get("symbol",""))
    if seg in ("nse","nse_cm") and sym.endswith("-EQ"):
        s=sym[:-3]
        if s not in ("LTIM","TATAMOTORS"):
            tokens[s]=str(x["token"])

stocks=list(tokens.items())
print(f"NSE stocks: {len(stocks)}",flush=True)

# ------------------------------------------------
# BULK MARKET DATA
# ------------------------------------------------
def bulk_quotes(token_list):
    out={}

    for i in range(0,len(token_list),50):
        batch=token_list[i:i+50]

        for attempt in range(3):
            try:
                r=obj.getMarketData(
                    "FULL",
                    {"NSE":batch}
                )

                data=(r or {}).get("data",{})
                rows=[]

                if isinstance(data,dict):
                    rows=data.get("fetched",[]) or data.get("data",[])
                elif isinstance(data,list):
                    rows=data

                for q in rows:
                    t=str(q.get("symbolToken",""))
                    out[t]=q

                break

            except Exception as e:
                print("Bulk retry:",e,flush=True)
                time.sleep(3*(attempt+1))

        # Angel bulk quote endpoint safety
        time.sleep(1.1)

        print(
            f"Bulk quote {min(i+50,len(token_list))}/{len(token_list)}",
            flush=True
        )

    return out

print("\nPHASE 1: BULK VOLUME SCAN...",flush=True)

q=bulk_quotes([t for _,t in stocks])

# Current volume >= 100000 is mathematically necessary
# because final rule requires Avg20 >= 50000 AND Volume >= 2x.
candidates=[]

for sym,token in stocks:
    x=q.get(token,{})
    try:
        ltp=float(x.get("ltp",0))
        vol=float(
            x.get("tradeVolume",x.get("tradeVolume",0)) or 0
        )

        if ltp>=50 and vol>=100000:
            candidates.append({
                "sym":sym,
                "token":token,
                "ltp":ltp,
                "volume":vol
            })
    except:
        pass

candidates.sort(key=lambda x:x["volume"],reverse=True)
candidates=candidates[:80]

print(
    f"PHASE 1 DONE: {len(candidates)} candidates / "
    f"{len(stocks)} NSE stocks",
    flush=True
)

print(
    "TOP:",
    [x["sym"] for x in candidates[:10]],
    flush=True
)

# ------------------------------------------------
# PHASE 2
# ------------------------------------------------
print("\nPHASE 2: DAILY + WEEKLY CHECK...",flush=True)

picks=[]

for n,x in enumerate(candidates,1):
    print(f"[{n}/{len(candidates)}] {x['sym']}",flush=True)

    df=hist(x["token"],380)

    if df is None or len(df)<200:
        continue

    df["close"]=pd.to_numeric(df["close"])
    df["open"]=pd.to_numeric(df["open"])
    df["high"]=pd.to_numeric(df["high"])
    df["low"]=pd.to_numeric(df["low"])
    df["volume"]=pd.to_numeric(df["volume"])

    last=df.iloc[-1]

    # SAME VOLUME RULE
    avg20=df["volume"].iloc[-21:-1].mean()
    volx=float(last["volume"])/avg20 if avg20 else 0

    if avg20<50000 or volx<2:
        continue

    # SAME DAILY CONDITIONS
    dma200=df["close"].rolling(200).mean().iloc[-1]
    high52=df["high"].tail(252).max()

    near_high=float(last["close"])>=float(high52)*0.92
    green=float(last["close"])>float(last["open"])

    # WEEKLY FROM DAILY DATA
    w=df.copy()
    w["time"]=pd.to_datetime(w["time"])
    w=w.set_index("time").resample("W-FRI").agg({
        "open":"first",
        "high":"max",
        "low":"min",
        "close":"last",
        "volume":"sum"
    }).dropna()

    if len(w)<2:
        continue

    weekly_close=float(w["close"].iloc[-1])
    weekly_up=weekly_close>float(dma200)

    if weekly_up and near_high and green:
        entry=float(last["close"])
        sl=float(last["low"])

        picks.append({
            "Stock":x["sym"],
            "LTP":entry,
            "52W":float(high52),
            "VolX":volx,
            "SL":sl
        })

        print(
            f"BUY FOUND: {x['sym']} "
            f"Vol {volx:.1f}x",
            flush=True
        )

    time.sleep(0.7)

# ------------------------------------------------
# TELEGRAM
# ------------------------------------------------
now=datetime.now().strftime("%d %b %I:%M %p")

if not picks:
    msg=(
        f"📉 *PURA NSE SCAN - {now}*\n\n"
        f"Total NSE: {len(stocks)}\n"
        f"Top Candidates: {len(candidates)}\n"
        f"Final BUY: 0\n\n"
        f"_Aaj qualifying setup nahi mila._"
    )
else:
    picks.sort(key=lambda x:x["VolX"],reverse=True)

    msg=(
        f"🚀 *PURA NSE BUY - {now}* 🚀\n\n"
        f"Total NSE: {len(stocks)}\n"
        f"Top Candidates: {len(candidates)}\n"
        f"BUY: {len(picks)}\n\n"
    )

    for r in picks:
        e=r["LTP"]
        msg+=(
            f"*{r['Stock']}*\n"
            f"LTP: ₹{e:.2f} | 52W: ₹{r['52W']:.2f}\n"
            f"Vol: {r['VolX']:.2f}x | "
            f"SL: ₹{r['SL']:.2f}\n"
            f"TGT: ₹{e*1.05:.2f} / ₹{e*1.08:.2f}\n\n"
        )

print("\n"+msg,flush=True)
telegram(msg)

print("\nDONE - FAST NSE SCAN COMPLETE",flush=True)
