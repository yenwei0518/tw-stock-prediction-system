import os
import time
import threading
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
    description="整合全台股上市櫃資料、TradingView、行情四價與具體量化買賣點位",
    version="4.0.0"
)

# 跨域連線配置 (CORS)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DEFAULT_STOCKS = [
    {"symbol": "2330", "name": "台積電", "market": "上市"},
    {"symbol": "0050", "name": "元大台灣50", "market": "ETF"},
    {"symbol": "006208", "name": "富邦科技台50", "market": "ETF"},
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
    {"symbol": "3293", "name": "鈊象", "market": "上櫃"},
    {"symbol": "8069", "name": "元太", "market": "上櫃"},
    {"symbol": "6488", "name": "環球晶", "market": "上櫃"},
    {"symbol": "3131", "name": "弘塑", "market": "上櫃"},
    {"symbol": "0056", "name": "元大高股息", "market": "ETF"},
    {"symbol": "00878", "name": "國泰永續高股息", "market": "ETF"},
    {"symbol": "00919", "name": "群益台灣精選高息", "market": "ETF"},
    {"symbol": "00929", "name": "復華台灣科技優息", "market": "ETF"},
    {"symbol": "00940", "name": "元大台灣價值高息", "market": "ETF"},
    {"symbol": "00679B", "name": "元大美債20年", "market": "ETF"},
]

STOCK_DATABASE: Dict[str, dict] = {s["symbol"]: s for s in DEFAULT_STOCKS}
OTC_SYMBOLS: set = {"3293", "8069", "6488", "3131", "5483", "6547", "3529", "8299", "6274"}
FUNDAMENTAL_CACHE: Dict[str, dict] = {}
LAST_FETCH_TIME = 0
CACHE_TTL = 3600 * 4


# ==========================================
# 1. 全市場快取引擎 (TWSE + TPEx)
# ==========================================
def update_full_market_cache():
    global STOCK_DATABASE, OTC_SYMBOLS, FUNDAMENTAL_CACHE, LAST_FETCH_TIME
    now = time.time()
    if FUNDAMENTAL_CACHE and (now - LAST_FETCH_TIME) < CACHE_TTL:
        return

    headers = {"User-Agent": "Mozilla/5.0"}

    # A. 證交所上市股票與 ETF
    try:
        twse_stocks_url = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
        resp = requests.get(twse_stocks_url, headers=headers, timeout=6)
        if resp.status_code == 200:
            for item in resp.json():
                code = str(item.get("Code", "")).strip()
                name = str(item.get("Name", "")).strip()
                if code and name:
                    market = "ETF" if code.startswith("00") else "上市"
                    STOCK_DATABASE[code] = {"symbol": code, "name": name, "market": market}
    except Exception:
        pass

    # B. 證交所本益比與殖利率
    try:
        twse_pe_url = "https://openapi.twse.com.tw/v1/exchangeReport/BWIBBU_ALL"
        resp = requests.get(twse_pe_url, headers=headers, timeout=6)
        if resp.status_code == 200:
            for item in resp.json():
                code = str(item.get("Code", "")).strip()
                name = str(item.get("Name", "")).strip()
                if not code:
                    continue
                if name and code not in STOCK_DATABASE:
                    STOCK_DATABASE[code] = {"symbol": code, "name": name, "market": "上市"}

                pe_str = str(item.get("PEratio", "")).replace(",", "").strip()
                yield_str = str(item.get("DividendYield", "")).replace(",", "").replace("%", "").strip()

                pe_val = float(pe_str) if pe_str and pe_str not in ("-", "--", "N/A") else None
                yield_val = float(yield_str) if yield_str and yield_str not in ("-", "--", "N/A") else None

                FUNDAMENTAL_CACHE[code] = {"pe": pe_val, "yield_rate": yield_val}
    except Exception:
        pass

    # C. 櫃買中心上櫃公司
    try:
        tpex_pe_url = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_peratio_analysis"
        resp = requests.get(tpex_pe_url, headers=headers, timeout=6)
        if resp.status_code == 200:
            for item in resp.json():
                code = str(item.get("SecuritiesCompanyCode") or item.get("Code", "")).strip()
                name = str(item.get("CompanyName") or item.get("Name", "")).strip()
                if not code:
                    continue
                if name:
                    market = "ETF" if code.startswith("00") else "上櫃"
                    STOCK_DATABASE[code] = {"symbol": code, "name": name, "market": market}
                OTC_SYMBOLS.add(code)

                pe_str = str(item.get("PriceEarningRatio") or item.get("PEratio", "")).replace(",", "").strip()
                yield_str = str(item.get("DividendYield", "")).replace(",", "").replace("%", "").strip()

                pe_val = float(pe_str) if pe_str and pe_str not in ("-", "--", "N/A") else None
                yield_val = float(yield_str) if yield_str and yield_str not in ("-", "--", "N/A") else None

                FUNDAMENTAL_CACHE[code] = {"pe": pe_val, "yield_rate": yield_val}
    except Exception:
        pass

    LAST_FETCH_TIME = now


@app.on_event("startup")
def startup_event():
    threading.Thread(target=update_full_market_cache, daemon=True).start()


def get_ticker_symbol(symbol: str) -> str:
    sym = symbol.strip().upper()
    if sym.endswith(".TW") or sym.endswith(".TWO"):
        return sym
    clean = sym.split(".")[0]
    stock_info = STOCK_DATABASE.get(clean)
    if (stock_info and stock_info.get("market") == "上櫃") or clean in OTC_SYMBOLS:
        return f"{clean}.TWO"
    return f"{clean}.TW"


# ==========================================
# 2. Pydantic v2 基本面模型
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
# 3. PWA 專屬路由
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
# 4. 核心 API 端點
# ==========================================
@app.get("/api/stocks/search")
def search_stocks(q: str = Query(..., min_length=1)):
    if len(STOCK_DATABASE) <= len(DEFAULT_STOCKS):
        update_full_market_cache()

    query = q.strip().lower()
    exact_matches, prefix_matches, fuzzy_matches = [], [], []

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
    if query.isdigit() and len(query) >= 4 and not any(r["symbol"] == query for r in results):
        results.insert(0, {"symbol": query, "name": f"台股 {query}", "market": "台股"})

    return results[:10]


@app.get("/api/stocks/{symbol}/kline")
def get_kline(symbol: str):
    try:
        ticker = get_ticker_symbol(symbol)
        df = yf.download(ticker, period="6mo", interval="1d", progress=False, auto_adjust=False)

        if df.empty:
            alt_ticker = ticker.replace(".TW", ".TWO") if ticker.endswith(".TW") else ticker.replace(".TWO", ".TW")
            df = yf.download(alt_ticker, period="6mo", interval="1d", progress=False, auto_adjust=False)

        if df.empty:
            raise HTTPException(status_code=404, detail=f"查無 {symbol} 走勢資料")

        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        df = df.dropna(subset=['Close'])
        df['MA5'] = df['Close'].rolling(window=5).mean()
        df['MA20'] = df['Close'].rolling(window=20).mean()

        kline_data, volume_data, ma5_data, ma20_data = [], [], [], []

        for idx, row in df.iterrows():
            date_str = idx.strftime("%Y-%m-%d")
            o, h, l, c = round(float(row['Open']), 2), round(float(row['High']), 2), round(float(row['Low']), 2), round(float(row['Close']), 2)
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
    """回傳第一層（行情四價+張數）與第二層（量化具體買賣點位）"""
    try:
        ticker = get_ticker_symbol(symbol)
        df = yf.download(ticker, period="3mo", interval="1d", progress=False, auto_adjust=False)

        if df.empty:
            alt_ticker = ticker.replace(".TW", ".TWO") if ticker.endswith(".TW") else ticker.replace(".TWO", ".TW")
            df = yf.download(alt_ticker, period="3mo", interval="1d", progress=False, auto_adjust=False)

        if df.empty or len(df) < 5:
            raise HTTPException(status_code=404, detail="資料天數不足以計算指標")

        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        df = df.dropna(subset=['Close'])
        latest_row = df.iloc[-1]
        prev_row = df.iloc[-2] if len(df) > 1 else latest_row

        latest_close = round(float(latest_row['Close']), 2)
        prev_close = round(float(prev_row['Close']), 2)
        change = round(latest_close - prev_close, 2)
        change_pct = round((change / prev_close) * 100, 2) if prev_close else 0.0

        # 第一層：行情四價與成交量（換算為張）
        open_price = round(float(latest_row['Open']), 2)
        high_price = round(float(latest_row['High']), 2)
        low_price = round(float(latest_row['Low']), 2)
        volume_today_shares = int(latest_row['Volume'])
        volume_lots = int(volume_today_shares // 1000)  # 1 張 = 1000 股

        # 技術均線與 RSI
        ma5 = float(df['Close'].tail(5).mean())
        ma20 = float(df['Close'].tail(20).mean()) if len(df) >= 20 else ma5
        rsi_val = calculate_rsi(df['Close'], period=14)

        vol_avg5 = float(df['Volume'].tail(5).mean()) if len(df) >= 5 else volume_today_shares
        vol_ratio = round(volume_today_shares / vol_avg5, 2) if vol_avg5 > 0 else 1.0

        # 第二層：量化實戰具體點位運算
        recent_low_10 = float(df['Low'].tail(10).min())
        recent_high_20 = float(df['High'].tail(20).max())

        # 1. 防守停損價 (破月線或破近 10 日前低)
        stop_loss = round(min(ma20, recent_low_10 * 0.99), 2)
        if stop_loss >= latest_close:
            stop_loss = round(latest_close * 0.96, 2)

        # 2. 建議買入區間 (回測支撐甜美價)
        buy_low = round(max(stop_loss * 1.015, latest_close * 0.985), 2)
        buy_high = round(latest_close, 2)
        buy_range_str = f"{buy_low} ~ {buy_high}"

        # 3. 第一目標停利價 (考量前高壓力與波段盈虧比)
        risk = max(latest_close - stop_loss, latest_close * 0.02)
        target_price = round(max(recent_high_20, latest_close + risk * 1.6), 2)
        target_roi = round(((target_price - latest_close) / latest_close) * 100, 2)

        # 4. 策略盈虧比 (Risk/Reward)
        potential_reward = target_price - latest_close
        potential_risk = max(latest_close - stop_loss, 0.1)
        rr_ratio = round(potential_reward / potential_risk, 1)

        # 訊號與觀點
        reasons = []
        signal = "區間整理 (NEUTRAL HOLD)"

        if latest_close >= ma20:
            reasons.append("股價站穩月線 (MA20) 之上，維持多方走勢")
        else:
            reasons.append("股價位於月線 (MA20) 之下，短線偏弱整理")

        if rsi_val >= 70:
            reasons.append("RSI 超買警戒，短線不追高，宜拉回買進")
        elif rsi_val <= 30:
            reasons.append("RSI 進入超賣低檔，醞釀跌深反彈")
        else:
            reasons.append(f"RSI 為 {rsi_val}，動能處於中性健康區間")

        if vol_ratio >= 1.3:
            reasons.append(f"成交量放大 (量能比 {vol_ratio}x)，交投熱絡")

        if latest_close > ma20 and ma5 > ma20 and rsi_val > 50:
            signal = "多頭持有 (BULLISH HOLD)"
        elif latest_close > ma20 and vol_ratio >= 1.3:
            signal = "帶量突破 (BUY)"
        elif latest_close < ma20 and ma5 < ma20:
            signal = "空頭防守 (BEARISH AVOID)"

        return {
            "symbol": symbol,
            "latest_close": latest_close,
            "prev_close": prev_close,
            "change": change,
            "change_pct": change_pct,
            "open": open_price,
            "high": high_price,
            "low": low_price,
            "volume_lots": volume_lots,
            "volume_ratio": vol_ratio,
            "rsi": rsi_val,
            # 第二層量化點位
            "buy_range": buy_range_str,
            "target_price": target_price,
            "target_roi": f"+{target_roi}%" if target_roi > 0 else f"{target_roi}%",
            "stop_loss_price": stop_loss,
            "risk_reward": f"1 : {rr_ratio}",
            "signal": signal,
            "reasons": reasons
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"技術指標計算失敗: {str(e)}")


@app.get("/api/stocks/{symbol}/fundamental")
def get_fundamental(symbol: str):
    clean_code = symbol.split('.')[0].strip()
    ticker = get_ticker_symbol(symbol)
    
    raw_payload = {"pe": None, "eps": None, "yield_rate": None, "market_cap": None}
    latest_price = None

    try:
        tk = yf.Ticker(ticker)
        try:
            raw_payload["market_cap"] = tk.fast_info.market_cap
            latest_price = tk.fast_info.last_price or tk.fast_info.previous_close
        except Exception:
            pass

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

    market_info = FUNDAMENTAL_CACHE.get(clean_code)
    if market_info:
        if raw_payload["pe"] is None and market_info.get("pe"):
            raw_payload["pe"] = market_info["pe"]
        if raw_payload["yield_rate"] is None and market_info.get("yield_rate"):
            raw_payload["yield_rate"] = market_info["yield_rate"]

    if raw_payload["eps"] is None and raw_payload["pe"] and raw_payload["pe"] > 0:
        if latest_price and latest_price > 0:
            raw_payload["eps"] = round(latest_price / raw_payload["pe"], 2)

    cleaned = FundamentalData(**raw_payload)
    return cleaned.to_display_dict()


# ==========================================
# 5. 前端靜態檔案託管
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


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)