"""Fix copper multiplier error in CSV.

backfill_april.py used multiplier 22.05 (treating Yahoo HG=F as ¢/lb), but
HG=F is USD/lb. Correct multiplier is 2204.62 (USD/lb → USD/tonne), so existing
copper cells need to be multiplied by 100.

Apply only to cells that are in the wrong magnitude range (< 1000), to avoid
double-multiplying cells that may already be correct.
"""
import csv
from pathlib import Path

CSV_PATH = Path(__file__).parent / "2026 Raw material trend history.csv"


def main():
    with open(CSV_PATH, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))

    header = rows[0]
    fixed = 0
    for ridx, row in enumerate(rows[1:], start=1):
        if row[0] != "銅 (copper) US$/tonne":
            continue
        for cidx in range(1, len(row)):
            cell = row[cidx].strip() if row[cidx] else ""
            if not cell:
                continue
            # Detect carry-forward marker
            star = cell.endswith("*")
            num_str = cell[:-1] if star else cell
            try:
                v = float(num_str)
            except ValueError:
                continue
            # Only fix obviously-wrong values (< 1000 = decimal-place error)
            if v < 1000:
                v_fixed = v * 100
                row[cidx] = (str(int(v_fixed)) if v_fixed == int(v_fixed)
                             else str(round(v_fixed, 2)))
                if star:
                    row[cidx] += "*"
                fixed += 1

    print(f"Fixed {fixed} copper cells (×100)")
    with open(CSV_PATH, "w", encoding="utf-8-sig", newline="") as f:
        csv.writer(f).writerows(rows)


if __name__ == "__main__":
    main()
