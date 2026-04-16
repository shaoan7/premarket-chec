"""
開盤前自動化檢查：匯率 + 台指期夜盤
GCP Cloud Functions 版本
"""

import datetime as dt
import json
import re
import os
import requests
import yfinance as yf
import functions_framework

# ========== 設定區 ==========
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
# ============================

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
    score = -diff * 500
    score = max(-100, min(100, score))

    abs_diff = abs(diff)
    if abs_diff < 0.03:
        level = "幾乎沒動"
    elif abs_diff < 0.06:
        level = "微幅"
    elif abs_diff < 0.10:
        level = "小幅"
    elif abs_diff < 0.15:
        level = "明顯"
    elif abs_diff < 0.25:
        level = "大幅"
    else:
        level = "劇烈"

    direction = "升值" if diff < 0 else "貶值"
    emoji = "🟢" if diff < -0.03 else "🔴" if diff > 0.03 else "⚪"

    if abs_diff < 0.03:
        hint = "匯率中性，回到個股判斷"
    elif diff < 0:
        hint = "外資錢進來，權值股有機會" if abs_diff >= 0.10 else "外資小幅匯入，稍偏多"
    else:
        hint = "外資匯出，今天別衝" if abs_diff >= 0.10 else "外資小幅匯出，稍偏空"

    text = f"{emoji} 台幣{level}{direction}（{diff:+.3f}）→ {hint}（信心 {abs(score):.0f}%）"
    return text, score


# ── 台指期（Yahoo TW 永續合約）──

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


def get_night_futures():
    """
    抓 https://tw.stock.yahoo.com/future/WTX& 自動取得最近月台指期，
    回傳 {"symbol": "顯示名稱", "price": float, "source": str} 或 None
    """
    url = "https://tw.stock.yahoo.com/future/WTX&"
    headers = {
        "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
        "Accept-Language": "zh-TW,zh;q=0.9",
    }
    r = requests.get(url, headers=headers, timeout=15)
    r.raise_for_status()
    html = r.text

    # 嘗試從 __NEXT_DATA__ JSON 取價格
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.+?)</script>', html, re.DOTALL)
    price = None
    if m:
        try:
            data = json.loads(m.group(1))
            price = _find_price_in_json(data)
        except Exception:
            pass

    # fallback：HTML pattern
    if price is None:
        for pattern in [
            r'Fz\(32px\)[^>]*>([\d,]+\.\d+)<',
            r'"regularMarketPrice"[^}]*?"raw":([\d.]+)',
        ]:
            fm = re.search(pattern, html)
            if fm:
                try:
                    price = float(fm.group(1).replace(",", ""))
                    break
                except ValueError:
                    continue

    if price is None:
        return None

    # 嘗試從頁面標題抓實際合約名稱（如「台指期 202506」），找不到就用預設
    symbol_display = "台指期近月(WTX&)"
    tm = re.search(r'台指期\w*\s*(\d{6})', html)
    if tm:
        symbol_display = f"台指期 {tm.group(1)}"

    source = "Yahoo TW future (JSON)" if m and price else "Yahoo TW future (HTML)"
    return {"symbol": symbol_display, "price": price, "source": source}


def judge_futures(spread, spot=None):
    if spread is None:
        return "台指期資料抓取失敗，請手動確認", 0

    month = dt.date.today().month
    warn = "\n  ⚠️ 除息旺季，記得扣除息點數再判斷" if month in (6, 7, 8) else ""

    pct = (spread / spot * 100) if spot and spot > 0 else 0
    abs_pct = abs(pct)

    level = min(10, int(abs_pct / 0.2) + (1 if abs_pct > 0 else 0))
    level = max(0, level)

    LABELS = [
        "無感",
        "極微 (1/10)",
        "微弱 (2/10)",
        "輕微 (3/10)",
        "小幅 (4/10)",
        "中等 (5/10)",
        "偏強 (6/10)",
        "明顯 (7/10)",
        "強勢 (8/10)",
        "大幅 (9/10)",
        "極端 (10/10)",
    ]

    score = level * 10
    if pct < 0:
        score = -score

    direction = "正價差" if spread >= 0 else "逆價差"
    emoji = "🟢" if level >= 1 and pct > 0 else "🔴" if level >= 1 and pct < 0 else "⚪"

    if level == 0:
        hint = "期現貨接近，方向不明"
    elif pct > 0:
        hint = "開高機率高，可順勢做多" if level >= 5 else "稍偏多，但力道不強"
    else:
        hint = "開低機率高，不要亂接刀" if level >= 5 else "稍偏空，但力道不強"

    text = (f"{emoji} {LABELS[level]} {direction} {spread:+.0f} 點（{pct:+.2f}%）"
            f"→ {hint}{warn}")
    return text, score


# ── 綜合判斷 ──

def overall_direction(fx_score, fut_score):
    total = fx_score * 0.4 + fut_score * 0.6
    total = max(-100, min(100, total))

    if total >= 60:
        direction = "🟢🟢 強烈偏多"
        action = "權值股優先，可積極做多。"
    elif total >= 30:
        direction = "🟢 偏多"
        action = "順勢做多，但控制部位。"
    elif total >= 10:
        direction = "🟢 微偏多"
        action = "小量試單，嚴設停損。"
    elif total > -10:
        direction = "⚪ 中性"
        action = "方向不明，等 9:15 再決定。"
    elif total > -30:
        direction = "🔴 微偏空"
        action = "觀望為主，不追高。"
    elif total > -60:
        direction = "🔴 偏空"
        action = "今天別衝，等拉回再說。"
    else:
        direction = "🔴🔴 強烈偏空"
        action = "空方主導，不要亂接刀。"

    bar = _score_bar(total)
    return f"{direction}（綜合 {total:+.0f}）\n  {bar}\n  → {action}"


def _score_bar(score):
    width = 10
    mid = width // 2
    filled = int(abs(score) / 100 * mid)
    filled = max(1, min(mid, filled))

    bar = list("─" * width)
    bar[mid] = "│"
    if score > 0:
        for i in range(mid + 1, mid + 1 + filled):
            if i < width:
                bar[i] = "█"
    elif score < 0:
        for i in range(mid - filled, mid):
            if i >= 0:
                bar[i] = "█"

    return f"空[{''.join(bar)}]多"


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

@functions_framework.http
def main(request):
    today = dt.date.today().strftime("%Y-%m-%d")
    lines = [f"📊 開盤前檢查 {today}", ""]

    fx_score = 0
    fut_score = 0

    # 1. 匯率
    try:
        cur, prev, diff = get_usdtwd()
        fx_text, fx_score = judge_fx(diff)
        lines.append(f"【匯率】USD/TWD {cur:.3f}（前日 {prev:.3f}）")
        lines.append(f"  {fx_text}")
    except Exception as e:
        lines.append(f"【匯率】抓取失敗：{e}")

    lines.append("")

    # 2. 台指期（WTX& 永續，自動近月）
    try:
        # 現貨基準：先試 WTX00，失敗再用 ^TWII
        taiex = get_night_futures.__wrapped__ if hasattr(get_night_futures, "__wrapped__") else None
        spot_data = None
        try:
            spot_raw = requests.get(
                "https://tw.stock.yahoo.com/future/WTX00",
                headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "zh-TW"},
                timeout=15,
            )
            spot_raw.raise_for_status()
            _m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.+?)</script>',
                           spot_raw.text, re.DOTALL)
            if _m:
                spot_data = _find_price_in_json(json.loads(_m.group(1)))
        except Exception:
            pass

        if not spot_data:
            spot_data = float(yf.Ticker("^TWII").history(period="5d")["Close"].iloc[-1])

        spot = spot_data
        lines.append(f"【現貨】加權指數 {spot:.0f}")

        # 夜盤：WTX& 永續合約
        futures = get_night_futures()
        if futures:
            spread = futures["price"] - spot
            fut_text, fut_score = judge_futures(spread, spot)
            lines.append(f"【夜盤】{futures['symbol']} 即時 {futures['price']:.0f}（{futures['source']}）")
            lines.append(f"  vs 現貨價差 {spread:+.0f} 點")
            lines.append(f"  📐 {fut_text}")
        else:
            lines.append("【夜盤】WTX& 抓不到價格")

    except Exception as e:
        lines.append(f"【期貨】抓取失敗：{e}")

    lines.append("")
    lines.append(f"📝 {overall_direction(fx_score, fut_score)}")

    msg = "\n".join(lines)
    print(msg)
    send_telegram(msg)

    return "OK", 200


if __name__ == "__main__":
    class MockRequest:
        pass
    main(MockRequest())
