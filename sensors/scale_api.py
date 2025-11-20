# sensors/scale_api.py
import json, time
from flask import Blueprint, jsonify, render_template, request, current_app

# Use the one true HX711 stack + cal file from sensors/scale.py
from sensors.scale import (
    _SCALE_LOCK,
    _HUMID_SCALE_LOCK,
    _scale_read_counts,
    _scale_read_counts_humid,
    _load_scale_cal,
    _load_humid_scale_cal,
    CAL_PATH,
    HUMID_CAL_PATH,
)
from global_settings import load_global_settings

scale_bp = Blueprint("scale", __name__)

# Session memory for captured baselines (keyed by scale id)
_scale_session = {
    "main": {"baseline": None, "captured_at": None},
    "humid_res": {"baseline": None, "captured_at": None},
}


def _scale_defs():
    return {
        "main": {
            "lock": _SCALE_LOCK,
            "reader": _scale_read_counts,
            "cal_loader": _load_scale_cal,
            "cal_path": CAL_PATH,
            "empty_key": "reservoir_empty_weight_kg",
            "capacity_key": "reservoir_full_capacity_kg",
            "half_key": "reservoir_half_water_kg",
            "low_key": "reservoir_low_water_kg",
            "crit_key": "reservoir_critical_water_kg",
            "margin_key": "reservoir_full_margin_kg",
        },
        "humid_res": {
            "lock": _HUMID_SCALE_LOCK,
            "reader": _scale_read_counts_humid,
            "cal_loader": _load_humid_scale_cal,
            "cal_path": HUMID_CAL_PATH,
            "empty_key": "humid_res_empty_weight_kg",
            "capacity_key": "humid_res_full_capacity_kg",
            "half_key": "humid_res_half_water_kg",
            "low_key": "humid_res_low_water_kg",
            "crit_key": "humid_res_critical_water_kg",
            "margin_key": "humid_res_full_margin_kg",
        },
    }


def _session(scale_id: str):
    return _scale_session.setdefault(scale_id, {"baseline": None, "captured_at": None})


def _compute_from_counts(scale_id: str, counts: float | None, cal: dict | None):
    water_kg = gross_kg = label = None
    if counts is not None and cal is not None:
        try:
            water_kg = (counts - cal["baseline_counts"]) / cal["counts_per_kg"]
            if water_kg < 0:
                water_kg = 0.0
        except Exception:
            water_kg = None

        gs = load_global_settings()
        defs = _scale_defs()[scale_id]
        empty = float(gs.get(defs["empty_key"], 0.0) or 0.0)
        usable = float(gs.get(defs["capacity_key"], 0.0) or 0.0)
        gross_kg = (empty + water_kg) if (water_kg is not None) else None

        half = float(gs.get(defs["half_key"], 0.0) or 0.0)
        low  = float(gs.get(defs["low_key"], 0.0) or 0.0)
        crit = float(gs.get(defs["crit_key"], 0.0) or 0.0)
        fm   = float(gs.get(defs["margin_key"], 1.0) or 0.0)

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
    return water_kg, gross_kg, label


def _api_scale_raw(scale_id: str):
    defs = _scale_defs()[scale_id]
    # Make sure RPi.GPIO mode is set before touching HX711
    try:
        current_app.config["CTX"]["_ensure_gpio_mode"]()
    except Exception:
        pass

    with defs["lock"]:
        counts = defs["reader"](6)
    cal = defs["cal_loader"]()

    water_kg, gross_kg, label = _compute_from_counts(scale_id, counts, cal)
    sess = _session(scale_id)
    return jsonify({
        "ok": counts is not None,
        "counts": counts,
        "water_kg": None if water_kg is None else round(water_kg, 3),
        "gross_kg": None if gross_kg is None else round(gross_kg, 3),
        "label": label,
        "baseline_session": sess["baseline"],
        "calibrated": bool(cal),
        "cal": cal,
    })


@scale_bp.route("/settings/scale", methods=["GET"])
def settings_scale():
    return render_template("calibrate_scale.html", cal=_load_scale_cal())


@scale_bp.route("/settings/humidifier-scale", methods=["GET"])
def settings_humid_scale():
    return render_template("calibrate_scale_humid.html", cal=_load_humid_scale_cal())


@scale_bp.route("/api/scale/raw", methods=["GET"])
def api_scale_raw():
    return _api_scale_raw("main")


@scale_bp.route("/api/scale/humid_res/raw", methods=["GET"])
def api_scale_humid_raw():
    return _api_scale_raw("humid_res")


def _api_scale_cal_start(scale_id: str):
    defs = _scale_defs()[scale_id]
    with defs["lock"]:
        baseline = defs["reader"](12)
    if baseline is None:
        return jsonify({"ok": False, "error": "No readings. Check wiring/power."}), 400
    sess = _session(scale_id)
    sess["baseline"] = float(baseline)
    sess["captured_at"] = time.time()
    return jsonify({"ok": True, "baseline_counts": sess["baseline"]})


@scale_bp.route("/api/scale/cal/start", methods=["POST"])
def api_scale_cal_start():
    return _api_scale_cal_start("main")


@scale_bp.route("/api/scale/humid_res/cal/start", methods=["POST"])
def api_scale_cal_start_humid():
    return _api_scale_cal_start("humid_res")


def _api_scale_cal_commit(scale_id: str):
    data = request.get_json(silent=True) or {}
    try:
        known = float(data.get("known_mass_kg", 0))
    except Exception:
        known = 0.0
    if known <= 0:
        return jsonify({"ok": False, "error": "known_mass_kg must be > 0"}), 400
    sess = _session(scale_id)
    if sess["baseline"] is None:
        return jsonify({"ok": False, "error": "Capture baseline first."}), 400

    defs = _scale_defs()[scale_id]
    with defs["lock"]:
        loaded = defs["reader"](12)
    if loaded is None:
        return jsonify({"ok": False, "error": "No readings under load."}), 400

    baseline = sess["baseline"]
    delta = loaded - baseline
    if abs(delta) < 1:
        return jsonify({"ok": False, "error": "Delta counts too small; use a heavier known mass."}), 400

    counts_per_kg = delta / known
    cal = {
        # pins are optional; sensors/scale.py only needs these two:
        "baseline_counts": float(baseline),
        "counts_per_kg": float(counts_per_kg),
    }
    with open(defs["cal_path"], "w") as f:
        json.dump(cal, f, indent=2)

    sess["baseline"] = None
    sess["captured_at"] = None

    return jsonify({"ok": True, "saved": cal})


@scale_bp.route("/api/scale/cal/commit", methods=["POST"])
def api_scale_cal_commit():
    return _api_scale_cal_commit("main")


@scale_bp.route("/api/scale/humid_res/cal/commit", methods=["POST"])
def api_scale_cal_commit_humid():
    return _api_scale_cal_commit("humid_res")



