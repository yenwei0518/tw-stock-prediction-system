# analyzer.py
import pandas as pd
import yfinance as yf

# 讀取剛剛抓好的全台股清單，用來自動判斷市場（上市用 .TW，上櫃用 .TWO）
try:
    STOCKS_DB = pd.read_csv("tw_stocks.csv", dtype={"symbol": str})
except FileNotFoundError:
    STOCKS_DB = pd.DataFrame()

def get_ticker_symbol(symbol: str) -> str:
    """自動判定該加 .TW (上市) 還是 .TWO (上櫃)"""
    symbol = str(symbol).strip()
    if not STOCKS_DB.empty:
        match = STOCKS_DB[STOCKS_DB['symbol'] == symbol]
        if not match.empty:
            market = match.iloc[0]['market']
            return f"{symbol}.TWO" if market == "上櫃" else f"{symbol}.TW"
    # 若清單找不到，預設為上市
    return f"{symbol}.TW"

def calculate_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """計算 RSI 相對強弱指標 (14日)"""
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))
    return rsi

def analyze_stock(symbol: str):
    ticker = get_ticker_symbol(symbol)
    print(f"\n正在分析標的：{symbol} (Yahoo 查詢代碼: {ticker})...")
    
    # 抓取最近 6 個月的日 K 線
    df = yf.download(ticker, period="6mo", interval="1d", progress=False)
    
    if df.empty or len(df) < 25:
        print(f"無法取得足夠的歷史數據，請確認代號是否正確。")
        return None

    # 多重索引 (MultiIndex) 扁平化相容處理
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [col[0] for col in df.columns]

    # 1. 特徵計算
    df['MA5'] = df['Close'].rolling(window=5).mean()    # 5日週線 (短線動能)
    df['MA20'] = df['Close'].rolling(window=20).mean()  # 20日月線 (生命線)
    df['Vol_MA5'] = df['Volume'].rolling(window=5).mean()
    df['RSI'] = calculate_rsi(df['Close'], period=14)

    # 2. 取得今日與昨日數據
    today = df.iloc[-1]
    yesterday = df.iloc[-2]
    
    close_price = round(float(today['Close']), 2)
    today_rsi = round(float(today['RSI']), 2) if pd.notnull(today['RSI']) else 50.0
    vol_ratio = round(float(today['Volume'] / today['Vol_MA5']), 2) if today['Vol_MA5'] > 0 else 1.0

    # 3. 量化進出場邏輯判定
    # 均線黃金交叉：昨天短均在長均下，今天短均衝過長均
    is_golden_cross = (yesterday['MA5'] <= yesterday['MA20']) and (today['MA5'] > today['MA20'])
    # 死亡交叉：昨天短均在長均上，今天短均跌破長均
    is_death_cross = (yesterday['MA5'] >= yesterday['MA20']) and (today['MA5'] < today['MA20'])
    
    # 是否帶量突破（今日量大於 5 日均量的 1.3 倍）
    is_volume_up = vol_ratio >= 1.3

    # 判定訊號與停損點
    signal = "觀望中 (HOLD)"
    reason = []
    stop_loss = 0.0

    if is_golden_cross and is_volume_up:
        signal = "🚀 強烈建議買進 (STRONG BUY)"
        reason.append("MA5 向上黃金交叉突破 MA20")
        reason.append(f"成交量放大為 5 日均量的 {vol_ratio} 倍 (帶量突破)")
        stop_loss = round(close_price * 0.96, 2)  # 建議 4% 硬停損
    elif today['MA5'] > today['MA20']:
        if today_rsi > 80:
            signal = "⚠️ 警示：短線過熱 (OVERBOUGHT)"
            reason.append(f"多頭排列，但 RSI 達到 {today_rsi}，已進入極度超買區，慎防拉回")
        else:
            signal = "多頭持有 (BULLISH HOLD)"
            reason.append("股價位於 MA20 之上，趨勢偏多")
            stop_loss = round(float(today['MA20']), 2)  # 以月線作為移動停損點
    elif is_death_cross or today['MA5'] < today['MA20']:
        signal = "🔻 建議減碼/賣出 (SELL)"
        reason.append("股價跌破月線或出現 MA5 死亡交叉，動能轉弱")

    # 4. 輸出分析結果
    print("-" * 45)
    print(f"【{symbol} 量化診斷報告】")
    print(f"最新收盤價: {close_price} 元")
    print(f"RSI 強弱指標: {today_rsi} (30以下超跌，70以上超買)")
    print(f"今日成交量比: {vol_ratio} 倍 (相對 5 日均量)")
    print(f"系統決策訊號: {signal}")
    print(f"觸發原因: {'；'.join(reason) if reason else '無明顯突破或轉折訊號'}")
    if stop_loss > 0:
        print(f"建議防守停損價: {stop_loss} 元")
    print("-" * 45)

if __name__ == "__main__":
    # 測試幾檔具代表性的標的：台積電(2330)、元大台灣50(0050)
    analyze_stock("2330")
    analyze_stock("0050")