#!/bin/bash

# Fetch Bing News RSS and convert to news_archive.json format
# This is a simple bootstrap to get some initial data

cd /c/Users/Lara1_Lai/Desktop/news_platform

# Fetch a few keywords
declare -a keywords=("AI" "半導體" "台積電" "筆電" "伺服器" "財報")

echo "Fetching Bing News RSS for bootstrap..."

# Use sed/awk to parse RSS and convert to JSON
(
  echo "["
  for kw in "${keywords[@]}"; do
    url="https://www.bing.com/news/search?q=$(echo $kw | tr ' ' '+')&format=rss"
    curl -s "$url" 2>/dev/null | grep -oP '<item>.*?</item>' | head -5 | while read item; do
      title=$(echo "$item" | grep -oP '(?<=<title>)[^<]+' | head -1 | sed 's/"//g')
      link=$(echo "$item" | grep -oP '(?<=<link>)[^<]+' | head -1)
      desc=$(echo "$item" | grep -oP '(?<=<description>)[^<]+' | head -1 | sed 's/"//g')
      if [ -n "$title" ] && [ -n "$link" ]; then
        echo "    {"
        echo "      \"source\": \"Bing News\","
        echo "      \"source_url\": \"$link\","
        echo "      \"title\": \"$title\","
        echo "      \"summary\": \"$desc\","
        echo "      \"published\": \"$(date +'%Y-%m-%d %H:%M')\","
        echo "      \"fetched_at\": \"$(date +'%Y-%m-%d %H:%M:%S')\","
        echo "      \"category\": \"科技\","
        echo "      \"provider\": \"Bing News\""
        echo "    },"
      fi
    done
    sleep 1
  done
  echo "    {}"
  echo "]"
) | sed '$ s/,$//' > news_archive.json

echo "✅ Bootstrap complete: $(wc -l < news_archive.json) lines"
