import os,time,requests,pyotp,pandas as pd,numpy as np
from datetime import datetime,timedelta
from SmartApi import SmartConnect
import pytz

API_KEY=os.getenv("API_KEY");CLIENT_ID=os.getenv("CLIENT_ID");PASSWORD=os.getenv("PASSWORD");TOTP_SECRET=os.getenv("TOTP_SECRET")
TELEGRAM_BOT_TOKEN=os.getenv("TELEGRAM_BOT_TOKEN");TELEGRAM_CHAT_ID=os.getenv("TELEGRAM_CHAT_ID")
MASTER_URL="https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json";IST=pytz.timezone("Asia/Kolkata")
HISTORY=900;TOP_SECTOR=3;TOP_LIQUID=100;TOP_FUNDAMENTAL=10;MAX_SIGNALS=3
MIN_PRICE=50;MIN_AVG20=50000;VOL_THRESHOLD=1.8;CANDLE_DELAY=.35

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
    for s in v:SYMBOL_SECTOR.setdefault(s,k)

def tg(x):
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        try:requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",data={"chat_id":TELEGRAM_CHAT_ID,"text":x},timeout=15)
        except Exception as e:print("Telegram:",e)

def login():
    if not all([API_KEY,CLIENT_ID,PASSWORD,TOTP_SECRET]):raise RuntimeError("Credentials missing")
    a=SmartConnect(api_key=API_KEY)
    r=a.generateSession(CLIENT_ID,PASSWORD,pyotp.TOTP(TOTP_SECRET).now())
    if not r or not r.get("status"):raise RuntimeError(f"Login failed: {r}")
    return a

def master():
    r=requests.get(MASTER_URL,timeout=30);r.raise_for_status();out={}
    for x in r.json():
        if str(x.get("exch_seg","")).upper() not in ("NSE_CM","NSE"):continue
        sym=str(x.get("symbol","")).upper();tok=str(x.get("token",""))
        if sym and tok:out.setdefault(sym.replace("-EQ",""),{"token":tok,"symbol":sym})
    if len(out)<100:raise RuntimeError(f"NSE master too small: {len(out)}")
    return out

def candles(a,token,days=HISTORY):
    try:
        e=datetime.now(IST);s=e-timedelta(days=days)
        r=a.getCandleData({"exchange":"NSE","symboltoken":str(token),"interval":"ONE_DAY",
                           "fromdate":s.strftime("%Y-%m-%d %H:%M"),"todate":e.strftime("%Y-%m-%d %H:%M")})
        z=(r or {}).get("data") or []
        if not z:return pd.DataFrame()
        d=pd.DataFrame(z,columns=["date","open","high","low","close","volume"])
        for c in ["open","high","low","close","volume"]:d[c]=pd.to_numeric(d[c],errors="coerce")
        d["date"]=pd.to_datetime(d["date"])
        return d.dropna().sort_values("date").drop_duplicates("date").set_index("date")
    except Exception as e:
        print("Candle:",e);return pd.DataFrame()

def comp(d):
    return d.iloc[:-1].copy() if d is not None and len(d)>1 else (d.copy() if d is not None else pd.DataFrame())

def ema(s,n):return s.ewm(span=n,adjust=False).mean()

def wma(s,n):
    w=np.arange(1,n+1)
    return s.rolling(n).apply(lambda x:np.dot(x,w)/w.sum(),raw=True)

def hma(s,n):return wma(2*wma(s,n//2)-wma(s,n),max(1,int(np.sqrt(n))))

def rsi(s,n=14):
    d=s.diff();u=d.clip(lower=0);v=-d.clip(upper=0)
    au=u.ewm(alpha=1/n,adjust=False).mean();av=v.ewm(alpha=1/n,adjust=False).mean()
    return 100-100/(1+au/av.replace(0,np.nan))

def macd(s):
    m=ema(s,3)-ema(s,21);q=ema(m,9);return m,q,m-q

def atr(d,n=14):
    pc=d.close.shift()
    tr=pd.concat([d.high-d.low,(d.high-pc).abs(),(d.low-pc).abs()],axis=1).max(axis=1)
    return tr.rolling(n).mean()

def wk(d):
    return d.resample("W-FRI").agg({"open":"first","high":"max","low":"min","close":"last","volume":"sum"}).dropna()

def mo(d):
    return d.resample("ME").agg({"open":"first","high":"max","low":"min","close":"last","volume":"sum"}).dropna()

def index_status(d):
    d=comp(d)
    if len(d)<210:return {"status":"UNKNOWN","ret20":0,"rsi":50}
    c=d.close;v=c.iloc[-1];d20=c.rolling(20).mean().iloc[-1];d50=c.rolling(50).mean().iloc[-1];d200=c.rolling(200).mean().iloc[-1]
    rr=(v/c.iloc[-21]-1)*100;rs=rsi(c,14).iloc[-1]
    st="POSITIVE" if v>d20>d50>d200 and rs>55 and rr>3 else "NEGATIVE" if v<d20<d50 and rs<45 and rr<-3 else "SIDEWAYS"
    return {"status":st,"ret20":rr,"rsi":rs}

def sector_strength(data,nifty):
    n=comp(nifty)
    nr=(n.close.iloc[-1]/n.close.iloc[-21]-1)*100 if len(n)>=25 else 0
    res=[]
    for sec,stocks in SECTOR_MAP.items():
        xs=[data[s]["df"] for s in stocks if s in data];rs=[];above=0
        for d in xs:
            d=comp(d)
            if len(d)<60:continue
            rs.append((d.close.iloc[-1]/d.close.iloc[-21]-1)*100)
            above+=d.close.iloc[-1]>d.close.rolling(50).mean().iloc[-1]
        if not rs:continue
        avg=float(np.mean(rs));pct=above/len(rs)*100;rel=avg-nr
        lab="FRESH" if rel>=2 and pct>=60 else "STRONG" if rel>0 and pct>=50 else "WEAK" if rel<0 and pct<40 else "NEUTRAL"
        res.append({"sector":sec,"rs":rel,"pct":pct,"label":lab})
    return sorted(res,key=lambda x:(x["label"] in ("FRESH","STRONG"),x["rs"],x["pct"]),reverse=True)

def quality(d):
    d=comp(d)
    if len(d)<220:return None
    c=d.close.iloc[-1];av=d.volume.rolling(20).mean().iloc[-1];v=d.volume.iloc[-1];hi=d.high.tail(252).max()
    if c<MIN_PRICE or av<MIN_AVG20 or v<av*VOL_THRESHOLD or c<d.close.rolling(200).mean().iloc[-1] or c<hi*.92 or d.close.iloc[-1]<=d.open.iloc[-1]:return None
    return {"volx":v/av,"high52":hi,"avg20":av}

def monthly(d):
    x=comp(mo(d))
    if len(x)<35:return None
    c=x.close;h10=hma(c,10);h30=hma(c,30);ok=c.iloc[-1]>h10.iloc[-1]>h30.iloc[-1]
    return {"ok":bool(ok),"trend":"BULLISH" if ok else "WEAK","h10":h10.iloc[-1],"h30":h30.iloc[-1],"close":c.iloc[-1]}

def weekly(d):
    x=comp(wk(d))
    if len(x)<60:return None
    c=x.close;h30=hma(c,30);h44=hma(c,44);m,s,h=macd(c);rr=rsi(c,9);rm=rr.rolling(3).mean();i=len(x)-1
    cross=any(c.iloc[j-1]<=h30.iloc[j-1] and c.iloc[j]>h30.iloc[j] for j in range(max(1,i-8),i+1))
    mcross=any(m.iloc[j-1]<=s.iloc[j-1] and m.iloc[j]>s.iloc[j] for j in range(max(1,i-6),i+1))
    neg=int((h.iloc[max(0,i-14):i]<0).sum())
    ok=c.iloc[i]>h30.iloc[i]>h44.iloc[i] and rr.iloc[i]>50 and rr.iloc[i]>rm.iloc[i] and cross and mcross and neg>=8
    return {"ok":bool(ok),"rsi":rr.iloc[i],"h30":h30.iloc[i],"h44":h44.iloc[i],"macdh":h.iloc[i],"neg":neg}

def daily(d):
    x=comp(d);c=x.close;e21=ema(c,21);e50=ema(c,50);e200=ema(c,200);a=atr(x,14)
    last=c.iloc[-1];av=a.iloc[-1];sw=x.low.tail(10).min();support=max(sw,e21.iloc[-1]*.985);sl=support-.5*av
    if sl>=last:sl=last-1.2*av
    risk=last-sl;rh=x.high.tail(20).max();breakout=last>=rh*.995
    entry_low=max(e21.iloc[-1],support);entry_high=min(last,max(entry_low,rh*1.002));extended=last>entry_high*1.025
    valid=last>e21.iloc[-1]>e50.iloc[-1]>e200.iloc[-1] and not extended
    return {"valid":bool(valid),"setup":"BREAKOUT" if breakout else "PULLBACK/RECLAIM",
            "entry_low":entry_low,"entry_high":entry_high,"sl":sl,"t1":last+2*risk,"t2":last+3*risk,
            "support":support,"rsi":rsi(c,14).iloc[-1],"ema21":e21.iloc[-1],"ema50":e50.iloc[-1],
            "ema200":e200.iloc[-1],"atr":av,"breakout":breakout,"high52":x.high.tail(252).max(),"ltp":last}

def fundamentals(sym):
    try:
        import yfinance as yf
        t=yf.Ticker(sym+".NS");inc=t.quarterly_income_stmt;cf=t.quarterly_cashflow;bs=t.balance_sheet
        if inc is None or inc.empty:return {"score":0,"text":"No quarterly data"}
        cols=list(inc.columns);score=0;notes=[]
        def val(df,key,col):
            try:return float(df.loc[key,col])
            except:return np.nan
        rev=next((x for x in ["Total Revenue","Operating Revenue"] if x in inc.index),None)
        prof=next((x for x in ["Net Income","Net Income Common Stockholders"] if x in inc.index),None)
        opcf=next((x for x in ["Operating Cash Flow","Total Cash From Operating Activities"] if x in cf.index),None) if cf is not None and not cf.empty else None
        if rev and len(cols)>=2 and np.isfinite(val(inc,rev,cols[0])) and np.isfinite(val(inc,rev,cols[1])) and val(inc,rev,cols[0])>val(inc,rev,cols[1]):score+=20;notes.append("Revenue QoQ+")
        if prof and len(cols)>=2 and np.isfinite(val(inc,prof,cols[0])) and np.isfinite(val(inc,prof,cols[1])) and val(inc,prof,cols[0])>val(inc,prof,cols[1]):score+=20;notes.append("Profit QoQ+")
        if prof and rev and val(inc,prof,cols[0])>0 and val(inc,rev,cols[0])>0:score+=15;notes.append("Profit positive")
        if opcf and val(cf,opcf,cf.columns[0])>0:score+=25;notes.append("OCF positive")
        debt=0
        if bs is not None and not bs.empty:
            for k in ["Total Debt","Long Term Debt","Current Debt"]:
                if k in bs.index:
                    z=val(bs,k,bs.columns[0])
                    if np.isfinite(z):debt+=z
        score+=20 if debt<=0 else 10;notes.append("Low/zero debt" if debt<=0 else "Debt present")
        return {"score":min(100,score),"text":", ".join(notes) or "Mixed"}
    except:return {"score":0,"text":"Fundamental unavailable"}

def stars(s):
    return "★★★★★" if s>=85 else "★★★★☆" if s>=75 else "★★★☆☆" if s>=65 else "★★☆☆☆"

def no_setup(reason=""):
    msg="SWING SECTOR NO SETUP"
    if reason:msg+=f"\nReason: {reason}"
    msg+=f"\n⏰ {datetime.now(IST).strftime('%d %b %Y %I:%M %p')}"
    tg(msg);print(msg)

def main():
    a=login();m=master()

    # Market mood is informational only. It MUST NOT block sector scanning when unavailable.
    nifty=next((x for x in ["NIFTY","NIFTY 50","NIFTY50"] if x in m),None)
    sensex=next((x for x in ["SENSEX","BSE SENSEX"] if x in m),None)
    if not nifty:
        no_setup("NIFTY data unavailable")
        return

    nd=candles(a,m[nifty]["token"])
    ns=index_status(nd)
    sd=candles(a,m[sensex]["token"]) if sensex else pd.DataFrame()
    ss=index_status(sd) if sensex else {"status":"UNAVAILABLE","ret20":0,"rsi":50}
    print("MARKET:",ns["status"],ss["status"])

    # Only skip when both indices have confirmed negative completed-candle trend.
    # UNKNOWN/UNAVAILABLE is allowed to continue.
    if ns["status"]=="NEGATIVE" and ss["status"]=="NEGATIVE":
        no_setup("NIFTY and SENSEX both negative")
        return

    sector_symbols=set(SYMBOL_SECTOR)
    sector_master={s:m[s] for s in sector_symbols if s in m}
    print("Sector universe:",len(sector_master))

    sector_data={}
    for s,info in sector_master.items():
        d=candles(a,info["token"])
        if len(d)>=220:sector_data[s]={"df":d}
        time.sleep(CANDLE_DELAY)

    sec=sector_strength(sector_data,comp(nd))
    goodsec=[x for x in sec if x["label"] in ("FRESH","STRONG")][:TOP_SECTOR]

    if not goodsec:
        no_setup("No strong sector found")
        return

    goodnames={x["sector"] for x in goodsec}
    print("TOP SECTORS:",goodnames)

    selected={s:x for s,x in sector_data.items() if SYMBOL_SECTOR.get(s) in goodnames}
    print("Stocks in top sectors:",len(selected))

    liquid=[]
    for s,x in selected.items():
        d=comp(x["df"]);ltp=float(d.close.iloc[-1]);vol=float(d.volume.iloc[-1])
        if ltp>0 and vol>0:liquid.append((s,ltp*vol,ltp))
    liquid=sorted(liquid,key=lambda z:z[1],reverse=True)[:TOP_LIQUID]
    print("Liquid stocks after sector filter:",len(liquid))

    data={s:{"df":selected[s]["df"]} for s,_,_ in liquid}
    data={s:x for s,x in data.items() if quality(x["df"])}

    candidates=[]
    for s,x in data.items():
        d=x["df"];w=weekly(d);moq=monthly(d);di=daily(d)
        if not w or not w["ok"] or not moq or not moq["ok"] or not di["valid"]:continue
        sector=next(z for z in goodsec if z["sector"]==SYMBOL_SECTOR[s])
        candidates.append({"s":s,"df":d,"w":w,"mo":moq,"di":di,"sec":sector})

    if not candidates:
        no_setup("No stock passed Monthly + Weekly + Daily setup")
        return

    candidates=sorted(candidates,key=lambda z:
        .25*(100 if z["sec"]["label"]=="FRESH" else 75)+
        .25*min(100,z["w"]["rsi"]*1.5)+
        .25*min(100,z["di"]["rsi"]*1.25)+
        .15*min(100,(z["df"].volume.iloc[-1]/max(1,z["df"].volume.rolling(20).mean().iloc[-1]))*25)+
        .10*(15 if z["di"]["breakout"] else 10),reverse=True)[:TOP_FUNDAMENTAL]

    for z in candidates:
        z["fund"]=fundamentals(z["s"])
        z["score"]=.20*(100 if z["sec"]["label"]=="FRESH" else 75)+.20*min(100,z["w"]["rsi"]*1.5)+.20*min(100,z["di"]["rsi"]*1.25)+.40*z["fund"]["score"]

    final=sorted(candidates,key=lambda z:z["score"],reverse=True)[:MAX_SIGNALS]
    if not final:
        no_setup("No final BUY setup")
        return

    now=datetime.now(IST).strftime("%d %b %Y %I:%M %p")
    msg=f"🔥 DIVINE DWIJA V4.3 — SECTOR-FIRST SWING\n⏰ {now}\n\n"
    msg+=f"🧠 MARKET MOOD\nNIFTY: {ns['status']} ({ns['ret20']:+.1f}% 20D)\nSENSEX: {ss['status']} ({ss['ret20']:+.1f}% 20D)\n\n"
    msg+="🔥 TOP SECTORS\n"
    for i,z in enumerate(goodsec,1):msg+=f"{i}. {z['sector']} — {z['label']} | RS {z['rs']:+.1f}% | {z['pct']:.0f}% >50DMA\n"
    msg+="\n🏆 TOP 3 SWING SETUPS\n━━━━━━━━━━━━━━━━━━\n"
    for i,z in enumerate(final,1):
        d=z["df"];di=z["di"];w=z["w"];moq=z["mo"];f=z["fund"]
        volx=d.volume.iloc[-1]/max(1,d.volume.rolling(20).mean().iloc[-1])
        msg+=f"\n#{i} {z['s']} {stars(z['score'])}\n🏭 Sector: {z['sec']['sector']} | {z['sec']['label']}\n📊 Score: {z['score']:.0f}/100 | Setup: {di['setup']}\n"
        msg+=f"\n📅 MONTHLY\nTrend: {moq['trend']} | HMA10 ₹{moq['h10']:.2f} > HMA30 ₹{moq['h30']:.2f}\n"
        msg+=f"📅 WEEKLY\nTrend: BULLISH | RSI9 {w['rsi']:.1f} | HMA30/44 ₹{w['h30']:.2f}/₹{w['h44']:.2f} | MACD Bullish\n"
        msg+=f"📅 DAILY\nEMA21/50/200 ₹{di['ema21']:.2f}/₹{di['ema50']:.2f}/₹{di['ema200']:.2f} | RSI14 {di['rsi']:.1f}\n"
        msg+=f"Volume: {volx:.2f}x | 52W High: ₹{di['high52']:.2f}\n\n🎯 TRADE PLAN\nBUY ZONE: ₹{di['entry_low']:.2f}–₹{di['entry_high']:.2f}\n"
        msg+=f"ENTRY: ₹{di['entry_high']:.2f}\n🛑 SL: ₹{di['sl']:.2f}\n🎯 T1: ₹{di['t1']:.2f} | T2: ₹{di['t2']:.2f}\n"
        msg+=f"📐 R:R: 1:{(di['t1']-di['entry_high'])/max(.01,di['entry_high']-di['sl']):.1f} / 1:{(di['t2']-di['entry_high'])/max(.01,di['entry_high']-di['sl']):.1f}\n"
        msg+=f"📌 Support: ₹{di['support']:.2f} | Invalid below: ₹{di['sl']:.2f}\n"
        msg+=f"Fundamental: {f['score']}/100 | {f['text']}\n━━━━━━━━━━━━━━━━━━\n"
    tg(msg);print(msg)

if __name__=="__main__":main()
