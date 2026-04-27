#!/usr/bin/env python3
"""
Digitimes 新闻每日抓取脚本
自动登入 Digitimes 企業帳號，抓取最新新闻，翻译成中文，保存为 JSON
"""

import requests
import json
import sys
import time
from datetime import datetime, timezone, timedelta
from bs4 import BeautifulSoup
from urllib.parse import quote
from pathlib import Path

TW_TZ = timezone(timedelta(hours=8))

def fetch_digitimes_news():
    """登入并抓取 Digitimes 新闻"""

    print("=" * 70)
    print("Digitimes 新闻自动抓取")
    print("=" * 70)
    print(f"时间: {datetime.now(TW_TZ).strftime('%Y-%m-%d %H:%M:%S')}\n")

    try:
        print("[1/4] 建立会话并登入...")

        session = requests.Session()
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8",
        }

        # 登入 (使用 Digitimes 正確的登入端點)
        login_url = "https://www.digitimes.com.tw/tech/lgn/lgn.asp"
        login_data = {
            "mail": "lara1_lai@asus.com",  # Digitimes 用 mail 而不是 email
            "pwd": "sourcer888",           # Digitimes 用 pwd 而不是 password
            "tourl": "/tech/default.asp"
        }

        r = session.post(login_url, data=login_data, headers=headers, timeout=15)
        if r.status_code != 200:
            print(f"❌ 登入失败: HTTP {r.status_code}")
            return False

        print(f"✅ 登入成功 (HTTP {r.status_code})\n")

        print("[2/4] 抓取关键字搜索结果...")

        keywords = [
            "AI", "ChatGPT", "NVIDIA", "半導體", "台積電",
            "筆電", "PC", "伺服器", "記憶體", "DRAM",
            "面板", "供應鏈", "財報", "營收", "法說會"
        ]

        all_articles = {}

        for keyword in keywords:
            search_url = f"https://www.digitimes.com.tw/tech/searchdomain/srchlst_main.asp?q={quote(keyword)}"

            try:
                r = session.get(search_url, headers=headers, timeout=15)
                soup = BeautifulSoup(r.content, "html.parser")

                # 查找所有文章链接
                links = soup.find_all("a", href=True)

                for link in links:
                    href = link.get("href", "")
                    text = link.get_text(strip=True)

                    # 过滤有效的文章链接
                    if ("/tech/" in href or "/news/" in href) and len(text) > 8:
                        if not href.startswith("http"):
                            href = f"https://www.digitimes.com.tw{href}"

                        if href not in all_articles and "digitimes" in href.lower():
                            all_articles[href] = text

                found_count = len(all_articles)
                print(f"  {keyword:12} → 累计 {found_count} 篇")
                time.sleep(0.5)

            except Exception as e:
                print(f"  {keyword:12} → 错误: {e}")
                continue

        print(f"\n✅ 总共抓到 {len(all_articles)} 篇新闻\n")

        if not all_articles:
            print("⚠️ 没有抓到文章")
            return False

        print("[3/4] 翻译标题到中文...")

        try:
            from scraper import translate_to_chinese
        except ImportError:
            print("⚠️ 无法导入 scraper，使用原始标题")
            translate_to_chinese = lambda t, s="": (t, s)

        articles = []
        for i, (url, title) in enumerate(all_articles.items(), 1):
            translated, _ = translate_to_chinese(title)

            has_zh = any('一' <= c <= '鿿' for c in translated)
            lang = "🇹🇼" if has_zh else "🇬🇧"

            articles.append({
                "title": translated,
                "original_title": title,
                "url": url,
                "source": "Digitimes",
                "language": "zh-TW" if has_zh else "en",
                "fetched_at": datetime.now(TW_TZ).isoformat()
            })

            if i % 50 == 0:
                print(f"  {i}/{len(all_articles)} 篇已翻译...")

        print(f"✅ 翻译完成\n")

        print("[4/4] 保存到本地...")

        output = {
            "timestamp": datetime.now(TW_TZ).isoformat(),
            "date": datetime.now(TW_TZ).strftime("%Y-%m-%d"),
            "total": len(articles),
            "articles": articles
        }

        output_file = Path("digitimes_daily.json")
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(output, f, ensure_ascii=False, indent=2)

        print(f"✅ 保存到 {output_file}\n")

        # 统计
        chinese_count = sum(1 for a in articles if any('一' <= c <= '鿿' for c in a['title']))

        print("=" * 70)
        print("📊 统计结果")
        print("=" * 70)
        print(f"总篇数:   {len(articles)}")
        print(f"中文:     {chinese_count} 篇 ({chinese_count*100//len(articles) if articles else 0}%)")
        print(f"英文:     {len(articles)-chinese_count} 篇\n")

        print("前 10 篇:")
        for i, article in enumerate(articles[:10], 1):
            has_zh = any('一' <= c <= '鿿' for c in article['title'])
            lang = "🇹🇼" if has_zh else "🇬🇧"
            title = article['title'][:65]
            print(f"{i:2}. {lang} {title}")

        print("\n" + "=" * 70)
        print("✅ 完成！文件已保存，可以提交到 GitHub")
        print("=" * 70)

        return True

    except Exception as e:
        print(f"❌ 错误: {e}")
        import traceback
        traceback.print_exc()
        return False

if __name__ == "__main__":
    success = fetch_digitimes_news()
    sys.exit(0 if success else 1)
