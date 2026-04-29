"""python-telegram-bot Application 工廠 + 本地 polling 入口。

兩種啟動模式：
  1) 本地測試：直接 `python -m telegram_bot.bot`（用 polling）
  2) 部署到 Render：app.py 啟動時呼叫 build_application()，並把 webhook 路由接到 PTB

不論哪種模式，handler 都從 handlers/ 子模組註冊，避免重複碼。
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

# 讓 `python telegram_bot/bot.py` 也能 import telegram_bot.*
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from telegram.ext import Application, ApplicationBuilder

from telegram_bot import db, notifier
from telegram_bot.handlers import basic, subscribe_wizard, quick_subscribe

logger = logging.getLogger(__name__)


def build_application() -> Application:
    """建立 PTB Application 並註冊所有 handlers。同步呼叫，不啟動 loop。"""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN 未設定 (.env)")

    app = (
        ApplicationBuilder()
        .token(token)
        .build()
    )

    # 註冊 handlers
    basic.register(app)
    subscribe_wizard.register(app)
    quick_subscribe.register(app)
    notifier.register(app)  # 推播訊息上的 Inline 按鈕（已知悉 / 靜音 24h）

    # ─── 推播 Dispatcher：每 60 秒掃一次 risk_events.notified=false ───
    rate = int(os.environ.get("TELEGRAM_RATE_LIMIT_PER_SEC", "25"))

    async def _dispatcher_job(context):
        try:
            await notifier.dispatch_pending(context.bot, batch=50, rate_per_sec=rate)
        except Exception as e:
            logger.error(f"[dispatcher] error: {e}", exc_info=True)

    if app.job_queue is not None:
        app.job_queue.run_repeating(_dispatcher_job, interval=60, first=10, name="dispatch_pending")
        logger.info("Dispatcher job scheduled (interval=60s, first=10s)")
    else:
        logger.warning("No JobQueue available — install python-telegram-bot[job-queue]")

    # 啟動後把指令清單註冊給 BotFather（Telegram 客戶端 autocomplete 用）
    async def _set_commands(app):
        from telegram import BotCommand
        await app.bot.set_my_commands([
            BotCommand("start",              "註冊 / 查看歡迎訊息"),
            BotCommand("subscribe",          "🔔 開啟訂閱精靈（建議）"),
            BotCommand("subscribe_region",   "快速訂閱地區"),
            BotCommand("subscribe_part",     "快速訂閱料件類別"),
            BotCommand("subscribe_supplier", "快速訂閱供應商分布"),
            BotCommand("subscribe_radius",   "快速訂閱半徑範圍"),
            BotCommand("list",               "📋 列出我的訂閱"),
            BotCommand("unsubscribe",        "取消指定訂閱"),
            BotCommand("clear",              "🗑️ 清除所有訂閱"),
            BotCommand("help",               "說明"),
        ])
        logger.info("BotFather commands menu registered")

    app.post_init = _set_commands

    logger.info(f"PTB Application built, {len(app.handlers[0])} handlers registered")
    return app


def run_polling() -> None:
    """本地測試入口。直接 polling，不需要 webhook / ngrok。"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    db.init_pool()
    logger.info("DB pool initialized")

    app = build_application()
    logger.info("Starting polling (Ctrl+C to stop)...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    run_polling()
