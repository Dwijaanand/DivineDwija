import os, pyotp, time
import pandas as pd
import requests
from SmartApi import SmartConnect

# --- TERE SECRET NAAM ---
API_KEY = os.getenv("API_KEY")
CLIENT_ID = os.getenv("CLIENT_ID")
PASSWORD = os.getenv("PASSWORD")
TOTP_SECRET = os.getenv("TOTP_SECRET")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

def login():
    print("=== GITHUB SCANNER ===")
    for i in range(3):
        try:
            totp = pyotp.TOTP(TOTP_SECRET).now()
            obj = SmartConnect(api_key=API_KEY)
            data = obj.generateSession(CLIENT_ID, PASSWORD, totp)
            if data and data.get('status'):
                print("Login Success")
                return obj
            else:
                print(f"Login response: {data}")
        except Exception as e:
            print(f"[{time.strftime('%H:%M:%S')}] Login object of type 'NoneType' has no len() - Retry {i+1}")
            time.sleep(2)
    raise Exception("Login Fail")

def send_telegram(msg):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, data={"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"}, timeout=10)
    except Exception as e:
        print(f"Telegram Error: {e}")

def get_52w():
    # yaha tera 52W high low wala logic
    return 100, 50 # dummy

def load_master():
    return {}

def main():
    high, low = get_52w()
    master = load_master()
    obj = login()
    
    # --- Yaha tera scan logic aayega ---
    # Example message
    message = f"🚀 *52 Week Scanner*\nHigh: {high}\nLow: {low}\nTime: {time.strftime('%d-%m-%Y %H:%M')}"
    send_telegram(message)
    print("Done")

if __name__=="__main__":
    main()
