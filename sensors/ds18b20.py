# sensors/ds18b20.py
import os, json, threading, time, math

# 1-Wire sysfs directory
DS18B20_DIR = "/sys/bus/w1/devices"

# Persist the role→device map under data/config (stable and writable)
_BASE = os.path.dirname(os.path.dirname(__file__))
_CFG_DIR = os.path.join(_BASE, "config")
os.makedirs(_CFG_DIR, exist_ok=True)
DS18B20_MAP_PATH = os.path.join(_CFG_DIR, "ds18b20_map.json")

# Locks & caches
_MAP_CACHE = None
_MAP_LOCK = threading.RLock()

_READ_LOCK = threading.RLock()     # serialize file reads; 1-wire is happier this way
_LAST_GOOD = {"top": None, "bottom": None, "water": None}
_LAST_TS   = {"top": 0.0,  "bottom": 0.0,  "water": 0.0}
_TTL_S     = 0.5  # return cached value if read was <1s ago

INVALID_SENTINELS = {None, 85.0, -127.0, 0.0}  # extend if needed
MIN_VALID, MAX_VALID = -30.0, 120.0

def _bad(v: float | None) -> bool:
    if v is None: return True
    try:
        f = float(v)
    except Exception:
        return True
    if math.isnan(f): return True
    if f in INVALID_SENTINELS: return True
    return not (MIN_VALID <= f <= MAX_VALID)

def _read_ds18b20_file(dev_id: str) -> float | None:
    """Single-shot raw read; return °C or None."""
    if not dev_id:
        return None
    try:
        p = os.path.join(DS18B20_DIR, dev_id, "w1_slave")
        with open(p, "r") as fh:
            l1, l2 = fh.readlines()
        if not l1.strip().endswith("YES"):
            return None
        pos = l2.find("t=")
        if pos == -1:
            return None
        return float(l2[pos + 2:]) / 1000.0
    except Exception:
        return None

def _detect_ids() -> list[str]:
    try:
        return sorted([d for d in os.listdir(DS18B20_DIR) if d.startswith("28-")])
    except Exception:
        return []

def _load_map_from_disk() -> dict:
    try:
        with open(DS18B20_MAP_PATH) as f:
            return json.load(f)
    except Exception:
        return {}

def _save_map_to_disk(m: dict) -> None:
    try:
        with open(DS18B20_MAP_PATH, "w") as f:
            json.dump(m, f, indent=2)
    except Exception:
        pass

def get_ds18b20_map() -> dict:
    """
    Returns mapping like {'top': '28-xxxx', 'bottom': '28-yyyy', 'water': '28-zzzz'}
    Fills missing roles deterministically from detected IDs.
    """
    global _MAP_CACHE
    with _MAP_LOCK:
        if _MAP_CACHE is not None:
            return _MAP_CACHE

        mapping = _load_map_from_disk() or {}
        ids = _detect_ids()

        if "top" not in mapping and len(ids) >= 1:
            mapping["top"] = ids[0]
        if "bottom" not in mapping:
            candidates = [i for i in ids if i != mapping.get("top")]
            if candidates:
                mapping["bottom"] = candidates[0]
        if "water" not in mapping:
            remaining = [i for i in ids if i not in mapping.values()]
            if remaining:
                mapping["water"] = remaining[0]

        _save_map_to_disk(mapping)
        _MAP_CACHE = mapping
        return mapping

def _robust_read(role: str, dev_id: str, retries: int = 3, pause: float = 0.25) -> float | None:
    """
    Debounced, cached read with retries and last-good fallback.
    Returns float °C or None (if never had a good value yet).
    """
    now = time.time()

    # 1) TTL cache to avoid hammering the bus
    if (now - _LAST_TS.get(role, 0.0)) <= _TTL_S and _LAST_GOOD.get(role) is not None:
        return _LAST_GOOD[role]

    # 2) Serialized read with retries
    with _READ_LOCK:
        val = None
        for _ in range(max(1, retries)):
            v = _read_ds18b20_file(dev_id)
            if not _bad(v):
                val = float(v)
                break
            time.sleep(max(0.0, float(pause)))

    # 3) Update last-good or fall back
    if val is not None:
        _LAST_GOOD[role] = val
        _LAST_TS[role] = now
        return val

    # No new good reading: return last-good (may be None on first boot)
    return _LAST_GOOD.get(role)

def read_air_temps_top_bottom(retries: int = 2, pause: float = 0.2) -> dict:
    """
    Returns dict: {'top': float|None, 'bottom': float|None, 'avg': float|None, 'gradient': float|None}
    Uses robust read with caching + retries + last-good fallback.
    """
    m = get_ds18b20_map()
    top = _robust_read("top", m.get("top"), retries=retries, pause=pause)
    bot = _robust_read("bottom", m.get("bottom"), retries=retries, pause=pause)

    if top is not None and bot is not None:
        avg = (top + bot) / 2.0
        grad = top - bot
    elif top is not None:
        avg, grad = top, None
    elif bot is not None:
        avg, grad = bot, None
    else:
        avg, grad = None, None

    return {"top": top, "bottom": bot, "avg": avg, "gradient": grad}

def read_water_temp(retries: int = 2, pause: float = 0.2) -> float | None:
    """Convenience: robust read for the 'water' probe (°C) or None."""
    m = get_ds18b20_map()
    return _robust_read("water", m.get("water"), retries=retries, pause=pause)



