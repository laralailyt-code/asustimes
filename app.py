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
                logger.info("Background threads started in worker")


# ── Routes ─────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/ping")
def api_ping():
    """Lightweight keep-alive endpoint for uptime monitors."""
    return jsonify({"ok": True})


@app.route("/api/debug-snippets")
def debug_snippets():
    """Diagnose snippet fetching on this server."""
    from scraper import _resolve_google_news_url, _fetch_snippet, _summary_is_empty
    with _cache_lock:
        articles = list(_cache.get("articles", []))

    # Test: inspect the Google News page HTML for JS redirect patterns
    redirect_test = {}
    for a in articles[:15]:
        gurl = a.get("source_url", "")
        if gurl and "news.google.com" in gurl:
            try:
                import re as _re
                resp = req_lib.get(gurl, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36", "Accept": "text/html"}, timeout=8, allow_redirects=True)
                text = resp.text
                total_len = len(text)
                # Search for any http URLs in the page that aren't google.com
                non_google_urls = _re.findall(r'https?://(?!(?:www\.)?google\.com)[^\s"\'<>]{20,}', text)
                # Look for common redirect patterns
                patterns_found = {}
                for pat_name, pat in [
                    ("window.location", r'window\.location[^;]{0,60}https?://[^"\']{20,}'),
                    ("meta_refresh", r'http-equiv=["\']?refresh[^\>]+url=[^\s"\']{10,}'),
                    ("json_url", r'"url"\s*:\s*"(https?://[^"]{20,})"'),
                    ("data_n", r'data-n-[a-z]+="(https?://[^"]{20,})"'),
                    ("href_external", r'href="(https?://(?!(?:www\.)?google\.com)[^"]{20,})"'),
                ]:
                    m = _re.search(pat, text, _re.IGNORECASE)
                    if m:
                        patterns_found[pat_name] = m.group(0)[:120]
                redirect_test = {
                    "status": resp.status_code,
                    "final_url": resp.url[:120],
                    "still_google": "news.google.com" in resp.url,
                    "total_html_len": total_len,
                    "non_google_urls": non_google_urls[:5],
                    "patterns_found": patterns_found,
                    "body_sample": text[600:1400],
                }
            except Exception as e:
                redirect_test = {"error": str(e)[:100]}
            break

    results = []
    for a in articles[:6]:
        url = a.get("source_url", "")
        resolved = _resolve_google_news_url(url) if url else ""
        snippet = _fetch_snippet(url) if url else ""
        results.append({
            "title": a["title"][:60],
            "needs_enrich": _summary_is_empty(a["title"], a.get("summary", "")),
            "resolved_changed": resolved != url,
            "resolved_url": resolved[:100],
            "snippet": snippet[:120] if snippet else "",
        })
    return jsonify({"server": "render", "count": len(articles), "redirect_test": redirect_test, "sample": results})


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


@app.route("/api/digest")
def api_digest():
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

    # Prefer today's articles; fall back to latest
    cat_articles = [
        a for a in all_articles
        if a.get("category") == category
        and (a.get("published") or a.get("fetched_at", ""))[:10] == today
    ]
    if len(cat_articles) < 3:
        cat_articles = [a for a in all_articles if a.get("category") == category][:12]

    if not cat_articles:
        return jsonify({"category": category, "points": [], "articles": [], "ai_powered": False})

    top = cat_articles[:10]
    article_links = [
        {
            "title":     a["title"],
            "url":       a.get("source_url", ""),
            "source":    a.get("source", ""),
            "published": (a.get("published") or "")[:10],
        }
        for a in cat_articles[:6]
    ]

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    points: list[str] = []
    ai_powered = False

    if api_key and _ANTHROPIC_AVAILABLE:
        try:
            articles_text = "\n".join([
                f"{i+1}. {a['title']}：{a.get('summary', '')}"
                for i, a in enumerate(top)
            ])
            client = _anthropic_lib.Anthropic(api_key=api_key)
            msg = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=600,
                messages=[{
                    "role": "user",
                    "content": (
                        f"以下是「{category}」類別的最新科技新聞：\n\n{articles_text}\n\n"
                        "請用繁體中文，條列出4-5條今日最重要的新聞重點。\n"
                        "要求：每條重點用一句話（30-60字）說明核心資訊與產業意義；"
                        "聚焦不同主題，避免重複；每條前加「•」；只輸出條列內容，不要標題或說明。"
                    ),
                }],
            )
            raw = msg.content[0].text.strip()
            points = [
                line.strip().lstrip("•·▪▸►→- ").strip()
                for line in raw.split("\n")
                if line.strip() and len(line.strip()) > 8
            ]
            ai_powered = True
        except Exception as e:
            logger.warning(f"Digest AI error: {e}")

    # Fallback: clean RSS summary (often just title+source), fetch snippet if still empty
    if not points:
        import re as _re
        from concurrent.futures import ThreadPoolExecutor as _TPE, as_completed as _ac

        def _norm(t: str) -> str:
            """Normalize for comparison: remove dashes/spaces/punctuation, lowercase."""
            return _re.sub(r'[\s\-–—·|·•]+', '', t).lower()

        def _clean_rss_summary(title: str, raw: str) -> str:
            """Remove title/source repetition common in Google News RSS descriptions."""
            s = raw.strip()
            if not s:
                return ""
            # Normalized comparison handles "Title - Source" vs "Title Source" variants
            t_norm = _norm(title)
            s_norm = _norm(s)
            if s_norm.startswith(t_norm) or s_norm == t_norm:
                # Discard the title-equivalent portion from the start
                s = s[len(title):].lstrip(" -–—\t").strip() if len(s) > len(title) else ""
            elif s.lower().startswith(title.lower()):
                s = s[len(title):].lstrip(" -–—\t").strip()
            # Strip trailing source suffix like "- DIGITIMES" or "TechNews 科技新報"
            s = _re.sub(r'\s*[-–—]\s*\S[\w\s]{1,30}$', '', s).strip()
            return s

        candidates: list[tuple] = []  # (article, title, snippet)
        for a in top[:5]:
            title = a["title"]
            snippet = _clean_rss_summary(title, a.get("summary") or "")
            candidates.append((a, title, snippet))

        # Fetch article snippets in parallel for items with no useful summary
        needs_fetch = [(i, a) for i, (a, title, snippet) in enumerate(candidates) if len(snippet) < 25]
        if needs_fetch:
            with _TPE(max_workers=min(len(needs_fetch), 5)) as ex:
                futs = {ex.submit(_fetch_article_snippet, a.get("source_url", "")): i
                        for i, a in needs_fetch if a.get("source_url")}
                for fut in _ac(futs, timeout=12):
                    idx = futs[fut]
                    try:
                        fetched = fut.result()
                        if fetched:
                            a, title, _ = candidates[idx]
                            candidates[idx] = (a, title, fetched)
                    except Exception:
                        pass

        for a, title, snippet in candidates:
            if snippet and len(snippet) > 25:
                points.append(f"{title}　{snippet}")
            else:
                points.append(title)

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
    "HG=F":  ("銅 (copper) US$/tonne",         2204.62),   # Copper $/lb → $/tonne
    "ALI=F": ("鋁 (aluminum) US$/tonne",       1.0),       # COMEX Aluminum $/tonne
}

# Free ExchangeRate-API code → exact CSV item name
_LIVE_FX_CODES = {
    "TWD": "美元 / 台幣",
    "CNY": "美元 / 人民幣",
    "JPY": "美元 / 日圓",
    "EUR": "美元 / 歐元",          # stored as EUR/USD (how many EUR per 1 USD)
    "BRL": "美元 / 巴西里爾(巴西幣)",
    "KRW": "美元 / 韓圜",
    "IDR": "美元 / 印尼盾",
    "INR": "美元 / 印度幣",
}

_live_commodity_cache: dict = {}   # {csv_item_name: [(date_str, value)]}
_live_cache_lock = threading.Lock()

# bot.com.tw BCD API code → (csv_item_name, price_multiplier)
# API: https://fund.bot.com.tw/Z/ZH/ZHG/CZHG.djbcd?A=<code>
# Response format: "date1,date2,...,dateN,val1,val2,...,valN"
_BOT_BCD_CODES = {
    "130041": ("ABS聚合物(注塑) 中國到岸價 US$/tonne", 1.0),   # ABS China CIF ✓ confirmed
    "190020": ("NOREXECO 長纖紙漿  USD/T",             1.0),   # Long-fiber pulp ✓ bot.com.tw
}

# Codes that need full historical data (not just latest point)
_BOT_BCD_HISTORY_CODES = {
    "190060": ("瓦楞芯紙 CNY$/tonne", 1.0),   # Corrugated paper — full history
}

# Trading Economics slug → (csv_item_name, price_multiplier)
# Prices are scraped from tradingeconomics.com/commodity/<slug>
_TE_SLUGS = {
    "tin":        ("錫 (tin) US$/tonne",         1.0),       # TE in USD/tonne ✓
    "nickel":     ("鎳 (nickel)  US$/tonne",     1.0),       # TE in USD/tonne ✓
    "zinc":       ("鋅 (zinc)  US$/tonne",       1.0),       # TE in USD/tonne ✓
    "cobalt":     ("鈷 (cobalt) US$/tonne",      1.0),       # TE in USD/tonne ✓
    "lithium":    ("鋰 (Lithium) CNY$/tonne",    1.0),       # TE in CNY/tonne ✓
    "phosphorus": ("黃磷 CNY$/tonne",            29.4274),   # TE in CNY/百kg → CNY/tonne
}


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


def _refresh_live_prices():
    """Fetch latest commodity & FX prices. Called once on startup and then daily."""
    today = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
    fresh: dict = {}

    # 1. Commodity prices via yfinance (per-symbol to avoid bulk rate limiting)
    if _YF_AVAILABLE:
        for sym, (csv_name, mult) in _LIVE_COMMODITY_SYMBOLS.items():
            for attempt in range(2):
                try:
                    hist = yf.Ticker(sym).history(period="5d", interval="1d", auto_adjust=True)
                    series = hist["Close"].dropna() if "Close" in hist.columns else hist.dropna()
                    if series.empty:
                        break
                    price = round(float(series.iloc[-1]) * mult, 2)
                    date  = str(series.index[-1].date())
                    fresh.setdefault(csv_name, []).append((date, price))
                    logger.info(f"yfinance {sym}: {csv_name} = {price} on {date}")
                    break
                except Exception as e:
                    if attempt == 0 and "RateLimit" in type(e).__name__:
                        logger.warning(f"yfinance {sym} rate limited, retrying in 15s")
                        time.sleep(15)
                    else:
                        logger.warning(f"yfinance {sym}: {e}")
                        break
            time.sleep(1)  # 1s between symbols to avoid rate limiting

    # 2. bot.com.tw BCD API (latest price only)
    for code, (csv_name, mult) in _BOT_BCD_CODES.items():
        price = _fetch_bot_bcd_price(code)
        if price is not None:
            val = round(price * mult, 2)
            fresh[csv_name] = [(today, val)]
            logger.info(f"bot.com.tw BCD {code}: {csv_name} = {val}")

    # 2b. bot.com.tw BCD API (full historical series)
    for code, (csv_name, mult) in _BOT_BCD_HISTORY_CODES.items():
        history = _fetch_bot_bcd_history(code)
        if history:
            fresh[csv_name] = [(d, round(v * mult, 2)) for d, v in history]
            logger.info(f"bot.com.tw BCD history {code}: {csv_name}, {len(history)} pts")

    # 3. buyplas.com plastic prices
    _BUYPLAS_ITEMS = {
        "PC_SABIC":     "PC塑料 (SABIC) CNY$/tonne",
        "PC_ABS_SABIC": "PC/ABS塑料 (SABIC) CNY$/tonne",
    }

    for key, csv_name in _BUYPLAS_ITEMS.items():
        price = _fetch_buyplas_price(key)
        if price is not None:
            fresh[csv_name] = [(today, price)]
            logger.info(f"buyplas.com {key}: {csv_name} = {price}")
        else:
            logger.warning(f"buyplas.com {key}: no price found")

    # 4. LME metals + lithium + phosphorus via Trading Economics scrape
    for slug, (csv_name, mult) in _TE_SLUGS.items():
        price = _fetch_te_price(slug)
        if price is not None:
            val = round(price * mult, 2)
            fresh[csv_name] = [(today, val)]
            logger.info(f"TradingEconomics: {csv_name} = {val}")

    # 5. FX rates via free ExchangeRate-API
    try:
        resp  = req_lib.get("https://api.exchangerate-api.com/v4/latest/USD", timeout=10)
        rates = resp.json().get("rates", {})
        for code, csv_name in _LIVE_FX_CODES.items():
            if code == "EUR":
                val = round(1.0 / rates["EUR"], 4) if rates.get("EUR") else None
            else:
                val = rates.get(code)
            if val is not None:
                fresh[csv_name] = [(today, float(val))]
    except Exception as e:
        logger.warning(f"FX rate fetch error: {e}")

    with _live_cache_lock:
        _live_commodity_cache.update(fresh)
    logger.info(f"Live prices updated: {len(fresh)} items")


def _live_price_loop():
    _refresh_live_prices()
    # Refresh at 08:00, 12:00, 15:00 Taiwan time (UTC+8) every day
    _REFRESH_HOURS = {8, 12, 15}
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
    "金屬": ["銅", "錫", "鋁", "鎳", "鋅", "鈷", "鋰"],
    "貴金屬": ["金", "銀"],
    "能源": ["石油 西德州", "石油 杜拜", "石油 北海布蘭特"],
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

            # Pair dates with values, skip None dates
            paired = [(d, v) for d, v in zip(dates, values) if d is not None]

            result[name] = {
                "unit":     unit,
                "category": cat,
                "dates":    [p[0] for p in paired],
                "values":   [p[1] for p in paired],
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


@app.route("/api/commodities")
def api_commodities():
    # If live cache is empty (first load), fetch synchronously before serving
    with _live_cache_lock:
        cache_empty = not _live_commodity_cache
    if cache_empty:
        _refresh_live_prices()
    data = _parse_commodity_csv()
    # Return items list with latest value for overview
    items = []
    for name, d in data.items():
        # Latest non-null value
        latest = next((v for v in reversed(d["values"]) if v is not None), None)
        prev   = next((v for v in reversed(d["values"][:-1]) if v is not None), None)
        change = round(((latest - prev) / prev * 100), 2) if latest and prev and prev != 0 else None
        items.append({
            "name":     name,
            "unit":     d["unit"],
            "category": d["category"],
            "latest":   latest,
            "change":   change,
            "dates":    d["dates"],
            "values":   d["values"],
        })
    categories = list(_COMMODITY_CATEGORIES.keys())
    return jsonify({"items": items, "categories": categories})


if __name__ == "__main__":
    app.run(host="0.0.0.0", debug=False, port=5050, use_reloader=False)
