#!/usr/bin/env python3
"""
Bootstrap news archive with 6 months of historical data from Yahoo News Taiwan.
Crawls multiple pages of search results for tech-related keywords.
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
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

KEYWORDS = [
    "AI", "人工智慧", "ChatGPT", "半導體", "台積電", "TSMC",
    "筆電", "PC", "伺服器", "記憶體", "DRAM", "GPU",
    "面板", "OLED", "LCD", "iPhone", "蘋果",
    "財報", "營收", "季報", "法說會",
    "供應鏈", "關稅", "貿易戰",
    "罷工", "勞資", "工人",
]

def fetch_yahoo_search_results(keyword: str, max_pages: int = 5) -> list:
    """爬取 Yahoo 新聞搜尋結果，支持多頁"""
    articles = []
    base_url = "https://tw.news.yahoo.com/search"

    for page in range(1, max_pages + 1):
        try:
            params = {"p": keyword}
            if page > 1:
                params["n"] = 10 * (page - 1)  # Offset for pagination

            url = f"{base_url}?p={quote(keyword)}"
            if page > 1:
                url += f"&n={10 * (page - 1)}"

            logger.debug(f"  Fetching page {page}: {url}")
            resp = requests.get(url, headers=HEADERS, timeout=10)
            resp.encoding = "utf-8"

            if resp.status_code != 200:
                logger.warning(f"  ✗ HTTP {resp.status_code} for page {page}")
                break  # Stop if page doesn't exist

            soup = BeautifulSoup(resp.content, "html.parser")

            # Yahoo News uses specific article container classes
            # Look for article links and metadata
            found_on_page = 0

            # Try to find article containers
            article_containers = soup.find_all("div", class_=re.compile("StreamItem|newsItem|news-item"))

            if not article_containers:
                # Fallback: look for <a> tags with news-like attributes
                article_containers = soup.find_all("h3")

            for container in article_containers:
                try:
                    # Try to extract title and link
                    title_elem = container.find("a") or container.find("h3")
                    if not title_elem:
                        continue

                    title = title_elem.get_text(strip=True)
                    link = title_elem.get("href", "")

                    if not title or len(title) < 10 or not link:
                        continue

                    # Normalize link (make absolute if relative)
                    if link.startswith("/"):
                        link = f"https://tw.news.yahoo.com{link}"
                    elif not link.startswith("http"):
                        continue

                    # Skip duplicates
                    if any(a["source_url"] == link for a in articles):
                        continue

                    # Try to extract timestamp/summary from parent
                    summary = ""
                    timestamp = ""
                    parent = container.parent
                    if parent:
                        # Look for time element
                        time_elem = parent.find("span", class_=re.compile("time|timestamp|date"))
                        if time_elem:
                            timestamp = time_elem.get_text(strip=True)

                        # Look for summary/description
                        summary_elem = parent.find("p", class_=re.compile("summary|desc"))
                        if summary_elem:
                            summary = summary_elem.get_text(strip=True)[:300]

                    article = {
                        "source": "Yahoo News",
                        "source_url": link,
                        "title": title,
                        "summary": summary,
                        "published": timestamp,
                        "fetched_at": datetime.now(TW_TZ).strftime("%Y-%m-%d %H:%M:%S"),
                        "category": "科技",
                        "provider": "Yahoo News TW",
                    }

                    articles.append(article)
                    found_on_page += 1
                except Exception as e:
                    logger.debug(f"    Error parsing article: {e}")
                    continue

            logger.info(f"  ✓ Page {page}: {found_on_page} articles (total: {len(articles)})")

            if found_on_page == 0:
                break  # No more results

            time.sleep(1)  # Rate limiting

        except Exception as e:
            logger.warning(f"  ✗ Error on page {page}: {e}")
            break

    return articles

def bootstrap_yahoo_news(days_back: 180, max_pages_per_keyword: 5):
    """爬取過去 N 天的 Yahoo 新聞，存到 news_archive.json"""

    logger.info("=" * 70)
    logger.info(f"YAHOO NEWS BOOTSTRAP: {days_back} days back, {max_pages_per_keyword} pages/keyword")
    logger.info("=" * 70)

    all_articles = {}  # URL → article dict
    now = datetime.now(TW_TZ)

    for i, keyword in enumerate(KEYWORDS, 1):
        logger.info(f"\n[{i}/{len(KEYWORDS)}] Keyword: {keyword}")

        try:
            articles = fetch_yahoo_search_results(keyword, max_pages=max_pages_per_keyword)

            for article in articles:
                url = article["source_url"]
                if url not in all_articles:
                    all_articles[url] = article

            logger.info(f"  Total unique articles so far: {len(all_articles)}")
        except Exception as e:
            logger.warning(f"  ✗ Error fetching '{keyword}': {e}")

        time.sleep(2)  # Rate limiting between keywords

    # Convert to list and sort by date (newest first)
    articles_list = list(all_articles.values())
    articles_list.sort(
        key=lambda a: a.get("published", "") or a.get("fetched_at", ""),
        reverse=True
    )

    logger.info(f"\n✅ Bootstrap complete: {len(articles_list)} unique articles fetched from {len(KEYWORDS)} keywords")

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
    logger.info("Starting Yahoo News bootstrap...")
    success = bootstrap_yahoo_news(days_back=180, max_pages_per_keyword=5)

    if success:
        logger.info("\n✅ Bootstrap complete! Now run: python3 app.py")
    else:
        logger.error("\n❌ Bootstrap failed")
        exit(1)
