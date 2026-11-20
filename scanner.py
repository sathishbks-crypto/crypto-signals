#!/usr/bin/env python3
"""
Crypto futures signal scanner (signals only - no auto trading).

Strategy (trend pullback, multi-timeframe):
  * 1h chart  -> decides the direction (trend must be clear and strong)
  * 15m chart -> waits for a pullback to EMA20 and a confirmed bounce
  * BTC 1h    -> must not be moving against the trade
Only signals that pass EVERY check are sent. No "skip" / "no trade" messages.

Coins scanned each run (max 40):
  * your coins in favorites.txt (one per line, e.g. NEARUSDT or NEAR)
  * top 10 gainers of the last 24h
  * top 10 losers of the last 24h

Usage:
  python scanner.py scan       -> normal run (used every 15 min by GitHub Actions)
  python scanner.py backtest   -> tests the same rules on past data and sends the win rate
  python scanner.py test       -> sends one test message to Telegram

Only the Python standard library is used, so nothing needs to be installed.
"""

import bisect
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

# ----------------------------------------------------------------------------
# SETTINGS (safe to change)
# ----------------------------------------------------------------------------
FAV_FILE = "favorites.txt"
STATE_FILE = "state.json"
TOP_N = 10                      # top gainers / top losers to add
MIN_QUOTE_VOLUME = 20_000_000   # gainers/losers need >= 20M USDT 24h volume
COOLDOWN_HOURS = 4              # no repeat signal for the same coin within 4h
TP1_R = 1.0                     # Target 1 = 1x risk
TP2_R = 2.0                     # Target 2 = 2x risk
MIN_RISK_PCT = 0.4              # stop closer than 0.4% = too tight (fees/noise)
MAX_RISK_PCT = 4.0              # stop wider than 4% = too risky, skip
BACKTEST_MAX_BARS = 48          # backtest: close trade after 12h (48 x 15m)
FEE_PCT_ROUND_TRIP = 0.10       # backtest: taker fees in + out

DEFAULT_FAVORITES = [
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT",
    "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "LINKUSDT", "DOTUSDT",
    "NEARUSDT", "LTCUSDT", "TRXUSDT", "SUIUSDT", "APTUSDT",
    "ARBUSDT", "OPUSDT", "INJUSDT", "ATOMUSDT", "FILUSDT",
]

STABLE_BASES = {
    "USDC", "FDUSD", "TUSD", "BUSD", "USDP", "DAI", "USDE", "EUR", "PAXG",
    "AEUR", "XUSD", "USD1", "BFUSD", "RLUSD", "EURI",
}

IST = timezone(timedelta(hours=5, minutes=30))
UA = {"User-Agent": "Mozilla/5.0 (signal-scanner)"}

FUTURES_HOSTS = ["https://fapi.binance.com", "https://fapi1.binance.com",
                 "https://fapi2.binance.com"]
SPOT_HOST = "https://data-api.binance.vision"


# ----------------------------------------------------------------------------
# HTTP helpers
# ----------------------------------------------------------------------------
class HttpError(Exception):
    def __init__(self, code, msg=""):
        super().__init__(f"HTTP {code} {msg}")
        self.code = code


def http_get(url, params=None, retries=3, timeout=20):
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            last = HttpError(e.code, url)
            # 451/403 = region blocked -> do not retry, caller switches source
            if e.code in (403, 451, 400, 404):
                raise last
            if e.code == 429 or e.code == 418:
                time.sleep(5 * (attempt + 1))
            else:
                time.sleep(1.5 * (attempt + 1))
        except Exception as e:  # network hiccup
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise last


# ----------------------------------------------------------------------------
# Market data source: Binance Futures, falls back to Binance spot data API
# (GitHub's US servers are often blocked by Binance Futures with HTTP 451).
# ----------------------------------------------------------------------------
class Market:
    def __init__(self):
        self.mode = None
        self.host = None
        for h in FUTURES_HOSTS:
            try:
                http_get(h + "/fapi/v1/ping", retries=1, timeout=10)
                self.mode, self.host = "futures", h
                break
            except Exception:
                continue
        if self.mode is None:
            http_get(SPOT_HOST + "/api/v3/ping", retries=3)
            self.mode, self.host = "spot", SPOT_HOST
        print(f"[data] using {self.mode} data from {self.host}")

    def kline_limit_max(self):
        return 1500 if self.mode == "futures" else 1000

    def klines(self, symbol, interval, limit):
        path = "/fapi/v1/klines" if self.mode == "futures" else "/api/v3/klines"
        raw = http_get(self.host + path,
                       {"symbol": symbol, "interval": interval, "limit": limit})
        now_ms = int(time.time() * 1000)
        rows = [r for r in raw if int(r[6]) < now_ms]  # closed candles only
        return {
            "t": [int(r[0]) for r in rows],
            "tc": [int(r[6]) for r in rows],
            "o": [float(r[1]) for r in rows],
            "h": [float(r[2]) for r in rows],
            "l": [float(r[3]) for r in rows],
            "c": [float(r[4]) for r in rows],
            "v": [float(r[7]) for r in rows],  # quote volume (USDT)
        }

    def tradable_symbols(self):
        if self.mode == "futures":
            info = http_get(self.host + "/fapi/v1/exchangeInfo")
            return {s["symbol"] for s in info["symbols"]
                    if s.get("quoteAsset") == "USDT"
                    and s.get("contractType") == "PERPETUAL"
                    and s.get("status") == "TRADING"}
        return None  # spot mode: filter by name + volume instead

    def tickers_24h(self):
        path = "/fapi/v1/ticker/24hr" if self.mode == "futures" else "/api/v3/ticker/24hr"
        return http_get(self.host + path)


def is_clean_usdt_symbol(sym):
    if not sym.endswith("USDT"):
        return False
    base = sym[:-4]
    if not base or base in STABLE_BASES:
        return False
    for bad in ("UP", "DOWN", "BULL", "BEAR"):
        if base.endswith(bad) and len(base) > len(bad):
            return False
    return True


# ----------------------------------------------------------------------------
# Coin list
# ----------------------------------------------------------------------------
def load_favorites():
    favs = []
    if os.path.exists(FAV_FILE):
        with open(FAV_FILE, encoding="utf-8") as f:
            text = f.read()
        cleaned = "\n".join(line.split("#")[0] for line in text.splitlines())
        for part in cleaned.replace(",", "\n").replace(" ", "\n").replace("\t", "\n").splitlines():
            s = part.strip().upper().replace("/", "").replace("-", "")
            if s.endswith(".P"):
                s = s[:-2]
            if not s:
                continue
            if not s.endswith("USDT"):
                s += "USDT"
            if s not in favs:
                favs.append(s)
    if not favs:
        favs = list(DEFAULT_FAVORITES)
        print("[coins] favorites.txt missing/empty -> using default list")
    return favs


def build_watchlist(mkt):
    favs = load_favorites()
    tradable = None
    try:
        tradable = mkt.tradable_symbols()
    except Exception as e:
        print("[coins] exchangeInfo failed:", e)

    tickers = mkt.tickers_24h()
    rows = []
    for t in tickers:
        sym = t.get("symbol", "")
        if not is_clean_usdt_symbol(sym):
            continue
        if tradable is not None and sym not in tradable:
            continue
        try:
            qv = float(t["quoteVolume"])
            chg = float(t["priceChangePercent"])
        except (KeyError, ValueError, TypeError):
            continue
        rows.append((sym, chg, qv))

    valid = {r[0] for r in rows}
    favs_ok = [s for s in favs if s in valid]
    dropped = [s for s in favs if s not in valid]
    if dropped:
        print("[coins] not found / not tradable, ignored:", ", ".join(dropped))

    liquid = [r for r in rows if r[2] >= MIN_QUOTE_VOLUME and r[0] not in favs_ok]
    gainers = [r[0] for r in sorted(liquid, key=lambda r: r[1], reverse=True)[:TOP_N]]
    losers_pool = [r for r in liquid if r[0] not in gainers]
    losers = [r[0] for r in sorted(losers_pool, key=lambda r: r[1])[:TOP_N]]

    groups = {}
    for s in favs_ok:
        groups[s] = "Your list"
    for s in gainers:
        groups.setdefault(s, "Top 24h gainer")
    for s in losers:
        groups.setdefault(s, "Top 24h loser")
    return groups


# ----------------------------------------------------------------------------
# Indicators (pure Python)
# ----------------------------------------------------------------------------
def ema(vals, n):
    out = [None] * len(vals)
    if len(vals) < n:
        return out
    k = 2.0 / (n + 1)
    s = sum(vals[:n]) / n
    out[n - 1] = s
    for i in range(n, len(vals)):
        s = vals[i] * k + s * (1 - k)
        out[i] = s
    return out


def sma(vals, n):
    out = [None] * len(vals)
    if len(vals) < n:
        return out
    s = sum(vals[:n])
    out[n - 1] = s / n
    for i in range(n, len(vals)):
        s += vals[i] - vals[i - n]
        out[i] = s / n
    return out


def rsi(c, n=14):
    out = [None] * len(c)
    if len(c) <= n:
        return out
    g = l = 0.0
    for i in range(1, n + 1):
        d = c[i] - c[i - 1]
        g += max(d, 0.0)
        l += max(-d, 0.0)
    ag, al = g / n, l / n
    out[n] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    for i in range(n + 1, len(c)):
        d = c[i] - c[i - 1]
        ag = (ag * (n - 1) + max(d, 0.0)) / n
        al = (al * (n - 1) + max(-d, 0.0)) / n
        out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def atr(h, l, c, n=14):
    out = [None] * len(c)
    if len(c) <= n:
        return out
    tr = [h[0] - l[0]] + [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
                          for i in range(1, len(c))]
    a = sum(tr[1:n + 1]) / n
    out[n] = a
    for i in range(n + 1, len(c)):
        a = (a * (n - 1) + tr[i]) / n
        out[i] = a
    return out


def adx(h, l, c, n=14):
    size = len(c)
    out = [None] * size
    if size <= 2 * n + 1:
        return out
    pdm, mdm, tr = [0.0] * size, [0.0] * size, [0.0] * size
    for i in range(1, size):
        up = h[i] - h[i - 1]
        dn = l[i - 1] - l[i]
        pdm[i] = up if (up > dn and up > 0) else 0.0
        mdm[i] = dn if (dn > up and dn > 0) else 0.0
        tr[i] = max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
    st, sp, sm = sum(tr[1:n + 1]), sum(pdm[1:n + 1]), sum(mdm[1:n + 1])
    dx = [None] * size

    def calc_dx(st_, sp_, sm_):
        if st_ <= 0:
            return 0.0
        pdi, mdi = 100 * sp_ / st_, 100 * sm_ / st_
        return 0.0 if pdi + mdi == 0 else 100 * abs(pdi - mdi) / (pdi + mdi)

    dx[n] = calc_dx(st, sp, sm)
    for i in range(n + 1, size):
        st = st - st / n + tr[i]
        sp = sp - sp / n + pdm[i]
        sm = sm - sm / n + mdm[i]
        dx[i] = calc_dx(st, sp, sm)
    a = sum(dx[n:2 * n]) / n
    out[2 * n - 1] = a
    for i in range(2 * n, size):
        a = (a * (n - 1) + dx[i]) / n
        out[i] = a
    return out


def macd_hist(c, fast=12, slow=26, sig=9):
    ef, es = ema(c, fast), ema(c, slow)
    line = [None if (a is None or b is None) else a - b for a, b in zip(ef, es)]
    start = next((i for i, v in enumerate(line) if v is not None), None)
    out = [None] * len(c)
    if start is None:
        return out
    s = ema(line[start:], sig)
    for k, v in enumerate(s):
        if v is not None:
            out[start + k] = line[start + k] - v
    return out


def indicators(k):
    c, h, l, v = k["c"], k["h"], k["l"], k["v"]
    d = dict(k)
    d["ema20"] = ema(c, 20)
    d["ema50"] = ema(c, 50)
    d["ema200"] = ema(c, 200)
    d["rsi"] = rsi(c, 14)
    d["atr"] = atr(h, l, c, 14)
    d["adx"] = adx(h, l, c, 14)
    d["hist"] = macd_hist(c)
    d["vsma"] = sma(v, 20)
    return d


# ----------------------------------------------------------------------------
# The strategy (same function for live scan and backtest)
# ----------------------------------------------------------------------------
def _ok(*vals):
    return all(x is not None for x in vals)


def evaluate(sym, m15, i, h1, j, btc, jb, side):
    """Return a signal dict, or None if any check fails.
    m15/h1/btc: indicator dicts; i/j/jb: index of the last CLOSED candle to use.
    side: +1 LONG, -1 SHORT."""
    d = side
    if i < 30 or j < 10:
        return None

    # ---- 1h trend ------------------------------------------------------
    c1, e50, e200 = h1["c"][j], h1["ema50"][j], h1["ema200"][j]
    e50_prev = h1["ema50"][j - 5]
    adx1, rsi1, atr1 = h1["adx"][j], h1["rsi"][j], h1["atr"][j]
    if not _ok(c1, e50, e200, e50_prev, adx1, rsi1, atr1) or atr1 <= 0:
        return None
    if not (d * (c1 - e50) > 0 and d * (e50 - e200) > 0):
        return None                                   # trend not aligned
    if d * (e50 - e50_prev) <= 0:
        return None                                   # EMA50 not sloping our way
    if adx1 < 20:
        return None                                   # trend too weak / choppy
    if d == 1 and not (50 <= rsi1 <= 70):
        return None
    if d == -1 and not (30 <= rsi1 <= 50):
        return None
    if d * (c1 - e50) > 2.5 * atr1:
        return None                                   # too extended, late entry

    # ---- BTC filter ----------------------------------------------------
    if sym != "BTCUSDT" and btc is not None:
        bc, be, br = btc["c"][jb], btc["ema50"][jb], btc["rsi"][jb]
        if not _ok(bc, be, br):
            return None
        if d == 1 and not (bc > be and br > 45):
            return None
        if d == -1 and not (bc < be and br < 55):
            return None

    # ---- 15m entry -----------------------------------------------------
    o, h, l, c, v = m15["o"], m15["h"], m15["l"], m15["c"], m15["v"]
    e20, e50m = m15["ema20"], m15["ema50"]
    a, r, hist, vs = m15["atr"][i], m15["rsi"], m15["hist"], m15["vsma"]
    if not _ok(a, e20[i], e50m[i], r[i], r[i - 1], hist[i], hist[i - 1], vs[i - 1]):
        return None
    if a <= 0 or c[i] <= 0:
        return None
    if d * (e20[i] - e50m[i]) <= 0:
        return None                                   # 15m trend not aligned

    look = range(i - 5, i)                            # last 5 candles before trigger
    if any(e20[k] is None for k in look):
        return None
    if d == 1:
        touched = any(l[k] <= e20[k] + 0.2 * a for k in look)
        held = min(l[i - 5:i + 1]) >= e50m[i] - 0.5 * a
    else:
        touched = any(h[k] >= e20[k] - 0.2 * a for k in look)
        held = max(h[i - 5:i + 1]) <= e50m[i] + 0.5 * a
    if not (touched and held):
        return None                                   # no clean pullback

    if d * (c[i] - o[i]) <= 0 or d * (c[i] - e20[i]) <= 0:
        return None                                   # trigger candle wrong colour/side
    if d == 1 and not c[i] > h[i - 1]:
        return None                                   # no break of previous high
    if d == -1 and not c[i] < l[i - 1]:
        return None
    if abs(c[i] - o[i]) > 1.8 * a:
        return None                                   # spike candle, don't chase

    if d == 1 and not (50 <= r[i] <= 68 and r[i] > r[i - 1]):
        return None
    if d == -1 and not (32 <= r[i] <= 50 and r[i] < r[i - 1]):
        return None
    if d * (hist[i] - hist[i - 1]) <= 0:
        return None                                   # momentum not turning our way
    vol_ratio = v[i] / vs[i - 1] if vs[i - 1] > 0 else 0
    if vol_ratio < 1.2:
        return None                                   # no volume confirmation

    # ---- Stop-loss / targets ------------------------------------------
    entry = c[i]
    if d == 1:
        swing = min(l[i - 5:i + 1]) - 0.2 * a
    else:
        swing = max(h[i - 5:i + 1]) + 0.2 * a
    risk = abs(entry - swing)
    risk = max(risk, 0.8 * a)
    if risk > 2.5 * a:
        return None
    risk_pct = risk / entry * 100
    if risk_pct < MIN_RISK_PCT or risk_pct > MAX_RISK_PCT:
        return None

    sl = entry - d * risk
    tp1 = entry + d * risk * TP1_R
    tp2 = entry + d * risk * TP2_R

    side_word = "uptrend" if d == 1 else "downtrend"
    reasons = [
        f"1h {side_word}: price {'above' if d == 1 else 'below'} EMA50 & EMA200, "
        f"ADX {adx1:.0f} (strong trend)",
        f"15m pullback to EMA20, then {'bullish' if d == 1 else 'bearish'} candle "
        f"broke the previous {'high' if d == 1 else 'low'}",
        f"RSI 15m {r[i]:.0f} {'rising' if d == 1 else 'falling'}, MACD momentum "
        f"turning {'up' if d == 1 else 'down'}",
        f"Volume {vol_ratio:.1f}x the 20-candle average",
    ]
    if sym != "BTCUSDT" and btc is not None:
        reasons.append(f"BTC 1h supports the move ({'above' if d == 1 else 'below'} EMA50)")

    return {
        "symbol": sym, "side": "LONG" if d == 1 else "SHORT", "d": d,
        "entry": entry, "sl": sl, "tp1": tp1, "tp2": tp2, "risk": risk,
        "risk_pct": risk_pct, "reasons": reasons, "candle_time": m15["t"][i],
    }


# ----------------------------------------------------------------------------
# Formatting + Telegram
# ----------------------------------------------------------------------------
def fmt_price(p):
    if p >= 1000:
        return f"{p:,.2f}"
    if p >= 1:
        return f"{p:.4f}"
    if p >= 0.01:
        return f"{p:.5f}"
    return f"{p:.8f}".rstrip("0")


def pct(a, b):
    return (b - a) / a * 100


def format_signal(s, group, data_mode):
    emoji = "🟢" if s["side"] == "LONG" else "🔴"
    e = s["entry"]
    now = datetime.now(IST).strftime("%d %b %Y, %I:%M %p IST")
    lines = [
        f"{emoji} <b>{s['side']} — {s['symbol']}</b>",
        f"<i>{group}</i>",
        "",
        f"Entry: <b>{fmt_price(e)}</b>",
        f"Stop-loss: {fmt_price(s['sl'])} ({pct(e, s['sl']):+.2f}%)",
        f"Target 1: {fmt_price(s['tp1'])} ({pct(e, s['tp1']):+.2f}%)",
        f"Target 2: {fmt_price(s['tp2'])} ({pct(e, s['tp2']):+.2f}%)",
        "",
        "<b>Reasons:</b>",
    ]
    lines += [f"• {r}" for r in s["reasons"]]
    lines += [
        "",
        "<b>Plan:</b> At Target 1 book half and move stop-loss to entry. "
        "Skip if price has already moved more than 0.5% from entry.",
        "",
        f"Timeframes: 1h trend + 15m entry  |  {now}",
    ]
    if data_mode == "spot":
        lines.append("Note: prices from Binance spot feed (futures price may differ slightly).")
    lines.append("⚠️ Paper trade first — not financial advice.")
    return "\n".join(lines)


def telegram_send(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat:
        print("[telegram] token/chat id not set -> printing instead:\n" + text + "\n")
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for chunk_start in range(0, len(text), 4000):
        body = urllib.parse.urlencode({
            "chat_id": chat, "text": text[chunk_start:chunk_start + 4000],
            "parse_mode": "HTML", "disable_web_page_preview": "true",
        }).encode()
        for attempt in range(3):
            try:
                req = urllib.request.Request(url, data=body, headers=UA)
                urllib.request.urlopen(req, timeout=20).read()
                break
            except urllib.error.HTTPError as e:
                print("[telegram] error", e.code, e.read().decode()[:300])
                if e.code in (400, 401, 403, 404):
                    return False
                time.sleep(3)
            except Exception as e:
                print("[telegram] error", e)
                time.sleep(3)
    return True


# ----------------------------------------------------------------------------
# State (stops duplicate signals)
# ----------------------------------------------------------------------------
def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            s = json.load(f)
            if isinstance(s, dict):
                s.setdefault("sent", {})
                return s
    except Exception:
        pass
    return {"sent": {}, "last_error_alert": 0}


def save_state(state):
    now = time.time()
    state["sent"] = {k: v for k, v in state.get("sent", {}).items()
                     if now - v < 3 * 86400}
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f)


# ----------------------------------------------------------------------------
# Modes
# ----------------------------------------------------------------------------
def run_scan():
    state = load_state()
    save_state(state)  # make sure the file exists for the cache step
    try:
        mkt = Market()
        groups = build_watchlist(mkt)
        btc = indicators(mkt.klines("BTCUSDT", "1h", 500))
    except Exception as e:
        print("[scan] market data failed:", e)
        if time.time() - state.get("last_error_alert", 0) > 6 * 3600:
            telegram_send(f"⚠️ Scanner could not get market data: {e}\n"
                          "It will retry automatically every 15 minutes.")
            state["last_error_alert"] = time.time()
        save_state(state)
        return 0  # alert already sent; avoid GitHub failure e-mails every 15 min

    print(f"[scan] {len(groups)} coins: {', '.join(groups)}")
    jb = len(btc["c"]) - 1
    sent = 0
    for sym, group in groups.items():
        try:
            m15 = indicators(mkt.klines(sym, "15m", 300))
            h1 = btc if sym == "BTCUSDT" else indicators(mkt.klines(sym, "1h", 500))
        except Exception as e:
            print(f"[scan] {sym}: data error {e}")
            continue
        i, j = len(m15["c"]) - 1, len(h1["c"]) - 1
        if i < 60 or j < 210:
            print(f"[scan] {sym}: not enough history, skipped")
            continue
        # make sure the 15m candle is fresh (closed within the last 20 minutes)
        if time.time() * 1000 - m15["tc"][i] > 20 * 60 * 1000:
            print(f"[scan] {sym}: stale data, skipped")
            continue
        for side in (1, -1):
            sig = evaluate(sym, m15, i, h1, j, btc, jb, side)
            if not sig:
                continue
            key = sym
            last = state["sent"].get(key, 0)
            if time.time() - last < COOLDOWN_HOURS * 3600:
                print(f"[scan] {sym}: signal in cooldown, not resent")
                continue
            telegram_send(format_signal(sig, group, mkt.mode))
            state["sent"][key] = time.time()
            sent += 1
            print(f"[scan] SIGNAL {sig['side']} {sym} @ {sig['entry']}")
        time.sleep(0.05)

    save_state(state)
    print(f"[scan] done. signals sent: {sent}")
    return 0


def simulate(m15, i, sig):
    """Walk forward candle by candle. Returns (result, R multiple)."""
    d, e, sl, tp1, tp2, risk = sig["d"], sig["entry"], sig["sl"], sig["tp1"], sig["tp2"], sig["risk"]
    fee_r = (FEE_PCT_ROUND_TRIP / 100 * e) / risk
    hit1 = False
    stop = sl
    end = min(len(m15["c"]) - 1, i + BACKTEST_MAX_BARS)
    for k in range(i + 1, end + 1):
        hi, lo = m15["h"][k], m15["l"][k]
        adverse = lo <= stop if d == 1 else hi >= stop
        if not hit1:
            # same candle touches stop and target -> count as a loss (worst case)
            if adverse:
                return "loss", -1.0 - fee_r, k
            if (hi >= tp1) if d == 1 else (lo <= tp1):
                hit1 = True
                stop = e  # move stop to entry
                if (hi >= tp2) if d == 1 else (lo <= tp2):
                    return "tp2", 0.5 * TP1_R + 0.5 * TP2_R - fee_r, k
            continue
        if adverse:
            return "tp1", 0.5 * TP1_R - fee_r, k
        if (hi >= tp2) if d == 1 else (lo <= tp2):
            return "tp2", 0.5 * TP1_R + 0.5 * TP2_R - fee_r, k
    if end <= i:
        return "open", 0.0, i
    last = m15["c"][end]
    r_mult = d * (last - e) / risk
    if hit1:
        return "tp1", 0.5 * TP1_R + 0.5 * max(r_mult, 0) - fee_r, end
    return "timeout", r_mult - fee_r, end


def run_backtest():
    mkt = Market()
    groups = build_watchlist(mkt)
    lim15 = mkt.kline_limit_max()
    btc = indicators(mkt.klines("BTCUSDT", "1h", 1000))
    trades = []
    per_coin = {}
    span_ms = 0
    for sym in groups:
        try:
            m15 = indicators(mkt.klines(sym, "15m", lim15))
            h1 = btc if sym == "BTCUSDT" else indicators(mkt.klines(sym, "1h", 1000))
        except Exception as e:
            print(f"[bt] {sym}: data error {e}")
            continue
        if len(m15["t"]) > 1:
            span_ms = max(span_ms, m15["t"][-1] - m15["t"][0])
        busy_until = -1
        for i in range(60, len(m15["c"]) - 1):
            if i <= busy_until:
                continue
            tclose = m15["tc"][i]
            j = bisect.bisect_right(h1["tc"], tclose) - 1
            jb = bisect.bisect_right(btc["tc"], tclose) - 1
            if j < 210 or jb < 210:
                continue
            for side in (1, -1):
                sig = evaluate(sym, m15, i, h1, j, btc, jb, side)
                if sig:
                    res, rr, exit_i = simulate(m15, i, sig)
                    if res == "open":
                        break
                    trades.append((sym, sig["side"], res, rr, m15["t"][i]))
                    per_coin.setdefault(sym, []).append(res)
                    # same coin: wait until trade is closed and cooldown passed
                    busy_until = max(exit_i, i + COOLDOWN_HOURS * 4)
                    break
        time.sleep(0.05)

    n = len(trades)
    if n == 0:
        msg = "📊 Backtest: no signals in the test period. Rules may be too strict."
        telegram_send(msg)
        print(msg)
        return 0
    wins = sum(1 for t in trades if t[2] in ("tp1", "tp2"))
    tp2 = sum(1 for t in trades if t[2] == "tp2")
    losses = sum(1 for t in trades if t[2] == "loss")
    tos = sum(1 for t in trades if t[2] == "timeout")
    total_r = sum(t[3] for t in trades)
    days = max(span_ms / 86400000, 1)
    longs = [t for t in trades if t[1] == "LONG"]
    shorts = [t for t in trades if t[1] == "SHORT"]

    def wr(ts):
        return (sum(1 for t in ts if t[2] in ("tp1", "tp2")) / len(ts) * 100) if ts else 0

    msg = "\n".join([
        "📊 <b>Backtest result</b> (same rules as live signals)",
        f"Data: Binance {mkt.mode}, last ~{days:.0f} days, {len(groups)} coins",
        "",
        f"Total signals: <b>{n}</b> (~{n / days:.1f} per day)",
        f"Win rate (Target 1 hit before stop): <b>{wins / n * 100:.0f}%</b>",
        f"Reached Target 2: {tp2 / n * 100:.0f}%",
        f"Stopped out: {losses / n * 100:.0f}%   |   Closed after 12h: {tos / n * 100:.0f}%",
        f"LONG win rate: {wr(longs):.0f}% ({len(longs)})   SHORT win rate: {wr(shorts):.0f}% ({len(shorts)})",
        f"Net result: {total_r:+.1f}R (R = amount risked per trade, fees included)",
        "",
        "Note: past results do not guarantee future results. Today's gainer/loser "
        "list is used for the whole period.",
    ])
    telegram_send(msg)
    print(msg.replace("<b>", "").replace("</b>", ""))
    return 0


def run_test_message():
    ok = telegram_send("✅ Scanner connected. Signals will arrive here in English.")
    print("sent" if ok else "not sent")
    return 0


if __name__ == "__main__":
    mode = (sys.argv[1] if len(sys.argv) > 1 else "scan").strip().lower()
    if mode == "backtest":
        sys.exit(run_backtest())
    if mode == "test":
        sys.exit(run_test_message())
    sys.exit(run_scan())
