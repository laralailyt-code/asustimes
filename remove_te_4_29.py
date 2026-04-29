"""Remove 4/29 TE-sourced LME metal values (basis mismatch with Excel settlement).

TE quotes are bid prices on LME Trading; the Excel data uses settlement prices.
The two differ by ~1% which causes a fake jump on 4/29 in the chart.

This script clears the 4/29 cell for these commodities so carry-forward will
sync 4/29 to 4/28 (last real settlement).
"""
import csv
from pathlib import Path

CSV_PATH = Path(__file__).parent / "2026 Raw material trend history.csv"

TARGETS = [
    "鈷 (cobalt) US$/tonne",
    "錫 (tin) US$/tonne",
    "鎳 (nickel)  US$/tonne",
    "鋅 (zinc)  US$/tonne",
    "鋰 (Lithium) CNY$/tonne",
]


def main():
    with open(CSV_PATH, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
    header = rows[0]

    try:
        col_429 = header.index("2026/4/29")
    except ValueError:
        print("4/29 column not found")
        return

    cleared = 0
    for row in rows[1:]:
        if not row or row[0] not in TARGETS:
            continue
        old = row[col_429] if col_429 < len(row) else ""
        if old:
            print(f"  Cleared {row[0]}: 4/29 was {old}")
            row[col_429] = ""
            cleared += 1

    print(f"Cleared {cleared} cells")
    with open(CSV_PATH, "w", encoding="utf-8-sig", newline="") as f:
        csv.writer(f).writerows(rows)


if __name__ == "__main__":
    main()
