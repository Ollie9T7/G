# sensors/dht.py
import time
import threading

import board
import adafruit_dht

# --- Pin selection (override via env if you want later) ---
TOP_PIN = board.D17
BOT_PIN = board.D23

# one sensor object per probe
_dht_top = adafruit_dht.DHT22(TOP_PIN, use_pulseio=False)
_dht_bot = adafruit_dht.DHT22(BOT_PIN, use_pulseio=False)

# locks because Adafruit_DHT isn’t thread-safe
_top_lock = threading.Lock()
_bot_lock = threading.Lock()

# throttle to ~2s per sensor to avoid “RuntimeError: DHT Busy”
_next_top_ok = 0.0
_next_bot_ok = 0.0

# last-good values to return if a read is throttled or fails
_last_top = None
_last_bot = None

def _read_one(dht, lock, retries=3, backoff=0.4):
    for _ in range(retries):
        try:
            with lock:
                h = dht.humidity
            if h is not None and 0.0 <= h <= 100.0:
                return float(h)
        except RuntimeError:
            time.sleep(backoff)
        except Exception:
            # hardware unplugged or driver error → don’t crash loop
            time.sleep(backoff)
    return None

def read_humidity_top_bottom():
    """
    Returns a dict:
      { "top": float|None, "bottom": float|None, "avg": float|None }
    Respects per-sensor throttling; uses last-good if no new sample is ready.
    """
    global _next_top_ok, _next_bot_ok, _last_top, _last_bot
    now = time.time()

    top = None
    bot = None

    if now >= _next_top_ok:
        v = _read_one(_dht_top, _top_lock)
        if v is not None: _last_top = v
        _next_top_ok = now + 2.0
    top = _last_top

    if now >= _next_bot_ok:
        v = _read_one(_dht_bot, _bot_lock)
        if v is not None: _last_bot = v
        _next_bot_ok = now + 2.0
    bot = _last_bot

    avg = None
    if top is not None and bot is not None:
        avg = (top + bot) / 2.0
    elif top is not None:
        avg = top
    elif bot is not None:
        avg = bot

    return {"top": top, "bottom": bot, "avg": avg}



