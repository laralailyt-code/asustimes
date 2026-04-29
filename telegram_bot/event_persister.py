"""把現有風險事件落地到 Supabase risk_events 表，標記為「未推播」。

設計原則：
- 不阻塞現有風險評分主流程（背景執行緒呼叫，失敗只 log）
- 用穩定 event ID 去重（IF NOT EXISTS by id）
- 既有事件結構：{id, type, title, lat, lng, impact, region, time, supply, source, sourceUrl}
- 新事件偵測：只有 INSERT 成功的才算「新」，會回傳 list[event_id]，給 notifier 用
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Iterable

from telegram_bot import db

logger = logging.getLogger(__name__)


def _stable_event_id(event: dict) -> str:
    """產生穩定 ID。
    優先用既有 id；沒有的話 hash(type + title + lat + lng + date)。"""
    if event.get("id"):
        return str(event["id"])
    raw = f"{event.get('type','')}|{event.get('title','')}|{event.get('lat','')}|{event.get('lng','')}|{event.get('time','')[:10]}"
    return f"hash-{hashlib.md5(raw.encode('utf-8')).hexdigest()[:16]}"


def _parse_time(value) -> datetime | None:
    """把 'time' 欄位（可能是 ISO 或 YYYY-MM-DD）轉成 timestamptz。"""
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str):
        return None
    s = value.strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s[:len(fmt)], fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def persist_events(events: Iterable[dict]) -> list[str]:
    """把 events 寫入 risk_events 表，回傳「新插入的 event_id 清單」（去重用）。

    若同 id 已存在 → 不更新（避免重複推播）。
    """
    new_ids: list[str] = []
    if not events:
        return new_ids

    try:
        with db.get_cursor() as cur:
            for ev in events:
                if not ev:
                    continue
                eid = _stable_event_id(ev)
                cur.execute(
                    """
                    INSERT INTO risk_events
                        (id, type, title, lat, lng, impact, region,
                         occurred_at, supply_note, source, source_url, raw_data)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, %s::jsonb)
                    ON CONFLICT (id) DO NOTHING
                    """,
                    (
                        eid,
                        ev.get("type"),
                        ev.get("title"),
                        ev.get("lat"),
                        ev.get("lng"),
                        ev.get("impact"),
                        ev.get("region"),
                        _parse_time(ev.get("time")),
                        ev.get("supply"),
                        ev.get("source"),
                        ev.get("sourceUrl"),
                        json.dumps(ev, default=str, ensure_ascii=False),
                    ),
                )
                if cur.rowcount > 0:
                    new_ids.append(eid)
    except Exception as e:
        # 不阻塞主流程
        logger.error(f"[persister] DB write failed: {e}", exc_info=True)
        return []

    if new_ids:
        logger.info(f"[persister] {len(new_ids)} new events landed (total submitted: {sum(1 for _ in events)})")
    return new_ids


def fetch_pending_events(limit: int = 100) -> list[dict]:
    """notifier 用：撈尚未推播的事件。"""
    try:
        with db.get_cursor() as cur:
            cur.execute(
                """
                SELECT * FROM risk_events
                WHERE notified = FALSE
                ORDER BY created_at ASC
                LIMIT %s
                """,
                (limit,),
            )
            return [dict(r) for r in cur.fetchall()]
    except Exception as e:
        logger.error(f"[persister] fetch_pending failed: {e}")
        return []


def mark_notified(event_ids: Iterable[str]) -> None:
    ids = list(event_ids)
    if not ids:
        return
    try:
        with db.get_cursor() as cur:
            cur.execute(
                "UPDATE risk_events SET notified = TRUE WHERE id = ANY(%s)",
                (ids,),
            )
    except Exception as e:
        logger.error(f"[persister] mark_notified failed: {e}")


def fetch_event_by_id(event_id: str) -> dict | None:
    try:
        with db.get_cursor() as cur:
            cur.execute("SELECT * FROM risk_events WHERE id = %s", (event_id,))
            row = cur.fetchone()
            return dict(row) if row else None
    except Exception as e:
        logger.error(f"[persister] fetch_event_by_id failed: {e}")
        return None
