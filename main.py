#!/usr/bin/env python3
"""
Pure DSR Trading Bot for 1s Indices - M10 Timeframe Only
Stripped back to core DSR logic with EFFECTIVE ranging market detection
"""

import os, json, time, tempfile, traceback, requests
from datetime import datetime, timezone
import websocket, matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle
import threading, queue

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

DEBUG = os.getenv("DEBUG","0") == "1"
TEST_MODE = os.getenv("TEST_MODE","0") == "1"

CANDLES_N = 480
LAST_N_CHART = 180
CANDLE_WIDTH = 0.35
TMPDIR = tempfile.gettempdir()
ALERT_FILE = os.path.join(TMPDIR, "dsr_last_sent_pure_1s.json")
MIN_CANDLES = 50

# M10 TIMEFRAME ONLY
TIMEFRAME = 600  # 10 minutes only

# -------------------------
# 1s Indices Symbol Mappings
# -------------------------
SYMBOL_MAP = {
    "V75(1s)": "1HZ75V",
    "V100(1s)": "1HZ100V", 
    "V150(1s)": "1HZ150V",
}

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

def already_sent(shorthand, epoch, side):
    if TEST_MODE:
        return False
    rec = load_persist().get(shorthand)
    return bool(rec and rec.get("epoch")==epoch and rec.get("side")==side)

def mark_sent(shorthand, epoch, side):
    d=load_persist(); d[shorthand]={"epoch":epoch,"side":side}; save_persist(d)

# -------------------------
# Enhanced Data Fetching with HTTP Fallback
# -------------------------
def fetch_candles_http_fallback(symbol, count=None):
    """Fallback HTTP method to fetch candles"""
    if count is None:
        count = CANDLES_N
    
    try:
        url = "https://api.deriv.com/api/v1/ticks_history"
        params = {
            "ticks_history": symbol,
            "adjust_start_time": 1,
            "count": count,
            "end": "latest",
            "start": 1,
            "style": "candles",
            "granularity": TIMEFRAME
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

def fetch_candles(symbol, count=None):
    """Enhanced fetch with WebSocket and HTTP fallback for 1s indices"""
    if count is None:
        count = CANDLES_N
    
    candles = []
    ws = None
    connection_established = False
    data_received = False
    
    try:
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
                    "granularity": TIMEFRAME
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
            result = result_queue.get(timeout=10)
            if result == "SUCCESS":
                if DEBUG:
                    print(f"Successfully received data for {symbol}")
            else:
                if DEBUG:
                    print(f"Failed to get data for {symbol}, trying HTTP fallback")
                candles = fetch_candles_http_fallback(symbol, count)
        except queue.Empty:
            if DEBUG:
                print(f"Timeout waiting for data from {symbol}, trying HTTP fallback")
            candles = fetch_candles_http_fallback(symbol, count)
        
        # Give a bit more time for data to arrive if we got some via WebSocket
        if data_received:
            time.sleep(1)
        
    except Exception as e:
        if DEBUG:
            print(f"Error fetching candles for {symbol}: {e}")
            traceback.print_exc()
        # Try HTTP fallback
        candles = fetch_candles_http_fallback(symbol, count)
    finally:
        if ws:
            ws.close()
    
    # Sort candles by epoch
    candles.sort(key=lambda x: x["epoch"])
    
    if DEBUG:
        print(f"Final result: Fetched {len(candles)} candles for {symbol}")
    
    return candles

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
# EFFECTIVE Ranging Market Detection (Same as main bot)
# -------------------------
def is_ranging_market(candles, ma1, ma2, current_idx, lookback=20):
    """EFFECTIVE ranging market detection - multiple checks"""
    if current_idx < lookback:
        return False
    
    # Get recent data
    recent_candles = candles[current_idx - lookback:current_idx + 1]
    recent_ma1 = ma1[current_idx - lookback:current_idx + 1]
    recent_ma2 = ma2[current_idx - lookback:current_idx + 1]
    
    # Filter out None values
    valid_data = []
    for i in range(len(recent_candles)):
        if recent_ma1[i] is not None and recent_ma2[i] is not None:
            valid_data.append({
                'candle': recent_candles[i],
                'ma1': recent_ma1[i],
                'ma2': recent_ma2[i]
            })
    
    if len(valid_data) < lookback // 2:
        return True  # Not enough data = assume ranging
    
    # CHECK 1: MA Crossover Count (ranging = multiple crossovers)
    crossovers = 0
    for i in range(1, len(valid_data)):
        prev_ma1 = valid_data[i-1]['ma1']
        prev_ma2 = valid_data[i-1]['ma2']
        curr_ma1 = valid_data[i]['ma1']
        curr_ma2 = valid_data[i]['ma2']
        
        # Detect crossover
        if ((prev_ma1 <= prev_ma2 and curr_ma1 > curr_ma2) or
            (prev_ma1 >= prev_ma2 and curr_ma1 < curr_ma2)):
            crossovers += 1
    
    if crossovers >= 2:  # 2 or more crossovers = ranging
        if DEBUG:
            print(f"Ranging detected: {crossovers} MA crossovers")
        return True
    
    # CHECK 2: Price bouncing between MAs
    price_between_count = 0
    ma1_touches = 0
    ma2_touches = 0
    
    for data in valid_data:
        candle = data['candle']
        ma1_val = data['ma1']
        ma2_val = data['ma2']
        
        # Count when price is between MAs
        if ma1_val > ma2_val:  # Uptrend setup
            if ma2_val <= candle['close'] <= ma1_val:
                price_between_count += 1
        else:  # Downtrend setup
            if ma1_val <= candle['close'] <= ma2_val:
                price_between_count += 1
        
        # Count MA touches (ranging = frequent touches)
        ma1_touch_threshold = abs(ma1_val) * 0.002  # 0.2% threshold
        ma2_touch_threshold = abs(ma2_val) * 0.002  # 0.2% threshold
        
        if (abs(candle['high'] - ma1_val) <= ma1_touch_threshold or
            abs(candle['low'] - ma1_val) <= ma1_touch_threshold):
            ma1_touches += 1
            
        if (abs(candle['high'] - ma2_val) <= ma2_touch_threshold or
            abs(candle['low'] - ma2_val) <= ma2_touch_threshold):
            ma2_touches += 1
    
    # Too much time between MAs = ranging
    if price_between_count > len(valid_data) * 0.4:  # More than 40% of time
        if DEBUG:
            print(f"Ranging detected: Price between MAs {price_between_count}/{len(valid_data)} times")
        return True
    
    # Too many MA touches = ranging
    total_touches = ma1_touches + ma2_touches
    if total_touches > lookback * 0.6:  # More than 60% of candles touching MAs
        if DEBUG:
            print(f"Ranging detected: {total_touches} MA touches in {lookback} candles")
        return True
    
    # CHECK 3: MA convergence (MAs too close = ranging)
    ma_distances = []
    for data in valid_data:
        distance = abs(data['ma1'] - data['ma2']) / data['ma2']
        ma_distances.append(distance)
    
    avg_distance = sum(ma_distances) / len(ma_distances)
    if avg_distance < 0.008:  # Average distance less than 0.8% = ranging
        if DEBUG:
            print(f"Ranging detected: MA average distance {avg_distance:.4f}")
        return True
    
    # CHECK 4: Recent price range compression
    recent_highs = [d['candle']['high'] for d in valid_data[-10:]]  # Last 10 candles
    recent_lows = [d['candle']['low'] for d in valid_data[-10:]]
    recent_range = max(recent_highs) - min(recent_lows)
    
    # Compare to longer-term range
    all_highs = [d['candle']['high'] for d in valid_data]
    all_lows = [d['candle']['low'] for d in valid_data]
    total_range = max(all_highs) - min(all_lows)
    
    if total_range > 0 and recent_range / total_range < 0.3:  # Recent range < 30% of total
        if DEBUG:
            print(f"Ranging detected: Range compression {recent_range/total_range:.2f}")
        return True
    
    return False

# -------------------------
# Simple Rejection Detection (No Thresholds)
# -------------------------
def detect_rejection(candle):
    """Simple rejection detection - ANY rejection pattern"""
    o, h, l, c = candle["open"], candle["high"], candle["low"], candle["close"]
    body_size = abs(c - o)
    total_range = h - l
    
    if total_range <= 0:
        return False, "NONE"
    
    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - l
    
    # ANY wick = rejection
    if upper_wick > 0 and upper_wick >= lower_wick:
        return True, "UPPER_REJECTION"
    
    if lower_wick > 0 and lower_wick > upper_wick:
        return True, "LOWER_REJECTION"
    
    # Small body with any wick
    if body_size < total_range * 0.5 and (upper_wick > 0 or lower_wick > 0):
        if upper_wick >= lower_wick:
            return True, "SMALL_BODY_UPPER"
        else:
            return True, "SMALL_BODY_LOWER"
    
    return False, "NONE"

# -------------------------
# Dynamic MA Touch Detection
# -------------------------
def price_touches_ma(candle, ma_value):
    """Check if price touches MA level dynamically"""
    if ma_value is None:
        return False
    
    o, h, l, c = candle["open"], candle["high"], candle["low"], candle["close"]
    
    # Use candle range for touch threshold
    candle_range = h - l
    if candle_range <= 0:
        return False
    
    # Touch threshold = 15% of candle range
    threshold = candle_range * 0.15
    
    # Check if any part of candle is near MA
    distances = [abs(h - ma_value), abs(l - ma_value), abs(c - ma_value), abs(o - ma_value)]
    min_distance = min(distances)
    
    return min_distance <= threshold

# -------------------------
# PURE DSR Signal Detection for 1s Indices
# -------------------------
def detect_pure_dsr_signal(candles, shorthand):
    """PURE DSR Strategy for 1s indices - Core logic only with effective ranging filter"""
    n = len(candles)
    if n < MIN_CANDLES:
        return None
    
    current_idx = n - 1
    current_candle = candles[current_idx]
    
    # Compute moving averages
    ma1, ma2, ma3 = compute_mas(candles)
    
    current_ma1 = ma1[current_idx] if current_idx < len(ma1) else None
    current_ma2 = ma2[current_idx] if current_idx < len(ma2) else None
    current_ma3 = ma3[current_idx] if current_idx < len(ma3) else None
    
    if not all(v is not None for v in [current_ma1, current_ma2, current_ma3]):
        return None
    
    # CRITICAL: Check for ranging market FIRST
    if is_ranging_market(candles, ma1, ma2, current_idx):
        if DEBUG:
            print(f"Signal blocked: {shorthand} in ranging market")
        return None
    
    # DSR RULE 1: Determine bias from MA1/MA2 relationship
    if current_ma1 > current_ma2:
        bias = "BUY_BIAS"
    elif current_ma1 < current_ma2:
        bias = "SELL_BIAS"
    else:
        return None  # No clear bias
    
    # DSR RULE 2: Must have rejection pattern
    is_rejection, rejection_type = detect_rejection(current_candle)
    if not is_rejection:
        return None
    
    # DSR RULE 3: Price must touch MA1 or MA2
    touched_ma1 = price_touches_ma(current_candle, current_ma1)
    touched_ma2 = price_touches_ma(current_candle, current_ma2)
    
    if not (touched_ma1 or touched_ma2):
        return None
    
    # Determine which MA was touched
    ma_level = "MA1" if touched_ma1 else "MA2"
    
    # DSR RULE 4: Generate signal based on bias and rejection
    signal_side = None
    
    if bias == "BUY_BIAS":
        # Buy signals: Lower rejection at MA level in uptrend
        if rejection_type in ["LOWER_REJECTION", "SMALL_BODY_LOWER"]:
            signal_side = "BUY"
    
    elif bias == "SELL_BIAS":
        # Sell signals: Upper rejection at MA level in downtrend
        if rejection_type in ["UPPER_REJECTION", "SMALL_BODY_UPPER"]:
            signal_side = "SELL"
    
    if not signal_side:
        return None
    
    # Cooldown (30 minutes per symbol)
    cooldown_key = f'last_signal_{shorthand}'
    last_signal_time = getattr(detect_pure_dsr_signal, cooldown_key, 0)
    current_time = current_candle["epoch"]
    
    if current_time - last_signal_time < 1800:  # 30 minutes
        return None
    
    setattr(detect_pure_dsr_signal, cooldown_key, current_time)
    
    if DEBUG:
        print(f"PURE DSR: {signal_side} - {rejection_type} at {ma_level}")
        print(f"MA1: {current_ma1:.5f}, MA2: {current_ma2:.5f}, Price: {current_candle['close']:.5f}")
    
    return {
        "symbol": shorthand,
        "side": signal_side,
        "pattern": rejection_type,
        "ma_level": ma_level,
        "price": current_candle["close"],
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
def create_pure_dsr_chart(signal_data):
    """Create clean DSR chart for 1s indices"""
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
            body_color = "#00FF88"
            edge_color = "#00CC66"
        else:
            body_color = "#FF3366"
            edge_color = "#CC1144"
        
        ax.add_patch(Rectangle(
            (i - CANDLE_WIDTH/2, min(o, c)), 
            CANDLE_WIDTH, 
            max(abs(c - o), 1e-9),
            facecolor=body_color, 
            edgecolor=edge_color, 
            alpha=0.9,
            linewidth=1.2
        ))
        
        ax.plot([i, i], [l, h], color=edge_color, linewidth=1.5, alpha=0.9)
    
    # Plot moving averages
    def plot_ma(ma_values, label, color, linewidth=2):
        chart_ma = []
        for i in range(chart_start, n):
            if i < len(ma_values) and ma_values[i] is not None:
                chart_ma.append(ma_values[i])
            else:
                chart_ma.append(None)
        
        valid_points = [(i, v) for i, v in enumerate(chart_ma) if v is not None]
        if valid_points:
            x_vals, y_vals = zip(*valid_points)
            ax.plot(x_vals, y_vals, color=color, linewidth=linewidth, 
                   label=label, alpha=0.95, linestyle='-')
    
    # Plot all MAs
    plot_ma(ma1, "MA1 (SMMA HLC3-9) - Dynamic S/R", "#FFFFFF", 3)
    plot_ma(ma2, "MA2 (SMMA Close-19) - Dynamic S/R", "#00BFFF", 2.5)
    plot_ma(ma3, "MA3 (SMA MA2-25) - Trend Filter", "#FF6347", 2)
    
    # Mark signal point
    signal_chart_idx = signal_idx - chart_start
    if 0 <= signal_chart_idx < len(chart_candles):
        signal_candle = chart_candles[signal_chart_idx]
        signal_price = signal_candle["close"]
        
        if signal_data["side"] == "BUY":
            marker_color = "#00FF88"
            marker_symbol = "^"
        else:
            marker_color = "#FF3366" 
            marker_symbol = "v"
        
        ax.scatter([signal_chart_idx], [signal_price], 
                  color=marker_color, marker=marker_symbol, 
                  s=400, edgecolor="#FFFFFF", linewidth=4, zorder=10)
        
        # Add signal annotation
        ax.annotate(f'{signal_data["side"]} DSR Signal', 
                   xy=(signal_chart_idx, signal_price),
                   xytext=(signal_chart_idx + 15, signal_price),
                   fontsize=12, color='white', fontweight='bold',
                   bbox=dict(boxstyle="round,pad=0.3", facecolor=marker_color, alpha=0.8),
                   arrowprops=dict(arrowstyle='->', color='white', lw=1))
    
    # Clean title
    bias = "UPTREND" if signal_data["ma1"] > signal_data["ma2"] else "DOWNTREND"
    bias_emoji = "📈" if bias == "UPTREND" else "📉"
    
    ax.set_title(f"{signal_data['symbol']} M10 - {signal_data['side']} DSR Signal {bias_emoji}\n"
                f"Pure Trend Following Strategy | {bias} Confirmed | 1s Indices", 
                fontsize=14, color='white', fontweight='bold', pad=20)
    
    # Legend
    legend = ax.legend(loc="upper left", frameon=True, facecolor='black', 
                      edgecolor='white', fontsize=11)
    legend.get_frame().set_alpha(0.9)
    
    # Grid and styling
    ax.grid(True, alpha=0.2, color='gray', linestyle=':', linewidth=0.8)
    ax.tick_params(colors='white', labelsize=11)
    
    for spine in ax.spines.values():
        spine.set_color('white')
        spine.set_linewidth(1.2)
    
    plt.tight_layout()
    
    chart_file = tempfile.NamedTemporaryFile(delete=False, suffix=".png")
    plt.savefig(chart_file.name, 
                dpi=180, 
                bbox_inches="tight", 
                facecolor='black',
                edgecolor='none',
                pad_inches=0.2)
    plt.close()
    plt.style.use('default')
    
    return chart_file.name

# -------------------------
# Main Execution for 1s Indices
 #------------------------
def run_pure_dsr_analysis():
    """Pure DSR analysis with effective ranging detection"""
    signals_found = 0
    
    for shorthand, deriv_symbol in SYMBOL_MAP.items():
        try:
            if DEBUG:
                print(f"Analyzing {shorthand} on M10...")
            
            candles = fetch_candles(deriv_symbol)
            if len(candles) < MIN_CANDLES:
                if DEBUG:
                    print(f"Insufficient candles for {shorthand}: {len(candles)}")
                continue
            
            signal = detect_pure_dsr_signal(candles, shorthand)
            if not signal:
                continue
            
            current_epoch = signal["candles"][signal["idx"]]["epoch"]
            if already_sent(shorthand, current_epoch, signal["side"]):
                if DEBUG:
                    print(f"Signal already sent for {shorthand}")
                continue
            
            # Create clean alert message
            bias = "UPTREND" if signal["ma1"] > signal["ma2"] else "DOWNTREND"
            bias_emoji = "📈" if bias == "UPTREND" else "📉"
            
            caption = (f"🎯 {signal['symbol']} M10 - {signal['side']} DSR SIGNAL {bias_emoji}\n\n"
                      f"📊 Market Bias: {bias}\n"
                      f"🎨 Rejection Pattern: {signal['pattern']}\n"
                      f"📍 Dynamic Level: {signal['ma_level']}\n"
                      f"💰 Entry Price: {signal['price']:.5f}\n"
                      f"🔄 MA1: {signal['ma1']:.5f} | MA2: {signal['ma2']:.5f}\n\n"
                      f"💎 Pure DSR Strategy: Trend Following at Dynamic S/R\n"
                      f"✅ Ranging Market Filter: PASSED")
            
            chart_path = create_pure_dsr_chart(signal)
            
            success, msg_id = send_telegram_photo(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, caption, chart_path)
            
            if success:
                mark_sent(shorthand, current_epoch, signal["side"])
                signals_found += 1
                if DEBUG:
                    print(f"Pure DSR signal sent for {shorthand}: {signal['side']}")
            
            try:
                os.unlink(chart_path)
            except:
                pass
                    
        except Exception as e:
            if DEBUG:
                print(f"Error analyzing {shorthand}: {e}")
                traceback.print_exc()
    
    if DEBUG:
        print(f"Pure DSR Analysis complete. {signals_found} clean signals found.")

# -------------------------
# Entry Point
# -------------------------
if __name__ == "__main__":
    try:
        print("Starting Pure DSR Trading Bot - M10 Timeframe Only")
        print("Core Strategy: MA1/MA2 Bias + MA Touch + Rejection = Signal")
        print("Single Filter: Effective Ranging Market Detection")
        
        run_pure_dsr_analysis()
        
    except Exception as e:
        print(f"Critical error in Pure DSR Bot: {e}")
        traceback.print_exc()
