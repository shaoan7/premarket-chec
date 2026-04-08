"""
開盤前自動化檢查：匯率 + 台指期夜盤 + 分點籌碼
GitHub Actions 排程執行，結果發到 Telegram。

安裝：
    pip install yfinance requests beautifulsoup4

排程（GitHub Actions - daily.yml）：
    台灣 8:35 = UTC 0:35 → cron: '35 0 * * 1-5'
"""

import datetime as dt
import json
import re
import os
import requests
import yfinance as yf
from bs4 import BeautifulSoup

# ========== 設定區 ==========
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
FX_THRESHOLD = 0.10
SPREAD_THRESHOLD = 100

# 重點分點（券商代號, 顯示名稱, 風格）
# 券商代號查詢：https://bsr.twse.com.tw/bshtm/bsMenu.aspx → 券商代號查詢
TARGET_BRANCHES = [
    ("7001", "兆豐-嘉義", "長線主力"),
    ("9268", "凱基-台北", "短線高手"),
    ("9A92", "永豐金-萬盛", "波段王"),
]
TOP_N = 5  # 每個分點顯示前幾大買超/賣超
# ============================

MONTH_CODES = {1: "F", 2: "G", 3: "H", 4: "J", 5: "K", 6: "M",
               7: "N", 8: "Q", 9: "U", 10: "V", 11: "X", 12: "Z"}


# ── 匯率 ──

def get_usdtwd():
    t = yf.Ticker("TWD=X")
    hist = t.history(period="5d", interval="1d")
    if len(hist) < 2:
        raise RuntimeError("匯率資料不足")
    prev_close = float(hist["Close"].iloc[-2])
    current = float(hist["Close"].iloc[-1])
    return current, prev_close, current - prev_close


def judge_fx(diff):
    if diff <= -FX_THRESHOLD:
        return f"台幣明顯升值（{diff:+.3f}）→ 外資錢進來，權值股有機會"
    if diff >= FX_THRESHOLD:
        return f"台幣明顯貶值（{diff:+.3f}）→ 外資匯出，今天別衝"
    return f"匯率平盤（{diff:+.3f}）→ 回到個股判斷"


# ── 台指期（Yahoo only）──

def get_tx_symbol():
    today = dt.date.today()
    return f"WTX{MONTH_CODES[today.month]}{today.year % 10}"


def _find_price_in_json(obj, keys=("regularMarketPrice", "last", "price", "lastPrice"), depth=0):
    if depth > 25 or obj is None:
        return None
    if isinstance(obj, dict):
        for k in keys:
            if k in obj:
                v = obj[k]
                if isinstance(v, dict) and "raw" in v:
                    return float(v["raw"])
                if isinstance(v, (int, float)) and v > 0:
                    return float(v)
                if isinstance(v, str):
                    try:
                        return float(v.replace(",", ""))
                    except ValueError:
                        pass
        for v in obj.values():
            result = _find_price_in_json(v, keys, depth + 1)
            if result is not None:
                return result
    elif isinstance(obj, list):
        for item in obj:
            result = _find_price_in_json(item, keys, depth + 1)
            if result is not None:
                return result
    return None


def get_night_from_yahoo(symbol):
    url = f"https://tw.stock.yahoo.com/quote/{symbol}"
    headers = {
        "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
        "Accept-Language": "zh-TW,zh;q=0.9",
    }
    r = requests.get(url, headers=headers, timeout=15)
    r.raise_for_status()
    html = r.text

    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.+?)</script>', html, re.DOTALL)
    if m:
        try:
            data = json.loads(m.group(1))
            price = _find_price_in_json(data)
            if price:
                return {"symbol": symbol, "price": price, "source": "Yahoo TW (JSON)"}
        except Exception:
            pass

    for pattern in [
        r'Fz\(32px\)[^>]*>([\d,]+\.\d+)<',
        r'data-reactid="[^"]*"[^>]*>([\d,]+\.\d+)</span>',
        r'"regularMarketPrice"[^}]*?"raw":([\d.]+)',
    ]:
        m = re.search(pattern, html)
        if m:
            try:
                price = float(m.group(1).replace(",", ""))
                return {"symbol": symbol, "price": price, "source": "Yahoo TW (HTML)"}
            except ValueError:
                continue
    return None


def judge_futures(spread):
    if spread is None:
        return "台指期資料抓取失敗，請手動確認"
    month = dt.date.today().month
    warn = "（⚠️ 除息旺季，記得扣除息點數再判斷）" if month in (6, 7, 8) else ""
    if spread >= SPREAD_THRESHOLD:
        return f"正價差 {spread:+.0f} 點 → 開高機率高{warn}"
    if spread <= -SPREAD_THRESHOLD:
        return f"逆價差 {spread:+.0f} 點 → 開低機率高{warn}"
    return f"價差 {spread:+.0f} 點 → 無明顯方向{warn}"


# ── 分點籌碼（證交所，以券商代號查詢）──

def get_branch_trades(broker_code):
    """從證交所查詢單一券商分點的當日個股進出。
    回傳 list of dict: [{stock, name, buy, sell, net}, ...]
    依淨買超排序（大→小）。
    """
    url = "https://bsr.twse.com.tw/bshtm/bsContent.aspx"
    session = requests.Session()
    session.headers.update({
        "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
    })

    # 1. GET 拿 ASP.NET form state
    resp = session.get(url, timeout=15)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    def val(field_id):
        tag = soup.find("input", {"id": field_id})
        return tag["value"] if tag else ""

    # 2. POST 查詢（券商代號欄位）
    form = {
        "__VIEWSTATE": val("__VIEWSTATE"),
        "__VIEWSTATEGENERATOR": val("__VIEWSTATEGENERATOR"),
        "__EVENTVALIDATION": val("__EVENTVALIDATION"),
        "TextBox_Stkno": broker_code,
        "CaptchaControl1": "",
        "btnOK": "查詢",
    }
    resp = session.post(url, data=form, timeout=15)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    # 3. 解析表格
    results = []
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue
        for row in rows[1:]:
            cols = [td.get_text(strip=True) for td in row.find_all("td")]
            if len(cols) < 4:
                continue
            try:
                buy = int(cols[-3].replace(",", ""))
                sell = int(cols[-2].replace(",", ""))
                net = buy - sell
                # 股票代號通常在第一欄，名稱在第二欄
                stock = cols[0]
                name = cols[1] if len(cols) >= 5 else ""
                results.append({
                    "stock": stock, "name": name,
                    "buy": buy, "sell": sell, "net": net,
                })
            except (ValueError, IndexError):
                continue

    results.sort(key=lambda x: x["net"], reverse=True)
    return results


def format_branch_report(broker_code, display_name, style):
    """產生單一分點的文字報告。"""
    header = f"【{display_name}】（{style}）"
    try:
        trades = get_branch_trades(broker_code)
        if not trades:
            return f"{header}\n  無資料（可能非交易日或代號有誤）"

        lines = [header]
        # 買超前 N
        buys = [t for t in trades if t["net"] > 0][:TOP_N]
        if buys:
            lines.append("  📈 買超：")
            for t in buys:
                label = f"{t['stock']} {t['name']}" if t["name"] else t["stock"]
                lines.append(f"    {label}  +{t['net']:,} 張")

        # 賣超前 N
        sells = [t for t in trades if t["net"] < 0]
        sells.sort(key=lambda x: x["net"])
        sells = sells[:TOP_N]
        if sells:
            lines.append("  📉 賣超：")
            for t in sells:
                label = f"{t['stock']} {t['name']}" if t["name"] else t["stock"]
                lines.append(f"    {label}  {t['net']:,} 張")

        return "\n".join(lines)
    except Exception as e:
        return f"{header}\n  抓取失敗：{e}"


# ── 綜合判斷 ──

def overall_direction(fx_diff, spread):
    fx_bull = fx_diff <= -FX_THRESHOLD
    fx_bear = fx_diff >= FX_THRESHOLD
    fut_bull = spread is not None and spread >= SPREAD_THRESHOLD
    fut_bear = spread is not None and spread <= -SPREAD_THRESHOLD

    if fx_bull and fut_bull:
        return "方向偏多。權值股優先。"
    if fx_bear and fut_bear:
        return "方向偏空。今天別衝。"
    if fx_bull or fut_bull:
        return "偏多但訊號不一致。小量試單，嚴設停損。"
    if fx_bear or fut_bear:
        return "偏空但訊號不一致。觀望為主。"
    return "方向不明。等 9:15 再決定。"


# ── Telegram ──

def send_telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[Telegram] 未設定 TOKEN / CHAT_ID，僅印出結果")
        print(text)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=10)
        r.raise_for_status()
    except Exception as e:
        print(f"[Telegram 發送失敗] {e}")
        print(text)


# ── 主程式 ──

def main():
    today = dt.date.today().strftime("%Y-%m-%d")
    lines = [f"📊 開盤前檢查 {today}", ""]

    # 1. 匯率
    diff = 0
    try:
        cur, prev, diff = get_usdtwd()
        lines.append(f"【匯率】USD/TWD {cur:.3f}（前日 {prev:.3f}）")
        lines.append(f"  {judge_fx(diff)}")
    except Exception as e:
        lines.append(f"【匯率】抓取失敗：{e}")

    lines.append("")

    # 2. 台指期（Yahoo only）
    spread = None
    try:
        spot = float(yf.Ticker("^TWII").history(period="5d")["Close"].iloc[-1])
        lines.append(f"【現貨】加權指數 {spot:.0f}")

        symbol = get_tx_symbol()
        yahoo = get_night_from_yahoo(symbol)
        if yahoo:
            spread = yahoo["price"] - spot
            lines.append(f"【夜盤】{symbol} 即時 {yahoo['price']:.0f}（{yahoo['source']}）")
            lines.append(f"  vs 現貨價差 {spread:+.0f} 點")
            lines.append(f"  📐 {judge_futures(spread)}")
        else:
            lines.append(f"【夜盤】{symbol} 抓不到價格")
    except Exception as e:
        lines.append(f"【期貨】抓取失敗：{e}")

    lines.append("")

    # 3. 分點籌碼
    lines.append("── 分點追蹤 ──")
    for code, name, style in TARGET_BRANCHES:
        lines.append(format_branch_report(code, name, style))
        lines.append("")

    lines.append(f"📝 {overall_direction(diff, spread)}")

    msg = "\n".join(lines)
    print(msg)
    send_telegram(msg)


if __name__ == "__main__":
    main()
# """
# 開盤前自動化檢查：匯率 + 台指期夜盤
# 每天早上 8:50 執行，結果發到 Telegram。

# 安裝：
#     pip install yfinance requests beautifulsoup4

# 設定（改下面三個變數）：
#     TELEGRAM_TOKEN：從 @BotFather 拿到
#     TELEGRAM_CHAT_ID：跟你的 bot 說句話後，瀏覽器開
#                       https://api.telegram.org/bot<TOKEN>/getUpdates
#                       找 "chat":{"id": ... }
#     FX_THRESHOLD：匯率升貶多少算「明顯」（作者用 0.1）

# 排程（Mac/Linux）：
#     crontab -e
#     50 8 * * 1-5 /usr/bin/python3 /path/to/premarket_check.py

# 排程（Windows）：工作排程器 → 每天 8:50 → 執行 python premarket_check.py
# """

# import datetime as dt
# import json
# import re
# import requests
# import yfinance as yf

# import os

# # ========== 設定區 ==========
# # 優先讀環境變數（雲端用），沒有就用下面寫死的值（本地測試用）
# TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
# TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
# FX_THRESHOLD = 0.10       # 台幣升貶超過這個數字算「明顯」
# SPREAD_THRESHOLD = 100    # 台指期跟現貨價差超過這個點數算「明顯」
# # ============================


# def get_usdtwd():
#     """抓美元/台幣現價與前一日收盤，回傳 (現價, 前日收盤, 差額)。
#     差額為負 = 台幣升值，為正 = 台幣貶值。
#     """
#     t = yf.Ticker("TWD=X")
#     hist = t.history(period="5d", interval="1d")
#     if len(hist) < 2:
#         raise RuntimeError("匯率資料不足")
#     prev_close = float(hist["Close"].iloc[-2])
#     current = float(hist["Close"].iloc[-1])
#     return current, prev_close, current - prev_close


# # 台指期月份代碼：F=1月 G=2月 H=3月 J=4月 K=5月 M=6月 N=7月 Q=8月 U=9月 V=10月 X=11月 Z=12月
# MONTH_CODES = {1: "F", 2: "G", 3: "H", 4: "J", 5: "K", 6: "M",
#                7: "N", 8: "Q", 9: "U", 10: "V", 11: "X", 12: "Z"}


# def yahoo_tx_symbol_from_contract(contract_date_str):
#     """把 FinMind 的 '202604' 轉成 Yahoo 股市的 'WTXJ6'"""
#     year = int(contract_date_str[:4])
#     month = int(contract_date_str[4:6])
#     return f"WTX{MONTH_CODES[month]}{year % 10}"


# def _find_price_in_json(obj, keys=("regularMarketPrice", "last", "price", "lastPrice"), depth=0):
#     """在巢狀 JSON 裡遞迴找出價格欄位。"""
#     if depth > 25 or obj is None:
#         return None
#     if isinstance(obj, dict):
#         for k in keys:
#             if k in obj:
#                 v = obj[k]
#                 if isinstance(v, dict) and "raw" in v:
#                     return float(v["raw"])
#                 if isinstance(v, (int, float)) and v > 0:
#                     return float(v)
#                 if isinstance(v, str):
#                     try:
#                         return float(v.replace(",", ""))
#                     except ValueError:
#                         pass
#         for v in obj.values():
#             result = _find_price_in_json(v, keys, depth + 1)
#             if result is not None:
#                 return result
#     elif isinstance(obj, list):
#         for item in obj:
#             result = _find_price_in_json(item, keys, depth + 1)
#             if result is not None:
#                 return result
#     return None


# def get_night_from_yahoo(symbol):
#     """爬 Yahoo 股市台灣的台指期即時報價頁面。
#     範例 URL: https://tw.stock.yahoo.com/quote/WTXJ6
#     兩段式抓取：先試 __NEXT_DATA__ 的 JSON，失敗就用 regex 抓 HTML 價格。
#     回傳 dict: {symbol, price, source} 或 None。
#     """
#     url = f"https://tw.stock.yahoo.com/quote/{symbol}"
#     headers = {
#         "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
#                        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
#         "Accept-Language": "zh-TW,zh;q=0.9",
#     }
#     r = requests.get(url, headers=headers, timeout=15)
#     r.raise_for_status()
#     html = r.text

#     # 嘗試 1：從 Next.js 的 __NEXT_DATA__ JSON 拿
#     m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.+?)</script>', html, re.DOTALL)
#     if m:
#         try:
#             data = json.loads(m.group(1))
#             price = _find_price_in_json(data)
#             if price:
#                 return {"symbol": symbol, "price": price, "source": "Yahoo TW (JSON)", "url": url}
#         except Exception:
#             pass

#     # 嘗試 2：從 HTML class 抓大字級價格元素
#     for pattern in [
#         r'Fz\(32px\)[^>]*>([\d,]+\.\d+)<',
#         r'data-reactid="[^"]*"[^>]*>([\d,]+\.\d+)</span>',
#         r'"regularMarketPrice"[^}]*?"raw":([\d.]+)',
#     ]:
#         m = re.search(pattern, html)
#         if m:
#             try:
#                 price = float(m.group(1).replace(",", ""))
#                 return {"symbol": symbol, "price": price, "source": "Yahoo TW (HTML)", "url": url}
#             except ValueError:
#                 continue

#     return None


# def get_night_from_finmind():
#     """抓 FinMind 台指期夜盤（after_market）近月合約資料，回傳 dict 或 None。
#     注意：此資料源的夜盤標記邏輯可疑，僅作交叉比對用。
#     """
#     end = dt.date.today()
#     start = end - dt.timedelta(days=10)
#     url = "https://api.finmindtrade.com/api/v4/data"
#     params = {
#         "dataset": "TaiwanFuturesDaily",
#         "data_id": "TX",
#         "start_date": start.strftime("%Y-%m-%d"),
#         "end_date": end.strftime("%Y-%m-%d"),
#     }
#     r = requests.get(url, params=params, timeout=15)
#     r.raise_for_status()
#     rows = r.json().get("data", [])
#     rows = [
#         row for row in rows
#         if row.get("trading_session") == "after_market"
#         and "/" not in str(row.get("contract_date", ""))
#         and row.get("volume", 0) > 0
#     ]
#     if not rows:
#         return None
#     latest_date = max(row["date"] for row in rows)
#     same_day = [row for row in rows if row["date"] == latest_date]
#     same_day.sort(key=lambda x: x.get("volume", 0), reverse=True)
#     return same_day[0]


# def get_taifex_tx_day():
#     """從 FinMind 抓台指期日盤（position session）近月合約最新收盤。
#     挑選邏輯：排除價差交易 → 取日盤同一天成交量最大的合約。
#     回傳 dict 含 close / date / contract_date / volume。
#     """
#     end = dt.date.today()
#     start = end - dt.timedelta(days=10)
#     url = "https://api.finmindtrade.com/api/v4/data"
#     params = {
#         "dataset": "TaiwanFuturesDaily",
#         "data_id": "TX",
#         "start_date": start.strftime("%Y-%m-%d"),
#         "end_date": end.strftime("%Y-%m-%d"),
#     }
#     r = requests.get(url, params=params, timeout=15)
#     r.raise_for_status()
#     rows = r.json().get("data", [])

#     # 只要日盤、排除價差交易、排除零成交
#     rows = [
#         row for row in rows
#         if row.get("trading_session") == "position"
#         and "/" not in str(row.get("contract_date", ""))
#         and row.get("volume", 0) > 0
#     ]
#     if not rows:
#         return None

#     latest_date = max(row["date"] for row in rows)
#     same_day = [row for row in rows if row["date"] == latest_date]
#     same_day.sort(key=lambda x: x.get("volume", 0), reverse=True)
#     return same_day[0]


# def get_taiex_and_futures():
#     """抓加權指數現貨收盤與台指期日盤近月收盤。回傳 (現貨, 期貨dict, 價差)"""
#     spot = float(yf.Ticker("^TWII").history(period="5d")["Close"].iloc[-1])
#     try:
#         day = get_taifex_tx_day()
#         if day is None:
#             return spot, None, None
#         return spot, day, day["close"] - spot
#     except Exception as e:
#         print(f"[台指期抓取失敗] {e}")
#         return spot, None, None


# def judge_fx(diff):
#     """根據匯率差額產生一句話判斷。"""
#     if diff <= -FX_THRESHOLD:
#         return f"台幣明顯升值（{diff:+.3f}）→ 外資錢進來，權值股有機會"
#     if diff >= FX_THRESHOLD:
#         return f"台幣明顯貶值（{diff:+.3f}）→ 外資匯出，今天別衝"
#     return f"匯率平盤（{diff:+.3f}）→ 回到個股判斷"


# def judge_futures(spread):
#     """根據期現貨價差產生一句話判斷。注意：6-8 月除息旺季要手動扣除息點數。"""
#     if spread is None:
#         return "台指期資料抓取失敗，請手動確認"
#     month = dt.date.today().month
#     warn = "（⚠️ 除息旺季，記得扣除息點數再判斷）" if month in (6, 7, 8) else ""
#     if spread >= SPREAD_THRESHOLD:
#         return f"正價差 {spread:+.0f} 點 → 開高機率高{warn}"
#     if spread <= -SPREAD_THRESHOLD:
#         return f"逆價差 {spread:+.0f} 點 → 開低機率高{warn}"
#     return f"價差 {spread:+.0f} 點 → 無明顯方向{warn}"


# def overall_direction(fx_diff, spread):
#     """綜合兩者給出一句話結論，模仿作者筆記本風格。"""
#     fx_bull = fx_diff <= -FX_THRESHOLD
#     fx_bear = fx_diff >= FX_THRESHOLD
#     fut_bull = spread is not None and spread >= SPREAD_THRESHOLD
#     fut_bear = spread is not None and spread <= -SPREAD_THRESHOLD

#     if fx_bull and fut_bull:
#         return "方向偏多。權值股優先。"
#     if fx_bear and fut_bear:
#         return "方向偏空。今天別衝。"
#     if fx_bull or fut_bull:
#         return "偏多但訊號不一致。小量試單，嚴設停損。"
#     if fx_bear or fut_bear:
#         return "偏空但訊號不一致。觀望為主。"
#     return "方向不明。等 9:15 再決定。"


# def send_telegram(text):
#     """發訊息到 Telegram。失敗就印在終端機不中斷。"""
#     url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
#     try:
#         r = requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=10)
#         r.raise_for_status()
#     except Exception as e:
#         print(f"[Telegram 發送失敗] {e}")
#         print(text)


# def main():
#     today = dt.date.today().strftime("%Y-%m-%d")
#     lines = [f"📊 開盤前檢查 {today}", ""]

#     # 第一件事：匯率
#     try:
#         cur, prev, diff = get_usdtwd()
#         lines.append(f"【匯率】USD/TWD {cur:.3f}（前日 {prev:.3f}）")
#         lines.append(f"  {judge_fx(diff)}")
#     except Exception as e:
#         lines.append(f"【匯率】抓取失敗：{e}")
#         diff = 0

#     lines.append("")

#     # 第二件事：台指期
#     # 先用 FinMind 日盤資料確認「近月合約」是哪個（成交量最大的那個）
#     # 然後分別抓：日盤收盤（FinMind）、夜盤收盤（FinMind）、即時夜盤（Yahoo 股市）
#     spread_for_judge = None
#     try:
#         spot, day, spread = get_taiex_and_futures()
#         lines.append(f"【現貨】加權指數 {spot:.0f}")

#         if day:
#             lines.append(
#                 f"【日盤收盤】{day['contract_date']} 合約 {day['close']:.0f}"
#                 f"（{day['date']}，量 {day['volume']:,}）"
#             )
#             lines.append(f"  vs 現貨價差 {spread:+.0f} 點")
#             spread_for_judge = spread

#             # FinMind 夜盤（來源 A）
#             try:
#                 fin_night = get_night_from_finmind()
#                 if fin_night:
#                     fin_spread = fin_night["close"] - spot
#                     lines.append(
#                         f"【夜盤 FinMind】{fin_night['contract_date']} 收 {fin_night['close']:.0f}"
#                         f"（{fin_night['date']}）"
#                     )
#                     lines.append(f"  vs 現貨價差 {fin_spread:+.0f} 點")
#                 else:
#                     lines.append("【夜盤 FinMind】無資料")
#             except Exception as e:
#                 lines.append(f"【夜盤 FinMind】抓取失敗：{e}")

#             # Yahoo 股市夜盤（來源 B）
#             try:
#                 symbol = yahoo_tx_symbol_from_contract(day["contract_date"])
#                 yahoo_night = get_night_from_yahoo(symbol)
#                 if yahoo_night:
#                     yahoo_spread = yahoo_night["price"] - spot
#                     lines.append(
#                         f"【夜盤 Yahoo】{symbol} 即時 {yahoo_night['price']:.0f}"
#                         f"（來源 {yahoo_night['source']}）"
#                     )
#                     lines.append(f"  vs 現貨價差 {yahoo_spread:+.0f} 點")
#                 else:
#                     lines.append(f"【夜盤 Yahoo】{symbol} 抓不到價格（網頁結構可能變了）")
#             except Exception as e:
#                 lines.append(f"【夜盤 Yahoo】抓取失敗：{e}")

#             lines.append(f"  📐 判斷依據：日盤價差 → {judge_futures(spread)}")
#         else:
#             lines.append("【日盤】無資料")
#     except Exception as e:
#         lines.append(f"【期貨】抓取失敗：{e}")

#     lines.append("")
#     lines.append(f"📝 {overall_direction(diff, spread_for_judge)}")
#     lines.append("")
#     lines.append("⚠️ 分點籌碼請手動確認（過去5日連續買超的主力分點）")

#     msg = "\n".join(lines)
#     print(msg)
#     send_telegram(msg)


# if __name__ == "__main__":
#     main()
