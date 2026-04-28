# ASUSTIMES 開發者指南
## 架構、部署與維護指南

---

## 📋 目錄

1. [快速開始](#快速開始)
2. [系統架構](#系統架構)
3. [環境設置](#環境設置)
4. [主要模組](#主要模組)
5. [新聞聚合](#新聞聚合)
6. [風險監控](#風險監控)
7. [數據更新](#數據更新)
8. [部署與維護](#部署與維護)
9. [常見問題](#常見問題)

---

## 快速開始

### 本地開發

```bash
# 1. 克隆代碼庫
git clone <repo-url>
cd news_platform

# 2. 安裝依賴
pip install -r requirements.txt

# 3. 設置環境變數（可選）
# .env 或在 Render 環境變數設置
# 詳見「環境設置」部分

# 4. 啟動本地伺服器
python app.py

# 5. 訪問：http://localhost:5000
```

### 部署到 Render

```bash
# 1. 確保 app.py、requirements.txt、Procfile 已準備
# 2. 推送至 GitHub master（自動部署）
git push origin master

# 3. Render 自動從 GitHub webhook 觸發部署
# 4. 訪問：https://asustimes.onrender.com
```

---

## 系統架構

### 技術棧

| 層級 | 技術 | 用途 |
|------|------|------|
| **後端** | Python Flask | HTTP API、數據聚合、定時任務 |
| **爬蟲** | requests + BeautifulSoup4 | RSS 解析、網頁爬蟲 |
| **瀏覽器自動化** | Playwright (Optional) | 動態網頁渲染（鎢粉、Digitimes） |
| **數據處理** | pandas | CSV 讀寫、時間序列 |
| **前端** | HTML5 + JavaScript | 交互界面、圖表渲染 |
| **圖表庫** | Chart.js | 時間序列圖表 |
| **地圖** | Leaflet | 地理信息顯示 |
| **部署** | Render | 雲託管、自動 CI/CD |

### 文件結構

```
news_platform/
├── app.py                          # Flask 主應用（核心業務邏輯）
├── scraper.py                      # 新聞爬蟲（RSS 聚合）
├── templates/
│   └── index.html                  # 前端界面
├── static/
│   ├── css/                        # 樣式表
│   ├── js/                         # JavaScript（交互、圖表）
│   └── data/                       # 靜態數據（地理坐標等）
├── 2026 Raw material trend history.csv  # 原物料歷史數據
├── watchlist.csv                   # 供應商風險監控列表
├── requirements.txt                # Python 依賴
├── Procfile                        # Render 部署配置
├── ASUSTIMES 使用說明書.md         # 用戶指南
└── DEVELOPER_GUIDE.md              # 本文件
```

---

## 環境設置

### 環境變數

```bash
# Render Dashboard 設置（Settings > Environment Variables）

# 1. News Feed 來源（可選）
DIGITIMES_EMAIL=<email>        # (已棄用，登入失敗後自動跳過)
DIGITIMES_PASSWORD=<password>  # (已棄用，登入失敗後自動跳過)

# 2. 日誌級別
LOG_LEVEL=INFO                 # DEBUG / INFO / WARNING / ERROR

# 3. API 連接
FLASK_ENV=production           # 設置為 production
```

### 本地開發環境

```bash
# 建議使用虛擬環境
python -m venv venv
source venv/bin/activate  # macOS/Linux
# 或
venv\Scripts\activate  # Windows

# 安裝依賴
pip install -r requirements.txt

# 可選：安裝開發工具
pip install pytest pytest-cov black flake8
```

### 外部依賴

#### 必須
- **requests** - HTTP 請求
- **beautifulsoup4** - RSS/HTML 解析
- **flask** - Web 框架
- **pandas** - 數據處理

#### 可選（已安裝）
- **playwright** - 瀏覽器自動化（鎢粉爬蟲）
- **yfinance** - Yahoo Finance 數據
- **lxml** - 快速 XML 解析

---

## 主要模組

### app.py - Flask 應用

**核心功能**：

1. **新聞 API** (`/api/news`)
   - 聚合來自 scraper.py 的新聞
   - 支持分類篩選、搜尋、排序
   - 定時刷新（30 分鐘）

2. **原物料 API** (`/api/commodity-history`)
   - 返回歷史價格數據
   - 支持多種來源（LME、SMM、sci99.com 等）
   - 每日自動更新

3. **風險評估 API** (`/api/risk/*`)
   - `/risk/strikes` - 罷工事件（8 週內）
   - `/risk/geopolitical` - 地緣政治事件（8 週內）
   - `/risk/clusters` - 供應商風險評分
   - 基於新聞發布日期篩選

4. **前端** (`/`)
   - 服務 index.html
   - 靜態資源（CSS、JS、圖片）

**關鍵函數**：

```python
# 新聞更新
_refresh_live_prices()        # 定時更新所有數據

# 風險監控
_scan_one_strike(target)      # 掃描單個廠商罷工事件
_do_strike_scan()             # 並行掃描所有廠商（8週內）
_scan_one_geo_risk(risk)      # 掃描地緣政治事件（8週內）
_do_geo_scan()                # 並行掃描所有地區

# 價格爬蟲
_fetch_smm_tungsten_powder_price()  # SMM 鎢粉價格
_fetch_pc_price_from_sci99()        # sci99.com PC 價格
```

### scraper.py - 新聞爬蟲

**功能**：

1. **RSS 聚合**
   - 解析 Digitimes、科技新報、iThome 等 RSS
   - 自動分類（AI、半導體、供應鏈等）
   - 去重與清理

2. **Bing News 搜尋**
   - 替代 Google News（因 Render IP 被屏蔽）
   - 支持 `site:` 搜尋操作符
   - 自動解析 Bing apiclick 重定向

3. **Digitimes 爬蟲**（已簡化）
   - 原本支持登入（現已跳過）
   - 通過 Bing News `site:digitimes.com` 搜尋

**Bing News URL 結構**：

```python
# 一般搜尋
url = "https://www.bing.com/news/search?format=rss&q=<keyword>"

# Site 搜尋
url = "https://www.bing.com/news/search?format=rss&q=site:digitimes.com+<keyword>"

# 注意：Bing 不支持 .tw 域名篩選，使用 .com
```

**關鍵函數**：

```python
def fetch_from_url(url, source_name, hint=""):
    """從 URL 抓取 RSS 並解析"""
    
def parse_date(raw: str) -> str:
    """解析 RFC 2822 日期格式，轉換為台北時間"""
    
def classify_category(title, summary="", hint="") -> str:
    """AI 分類新聞到 11 個類別"""
    
def scrape_digitimes_with_login():
    """Digitimes 爬蟲（現已簡化，主要用 Bing 搜尋）"""
```

---

## 新聞聚合

### 新聞來源

#### 直接 RSS（無需搜尋）
| 來源 | URL | 優先級 |
|------|-----|--------|
| Digitimes | `https://www.digitimes.com.tw/tech/rss/xml/xmlrss_*_*.xml` (29 個分類) | ⭐⭐⭐ |
| 科技新報 | `https://technews.tw/feed/` | ⭐⭐ |
| iThome | `https://www.ithome.com.tw/rss` | ⭐⭐ |
| 工商時報 | `https://www.ctee.com.tw/rss.xml` | ⭐ |

#### Bing News 搜尋（動態）
```python
# 格式
GN = "https://www.bing.com/news/search?format=rss&q="

# 例子
GN + "site:digitimes.com+筆電+PC"       # Digitimes PC 新聞
GN + "AI+伺服器+臺灣"                   # AI 伺服器新聞
GN + "workers+strike"                   # 罷工新聞
```

### 新聞分類邏輯

```python
# 11 個主要類別
CATEGORY_KEYWORDS = {
    "AI 產業": ["AI", "LLM", "ChatGPT", ...],
    "記憶體/儲存": ["DRAM", "NAND", ...],
    "半導體": ["TSMC", "晶片", ...],
    ...
}

# 供應鏈風險（特殊類別，優先不過濾）
_SUPPLY_CHAIN_RISK_KEYWORDS = {
    "strike": ["罷工", "工潮", "workers strike", ...],
    "disaster": ["地震", "颱風", "洪水", ...],
    ...
}

# 分類流程
if "AI" in title: return "AI 產業"
elif "供應鏈風險" in title: return "供應鏈/關稅"  # 優先
else: return None  # 過濾非科技新聞
```

### Bing News URL 解析

**問題**：Bing News RSS 的 `<link>` 是重定向 URL

```xml
<link>http://www.bing.com/news/apiclick.aspx?...&url=https%3a%2f%2factual-site.com%2fpath...</link>
```

**解決**：解析 `url` 參數獲取實際 URL

```python
if "bing.com/news/apiclick.aspx" in raw_url:
    from urllib.parse import urlparse, parse_qs
    qs = parse_qs(urlparse(raw_url).query)
    if "url" in qs:
        raw_url = qs["url"][0]  # 提取實際 URL
```

---

## 風險監控

### 罷工事件監控

**數據來源**：Bing News RSS（8 週內）

**監控對象** (`_STRIKE_TARGETS`):
```python
{
    "company": "Samsung",
    "kw": ["三星罷工", "Samsung strike", ...],
    "lat": 37.278, "lng": 127.009,
    "region": "Korea"
}
```

**關鍵驗證邏輯**（防止誤報）：

1. **罷工關鍵字檢查**
   ```python
   has_strike_action = any(kw in full_text for kw in [
       "罷工", "工潮", "workers strike", "labor strike",
       "strike threat", "strike action", ...
   ])
   ```

2. **排除非勞資爭議 "strike"**
   ```python
   has_exclude = any(kw in full_text for kw in [
       "strike deal", "strike price", "court strike",
       "legal strike", "patent strike", ...
   ])
   ```

3. **公司名驗證**
   ```python
   # 檢查文章標題是否包含該公司名稱
   if target["company"] not in result["title"]:
       reject_result()  # 防止誤歸類（如三星罷工被歸類為台積電）
   ```

4. **日期過濾**
   ```python
   cutoff = datetime.now(timezone.utc) - timedelta(days=56)  # 8 週
   if article_date < cutoff:
       skip_article()
   ```

**供應商頁面特殊篩選**：
```python
# API: /api/suppliers/<region>/summary
# 僅顯示過去 7 天內有新聞的罷工事件
cutoff_7day = datetime.now(TW_TZ) - timedelta(days=7)
if event_date < cutoff_7day:
    hide_event()  # 供應商頁面隱藏
```

### 地緣政治事件監控

**監控事件** (`_GEO_RISKS`):
```python
{
    "title": "台灣海峽地緣風險",
    "kw": ["台灣海峽", "Taiwan Strait", ...],
    "lat": 24.0, "lng": 120.0,
    "type": "Geopolitical",
    "impact": "HIGH"
}
```

**日期過濾**：過去 8 週（56 天）新聞

**地震特殊規則**：
- M ≥ 5.0 才顯示（避免誤報小地震）
- 不計入整體風險評分，但顯示在地圖上

---

## 數據更新

### 自動更新機制

```python
# app.py 主循環
def _live_price_loop():
    # 每天在 7、9、11、13、15、17 點（台北時間）更新
    _REFRESH_HOURS = {7, 9, 11, 13, 15, 17}
    
    while True:
        sleep(60)
        now_tw = datetime.now(timezone(timedelta(hours=8)))
        if now_tw.hour in _REFRESH_HOURS and key not in last_run_hour:
            _refresh_live_prices()  # 並行更新所有數據
            last_run_hour.add(key)
```

### 原物料價格來源

| 商品 | 來源 | 方法 | 優先級 |
|------|------|------|--------|
| **LME 金屬** | metals.live API | JSON API | ⭐⭐⭐ |
| **鎢粉** | SMM | Playwright 爬蟲 | ⭐⭐ |
| **黃磷** | Trading Economics | requests | ⭐⭐ |
| **PC** | sci99.com | BeautifulSoup | ⭐⭐ |
| **匯率** | Yahoo Finance | yfinance | ⭐⭐⭐ |

### 鎢粉爬蟲（性能待優化）

```python
def _fetch_smm_tungsten_powder_price() -> float | None:
    """從 SMM 爬取鎢粉價格（國產钨粉）"""
    try:
        from playwright.sync_api import sync_playwright
        
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            
            # 設置超時
            page.goto(
                "https://hq.smm.cn/h5/tungsten-powder-price",
                timeout=15000  # 15 秒
            )
            page.wait_for_load_state("networkidle", timeout=10000)
            
            # 抽取價格
            pattern = r'(\d{3,4})\s*-\s*(\d{3,4})'
            matches = re.findall(pattern, page.content())
            
            if matches:
                low, high = matches[0]
                avg = (float(low) + float(high)) / 2
                if 200 < avg < 5000:
                    return avg
```

**性能問題**：
- Playwright 啟動瀏覽器較慢（~5-10 秒）
- 網絡空閒等待（~3-5 秒）
- **總計**：每次更新 10-20 秒

**未來優化**：
- 增加緩存機制
- 背景線程非阻塞更新
- 減少超時設置（目前過於保守）

---

## 部署與維護

### Render 部署

**自動部署**：
1. Push 至 GitHub master
2. Render webhook 自動觸發
3. 執行 `pip install -r requirements.txt`
4. 運行 `gunicorn app:app` (Procfile 指定)

**Render 配置文件**：

```
# Procfile
web: gunicorn app:app

# requirements.txt
Flask==2.3.2
requests==2.31.0
beautifulsoup4==4.12.0
playwright==1.40.0
pandas==2.1.0
yfinance==0.2.32
gunicorn==21.2.0
```

### 監控與日誌

```bash
# Render Dashboard 查看實時日誌
https://dashboard.render.com -> Services -> ASUSTIMES -> Logs

# 本地日誌
python app.py 2>&1 | tee app.log
```

**關鍵日誌模式**：
```
[NEWS] ✓ Scraped 94 articles from Digitimes
[STRIKE] [Samsung] + '罷工': 3 items (status 200)
[STRIKE] ✓✓✓ ACCEPTED Samsung: 'Samsung workers strike...'
[GEO] [Taiwan Strait] + 'conflict': 2 items
[COMMODITY] Tungsten: 2450.5 CNY/kg from SMM
```

### 常見部署問題

**1. Render IP 被 Google 屏蔽**
- 症狀：所有 news.google.com 查詢返回 HTTP 503
- 解決：已遷移到 Bing News RSS

**2. Playwright 在 Render 上崩潰**
- 症狀：鎢粉價格始終為 None
- 原因：Render 容器缺少瀏覽器依賴
- 解決：未解決（可能需要 Docker 自定義鏡像）

**3. 記憶體溢出**
- 症狀：應用重啟或超時
- 原因：長時間運行堆積舊數據
- 解決：定期清理緩存（已在 _refresh_live_prices 中實現）

---

## 常見問題

### Q: 如何添加新的新聞來源？

**A**: 在 scraper.py 的 `FEEDS` 列表中添加：

```python
FEEDS = [
    # 直接 RSS
    {"url": "https://example.com/feed.xml", "source": "新源", "hint": "AI 產業"},
    
    # Bing News 搜尋
    {"url": GN + "site:example.com+keyword", "source": "新源", "hint": "半導體"},
]
```

### Q: 如何修改罷工監控對象？

**A**: 在 app.py 中編輯 `_STRIKE_TARGETS`：

```python
_STRIKE_TARGETS = [
    {
        "company": "新廠商",
        "kw": ["罷工關鍵字", "strike keyword"],
        "lat": 35.0, "lng": 130.0,
        "region": "Japan"
    }
]
```

### Q: 如何調試新聞分類問題？

**A**: 

```python
# scraper.py 中添加調試日誌
def classify_category(title, summary="", hint=""):
    logger.info(f"[DEBUG] Classifying: {title[:50]}")
    # ... 分類邏輯 ...
    logger.info(f"[DEBUG] Result: {best_category}")
    return best_category
```

### Q: 如何優化鎢粉爬蟲性能？

**A**: 目前方案（SMM only）已確認可行。待優化方向：
1. 減少 Playwright 超時時間（目前過於保守）
2. 實現結果緩存，避免每次都爬蟲
3. 使用後台線程非阻塞更新

---

## 版本資訊

**文件版本**：v1.1 (2026-04-28)
**最後更新**：Bing News 遷移、8 週事件過濾、罷工驗證邏輯
**維護者**：ASUSTIMES 開發團隊

---

## 参考資源

- [Flask 文檔](https://flask.palletsprojects.com/)
- [Bing News RSS](https://www.bing.com/news)
- [Playwright 文檔](https://playwright.dev/)
- [Render 部署指南](https://render.com/docs)
