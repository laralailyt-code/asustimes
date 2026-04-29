"""訂閱命中邏輯。

對每筆「未推播」事件，找出所有命中的 (user_id, subscription_id) 組合。
回傳 list[(user_id, subscription_id, hit_reason)]，notifier 用來組訊息。

命中規則（subscription.type）：
1. region   — 訂閱地區與事件地區「子字串雙向比對」
                例：訂「台灣」→ 命中事件「台灣/新竹」、「台灣」
                    訂「台灣/新竹」→ 命中事件「台灣/新竹」、「台灣」
2. part     — 事件影響區域內，是否存在 supplier 含此 part_category
3. supplier — 事件影響區域內，是否包含此 supplier 的 region
4. radius   — Haversine 距離 ≤ km

嚴重等級門檻：subscription.min_severity ≤ event.impact 才算命中。
"""
from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Any

from telegram_bot import db

logger = logging.getLogger(__name__)


# 嚴重度排序（cluster_risk 用 CRITICAL/HIGH/MED/LOW；訂閱用 high/medium/low）
SEVERITY_RANK = {
    "low": 1, "medium": 2, "high": 3,
    "LOW": 1, "MED": 2, "MEDIUM": 2, "HIGH": 3, "CRITICAL": 4,
}

# ── 推播新鮮度窗口 ────────────────────────────────────────────────
# 地震：發生後 30 分鐘內才有意義（過了就只是新聞）
# 其他事件（罷工、地緣政治、操作異常等）：當天 24 小時內
EARTHQUAKE_WINDOW = timedelta(minutes=30)
DEFAULT_FRESHNESS_WINDOW = timedelta(hours=24)

EARTHQUAKE_KEYWORDS = (
    "地震", "earthquake", "震度", "magnitude", "震源", "震中", "規模",
    "M5", "M6", "M7", "M8", "M9",  # 一般用語常見
    "seismic",
)


def _is_earthquake(event: dict) -> bool:
    """是不是地震事件。從 type 與 title/keywords 雙重判斷。"""
    typ = (event.get("type") or "").lower()
    title = (event.get("title") or "").lower()
    if typ in ("earthquake", "quake"):
        return True
    if typ == "disaster" and any(kw.lower() in title for kw in EARTHQUAKE_KEYWORDS):
        return True
    return False


def _coerce_dt(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str):
        return None
    s = value.strip()
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(s[:len(fmt)], fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
    return None


def _event_freshness_passes(event: dict) -> tuple[bool, str]:
    """事件夠不夠新鮮？回傳 (是否通過, 描述)。
    - 地震：發生時間（occurred_at）30 分鐘內
    - 其他：發生時間 24 小時內
    - 沒有發生時間：用 created_at（事件入庫時間）
    """
    is_quake = _is_earthquake(event)
    window = EARTHQUAKE_WINDOW if is_quake else DEFAULT_FRESHNESS_WINDOW

    # 優先用 occurred_at（事件實際發生時間），fallback 到 created_at
    occurred = _coerce_dt(event.get("occurred_at"))
    fallback_created = _coerce_dt(event.get("created_at"))
    ts = occurred or fallback_created
    if ts is None:
        # 沒有任何時間資訊 → 保守起見「不過期」（避免漏推）
        return True, "no-time-info"

    age = datetime.now(timezone.utc) - ts
    label = "earthquake" if is_quake else "default"
    if age <= window:
        return True, f"{label} fresh ({age})"
    return False, f"{label} stale ({age} > {window})"


def _severity_passes(event_impact: str | None, sub_min: str) -> bool:
    """事件嚴重度是否達到訂閱門檻。"""
    e = SEVERITY_RANK.get((event_impact or "MED").upper(), 2)
    s = SEVERITY_RANK.get(sub_min.lower(), 1)
    return e >= s


def _haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """兩點間球面距離（公里）。"""
    R = 6371.0
    lat1r, lat2r = math.radians(lat1), math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1r) * math.cos(lat2r) * math.sin(dlng / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def _region_matches(sub_region: str, event_region: str | None) -> bool:
    """雙向子字串：訂「台灣」吃到事件「台灣/新竹」；訂「台灣/新竹」也吃到事件「台灣」。"""
    if not event_region:
        return False
    s = sub_region.strip()
    e = event_region.strip()
    return s in e or e in s


def _suppliers_in_region(event_region: str | None) -> list[dict]:
    """找事件地區內的所有 supplier（用 region 雙向 LIKE）。"""
    if not event_region:
        return []
    pat_like = f"%{event_region}%"
    pat_rev = event_region
    with db.get_cursor() as cur:
        cur.execute(
            """
            SELECT * FROM suppliers
            WHERE region ILIKE %s OR %s ILIKE '%%' || region || '%%'
            """,
            (pat_like, pat_rev),
        )
        return [dict(r) for r in cur.fetchall()]


def _is_subscription_muted(sub: dict) -> bool:
    mu = sub.get("muted_until")
    if not mu:
        return False
    if isinstance(mu, str):
        try:
            mu = datetime.fromisoformat(mu)
        except ValueError:
            return False
    if mu.tzinfo is None:
        mu = mu.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) < mu


def _value_dict(sub: dict) -> dict:
    v = sub.get("value")
    if isinstance(v, dict):
        return v
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:
            return {}
    return {}


def find_hits(event: dict) -> list[dict]:
    """對單一事件找命中的訂閱。

    回傳：[{user_id, chat_id, subscription_id, sub_type, sub_value, reason, ...}]
    其中 reason 是給推播訊息顯示用的「為什麼收到這則」描述。
    """
    hits: list[dict] = []
    if not event:
        return hits

    # 新鮮度檢查（地震 30 分鐘內、其他 24 小時內）
    fresh_ok, fresh_reason = _event_freshness_passes(event)
    if not fresh_ok:
        logger.info(f"[matcher] event={event.get('id','?')} skipped — {fresh_reason}")
        return hits

    event_region = event.get("region")
    event_lat = event.get("lat")
    event_lng = event.get("lng")
    event_impact = event.get("impact")

    # 取得所有 active 訂閱 + 對應使用者
    with db.get_cursor() as cur:
        cur.execute(
            """
            SELECT s.id AS sub_id, s.user_id, s.type, s.value,
                   s.min_severity, s.muted_until,
                   u.chat_id, u.is_active
            FROM subscriptions s
            JOIN telegram_users u ON s.user_id = u.id
            WHERE s.is_active = TRUE AND u.is_active = TRUE
            """
        )
        all_subs = [dict(r) for r in cur.fetchall()]

    if not all_subs:
        return hits

    # 預先撈一次「事件地區內的供應商」，避免每筆訂閱都查一次
    region_suppliers: list[dict] | None = None
    def _get_region_suppliers() -> list[dict]:
        nonlocal region_suppliers
        if region_suppliers is None:
            region_suppliers = _suppliers_in_region(event_region)
        return region_suppliers

    for sub in all_subs:
        if _is_subscription_muted(sub):
            continue
        if not _severity_passes(event_impact, sub["min_severity"]):
            continue

        v = _value_dict(sub)
        sub_type = sub["type"]
        reason = None

        if sub_type == "region":
            sub_region = v.get("region", "")
            if _region_matches(sub_region, event_region):
                reason = f"地區訂閱：{sub_region}"

        elif sub_type == "part":
            cat = (v.get("part_category") or "").upper()
            if cat:
                for sup in _get_region_suppliers():
                    sup_cats = sup.get("part_categories") or []
                    if cat in [c.upper() for c in sup_cats]:
                        reason = f"料件訂閱：{cat}（事件地區供應商生產此料件）"
                        break

        elif sub_type == "supplier":
            sup_id = v.get("supplier_id")
            if sup_id is not None:
                # 直接撈該供應商，比對其 region 與事件 region
                sup = db.get_supplier_by_id(int(sup_id))
                if sup and _region_matches(sup.get("region") or "", event_region):
                    reason = f"供應商訂閱：#{sup_id} {sup.get('region')}"

        elif sub_type == "radius":
            try:
                slat = float(v.get("lat")); slng = float(v.get("lng"))
                km = float(v.get("km", 0))
                if event_lat is not None and event_lng is not None and km > 0:
                    dist = _haversine_km(slat, slng, float(event_lat), float(event_lng))
                    if dist <= km:
                        reason = f"半徑訂閱：圓心 ({slat:.2f},{slng:.2f}) 內 {dist:.1f}/{km:g}km"
            except (TypeError, ValueError):
                pass

        if reason:
            hits.append({
                "user_id":         sub["user_id"],
                "chat_id":         sub["chat_id"],
                "subscription_id": sub["sub_id"],
                "sub_type":        sub_type,
                "sub_value":       v,
                "min_severity":    sub["min_severity"],
                "reason":          reason,
            })

    if hits:
        logger.info(f"[matcher] event={event.get('id','?')} → {len(hits)} hits")
    return hits


def already_notified(user_id: int, event_id: str) -> bool:
    """同事件對同人去重檢查。"""
    with db.get_cursor() as cur:
        cur.execute(
            "SELECT 1 FROM notification_log WHERE user_id = %s AND event_id = %s",
            (user_id, event_id),
        )
        return cur.fetchone() is not None
