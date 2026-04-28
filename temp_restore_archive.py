#!/usr/bin/env python3
import xml.etree.ElementTree as ET
import json
import requests
from urllib.parse import quote, urlparse, parse_qs, unquote
from datetime import datetime, timezone, timedelta

articles = []

# Fetch from a few Digitimes Bing News searches
searches = [
    "site:digitimes.com",
    "site:digitimes.com AI",
    "site:digitimes.com 台積電",
]

headers = {"User-Agent": "Mozilla/5.0"}

for search_term in searches:
    try:
        url = f"https://www.bing.com/news/search?format=rss&q={quote(search_term)}"
        r = requests.get(url, headers=headers, timeout=10)
        
        root = ET.fromstring(r.content)
        for item in root.findall(".//item")[:5]:
            title = item.findtext("title") or ""
            link = item.findtext("link") or ""
            pub = item.findtext("pubDate") or ""
            
            # Decode Bing redirect
            if "bing.com/news/apiclick.aspx" in link:
                try:
                    qs = parse_qs(urlparse(link).query)
                    if "url" in qs:
                        link = unquote(qs["url"][0])
                except:
                    pass
            
            if title and link:
                articles.append({
                    "source": "Digitimes",
                    "source_url": link,
                    "title": title,
                    "summary": "",
                    "published": pub,
                    "fetched_at": datetime.now(timezone.utc).isoformat(),
                    "category": "科技"
                })
        print(f"✓ {len(articles)} from '{search_term}'")
    except Exception as e:
        print(f"✗ {search_term}: {e}")

# Deduplicate by URL
unique_urls = {}
for a in articles:
    url = a.get("source_url", "")
    if url and url not in unique_urls:
        unique_urls[url] = a

final_list = list(unique_urls.values())
final_list.sort(key=lambda a: a.get("published", ""), reverse=True)

print(f"\nTotal: {len(final_list)} unique articles")
if final_list:
    with open("news_archive.json", "w", encoding="utf-8") as f:
        json.dump(final_list, f, ensure_ascii=False, indent=2)
    print("✓ Saved to news_archive.json")
