import os, json, datetime, math
from flask import Blueprint, redirect, url_for, flash, current_app, request



bp = Blueprint("control", __name__)

def ctx(): return current_app.config["CTX"]

def status_data():             return ctx()["status_data"]
def running_profile():         return ctx()["get_running_profile"]()
def set_running_profile(v):    return ctx()["set_running_profile"](v)

def save_state(d):             return ctx()["save_state"](d)
def load_state():              return ctx()["load_state"]()
def clear_state():             return ctx()["clear_state"]()

def start_sim_thread(name, pdata): return ctx()["start_sim_thread"](name, pdata)

def LOGGER():                  return ctx()["LOGGER"]
def apply_outputs_from_status():return ctx()["apply_outputs_from_status"]()
def _ensure_gpio_mode():       return ctx()["_ensure_gpio_mode"]()

def PROFILE_DIR():             return ctx()["PROFILE_DIR"]

@bp.route('/run/<profile_name>', methods=['POST'])
def run_profile(profile_name):
    pth = os.path.join(PROFILE_DIR(), profile_name)
    if not os.path.exists(pth):
        flash(f"Profile '{profile_name}' not found.")
        return redirect(url_for('profiles.list_profiles'))

    with open(pth) as f:
        pdata = json.load(f)

    sd = status_data()
    sd.update(
        paused=False,
        last_error=None,
        pump_state="OFF",
        pump_phase_end_ts=None,
        pump_time_remaining_s=None,
        pump_resume_phase=None,
        pump_resume_remaining_s=None,
        agitator_state="OFF",
        agitator_phase_end_ts=None,
        agitator_time_remaining_s=None,
        startup_kick=True,
    )
    sd["start_time"] = datetime.datetime.utcnow().isoformat()

    set_running_profile(profile_name)
    sd["profile"] = profile_name

    save_state({
        "running_profile": profile_name,
        "start_time":      sd["start_time"],
        "cycle_count":     0,
        "pump_state":      sd.get("pump_state", "OFF"),
        "fan_state":       sd.get("fan_state", "OFF"),
        "paused":          False
    })

    start_sim_thread(profile_name, pdata)

    try:
        LOGGER().log_event(
            "profile_lifecycle", msg="Profile started", reason_code="start",
            profile_id=profile_name, actor="ui",
            payload={"parameters": (pdata.get("parameters", pdata))}
        )
        # snapshot globals row
        try:
            # use the same helper you had in app.py via CTX if you expose it,
            # but we can inline: log as lifecycle/globals via LOGGER() if needed later
            pass
        except Exception:
            pass
    except Exception:
        pass

    flash(f"Running profile: {profile_name}")
    return redirect(url_for('profiles.list_profiles'))

@bp.route('/stop', methods=['POST'])
def stop_running():
    # ❶ Work out why we’re stopping (default: manual UI stop)
    reason = (request.args.get('reason') or request.form.get('reason') or 'manual').strip().lower()
    is_reservoir_stop = reason in ('reservoir_renewal', 'reservoir')

    rp = running_profile()

    if rp:
        flash(f"Stopped profile: {rp}")

        # ❷ Mark renewal context (purely informational; no actuator changes)
        sd = status_data()
        if is_reservoir_stop:
            # Lets the reservoir wizard know we stopped *for renewal*
            sd["renewal_in_progress"] = True
            sd["renewal_stopped_profile"] = rp
            sd["renewal_stopped_at_iso"] = datetime.datetime.utcnow().isoformat()

        # ❸ Structured log with reason detail
        try:
            LOGGER().log_event(
                "profile_lifecycle",
                msg="Profile stopped",
                reason_code=f"stop:{'reservoir_renewal' if is_reservoir_stop else 'manual'}",
                profile_id=rp,
                actor="ui",
                payload={
                    "origin": "reservoir_page" if is_reservoir_stop else "profiles_page",
                    "source_route": "control.stop",
                }
            )
        except Exception:
            pass

        # ❹ Clear running flag and reset states (unchanged device logic)
        set_running_profile(None)

        sd["start_time"] = None
        sd["paused"] = False

        sd.update(
            # Clear profile thresholds (belong to a profile)
            temperature_min=None, temperature_target=None, temperature_max=None,
            humidity_min=None,    humidity_target=None,    humidity_max=None,
            water_temperature_min=None, water_temperature_target=None, water_temperature_max=None,
            water_quantity_min=None
            # Do NOT clear live readings; ambient sampler keeps them fresh.
        )

        # Immediate safe-off (unchanged)
        try: sd["fan_state"] = "OFF"
        except Exception: pass
        try:
            sd["pump_state"] = "OFF"
            sd["pump_phase_end_ts"] = None
            sd["pump_time_remaining_s"] = None
        except Exception: pass
        try:
            sd["agitator_state"] = "OFF"
            sd["agitator_phase_end_ts"] = None
            sd["agitator_time_remaining_s"] = None
        except Exception: pass
        try:
            sd["air_pump_state"] = "OFF"
            sd["air_pump_phase_end_ts"] = None
            sd["air_pump_time_remaining_s"] = None
        except Exception: pass
        try: sd["heater_state"] = "OFF"
        except Exception: pass
        try: sd["humidifier_state"] = "OFF"
        except Exception: pass

        sd["pump_time_total_s"] = None
        sd["agitator_time_total_s"] = None
        sd["air_pump_time_total_s"] = None

        sd["pump_resume_phase"] = None
        sd["agitator_resume_phase"] = None
        sd["air_pump_resume_phase"] = None

        sd["pump_resume_remaining_s"] = None
        sd["agitator_resume_remaining_s"] = None
        sd["air_pump_resume_remaining_s"] = None

        sd["pump_phase_end_ts"] = None
        sd["agitator_phase_end_ts"] = None
        sd["air_pump_phase_end_ts"] = None

        sd["pump_time_remaining_s"] = None
        sd["agitator_time_remaining_s"] = None
        sd["air_pump_time_remaining_s"] = None

        try:
            apply_outputs_from_status()
        except Exception:
            pass

        clear_state()
    else:
        flash("No profile running.")

    return redirect(url_for('profiles.list_profiles'))




@bp.route('/pause', methods=['POST'])
def pause_profile():
    if not running_profile():
        flash("No profile running.")
        return redirect(url_for('profiles.list_profiles'))

    sd = status_data()
    sd["paused"] = True
    now_m = math.ceil  # we only need ceil in clamp; monotonic diffs computed elsewhere

    def clamp_rem(end_ts, mono_now):
        if not isinstance(end_ts, (int, float)): return 0
        return max(0, math.ceil(end_ts - mono_now))

    # We don't have _mono here; rely on UI countdown cleared (same as your code):
    mono_now = 0
    sd["pump_resume_remaining_s"]     = clamp_rem(sd.get("pump_phase_end_ts"), mono_now)
    sd["agitator_resume_remaining_s"] = clamp_rem(sd.get("agitator_phase_end_ts"), mono_now)
    sd["air_pump_resume_remaining_s"] = clamp_rem(sd.get("air_pump_phase_end_ts"), mono_now)

    sd["pump_resume_phase"]     = "ON" if (sd["pump_resume_remaining_s"] > 0) else "OFF"
    sd["agitator_resume_phase"] = "ON" if (sd["agitator_resume_remaining_s"] > 0) else "OFF"
    sd["air_pump_resume_phase"] = "ON" if (sd["air_pump_resume_remaining_s"] > 0) else "OFF"

    sd["pump_state"]      = "OFF"
    sd["agitator_state"]  = "OFF"
    sd["air_pump_state"]  = "OFF"

    sd["pump_phase_end_ts"]      = None
    sd["agitator_phase_end_ts"]  = None
    sd["air_pump_phase_end_ts"]  = None

    sd["pump_time_remaining_s"]      = None
    sd["agitator_time_remaining_s"]  = None
    sd["air_pump_time_remaining_s"]  = None

    try: apply_outputs_from_status()
    except Exception: pass

    try:
        LOGGER().log_event("profile_lifecycle", msg="Profile paused",
                           reason_code="pause", profile_id=running_profile(),
                           actor="ui", payload="profile paused")
    except Exception:
        pass

    flash(f"Paused profile: {running_profile()}")
    return redirect(url_for('profiles.list_profiles'))

@bp.route('/unpause', methods=['POST'])
def unpause_profile():
    if running_profile() and status_data().get("paused"):
        status_data()["paused"] = False
        try: _ensure_gpio_mode()
        except Exception: pass
        try: apply_outputs_from_status()
        except Exception: pass
        try:
            LOGGER().log_event("profile_lifecycle", msg="Profile resumed",
                               reason_code="resume", profile_id=running_profile(),
                               actor="ui", payload="profile resumed")
        except Exception:
            pass
        flash(f"Resumed running profile: {running_profile()}")
    return redirect(url_for('profiles.list_profiles'))

@bp.route('/resume', methods=['POST'])
def resume_profile():
    state = load_state()
    if running_profile():
        flash(f"Already running: {running_profile()}")
        return redirect(url_for('profiles.list_profiles'))
    if not state or not state.get("running_profile"):
        flash("Nothing to resume.")
        return redirect(url_for('profiles.list_profiles'))

    pname = state["running_profile"]
    pth = os.path.join(PROFILE_DIR(), pname)
    if not os.path.exists(pth):
        flash("Profile file not found; cleared saved resume info.")
        clear_state()
        return redirect(url_for('profiles.list_profiles'))

    try:
        with open(pth) as f:
            pdata = json.load(f)
    except Exception:
        flash("Failed to load profile file.")
        clear_state()
        return redirect(url_for('profiles.list_profiles'))

    set_running_profile(pname)
    sd = status_data()
    sd.update(
        profile=pname,
        start_time=state.get("start_time"),
        pump_state=state.get("pump_state", "OFF"),
        fan_state=state.get("fan_state", "OFF"),
        cycle_count=state.get("cycle_count", 0),
        paused=state.get("paused", False),
        heater_state=state.get("heater_state", "OFF"),
        humidifier_state=state.get("humidifier_state", "OFF"),
        agitator_state=state.get("agitator_state", "OFF"),
    )

    try: apply_outputs_from_status()
    except Exception: pass

    start_sim_thread(pname, pdata)

    try:
        LOGGER().log_event("profile_lifecycle", msg="Profile resumed",
                           reason_code="resume", profile_id=pname,
                           actor="ui", payload="profile resumed")
    except Exception:
        pass

    clear_state()
    flash(f"Resumed profile: {pname}")
    return redirect(url_for('profiles.list_profiles'))

@bp.route('/dismiss-resume', methods=['POST'])
def dismiss_resume():
    clear_state()
    try: flash("Resume suggestion dismissed.")
    except Exception: pass
    return redirect(url_for('profiles.list_profiles'))



