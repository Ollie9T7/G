# sensors/calibrate_hx711_humid.py
"""
CLI helper to calibrate the humidifier reservoir HX711 using a known mass.
Matches the UI flow for the humidifier scale calibration page.
"""
#!/usr/bin/env python3
import os, json, time, statistics
import RPi.GPIO as GPIO
from hx711 import HX711

from sensors.scale import HUMID_DT_PIN, HUMID_SCK_PIN, HUMID_CAL_PATH


# Where the calibration is stored
CONFIG_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config")
CAL_PATH = HUMID_CAL_PATH


def read_counts(hx, n=25):
    vals = hx.get_raw_data(n)
    vals = [int(v) for v in vals if v is not None]
    return statistics.median(vals) if vals else None


def main():
    GPIO.setwarnings(False)
    hx = HX711(dout_pin=HUMID_DT_PIN, pd_sck_pin=HUMID_SCK_PIN, channel="A", gain=128)
    try:
        hx.reset()
        print("Ensure the humidifier reservoir scale is EMPTY. Measuring baseline…")
        time.sleep(2)
        baseline = read_counts(hx, 35)
        if baseline is None:
            raise SystemExit("No readings. Check wiring/power.")
        print(f"baseline_counts={baseline}")

        known = float(input("Place a known mass and enter its weight in kg (e.g. 1.5): ").strip())
        if known <= 0:
            raise SystemExit("Known mass must be > 0 kg")

        time.sleep(2)
        loaded = read_counts(hx, 35)
        if loaded is None:
            raise SystemExit("No readings under load. Check wiring/mechanics.")
        print(f"loaded_counts={loaded}")

        delta = loaded - baseline
        counts_per_kg = delta / known   # may be negative depending on wiring polarity

        cal = {
            "dt_pin": HUMID_DT_PIN,
            "sck_pin": HUMID_SCK_PIN,
            "baseline_counts": float(baseline),
            "counts_per_kg": float(counts_per_kg),
        }
        with open(CAL_PATH, "w") as f:
            json.dump(cal, f, indent=2)
        print("Saved calibration →", CAL_PATH, cal)
    finally:
        GPIO.cleanup()


if __name__ == "__main__":
    main()
