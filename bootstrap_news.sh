#!/bin/bash

echo "Fetching 6 months of Bing News..."

# Create temporary files
tmpdir=$(mktemp -d)
trap "rm -rf $tmpdir" EXIT

keywords=(
  "AI 人工智慧"
  "半導體 晶片"
  "台積電"
  "筆電 PC"
  "伺服器"
  "記憶體"
  "罷工"
  "供應鏈"
  "Digitimes"
  "iPhone"
)

articles_file="$tmpdir/articles.json"
echo "[]" > "$articles_file"

for kw in "${keywords[@]}"; do
  echo "[+] Searching: $kw"
  curl -s "https://www.bing.com/news/search?format=rss&q=$(python3 -c "import urllib.parse; print(urllib.parse.quote('$kw'))")" 2>/dev/null | grep -o '<item>.*</item>' | head -5 &
done
wait

echo "Done! Check news_archive.json"
