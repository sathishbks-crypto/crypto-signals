#!/usr/bin/env python3
"""
TEST ONLY - places no trade.

One account, Rs 1,00,000, last 1 year, compounding.

Every trading day at about 10 AM, take the No.1 top gainer (biggest % rise
from yesterday's close) among the stocks in stocks_list.txt. Then decide the
bias the way a technical trader would, from what is known at that moment:
  - how strong the first hour was (where it closed in its range, red/green)
  - volume versus its usual first hour (relative volume)
  - the daily trend (price vs 20-day average), how far it already ran
  - hourly RSI (overbought?) and what the whole market (Nifty) is doing
-> BUY (momentum continues), SELL (the spike fades) or NO TRADE (unclear).

Trade: whole balance, no leverage, target 2%, stop-loss 1% (1:2).
Whatever is still open at about 2 PM is closed. If target and stop-loss fall
inside the same 1-hour candle it counts as a LOSS. Costs 0.10% per trade.

The rules are written once, up front, and NOT tuned on the results.

Run on the server (bot folder):   python3 intraday_test.py
The report is sent to Telegram (takes about 5-8 minutes).
"""

import gc
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
IST = timezone(timedelta(hours=5, minutes=30))

TP = 2.0            # % target
SL = 1.0            # % stop-loss  (1:2)
COST = 0.10         # % per trade
START = 100000.0
ENTRY_BAR = "09:15"     # 1-hour candle 9:15-10:15 -> decision at about 10 AM, entry by 10:15
LAST_BAR = "13:15"      # candle 13:15-14:15 -> trade closed at about 2 PM
TEST_DAYS = 365
FETCH_DAYS = 430        # a little more than a year, for the 20-day average (keeps memory low)
LIST_FILE = "stocks_list.txt"

SECRETS = {}
if os.path.exists("secrets.txt"):
    with open("secrets.txt", encoding="utf-8") as f:
        for line in f:
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                SECRETS[k.strip().upper()] = v.strip()


def tg(text):
    print(text.replace("<b>", "").replace("</b>", ""))
    token, chat = SECRETS.get("TELEGRAM_BOT_TOKEN", ""), SECRETS.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat:
        return
    for start in range(0, len(text), 4000):
        body = urllib.parse.urlencode({"chat_id": chat, "text": text[start:start + 4000],
                                       "parse_mode": "HTML"}).encode()
        try:
            urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", body, timeout=20)
        except Exception as e:
            print("[telegram]", e)


def http_json(url, tries=3):
    last = None
    for a in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            last = e
            time.sleep(3 * (a + 1) if e.code == 429 else 1.5)
        except Exception as e:
            last = e
            time.sleep(1.5 * (a + 1))
    raise last


def hourly(sym):
    """1-hour candles (about 14 months, so indicators are ready), grouped by day."""
    ys = sym if sym.startswith("^") else sym + ".NS"
    q = urllib.parse.quote(ys)
    err = None
    for host in ("query1", "query2"):
        try:
            j = http_json(f"https://{host}.finance.yahoo.com/v8/finance/chart/{q}"
                          f"?period1={int(time.time()) - FETCH_DAYS * 86400}&period2={int(time.time())}"
                          f"&interval=60m&includePrePost=false")
            res = j["chart"]["result"][0]
            break
        except Exception as e:
            err = e
    else:
        raise err
    ts = res.get("timestamp") or []
    qd = res["indicators"]["quote"][0]
    days = {}
    for k, t in enumerate(ts):
        o, h, l, c, v = (qd.get(x, [None] * len(ts))[k] for x in ("open", "high", "low", "close", "volume"))
        if None in (o, h, l, c) or c <= 0:
            continue
        d = datetime.fromtimestamp(t, IST)
        days.setdefault(sys.intern(d.strftime("%Y-%m-%d")), []).append(
            (sys.intern(d.strftime("%H:%M")), round(o, 2), round(h, 2), round(l, 2), round(c, 2), int(v or 0)))
    for d in days:
        days[d].sort()
    return days


def load_list():
    out = []
    with open(LIST_FILE, encoding="utf-8") as f:
        for line in f:
            s = line.split("#")[0].strip().upper()
            if s:
                out.append(s)
    return out


def ema_last(vals, n):
    if len(vals) < n:
        return None
    k = 2 / (n + 1)
    e = sum(vals[:n]) / n
    for v in vals[n:]:
        e = v * k + e * (1 - k)
    return e


def rsi_last(c, n=14):
    if len(c) <= n:
        return None
    g = l = 0.0
    for i in range(1, n + 1):
        d = c[i] - c[i - 1]
        g += max(d, 0)
        l += max(-d, 0)
    ag, al = g / n, l / n
    for i in range(n + 1, len(c)):
        d = c[i] - c[i - 1]
        ag = (ag * (n - 1) + max(d, 0)) / n
        al = (al * (n - 1) + max(-d, 0)) / n
    return 100.0 if al == 0 else 100 - 100 / (1 + ag / al)


def features(dd, days_sorted, di, nifty):
    """Everything a trader can see at about 10:15 AM on day days_sorted[di]."""
    day, prev = days_sorted[di], days_sorted[di - 1]
    bars = dd[day]
    if not bars or bars[0][0] != ENTRY_BAR:
        return None
    _, o, h, l, c, v = bars[0]
    pc = dd[prev][-1][4]
    hist = days_sorted[max(0, di - 60):di]
    closes = [dd[d][-1][4] for d in hist]
    fh_vols = [dd[d][0][5] for d in hist[-20:] if dd[d] and dd[d][0][0] == ENTRY_BAR]
    hourly_c = [b[4] for d in hist[-10:] for b in dd[d]] + [c]
    rng = h - l
    nb, npc = nifty.get(day), nifty.get(prev)
    n_move = ((nb[0][4] / npc[-1][4] - 1) * 100) if nb and npc and nb[0][0] == ENTRY_BAR else 0.0
    avgv = sum(fh_vols) / len(fh_vols) if fh_vols else 0
    return {
        "gain": (c / pc - 1) * 100,
        "green": c > o,
        "pos": (c - l) / rng if rng > 0 else 0.5,           # 1 = closed at the high
        "rvol": v / avgv if avgv > 0 else 1.0,
        "ema20": ema_last(closes, 20),
        "rsi": rsi_last(hourly_c),
        "price": c,
        "nifty": n_move,
    }


def decide(f):
    """The professional-style bias. Returns ('BUY'|'SELL'|None, reason)."""
    if f["ema20"] is None or f["rsi"] is None:
        return None, "not enough history"
    up_trend = f["price"] > f["ema20"]
    # momentum continuation: strong close near the high, real volume, trend and market with it,
    # not yet overextended
    if (f["green"] and f["pos"] >= 0.7 and f["rvol"] >= 1.5 and up_trend
            and f["nifty"] >= -0.2 and 1.0 <= f["gain"] <= 6.0 and f["rsi"] < 80):
        return "BUY", "strong first hour, high volume, uptrend"
    # exhaustion / fade: big run that is already failing (red or long upper wick) on a weak market
    if f["gain"] >= 3.0 and (not f["green"] or f["pos"] <= 0.35) and f["nifty"] <= 0.3:
        return "SELL", "big gap-up failing in the first hour"
    if f["gain"] >= 5.0 and f["rsi"] >= 85 and f["pos"] <= 0.5:
        return "SELL", "very overbought, losing steam"
    return None, "no clear edge"


def simulate(bars, entry, side):
    """bars after entry up to about 2 PM; side +1 buy / -1 sell -> (% result, how)."""
    tp = entry * (1 + side * TP / 100)
    sl = entry * (1 - side * SL / 100)
    last = entry
    for t, o, h, l, c, v in bars:
        if t > LAST_BAR:
            break
        hit_tp = h >= tp if side == 1 else l <= tp
        hit_sl = l <= sl if side == 1 else h >= sl
        if hit_tp and hit_sl:
            return -SL, "both"
        if hit_sl:
            return -SL, "stop"
        if hit_tp:
            return TP, "target"
        last = c
    return side * (last / entry - 1) * 100, "2pm"


def account(trades):
    bal = peak = START
    dd = 0.0
    months = {}
    for day, pct in trades:
        months.setdefault(day[:7], [bal, bal])
        bal *= 1 + (pct - COST) / 100
        months[day[:7]][1] = bal
        peak = max(peak, bal)
        dd = max(dd, (peak - bal) / peak * 100)
    return bal, dd, months


def main():
    t0 = time.time()
    syms = load_list()
    print(f"Downloading 14 months of 1-hour candles for {len(syms)} stocks + Nifty (about 5-8 minutes)...",
          flush=True)
    try:
        nifty = hourly("^NSEI")
    except Exception as e:
        print("Nifty data skipped:", e)
        nifty = {}
    data = {}
    for n, s in enumerate(syms, 1):
        try:
            data[s] = hourly(s)
        except Exception as e:
            print(f"  {s}: skipped ({e})")
        if n % 40 == 0:
            gc.collect()
            print(f"  {n}/{len(syms)} done", flush=True)
        time.sleep(0.3)
    if not data:
        tg("🔬 Top-gainer test: no data could be downloaded. Try again later.")
        return 1

    all_days = sorted({d for v in data.values() for d in v})
    cut = (datetime.now(IST) - timedelta(days=TEST_DAYS)).strftime("%Y-%m-%d")
    sorted_days = {s: sorted(dd) for s, dd in data.items()}
    idx_of = {s: {d: i for i, d in enumerate(ds)} for s, ds in sorted_days.items()}

    rows = []      # (day, sym, gain, decision, reason, result%, how, buy%, sell%)
    for i in range(1, len(all_days)):
        day, prev = all_days[i], all_days[i - 1]
        if day < cut:
            continue
        best = None
        for s, dd in data.items():
            if day not in dd or prev not in dd or not dd[day] or dd[day][0][0] != ENTRY_BAR:
                continue
            g = (dd[day][0][4] / dd[prev][-1][4] - 1) * 100
            if best is None or g > best[1]:
                best = (s, g)
        if not best or best[1] <= 0:
            continue
        s = best[0]
        di = idx_of[s][day]
        if di < 1:
            continue
        f = features(data[s], sorted_days[s], di, nifty)
        if not f:
            continue
        side_txt, why = decide(f)
        later = data[s][day][1:]
        entry = f["price"]
        buy_r, _ = simulate(later, entry, 1)
        sell_r, _ = simulate(later, entry, -1)
        if side_txt:
            r, how = simulate(later, entry, 1 if side_txt == "BUY" else -1)
        else:
            r, how = 0.0, "skip"
        rows.append((day, s, best[1], side_txt, why, r, how, buy_r, sell_r))

    if not rows:
        tg("🔬 Top-gainer test: not enough data for the last year.")
        return 1

    trades = [(r[0], r[5]) for r in rows if r[3]]
    n_all, n_tr = len(rows), len(trades)
    bal, dd, months = account(trades)
    ms = sorted(months.items())
    mname = lambda k: datetime.strptime(k, "%Y-%m").strftime("%b %y")
    wins = sum(1 for _, p in trades if p - COST > 0)
    how = {}
    for r in rows:
        if r[3]:
            how[r[6]] = how.get(r[6], 0) + 1

    def side_line(side):
        rs = [r for r in rows if r[3] == side]
        if not rs:
            return f"{side}: 0 trades"
        w = sum(1 for r in rs if r[5] - COST > 0)
        net = sum(r[5] - COST for r in rs)
        return f"{side}: {len(rs)} trades, won {w} ({w / len(rs) * 100:.0f}%), net {net:+.1f}% of the amount"

    half = rows[len(rows) // 2][0]
    b1, _, _ = account([t for t in trades if t[0] < half])
    b2, _, _ = account([t for t in trades if t[0] >= half])
    always_buy, _, _ = account([(r[0], r[7]) for r in rows])
    always_sell, _, _ = account([(r[0], r[8]) for r in rows])

    lines = [
        "🔬 <b>Top-gainer, smart bias — last 1 year</b>",
        f"Every day at about 10 AM: the No.1 gainer among {len(data)} stocks. The bias is decided from the first "
        f"hour (strength, volume), daily trend, hourly RSI and Nifty → BUY, SELL or no trade.",
        f"Whole balance, no leverage. Target {TP:g}%, stop-loss {SL:g}% (1:2), closed at about 2 PM. "
        f"Costs {COST:.2f}%. Same candle hits both = loss. Compounding.", "",
        f"Days checked: {n_all}  |  Trades taken: <b>{n_tr}</b>  |  No clear edge (skipped): {n_all - n_tr}",
        side_line("BUY"), side_line("SELL"),
        f"Target hit {how.get('target', 0)}, stop-loss hit {how.get('stop', 0)}, both in one candle "
        f"{how.get('both', 0)}, closed at 2 PM {how.get('2pm', 0)}",
        f"Won {wins} of {n_tr} ({(wins / n_tr * 100) if n_tr else 0:.0f}%)", "",
        f"💰 <b>₹1,00,000 → ₹{bal:,.0f}</b> ({(bal / START - 1) * 100:+.1f}%)  |  biggest drop -{dd:.1f}%",
        f"Months in profit: {sum(1 for _, (a, b) in ms if b > a)} of {len(ms)}",
        "Month-end balance: " + ",  ".join(f"{mname(k)} ₹{b:,.0f}" for k, (a, b) in ms),
        f"Is it steady? First half of the year: {(b1 / START - 1) * 100:+.1f}%,  second half: "
        f"{(b2 / START - 1) * 100:+.1f}% (each from ₹1,00,000)", "",
        "<b>For comparison</b> (same stock, same target/stop, no thinking):",
        f"Always BUY every day: ₹1,00,000 → ₹{always_buy:,.0f}",
        f"Always SELL every day: ₹1,00,000 → ₹{always_sell:,.0f}", "",
        "<b>Last 10 trades</b>",
    ]
    for r in [x for x in rows if x[3]][-10:]:
        lines.append(f"{'✅' if r[5] - COST > 0 else '❌'} {r[0][5:]} {r[3]} {r[1]} (up {r[2]:.1f}% at 10 AM): "
                     f"{r[5]:+.2f}% · {r[4]}")
    lines.append("")
    lines.append("Test only — no trades placed. 1-hour candles from Yahoo, so 'about 10 AM' is the 10:15 "
                 "candle close. Real slippage and circuits make results a little worse. "
                 f"({(time.time() - t0) / 60:.0f} min)")
    tg("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
