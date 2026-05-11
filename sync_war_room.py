"""Sync the newest DIGITIMES desktop-pipeline output into news_platform's
war-room data directory.

Pipeline:
  C:\\Users\\Lara1_Lai\\Desktop\\digitimes\\data\\parsed\\notebooks_api_YYYYMMDD_HHMMSS.json
                                ↓  (this script)
  c:\\...\\news_platform\\data\\digitimes_competitor\\latest.json
                                ↓
  /api/competitor-warroom (web reads on demand)

Behavior:
- Scan SOURCE_DIR for notebooks_api_*.json, pick newest by mtime.
- If newer than current latest.json (by mtime), copy it.
- Update state.json's connector.last_success_at + last_attempted_at + status.
- Idempotent: re-run is a no-op when up-to-date.

Schedule via Windows Task Scheduler every ~10 min (lightweight; just stat + maybe copy).
Or call manually:  python sync_war_room.py
"""
from __future__ import annotations
import json
import shutil
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

SOURCE_DIR = Path(r"C:\Users\Lara1_Lai\Desktop\digitimes\data\parsed")
TARGET_DIR = Path(__file__).parent / "data" / "digitimes_competitor"
LATEST     = TARGET_DIR / "latest.json"
STATE      = TARGET_DIR / "state.json"
TW_TZ      = timezone(timedelta(hours=8))


def newest_source() -> Path | None:
    if not SOURCE_DIR.exists():
        return None
    candidates = sorted(SOURCE_DIR.glob("notebooks_api_*.json"),
                        key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else None


def update_state(success: bool, message: str, source_name: str | None = None) -> None:
    if not STATE.exists():
        return  # let app.py initialize default state
    try:
        state = json.loads(STATE.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[warn] state.json parse failed: {e}; skipping state update")
        return
    now_iso = datetime.now(TW_TZ).isoformat(timespec="seconds")
    conn = state.get("connector") or {}
    conn["last_attempted_at"] = now_iso
    if success:
        conn["last_success_at"] = now_iso
        conn["last_error"]      = None
        conn["status"]          = "ready"
        if source_name:
            conn.setdefault("loaded_files", []).insert(0, source_name)
            conn["loaded_files"] = list(dict.fromkeys(conn["loaded_files"]))[:10]
    else:
        conn["last_error"] = message
    state["connector"] = conn
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    src = newest_source()
    if src is None:
        print(f"[skip] no notebooks_api_*.json under {SOURCE_DIR}")
        update_state(False, f"source dir empty: {SOURCE_DIR}")
        return 1
    src_mtime = src.stat().st_mtime
    if LATEST.exists():
        cur_mtime = LATEST.stat().st_mtime
        if src_mtime <= cur_mtime:
            print(f"[skip] latest.json already up to date  "
                  f"(target mtime {datetime.fromtimestamp(cur_mtime, TW_TZ):%Y-%m-%d %H:%M:%S} >= "
                  f"source mtime {datetime.fromtimestamp(src_mtime, TW_TZ):%Y-%m-%d %H:%M:%S})")
            return 0
    TARGET_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, LATEST)
    print(f"[ok] copied {src.name} → {LATEST}  "
          f"(source mtime {datetime.fromtimestamp(src_mtime, TW_TZ):%Y-%m-%d %H:%M:%S})")
    update_state(True, "synced", source_name=src.name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
