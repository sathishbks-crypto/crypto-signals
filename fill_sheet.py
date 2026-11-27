#!/usr/bin/env python3
"""
Fills your "Market Time Sheet 24h" Excel format with today's Binance FUTURES
15-minute candle CLOSING prices for 40 coins, with your 4 formula rows,
and sends the Excel file to Telegram.

Your format (copied from your sample, one block per coin):
  row 1: coin | date | close of each 15-min candle (5:30 AM, 5:45 AM, ...)
  row 2: running total of the closes        (=C2+D2, =D3+E2, ...)
  row 3: number of candles                  (2, 3, 4, ...)
  row 4: running average  (total / count)   (=D3/D4, ...)
  row 5: % the close is above(+)/below(-) the running average
                                            (=(100-(D5/D2)*100)/100)
A blank row separates the coins.
Colours (your conditional formatting): % row green when above 0, red when below 0.

Time columns: 5:30 AM IST (= Binance day start) every 15 minutes until
5:15 AM next day. A column is the candle that STARTS at that time
(like Binance/TradingView), filled once that candle has closed.

Coins: your favorites.txt list + today's top 10 gainers + top 10 losers.

1-SECOND SHEET (python3 fill_sheet.py 1s):
  Binance FUTURES has no 1-second candles, so this sheet uses Binance SPOT
  1-second candles (same coins on spot). Excel allows at most 16,384 columns,
  so it covers 5:30:00 AM to 10:02:59 AM (16,380 seconds).
  Futures coins named 1000XXX use spot XXX x 1000. Coins with no spot pair are left out.

Usage (on the server, in the bot folder):
  python3 fill_sheet.py 1s        -> 1-second sheet, 5:30:00 to 10:02:59 AM (or until now)
  python3 fill_sheet.py 1s 10     -> same, only the first 10 coins (smaller file)
  python3 fill_sheet.py check     -> FIRST: compares your typed BTC sample
                                     (26 Nov, 5:30-7:15 AM) with Binance futures,
                                     finds which candle your times mean, remembers it
  python3 fill_sheet.py           -> today, from 5:30 AM IST until now
Only the Python standard library is used (no extra installs).
"""

import json
import os
import sys
import time
import urllib.request
import uuid
import zipfile
from datetime import datetime, timedelta
from xml.sax.saxutils import escape

import scanner as S

DAY_MS = 86400 * 1000
SLOT_MS = 15 * 60 * 1000
SLOTS = 96                                     # 5:30 AM ... 5:15 AM next day
FIRST_COL = 3                                  # column C = 5:30 AM
BLOCK = 6                                      # 5 rows per coin + 1 blank row
SEC_SLOTS = 16380                              # 5:30:00 AM ... 10:02:59 AM (Excel column limit)
LABEL_FILE = "sheet_label.txt"                 # "start" or "end" (set by: fill_sheet.py check)

# your typed sample: BTC, 26 Nov 2026, 5:30 AM ... 7:15 AM
SAMPLE_DAY_MS = 1795651200000                  # 26 Nov 2026 00:00 UTC = 5:30 AM IST
SAMPLE = [60939.9, 60800, 60623.9, 60496, 60766, 60756.3, 60759.4, 60848]


def read_label():
    try:
        with open(LABEL_FILE) as f:
            return "end" if f.read().strip() == "end" else "start"
    except OSError:
        return "start"


# ---------------------------------------------------------------------------
# Tiny Excel (.xlsx) writer using only the standard library
# ---------------------------------------------------------------------------
def col_name(n):
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


STYLES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<numFmts count="5">
<numFmt numFmtId="168" formatCode="0.000%"/>
<numFmt numFmtId="167" formatCode="h:mm:ss AM/PM"/>
<numFmt numFmtId="164" formatCode="h:mm AM/PM"/>
<numFmt numFmtId="165" formatCode="dd\\-mmm\\-yyyy"/>
<numFmt numFmtId="166" formatCode="0.0"/>
</numFmts>
<fonts count="3">
<font><sz val="10"/><name val="Arial"/></font>
<font><b/><sz val="10"/><color rgb="FFFFFFFF"/><name val="Arial"/></font>
<font><b/><sz val="10"/><name val="Arial"/></font>
</fonts>
<fills count="3">
<fill><patternFill patternType="none"/></fill>
<fill><patternFill patternType="gray125"/></fill>
<fill><patternFill patternType="solid"><fgColor rgb="FF1F4E78"/><bgColor indexed="64"/></patternFill></fill>
</fills>
<borders count="2">
<border><left/><right/><top/><bottom/><diagonal/></border>
<border><left style="thin"><color rgb="FFBFBFBF"/></left><right style="thin"><color rgb="FFBFBFBF"/></right><top style="thin"><color rgb="FFBFBFBF"/></top><bottom style="thin"><color rgb="FFBFBFBF"/></bottom><diagonal/></border>
</borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="10">
<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
<xf numFmtId="0" fontId="1" fillId="2" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center" wrapText="1"/></xf>
<xf numFmtId="164" fontId="1" fillId="2" borderId="1" xfId="0" applyNumberFormat="1" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center" wrapText="1"/></xf>
<xf numFmtId="165" fontId="0" fillId="0" borderId="1" xfId="0" applyNumberFormat="1" applyFont="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center"/></xf>
<xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyFont="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center"/></xf>
<xf numFmtId="166" fontId="0" fillId="0" borderId="1" xfId="0" applyNumberFormat="1" applyFont="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center"/></xf>
<xf numFmtId="10" fontId="0" fillId="0" borderId="1" xfId="0" applyNumberFormat="1" applyFont="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center"/></xf>
<xf numFmtId="0" fontId="2" fillId="0" borderId="1" xfId="0" applyFont="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center"/></xf>
<xf numFmtId="167" fontId="1" fillId="2" borderId="1" xfId="0" applyNumberFormat="1" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center" vertical="center" wrapText="1"/></xf>
<xf numFmtId="168" fontId="0" fillId="0" borderId="1" xfId="0" applyNumberFormat="1" applyFont="1" applyBorder="1" applyAlignment="1"><alignment horizontal="center"/></xf>
</cellXfs>
<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
<dxfs count="2">
<dxf><fill><patternFill><bgColor rgb="FFFF0000"/></patternFill></fill></dxf>
<dxf><fill><patternFill><bgColor rgb="FF92D050"/></patternFill></fill></dxf>
</dxfs>
</styleSheet>"""
ST_HDR, ST_HDR_TIME, ST_DATE, ST_BODY, ST_AVG, ST_PCT, ST_COIN, ST_HDR_SEC, ST_PCT3 = 1, 2, 3, 4, 5, 6, 7, 8, 9


def cell_xml(ref, val, style):
    if val is None:
        return f'<c r="{ref}" s="{style}"/>'
    if isinstance(val, str) and val.startswith("="):
        return f'<c r="{ref}" s="{style}"><f>{escape(val[1:])}</f></c>'
    if isinstance(val, str):
        return f'<c r="{ref}" s="{style}" t="inlineStr"><is><t>{escape(val)}</t></is></c>'
    return f'<c r="{ref}" s="{style}"><v>{repr(float(val)) if isinstance(val, float) else val}</v></c>'


def write_xlsx(path, rows, ncols, sheet="Time Sheet 24h", color_rows=()):
    """rows: {row_number: {col_number: (value, style)}} or an iterator of
    (row_number, {col_number: (value, style)}) — written one row at a time,
    so even a very big sheet needs little memory.
    color_rows: rows that get your colour rule (green above 0, red below 0)."""
    cols = ['<col min="1" max="1" width="16.3" customWidth="1"/>',
            '<col min="2" max="2" width="14" customWidth="1"/>',
            f'<col min="3" max="{ncols}" width="11" customWidth="1"/>']
    if isinstance(rows, dict):
        rows = ((r, rows[r]) for r in sorted(rows))
    head = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetViews><sheetView workbookViewId="0">'
        '<pane xSplit="2" ySplit="1" topLeftCell="C2" activePane="bottomRight" state="frozen"/>'
        '<selection pane="bottomRight" activeCell="C2" sqref="C2"/></sheetView></sheetViews>'
        '<sheetFormatPr defaultRowHeight="13"/>'
        f'<cols>{"".join(cols)}</cols><sheetData>')
    tail = ('</sheetData>' + "".join(
            f'<conditionalFormatting sqref="{col_name(FIRST_COL + 1)}{r}:{col_name(ncols)}{r}">'
            f'<cfRule type="cellIs" dxfId="1" priority="{2 * n + 1}" operator="greaterThan"><formula>0</formula></cfRule>'
            f'<cfRule type="cellIs" dxfId="0" priority="{2 * n + 2}" operator="lessThan"><formula>0</formula></cfRule>'
            f'</conditionalFormatting>' for n, r in enumerate(color_rows))
        + '</worksheet>')
    files = {
        "[Content_Types].xml":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
            '</Types>',
        "_rels/.rels":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            '</Relationships>',
        "xl/workbook.xml":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            f'<sheets><sheet name="{escape(sheet)}" sheetId="1" r:id="rId1"/></sheets>'
            '<calcPr calcId="191029" fullCalcOnLoad="1"/></workbook>',
        "xl/_rels/workbook.xml.rels":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
            '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
            '</Relationships>',
        "xl/styles.xml": STYLES,
    }
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for name, text in files.items():
            z.writestr(name, text)
        with z.open("xl/worksheets/sheet1.xml", "w", force_zip64=True) as f:
            f.write(head.encode())
            for r, cells in rows:
                ht = ' ht="30" customHeight="1"' if r == 1 else ""
                f.write((f'<row r="{r}"{ht}>'
                         + "".join(cell_xml(f"{col_name(c)}{r}", v, st)
                                   for c, (v, st) in sorted(cells.items()))
                         + '</row>').encode())
            f.write(tail.encode())


# ---------------------------------------------------------------------------
# Build the sheet in your format
# ---------------------------------------------------------------------------
def build_rows(day_start_ms, coins, slots=SLOTS, slot_sec=15 * 60):
    """Yields the sheet one row at a time.
    coins: list of (name, {slot_index: close_price})"""
    hdr_style = ST_HDR_SEC if slot_sec < 60 else ST_HDR_TIME
    pct_style = ST_PCT3 if slot_sec < 60 else ST_PCT          # 1-second moves are tiny: 3 decimals
    head = {1: ("CRYPTO", ST_HDR), 2: ("Date", ST_HDR)}
    for k in range(slots):
        head[FIRST_COL + k] = (((5 * 3600 + 30 * 60 + slot_sec * k) % 86400) / 86400, hdr_style)
    yield 1, head
    day = datetime.utcfromtimestamp(day_start_ms / 1000).date()
    date_serial = (day - datetime(1899, 12, 30).date()).days
    for n, (name, closes) in enumerate(coins):
        r = 2 + n * BLOCK                                     # price row
        last = max(closes) if closes else -1                 # last filled time slot
        price = {1: (name, ST_COIN), 2: (date_serial, ST_DATE)}
        extra = [{1: (None, ST_BODY), 2: (None, ST_BODY)} for _ in range(4)]
        for k in range(slots):
            price[FIRST_COL + k] = (closes.get(k), ST_BODY)
            for e in extra:
                e[FIRST_COL + k] = (None, ST_BODY)
        for k in range(1, last + 1):                         # formulas only where prices exist
            c, p = col_name(FIRST_COL + k), col_name(FIRST_COL + k - 1)
            extra[0][FIRST_COL + k] = (f"={p}{r}+{c}{r}" if k == 1 else f"={p}{r + 1}+{c}{r}", ST_BODY)
            extra[1][FIRST_COL + k] = (k + 1, ST_BODY)
            extra[2][FIRST_COL + k] = (f"={c}{r + 1}/{c}{r + 2}", ST_AVG)
            extra[3][FIRST_COL + k] = (f"=(100-({c}{r + 3}/{c}{r})*100)/100", pct_style)
        yield r, price
        for j, e in enumerate(extra, 1):
            yield r + j, e


def fetch_closes(mkt, sym, day_start_ms, now_ms, label="start"):
    """label start: 5:30 column = candle 5:30-5:45.  end: 5:30 column = candle 5:15-5:30."""
    shift = SLOT_MS if label == "end" else 0
    raw = mkt._raw(sym, "15m", SLOTS + 1, start_time=day_start_ms - shift)
    out = {}
    for b in raw:
        t, close_time = int(b[0]), int(b[6])
        k = (t + shift - day_start_ms) // SLOT_MS
        if 0 <= k < SLOTS and close_time < now_ms:            # closed candles only
            out[k] = float(b[4])
    # fill a missing candle (no trades) with the previous close so formulas stay correct
    if out:
        for k in range(min(out), max(out) + 1):
            if k not in out:
                out[k] = out[k - 1]
    return out


# ---------------------------------------------------------------------------
# Telegram file
# ---------------------------------------------------------------------------
def send_document(path, caption):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat:
        print("[telegram] token/chat id not set -> file only saved:", path)
        return False
    b = uuid.uuid4().hex
    with open(path, "rb") as f:
        blob = f.read()
    parts = [f"--{b}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode()
             for k, v in (("chat_id", chat), ("caption", caption[:1000]))]
    parts.append(f"--{b}\r\nContent-Disposition: form-data; name=\"document\"; "
                 f"filename=\"{os.path.basename(path)}\"\r\nContent-Type: application/"
                 f"vnd.openxmlformats-officedocument.spreadsheetml.sheet\r\n\r\n".encode() + blob + b"\r\n")
    parts.append(f"--{b}--\r\n".encode())
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendDocument",
                                 data=b"".join(parts),
                                 headers={"Content-Type": f"multipart/form-data; boundary={b}"})
    try:
        return bool(json.loads(urllib.request.urlopen(req, timeout=60).read().decode()).get("ok"))
    except Exception as e:
        print("[telegram] file error:", e)
        return False


# ---------------------------------------------------------------------------
# Spot 1-second candles
# ---------------------------------------------------------------------------
_SPOT = None


def spot_symbol(sym):
    """Futures symbol -> (spot symbol, price factor) or None."""
    global _SPOT
    if _SPOT is None:
        _SPOT = {r["symbol"] for r in S.http_get(S.SPOT_HOST + "/api/v3/ticker/price")}
    if sym in _SPOT:
        return sym, 1.0
    for pre, f in (("1000000", 1e6), ("1000", 1e3), ("1M", 1e6)):
        if sym.startswith(pre) and sym[len(pre):] in _SPOT:
            return sym[len(pre):], f
    return None


def spot_1s(spot_sym, factor, start_ms, n, now_ms, stop_early=None):
    """Spot 1-second closes from start_ms: {second_index: close}. Missing seconds
    (no trade) are filled with the previous close. stop_early(closes) -> True ends early."""
    out, st, end = {}, start_ms, min(start_ms + n * 1000, now_ms)
    while st < end:
        rows = S.http_get(S.SPOT_HOST + "/api/v3/klines",
                          {"symbol": spot_sym, "interval": "1s", "startTime": st,
                           "endTime": end - 1, "limit": 1000})
        if not rows:
            st += 1000 * 1000                          # quiet coin: jump ahead
            continue
        for r in rows:
            k = (int(r[0]) - start_ms) // 1000
            if 0 <= k < n and int(r[6]) < now_ms:
                out[k] = float(r[4]) * factor
        st = int(rows[-1][0]) + 1000
        if stop_early and stop_early(out):
            break
        time.sleep(0.05)
    if out:
        last = None
        for k in range(max(out) + 1):
            if k in out:
                last = out[k]
            elif last is not None:
                out[k] = last
    return out


def main_1s(mkt, max_coins):
    now_ms = int(time.time() * 1000)
    day_start = now_ms // DAY_MS * DAY_MS                      # 00:00 UTC = 5:30 AM IST
    groups = list(S.build_watchlist(mkt))[:max_coins]
    coins, missing = [], []
    for n, sym in enumerate(groups, 1):
        try:
            m = spot_symbol(sym)
            if not m:
                missing.append(sym)
                continue
            closes = spot_1s(m[0], m[1], day_start, SEC_SLOTS, now_ms)
        except Exception as e:
            print(f"[sheet] {sym}: {e}")
            missing.append(sym)
            continue
        coins.append((sym[:-4] if sym.endswith("USDT") else sym, closes))
        print(f"[sheet] {n}/{len(groups)} {sym}: {len(closes)} seconds", flush=True)
    if not coins:
        S.telegram_send("⚠️ 1-second sheet: no coin data.")
        return 1
    ist_now = datetime.now(S.IST)
    path = f"Market_Time_Sheet_1s_{ist_now:%d-%b-%Y_%I%M%p}.xlsx"
    pct_rows = [2 + n * BLOCK + 4 for n in range(len(coins))]
    write_xlsx(path, build_rows(day_start, coins, SEC_SLOTS, 1), FIRST_COL + SEC_SLOTS - 1,
               sheet="Time Sheet 1s", color_rows=pct_rows)
    size_mb = os.path.getsize(path) / 1e6
    filled = max((max(c) + 1 for _, c in coins if c), default=0)
    last_txt = (datetime(2000, 1, 1, 5, 30) + timedelta(seconds=filled - 1)).strftime("%I:%M:%S %p")
    caption = (f"📒 1-second sheet — {ist_now:%d %b %Y}, {len(coins)} coins (Binance SPOT 1s; "
               f"futures has no 1s candles)\n05:30:00 AM to {last_txt} | {size_mb:.1f} MB"
               + (f"\nNo spot pair: {', '.join(m[:-4] for m in missing)}" if missing else ""))
    if size_mb > 49:
        S.telegram_send(caption + "\n⚠️ File is too big for Telegram (50 MB). "
                        "Run again with fewer coins, e.g.: python3 fill_sheet.py 1s 15")
        print(f"[sheet] saved {path} ({size_mb:.1f} MB) — too big for Telegram")
        return 0
    ok = send_document(path, caption)
    print(f"[sheet] saved {path} ({size_mb:.1f} MB), sent to Telegram: {ok}")
    return 0


def check(mkt):
    """Compare your typed BTC sample with Binance futures, both ways of reading the time."""
    now_ms = int(time.time() * 1000)
    res = {lab: fetch_closes(mkt, "BTCUSDT", SAMPLE_DAY_MS, now_ms, lab) for lab in ("start", "end")}
    lines = ["🔎 <b>Sample check — BTC 26 Nov (Binance futures)</b>",
             "Time | yours | candle STARTING at time | candle ENDING at time"]
    score = {"start": 0, "end": 0}
    for k, mine in enumerate(SAMPLE):
        a, b = res["start"].get(k), res["end"].get(k)
        for lab, v in (("start", a), ("end", b)):
            if v is not None and abs(v - mine) <= max(0.05, mine * 0.00002):
                score[lab] += 1
        t = (datetime(2000, 1, 1, 5, 30) + timedelta(minutes=15 * k)).strftime("%I:%M %p")
        mark = lambda v: ("—" if v is None else f"{v:g}" + (" ✅" if abs(v - mine) <= max(0.05, mine * 0.00002) else " ❌"))
        lines.append(f"{t} | {mine:g} | {mark(a)} | {mark(b)}")
    best = max(score, key=score.get)
    lines.append("")
    if score[best] == 0:
        lines.append("❌ None of your values match Binance futures 15-min closes. "
                     "Please check the chart (futures, 15m) and send me this message.")
        best = "start"
    else:
        lines.append(f"<b>{score[best]} of {len(SAMPLE)} match</b> when the time means the candle "
                     f"{'STARTING' if best == 'start' else 'ENDING'} at that time → this is used from now on.")
        if score[best] < len(SAMPLE):
            lines.append("⚠️ Not all match — please look at the ❌ rows on the chart.")
    with open(LABEL_FILE, "w") as f:
        f.write(best)
    msg = "\n".join(lines)
    print(msg.replace("<b>", "").replace("</b>", ""))
    S.telegram_send(msg)
    return 0


def main():
    try:
        mkt = S.Market()
    except Exception as e:
        S.telegram_send(f"⚠️ Time sheet not made: {e}")
        print(e)
        return 1
    if any(a.lower() == "check" for a in sys.argv[1:]):
        return check(mkt)
    if any(a.lower() == "1s" for a in sys.argv[1:]):
        nums = [int(a) for a in sys.argv[1:] if a.isdigit()]
        return main_1s(mkt, nums[0] if nums else 40)
    label = read_label()
    now_ms = int(time.time() * 1000)
    day_start = now_ms // DAY_MS * DAY_MS                      # 00:00 UTC = 5:30 AM IST
    groups = S.build_watchlist(mkt)                            # your list + gainers + losers
    coins = []
    for sym in groups:
        try:
            closes = fetch_closes(mkt, sym, day_start, now_ms, label)
        except Exception as e:
            print(f"[sheet] {sym}: {e}")
            continue
        name = sym[:-4] if sym.endswith("USDT") else sym
        coins.append((name, closes))
        time.sleep(0.1)
    if not coins:
        S.telegram_send("⚠️ Time sheet: no coin data.")
        return 1
    ist_now = datetime.now(S.IST)
    path = f"Market_Time_Sheet_24h_{ist_now:%d-%b-%Y_%I%M%p}.xlsx"
    pct_rows = [2 + n * BLOCK + 4 for n in range(len(coins))]   # your % row of every coin
    write_xlsx(path, build_rows(day_start, coins), FIRST_COL + SLOTS - 1, color_rows=pct_rows)
    filled = max((max(c) + 1 for _, c in coins if c), default=0)
    last_txt = (datetime.utcfromtimestamp(day_start / 1000) + timedelta(hours=5, minutes=30)
                + timedelta(minutes=15 * (filled - 1))).strftime("%I:%M %p") if filled else "-"
    caption = (f"📒 Time sheet — {ist_now:%d %b %Y}, {len(coins)} coins (Binance {mkt.mode})\n"
               f"15-min closes, 05:30 AM to {last_txt} column (made at {ist_now:%I:%M %p} IST)\n"
               f"Each time = close of the candle {'STARTING' if label == 'start' else 'ENDING'} at that time")
    ok = send_document(path, caption)
    print(f"[sheet] saved {path}, sent to Telegram: {ok}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
