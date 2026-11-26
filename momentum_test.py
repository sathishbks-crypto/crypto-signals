#!/usr/bin/env python3
"""
Momentum LONG test with a ladder exit — Binance FUTURES data only.

THE RULES (your idea, written as exact rules)
  Coin:     any Binance USDT futures coin with at least $5M traded in 24h.
  Setup:    take the lowest price of the last 7 days (the "base").
            1) the coin first rose +30% or more from the base,
            2) then made 1 or 2 corrections (a drop of 10% or more from its high),
            3) then a 1-hour candle CLOSES between +50% and +60% above the base
               (crossing +50% on that candle)  ->  LONG at that close.
  Leverage: 10x on every trade (trade amount = margin, position = 10 x amount).
  Stop-loss: -100% of the trade amount. At 10x Binance would liquidate a little
             before a 10% drop, so the stop sits at -9.5% coin price
             (= -95% of the trade, about -100% with fees).
  Ladder:   every +10% coin move = another +100% of the trade amount ->
            close HALF of what is still open; the rest keeps running.
            The stop-loss stays where it started (as you asked).
  One new trade per day (5:30 AM to 5:30 AM IST): the first coin that sets up.
  Several trades can be open at the same time.

  Money:    start $100. Trade amount $10 per $100 of balance:
            balance $100–199 -> $10, $200–299 -> $20, $300–399 -> $30 ...
            (below $100 it stays $10).
            "Trade amount" = what is lost if the stop-loss hits (-100%).
  Costs:    fees + slippage 0.15% of the position, and the real Binance
            funding fees for the hours the trade was open.

  Ladder step can be changed (2nd number, in %):
            100  = close half at +100%, +200%, +300% ...      (default)
            1000 = close half only at +1000%, then +2000% ... (big-move hunting:
                   lose $10 or make about $100 on a $10 trade)

Usage (on the server, in the bot folder):
  python3 momentum_test.py              -> last 30 days, ladder every +100%
  python3 momentum_test.py 90           -> last 90 days (up to 180)
  python3 momentum_test.py 180 1000     -> 6 months, half closed only at +1000% steps
"""

import collections
import sys
import time
from datetime import datetime

import scanner as S
import smc_today as T

START_BALANCE = 100.0
STEP_BALANCE, STEP_AMOUNT = 100.0, 10.0     # $10 per $100 of balance
BASE_HOURS = 7 * 24                          # "last 7 days" for the base low
FIRST_LEG = 0.30                             # +30% first move
CORRECTION = 0.10                            # a drop of 10%+ from the high = 1 correction
MIN_CORR, MAX_CORR = 1, 2
TRIGGER_LO, TRIGGER_HI = 0.50, 0.60          # entry when close is +50% to +60% above base
LEVERAGE = 10.0
STEP_MOVE = 1 / LEVERAGE                     # +10% coin move = +100% of the trade amount
STOP_MOVE = 0.095                            # stop just before 10x liquidation
LADDER_STEP_R = 1.0                          # 1 = +100% steps; 10 = +1000% steps (set from command)
COST_PCT = 0.15                              # fees + slippage, % of position
MIN_VOLUME = 5_000_000
H_MS = 3600 * 1000


# ---------------------------------------------------------------------------
# Find setups on one coin (1h candles). Only past candles are used at each hour.
# ---------------------------------------------------------------------------
def setups(sym, k, start_ms):
    o, h, l, c, t, tc = k["o"], k["h"], k["l"], k["c"], k["t"], k["tc"]
    found = []
    dq = collections.deque()                        # rolling lowest low of the last 7 days
    for i in range(BASE_HOURS, len(c)):
        if not dq:
            for x in range(i - BASE_HOURS, i):
                while dq and l[dq[-1]] >= l[x]:
                    dq.pop()
                dq.append(x)
        else:
            x = i - 1
            while dq and l[dq[-1]] >= l[x]:
                dq.pop()
            dq.append(x)
        while dq[0] < i - BASE_HOURS:
            dq.popleft()
        if t[i] < start_ms:
            continue
        lo_i = dq[0]
        base = l[lo_i]
        if base <= 0:
            continue
        gain_now, gain_prev = c[i] / base - 1, c[i - 1] / base - 1
        if not (TRIGGER_LO <= gain_now <= TRIGGER_HI and gain_prev < TRIGGER_LO):
            continue
        # walk from the base to now: first leg, then corrections
        leg_done, high, corrections, in_corr, trough = False, base, 0, False, None
        for x in range(lo_i, i):
            if not leg_done:
                if h[x] >= base * (1 + FIRST_LEG):
                    leg_done, high = True, h[x]
                continue
            if h[x] > high:
                high, in_corr = h[x], False
            if l[x] <= high * (1 - CORRECTION):
                if not in_corr:
                    corrections += 1
                    in_corr, trough = True, l[x]
                trough = min(trough, l[x])
        if not leg_done or not (MIN_CORR <= corrections <= MAX_CORR):
            continue
        entry = c[i]
        found.append({"sym": sym, "i": i, "ms": tc[i], "entry": entry,
                      "stop": entry * (1 - STOP_MOVE), "dist": STEP_MOVE,
                      "base": base, "gain": gain_now, "corr": corrections})
    return found


# ---------------------------------------------------------------------------
# Ladder: returns the trade's events in "R" (1R = the trade amount)
# ---------------------------------------------------------------------------
def ladder(k, s):
    e, stop = s["entry"], s["stop"]
    risk = e * STEP_MOVE                            # price move that = 100% of the trade
    stop_r = -(e - stop) / risk                     # -0.95
    rem, step, events = 1.0, 1, []
    for x in range(s["i"] + 1, len(k["c"])):
        s["best_r"] = max(s.get("best_r", 0.0), (k["h"][x] - e) / risk)
        if k["l"][x] <= stop:                       # stop first if both in one candle
            events.append((k["tc"][x], rem, stop_r, "stop"))
            return events, 0.0, k["tc"][x], 0.0
        while k["h"][x] >= e + step * LADDER_STEP_R * risk:
            half = rem / 2 if rem > 0.02 else rem      # last tiny piece: close it all
            lvl = step * LADDER_STEP_R
            events.append((k["tc"][x], half, lvl, f"+{lvl * 100:.0f}%"))
            rem -= half
            step += 1
            if rem <= 1e-9:
                return events, 0.0, k["tc"][x], 0.0
    last = len(k["c"]) - 1
    r_now = (k["c"][last] - e) / risk
    return events, rem, k["tc"][last], r_now


def funding_rates(mkt, s, end_ms):
    """Real Binance funding rates while the trade was open: [(time_ms, rate)]."""
    try:
        rows = S.http_get(mkt.host + "/fapi/v1/fundingRate",
                          {"symbol": s["sym"], "startTime": s["ms"], "endTime": end_ms, "limit": 1000})
        return [(int(r["fundingTime"]), float(r["fundingRate"])) for r in rows]
    except Exception as ex:
        print(f"[funding] {s['sym']}: {ex}")
        return []


# ---------------------------------------------------------------------------
def main():
    global LADDER_STEP_R
    nums = [int(a) for a in sys.argv[1:] if a.strip().isdigit()]
    days = min(max(nums[0] if nums else 30, 1), 180)
    if len(nums) > 1:
        LADDER_STEP_R = min(max(nums[1], 100), 5000) / 100
    try:
        mkt = S.Market()
    except Exception as e:
        S.telegram_send(f"⚠️ Momentum test did not run: {e}")
        print(e)
        return 1
    if mkt.mode != "futures":
        S.telegram_send("⚠️ Momentum test needs Binance FUTURES data. Run it on the server.")
        return 1
    now_ms = int(time.time() * 1000)
    start_ms = (now_ms // T.DAY_MS - days + 1) * T.DAY_MS          # 5:30 AM IST
    S.MIN_QUOTE_VOLUME = MIN_VOLUME
    coins = T.all_qualified_coins(mkt)
    print(f"[momentum] {len(coins)} coins, last {days} days", flush=True)

    data, cand = {}, []
    for n, sym in enumerate(coins, 1):
        try:
            k = T.fetch(mkt, sym, "1h", start_ms - (BASE_HOURS + 24) * H_MS)
        except Exception as e:
            print(f"[momentum] {sym}: {e}")
            continue
        if len(k["c"]) < BASE_HOURS + 2:
            continue
        found = setups(sym, k, start_ms)
        if found:
            data[sym] = k
            cand += found
        if n % 50 == 0:
            print(f"[momentum] {n}/{len(coins)} coins checked, {len(cand)} setups", flush=True)

    # one new trade per day: the first setup of the day, coin not already open
    cand.sort(key=lambda s: s["ms"])
    trades, day_used, open_until = [], set(), {}
    for s in cand:
        day = s["ms"] // T.DAY_MS
        if day in day_used or open_until.get(s["sym"], 0) > s["ms"]:
            continue
        events, rem, end_ms, r_open = ladder(data[s["sym"]], s)
        fund = funding_rates(mkt, s, end_ms)
        s.update(events=events, rem=rem, end_ms=end_ms, r_open=r_open, fund=fund)
        trades.append(s)
        day_used.add(day)
        open_until[s["sym"]] = end_ms if rem == 0 else now_ms + 1

    # money: go through every event in time order
    timeline = []
    for n, s in enumerate(trades):
        timeline.append((s["ms"], 0, n, "open", 0, 0))
        for ms, frac, r, why in s["events"]:
            timeline.append((ms, 1, n, why, frac, r))
        for ms, rate in s["fund"]:
            timeline.append((ms, 2, n, "funding", rate, 0))
    timeline.sort()
    bal, peak, max_dd = START_BALANCE, START_BALANCE, 0.0
    for ms, kind, n, why, a, b in timeline:
        s = trades[n]
        if kind == 0:
            s["amount"] = STEP_AMOUNT * max(1, int(bal // STEP_BALANCE))
            s["rem_live"] = 1.0
            s["pnl"] = -s["amount"] * COST_PCT / 100 / s["dist"]       # fees + slippage
            bal += s["pnl"]
            s["bal_at_entry"] = bal - s["pnl"]
        elif kind == 1:
            usd = s["amount"] * a * b
            s["pnl"] += usd
            s["rem_live"] -= a
            bal += usd
        else:
            if s["rem_live"] > 1e-9:
                usd = -a * s["amount"] / s["dist"] * s["rem_live"]       # long pays when rate > 0
                s["pnl"] += usd
                s["fund_usd"] = s.get("fund_usd", 0.0) + usd
                bal += usd
        peak = max(peak, bal)
        max_dd = max(max_dd, (peak - bal) / peak * 100)
    open_value = 0.0
    for s in trades:
        if s["rem"] > 1e-9:
            s["open_usd"] = s["amount"] * s["rem"] * s["r_open"]
            open_value += s["open_usd"]

    msg = report(trades, days, start_ms, len(coins), bal, open_value, max_dd)
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


def report(trades, days, start_ms, ncoins, bal, open_value, max_dd):
    ist = lambda ms: datetime.fromtimestamp(ms / 1000, S.IST)
    st = LADDER_STEP_R * 100
    lines = [f"🚀 <b>Momentum LONG + ladder (+{st:.0f}% steps) — last {days} days</b>",
             f"{ist(start_ms):%d %b %I:%M %p} → {datetime.now(S.IST):%d %b %I:%M %p} IST | "
             f"{ncoins} futures coins ($5M+ daily volume)",
             "Rules: +30% from 7-day low, 1–2 corrections, entry when a 1h candle closes "
             "+50% to +60% above that low. 10x leverage: stop -100% of the trade (coin -9.5%), "
             f"close half at every +{st:.0f}% (every +{st / 10:.0f}% coin move). "
             "One new trade per day. Start $100, "
             "$10 per $100 of balance. Fees, slippage and real funding included.", ""]
    if not trades:
        lines.append("No coin matched these rules in this period → no trade.")
        return "\n".join(lines)
    short = len(trades) > 40                        # many trades -> one line each
    for n, s in enumerate(trades, 1):
        steps = [ev[3] for ev in s["events"] if ev[3] != "stop"]
        if len(steps) > 4:
            steps = [f"{steps[0]} … {steps[-1]} ({len(steps)} steps)"]
        stopped = any(ev[3] == "stop" for ev in s["events"])
        if s["rem"] > 1e-9:
            end = f"⏳ still open ({s['rem'] * 100:.0f}% of position, now {s.get('open_usd', 0):+.2f}$)"
        elif stopped:
            end = f"🛑 rest closed at stop-loss {ist(s['end_ms']):%d %b %I:%M %p}"
        else:
            end = "fully closed by the ladder"
        icon = "✅" if s["pnl"] + s.get("open_usd", 0) > 0 else "❌"
        if short:
            tag = ("open" if s["rem"] > 1e-9 else "stop" if stopped else "done")
            lines.append(f"{icon} {n}. {s['sym']} {ist(s['ms']):%d %b} ${s['amount']:.0f} | "
                         f"best +{s.get('best_r', 0) * 100:.0f}% | "
                         f"{', '.join(steps) if steps else '-'} | {tag} | <b>{s['pnl']:+.2f}$</b>")
            continue
        lines += [
            f"{icon} <b>{n}. LONG {s['sym']}</b> — {ist(s['ms']):%d %b %I:%M %p} | amount ${s['amount']:.0f}",
            f"Entry {S.fmt_price(s['entry'])} (+{s['gain'] * 100:.0f}% above 7-day low, "
            f"{s['corr']} correction{'s' if s['corr'] > 1 else ''}) | "
            f"Stop {S.fmt_price(s['stop'])} (coin -{STOP_MOVE * 100:.1f}% = trade -100%, 10x)",
            f"Ladder: {', '.join(steps) if steps else f'no +{st:.0f}% step reached'} | {end}"
            f" | best move +{s.get('best_r', 0) * 100:.0f}%",
            f"→ Closed result <b>{s['pnl']:+.2f}$</b>"
            + (f" (funding {s['fund_usd']:+.2f}$)" if abs(s.get("fund_usd", 0)) >= 0.01 else ""), ""]
    wins = sum(1 for s in trades if s["pnl"] + s.get("open_usd", 0) > 0)
    hit = sum(1 for s in trades if any(ev[3] != "stop" for ev in s["events"]))
    reach = [(lvl, sum(1 for s in trades if s.get("best_r", 0) >= lvl / 100))
             for lvl in (100, 300, 500, 1000, 2000)]
    if short:
        lines.append("")
    lines += [
        f"<b>Summary:</b> {len(trades)} trades — ✅ {wins} in profit, ❌ {len(trades) - wins} in loss",
        f"Start balance: $100.00",
        f"Balance with closed results: <b>${bal:.2f}</b> ({(bal / START_BALANCE - 1) * 100:+.1f}%)",
        f"Open trades right now: {open_value:+.2f}$ → if closed now: <b>${bal + open_value:.2f}</b>",
        f"Biggest fall from a high point: -{max_dd:.1f}%",
        f"Trades that reached the first +{st:.0f}% step: <b>{hit} of {len(trades)}</b>",
        "Best move after entry (before stop): "
        + ", ".join(f"+{lvl}%: {n}" for lvl, n in reach),
        "Note: coins that crashed and were removed from Binance are missing from this test, "
        "so real results could be worse. 30 days is a short test — try 90 or 180 days too.",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())
