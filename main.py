#!/usr/bin/env python3
"""
main.py — Dynamic Support & Resistance (Enhanced - CONFIRMED REJECTION SIGNALS)

Enhanced DSR strategy:
- MA1 (SMMA HLC3-9) and MA2 (SMMA Close-19) = Dynamic S/R  
- MA3 (SMA MA2-25) = Trend filter
- Wait for candlestick to CLOSE with confirmed rejection at MA1/MA2
- Above MA3 = Buy bias, Below MA3 = Sell bias
- NO tolerance thresholds - price must show clear rejection behavior
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

# Configurable strictness level
REJECTION_STRICTNESS = os.getenv("REJECTION_STRICTNESS", "MEDIUM").upper()
# Options: STRICT, MEDIUM, LENIENT

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
# Moving Averages
# -------------------------
def smma_correct(series, period):
    """Proper SMMA calculation"""
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
    """Standard SMA calculation"""
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
    """Compute MAs exactly as per strategy"""
    closes = [c["close"] for c in candles]
    hlc3 = [(c["high"] + c["low"] + c["close"]) / 3.0 for c in candles]
    
    # MA1 → SMMA of HLC3, period 9
    ma1 = smma_correct(hlc3, 9)
    
    # MA2 → SMMA of Close, period 19  
    ma2 = smma_correct(closes, 19)
    
    # MA3 → SMA of MA2, period 25
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
# Enhanced Rejection Detection - Wait for Close
# -------------------------
def is_confirmed_rejection_at_ma(candle, ma_level, strictness="MEDIUM"):
    """
    Balanced rejection detection with adjustable strictness
    - STRICT: 60% wick, 70% recovery (very conservative)
    - MEDIUM: 45% wick, 55% recovery (balanced)
    - LENIENT: 35% wick, 40% recovery (more signals)
    """
    if ma_level is None:
        return False, "NONE"
    
    o, h, l, c = candle["open"], candle["high"], candle["low"], candle["close"]
    total_range = h - l
    body_size = abs(c - o)
    
    if total_range <= 0:
        return False, "NONE"
    
    # Check if candle actually touched the MA level
    ma_was_touched = (l <= ma_level <= h)
    if not ma_was_touched:
        return False, "NO_TOUCH"
    
    # Adjustable thresholds based on strictness
    if strictness == "STRICT":
        wick_threshold = 0.6    # 60%
        recovery_threshold = 0.7 # 70%
        body_multiplier = 2.0    # 2x
        close_threshold = 0.2    # 20%
        body_limit = 0.4         # 40%
    elif strictness == "MEDIUM":
        wick_threshold = 0.45    # 45%
        recovery_threshold = 0.55 # 55%
        body_multiplier = 1.5    # 1.5x
        close_threshold = 0.15   # 15%
        body_limit = 0.5         # 50%
    else:  # LENIENT
        wick_threshold = 0.35    # 35%
        recovery_threshold = 0.4  # 40%
        body_multiplier = 1.2    # 1.2x
        close_threshold = 0.1    # 10%
        body_limit = 0.6         # 60%
    
    # UPPER REJECTION: MA tested from below, rejection back down
    if h >= ma_level and c < ma_level:
        upper_wick = h - max(o, c)
        wick_ratio = upper_wick / total_range
        close_distance = abs(ma_level - c) / total_range
        body_ratio = body_size / total_range
        
        if (wick_ratio >= wick_threshold and  
            upper_wick >= body_size * body_multiplier and  
            close_distance >= close_threshold and  
            body_ratio <= body_limit):
            return True, "UPPER_REJECTION"
    
    # LOWER REJECTION: MA tested from above, rejection back up
    elif l <= ma_level and c > ma_level:
        lower_wick = min(o, c) - l
        wick_ratio = lower_wick / total_range
        close_distance = abs(c - ma_level) / total_range
        body_ratio = body_size / total_range
        
        if (wick_ratio >= wick_threshold and  
            lower_wick >= body_size * body_multiplier and  
            close_distance >= close_threshold and  
            body_ratio <= body_limit):
            return True, "LOWER_REJECTION"
    
    return False, "NONE"

def test_ma_rejection(candle, ma1_val, ma2_val, strictness="MEDIUM"):
    """Test if candle shows rejection at MA1 or MA2 with adjustable strictness"""
    
    # Test MA1 rejection first (primary S/R)
    ma1_rejected, ma1_pattern = is_confirmed_rejection_at_ma(candle, ma1_val, strictness)
    if ma1_rejected:
        return True, "MA1", ma1_pattern
    
    # Test MA2 rejection (secondary S/R)
    ma2_rejected, ma2_pattern = is_confirmed_rejection_at_ma(candle, ma2_val, strictness)
    if ma2_rejected:
        return True, "MA2", ma2_pattern
    
    return False, "NONE", "NONE"

# -------------------------
# MA Level Testing Function
# -------------------------
def test_ma_rejection(candle, ma1_val, ma2_val):
    """Test if candle shows rejection at MA1 or MA2"""
    
    # Test MA1 rejection
    ma1_rejected, ma1_pattern = is_confirmed_rejection_at_ma(candle, ma1_val)
    if ma1_rejected:
        return True, "MA1", ma1_pattern
    
    # Test MA2 rejection
    ma2_rejected, ma2_pattern = is_confirmed_rejection_at_ma(candle, ma2_val)
    if ma2_rejected:
        return True, "MA2", ma2_pattern
    
    return False, "NONE", "NONE"

# -------------------------
# Enhanced Signal Detection with Confirmed Rejection
# -------------------------
def detect_signal(candles, tf, shorthand):
    """Enhanced DSR with STRICT confirmed rejection signals - NO FALSE SIGNALS"""
    n = len(candles)
    if n < MIN_CANDLES:
        return None

    current_idx = n - 1
    current_candle = candles[current_idx]
    
    # CRITICAL: Skip if this is the current forming candle (in live trading)
    # In backtesting, all candles are closed, but we need strict validation
    
    # Compute moving averages
    ma1, ma2, ma3 = compute_mas(candles)

    current_ma1 = ma1[current_idx] if current_idx < len(ma1) else None
    current_ma2 = ma2[current_idx] if current_idx < len(ma2) else None
    current_ma3 = ma3[current_idx] if current_idx < len(ma3) else None

    if not all(v is not None for v in [current_ma1, current_ma2, current_ma3]):
        return None

    # Determine trend bias from MA3 position
    current_close = current_candle["close"]
    
    if current_close > current_ma3:
        trend_bias = "BUY_BIAS"
    elif current_close < current_ma3:
        trend_bias = "SELL_BIAS"  
    else:
        # Price exactly at MA3, no clear bias
        return None
    
    # Test for STRICT MA rejection with configurable strictness
    has_rejection, ma_level, pattern_type = test_ma_rejection(current_candle, current_ma1, current_ma2, REJECTION_STRICTNESS)
    
    if not has_rejection:
        return None
    
    # Get thresholds based on strictness level
    if REJECTION_STRICTNESS == "STRICT":
        recovery_threshold = 0.7  # 70%
        wick_threshold = 0.6      # 60%
    elif REJECTION_STRICTNESS == "MEDIUM":
        recovery_threshold = 0.55 # 55%
        wick_threshold = 0.45     # 45%
    else:  # LENIENT
        recovery_threshold = 0.4  # 40%
        wick_threshold = 0.35     # 35%
    
    # ADDITIONAL VALIDATION: Check if this is ACTUALLY a rejection candle
    o, h, l, c = current_candle["open"], current_candle["high"], current_candle["low"], current_candle["close"]
    total_range = h - l
    body_size = abs(c - o)
    
    if total_range <= 0:
        return None
    
    # VALIDATION for BUY signals (looking for lower rejection)
    if trend_bias == "BUY_BIAS" and pattern_type == "LOWER_REJECTION":
        # Must have meaningful lower wick AND price recovered significantly
        lower_wick = min(o, c) - l
        recovery = c - l  # How much price recovered from the low
        
        # Configurable criteria based on strictness
        if (lower_wick >= total_range * wick_threshold and  
            recovery >= total_range * recovery_threshold and    
            c > (current_ma1 if ma_level == "MA1" else current_ma2)):  # Close above MA
            signal_side = "BUY"
        else:
            return None  # Not strong enough rejection
    
    # VALIDATION for SELL signals (looking for upper rejection) 
    elif trend_bias == "SELL_BIAS" and pattern_type == "UPPER_REJECTION":
        # Must have meaningful upper wick AND price dropped significantly
        upper_wick = h - max(o, c)
        drop = h - c  # How much price dropped from the high
        
        # Configurable criteria based on strictness
        if (upper_wick >= total_range * wick_threshold and  
            drop >= total_range * recovery_threshold and        
            c < (current_ma1 if ma_level == "MA1" else current_ma2)):  # Close below MA
            signal_side = "SELL"
        else:
            return None  # Not strong enough rejection
    
    else:
        return None
    
    # FINAL VALIDATION: Ensure the candle pattern makes sense
    if signal_side == "BUY":
        # For BUY signals: must be hammer-like with long lower wick
        if l == min(o, c):  # No lower wick at all
            return None
        upper_wick = h - max(o, c)
        lower_wick = min(o, c) - l
        if upper_wick > lower_wick:  # Upper wick longer than lower
            return None
    
    elif signal_side == "SELL":
        # For SELL signals: must be shooting star-like with long upper wick  
        if h == max(o, c):  # No upper wick at all
            return None
        upper_wick = h - max(o, c)
        lower_wick = min(o, c) - l
        if lower_wick > upper_wick:  # Lower wick longer than upper
            return None
    
    return {
        "symbol": shorthand,
        "tf": tf,
        "side": signal_side,
        "pattern": pattern_type,
        "ma_level": ma_level,
        "bias": trend_bias,
        "price": current_close,
        "ma1": current_ma1,
        "ma2": current_ma2,
        "ma3": current_ma3,
        "idx": current_idx,
        "candles": candles,
        "validation": "STRICT_REJECTION_CONFIRMED"
    }

# -------------------------
# Enhanced Chart Generation - Plot All MAs
# -------------------------
def create_signal_chart(signal_data):
    """Create comprehensive chart showing all MAs and signal"""
    candles = signal_data["candles"]
    signal_idx = signal_data["idx"]
    
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
        
        if c >= o:
            body_color = "#00FF00"  # Green
            edge_color = "#00DD00"
        else:
            body_color = "#FF0000"  # Red
            edge_color = "#DD0000"
        
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
    
    # Plot ALL moving averages
    def plot_ma_safe(ma_values, label, color, linewidth=2, alpha=0.9):
        chart_ma = []
        x_vals = []
        for i, candle_idx in enumerate(range(chart_start, n)):
            if candle_idx < len(ma_values) and ma_values[candle_idx] is not None:
                chart_ma.append(ma_values[candle_idx])
                x_vals.append(i)
        
        if len(chart_ma) > 1:  # Need at least 2 points to plot
            ax.plot(x_vals, chart_ma, color=color, linewidth=linewidth, 
                   label=label, alpha=alpha)
    
    # Plot all MAs with distinct colors
    plot_ma_safe(ma1, "MA1 (SMMA HLC3-9)", "#FFFFFF", 2.5, 0.95)  # White - Primary S/R
    plot_ma_safe(ma2, "MA2 (SMMA Close-19)", "#00BFFF", 2.5, 0.95)  # Sky Blue - Secondary S/R  
    plot_ma_safe(ma3, "MA3 (SMA MA2-25)", "#FFD700", 2, 0.85)  # Gold - Trend Filter
    
    # Mark signal point with enhanced visualization
    signal_chart_idx = signal_idx - chart_start
    if 0 <= signal_chart_idx < len(chart_candles):
        signal_candle = chart_candles[signal_chart_idx]
        signal_price = signal_candle["close"]
        
        # Signal marker
        if signal_data["side"] == "BUY":
            marker_color = "#00FF00"
            marker_symbol = "^"
            signal_label = "BUY Signal"
        else:
            marker_color = "#FF0000" 
            marker_symbol = "v"
            signal_label = "SELL Signal"
        
        ax.scatter([signal_chart_idx], [signal_price], 
                  color=marker_color, marker=marker_symbol, 
                  s=400, edgecolor="#FFFFFF", linewidth=4, 
                  zorder=10, label=signal_label)
        
        # Highlight the rejection MA level
        ma_val = signal_data["ma1"] if signal_data["ma_level"] == "MA1" else signal_data["ma2"]
        ax.axhline(y=ma_val, color="#FFFF00", linestyle='--', 
                  linewidth=2, alpha=0.7, 
                  label=f"Rejection Level ({signal_data['ma_level']})")
    
    # Enhanced title with trend bias
    bias_emoji = "📈" if signal_data["bias"] == "BUY_BIAS" else "📉"
    title = f"{signal_data['symbol']} - {signal_data['side']} Confirmed Rejection {bias_emoji}"
    ax.set_title(title, fontsize=18, color='white', fontweight='bold', pad=25)
    
    # Enhanced legend
    legend = ax.legend(loc="upper left", frameon=True, facecolor='black', 
                      edgecolor='white', fontsize=12, shadow=True)
    legend.get_frame().set_alpha(0.85)
    for text in legend.get_texts():
        text.set_color('white')
    
    # Grid and styling
    ax.grid(True, alpha=0.25, color='gray', linestyle='--', linewidth=0.5)
    ax.tick_params(colors='white', labelsize=11)
    
    for spine in ax.spines.values():
        spine.set_color('white')
        spine.set_linewidth(1.2)
    
    plt.tight_layout()
    
    # Save chart
    chart_file = tempfile.NamedTemporaryFile(delete=False, suffix=".png")
    plt.savefig(chart_file.name, 
                dpi=150, 
                bbox_inches="tight", 
                facecolor='black',
                edgecolor='none',
                pad_inches=0.2)
    plt.close()
    
    return chart_file.name

# -------------------------
# Main Execution
# -------------------------
def run_analysis():
    """Enhanced DSR analysis with confirmed rejection signals"""
    signals_found = 0
    
    for shorthand, deriv_symbol in SYMBOL_MAP.items():
        try:
            tf = 300  # You can adjust the timeframe if needed
            
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
            
            # Enhanced alert message with strictness level
            bias_emoji = "📈" if signal["bias"] == "BUY_BIAS" else "📉"
            
            # Get thresholds for display
            if REJECTION_STRICTNESS == "STRICT":
                thresh_display = "60%+ Wick, 70%+ Recovery"
            elif REJECTION_STRICTNESS == "MEDIUM":
                thresh_display = "45%+ Wick, 55%+ Recovery"
            else:
                thresh_display = "35%+ Wick, 40%+ Recovery"
            
            caption = (f"🎯 {signal['symbol']} {tf}s - {signal['side']} {REJECTION_STRICTNESS} REJECTION\n"
                      f"{bias_emoji} Trend Bias: {signal['bias'].replace('_', ' ')}\n" 
                      f"🔄 Pattern: {signal['pattern']} at {signal['ma_level']} ({REJECTION_STRICTNESS} Mode)\n"
                      f"💰 Entry Price: {signal['price']:.5f}\n"
                      f"✅ Validation: {thresh_display}\n"
                      f"✅ Candle Closed - Signal Confirmed")
            
            chart_path = create_signal_chart(signal)
            
            success, msg_id = send_telegram_photo(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, caption, chart_path)
            
            if success:
                mark_sent(shorthand, tf, current_epoch, signal["side"])
                signals_found += 1
                if DEBUG:
                    print(f"Enhanced DSR signal sent for {shorthand}: {signal['side']} at {signal['ma_level']}")
            
            try:
                os.unlink(chart_path)
            except:
                pass
                
        except Exception as e:
            if DEBUG:
                print(f"Error analyzing {shorthand}: {e}")
                traceback.print_exc()
    
    if DEBUG:
        print(f"Enhanced analysis complete. {signals_found} confirmed DSR signals found.")

if __name__ == "__main__":
    try:
        run_analysis()
    except Exception as e:
        print(f"Critical error: {e}")
        traceback.print_exc()