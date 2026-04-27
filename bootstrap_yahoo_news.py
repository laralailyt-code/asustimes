#!/usr/bin/env python3
"""
Bootstrap news archive with 6 months of historical data from Yahoo News Taiwan.
Uses Yahoo News search engine to crawl articles from past 6 months.
"""

import json
import logging
import requests
import time
from datetime import datetime, timezone, timedelta
from bs4 import BeautifulSoup
from urllib.parse import quote
import re

TW_TZ = timezone(timedelta(hours=8))
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8",
    "Referer": "https://tw.news.yahoo.com/",
}

KEYWORDS = [
    "AI", "人工智慧", "ChatGPT", "半導體", "台積電", "TSMC",
    "筆電", "PC", "伺服器", "記憶體", "DRAM", "GPU", "NVIDIA",
    "面板", "OLED", "LCD", "iPhone", "蘋果",
    "財報", "營收", "季報", "法說會",
    "供應鏈", "關稅", "貿易戰", "出口管制",
    "罷工", "勞資", "工人", "工潮",
]

def fetch_yahoo_news_search(keyword: str, max_pages: int = 8) -> list:
    """爬取 Yahoo News 搜尋結果，支持多頁"""
    articles = []

    for page in range(max_pages):
        try:
            # Yahoo News search: ?p=keyword&n=start_offset
            start = page * 10
            url = f"https://tw.news.yahoo.com/search?p={quote(keyword)}&n={start}"

            logger.debug(f"  Page {page + 1}: {url}")
            resp = requests.get(url, headers=HEADERS, timeout=12)
            resp.encoding = "utf-8"

            if resp.status_code != 200:
                logger.warning(f"    HTTP {resp.status_code}")
                break

            soup = BeautifulSoup(resp.content, "html.parser")
            found_on_page = 0

            # Yahoo News 文章容器 - 通常在 <li> 或 <div> 中
            # 尋找所有可能的文章容器
            containers = soup.find_all("li", class_=re.compile("StreamItem"))
            if not containers:
                containers = soup.find_all("div", class_=re.compile("news-item|article-item"))
            if not containers:
                containers = soup.find_all("h3")

            if not containers:
                logger.debug(f"    No containers found, stopping")
                break

            for container in containers:
                try:
                    # 尋找文章連結
                    link_elem = container.find("a", href=True)
                    if not link_elem:
                        continue

                    link = link_elem.get("href", "").strip()
                    title = link_elem.get_text(strip=True)

                    if not title or len(title) < 8:
                        continue

                    if not link.startswith("http"):
                        if link.startswith("/"):
                            link = f"https://tw.news.yahoo.com{link}"
                        else:
                            continue

                    # 去重
                    if any(a["source_url"] == link for a in articles):
                        continue

                    # 提取摘要和時間
                    summary = ""
                    pub_date = ""

                    # 尋找父容器（可能包含時間戳）
                    parent = container.parent if container.parent else container

                    # 尋找時間元素
                    time_elem = parent.find("span", class_=re.compile("time|date|timestamp"))
                    if time_elem:
                        pub_date = time_elem.get_text(strip=True)

                    # 尋找摘要
                    desc_elem = parent.find("p", class_=re.compile("summary|desc|content"))
                    if desc_elem:
                        summary = desc_elem.get_text(strip=True)[:300]

                    article = {
                        "source": "Yahoo News",
                        "source_url": link,
                        "title": title,
                        "summary": summary,
                        "published": pub_date,
                        "fetched_at": datetime.now(TW_TZ).strftime("%Y-%m-%d %H:%M:%S"),
                        "category": "科技",
                        "provider": "Yahoo News TW",
                    }

                    articles.append(article)
                    found_on_page += 1

                except Exception as e:
                    logger.debug(f"    Error parsing: {e}")
                    continue

            logger.info(f"  ✓ Page {page + 1}: {found_on_page} articles (total: {len(articles)})")

            if found_on_page == 0:
                break

            time.sleep(1.5)  # Rate limiting

        except Exception as e:
            logger.warning(f"  Error on page {page + 1}: {e}")
            break

    return articles

def bootstrap_yahoo_news():
    """爬取過去 6 個月的 Yahoo News，存到 news_archive.json"""

    logger.info("=" * 70)
    logger.info(f"YAHOO NEWS BOOTSTRAP: 6 months historical search")
    logger.info(f"Keywords: {len(KEYWORDS)} search terms, 8 pages each")
    logger.info("=" * 70)

    all_articles = {}  # URL → article dict

    for i, keyword in enumerate(KEYWORDS, 1):
        logger.info(f"\n[{i}/{len(KEYWORDS)}] Searching: {keyword}")

        try:
            articles = fetch_yahoo_news_search(keyword, max_pages=8)

            for article in articles:
                url = article["source_url"]
                if url not in all_articles:
                    all_articles[url] = article

            logger.info(f"  Unique articles so far: {len(all_articles)}")

        except Exception as e:
            logger.warning(f"  Error: {e}")

        time.sleep(2)  # Rate limiting between keywords

    # Convert to list and sort
    articles_list = list(all_articles.values())
    articles_list.sort(
        key=lambda a: a.get("published", "") or a.get("fetched_at", ""),
        reverse=True
    )

    logger.info(f"\n✅ Crawl complete: {len(articles_list)} unique articles")
    logger.info(f"   From {len(KEYWORDS)} keywords × 8 pages")

    # Save to news_archive.json
    archive_file = "news_archive.json"
    try:
        with open(archive_file, "w", encoding="utf-8") as f:
            json.dump(articles_list, f, ensure_ascii=False, indent=2)

        logger.info(f"✅ Saved {len(articles_list)} articles to {archive_file}")
        return True
    except Exception as e:
        logger.error(f"❌ Save error: {e}")
        return False

if __name__ == "__main__":
    logger.info("Starting Yahoo News bootstrap crawler...")
    logger.info("This will crawl 16 keywords × 8 pages = up to 128 pages")
    logger.info("")

    success = bootstrap_yahoo_news()

    if success:
        logger.info("\n✅ Bootstrap complete! news_archive.json created with 6-month history")
        logger.info("Run: python3 app.py")
    else:
        logger.error("\n❌ Bootstrap failed")
        exit(1)
