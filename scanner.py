import os,time,requests,pyotp,pandas as pd,numpy as np
from datetime import datetime,timedelta
from SmartApi import SmartConnect
import pytz

API_KEY=os.getenv("API_KEY");CLIENT_ID=os.getenv("CLIENT_ID");PASSWORD=os.getenv("PASSWORD");TOTP_SECRET=os.getenv("TOTP_SECRET")
TELEGRAM_BOT_TOKEN=os.getenv("TELEGRAM_BOT_TOKEN");TELEGRAM_CHAT_ID=os.getenv("TELEGRAM_CHAT_ID")
MASTER_URL="https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
IST=pytz.timezone("Asia/Kolkata")

HISTORY=900;TOP_LIQUID=100;TOP_SECTOR=3;TOP_FUNDAMENTAL=10;MAX_SIGNALS=5
MIN_PRICE=50;MIN_AVG20=50000;VOL_THRESHOLD=1.8;FUNDAMENTAL_MIN=60
QUOTE_BATCH=50;QUOTE_DELAY=1.0;CANDLE_DELAY=.35

SECTOR_MAP={
"IT":"TCS INFY HCLTECH WIPRO TECHM LTIM MPHASIS COFORGE PERSISTENT LTTS OFSS TATAELXSI KPITTECH CYIENT BSOFT",
"PHARMA":"SUNPHARMA DRREDDY CIPLA DIVISLAB AUROPHARMA TORNTPHARM LUPIN ALKEM ZYDUSLIFE GLAND LAURUSLABS MANKIND BIOCON ABBOTINDIA IPCALAB",
"AUTO":"MARUTI M&M TATAMOTORS EICHERMOT HEROMOTOCO BAJAJ-AUTO TVSMOTOR ASHOKLEY BHARATFORG MOTHERSON BOSCHLTD EXIDEIND SONACOMS UNOMINDA",
"BANK":"HDFCBANK ICICIBANK SBIN AXISBANK KOTAKBANK INDUSINDBK BANKBARODA PNB CANBK IDFCFIRSTB FEDERALBNK AUBANK BANDHANBNK",
"FINANCE":"BAJFINANCE BAJAJFINSV SHRIRAMFIN CHOLAFIN MUTHOOTFIN MANAPPURAM LICHSGFIN PFC RECLTD IRFC HUDCO JIOFIN SBICARD",
"FMCG":"HINDUNILVR ITC NESTLEIND BRITANNIA DABUR MARICO GODREJCP COLPAL TATACONSUM VBL UNITDSPR EMAMILTD RADICO JUBLFOOD",
"METAL":"TATASTEEL JSWSTEEL HINDALCO JINDALSTEL SAIL NMDC VEDL NATIONALUM APLAPOLLO HINDZINC JSL RATNAMANI",
"ENERGY":"RELIANCE ONGC NTPC POWERGRID COALINDIA BPCL IOC GAIL ADANIGREEN ADANIPOWER TORNTPOWER TATAPOWER JSWENERGY NHPC ACMESOLAR",
"INFRA":"LT ADANIENT ADANIPORTS DLF RVNL IRCON NBCC NCC BHEL BEL CONCOR KEC KALPATPOWR ASHOKA",
"REALTY":"DLF LODHA GODREJPROP OBEROIRLTY PRESTIGE PHOENIXLTD BRIGADE SOBHA RAYMOND",
"CHEMICAL":"SRF PIDILITIND DEEPAKNTR NAVINFLUOR PIIND ATUL ALKYLAMINE AARTIIND FINEORG CLEAN FLUOROCHEM SOLARINDS",
"DEFENCE":"HAL BEL BDL MAZDOCK COCHINSHIP GRSE SOLARINDS DATAPATTNS ASTRAMICRO",
"CAPITAL_GOODS":"SIEMENS ABB CUMMINSIND THERMAX CGPOWER SCHNEIDER KEI POLYCAB HAVELLS VOLTAS",
"TELECOM":"BHARTIARTL INDUSTOWER IDEA HFCL TANLA",
"CONSUMER":"TRENT TITAN KALYAN JUBLFOOD DMART NYKAA ABFRL INDIAMART",
"LOGISTICS":"CONCOR DELHIVERY BLUEDART TCI VRLLOG IRCTC"}
SECTOR_MAP={k:v.split() for k,v in SECTOR_MAP.items()}
SYMBOL_SECTOR={}
for k,v in SECTOR_MAP.items():
    for s in v: SYMBOL_SECTOR.setdefault(s,k)

def tg(x):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:return
    try: requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",data={"chat_id":TELEGRAM_CHAT_ID,"text":x},timeout=15)
    except Exception as e: print("Telegram:",e)

def login():
    if not all([API_KEY,CLIENT_ID,PASSWORD,TOTP_SECRET]): raise RuntimeError("Credentials missing")
    a=SmartConnect(api_key=API_KEY)
    r=a.generateSession(CLIENT_ID,PASSWORD,pyotp.TOTP(TOTP_SECRET).now())
    if not r or not r.get("status"): raise RuntimeError(f"Login failed: {r}")
    return a

def master():
    r=requests.get(MASTER_URL,timeout=30);r.raise_for_status(); rows=r.json();out={}
    for x in rows:
        seg=str(x.get("exch_seg","")).upper()
        if seg not in ("NSE_CM","NSE"): continue
        sym=str(x.get("symbol","")).upper();tok=str(x.get("token",""))
        if not sym or not tok: continue
        base=sym.replace("-EQ","")
        out.setdefault(base,{"token":tok,"symbol":sym})
    if len(out)<100: raise RuntimeError(f"NSE master too small: {len(out)}")
    return out

def quotes(a,m):
    out={}
    items=list(m.items())
    for i in range(0,len(items),QUOTE_BATCH):
        batch = items[i:i+QUOTE_BATCH]
        toks=[x[1]["token"] for x in batch]
        token_to_sym = {info["token"]: sym for sym, info in batch}
        try:
            # FIXED: signature is getMarketData(mode, exchangeTokens)
            r=a.getMarketData("FULL", {"NSE":toks})
            fetched = ((r or {}).get("data") or {}).get("fetched",[]) or []
            for x in fetched:
                tok = str(x.get("symbolToken") or "")
                sym = token_to_sym.get(tok)
                if sym:
                    out[sym]=x
        except Exception as e:
            print("Quote batch:",e)
        time.sleep(QUOTE_DELAY)
    return out

def candles(a,token,days=HISTORY):
    try:
        end=datetime.now(IST);start=end-timedelta(days=days)
        r=a.getCandleData({"exchange":"NSE","symboltoken":str(token),"interval":"ONE_DAY",
            "fromdate":start.strftime("%Y-%m-%d %H:%M"),"todate":end.strftime("%Y-%m-%d %H:%M")})
        z=(r or {}).get("data") or []
        if not z:return pd.DataFrame()
        d=pd.DataFrame(z,columns=["date","open","high","low","close","volume"])
        for c in ["open","high","low","close","volume"]: d[c]=pd.to_numeric(d[c],errors="coerce")
        d["date"]=pd.to_datetime(d["date"])
        return d.dropna().sort_values("date").drop_duplicates("date").set_index("date")
    except Exception as e:
        print("Candle:",e);return pd.DataFrame()

def comp(d): return d.iloc[:-1].copy() if len(d)>1 else d.copy()
def ema(s,n): return s.ewm(span=n,adjust=False).mean()
def wma(s,n):
    w=np.arange(1,n+1);return s.rolling(n).apply(lambda x:np.dot(x,w)/w.sum(),raw=True)
def hma(s,n): return wma(2*wma(s,n//2)-wma(s,n),max(1,int(np.sqrt(n))))
def rsi(s,n=14):
    d=s.diff();u=d.clip(lower=0);v=-d.clip(upper=0)
    au=u.ewm(alpha=1/n,adjust=False).mean();av=v.ewm(alpha=1/n,adjust=False).mean()
    return 100-100/(1+au/av.replace(0,np.nan))
def macd(s):
    m=ema(s,3)-ema(s,21);q=ema(m,9);return m,q,m-q
def atr(d,n=14):
    pc=d.close.shift();tr=pd.concat([d.high-d.low,(d.high-pc).abs(),(d.low-pc).abs()],axis=1).max(axis=1)
    return tr.rolling(n).mean()
def wk(d):
    return d.resample("W-FRI").agg({"open":"first","high":"max","low":"min","close":"last","volume":"sum"}).dropna()
def mo(d):
    return d.resample("ME").agg({"open":"first","high":"max","low":"min","close":"last","volume":"sum"}).dropna()

def index_status(d):
    d=comp(d)
    if len(d)<210:return {"status":"UNKNOWN","ret20":0}
    c=d.close;v=c.iloc[-1];d20=c.rolling(20).mean().iloc[-1];d50=c.rolling(50).mean().iloc[-1];d200=c.rolling(200).mean().iloc[-1]
    rr=(v/c.iloc[-21]-1)*100;rs=rsi(c,14).iloc[-1]
    if v>d20>d50>d200 and rs>55 and rr>3:s="POSITIVE"
    elif v<d20<d50 and rs<45 and rr<-3:s="NEGATIVE"
    else:s="SIDEWAYS"
    return {"status":s,"ret20":rr,"rsi":rs,"dma20":d20,"dma50":d50,"dma200":d200}

def stock_quality(d,q):
    d=comp(d)
    if len(d)<220:return None
    c=d.close.iloc[-1];av=d.volume.rolling(20).mean().iloc[-1];vol=d.volume.iloc[-1]
    hi=d.high.tail(252).max()
    if c<MIN_PRICE or av<MIN_AVG20 or vol<av*VOL_THRESHOLD or c<d.close.rolling(200).mean().iloc[-1] or c<hi*.92 or d.close.iloc[-1]<=d.open.iloc[-1]:return None
    return {"close":c,"volx":vol/av,"high52":hi,"avg20":av}

def sector_strength(data,nifty):
    nr=(nifty.close.iloc[-1]/nifty.close.iloc[-21]-1)*100
    res=[]
    for sec,stocks in SECTOR_MAP.items():
        xs=[data[s]["df"] for s in stocks if s in data]
        if not xs:continue
        rs=[];above=0
        for d in xs:
            d=comp(d)
            if len(d)<60:continue
            rr=(d.close.iloc[-1]/d.close.iloc[-21]-1)*100;rs.append(rr)
            if d.close.iloc[-1]>d.close.rolling(50).mean().iloc[-1]:above+=1
        if not rs:continue
        avg=float(np.mean(rs));pct=above/len(rs)*100
        label="FRESH" if avg-nr>=2 and pct>=60 else "STRONG" if avg-nr>0 and pct>=50 else "WEAK" if avg-nr<0 and pct<40 else "NEUTRAL"
        res.append({"sector":sec,"rs":avg-nr,"pct":pct,"label":label,"count":len(rs)})
    return sorted(res,key=lambda x:(x["label"] in ("FRESH","STRONG"),x["rs"],x["pct"]),reverse=True)

def nitin(d):
    w=comp(wk(d))
    if len(w)<60:return None
    c=w.close;h30=hma(c,30);h44=hma(c,44);m,s,h=macd(c);r=rsi(c,9);rm=r.rolling(3).mean();rw=wma(r,21)
    i=len(w)-1
    if not all(np.isfinite([h30.iloc[i],h44.iloc[i],m.iloc[i],s.iloc[i],r.iloc[i],rm.iloc[i],rw.iloc[i]])):return None
    cross=None
    for j in range(max(1,i-8),i+1):
        if c.iloc[j-1]<=h30.iloc[j-1] and c.iloc[j]>h30.iloc[j]:cross=j
    if cross is None:return None
    macdcross=None
    for j in range(max(1,i-6),i+1):
        if m.iloc[j-1]<=s.iloc[j-1] and m.iloc[j]>s.iloc[j]:macdcross=j
    if macdcross is None:return None
    neg=int((h.iloc[max(0,macdcross-14):macdcross]<0).sum())
    if neg<8 or c.iloc[i]<=h30.iloc[i] or h30.iloc[i]<h44.iloc[i] or r.iloc[i]<=50 or r.iloc[i]<=rm.iloc[i]:return None
    return {"h30":h30.iloc[i],"h44":h44.iloc[i],"rsi":r.iloc[i],"macdh":h.iloc[i],"neg":neg,"cross":i-cross,"macdcross":i-macdcross}

def chart_setup(d):
    d=comp(d)
    c=d.close
    e21=ema(c,21);e50=ema(c,50);e200=ema(c,200);a=atr(d,14)
    last=c.iloc[-1];atrv=a.iloc[-1]
    swing=d.low.tail(10).min()
    support=max(swing,e21.iloc[-1]*.985)
    sl=support-.5*atrv
    if sl>=last: sl=last-1.2*atrv
    risk=last-sl
    recent_high=d.high.tail(20).max()
    breakout=last>=recent_high*.995
    entry_low=max(e21.iloc[-1],support)
    entry_high=min(last, max(entry_low, recent_high*1.002))
    if entry_low>entry_high:entry_low=last*.985;entry_high=last
    extended=last>entry_high*1.025
    t1=last+2*risk;t2=last+3*risk
    daily_rsi=rsi(c,14).iloc[-1]
    setup="BREAKOUT" if breakout else "PULLBACK/RECLAIM"
    valid=(last>e21.iloc[-1] and e21.iloc[-1]>e50.iloc[-1] and e50.iloc[-1]>e200.iloc[-1] and not extended)
    return {"entry_low":entry_low,"entry_high":entry_high,"sl":sl,"t1":t1,"t2":t2,
            "support":support,"rsi":daily_rsi,"ema21":e21.iloc[-1],"ema50":e50.iloc[-1],
            "ema200":e200.iloc[-1],"atr":atrv,"breakout":breakout,"extended":extended,
            "setup":setup,"valid":valid}

def monthly(d):
    x=comp(mo(d))
    if len(x)<35:return None
    c=x.close;h10=hma(c,10);h30=hma(c,30)
    ok=c.iloc[-1]>h10.iloc[-1]>h30.iloc[-1]
    return {"ok":bool(ok),"h10":h10.iloc[-1],"h30":h30.iloc[-1]}

def fundamentals(sym):
    try:
        import yfinance as yf
        t=yf.Ticker(sym+".NS")
        inc=t.quarterly_income_stmt;cf=t.quarterly_cashflow;bs=t.balance_sheet
        if inc is None or inc.empty:return {"score":0,"text":"No quarterly data"}
        cols=list(inc.columns)
        def val(df,key,col):
            try:return float(df.loc[key,col])
            except:return np.nan
        rev=next((x for x in ["Total Revenue","Operating Revenue"] if x in inc.index),None)
        prof=next((x for x in ["Net Income","Net Income Common Stockholders"] if x in inc.index),None)
        opcf=next((x for x in ["Operating Cash Flow","Total Cash From Operating Activities"] if x in cf.index),None) if cf is not None and not cf.empty else None
        score=0;notes=[]
        if rev and len(cols)>=2:
            a=val(inc,rev,cols[0]);b=val(inc,rev,cols[1])
            if np.isfinite(a) and np.isfinite(b) and a>b:score+=20;notes.append("Revenue QoQ+")
        if prof and len(cols)>=2:
            a=val(inc,prof,cols[0]);b=val(inc,prof,cols[1])
            if np.isfinite(a) and np.isfinite(b) and a>b:score+=20;notes.append("Profit QoQ+")
        if prof and rev:
            p=val(inc,prof,cols[0]);r=val(inc,rev,cols[0])
            if np.isfinite(p) and np.isfinite(r) and r>0 and p>0:score+=15;notes.append("Profit positive")
        if opcf:
            v=val(cf,opcf,cf.columns[0])
            if np.isfinite(v) and v>0:score+=25;notes.append("OCF positive")
        if bs is not None and not bs.empty:
            debt=0
            for k in ["Total Debt","Long Term Debt","Current Debt"]:
                if k in bs.index:
                    z=val(bs,k,bs.columns[0])
                    if np.isfinite(z):debt+=z
            if debt<=0:score+=20;notes.append("Low/zero debt")
            else:score+=10;notes.append("Debt present")
        return {"score":min(100,score),"text":", ".join(notes) or "Mixed"}
    except Exception as e:return {"score":0,"text":"Fundamental unavailable"}

def stars(score):
    return "★★★★★" if score>=85 else "★★★★☆" if score>=75 else "★★★☆☆" if score>=65 else "★★☆☆☆"

def main():
    a=login();m=master();q=quotes(a,m)
    liquid=[]
    for s,x in q.items():
        try:
            l=float(x.get("ltp") or 0);v=float(x.get("tradeVolume") or x.get("volume") or 0)
            if l>0 and v>0:liquid.append((s,l*v,l))
        except:pass
    liquid=sorted(liquid,key=lambda x:x[1],reverse=True)[:TOP_LIQUID]
    print("Actual Liquid Top:",len(liquid))
    data={}
    for s,turn,l in liquid:
        d=candles(a,m[s]["token"])
        if len(d)>=220:
            data[s]={"df":d,"quality":stock_quality(d,q.get(s,{}))}
        time.sleep(CANDLE_DELAY)
    data={s:x for s,x in data.items() if x["quality"]}
    nt=None
    for n in ["NIFTY","NIFTY 50","NIFTY50"]:
        if n in m: nt=n;break
    if not nt: raise RuntimeError("NIFTY token not found")
    nd=candles(a,m[nt]["token"])
    ns=index_status(nd)
    st=None
    for n in ["SENSEX","BSE SENSEX"]:
        if n in m:st=n;break
    ss=index_status(candles(a,m[st]["token"])) if st else {"status":"UNAVAILABLE","ret20":0}
    if ns["status"]=="NEGATIVE" and ss["status"]=="NEGATIVE":
        print("Both NIFTY and SENSEX negative. BUY scan skipped.")
        return
    sec=sector_strength(data,comp(nd))
    goodsec=[x for x in sec if x["label"] in ("FRESH","STRONG")][:TOP_SECTOR]
    goodnames={x["sector"] for x in goodsec}
    candidates=[]
    for s,x in data.items():
        secname=SYMBOL_SECTOR.get(s)
        if secname not in goodnames:continue
        n=nitin(x["df"]); moq=monthly(x["df"]); cs=chart_setup(x["df"])
        if not n or not moq or not moq["ok"] or not cs["valid"]:continue
        sector=next(z for z in goodsec if z["sector"]==secname)
        candidates.append({"s":s,"df":x["df"],"n":n,"mo":moq,"cs":cs,"sec":sector})
    def techscore(z):
        n=z["n"];c=z["cs"];sec=z["sec"]
        ss=100 if sec["label"]=="FRESH" else 75
        rs=min(100,max(0,50+n["rsi"]-50))
        vol=min(100,z["df"].volume.iloc[-1]/max(1,z["df"].volume.rolling(20).mean().iloc[-1])*25)
        br=15 if c["breakout"] else 10
        return.25*ss+.35*rs+.20*min(100,c["rsi"]*1.25)+.10*vol+.10*br
    candidates=sorted(candidates,key=techscore,reverse=True)
    candidates=candidates[:TOP_FUNDAMENTAL]
    for z in candidates:
        z["fund"]=fundamentals(z["s"])
        z["score"]=.20*(100 if z["sec"]["label"]=="FRESH" else 75)+.30*min(100,z["n"]["rsi"]*1.5)+.20*min(100,z["cs"]["rsi"]*1.25)+.30*z["fund"]["score"]
    candidates=sorted(candidates,key=lambda z:z["score"],reverse=True)
    final=candidates[:MAX_SIGNALS]
    if not final:
        print("No final BUY setup.")
        return
    now=datetime.now(IST).strftime("%d %b %Y %I:%M %p")
    msg=f"🔥 DIVINE DWIJA V4.2 — SECTOR SWING\n⏰ {now}\n\n"
    msg+=f"BRAHMA — MARKET\nNIFTY: {ns['status']} ({ns['ret20']:+.1f}% 20D)\n"
    msg+=f"SENSEX: {ss['status']} ({ss['ret20']:+.1f}% 20D)\n\nVISHNU — TOP SECTORS\n"
    for i,z in enumerate(goodsec,1):
        msg+=f"{i}. {z['sector']} — {z['label']} | RS {z['rs']:+.1f}% | {z['pct']:.0f}% >50DMA\n"
    msg+="\nMAHESH — FINAL TECHNICAL BUY SETUPS\n━━━━━━━━━━━━━━━━━━\n"
    for i,z in enumerate(final,1):
        c=z["cs"];n=z["n"];f=z["fund"];d=z["df"];s=z["s"]
        ltp=float(d.close.iloc[-1]);volx=d.volume.iloc[-1]/max(1,d.volume.rolling(20).mean().iloc[-1])
        msg+=f"\n#{i} {s} {stars(z['score'])}\n"
        msg+=f"🏭 Sector: {z['sec']['sector']} | {z['sec']['label']}\n"
        msg+=f"📊 Score: {z['score']:.0f}/100 | Setup: {c['setup']}\n"
        msg+=f"💰 LTP: ₹{ltp:.2f}\n🎯 BUY ZONE: ₹{c['entry_low']:.2f}–₹{c['entry_high']:.2f}\n"
        msg+=f"🛑 SL: ₹{c['sl']:.2f}\n🎯 T1: ₹{c['t1']:.2f} | T2: ₹{c['t2']:.2f}\n"
        msg+=f"📐 R:R: 1:{(c['t1']-ltp)/max(.01,ltp-c['sl']):.1f} / 1:{(c['t2']-ltp)/max(.01,ltp-c['sl']):.1f}\n"
        msg+=f"📌 Support: ₹{c['support']:.2f} | Invalid below: ₹{c['sl']:.2f}\n"
        msg+=f"📈 Daily RSI: {c['rsi']:.1f} | Weekly RSI9: {n['rsi']:.1f}\n"
        msg+=f"Weekly HMA30/44: ₹{n['h30']:.2f}/₹{n['h44']:.2f}\n"
        msg+=f"MACD: bullish | <0 bars: {n['neg']} | Volume: {volx:.2f}x\n"
        msg+=f"Monthly: PASS | Fundamental: {f['score']}/100\n"
        msg+=f"🧾 {f['text']}\n"
        msg+="━━━━━━━━━━━━━━━━━━\n"
    tg(msg);print(msg)

if __name__=="__main__":
    main()
