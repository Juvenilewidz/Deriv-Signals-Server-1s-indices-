#!/usr/bin/env python3
"""
main.py — Dynamic Support & Resistance (Simplified - NO MISSED SIGNALS)

Simple, effective DSR strategy:
- MA1 (SMMA HLC3-9) and MA2 (SMMA Close-19) = Dynamic S/R  
- MA3 (SMA MA2-25) = Trend filter
- Any rejection candle at MA1/MA2 = Signal
- Above MA3 = Buy bias, Below MA3 = Sell bias
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

# Alternative symbol formats to try if primary fails
SYMBOL_ALTERNATIVES = {
    "V75(1s)": ["1HZ75V", "R_75", "RDBULL", "frxVOL75"],
    "V100(1s)": ["1HZ100V", "R_100", "frxVOL100"], 
    "V150(1s)": ["1HZ150V", "R_150", "frxVOL150"],
}

# -------------------------
# WebSocket Data Fetching
# -------------------------
def try_symbol_alternatives(shorthand, timeframe, count=None):
    """Try different symbol formats until one works"""
    alternatives = SYMBOL_ALTERNATIVES.get(shorthand, [SYMBOL_MAP.get(shorthand)])
    
    for symbol in alternatives:
        if DEBUG:
            print(f"Trying symbol: {symbol} for {shorthand}")
        
        candles = fetch_candles_internal(symbol, timeframe, count)
        if len(candles) > 0:
            if DEBUG:
                print(f"Success with symbol: {symbol} for {shorthand}")
            return candles
        
        if DEBUG:
            print(f"No data with symbol: {symbol} for {shorthand}")
    
    if DEBUG:
        print(f"All symbol alternatives failed for {shorthand}")
    return []

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

def fetch_candles_internal(symbol, timeframe, count=None):
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

def fetch_candles(symbol_shorthand, timeframe, count=None):
    """Main function to fetch candles - tries alternatives if primary fails"""
    return try_symbol_alternatives(symbol_shorthand, timeframe, count)
def fetch_candles_internal(symbol, timeframe, count=None):
    """Internal function to fetch candlestick data from Deriv WebSocket API with HTTP fallback"""
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
                    print(f"Received message type: {data.get('msg_type')} for {symbol}")
                
                # Handle different response types
                if data.get("msg_type") == "candles":
                    candle_data = data.get("candles", [])
                    if DEBUG:
                        print(f"Received {len(candle_data)} candles for {symbol}")
                    
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
                        print(f"API Error for {symbol}: {data.get('error')}")
                    result_queue.put("ERROR")
                
            except Exception as e:
                if DEBUG:
                    print(f"Error processing message for {symbol}: {e}")
                result_queue.put("ERROR")
        
        def on_error(ws, error):
            if DEBUG:
                print(f"WebSocket error for {symbol}: {error}")
            result_queue.put("ERROR")
        
        def on_close(ws, close_status_code, close_msg):
            if DEBUG:
                print(f"WebSocket connection closed for {symbol}: {close_status_code}, {close_msg}")
        
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
                    print(f"Sent request for {symbol}: {request}")
            except Exception as e:
                if DEBUG:
                    print(f"Error sending request for {symbol}: {e}")
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
                ws.run_forever()  # Remove timeout parameter - not supported in all versions
            except Exception as e:
                if DEBUG:
                    print(f"WebSocket run_forever error for {symbol}: {e}")
                result_queue.put("ERROR")
        
        ws_thread = threading.Thread(target=run_ws, daemon=True)
        ws_thread.start()
        
        # Wait for result with timeout using queue instead
        try:
            result = result_queue.get(timeout=8)  # 8 second timeout for each attempt
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
        
        # Stop the WebSocket thread
        if ws:
            ws.close()
        
        # Give a bit more time for data to arrive if we got some via WebSocket
        if data_received:
            time.sleep(0.5)
        
    except Exception as e:
        if DEBUG:
            print(f"Error fetching candles for {symbol}: {e}")
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
# Helper Functions
# -------------------------
def near_ma_levels(price, ma1, ma2, tolerance=0.001):
    """Check if price is near MA levels"""
    if ma1 is None or ma2 is None:
        return False, False
    
    near_ma1 = abs(price - ma1) / ma1 <= tolerance
    near_ma2 = abs(price - ma2) / ma2 <= tolerance
    
    return near_ma1, near_ma2

# -------------------------
# Updated Simple Rejection Detection
# -------------------------
def is_rejection_candle(candle):
    """Check for rejection candles as per the specified family types."""
    o, h, l, c = candle["open"], candle["high"], candle["low"], candle["close"]
    body_size = abs(c - o)
    total_range = h - l

    if total_range <= 0:
        return False, "NONE"

    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - l

    has_upper_wick = upper_wick > 0
    has_lower_wick = lower_wick > 0
    has_small_body = body_size < total_range * 0.3  # Adjusted for tiny-body candles

    # Define rejection patterns
    if has_upper_wick and (upper_wick >= body_size * 0.5 or has_small_body):
        return True, "UPPER_REJECTION"
    if has_lower_wick and (lower_wick >= body_size * 0.5 or has_small_body):
        return True, "LOWER_REJECTION"
    if has_small_body and (has_upper_wick or has_lower_wick):
        return True, "SMALL_BODY_REJECTION"

    return False, "NONE"

# -------------------------
# Updated Trend Detection Logic
# -------------------------
def get_trend_bias(current_price, ma1_val, ma3_val):
    """Determine trend bias based on price vs MA3 (trend filter) and MA1 position."""
    if ma1_val is None or ma3_val is None or current_price is None:
        return "UNDEFINED"

    # MA3 determines overall bias, but price must be on correct side of MA1
    if current_price > ma3_val and current_price > ma1_val:
        return "BUY_BIAS"  # Above MA3 trend filter AND above MA1
    elif current_price < ma3_val and current_price < ma1_val:
        return "SELL_BIAS"  # Below MA3 trend filter AND below MA1
    else:
        return "BETWEEN_MAS"  # Price between MAs - no clear bias

# -------------------------
# Updated Signal Detection Logic
# -------------------------
def detect_signal(candles, tf, shorthand):
    """Detect signals based on updated strategy."""
    n = len(candles)
    if n < MIN_CANDLES:
        if DEBUG:
            print(f"Not enough candles for {shorthand}: {n} < {MIN_CANDLES}")
        return None

    current_idx = n - 1
    current_candle = candles[current_idx]

    # Compute moving averages
    ma1, ma2, ma3 = compute_mas(candles)

    current_ma1 = ma1[current_idx] if current_idx < len(ma1) else None
    current_ma2 = ma2[current_idx] if current_idx < len(ma2) else None
    current_ma3 = ma3[current_idx] if current_idx < len(ma3) else None

    if DEBUG:
        print(f"Signal analysis for {shorthand}:")
        print(f"  Current candle: O={current_candle['open']:.5f}, H={current_candle['high']:.5f}, L={current_candle['low']:.5f}, C={current_candle['close']:.5f}")
        print(f"  MA1 (SMMA HLC3-9): {current_ma1:.5f if current_ma1 is not None else 'None'}")
        print(f"  MA2 (SMMA Close-19): {current_ma2:.5f if current_ma2 is not None else 'None'}")
        print(f"  MA3 (SMA MA2-25): {current_ma3:.5f if current_ma3 is not None else 'None'}")

    trend_bias = get_trend_bias(current_candle["close"], current_ma1, current_ma3)
    if DEBUG:
        print(f"  Trend bias: {trend_bias} (Price vs MA3 and MA1 position)")

    # Check for rejection candle
    is_rejection, pattern_type = is_rejection_candle(current_candle)
    if DEBUG:
        print(f"  Rejection candle: {is_rejection}, Pattern: {pattern_type}")

    # DSR Rules: Price must be on correct side of MA1, not between MAs
    if trend_bias == "BUY_BIAS":
        # Buy bias: Price > MA3 AND Price > MA1 (above dynamic S/R)
        near_ma1_low, near_ma2_low = near_ma_levels(current_candle["low"], current_ma1, current_ma2)
        if DEBUG:
            print(f"  BUY conditions: Price above MA1 ✅, Near MA1/MA2: {near_ma1_low}/{near_ma2_low}")
        
        if is_rejection and (near_ma1_low or near_ma2_low) and pattern_type in ["LOWER_REJECTION", "SMALL_BODY_REJECTION"]:
            signal_side = "BUY"
            if DEBUG:
                print(f"  ✅ BUY signal detected!")
        else:
            if DEBUG:
                print(f"  ❌ No BUY signal: rejection={is_rejection}, near_ma={near_ma1_low or near_ma2_low}, pattern={pattern_type}")
            return None

    elif trend_bias == "SELL_BIAS":
        # Sell bias: Price < MA3 AND Price < MA1 (below dynamic S/R)
        near_ma1_high, near_ma2_high = near_ma_levels(current_candle["high"], current_ma1, current_ma2)
        if DEBUG:
            print(f"  SELL conditions: Price below MA1 ✅, Near MA1/MA2: {near_ma1_high}/{near_ma2_high}")
        
        if is_rejection and (near_ma1_high or near_ma2_high) and pattern_type in ["UPPER_REJECTION", "SMALL_BODY_REJECTION"]:
            signal_side = "SELL"
            if DEBUG:
                print(f"  ✅ SELL signal detected!")
        else:
            if DEBUG:
                print(f"  ❌ No SELL signal: rejection={is_rejection}, near_ma={near_ma1_high or near_ma2_high}, pattern={pattern_type}")
            return None

    else:
        if DEBUG:
            if trend_bias == "BETWEEN_MAS":
                print(f"  ❌ Price between MAs - no valid setup")
            else:
                print(f"  ❌ No clear trend bias: {trend_bias}")
        return None

    return {
        "symbol": shorthand,
        "tf": tf,
        "side": signal_side,
        "pattern": pattern_type,
        "bias": trend_bias,
        "price": current_candle["close"],
        "idx": current_idx,
        "candles": candles,
        "ma1": current_ma1,
        "ma2": current_ma2,
        "ma3": current_ma3,
    }

# -------------------------
# Chart Generation
# -------------------------
def create_signal_chart(signal_data):
    """Create chart for signal visualization"""
    candles = signal_data["candles"]
    signal_idx = signal_data["idx"]
    
    # Recompute MAs for chart
    ma1, ma2, ma3 = compute_mas(candles)
    
    n = len(candles)
    chart_start = max(0, n - LAST_N_CHART)
    chart_candles = candles[chart_start:]
    chart_ma1 = ma1[chart_start:] if ma1 else [None] * len(chart_candles)
    chart_ma2 = ma2[chart_start:] if ma2 else [None] * len(chart_candles)

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
    
    # Plot moving averages (filter out None values)
    chart_ma3 = ma3[chart_start:] if ma3 else [None] * len(chart_candles)
    
    ma1_valid = [(i, v) for i, v in enumerate(chart_ma1) if v is not None]
    ma2_valid = [(i, v) for i, v in enumerate(chart_ma2) if v is not None]
    ma3_valid = [(i, v) for i, v in enumerate(chart_ma3) if v is not None]
    
    if ma1_valid:
        x_vals, y_vals = zip(*ma1_valid)
        ax.plot(x_vals, y_vals, color="#FFFFFF", linewidth=2, label="MA1 (SMMA HLC3-9)", alpha=0.9)
    
    if ma2_valid:
        x_vals, y_vals = zip(*ma2_valid)
        ax.plot(x_vals, y_vals, color="#00BFFF", linewidth=2, label="MA2 (SMMA Close-19)", alpha=0.9)
    
    if ma3_valid:
        x_vals, y_vals = zip(*ma3_valid)
        ax.plot(x_vals, y_vals, color="#FF6600", linewidth=2, label="MA3 (SMA MA2-25)", alpha=0.9)

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
    
    # Title
    bias_emoji = "📈" if signal_data["bias"] == "BUY_BIAS" else "📉"
    ax.set_title(f"{signal_data['symbol']} - {signal_data['side']} DSR Signal {bias_emoji}", 
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
    
    return chart_file.name

# -------------------------
# Main Execution
# -------------------------
def run_analysis():
    """Simple analysis - catch all valid DSR setups"""
    signals_found = 0
    
    for shorthand, deriv_symbol in SYMBOL_MAP.items():
        try:
            tf = 300  # You can adjust the timeframe if needed
            
            if DEBUG:
                print(f"Analyzing {shorthand} on {tf}s...")
            
            candles = fetch_candles(shorthand, tf)
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
            
            # Create alert message
            bias_emoji = "📈" if signal["bias"] == "BUY_BIAS" else "📉"
            caption = (f"🎯 {signal['symbol']} {tf}s - {signal['side']} SIGNAL\n"
                      f"{bias_emoji} Bias: {signal['bias'].replace('_', ' ')}\n" 
                      f"🎨 Pattern: {signal['pattern']}\n"
                      f"💰 Price: {signal['price']:.5f}")
            
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
