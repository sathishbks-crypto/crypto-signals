#!/usr/bin/env python3
"""
Watch-then-trade test on your 1-second time sheet — one day.

YOUR RULE
  The sheet's % row, worked out every second from 5:30:00 AM IST
  (Binance SPOT 1-second closes — futures has no 1-second candles):
       % = 1 - (average of today's closes so far) / close
  1) Wait for the FIRST colour change of the day (green<->red). Do NOT trade yet.
  2) From then on, WATCH the % row:
        % reaches +0.5%  -> LONG
        % reaches -0.5%  -> SHORT
     ONE mode (default): only one trade per coin per day.
     MULTI mode: after a trade closes, the % row must come back inside
     -0.5%..+0.5%, then the next time it reaches +/-0.5% a new trade is taken
     (same coin, as many times as it happens). The first colour change is
     needed only once, at the start of the day.
  3) Target 0.5%, stop-loss 0.25% of the coin price  (1 : 2)
     At 10x: target = +5% of the trade (+$5), stop = -2.5% (-$2.50).
     Stop first if both are touched in the same second.
     Neither touched -> closed at 5:30 AM next morning (or shown "open" today).

  Prices: the trade is priced on FUTURES = spot price x (futures/spot gap at
  the entry minute). Exits are checked second by second.
  Money: start $1000, $100 per trade at 10x (position $1000), at most 10 open.
  Fees + slippage 0.15% of the position = about $1.50 per trade (in + out).

Usage (on the server, in the bot folder):
  python3 watch_test.py          -> today, from 5:30 AM IST until now, one trade per coin
  python3 watch_test.py multi    -> today, many trades per coin (re-enter after each close)
  python3 watch_test.py 0.5 0.25 -> other trigger / target and stop (target = trigger here)
"""

import sys
import time
from datetime import datetime

import fill_sheet as F
import scanner as S

TRIGGER = 0.005                 # % row must reach +/-0.5%
TARGET = 0.005                  # +0.5% price
STOP = 0.0025                   # -0.25% price
START, MARGIN, LEV = 1000.0, 100.0, 10.0
MAX_OPEN = 10
COST = 0.0015                   # fees + slippage, of the position
DAY_MS = 86400 * 1000


class Walker:
    """Runs your rule second by second. Can be fed more seconds later (fast)."""
    def __init__(self, multi=False):
        self.multi = multi
        self.k, self.total, self.col, self.last = 0, 0.0, 0, None
        self.changed, self.entry = None, None
        self.armed = False                            # True = watching for +/-0.5%
        self.trades = []
        self.finished = False                         # one-trade mode: done for the day

    def feed(self, closes):
        top = max(closes) + 1 if closes else 0
        while self.k < top and not self.finished:
            c = closes.get(self.k, self.last)          # quiet second: previous close
            k = self.k
            self.k += 1
            if c is None:
                continue
            self.last = c
            self.total += c                           # the average always keeps running
            if k == 0:
                continue
            pct = 1 - (self.total / (k + 1)) / c
            if self.entry is not None:                # trade open: stop / target
                e, side = self.entry["price"], self.entry["side"]
                move = (c - e) / e * side
                if move <= -STOP or move >= TARGET:   # stop first
                    hit = "stop" if move <= -STOP else "target"
                    self.trades.append({**self.entry, "exit_k": k, "why": hit,
                                        "move": -STOP if hit == "stop" else TARGET})
                    self.entry = None
                    self.armed = False                # must come back inside the band first
                    if not self.multi:
                        self.finished = True
                continue
            new = 1 if pct > 0 else -1 if pct < 0 else 0
            if self.changed is None:
                if new and self.col and new != self.col:
                    self.changed = k                  # first colour change: start watching
                    self.armed = abs(pct) < TRIGGER
                if new:
                    self.col = new
                continue
            if not self.armed:
                if abs(pct) < TRIGGER:
                    self.armed = True
                continue
            if pct >= TRIGGER or pct <= -TRIGGER:
                self.entry = {"k": k, "side": 1 if pct > 0 else -1, "price": c, "pct": pct}
        return self.finished

    def final(self):
        out = list(self.trades)
        if self.entry:
            e = self.entry["price"]
            out.append({**self.entry, "exit_k": self.k - 1, "why": "open",
                        "move": (self.last - e) / e * self.entry["side"]})
        status = ("traded" if out else
                  "no trigger" if self.changed is not None else "no colour change")
        return out, status


def futures_gap(mkt, sym, minute_ms, spot_price):
    """futures price / spot price at the start of that minute."""
    try:
        b = mkt._raw(sym, "1m", 1, start_time=minute_ms)
        if b and spot_price:
            return float(b[0][1]) / spot_price
    except Exception as e:
        print(f"[watch] {sym} futures price: {e}")
    return 1.0


def main():
    global TRIGGER, TARGET, STOP
    nums = [float(a) for a in sys.argv[1:] if a.replace(".", "", 1).isdigit()]
    if len(nums) >= 2:
        TRIGGER, TARGET, STOP = nums[0] / 100, nums[0] / 100, nums[1] / 100
    try:
        mkt = S.Market()
    except Exception as e:
        S.telegram_send(f"⚠️ Watch test did not run: {e}")
        print(e)
        return 1
    now_ms = int(time.time() * 1000)
    day = now_ms // DAY_MS * DAY_MS                    # today 00:00 UTC = 5:30 AM IST
    multi = any(a.lower() == "multi" for a in sys.argv[1:])
    coins = list(S.build_watchlist(mkt))
    trades, status, no_spot = [], {}, []
    for n, sym in enumerate(coins, 1):
        m = F.spot_symbol(sym)
        if not m:
            no_spot.append(sym)
            continue
        w = Walker(multi)
        try:
            closes = F.spot_1s(m[0], m[1], day, 86400, now_ms, stop_early=w.feed)
        except Exception as e:
            print(f"[watch] {sym}: {e}")
            continue
        w.feed(closes)
        got, status[sym] = w.final()
        for r in got:
            minute = (day + r["k"] * 1000) // 60000 * 60000
            gap = futures_gap(mkt, sym, minute, closes.get((minute - day) // 1000))
            r.update(sym=sym, fut_price=r["price"] * gap,
                     entry_ms=day + (r["k"] + 1) * 1000, exit_ms=day + (r["exit_k"] + 1) * 1000)
        trades += got
        print(f"[watch] {n}/{len(coins)} {sym}: {len(got)} trade(s)", flush=True)

    # money: take trades in time order, at most 10 open
    trades.sort(key=lambda r: r["entry_ms"])
    bal, open_until, taken = START, [], []
    for r in trades:
        open_until = [t for t in open_until if t > r["entry_ms"]]
        if len(open_until) >= MAX_OPEN or bal < MARGIN:
            r["skipped"] = True
            continue
        pos = MARGIN * LEV
        r["usd"] = pos * r["move"] - pos * COST
        r["skipped"] = False
        if r["why"] != "open":
            bal += r["usd"]
            open_until.append(r["exit_ms"])
        else:
            open_until.append(now_ms + 1)
        taken.append(r)
    msg = report(status, taken, trades, bal, day, no_spot, multi)
    print(msg.replace("<b>", "").replace("</b>", ""))
    chunk = ""
    for line in msg.split("\n"):
        if len(chunk) + len(line) + 1 > 3800 and chunk:
            S.telegram_send(chunk)
            chunk = ""
        chunk += ("\n" if chunk else "") + line
    if chunk:
        S.telegram_send(chunk)
    return 0


def report(status, taken, trades, bal, day, no_spot, multi=False):
    ist = lambda ms: datetime.fromtimestamp(ms / 1000, S.IST)
    tp, sl, tr = TARGET * 100, STOP * 100, TRIGGER * 100
    lines = [f"👀 <b>Watch-then-trade test ({'MANY trades' if multi else 'ONE trade'} per coin) — "
             f"today {ist(day):%d %b}, {len(status)} coins</b>",
             f"5:30 AM → {datetime.now(S.IST):%I:%M %p} IST. Colours from SPOT 1-second candles, "
             "trades priced on FUTURES.",
             f"Rule: after the first colour change, wait until the % row reaches +{tr:.2f}% (LONG) "
             f"or -{tr:.2f}% (SHORT). Target +{tp:.2f}% / stop -{sl:.2f}% price (1:2) = "
             f"+{tp * LEV:.1f}% / -{sl * LEV:.1f}% of a $100 trade at 10x. "
             "Fees + slippage about $1.50 per trade."
             + (" After each close the % row must come back inside ±"
                f"{tr:.2f}% before the next trade on that coin." if multi else ""), ""]
    show = taken if len(taken) <= 60 else taken[:20]
    for n, r in enumerate(show, 1):
        icon = {"target": "✅", "stop": "❌", "open": "⏳"}[r["why"]]
        end = ("still open" if r["why"] == "open" else f"{r['why']} {ist(r['exit_ms']):%I:%M:%S %p}")
        lines.append(f"{icon} {n}. {'LONG' if r['side'] == 1 else 'SHORT'} {r['sym'][:-4]} "
                     f"{ist(r['entry_ms']):%I:%M:%S %p} @ {S.fmt_price(r['fut_price'])} "
                     f"(% row {r['pct'] * 100:+.2f}%) → {end} <b>{r['usd']:+.2f}$</b>")
    if len(show) < len(taken):
        lines.append(f"… {len(taken) - len(show)} more trades (only the first 20 are listed)")
    closed = [r for r in taken if r["why"] != "open"]
    wins = sum(1 for r in closed if r["why"] == "target")
    open_ = [r for r in taken if r["why"] == "open"]
    waiting = sum(1 for v in status.values() if v == "no trigger")
    no_change = sum(1 for v in status.values() if v == "no colour change")
    lines += [
        "",
        f"<b>Summary:</b> {len(taken)} trades — ✅ {wins} target, ❌ {len(closed) - wins} stop, "
        f"⏳ {len(open_)} still open"
        + (f" | skipped (10 already open): {sum(1 for r in trades if r.get('skipped'))}"
           if any(r.get("skipped") for r in trades) else ""),
        f"Coins with no trade (colour changed, % never reached ±{tr:.2f}%): {waiting}"
        + (f" | no colour change yet: {no_change}" if no_change else ""),
        f"Wallet: $1000 → <b>${bal:.2f}</b> ({(bal / START - 1) * 100:+.2f}%) with closed trades"
        + (f"; open trades now {sum(r['usd'] for r in open_):+.2f}$" if open_ else ""),
        "Note: with $1.50 fees, a win nets about +$3.50 and a loss about -$4.00, "
        "so more than 53% of trades must win just to break even. One day is not enough to judge.",
    ]
    if multi and taken:
        by = {}
        for r in closed:
            by[r["sym"]] = by.get(r["sym"], 0.0) + r["usd"]
        best = sorted(by.items(), key=lambda x: x[1], reverse=True)
        cnt = {}
        for r in taken:
            cnt[r["sym"]] = cnt.get(r["sym"], 0) + 1
        lines.append("Best coins: " + ", ".join(f"{k[:-4]} {v:+.1f}$ ({cnt[k]})" for k, v in best[:5]))
        lines.append("Worst coins: " + ", ".join(f"{k[:-4]} {v:+.1f}$ ({cnt[k]})" for k, v in best[-5:][::-1]))
    if no_spot:
        lines.append("No spot pair (not tested): " + ", ".join(s[:-4] for s in no_spot))
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())
