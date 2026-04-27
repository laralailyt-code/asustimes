# 在 Render 上設置 Playwright

為了讓 Playwright 能在 Render 上正常運行，需要安裝系統依賴和配置 Build Command。

## 步驟 1：在 Render 儀表板配置 Build Command

1. 進入 Render Dashboard：https://dashboard.render.com
2. 選擇你的 ASUSTIMES 服務
3. 點擊 **Settings** → **Build & Deploy**
4. 在 **Build Command** 欄位，輸入以下命令：

```bash
apt-get update && apt-get install -y libglib2.0-0 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 libxext6 libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 libpangocairo-1.0-0 libatspi2.0-0 libxinerama1 libxi6 libxtst6 libnss3 libxss1 libasound2 libexpat1 libfontconfig1 fonts-dejavu-core libfreetype6 libssl3 libharfbuzz0b libfribidi0 libgraphite2-3 && pip install -r requirements.txt && playwright install chromium
```

5. 點擊 **Save Changes**

## 步驟 2：手動重新部署

1. 在 Render Dashboard 點擊 **Manual Deploy** 按鈕
2. 選擇 **Deploy latest commit**
3. 等待構建完成（通常需要 10-15 分鐘）

## 步驟 3：檢查日誌

構建期間，觀察日誌確認：
- ✅ `apt-get` 依賴安裝成功
- ✅ `pip install -r requirements.txt` 成功
- ✅ `playwright install chromium` 成功
- ✅ App 正常啟動

如果看到 Playwright 相關的錯誤，查看完整日誌並報告具體錯誤。

## 原理說明

Playwright 在 Linux 上運行 Chromium 需要這些系統級依賴：

| 依賴 | 用途 |
|------|------|
| libglib2.0-0, libatk*, libatspi2.0-0 | GTK 和無障礙框架 |
| libxkbcommon0, libxcomposite1, libxdamage1, libxext6, libxfixes3, libxrandr2 | X11 圖形系統 |
| libgbm1, libdrm2 | GPU 驅動 |
| libpango*, libharfbuzz0b, libfribidi0, libfreetype6, fonts-dejavu-core | 文字渲染 |
| libnss3, libxss1 | 安全和沙箱 |
| libasound2 | 音訊庫（雖然不需要，但 Chromium 期望它存在） |
| libexpat1, libssl3 | XML 和 SSL 支援 |
| libfontconfig1 | 字體配置 |

## 備用方案（如果 Playwright 仍失敗）

如果即使安裝依賴後 Playwright 仍無法工作，回退到 requests 備用方案：
1. 在 scraper.py 中禁用 Playwright 初始化
2. 使用 requests + BeautifulSoup 爬取最新文章（無歷史）
3. 改進搜尋關鍵字和結果頁數量

## 測試本地環境

在本地 Linux 上測試：
```bash
pip install -r requirements.txt
playwright install chromium
python3 -c "from playwright.sync_api import sync_playwright; p = sync_playwright().start(); print('✓ Playwright works!')"
```
