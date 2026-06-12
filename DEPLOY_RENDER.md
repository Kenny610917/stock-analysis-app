# Stock analysis 雲端部署到 Render

這個版本可以部署成 Render Python Web Service。部署後會取得固定的 `onrender.com` 網址，不需要讓本機電腦保持開機。

## 部署前

1. 把這個專案推到 GitHub。
2. 到 Render 建立帳號並連接 GitHub。

## 用 Blueprint 部署

1. 在 Render Dashboard 選 New > Blueprint。
2. 選擇包含本專案的 GitHub repository。
3. Render 會讀取根目錄的 `render.yaml`。
4. 建立服務後，等待部署完成。
5. 完成後打開 Render 顯示的 `https://...onrender.com` 網址。

## 手動建立 Web Service 時的設定

- Runtime: Python
- Region: Singapore
- Build Command:

```bash
python -m py_compile nvda-bollinger-app/server.py stock-analysis-skill-main/tools/bollinger_volume_strategy.py
```

- Start Command:

```bash
python nvda-bollinger-app/server.py --host 0.0.0.0 --port $PORT
```

- Health Check Path:

```text
/health
```

## 注意

- Render Free instance 可能會休眠，第一次打開可能需要等它喚醒。
- 全市場掃描會呼叫 Nasdaq Trader、TWSE、TPEx 和 Yahoo Finance；若一次掃太多檔，可能遇到行情來源限速。
- 台股股票池使用 TWSE 上市公司 OpenAPI 與 TPEx 上櫃公司 OpenAPI，並自動轉成 Yahoo Finance 的 `.TW` / `.TWO` 代號格式。
- 這個 app 已支援 PWA。部署到 HTTPS 網址後，可在 Chrome/Edge 使用網址列的安裝按鈕，Android Chrome 使用「安裝應用程式」或「加到主畫面」，iPhone Safari 使用分享選單的「加入主畫面」。
- 這個工具只提供規則型技術訊號研究，不構成投資建議。
