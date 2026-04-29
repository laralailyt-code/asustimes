"""One-shot: backfill the cobalt CSV row with daily LME cash-settle data
from cnyes (鉅亨網 lcocs).

Why: until 2026-04-29 cobalt history was a mix of user-Excel settlement
and Trading Economics bid (different basis, caused chart jumps). cnyes
provides 360 days of consistent LME daily settlement, free and updated
nightly. After running this script the entire cobalt row is on cnyes
basis and the daily refresh extends it cleanly.

Idempotent: safe to re-run; just overwrites the cobalt row each time.
"""
from __future__ import annotations

import csv
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib import request as _ur

CSV_PATH = Path(__file__).parent.parent / "2026 Raw material trend history.csv"
ITEM_NAME = "鈷 (cobalt) US$/tonne"
CNYES_URL = (
    "https://www.cnyes.com/futures/highChart/ChartSource.aspx"
    "?type=futures&source=javachart&code=lcocs"
)


def fetch_cnyes() -> dict[str, float]:
    req = _ur.Request(CNYES_URL, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://www.cnyes.com/futures/Javachart/lcocs.html",
    })
    text = _ur.urlopen(req, timeout=20).read().decode("utf-8").strip()
    m = re.match(r"^\((.*?)\)\s*;?\s*$", text, re.DOTALL)
    if not m:
        raise RuntimeError(f"cnyes JSONP shape unexpected: {text[:120]}")
    raw = json.loads(m.group(1))
    out: dict[str, float] = {}
    for ts_ms, val in raw:
        dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
        out[f"{dt.year}/{dt.month}/{dt.day}"] = float(val)
    return out


def main() -> int:
    cnyes = fetch_cnyes()
    if not cnyes:
        print("cnyes returned no data", file=sys.stderr)
        return 1
    print(f"fetched {len(cnyes)} cnyes points (latest: {sorted(cnyes.keys())[-1]} = {cnyes[sorted(cnyes.keys())[-1]]:.0f})")

    with CSV_PATH.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
    header = rows[0]
    if header[0] != "項目":
        print(f"unexpected header[0]={header[0]!r}", file=sys.stderr)
        return 1

    cob_idx = next((i for i, r in enumerate(rows) if r and r[0] == ITEM_NAME), -1)
    if cob_idx < 0:
        print(f"cobalt row not found", file=sys.stderr)
        return 1

    # Rebuild cobalt row: blank everywhere except dates we have in cnyes
    new_row = [ITEM_NAME] + [""] * (len(header) - 1)
    matched = 0
    for col_idx, header_date in enumerate(header[1:], start=1):
        if header_date in cnyes:
            v = cnyes[header_date]
            new_row[col_idx] = str(int(v)) if abs(v - round(v)) < 1e-6 else f"{v:.2f}"
            matched += 1

    print(f"matched {matched} CSV columns with cnyes dates")
    rows[cob_idx] = new_row

    with CSV_PATH.open("w", encoding="utf-8-sig", newline="") as f:
        csv.writer(f).writerows(rows)
    print(f"saved {CSV_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
