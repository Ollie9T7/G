# sensors/reservoir_eval.py
import math
from typing import Optional, Dict, Any

class _EMA:
    """Exponential moving average with a real time-constant (seconds)."""
    def __init__(self, tau_s: float = 8.0):
        self.tau = float(tau_s)
        self.y: Optional[float] = None
        self.t_last: Optional[float] = None

    def update(self, x: Optional[float], t_now: float) -> Optional[float]:
        if x is None:
            return self.y
        if self.y is None or self.t_last is None or self.tau <= 0:
            self.y, self.t_last = float(x), float(t_now)
            return self.y
        dt = max(0.0, float(t_now) - self.t_last)
        self.t_last = float(t_now)
        alpha = 1.0 - math.exp(-dt / self.tau)
        self.y = self.y + alpha * (float(x) - self.y)
        return self.y

    def force(self, x: float, t_now: float) -> float:
        self.y = float(x); self.t_last = float(t_now); return self.y


class ReservoirTracker:
    """
    Tracks reservoir water (kg) with:
      - EMA smoothing that snaps during pump ON or sudden steps
      - Hysteretic classification: full|half|low|critical|ok
      - Cutoff evaluation and a UI-friendly debug bundle

    update(...) returns a dict with:
      {
        "gross_kg", "water_raw", "water_smoothed", "water_kg",
        "status_label",
        "debug": {...},
        "below_cutoff_now": bool,
        "below_cutoff_value": float|None,
      }
    """
    def __init__(self,
                 tau_s: float = 8.0,
                 snap_delta_kg: float = 0.25,
                 water_quant_kg: float = 0.0,
                 hyst_kg: float = 0.5):
        self.filter = _EMA(tau_s=tau_s)
        self.snap_delta_kg = float(snap_delta_kg)
        self.water_quant_kg = float(water_quant_kg)
        self.hyst_kg = float(hyst_kg)

        self._last_label: Optional[str] = None
        self._last_water_raw: Optional[float] = None

    def _classify(self, usable: float, fm: float, half: float, low: float, crit: float,
                  w: Optional[float], prev: Optional[str]) -> str:
        if w is None:
            return prev or "ok"
        if usable and w >= (usable - fm):
            return "full"
        if w <= crit:      target = "critical"
        elif w <= low:     target = "low"
        elif w <= half:    target = "half"
        else:              target = "ok"

        if prev in (None, "ok", "full"):
            return target

        sev = {"ok": 0, "half": 1, "low": 2, "critical": 3}
        if sev.get(target, 0) > sev.get(prev, 0):
            return target
        if sev.get(target, 0) == sev.get(prev, 0):
            return prev

        # improving → require hysteresis to exit
        if prev == "critical": return target if w > (crit + self.hyst_kg) else prev
        if prev == "low":      return target if w > (low  + self.hyst_kg) else prev
        if prev == "half":     return target if w > (half + self.hyst_kg) else prev
        return target

    def update(self, gross_kg: Optional[float], gs: Dict[str, Any],
               pump_on: bool, now_wall_s: float) -> Dict[str, Any]:
        if gross_kg is None:
            # Reset visible fields; keep EMA state (it will reseed next time)
            return {
                "gross_kg": None,
                "water_raw": None,
                "water_smoothed": None,
                "water_kg": None,
                "status_label": None,
                "debug": None,
                "below_cutoff_now": False,
                "below_cutoff_value": None,
            }

        empty = float(gs.get("reservoir_empty_weight_kg", 0.0) or 0.0)
        full  = float(gs.get("reservoir_full_weight_kg", 0.0) or 0.0)
        usable = max(0.0, full - empty)

        water_raw = max(0.0, float(gross_kg) - empty)

        rapid_step = (self._last_water_raw is not None
                      and abs(water_raw - self._last_water_raw) >= self.snap_delta_kg)
        if pump_on or rapid_step:
            water_sm = self.filter.force(water_raw, now_wall_s)
        else:
            water_sm = self.filter.update(water_raw, now_wall_s)
        self._last_water_raw = water_raw

        if water_sm is not None and self.water_quant_kg > 0:
            water_sm = round(water_sm / self.water_quant_kg) * self.water_quant_kg

        half = float(gs.get("reservoir_half_water_kg", 0.0) or 0.0)
        low  = float(gs.get("reservoir_low_water_kg", 0.0) or 0.0)
        crit = float(gs.get("reservoir_critical_water_kg", 0.0) or 0.0)
        fm   = float(gs.get("reservoir_full_margin_kg", 1.0) or 0.0)

        label = self._classify(usable, fm, half, low, crit, water_sm, self._last_label)
        self._last_label = label

        cutoff_kg = float(gs.get("reservoir_pump_cutoff_water_kg", 0.0) or 0.0)
        min_w = water_sm if water_sm is not None else water_raw
        below_cutoff_now = (min_w is not None and min_w <= cutoff_kg)

        debug = {
            "empty": round(empty, 3),
            "full": round(full, 3),
            "usable": round(usable, 3),
            "half": round(half, 3),
            "low": round(low, 3),
            "critical": round(crit, 3),
            "full_margin": round(fm, 3),
            "raw": round(water_raw, 3),
            "smoothed": None if water_sm is None else round(water_sm, 3),
            # caller can add "pump_on" if they want; kept here for parity:
            "pump_on": bool(pump_on),
        }

        return {
            "gross_kg": round(gross_kg, 2),
            "water_raw": round(water_raw, 2),
            "water_smoothed": None if water_sm is None else round(water_sm, 2),
            "water_kg": None if water_sm is None else round(water_sm, 2),
            "status_label": label,
            "debug": debug,
            "below_cutoff_now": bool(below_cutoff_now),
            "below_cutoff_value": min_w,
        }



