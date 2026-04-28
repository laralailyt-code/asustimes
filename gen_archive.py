#!/usr/bin/env python3
import urllib.request
import xml.etree.ElementTree as ET
import json
from datetime import datetime

articles = {}

keywords = [
    "AI", "semiconductor", "TSMC", "PC", "memory",
    "strike", "supply chain", "Digitimes", "iPhone", "GPU"
]

print("Fetching Bing News RSS...")
for kw in keywords:
    try:
        url = f"https://www.bing.com/news/search?format=rss&q={kw}"
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = resp.read()
            root = ET.fromstring(data)
            for item in root.findall(".//item"):
                link_elem = item.find("link")
                title_elem = item.find("title")
                
                if link_elem is not None and title_elem is not None:
                    link = link_elem.text or ""
                    title = title_elem.text or ""
                    desc = ""
                    desc_elem = item.find("description")
                    if desc_elem is not None:
                        desc = desc_elem.text or ""
                    
                    if link and title and link not in articles:
                        articles[link] = {
                            "source": "Bing News",
                            "source_url": link,
                            "title": title,
                            "summary": desc[:200],
                            "published": "",
                            "category": "tech"
                        }
        print(f"  {kw}: OK ({len(articles)} total)")
    except Exception as e:
        print(f"  {kw}: {e}")

if articles:
    data = list(articles.values())
    with open("news_archive.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"\n✓ Created news_archive.json with {len(data)} articles")
else:
    print("\n✗ No articles found")
