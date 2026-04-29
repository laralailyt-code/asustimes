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

### 風險指數計算公式

每個供應鏈集群（cluster）的風險分數是由「最近 21 天內」的相關新聞累加而成，上限 **100**。

**公式**：
```
最終分數 = Σ (基礎權重 × 時間衰減 × 修正係數)
```

**基礎權重**（`app.py` `weights` dict）：

| 風險類型 | 權重 |
|---------|------|
| 🌪 災害 disaster | 30 |
| ⚔️ 地緣政治 geopolitical | 20 |
| ✊ 罷工 strike | 20 |
| ⚡ 操作異常 operational | 15 |
| 💸 財務 financial | 0（不計分） |

**修正係數**：

| 條件 | 倍率 |
|------|------|
| 7 天內 | × 1.0 |
| 第 8 天起每天遞減 10% | `max(0.3, 1 - (days-7)×0.1)` |
| 超過 21 天 | 跳過（不計分） |
| 罷工/地緣政治「未確認」 | × 0.6 |
| 罷工/操作異常「< 7 天短期」 | × 0.7 |

**篩選門檻**（不通過 → 0 分）：
1. 新聞必須提到關鍵晶圓廠（TSMC / Samsung / SK Hynix / Micron / Intel...）
2. 必須能識別出受影響地區（透過 `_CLUSTER_KEYWORDS` 或 `_FAB_TO_REGIONS`）
3. 地震必須 **M ≥ 5.0**
4. 颱風必須 **3 天內** + **嚴重級別**（強颱、超強颱）

**警示閾值**（前端顯示）：
- 🔴 ≥ 60：立即關注
- 🟡 ≥ 28：持續監控
- 🟢 < 28：正常運營

**舉例**：
```python
# 例 1：台灣 M6.0 地震（今天）
30（disaster） × 1.0（時間衰減） = +30
→ 🟡 黃燈

# 例 2：Samsung 罷工，10 天前，確認且持續
20 × max(0.3, 1-(10-7)*0.1) = 20 × 0.7 = +14
→ 🟢 綠燈

# 例 3：以伊衝突（5 天前確認）+ 台積電罷工（1 天前確認）
20 × 1.0 + 20 × 1.0 = 40
→ 🟡 黃燈
```

**程式位置**：`app.py` 函式 `cluster_risk()`（約 3205 行）。

---

## 數據更新

> 本章節列出 Flask 啟動時拉起的所有背景執行緒、各類資料的來源與抓取方式。
> 業務面的「資料來源與更新頻率」整合表請見 `資訊來源與更新頻率報告.md`。

### 背景執行緒總覽

Flask 啟動後會在 `_ensure_bg_running()` 拉起 **6 個 daemon thread**：

| Thread 名 | 函式 | 頻率 | 用途 |
|-----------|------|------|------|
| `(news)` | `background_refresh_loop` | 30 分鐘 | 新聞主刷新 |
| `(digitimes)` | `_digitimes_refresh_loop` | 2 小時 | Digitimes 強化（透過 Bing News）|
| `(commodity)` | `_live_price_loop` | 整點 6 次 (07/09/11/13/15/17 TW) | 商品價格 |
| `(risk)` | `_risk_cache_preload_loop` | 3 小時 | 罷工 + 地緣政治預熱 |
| `(digest)` | `daily_digest_loop` | 每日 1 次 | 摘要 email |
| `telegram-bot` | `_telegram_bot_loop` | 持續 | PTB polling（M4 部署後改 webhook）|

### 商品價格更新邏輯

```python
def _live_price_loop():
    _load_commodity_csv_to_cache()
    _refresh_live_prices()                         # 啟動時跑一次
    _REFRESH_HOURS = {7, 9, 11, 13, 15, 17}       # 台北時間
    last_run_hour: set = set()
    while True:
        time.sleep(60)
        now_tw = datetime.now(timezone(timedelta(hours=8)))
        key = (now_tw.date(), now_tw.hour)
        if now_tw.hour in _REFRESH_HOURS and key not in last_run_hour:
            last_run_hour.add(key)
            _refresh_live_prices()
```

### 商品來源 vs 抓取方式（截至 2026-04-29）

| 商品 | 來源 | 抓取方式 | 備註 |
|------|------|---------|------|
| **鈷/銅/鋁/錫/鎳/鋅** | metals.live `/v1/spot/{slug}` | requests + JSON | TE fallback 已關（bid vs settlement 基準差）|
| **鋰** | tradingeconomics.com | requests + regex 爬取 | 唯一仍用 TE 的金屬（無更佳來源）|
| **鎢粉** | SMM (smm.cn) | Playwright headless 爬蟲 | 啟動慢（10–20 秒）|
| **黃磷** | sci99.com `/priceMonitor/listProductPagePrice?oldId=678` | requests + JSON | 2026-04-29 從 HTML 改 JSON API |
| **PC 塑料** | sci99.com `/priceMonitor/listProductPagePrice?oldId=68` | requests + JSON | 同上 |
| **ABS 聚合物 / 瓦楞芯紙** | bot.com.tw BCD API | requests | 720 天日線歷史 |
| **長纖紙漿** | MoneyDJ + 歷史檔 fallback | requests | SSL 失敗時退到 hardcoded `_LONGFIBER_PULP_HISTORY` |
| **金/銀/油 WTI/Brent** | Yahoo Finance v8 chart API（直接 HTTP）| requests | 不依賴 yfinance lib |
| **匯率 TWD/CNY/JPY/KRW/EUR** | 同上（`{ccy}=X` ticker） | requests | 同上 |
| **歷史回填** | 使用者提供 Excel | `tools/merge_excel_history.py` | 對鈷/錫/鎳/鋅/鋰 等只能取得當日值的金屬必要 |

### sci99.com JSON API 範例

```python
def _fetch_sci99_price(old_id: int, label: str = "") -> tuple[float | None, str | None]:
    """sci99.com 改成 JS 渲染後，舊的 BeautifulSoup 表格解析失效。
    改用網站自己呼叫的 AJAX 端點。"""
    headers = {
        "User-Agent": "Mozilla/5.0 ...",
        "Accept":     "application/json, text/plain, */*",
        "Referer":    f"https://www.sci99.com/monitor-{old_id}-0.html",
    }
    r = req_lib.get(
        "https://www.sci99.com/priceMonitor/listProductPagePrice",
        params={"oldId": old_id, "type": 0},
        headers=headers, timeout=12,
    )
    body = r.json()
    first = body["data"][0]
    return float(first["mdataValue"].replace(",", "")), first["dateRange"]
```

oldId 對照：黃磷 = 678，PC 塑料 = 68（即 monitor-{N}-0.html 的數字）。

### Yahoo Finance v8（不依賴 yfinance lib）

```python
def fetch_yahoo(symbol: str, days: int = 60):
    end = int(time.time())
    start = end - days * 86400
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
           f"?period1={start}&period2={end}&interval=1d")
    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
    body = r.json()
    result = body["chart"]["result"][0]
    timestamps = result["timestamp"]
    closes = result["indicators"]["quote"][0]["close"]
    return [(date.fromtimestamp(ts), float(c))
            for ts, c in zip(timestamps, closes) if c is not None]
```

> 為什麼放棄 yfinance lib：Render pip 安裝失敗（lxml 依賴衝突）。直接 HTTP 反而更穩。

### 鎢粉 Playwright 爬蟲（仍是性能瓶頸）

```python
def _fetch_smm_tungsten_powder_price() -> float | None:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto("https://hq.smm.cn/h5/tungsten-powder-price", timeout=15000)
        page.wait_for_load_state("networkidle", timeout=10000)
        pattern = r'(\d{3,4})\s*-\s*(\d{3,4})'
        matches = re.findall(pattern, page.content())
        if matches:
            low, high = matches[0]
            avg = (float(low) + float(high)) / 2
            if 200 < avg < 5000:
                return avg
```

**性能**：每次更新 10–20 秒（瀏覽器啟動 5–10s + networkidle 3–5s）。
**待優化**：HTTP-only 端點探勘、降低 timeout、結果快取。

### Carry-forward 機制

`_save_commodity_csv()` 寫入 CSV 時呼叫 `_apply_carry_forward(rows, header)`：

```python
def _apply_carry_forward(rows, header, carry_back_days: int = 30) -> None:
    # Pass 1：carry-forward — 空白格 ← 最近一筆真實值，標 '*'
    # Pass 2：carry-back   — 第一筆真實點之前的空白 ← 第一筆真實值，
    #                       但只在過去 carry_back_days 天內套用
    ...
```

**規則**：
- 真實值 → 寫進去不帶 `*`
- 沿用值 → 寫進去帶 `*`（如 `27000*`）
- 真實值會自動覆蓋舊的 `*`（cache 是 source of truth）
- 30 天前的歷史空白保持空白（不污染長期趨勢圖）

`_parse_commodity_csv()` 讀回時用 `clean = v.replace(",", "").rstrip("*")` 剝除 `*` 才解析浮點。

### 新聞抓取（scraper.py）

| 來源類型 | 範例 | 取得方式 |
|---------|------|---------|
| 直接 RSS | `technews.tw/feed/`, `ithome.com.tw/rss`, `cool3c-all`, `ctee.com.tw/rss.xml` | `requests` + `feedparser` |
| Bing News 站內搜尋 | `https://www.bing.com/news/search?format=rss&q=site:digitimes.com+...` | 同上 |
| AI 摘要 | Claude（`anthropic` package） | `messages.create()` |

**Bing 連結解碼**（從 `bing.com/news/apiclick.aspx?...&url=X` 提取真實 X）：

```python
if "bing.com/news/apiclick.aspx" in article_url:
    qs = parse_qs(urlparse(article_url).query)
    if "url" in qs:
        article_url = unquote(qs["url"][0])
```

### Telegram Bot 推播

詳見 `telegram_bot/` 子套件。重點：

| 模組 | 角色 |
|------|------|
| `bot.py` | PTB Application 工廠 + polling 入口 |
| `db.py` | psycopg2 ThreadedConnectionPool + CRUD |
| `event_persister.py` | 把 scan 結果寫進 `risk_events` 表 |
| `matcher.py` | 訂閱命中（含 30 分/24 小時新鮮度過濾）|
| `notifier.py` | 推播 worker（限速 25/s、403 自動停用、429 退避）|
| `handlers/basic.py` | `/start /help /list /clear` + Reply Keyboard |
| `handlers/subscribe_wizard.py` | 4 步驟訂閱精靈（ConversationHandler）|
| `handlers/quick_subscribe.py` | 4 個快速指令（地區/料件/供應商/半徑）|

新事件流：
```
_do_geo_scan / _do_strike_scan
  → results 寫進 _strike_cache / _geo_risk_cache
  → 同時呼叫 _persist_events_async() 把 results 寫到 Supabase risk_events（INSERT IF NOT EXISTS）
  → dispatcher（PTB job_queue, 每 60s）撈 notified=false 的事件
  → matcher.find_hits() 找命中
  → notifier.push_event_to_users() 推 + 標記 notified=true
```

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
