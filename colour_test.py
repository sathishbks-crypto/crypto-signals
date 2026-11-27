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

CANDLE SIZE (2nd word): 15m (default), 5m, 1m or 1s.
  The % row and colours are worked out on that candle size, same formula.
  1s: Binance FUTURES has no 1-second candles, so the COLOURS come from
      Binance SPOT 1-second candles (same coin on spot), but the TRADE is
      priced on FUTURES: entry = spot price x (futures/spot price gap at that
      minute), then stop/target are checked on spot seconds for the rest of
      that minute and on futures 1-minute candles after that.

Usage (on the server, in the bot folder):
  python3 colour_test.py              -> last 30 days, 15-min candles
  python3 colour_test.py 30 1m        -> last 30 days, 1-min candles (about 10 minutes to download)
  python3 colour_test.py 7 5m         -> last 7 days, 5-min candles (up to 60 days)
  python3 colour_test.py 30 1s        -> last 30 days, colours from SPOT 1-second candles
"""

import array
import sys
import time
from datetime import datetime

import fill_sheet as F
import scanner as S
import smc_today as T

START = 1000.0
MARGIN = 100.0
LEV = 10.0
TARGET_PCT, STOP_PCT = 2.0, 1.0
COST_SIDE = 0.075 / 100            # half of 0.15% on every buy/sell
TF_MIN = {"1m": 1, "3m": 3, "5m": 5, "15m": 15}
SEC_LIMIT = F.SEC_SLOTS            # 1s mode: look for the first colour change until 10:02:59 AM
TF = "15m"
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


def first_change(closes):
    """First colour change in {index: close} (index 0 = 5:30). Returns (index, side) or None."""
    total, col = 0.0, 0
    for k in range(max(closes) + 1 if closes else 0):
        c = closes.get(k)
        if c is None:
            return None
        total += c
        if k == 0:
            continue
        pct = 1 - (total / (k + 1)) / c
        new = 1 if pct > 0 else -1 if pct < 0 else 0
        if new == 0:
            continue
        if col and new != col:
            return k, "LONG" if new == 1 else "SHORT"
        col = new
    return None


def signals_1s(sym, day, now_ms, fut):
    """Spot 1-second colours -> first change -> futures entry. None if no signal."""
    m = F.spot_symbol(sym)
    if not m:
        return None

    def enough(out):
        fc = first_change(out)
        return fc is not None and max(out) >= fc[0] + 61
    closes = F.spot_1s(m[0], m[1], day, SEC_LIMIT, now_ms, stop_early=enough)
    fc = first_change(closes)
    if not fc:
        return None
    k, side = fc
    entry_ms = day + (k + 1) * 1000                   # the next second starts
    minute = entry_ms // 60000 * 60000
    bar = fut.get(minute)
    spot_at_minute = closes.get((minute - day) // 1000)
    if not bar or not spot_at_minute:
        return None
    ratio = bar[3] / spot_at_minute if len(bar) > 3 else 1.0
    seconds_left = [closes[j] * ratio for j in range(k + 1, (minute + 60000 - day) // 1000)
                    if j in closes]
    return {"ms": entry_ms, "minute": minute, "side": side, "price": closes[k] * ratio,
            "rest": seconds_left, "wait": (entry_ms - day) / 60000}


class Bars:
    """Candles of one coin stored by position (small memory even for 1-min candles)."""
    def __init__(self, start_ms, n):
        self.start = start_ms
        self.o, self.h, self.l, self.c = (array.array("d", [float("nan")]) * n for _ in range(4))

    def get(self, t):
        i = (t - self.start) // SLOT_MS
        if 0 <= i < len(self.c) and self.c[i] == self.c[i]:          # not empty
            return self.h[i], self.l[i], self.c[i], self.o[i]
        return None


def load(mkt, sym, start_ms, now_ms):
    n = (now_ms - start_ms) // SLOT_MS + 1
    bars = Bars(start_ms, n)
    st = start_ms
    while st < now_ms:
        T.THROTTLE.wait(T.kline_weight(1000))
        rows = mkt._raw(sym, TF, 1000, start_time=st)
        if not rows:
            break
        for r in rows:
            t, tc = int(r[0]), int(r[6])
            i = (t - start_ms) // SLOT_MS
            if tc < now_ms and 0 <= i < n:
                bars.o[i], bars.h[i], bars.l[i], bars.c[i] = (float(r[1]), float(r[2]),
                                                              float(r[3]), float(r[4]))
        if len(rows) < 1000:
            break
        st = int(rows[-1][0]) + 1
    return bars


def main():
    global TF, SLOT_MS, SLOTS
    nums = [int(a) for a in sys.argv[1:] if a.isdigit()]
    days = min(max(nums[0] if nums else 30, 1), 60)
    tf = next((a.lower() for a in sys.argv[1:] if a.lower() in TF_MIN or a.lower() == "1s"), "15m")
    one_sec = tf == "1s"
    TF = "1m" if one_sec else tf                      # 1s mode: futures 1-min candles for the exits
    SLOT_MS, SLOTS = TF_MIN[TF] * 60000, 1440 // TF_MIN[TF]
    try:
        mkt = S.Market()
    except Exception as e:
        S.telegram_send(f"⚠️ Colour test did not run: {e}")
        print(e)
        return 1
    now_ms = int(time.time() * 1000)
    first_day = (now_ms // T.DAY_MS - days + 1) * T.DAY_MS
    coins = list(S.build_watchlist(mkt))
    pages = (days * SLOTS) // 1000 + 1
    print(f"[colour] {len(coins)} coins, last {days} days, {tf} candles — about "
          f"{len(coins) * pages * 5 / T.MAX_WEIGHT_PER_MIN + 0.5:.0f} min to download", flush=True)
    data = {}
    for n, sym in enumerate(coins, 1):
        try:
            data[sym] = load(mkt, sym, first_day, now_ms)
        except Exception as e:
            print(f"[colour] {sym}: {e}")
        if n % 10 == 0:
            print(f"[colour] {n}/{len(coins)} coins downloaded", flush=True)
    books = {"A": Book("A) 2% / 1% of the trade amount (price 0.2% / 0.1%)",
                       TARGET_PCT / 100 / LEV, STOP_PCT / 100 / LEV),
             "B": Book("B) 2% / 1% of the coin price (trade +20% / -10%)",
                       TARGET_PCT / 100, STOP_PCT / 100)}
    signals = 0
    wait_min = []
    no_spot = set()
    for d in range(days):
        day = first_day + d * T.DAY_MS
        state = {s: {"sum": 0.0, "n": 0, "col": 0, "done": False} for s in data}
        last_close = {}
        sig = {}
        if one_sec:
            for sym in data:
                try:
                    if not F.spot_symbol(sym):
                        no_spot.add(sym)
                        continue
                    x = signals_1s(sym, day, now_ms, data[sym])
                except Exception as e:
                    print(f"[colour] {sym} 1s: {e}")
                    continue
                if x:
                    sig[sym] = x
            print(f"[colour] day {d + 1}/{days}: {len(sig)} first colour changes", flush=True)
        for k in range(SLOTS):
            t = day + k * SLOT_MS
            if t + SLOT_MS > now_ms:
                break
            for sym, kl in data.items():
                bar = kl.get(t)
                if not bar:
                    continue
                h, l, c = bar[:3]
                last_close[sym] = c
                x = sig.get(sym)
                if x and x["minute"] == t:                # 1s mode: the entry minute
                    signals += 1
                    wait_min.append(x["wait"])
                    for bk in books.values():
                        bk.candle(sym, h, l, t + SLOT_MS)     # (no trade open yet on this coin)
                        bk.enter(sym, x["side"], x["price"], x["ms"])
                        for p in x["rest"]:                   # rest of that minute, second by second
                            bk.candle(sym, p, p, t + SLOT_MS)
                    continue
                for bk in books.values():
                    bk.candle(sym, h, l, t + SLOT_MS)
                if one_sec:
                    continue
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
                wait_min.append((ms - day) / 60000)
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
            dd = 1 if p["side"] == "LONG" else -1
            bk.open_value += p["qty"] * (last_close.get(sym, p["entry"]) - p["entry"]) * dd
    wait_min.sort()
    med = wait_min[len(wait_min) // 2] if wait_min else 0
    msg = report(books, days, first_day, len(data), signals, med, tf, sorted(no_spot))
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


def report(books, days, first_day, ncoins, signals, med_wait, tf="15m", no_spot=()):
    ist = lambda ms: datetime.fromtimestamp(ms / 1000, S.IST)
    src = ("colours from SPOT 1-second candles, trades on FUTURES" if tf == "1s"
           else "Binance futures")
    lines = [f"🎨 <b>Colour-change test — {tf} candles, last {days} days, {ncoins} coins "
             f"({src})</b>",
             f"From {ist(first_day):%d %b %I:%M %p} IST. Red→green = LONG, green→red = SHORT at the "
             "next candle — only the FIRST colour change of the day per coin. Start $1000, "
             "$100 per trade at 10x (max 10 open). Target 2%, stop 1%. Not hit → closed 5:30 AM. "
             "Fees + slippage included (about $1.50 per trade).",
             f"First colour changes found: {signals} (one per coin per day). "
             + (f"Half of them came within {med_wait * 60:.0f} seconds after 5:30 AM."
                if tf == "1s" else f"Half of them came within {med_wait:.0f} min after 5:30 AM.")
             + (f"\nNo spot pair (not tested): {', '.join(x[:-4] for x in no_spot)}" if no_spot else ""),
             ""]
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
