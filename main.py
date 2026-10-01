import os
from typing import Optional, List, Dict, Any
from fastapi import FastAPI, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel, field_validator
import yfinance as yf
import pandas as pd
import numpy as np

app = FastAPI(
    title="AI 台股量化決策終端 API",
    description="提供台股即時走勢、均線、RSI、籌碼量能、基本面財務數據與量化買賣訊號",
    version="2.0.0"
)

# 允許跨域請求 (CORS)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 常見上櫃 (TWO) 代碼清單，其餘預設加上 .TW
OTC_SYMBOLS = {"6547", "3293", "8069", "5483", "6488", "3131", "3529", "8299", "6274"}

# 預設熱門股票清單 (用於模糊搜尋推薦)
POPULAR_STOCKS = [
    {"symbol": "2330", "name": "台積電", "market": "上市"},
    {"symbol": "0050", "name": "元大台灣50", "market": "ETF"},
    {"symbol": "2454", "name": "聯發科", "market": "上市"},
    {"symbol": "2317", "name": "鴻海", "market": "上市"},
    {"symbol": "2382", "name": "廣達", "market": "上市"},
    {"symbol": "2308", "name": "台達電", "market": "上市"},
    {"symbol": "2881", "name": "富邦金", "market": "上市"},
    {"symbol": "2882", "name": "國泰金", "market": "上市"},
    {"symbol": "2603", "name": "長榮", "market": "上市"},
    {"symbol": "3231", "name": "緯創", "market": "上市"},
    {"symbol": "2376", "name": "技嘉", "market": "上市"},
    {"symbol": "0056", "name": "元大高股息", "market": "ETF"},
    {"symbol": "00878", "name": "國泰永續高股息", "market": "ETF"},
    {"symbol": "00919", "name": "群益台灣精選高息", "market": "ETF"},
    {"symbol": "00929", "name": "復華台灣科技優息", "market": "ETF"},
]


def get_ticker_symbol(symbol: str) -> str:
    """轉換台股代號為 Yahoo Finance 相容格式"""
    sym = symbol.strip().upper()
    if sym.endswith(".TW") or sym.endswith(".TWO"):
        return sym
    if sym in OTC_SYMBOLS:
        return f"{sym}.TWO"
    return f"{sym}.TW"


# ==========================================
# 1. Pydantic v2 基本面資料清洗模型
# ==========================================
class FundamentalData(BaseModel):
    pe: Optional[float] = None
    eps: Optional[float] = None
    yield_rate: Optional[float] = None
    market_cap: Optional[float] = None

    @field_validator("pe", "eps", "yield_rate", "market_cap", mode="before")
    @classmethod
    def clean_financial_metrics(cls, val: object) -> Optional[float]:
        # 攔截空值或 Yahoo 的佔位符號
        if val is None or val in ("--", "N/A", "NA", "-", "", "None", "null"):
            return None

        # 數值型別直接轉換
        if isinstance(val, (int, float)):
            return float(val)

        # 字串型別清除雜訊 (逗號、百分比)
        if isinstance(val, str):
            clean_str = val.replace(",", "").replace("%", "").strip()
            if clean_str in ("--", "N/A", "NA", "-", "", "None", "null"):
                return None
            try:
                return float(clean_str)
            except ValueError:
                return None

        return None

    def to_display_dict(self) -> dict:
        """轉換為前端易讀的格式化字串"""
        mcap_str = "--"
        if self.market_cap and self.market_cap > 0:
            if self.market_cap >= 1e12:
                mcap_str = f"{round(self.market_cap / 1e12, 2)} 兆"
            elif self.market_cap >= 1e8:
                mcap_str = f"{round(self.market_cap / 1e8, 2)} 億"
            else:
                mcap_str = f"{int(self.market_cap):,}"

        yield_str = "--"
        if self.yield_rate and self.yield_rate > 0:
            rate = self.yield_rate * 100 if self.yield_rate < 1 else self.yield_rate
            yield_str = f"{round(rate, 2)}%"

        return {
            "pe": f"{round(self.pe, 2)} 倍" if self.pe and self.pe > 0 else "--",
            "eps": f"{round(self.eps, 2)} 元" if self.eps is not None else "--",
            "yield": yield_str,
            "market_cap": mcap_str,
        }


# ==========================================
# 2. 技術指標輔助計算函式
# ==========================================
def calculate_rsi(series: pd.Series, period: int = 14) -> float:
    """計算 14 日 RSI 相對強弱指標"""
    if len(series) < period + 1:
        return 50.0
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = gain.rolling(window=period, min_periods=period).mean()
    avg_loss = loss.rolling(window=period, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    val = rsi.iloc[-1]
    return round(float(val), 2) if not np.isnan(val) else 50.0


# ==========================================
# 3. 核心 API 端點
# ==========================================
@app.get("/api/stocks/search")
def search_stocks(q: str = Query(..., min_length=1)):
    """股票模糊搜尋 (支援代碼與中文名稱)"""
    query = q.strip().lower()
    results = [
        item for item in POPULAR_STOCKS 
        if query in item["symbol"].lower() or query in item["name"].lower()
    ]
    # 若為自訂 4 碼數字且不在預設清單中，自動建立項目
    if query.isdigit() and len(query) >= 4 and not any(r["symbol"] == query for r in results):
        results.insert(0, {"symbol": query, "name": f"台股 {query}", "market": "台股"})
    return results[:8]


@app.get("/api/stocks/{symbol}/kline")
def get_kline(symbol: str):
    """取得日線 K 棒資料、MA5、MA20 與成交量 (供 lightweight-charts 繪圖)"""
    try:
        ticker = get_ticker_symbol(symbol)
        df = yf.download(ticker, period="6mo", interval="1d", progress=False, auto_adjust=False)

        # 嘗試備用代碼 (例如 .TWO)
        if df.empty and ticker.endswith(".TW"):
            ticker_alt = ticker.replace(".TW", ".TWO")
            df = yf.download(ticker_alt, period="6mo", interval="1d", progress=False, auto_adjust=False)

        if df.empty:
            raise HTTPException(status_code=404, detail=f"查無 {symbol} 的歷史交易資料")

        # 處理 yfinance 可能回傳的多層欄位 (MultiIndex)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        df = df.dropna(subset=['Close'])
        df['MA5'] = df['Close'].rolling(window=5).mean()
        df['MA20'] = df['Close'].rolling(window=20).mean()

        kline_data = []
        volume_data = []
        ma5_data = []
        ma20_data = []

        for idx, row in df.iterrows():
            date_str = idx.strftime("%Y-%m-%d")
            o = round(float(row['Open']), 2)
            h = round(float(row['High']), 2)
            l = round(float(row['Low']), 2)
            c = round(float(row['Close']), 2)
            v = int(row['Volume']) if not np.isnan(row['Volume']) else 0

            kline_data.append({"time": date_str, "open": o, "high": h, "low": l, "close": c})

            # 台股紅漲綠跌
            vol_color = "rgba(239, 68, 68, 0.5)" if c >= o else "rgba(34, 197, 94, 0.5)"
            volume_data.append({"time": date_str, "value": v, "color": vol_color})

            if not np.isnan(row['MA5']):
                ma5_data.append({"time": date_str, "value": round(float(row['MA5']), 2)})
            if not np.isnan(row['MA20']):
                ma20_data.append({"time": date_str, "value": round(float(row['MA20']), 2)})

        return {
            "symbol": symbol,
            "kline": kline_data,
            "volume": volume_data,
            "ma5": ma5_data,
            "ma20": ma20_data,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"K 線資料處理失敗: {str(e)}")


@app.get("/api/stocks/{symbol}/signal")
def get_signal(symbol: str):
    """計算即時技術面診斷、量能比、動態停損與買賣觀點"""
    try:
        ticker = get_ticker_symbol(symbol)
        df = yf.download(ticker, period="3mo", interval="1d", progress=False, auto_adjust=False)

        if df.empty and ticker.endswith(".TW"):
            ticker_alt = ticker.replace(".TW", ".TWO")
            df = yf.download(ticker_alt, period="3mo", interval="1d", progress=False, auto_adjust=False)

        if df.empty or len(df) < 5:
            raise HTTPException(status_code=404, detail="交易天數不足以計算指標")

        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        df = df.dropna(subset=['Close'])
        latest_row = df.iloc[-1]
        latest_close = round(float(latest_row['Close']), 2)

        # 計算均線
        ma5 = float(df['Close'].tail(5).mean())
        ma20 = float(df['Close'].tail(20).mean()) if len(df) >= 20 else ma5

        # 計算 RSI
        rsi_val = calculate_rsi(df['Close'], period=14)

        # 今日成交量與近 5 日均量對比 (量能比)
        vol_today = float(latest_row['Volume'])
        vol_avg5 = float(df['Volume'].tail(5).mean()) if len(df) >= 5 else vol_today
        vol_ratio = round(vol_today / vol_avg5, 2) if vol_avg5 > 0 else 1.0

        # 動態防守停損價 (以 MA20 支撐或近 5 日低點作為參考)
        recent_low = float(df['Low'].tail(5).min())
        stop_loss = round(min(ma20, recent_low * 0.99), 2)

        # 量化訊號與決策分析
        reasons = []
        signal = "區間整理 (NEUTRAL HOLD)"

        if latest_close >= ma20:
            reasons.append("股價站穩月線 (MA20) 之上，維持多方走勢")
        else:
            reasons.append("股價位於月線 (MA20) 之下，短線偏弱震盪")

        if rsi_val >= 70:
            reasons.append("RSI 處於超買熱區，防範衝高拉回")
        elif rsi_val <= 30:
            reasons.append("RSI 進入超賣低檔，醞釀技術性反彈")
        else:
            reasons.append(f"RSI 為 {rsi_val}，動能處於中性健康區間")

        if vol_ratio >= 1.3:
            reasons.append(f"量能增溫 (量能比 {vol_ratio}x)，資金交投熱絡")

        # 訊號判定
        if latest_close > ma20 and ma5 > ma20 and rsi_val > 50:
            signal = "多頭持有 (BULLISH HOLD)"
        elif latest_close > ma20 and vol_ratio >= 1.3:
            signal = "帶量突破 (BUY)"
        elif latest_close < ma20 and ma5 < ma20:
            signal = "空頭防守 (BEARISH AVOID)"

        return {
            "symbol": symbol,
            "latest_close": latest_close,
            "rsi": rsi_val,
            "volume_ratio": vol_ratio,
            "stop_loss_price": stop_loss,
            "signal": signal,
            "reasons": reasons
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"技術指標計算失敗: {str(e)}")


@app.get("/api/stocks/{symbol}/fundamental")
def get_fundamental(symbol: str):
    """取得股票基本面核心指標 (PE, EPS, 殖利率, 市值)"""
    ticker = get_ticker_symbol(symbol)
    raw_payload = {}

    try:
        tk = yf.Ticker(ticker)

        # 1. 市值優先由 fast_info 取得 (海外雲端最穩定)
        try:
            raw_payload["market_cap"] = tk.fast_info.market_cap
        except Exception:
            raw_payload["market_cap"] = None

        # 2. 本益比、EPS、殖利率由 info 補充
        try:
            info = tk.info or {}
            raw_payload["pe"] = info.get("trailingPE") or info.get("forwardPE")
            raw_payload["eps"] = info.get("trailingEps")
            raw_payload["yield_rate"] = info.get("dividendYield")
        except Exception:
            pass

    except Exception as e:
        print(f"yfinance 讀取異常: {e}")

    # 使用 Pydantic v2 模型過濾與格式化
    cleaned = FundamentalData(**raw_payload)
    return cleaned.to_display_dict()


# ==========================================
# 4. 靜態檔案託管 (自動相容不同目錄結構)
# ==========================================
if os.path.exists("static"):
    app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
def serve_index():
    """首頁路由：自動尋找 static/index.html 或根目錄 index.html"""
    if os.path.exists("static/index.html"):
        return FileResponse("static/index.html")
    elif os.path.exists("index.html"):
        return FileResponse("index.html")
    return {"message": "AI Quant Stock API 運行中，但未找到 index.html 前端檔案"}