"""One-time backfill of April 2026 commodity history into CSV.

Sources:
- Yahoo Finance v8 chart API (direct HTTP, no yfinance lib needed) — copper, aluminum,
  oil (WTI/Brent), gold, silver, JPY, TWD, CNY
- Trading Economics current price (no public history API) — cobalt, tin, nickel, zinc, lithium
- sci99 JSON API — yellow phosphorus, PC

Strategy:
- Yahoo: pull 60-day daily close per ticker, write each weekday into CSV
- TE/sci99: pull what we can, write the latest known value to today and any explicit dated points

CSV format: wide, item rows × date columns (YYYY/M/D), preserves existing data.
"""
import csv
import re
import time
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

CSV_PATH = Path(__file__).parent / "2026 Raw material trend history.csv"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0",
    "Accept": "application/json, text/csv, */*",
    "Accept-Language": "en-US,en;q=0.9",
}

# Yahoo Finance v8 ticker → (csv_item_name, multiplier_to_target_unit)
YAHOO_MAP = {
    "HG=F":  ("銅 (copper) US$/tonne",       2204.62),        # USD/lb → USD/tonne
    "ALI=F": ("鋁 (aluminum) US$/tonne",     1.0),            # USD/tonne
    "CL=F":  ("石油 西德州 ( US$/桶)",        1.0),            # USD/barrel
    "BZ=F":  ("石油 北海布蘭特 (US$/桶)",     1.0),            # USD/barrel
    "GC=F":  ("金 (gold) US$/盎司",          1.0),            # USD/oz
    "SI=F":  ("銀 (silver) US$/盎司",        1.0),            # USD/oz
    "JPY=X": ("匯率 / 日幣",                  1.0),            # USD/JPY
    "TWD=X": ("匯率 / 台幣",                  1.0),            # USD/TWD
    "CNY=X": ("匯率 / 人民幣",                1.0),            # USD/CNY
}

# Trading Economics slug → (csv_item_name, multiplier)
TE_SLUGS = {
    "cobalt":  ("鈷 (cobalt) US$/tonne",     1.0),
    "tin":     ("錫 (tin) US$/tonne",        1.0),
    "nickel":  ("鎳 (nickel)  US$/tonne",    1.0),
    "zinc":    ("鋅 (zinc)  US$/tonne",      1.0),
    "lithium": ("鋰 (Lithium) CNY$/tonne",   1.0),
}

# sci99 oldId → csv_item_name
SCI99_MAP = {
    678: "黃磷 CNY$/tonne",
    68:  "PC塑料 (SABIC) CNY$/tonne",
}

START_DATE = date(2026, 4, 1)
END_DATE = date(2026, 4, 29)


def fetch_yahoo(symbol: str, days: int = 60) -> list[tuple[date, float]]:
    """Yahoo Finance v8 chart API. Returns [(date, close_price), ...]."""
    end = int(time.time())
    start = end - days * 86400
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        f"?period1={start}&period2={end}&interval=1d"
    )
    r = requests.get(url, headers=HEADERS, timeout=15)
    if r.status_code != 200:
        print(f"  Yahoo {symbol}: HTTP {r.status_code}")
        return []
    body = r.json()
    result = body.get("chart", {}).get("result", [None])[0]
    if not result:
        return []
    timestamps = result.get("timestamp", [])
    closes = result.get("indicators", {}).get("quote", [{}])[0].get("close", [])
    out = []
    for ts, close in zip(timestamps, closes):
        if close is None:
            continue
        d = date.fromtimestamp(ts)
        if START_DATE <= d <= END_DATE:
            out.append((d, float(close)))
    return out


def fetch_te(slug: str) -> float | None:
    """Trading Economics current 'last' price."""
    try:
        r = requests.get(
            f"https://tradingeconomics.com/commodity/{slug}",
            headers=HEADERS,
            timeout=15,
        )
        m = re.search(r'"last":"?([\d.]+)', r.text)
        return float(m.group(1)) if m else None
    except Exception as e:
        print(f"  TE {slug}: {e}")
        return None


def fetch_sci99(old_id: int) -> list[tuple[date, float]]:
    """sci99 JSON API, returns up to 7 recent days."""
    try:
        r = requests.get(
            "https://www.sci99.com/priceMonitor/listProductPagePrice",
            params={"oldId": old_id, "type": 0},
            headers={
                "User-Agent": HEADERS["User-Agent"],
                "Referer": f"https://www.sci99.com/monitor-{old_id}-0.html",
            },
            timeout=15,
        )
        body = r.json()
        if body.get("code") != 200 or not body.get("data"):
            return []
        out = []
        for row in body["data"]:
            d_str = row.get("dateRange")
            v_str = row.get("mdataValue")
            if not d_str or not v_str:
                continue
            try:
                d = date.fromisoformat(d_str)
                v = float(v_str.replace(",", ""))
                if START_DATE <= d <= END_DATE:
                    out.append((d, v))
            except Exception:
                pass
        return out
    except Exception as e:
        print(f"  sci99 {old_id}: {e}")
        return []


def date_to_key(d: date) -> str:
    """Convert date to CSV header format YYYY/M/D (no leading zeros)."""
    return f"{d.year}/{d.month}/{d.day}"


def main():
    if not CSV_PATH.exists():
        print(f"CSV not found: {CSV_PATH}")
        sys.exit(1)

    # Read CSV
    with open(CSV_PATH, encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        rows = list(reader)

    if not rows:
        print("CSV is empty")
        sys.exit(1)

    header = rows[0]
    item_to_row = {row[0]: i for i, row in enumerate(rows[1:], start=1)}
    print(f"CSV loaded: {len(rows) - 1} commodities, {len(header) - 1} date columns")

    # Build set of existing date keys for quick lookup
    existing_dates = set(header[1:])

    # Collect all backfill data: dict[item_name] = dict[date] = price
    backfill = {}

    # === Yahoo Finance ===
    print("\n[Yahoo Finance]")
    for sym, (item_name, mult) in YAHOO_MAP.items():
        time.sleep(2)  # avoid 429
        points = fetch_yahoo(sym)
        if not points:
            print(f"  {sym} → {item_name}: 0 points")
            continue
        backfill.setdefault(item_name, {})
        for d, raw in points:
            backfill[item_name][d] = round(raw * mult, 4)
        print(f"  {sym} → {item_name}: {len(points)} points")

    # === Trading Economics (current value only) ===
    print("\n[Trading Economics] (current value only — no public history)")
    today = date.today()
    for slug, (item_name, mult) in TE_SLUGS.items():
        time.sleep(1)
        price = fetch_te(slug)
        if price is None:
            print(f"  {slug}: failed")
            continue
        # TE copper-style fix: cobalt/tin/nickel/zinc/lithium are USD/tonne directly,
        # but be defensive — if value is suspiciously low, assume USD/lb
        val = price * mult
        backfill.setdefault(item_name, {})[today] = round(val, 2)
        print(f"  {slug} → {item_name}: {today} = {val}")

    # === sci99 JSON ===
    print("\n[sci99]")
    for old_id, item_name in SCI99_MAP.items():
        time.sleep(1)
        points = fetch_sci99(old_id)
        if not points:
            print(f"  oldId={old_id} → {item_name}: 0 points")
            continue
        backfill.setdefault(item_name, {})
        for d, v in points:
            backfill[item_name][d] = round(v, 2)
        print(f"  oldId={old_id} → {item_name}: {len(points)} points")

    # === Apply backfill to CSV rows ===
    print("\n[Applying to CSV]")
    # First, ensure all needed date columns exist in header
    needed_dates = set()
    for item_data in backfill.values():
        for d in item_data:
            needed_dates.add(date_to_key(d))

    new_dates = sorted(needed_dates - existing_dates,
                       key=lambda s: datetime.strptime(s, "%Y/%m/%d"))
    if new_dates:
        print(f"  Adding {len(new_dates)} new date columns: {new_dates}")
        # Insert new dates in sorted position
        # Easier approach: rebuild with combined sorted dates
        all_dates = sorted(existing_dates | needed_dates,
                           key=lambda s: datetime.strptime(s, "%Y/%m/%d"))
        new_header = ["項目"] + all_dates
        # Build new rows
        new_rows = [new_header]
        for row in rows[1:]:
            item = row[0]
            old_data = dict(zip(header[1:], row[1:]))
            new_row = [item]
            for d_key in all_dates:
                new_row.append(old_data.get(d_key, ""))
            new_rows.append(new_row)
        rows = new_rows
        header = new_header
        item_to_row = {row[0]: i for i, row in enumerate(rows[1:], start=1)}

    # Add missing rows for items not in CSV (oil, gold, silver, FX rates)
    for item_name in backfill:
        if item_name not in item_to_row:
            new_row = [item_name] + [""] * (len(header) - 1)
            rows.append(new_row)
            item_to_row[item_name] = len(rows) - 1
            print(f"  [ADD] new row for: {item_name}")

    # Now write backfill values, but ONLY where current cell is empty
    written = 0
    skipped = 0
    for item_name, dates_map in backfill.items():
        if item_name not in item_to_row:
            print(f"  [WARN] {item_name} not found in CSV - skipping")
            continue
        ridx = item_to_row[item_name]
        for d, val in dates_map.items():
            d_key = date_to_key(d)
            try:
                cidx = header.index(d_key)
            except ValueError:
                continue
            current = rows[ridx][cidx] if cidx < len(rows[ridx]) else ""
            if current and current.strip() not in ("", "0"):
                skipped += 1
                continue
            # Pad row if needed
            while len(rows[ridx]) <= cidx:
                rows[ridx].append("")
            # Format: int if whole number, else 2 decimals
            if val == int(val):
                rows[ridx][cidx] = str(int(val))
            else:
                rows[ridx][cidx] = str(round(val, 2))
            written += 1

    print(f"\n  Wrote {written} cells, skipped {skipped} (already had value)")

    # Write back
    with open(CSV_PATH, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerows(rows)
    print(f"\n[DONE] CSV updated: {CSV_PATH}")


if __name__ == "__main__":
    main()
