#!/usr/bin/env python3
"""
TEST ONLY - places no trade.

Idea (BUY only, never SELL):
  The daily candle opens at 5:30 AM India time (00:00 UTC). Take that open price.
  Every time the price reaches another +1% above it (+1%, +2%, +3% ...),
  open a new LONG at that level (each level once per day, several trades can
  be open together).
  Each trade: $10, 5x leverage.
     stop-loss = 5% of the trade amount  -> price 1% below the entry
     target    = 10% of the trade amount -> price 2% above the entry   (1:2)
  If the price only falls, no trade is taken at all.

Run on the server (bot folder):
    python3 btc_levels.py                  today, BTCUSDT
    python3 btc_levels.py BTCUSDT 30       each of the last 30 days + total
    python3 btc_levels.py ETHUSDT          today, ETH
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

STEP = 1.0          # % between levels
SL_PRICE = 1.0      # % price  (= 5% of the amount at 5x)
TP_PRICE = 2.0      # % price  (= 10% of the amount at 5x)
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
    next_k = 1
    for i, b in enumerate(bars):
        if b[0] >= day_end:
            break
        if b[0] < day_start:
            continue
        # new levels reached in this candle
        while b[2] >= open_px * (1 + next_k * STEP / 100):
            lvl = open_px * (1 + next_k * STEP / 100)
            trades.append({"k": next_k, "entry": lvl, "t": b[0], "i": i, "res": None})
            next_k += 1
    for t in trades:
        tp, sl = t["entry"] * (1 + TP_PRICE / 100), t["entry"] * (1 - SL_PRICE / 100)
        for b in bars[t["i"] + 1:]:
            if b[3] <= sl:                       # stop first if both in one candle (safe side)
                t["res"], t["out_t"] = "stop", b[0]
                break
            if b[2] >= tp:
                t["res"], t["out_t"] = "target", b[0]
                break
        if t["res"] is None:
            last = bars[-1][4]
            t["res"], t["open_pnl"] = "open", (last / t["entry"] - 1) * 100 * LEV
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
    interval = "1m" if days <= 3 else "5m"
    bars = klines(sym, interval, start, now)
    if not bars:
        tg(f"🔬 {sym}: no data.")
        return 1

    header = [f"🔬 <b>{sym} +1% ladder test — BUY only</b>",
              f"From the 5:30 AM open, a new LONG at every +{STEP:g}% (once per level per day). "
              f"${AMOUNT:g} each, {LEV}x. Stop-loss 5% (price -{SL_PRICE:g}%), target 10% (price +{TP_PRICE:g}%). "
              f"Fees included. No SELL trades are ever taken.", ""]
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
            hi = max(b[2] for b in bars)
            lines.append(f"Highest today: {hi:,.2f} ({(hi / op - 1) * 100:+.2f}%)")
            lines.append("")
            if not tr:
                lines.append("Price never went +1% above the open → <b>no trade today</b>.")
            for t in tr:
                mark = {"target": "✅ target", "stop": "❌ stop-loss", "open": "⏳ still open"}[t["res"]]
                extra = f" at {fmt_t(t['out_t'])}" if "out_t" in t else f" ({t['open_pnl']:+.1f}% now)"
                lines.append(f"Level +{t['k']}% · entry {t['entry']:,.2f} at {fmt_t(t['t'])} → {mark}{extra} "
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
