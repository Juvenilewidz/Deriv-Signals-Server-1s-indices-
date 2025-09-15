#!/usr/bin/env python3
"""
main.py — Dynamic Support & Resistance Trading Bot with Adaptive Tolerances

Core DSR Strategy (TREND FOLLOWING):
- MA1 (SMMA HLC3-9) and MA2 (SMMA Close-19) = Dynamic S/R
- MA3 (SMA MA2-25) = Trend filter
- Adaptive tolerances based on market volatility
- Practical chart pattern recognition (secondary confirmation)
- Enhanced market structure analysis
- Supports both 5min and 10min timeframes independently
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

SYMBOL_TF_MAP = {
    "V75(1s)": [300, 600],    # 5min + 10min
    "V100(1s)": [300, 600],   # 5min + 10min  
    "V150(1s)": [300, 600],   # 5min + 10min
}

# -------------------------
# Adaptive Tolerance System
# -------------------------
def calculate_adaptive_tolerance(candles, lookback=20):
    """Calculate dynamic tolerance based on recent market volatility"""
    if len(candles) < lookback:
        return 0.008  # Default 0.8%
    
    recent = candles[-lookback:]
    
    # Calculate ATR-based volatility
    atr_sum = 0
    for i in range(1, len(recent)):
        curr, prev = recent[i], recent[i-1]
        tr = max(curr["high"] - curr["low"],
                abs(curr["high"] - prev["close"]),
                abs(curr["low"] - prev["close"]))
        atr_sum += tr
    
    avg_atr = atr_sum / (len(recent) - 1)
    avg_price = sum(c["close"] for c in recent) / len(recent)
    
    # Dynamic tolerance: base 0.4% + volatility component
    volatility_factor = avg_atr / avg_price
    adaptive_tol = 0.004 + (volatility_factor * 1.5)
    
    # Cap between 0.2% and 4%
    return max(0.002, min(0.04, adaptive_tol))

def get_price_based_tolerance(price):
    """Get tolerance based on price level"""
    if price < 10:
        return 0.015  # 1.5% for low prices
    elif price < 100:
        return 0.010  # 1.0% for medium prices
    elif price < 1000:
        return 0.008  # 0.8% for higher prices
    else:
        return 0.006  # 0.6% for very high prices

# -------------------------
# WebSocket Data Fetching
# -------------------------
def fetch_candles_http_fallback(symbol, timeframe, count=None):
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
        
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        candles = []
        
        if data.get("msg_type") == "candles":
            for candle in data.get("candles", []):
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

def fetch_candles(symbol, timeframe, count=None):
    if count is None:
        count = CANDLES_N
    
    candles = []
    try:
        import threading, queue
        result_queue = queue.Queue()
        data_received = False
        
        def on_message(ws, message):
            nonlocal candles, data_received
            try:
                data = json.loads(message)
                if data.get("msg_type") == "candles":
                    for candle in data.get("candles", []):
                        candles.append({
                            "epoch": int(candle.get("epoch", 0)),
                            "open": float(candle.get("open", 0)),
                            "high": float(candle.get("high", 0)),
                            "low": float(candle.get("low", 0)),
                            "close": float(candle.get("close", 0))
                        })
                    data_received = True
                    result_queue.put("SUCCESS")
                elif data.get("error"):
                    result_queue.put("ERROR")
            except Exception:
                result_queue.put("ERROR")
        
        def on_error(ws, error):
            result_queue.put("ERROR")
        
        def on_open(ws):
            try:
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
            except Exception:
                result_queue.put("ERROR")
        
        ws = websocket.WebSocketApp(DERIV_WS_URL,
                                   on_open=on_open,
                                   on_message=on_message,
                                   on_error=on_error)
        
        def run_ws():
            try:
                ws.run_forever()
            except Exception:
                result_queue.put("ERROR")
        
        ws_thread = threading.Thread(target=run_ws, daemon=True)
        ws_thread.start()
        
        try:
            result = result_queue.get(timeout=10)
            if result != "SUCCESS":
                candles = fetch_candles_http_fallback(symbol, timeframe, count)
        except queue.Empty:
            candles = fetch_candles_http_fallback(symbol, timeframe, count)
        
        if data_received:
            time.sleep(1)
            
    except Exception:
        candles = fetch_candles_http_fallback(symbol, timeframe, count)
    finally:
        try:
            ws.close()
        except:
            pass
    
    candles.sort(key=lambda x: x["epoch"])
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
# Moving Averages (CORE DSR)
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
# Market Analysis
# -------------------------
def calculate_momentum(candles, period=14):
    if len(candles) < period + 5:
        return 0
    
    closes = [c["close"] for c in candles]
    gains, losses = [], []
    
    for i in range(1, len(closes)):
        change = closes[i] - closes[i-1]
        gains.append(max(0, change))
        losses.append(max(0, -change))
    
    if len(gains) < period:
        return 0
    
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    
    if avg_loss == 0:
        return 100
    
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    momentum = (rsi - 50) * 2
    
    if len(closes) >= 10:
        roc = ((closes[-1] - closes[-10]) / closes[-10]) * 100
        momentum = (momentum + roc) / 2
    
    return max(-100, min(100, momentum))

def calculate_trend_strength(ma1, ma2, ma3, current_idx, lookback=20):
    if current_idx < lookback or not all(v is not None for v in [ma1[current_idx], ma2[current_idx], ma3[current_idx]]):
        return 0
    
    c_ma1, c_ma2, c_ma3 = ma1[current_idx], ma2[current_idx], ma3[current_idx]
    
    alignment_score = 0
    if c_ma1 > c_ma2 > c_ma3:
        alignment_score = 50
    elif c_ma1 < c_ma2 < c_ma3:
        alignment_score = -50
    elif c_ma1 > c_ma2:
        alignment_score = 25
    elif c_ma1 < c_ma2:
        alignment_score = -25
    
    if current_idx >= 10:
        ma1_slope = (c_ma1 - ma1[current_idx-10]) / ma1[current_idx-10] * 100
        ma2_slope = (c_ma2 - ma2[current_idx-10]) / ma2[current_idx-10] * 100
        ma3_slope = (c_ma3 - ma3[current_idx-10]) / ma3[current_idx-10] * 100
        
        avg_slope = (ma1_slope + ma2_slope + ma3_slope) / 3
        slope_score = max(-50, min(50, avg_slope * 10))
        return max(-100, min(100, alignment_score + slope_score))
    
    return alignment_score

def detect_exhaustion_signals(candles, current_idx, lookback=15):
    if len(candles) < lookback or current_idx < lookback:
        return 0
    
    recent = candles[-lookback:]
    exhaustion_score = 0
    
    # Diminishing ranges
    recent_ranges = [(c["high"] - c["low"]) for c in recent[-10:]]
    earlier_ranges = [(c["high"] - c["low"]) for c in recent[-20:-10]] if len(recent) >= 20 else recent_ranges
    
    if recent_ranges and earlier_ranges:
        recent_avg = sum(recent_ranges) / len(recent_ranges)
        earlier_avg = sum(earlier_ranges) / len(earlier_ranges)
        
        if recent_avg < earlier_avg * 0.7:
            exhaustion_score += 30
    
    # Price convergence to MAs
    current_price = candles[-1]["close"]
    
    return min(100, exhaustion_score)

def analyze_accumulation_distribution(candles, period=20):
    if len(candles) < period:
        return 0
    
    ad_score = 0
    for candle in candles[-period:]:
        o, h, l, c = candle["open"], candle["high"], candle["low"], candle["close"]
        
        if h != l:
            mf_multiplier = ((c - l) - (h - c)) / (h - l)
            volume_proxy = h - l
            ad_score += mf_multiplier * volume_proxy
    
    total_volume = sum([(c["high"] - c["low"]) for c in candles[-period:]])
    if total_volume > 0:
        ad_score = (ad_score / total_volume) * 100
    
    return max(-100, min(100, ad_score))

def analyze_market_structure(candles, ma1, ma2, ma3, current_idx):
    if current_idx < 30:
        return None
    
    momentum = calculate_momentum(candles)
    trend_strength = calculate_trend_strength(ma1, ma2, ma3, current_idx)
    exhaustion = detect_exhaustion_signals(candles, current_idx)
    ad_score = analyze_accumulation_distribution(candles)
    
    # Determine market phase
    if exhaustion > 50:
        phase = "EXHAUSTION"
    elif abs(momentum) < 20 and abs(trend_strength) < 30:
        phase = "CONSOLIDATION"
    elif momentum > 40 and trend_strength > 40:
        phase = "STRONG_UPTREND"
    elif momentum < -40 and trend_strength < -40:
        phase = "STRONG_DOWNTREND"
    elif momentum > 20 and trend_strength > 20:
        phase = "MODERATE_UPTREND"
    elif momentum < -20 and trend_strength < -20:
        phase = "MODERATE_DOWNTREND"
    else:
        phase = "TRANSITION"
    
    return {
        "momentum_score": momentum,
        "trend_strength": trend_strength,
        "exhaustion_signals": exhaustion,
        "accumulation_distribution": ad_score,
        "market_phase": phase
    }

def is_trend_exhausted(market_analysis, signal_side):
    if not market_analysis:
        return True
    
    exhaustion = market_analysis["exhaustion_signals"]
    momentum = market_analysis["momentum_score"]
    trend_strength = market_analysis["trend_strength"]
    phase = market_analysis["market_phase"]
    
    if exhaustion > 60:
        return True
    
    if signal_side == "BUY":
        if trend_strength < -30 or momentum < -40:
            return True
        if phase == "EXHAUSTION":
            return True
        if trend_strength < 30 and exhaustion > 40:
            return True
    elif signal_side == "SELL":
        if trend_strength > 30 or momentum > 40:
            return True
        if phase == "EXHAUSTION":
            return True
        if trend_strength > -30 and exhaustion > 40:
            return True
    
    return False

# -------------------------
# Enhanced Ranging Market Detection with Adaptive Tolerances
# -------------------------
def check_ranging_market(candles, ma1, ma2, current_idx, lookback=20):
    if current_idx < lookback:
        return False
    
    adaptive_tol = calculate_adaptive_tolerance(candles[current_idx - lookback + 1:current_idx + 1])
    recent_candles = candles[current_idx - lookback + 1:current_idx + 1]
    
    # Method 1: MA Distance with adaptive tolerance
    ma_distances = []
    for i in range(current_idx - lookback + 1, current_idx + 1):
        if i < len(ma1) and i < len(ma2) and ma1[i] is not None and ma2[i] is not None:
            distance = abs(ma1[i] - ma2[i]) / ma2[i]
            ma_distances.append(distance)
    
    if ma_distances:
        avg_distance = sum(ma_distances) / len(ma_distances)
        if avg_distance < adaptive_tol:
            return True
    
    # Method 2: Price volatility with adaptive threshold
    closes = [c["close"] for c in recent_candles]
    if closes:
        price_range = max(closes) - min(closes)
        avg_price = sum(closes) / len(closes)
        volatility = price_range / avg_price
        
        if volatility < adaptive_tol * 3:
            return True
    
    # Method 3: Range contraction
    if len(recent_candles) >= 10:
        early_range = max([c["high"] for c in recent_candles[:5]]) - min([c["low"] for c in recent_candles[:5]])
        later_range = max([c["high"] for c in recent_candles[-5:]]) - min([c["low"] for c in recent_candles[-5:]])
        
        if later_range < early_range * 0.6:
            return True
    
    return False

# -------------------------
# Adaptive Pattern Recognition (SECONDARY CONFIRMATION)
# -------------------------
def detect_chart_patterns_adaptive(candles, current_idx, lookback=35):
    if current_idx < lookback or len(candles) < lookback:
        return {"pattern": "NONE", "strength": 0, "breakout_direction": None}
    
    adaptive_tol = calculate_adaptive_tolerance(candles[current_idx - lookback + 1:current_idx + 1])
    recent = candles[current_idx - lookback + 1:current_idx + 1]
    highs = [c["high"] for c in recent]
    lows = [c["low"] for c in recent]
    
    # Double patterns with adaptive tolerance
    double_pattern = detect_double_patterns_adaptive(highs, lows, adaptive_tol)
    if double_pattern["pattern"] != "NONE":
        return double_pattern
    
    # Head & Shoulders with adaptive tolerance
    hs_pattern = detect_head_shoulders_adaptive(highs, lows, adaptive_tol)
    if hs_pattern["pattern"] != "NONE":
        return hs_pattern
    
    # Triangles with adaptive tolerance
    triangle_pattern = detect_triangles_adaptive(highs, lows, adaptive_tol)
    if triangle_pattern["pattern"] != "NONE":
        return triangle_pattern
    
    return {"pattern": "NONE", "strength": 0, "breakout_direction": None}

def detect_double_patterns_adaptive(highs, lows, tolerance):
    if len(highs) < 10:
        return {"pattern": "NONE", "strength": 0}
    
    # Find peaks with adaptive criteria
    high_points = []
    for i in range(2, len(highs) - 2):
        if highs[i] >= max(highs[max(0, i-2):i+3]):
            high_points.append((i, highs[i]))
    
    # Double top check
    if len(high_points) >= 2:
        for j in range(len(high_points) - 1):
            peak1_idx, peak1_val = high_points[j]
            peak2_idx, peak2_val = high_points[-1]
            
            if abs(peak1_val - peak2_val) / max(peak1_val, peak2_val) <= tolerance * 2:
                valley_start, valley_end = min(peak1_idx, peak2_idx), max(peak1_idx, peak2_idx)
                if valley_end - valley_start > 3:
                    valley_low = min(lows[valley_start:valley_end + 1])
                    valley_depth = (peak1_val - valley_low) / peak1_val
                    
                    if valley_depth > tolerance:
                        return {
                            "pattern": "DOUBLE_TOP",
                            "strength": 65,
                            "breakout_direction": "DOWN"
                        }
    
    # Find troughs
    low_points = []
    for i in range(2, len(lows) - 2):
        if lows[i] <= min(lows[max(0, i-2):i+3]):
            low_points.append((i, lows[i]))
    
    # Double bottom check
    if len(low_points) >= 2:
        for j in range(len(low_points) - 1):
            trough1_idx, trough1_val = low_points[j]
            trough2_idx, trough2_val = low_points[-1]
            
            if abs(trough1_val - trough2_val) / max(trough1_val, trough2_val) <= tolerance * 2:
                peak_start, peak_end = min(trough1_idx, trough2_idx), max(trough1_idx, trough2_idx)
                if peak_end - peak_start > 3:
                    peak_high = max(highs[peak_start:peak_end + 1])
                    peak_height = (peak_high - trough1_val) / trough1_val
                    
                    if peak_height > tolerance:
                        return {
                            "pattern": "DOUBLE_BOTTOM",
                            "strength": 65,
                            "breakout_direction": "UP"
                        }
    
    return {"pattern": "NONE", "strength": 0}

def detect_head_shoulders_adaptive(highs, lows, tolerance):
    if len(highs) < 15:
        return {"pattern": "NONE", "strength": 0}
    
    # Find significant peaks
    peaks = []
    for i in range(3, len(highs) - 3):
        if highs[i] == max(highs[i-2:i+3]):
            peaks.append((i, highs[i]))
    
    # Head and shoulders check
    if len(peaks) >= 3:
        for start in range(max(0, len(peaks) - 4), len(peaks) - 2):
            if start + 2 < len(peaks):
                left_idx, left_val = peaks[start]
                head_idx, head_val = peaks[start + 1]
                right_idx, right_val = peaks[start + 2]
                
                if (head_val > left_val and head_val > right_val and
                    abs(left_val - right_val) / max(left_val, right_val) <= tolerance * 3):
                    return {
                        "pattern": "HEAD_AND_SHOULDERS",
                        "strength": 70,
                        "breakout_direction": "DOWN"
                    }
    
    # Inverse head and shoulders
    troughs = []
    for i in range(3, len(lows) - 3):
        if lows[i] == min(lows[i-2:i+3]):
            troughs.append((i, lows[i]))
    
    if len(troughs) >= 3:
        for start in range(max(0, len(troughs) - 4), len(troughs) - 2):
            if start + 2 < len(troughs):
                left_idx, left_val = troughs[start]
                head_idx, head_val = troughs[start + 1]
                right_idx, right_val = troughs[start + 2]
                
                if (head_val < left_val and head_val < right_val and
                    abs(left_val - right_val) / max(left_val, right_val) <= tolerance * 3):
                    return {
                        "pattern": "INVERSE_HEAD_AND_SHOULDERS",
                        "strength": 70,
                        "breakout_direction": "UP"
                    }
    
    return {"pattern": "NONE", "strength": 0}

def detect_triangles_adaptive(highs, lows, tolerance):
    if len(highs) < 12:
        return {"pattern": "NONE", "strength": 0}
    
    mid = len(highs) // 2
    early_high_max, later_high_max = max(highs[:mid]), max(highs[mid:])
    early_low_min, later_low_min = min(lows[:mid]), min(lows[mid:])
    
    # Ascending triangle
    resistance_flat = abs(early_high_max - later_high_max) / early_high_max <= tolerance * 2
    support_rising = later_low_min > early_low_min * (1 + tolerance)
    
    if resistance_flat and support_rising:
        return {"pattern": "ASCENDING_TRIANGLE", "strength": 60, "breakout_direction": "UP"}
    
    # Descending triangle
    support_flat = abs(early_low_min - later_low_min) / early_low_min <= tolerance * 2
    resistance_falling = later_high_max < early_high_max * (1 - tolerance)
    
    if support_flat and resistance_falling:
        return {"pattern": "DESCENDING_TRIANGLE", "strength": 60, "breakout_direction": "DOWN"}
    
    # Symmetrical triangle
    range_early = early_high_max - early_low_min
    range_later = later_high_max - later_low_min
    
    if range_later < range_early * 0.7:
        return {"pattern": "SYMMETRICAL_TRIANGLE", "strength": 55, "breakout_direction": "PENDING"}
    
    return {"pattern": "NONE", "strength": 0}

def identify_support_resistance_adaptive(candles, current_idx, lookback=60):
    if current_idx < lookback:
        return {"support_zones": [], "resistance_zones": []}
    
    recent = candles[current_idx - lookback + 1:current_idx + 1]
    adaptive_tol = calculate_adaptive_tolerance(recent)
    
    highs = [c["high"] for c in recent]
    lows = [c["low"] for c in recent]
    current_price = recent[-1]["close"]
    
    # Adaptive resistance zones
    resistance_zones = []
    high_groups = {}
    
    for i, high in enumerate(highs):
        placed = False
        for level in high_groups:
            if abs(high - level) / level <= adaptive_tol:
                high_groups[level].append((i, high))
                placed = True
                break
        if not placed:
            high_groups[high] = [(i, high)]
    
    for level, touches in high_groups.items():
        if len(touches) >= 2 and level > current_price:
            avg_level = sum(touch[1] for touch in touches) / len(touches)
            resistance_zones.append({
                "level": avg_level,
                "strength": len(touches) * 30,
                "zone_type": "RESISTANCE"
            })
    
    # Adaptive support zones
    support_zones = []
    low_groups = {}
    
    for i, low in enumerate(lows):
        placed = False
        for level in low_groups:
            if abs(low - level) / level <= adaptive_tol:
                low_groups[level].append((i, low))
                placed = True
                break
        if not placed:
            low_groups[low] = [(i, low)]
    
    for level, touches in low_groups.items():
        if len(touches) >= 2 and level < current_price:
            avg_level = sum(touch[1] for touch in touches) / len(touches)
            support_zones.append({
                "level": avg_level,
                "strength": len(touches) * 30,
                "zone_type": "SUPPORT"
            })
    
    return {
        "support_zones": sorted(support_zones, key=lambda x: x["strength"], reverse=True)[:3],
        "resistance_zones": sorted(resistance_zones, key=lambda x: x["strength"], reverse=True)[:3]
    }

# -------------------------
# Rejection Pattern Detection (CORE DSR)
# -------------------------
def is_rejection_candle(candle):
    o, h, l, c = candle["open"], candle["high"], candle["low"], candle["close"]
    body_size = abs(c - o)
    total_range = h - l
    
    if total_range <= 0:
        return False, "NONE"
    
    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - l
    has_small_body = body_size < total_range * 0.7
    
    if upper_wick > 0 and (upper_wick >= body_size * 0.5 or has_small_body):
        return True, "UPPER_REJECTION"
    
    if lower_wick > 0 and (lower_wick >= body_size * 0.5 or has_small_body):
        return True, "LOWER_REJECTION"
    
    if has_small_body and (upper_wick > 0 or lower_wick > 0):
        return True, "SMALL_BODY_REJECTION"
    
    return False, "NONE"

# -------------------------
# CORE DSR SIGNAL DETECTION (TREND FOLLOWING PRIMARY)
# -------------------------
def detect_signal(candles, tf, shorthand):
    """DSR Strategy (PRIMARY) with adaptive tolerances - Core trend-following logic"""
    n = len(candles)
    if n < MIN_CANDLES:
        return None
    
    current_idx = n - 1
    current_candle = candles[current_idx]
    current_close = current_candle["close"]
    current_high = current_candle["high"]
    current_low = current_candle["low"]
    
    # Compute moving averages (CORE DSR)
    ma1, ma2, ma3 = compute_mas(candles)
    
    current_ma1 = ma1[current_idx] if current_idx < len(ma1) else None
    current_ma2 = ma2[current_idx] if current_idx < len(ma2) else None
    current_ma3 = ma3[current_idx] if current_idx < len(ma3) else None
    
    if not all(v is not None for v in [current_ma1, current_ma2, current_ma3]):
        return None
    
    # STEP 1: DSR RULE 1 - MA bias determination (CORE)
    if current_ma1 > current_ma2:
        bias = "BUY_BIAS"
        signal_side = "BUY"
    elif current_ma1 < current_ma2:
        bias = "SELL_BIAS"
        signal_side = "SELL"
    else:
        return None
    
    # STEP 2: Market structure analysis (PRIMARY)
    market_analysis = analyze_market_structure(candles, ma1, ma2, ma3, current_idx)
    if not market_analysis:
        return None
    
    # STEP 3: Check ranging market with adaptive tolerances
    if check_ranging_market(candles, ma1, ma2, current_idx):
        return None
    
    # STEP 4: Trend exhaustion check (PRIMARY)
    if is_trend_exhausted(market_analysis, signal_side):
        return None
    
    # STEP 5: Market phase compatibility (PRIMARY)
    market_phase = market_analysis["market_phase"]
    if market_phase in ["EXHAUSTION", "CONSOLIDATION"]:
        return None
    
    if signal_side == "BUY" and market_phase not in ["STRONG_UPTREND", "MODERATE_UPTREND"]:
        return None
    if signal_side == "SELL" and market_phase not in ["STRONG_DOWNTREND", "MODERATE_DOWNTREND"]:
        return None
    
    # STEP 6: Trend strength requirements with adaptive thresholds
    trend_strength = market_analysis["trend_strength"]
    momentum = market_analysis["momentum_score"]
    
    # Adaptive strength requirements based on market volatility
    adaptive_tol = calculate_adaptive_tolerance(candles[current_idx-20:current_idx+1] if current_idx >= 20 else candles)
    min_strength = max(15, 25 - (adaptive_tol * 500))  # Lower requirements in volatile markets
    min_momentum = max(8, 15 - (adaptive_tol * 300))
    
    if signal_side == "BUY" and (trend_strength < min_strength or momentum < min_momentum):
        return None
    elif signal_side == "SELL" and (trend_strength > -min_strength or momentum > -min_momentum):
        return None
    
    # STEP 7: DSR price position requirements with adaptive tolerances
    price_tol = get_price_based_tolerance(current_close)
    
    if bias == "BUY_BIAS" and current_close <= current_ma1 * (1 + price_tol):
        return None
    elif bias == "SELL_BIAS" and current_close >= current_ma1 * (1 - price_tol):
        return None
    
    # STEP 8: DSR RULE 4 - No signals between MAs (CORE)
    if current_ma1 > current_ma2 and current_ma2 < current_close < current_ma1:
        return None
    elif current_ma1 < current_ma2 and current_ma1 < current_close < current_ma2:
        return None
    
    # STEP 9: Rejection pattern required (CORE DSR)
    is_rejection, pattern_type = is_rejection_candle(current_candle)
    if not is_rejection:
        return None
    
    # STEP 10: DSR RULE 5 - Price near MA levels with adaptive tolerances
    adaptive_ma_tol = calculate_adaptive_tolerance(candles[current_idx-10:current_idx+1] if current_idx >= 10 else candles)
    
    ma1_tolerance = current_ma1 * adaptive_ma_tol
    ma2_tolerance = current_ma2 * adaptive_ma_tol
    
    touched_ma1 = (abs(current_high - current_ma1) <= ma1_tolerance or 
                   abs(current_low - current_ma1) <= ma1_tolerance or 
                   abs(current_close - current_ma1) <= ma1_tolerance)
    
    touched_ma2 = (abs(current_high - current_ma2) <= ma2_tolerance or 
                   abs(current_low - current_ma2) <= ma2_tolerance or 
                   abs(current_close - current_ma2) <= ma2_tolerance)
    
    if not (touched_ma1 or touched_ma2):
        return None
    
    ma_level = "MA1" if touched_ma1 else "MA2"
    
    # STEP 11: A/D flow check (PRIMARY)
    ad_score = market_analysis["accumulation_distribution"]
    if signal_side == "BUY" and ad_score < -25:
        return None
    if signal_side == "SELL" and ad_score > 25:
        return None
    
    # SECONDARY CONFIRMATIONS (for context only, don't filter)
    chart_pattern = detect_chart_patterns_adaptive(candles, current_idx)
    sr_zones = identify_support_resistance_adaptive(candles, current_idx)
    
    # Check S/R confluence
    price_near_sr = False
    for zone in sr_zones.get("support_zones", []) + sr_zones.get("resistance_zones", []):
        if abs(current_close - zone["level"]) / zone["level"] <= adaptive_ma_tol * 1.5:
            if ((signal_side == "BUY" and zone["zone_type"] == "SUPPORT") or 
                (signal_side == "SELL" and zone["zone_type"] == "RESISTANCE")):
                price_near_sr = True
                break
    
    # Generate context
    context = f"MA1 {'above' if bias == 'BUY_BIAS' else 'below'} MA2 - {market_phase.lower().replace('_', ' ')}"
    context += f" (Strength: {trend_strength:.0f}, Momentum: {momentum:.0f})"
    
    confirmations = []
    if chart_pattern["pattern"] != "NONE":
        confirmations.append(f"Pattern: {chart_pattern['pattern']}")
    if price_near_sr:
        confirmations.append("S/R confluence")
    
    if confirmations:
        context += f" | {', '.join(confirmations)}"
    
    # Standard DSR cooldown
    cooldown_key = f"last_signal_{shorthand}_{tf}"
    last_signal_time = getattr(detect_signal, cooldown_key, 0)
    current_time = current_candle["epoch"]
    
    cooldown_period = 1200 if market_phase in ["STRONG_UPTREND", "STRONG_DOWNTREND"] else 2400
    
    if current_time - last_signal_time < cooldown_period:
        return None
    
    setattr(detect_signal, cooldown_key, current_time)
    
    if DEBUG:
        print(f"VALID DSR: {signal_side} - {pattern_type} at {ma_level}")
        print(f"  Market: {market_phase} | Strength: {trend_strength:.1f} | Momentum: {momentum:.1f}")
        print(f"  Adaptive tolerances: MA={adaptive_ma_tol:.4f}, Price={price_tol:.4f}")
    
    return {
        "symbol": shorthand,
        "tf": tf,
        "side": signal_side,
        "pattern": pattern_type,
        "ma_level": ma_level,
        "ma_arrangement": "BULLISH_ARRANGEMENT" if bias == "BUY_BIAS" else "BEARISH_ARRANGEMENT",
        "context": context,
        "price": current_close,
        "ma1": current_ma1,
        "ma2": current_ma2,
        "ma3": current_ma3,
        "market_phase": market_phase,
        "trend_strength": trend_strength,
        "momentum": momentum,
        "exhaustion": market_analysis["exhaustion_signals"],
        "chart_pattern": chart_pattern["pattern"],
        "sr_confluence": price_near_sr,
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
    signal_idx = signal_data["idx"]
    ma1, ma2, ma3 = signal_data["ma1_array"], signal_data["ma2_array"], signal_data["ma3_array"]
    
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
        
        body_color = "#00FF00" if c >= o else "#FF0000"
        edge_color = "#00AA00" if c >= o else "#AA0000"
        
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
            ax.plot(x_vals, y_vals, color=color, linewidth=linewidth, label=label, alpha=0.9)
    
    plot_ma(ma1, "MA1 (SMMA HLC3-9)", "#FFFFFF", 2)
    plot_ma(ma2, "MA2 (SMMA Close-19)", "#00BFFF", 2)
    plot_ma(ma3, "MA3 (SMA MA2-25)", "#FF6347", 2)
    
    # Mark signal point
    signal_chart_idx = signal_idx - chart_start
    if 0 <= signal_chart_idx < len(chart_candles):
        signal_candle = chart_candles[signal_chart_idx]
        signal_price = signal_candle["close"]
        
        marker_color = "#00FF00" if signal_data["side"] == "BUY" else "#FF0000"
        marker_symbol = "^" if signal_data["side"] == "BUY" else "v"
        
        ax.scatter([signal_chart_idx], [signal_price], 
                  color=marker_color, marker=marker_symbol, 
                  s=300, edgecolor="#FFFFFF", linewidth=3, zorder=10)
    
    # Title
    tf_display = f"{signal_data['tf']}s" if signal_data['tf'] < 60 else f"{signal_data['tf']//60}m"
    phase_emoji = {"STRONG_UPTREND": "🚀", "MODERATE_UPTREND": "📈", 
                   "STRONG_DOWNTREND": "🔻", "MODERATE_DOWNTREND": "📉"}.get(signal_data.get("market_phase", ""), "📊")
    
    ax.set_title(f"{signal_data['symbol']} {tf_display} - {signal_data['side']} DSR Signal {phase_emoji}", 
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
    plt.savefig(chart_file.name, dpi=150, bbox_inches="tight", 
                facecolor='black', edgecolor='none', pad_inches=0.1)
    plt.close()
    
    return chart_file.name

# -------------------------
# Main Execution
# -------------------------
def run_analysis():
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
                
                # Enhanced alert message
                phase_emoji = {"STRONG_UPTREND": "🚀", "MODERATE_UPTREND": "📈",
                              "STRONG_DOWNTREND": "🔻", "MODERATE_DOWNTREND": "📉"}.get(signal.get("market_phase", ""), "📊")
                
                strength_bars = "🟩" * min(5, max(1, int(abs(signal.get("trend_strength", 0)) / 20)))
                momentum_bars = "🟦" * min(5, max(1, int(abs(signal.get("momentum", 0)) / 20)))
                
                caption = f"🎯 {signal['symbol']} {tf_display} - {signal['side']} SIGNAL\n"
                caption += f"📈 Setup: {signal['ma_arrangement'].replace('_', ' ')}\n"
                caption += f"{phase_emoji} Phase: {signal.get('market_phase', 'UNKNOWN').replace('_', ' ')}\n"
                caption += f"🎨 Pattern: {signal['pattern']} at {signal['ma_level']}\n"
                caption += f"💪 Strength: {strength_bars} ({signal.get('trend_strength', 0):.0f})\n"
                caption += f"⚡ Momentum: {momentum_bars} ({signal.get('momentum', 0):.0f})\n"
                caption += f"🔥 Exhaustion: {signal.get('exhaustion', 0):.0f}%\n"
                caption += f"💰 Price: {signal['price']:.5f}\n"
                caption += f"📊 {signal['context']}"
                
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
