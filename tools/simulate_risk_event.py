"""模擬風險事件並驗證命中 + 推播流程。

用法：
  python tools/simulate_risk_event.py preset taiwan-quake
  python tools/simulate_risk_event.py preset shenzhen-strike
  python tools/simulate_risk_event.py preset japan-typhoon
  python tools/simulate_risk_event.py preset korea-strike
  python tools/simulate_risk_event.py custom \\
      --type disaster --title "東京 M6.5 地震" \\
      --lat 35.68 --lng 139.76 --region "日本" --impact HIGH

  python tools/simulate_risk_event.py list-presets
  python tools/simulate_risk_event.py list-pending          # 看 DB 裡未推播的事件
  python tools/simulate_risk_event.py dispatch              # 強制觸發推播
  python tools/simulate_risk_event.py reset-notified <id>   # 把某事件改回未推播（重測用）

執行流程：
  1. 把模擬事件用 event_persister 寫入 risk_events 表
  2. 呼叫 notifier.push_event_to_users() 推播給命中的訂閱者
  3. 印出統計（matched / sent / failed / blocked / skipped_dup）
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import logging
import sys
from pathlib import Path

# Windows cp950 主控台中文 + emoji 友善
if sys.platform == "win32":
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

import os
from telegram import Bot

from telegram_bot import db, event_persister, notifier


PRESETS = {
    "taiwan-quake": {
        "id":        "sim-taiwan-quake-001",
        "type":      "disaster",
        "title":     "[模擬] 台灣花蓮外海 M6.5 地震",
        "lat":       23.97, "lng": 121.60,
        "region":    "台灣",
        "impact":    "HIGH",
        "time":      "2026-04-29",
        "supply":    "新竹科學園區部分晶圓廠暫停運作 4-6 小時，TSMC、聯電可能受影響",
        "source":    "USGS（模擬）",
        "sourceUrl": "https://earthquake.usgs.gov/",
    },
    "shenzhen-strike": {
        "id":        "sim-shenzhen-strike-001",
        "type":      "strike",
        "title":     "[模擬] 富士康深圳廠工人罷工",
        "lat":       22.54, "lng": 114.06,
        "region":    "中國大陸",
        "impact":    "HIGH",
        "time":      "2026-04-29",
        "supply":    "iPhone 組裝線停工，預估影響 10-15% 出貨",
        "source":    "Bing News（模擬）",
        "sourceUrl": "https://example.com/foxconn-strike",
    },
    "japan-typhoon": {
        "id":        "sim-japan-typhoon-001",
        "type":      "disaster",
        "title":     "[模擬] 強颱直撲熊本，TSMC JASM 暫停",
        "lat":       32.80, "lng": 130.71,
        "region":    "日本",
        "impact":    "CRITICAL",
        "time":      "2026-04-29",
        "supply":    "熊本廠暫停 24 小時，車用晶片供應吃緊",
        "source":    "JMA（模擬）",
        "sourceUrl": "https://www.jma.go.jp/",
    },
    "korea-strike": {
        "id":        "sim-korea-strike-001",
        "type":      "strike",
        "title":     "[模擬] Samsung 平澤工廠勞資爭議擴大",
        "lat":       36.99, "lng": 127.11,
        "region":    "韓國",
        "impact":    "MED",
        "time":      "2026-04-29",
        "supply":    "DRAM 產線部分停工，記憶體現貨價上漲",
        "source":    "Bing News（模擬）",
        "sourceUrl": "https://example.com/samsung-strike",
    },
    "battery-supplier": {
        "id":        "sim-battery-supplier-001",
        "type":      "operational",
        "title":     "[模擬] 中國大陸鋰電池供應商廠房火災",
        "lat":       30.50, "lng": 114.30,
        "region":    "中國大陸",
        "impact":    "HIGH",
        "time":      "2026-04-29",
        "supply":    "BATTERY 料件預估短缺 2-4 週，建議啟動備用供應",
        "source":    "（模擬）",
        "sourceUrl": "https://example.com/battery-fire",
    },
}


async def cmd_preset(name: str) -> None:
    if name not in PRESETS:
        print(f"❌ Unknown preset: {name}")
        print(f"Available: {', '.join(PRESETS)}")
        return
    event = PRESETS[name]
    await _send_event(event)


async def cmd_custom(args) -> None:
    event = {
        "id":        args.id or f"sim-custom-{abs(hash(args.title)) % 1000000:06d}",
        "type":      args.type,
        "title":     args.title,
        "lat":       args.lat,
        "lng":       args.lng,
        "region":    args.region,
        "impact":    args.impact,
        "time":      args.time or "2026-04-29",
        "supply":    args.supply or "",
        "source":    "simulate_risk_event.py",
        "sourceUrl": "",
    }
    await _send_event(event)


async def _send_event(event: dict) -> None:
    # 把 time 改成「現在」（讓新版 matcher 的新鮮度檢查通過）
    from datetime import datetime, timezone
    event["time"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

    print(f"📨 模擬事件：{event['title']}")
    print(f"   id={event['id']}  region={event['region']}  impact={event['impact']}")
    print(f"   座標 ({event['lat']}, {event['lng']})  time={event['time']}")

    db.init_pool()

    # 1. 落地
    new_ids = event_persister.persist_events([event])
    if not new_ids:
        print(f"⚠️ 事件 {event['id']} 已存在於 DB（不會重複推播）")
        print("   要重測請先用 reset-notified 指令")
        # 繼續嘗試推（會被去重邏輯擋掉）
    else:
        print(f"✅ 寫入 risk_events: {new_ids}")

    # 2. 連 Bot 推播
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        print("❌ TELEGRAM_BOT_TOKEN 未設定")
        return
    bot = Bot(token=token)
    print("📤 推播中...")
    stats = await notifier.push_event_to_users(bot, event)
    print(f"📊 結果：{stats}")
    if stats["matched"] == 0:
        print("\n💡 0 命中提示：")
        print("   1) 先用 /subscribe 在 bot 建立訂閱規則")
        print("   2) 確認規則的地區/料件能對應到此事件")
        print(f"      事件 region: {event['region']}, impact: {event['impact']}")

    # 3. 標記為已推播（避免之後 dispatcher 重推）
    event_persister.mark_notified([event["id"]])


async def cmd_dispatch() -> None:
    """強制掃一次未推播的事件，全部送出。"""
    db.init_pool()
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        print("❌ TELEGRAM_BOT_TOKEN 未設定")
        return
    bot = Bot(token=token)
    print("🔄 掃描未推播事件...")
    stats = await notifier.dispatch_pending(bot)
    print(f"📊 結果：{stats}")


def cmd_list_pending() -> None:
    db.init_pool()
    events = event_persister.fetch_pending_events(50)
    if not events:
        print("✅ 沒有未推播事件")
        return
    print(f"📋 未推播事件 ({len(events)}):")
    for ev in events:
        print(f"  - {ev['id']:40s} | {ev.get('type','?'):12s} | {ev.get('region','?'):12s} | {ev.get('impact','?'):8s} | {ev.get('title','')[:60]}")


def cmd_reset_notified(event_id: str) -> None:
    db.init_pool()
    with db.get_cursor() as cur:
        cur.execute("UPDATE risk_events SET notified = FALSE WHERE id = %s", (event_id,))
        cur.execute("DELETE FROM notification_log WHERE event_id = %s", (event_id,))
        affected = cur.rowcount
    print(f"✅ 重置 {event_id}（清掉 {affected} 筆 notification_log）")


def cmd_list_presets() -> None:
    print("Available presets:")
    for name, ev in PRESETS.items():
        print(f"  {name:25s}  {ev['type']:12s}  {ev['region']:10s}  {ev['impact']:8s}  {ev['title']}")


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd")

    p_preset = sub.add_parser("preset", help="使用內建情境")
    p_preset.add_argument("name", help="情境名（list-presets 看清單）")

    p_custom = sub.add_parser("custom", help="自訂事件")
    p_custom.add_argument("--id", default=None)
    p_custom.add_argument("--type", required=True, choices=["disaster", "geopolitical", "war", "strike", "operational"])
    p_custom.add_argument("--title", required=True)
    p_custom.add_argument("--lat", type=float, required=True)
    p_custom.add_argument("--lng", type=float, required=True)
    p_custom.add_argument("--region", required=True)
    p_custom.add_argument("--impact", required=True, choices=["LOW", "MED", "HIGH", "CRITICAL"])
    p_custom.add_argument("--time", default=None)
    p_custom.add_argument("--supply", default=None)

    sub.add_parser("list-presets", help="列出內建情境")
    sub.add_parser("list-pending", help="列 DB 裡未推播的事件")
    sub.add_parser("dispatch", help="強制觸發推播 dispatcher")

    p_reset = sub.add_parser("reset-notified", help="把事件改回未推播（重測用）")
    p_reset.add_argument("event_id")

    args = parser.parse_args()

    if args.cmd == "preset":
        asyncio.run(cmd_preset(args.name))
    elif args.cmd == "custom":
        asyncio.run(cmd_custom(args))
    elif args.cmd == "list-presets":
        cmd_list_presets()
    elif args.cmd == "list-pending":
        cmd_list_pending()
    elif args.cmd == "dispatch":
        asyncio.run(cmd_dispatch())
    elif args.cmd == "reset-notified":
        cmd_reset_notified(args.event_id)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
