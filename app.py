"""
News Aggregation Platform — Flask Backend
ASUSTIMES: ASUS tech industry news hub
Auto-refreshes every 30 minutes in background.
"""

import os
import threading
import time
import logging
import requests as req_lib
from datetime import datetime, date as date_cls, timedelta
from flask import Flask, jsonify, render_template, request
from scraper import fetch_all_news, CATEGORY_KEYWORDS

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)

# ── In-memory cache ────────────────────────────────────────────────────────────
_cache: dict = {
    "articles": [],
    "last_updated": None,
    "loading": False,
}
_cache_lock = threading.Lock()

REFRESH_INTERVAL = 30 * 60  # seconds


def refresh_news():
    with _cache_lock:
        if _cache["loading"]:
            return
        _cache["loading"] = True
    try:
        articles = fetch_all_news()
        with _cache_lock:
            _cache["articles"] = articles
            _cache["last_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            _cache["loading"] = False
        logger.info(f"Cache refreshed: {len(articles)} articles")
    except Exception as e:
        logger.error(f"refresh_news error: {e}")
        with _cache_lock:
            _cache["loading"] = False


def background_refresh_loop():
    refresh_news()
    while True:
        time.sleep(REFRESH_INTERVAL)
        refresh_news()


def daily_digest_loop():
    """Send digest email every day at DIGEST_HOUR (UTC)."""
    sent_date = None
    while True:
        time.sleep(60)
        now = datetime.utcnow()
        digest_hour = int(os.environ.get("DIGEST_HOUR", "0"))
        today_str = now.strftime("%Y-%m-%d")
        if now.hour == digest_hour and sent_date != today_str:
            recipients_raw = os.environ.get("DIGEST_RECIPIENTS", "")
            recipients = [r.strip() for r in recipients_raw.split(",") if r.strip()]
            if recipients:
                with _cache_lock:
                    articles = list(_cache["articles"])
                    last_updated = _cache["last_updated"]
                api_key = os.environ.get("RESEND_API_KEY", "")
                if api_key and articles:
                    html_body = _build_digest_html(articles, last_updated)
                    for r in recipients:
                        try:
                            req_lib.post(
                                "https://api.resend.com/emails",
                                headers={"Authorization": f"Bearer {api_key}",
                                         "Content-Type": "application/json"},
                                json={
                                    "from": "ASUSTIMES <onboarding@resend.dev>",
                                    "to": [r],
                                    "subject": f"ASUSTIMES 科技摘要 {today_str}",
                                    "html": html_body,
                                },
                                timeout=15,
                            )
                            logger.info(f"Daily digest sent to {r}")
                        except Exception as e:
                            logger.error(f"Daily digest error for {r}: {e}")
            sent_date = today_str


# ── Background thread: starts on first request (gunicorn-compatible) ───────────
_bg_started = False
_bg_lock = threading.Lock()

@app.before_request
def _ensure_bg_running():
    global _bg_started
    if not _bg_started:
        with _bg_lock:
            if not _bg_started:
                _bg_started = True
                t = threading.Thread(target=background_refresh_loop, daemon=True)
                t.start()
                td = threading.Thread(target=daily_digest_loop, daemon=True)
                td.start()
                logger.info("Background threads started in worker")


# ── Routes ─────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/ping")
def api_ping():
    """Lightweight keep-alive endpoint for uptime monitors."""
    return jsonify({"ok": True})


@app.route("/api/news")
def api_news():
    # Multi-category support: comma-separated ?categories=AI%20產業,半導體
    cats_param = request.args.get("categories", "").strip()
    # Legacy single-category fallback
    cat_param  = request.args.get("category", "").strip()
    source     = request.args.get("source", "").strip()
    q          = request.args.get("q", "").strip()
    date_filter = request.args.get("date_filter", "").strip()
    page       = max(1, int(request.args.get("page", 1)))
    per_page   = 20

    with _cache_lock:
        articles = list(_cache["articles"])
        last_updated = _cache["last_updated"]
        loading = _cache["loading"]

    # Category filter
    selected_cats = []
    if cats_param:
        selected_cats = [c.strip() for c in cats_param.split(",") if c.strip()]
    elif cat_param and cat_param != "全部":
        selected_cats = [cat_param]

    if selected_cats:
        articles = [a for a in articles if a.get("category") in selected_cats]

    # Source filter
    if source and source != "全部":
        articles = [a for a in articles if a.get("source") == source]

    # Date filter
    if date_filter:
        today = date_cls.today()
        if date_filter == "today":
            cutoff = today.strftime("%Y-%m-%d")
            articles = [a for a in articles
                        if (a.get("published") or a.get("fetched_at", ""))[:10] == cutoff]
        elif date_filter == "yesterday":
            cutoff = (today - timedelta(days=1)).strftime("%Y-%m-%d")
            articles = [a for a in articles
                        if (a.get("published") or a.get("fetched_at", ""))[:10] == cutoff]
        elif date_filter == "3days":
            cutoff = (today - timedelta(days=3)).strftime("%Y-%m-%d")
            articles = [a for a in articles
                        if (a.get("published") or a.get("fetched_at", ""))[:10] >= cutoff]

    # Keyword search
    if q:
        ql = q.lower()
        articles = [
            a for a in articles
            if ql in a.get("title", "").lower() or ql in a.get("summary", "").lower()
        ]

    total = len(articles)
    start = (page - 1) * per_page
    paged = articles[start: start + per_page]

    return jsonify({
        "articles":     paged,
        "total":        total,
        "page":         page,
        "per_page":     per_page,
        "last_updated": last_updated,
        "loading":      loading,
    })


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    t = threading.Thread(target=refresh_news, daemon=True)
    t.start()
    return jsonify({"status": "refreshing"})


@app.route("/api/stats")
def api_stats():
    source      = request.args.get("source", "").strip()
    date_filter = request.args.get("date_filter", "").strip()

    with _cache_lock:
        articles = list(_cache["articles"])
        last_updated = _cache["last_updated"]

    if source and source != "全部":
        articles = [a for a in articles if a.get("source") == source]

    if date_filter:
        today = date_cls.today()
        if date_filter == "today":
            cutoff = today.strftime("%Y-%m-%d")
            articles = [a for a in articles if (a.get("published") or a.get("fetched_at", ""))[:10] == cutoff]
        elif date_filter == "yesterday":
            cutoff = (today - timedelta(days=1)).strftime("%Y-%m-%d")
            articles = [a for a in articles if (a.get("published") or a.get("fetched_at", ""))[:10] == cutoff]
        elif date_filter == "3days":
            cutoff = (today - timedelta(days=3)).strftime("%Y-%m-%d")
            articles = [a for a in articles if (a.get("published") or a.get("fetched_at", ""))[:10] >= cutoff]

    categories: dict[str, int] = {}
    sources: dict[str, int] = {}
    for a in articles:
        cat = a.get("category", "其他")
        src = a.get("source", "未知")
        categories[cat] = categories.get(cat, 0) + 1
        sources[src] = sources.get(src, 0) + 1

    return jsonify({
        "total":        len(articles),
        "categories":   categories,
        "sources":      sources,
        "last_updated": last_updated,
    })


# ── Email digest ───────────────────────────────────────────────────────────────
def _build_digest_html(articles: list[dict], last_updated: str | None) -> str:
    from scraper import CATEGORY_KEYWORDS
    cats = list(CATEGORY_KEYWORDS.keys())

    rows_by_cat: dict[str, list[dict]] = {c: [] for c in cats}
    for a in articles:
        cat = a.get("category", "")
        if cat in rows_by_cat:
            rows_by_cat[cat].append(a)

    sections = ""
    for cat in cats:
        items = rows_by_cat[cat][:5]
        if not items:
            continue
        links = "".join(
            f'<li style="margin:6px 0"><a href="{a["source_url"]}" style="color:#1464f6;text-decoration:none">'
            f'{a["title"]}</a>'
            f'<span style="color:#888;font-size:12px"> — {a.get("source","")} {(a.get("published") or "")[:10]}</span>'
            f'</li>'
            for a in items
        )
        sections += (
            f'<h3 style="margin:20px 0 6px;color:#0f172a;font-size:15px">{cat}</h3>'
            f'<ul style="margin:0;padding-left:18px;color:#334155">{links}</ul>'
        )

    updated_str = last_updated or datetime.now().strftime("%Y-%m-%d %H:%M")
    return f"""
    <html><body style="font-family:sans-serif;max-width:640px;margin:0 auto;padding:24px;color:#0f172a">
    <h1 style="font-size:22px;border-bottom:2px solid #1464f6;padding-bottom:10px">
      📰 ASUSTIMES 科技摘要</h1>
    <p style="color:#64748b;font-size:13px">資料更新：{updated_str}</p>
    {sections}
    <hr style="margin-top:30px;border:none;border-top:1px solid #e2e8f0"/>
    <p style="color:#94a3b8;font-size:12px">由 ASUSTIMES 自動發送 — asustimes.onrender.com</p>
    </body></html>
    """


@app.route("/api/send-digest", methods=["POST"])
def api_send_digest():
    data = request.get_json(silent=True) or {}
    recipient = data.get("recipient", "").strip()
    if not recipient or "@" not in recipient:
        return jsonify({"ok": False, "message": "請輸入有效的 Email 地址"}), 400

    api_key = os.environ.get("RESEND_API_KEY", "")
    if not api_key:
        return jsonify({"ok": False, "message": "❌ 伺服器尚未設定 RESEND_API_KEY"}), 503

    with _cache_lock:
        articles = list(_cache["articles"])
        last_updated = _cache["last_updated"]

    if not articles:
        return jsonify({"ok": False, "message": "目前無新聞資料，請稍後再試"}), 503

    try:
        html_body = _build_digest_html(articles, last_updated)
        resp = req_lib.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "from": "ASUSTIMES <onboarding@resend.dev>",
                "to": [recipient],
                "subject": f"ASUSTIMES 科技摘要 {datetime.now().strftime('%Y-%m-%d')}",
                "html": html_body,
            },
            timeout=15,
        )
        if resp.status_code in (200, 201):
            logger.info(f"Digest sent to {recipient}")
            return jsonify({"ok": True, "message": f"✅ 摘要已發送至 {recipient}"})
        else:
            logger.error(f"Resend error: {resp.status_code} {resp.text}")
            return jsonify({"ok": False, "message": f"❌ 發送失敗：{resp.text}"}), 500
    except Exception as e:
        logger.error(f"send_digest error: {e}")
        return jsonify({"ok": False, "message": f"❌ 發送失敗：{e}"}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", debug=False, port=5050, use_reloader=False)
