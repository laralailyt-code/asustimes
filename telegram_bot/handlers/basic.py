"""M1 基本指令：/start /help /list /clear

DB I/O 都是同步函式，用 asyncio.to_thread() 包進 async handler。
"""
from __future__ import annotations

import asyncio
import json
import logging

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, ReplyKeyboardMarkup,
)
from telegram.constants import ParseMode
from telegram.ext import (
    ContextTypes, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters,
)

from telegram_bot import db

logger = logging.getLogger(__name__)


WELCOME = (
    "👋 *歡迎使用 ASUSTIMES 供應鏈風險推播*\n\n"
    "我會在偵測到災害、地緣政治、罷工、供應中斷時，把「跟你訂閱條件相關」的事件推給你。\n\n"
    "👇 *直接點下方鍵盤的按鈕* 就能用，不用記指令"
)


# 常駐 Reply Keyboard（顯示在手機鍵盤上方，永遠都在）
def _main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("🔔 新增訂閱"), KeyboardButton("📋 我的訂閱")],
            [KeyboardButton("❓ 說明"),     KeyboardButton("🗑️ 全部清除")],
        ],
        resize_keyboard=True,    # 按鈕大小自適應
        is_persistent=True,      # 永遠顯示
        one_time_keyboard=False,
    )

HELP = (
    "📖 *ASUSTIMES Bot 使用說明*\n\n"
    "*訂閱類型*\n"
    "🌍 地區 — 國家/省份/城市，例如「日本」、「華東」\n"
    "📦 料件類別 — BATTERY/IC/MEMORY/DISPLAY 等\n"
    "🏭 特定供應商分布 — 從現有清單選\n"
    "📍 半徑 — 以指定座標為圓心 N 公里內\n\n"
    "*快速指令（熟手）*\n"
    "`/subscribe_region <名稱>`\n"
    "`/subscribe_part <類別>`\n"
    "`/subscribe_supplier <關鍵字>`\n"
    "`/subscribe_radius <緯度> <經度> <公里>`\n\n"
    "*推播訊息*\n"
    "🔴 高風險 / 🟠 中風險 / 🟡 低風險\n"
    "每筆訂閱可設定最低風險門檻\n"
    "同一事件對同一人只推一次\n"
    "可在訊息上點「靜音 24h」避免洗版"
)


# ─────────────────────────────── /start ──────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return

    # 註冊 / 更新使用者
    await asyncio.to_thread(
        db.upsert_user,
        chat_id=chat.id,
        username=user.username,
        first_name=user.first_name,
        language_code=user.language_code,
    )
    logger.info(f"/start from chat_id={chat.id} username={user.username}")

    await update.message.reply_text(
        WELCOME,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_main_keyboard(),
    )


# ─────────────────────────────── /help ───────────────────────────────────

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        HELP,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_main_keyboard(),
    )


# ─────────────────────────────── /list ───────────────────────────────────

def _format_sub_value(sub: dict) -> str:
    """把 subscriptions.value JSON 轉成人話描述。"""
    t = sub["type"]
    v = sub["value"] if isinstance(sub["value"], dict) else json.loads(sub["value"])
    if t == "region":
        return f"🌍 地區：{v.get('region', '?')}"
    if t == "part":
        return f"📦 料件：{v.get('part_category', '?')}"
    if t == "supplier":
        sup_id = v.get("supplier_id")
        if sup_id is None:
            return "🏭 供應商分布（unknown）"
        try:
            sup = db.get_supplier_by_id(int(sup_id))
        except Exception:
            sup = None
        if not sup:
            return f"🏭 供應商分布 #{sup_id}（已不存在）"
        region = sup.get("region", "?")
        cats = sup.get("part_categories") or []
        # 預覽前 3 個料件類別
        cats_preview = "/".join(cats[:3])
        if len(cats) > 3:
            cats_preview += f"…+{len(cats) - 3}"
        return (
            f"🏭 供應商 #{sup_id}：{region}"
            + (f"\n     料件：{cats_preview}" if cats_preview else "")
        )
    if t == "radius":
        label = v.get("label") or f"({v.get('lat')}, {v.get('lng')})"
        return f"📍 半徑：{label} {v.get('km', '?')}km"
    return f"❔ {t}: {v}"


def _format_severity(s: str) -> str:
    return {"low": "🟡 低", "medium": "🟠 中", "high": "🔴 高"}.get(s, s)


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if not chat:
        return

    user = await asyncio.to_thread(db.get_user_by_chat_id, chat.id)
    if not user:
        await update.message.reply_text("還沒註冊喔，先用 /start")
        return

    subs = await asyncio.to_thread(db.list_subscriptions, user["id"])
    if not subs:
        await update.message.reply_text(
            "📭 *還沒有任何訂閱*\n\n用 /subscribe 開始建立第一筆訂閱規則。",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    lines = ["📋 *目前訂閱規則*\n"]
    for s in subs:
        muted = ""
        if s.get("muted_until"):
            muted = "  🔕"
        lines.append(
            f"`#{s['id']}` {_format_sub_value(s)}\n"
            f"     門檻：{_format_severity(s['min_severity'])}{muted}"
        )
    lines.append("\n_用 `/unsubscribe <編號>` 取消_")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


# ─────────────────────────── /unsubscribe ────────────────────────────────

async def cmd_unsubscribe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if not chat:
        return

    args = context.args
    if not args:
        await update.message.reply_text(
            "用法：`/unsubscribe <編號>`\n例：`/unsubscribe 3`\n（編號用 /list 查）",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    try:
        sub_id = int(args[0])
    except ValueError:
        await update.message.reply_text("編號要是數字喔")
        return

    user = await asyncio.to_thread(db.get_user_by_chat_id, chat.id)
    if not user:
        await update.message.reply_text("還沒註冊喔，先用 /start")
        return

    ok = await asyncio.to_thread(db.delete_subscription, user["id"], sub_id)
    if ok:
        await update.message.reply_text(f"✅ 已刪除訂閱 #{sub_id}")
    else:
        await update.message.reply_text(f"⚠️ 找不到訂閱 #{sub_id}（或不是你的）")


# ─────────────────────────────── /clear ──────────────────────────────────

async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    if not chat:
        return

    # 二次確認
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ 確認清除", callback_data="clear:yes"),
        InlineKeyboardButton("❌ 取消", callback_data="clear:no"),
    ]])
    await update.message.reply_text(
        "⚠️ 確定要清除所有訂閱嗎？此操作無法復原。",
        reply_markup=keyboard,
    )


async def callback_clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()
    chat_id = query.message.chat.id

    if query.data == "clear:no":
        await query.edit_message_text("已取消，沒動到任何訂閱。")
        return

    user = await asyncio.to_thread(db.get_user_by_chat_id, chat_id)
    if not user:
        await query.edit_message_text("還沒註冊喔，先用 /start")
        return

    n = await asyncio.to_thread(db.clear_subscriptions, user["id"])
    await query.edit_message_text(f"🗑️ 已清除 {n} 筆訂閱規則。")


# ───────────────────────── handler 註冊 helper ───────────────────────────

def register(app):
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("unsubscribe", cmd_unsubscribe))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(CallbackQueryHandler(callback_clear, pattern=r"^clear:"))

    # 常駐鍵盤按鈕：把文字訊息當指令觸發
    # （subscribe_wizard 會自己接「🔔 新增訂閱」進精靈入口，這裡只接非 wizard 的）
    app.add_handler(MessageHandler(filters.Regex(r"^📋 我的訂閱$"),  cmd_list))
    app.add_handler(MessageHandler(filters.Regex(r"^❓ 說明$"),      cmd_help))
    app.add_handler(MessageHandler(filters.Regex(r"^🗑️ 全部清除$"), cmd_clear))
