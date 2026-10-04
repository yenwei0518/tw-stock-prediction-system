import os
import time
import threading
from concurrent.futures import ThreadPoolExecutor
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
    description="整合雙視圖自選清單、三大法人籌碼、TradingView 與量化決策終端",
    version="5.5.0"
)

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
    {"symbol": "2454", "name": "聯發科", "market": "上市"},
    {"symbol": "2317", "name": "鴻海", "market": "上市"},
]

STOCK_DATABASE: Dict[str, dict] = {s["symbol"]: s for s in DEFAULT_STOCKS}
OTC_SYMBOLS: set = {"3293", "8069", "6488", "3131", "5483", "6547", "3529", "8299", "6274"}
FUNDAMENTAL_CACHE: Dict[str, dict] = {}
INSTITUTIONAL_CACHE: Dict[str, dict] = {}
BATCH_QUOTE_CACHE: Dict[str, tuple] = {}
LAST_FETCH_TIME = 0
CACHE_TTL = 3600 * 4


def parse_shares_to_lots(val: Any) -> int:
    """轉換官方股數為台灣張數 (1 張 = 1000 股)"""
    if val is None:
        return 0
    s = str(val).replace(",", "").replace("+", "").strip()
    try:
        shares = int(float(s))
        return int(round(shares / 1000))
    except Exception:
        return 0


# ==========================================
# 1. 全市場快取引擎 (TWSE + TPEx + 三大法人)
# ==========================================
def update_full_market_cache():
    global STOCK_DATABASE, OTC_SYMBOLS, FUNDAMENTAL_CACHE, INSTITUTIONAL_CACHE, LAST_FETCH_TIME
    now = time.time()
    if FUNDAMENTAL_CACHE and (now - LAST_FETCH_TIME) < CACHE_TTL:
        return

    headers = {"User-Agent": "Mozilla/5.0"}

    # A. 證交所上市股票與 ETF 清單
    try:
        resp = requests.get("https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL", headers=headers, timeout=6)
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
        resp = requests.get("https://openapi.twse.com.tw/v1/exchangeReport/BWIBBU_ALL", headers=headers, timeout=6)
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

    # C. 證交所三大法人買賣超 (T86 官方日報表)
    try:
        resp = requests.get("https://openapi.twse.com.tw/v1/fund/T86", headers=headers, timeout=6)
        if resp.status_code == 200:
            for item in resp.json():
                code = str(item.get("Code", "")).strip()
                if not code:
                    continue
                foreign = parse_shares_to_lots(item.get("ForeignInvestorsTotal") or item.get("ForeignInvestors") or 0)
                trust = parse_shares_to_lots(item.get("InvestmentTrustTotal") or item.get("InvestmentTrust") or 0)
                dealer = parse_shares_to_lots(item.get("DealerTotal") or item.get("Dealer") or 0)
                total = parse_shares_to_lots(item.get("Total") or (foreign * 1000 + trust * 1000 + dealer * 1000))
                
                INSTITUTIONAL_CACHE[code] = {
                    "foreign": foreign,
                    "trust": trust,
                    "dealer": dealer,
                    "total": total
                }
    except Exception as e:
        print(f"[TWSE 三大法人] 讀取跳過: {e}")

    # D. 櫃買中心上櫃清單與三大法人
    try:
        resp = requests.get("https://www.tpex.org.tw/openapi/v1/tpex_mainboard_peratio_analysis", headers=headers, timeout=6)
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

    try:
        resp = requests.get("https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_institutional_investors", headers=headers, timeout=6)
        if resp.status_code == 200:
            for item in resp.json():
                code = str(item.get("SecuritiesCompanyCode") or item.get("Code", "")).strip()
                if not code:
                    continue
                foreign = parse_shares_to_lots(item.get("ForeignInvestorsTotal") or 0)
                trust = parse_shares_to_lots(item.get("InvestmentTrustTotal") or 0)
                dealer = parse_shares_to_lots(item.get("DealerTotal") or 0)
                total = parse_shares_to_lots(item.get("Total") or (foreign * 1000 + trust * 1000 + dealer * 1000))
                INSTITUTIONAL_CACHE[code] = {
                    "foreign": foreign,
                    "trust": trust,
                    "dealer": dealer,
                    "total": total
                }
    except Exception:
        pass

    LAST_FETCH_TIME = now
    print(f"[市場快取完成] 股票庫: {len(STOCK_DATABASE)} 檔 | 三大法人庫: {len(INSTITUTIONAL_CACHE)} 檔")


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
# 2. Pydantic v2 模型與輔助計算
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
def fetch_single_quote(symbol: str) -> dict:
    clean_sym = symbol.strip().upper().split(".")[0]
    now = time.time()
    if clean_sym in BATCH_QUOTE_CACHE:
        cached_data, cached_time = BATCH_QUOTE_CACHE[clean_sym]
        if now - cached_time < 20:
            return cached_data

    stock_info = STOCK_DATABASE.get(clean_sym, {})
    name = stock_info.get("name", f"台股 {clean_sym}")
    market = stock_info.get("market", "台股")
    ticker = get_ticker_symbol(clean_sym)

    price, change, change_pct = 0.0, 0.0, 0.0
    volume_lots = 0
    vol_ratio = 1.0
    signal = "區間整理"

    try:
        df = yf.download(ticker, period="1mo", interval="1d", progress=False, auto_adjust=False)
        if df.empty:
            alt_ticker = ticker.replace(".TW", ".TWO") if ticker.endswith(".TW") else ticker.replace(".TWO", ".TW")
            df = yf.download(alt_ticker, period="1mo", interval="1d", progress=False, auto_adjust=False)

        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        if not df.empty and len(df) >= 3:
            df = df.dropna(subset=['Close'])
            latest = df.iloc[-1]
            prev = df.iloc[-2] if len(df) > 1 else latest

            price = round(float(latest['Close']), 2)
            prev_close = round(float(prev['Close']), 2)
            change = round(price - prev_close, 2)
            change_pct = round((change / prev_close) * 100, 2) if prev_close else 0.0

            vol_today = float(latest['Volume'])
            volume_lots = int(vol_today // 1000)
            vol_avg5 = float(df['Volume'].tail(5).mean()) if len(df) >= 5 else vol_today
            vol_ratio = round(vol_today / vol_avg5, 2) if vol_avg5 > 0 else 1.0

            ma5 = float(df['Close'].tail(5).mean())
            ma20 = float(df['Close'].tail(20).mean()) if len(df) >= 20 else ma5
            rsi_val = calculate_rsi(df['Close'], period=14)

            if price > ma20 and vol_ratio >= 1.3:
                signal = "帶量突破"
            elif price > ma20 and ma5 > ma20 and rsi_val > 50:
                signal = "多頭持有"
            elif price < ma20 and ma5 < ma20:
                signal = "空頭防守"
            else:
                signal = "區間整理"
        else:
            tk = yf.Ticker(ticker)
            price = round(float(tk.fast_info.last_price or 0.0), 2)
            prev_close = round(float(tk.fast_info.previous_close or 0.0), 2)
            if price and prev_close:
                change = round(price - prev_close, 2)
                change_pct = round((change / prev_close) * 100, 2)
    except Exception as e:
        print(f"[行情例外] {clean_sym}: {e}")

    if volume_lots >= 10000:
        vol_str = f"{round(volume_lots / 10000, 1)}萬張 ({vol_ratio}x)"
    elif volume_lots > 0:
        vol_str = f"{volume_lots:,}張 ({vol_ratio}x)"
    else:
        vol_str = "-- 張"

    result = {
        "symbol": clean_sym,
        "name": name,
        "market": market,
        "price": price,
        "change": change,
        "change_pct": change_pct,
        "volume_str": vol_str,
        "signal": signal
    }
    BATCH_QUOTE_CACHE[clean_sym] = (result, now)
    return result


@app.get("/api/stocks/batch-quotes")
def get_batch_quotes(symbols: str = Query(..., description="逗號分隔股票代號")):
    sym_list = [s.strip() for s in symbols.split(",") if s.strip()]
    if not sym_list:
        return []

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(fetch_single_quote, sym_list))

    return results


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
    clean_sym = symbol.split('.')[0].strip()
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

        open_price = round(float(latest_row['Open']), 2)
        high_price = round(float(latest_row['High']), 2)
        low_price = round(float(latest_row['Low']), 2)
        volume_today_shares = int(latest_row['Volume'])
        volume_lots = int(volume_today_shares // 1000)

        ma5 = float(df['Close'].tail(5).mean())
        ma20 = float(df['Close'].tail(20).mean()) if len(df) >= 20 else ma5
        rsi_val = calculate_rsi(df['Close'], period=14)

        vol_avg5 = float(df['Volume'].tail(5).mean()) if len(df) >= 5 else volume_today_shares
        vol_ratio = round(volume_today_shares / vol_avg5, 2) if vol_avg5 > 0 else 1.0

        recent_low_10 = float(df['Low'].tail(10).min())
        recent_high_20 = float(df['High'].tail(20).max())

        stop_loss = round(min(ma20, recent_low_10 * 0.99), 2)
        if stop_loss >= latest_close:
            stop_loss = round(latest_close * 0.96, 2)

        buy_low = round(max(stop_loss * 1.015, latest_close * 0.985), 2)
        buy_high = round(latest_close, 2)
        buy_range_str = f"{buy_low} ~ {buy_high}"

        risk = max(latest_close - stop_loss, latest_close * 0.02)
        target_price = round(max(recent_high_20, latest_close + risk * 1.6), 2)
        target_roi = round(((target_price - latest_close) / latest_close) * 100, 2)

        potential_reward = target_price - latest_close
        potential_risk = max(latest_close - stop_loss, 0.1)
        rr_ratio = round(potential_reward / potential_risk, 1)

        # 讀取三大法人籌碼
        inst_data = INSTITUTIONAL_CACHE.get(clean_sym, {
            "foreign": 0, "trust": 0, "dealer": 0, "total": 0
        })

        reasons = []
        signal = "區間整理 (NEUTRAL HOLD)"

        if latest_close >= ma20:
            reasons.append("股價站穩月線 (MA20) 之上，維持多方走勢")
        else:
            reasons.append("股價位於月線 (MA20) 之下，短線偏弱整理")

        # 籌碼面量化點評
        if inst_data["trust"] > 300:
            reasons.append(f"投信積極認養加碼 (買超 {inst_data['trust']:,} 張)，內資法人籌碼集中")
        elif inst_data["trust"] < -300:
            reasons.append(f"投信調節持股 (賣超 {abs(inst_data['trust']):,} 張)")

        if inst_data["foreign"] > 1500:
            reasons.append(f"外資積極回補買超 ({inst_data['foreign']:,} 張)，買盤動能強勁")
        elif inst_data["foreign"] < -1500:
            reasons.append(f"外資調節賣壓 (賣超 {abs(inst_data['foreign']):,} 張)，提防壓盤")

        if rsi_val >= 70:
            reasons.append("RSI 超買警戒，短線不追高，宜拉回買進")
        elif rsi_val <= 30:
            reasons.append("RSI 進入超賣低檔，醞釀跌深反彈")
        else:
            reasons.append(f"RSI 為 {rsi_val}，動能處於中性健康區間")

        if vol_ratio >= 1.3:
            reasons.append(f"成交量放大 (量能比 {vol_ratio}x)，交投熱絡")

        # 訊號判定
        if latest_close > ma20 and vol_ratio >= 1.3 and (inst_data["total"] >= 0):
            signal = "帶量突破 (BUY)"
        elif latest_close > ma20 and ma5 > ma20 and rsi_val > 50:
            signal = "多頭持有 (BULLISH HOLD)"
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
            "buy_range": buy_range_str,
            "target_price": target_price,
            "target_roi": f"+{target_roi}%" if target_roi > 0 else f"{target_roi}%",
            "stop_loss_price": stop_loss,
            "risk_reward": f"1 : {rr_ratio}",
            "institutional": inst_data,
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