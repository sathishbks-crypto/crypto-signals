#!/usr/bin/env python3
"""
TEST ONLY - places no trade.

The BTC "ladder" idea, tested on the SAME 40 coins the live bot watches
(your favorites.txt list + today's top 10 gainers + top 10 losers),
for the last 90 days, 24 hours a day, using 1-minute candles.

Six separate tests, each with its own wallet ($100 start, $10 per trade, 5x):
   step 1%    : BUY only  and  SELL only
   step 0.5%  : BUY only  and  SELL only
   step 0.25% : BUY only  and  SELL only

BUY  : every time a coin rises <step>% from its lowest price since its last
       trade (each day starts again from the 5:30 AM open) -> LONG.
SELL : every time a coin falls <step>% from its highest price since its last
       trade -> SHORT.
Stop-loss = <step>% price against (= 5 x step % of the $10)
Target    = 2 x <step>% price in favour (1:2). Fees included (0.05% per side).
If stop and target are hit inside the same 1-minute candle -> counted as stop.

Wallet: a trade is taken only if the wallet has $10 free
(each open trade holds $10). Otherwise the signal is skipped.

Note: the gainer/loser part of the list is TODAY's list (the past lists are
not stored), so it is a little optimistic/pessimistic for those 20 coins.

Run on the server (bot folder):
    nohup python3 ladder_40.py > ladder.log 2>&1 &
    (optional)  python3 ladder_40.py 30      -> last 30 days only
Takes about 20-30 minutes. The report goes to Telegram.
Each finished coin is saved, so if the run stops (Killed), just run the same
command again: it skips the coins already done and continues.
Progress:   tail -3 ladder.log
"""

import gc
import heapq
import os
import sys
import time
from array import array
from datetime import datetime

import scanner as S
import btc_levels as B          # tg(), get(), IST

HERE = os.path.dirname(os.path.abspath(__file__))

DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 90
STEPS = (1.0, 0.5, 0.25)
LEV = 5
AMOUNT = 10.0
START_BAL = 100.0
FEE = 0.10 * LEV                # % of the amount, round trip
DAY_MS = 86_400_000
INF = float("inf")


def klines_1m(sym, start_ms, end_ms):
    T, H, L, C, O = array("d"), array("d"), array("d"), array("d"), array("d")
    cur = start_ms
    while cur < end_ms:
        rows = B.get("/fapi/v1/klines", symbol=sym, interval="1m", startTime=cur, endTime=end_ms, limit=1500)
        if not rows:
            break
        for r in rows:
            T.append(int(r[0])); O.append(float(r[1])); H.append(float(r[2]))
            L.append(float(r[3])); C.append(float(r[4]))
        cur = int(rows[-1][0]) + 60_000
        if len(rows) < 1500:
            break
        time.sleep(0.25)
    return T, O, H, L, C


def ladder(T, O, H, L, C, step, side, out):
    """Find all trades of one coin. Appends entry_ms, exit_ms, pnl $ (3 numbers per trade) to out."""
    n = len(T)
    sl_p, tp_p = step, 2 * step
    win = AMOUNT * (tp_p * LEV - FEE) / 100
    loss = AMOUNT * (-sl_p * LEV - FEE) / 100
    day = None
    ref = 0.0
    ent_i, ent_p = array("l"), array("d")      # index, entry price (small memory)
    up, dn = 1 + step / 100, 1 - step / 100
    for i in range(n):
        d = int(T[i]) // DAY_MS
        if d != day:             # new day at 5:30 AM IST: start from the open
            day, ref = d, O[i]
        if side == 1:
            took = False
            while H[i] >= ref * up:
                ref *= up
                ent_i.append(i); ent_p.append(ref)
                took = True
            if not took and L[i] < ref:
                ref = L[i]
        else:
            took = False
            while L[i] <= ref * dn:
                ref *= dn
                ent_i.append(i); ent_p.append(ref)
                took = True
            if not took and H[i] > ref:
                ref = H[i]
    last = C[n - 1]
    for i, e in zip(ent_i, ent_p):
        tp = e * (1 + side * tp_p / 100)
        sl = e * (1 - side * sl_p / 100)
        res_t, res = INF, None
        if side == 1:
            for j in range(i + 1, n):
                if L[j] <= sl:
                    res_t, res = T[j], loss; break
                if H[j] >= tp:
                    res_t, res = T[j], win; break
        else:
            for j in range(i + 1, n):
                if H[j] >= sl:
                    res_t, res = T[j], loss; break
                if L[j] <= tp:
                    res_t, res = T[j], win; break
        if res is None:          # still open: value at the latest price
            res = AMOUNT * (side * (last / e - 1) * 100 * LEV - FEE) / 100
        out.append(T[i]); out.append(res_t); out.append(res)
    return len(ent_i)


def load(path):
    arr = array("d")
    with open(path, "rb") as f:
        arr.frombytes(f.read())
    return arr


def triples(arr):
    for k in range(0, len(arr), 3):
        yield arr[k], arr[k + 1], arr[k + 2]


def wallet(files):
    """Run the $100 wallet over all coins' signals in time order (merged file by file, low memory)."""
    arrs = [load(f) for f in files]
    n_sig = sum(len(a) // 3 for a in arrs)
    bal = peak = START_BAL
    dd = 0.0
    open_ = []                   # (exit_ms, pnl)
    taken = skipped = w = l = still = 0
    months = {}
    for t, x_t, p_v in heapq.merge(*(triples(a) for a in arrs), key=lambda z: z[0]):
        if open_:
            keep = []
            for o in open_:
                if o[0] <= t:
                    bal += o[1]
                else:
                    keep.append(o)
            open_ = keep
        peak = max(peak, bal)
        dd = max(dd, (peak - bal) / peak * 100 if peak > 0 else 100)
        m = datetime.fromtimestamp(t / 1000, B.IST).strftime("%Y-%m")
        months[m] = bal
        if bal - AMOUNT * len(open_) < AMOUNT:
            skipped += 1
            continue
        taken += 1
        open_.append((x_t, p_v))
        if x_t == INF:
            still += 1
        elif p_v > 0:
            w += 1
        else:
            l += 1
    unreal = sum(o[1] for o in open_ if o[0] == INF)
    bal += sum(o[1] for o in open_)
    if months:
        months[sorted(months)[-1]] = bal
    peak = max(peak, bal)
    dd = max(dd, (peak - bal) / peak * 100 if peak > 0 else 100)
    del arrs
    gc.collect()
    return {"sig": n_sig, "bal": bal, "dd": dd, "taken": taken, "skipped": skipped, "w": w, "l": l,
            "open": still, "unreal": unreal, "months": months}


def main():
    t0 = time.time()
    groups = S.build_watchlist(S.Market())
    syms = list(groups)
    end_ms = int(time.time() * 1000) // 60_000 * 60_000
    start_ms = (end_ms // DAY_MS - DAYS + 1) * DAY_MS          # a 5:30 AM IST day start
    print(f"Testing {len(syms)} coins, last {DAYS} days, 1-minute candles (about 20-30 minutes)...", flush=True)
    cache = os.path.join(HERE, f"ladder_cache_{DAYS}d_{datetime.now(B.IST).strftime('%Y%m%d')}")
    os.makedirs(cache, exist_ok=True)
    fname = lambda sym, st, side: os.path.join(cache, f"{sym}_{st:g}_{'buy' if side == 1 else 'sell'}.bin")
    done = []
    for k, sym in enumerate(syms, 1):
        if os.path.exists(os.path.join(cache, sym + ".done")):      # finished in an earlier run
            done.append(sym)
            print(f"  {k}/{len(syms)} {sym} already done (saved)", flush=True)
            continue
        try:
            T, O, H, L, C = klines_1m(sym, start_ms, end_ms)
        except Exception as e:
            print(f"  {sym}: skipped ({e})", flush=True)
            continue
        if len(T) < 1000:
            print(f"  {sym}: too little data, skipped", flush=True)
            continue
        for st in STEPS:
            for side in (1, -1):
                out = array("d")
                ladder(T, O, H, L, C, st, side, out)
                with open(fname(sym, st, side), "wb") as f:
                    f.write(out.tobytes())
                del out
        open(os.path.join(cache, sym + ".done"), "w").close()
        done.append(sym)
        del T, O, H, L, C
        gc.collect()
        print(f"  {k}/{len(syms)} {sym} done ({(time.time() - t0) / 60:.0f} min)", flush=True)

    first = datetime.fromtimestamp(start_ms / 1000, B.IST).strftime("%d %b")
    lines = [f"🔬 <b>40-coin ladder test — last {DAYS} days</b> ({first} → today)",
             f"{len(done)} coins (your list + today's top 10 gainers + top 10 losers), 24 hours, 1-minute candles. "
             f"Each test: own wallet ${START_BAL:g}, ${AMOUNT:g} per trade, {LEV}x, 1:2, fees included. "
             f"A trade is skipped when the wallet has no free $10.", ""]
    for st in STEPS:
        win_amt = AMOUNT * (2 * st * LEV - FEE) / 100
        loss_amt = AMOUNT * (-st * LEV - FEE) / 100
        lines.append(f"━━ <b>Step {st:g}%</b> — stop-loss {st * LEV:g}% / target {2 * st * LEV:g}% of the $10 "
                     f"(win +${win_amt:.2f}, loss ${loss_amt:.2f})")
        for side, name in ((1, "📈 BUY only"), (-1, "📉 SELL only")):
            r = wallet([fname(sym, st, side) for sym in done])
            closed = r["w"] + r["l"]
            wr = r["w"] / closed * 100 if closed else 0
            lines.append(f"{name}: signals {r['sig']:,} · taken {r['taken']:,} · skipped {r['skipped']:,}")
            lines.append(f"   ✅ {r['w']:,}  ❌ {r['l']:,}  ⏳ {r['open']}  · win {wr:.0f}%")
            lines.append(f"   💰 <b>$100 → ${r['bal']:,.2f}</b> ({(r['bal'] / START_BAL - 1) * 100:+.1f}%) "
                         f"· biggest drop -{r['dd']:.0f}%")
            if r["months"]:
                lines.append("   Month-end: " + ",  ".join(
                    f"{datetime.strptime(m, '%Y-%m').strftime('%b')} ${v:,.0f}" for m, v in sorted(r["months"].items())))
        lines.append("")
    lines.append(f"Test only — no trades placed. Real slippage makes results a little worse. "
                 f"({(time.time() - t0) / 60:.0f} min)")
    B.tg("\n".join(lines))


if __name__ == "__main__":
    sys.exit(main())
