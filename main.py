#!/usr/bin/env python3
"""
main_1s.py — Dynamic Support & Resistance (Strict DSR, 1s Markets, No Ranging)

Advanced DSR strategy:
- MA1 (SMMA HLC3-9), MA2 (SMMA Close-19) = Dynamic S/R
- MA3 (SMA MA2-25) = Trend filter (must act as filter, with strict MA arrangement)
- Signal only on strict rejection candle at MA1 or MA2, matching prevailing trend, and only if market is trending (ignore ranging)
- Multiple signals allowed as long as criteria met
"""

import os, json, time, tempfile, traceback, requests
from datetime import datetime, timezone
import websocket, matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle

# Telegram helpers (fallback to print)
try:
    from bot import send_telegram_message, send_telegram_photo
except Exception:
    def send_telegram_message(token, chat_id, text): print("[TEXT]", text); return True, "local"
    def send_telegram_photo(token, chat_id, caption, photo): print("[PHOTO]", caption, photo); return True, "local"

# -------------------------
# Config
# -------------------------
DERIV_API_KEY = os.getenv("DERIV_API_KEY","").strip()
DERIV_APP_ID  = os.getenv("DERIV_APP_ID","1089").strip()
DERIV_WS_URL  = f"wss://ws.derivws.com/websockets/v3?app_id={DERIV_APP_ID}"
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN","").strip()
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID","").strip()

TIMEFRAMES = [int(x) for x in os.getenv("TIMEFRAMES","1").split(",") if x.strip().isdigit()]
DEBUG = os.getenv("DEBUG","0") == "1"
TEST_MODE = os.getenv("TEST_MODE","0") == "1"

CANDLES_N = 480
LAST_N_CHART = 180
CANDLE_WIDTH = 0.35
TMPDIR = tempfile.gettempdir()
ALERT_FILE = os.path.join(TMPDIR, "dsr_last_sent_main_1s.json")
MIN_CANDLES = 50

# -------------------------
# Symbol Mappings
# -------------------------
SYMBOL_MAP = {
    "V75(1s)": "1HZ75V",
    "V100(1s)": "1HZ100V", 
    "V150(1s)": "1HZ150V",
}

# -------------------------
# WebSocket Data Fetching
# -------------------------
def fetch_candles(symbol, timeframe, count=None):
    """Fetch candlestick data from Deriv WebSocket API with HTTP fallback"""
    if count is None:
        count = CANDLES_N
    candles = []
    ws = None
    try:
        ws = websocket.create_connection(DERIV_WS_URL, timeout=20)
        if DERIV_API_KEY:
            ws.send(json.dumps({"authorize": DERIV_API_KEY}))
            ws.recv()  # Ignore auth response
        request = {
            "ticks_history": symbol,
            "style": "candles", 
            "granularity": timeframe,
            "count": count,
            "end": "latest"
        }
        ws.send(json.dumps(request))
        response = json.loads(ws.recv())
        ws.close()
        if DEBUG:
            print(f"Fetched {len(response.get('candles', []))} candles for {symbol}")
        if "candles" in response and response["candles"]:
            return [{
                "epoch": int(c["epoch"]),
                "open": float(c["open"]),
                "high": float(c["high"]),
                "low": float(c["low"]),
                "close": float(c["close"])
            } for c in response["candles"]]
    except Exception as e:
        if DEBUG:
            print(f"WebSocket failed for {symbol}: {e}")
        # HTTP fallback
        candles = fetch_candles_http_fallback(symbol, timeframe, count)
    finally:
        if ws:
            try: ws.close()
            except: pass
    return candles

def fetch_candles_http_fallback(symbol, timeframe, count=None):
    """Fallback HTTP method to fetch candles"""
    if count is None:
        count = CANDLES_N
    try:
        url = f"https://api.deriv.com/api/v1/ticks_history"
        params = {
            "ticks_history": symbol,
            "adjust_start_time": 1,
            "count": count,
            "end": "latest",
            "start": 1,
            "style": "candles",
            "granularity": timeframe
        }
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        candles = []
        if data.get("msg_type") == "candles":
            candle_data = data.get("candles", [])
            for candle in candle_data:
                candles.append({
                    "epoch": int(candle.get("epoch", 0)),
                    "open": float(candle.get("open", 0)),
                    "high": float(candle.get("high", 0)),
                    "low": float(candle.get("low", 0)),
                    "close": float(candle.get("close", 0))
                })
        candles.sort(key=lambda x: x["epoch"])
        return candles
    except Exception as e:
        if DEBUG:
            print(f"HTTP fallback failed for {symbol}: {e}")
        return []

# -------------------------
# Persistence
# -------------------------
def load_persist():
    try:
        return json.load(open(ALERT_FILE))
    except Exception:
        return {}

def save_persist(d):
    try:
        json.dump(d, open(ALERT_FILE,"w"))
    except Exception:
        pass

def already_sent(shorthand, tf, epoch, side):
    if TEST_MODE:
        return False
    rec = load_persist().get(f"{shorthand}|{tf}")
    return bool(rec and rec.get("epoch") == epoch and rec.get("side") == side)

def mark_sent(shorthand, tf, epoch, side):
    d = load_persist()
    d[f"{shorthand}|{tf}"] = {"epoch": epoch, "side": side}
    save_persist(d)

# -------------------------
# Moving Averages
# -------------------------
def smma_correct(series, period):
    n = len(series)
    if n < period:
        return [None] * n
    result = [None] * (period - 1)
    first_sma = sum(series[:period]) / period
    result.append(first_sma)
    prev_smma = first_sma
    for i in range(period, n):
        current_smma = (prev_smma * (period - 1) + series[i]) / period
        result.append(current_smma)
        prev_smma = current_smma
    return result

def sma(series, period):
    n = len(series)
    if n < period:
        return [None] * n
    result = [None] * (period - 1)
    window_sum = sum(series[:period])
    result.append(window_sum / period)
    for i in range(period, n):
        window_sum += series[i] - series[i - period]
        result.append(window_sum / period)
    return result

def compute_mas(candles):
    closes = [c["close"] for c in candles]
    hlc3 = [(c["high"] + c["low"] + c["close"]) / 3.0 for c in candles]
    ma1 = smma_correct(hlc3, 9)
    ma2 = smma_correct(closes, 19)
    ma2_valid = [v for v in ma2 if v is not None]
    if len(ma2_valid) >= 25:
        ma3_calc = sma(ma2_valid, 25)
        ma3 = []
        valid_idx = 0
        for v in ma2:
            if v is None:
                ma3.append(None)
            else:
                if valid_idx < len(ma3_calc):
                    ma3.append(ma3_calc[valid_idx])
                else:
                    ma3.append(None)
                valid_idx += 1
    else:
        ma3 = [None] * len(candles)
    return ma1, ma2, ma3

# -------------------------
# Strict Rejection Detection
# -------------------------
def is_strict_rejection_candle(candle):
    """
    Returns (bool, "UPPER_REJECTION"/"LOWER_REJECTION"/"NONE"):
    - Wick >= 1.5 × body
    - Wick >= 60% of candle range
    - Only upper or lower rejection, NOT just interaction
    """
    o, h, l, c = candle["open"], candle["high"], candle["low"], candle["close"]
    body = abs(c - o)
    rng = h - l
    if rng <= 0 or body == 0:
        return False, "NONE"
    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - l
    # Upper rejection
    if upper_wick >= 1.5 * body and upper_wick >= 0.6 * rng:
        return True, "UPPER_REJECTION"
    # Lower rejection
    if lower_wick >= 1.5 * body and lower_wick >= 0.6 * rng:
        return True, "LOWER_REJECTION"
    return False, "NONE"

def candle_touches_ma(candle, ma_val):
    """Returns True if candle's high/low actually crosses MA value"""
    if ma_val is None:
        return False
    return candle["low"] <= ma_val <= candle["high"]

# -------------------------
# Trend Filter & MA Rearrangement (Strict, Non-Ranging)
# -------------------------
def is_market_trending(ma1, ma2, ma3, price):
    """
    Returns "BULLISH", "BEARISH", or None (ranging/consolidation).
    Trend detected if:
    - MA1 closest to price, then MA2, then MA3 (no tangling)
    - For bullish: price > MA3, order is MA1, MA2, MA3 (MA1 closest, MA3 furthest below)
    - For bearish: price < MA3, order is MA1, MA2, MA3 (MA1 closest, MA3 furthest above)
    """
    if None in [ma1, ma2, ma3]:
        return None

    # Distance from price to each MA
    d_ma1 = abs(price - ma1)
    d_ma2 = abs(price - ma2)
    d_ma3 = abs(price - ma3)

    # MA order by proximity to price
    proximity = sorted([(d_ma1, 'MA1'), (d_ma2, 'MA2'), (d_ma3, 'MA3')], key=lambda x: x[0])
    order = [x[1] for x in proximity]

    # Bullish: price > MA3, order is MA1, MA2, MA3 (MA1 closest, MA3 furthest below)
    if price > ma3 and order == ["MA1", "MA2", "MA3"] and ma1 > ma2 > ma3:
        return "BULLISH"
    # Bearish: price < ma3, order is MA1, MA2, MA3 (MA1 closest, MA3 furthest above)
    if price < ma3 and order == ["MA1", "MA2", "MA3"] and ma1 < ma2 < ma3:
        return "BEARISH"

    # Otherwise, likely ranging market
    return None

# -------------------------
# Core DSR Signal Detection (Strict)
# -------------------------
def detect_signal(candles, tf, shorthand):
    n = len(candles)
    if n < MIN_CANDLES:
        return None
    current_idx = n - 1
    current_candle = candles[current_idx]
    ma1, ma2, ma3 = compute_mas(candles)
    current_ma1 = ma1[current_idx] if current_idx < len(ma1) else None
    current_ma2 = ma2[current_idx] if current_idx < len(ma2) else None
    current_ma3 = ma3[current_idx] if current_idx < len(ma3) else None
    current_close = current_candle["close"]
    # Determine market trend (ignore ranging/consolidation)
    trend = is_market_trending(current_ma1, current_ma2, current_ma3, current_close)
    if trend is None:
        if DEBUG:
            print(f"Ranging market for {shorthand} - no trend detected.")
        return None

    # Confirmed candlestick close only, strict rejection candle
    is_rejection, pattern_type = is_strict_rejection_candle(current_candle)
    if not is_rejection:
        return None

    # Must be rejection at MA1 or MA2 (not just touch)
    touched_ma1 = candle_touches_ma(current_candle, current_ma1)
    touched_ma2 = candle_touches_ma(current_candle, current_ma2)
    if not (touched_ma1 or touched_ma2):
        return None

    # Rejection direction must match trend
    if trend == "BULLISH":
        # Only accept lower wick rejection at MA1/MA2 (bullish continuation)
        if pattern_type != "LOWER_REJECTION":
            return None
    elif trend == "BEARISH":
        # Only accept upper wick rejection at MA1/MA2 (bearish continuation)
        if pattern_type != "UPPER_REJECTION":
            return None

    # MA level for rejection
    if touched_ma1:
        ma_level = "MA1"
    elif touched_ma2:
        ma_level = "MA2"
    else:
        return None

    # Allow many signals if criteria met, as requested (no cooldown block here)
    if DEBUG:
        print(f"VALID DSR: {trend} - {pattern_type} at {ma_level} - Price: {current_close:.2f}, MA1: {current_ma1:.2f}, MA2: {current_ma2:.2f}, MA3: {current_ma3:.2f}")

    return {
        "symbol": shorthand,
        "tf": tf,
        "side": "BUY" if trend == "BULLISH" else "SELL",
        "pattern": pattern_type,
        "ma_level": ma_level,
        "ma_arrangement": "BULLISH_ARRANGEMENT" if trend == "BULLISH" else "BEARISH_ARRANGEMENT",
        "context": f"Confirmed {pattern_type.replace('_',' ').title()} at {ma_level}",
        "price": current_close,
        "ma1": current_ma1,
        "ma2": current_ma2, 
        "ma3": current_ma3,
        "idx": current_idx,
        "candles": candles,
        "ma1_array": ma1,
        "ma2_array": ma2,
        "ma3_array": ma3
    }

# -------------------------
# Chart Generation
# -------------------------
def create_signal_chart(signal_data):
    candles = signal_data["candles"]
    ma1, ma2, ma3 = signal_data["ma1_array"], signal_data["ma2_array"], signal_data["ma3_array"]
    signal_idx = signal_data["idx"]
    n = len(candles)
    chart_start = max(0, n - LAST_N_CHART)
    chart_candles = candles[chart_start:]
    plt.style.use('dark_background')
    fig, ax = plt.subplots(figsize=(14, 10))
    fig.patch.set_facecolor('black')
    ax.set_facecolor('black')
    # Plot candlesticks
    for i, candle in enumerate(chart_candles):
        o, h, l, c = candle["open"], candle["high"], candle["low"], candle["close"]
        if c >= o:
            body_color = "#00FF00"
            edge_color = "#00AA00"
        else:
            body_color = "#FF0000"
            edge_color = "#AA0000"
        ax.add_patch(Rectangle(
            (i - CANDLE_WIDTH/2, min(o, c)), 
            CANDLE_WIDTH, 
            max(abs(c - o), 1e-9),
            facecolor=body_color, 
            edgecolor=edge_color, 
            alpha=0.9,
            linewidth=1
        ))
        ax.plot([i, i], [l, h], color=edge_color, linewidth=1.2, alpha=0.8)
    # Plot all MAs
    def plot_ma(ma_values, label, color, linewidth=2):
        chart_ma = []
        for j in range(chart_start, n):
            if j < len(ma_values) and ma_values[j] is not None:
                chart_ma.append(ma_values[j])
            else:
                chart_ma.append(None)
        ax.plot(range(len(chart_candles)), chart_ma, 
                color=color, linewidth=linewidth, label=label, alpha=0.9)
    plot_ma(ma1, "MA1 (SMMA HLC3-9)", "#FFFFFF", 2)
    plot_ma(ma2, "MA2 (SMMA Close-19)", "#00BFFF", 2)
    plot_ma(ma3, "MA3 (SMA MA2-25)", "#FF6347", 2)
    # Mark signal point
    signal_chart_idx = signal_idx - chart_start
    if 0 <= signal_chart_idx < len(chart_candles):
        signal_candle = chart_candles[signal_chart_idx]
        signal_price = signal_candle["close"]
        if signal_data["side"] == "BUY":
            marker_color = "#00FF00"
            marker_symbol = "^"
        else:
            marker_color = "#FF0000" 
            marker_symbol = "v"
        ax.scatter([signal_chart_idx], [signal_price], 
                  color=marker_color, marker=marker_symbol, 
                  s=300, edgecolor="#FFFFFF", linewidth=3, zorder=10)
    arrangement_emoji = "📈" if signal_data["ma_arrangement"] == "BULLISH_ARRANGEMENT" else "📉"
    ax.set_title(f"{signal_data['symbol']} - {signal_data['side']} DSR Signal {arrangement_emoji}", 
                fontsize=16, color='white', fontweight='bold', pad=20)
    legend = ax.legend(loc="upper left", frameon=True, facecolor='black', 
                      edgecolor='white', fontsize=11)
    legend.get_frame().set_alpha(0.8)
    ax.grid(True, alpha=0.3, color='gray', linestyle='--', linewidth=0.5)
    ax.tick_params(colors='white', labelsize=10)
    for spine in ax.spines.values():
        spine.set_color('white')
    plt.tight_layout()
    chart_file = tempfile.NamedTemporaryFile(delete=False, suffix=".png")
    plt.savefig(chart_file.name, 
                dpi=150, 
                bbox_inches="tight", 
                facecolor='black',
                edgecolor='none',
                pad_inches=0.1)
    plt.close()
    plt.style.use('default')
    return chart_file.name

# -------------------------
# Main Execution
# -------------------------
def run_analysis():
    signals_found = 0
    for shorthand, deriv_symbol in SYMBOL_MAP.items():
        try:
            # Use 1s timeframe for all 1s symbols
            tf = 1
            if DEBUG:
                print(f"Analyzing {shorthand} on {tf}s...")
            candles = fetch_candles(deriv_symbol, tf)
            if len(candles) < MIN_CANDLES:
                if DEBUG:
                    print(f"Insufficient candles for {shorthand}: {len(candles)}")
                continue
            signal = detect_signal(candles, tf, shorthand)
            if not signal:
                continue
            current_epoch = signal["candles"][signal["idx"]]["epoch"]
            if already_sent(shorthand, tf, current_epoch, signal["side"]):
                if DEBUG:
                    print(f"Signal already sent for {shorthand}")
                continue
            arrangement_emoji = "📈" if signal["ma_arrangement"] == "BULLISH_ARRANGEMENT" else "📉"
            caption = (f"🎯 {signal['symbol']} {tf}s - {signal['side']} SIGNAL\n"
                      f"{arrangement_emoji} MA Setup: {signal['ma_arrangement'].replace('_', ' ')}\n" 
                      f"🎨 Pattern: {signal['pattern']}\n"
                      f"📍 Level: {signal['ma_level']} Dynamic S/R\n"
                      f"💰 Price: {signal['price']:.5f}\n"
                      f"📊 Context: {signal['context']}")
            chart_path = create_signal_chart(signal)
            success, msg_id = send_telegram_photo(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, caption, chart_path)
            if success:
                mark_sent(shorthand, tf, current_epoch, signal["side"])
                signals_found += 1
                if DEBUG:
                    print(f"DSR signal sent for {shorthand}: {signal['side']}")
            try:
                os.unlink(chart_path)
            except:
                pass
        except Exception as e:
            if DEBUG:
                print(f"Error analyzing {shorthand}: {e}")
                traceback.print_exc()
    if DEBUG:
        print(f"Analysis complete. {signals_found} DSR signals found.")

if __name__ == "__main__":
    try:
        run_analysis()
    except Exception as e:
        print(f"Critical error: {e}")
        traceback.print_exc()
