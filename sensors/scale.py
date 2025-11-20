# sensors/scale.py
import os, json, statistics, threading
import RPi.GPIO as GPIO
GPIO.setwarnings(False)

try:
    from hx711 import HX711
except Exception as e:
    raise RuntimeError("HX711 library not installed or import failed") from e

# Where the calibration is stored
CONFIG_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config")
CAL_PATH = os.path.join(CONFIG_DIR, "scale_cal.json")
HUMID_CAL_PATH = os.path.join(CONFIG_DIR, "humid_res_scale_cal.json")

# BCM pins for the HX711 boards
MAIN_DT_PIN  = 16
MAIN_SCK_PIN = 12

# Humidifier reservoir HX711 pins
HUMID_DT_PIN  = 13
HUMID_SCK_PIN = 19


# Shared locks so HX711 access is serialized
_SCALE_LOCK = threading.RLock()
_HUMID_SCALE_LOCK = threading.RLock()

def _load_scale_cal(path: str = CAL_PATH):
    """Return calibration dict or None if missing/invalid."""
    try:
        with open(path, "r") as f:
            cal = json.load(f)
        # minimal sanity
        _ = float(cal["baseline_counts"])
        _ = float(cal["counts_per_kg"])
        # Optional pins for documentation; the HX711 object is still created with BCM GPIO numbers
        return cal
    except Exception:
        return None

def _load_humid_scale_cal():
    return _load_scale_cal(HUMID_CAL_PATH)

def _open_hx(dt_pin: int | None = None, sck_pin: int | None = None):
    """
    Open and return an HX711 instance (BCM pin numbers).
    You can hardcode the pins here or read them from calibration if you stored them.
    """
    # Default pins (match your calibrate_hx711.py)
    dt = MAIN_DT_PIN if dt_pin is None else dt_pin
    sck = MAIN_SCK_PIN if sck_pin is None else sck_pin
    hx = HX711(dout_pin=dt, pd_sck_pin=sck, channel="A", gain=128)
    hx.reset()
    return hx

def _read_counts_n(hx, n=15):
    """
    Return a median of n raw counts, supporting several hx711 APIs.
    """
    # Preferred: batch list
    if hasattr(hx, "get_raw_data"):
        vals = hx.get_raw_data(n)
        if vals:
            try:
                vals = [int(v) for v in vals if v is not None]
            except Exception:
                vals = [v for v in vals if isinstance(v, (int, float))]
            if vals:
                return statistics.median(vals)
    # Fallbacks: single numeric mean
    if hasattr(hx, "get_raw_data_mean"):
        return float(hx.get_raw_data_mean(n))
    if hasattr(hx, "get_data_mean"):
        return float(hx.get_data_mean(n))
    raise RuntimeError("HX711 library missing raw read methods")


def _scale_read_counts_for_pins(dt_pin: int, sck_pin: int, n=8, lock=None):
    """
    Thread-safe median of raw counts from HX711.
    Opens a device, reads, and returns the median.
    """
    lock_obj = lock or _SCALE_LOCK
    with lock_obj:
        hx = _open_hx(dt_pin=dt_pin, sck_pin=sck_pin)
        try:
            return _read_counts_n(hx, n=n)
        finally:
            try:
                if hasattr(hx, "power_down"):
                    hx.power_down()
            except Exception:
                pass
    # Do NOT call GPIO.cleanup() here; it can interfere with other devices.


def _scale_read_counts(n=8):
    return _scale_read_counts_for_pins(MAIN_DT_PIN, MAIN_SCK_PIN, n=n, lock=_SCALE_LOCK)


def _scale_read_counts_humid(n=8):
    return _scale_read_counts_for_pins(HUMID_DT_PIN, HUMID_SCK_PIN, n=n, lock=_HUMID_SCALE_LOCK)




def _read_scale_kg(*, cal_loader, reader, empty_kg: float = 0.0):
    try:
        cal = cal_loader()
        if not cal:
            return (None, None)

        baseline = float(cal["baseline_counts"])
        cpp      = float(cal["counts_per_kg"])
        empty    = float(cal.get("label_empty_kg", empty_kg))

        if cpp == 0:
            return (None, None)

        counts = reader()
        if counts is None:
            return (None, None)

        water_kg = (float(counts) - baseline) / cpp
        water_kg = max(0.0, water_kg)

        gross_kg = water_kg + empty

        return (round(water_kg, 2), round(gross_kg, 2))
    except Exception:
        return (None, None)


def read_reservoir_kg():
    """
    Returns (water_kg, gross_kg) rounded to 2dp, or (None, None) if unavailable.

    Uses your existing calibration fields:
      - baseline_counts: counts at zero (tare point)
      - counts_per_kg:   how many counts equal 1 kg
      - label_empty_kg:  OPTIONAL — weight of the empty reservoir/label to compute gross

    Formulas:
      water_kg = (counts - baseline_counts) / counts_per_kg
      gross_kg = water_kg + label_empty_kg (if provided)

    Notes:
      - water_kg is clamped to >= 0.0
      - We read a small median sample (n=6) to reduce noise
    """
    return _read_scale_kg(
        cal_loader=_load_scale_cal,
        reader=lambda: _scale_read_counts(n=6),
        empty_kg=0.0,
    )


def read_humid_reservoir_kg(empty_kg: float = 0.0):
    """Same as read_reservoir_kg but using the humidifier HX711 + calibration."""
    return _read_scale_kg(
        cal_loader=_load_humid_scale_cal,
        reader=lambda: _scale_read_counts_humid(n=6),
        empty_kg=empty_kg,
    )











