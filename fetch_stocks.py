# fetch_stocks.py
import re
import pandas as pd
import requests

def get_stocks_from_gov():
    print("正在從政府資料開放平台取得上市與上櫃股票清單...")
    
    # 臺灣證券交易所 (上市公司清單 OpenData)
    tse_url = "https://openapi.twse.com.tw/v1/exchangeReport/BWIBBU_ALL"
    # 證券櫃檯買賣中心 (上櫃公司清單 OpenData)
    otc_url = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_peratio_analysis"
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    
    stocks = []
    
    # 1. 抓取上市股票
    try:
        res_tse = requests.get(tse_url, headers=headers, timeout=10)
        if res_tse.status_code == 200:
            data_tse = res_tse.json()
            for item in data_tse:
                code = item.get("Code", "").strip()
                name = item.get("Name", "").strip()
                # 篩選 4 碼純數字的普通股
                if re.match(r"^\d{4}$", code):
                    stocks.append({
                        "symbol": code,
                        "name": name,
                        "market": "上市",
                        "pe_ratio": item.get("PEratio", "N/A"),     # 本益比
                        "dividend_yield": item.get("DividendYield", "N/A") # 殖利率
                    })
            print(f"成功取得上市公司資料！")
    except Exception as e:
        print(f"上市資料獲取失敗: {e}")

    # 2. 抓取上櫃股票
    try:
        res_otc = requests.get(otc_url, headers=headers, timeout=10)
        if res_otc.status_code == 200:
            data_otc = res_otc.json()
            for item in data_otc:
                code = item.get("SecuritiesCompanyCode", "").strip()
                name = item.get("CompanyName", "").strip()
                # 篩選 4 碼純數字的普通股
                if re.match(r"^\d{4}$", code):
                    stocks.append({
                        "symbol": code,
                        "name": name,
                        "market": "上櫃",
                        "pe_ratio": item.get("PriceEarningRatio", "N/A"),
                        "dividend_yield": item.get("YieldRatio", "N/A")
                    })
            print(f"成功取得上櫃公司資料！")
    except Exception as e:
        print(f"上櫃資料獲取失敗: {e}")

    # 3. 補充常用主流 ETF (0050, 0056, 00878 等)
    core_etfs = [
        {"symbol": "0050", "name": "元大台灣50", "market": "上市", "pe_ratio": "N/A", "dividend_yield": "N/A"},
        {"symbol": "0056", "name": "元大高股息", "market": "上市", "pe_ratio": "N/A", "dividend_yield": "N/A"},
        {"symbol": "00878", "name": "國泰永續高股息", "market": "上市", "pe_ratio": "N/A", "dividend_yield": "N/A"},
        {"symbol": "00919", "name": "群益台灣精選高息", "market": "上市", "pe_ratio": "N/A", "dividend_yield": "N/A"},
        {"symbol": "00929", "name": "復華台灣科技優息", "market": "上市", "pe_ratio": "N/A", "dividend_yield": "N/A"},
        {"symbol": "006208", "name": "富邦台50", "market": "上市", "pe_ratio": "N/A", "dividend_yield": "N/A"}
    ]
    stocks.extend(core_etfs)

    # 4. 轉成 DataFrame 並存檔
    df = pd.DataFrame(stocks)
    if not df.empty:
        # 去重並依代碼排序
        df = df.drop_duplicates(subset=["symbol"]).sort_values(by="symbol").reset_index(drop=True)
        df.to_csv("tw_stocks.csv", index=False, encoding="utf-8-sig")
        
        print("=" * 45)
        print(f"抓取成功！共儲存 {len(df)} 檔股票/ETF 到 tw_stocks.csv")
        print("=" * 45)
        print("前 10 筆資料預覽：")
        print(df.head(10))
    else:
        print("依然沒有取得資料，請檢查網路連線。")

if __name__ == "__main__":
    get_stocks_from_gov()