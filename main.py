#!/usr/bin/env python3
"""
Simple DSR Strategy - Exact Implementation from Screenshots

Core Logic:
1. MA1 (SMMA HLC3-9) = Primary dynamic support/resistance
2. MA2 (SMMA Close-19) = Backup dynamic support/resistance  
3. MA3 (SMA MA2-25) = Trend filter
4. Buy when price above MA3 + rejection at MA1/MA2 + bullish confirmation
5. Sell when price below MA3 + rejection at MA1/MA2 + bearish confirmation
"""

import os, json, time, tempfile, traceback, requests
from datetime import datetime, timezone
import websocket, matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle

# Telegram helpers
try:
    from bot import send_telegram_message, send_telegram_photo
except Exception:
    def send_telegram_message(token, chat_id, text): print("[TEXT]", text); return True, "local"
    def send_telegram_photo(token, chat_id, caption, photo): print("[PHOTO]", caption, photo); return True, "local"

# Config
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
ALERT_FILE = os.path.join(TMPDIR, "dsr_simple.json")

# Symbol Mappings
SYMBOL_MAP = {
    "V75(1s)": "1HZ75V",
    "V100(1s)": "1HZ100V", 
    "V150(1s)": "1HZ150V",
}

# Data Fetching

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

# Persistence
def load_persist():
    try:
        return json.load(open(ALERT_FILE))
    except:
        return {}

def save_persist(d):
    try:
        json.dump(d, open(ALERT_FILE,"w"))
    except:
        pass

def already_sent(symbol, tf, epoch, side):
    if TEST_MODE:
        return False
    key = f"{symbol}|{tf}"
    rec = load_persist().get(key)
    return rec and rec.get("epoch") == epoch and rec.get("side") == side

def mark_sent(symbol, tf, epoch, side):
    d = load_persist()
    d[f"{symbol}|{tf}"] = {"epoch": epoch, "side": side}
    save_persist(d)

# Moving Averages
def smma(series, period):
    n = len(series)
    if n < period:
        return [None] * n
    
    result = [None] * (period - 1)
    sma = sum(series[:period]) / period
    result.append(sma)
    
    prev = sma
    for i in range(period, n):
        current = (prev * (period - 1) + series[i]) / period
        result.append(current)
        prev = current
    
    return result

def sma(series, period):
    n = len(series)
    if n < period:
        return [None] * n
    
    result = [None] * (period - 1)
    for i in range(period - 1, n):
        result.append(sum(series[i-period+1:i+1]) / period)
    
    return result

def compute_mas(candles):
    closes = [c["close"] for c in candles]
    hlc3 = [(c["high"] + c["low"] + c["close"]) / 3.0 for c in candles]
    
    # MA1 = SMMA of HLC3, period 9
    ma1 = smma(hlc3, 9)
    
    # MA2 = SMMA of Close, period 19
    ma2 = smma(closes, 19)
    
    # MA3 = SMA of MA2, period 25
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

# Pattern Recognition
def is_rejection_candle(candle):
    o, h, l, c = candle["open"], candle["high"], candle["low"], candle["close"]
    body = abs(c - o)
    total_range = h - l
    
    if total_range <= 0:
        return False, "NONE"
    
    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - l
    
    # Pin bar (long wick, small body)
    if upper_wick >= body * 1.2 and upper_wick >= total_range * 0.4:
        return True, "PIN_BAR"
    
    if lower_wick >= body * 1.2 and lower_wick >= total_range * 0.4:
        return True, "PIN_BAR"
    
    # Doji (very small body)
    if body <= total_range * 0.15:
        return True, "DOJI"
    
    return False, "NONE"

def is_bullish_candle(candle):
    return candle["close"] > candle["open"]

def is_bearish_candle(candle):
    return candle["close"] < candle["open"]

def price_touches_ma(price_high, price_low, ma_level, tolerance=0.002):
    if ma_level is None:
        return False
    
    # Check if candle high/low is within tolerance of MA
    touch_high = abs(price_high - ma_level) / ma_level <= tolerance
    touch_low = abs(price_low - ma_level) / ma_level <= tolerance
    
    return touch_high or touch_low

# Signal Detection
def detect_signal(candles, tf, symbol):
    n = len(candles)
    if n < 50:  # Need sufficient history
        return None
    
    # Current candle (just closed)
    current = candles[-1]
    
    # Previous candle (for rejection pattern)  
    if n < 2:
        return None
    previous = candles[-2]
    
    # Compute MAs
    ma1, ma2, ma3 = compute_mas(candles)
    
    # Get current MA values
    curr_ma1 = ma1[-1] if ma1[-1] is not None else None
    curr_ma2 = ma2[-1] if ma2[-1] is not None else None
    curr_ma3 = ma3[-1] if ma3[-1] is not None else None
    
    if any(v is None for v in [curr_ma1, curr_ma2, curr_ma3]):
        return None
    
    # Get previous MA values for rejection test
    prev_ma1 = ma1[-2] if len(ma1) >= 2 and ma1[-2] is not None else curr_ma1
    prev_ma2 = ma2[-2] if len(ma2) >= 2 and ma2[-2] is not None else curr_ma2
    
    current_price = current["close"]
    
    # Check for rejection at MA levels in previous candle
    rejection_at_ma1 = price_touches_ma(previous["high"], previous["low"], prev_ma1)
    rejection_at_ma2 = price_touches_ma(previous["high"], previous["low"], prev_ma2)
    
    if not (rejection_at_ma1 or rejection_at_ma2):
        return None
    
    # Check for rejection pattern in previous candle
    is_rejection, pattern = is_rejection_candle(previous)
    if not is_rejection:
        return None
    
    # Determine bias and signal
    signal_side = None
    
    # Buy signal: Price above MA3 + rejection + bullish confirmation
    if current_price > curr_ma3:
        if is_bullish_candle(current):  # Bullish confirmation
            signal_side = "BUY"
    
    # Sell signal: Price below MA3 + rejection + bearish confirmation  
    elif current_price < curr_ma3:
        if is_bearish_candle(current):  # Bearish confirmation
            signal_side = "SELL"
    
    if not signal_side:
        return None
    
    return {
        "symbol": symbol,
        "tf": tf,
        "side": signal_side,
        "pattern": pattern,
        "price": current_price,
        "ma1": curr_ma1,
        "ma2": curr_ma2,
        "ma3": curr_ma3,
        "candles": candles,
        "signal_candle_idx": n - 1
    }

# Chart Generation
def create_chart(signal):
    candles = signal["candles"]
    n = len(candles)
    chart_start = max(0, n - LAST_N_CHART)
    chart_candles = candles[chart_start:]
    
    # Recompute MAs for chart
    ma1, ma2, ma3 = compute_mas(candles)
    chart_ma1 = ma1[chart_start:]
    chart_ma2 = ma2[chart_start:]
    chart_ma3 = ma3[chart_start:]
    
    plt.style.use('dark_background')
    fig, ax = plt.subplots(figsize=(14, 8))
    fig.patch.set_facecolor('black')
    ax.set_facecolor('black')
    
    # Plot candles
    for i, candle in enumerate(chart_candles):
        o, h, l, c = candle["open"], candle["high"], candle["low"], candle["close"]
        
        color = "#00FF00" if c >= o else "#FF0000"
        edge = "#00AA00" if c >= o else "#AA0000"
        
        # Highlight signal candle
        if chart_start + i == signal["signal_candle_idx"]:
            color = "#FFFF00"
            edge = "#FFAA00"
        
        ax.add_patch(Rectangle((i - CANDLE_WIDTH/2, min(o, c)), CANDLE_WIDTH, 
                              abs(c - o), facecolor=color, edgecolor=edge, alpha=0.8))
        ax.plot([i, i], [l, h], color=edge, linewidth=1, alpha=0.8)
    
    # Plot MAs
    x_range = range(len(chart_candles))
    
    ma1_vals = [v for v in chart_ma1 if v is not None]
    ma2_vals = [v for v in chart_ma2 if v is not None]  
    ma3_vals = [v for v in chart_ma3 if v is not None]
    
    if len(ma1_vals) > 10:
        ma1_x = [i for i, v in enumerate(chart_ma1) if v is not None]
        ax.plot(ma1_x, ma1_vals, color="#FFFFFF", linewidth=2, label="MA1 (SMMA HLC3-9)", alpha=0.9)
    
    if len(ma2_vals) > 10:
        ma2_x = [i for i, v in enumerate(chart_ma2) if v is not None]
        ax.plot(ma2_x, ma2_vals, color="#00BFFF", linewidth=2, label="MA2 (SMMA Close-19)", alpha=0.9)
    
    if len(ma3_vals) > 10:
        ma3_x = [i for i, v in enumerate(chart_ma3) if v is not None]
        ax.plot(ma3_x, ma3_vals, color="#FF6347", linewidth=1.5, label="MA3 (SMA MA2-25)", alpha=0.8)
    
    # Title and formatting
    title = f"{signal['symbol']} - {signal['side']} Signal ({signal['pattern']})"
    ax.set_title(title, color='white', fontsize=12, pad=15)
    
    ax.legend(loc="upper left", facecolor='black', edgecolor='white')
    ax.grid(True, alpha=0.2, color='gray')
    ax.tick_params(colors='white')
    
    for spine in ax.spines.values():
        spine.set_color('white')
    
    plt.tight_layout()
    
    chart_file = tempfile.NamedTemporaryFile(delete=False, suffix=".png")
    plt.savefig(chart_file.name, dpi=120, bbox_inches="tight", 
                facecolor='black', edgecolor='none')
    plt.close()
    
    return chart_file.name

# Main Analysis
def run_analysis():
    signals_found = 0
    
    for shorthand, deriv_symbol in SYMBOL_MAP.items():
        try:
            tf = 300  # 5 minutes
            
            if DEBUG:
                print(f"Analyzing {shorthand}...")
            
            candles = fetch_candles(deriv_symbol, tf)
            if len(candles) < 50:
                continue
            
            signal = detect_signal(candles, tf, shorthand)
            if not signal:
                continue
            
            # Check if already sent
            signal_epoch = signal["candles"][-1]["epoch"]
            if already_sent(shorthand, tf, signal_epoch, signal["side"]):
                continue
            
            # Create message
            caption = (
                f"🎯 DSR SIGNAL\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"📊 {signal['symbol']} (5m) - {signal['side']}\n"
                f"🎨 Pattern: {signal['pattern']}\n"
                f"💰 Price: {signal['price']:.5f}\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🔵 MA1: {signal['ma1']:.5f}\n"
                f"🔵 MA2: {signal['ma2']:.5f}\n"
                f"🔴 MA3: {signal['ma3']:.5f}\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"📊 Adaptive DSR Strategy\n"
                f"🎯 Rejection + Confirmation"
            )
            
            chart_path = create_chart(signal)
            
            success, _ = send_telegram_photo(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, caption, chart_path)
            
            if success:
                mark_sent(shorthand, tf, signal_epoch, signal["side"])
                signals_found += 1
                if DEBUG:
                    print(f"Signal sent: {shorthand} {signal['side']} at {signal['price']}")
            
            try:
                os.unlink(chart_path)
            except:
                pass
                
        except Exception as e:
            if DEBUG:
                print(f"Error with {shorthand}: {e}")
    
    if DEBUG:
        print(f"Found {signals_found} signals")

if __name__ == "__main__":
    try:
        run_analysis()
    except Exception as e:
        print(f"Error: {e}")
        if DEBUG:
            traceback.print_exc()