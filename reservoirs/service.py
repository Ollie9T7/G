# reservoirs/service.py
# -*- coding: utf-8 -*-
import time
import threading
from typing import Optional, Dict, Any
from time import monotonic as _mono

from global_settings import load_global_settings
from devices import _set_agitator, _set_concentrate_mix, _set_nutrient_a, _set_nutrient_b

# NEW: capacity & conversion helpers
from global_settings import usable_capacity_kg, water_kg_from_gross


from flask import current_app

# ───────────────────────── Dosing generation & cancel ─────────────────────────
DOSE_CANCEL = threading.Event()
DOSE_GEN = 0                        # monotonically increasing generation id
DOSE_GEN_LOCK = threading.Lock()    # guard updates to DOSE_GEN

def current_gen() -> int:
    return DOSE_GEN

def bump_gen() -> int:
    global DOSE_GEN
    with DOSE_GEN_LOCK:
        DOSE_GEN += 1
        return DOSE_GEN

def clear_dose_cancel_flag():
    """Clear cancel + UI hints."""
    DOSE_CANCEL.clear()
    try:
        ctx = current_app.config.get("CTX") or {}
        sd  = ctx.get("status_data") or {}
        sd.pop("reservoir_dose_cancel", None)
        sd.pop("dosing_cancelled", None)
    except Exception:
        pass

def cancel_current_dose_immediately():
    """Trip cancel; hardware OFF is done in routes.stop and finally blocks."""
    DOSE_CANCEL.set()

def _sleep_until(deadline_mono: float):
    """Cancel-aware sleep with ~5 ms slices for fast reaction."""
    while True:
        if DOSE_CANCEL.is_set():
            return
        now = _mono()
        if now >= deadline_mono:
            return
        dt = deadline_mono - now
        time.sleep(dt if dt < 0.005 else 0.005)






def _sd():
    try:
        return current_app.config["CTX"]["status_data"]
    except Exception:
        return {}


CAL_PATH = "config/nutrient_cal.json"

def _load_cal() -> Dict[str, Any]:
    import json, os
    if not os.path.exists(CAL_PATH):
        return {"A": {"ml_per_s": None, "last_cal": None}, "B": {"ml_per_s": None, "last_cal": None}}
    with open(CAL_PATH, "r") as f:
        return json.load(f)

def _save_cal(data: Dict[str, Any]) -> None:
    import json, os, tempfile, shutil
    os.makedirs("config", exist_ok=True)
    tmp = tempfile.mktemp(prefix="nutcal_", dir="config")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    shutil.move(tmp, CAL_PATH)

def compute_fill_status(res_gross_kg: Optional[float]) -> Dict[str, Any]:
    """
    Kept for compatibility if you want to call it elsewhere.
    Not used directly by the current routes.py.
    """
    gs = load_global_settings()
    target_l = float(gs.get("reservoir_target_liters", 0.0) or 0.0)

    # If we don't have a reading or capacity is invalid, return empty-ish status
    cap_kg = usable_capacity_kg(gs)
    if res_gross_kg is None or cap_kg <= 0:
        return {"percent": None, "litres_to_go": None, "fine": None}

    # Convert gross scale reading -> net water kg using global helper
    water_kg = max(0.0, water_kg_from_gross(res_gross_kg, gs))

    pct = 0.0 if cap_kg == 0 else max(0.0, min(100.0, 100.0 * (water_kg / cap_kg)))
    target_kg  = min(cap_kg, target_l) if target_l > 0 else cap_kg
    to_go_kg   = max(0.0, target_kg - water_kg)
    fine = {
        "window_kg": 1.0,
        "pos_kg": max(0.0, min(1.0, (water_kg - (target_kg - 0.5)) / 1.0)),  # 0..1, 0.5 sweet spot
        "under_label": "Under filled",
        "over_label": "Over filled"
    }
    return {"percent": round(pct, 1), "litres_to_go": round(to_go_kg, 2), "fine": fine}

def dose_from_profile(profile: Dict[str, Any], litres: float) -> Dict[str, float]:
    """
    Given a profile.nutrients spec {A:{ml_for, ml}, B:{...}} and actual litres,
    return the ml required for each pump.
    """
    out = {"A": 0.0, "B": 0.0}
    n = (profile or {}).get("nutrients", {})
    for k in ("A","B"):
        spec = n.get(k) or {}
        ml_for = float(spec.get("ml_for", 0) or 0)
        ml = float(spec.get("ml", 0) or 0)
        ml_per_l = (ml / ml_for) if ml_for > 0 else 0.0
        out[k] = round(ml_per_l * float(litres), 2)
    return out

def run_agitator(seconds: float) -> None:
    """Exact-duration agitator run using monotonic time."""
    t_end = _mono() + max(0.0, float(seconds))
    _set_agitator(True)
    try:
        while True:
            remaining = t_end - _mono()
            if remaining <= 0:
                break
            time.sleep(remaining if remaining < 0.02 else 0.02)
    finally:
        _set_agitator(False)


def run_concentrate_mix(seconds: float) -> None:
    """Exact-duration run for the concentrate mix relay (GPIO pin 7)."""
    t_end = _mono() + max(0.0, float(seconds))
    _set_concentrate_mix(True)
    try:
        while True:
            remaining = t_end - _mono()
            if remaining <= 0:
                break
            time.sleep(remaining if remaining < 0.02 else 0.02)
    finally:
        _set_concentrate_mix(False)

# ── Precise, no-overlap dosing helpers ─────────────────────────────────────

_DOSE_LOCK = threading.Lock()

def _seconds_for(letter: str, ml: float) -> float:
    cal = _load_cal() or {}
    mlps = float((cal.get(letter) or {}).get("ml_per_s") or 0.0)
    if mlps <= 0.0 or ml <= 0.0:
        return 0.0
    return float(ml) / mlps
    
def plan_seconds_for_ml(ml_a: float, ml_b: float) -> Dict[str, float]:
    """
    Compute planned durations for A/B (in seconds) from current calibration,
    without running any hardware.
    """
    a = _seconds_for("A", float(ml_a or 0.0))
    b = _seconds_for("B", float(ml_b or 0.0))
    return {"A_seconds": round(a, 3), "B_seconds": round(b, 3)}




def _run_exact(letter: str, seconds: float) -> None:
    """Run one pump for an exact duration using monotonic end-time targeting."""
    if seconds <= 0:
        return

    ctx = current_app.config.get("CTX") or {}
    sd  = ctx.get("status_data") or {}

    # Latch the current generation; if it changes mid-run, we abort state writes
    my_gen = current_gen()

    # Mark phase + start timestamp + set ON flag for the correct pump
    try:
        sd["dosing_running"] = True
        if letter == "A":
            sd["dosing_phase"] = "A"
            sd["dosing_phase_started_at"] = time.time()
            sd["nutrient_A_on"] = True
        else:
            sd["dosing_phase"] = "B"
            sd["dosing_phase_started_at"] = time.time()
            sd["nutrient_B_on"] = True
    except Exception:
        pass

    t_end = _mono() + float(seconds)

    if letter == "A":
        _set_nutrient_a(True)
    else:
        _set_nutrient_b(True)

    try:
        # cancel-aware wait
        _sleep_until(t_end)
    finally:
        # Always turn hardware OFF for this phase
        if letter == "A":
            _set_nutrient_a(False)
            sd["nutrient_A_on"] = False
        else:
            _set_nutrient_b(False)
            sd["nutrient_B_on"] = False

        # If generation changed during our run, don't touch any other state
        if my_gen != current_gen():
            return







def run_dose(ml_a: float, ml_b: float) -> Dict[str, float]:
    """
    Run nutrient pumps to deliver ml_a / ml_b with strict sequencing and a global lock.
    Returns seconds used for each pump.
    """
    sd = _sd()
    # Fresh run must not inherit a previous cancel
    clear_dose_cancel_flag()
    # Latch the current generation at the start of this run
    my_gen = current_gen()

    with _DOSE_LOCK:
        dur_a = _seconds_for("A", ml_a)
        dur_b = _seconds_for("B", ml_b)

        # Mark run started (routes.py also marks, duplicate is harmless)
        try:
            sd["dosing_running"] = True
            sd["dosing_phase"] = None
            sd["dosing_phase_started_at"] = None
        except Exception:
            pass

        # Strictly sequential: A then B (no overlap)
        if dur_a > 0 and not DOSE_CANCEL.is_set() and my_gen == current_gen():
            _run_exact("A", dur_a)
            if DOSE_CANCEL.is_set():
                try:
                    sd["dosing_phase"] = None
                    sd["dosing_phase_started_at"] = None
                    sd["dosing_running"] = False
                except Exception:
                    pass
                return {"A_seconds": round(dur_a, 3), "B_seconds": 0.0}

        if dur_b > 0 and not DOSE_CANCEL.is_set() and my_gen == current_gen():
            _run_exact("B", dur_b)
            if DOSE_CANCEL.is_set():
                try:
                    sd["dosing_phase"] = None
                    sd["dosing_phase_started_at"] = None
                    sd["dosing_running"] = False
                except Exception:
                    pass
                return {"A_seconds": round(dur_a, 3), "B_seconds": round(dur_b, 3)}





        # Clear flags at the end only if still the active generation
        if my_gen == current_gen():
            try:
                sd["dosing_phase"] = None
                sd["dosing_phase_started_at"] = None
                sd["dosing_running"] = False
            except Exception:
                pass




        return {"A_seconds": round(dur_a, 3), "B_seconds": round(dur_b, 3)}




# ── Thin wrappers to match routes.py expectations ─────────────────────────

def run_dose_ml(ml_a: float, ml_b: float, logger=None) -> Dict[str, float]:
    """
    Wrapper so routes.py can call service.run_dose_ml(...).
    We intentionally DO NOT log here to avoid duplicate logs with routes.py.
    """
    return run_dose(ml_a, ml_b)

def run_agitator_seconds(seconds: float) -> None:
    """
    Wrapper so routes.py can call service.run_agitator_seconds(...).
    """
    run_agitator(seconds)


def run_concentrate_mix_seconds(seconds: float) -> None:
    """Wrapper so routes.py can trigger the concentrate mix relay."""
    run_concentrate_mix(seconds)


