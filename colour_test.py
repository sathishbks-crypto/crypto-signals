#!/usr/bin/env python3
"""
Colour-change test (your time-sheet rule) — Binance FUTURES data only.

YOUR RULE (from the time sheet)
  Every day starts fresh at 5:30 AM IST. For each 15-min candle:
     running average = average of today's closes so far
     % row           = 1 - average / close      (same as your Excel formula)
     colour          = GREEN if % > 0, RED if % < 0
  Only the FIRST colour change of the day counts, for every coin:
     RED  -> GREEN : LONG  at the start of the next candle
     GREEN -> RED  : SHORT at the start of the next candle
  After that trade (or if the first change is skipped), that coin is done
  for the day — later colour changes are ignored.
  (e.g. BTC red at 5:45, 6:00, 6:15, green at 6:30 -> LONG when 6:45 starts)

MONEY
  Start wallet $1000. Every trade: $100 at 10x (position $1000).
  $100 margin per open trade -> at most 10 trades open at the same time
  (no free money = signal skipped). Fees + slippage 0.15% of the position
  (about $1.50 per trade). Funding fees not included.

TARGET 2% / STOP-LOSS 1% — tested both ways, side by side:
  A) 2% / 1% of the TRADE amount ($100): target +$2, stop -$1.
     At 10x that is a price move of only 0.2% / 0.1%.
  B) 2% / 1% of the COIN PRICE: at 10x = +20% ($20) / -10% ($10) of the trade.
  Stop first if both are touched in the same candle.
  Neither touched -> closed at 5:30 AM (the colours start again).

Coins: the same 40 as the time sheet (your list + today's top 10 gainers
and top 10 losers).

Usage (on the server, in the bot folder):
  python3 colour_test.py          -> last 30 days
  python3 colour_test.py 7        -> last 7 days (up to 60)
"""

import sys
import time
from datetime import datetime

import scanner as S
import smc_today as T

START = 1000.0
MARGIN = 100.0
LEV = 10.0
TARGET_PCT, STOP_PCT = 2.0, 1.0
COST_SIDE = 0.075 / 100            # half of 0.15% on every buy/sell
SLOT_MS = 15 * 60 * 1000
SLOTS = 96


class Book:
    def __init__(self, name, price_target, price_stop):
        self.name = name
        self.price_target, self.price_stop = price_target, price_stop   # as price fractions
        self.bal = START
        self.open = {}             # sym -> position
        self.trades = []
        self.skipped = 0
        self.peak, self.max_dd = START, 0.0
        self.daily = []

    def free(self):
        return self.bal - MARGIN * len(self.open)

    def enter(self, sym, side, price, ms):
        if self.free() < MARGIN:
            self.skipped += 1
            return
        qty = MARGIN * LEV / price
        fee = qty * price * COST_SIDE
        self.bal -= fee
        d = 1 if side == "LONG" else -1
        self.open[sym] = {"sym": sym, "side": side, "entry": price, "qty": qty, "ms": ms,
                          "pnl": -fee,
                          "stop": price * (1 - d * self.price_stop),
                          "target": price * (1 + d * self.price_target)}
        self.mark()

    def close(self, sym, price, ms, why):
        p = self.open.pop(sym)
        d = 1 if p["side"] == "LONG" else -1
        usd = p["qty"] * (price - p["entry"]) * d - p["qty"] * price * COST_SIDE
        p["pnl"] += usd
        self.bal += usd
        p.update(exit=price, exit_ms=ms, why=why)
        self.trades.append(p)
        self.mark()

    def mark(self):
        self.peak = max(self.peak, self.bal)
        self.max_dd = max(self.max_dd, (self.peak - self.bal) / self.peak * 100)

    def candle(self, sym, h, l, t):
        """Stop-loss / target inside the candle. Stop first if both are touched."""
        p = self.open.get(sym)
        if not p:
            return
        long_ = p["side"] == "LONG"
        if (l <= p["stop"]) if long_ else (h >= p["stop"]):
            self.close(sym, p["stop"], t, "stop")
        elif (h >= p["target"]) if long_ else (l <= p["target"]):
            self.close(sym, p["target"], t, "target")


def load(mkt, sym, start_ms):
    k = T.fetch(mkt, sym, "15m", start_ms)
    return {t: (o, h, l, c) for t, o, h, l, c in zip(k["t"], k["o"], k["h"], k["l"], k["c"])}


def main():
    nums = [int(a) for a in sys.argv[1:] if a.isdigit()]
    days = min(max(nums[0] if nums else 30, 1), 60)
    try:
        mkt = S.Market()
    except Exception as e:
        S.telegram_send(f"⚠️ Colour test did not run: {e}")
        print(e)
        return 1
    now_ms = int(time.time() * 1000)
    first_day = (now_ms // T.DAY_MS - days + 1) * T.DAY_MS
    coins = list(S.build_watchlist(mkt))
    print(f"[colour] {len(coins)} coins, last {days} days", flush=True)
    data = {}
    for sym in coins:
        try:
            data[sym] = load(mkt, sym, first_day)
        except Exception as e:
            print(f"[colour] {sym}: {e}")
    books = {"A": Book("A) 2% / 1% of the trade amount (price 0.2% / 0.1%)",
                       TARGET_PCT / 100 / LEV, STOP_PCT / 100 / LEV),
             "B": Book("B) 2% / 1% of the coin price (trade +20% / -10%)",
                       TARGET_PCT / 100, STOP_PCT / 100)}
    signals = 0
    for d in range(days):
        day = first_day + d * T.DAY_MS
        state = {s: {"sum": 0.0, "n": 0, "col": 0, "done": False} for s in data}
        last_close = {}
        for k in range(SLOTS):
            t = day + k * SLOT_MS
            if t + SLOT_MS > now_ms:
                break
            for sym, kl in data.items():
                bar = kl.get(t)
                if not bar:
                    continue
                o, h, l, c = bar
                last_close[sym] = c
                for bk in books.values():
                    bk.candle(sym, h, l, t + SLOT_MS)
                st = state[sym]
                st["sum"] += c
                st["n"] += 1
                if st["n"] < 2:                       # 5:30 column has no % (like your sheet)
                    continue
                pct = 1 - (st["sum"] / st["n"]) / c
                col = 1 if pct > 0 else -1 if pct < 0 else 0
                if col == 0:
                    continue
                prev, st["col"] = st["col"], col
                if st["done"] or prev == 0 or col == prev or k == SLOTS - 1:
                    continue
                st["done"] = True                     # only the FIRST colour change of the day
                signals += 1
                side = "LONG" if col == 1 else "SHORT"
                ms = t + SLOT_MS                      # next candle starts = this close
                for bk in books.values():
                    bk.enter(sym, side, c, ms)
        end_ms = day + T.DAY_MS                       # 5:30 AM: close everything
        if end_ms <= now_ms:
            for bk in books.values():
                for sym in list(bk.open):
                    bk.close(sym, last_close.get(sym, bk.open[sym]["entry"]), end_ms, "5:30 AM")
        for bk in books.values():
            bk.daily.append((day, bk.bal))
    for bk in books.values():                          # today: still open -> value now
        bk.open_value = 0.0
        for sym, p in bk.open.items():
            last = max(t2 for t2 in data[sym] if t2 + SLOT_MS <= now_ms)
            dd = 1 if p["side"] == "LONG" else -1
            bk.open_value += p["qty"] * (data[sym][last][3] - p["entry"]) * dd
    msg = report(books, days, first_day, len(data), signals)
    print(msg.replace("<b>", "").replace("</b>", ""))
    chunk = ""
    for block in msg.split("\n\n"):
        if len(chunk) + len(block) + 2 > 3800 and chunk:
            S.telegram_send(chunk)
            chunk = ""
        chunk += ("\n\n" if chunk else "") + block
    if chunk:
        S.telegram_send(chunk)
    return 0


def report(books, days, first_day, ncoins, signals):
    ist = lambda ms: datetime.fromtimestamp(ms / 1000, S.IST)
    lines = [f"🎨 <b>Colour-change test — last {days} days, {ncoins} coins (Binance futures)</b>",
             f"From {ist(first_day):%d %b %I:%M %p} IST. Red→green = LONG, green→red = SHORT at the "
             "next candle — only the FIRST colour change of the day per coin. Start $1000, "
             "$100 per trade at 10x (max 10 open). Target 2%, stop 1%. Not hit → closed 5:30 AM. "
             "Fees + slippage included (about $1.50 per trade).",
             f"First colour changes found: {signals} (one per coin per day)", ""]
    for bk in books.values():
        tr = bk.trades
        if not tr:
            lines += [f"<b>{bk.name}</b>", "No trades", ""]
            continue
        wins = sum(1 for p in tr if p["pnl"] > 0)
        why = {w: sum(1 for p in tr if p["why"] == w) for w in ("target", "stop", "5:30 AM")}
        by_coin = {}
        for p in tr:
            by_coin[p["sym"]] = by_coin.get(p["sym"], 0.0) + p["pnl"]
        best = sorted(by_coin.items(), key=lambda x: x[1], reverse=True)
        lines += [
            f"<b>{bk.name}</b>",
            f"Trades {len(tr)} — ✅ {wins} profit, ❌ {len(tr) - wins} loss ({wins / len(tr) * 100:.0f}% win)",
            "Closed by: " + ", ".join(f"{w} {n}" for w, n in why.items() if n)
            + f" | skipped (no free money): {bk.skipped}",
            f"LONG {sum(1 for p in tr if p['side'] == 'LONG')} / SHORT {sum(1 for p in tr if p['side'] == 'SHORT')}",
            f"Average per trade: {sum(p['pnl'] for p in tr) / len(tr):+.2f}$ | "
            f"fees paid about ${sum(p['qty'] * p['entry'] * COST_SIDE * 2 for p in tr):.0f}",
            f"Final wallet: <b>${bk.bal:.2f}</b> ({(bk.bal / START - 1) * 100:+.1f}%)"
            + (f" | open now {bk.open_value:+.2f}$" if bk.open else ""),
            f"Biggest fall from a high: -{bk.max_dd:.1f}%",
            "Best coins: " + ", ".join(f"{s[:-4]} {v:+.0f}$" for s, v in best[:3]),
            "Worst coins: " + ", ".join(f"{s[:-4]} {v:+.0f}$" for s, v in best[-3:][::-1]),
            "",
        ]
    lines.append("<b>Wallet at the end of each day (A | B)</b>")
    for (day, a), (_, b) in zip(books["A"].daily, books["B"].daily):
        lines.append(f"{ist(day):%d %b}: ${a:.0f} | ${b:.0f}")
    lines += ["", "Note: the 40 coins are TODAY's list (incl. today's top gainers/losers), "
              "so older days may look better or worse than real. Past results do not guarantee "
              "future results."]
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())
