"""One-time carry-forward + bounded carry-back applied to existing CSV.
- Carry-forward: any empty cell ← most recent prior real value (tagged '*')
- Carry-back:    leading empty cells ← first real value, BUT only for dates
  within the last CARRY_BACK_DAYS days (avoid polluting 10+ years of history).
- All '*' tags are stripped first so reruns produce a clean state.

After this, _save_commodity_csv() in app.py will keep applying the same logic
on every save, so blanks won't reappear (until cache is cleared).
"""
import csv
import sys
from pathlib import Path
from datetime import datetime, timedelta

CSV_PATH = Path(__file__).parent / "2026 Raw material trend history.csv"
CARRY_BACK_DAYS = 30


def main():
    if not CSV_PATH.exists():
        print(f"CSV not found: {CSV_PATH}")
        sys.exit(1)

    with open(CSV_PATH, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))

    if len(rows) < 2:
        print("CSV empty")
        return

    header = rows[0]
    n_cols = len(header)

    # Sort date columns chronologically (CSV may have dates out of order)
    # Header[0] is "項目", rest are dates "YYYY/M/D"
    date_cols = []  # list of (header_str, original_index)
    for i, h in enumerate(header[1:], start=1):
        try:
            d = datetime.strptime(h, "%Y/%m/%d")
            date_cols.append((d, h, i))
        except ValueError:
            pass

    date_cols.sort()  # chronological order
    sorted_indices = [i for _, _, i in date_cols]

    print(f"CSV: {len(rows)-1} commodities, {len(date_cols)} date columns")

    # Build new header in chronological order
    new_header = ["項目"] + [h for _, h, _ in date_cols]
    new_rows = [new_header]

    cutoff = datetime.now().date() - timedelta(days=CARRY_BACK_DAYS)
    # Pre-parse sorted dates
    sorted_date_objs = [d for d, _, _ in date_cols]

    written_fwd = 0
    written_back = 0
    for ridx in range(1, len(rows)):
        old_row = rows[ridx]
        while len(old_row) < n_cols:
            old_row.append("")
        new_row = [old_row[0]] + [old_row[i] for i in sorted_indices]

        # Strip existing '*' tags to allow clean re-application
        for cidx in range(1, len(new_row)):
            cell = new_row[cidx].strip() if new_row[cidx] else ""
            if cell.endswith("*"):
                new_row[cidx] = ""

        # Pass 1: carry-forward (no time limit)
        last_real = None
        for cidx in range(1, len(new_row)):
            cell = new_row[cidx].strip() if new_row[cidx] else ""
            if cell and cell != "0":
                last_real = cell
                continue
            if last_real is not None:
                new_row[cidx] = f"{last_real}*"
                written_fwd += 1

        # Pass 2: carry-back leading empties — only for dates within cutoff
        first_real = None
        for cidx in range(1, len(new_row)):
            cell = new_row[cidx].strip() if new_row[cidx] else ""
            if cell and cell != "0" and not cell.endswith("*"):
                first_real = cell
                break
        if first_real is not None:
            for cidx in range(1, len(new_row)):
                cell = new_row[cidx].strip() if new_row[cidx] else ""
                if cell and cell != "0":
                    break
                d = sorted_date_objs[cidx - 1].date() if cidx - 1 < len(sorted_date_objs) else None
                if d is None or d < cutoff:
                    continue
                new_row[cidx] = f"{first_real}*"
                written_back += 1

        new_rows.append(new_row)
    written = written_fwd + written_back

    print(f"Carry-forward: {written_fwd} cells | Carry-back (within {CARRY_BACK_DAYS}d): {written_back} cells")

    # Save back
    with open(CSV_PATH, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerows(new_rows)

    print(f"[DONE] CSV updated: {CSV_PATH}")


if __name__ == "__main__":
    main()
