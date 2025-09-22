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
from enum import Enum
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
################$=============================================##########
# # Enums
class TrendDirection(Enum):
    UPTREND = "UPTREND"
    DOWNTREND = "DOWNTREND"
    RANGING = "RANGING"

class SignalType(Enum):
    BUY = "BUY"
    SELL = "SELL"
    NO_SIGNAL = "NO_SIGNAL"

class CandlestickPattern(Enum):
    PIN_BAR_UP = "PIN_BAR_UP"
    PIN_BAR_DOWN = "PIN_BAR_DOWN"
    DOJI = "DOJI"
    BULLISH_ENGULFING = "BULLISH_ENGULFING"
    BEARISH_ENGULFING = "BEARISH_ENGULFING"
    NO_PATTERN = "NO_PATTERN"

@dataclass
class MovingAverages:
    ma1: float
    ma2: float
    ma3: float

# Persistence
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
    return bool(rec and rec.get("epoch")==epoch and rec.get("side")==side)

def mark_sent(shorthand, tf, epoch, side):
    d=load_persist(); d[f"{shorthand}|{tf}"]={"epoch":epoch,"side":side}; save_persist(d)

def get_timeframe_for_symbol(shorthand):
    return SYMBOL_TF_MAP.get(shorthand, TIMEFRAMES[0] if TIMEFRAMES else 300)

# Moving Averages
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

def create_ma_objects(ma1_list, ma2_list, ma3_list):
    mas = []
    min_len = min(len(ma1_list), len(ma2_list), len(ma3_list))

    for i in range(min_len):
        if all(v is not None for v in [ma1_list[i], ma2_list[i], ma3_list[i]]):
            mas.append(MovingAverages(ma1_list[i], ma2_list[i], ma3_list[i]))
        else:
            mas.append(None)

    return mas

# Trend Detection
def detect_adaptive_trend(mas_objects, current_price):
    if len(mas_objects) < LOOKBACK_PERIOD:
        return TrendDirection.RANGING

    current_ma = mas_objects[-1]
    if current_ma is None:
        return TrendDirection.RANGING

    uptrend_arrangement = (current_price > current_ma.ma1 > current_ma.ma2 > current_ma.ma3)
    downtrend_arrangement = (current_ma.ma3 > current_ma.ma2 > current_ma.ma1 > current_price)

    if not (uptrend_arrangement or downtrend_arrangement):
        return TrendDirection.RANGING

    consistent_periods = 0
    lookback_mas = mas_objects[-min(LOOKBACK_PERIOD, len(mas_objects)):]
    valid_mas = [ma for ma in lookback_mas if ma is not None]

    if len(valid_mas) < LOOKBACK_PERIOD * 0.7:
        return TrendDirection.RANGING

    for ma in valid_mas:
        if uptrend_arrangement:
            if ma.ma1 > ma.ma2 > ma.ma3:
                consistent_periods += 1
        elif downtrend_arrangement:
            if ma.ma3 > ma.ma2 > ma.ma1:
                consistent_periods += 1

    consistency_ratio = consistent_periods / len(valid_mas)

    if consistency_ratio >= 0.7:
        return TrendDirection.UPTREND if uptrend_arrangement else TrendDirection.DOWNTREND

    return TrendDirection.RANGING

# Market Condition Detection
def assess_ma_separation_quality(mas_objects):
    if len(mas_objects) < LOOKBACK_PERIOD:
        return False

    recent_mas = mas_objects[-LOOKBACK_PERIOD:]
    valid_mas = [ma for ma in recent_mas if ma is not None]

    if len(valid_mas) < LOOKBACK_PERIOD * 0.7:
        return False

    separations = []
    for ma in valid_mas:
        sep1 = abs(ma.ma1 - ma.ma2)
        sep2 = abs(ma.ma2 - ma.ma3)
        separations.extend([sep1, sep2])

    if not separations:
        return False

    avg_separation = sum(separations) / len(separations)
    current_ma = mas_objects[-1]

    if current_ma is None:
        return False

    current_sep1 = abs(current_ma.ma1 - current_ma.ma2)
    current_sep2 = abs(current_ma.ma2 - current_ma.ma3)

    return (current_sep1 > avg_separation * 0.6 and current_sep2 > avg_separation * 0.6)

def count_ma_crossovers(mas_objects):
    if len(mas_objects) < 2:
        return 0

    crossovers = 0
    recent_mas = mas_objects[-min(LOOKBACK_PERIOD, len(mas_objects)):]
    valid_pairs = []

    for i in range(1, len(recent_mas)):
        if recent_mas[i-1] is not None and recent_mas[i] is not None:
            valid_pairs.append((recent_mas[i-1], recent_mas[i]))

    for prev_ma, curr_ma in valid_pairs:
        if ((prev_ma.ma1 > prev_ma.ma2) != (curr_ma.ma1 > curr_ma.ma2)):
            crossovers += 1
        if ((prev_ma.ma2 > prev_ma.ma3) != (curr_ma.ma2 > curr_ma.ma3)):
            crossovers += 1

    return crossovers

def is_adaptive_ranging_market(mas_objects, candles):
    if len(mas_objects) < LOOKBACK_PERIOD:
        return True

    crossovers = count_ma_crossovers(mas_objects)
    recent_candles = candles[-LOOKBACK_PERIOD:]
    avg_range = sum(c["high"] - c["low"] for c in recent_candles) / len(recent_candles)
    max_crossovers = max(3, int(avg_range * 1000))

    recent_ma3 = []
    for ma in mas_objects[-min(10, len(mas_objects)):]:
        if ma is not None:
            recent_ma3.append(ma.ma3)

    if len(recent_ma3) >= 2:
        ma3_slope = abs(recent_ma3[-1] - recent_ma3[0]) / len(recent_ma3)
        slope_threshold = avg_range * 0.1
    else:
        ma3_slope = 0
        slope_threshold = avg_range * 0.1

    return crossovers > max_crossovers and ma3_slope < slope_threshold

def detect_adaptive_price_spike(candles):
    if len(candles) < 10:
        return False

    recent_candles = candles[-10:]
    current_candle = candles[-1]

    historical_candles = recent_candles[:-1]
    avg_range = sum(c["high"] - c["low"] for c in historical_candles) / len(historical_candles)
    avg_body = sum(abs(c["close"] - c["open"]) for c in historical_candles) / len(historical_candles)

    current_range = current_candle["high"] - current_candle["low"]
    current_body = abs(current_candle["close"] - current_candle["open"])

    range_spike = current_range > avg_range * 2.5
    body_spike = current_body > avg_body * 3.0

    return range_spike or body_spike

# Pattern Detection
def detect_candlestick_pattern(candle, recent_candles):
    if len(recent_candles) < 5:
        return CandlestickPattern.NO_PATTERN

    recent_bodies = [abs(c["close"] - c["open"]) for c in recent_candles[-5:]]
    avg_body = sum(recent_bodies) / len(recent_bodies) if recent_bodies else 0.01

    o, h, l, c = candle["open"], candle["high"], candle["low"], candle["close"]
    body_size = abs(c - o)
    total_range = h - l
    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - l

    if total_range <= 0:
        return CandlestickPattern.NO_PATTERN

    min_wick_ratio = 1.2
    min_wick_size = max(body_size * min_wick_ratio, avg_body * 0.8)

    if lower_wick >= min_wick_size and upper_wick < body_size * 0.5:
        return CandlestickPattern.PIN_BAR_UP

    if upper_wick >= min_wick_size and lower_wick < body_size * 0.5:
        return CandlestickPattern.PIN_BAR_DOWN

    if body_size <= total_range * 0.1 and total_range > avg_body:
        return CandlestickPattern.DOJI

    if len(recent_candles) >= 2:
        prev_candle = recent_candles[-2]
        prev_o, prev_c = prev_candle["open"], prev_candle["close"]

        if (prev_c < prev_o and c > o and c > prev_o and o < prev_c):
            return CandlestickPattern.BULLISH_ENGULFING

        if (prev_c > prev_o and c < o and c < prev_o and o > prev_c):
            return CandlestickPattern.BEARISH_ENGULFING

    return CandlestickPattern.NO_PATTERN

def is_price_near_ma(price_level, ma_level, recent_ranges):
    if not recent_ranges:
        return False

    avg_range = sum(recent_ranges) / len(recent_ranges)
    proximity_threshold = avg_range * 0.5

    return abs(price_level - ma_level) <= proximity_threshold

def detect_rejection_at_ma(candle, current_ma, trend, recent_candles):
    recent_ranges = [c["high"] - c["low"] for c in recent_candles[-10:]]
    pattern = detect_candlestick_pattern(candle, recent_candles)

    if pattern == CandlestickPattern.NO_PATTERN:
        return False, pattern, "NONE"

    h, l = candle["high"], candle["low"]

    if trend == TrendDirection.UPTREND:
        at_ma1 = is_price_near_ma(l, current_ma.ma1, recent_ranges)
        at_ma2 = is_price_near_ma(l, current_ma.ma2, recent_ranges)

        valid_patterns = [CandlestickPattern.PIN_BAR_UP, CandlestickPattern.DOJI, 
                         CandlestickPattern.BULLISH_ENGULFING]

        if pattern in valid_patterns and at_ma1:
            return True, pattern, "MA1"
        elif pattern in valid_patterns and at_ma2:
            return True, pattern, "MA2"

    elif trend == TrendDirection.DOWNTREND:
        at_ma1 = is_price_near_ma(h, current_ma.ma1, recent_ranges)
        at_ma2 = is_price_near_ma(h, current_ma.ma2, recent_ranges)

        valid_patterns = [CandlestickPattern.PIN_BAR_DOWN, CandlestickPattern.DOJI,
                         CandlestickPattern.BEARISH_ENGULFING]

        if pattern in valid_patterns and at_ma1:
            return True, pattern, "MA1"
        elif pattern in valid_patterns and at_ma2:
            return True, pattern, "MA2"

    return False, CandlestickPattern.NO_PATTERN, "NONE"

def calculate_signal_confidence(trend, pattern, mas_objects, candles):
    confidence = 0.0

    if len(mas_objects) >= LOOKBACK_PERIOD:
        consistent_periods = 0
        lookback_mas = mas_objects[-LOOKBACK_PERIOD:]
        valid_mas = [ma for ma in lookback_mas if ma is not None]

        for ma in valid_mas:
            if trend == TrendDirection.UPTREND and ma.ma1 > ma.ma2 > ma.ma3:
                consistent_periods += 1
            elif trend == TrendDirection.DOWNTREND and ma.ma3 > ma.ma2 > ma.ma1:
                consistent_periods += 1

        if valid_mas:
            trend_consistency = consistent_periods / len(valid_mas)
            confidence += trend_consistency * 0.3

    if assess_ma_separation_quality(mas_objects):
        confidence += 0.25

    pattern_scores = {
        CandlestickPattern.PIN_BAR_UP: 0.25,
        CandlestickPattern.PIN_BAR_DOWN: 0.25,
        CandlestickPattern.DOJI: 0.15,
        CandlestickPattern.BULLISH_ENGULFING: 0.20,
        CandlestickPattern.BEARISH_ENGULFING: 0.20
    }
    confidence += pattern_scores.get(pattern, 0)

    recent_candles = candles[-5:]
    avg_body = sum(abs(c["close"] - c["open"]) for c in recent_candles) / len(recent_candles)
    avg_range = sum(c["high"] - c["low"] for c in recent_candles) / len(recent_candles)

    if avg_range > 0:
        stability_ratio = 1 - (avg_body / avg_range)
        confidence += stability_ratio * 0.2

    return min(confidence, 1.0)

# Data Fetching
def fetch_candles(sym, tf, count=CANDLES_N):
    for attempt in range(3):
        try:
            ws = websocket.create_connection(DERIV_WS_URL, timeout=20)

            if DERIV_API_KEY:
                ws.send(json.dumps({"authorize": DERIV_API_KEY}))
                auth_resp = ws.recv()

            request = {
                "ticks_history": sym,
                "style": "candles", 
                "granularity": tf,
                "count": count,
                "end": "latest"
            }

            ws.send(json.dumps(request))
            response = json.loads(ws.recv())
            ws.close()

            if DEBUG:
                print(f"Fetched {len(response.get('candles', []))} candles for {sym}")

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
                print(f"Attempt {attempt + 1} failed for {sym}: {e}")
            time.sleep(1)

    return []

# Signal Detection with Timing Fix
def detect_adaptive_signal(candles, tf, shorthand):
    n = len(candles)
    if n < MIN_CANDLES + 1:
        return None

    analysis_candles = candles[:-1]
    current_candle = analysis_candles[-1]

    ma1_list, ma2_list, ma3_list = compute_mas(analysis_candles)
    mas_objects = create_ma_objects(ma1_list, ma2_list, ma3_list)

    if not mas_objects or mas_objects[-1] is None:
        return None

    current_ma = mas_objects[-1]
    current_price = current_candle["close"]

    if is_adaptive_ranging_market(mas_objects, analysis_candles):
        return None

    if detect_adaptive_price_spike(analysis_candles):
        return None

    trend = detect_adaptive_trend(mas_objects, current_price)
    if trend == TrendDirection.RANGING:
        return None

    if not assess_ma_separation_quality(mas_objects):
        return None

    if len(analysis_candles) >= 2:
        rejection_candle = analysis_candles[-2]
        rejection_ma = mas_objects[-2] if len(mas_objects) >= 2 and mas_objects[-2] is not None else mas_objects[-1]
        confirmation_candle = analysis_candles[-1]

        rejection_found, pattern, ma_level = detect_rejection_at_ma(
            rejection_candle, rejection_ma, trend, analysis_candles[:-2]
        )

        if rejection_found:
            confirmation_valid = False

            if trend == TrendDirection.UPTREND and confirmation_candle["close"] > confirmation_candle["open"]:
                confirmation_valid = True
            elif trend == TrendDirection.DOWNTREND and confirmation_candle["close"] < confirmation_candle["open"]:
                confirmation_valid = True

            if confirmation_valid:
                confidence = calculate_signal_confidence(trend, pattern, mas_objects, analysis_candles)
                signal_side = SignalType.BUY if trend == TrendDirection.UPTREND else SignalType.SELL

                return {
                    "symbol": shorthand,
                    "tf": tf,
                    "side": signal_side.value,
                    "pattern": pattern.value,
                    "ma_level": ma_level,
                    "trend": trend.value,
                    "confidence": confidence,
                    "price": confirmation_candle["close"],
                    "ma1": current_ma.ma1,
                    "ma2": current_ma.ma2,
                    "ma3": current_ma.ma3,
                    "idx": len(analysis_candles) - 1,
                    "candles": analysis_candles,
                    "ma1_array": ma1_list,
                    "ma2_array": ma2_list,
                    "ma3_array": ma3_list
                }

    return None

# Chart Generation with Arrow Fix
def create_adaptive_signal_chart(signal_data):
    candles = signal_data["candles"]
    ma1, ma2, ma3 = signal_data["ma1_array"], signal_data["ma2_array"], signal_data["ma3_array"]
    signal_idx = signal_data["idx"]

    n = len(candles)
    chart_start = max(0, n - LAST_N_CHART)
    chart_candles = candles[chart_start:]

    plt.style.use('dark_background')
    fig, ax = plt.subplots(figsize=(16, 10))
    fig.patch.set_facecolor('#0a0a0a')
    ax.set_facecolor('#0a0a0a')

    for i, candle in enumerate(chart_candles):
        o, h, l, c = candle["open"], candle["high"], candle["low"], candle["close"]

        if c >= o:
            body_color = "#00ff88"
            wick_color = "#00cc66"
        else:
            body_color = "#ff4444"
            wick_color = "#cc3333"

        ax.add_patch(Rectangle(
            (i - CANDLE_WIDTH/2, min(o, c)), 
            CANDLE_WIDTH, 
            max(abs(c - o), 1e-9),
            facecolor=body_color, 
            edgecolor=wick_color, 
            alpha=0.9,
            linewidth=1.2
        ))

        ax.plot([i, i], [l, h], color=wick_color, linewidth=1.5, alpha=0.8)

    def plot_enhanced_ma(ma_values, label, color, linewidth=2.5):
        chart_ma = []
        for i in range(chart_start, n):
            if i < len(ma_values) and ma_values[i] is not None:
                chart_ma.append(ma_values[i])
            else:
                chart_ma.append(None)

        ax.plot(range(len(chart_candles)), chart_ma, 
                color=color, linewidth=linewidth, label=label,
                alpha=0.9, zorder=5)

    plot_enhanced_ma(ma1, "MA1 (SMMA HLC3-9) - Dynamic S/R", "#ffffff")
    plot_enhanced_ma(ma2, "MA2 (SMMA Close-19) - Backup S/R", "#00bfff")
    plot_enhanced_ma(ma3, "MA3 (SMA MA2-25) - Trend Filter", "#ff6347")

    # ARROW VISUALIZATION
    signal_chart_idx = signal_idx - chart_start
    if 0 <= signal_chart_idx < len(chart_candles):
        signal_candle = chart_candles[signal_chart_idx]

        price_range = max([c["high"] for c in chart_candles]) - min([c["low"] for c in chart_candles])
        arrow_offset = price_range * 0.05

        if signal_data["side"] == "BUY":
            arrow_color = "#00ff88"
            arrow_start_y = signal_candle["low"] - arrow_offset
            arrow_end_y = signal_candle["low"] + arrow_offset * 2
        else:
            arrow_color = "#ff4444"
            arrow_start_y = signal_candle["high"] + arrow_offset
            arrow_end_y = signal_candle["high"] - arrow_offset * 2

        ax.plot([signal_chart_idx, signal_chart_idx], 
               [arrow_start_y, arrow_end_y], 
               color=arrow_color, linewidth=4, alpha=0.9, zorder=20)

        if signal_data["side"] == "BUY":
            ax.annotate('', xy=(signal_chart_idx, arrow_end_y), 
                       xytext=(signal_chart_idx, arrow_end_y - arrow_offset * 0.3),
                       arrowprops=dict(arrowstyle='->', lw=3, color=arrow_color),
                       zorder=21)
        else:
            ax.annotate('', xy=(signal_chart_idx, arrow_end_y), 
                       xytext=(signal_chart_idx, arrow_end_y + arrow_offset * 0.3),
                       arrowprops=dict(arrowstyle='->', lw=3, color=arrow_color),
                       zorder=21)

        confidence_text = f"{signal_data['confidence']:.0%}"
        ax.annotate(confidence_text, 
                   xy=(signal_chart_idx, arrow_start_y),
                   xytext=(15, 0), textcoords='offset points',
                   fontsize=12, fontweight='bold', color='white',
                   bbox=dict(boxstyle='round,pad=0.3', facecolor=arrow_color, alpha=0.8),
                   zorder=22)

    trend_emoji = "📈" if signal_data["trend"] == "UPTREND" else "📉"
    confidence_stars = "★" * min(5, int(signal_data["confidence"] * 5))

    title = (f'{signal_data["symbol"]} - {signal_data["side"]} ADAPTIVE DSR {trend_emoji}\n'
             f'Pattern: {signal_data["pattern"]} at {signal_data["ma_level"]} | '
             f'Confidence: {confidence_stars} ({signal_data["confidence"]:.0%})')

    ax.set_title(title, fontsize=16, color='white', fontweight='bold', pad=25)

    legend = ax.legend(loc="upper left", frameon=True, facecolor='#1a1a1a', 
                      edgecolor='white', fontsize=11, framealpha=0.9)
    for text in legend.get_texts():
        text.set_color('white')

    ax.grid(True, alpha=0.2, color='gray', linestyle='--', linewidth=0.5)
    ax.tick_params(colors='white', labelsize=10)

    for spine in ax.spines.values():
        spine.set_color('white')
        spine.set_linewidth(1.2)

    trend_text = f"Trend: {signal_data['trend']}"
    ax.text(0.02, 0.98, trend_text, transform=ax.transAxes,
            fontsize=12, fontweight='bold', color='white',
            verticalalignment='top',
            bbox=dict(boxstyle='round,pad=0.5', facecolor='#333333', alpha=0.8))

    plt.tight_layout()

    chart_file = tempfile.NamedTemporaryFile(delete=False, suffix=".png")
    plt.savefig(chart_file.name, 
                dpi=200, 
                bbox_inches="tight", 
                facecolor='#0a0a0a',
                edgecolor='none',
                pad_inches=0.2)
    plt.close()
    plt.style.use('default')

    return chart_file.name

# Main Analysis Function
def run_adaptive_analysis():
    signals_found = 0

    for shorthand, deriv_symbol in SYMBOL_MAP.items():
        try:
            tf = get_timeframe_for_symbol(shorthand)

            if DEBUG:
                tf_display = f"{tf}s" if tf < 60 else f"{tf//60}m"
                print(f"Analyzing {shorthand} ({deriv_symbol}) on {tf_display}...")

            candles = fetch_candles(deriv_symbol, tf)
            if len(candles) < MIN_CANDLES + 1:
                if DEBUG:
                    print(f"Insufficient candles for {shorthand}: {len(candles)}")
                continue

            signal = detect_adaptive_signal(candles, tf, shorthand)
            if not signal:
                continue

            confirmation_epoch = signal["candles"][signal["idx"]]["epoch"]

            current_time = int(time.time())
            candle_close_time = confirmation_epoch + tf

            if current_time < candle_close_time:
                if DEBUG:
                    print(f"{shorthand}: Confirmation candle not yet closed, skipping signal")
                continue

            if already_sent(shorthand, tf, confirmation_epoch, signal["side"]):
                if DEBUG:
                    print(f"Signal already sent for {shorthand}")
                continue

            tf_display = f"{tf}s" if tf < 60 else f"{tf//60}m"

            confidence_level = signal["confidence"]
            if confidence_level >= 0.8:
                confidence_indicator = "🔥 HIGH"
            elif confidence_level >= 0.6:
                confidence_indicator = "⚡ MEDIUM"
            else:
                confidence_indicator = "💫 LOW"

            trend_indicator = "🚀 STRONG" if signal["trend"] == "UPTREND" else "🔻 STRONG"

            caption = (
                f"🎯 ADAPTIVE DSR SIGNAL\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"📊 {signal['symbol']} ({tf_display})\n"
                f"📈 {signal['side']} Signal\n"
                f"🎨 Pattern: {signal['pattern']}\n"
                f"📍 Level: {signal['ma_level']} (Dynamic S/R)\n"
                f"📉 Trend: {signal['trend']} {trend_indicator}\n"
                f"💪 Confidence: {confidence_indicator} ({confidence_level:.0%})\n"
                f"💰 Entry Price: {signal['price']:.5f}\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"🔹 MA1: {signal['ma1']:.5f}\n"
                f"🔸 MA2: {signal['ma2']:.5f}\n"
                f"🔺 MA3: {signal['ma3']:.5f}\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"🤖 Adaptive Algorithm\n"
                f"⚙️ Market-Responsive Thresholds\n"
                f"⏰ Signal: AFTER Confirmation Close"
            )

            chart_path = create_adaptive_signal_chart(signal)

            success, msg_id = send_telegram_photo(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, caption, chart_path)

            if success:
                mark_sent(shorthand, tf, confirmation_epoch, signal["side"])
                signals_found += 1
                if DEBUG:
                    print(f"PROPERLY TIMED Adaptive DSR signal sent for {shorthand}: {signal['side']} (Confidence: {confidence_level:.0%})")

            try:
                os.unlink(chart_path)
            except:
                pass

        except Exception as e:
            if DEBUG:
                print(f"Error analyzing {shorthand}: {e}")
                traceback.print_exc()

    if DEBUG:
        print(f"Analysis complete. {signals_found} properly-timed adaptive DSR signals found.")

    return signals_found

def run_analysis():
    return run_adaptive_analysis()

# Validation Functions
def validate_strategy_components():
    print("Validating Adaptive DSR Strategy Components...")
    print("=" * 50)

    sample_candles = []
    base_price = 1000.0

    for i in range(100):
        price_change = 0.1 if i < 50 else -0.1
        base_price += price_change

        candle = {
            "epoch": 1640995200 + i * 300,
            "open": base_price - 0.05,
            "high": base_price + 0.1,
            "low": base_price - 0.1,
            "close": base_price
        }
        sample_candles.append(candle)

    ma1, ma2, ma3 = compute_mas(sample_candles)
    mas_objects = create_ma_objects(ma1, ma2, ma3)

    print(f"✓ MA calculations: {len([ma for ma in mas_objects if ma is not None])} valid periods")

    if mas_objects and mas_objects[-1]:
        trend = detect_adaptive_trend(mas_objects, sample_candles[-1]["close"])
        print(f"✓ Trend detection: {trend.value}")

    ranging = is_adaptive_ranging_market(mas_objects, sample_candles)
    spike = detect_adaptive_price_spike(sample_candles)
    separation = assess_ma_separation_quality(mas_objects)

    print(f"✓ Market filters - Ranging: {ranging}, Spike: {spike}, Separation: {separation}")
    print("Strategy validation complete.")

if __name__ == "__main__":
    try:
        if DEBUG:
            validate_strategy_components()
            print("\n" + "="*60 + "\n")

        run_analysis()

    except Exception as e:
        print(f"Critical error: {e}")
        if DEBUG:
            traceback.print_exc()