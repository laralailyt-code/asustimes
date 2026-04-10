"""
ASUSTIMES — News scraper
Only tech-industry news relevant to ASUS executives.
Categories: AI產業 / 記憶體儲存 / 半導體 / PC_NB / 伺服器雲端 / 面板顯示 / 電競ROG / 供應鏈關稅
"""

import re
import html
import logging
import requests
from bs4 import BeautifulSoup
from datetime import datetime
from email.utils import parsedate_to_datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

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
TIMEOUT = 8

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
        "eMMC", "UFS", "記憶體模組", "DIMM", "容量擴充", "頻寬",
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
        "HP", "Dell", "Lenovo", "聯想", "Acer", "宏碁",
        "AI PC", "Copilot+", "二合一筆電", "商務筆電", "輕薄筆電",
        "Core Ultra", "Ryzen AI", "Snapdragon X",
    ],
    "伺服器/雲端": [
        "伺服器", "server", "資料中心", "data center", "雲端", "cloud",
        "AWS", "Azure", "Google Cloud", "GCP", "超大規模", "hyperscaler",
        "機架", "rack", "散熱", "液冷", "浸沒式冷卻", "AI server",
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
        "esports", "FPS", "幀率", "高刷", "電競椅", "散熱器",
    ],
    "供應鏈/關稅": [
        "關稅", "tariff", "供應鏈", "supply chain", "貿易戰", "出口管制",
        "ODM", "OEM", "代工", "制裁", "禁令", "entity list", "晶片禁令",
        "移轉", "遷廠", "越南", "印度", "墨西哥", "轉單", "去中化",
        "庫存", "去庫存", "產能利用率", "月營收", "法說會",
    ],
}

# ── Non-tech blocklist (articles matching these are dropped if no tech match) ──
NON_TECH_SIGNALS = [
    "選舉", "民調", "立委", "縣市長", "政黨", "藍綠",
    "棒球", "籃球", "足球", "奧運", "世界盃", "體育賽",
    "娛樂", "藝人", "明星", "電影票房", "韓劇", "偶像",
    "美食", "餐廳", "食安", "咖啡廳",
    "颱風", "地震", "天氣預報",
    "房地產", "買房", "炒房", "房市",
    "醫療糾紛", "新冠疫苗", "醫院",
]


def classify_category(title: str, summary: str = "", hint: str = "") -> str | None:
    """Return matched category, or None if no tech keyword matches at all."""
    text = f"{title} {summary} {hint}"
    text_lower = text.lower()

    scores: dict[str, int] = {cat: 0 for cat in CATEGORY_KEYWORDS}
    for cat, keywords in CATEGORY_KEYWORDS.items():
        for kw in keywords:
            if kw.lower() in text_lower:
                scores[cat] += 1

    best = max(scores, key=scores.get)
    if scores[best] > 0:
        return best

    # No tech keyword hit → check blocklist
    # If blocklist word found, definitely drop
    for word in NON_TECH_SIGNALS:
        if word in text:
            return None  # drop

    # Ambiguous: keep with hint or drop
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
        dt = parsedate_to_datetime(raw)
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return raw[:16] if len(raw) > 16 else raw


# ── Generic RSS parser ────────────────────────────────────────────────────────
def parse_rss(url: str, source_name: str, hint: str = "") -> list[dict]:
    articles = []
    try:
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        resp.encoding = "utf-8"
        soup = BeautifulSoup(resp.content, "xml")
        items = soup.find_all("item")
        logger.info(f"  {source_name}: {len(items)} items")

        for item in items:
            title_el = item.find("title")
            link_el  = item.find("link")
            desc_el  = item.find("description")
            date_el  = item.find("pubDate")
            src_el   = item.find("source")

            title    = clean(title_el.get_text() if title_el else "")
            raw_url  = link_el.get_text(strip=True) if link_el else ""
            summary  = clean(desc_el.get_text() if desc_el else "")[:220]
            pub_date = parse_date(date_el.get_text() if date_el else "")

            if len(title) < 6:
                continue

            # Strip source name suffix from Google News titles (e.g., "… - Digitimes")
            title = re.sub(r"\s*[-–]\s*\S.*$", "", title).strip() if " - " in title or " – " in title else title

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
                "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "provider":   clean(src_el.get_text() if src_el else source_name),
            })

    except Exception as e:
        logger.warning(f"RSS failed ({source_name}): {e}")

    return articles


# ── Feed definitions ──────────────────────────────────────────────────────────
GN = "https://news.google.com/rss/search?hl=zh-TW&gl=TW&ceid=TW:zh-Hant&q="
GN_EN = "https://news.google.com/rss/search?hl=en-US&gl=US&ceid=US:en&q="

FEEDS = [
    # ── 台灣科技媒體 ────────────────────────────────────────────────────────
    {"url": GN + "site:digitimes.com.tw",   "source": "Digitimes",   "hint": "半導體"},
    {"url": GN + "site:ctee.com.tw+科技",   "source": "工商時報",    "hint": "PC / NB"},
    {"url": GN + "site:technews.tw",        "source": "科技新報",    "hint": "AI 產業"},
    {"url": GN + "site:ithome.com.tw",      "source": "iThome",      "hint": "AI 產業"},
    {"url": GN + "site:cool3c.com",         "source": "電腦王",      "hint": "電競/ROG"},

    # ── 主題精選 ────────────────────────────────────────────────────────────
    {"url": GN + "AI+伺服器+台灣",          "source": "Google News", "hint": "伺服器/雲端"},
    {"url": GN + "HBM+記憶體+AI",           "source": "Google News", "hint": "記憶體/儲存"},
    {"url": GN + "台積電+先進製程",         "source": "Google News", "hint": "半導體"},
    {"url": GN + "電競+顯卡+RTX",           "source": "Google News", "hint": "電競/ROG"},
    {"url": GN + "筆電+出貨+PC市場",        "source": "Google News", "hint": "PC / NB"},
    {"url": GN + "關稅+科技+供應鏈",        "source": "Google News", "hint": "供應鏈/關稅"},
    {"url": GN + "OLED+面板+顯示器",        "source": "Google News", "hint": "面板/顯示"},

    # ── 英文科技媒體 ────────────────────────────────────────────────────────
    {"url": GN_EN + "site:tomshardware.com",  "source": "Tom's Hardware", "hint": "電競/ROG"},
    {"url": GN_EN + "TSMC+semiconductor+AI",  "source": "Global Tech",    "hint": "半導體"},
    {"url": GN_EN + "NVIDIA+GPU+data+center", "source": "Global Tech",    "hint": "AI 產業"},

    # ── Yahoo財經 ────────────────────────────────────────────────────────────
    {"url": "https://tw.news.yahoo.com/rss/finance", "source": "Yahoo財經", "hint": "供應鏈/關稅"},
]


# ── Main aggregator (parallel fetch) ─────────────────────────────────────────
def fetch_all_news() -> list[dict]:
    logger.info("ASUSTIMES: starting parallel fetch…")
    results: list[dict] = []

    def _fetch(feed):
        return parse_rss(feed["url"], feed["source"], feed.get("hint", ""))

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {executor.submit(_fetch, feed): feed for feed in FEEDS}
        for future in as_completed(futures, timeout=60):
            try:
                results.extend(future.result())
            except Exception as e:
                logger.warning(f"Feed failed: {e}")

    # Deduplicate by normalised title prefix
    seen: set[str] = set()
    unique: list[dict] = []
    for item in results:
        key = re.sub(r"\W+", "", item["title"])[:28]
        if key and key not in seen:
            seen.add(key)
            unique.append(item)

    unique.sort(key=lambda a: a.get("published", "") or a.get("fetched_at", ""), reverse=True)
    logger.info(f"ASUSTIMES: {len(unique)} tech articles ready")
    return unique
