#!/usr/bin/env python3
"""
Indian stock SWING signals (NSE cash market, long only) -> separate Telegram bot.

* Daily candles. Run once a day after market close (GitHub Actions).
* Several rule sets are tested on ~3 years of data; only one that made a profit
  in BOTH halves of the test is used. If none qualifies, signals pause.
* Signal = BUY at next day's open, with stop-loss and 1:2 target.
* Each signal is tracked until target / stop / time limit and a result is sent.
* Monthly report on the 1st.

Modes:
  python stocks.py scan       -> daily run
  python stocks.py backtest   -> rule-set comparison table
  python stocks.py simulate   -> "what if" account simulation (Rs.1,00,000 start)
  python stocks.py report     -> this month's results so far
  python stocks.py test       -> test message

Data: Yahoo Finance daily candles (free). Standard library only.
"""

import csv
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from scanner import ema, sma, rsi, atr, adx, macd_hist  # same tested indicator code

IST = timezone(timedelta(hours=5, minutes=30))
LIST_FILE = "stocks_list.txt"
STATE_FILE = "stocks_state.json"
LOG_FILE = "stocks_signals.csv"

RR = 2.0                    # target = 2 x risk
HOLD_DAYS = 20              # close a trade after 20 trading days
COST_PCT = 0.25             # delivery round-trip cost (STT, exchange, stamp, GST...)
BT_YEARS = 3                # backtest length
MIN_TRADES = 20
START_CAPITAL = 100000      # simulation start (Rs.)
SIM_RISKS = [1, 2]          # % of capital lost if a stop-loss hits
MAX_POSITIONS = 5           # simulation: max stocks held together
MAX_POSITION_SHARE = 0.20   # simulation: one stock max 20% of capital
INDEX = "^NSEI"             # Nifty 50 index (market filter)

DEFAULT_STOCKS = [
    "RELIANCE", "TCS", "HDFCBANK", "ICICIBANK", "INFY", "BHARTIARTL", "ITC", "SBIN",
    "LT", "HINDUNILVR", "KOTAKBANK", "AXISBANK", "BAJFINANCE", "MARUTI", "SUNPHARMA",
    "HCLTECH", "M&M", "TITAN", "ULTRACEMCO", "NTPC", "ASIANPAINT", "POWERGRID",
    "BAJAJFINSV", "NESTLEIND", "ONGC", "JSWSTEEL", "TATASTEEL", "ADANIENT",
    "ADANIPORTS", "COALINDIA", "WIPRO", "TECHM", "GRASIM", "HINDALCO", "CIPLA",
    "DRREDDY", "EICHERMOT", "SBILIFE", "HDFCLIFE", "BRITANNIA", "APOLLOHOSP",
    "BAJAJ-AUTO", "HEROMOTOCO", "INDUSINDBK", "SHRIRAMFIN", "TRENT", "BEL",
    "ETERNAL", "JIOFIN", "TATACONSUM",
]

# Rule sets tested (long only)
VARIANTS = {
    "P1": {"name": "Dip buy in uptrend", "entry": "dip", "sl_buf": 0.5},
    "P2": {"name": "Dip buy, wide stop", "entry": "dip", "sl_buf": 1.5},
    "B1": {"name": "20-day breakout", "entry": "breakout", "sl_buf": 0.5},
    "B2": {"name": "20-day breakout, wide stop", "entry": "breakout", "sl_buf": 1.5},
    "E1": {"name": "Pullback to EMA20", "entry": "ema20", "sl_buf": 0.5},
    "E2": {"name": "Pullback to EMA20, wide stop", "entry": "ema20", "sl_buf": 1.5},
}


# ----------------------------------------------------------------------------
# Data
# ----------------------------------------------------------------------------
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


def yahoo_daily(symbol, rng="5y"):
    """Daily candles. symbol like RELIANCE (NSE) or ^NSEI."""
    ys = symbol if symbol.startswith("^") else symbol + ".NS"
    q = urllib.parse.quote(ys)
    for host in ("query1", "query2"):
        try:
            j = http_json(f"https://{host}.finance.yahoo.com/v8/finance/chart/{q}"
                          f"?range={rng}&interval=1d&includePrePost=false")
            res = j["chart"]["result"][0]
            break
        except Exception as e:
            err = e
    else:
        raise err
    ts = res.get("timestamp") or []
    qd = res["indicators"]["quote"][0]
    rows = []
    for k, t in enumerate(ts):
        o, h, l, c, v = (qd.get(x, [None] * len(ts))[k] for x in ("open", "high", "low", "close", "volume"))
        if None in (o, h, l, c) or c <= 0:
            continue
        rows.append((t, float(o), float(h), float(l), float(c), float(v or 0)))
    # drop today's candle if the market is still open (before 15:45 IST)
    now = datetime.now(IST)
    if rows:
        last_day = datetime.fromtimestamp(rows[-1][0], IST).date()
        if last_day == now.date() and (now.hour, now.minute) < (15, 45):
            rows = rows[:-1]
    return {
        "t": [r[0] for r in rows],
        "day": [datetime.fromtimestamp(r[0], IST).strftime("%Y-%m-%d") for r in rows],
        "o": [r[1] for r in rows], "h": [r[2] for r in rows], "l": [r[3] for r in rows],
        "c": [r[4] for r in rows], "v": [r[5] for r in rows],
    }


def indicators(d):
    c, h, l, v = d["c"], d["h"], d["l"], d["v"]
    d = dict(d)
    d["ema20"], d["ema50"], d["ema200"] = ema(c, 20), ema(c, 50), ema(c, 200)
    d["rsi"], d["atr"], d["adx"] = rsi(c, 14), atr(h, l, c, 14), adx(h, l, c, 14)
    d["hist"], d["vsma"] = macd_hist(c), sma(v, 20)
    return d


def load_list():
    out = []
    if os.path.exists(LIST_FILE):
        with open(LIST_FILE, encoding="utf-8") as f:
            for line in f:
                s = line.split("#")[0].strip().upper().replace(".NS", "")
                if s and s not in out:
                    out.append(s)
    return out or list(DEFAULT_STOCKS)


def load_all(rng="5y"):
    idx = indicators(yahoo_daily(INDEX, rng))
    data = {}
    for s in load_list():
        try:
            d = yahoo_daily(s, rng)
            if len(d["c"]) > 260:
                data[s] = indicators(d)
            else:
                print(f"[data] {s}: not enough history")
        except Exception as e:
            print(f"[data] {s}: {e}")
        time.sleep(0.3)
    return idx, data


# ----------------------------------------------------------------------------
# Strategy
# ----------------------------------------------------------------------------
def _ok(*x):
    return all(v is not None for v in x)


def index_ok(idx, day):
    """Nifty itself must be above its 50-day average on that day."""
    if "ok" not in idx:
        idx["ok"] = {dd: (e is not None and c > e)
                     for dd, c, e in zip(idx["day"], idx["c"], idx["ema50"])}
    return idx["ok"].get(day, False)


def evaluate(sym, d, i, idx, P):
    if i < 210:
        return None
    c, o, h, l = d["c"], d["o"], d["h"], d["l"]
    e20, e50, e200 = d["ema20"], d["ema50"], d["ema200"]
    a, r, ad, hist, vs = d["atr"][i], d["rsi"], d["adx"][i], d["hist"], d["vsma"]
    if not _ok(e20[i], e50[i], e200[i], e50[i - 10], a, r[i], r[i - 1], ad, hist[i], hist[i - 1], vs[i - 1]):
        return None
    if a <= 0:
        return None
    # uptrend
    if not (c[i] > e50[i] > e200[i] and e50[i] > e50[i - 10] and ad >= 18):
        return None
    if (c[i] - e50[i]) > 3 * a:
        return None                                    # too extended
    if not index_ok(idx, d["day"][i]):
        return None
    if abs(c[i] - o[i]) > 2.5 * a:
        return None                                    # huge candle, don't chase
    reasons = [f"Uptrend: price above 50 & 200-day averages, ADX {ad:.0f}",
               "Nifty 50 above its 50-day average"]
    if P["entry"] == "dip":
        if any(x is None for x in r[i - 3:i]):
            return None
        if not (min(r[i - 3:i]) < 40 and r[i] > r[i - 1] and c[i] > o[i] and c[i] > c[i - 1]):
            return None
        reasons.append(f"RSI dipped below 40 and is turning up ({r[i]:.0f}); green reversal candle")
    elif P["entry"] == "breakout":
        if not (c[i] > max(h[i - 20:i]) and d["v"][i] > 1.5 * vs[i - 1] and r[i] < 75):
            return None
        reasons.append(f"Closed above the 20-day high with {d['v'][i] / vs[i - 1]:.1f}x volume")
    else:  # ema20 pullback
        if any(x is None for x in e20[i - 3:i]):
            return None
        touched = any(l[k] <= e20[k] + 0.005 * abs(e20[k]) for k in range(i - 3, i + 1))
        if not (touched and c[i] > e20[i] and c[i] > h[i - 1] and hist[i] > hist[i - 1]):
            return None
        reasons.append("Pulled back to the 20-day average and bounced above yesterday's high")
    swing = min(l[i - 5:i + 1]) - P["sl_buf"] * a
    risk = max(c[i] - swing, 1.0 * a)
    if risk > 3.5 * a:
        return None
    risk_pct = risk / abs(c[i]) * 100
    if risk_pct < 2 or risk_pct > 12:
        return None
    return {"symbol": sym, "close": c[i], "sl": c[i] - risk, "risk": risk,
            "risk_pct": risk_pct, "reasons": reasons, "day": d["day"][i]}


def simulate_trade(d, i, sig):
    """Buy next day's open; returns (R, exit_index, result) or None if not filled."""
    if i + 1 >= len(d["c"]):
        return None
    entry = d["o"][i + 1]
    sl = sig["sl"]
    if entry <= sl:
        return None                                    # gapped below stop: skip
    risk = entry - sl
    tp = entry + RR * risk
    cost_r = COST_PCT / 100 * abs(entry) / risk
    end = min(len(d["c"]) - 1, i + HOLD_DAYS)
    for k in range(i + 1, end + 1):
        o, hi, lo = d["o"][k], d["h"][k], d["l"][k]
        if k > i + 1 and o <= sl:                      # gap down through stop
            return (o - entry) / risk - cost_r, k, "loss"
        if lo <= sl:
            return -1.0 - cost_r, k, "loss"
        if k > i + 1 and o >= tp:
            return (o - entry) / risk - cost_r, k, "target"
        if hi >= tp:
            return RR - cost_r, k, "target"
    if end - i < HOLD_DAYS:
        return None                                    # still running at data end
    return (d["c"][end] - entry) / risk - cost_r, end, "time"


def gen_trades(idx, data, start_day, variants=None):
    out = {}
    for key, P in (variants or VARIANTS).items():
        trades = []
        for sym, d in data.items():
            busy = -1
            for i in range(210, len(d["c"]) - 1):
                if i <= busy or d["day"][i] < start_day:
                    continue
                sig = evaluate(sym, d, i, idx, P)
                if not sig:
                    continue
                res = simulate_trade(d, i, sig)
                if not res:
                    continue
                rr, k, what = res
                entry = d["o"][i + 1]
                trades.append({"sym": sym, "r": rr, "in": d["day"][i + 1], "out": d["day"][k],
                               "res": what, "stop_pct": (entry - sig["sl"]) / abs(entry) * 100})
                busy = k
        out[key] = trades
    return out


def stats(trades, a, b):
    t = [x for x in trades if a <= x["in"] < b]
    n = len(t)
    if not n:
        return {"n": 0, "win": 0, "net": 0, "net1": 0, "net2": 0}
    # split by calendar middle of the window
    da = datetime.strptime(a, "%Y-%m-%d")
    db = datetime.strptime(b, "%Y-%m-%d")
    mid = (da + (db - da) / 2).strftime("%Y-%m-%d")
    return {
        "n": n, "win": sum(1 for x in t if x["r"] > 0) / n * 100,
        "net": sum(x["r"] for x in t),
        "net1": sum(x["r"] for x in t if x["in"] < mid),
        "net2": sum(x["r"] for x in t if x["in"] >= mid),
    }


def choose(res):
    good = [(k, s) for k, s in res.items()
            if s["n"] >= MIN_TRADES and s["net"] > 0 and s["net1"] > 0 and s["net2"] > 0]
    if not good:
        return None
    good.sort(key=lambda ks: (ks[1]["win"], ks[1]["net"]), reverse=True)
    return good[0][0]


def simulate_account(trades, a, b, risk_pct, max_share=None, max_exposure=1.0):
    """Replay trades with capital %, max 5 positions, max share per stock, exposure limit."""
    max_share = MAX_POSITION_SHARE if max_share is None else max_share
    tr = sorted([x for x in trades if a <= x["in"] < b], key=lambda x: (x["in"], x["sym"]))
    cap = peak = START_CAPITAL
    dd = 0.0
    open_ = []          # (out_day, R, risk_amount, position_value)
    taken = won = 0
    for t in tr:
        for o in sorted([o for o in open_ if o[0] < t["in"]]):
            open_.remove(o)
            cap += o[1] * o[2]
            won += 1 if o[1] > 0 else 0
            peak = max(peak, cap)
            dd = max(dd, (peak - cap) / peak * 100)
        if len(open_) >= MAX_POSITIONS:
            continue
        risk_amt = cap * risk_pct / 100
        pos = risk_amt / (t["stop_pct"] / 100)
        if pos > cap * max_share:                      # cap position size, risk shrinks
            pos = cap * max_share
            risk_amt = pos * t["stop_pct"] / 100
        if sum(o[3] for o in open_) + pos > cap * max_exposure:
            continue
        open_.append((t["out"], t["r"], risk_amt, pos))
        taken += 1
    for o in sorted(open_):
        cap += o[1] * o[2]
        won += 1 if o[1] > 0 else 0
        peak = max(peak, cap)
        dd = max(dd, (peak - cap) / peak * 100)
    return {"end": cap, "growth": (cap / START_CAPITAL - 1) * 100, "taken": taken,
            "won": won, "dd": dd}


# ----------------------------------------------------------------------------
# Telegram + state + log
# ----------------------------------------------------------------------------
def tg(text):
    token = os.environ.get("STOCKS_BOT_TOKEN", "").strip()
    chat = os.environ.get("STOCKS_CHAT_ID", "").strip()
    if not token or not chat:
        print("[telegram] not configured, message:\n" + text + "\n")
        return False
    for start in range(0, len(text), 4000):
        body = urllib.parse.urlencode({"chat_id": chat, "text": text[start:start + 4000],
                                       "parse_mode": "HTML",
                                       "disable_web_page_preview": "true"}).encode()
        try:
            urllib.request.urlopen(urllib.request.Request(
                f"https://api.telegram.org/bot{token}/sendMessage", data=body), timeout=20).read()
        except urllib.error.HTTPError as e:
            print("[telegram] error", e.code, e.read().decode()[:300])
            return False
        except Exception as e:
            print("[telegram] error", e)
            return False
    return True


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            s = json.load(f)
            if isinstance(s, dict):
                return s
    except Exception:
        pass
    return {}


def save_state(s):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(s, f)


FIELDS = ["id", "signal_day", "symbol", "rule", "close", "sl", "tp_pct", "entry_day",
          "entry", "tp", "status", "result", "r", "exit_day"]


def read_log():
    if not os.path.exists(LOG_FILE):
        return []
    with open(LOG_FILE, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_log(rows):
    with open(LOG_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def rs(x):
    return f"₹{x:,.2f}"


# ----------------------------------------------------------------------------
# Rule-set choice
# ----------------------------------------------------------------------------
def window(years_back_start, years_back_end=0):
    today = datetime.now(IST)
    a = (today - timedelta(days=int(365 * years_back_start))).strftime("%Y-%m-%d")
    b = (today - timedelta(days=int(365 * years_back_end)) + timedelta(days=1)).strftime("%Y-%m-%d")
    return a, b


def backtest_and_choose(state, idx, data, announce):
    a, b = window(BT_YEARS)
    trades = gen_trades(idx, data, a)
    res = {k: stats(t, a, b) for k, t in trades.items()}
    best = choose(res)
    prev = (state.get("active") or {}).get("variant")
    state["active"] = {"variant": best, "updated": time.time(),
                       "win": res[best]["win"] if best else 0, "n": res[best]["n"] if best else 0}
    lines = [f"📊 <b>Stock rule-set test</b> — last {BT_YEARS} years, {len(data)} Nifty stocks",
             f"Buy next day open, stop-loss, target 1:{RR:g}, max {HOLD_DAYS} days, costs included", ""]
    for k, s in res.items():
        mark = "✅" if k == best else ("➖" if s["net"] > 0 else "❌")
        lines.append(f"{mark} {k} {VARIANTS[k]['name']}: {s['n']} trades, win {s['win']:.0f}%, "
                     f"total {s['net'] * 100:+.0f}% (halves {s['net1'] * 100:+.0f}% / {s['net2'] * 100:+.0f}%)")
    lines.append("")
    lines.append(f"✅ <b>Signals now use {best}: {VARIANTS[best]['name']}</b>" if best else
                 "⏸ <b>Signals PAUSED</b> — no rule set was profitable in both halves.")
    lines.append("% = profit/loss compared to the amount risked per trade (100% = one full stop-loss).")
    msg = "\n".join(lines)
    print(msg)
    if announce or best != prev:
        tg(msg)
    return best


# ----------------------------------------------------------------------------
# Modes
# ----------------------------------------------------------------------------
def update_open(data):
    rows = read_log()
    changed = False
    for r in rows:
        if r.get("status") != "OPEN":
            continue
        d = data.get(r["symbol"])
        if not d:
            continue
        try:
            i = d["day"].index(r["signal_day"])
        except ValueError:
            continue
        if i + 1 >= len(d["c"]):
            continue                                       # not filled yet
        sig = {"sl": float(r["sl"])}
        res = simulate_trade(d, i, sig)
        entry = d["o"][i + 1]
        r["entry_day"], r["entry"] = d["day"][i + 1], f"{entry:.2f}"
        r["tp"] = f"{entry + RR * (entry - float(r['sl'])):.2f}"
        if entry <= float(r["sl"]):
            r.update(status="CLOSED", result="SKIPPED (opened below stop-loss)", r="0", exit_day=d["day"][i + 1])
            changed = True
            tg(f"⚪ <b>{r['symbol']}</b> opened below the stop-loss — signal cancelled, no trade.")
            continue
        if not res:
            changed = True
            continue
        rr, k, what = res
        txt = {"target": "TARGET HIT", "loss": "STOP-LOSS HIT", "time": f"CLOSED AFTER {HOLD_DAYS} DAYS"}[what]
        r.update(status="CLOSED", result=txt, r=f"{rr:.2f}", exit_day=d["day"][k])
        changed = True
        tg(f"{'✅' if rr > 0 else '❌'} <b>Result: {r['symbol']}</b> (bought {r['entry_day']} at {rs(entry)})\n"
           f"{txt} on {d['day'][k]} → <b>{rr * 100:+.0f}%</b> of the trade amount")
    if changed:
        write_log(rows)


def month_report(ym, title=None):
    rows = [r for r in read_log() if r.get("signal_day", "").startswith(ym)]
    name = datetime.strptime(ym, "%Y-%m").strftime("%B %Y")
    head = title or f"📅 <b>Stock monthly report — {name}</b>"
    real = [r for r in rows if not r.get("result", "").startswith("SKIPPED")]
    closed = [r for r in real if r["status"] == "CLOSED"]
    if not real:
        return f"{head}\nNo stock signals in this month."
    won = [r for r in closed if float(r["r"] or 0) > 0]
    net = sum(float(r["r"] or 0) for r in closed)
    return "\n".join([
        head, "",
        f"Signals: <b>{len(real)}</b> (finished {len(closed)}, still open {len(real) - len(closed)})",
        f"✅ Won: <b>{len(won)}</b>   ❌ Lost: <b>{len(closed) - len(won)}</b>   "
        f"Win rate: <b>{(len(won) / len(closed) * 100) if closed else 0:.0f}%</b>",
        f"Total: <b>{net * 100:+.0f}%</b> of one trade amount "
        f"(at 1% risk per trade ≈ {net:+.1f}% capital growth)",
        "(Costs included. Full list: stocks_signals.csv in GitHub.)",
    ])


def run_scan():
    state = load_state()
    idx, data = load_all("5y")
    if not data:
        tg("⚠️ Stock scanner could not download market data today. Will retry tomorrow.")
        return 0
    update_open(data)

    now = datetime.now(IST)
    ym = now.strftime("%Y-%m")
    if state.get("report_month") and state["report_month"] != ym:
        prev = (now.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
        tg(month_report(prev))
    state["report_month"] = ym

    active = state.get("active")
    if not active or time.time() - active.get("updated", 0) > 6 * 86400:   # re-test weekly
        backtest_and_choose(state, idx, data, announce=False)
        active = state["active"]
    save_state(state)
    if not active.get("variant"):
        print("paused: no profitable rule set")
        return 0
    P = VARIANTS[active["variant"]]

    latest = max(d["day"][-1] for d in data.values())
    if latest != now.strftime("%Y-%m-%d"):
        print(f"no new candle today (latest {latest}) - market holiday or data delay")
        return 0
    rows = read_log()
    open_syms = {r["symbol"] for r in rows if r["status"] == "OPEN"}
    new = 0
    for sym, d in data.items():
        if sym in open_syms or d["day"][-1] != latest:
            continue
        sig = evaluate(sym, d, len(d["c"]) - 1, idx, P)
        if not sig:
            continue
        c, sl = sig["close"], sig["sl"]
        tp = c + RR * (c - sl)
        qty_note = (f"Example at 1% risk on ₹1,00,000: risk ₹1,000 → about "
                    f"{int(1000 / (c - sl))} shares (≈ {rs(int(1000 / (c - sl)) * c)})")
        tg("\n".join([
            f"🟢 <b>BUY — {sym}</b> (NSE)",
            f"Plan: buy at tomorrow's open (around {rs(c)})", "",
            f"Stop-loss: {rs(sl)} ({(sl / c - 1) * 100:+.1f}%)",
            f"Target: {rs(tp)} ({(tp / c - 1) * 100:+.1f}%)  [1:{RR:g}]",
            f"Hold max {HOLD_DAYS} trading days.", "",
            "<b>Reasons:</b>"] + [f"• {x}" for x in sig["reasons"]] + [
            "", qty_note,
            "If it opens below the stop-loss, skip it. Delivery (CNC) only — no F&O.",
            f"Rule set: {P['name']} — {BT_YEARS}-year test: {active['win']:.0f}% win, {active['n']} trades",
            "⚠️ Paper trade first — not financial advice."]))
        rows.append({"id": f"{sym}-{latest}", "signal_day": latest, "symbol": sym,
                     "rule": active["variant"], "close": f"{c:.2f}", "sl": f"{sl:.2f}",
                     "tp_pct": f"{(tp / c - 1) * 100:.2f}", "status": "OPEN"})
        new += 1
    write_log(rows)
    print(f"scan done, {new} new signals")
    return 0


def run_backtest():
    state = load_state()
    idx, data = load_all("5y")
    backtest_and_choose(state, idx, data, announce=True)
    save_state(state)
    return 0


def run_simulate():
    idx, data = load_all("5y")
    a_all, b = window(BT_YEARS)
    trades = gen_trades(idx, data, a_all)
    a_old, split = window(BT_YEARS, 1)
    _, now_b = window(0)
    old = {k: stats(t, a_old, split) for k, t in trades.items()}
    full = {k: stats(t, a_all, b) for k, t in trades.items()}
    pick_old, pick_full = choose(old), choose(full)
    lines = [f"💰 <b>Stock simulation: ₹{START_CAPITAL:,} start</b>",
             f"Nifty stocks, buy next open, stop-loss, target 1:{RR:g}, max {MAX_POSITIONS} stocks, "
             f"max {int(MAX_POSITION_SHARE * 100)}% per stock, no leverage, costs included.", ""]

    def block(title, key, a, bb):
        out = [title]
        t = [x for x in trades[key] if a <= x["in"] < bb]
        for rk in SIM_RISKS:
            r = simulate_account(trades[key], a, bb, rk)
            if rk == SIM_RISKS[0]:
                out.append(f"Trades taken: <b>{r['taken']}</b> | won {r['won']} | "
                           f"win rate <b>{(r['won'] / r['taken'] * 100) if r['taken'] else 0:.0f}%</b> "
                           f"(signals: {len(t)})")
            out.append(f"• {rk}% risk per trade: ₹{START_CAPITAL:,} → <b>{rs(r['end'])}</b> "
                       f"({r['growth']:+.1f}%), biggest drop -{r['dd']:.1f}%")
        return out

    if pick_old:
        lines += block(f"<b>A) Honest test — last 12 months</b> (rule {pick_old}: "
                       f"{VARIANTS[pick_old]['name']}, chosen from the 2 years before)", pick_old, split, now_b)
    else:
        lines.append("<b>A) Honest test — last 12 months:</b> no rule set qualified on the older data.")
    lines.append("")
    if pick_full:
        lines += block(f"<b>B) Full {BT_YEARS} years</b> (rule {pick_full}: {VARIANTS[pick_full]['name']}, "
                       f"optimistic)", pick_full, a_all, b)
    else:
        lines.append(f"<b>B) Full {BT_YEARS} years:</b> no rule set qualified.")
    lines += ["", "Trust A more than B. Real trading has slippage. Past results do not guarantee future results."]
    msg = "\n".join(lines)
    tg(msg)
    print(msg)
    return 0


# ----------------------------------------------------------------------------
# TEST ONLY: stock futures, both directions (BUY in uptrends, SELL in downtrends)
# ----------------------------------------------------------------------------
FUT_COST_PCT = 0.10        # futures round-trip cost incl. slippage
FUT_MAX_SHARE = 1.0        # one position up to 1x capital (notional)
FUT_MAX_EXPOSURE = 3.0     # all positions together up to 3x capital (margin)


def mirror(d):
    """Price series turned upside down: a SHORT on d == a LONG on mirror(d)."""
    neg = lambda xs: [None if x is None else -x for x in xs]
    m = dict(d)
    m["o"], m["c"] = neg(d["o"]), neg(d["c"])
    m["h"], m["l"] = neg(d["l"]), neg(d["h"])
    for k in ("ema20", "ema50", "ema200", "hist"):
        m[k] = neg(d[k])
    m["rsi"] = [None if x is None else 100 - x for x in d["rsi"]]
    m.pop("ok", None)
    return m


def run_futures_test():
    global COST_PCT
    idx, data = load_all("5y")
    a_all, b = window(BT_YEARS)
    a_old, split = window(BT_YEARS, 1)
    _, now_b = window(0)
    saved = COST_PCT
    COST_PCT = FUT_COST_PCT
    try:
        longs = gen_trades(idx, data, a_all)
        shorts = gen_trades(mirror(idx), {s: mirror(d) for s, d in data.items()}, a_all)
    finally:
        COST_PCT = saved
    both = {k: longs[k] + shorts[k] for k in VARIANTS}

    full = {k: stats(t, a_all, b) for k, t in both.items()}
    old = {k: stats(t, a_old, split) for k, t in both.items()}
    pick_full, pick_old = choose(full), choose(old)

    lines = [f"🧪 <b>TEST: Stock FUTURES, both directions</b> — {len(data)} stocks, {BT_YEARS} years",
             "BUY in uptrends, SELL (short) in downtrends, target 1:2, max 20 days, costs included", ""]
    for k, s in full.items():
        sl = stats(shorts[k], a_all, b)
        mark = "✅" if k == pick_full else ("➖" if s["net"] > 0 else "❌")
        lines.append(f"{mark} {k} {VARIANTS[k]['name']}: {s['n']} trades ({sl['n']} short), win {s['win']:.0f}%, "
                     f"total {s['net'] * 100:+.0f}% (halves {s['net1'] * 100:+.0f}% / {s['net2'] * 100:+.0f}%)")
    lines.append("")

    def block(title, key, a, bb):
        out = [title]
        t = [x for x in both[key] if a <= x["in"] < bb]
        nshort = sum(1 for x in shorts[key] if a <= x["in"] < bb)
        for rk in SIM_RISKS:
            r = simulate_account(both[key], a, bb, rk, FUT_MAX_SHARE, FUT_MAX_EXPOSURE)
            if rk == SIM_RISKS[0]:
                out.append(f"Trades taken: <b>{r['taken']}</b> | won {r['won']} | win rate "
                           f"<b>{(r['won'] / r['taken'] * 100) if r['taken'] else 0:.0f}%</b> "
                           f"(signals {len(t)}, of which {nshort} short)")
            out.append(f"• {rk}% risk per trade: ₹{START_CAPITAL:,} → <b>{rs(r['end'])}</b> "
                       f"({r['growth']:+.1f}%), biggest drop -{r['dd']:.1f}%")
        return out

    if pick_old:
        lines += block(f"<b>A) Honest test — last 12 months</b> (rule {pick_old}, chosen from the 2 years before)",
                       pick_old, split, now_b)
    else:
        lines.append("<b>A) Honest test — last 12 months:</b> no rule set qualified on the older data.")
    lines.append("")
    if pick_full:
        lines += block(f"<b>B) Full {BT_YEARS} years</b> (rule {pick_full}, optimistic)", pick_full, a_all, b)
    else:
        lines.append(f"<b>B) Full {BT_YEARS} years:</b> no rule set qualified.")
    lines += ["", "TEST ONLY — no signals are sent from this. Real stock futures need big lots "
                  "(one lot usually needs about ₹1–3 lakh margin). Past results do not guarantee future results."]
    msg = "\n".join(lines)
    tg(msg)
    print(msg)
    return 0


def run_report():
    now = datetime.now(IST)
    tg(month_report(now.strftime("%Y-%m"), f"📅 <b>Stock report so far — {now.strftime('%B %Y')}</b>"))
    return 0


if __name__ == "__main__":
    mode = (sys.argv[1] if len(sys.argv) > 1 else "scan").lower()
    if mode == "test":
        ok = tg("✅ India Stocks bot connected. Stock signals will arrive here (separate from crypto).")
        print("sent" if ok else "not sent")
        sys.exit(0)
    sys.exit({"backtest": run_backtest, "simulate": run_simulate, "futures": run_futures_test,
              "report": run_report}.get(mode, run_scan)())
