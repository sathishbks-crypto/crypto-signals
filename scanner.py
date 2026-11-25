#!/usr/bin/env python3
"""
Crypto futures signal scanner (signals only - no auto trading).

HOW IT PROTECTS YOU
  * 6 rule sets (1h entry + 4h trend) are tested on the last ~180 days (6 months).
  * The test uses only YOUR coin list (favorites.txt). Today's top gainers/losers
    are NOT tested, because picking coins by today's move makes old results look
    better than they really were.
  * Only a rule set that made a profit in EVERY 2-month part is used live.
  * An "honest check" also shows: chosen on the first 4 months -> result in the
    last 2 months it never saw.
  * If no rule set is profitable, live signals are PAUSED (you get one notice).
  * The test repeats automatically once a day, so the choice follows the market.

Market data: Binance FUTURES only (never spot). GitHub's US servers cannot
reach Binance Futures, so run tests on the Lightsail server.

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
COOLDOWN_BARS = 16                # no repeat signal for a coin within 16 entry candles
MIN_RISK_PCT = 0.4
MAX_RISK_PCT = 4.0
BACKTEST_DAYS = 180               # 6 months of history
BACKTEST_PARTS = 3                # split into 3 parts (2 months each); each must be in profit
HONEST_DAYS = 60                  # honest check: last 60 days are hidden from the choice
BACKTEST_MAX_BARS = 48            # close a trade after 48 entry candles (12h on 15m, 48h on 1h)
TF_MIN = {"15m": 15, "1h": 60, "4h": 240}
FEE_PCT_ROUND_TRIP = 0.10
SLIPPAGE_PCT = 0.05               # extra cost: stop-loss/market orders fill a little worse
MIN_TRADES_TO_TRUST = 20          # rule set needs at least this many test trades
LOG_FILE = "live_signals.csv"     # every live signal + its real result


def rp(x):
    """Result as % of the amount risked: 1.4 -> +140%, -1 -> -100%."""
    return f"{x * 100:+.0f}%"
BACKTEST_MAX_AGE_HOURS = 30       # live scan re-tests if the last test is older

# Rule sets that are tested. Live uses the best one that is profitable.
_BASE = {
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
# ----------------------------------------------------------------------------
# Your settings (settings.txt in the repo). Change numbers there, not here.
# ----------------------------------------------------------------------------
SETTINGS_FILE = "settings.txt"
DEFAULT_SETTINGS = {
    "start_balance": 100.0,       # paper balance (USDT) the counting starts from
    "risk_per_trade": 1.0,        # % of current balance lost if a stop-loss hits
    "daily_profit_target": "off", # % profit in a day -> no new signals that day (off = no limit)
    "daily_loss_limit": "off",    # % loss in a day -> no new signals that day (off = no limit)
    "max_open_trades": "off",     # trades running at the same time (off = no limit)
    "target_rr": "2",             # 2 = one target at 1:2 risk-reward; auto = rule set's own
    "leverage": 3.0,              # only used to show the margin needed
    "exit_mode": "fixed",         # fixed = close all at target; ladder = close half, let rest run
}


def load_settings():
    cfg = dict(DEFAULT_SETTINGS)
    if os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.split("#")[0].strip()
                if "=" not in line:
                    continue
                k, v = [x.strip().lower() for x in line.split("=", 1)]
                if k not in cfg:
                    continue
                if k == "target_rr":
                    cfg[k] = v.replace("1:", "")
                    continue
                if k == "exit_mode":
                    cfg[k] = "ladder" if v.startswith("ladder") else "fixed"
                    continue
                if v in ("off", "no", "none", "0"):
                    cfg[k] = "off"
                    continue
                try:
                    cfg[k] = float(v.replace("%", "").replace("$", ""))
                except ValueError:
                    print(f"[settings] bad value for {k}: {v} (using {cfg[k]})")
    for k in ("daily_profit_target", "daily_loss_limit", "max_open_trades"):
        if cfg[k] == "off":
            cfg[k] = float("inf")
        else:
            try:
                cfg[k] = float(cfg[k])
            except (TypeError, ValueError):
                cfg[k] = float("inf")
    if not isinstance(cfg["risk_per_trade"], float):
        cfg["risk_per_trade"] = 1.0
    # safety limits so a typo cannot blow up the account
    cfg["risk_per_trade"] = min(max(cfg["risk_per_trade"], 0.1), 20.0)
    if cfg["daily_loss_limit"] != float("inf"):
        cfg["daily_loss_limit"] = max(cfg["daily_loss_limit"], cfg["risk_per_trade"])
    if cfg["daily_profit_target"] != float("inf"):
        cfg["daily_profit_target"] = max(cfg["daily_profit_target"], 0.5)
    if cfg["max_open_trades"] != float("inf"):
        cfg["max_open_trades"] = max(cfg["max_open_trades"], 1)
    cfg["start_balance"] = max(cfg["start_balance"], 1.0)
    cfg["leverage"] = min(max(cfg["leverage"], 1.0), 20.0)
    try:
        rr = float(cfg["target_rr"])
        cfg["rr"] = rr if 0.5 <= rr <= 5 else None
    except ValueError:
        cfg["rr"] = None
    return cfg


SET = load_settings()
LADDER = SET["exit_mode"] == "ladder"
LADDER_MAX_BARS = 240            # a runner may stay open up to 240 candles (10 days on 1h)
RR_KEY = f"{SET['rr']:g}" if SET["rr"] else "auto"

# Only slow rule sets: 1h entry + 4h trend (15m tested worse because of fees)
VARIANTS = {}
for _k, _v in zip("GHIJKL", _BASE.values()):
    VARIANTS[_k] = dict(_v, tf="1h", trend_tf="4h", name=_v["name"] + " (slow)",
                        max_risk_pct=8.0)
    if SET["rr"]:                               # single target at your risk:reward
        VARIANTS[_k]["tp1"] = VARIANTS[_k]["tp2"] = SET["rr"]

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


def _load_server_secrets():
    """On the server: read the Telegram keys from secrets.txt (made by setup.sh)."""
    if os.environ.get("TELEGRAM_BOT_TOKEN") or not os.path.exists("secrets.txt"):
        return
    try:
        with open("secrets.txt", encoding="utf-8") as f:
            for line in f:
                if "=" in line and not line.strip().startswith("#"):
                    k, v = line.split("=", 1)
                    if k.strip().upper() in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
                        os.environ.setdefault(k.strip().upper(), v.strip())
    except Exception as e:
        print("[secrets] could not read secrets.txt:", e)


_load_server_secrets()
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
            # Futures data ONLY. Spot prices are never used as a replacement.
            raise RuntimeError(
                "Binance FUTURES data could not be reached from this computer "
                "(GitHub's US servers are blocked by Binance Futures). "
                "Run this on your Lightsail server instead.")
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
    if risk_pct < MIN_RISK_PCT or risk_pct > P.get("max_risk_pct", MAX_RISK_PCT):
        return None

    up = d == 1
    TT, ET = P["trend_tf"], P["tf"]
    reasons = [f"{TT} {'uptrend' if up else 'downtrend'}: price {'above' if up else 'below'} "
               f"EMA50 & EMA200, ADX {adx1:.0f}"]
    if dip:
        reasons += [
            f"{ET} RSI {'dipped below 35' if up else 'rose above 65'} and is now turning "
            f"{'up' if up else 'down'} ({r[i]:.0f})",
            f"{'Bullish' if up else 'Bearish'} reversal candle closed "
            f"{'above' if up else 'below'} the previous close",
        ]
    else:
        reasons += [
            f"{ET} pullback to EMA20, then {'bullish' if up else 'bearish'} candle broke "
            f"the previous {'high' if up else 'low'}",
            f"RSI {ET} {r[i]:.0f} {'rising' if up else 'falling'}, MACD turning "
            f"{'up' if up else 'down'}, volume {vol_ratio:.1f}x average",
        ]
    if sym != "BTCUSDT" and btc is not None:
        reasons.append(f"BTC {TT} supports the move ({'above' if up else 'below'} EMA50)")

    return {
        "symbol": sym, "side": "LONG" if up else "SHORT", "d": d,
        "entry": entry, "sl": entry - d * risk,
        "tp1": entry + d * risk * P["tp1"], "tp2": entry + d * risk * P["tp2"],
        "tp1_r": P["tp1"], "tp2_r": P["tp2"], "tf": ET, "risk": risk, "risk_pct": risk_pct,
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


def format_signal(s, group, data_mode, active, acct=None):
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
    ]
    single = abs(s["tp1"] - s["tp2"]) < 1e-12
    if single:
        lines.append(f"Target: {fmt_price(s['tp2'])} ({pct(e, s['tp2']):+.2f}%)  "
                     f"[1:{s['tp2_r']:g}]")
    else:
        lines += [f"Target 1: {fmt_price(s['tp1'])} ({pct(e, s['tp1']):+.2f}%)",
                  f"Target 2: {fmt_price(s['tp2'])} ({pct(e, s['tp2']):+.2f}%)"]
    stop_pct = abs(pct(e, s["sl"]))
    if stop_pct > 0:
        pos = 1 / (stop_pct / 100)
        lines += [
            "",
            f"<b>1:2 trade</b> — stop-loss = -100% of your trade amount, target = +200%",
            f"Size: for every $1 you put at risk, open a ${pos:,.0f} position "
            f"(stop {stop_pct:.2f}% away). Example: risk $10 → position ${pos * 10:,.0f}.",
            f"Never use more than {max(1, min(20, int(70 / stop_pct)))}x leverage on this trade "
            f"(higher = liquidation before the stop-loss).",
        ]
    lines += ["", "<b>Reasons:</b>"]
    lines += [f"• {x}" for x in s["reasons"]]
    lines += [
        "",
        ("<b>Plan (ladder):</b> At the target close HALF and move stop-loss to entry. "
         "Each further +200% step: close half again and move stop-loss up one step. "
         if LADDER else
         "<b>Plan:</b> Place stop-loss and target together. " if single else
         "<b>Plan:</b> At Target 1 book half and move stop-loss to entry. ") +
        (f"If the first target is not hit within {hold_hours(P['tf'])} hours, close the trade. "
         if not LADDER else "") +
        "Skip if price already moved more than 0.5% from entry.",
        "",
        f"Rule set: {P['name']} — last {BACKTEST_DAYS // 30} months test: "
        f"{active['win']:.0f}% of trades in profit, {active['n']} trades",
        f"{P['trend_tf']} trend + {P['tf']} entry  |  {now}",
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
    fee_r = ((FEE_PCT_ROUND_TRIP + SLIPPAGE_PCT) / 100 * e) / risk
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


def simulate_ladder(m, i, sig, max_bars=None):
    """Close half at every +2R step, move stop to the previous step, let the rest run.
    Returns (R result, exit index, steps reached, closed_for_real)."""
    max_bars = max_bars or LADDER_MAX_BARS
    d, e, risk = sig["d"], sig["entry"], sig["risk"]
    step = sig.get("tp2_r", 2.0) or 2.0
    fee_r = ((FEE_PCT_ROUND_TRIP + SLIPPAGE_PCT) / 100 * e) / risk
    size, locked, n = 1.0, 0.0, 0
    stop = e - d * risk
    nxt = e + d * risk * step
    end = min(len(m["c"]) - 1, i + max_bars)
    if end <= i:
        return 0.0, i, 0, False
    for k in range(i + 1, end + 1):
        hi, lo = m["h"][k], m["l"][k]
        if (lo <= stop) if d == 1 else (hi >= stop):          # stop first (worst case)
            return locked + size * d * (stop - e) / risk - fee_r, k, n, True
        while (hi >= nxt) if d == 1 else (lo <= nxt):
            n += 1
            locked += size / 2 * step * n                     # close half at this step
            size /= 2
            stop = e + d * risk * step * (n - 1)              # stop to previous step
            nxt = e + d * risk * step * (n + 1)
            if size < 1 / 32:                                  # tiny rest: close it
                return locked + size * step * n - fee_r, k, n, True
    rm = d * (m["c"][end] - e) / risk
    finished = end - i >= max_bars
    return locked + size * rm - fee_r, end, n, finished


def hold_hours(tf):
    return BACKTEST_MAX_BARS * TF_MIN[tf] // 60


def gen_trades(mkt, groups, days):
    """All test trades of every rule set over the last `days` days."""
    start_ms = int((time.time() - days * 86400) * 1000)
    need = {tf: days * 1440 // m + 300 for tf, m in TF_MIN.items()}
    tfs = sorted({P["tf"] for P in VARIANTS.values()} | {P["trend_tf"] for P in VARIANTS.values()})
    btc = {tf: indicators(mkt.klines("BTCUSDT", tf, need[tf]))
           for tf in {P["trend_tf"] for P in VARIANTS.values()}}
    data = {}
    for sym in groups:
        try:
            data[sym] = {tf: (btc[tf] if sym == "BTCUSDT" and tf in btc
                              else indicators(mkt.klines(sym, tf, need[tf]))) for tf in tfs}
        except Exception as e:
            print(f"[bt] {sym}: data error {e}")
        time.sleep(0.05)

    out = {}
    for key, P in VARIANTS.items():
        trades = []
        bt = btc[P["trend_tf"]]
        for sym, dd in data.items():
            m15, h1 = dd[P["tf"]], dd[P["trend_tf"]]
            busy_until = -1
            for i in range(60, len(m15["c"]) - 1):
                if i <= busy_until or m15["t"][i] < start_ms:
                    continue
                tc = m15["tc"][i]
                j = bisect.bisect_right(h1["tc"], tc) - 1
                jb = bisect.bisect_right(bt["tc"], tc) - 1
                if j < 210 or jb < 210:
                    continue
                for side in (1, -1):
                    sig = evaluate(sym, m15, i, h1, j, bt, jb, side, P)
                    if sig:
                        res, rr, exit_i = simulate(m15, i, sig)
                        if res != "open":
                            # (symbol, side, result, R, entry time, exit time)
                            lr, lx, ln, _ = simulate_ladder(m15, i, sig)
                            # (symbol, side, result, R, entry, exit, ladder R, ladder exit, steps)
                            trades.append((sym, sig["side"], res, rr, m15["tc"][i], m15["tc"][exit_i],
                                           lr, m15["tc"][lx], ln))
                            busy_until = max(exit_i, i + COOLDOWN_BARS)
                        break
        out[key] = trades
    return out, start_ms, len(data), mkt.mode


def as_ladder(trades):
    """Same trades, but with the ladder exit result."""
    return [(t[0], t[1], "tp2" if t[6] > 0 else "loss", t[6], t[4], t[7]) + t[6:]
            for t in trades]


def backtest_coins(mkt):
    """Coins for the rule-set test: only your fixed list (favorites.txt).
    Top gainers/losers are left out on purpose: they are chosen by TODAY's move,
    so testing them on old data would show results that were not really possible."""
    groups = build_watchlist(mkt)
    return {s: g for s, g in groups.items() if g == "Your list"} or groups


def run_all_backtests(mkt, groups):
    trades, start_ms, ncoins, mode = gen_trades(mkt, groups, BACKTEST_DAYS)
    if LADDER:
        trades = {k: as_ladder(v) for k, v in trades.items()}
    now_ms = time.time() * 1000
    split_ms = now_ms - HONEST_DAYS * 86400 * 1000
    results = {k: stats(t, start_ms, now_ms) for k, t in trades.items()}
    # honest check: choose using only the older part, then look at the hidden part
    older = {k: stats(t, start_ms, split_ms) for k, t in trades.items()}
    pick = choose(older)
    honest = {"pick": pick,
              "res": stats(trades[pick], split_ms, now_ms, parts=1) if pick else None}
    return results, ncoins, mode, honest


def stats(trades, start_ms, end_ms=None, parts=None):
    end_ms = end_ms or time.time() * 1000
    parts = parts or BACKTEST_PARTS
    trades = [t for t in trades if start_ms <= t[4] < end_ms]
    n = len(trades)
    mid = start_ms + (end_ms - start_ms) / 2
    first = [t for t in trades if t[4] < mid]
    second = [t for t in trades if t[4] >= mid]
    step = (end_ms - start_ms) / parts
    part_net = [sum(t[3] for t in trades
                    if start_ms + p * step <= t[4] < start_ms + (p + 1) * step)
                for p in range(parts)]

    # How every trade ended (single target):
    #   target  = target reached, stop = stop-loss hit,
    #   time    = closed at the 48-candle time limit (can be profit OR loss)
    tgt = [t for t in trades if t[2] in ("tp2", "tp1", "tp1_end")]
    stp = [t for t in trades if t[2] == "loss"]
    tim = [t for t in trades if t[2] == "timeout"]
    prof = sum(1 for t in trades if t[3] > 0)

    def avg(ts):
        return sum(t[3] for t in ts) / len(ts) if ts else 0.0

    return {
        "n": n,
        "win": prof / n * 100 if n else 0.0,          # % of trades that ended in profit
        "tgt_pct": len(tgt) / n * 100 if n else 0.0,  # % of trades that reached the target
        "tgt": len(tgt), "stop": len(stp), "time": len(tim),
        "time_win": sum(1 for t in tim if t[3] > 0),
        "avg_tgt": avg(tgt), "avg_stop": avg(stp), "avg_time": avg(tim),
        "avg": avg(trades),
        "tp2": (sum(1 for t in trades if t[2] == "tp2") / n * 100) if n else 0.0,
        "net": sum(t[3] for t in trades),
        "net1": sum(t[3] for t in first), "net2": sum(t[3] for t in second),
        "parts": part_net,
        "long": sum(1 for t in trades if t[1] == "LONG"),
        "short": sum(1 for t in trades if t[1] == "SHORT"),
    }


def choose(results):
    good = [(k, s) for k, s in results.items()
            if s["n"] >= MIN_TRADES_TO_TRUST and s["net"] > 0 and all(p > 0 for p in s["parts"])]
    if not good:
        return None
    # best average result per trade (not "win %": a 1:2 trade can win less often and still earn)
    good.sort(key=lambda ks: (ks[1]["avg"], ks[1]["net"]), reverse=True)
    return good[0][0]


def backtest_and_choose(state, announce):
    mkt = Market()
    groups = backtest_coins(mkt)
    results, ncoins, mode, honest = run_all_backtests(mkt, groups)
    best = choose(results)
    prev = (state.get("active") or {}).get("variant")
    if best:
        s = results[best]
        state["active"] = {"variant": best, "win": round(s["win"], 1), "n": s["n"],
                           "net": round(s["net"], 1), "updated": time.time(), "rr": RR_KEY}
    else:
        state["active"] = {"variant": None, "updated": time.time(), "rr": RR_KEY}

    tgt = f"one target at 1:{SET['rr']:g}" if SET["rr"] else "rule set's own targets"
    months = BACKTEST_DAYS // 30
    pm = BACKTEST_DAYS // BACKTEST_PARTS // 30
    lines = [f"📊 <b>Rule-set test</b> — last {months} months, {ncoins} coins from your list "
             f"(Binance {mode})",
             f"Targets: {tgt}. Time limit: {BACKTEST_MAX_BARS} candles.", ""]
    for k, s in results.items():
        mark = "✅" if k == best else ("➖" if s["net"] > 0 else "❌")
        lines += [
            f"{mark} <b>{k} {VARIANTS[k]['name']}</b> [{VARIANTS[k]['tf']}]",
            f"   {s['n']} trades | in profit {s['win']:.0f}% | total <b>{rp(s['net'])}</b> "
            f"| per trade {rp(s['avg'])}",
            f"   🎯 target {s['tgt']} ({rp(s['avg_tgt'])} each)  "
            f"🛑 stop {s['stop']} ({rp(s['avg_stop'])} each)",
            f"   ⏱ time limit {s['time']} ({s['time_win']} in profit, avg {rp(s['avg_time'])})",
            f"   Each {pm}-month part: " + " / ".join(rp(p) for p in s["parts"]),
        ]
    lines.append("")
    if honest["pick"]:
        h = honest["res"]
        lines.append(f"🔍 <b>Honest check:</b> chosen on the first {months - HONEST_DAYS // 30} months only "
                     f"→ {honest['pick']}. In the last {HONEST_DAYS // 30} months (not seen): "
                     f"{h['n']} trades, in profit {h['win']:.0f}%, total <b>{rp(h['net'])}</b>"
                     + (" ✅" if h["net"] > 0 else " ❌"))
    else:
        lines.append(f"🔍 <b>Honest check:</b> on the first {months - HONEST_DAYS // 30} months no rule "
                     f"set was good enough → would not have traded.")
    lines.append("")
    if best:
        lines.append(f"✅ <b>Live signals now use rule set {best}: {VARIANTS[best]['name']}</b>")
    else:
        lines.append(f"⏸ <b>Live signals PAUSED</b> — no rule set made a profit in every {pm}-month "
                     "part. The test repeats daily and signals restart automatically.")
    lines.append("% = profit/loss compared to the amount risked per trade (100% = one full stop-loss). "
                 "Fees and slippage included. \"In profit\" counts every trade that ended above zero, "
                 "including time-limit exits. Past results do not guarantee future results.")
    msg = "\n".join(lines)
    print(msg.replace("<b>", "").replace("</b>", ""))
    if announce or best != prev:
        telegram_send(msg)
    return best


# ----------------------------------------------------------------------------
# Live signal log + result tracking + monthly report
# ----------------------------------------------------------------------------
LOG_FIELDS = ["id", "time_ist", "symbol", "side", "entry", "sl", "tp1", "tp2",
              "tp1_r", "tp2_r", "risk", "rule", "tf", "group", "candle_ms", "acct_risk_pct",
              "exit_mode",
              "status", "result", "r_multiple", "closed_ist"]
RESULT_TEXT = {
    "tp2": "TARGET 2 HIT",
    "tp1": "TARGET 1 HIT (rest closed at entry)",
    "loss": "STOP-LOSS HIT",
    "timeout": "CLOSED AT TIME LIMIT",
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


def account(rows=None):
    """Paper account: compounding balance, today's result %, open trades."""
    rows = read_log() if rows is None else rows
    today = datetime.now(IST).strftime("%Y-%m-%d")
    closed = sorted([r for r in rows if r.get("status") == "CLOSED" and r.get("closed_ist")],
                    key=lambda r: r["closed_ist"])
    bal, day_start = SET["start_balance"], None
    for r in closed:
        if day_start is None and r["closed_ist"] >= today:
            day_start = bal
        rk = float(r.get("acct_risk_pct") or SET["risk_per_trade"])
        bal *= 1 + float(r.get("r_multiple") or 0) * rk / 100
    if day_start is None:
        day_start = bal
    open_ = [r for r in rows if r.get("status") == "OPEN"]
    return {
        "balance": bal,
        "today_pct": (bal / day_start - 1) * 100,
        "open": len(open_),
        "open_risk": sum(float(r.get("acct_risk_pct") or SET["risk_per_trade"]) for r in open_),
    }


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
        "rule": rule_key, "tf": sig.get("tf", "15m"), "group": group, "candle_ms": sig["candle_time"],
        "acct_risk_pct": SET["risk_per_trade"], "exit_mode": SET["exit_mode"],
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
            k = mkt.klines_from(r["symbol"], r.get("tf") or "15m", int(r["candle_ms"]),
                                BACKTEST_MAX_BARS + 5)
        except Exception as e:
            print(f"[track] {r.get('symbol')}: {e}")
            continue
        if len(k["c"]) < 2:
            continue
        if r.get("exit_mode") == "ladder":
            try:
                k = mkt.klines_from(r["symbol"], r.get("tf") or "1h", int(r["candle_ms"]),
                                    LADDER_MAX_BARS + 5)
            except Exception as e:
                print(f"[track] {r.get('symbol')}: {e}")
                continue
            rr, exit_i, steps, done = simulate_ladder(k, 0, sig)
            if not done:
                continue
            r["status"] = "CLOSED"
            r["result"] = (f"LADDER: {steps} target step(s) hit" if steps else "STOP-LOSS HIT")
            r["r_multiple"] = f"{rr:.2f}"
            r["closed_ist"] = datetime.fromtimestamp(k["tc"][exit_i] / 1000, IST).strftime("%Y-%m-%d %H:%M")
            changed = True
            telegram_send(f"{'✅' if rr > 0 else '❌'} <b>Result: {r['side']} {r['symbol']}</b> "
                          f"(signal {r['time_ist']} IST)\n{r['result']} → <b>{rp(rr)}</b> of the trade amount")
            continue
        res, rr, exit_i = simulate(k, 0, sig)
        finished_window = len(k["c"]) - 1 >= BACKTEST_MAX_BARS
        if res == "open":
            continue
        if res == "timeout" and not finished_window:
            continue                                  # time limit not over yet
        if res == "tp1_end" and not finished_window:
            continue                                  # Target 1 hit, rest still running
        if res == "tp1_end":
            res = "tp1"
        r["status"] = "CLOSED"
        r["result"] = RESULT_TEXT.get(res, res)
        if res == "tp2" and abs(sig["tp1"] - sig["tp2"]) < 1e-12:
            r["result"] = "TARGET HIT"
        r["r_multiple"] = f"{rr:.2f}"
        r["closed_ist"] = datetime.fromtimestamp(k["tc"][exit_i] / 1000, IST).strftime("%Y-%m-%d %H:%M")
        changed = True
        emoji = "✅" if rr > 0 else "❌"
        telegram_send(f"{emoji} <b>Result: {r['side']} {r['symbol']}</b> (signal {r['time_ist']} IST)\n"
                      f"{r['result']} → <b>{rp(rr)}</b> of the trade amount")
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
    t2 = sum(1 for r in closed if r["result"].startswith("TARGET 2") or r["result"] == "TARGET HIT")
    t1 = sum(1 for r in closed if r["result"].startswith("TARGET 1"))
    sl = sum(1 for r in closed if r["result"].startswith("STOP"))
    to = sum(1 for r in closed if r["result"].startswith("CLOSED AT"))
    net = sum(rv)
    month_pct = 1.0
    for r in sorted(closed, key=lambda r: r.get("closed_ist", "")):
        rk = float(r.get("acct_risk_pct") or SET["risk_per_trade"])
        month_pct *= 1 + float(r["r_multiple"] or 0) * rk / 100
    month_pct = (month_pct - 1) * 100
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
        f"Target hit: {t2}",
    ] + ([f"Target 1 hit (rest at entry): {t1}"] if t1 else []) + [
        f"Stop-loss hit: {sl}",
        f"Closed at time limit: {to}",
        "",
        side_line("LONG") + "\n" + side_line("SHORT"),
        "",
        f"Total result: <b>{rp(net)}</b> of one trade amount",
        "(each loss ≈ -100%, each win = +200%; e.g. +600% = 6 times your trade amount)",
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
        if (not active or time.time() - active.get("updated", 0) > 24 * 3600
                or active.get("rr", "auto") != RR_KEY):
            print("[scan] rule-set test is missing or older than 24h -> testing now")
            backtest_and_choose(state, announce=False)
            save_state(state)
            active = state["active"]
        if not active.get("variant") or active["variant"] not in VARIANTS:
            print("[scan] paused: no profitable rule set right now")
            return 0
        P = VARIANTS[active["variant"]]
        groups = build_watchlist(mkt)
        btc = indicators(mkt.klines("BTCUSDT", P["trend_tf"], 500))
    except Exception as e:
        print("[scan] market data failed:", e)
        if time.time() - state.get("last_error_alert", 0) > 6 * 3600:
            telegram_send(f"⚠️ Scanner could not get market data: {e}\n"
                          "It will retry automatically every 15 minutes.")
            state["last_error_alert"] = time.time()
        save_state(state)
        return 0

    print(f"[scan] rule set {active['variant']} | {len(groups)} coins: {', '.join(groups)}")
    acct = account()
    print(f"[acct] balance {acct['balance']:.2f} today {acct['today_pct']:+.2f}% open {acct['open']}")
    today = datetime.now(IST).strftime("%Y-%m-%d")
    stop_reason = None
    if acct["today_pct"] >= SET["daily_profit_target"]:
        stop_reason = (f"🎯 <b>Daily target reached: {acct['today_pct']:+.2f}% today.</b>\n"
                       "No new signals until tomorrow (12:00 AM IST). Open trades keep running.")
    elif acct["today_pct"] - SET["risk_per_trade"] < -SET["daily_loss_limit"] - 1e-9:
        stop_reason = (f"🛑 <b>Daily loss limit reached: {acct['today_pct']:+.2f}% today.</b>\n"
                       "No new signals until tomorrow (12:00 AM IST). Open trades keep running.")
    if stop_reason:
        if state.get("day_stop") != today:
            telegram_send(stop_reason + f"\nPaper balance: ${acct['balance']:,.2f}")
            state["day_stop"] = today
        save_state(state)
        print("[scan] daily limit reached, no new signals today")
        return 0
    jb = len(btc["c"]) - 1
    sent = 0
    for sym, group in groups.items():
        try:
            m15 = indicators(mkt.klines(sym, P["tf"], 300))
            h1 = btc if sym == "BTCUSDT" else indicators(mkt.klines(sym, P["trend_tf"], 500))
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
            if time.time() - state["sent"].get(sym, 0) < COOLDOWN_BARS * TF_MIN[P["tf"]] * 60:
                break
            rk = SET["risk_per_trade"]
            if acct["open"] >= SET["max_open_trades"]:
                print(f"[scan] {sym}: skipped, {acct['open']} trades already open")
                break
            # worst case: every open trade + this one hits stop-loss
            if acct["today_pct"] - (acct["open_risk"] + rk) < -SET["daily_loss_limit"] - 1e-9:
                print(f"[scan] {sym}: skipped, could break today's loss limit")
                break
            telegram_send(format_signal(sig, group, mkt.mode, active, acct))
            acct["open"] += 1
            acct["open_risk"] += rk
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


# ----------------------------------------------------------------------------
# Account simulation: "what if auto-trade had run with my settings?"
# ----------------------------------------------------------------------------
SIM_DAYS = 180         # total history used
SIM_UNSEEN_DAYS = 60   # last part kept hidden from the rule-set choice
SIM_RISKS = [1, 2]   # trade sizes shown (% of balance per trade)


def ist_day(ms):
    return datetime.fromtimestamp(ms / 1000, IST).strftime("%Y-%m-%d")


def simulate_account(trades, start_ms, end_ms):
    """Replay trades in time order with balance %, max open trades and daily limits."""
    rk, target, limit = SET["risk_per_trade"], SET["daily_profit_target"], SET["daily_loss_limit"]
    tr = sorted([t for t in trades if start_ms <= t[4] < end_ms], key=lambda t: t[4])
    bal = peak = SET["start_balance"]
    max_dd = 0.0
    open_ = []                       # (exit_ms, R, risk_amount)
    day_start, day_pnl = {}, {}
    taken = won = skipped = 0
    month_start = {}
    stop_days_target, stop_days_loss = set(), set()
    ruined = [False]

    def touch(day):
        day_start.setdefault(day, bal)
        day_pnl.setdefault(day, 0.0)
        month_start.setdefault(day[:7], bal)

    def close_until(ms):
        nonlocal bal, peak, max_dd, won
        open_.sort(key=lambda o: o[0])
        while open_ and open_[0][0] <= ms:
            ex, r, amt = open_.pop(0)
            d = ist_day(ex)
            touch(d)
            pnl = amt * r
            bal += pnl
            day_pnl[d] += pnl
            if bal <= 0.01:                      # account wiped out
                bal = 0.0
                ruined[0] = True
                open_.clear()
                max_dd = 100.0
                return
            won += 1 if r > 0 else 0
            peak = max(peak, bal)
            max_dd = max(max_dd, (peak - bal) / peak * 100)

    for t in tr:
        close_until(t[4])
        if ruined[0]:
            break
        d = ist_day(t[4])
        touch(d)
        day_pct = day_pnl[d] / day_start[d] * 100
        open_risk_pct = sum(o[2] for o in open_) / bal * 100 if bal > 0 else 999
        if day_pct >= target:
            stop_days_target.add(d); skipped += 1; continue
        if day_pct - rk < -limit - 1e-9:
            stop_days_loss.add(d); skipped += 1; continue
        if len(open_) >= SET["max_open_trades"] or day_pct - (open_risk_pct + rk) < -limit - 1e-9:
            skipped += 1; continue
        open_.append((t[5], t[3], bal * rk / 100))
        taken += 1
    close_until(float("inf"))
    for d in day_pnl:                                     # days that ended at a limit
        p = day_pnl[d] / day_start[d] * 100
        if p >= target:
            stop_days_target.add(d)
        if p - rk < -limit - 1e-9 and p < 0:
            stop_days_loss.add(d)
    day_pcts = [day_pnl[d] / day_start[d] * 100 for d in day_pnl]
    months = []
    keys = sorted(month_start)
    for n, m in enumerate(keys):
        end_bal = month_start[keys[n + 1]] if n + 1 < len(keys) else bal
        months.append((m, (end_bal / month_start[m] - 1) * 100))
    start = SET["start_balance"]
    return {
        "start": start, "end": bal, "growth": (bal / start - 1) * 100,
        "taken": taken, "won": won, "skipped": skipped,
        "win": won / taken * 100 if taken else 0.0,
        "max_dd": max_dd,
        "best_day": max(day_pcts) if day_pcts else 0.0,
        "worst_day": min(day_pcts) if day_pcts else 0.0,
        "days_target": len(stop_days_target), "days_loss": len(stop_days_loss),
        "months": months, "ruined": ruined[0],
    }


def fmt_lim(x):
    return "off" if x == float("inf") else f"{x:g}"


def sim_lines(title, r):
    out = [title,
           f"${r['start']:,.2f} → <b>${r['end']:,.2f}</b> (<b>{r['growth']:+.1f}%</b>)",
           f"Trades taken: {r['taken']}  (won {r['won']}, {r['win']:.0f}%)  |  skipped by limits: {r['skipped']}",
           f"Biggest drop from a high: -{r['max_dd']:.1f}%",
           f"Best day: {r['best_day']:+.1f}%   Worst day: {r['worst_day']:+.1f}%",
           ]
    if SET["daily_profit_target"] != float("inf") or SET["daily_loss_limit"] != float("inf"):
        out.append(f"Days profit limit reached: {r['days_target']}   "
                   f"Days loss limit reached: {r['days_loss']}")
    if r["months"]:
        out.append("By month: " + ",  ".join(
            f"{datetime.strptime(m, '%Y-%m').strftime('%b')} {g:+.1f}%" for m, g in r["months"]))
    return out


def run_account_simulation():
    mkt = Market()
    groups = backtest_coins(mkt)
    all_trades, start_ms, ncoins, mode = gen_trades(mkt, groups, SIM_DAYS)
    now_ms = int(time.time() * 1000)
    split_ms = now_ms - SIM_UNSEEN_DAYS * 86400 * 1000

    # A) honest test: pick the rule set on older data only, then trade the unseen days
    older = {k: stats(t, start_ms, split_ms) for k, t in all_trades.items()}
    pick_old = choose(older)
    # B) full period with the rule set picked on all data (optimistic)
    full = {k: stats(t, start_ms, now_ms) for k, t in all_trades.items()}
    pick_full = choose(full)

    rr = f"1:{SET['rr']:g}" if SET["rr"] else "rule set's own targets"
    start = SET["start_balance"]
    lines = [
        f"💰 <b>Simulation: ${start:,.0f} start, {rr} trades</b>",
        f"Stop-loss = -100% of the trade amount, target = +200%. "
        f"{ncoins} coins, Binance {mode} data, fees included.",
        f"Daily loss limit: {fmt_lim(SET['daily_loss_limit'] / SET['risk_per_trade'])} losing trades, "
        f"max open trades: {fmt_lim(SET['max_open_trades'])}. Trade amount = % of the balance at that moment.", "",
    ]

    def block(title, trades, t0, t1):
        out = [title]
        lad = [t for t in as_ladder(trades) if t0 <= t[4] < t1]
        if lad:
            big = max(t[6] for t in lad)
            runners = sum(1 for t in lad if t[8] >= 2)
            out.append(f"Ladder mode: {runners} trades reached +400% or more, "
                       f"best single trade {rp(big)}")
        for label, tl in (("Fixed 1:2 (close all at +200%)", trades),
                          ("Ladder (half at each +200% step)", as_ladder(trades))):
            out.append(f"<i>{label}</i>")
            out += block_sizes(tl, t0, t1)
        return out

    def block_sizes(trades, t0, t1):
        out = []
        saved = SET["risk_per_trade"]
        saved_lim = SET["daily_loss_limit"]
        first = True
        for rk in SIM_RISKS:
            SET["risk_per_trade"] = rk
            # keep the daily loss limit as the same number of losing trades
            SET["daily_loss_limit"] = saved_lim * rk / saved if saved_lim != float("inf") else saved_lim
            r = simulate_account(trades, t0, t1)
            if first:
                out.append(f"Trades: <b>{r['taken']}</b>  |  won {r['won']}, lost "
                           f"{r['taken'] - r['won']}  |  win rate <b>{r['win']:.0f}%</b>")
                first = False
            if r["ruined"]:
                out.append(f"• {rk:g}% per trade: ❌ <b>account wiped out</b> (too many losing "
                           f"trades open at the same time)")
            else:
                out.append(f"• {rk:g}% per trade: ${start:,.0f} → <b>${r['end']:,.2f}</b> "
                           f"({r['growth']:+.1f}%), biggest drop -{r['max_dd']:.1f}%")
        SET["risk_per_trade"] = saved
        SET["daily_loss_limit"] = saved_lim
        return out

    if pick_old:
        lines += block(f"<b>A) Honest test — last {SIM_UNSEEN_DAYS} days</b> (strategy {pick_old} "
                       f"chosen only from the {SIM_DAYS - SIM_UNSEEN_DAYS} days before)",
                       all_trades[pick_old], split_ms, now_ms)
    else:
        lines.append(f"<b>A) Honest test — last {SIM_UNSEEN_DAYS} days:</b> no strategy was good "
                     f"enough on the older data → the bot would not have traded.")
    lines.append("")
    if pick_full:
        lines += block(f"<b>B) Full {SIM_DAYS} days</b> (strategy {pick_full}, optimistic)",
                       all_trades[pick_full], start_ms, now_ms)
    else:
        lines.append(f"<b>B) Full {SIM_DAYS} days:</b> no strategy qualified.")
    lines += ["", "\"Biggest drop\" = largest fall from a high point. Trust A more than B. "
                  "Real trading also has slippage, so expect a bit less."]
    msg = "\n".join(lines)
    telegram_send(msg)
    print(msg.replace("<b>", "").replace("</b>", ""))
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
    if mode == "simulate":
        sys.exit(run_account_simulation())
    sys.exit(run_scan())
