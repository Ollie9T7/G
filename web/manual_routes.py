import datetime
import threading
import time
import uuid
from time import monotonic as _mono

from flask import Blueprint, current_app, jsonify, render_template, request

from devices import (
    _set_air_pump,
    _set_agitator,
    _set_concentrate_mix,
    _set_fan,
    _set_heater,
    _set_humidifier,
    _set_main_pump,
    _set_nutrient_a,
    _set_nutrient_b,
)

bp = Blueprint("manual", __name__)


def ctx():
    return current_app.config["CTX"]


def status_data():
    return ctx()["status_data"]


def running_profile():
    return ctx()["get_running_profile"]()


def LOGGER():
    return ctx()["LOGGER"]


MANUAL_DEVICES = {
    "heater": {
        "label": "Heater",
        "state_key": "heater_state",
        "setter": _set_heater,
    },
    "extractor": {
        "label": "Extractor",
        "state_key": "fan_state",
        "setter": _set_fan,
    },
    "humidifier": {
        "label": "Humidifier",
        "state_key": "humidifier_state",
        "setter": _set_humidifier,
    },
    "main_pump": {
        "label": "Main Water Pump",
        "state_key": "pump_state",
        "setter": _set_main_pump,
    },
    "agitator_pump": {
        "label": "Agitator Pump",
        "state_key": "agitator_state",
        "setter": _set_agitator,
    },
    "air_pump": {
        "label": "Air Pump",
        "state_key": "air_pump_state",
        "setter": _set_air_pump,
    },
    "nutrient_pump_a": {
        "label": "Nutrient Pump A",
        "state_key": "nutrient_A_on",
        "setter": _set_nutrient_a,
        "bool_state": True,
    },
    "nutrient_pump_b": {
        "label": "Nutrient Pump B",
        "state_key": "nutrient_B_on",
        "setter": _set_nutrient_b,
        "bool_state": True,
    },
    "nutrient_stirrer": {
        "label": "Nutrient Stirrer",
        "state_key": "concentrate_mix_state",
        "setter": _set_concentrate_mix,
    },
}


def _state_string(val):
    if isinstance(val, str):
        return "ON" if val.strip().upper() == "ON" else "OFF"
    return "ON" if bool(val) else "OFF"


def _manual_overrides():
    return status_data().setdefault("manual_overrides", {})


def _log_manual(device_key: str, new_state: str, duration_s: float | None = None):
    label = MANUAL_DEVICES.get(device_key, {}).get("label", device_key)
    payload = {
        "device": device_key,
        "device_label": label,
        "after_state": new_state.lower(),
        "manual": True,
    }
    if duration_s is not None:
        payload["duration_s"] = round(duration_s, 2)
    try:
        LOGGER().log_event(
            "actuator_change",
            msg=f"{label} {'ON' if new_state == 'ON' else 'OFF'} via manual control",
            reason_code=f"manual_{new_state.lower()}",
            profile_id=running_profile(),
            actor="manual_override_ui",
            payload=payload,
        )
    except Exception:
        pass


def _apply_toggle(device_key: str, turn_on: bool):
    sd = status_data()
    device = MANUAL_DEVICES[device_key]
    setter = device["setter"]
    state_key = device.get("state_key")
    manual = _manual_overrides()
    entry = manual.setdefault(device_key, {})

    current_state = _state_string(sd.get(state_key)) if state_key else "OFF"
    desired_state = "ON" if turn_on else "OFF"

    now_m = _mono()
    if current_state == desired_state:
        if turn_on:
            entry.update(
                active=True,
                state="ON",
                since_mono=entry.get("since_mono") or now_m,
                since_iso=entry.get("since_iso") or datetime.datetime.utcnow().isoformat() + "Z",
                last_duration_s=None,
                run_seconds=None,
                run_until_mono=None,
                timer_token=None,
            )
            _log_manual(device_key, "ON", None)
        else:
            entry.update(
                active=False,
                state="OFF",
                since_mono=None,
                since_iso=None,
                run_seconds=None,
                run_until_mono=None,
                timer_token=None,
            )
            _log_manual(device_key, "OFF", None)
        return

    setter(turn_on, log=False, notify=False)
    if state_key:
        if device.get("bool_state"):
            sd[state_key] = bool(turn_on)
        else:
            sd[state_key] = desired_state

    if turn_on:
        entry.update(
            active=True,
            state="ON",
            since_mono=now_m,
            since_iso=datetime.datetime.utcnow().isoformat() + "Z",
            last_duration_s=None,
            run_seconds=None,
            run_until_mono=None,
            timer_token=None,
        )
        _log_manual(device_key, "ON", None)
    else:
        since = entry.get("since_mono")
        duration = None
        try:
            if since is not None:
                duration = max(0.0, float(now_m) - float(since))
        except Exception:
            duration = None
        entry.update(
            active=False,
            state="OFF",
            last_duration_s=duration,
            since_mono=None,
            since_iso=None,
            run_seconds=None,
            run_until_mono=None,
            timer_token=None,
        )
        _log_manual(device_key, "OFF", duration)


def _expire_manual_if_due(device_key: str):
    """If a timed manual run has elapsed, clear the override immediately.

    This keeps automation in control when a timer has finished even if the
    background timer thread was interrupted or UI polling is infrequent.
    """

    entry = _manual_overrides().get(device_key, {})
    if not entry.get("active"):
        return

    try:
        run_until = entry.get("run_until_mono")
        if run_until is None:
            return
        if float(run_until) > float(_mono()):
            return
    except Exception:
        return

    # Timer elapsed — turn the device off and clear the manual flags so
    # automation can resume control.
    _apply_toggle(device_key, False)
    entry.update(run_seconds=None, run_until_mono=None, run_until_iso=None, timer_token=None)


def _device_snapshot(device_key: str):
    _expire_manual_if_due(device_key)
    sd = status_data()
    device = MANUAL_DEVICES[device_key]
    state_key = device.get("state_key")
    manual_entry = _manual_overrides().get(device_key, {})
    state_val = sd.get(state_key) if state_key else False
    run_until = manual_entry.get("run_until_mono")
    run_remaining = None
    try:
        if run_until:
            run_remaining = max(0.0, float(run_until) - float(_mono()))
    except Exception:
        run_remaining = None
    return {
        "key": device_key,
        "label": device.get("label", device_key),
        "state": _state_string(state_val),
        "manual_active": bool(manual_entry.get("active")),
        "since": manual_entry.get("since_iso"),
        "last_duration_s": manual_entry.get("last_duration_s"),
        "run_seconds": manual_entry.get("run_seconds"),
        "run_until": manual_entry.get("run_until_iso"),
        "run_remaining_s": run_remaining,
    }


def _schedule_off_timer(app, device_key: str, token: str, seconds: float):
    def _worker():
        with app.app_context():
            time.sleep(max(0.0, seconds))
            entry = _manual_overrides().get(device_key, {})
            if entry.get("timer_token") != token:
                return
            if str(entry.get("state", "OFF")).upper() != "ON":
                return
            _apply_toggle(device_key, False)

    threading.Thread(target=_worker, daemon=True).start()


@bp.route("/manual")
def manual_page():
    return render_template("manual_override.html")


@bp.route("/manual/api/status")
def manual_status():
    devices = {_k: _device_snapshot(_k) for _k in MANUAL_DEVICES}
    return jsonify({
        "devices": devices,
        "running_profile": running_profile(),
        "manual_overrides": _manual_overrides(),
    })


@bp.route("/manual/api/toggle", methods=["POST"])
def manual_toggle():
    data = request.get_json(silent=True) or {}
    device_key = data.get("device")
    turn_on = bool(data.get("on"))
    if device_key not in MANUAL_DEVICES:
        return jsonify({"ok": False, "error": "Unknown device"}), 400

    _apply_toggle(device_key, turn_on)
    return jsonify({"ok": True, "devices": {_k: _device_snapshot(_k) for _k in MANUAL_DEVICES}})


@bp.route("/manual/api/run_for", methods=["POST"])
def manual_run_for():
    data = request.get_json(silent=True) or {}
    device_key = data.get("device")
    seconds = data.get("seconds")
    try:
        seconds = float(seconds)
    except Exception:
        seconds = 0

    if device_key not in MANUAL_DEVICES:
        return jsonify({"ok": False, "error": "Unknown device"}), 400
    if seconds <= 0:
        return jsonify({"ok": False, "error": "Seconds must be greater than zero"}), 400

    app = current_app._get_current_object()
    _apply_toggle(device_key, True)

    manual = _manual_overrides()
    entry = manual.setdefault(device_key, {})
    now_m = _mono()
    token = uuid.uuid4().hex
    entry.update(
        run_seconds=seconds,
        run_until_mono=now_m + seconds,
        run_until_iso=(datetime.datetime.utcnow() + datetime.timedelta(seconds=seconds)).isoformat() + "Z",
        timer_token=token,
    )

    _schedule_off_timer(app, device_key, token, seconds)

    return jsonify({"ok": True, "devices": {_k: _device_snapshot(_k) for _k in MANUAL_DEVICES}})


@bp.route("/manual/api/stop", methods=["POST"])
def manual_stop():
    data = request.get_json(silent=True) or {}
    device_key = data.get("device")
    if device_key not in MANUAL_DEVICES:
        return jsonify({"ok": False, "error": "Unknown device"}), 400

    entry = _manual_overrides().setdefault(device_key, {})
    entry.update(run_seconds=None, run_until_mono=None, run_until_iso=None, timer_token=None)
    _apply_toggle(device_key, False)

    return jsonify({"ok": True, "devices": {_k: _device_snapshot(_k) for _k in MANUAL_DEVICES}})
