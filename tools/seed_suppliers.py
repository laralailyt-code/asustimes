"""把現有 suppliers.json 同步到 Supabase suppliers 表。

使用：python tools/seed_suppliers.py [--reset]
  --reset：先 TRUNCATE 舊資料再匯入（避免重複）

suppliers.json 的 part_category 是頓號（、）分隔的字串，這裡會切成陣列。
未來 user 提供更詳細名單時，可改寫此腳本支援其他來源（CSV/Excel）。
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from telegram_bot import db


def parse_categories(raw: str) -> list[str]:
    """把 'BATTERY、CABLE、CONN' 切成 ['BATTERY','CABLE','CONN']。
    支援頓號（、）、逗號、分號分隔。"""
    if not raw:
        return []
    sep = None
    for c in ("、", "，", ",", ";"):
        if c in raw:
            sep = c
            break
    parts = [p.strip().upper() for p in raw.split(sep)] if sep else [raw.strip().upper()]
    return [p for p in parts if p]


def parse_region(raw: str) -> tuple[str, str | None, str | None]:
    """'台灣/台中' → ('台灣/台中', '台灣', '台中')
       '中國大陸'   → ('中國大陸', '中國', None)
       '日本'      → ('日本', '日本', None)
    """
    if "/" in raw:
        country, city = raw.split("/", 1)
        return raw, country.strip(), city.strip()
    return raw, raw.strip(), None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--reset", action="store_true", help="先清空再匯入")
    args = parser.parse_args()

    src = ROOT / "suppliers.json"
    if not src.exists():
        print(f"[ERROR] {src} 不存在")
        sys.exit(1)

    data = json.loads(src.read_text(encoding="utf-8"))
    print(f"Loaded {len(data)} entries from suppliers.json")

    db.init_pool()

    if args.reset:
        print("Resetting suppliers table...")
        db.truncate_suppliers()

    inserted = 0
    for entry in data:
        region_full, country, city = parse_region(entry.get("region", ""))
        cats = parse_categories(entry.get("part_category", ""))
        db.upsert_supplier(
            name=None,
            region=region_full,
            country=country,
            city=city,
            lat=entry.get("lat"),
            lng=entry.get("lng"),
            part_categories=cats,
        )
        inserted += 1
        print(f"  + [{region_full}] cats={len(cats)} ({cats[:3]}...)")

    print(f"\n[OK] Inserted {inserted} suppliers")
    print(f"Distinct regions:        {len(db.list_distinct_regions())}")
    print(f"Distinct part categories: {len(db.list_distinct_part_categories())}")
    print(f"Sample regions:           {db.list_distinct_regions()[:8]}")
    print(f"Sample part categories:   {db.list_distinct_part_categories()[:8]}")

    db.close_pool()


if __name__ == "__main__":
    main()
