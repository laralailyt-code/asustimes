"""Sync the newest DIGITIMES desktop-pipeline output into news_platform's
war-room data directory.

Pipeline:
  C:\\Users\\Lara1_Lai\\Desktop\\digitimes\\data\\parsed\\notebooks_api_YYYYMMDD_HHMMSS.json
                                ↓  (this script)
  c:\\...\\news_platform\\data\\digitimes_competitor\\latest.json
                                ↓
  /api/competitor-warroom (web reads on demand)
                                ↓
  (optional) git add + commit + push  →  Render auto-deploy

Behavior:
- Scan SOURCE_DIR for notebooks_api_*.json, pick newest by mtime.
- If newer than current latest.json (by mtime), copy it.
- Update state.json's connector.last_success_at + last_attempted_at + status.
- With --push, also git add + commit + push (REQUIRES data/digitimes_competitor/
  to be removed from .gitignore first, AND legal sign-off to expose DIGITIMES
  derivative data on a public repo). Default: copy-only.
- Idempotent: re-run is a no-op when up-to-date.

Usage:
  python sync_war_room.py              # copy only, local server picks up
  python sync_war_room.py --push       # copy + commit + push (triggers Render)

Schedule via Windows Task Scheduler every ~10 min (lightweight; just stat + maybe copy).
"""
from __future__ import annotations
import argparse
import json
import shutil
import subprocess
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


def _run(cmd: list[str], cwd: Path) -> tuple[int, str]:
    r = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", errors="replace")
    return r.returncode, (r.stdout + r.stderr).strip()


def git_commit_and_push(source_name: str, repo_root: Path) -> bool:
    """Stage data files, commit only if changed, push to origin/master.
    Safety guard: refuse to push if latest.json is gitignored AND not yet
    tracked. Once the file is in the index (as of master 22b78d6), .gitignore
    rules are advisory and don't block tracked-file commits."""
    # 1. Refuse only if latest.json is BOTH gitignored AND not yet tracked.
    rc_ignored, _ = _run(["git", "check-ignore", "-q", str(LATEST)], repo_root)
    if rc_ignored == 0:
        rc_tracked, _ = _run(["git", "ls-files", "--error-unmatch", str(LATEST)], repo_root)
        if rc_tracked != 0:
            print(f"[push-refuse] {LATEST.relative_to(repo_root)} is gitignored "
                  f"AND untracked — remove 'data/digitimes_competitor/' from "
                  f".gitignore first (and confirm legal allows public exposure).")
            return False

    # 2. Stage only the two data files (don't bundle other WIP).
    # -f overrides any local .gitignore rule (files are already tracked on master).
    rc, out = _run(["git", "add", "-f", str(LATEST), str(STATE)], repo_root)
    if rc != 0:
        print(f"[push-fail] git add: {out}")
        return False

    # 3. Anything to commit? If staged diff is empty, skip.
    rc, out = _run(["git", "diff", "--cached", "--quiet"], repo_root)
    if rc == 0:
        print("[push-skip] no staged changes to commit (files already in sync)")
        return True

    # 4. Commit
    msg = f"War room: sync {source_name}"
    rc, out = _run(["git", "commit", "-m", msg], repo_root)
    if rc != 0:
        print(f"[push-fail] git commit: {out}")
        return False
    print(f"[push] committed: {msg}")

    # 5. Push
    rc, out = _run(["git", "push", "origin", "master"], repo_root)
    if rc != 0:
        print(f"[push-fail] git push: {out}")
        return False
    print(f"[push-ok] pushed to origin/master — Render auto-deploy will trigger")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync DIGITIMES pipeline output to news_platform")
    parser.add_argument("--push", action="store_true",
                        help="After successful copy, git commit+push to trigger Render deploy. "
                             "Requires data/digitimes_competitor/ removed from .gitignore and legal sign-off.")
    args = parser.parse_args()

    src = newest_source()
    if src is None:
        print(f"[skip] no notebooks_api_*.json under {SOURCE_DIR}")
        update_state(False, f"source dir empty: {SOURCE_DIR}")
        return 1
    src_mtime = src.stat().st_mtime
    copied = False
    if LATEST.exists():
        cur_mtime = LATEST.stat().st_mtime
        if src_mtime <= cur_mtime:
            print(f"[skip] latest.json already up to date  "
                  f"(target mtime {datetime.fromtimestamp(cur_mtime, TW_TZ):%Y-%m-%d %H:%M:%S} >= "
                  f"source mtime {datetime.fromtimestamp(src_mtime, TW_TZ):%Y-%m-%d %H:%M:%S})")
        else:
            copied = True
    else:
        copied = True

    if copied:
        TARGET_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, LATEST)
        print(f"[ok] copied {src.name} → {LATEST}  "
              f"(source mtime {datetime.fromtimestamp(src_mtime, TW_TZ):%Y-%m-%d %H:%M:%S})")
        update_state(True, "synced", source_name=src.name)

    if args.push:
        git_commit_and_push(src.name, Path(__file__).parent)
    return 0


if __name__ == "__main__":
    sys.exit(main())
