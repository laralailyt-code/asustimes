"""執行 telegram_bot/migrations.sql 到 Supabase。

使用：python tools/run_migrations.py

設計成可重複執行（IDEMPOTENT）— 所有 CREATE 都用 IF NOT EXISTS。
"""
import os
import sys
from pathlib import Path

# Allow running from project root
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

import psycopg2

DB_URL = os.environ.get("SUPABASE_DB_URL")
SQL_FILE = ROOT / "telegram_bot" / "migrations.sql"


def main():
    if not DB_URL:
        print("[ERROR] SUPABASE_DB_URL 未設定 (.env)")
        sys.exit(1)
    if not SQL_FILE.exists():
        print(f"[ERROR] SQL 檔不存在：{SQL_FILE}")
        sys.exit(1)

    sql = SQL_FILE.read_text(encoding="utf-8")
    print(f"Loaded {len(sql)} bytes from {SQL_FILE.name}")

    print("Connecting to Supabase...")
    conn = psycopg2.connect(DB_URL)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
        print("[OK] Migrations applied")

        # 驗證表是否存在
        with conn.cursor() as cur:
            cur.execute("""
                SELECT tablename FROM pg_tables
                WHERE schemaname = 'public'
                ORDER BY tablename
            """)
            tables = [r[0] for r in cur.fetchall()]
            print(f"\nTables in public schema ({len(tables)}):")
            for t in tables:
                print(f"  - {t}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
