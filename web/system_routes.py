#web/systems_routes.py
from flask import Blueprint, render_template, jsonify, make_response, current_app
# NEW: capacity helper to compute "water used"
from global_settings import usable_capacity_kg

bp = Blueprint("system", __name__)

def ctx(): return current_app.config["CTX"]

def status_data():             return ctx()["status_data"]
def running_profile():         return ctx()["get_running_profile"]()
def compute_banner(payload):   return ctx()["compute_banner"](payload)

@bp.route('/')
def home():
    return render_template('dashboard.html')

@bp.route('/logs')
def logs_ui():
    return render_template('logs.html')

@bp.route('/status.json')
def status_json():
    sd = status_data()
    def ONOFF(val): return "ON" if str(val).strip().upper() == "ON" else "OFF"
    payload = {
        "profile":        running_profile(),
        "start_time":     sd.get("start_time"),

        "pump_state":     ONOFF(sd.get("pump_state")),
        "agitator_state": ONOFF(sd.get("agitator_state")),
        "air_pump_state": ONOFF(sd.get("air_pump_state")),

        "cycle_count":    sd.get("cycle_count") or 0,
        "fan_state":      ONOFF(sd.get("fan_state")),
        "paused":         bool(sd.get("paused")),

        "heater_state":       ONOFF(sd.get("heater_state")),
        "humidifier_state":   ONOFF(sd.get("humidifier_state")),
        "extractor_state":    ONOFF(sd.get("extractor_state", sd.get("fan_state", "OFF"))),
        "nutrient_a_state":   ONOFF(sd.get("nutrient_A_on")),
        "nutrient_b_state":   ONOFF(sd.get("nutrient_B_on")),
        "concentrate_mix_state": ONOFF(sd.get("concentrate_mix_state")),

        "pump_time_remaining_s":      sd.get("pump_time_remaining_s"),
        "agitator_time_remaining_s":  sd.get("agitator_time_remaining_s"),
        "air_pump_time_remaining_s":  sd.get("air_pump_time_remaining_s"),
        "pump_time_total_s":          sd.get("pump_time_total_s"),
        "agitator_time_total_s":      sd.get("agitator_time_total_s"),
        "air_pump_time_total_s":      sd.get("air_pump_time_total_s"),

        "temperature_c":        sd.get("temperature_c"),
        "temperature_top":      sd.get("temperature_top"),
        "temperature_bottom":   sd.get("temperature_bottom"),
        "temperature_avg":      sd.get("temperature_avg"),
        "temperature_gradient": sd.get("temperature_gradient"),
        "temperature_min":      sd.get("temperature_min"),
        "temperature_target":   sd.get("temperature_target"),
        "temperature_max":      sd.get("temperature_max"),
        "humidity":             sd.get("humidity"),
        "humidity_min":         sd.get("humidity_min"),
        "humidity_target":      sd.get("humidity_target"),
        "humidity_max":         sd.get("humidity_max"),
        "humidity_top":         sd.get("humidity_top"),
        "humidity_bottom":      sd.get("humidity_bottom"),

        "water_temperature":        sd.get("water_temperature"),
        "water_temperature_min":    sd.get("water_temperature_min"),
        "water_temperature_target": sd.get("water_temperature_target"),
        "water_temperature_max":    sd.get("water_temperature_max"),
        "water_quantity_min":       sd.get("water_quantity_min"),
        "last_error":               sd.get("last_error"),
    }

    payload["system_active"] = bool(running_profile())



    payload.update({
        "reservoir_gross_kg":  sd.get("reservoir_gross_kg"),
        "reservoir_water_raw": sd.get("reservoir_water_raw"),
        "reservoir_water_kg":  sd.get("reservoir_water_kg"),
        "reservoir_status":    sd.get("reservoir_status"),
        "reservoir_debug":     sd.get("reservoir_debug"),
    })
    payload["water_quantity"] = (
        payload["reservoir_water_kg"]
        if payload.get("reservoir_water_kg") is not None
        else sd.get("water_quantity")
    )

    # NEW: compute "Water Used" = full capacity - water left
    try:
        gs = ctx()["load_global_settings"]()
        cap = usable_capacity_kg(gs)  # kg at "full"
    except Exception:
        cap = 0.0
    water_left = payload.get("reservoir_water_kg")
    payload["water_used"] = (
        None if water_left is None
        else round(max(0.0, float(cap) - float(water_left)), 2)
    )

    try:
        payload["banner"] = compute_banner(payload)
    except Exception:
        payload["banner"] = {"level": "info", "message": "System nominal", "rotate": []}

    try:
        manual = sd.get("manual_overrides", {}) if isinstance(sd.get("manual_overrides"), dict) else {}
        payload["manual_overrides"] = {
            k: {kk: vv for kk, vv in (v or {}).items() if kk != "since_mono"}
            for k, v in manual.items()
        }
    except Exception:
        payload["manual_overrides"] = {}

    resp = make_response(jsonify(payload))
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    resp.headers['Pragma'] = 'no-cache'
    return resp


@bp.route("/api/settings", methods=["GET"])
def api_settings():
    gs = ctx()["load_global_settings"]()
    resp = make_response(jsonify(gs))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    return resp



# ----- Global Settings -----
@bp.route("/settings/global", methods=["GET", "POST"])
def settings_global():
    load_global_settings = ctx()["load_global_settings"]
    save_global_settings = ctx()["save_global_settings"]
    validate_settings    = ctx()["validate_settings"]

    cfg = load_global_settings()
    from flask import request, redirect, url_for, flash, render_template

    if request.method == "POST":
        fields = [
            "hysteresis_temp_c",
            "hysteresis_temp_heater_c",
            "hysteresis_temp_extractor_c",
            "hysteresis_humidity_pct",
            "hysteresis_humidity_humidifier_pct",
            "hysteresis_humidity_extractor_pct",
            "heater_min_on_s",
            "fan_min_on_s",
            "humidifier_min_on_s",
            "absolute_temp_min_c",
            "absolute_temp_max_c",
            "absolute_humidity_min_pct",
            "absolute_humidity_max_pct",
            "reservoir_empty_weight_kg",
            # capacity-first (new)
            "reservoir_full_capacity_kg",
            "reservoir_half_water_kg",
            "reservoir_low_water_kg",
            "reservoir_critical_water_kg",
            "reservoir_pump_cutoff_water_kg",
            "reservoir_full_margin_kg",
            "agitator_enabled", "agitator_run_sec",
            "air_pump_enabled","air_pump_run_sec",
            "water_temp_min_c","water_temp_target_c","water_temp_max_c",
        ]
        raw = {k: (request.form.get(k, "").strip()) for k in fields}
        ok, errors, cleaned = validate_settings(raw)
        if ok:
            save_global_settings(cleaned)
            # refresh in-memory snapshot
            new_gs = load_global_settings()
            ctx()["set_global_settings"](new_gs)

            try: flash("Global settings saved.", "success")
            except Exception: pass
            return redirect(url_for("system.settings_global"))
        else:
            try:
                for e in errors: flash(e, "error")
            except Exception: pass
            cfg = {**cfg, **raw}

    return render_template("settings_global.html", s=cfg)


