#!/usr/bin/env python3
"""
Fetch Digitimes articles using search engines (Bing)
Extract: title, first 2 sentences, and link
"""

import requests
from bs4 import BeautifulSoup
from datetime import datetime, timezone, timedelta
import json
import time

TW_TZ = timezone(timedelta(hours=8))

def fetch_digitimes_via_bing():
    """搜尋 Digitimes 文章"""

    print("=" * 70)
    print("Digitimes 文章抓取 (via Bing Search)")
    print("=" * 70)
    print(f"時間: {datetime.now(TW_TZ).strftime('%Y-%m-%d %H:%M:%S')}\n")

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }

    # 搜尋參數
    keywords = [
        "台積電", "半導體", "晶片", "AI", "iPhone",
        "GPU", "記憶體", "面板", "供應鏈", "財報"
    ]

    all_articles = {}

    for kw in keywords:
        try:
            # Bing 搜尋 + site:digitimes.com
            url = f"https://www.bing.com/search?q=site:digitimes.com {kw}"

            resp = requests.get(url, headers=headers, timeout=10)
            resp.encoding = "utf-8"
            soup = BeautifulSoup(resp.content, "html.parser")

            # 查找搜尋結果
            results = soup.find_all("div", class_="b_algo")

            for result in results:
                try:
                    # 標題
                    title_elem = result.find("h2").find("a")
                    if not title_elem:
                        continue
                    title = title_elem.get_text(strip=True)

                    # 連結
                    link = title_elem.get("href", "")
                    if not link or "digitimes" not in link:
                        continue

                    # 摘要
                    desc_elem = result.find("p", class_="b_algoSlug")
                    summary = desc_elem.get_text(strip=True)[:200] if desc_elem else ""

                    if link not in all_articles and "digitimes" in link.lower():
                        all_articles[link] = {
                            "title": title,
                            "summary": summary,
                            "url": link,
                            "source": "Digitimes (Bing Search)",
                            "fetched_at": datetime.now(TW_TZ).isoformat()
                        }

                    print(f"✓ {title[:60]}")

                except Exception as e:
                    pass

            print(f"  {kw}: 累計 {len(all_articles)} 篇\n")
            time.sleep(1)

        except Exception as e:
            print(f"❌ {kw}: {e}\n")

    # 轉換為列表
    articles = list(all_articles.values())
    articles.sort(key=lambda a: a.get("fetched_at", ""), reverse=True)

    print(f"\n✅ 共抓取 {len(articles)} 篇 Digitimes 文章\n")

    # 保存為 JSON
    output = {
        "timestamp": datetime.now(TW_TZ).isoformat(),
        "total": len(articles),
        "articles": articles
    }

    with open("digitimes_search.json", "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"✅ 已保存到 digitimes_search.json")

    # 顯示前 5 篇
    print("\n最新 5 篇:")
    for i, a in enumerate(articles[:5], 1):
        print(f"{i}. {a['title'][:70]}")
        print(f"   {a['summary'][:100]}...")

if __name__ == "__main__":
    fetch_digitimes_via_bing()
