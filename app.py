"""
News Aggregation Platform — Flask Backend
ASUSTIMES: ASUS tech industry news hub
Auto-refreshes every 30 minutes in background.
"""

import os
import csv
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
        articles = fetch_all_news()
        with _cache_lock:
            _cache["articles"] = articles
            _cache["last_updated"] = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
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
                logger.info("Background threads started in worker")


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
    """Decode Google News redirect URL (CBMi...) to get the actual article URL."""
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
    """Resolve Google News redirect, then extract a short text snippet."""
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

    today = date_cls.today().isoformat()

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
        cutoff = (date_cls.today() - timedelta(days=2)).isoformat()
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


# ── Live commodity price fetching ─────────────────────────────────────────────

# yfinance symbol → (exact CSV item name, price multiplier to match CSV unit)
_LIVE_COMMODITY_SYMBOLS = {
    "GC=F":  ("金 (gold) US$/盎司",            1.0),       # Gold $/oz
    "SI=F":  ("銀 (silver) US$/盎司",          1.0),       # Silver $/oz
    "CL=F":  ("石油 西德州 ( US$/桶)",          1.0),       # WTI $/barrel
    "BZ=F":  ("石油 北海布蘭特 (US$/桶)",       1.0),       # Brent $/barrel
    # HG=F, ALI=F removed — now uses dedicated LME fetchers
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
_BOT_BCD_CODES = {
    "130041": ("ABS聚合物(注塑) 中國到岸價 US$/tonne", 1.0),   # ABS China CIF
    "190020": ("NOREXECO 長纖紙漿  USD/T",             1.0),   # Long-fiber pulp
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
    """Fetch cobalt price from metals.live API (LME data).
    Primary: metals.live API (LME settlement prices)
    Fallback: Trading Economics
    """
    try:
        r = req_lib.get("https://api.metals.live/v1/spot/cobalt", timeout=10)
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, dict) and "price" in data:
                price = float(data["price"])
                if price > 0:
                    logger.info(f"Cobalt from metals.live (LME): ${price}")
                    return price
    except Exception as e:
        logger.debug(f"metals.live cobalt fetch: {e}")

    # Fallback: Try Trading Economics
    try:
        price = _fetch_te_price("cobalt")
        if price and price > 0:
            logger.info(f"Cobalt from Trading Economics (fallback): ${price}")
            return price
    except Exception as e:
        logger.debug(f"Trading Economics cobalt fallback: {e}")

    return None


def _fetch_aluminum_price() -> float | None:
    """Fetch aluminum price from metals.live API (LME data).
    Primary: metals.live API (LME settlement prices)
    Fallback: Trading Economics
    """
    try:
        r = req_lib.get("https://api.metals.live/v1/spot/aluminum", timeout=10)
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, dict) and "price" in data:
                price = float(data["price"])
                if price > 0:
                    logger.info(f"Aluminum from metals.live (LME): ${price}")
                    return price
    except Exception as e:
        logger.debug(f"metals.live aluminum fetch: {e}")

    # Fallback: Try Trading Economics
    try:
        price = _fetch_te_price("aluminum")
        if price and price > 0:
            logger.info(f"Aluminum from Trading Economics (fallback): ${price}")
            return price
    except Exception as e:
        logger.debug(f"Trading Economics aluminum fallback: {e}")

    return None


def _fetch_copper_price() -> float | None:
    """Fetch copper price from metals.live API (LME data).
    Primary: metals.live API (uses LME settlement prices)
    """
    try:
        r = req_lib.get("https://api.metals.live/v1/spot/copper", timeout=10)
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, dict) and "price" in data:
                price = float(data["price"])
                if price > 0:
                    logger.info(f"Copper from metals.live (LME): ${price}")
                    return price
    except Exception as e:
        logger.debug(f"metals.live copper fetch: {e}")
    return None


def _fetch_lme_metal_price(metal_name: str, metals_live_slug: str) -> float | None:
    """Generic LME metal price fetcher using metals.live API.
    Args:
        metal_name: Display name (e.g., "Tin", "Nickel")
        metals_live_slug: metals.live API slug (e.g., "tin", "nickel")
    Returns:
        Price in USD or None if fetch fails
    """
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
        logger.debug(f"metals.live {metal_name} fetch: {e}")
    return None


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
    """Fetch tungsten price from reliable sources.
    Primary: ebaiyin (1#钨条 tungsten rod)
    Fallback: SMM (国产钨粉 tungsten powder) - but SMM uses JS rendering, may not work
    Returns price in CNY/kg or None if fetch fails.
    """
    # Primary: Try ebaiyin first (more reliable)
    try:
        ebaiyin_price, _, _ = _fetch_ebaiyin_tungsten()
        if ebaiyin_price and ebaiyin_price > 0:
            logger.info(f"Tungsten from ebaiyin (1#钨条): {ebaiyin_price} CNY/kg")
            return ebaiyin_price
    except Exception as e:
        logger.debug(f"ebaiyin tungsten fetch: {e}")

    # Fallback: Try SMM if ebaiyin fails
    import re
    try:
        r = req_lib.get(
            "https://hq.smm.cn/h5/tungsten-powder-price",
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
                     "Accept-Language": "zh-CN,zh;q=0.9"},
            timeout=12
        )
        # Look for price patterns in the page
        patterns = [
            r'均价[：:]\s*([0-9.]+)',
            r'([0-9.]+)\s*元/千克',
            r'>(\d{3,}\.\d+)<',
        ]
        for pattern in patterns:
            matches = re.findall(pattern, r.text)
            for match in matches:
                try:
                    price = float(match.replace(',', ''))
                    if 100 < price < 500:  # Reasonable tungsten price range
                        logger.info(f"Tungsten from SMM (fallback): {price} CNY/kg")
                        return price
                except (ValueError, AttributeError):
                    continue
        logger.debug(f"SMM tungsten: no valid price pattern matched")
    except Exception as e:
        logger.debug(f"SMM tungsten fetch: {e}")

    return None


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
    lme_slugs = {"copper", "tin", "nickel", "zinc", "aluminum"}  # These will be fetched from LME below
    for slug, (csv_name, mult) in _TE_SLUGS.items():
        if slug in lme_slugs:
            continue  # Skip LME metals, fetch them separately
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

    logger.info("[REFRESH] Starting Copper (LME source)...")
    copper_name = "銅 (copper) US$/tonne"
    copper_price = _fetch_copper_price()
    if copper_price is not None:
        # LME copper is in USD/tonne, convert from $/lb if needed
        copper_val = round(copper_price, 2)
        with _live_cache_lock:
            prev = list(_live_commodity_cache.get(copper_name, []))
        existing_dates = {d for d, _ in prev}
        if today not in existing_dates:
            prev.append((today, copper_val))
        fresh[copper_name]   = prev
        sources[copper_name] = {"label": "LME (歷史)",
                                "url":   "https://www.lme.com"}
        logger.info(f"Copper: {copper_name} = {copper_val}")
    else:
        # If fetch fails, preserve existing cache
        with _live_cache_lock:
            existing = _live_commodity_cache.get(copper_name, [])
            if existing:
                fresh[copper_name] = list(existing)
                sources[copper_name] = {"label": "LME (cached)", "url": "https://www.lme.com"}
                logger.warning(f"Copper fetch failed, using cached data ({len(existing)} points)")

    logger.info("[REFRESH] Starting LME metals (Tin, Nickel, Zinc)...")
    lme_metals = {
        "錫 (tin) US$/tonne": ("Tin", "tin", 1.0),
        "鎳 (nickel)  US$/tonne": ("Nickel", "nickel", 1.0),
        "鋅 (zinc)  US$/tonne": ("Zinc", "zinc", 1.0),
    }
    for csv_name, (display_name, api_slug, mult) in lme_metals.items():
        price = _fetch_lme_metal_price(display_name, api_slug)
        if price is not None:
            val = round(price * mult, 2)
            with _live_cache_lock:
                prev = list(_live_commodity_cache.get(csv_name, []))
            existing_dates = {d for d, _ in prev}
            if today not in existing_dates:
                prev.append((today, val))
            fresh[csv_name]   = prev
            sources[csv_name] = {"label": "LME (歷史)",
                                "url":   "https://www.lme.com"}
            logger.info(f"LME: {csv_name} = {val}")
        else:
            # If fetch fails, preserve existing cache
            with _live_cache_lock:
                existing = _live_commodity_cache.get(csv_name, [])
                if existing:
                    fresh[csv_name] = list(existing)
                    sources[csv_name] = {"label": "LME (cached)", "url": "https://www.lme.com"}
                    logger.warning(f"{csv_name} fetch failed, using cached data")

    logger.info("[REFRESH] Starting Cobalt (website source)...")
    cobalt_name = "鈷 (cobalt) US$/tonne"
    cobalt_price = _fetch_cobalt_price()
    if cobalt_price is not None:
        cobalt_val = round(cobalt_price, 2)
        with _live_cache_lock:
            prev = list(_live_commodity_cache.get(cobalt_name, []))
        existing_dates = {d for d, _ in prev}
        if today not in existing_dates:
            prev.append((today, cobalt_val))
        fresh[cobalt_name]   = prev
        sources[cobalt_name] = {"label": "LME (metals.live API fallback)",
                                "url":   "https://www.lme.com"}
        logger.info(f"Cobalt: {cobalt_name} = {cobalt_val}")
    else:
        # If fetch fails, preserve existing cache (don't drop it)
        with _live_cache_lock:
            existing = _live_commodity_cache.get(cobalt_name, [])
            if existing:
                fresh[cobalt_name] = list(existing)
                sources[cobalt_name] = {"label": "LME (cached)",
                                        "url":   "https://www.lme.com"}
                logger.warning(f"Cobalt fetch failed, using cached data ({len(existing)} points)")
            else:
                logger.warning("Cobalt price fetch failed from all sources and no cache available")

    logger.info("[REFRESH] Starting Aluminum (LME source)...")
    aluminum_name = "鋁 (aluminum) US$/tonne"
    aluminum_price = _fetch_aluminum_price()
    if aluminum_price is not None:
        aluminum_val = round(aluminum_price, 2)
        with _live_cache_lock:
            prev = list(_live_commodity_cache.get(aluminum_name, []))
        existing_dates = {d for d, _ in prev}
        if today not in existing_dates:
            prev.append((today, aluminum_val))
        fresh[aluminum_name]   = prev
        sources[aluminum_name] = {"label": "LME (metals.live API fallback)",
                                  "url":   "https://www.lme.com"}
        logger.info(f"Aluminum: {aluminum_name} = {aluminum_val}")
    else:
        # If fetch fails, preserve existing cache (don't drop it)
        with _live_cache_lock:
            existing = _live_commodity_cache.get(aluminum_name, [])
            if existing:
                fresh[aluminum_name] = list(existing)
                sources[aluminum_name] = {"label": "LME (cached)",
                                          "url":   "https://www.lme.com"}
                logger.warning(f"Aluminum fetch failed, using cached data ({len(existing)} points)")
            else:
                logger.warning("Aluminum price fetch failed from all sources and no cache available")

    logger.info("[REFRESH] Starting Tungsten (ebaiyin 1#钨条 + SMM fallback)...")
    tungsten_name = "鎢"
    tungsten_source = {"label": "八百易 ebaiyin (1#钨条)", "url": "https://www.ebaiyin.com/quote/wu.shtml"}
    tungsten_price = _fetch_smm_tungsten_powder_price()

    if tungsten_price is not None:
        tungsten_val = round(tungsten_price, 2)
        with _live_cache_lock:
            prev = list(_live_commodity_cache.get(tungsten_name, []))
        existing_dates = {d for d, _ in prev}
        if today not in existing_dates:
            prev.append((today, tungsten_val))
            fresh[tungsten_name] = prev
        else:
            # Update today's price if it already exists
            fresh[tungsten_name] = [(d if d != today else today, tungsten_val if d == today else p) for d, p in prev]
        sources[tungsten_name] = tungsten_source
        logger.info(f"Tungsten: {tungsten_val} CNY/kg (ebaiyin)")
    else:
        # If fetch fails, preserve existing cache or create placeholder
        with _live_cache_lock:
            existing = _live_commodity_cache.get(tungsten_name, [])
        if existing:
            fresh[tungsten_name] = list(existing)
            sources[tungsten_name] = {"label": "八百易 ebaiyin (1#钨条) [cached]", "url": "https://www.ebaiyin.com/quote/wu.shtml"}
            logger.warning(f"Tungsten fetch failed, using cached data ({len(existing)} points)")
        else:
            # No existing cache — create placeholder entry so tungsten appears in API
            fresh[tungsten_name] = []  # Empty list, will be populated with CSV data if available
            sources[tungsten_name] = {"label": "八百易 ebaiyin (1#钨条)", "url": "https://www.ebaiyin.com/quote/wu.shtml"}
            logger.warning("Tungsten: fetch failed, no cache available")

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
    # Invalidate CSV parse cache so next request re-merges fresh live data
    with _csv_parse_lock:
        _csv_parse_cache["data"] = None
    logger.info(f"Live prices updated: {len(fresh)} items")
    logger.info("[REFRESH] Done!")


def _live_price_loop():
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
    "原物料": ["黃磷", "ABS聚合物", "PC塑料", "PC/ABS塑料", "NOREXECO 長纖紙漿", "瓦楞芯紙"],
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

        today = datetime.now()
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


@app.route("/api/commodity-news")
def api_commodity_news():
    """Search Google News for commodity-related articles."""
    import xml.etree.ElementTree as ET
    from urllib.parse import quote
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"articles": []})
    url = f"https://news.google.com/rss/search?q={quote(q)}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant&num=10"
    try:
        resp = req_lib.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        root = ET.fromstring(resp.content)
        articles = []
        for item in root.findall(".//item")[:8]:
            title = item.findtext("title") or ""
            link  = item.findtext("link") or ""
            pub   = item.findtext("pubDate") or ""
            articles.append({"title": title, "source_url": link,
                             "published": pub, "source": "Google News"})
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
    """Proxy GDACS floods and volcanic events (Orange/Red alerts only)."""
    try:
        r = req_lib.get(
            "https://www.gdacs.org/gdacsapi/api/events/geteventlist/SEARCH"
            "?eventlist=FL;VO;TC&alertlevel=Orange;Red&limit=40",
            timeout=8,
        )
        return r.content, r.status_code, {"Content-Type": "application/json"}
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

_GEO_RISKS = [
    {"id":"geo-redsea",  "kw":["Houthi Red Sea ship attack","Red Sea shipping attack"],
     "title":"紅海航運威脅（胡塞武裝）","type":"war","lat":14.5,"lng":42.5,"region":"葉門/紅海",
     "impact":"CRITICAL","supply":"亞歐航程延長10-14天，運費上漲200-400%，建議改走好望角或提前備貨"},
    {"id":"geo-taiwan",  "kw":["PLA Taiwan Strait military","China Taiwan military exercise"],
     "title":"台灣海峽地緣緊張","type":"war","lat":24.0,"lng":122.0,"region":"東亞",
     "impact":"HIGH","supply":"全球半導體（TSMC等）供應鏈最高風險區"},
    {"id":"geo-iran",    "kw":["Iran Israel attack war","Iran US military strike","Iran attack Israel"],
     "title":"伊朗地區衝突","type":"war","lat":32.0,"lng":53.0,"region":"中東/波斯灣",
     "impact":"HIGH","supply":"荷姆茲海峽石油供應威脅，波斯灣航運風險"},
    {"id":"geo-ukraine", "kw":["Ukraine Russia war attack","Russia Ukraine missile"],
     "title":"俄烏戰爭","type":"war","lat":49.0,"lng":32.0,"region":"東歐",
     "impact":"CRITICAL","supply":"穀物、化肥、氖氣供應中斷；黑海航運受限"},
    {"id":"geo-drc",     "kw":["DRC Congo M23 conflict cobalt","Congo mineral conflict"],
     "title":"剛果衝突（礦產風險）","type":"war","lat":-1.5,"lng":29.0,"region":"中非",
     "impact":"HIGH","supply":"鈷、鋰等電池礦產供應不穩定"},
    {"id":"geo-myanmar", "kw":["Myanmar civil war military","Myanmar junta conflict"],
     "title":"緬甸內戰","type":"war","lat":19.8,"lng":96.2,"region":"東南亞",
     "impact":"HIGH","supply":"稀土、天然氣出口受阻；紡織供應鏈中斷"},
    {"id":"geo-india-pak",
     "kw":["India Pakistan military tension border","India Pakistan conflict"],
     "title":"印巴邊境緊張","type":"war","lat":30.0,"lng":71.0,"region":"南亞",
     "impact":"MED","supply":"南亞製造業（電子/紡織）物流中斷風險"},
]

def _scan_one_geo_risk(risk, headers, cutoff):
    """Scan Google News for one geopolitical risk entry. Returns result dict or None."""
    import xml.etree.ElementTree as ET
    from urllib.parse import quote
    from email.utils import parsedate_to_datetime
    found_date = ""
    for kw in risk["kw"]:
        try:
            url = f"https://news.google.com/rss/search?q={quote(kw)}&hl=en-US&gl=US&ceid=US:en"
            r = req_lib.get(url, timeout=5, headers=headers)
            items = ET.fromstring(r.content).findall('.//item')[:5]
            logger.info(f"[GEO] {risk['title']} + '{kw}': {len(items)} items (status {r.status_code})")
            for item in items:
                pub = item.findtext('pubDate', '')
                try:
                    dt = parsedate_to_datetime(pub)
                    if dt >= cutoff:
                        found_date = str(dt.date())
                        logger.info(f"[GEO] ✓ {risk['title']}: found recent article")
                        break
                except Exception:
                    found_date = "持續"
                    logger.info(f"[GEO] ✓ {risk['title']}: ongoing (no date)")
                    break
            if found_date:
                break
        except Exception as e:
            logger.warning(f"[GEO] {risk['title']} + '{kw}' ERROR: {type(e).__name__}: {e}")
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
        "source": "Google News自動監測",
        "sourceUrl": f"https://news.google.com/search?q={_q(risk['kw'][0])}",
    }


def _do_geo_scan():
    """Run parallel geopolitical scan and update cache. Returns results list."""
    from datetime import datetime, timezone, timedelta
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
    }
    cutoff = datetime.now(timezone.utc) - timedelta(days=45)
    results = []
    executor = ThreadPoolExecutor(max_workers=min(3, len(_GEO_RISKS)))  # Limit to 3 parallel
    try:
        futs = [executor.submit(_scan_one_geo_risk, risk, headers, cutoff)
                for risk in _GEO_RISKS]
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
        _geo_risk_cache["data"] = results
        _geo_risk_cache["ts"] = time.time()
    logger.info(f"Geopolitical risks detected: {len(results)}/{len(_GEO_RISKS)}")
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
    """Scan Google News for one strike target. Returns result dict or None."""
    import xml.etree.ElementTree as ET
    from urllib.parse import quote
    from email.utils import parsedate_to_datetime
    found_article = None
    for kw in target["kw"]:
        try:
            url = f"https://news.google.com/rss/search?q={quote(kw)}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
            r = req_lib.get(url, timeout=6, headers=headers)
            root = ET.fromstring(r.content)
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
            if found_article:
                break
        except Exception as e:
            logger.warning(f"[STRIKE] {target['company']} + '{kw}' ERROR: {type(e).__name__}: {e}")
    if not found_article:
        logger.info(f"[STRIKE] ✗ {target['company']}: no matching articles")
        return None
    return {
        "id":        f"strike-{target['company']}",
        "type":      "strike",
        "title":     f"{target['company']} 罷工事件",
        "lat":       target["lat"], "lng": target["lng"],
        "region":    target["region"],
        "time":      found_article["date"],
        "impact":    "HIGH",
        "supply":    f"{target['company']}勞資衝突，可能影響生產排程與出貨交期，建議評估替代供應",
        "source":    "Google News自動監測",
        "sourceUrl": found_article["url"],
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
    executor = ThreadPoolExecutor(max_workers=min(3, len(_STRIKE_TARGETS)))  # Limit to 3 parallel
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
        _strike_cache["data"] = results
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
    """Return parsed CSV + live data, with 5-min in-memory cache."""
    with _csv_parse_lock:
        if _csv_parse_cache["data"] is not None and time.time() - _csv_parse_cache["ts"] < 300:
            return _csv_parse_cache["data"]
        data = _parse_commodity_csv()
        _csv_parse_cache["data"] = data
        _csv_parse_cache["ts"]   = time.time()
        return data


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
    {"id": "shenzhen",   "name": "深圳",     "name_en": "Shenzhen",       "lat": 22.54, "lng": 114.06, "industries": ["消費電子", "PCB"],         "region": "CN"},
    {"id": "kunshan",    "name": "昆山",     "name_en": "Kunshan",        "lat": 31.39, "lng": 121.16, "industries": ["PCB", "NB代工"],          "region": "CN"},
    {"id": "zhengzhou",  "name": "鄭州",     "name_en": "Zhengzhou",      "lat": 34.75, "lng": 113.62, "industries": ["手機組裝", "EMS"],         "region": "CN"},
    {"id": "shanghai",   "name": "上海",     "name_en": "Shanghai",       "lat": 31.23, "lng": 121.47, "industries": ["汽車電子", "IC設計"],      "region": "CN"},
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
    "日本": ["kumamoto", "osaka"],  # 日本集群
    "韓國": ["pyeongtaek", "icheon"],  # 韓國集群
    "馬來西亞": ["penang"],  # 馬來西亞集群
    "美國": ["san_jose", "austin"],  # 美國集群
    "德國": ["dresden"],  # 德國集群
    "荷蘭": ["eindhoven"],  # 荷蘭集群
}

_REGION_LABELS = {
    "TW": "🇹🇼 台灣", "CN": "🇨🇳 中國", "KR": "🇰🇷 韓國",
    "JP": "🇯🇵 日本", "US": "🇺🇸 美國", "MY": "🇲🇾 東南亞", "EU": "🇪🇺 歐洲",
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
            if rtype == "disaster" and any(tk.lower() in text for tk in _TYPHOON_KEYWORDS):
                # Typhoon/flood only count if: (a) not pure forecast, AND (b) has severity keywords
                # AND (c) within 3 days (as per user requirement)
                if days_old > 3:
                    continue  # Skip typhoon/flood events older than 3 days
                if not is_typhoon_forecast and any(sk.lower() in text for sk in _DISASTER_SEVERITY_KEYWORDS):
                    risk_found = True
            elif rtype == "disaster":
                # Other disasters always count
                if not any(tk.lower() in text for tk in _TYPHOON_KEYWORDS):
                    risk_found = True
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

        # Detect risk types with special handling for typhoon/flood (require severity keywords)
        risk_types = []
        for rt, rkws in _RISK_KEYWORDS.items():
            if any(rk.lower() in text for rk in rkws):
                # For disaster: typhoon/flood only count if paired with severity keywords
                if rt == "disaster" and any(tk.lower() in text for tk in _TYPHOON_KEYWORDS):
                    if any(sk.lower() in text for sk in _DISASTER_SEVERITY_KEYWORDS):
                        risk_types.append(rt)
                elif rt == "disaster":
                    # Other disasters (earthquake, tsunami, etc.) always count
                    if not any(tk.lower() in text for tk in _TYPHOON_KEYWORDS):
                        risk_types.append(rt)
                    # Or earthquake/tsunami with severity
                    elif any(sk.lower() in text for sk in _DISASTER_SEVERITY_KEYWORDS):
                        risk_types.append(rt)
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

    clusters_out = [{**c, "risk_score": cluster_scores.get(c["id"], 0)}
                    for c in _SUPPLY_CHAIN_CLUSTERS]

    # Get cached strikes and geopolitical risks
    with _strike_lock:
        strikes = _strike_cache.get("data", []) or []
    with _geo_risk_lock:
        geo_risks = _geo_risk_cache.get("data", []) or []

    # Combine specific events (strikes + geo risks) for the map
    specific_events = []
    for event in strikes + geo_risks:
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
    logger.info("Background threads started")

if __name__ == "__main__":
    logger.info("Fetching initial live prices...")
    _refresh_live_prices()
    # Pre-warm risk caches in background so first page visit is fast
    threading.Thread(target=_risk_cache_preload_loop, daemon=True).start()
    app.run(host="0.0.0.0", debug=False, port=5050, use_reloader=False)
