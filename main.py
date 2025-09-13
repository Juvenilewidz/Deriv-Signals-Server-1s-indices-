#!/usr/bin/env python3
"""
main.py — Debug-Enhanced Continuous DSR Trading Bot

Added comprehensive debugging to identify exactly what's happening.
"""

import os, json, time, tempfile, traceback, threading, queue
from datetime import datetime, timezone, timedelta
import websocket, matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle

# Telegram helpers (fallback to print)
try:
    from bot import send_telegram_message, send_telegram_photo
except Exception:
    def send_telegram_message(token, chat_id, text): print(f"[TELEGRAM] {text}"); return True, "local"
    def send_telegram_photo(token, chat_id, caption, photo): print(f"[TELEGRAM] {caption}"); return True, "local"

# Config
DERIV_API_KEY = os.getenv("DERIV_API_KEY","").strip()
DERIV_APP_ID  = os.getenv("DERIV_APP_ID","1089").strip()
DERIV_WS_URL  = f"wss://ws.derivws.com/websockets/v3?app_id={DERIV_APP_ID}"
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN","").strip()
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID","").strip()

DEBUG = True  # Force debug mode
TEST_MODE = os.getenv("TEST_MODE","0") == "1"

SESSION_DURATION = 6 * 60 * 60
CANDLES_BUFFER_SIZE = 500
MIN_CANDLES = 50
CANDLE_WIDTH = 0.35
TMPDIR = tempfile.gettempdir()
ALERT_FILE = os.path.join(TMPDIR, "dsr_last_sent_continuous.json")

SYMBOL_MAP = {
    "V75(1s)": "1HZ75V",
    "V100(1s)": "1HZ100V", 
    "V150(1s)": "1HZ150V",
}

print(f"[STARTUP] Bot starting with symbols: {SYMBOL_MAP}")
print(f"[STARTUP] WebSocket URL: {DERIV_WS_URL}")
print(f"[STARTUP] Has API Key: {'Yes' if DERIV_API_KEY else 'No'}")

class ContinuousTradingBot:
    def __init__(self):
        self.ws = None
        self.running = False
        self.start_time = None
        self.symbol_data = {}
        self.signal_queue = queue.Queue()
        self.last_signals = {}
        self.message_count = 0
        self.candle_count = 0
        self.subscription_count = 0
        
        for symbol in SYMBOL_MAP.keys():
            self.symbol_data[symbol] = {
                'candles': [],
                'subscribed': False,
                'last_candle_time': 0,
                'historical_loaded': False
            }
        print(f"[INIT] Initialized data storage for {len(SYMBOL_MAP)} symbols")
    
    def should_continue(self):
        if not self.start_time:
            return True
        elapsed = time.time() - self.start_time
        return elapsed < SESSION_DURATION
    
    def log_status(self):
        elapsed = time.time() - self.start_time if self.start_time else 0
        print(f"\n[STATUS] Runtime: {elapsed//3600:.0f}h {(elapsed%3600)//60:.0f}m")
        print(f"[STATUS] Messages received: {self.message_count}")
        print(f"[STATUS] Candles processed: {self.candle_count}")
        print(f"[STATUS] Subscriptions made: {self.subscription_count}")
        
        for symbol, data in self.symbol_data.items():
            candle_count = len(data['candles'])
            subscribed = "✓" if data['subscribed'] else "✗"
            historical = "✓" if data['historical_loaded'] else "✗"
            last_time = data['last_candle_time']
            age = time.time() - last_time if last_time > 0 else 0
            print(f"[STATUS] {symbol}: {candle_count} candles, Sub: {subscribed}, Hist: {historical}, Age: {age:.0f}s")
    
    def add_candle(self, symbol, candle_data):
        if symbol not in self.symbol_data:
            print(f"[ERROR] Unknown symbol: {symbol}")
            return
        
        try:
            candle = {
                "epoch": int(candle_data.get("epoch", 0)),
                "open": float(candle_data.get("open", 0)),
                "high": float(candle_data.get("high", 0)),
                "low": float(candle_data.get("low", 0)),
                "close": float(candle_data.get("close", 0))
            }
            
            if candle["epoch"] <= 0 or candle["close"] <= 0:
                print(f"[ERROR] Invalid candle data for {symbol}: {candle}")
                return
            
            candles = self.symbol_data[symbol]['candles']
            
            if candles and candles[-1]["epoch"] >= candle["epoch"]:
                print(f"[DEBUG] Duplicate candle for {symbol}, skipping")
                return
            
            candles.append(candle)
            self.candle_count += 1
            
            if len(candles) > CANDLES_BUFFER_SIZE:
                candles.pop(0)
            
            self.symbol_data[symbol]['last_candle_time'] = candle["epoch"]
            print(f"[CANDLE] {symbol}: {candle['close']:.5f} at {candle['epoch']} (Total: {len(candles)})")
            
            if len(candles) >= MIN_CANDLES:
                print(f"[ANALYZE] Analyzing {symbol} with {len(candles)} candles")
                self.analyze_symbol(symbol)
            else:
                print(f"[ANALYZE] {symbol} needs {MIN_CANDLES - len(candles)} more candles")
                
        except Exception as e:
            print(f"[ERROR] Failed to add candle for {symbol}: {e}")
            traceback.print_exc()
    
    def analyze_symbol(self, symbol):
        candles = self.symbol_data[symbol]['candles']
        if len(candles) < MIN_CANDLES:
            return
        
        print(f"[SIGNAL_CHECK] Analyzing {symbol} for signals...")
        signal = detect_signal(candles, 300, symbol)
        
        if not signal:
            print(f"[SIGNAL_CHECK] No signal found for {symbol}")
            return
        
        print(f"[SIGNAL_FOUND] {symbol}: {signal['side']} signal detected!")
        
        current_epoch = signal["candles"][signal["idx"]]["epoch"]
        last_signal_key = f"{symbol}_last_signal"
        last_time = self.last_signals.get(last_signal_key, 0)
        
        if current_epoch - last_time < 1800:
            print(f"[COOLDOWN] {symbol} signal blocked by cooldown")
            return
        
        if already_sent(symbol, 300, current_epoch, signal["side"]):
            print(f"[DUPLICATE] {symbol} signal already sent")
            return
        
        self.last_signals[last_signal_key] = current_epoch
        self.signal_queue.put(signal)
        print(f"[SIGNAL_QUEUED] {symbol} {signal['side']} signal queued")
    
    def process_signals(self):
        print("[THREAD] Signal processing thread started")
        
        while self.running:
            try:
                signal = self.signal_queue.get(timeout=5)
                print(f"[SIGNAL_SEND] Processing signal for {signal['symbol']}")
                
                arrangement_emoji = "📈" if signal["ma_arrangement"] == "BULLISH_ARRANGEMENT" else "📉"
                
                caption = (f"🎯 {signal['symbol']} M5 - {signal['side']} SIGNAL\n"
                          f"{arrangement_emoji} MA Setup: {signal['ma_arrangement'].replace('_', ' ')}\n" 
                          f"🎨 Pattern: {signal['pattern']}\n"
                          f"📍 Level: {signal['ma_level']} Dynamic S/R\n"
                          f"💰 Price: {signal['price']:.5f}\n"
                          f"📊 Context: {signal['context']}")
                
                print(f"[CHART] Creating chart for {signal['symbol']}")
                chart_path = create_signal_chart(signal)
                
                print(f"[TELEGRAM] Sending to Telegram...")
                success, msg_id = send_telegram_photo(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, caption, chart_path)
                
                if success:
                    current_epoch = signal["candles"][signal["idx"]]["epoch"]
                    mark_sent(signal['symbol'], 300, current_epoch, signal["side"])
                    print(f"[SUCCESS] Signal sent for {signal['symbol']}: {signal['side']}")
                else:
                    print(f"[ERROR] Failed to send signal for {signal['symbol']}")
                
                try:
                    os.unlink(chart_path)
                except:
                    pass
                    
            except queue.Empty:
                continue
            except Exception as e:
                print(f"[ERROR] Signal processing error: {e}")
                traceback.print_exc()
        
        print("[THREAD] Signal processing thread ended")
    
    def on_message(self, ws, message):
        self.message_count += 1
        
        try:
            data = json.loads(message)
            msg_type = data.get("msg_type", "")
            print(f"[WS_MSG #{self.message_count}] Type: {msg_type}")
            
            if msg_type == "candles":
                req_id = data.get("req_id", "")
                print(f"[WS_CANDLES] Request ID: {req_id}")
                
                symbol = self.find_symbol_by_request_id(req_id)
                if symbol:
                    candle_data = data.get("candles", [])
                    print(f"[WS_CANDLES] Loading {len(candle_data)} historical candles for {symbol}")
                    
                    for candle in candle_data:
                        self.add_candle(symbol, candle)
                    
                    self.symbol_data[symbol]['historical_loaded'] = True
                else:
                    print(f"[WS_CANDLES] Could not map request ID '{req_id}' to symbol")
            
            elif msg_type == "ohlc":
                subscription = data.get("subscription", {})
                symbol_id = subscription.get("id", "")
                print(f"[WS_OHLC] Subscription ID: {symbol_id}")
                
                symbol = self.find_symbol_by_id(symbol_id)
                
                if symbol:
                    ohlc = data.get("ohlc", {})
                    print(f"[WS_OHLC] Live candle for {symbol}: {ohlc}")
                    self.add_candle(symbol, ohlc)
                else:
                    print(f"[WS_OHLC] Could not map subscription ID '{symbol_id}' to symbol")
                    ohlc = data.get("ohlc", {})
                    if ohlc:
                        print(f"[WS_OHLC] Adding to all symbols as fallback")
                        for sym in SYMBOL_MAP.keys():
                            self.add_candle(sym, ohlc)
            
            elif msg_type == "tick":
                print(f"[WS_TICK] Tick data received (ignored)")
                pass
            
            elif msg_type == "authorize":
                print(f"[WS_AUTH] Authorization successful!")
            
            elif msg_type == "subscription":
                sub_id = data.get("subscription", {}).get("id", "")
                print(f"[WS_SUB] Subscription confirmed: {sub_id}")
            
            elif data.get("error"):
                error_msg = data.get("error", {})
                print(f"[WS_ERROR] API Error: {error_msg}")
            
            else:
                print(f"[WS_OTHER] Unknown message type: {msg_type}")
                if len(str(data)) < 200:
                    print(f"[WS_OTHER] Data: {data}")
        
        except json.JSONDecodeError as e:
            print(f"[WS_ERROR] JSON decode error: {e}")
            print(f"[WS_ERROR] Raw message: {message[:200]}...")
        except Exception as e:
            print(f"[WS_ERROR] Message processing error: {e}")
            traceback.print_exc()
    
    def on_error(self, ws, error):
        print(f"[WS_ERROR] WebSocket error: {error}")
    
    def on_close(self, ws, close_status_code, close_msg):
        print(f"[WS_CLOSE] Connection closed: {close_status_code} - {close_msg}")
        
        if self.should_continue():
            print("[WS_RECONNECT] Attempting to reconnect in 5 seconds...")
            time.sleep(5)
            self.connect()
        else:
            print("[WS_CLOSE] Session time exceeded, not reconnecting")
    
    def on_open(self, ws):
        print("[WS_OPEN] WebSocket connected successfully!")
        
        if DERIV_API_KEY:
            print("[WS_AUTH] Sending authorization...")
            auth_msg = {"authorize": DERIV_API_KEY}
            ws.send(json.dumps(auth_msg))
        else:
            print("[WS_AUTH] No API key provided")
        
        for shorthand, deriv_symbol in SYMBOL_MAP.items():
            print(f"[WS_SUB] Setting up subscriptions for {shorthand} ({deriv_symbol})")
            
            history_request = {
                "ticks_history": deriv_symbol,
                "adjust_start_time": 1,
                "count": CANDLES_BUFFER_SIZE,
                "end": "latest",
                "start": 1,
                "style": "candles",
                "granularity": 300,
                "req_id": f"history_{shorthand}"
            }
            print(f"[WS_SUB] Sending history request: {history_request}")
            ws.send(json.dumps(history_request))
            
            subscribe_request = {
                "ticks_history": deriv_symbol,
                "adjust_start_time": 1,
                "count": 1,
                "end": "latest",
                "start": 1,
                "style": "candles",
                "granularity": 300,
                "subscribe": 1,
                "req_id": f"subscribe_{shorthand}"
            }
            print(f"[WS_SUB] Sending subscription request: {subscribe_request}")
            ws.send(json.dumps(subscribe_request))
            
            self.symbol_data[shorthand]['subscribed'] = True
            self.subscription_count += 1
            time.sleep(0.5)
        
        print(f"[WS_SUB] Completed setup for {len(SYMBOL_MAP)} symbols")
    
    def find_symbol_by_request_id(self, req_id):
        if not req_id:
            return None
        
        print(f"[MAPPING] Looking for symbol with request ID: {req_id}")
        
        for symbol in SYMBOL_MAP.keys():
            if req_id in [f"history_{symbol}", f"subscribe_{symbol}"]:
                print(f"[MAPPING] Found symbol: {symbol}")
                return symbol
        
        print(f"[MAPPING] No symbol found for request ID: {req_id}")
        return None
    
    def find_symbol_by_id(self, symbol_id):
        if not symbol_id:
            return None
        
        print(f"[MAPPING] Looking for symbol with subscription ID: {symbol_id}")
        
        for shorthand, deriv_symbol in SYMBOL_MAP.items():
            if symbol_id == deriv_symbol:
                print(f"[MAPPING] Direct match found: {shorthand}")
                return shorthand
        
        for shorthand, deriv_symbol in SYMBOL_MAP.items():
            if deriv_symbol in symbol_id or symbol_id in deriv_symbol:
                print(f"[MAPPING] Partial match found: {shorthand}")
                return shorthand
        
        print(f"[MAPPING] No symbol found for subscription ID: {symbol_id}")
        return None
    
    def connect(self):
        print("[WS_CONNECT] Attempting WebSocket connection...")
        
        try:
            self.ws = websocket.WebSocketApp(
                DERIV_WS_URL,
                on_open=self.on_open,
                on_message=self.on_message,
                on_error=self.on_error,
                on_close=self.on_close
            )
            
            print("[WS_CONNECT] WebSocket app created, starting run_forever...")
            self.ws.run_forever(ping_interval=30, ping_timeout=10)
            
        except Exception as e:
            print(f"[WS_ERROR] Connection error: {e}")
            traceback.print_exc()
            
            if self.should_continue():
                print("[WS_RECONNECT] Retrying connection in 10 seconds...")
                time.sleep(10)
                self.connect()
            else:
                print("[WS_CONNECT] Session time exceeded, giving up")
    
    def status_monitor(self):
        print("[THREAD] Status monitor thread started")
        
        while self.running and self.should_continue():
            try:
                time.sleep(60)
                self.log_status()
            except Exception as e:
                print(f"[ERROR] Status monitor error: {e}")
        
        print("[THREAD] Status monitor thread ended")
    
    def run(self):
        self.running = True
        self.start_time = time.time()
        
        print("=" * 60)
        print(f"🚀 STARTING 6-HOUR CONTINUOUS DSR TRADING SESSION")
        print(f"Started at: {datetime.now()}")
        print(f"Symbols to monitor: {list(SYMBOL_MAP.keys())}")
        print("=" * 60)
        
        signal_thread = threading.Thread(target=self.process_signals, daemon=True, name="SignalProcessor")
        signal_thread.start()
        print("[THREAD] Signal processing thread started")
        
        status_thread = threading.Thread(target=self.status_monitor, daemon=True, name="StatusMonitor")
        status_thread.start()
        print("[THREAD] Status monitoring thread started")
        
        try:
            self.connect()
        except KeyboardInterrupt:
            print("\n[SHUTDOWN] Bot stopped by user")
        except Exception as e:
            print(f"[ERROR] Critical error: {e}")
            traceback.print_exc()
        finally:
            self.running = False
            elapsed = time.time() - self.start_time
            
            print("\n" + "=" * 60)
            print("✅ SESSION COMPLETED")
            print(f"Total runtime: {elapsed//3600:.0f}h {(elapsed%3600)//60:.0f}m")
            print(f"Messages processed: {self.message_count}")
            print(f"Candles processed: {self.candle_count}")
            print("Final status:")
            self.log_status()
            print("=" * 60)

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

def is_rejection_candle(candle):
    o, h, l, c = candle["open"], candle["high"], candle["low"], candle["close"]
    body_size = abs(c - o)
    total_range = h - l
    
    if total_range <= 0:
        return False, "NONE"
    
    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - l
    has_upper_wick = upper_wick > 0
    has_lower_wick = lower_wick > 0
    has_small_body = body_size < total_range * 0.7
    
    if has_upper_wick and (upper_wick >= body_size * 0.5 or has_small_body):
        return True, "UPPER_REJECTION"
    
    if has_lower_wick and (lower_wick >= body_size * 0.5 or has_small_body):
        return True, "LOWER_REJECTION"
    
    if has_small_body and (has_upper_wick or has_lower_wick):
        return True, "SMALL_BODY_REJECTION"
    
    return False, "NONE"

def check_ranging_market(candles, ma1, ma2, current_idx, lookback=10):
    if current_idx < lookback:
        return False
    
    ma2_touches = 0
    for i in range(current_idx - lookback + 1, current_idx + 1):
        if i < len(candles) and i < len(ma2) and ma2[i] is not None:
            candle = candles[i]
            ma2_val = ma2[i]
            if candle["low"] <= ma2_val <= candle["high"]:
                ma2_touches += 1
    
    return ma2_touches > 2

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
    
    if not all(v is not None for v in [current_ma1, current_ma2, current_ma3]):
        return None
    
    current_close = current_candle["close"]
    current_high = current_candle["high"]
    current_low = current_candle["low"]
    
    if current_ma1 > current_ma2:
        bias = "BUY_BIAS"
    elif current_ma1 < current_ma2:
        bias = "SELL_BIAS"
    else:
        return None
    
    if bias == "BUY_BIAS" and current_close <= current_ma1:
        return None
        
    if bias == "SELL_BIAS" and current_close >= current_ma1:
        return None
    
    if current_ma1 > current_ma2:
        if current_ma2 < current_close < current_ma1:
            return None
    else:
        if current_ma1 < current_close < current_ma2:
            return None
    
    is_ranging = check_ranging_market(candles, ma1, ma2, current_idx)
    if is_ranging:
        return None
    
    is_rejection, pattern_type = is_rejection_candle(current_candle)
    if not is_rejection:
        return None
    
    ma1_tolerance = current_ma1 * 0.001
    ma2_tolerance = current_ma2 * 0.001
    
    touched_ma1 = (abs(current_high - current_ma1) <= ma1_tolerance or 
                   abs(current_low - current_ma1) <= ma1_tolerance or 
                   abs(current_close - current_ma1) <= ma1_tolerance)
    
    touched_ma2 = (abs(current_high - current_ma2) <= ma2_tolerance or 
                   abs(current_low - current_ma2) <= ma2_tolerance or 
                   abs(current_close - current_ma2) <= ma2_tolerance)
    
    if not (touched_ma1 or touched_ma2):
        return None
    
    if touched_ma1:
        ma_level = "MA1"
    else:
        ma_level = "MA2"
    
    if bias == "BUY_BIAS":
        signal_side = "BUY"
        context = "MA1 above MA2 - uptrend confirmed"
    else:
        signal_side = "SELL" 
        context = "MA1 below MA2 - downtrend confirmed"
    
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
        "idx": current_idx,
        "candles": candles,
        "ma1_array": ma1,
        "ma2_array": ma2,
        "ma3_array": ma3
    }

def create_signal_chart(signal_data):
    candles = signal_data["candles"]
    ma1, ma2, ma3 = signal_data["ma1_array"], signal_data["ma2_array"], signal_data["ma3_array"]
    signal_idx = signal_data["idx"]
    
    n = len(candles)
    chart_start = max(0, n - 180)
    chart_candles = candles[chart_start:]
    
    plt.style.use('dark_background')
    fig, ax = plt.subplots(figsize=(14, 10))
    fig.patch.set_facecolor('black')
    ax.set_facecolor('black')
    
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
    
    def plot_ma(ma_values, label, color, linewidth=2):
        chart_ma = []
        for i in range(chart_start, n):
            if i < len(ma_values) and ma_values[i] is not None:
                chart_ma.append(ma_values[i])
            else:
                chart_ma.append(None)
        ax.plot(range(len(chart_candles)), chart_ma, 
                color=color, linewidth=linewidth, label=label, alpha=0.9)
    
    plot_ma(ma1, "MA1 (SMMA HLC3-9)", "#FFFFFF", 2)
    plot_ma(ma2, "MA2 (SMMA Close-19)", "#00BFFF", 2)
    plot_ma(ma3, "MA3 (SMA MA2-25)", "#FF6347", 2)
    
    signal_chart_idx = signal_idx - chart_start
    if 0 <= signal_chart_idx < len(chart_candles):
        signal_candle = chart_candles[signal_chart_idx]
        signal_price = signal_candle["close"]
        
        if signal_data["side"] == "BUY":
            marker_color = "#00FF00"
            marker_symbol = "^"
        else:
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

if __name__ == "__main__":
    print("[MAIN] Starting bot execution...")
    
    try:
        bot = ContinuousTradingBot()
        bot.run()
    except KeyboardInterrupt:
        print("[MAIN] Bot stopped by user")
    except Exception as e:
        print(f"[MAIN] Critical error: {e}")
        traceback.print_exc()
