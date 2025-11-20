#!/usr/bin/env python3
# global_settings.py
import os, json
import tempfile, os, json, threading, shutil, time  # ensure tempfile, threading, shutil, time imported

_LOCK = threading.RLock()
_CACHE = None           # last loaded dict
_CACHE_VERSION = 0
_CACHE_LOADED_TS = 0.0

# Where the settings live on disk (create the folder if it doesn't exist)
CONFIG_DIR = os.path.join(os.path.dirname(__file__), "config")
os.makedirs(CONFIG_DIR, exist_ok=True)
GLOBALS_PATH = os.path.join(CONFIG_DIR, "global_settings.json")

# Keys we no longer use (purged on load if found in older files)
OBSOLETE_KEYS = [
    # Old agitator (moved to profile-level long ago)
    "agitator_gpio_pin", "agitator_pre_pump_s", "agitator_run_s",
    # Old percent-based + capacity fields
    "reservoir_capacity_l", "warn_full_pct", "warn_half_pct",
    "warn_low_pct", "warn_critical_pct",
    # ❌ obsolete since premix has NO leads now
    "agitator_lead_sec", "air_pump_lead_sec",
    # NOTE: we are NOT purging 'reservoir_full_weight_kg' yet — we mirror it for back-compat.
]

# Sensible defaults; Reservoir now uses NET-water capacity for "full"
# Previous defaults: empty=5.0, full gross=65.0 → usable 60.0 kg
DEFAULTS = {
    # Control behaviour
    "hysteresis_temp_c": 0.5,
    "hysteresis_humidity_pct": 3.0,
    "hysteresis_temp_heater_c": None,
    "hysteresis_temp_extractor_c": None,
    "hysteresis_humidity_humidifier_pct": None,
    "hysteresis_humidity_extractor_pct": None,

    # Anti short-cycle (minimum ON times in seconds)
    "heater_min_on_s": 30,
    "fan_min_on_s": 15,
    "humidifier_min_on_s": 30,

    # Absolute hard limits
    "absolute_temp_min_c": 5.0,
    "absolute_temp_max_c": 40.0,
    "absolute_humidity_min_pct": 10.0,
    "absolute_humidity_max_pct": 95.0,

    # Reservoir (kg)
    "reservoir_empty_weight_kg": 5.0,      # tank + fittings with no water
    "reservoir_full_capacity_kg": 60.0,    # ★ NET water at your chosen "full" line

    # Humidifier reservoir (kg)
    "humid_res_empty_weight_kg": 1.0,
    "humid_res_full_capacity_kg": 5.0,

    # Humidifier thresholds (kg of water; referenced to empty)
    "humid_res_half_water_kg": 2.5,
    "humid_res_low_water_kg": 1.2,
    "humid_res_critical_water_kg": 0.5,
    "humid_res_full_margin_kg": 0.3,

    # Thresholds are kg of water (net, referenced to empty)
    "reservoir_half_water_kg": 30.0,
    "reservoir_low_water_kg": 15.0,
    "reservoir_critical_water_kg": 6.0,
    "reservoir_pump_cutoff_water_kg": 5.0,

    # Margin to treat near-full as "full" (helps with scale noise)
    "reservoir_full_margin_kg": 1.0,

    # Premix defaults (NO LEADS)
    "agitator_enabled": False,
    "agitator_run_sec": 15,
    "air_pump_enabled": False,
    "air_pump_run_sec": 0,

    # Water temperature (°C)
    "water_temp_min_c": None,
    "water_temp_target_c": None,
    "water_temp_max_c": None,
}

def _clamp(v, lo, hi):
    return max(lo, min(hi, v))

# ---------- Small helpers (import these from other modules) ----------

def usable_capacity_kg(s: dict) -> float:
    """Usable water at 'full' (kg)."""
    return max(0.0, float(s.get("reservoir_full_capacity_kg", 0.0)))

def full_gross_weight_kg(s: dict) -> float:
    """Derived full gross weight = empty gross + net capacity (kg)."""
    return float(s.get("reservoir_empty_weight_kg", 0.0)) + usable_capacity_kg(s)

def water_kg_from_gross(gross_kg: float, s: dict) -> float:
    """Convert gross scale reading to net water kg above empty."""
    return max(0.0, float(gross_kg) - float(s.get("reservoir_empty_weight_kg", 0.0)))


# Humidifier helpers (mirror the main reservoir helpers but use humid_res_* keys)
def humid_usable_capacity_kg(s: dict) -> float:
    return max(0.0, float(s.get("humid_res_full_capacity_kg", 0.0)))


def humid_full_gross_weight_kg(s: dict) -> float:
    return float(s.get("humid_res_empty_weight_kg", 0.0)) + humid_usable_capacity_kg(s)


def humid_water_kg_from_gross(gross_kg: float, s: dict) -> float:
    return max(0.0, float(gross_kg) - float(s.get("humid_res_empty_weight_kg", 0.0)))

# --------------------------------------------------------------------

def load_global_settings():
    global _CACHE, _CACHE_VERSION, _CACHE_LOADED_TS
    with _LOCK:
        if _CACHE is not None:
            return dict(_CACHE)

        if not os.path.exists(GLOBALS_PATH):
            save_global_settings(DEFAULTS)
            return dict(DEFAULTS)

        try:
            with open(GLOBALS_PATH, "r") as f:
                data = json.load(f)
        except Exception:
            ts = int(time.time())
            bad = os.path.join(CONFIG_DIR, f"global_settings.corrupt.{ts}.json")
            try: shutil.copyfile(GLOBALS_PATH, bad)
            except Exception: pass
            data = {}

        merged = dict(DEFAULTS); merged.update(data or {})

        # --- MIGRATION: if old 'reservoir_full_weight_kg' present but no 'reservoir_full_capacity_kg',
        # derive capacity = full_gross - empty_gross.
        if "reservoir_full_capacity_kg" not in merged:
            old_full = merged.get("reservoir_full_weight_kg", None)
            empty = merged.get("reservoir_empty_weight_kg", None)
            if isinstance(old_full, (int, float)) and isinstance(empty, (int, float)):
                cap = max(0.0, float(old_full) - float(empty))
                merged["reservoir_full_capacity_kg"] = cap

        # purge obsolete keys (but we KEEP reservoir_full_weight_kg for compatibility)
        removed = False
        for k in list(merged.keys()):
            if k in OBSOLETE_KEYS:
                merged.pop(k, None); removed = True
        if removed:
            save_global_settings(merged)
            return dict(merged)

        _CACHE = dict(merged)
        _CACHE_VERSION += 1
        _CACHE_LOADED_TS = time.time()
        return dict(_CACHE)

def _atomic_write(path: str, text: str):
    d = os.path.dirname(path) or "."
    with tempfile.NamedTemporaryFile("w", dir=d, delete=False) as tmp:
        tmp.write(text)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp_path = tmp.name
    os.replace(tmp_path, path)

def save_global_settings(data: dict):
    global _CACHE, _CACHE_VERSION, _CACHE_LOADED_TS
    with _LOCK:
        # keep cache in sync before write
        _CACHE = dict(DEFAULTS); _CACHE.update(data or {})

        # ★ Back-compat mirror: always write derived 'reservoir_full_weight_kg'
        _CACHE["reservoir_full_weight_kg"] = full_gross_weight_kg(_CACHE)
        _CACHE["humid_res_full_weight_kg"] = humid_full_gross_weight_kg(_CACHE)

        payload = json.dumps(_CACHE, indent=2, sort_keys=True)
        if os.path.exists(GLOBALS_PATH):
            ts = int(time.time())
            shutil.copyfile(GLOBALS_PATH, os.path.join(CONFIG_DIR, f"global_settings.backup.{ts}.json"))
        _atomic_write(GLOBALS_PATH, payload)
        _CACHE_VERSION += 1
        _CACHE_LOADED_TS = time.time()

def validate_settings(raw: dict):
    """
    Accepts strings/numbers; returns (ok: bool, errors: list[str], cleaned: dict)
    Only enforces basic sanity. Keep it permissive so you can experiment.
    """
    errors = []
    cleaned = {}

    def as_bool(name, default=None):
        val = raw.get(name, None)
        if val is None:
            cleaned[name] = DEFAULTS.get(name) if default is None else default
            return
        if isinstance(val, bool):
            cleaned[name] = val
            return
        s = str(val).strip().lower()
        cleaned[name] = s in ("1", "true", "yes", "on", "checked")

    def as_float(name, lo=None, hi=None, required=True):
        val = raw.get(name, "")
        try:
            v = float(val)
            if lo is not None and v < lo: errors.append(f"{name} < {lo}")
            if hi is not None and v > hi: errors.append(f"{name} > {hi}")
            cleaned[name] = v
        except Exception:
            if required:
                errors.append(f"{name} must be a number")
            else:
                cleaned[name] = DEFAULTS.get(name)

    def as_int(name, lo=None, hi=None, required=True):
        val = raw.get(name, "")
        try:
            v = int(float(val))  # allow "15.0"
            if lo is not None and v < lo: errors.append(f"{name} < {lo}")
            if hi is not None and v > hi: errors.append(f"{name} > {hi}")
            cleaned[name] = v
        except Exception:
            if required:
                errors.append(f"{name} must be an integer")
            else:
                cleaned[name] = DEFAULTS.get(name)

    # Hysteresis
    as_float("hysteresis_temp_c", lo=0, hi=10)
    as_float("hysteresis_humidity_pct", lo=0, hi=30)
    as_float("hysteresis_temp_heater_c",          lo=0, hi=10, required=False)
    as_float("hysteresis_temp_extractor_c",       lo=0, hi=10, required=False)
    as_float("hysteresis_humidity_humidifier_pct",lo=0, hi=30, required=False)
    as_float("hysteresis_humidity_extractor_pct", lo=0, hi=30, required=False)

    # Anti short-cycle
    as_int("heater_min_on_s", lo=0, hi=3600)
    as_int("fan_min_on_s", lo=0, hi=3600)
    as_int("humidifier_min_on_s", lo=0, hi=3600)

    # Hard limits
    as_float("absolute_temp_min_c", lo=-20, hi=80)
    as_float("absolute_temp_max_c", lo=-20, hi=80)
    as_float("absolute_humidity_min_pct", lo=0, hi=100)
    as_float("absolute_humidity_max_pct", lo=0, hi=100)

    # Reservoir (capacity-based)
    as_float("reservoir_empty_weight_kg",   lo=0, hi=10000)
    as_float("reservoir_full_capacity_kg",  lo=0, hi=100000)

    # Reservoir thresholds (kg of water; referenced to empty)
    as_float("reservoir_half_water_kg",      lo=0, hi=100000)
    as_float("reservoir_low_water_kg",       lo=0, hi=100000)
    as_float("reservoir_critical_water_kg",  lo=0, hi=100000)
    as_float("reservoir_pump_cutoff_water_kg", lo=0, hi=100000)
    as_float("reservoir_full_margin_kg",     lo=0, hi=10000)

    # Humidifier reservoir (capacity-based)
    as_float("humid_res_empty_weight_kg",   lo=0, hi=10000)
    as_float("humid_res_full_capacity_kg",  lo=0, hi=100000)

    # Humidifier thresholds
    as_float("humid_res_half_water_kg",     lo=0, hi=100000)
    as_float("humid_res_low_water_kg",      lo=0, hi=100000)
    as_float("humid_res_critical_water_kg", lo=0, hi=100000)
    as_float("humid_res_full_margin_kg",    lo=0, hi=10000)

    # Premix (NO LEADS)
    as_bool("agitator_enabled")
    as_int("agitator_run_sec",  lo=0, hi=3600, required=False)
    as_bool("air_pump_enabled")
    as_int("air_pump_run_sec",  lo=0, hi=3600, required=False)

    # Water temperature thresholds (optional)
    as_float("water_temp_min_c",    lo=-20, hi=80, required=False)
    as_float("water_temp_target_c", lo=-20, hi=80, required=False)
    as_float("water_temp_max_c",    lo=-20, hi=80, required=False)

    # Cross-field checks
    if "absolute_temp_min_c" in cleaned and "absolute_temp_max_c" in cleaned:
        if cleaned["absolute_temp_min_c"] >= cleaned["absolute_temp_max_c"]:
            errors.append("absolute_temp_min_c must be < absolute_temp_max_c")

    if ("absolute_humidity_min_pct" in cleaned and
        "absolute_humidity_max_pct" in cleaned and
        cleaned["absolute_humidity_min_pct"] >= cleaned["absolute_humidity_max_pct"]):
        errors.append("absolute_humidity_min_pct must be < absolute_humidity_max_pct")

    # Capacity and thresholds ordering
    cap = cleaned.get("reservoir_full_capacity_kg")
    if cap is not None:
        if cap <= 0:
            errors.append("reservoir_full_capacity_kg must be > 0")
        for key in (
            "reservoir_half_water_kg",
            "reservoir_low_water_kg",
            "reservoir_critical_water_kg",
            "reservoir_pump_cutoff_water_kg",
        ):
            if key in cleaned and cleaned[key] > cap:
                errors.append(f"{key} cannot exceed usable water ({cap} kg)")

        # logical ordering: cutoff ≤ critical ≤ low ≤ half
        a = cleaned.get("reservoir_pump_cutoff_water_kg")
        b = cleaned.get("reservoir_critical_water_kg")
        c = cleaned.get("reservoir_low_water_kg")
        d = cleaned.get("reservoir_half_water_kg")
        if a is not None and b is not None and a > b:
            errors.append("reservoir_pump_cutoff_water_kg must be ≤ reservoir_critical_water_kg")
        if b is not None and c is not None and b > c:
            errors.append("reservoir_critical_water_kg must be ≤ reservoir_low_water_kg")
        if c is not None and d is not None and c > d:
            errors.append("reservoir_low_water_kg must be ≤ reservoir_half_water_kg")

    hcap = cleaned.get("humid_res_full_capacity_kg")
    if hcap is not None:
        if hcap <= 0:
            errors.append("humid_res_full_capacity_kg must be > 0")
        for key in (
            "humid_res_half_water_kg",
            "humid_res_low_water_kg",
            "humid_res_critical_water_kg",
        ):
            if key in cleaned and cleaned[key] > hcap:
                errors.append(f"{key} cannot exceed usable water ({hcap} kg)")

        h_half = cleaned.get("humid_res_half_water_kg")
        h_low  = cleaned.get("humid_res_low_water_kg")
        h_crit = cleaned.get("humid_res_critical_water_kg")
        if h_crit is not None and h_low is not None and h_crit > h_low:
            errors.append("humid_res_critical_water_kg must be ≤ humid_res_low_water_kg")
        if h_low is not None and h_half is not None and h_low > h_half:
            errors.append("humid_res_low_water_kg must be ≤ humid_res_half_water_kg")

        # Full margin sanity
        if "reservoir_full_margin_kg" in cleaned and cleaned["reservoir_full_margin_kg"] > cap:
            errors.append(f"reservoir_full_margin_kg should be ≤ usable water ({cap} kg)")

    # Water temp ordering checks if provided
    wmin = cleaned.get("water_temp_min_c")
    wtgt = cleaned.get("water_temp_target_c")
    wmax = cleaned.get("water_temp_max_c")
    if wmin is not None and wmax is not None and wmin >= wmax:
        errors.append("water_temp_min_c must be < water_temp_max_c")
    if wtgt is not None and wmin is not None and wtgt < wmin:
        errors.append("water_temp_target_c must be ≥ water_temp_min_c")
    if wtgt is not None and wmax is not None and wtgt > wmax:
        errors.append("water_temp_target_c must be ≤ water_temp_max_c")

    ok = len(errors) == 0
    return ok, errors, cleaned


