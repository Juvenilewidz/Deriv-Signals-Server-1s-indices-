#!/usr/bin/env python3
"""
main.py — Dynamic Support & Resistance (Simplified - NO MISSED SIGNALS)

Simple, effective DSR strategy:
- MA1 (SMMA HLC3-9) and MA2 (SMMA Close-19) = Dynamic S/R  
- MA3 (SMA MA2-25) = Trend filter
- Any rejection candle at MA1/MA2 = Signal
- Above MA3 = Buy bias, Below MA3 = Sell bias
"""

import os, json, time, tempfile, traceback
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
    "V10": "R_10",
    "V50": "R_50", 
    "V75": "R_75",
    "Jump10": "JD10",
    "Jump25": "JD25",
    "Jump50": "JD50", 
    "Jump100": "JD100",
    "V75(1s)": "R_1HZ75V",
    "V100(1s)": "R_1HZ100V",
    "V150(1s)": "R_1HZ150V",
    "V15(1s)": "R_1HZ15V"
}

SYMBOL_TF_MAP = {
    "V75(1s)": 1,
    "V100(1s)": 1,  
    "V150(1s)": 1,
    "V15(1s)": 1
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

def already_sent(shorthand, tf, epoch, side):
    if TEST_MODE:
        return False
    rec = load_persist().get(f"{shorthand}|{tf}")
    return bool(rec and rec.get("epoch")==epoch and rec.get("side")==side)

def mark_sent(shorthand, tf, epoch, side):
    d=load_persist(); d[f"{shorthand}|{tf}"]={"epoch":epoch,"side":side}; save_persist(d)

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
# Simple Trend Detection
# -------------------------
def get_trend_bias(price, ma1_val, ma2_val, ma3_val):
    """Enhanced trend bias with MA alignment check - prevents spam"""
    if not all(v is not None for v in [ma1_val, ma2_val, ma3_val]):
        return "UNDEFINED"
    
    # Check for proper MA alignment to confirm trend strength
    uptrend_alignment = ma1_val > ma2_val > ma3_val
    downtrend_alignment = ma1_val < ma2_val < ma3_val
    
    # MA3 filter + alignment requirement
    if price > ma3_val and uptrend_alignment:
        return "BUY_BIAS"
    elif price < ma3_val and downtrend_alignment:
        return "SELL_BIAS"
    else:
        return "UNCLEAR"  # No signals in unclear trends

# -------------------------
# Simple Rejection Detection
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
    has_small_body = body_size < total_range * 0.7  # Body less than 70% of range
    
    # Rejection criteria - very lenient
    if has_upper_wick and (upper_wick >= body_size * 0.5 or has_small_body):
        return True, "UPPER_REJECTION"
    
    if has_lower_wick and (lower_wick >= body_size * 0.5 or has_small_body):
        return True, "LOWER_REJECTION"
    
    if has_small_body and (has_upper_wick or has_lower_wick):
        return True, "SMALL_BODY_REJECTION"
    
    return False, "NONE"

# -------------------------
# Simple DSR Zone Check
# -------------------------
def near_ma_levels(price, ma1_val, ma2_val):
    """Check if price is near MA1 or MA2 - very lenient"""
    if ma1_val is None or ma2_val is None:
        return False, "NONE"
    
    # Simple percentage-based proximity - generous thresholds
    tolerance1 = abs(ma1_val) * 0.01  # 1% around MA1
    tolerance2 = abs(ma2_val) * 0.01  # 1% around MA2
    
    if abs(price - ma1_val) <= tolerance1:
        return True, "MA1"
    
    if abs(price - ma2_val) <= tolerance2:
        return True, "MA2"
    
    return False, "NONE"

# -------------------------
# Data Fetching
# -------------------------
def fetch_candles(sym, tf, count=CANDLES_N):
    """Fetch candles from Deriv API"""
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

# -------------------------
# SIMPLE Signal Detection
# -------------------------
def detect_signal(candles, tf, shorthand):
    """Anti-spam DSR signal detection - only quality setups"""
    n = len(candles)
    if n < MIN_CANDLES:
        return None
    
    # Use last candle (most recent)
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
    
    # ENHANCED trend bias with alignment check
    trend_bias = get_trend_bias(current_close, current_ma1, current_ma2, current_ma3)
    if trend_bias not in ["BUY_BIAS", "SELL_BIAS"]:
        if DEBUG:
            print(f"No clear trend for {shorthand} - trend bias: {trend_bias}")
        return None
    
    # STRICTER rejection pattern check
    is_rejection, pattern_type = is_rejection_candle(current_candle)
    if not is_rejection:
        return None
    
    # TIGHTER MA level proximity check
    near_ma_high, ma_level_high = near_ma_levels(current_high, current_ma1, current_ma2)
    near_ma_low, ma_level_low = near_ma_levels(current_low, current_ma1, current_ma2)
    near_ma_close, ma_level_close = near_ma_levels(current_close, current_ma1, current_ma2)
    
    # QUALITY FILTER: Must have clear interaction with MA levels
    has_ma_interaction = near_ma_high or near_ma_low or near_ma_close
    if not has_ma_interaction:
        if DEBUG:
            print(f"No MA interaction for {shorthand} - price too far from dynamic S/R")
        return None
    
    signal_side = None
    ma_level = "NONE"
    
    # BUY SIGNAL: Strong buy bias + meaningful rejection at MA support
    if trend_bias == "BUY_BIAS":
        if near_ma_low and pattern_type in ["LOWER_REJECTION", "DOJI_REJECTION", "INDECISION_REJECTION"]:
            signal_side = "BUY"
            ma_level = ma_level_low
        elif near_ma_close and pattern_type in ["LOWER_REJECTION", "DOJI_REJECTION"]:
            signal_side = "BUY"
            ma_level = ma_level_close
    
    # SELL SIGNAL: Strong sell bias + meaningful rejection at MA resistance
    elif trend_bias == "SELL_BIAS":
        if near_ma_high and pattern_type in ["UPPER_REJECTION", "DOJI_REJECTION", "INDECISION_REJECTION"]:
            signal_side = "SELL"
            ma_level = ma_level_high
        elif near_ma_close and pattern_type in ["UPPER_REJECTION", "DOJI_REJECTION"]:
            signal_side = "SELL"
            ma_level = ma_level_close
    
    if signal_side:
        if DEBUG:
            print(f"QUALITY DSR Signal: {signal_side} - {pattern_type} at {ma_level} (Clear {trend_bias})")
        
        return {
            "symbol": shorthand,
            "tf": tf,
            "side": signal_side,
            "pattern": pattern_type,
            "bias": trend_bias,
            "ma_level": ma_level,
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
    
    if DEBUG:
        print(f"Signal rejected for {shorthand} - not meeting quality criteria")
    
    return None

# -------------------------
# Chart Generation
# -------------------------
def create_signal_chart(signal_data):
    """Create chart for signal visualization"""
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
    
    # Plot moving averages
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
    plt.style.use('default')
    
    return chart_file.name

# -------------------------
# Main Execution
# -------------------------
def get_timeframe_for_symbol(shorthand):
    return SYMBOL_TF_MAP.get(shorthand, TIMEFRAMES[0] if TIMEFRAMES else 300)

def run_analysis():
    """Simple analysis - catch all valid DSR setups"""
    signals_found = 0
    
    for shorthand, deriv_symbol in SYMBOL_MAP.items():
        try:
            tf = get_timeframe_for_symbol(shorthand)
            
            if DEBUG:
                tf_display = f"{tf}s" if tf < 60 else f"{tf//60}m"
                print(f"Analyzing {shorthand} on {tf_display}...")
            
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
            
            # Create alert message
            tf_display = f"{tf}s" if tf < 60 else f"{tf//60}m"
            bias_emoji = "📈" if signal["bias"] == "BUY_BIAS" else "📉"
            
            caption = (f"🎯 {signal['symbol']} {tf_display} - {signal['side']} SIGNAL\n"
                      f"{bias_emoji} Bias: {signal['bias'].replace('_', ' ')}\n" 
                      f"🎨 Pattern: {signal['pattern']}\n"
                      f"📍 Level: {signal['ma_level']} Dynamic S/R\n"
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
