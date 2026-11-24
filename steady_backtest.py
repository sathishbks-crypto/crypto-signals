#!/usr/bin/env python3
"""
TEST ONLY - places no trade.

Idea: every hour, find Binance futures coins that moved STEADILY in the last
60 minutes (at least 2%, never 1% the other way) and trade in the
same direction, hoping the momentum continues:
    steady riser  -> BUY
    steady faller -> SELL
Each trade: 5x leverage, stop-loss = 5% of the trade amount (1% price move),
target = 10% of the trade amount (2% price move). 1:2.
Fees 0.05% per side of the position are included (= 0.5% of the trade amount
for a round trip at 5x). A trade still open after 24 hours is closed.
If target and stop-loss fall inside the same 5-minute candle -> counted as a loss.

Tested on the last 90 days of 5-minute candles. For comparison it also shows
the opposite idea (trading AGAINST the move).

Run on the server (bot folder):   python3 steady_backtest.py
Takes about 10-15 minutes. The report goes to Telegram.
"""

import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
IST = timezone(timedelta(hours=5, minutes=30))
BASE = "https://fapi.binance.com"

DAYS = 90
LEV = 5
SL_PRICE = 1.0            # % price  -> 5% of the trade amount
TP_PRICE = 2.0            # % price  -> 10% of the trade amount
FEE_PRICE = 0.10          # % of the position, round trip
MIN_MOVE = 2.0            # % the coin must have moved in the last hour
PULLBACKS = (1.0,)        # largest move against the trend allowed (1 : 2)
MAX_HOLD_MS = 24 * 3600 * 1000
MIN_VOL = 20_000_000
BAR_MS = 5 * 60 * 1000
START_BAL = 100.0

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
    for i in range(0, len(text), 4000):
        body = urllib.parse.urlencode({"chat_id": chat, "text": text[i:i + 4000], "parse_mode": "HTML"}).encode()
        try:
            urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", body, timeout=20)
        except Exception as e:
            print("[telegram]", e)


def get(path, **params):
    url = BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
    for a in range(4):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}),
                                        timeout=25) as r:
                return json.loads(r.read().decode())
        except Exception:
            if a == 3:
                raise
            time.sleep(2 * (a + 1))


def klines(sym, start_ms, end_ms):
    """5-minute candles as 5 parallel lists (t, o, h, l, c)."""
    T, O, H, L, C = [], [], [], [], []
    cur = start_ms
    while cur < end_ms:
        rows = get("/fapi/v1/klines", symbol=sym, interval="5m", startTime=cur, endTime=end_ms, limit=1000)
        if not rows:
            break
        for r in rows:
            T.append(int(r[0])); O.append(float(r[1])); H.append(float(r[2])); L.append(float(r[3])); C.append(float(r[4]))
        cur = int(rows[-1][0]) + BAR_MS
        time.sleep(0.12)
        if len(rows) < 1000:
            break
    return T, O, H, L, C


def steady(O, H, L, C, a, b, pullback):
    """Bars a..b-1 = the last hour. Returns +1 (riser), -1 (faller) or 0."""
    start = O[a]
    hi = lo = start
    wu = wd = 0.0
    for i in range(a, b):
        wu = max(wu, (hi - L[i]) / hi * 100)
        wd = max(wd, (H[i] - lo) / lo * 100)
        hi, lo = max(hi, H[i]), min(lo, L[i])
    last = C[b - 1]
    move = (last / start - 1) * 100
    if move >= MIN_MOVE and wu < pullback and (hi - last) / hi * 100 <= 0.3:
        return 1
    if -move >= MIN_MOVE and wd < pullback and (last - lo) / lo * 100 <= 0.3:
        return -1
    return 0


def trade(T, H, L, C, i, side):
    """Enter at close of bar i-1 (= start of bar i). Returns (result % of trade amount, exit_ms)."""
    e = C[i - 1]
    tp = e * (1 + side * TP_PRICE / 100)
    sl = e * (1 - side * SL_PRICE / 100)
    fee = FEE_PRICE * LEV
    j = i
    while j < len(T) and T[j] - T[i] < MAX_HOLD_MS:
        hit_tp = H[j] >= tp if side == 1 else L[j] <= tp
        hit_sl = L[j] <= sl if side == 1 else H[j] >= sl
        if hit_sl:
            return -SL_PRICE * LEV - fee, T[j] + BAR_MS
        if hit_tp:
            return TP_PRICE * LEV - fee, T[j] + BAR_MS
        j += 1
    j = min(j, len(T) - 1)
    return side * (C[j] / e - 1) * 100 * LEV - fee, T[j] + BAR_MS


def account(trades):
    """trades: (entry_ms, exit_ms, result%).
    Each day (India time) the balance at the start of the day is split into 24 hourly parts.
    In an hour with k signals, each trade gets 1/k of that hour's part. Profits compound daily.
    Returns final balance, biggest drop, months, busy hours."""
    bal = peak = START_BAL
    dd = 0.0
    open_ = []          # (exit_ms, stake, result%)
    months = {}
    by_hour = {}
    for en, ex, r in trades:
        by_hour.setdefault(en, []).append((ex, r))
    day, budget = None, 0.0
    for hb in sorted(by_hour):
        still = []
        for o in open_:
            if o[0] <= hb:
                bal += o[1] * o[2] / 100
            else:
                still.append(o)
        open_ = still
        peak = max(peak, bal)
        dd = max(dd, (peak - bal) / peak * 100)
        d = datetime.fromtimestamp(hb / 1000, IST)
        if d.strftime("%Y-%m-%d") != day:
            day, budget = d.strftime("%Y-%m-%d"), max(bal, 0) / 24
        m = d.strftime("%Y-%m")
        months.setdefault(m, [bal, bal])
        months[m][1] = bal
        sig = by_hour[hb]
        for ex, r in sig:
            open_.append((ex, budget / len(sig), r))
    for o in open_:
        bal += o[1] * o[2] / 100
    if months:
        months[sorted(months)[-1]][1] = bal
    peak = max(peak, bal)
    dd = max(dd, (peak - bal) / peak * 100)
    return bal, dd, months, len(by_hour)


def main():
    t0 = time.time()
    end_ms = int(time.time() * 1000) // 3_600_000 * 3_600_000
    start_ms = end_ms - DAYS * 86400 * 1000
    info = get("/fapi/v1/exchangeInfo")
    live = {x["symbol"] for x in info["symbols"]
            if x.get("contractType") == "PERPETUAL" and x.get("quoteAsset") == "USDT" and x.get("status") == "TRADING"}
    vols = {t["symbol"]: float(t["quoteVolume"]) for t in get("/fapi/v1/ticker/24hr")}
    syms = sorted(s for s in live if vols.get(s, 0) >= MIN_VOL)
    print(f"Testing {len(syms)} coins over {DAYS} days of 5-minute candles (about 10-15 minutes)...", flush=True)

    res = {p: {"mom": [], "fade": [], "n": {1: 0, -1: 0}} for p in PULLBACKS}
    side_stats = {p: {1: [], -1: []} for p in PULLBACKS}
    for k, s in enumerate(syms, 1):
        try:
            T, O, H, L, C = klines(s, start_ms, end_ms)
        except Exception as e:
            print(f"  {s}: skipped ({e})")
            continue
        if len(T) < 300:
            continue
        idx = {t: i for i, t in enumerate(T)}
        busy = {p: 0 for p in PULLBACKS}
        for hb in range(start_ms + 3_600_000, end_ms, 3_600_000):
            b = idx.get(hb)                       # first bar after the hour mark = entry bar
            a = idx.get(hb - 3_600_000)
            if a is None or b is None or b - a != 12 or b >= len(T) - 1:
                continue
            for p in PULLBACKS:
                if hb < busy[p]:
                    continue
                d = steady(O, H, L, C, a, b, p)
                if not d:
                    continue
                r, ex = trade(T, H, L, C, b, d)
                rf, exf = trade(T, H, L, C, b, -d)
                res[p]["mom"].append((hb, ex, r))
                res[p]["fade"].append((hb, exf, rf))
                res[p]["n"][d] += 1
                side_stats[p][d].append(r)
                busy[p] = ex
        del T, O, H, L, C, idx
        if k % 20 == 0:
            print(f"  {k}/{len(syms)} coins done", flush=True)

    def summary(tr):
        n = len(tr)
        if not n:
            return "no trades", None
        w = sum(1 for _, _, r in tr if r > 0)
        net = sum(r for _, _, r in tr)
        bal, dd, months, hours = account(tr)
        return (f"{n} trades, won {w} ({w / n * 100:.0f}%), total {net:+.0f}% of one trade amount\n"
                f"   ${START_BAL:.0f} wallet (each day ÷ 24 per hour, split between that hour's signals; "
                f"signals in {hours} of {DAYS * 24} hours): "
                f"<b>${bal:,.2f}</b> ({(bal / START_BAL - 1) * 100:+.1f}%), biggest drop -{dd:.1f}%"), months

    lines = [f"🔬 <b>Steady-mover test — last {DAYS} days</b>",
             f"{len(syms)} Binance futures coins, checked every hour. Steady = moved ≥{MIN_MOVE:g}% in the last hour "
             f"without going {PULLBACKS[0]:g}% the other way. 5x, stop-loss 5% / target 10% of the trade amount (1:2), "
             f"fees included, max 24h per trade.", ""]
    for p in PULLBACKS:
        mom_txt, months = summary(res[p]["mom"])
        fade_txt, _ = summary(res[p]["fade"])
        lines.append(f"<b>Steady = +{MIN_MOVE:g}% without -{p:g}%</b> (signals: {res[p]['n'][1]} risers, {res[p]['n'][-1]} fallers)")
        for d, name in ((1, "BUY risers"), (-1, "SELL fallers")):
            rs = side_stats[p][d]
            if rs:
                w = sum(1 for r in rs if r > 0)
                lines.append(f"   {name}: {len(rs)} trades, won {w / len(rs) * 100:.0f}%, "
                             f"total {sum(rs):+.0f}% of one trade amount")
        lines.append(f"   📈 <b>Follow the move</b>: {mom_txt}")
        if months:
            lines.append("   Month-end: " + ",  ".join(
                f"{datetime.strptime(m, '%Y-%m').strftime('%b')} ${v[1]:,.0f}" for m, v in sorted(months.items())))
        lines.append(f"   🔄 Opposite (trade against the move): {fade_txt}")
        lines.append("")
    lines.append(f"Test only — no trades placed. Real slippage makes results a little worse. "
                 f"({(time.time() - t0) / 60:.0f} min)")
    tg("\n".join(lines))


if __name__ == "__main__":
    main()
