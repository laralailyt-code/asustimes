#!/bin/bash

echo "Fetching news from Bing RSS..."

# Simple JSON builder
cat > news_archive.json << 'JSONEOF'
[
JSONEOF

keywords=("AI" "semiconductor" "TSMC" "strike" "supply chain" "Samsung" "iPhone" "memory" "GPU" "earnings")

first=true
total=0

for kw in "${keywords[@]}"; do
  echo "[+] $kw"
  
  # URL encode
  encoded=$(echo "$kw" | sed 's/ /+/g')
  
  # Fetch and parse
  curl -s "https://www.bing.com/news/search?format=rss&q=$encoded" 2>/dev/null | \
  sed -n '/<item>/,/<\/item>/p' | \
  sed 's/<item>/\n<item>/g' | \
  grep -A5 "<item>" | head -40 | while IFS= read -r line; do
    
    if echo "$line" | grep -q "<title>"; then
      title=$(echo "$line" | sed -n 's/.*<title>\(.*\)<\/title>.*/\1/p' | sed 's/"/\\"/g')
      echo "$line" | grep "<link>" | while IFS= read link_line; do
        link=$(echo "$link_line" | sed -n 's/.*<link>\(.*\)<\/link>.*/\1/p')
        
        if [ ! -z "$title" ] && [ ! -z "$link" ] && [ "$title" != "Bing新聞" ]; then
          if [ "$first" = true ]; then
            first=false
          else
            echo "  ," >> news_archive.json
          fi
          
          cat >> news_archive.json << EOF
  {
    "source": "Bing News",
    "source_url": "$link",
    "title": "$title",
    "summary": "",
    "published": "$(date -u +'%Y-%m-%dT%H:%M:%SZ')",
    "category": "科技"
  }
EOF
          total=$((total + 1))
        fi
      done
    fi
  done
done

cat >> news_archive.json << 'JSONEOF'
]
JSONEOF

echo "✓ Saved $total articles to news_archive.json"
