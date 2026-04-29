"""ASUSTIMES Telegram Bot 整合套件。

包含以下模組：
- db.py：PostgreSQL 連線池與 CRUD 函式
- bot.py：python-telegram-bot Application 與 handler 註冊
- handlers/：各 Telegram 指令處理函式
- matcher.py：訂閱命中邏輯（M3）
- notifier.py：推播工作器（M3）
- event_persister.py：風險事件落地（M3）
"""
