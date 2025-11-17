# devices/__init__.py
"""
Devices package façade exposing the exact actuator API used by app.py today.
This keeps app.py stable while we later split internals into relays/fans/heaters/pumps.

Public API (compat with current app.py):
- init_actuators(LOGGER, status_data, send_discord_fn)
- apply_outputs_from_status()
- cleanup_gpio()
- _ensure_gpio_mode()

- _set_fan(on: bool)
- _set_heater(on: bool)
- _set_humidifier(on: bool)
- _set_agitator(on: bool)
- _set_air_pump(on: bool)
- _set_concentrate_mix(on: bool)

Also exports runtime flags (names preserved):
- fan_configured, pump_configured, heater_configured, humidifier_configured, agitator_configured, air_pump_configured
- fan_on, heater_on, humidifier_on, agitator_on, air_pump_on
- fan_on_since, heater_on_since
- fan_trigger_cause
"""

import os
os.environ.setdefault("BLINKA_PIN_FACTORY", "RPiGPIO")

from time import monotonic as _mono
import RPi.GPIO as GPIO
from gpiozero import Device
from gpiozero.pins.rpigpio import RPiGPIOFactory
Device.pin_factory = RPiGPIOFactory()

# ---- External dependencies (late-bound by init_actuators) -----------------
_LOGGER = None
_status_ref = None   # dict or callable returning dict
_send_discord = None

def _status():
    return _status_ref() if callable(_status_ref) else _status_ref

def init_actuators(LOGGER, status_data, send_discord_fn):
    """
    Call once from app.py after LOGGER and status_data exist.
    """
    global _LOGGER, _status_ref, _send_discord
    _LOGGER = LOGGER
    _status_ref = status_data
    _send_discord = send_discord_fn

# ---- Pins & polarity (identical to current app.py) ------------------------
FAN_PIN         = 22
MAIN_PUMP_PIN   = 27
HEATER_PIN      = 24
HUMIDIFIER_PIN  = 18
AGITATOR_PIN    = 5
CONCENTRATE_MIX_PIN = 7
AIR_PUMP_PIN    = 25
NUTRIENT_A_PIN  = 6
NUTRIENT_B_PIN  = 26


FAN_ACTIVE_HIGH         = True
HEATER_ACTIVE_HIGH      = True
HUMIDIFIER_ACTIVE_HIGH  = True
PUMP_ACTIVE_HIGH        = True
AGITATOR_ACTIVE_HIGH    = True
CONCENTRATE_MIX_ACTIVE_HIGH = True
AIR_PUMP_ACTIVE_HIGH    = True
NUTRIENT_ACTIVE_HIGH    = True

GPIO.setwarnings(False)
GPIO.setmode(GPIO.BCM)

def _on_level(active_high: bool):   return GPIO.HIGH if active_high else GPIO.LOW
def _off_level(active_high: bool):  return GPIO.LOW if active_high else GPIO.HIGH

def _ensure_gpio_mode():
    if GPIO.getmode() is None:
        GPIO.setwarnings(False)
        GPIO.setmode(GPIO.BCM)

# ---- Configured flags (names preserved) -----------------------------------
fan_configured = pump_configured = heater_configured = humidifier_configured = False
agitator_configured = False
concentrate_mix_configured = False
air_pump_configured = False
nutrient_a_configured = False
nutrient_b_configured = False

fan_on = heater_on = humidifier_on = False
agitator_on = False
concentrate_mix_on = False
air_pump_on = False

fan_trigger_cause = None  # "temperature" | "humidity" | None

# Anti short-cycle timers
fan_on_since = None
heater_on_since = None
humidifier_on_since = None

# ---- Hardware setup (unchanged) ------------------------------------------
try:
    GPIO.setup(FAN_PIN, GPIO.OUT, initial=_off_level(FAN_ACTIVE_HIGH))
    fan_configured = True
except Exception as e:
    print(f"⚠️ Failed to setup FAN_PIN: {e}")

try:
    GPIO.setup(MAIN_PUMP_PIN, GPIO.OUT, initial=_off_level(PUMP_ACTIVE_HIGH))
    pump_configured = True
except Exception as e:
    print(f"⚠️ Failed to setup MAIN_PUMP_PIN: {e}")

try:
    GPIO.setup(HEATER_PIN, GPIO.OUT, initial=_off_level(HEATER_ACTIVE_HIGH))
    heater_configured = True
except Exception as e:
    print(f"⚠️ Failed to setup HEATER_PIN: {e}")

try:
    GPIO.setup(HUMIDIFIER_PIN, GPIO.OUT, initial=_off_level(HUMIDIFIER_ACTIVE_HIGH))
    humidifier_configured = True
except Exception as e:
    print(f"⚠️ Failed to setup HUMIDIFIER_PIN: {e}")

try:
    GPIO.setup(AGITATOR_PIN, GPIO.OUT, initial=_off_level(AGITATOR_ACTIVE_HIGH))
    agitator_configured = True
except Exception as e:
    print(f"⚠️ Failed to setup AGITATOR_PIN: {e}")

try:
    GPIO.setup(CONCENTRATE_MIX_PIN, GPIO.OUT, initial=_off_level(CONCENTRATE_MIX_ACTIVE_HIGH))
    concentrate_mix_configured = True
except Exception as e:
    print(f"⚠️ Failed to setup CONCENTRATE_MIX_PIN: {e}")

try:
    GPIO.setup(AIR_PUMP_PIN, GPIO.OUT, initial=_off_level(AIR_PUMP_ACTIVE_HIGH))
    air_pump_configured = True
except Exception as e:
    print(f"⚠️ Failed to setup AIR_PUMP_PIN: {e}")
    
try:
    GPIO.setup(NUTRIENT_A_PIN, GPIO.OUT, initial=_off_level(NUTRIENT_ACTIVE_HIGH))
    nutrient_a_configured = True
except Exception as e:
    print(f"⚠️ Failed to setup NUTRIENT_A_PIN: {e}")

try:
    GPIO.setup(NUTRIENT_B_PIN, GPIO.OUT, initial=_off_level(NUTRIENT_ACTIVE_HIGH))
    nutrient_b_configured = True
except Exception as e:
    print(f"⚠️ Failed to setup NUTRIENT_B_PIN: {e}")







# ---- Device setters (unchanged behaviour + structured logs) --------------
def _set_fan(on: bool, *, log: bool = True, notify: bool = True):
    global fan_on, fan_on_since, fan_trigger_cause
    if not fan_configured or on == fan_on:
        return
    GPIO.output(FAN_PIN, _on_level(FAN_ACTIVE_HIGH) if on else _off_level(FAN_ACTIVE_HIGH))
    if on and not fan_on:
        fan_on_since = _mono()
    if not on:
        fan_on_since = None
    fan_on = on

    if log:
        try:
            if _LOGGER is not None:
                sd = _status()
                _LOGGER.log_event(
                    "actuator_change",
                    msg=f"Extractor fan {'ON' if fan_on else 'OFF'}",
                    reason_code=(
                        "humidity_high"
                        if (fan_trigger_cause == "humidity")
                        else ("temp_high" if fan_on else "hysteresis_clear")
                    ),
                    profile_id=sd.get("profile") if isinstance(sd, dict) else None,
                    actor="rule_engine",
                    payload={
                        "device_name": "<extractor fan>",
                        "after_state": ("on" if on else "off"),
                        "air_temp_c": sd.get("temperature_c") if isinstance(sd, dict) else None,
                        "air_rh_pct": sd.get("humidity") if isinstance(sd, dict) else None,
                        "water_temp_c": sd.get("water_temperature") if isinstance(sd, dict) else None,
                        "reservoir_water_kg": sd.get("reservoir_water_kg") if isinstance(sd, dict) else None,
                    },
                )
        except Exception:
            pass

    if notify:
        try:
            if _send_discord:
                if fan_on:
                    cause = fan_trigger_cause or "temperature"
                    if cause == "humidity":
                        _send_discord("Feels like a rainforest in here… Extracting moisture: **Extractor Fan: ON**")
                    else:
                        _send_discord("Good heavens it's warm… Exchanging some air: **Extractor Fan: ON**")
            fan_trigger_cause = None
        except Exception:
            pass


def _set_heater(on: bool, *, log: bool = True, notify: bool = True):
    global heater_on, heater_on_since
    if not heater_configured or on == heater_on:
        return
    GPIO.output(HEATER_PIN, _on_level(HEATER_ACTIVE_HIGH) if on else _off_level(HEATER_ACTIVE_HIGH))
    if on and not heater_on:
        heater_on_since = _mono()
    if not on:
        heater_on_since = None
    heater_on = on

    if log:
        try:
            if _LOGGER is not None:
                sd = _status()
                _LOGGER.log_event(
                    "actuator_change",
                    msg=f"Heater {'ON' if heater_on else 'OFF'}",
                    reason_code=("temp_below_min" if heater_on else "hysteresis_clear"),
                    profile_id=sd.get("profile") if isinstance(sd, dict) else None,
                    actor="rule_engine",
                    payload={
                        "device_name": "<heater>",
                        "after_state": ("on" if on else "off"),
                        "trigger": sd.get("last_trigger") if isinstance(sd, dict) else None,
                        "air_temp_c": sd.get("temperature_c") if isinstance(sd, dict) else None,
                        "air_rh_pct": sd.get("humidity") if isinstance(sd, dict) else None,
                        "water_temp_c": sd.get("water_temperature") if isinstance(sd, dict) else None,
                        "reservoir_water_kg": sd.get("reservoir_water_kg") if isinstance(sd, dict) else None,
                    },
                )
        except Exception:
            pass

    if notify:
        try:
            if _send_discord and heater_on:
                _send_discord("Brrr. It's a little chilly...️ Adding some heat: **Heater: ON**")
        except Exception:
            pass


def _set_humidifier(on: bool, *, log: bool = True, notify: bool = True):
    global humidifier_on, humidifier_on_since
    if not humidifier_configured or on == humidifier_on:
        return
    GPIO.output(HUMIDIFIER_PIN, _on_level(HUMIDIFIER_ACTIVE_HIGH) if on else _off_level(HUMIDIFIER_ACTIVE_HIGH))
    if on and not humidifier_on:
        humidifier_on_since = _mono()
    if not on:
        humidifier_on_since = None
    humidifier_on = on

    if log:
        try:
            if _LOGGER is not None:
                sd = _status()
                _LOGGER.log_event(
                    "actuator_change",
                    msg=f"Humidifier {'ON' if humidifier_on else 'OFF'}",
                    reason_code=("humidity_below_min" if humidifier_on else "hysteresis_clear"),
                    profile_id=sd.get("profile") if isinstance(sd, dict) else None,
                    actor="rule_engine",
                    payload={
                        "device_name": "<humidifier>",
                        "after_state": ("on" if on else "off"),
                        "air_temp_c": sd.get("temperature_c") if isinstance(sd, dict) else None,
                        "air_rh_pct": sd.get("humidity") if isinstance(sd, dict) else None,
                        "water_temp_c": sd.get("water_temperature") if isinstance(sd, dict) else None,
                        "reservoir_water_kg": sd.get("reservoir_water_kg") if isinstance(sd, dict) else None,
                    },
                )
        except Exception:
            pass

    if notify:
        try:
            if _send_discord and humidifier_on:
                _send_discord("Paahh. It's dry in here...️ Adding some humidity: **Humidifier: ON**")
        except Exception:
            pass


def _set_agitator(on: bool, *, log: bool = True, notify: bool = True):
    global agitator_on
    if not agitator_configured or on == agitator_on:
        return
    GPIO.output(AGITATOR_PIN, _on_level(AGITATOR_ACTIVE_HIGH) if on else _off_level(AGITATOR_ACTIVE_HIGH))
    agitator_on = on

    if log:
        try:
            if _LOGGER is not None:
                sd = _status()
                _LOGGER.log_event(
                    "irrigation_cycle",
                    msg=f"Agitator {'ON' if on else 'OFF'}",
                    reason_code=("premix" if on else "premix_end"),
                    profile_id=sd.get("profile") if isinstance(sd, dict) else None,
                    actor="scheduler",
                )
        except Exception:
            pass


def _set_air_pump(on: bool, *, log: bool = True, notify: bool = True):
    global air_pump_on
    if not air_pump_configured or on == air_pump_on:
        return
    GPIO.output(AIR_PUMP_PIN, _on_level(AIR_PUMP_ACTIVE_HIGH) if on else _off_level(AIR_PUMP_ACTIVE_HIGH))
    air_pump_on = on

    if log:
        try:
            if _LOGGER is not None:
                sd = _status()
                _LOGGER.log_event(
                    "irrigation_cycle",
                    msg=f"Air pump {'ON' if on else 'OFF'}",
                    reason_code=("premix" if on else "premix_end"),
                    profile_id=sd.get("profile") if isinstance(sd, dict) else None,
                    actor="scheduler",
                )
        except Exception:
            pass
    try:
        sd = _status()
        if isinstance(sd, dict):
            sd["air_pump_state"] = "ON" if on else "OFF"
            if not on:
                sd["air_pump_phase_end_ts"] = None
                sd["air_pump_time_remaining_s"] = None
    except Exception:
        pass


def _set_concentrate_mix(on: bool, *, log: bool = True, notify: bool = True):
    """Toggle the concentrate mix relay on GPIO pin 7."""
    global concentrate_mix_on
    if not concentrate_mix_configured or on == concentrate_mix_on:
        return

    _ensure_gpio_mode()
    GPIO.output(
        CONCENTRATE_MIX_PIN,
        _on_level(CONCENTRATE_MIX_ACTIVE_HIGH) if on else _off_level(CONCENTRATE_MIX_ACTIVE_HIGH),
    )
    concentrate_mix_on = on

    try:
        sd = _status()
        if isinstance(sd, dict):
            sd["concentrate_mix_state"] = "ON" if on else "OFF"
    except Exception:
        pass

    if log:
        try:
            if _LOGGER is not None:
                sd = _status()
                _LOGGER.log_event(
                    "reservoir_mix",
                    msg=f"Concentrate mix relay {'ON' if on else 'OFF'}",
                    reason_code=("mix" if on else "mix_end"),
                    profile_id=sd.get("profile") if isinstance(sd, dict) else None,
                    actor="wizard",
                )
        except Exception:
            pass


def _set_nutrient_a(on: bool, *, log: bool = True, notify: bool = True):
    if not nutrient_a_configured:
        return
    _ensure_gpio_mode()
    GPIO.output(NUTRIENT_A_PIN, _on_level(NUTRIENT_ACTIVE_HIGH) if on else _off_level(NUTRIENT_ACTIVE_HIGH))

    # --- NEW: reflect in shared status_data
    try:
        sd = _status()
        if isinstance(sd, dict):
            sd["nutrient_A_on"] = bool(on)
            if on:
                sd["dosing_phase"] = "A"
                sd["dosing_running"] = True
            else:
                # If B isn't running, dosing is no longer running and phase clears
                b_on = bool(sd.get("nutrient_B_on"))
                sd["dosing_running"] = b_on
                if not b_on:
                    sd["dosing_phase"] = None
    except Exception:
        pass

    # Optional: structured log (unchanged)
    if log:
        try:
            if _LOGGER is not None:
                sd = _status()
                (_LOGGER.log_event)(
                    "actuator_change",
                    msg=f"Nutrient pump A {'ON' if on else 'OFF'}",
                    reason_code="nutrient_a_toggle",
                    profile_id=sd.get("profile") if isinstance(sd, dict) else None,
                    actor="wizard_or_calibration",
                    payload={"device_name": "nutrient_pump_a", "after_state": ("on" if on else "off")},
                )
        except Exception:
            pass


def _set_nutrient_b(on: bool, *, log: bool = True, notify: bool = True):
    if not nutrient_b_configured:
        return
    _ensure_gpio_mode()
    GPIO.output(NUTRIENT_B_PIN, _on_level(NUTRIENT_ACTIVE_HIGH) if on else _off_level(NUTRIENT_ACTIVE_HIGH))

    # --- NEW: reflect in shared status_data
    try:
        sd = _status()
        if isinstance(sd, dict):
            sd["nutrient_B_on"] = bool(on)
            if on:
                sd["dosing_phase"] = "B"
                sd["dosing_running"] = True
            else:
                a_on = bool(sd.get("nutrient_A_on"))
                sd["dosing_running"] = a_on
                if not a_on:
                    sd["dosing_phase"] = None
    except Exception:
        pass

    # Optional: structured log (unchanged)
    if log:
        try:
            if _LOGGER is not None:
                sd = _status()
                (_LOGGER.log_event)(
                    "actuator_change",
                    msg=f"Nutrient pump B {'ON' if on else 'OFF'}",
                    reason_code="nutrient_b_toggle",
                    profile_id=sd.get("profile") if isinstance(sd, dict) else None,
                    actor="wizard_or_calibration",
                    payload={"device_name": "nutrient_pump_b", "after_state": ("on" if on else "off")},
                )
        except Exception:
            pass









pump_on = False  # track state for idempotence

def _set_main_pump(on: bool, *, log: bool = True, notify: bool = True):
    global pump_on
    if not pump_configured or on == pump_on:
        return
    GPIO.output(MAIN_PUMP_PIN, _on_level(PUMP_ACTIVE_HIGH) if on else _off_level(PUMP_ACTIVE_HIGH))
    pump_on = on
    
    if log:
        try:
            if _LOGGER is not None:
                sd = _status()
                _LOGGER.log_event(
                    "irrigation_cycle",
                    msg=f"Main irrigation pump {'ON' if on else 'OFF'}",
                    reason_code=("cycle_start" if on else "cycle_end"),
                    profile_id=sd.get("profile") if isinstance(sd, dict) else None,
                    actor="scheduler",
                )
        except Exception:
            pass







# ---- Sync + cleanup -------------------------------------------------------
def apply_outputs_from_status():
    try:
        sd = _status()
        if not isinstance(sd, dict):
            return
        if pump_configured:
            GPIO.output(MAIN_PUMP_PIN, _on_level(PUMP_ACTIVE_HIGH) if str(sd.get("pump_state")) == "ON" else _off_level(PUMP_ACTIVE_HIGH))
        if fan_configured:
            GPIO.output(FAN_PIN, _on_level(FAN_ACTIVE_HIGH) if str(sd.get("fan_state")) == "ON" else _off_level(FAN_ACTIVE_HIGH))
        if heater_configured:
            GPIO.output(HEATER_PIN, _on_level(HEATER_ACTIVE_HIGH) if str(sd.get("heater_state")) == "ON" else _off_level(HEATER_ACTIVE_HIGH))
        if humidifier_configured:
            GPIO.output(HUMIDIFIER_PIN, _on_level(HUMIDIFIER_ACTIVE_HIGH) if str(sd.get("humidifier_state")) == "ON" else _off_level(HUMIDIFIER_ACTIVE_HIGH))
        if agitator_configured:
            GPIO.output(AGITATOR_PIN, _on_level(AGITATOR_ACTIVE_HIGH) if str(sd.get("agitator_state")) == "ON" else _off_level(AGITATOR_ACTIVE_HIGH))
        if air_pump_configured:
            GPIO.output(AIR_PUMP_PIN, _on_level(AIR_PUMP_ACTIVE_HIGH) if str(sd.get("air_pump_state")) == "ON" else _off_level(AIR_PUMP_ACTIVE_HIGH))
    except Exception:
        pass

def cleanup_gpio():
    try:
        if fan_configured: GPIO.output(FAN_PIN, _off_level(FAN_ACTIVE_HIGH))
        if pump_configured: GPIO.output(MAIN_PUMP_PIN, _off_level(PUMP_ACTIVE_HIGH))
        if heater_configured: GPIO.output(HEATER_PIN, _off_level(HEATER_ACTIVE_HIGH))
        if humidifier_configured: GPIO.output(HUMIDIFIER_PIN, _off_level(HUMIDIFIER_ACTIVE_HIGH))
        if agitator_configured: GPIO.output(AGITATOR_PIN, _off_level(AGITATOR_ACTIVE_HIGH))
        if air_pump_configured: GPIO.output(AIR_PUMP_PIN, _off_level(AIR_PUMP_ACTIVE_HIGH))
        if nutrient_a_configured: GPIO.output(NUTRIENT_A_PIN, _off_level(NUTRIENT_ACTIVE_HIGH))
        if nutrient_b_configured: GPIO.output(NUTRIENT_B_PIN, _off_level(NUTRIENT_ACTIVE_HIGH))

    except Exception:
        pass
    try:
        GPIO.cleanup()
    except Exception:
        pass



