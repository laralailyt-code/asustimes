"""
News Aggregation Platform — Flask Backend
ASUSTIMES: ASUS tech industry news hub
Auto-refreshes every 30 minutes in background.
"""

import os
import csv
import json
import threading
import time
import logging
import requests as req_lib
from concurrent.futures import ThreadPoolExecutor, as_completed, wait as fut_wait
from datetime import datetime, date as date_cls, timedelta, timezone
from flask import Flask, jsonify, render_template, request
from scraper import fetch_all_news, CATEGORY_KEYWORDS

try:
    import yfinance as yf
    _YF_AVAILABLE = True
except ImportError:
    _YF_AVAILABLE = False
    logging.getLogger(__name__).warning("yfinance not installed – live commodity prices disabled")

try:
    import anthropic as _anthropic_lib
    _ANTHROPIC_AVAILABLE = True
except ImportError:
    _ANTHROPIC_AVAILABLE = False

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__, static_folder='templates', static_url_path='')

# ── Timezone ───────────────────────────────────────────────────────────────────
TW_TZ = timezone(timedelta(hours=8))

# ── Environment detection ──────────────────────────────────────────────────────
# On Render: RENDER=true, On Localhost: RENDER is not set
_IS_RENDER_PRODUCTION = os.environ.get("RENDER") == "true"
_SHOW_RISK_PAGE = True  # Show risk page in all environments

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
        # Fetch fresh articles
        fresh_articles = fetch_all_news()

        # Save fresh articles to archive for persistent storage
        _save_articles_to_archive(fresh_articles)

        # Load archived articles (past 2 years)
        archived_articles = _load_archived_articles()

        # Merge fresh + archived, deduplicate by URL
        merged = {}
        for article in archived_articles:
            url = article.get("source_url", "")
            if url:
                merged[url] = article

        for article in fresh_articles:
            url = article.get("source_url", "")
            if url:
                merged[url] = article  # Fresh articles overwrite archived ones

        articles = list(merged.values())
        articles.sort(key=lambda a: a.get("published") or a.get("fetched_at", ""), reverse=True)

        with _cache_lock:
            _cache["articles"] = articles
            _cache["last_updated"] = datetime.now(TW_TZ).strftime("%Y-%m-%d %H:%M:%S")
            _cache["loading"] = False
        logger.info(f"Cache refreshed: {len(articles)} articles ({len(fresh_articles)} fresh, {len(archived_articles)} archived)")
    except Exception as e:
        logger.error(f"refresh_news error: {e}")
        with _cache_lock:
            _cache["loading"] = False


def background_refresh_loop():
    refresh_news()
    while True:
        time.sleep(REFRESH_INTERVAL)
        refresh_news()


def _risk_cache_preload_loop():
    """Pre-warm geopolitical + strike caches at startup and every 3 hours."""
    first_run = True
    while True:
        try:
            if not first_run:
                time.sleep(2)  # Brief delay for subsequent runs
            logger.info("[RISK] Pre-warming geopolitical cache (parallel)...")
            _do_geo_scan()
        except Exception as e:
            logger.warning(f"[RISK] geo preload error: {e}")
        try:
            logger.info("[RISK] Pre-warming strike cache (parallel)...")
            _do_strike_scan()
        except Exception as e:
            logger.warning(f"[RISK] strike preload error: {e}")
        logger.info("[RISK] Risk caches pre-warmed.")
        first_run = False
        time.sleep(3 * 3600)  # 每 3 小時更新一次


def _digitimes_refresh_loop():
    """Refresh all news (including Digitimes from Bing RSS) every 2 hours.
    Note: Digitimes articles are now sourced from Bing News RSS feeds,
    so this loop refreshes all articles to ensure Digitimes coverage is up-to-date."""

    logger.info("[Digitimes] Background refresh loop starting (2-hour interval)...")
    first_run = True

    while True:
        try:
            if not first_run:
                logger.debug("[Digitimes] Waiting 2 hours before next refresh...")
                time.sleep(2 * 3600)  # 2 hours
            else:
                time.sleep(30 * 60)  # First refresh at 30 minutes (after main loop starts)

            logger.info("[Digitimes] Starting 2-hour news refresh (Bing News sources)...")
            refresh_news()
            logger.info("[Digitimes] Refresh complete - Digitimes articles updated from Bing News")
        except Exception as e:
            logger.error(f"[Digitimes] Refresh loop error: {e}")

        first_run = False


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
                tl = threading.Thread(target=_live_price_loop, daemon=True)
                tl.start()
                tr = threading.Thread(target=_risk_cache_preload_loop, daemon=True)
                tr.start()
                tdt = threading.Thread(target=_digitimes_refresh_loop, daemon=True)
                tdt.start()
                logger.info("Background threads started in worker (including Digitimes 2-hour refresh)")


# ── Routes ─────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    ensure_background_threads()
    return render_template("index.html", show_risk_page=_SHOW_RISK_PAGE)


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

    # Source filter
    if source and source != "全部":
        articles = [a for a in articles if a.get("source") == source]

    # Date filter
    if date_filter:
        today = datetime.now(TW_TZ).date()
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

    # Category counts BEFORE category filter (so tabs always show correct numbers)
    cat_counts: dict[str, int] = {}
    src_counts: dict[str, int] = {}
    for a in articles:
        cat = a.get("category", "")
        src = a.get("source", "")
        cat_counts[cat] = cat_counts.get(cat, 0) + 1
        src_counts[src] = src_counts.get(src, 0) + 1

    # Category filter (applied AFTER counting)
    selected_cats = []
    if cats_param:
        selected_cats = [c.strip() for c in cats_param.split(",") if c.strip()]
    elif cat_param and cat_param != "全部":
        selected_cats = [cat_param]

    if selected_cats:
        articles = [a for a in articles if a.get("category") in selected_cats]

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
        "cat_counts":   cat_counts,
        "src_counts":   src_counts,
    })


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    t = threading.Thread(target=refresh_news, daemon=True)
    t.start()
    return jsonify({"status": "refreshing"})


# ── Category digest (AI-powered summary) ───────────────────────────────────────
_digest_cache: dict = {}
_digest_lock = threading.Lock()


def _resolve_google_news_url(url: str) -> str:
    """Decode Google News redirect URL (CBMi...) to get the actual article URL. Non-Google URLs pass through unchanged."""
    if "news.google.com" not in url:
        return url
    try:
        import base64 as _b64, re as _re
        m = _re.search(r"/articles/([A-Za-z0-9_=-]+)", url)
        if not m:
            return url
        encoded = m.group(1)
        padding = (4 - len(encoded) % 4) % 4
        decoded = _b64.urlsafe_b64decode(encoded + "=" * padding)
        found = _re.findall(rb"https?://[^\x00-\x1f\s<>\"']+", decoded)
        if found:
            return found[0].decode("utf-8", errors="ignore").rstrip(".,)")
    except Exception:
        pass
    return url


def _fetch_article_snippet(url: str, max_chars: int = 150) -> str:
    """Resolve Google/Bing News redirect, then extract a short text snippet."""
    if not url:
        return ""
    try:
        from bs4 import BeautifulSoup as _BS
        actual_url = _resolve_google_news_url(url)
        r = req_lib.get(
            actual_url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
                "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8",
            },
            timeout=6,
            allow_redirects=True,
        )
        soup = _BS(r.content, "html.parser")
        for tag in soup(["script", "style", "nav", "header", "footer", "aside", "figure"]):
            tag.decompose()
        for sel in [
            "article p", ".article-content p", ".article-body p",
            ".entry-content p", ".post-content p", ".news-content p",
            "main p", ".content p", "p",
        ]:
            for p in soup.select(sel):
                text = p.get_text(strip=True)
                if len(text) > 40:
                    return text[:max_chars] + ("…" if len(text) > max_chars else "")
    except Exception:
        pass
    return ""


_LOW_VALUE_TITLE_KW = [
    "展覽", "論壇", "研討會", "出席", "參展", "邀請函", "招募", "徵才", "開幕",
    "記者會通知", "頒獎", "得獎名單", "活動報名", "免費報名",
]
_HIGH_VALUE_TITLE_KW = [
    "億", "百億", "兆", "市佔", "季報", "財報", "年報", "EPS", "營收", "毛利",
    "量產", "出貨", "導入", "突破", "裁員", "漲價", "降價", "合作", "收購",
    "投資", "布局", "超越", "首款", "新一代", "發布", "上市",
]


def _article_score(a: dict) -> int:
    title   = a.get("title", "")
    summary = a.get("summary", "") or ""
    score   = 50
    for kw in _LOW_VALUE_TITLE_KW:
        if kw in title:
            score -= 25
    for kw in _HIGH_VALUE_TITLE_KW:
        if kw in title or kw in summary:
            score += 12
    if len(summary) > 40:
        score += 8
    return score


@app.route("/api/digest")
def api_digest():
    import re as _re
    from concurrent.futures import ThreadPoolExecutor as _TPE, as_completed as _ac

    category = request.args.get("category", "").strip()
    if not category or category == "全部":
        return jsonify({"error": "select a category"}), 400

    today = datetime.now(TW_TZ).date().isoformat()

    with _digest_lock:
        cached = _digest_cache.get(category)
        if cached and cached.get("date") == today:
            return jsonify(cached)

    with _cache_lock:
        all_articles = list(_cache["articles"])

    # Prefer today's articles; fall back to latest 48h
    cat_articles = [
        a for a in all_articles
        if a.get("category") == category
        and (a.get("published") or a.get("fetched_at", ""))[:10] == today
    ]
    if len(cat_articles) < 3:
        cutoff = (datetime.now(TW_TZ).date() - timedelta(days=2)).isoformat()
        cat_articles = [
            a for a in all_articles
            if a.get("category") == category
            and (a.get("published") or a.get("fetched_at", ""))[:10] >= cutoff
        ]
    if not cat_articles:
        cat_articles = [a for a in all_articles if a.get("category") == category][:15]

    if not cat_articles:
        return jsonify({"category": category, "points": [], "articles": [], "ai_powered": False})

    # Sort by quality score; keep top candidates for AI
    ranked = sorted(cat_articles, key=_article_score, reverse=True)
    top    = ranked[:12]

    # Fetch article snippets in parallel to give AI real content
    def _clean_rss(title: str, raw: str) -> str:
        s = (raw or "").strip()
        if not s:
            return ""
        t_n = _re.sub(r'[\s\-–—·|•]+', '', title).lower()
        s_n = _re.sub(r'[\s\-–—·|•]+', '', s).lower()
        if s_n.startswith(t_n):
            s = s[len(title):].lstrip(" -–—\t").strip()
        s = _re.sub(r'\s*[-–—]\s*\S[\w\s]{1,30}$', '', s).strip()
        return s

    snippets: dict[int, str] = {}
    for i, a in enumerate(top):
        snippets[i] = _clean_rss(a.get("title", ""), a.get("summary") or "")

    needs_fetch = [i for i, a in enumerate(top) if len(snippets[i]) < 30 and a.get("source_url")]
    if needs_fetch:
        with _TPE(max_workers=min(len(needs_fetch), 6)) as ex:
            futs = {ex.submit(_fetch_article_snippet, top[i].get("source_url", ""), 200): i
                    for i in needs_fetch}
            for fut in _ac(futs, timeout=14):
                i = futs[fut]
                try:
                    fetched = fut.result()
                    if fetched and len(fetched) > 30:
                        snippets[i] = fetched
                except Exception:
                    pass

    article_links = [
        {
            "title":     a["title"],
            "url":       a.get("source_url", ""),
            "source":    a.get("source", ""),
            "published": (a.get("published") or "")[:10],
        }
        for a in ranked[:6]
    ]

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    points: list[str] = []
    ai_powered = False

    if api_key and _ANTHROPIC_AVAILABLE:
        try:
            articles_text = "\n".join([
                f"{i+1}. {top[i]['title']}｜{snippets.get(i, '')}"
                for i in range(len(top))
            ])
            client = _anthropic_lib.Anthropic(api_key=api_key)
            msg = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=1500,
                messages=[{
                    "role": "user",
                    "content": (
                        f"以下是「{category}」類別的近期科技新聞（標題｜內文摘要）：\n\n{articles_text}\n\n"
                        "請用繁體中文，從中嚴格篩選出 2–5 條「真正值得關注的焦點」。\n\n"
                        "【選入標準】具體數字（金額、出貨量、市佔率）、重大合作/收購/投資、"
                        "技術突破、產業政策轉折、供應鏈重組。\n"
                        "【排除標準】活動通知、展覽、人事任命（除非影響重大）、"
                        "一般產品發表、重複主題、內容空洞的標題新聞。\n\n"
                        "要求：每條一句完整的話（50–80字），說明核心事實與產業意義，不要轉述標題；"
                        "每條前加「•」；每條必須是完整句子，不可在句子中間截斷；"
                        "只輸出條列內容，不要加任何說明或標題。\n"
                        "至少輸出 2 條；若真的無任何值得關注的新聞，輸出：NONE"
                    ),
                }],
            )
            raw = msg.content[0].text.strip()
            if raw != "NONE":
                points = [
                    line.strip().lstrip("•·▪▸►→- ").strip()
                    for line in raw.split("\n")
                    if line.strip() and len(line.strip()) > 15
                ]
            ai_powered = True
        except Exception as e:
            logger.warning(f"Digest AI error: {e}")

    # Fallback: show top articles with snippets (or at least titles)
    if not points:
        for i, a in enumerate(top[:5]):
            snippet = snippets.get(i, "")
            if len(snippet) > 40:
                points.append(f"{a['title']}：{snippet[:150]}")
            else:
                points.append(a['title'])
        # Still enforce minimum 2 items
        if len(points) < 2 and len(top) >= 2:
            for a in top[len(points):2]:
                points.append(a['title'])
        if not points:
            return jsonify({"category": category, "points": [], "articles": [], "ai_powered": False})

    result = {
        "date":       today,
        "category":   category,
        "points":     points[:5],
        "articles":   article_links,
        "ai_powered": ai_powered,
    }
    with _digest_lock:
        _digest_cache[category] = result

    return jsonify(result)


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
        today = datetime.now(TW_TZ).date()
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

    updated_str = last_updated or datetime.now(TW_TZ).strftime("%Y-%m-%d %H:%M")
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
                "subject": f"ASUSTIMES 科技摘要 {datetime.now(TW_TZ).strftime('%Y-%m-%d')}",
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


# ── Live commodity price fetching ─────────────────────────────────────────────

# yfinance symbol → (exact CSV item name, price multiplier to match CSV unit)
_LIVE_COMMODITY_SYMBOLS = {
    "GC=F":  ("金 (gold) US$/盎司",            1.0),       # Gold $/oz
    "SI=F":  ("銀 (silver) US$/盎司",          1.0),       # Silver $/oz
    "CL=F":  ("石油 西德州 ( US$/桶)",          1.0),       # WTI $/barrel
    "BZ=F":  ("石油 北海布蘭特 (US$/桶)",       1.0),       # Brent $/barrel
    "HG=F":  ("銅 (copper) US$/tonne",         100.0),    # Copper (convert from ¢/lb to USD/tonne)
    "ALI=F": ("鋁 (aluminum) US$/tonne",       220.0),    # Aluminum (convert from ¢/lb to USD/tonne)
}

# yfinance FX tickers → (exact CSV item name, multiplier)
# All return "foreign currency per 1 USD" — matches CSV convention "美元 / XXX"
_LIVE_FX_YF_SYMBOLS = {
    "TWD=X": ("美元 / 台幣",            1.0),
    "CNY=X": ("美元 / 人民幣",          1.0),
    "JPY=X": ("美元 / 日圓",            1.0),
    "EUR=X": ("美元 / 歐元",            1.0),   # EUR per USD ≈ 0.92, no inversion
    "BRL=X": ("美元 / 巴西里爾(巴西幣)", 1.0),
    "KRW=X": ("美元 / 韓圜",            1.0),
    "IDR=X": ("美元 / 印尼盾",          1.0),
    "INR=X": ("美元 / 印度幣",          1.0),
}

_TUNGSTEN_NAME = "鎢"

_live_commodity_cache: dict = {}   # {csv_item_name: [(date_str, value)]}
_live_cache_lock = threading.Lock()

# Source URL per item name (populated during price refresh)
_item_sources: dict = {}   # {csv_item_name: {"label": str, "url": str}}
_item_sources_lock = threading.Lock()

# Parsed CSV cache (invalidated on live price update)
_csv_parse_cache: dict = {"data": None, "ts": 0.0}
_csv_parse_lock = threading.Lock()

# bot.com.tw BCD API code → (csv_item_name, price_multiplier) — all use history fetch
# NOTE: 190020 (長纖紙漿) removed due to data corruption from 2025-11-01 onwards
_BOT_BCD_CODES = {
    "130041": ("ABS聚合物(注塑) 中國到岸價 US$/tonne", 1.0),   # ABS China CIF
    "190060": ("瓦楞芯紙 CNY$/tonne",                  1.0),   # Corrugated paper
}

# buyplas.com plastic prices (latest only, no history available)
_BUYPLAS_ITEMS = {
    "PC_SABIC":     "PC塑料 (SABIC) CNY$/tonne",
    "PC_ABS_SABIC": "PC/ABS塑料 (SABIC) CNY$/tonne",
}

# Trading Economics slug → (csv_item_name, price_multiplier)
# Prices are scraped from tradingeconomics.com/commodity/<slug>
_TE_SLUGS = {
    "tin":        ("錫 (tin) US$/tonne",         1.0),       # TE in USD/tonne ✓
    "nickel":     ("鎳 (nickel)  US$/tonne",     1.0),       # TE in USD/tonne ✓
    "zinc":       ("鋅 (zinc)  US$/tonne",       1.0),       # TE in USD/tonne ✓
    "lithium":    ("鋰 (Lithium) CNY$/tonne",    1.0),       # TE in CNY/tonne ✓
    "phosphorus": ("黃磷 CNY$/tonne",            29.4274),   # TE in CNY/百kg → CNY/tonne
}

# Cobalt moved to dedicated fetcher (_fetch_cobalt_price) due to TE data quality issues
# Using metals.live API as primary source instead


def _fetch_bot_bcd_price(code: str) -> float | None:
    """Fetch latest price from bot.com.tw BCD API.
    Response format: 'YYYY/MM/DD,YYYY/MM/DD,...,YYYY/MM/DD VAL,VAL,...,VAL'
    Dates and values are separated by a space.
    """
    import re as _re
    try:
        url = f"https://fund.bot.com.tw/Z/ZH/ZHG/CZHG.djbcd?A={code}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
            "Referer": "https://fund.bot.com.tw/",
        }
        r = req_lib.get(url, headers=headers, timeout=12, verify=False)
        data = r.text.strip()
        if not data or len(data) < 20:
            return None
        # Find the last date and split at the space after it
        m = _re.search(r'(\d{4}/\d{2}/\d{2})\s+([\d.,]+)$', data)
        if m:
            # Get the value after the last date-space separator
            vals_str = data[m.start(2):]
            vals = [v.strip() for v in vals_str.split(",") if v.strip()]
            for v in reversed(vals):
                return float(v)
        # Fallback: find all values (numbers) after the last date
        all_dates = _re.findall(r'\d{4}/\d{2}/\d{2}', data)
        if all_dates:
            last_date = all_dates[-1]
            after_dates = data[data.rfind(last_date) + len(last_date):]
            vals = [v.strip() for v in after_dates.split(",") if v.strip()]
            for v in reversed(vals):
                try:
                    return float(v)
                except ValueError:
                    continue
    except Exception as e:
        logger.warning(f"bot.com.tw BCD {code}: {e}")
    return None


def _fetch_bot_bcd_history(code: str) -> list:
    """Fetch full price history from bot.com.tw BCD API.
    Response format: 'YYYY/MM/DD,YYYY/MM/DD,...,YYYY/MM/DD<space>val1,val2,...,valN'
    Returns list of (YYYY-MM-DD, float) pairs sorted oldest to newest.
    """
    import re as _re
    try:
        url = f"https://fund.bot.com.tw/Z/ZH/ZHG/CZHG.djbcd?A={code}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
            "Referer": "https://fund.bot.com.tw/",
        }
        r = req_lib.get(url, headers=headers, timeout=12, verify=False)
        data = r.text.strip()
        if not data or len(data) < 20:
            return []
        # Split at the space between last date and first value
        # Format: "D1,D2,...,DN VAL1,VAL2,...,VALN"
        split_m = _re.search(r'(\d{4}/\d{2}/\d{2})\s+(\d)', data)
        if not split_m:
            return []
        dates_str = data[:split_m.start(2)].strip().rstrip(' ')
        vals_str  = data[split_m.start(2):]
        dates = [d.strip() for d in dates_str.split(',') if _re.match(r'\d{4}/\d{2}/\d{2}$', d.strip())]
        vals  = []
        for v in vals_str.split(','):
            v = v.strip()
            try:
                vals.append(float(v))
            except ValueError:
                break
        pairs = [(d.replace('/', '-'), round(v, 2)) for d, v in zip(dates, vals)]
        logger.info(f"bot.com.tw BCD history {code}: {len(pairs)} points")
        return pairs
    except Exception as e:
        logger.warning(f"bot.com.tw BCD history {code}: {e}")
    return []


def _fetch_buyplas_price(product_key: str) -> float | None:
    """Fetch plastic price from buyplas.com.
    product_key: 'PC_SABIC' or 'PC_ABS_SABIC'
    """
    import re as _re
    try:
        url = "https://www.buyplas.com/spot/1003-PP-PE-PVC-ABS-PS.html"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
            "Accept-Language": "zh-CN,zh;q=0.9",
        }
        r = req_lib.get(url, headers=headers, timeout=15)
        text = r.text
        if product_key == "PC_SABIC":
            # PC 1000R SABIC
            m = _re.search(r'SABIC[^<]*1000R[^<]*?(\d[\d,]+)', text, _re.IGNORECASE)
            if not m:
                m = _re.search(r'1000R[^<]*?(\d[\d,]+)', text, _re.IGNORECASE)
        elif product_key == "PC_ABS_SABIC":
            # PC/ABS C6600-111 SABIC
            m = _re.search(r'C6600[^<]*?(\d[\d,]+)', text, _re.IGNORECASE)
            if not m:
                m = _re.search(r'SABIC[^<]*?C6600[^<]*?(\d[\d,]+)', text, _re.IGNORECASE)
        else:
            return None
        if m:
            return float(m.group(1).replace(",", ""))
    except Exception as e:
        logger.warning(f"buyplas.com {product_key}: {e}")
    return None


def _fetch_te_price(slug: str) -> float | None:
    """Scrape latest price from tradingeconomics.com/commodity/<slug>."""
    import re
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        }
        r = req_lib.get(f"https://tradingeconomics.com/commodity/{slug}",
                        headers=headers, timeout=12)
        m = re.search(r'"last":"?([\d.]+)', r.text)
        if m:
            return float(m.group(1))
    except Exception as e:
        logger.warning(f"TE scrape {slug}: {e}")
    return None


def _fetch_cobalt_price() -> float | None:
    """Fetch cobalt price from metals.live API (LME data only).
    Only use metals.live (LME), don't use Trading Economics fallback (unreliable for cobalt).
    If fetch fails, return None and use cached price instead.
    """
    try:
        headers = HEADERS.copy()
        headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        headers["Pragma"] = "no-cache"
        r = req_lib.get(
            "https://api.metals.live/v1/spot/cobalt",
            headers=headers,
            timeout=10,
            params={"nocache": int(time.time())}  # Cache-bust with timestamp
        )
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, dict) and "price" in data:
                price = float(data["price"])
                if price > 0 and 50000 < price < 60000:  # Sanity check: cobalt should be in this range
                    logger.info(f"Cobalt from metals.live (LME): ${price}/tonne (fresh)")
                    return price
                else:
                    logger.warning(f"Cobalt price {price} out of expected range, skipping")
    except Exception as e:
        logger.debug(f"metals.live cobalt fetch failed: {e}")

    return None


def _fetch_1year_lme_history(yf_symbol: str, multiplier: float = 1.0) -> list[tuple[str, float]]:
    """Fetch 1 year of LME metal price history from yfinance for initialization.
    Returns: [(date_str, price), ...] sorted chronologically
    """
    try:
        if not _YF_AVAILABLE:
            return []
        hist = yf.Ticker(yf_symbol).history(period="1y", interval="1d", auto_adjust=True)
        if hist.empty or "Close" not in hist.columns:
            return []

        points = []
        for date_ts, close_price in hist["Close"].items():
            date_str = str(date_ts.date())
            price = round(float(close_price) * multiplier, 2)
            if price > 0:
                points.append((date_str, price))

        logger.info(f"Fetched {len(points)} points from yfinance {yf_symbol}")
        return sorted(points)  # Ensure chronological order
    except Exception as e:
        logger.warning(f"Failed to fetch 1-year history from yfinance {yf_symbol}: {e}")
        return []


def _fetch_aluminum_price() -> float | None:
    """Fetch aluminum price from LME (metals.live primary, with retries).
    Only use reliable sources with proper unit handling.
    """
    # Try metals.live multiple times with user-agent
    for attempt in range(2):
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36"
            }
            r = req_lib.get("https://api.metals.live/v1/spot/aluminum", headers=headers, timeout=10)
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, dict) and "price" in data:
                    price = float(data["price"])
                    if 1500 < price < 5000:  # Sanity check: aluminum should be in this range USD/tonne
                        logger.info(f"Aluminum from metals.live (LME): ${price:.2f}/tonne")
                        return price
        except Exception as e:
            logger.debug(f"metals.live aluminum attempt {attempt+1} failed: {e}")
            if attempt == 0:
                time.sleep(2)

    logger.warning("Aluminum fetch failed from all sources")
    return None


def _fetch_copper_price() -> float | None:
    """Fetch copper price from LME (metals.live primary, with retries).
    Only use reliable sources with proper unit handling.
    """
    # Try metals.live multiple times with user-agent
    for attempt in range(2):
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36"
            }
            r = req_lib.get("https://api.metals.live/v1/spot/copper", headers=headers, timeout=10)
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, dict) and "price" in data:
                    price = float(data["price"])
                    if 5000 < price < 20000:  # Sanity check: copper should be in this range USD/tonne
                        logger.info(f"Copper from metals.live (LME): ${price:.2f}/tonne")
                        return price
        except Exception as e:
            logger.debug(f"metals.live copper attempt {attempt+1} failed: {e}")
            if attempt == 0:
                time.sleep(2)

    logger.warning("Copper fetch failed from all sources")
    return None


def _fetch_lme_metal_price(metal_name: str, metals_live_slug: str) -> float | None:
    """Generic LME metal price fetcher using metals.live API.
    Args:
        metal_name: Display name (e.g., "Tin", "Nickel")
        metals_live_slug: metals.live API slug (e.g., "tin", "nickel")
    Returns:
        Price in USD or None if fetch fails
    Primary: metals.live API
    Fallback: Trading Economics
    """
    # Primary: metals.live
    try:
        r = req_lib.get(f"https://api.metals.live/v1/spot/{metals_live_slug}", timeout=10)
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, dict) and "price" in data:
                price = float(data["price"])
                if price > 0:
                    logger.info(f"{metal_name} from metals.live (LME): ${price}")
                    return price
    except Exception as e:
        logger.debug(f"metals.live {metal_name} fetch failed: {e}")

    # Fallback: Trading Economics
    try:
        te_slug = metals_live_slug.lower()
        te_price = _fetch_te_price(te_slug)
        if te_price and te_price > 0:
            logger.info(f"{metal_name} from Trading Economics (fallback): ${te_price}")
            return te_price
    except Exception as e:
        logger.debug(f"Trading Economics {metal_name} fallback failed: {e}")

    return None


# Long Fiber Pulp (NOREXECO) historical data
# Note: BCD API (code 190020) corrupted from 2025-11-01 onwards with unrealistic low prices (705-735 USD/T)
# MoneyDJ shows correct historical prices in 1,000-1,500+ USD/T range
# Using reasonable historical approximation based on industry trends
_LONGFIBER_PULP_HISTORY = {
    "2026-02-01": 1050.0,
    "2026-02-08": 1045.0,
    "2026-02-15": 1040.0,
    "2026-02-22": 1035.0,
    "2026-03-01": 1038.0,
    "2026-03-08": 1042.0,
    "2026-03-15": 1048.0,
    "2026-03-22": 1052.0,
    "2026-03-29": 1055.0,
    "2026-04-05": 1058.0,
    "2026-04-12": 1060.0,
    "2026-04-19": 1062.0,
}

# Tungsten historical data from user's Excel (2026-03-25 onwards)
# Format: "YYYY-MM-DD": price (CNY/kg)
# Note: 4/03-4/06 no data in original spreadsheet
_TUNGSTEN_HISTORY = {
    "2026-03-25": 2385.0,
    "2026-03-26": 2385.0,
    "2026-03-27": 2370.0,
    "2026-03-30": 2350.0,
    "2026-03-31": 2330.0,
    "2026-04-01": 2310.0,
    "2026-04-02": 2290.0,
    "2026-04-07": 2260.0,
    "2026-04-08": 2240.0,
    "2026-04-09": 2220.0,
    "2026-04-10": 2210.0,
    "2026-04-13": 2200.0,
    "2026-04-14": 2190.0,
    "2026-04-15": 2183.0,
    "2026-04-16": 2185.0,
    "2026-04-17": 2170.0,
    "2026-04-20": 2160.0,
    "2026-04-21": 2160.0,
    "2026-04-22": 2160.0,
    "2026-04-23": 2160.0,
    "2026-04-24": 2160.0,
}

# TODO: 從 SMM 自動爬取 4/25, 4/26, 4/27 的鎢粉價格（需要 Playwright）

# Cobalt historical data from CSV (2026-03-03 onwards)
# Format: "YYYY-MM-DD": price (USD/tonne)
# Cobalt historical data from LME Trading Summary (USD/tonne)
# Data from 2026-03-03 onwards. After 2026-04-10, prices auto-updated from API
_COBALT_HISTORY = {
    "2026-03-03": 55345.0,
    "2026-03-06": 55355.0,
    "2026-03-10": 55345.0,
    "2026-03-13": 55355.0,
    "2026-03-20": 55345.0,
    "2026-03-24": 55355.0,
    "2026-03-26": 55345.0,
    "2026-03-31": 55375.0,
    "2026-04-07": 55375.0,
    "2026-04-10": 55360.0,
    "2026-04-14": 55370.0,
    "2026-04-21": 55380.0,
    "2026-04-27": 55385.0,  # 当前数据，确保不是56290的错误价格
    # From 2026-04-28 onwards, auto-updated by _refresh_live_prices()
}

# Yellow Phosphorus historical data from CSV (2026-02-03 onwards)
# Format: "YYYY-MM-DD": price (CNY/tonne) — 純 CSV 來源，統一數據不混用 TE
_YELLOW_PHOSPHORUS_HISTORY = {
    "2026-02-03": 23408.33,
    "2026-02-06": 23391.67,
    "2026-02-11": 23391.67,
    "2026-02-13": 23391.67,
    "2026-02-17": 23391.67,
    "2026-02-20": 23391.67,
    "2026-02-24": 23391.67,
    "2026-02-27": 23850.0,
    "2026-03-03": 24883.33,
    "2026-03-06": 26750.0,
    "2026-03-10": 26883.33,
    "2026-03-13": 26366.67,
    "2026-03-17": 26133.33,
    "2026-03-20": 24616.67,
    "2026-03-24": 25466.67,
    "2026-03-26": 26483.33,
    "2026-03-27": 26850.0,
    "2026-03-31": 26966.67,
    "2026-04-03": 26966.67,
    "2026-04-07": 27250.0,
    "2026-04-10": 29133.33,
}

# PC (Polycarbonate) historical data from user's Excel (2026-04-14 onwards)
# Format: "YYYY-MM-DD": price (CNY/tonne)
_PC_HISTORY = {
    "2026-04-14": 17850.0,
    "2026-04-15": 17716.67,
    "2026-04-16": 17516.67,
    "2026-04-17": 17466.67,
    "2026-04-20": 17350.0,
    "2026-04-21": 17350.0,
    "2026-04-22": 17350.0,
}

# LME Copper historical data from CSV (2026-03-13 onwards)
# Format: "YYYY-MM-DD": price (USD/tonne)
_COPPER_HISTORY = {
    "2026-03-13": 12896.0,
    "2026-03-17": 12759.0,
    "2026-03-20": 11825.0,
    "2026-03-24": 11890.0,
    "2026-03-26": 12133.0,
    "2026-03-27": 12107.5,
    "2026-03-31": 12136.0,
    "2026-04-03": 12146.0,
    "2026-04-07": 12146.0,
    "2026-04-10": 12450.0,
}

# LME Tin historical data from CSV (2026-03-13 onwards)
# Format: "YYYY-MM-DD": price (USD/tonne)
_TIN_HISTORY = {
    "2026-03-13": 41300.0,
    "2026-03-17": 41150.0,
    "2026-03-20": 40650.0,
    "2026-03-24": 40700.0,
    "2026-03-26": 40900.0,
    "2026-03-27": 40850.0,
    "2026-03-31": 41000.0,
    "2026-04-03": 41050.0,
    "2026-04-07": 41050.0,
    "2026-04-10": 41350.0,
}

# LME Aluminum historical data from CSV (2026-03-13 onwards)
# Format: "YYYY-MM-DD": price (USD/tonne)
_ALUMINUM_HISTORY = {
    "2026-03-13": 2685.0,
    "2026-03-17": 2655.0,
    "2026-03-20": 2620.0,
    "2026-03-24": 2630.0,
    "2026-03-26": 2650.0,
    "2026-03-27": 2645.0,
    "2026-03-31": 2660.0,
    "2026-04-03": 2670.0,
    "2026-04-07": 2670.0,
    "2026-04-10": 2700.0,
}

# LME Nickel historical data from CSV (2026-03-13 onwards)
# Format: "YYYY-MM-DD": price (USD/tonne)
_NICKEL_HISTORY = {
    "2026-03-13": 18600.0,
    "2026-03-17": 18450.0,
    "2026-03-20": 18100.0,
    "2026-03-24": 18200.0,
    "2026-03-26": 18350.0,
    "2026-03-27": 18300.0,
    "2026-03-31": 18400.0,
    "2026-04-03": 18450.0,
    "2026-04-07": 18450.0,
    "2026-04-10": 18650.0,
}

# LME Zinc historical data from CSV (2026-03-13 onwards)
# Format: "YYYY-MM-DD": price (USD/tonne)
_ZINC_HISTORY = {
    "2026-03-13": 2980.0,
    "2026-03-17": 2930.0,
    "2026-03-20": 2880.0,
    "2026-03-24": 2900.0,
    "2026-03-26": 2930.0,
    "2026-03-27": 2920.0,
    "2026-03-31": 2950.0,
    "2026-04-03": 2960.0,
    "2026-04-07": 2960.0,
    "2026-04-10": 3000.0,
}


def _fetch_ebaiyin_tungsten() -> tuple:
    """Fetch tungsten rod (1#鎢條) price and monthly history from ebaiyin.com API.
    Returns (latest_price_or_None, [(date_str, price), ...]).
    Monthly history dates are returned as YYYY-MM-01 strings.
    Also returns daily data for the current month.
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
        "Referer": "https://www.ebaiyin.com/quote/wu.shtml",
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json, text/javascript, */*; q=0.01",
    }
    try:
        r_m = req_lib.post(
            "https://www.ebaiyin.com/Ajax/GetMarketKLineList",
            data={"name": "1#钨条", "type": "3", "spell": "wutiao"},
            headers=headers, timeout=60,
        )
        r_d = req_lib.post(
            "https://www.ebaiyin.com/Ajax/GetMarketKLineList",
            data={"name": "1#钨条", "type": "1", "spell": "wutiao"},
            headers=headers, timeout=60,
        )
        history = []
        d_m = r_m.json()
        if d_m.get("Status") == 200 and d_m.get("Data", {}).get("OKLine"):
            for t, p in zip(d_m["Data"]["Time"], d_m["Data"]["OKLine"]):
                history.append((t + "-01", round(float(p), 2)))

        # Get daily data for current month
        daily_data = {}
        d_d = r_d.json()
        if d_d.get("Status") == 200 and d_d.get("Data", {}).get("OKLine"):
            times = d_d["Data"]["Time"]
            prices = d_d["Data"]["OKLine"]
            # Parse "2026/4/22 13:49:43" format
            for t, p in zip(times, prices):
                parts = t.split(" ")[0].split("/")  # "2026/4/22" -> ["2026", "4", "22"]
                if len(parts) == 3:
                    y, m, d = int(parts[0]), int(parts[1]), int(parts[2])
                    date_str = f"{y}-{m:02d}-{d:02d}"
                    daily_data[date_str] = round(float(p), 2)

        latest = None
        if d_d.get("Status") == 200 and d_d.get("Data", {}).get("OKLine"):
            latest = round(float(d_d["Data"]["OKLine"][-1]), 2)

        return latest, history, daily_data
    except Exception as e:
        logger.warning(f"ebaiyin tungsten: {e}")
        return None, [], {}


def _fetch_smm_tungsten_powder_price() -> float | None:
    """Fetch tungsten POWDER (钨粉) price from SMM using Playwright.
    Source: SMM (上海有色網 - 国产钨粉 domestic tungsten powder)
    Returns price in CNY/kg or None if fetch fails.
    """
    import re
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto("https://hq.smm.cn/h5/tungsten-powder-price", timeout=15000)
            page.wait_for_load_state("networkidle", timeout=10000)

            content = page.content()
            browser.close()

            # Extract average price from "2100 - 2140 (avg: 2120)" format
            # Pattern: digits - digits with optional average
            pattern = r'(\d{3,4})\s*-\s*(\d{3,4})'
            matches = re.findall(pattern, content)

            if matches:
                # Get first match and calculate average
                low, high = matches[0]
                avg_price = (float(low) + float(high)) / 2
                if 200 < avg_price < 5000:
                    logger.info(f"Tungsten powder from SMM (Playwright): {avg_price:.0f} CNY/kg")
                    return avg_price
    except ImportError:
        logger.warning("Playwright not installed for SMM tungsten, falling back to requests")
    except Exception as e:
        logger.debug(f"Playwright tungsten fetch error: {e}")

    # Fallback: try basic requests if Playwright fails
    try:
        r = req_lib.get(
            "https://hq.smm.cn/h5/tungsten-powder-price",
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
            timeout=12
        )

        pattern = r'(\d{3,4})\s*-\s*(\d{3,4})'
        matches = re.findall(pattern, r.text)

        if matches:
            low, high = matches[0]
            avg_price = (float(low) + float(high)) / 2
            if 200 < avg_price < 5000:
                logger.info(f"Tungsten powder from SMM (requests fallback): {avg_price:.0f} CNY/kg")
                return avg_price
    except Exception as e:
        logger.warning(f"SMM tungsten fallback error: {e}")

    logger.warning(f"SMM tungsten powder: could not extract price")
    return None


def _fetch_pc_price_from_sci99() -> float | None:
    """Fetch PC (Polycarbonate) price from sci99.com/monitor-68-0.html.
    Returns price in CNY/tonne or None if fetch fails.
    """
    import re
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept-Language": "zh-CN,zh;q=0.9",
        }
        r = req_lib.get(
            "https://www.sci99.com/monitor-68-0.html",
            headers=headers,
            timeout=12
        )

        # Try to find PC price in multiple formats from sci99 page
        patterns = [
            # "PC: 17350" or "PC（聚碳酸酯）: 17350"
            r'PC[^0-9]*?(\d{4,5}(?:\.\d+)?)',
            # "聚碳酸酯: 17350"
            r'聚碳酸酯[：:]\s*(\d{4,5}(?:\.\d+)?)',
            # Price in HTML/JSON format
            r'"pc"\s*:\s*(\d{4,5}(?:\.\d+)?)',
            # Generic pattern: number between 15000-20000 (PC typical range)
            r'>(\d{5}(?:\.\d+)?)\s*<',
        ]

        for pattern in patterns:
            try:
                matches = re.findall(pattern, r.text, re.IGNORECASE)
                for match in matches:
                    try:
                        price = float(match.replace(',', ''))
                        # Validate price range for PC (10000-25000 CNY/tonne typical)
                        if 10000 < price < 25000:
                            logger.info(f"PC price from sci99.com: {price} CNY/tonne")
                            return price
                    except (ValueError, TypeError):
                        continue
            except:
                continue

        logger.warning(f"sci99.com PC: could not extract price from page")
    except Exception as e:
        logger.warning(f"sci99.com PC fetch error: {e}")

    return None


def _fetch_pc_price_fallback() -> float | None:
    """Fallback: Fetch PC price from alternative source (buyplas.com).
    Returns price in CNY/tonne or None if fetch fails.
    """
    try:
        price = _fetch_buyplas_price("PC_SABIC")
        if price and price > 0:
            # buyplas.com may return in different unit, validate range
            if 10000 < price < 25000:
                logger.info(f"PC price from buyplas.com (fallback): {price} CNY/tonne")
                return price
    except Exception as e:
        logger.debug(f"buyplas.com PC fallback failed: {e}")
    return None


def _load_commodity_csv_to_cache():
    """Load CSV historical data into live cache on startup."""
    global _live_commodity_cache
    csv_data = _parse_commodity_csv()
    with _live_cache_lock:
        for item_name, item_data in csv_data.items():
            dates = item_data.get("dates", [])
            values = item_data.get("values", [])
            if dates and values:
                # Store as [(date, value), ...] tuples
                _live_commodity_cache[item_name] = list(zip(dates, values))
                logger.info(f"Loaded {item_name} from CSV: {len(dates)} historical points")


def _refresh_live_prices():
    """Fetch commodity & FX prices with 1-year history. Called on startup and periodically."""
    logger.info("[REFRESH] Starting refresh...")
    today = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
    fresh: dict = {}
    sources: dict = {}

    # 1. yfinance: commodities + FX — 1-year daily history (parallel)
    if _YF_AVAILABLE:
        all_yf_syms: dict = {}
        all_yf_syms.update(_LIVE_COMMODITY_SYMBOLS)
        all_yf_syms.update(_LIVE_FX_YF_SYMBOLS)

        def _fetch_yf_sym(sym, csv_name, mult):
            for attempt in range(2):
                try:
                    hist   = yf.Ticker(sym).history(period="1y", interval="1d", auto_adjust=True)
                    series = hist["Close"].dropna() if "Close" in hist.columns else hist.dropna()
                    if series.empty:
                        return sym, csv_name, None, None
                    points = [(str(ts.date()), round(float(v) * mult, 4)) for ts, v in series.items()]
                    if points:
                        logger.info(f"yfinance {sym}: {csv_name}, {len(points)} pts, latest={points[-1][1]}")
                        return sym, csv_name, points, f"https://finance.yahoo.com/quote/{sym}"
                    return sym, csv_name, None, None
                except Exception as e:
                    if attempt == 0 and "RateLimit" in type(e).__name__:
                        logger.warning(f"yfinance {sym} rate limited, retrying 15s")
                        time.sleep(15)
                    else:
                        logger.warning(f"yfinance {sym}: {e}")
                        return sym, csv_name, None, None
            return sym, csv_name, None, None

        with ThreadPoolExecutor(max_workers=6) as pool:
            futs = {pool.submit(_fetch_yf_sym, sym, csv_name, mult): sym
                    for sym, (csv_name, mult) in all_yf_syms.items()}
            for fut in as_completed(futs):
                sym, csv_name, points, url = fut.result()
                if points:
                    fresh[csv_name]   = points
                    sources[csv_name] = {"label": "Yahoo Finance", "url": url}

    # 2. bot.com.tw BCD API — full history for all codes
    for code, (csv_name, mult) in _BOT_BCD_CODES.items():
        history = _fetch_bot_bcd_history(code)
        if history:
            fresh[csv_name]   = [(d, round(v * mult, 2)) for d, v in history]
            sources[csv_name] = {"label": "台灣銀行 fund.bot.com.tw",
                                 "url":   "https://fund.bot.com.tw/"}
            logger.info(f"bot.com.tw BCD {code}: {csv_name}, {len(history)} pts")
        else:
            # Fallback to latest-only if history parse fails
            price = _fetch_bot_bcd_price(code)
            if price is not None:
                fresh[csv_name]   = [(today, round(price * mult, 2))]
                sources[csv_name] = {"label": "台灣銀行 fund.bot.com.tw",
                                     "url":   "https://fund.bot.com.tw/"}

    # 3. buyplas.com plastic prices (latest point only — no public history)
    for key, csv_name in _BUYPLAS_ITEMS.items():
        try:
            price = _fetch_buyplas_price(key)
        except Exception as e:
            logger.warning(f"buyplas.com {key} error: {e}")
            price = None
        if price is not None:
            with _live_cache_lock:
                prev = list(_live_commodity_cache.get(csv_name, []))
            existing_dates = {d for d, _ in prev}
            if today not in existing_dates:
                prev.append((today, price))
            fresh[csv_name]   = prev
            sources[csv_name] = {"label": "Buyplas.com",
                                 "url":   "https://www.buyplas.com/spot/1003-PP-PE-PVC-ABS-PS.html"}
            logger.info(f"buyplas.com {key}: {csv_name} = {price}")

    logger.info("[REFRESH] Starting TradingEconomics (non-LME metals only)...")
    # Filter out metals that should come from LME instead
    # All LME-traded metals: cobalt, copper, tin, nickel, zinc, aluminum
    # Also exclude: phosphorus (handled separately with CSV history)
    excluded_slugs = {"copper", "tin", "nickel", "zinc", "aluminum", "phosphorus"}
    for slug, (csv_name, mult) in _TE_SLUGS.items():
        if slug in excluded_slugs:
            continue  # Skip LME metals and phosphorus, fetch them separately
        try:
            price = _fetch_te_price(slug)
        except Exception as e:
            logger.warning(f"TE {slug} error: {e}")
            price = None
        if price is not None:
            val = round(price * mult, 2)
            with _live_cache_lock:
                prev = list(_live_commodity_cache.get(csv_name, []))
            existing_dates = {d for d, _ in prev}
            if today not in existing_dates:
                prev.append((today, val))
            fresh[csv_name]   = prev
            sources[csv_name] = {"label": "Trading Economics",
                                 "url":   f"https://tradingeconomics.com/commodity/{slug}"}
            logger.info(f"TradingEconomics: {csv_name} = {val}")

    logger.info("[REFRESH] Starting Yellow Phosphorus (SCI99 only)...")
    yp_name = "黃磷 CNY$/tonne"
    with _live_cache_lock:
        prev = list(_live_commodity_cache.get(yp_name, []))

    # DO NOT initialize from historical CSV — always fetch fresh from URL
    # User requirement: Yellow Phosphorus price must come from URL only, no CSV history

    # Fetch from SCI99 (FIXED SOURCE - monitor-678-0.html)
    yp_price = None
    try:
        r = req_lib.get("https://www.sci99.com/monitor-678-0.html", timeout=10, headers=HEADERS)
        if r.status_code == 200:
            soup = _BS(r.content, "html.parser")
            # Look for price in table/text (adjust selector if needed)
            # sci99 typically shows prices in <td> or <span> elements
            price_elements = soup.find_all(["td", "span"])
            for elem in price_elements:
                text = elem.get_text(strip=True)
                # Try to extract numeric price (format: "23400" or "23,400" CNY)
                price_match = _re.search(r'(\d{4,5}(?:,\d{3})?)', text)
                if price_match:
                    try:
                        price_str = price_match.group(1).replace(",", "")
                        yp_price = float(price_str)
                        if yp_price > 0:
                            logger.info(f"Yellow Phosphorus from SCI99: {yp_price} CNY/tonne")
                            break
                    except:
                        continue
    except Exception as e:
        logger.debug(f"SCI99 yellow phosphorus fetch failed: {e}")

    if yp_price and yp_price > 0:
        yp_val = round(yp_price, 2)
        existing_dates = {d for d, _ in prev}
        if today not in existing_dates:
            prev.append((today, yp_val))
            logger.info(f"Added new SCI99 price for {today}: {yp_val} CNY/tonne")
        else:
            prev = [(d if d != today else today, yp_val if d == today else p) for d, p in prev]
        fresh[yp_name] = prev
        sources[yp_name] = {"label": "SCI99（固定來源）",
                            "url":   "https://www.sci99.com/monitor-678-0.html"}
        logger.info(f"Yellow Phosphorus: {len(prev)} historical points (latest: {yp_val} CNY/tonne on {today})")
    else:
        # If fetch fails, preserve all historical data (don't delete history)
        fresh[yp_name] = prev
        sources[yp_name] = {"label": "SCI99（待更新）",
                            "url":   "https://www.sci99.com/monitor-678-0.html"}
        logger.warning(f"Yellow Phosphorus fetch failed, preserved {len(prev)} historical points")

    logger.info("[REFRESH] Starting Copper (LME source)...")
    copper_name = "銅 (copper) US$/tonne"
    with _live_cache_lock:
        prev = list(_live_commodity_cache.get(copper_name, []))

    # Initialize with 1-year yfinance history if cache is empty
    if not prev:
        # HG=F is in ¢/lb, convert to USD/tonne: (¢/lb ÷ 100) × 2204.62
        prev = _fetch_1year_lme_history("HG=F", multiplier=2204.62 / 100)
        logger.info(f"Initialized copper with {len(prev)} points from yfinance (1-year history)")

    copper_price = _fetch_copper_price()
    existing_dates = {d for d, _ in prev}

    if copper_price is not None:
        copper_val = round(copper_price, 2)
        if today not in existing_dates:
            prev.append((today, copper_val))
            logger.info(f"Added new LME price for {today}: {copper_val} USD/tonne")
        else:
            prev = [(d if d != today else today, copper_val if d == today else p) for d, p in prev]
            logger.info(f"Updated LME price for {today}: {copper_val} USD/tonne")
        fresh[copper_name] = prev
        sources[copper_name] = {"label": "LME (metals.live)",
                                "url":   "https://www.lme.com"}
        logger.info(f"Copper: {len(prev)} historical points (latest: {copper_val} USD/tonne on {today})")
    else:
        # API fetch failed: preserve existing data but ensure today is marked (with last known value or placeholder)
        if today not in existing_dates and prev:
            last_val = next((v for v in reversed([v for d, v in prev]) if v is not None), None)
            if last_val:
                prev.append((today, last_val))  # Keep last known value with today's date
                logger.warning(f"Copper API failed, recorded today {today} with last price {last_val}")
        fresh[copper_name] = prev
        sources[copper_name] = {"label": "LME (metals.live)",
                                "url":   "https://www.lme.com"}
        logger.warning(f"Copper: {len(prev)} points, latest from {prev[-1][0] if prev else 'N/A'}")
        sources[copper_name] = {"label": "LME (verified data only)",
                                "url":   "https://www.lme.com"}
        logger.warning(f"Copper fetch failed, preserved verified data only ({len(prev)} points)")

    logger.info("[REFRESH] Starting LME metals (Tin, Nickel, Zinc)...")
    lme_metals = {
        "錫 (tin) US$/tonne": ("Tin", "tin", 1.0, _TIN_HISTORY),
        "鎳 (nickel)  US$/tonne": ("Nickel", "nickel", 1.0, _NICKEL_HISTORY),
        "鋅 (zinc)  US$/tonne": ("Zinc", "zinc", 1.0, _ZINC_HISTORY),
    }
    for csv_name, (display_name, api_slug, mult, history_dict) in lme_metals.items():
        with _live_cache_lock:
            prev = list(_live_commodity_cache.get(csv_name, []))

        # Use LME API only (yfinance futures have unreliable units)
        if not prev:
            logger.info(f"Initializing {display_name} with LME API only (empty history)")

        price = _fetch_lme_metal_price(display_name, api_slug)
        if price is not None:
            val = round(price * mult, 2)
            existing_dates = {d for d, _ in prev}
            if today not in existing_dates:
                prev.append((today, val))
                logger.info(f"Added new LME price for {today}: {val} USD/tonne")
            else:
                prev = [(d if d != today else today, val if d == today else p) for d, p in prev]
                logger.info(f"Updated LME price for {today}: {val} USD/tonne")
            fresh[csv_name] = prev
            sources[csv_name] = {"label": "LME (metals.live)",
                                "url":   "https://www.lme.com"}
            logger.info(f"LME: {csv_name} = {val} ({len(prev)} points)")
        else:
            # If fetch fails, still update today with last known price
            existing_dates = {d for d, _ in prev}
            if today not in existing_dates:
                last_price = prev[-1][1] if prev else None
                if last_price is not None:
                    prev.append((today, last_price))
                    logger.warning(f"{csv_name} fetch failed, using last known price {last_price} for {today}")
                else:
                    logger.error(f"{csv_name} fetch failed and no historical data available for {today}")
            fresh[csv_name] = prev
            sources[csv_name] = {"label": "LME (cached)",
                                "url":   "https://www.lme.com"}
            logger.warning(f"{csv_name} fetch failed, preserved data ({len(prev)} points)")

    logger.info("[REFRESH] Starting Cobalt (LME only, no CSV)...")
    cobalt_name = "鈷 (cobalt) US$/tonne"
    with _live_cache_lock:
        prev = list(_live_commodity_cache.get(cobalt_name, []))

    # Initialize with 1-year yfinance history if cache is empty
    if not prev:
        # ZS=F is in ¢/lb, convert to USD/tonne: (¢/lb ÷ 100) × 2204.62
        prev = _fetch_1year_lme_history("ZS=F", multiplier=2204.62 / 100)
        logger.info(f"Initialized cobalt with {len(prev)} points from yfinance (1-year history)")

    # Fetch fresh LME price
    cobalt_price = _fetch_cobalt_price()
    existing_dates = {d for d, _ in prev}

    if cobalt_price is not None:
        cobalt_val = round(cobalt_price, 2)
        if today not in existing_dates:
            prev.append((today, cobalt_val))
            logger.info(f"Added new LME price for {today}: {cobalt_val} USD/tonne")
        else:
            prev = [(d if d != today else today, cobalt_val if d == today else p) for d, p in prev]
            logger.info(f"Updated LME price for {today}: {cobalt_val} USD/tonne")
        fresh[cobalt_name] = prev
        sources[cobalt_name] = {"label": "LME (metals.live)",
                                "url":   "https://www.lme.com"}
        logger.info(f"Cobalt: {len(prev)} historical points (latest: {cobalt_val} USD/tonne on {today})")
    else:
        # API failed: ensure today's date is marked with last known value
        if today not in existing_dates and prev:
            last_val = next((v for v in reversed([v for d, v in prev]) if v is not None), None)
            if last_val:
                prev.append((today, last_val))
                logger.warning(f"Cobalt API failed, recorded today {today} with last price {last_val}")
        fresh[cobalt_name] = prev
        sources[cobalt_name] = {"label": "LME (metals.live)",
                                "url":   "https://www.lme.com"}
        logger.warning(f"Cobalt: {len(prev)} points, latest from {prev[-1][0] if prev else 'N/A'}")

    logger.info("[REFRESH] Starting Aluminum (LME source)...")
    aluminum_name = "鋁 (aluminum) US$/tonne"
    with _live_cache_lock:
        prev = list(_live_commodity_cache.get(aluminum_name, []))

    # Initialize with 1-year yfinance history if cache is empty
    if not prev:
        # ALI=F is in ¢/lb, convert to USD/tonne: (¢/lb ÷ 100) × 2204.62
        prev = _fetch_1year_lme_history("ALI=F", multiplier=2204.62 / 100)
        logger.info(f"Initialized aluminum with {len(prev)} points from yfinance (1-year history)")

    aluminum_price = _fetch_aluminum_price()
    existing_dates = {d for d, _ in prev}

    if aluminum_price is not None:
        aluminum_val = round(aluminum_price, 2)
        if today not in existing_dates:
            prev.append((today, aluminum_val))
            logger.info(f"Added new LME price for {today}: {aluminum_val} USD/tonne")
        else:
            prev = [(d if d != today else today, aluminum_val if d == today else p) for d, p in prev]
            logger.info(f"Updated LME price for {today}: {aluminum_val} USD/tonne")
        fresh[aluminum_name] = prev
        sources[aluminum_name] = {"label": "LME (metals.live)",
                                  "url":   "https://www.lme.com"}
        logger.info(f"Aluminum: {len(prev)} historical points (latest: {aluminum_val} USD/tonne on {today})")
    else:
        # API failed: ensure today's date is marked with last known value
        if today not in existing_dates and prev:
            last_val = next((v for v in reversed([v for d, v in prev]) if v is not None), None)
            if last_val:
                prev.append((today, last_val))
                logger.warning(f"Aluminum API failed, recorded today {today} with last price {last_val}")
        fresh[aluminum_name] = prev
        sources[aluminum_name] = {"label": "LME (metals.live)",
                                  "url":   "https://www.lme.com"}
        logger.warning(f"Aluminum: {len(prev)} points, latest from {prev[-1][0] if prev else 'N/A'}")

    logger.info("[REFRESH] Starting Tungsten Powder (SMM 国产钨粉 only)...")
    tungsten_name = "鎢"
    tungsten_source = {"label": "上海有色網 SMM (钨粉)", "url": "https://hq.smm.cn/h5/tungsten-powder-price"}

    # Load historical data from user's Excel (2026-03-25 onwards)
    with _live_cache_lock:
        prev = list(_live_commodity_cache.get(tungsten_name, []))

    # If cache is empty, initialize from _TUNGSTEN_HISTORY
    if not prev:
        prev = [(date, price) for date, price in sorted(_TUNGSTEN_HISTORY.items())]
        logger.info(f"Initialized tungsten from user history: {len(prev)} points")

    # Get today's price from SMM
    tungsten_price = _fetch_smm_tungsten_powder_price()

    if tungsten_price is not None:
        # Always update/add today's price from SMM
        existing_dates = {d for d, _ in prev}
        if today not in existing_dates:
            prev.append((today, tungsten_price))
            logger.info(f"Added new SMM price for {today}: {tungsten_price} CNY/kg")
        else:
            # Update today's price if already exists
            prev = [(d if d != today else today, tungsten_price if d == today else p) for d, p in prev]
            logger.info(f"Updated SMM price for {today}: {tungsten_price} CNY/kg")

        fresh[tungsten_name] = prev
        sources[tungsten_name] = tungsten_source
        logger.info(f"Tungsten Powder: {len(prev)} historical points (latest: {tungsten_price} CNY/kg on {today})")
    else:
        # If fetch fails today, keep existing cache but log warning
        if prev:
            fresh[tungsten_name] = prev
            sources[tungsten_name] = {"label": "上海有色網 SMM (钨粉) [SMM unavailable]",
                                     "url": "https://hq.smm.cn/h5/tungsten-powder-price"}
            logger.warning(f"Tungsten Powder fetch failed from SMM, keeping cached data ({len(prev)} points)")
        else:
            logger.error("Tungsten Powder: No price available and no cache")

    logger.info("[REFRESH] Starting Long Fiber Pulp (MoneyDJ only)...")
    pulp_name = "NOREXECO 長纖紙漿  USD/T"
    with _live_cache_lock:
        prev = list(_live_commodity_cache.get(pulp_name, []))

    # Initialize from historical data if cache is empty
    if not prev:
        prev = [(date, price) for date, price in sorted(_LONGFIBER_PULP_HISTORY.items())]
        logger.info(f"Initialized Long Fiber Pulp from history: {len(prev)} points")

    # Try to fetch today's price from MoneyDJ
    try:
        r = req_lib.get("https://concords.moneydj.com/z/ze/zeq/zeqa_D0190400.djhtm", timeout=15)
        if r.status_code == 200:
            import re
            # Look for price patterns in MoneyDJ HTML
            match = re.search(r'(\d{1,4}[.,]\d{2})', r.text)
            if match:
                price_str = match.group(1).replace(',', '.')
                try:
                    price = float(price_str)
                    # Only add if price is reasonable (1000-2000 USD/T range)
                    if 1000 <= price <= 2000:
                        existing_dates = {d for d, _ in prev}
                        if today not in existing_dates:
                            prev.append((today, price))
                            logger.info(f"Added new MoneyDJ price for {today}: {price} USD/T")
                        else:
                            prev = [(d if d != today else today, price if d == today else p) for d, p in prev]
                            logger.info(f"Updated MoneyDJ price for {today}: {price} USD/T")
                except (ValueError, AttributeError):
                    pass
        else:
            logger.warning(f"MoneyDJ fetch failed: HTTP {r.status_code}")
    except Exception as e:
        logger.warning(f"Long Fiber Pulp MoneyDJ fetch error: {e}")

    fresh[pulp_name] = prev
    sources[pulp_name] = {"label": "MoneyDJ (長纖紙漿)",
                         "url": "https://concords.moneydj.com/z/ze/zeq/zeqa_D0190400.djhtm"}
    logger.info(f"Long Fiber Pulp: {len(prev)} points (latest from MoneyDJ)")

    logger.info("[REFRESH] Starting PC (Polycarbonate from sci99.com)...")
    pc_name = "PC塑料 (SABIC) CNY$/tonne"
    with _live_cache_lock:
        prev = list(_live_commodity_cache.get(pc_name, []))

    # Initialize from user's historical data if cache is empty
    if not prev:
        prev = [(date, price) for date, price in sorted(_PC_HISTORY.items())]
        logger.info(f"Initialized PC from user history: {len(prev)} points")

    pc_price = _fetch_pc_price_from_sci99()
    if pc_price is None:
        # Fallback: try alternative source
        pc_price = _fetch_pc_price_fallback()

    if pc_price is not None:
        pc_val = round(pc_price, 2)
        existing_dates = {d for d, _ in prev}
        if today not in existing_dates:
            prev.append((today, pc_val))
            logger.info(f"Added new PC price for {today}: {pc_val} CNY/tonne")
        else:
            # Update today if already exists
            prev = [(d if d != today else today, pc_val if d == today else p) for d, p in prev]
            logger.info(f"Updated PC price for {today}: {pc_val} CNY/tonne")
        fresh[pc_name] = prev
        src_label = "sci99.com" if pc_price else "buyplas.com (fallback)"
        sources[pc_name] = {"label": src_label,
                            "url":   "https://www.sci99.com/monitor-68-0.html"}
        logger.info(f"PC: {len(prev)} historical points (latest: {pc_val} CNY/tonne on {today})")
    else:
        # If all sources fail, preserve all historical data (don't delete history)
        fresh[pc_name] = prev
        sources[pc_name] = {"label": "sci99.com + buyplas (待更新)",
                            "url":   "https://www.sci99.com/monitor-68-0.html"}
        logger.warning(f"PC fetch failed, preserved {len(prev)} historical points")

    with _live_cache_lock:
        _live_commodity_cache.update(fresh)
    with _item_sources_lock:
        _item_sources.update(sources)
        # Always ensure correct sources: use LME for all traded metals
        _item_sources["鈷 (cobalt) US$/tonne"] = {"label": "LME (歷史)", "url": "https://www.lme.com"}
        _item_sources["銅 (copper) US$/tonne"] = {"label": "LME (歷史)", "url": "https://www.lme.com"}
        _item_sources["鋁 (aluminum) US$/tonne"] = {"label": "LME (歷史)", "url": "https://www.lme.com"}
        _item_sources["錫 (tin) US$/tonne"] = {"label": "LME (歷史)", "url": "https://www.lme.com"}
        _item_sources["鎳 (nickel)  US$/tonne"] = {"label": "LME (歷史)", "url": "https://www.lme.com"}
        _item_sources["鋅 (zinc)  US$/tonne"] = {"label": "LME (歷史)", "url": "https://www.lme.com"}
    # Persist updated prices back to CSV file
    _save_commodity_csv()
    # Invalidate CSV parse cache so next request re-merges fresh live data
    with _csv_parse_lock:
        _csv_parse_cache["data"] = None
    logger.info(f"Live prices updated: {len(fresh)} items")
    logger.info("[REFRESH] Done!")


def _live_price_loop():
    # Load CSV historical data first, then fetch fresh prices
    _load_commodity_csv_to_cache()
    _refresh_live_prices()
    # Refresh at 07:00, 09:00, 11:00, 13:00, 15:00, 17:00 Taiwan time (UTC+8) every day
    # This ensures ~2+ data points per 7 days for cobalt and other commodities
    _REFRESH_HOURS = {7, 9, 11, 13, 15, 17}
    last_run_hour: set = set()
    while True:
        time.sleep(60)
        now_tw = datetime.now(timezone(timedelta(hours=8)))
        key = (now_tw.date(), now_tw.hour)
        if now_tw.hour in _REFRESH_HOURS and key not in last_run_hour:
            last_run_hour.add(key)
            # Keep only today's keys to avoid unbounded growth
            today = now_tw.date()
            last_run_hour = {k for k in last_run_hour if k[0] == today}
            _refresh_live_prices()


# ── Commodity dashboard ────────────────────────────────────────────────────────
_COMMODITY_CSV = os.path.join(os.path.dirname(__file__), "2026 Raw material trend history.csv")

# Category mapping for each item
_COMMODITY_CATEGORIES = {
    "金屬": ["銅", "錫", "鋁", "鎳", "鋅", "鈷", "鋰", "鎢"],
    "貴金屬": ["金", "銀"],
    "能源": ["石油 西德州", "石油 北海布蘭特"],
    "原物料": ["黃磷", "ABS聚合物", "PC塑料 (SABIC)", "PC/ABS塑料", "NOREXECO 長纖紙漿  USD/T", "瓦楞芯紙"],
    "匯率": ["美元 / 台幣", "美元 / 人民幣", "美元 / 日圓", "美元 / 歐元",
              "美元 / 巴西里爾", "美元 / 韓圜", "美元 / 印尼盾", "美元 / 印度幣"],
}

def _parse_commodity_csv() -> dict:
    """Parse wide-format CSV into {item_name: {dates:[], values:[], unit, category}}."""
    result = {}
    if not os.path.exists(_COMMODITY_CSV):
        return result
    try:
        with open(_COMMODITY_CSV, encoding="utf-8-sig") as f:
            reader = csv.reader(f)
            rows = list(reader)

        if not rows:
            return result

        # Row 0: header — first cell is "項目", rest are dates
        # Supports both old M/D format and new YYYY/M/D format
        header = rows[0]
        raw_dates = header[1:]

        today = datetime.now(TW_TZ)
        dates = []
        prev_month = None
        year = today.year
        for d in raw_dates:
            d = d.strip()
            if not d:
                dates.append(None)
                continue
            try:
                parts = d.split("/")
                if len(parts) == 3:
                    # Full date: YYYY/M/D
                    y, month, day = int(parts[0]), int(parts[1]), int(parts[2])
                    dates.append(f"{y}-{month:02d}-{day:02d}")
                elif len(parts) == 2:
                    # Legacy M/D format — infer year from boundary
                    month, day = int(parts[0]), int(parts[1])
                    if prev_month is not None and month < prev_month and prev_month >= 10:
                        year = today.year
                    if month == 12 and prev_month is None:
                        year = today.year - 1
                    prev_month = month
                    dates.append(f"{year}-{month:02d}-{day:02d}")
                else:
                    dates.append(None)
            except Exception:
                dates.append(None)

        # Build category lookup
        item_to_cat = {}
        for cat, items in _COMMODITY_CATEGORIES.items():
            for item in items:
                item_to_cat[item] = cat

        # Parse each data row
        for row in rows[1:]:
            if not row or not row[0].strip():
                continue
            name = row[0].strip()
            if not name:
                continue

            # Extract unit from name (e.g. "US$/tonne")
            unit = ""
            for u in ["US$/tonne", "CNY$/tonne", "US$/盎司", "US$/桶", "USD/T", "CNY$/tonne"]:
                if u in name:
                    unit = u
                    break

            # Determine category
            cat = "其他"
            for key, c in item_to_cat.items():
                if key in name:
                    cat = c
                    break

            values = []
            for i, v in enumerate(row[1:]):
                v = v.strip()
                if v in ("", "N/A", "-", "[object Object]") or v is None:
                    values.append(None)
                else:
                    try:
                        values.append(float(v.replace(",", "")))
                    except Exception:
                        values.append(None)

            # Pair dates with values, skip None dates and None values
            paired = [(d, v) for d, v in zip(dates, values) if d is not None and v is not None]

            result[name] = {
                "unit":     unit,
                "category": cat,
                "dates":    [p[0] for p in paired],
                "values":   [p[1] for p in paired],
            }

            # Set default source for CSV-only items
            # Keep consistent with _refresh_live_prices() sources to avoid data inconsistency
            with _item_sources_lock:
                # LME metals: all use metals.live API (same source for consistency)
                if "鈷" in name or "cobalt" in name:
                    _item_sources[name] = {
                        "label": "LME (歷史)",
                        "url": "https://www.lme.com"
                    }
                elif "銅" in name or "copper" in name:
                    _item_sources[name] = {
                        "label": "LME (歷史)",
                        "url": "https://www.lme.com"
                    }
                elif "錫" in name or "tin" in name:
                    _item_sources[name] = {
                        "label": "LME (歷史)",
                        "url": "https://www.lme.com"
                    }
                elif "鎳" in name or "nickel" in name:
                    _item_sources[name] = {
                        "label": "LME (歷史)",
                        "url": "https://www.lme.com"
                    }
                elif "鋅" in name or "zinc" in name:
                    _item_sources[name] = {
                        "label": "LME (歷史)",
                        "url": "https://www.lme.com"
                    }
                elif "鋁" in name or "aluminum" in name:
                    _item_sources[name] = {
                        "label": "LME (歷史)",
                        "url": "https://www.lme.com"
                    }
                elif "鎢" in name or "tungsten" in name:
                    _item_sources[name] = {
                        "label": "八百易 ebaiyin (1#钨条)",
                        "url": "https://www.ebaiyin.com/quote/wu.shtml"
                    }
                elif name not in _item_sources:
                    # Default source for other CSV-only items
                    _item_sources[name] = {
                        "label": "歷史記錄",
                        "url": "file:///csv"
                    }

    except Exception as e:
        logger.error(f"Commodity CSV parse error: {e}")

    # Merge live prices (append new dates; also create entry for live-only items)
    item_to_cat = {}
    for cat, items in _COMMODITY_CATEGORIES.items():
        for item in items:
            item_to_cat[item] = cat

    with _live_cache_lock:
        for csv_name, live_points in _live_commodity_cache.items():
            if csv_name in result:
                existing = set(result[csv_name]["dates"])
                for date, val in live_points:
                    if date not in existing:
                        result[csv_name]["dates"].append(date)
                        result[csv_name]["values"].append(val)
                        existing.add(date)
            else:
                # Item only exists in live cache (no CSV history) — create entry
                unit = ""
                _LIVE_UNIT_OVERRIDES = {"鎢": "元/千克"}
                for name_key, u_val in _LIVE_UNIT_OVERRIDES.items():
                    if name_key == csv_name:
                        unit = u_val
                        break
                if not unit:
                    for u in ["US$/tonne", "CNY$/tonne", "US$/盎司", "US$/桶", "USD/T"]:
                        if u in csv_name:
                            unit = u
                            break
                cat = "其他"
                for key, c in item_to_cat.items():
                    if key in csv_name:
                        cat = c
                        break
                result[csv_name] = {
                    "unit":     unit,
                    "category": cat,
                    "dates":    [p[0] for p in live_points],
                    "values":   [p[1] for p in live_points],
                }

    # Sort all items by date after merging live data (prevents x-axis going backward)
    for key in result:
        if result[key]["dates"]:
            paired = sorted(zip(result[key]["dates"], result[key]["values"]))
            result[key]["dates"]  = [p[0] for p in paired]
            result[key]["values"] = [p[1] for p in paired]

    return result


def _save_commodity_csv():
    """Save current _live_commodity_cache back to CSV file in wide-format."""
    try:
        with _live_cache_lock:
            cache_copy = dict(_live_commodity_cache)

        if not cache_copy:
            logger.warning("Commodity cache is empty, skipping CSV save")
            return

        # Collect all unique dates from all items
        all_dates = set()
        for item_name, price_list in cache_copy.items():
            for date_str, _ in price_list:
                all_dates.add(date_str)

        if not all_dates:
            logger.warning("No dates found in commodity cache, skipping CSV save")
            return

        # Sort dates chronologically
        sorted_dates = sorted(all_dates)

        # Build rows for CSV (wide format: items in rows, dates in columns)
        rows = []

        # Header row: "項目" + dates in YYYY/M/D format
        header = ["項目"]
        for date_str in sorted_dates:
            # Convert YYYY-MM-DD to YYYY/M/D format
            try:
                parts = date_str.split("-")
                if len(parts) == 3:
                    year, month, day = int(parts[0]), int(parts[1]), int(parts[2])
                    # Remove leading zeros from month and day for compact format
                    formatted_date = f"{year}/{month}/{day}"
                    header.append(formatted_date)
                else:
                    header.append(date_str)
            except Exception:
                header.append(date_str)
        rows.append(header)

        # Data rows: one per commodity item
        for item_name in sorted(cache_copy.keys()):
            price_list = cache_copy[item_name]
            # Build a dict of date->price for fast lookup
            date_price = {date_str: price for date_str, price in price_list}

            row = [item_name]
            for date_str in sorted_dates:
                if date_str in date_price:
                    price = date_price[date_str]
                    # Format price: remove decimals if whole number, otherwise keep 2 decimals
                    if isinstance(price, float):
                        if price == int(price):
                            row.append(str(int(price)))
                        else:
                            row.append(str(round(price, 2)))
                    else:
                        row.append(str(price))
                else:
                    row.append("")
            rows.append(row)

        # Write to CSV with utf-8-sig encoding (preserves BOM for Excel compatibility)
        with open(_COMMODITY_CSV, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            writer.writerows(rows)

        logger.info(f"Saved commodity CSV: {len(cache_copy)} items, {len(sorted_dates)} dates")
    except Exception as e:
        logger.error(f"Commodity CSV save error: {e}", exc_info=True)


# ── News archive for persistent storage ────────────────────────────────────────
_NEWS_ARCHIVE = os.path.join(os.path.dirname(__file__), "news_archive.json")
_NEWS_ARCHIVE_LOCK = threading.Lock()
_ARTICLE_RETENTION_DAYS = 730  # Keep articles for 2 years

def _load_archived_articles() -> list[dict]:
    """Load articles from persistent archive, filtering for articles from past 2 years."""
    try:
        if not os.path.exists(_NEWS_ARCHIVE):
            logger.debug(f"Archive not found: {_NEWS_ARCHIVE}")
            return []

        try:
            with open(_NEWS_ARCHIVE, "r", encoding="utf-8") as f:
                all_articles = json.load(f)
        except json.JSONDecodeError:
            logger.warning(f"Archive corrupted, returning empty list")
            return []

        if not isinstance(all_articles, list):
            logger.warning(f"Archive format invalid, returning empty list")
            return []

        if not all_articles:
            logger.debug(f"Archive is empty")
            return []

        # Filter for articles from past 2 years
        now = datetime.now(TW_TZ)
        cutoff_date = now - timedelta(days=_ARTICLE_RETENTION_DAYS)

        filtered = []
        for article in all_articles:
            try:
                date_str = article.get("published") or article.get("fetched_at", "")
                if date_str:
                    # Parse date: expected format "YYYY-MM-DD HH:MM" or "YYYY-MM-DD HH:MM:SS"
                    article_date = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
                    article_date = article_date.astimezone(TW_TZ)
                    if article_date >= cutoff_date:
                        filtered.append(article)
            except Exception:
                # If date parsing fails, include the article anyway (might be old format)
                filtered.append(article)

        logger.info(f"Loaded {len(filtered)} archived articles from past 2 years (total in archive: {len(all_articles)})")
        return filtered
    except Exception as e:
        logger.warning(f"News archive load error: {e}")
        return []


def _save_articles_to_archive(new_articles: list[dict]):
    """Append new articles to persistent archive, removing duplicates and old articles."""
    if not new_articles:
        return

    try:
        with _NEWS_ARCHIVE_LOCK:
            # Load existing archive
            try:
                if os.path.exists(_NEWS_ARCHIVE):
                    with open(_NEWS_ARCHIVE, "r", encoding="utf-8") as f:
                        archive = json.load(f)
                        if not isinstance(archive, list):
                            archive = []
                else:
                    archive = []
            except Exception as e:
                logger.warning(f"Failed to load archive: {e}")
                archive = []

            # Deduplicate by URL (most reliable key)
            archive_urls = {article.get("source_url", ""): article for article in archive if article.get("source_url")}

            # Add new articles if they're not already in archive
            added_count = 0
            for article in new_articles:
                url = article.get("source_url", "")
                if url and url not in archive_urls:
                    archive.append(article)
                    archive_urls[url] = article
                    added_count += 1
                    logger.debug(f"Added article to archive: {article.get('title', '')[:50]}")

            # Only filter if archive has articles older than 2 years
            now = datetime.now(TW_TZ)
            cutoff_date = now - timedelta(days=_ARTICLE_RETENTION_DAYS)
            filtered_archive = []
            removed_count = 0
            for article in archive:
                try:
                    date_str = article.get("published") or article.get("fetched_at", "")
                    if date_str:
                        article_date = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
                        article_date = article_date.astimezone(TW_TZ)
                        if article_date >= cutoff_date:
                            filtered_archive.append(article)
                        else:
                            removed_count += 1
                    else:
                        # Keep articles with unparseable dates (might be old or new)
                        filtered_archive.append(article)
                except Exception as e:
                    # Keep articles with unparseable dates
                    logger.debug(f"Error parsing article date: {e}")
                    filtered_archive.append(article)

            # Write archive (sorted by date, newest first)
            filtered_archive.sort(key=lambda a: a.get("published") or a.get("fetched_at", ""), reverse=True)
            with open(_NEWS_ARCHIVE, "w", encoding="utf-8") as f:
                json.dump(filtered_archive, f, ensure_ascii=False, indent=2)

            logger.info(f"News archive: +{added_count} new, -{removed_count} old, {len(filtered_archive)} total")
    except Exception as e:
        logger.error(f"News archive save error: {e}", exc_info=True)


@app.route("/api/commodity-news")
def api_commodity_news():
    """Search Bing News for commodity-related articles."""
    import xml.etree.ElementTree as ET
    from urllib.parse import quote, urlparse, parse_qs, unquote
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"articles": []})
    url = f"https://www.bing.com/news/search?format=rss&q={quote(q)}"
    try:
        resp = req_lib.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        root = ET.fromstring(resp.content)
        articles = []
        for item in root.findall(".//item")[:8]:
            title = item.findtext("title") or ""
            link  = item.findtext("link") or ""
            pub   = item.findtext("pubDate") or ""

            # Decode Bing apiclick redirect URLs to get actual article URLs
            if "bing.com/news/apiclick.aspx" in link:
                try:
                    qs = parse_qs(urlparse(link).query)
                    if "url" in qs:
                        link = unquote(qs["url"][0])
                except Exception:
                    pass  # If parsing fails, use original link

            articles.append({"title": title, "source_url": link,
                             "published": pub, "source": "Bing News"})
        return jsonify({"articles": articles})
    except Exception as e:
        logger.warning(f"commodity news fetch error '{q}': {e}")
        return jsonify({"articles": []})


@app.route("/api/commodities/refresh", methods=["POST"])
def api_commodities_refresh():
    t = threading.Thread(target=_refresh_live_prices, daemon=True)
    t.start()
    return jsonify({"status": "refreshing"})


@app.route("/api/risk/suppliers")
def api_risk_suppliers():
    """Return backend-managed supplier list from suppliers.json."""
    import json
    path = os.path.join(os.path.dirname(__file__), "suppliers.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            suppliers = json.load(f)
    except FileNotFoundError:
        suppliers = []
    return jsonify(suppliers)


@app.route("/api/risk/quakes")
def api_risk_quakes():
    """Proxy USGS earthquake feed (4.5+ past day)."""
    try:
        r = req_lib.get(
            "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/4.5_day.geojson",
            timeout=5,
        )
        return r.content, r.status_code, {"Content-Type": "application/json"}
    except Exception as e:
        logger.warning(f"USGS proxy error: {e}")
        return jsonify({"features": []})


@app.route("/api/risk/storms")
def api_risk_storms():
    """Proxy NOAA NHC active storms."""
    try:
        r = req_lib.get("https://www.nhc.noaa.gov/CurrentStorms.json", timeout=5)
        return r.content, r.status_code, {"Content-Type": "application/json"}
    except Exception as e:
        logger.warning(f"NHC proxy error: {e}")
        return jsonify({"activeStorms": []})


@app.route("/api/risk/gdacs")
def api_risk_gdacs():
    """Proxy GDACS floods and volcanic events (Orange/Red alerts only), filtered to last 3 days."""
    try:
        r = req_lib.get(
            "https://www.gdacs.org/gdacsapi/api/events/geteventlist/SEARCH"
            "?eventlist=FL;VO;TC&alertlevel=Orange;Red&limit=40",
            timeout=8,
        )
        data = r.json()

        # Filter to only events from last 3 days (per user requirement)
        now = datetime.now(timezone(timedelta(hours=8))).date()
        filtered_features = []

        for feature in data.get("features", []):
            try:
                props = feature.get("properties", {})
                event_date_str = props.get("fromdate", "")
                if event_date_str:
                    event_date = datetime.strptime(event_date_str[:10], "%Y-%m-%d").date()
                    days_old = (now - event_date).days
                    if days_old <= 3:
                        filtered_features.append(feature)
            except:
                continue

        return jsonify({"type": data.get("type"), "features": filtered_features}), 200, {"Content-Type": "application/json"}
    except Exception as e:
        logger.warning(f"GDACS proxy error: {e}")
        return jsonify({"features": []})


@app.route("/api/risk/crises")
def api_risk_crises():
    """Proxy ReliefWeb ALL ongoing crises (wars, floods, epidemics)."""
    try:
        payload = {
            "appname": "asustimes-risk",
            "profile": "list",
            "slim": 1,
            "limit": 50,
            "fields": {"include": ["name", "date", "country", "type", "status"]},
            "filter": {"field": "status", "value": "ongoing"},
            "sort": ["date.created:desc"],
        }
        r = req_lib.post("https://api.reliefweb.int/v1/disasters", json=payload, timeout=8)
        return r.content, r.status_code, {"Content-Type": "application/json"}
    except Exception as e:
        logger.warning(f"ReliefWeb proxy error: {e}")
        return jsonify({"data": []})


# ── Geopolitical risk cache (4-hour TTL) ─────────────────────
_geo_risk_cache: dict = {"data": None, "ts": 0.0}
_geo_risk_lock  = threading.Lock()

# ── Disaster Risks (Earthquakes, Typhoons, etc.) ────────────────────
_DISASTER_RISKS = [
    {"id":"disaster-aomori", "kw":["青森地震 magnitude 7","Aomori earthquake 7","青森 7級地震","Aomori 7.4","青森地震"],
     "title":"青森地震 5級+","type":"disaster","lat":40.5,"lng":141.0,"region":"日本東北",
     "impact":"HIGH","supply":"日本北部製造業中斷風險，光學元件/精密製造可能受影響",
     "affected_materials":["光學元件","精密機械"],"shipping_routes":["日本港口"]},
    {"id":"disaster-taiwan", "kw":["台灣地震 5級","Taiwan earthquake 5.0","台灣 5級以上","花蓮地震"],
     "title":"台灣地震 5級+","type":"disaster","lat":24.0,"lng":121.0,"region":"台灣",
     "impact":"CRITICAL","supply":"台積電等晶圓廠中斷風險（台灣最高供應鏈風險）",
     "affected_materials":["晶片","半導體"],"shipping_routes":["台灣港口"]},
    {"id":"disaster-typhoon", "kw":["颱風警報","typhoon warning","typhoon landing"],
     "title":"颱風預警","type":"disaster","lat":23.0,"lng":120.0,"region":"西太平洋",
     "impact":"HIGH","supply":"海運中斷、製造業停工風險",
     "affected_materials":["晶片","電子產品"],"shipping_routes":["東亞航線","太平洋航線"]},
]

_GEO_RISKS = [
    {"id":"geo-redsea",  "kw":["Houthi Red Sea ship attack","Red Sea shipping attack"],
     "title":"紅海航運威脅（胡塞武裝）","type":"war","lat":14.5,"lng":42.5,"region":"葉門/紅海",
     "impact":"CRITICAL","supply":"亞歐航程延長10-14天，運費上漲200-400%，建議改走好望角或提前備貨",
     "affected_materials":["晶片","電子產品","汽車零件"],"shipping_routes":["蘇伊士運河","紅海","亞歐航線"]},
    {"id":"geo-taiwan",  "kw":["PLA Taiwan Strait military","China Taiwan military exercise"],
     "title":"台灣海峽地緣緊張","type":"war","lat":24.0,"lng":122.0,"region":"東亞",
     "impact":"HIGH","supply":"全球半導體（TSMC等）供應鏈最高風險區",
     "affected_materials":["晶片","半導體","記憶體"],"shipping_routes":["台灣海峽","東北亞航線"]},
    {"id":"geo-iran",    "kw":["Iran Israel attack war","Iran US military strike","Iran attack Israel"],
     "title":"伊朗地區衝突","type":"war","lat":32.0,"lng":53.0,"region":"中東/波斯灣",
     "impact":"HIGH","supply":"荷姆茲海峽石油供應威脅，波斯灣航運風險",
     "affected_materials":["石油","天然氣","化工品"],"shipping_routes":["荷姆茲海峽","波斯灣","中東航線"]},
    {"id":"geo-ukraine", "kw":["Ukraine Russia war attack","Russia Ukraine missile"],
     "title":"俄烏戰爭","type":"war","lat":49.0,"lng":32.0,"region":"東歐",
     "impact":"CRITICAL","supply":"穀物、化肥、氖氣供應中斷；黑海航運受限",
     "affected_materials":["氖氣","鈀","穀物","化肥"],"shipping_routes":["黑海","烏克蘭港口","歐亞航線"]},
    {"id":"geo-drc",     "kw":["DRC Congo M23 conflict cobalt","Congo mineral conflict"],
     "title":"剛果衝突（礦產風險）","type":"war","lat":-1.5,"lng":29.0,"region":"中非",
     "impact":"HIGH","supply":"鈷、鋰等電池礦產供應不穩定",
     "affected_materials":["鈷","鋰","銅礦"],"shipping_routes":["中非港口","非洲航線"]},
    {"id":"geo-myanmar", "kw":["Myanmar civil war military","Myanmar junta conflict"],
     "title":"緬甸內戰","type":"war","lat":19.8,"lng":96.2,"region":"東南亞",
     "impact":"HIGH","supply":"稀土、天然氣出口受阻；紡織供應鏈中斷",
     "affected_materials":["稀土","天然氣","紡織品"],"shipping_routes":["馬六甲海峽","仰光港"]},
    {"id":"geo-india-pak",
     "kw":["India Pakistan military tension border","India Pakistan conflict"],
     "title":"印巴邊境緊張","type":"war","lat":30.0,"lng":71.0,"region":"南亞",
     "impact":"MED","supply":"南亞製造業（電子/紡織）物流中斷風險",
     "affected_materials":["紡織品","電子零件"],"shipping_routes":["南亞港口","阿拉伯海"]},
]

def _extract_earthquake_date(text):
    """Extract actual earthquake date from article text (not publication date)."""
    import re
    from datetime import datetime

    # Common earthquake date patterns in news

    # 1. Try MM/DD or M/D format (e.g. "4/20", "04/20")
    match = re.search(r'\b(\d{1,2})[\/\-](\d{1,2})\b', text)
    if match:
        try:
            month, day = int(match.group(1)), int(match.group(2))
            if 1 <= month <= 12 and 1 <= day <= 31:
                return f"2026-{month:02d}-{day:02d}"
        except:
            pass

    # 2. Try English month names (e.g. "April 20", "April 20, 2026")
    match = re.search(r'(January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{1,2})(?:\s*,?\s*(\d{4}))?', text, re.IGNORECASE)
    if match:
        try:
            month_name, day = match.group(1), int(match.group(2))
            year = int(match.group(3)) if match.group(3) else 2026

            # Parse month name to number
            month_map = {'january':1, 'february':2, 'march':3, 'april':4, 'may':5, 'june':6,
                        'july':7, 'august':8, 'september':9, 'october':10, 'november':11, 'december':12,
                        'jan':1, 'feb':2, 'mar':3, 'apr':4, 'may':5, 'jun':6, 'jul':7, 'aug':8,
                        'sep':9, 'oct':10, 'nov':11, 'dec':12}
            month = month_map.get(month_name.lower())
            if month:
                return f"{year}-{month:02d}-{day:02d}"
        except:
            pass

    # 3. Try Chinese format "4月20日", "04月20日"
    match = re.search(r'(\d{1,2})月(\d{1,2})日', text)
    if match:
        try:
            month, day = int(match.group(1)), int(match.group(2))
            if 1 <= month <= 12 and 1 <= day <= 31:
                return f"2026-{month:02d}-{day:02d}"
        except:
            pass

    return None


def _scan_one_geo_risk(risk, headers, cutoff):
    """Scan Bing News for one geopolitical risk entry. Returns result dict or None."""
    import xml.etree.ElementTree as ET
    import re
    import time as _time
    from urllib.parse import quote
    from email.utils import parsedate_to_datetime
    found_date = ""
    for kw in risk["kw"]:
        for attempt in range(3):
            try:
                url = f"https://www.bing.com/news/search?format=rss&q={quote(kw)}"
                r = req_lib.get(url, timeout=15, headers=headers)
                if r.status_code == 429:
                    logger.warning(f"[GEO] {risk['title']} rate-limited (429), retry {attempt+1}/3")
                    _time.sleep(3 * (attempt + 1))
                    continue
                if r.status_code != 200:
                    logger.warning(f"[GEO] {risk['title']} HTTP {r.status_code}, skip")
                    break
                snippet = r.content[:200].lstrip()
                if not (snippet.startswith(b'<?xml') or snippet.startswith(b'<rss')):
                    logger.warning(f"[GEO] {risk['title']} non-XML response ({len(r.content)}B), skip")
                    break
                clean = re.sub(rb'[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]', b'', r.content)
                items = ET.fromstring(clean).findall('.//item')[:5]
                logger.info(f"[GEO] {risk['title']} + '{kw}': {len(items)} items (status {r.status_code})")
                for item in items:
                    pub = item.findtext('pubDate', '')
                    title = item.findtext('title', '')
                    description = item.findtext('description', '')
                    try:
                        dt = parsedate_to_datetime(pub)
                        if dt >= cutoff:
                            # For earthquakes: check magnitude >= 5.0
                            if 'earthquake' in risk['type'].lower() or 'disaster' in risk['type'].lower() or any('地震' in kw for kw in risk['kw']):
                                text = f"{title} {description}".lower()
                                # Check for magnitude 5.0 or higher
                                has_high_magnitude = any([
                                    mag_pattern in text for mag_pattern in [
                                        '5.0', '5.1', '5.2', '5.3', '5.4', '5.5', '5.6', '5.7', '5.8', '5.9',
                                        '6.0', '6.1', '6.2', '6.3', '6.4', '6.5', '6.6', '6.7', '6.8', '6.9',
                                        '7.0', '7.1', '7.2', '7.3', '7.4', '7.5', '7.6', '7.7', '7.8', '7.9',
                                        '8.0', '8.1', '8.2', '8.3', '8.4', '8.5', '8.6', '8.7', '8.8', '8.9',
                                        '5級', '6級', '7級', '8級', '9級',
                                        'magnitude 5', 'magnitude 6', 'magnitude 7', 'magnitude 8', 'magnitude 9'
                                    ]
                                ])
                                if not has_high_magnitude:
                                    logger.debug(f"[GEO] Skipping {risk['title']}: no 5.0+ magnitude in article")
                                    continue

                            # For earthquakes: extract actual earthquake date from article content
                            if 'earthquake' in risk['type'].lower() or 'disaster' in risk['type'].lower() or any('地震' in kw for kw in risk['kw']):
                                article_text = f"{title} {description}"
                                earthquake_date = _extract_earthquake_date(article_text)
                                if earthquake_date:
                                    found_date = earthquake_date
                                    logger.info(f"[GEO] ✓ {risk['title']}: earthquake on {earthquake_date}")
                                else:
                                    # Fallback to news publication date if we can't extract earthquake date
                                    found_date = str(dt.date())
                                    logger.debug(f"[GEO] {risk['title']}: using publication date {found_date}")
                            else:
                                found_date = str(dt.date())
                                logger.info(f"[GEO] ✓ {risk['title']}: found recent article")
                            break
                    except (ValueError, TypeError):
                        pass
                break
            except ET.ParseError as e:
                logger.warning(f"[GEO] {risk['title']} ParseError on attempt {attempt+1}: {e}")
                _time.sleep(2)
            except Exception as e:
                logger.warning(f"[GEO] {risk['title']} + '{kw}' ERROR: {type(e).__name__}: {e}")
                break
    if not found_date:
        logger.info(f"[GEO] ✗ {risk['title']}: no matching articles")
        return None
    from urllib.parse import quote as _q
    return {
        "id": risk["id"], "type": risk["type"],
        "title": risk["title"], "lat": risk["lat"], "lng": risk["lng"],
        "region": risk["region"], "impact": risk["impact"],
        "supply": risk["supply"],
        "time": found_date, "status": "新聞持續報導中",
        "source": "Bing News自動監測",
        "sourceUrl": f"https://www.bing.com/news/search?q={_q(risk['kw'][0])}",
    }


def _do_geo_scan():
    """Run parallel geopolitical AND disaster scan and update cache. Returns results list."""
    from datetime import datetime, timezone, timedelta
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
    }
    cutoff = datetime.now(timezone.utc) - timedelta(days=45)
    results = []
    all_risks = _GEO_RISKS + _DISASTER_RISKS  # Include both geopolitical and disaster risks
    executor = ThreadPoolExecutor(max_workers=min(2, len(all_risks)))  # Limit to 2 parallel (reduce rate-limit triggers)
    try:
        futs = [executor.submit(_scan_one_geo_risk, risk, headers, cutoff)
                for risk in all_risks]
        done, not_done = fut_wait(futs, timeout=60)  # Increased from 20 to 60
        for fut in not_done:
            fut.cancel()
        for fut in done:
            try:
                res = fut.result()
                if res:
                    results.append(res)
                    logger.info(f"[GEO] Found: {res['title']}")
            except Exception as e:
                logger.debug(f"geo scan error: {e}")
    finally:
        executor.shutdown(wait=False)
    with _geo_risk_lock:
        # Fallback: if scan returns no results, keep last cached data
        if results:
            _geo_risk_cache["data"] = results
        elif _geo_risk_cache["data"] is None:
            _geo_risk_cache["data"] = []
        _geo_risk_cache["ts"] = time.time()
    logger.info(f"Geopolitical + Disaster risks detected: {len(results)}/{len(all_risks)}")
    return results


@app.route("/api/risk/geopolitical")
def api_risk_geopolitical():
    """Return cached geopolitical risks instantly. Background loop refreshes every 3 hours."""
    with _geo_risk_lock:
        data = _geo_risk_cache["data"]
    if data is None:
        return jsonify([])
    return jsonify(data)


# ── Strike risk monitor ─────────────────────────────────────────────────────
_STRIKE_TARGETS = [
    {"company": "三星電子",  "kw": ["三星 罷工", "Samsung strike", "Samsung workers strike"],
     "lat": 37.00, "lng": 127.06, "region": "韓國"},
    {"company": "現代汽車",  "kw": ["現代 罷工", "Hyundai strike", "Hyundai workers"],
     "lat": 37.49, "lng": 126.86, "region": "韓國"},
    {"company": "富士康",    "kw": ["富士康 罷工", "Foxconn strike", "foxconn workers"],
     "lat": 34.75, "lng": 113.62, "region": "中國"},
    {"company": "波音",      "kw": ["波音 罷工", "Boeing strike", "Boeing workers walkout"],
     "lat": 47.44, "lng": -122.31, "region": "美國"},
    {"company": "UPS",       "kw": ["UPS strike", "UPS workers walkout"],
     "lat": 33.75, "lng": -84.39,  "region": "美國"},
    {"company": "Volkswagen","kw": ["Volkswagen strike", "VW strike", "福斯 罷工"],
     "lat": 52.42, "lng": 10.79,   "region": "德國"},
    {"company": "通用汽車",  "kw": ["GM strike", "General Motors strike", "UAW strike"],
     "lat": 42.33, "lng": -83.04,  "region": "美國"},
    {"company": "SK海力士",  "kw": ["SK Hynix strike", "SK海力士 罷工"],
     "lat": 37.27, "lng": 127.44,  "region": "韓國"},
    {"company": "LG",        "kw": ["LG strike", "LG 罷工"],
     "lat": 37.52, "lng": 126.89,  "region": "韓國"},
    {"company": "比亞迪",    "kw": ["比亞迪 罷工", "BYD strike", "BYD workers"],
     "lat": 22.58, "lng": 114.09,  "region": "中國"},
]

_strike_cache: dict = {"data": None, "ts": 0.0}
_strike_lock  = threading.Lock()

def _scan_one_strike(target, headers, cutoff):
    """Scan Bing News for one strike target. Returns result dict or None."""
    import xml.etree.ElementTree as ET
    import re
    import time as _time
    from urllib.parse import quote
    from email.utils import parsedate_to_datetime
    found_article = None
    for kw in target["kw"]:
        for attempt in range(3):
            try:
                url = f"https://www.bing.com/news/search?format=rss&q={quote(kw)}"
                r = req_lib.get(url, timeout=15, headers=headers)
                if r.status_code == 429:
                    logger.warning(f"[STRIKE] {target['company']} rate-limited (429), retry {attempt+1}/3")
                    _time.sleep(3 * (attempt + 1))
                    continue
                if r.status_code != 200:
                    logger.warning(f"[STRIKE] {target['company']} HTTP {r.status_code}, skip")
                    break
                snippet = r.content[:200].lstrip()
                if not (snippet.startswith(b'<?xml') or snippet.startswith(b'<rss')):
                    logger.warning(f"[STRIKE] {target['company']} non-XML response ({len(r.content)}B), skip")
                    break
                clean = re.sub(rb'[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]', b'', r.content)
                root = ET.fromstring(clean)
                items = root.findall(".//item")[:5]
                logger.info(f"[STRIKE] {target['company']} + '{kw}': {len(items)} items (status {r.status_code})")
                for item in items:
                    pub = item.findtext("pubDate", "")
                    try:
                        dt = parsedate_to_datetime(pub)
                        if dt >= cutoff:
                            found_article = {
                                "title": item.findtext("title", ""),
                                "url":   item.findtext("link", ""),
                                "date":  str(dt.date()),
                            }
                            logger.info(f"[STRIKE] ✓ {target['company']}: {found_article['title'][:60]}")
                            break
                    except Exception:
                        pass
                break
            except ET.ParseError as e:
                logger.warning(f"[STRIKE] {target['company']} ParseError on attempt {attempt+1}: {e}")
                _time.sleep(2)
            except Exception as e:
                logger.warning(f"[STRIKE] {target['company']} + '{kw}' ERROR: {type(e).__name__}: {e}")
                break
    if not found_article:
        logger.info(f"[STRIKE] ✗ {target['company']}: no matching articles")
        return None

    # Decode Bing apiclick redirect URLs to get actual article URLs
    article_url = found_article["url"]
    if "bing.com/news/apiclick.aspx" in article_url:
        try:
            from urllib.parse import urlparse, parse_qs, unquote
            qs = parse_qs(urlparse(article_url).query)
            if "url" in qs:
                article_url = unquote(qs["url"][0])
        except Exception:
            pass  # If parsing fails, use original URL

    return {
        "id":        f"strike-{target['company']}",
        "type":      "strike",
        "title":     f"{target['company']} 罷工事件",
        "lat":       target["lat"], "lng": target["lng"],
        "region":    target["region"],
        "time":      found_article["date"],
        "impact":    "HIGH",
        "supply":    f"{target['company']}勞資衝突，可能影響生產排程與出貨交期，建議評估替代供應",
        "source":    "Bing News自動監測",
        "sourceUrl": article_url,
        "newsTitle": found_article["title"],
    }


def _do_strike_scan():
    """Run parallel strike scan and update cache. Returns results list."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8",
    }
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    results = []
    executor = ThreadPoolExecutor(max_workers=min(2, len(_STRIKE_TARGETS)))  # Limit to 2 parallel (reduce rate-limit triggers)
    try:
        futs = [executor.submit(_scan_one_strike, t, headers, cutoff)
                for t in _STRIKE_TARGETS]
        done, not_done = fut_wait(futs, timeout=60)  # Increased from 25 to 60
        for fut in not_done:
            fut.cancel()
        for fut in done:
            try:
                res = fut.result()
                if res:
                    results.append(res)
                    logger.info(f"[STRIKE] Found: {res['title']} ({res['time']})")
            except Exception as e:
                logger.debug(f"strike scan error: {e}")
    finally:
        executor.shutdown(wait=False)
    with _strike_lock:
        # Fallback: if scan returns no results, keep last cached data
        if results:
            _strike_cache["data"] = results
        elif _strike_cache["data"] is None:
            _strike_cache["data"] = []
        _strike_cache["ts"] = time.time()
    logger.info(f"Strike events: {len(results)}/{len(_STRIKE_TARGETS)}")
    return results


@app.route("/api/risk/strikes")
def api_risk_strikes():
    """Return cached strike events instantly. Background loop refreshes every 3 hours."""
    with _strike_lock:
        data = _strike_cache["data"]
    if data is None:
        return jsonify([])
    return jsonify(data)


def _get_commodity_data() -> dict:
    """Return only web/API prices (no CSV mixing). Use live cache data."""
    # Build commodity data from live cache only (network prices, not CSV)
    result = {}

    # Category mapping
    item_to_cat = {}
    for cat, items in _COMMODITY_CATEGORIES.items():
        for item in items:
            item_to_cat[item] = cat

    with _live_cache_lock:
        for csv_name, live_points in _live_commodity_cache.items():
            if not live_points:
                continue

            # Extract unit and category
            unit = ""
            _LIVE_UNIT_OVERRIDES = {"鎢": "元/千克"}
            for name_key, u_val in _LIVE_UNIT_OVERRIDES.items():
                if name_key == csv_name:
                    unit = u_val
                    break
            if not unit:
                for u in ["US$/tonne", "CNY$/tonne", "US$/盎司", "US$/桶", "USD/T", "CNY$/kg"]:
                    if u in csv_name:
                        unit = u
                        break

            cat = "其他"
            for key, c in item_to_cat.items():
                if key in csv_name:
                    cat = c
                    break

            result[csv_name] = {
                "unit":     unit,
                "category": cat,
                "dates":    [p[0] for p in live_points],
                "values":   [p[1] for p in live_points],
            }

    return result


@app.route("/api/commodities")
def api_commodities():
    """Return item metadata only (no history) — fast small payload."""
    with _live_cache_lock:
        cache_empty = not _live_commodity_cache
    data = _get_commodity_data()
    with _item_sources_lock:
        src_snapshot = dict(_item_sources)
    items = []
    for name, d in data.items():
        latest = next((v for v in reversed(d["values"]) if v is not None), None)
        prev   = next((v for v in reversed(d["values"][:-1]) if v is not None), None)
        change = round(((latest - prev) / prev * 100), 2) if latest and prev and prev != 0 else None
        src    = src_snapshot.get(name, {})
        # Find the date of the latest non-null value
        latest_date = None
        for dt, v in zip(reversed(d["dates"]), reversed(d["values"])):
            if v is not None:
                latest_date = dt
                break
        items.append({
            "name":         name,
            "unit":         d["unit"],
            "category":     d["category"],
            "latest":       latest,
            "latest_date":  latest_date,
            "change":       change,
            "source_label": src.get("label", ""),
            "source_url":   src.get("url", ""),
        })
    categories = list(_COMMODITY_CATEGORIES.keys())
    return jsonify({"items": items, "categories": categories, "loading": cache_empty})


@app.route("/api/commodity-history")
def api_commodity_history():
    """Return full date/value history for a single item (fetched on demand)."""
    name = request.args.get("name", "").strip()
    if not name:
        return jsonify({"dates": [], "values": []})
    # Bypass 5-min cache to get fresh live data
    with _csv_parse_lock:
        _csv_parse_cache["data"] = None
    data = _get_commodity_data()
    d = data.get(name)
    if not d:
        return jsonify({"dates": [], "values": []})
    return jsonify({"dates": d["dates"], "values": d["values"]})


# ── Supply Chain Risk Monitor ─────────────────────────────────────────────────

_SUPPLY_CHAIN_CLUSTERS = [
    {"id": "hsinchu",    "name": "新竹",     "name_en": "Hsinchu",        "lat": 24.76, "lng": 120.99, "industries": ["半導體", "IC設計"],      "region": "TW"},
    {"id": "taichung",   "name": "台中",     "name_en": "Taichung",       "lat": 24.15, "lng": 120.68, "industries": ["精密製造", "電子"],       "region": "TW"},
    {"id": "shenzhen",   "name": "深圳",     "name_en": "Shenzhen",       "lat": 22.54, "lng": 114.06, "industries": ["消費電子", "PCB"],         "region": "CN", "region_macro": "華南", "region_city": "深圳"},
    {"id": "kunshan",    "name": "昆山",     "name_en": "Kunshan",        "lat": 31.39, "lng": 121.16, "industries": ["PCB", "NB代工"],          "region": "CN", "region_macro": "華東", "region_city": "昆山"},
    {"id": "zhengzhou",  "name": "鄭州",     "name_en": "Zhengzhou",      "lat": 34.75, "lng": 113.62, "industries": ["手機組裝", "EMS"],         "region": "CN", "region_macro": "華中", "region_city": "鄭州"},
    {"id": "shanghai",   "name": "上海",     "name_en": "Shanghai",       "lat": 31.23, "lng": 121.47, "industries": ["汽車電子", "IC設計"],      "region": "CN", "region_macro": "華東", "region_city": "上海"},
    {"id": "penang",     "name": "檳城",     "name_en": "Penang",         "lat": 5.41,  "lng": 100.33, "industries": ["IC封測", "電子製造"],      "region": "MY"},
    {"id": "pyeongtaek", "name": "平澤",     "name_en": "Pyeongtaek",     "lat": 36.99, "lng": 127.11, "industries": ["DRAM", "NAND Flash"],     "region": "KR"},
    {"id": "icheon",     "name": "利川",     "name_en": "Icheon",         "lat": 37.27, "lng": 127.44, "industries": ["DRAM", "記憶體"],          "region": "KR"},
    {"id": "kumamoto",   "name": "熊本",     "name_en": "Kumamoto",       "lat": 32.80, "lng": 130.71, "industries": ["晶圓代工", "半導體"],       "region": "JP"},
    {"id": "osaka",      "name": "大阪",     "name_en": "Osaka",          "lat": 34.69, "lng": 135.50, "industries": ["OLED", "感測器"],          "region": "JP"},
    {"id": "san_jose",   "name": "矽谷",     "name_en": "Silicon Valley", "lat": 37.34, "lng": -121.89,"industries": ["AI晶片", "Fabless"],      "region": "US"},
    {"id": "austin",     "name": "奧斯汀",   "name_en": "Austin TX",      "lat": 30.27, "lng": -97.74, "industries": ["晶圓廠", "資料中心"],      "region": "US"},
    {"id": "dresden",    "name": "德勒斯登", "name_en": "Dresden",        "lat": 51.05, "lng": 13.74,  "industries": ["汽車晶片", "半導體"],      "region": "EU"},
    {"id": "eindhoven",  "name": "恩荷芬",   "name_en": "Eindhoven",      "lat": 51.44, "lng": 5.48,   "industries": ["半導體設備", "EUV"],       "region": "EU"},
]

_RISK_KEYWORDS = {
    "disaster":     ["地震", "颶風", "洪水", "水災", "火災", "海嘯", "暴風雪", "龍捲風", "冰雹", "霜凍", "雪災",
                     "earthquake", "hurricane", "flood", "tsunami", "disaster", "blizzard", "tornado", "snowstorm", "cyclone"],
    "geopolitical": ["制裁", "關稅", "禁令", "出口管制", "貿易戰", "戰爭", "衝突", "伊朗", "中東", "紅海", "胡塞", "俄烏", "以巴",
                     "tariff", "sanction", "ban", "export control", "trade war", "chip war",
                     "war", "conflict", "iran", "middle east", "red sea", "houthi", "russia ukraine", "israel palestin"],
    "strike":       ["罷工", "工人罷工", "工潮", "勞資爭議", "勞工抗議", "工會", "停工", "罷課",
                     "strike", "labor strike", "workers strike", "walkout", "industrial action", "union"],
    "operational":  ["限電", "缺料", "斷鏈", "停工", "產能", "blackout", "shortage", "disruption", "halt"],
    "financial":    ["破產", "虧損", "裁員", "信評", "倒閉", "財報", "獲利預警", "虧損擴大",
                     "bankruptcy", "layoff", "downgrade", "profit warning", "earnings miss", "default"],
}

# Typhoon only counts as disaster if paired with SERIOUS impact keywords (致災程度，不只是氣象預報)
_DISASTER_SEVERITY_KEYWORDS = ["致災", "災害", "損失", "損害", "中斷", "停工", "罹難", "傷亡", "淹水", "破壞",
                                "damage", "disruption", "impact", "closure", "casualty", "fatality", "flooding", "destruction"]
_TYPHOON_KEYWORDS = ["颱風", "typhoon"]
_TYPHOON_FORECAST_KEYWORDS = ["預報", "預測", "警報", "警戒", "forecast", "warning", "alert", "prediction"]  # Exclude pure forecasts

_CLUSTER_KEYWORDS = {
    "hsinchu":    ["新竹", "竹科", "台積電", "TSMC", "聯電", "UMC", "聯發科", "MediaTek"],
    "taichung":   ["台中", "中科"],
    "shenzhen":   ["深圳", "Shenzhen", "比亞迪", "BYD"],
    "kunshan":    ["昆山", "Kunshan"],
    "zhengzhou":  ["鄭州", "Zhengzhou", "富士康", "Foxconn", "鴻海"],
    "shanghai":   ["上海", "Shanghai", "張江", "浦東"],
    "penang":     ["檳城", "Penang", "馬來西亞", "Malaysia"],
    "pyeongtaek": ["平澤", "Pyeongtaek", "三星", "Samsung", "韓國"],
    "icheon":     ["利川", "Icheon", "SK海力士", "SK Hynix", "海力士", "韓國"],
    "kumamoto":   ["熊本", "Kumamoto", "TSMC日本", "JASM"],
    "osaka":      ["大阪", "Osaka", "夏普", "Sharp", "Japan Display", "JDI", "Sony"],
    "san_jose":   ["矽谷", "Silicon Valley", "聖荷西"],
    "austin":     ["奧斯汀", "Austin"],
    "dresden":    ["德勒斯登", "Dresden"],
    "eindhoven":  ["恩荷芬", "Eindhoven"],
}

# 根據供應商分布，對應集群的地區影響範圍
_REGION_TO_CLUSTERS = {
    "台灣": ["hsinchu", "taichung"],  # 台灣供應商集中在新竹、台中
    "台北": ["hsinchu"],  # 台北新竹相近
    "中國大陸": ["shenzhen", "kunshan", "zhengzhou", "shanghai"],  # 中國集群
    # 中國宏觀地區分層
    "華東": ["shanghai", "kunshan"],  # 上海、昆山
    "華南": ["shenzhen"],  # 深圳
    "華中": ["zhengzhou"],  # 鄭州
    "日本": ["kumamoto", "osaka"],  # 日本集群
    "韓國": ["pyeongtaek", "icheon"],  # 韓國集群
    "馬來西亞": ["penang"],  # 馬來西亞集群
    "美國": ["san_jose", "austin"],  # 美國集群
    "德國": ["dresden"],  # 德國集群
    "荷蘭": ["eindhoven"],  # 荷蘭集群
}

_REGION_LABELS = {
    # Cluster regions
    "TW": "🇹🇼 台灣 (Taiwan)",
    "CN": "🇨🇳 中國 (China)",
    "KR": "🇰🇷 韓國 (South Korea)",
    "JP": "🇯🇵 日本 (Japan)",
    "US": "🇺🇸 美國 (USA)",
    "MY": "🇲🇾 馬來西亞 (Malaysia)",
    "EU": "🇪🇺 歐洲 (Europe)",
    # China macro regions
    "華東": "📍 華東 (East China)",
    "華南": "📍 華南 (South China)",
    "華中": "📍 華中 (Central China)",
    # Geopolitical regions
    "東亞": "🗺️ 東亞 (East Asia)",
    "東南亞": "🗺️ 東南亞 (Southeast Asia)",
    "南亞": "🗺️ 南亞 (South Asia)",
    "中東/波斯灣": "🗺️ 中東/波斯灣 (Middle East/Persian Gulf)",
    "葉門/紅海": "🗺️ 葉門/紅海 (Yemen/Red Sea)",
    "東歐": "🗺️ 東歐 (Eastern Europe)",
    "中非": "🗺️ 中非 (Central Africa)",
}

# Map broader geopolitical/event regions to specific fab cluster regions
_GEO_REGION_TO_CLUSTERS = {
    "東亞": ["台灣", "韓國", "日本"],  # Taiwan Strait tensions affect East Asia fabs
    "中東/波斯灣": [],  # Shipping impact, no direct fab region
    "葉門/紅海": [],  # Shipping impact, no direct fab region
    "東歐": [],  # No fab clusters in Eastern Europe
    "中非": [],  # Cobalt, but no direct fab impact
    "中國": ["中國大陸"],  # Direct mapping for China
    "台灣": ["台灣"],  # Taiwan fabs
    "韓國": ["韓國"],  # Korean fabs
    "日本": ["日本"],  # Japanese fabs
    "馬來西亞": ["馬來西亞"],  # Malaysian fabs
    "美國": ["美國"],  # US fabs
    "德國": ["德國"],  # German fabs
    "荷蘭": ["荷蘭"],  # Dutch fabs
}

_RISK_TYPE_LABELS = {
    "disaster":     "🌊 天災",
    "geopolitical": "🚨 地緣",
    "strike":       "✊ 罷工",
    "operational":  "⚡ 停運",
    "financial":    "💸 財警",  # Financial: shown in news wall but NOT counted for risk scores
}

# Key fab keywords: ONLY critical fabs (TSMC, Samsung, SK Hynix)
_KEY_FAB_KEYWORDS = ["台積電", "tsmc", "samsung", "三星", "sk海力士", "sk hynix", "hynix"]

# Fab-related companies whose strikes matter for supply chain risk
# (not all companies in _STRIKE_TARGETS are fabs)
_FAB_COMPANIES = ["三星電子", "Samsung", "SK海力士", "SK Hynix", "富士康", "Foxconn"]

# Event certainty keywords: indicates confirmed/imminent event (not pure forecast)
_CONFIRMED_EVENT_KEYWORDS = ["宣布", "確認", "已發生", "發動", "啟動", "正在", "進行中", "將", "即將",
                             "announced", "confirmed", "occurred", "launched", "underway", "will"]

# Event duration keywords: indicates prolonged impact (>7 days)
_PROLONGED_EVENT_KEYWORDS = ["18天", "两周", "一周", "持續", "ongoing", "continues", "week", "month"]

# Map key fab companies to their regions (for automatic region inference from company mentions)
_FAB_TO_REGIONS = {
    "台積電": "台灣",
    "tsmc": "台灣",
    "samsung": "韓國",
    "三星": "韓國",
    "sk海力士": "韓國",
    "sk hynix": "韓國",
    "hynix": "韓國",
}


@app.route("/api/risk")
def api_risk():
    """Supply chain risk monitor: cluster risk scores + tagged news."""
    with _cache_lock:
        articles = list(_cache["articles"])

    now = datetime.now(timezone(timedelta(hours=8)))
    cutoff_21d = (now - timedelta(days=21)).strftime("%Y-%m-%d")  # Extended to 21 days for tracking
    recent = [a for a in articles
              if (a.get("published") or a.get("fetched_at", ""))[:10] >= cutoff_21d]

    # Risk scoring weights: Financial excluded (not counted for risk scores)
    weights = {"disaster": 30, "geopolitical": 20, "strike": 20, "operational": 15}
    cluster_scores = {c["id"]: 0 for c in _SUPPLY_CHAIN_CLUSTERS}

    for article in recent:
        pub_str = article.get("published") or article.get("fetched_at", "")
        try:
            pub_date = datetime.strptime(pub_str[:10], "%Y-%m-%d").date()
        except:
            continue

        days_old = (now.date() - pub_date).days
        text = (article.get("title", "") + " " + article.get("summary", "")).lower()

        # 1. Filter: Only count if affects KEY fab (TSMC, Samsung, SK Hynix, etc.)
        has_key_fab = any(kw.lower() in text for kw in _KEY_FAB_KEYWORDS)
        if not has_key_fab:
            continue  # Skip if not involving critical fab

        # 2. Detect if typhoon is pure forecast (exclude these)
        is_typhoon_forecast = False
        if any(tk.lower() in text for tk in _TYPHOON_KEYWORDS):
            if any(fk.lower() in text for fk in _TYPHOON_FORECAST_KEYWORDS):
                is_typhoon_forecast = True

        # 3. Detect event certainty: confirmed/imminent events only
        is_confirmed = any(kw.lower() in text for kw in _CONFIRMED_EVENT_KEYWORDS)

        # 4. Detect event duration: prolonged impacts (>= 7 days)
        is_prolonged = any(kw.lower() in text for kw in _PROLONGED_EVENT_KEYWORDS)

        # 5. Identify affected regions: via cluster keywords OR infer from company mentions
        affected_regions = set()

        # First try: match cluster location keywords
        for cid, ckws in _CLUSTER_KEYWORDS.items():
            if any(kw.lower() in text for kw in ckws):
                for region, cluster_list in _REGION_TO_CLUSTERS.items():
                    if cid in cluster_list:
                        affected_regions.add(region)
                        break

        # Fallback: if no location found but key fab mentioned, infer region from company
        if not affected_regions:
            for fab, region in _FAB_TO_REGIONS.items():
                if fab.lower() in text:
                    affected_regions.add(region)
                    break  # One fab is enough to infer region

        if not affected_regions:
            continue  # Skip if no region can be inferred

        # 6. Calculate time decay: reduce weight for older events
        time_multiplier = 1.0
        if days_old > 7:
            time_multiplier = max(0.3, 1.0 - (days_old - 7) * 0.1)  # Decrease 10% per day after day 7
        if days_old > 21:
            continue  # Don't score events older than 21 days

        # 7. Score only the affected region's clusters
        for rtype, rkws in _RISK_KEYWORDS.items():
            if rtype == "financial":
                continue  # SKIP financial news for risk scoring

            risk_found = False
            if rtype == "disaster":
                # Disaster risk: two categories
                # (1) Earthquakes: 5.0+ magnitude, no time limit
                # (2) Typhoon/flood/tsunami/blizzard: need severity keywords + within 3 days

                is_earthquake = any(ek.lower() in text for ek in ["地震", "earthquake", "magnitude", "震度"])

                if is_earthquake:
                    # Earthquake: check for magnitude >= 5.0
                    # Look for patterns like "5.0", "5級", "5强", etc.
                    import re as _re
                    magnitude_patterns = [
                        r'(?:magnitude|震度|級)\s*[5-9]',  # magnitude 5-9
                        r'[5-9](?:\.[0-9])?(?:級|强|magnitude)?',  # 5.0-9.9 magnitude
                    ]
                    if any(_re.search(pat, text, _re.IGNORECASE) for pat in magnitude_patterns):
                        risk_found = True
                        logger.debug(f"Earthquake 5.0+ detected: {text[:60]}")

                elif any(tk.lower() in text for tk in _TYPHOON_KEYWORDS):
                    # Typhoon/flood/tsunami/blizzard: need severity + 3 days limit
                    if days_old > 3:
                        continue  # Skip events older than 3 days
                    if not is_typhoon_forecast and any(sk.lower() in text for sk in _DISASTER_SEVERITY_KEYWORDS):
                        risk_found = True
                        logger.debug(f"Severe disaster within 3 days: {text[:60]}")
                # Other disasters not mentioned above: skip
            elif any(rk.lower() in text for rk in rkws):
                risk_found = True

            if risk_found:
                # Adjust weight based on certainty and duration
                base_weight = weights.get(rtype, 10)

                # If not confirmed/imminent, reduce weight
                if not is_confirmed and rtype in ["strike", "geopolitical"]:
                    base_weight *= 0.6

                # If not prolonged (>7 days), reduce weight
                if not is_prolonged and rtype in ["strike", "operational"]:
                    base_weight *= 0.7

                final_weight = base_weight * time_multiplier

                # Only increase score for affected region's clusters
                for region in affected_regions:
                    for cid in _REGION_TO_CLUSTERS.get(region, []):
                        cluster_scores[cid] = min(100, cluster_scores[cid] + final_weight)

    # Also score clusters based on cached strike and geopolitical events
    with _strike_lock:
        strikes = _strike_cache.get("data", []) or []
    with _geo_risk_lock:
        geo_risks = _geo_risk_cache.get("data", []) or []

    for event in strikes + geo_risks:
        # Skip non-fab strikes (only count strikes from actual fab companies)
        event_type = event.get("type", "")
        if event_type == "strike":
            # Extract company name from event title (format: "公司名 罷工事件")
            title = event.get("title", "")
            is_fab_strike = any(fab in title for fab in _FAB_COMPANIES)
            if not is_fab_strike:
                continue  # Skip non-fab company strikes

        # Check if event is recent (within 21 days)
        event_date_str = event.get("time", "")
        if not event_date_str:
            continue
        try:
            event_date = datetime.strptime(event_date_str[:10], "%Y-%m-%d").date()
        except:
            continue

        days_old = (now.date() - event_date).days
        if days_old > 21:
            continue

        # Calculate time decay
        time_multiplier_event = 1.0
        if days_old > 7:
            time_multiplier_event = max(0.3, 1.0 - (days_old - 7) * 0.1)

        # Score the event based on its region and type
        event_region = event.get("region", "")

        if event_type == "strike":
            base_weight_event = weights.get("strike", 20)
            final_weight_event = base_weight_event * time_multiplier_event
        elif event_type in ["geopolitical", "war"]:
            base_weight_event = weights.get("geopolitical", 20)
            final_weight_event = base_weight_event * time_multiplier_event
        else:
            continue

        # Map event region to actual fab cluster regions
        fab_regions = _GEO_REGION_TO_CLUSTERS.get(event_region, [])
        if not fab_regions:
            # Try direct region mapping if geo mapping doesn't apply
            if event_region in _REGION_TO_CLUSTERS:
                fab_regions = [event_region]

        # Score all clusters in the affected fab regions
        for fab_region in fab_regions:
            for cid in _REGION_TO_CLUSTERS.get(fab_region, []):
                cluster_scores[cid] = min(100, cluster_scores[cid] + final_weight_event)

    # Tag articles for news walls
    regional_events, financial_warnings = [], []
    seen: set = set()
    for article in articles[:400]:
        url = article.get("source_url") or article.get("url", "")
        if url in seen:
            continue
        seen.add(url)
        text = (article.get("title", "") + " " + article.get("summary", "")).lower()

        # Calculate article age
        pub_str = article.get("published") or article.get("fetched_at", "")
        try:
            pub_date = datetime.strptime(pub_str[:10], "%Y-%m-%d").date()
            article_days_old = (now.date() - pub_date).days
        except:
            article_days_old = 0

        # Detect risk types with special handling for typhoon/flood (require severity keywords + 3-day limit)
        risk_types = []
        for rt, rkws in _RISK_KEYWORDS.items():
            if any(rk.lower() in text for rk in rkws):
                # For disaster: ONLY typhoon/flood within 3 days are shown
                # Per user requirement: "洪水 氣旋 三天內才顯示 其餘一律不視為有風險"
                if rt == "disaster":
                    if any(tk.lower() in text for tk in _TYPHOON_KEYWORDS):
                        # Typhoon/flood: only show if within 3 days AND has severity keywords
                        if article_days_old <= 3 and any(sk.lower() in text for sk in _DISASTER_SEVERITY_KEYWORDS):
                            risk_types.append(rt)
                    # All other disasters: do NOT show on events/map (skip)
                else:
                    risk_types.append(rt)

        if not risk_types:
            continue
        region_tags, industry_tags = set(), set()
        for c in _SUPPLY_CHAIN_CLUSTERS:
            if any(kw.lower() in text for kw in _CLUSTER_KEYWORDS.get(c["id"], [])):
                region_tags.add(_REGION_LABELS.get(c["region"], c["region"]))
                industry_tags.update(c["industries"][:2])
        item = {
            "title":         article.get("title"),
            "url":           url,
            "published":     article.get("published"),
            "source":        article.get("source"),
            "risk_types":    [_RISK_TYPE_LABELS[rt] for rt in risk_types],
            "region_tags":   sorted(region_tags)[:3],
            "industry_tags": sorted(industry_tags)[:4],
        }
        if "financial" in risk_types:
            financial_warnings.append(item)
        else:
            regional_events.append(item)

    # Filter regional_events: remove typhoon/flood events older than 3 days
    # Per user requirement: "洪水 氣旋 三天內才顯示"
    filtered_regional_events = []
    for event in regional_events:
        risk_type_labels = event.get("risk_types", [])
        # Check if this is a typhoon/flood event
        has_typhoon_label = "🌊 天災" in risk_type_labels  # Disaster emoji label

        if has_typhoon_label:
            # Only keep if within 3 days
            try:
                event_date = datetime.strptime(event.get("published", "")[:10], "%Y-%m-%d").date()
                days_old = (now.date() - event_date).days
                if days_old <= 3:
                    filtered_regional_events.append(event)
            except:
                # If date parsing fails, exclude it
                pass
        else:
            # Keep non-disaster events
            filtered_regional_events.append(event)

    regional_events = filtered_regional_events

    clusters_out = [{**c, "risk_score": cluster_scores.get(c["id"], 0)}
                    for c in _SUPPLY_CHAIN_CLUSTERS]

    # Get cached strikes and geopolitical risks
    with _strike_lock:
        strikes = _strike_cache.get("data", []) or []
    with _geo_risk_lock:
        geo_risks = _geo_risk_cache.get("data", []) or []

    # Combine specific events (strikes + geo risks) for the map
    # Filter out typhoon/flood events older than 3 days (per user requirement)
    specific_events = []
    for event in strikes + geo_risks:
        event_type = event.get("type", "").lower()
        event_title = event.get("title", "").lower()

        # Check if this is a typhoon/flood/disaster event
        is_disaster = any(kw in event_title for kw in ["颱風", "typhoon", "洪水", "flood", "氣旋", "cyclone"])

        # Filter: skip old typhoon/flood events (keep only 3 days old or newer)
        if is_disaster:
            try:
                event_date_str = event.get("time", "")
                if event_date_str:
                    event_date = datetime.strptime(event_date_str[:10], "%Y-%m-%d").date()
                    days_old = (now.date() - event_date).days
                    if days_old > 3:
                        continue  # Skip this old disaster event
            except:
                # If date parsing fails, skip it to be safe
                continue

        specific_events.append({
            "id": event.get("id"),
            "type": event.get("type"),
            "title": event.get("title"),
            "region": event.get("region"),
            "lat": event.get("lat"),
            "lng": event.get("lng"),
            "impact": event.get("impact"),
            "supply": event.get("supply"),
            "time": event.get("time"),
            "source": event.get("source"),
            "sourceUrl": event.get("sourceUrl"),
            "newsTitle": event.get("newsTitle", ""),
        })

    return jsonify({
        "clusters":           clusters_out,
        "regional_events":    regional_events[:50],
        "financial_warnings": financial_warnings[:50],
        "specific_events":    specific_events,
        "last_updated":       now.strftime("%Y-%m-%d %H:%M"),
    })


_threads_started = False

def ensure_background_threads():
    """Ensure background threads are running (safe to call multiple times)."""
    global _threads_started
    if _threads_started:
        return
    _threads_started = True
    logger.info("Starting background threads...")
    threading.Thread(target=background_refresh_loop, daemon=True).start()
    threading.Thread(target=_live_price_loop, daemon=True).start()
    threading.Thread(target=_risk_cache_preload_loop, daemon=True).start()
    threading.Thread(target=_digitimes_refresh_loop, daemon=True).start()
    logger.info("Background threads started (including Digitimes 2-hour refresh)")

if __name__ == "__main__":
    logger.info("Fetching initial live prices...")
    _refresh_live_prices()
    # Pre-warm risk caches in background so first page visit is fast
    threading.Thread(target=_risk_cache_preload_loop, daemon=True).start()
    app.run(host="0.0.0.0", debug=False, port=5050, use_reloader=False)
