"""訂閱精靈 — 多步驟 ConversationHandler。

流程：
  Step 1: SELECTING_TYPE       使用者選類型（地區/料件/供應商/半徑）
  Step 2: 依類型分支
          - 地區 → SELECTING_COUNTRY → SELECTING_CITY（可選）
          - 料件 → SELECTING_PART
          - 供應商 → SELECTING_SUPPLIER
          - 半徑 → ENTERING_RADIUS（純文字輸入）
  Step 3: SELECTING_SEVERITY   選最低風險門檻（低/中/高）
  Step 4: CONFIRMING           確認摘要 → 寫入 DB

context.user_data 暫存：
  - 'sub_type'      : 'region' | 'part' | 'supplier' | 'radius'
  - 'sub_value'     : dict（依 type 不同）
  - 'sub_severity'  : 'low' | 'medium' | 'high'
  - 'sub_label'     : 給確認頁顯示用的人話描述
"""
from __future__ import annotations

import asyncio
import json
import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    CommandHandler, CallbackQueryHandler, ConversationHandler,
    ContextTypes, MessageHandler, filters,
)

from telegram_bot import db

logger = logging.getLogger(__name__)

# ─── 狀態常數 ─────────────────────────────────────────────────────────
(
    SELECTING_TYPE,
    SELECTING_COUNTRY,
    SELECTING_CITY,
    SELECTING_PART,
    SELECTING_SUPPLIER,
    ENTERING_RADIUS,
    SELECTING_SEVERITY,
    CONFIRMING,
) = range(8)


# ─── 共用工具 ─────────────────────────────────────────────────────────

PAGE_SIZE = 8  # 每頁按鈕數量（Telegram 建議 ≤ 8）


def _kb_chunked(buttons: list[InlineKeyboardButton], cols: int = 2) -> InlineKeyboardMarkup:
    rows = [buttons[i:i + cols] for i in range(0, len(buttons), cols)]
    return InlineKeyboardMarkup(rows)


def _back_cancel_row(back_cb: str | None = None) -> list[InlineKeyboardButton]:
    row = []
    if back_cb:
        row.append(InlineKeyboardButton("⬅️ 上一步", callback_data=back_cb))
    row.append(InlineKeyboardButton("❌ 取消", callback_data="wiz:cancel"))
    return row


# ─── Step 1: /subscribe → 選類型 ─────────────────────────────────────

async def cmd_subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat = update.effective_chat
    user = update.effective_user
    if not chat or not user:
        return ConversationHandler.END

    # 確保 user 已註冊
    await asyncio.to_thread(
        db.upsert_user,
        chat_id=chat.id,
        username=user.username,
        first_name=user.first_name,
        language_code=user.language_code,
    )
    context.user_data.clear()

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🌍 地區",      callback_data="wiz:type:region"),
         InlineKeyboardButton("📦 料件類別",  callback_data="wiz:type:part")],
        [InlineKeyboardButton("🏭 供應商分布", callback_data="wiz:type:supplier"),
         InlineKeyboardButton("📍 半徑",      callback_data="wiz:type:radius")],
        [InlineKeyboardButton("❌ 取消", callback_data="wiz:cancel")],
    ])
    await update.message.reply_text(
        "🔔 *訂閱精靈 — 步驟 1/4*\n\n請選擇訂閱類型：",
        reply_markup=keyboard,
        parse_mode=ParseMode.MARKDOWN,
    )
    return SELECTING_TYPE


# ─── Step 2 (地區): 選國家 ──────────────────────────────────────────

async def select_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    sub_type = query.data.split(":")[2]
    context.user_data["sub_type"] = sub_type

    if sub_type == "region":
        return await _show_countries(update, context)
    if sub_type == "part":
        return await _show_parts(update, context)
    if sub_type == "supplier":
        return await _show_suppliers(update, context, page=0)
    if sub_type == "radius":
        return await _ask_radius(update, context)
    return ConversationHandler.END


async def _show_countries(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    countries = await asyncio.to_thread(db.list_distinct_countries)
    btns = [
        InlineKeyboardButton(c, callback_data=f"wiz:country:{c}")
        for c in countries
    ]
    kb = _kb_chunked(btns, cols=2)
    rows = list(kb.inline_keyboard)
    rows.append(_back_cancel_row(back_cb="wiz:back:type"))
    kb = InlineKeyboardMarkup(rows)
    await update.callback_query.edit_message_text(
        "🌍 *步驟 2/4 — 選擇國家*\n\n選一個國家：",
        reply_markup=kb,
        parse_mode=ParseMode.MARKDOWN,
    )
    return SELECTING_COUNTRY


async def select_country(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    country = query.data.split(":", 2)[2]
    context.user_data["country"] = country

    cities = await asyncio.to_thread(db.list_cities_by_country, country)
    if not cities:
        # 沒有細分城市 → 直接整個國家
        context.user_data["sub_value"] = {"region": country}
        context.user_data["sub_label"] = f"🌍 {country}（整個國家）"
        return await _ask_severity(update, context)

    btns = [
        InlineKeyboardButton(f"📍 {c}", callback_data=f"wiz:city:{c}")
        for c in cities
    ]
    btns.append(InlineKeyboardButton(f"✅ 整個 {country}", callback_data="wiz:city:__all__"))
    kb = _kb_chunked(btns, cols=2)
    rows = list(kb.inline_keyboard)
    rows.append(_back_cancel_row(back_cb="wiz:back:country"))
    await query.edit_message_text(
        f"🌍 *步驟 2/4 — 選擇 {country} 內的城市*\n\n"
        f"共 {len(cities)} 個城市，或選整個 {country}：",
        reply_markup=InlineKeyboardMarkup(rows),
        parse_mode=ParseMode.MARKDOWN,
    )
    return SELECTING_CITY


async def select_city(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    city = query.data.split(":", 2)[2]
    country = context.user_data.get("country", "")

    if city == "__all__":
        context.user_data["sub_value"] = {"region": country}
        context.user_data["sub_label"] = f"🌍 {country}（整個國家）"
    else:
        # 用 "country/city" 對應 suppliers.region 欄位的值
        region_value = f"{country}/{city}" if country else city
        context.user_data["sub_value"] = {"region": region_value}
        context.user_data["sub_label"] = f"🌍 {country} / {city}"
    return await _ask_severity(update, context)


# ─── Step 2 (料件): 選 category ─────────────────────────────────────

async def _show_parts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    cats = await asyncio.to_thread(db.list_distinct_part_categories)
    btns = [
        InlineKeyboardButton(c, callback_data=f"wiz:part:{c}")
        for c in cats
    ]
    kb = _kb_chunked(btns, cols=2)
    rows = list(kb.inline_keyboard)
    rows.append(_back_cancel_row(back_cb="wiz:back:type"))
    await update.callback_query.edit_message_text(
        "📦 *步驟 2/4 — 選擇料件類別*\n\n選一個料件類別：",
        reply_markup=InlineKeyboardMarkup(rows),
        parse_mode=ParseMode.MARKDOWN,
    )
    return SELECTING_PART


async def select_part(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    cat = query.data.split(":", 2)[2]
    context.user_data["sub_value"] = {"part_category": cat}
    context.user_data["sub_label"] = f"📦 料件：{cat}"
    return await _ask_severity(update, context)


# ─── Step 2 (供應商): 分頁列出 ──────────────────────────────────────

async def _show_suppliers(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int = 0) -> int:
    all_sups = await asyncio.to_thread(db.list_suppliers)
    total = len(all_sups)
    start = page * PAGE_SIZE
    end = start + PAGE_SIZE
    sups = all_sups[start:end]

    btns = []
    for s in sups:
        label = f"#{s['id']} {s['region']}"
        if s.get("part_categories"):
            cats = s["part_categories"]
            if isinstance(cats, list) and cats:
                preview = "/".join(cats[:2])
                label += f" • {preview}"
                if len(cats) > 2:
                    label += "+"
        btns.append(InlineKeyboardButton(label[:60], callback_data=f"wiz:sup:{s['id']}"))

    rows = [[b] for b in btns]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️ 上一頁", callback_data=f"wiz:sup_page:{page-1}"))
    nav.append(InlineKeyboardButton(f"{page+1}/{(total - 1)//PAGE_SIZE + 1}", callback_data="wiz:noop"))
    if end < total:
        nav.append(InlineKeyboardButton("下一頁 ▶️", callback_data=f"wiz:sup_page:{page+1}"))
    if nav:
        rows.append(nav)
    rows.append(_back_cancel_row(back_cb="wiz:back:type"))

    text = f"🏭 *步驟 2/4 — 選擇供應商分布*\n\n共 {total} 筆，第 {page+1} 頁："
    if update.callback_query:
        await update.callback_query.edit_message_text(
            text, reply_markup=InlineKeyboardMarkup(rows), parse_mode=ParseMode.MARKDOWN,
        )
    else:
        await update.message.reply_text(
            text, reply_markup=InlineKeyboardMarkup(rows), parse_mode=ParseMode.MARKDOWN,
        )
    return SELECTING_SUPPLIER


async def supplier_page(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    page = int(query.data.split(":", 2)[2])
    return await _show_suppliers(update, context, page=page)


async def select_supplier(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    sup_id = int(query.data.split(":", 2)[2])
    sup = await asyncio.to_thread(db.get_supplier_by_id, sup_id)
    if not sup:
        await query.edit_message_text("⚠️ 找不到該供應商分布，請重新開始 /subscribe")
        return ConversationHandler.END

    context.user_data["sub_value"] = {"supplier_id": sup_id}
    cats = "/".join((sup.get("part_categories") or [])[:3])
    context.user_data["sub_label"] = (
        f"🏭 #{sup_id} {sup['region']}" + (f" • {cats}" if cats else "")
    )
    return await _ask_severity(update, context)


# ─── Step 2 (半徑): 文字輸入 ────────────────────────────────────────

async def _ask_radius(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (
        "📍 *步驟 2/4 — 半徑訂閱*\n\n"
        "請輸入：`緯度 經度 半徑公里` （空格分隔）\n\n"
        "例：\n"
        "• 台北 101 50 公里圓內 → `25.034 121.564 50`\n"
        "• 東京 100 公里 → `35.68 139.76 100`\n\n"
        "或點 ❌ 取消結束。"
    )
    rows = [_back_cancel_row(back_cb="wiz:back:type")]
    await update.callback_query.edit_message_text(
        text, reply_markup=InlineKeyboardMarkup(rows), parse_mode=ParseMode.MARKDOWN,
    )
    return ENTERING_RADIUS


async def receive_radius(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.message.text or "").strip()
    parts = text.split()
    if len(parts) != 3:
        await update.message.reply_text("⚠️ 格式錯誤，請輸入 3 個數字（緯度 經度 公里），例：`25.034 121.564 50`")
        return ENTERING_RADIUS
    try:
        lat = float(parts[0])
        lng = float(parts[1])
        km = float(parts[2])
    except ValueError:
        await update.message.reply_text("⚠️ 三個值都要是數字")
        return ENTERING_RADIUS
    if not (-90 <= lat <= 90 and -180 <= lng <= 180 and 0 < km <= 5000):
        await update.message.reply_text("⚠️ 數值超出範圍（緯度 -90~90, 經度 -180~180, 半徑 0~5000km）")
        return ENTERING_RADIUS

    context.user_data["sub_value"] = {"lat": lat, "lng": lng, "km": km}
    context.user_data["sub_label"] = f"📍 ({lat:.3f}, {lng:.3f}) {km:g}km 圓內"
    return await _ask_severity(update, context)


# ─── Step 3: 風險等級 ──────────────────────────────────────────────

async def _ask_severity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🟡 低（全收）",       callback_data="wiz:sev:low"),
         InlineKeyboardButton("🟠 中（含以上）",     callback_data="wiz:sev:medium"),
         InlineKeyboardButton("🔴 高（僅高風險）",   callback_data="wiz:sev:high")],
        _back_cancel_row(back_cb="wiz:back:type"),
    ])
    text = (
        "🚦 *步驟 3/4 — 風險等級門檻*\n\n"
        f"已選：{context.user_data.get('sub_label', '?')}\n\n"
        "只在風險達到以下等級才推播："
    )
    if update.callback_query:
        await update.callback_query.edit_message_text(
            text, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN,
        )
    else:
        await update.message.reply_text(
            text, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN,
        )
    return SELECTING_SEVERITY


async def select_severity(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    sev = query.data.split(":", 2)[2]
    context.user_data["sub_severity"] = sev

    label_sev = {"low": "🟡 低", "medium": "🟠 中", "high": "🔴 高"}[sev]
    label_main = context.user_data.get("sub_label", "?")
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ 確認訂閱",   callback_data="wiz:confirm:yes"),
         InlineKeyboardButton("❌ 取消",       callback_data="wiz:cancel")],
    ])
    summary = (
        "✅ *步驟 4/4 — 確認訂閱*\n"
        "━━━━━━━━━━━━━━━\n"
        f"類型：{label_main}\n"
        f"門檻：{label_sev}（含以上）\n"
        "━━━━━━━━━━━━━━━"
    )
    await query.edit_message_text(summary, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN)
    return CONFIRMING


# ─── Step 4: 確認 → 寫入 ───────────────────────────────────────────

async def confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat.id

    user = await asyncio.to_thread(db.get_user_by_chat_id, chat_id)
    if not user:
        await query.edit_message_text("⚠️ 還沒註冊，先用 /start")
        return ConversationHandler.END

    sub = await asyncio.to_thread(
        db.add_subscription,
        user_id=user["id"],
        sub_type=context.user_data["sub_type"],
        value=context.user_data["sub_value"],
        min_severity=context.user_data["sub_severity"],
    )
    await query.edit_message_text(
        f"🎉 *訂閱成功！* 規則編號 `#{sub['id']}`\n\n"
        f"{context.user_data.get('sub_label', '')}\n\n"
        "👉 用 /list 查看所有訂閱\n"
        f"👉 `/unsubscribe {sub['id']}` 取消這筆",
        parse_mode=ParseMode.MARKDOWN,
    )
    context.user_data.clear()
    return ConversationHandler.END


# ─── 取消 / 上一步 ─────────────────────────────────────────────────

async def cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query:
        await query.answer()
        await query.edit_message_text("已取消，沒建立任何訂閱。")
    else:
        await update.message.reply_text("已取消。")
    context.user_data.clear()
    return ConversationHandler.END


async def back_to_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data.pop("sub_type", None)
    context.user_data.pop("sub_value", None)
    context.user_data.pop("sub_label", None)
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🌍 地區",      callback_data="wiz:type:region"),
         InlineKeyboardButton("📦 料件類別",  callback_data="wiz:type:part")],
        [InlineKeyboardButton("🏭 供應商分布", callback_data="wiz:type:supplier"),
         InlineKeyboardButton("📍 半徑",      callback_data="wiz:type:radius")],
        [InlineKeyboardButton("❌ 取消", callback_data="wiz:cancel")],
    ])
    await query.edit_message_text(
        "🔔 *訂閱精靈 — 步驟 1/4*\n\n請選擇訂閱類型：",
        reply_markup=keyboard,
        parse_mode=ParseMode.MARKDOWN,
    )
    return SELECTING_TYPE


async def back_to_country(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    return await _show_countries(update, context)


async def noop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """頁碼按鈕（不做事，只回 ack）。回傳當前狀態。"""
    await update.callback_query.answer()
    return SELECTING_SUPPLIER


# ─── 註冊 ConversationHandler ───────────────────────────────────────

def register(app):
    conv = ConversationHandler(
        entry_points=[
            CommandHandler("subscribe", cmd_subscribe),
            MessageHandler(filters.Regex(r"^🔔 新增訂閱$") & ~filters.COMMAND, cmd_subscribe),
        ],
        states={
            SELECTING_TYPE: [
                CallbackQueryHandler(select_type,        pattern=r"^wiz:type:"),
                CallbackQueryHandler(cancel_callback,    pattern=r"^wiz:cancel$"),
            ],
            SELECTING_COUNTRY: [
                CallbackQueryHandler(select_country,     pattern=r"^wiz:country:"),
                CallbackQueryHandler(back_to_type,       pattern=r"^wiz:back:type$"),
                CallbackQueryHandler(cancel_callback,    pattern=r"^wiz:cancel$"),
            ],
            SELECTING_CITY: [
                CallbackQueryHandler(select_city,        pattern=r"^wiz:city:"),
                CallbackQueryHandler(back_to_country,    pattern=r"^wiz:back:country$"),
                CallbackQueryHandler(cancel_callback,    pattern=r"^wiz:cancel$"),
            ],
            SELECTING_PART: [
                CallbackQueryHandler(select_part,        pattern=r"^wiz:part:"),
                CallbackQueryHandler(back_to_type,       pattern=r"^wiz:back:type$"),
                CallbackQueryHandler(cancel_callback,    pattern=r"^wiz:cancel$"),
            ],
            SELECTING_SUPPLIER: [
                CallbackQueryHandler(select_supplier,    pattern=r"^wiz:sup:"),
                CallbackQueryHandler(supplier_page,      pattern=r"^wiz:sup_page:"),
                CallbackQueryHandler(noop_callback,      pattern=r"^wiz:noop$"),
                CallbackQueryHandler(back_to_type,       pattern=r"^wiz:back:type$"),
                CallbackQueryHandler(cancel_callback,    pattern=r"^wiz:cancel$"),
            ],
            ENTERING_RADIUS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_radius),
                CallbackQueryHandler(back_to_type,       pattern=r"^wiz:back:type$"),
                CallbackQueryHandler(cancel_callback,    pattern=r"^wiz:cancel$"),
            ],
            SELECTING_SEVERITY: [
                CallbackQueryHandler(select_severity,    pattern=r"^wiz:sev:"),
                CallbackQueryHandler(back_to_type,       pattern=r"^wiz:back:type$"),
                CallbackQueryHandler(cancel_callback,    pattern=r"^wiz:cancel$"),
            ],
            CONFIRMING: [
                CallbackQueryHandler(confirm,            pattern=r"^wiz:confirm:yes$"),
                CallbackQueryHandler(cancel_callback,    pattern=r"^wiz:cancel$"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_callback),
            CallbackQueryHandler(cancel_callback, pattern=r"^wiz:cancel$"),
        ],
        per_chat=True,
        per_user=True,
    )
    app.add_handler(conv)
