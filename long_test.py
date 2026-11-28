#!/usr/bin/env python3
"""
6-month test of the SLOWER setups only — Binance FUTURES data.

STRATEGIES (the ones that look at bigger candles)
  G Trend breakout (slow)          1h entry + 4h trend
  H Trend breakout, wide stop      1h entry + 4h trend
  I Strong-trend breakout          1h entry + 4h trend
  J Dip buy / rally sell in trend  1h entry + 4h trend
  K Dip in trend, wide stop        1h entry + 4h trend
  L Dip in strong trend            1h entry + 4h trend
  M 4H -> 1H -> 15m                4h direction, 1h sweep + break + order block, 15m confirmation
  (G-L are the 6 rule sets of the live scanner; M is the "How to Analysis" picture.)
  All with target 1:2, fees + slippage included (same as before).

MONEY (assumed)
  Start $1000 for EACH strategy. Every trade risks 1% of the balance
  (stop-loss = -1% of the wallet, target = about +2%). At most 10 open at once.

WHAT THE REPORT SHOWS for each strategy
  trades, % in profit, result in R (1R = the 1% risked), the 3 two-month parts,
  final balance, biggest fall, and an HONEST CHECK: choose the best strategy on the
  first 4 months only, then see what it did in the last 2 months it never saw.

YOUR HALF-DAY RULE (second part of the report)
  Every day is split in two 12-hour halves (5:30 AM-5:30 PM and 5:30 PM-5:30 AM).
     SEGMENT 1: watch the MORNING -> trade the EVENING
     SEGMENT 2: watch the EVENING -> trade the next MORNING
  While watching, the 7 strategies are only scored (trades that already
  finished by the end of the watch half — no peeking).
  "Failed the same way": the strategy lost in the watch half and 60%+ of its
  losses came from one side (LONG trades stopped out, or SHORT trades stopped out).
  If at least 3 strategies failed the same way, ONLY those are traded in the next
  half; otherwise no trade. A trade runs until its target or stop (no time close).
  Compared with: C) the other strategies, D) all strategies every half.

Coins: your favorites.txt list (today's gainers/losers left out on purpose).

Usage (on the server, in the bot folder):
  python3 long_test.py           -> last 180 days (6 months)
  python3 long_test.py 90        -> last 90 days
"""

import sys
import time
from datetime import datetime

import scanner as S
import smc_today as T

START, RISK, MAX_OPEN = 1000.0, 0.01, 10
DAY_MS, HALF_MS = 86400 * 1000, 43200 * 1000
MIN_GROUP, ONE_SIDE = 3, 0.60


def money(trades):
    """trades: [(entry_ms, exit_ms, R)] -> (final balance, biggest fall %, skipped)."""
    bal = peak = START
    dd, skipped, live = 0.0, 0, []
    for ent, ex, r, *_ in sorted(trades):
        for x in sorted(live):
            if x[0] <= ent:
                bal += x[1] * bal                      # R x 1% of the balance at that time
                peak = max(peak, bal)
                dd = max(dd, (peak - bal) / peak * 100)
                live.remove(x)
        if len(live) >= MAX_OPEN:
            skipped += 1
            continue
        live.append((ex, r * RISK))
    for x in sorted(live):
        bal += x[1] * bal
        peak = max(peak, bal)
        dd = max(dd, (peak - bal) / peak * 100)
    return bal, dd, skipped


def smc_trades(mkt, coins, start_ms):
    out = []
    for n, sym in enumerate(coins, 1):
        try:
            k4 = T.fetch(mkt, sym, "4h", start_ms - 70 * DAY_MS)
            k1 = T.fetch(mkt, sym, "1h", start_ms - 20 * DAY_MS)
            k15 = T.fetch(mkt, sym, "15m", start_ms - 4 * DAY_MS)
        except Exception as e:
            print(f"[long] {sym} 4H-1H-15m data: {e}")
            continue
        for side in (1, -1):
            for t in T.find_trades(sym, k4, k1, k15, side, start_ms):
                if t["res"] != "open":
                    out.append((int(t["time"].timestamp() * 1000), int(t["exit"].timestamp() * 1000),
                                t["r"], 1 if t["side"] == "LONG" else -1))
        print(f"[long] 4H-1H-15m {n}/{len(coins)} {sym}", flush=True)
    return out


def half_day(strat, start_ms, now_ms, offset, title):
    """Your 12-hours-watch / 12-hours-trade rule on these strategies."""
    names = list(strat)
    first = (start_ms // DAY_MS + 1) * DAY_MS + offset
    picked = {"A": [], "C": [], "D": []}
    halves = {"A": [], "C": [], "D": []}              # R of every traded half
    groups_seen = {1: 0, -1: 0}
    used = {n: 0 for n in names}
    w0 = first
    while w0 + 2 * HALF_MS <= now_ms:
        w1, t1 = w0 + HALF_MS, w0 + 2 * HALF_MS
        grp = {1: [], -1: []}
        for n in names:
            done = [x for x in strat[n] if w0 <= x[0] < w1 and x[1] <= w1]   # finished while watching
            if not done or sum(x[2] for x in done) >= 0:
                continue
            lost = {sd: sum(-x[2] for x in done if x[2] < 0 and x[3] == sd) for sd in (1, -1)}
            tot = lost[1] + lost[-1]
            for sd in (1, -1):
                if tot and lost[sd] / tot >= ONE_SIDE:
                    grp[sd].append(n)
        side = 1 if len(grp[1]) >= len(grp[-1]) else -1
        group = grp[side] if len(grp[side]) >= MIN_GROUP else []
        if group:
            groups_seen[side] += 1
        nxt = {n: [x for x in strat[n] if w1 <= x[0] < t1] for n in names}
        sets = {"A": group, "C": [n for n in names if n not in group] if group else [], "D": names}
        for key, use in sets.items():
            tr = [x for n in use for x in nxt[n]]
            picked[key] += tr
            if tr:
                halves[key].append(sum(x[2] for x in tr))
        for n in group:
            used[n] += 1
        w0 += DAY_MS
    total_halves = int((now_ms - first) // DAY_MS)
    lines = [f"<b>{title}</b>",
             f"Same-way failure group found in {groups_seen[1] + groups_seen[-1]} of about "
             f"{total_halves} halves (LONG-failures {groups_seen[1]}, SHORT-failures {groups_seen[-1]})"]
    for key, label in (("A", "A) YOUR RULE — same-way failures"), ("C", "C) the OTHER strategies"),
                       ("D", "D) all strategies every half")):
        tr = picked[key]
        bal, dd, sk = money(tr)
        r = sum(x[2] for x in tr)
        pos = sum(1 for h in halves[key] if h > 0)
        lines.append(f"<b>{label}</b>: {len(tr)} trades, <b>{r:+.1f}R</b>"
                     + (f" ({r / len(tr):+.2f}R per trade)" if tr else "")
                     + f" | halves traded {len(halves[key])}, in profit {pos}"
                     + f" | $1000 → <b>${bal:.0f}</b> (fall -{dd:.0f}%)")
    lines.append("In the group: " + ", ".join(f"{n.split()[0]}:{c}" for n, c in used.items() if c))
    return lines


def main():
    nums = [int(a) for a in sys.argv[1:] if a.isdigit()]
    days = min(max(nums[0] if nums else 180, 30), 180)
    try:
        mkt = S.Market()
    except Exception as e:
        S.telegram_send(f"⚠️ Long test did not run: {e}")
        print(e)
        return 1
    if mkt.mode != "futures":
        S.telegram_send("⚠️ Long test needs Binance FUTURES data. Run it on the server.")
        return 1
    groups = S.backtest_coins(mkt)
    coins = list(groups)
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - days * DAY_MS
    print(f"[long] {len(coins)} coins, {days} days — rule sets G-L…", flush=True)
    gl, _, ncoins, mode = S.gen_trades(mkt, groups, days)
    strat = {}
    for key, trades in gl.items():
        strat[f"{key} {S.VARIANTS[key]['name']}"] = [
            (t[4], t[5], t[3], 1 if t[1] == "LONG" else -1) for t in trades]
    print("[long] 4H → 1H → 15m…", flush=True)
    strat["M 4H→1H→15m (How to Analysis)"] = smc_trades(mkt, coins, start_ms)

    parts = 3
    step = (now_ms - start_ms) / parts
    split = now_ms - step                              # honest check: last part hidden
    res = {}
    for name, tr in strat.items():
        tr = [x for x in tr if start_ms <= x[0] < now_ms]
        bal, dd, sk = money(tr)
        part_r = [sum(r for e, _, r, _ in tr if start_ms + p * step <= e < start_ms + (p + 1) * step)
                  for p in range(parts)]
        res[name] = {"n": len(tr), "win": sum(1 for x in tr if x[2] > 0), "r": sum(x[2] for x in tr),
                     "parts": part_r, "bal": bal, "dd": dd, "skipped": sk,
                     "early": sum(r for e, _, r, _ in tr if e < split),
                     "late": [x for x in tr if x[0] >= split]}

    ist = lambda ms: datetime.fromtimestamp(ms / 1000, S.IST)
    pm = days // parts // 30 or 1
    lines = [f"🕰 <b>Slower setups — last {days // 30} months, {len(coins)} coins "
             f"(Binance {mkt.mode})</b>",
             f"{ist(start_ms):%d %b %Y} → {ist(now_ms):%d %b %Y}. Target 1:2, fees + slippage "
             "included. Money: start $1000 per strategy, 1% of the wallet risked per trade, "
             "max 10 open. 1R = the 1% risked.", ""]
    for name, r in sorted(res.items(), key=lambda x: x[1]["r"], reverse=True):
        good = r["r"] > 0 and all(p > 0 for p in r["parts"])
        icon = "✅" if good else ("➖" if r["r"] > 0 else "❌")
        lines += [f"{icon} <b>{name}</b>",
                  f"   {r['n']} trades | in profit {r['win'] / r['n'] * 100:.0f}% | total "
                  f"<b>{r['r']:+.1f}R</b> | per trade {r['r'] / r['n']:+.2f}R" if r["n"] else "   no trades",
                  f"   each {pm}-month part: " + " / ".join(f"{p:+.1f}R" for p in r["parts"]),
                  f"   $1000 → <b>${r['bal']:.0f}</b> | biggest fall -{r['dd']:.0f}%"
                  + (f" | skipped {r['skipped']}" if r["skipped"] else ""), ""]
    tried = [k for k in res if res[k]["n"] > len(res[k]["late"])] or list(res)
    best = max(tried, key=lambda k: res[k]["early"])
    late = res[best]["late"]
    late_r = sum(x[2] for x in late)
    lines.append(f"🔍 <b>Honest check:</b> best on the first {days * 2 // 3 // 30} months = {best} "
                 f"({res[best]['early']:+.1f}R). In the last {pm} months it never saw: "
                 f"{len(late)} trades, <b>{late_r:+.1f}R</b>" + (" ✅" if late_r > 0 else " ❌"))
    winners = [k for k, r in res.items() if r["r"] > 0 and all(p > 0 for p in r["parts"])]
    lines += ["",
              ("✅ Profit in EVERY part: " + ", ".join(winners)) if winners else
              "❌ No strategy made a profit in every part of the test.",
              "✅ = profit in every 2-month part, ➖ = profit overall but not every part, "
              "❌ = loss. Past results do not guarantee future results."]
    lines += [""] + half_day(strat, start_ms, now_ms, 0,
                             "SEGMENT 1 — watch MORNING (5:30 AM-5:30 PM) → trade EVENING")
    lines += [""] + half_day(strat, start_ms, now_ms, HALF_MS,
                             "SEGMENT 2 — watch EVENING (5:30 PM-5:30 AM) → trade next MORNING")
    lines += ["", "If A is not clearly better than D, watching the first half does not help to choose."]
    msg = "\n".join(lines)
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
