#!/usr/bin/env python3
"""
Live check (no trading): Binance USD-M futures coins that moved STEADILY
in one direction inside a time window.

  Steady riser : rose at least 1% and never fell 0.5% below its best price
                 so far inside the window; still near the top at the end.
  Steady faller: fell at least 1% and never bounced 0.5% above its lowest
                 price so far; still near the bottom at the end.

At 5x leverage a 0.5% price move = 2.5% of the trade amount.

Run on the server (bot folder):
    python3 steady_crypto.py                 last 60 minutes
    python3 steady_crypto.py 30              last 30 minutes
    python3 steady_crypto.py 16:00 17:00     today 4 PM to 5 PM (India time)
    python3 steady_crypto.py 16:00 17:00 0.8 same, but allow a 0.8% dip
The list goes to Telegram and the screen. Takes about 1-2 minutes.
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
MIN_MOVE = 1.0          # % the coin must have moved in the window
PULLBACK = 0.5          # % largest allowed move against the trend
NEAR_END = 0.3          # % last price must be this close to the window's extreme
MIN_VOL = 20_000_000    # 24h volume in USDT (skip thin coins)
LEV = 5
TOP = 8

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
    if token and chat:
        body = urllib.parse.urlencode({"chat_id": chat, "text": text[:4000], "parse_mode": "HTML"}).encode()
        try:
            urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", body, timeout=20)
        except Exception as e:
            print("[telegram]", e)


def get(path, **params):
    url = BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
    for a in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}),
                                        timeout=20) as r:
                return json.loads(r.read().decode())
        except Exception:
            if a == 2:
                raise
            time.sleep(1.5)


def window():
    """Returns (start_ms, end_ms, label) from the command line."""
    args = sys.argv[1:]
    global PULLBACK
    now = datetime.now(IST)
    if len(args) >= 2 and ":" in args[0]:
        h1, m1 = map(int, args[0].split(":"))
        h2, m2 = map(int, args[1].split(":"))
        s = now.replace(hour=h1, minute=m1, second=0, microsecond=0)
        e = now.replace(hour=h2, minute=m2, second=0, microsecond=0)
        if e > now:                                   # asked for a window that has not ended yet
            e = now.replace(second=0, microsecond=0)
        if len(args) >= 3:
            PULLBACK = float(args[2])
        return int(s.timestamp() * 1000), int(e.timestamp() * 1000), f"{args[0]}–{e.strftime('%H:%M')} IST"
    mins = int(args[0]) if args else 60
    if len(args) >= 2:
        PULLBACK = float(args[1])
    e = now.replace(second=0, microsecond=0)
    s = e - timedelta(minutes=mins)
    return int(s.timestamp() * 1000), int(e.timestamp() * 1000), f"last {mins} min ({s.strftime('%H:%M')}–{e.strftime('%H:%M')} IST)"


def check(kl):
    """kl: 1-minute klines inside the window. Returns ('UP'|'DOWN', move%, worst%, last) or None."""
    if len(kl) < 10:
        return None
    start = float(kl[0][1])
    hi = lo = start
    worst_up = worst_dn = 0.0
    for k in kl:
        h, l = float(k[2]), float(k[3])
        worst_up = max(worst_up, (hi - l) / hi * 100)      # dip below the best price so far
        worst_dn = max(worst_dn, (h - lo) / lo * 100)      # bounce above the lowest price so far
        hi, lo = max(hi, h), min(lo, l)
    last = float(kl[-1][4])
    move = (last / start - 1) * 100
    if move >= MIN_MOVE and worst_up < PULLBACK and (hi - last) / hi * 100 <= NEAR_END:
        return "UP", move, worst_up, last
    if -move >= MIN_MOVE and worst_dn < PULLBACK and (last - lo) / lo * 100 <= NEAR_END:
        return "DOWN", move, worst_dn, last
    return None


def main():
    s_ms, e_ms, label = window()
    info = get("/fapi/v1/exchangeInfo")
    live = {x["symbol"] for x in info["symbols"]
            if x.get("contractType") == "PERPETUAL" and x.get("quoteAsset") == "USDT" and x.get("status") == "TRADING"}
    vols = {t["symbol"]: float(t["quoteVolume"]) for t in get("/fapi/v1/ticker/24hr")}
    syms = sorted(s for s in live if vols.get(s, 0) >= MIN_VOL)
    print(f"Checking {len(syms)} coins for {label}...", flush=True)
    ups, downs = [], []
    for s in syms:
        try:
            kl = get("/fapi/v1/klines", symbol=s, interval="1m", startTime=s_ms, endTime=e_ms - 1, limit=1000)
        except Exception:
            continue
        r = check(kl)
        if r:
            (ups if r[0] == "UP" else downs).append((s, *r[1:]))
        time.sleep(0.05)
    ups.sort(key=lambda x: -x[1])
    downs.sort(key=lambda x: x[1])
    lines = [f"🪙 <b>Steady movers — {label}</b>",
             f"{len(syms)} Binance futures coins (24h volume ≥ ${MIN_VOL / 1e6:.0f}M). Moved at least {MIN_MOVE:g}% "
             f"and never went {PULLBACK:g}% the other way (= {PULLBACK * LEV:g}% of the amount at {LEV}x).", "",
             "📈 <b>Steady risers</b>"]
    lines += [f"• <b>{s}</b> {m:+.2f}% · biggest dip {w:.2f}% (={w * LEV:.1f}% at {LEV}x) · now {p:g}"
              for s, m, w, p in ups[:TOP]] or ["None in this window."]
    lines += ["", "📉 <b>Steady fallers</b>"]
    lines += [f"• <b>{s}</b> {m:+.2f}% · biggest bounce {w:.2f}% (={w * LEV:.1f}% at {LEV}x) · now {p:g}"
              for s, m, w, p in downs[:TOP]] or ["None in this window."]
    lines += ["", "Shows what already happened, not what comes next. Not a trade signal."]
    tg("\n".join(lines))


if __name__ == "__main__":
    main()
