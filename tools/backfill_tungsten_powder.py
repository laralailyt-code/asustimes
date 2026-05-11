from __future__ import annotations

import csv
import shutil
from bisect import bisect_right
from datetime import date
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MAIN_CSV = ROOT / "2026 Raw material trend history.csv"
BACKUP_CSV = ROOT / "2026 Raw material trend history.csv.before-tungsten-detail-backfill.bak"
POINTS_CSV = ROOT / "data" / "tungsten_powder_price_points.csv"
DAILY_CSV = ROOT / "data" / "tungsten_powder_daily_backfill.csv"

TUNGSTEN_ROW_NAME = "\u93a2"
BACKFILL_START = date(2024, 1, 1)
KEEP_EXISTING_FROM = date(2026, 3, 25)


# Prices are normalized to CNY/kg. Articles often quote CNY/kg directly;
# "wan CNY/tonne" values are divided by 1,000 (e.g. 33.8 wan/t = 338 CNY/kg).
PRICE_POINTS = [
    ("2024-01-04", 273, "https://www.ctia.net.cn/news/tungsten/1711.html", "earliest public 2024-01 quote found; carried back to 2024-01-01"),
    ("2024-01-15", 273, "https://www.ctia.com.cn/news/102715.html", ""),
    ("2024-01-29", 273, "https://www.ctia.com.cn/news/103109.html", ""),
    ("2024-02-18", 275, "https://ctia.net.cn/news/tungsten/1720.html", ""),
    ("2024-02-26", 276, "https://www.ctia.com.cn/news/103651.html", ""),
    ("2024-03-04", 278, "https://www.ctia.com.cn/news/103871.html", ""),
    ("2024-03-07", 279, "https://www.ctia.com.cn/news/104056.html", ""),
    ("2024-03-11", 280, "https://www.ctia.com.cn/news/104081.html", ""),
    ("2024-03-18", 280, "https://www.ctia.com.cn/news/104311.html", ""),
    ("2024-03-27", 281, "https://www.ctia.com.cn/news/104630.html", ""),
    ("2024-03-28", 281, "https://www.ctia.com.cn/news/104728.html", ""),
    ("2024-04-02", 283, "https://www.ctia.com.cn/news/104846.html", ""),
    ("2024-04-03", 283, "https://www.ctia.com.cn/news/104904.html", ""),
    ("2024-04-08", 285, "https://www.ctia.com.cn/news/105059.html", ""),
    ("2024-04-10", 286, "https://www.ctia.com.cn/news/105135.html", ""),
    ("2024-04-24", 300, "https://www.ctia.com.cn/news/105490.html", "30 wan CNY/tonne normalized"),
    ("2024-05-06", 311, "https://www.ctia.com.cn/news/105684.html", ""),
    ("2024-05-15", 338, "https://www.ctia.com.cn/news/106056.html", "33.8 wan CNY/tonne normalized"),
    ("2024-05-28", 340, "https://www.ctia.net.cn/news/tungsten/1793.html", "34 wan CNY/tonne normalized"),
    ("2024-05-29", 340, "https://www.ctia.com.cn/news/106456.html", "34 wan CNY/tonne normalized"),
    ("2024-06-03", 340, "https://www.ctia.com.cn/news/106585.html", "34 wan CNY/tonne normalized"),
    ("2024-06-05", 339, "https://www.ctia.com.cn/news/106677.html", "33.9 wan CNY/tonne normalized"),
    ("2024-06-12", 338, "https://www.ctia.com.cn/news/106879.html", "33.8 wan CNY/tonne normalized"),
    ("2024-06-19", 333, "https://www.ctia.com.cn/news/107045.html", ""),
    ("2024-06-27", 326, "https://www.ctia.com.cn/news/107382.html", "article unit typo normalized as 32.6 wan CNY/tonne"),
    ("2024-07-01", 323, "https://www.ctia.com.cn/news/107461.html", "32.3 wan CNY/tonne normalized"),
    ("2024-07-04", 317, "https://www.ctia.com.cn/news/107629.html", "31.7 wan CNY/tonne normalized"),
    ("2024-07-10", 312, "https://www.ctia.com.cn/news/107782.html", "31.2 wan CNY/tonne normalized"),
    ("2024-07-23", 304, "https://www.ctia.com.cn/news/108180.html", ""),
    ("2024-08-21", 307, "https://www.ctia.com.cn/news/109457.html", ""),
    ("2024-09-11", 311, "https://www.ctia.com.cn/news/110441.html", ""),
    ("2024-10-15", 311, "https://www.ctia.com.cn/news/111380.html", ""),
    ("2024-10-21", 311, "https://www.ctia.com.cn/news/111580.html", ""),
    ("2024-10-25", 311, "https://www.ctia.com.cn/news/111763.html", ""),
    ("2024-11-01", 312, "https://www.ctia.com.cn/news/112053.html", ""),
    ("2024-11-11", 315, "https://www.ctia.com.cn/news/112326.html", ""),
    ("2024-11-12", 316, "https://www.ctia.com.cn/news/112366.html", ""),
    ("2024-11-14", 316, "https://www.ctia.com.cn/news/112443.html", ""),
    ("2024-11-15", 316, "https://www.ctia.com.cn/news/112515.html", ""),
    ("2024-11-20", 317, "https://www.ctia.com.cn/news/112666.html", ""),
    ("2024-11-25", 319, "https://www.ctia.com.cn/news/112795.html", ""),
    ("2024-11-27", 319, "https://www.ctia.com.cn/news/112853.html", ""),
    ("2024-12-02", 318, "https://www.ctia.com.cn/news/113076.html", ""),
    ("2024-12-20", 316, "https://ctia.net.cn/news/tungsten/1853.html", ""),
    ("2025-01-14", 318, "https://www.ctia.com.cn/news/114553.html", ""),
    ("2025-01-15", 318, "https://www.ctia.com.cn/news/114576.html", ""),
    ("2025-01-17", 318, "https://www.ctia.com.cn/news/114652.html", ""),
    ("2025-01-20", 318, "https://www.ctia.com.cn/news/114703.html", ""),
    ("2025-01-23", 320, "https://www.ctia.com.cn/news/114853.html", ""),
    ("2025-01-24", 320, "https://www.ctia.com.cn/news/114865.html", ""),
    ("2025-02-07", 320, "https://www.ctia.com.cn/news/115049.html", ""),
    ("2025-02-20", 318, "https://www.ctia.com.cn/news/115508.html", ""),
    ("2025-02-27", 317, "https://www.ctia.com.cn/news/115755.html", ""),
    ("2025-03-05", 315, "https://www.ctia.com.cn/news/115992.html", ""),
    ("2025-03-18", 312, "https://www.ctia.com.cn/news/116404.html", ""),
    ("2025-03-27", 313, "https://www.ctia.com.cn/news/116820.html", ""),
    ("2025-04-02", 315, "https://www.ctia.com.cn/news/116958.html", ""),
    ("2025-04-17", 319, "https://www.ctia.com.cn/news/117597.html", ""),
    ("2025-04-27", 326, "https://www.ctia.com.cn/news/117909.html", ""),
    ("2025-05-15", 348, "https://www.ctia.com.cn/news/118494.html", ""),
    ("2025-05-26", 360, "https://www.ctia.com.cn/news/118742.html", ""),
    ("2025-05-29", 366, "https://www.ctia.com.cn/news/118933.html", ""),
    ("2025-06-03", 370, "https://www.ctia.com.cn/news/119034.html", ""),
    ("2025-06-04", 372, "https://www.ctia.com.cn/news/119111.html", ""),
    ("2025-06-09", 378, "https://www.ctia.com.cn/news/119384.html", ""),
    ("2025-07-09", 381, "https://www.ctia.com.cn/news/120466.html", ""),
    ("2025-07-30", 430, "https://www.ctia.com.cn/news/121075.html", ""),
    ("2025-08-12", 438, "https://www.ctia.com.cn/news/121540.html", ""),
    ("2025-08-14", 445, "https://www.ctia.com.cn/news/121790.html", ""),
    ("2025-08-21", 485, "https://www.ctia.com.cn/news/121880.html", ""),
    ("2025-08-28", 570, "https://www.ctia.com.cn/news/122055.html", ""),
    ("2025-09-02", 615, "https://www.ctia.com.cn/news/122132.html", ""),
    ("2025-09-08", 640, "https://www.ctia.com.cn/news/122313.html", ""),
    ("2025-09-23", 620, "https://www.ctia.com.cn/news/122886.html", "backsolved from high pullback and year-to-date gain context"),
    ("2025-10-11", 625, "https://www.ctia.com.cn/news/123189.html", ""),
    ("2025-10-30", 650, "https://www.ctia.com.cn/news/123829.html", ""),
    ("2025-11-19", 770, "https://www.ctia.com.cn/news/124623.html", ""),
    ("2025-11-27", 790, "https://www.ctia.com.cn/news/124798.html", ""),
    ("2025-12-08", 840, "https://www.ctia.com.cn/news/125069.html", ""),
    ("2025-12-23", 1080, "https://www.ctia.com.cn/news/125660.html", ""),
    ("2025-12-29", 1080, "https://www.ctia.com.cn/news/125784.html", ""),
    ("2026-01-06", 1100, "https://www.ctia.com.cn/news/126055.html", ""),
    ("2026-01-09", 1150, "https://www.ctia.com.cn/news/126232.html", "115 wan CNY/tonne normalized"),
    ("2026-01-13", 1170, "https://www.ctia.com.cn/news/126311.html", ""),
    ("2026-01-14", 1190, "https://www.ctia.com.cn/news/126330.html", ""),
    ("2026-01-16", 1210, "https://www.ctia.com.cn/news/126563.html", ""),
    ("2026-01-21", 1240, "https://www.ctia.com.cn/news/126863.html", ""),
    ("2026-01-23", 1270, "https://www.ctia.com.cn/news/126887.html", ""),
    ("2026-01-30", 1480, "https://www.ctia.com.cn/news/127143.html", "148 wan CNY/tonne normalized"),
    ("2026-02-10", 1700, "https://www.ctia.com.cn/news/127360.html", ""),
    ("2026-02-27", 1880, "https://www.ctia.com.cn/news/127673.html", ""),
    ("2026-03-09", 2210, "https://www.ctia.com.cn/news/127998.html", ""),
    ("2026-03-12", 2400, "https://www.ctia.com.cn/news/128118.html", ""),
]


def parse_date(value: str) -> date:
    year, month, day = value.replace("-", "/").split("/")
    return date(int(year), int(month), int(day))


def date_to_header(value: date) -> str:
    return f"{value.year}/{value.month}/{value.day}"


def value_to_text(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:.2f}".rstrip("0").rstrip(".")


def load_main_csv() -> list[list[str]]:
    with MAIN_CSV.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.reader(handle))


def write_csv(path: Path, rows: list[list[str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerows(rows)


def find_or_create_tungsten_row(rows: list[list[str]]) -> list[str]:
    width = len(rows[0])
    for row in rows[1:]:
        if row and row[0].strip() == TUNGSTEN_ROW_NAME:
            row.extend([""] * (width - len(row)))
            return row
    row = [TUNGSTEN_ROW_NAME] + [""] * (width - 1)
    rows.append(row)
    return row


def main() -> None:
    if not BACKUP_CSV.exists():
        shutil.copy2(MAIN_CSV, BACKUP_CSV)

    rows = load_main_csv()
    header = rows[0]
    tungsten_row = find_or_create_tungsten_row(rows)

    points = [(parse_date(d), float(price), url, note) for d, price, url, note in PRICE_POINTS]
    points.sort(key=lambda item: item[0])
    point_dates = [item[0] for item in points]

    changed = 0
    filled = 0
    daily_rows = [["date", "price_cny_per_kg", "source_date", "source_url", "method", "note"]]

    for idx, raw_header in enumerate(header[1:], start=1):
        if not raw_header.strip():
            continue
        try:
            current_date = parse_date(raw_header)
        except Exception:
            continue
        if current_date < BACKFILL_START or current_date >= KEEP_EXISTING_FROM:
            continue

        point_index = bisect_right(point_dates, current_date) - 1
        if point_index < 0:
            point_index = 0
        source_date, price, source_url, note = points[point_index]
        if current_date < point_dates[0]:
            source_date, price, source_url, note = points[0]

        value = value_to_text(price)
        method = "reported" if current_date == source_date else "carry_forward"
        if not tungsten_row[idx].strip():
            tungsten_row[idx] = value
            changed += 1
        filled += 1
        daily_rows.append([
            date_to_header(current_date),
            value,
            date_to_header(source_date),
            source_url,
            method,
            note,
        ])

    point_rows = [["date", "price_cny_per_kg", "source_url", "note"]]
    point_rows.extend([[date_to_header(d), value_to_text(price), url, note] for d, price, url, note in points])

    write_csv(MAIN_CSV, rows)
    write_csv(POINTS_CSV, point_rows)
    write_csv(DAILY_CSV, daily_rows)

    print(f"backup={BACKUP_CSV}")
    print(f"points={len(point_rows) - 1} -> {POINTS_CSV}")
    print(f"daily_rows={len(daily_rows) - 1} -> {DAILY_CSV}")
    print(f"eligible_dates={filled}; changed_blank_cells={changed}")


if __name__ == "__main__":
    main()
