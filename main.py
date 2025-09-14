#!/usr/bin/env python3
"""
main.py — Dynamic Support & Resistance Trading Bot

Core DSR Strategy (Same logic as main(6).py):
- MA crossovers and rearrangement confirm trend changes
- MA1 (SMMA HLC3-9) and MA2 (SMMA Close-19) = Dynamic S/R
- MA3 (SMA MA2-25) = Trend filter
- BUY BIAS: Only when MA1 > MA2 (bullish arrangement)
- SELL BIAS: Only when MA1 < MA2 (bearish arrangement)
- Rejection patterns at MA1/MA2 levels = Signals
- Now supports both 5min and 10min timeframes independently
"""

import os
import json
import time
import tempfile
import traceback
import requests
from datetime import datetime, timezone
import websocket
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle

# Telegram helpers (fallback to print)
try:
    from bot import send_telegram_message, send_telegram_photo
except Exception:
    def send_telegram_message(token, chat_id, text):
        print("[TEXT]", text)
        return True, "local"
    
    def send_telegram_photo(token, chat_id, caption, photo):
        print("[PHOTO]", caption, photo)
        return True, "local"

# -------------------------
# Config
# -------------------------
DERIV_API_KEY = os.getenv("DERIV_API_KEY", "").strip()
DERIV_APP_ID = os.getenv("DERIV_APP_ID", "1089").strip()
DERIV_WS_URL = f"wss://ws.derivws.com/websockets/v3?app_id={DERIV_APP_ID}"
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

DEBUG = os.getenv("DEBUG", "0") == "1"
TEST_MODE = os.getenv("TEST_MODE", "0") == "1"

CANDLES_N = 480
LAST_N_CHART = 180
CANDLE_WIDTH = 0.35
TMPDIR = tempfile.gettempdir()
ALERT_FILE = os.path.join(TMPDIR, "dsr_last_sent_main.json")
MIN_CANDLES = 50

# -------------------------
# Symbol Mappings with Multiple Timeframes
# -------------------------
SYMBOL_MAP = {
    "V75(1s)": "1HZ75V",
    "V100(1s)": "1HZ100V", 
    "V150(1s)": "1HZ150V",
}

# Symbol-specific timeframes - each symbol runs on 2 independent timeframes
SYMBOL_TF_MAP = {
    "V75(1s)": [300, 600],    # 5min + 10min
    "V100(1s)": [300, 600],   # 5min + 10min  
    "V150(1s)": [300, 600],   # 5min + 10min
}

# -------------------------
# WebSocket Data Fetching
# -------------------------
def fetch_candles_http_fallback(symbol, timeframe, count=None):
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
            result = result_queue.get(timeout=10)
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
        json.dump(d, open(ALERT_FILE, "w"))
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
# MA Crossover Detection (Core DSR)
# -------------------------
def detect_ma_crossover(ma1, ma2, current_idx, lookback=3):
    """Detect MA crossovers - key for trend change confirmation"""
    if current_idx < lookback:
        return False, "NONE"
    
    current_ma1 = ma1[current_idx]
    current_ma2 = ma2[current_idx]
    
    if current_ma1 is None or current_ma2 is None:
        return False, "NONE"
    
    # Check for crossover in last 3 candles
    for i in range(current_idx - lookback + 1, current_idx + 1):
        if i > 0 and i < len(ma1) and i < len(ma2):
            prev_ma1 = ma1[i-1]
            prev_ma2 = ma2[i-1]
            curr_ma1 = ma1[i]
            curr_ma2 = ma2[i]
            
            if all(v is not None for v in [prev_ma1, prev_ma2, curr_ma1, curr_ma2]):
                # Bullish crossover: MA1 crosses above MA2
                if prev_ma1 <= prev_ma2 and curr_ma1 > curr_ma2:
                    return True, "BULLISH_CROSSOVER"
                
                # Bearish crossover: MA1 crosses below MA2  
                if prev_ma1 >= prev_ma2 and curr_ma1 < curr_ma2:
                    return True, "BEARISH_CROSSOVER"
    
    return False, "NONE"

def get_ma_arrangement(ma1_val, ma2_val, ma3_val):
    """Get current MA arrangement for trend confirmation"""
    if not all(v is not None for v in [ma1_val, ma2_val, ma3_val]):
        return "UNDEFINED"
    
    if ma1_val > ma2_val > ma3_val:
        return "BULLISH_ARRANGEMENT"
    elif ma1_val < ma2_val < ma3_val:
        return "BEARISH_ARRANGEMENT"
    else:
        return "MIXED_ARRANGEMENT"

# -------------------------
# Rejection Detection
# -------------------------
def is_rejection_candle(candle):
    """Simple rejection detection - any candle showing rejection behavior"""
    o, h, l, c = candle["open"], candle["high"], candle["low"], candle["close"]
    body_size = abs(c - o)
    total_range = h - l
    
    if total_range <= 0:
        return False, "NONE"
    
    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - l
    
    # Any meaningful rejection signals
    has_upper_wick = upper_wick > 0
    has_lower_wick = lower_wick > 0
    has_small_body = body_size < total_range * 0.7
    
    # Rejection criteria - very lenient
    if has_upper_wick and (upper_wick >= body_size * 0.5 or has_small_body):
        return True, "UPPER_REJECTION"
    
    if has_lower_wick and (lower_wick >= body_size * 0.5 or has_small_body):
        return True, "LOWER_REJECTION"
    
    if has_small_body and (has_upper_wick or has_lower_wick):
        return True, "SMALL_BODY_REJECTION"
    
    return False, "NONE"

# -------------------------
# MA Level Proximity
# -------------------------
def near_ma_levels(price, ma1_val, ma2_val):
    """Check if price is ACTUALLY near MA1 or MA2 - much stricter"""
    if ma1_val is None or ma2_val is None:
        return False, "NONE"
    
    # MUCH TIGHTER tolerance - 0.1% instead of 1%
    tolerance1 = abs(ma1_val) * 0.001
    tolerance2 = abs(ma2_val) * 0.001
    
    if abs(price - ma1_val) <= tolerance1:
        return True, "MA1"
    
    if abs(price - ma2_val) <= tolerance2:
        return True, "MA2"
    
    return False, "NONE"

# -------------------------
# Enhanced Ranging Market Detection for Volatile Indices
# -------------------------
def check_ranging_market(candles, ma1, ma2, current_idx, lookback=20):
    """Enhanced ranging market detection optimized for volatile synthetic indices"""
    if current_idx < lookback:
        return False
    
    # Get recent candles for analysis
    recent_candles = candles[current_idx - lookback + 1:current_idx + 1]
    recent_ma1 = [ma1[i] for i in range(current_idx - lookback + 1, current_idx + 1) if i < len(ma1) and ma1[i] is not None]
    recent_ma2 = [ma2[i] for i in range(current_idx - lookback + 1, current_idx + 1) if i < len(ma2) and ma2[i] is not None]
    
    if len(recent_ma1) < lookback//2 or len(recent_ma2) < lookback//2:
        return False
    
    # Method 1: Horizontal Movement Detection (Key for Jump indices)
    highs = [c["high"] for c in recent_candles]
    lows = [c["low"] for c in recent_candles]
    closes = [c["close"] for c in recent_candles]
    
    # Calculate support/resistance zones
    max_high = max(highs)
    min_low = min(lows)
    price_range = max_high - min_low
    avg_price = sum(closes) / len(closes)
    
    # Check if price is stuck in a tight range
    range_percentage = price_range / avg_price
    
    # For volatile indices, look for tighter consolidations
    if range_percentage < 0.015:  # 1.5% range indicates consolidation
        return True
    
    # Method 2: MA Convergence Analysis
    ma_distances = []
    for i in range(len(recent_ma1)):
        if i < len(recent_ma2):
            distance = abs(recent_ma1[i] - recent_ma2[i]) / recent_ma2[i]
            ma_distances.append(distance)
    
    if ma_distances:
        avg_ma_distance = sum(ma_distances) / len(ma_distances)
        # Tighter threshold for volatile indices
        if avg_ma_distance < 0.003:  # 0.3% MA separation
            return True
    
    # Method 3: Price Oscillation Around MAs (Critical for ranging detection)
    ma1_crosses = 0
    ma2_crosses = 0
    
    for i in range(1, len(recent_candles)):
        candle = recent_candles[i]
        prev_candle = recent_candles[i-1]
        
        if (i-1 < len(recent_ma1) and i < len(recent_ma1) and 
            i-1 < len(recent_ma2) and i < len(recent_ma2)):
            
            ma1_val = recent_ma1[i]
            ma2_val = recent_ma2[i]
            
            # Check for price crossing MAs back and forth
            prev_above_ma1 = prev_candle["close"] > recent_ma1[i-1]
            curr_above_ma1 = candle["close"] > ma1_val
            
            prev_above_ma2 = prev_candle["close"] > recent_ma2[i-1]
            curr_above_ma2 = candle["close"] > ma2_val
            
            if prev_above_ma1 != curr_above_ma1:
                ma1_crosses += 1
            
            if prev_above_ma2 != curr_above_ma2:
                ma2_crosses += 1
    
    # Too many price crosses of MAs = ranging
    if ma1_crosses > 4 or ma2_crosses > 4:
        return True
    
    # Method 4: Directional Movement Analysis
    upward_moves = 0
    downward_moves = 0
    sideways_moves = 0
    
    for i in range(1, len(recent_candles)):
        candle = recent_candles[i]
        prev_candle = recent_candles[i-1]
        
        move_percent = (candle["close"] - prev_candle["close"]) / prev_candle["close"]
        
        if move_percent > 0.002:  # 0.2% up move
            upward_moves += 1
        elif move_percent < -0.002:  # 0.2% down move
            downward_moves += 1
        else:
            sideways_moves += 1
    
    # Too many sideways moves or balanced up/down moves = ranging
    total_moves = upward_moves + downward_moves + sideways_moves
    sideways_ratio = sideways_moves / total_moves if total_moves > 0 else 0
    
    if sideways_ratio > 0.4:  # 40% sideways moves
        return True
    
    # Balanced moves (similar up and down) also indicate ranging
    if total_moves > 0:
        move_balance = abs(upward_moves - downward_moves) / total_moves
        if move_balance < 0.3:  # Very balanced moves
            return True
    
    # Method 5: MA Slope Convergence (Enhanced)
    if len(recent_ma1) >= 8 and len(recent_ma2) >= 8:
        # Calculate slopes over different periods
        ma1_short_slope = (recent_ma1[-1] - recent_ma1[-4]) / recent_ma1[-4] if recent_ma1[-4] != 0 else 0
        ma1_long_slope = (recent_ma1[-1] - recent_ma1[-8]) / recent_ma1[-8] if recent_ma1[-8] != 0 else 0
        
        ma2_short_slope = (recent_ma2[-1] - recent_ma2[-4]) / recent_ma2[-4] if recent_ma2[-4] != 0 else 0
        ma2_long_slope = (recent_ma2[-1] - recent_ma2[-8]) / recent_ma2[-8] if recent_ma2[-8] != 0 else 0
        
        # Both MAs should be flattening for ranging
        if (abs(ma1_short_slope) < 0.002 and abs(ma1_long_slope) < 0.003 and
            abs(ma2_short_slope) < 0.002 and abs(ma2_long_slope) < 0.003):
            return True
    
    # Method 6: Recent High/Low Repetition (Key for Jump indices)
    # Check if price keeps hitting similar levels
    resistance_level = max(highs[-10:]) if len(highs) >= 10 else max(highs)
    support_level = min(lows[-10:]) if len(lows) >= 10 else min(lows)
    
    resistance_touches = 0
    support_touches = 0
    tolerance = avg_price * 0.003  # 0.3% tolerance
    
    for candle in recent_candles[-10:]:  # Check last 10 candles
        if abs(candle["high"] - resistance_level) <= tolerance:
            resistance_touches += 1
        if abs(candle["low"] - support_level) <= tolerance:
            support_touches += 1
    
    # Multiple touches of same levels = ranging
    if resistance_touches >= 3 or support_touches >= 3:
        return True
    
    return False

# -------------------------
# Market Structure Analysis
# -------------------------
def analyze_market_structure(candles, ma1, ma2, ma3, current_idx, lookback=30):
    """Analyze market structure for trend strength and exhaustion detection"""
    if current_idx < lookback:
        return None
    
    recent_candles = candles[current_idx - lookback + 1:current_idx + 1]
    closes = [c["close"] for c in recent_candles]
    highs = [c["high"] for c in recent_candles]
    lows = [c["low"] for c in recent_candles]
    
    # Calculate momentum indicators
    momentum_score = calculate_momentum(recent_candles)
    trend_strength = calculate_trend_strength(ma1, ma2, ma3, current_idx, lookback)
    exhaustion_signals = detect_exhaustion_signals(recent_candles, ma1, ma2, current_idx)
    accumulation_distribution = analyze_accumulation_distribution(recent_candles)
    
    return {
        "momentum_score": momentum_score,
        "trend_strength": trend_strength,
        "exhaustion_signals": exhaustion_signals,
        "accumulation_distribution": accumulation_distribution,
        "market_phase": determine_market_phase(momentum_score, trend_strength, exhaustion_signals)
    }

def calculate_momentum(candles, period=14):
    """Calculate price momentum using multiple methods"""
    if len(candles) < period + 5:
        return 0
    
    closes = [c["close"] for c in candles]
    
    # RSI-like momentum calculation
    gains = []
    losses = []
    
    for i in range(1, len(closes)):
        change = closes[i] - closes[i-1]
        if change > 0:
            gains.append(change)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(abs(change))
    
    if len(gains) < period:
        return 0
    
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    
    if avg_loss == 0:
        return 100
    
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    
    # Convert RSI to momentum score (-100 to +100)
    momentum = (rsi - 50) * 2
    
    # Rate of Change momentum
    if len(closes) >= 10:
        roc = ((closes[-1] - closes[-10]) / closes[-10]) * 100
        momentum = (momentum + roc) / 2  # Average both momentum measures
    
    return max(-100, min(100, momentum))

def calculate_trend_strength(ma1, ma2, ma3, current_idx, lookback=20):
    """Calculate trend strength based on MA alignment and slopes"""
    if current_idx < lookback or not all(v is not None for v in [ma1[current_idx], ma2[current_idx], ma3[current_idx]]):
        return 0
    
    # MA alignment score
    current_ma1 = ma1[current_idx]
    current_ma2 = ma2[current_idx]
    current_ma3 = ma3[current_idx]
    
    alignment_score = 0
    
    # Perfect bullish alignment: MA1 > MA2 > MA3
    if current_ma1 > current_ma2 > current_ma3:
        alignment_score = 50
    # Perfect bearish alignment: MA1 < MA2 < MA3
    elif current_ma1 < current_ma2 < current_ma3:
        alignment_score = -50
    # Partial alignment
    elif current_ma1 > current_ma2:
        alignment_score = 25
    elif current_ma1 < current_ma2:
        alignment_score = -25
    
    # MA slope strength
    if current_idx >= 10:
        ma1_slope = (current_ma1 - ma1[current_idx-10]) / ma1[current_idx-10] * 100
        ma2_slope = (current_ma2 - ma2[current_idx-10]) / ma2[current_idx-10] * 100
        ma3_slope = (current_ma3 - ma3[current_idx-10]) / ma3[current_idx-10] * 100
        
        avg_slope = (ma1_slope + ma2_slope + ma3_slope) / 3
        slope_score = max(-50, min(50, avg_slope * 10))  # Scale slope
        
        total_strength = alignment_score + slope_score
    else:
        total_strength = alignment_score
    
    return max(-100, min(100, total_strength))

def detect_exhaustion_signals(candles, ma1, ma2, current_idx, lookback=15):
    """Detect trend exhaustion signals"""
    if len(candles) < lookback or current_idx < lookback:
        return 0
    
    recent_candles = candles[-lookback:]
    exhaustion_score = 0
    
    # 1. Diminishing momentum (smaller moves despite trend)
    recent_ranges = [(c["high"] - c["low"]) for c in recent_candles[-10:]]
    earlier_ranges = [(c["high"] - c["low"]) for c in recent_candles[-20:-10]] if len(recent_candles) >= 20 else recent_ranges
    
    if recent_ranges and earlier_ranges:
        recent_avg_range = sum(recent_ranges) / len(recent_ranges)
        earlier_avg_range = sum(earlier_ranges) / len(earlier_ranges)
        
        if recent_avg_range < earlier_avg_range * 0.7:  # 30% smaller ranges
            exhaustion_score += 30
    
    # 2. Failed breakouts/breakdowns
    highs = [c["high"] for c in recent_candles]
    lows = [c["low"] for c in recent_candles]
    
    recent_high = max(highs[-5:])
    earlier_high = max(highs[-10:-5]) if len(highs) >= 10 else recent_high
    
    recent_low = min(lows[-5:])
    earlier_low = min(lows[-10:-5]) if len(lows) >= 10 else recent_low
    
    # Failed to make new highs in uptrend
    if (ma1[current_idx] > ma2[current_idx] and  # Uptrend
        recent_high <= earlier_high * 1.001):  # Failed to break higher
        exhaustion_score += 25
    
    # Failed to make new lows in downtrend
    if (ma1[current_idx] < ma2[current_idx] and  # Downtrend
        recent_low >= earlier_low * 0.999):  # Failed to break lower
        exhaustion_score += 25
    
    # 3. Convergence of price to MAs (losing momentum)
    current_price = candles[-1]["close"]
    ma1_distance = abs(current_price - ma1[current_idx]) / ma1[current_idx]
    ma2_distance = abs(current_price - ma2[current_idx]) / ma2[current_idx]
    
    if ma1_distance < 0.005 and ma2_distance < 0.01:  # Very close to MAs
        exhaustion_score += 20
    
    return min(100, exhaustion_score)

def analyze_accumulation_distribution(candles, period=20):
    """Analyze accumulation/distribution patterns"""
    if len(candles) < period:
        return 0
    
    recent_candles = candles[-period:]
    ad_score = 0
    
    for candle in recent_candles:
        o, h, l, c = candle["open"], candle["high"], candle["low"], candle["close"]
        
        if h != l:  # Avoid division by zero
            # Money Flow Multiplier
            mf_multiplier = ((c - l) - (h - c)) / (h - l)
            
            # Approximate volume using range (for synthetic indices)
            volume_proxy = h - l
            
            # Money Flow Volume
            mf_volume = mf_multiplier * volume_proxy
            ad_score += mf_volume
    
    # Normalize the score
    total_volume = sum([(c["high"] - c["low"]) for c in recent_candles])
    if total_volume > 0:
        ad_score = (ad_score / total_volume) * 100
    
    return max(-100, min(100, ad_score))

def determine_market_phase(momentum, trend_strength, exhaustion):
    """Determine current market phase"""
    if exhaustion > 50:
        return "EXHAUSTION"
    elif abs(momentum) < 20 and abs(trend_strength) < 30:
        return "CONSOLIDATION"
    elif momentum > 40 and trend_strength > 40:
        return "STRONG_UPTREND"
    elif momentum < -40 and trend_strength < -40:
        return "STRONG_DOWNTREND"
    elif momentum > 20 and trend_strength > 20:
        return "MODERATE_UPTREND"
    elif momentum < -20 and trend_strength < -20:
        return "MODERATE_DOWNTREND"
    else:
        return "TRANSITION"

def is_trend_exhausted(market_analysis, signal_side):
    """Check if trend is exhausted before taking signal"""
    if not market_analysis:
        return True
    
    exhaustion = market_analysis["exhaustion_signals"]
    momentum = market_analysis["momentum_score"]
    trend_strength = market_analysis["trend_strength"]
    phase = market_analysis["market_phase"]
    
    # High exhaustion = no trades
    if exhaustion > 60:
        return True
    
    # Check for trend-signal alignment
    if signal_side == "BUY":
        # Don't buy in strong downtrend or when momentum is very negative
        if trend_strength < -30 or momentum < -40:
            return True
        # Don't buy in exhaustion phase
        if phase == "EXHAUSTION":
            return True
        # Weak uptrend with high exhaustion
        if trend_strength < 30 and exhaustion > 40:
            return True
    
    elif signal_side == "SELL":
        # Don't sell in strong uptrend or when momentum is very positive
        if trend_strength > 30 or momentum > 40:
            return True
        # Don't sell in exhaustion phase
        if phase == "EXHAUSTION":
            return True
        # Weak downtrend with high exhaustion
        if trend_strength > -30 and exhaustion > 40:
            return True
    
    return False
# -------------------------
# Enhanced Core DSR Signal Detection with Market Analysis
# -------------------------
def detect_signal(candles, tf, shorthand):
    """Complete DSR Strategy with market structure analysis for trend strength and exhaustion detection"""
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
    
    current_close = current_candle["close"]
    current_high = current_candle["high"]
    current_low = current_candle["low"]
    
    # STEP 1: Analyze market structure and trend strength
    market_analysis = analyze_market_structure(candles, ma1, ma2, ma3, current_idx)
    if not market_analysis:
        return None
    
    # STEP 2: Check for ranging market (enhanced filter)
    is_ranging = check_ranging_market(candles, ma1, ma2, current_idx)
    if is_ranging:
        return None
    
    # STEP 3: DSR RULE 1 - Determine bias from MA1/MA2 relationship
    if current_ma1 > current_ma2:
        bias = "BUY_BIAS"
        signal_side = "BUY"
    elif current_ma1 < current_ma2:
        bias = "SELL_BIAS"
        signal_side = "SELL"
    else:
        return None  # No clear bias
    
    # STEP 4: Check trend exhaustion BEFORE other rules
    if is_trend_exhausted(market_analysis, signal_side):
        if DEBUG:
            print(f"Trend exhausted for {shorthand} {signal_side}: Phase={market_analysis['market_phase']}, Exhaustion={market_analysis['exhaustion_signals']:.1f}")
        return None
    
    # STEP 5: Market phase compatibility check
    market_phase = market_analysis["market_phase"]
    if market_phase in ["EXHAUSTION", "CONSOLIDATION"]:
        return None
    
    # For BUY signals, only trade in uptrend phases
    if signal_side == "BUY" and market_phase not in ["STRONG_UPTREND", "MODERATE_UPTREND"]:
        return None
    
    # For SELL signals, only trade in downtrend phases  
    if signal_side == "SELL" and market_phase not in ["STRONG_DOWNTREND", "MODERATE_DOWNTREND"]:
        return None
    
    # STEP 6: Minimum trend strength requirement
    trend_strength = market_analysis["trend_strength"]
    momentum = market_analysis["momentum_score"]
    
    if signal_side == "BUY":
        # Require positive trend strength and momentum for buy signals
        if trend_strength < 25 or momentum < 15:
            return None
    elif signal_side == "SELL":
        # Require negative trend strength and momentum for sell signals
        if trend_strength > -25 or momentum > -15:
            return None
    
    # STEP 7: DSR RULE 2 & 3 - Price position requirements (stricter)
    if bias == "BUY_BIAS":
        # For BUY: Price must be meaningfully above MA1, not just touching
        if current_close <= current_ma1 * 1.002:  # Must be at least 0.2% above MA1
            return None
    elif bias == "SELL_BIAS":
        # For SELL: Price must be meaningfully below MA1, not just touching
        if current_close >= current_ma1 * 0.998:  # Must be at least 0.2% below MA1
            return None
    
    # STEP 8: DSR RULE 4 - NO signals when price between MAs
    if current_ma1 > current_ma2:  # Uptrend structure
        if current_ma2 < current_close < current_ma1:
            return None  # Price between MA2 and MA1
    else:  # Downtrend structure  
        if current_ma1 < current_close < current_ma2:
            return None  # Price between MA1 and MA2
    
    # STEP 9: Must have rejection pattern
    is_rejection, pattern_type = is_rejection_candle(current_candle)
    if not is_rejection:
        return None
    
    # STEP 10: DSR RULE 5 - Price at or near MA1/MA2 (enhanced tolerance)
    ma1_tolerance = current_ma1 * 0.002  # Increased to 0.2%
    ma2_tolerance = current_ma2 * 0.002  # Increased to 0.2%
    
    # Check if any part of candle touched MA1 or MA2
    touched_ma1 = (abs(current_high - current_ma1) <= ma1_tolerance or 
                   abs(current_low - current_ma1) <= ma1_tolerance or 
                   abs(current_close - current_ma1) <= ma1_tolerance)
    
    touched_ma2 = (abs(current_high - current_ma2) <= ma2_tolerance or 
                   abs(current_low - current_ma2) <= ma2_tolerance or 
                   abs(current_close - current_ma2) <= ma2_tolerance)
    
    if not (touched_ma1 or touched_ma2):
        return None
    
    # STEP 11: Determine which MA level was touched
    ma_level = "MA1" if touched_ma1 else "MA2"
    
    # STEP 12: Additional confluence check - Accumulation/Distribution alignment
    ad_score = market_analysis["accumulation_distribution"]
    if signal_side == "BUY" and ad_score < -20:  # Negative A/D for buy signal
        return None
    if signal_side == "SELL" and ad_score > 20:  # Positive A/D for sell signal  
        return None
    
    # STEP 13: Generate enhanced context
    context = f"MA1 {'above' if bias == 'BUY_BIAS' else 'below'} MA2 - {market_phase.lower().replace('_', ' ')}"
    context += f" (Strength: {trend_strength:.0f}, Momentum: {momentum:.0f})"
    
    # STEP 14: Enhanced cooldown with market phase consideration
    cooldown_key = f"last_signal_{shorthand}_{tf}"
    last_signal_time = getattr(detect_signal, cooldown_key, 0)
    current_time = current_candle["epoch"]
    
    # Shorter cooldown for strong trends, longer for weak trends
    if market_phase in ["STRONG_UPTREND", "STRONG_DOWNTREND"]:
        cooldown_period = 1200  # 20 minutes for strong trends
    else:
        cooldown_period = 2400  # 40 minutes for moderate trends
    
    if current_time - last_signal_time < cooldown_period:
        return None
    
    setattr(detect_signal, cooldown_key, current_time)
    
    if DEBUG:
        print(f"VALID DSR: {signal_side} - {pattern_type} at {ma_level}")
        print(f"  Market: {market_phase} | Strength: {trend_strength:.1f} | Momentum: {momentum:.1f}")
        print(f"  Price: {current_close:.2f} | MA1: {current_ma1:.2f} | MA2: {current_ma2:.2f}")
        print(f"  Exhaustion: {market_analysis['exhaustion_signals']:.1f} | A/D: {ad_score:.1f}")
    
    return {
        "symbol": shorthand,
        "tf": tf,
        "side": signal_side,
        "pattern": pattern_type,
        "ma_level": ma_level,
        "ma_arrangement": "BULLISH_ARRANGEMENT" if bias == "BUY_BIAS" else "BEARISH_ARRANGEMENT",
        "crossover": "NONE",
        "context": context,
        "price": current_close,
        "ma1": current_ma1,
        "ma2": current_ma2, 
        "ma3": current_ma3,
        "market_phase": market_phase,
        "trend_strength": trend_strength,
        "momentum": momentum,
        "exhaustion": market_analysis["exhaustion_signals"],
        "idx": current_idx,
        "candles": candles,
        "ma1_array": ma1,
        "ma2_array": ma2,
        "ma3_array": ma3
    }

# -------------------------
# Chart Generation with ALL Moving Averages
# -------------------------
def create_signal_chart(signal_data):
    """Create chart for signal visualization with ALL 3 MAs plotted"""
    candles = signal_data["candles"]
    signal_idx = signal_data["idx"]
    
    # Get MA arrays from signal data
    ma1 = signal_data["ma1_array"]
    ma2 = signal_data["ma2_array"] 
    ma3 = signal_data["ma3_array"]
    
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
    
    # Plot ALL moving averages (filter out None values)
    def plot_ma(ma_values, label, color, linewidth=2):
        chart_ma = []
        for i in range(chart_start, n):
            if i < len(ma_values) and ma_values[i] is not None:
                chart_ma.append(ma_values[i])
            else:
                chart_ma.append(None)
        
        # Only plot points where we have valid data
        valid_points = [(i, v) for i, v in enumerate(chart_ma) if v is not None]
        if valid_points:
            x_vals, y_vals = zip(*valid_points)
            ax.plot(x_vals, y_vals, color=color, linewidth=linewidth, label=label, alpha=0.9)
    
    # Plot all three moving averages
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
    
    # Title with timeframe and MA arrangement info
    tf_display = f"{signal_data['tf']}s" if signal_data['tf'] < 60 else f"{signal_data['tf']//60}m"
    arrangement_emoji = "📈" if signal_data["ma_arrangement"] == "BULLISH_ARRANGEMENT" else "📉"
    title = f"{signal_data['symbol']} {tf_display} - {signal_data['side']} DSR Signal {arrangement_emoji}"
    ax.set_title(title, fontsize=16, color='white', fontweight='bold', pad=20)
    
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
# Main Execution with Multiple Timeframes
# -------------------------
def run_analysis():
    """DSR analysis with advanced logic on multiple timeframes per symbol"""
    signals_found = 0
    
    for shorthand, deriv_symbol in SYMBOL_MAP.items():
        timeframes = SYMBOL_TF_MAP.get(shorthand, [300, 600])
        
        for tf in timeframes:
            try:
                tf_display = f"{tf}s" if tf < 60 else f"{tf//60}m"
                
                if DEBUG:
                    print(f"Analyzing {shorthand} on {tf_display}...")
                
                candles = fetch_candles(deriv_symbol, tf)
                if len(candles) < MIN_CANDLES:
                    if DEBUG:
                        print(f"Insufficient candles for {shorthand} {tf_display}: {len(candles)}")
                    continue
                
                signal = detect_signal(candles, tf, shorthand)
                if not signal:
                    continue
                
                current_epoch = signal["candles"][signal["idx"]]["epoch"]
                if already_sent(shorthand, tf, current_epoch, signal["side"]):
                    if DEBUG:
                        print(f"Signal already sent for {shorthand} {tf_display}")
                    continue
                
                # Create enhanced alert message with MA crossover info and timeframe
                arrangement_emoji = "📈" if signal["ma_arrangement"] == "BULLISH_ARRANGEMENT" else "📉"
                crossover_info = f" ({signal['crossover']})" if signal['crossover'] != "NONE" else ""
                
                caption = f"🎯 {signal['symbol']} {tf_display} - {signal['side']} SIGNAL\n"
                caption += f"{arrangement_emoji} MA Setup: {signal['ma_arrangement'].replace('_', ' ')}{crossover_info}\n"
                caption += f"🎨 Pattern: {signal['pattern']}\n"
                caption += f"📍 Level: {signal['ma_level']} Dynamic S/R\n"
                caption += f"💰 Price: {signal['price']:.5f}\n"
                caption += f"📊 Context: {signal['context']}"
                
                chart_path = create_signal_chart(signal)
                
                success, msg_id = send_telegram_photo(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, caption, chart_path)
                
                if success:
                    mark_sent(shorthand, tf, current_epoch, signal["side"])
                    signals_found += 1
                    if DEBUG:
                        print(f"DSR signal sent for {shorthand} {tf_display}: {signal['side']}")
                
                try:
                    os.unlink(chart_path)
                except:
                    pass
                    
            except Exception as e:
                if DEBUG:
                    tf_display = f"{tf}s" if tf < 60 else f"{tf//60}m"
                    print(f"Error analyzing {shorthand} {tf_display}: {e}")
                    traceback.print_exc()
    
    if DEBUG:
        print(f"Analysis complete. {signals_found} DSR signals found.")

if __name__ == "__main__":
    try:
        run_analysis()
    except Exception as e:
        print(f"Critical error: {e}")
        traceback.print_exc()
