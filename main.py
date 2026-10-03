import os
import time
from typing import Optional, List, Dict, Any
from fastapi import FastAPI, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, field_validator
import yfinance as yf
import pandas as pd
import numpy as np
import requests
import uvicorn

app = FastAPI(
    title="AI 台股量化決策終端系統",
    description="整合全台股代碼名稱庫、TWSE 官方資料與 TradingView 看盤系統",
    version="3.1.0"
)

# 跨域連線配置 (CORS)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 常見上櫃 (TWO) 代碼清單，其餘預設加上 .TW
OTC_SYMBOLS = {"6547", "3293", "8069", "5483", "6488", "3131", "3529", "8299", "6274"}

# 預設備援熱門清單
DEFAULT_STOCKS = [
    {"symbol": "2330", "name": "台積電", "market": "上市"},
    {"symbol": "0050", "name": "元大台灣50", "market": "ETF"},
    {"symbol": "2454", "name": "聯發科", "market": "上市"},
    {"symbol": "2317", "name": "鴻海", "market": "上市"},
    {"symbol": "3481", "name": "群創", "market": "上市"},
    {"symbol": "2382", "name": "廣達", "market": "上市"},
    {"symbol": "2308", "name": "台達電", "market": "上市"},
    {"symbol": "2881", "name": "富邦金", "market": "上市"},
    {"symbol": "2882", "name": "國泰金", "market": "上市"},
    {"symbol": "2603", "name": "長榮", "market": "上市"},
    {"symbol": "3231", "name": "緯創", "market": "上市"},
    {"symbol": "2376", "name": "技嘉", "market": "上市"},
    {"symbol": "8069", "name": "元太", "market": "上櫃"},
    {"symbol": "0056", "name": "元大高股息", "market": "ETF"},
    {"symbol": "00878", "name": "國泰永續高股息", "market": "ETF"},
    {"symbol": "00919", "name": "群益台灣精選高息", "market": "ETF"},
    {"symbol": "00929", "name": "復華台灣科技優息", "market": "ETF"},
]


def get_ticker_symbol(symbol: str) -> str:
    """轉換為 Yahoo Finance 代號格式"""
    sym = symbol.strip().upper()
    if sym.endswith(".TW") or sym.endswith(".TWO"):
        return sym
    if sym in OTC_SYMBOLS:
        return f"{sym}.TWO"
    return f"{sym}.TW"


# ==========================================
# 1. 全台股股票代號與名稱資料庫 + 官方快取
# ==========================================
STOCK_DATABASE: Dict[str, dict] = {s["symbol"]: s for s in DEFAULT_STOCKS}
TWSE_FUNDAMENTAL_CACHE: Dict[str, dict] = {}
LAST_FETCH_TIME = 0
CACHE_TTL = 3600 * 4  # 快取 4 小時


def update_market_cache():
    """向臺灣證券交易所 OpenAPI 獲取全市場代碼、名稱、本益比與殖利率"""
    global STOCK_DATABASE, TWSE_FUNDAMENTAL_CACHE, LAST_FETCH_TIME
    now = time.time()
    if TWSE_FUNDAMENTAL_CACHE and (now - LAST_FETCH_TIME) < CACHE_TTL:
        return

    url = "https://openapi.twse.com.tw/v1/exchangeReport/BWIBBU_ALL"
    headers = {"User-Agent": "Mozilla/5.0"}

    try:
        resp = requests.get(url, headers=headers, timeout=6)
        if resp.status_code == 200:
            data = resp.json()
            new_fund_cache = {}
            for item in data:
                code = str(item.get("Code", "")).strip()
                name = str(item.get("Name", "")).strip()
                if not code:
                    continue

                # 1. 自動擴充全台股代碼與中文名稱搜尋庫 (解決如 3481 -> 群創 的對應問題)
                if name:
                    STOCK_DATABASE[code] = {
                        "symbol": code,
                        "name": name,
                        "market": "上市"
                    }

                # 2. 儲存本益比與殖利率數據
                pe_val = None
                pe_str = str(item.get("PEratio", "")).replace(",", "").strip()
                if pe_str and pe_str not in ("-", "--", "N/A"):
                    try:
                        pe_val = float(pe_str)
                    except ValueError:
                        pass

                yield_val = None
                yield_str = str(item.get("DividendYield", "")).replace(",", "").replace("%", "").strip()
                if yield_str and yield_str not in ("-", "--", "N/A"):
                    try:
                        yield_val = float(yield_str)
                    except ValueError:
                        pass

                new_fund_cache[code] = {
                    "pe": pe_val,
                    "yield_rate": yield_val
                }

            TWSE_FUNDAMENTAL_CACHE = new_fund_cache
            LAST_FETCH_TIME = now
            print(f"[系統快取] 已自動同步全市場股票資料庫，涵蓋 {len(STOCK_DATABASE)} 檔股票")
    except Exception as e:
        print(f"[系統快取] 取得失敗: {e}")


# 伺服器啟動時在背景預熱快取
@app.on_event("startup")
def startup_event():
    try:
        update_market_cache()
    except Exception:
        pass


# ==========================================
# 2. Pydantic v2 基本面資料清洗模型
# ==========================================
class FundamentalData(BaseModel):
    pe: Optional[float] = None
    eps: Optional[float] = None
    yield_rate: Optional[float] = None
    market_cap: Optional[float] = None

    @field_validator("pe", "eps", "yield_rate", "market_cap", mode="before")
    @classmethod
    def clean_metrics(cls, val: object) -> Optional[float]:
        if val is None or val in ("--", "N/A", "NA", "-", "", "None", "null"):
            return None
        if isinstance(val, (int, float)):
            return float(val)
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
            yield_str = f"{round(self.yield_rate, 2)}%"

        return {
            "pe": f"{round(self.pe, 2)} 倍" if self.pe and self.pe > 0 else "--",
            "eps": f"{round(self.eps, 2)} 元" if self.eps is not None else "--",
            "yield": yield_str,
            "market_cap": mcap_str,
        }


# ==========================================
# 3. 技術面指標計算輔助函式
# ==========================================
def calculate_rsi(series: pd.Series, period: int = 14) -> float:
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
# 4. PWA 專屬路由
# ==========================================
@app.get("/manifest.json")
def get_manifest():
    path = "static/manifest.json" if os.path.exists("static/manifest.json") else "manifest.json"
    if os.path.exists(path):
        return FileResponse(path, media_type="application/manifest+json")
    raise HTTPException(status_code=404, detail="manifest.json 不存在")


@app.get("/sw.js")
def get_service_worker():
    path = "static/sw.js" if os.path.exists("static/sw.js") else "sw.js"
    if os.path.exists(path):
        return FileResponse(path, media_type="application/javascript")
    raise HTTPException(status_code=404, detail="sw.js 不存在")


# ==========================================
# 5. 前端核心 API 端點
# ==========================================
@app.get("/api/stocks/search")
def search_stocks(q: str = Query(..., min_length=1)):
    """支援全台股代號與中文名稱模糊搜尋"""
    update_market_cache()
    query = q.strip().lower()

    exact_matches = []
    prefix_matches = []
    fuzzy_matches = []

    for symbol, info in STOCK_DATABASE.items():
        sym_lower = symbol.lower()
        name_lower = info["name"].lower()

        if query == sym_lower or query == name_lower:
            exact_matches.append(info)
        elif sym_lower.startswith(query) or name_lower.startswith(query):
            prefix_matches.append(info)
        elif query in sym_lower or query in name_lower:
            fuzzy_matches.append(info)

    results = exact_matches + prefix_matches + fuzzy_matches

    # 若為未登錄的 4 碼自訂代號，才使用通稱兜底
    if query.isdigit() and len(query) >= 4 and not any(r["symbol"] == query for r in results):
        results.insert(0, {"symbol": query, "name": f"台股 {query}", "market": "台股"})

    return results[:10]


@app.get("/api/stocks/{symbol}/kline")
def get_kline(symbol: str):
    try:
        ticker = get_ticker_symbol(symbol)
        df = yf.download(ticker, period="6mo", interval="1d", progress=False, auto_adjust=False)

        if df.empty and ticker.endswith(".TW"):
            ticker_alt = ticker.replace(".TW", ".TWO")
            df = yf.download(ticker_alt, period="6mo", interval="1d", progress=False, auto_adjust=False)

        if df.empty:
            raise HTTPException(status_code=404, detail=f"查無 {symbol} 走勢資料")

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
    try:
        ticker = get_ticker_symbol(symbol)
        df = yf.download(ticker, period="3mo", interval="1d", progress=False, auto_adjust=False)

        if df.empty and ticker.endswith(".TW"):
            ticker_alt = ticker.replace(".TW", ".TWO")
            df = yf.download(ticker_alt, period="3mo", interval="1d", progress=False, auto_adjust=False)

        if df.empty or len(df) < 5:
            raise HTTPException(status_code=404, detail="資料天數不足以計算指標")

        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        df = df.dropna(subset=['Close'])
        latest_row = df.iloc[-1]
        latest_close = round(float(latest_row['Close']), 2)

        ma5 = float(df['Close'].tail(5).mean())
        ma20 = float(df['Close'].tail(20).mean()) if len(df) >= 20 else ma5
        rsi_val = calculate_rsi(df['Close'], period=14)

        vol_today = float(latest_row['Volume'])
        vol_avg5 = float(df['Volume'].tail(5).mean()) if len(df) >= 5 else vol_today
        vol_ratio = round(vol_today / vol_avg5, 2) if vol_avg5 > 0 else 1.0

        recent_low = float(df['Low'].tail(5).min())
        stop_loss = round(min(ma20, recent_low * 0.99), 2)

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
    clean_code = symbol.split('.')[0].strip()
    ticker = get_ticker_symbol(symbol)
    
    raw_payload = {
        "pe": None,
        "eps": None,
        "yield_rate": None,
        "market_cap": None
    }
    latest_price = None

    # 1. 市值與價格由 fast_info 取得 (穩定且不被限流)
    try:
        tk = yf.Ticker(ticker)
        try:
            raw_payload["market_cap"] = tk.fast_info.market_cap
            latest_price = tk.fast_info.last_price or tk.fast_info.previous_close
        except Exception:
            pass

        # 嘗試從 info 補充
        try:
            info = tk.info or {}
            raw_payload["pe"] = info.get("trailingPE") or info.get("forwardPE")
            raw_payload["eps"] = info.get("trailingEps")
            div_y = info.get("dividendYield")
            if div_y:
                raw_payload["yield_rate"] = div_y * 100 if div_y < 1 else div_y
        except Exception:
            pass
    except Exception as e:
        print(f"yfinance 基本面異常: {e}")

    # 2. 若 Yahoo 缺乏本益比或殖利率，自動由證交所官方快取補齊
    update_market_cache()
    twse_info = TWSE_FUNDAMENTAL_CACHE.get(clean_code)
    if twse_info:
        if raw_payload["pe"] is None and twse_info.get("pe"):
            raw_payload["pe"] = twse_info["pe"]
        if raw_payload["yield_rate"] is None and twse_info.get("yield_rate"):
            raw_payload["yield_rate"] = twse_info["yield_rate"]

    # 3. 若 EPS 缺失，依公式以最新股價動態推算: EPS = 股價 / PE
    if raw_payload["eps"] is None and raw_payload["pe"] and raw_payload["pe"] > 0:
        if latest_price and latest_price > 0:
            raw_payload["eps"] = round(latest_price / raw_payload["pe"], 2)

    cleaned = FundamentalData(**raw_payload)
    return cleaned.to_display_dict()


# ==========================================
# 6. 前端靜態檔案託管與首頁路由
# ==========================================
if os.path.exists("static"):
    app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/", response_class=FileResponse)
def serve_index():
    if os.path.exists("static/index.html"):
        return FileResponse("static/index.html")
    elif os.path.exists("index.html"):
        return FileResponse("index.html")
    return HTMLResponse("<h1>未找到 index.html 前端檔案，請確認檔案位置。</h1>")


# ==========================================
# 7. 自動適配本機開發與 Render 雲端環境
# ==========================================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)