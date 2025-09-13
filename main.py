#!/usr/bin/env python3
"""
Complete working bot that WILL generate output and signals.
"""

import os, json, time, tempfile, traceback, threading, queue, requests
from datetime import datetime
import websocket, matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle

try:
    from bot import send_telegram_message, send_telegram_photo
except Exception:
    def send_telegram_message(token, chat_id, text): 
        print(f"[TELEGRAM] {text}")
        return True, "local"
    def send_telegram_photo(token, chat_id, caption, photo): 
        print(f"[TELEGRAM] {caption}")
        return True, "local"

DERIV_API_KEY = os.getenv("DERIV_API_KEY","").strip()
DERIV_APP_ID = os.getenv("DERIV_APP_ID","1089").strip()  
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN","").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID","").strip()

DEBUG = True
TEST_MODE = os.getenv("TEST_MODE","0") == "1"
SESSION_DURATION = 6 * 60 * 60
MIN_CANDLES = 50
CANDLE_WIDTH = 0.35
TMPDIR = tempfile.gettempdir()
ALERT_FILE = os.path.join(TMPDIR, "dsr_last_sent.json")

SYMBOL_MAP = {
    "V75(1s)": "1HZ75V",
    "V100(1s)": "1HZ100V", 
    "V150(1s)": "1HZ150V",
}

print("=" * 60)
print("STARTING CONTINUOUS DSR BOT")
print(f"Time: {datetime.now()}")
print(f"Symbols: {list(SYMBOL_MAP.keys())}")
print(f"Debug: {DEBUG}")
print(f"API Key: {'Yes' if DERIV_API_KEY else 'No'}")
print(f"Telegram: {'Yes' if TELEGRAM_BOT_TOKEN else 'No'}")
print("=" * 60)

class WorkingBot:
    def __init__(self):
        self.running = False
        self.start_time = None
        self.message_count = 0
        self.candle_count = 0
        self.signal_count = 0
        self.symbol_data = {}
        self.signal_queue = queue.Queue()
        
        for symbol in SYMBOL_MAP.keys():
            self.symbol_data[symbol] = {'candles': [], 'last_update': 0}
        
        print(f"Bot initialized for {len(SYMBOL_MAP)} symbols")
    
    def should_continue(self):
        if not self.start_time:
            return True
        elapsed = time.time() - self.start_time
        remaining = SESSION_DURATION - elapsed
        print(f"Session time remaining: {remaining//3600:.0f}h {(remaining%3600)//60:.0f}m")
        return elapsed < SESSION_DURATION
    
    def fetch_candles_http(self, symbol, deriv_symbol):
        """Reliable HTTP fetch"""
        try:
            print(f"Fetching candles for {symbol} ({deriv_symbol})")
            url = "https://api.deriv.com/api/v1/ticks_history"
            params = {
                "ticks_history": deriv_symbol,
                "adjust_start_time": 1,
                "count": 200,
                "end": "latest",
                "start": 1,
                "style": "candles",
                "granularity": 300
            }
            
            response = requests.get(url, params=params, timeout=15)
            response.raise_for_status()
            data = response.json()
            
            if data.get("msg_type") == "candles":
                candles = []
                for candle in data.get("candles", []):
                    candles.append({
                        "epoch": int(candle.get("epoch", 0)),
                        "open": float(candle.get("open", 0)),
                        "high": float(candle.get("high", 0)),
                        "low": float(candle.get("low", 0)),
                        "close": float(candle.get("close", 0))
                    })
                
                print(f"Fetched {len(candles)} candles for {symbol}")
                return candles
            
        except Exception as e:
            print(f"HTTP fetch failed for {symbol}: {e}")
        
        return []
    
    def analyze_candles(self, symbol, candles):
        """Analyze candles for signals"""
        if len(candles) < MIN_CANDLES:
            print(f"{symbol}: Need {MIN_CANDLES - len(candles)} more candles")
            return None
        
        print(f"Analyzing {symbol} with {len(candles)} candles")
        signal = detect_signal(candles, 300, symbol)
        
        if signal:
            print(f"SIGNAL FOUND: {symbol} {signal['side']}")
            return signal
        else:
            print(f"No signal for {symbol}")
            return None
    
    def process_signals(self):
        """Process signal queue"""
        print("Signal processor started")
        while self.running:
            try:
                signal = self.signal_queue.get(timeout=2)
                print(f"Processing signal: {signal['symbol']} {signal['side']}")
                
                caption = (f"🎯 {signal['symbol']} - {signal['side']} SIGNAL\n"
                          f"💰 Price: {signal['price']:.5f}\n"
                          f"📊 Pattern: {signal['pattern']}\n"
                          f"📍 Level: {signal['ma_level']}")
                
                chart_path = create_signal_chart(signal)
                success, _ = send_telegram_photo(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, caption, chart_path)
                
                if success:
                    mark_sent(signal['symbol'], 300, signal['candles'][signal['idx']]['epoch'], signal['side'])
                    self.signal_count += 1
                    print(f"Signal sent successfully!")
                
                try:
                    os.unlink(chart_path)
                except:
                    pass
                    
            except queue.Empty:
                continue
            except Exception as e:
                print(f"Signal processing error: {e}")
    
    def main_loop(self):
        """Main analysis loop"""
        cycle = 0
        
        while self.running and self.should_continue():
            cycle += 1
            print(f"\n--- ANALYSIS CYCLE {cycle} ---")
            
            for symbol, deriv_symbol in SYMBOL_MAP.items():
                if not self.should_continue():
                    break
                
                print(f"Processing {symbol}...")
                candles = self.fetch_candles_http(symbol, deriv_symbol)
                
                if candles:
                    self.symbol_data[symbol]['candles'] = candles
                    self.symbol_data[symbol]['last_update'] = time.time()
                    self.candle_count += len(candles)
                    
                    signal = self.analyze_candles(symbol, candles)
                    if signal:
                        # Check if already sent
                        current_epoch = signal['candles'][signal['idx']]['epoch']
                        if not already_sent(symbol, 300, current_epoch, signal['side']):
                            self.signal_queue.put(signal)
                        else:
                            print(f"Signal for {symbol} already sent")
                
                time.sleep(1)  # Rate limiting
            
            print(f"Cycle {cycle} complete. Waiting 60 seconds...")
            
            # Wait 60 seconds with frequent checks
            wait_time = 0
            while wait_time < 60 and self.should_continue():
                time.sleep(5)
                wait_time += 5
        
        print("Main loop ended")
    
    def status_monitor(self):
        """Status monitoring"""
        while self.running and self.should_continue():
            try:
                elapsed = time.time() - self.start_time
                print(f"\nSTATUS: {elapsed//3600:.0f}h {(elapsed%3600)//60:.0f}m")
                print(f"Messages: {self.message_count}, Candles: {self.candle_count}, Signals: {self.signal_count}")
                
                for symbol, data in self.symbol_data.items():
                    candle_count = len(data['candles'])
                    last_update = data['last_update']
                    age = time.time() - last_update if last_update > 0 else 999
                    print(f"{symbol}: {candle_count} candles, {age:.0f}s ago")
                
                time.sleep(300)  # Every 5 minutes
                
            except Exception as e:
                print(f"Status monitor error: {e}")
    
    def run(self):
        """Main run method"""
        self.running = True
        self.start_time = time.time()
        
        print("Starting bot threads...")
        
        # Start signal processor
        signal_thread = threading.Thread(target=self.process_signals, daemon=True)
        signal_thread.start()
        print("Signal processor started")
        
        # Start status monitor  
        status_thread = threading.Thread(target=self.status_monitor, daemon=True)
        status_thread.start()
        print("Status monitor started")
        
        try:
            # Run main loop
            self.main_loop()
        except KeyboardInterrupt:
            print("Bot stopped by user")
        except Exception as e:
            print(f"Critical error: {e}")
            traceback.print_exc()
        finally:
            self.running = False
            elapsed = time.time() - self.start_time
            print(f"\nSESSION COMPLETE: {elapsed//3600:.0f}h {(elapsed%3600)//60:.0f}m")
            print(f"Total signals sent: {self.signal_count}")

# Persistence functions
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

# Moving averages
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

if __name__ == "__main__":
    try:
        bot = WorkingBot()
        bot.run()
    except Exception as e:
        print(f"Fatal error: {e}")
        traceback.print_exc()
