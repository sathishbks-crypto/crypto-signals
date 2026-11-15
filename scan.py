import os
import csv
import time
import requests
import pandas as pd
from datetime import datetime, timezone, timedelta

TG_TOKEN = os.environ["TG_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]
CLAUDE_KEY = os.environ["CLAUDE_KEY"]

BASE = "https://data-api.binance.vision/api/v3"
TOP_N = 100
MAX_PER_RUN = 3
DAILY_CAP = 15
COOLDOWN_H = 6
LOG = "signals_log.csv"
FIELDS = ["time", "symbol", "side", "entry", "sl", "tp", "status"]
FMT = "%Y-%m-%d %H:%M"
STABLE = {"USDC", "FDUSD", "TUSD", "USDP", "EUR", "DAI", "AEUR", "USD1", "XUSD", "BFUSD", "USDE", "RLUSD"}


def tg(text):
    try:
        requests.post(
            "https://api.telegram.org/bot" + TG_TOKEN + "/sendMessage",
            data={"chat_id": CHAT_ID, "text": text},
            timeout=20,
        )
    except Exception as e:
        print("telegram error", type(e).__name__)


def fmt(x):
    if x >= 100:
        return f"{x:.2f}"
    if x >= 1:
        return f"{x:.4f}"
    return f"{x:.8f}"


def tparse(s):
    return datetime.strptime(s, FMT).replace(tzinfo=timezone.utc)


def read_log():
    if not os.path.exists(LOG):
        return []
    with open(LOG, newline="") as f:
        return list(csv.DictReader(f))


def write_log(rows):
    with open(LOG, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)


def favorites():
    out = []
    try:
        with open("favorites.txt") as f:
            for line in f:
                x = line.strip().upper()
                if not x or x.startswith("#"):
                    continue
                if not x.endswith("USDT"):
                    x = x + "USDT"
                out.append(x)
    except FileNotFoundError:
        pass
    return out


def top_symbols():
    r = requests.get(BASE + "/ticker/24hr", timeout=30)
    r.raise_for_status()
    rows = []
    for t in r.json():
        s = t["symbol"]
        if not s.endswith("USDT"):
            continue
        if s[:-4] in STABLE:
            continue
        rows.append((float(t["quoteVolume"]), s))
    rows.sort(reverse=True)
    return [s for _, s in rows[:TOP_N]]


def klines(symbol, interval, limit=120):
    r = requests.get(
        BASE + "/klines",
        params={"symbol": symbol, "interval": interval, "limit": limit},
        timeout=20,
    )
    r.raise_for_status()
    cols = ["t", "o", "h", "l", "c", "v", "ct", "qv", "n", "tb", "tq", "i"]
    df = pd.DataFrame(r.json(), columns=cols)
    for col in ["o", "h", "l", "c", "v"]:
        df[col] = df[col].astype(float)
    return df.iloc[:-1].reset_index(drop=True)


def add_ind(df):
    c = df["c"]
    df["ema20"] = c.ewm(span=20, adjust=False).mean()
    df["ema50"] = c.ewm(span=50, adjust=False).mean()
    d = c.diff()
    gain = d.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    loss = (-d.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    df["rsi"] = 100 - 100 / (1 + gain / loss)
    p1 = df["h"] - df["l"]
    p2 = (df["h"] - c.shift()).abs()
    p3 = (df["l"] - c.shift()).abs()
    tr = pd.concat([p1, p2, p3], axis=1).max(axis=1)
    df["atr"] = tr.ewm(alpha=1 / 14, adjust=False).mean()
    df["volavg"] = df["v"].rolling(20).mean()
    return df


def trend(df):
    x = df.iloc[-1]
    if x["c"] > x["ema50"] and x["ema20"] > x["ema50"]:
        return "UP"
    if x["c"] < x["ema50"] and x["ema20"] < x["ema50"]:
        return "DOWN"
    return "FLAT"


def analyze(symbol, now):
    h1 = add_ind(klines(symbol, "1h"))
    t1 = trend(h1)
    if t1 == "FLAT":
        return None
    h4 = add_ind(klines(symbol, "4h"))
    if trend(h4) != t1:
        return None
    m = add_ind(klines(symbol, "15m"))
    a = m.iloc[-2]
    b = m.iloc[-1]
    if now.timestamp() * 1000 - b["ct"] > 30 * 60 * 1000:
        return None
    vol_ratio = b["v"] / b["volavg"] if b["volavg"] > 0 else 0
    rsi1 = h1.iloc[-1]["rsi"]
    side = None
    if t1 == "UP" and a["c"] <= a["ema20"] and b["c"] > b["ema20"] and 40 <= rsi1 <= 68:
        side = "LONG"
    if t1 == "DOWN" and a["c"] >= a["ema20"] and b["c"] < b["ema20"] and 32 <= rsi1 <= 60:
        side = "SHORT"
    if side is None or vol_ratio < 1.2:
        return None
    atr = h1.iloc[-1]["atr"]
    entry = b["c"]
    if side == "LONG":
        sl = entry - 1.5 * atr
        tp = entry + 3 * atr
    else:
        sl = entry + 1.5 * atr
        tp = entry - 3 * atr
    ctime = datetime.fromtimestamp((b["ct"] + 1) / 1000, timezone.utc).strftime(FMT)
    return {
        "symbol": symbol, "side": side, "entry": entry, "sl": sl, "tp": tp,
        "time": ctime, "trend": t1, "rsi1": rsi1, "vol_ratio": vol_ratio,
    }


def ask_claude(c):
    prompt = (
        "You are a cautious crypto futures analyst reviewing an auto-detected setup. "
        "Coin " + c["symbol"] + ", side " + c["side"] + ". "
        "4h and 1h trend: " + c["trend"] + ". 1h RSI " + str(round(c["rsi1"])) + ". "
        "15m volume is " + str(round(c["vol_ratio"], 1)) + "x its 20-candle average. "
        "Entry " + fmt(c["entry"]) + ", stop " + fmt(c["sl"]) + ", target " + fmt(c["tp"]) + " (risk:reward 1:2). "
        "Reply in exactly two lines. Line 1: TAKE or SKIP. "
        "Line 2: reason in very simple Tamil, max 20 words. "
        "Say SKIP if the setup looks weak, chasing, or risky."
    )
    headers = {
        "x-api-key": CLAUDE_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    body = {
        "model": "claude-haiku-4-5",
        "max_tokens": 300,
        "messages": [{"role": "user", "content": prompt}],
    }
    try:
        r = requests.post("https://api.anthropic.com/v1/messages", headers=headers, json=body, timeout=60)
        j = r.json()
        if "content" not in j:
            print("claude error", r.status_code)
            return None, ""
        lines = j["content"][0]["text"].strip().splitlines()
        verdict = "TAKE" if "TAKE" in lines[0].upper() else "SKIP"
        reason = lines[1].strip() if len(lines) > 1 else ""
        return verdict, reason
    except Exception as e:
        print("claude error", type(e).__name__)
        return None, ""


def check_open(rows, now):
    for r in rows:
        if r["status"] != "OPEN":
            continue
        t0 = tparse(r["time"])
        try:
            resp = requests.get(
                BASE + "/klines",
                params={"symbol": r["symbol"], "interval": "15m",
                        "startTime": int(t0.timestamp() * 1000), "limit": 200},
                timeout=20,
            )
            data = resp.json()
        except Exception as e:
            print("check error", type(e).__name__)
            continue
        if not isinstance(data, list):
            continue
        sl = float(r["sl"])
        tp = float(r["tp"])
        res = None
        for k in data:
            hi = float(k[2])
            lo = float(k[3])
            if r["side"] == "LONG":
                if lo <= sl:
                    res = "LOSS"
                elif hi >= tp:
                    res = "WIN"
            else:
                if hi >= sl:
                    res = "LOSS"
                elif lo <= tp:
                    res = "WIN"
            if res:
                break
        if res is None and now - t0 > timedelta(hours=48):
            res = "EXPIRED"
        if res:
            r["status"] = res
            wins = sum(1 for x in rows if x["status"] == "WIN")
            losses = sum(1 for x in rows if x["status"] == "LOSS")
            icon = {"WIN": "✅ Target தொட்டது", "LOSS": "❌ Stop-loss தொட்டது", "EXPIRED": "⏳ 48 மணி நேரத்தில் முடிவில்லை"}[res]
            tg(icon + "\n" + r["symbol"] + " " + r["side"] + "\nமொத்தம்: ✅ " + str(wins) + " | ❌ " + str(losses))


def main():
    now = datetime.now(timezone.utc)
    first_run = not os.path.exists(LOG)
    rows = read_log()
    if first_run:
        tg("✅ Scanner தொடங்கியது. Setup உருவானால் மட்டும் மெசேஜ் வரும்.")
    check_open(rows, now)

    syms = list(dict.fromkeys(favorites() + top_symbols()))
    print("coins:", len(syms))
    cands = []
    for s in syms:
        try:
            res = analyze(s, now)
        except Exception as e:
            print(s, "error", type(e).__name__)
            continue
        time.sleep(0.05)
        if res is None:
            continue
        if any(x["symbol"] == s and x["time"] == res["time"] for x in rows):
            continue
        cands.append(res)

    cands.sort(key=lambda x: x["vol_ratio"], reverse=True)
    sent = 0
    for c in cands:
        if sent >= MAX_PER_RUN:
            break
        day = [x for x in rows if x["status"] != "SKIP" and now - tparse(x["time"]) < timedelta(hours=24)]
        if len(day) >= DAILY_CAP:
            break
        recent = [x for x in rows if x["symbol"] == c["symbol"] and x["status"] != "SKIP"
                  and now - tparse(x["time"]) < timedelta(hours=COOLDOWN_H)]
        if recent:
            continue
        verdict, reason = ask_claude(c)
        if verdict is None:
            continue
        row = {"time": c["time"], "symbol": c["symbol"], "side": c["side"],
               "entry": str(c["entry"]), "sl": str(c["sl"]), "tp": str(c["tp"]), "status": "SKIP"}
        if verdict == "TAKE":
            row["status"] = "OPEN"
            sent += 1
            tg(
                "📊 " + c["symbol"] + " — " + c["side"] + "\n"
                "Entry: " + fmt(c["entry"]) + "\n"
                "Stop-loss: " + fmt(c["sl"]) + "\n"
                "Target: " + fmt(c["tp"]) + "\n"
                "Leverage: 3x-க்கு மேல் வேண்டாம், Isolated margin\n\n"
                + reason + "\n\n⚠️ உத்தரவாதம் இல்லை. Stop-loss கட்டாயம்."
            )
        rows.append(row)

    rows = [x for x in rows if not (x["status"] == "SKIP" and now - tparse(x["time"]) > timedelta(days=2))]
    write_log(rows)


main()
