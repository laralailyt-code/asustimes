"""PostgreSQL（Supabase）連線池與 CRUD 函式。

設計：
- 用 psycopg2.pool.ThreadedConnectionPool（執行緒安全，跟現有 Flask threading 風格一致）
- 所有函式同步（sync）— PTB 的 handler 是 async，但 DB I/O 走執行緒池
  在 async handler 裡呼叫時請用 `asyncio.to_thread(func, ...)` 包起來
- 連線字串從環境變數 SUPABASE_DB_URL 讀
"""
from __future__ import annotations

import json
import logging
import os
import threading
from contextlib import contextmanager
from typing import Any

import psycopg2
import psycopg2.extras
import psycopg2.pool

logger = logging.getLogger(__name__)

_pool: psycopg2.pool.ThreadedConnectionPool | None = None
_pool_lock = threading.Lock()


def init_pool(min_conn: int = 1, max_conn: int = 5) -> None:
    """啟動時呼叫。建立連線池。"""
    global _pool
    with _pool_lock:
        if _pool is not None:
            return
        url = os.environ.get("SUPABASE_DB_URL")
        if not url:
            raise RuntimeError("SUPABASE_DB_URL 未設定")
        _pool = psycopg2.pool.ThreadedConnectionPool(min_conn, max_conn, url)
        logger.info(f"[telegram_bot.db] Pool created (min={min_conn}, max={max_conn})")


def close_pool() -> None:
    global _pool
    with _pool_lock:
        if _pool is not None:
            _pool.closeall()
            _pool = None


@contextmanager
def get_conn():
    """從池取連線，使用完自動還。"""
    if _pool is None:
        init_pool()
    conn = _pool.getconn()
    try:
        yield conn
        if not conn.closed:
            conn.commit()
    except Exception:
        if not conn.closed:
            conn.rollback()
        raise
    finally:
        _pool.putconn(conn)


@contextmanager
def get_cursor(dict_cursor: bool = True):
    """快捷：直接拿 cursor。"""
    with get_conn() as conn:
        factory = psycopg2.extras.RealDictCursor if dict_cursor else None
        with conn.cursor(cursor_factory=factory) as cur:
            yield cur


# ───────────────────────────── Users ──────────────────────────────

def upsert_user(chat_id: int, username: str | None, first_name: str | None,
                language_code: str | None) -> dict:
    """新使用者註冊或更新既有使用者資料。回傳 user row（含 id）。"""
    with get_cursor() as cur:
        cur.execute(
            """
            INSERT INTO telegram_users (chat_id, username, first_name, language_code, is_active)
            VALUES (%s, %s, %s, %s, TRUE)
            ON CONFLICT (chat_id) DO UPDATE SET
                username      = EXCLUDED.username,
                first_name    = EXCLUDED.first_name,
                language_code = EXCLUDED.language_code,
                is_active     = TRUE,
                blocked_at    = NULL,
                updated_at    = NOW()
            RETURNING *
            """,
            (chat_id, username, first_name, language_code),
        )
        return dict(cur.fetchone())


def get_user_by_chat_id(chat_id: int) -> dict | None:
    with get_cursor() as cur:
        cur.execute("SELECT * FROM telegram_users WHERE chat_id = %s", (chat_id,))
        row = cur.fetchone()
        return dict(row) if row else None


def deactivate_user(chat_id: int, reason: str = "blocked") -> None:
    """使用者封鎖 bot 時呼叫。"""
    with get_cursor() as cur:
        cur.execute(
            """
            UPDATE telegram_users
            SET is_active = FALSE, blocked_at = NOW()
            WHERE chat_id = %s
            """,
            (chat_id,),
        )
        # 同時把該使用者的訂閱也停用
        cur.execute(
            """
            UPDATE subscriptions s
            SET is_active = FALSE
            FROM telegram_users u
            WHERE s.user_id = u.id AND u.chat_id = %s
            """,
            (chat_id,),
        )
    logger.info(f"User {chat_id} deactivated ({reason})")


# ──────────────────────── Subscriptions ───────────────────────────

def list_subscriptions(user_id: int, only_active: bool = True) -> list[dict]:
    with get_cursor() as cur:
        sql = "SELECT * FROM subscriptions WHERE user_id = %s"
        if only_active:
            sql += " AND is_active = TRUE"
        sql += " ORDER BY id"
        cur.execute(sql, (user_id,))
        return [dict(r) for r in cur.fetchall()]


def add_subscription(user_id: int, sub_type: str, value: dict,
                     min_severity: str = "low") -> dict:
    if sub_type not in {"region", "part", "supplier", "radius"}:
        raise ValueError(f"Invalid subscription type: {sub_type}")
    if min_severity not in {"low", "medium", "high"}:
        raise ValueError(f"Invalid severity: {min_severity}")
    with get_cursor() as cur:
        cur.execute(
            """
            INSERT INTO subscriptions (user_id, type, value, min_severity)
            VALUES (%s, %s, %s::jsonb, %s)
            RETURNING *
            """,
            (user_id, sub_type, json.dumps(value), min_severity),
        )
        return dict(cur.fetchone())


def delete_subscription(user_id: int, sub_id: int) -> bool:
    """安全刪除：檢查 user_id 確實擁有這筆訂閱才能刪。"""
    with get_cursor() as cur:
        cur.execute(
            "DELETE FROM subscriptions WHERE id = %s AND user_id = %s",
            (sub_id, user_id),
        )
        return cur.rowcount > 0


def clear_subscriptions(user_id: int) -> int:
    with get_cursor() as cur:
        cur.execute("DELETE FROM subscriptions WHERE user_id = %s", (user_id,))
        return cur.rowcount


def mute_subscription(sub_id: int, hours: int = 24) -> None:
    """Inline 按鈕「靜音 N 小時」。"""
    with get_cursor() as cur:
        cur.execute(
            """
            UPDATE subscriptions
            SET muted_until = NOW() + (%s || ' hours')::interval
            WHERE id = %s
            """,
            (hours, sub_id),
        )


# ───────────────────────── Suppliers ──────────────────────────────

def upsert_supplier(name: str | None, region: str, country: str | None,
                    city: str | None, lat: float | None, lng: float | None,
                    part_categories: list[str]) -> dict:
    with get_cursor() as cur:
        cur.execute(
            """
            INSERT INTO suppliers (name, region, country, city, lat, lng, part_categories)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING *
            """,
            (name, region, country, city, lat, lng, part_categories),
        )
        return dict(cur.fetchone())


def list_suppliers(region: str | None = None,
                   part_category: str | None = None) -> list[dict]:
    with get_cursor() as cur:
        sql = "SELECT * FROM suppliers WHERE 1=1"
        params: list[Any] = []
        if region:
            sql += " AND region = %s"
            params.append(region)
        if part_category:
            sql += " AND %s = ANY(part_categories)"
            params.append(part_category)
        sql += " ORDER BY id"
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]


def list_distinct_regions() -> list[str]:
    with get_cursor() as cur:
        cur.execute("SELECT DISTINCT region FROM suppliers ORDER BY region")
        return [r["region"] for r in cur.fetchall()]


def list_distinct_part_categories() -> list[str]:
    with get_cursor() as cur:
        cur.execute("""
            SELECT DISTINCT unnest(part_categories) AS cat
            FROM suppliers
            ORDER BY cat
        """)
        return [r["cat"] for r in cur.fetchall()]


def truncate_suppliers() -> None:
    """⚠️ 重新匯入用。會清掉 supplier_parts。"""
    with get_cursor() as cur:
        cur.execute("TRUNCATE suppliers RESTART IDENTITY CASCADE")


def list_distinct_countries() -> list[str]:
    """訂閱精靈第二步用 — 列出所有 country。"""
    with get_cursor() as cur:
        cur.execute("""
            SELECT DISTINCT country FROM suppliers
            WHERE country IS NOT NULL
            ORDER BY country
        """)
        return [r["country"] for r in cur.fetchall()]


def list_cities_by_country(country: str) -> list[str]:
    with get_cursor() as cur:
        cur.execute("""
            SELECT DISTINCT city FROM suppliers
            WHERE country = %s AND city IS NOT NULL
            ORDER BY city
        """, (country,))
        return [r["city"] for r in cur.fetchall()]


def search_suppliers(keyword: str, limit: int = 20) -> list[dict]:
    """快速指令 /subscribe_supplier <關鍵字> 用。
    沒 name 時用 region 和 city 模糊比對。"""
    pattern = f"%{keyword}%"
    with get_cursor() as cur:
        cur.execute("""
            SELECT * FROM suppliers
            WHERE COALESCE(name,'')   ILIKE %s
               OR COALESCE(region,'') ILIKE %s
               OR COALESCE(city,'')   ILIKE %s
               OR COALESCE(country,'') ILIKE %s
            ORDER BY id
            LIMIT %s
        """, (pattern, pattern, pattern, pattern, limit))
        return [dict(r) for r in cur.fetchall()]


def get_supplier_by_id(supplier_id: int) -> dict | None:
    with get_cursor() as cur:
        cur.execute("SELECT * FROM suppliers WHERE id = %s", (supplier_id,))
        row = cur.fetchone()
        return dict(row) if row else None
