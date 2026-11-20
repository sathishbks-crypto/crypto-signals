#!/usr/bin/env python3
"""
Crypto futures signal scanner (signals only - no auto trading).

HOW IT PROTECTS YOU
  * Several rule sets ("strategies") are tested on the last ~30 days of data.
  * Only a rule set that made a profit in BOTH halves of that period is used live.
  * If no rule set is profitable, live signals are PAUSED (you get one notice).
  * The test repeats automatically once a day, so the choice follows the market.

Every signal: 1h chart decides direction, 15m chart gives the entry,
BTC must not move against the trade. English messages only.

Coins scanned (max 40): favorites.txt + top 10 gainers + top 10 losers (24h).

Usage:
  python scanner.py scan          -> live run (every 15 min)
  python scanner.py backtest      -> full test, sends the comparison table
  python scanner.py autobacktest  -> daily silent test (message only if choice changes)
  python scanner.py test          -> sends one test message to Telegram

Only the Python standard library is used.
"""

import bisect
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

# ----------------------------------------------------------------------------
# SETTINGS
# ----------------------------------------------------------------------------
FAV_FILE = "favorites.txt"
STATE_FILE = "state.json"
TOP_N = 10
MIN_QUOTE_VOLUME = 20_000_000
COOLDOWN_HOURS = 4
MIN_RISK_PCT = 0.4
MAX_RISK_PCT = 4.0
BACKTEST_DAYS = 30
BACKTEST_MAX_BARS = 48            # close a test trade after 12h
FEE_PCT_ROUND_TRIP = 0.10
MIN_TRADES_TO_TRUST = 20          # rule set needs at least this many test trades
LOG_FILE = "live_signals.csv"     # every live signal + its real result


def rp(x):
    """Result as % of the amount risked: 1.4 -> +140%, -1 -> -100%."""
    return f"{x * 100:+.0f}%"
BACKTEST_MAX_AGE_HOURS = 30       # live scan re-tests if the last test is older

# Rule sets that are tested. Live uses the best one that is profitable.
VARIANTS = {
    "A": {"name": "Trend breakout", "entry": "breakout", "adx": 20,
          "sl_buf": 0.2, "tp1": 1.0, "tp2": 2.0, "vol": 1.2, "max_risk_atr": 2.5},
    "B": {"name": "Trend breakout, wide stop", "entry": "breakout", "adx": 20,
          "sl_buf": 1.0, "tp1": 0.8, "tp2": 1.6, "vol": 1.2, "max_risk_atr": 3.5},
    "C": {"name": "Strong-trend breakout", "entry": "breakout", "adx": 28,
          "sl_buf": 0.6, "tp1": 0.8, "tp2": 1.6, "vol": 1.0, "max_risk_atr": 3.0},
    "D": {"name": "Dip buy / rally sell in trend", "entry": "dip", "adx": 20,
          "sl_buf": 0.5, "tp1": 1.0, "tp2": 2.0, "vol": 0.0, "max_risk_atr": 3.0},
    "E": {"name": "Dip in trend, wide stop", "entry": "dip", "adx": 20,
          "sl_buf": 1.0, "tp1": 0.8, "tp2": 1.6, "vol": 0.0, "max_risk_atr": 3.5},
    "F": {"name": "Dip in strong trend", "entry": "dip", "adx": 28,
          "sl_buf": 0.8, "tp1": 0.8, "tp2": 1.6, "vol": 0.0, "max_risk_atr": 3.5},
}

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
# HTTP
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
            if e.code in (400, 403, 404, 451):
                raise last
            time.sleep((5 if e.code in (418, 429) else 1.5) * (attempt + 1))
        except Exception as e:
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise last


# ----------------------------------------------------------------------------
# Market data (Binance Futures; falls back to Binance spot data API because
# GitHub's US servers are usually blocked by Binance Futures)
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

    def _raw(self, symbol, interval, limit, end_time=None, start_time=None):
        path = "/fapi/v1/klines" if self.mode == "futures" else "/api/v3/klines"
        p = {"symbol": symbol, "interval": interval, "limit": limit}
        if end_time:
            p["endTime"] = end_time
        if start_time:
            p["startTime"] = start_time
        return http_get(self.host + path, p)

    def klines_from(self, symbol, interval, start_ms, limit=100):
        """Closed candles starting at start_ms (used to check live signals)."""
        now_ms = int(time.time() * 1000)
        rows = [r for r in self._raw(symbol, interval, limit, start_time=start_ms)
                if int(r[6]) < now_ms]
        return {
            "t": [int(r[0]) for r in rows], "tc": [int(r[6]) for r in rows],
            "o": [float(r[1]) for r in rows], "h": [float(r[2]) for r in rows],
            "l": [float(r[3]) for r in rows], "c": [float(r[4]) for r in rows],
            "v": [float(r[7]) for r in rows],
        }

    def klines(self, symbol, interval, total):
        """Fetch `total` candles (pages backwards if needed). Closed candles only."""
        page = 1500 if self.mode == "futures" else 1000
        rows, end = [], None
        while len(rows) < total:
            need = min(page, total - len(rows))
            batch = self._raw(symbol, interval, need, end)
            if not batch:
                break
            rows = batch + rows
            end = int(batch[0][0]) - 1
            if len(batch) < need:
                break
        seen, clean = set(), []
        for r in rows:
            if r[0] not in seen:
                seen.add(r[0])
                clean.append(r)
        clean.sort(key=lambda r: r[0])
        now_ms = int(time.time() * 1000)
        clean = [r for r in clean if int(r[6]) < now_ms]
        return {
            "t": [int(r[0]) for r in clean],
            "tc": [int(r[6]) for r in clean],
            "o": [float(r[1]) for r in clean],
            "h": [float(r[2]) for r in clean],
            "l": [float(r[3]) for r in clean],
            "c": [float(r[4]) for r in clean],
            "v": [float(r[7]) for r in clean],
        }

    def tradable_symbols(self):
        if self.mode == "futures":
            info = http_get(self.host + "/fapi/v1/exchangeInfo")
            return {s["symbol"] for s in info["symbols"]
                    if s.get("quoteAsset") == "USDT"
                    and s.get("contractType") == "PERPETUAL"
                    and s.get("status") == "TRADING"}
        return None

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
    rows = []
    for t in mkt.tickers_24h():
        sym = t.get("symbol", "")
        if not is_clean_usdt_symbol(sym):
            continue
        if tradable is not None and sym not in tradable:
            continue
        try:
            rows.append((sym, float(t["priceChangePercent"]), float(t["quoteVolume"])))
        except (KeyError, ValueError, TypeError):
            continue
    valid = {r[0] for r in rows}
    favs_ok = [s for s in favs if s in valid]
    dropped = [s for s in favs if s not in valid]
    if dropped:
        print("[coins] not found / not tradable, ignored:", ", ".join(dropped))
    liquid = [r for r in rows if r[2] >= MIN_QUOTE_VOLUME and r[0] not in favs_ok]
    gainers = [r[0] for r in sorted(liquid, key=lambda r: r[1], reverse=True)[:TOP_N]]
    losers = [r[0] for r in sorted([r for r in liquid if r[0] not in gainers],
                                   key=lambda r: r[1])[:TOP_N]]
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
# Strategy (one function, used by live scan AND backtest)
# ----------------------------------------------------------------------------
def _ok(*vals):
    return all(x is not None for x in vals)


def evaluate(sym, m15, i, h1, j, btc, jb, side, P):
    """Return a signal dict or None. side: +1 LONG, -1 SHORT. P: rule set."""
    d = side
    dip = P["entry"] == "dip"
    if i < 30 or j < 10:
        return None

    # ---- 1h trend ------------------------------------------------------
    c1, e50, e200 = h1["c"][j], h1["ema50"][j], h1["ema200"][j]
    e50_prev = h1["ema50"][j - 5]
    adx1, rsi1, atr1 = h1["adx"][j], h1["rsi"][j], h1["atr"][j]
    if not _ok(c1, e50, e200, e50_prev, adx1, rsi1, atr1) or atr1 <= 0:
        return None
    if not (d * (c1 - e50) > 0 and d * (e50 - e200) > 0):
        return None
    if d * (e50 - e50_prev) <= 0:
        return None
    if adx1 < P["adx"]:
        return None
    lo, hi = (45, 70) if dip else (50, 70)
    if d == -1:
        lo, hi = 100 - hi, 100 - lo
    if not (lo <= rsi1 <= hi):
        return None
    if d * (c1 - e50) > 2.5 * atr1:
        return None

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
    if any(x is None for x in r[i - 4:i]):
        return None
    if a <= 0 or c[i] <= 0:
        return None
    if abs(c[i] - o[i]) > 1.8 * a:
        return None                                  # spike candle, don't chase
    vol_ratio = v[i] / vs[i - 1] if vs[i - 1] > 0 else 0

    if dip:
        if d == 1:
            if not (min(r[i - 4:i]) < 35 and r[i - 1] < r[i] < 55):
                return None
        else:
            if not (max(r[i - 4:i]) > 65 and 45 < r[i] < r[i - 1]):
                return None
        if d * (c[i] - o[i]) <= 0 or d * (c[i] - c[i - 1]) <= 0:
            return None                              # no reversal candle
    else:
        if d * (e20[i] - e50m[i]) <= 0:
            return None
        if any(e20[k] is None for k in range(i - 5, i)):
            return None
        if d == 1:
            touched = any(l[k] <= e20[k] + 0.2 * a for k in range(i - 5, i))
            held = min(l[i - 5:i + 1]) >= e50m[i] - 0.5 * a
        else:
            touched = any(h[k] >= e20[k] - 0.2 * a for k in range(i - 5, i))
            held = max(h[i - 5:i + 1]) <= e50m[i] + 0.5 * a
        if not (touched and held):
            return None
        if d * (c[i] - o[i]) <= 0 or d * (c[i] - e20[i]) <= 0:
            return None
        if d == 1 and not c[i] > h[i - 1]:
            return None
        if d == -1 and not c[i] < l[i - 1]:
            return None
        if d == 1 and not (50 <= r[i] <= 68 and r[i] > r[i - 1]):
            return None
        if d == -1 and not (32 <= r[i] <= 50 and r[i] < r[i - 1]):
            return None
        if d * (hist[i] - hist[i - 1]) <= 0:
            return None
        if vol_ratio < P["vol"]:
            return None

    # ---- Stop-loss / targets ------------------------------------------
    entry = c[i]
    if d == 1:
        swing = min(l[i - 5:i + 1]) - P["sl_buf"] * a
    else:
        swing = max(h[i - 5:i + 1]) + P["sl_buf"] * a
    risk = max(abs(entry - swing), 0.8 * a)
    if risk > P["max_risk_atr"] * a:
        return None
    risk_pct = risk / entry * 100
    if risk_pct < MIN_RISK_PCT or risk_pct > MAX_RISK_PCT:
        return None

    up = d == 1
    reasons = [f"1h {'uptrend' if up else 'downtrend'}: price {'above' if up else 'below'} "
               f"EMA50 & EMA200, ADX {adx1:.0f}"]
    if dip:
        reasons += [
            f"15m RSI {'dipped below 35' if up else 'rose above 65'} and is now turning "
            f"{'up' if up else 'down'} ({r[i]:.0f})",
            f"{'Bullish' if up else 'Bearish'} reversal candle closed "
            f"{'above' if up else 'below'} the previous close",
        ]
    else:
        reasons += [
            f"15m pullback to EMA20, then {'bullish' if up else 'bearish'} candle broke "
            f"the previous {'high' if up else 'low'}",
            f"RSI 15m {r[i]:.0f} {'rising' if up else 'falling'}, MACD turning "
            f"{'up' if up else 'down'}, volume {vol_ratio:.1f}x average",
        ]
    if sym != "BTCUSDT" and btc is not None:
        reasons.append(f"BTC 1h supports the move ({'above' if up else 'below'} EMA50)")

    return {
        "symbol": sym, "side": "LONG" if up else "SHORT", "d": d,
        "entry": entry, "sl": entry - d * risk,
        "tp1": entry + d * risk * P["tp1"], "tp2": entry + d * risk * P["tp2"],
        "tp1_r": P["tp1"], "tp2_r": P["tp2"], "risk": risk, "risk_pct": risk_pct,
        "reasons": reasons, "candle_time": m15["t"][i],
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


def format_signal(s, group, data_mode, active):
    emoji = "🟢" if s["side"] == "LONG" else "🔴"
    e = s["entry"]
    now = datetime.now(IST).strftime("%d %b %Y, %I:%M %p IST")
    P = VARIANTS[active["variant"]]
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
    lines += [f"• {x}" for x in s["reasons"]]
    lines += [
        "",
        "<b>Plan:</b> At Target 1 book half and move stop-loss to entry. "
        "If nothing is hit within 12 hours, close the trade. "
        "Skip if price already moved more than 0.5% from entry.",
        "",
        f"Rule set: {P['name']} — last {BACKTEST_DAYS} days test: "
        f"{active['win']:.0f}% win, {active['n']} trades",
        f"1h trend + 15m entry  |  {now}",
    ]
    if data_mode == "spot":
        lines.append("Prices from Binance spot feed (futures price may differ slightly).")
    lines.append("⚠️ Paper trade first — not financial advice.")
    return "\n".join(lines)


def telegram_send(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat:
        print("[telegram] token/chat id not set -> printing instead:\n" + text + "\n")
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for start in range(0, len(text), 4000):
        body = urllib.parse.urlencode({
            "chat_id": chat, "text": text[start:start + 4000],
            "parse_mode": "HTML", "disable_web_page_preview": "true",
        }).encode()
        for _ in range(3):
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
# State (remembers sent signals and the chosen rule set between runs)
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
    state["sent"] = {k: v for k, v in state.get("sent", {}).items() if now - v < 3 * 86400}
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f)


# ----------------------------------------------------------------------------
# Backtest
# ----------------------------------------------------------------------------
def simulate(m15, i, sig):
    d, e, sl, tp1, tp2, risk = sig["d"], sig["entry"], sig["sl"], sig["tp1"], sig["tp2"], sig["risk"]
    r1, r2 = sig["tp1_r"], sig["tp2_r"]
    fee_r = (FEE_PCT_ROUND_TRIP / 100 * e) / risk
    hit1, stop = False, sl
    end = min(len(m15["c"]) - 1, i + BACKTEST_MAX_BARS)
    if end <= i:
        return "open", 0.0, i
    for k in range(i + 1, end + 1):
        hi, lo = m15["h"][k], m15["l"][k]
        adverse = lo <= stop if d == 1 else hi >= stop
        reach1 = hi >= tp1 if d == 1 else lo <= tp1
        reach2 = hi >= tp2 if d == 1 else lo <= tp2
        if not hit1:
            if adverse:                       # stop and target same candle = loss
                return "loss", -1.0 - fee_r, k
            if reach1:
                hit1, stop = True, e
                if reach2:
                    return "tp2", 0.5 * r1 + 0.5 * r2 - fee_r, k
            continue
        if adverse:
            return "tp1", 0.5 * r1 - fee_r, k
        if reach2:
            return "tp2", 0.5 * r1 + 0.5 * r2 - fee_r, k
    rm = d * (m15["c"][end] - e) / risk
    if hit1:
        return "tp1_end", 0.5 * r1 + 0.5 * max(rm, 0) - fee_r, end
    return "timeout", rm - fee_r, end


def run_all_backtests(mkt, groups):
    n15 = BACKTEST_DAYS * 96 + 300
    n1h = BACKTEST_DAYS * 24 + 300
    start_ms = int((time.time() - BACKTEST_DAYS * 86400) * 1000)
    btc = indicators(mkt.klines("BTCUSDT", "1h", n1h))
    data = {}
    for sym in groups:
        try:
            m15 = indicators(mkt.klines(sym, "15m", n15))
            h1 = btc if sym == "BTCUSDT" else indicators(mkt.klines(sym, "1h", n1h))
            data[sym] = (m15, h1)
        except Exception as e:
            print(f"[bt] {sym}: data error {e}")
        time.sleep(0.05)

    results = {}
    for key, P in VARIANTS.items():
        trades = []
        for sym, (m15, h1) in data.items():
            busy_until = -1
            for i in range(60, len(m15["c"]) - 1):
                if i <= busy_until or m15["t"][i] < start_ms:
                    continue
                tc = m15["tc"][i]
                j = bisect.bisect_right(h1["tc"], tc) - 1
                jb = bisect.bisect_right(btc["tc"], tc) - 1
                if j < 210 or jb < 210:
                    continue
                for side in (1, -1):
                    sig = evaluate(sym, m15, i, h1, j, btc, jb, side, P)
                    if sig:
                        res, rr, exit_i = simulate(m15, i, sig)
                        if res != "open":
                            trades.append((sym, sig["side"], res, rr, m15["t"][i]))
                            busy_until = max(exit_i, i + COOLDOWN_HOURS * 4)
                        break
        results[key] = stats(trades, start_ms)
    return results, len(data), mkt.mode


def stats(trades, start_ms):
    n = len(trades)
    mid = start_ms + (time.time() * 1000 - start_ms) / 2
    first = [t for t in trades if t[4] < mid]
    second = [t for t in trades if t[4] >= mid]

    def win(ts):
        return sum(1 for t in ts if t[2] in ("tp1", "tp1_end", "tp2")) / len(ts) * 100 if ts else 0.0

    return {
        "n": n, "win": win(trades),
        "tp2": (sum(1 for t in trades if t[2] == "tp2") / n * 100) if n else 0.0,
        "net": sum(t[3] for t in trades),
        "net1": sum(t[3] for t in first), "net2": sum(t[3] for t in second),
        "long": sum(1 for t in trades if t[1] == "LONG"),
        "short": sum(1 for t in trades if t[1] == "SHORT"),
    }


def choose(results):
    good = [(k, s) for k, s in results.items()
            if s["n"] >= MIN_TRADES_TO_TRUST and s["net"] > 0 and s["net1"] > 0 and s["net2"] > 0]
    if not good:
        return None
    good.sort(key=lambda ks: (ks[1]["win"], ks[1]["net"]), reverse=True)
    return good[0][0]


def backtest_and_choose(state, announce):
    mkt = Market()
    groups = build_watchlist(mkt)
    results, ncoins, mode = run_all_backtests(mkt, groups)
    best = choose(results)
    prev = (state.get("active") or {}).get("variant")
    if best:
        s = results[best]
        state["active"] = {"variant": best, "win": round(s["win"], 1), "n": s["n"],
                           "net": round(s["net"], 1), "updated": time.time()}
    else:
        state["active"] = {"variant": None, "updated": time.time()}

    lines = [f"📊 <b>Rule-set test</b> — last {BACKTEST_DAYS} days, {ncoins} coins (Binance {mode})", ""]
    for k, s in results.items():
        mark = "✅" if k == best else ("➖" if s["net"] > 0 else "❌")
        lines.append(f"{mark} {k} {VARIANTS[k]['name']}: {s['n']} trades, win {s['win']:.0f}%, "
                     f"total {rp(s['net'])} (halves {rp(s['net1'])} / {rp(s['net2'])})")
    lines.append("")
    if best:
        lines.append(f"✅ <b>Live signals now use rule set {best}: {VARIANTS[best]['name']}</b>")
    else:
        lines.append("⏸ <b>Live signals PAUSED</b> — no rule set made a profit in both halves of "
                     "the test period. The test repeats daily and signals restart automatically.")
    lines.append("% = profit/loss compared to the amount risked per trade (100% = one full stop-loss). "
                 "Fees included. Past results do not guarantee "
                 "future results.")
    msg = "\n".join(lines)
    print(msg.replace("<b>", "").replace("</b>", ""))
    if announce or best != prev:
        telegram_send(msg)
    return best


# ----------------------------------------------------------------------------
# Live signal log + result tracking + monthly report
# ----------------------------------------------------------------------------
LOG_FIELDS = ["id", "time_ist", "symbol", "side", "entry", "sl", "tp1", "tp2",
              "tp1_r", "tp2_r", "risk", "rule", "group", "candle_ms",
              "status", "result", "r_multiple", "closed_ist"]
RESULT_TEXT = {
    "tp2": "TARGET 2 HIT",
    "tp1": "TARGET 1 HIT (rest closed at entry)",
    "loss": "STOP-LOSS HIT",
    "timeout": "CLOSED AFTER 12H",
}


def read_log():
    if not os.path.exists(LOG_FILE):
        return []
    try:
        with open(LOG_FILE, newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))
    except Exception as e:
        print("[log] could not read log:", e)
        return []


def write_log(rows):
    with open(LOG_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=LOG_FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def log_signal(sig, group, rule_key):
    rows = read_log()
    now = datetime.now(IST)
    rows.append({
        "id": f"{sig['symbol']}-{sig['candle_time']}",
        "time_ist": now.strftime("%Y-%m-%d %H:%M"),
        "symbol": sig["symbol"], "side": sig["side"],
        "entry": f"{sig['entry']:.10g}", "sl": f"{sig['sl']:.10g}",
        "tp1": f"{sig['tp1']:.10g}", "tp2": f"{sig['tp2']:.10g}",
        "tp1_r": sig["tp1_r"], "tp2_r": sig["tp2_r"], "risk": f"{sig['risk']:.10g}",
        "rule": rule_key, "group": group, "candle_ms": sig["candle_time"],
        "status": "OPEN", "result": "", "r_multiple": "", "closed_ist": "",
    })
    write_log(rows)


def update_open_signals(mkt):
    """Check every OPEN signal against real prices after it was sent."""
    rows = read_log()
    changed = False
    for r in rows:
        if r.get("status") != "OPEN":
            continue
        try:
            sig = {
                "d": 1 if r["side"] == "LONG" else -1,
                "entry": float(r["entry"]), "sl": float(r["sl"]),
                "tp1": float(r["tp1"]), "tp2": float(r["tp2"]),
                "tp1_r": float(r["tp1_r"]), "tp2_r": float(r["tp2_r"]),
                "risk": float(r["risk"]),
            }
            k = mkt.klines_from(r["symbol"], "15m", int(r["candle_ms"]), BACKTEST_MAX_BARS + 5)
        except Exception as e:
            print(f"[track] {r.get('symbol')}: {e}")
            continue
        if len(k["c"]) < 2:
            continue
        res, rr, exit_i = simulate(k, 0, sig)
        finished_window = len(k["c"]) - 1 >= BACKTEST_MAX_BARS
        if res == "open":
            continue
        if res == "timeout" and not finished_window:
            continue                                  # 12h not over yet
        if res == "tp1_end" and not finished_window:
            continue                                  # Target 1 hit, rest still running
        if res == "tp1_end":
            res = "tp1"
        r["status"] = "CLOSED"
        r["result"] = RESULT_TEXT.get(res, res)
        r["r_multiple"] = f"{rr:.2f}"
        r["closed_ist"] = datetime.fromtimestamp(k["tc"][exit_i] / 1000, IST).strftime("%Y-%m-%d %H:%M")
        changed = True
        emoji = "✅" if rr > 0 else "❌"
        telegram_send(f"{emoji} <b>Result: {r['side']} {r['symbol']}</b> (signal {r['time_ist']} IST)\n"
                      f"{r['result']} → <b>{rp(rr)}</b> of the amount risked")
        time.sleep(0.05)
    if changed:
        write_log(rows)


def month_report(year, month, title=None):
    rows = [r for r in read_log() if r.get("time_ist", "").startswith(f"{year:04d}-{month:02d}")]
    name = datetime(year, month, 1).strftime("%B %Y")
    head = title or f"📅 <b>Monthly report — {name}</b>"
    if not rows:
        return f"{head}\nNo signals were sent in this month."
    closed = [r for r in rows if r["status"] == "CLOSED"]
    open_ = [r for r in rows if r["status"] != "CLOSED"]
    rv = [float(r["r_multiple"] or 0) for r in closed]
    wins = [r for r in closed if float(r["r_multiple"] or 0) > 0]
    losses = [r for r in closed if float(r["r_multiple"] or 0) <= 0]
    t2 = sum(1 for r in closed if r["result"].startswith("TARGET 2"))
    t1 = sum(1 for r in closed if r["result"].startswith("TARGET 1"))
    sl = sum(1 for r in closed if r["result"].startswith("STOP"))
    to = sum(1 for r in closed if r["result"].startswith("CLOSED AFTER"))
    net = sum(rv)
    win_pct = len(wins) / len(closed) * 100 if closed else 0

    per = {}
    for r in closed:
        per.setdefault(r["symbol"], 0.0)
        per[r["symbol"]] += float(r["r_multiple"] or 0)
    best = sorted(per.items(), key=lambda x: x[1], reverse=True)

    def side_line(side):
        ss = [r for r in closed if r["side"] == side]
        if not ss:
            return f"{side}: 0"
        w = sum(1 for r in ss if float(r["r_multiple"] or 0) > 0)
        return f"{side}: {len(ss)} trades, {w} won ({w / len(ss) * 100:.0f}%)"

    lines = [
        head, "",
        f"Total signals: <b>{len(rows)}</b>  (finished {len(closed)}, still open {len(open_)})",
        f"✅ Won: <b>{len(wins)}</b>   ❌ Lost: <b>{len(losses)}</b>   Win rate: <b>{win_pct:.0f}%</b>",
        "",
        f"Target 2 hit: {t2}",
        f"Target 1 hit (rest at entry): {t1}",
        f"Stop-loss hit: {sl}",
        f"Closed after 12h: {to}",
        "",
        side_line("LONG") + "\n" + side_line("SHORT"),
        "",
        f"Total result: <b>{rp(net)}</b> of the amount risked per trade",
        "(100% = one full stop-loss amount)",
    ]
    if best:
        lines.append(f"Best coin: {best[0][0]} ({rp(best[0][1])})   "
                     f"Worst coin: {best[-1][0]} ({rp(best[-1][1])})")
    lines.append("(Fees included. Full list is in live_signals.csv in your GitHub repo.)")
    return "\n".join(lines)


def maybe_send_monthly_report(state):
    now = datetime.now(IST)
    this_month = now.strftime("%Y-%m")
    last = state.get("report_month")
    if last is None:
        state["report_month"] = this_month
        return
    if last != this_month:
        prev = (now.replace(day=1) - timedelta(days=1))
        telegram_send(month_report(prev.year, prev.month))
        state["report_month"] = this_month


# ----------------------------------------------------------------------------
# Modes
# ----------------------------------------------------------------------------
def run_report():
    now = datetime.now(IST)
    msg = month_report(now.year, now.month,
                       title=f"📅 <b>Report so far — {now.strftime('%B %Y')}</b> "
                             f"(1st to {now.strftime('%d')})")
    telegram_send(msg)
    print(msg)
    return 0


def run_scan():
    state = load_state()
    save_state(state)
    try:
        mkt = Market()
        # 1) check results of earlier signals + monthly report (even when paused)
        try:
            update_open_signals(mkt)
        except Exception as e:
            print("[track] error:", e)
        try:
            maybe_send_monthly_report(state)
            save_state(state)
        except Exception as e:
            print("[report] error:", e)
        # 2) make sure a tested rule set is chosen
        active = state.get("active")
        if not active or time.time() - active.get("updated", 0) > 24 * 3600:
            print("[scan] rule-set test is missing or older than 24h -> testing now")
            backtest_and_choose(state, announce=False)
            save_state(state)
            active = state["active"]
        if not active.get("variant") or active["variant"] not in VARIANTS:
            print("[scan] paused: no profitable rule set right now")
            return 0
        P = VARIANTS[active["variant"]]
        groups = build_watchlist(mkt)
        btc = indicators(mkt.klines("BTCUSDT", "1h", 500))
    except Exception as e:
        print("[scan] market data failed:", e)
        if time.time() - state.get("last_error_alert", 0) > 6 * 3600:
            telegram_send(f"⚠️ Scanner could not get market data: {e}\n"
                          "It will retry automatically every 15 minutes.")
            state["last_error_alert"] = time.time()
        save_state(state)
        return 0

    print(f"[scan] rule set {active['variant']} | {len(groups)} coins: {', '.join(groups)}")
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
            continue
        if time.time() * 1000 - m15["tc"][i] > 20 * 60 * 1000:
            continue  # stale data
        for side in (1, -1):
            sig = evaluate(sym, m15, i, h1, j, btc, jb, side, P)
            if not sig:
                continue
            if time.time() - state["sent"].get(sym, 0) < COOLDOWN_HOURS * 3600:
                break
            telegram_send(format_signal(sig, group, mkt.mode, active))
            try:
                log_signal(sig, group, active["variant"])
            except Exception as e:
                print("[log] error:", e)
            state["sent"][sym] = time.time()
            sent += 1
            print(f"[scan] SIGNAL {sig['side']} {sym} @ {sig['entry']}")
            break
        time.sleep(0.05)
    save_state(state)
    print(f"[scan] done. signals sent: {sent}")
    return 0


def run_backtest():
    state = load_state()
    backtest_and_choose(state, announce=True)
    save_state(state)
    return 0


def run_test_message():
    ok = telegram_send("✅ Scanner connected. Signals will arrive here in English.")
    print("sent" if ok else "not sent")
    return 0


if __name__ == "__main__":
    mode = (sys.argv[1] if len(sys.argv) > 1 else "scan").strip().lower()
    if mode in ("backtest", "autobacktest"):
        if mode == "autobacktest":
            st = load_state()
            backtest_and_choose(st, announce=False)
            save_state(st)
            sys.exit(0)
        sys.exit(run_backtest())
    if mode == "test":
        sys.exit(run_test_message())
    if mode == "report":
        sys.exit(run_report())
    sys.exit(run_scan())
