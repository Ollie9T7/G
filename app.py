#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Grow Controller – Flask app entrypoint

Responsibilities of this file:
  • Initialize Flask + structured event logger
  • Initialize GPIO + device layer + background scale sampler
  • Provide a shared CTX object so blueprints can access runtime state & helpers
  • Register all blueprints (system / profiles / control / scale / logs api)
  • Host the control-loop (simulate_profile) and graceful shutdown

The HTTP routes themselves live in:
  - web/system_routes.py    (dashboard, status.json, global settings UI, etc.)
  - web/profiles_routes.py  (list/create/edit/archive/restore/duplicate profiles)
  - web/control_routes.py   (run/stop/pause/unpause/resume/dismiss-resume)

Keep this file focused on bootstrapping, state, and lifecycle.
"""

# ───────────────────────────── Standard libs ──────────────────────────────
import os
import json
import math
import time
import atexit
import signal
import logging
import threading
import datetime
from typing import Optional
from time import monotonic as _mono
import re, unicodedata

# ───────────────────────────── Flask imports ───────────────────────────────
from flask import Flask
from flask import redirect, url_for

# ──────────────────────────── Environment setup ────────────────────────────
# Force Blinka to use RPi.GPIO on Raspberry Pi
os.environ["BLINKA_PIN_FACTORY"] = "RPiGPIO"

# ───────────────────────────── Hardware imports ────────────────────────────
import RPi.GPIO as GPIO  # noqa: F401 (needed for cleanup on failures)
from gpiozero import Device
from gpiozero.pins.rpigpio import RPiGPIOFactory
Device.pin_factory = RPiGPIOFactory()

# Ensure RPi.GPIO numbering mode is set for libraries that use it directly (e.g. hx711)
try:
    GPIO.setmode(GPIO.BCM)
    GPIO.setwarnings(False)
except Exception:
    pass


# ───────────────────────────── App-local imports ───────────────────────────
from devices import (
    init_actuators,
    _set_fan, _set_heater, _set_humidifier, _set_agitator, _set_air_pump, _set_main_pump,
    apply_outputs_from_status, cleanup_gpio as devices_cleanup_gpio,
    fan_configured, pump_configured, heater_configured, humidifier_configured, agitator_configured, air_pump_configured,
    fan_on, heater_on, humidifier_on, agitator_on, air_pump_on,
    fan_on_since, heater_on_since,
    fan_trigger_cause,
    _ensure_gpio_mode,
)

from core.alerts import send_discord, stop_alert_worker

from sensors.dht import read_humidity_top_bottom
from sensors.ds18b20 import read_air_temps_top_bottom, read_water_temp
from sensors.reservoir_eval import ReservoirTracker
from reservoirs.routes import reservoirs_bp


# ── Scale (HX711) raw access (sampler below uses it internally) ───────────
from sensors.scale import _SCALE_LOCK, _scale_read_counts, _load_scale_cal  # noqa: F401

# ── Structured logging (SQLite) ───────────────────────────────────────────
from logging_store.store import EventLogger
from logging_store.api import logs_bp  # JSON/CSV endpoints

# ── State persistence (resume after power loss) ───────────────────────────
from state_manager import save_state, load_state, clear_state

# ── Global settings module ────────────────────────────────────────────────
from global_settings import (
    load_global_settings, save_global_settings, validate_settings,
    DEFAULTS as GLOBAL_DEFAULTS,
)

from logging_store.logging_helpers import (
    bind_logger,
    log_global_settings_snapshot,
    log_profile_resume,
)


# ──────────────────────────── Flask application ───────────────────────────
app = Flask(__name__)
app.secret_key = '91297'  # UI sessions (flashes, etc.)

# Paths
BASE_DIR = os.path.dirname(__file__)
DATA_DIR = os.path.join(BASE_DIR, "data")
LOGS_DIR = os.path.join(DATA_DIR, "logs")
EVENTS_DB_PATH = os.path.join(LOGS_DIR, "events.db")

# Profiles/Archive directories (always absolute)
PROFILE_DIR = os.path.join(BASE_DIR, "profiles")
ARCHIVE_DIR = os.path.join(BASE_DIR, "archive")
os.makedirs(PROFILE_DIR, exist_ok=True)
os.makedirs(ARCHIVE_DIR, exist_ok=True)
os.makedirs(LOGS_DIR, exist_ok=True)

# Logging: app-level log file (not the SQLite event log)
logging.basicConfig(
    filename=os.path.join(BASE_DIR, 'growcontroller.log'),
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)

# Structured Event Logger (SQLite)
app.config["EVENTS_DB_PATH"] = EVENTS_DB_PATH
LOGGER = EventLogger(
    db_path=EVENTS_DB_PATH,
    schema_path=os.path.join(BASE_DIR, "logging_store", "schema.sql"),
).start()
bind_logger(LOGGER)
atexit.register(lambda: LOGGER.stop())

# Expose logs API (choose /logs/api to avoid UI route collisions)
app.register_blueprint(logs_bp, url_prefix="/logs/api")


# ──────────────────────────── Runtime State ───────────────────────────────
running_profile: Optional[str] = None  # filename of the running profile
status_data = {
    "profile":     None,
    "pump_state":  "OFF",
    "cycle_count": 0,
    "start_time":  None,
    "fan_state":   "OFF",
    "heater_state":"OFF",
    "humidifier_state": "OFF",
    "paused": False,
    "humidity": None,
    "temperature_c": None,
    "agitator_state": "OFF",
    "last_error": None,

    # Timed devices (phase bookkeeping)
    "pump_resume_phase": None,
    "pump_time_remaining_s": None,
    "pump_phase_end_ts": None,
    "pump_time_total_s": None,
    "pump_resume_remaining_s": None,

    # Reservoir exposure
    "reservoir_status": None,
    "reservoir_weight_kg": None,
    "reservoir_water_kg": None,
    "pump_cycle_res_before_kg": None,

    # Agitator/Air pump timers
    "agitator_time_remaining_s": None,
    "agitator_phase_end_ts": None,
    "agitator_time_total_s": None,
    "air_pump_time_remaining_s": None,
    "air_pump_phase_end_ts": None,
    "air_pump_time_total_s": None,
    
    # NEW — nutrient dosing live flags (populated via devices + service)
    "nutrient_A_on": False,         # True while pump A GPIO is energised
    "nutrient_B_on": False,         # True while pump B GPIO is energised
    "dosing_phase": None,           # "A" | "B" | None (which pump is running right now)
    "dosing_running": False,        # True if either A or B is running

    # Thresholds & readings (start cleared)
    "temperature_min": None,
    "temperature_target": None,
    "temperature_max": None,
    "humidity_min": None,
    "humidity_target": None,
    "humidity_max": None,
    "water_temperature": None,
    "water_temperature_min": None,
    "water_temperature_target": None,
    "water_temperature_max": None,
    "water_quantity_min": None,
    "temperature_top": None,
    "temperature_bottom": None,
    "temperature_avg": None,
    "temperature_gradient": None,
    "humidity_top": None,
    "humidity_bottom": None,
    "reservoir_gross_kg": None,

}

# Last-good caches for UI smoothing (DHT/DS18B20)
_last_hum_top = None
_last_hum_bot = None
_last_humidity = None
_last_temp = None

# Globals snapshot (refresh periodically inside the loop)
global_settings = load_global_settings()

# Back-compat for templates that still call url_for('home')
@app.route("/home", endpoint="home")
def _home_alias():
    return redirect(url_for("system.home"))

# ───────────────────── Graceful stop plumbing ─────────────────────────────
STOP_EVENT = threading.Event()
SIM_THREAD: Optional[threading.Thread] = None

def _graceful_stop(*_args):
    """Handle SIGTERM by signaling worker loop to exit."""
    try:
        STOP_EVENT.set()
    except Exception:
        pass

signal.signal(signal.SIGTERM, _graceful_stop)
try:
    # Leave Ctrl+C to raise KeyboardInterrupt in dev runs
    signal.signal(signal.SIGINT, signal.default_int_handler)
except Exception:
    pass

# ───────────────────── Background: Scale Sampler ──────────────────────────
class _ScaleSampler:
    """
    Lightweight HX711 sampler providing a thread-safe gross reservoir kg value.
    Keeps UI responsive without blocking control loop on each read.
    """
    def __init__(self, period_s=0.5, n=6):
        self.period_s = float(period_s)
        self.n = int(n)
        self._val = None
        self._lock = threading.Lock()
        self._t = None
        self._stop = threading.Event()

    def start(self):
        if self._t and self._t.is_alive():
            return
        self._stop.clear()
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()

    def stop(self):
        self._stop.set()
        if self._t:
            self._t.join(timeout=1.0)

    def value(self):
        with self._lock:
            return self._val

    def _run(self):
        from sensors.scale import _SCALE_LOCK, _scale_read_counts, _load_scale_cal
        from global_settings import load_global_settings

        while not self._stop.is_set():
            try:
                cal = _load_scale_cal()
                if cal:
                    with _SCALE_LOCK:
                        counts = _scale_read_counts(self.n)
                    if counts is not None:
                        water_kg = (counts - cal["baseline_counts"]) / cal["counts_per_kg"]
                        if water_kg < 0:
                            water_kg = 0.0
                        gs = load_global_settings()
                        empty = float(gs.get("reservoir_empty_weight_kg", 0.0) or 0.0)
                        gross_kg = empty + water_kg
                        with self._lock:
                            self._val = gross_kg
            except Exception:
                pass
            self._stop.wait(self.period_s)

# Global sampler instance
SCALE_SAMPLER = _ScaleSampler(period_s=0.5, n=6)

class _AmbientSampler:
    """
    Lightweight sampler for air temp, humidity, water temp AND reservoir
    that runs only when there is NO active profile. Keeps UI populated at all times.
    """
    def __init__(self, period_s=1.0):
        self.period_s = float(period_s)
        self._t = None
        self._stop = threading.Event()
        # NEW: persistent reservoir tracker for smoothing while idle
        self._rt = ReservoirTracker(tau_s=8.0, snap_delta_kg=0.25, water_quant_kg=0.0, hyst_kg=0.5)

    def start(self):
        if self._t and self._t.is_alive():
            return
        self._stop.clear()
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()

    def stop(self):
        try:
            self._stop.set()
        except Exception:
            pass

    def _run(self):
        # Uses existing globals and helpers in app.py
        global status_data, running_profile, _last_temp, _last_humidity
        while not self._stop.is_set():
            try:
                # run when there is NO profile OR when a profile is paused
                if (not running_profile) or bool(status_data.get("paused", False)):
                    # ---- existing ambient sensors ----
                    air = read_air_temps_top_bottom()     # dict: top, bottom, avg, gradient
                    hum = read_humidity_top_bottom()      # dict: top, bottom, avg
                    try:
                        water_c = read_water_temp()
                    except Exception:
                        water_c = None

                    if air.get("avg") is not None:
                        _last_temp = air["avg"]
                    if hum.get("avg") is not None:
                        _last_humidity = hum["avg"]

                    status_data.update(
                        temperature_c=_last_temp,
                        temperature_top=air.get("top"),
                        temperature_bottom=air.get("bottom"),
                        temperature_avg=air.get("avg"),
                        temperature_gradient=air.get("gradient"),
                        humidity=_last_humidity,
                        humidity_top=hum.get("top"),
                        humidity_bottom=hum.get("bottom"),
                        water_temperature=water_c,
                    )

                    # ---- NEW: reservoir from scale sampler while idle ----
                    gs = load_global_settings()  # use latest thresholds/capacities
                    res_gross = SCALE_SAMPLER.value()  # gross kg (empty+water) or None

                    if res_gross is not None:
                        info = self._rt.update(
                            res_gross,
                            gs,
                            pump_on=False,               # idle; pump not running
                            now_wall_s=time.time()
                        )
                        # Publish exactly what the UI expects
                        status_data["reservoir_gross_kg"]  = info.get("gross_kg")
                        status_data["reservoir_weight_kg"] = info.get("gross_kg")
                        status_data["reservoir_water_raw"] = info.get("water_raw")
                        status_data["reservoir_water_kg"]  = info.get("water_kg")
                        status_data["reservoir_status"]    = info.get("status_label")
                        status_data["reservoir_debug"]     = info.get("debug")

                        # --- ADDED: idle recovery banner clear ---
                        # If we’re idle and the reservoir is no longer below cutoff,
                        # clear a lingering banner set during a previous fault.
                        try:
                            if (not running_profile) and status_data.get("last_error"):
                                alerts = status_data.get("alert_states") or {}
                                res_alert = alerts.get("reservoir_cutoff") or {}
                                if (not info.get("below_cutoff_now")) and (not res_alert.get("active")):
                                    status_data["last_error"] = None
                        except Exception:
                            pass

                    else:
                        status_data.update(
                            reservoir_gross_kg=None,
                            reservoir_weight_kg=None,
                            reservoir_water_raw=None,
                            reservoir_water_kg=None,
                            reservoir_status=None,
                            reservoir_debug=None
                        )
            except Exception:
                # Never crash the sampler loop
                pass

            self._stop.wait(self.period_s)







# ───────────────────── System banner helper for UI ────────────────────────
def compute_banner(status: dict) -> dict:
    """
    Decide the top-of-dashboard system message.
    Priority: no-profile > error > paused > active action > out-of-range > ok
    Returns: {"level": "error|warning|info|ok", "message": str, "rotate": [str,...]}
    """
    if not status.get("profile"):
        return {"level": "info", "message": "Idle — live monitoring (no active profile)", "rotate": []}


    err = status.get("last_error")
    if err:
        return {"level": "error", "message": f"Fault detected – {err}", "rotate": []}

    if status.get("paused"):
        return {"level": "warning", "message": "Paused – automation is halted", "rotate": []}

    if status.get("heater_state") == "ON":
        return {"level": "info", "message": "Environment cold – heating now", "rotate": []}
    if status.get("humidifier_state") == "ON":
        return {"level": "info", "message": "Air dry – humidifying now", "rotate": []}
    if status.get("fan_state") == "ON":
        return {"level": "info", "message": "Air hot/stale – extracting now", "rotate": []}
    if status.get("agitator_state") == "ON" and status.get("pump_state") != "ON":
        return {"level": "info", "message": "Mixing reservoir – agitator running", "rotate": []}
    if status.get("pump_state") == "ON":
        return {"level": "info", "message": "Irrigating – water pump running", "rotate": []}

    # quick out-of-range nudges
    t = status.get("temperature_c")
    tmin, tmax = status.get("temperature_min"), status.get("temperature_max")
    h = status.get("humidity")
    hmin, hmax = status.get("humidity_min"), status.get("humidity_max")

    if t is not None and tmin is not None and t < tmin: return {"level": "warning", "message": "Temperature below range", "rotate": []}
    if t is not None and tmax is not None and t > tmax: return {"level": "warning", "message": "Temperature above range", "rotate": []}
    if h is not None and hmin is not None and h < hmin: return {"level": "warning", "message": "Humidity below range", "rotate": []}
    if h is not None and hmax is not None and h > hmax: return {"level": "warning", "message": "Humidity above range", "rotate": []}

    rotate_msgs = [
        "Checking all systems…", "Everything looks good.", "Checking all systems…",
        "Nothing on fire. Good start.", "Plants are plotting world domination.",
        "Photosynthesis in progress.", "Holding steady.", "Sensors gossiping behind your back.",
        "Humidity behaving (for now).", "Nutrients shaken, not stirred.",
        "All sensors nominal.", "Counting imaginary sheep… I mean plants.",
        "Logging sensor data… probably.", "AI says: everything looks tasty.",
        "No weeds, we're in the clear.", "Roots on strike until further watering.",
        "No leaks detected - just leeks detected",
    ]
    return {"level": "ok", "message": rotate_msgs[0], "rotate": rotate_msgs}


# ─────────────────────────── Device + workers init ────────────────────────
init_actuators(LOGGER, status_data, send_discord)

# Make sure GPIO mode is valid before any background threads touch HX711
try:
    _ensure_gpio_mode()
except Exception:
    pass

SCALE_SAMPLER.start()
AMBIENT_SAMPLER = _AmbientSampler(period_s=1.0)
AMBIENT_SAMPLER.start()



atexit.register(stop_alert_worker)  # stop Discord alert worker on exit

# ───────────────────── Control loop (simulate_profile) ────────────────────
def simulate_profile(profile_name: str, profile_data: dict):
    """
    The main control loop thread for a running profile.

    Reads sensors; enforces climate thresholds; schedules pump/agitator/air-pump
    with hysteresis and windowing; emits structured logs; keeps UI fields updated.
    """
    import math

    global running_profile, status_data, _last_humidity, _last_temp, _last_hum_top, _last_hum_bot, global_settings

    # Ensure GPIO mode is valid in this thread prior to any writes
    _ensure_gpio_mode()

    def _safe_seconds(val, fallback=0.0):
        """Coerce a duration-like value to float seconds."""
        try:
            if val is None:
                raise ValueError
            if isinstance(val, str):
                val = val.strip()
                if val == "":
                    raise ValueError
            return float(val)
        except Exception:
            return float(fallback or 0.0)

    def _round_positive(val):
        """Return a rounded positive duration or None if not > 0."""
        try:
            v = float(val)
        except (TypeError, ValueError):
            return None
        return round(v, 3) if v > 0 else None

    def _round_remaining(val):
        """Clamp remaining seconds to ≥0.0 and round for UI."""
        try:
            v = float(val)
        except (TypeError, ValueError):
            return None
        if v <= 0:
            return 0.0
        return round(v, 3)

    def _resolve_total(actual, planned):
        """Prefer an actual runtime if available; otherwise fall back to plan."""
        try:
            actual_v = float(actual)
        except (TypeError, ValueError):
            actual_v = 0.0
        try:
            planned_v = float(planned)
        except (TypeError, ValueError):
            planned_v = 0.0
        chosen = actual_v if actual_v > 0 else planned_v
        return round(chosen, 3) if chosen > 0 else None

    # pump durations (live-updated by hot-reload)
    pump_on  = _safe_seconds(profile_data.get("pump", {}).get("on_duration_sec"), 0.0)
    pump_off = _safe_seconds(profile_data.get("pump", {}).get("off_duration_sec"), 0.0)

    running_profile = profile_name
    status_data.update(profile=profile_name, cycle_count=0)
    
    # Log the current Global Settings and the profile parameters (once per run)
    try:
        _gs_now = load_global_settings()
        log_global_settings_snapshot(profile_id=profile_name, globals_dict=_gs_now, reason="globals")
    except Exception:
        pass


    profile_path = os.path.join(PROFILE_DIR, profile_name)

    # Globals snapshot & refresh cadence
    gs = dict(global_settings)
    next_gs_reload = 0.0

    # Premix config
    ag_enabled = False; ag_run = 0
    air_enabled = False; air_run = 0

    def _apply_profile_cfg(cfg):
        """Refresh thresholds/durations/UI totals from profile JSON on disk."""
        nonlocal pump_on, pump_off, profile_data, ag_enabled, ag_run, air_enabled, air_run
        profile_data = cfg
        t = cfg.get("temperature", {})
        h = cfg.get("humidity", {})
        w = cfg.get("water", {}).get("temperature", {})
        q = cfg.get("water", {}).get("quantity", {})
        status_data.update(
            temperature_min=t.get("min"),
            temperature_target=t.get("target"),
            temperature_max=t.get("max"),
            humidity_min=h.get("min"),
            humidity_target=h.get("target"),
            humidity_max=h.get("max"),
            water_temperature_min=w.get("min"),
            water_temperature_target=w.get("target"),
            water_temperature_max=w.get("max"),
            water_quantity_min=q.get("min"),
        )
        try:
            pump_on  = _safe_seconds(cfg.get("pump", {}).get("on_duration_sec", pump_on), pump_on)
            pump_off = _safe_seconds(cfg.get("pump", {}).get("off_duration_sec", pump_off), pump_off)
        except Exception:
            pass

        # premix (from profile)
        try:
            ag_enabled = bool(cfg.get("pump", {}).get("agitator_enabled", False))
            ag_run     = _safe_seconds(cfg.get("pump", {}).get("agitator_run_sec", ag_run), ag_run)
            air_enabled = bool(cfg.get("pump", {}).get("air_pump_enabled", False))
            air_run     = _safe_seconds(cfg.get("pump", {}).get("air_pump_run_sec", air_run), air_run)
        except Exception:
            ag_enabled, ag_run = False, 0
            air_enabled, air_run = False, 0

        status_data["pump_time_total_s"]      = _round_positive(pump_on)
        status_data["agitator_time_total_s"]  = _round_positive(ag_run)
        status_data["air_pump_time_total_s"]  = _round_positive(air_run)

        # Carry window hours for scheduler
        P = cfg.get("pump", {}) or {}
        profile_data.setdefault("pump", {})
        profile_data["pump"]["_win_on_h"]  = P.get("on_time")
        profile_data["pump"]["_win_off_h"] = P.get("off_time")

    _apply_profile_cfg(profile_data)

    try:
        _last_profile_mtime = os.path.getmtime(profile_path)
    except Exception:
        _last_profile_mtime = None
    _next_reload_check = 0.0

    # Fail-safe: ensure climate outputs and premix are OFF before entering loop
    _set_fan(False);        status_data["fan_state"] = "OFF"
    _set_heater(False);     status_data["heater_state"] = "OFF"
    _set_humidifier(False); status_data["humidifier_state"] = "OFF"
    _set_agitator(False);   status_data["agitator_state"] = "OFF"
    _set_air_pump(False);   status_data["air_pump_state"] = "OFF"

    logging.info(f"🌱 Starting simulation for profile: {profile_name}")

    # Startup: merge in runtime-global premix config (seconds + enables only)
    gs = load_global_settings()
    try:
        ag_enabled = bool(gs.get("agitator_enabled", ag_enabled))
        ag_run     = _safe_seconds(gs.get("agitator_run_sec", ag_run), ag_run)
    except Exception:
        pass
    try:
        air_enabled = bool(gs.get("air_pump_enabled", air_enabled))
        air_run     = _safe_seconds(gs.get("air_pump_run_sec", air_run), air_run)
    except Exception:
        pass

    status_data["pump_time_total_s"]     = _round_positive(pump_on)
    status_data["agitator_time_total_s"] = _round_positive(ag_run)
    status_data["air_pump_time_total_s"] = _round_positive(air_run)

    # ───────────────── STARTUP INITIALISATION ─────────────────
    now = time.time()
    now_m = _mono()

    win_on  = profile_data.get("pump", {}).get("_win_on_h")
    win_off = profile_data.get("pump", {}).get("_win_off_h")

    def _in_hour_window(start_h, end_h, now_ts=None):
        """Return True if local current hour is within [start_h, end_h)."""
        try:
            if start_h is None or end_h is None: return True
            start_h = int(start_h); end_h = int(end_h)
            if start_h == end_h: return False  # disabled
            now_dt = datetime.datetime.now() if now_ts is None else datetime.datetime.fromtimestamp(now_ts)
            h = now_dt.hour
            if start_h < end_h:
                return start_h <= h < end_h
            return (h >= start_h) or (h < end_h)  # window wraps midnight
        except Exception:
            return True

    def _next_window_open_ts(start_h, end_h, now_dt=None):
        """Epoch seconds when the next window opens at start_h, else None."""
        if start_h is None or end_h is None: return None
        try:
            start_h = int(start_h); end_h = int(end_h)
        except Exception:
            return None
        if start_h == end_h: return None
        now_dt = now_dt or datetime.datetime.now()
        today = now_dt.date()
        cand = datetime.datetime.combine(today, datetime.time(hour=start_h, minute=0, second=0))
        if cand <= now_dt:
            cand = cand + datetime.timedelta(days=1)
        return cand.timestamp()

    allowed_now = _in_hour_window(win_on, win_off)

    # ── Detect resume-after-crash via current status_data snapshot (no file IO) ──
    # control_routes.resume_profile() sets status_data before starting this thread.
    # If pump_state was ON and we are not paused, it's a crash-resume case.
    do_crash_premix = (
        status_data.get("profile") == profile_name and
        str(status_data.get("pump_state", "OFF")).upper() == "ON" and
        not bool(status_data.get("paused", False))
    )
    
    
    # Log outage-recovery resume event (once at thread start if applicable)
    if do_crash_premix:
        try:
            log_profile_resume(profile_name, previous_run_id=None)
        except Exception:
            pass


    # loop state
    pump_state = False
    pump_timer = now_m
    next_on_due_at = None   # wall-clock when pump should turn ON
    agitator_started_this_cycle = False
    agitator_timer = 0.0
    air_started_this_cycle = False
    air_timer = 0.0

    def _schedule_next_on(now_wall):
        return now_wall + pump_off if pump_off > 0 else now_wall

    def _trigger_startup_premix(now_wall, now_mono, *, force=False):
        """Run agitator/air premix before the main pump resumes."""
        nonlocal next_on_due_at, agitator_timer, air_timer, agitator_started_this_cycle
        nonlocal air_started_this_cycle, pump_state, pump_timer
        try:
            _ensure_gpio_mode()
        except Exception:
            pass
        # Ensure the main pump is OFF so premix has exclusive control.
        if force or status_data.get("pump_state") == "ON":
            try:
                if pump_configured:
                    _set_main_pump(False)
            except Exception:
                pass
            status_data["pump_state"] = "OFF"
            status_data["pump_phase_end_ts"] = None
            status_data["pump_time_remaining_s"] = None
            pump_state = False
            pump_timer = now_mono
        agitator_started_this_cycle = False
        air_started_this_cycle = False
        longest = max(ag_run if ag_enabled else 0, air_run if air_enabled else 0)
        existing_delay = 0.0
        if next_on_due_at is not None:
            try:
                existing_delay = max(0.0, float(next_on_due_at - now_wall))
            except Exception:
                existing_delay = 0.0
        target_delay = float(longest if longest > 0 else 0.0)
        pump_delay = max(existing_delay, target_delay)
        next_on_due_at = now_wall + pump_delay
        clamp = pump_delay
        if air_enabled and air_run > 0:
            _set_air_pump(True)
            status_data["air_pump_state"] = "ON"
            air_timer = now_mono
            air_started_this_cycle = True
            end_by_run = air_timer + air_run
            end_by_pump = now_mono + clamp
            status_data["air_pump_phase_end_ts"] = min(end_by_run, end_by_pump)
            try:
                duration = max(0.0, float(status_data["air_pump_phase_end_ts"]) - float(air_timer))
                status_data["air_pump_time_total_s"] = _resolve_total(duration, air_run)
            except Exception:
                status_data["air_pump_time_total_s"] = _round_positive(air_run)
            try:
                remaining = float(status_data["air_pump_phase_end_ts"]) - float(now_mono)
            except Exception:
                remaining = None
            status_data["air_pump_time_remaining_s"] = _round_remaining(remaining)
        else:
            status_data["air_pump_state"] = "OFF"
            status_data["air_pump_phase_end_ts"] = None
            status_data["air_pump_time_remaining_s"] = None
        if ag_enabled and ag_run > 0:
            _set_agitator(True)
            status_data["agitator_state"] = "ON"
            agitator_timer = now_mono
            agitator_started_this_cycle = True
            end_by_run = agitator_timer + ag_run
            end_by_pump = now_mono + clamp
            status_data["agitator_phase_end_ts"] = min(end_by_run, end_by_pump)
            try:
                duration = max(0.0, float(status_data["agitator_phase_end_ts"]) - float(agitator_timer))
                status_data["agitator_time_total_s"] = _resolve_total(duration, ag_run)
            except Exception:
                status_data["agitator_time_total_s"] = _round_positive(ag_run)
            try:
                ag_remaining = float(status_data["agitator_phase_end_ts"]) - float(now_mono)
            except Exception:
                ag_remaining = None
            status_data["agitator_time_remaining_s"] = _round_remaining(ag_remaining)
        else:
            status_data["agitator_state"] = "OFF"
            status_data["agitator_phase_end_ts"] = None
            status_data["agitator_time_remaining_s"] = None
        return pump_delay




    # First due time:
    if not allowed_now:
        next_on_due_at = _next_window_open_ts(win_on, win_off, now_dt=datetime.datetime.now())
    else:
        if status_data.pop("startup_kick", False) or do_crash_premix:
            _trigger_startup_premix(now, now_m, force=do_crash_premix)
        else:
            next_on_due_at = now  # start immediately

    # If allowed and nothing pending, start pump now
    if allowed_now and next_on_due_at is not None and next_on_due_at <= now and not (agitator_started_this_cycle or air_started_this_cycle):
        if pump_on > 0:
            status_data["pump_state"] = "ON"
            pump_state = True
            pump_timer = now_m
            status_data["cycle_count"] += 1
            _set_main_pump(True)
            status_data["pump_phase_end_ts"] = now_m + pump_on
        else:
            status_data["pump_state"] = "OFF"
            pump_state = False
            pump_timer = now_m
            status_data["pump_phase_end_ts"] = None
    else:
        status_data["pump_state"] = "OFF"
        status_data["pump_phase_end_ts"] = None
        status_data["pump_time_remaining_s"] = None

    # Pause/resume and logging trackers
    last_paused = bool(status_data.get("paused", False))
    pump_resume_phase = None
    last_state_save = time.time()
    state_save_interval = 60
    last_logged_pump_state = None
    last_logged_error = None

    # Reservoir evaluation helper
    tracker = ReservoirTracker(tau_s=8.0, snap_delta_kg=0.25, water_quant_kg=0.0, hyst_kg=0.5)
    CUTOFF_DEBOUNCE_S = 20
    cutoff_below_since = None
    reservoir_cutoff_alerted = False
    cutoff_clear_hyst_kg = 0.5
    water_temp_hard_alerted = False
    below_cutoff_now = False
    below_cutoff_value = None



    # Per-alert state container (only alert once per hard fault)
    status_data.setdefault("alert_states", {})

    def _alert_state(name: str):
        st = status_data["alert_states"].setdefault(name, {
            "active": False,
            "first_triggered": None,
            "last_notified": 0.0,
            "message": None,
        })
        return st
        
    def _log_alert(kind: str, phase: str, msg: str, payload: dict | None = None):
        """
        Write a single row to SQLite alerts log.
        kind:   e.g. 'temp_hard_low', 'hum_hard_high', 'reservoir_cutoff'
        phase:  'breach' or 'recover'
        msg:    human message
        payload: extra measurements/thresholds for audit
        """
        try:
            LOGGER.log_event(
                event_type="alert",
                reason_code=f"{kind}:{phase}",
                msg=msg,
                profile_id=profile_name,
                actor="safety",
                payload=payload or {}
            )
        except Exception:
            pass





    def _edge_alert(
        name: str,
        breach_now: bool,
        recovered_now: bool,
        msg: str,
        cooldown_s: int = 300,
        payload: dict | None = None,
    ):
        """
        Edge-based alerting with cooldown. Returns True if 'hard stop' actions should apply.
        - On first breach: set active, set last_error, notify immediately, LOG 'breach'
        - While breached: optionally send periodic reminders (no extra logs to avoid noise)
        - On recovery: clear active + last_error and notify, LOG 'recover'
        """
        now_ts = time.time()
        st = _alert_state(name)

        # Enter breach
        if breach_now and not st["active"]:
            st["active"] = True
            st["first_triggered"] = now_ts
            st["last_notified"] = 0.0  # force immediate notify
            st["message"] = msg
            status_data["last_error"] = msg  # drives the banner/sound

            # --- NEW: write breach log row
            _log_alert(name, "breach", msg, payload)

            try:
                send_discord(f"⛔ {msg} — all outputs halted. @everyone")
            except Exception:
                pass
            return True  # apply hard-stop actions this tick

        # Still in breach: throttle reminders (NO log row here)
        if breach_now and st["active"]:
            if (now_ts - (st["last_notified"] or 0)) >= float(cooldown_s or 0):
                st["last_notified"] = now_ts
                try:
                    send_discord(f"⚠️ Still breached: {msg}")
                except Exception:
                    pass
            return True

        # Recovery edge
        if recovered_now and st["active"]:
            st["active"] = False
            st["message"] = None

            # --- NEW: write recovery log row
            _log_alert(name, "recover", f"Recovered: {msg}", payload)

            # Only clear last_error if no *other* hard alerts are active
            any_active = any(v.get("active") for k, v in status_data["alert_states"].items())
            if not any_active:
                status_data["last_error"] = None
            try:
                send_discord("✅ Recovery: back within hard limit + hysteresis")
            except Exception:
                pass
            return False

        # Neither breached nor recovering
        return False






    # ───────────────────────────── main loop ───────────────────────────────
    while (running_profile == profile_name) and (not STOP_EVENT.is_set()):
        now = time.time()
        now_m = _mono()

        # ─── Pause/Resume edges ───
        paused_now = bool(status_data.get("paused", False))
        if paused_now and not last_paused:
            phase_from_status = status_data.get("pump_resume_phase")
            pump_resume_phase = phase_from_status if phase_from_status in ("ON", "OFF") else ("ON" if (status_data.get("pump_state") == "ON") else "OFF")

        # Capture remaining times + phases for premix on pause
        def _rem_from(end_ts, now_mono):
            try:
                if isinstance(end_ts, (int, float)) and end_ts > 0:
                    r = float(end_ts) - float(now_mono)
                    return r if r > 0 else 0.0
            except Exception:
                pass
            return 0.0

        # Agitator
        status_data["agitator_resume_phase"] = "ON" if status_data.get("agitator_state") == "ON" else "OFF"
        status_data["agitator_resume_remaining_s"] = _round_remaining(
            _rem_from(status_data.get("agitator_phase_end_ts"), _mono())
        )

        # Air pump
        status_data["air_pump_resume_phase"] = "ON" if status_data.get("air_pump_state") == "ON" else "OFF"
        status_data["air_pump_resume_remaining_s"] = _round_remaining(
            _rem_from(status_data.get("air_pump_phase_end_ts"), _mono())
        )

        # Pump remaining (useful if not already set by routes/UI)
        if status_data.get("pump_state") == "ON":
            status_data["pump_resume_remaining_s"] = _round_remaining(
                _rem_from(status_data.get("pump_phase_end_ts"), _mono())
            )
        else:
            status_data["pump_resume_remaining_s"] = None


        if (not paused_now) and last_paused:
            # Resume path...
            if pump_resume_phase not in ("ON", "OFF"):
                pump_resume_phase = status_data.get("pump_resume_phase")

            if pump_resume_phase == "ON":
                remaining = status_data.get("pump_resume_remaining_s")
                try: _ensure_gpio_mode()
                except Exception: pass
                rem_raw = _safe_seconds(remaining, 0.0)
                if rem_raw > 0:
                    elapsed = max(0.0, float(pump_on) - rem_raw)
                    status_data["pump_phase_end_ts"] = (now_m - elapsed) + float(pump_on)
                    status_data["pump_time_remaining_s"] = _round_remaining(rem_raw)
                else:
                    status_data["pump_phase_end_ts"] = now_m + float(pump_on)
                    status_data["pump_time_remaining_s"] = _round_positive(pump_on)
                _set_main_pump(True)
                status_data["pump_state"] = "ON"
                next_on_due_at = None
            else:
                def _rem(v):
                    try:
                        v = float(v)
                        return v if v > 0 else 0.0
                    except Exception:
                        return 0.0

                remain_air = _rem(status_data.get("air_pump_resume_remaining_s"))
                remain_ag  = _rem(status_data.get("agitator_resume_remaining_s"))

                resume_air_phase = str(status_data.get("air_pump_resume_phase") or "").upper()
                resume_ag_phase  = str(status_data.get("agitator_resume_phase") or "").upper()

                longest = 0.0

                # AIR PUMP: resume only if it was ON at pause OR there is actual remainder
                if air_enabled and ((resume_air_phase == "ON") or (remain_air > 0)):
                    run_sec = remain_air if remain_air > 0 else float(air_run or 0)
                    if run_sec > 0:
                        _set_air_pump(True); status_data["air_pump_state"] = "ON"
                        air_timer = now_m; air_started_this_cycle = True
                        status_data["air_pump_phase_end_ts"] = now_m + run_sec
                        status_data["air_pump_time_remaining_s"] = _round_remaining(run_sec)
                        longest = max(longest, run_sec)
                else:
                    status_data["air_pump_state"] = "OFF"
                    status_data["air_pump_phase_end_ts"] = None
                    status_data["air_pump_time_remaining_s"] = None

                # AGITATOR: resume only if it was ON at pause OR there is actual remainder
                if ag_enabled and ((resume_ag_phase == "ON") or (remain_ag > 0)):
                    run_sec = remain_ag if remain_ag > 0 else float(ag_run or 0)
                    if run_sec > 0:
                        _set_agitator(True); status_data["agitator_state"] = "ON"
                        agitator_timer = now_m; agitator_started_this_cycle = True
                        status_data["agitator_phase_end_ts"] = now_m + run_sec
                        status_data["agitator_time_remaining_s"] = _round_remaining(run_sec)
                        longest = max(longest, run_sec)
                else:
                    status_data["agitator_state"] = "OFF"
                    status_data["agitator_phase_end_ts"] = None
                    status_data["agitator_time_remaining_s"] = None

                next_on_due_at = now + longest if longest > 0 else (now if (pump_off or 0) <= 0 else _schedule_next_on(now))





            # Clear one-shot resume hints
            pump_resume_phase = None
            status_data["pump_resume_remaining_s"] = None
            status_data["pump_resume_phase"] = None
            status_data["agitator_resume_remaining_s"] = None
            status_data["agitator_resume_phase"] = None
            status_data["air_pump_resume_remaining_s"] = None
            status_data["air_pump_resume_phase"] = None

        last_paused = paused_now

        if paused_now:
            # --- SAFETY: ensure all actuators are OFF exactly as you already do ---
            try:
                _set_main_pump(False)
                _set_agitator(False)
                _set_air_pump(False)
                _set_fan(False)
                _set_heater(False)
                _set_humidifier(False)
            except Exception:
                pass

            # Make sure status reflects paused (keeps your UI badges consistent)
            status_data["paused"] = True

            # --- keep reservoir/scale live while paused ---
            # We publish a fresh reservoir snapshot so /status.json and /api/reservoirs/live
            # continue to show live kg/litres/percent while the profile is paused.
            try:
                # Current gross kg from the running sampler (non-blocking)
                res_gross = SCALE_SAMPLER.value()

                # Recompute reservoir info without driving any pumps (pump_on=False)
                info = tracker.update(
                    res_gross,
                    gs,                       # your current global settings object in scope
                    pump_on=False,            # paused => no pump activity
                    now_wall_s=time.time(),   # use wall clock for user-facing timestamps
                )

                if info.get("gross_kg") is not None:
                    # Maintain the exact keys your UI already reads
                    status_data["reservoir_gross_kg"]  = info["gross_kg"]
                    status_data["reservoir_weight_kg"] = info["gross_kg"]  # you mirror gross here
                    status_data["reservoir_water_raw"] = info.get("water_raw")
                    status_data["reservoir_water_kg"]  = info.get("water_kg")
                    status_data["reservoir_status"]    = info.get("status_label")
                    status_data["reservoir_debug"]     = info.get("debug")
                else:
                    # Explicitly publish Nones if we can't compute a reading
                    status_data.update(
                        reservoir_gross_kg=None,
                        reservoir_weight_kg=None,
                        reservoir_water_raw=None,
                        reservoir_water_kg=None,
                        reservoir_status=None,
                        reservoir_debug=None,
                    )
            except Exception:
                # Never let a UI refresh failure break the pause safety path
                logging.exception("Paused-state reservoir refresh failed")

            # Preserve your existing short sleep + loop continue while paused
            if STOP_EVENT.wait(0.25):
                return
            continue




        # ─── Periodic global settings refresh ───
        if now >= next_gs_reload:
            gs = load_global_settings()
            status_data["water_temperature_min"]    = gs.get("water_temp_min_c")
            status_data["water_temperature_target"] = gs.get("water_temp_target_c")
            status_data["water_temperature_max"]    = gs.get("water_temp_max_c")
            next_gs_reload = now + 5.0
            try:
                ag_enabled = bool(gs.get("agitator_enabled", ag_enabled))
                ag_run     = _safe_seconds(gs.get("agitator_run_sec", ag_run), ag_run)
            except Exception:
                pass
            try:
                air_enabled = bool(gs.get("air_pump_enabled", air_enabled))
                air_run     = _safe_seconds(gs.get("air_pump_run_sec", air_run), air_run)
            except Exception:
                pass
            status_data["agitator_time_total_s"] = _round_positive(ag_run)
            status_data["air_pump_time_total_s"] = _round_positive(air_run)

        # ─── Hot-reload the profile JSON ───
        if now >= _next_reload_check:
            _next_reload_check = now + 3.0
            try:
                mtime = os.path.getmtime(profile_path)
                if _last_profile_mtime is None or mtime > _last_profile_mtime:
                    with open(profile_path) as f:
                        new_cfg = json.load(f)
                    _apply_profile_cfg(new_cfg)
                    _last_profile_mtime = mtime
                    logging.info("♻️  Hot-reloaded profile config from disk.")
            except Exception as e:
                logging.warning(f"Hot-reload failed: {e}")

        # ─── Sensors ───
        hum = read_humidity_top_bottom()
        if hum["top"] is not None: _last_hum_top = hum["top"]
        if hum["bottom"] is not None: _last_hum_bot = hum["bottom"]
        if _last_hum_top is not None and _last_hum_bot is not None:
            _last_humidity = (_last_hum_top + _last_hum_bot) / 2.0
        elif _last_hum_top is not None:
            _last_humidity = _last_hum_top
        elif _last_hum_bot is not None:
            _last_humidity = _last_hum_bot
        status_data.update(humidity=_last_humidity, humidity_top=_last_hum_top, humidity_bottom=_last_hum_bot)

        air = read_air_temps_top_bottom()
        if air["avg"] is not None: _last_temp = air["avg"]
        status_data.update(
            temperature_c=_last_temp,
            temperature_top=air["top"],
            temperature_bottom=air["bottom"],
            temperature_avg=air["avg"],
            temperature_gradient=air["gradient"],
        )
        try:
            water_c = read_water_temp()
        except Exception:
            water_c = None
        status_data["water_temperature"] = water_c

        # HX711 reservoir (thread-averaged)
        res_gross = SCALE_SAMPLER.value()
        info = tracker.update(
            res_gross,
            gs,
            pump_on=(status_data.get("pump_state") == "ON"),
            now_wall_s=now
        )

        if info["gross_kg"] is not None:
            status_data["reservoir_gross_kg"]  = info["gross_kg"]
            status_data["reservoir_weight_kg"] = info["gross_kg"]
            status_data["reservoir_water_raw"] = info["water_raw"]
            status_data["reservoir_water_kg"]  = info["water_kg"]
            status_data["reservoir_status"]    = info["status_label"]
            status_data["reservoir_debug"]     = info["debug"]

            below_cutoff_now   = info["below_cutoff_now"]
            below_cutoff_value = info["below_cutoff_value"]
            
            # Proactively de-stale the reservoir_cutoff alert if readings are OK
            try:
                status_data.setdefault("alert_states", {})
                rc = status_data["alert_states"].setdefault("reservoir_cutoff", {})
                if not info.get("below_cutoff_now", False):
                    rc["active"] = False
                    rc["message"] = None
            except Exception:
                pass
    
            
        else:
            status_data.update(
                reservoir_gross_kg=None,
                reservoir_weight_kg=None,
                reservoir_water_raw=None,
                reservoir_water_kg=None,
                reservoir_status=None,
                reservoir_debug=None
            )
            below_cutoff_now = False

        # ─── Hard safety limits (edge-based with hysteresis + cooldown) ───
        # Hysteresis values (fallback to your general hysteresis if dedicated one not set)
        TEMP_HYST = float((gs.get("absolute_temp_hyst_c")
                           if gs.get("absolute_temp_hyst_c") is not None
                           else gs.get("hysteresis_temp_c", 0.5)) or 0.0)
        HUM_HYST  = float((gs.get("absolute_humidity_hyst_pct")
                           if gs.get("absolute_humidity_hyst_pct") is not None
                           else gs.get("hysteresis_humidity_pct", 1.5)) or 0.0)
        WTMP_HYST = float((gs.get("absolute_water_temp_hyst_c")
                           if gs.get("absolute_water_temp_hyst_c") is not None
                           else gs.get("hysteresis_temp_c", 0.5)) or 0.0)
        REMIND_COOLDOWN_S = int(gs.get("hard_alert_cooldown_s") or 300)  # 5 min default

        abs_tmin = gs.get("absolute_temp_min_c")
        abs_tmax = gs.get("absolute_temp_max_c")
        abs_hmin = gs.get("absolute_humidity_min_pct")
        abs_hmax = gs.get("absolute_humidity_max_pct")
        abs_wmin = gs.get("water_temp_min_c")
        abs_wmax = gs.get("water_temp_max_c")

        # Current readings
        air_t   = _last_temp
        hum_av  = _last_humidity
        water_c = status_data.get("water_temperature")

        # Breach/recovery booleans (use hysteresis for clear)
        t_low_breach = (air_t is not None and abs_tmin is not None and air_t < abs_tmin)
        t_low_recover = (air_t is not None and abs_tmin is not None and air_t >= (abs_tmin + TEMP_HYST))

        t_high_breach = (air_t is not None and abs_tmax is not None and air_t > abs_tmax)
        t_high_recover = (air_t is not None and abs_tmax is not None and air_t <= (abs_tmax - TEMP_HYST))

        h_low_breach = (hum_av is not None and abs_hmin is not None and hum_av < abs_hmin)
        h_low_recover = (hum_av is not None and abs_hmin is not None and hum_av >= (abs_hmin + HUM_HYST))

        h_high_breach = (hum_av is not None and abs_hmax is not None and hum_av > abs_hmax)
        h_high_recover = (hum_av is not None and abs_hmax is not None and hum_av <= (abs_hmax - HUM_HYST))

        w_low_breach = (water_c is not None and abs_wmin is not None and water_c < abs_wmin)
        w_low_recover = (water_c is not None and abs_wmin is not None and water_c >= (abs_wmin + WTMP_HYST))

        w_high_breach = (water_c is not None and abs_wmax is not None and water_c > abs_wmax)
        w_high_recover = (water_c is not None and abs_wmax is not None and water_c <= (abs_wmax - WTMP_HYST))

        # Reservoir cutoff breach (already debounced in your tracker output)
        below_cutoff_now   = bool(below_cutoff_now)
        cutoff_recover     = not below_cutoff_now

        # Evaluate each hard alert; ALWAYS call _edge_alert so recovery is seen
        hard_stop = False
        cutoff_msg = (
            f"Reservoir {float(below_cutoff_value or 0.0):.2f} kg ≤ cutoff "
            f"{float(gs.get('reservoir_pump_cutoff_water_kg', 0) or 0.0):.2f} kg"
        )
        hard_stop |= _edge_alert(
            "reservoir_cutoff",
            below_cutoff_now,       # breach_now
            cutoff_recover,         # recovered_now
            cutoff_msg,
            REMIND_COOLDOWN_S,
            payload={
                "gross_kg": info.get("gross_kg"),
                "water_kg": info.get("water_kg"),
                "cutoff_kg": float(gs.get("reservoir_pump_cutoff_water_kg", 0) or 0.0),
            },
        )



        if abs_tmin is not None:
            hard_stop |= _edge_alert(
            "temp_hard_low",
            t_low_breach,
            t_low_recover,
            f"Temperature {air_t:.1f}°C below hard minimum {abs_tmin}°C",
            REMIND_COOLDOWN_S,
            payload={"air_t": air_t, "min_c": abs_tmin, "hyst_c": TEMP_HYST},
        )

        if abs_tmax is not None:
            hard_stop |= _edge_alert(
            "temp_hard_high",
            t_high_breach,
            t_high_recover,
            f"Temperature {air_t:.1f}°C above hard maximum {abs_tmax}°C",
            REMIND_COOLDOWN_S,
            payload={"air_t": air_t, "max_c": abs_tmax, "hyst_c": TEMP_HYST},
        )

        if abs_hmin is not None:
            hard_stop |= _edge_alert(
            "hum_hard_low",
            h_low_breach,
            h_low_recover,
            f"Humidity {hum_av:.1f}% below hard minimum {abs_hmin}%",
            REMIND_COOLDOWN_S,
            payload={"humidity": hum_av, "min_pct": abs_hmin, "hyst_pct": HUM_HYST},
        )

        if abs_hmax is not None:
            hard_stop |= _edge_alert(
            "hum_hard_high",
            h_high_breach,
            h_high_recover,
            f"Humidity {hum_av:.1f}% above hard maximum {abs_hmax}%",
            REMIND_COOLDOWN_S,
            payload={"humidity": hum_av, "max_pct": abs_hmax, "hyst_pct": HUM_HYST},
        )

        if abs_wmin is not None:
            hard_stop |= _edge_alert(
            "water_temp_low",
            w_low_breach,
            w_low_recover,
            f"Water temperature {water_c:.1f}°C below hard minimum {abs_wmin}°C",
            REMIND_COOLDOWN_S,
            payload={"water_c": water_c, "min_c": abs_wmin, "hyst_c": WTMP_HYST},
        )

        if abs_wmax is not None:
            hard_stop |= _edge_alert(
            "water_temp_high",
            w_high_breach,
            w_high_recover,
            f"Water temperature {water_c:.1f}°C above hard maximum {abs_wmax}°C",
            REMIND_COOLDOWN_S,
            payload={"water_c": water_c, "max_c": abs_wmax, "hyst_c": WTMP_HYST},
        )


        if hard_stop:
            # Apply your existing 'all outputs OFF' behaviour while any hard fault is active.
            status_data["last_error"] = status_data.get("last_error")  # unchanged; set by _edge_alert
            _set_fan(False);        status_data["fan_state"] = "OFF"
            _set_heater(False);     status_data["heater_state"] = "OFF"
            _set_humidifier(False); status_data["humidifier_state"] = "OFF"
            if status_data.get("agitator_state") == "ON":
                _set_agitator(False); status_data["agitator_state"] = "OFF"
            if status_data.get("air_pump_state") == "ON":
                _set_air_pump(False); status_data["air_pump_state"] = "OFF"
            status_data["agitator_phase_end_ts"] = None
            status_data["agitator_time_remaining_s"] = None
            status_data["air_pump_phase_end_ts"] = None
            status_data["air_pump_time_remaining_s"] = None
            if pump_configured:
                _set_main_pump(False); status_data["pump_state"] = "OFF"
                if STOP_EVENT.wait(timeout=1.0): return
            # Skip the rest of this tick while in hard-stop
            continue
        
        
        # --- Force-clear phantom errors if nothing is active anymore ---
        try:
            alerts = status_data.get("alert_states", {})
            any_active = any(bool(v.get("active")) for v in alerts.values())
            if not any_active and status_data.get("last_error"):
                status_data["last_error"] = None
        except Exception:
            pass






        # ─── Climate control (fan/heater/humidifier) ───
        if _last_temp is None or _last_humidity is None:
            _set_fan(False);        status_data["fan_state"] = "OFF"
            _set_heater(False);     status_data["heater_state"] = "OFF"
            _set_humidifier(False); status_data["humidifier_state"] = "OFF"
        else:
            fan_should_on = fan_on
            fan_cause = None
            if not status_data.get("paused", False):
                t_max = status_data.get("temperature_max")
                h_max = status_data.get("humidity_max")
                if _last_temp is not None and t_max is not None:
                    HYST_EX_T = float((gs.get("hysteresis_temp_extractor_c")
                                      if gs.get("hysteresis_temp_extractor_c") is not None
                                      else gs.get("hysteresis_temp_c", 0.5)) or 0.0)
                    if _last_temp > t_max:
                        fan_should_on = True; fan_cause = "temperature"
                    elif _last_temp < (t_max - HYST_EX_T):
                        fan_should_on = False
                if _last_humidity is not None and h_max is not None:
                    HYST_EX_H = float((gs.get("hysteresis_humidity_extractor_pct")
                                      if gs.get("hysteresis_humidity_extractor_pct") is not None
                                      else gs.get("hysteresis_humidity_pct", 1.5)) or 0.0)
                    if _last_humidity > h_max:
                        fan_should_on = True
                        if not (_last_temp is not None and t_max is not None and _last_temp > t_max):
                            fan_cause = "humidity"
                    elif _last_humidity < (h_max - HYST_EX_H):
                        if not (_last_temp is not None and t_max is not None and _last_temp > t_max):
                            fan_should_on = False
            fan_min_on = int(gs.get("fan_min_on_s", 0) or 0)
            if not fan_should_on and fan_on and fan_on_since is not None and (_mono() - fan_on_since) < fan_min_on:
                fan_should_on = True
            global fan_trigger_cause
            if not fan_on and fan_should_on:
                fan_trigger_cause = fan_cause or "temperature"
            status_data["fan_state"] = "ON" if fan_should_on else "OFF"
            _set_fan(fan_should_on)

            # heater
            t_min = status_data.get("temperature_min")
            if t_min is not None and _last_temp is not None:
                HYST = float((gs.get("hysteresis_temp_heater_c")
                             if gs.get("hysteresis_temp_heater_c") is not None
                             else gs.get("hysteresis_temp_c", 0.5)) or 0.0)
                heater_should_on = heater_on
                if _last_temp < t_min:
                    heater_should_on = True
                elif _last_temp >= (t_min + HYST):
                    heater_should_on = False
                heater_min_on = int(gs.get("heater_min_on_s", 0) or 0)
                if (not heater_should_on) and heater_on and heater_on_since is not None and (_mono() - heater_on_since) < heater_min_on:
                    heater_should_on = True
                _set_heater(heater_should_on)
                status_data["heater_state"] = "ON" if heater_should_on else "OFF"

            # humidifier
            h_min = status_data.get("humidity_min")
            if h_min is not None and _last_humidity is not None:
                HUM_HYST = float((gs.get("hysteresis_humidity_humidifier_pct")
                                 if gs.get("hysteresis_humidity_humidifier_pct") is not None
                                 else gs.get("hysteresis_humidity_pct", 1.5)) or 0.0)
                humid_should_on = (status_data.get("humidifier_state") == "ON")
                if _last_humidity < h_min:
                    humid_should_on = True
                elif _last_humidity >= (h_min + HUM_HYST):
                    humid_should_on = False
                _set_humidifier(humid_should_on)
                status_data["humidifier_state"] = "ON" if humid_should_on else "OFF"

        # ─── Pump window hard gate ───
        win_on  = profile_data.get("pump", {}).get("_win_on_h")
        win_off = profile_data.get("pump", {}).get("_win_off_h")
        allowed_now = _in_hour_window(win_on, win_off)
        if not allowed_now:
            if status_data.get("pump_state") == "ON":
                status_data["pump_state"] = "OFF"
                _set_main_pump(False)
                status_data["pump_phase_end_ts"] = None
                status_data["pump_time_remaining_s"] = None
            if status_data.get("agitator_state") == "ON":
                _set_agitator(False); status_data["agitator_state"] = "OFF"
                status_data["agitator_phase_end_ts"] = None
                status_data["agitator_time_remaining_s"] = None
            if status_data.get("air_pump_state") == "ON":
                _set_air_pump(False); status_data["air_pump_state"] = "OFF"
                status_data["air_pump_phase_end_ts"] = None
                status_data["air_pump_time_remaining_s"] = None
            next_on_due_at = _next_window_open_ts(win_on, win_off, now_dt=datetime.datetime.now())
            if STOP_EVENT.wait(0.25):
                return
            continue


        # ─── Scheduler (pump + premix) ───
        if pump_off <= 0 and pump_on <= 0:
            # disabled
            if status_data.get("pump_state") == "ON":
                status_data["pump_state"] = "OFF"
                _set_main_pump(False)
            if status_data.get("agitator_state") == "ON":
                _set_agitator(False); status_data["agitator_state"] = "OFF"
                status_data["agitator_phase_end_ts"] = None
                status_data["agitator_time_remaining_s"] = None
            if status_data.get("air_pump_state") == "ON":
                _set_air_pump(False); status_data["air_pump_state"] = "OFF"
                status_data["air_pump_phase_end_ts"] = None
                status_data["air_pump_time_remaining_s"] = None
        else:
            if status_data.get("startup_kick", False):
                status_data.pop("startup_kick", None)
                _trigger_startup_premix(now, now_m)


            if status_data.get("pump_state") != "ON":
                if next_on_due_at is None:
                    next_on_due_at = _schedule_next_on(now)

                # Air premix: finish at pump start (clamped)
                if air_enabled and air_run > 0 and next_on_due_at is not None:
                    if (not air_started_this_cycle) and (now >= (next_on_due_at - air_run)):
                        _set_air_pump(True); status_data["air_pump_state"] = "ON"
                        air_timer = now_m; air_started_this_cycle = True
                        end_by_run  = air_timer + air_run
                        end_by_pump = now_m + max(0.0, float(next_on_due_at - now))
                        status_data["air_pump_phase_end_ts"] = min(end_by_run, end_by_pump)
                        try:
                            duration = max(0.0, float(status_data["air_pump_phase_end_ts"]) - float(air_timer))
                            status_data["air_pump_time_total_s"] = _resolve_total(duration, air_run)
                        except Exception:
                            status_data["air_pump_time_total_s"] = _round_positive(air_run)
                    ap_end = status_data.get("air_pump_phase_end_ts")
                    if status_data.get("air_pump_state") == "ON" and ap_end and now_m >= float(ap_end):
                        _set_air_pump(False); status_data["air_pump_state"] = "OFF"
                        status_data["air_pump_phase_end_ts"] = None
                        status_data["air_pump_time_remaining_s"] = None

                # Agitator premix
                if ag_enabled and ag_run > 0 and next_on_due_at is not None:
                    if (not agitator_started_this_cycle) and (now >= (next_on_due_at - ag_run)):
                        _set_agitator(True); status_data["agitator_state"] = "ON"
                        agitator_timer = now_m; agitator_started_this_cycle = True
                        end_by_run  = agitator_timer + ag_run
                        end_by_pump = now_m + max(0.0, float(next_on_due_at - now))
                        status_data["agitator_phase_end_ts"] = min(end_by_run, end_by_pump)
                        try:
                            duration = max(0.0, float(status_data["agitator_phase_end_ts"]) - float(agitator_timer))
                            status_data["agitator_time_total_s"] = _resolve_total(duration, ag_run)
                        except Exception:
                            status_data["agitator_time_total_s"] = _round_positive(ag_run)
                    a_end = status_data.get("agitator_phase_end_ts")
                    if status_data.get("agitator_state") == "ON" and a_end and now_m >= float(a_end):
                        _set_agitator(False); status_data["agitator_state"] = "OFF"
                        status_data["agitator_phase_end_ts"] = None
                        status_data["agitator_time_remaining_s"] = None

                # Turn ON main pump when due (after premix finishes or is clamped)
                if next_on_due_at is not None and now >= next_on_due_at:
                    status_data["pump_state"] = "ON"
                    pump_state, pump_timer = True, now_m
                    status_data["cycle_count"] += 1
                    _set_main_pump(True)
                    status_data["pump_phase_end_ts"] = now_m + pump_on
            else:
                # Pump is ON; turn OFF after pump_on seconds and schedule next
                if now_m - pump_timer >= pump_on:
                    status_data["pump_state"] = "OFF"
                    pump_state, pump_timer = False, now_m
                    _set_main_pump(False)
                    next_on_due_at = _schedule_next_on(now)
                    agitator_started_this_cycle = False
                    air_started_this_cycle = False
                    status_data["pump_phase_end_ts"] = None
                    status_data["pump_time_remaining_s"] = None
                    if status_data.get("agitator_state") == "ON":
                        _set_agitator(False); status_data["agitator_state"] = "OFF"
                    status_data["agitator_phase_end_ts"] = None
                    status_data["agitator_time_remaining_s"] = None
                    if status_data.get("air_pump_state") == "ON":
                        _set_air_pump(False); status_data["air_pump_state"] = "OFF"
                    status_data["air_pump_phase_end_ts"] = None
                    status_data["air_pump_time_remaining_s"] = None

        # ─── Update countdowns (for UI) ───
        if status_data.get("pump_state") == "ON":
            end_ts = status_data.get("pump_phase_end_ts")
            if isinstance(end_ts, (int, float)) and end_ts > 0:
                rem_val = float(end_ts) - now_m
            else:
                rem_val = float(pump_on) - (now_m - pump_timer)
                if rem_val > 0:
                    status_data["pump_phase_end_ts"] = now_m + rem_val
            status_data["pump_time_remaining_s"] = _round_remaining(rem_val)
        else:
            status_data["pump_time_remaining_s"] = None

        if status_data.get("agitator_state") == "ON":
            a_end = status_data.get("agitator_phase_end_ts")
            if not isinstance(a_end, (int, float)) or a_end <= 0:
                a_end = agitator_timer + float(ag_run)
                status_data["agitator_phase_end_ts"] = a_end
            a_rem_f = a_end - now_m if isinstance(a_end, (int, float)) else None
            status_data["agitator_time_remaining_s"] = _round_remaining(a_rem_f)
        else:
            status_data["agitator_time_remaining_s"] = None
            status_data["agitator_phase_end_ts"] = None

        if status_data.get("air_pump_state") == "ON":
            ap_end = status_data.get("air_pump_phase_end_ts")
            if not isinstance(ap_end, (int, float)) or ap_end <= 0:
                ap_end = air_timer + float(air_run)
                status_data["air_pump_phase_end_ts"] = ap_end
            ap_rem_f = ap_end - now_m if isinstance(ap_end, (int, float)) else None
            status_data["air_pump_time_remaining_s"] = _round_remaining(ap_rem_f)
        else:
            status_data["air_pump_time_remaining_s"] = None
            status_data["air_pump_phase_end_ts"] = None

        # ─── Periodic state save (for resume) ───
        if now - last_state_save >= state_save_interval:
            save_state({
                "running_profile": profile_name,
                "start_time":      status_data["start_time"],
                "cycle_count":     status_data["cycle_count"],
                "pump_state":      status_data["pump_state"],
                "fan_state":       status_data["fan_state"],
                "last_temp":       _last_temp,
                "last_humidity":   _last_humidity,
                "paused":          status_data.get("paused", False)
            })
            last_state_save = now
            logging.info("✅ Saved state to disk.")

        # Deadline-aware sleep
        cands = []
        for key in ("pump_phase_end_ts", "agitator_phase_end_ts", "air_pump_phase_end_ts"):
            ts = status_data.get(key)
            if isinstance(ts, (int, float)) and ts > 0:
                cands.append(ts)
        now_m = _mono()
        if cands:
            sleep_s = max(0.02, min(0.05, min(cands) - now_m))
        else:
            sleep_s = 0.25
        if STOP_EVENT.wait(timeout=sleep_s):
            return

    # ───────────────────────────── Cleanup (thread exit) ───────────────────
    logging.info(f"⛔ Simulation for '{profile_name}' ended.")
    running_profile = None
    status_data.update(
        profile=None,
        pump_state="OFF",
        fan_state="OFF",
        heater_state="OFF",
        humidifier_state="OFF",
        start_time=None,

        # Clear ONLY profile thresholds – keep live readings intact so the
        # Ambient Sampler can continue showing data on the UI.
        temperature_min=None, temperature_target=None, temperature_max=None,
        humidity_min=None,    humidity_target=None,    humidity_max=None,
        water_temperature_min=None, water_temperature_target=None, water_temperature_max=None,
        water_quantity_min=None
        # Do NOT touch:
        #   humidity, temperature_c, temperature_top/bottom/avg/gradient,
        #   humidity_top/bottom, water_temperature
    )
    try: _set_fan(False)
    except Exception: pass
    try: _set_main_pump(False)
    except Exception: pass
    try: _set_agitator(False)
    except Exception: pass
    try: _set_air_pump(False)
    except Exception: pass
    try: _set_heater(False)
    except Exception: pass
    try: _set_humidifier(False)
    except Exception: pass
    status_data["agitator_state"] = "OFF"
    status_data["air_pump_state"] = "OFF"
    status_data["air_pump_phase_end_ts"] = None
    status_data["air_pump_time_remaining_s"] = None
    clear_state()




# ───────────────────────── CTX: shared deps for BPs ───────────────────────
def _get_running_profile() -> Optional[str]:
    return running_profile

def _set_running_profile(name: Optional[str]):
    global running_profile
    running_profile = name

def _start_sim_thread(profile_name: str, pdata: dict) -> threading.Thread:
    """Start the control loop thread for a given profile."""
    global SIM_THREAD
    STOP_EVENT.clear()
    t = threading.Thread(target=simulate_profile, args=(profile_name, pdata), daemon=False)
    t.start()
    SIM_THREAD = t
    return t

# ───────────────────────── Small parsing helpers for blueprints ─────────────────────────
def _to_int(v, default=None):
    """Parse an integer from form values like '12' or '12.0'. Returns default on failure."""
    try:
        if v is None or (isinstance(v, str) and v.strip() == ""):
            return default
        return int(float(v))
    except Exception:
        return default

def _to_float(v, default=None):
    """Parse a float from form values. Returns default on failure."""
    try:
        if v is None or (isinstance(v, str) and v.strip() == ""):
            return default
        return float(v)
    except Exception:
        return default

def _parse_bool(v, default=False):
    """Accepts checkboxes/strings like 'on', 'true', '1'."""
    if isinstance(v, bool):
        return v
    s = str(v or "").strip().lower()
    if s in ("1", "true", "yes", "on", "checked"):
        return True
    if s in ("0", "false", "no", "off", ""):
        return False
    return bool(default)

def _clamp_hour(h, default=None):
    """Clamp an hour into [0, 23]. Returns default if parsing fails."""
    x = _to_int(h, None)
    if x is None:
        return default
    if x < 0: x = 0
    if x > 23: x = 23
    return x



# ───────────────────────── Small parsing helpers for blueprints ─────────────────────────
def _slugify(name: str, maxlen: int = 80) -> str:
    s = unicodedata.normalize("NFKD", str(name or "")).encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-").lower()
    s = s[:maxlen].strip("-")
    return s or "profile"





CTX = {
    # state & io
    "status_data":          status_data,
    "get_running_profile":  _get_running_profile,
    "set_running_profile":  _set_running_profile,
    "start_sim_thread":     _start_sim_thread,
    "STOP_EVENT":           STOP_EVENT,

    # logging & stores
    "LOGGER":               LOGGER,
    "save_state":           save_state,
    "load_state":           load_state,
    "clear_state":          clear_state,

    # globals/settings
    "load_global_settings": load_global_settings,
    "save_global_settings": save_global_settings,
    "validate_settings":    validate_settings,
    "GLOBAL_DEFAULTS":      GLOBAL_DEFAULTS,
    "global_settings_ref":  lambda: global_settings,   # read current snapshot
    "set_global_settings":  lambda d: globals().__setitem__('global_settings', d),

    # devices/helpers
    "apply_outputs_from_status": apply_outputs_from_status,
    "_ensure_gpio_mode":         _ensure_gpio_mode,

    # profile helpers & dirs (used in blueprint route logic)
    "PROFILE_DIR":               PROFILE_DIR,
    "ARCHIVE_DIR":               ARCHIVE_DIR,
    
    # parsing helpers needed by web/profiles_routes.py
    "_to_int":                   _to_int,         
    "_to_float":                 _to_float,        
    "_parse_bool":               _parse_bool,      
    "_clamp_hour":               _clamp_hour,    
    "_slugify":                  _slugify,  

    # UI helpers
    "compute_banner":            compute_banner,

    # scale sampler (for teardown)
    "SCALE_SAMPLER":             SCALE_SAMPLER,
    

}
app.config["CTX"] = CTX  # make available to blueprints via current_app.config["CTX"]






# ─────────────────────────── Register blueprints ───────────────────────────
from web.profiles_routes import bp as profiles_bp
from web.control_routes import bp as control_bp
from web.system_routes import bp as system_bp

app.register_blueprint(system_bp)       # '/', '/status.json', '/settings/global', etc.
app.register_blueprint(profiles_bp)     # '/profiles', '/new', '/edit/...', archive/restore/duplicate
app.register_blueprint(control_bp)      # '/run/...', '/stop', '/pause', '/unpause', '/resume', '/dismiss-resume'
app.register_blueprint(reservoirs_bp)   # for the reservoir management

# Scale settings + raw data API (keeps same routes as before)
from sensors.scale_api import scale_bp
app.register_blueprint(scale_bp)     # '/settings/scale', '/api/scale/...'

# ───────────────────────── Cleanup & shutdown ──────────────────────────────
def cleanup_gpio():
    """Delegate all actuator GPIO OFF + release to devices, then clean up local sensors."""
    try:
        devices_cleanup_gpio()
    except Exception:
        pass

def _ordered_shutdown():
    """Stop worker thread(s) cleanly, then do GPIO cleanup."""
    # 0a) Stop the scale sampler first so HX711 is released
    try:
        SCALE_SAMPLER.stop()
    except Exception:
        pass

    # 0b) Stop the Discord alert worker
    try:
        stop_alert_worker()
    except Exception:
        pass

    # 1) Signal the control loop to stop
    try:
        STOP_EVENT.set()
    except Exception:
        pass

    # 2) Wait briefly for the worker thread to exit
    try:
        if SIM_THREAD is not None and SIM_THREAD.is_alive():
            SIM_THREAD.join(timeout=5.0)
    except Exception:
        pass

    # 3) Ensure GPIO mode is valid for any final OFF writes
    try:
        _ensure_gpio_mode()
    except Exception:
        pass

    # 4) Perform cleanup (devices → sensors)
    try:
        cleanup_gpio()
    except Exception:
        # Last resort direct cleanup
        try:
            GPIO.cleanup()
        except Exception:
            pass

atexit.register(_ordered_shutdown)
atexit.register(lambda: SCALE_SAMPLER.stop())
atexit.register(lambda: AMBIENT_SAMPLER.stop())

# ─────────────────────────────── Entrypoint ────────────────────────────────
if __name__ == '__main__':
    try:
        # Single process only; no reloader (avoid duplicate GPIO threads)
        app.run(host='0.0.0.0', port=5000, debug=True, use_reloader=False)
    except KeyboardInterrupt:
        print("\nCtrl+C received — shutting down cleanly…")
    finally:
        try:
            signal.signal(signal.SIGINT, signal.SIG_IGN)
        except Exception:
            pass
        try:
            _ordered_shutdown()
        except NameError:
            try:
                cleanup_gpio()
            except Exception:
                pass



