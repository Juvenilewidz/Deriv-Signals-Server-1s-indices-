#!/usr/bin/env python3
"""
main.py — Dynamic Support & Resistance (Complete Strategy Implementation)

Implements the exact DSR strategy from documentation:
- MA1 (SMMA HLC3-9), MA2 (SMMA Close-19), MA3 (SMA MA2-25)
- Only trade in trending markets with smoothly dispensed MAs
- Signal on rejection at MA1/MA2 + confirmation candle
- Above MA3 = Buy bias, Below MA3 = Sell bias
- No premature signals - wait for confirmation candle close
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

TIMEFRAMES = [int(x) for x in os.getenv("TIMEFRAMES","300").split(",") if x.strip().isdigit()]
DEBUG = os.getenv("DEBUG","0") == "1"
TEST_MODE = os.getenv("TEST_MODE","0") == "1"

CANDLES_N = 480
LAST_N_CHART = 180
CANDLE_WIDTH = 0.35
TMPDIR = tempfile.gettempdir()
ALERT_FILE = os.path.join(TMPDIR, "dsr_last_sent_main.json")
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
        
        if DEBUG:
            print(f"Trying HTTP fallback for {symbol}")
        
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
        if DEBUG:
            print(f"HTTP fallback fetched {len(candles)} candles for {symbol}")
        return candles
        
    except Exception as e:
        if DEBUG:
            print(f"HTTP fallback failed for {symbol}: {e}")
        return []

def fetch_candles(symbol, timeframe, count=None):
    """Fetch candlestick data from Deriv WebSocket API with HTTP fallback"""
    if count is None:
        count = CANDLES_N
    
    candles = []
    ws = None
    connection_established = False
    data_received = False
    
    try:
        import threading
        import queue
        
        # Use a queue to communicate between threads
        result_queue = queue.Queue()
        
        def on_message(ws, message):
            nonlocal candles, data_received
            try:
                data = json.loads(message)
                if DEBUG:
                    print(f"Received message type: {data.get('msg_type')}")
                
                # Handle different response types
                if data.get("msg_type") == "candles":
                    candle_data = data.get("candles", [])
                    if DEBUG:
                        print(f"Received {len(candle_data)} candles")
                    
                    for candle in candle_data:
                        candles.append({
                            "epoch": int(candle.get("epoch", 0)),
                            "open": float(candle.get("open", 0)),
                            "high": float(candle.get("high", 0)),
                            "low": float(candle.get("low", 0)),
                            "close": float(candle.get("close", 0))
                        })
                    
                    data_received = True
                    result_queue.put("SUCCESS")
                
                elif data.get("msg_type") == "ohlc":
                    ohlc = data.get("ohlc", {})
                    if ohlc:
                        candles.append({
                            "epoch": int(ohlc.get("epoch", 0)),
                            "open": float(ohlc.get("open", 0)),
                            "high": float(ohlc.get("high", 0)),
                            "low": float(ohlc.get("low", 0)),
                            "close": float(ohlc.get("close", 0))
                        })
                        data_received = True
                
                elif data.get("error"):
                    if DEBUG:
                        print(f"API Error: {data.get('error')}")
                    result_queue.put("ERROR")
                
            except Exception as e:
                if DEBUG:
                    print(f"Error processing message: {e}")
                result_queue.put("ERROR")
        
        def on_error(ws, error):
            if DEBUG:
                print(f"WebSocket error: {error}")
            result_queue.put("ERROR")
        
        def on_close(ws, close_status_code, close_msg):
            if DEBUG:
                print(f"WebSocket connection closed: {close_status_code}, {close_msg}")
        
        def on_open(ws):
            nonlocal connection_established
            connection_established = True
            if DEBUG:
                print(f"WebSocket connected, requesting candles for {symbol}")
            
            try:
                # Request historical candles
                request = {
                    "ticks_history": symbol,
                    "adjust_start_time": 1,
                    "count": count,
                    "end": "latest",
                    "start": 1,
                    "style": "candles",
                    "granularity": timeframe
                }
                ws.send(json.dumps(request))
                if DEBUG:
                    print(f"Sent request: {request}")
            except Exception as e:
                if DEBUG:
                    print(f"Error sending request: {e}")
                result_queue.put("ERROR")
        
        # Create WebSocket connection
        ws = websocket.WebSocketApp(DERIV_WS_URL,
                                  on_open=on_open,
                                  on_message=on_message,
                                  on_error=on_error,
                                  on_close=on_close)
        
        # Run WebSocket in a separate thread
        def run_ws():
            try:
                ws.run_forever()
            except Exception as e:
                if DEBUG:
                    print(f"WebSocket run_forever error: {e}")
                result_queue.put("ERROR")
        
        ws_thread = threading.Thread(target=run_ws, daemon=True)
        ws_thread.start()
        
        # Wait for result with timeout
        try:
            result = result_queue.get(timeout=10)  # 10 second timeout
            if result == "SUCCESS":
                if DEBUG:
                    print(f"Successfully received data for {symbol}")
            else:
                if DEBUG:
                    print(f"Failed to get data for {symbol}, trying HTTP fallback")
                candles = fetch_candles_http_fallback(symbol, timeframe, count)
        except queue.Empty:
            if DEBUG:
                print(f"Timeout waiting for data from {symbol}, trying HTTP fallback")
            candles = fetch_candles_http_fallback(symbol, timeframe, count)
        
        # Give a bit more time for data to arrive if we got some via WebSocket
        if data_received:
            time.sleep(1)
        
    except Exception as e:
        if DEBUG:
            print(f"Error fetching candles for {symbol}: {e}")
            traceback.print_exc()
        # Try HTTP fallback
        candles = fetch_candles_http_fallback(symbol, timeframe, count)
    finally:
        if ws:
            ws.close()
    
    # Sort candles by epoch
    candles.sort(key=lambda x: x["epoch"])
    
    if DEBUG:
        print(f"Final result: Fetched {len(candles)} candles for {symbol}")
    
    return candles

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
# Moving Averages - Exact Implementation
# -------------------------
def smma_correct(series, period):
    """Proper SMMA calculation as per strategy"""
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
    """Standard SMA calculation for MA3"""
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
    """Compute MAs exactly as per strategy documentation"""
    closes = [c["close"] for c in candles]
    hlc3 = [(c["high"] + c["low"] + c["close"]) / 3.0 for c in candles]
    
    # MA1 → SMMA of HLC3, period 9 (fastest MA, closest to price)
    ma1 = smma_correct(hlc3, 9)
    
    # MA2 → SMMA of Close, period 19 (backup dynamic support/resistance)
    ma2 = smma_correct(closes, 19)
    
    # MA3 → SMA of MA2, period 25 (trend filter)
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
# Market Condition Analysis
# -------------------------
def is_trending_market(ma1, ma2, ma3, lookback=10):
    """Check if market is trending (MAs smoothly dispensed, no interceptions)"""
    if not ma1 or not ma2 or not ma3:
        return False, "INSUFFICIENT_DATA"
    
    # Check last 'lookback' periods for MA arrangement and smoothness
    n = len(ma1)
    start_idx = max(0, n - lookback)
    
    # Count valid MAs in the lookback period
    valid_count = 0
    ma_crossings = 0
    horizontal_count = 0
    
    for i in range(start_idx, n):
        if ma1[i] is None or ma2[i] is None or ma3[i] is None:
            continue
        
        valid_count += 1
        
        # Check for MA crossings (interceptions)
        if i > start_idx:
            prev_i = i - 1
            if (ma1[prev_i] is not None and ma2[prev_i] is not None and 
                ma1[i] is not None and ma2[i] is not None):
                
                # MA1 and MA2 crossing
                if ((ma1[prev_i] > ma2[prev_i] and ma1[i] < ma2[i]) or 
                    (ma1[prev_i] < ma2[prev_i] and ma1[i] > ma2[i])):
                    ma_crossings += 1
                
                # Check for horizontal packing (MAs too close)
                ma1_diff = abs(ma1[i] - ma1[prev_i])
                ma2_diff = abs(ma2[i] - ma2[prev_i])
                avg_price = (ma1[i] + ma2[i]) / 2
                
                if (ma1_diff / avg_price < 0.0001 or ma2_diff / avg_price < 0.0001):
                    horizontal_count += 1
    
    if valid_count < 5:  # Need sufficient data
        return False, "INSUFFICIENT_VALID_DATA"
    
    # Too many crossings indicate ranging market
    if ma_crossings > 2:
        return False, "RANGING_CROSSINGS"
    
    # Too much horizontal movement indicates consolidation
    if horizontal_count > valid_count * 0.6:
        return False, "HORIZONTAL_PACKING"
    
    return True, "TRENDING"

def get_market_bias(ma1_val, ma2_val, ma3_val, price):
    """Determine market bias based on MA arrangement and price position"""
    if any(v is None for v in [ma1_val, ma2_val, ma3_val]):
        return "UNDEFINED"
    
    # Check MA arrangement for trend direction
    if ma1_val > ma2_val > ma3_val:
        # Uptrend arrangement, check price relative to MA3
        if price > ma3_val:
            return "BUY_BIAS"
    elif ma1_val < ma2_val < ma3_val:
        # Downtrend arrangement, check price relative to MA3  
        if price < ma3_val:
            return "SELL_BIAS"
    
    return "UNCLEAR"

# -------------------------
# Candlestick Pattern Recognition
# -------------------------
def is_rejection_candle(candle, prev_candle=None):
    """Identify rejection candles (pin bar, doji, engulfing patterns)"""
    o, h, l, c = candle["open"], candle["high"], candle["low"], candle["close"]
    body_size = abs(c - o)
    total_range = h - l
    
    if total_range <= 0:
        return False, "NONE"
    
    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - l
    
    # Pin Bar Detection
    if upper_wick >= body_size * 1.2 and upper_wick >= total_range * 0.5:
        return True, "PIN_BAR_BEARISH"
    
    if lower_wick >= body_size * 1.2 and lower_wick >= total_range * 0.5:
        return True, "PIN_BAR_BULLISH"
    
    # Doji Detection (small body relative to range)
    if body_size <= total_range * 0.1 and total_range > 0:
        return True, "DOJI"
    
    # Engulfing Pattern Detection (requires previous candle)
    if prev_candle:
        prev_o, prev_c = prev_candle["open"], prev_candle["close"]
        prev_body = abs(prev_c - prev_o)
        
        # Bullish engulfing
        if (c > o and prev_c < prev_o and  # Current bullish, previous bearish
            o <= prev_c and c >= prev_o and  # Engulfing body
            body_size > prev_body * 1.1):  # Significantly larger body
            return True, "ENGULFING_BULLISH"
        
        # Bearish engulfing  
        if (c < o and prev_c > prev_o and  # Current bearish, previous bullish
            o >= prev_c and c <= prev_o and  # Engulfing body
            body_size > prev_body * 1.1):  # Significantly larger body
            return True, "ENGULFING_BEARISH"
    
    return False, "NONE"

def is_confirmation_candle(candle, expected_direction):
    """Check if candle confirms the signal direction"""
    o, c = candle["open"], candle["close"]
    
    if expected_direction == "BUY":
        return c > o  # Bullish candle (close above open)
    elif expected_direction == "SELL":
        return c < o  # Bearish candle (close below open)
    
    return False

# -------------------------
# Signal Detection Logic
# -------------------------
def price_near_ma(price, ma_level, tolerance_pct=0.15):
    """Check if price is near MA level within tolerance"""
    if ma_level is None:
        return False
    return abs(price - ma_level) / ma_level <= (tolerance_pct / 100.0)

def detect_signal(candles, tf, shorthand):
    """Detect DSR signals according to exact strategy rules"""
    n = len(candles)
    if n < MIN_CANDLES:
        return None
    
    # Need at least 2 candles (rejection + confirmation)
    if n < 2:
        return None
    
    confirmation_idx = n - 1  # Current (just closed) candle
    rejection_idx = n - 2     # Previous candle
    
    confirmation_candle = candles[confirmation_idx]
    rejection_candle = candles[rejection_idx]
    prev_to_rejection = candles[rejection_idx - 1] if rejection_idx > 0 else None
    
    # Compute moving averages
    ma1, ma2, ma3 = compute_mas(candles)
    
    # Check if we have valid MA values at confirmation candle
    conf_ma1 = ma1[confirmation_idx] if confirmation_idx < len(ma1) else None
    conf_ma2 = ma2[confirmation_idx] if confirmation_idx < len(ma2) else None
    conf_ma3 = ma3[confirmation_idx] if confirmation_idx < len(ma3) else None
    
    if any(v is None for v in [conf_ma1, conf_ma2, conf_ma3]):
        return None
    
    # 1. Check if market is trending (not ranging/consolidating)
    is_trending, trend_reason = is_trending_market(ma1, ma2, ma3)
    if not is_trending:
        if DEBUG:
            print(f"Market not trending: {trend_reason}")
        return None
    
    # 2. Determine market bias
    current_price = confirmation_candle["close"]
    market_bias = get_market_bias(conf_ma1, conf_ma2, conf_ma3, current_price)
    
    if market_bias not in ["BUY_BIAS", "SELL_BIAS"]:
        if DEBUG:
            print(f"Market bias unclear: {market_bias}")
        return None
    
    # 3. Check for rejection candle at MA levels
    is_rejection, rejection_type = is_rejection_candle(rejection_candle, prev_to_rejection)
    if not is_rejection:
        return None
    
    # 4. Check if rejection occurred near MA1 or MA2
    rej_ma1 = ma1[rejection_idx] if rejection_idx < len(ma1) else None
    rej_ma2 = ma2[rejection_idx] if rejection_idx < len(ma2) else None
    
    if rej_ma1 is None or rej_ma2 is None:
        return None
    
    rejection_high = rejection_candle["high"]
    rejection_low = rejection_candle["low"]
    
    near_ma1_high = price_near_ma(rejection_high, rej_ma1)
    near_ma1_low = price_near_ma(rejection_low, rej_ma1)
    near_ma2_high = price_near_ma(rejection_high, rej_ma2)
    near_ma2_low = price_near_ma(rejection_low, rej_ma2)
    
    signal_side = None
    
    # 5. Signal logic based on bias and rejection pattern
    if market_bias == "BUY_BIAS":
        # Look for bullish rejection patterns at MA support levels
        if rejection_type in ["PIN_BAR_BULLISH", "DOJI", "ENGULFING_BULLISH"]:
            if near_ma1_low or near_ma2_low:  # Rejection from support
                # Check confirmation candle
                if is_confirmation_candle(confirmation_candle, "BUY"):
                    signal_side = "BUY"
    
    elif market_bias == "SELL_BIAS":
        # Look for bearish rejection patterns at MA resistance levels  
        if rejection_type in ["PIN_BAR_BEARISH", "DOJI", "ENGULFING_BEARISH"]:
            if near_ma1_high or near_ma2_high:  # Rejection from resistance
                # Check confirmation candle
                if is_confirmation_candle(confirmation_candle, "SELL"):
                    signal_side = "SELL"
    
    if not signal_side:
        return None
    
    # 6. Final validation - ensure signal aligns with trend direction
    if signal_side == "BUY" and market_bias != "BUY_BIAS":
        return None
    if signal_side == "SELL" and market_bias != "SELL_BIAS":
        return None
    
    return {
        "symbol": shorthand,
        "tf": tf,
        "side": signal_side,
        "rejection_pattern": rejection_type,
        "market_bias": market_bias,
        "price": confirmation_candle["close"],
        "rejection_idx": rejection_idx,
        "confirmation_idx": confirmation_idx,
        "candles": candles,
        "ma1": conf_ma1,
        "ma2": conf_ma2,
        "ma3": conf_ma3,
        "trend_status": trend_reason
    }

# -------------------------
# Chart Generation
# -------------------------
def create_signal_chart(signal_data):
    """Create detailed chart showing the DSR signal"""
    candles = signal_data["candles"]
    rejection_idx = signal_data["rejection_idx"] 
    confirmation_idx = signal_data["confirmation_idx"]
    
    # Recompute MAs for chart
    ma1, ma2, ma3 = compute_mas(candles)
    
    n = len(candles)
    chart_start = max(0, n - LAST_N_CHART)
    chart_candles = candles[chart_start:]
    chart_ma1 = ma1[chart_start:] if ma1 else [None] * len(chart_candles)
    chart_ma2 = ma2[chart_start:] if ma2 else [None] * len(chart_candles)
    chart_ma3 = ma3[chart_start:] if ma3 else [None] * len(chart_candles)

    plt.style.use('dark_background')
    fig, ax = plt.subplots(figsize=(16, 10))
    fig.patch.set_facecolor('black')
    ax.set_facecolor('black')
    
    # Plot candlesticks
    for i, candle in enumerate(chart_candles):
        o, h, l, c = candle["open"], candle["high"], candle["low"], candle["close"]
        
        # Highlight rejection and confirmation candles
        chart_idx = chart_start + i
        if chart_idx == rejection_idx:
            body_color = "#FFFF00"  # Yellow for rejection candle
            edge_color = "#FFAA00"
        elif chart_idx == confirmation_idx:
            body_color = "#00FFFF"  # Cyan for confirmation candle
            edge_color = "#00AAFF"
        elif c >= o:
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
            linewidth=1.5 if chart_idx in [rejection_idx, confirmation_idx] else 1
        ))
        
        ax.plot([i, i], [l, h], color=edge_color, linewidth=1.5, alpha=0.9)
    
    # Plot moving averages
    ma1_valid = [(i, v) for i, v in enumerate(chart_ma1) if v is not None]
    ma2_valid = [(i, v) for i, v in enumerate(chart_ma2) if v is not None] 
    ma3_valid = [(i, v) for i, v in enumerate(chart_ma3) if v is not None]
    
    if ma1_valid:
        x_vals, y_vals = zip(*ma1_valid)
        ax.plot(x_vals, y_vals, color="#FFFFFF", linewidth=2.5, label="MA1 (SMMA HLC3-9)", alpha=0.9)
    
    if ma2_valid:
        x_vals, y_vals = zip(*ma2_valid)
        ax.plot(x_vals, y_vals, color="#00BFFF", linewidth=2.5, label="MA2 (SMMA Close-19)", alpha=0.9)
    
    if ma3_valid:
        x_vals, y_vals = zip(*ma3_valid)  
        ax.plot(x_vals, y_vals, color="#FF6347", linewidth=2, label="MA3 (SMA MA2-25)", alpha=0.8)

    # Mark rejection and confirmation candles
    rejection_chart_idx = rejection_idx - chart_start
    confirmation_chart_idx = confirmation_idx - chart_start
    
    if 0 <= rejection_chart_idx < len(chart_candles):
        rejection_price = chart_candles[rejection_chart_idx]["close"]
        ax.scatter([rejection_chart_idx], [rejection_price], 
                  color="#FFFF00", marker="*", s=400, 
                  edgecolor="#FFFFFF", linewidth=2, zorder=10,
                  label="Rejection Candle")
    
    if 0 <= confirmation_chart_idx < len(chart_candles):
        confirmation_price = chart_candles[confirmation_chart_idx]["close"]
        if signal_data["side"] == "BUY":
            marker_color = "#00FF00"
            marker_symbol = "^"
        else:
            marker_color = "#FF0000"
            marker_symbol = "v"
        
        ax.scatter([confirmation_chart_idx], [confirmation_price], 
                  color=marker_color, marker=marker_symbol, s=400,
                  edgecolor="#FFFFFF", linewidth=2, zorder=10,
                  label="Confirmation Candle")
    
    # Enhanced title with more details
    bias_emoji = "📈" if signal_data["market_bias"] == "BUY_BIAS" else "📉"
    tf = signal_data["tf"]
    title = (f"{signal_data['symbol']} {tf}s - {signal_data['side']} DSR Signal {bias_emoji}\n"
             f"Pattern: {signal_data['rejection_pattern']} → Confirmation")
    
    ax.set_title(title, fontsize=14, color='white', fontweight='bold', pad=20)
    
    legend = ax.legend(loc="upper left", frameon=True, facecolor='black', 
                      edgecolor='white', fontsize=10)
    legend.get_frame().set_alpha(0.8)
    
    ax.grid(True, alpha=0.3, color='gray', linestyle='--', linewidth=0.5)
    ax.tick_params(colors='white', labelsize=9)
    
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
    
    return chart_file.name

# -------------------------
# Main Execution
# -------------------------
def run_analysis():
    """Run complete DSR analysis with proper signal detection"""
    signals_found = 0
    
    for shorthand, deriv_symbol in SYMBOL_MAP.items():
        try:
            tf = 300  # 5-minute timeframe
            
            if DEBUG:
                print(f"Analyzing {shorthand} on {tf}s...")
            
            candles = fetch_candles(deriv_symbol, tf)
            if len(candles) < MIN_CANDLES:
                if DEBUG:
                    print(f"Insufficient candles for {shorthand}: {len(candles)}")
                continue
            
            signal = detect_signal(candles, tf, shorthand)
            if not signal:
                if DEBUG:
                    print(f"No signal detected for {shorthand}")
                continue
            
            # Use confirmation candle epoch for deduplication
            confirmation_epoch = signal["candles"][signal["confirmation_idx"]]["epoch"]
            if already_sent(shorthand, tf, confirmation_epoch, signal["side"]):
                if DEBUG:
                    print(f"Signal already sent for {shorthand}")
                continue
            
            # Create detailed alert message
            bias_emoji = "📈" if signal["market_bias"] == "BUY_BIAS" else "📉"
            
            caption = (
                f"🎯 {signal['symbol']} {tf}s - {signal['side']} DSR SIGNAL\n"
                f"{bias_emoji} Market Bias: {signal['market_bias'].replace('_', ' ')}\n"
                f"🎨 Rejection Pattern: {signal['rejection_pattern']}\n" 
                f"✅ Confirmation: Candle Closed {signal['side'].lower()}\n"
                f"💰 Entry Price: {signal['price']:.5f}\n"
                f"📊 Trend Status: {signal['trend_status']}\n"
                f"⚡ Signal Quality: HIGH (Rejection + Confirmation)"
            )
            
            chart_path = create_signal_chart(signal)
            
            success, msg_id = send_telegram_photo(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, caption, chart_path)
            
            if success:
                mark_sent(shorthand, tf, confirmation_epoch, signal["side"])
                signals_found += 1
                if DEBUG:
                    print(f"DSR signal sent for {shorthand}: {signal['side']}")
                    print(f"Rejection: {signal['rejection_pattern']}")
                    print(f"Market Bias: {signal['market_bias']}")
            
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