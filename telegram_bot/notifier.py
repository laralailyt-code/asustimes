"""推播工作器 — 把命中的事件丟到 Telegram。

功能：
- 全域限速（預設 25 訊息/秒，留 buffer 給 30/s 的 Telegram 全域上限）
- 推播失敗處理：
  * 403 Forbidden（使用者封鎖 bot）→ 自動停用該使用者所有訂閱
  * 429 Too Many Requests → 退避重試
  * 其他錯誤 → log 後跳過
- 同事件對同人只推一次（用 notification_log unique constraint 保護）
- 訊息含 Inline 按鈕：📍 詳情 / ✅ 已知悉 / 🔕 靜音 24h
- sendLocation 附事件座標
- 高風險響鈴；低風險 disable_notification

呼叫方式：
- async dispatch_pending() — 由 background dispatcher 定期跑
- async push_event_to_users(event, hits) — 給單一事件直接推（simulate 用）
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

from telegram import (
    Bot, InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions,
)
from telegram.constants import ParseMode
from telegram.error import (
    BadRequest, Forbidden, NetworkError, RetryAfter, TelegramError,
)

from telegram_bot import db, event_persister, matcher

logger = logging.getLogger(__name__)


SEV_DISPLAY = {
    "CRITICAL": ("🔴", "極高風險"),
    "HIGH":     ("🔴", "高風險"),
    "MED":      ("🟠", "中風險"),
    "MEDIUM":   ("🟠", "中風險"),
    "LOW":      ("🟡", "低風險"),
}


def _format_event_message(event: dict, hit: dict) -> str:
    """組推播訊息（Markdown）。"""
    impact = (event.get("impact") or "MED").upper()
    emoji, sev_label = SEV_DISPLAY.get(impact, ("🟡", "低風險"))
    title = event.get("title") or "(無標題)"
    region = event.get("region") or "—"
    occurred = event.get("occurred_at") or event.get("time") or "—"
    if hasattr(occurred, "strftime"):
        occurred = occurred.strftime("%Y-%m-%d %H:%M UTC")
    elif isinstance(occurred, str) and len(occurred) > 16:
        occurred = occurred[:16].replace("T", " ")

    supply = event.get("supply_note") or event.get("supply") or ""
    source = event.get("source") or ""
    src_url = event.get("source_url") or event.get("sourceUrl") or ""
    typ = event.get("type") or ""

    type_label = {
        "disaster":     "🌪 災害",
        "geopolitical": "⚔️ 地緣政治",
        "war":          "⚔️ 戰爭/衝突",
        "strike":       "✊ 罷工",
        "operational":  "⚡ 操作異常",
    }.get(typ, typ)

    lines = [
        f"{emoji} *{sev_label}*  ·  {type_label}",
        f"*{_escape_md(title)}*",
        "",
        f"📍 地區：{_escape_md(region)}",
        f"🕐 時間：{occurred}",
    ]
    if supply:
        lines.append(f"📦 影響：{_escape_md(supply[:200])}")
    if source:
        if src_url:
            lines.append(f"🔗 來源：[{_escape_md(source)}]({src_url})")
        else:
            lines.append(f"🔗 來源：{_escape_md(source)}")
    lines.append("")
    lines.append(f"_📨 命中規則：{_escape_md(hit['reason'])}_")
    return "\n".join(lines)


def _escape_md(s: str | None) -> str:
    """簡易 Markdown v1 跳脫（不徹底，但避免破版）。"""
    if not s:
        return ""
    return s.replace("*", "·").replace("_", " ").replace("[", "(").replace("]", ")").replace("`", "'")


def _build_keyboard(event_id: str, sub_id: int, src_url: str = "") -> InlineKeyboardMarkup:
    btns: list[list[InlineKeyboardButton]] = []
    row1 = []
    if src_url:
        row1.append(InlineKeyboardButton("📰 詳情", url=src_url))
    public_url = os.environ.get("TELEGRAM_PUBLIC_URL", "")
    if public_url:
        row1.append(InlineKeyboardButton("🌐 開啟風險地圖", url=public_url + "/#risk"))
    if row1:
        btns.append(row1)
    btns.append([
        InlineKeyboardButton("✅ 已知悉",       callback_data=f"notif:ack:{event_id}:{sub_id}"),
        InlineKeyboardButton("🔕 靜音此規則 24h", callback_data=f"notif:mute:{sub_id}"),
    ])
    return InlineKeyboardMarkup(btns)


async def _send_one(bot: Bot, hit: dict, event: dict, rate_lock: asyncio.Semaphore) -> tuple[str, str | None]:
    """送一則。回傳 (status, error_message)。
    status: 'sent' | 'failed' | 'blocked' | 'retry_later'
    """
    chat_id = hit["chat_id"]
    user_id = hit["user_id"]
    sub_id = hit["subscription_id"]
    event_id = event["id"]
    impact = (event.get("impact") or "MED").upper()
    disable_notif = impact in ("LOW",)

    text = _format_event_message(event, hit)
    keyboard = _build_keyboard(event_id, sub_id, event.get("source_url") or event.get("sourceUrl") or "")

    async with rate_lock:
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=keyboard,
                disable_notification=disable_notif,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
            # 附座標（若有）
            try:
                lat = event.get("lat"); lng = event.get("lng")
                if lat is not None and lng is not None:
                    await bot.send_location(
                        chat_id=chat_id, latitude=float(lat), longitude=float(lng),
                        disable_notification=True,
                    )
            except Exception as loc_err:
                logger.debug(f"send_location failed (non-fatal): {loc_err}")
            return "sent", None

        except Forbidden:
            # 使用者封鎖 bot
            await asyncio.to_thread(db.deactivate_user, chat_id, "bot blocked by user")
            logger.warning(f"[notifier] user {chat_id} blocked bot, deactivated")
            return "blocked", "Forbidden"

        except RetryAfter as e:
            wait = float(e.retry_after) + 0.5
            logger.warning(f"[notifier] rate-limited, sleeping {wait}s")
            await asyncio.sleep(wait)
            return "retry_later", f"RetryAfter {wait}s"

        except BadRequest as e:
            logger.error(f"[notifier] BadRequest chat={chat_id}: {e}")
            return "failed", f"BadRequest: {e}"

        except (NetworkError, TelegramError) as e:
            logger.error(f"[notifier] TelegramError chat={chat_id}: {e}")
            return "failed", f"{type(e).__name__}: {e}"


def _log_send(user_id: int, event_id: str, sub_id: int, status: str, err: str | None) -> None:
    try:
        with db.get_cursor() as cur:
            cur.execute(
                """
                INSERT INTO notification_log
                    (user_id, event_id, subscription_id, status, error_message)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (user_id, event_id) DO NOTHING
                """,
                (user_id, event_id, sub_id, status, err),
            )
    except Exception as e:
        logger.error(f"[notifier] log_send failed: {e}")


async def push_event_to_users(bot: Bot, event: dict, rate_per_sec: int = 25) -> dict:
    """把單一事件推給所有命中的訂閱。
    回傳統計：{matched, sent, failed, blocked, skipped_dup}
    """
    hits = await asyncio.to_thread(matcher.find_hits, event)
    stats = {"matched": len(hits), "sent": 0, "failed": 0, "blocked": 0, "skipped_dup": 0}

    if not hits:
        return stats

    # 簡易限速：每秒 N 個 token
    rate_lock = asyncio.Semaphore(rate_per_sec)
    interval = 1.0 / max(rate_per_sec, 1)

    # 同事件對同人去重（user_id 第一筆 hit 為主，避免一個事件觸發多筆規則 → 多訊息）
    seen_users: set[int] = set()
    for hit in hits:
        if hit["user_id"] in seen_users:
            stats["skipped_dup"] += 1
            continue
        if await asyncio.to_thread(matcher.already_notified, hit["user_id"], event["id"]):
            stats["skipped_dup"] += 1
            seen_users.add(hit["user_id"])
            continue

        status, err = await _send_one(bot, hit, event, rate_lock)
        if status == "retry_later":
            # 重試一次
            status, err = await _send_one(bot, hit, event, rate_lock)
        await asyncio.to_thread(
            _log_send, hit["user_id"], event["id"], hit["subscription_id"], status, err,
        )

        if status == "sent":
            stats["sent"] += 1
            seen_users.add(hit["user_id"])
        elif status == "blocked":
            stats["blocked"] += 1
            seen_users.add(hit["user_id"])
        else:
            stats["failed"] += 1

        await asyncio.sleep(interval)

    return stats


async def dispatch_pending(bot: Bot, batch: int = 50, rate_per_sec: int = 25) -> dict:
    """掃描所有 notified=false 的事件，依序推播。"""
    events = await asyncio.to_thread(event_persister.fetch_pending_events, batch)
    total = {"events": len(events), "matched": 0, "sent": 0, "failed": 0, "blocked": 0, "skipped_dup": 0}
    if not events:
        return total

    notified_ids: list[str] = []
    for ev in events:
        try:
            stat = await push_event_to_users(bot, ev, rate_per_sec=rate_per_sec)
            for k in ("matched", "sent", "failed", "blocked", "skipped_dup"):
                total[k] += stat[k]
        except Exception as e:
            logger.error(f"[notifier] event {ev.get('id')} dispatch failed: {e}", exc_info=True)
        notified_ids.append(ev["id"])

    await asyncio.to_thread(event_persister.mark_notified, notified_ids)
    logger.info(f"[notifier] dispatch done: {total}")
    return total


# ─── Inline 按鈕 callback ────────────────────────────────────────

async def callback_ack(update, context) -> None:
    """『✅ 已知悉』— 把訊息上的 reply_markup 拿掉，加個確認文字。"""
    query = update.callback_query
    await query.answer("已標記為知悉")
    parts = (query.data or "").split(":")
    # notif:ack:<event_id>:<sub_id>
    try:
        await query.edit_message_reply_markup(reply_markup=None)
        await query.edit_message_text(
            (query.message.text_markdown or query.message.text or "") + "\n\n_✅ 已標記為知悉_",
            parse_mode=ParseMode.MARKDOWN,
        )
    except Exception as e:
        logger.debug(f"ack edit failed (non-fatal): {e}")


async def callback_mute(update, context) -> None:
    """『🔕 靜音此規則 24h』。"""
    query = update.callback_query
    parts = (query.data or "").split(":")
    if len(parts) >= 3:
        try:
            sub_id = int(parts[2])
            await asyncio.to_thread(db.mute_subscription, sub_id, 24)
            await query.answer("已靜音此規則 24 小時")
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass
            return
        except ValueError:
            pass
    await query.answer("操作失敗")


def register(app):
    from telegram.ext import CallbackQueryHandler
    app.add_handler(CallbackQueryHandler(callback_ack,  pattern=r"^notif:ack:"))
    app.add_handler(CallbackQueryHandler(callback_mute, pattern=r"^notif:mute:"))
