import json
import re

# Read the malformed JSON
with open('news_archive.json', 'r', encoding='utf-8') as f:
    content = f.read()

# Extract all article objects using regex
articles = []
pattern = r'\{\s*"source"[^}]*"category"[^}]*\}'
matches = re.findall(pattern, content, re.DOTALL)

for match in matches:
    try:
        # Clean up the match
        article_str = match.replace('\n  ', ' ').strip()
        # Fix escaped characters
        article_str = article_str.replace('&amp;', '&')
        article = json.loads(article_str)
        articles.append(article)
    except:
        pass

# Write clean JSON
with open('news_archive.json', 'w', encoding='utf-8') as f:
    json.dump(articles, f, ensure_ascii=False, indent=2)

print(f"✓ Cleaned JSON: {len(articles)} articles")
