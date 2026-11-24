#!/usr/bin/env python3
"""
TEST ONLY - places no trade.

Idea (BUY only, never SELL):
  The daily candle opens at 5:30 AM India time (00:00 UTC). Start from that open price.
  Every time the price rises +1% from its LOWEST point since the last trade
  (at the start: since the open), open a new LONG. After each trade the count
  starts again from that entry price. So:
     rises 1%, 2%, 3% above the open  -> a trade at each step
     falls, then bounces 1% from the new low -> another trade
  No limit on the number of trades per day; several can be open together.
  Each trade: $10, 5x leverage.
     stop-loss = 5% of the trade amount  -> price 1% below the entry
     target    = 10% of the trade amount -> price 2% above the entry   (1:2)
  If the price only falls, no trade is taken at all.

Run on the server (bot folder):
    python3 btc_levels.py                  today, BTCUSDT
    python3 btc_levels.py BTCUSDT 30       each of the last 30 days + total
    python3 btc_levels.py ETHUSDT          today, ETH
    python3 btc_levels.py BTCUSDT 30 0.5   same with a 0.5% step:
                                           BUY on +0.5%, stop -0.5% (2.5%), target +1% (5%)
    python3 btc_levels.py BTCUSDT 30 1 sell   OPPOSITE: SELL (short) every time the price
                                           falls 1% from its highest point since the last trade
                                           (stop +1% above, target -2% below). Never BUYs.
The report goes to Telegram.
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

STEP = float(sys.argv[3]) if len(sys.argv) > 3 else 1.0   # % rise that triggers a BUY
SL_PRICE = STEP     # % price below the entry  (1% -> 5% of the amount at 5x)
TP_PRICE = 2 * STEP # % price in favour        (2% -> 10% of the amount at 5x)  1:2
SIDE = -1 if len(sys.argv) > 4 and sys.argv[4].lower().startswith("s") else 1   # 1 = BUY, -1 = SELL
LEV = 5
AMOUNT = 10.0       # $ per trade
FEE = 0.10 * LEV    # % of the amount, round trip

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
    url = BASE + path + "?" + urllib.parse.urlencode(params)
    for a in range(4):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}),
                                        timeout=25) as r:
                return json.loads(r.read().decode())
        except Exception:
            if a == 3:
                raise
            time.sleep(2 * (a + 1))


def klines(sym, interval, start_ms, end_ms):
    out, cur = [], start_ms
    while cur < end_ms:
        rows = get("/fapi/v1/klines", symbol=sym, interval=interval, startTime=cur, endTime=end_ms, limit=1500)
        if not rows:
            break
        out += [(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4])) for r in rows]
        cur = int(rows[-1][0]) + 1
        if len(rows) < 1500:
            break
        time.sleep(0.1)
    return out


def run_day(bars, day_start, day_end):
    """bars: candles from day_start until now. Returns list of trades."""
    day_bars = [b for b in bars if day_start <= b[0] < day_end]
    if not day_bars:
        return [], None
    open_px = day_bars[0][1]
    trades = []
    ref = open_px     # BUY: lowest price since the last trade; SELL: highest (start: the open)
    for i, b in enumerate(bars):
        if b[0] >= day_end:
            break
        if b[0] < day_start:
            continue
        took = False
        if SIDE == 1:
            # every +STEP% from the low reached in this candle = one more BUY
            while b[2] >= ref * (1 + STEP / 100):
                lvl = ref * (1 + STEP / 100)
                trades.append({"k": len(trades) + 1, "entry": lvl, "low": ref, "t": b[0], "i": i, "res": None})
                ref, took = lvl, True
            if not took:
                ref = min(ref, b[3])            # follow the price down
        else:
            # every -STEP% from the high reached in this candle = one more SELL
            while b[3] <= ref * (1 - STEP / 100):
                lvl = ref * (1 - STEP / 100)
                trades.append({"k": len(trades) + 1, "entry": lvl, "low": ref, "t": b[0], "i": i, "res": None})
                ref, took = lvl, True
            if not took:
                ref = max(ref, b[2])            # follow the price up
    for t in trades:
        e = t["entry"]
        tp, sl = e * (1 + SIDE * TP_PRICE / 100), e * (1 - SIDE * SL_PRICE / 100)
        for b in bars[t["i"] + 1:]:
            hit_sl = b[3] <= sl if SIDE == 1 else b[2] >= sl
            hit_tp = b[2] >= tp if SIDE == 1 else b[3] <= tp
            if hit_sl:                           # stop first if both in one candle (safe side)
                t["res"], t["out_t"] = "stop", b[0]
                break
            if hit_tp:
                t["res"], t["out_t"] = "target", b[0]
                break
        if t["res"] is None:
            last = bars[-1][4]
            t["res"], t["open_pnl"] = "open", SIDE * (last / e - 1) * 100 * LEV
    return trades, open_px


def pnl(t):
    if t["res"] == "target":
        return AMOUNT * (TP_PRICE * LEV - FEE) / 100
    if t["res"] == "stop":
        return AMOUNT * (-SL_PRICE * LEV - FEE) / 100
    return AMOUNT * (t["open_pnl"] - FEE) / 100


def fmt_t(ms):
    return datetime.fromtimestamp(ms / 1000, IST).strftime("%d %b %H:%M")


def main():
    sym = (sys.argv[1] if len(sys.argv) > 1 else "BTCUSDT").upper()
    days = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    now = int(time.time() * 1000)
    today0 = now // 86_400_000 * 86_400_000                 # 00:00 UTC = 5:30 AM IST
    start = today0 - (days - 1) * 86_400_000
    interval = "1m"          # 1-minute candles always (small steps need them)
    bars = klines(sym, interval, start, now)
    if not bars:
        tg(f"🔬 {sym}: no data.")
        return 1

    if SIDE == 1:
        header = [f"🔬 <b>{sym} +{STEP:g}% bounce/ladder test — BUY only</b>",
                  f"From the 5:30 AM open: a new LONG every time the price rises +{STEP:g}% from its lowest point "
                  f"since the last trade (also after a fall). No limit per day. "
                  f"${AMOUNT:g} each, {LEV}x. Stop-loss {SL_PRICE * LEV:g}% (price -{SL_PRICE:g}%), "
                  f"target {TP_PRICE * LEV:g}% (price +{TP_PRICE:g}%). Fees included. No SELL trades are ever taken.", ""]
    else:
        header = [f"🔬 <b>{sym} -{STEP:g}% drop/ladder test — SELL only</b>",
                  f"From the 5:30 AM open: a new SHORT every time the price falls -{STEP:g}% from its highest point "
                  f"since the last trade (also after a rise). No limit per day. "
                  f"${AMOUNT:g} each, {LEV}x. Stop-loss {SL_PRICE * LEV:g}% (price +{SL_PRICE:g}%), "
                  f"target {TP_PRICE * LEV:g}% (price -{TP_PRICE:g}%). Fees included. No BUY trades are ever taken.", ""]
    all_tr, lines = [], []
    for d in range(days):
        ds = start + d * 86_400_000
        tr, op = run_day(bars, ds, ds + 86_400_000)
        if op is None:
            continue
        all_tr += tr
        if days == 1:
            lines.append(f"5:30 AM open price: <b>{op:,.2f}</b>   now: {bars[-1][4]:,.2f} "
                         f"({(bars[-1][4] / op - 1) * 100:+.2f}%)")
            hi, lo = max(b[2] for b in bars), min(b[3] for b in bars)
            lines.append(f"Highest today: {hi:,.2f} ({(hi / op - 1) * 100:+.2f}%)   "
                         f"lowest: {lo:,.2f} ({(lo / op - 1) * 100:+.2f}%)")
            lines.append("")
            if not tr:
                lines.append(f"Price never moved {STEP:g}% → <b>no trade today</b>.")
            for t in tr:
                mark = {"target": "✅ target", "stop": "❌ stop-loss", "open": "⏳ still open"}[t["res"]]
                extra = f" at {fmt_t(t['out_t'])}" if "out_t" in t else f" ({t['open_pnl']:+.1f}% now)"
                lines.append(f"#{t['k']} · {'up' if SIDE == 1 else 'down'} {STEP:g}% from {t['low']:,.2f} → entry {t['entry']:,.2f} at {fmt_t(t['t'])} → {mark}{extra} "
                             f"· {pnl(t):+.2f} $")
        else:
            net = sum(pnl(t) for t in tr)
            lines.append(f"{datetime.fromtimestamp(ds / 1000, IST).strftime('%d %b')}: "
                         f"{len(tr)} trades, {sum(1 for t in tr if t['res'] == 'target')} ✅ "
                         f"{sum(1 for t in tr if t['res'] == 'stop')} ❌ → {net:+.2f} $")
    n = len(all_tr)
    w = sum(1 for t in all_tr if t["res"] == "target")
    s = sum(1 for t in all_tr if t["res"] == "stop")
    o = n - w - s
    net = sum(pnl(t) for t in all_tr)
    summary = ["", f"<b>Total{'' if days == 1 else f' ({days} days)'}</b>: {n} trades · ✅ {w} · ❌ {s} · ⏳ {o}",
               f"Result: <b>{net:+.2f} $</b> (each trade ${AMOUNT:g}; +{AMOUNT * (TP_PRICE * LEV - FEE) / 100:.2f} $ "
               f"per win, {AMOUNT * (-SL_PRICE * LEV - FEE) / 100:.2f} $ per loss)",
               "", f"Test only — no trades placed. {interval} candles."]
    tg("\n".join(header + lines + summary))


if __name__ == "__main__":
    sys.exit(main())
