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
TOP_GAINERS = 10        # 24 மணி நேரத்தில் அதிகம் ஏறிய coin-கள் எத்தனை
TOP_LOSERS = 10         # 24 மணி நேரத்தில் அதிகம் இறங்கிய coin-கள் எத்தனை
MIN_QUOTE_VOL = 5_000_000   # குறைந்தது 5 மில்லியன் USDT volume உள்ள coin-கள் மட்டும் (குப்பை coin-களைத் தவிர்க்க)
MAX_PER_RUN = 3
DAILY_CAP = 15
COOLDOWN_H = 6
RSI_LONG = (52, 70)      # LONG-க்கு 1h RSI இந்த வரம்பில் இருக்கணும்
RSI_SHORT = (30, 48)     # SHORT-க்கு 1h RSI இந்த வரம்பில் இருக்கணும்
MIN_STRENGTH = 0.5       # 1h trend வலிமை (பெரிய எண் = கடுமையான filter)
MIN_VOL_RATIO = 1.2     # 15m volume, 20-candle average-ஐ விட எத்தனை மடங்கு இருக்கணும்
SHOW_SKIPPED = True      # AI "SKIP" சொன்ன setup-களையும் Telegram-ல் காட்டு (True / False)
AI_MODEL = "claude-sonnet-5-5"     # ஆழமான analysis-க்கு பெரிய model
AI_FALLBACK = "claude-haiku-4-5"   # பெரிய model வேலை செய்யலைனா இது
MIN_SCORE = 7            # AI மதிப்பெண் 10-க்கு இவ்வளவு அல்லது மேல் இருந்தால் மட்டும் 📊 signal
MAX_AI_CALLS = 6         # ஒரு ஓட்டத்தில் AI-யிடம் அதிகபட்சம் எத்தனை setup கேட்கலாம் (செலவைக் கட்டுப்படுத்த)
LOG = "signals_log.csv"
FIELDS = ["time", "symbol", "side", "entry", "sl", "tp", "status"]
FMT = "%Y-%m-%d %H:%M"
STABLE = {"USDC", "FDUSD", "TUSD", "USDP", "EUR", "DAI", "AEUR", "USD1", "XUSD", "BFUSD", "USDE", "RLUSD"}


def tg(text):
    try:
        r = requests.post(
            "https://api.telegram.org/bot" + TG_TOKEN + "/sendMessage",
            data={"chat_id": CHAT_ID, "text": text},
            timeout=20,
        )
        if r.status_code != 200:
            print("telegram error", r.status_code, r.text[:200])
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


TICK = {}   # ஒவ்வொரு coin-இன் 24 மணி நேர தகவல்


def movers():
    # 24 மணி நேரத்தில் அதிகம் ஏறிய top 10, அதிகம் இறங்கிய top 10
    r = requests.get(BASE + "/ticker/24hr", timeout=30)
    r.raise_for_status()
    rows = []
    for t in r.json():
        s = t["symbol"]
        if not s.endswith("USDT"):
            continue
        TICK[s] = {"chg": float(t["priceChangePercent"]), "qv": float(t["quoteVolume"]),
                   "hi": float(t["highPrice"]), "lo": float(t["lowPrice"])}
        base = s[:-4]
        if base in STABLE:
            continue
        if base.endswith(("UP", "DOWN", "BULL", "BEAR")):
            continue
        if float(t["quoteVolume"]) < MIN_QUOTE_VOL:
            continue
        rows.append((float(t["priceChangePercent"]), s))
    rows.sort(reverse=True)
    gainers = [s for _, s in rows[:TOP_GAINERS]]
    losers = [s for _, s in rows[::-1][:TOP_LOSERS]]
    return gainers, losers


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


def analyze(symbol, now, stats):
    # படி 1: 1 hour கேண்டில் - trend (மேலே / கீழே)
    h1 = add_ind(klines(symbol, "1h"))
    t1 = trend(h1)
    if t1 == "FLAT":
        return None
    stats["trend"] += 1

    # படி 2: 15 minutes கேண்டில் - entry
    m = add_ind(klines(symbol, "15m"))
    a = m.iloc[-2]
    b = m.iloc[-1]
    if now.timestamp() * 1000 - b["ct"] > 30 * 60 * 1000:
        return None
    vol_ratio = b["v"] / b["volavg"] if b["volavg"] > 0 else 0
    x1 = h1.iloc[-1]
    rsi1 = x1["rsi"]
    atr = x1["atr"]
    side = None
    # RSI தெளிவாக trend பக்கம் இருக்கணும் (நடுவில் 48-52 இருந்தால் trend பலவீனம்)
    if t1 == "UP" and a["c"] <= a["ema20"] and b["c"] > b["ema20"] and RSI_LONG[0] <= rsi1 <= RSI_LONG[1]:
        side = "LONG"
    if t1 == "DOWN" and a["c"] >= a["ema20"] and b["c"] < b["ema20"] and RSI_SHORT[0] <= rsi1 <= RSI_SHORT[1]:
        side = "SHORT"
    if side is None:
        return None
    # Trend வலிமை: 1h EMA20, EMA50 இடையே போதுமான இடைவெளி இருக்கணும்
    strength = abs(x1["ema20"] - x1["ema50"]) / atr if atr > 0 else 0
    if strength < MIN_STRENGTH:
        return None
    stats["cross"] += 1
    if vol_ratio < MIN_VOL_RATIO:
        return None
    stats["setup"] += 1

    entry = b["c"]
    stretch = abs(entry - x1["ema20"]) / atr if atr > 0 else 0   # entry, 1h EMA20-லிருந்து எவ்வளவு தூரம்
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
        "strength": strength, "stretch": stretch, "rsi15": b["rsi"],
        "_h1": h1, "_m": m,
    }


def candles_table(df, n, label):
    # கடைசி n கேண்டில்களை AI படிக்கும் சிறிய அட்டவணையாக மாற்று
    out = [label + " (oldest first): time_utc | open | high | low | close | vol_x_avg"]
    for _, k in df.tail(n).iterrows():
        t = datetime.fromtimestamp((k["ct"] + 1) / 1000, timezone.utc).strftime("%d %H:%M")
        va = k["v"] / k["volavg"] if k["volavg"] and k["volavg"] > 0 else 0
        out.append(t + " | " + fmt(k["o"]) + " | " + fmt(k["h"]) + " | " + fmt(k["l"]) + " | "
                   + fmt(k["c"]) + " | " + f"{va:.1f}")
    return "\n".join(out)


def tf_summary(df, name):
    x = df.iloc[-1]
    price = x["c"]
    return (name + ": trend " + trend(df) + ", RSI " + str(round(x["rsi"])) +
            ", price vs EMA20 " + pct(price, x["ema20"]) + ", price vs EMA50 " + pct(price, x["ema50"]) +
            ", ATR " + f"{x['atr'] / price * 100:.2f}% of price")


BTC_BRIEF = None


def btc_brief():
    # மொத்த மார்க்கெட் திசை (BTC) - ஒரு ஓட்டத்துக்கு ஒருமுறை மட்டும்
    global BTC_BRIEF
    if BTC_BRIEF is None:
        try:
            b1 = add_ind(klines("BTCUSDT", "1h"))
            b4 = add_ind(klines("BTCUSDT", "4h"))
            chg = TICK.get("BTCUSDT", {}).get("chg", 0)
            BTC_BRIEF = ("BTC (overall market): 24h change " + f"{chg:+.2f}%" + "; "
                         + tf_summary(b4, "4h") + "; " + tf_summary(b1, "1h"))
        except Exception as e:
            print("btc error", type(e).__name__)
            BTC_BRIEF = "BTC data unavailable"
    return BTC_BRIEF


def build_brief(c):
    # AI-க்கு ஒரு specialist பார்க்கும் அளவுக்கு முழு தகவல்
    h1, m = c["_h1"], c["_m"]
    entry = c["entry"]
    parts = []
    try:
        h4 = add_ind(klines(c["symbol"], "4h"))
        parts.append(tf_summary(h4, "4h"))
    except Exception:
        h4 = None
        parts.append("4h: unavailable")
    parts.append(tf_summary(h1, "1h"))
    parts.append(tf_summary(m, "15m"))
    last50 = h1.tail(50)
    last20 = h1.tail(20)
    parts.append("1h swing levels: 50-candle high " + fmt(last50["h"].max()) + " (" + pct(last50["h"].max(), entry) +
                 " from entry), 50-candle low " + fmt(last50["l"].min()) + " (" + pct(last50["l"].min(), entry) +
                 "), 20-candle high " + fmt(last20["h"].max()) + " (" + pct(last20["h"].max(), entry) +
                 "), 20-candle low " + fmt(last20["l"].min()) + " (" + pct(last20["l"].min(), entry) + ")")
    t = TICK.get(c["symbol"])
    if t:
        parts.append("24h: change " + f"{t['chg']:+.2f}%" + ", high " + fmt(t["hi"]) + ", low " + fmt(t["lo"]) +
                     ", volume " + f"{t['qv'] / 1e6:.1f}" + "M USDT")
    if h4 is not None:
        parts.append(candles_table(h4, 12, "4h candles"))
    parts.append(candles_table(h1, 24, "1h candles"))
    parts.append(candles_table(m, 24, "15m candles"))
    return "\n".join(parts)


AI_SYSTEM = (
    "You are a senior crypto futures trader and risk manager with years of experience in trend-following "
    "pullback trades. You review setups found by an automatic scanner and decide like a disciplined specialist: "
    "balanced, neither over-cautious nor greedy. Your job is to pass only high-quality trades.\n"
    "Check carefully, step by step, before deciding:\n"
    "1. Higher-timeframe alignment: do 4h and 1h agree with the trade side? Counter-4h trades need strong reasons.\n"
    "2. Overall market: does BTC direction support or fight this trade?\n"
    "3. Trend quality: clean higher highs/lows (or lower for shorts) in the 1h candles, or choppy?\n"
    "4. Entry quality: has the 15m pullback really ended? Is entry close to value (EMA20) or chasing an extended move?\n"
    "5. Room to target: is there a nearby swing high/low or 24h high/low that blocks the target before it is reached?\n"
    "6. Stop placement: is the stop beyond a logical level, or likely to be hit by normal noise?\n"
    "7. Momentum and volume: does RSI and volume confirm, or show exhaustion/divergence?\n"
    "8. Risks: news-like spikes, huge 24h moves (late entry), very low liquidity.\n"
    "Score the trade 1-10 (10 = textbook, 7 = good and tradable, 5 = average, 3 or less = avoid). "
    "Say TAKE only if score is 7 or more.\n"
    "Answer in exactly this format, nothing else:\n"
    "VERDICT: TAKE or SKIP\n"
    "SCORE: number 1-10\n"
    "REASON: 2 short sentences in very simple Tamil script (no English sentences; words like RSI, BTC, trend are OK), "
    "mention the strongest point and the biggest risk, max 40 words."
)


def ask_claude(c):
    prompt = (
        "Setup found by scanner:\n"
        "Coin " + c["symbol"] + ", side " + c["side"] + ".\n"
        "Scanner logic: 1h trend " + c["trend"] + " (price and EMA20 on the same side of EMA50), "
        "1h RSI on the trend side, 15m candle just closed back across its EMA20 in the trend direction "
        "(pullback ending), 15m volume " + str(round(c["vol_ratio"], 1)) + "x its 20-candle average.\n"
        "1h trend strength (EMA20-EMA50 gap in ATR): " + str(round(c["strength"], 2)) + ". "
        "Entry distance from 1h EMA20 in ATR: " + str(round(c["stretch"], 2)) + " (above 2 = chasing).\n"
        "Entry " + fmt(c["entry"]) + ", stop " + fmt(c["sl"]) + " (" + pct(c["sl"], c["entry"]) + "), "
        "target " + fmt(c["tp"]) + " (" + pct(c["tp"], c["entry"]) + "), risk:reward 1:2. "
        "Stop = 1.5 x 1h ATR, target = 3 x 1h ATR.\n\n"
        + btc_brief() + "\n\n" + build_brief(c)
    )
    headers = {
        "x-api-key": CLAUDE_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    for model in (AI_MODEL, AI_FALLBACK):
        body = {
            "model": model,
            "max_tokens": 400,
            "system": AI_SYSTEM,
            "messages": [{"role": "user", "content": prompt}],
        }
        try:
            r = requests.post("https://api.anthropic.com/v1/messages", headers=headers, json=body, timeout=90)
            j = r.json()
        except Exception as e:
            print("claude error", model, type(e).__name__)
            continue
        if "content" not in j:
            print("claude error", model, r.status_code, str(j)[:300])
            continue
        return parse_ai(j["content"][0]["text"])
    return None, 0, ""


def parse_ai(text):
    verdict, score, reason = "SKIP", 0, ""
    for line in text.strip().splitlines():
        u = line.strip()
        up = u.upper()
        if up.startswith("VERDICT"):
            verdict = "TAKE" if "TAKE" in up else "SKIP"
        elif up.startswith("SCORE"):
            digits = "".join(ch if ch.isdigit() else " " for ch in u).split()
            if digits:
                score = min(10, int(digits[0]))
        elif up.startswith("REASON"):
            reason = u.split(":", 1)[1].strip() if ":" in u else u
        elif reason:
            reason += " " + u
    return verdict, score, reason


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


def pct(price, entry):
    # Entry-யிலிருந்து எத்தனை சதவீதம் தூரம் (+ மேலே, - கீழே)
    p = (price - entry) / entry * 100
    return f"{p:+.2f}%"


def signal_text(c, header, reason, footer):
    return (
        header + "\n"
        + (c.get("cat", "") + "\n" if c.get("cat") else "") +
        "Entry: " + fmt(c["entry"]) + "\n"
        "Stop-loss: " + fmt(c["sl"]) + " (" + pct(c["sl"], c["entry"]) + ")\n"
        "Target: " + fmt(c["tp"]) + " (" + pct(c["tp"], c["entry"]) + ")\n"
        "Leverage: 3x-க்கு மேல் வேண்டாம், Isolated margin\n\n"
        + (reason + "\n\n" if reason else "") + footer
    )


def main():
    now = datetime.now(timezone.utc)
    first_run = not os.path.exists(LOG)
    rows = read_log()
    if first_run:
        tg("✅ Scanner தொடங்கியது (1h trend + 15m entry). Setup உருவானால் மட்டும் மெசேஜ் வரும்.")
    check_open(rows, now)

    favs = favorites()
    gainers, losers = movers()
    cat = {}
    for s in losers:
        cat[s] = "📉 இன்று அதிகம் இறங்கியது"
    for s in gainers:
        cat[s] = "🚀 இன்று அதிகம் ஏறியது"
    for s in favs:
        cat[s] = "⭐ உங்கள் பட்டியல்"
    syms = list(dict.fromkeys(favs + gainers + losers))
    print("favorites:", len(favs), "| gainers:", gainers, "| losers:", losers)
    stats = {"trend": 0, "cross": 0, "setup": 0}
    errors = 0
    cands = []
    for s in syms:
        try:
            res = analyze(s, now, stats)
        except Exception as e:
            print(s, "error", type(e).__name__)
            errors += 1
            continue
        time.sleep(0.05)
        if res is None:
            continue
        if any(x["symbol"] == s and x["time"] == res["time"] for x in rows):
            continue
        res["cat"] = cat.get(s, "")
        cands.append(res)

    cands.sort(key=lambda x: x["vol_ratio"], reverse=True)
    sent = 0
    skipped = 0
    claude_fail = 0
    ai_calls = 0
    for c in cands:
        if sent >= MAX_PER_RUN or ai_calls >= MAX_AI_CALLS:
            break
        day = [x for x in rows if x["status"] != "SKIP" and now - tparse(x["time"]) < timedelta(hours=24)]
        if len(day) >= DAILY_CAP:
            break
        recent = [x for x in rows if x["symbol"] == c["symbol"] and x["status"] != "SKIP"
                  and now - tparse(x["time"]) < timedelta(hours=COOLDOWN_H)]
        if recent:
            continue
        ai_calls += 1
        verdict, score, reason = ask_claude(c)
        row = {"time": c["time"], "symbol": c["symbol"], "side": c["side"],
               "entry": str(c["entry"]), "sl": str(c["sl"]), "tp": str(c["tp"]), "status": "SKIP"}
        score_line = "🧠 AI மதிப்பெண்: " + str(score) + "/10\n"
        if verdict is None:
            # AI வேலை செய்யலைனாலும் signal-ஐ விடாமல் அனுப்பு
            claude_fail += 1
            row["status"] = "OPEN"
            sent += 1
            tg(signal_text(c, "📊 " + c["symbol"] + " — " + c["side"],
                           "(AI review கிடைக்கவில்லை)", "⚠️ உத்தரவாதம் இல்லை. Stop-loss கட்டாயம்."))
        elif verdict == "TAKE" and score >= MIN_SCORE:
            row["status"] = "OPEN"
            sent += 1
            tg(signal_text(c, "📊 " + c["symbol"] + " — " + c["side"],
                           score_line + reason, "⚠️ உத்தரவாதம் இல்லை. Stop-loss கட்டாயம்."))
        else:
            skipped += 1
            if SHOW_SKIPPED:
                tg(signal_text(c, "👀 " + c["symbol"] + " — " + c["side"] + " (AI: SKIP)",
                               score_line + reason, "ℹ️ தகவலுக்கு மட்டும். Trade எடுக்க வேண்டாம்."))
        rows.append(row)

    if claude_fail:
        tg("⚠️ Claude API வேலை செய்யவில்லை (" + str(claude_fail) + " முறை). "
           "CLAUDE_KEY சரியா, Anthropic account-ல் credit இருக்கா என்று பாருங்கள்.")

    print("coins:", len(syms), "| errors:", errors)
    print("1h trend உள்ளவை:", stats["trend"], "| 15m cross:", stats["cross"],
          "| volume-உம் சரி (setup):", stats["setup"])
    print("புதிய candidates:", len(cands), "| அனுப்பியது:", sent,
          "| AI SKIP:", skipped, "| AI error:", claude_fail)

    rows = [x for x in rows if not (x["status"] == "SKIP" and now - tparse(x["time"]) > timedelta(days=2))]
    write_log(rows)


main()
