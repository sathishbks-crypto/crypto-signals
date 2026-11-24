#!/usr/bin/env python3
"""
TEST ONLY - places no trade.

Same steady-mover signals as steady_backtest.py (checked every hour):
    steady riser  (+2% in the last hour without -1%)  -> BUY
    steady faller (-2% in the last hour without +1%)  -> SELL
but with a WIDE stop and target on each trade:
    trade amount $10 at 5x
    stop-loss   = 100% of the trade amount  (price 20% against)
    target      = 200% of the trade amount  (price 40% in favour)
No time limit: a trade stays open until stop or target (or the end of the data).

Wallet starts at $100. Trade amount = $10 for every full $100 in the wallet
($100-199 -> $10, $200-299 -> $20 ...). A new trade is taken only if the
wallet has enough free money (money not already in open trades).
One trade per coin at a time. Fees included (0.5% of the trade amount).
Note: at 5x a 20% move against you is about where Binance liquidates, so
the stop-loss here is close to liquidation. Funding fees are not included.

Run on the server (bot folder):   python3 wide_backtest.py
Takes about 10-15 minutes. The report goes to Telegram.
"""

import sys
import time
from datetime import datetime

import steady_backtest as SB

LEV = 5
SL_PCT_AMOUNT = 100.0          # % of trade amount
TP_PCT_AMOUNT = 200.0
SL_PRICE = SL_PCT_AMOUNT / LEV  # 20% price
TP_PRICE = TP_PCT_AMOUNT / LEV  # 40% price
PULLBACK = 1.0
FEE = SB.FEE_PRICE * LEV        # % of the trade amount


def run_trade(T, H, L, C, i, side):
    """Enter at close of bar i-1. Returns (result % of amount, exit_ms, how)."""
    e = C[i - 1]
    tp = e * (1 + side * TP_PRICE / 100)
    sl = e * (1 - side * SL_PRICE / 100)
    for j in range(i, len(T)):
        hit_tp = H[j] >= tp if side == 1 else L[j] <= tp
        hit_sl = L[j] <= sl if side == 1 else H[j] >= sl
        if hit_sl:
            return -SL_PCT_AMOUNT - FEE, T[j] + SB.BAR_MS, "stop"
        if hit_tp:
            return TP_PCT_AMOUNT - FEE, T[j] + SB.BAR_MS, "target"
    return side * (C[-1] / e - 1) * 100 * LEV - FEE, None, "open"


def size_for(bal):
    return 10.0 * max(1, int(bal // 100))


def main():
    t0 = time.time()
    end_ms = int(time.time() * 1000) // 3_600_000 * 3_600_000
    start_ms = end_ms - SB.DAYS * 86400 * 1000
    info = SB.get("/fapi/v1/exchangeInfo")
    live = {x["symbol"] for x in info["symbols"]
            if x.get("contractType") == "PERPETUAL" and x.get("quoteAsset") == "USDT" and x.get("status") == "TRADING"}
    vols = {t["symbol"]: float(t["quoteVolume"]) for t in SB.get("/fapi/v1/ticker/24hr")}
    syms = sorted(s for s in live if vols.get(s, 0) >= SB.MIN_VOL)
    print(f"Testing {len(syms)} coins over {SB.DAYS} days (about 10-15 minutes)...", flush=True)

    cands = []    # (entry_ms, exit_ms or None, result%, how, sym, side, hours)
    for k, s in enumerate(syms, 1):
        try:
            T, O, H, L, C = SB.klines(s, start_ms, end_ms)
        except Exception as e:
            print(f"  {s}: skipped ({e})")
            continue
        if len(T) < 300:
            continue
        idx = {t: i for i, t in enumerate(T)}
        busy = 0
        for hb in range(start_ms + 3_600_000, end_ms, 3_600_000):
            b, a = idx.get(hb), idx.get(hb - 3_600_000)
            if a is None or b is None or b - a != 12 or b >= len(T) - 1 or hb < busy:
                continue
            d = SB.steady(O, H, L, C, a, b, PULLBACK)
            if not d:
                continue
            r, ex, how = run_trade(T, H, L, C, b, d)
            hrs = ((ex or end_ms) - hb) / 3_600_000
            cands.append((hb, ex, r, how, s, d, hrs))
            busy = ex if ex else end_ms + 1          # one trade per coin at a time
        del T, O, H, L, C, idx
        if k % 20 == 0:
            print(f"  {k}/{len(syms)} coins done", flush=True)

    # wallet: $10 per full $100, only with free money, profits/losses when a trade closes
    cands.sort(key=lambda c: (c[0], c[4]))
    bal = peak = 100.0
    dd = 0.0
    open_ = []          # (exit_ms or None, amount, result%, how, ...)
    taken, skipped = [], 0
    months = {}

    def settle(upto):
        nonlocal bal, open_
        still = []
        for o in open_:
            if o[0] is not None and o[0] <= upto:
                bal += o[1] * o[2] / 100
            else:
                still.append(o)
        open_ = still

    for c in cands:
        hb = c[0]
        settle(hb)
        peak = max(peak, bal)
        dd = max(dd, (peak - bal) / peak * 100 if peak else 0)
        m = datetime.fromtimestamp(hb / 1000, SB.IST).strftime("%Y-%m")
        months.setdefault(m, [bal, bal])
        months[m][1] = bal
        amt = size_for(bal)
        locked = sum(o[1] for o in open_)
        if bal - locked < amt or bal <= 0:
            skipped += 1
            continue
        open_.append((c[1], amt, c[2], c[3]))
        taken.append((c, amt))
    settle(end_ms + 1)                                   # closed trades
    unreal = sum(o[1] * o[2] / 100 for o in open_)       # still open at the end (value now)
    final = bal + unreal
    if months:
        months[sorted(months)[-1]][1] = final
    peak = max(peak, final)
    dd = max(dd, (peak - final) / peak * 100)

    n = len(taken)
    tgt = sum(1 for c, _ in taken if c[3] == "target")
    stp = sum(1 for c, _ in taken if c[3] == "stop")
    opn = n - tgt - stp
    buys = sum(1 for c, _ in taken if c[5] == 1)
    avg_h = sum(c[6] for c, _ in taken if c[3] != "open") / max(1, tgt + stp)
    lines = [f"🔬 <b>Wide stop test — last {SB.DAYS} days</b>",
             f"{len(syms)} Binance futures coins. Signal: steady riser → BUY, steady faller → SELL "
             f"(+2% in 1 hour without -1%). Trade amount $10 per full $100 of wallet, 5x. "
             f"Stop-loss 100% of the amount (price -{SL_PRICE:g}%), target 200% (price +{TP_PRICE:g}%), no time limit. "
             f"Fees included, funding fees not.", "",
             f"Signals: {len(cands)}  |  Trades taken: <b>{n}</b> ({buys} BUY, {n - buys} SELL)  |  "
             f"skipped (no free money): {skipped}",
             f"✅ Target +200% hit: <b>{tgt}</b>   ❌ Stop-loss -100% hit: <b>{stp}</b>   ⏳ still open: {opn}",
             f"Average time to close: {avg_h:.0f} hours ({avg_h / 24:.1f} days)", "",
             f"💰 <b>$100 → ${final:,.2f}</b> ({(final / 100 - 1) * 100:+.1f}%)  "
             f"(of which open trades now: {unreal:+.2f})  |  biggest drop -{dd:.1f}%",
             "Month-end wallet: " + ",  ".join(
                 f"{datetime.strptime(m, '%Y-%m').strftime('%b')} ${v[1]:,.0f}" for m, v in sorted(months.items())),
             "", "<b>Last 8 trades</b>"]
    for c, amt in taken[-8:]:
        d = datetime.fromtimestamp(c[0] / 1000, SB.IST).strftime("%d %b %H:%M")
        mark = {"target": "✅", "stop": "❌", "open": "⏳"}[c[3]]
        lines.append(f"{mark} {d} {'BUY' if c[5] == 1 else 'SELL'} {c[4]} ${amt:.0f}: {c[2]:+.0f}% "
                     f"({c[6]:.0f} h)")
    lines += ["", f"Test only — no trades placed. ({(time.time() - t0) / 60:.0f} min)"]
    SB.tg("\n".join(lines))


if __name__ == "__main__":
    sys.exit(main())
