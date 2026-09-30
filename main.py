# main.py
import os
import traceback
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
import pandas as pd
import yfinance as yf

app = FastAPI(title="AI 台股量化決策系統")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

try:
    STOCKS_DF = pd.read_csv("tw_stocks.csv", dtype={"symbol": str}).fillna("")
    print(f"✅ 成功載入 {len(STOCKS_DF)} 檔股票資料")
except Exception as e:
    print(f"⚠ 讀取 tw_stocks.csv 失敗: {e}")
    STOCKS_DF = pd.DataFrame()

def get_ticker_symbol(symbol: str) -> str:
    symbol = str(symbol).strip()
    if not STOCKS_DF.empty:
        match = STOCKS_DF[STOCKS_DF['symbol'] == symbol]
        if not match.empty:
            market = match.iloc[0].get('market', '')
            return f"{symbol}.TWO" if market == "上櫃" else f"{symbol}.TW"
    return f"{symbol}.TW"

def clean_yfinance_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    """展平 MultiIndex 並統一欄位名稱，避免 500 錯誤"""
    if df.empty:
        return df
    if isinstance(df.columns, pd.MultiIndex):
        # 取第一層欄位名 (Open, High, Low, Close, Volume)
        df.columns = [col[0] for col in df.columns]
    df = df.dropna(subset=['Open', 'High', 'Low', 'Close'])
    return df.sort_index()

def calculate_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

@app.get("/api/stocks/search")
def search_stocks(q: str = Query(..., min_length=1)):
    if STOCKS_DF.empty:
        return []
    query = q.strip().lower()
    matched = STOCKS_DF[
        STOCKS_DF['symbol'].str.lower().str.contains(query) | 
        STOCKS_DF['name'].str.lower().str.contains(query)
    ]
    return matched.head(10).to_dict(orient="records")

@app.get("/api/stocks/{symbol}/kline")
def get_kline(symbol: str, period: str = "6mo"):
    try:
        ticker = get_ticker_symbol(symbol)
        df = yf.download(ticker, period=period, interval="1d", progress=False)
        df = clean_yfinance_dataframe(df)

        if df.empty:
            raise HTTPException(status_code=404, detail="無歷史資料")

        # 計算 MA5 與 MA20
        close_s = df['Close']
        if isinstance(close_s, pd.DataFrame):
            close_s = close_s.iloc[:, 0]
            
        df['MA5'] = close_s.rolling(window=5).mean()
        df['MA20'] = close_s.rolling(window=20).mean()

        kline_data = []
        volume_data = []
        ma5_data = []
        ma20_data = []

        for idx, row in df.iterrows():
            time_str = idx.strftime("%Y-%m-%d")
            o = round(float(pd.Series(row['Open']).iloc[0] if isinstance(row['Open'], pd.Series) else row['Open']), 2)
            h = round(float(pd.Series(row['High']).iloc[0] if isinstance(row['High'], pd.Series) else row['High']), 2)
            l = round(float(pd.Series(row['Low']).iloc[0] if isinstance(row['Low'], pd.Series) else row['Low']), 2)
            c = round(float(pd.Series(row['Close']).iloc[0] if isinstance(row['Close'], pd.Series) else row['Close']), 2)
            v = int(pd.Series(row['Volume']).iloc[0] if isinstance(row['Volume'], pd.Series) else row['Volume'])

            kline_data.append({"time": time_str, "open": o, "high": h, "low": l, "close": c})
            volume_data.append({
                "time": time_str,
                "value": v,
                "color": "rgba(239, 83, 80, 0.5)" if c >= o else "rgba(38, 166, 154, 0.5)"
            })

            # 若有 MA 數值則加入，忽略開頭的 NaN
            m5 = row['MA5'] if not isinstance(row['MA5'], pd.Series) else row['MA5'].iloc[0]
            m20 = row['MA20'] if not isinstance(row['MA20'], pd.Series) else row['MA20'].iloc[0]

            if pd.notnull(m5):
                ma5_data.append({"time": time_str, "value": round(float(m5), 2)})
            if pd.notnull(m20):
                ma20_data.append({"time": time_str, "value": round(float(m20), 2)})

        return {
            "symbol": symbol,
            "kline": kline_data,
            "volume": volume_data,
            "ma5": ma5_data,
            "ma20": ma20_data
        }
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"K 線運算錯誤: {str(e)}")

@app.get("/api/stocks/{symbol}/signal")
def get_signal(symbol: str):
    try:
        ticker = get_ticker_symbol(symbol)
        df = yf.download(ticker, period="6mo", interval="1d", progress=False)
        df = clean_yfinance_dataframe(df)

        if len(df) < 20:
            raise HTTPException(status_code=404, detail="交易日歷史資料不足")

        # 確保為一維 Series
        close_series = df['Close']
        if isinstance(close_series, pd.DataFrame):
            close_series = close_series.iloc[:, 0]

        vol_series = df['Volume']
        if isinstance(vol_series, pd.DataFrame):
            vol_series = vol_series.iloc[:, 0]

        ma5 = close_series.rolling(window=5).mean()
        ma20 = close_series.rolling(window=20).mean()
        vol_ma5 = vol_series.rolling(window=5).mean()
        rsi_series = calculate_rsi(close_series, period=14)

        close_price = round(float(close_series.iloc[-1]), 2)
        today_rsi = round(float(rsi_series.iloc[-1]), 2) if pd.notnull(rsi_series.iloc[-1]) else 50.0
        today_vol = float(vol_series.iloc[-1])
        recent_vol_ma5 = float(vol_ma5.iloc[-1]) if vol_ma5.iloc[-1] > 0 else 1.0
        vol_ratio = round(today_vol / recent_vol_ma5, 2)

        is_golden_cross = (ma5.iloc[-2] <= ma20.iloc[-2]) and (ma5.iloc[-1] > ma20.iloc[-1])
        is_death_cross = (ma5.iloc[-2] >= ma20.iloc[-2]) and (ma5.iloc[-1] < ma20.iloc[-1])
        is_volume_up = vol_ratio >= 1.3

        signal = "觀望 (HOLD)"
        reasons = []
        stop_loss = 0.0

        if is_golden_cross and is_volume_up:
            signal = "強烈建議買進 (STRONG BUY)"
            reasons.append("MA5 向上突破 MA20 (黃金交叉)")
            reasons.append(f"成交量放大為 5 日均量之 {vol_ratio} 倍 (帶量突破)")
            stop_loss = round(close_price * 0.96, 2)
        elif ma5.iloc[-1] > ma20.iloc[-1]:
            if today_rsi > 80:
                signal = "警示：短線過熱 (OVERBOUGHT)"
                reasons.append(f"RSI 達 {today_rsi}，進入超買區，慎防回檔")
            else:
                signal = "多頭持有 (BULLISH HOLD)"
                reasons.append("股價站穩月線 (MA20) 之上，維持多方走勢")
                stop_loss = round(float(ma20.iloc[-1]), 2)
        elif is_death_cross or ma5.iloc[-1] < ma20.iloc[-1]:
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
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"訊號運算錯誤: {str(e)}")

@app.get("/", include_in_schema=False)
def serve_index():
    for path in ["static/index.html", "index.html"]:
        if os.path.exists(path):
            return FileResponse(path)
    return {"message": "找不到 index.html"}