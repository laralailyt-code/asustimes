"""
News Aggregation Platform — Flask Backend
Serves scraped news from Digitimes, 工商時報, 經濟日報
Auto-refreshes every 30 minutes in background.
"""

import threading
import time
import json
import logging
from datetime import datetime
from flask import Flask, jsonify, render_template, request
from scraper import fetch_all_news

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
    # Initial fetch on startup
    refresh_news()
    while True:
        time.sleep(REFRESH_INTERVAL)
        refresh_news()


# Start background thread
_bg_thread = threading.Thread(target=background_refresh_loop, daemon=True)
_bg_thread.start()


# ── Routes ─────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/news")
def api_news():
    category = request.args.get("category", "")
    source = request.args.get("source", "")
    q = request.args.get("q", "").strip()
    page = max(1, int(request.args.get("page", 1)))
    per_page = 20

    with _cache_lock:
        articles = list(_cache["articles"])
        last_updated = _cache["last_updated"]
        loading = _cache["loading"]

    # Filters
    if category and category != "全部":
        articles = [a for a in articles if a.get("category") == category]
    if source and source != "全部":
        articles = [a for a in articles if a.get("source") == source]
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
        "articles": paged,
        "total": total,
        "page": page,
        "per_page": per_page,
        "last_updated": last_updated,
        "loading": loading,
    })


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    t = threading.Thread(target=refresh_news, daemon=True)
    t.start()
    return jsonify({"status": "refreshing"})


@app.route("/api/stats")
def api_stats():
    with _cache_lock:
        articles = _cache["articles"]
        last_updated = _cache["last_updated"]

    categories: dict[str, int] = {}
    sources: dict[str, int] = {}
    for a in articles:
        categories[a.get("category", "其他")] = categories.get(a.get("category", "其他"), 0) + 1
        sources[a.get("source", "未知")] = sources.get(a.get("source", "未知"), 0) + 1

    return jsonify({
        "total": len(articles),
        "categories": categories,
        "sources": sources,
        "last_updated": last_updated,
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", debug=False, port=5050, use_reloader=False)
