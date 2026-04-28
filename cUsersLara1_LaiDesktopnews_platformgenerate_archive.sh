#!/bin/bash

# Simple bootstrap: generate mock historical data for testing
# (Real data from Bing would take too long; this ensures platform works with data)

cat > news_archive.json << 'ARCHIVE'
[
  {
    "source": "Bing News",
    "source_url": "https://example.com/article1",
    "title": "台積電Q1業績創新高，先進製程需求強勁",
    "summary": "台積電今日公布Q1財報，營收和獲利均創新高紀錄...",
    "published": "2026-04-20 14:30",
    "fetched_at": "2026-04-27 10:00:00",
    "category": "半導體",
    "provider": "Bing News"
  },
  {
    "source": "Bing News",
    "source_url": "https://example.com/article2",
    "title": "NVIDIA GH200超級芯片交付，數據中心需求持續旺盛",
    "summary": "NVIDIA最新推出的GH200超級芯片開始交付客戶...",
    "published": "2026-04-18 09:15",
    "fetched_at": "2026-04-27 10:00:00",
    "category": "AI 產業",
    "provider": "Bing News"
  }
]
ARCHIVE

echo "✅ Created basic news_archive.json with sample data"
