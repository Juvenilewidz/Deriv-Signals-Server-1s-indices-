# bot.py (replace or append whole file with this)
import os
import io
import requests
from typing import List, Optional
import matplotlib.pyplot as plt
from datetime import datetime, timezone

# Read env once (these wrappers allow passing tokens too)
ENV_TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ENV_TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# ============== Low-level Telegram helpers ==============
def _post_telegram_text(token: str, chat_id: str, text: str):
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        resp = requests.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"}, timeout=20)
        return resp.ok, resp.text
    except Exception as e:
        return False, str(e)

def _post_telegram_photo_file(token: str, chat_id: str, caption: str, photo_path: str):
    try:
        url = f"https://api.telegram.org/bot{token}/sendPhoto"
        with open(photo_path, "rb") as f:
            files = {"photo": (os.path.basename(photo_path), f)}
            data = {"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"}
            resp = requests.post(url, files=files, data=data, timeout=30)
        return resp.ok, resp.text
    except Exception as e:
        return False, str(e)

def _post_telegram_photo_bytes(token: str, chat_id: str, caption: str, image_bytes: bytes):
    try:
        url = f"https://api.telegram.org/bot{token}/sendPhoto"
        files = {"photo": ("chart.png", io.BytesIO(image_bytes))}
        data = {"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"}
        resp = requests.post(url, files=files, data=data, timeout=30)
        return resp.ok, resp.text
    except Exception as e:
        return False, str(e)

# ============== Backwards-compatible API (expected by main.py) ==============
def send_telegram_message(token: str, chat_id: str, text: str):
    """
    Signature expected by main.py
    """
    if not token or not chat_id:
        # fall back to env or no-op
        token = token or ENV_TELEGRAM_BOT_TOKEN
        chat_id = chat_id or ENV_TELEGRAM_CHAT_ID
    if not token or not chat_id:
        # nothing to do
        return False, "no-token-or-chat"
    ok, info = _post_telegram_text(token, chat_id, text)
    return ok, info

def send_telegram_photo(token: str, chat_id: str, caption: str, photo_path: str):
    """
    Signature expected by main.py: send photo by file path
    """
    if not token or not chat_id:
        token = token or ENV_TELEGRAM_BOT_TOKEN
        chat_id = chat_id or ENV_TELEGRAM_CHAT_ID
    if not token or not chat_id:
        return False, "no-token-or-chat"
    ok, info = _post_telegram_photo_file(token, chat_id, caption, photo_path)
    return ok, info

def send_single_timeframe_signal(symbol: str, tf: int, direction: str, reason: str, chart_path: Optional[str] = None):
    """
    Called by main.py as a fallback / simple notification.
    Uses environment TELEGRAM_* if available.
    """
    token = ENV_TELEGRAM_BOT_TOKEN
    chat_id = ENV_TELEGRAM_CHAT_ID
    if not token or not chat_id:
        # no Telegram configured; print fallback
        print("[SIG]", symbol, tf, direction, reason, chart_path)
        return False
    caption = f"🔔 {symbol} | {tf//60}m | {direction}\n{reason}"
    if chart_path:
        ok, info = send_telegram_photo(token, chat_id, caption, chart_path)
        return ok
    else:
        ok, _ = send_telegram_message(token, chat_id, caption)
        return ok

def send_strong_signal(symbol: str, direction: str, details: str, chart_path: Optional[str] = None):
    """
    Called by main.py for stronger alerts.
    """
    token = ENV_TELEGRAM_BOT_TOKEN
    chat_id = ENV_TELEGRAM_CHAT_ID
    if not token or not chat_id:
        print("[STRONG]", symbol, direction, details, chart_path)
        return False
    caption = f"🚨 {symbol}  •  {direction}\n{details}"
    if chart_path:
        ok, info = send_telegram_photo(token, chat_id, caption, chart_path)
        return ok
    else:
        ok, _ = send_telegram_message(token, chat_id, caption)
        return ok

# ============== Existing convenience API (kept) ==============
def send_telegram_message_simple(text: str):
    """Deprecated name — kept for backward compat, uses env vars."""
    token = ENV_TELEGRAM_BOT_TOKEN
    chat_id = ENV_TELEGRAM_CHAT_ID
    if not token or not chat_id:
        return
    _post_telegram_text(token, chat_id, text)

def send_telegram_photo_bytes(caption: str, image_bytes: bytes):
    """Deprecated name — kept for backward compat with code that used bytes directly."""
    token = ENV_TELEGRAM_BOT_TOKEN
    chat_id = ENV_TELEGRAM_CHAT_ID
    if not token or not chat_id:
        return
    _post_telegram_photo_bytes(token, chat_id, caption, image_bytes)

# ============== (Optional) chart helper - keep if you rely on it elsewhere ==============
def make_signal_chart(asset_alias: str,
                      tf: int,
                      candles: List[dict],
                      ma1: List[Optional[float]],
                      ma2: List[Optional[float]],
                      ma3: List[Optional[float]],
                      highlight_index: Optional[int] = None,
                      pad_right: int = 10) -> bytes:
    n = len(candles)
    fig, ax = plt.subplots(figsize=(13, 5), dpi=140)

    # simple candlesticks
    for i, c in enumerate(candles):
        o, h, l, cl = c["open"], c["high"], c["low"], c["close"]
        color = "#26a69a" if cl >= o else "#ef5350"
        ax.vlines(i, l, h, linewidth=1, color=color)
        ax.add_patch(plt.Rectangle((i - 0.35, min(o, cl)), 0.7, abs(cl - o), linewidth=0.6,
                                   edgecolor=color, facecolor=color, alpha=0.9))
    xs = range(n)
    y1 = [ma1[i] if ma1[i] is not None else float("nan") for i in range(n)]
    y2 = [ma2[i] if ma2[i] is not None else float("nan") for i in range(n)]
    y3 = [ma3[i] if ma3[i] is not None else float("nan") for i in range(n)]
    ax.plot(xs, y1, linewidth=1.2, label="MA1(9) SMMA HLC3")
    ax.plot(xs, y2, linewidth=1.2, label="MA2(19) SMMA Close")
    ax.plot(xs, y3, linewidth=1.2, label="MA3(25) SMA on MA2")

    if highlight_index is not None and 0 <= highlight_index < n:
        c = candles[highlight_index]
        ax.vlines(highlight_index, c["low"], c["high"], linewidth=3, color="#ffd54f", alpha=0.9)
    ax.set_xlim(-1, n + pad_right)
    lows  = [c["low"] for c in candles]
    highs = [c["high"] for c in candles]
    ymin, ymax = min(lows), max(highs)
    yrange = ymax - ymin if ymax > ymin else 1
    ax.set_ylim(ymin - 0.05 * yrange, ymax + 0.05 * yrange)
    ax.grid(True, linewidth=0.4, alpha=0.3)
    ax.legend(loc="upper left", fontsize=8)
    tf_min = tf // 60
    ax.set_title(f"{asset_alias}  •  {tf_min}m  •  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    buf = io.BytesIO()
    plt.tight_layout()
    plt.savefig(buf, format="png")
    plt.close(fig)
    return buf.getvalue()
