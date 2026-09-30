# 📈 AI 台股即時看盤與量化決策預測系統

一個專為行動裝置與桌面端設計的現代化台股看盤與量化決策工具。具備全台股代碼模糊檢索、TradingView 日 K 線互動圖表、自適應暗黑模式，以及基於動能均線與量能的即時買賣信號推薦。

---

## 🌟 核心特色

- **全台股即時模糊搜尋**：整合證交所與櫃買中心近 2,000 檔上市櫃股票與主流 ETF，支援毫秒級代號與名稱比對。
- **TradingView 互動式走勢圖**：採用 TradingView Lightweight Charts，支援雙指滑動縮放、成交量柱狀圖與紅漲綠跌自定義配色。
- **量化決策與停損推薦引擎**：
  - **趨勢判斷**：MA5 與 MA20 黃金交叉 / 死亡交叉動能判定。
  - **量能共振**：盤中爆量突破篩選（相對 5 日均量）。
  - **超買超賣預警**：14 日 RSI 相對強弱指標。
  - **風控機制**：自動計算動態防守停損價。
- **沉浸式暗黑介面**：針對手機螢幕最佳化排版，支援 10 秒靜默輪詢（Silent Polling）即時更新。

---

## 🛠️ 技術棧

- **後端框架**：Python 3.10+, FastAPI, Uvicorn
- **資料處理與運算**：Pandas, yfinance, Requests
- **前端介面**：HTML5, JavaScript (ES6+), Tailwind CSS
- **圖表視覺化**：TradingView Lightweight Charts v4

---

## 🚀 快速開始

### 1. 取得專案並安裝依賴
```bash
git clone [https://github.com/yenwei0518/tw-stock-prediction-system.git](https://github.com/yenwei0518/tw-stock-prediction-system.git)
cd tw-stock-prediction-system
pip install -r requirements.txt