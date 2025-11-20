# sensors/humidity_scale_api.py
import json, time
from flask import Blueprint, jsonify, render_template, request, current_app

from sensors.humidity_scale import _HUM_SCALE_LOCK, _scale_read_counts, _load_humidity_scale_cal, CAL_PATH
from global_settings import load_global_settings

humidity_scale_bp = Blueprint("humidity_scale", __name__)

_scale_session = {"baseline": None, "captured_at": None}

@humidity_scale_bp.route("/settings/humidity-scale", methods=["GET"])
def settings_humidity_scale():
    return render_template("calibrate_humidity_scale.html", cal=_load_humidity_scale_cal())

@humidity_scale_bp.route("/api/humidity-scale/raw", methods=["GET"])
def api_humidity_scale_raw():
    try:
        current_app.config["CTX"]["_ensure_gpio_mode"]()
    except Exception:
        pass

    with _HUM_SCALE_LOCK:
        counts = _scale_read_counts(6)
    cal = _load_humidity_scale_cal()

    water_kg = gross_kg = label = None
    if counts is not None and cal is not None:
        try:
            water_kg = (counts - cal["baseline_counts"]) / cal["counts_per_kg"]
            if water_kg < 0:
                water_kg = 0.0
        except Exception:
            water_kg = None

        gs = load_global_settings()
        empty = float(gs.get("humidifier_reservoir_empty_weight_kg", 0.0) or 0.0)
        full = float(gs.get("humidifier_reservoir_full_capacity_kg", 0.0) or 0.0)
        usable = max(0.0, full)
        gross_kg = (empty + water_kg) if (water_kg is not None) else None

        half = float(gs.get("humidifier_reservoir_half_water_kg", 0.0) or 0.0)
        low = float(gs.get("humidifier_reservoir_low_water_kg", 0.0) or 0.0)
        crit = float(gs.get("humidifier_reservoir_critical_water_kg", 0.0) or 0.0)
        fm = float(gs.get("humidifier_reservoir_full_margin_kg", 1.0) or 0.0)

        if water_kg is not None:
            if usable and water_kg >= (usable - fm):
                label = "Full"
            elif water_kg <= crit:
                label = "Critical"
            elif water_kg <= low:
                label = "Low"
            elif water_kg <= half:
                label = "Half"
            else:
                label = "OK"

    return jsonify({
        "ok": counts is not None,
        "counts": counts,
        "water_kg": None if water_kg is None else round(water_kg, 3),
        "gross_kg": None if gross_kg is None else round(gross_kg, 3),
        "label": label,
        "baseline_session": _scale_session["baseline"],
        "calibrated": bool(cal),
        "cal": cal,
    })

@humidity_scale_bp.route("/api/humidity-scale/cal/start", methods=["POST"])
def api_humidity_scale_cal_start():
    with _HUM_SCALE_LOCK:
        baseline = _scale_read_counts(12)
    if baseline is None:
        return jsonify({"ok": False, "error": "No readings. Check wiring/power."}), 400
    _scale_session["baseline"] = float(baseline)
    _scale_session["captured_at"] = time.time()
    return jsonify({"ok": True, "baseline_counts": _scale_session["baseline"]})

@humidity_scale_bp.route("/api/humidity-scale/cal/commit", methods=["POST"])
def api_humidity_scale_cal_commit():
    data = request.get_json(silent=True) or {}
    try:
        known = float(data.get("known_mass_kg", 0))
    except Exception:
        known = 0.0
    if known <= 0:
        return jsonify({"ok": False, "error": "known_mass_kg must be > 0"}), 400
    if _scale_session["baseline"] is None:
        return jsonify({"ok": False, "error": "Capture baseline first."}), 400

    with _HUM_SCALE_LOCK:
        loaded = _scale_read_counts(12)
    if loaded is None:
        return jsonify({"ok": False, "error": "No readings under load."}), 400

    baseline = _scale_session["baseline"]
    delta = loaded - baseline
    if abs(delta) < 1:
        return jsonify({"ok": False, "error": "Delta counts too small; use a heavier known mass."}), 400

    counts_per_kg = delta / known
    cal = {
        "baseline_counts": float(baseline),
        "counts_per_kg": float(counts_per_kg),
    }
    with open(CAL_PATH, "w") as f:
        json.dump(cal, f, indent=2)

    _scale_session["baseline"] = None
    _scale_session["captured_at"] = None

    return jsonify({"ok": True, "saved": cal})
