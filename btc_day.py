#!/usr/bin/env python3
"""
One coin (BTC), one full day, 1-second candles, your time-sheet rule, 1:1.

WHAT IT DOES
  1) Builds your Excel time sheet for the whole day (5:30:00 AM to 5:29:59 AM
     next day = 86,400 seconds). Excel allows only 16,384 columns, so the day is
     split into 6 blocks of 4 hours, one under the other:
        5:30-9:30 AM, 9:30 AM-1:30 PM, 1:30-5:30 PM, 5:30-9:30 PM,
        9:30 PM-1:30 AM, 1:30-5:30 AM
     Each block: time row, price row, your 4 formula rows, a TRADE row.
     The running total and count carry on from one block to the next, so the
     average is for the whole day (same as one long sheet).
     Green/red colouring on every % row, like your sheet.
  2) Runs your rule on it:
       - wait for the first colour change of the day
       - % row reaches +0.5% -> LONG, -0.5% -> SHORT
       - target 0.5% / stop-loss 0.5% of the price  (1 : 1)
       - after a trade closes, the % row must come back inside -0.5%..+0.5%,
         then the next +/-0.5% gives the next trade (as many times as it happens)
       - a trade still open at 5:30 AM is closed there
  3) Money (my assumption, so no trade is ever skipped):
       start balance $1000, every trade risks 1% of the current balance.
       Stop 0.5% -> position = 2 x balance (e.g. $2000 with $1000, which is
       $200 margin at 10x). Win = +1% of balance, loss = -1%, minus fees.
       Fees + slippage 0.15% of the position (in + out) = 0.3% of balance.
     Only one BTC trade is open at a time, so money never runs short.
  Colours/prices come from Binance SPOT 1-second candles (futures has no 1s);
  the report also shows the FUTURES entry price.

Sends the Excel file (sheet "BTC 1s" + sheet "Trades") and a short report to Telegram.

Usage (on the server, in the bot folder):
  python3 btc_day.py                 -> yesterday (5:30 AM to 5:30 AM), BTC
  python3 btc_day.py 2026-11-26      -> that day
  python3 btc_day.py ETHUSDT         -> another coin
  python3 btc_day.py 0.5 0.5 0.5     -> trigger %, target %, stop %
  python3 btc_day.py days 30         -> the last 30 days in ONE report (no Excel);
                                        the balance carries on from day to day
"""

import os
import sys
import time
import zipfile
from datetime import datetime, timedelta, timezone
from xml.sax.saxutils import escape

import fill_sheet as F
import scanner as S
import watch_test as W

DAY_MS = 86400 * 1000
BLOCK_SEC = 14400                    # 4 hours per block
BLOCKS = 6
FIRST_COL = 3                        # column C = first second of the block
ROWS_PER_BLOCK = 8                   # time, price, total, count, average, %, trade, blank
START_BAL = 1000.0
RISK = 0.01                          # 1% of balance per trade
COST = 0.0015                        # fees + slippage, of the position


# ---------------------------------------------------------------------------
# Excel with two sheets (standard library only)
# ---------------------------------------------------------------------------
def sheet_xml_parts(cols_xml, freeze, color_ranges):
    pane = ('<pane xSplit="2" topLeftCell="C1" activePane="topRight" state="frozen"/>'
            if freeze == "cols" else
            '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>')
    head = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f'<sheetViews><sheetView workbookViewId="0">{pane}</sheetView></sheetViews>'
            '<sheetFormatPr defaultRowHeight="13"/>'
            f'<cols>{cols_xml}</cols><sheetData>')
    tail = ('</sheetData>' + "".join(
        f'<conditionalFormatting sqref="{rng}">'
        f'<cfRule type="cellIs" dxfId="1" priority="{2 * n + 1}" operator="greaterThan"><formula>0</formula></cfRule>'
        f'<cfRule type="cellIs" dxfId="0" priority="{2 * n + 2}" operator="lessThan"><formula>0</formula></cfRule>'
        '</conditionalFormatting>' for n, rng in enumerate(color_ranges)) + '</worksheet>')
    return head, tail


def write_book(path, sheets):
    """sheets: list of (name, rows_iterator, cols_xml, freeze, color_ranges)"""
    ct = "".join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/'
                 f'vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                 for i in range(1, len(sheets) + 1))
    files = {
        "[Content_Types].xml":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            f'{ct}<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
            '</Types>',
        "_rels/.rels":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
            '</Relationships>',
        "xl/workbook.xml":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'
            + "".join(f'<sheet name="{escape(s[0])}" sheetId="{i}" r:id="rId{i}"/>'
                      for i, s in enumerate(sheets, 1))
            + '</sheets><calcPr calcId="191029" fullCalcOnLoad="1"/></workbook>',
        "xl/_rels/workbook.xml.rels":
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            + "".join(f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/'
                      f'2006/relationships/worksheet" Target="worksheets/sheet{i}.xml"/>'
                      for i in range(1, len(sheets) + 1))
            + f'<Relationship Id="rId{len(sheets) + 1}" Type="http://schemas.openxmlformats.org/'
              'officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>',
        "xl/styles.xml": F.STYLES,
    }
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for name, text in files.items():
            z.writestr(name, text)
        for i, (name, rows, cols_xml, freeze, cranges) in enumerate(sheets, 1):
            head, tail = sheet_xml_parts(cols_xml, freeze, cranges)
            with z.open(f"xl/worksheets/sheet{i}.xml", "w", force_zip64=True) as f:
                f.write(head.encode())
                for r, cells in rows:
                    f.write((f'<row r="{r}">' + "".join(
                        F.cell_xml(f"{F.col_name(c)}{r}", v, st) for c, (v, st) in sorted(cells.items()))
                        + '</row>').encode())
                f.write(tail.encode())


# ---------------------------------------------------------------------------
# The day sheet in your format, in 6 blocks
# ---------------------------------------------------------------------------
def block_rows(day_ms, name, closes, marks):
    """marks: {second: text} for the TRADE row."""
    date_serial = (datetime.utcfromtimestamp(day_ms / 1000).date() - datetime(1899, 12, 30).date()).days
    last_col = FIRST_COL + BLOCK_SEC - 1
    for b in range(BLOCKS):
        r0 = 1 + b * ROWS_PER_BLOCK
        P, SM, N, AV, PC, TR = r0 + 1, r0 + 2, r0 + 3, r0 + 4, r0 + 5, r0 + 6
        start = (5 * 3600 + 30 * 60 + b * BLOCK_SEC)
        t1 = (datetime(2000, 1, 1) + timedelta(seconds=start)).strftime("%I:%M %p")
        t2 = (datetime(2000, 1, 1) + timedelta(seconds=start + BLOCK_SEC)).strftime("%I:%M %p")
        rows = {r: {} for r in (r0, P, SM, N, AV, PC, TR)}
        rows[r0][1] = (f"Block {b + 1}", F.ST_HDR)
        rows[r0][2] = (f"{t1}-{t2}", F.ST_HDR)
        rows[P][1], rows[P][2] = (name, F.ST_COIN), (date_serial, F.ST_DATE)
        rows[SM][2] = ("Total", F.ST_BODY)
        rows[N][2] = ("Count", F.ST_BODY)
        rows[AV][2] = ("Average", F.ST_BODY)
        rows[PC][2] = ("% row", F.ST_BODY)
        rows[TR][2] = ("Trade", F.ST_COIN)
        for j in range(BLOCK_SEC):
            g = b * BLOCK_SEC + j                     # second of the day
            col = FIRST_COL + j
            c = F.col_name(col)
            rows[r0][col] = (((start + j) % 86400) / 86400, F.ST_HDR_SEC)
            price = closes.get(g)
            rows[P][col] = (price, F.ST_BODY)
            if marks.get(g):
                rows[TR][col] = (marks[g], F.ST_COIN)
            if price is None or g == 0:
                continue
            if g == 1:
                total = f"=C{P}+D{P}"
            elif j == 0:                              # carry on from the block above
                total = f"={F.col_name(last_col)}{SM - ROWS_PER_BLOCK}+{c}{P}"
            else:
                total = f"={F.col_name(col - 1)}{SM}+{c}{P}"
            rows[SM][col] = (total, F.ST_BODY)
            rows[N][col] = (g + 1, F.ST_BODY)
            rows[AV][col] = (f"={c}{SM}/{c}{N}", F.ST_AVG)
            rows[PC][col] = (f"=(100-({c}{AV}/{c}{P})*100)/100", F.ST_PCT3)
        for r in sorted(rows):
            yield r, rows[r]


def trades_rows(trades, summary):
    head = ["#", "Side", "Entry time (IST)", "Entry price (futures)", "% row at entry",
            "Exit time (IST)", "Result", "Price move", "Trade size ($)", "Profit / loss ($)",
            "Balance after ($)"]
    yield 1, {i + 1: (h, F.ST_HDR) for i, h in enumerate(head)}
    r = 2
    for n, t in enumerate(trades, 1):
        vals = [n, t["side_txt"], t["in_txt"], round(t["fut"], 2), f"{t['pct'] * 100:+.3f}%",
                t["out_txt"], t["why"], f"{t['move'] * 100:+.2f}%", round(t["size"], 2),
                round(t["usd"], 2), round(t["bal"], 2)]
        yield r, {i + 1: (v, F.ST_BODY) for i, v in enumerate(vals)}
        r += 1
    r += 1
    for k, v in summary:
        yield r, {1: (k, F.ST_COIN), 3: (v, F.ST_BODY)}
        r += 1


# ---------------------------------------------------------------------------
def main_days(sym, ndays):
    """Many days, one report, no Excel. Balance carries on day to day."""
    try:
        mkt = S.Market()
    except Exception as e:
        S.telegram_send(f"⚠️ BTC days test did not run: {e}")
        print(e)
        return 1
    m = F.spot_symbol(sym)
    if not m:
        S.telegram_send(f"⚠️ {sym} has no spot pair (needed for 1-second candles).")
        return 1
    now_ms = int(time.time() * 1000)
    last_day = (now_ms // DAY_MS - 1) * DAY_MS                    # yesterday
    first_day = last_day - (ndays - 1) * DAY_MS
    ist = lambda ms: datetime.fromtimestamp(ms / 1000, S.IST)
    bal, peak, max_dd = START_BAL, START_BAL, 0.0
    tot_n = tot_w = tot_l = 0
    gross = fees = 0.0
    rows = []
    for d in range(ndays):
        day = first_day + d * DAY_MS
        print(f"[days] {d + 1}/{ndays} {ist(day):%d %b}: downloading…", flush=True)
        try:
            closes = F.spot_1s(m[0], m[1], day, 86400, now_ms)
        except Exception as e:
            rows.append(f"{ist(day):%d %b}: data error ({e})")
            continue
        w = W.Walker(multi=True)
        w.feed(closes)
        raw, _ = w.final()
        start = bal
        dw = dl = 0
        for t in raw:
            size = bal * RISK / W.STOP
            fee = size * COST
            gross += size * t["move"]
            fees += fee
            bal += size * t["move"] - fee
            peak = max(peak, bal)
            max_dd = max(max_dd, (peak - bal) / peak * 100)
            dw += t["why"] == "target"
            dl += t["why"] == "stop"
        tot_n += len(raw)
        tot_w += dw
        tot_l += dl
        icon = "🟢" if bal > start else "🔴" if bal < start else "⚪"
        rows.append(f"{icon} {ist(day):%d %b}: {len(raw)} trades (✅{dw} ❌{dl}) "
                    f"{bal - start:+.2f}$ → ${bal:.2f}")
    tp, sl, tr = W.TARGET * 100, W.STOP * 100, W.TRIGGER * 100
    be = (W.STOP + COST) / (W.TARGET + W.STOP) * 100
    green = sum(1 for r in rows if r.startswith("🟢"))
    red = sum(1 for r in rows if r.startswith("🔴"))
    lines = [f"📘 <b>{sym[:-4]} — 1-second test, last {ndays} days "
             f"({ist(first_day):%d %b} → {ist(last_day):%d %b})</b>",
             f"Rule: after the first colour change, % row ±{tr:.2f}% → trade. Target {tp:.2f}% / "
             f"stop {sl:.2f}% (1:{W.TARGET / W.STOP:g}). Re-enter after each close.",
             "Money (assumed): start $1000, each trade risks 1% of the balance, the balance "
             "carries on day to day. Fees + slippage 0.15% of the position.", ""]
    lines += rows
    lines += ["",
              f"<b>Summary:</b> {tot_n} trades — ✅ {tot_w} target, ❌ {tot_l} stop"
              + (f" ({tot_w / max(tot_w + tot_l, 1) * 100:.0f}% win; break-even about {be:.0f}%)"
                 if tot_n else ""),
              f"Days: 🟢 {green} profit, 🔴 {red} loss",
              f"Profit before fees: <b>${gross:+.2f}</b> | fees + slippage: -${fees:.2f}",
              f"Balance: $1000 → <b>${bal:.2f}</b> ({(bal / START_BAL - 1) * 100:+.2f}%)",
              f"Biggest fall from a high: -{max_dd:.1f}%"]
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


def main():
    args = sys.argv[1:]
    ndays = None
    if "days" in [a.lower() for a in args]:
        i = [a.lower() for a in args].index("days")
        ndays = int(args[i + 1]) if i + 1 < len(args) and args[i + 1].isdigit() else 30
        args = args[:i] + args[i + 2:]
    sym = next((a.upper() for a in args if a.upper().endswith("USDT")), "BTCUSDT")
    nums = [float(a) for a in args if a.replace(".", "", 1).isdigit()]
    if len(nums) >= 3:
        W.TRIGGER, W.TARGET, W.STOP = nums[0] / 100, nums[1] / 100, nums[2] / 100
    else:
        W.TRIGGER, W.TARGET, W.STOP = 0.005, 0.005, 0.005          # 1 : 1
    if ndays:
        return main_days(sym, min(max(ndays, 1), 60))
    now_ms = int(time.time() * 1000)
    date_arg = next((a for a in args if len(a) == 10 and a[4] == "-"), None)
    if date_arg:
        day = int(datetime.strptime(date_arg, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)
    else:
        day = (now_ms // DAY_MS - 1) * DAY_MS                        # yesterday 5:30 AM IST
    try:
        mkt = S.Market()
    except Exception as e:
        S.telegram_send(f"⚠️ BTC day test did not run: {e}")
        print(e)
        return 1
    m = F.spot_symbol(sym)
    if not m:
        S.telegram_send(f"⚠️ {sym} has no spot pair (needed for 1-second candles).")
        return 1
    print(f"[day] {sym}: downloading 86,400 one-second candles…", flush=True)
    closes = F.spot_1s(m[0], m[1], day, 86400, now_ms)
    if not closes:
        S.telegram_send(f"⚠️ No 1-second data for {sym}.")
        return 1
    w = W.Walker(multi=True)
    w.feed(closes)
    raw, status = w.final()
    ist = lambda ms: datetime.fromtimestamp(ms / 1000, S.IST)

    bal, trades, marks = START_BAL, [], {}
    wins = losses = 0
    gross = fees = 0.0
    for t in raw:
        size = bal * RISK / W.STOP                    # position so that the stop loses 1%
        usd_move = size * t["move"]
        fee = size * COST
        usd = usd_move - fee
        bal += usd
        gross += usd_move
        fees += fee
        why = {"target": "target", "stop": "stop", "open": "closed 5:30 AM"}[t["why"]]
        wins += t["why"] == "target"
        losses += t["why"] == "stop"
        minute = (day + t["k"] * 1000) // 60000 * 60000
        fut = t["price"] * W.futures_gap(mkt, sym, minute, closes.get((minute - day) // 1000))
        side = "LONG" if t["side"] == 1 else "SHORT"
        trades.append({**t, "side_txt": side, "size": size, "usd": usd, "bal": bal, "fut": fut,
                       "why": why,
                       "in_txt": ist(day + (t["k"] + 1) * 1000).strftime("%I:%M:%S %p"),
                       "out_txt": ist(day + (t["exit_k"] + 1) * 1000).strftime("%I:%M:%S %p")})
        marks[t["k"]] = f"{side} in"
        marks[t["exit_k"]] = {"target": "TARGET ✓", "stop": "STOP ✗"}.get(t["why"], "close")

    n = len(trades)
    tp, sl, tr = W.TARGET * 100, W.STOP * 100, W.TRIGGER * 100
    be = (W.STOP + COST) / (W.TARGET + W.STOP) * 100
    summary = [
        ("Coin", sym), ("Day", f"{ist(day):%d %b %Y} 5:30 AM to next day 5:30 AM IST"),
        ("Rule", f"first colour change, then ±{tr:.2f}% → trade; target {tp:.2f}% / stop {sl:.2f}%"),
        ("Trades", n), ("Target hit", wins), ("Stop hit", losses),
        ("Win %", f"{wins / n * 100:.0f}%" if n else "-"),
        ("Needed to break even", f"{be:.0f}% (because of fees)"),
        ("Start balance", "$1000.00"), ("End balance", f"${bal:.2f}"),
        ("Profit before fees", f"${gross:+.2f}"), ("Fees + slippage", f"-${fees:.2f}"),
    ]
    ist_now = datetime.now(S.IST)
    path = f"{sym[:-4]}_1s_{ist(day):%d-%b-%Y}.xlsx"
    name = sym[:-4]
    pct_ranges = [f"D{1 + b * ROWS_PER_BLOCK + 5}:{F.col_name(FIRST_COL + BLOCK_SEC - 1)}"
                  f"{1 + b * ROWS_PER_BLOCK + 5}" for b in range(BLOCKS)]
    cols1 = ('<col min="1" max="1" width="12" customWidth="1"/>'
             '<col min="2" max="2" width="18" customWidth="1"/>'
             f'<col min="3" max="{FIRST_COL + BLOCK_SEC - 1}" width="12" customWidth="1"/>')
    cols2 = ('<col min="1" max="2" width="8" customWidth="1"/>'
             '<col min="3" max="11" width="18" customWidth="1"/>')
    write_book(path, [
        (f"{name} 1s", block_rows(day, name, closes, marks), cols1, "cols", pct_ranges),
        ("Trades", trades_rows(trades, summary), cols2, "rows", []),
    ])
    size_mb = os.path.getsize(path) / 1e6

    lines = [f"📘 <b>{name} — 1-second test, {ist(day):%d %b} 5:30 AM → {ist(day + DAY_MS):%d %b} 5:30 AM</b>",
             f"Rule: after the first colour change, % row ±{tr:.2f}% → trade. Target {tp:.2f}% / "
             f"stop {sl:.2f}% (1:{W.TARGET / W.STOP:g}). Re-enter after each close.",
             "Money (assumed): start $1000, each trade risks 1% of the balance "
             f"(position about 2 x balance). Fees + slippage 0.15% of the position.", ""]
    show = trades if n <= 40 else trades[:15] + trades[-10:]
    for i, t in enumerate(show):
        if n > 40 and i == 15:
            lines.append(f"… {n - 25} more trades in the Excel file (sheet Trades) …")
        icon = {"target": "✅", "stop": "❌"}.get(t["why"], "⏹")
        lines.append(f"{icon} {t['side_txt']} {t['in_txt']} @ {S.fmt_price(t['fut'])} → "
                     f"{t['why']} {t['out_txt']} <b>{t['usd']:+.2f}$</b> (bal ${t['bal']:.0f})")
    lines += ["",
              f"<b>Summary:</b> {n} trades — ✅ {wins} target, ❌ {losses} stop"
              + (f", ⏹ {n - wins - losses} closed at 5:30 AM" if n - wins - losses else ""),
              f"Win rate {wins / n * 100:.0f}% (break-even needs about {be:.0f}% because of fees)"
              if n else "No trade today.",
              f"Profit before fees: <b>${gross:+.2f}</b> | fees + slippage: -${fees:.2f}",
              f"Balance: $1000 → <b>${bal:.2f}</b> ({(bal / START_BAL - 1) * 100:+.2f}%)",
              f"Excel: {path} ({size_mb:.1f} MB) — sheet '{name} 1s' (6 blocks of 4 hours) + sheet 'Trades'."]
    msg = "\n".join(lines)
    print(msg.replace("<b>", "").replace("</b>", ""))
    S.telegram_send(msg)
    ok = F.send_document(path, f"{name} 1-second sheet {ist(day):%d %b} + trades (open the 'Trades' sheet)")
    print(f"[day] saved {path} ({size_mb:.1f} MB), sent: {ok}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
