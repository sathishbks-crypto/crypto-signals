#!/usr/bin/env python3
"""
Top-down (4H -> 1H -> 15m) test for today, from 5:30 AM IST until now.

THE RULES (from "How to Analysis": higher timeframe = direction,
lower timeframe = confirmation)

  4H  Direction : last 2 swing highs AND last 2 swing lows rising (LONG)
                  or falling (SHORT), and price on the same side of EMA50.
      Key levels: the 4H swing high / swing low = the range.
      Supply/Demand: LONG only in the lower half of that range (demand side),
                  SHORT only in the upper half (supply side).
                  The target must not sit beyond the opposite 4H key level.

  1H  Liquidity : price swept (wicked through) a 1H swing low, then closed
                  back above it  -> stop-losses taken, reversal.
      Break     : then a 1H candle CLOSED above the last 1H swing high
                  (break of structure in the 4H direction).
      OB / FVG  : the zone = last opposite-colour 1H candle before the break
                  (order block). A fair value gap inside the move is noted.

  15m Confirmation: price comes back into that zone, then a 15m candle
                  closes in the trade direction above the high of the two
                  candles before it -> ENTRY at that close.

  Stop-loss  : beyond the zone / sweep wick (+ small buffer).
  Target     : 1:2 (stop = -100%, target = +200% of the trade amount).
  Fees 0.10% + slippage 0.05% included.  One trade per coin at a time.

Only information that existed at each moment is used (no peeking ahead).
SHORT trades use the same rules on a mirrored price chart.

Coins: EVERY Binance USDT coin with at least $20M traded in the last 24h
(stablecoins and leveraged tokens left out), plus your favorites.txt.
New coins without enough history are skipped automatically.

Usage:
  python smc_today.py              -> today (5:30 AM IST until now), all coins
  python smc_today.py 7            -> the last 7 days (more trades = fairer picture)
  python smc_today.py 7 mylist     -> only your favorites.txt coins
  python smc_today.py 180          -> 6 months (up to 180 days; about 25 minutes)

Data: Binance FUTURES only. Run it on the Lightsail server
(GitHub's US servers are blocked by Binance Futures).
"""

import bisect
import collections
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
import time
from datetime import datetime, timedelta, timezone

import scanner as S

RR = 2.0
COST_PCT = 0.10 + 0.05           # fees + slippage, % of entry
SWING_N = 2                      # swing = higher/lower than 2 candles on each side
BOS_LOOKBACK_1H = 24             # a 1H break stays usable for 24 hours
SWEEP_LOOKBACK_1H = 12           # the sweep must be within 12 hours before the break
TOUCH_LOOKBACK_15M = 4           # zone touched within the last hour
MIN_RISK_PCT, MAX_RISK_PCT = 0.3, 5.0
IST = S.IST
DAY_MS = 86400 * 1000
WORKERS = 6                      # coins downloaded at the same time
MAX_WEIGHT_PER_MIN = 1000        # Binance allows 2400/min per server; the rest is
                                 # left for the live bot so the server is never blocked


class Throttle:
    """Keeps Binance request 'weight' under MAX_WEIGHT_PER_MIN."""
    def __init__(self):
        self.lock = threading.Lock()
        self.used = collections.deque()          # (time, weight)

    def wait(self, w):
        while True:
            with self.lock:
                now = time.time()
                while self.used and now - self.used[0][0] > 60:
                    self.used.popleft()
                if sum(x[1] for x in self.used) + w <= MAX_WEIGHT_PER_MIN:
                    self.used.append((now, w))
                    return
            time.sleep(0.5)


THROTTLE = Throttle()


def kline_weight(limit):
    return 1 if limit < 100 else 2 if limit < 500 else 5 if limit <= 1000 else 10


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------
def fetch(mkt, sym, tf, start_ms):
    """Closed candles from start_ms until now."""
    tf_ms = {"15m": 15, "1h": 60, "4h": 240}[tf] * 60000
    want = (int(time.time() * 1000) - start_ms) // tf_ms + 2
    page = min(1000, max(want, 10))        # small requests = light on Binance's limits
    rows, st = [], start_ms
    while True:
        THROTTLE.wait(kline_weight(page))
        b = mkt._raw(sym, tf, page, start_time=st)
        if not b:
            break
        rows += b
        if len(b) < page:
            break
        st = int(b[-1][0]) + 1
    now = int(time.time() * 1000)
    seen, clean = set(), []
    for r in rows:
        if r[0] not in seen and int(r[6]) < now:
            seen.add(r[0])
            clean.append(r)
    return {
        "t": [int(r[0]) for r in clean], "tc": [int(r[6]) for r in clean],
        "o": [float(r[1]) for r in clean], "h": [float(r[2]) for r in clean],
        "l": [float(r[3]) for r in clean], "c": [float(r[4]) for r in clean],
    }


def mirror(k):
    """Flip the chart upside down so SHORT rules = LONG rules."""
    return {"t": k["t"], "tc": k["tc"],
            "o": [-x for x in k["o"]], "c": [-x for x in k["c"]],
            "h": [-x for x in k["l"]], "l": [-x for x in k["h"]]}


def swings(k):
    """Swing highs/lows. Each one becomes known only when the candle
    SWING_N bars later has closed. Returns sorted lists of
    (known_at_ms, index, price)."""
    h, l, n = k["h"], k["l"], SWING_N
    hi, lo = [], []
    for i in range(n, len(h) - n):
        if h[i] > max(h[i - n:i]) and h[i] >= max(h[i + 1:i + n + 1]):
            hi.append((k["tc"][i + n], i, h[i]))
        if l[i] < min(l[i - n:i]) and l[i] <= min(l[i + 1:i + n + 1]):
            lo.append((k["tc"][i + n], i, l[i]))
    return hi, lo


def known(sw, t_ms, before_idx=None):
    """Swings already known at time t_ms (optionally only those before an index)."""
    out = sw[:bisect.bisect_right(sw, (t_ms, float("inf"), float("inf")))]
    if before_idx is not None:
        out = [s for s in out if s[1] < before_idx]
    return out


def prep(k):
    d = dict(k)
    d["ema50"] = S.ema(k["c"], 50)
    d["atr"] = S.atr(k["h"], k["l"], k["c"], 14)
    d["sh"], d["sl"] = swings(k)
    d["tcs"] = k["tc"]
    return d


def last_closed(d, t_ms):
    return bisect.bisect_right(d["tcs"], t_ms) - 1


# ---------------------------------------------------------------------------
# Rules (written for LONG; SHORT uses the mirrored chart)
# ---------------------------------------------------------------------------
def bias_4h(h4, t_ms):
    j = last_closed(h4, t_ms)
    if j < 60 or h4["ema50"][j] is None:
        return None
    sh, sl = known(h4["sh"], t_ms), known(h4["sl"], t_ms)
    if len(sh) < 2 or len(sl) < 2:
        return None
    if not (sh[-1][2] > sh[-2][2] and sl[-1][2] > sl[-2][2]):
        return None                                   # not making higher highs + higher lows
    if h4["c"][j] <= h4["ema50"][j]:
        return None
    top, bottom = sh[-1][2], sl[-1][2]
    if top <= bottom:
        return None
    return {"top": top, "bottom": bottom, "mid": (top + bottom) / 2}


def setup_1h(h1, t_ms):
    """Most recent sweep -> break -> order block, known at t_ms."""
    j = last_closed(h1, t_ms)
    if j < 40:
        return None
    o, h, l, c, tc = h1["o"], h1["h"], h1["l"], h1["c"], h1["tc"]
    for b in range(j, max(j - BOS_LOOKBACK_1H, 30), -1):
        # break of structure: close above the last swing high known before this candle
        shs = known(h1["sh"], tc[b - 1], before_idx=b)
        if not shs:
            continue
        lvl = shs[-1][2]
        if not (c[b] > lvl and c[b - 1] <= lvl):
            continue
        # liquidity sweep before the break
        lo_i = min(range(b - SWEEP_LOOKBACK_1H, b), key=lambda x: l[x])
        sls = known(h1["sl"], tc[lo_i - 1], before_idx=lo_i)
        if not sls:
            return None
        swept = sls[-1][2]
        if not (l[lo_i] < swept and max(c[lo_i:b]) > swept):
            return None
        # order block: last down candle between the sweep and the break
        ob = None
        for x in range(b - 1, lo_i - 1, -1):
            if c[x] < o[x]:
                ob = x
                break
        if ob is None:
            return None
        zone_lo, zone_hi = l[ob], h[ob]
        # fair value gap in the move (info)
        fvg = None
        for x in range(lo_i + 2, min(b + 2, j + 1)):
            if l[x] > h[x - 2]:
                fvg = (h[x - 2], l[x])
        # still valid? no 1H close below the zone since the break
        if any(c[x] < zone_lo for x in range(b + 1, j + 1)):
            return None
        return {"b": b, "break_lvl": lvl, "swept": swept, "sweep_low": l[lo_i],
                "zone_lo": zone_lo, "zone_hi": zone_hi, "fvg": fvg,
                "break_ms": tc[b], "key": (b, round(zone_lo, 12))}
    return None


def confirm_15m(m, i, st):
    o, h, l, c = m["o"], m["h"], m["l"], m["c"]
    if i < 3 or m["tc"][i] <= st["break_ms"]:
        return False
    first = max(bisect.bisect_right(m["tcs"], st["break_ms"]), i - TOUCH_LOOKBACK_15M + 1)
    if not any(l[k] <= st["zone_hi"] for k in range(first, i + 1)):
        return False                                  # price did not come back to the zone
    if c[i] <= st["zone_lo"]:
        return False
    return c[i] > o[i] and c[i] > max(h[i - 1], h[i - 2])


def find_trades(sym, k4, k1, k15, side, day_start_ms):
    if side == -1:
        k4, k1, k15 = mirror(k4), mirror(k1), mirror(k15)
    h4, h1, m = prep(k4), prep(k1), prep(k15)
    trades, used, busy_until = [], set(), -1
    for i in range(len(m["c"])):
        t = m["tc"][i]
        if m["t"][i] < day_start_ms or i <= busy_until:
            continue
        b4 = bias_4h(h4, t)
        if not b4:
            continue
        price = m["c"][i]
        if price >= b4["mid"]:
            continue                                  # not in the demand (discount) half
        st = setup_1h(h1, t)
        if not st or st["key"] in used:
            continue
        if not confirm_15m(m, i, st):
            continue
        a = m["atr"][i] or 0
        entry = price
        stop = min(st["zone_lo"], st["sweep_low"]) - 0.1 * a
        risk = entry - stop
        if risk <= 0:
            continue
        risk_pct = risk / abs(entry) * 100
        if not (MIN_RISK_PCT <= risk_pct <= MAX_RISK_PCT):
            continue
        target = entry + RR * risk
        if target > b4["top"]:
            continue                                  # 4H key level blocks the target
        used.add(st["key"])
        res, r_mult, exit_i = simulate(m, i, entry, stop, target, risk)
        busy_until = exit_i if res != "open" else len(m["c"])
        trades.append(make_trade(sym, side, m, i, entry, stop, target, risk_pct,
                                 b4, st, res, r_mult, exit_i))
    return trades


def simulate(m, i, entry, stop, target, risk):
    fee_r = COST_PCT / 100 * abs(entry) / risk
    for k in range(i + 1, len(m["c"])):
        if m["l"][k] <= stop:                         # stop and target same candle = loss
            return "stop", -1.0 - fee_r, k
        if m["h"][k] >= target:
            return "target", RR - fee_r, k
    last = len(m["c"]) - 1
    return "open", (m["c"][last] - entry) / risk - fee_r, last


def make_trade(sym, side, m, i, entry, stop, target, risk_pct, b4, st, res, r_mult, exit_i):
    f = (lambda x: x) if side == 1 else (lambda x: -x)
    up = side == 1
    zone = sorted([f(st["zone_lo"]), f(st["zone_hi"])])
    reasons = [
        f"4H: {'higher highs + higher lows' if up else 'lower highs + lower lows'}, "
        f"price {'above' if up else 'below'} EMA50; entry in the "
        f"{'demand (lower)' if up else 'supply (upper)'} half of the 4H range "
        f"{S.fmt_price(min(f(b4['bottom']), f(b4['top'])))} – {S.fmt_price(max(f(b4['bottom']), f(b4['top'])))}",
        f"1H: liquidity sweep of {S.fmt_price(f(st['swept']))}, then close "
        f"{'above' if up else 'below'} {S.fmt_price(f(st['break_lvl']))} (break of structure)",
        f"1H order block {S.fmt_price(zone[0])} – {S.fmt_price(zone[1])}"
        + (" + fair value gap" if st["fvg"] else ""),
        f"15m: back in the zone, {'bullish' if up else 'bearish'} candle broke the last 2 candles' "
        f"{'highs' if up else 'lows'}",
    ]
    return {
        "sym": sym, "side": "LONG" if up else "SHORT",
        "time": datetime.fromtimestamp(m["tc"][i] / 1000, IST),
        "exit": datetime.fromtimestamp(m["tc"][exit_i] / 1000, IST),
        "entry": f(entry), "sl": f(stop), "tp": f(target), "risk_pct": risk_pct,
        "res": res, "r": r_mult, "reasons": reasons,
    }


# ---------------------------------------------------------------------------
# Coins
# ---------------------------------------------------------------------------
def all_qualified_coins(mkt):
    """Every clean USDT coin with enough volume (futures: only tradable perpetuals)."""
    tradable = None
    try:
        tradable = mkt.tradable_symbols()
    except Exception as e:
        print("[coins] exchangeInfo failed:", e)
    rows = []
    for t in mkt.tickers_24h():
        sym = t.get("symbol", "")
        if not S.is_clean_usdt_symbol(sym):
            continue
        if tradable is not None and sym not in tradable:
            continue
        try:
            vol = float(t["quoteVolume"])
        except (KeyError, ValueError, TypeError):
            continue
        if vol >= S.MIN_QUOTE_VOLUME:
            rows.append((sym, vol))
    rows.sort(key=lambda r: r[1], reverse=True)
    coins = [r[0] for r in rows]
    valid = {r[0] for r in rows} | {t for t in (tradable or [])}
    for f in S.load_favorites():                      # your coins always included
        if f not in coins and (tradable is None or f in valid):
            coins.append(f)
    return coins


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
def main():
    days = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].strip().isdigit() else 1
    days = min(max(days, 1), 180)
    now_ms = int(time.time() * 1000)
    day_start = (now_ms // DAY_MS - days + 1) * DAY_MS   # 00:00 UTC = 5:30 AM IST
    mylist = any(a.strip().lower() in ("mylist", "my", "favorites") for a in sys.argv[2:])
    try:
        mkt = S.Market()
    except Exception as e:
        msg = f"⚠️ 4H → 1H → 15m test did not run: {e}"
        print(msg)
        S.telegram_send(msg)
        return 1
    groups = list(S.backtest_coins(mkt)) if mylist else all_qualified_coins(mkt)
    print(f"[smc] {len(groups)} coins, from "
          f"{datetime.fromtimestamp(day_start / 1000, IST):%d %b %I:%M %p} IST")
    trades, failed = [], []

    def load(sym):
        try:
            return sym, (fetch(mkt, sym, "4h", day_start - 70 * DAY_MS),
                         fetch(mkt, sym, "1h", day_start - 20 * DAY_MS),
                         fetch(mkt, sym, "15m", day_start - 4 * DAY_MS))
        except Exception as e:
            print(f"[smc] {sym}: data error {e}")
            return sym, None

    # download several coins at the same time (much faster), then check the rules
    t0 = time.time()
    def batches():
        # small groups so the server never holds too much data in memory at once
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            for b in range(0, len(groups), WORKERS * 2):
                yield from pool.map(load, groups[b:b + WORKERS * 2])

    def est(n_candles):
        pages = n_candles // 1000 + 1
        return pages * kline_weight(min(1000, n_candles))
    per_coin = est((days + 70) * 6) + est((days + 20) * 24) + est((days + 4) * 96)
    print(f"[smc] about {len(groups) * per_coin / MAX_WEIGHT_PER_MIN:.0f} minute(s) "
          f"to download (kept slow on purpose so the live bot is never blocked)", flush=True)
    if True:
        for n, (sym, data) in enumerate(batches(), 1):
            if data is None:
                failed.append(sym)
                continue
            for side in (1, -1):
                trades += find_trades(sym, *data, side, day_start)
            if n % 50 == 0:
                print(f"[smc] {n}/{len(groups)} coins done, {len(trades)} trades, "
                      f"{time.time() - t0:.0f}s", flush=True)
    trades.sort(key=lambda t: t["time"])
    msg = report(trades, days, day_start, len(groups) - len(failed), mkt.mode, mylist)
    print(msg.replace("<b>", "").replace("</b>", ""))
    # send in pieces at trade boundaries (Telegram limit ~4000 characters)
    chunk = ""
    for block in msg.split("\n\n"):
        if len(chunk) + len(block) + 2 > 3800 and chunk:
            S.telegram_send(chunk)
            chunk = ""
        chunk += ("\n\n" if chunk else "") + block
    if chunk:
        S.telegram_send(chunk)
    return 0


def report(trades, days, day_start, ncoins, mode, mylist=False):
    start_txt = datetime.fromtimestamp(day_start / 1000, IST).strftime("%d %b, %I:%M %p")
    now_txt = datetime.now(IST).strftime("%d %b, %I:%M %p")
    title = "today" if days == 1 else f"last {days} days"
    lines = [f"🧭 <b>4H → 1H → 15m test — {title}</b>",
             f"{start_txt} → {now_txt} IST | {ncoins} coins "
             f"({'your list' if mylist else 'all with $20M+ daily volume'}, Binance {mode})",
             "Rules: 4H direction + demand/supply half, 1H sweep + break + order block, "
             "15m confirmation. Target 1:2, fees + slippage included.", ""]
    if not trades:
        lines.append("No coin met all the rules in this period → no trade. "
                     "(That is normal: these rules are strict.)")
        return "\n".join(lines)
    for n, t in enumerate(trades, 1):
        icon = {"target": "✅", "stop": "❌", "open": "⏳"}[t["res"]]
        when = (f"at {t['exit']:%I:%M %p}" if t["exit"].date() == t["time"].date()
                else f"on {t['exit']:%d %b %I:%M %p}")
        what = {"target": f"TARGET HIT {when}",
                "stop": f"STOP-LOSS HIT {when}",
                "open": "STILL OPEN now"}[t["res"]]
        e = t["entry"]
        lines += [
            f"{icon} <b>{n}. {t['side']} {t['sym']}</b> — {t['time']:%d %b %I:%M %p}",
            f"Entry {S.fmt_price(e)} | SL {S.fmt_price(t['sl'])} ({S.pct(e, t['sl']):+.2f}%) | "
            f"Target {S.fmt_price(t['tp'])} ({S.pct(e, t['tp']):+.2f}%)",
        ] + [f"  • {r}" for r in t["reasons"]] + [
            f"→ {what}: <b>{S.rp(t['r'])}</b> of the trade amount", ""]
    done = [t for t in trades if t["res"] != "open"]
    wins = sum(1 for t in done if t["res"] == "target")
    open_ = len(trades) - len(done)
    lines += [
        f"<b>Summary:</b> {len(trades)} trades — ✅ {wins} target, ❌ {len(done) - wins} stop-loss, "
        f"⏳ {open_} still open",
        f"Finished trades total: <b>{S.rp(sum(t['r'] for t in done))}</b> of one trade amount"
        + (f"  (open trades right now: {S.rp(sum(t['r'] for t in trades if t['res'] == 'open'))})"
           if open_ else ""),
        ("One day is too few trades to judge a method. Run with more days before trusting it."
         if days == 1 else
         f"LONG {sum(1 for t in trades if t['side'] == 'LONG')} / "
         f"SHORT {sum(1 for t in trades if t['side'] == 'SHORT')} trades. "
         "Fewer than ~50 trades is still a small sample."),
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())
