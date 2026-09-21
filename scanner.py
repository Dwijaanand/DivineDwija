import os,time,requests,pyotp,pandas as pd,numpy as np,re,logging
from datetime import datetime,timedelta
from SmartApi import SmartConnect
import pytz
logging.getLogger("smartapi.smartConnect").setLevel(logging.ERROR)

API_KEY=os.getenv("API_KEY");CLIENT_ID=os.getenv("CLIENT_ID");PASSWORD=os.getenv("PASSWORD");TOTP_SECRET=os.getenv("TOTP_SECRET")
TELEGRAM_BOT_TOKEN=os.getenv("TELEGRAM_BOT_TOKEN");TELEGRAM_CHAT_ID=os.getenv("TELEGRAM_CHAT_ID")
MASTER_URL="https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"

MIN_PRICE=50;MIN_VOL=100000;MIN_AVG20=20000
MIN_HISTORY=180
IPO_MIN_CALENDAR=365
TOP_STOCKS_PER_SECTOR=12;TOP_SECTORS=3;TOP_SIGNALS=3;MIN_SCORE=70;MIN_FUND=10
DAILY_DAYS=520;WEEKLY_DAYS=1800;MONTHLY_DAYS=3500
FUND_DELAY=.25;QUOTE_BATCH=50;QUOTE_DELAY=1.02
IST=pytz.timezone("Asia/Kolkata")
EXCLUDED={"LTIM"}

SECTOR_MAP={
"BANKING":set("HDFCBANK ICICIBANK SBIN AXISBANK KOTAKBANK INDUSINDBK BANKBARODA PNB FEDERALBNK CANBK IDFCFIRSTB RBLBANK BANDHANBNK AUBANK".split()),
"IT":set("TCS INFY HCLTECH WIPRO TECHM PERSISTENT COFORGE MPHASIS LTTS KPIT CYIENT OFSS LTIM".split()),
"AUTO":set("MARUTI M&M BAJAJ-AUTO BAJAJAUTO EICHERMOT HEROMOTOCO TVSMOTOR ASHOKLEY BOSCH BHARATFORG EXIDEIND MOTHERSON MOTHERSUMI TATAMOTORS".split()),
"PHARMA":set("SUNPHARMA DRREDDY CIPLA DIVISLAB AUROPHARMA LUPIN ZYDUSLIFE ALKEM TORRENTPHARM GLAND APOLLOHOSP MAXHEALTH".split()),
"METALS":set("TATASTEEL JSWSTEEL HINDALCO SAIL JINDALSTEL NMDC VEDL HINDZINC NATIONALUM MOIL".split()),
"ENERGY":set("RELIANCE ONGC NTPC POWERGRID COALINDIA ADANIGREEN ADANIPOWER TATAPOWER BPCL IOC GAIL PETRONET".split()),
"FMCG":set("HINDUNILVR ITC NESTLEIND BRITANNIA DABUR MARICO GODREJCP COLPAL TATACONSUM VBL".split()),
"FINANCE":set("BAJFINANCE BAJAJFINSV SHRIRAMFIN CHOLAFIN MUTHOOTFIN MANAPPURAM PFC RECLTD IRFC LICHSGFIN BAJAJHFL BLS SAMMAANCAP".split()),
"REALTY":set("DLF GODREJPROP OBEROIRLTY PRESTIGE PHOENIX SOBHA BRIGADE".split()),
"INFRA":set("LT LARSEN RVNL IRCON NBCC NCC KEC HCC APLAPOLLO".split()),
"CAPITAL_GOODS":set("SIEMENS ABB BHEL BEL HAL CGPOWER THERMAX CUMMINSIND SCHNEIDER ELX".split()),
"CHEMICALS":set("PIDILITIND SRF DEEPAKNTR NAVINFLUOR ATUL CLEAN AARTIIND PIIND TATACHEM UPL".split()),
"TELECOM":set("BHARTIARTL IDEA INDUSTOWER".split()),
"MEDIA":set("ZEEL SUNTV PVRINOX NETWORK18".split()),
"CONSUMER":set("TITAN TRENT KALYANKJIL PAGEIND JUBLFOOD DMART NYKAA ZOMATO ETERNAL".split()),
"POWER":set("TATAPOWER TORNTPOWER CESC JSWENERGY NHPC SJVN".split()),
}

def tg(x):
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        try:requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",data={"chat_id":TELEGRAM_CHAT_ID,"text":x},timeout=12)
        except:pass
def login():
    s=SmartConnect(api_key=API_KEY)
    r=s.generateSession(CLIENT_ID,PASSWORD,pyotp.TOTP(TOTP_SECRET).now())
    if not r or not r.get("status"):raise RuntimeError("Angel login failed")
    return s
def master():
    x=pd.DataFrame(requests.get(MASTER_URL,timeout=30).json())
    x["token"]=x["token"].astype(str);x["symbol"]=x["symbol"].astype(str);return x
def equity_master(m):return m[(m["exch_seg"]=="NSE")&m["symbol"].str.endswith("-EQ")].copy()
def quotes(s,tokens):
    out=[]
    for i in range(0,len(tokens),QUOTE_BATCH):
        try:
            r=s.getMarketData("FULL",{"NSE":[str(x) for x in tokens[i:i+QUOTE_BATCH]]});d=(r or {}).get("data",{});out+=(d.get("fetched",[]) if isinstance(d,dict) else [])
        except:pass
        time.sleep(QUOTE_DELAY)
    return pd.DataFrame(out)
def candles(s,tok,days,interval,exchange="NSE"):
    try:
        e=datetime.now();b=e-timedelta(days=days)
        r=s.getCandleData({"exchange":exchange,"symboltoken":str(tok),"interval":interval,"fromdate":b.strftime("%Y-%m-%d %H:%M"),"todate":e.strftime("%Y-%m-%d %H:%M")})
        d=(r or {}).get("data")
        if not d:return None
        x=pd.DataFrame(d,columns=["timestamp","open","high","low","close","volume"])
        x["timestamp"]=pd.to_datetime(x.timestamp,errors="coerce")
        for c in ["open","high","low","close","volume"]:x[c]=pd.to_numeric(x[c],errors="coerce")
        return x.dropna(subset=["timestamp","close"]).sort_values("timestamp").reset_index(drop=True)
    except:return None
def rsi(s,n=14):
    d=s.diff();u=d.clip(lower=0).ewm(alpha=1/n,adjust=False).mean();v=(-d.clip(upper=0)).ewm(alpha=1/n,adjust=False).mean()
    return 100-100/(1+u/v.replace(0,np.nan))
def indicators(x):
    x=x.copy()
    for n in [10,20,30,50,100,200]:x[f"ema{n}"]=x.close.ewm(span=n,adjust=False).mean()
    x["rsi"]=rsi(x.close)
    tr=pd.concat([x.high-x.low,(x.high-x.close.shift()).abs(),(x.low-x.close.shift()).abs()],axis=1).max(axis=1)
    x["atr"]=tr.ewm(span=14,adjust=False).mean();x["vavg20"]=x.volume.rolling(20).mean();x["vema20"]=x.volume.ewm(span=20,adjust=False).mean()
    x["volx"]=x.volume/x.vavg20.replace(0,np.nan);x["volemx"]=x.volume/x.vema20.replace(0,np.nan)
    x["prev20h"]=x.high.shift(1).rolling(20).max();x["prev20l"]=x.low.shift(1).rolling(20).min()
    return x
def completed(x):return None if x is None or len(x)<3 else x.iloc[-2]
def clean_symbol(sym):return str(sym).replace("-EQ","").upper().strip()
def sector_name(sym):
    b=clean_symbol(sym)
    for k,v in SECTOR_MAP.items():
        if b in v:return k
    return "OTHER"
def screener_symbol(sym):
    b=clean_symbol(sym);fixes={"M&M":"M_M","BAJAJ-AUTO":"BAJAJAUTO","MOTHERSUMI":"MOTHERSON","L&TFH":"LTF"}
    return fixes.get(b,b.replace("&","_"))
def num(v):
    try:
        if v is None:return np.nan
        z=str(v).replace(",","").replace("%","").replace("₹","").strip()
        if z in ("","-","--","None","nan"):return np.nan
        return float(z)
    except:return np.nan
def fund_score(f):
    if not f:return 0
    score=0
    def ok(v):return pd.notna(v)
    if ok(f.get("roce")):score+=20 if f["roce"]>=20 else 15 if f["roce"]>=15 else 8 if f["roce"]>=10 else 0
    if ok(f.get("roe")):score+=15 if f["roe"]>=20 else 10 if f["roe"]>=15 else 5 if f["roe"]>=10 else 0
    if ok(f.get("de")):score+=15 if f["de"]<=0.5 else 10 if f["de"]<=1 else 4 if f["de"]<=2 else 0
    if ok(f.get("sales5")):score+=10 if f["sales5"]>=10 else 6 if f["sales5"]>=5 else 0
    if ok(f.get("profit5")):score+=15 if f["profit5"]>=15 else 10 if f["profit5"]>=7 else 0
    if ok(f.get("eps5")):score+=10 if f["eps5"]>=15 else 5 if f["eps5"]>=7 else 0
    if ok(f.get("qprofit")):score+=5 if f["qprofit"]>0 else 0
    if ok(f.get("qsales")):score+=5 if f["qsales"]>0 else 0
    if ok(f.get("pledge")):score+=5 if f["pledge"]<1 else 2 if f["pledge"]<5 else 0
    return min(100,score)
def fundamental(sym):
    try:
        b=clean_symbol(sym);url=f"https://www.screener.in/company/{screener_symbol(b)}/"
        html=requests.get(url,headers={"User-Agent":"Mozilla/5.0"},timeout=10).text
        if "Company not found" in html or len(html)<5000:return None
        def grab(label):
            p=html.find(f">{label}<")
            if p<0:p=html.lower().find(label.lower())
            if p<0:return np.nan
            chunk=html[p:p+1800];m=re.search(r'<span[^>]*class="number"[^>]*>\s*([^<]+)',chunk)
            if not m:m=re.search(r'<td[^>]*>\s*([^<]+)',chunk)
            return num(m.group(1)) if m else np.nan
        vals={"roce":grab("ROCE"),"roe":grab("ROE"),"de":grab("Debt to equity"),"pe":grab("P/E"),"pledge":grab("Pledged"),"sales5":grab("Sales growth 5Years"),"profit5":grab("Profit growth 5Years"),"eps5":grab("EPS growth 5Years"),"qsales":grab("YOY Quarterly sales growth"),"qprofit":grab("YOY Quarterly profit growth")}
        vals["available"]=sum(pd.notna(v) for v in vals.values())>=3
        if not vals["available"]:return None
        vals["fund_score"]=fund_score(vals);vals["source"]="Screener";return vals
    except:return None
def market_mood(s):
    vals=[]
    for name,tok,ex in [("NIFTY","99926000","NSE"),("SENSEX","99919000","BSE")]:
        d=candles(s,tok,45,"ONE_DAY",ex)
        if d is None or len(d)<25:continue
        x=indicators(d);a=completed(x)
        if a is None:continue
        bull=a.close>a.ema20>a.ema50 and a.rsi>=50;bear=a.close<a.ema20<a.ema50 and a.rsi<=50
        vals.append(1 if bull else -1 if bear else 0)
    z=sum(vals);return ("BULLISH" if z>0 else "BEARISH" if z<0 else "NEUTRAL"),z
def is_junk(sym):
    b=clean_symbol(sym)
    if b.endswith("BEES"):return True
    if b.endswith("ETF"):return True
    if b in ("MASPTOP50","MOM50","MONQ50","MON100","HDFCGOLD","MOM100"):return True
    return False
def sector_scan(s,q):
    rows_list=[];daily_cache={}
    for _,r in q.iterrows():
        sym=r["symbol"];b=clean_symbol(sym)
        if b in EXCLUDED or is_junk(sym):continue
        d=candles(s,r["token"],DAILY_DAYS,"ONE_DAY")
        if d is None or len(d) < MIN_HISTORY:continue
        try:
            cal_days=(d.timestamp.iloc[-1]-d.timestamp.iloc[0]).days
            if cal_days < IPO_MIN_CALENDAR:continue
        except:continue
        x=indicators(d);a=completed(x)
        if a is None or pd.isna(a.vavg20) or a.vavg20<MIN_AVG20:continue
        tech=0;tech+=25 if a.close>a.ema50 else 0;tech+=25 if a.ema20>a.ema50 else 0;tech+=20 if a.close>a.ema200 else 0;tech+=15 if a.rsi>=50 else 0;tech+=15 if a.volume>a.vema20 else 0
        rows_list.append({"symbol":sym,"token":str(r["token"]),"ltp":float(r["ltp"]),"sector":sector_name(sym),"tech":tech,"history":cal_days,"candle_count":len(d)})
        daily_cache[str(r["token"])]=(d,x)
    if not rows_list:return [],[],daily_cache
    df=pd.DataFrame(rows_list)
    ss=df.groupby("sector").agg(avg_tech=("tech","mean"),breadth=("tech",lambda x:float((x>=60).mean()*100)),count=("tech","size")).reset_index()
    ss["score"]=ss.avg_tech*0.65+ss.breadth*0.35
    known=ss[ss.sector!="OTHER"].sort_values("score",ascending=False).head(TOP_SECTORS)
    return known.sort_values("score",ascending=False).to_dict("records"),df.to_dict("records"),daily_cache
def multi_tf_setup(s,tok,live,mbias,sector_score,f,daily_cache):
    tok=str(tok);cached=daily_cache.get(tok)
    if cached:daily,x=cached
    else:
        daily=candles(s,tok,DAILY_DAYS,"ONE_DAY")
        if daily is None:return None
        x=indicators(daily)
    weekly=candles(s,tok,WEEKLY_DAYS,"ONE_WEEK");monthly=candles(s,tok,MONTHLY_DAYS,"ONE_MONTH")
    if weekly is None or monthly is None or len(daily)<MIN_HISTORY or len(weekly)<80 or len(monthly)<24:return None
    w=indicators(weekly);m=indicators(monthly);a=completed(x);b=completed(w);c=completed(m)
    if any(v is None for v in [a,b,c]):return None
    mon=0;mon+=25 if c.close>c.ema10 else 0;mon+=25 if c.ema10>c.ema30 else 0;mon+=25 if c.close>c.ema100 else 0;mon+=15 if c.rsi>=50 else 0;mon+=10 if c.volume>c.vema20 else 0
    wk=0;wk+=20 if b.close>b.ema10 else 0;wk+=20 if b.ema10>b.ema30 else 0;wk+=20 if b.close>b.ema100 else 0;wk+=15 if b.rsi>=50 else 0;wk+=15 if b.volume>b.vema20 else 0;wk+=10 if b.volx>=1.0 else 0
    dy=0;dy+=15 if a.close>a.ema20 else 0;dy+=15 if a.ema20>a.ema50 else 0;dy+=15 if a.close>a.ema200 else 0;dy+=10 if 52<=a.rsi<=72 else 0;dy+=15 if a.volume>a.vema20 else 0;dy+=10 if a.volemx>=1.2 else 0;dy+=10 if a.close>a.prev20h else 0;dy+=10 if a.close>a.open else 0
    if not (c.close>c.ema10>c.ema30 and b.close>b.ema10>b.ema30 and a.close>a.ema20>a.ema50):return None
    if pd.isna(a.vema20) or a.volemx<1.0:return None
    atr=max(float(a.atr),live*.005);swing=float(daily.low.iloc[-12:-2].min());sl=min(live-atr,swing-.20*atr)
    if sl>=live:sl=live-atr
    risk=live-sl
    if risk/live<.006:return None
    t1=live+1.5*risk;t2=live+2.0*risk;t3=live+3.0*risk
    fund=f.get("fund_score",15) if f else 15;fund_pts=round(fund*.20);market_pts=8 if mbias=="BULLISH" else 0 if mbias=="NEUTRAL" else -8;sec_pts=min(15,round(sector_score*.15))
    total=min(100,round(mon*.15+wk*.20+dy*.20+fund_pts+sec_pts+max(0,market_pts)))
    if mbias=="BEARISH":total=max(0,total-8)
    if total<MIN_SCORE:return None
    return {"direction":"BUY","score":total,"fund":fund,"fund_pts":fund_pts,"monthly":mon,"weekly":wk,"daily":dy,"sector_pts":sec_pts,"market_pts":market_pts,"entry":live,"sl":sl,"t1":t1,"t2":t2,"t3":t3,"rsi":float(a.rsi),"volx":float(a.volx),"daily_volemx":float(a.volemx),"weekly_volx":float(b.volx),"monthly_volx":float(c.volx),"setup":"BREAKOUT + VOLUME" if pd.notna(a.prev20h) and a.close>a.prev20h and a.volemx>=1.2 else "EMA TREND + VOLUME"}
def stars(s):return "★★★★★" if s>=90 else "★★★★☆" if s>=80 else "★★★☆☆" if s>=70 else "★★☆☆☆"
def main():
    print(f"=== DIVINE SECTOR SWING V5.6 | {datetime.now(IST):%d %b %H:%M:%S IST} ===",flush=True)
    s=login();m=master();em=equity_master(m)
    mbias,mval=market_mood(s);print(f"MARKET MOOD: {mbias} | score {mval}",flush=True)
    q=quotes(s,em.token.tolist())
    if q.empty:tg("⚠️ Sector Swing: live quotes unavailable");return
    q["symbolToken"]=q.symbolToken.astype(str);q["ltp"]=pd.to_numeric(q.ltp,errors="coerce");q["tradeVolume"]=pd.to_numeric(q.tradeVolume,errors="coerce")
    q=q.dropna(subset=["symbolToken","ltp","tradeVolume"]);q=q[(q.ltp>=MIN_PRICE)&(q.tradeVolume>=MIN_VOL)]
    q=q.merge(em[["symbol","token"]].drop_duplicates("token"),left_on="symbolToken",right_on="token",how="left").dropna(subset=["symbol"])
    q=q[q.symbol.map(clean_symbol).map(lambda x:x not in EXCLUDED)]
    q=q.sort_values("tradeVolume",ascending=False).head(220)
    print(f"HIGH-VOLUME UNIVERSE: {len(q)}",flush=True)
    sectors,universe,daily_cache=sector_scan(s,q)
    if not sectors:
        out=f"⚡ DIVINE SECTOR SWING V5.6\n{datetime.now(IST):%d-%b %H:%M IST}\nMarket: {mbias}\n\n⚠️ No strong sector found.";print(out);tg(out);return
    print("SECTORS:",", ".join(f'{z["sector"]} {z["score"]:.1f}' for z in sectors),flush=True)
    secset={z["sector"]:z["score"] for z in sectors}
    candidates=[z for z in universe if z["sector"] in secset]
    other_pool=[z for z in universe if z["sector"]=="OTHER"]
    bysec={}
    for z in candidates:bysec.setdefault(z["sector"],[]).append(z)
    final_candidates=[]
    for sec,lst in bysec.items():final_candidates+=sorted(lst,key=lambda z:z["tech"],reverse=True)[:TOP_STOCKS_PER_SECTOR]
    if len(final_candidates) < 36:
        need=36-len(final_candidates)
        final_candidates+=sorted(other_pool,key=lambda z:z["tech"],reverse=True)[:need]
    print(f"TOP STOCKS: {len(final_candidates)} | 180C/365D DUAL: ON | ETF: ON | IPO: 1-YEAR",flush=True)
    results=[]
    for n,z in enumerate(final_candidates,1):
        print(f"[{n}/{len(final_candidates)}] {z['symbol']} | {z['sector']} | {z['history']}D/{z['candle_count']}c",flush=True)
        f=fundamental(z["symbol"]);time.sleep(FUND_DELAY)
        if f is None:f={"fund_score":15,"roce":"NA","roe":"NA","de":"NA","sales5":"NA","profit5":"NA","eps5":"NA","qsales":"NA","qprofit":"NA","source":"NA"}
        if f.get("source")!="NA" and f.get("fund_score",0)<MIN_FUND:continue
        setup=multi_tf_setup(s,z["token"],z["ltp"],mbias,secset.get(z["sector"],60),f,daily_cache)
        if setup:
            setup.update({"symbol":z["symbol"],"sector":z["sector"],"live_ltp":z["ltp"],"sector_score":secset.get(z["sector"],60),"fund_data":f,"history":z["history"]})
            results.append(setup)
    results.sort(key=lambda x:(x["score"],x["daily"],x["weekly"],x["monthly"],x["volx"]),reverse=True)
    sig=results[:TOP_SIGNALS]
    msg=[f"🔥 DIVINE SECTOR SWING V5.6 | {datetime.now(IST):%d-%b %H:%M IST}",f"Market Mood: {mbias} ({mval:+d})","Sector Ranking: "+" | ".join(f'{i+1}. {z["sector"]} {z["score"]:.1f}' for i,z in enumerate(sectors)),f"180C/365D DUAL | ETF: ON | IPO 1-YEAR | Analysed: {len(final_candidates)}","🟢 Entry = LIVE Angel LTP",""]
    if sig:
        msg.append("🏆 TOP 3 SWING SETUPS")
        for i,z in enumerate(sig,1):
            f=z["fund_data"] or {};src=f.get("source","NA")
            txt=(f"\n#{i} {z['symbol']} BUY {stars(z['score'])} {z['score']}/100\n"
                 f"Sector: {z['sector']} | Score {z['sector_score']:.1f} | Hist {z['history']}D\n"
                 f"Setup: {z['setup']}\n"
                 f"LTP/Entry {z['entry']:.2f}\n"
                 f"SL {z['sl']:.2f} | T1 {z['t1']:.2f} | T2 {z['t2']:.2f} | T3 {z['t3']:.2f}\n"
                 f"Monthly {z['monthly']}/100 | Weekly {z['weekly']}/100 | Daily {z['daily']}/100\n"
                 f"Fund {z['fund']}/100 ({src}) | Market {z['market_pts']:+d}\n"
                 f"ROCE {f.get('roce','NA')} | ROE {f.get('roe','NA')} | D/E {f.get('de','NA')}\n"
                 f"RSI {z['rsi']:.1f} | VolX {z['volx']:.2f}x")
            msg.append(txt)
    else:
        msg.append("⚠️ NO QUALIFYING SWING SETUP")
        msg.append(f"Dual 180C/365D passed: {len(final_candidates)} | Bearish safety ON")
    out="\n".join(msg);print(out,flush=True);tg(out)

if __name__=="__main__":
    try:main()
    except Exception as e:
        print("ERROR:",e,flush=True);tg(f"❌ SECTOR SWING V5.6 ERROR\n{e}");raise
