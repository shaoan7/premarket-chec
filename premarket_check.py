"""
開盤前自動化檢查：匯率 + 台指期夜盤
GitHub Actions 每天 8:35 (UTC 0:35) 執行，結果發到 Telegram。
"""

import datetime as dt
import json
import re
import os
import requests
import yfinance as yf

# ========== 設定區 ==========
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
FX_THRESHOLD = 0.10
SPREAD_THRESHOLD = 100
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
        r'"regularMarketPrice"[^}]*?"raw":([\d.]+)',
    ]:
        m = re.search(pattern, html)
        if m:
            try:
                return {"symbol": symbol, "price": float(m.group(1).replace(",", "")),
                        "source": "Yahoo TW (HTML)"}
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

    diff = 0
    try:
        cur, prev, diff = get_usdtwd()
        lines.append(f"【匯率】USD/TWD {cur:.3f}（前日 {prev:.3f}）")
        lines.append(f"  {judge_fx(diff)}")
    except Exception as e:
        lines.append(f"【匯率】抓取失敗：{e}")

    lines.append("")

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
    lines.append(f"📝 {overall_direction(diff, spread)}")
    lines.append("")
    lines.append("⚠️ 分點籌碼請本地端用 debug_broker.py 手動確認")

    msg = "\n".join(lines)
    print(msg)
    send_telegram(msg)


if __name__ == "__main__":
    main()
