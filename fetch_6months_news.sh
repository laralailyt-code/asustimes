#!/bin/bash

echo "Fetching 6 months of news from Bing News..."

# Keywords to search
declare -a keywords=(
  "AI"
  "semiconductor"
  "TSMC"
  "iPhone"
  "memory DRAM"
  "PC laptop"
  "server"
  "GPU"
  "panel OLED"
  "earnings"
  "strike"
  "supply chain"
  "trade war"
  "Foxconn"
  "Samsung"
)

# Temporary JSON array
tmpfile=$(mktemp)
echo "[" > "$tmpfile"

first=true
count=0

for kw in "${keywords[@]}"; do
  echo "[+] Searching: $kw"
  
  # URL encode the keyword
  encoded=$(python3 -c "import urllib.parse; print(urllib.parse.quote('$kw'))" 2>/dev/null || echo "$kw")
  
  # Fetch RSS from Bing
  rss=$(curl -s "https://www.bing.com/news/search?format=rss&q=$encoded" 2>/dev/null)
  
  # Extract items using grep/sed
  echo "$rss" | grep -oP '(?<=<item>).*?(?=</item>)' | head -5 | while read item; do
    title=$(echo "$item" | grep -oP '(?<=<title>).*?(?=</title>)' | head -1 | sed 's/"/\\"/g')
    link=$(echo "$item" | grep -oP '(?<=<link>).*?(?=</link>)' | head -1)
    pubdate=$(echo "$item" | grep -oP '(?<=<pubDate>).*?(?=</pubDate>)' | head -1)
    desc=$(echo "$item" | grep -oP '(?<=<description>).*?(?=</description>)' | head -1 | sed 's/"/\\"/g' | cut -c1-200)
    
    if [ ! -z "$title" ] && [ ! -z "$link" ]; then
      if [ "$first" = true ]; then
        first=false
      else
        echo "," >> "$tmpfile"
      fi
      
      cat >> "$tmpfile" << JSONEOF
{
    "source": "Bing News",
    "source_url": "$link",
    "title": "$title",
    "summary": "$desc",
    "published": "$pubdate",
    "fetched_at": "$(date -u +'%Y-%m-%dT%H:%M:%SZ')",
    "category": "科技"
}
JSONEOF
      count=$((count + 1))
    fi
  done
done

echo "]" >> "$tmpfile"

# Save to news_archive.json
if [ $count -gt 0 ]; then
  cp "$tmpfile" news_archive.json
  echo "✓ Created news_archive.json with $count articles"
  rm "$tmpfile"
else
  echo "✗ No articles found"
  rm "$tmpfile"
fi
