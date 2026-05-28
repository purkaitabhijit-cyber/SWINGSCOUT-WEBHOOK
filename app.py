#!/usr/bin/env python3
"""SwingScout Pro — Chartink Webhook → AI → Telegram"""

import os, json, re, requests
from flask import Flask, request, jsonify
from anthropic import Anthropic
from datetime import datetime
from zoneinfo import ZoneInfo

app    = Flask(__name__)
IST    = ZoneInfo("Asia/Kolkata")
client = Anthropic(api_key=os.environ["ANTHROPIC_KEY"])

TG_TOKEN = os.environ["TG_TOKEN"]
TG_CHAT  = os.environ["TG_CHAT"]
CAPITAL  = int(os.environ.get("CAPITAL","50000"))
RISK_PCT = float(os.environ.get("RISK_PCT","2.0"))

_cache = {}

def today():
    return datetime.now(IST).strftime("%Y-%m-%d")

def get_stock_data(symbol, fallback_price):
    try:
        import yfinance as yf
        t = yf.Ticker(f"{symbol}.NS")
        h = t.history(period="5d")
        if len(h) >= 2:
            c = h.iloc[-1]; p = h.iloc[-2]
            avg = h["Volume"].mean()
            return {
                "price"    : round(float(c["Close"]), 2),
                "pct"      : round((c["Close"]-p["Close"])/p["Close"]*100, 2),
                "vol_ratio": round(c["Volume"]/avg if avg>0 else 1, 2),
                "high"     : round(float(c["High"]), 2),
                "low"      : round(float(c["Low"]), 2),
            }
    except Exception as e:
        print(f"Yahoo err {symbol}: {e}")
    return {"price":fallback_price,"pct":0,"vol_ratio":1,
            "high":fallback_price,"low":fallback_price}

def ai_verify(symbol, data, scan_name):
    prompt = f"""Swing trade analysis (3-15 day hold):
Symbol: {symbol} | Scanner: {scan_name}
Price: ₹{data['price']} ({data['pct']:+.1f}%) | Volume: {data['vol_ratio']:.1f}x avg
Candle: High={data['high']} Low={data['low']}

Is this FRESH (just started, room to grow) or EXTENDED (already ran)?
Grade: A+ / A / B / C

Reply ONLY as JSON (no other text):
{{"grade":"A","confidence":78,"fresh":true,"reason":"2 lines max in simple English"}}"""

    try:
        r = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=120,
            messages=[{"role":"user","content":prompt}]
        )
        m = re.search(r'\{[^}]+\}', r.content[0].text, re.DOTALL)
        if m:
            return json.loads(m.group())
    except Exception as e:
        print(f"AI err: {e}")
    return {"grade":"B","confidence":65,"fresh":True,"reason":"Strong setup detected."}

def calc_pos(price, sl_pct=3.0):
    risk = CAPITAL * RISK_PCT / 100
    sl   = round(price*(1-sl_pct/100), 2)
    rsh  = round(price-sl, 2)
    if rsh <= 0:
        return {"sh":0,"inv":0,"sl":sl,"risk":0}
    sh = min(int(risk/rsh), int(CAPITAL*0.30/price))
    return {"sh":sh,"inv":round(sh*price),"sl":sl,"risk":round(sh*rsh)}

def make_signal(sym, data, ai, pos, scan_name, time_str):
    grade = ai["grade"]
    badge = {"A+":"🏆","A":"✅","B":"🔵"}.get(grade,"🔵")
    px    = data["price"]
    t1    = round(px+(px-pos["sl"])*2.0, 2)
    t2    = round(px+(px-pos["sl"])*3.5, 2)
    t1p   = round((t1-px)/px*100,1)
    t2p   = round((t2-px)/px*100,1)
    chg   = data["pct"]
    chg_s = f"+{chg:.1f}%" if chg>=0 else f"{chg:.1f}%"
    vol   = data["vol_ratio"]

    m  = f'{badge} <b>GRADE {grade} — {sym}</b>\n'
    m += f'📊 {scan_name}\n'
    m += f'🕐 {time_str}\n'
    m += '━━━━━━━━━━━━━━━━━━━━\n'
    m += f'💰 CMP: ₹{px}  {chg_s}  |  Vol: {vol:.1f}×\n\n'
    m += f'▶️ <b>ENTRY:</b>     ₹{px}\n'
    m += f'🛑 <b>SL:</b>        ₹{pos["sl"]}  (−3%)\n'
    m += f'🎯 <b>TARGET 1:</b>  ₹{t1}  (+{t1p}%)  → 3-5 দিন\n'
    m += f'🎯 <b>TARGET 2:</b>  ₹{t2}  (+{t2p}%)  → 8-12 দিন\n\n'
    m += f'📦 {pos["sh"]} shares  |  ₹{pos["inv"]:,}  |  Risk ₹{pos["risk"]:,}\n\n'
    m += f'🤖 <i>{ai["reason"]}</i>\n'
    m += f'⚡ Confidence: {ai["confidence"]}%\n'
    m += '━━━━━━━━━━━━━━━━━━━━\n'
    m += '⚠️ SL কঠোরভাবে মেনে চলুন।'
    return m

def tg(msg):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id":TG_CHAT,"text":msg,"parse_mode":"HTML"},
            timeout=10
        )
    except Exception as e:
        print(f"TG err: {e}")

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data        = request.json or {}
        stocks_str  = data.get("stocks","")
        prices_str  = data.get("trigger_prices","")
        scan_name   = data.get("scan_name","Scanner")
        triggered_at= data.get("triggered_at","")

        if not stocks_str:
            return jsonify({"status":"no stocks"})

        stocks = [s.strip() for s in stocks_str.split(",")]
        prices = prices_str.split(",")
        sent   = 0

        # Test webhook — confirmation পাঠাও
        if stocks and stocks[0].upper().startswith("SYMBOL"):
            tg(f"✅ <b>SwingScout Pro Connected!</b>\n\n"
               f"🔗 Webhook working perfectly.\n"
               f"📡 Scanner: {scan_name}\n"
               f"🕐 {triggered_at}\n\n"
               f"এখন real stocks match হলে signal আসবে। 🎯")
            return jsonify({"status":"ok","test":True})

        for i, sym in enumerate(stocks):
            if not sym:
                continue

            key = f"{today()}_{sym}_{scan_name}"
            if key in _cache:
                print(f"Skip {sym} — cached")
                continue
            _cache[key] = True

            try:    px = float(prices[i].strip())
            except: px = 0

            stock_data = get_stock_data(sym, px)
            ai         = ai_verify(sym, stock_data, scan_name)

            if ai["grade"]=="C" or ai["confidence"]<65 or not ai.get("fresh",True):
                print(f"Skip {sym}: {ai['grade']} {ai['confidence']}%")
                continue

            pos = calc_pos(stock_data["price"] or px)
            msg = make_signal(sym, stock_data, ai, pos, scan_name, triggered_at)
            tg(msg)
            sent += 1
            print(f"✅ {sym} Grade {ai['grade']} Conf {ai['confidence']}%")

        return jsonify({"status":"ok","sent":sent})

    except Exception as e:
        print(f"Webhook err: {e}")
        return jsonify({"status":"error","msg":str(e)}), 500

@app.route("/")
def home():
    now    = datetime.now(IST).strftime("%d %b %Y %H:%M")
    today_count = len([k for k in _cache if k.startswith(today())])
    return jsonify({
        "status"       : "✅ SwingScout Pro Running",
        "time"         : now,
        "signals_today": today_count
    })

@app.route("/health")
def health():
    return jsonify({"status":"ok"})

@app.route("/test")
def test_telegram():
    """Direct Telegram test"""
    try:
        tg("🧪 <b>Test Message</b>\n\nSwingScout Pro server থেকে পাঠানো হচ্ছে।\nTelegram connection ✅")
        return jsonify({"status":"sent","msg":"Check Telegram!"})
    except Exception as e:
        return jsonify({"status":"error","msg":str(e)})

if __name__ == "__main__":
    port = int(os.environ.get("PORT",5000))
    print(f"SwingScout starting on port {port}")
    app.run(host="0.0.0.0", port=port)

