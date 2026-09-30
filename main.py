# main.py
import os
import re
from typing import Dict, List, Optional
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
import pandas as pd
import yfinance as yf

app = FastAPI(
    title="AI 台股量化決策系統",
    description="提供全台股搜尋、TradingView 格式 K 線與量化買賣診斷訊號",
    version="1.0.0"
)

# 跨域支援
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 讀取全台股資產庫
try:
    STOCKS_DF = pd.read_csv("tw_stocks.csv", dtype={"symbol": str}).fillna("")
    print(f"後端成功載入 {len(STOCKS_DF)} 檔股票資料！")
except Exception as e:
    print(f"讀取 tw_stocks.csv 失敗: {e}")
    STOCKS_DF = pd.DataFrame()


def get_ticker_symbol(symbol: str) -> str:
    """自動判定加 .TW 或 .TWO"""
    symbol = str(symbol).strip()
    if not STOCKS_DF.empty:
        match = STOCKS_DF[STOCKS_DF['symbol'] == symbol]
        if not match.empty:
            market = match.iloc[0].get('market', '')
            return f"{symbol}.TWO" if market == "上櫃" else f"{symbol}.TW"
    return f"{symbol}.TW"


def calculate_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))


# ---------------- API 端點 ----------------

@app.get("/api/stocks/search", summary="全台股即時模糊搜尋")
def search_stocks(q: str = Query(..., min_length=1)):
    if STOCKS_DF.empty:
        return []
    query = q.strip().lower()
    matched = STOCKS_DF[
        STOCKS_DF['symbol'].str.lower().str.contains(query) | 
        STOCKS_DF['name'].str.lower().str.contains(query)
    ]
    return matched.head(10).to_dict(orient="records")


@app.get("/api/stocks/{symbol}/kline", summary="取得 TradingView 格式 K 線")
def get_kline(symbol: str, period: str = "6mo"):
    ticker = get_ticker_symbol(symbol)
    df = yf.download(ticker, period=period, interval="1d", progress=False)
    
    if df.empty:
        raise HTTPException(status_code=404, detail="找不到該股票資料")
    
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [col[0] for col in df.columns]

    kline_data = []
    volume_data = []

    for idx, row in df.iterrows():
        time_str = idx.strftime("%Y-%m-%d")
        o = round(float(row['Open']), 2)
        h = round(float(row['High']), 2)
        l = round(float(row['Low']), 2)
        c = round(float(row['Close']), 2)
        v = int(row['Volume'])

        kline_data.append({"time": time_str, "open": o, "high": h, "low": l, "close": c})
        volume_data.append({
            "time": time_str,
            "value": v,
            "color": "rgba(239, 83, 80, 0.5)" if c >= o else "rgba(38, 166, 154, 0.5)"
        })

    return {"symbol": symbol, "kline": kline_data, "volume": volume_data}


@app.get("/api/stocks/{symbol}/signal", summary="取得量化買賣診斷")
def get_signal(symbol: str):
    ticker = get_ticker_symbol(symbol)
    df = yf.download(ticker, period="6mo", interval="1d", progress=False)

    if df.empty or len(df) < 25:
        raise HTTPException(status_code=404, detail="歷史資料不足以運算指標")

    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [col[0] for col in df.columns]

    df['MA5'] = df['Close'].rolling(window=5).mean()
    df['MA20'] = df['Close'].rolling(window=20).mean()
    df['Vol_MA5'] = df['Volume'].rolling(window=5).mean()
    df['RSI'] = calculate_rsi(df['Close'], period=14)

    today = df.iloc[-1]
    yesterday = df.iloc[-2]

    close_price = round(float(today['Close']), 2)
    today_rsi = round(float(today['RSI']), 2) if pd.notnull(today['RSI']) else 50.0
    vol_ratio = round(float(today['Volume'] / today['Vol_MA5']), 2) if today['Vol_MA5'] > 0 else 1.0

    is_golden_cross = (yesterday['MA5'] <= yesterday['MA20']) and (today['MA5'] > today['MA20'])
    is_death_cross = (yesterday['MA5'] >= yesterday['MA20']) and (today['MA5'] < today['MA20'])
    is_volume_up = vol_ratio >= 1.3

    signal = "觀望 (HOLD)"
    reasons = []
    stop_loss = 0.0

    if is_golden_cross and is_volume_up:
        signal = "強烈建議買進 (STRONG BUY)"
        reasons.append("MA5 向上突破 MA20 (黃金交叉)")
        reasons.append(f"成交量放大為 5 日均量之 {vol_ratio} 倍 (帶量突破)")
        stop_loss = round(close_price * 0.96, 2)
    elif today['MA5'] > today['MA20']:
        if today_rsi > 80:
            signal = "警示：短線過熱 (OVERBOUGHT)"
            reasons.append(f"RSI 達 {today_rsi}，進入超買區，慎防回檔")
        else:
            signal = "多頭持有 (BULLISH HOLD)"
            reasons.append("股價站穩月線 (MA20) 之上，維持多方走勢")
            stop_loss = round(float(today['MA20']), 2)
    elif is_death_cross or today['MA5'] < today['MA20']:
        signal = "建議減碼/賣出 (SELL)"
        reasons.append("股價跌破月線或出現 MA5 死亡交叉，轉為弱勢")

    return {
        "symbol": symbol,
        "latest_close": close_price,
        "rsi": today_rsi,
        "volume_ratio": vol_ratio,
        "signal": signal,
        "reasons": reasons,
        "stop_loss_price": stop_loss
    }


# ---------------- 網頁前端託管 ----------------

# 掛載 static 目錄
if os.path.exists("static"):
    app.mount("/static", StaticFiles(directory="static"), name="static")

# 訪問根目錄 (首頁) 時，直接回傳黑底網頁
@app.get("/", include_in_schema=False)
def serve_index():
    index_file = os.path.join("static", "index.html")
    if os.path.exists(index_file):
        return FileResponse(index_file)
    return {"message": "找不到 static/index.html，請確認檔案位置"}