# reservoirs/calibration.py
# -*- coding: utf-8 -*-
from typing import Dict, Any
from time import monotonic as _mono
import time, json, os, tempfile, shutil
from devices import _set_nutrient_a, _set_nutrient_b

CAL_PATH = "config/nutrient_cal.json"

def _load() -> Dict[str, Any]:
    if not os.path.exists(CAL_PATH):
        return {"A": {"ml_per_s": None, "last_cal": None}, "B": {"ml_per_s": None, "last_cal": None}}
    with open(CAL_PATH, "r") as f:
        return json.load(f)

def _save(d: Dict[str, Any]) -> None:
    os.makedirs("config", exist_ok=True)
    tmp = tempfile.mktemp(prefix="nutcal_", dir="config")
    with open(tmp, "w") as f:
        json.dump(d, f, indent=2)
    shutil.move(tmp, CAL_PATH)

def prime(pump: str, on: bool) -> None:
    """
    Instant ON/OFF toggle for priming (no timing/lag).
    pump: "A" or "B"
    """
    p = (pump or "").strip().upper()
    if p == "A":
        _set_nutrient_a(bool(on))
    elif p == "B":
        _set_nutrient_b(bool(on))

def run_for_seconds(pump: str, seconds: float) -> None:
    """
    Accurate, blocking run for a given duration.
    """
    dur = max(0.0, float(seconds))
    t_end = _mono() + dur
    prime(pump, True)
    try:
        while _mono() < t_end:
            time.sleep(0.01)
    finally:
        prime(pump, False)

def record_measurement(pump: str, seconds: float, measured_ml: float) -> Dict[str, Any]:
    """
    Save calibration: ml_per_s = measured_ml / seconds (if valid).
    Returns the full calibration mapping.
    """
    d = _load()
    sec = float(seconds or 0.0)
    ml = float(measured_ml or 0.0)
    rate = (ml / sec) if sec > 0 else None

    if rate and rate > 0:
        from datetime import datetime, timezone
        p = (pump or "").strip().upper()
        d[p] = {"ml_per_s": round(rate, 4), "last_cal": datetime.now(timezone.utc).isoformat()}
        _save(d)

    return d



