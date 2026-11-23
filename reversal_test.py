#!/usr/bin/env python3
"""
TEST ONLY - does not place any trade.

Checks two "turn-around" ideas on the last 90 days of Binance USD-M futures
data (1h candles, every USDT perpetual coin with enough volume):

  PUMP -> SHORT : a coin jumped a lot in 1-3 days, then starts to fall.
  DUMP -> LONG  : a coin crashed a lot in 1-3 days, then starts to bounce.

Each idea is tested with a few settings, with:
  - fixed exit   : close all at 1:2 (+200% of the trade amount)
  - ladder exit  : close half at +200%, stop to entry, let the rest run
                   (half again at every next +200% step, up to 10 days)
and an account simulation ($1000 start, settings.txt risk / limits).
Fees + slippage (0.20% per trade) are included. Funding fees are not.

Run on the server (bot folder):   python3 reversal_test.py
The report is sent to Telegram (takes about 5-10 minutes).
"""

import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)

# Telegram settings from secrets.txt (same as trader.py)
if os.path.exists("secrets.txt"):
    with open("secrets.txt", encoding="utf-8") as f:
        for line in f:
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                k = k.strip().upper()
                if k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID") and v.strip():
                    os.environ[k] = v.strip()

import scanner as S  # noqa: E402

DAYS = 90
UNSEEN_DAYS = 30           # honest test: choose on the first 60 days, check the last 30
WARMUP = 120
WINDOW = 72                # the big move must happen within 72 candles (3 days)
RECENT = 24                # ...and its top/bottom must be within the last 24 candles
MIN_VOL_24H = 10_000_000   # at least $10M traded in the last 24h (enough liquidity)
COOLDOWN = 48              # one trade per event: wait 48 candles before the next one
S.FEE_PCT_ROUND_TRIP = 0.20   # fees + extra slippage for these wild coins
START_BALANCE = 1000.0

# name: (side, move, stop type)
VARIANTS = {
    "P1": ("SHORT", 0.40, "swing"),
    "P2": ("SHORT", 0.40, "peak"),
    "P3": ("SHORT", 0.80, "swing"),
    "P4": ("SHORT", 0.80, "peak"),
    "D1": ("LONG", 0.25, "swing"),
    "D2": ("LONG", 0.25, "peak"),
    "D3": ("LONG", 0.40, "swing"),
    "D4": ("LONG", 0.40, "peak"),
}


def describe(key):
    side, move, stop = VARIANTS[key]
    what = f"pump ≥{move * 100:.0f}%" if side == "SHORT" else f"crash ≥{move * 100:.0f}%"
    st = "stop above last swing" if stop == "swing" else "stop beyond the extreme"
    if side == "LONG":
        st = "stop below last swing" if stop == "swing" else "stop beyond the extreme"
    return f"{what}, {st}"


def find_trades(sym, k, start_ms):
    c, h, l, v, tc = k["c"], k["h"], k["l"], k["v"], k["tc"]
    r = S.rsi(c, 14)
    a = S.atr(h, l, c, 14)
    n = len(c)
    cum = [0.0]
    for x in v:
        cum.append(cum[-1] + x)
    busy = {key: -1 for key in VARIANTS}
    out = {key: [] for key in VARIANTS}

    for i in range(WARMUP, n - 1):
        if tc[i] < start_ms or r[i] is None or a[i] is None:
            continue
        for d in (-1, 1):
            # cheap checks first
            if d == -1:      # pump -> short: candle closes below the last 3 lows, RSI cooled
                if not (c[i] < min(l[i - 3:i]) and r[i] < 60):
                    continue
            else:            # dump -> long: candle closes above the last 3 highs, RSI recovered
                if not (c[i] > max(h[i - 3:i]) and r[i] > 40):
                    continue
            if cum[i + 1] - cum[i - 23] < MIN_VOL_24H:
                continue
            if d == -1:
                seg = h[i - RECENT:i + 1]
                ext = max(seg)
                ext_i = i - RECENT + seg.index(ext)
                base = min(l[i - WINDOW:ext_i + 1])
                move = ext / base - 1
                off = (ext - c[i]) / ext
                hot = max(x for x in r[i - RECENT:i + 1] if x is not None) >= 75
            else:
                seg = l[i - RECENT:i + 1]
                ext = min(seg)
                ext_i = i - RECENT + seg.index(ext)
                base = max(h[i - WINDOW:ext_i + 1])
                move = 1 - ext / base
                off = (c[i] - ext) / ext
                hot = min(x for x in r[i - RECENT:i + 1] if x is not None) <= 25
            if not hot or not (0.05 <= off <= 0.25):
                continue
            for key, (side, need, stop_type) in VARIANTS.items():
                if (side == "SHORT") != (d == -1) or i <= busy[key] or move < need:
                    continue
                e = c[i]
                if stop_type == "peak":
                    sl = ext - d * 0.3 * a[i]
                else:
                    sw = max(h[i - 5:i + 1]) if d == -1 else min(l[i - 5:i + 1])
                    sl = sw - d * 0.3 * a[i]
                risk = abs(e - sl)
                if not (0.01 <= risk / e <= 0.30):
                    continue
                tp = e + d * risk * 2
                sig = {"d": d, "entry": e, "sl": sl, "tp1": tp, "tp2": tp,
                       "tp1_r": 2.0, "tp2_r": 2.0, "risk": risk}
                res, rr, xi = S.simulate(k, i, sig)
                if res == "open":
                    continue
                lr, lx, ln, _ = S.simulate_ladder(k, i, sig)
                out[key].append((sym, side, res, rr, tc[i], tc[xi], lr, tc[lx], ln))
                busy[key] = max(xi, i + COOLDOWN)
    return out


def account(trades, t0, t1):
    S.SET["start_balance"] = START_BALANCE
    return S.simulate_account(trades, t0, t1)


def main():
    t_start = time.time()
    mkt = S.Market()
    if mkt.mode != "futures":
        print("Futures data is not reachable from here. Run this on the AWS server.")
        return 1
    tick = {t["symbol"]: float(t.get("quoteVolume") or 0) for t in mkt.tickers_24h()}
    syms = sorted(s for s in mkt.tradable_symbols() if S.is_clean_usdt_symbol(s))
    print(f"{len(syms)} futures coins. Downloading 90 days of 1h candles "
          f"(about 5-10 minutes, please wait)...", flush=True)
    start_ms = int((time.time() - DAYS * 86400) * 1000)
    now_ms = int(time.time() * 1000)
    split_ms = now_ms - UNSEEN_DAYS * 86400 * 1000
    need = DAYS * 24 + WARMUP + 10

    all_tr = {key: [] for key in VARIANTS}
    used = 0
    for n, sym in enumerate(syms, 1):
        try:
            k = mkt.klines(sym, "1h", need)
        except Exception as e:
            print(f"  {sym}: skipped ({e})")
            continue
        if len(k["c"]) > WARMUP + 50:
            used += 1
            for key, tr in find_trades(sym, k, start_ms).items():
                all_tr[key] += tr
        if n % 50 == 0:
            print(f"  {n}/{len(syms)} coins done...", flush=True)
        time.sleep(0.25)

    lines = [
        f"🔬 <b>Turn-around test — last {DAYS} days</b>",
        f"{used} Binance futures coins, 1h candles. Fees + slippage 0.2% per trade included "
        f"(funding fees not). Account: ${START_BALANCE:,.0f} start, "
        f"{S.SET['risk_per_trade']:g}% risk per trade, max {S.fmt_lim(S.SET['max_open_trades'])} open, "
        f"daily loss limit {S.fmt_lim(S.SET['daily_loss_limit'])}%.",
        "Fixed = close all at +200%.  Ladder = half at +200%, stop to entry, let the rest run.",
    ]

    def variant_block(key):
        tr = all_tr[key]
        st = S.stats(tr, start_ms, now_ms)
        lad = S.as_ladder(tr)
        lst = S.stats(lad, start_ms, now_ms)
        out = [f"<b>{key}</b> {describe(key)}"]
        if not tr:
            out.append("   no trades")
            return out
        best = max(t[6] for t in tr)
        fa = account(tr, start_ms, now_ms)
        la = account(lad, start_ms, now_ms)
        out.append(f"   {st['n']} trades, win {st['win']:.0f}% | fixed <b>{S.rp(st['net'])}</b> "
                   f"(halves {S.rp(st['net1'])} / {S.rp(st['net2'])}) | ladder <b>{S.rp(lst['net'])}</b>, "
                   f"best single trade {S.rp(best)}")
        out.append(f"   ${START_BALANCE:,.0f} → fixed <b>${fa['end']:,.0f}</b> (drop -{fa['max_dd']:.0f}%) | "
                   f"ladder <b>${la['end']:,.0f}</b> (drop -{la['max_dd']:.0f}%)")
        return out

    def honest(prefix):
        keys = [k for k in VARIANTS if k.startswith(prefix)]
        older = {k: S.stats(all_tr[k], start_ms, split_ms) for k in keys}
        good = [(k, st) for k, st in older.items()
                if st["n"] >= 10 and st["net"] > 0 and st["net1"] > 0 and st["net2"] > 0]
        good.sort(key=lambda ks: ks[1]["net"], reverse=True)
        pick = good[0][0] if good else None
        if not pick:
            return [f"Honest test (choose on first {DAYS - UNSEEN_DAYS} days, trade last "
                    f"{UNSEEN_DAYS} days): no setting was good enough → ❌ would not trade."]
        tr = all_tr[pick]
        fa = account(tr, split_ms, now_ms)
        la = account(S.as_ladder(tr), split_ms, now_ms)
        n30 = sum(1 for t in tr if t[4] >= split_ms)
        mark = "✅" if max(fa["growth"], la["growth"]) > 0 else "❌"
        return [f"{mark} Honest test: {pick} chosen on the first {DAYS - UNSEEN_DAYS} days → last "
                f"{UNSEEN_DAYS} days: {n30} trades, fixed <b>{fa['growth']:+.1f}%</b>, "
                f"ladder <b>{la['growth']:+.1f}%</b>"]

    lines += ["", "📉 <b>PUMP → SHORT</b> (coin jumped, then turns down)"]
    for key in ("P1", "P2", "P3", "P4"):
        lines += variant_block(key)
    lines += honest("P")
    lines += ["", "📈 <b>CRASH → LONG</b> (coin crashed, then turns up)"]
    for key in ("D1", "D2", "D3", "D4"):
        lines += variant_block(key)
    lines += honest("D")
    lines += ["", f"Test only — no trades were placed. ({(time.time() - t_start) / 60:.0f} min)"]

    msg = "\n".join(lines)
    S.telegram_send(msg)
    print(msg.replace("<b>", "").replace("</b>", ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
