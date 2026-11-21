#!/usr/bin/env python3
"""
Auto-trader for Binance USD-M Futures.

Uses the SAME signals and rule-set choice as scanner.py, and the rules in
settings.txt (risk per trade, 1:2 target, max open trades, daily loss limit).

Every trade gets, right after entry, a STOP-LOSS and a TAKE-PROFIT order on
Binance itself (so they work even if this server is off).

Commands (run from the bot folder):
  python3 trader.py scan      -> look for new trades (every hour, by cron)
  python3 trader.py monitor   -> check open trades, record results (every 5 min)
  python3 trader.py status    -> send balance + open trades to Telegram
  python3 trader.py check     -> test the API keys (no trading)

Pause new trades:   touch PAUSE      Resume:   rm PAUSE
Secrets are read from secrets.txt (created by setup.sh).
Only the Python standard library is used.
"""

import hashlib
import hmac
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)

SECRETS_FILE = "secrets.txt"
STATE_FILE = "trader_state.json"
PAUSE_FILE = "PAUSE"
LIVE_BASE = "https://fapi.binance.com"
DEMO_BASE = "https://demo-fapi.binance.com"
MAX_ENTRY_SLIPPAGE_PCT = 0.5     # skip if price already moved more than this
MARGIN_SHARE_PER_TRADE = 0.18    # aim to use ~18% of balance as margin per trade
MAX_LEVERAGE = 20


def load_secrets():
    s = {}
    if os.path.exists(SECRETS_FILE):
        with open(SECRETS_FILE, encoding="utf-8") as f:
            for line in f:
                if "=" in line and not line.strip().startswith("#"):
                    k, v = line.split("=", 1)
                    s[k.strip().upper()] = v.strip()
    return s


SECRETS = load_secrets()
for _k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
    if SECRETS.get(_k):
        os.environ[_k] = SECRETS[_k]

import scanner as S  # noqa: E402  (needs the Telegram env above)

MODE = SECRETS.get("MODE", "demo").lower()
BASE = LIVE_BASE if MODE == "live" else DEMO_BASE
TAG = "" if MODE == "live" else "[DEMO] "


def log(*a):
    print(datetime.now(S.IST).strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


def tg(text):
    S.telegram_send(TAG + text)


# ----------------------------------------------------------------------------
# Binance client
# ----------------------------------------------------------------------------
class BinanceError(Exception):
    def __init__(self, code, msg):
        super().__init__(f"Binance error {code}: {msg}")
        self.code = code
        self.msg = msg


class Binance:
    def __init__(self, key, secret, base):
        self.key, self.secret, self.base = key, secret.encode(), base
        self.offset = 0
        try:
            t = self.public("/fapi/v1/time")["serverTime"]
            self.offset = int(t) - int(time.time() * 1000)
        except Exception as e:
            log("time sync failed:", e)

    def _send(self, method, path, params=None, signed=False):
        params = dict(params or {})
        headers = {"User-Agent": "coopercraze-bot"}
        if signed:
            params["timestamp"] = int(time.time() * 1000) + self.offset
            params["recvWindow"] = 10000
            qs = urllib.parse.urlencode(params)
            sig = hmac.new(self.secret, qs.encode(), hashlib.sha256).hexdigest()
            qs += "&signature=" + sig
            headers["X-MBX-APIKEY"] = self.key
        else:
            qs = urllib.parse.urlencode(params)
        url = self.base + path
        data = None
        if method in ("POST", "PUT"):
            data = qs.encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        elif qs:
            url += "?" + qs
        last = None
        for attempt in range(3):
            try:
                req = urllib.request.Request(url, data=data, headers=headers, method=method)
                with urllib.request.urlopen(req, timeout=20) as r:
                    return json.loads(r.read().decode() or "{}")
            except urllib.error.HTTPError as e:
                body = e.read().decode(errors="replace")
                try:
                    j = json.loads(body)
                    raise BinanceError(j.get("code"), j.get("msg"))
                except (ValueError, AttributeError):
                    last = BinanceError(e.code, body[:200])
                    if e.code >= 500 or e.code == 429:
                        time.sleep(2 * (attempt + 1))
                        continue
                    raise last
            except BinanceError:
                raise
            except Exception as e:      # network problem: retry (safe for GET)
                last = e
                if method != "GET":
                    raise
                time.sleep(2 * (attempt + 1))
        raise last

    def public(self, path, params=None):
        return self._send("GET", path, params)

    def get(self, path, params=None):
        return self._send("GET", path, params, True)

    def post(self, path, params=None):
        return self._send("POST", path, params, True)

    def delete(self, path, params=None):
        return self._send("DELETE", path, params, True)


# ----------------------------------------------------------------------------
# State
# ----------------------------------------------------------------------------
def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            st = json.load(f)
    except Exception:
        st = {}
    st.setdefault("open", {})      # symbol -> trade info
    st.setdefault("closed", [])    # finished trades
    st.setdefault("sent", {})      # used by scanner rule-set test
    st.setdefault("last_trade", {})
    st.setdefault("day", {})
    return st


def save_state(st):
    st["closed"] = st["closed"][-500:]
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, indent=1)
    os.replace(tmp, STATE_FILE)


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------
def symbol_filters(bn):
    info = bn.public("/fapi/v1/exchangeInfo")
    out = {}
    for s in info.get("symbols", []):
        if s.get("status") != "TRADING" or s.get("contractType") != "PERPETUAL":
            continue
        f = {x["filterType"]: x for x in s.get("filters", [])}
        lot = f.get("MARKET_LOT_SIZE") or f.get("LOT_SIZE") or {}
        step = float(lot.get("stepSize") or f.get("LOT_SIZE", {}).get("stepSize") or 0)
        if step <= 0:
            step = float(f.get("LOT_SIZE", {}).get("stepSize", 0) or 0)
        out[s["symbol"]] = {
            "step": step,
            "min_qty": float(f.get("LOT_SIZE", {}).get("minQty", 0) or 0),
            "max_qty": float(lot.get("maxQty", 0) or 0) or float("inf"),
            "tick": float(f.get("PRICE_FILTER", {}).get("tickSize", 0) or 0),
            "min_notional": float(f.get("MIN_NOTIONAL", {}).get("notional", 5) or 5),
        }
    return out


def dec_places(step):
    s = f"{step:.12f}".rstrip("0")
    return len(s.split(".")[1]) if "." in s else 0


def floor_step(x, step):
    if step <= 0:
        return x
    return round(math.floor(x / step + 1e-9) * step, dec_places(step))


def round_tick(x, tick):
    if tick <= 0:
        return x
    return round(round(x / tick) * tick, dec_places(tick))


def fmt(x):
    return S.fmt_price(x)


def get_v3(bn, name, params=None):
    """Use the v3 endpoint, fall back to v2 if this Binance server lacks it."""
    try:
        return bn.get(f"/fapi/v3/{name}", params)
    except BinanceError as e:
        if e.code in (404, -5000, -1121) or "Not Found" in str(e.msg):
            return bn.get(f"/fapi/v2/{name}", params)
        raise


def usdt_balance(bn):
    for b in get_v3(bn, "balance"):
        if b.get("asset") == "USDT":
            return float(b["balance"]), float(b["availableBalance"])
    return 0.0, 0.0


def position_amt(bn, sym):
    for p in get_v3(bn, "positionRisk", {"symbol": sym}):
        if p.get("positionSide", "BOTH") in ("BOTH", "LONG", "SHORT"):
            amt = float(p.get("positionAmt", 0))
            if amt != 0:
                return amt
    return 0.0


def all_positions(bn):
    return {p["symbol"]: float(p["positionAmt"]) for p in get_v3(bn, "positionRisk")
            if float(p.get("positionAmt", 0)) != 0}


def place_stop(bn, sym, close_side, price, kind):
    return bn.post("/fapi/v1/algoOrder", {
        "algoType": "CONDITIONAL", "symbol": sym, "side": close_side,
        "type": kind, "triggerPrice": price, "closePosition": "true",
        "workingType": "MARK_PRICE", "priceProtect": "true",
    })


def market_close(bn, sym, amt):
    side = "SELL" if amt > 0 else "BUY"
    return bn.post("/fapi/v1/order", {
        "symbol": sym, "side": side, "type": "MARKET",
        "quantity": abs(amt), "reduceOnly": "true",
    })


def cancel_algos(bn, sym):
    try:
        bn.delete("/fapi/v1/algoOpenOrders", {"symbol": sym})
    except BinanceError as e:
        log(f"{sym}: cancel algo orders: {e}")


def today_ist():
    return datetime.now(S.IST).strftime("%Y-%m-%d")


def day_result_pct(st):
    d = st["day"]
    if not d.get("start_bal"):
        return 0.0
    pnl = sum(t.get("pnl", 0.0) for t in st["closed"] if t.get("closed_day") == d.get("date"))
    return pnl / d["start_bal"] * 100


def ensure_one_way(bn):
    try:
        if bn.get("/fapi/v1/positionSide/dual").get("dualSidePosition"):
            bn.post("/fapi/v1/positionSide/dual", {"dualSidePosition": "false"})
            log("switched account to One-way position mode")
    except BinanceError as e:
        tg(f"⚠️ Please set Binance Futures to <b>One-way mode</b> (Settings → Position Mode). {e.msg}")
        raise


# ----------------------------------------------------------------------------
# Monitor: record finished trades, enforce time limit, keep stop-loss in place
# ----------------------------------------------------------------------------
def monitor(bn, st):
    hold_ms = S.BACKTEST_MAX_BARS * 60 * 60 * 1000          # 48 x 1h candles
    for sym, t in list(st["open"].items()):
        try:
            amt = position_amt(bn, sym)
        except Exception as e:
            log(f"{sym}: position check failed: {e}")
            continue
        now_ms = int(time.time() * 1000)

        if amt == 0:                                         # closed by SL / TP / us
            cancel_algos(bn, sym)
            pnl = 0.0
            try:
                for inc in bn.get("/fapi/v1/income", {"symbol": sym, "startTime": t["opened_ms"] - 1000,
                                                      "limit": 1000}):
                    if inc.get("incomeType") in ("REALIZED_PNL", "COMMISSION", "FUNDING_FEE"):
                        pnl += float(inc.get("income", 0))
            except Exception as e:
                log(f"{sym}: income lookup failed: {e}")
            r = pnl / t["risk_usd"] if t.get("risk_usd") else 0.0
            t.update(pnl=pnl, r=r, closed_ms=now_ms, closed_day=today_ist())
            st["closed"].append(t)
            del st["open"][sym]
            bal, _ = usdt_balance(bn)
            why = t.get("close_reason") or ("TARGET HIT" if r > 0.5 else "STOP-LOSS HIT" if r < -0.5 else "CLOSED")
            tg(f"{'✅' if pnl > 0 else '❌'} <b>Closed {t['side']} {sym}</b> — {why}\n"
               f"Result: <b>{pnl:+.2f} USDT</b> ({r * 100:+.0f}% of the trade amount)\n"
               f"Balance: {bal:,.2f} USDT  |  Today: {day_result_pct(st):+.2f}%")
            log(f"{sym}: closed pnl {pnl:+.4f}")
            continue

        if now_ms - t["opened_ms"] > hold_ms:                # time limit reached
            try:
                market_close(bn, sym, amt)
                t["close_reason"] = f"CLOSED AFTER {S.BACKTEST_MAX_BARS}H (time limit)"
                cancel_algos(bn, sym)
                log(f"{sym}: closed at time limit")
            except Exception as e:
                log(f"{sym}: time-limit close failed: {e}")
            continue

        try:                                                 # make sure stop-loss exists
            algos = bn.get("/fapi/v1/openAlgoOrders", {"symbol": sym, "algoType": "CONDITIONAL"})
            if not any(o.get("orderType") == "STOP_MARKET" for o in algos):
                close_side = "SELL" if amt > 0 else "BUY"
                try:
                    place_stop(bn, sym, close_side, t["sl"], "STOP_MARKET")
                    tg(f"⚠️ Stop-loss for {sym} was missing — placed again at {fmt(t['sl'])}.")
                except BinanceError as e:
                    market_close(bn, sym, amt)
                    t["close_reason"] = "CLOSED (stop-loss could not be placed)"
                    tg(f"⚠️ Stop-loss for {sym} could not be placed ({e.msg}). Position closed for safety.")
        except Exception as e:
            log(f"{sym}: algo order check failed: {e}")


# ----------------------------------------------------------------------------
# Scan: find new signals and open trades
# ----------------------------------------------------------------------------
def open_trade(bn, st, filters, sig, group, bal, avail):
    sym, d = sig["symbol"], sig["d"]
    f = filters.get(sym)
    if not f:
        log(f"{sym}: not tradable on this Binance account/mode")
        return False

    mark = float(bn.public("/fapi/v1/premiumIndex", {"symbol": sym})["markPrice"])
    if abs(mark - sig["entry"]) / sig["entry"] * 100 > MAX_ENTRY_SLIPPAGE_PCT:
        log(f"{sym}: price moved too far from signal ({mark} vs {sig['entry']}), skipped")
        return False
    if (d == 1 and mark <= sig["sl"]) or (d == -1 and mark >= sig["sl"]):
        return False

    risk_usd = bal * S.SET["risk_per_trade"] / 100
    stop_dist = abs(mark - sig["sl"])
    qty = floor_step(risk_usd / stop_dist, f["step"])
    qty = min(qty, f["max_qty"])
    notional = qty * mark
    if qty < f["min_qty"] or notional < f["min_notional"]:
        log(f"{sym}: trade too small for Binance minimum "
            f"({notional:.2f} < {f['min_notional']} USDT) — balance too low for this coin")
        return False

    stop_pct = stop_dist / mark * 100
    lev_safe = max(1, min(MAX_LEVERAGE, int(70 / stop_pct)))      # liquidation beyond stop
    lev_need = max(1, math.ceil(notional / max(bal * MARGIN_SHARE_PER_TRADE, 1e-9)))
    lev = max(int(S.SET.get("leverage", 3)), lev_need)
    lev = min(lev, lev_safe)
    margin = notional / lev
    if margin > avail * 0.95:
        log(f"{sym}: not enough free margin ({margin:.2f} needed, {avail:.2f} free)")
        return False

    try:
        bn.post("/fapi/v1/marginType", {"symbol": sym, "marginType": "ISOLATED"})
    except BinanceError as e:
        if e.code != -4046:          # -4046 = already isolated
            log(f"{sym}: margin type: {e}")
    try:
        r = bn.post("/fapi/v1/leverage", {"symbol": sym, "leverage": lev})
        lev = int(r.get("leverage", lev))
    except BinanceError as e:
        log(f"{sym}: leverage {lev} refused: {e}")
        return False

    side = "BUY" if d == 1 else "SELL"
    close_side = "SELL" if d == 1 else "BUY"
    order = bn.post("/fapi/v1/order", {"symbol": sym, "side": side, "type": "MARKET",
                                       "quantity": qty, "newOrderRespType": "RESULT"})
    fill = float(order.get("avgPrice") or 0) or mark
    filled_qty = float(order.get("executedQty") or qty)
    opened_ms = int(time.time() * 1000)

    sl = round_tick(sig["sl"], f["tick"])
    tp = round_tick(fill + d * abs(fill - sl) * sig["tp2_r"], f["tick"])
    try:
        place_stop(bn, sym, close_side, sl, "STOP_MARKET")
    except BinanceError as e:                            # never keep a trade without stop
        try:
            market_close(bn, sym, d * filled_qty)
        finally:
            tg(f"⚠️ {sym}: stop-loss order failed ({e.msg}). Trade closed immediately for safety.")
        return False
    try:
        place_stop(bn, sym, close_side, tp, "TAKE_PROFIT_MARKET")
    except BinanceError as e:
        tg(f"⚠️ {sym}: take-profit order failed ({e.msg}). Stop-loss is in place; "
           f"please set the target {fmt(tp)} manually.")

    real_risk = abs(fill - sl) * filled_qty
    st["open"][sym] = {
        "symbol": sym, "side": sig["side"], "entry": fill, "sl": sl, "tp": tp,
        "qty": filled_qty, "risk_usd": real_risk, "risk_pct": real_risk / bal * 100,
        "leverage": lev, "opened_ms": opened_ms, "opened_ist": datetime.now(S.IST).strftime("%Y-%m-%d %H:%M"),
        "group": group, "rule": st.get("active", {}).get("variant"),
    }
    st["last_trade"][sym] = time.time()
    emoji = "🟢" if d == 1 else "🔴"
    tg(f"{emoji} <b>Opened {sig['side']} {sym}</b>  <i>{group}</i>\n"
       f"Entry: {fmt(fill)}\n"
       f"Stop-loss: {fmt(sl)} ({S.pct(fill, sl):+.2f}%)\n"
       f"Target: {fmt(tp)} ({S.pct(fill, tp):+.2f}%)  [1:{sig['tp2_r']:g}]\n"
       f"Risk: {real_risk:.2f} USDT ({real_risk / bal * 100:.2f}% of balance)  |  "
       f"Size: {filled_qty:g} ({filled_qty * fill:,.2f} USDT), {lev}x isolated\n"
       "Reasons:\n" + "\n".join(f"• {x}" for x in sig["reasons"]))
    log(f"{sym}: OPENED {sig['side']} qty {filled_qty} @ {fill} SL {sl} TP {tp} lev {lev}")
    return True


def scan(bn, st):
    monitor(bn, st)
    save_state(st)

    bal, avail = usdt_balance(bn)
    today = today_ist()
    if st["day"].get("date") != today:
        st["day"] = {"date": today, "start_bal": bal, "stopped": False}
    if bal <= 0:
        log("no USDT balance in the Futures wallet")
        return

    if os.path.exists(PAUSE_FILE):
        log("PAUSE file present: no new trades")
        return

    day_pct = day_result_pct(st)
    if day_pct - S.SET["risk_per_trade"] < -S.SET["daily_loss_limit"] - 1e-9:
        if not st["day"].get("stopped"):
            st["day"]["stopped"] = True
            tg(f"🛑 <b>Daily loss limit reached ({day_pct:+.2f}% today).</b> "
               "No new trades until tomorrow (12:00 AM IST). Open trades keep their stop-loss and target.")
        return

    active = st.get("active")
    if (not active or time.time() - active.get("updated", 0) > 24 * 3600
            or active.get("rr", "auto") != S.RR_KEY):
        log("testing rule sets (daily)")
        S.backtest_and_choose(st, announce=False)
        save_state(st)
        active = st["active"]
    if not active.get("variant") or active["variant"] not in S.VARIANTS:
        log("paused: no profitable rule set right now")
        return
    P = S.VARIANTS[active["variant"]]

    filters = symbol_filters(bn)
    positions = all_positions(bn)
    mkt = S.Market()
    groups = S.build_watchlist(mkt)
    btc = S.indicators(mkt.klines("BTCUSDT", P["trend_tf"], 500))
    jb = len(btc["c"]) - 1
    opened = 0
    for sym, group in groups.items():
        if len(st["open"]) >= S.SET["max_open_trades"]:
            log("max open trades reached")
            break
        if sym in st["open"] or sym in positions:
            continue
        if time.time() - st["last_trade"].get(sym, 0) < S.COOLDOWN_BARS * S.TF_MIN[P["tf"]] * 60:
            continue
        try:
            m = S.indicators(mkt.klines(sym, P["tf"], 300))
            h = btc if sym == "BTCUSDT" else S.indicators(mkt.klines(sym, P["trend_tf"], 500))
        except Exception as e:
            log(f"{sym}: data error {e}")
            continue
        i, j = len(m["c"]) - 1, len(h["c"]) - 1
        if i < 60 or j < 210 or time.time() * 1000 - m["tc"][i] > 20 * 60 * 1000:
            continue
        for side in (1, -1):
            sig = S.evaluate(sym, m, i, h, j, btc, jb, side, P)
            if not sig:
                continue
            open_risk = sum(t.get("risk_pct", S.SET["risk_per_trade"]) for t in st["open"].values())
            if day_pct - (open_risk + S.SET["risk_per_trade"]) < -S.SET["daily_loss_limit"] - 1e-9:
                log(f"{sym}: skipped, could break today's loss limit")
                break
            try:
                if open_trade(bn, st, filters, sig, group, bal, avail):
                    opened += 1
                    bal, avail = usdt_balance(bn)
                    save_state(st)
            except BinanceError as e:
                log(f"{sym}: order failed: {e}")
                tg(f"⚠️ Could not open {sig['side']} {sym}: {e.msg}")
            except Exception as e:
                log(f"{sym}: unexpected error: {e}")
            break
    log(f"scan done, opened {opened}")


def status(bn, st):
    bal, avail = usdt_balance(bn)
    lines = [f"📋 <b>Bot status</b> ({'LIVE' if MODE == 'live' else 'DEMO'})",
             f"Balance: {bal:,.2f} USDT (free {avail:,.2f})",
             f"Today: {day_result_pct(st):+.2f}%",
             f"Open trades: {len(st['open'])}"]
    for t in st["open"].values():
        lines.append(f"• {t['side']} {t['symbol']} entry {fmt(t['entry'])}, SL {fmt(t['sl'])}, TP {fmt(t['tp'])}")
    closed = [t for t in st["closed"] if t.get("closed_day", "")[:7] == today_ist()[:7]]
    if closed:
        w = sum(1 for t in closed if t.get("pnl", 0) > 0)
        lines.append(f"This month: {len(closed)} closed, {w} won, "
                     f"P/L {sum(t.get('pnl', 0) for t in closed):+.2f} USDT")
    if os.path.exists(PAUSE_FILE):
        lines.append("⏸ New trades are PAUSED (PAUSE file).")
    tg("\n".join(lines))
    print("\n".join(lines))


def check(bn):
    bal, avail = usdt_balance(bn)
    ensure_one_way(bn)
    msg = (f"✅ Binance connected ({'LIVE' if MODE == 'live' else 'DEMO'}). "
           f"Futures balance: {bal:,.2f} USDT (free {avail:,.2f}).")
    tg(msg)
    print(msg)


def main():
    cmd = (sys.argv[1] if len(sys.argv) > 1 else "scan").lower()
    if not SECRETS.get("API_KEY") or not SECRETS.get("API_SECRET"):
        print("secrets.txt is missing API_KEY / API_SECRET. Run: bash setup.sh")
        return 1
    bn = Binance(SECRETS["API_KEY"], SECRETS["API_SECRET"], BASE)
    st = load_state()
    try:
        if cmd == "check":
            check(bn)
        elif cmd == "monitor":
            monitor(bn, st)
        elif cmd == "status":
            status(bn, st)
        else:
            ensure_one_way(bn)
            scan(bn, st)
    except BinanceError as e:
        log("Binance error:", e)
        if e.code in (-2014, -2015, -1022):
            tg(f"⚠️ Binance rejected the API key ({e.msg}). Check the key, its permissions "
               "and the IP whitelist.")
    finally:
        save_state(st)
    return 0


if __name__ == "__main__":
    sys.exit(main())
