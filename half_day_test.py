#!/usr/bin/env python3
"""
Half-day strategy switch test — Binance FUTURES 5-minute candles.

YOUR IDEA
  Split every day in two halves (IST) and test BOTH ways, as two separate reports:
     SEGMENT 1: watch MORNING 5:30 AM-5:30 PM  -> trade EVENING 5:30 PM-5:30 AM
     SEGMENT 2: watch EVENING 5:30 PM-5:30 AM  -> trade next MORNING 5:30 AM-5:30 PM
  While watching, ~11 strategies run only to be SCORED.
  Picking (your rule, "failing the same way"):
     A strategy is a "same-way failure" when it LOST in the watch half and most of
     its losses (60%+ of the lost amount) came from ONE side:
        - LONG trades that hit the stop   (it said "up", the market went down), or
        - SHORT trades that hit the stop  (it said "down", the market went up).
     The bigger group (LONG-failures or SHORT-failures) is used in the next half,
     but only if it has at least 3 strategies. Otherwise no trade in that half.

  To see if the idea really works, the same half is also traded 3 other ways:
     A) YOUR RULE  - the same-way failures, as they are
     B) the same group FLIPPED (long<->short)
     C) all the OTHER strategies (not in the group)
     D) all strategies every time (no picking at all)
  Tested over many days (default 30), so one lucky day cannot fool us.

THE STRATEGIES (all the ideas we tried, made to work on 5-minute candles)
   1 Colour change     first colour change of the half (your sheet), target 2% / stop 1%
   2 % row 0.5 (1:1)   % row crosses ±0.5% -> trade that way, 0.5% / 0.5%
   3 % row 0.5 (1:3)   same, target 1.5% / stop 0.5%
   4 % row 1.0 (1:1)   % row crosses ±1%, 1% / 1%
   5 EMA 9/21 cross    fast EMA crosses slow EMA, 1% / 0.5%
   6 RSI reversal      RSI(14) under 30 -> LONG, over 70 -> SHORT, 0.6% / 0.6%
   7 Band breakout     close outside Bollinger band (20,2) -> follow, 1% / 0.5%
   8 Band bounce       close outside Bollinger band -> go back the other way, 0.6% / 0.6%
   9 1-hour breakout   close above last hour's high / below its low, 1% / 0.5%
  10 Dip in trend      EMA50 rising and price dips to EMA20 -> LONG (mirror for SHORT), 1% / 0.5%
  11 Opening range     break of the half's first-hour high/low (once), 1% / 0.5%
  A trade closes only at target or stop; anything still open at the end of the
  half is closed there (the next half starts fresh). Stop first if both are
  touched in the same 5-minute candle.

MONEY (assumed)
  Start $1000. Every trade risks 1% of the balance (position = 1% / stop).
  At most 10 trades open at the same time (more = skipped, counted).
  Fees + slippage 0.15% of the position. Funding not included.

COINS
  Your favorites.txt list (today's gainers/losers left out on purpose).

Usage (on the server, in the bot folder):
  python3 half_day_test.py          -> last 30 days, both segments
  python3 half_day_test.py 7        -> last 7 days (up to 60)
"""

import array
import bisect
import sys
import time
from datetime import datetime

import scanner as S
import smc_today as T

TF, BAR_MS = "5m", 5 * 60 * 1000
DAY_MS, HALF_MS = 86400 * 1000, 43200 * 1000
START, RISK, COST, MAX_OPEN = 1000.0, 0.01, 0.0015, 10
MIN_GROUP = 3                                         # at least 3 strategies failing the same way
ONE_SIDE = 0.60                                       # 60%+ of a strategy's losses on one side

STRATS = [  # (name, target %, stop %)
    ("Colour change", 2.0, 1.0),
    ("% row 0.5 (1:1)", 0.5, 0.5),
    ("% row 0.5 (1:3)", 1.5, 0.5),
    ("% row 1.0 (1:1)", 1.0, 1.0),
    ("EMA 9/21 cross", 1.0, 0.5),
    ("RSI reversal", 0.6, 0.6),
    ("Band breakout", 1.0, 0.5),
    ("Band bounce", 0.6, 0.6),
    ("1-hour breakout", 1.0, 0.5),
    ("Dip in trend", 1.0, 0.5),
    ("Opening range", 1.0, 0.5),
]


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------
def load(mkt, sym, start_ms, now_ms):
    t, o, h, l, c = [], [], [], [], []
    st = start_ms
    while st < now_ms:
        T.THROTTLE.wait(T.kline_weight(1000))
        rows = mkt._raw(sym, TF, 1000, start_time=st)
        if not rows:
            break
        for r in rows:
            if int(r[6]) < now_ms:
                t.append(int(r[0]))
                o.append(float(r[1])); h.append(float(r[2]))
                l.append(float(r[3])); c.append(float(r[4]))
        if len(rows) < 1000:
            break
        st = int(rows[-1][0]) + 1
    return {"t": t, "o": o, "h": h, "l": l, "c": c}


def stdev_list(c, mid, n):
    out = [None] * len(c)
    for i in range(n - 1, len(c)):
        m = mid[i]
        out[i] = (sum((x - m) ** 2 for x in c[i - n + 1:i + 1]) / n) ** 0.5
    return out


# ---------------------------------------------------------------------------
# Signals: one array per strategy, +1 LONG / -1 SHORT / 0 nothing (at candle close)
# ---------------------------------------------------------------------------
def signals(k):
    t, h, l, c = k["t"], k["h"], k["l"], k["c"]
    n = len(c)
    e9, e21, e20, e50 = S.ema(c, 9), S.ema(c, 21), S.ema(c, 20), S.ema(c, 50)
    rs = S.rsi(c, 14)
    mid = S.sma(c, 20)
    sd = stdev_list(c, mid, 20)
    sig = [array.array("b", bytes(n)) for _ in STRATS]

    # your sheet's % row: average of the day's closes since 5:30 AM
    pct = [0.0] * n
    total, cnt, day = 0.0, 0, None
    for i in range(n):
        d = t[i] // DAY_MS
        if d != day:
            day, total, cnt = d, 0.0, 0
        total += c[i]
        cnt += 1
        pct[i] = 1 - (total / cnt) / c[i] if cnt > 1 else 0.0

    half = None
    first_col = done_col = None
    or_hi = or_lo = None
    or_done = False
    for i in range(1, n):
        hs = t[i] // HALF_MS
        if hs != half:                                # a new half starts
            half, done_col = hs, False
            first_col = 1 if pct[i] > 0 else -1 if pct[i] < 0 else 0
            or_hi, or_lo, or_done = h[i], l[i], False
            start_i = i
        # 1 colour change: first change in this half
        col = 1 if pct[i] > 0 else -1 if pct[i] < 0 else 0
        if not done_col and col:
            if first_col == 0:
                first_col = col
            elif col != first_col:
                sig[0][i], done_col = col, True
        # 2-4 % row crosses
        for s, lvl in ((1, 0.005), (2, 0.005), (3, 0.01)):
            if pct[i] >= lvl > pct[i - 1]:
                sig[s][i] = 1
            elif pct[i] <= -lvl < pct[i - 1]:
                sig[s][i] = -1
        # 5 EMA cross
        if e9[i] and e21[i] and e9[i - 1] and e21[i - 1]:
            if e9[i] > e21[i] and e9[i - 1] <= e21[i - 1]:
                sig[4][i] = 1
            elif e9[i] < e21[i] and e9[i - 1] >= e21[i - 1]:
                sig[4][i] = -1
        # 6 RSI reversal (on the way back)
        if rs[i] is not None and rs[i - 1] is not None:
            if rs[i - 1] < 30 <= rs[i]:
                sig[5][i] = 1
            elif rs[i - 1] > 70 >= rs[i]:
                sig[5][i] = -1
        # 7 / 8 Bollinger
        if sd[i] and sd[i - 1]:
            up, lo = mid[i] + 2 * sd[i], mid[i] - 2 * sd[i]
            pup, plo = mid[i - 1] + 2 * sd[i - 1], mid[i - 1] - 2 * sd[i - 1]
            if c[i] > up and c[i - 1] <= pup:
                sig[6][i], sig[7][i] = 1, -1
            elif c[i] < lo and c[i - 1] >= plo:
                sig[6][i], sig[7][i] = -1, 1
        # 9 1-hour (12 candles) breakout
        if i >= 13:
            hh, ll = max(h[i - 12:i]), min(l[i - 12:i])
            if c[i] > hh and c[i - 1] <= max(h[i - 13:i - 1]):
                sig[8][i] = 1
            elif c[i] < ll and c[i - 1] >= min(l[i - 13:i - 1]):
                sig[8][i] = -1
        # 10 dip in trend
        if e50[i] and e50[i - 12] and e20[i]:
            if e50[i] > e50[i - 12] and l[i] <= e20[i] < c[i] and c[i - 1] > e20[i - 1]:
                sig[9][i] = 1
            elif e50[i] < e50[i - 12] and h[i] >= e20[i] > c[i] and c[i - 1] < e20[i - 1]:
                sig[9][i] = -1
        # 11 opening range: first hour of the half, then one break
        if i - start_i < 12:
            or_hi, or_lo = max(or_hi, h[i]), min(or_lo, l[i])
        elif not or_done:
            if c[i] > or_hi:
                sig[10][i], or_done = 1, True
            elif c[i] < or_lo:
                sig[10][i], or_done = -1, True
    return sig


# ---------------------------------------------------------------------------
# Trades of one strategy on one coin inside one half
# ---------------------------------------------------------------------------
def run_half(k, sig, s, i0, i1, flip=False):
    """Trades for candles i0..i1-1. Returns [(entry_ms, exit_ms, move, stop, side)]."""
    _, tp, sl = STRATS[s]
    tp, sl = tp / 100, sl / 100
    h, l, c, t = k["h"], k["l"], k["c"], k["t"]
    out, pos = [], None
    for i in range(i0, i1):
        if pos:
            e, side, ent_ms = pos
            if side == 1:
                hit_sl, hit_tp = l[i] <= e * (1 - sl), h[i] >= e * (1 + tp)
            else:
                hit_sl, hit_tp = h[i] >= e * (1 + sl), l[i] <= e * (1 - tp)
            if hit_sl or hit_tp:
                out.append((ent_ms, t[i] + BAR_MS, -sl if hit_sl else tp, sl, side))
                pos = None
            continue
        v = sig[s][i]
        if v and i < i1 - 1:
            pos = (c[i], -v if flip else v, t[i] + BAR_MS)
    if pos:                                           # end of the half
        e, side, ent_ms = pos
        out.append((ent_ms, t[i1 - 1] + BAR_MS, (c[i1 - 1] - e) / e * side, sl, side))
    return out


def r_of(move, sl):
    return move / sl - COST / sl                      # result in "risk units", after fees


# ---------------------------------------------------------------------------
class Book:
    def __init__(self, name):
        self.name, self.bal, self.peak, self.dd = name, START, START, 0.0
        self.n = self.w = self.skipped = self.days = 0
        self.r = self.day_r = 0.0                     # result in risk units (1R = 1% risk)
        self.daily = []

    def trade_evening(self, trades):
        """trades: [(entry_ms, exit_ms, move, stop, side)] — taken in time order, max 10 open."""
        if trades:
            self.days += 1
        self.day_r = 0.0
        trades = sorted(trades)
        live = []                                     # (exit_ms, usd)
        for ent, ex, move, sl, _ in trades:
            for x in sorted(live):
                if x[0] <= ent:
                    self._book(x[1])
                    live.remove(x)
            if len(live) >= MAX_OPEN:
                self.skipped += 1
                continue
            size = self.bal * RISK / sl
            usd = size * move - size * COST
            live.append((ex, usd))
            self.day_r += r_of(move, sl)
            self.n += 1
            self.w += move > 0
        for x in sorted(live):
            self._book(x[1])
        self.r += self.day_r
        self.daily.append(self.bal)

    def _book(self, usd):
        self.bal += usd
        self.peak = max(self.peak, self.bal)
        self.dd = max(self.dd, (self.peak - self.bal) / self.peak * 100)


def pick(watch):
    """watch: {strategy: [trades]} -> (side, [strategies]) of the biggest same-way failure group."""
    groups = {1: [], -1: []}
    for st, trades in watch.items():
        rs = [(r_of(m, sl), side) for _, _, m, sl, side in trades]
        if not rs or sum(r for r, _ in rs) >= 0:
            continue                                  # did not lose
        lost = {1: sum(-r for r, sd in rs if r < 0 and sd == 1),
                -1: sum(-r for r, sd in rs if r < 0 and sd == -1)}
        total = lost[1] + lost[-1]
        for sd in (1, -1):
            if total and lost[sd] / total >= ONE_SIDE:
                groups[sd].append(st)
    side = 1 if len(groups[1]) >= len(groups[-1]) else -1
    if len(groups[side]) < MIN_GROUP:
        return None, [], groups
    return side, groups[side], groups


def segment(data, days, first_day, now_ms, offset, title):
    """offset 0: watch morning, trade evening. offset 12h: watch evening, trade next morning."""
    ist = lambda ms: datetime.fromtimestamp(ms / 1000, S.IST)
    books = {"A": Book("A) YOUR RULE — same-way failures"),
             "B": Book("B) same group FLIPPED (long↔short)"),
             "C": Book("C) all OTHER strategies"),
             "D": Book("D) all strategies every time")}
    used = [0] * len(STRATS)
    rows = []
    for d in range(days):
        w0 = first_day + d * DAY_MS + offset          # watch window start
        w1, t1 = w0 + HALF_MS, w0 + 2 * HALF_MS       # trade window = w1 .. t1
        if t1 > now_ms:
            continue
        watch = {st: [] for st in range(len(STRATS))}
        trade = {st: [] for st in range(len(STRATS))}
        flipped = {st: [] for st in range(len(STRATS))}
        for sym, (k, sig) in data.items():
            t = k["t"]
            i0, i1, i2 = (bisect.bisect_left(t, x) for x in (w0, w1, t1))
            if i1 - i0 < 10 or i2 - i1 < 10:
                continue
            for st in range(len(STRATS)):
                watch[st] += run_half(k, sig, st, i0, i1)
                trade[st] += run_half(k, sig, st, i1, i2)
                flipped[st] += run_half(k, sig, st, i1, i2, flip=True)
        side, group, groups = pick(watch)
        others = [st for st in range(len(STRATS)) if st not in group]
        books["A"].trade_evening([x for st in group for x in trade[st]])
        books["B"].trade_evening([x for st in group for x in flipped[st]])
        books["C"].trade_evening([x for st in others for x in trade[st]] if group else [])
        books["D"].trade_evening([x for st in range(len(STRATS)) for x in trade[st]])
        for st in group:
            used[st] += 1
        what = (f"{len(group)} failed on {'LONG' if side == 1 else 'SHORT'} → trade "
                + ",".join(str(st + 1) for st in group)) if group else \
               f"no group (LONG-fail {len(groups[1])}, SHORT-fail {len(groups[-1])}) → no trade"
        rows.append(f"{ist(w1):%d %b %I:%M %p}: {what} | "
                    + " ".join(f"{x} {books[x].day_r:+.1f}R" for x in "ABCD"))
    lines = [f"<b>{title}</b>", ""]
    for b in books.values():
        lines += [f"<b>{b.name}</b>",
                  f"Halves traded {b.days} | trades {b.n} ({b.w / b.n * 100:.0f}% in profit)"
                  if b.n else "No trades",
                  f"Result: <b>{b.r:+.1f}R</b> ({b.r / b.n if b.n else 0:+.3f}R per trade)",
                  f"Balance: $1000 → <b>${b.bal:.2f}</b> ({(b.bal / START - 1) * 100:+.1f}%) | "
                  f"biggest fall -{b.dd:.1f}%" + (f" | skipped {b.skipped}" if b.skipped else ""), ""]
    lines.append("How often each strategy was in the group: "
                 + ", ".join(f"{st + 1}:{n}" for st, n in enumerate(used) if n))
    lines += ["", "<b>Each half (start of the trading half): the group → result in R</b>"] + rows
    return lines


def main():
    nums = [int(a) for a in sys.argv[1:] if a.isdigit()]
    days = min(max(nums[0] if nums else 30, 2), 60)
    try:
        mkt = S.Market()
    except Exception as e:
        S.telegram_send(f"⚠️ Half-day test did not run: {e}")
        print(e)
        return 1
    if mkt.mode != "futures":
        S.telegram_send("⚠️ Half-day test needs Binance FUTURES data. Run it on the server.")
        return 1
    now_ms = int(time.time() * 1000)
    first_day = (now_ms // DAY_MS - days) * DAY_MS
    coins = list(S.backtest_coins(mkt))
    print(f"[half] {len(coins)} coins, {days} days, {TF} futures candles", flush=True)
    data = {}
    for n, sym in enumerate(coins, 1):
        try:
            k = load(mkt, sym, first_day - DAY_MS, now_ms)
            if len(k["c"]) > 300:
                data[sym] = (k, signals(k))
        except Exception as e:
            print(f"[half] {sym}: {e}")
        print(f"[half] {n}/{len(coins)} {sym}", flush=True)

    head = [f"🌗 <b>Half-day strategy switch — last {days} days, {len(data)} coins "
            "(Binance futures, 5-min candles)</b>",
            f"Watch 12 hours, then trade the next 12 hours with the strategies that failed the "
            f"SAME way (most losses on LONG, or most on SHORT; at least {MIN_GROUP}). "
            "Start $1000, 1% risk per trade, max 10 open, fees + slippage 0.15%. "
            "1R = the 1% risked on one trade.",
            "Strategies: " + ", ".join(f"{i + 1} {n}" for i, (n, _, _) in enumerate(STRATS)), ""]
    seg1 = segment(data, days, first_day, now_ms, 0,
                   "SEGMENT 1 — watch MORNING (5:30 AM-5:30 PM) → trade EVENING (5:30 PM-5:30 AM)")
    seg2 = segment(data, days, first_day, now_ms, HALF_MS,
                   "SEGMENT 2 — watch EVENING (5:30 PM-5:30 AM) → trade next MORNING (5:30 AM-5:30 PM)")
    tail = ["", "If A is not clearly better than D (no picking), watching the first half does not "
            "help to choose. Past results do not guarantee future results."]
    for part in (head + seg1, ["🌗"] + seg2 + tail):
        msg = "\n".join(part)
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


if __name__ == "__main__":
    sys.exit(main())
