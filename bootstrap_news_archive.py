#!/usr/bin/env python3
"""
Bootstrap news archive with 6 months of historical data from Bing News.
Run once to populate news_archive.json, then scraper.py will auto-update daily.
"""

import json
import logging
import requests
from datetime import datetime, timezone, timedelta
from bs4 import BeautifulSoup
from urllib.parse import quote
import time

TW_TZ = timezone(timedelta(hours=8))
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "zh-TW,zh;q=0.9",
}

KEYWORDS = [
    # Core tech categories
    "AI 人工智慧", "半導體 晶片", "台積電 TSMC", "NVIDIA GPU",
    "筆電 PC 出貨", "伺服器 資料中心", "記憶體 DRAM HBM",
    "面板 OLED LCD", "電競 ROG", "財報 營收",
    # Supply chain
    "罷工 工潮", "供應鏈 關稅", "地緣政治 衝突",
    "颱風 水災 地震", "紅海 航運",
    # Vendors
    "Samsung", "Intel", "聯發科", "鴻海", "廣達",
]

def bootstrap_bing_news(days_back=180):
    """爬取過去 N 天的 Bing News，存到 news_archive.json"""

    logger.info(f"Starting bootstrap: fetching {days_back} days of Bing News...")
    logger.info(f"Keywords: {len(KEYWORDS)} search terms")

    articles = {}  # Use URL as unique key
    now = datetime.now(TW_TZ)
    start_date = now - timedelta(days=days_back)

    for i, keyword in enumerate(KEYWORDS, 1):
        logger.info(f"[{i}/{len(KEYWORDS)}] Searching: {keyword}")

        try:
            # Bing News 支持時間篩選
            # freshness=Day/Week/Month/Year
            # 我們用日期範圍來爬取
            url = f"https://www.bing.com/news/search?q={quote(keyword)}&format=rss"

            resp = requests.get(url, headers=HEADERS, timeout=10)
            resp.encoding = "utf-8"

            if resp.status_code != 200:
                logger.warning(f"  ✗ HTTP {resp.status_code}")
                continue

            # Parse RSS
            from email.utils import parsedate_to_datetime
            try:
                soup = BeautifulSoup(resp.content, "xml")
            except:
                soup = BeautifulSoup(resp.content, "html.parser")
            items = soup.find_all("item")

            found = 0
            for item in items:
                try:
                    link = item.find("link")
                    title = item.find("title")
                    summary = item.find("description")
                    pub_date = item.find("pubDate")

                    if not link or not title:
                        continue

                    url_str = link.get_text(strip=True)
                    title_str = title.get_text(strip=True)
                    summary_str = summary.get_text(strip=True) if summary else ""

                    # Parse date
                    pub_date_str = ""
                    if pub_date:
                        try:
                            dt = parsedate_to_datetime(pub_date.get_text(strip=True))
                            dt = dt.astimezone(TW_TZ)
                            pub_date_str = dt.strftime("%Y-%m-%d %H:%M")
                        except:
                            pass

                    # Skip old articles (before start_date)
                    if pub_date_str:
                        try:
                            article_dt = datetime.fromisoformat(pub_date_str)
                            if article_dt < start_date:
                                continue
                        except:
                            pass

                    # Bing URL decoding (extract actual URL from apiclick redirect)
                    if "bing.com/news/apiclick.aspx" in url_str:
                        from urllib.parse import urlparse, parse_qs, unquote
                        qs = parse_qs(urlparse(url_str).query)
                        if "url" in qs:
                            url_str = qs["url"][0]

                    # Deduplicate by URL
                    if url_str in articles:
                        continue

                    articles[url_str] = {
                        "source": "Bing News",
                        "source_url": url_str,
                        "title": title_str,
                        "summary": summary_str[:500],  # Limit length
                        "published": pub_date_str,
                        "fetched_at": datetime.now(TW_TZ).strftime("%Y-%m-%d %H:%M:%S"),
                        "category": "其他",
                        "provider": "Bing News",
                    }
                    found += 1
                except Exception as e:
                    logger.debug(f"    Error parsing item: {e}")
                    continue

            logger.info(f"  ✓ Found {found} articles (total: {len(articles)})")
            time.sleep(1)  # Rate limiting

        except Exception as e:
            logger.warning(f"  ✗ Error: {e}")
            continue

    logger.info(f"\n✓ Bootstrap complete: {len(articles)} unique articles fetched")

    # Save to news_archive.json
    archive_file = "news_archive.json"
    try:
        # Convert dict to list
        articles_list = list(articles.values())
        articles_list.sort(key=lambda a: a.get("published", ""), reverse=True)

        with open(archive_file, "w", encoding="utf-8") as f:
            json.dump(articles_list, f, ensure_ascii=False, indent=2)

        logger.info(f"✅ Saved {len(articles_list)} articles to {archive_file}")
    except Exception as e:
        logger.error(f"❌ Save error: {e}")
        return False

    return True

if __name__ == "__main__":
    logger.info("=" * 70)
    logger.info("NEWS ARCHIVE BOOTSTRAP")
    logger.info("=" * 70)

    success = bootstrap_bing_news(days_back=180)

    if success:
        logger.info("\n✅ Bootstrap successful! Now run: python3 app.py")
    else:
        logger.error("\n❌ Bootstrap failed")
        exit(1)
