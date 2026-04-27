"""
ASUSTIMES — News scraper
Only tech-industry news relevant to ASUS executives.
Categories: AI產業 / 記憶體儲存 / 半導體 / PC_NB / 伺服器雲端 / 面板顯示 / 電競ROG / 供應鏈關稅 / 財報法說
"""

import os
import re
import csv
import html
import logging
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime

TW_TZ = timezone(timedelta(hours=8))
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeoutError

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
}
TIMEOUT = 10

# ── ASUS-relevant categories ─────────────────────────────────────────────────
CATEGORY_KEYWORDS = {
    "AI 產業": [
        "AI", "人工智慧", "機器學習", "大語言模型", "LLM", "ChatGPT", "Gemini", "Grok",
        "inference", "推論", "NPU", "AI PC", "AI伺服器", "AI server", "算力",
        "NVIDIA", "H100", "H200", "Blackwell", "GB200", "RTX 50", "Rubin",
        "AMD", "MI300", "MI350", "生成式AI", "GenAI", "Copilot", "RAG",
        "transformer", "深度學習", "神經網路", "基礎模型", "foundation model",
        "AI晶片", "AI chip", "Edge AI", "AIoT",
    ],
    "記憶體/儲存": [
        "DRAM", "記憶體", "HBM", "HBM3", "HBM3E", "HBM4", "NAND", "SSD", "Flash",
        "Micron", "Samsung", "SK Hynix", "海力士", "南亞科", "華邦電",
        "DDR5", "DDR6", "LPDDR5", "LPDDR6", "儲存", "storage", "固態硬碟",
        "eMMC", "UFS", "記憶體模組", "DIMM",
        "NAND Flash", "3D NAND", "QLC", "TLC",
    ],
    "半導體": [
        "台積電", "TSMC", "半導體", "晶片", "IC", "晶圓", "製程", "封測",
        "CoWoS", "SoIC", "先進封裝", "EUV", "High-NA", "N2", "N3", "3奈米", "2奈米",
        "聯發科", "MediaTek", "高通", "Qualcomm", "Intel", "Broadcom",
        "三星晶圓", "GlobalFoundries", "UMC", "聯電", "foundry", "IC設計",
        "ASML", "光刻機", "Arm", "RISC-V",
    ],
    "PC / NB": [
        "筆電", "NB", "notebook", "laptop", "桌機", "desktop PC", "個人電腦",
        "PC出貨", "出貨量", "Chromebook", "Windows 11", "macOS",
        "HP", "Dell", "Lenovo", "聯想", "Acer", "宏碁", "ASUS筆電",
        "AI PC", "Copilot+", "二合一筆電", "商務筆電", "輕薄筆電",
        "Core Ultra", "Ryzen AI", "Snapdragon X",
    ],
    "伺服器/雲端": [
        "伺服器", "server", "資料中心", "data center", "雲端", "cloud",
        "AWS", "Azure", "Google Cloud", "GCP", "超大規模", "hyperscaler",
        "機架", "rack", "散熱", "液冷", "浸沒式冷卻",
        "鴻海", "廣達", "英業達", "緯穎", "緯創", "雲達", "Wiwynn",
        "基礎設施", "infrastructure", "GPU server",
    ],
    "面板/顯示": [
        "面板", "LCD", "OLED", "Mini LED", "MiniLED", "Micro LED", "AMOLED", "QD-OLED",
        "AUO", "友達", "群創", "Innolux", "顯示器", "monitor", "螢幕",
        "解析度", "4K", "8K", "刷新率", "HDR", "色域", "display",
        "電視面板", "車用面板", "折疊螢幕",
    ],
    "電競/ROG": [
        "電競", "gaming", "遊戲硬體", "ROG", "Republic of Gamers", "TUF Gaming",
        "顯卡", "繪圖卡", "RTX", "GeForce", "Radeon", "RX 9",
        "電競筆電", "電競螢幕", "機械鍵盤", "電競滑鼠", "電競耳機",
        "esports", "FPS", "幀率", "高刷",
        "InfiniGuard", "NAS", "網路儲存",
    ],
    "供應鏈/關稅": [
        "關稅", "tariff", "供應鏈", "supply chain", "貿易戰", "出口管制",
        "ODM", "OEM", "代工", "制裁", "禁令", "entity list", "晶片禁令",
        "移轉", "遷廠", "越南", "印度", "墨西哥", "轉單", "去中化",
        "產能利用率",
        "CCL", "PCB", "玻纖布", "銅箔", "覆銅板", "基板", "ABF", "載板",
        "罷工", "工人罷工", "工潮", "勞資爭議", "勞工抗議", "工會", "罷課",
        "strike", "labor strike", "workers strike", "walkout", "industrial action", "union",
    ],
    "財務風險": [
        "破產", "倒閉", "違約", "財務危機", "流動性危機", "債務重整", "欠款",
        "應收帳款", "呆帳", "信用評等下調", "評等調降", "停工", "停產",
        "大規模裁員", "財務困難", "週轉不靈", "跳票", "資金缺口", "資金周轉困難",
        "債務違約", "清算", "重整", "接管", "強制執行",
        "bankruptcy", "default", "liquidity crisis", "debt restructuring",
        "receivership", "insolvency", "financial distress", "credit downgrade",
        "mass layoffs", "shutdown", "seized",
    ],
    "財報/法說": [
        "財報", "法說會", "法說", "月營收", "季報", "年報", "EPS", "每股盈餘",
        "毛利率", "毛利", "營業利益", "淨利", "資本支出",
        "獲利", "虧損", "盈利", "年增", "季增", "年減", "季減",
        "Q1", "Q2", "Q3", "Q4", "業績", "財測", "展望", "營收",
        "revenue", "earnings", "profit", "guidance", "quarterly",
        "年成長", "創高", "創新低", "庫存", "去庫存",
    ],
    "ESG永續": [
        "ESG", "永續", "碳中和", "碳排放", "碳足跡", "淨零", "net zero",
        "再生能源", "綠能", "太陽能", "風電", "綠電", "RE100",
        "碳交易", "碳權", "碳費", "減碳", "溫室氣體",
        "企業社會責任", "CSR", "社會責任",
        "供應鏈碳排", "Scope 3", "永續報告書",
        "循環經濟", "廢棄物", "用水", "生物多樣性",
        "董事會多元", "獨立董事", "公司治理", "資訊揭露",
        "sustainability", "carbon neutral", "renewable", "green",
        "climate", "emission", "ESG report", "diversity",
    ],
}

# ── Non-tech blocklist (articles matching these are dropped if no tech match) ──
# NOTE: 颱風、地震、罷工 are supply chain risks, NOT filtered out
NON_TECH_SIGNALS = [
    "選舉", "民調", "立委", "縣市長", "政黨", "藍綠",
    "棒球", "籃球", "足球", "奧運", "世界盃", "體育賽",
    "娛樂", "藝人", "明星", "電影票房", "韓劇", "偶像",
    "美食", "餐廳", "食安", "咖啡廳",
    "房地產", "買房", "炒房", "房市",
    "醫療糾紛", "新冠疫苗", "醫院",
]

# ── Supply chain risk keywords (NOT filtered even without tech keywords) ──
_SUPPLY_CHAIN_RISK_KEYWORDS = {
    "typhoon":   ["颱風", "typhoon", "颶風", "hurricane"],
    "earthquake": ["地震", "earthquake"],
    "strike":    ["罷工", "工人罷工", "工潮", "strike", "labor strike"],
    "flood":     ["洪水", "水災", "flood"],
}


def is_chinese_text(text: str) -> bool:
    """Check if text contains Chinese characters."""
    return any('一' <= char <= '鿿' for char in text)


def translate_to_chinese(title: str, summary: str = "") -> tuple[str, str]:
    """Translate English title and summary to Chinese using Claude API.
    Returns: (translated_title, translated_summary)
    If translation fails or text is already Chinese, returns original.
    """
    try:
        import anthropic

        # Skip if already has significant Chinese
        if is_chinese_text(title) and is_chinese_text(summary):
            return title, summary

        # Build translation prompt
        prompt = f"""Translate the following to Traditional Chinese (繁體中文). Keep it concise.

Title: {title}
Summary: {summary}

Respond ONLY in this format (no other text):
TITLE: [translated title]
SUMMARY: [translated summary]"""

        client = anthropic.Anthropic()
        message = client.messages.create(
            model="claude-3-5-haiku-20241022",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}]
        )

        response = message.content[0].text

        # Parse response
        lines = response.strip().split('\n')
        translated_title = title
        translated_summary = summary

        for line in lines:
            if line.startswith("TITLE:"):
                translated_title = line.replace("TITLE:", "").strip()
            elif line.startswith("SUMMARY:"):
                translated_summary = line.replace("SUMMARY:", "").strip()

        logger.debug(f"Translated: '{title[:30]}...' → '{translated_title[:30]}...'")
        return translated_title, translated_summary

    except Exception as e:
        logger.warning(f"Translation failed for '{title[:40]}...': {type(e).__name__}: {e}")
        return title, summary


def classify_category(title: str, summary: str = "", hint: str = "") -> str | None:
    """Return matched category, or None if no tech keyword matches at all.
    Supply chain risks (strike, typhoon, earthquake, flood) are NEVER dropped.
    """
    text = f"{title} {summary}"   # hint excluded from scoring to avoid bias
    text_lower = text.lower()

    scores: dict[str, int] = {cat: 0 for cat in CATEGORY_KEYWORDS}
    for cat, keywords in CATEGORY_KEYWORDS.items():
        for kw in keywords:
            if kw.lower() in text_lower:
                scores[cat] += 1

    best = max(scores, key=scores.get)
    if scores[best] > 0:
        return best

    # Check if this is a supply chain risk (never filter out)
    for risk_type, risk_kws in _SUPPLY_CHAIN_RISK_KEYWORDS.items():
        if any(rk.lower() in text_lower for rk in risk_kws):
            return "供應鏈/關稅"  # Classify as supply chain even without tech keywords

    # No tech keyword hit → check blocklist
    for word in NON_TECH_SIGNALS:
        if word in text:
            return None  # drop

    # Ambiguous: use hint as fallback category, or drop
    return hint if hint else None


def clean(text: str) -> str:
    if not text:
        return ""
    text = html.unescape(text)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def parse_date(raw: str) -> str:
    if not raw:
        return ""
    try:
        dt = parsedate_to_datetime(raw).astimezone(TW_TZ)
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return raw[:16] if len(raw) > 16 else raw


# ── Generic RSS parser ────────────────────────────────────────────────────────
def parse_rss(url: str, source_name: str, hint: str = "") -> list[dict]:
    import time
    articles = []
    # Retry logic for transient network failures (especially Google News SSL issues)
    max_retries = 2
    for attempt in range(max_retries):
        try:
            # Increased timeout for Google/Bing News which can be slow
            timeout = 20 if ("news.google.com" in url or "bing.com" in url) else TIMEOUT
            resp = requests.get(url, headers=HEADERS, timeout=timeout)
            resp.encoding = "utf-8"
            soup = BeautifulSoup(resp.content, "xml")
            items = soup.find_all("item")
            logger.info(f"  {source_name}: {len(items)} items (attempt {attempt+1})")

            # Successfully fetched, now parse items
            for item in items:
                title_el = item.find("title")
                link_el  = item.find("link")
                desc_el  = item.find("description")
                date_el  = item.find("pubDate")
                src_el   = item.find("source")

                title    = clean(title_el.get_text() if title_el else "")
                raw_url  = link_el.get_text(strip=True) if link_el else ""

                # Bing News wraps URLs in apiclick redirects, extract actual URL
                if "bing.com/news/apiclick.aspx" in raw_url:
                    from urllib.parse import urlparse, parse_qs, unquote
                    try:
                        qs = parse_qs(urlparse(raw_url).query)
                        if "url" in qs:
                            raw_url = unquote(qs["url"][0])
                    except Exception:
                        pass  # If parsing fails, use original raw_url

                summary  = clean(desc_el.get_text() if desc_el else "")[:220]
                pub_date = parse_date(date_el.get_text() if date_el else "")

                if len(title) < 8:
                    continue

                # Strip source name suffix (e.g., "Article Title - Digitimes")
                if " - " in title or " – " in title:
                    stripped = re.sub(r"\s*[-–]\s*[^-–]{2,}\s*$", "", title).strip()
                    if len(stripped) >= 4:
                        title = stripped

                if len(title) < 8:
                    continue

                # Translate to Chinese if needed
                title, summary = translate_to_chinese(title, summary)

                category = classify_category(title, summary, hint)
                if category is None:
                    continue  # not tech-relevant, skip

                articles.append({
                    "source":     source_name,
                    "source_url": raw_url,
                    "title":      title,
                    "summary":    summary,
                    "category":   category,
                    "published":  pub_date,
                    "fetched_at": datetime.now(TW_TZ).strftime("%Y-%m-%d %H:%M:%S"),
                    "provider":   clean(src_el.get_text() if src_el else source_name),
                })
            break  # Success, exit retry loop
        except Exception as e:
            if attempt < max_retries - 1:
                logger.debug(f"  {source_name} attempt {attempt+1} failed, retrying in 2s: {type(e).__name__}")
                time.sleep(2)
            else:
                logger.warning(f"RSS failed ({source_name}) after {max_retries} attempts: {type(e).__name__}")

    return articles


# ── Feed definitions ──────────────────────────────────────────────────────────
GN = "https://www.bing.com/news/search?format=rss&q="
GN_EN = "https://www.bing.com/news/search?format=rss&q="

FEEDS = [
    # ── 直接 RSS（有真實文章 URL，可抓摘要）──────────────────────────────
    {"url": "https://technews.tw/feed/",                      "source": "科技新報",    "hint": "AI 產業"},
    {"url": "https://www.ithome.com.tw/rss",                  "source": "iThome",      "hint": "科技"},
    {"url": "https://feeds.feedburner.com/cool3c-all",        "source": "電腦王",      "hint": "科技"},
    {"url": "https://tw.news.yahoo.com/rss/finance",          "source": "Yahoo財經",   "hint": "科技"},
    {"url": "https://www.ctee.com.tw/rss.xml",                "source": "工商時報",    "hint": "科技"},

    # ── Bing News 搜尋（site: 操作符有效）────────────────────────────────────
    {"url": GN + "site:digitimes.com",                    "source": "Digitimes",   "hint": "AI 產業"},
    {"url": GN + "site:digitimes.com+AI+人工智慧",         "source": "Digitimes",   "hint": "AI 產業"},
    {"url": GN + "site:digitimes.com+半導體+晶片",         "source": "Digitimes",   "hint": "半導體"},
    {"url": GN + "site:digitimes.com+台積電+TSMC",         "source": "Digitimes",   "hint": "半導體"},
    {"url": GN + "site:digitimes.com+筆電+PC",             "source": "Digitimes",   "hint": "PC / NB"},
    {"url": GN + "site:digitimes.com+伺服器+資料中心",     "source": "Digitimes",   "hint": "伺服器/雲端"},
    {"url": GN + "site:digitimes.com+記憶體+DRAM+HBM",    "source": "Digitimes",   "hint": "記憶體/儲存"},
    {"url": GN + "site:digitimes.com+面板+OLED+LCD",      "source": "Digitimes",   "hint": "面板/顯示"},
    {"url": GN + "site:digitimes.com+財報+營收+法說",      "source": "Digitimes",   "hint": "財報/法說"},
    {"url": GN + "site:ctee.com.tw+科技",                    "source": "工商時報",    "hint": "科技"},
    {"url": GN + "AI+伺服器+臺灣",                           "source": "科技新聞",    "hint": "伺服器/雲端"},
    {"url": GN + "HBM+記憶體+AI",                            "source": "科技新聞",    "hint": "記憶體/儲存"},
    {"url": GN + "台積電+先進製程",                          "source": "科技新聞",    "hint": "半導體"},
    {"url": GN + "電競+顯卡+RTX",                            "source": "科技新聞",    "hint": "電競/ROG"},
    {"url": GN + "筆電+出貨+PC市場",                         "source": "科技新聞",    "hint": "PC / NB"},
    {"url": GN + "OLED+面板+顯示器",                         "source": "科技新聞",    "hint": "面板/顯示"},
    {"url": GN + "法說會+營收+科技",                         "source": "科技新聞",    "hint": "財報/法說"},
    {"url": GN + "財報+EPS+毛利率",                          "source": "科技新聞",    "hint": "財報/法說"},
    {"url": GN_EN + "TSMC+semiconductor+AI",                 "source": "Global Tech", "hint": "半導體"},
    {"url": GN_EN + "NVIDIA+GPU+data+center",                "source": "Global Tech", "hint": "AI 產業"},
    {"url": GN_EN + "earnings+semiconductor+quarterly",      "source": "Global Tech", "hint": "財報/法說"},

    # ── Supply chain risks: strikes, conflicts, disasters ───────────────────
    {"url": GN + "罷工",                                    "source": "Google News", "hint": "供應鏈/關稅"},
    {"url": GN + "工人罷工+三星+富士康+鴻海",               "source": "Google News", "hint": "供應鏈/關稅"},
    {"url": GN + "罷工+供應鏈+工潮",                        "source": "Google News", "hint": "供應鏈/關稅"},
    {"url": GN + "戰爭+衝突+地緣政治",                      "source": "Google News", "hint": "供應鏈/關稅"},
    {"url": GN + "伊朗+美國+中東+衝突",                     "source": "Google News", "hint": "供應鏈/關稅"},
    {"url": GN + "紅海+胡塞+航運",                          "source": "Google News", "hint": "供應鏈/關稅"},
    {"url": GN + "颱風+警報+停工+致災",                     "source": "Google News", "hint": "供應鏈/關稅"},
    {"url": GN + "地震+水災+災害",                          "source": "Google News", "hint": "供應鏈/關稅"},
]


# ── Vendor watchlist ──────────────────────────────────────────────────────────
_WATCHLIST_PATH = os.path.join(os.path.dirname(__file__), "watchlist.csv")


def load_watchlist() -> dict[str, str]:
    """Return {vendor_name: risk_level('紅'/'黃')} from watchlist.csv.
    Supports the quarterly report format:
      Row 1: English column names (vendor, report_signal, ...)
      Row 2: Descriptive labels — skipped automatically
      Data: vendor name in 'vendor' col, 紅燈/黃燈/綠燈 in 'report_signal' col
    """
    result: dict[str, str] = {}
    if not os.path.exists(_WATCHLIST_PATH):
        return result
    try:
        with open(_WATCHLIST_PATH, encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            headers = reader.fieldnames or []

            # Detect vendor column
            vendor_col = next((h for h in headers if h.lower() in ("vendor", "廠商")), None)
            # Detect signal column
            signal_col = next((h for h in headers
                               if h.lower() in ("report_signal", "report_risk")
                               or "signal" in h.lower() or "燈" in h), None)

            if not vendor_col or not signal_col:
                logger.warning(f"Watchlist: can't find vendor/signal columns in {headers}")
                return result

            # Skip second header row if it looks like labels (e.g. "Vendor", "Stock ID")
            first_row = next(reader, None)
            if first_row:
                v = (first_row.get(vendor_col) or "").strip()
                if v.lower() in ("vendor", "廠商", "stock id", ""):
                    pass  # was a label row, skip it
                else:
                    # It's real data, process it
                    _apply_watchlist_row(first_row, vendor_col, signal_col, result)

            for row in reader:
                _apply_watchlist_row(row, vendor_col, signal_col, result)

        logger.info(f"Watchlist loaded: {len(result)} vendors")
    except Exception as e:
        logger.warning(f"Watchlist load error: {e}")
    return result


def _apply_watchlist_row(row: dict, vendor_col: str, signal_col: str,
                         result: dict[str, str]) -> None:
    vendor = (row.get(vendor_col) or "").strip()
    signal = (row.get(signal_col) or "").strip()
    if not vendor:
        return
    if "紅" in signal:
        risk = "紅"
    elif "黃" in signal:
        risk = "黃"
    else:
        return  # 綠燈 or unknown → skip
    # Keep worst risk if vendor appears multiple times
    if result.get(vendor) != "紅":
        result[vendor] = risk


# ── Article snippet enrichment ────────────────────────────────────────────────
def _summary_is_empty(title: str, summary: str) -> bool:
    """Return True if summary adds no meaningful content beyond the title."""
    if not summary or len(summary) < 25:
        return True
    # Normalize: remove dashes/spaces/punctuation for comparison
    def _norm(t):
        return re.sub(r'[\s\-–—·|·•]+', '', t).lower()
    t_n = _norm(title)
    s_n = _norm(summary)
    return s_n.startswith(t_n) or s_n == t_n


def _resolve_google_news_url(url: str) -> str:
    """Decode Google News redirect URL (CBMi...) to get the actual article URL.

    Method 1: base64 decode the token and scan for a plain-text URL (works for
              older token formats where the URL is stored as ASCII in the binary).
    Method 2: fetch the Google News page and parse the JavaScript / meta redirect
              (required for the newer CBMi protobuf token format used since 2024).
    """
    if "news.google.com" not in url:
        return url

    # Method 1: base64 decode
    try:
        import base64 as _b64
        m = re.search(r"/articles/([A-Za-z0-9_=-]+)", url)
        if m:
            encoded = m.group(1)
            padding = (4 - len(encoded) % 4) % 4
            decoded = _b64.urlsafe_b64decode(encoded + "=" * padding)
            found = re.findall(rb"https?://[^\x00-\x1f\s<>\"']+", decoded)
            for candidate_bytes in found:
                candidate = candidate_bytes.decode("utf-8", errors="ignore").rstrip(".,)")
                if "google.com" not in candidate and len(candidate) > 20:
                    return candidate
    except Exception:
        pass

    # Method 2: fetch the page and parse JS / meta redirect
    try:
        r = requests.get(url, headers=HEADERS, timeout=6, allow_redirects=True)
        if r.status_code < 400:
            # If HTTP redirect already moved us off Google, we're done
            if "news.google.com" not in r.url and "google.com" not in r.url:
                return r.url
            text = r.text
            for pattern in [
                r'window\.location\.(?:href|replace)\s*[=\(]\s*["\']([^"\']{20,})["\']',
                r'<meta[^>]+http-equiv=["\']?refresh["\']?[^>]+content=[^>]+url=([^"\'>\s&]+)',
                r'"url"\s*:\s*"(https?://[^"]{20,})"',
            ]:
                match = re.search(pattern, text, re.IGNORECASE)
                if match:
                    candidate = match.group(1).strip()
                    if candidate.startswith("http") and "google.com" not in candidate:
                        return candidate
    except Exception:
        pass

    return url


def _fetch_snippet(url: str, max_chars: int = 160) -> str:
    """Resolve Google News redirect, then extract article summary via OG tags or first paragraph."""
    if not url:
        return ""
    try:
        actual_url = _resolve_google_news_url(url)
        # Always fetch with allow_redirects=True:
        # - If base64 decode succeeded, actual_url is the real article URL → fetch it
        # - If decode failed, actual_url is still news.google.com → HTTP redirect will
        #   carry us to the real article; r.url tells us the final destination
        r = requests.get(actual_url, headers=HEADERS, timeout=5, allow_redirects=True)
        if r.status_code >= 400:
            return ""
        # If we still ended up on a Google page, no article content available
        if "news.google.com" in r.url or "google.com/sorry" in r.url:
            return ""
        soup = BeautifulSoup(r.content, "html.parser")

        # 1. Try OG / meta description first (in <head>, fast and clean)
        for attr_name, attr_val in [
            ("property", "og:description"),
            ("name", "description"),
            ("name", "twitter:description"),
        ]:
            tag = soup.find("meta", {attr_name: attr_val})
            if tag:
                text = html.unescape((tag.get("content") or "").strip())
                if len(text) > 40:
                    return text[:max_chars] + ("…" if len(text) > max_chars else "")

        # 2. Fall back to first meaningful paragraph in article body
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
    except Exception as e:
        logger.debug(f"_fetch_snippet failed ({url[:60]}): {e}")
    return ""


# ── Main aggregator (parallel fetch) ─────────────────────────────────────────
def fetch_all_news() -> list[dict]:
    logger.info("ASUSTIMES: starting parallel fetch…")
    results: list[dict] = []

    # Build dynamic feeds for watchlist vendors (紅燈 first, cap at 20)
    watchlist = load_watchlist()
    red_vendors    = [(v, r) for v, r in watchlist.items() if r == "紅"]
    yellow_vendors = [(v, r) for v, r in watchlist.items() if r == "黃"]
    priority_vendors = (red_vendors + yellow_vendors)[:20]
    vendor_feeds = []
    for vendor, risk in priority_vendors:
        vendor_feeds.append({
            "url": GN + vendor,
            "source": "Google News",
            "hint": "財務風險",
            "_watchlist_vendor": vendor,
            "_watchlist_risk": risk,
        })
    logger.info(f"Watchlist: adding {len(vendor_feeds)} vendor feeds ({len(red_vendors)} 紅, {len(yellow_vendors)} 黃, capped at 20)")

    all_feeds = FEEDS + vendor_feeds

    def _fetch(feed):
        articles = parse_rss(feed["url"], feed["source"], feed.get("hint", ""))
        # Tag articles from vendor-specific feeds directly
        if feed.get("_watchlist_vendor"):
            for a in articles:
                a["watchlist_vendor"] = feed["_watchlist_vendor"]
                a["watchlist_risk"]   = feed["_watchlist_risk"]
                a["category"]         = "財務風險"
        return articles

    # Reduced from 8 to 4 workers to avoid overwhelming Google News with concurrent requests
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(_fetch, feed): feed for feed in all_feeds}
        try:
            for future in as_completed(futures, timeout=120):
                try:
                    results.extend(future.result())
                except Exception as e:
                    logger.warning(f"Feed failed: {e}")
        except FuturesTimeoutError:
            logger.warning(f"Fetch timeout — returning {len(results)} partial results")

    # Deduplicate by normalised title prefix
    seen: set[str] = set()
    unique: list[dict] = []
    for item in results:
        key = re.sub(r"\W+", "", item["title"])[:40]
        if key and key not in seen:
            seen.add(key)
            unique.append(item)

    unique.sort(key=lambda a: a.get("published", "") or a.get("fetched_at", ""), reverse=True)

    # Watchlist tagging: also scan all articles for vendor name mentions
    if watchlist:
        for article in unique:
            text = f"{article['title']} {article.get('summary', '')}"
            for vendor, risk in watchlist.items():
                if vendor in text:
                    article["watchlist_vendor"] = vendor
                    article["watchlist_risk"]   = risk
                    article["category"]         = "財務風險"
                    break

    # Enrich summaries: only for articles with a real (non-Google) URL
    to_enrich = [
        a for a in unique[:25]
        if _summary_is_empty(a["title"], a.get("summary", ""))
        and a.get("source_url")
        and "news.google.com" not in a.get("source_url", "")
        and "bing.com" not in a.get("source_url", "")
    ]
    if to_enrich:
        logger.info(f"Enriching summaries for {len(to_enrich)} articles…")
        with ThreadPoolExecutor(max_workers=8) as ex:
            futs = {ex.submit(_fetch_snippet, a["source_url"]): a for a in to_enrich}
            try:
                for fut in as_completed(futs, timeout=25):
                    art = futs[fut]
                    try:
                        snippet = fut.result()
                        if snippet:
                            art["summary"] = snippet
                            logger.info(f"Enriched: {art['title'][:40]}")
                    except Exception:
                        pass
            except FuturesTimeoutError:
                logger.warning("Snippet enrichment timed out (25s)")
        logger.info("Summary enrichment done")

    logger.info(f"ASUSTIMES: {len(unique)} tech articles ready")
    return unique
