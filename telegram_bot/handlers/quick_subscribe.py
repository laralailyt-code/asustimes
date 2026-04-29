"""快速訂閱指令（給熟手用，省略精靈步驟）。

  /subscribe_region <國家或城市>
  /subscribe_part   <料件類別>
  /subscribe_supplier <關鍵字或ID>
  /subscribe_radius <緯度> <經度> <公里>

預設 min_severity = 'medium'（含以上才推），可用最後一個參數覆蓋：
  /subscribe_region 日本 high
"""
from __future__ import annotations

import asyncio
import logging

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import CommandHandler, ContextTypes

from telegram_bot import db

logger = logging.getLogger(__name__)

DEFAULT_SEV = "medium"
VALID_SEV = {"low", "medium", "high"}


def _parse_severity(args: list[str]) -> tuple[list[str], str]:
    """如果最後一個 arg 是 low/medium/high，當作 severity 抽出來。"""
    if args and args[-1].lower() in VALID_SEV:
        return args[:-1], args[-1].lower()
    return args, DEFAULT_SEV


async def _ensure_user(update: Update) -> dict | None:
    chat = update.effective_chat
    user = update.effective_user
    if not chat or not user:
        return None
    return await asyncio.to_thread(
        db.upsert_user,
        chat_id=chat.id,
        username=user.username,
        first_name=user.first_name,
        language_code=user.language_code,
    )


# ─── /subscribe_region ────────────────────────────────────────────────

async def cmd_region(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args, sev = _parse_severity(context.args or [])
    if not args:
        await update.message.reply_text(
            "用法：`/subscribe_region <國家或城市> [low|medium|high]`\n"
            "例：`/subscribe_region 日本`、`/subscribe_region 台灣/新竹 high`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    region = " ".join(args).strip()
    user = await _ensure_user(update)
    if not user:
        return

    sub = await asyncio.to_thread(
        db.add_subscription,
        user_id=user["id"],
        sub_type="region",
        value={"region": region},
        min_severity=sev,
    )
    sev_label = {"low": "🟡 低", "medium": "🟠 中", "high": "🔴 高"}[sev]
    await update.message.reply_text(
        f"✅ 已訂閱地區 `{region}`\n門檻：{sev_label}（含以上）\n規則編號：`#{sub['id']}`",
        parse_mode=ParseMode.MARKDOWN,
    )


# ─── /subscribe_part ──────────────────────────────────────────────────

async def cmd_part(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args, sev = _parse_severity(context.args or [])
    if not args:
        cats = await asyncio.to_thread(db.list_distinct_part_categories)
        await update.message.reply_text(
            "用法：`/subscribe_part <類別> [low|medium|high]`\n"
            "例：`/subscribe_part BATTERY medium`\n\n"
            f"目前可用類別：`{', '.join(cats)}`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    cat = " ".join(args).strip().upper()
    user = await _ensure_user(update)
    if not user:
        return

    sub = await asyncio.to_thread(
        db.add_subscription,
        user_id=user["id"],
        sub_type="part",
        value={"part_category": cat},
        min_severity=sev,
    )
    sev_label = {"low": "🟡 低", "medium": "🟠 中", "high": "🔴 高"}[sev]
    await update.message.reply_text(
        f"✅ 已訂閱料件類別 `{cat}`\n門檻：{sev_label}（含以上）\n規則編號：`#{sub['id']}`",
        parse_mode=ParseMode.MARKDOWN,
    )


# ─── /subscribe_supplier ──────────────────────────────────────────────

async def cmd_supplier(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args, sev = _parse_severity(context.args or [])
    if not args:
        await update.message.reply_text(
            "用法：`/subscribe_supplier <ID 或關鍵字> [low|medium|high]`\n"
            "例：`/subscribe_supplier 1`、`/subscribe_supplier 台北`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    term = " ".join(args).strip()
    sup = None
    # 優先當 ID 解析
    try:
        sup_id = int(term)
        sup = await asyncio.to_thread(db.get_supplier_by_id, sup_id)
    except ValueError:
        pass

    if not sup:
        results = await asyncio.to_thread(db.search_suppliers, term, 10)
        if not results:
            await update.message.reply_text(
                f"⚠️ 找不到符合「{term}」的供應商分布"
            )
            return
        if len(results) > 1:
            lines = [f"⚠️ 「{term}」對應到 {len(results)} 筆，請用 ID：\n"]
            for r in results[:10]:
                cats = "/".join((r.get("part_categories") or [])[:3])
                lines.append(f"  `#{r['id']}` {r['region']}" + (f" • {cats}" if cats else ""))
            lines.append(f"\n例：`/subscribe_supplier {results[0]['id']}`")
            await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)
            return
        sup = results[0]

    user = await _ensure_user(update)
    if not user:
        return

    sub = await asyncio.to_thread(
        db.add_subscription,
        user_id=user["id"],
        sub_type="supplier",
        value={"supplier_id": sup["id"]},
        min_severity=sev,
    )
    sev_label = {"low": "🟡 低", "medium": "🟠 中", "high": "🔴 高"}[sev]
    cats = "/".join((sup.get("part_categories") or [])[:3])
    await update.message.reply_text(
        f"✅ 已訂閱供應商分布 `#{sup['id']}` {sup['region']}"
        + (f" • {cats}" if cats else "")
        + f"\n門檻：{sev_label}（含以上）\n規則編號：`#{sub['id']}`",
        parse_mode=ParseMode.MARKDOWN,
    )


# ─── /subscribe_radius ────────────────────────────────────────────────

async def cmd_radius(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args, sev = _parse_severity(context.args or [])
    if len(args) != 3:
        await update.message.reply_text(
            "用法：`/subscribe_radius <緯度> <經度> <公里> [low|medium|high]`\n"
            "例：`/subscribe_radius 25.034 121.564 50`（台北 101 50km 圓內）",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    try:
        lat = float(args[0]); lng = float(args[1]); km = float(args[2])
    except ValueError:
        await update.message.reply_text("⚠️ 三個值都要是數字（緯度 經度 公里）")
        return
    if not (-90 <= lat <= 90 and -180 <= lng <= 180 and 0 < km <= 5000):
        await update.message.reply_text("⚠️ 數值超出範圍（緯度 -90~90, 經度 -180~180, 半徑 0~5000）")
        return

    user = await _ensure_user(update)
    if not user:
        return

    sub = await asyncio.to_thread(
        db.add_subscription,
        user_id=user["id"],
        sub_type="radius",
        value={"lat": lat, "lng": lng, "km": km},
        min_severity=sev,
    )
    sev_label = {"low": "🟡 低", "medium": "🟠 中", "high": "🔴 高"}[sev]
    await update.message.reply_text(
        f"✅ 已訂閱半徑 ({lat:.3f}, {lng:.3f}) {km:g}km 圓內\n"
        f"門檻：{sev_label}（含以上）\n規則編號：`#{sub['id']}`",
        parse_mode=ParseMode.MARKDOWN,
    )


def register(app):
    app.add_handler(CommandHandler("subscribe_region",   cmd_region))
    app.add_handler(CommandHandler("subscribe_part",     cmd_part))
    app.add_handler(CommandHandler("subscribe_supplier", cmd_supplier))
    app.add_handler(CommandHandler("subscribe_radius",   cmd_radius))
