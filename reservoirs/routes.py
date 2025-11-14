# reservoirs/routes.py
# -*- coding: utf-8 -*-
"""
Reservoirs blueprint routes:
- /reservoirs (management page)
- /reservoirs/wizard (renewal wizard UI)
- /api/reservoirs/live  (live %/litres/temp/profile/last fill)
- /api/reservoirs/dose  (run nutrient dosing based on profile + calibrated ml/s)
- /api/reservoirs/mix   (run agitator for X seconds from global settings)
- /api/reservoirs/complete (log completion + stamp last fill)
- /api/nutrient/prime   (toggle peristaltic pump A/B instantly ON/OFF)
- /api/nutrient/run     (run a pump for precise seconds)
- /api/nutrient/calc    (save calibration: seconds + measured ml -> ml/s)
"""

from __future__ import annotations

from flask import current_app, render_template, request, jsonify, session, redirect, url_for
from reservoirs import reservoirs_bp
# NEW: capacity/gross helpers from global settings
from global_settings import usable_capacity_kg, full_gross_weight_kg, water_kg_from_gross
import time  # <-- ADDED (used by /api/reservoirs/dose)
import threading




#
# ────────────────────────────── CTX accessor ───────────────────────────────
#

def _CTX() -> dict:
    """
    Live context provided by app.py:
      - LOGGER (EventLogger)
      - status_data (dict)
      - load_global_settings(), save_global_settings(), validate_settings()
      - SCALE_SAMPLER (threaded HX711 sampler)  [optional]
      - other helpers if needed
    """
    return current_app.config["CTX"]





#
# ───────────────────────────── Internal helpers 1 ────────────────────────────
#

# --- helper: best-effort pause so BEGIN can pause + log in one call ---
def _pause_for_reservoir_renewal() -> bool:
    """
    Try to pause the active profile using an internal helper if available;
    fall back to setting status_data['paused']=True so the loop respects it.
    Returns True if we believe the system is now paused.
    """
    ctx = _CTX()
    sd = ctx["status_data"]

    # Try a dedicated pause helper if your app exposes one
    try:
        # Adjust import path if your project keeps this elsewhere
        from control_routes import pause_current_profile  # type: ignore
        return bool(pause_current_profile(reason="reservoir_renewal"))
    except Exception:
        pass

    # Soft fallback: flip the paused flag in shared status (your loop already reads this)
    try:
        sd["paused"] = True
        return True
    except Exception:
        return False


def _active_profile_id() -> str | None:
    """
    Return the currently active profile id/filename for logging.
    Preference order:
      - status_data['profile_id'] if present
      - status_data['profile'] (some builds store the filename here)
      - session['wizard_selected_profile'] (if a profile was picked in the wizard)
    """
    ctx = _CTX()
    sd = ctx["status_data"]
    pid = (sd.get("profile_id") or sd.get("profile") or session.get("wizard_selected_profile"))
    return pid or None




#
# ────────────────────────────── UI pages ───────────────────────────────────
#

@reservoirs_bp.route("/reservoirs", methods=["GET"])
def reservoirs_page():
    """Management page with two blocks: Main Reservoir + Humidifier Reservoir."""
    return render_template("reservoirs.html")


# ── NEW: move this helper to module scope so it's available everywhere ─────

def _load_nutrients_for_selected(ctx_local: dict, sel_fn: str):
    """
    Returns (nutrients_dict_or_None, selected_profile_pretty_name_or_None)
    Accepts both {nutrients:{...}} and {pump:{nutrients:{...}}} file formats.
    """
    if not sel_fn:
        return None, None
    import os, json
    prof_path = os.path.join(ctx_local["PROFILE_DIR"], sel_fn)
    try:
        with open(prof_path, "r") as f:
            pdata = json.load(f)
        nutrients = (pdata.get("nutrients") or pdata.get("pump", {}).get("nutrients") or None)
        sel_name = (pdata.get("name") or os.path.splitext(sel_fn)[0])
        return nutrients, sel_name
    except Exception:
        return None, None


@reservoirs_bp.route("/reservoirs/wizard", methods=["GET", "POST"])
def reservoir_wizard():
    step = int(request.args.get("step", "1") or 1)
    if step < 1 or step > 4:
        return redirect(url_for("reservoirs.reservoir_wizard", step=1))
        


    # POST actions
    if request.method == "POST":
        action = request.form.get("action") or ""

        if step == 1 and action == "confirm_empty":
            session["wizard_empty_ok"] = True
            return redirect(url_for("reservoirs.reservoir_wizard", step=2))

        if step == 3:
            # capture a newly chosen profile (filename)
            chosen = (request.form.get("selected_profile") or request.form.get("profile") or "").strip()
            if chosen:
                session["wizard_selected_profile"] = chosen

            if action == "choose_profile":
                # re-render step 3 with nutrients hydrated
                ctx_local = _CTX()
                main = _compute_main_res_status()

                # ↓ load nutrients + pretty name for the chosen profile
                nutrients, selected_profile_name = _load_nutrients_for_selected(
                    ctx_local, session.get("wizard_selected_profile")
                )

                return render_template(
                    "reservoir_wizard/step3.html",
                    step=3,
                    gs=ctx_local["load_global_settings"](),
                    empty_ok=bool(session.get("wizard_empty_ok")),
                    profiles=_list_profiles(ctx_local),
                    profiles_meta=_profiles_meta_from_disk(ctx_local),
                    selected_profile=session.get("wizard_selected_profile"),
                    selected_profile_name=selected_profile_name,
                    nutrients=nutrients,
                    main=main
                )

            if action == "next":
                # dosing now happens on Step 3, so continue to the premix summary
                return redirect(url_for("reservoirs.reservoir_wizard", step=4))

        if action == "back":
            return redirect(url_for("reservoirs.reservoir_wizard", step=max(1, step - 1)))
        if action == "next":
            return redirect(url_for("reservoirs.reservoir_wizard", step=min(4, step + 1)))

    # Build context
    ctx = _CTX()
    gs = ctx["load_global_settings"]()
    selected_profile = session.get("wizard_selected_profile")

    nutrients = None
    selected_profile_name = None

    # ↓ Load nutrients for Step 3 (since step 4 UI is merged here)
    if step == 3 and selected_profile:
        nutrients, selected_profile_name = _load_nutrients_for_selected(ctx, selected_profile)

    # Also kept for compatibility if you still visit step 4 (optional)
    if step == 4 and selected_profile and nutrients is None:
        nutrients, selected_profile_name = _load_nutrients_for_selected(ctx, selected_profile)

    tpl_name = f"reservoir_wizard/step{step}.html"
    main = _compute_main_res_status()

    if step == 3:
        return render_template(
            tpl_name,
            step=step,
            gs=gs,
            empty_ok=bool(session.get("wizard_empty_ok")),
            profiles=_list_profiles(ctx),
            profiles_meta=_profiles_meta_from_disk(ctx),
            selected_profile=selected_profile,
            selected_profile_name=selected_profile_name,
            nutrients=nutrients,
            main=main,
        )

    profiles_files = _list_profiles(ctx)
    return render_template(
        tpl_name,
        step=step,
        gs=gs,
        empty_ok=bool(session.get("wizard_empty_ok")),
        profiles=profiles_files,
        selected_profile=selected_profile,
        selected_profile_name=selected_profile_name,  # <-- passed generally
        nutrients=nutrients,
        main=main,
    )



@reservoirs_bp.route("/reservoirs/calibration", methods=["GET"])
def reservoirs_calibration_page():
    """Simple page for calibrating nutrient pumps (A/B)."""
    return render_template("nutrient_pump_calibration.html")




#
# ───────────────────────────── Internal helpers ────────────────────────────
#

def _safe_import_water_temp():
    """Optional sensor import that never hard-fails."""
    try:
        from sensors.water import read_water_temp_c  # type: ignore
        return read_water_temp_c
    except Exception:
        return None


def _read_water_kg_from_scale(ctx: dict, gs: dict):
    """
    Best-effort: read HX711 counts, apply calibration, convert to gross_kg,
    then subtract the configured empty gross weight to yield net water_kg.
    """
    try:
        from sensors.scale import _SCALE_LOCK, _scale_read_counts, _load_scale_cal  # type: ignore
    except Exception:
        return None

    try:
        cal = _load_scale_cal() or {}
        baseline = float(cal.get("baseline_counts") or 0.0)
        counts_per_kg = float(cal.get("counts_per_kg") or 0.0)
        if counts_per_kg <= 0:
            return None

        with _SCALE_LOCK:
            counts = float(_scale_read_counts(6))  # a few samples for stability

        gross_kg = (counts - baseline) / counts_per_kg
        empty_gross = float(gs.get("reservoir_empty_weight_kg", 0.0) or 0.0)
        water_kg = max(0.0, gross_kg - empty_gross)
        return water_kg
    except Exception:
        return None


def _compute_main_res_status():
    """
    Returns a dict with:
      percent (0..100), litres_to_go, fine (0..1 for last 1 L fine gauge),
      last_fill (ISO str or None), profile_running (str or None), water_temp_c (float or None)
      PLUS: water_kg, is_critical (bool), critical_threshold_kg (float|None)

    Profile-agnostic and robust after STOP:
      1) prefer status_data['reservoir_water_kg'] (if the control loop populated it)
      2) then prefer SCALE_SAMPLER.value() (gross kg -> convert to water kg)
      3) finally, fall back to a direct HX711 read
    """
    ctx = _CTX()
    gs  = ctx["load_global_settings"]()
    sd  = ctx["status_data"]

    # --- WATER KG ---
    # 1) what the loop/ambient already published
    water_kg = sd.get("reservoir_water_kg")

    # 2) use background sampler right away (works even just-after Stop)
    if water_kg is None:
        sampler = ctx.get("SCALE_SAMPLER")
        if sampler:
            try:
                gross_kg = sampler.value()  # gross = empty + water
            except Exception:
                gross_kg = None
            if gross_kg is not None:
                try:
                    # Convert gross -> water using your globals helper
                    water_kg = water_kg_from_gross(gs, float(gross_kg))
                except Exception:
                    water_kg = None

    # 3) last resort: direct HX711
    if water_kg is None:
        water_kg = _read_water_kg_from_scale(ctx, gs)

    # --- WATER TEMP ---
    water_temp_c = sd.get("water_temperature")
    if water_temp_c is None:
        _rt = _safe_import_water_temp()
        if _rt:
            try:
                water_temp_c = _rt()
            except Exception:
                water_temp_c = None

    profile_running = sd.get("profile")

    # Usable capacity (net water at "full") from globals
    usable_kg = usable_capacity_kg(gs)

    # Target fill in litres (wizard uses this to calculate how much to add)
    target_litres = float(gs.get("reservoir_target_liters", 0.0) or 0.0)
    target_kg = target_litres  # ~1 L per 1 kg

    # Critical threshold from settings (may be None/blank)
    _crit_raw = gs.get("reservoir_critical_water_kg")
    critical_threshold_kg = None
    try:
        if _crit_raw not in (None, ""):
            critical_threshold_kg = float(_crit_raw)
    except Exception:
        critical_threshold_kg = None

    # Compute percent, litres_to_go, fine, and is_critical
    if water_kg is None:
        percent = None
        litres_to_go = None
        fine = None
        is_critical = False
    else:
        denom = target_kg if target_kg > 0 else usable_kg
        if denom > 0:
            pct = max(0.0, min(100.0, (float(water_kg) / denom) * 100.0))
        else:
            pct = 0.0
        percent = round(pct, 1)

        if target_kg > 0:
            rem = max(0.0, target_kg - float(water_kg))
            litres_to_go = round(rem, 2)
            # fine 0..1 for last 1 L either side of target
            fine_delta = target_kg - float(water_kg)  # +ve under, -ve over
            if abs(fine_delta) >= 1.0:
                fine = 0.0 if fine_delta > 0 else 1.0
            else:
                fine = max(0.0, min(1.0, 0.5 - (fine_delta / 2.0)))
        else:
            litres_to_go = None
            fine = None

        # decide critical on the server
        is_critical = (critical_threshold_kg is not None) and (float(water_kg) <= critical_threshold_kg)

    return {
        "percent": percent,
        "litres_to_go": litres_to_go,
        "fine": fine,
        "last_fill": sd.get("reservoir_last_fill_iso"),
        "profile_running": profile_running,  # remains None when no profile is active
        "water_temp_c": water_temp_c,
        # helpful extras for consumers (as you already had)
        "water_kg": (None if water_kg is None else round(float(water_kg), 3)),
        "target_litres": (round(target_litres, 2) if target_litres > 0 else None),
        # NEW fields:
        "is_critical": bool(is_critical),
        "critical_threshold_kg": critical_threshold_kg,
    }





def _list_profiles(ctx: dict) -> list[str]:
    """Return profile filenames in PROFILE_DIR (no disk writes, no parsing)."""
    import os
    prof_dir = ctx.get("PROFILE_DIR")
    if not prof_dir or not os.path.isdir(prof_dir):
        return []
    out = []
    for name in os.listdir(prof_dir):
        if name.lower().endswith(".json"):
            out.append(name)
    return sorted(out, key=str.lower)


def _profiles_meta_from_disk(ctx: dict) -> list[dict]:
    """
    Reads each *.json profile and returns:
      [{"filename": str, "name": str, "nutrients": {"A":{"ml":float|None,"per_litres":float|None},
                                                    "B":{"ml":float|None,"per_litres":float|None}}}, ...]
    """
    import os, json
    prof_dir = ctx.get("PROFILE_DIR")
    out = []
    if not prof_dir or not os.path.isdir(prof_dir):
        return out

    for fn in sorted([f for f in os.listdir(prof_dir) if f.lower().endswith(".json")], key=str.lower):
        path = os.path.join(prof_dir, fn)
        try:
            with open(path, "r") as f:
                data = json.load(f)
        except Exception:
            data = {}

        name = (data.get("name") or os.path.splitext(fn)[0])

        nu = (data.get("nutrients") or data.get("pump", {}).get("nutrients") or {})
        A = nu.get("A") or {}
        B = nu.get("B") or {}

        def _norm(x):
            # accept either {"ml":X, "per_litres":Y} or any of: per_litre/per_liter/per_liters
            ml  = x.get("ml")
            pr  = (x.get("per_litres") or x.get("per_litre") or x.get("per_liter") or x.get("per_liters"))
            try: ml = float(ml) if ml is not None else None
            except: ml = None
            try: pr = float(pr) if pr is not None else None
            except: pr = None
            return {"ml": ml, "per_litres": pr}

        out.append({
            "filename": fn,
            "name": name,
            "nutrients": {"A": _norm(A), "B": _norm(B)},
        })
    return out



#
# ───────────────────────────── Live data API ───────────────────────────────
#

@reservoirs_bp.route("/api/reservoirs/live", methods=["GET"])
def api_reservoirs_live():
    """
    Returns live info for both cards.
    For now, humidifier reservoir is a placeholder (no scale), but the field is present for UI symmetry.
    """
    ctx = _CTX()
    sd = ctx["status_data"]

    main = _compute_main_res_status()
    


    # --- NEW: surface pump/dosing flags so the UI can show "Running pump A/B"
    a_on   = bool(sd.get("nutrient_A_on"))
    b_on   = bool(sd.get("nutrient_B_on"))
    phase  = sd.get("dosing_phase")          # expected values: "A", "B", or None
    drun   = bool(sd.get("dosing_running"))  # generic “dosing in progress”

    main.update({
        "nutrient_A_on": a_on,
        "nutrient_B_on": b_on,
        "dosing_phase": phase,
        "dosing_running": (a_on or b_on or drun),
        "dosing_cancelled": bool(sd.get("dosing_cancelled")),
    })
    
    # Also expose planned durations and the exact start of the current phase
    plan = sd.get("dosing_plan") or {}
    main.update({
        "dosing_plan": {
            "A_seconds": float(plan.get("A_seconds") or 0.0),
            "B_seconds": float(plan.get("B_seconds") or 0.0),
        },
        "dosing_phase_started_at": sd.get("dosing_phase_started_at"),  # epoch seconds
    })


    # If you later add a second scale for the humidifier reservoir, fill this similarly.
    humidifier = {
        "percent": None,
        "litres_to_go": None,
        "fine": None,
        "last_fill": None,
        "profile_running": None,
        "water_temp_c": None,
    }

    resp = jsonify({"main": main, "humidifier": humidifier})
    # prevent any caching so the UI always sees the latest running/cancel flags
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    return resp





# === Reservoir renewal BEGIN/END APIs (used by the "Stop for renewal" button and the wizard) ===

@reservoirs_bp.route("/api/reservoirs/renewal/begin", methods=["POST"])
def api_reservoirs_renewal_begin():
    """
    Called by the Reservoir page "Stop for renewal" button.
    - Pauses the profile (best-effort)
    - Sets a flag so your wizard/loop can know renewal is active
    - Logs an explicit 'Reservoir renewal: BEGIN' line
    Response: {"ok": true, "paused": true|false}
    """
    import time as _t
    ctx = _CTX()
    sd = ctx["status_data"]
    logger = ctx["LOGGER"]

    paused_ok = _pause_for_reservoir_renewal()

    # Mark renewal active + timestamp (handy for UI or later)
    try:
        sd["reservoir_renewal_active"] = True
        sd["reservoir_renewal_began_at"] = _t.time()
    except Exception:
        pass

    # Clear any stale wizard guards if you use them later
    try:
        sd.pop("reservoir_renewal_completed_at", None)
    except Exception:
        pass

    # Clear, explicit log line for your timeline
    try:
        pid = _active_profile_id()
        ctx["LOGGER"].log_event(
            "reservoir_renewal",
            "Reservoir renewal: BEGIN",
            reason_code="begin",
            profile_id=pid,
        )

    except Exception:
        pass

    return jsonify({"ok": True, "paused": bool(paused_ok)})


@reservoirs_bp.route("/api/reservoirs/renewal/end", methods=["POST"])
def api_reservoirs_renewal_end():
    """
    Called at the end of the wizard, before/after /api/reservoirs/complete.
    - Clears the renewal flag
    - Logs 'Reservoir renewal: END'
    (Does NOT auto-resume the profile; the wizard can separately unpause if desired.)
    """
    import time as _t
    ctx = _CTX()
    sd = ctx["status_data"]
    logger = ctx["LOGGER"]

    try:
        sd["reservoir_renewal_active"] = False
        sd["reservoir_renewal_completed_at"] = _t.time()
    except Exception:
        pass

    try:
        logger.log_event("reservoir_renewal", "Reservoir renewal: END")
    except Exception:
        pass

    return jsonify({"ok": True})


@reservoirs_bp.route("/api/reservoirs/unpause", methods=["POST"])
def api_reservoirs_unpause():
    """Resume the paused profile and request a premix cycle via startup_kick."""

    ctx = _CTX()
    sd = ctx["status_data"]
    get_running = ctx.get("get_running_profile")
    running_profile = get_running() if callable(get_running) else None

    if not running_profile:
        return jsonify({"ok": False, "error": "no_active_profile"}), 409

    was_paused = bool(sd.get("paused", False))
    sd["paused"] = False

    try:
        sd["startup_kick"] = True
    except Exception:
        pass

    gs = ctx["load_global_settings"]()
    agitator_enabled = bool(gs.get("agitator_enabled"))
    agitator_seconds = int(gs.get("agitator_run_sec") or 0) if agitator_enabled else 0
    air_enabled = bool(gs.get("air_pump_enabled"))
    air_seconds = int(gs.get("air_pump_run_sec") or 0) if air_enabled else 0

    try:
        ctx["_ensure_gpio_mode"]()
    except Exception:
        pass

    try:
        ctx["apply_outputs_from_status"]()
    except Exception:
        pass

    try:
        ctx["LOGGER"].log_event(
            "reservoir_renewal",
            "Reservoir renewal: resumed and premix triggered",
            reason_code="resume_mix",
            profile_id=running_profile,
            payload={
                "was_paused": was_paused,
                "agitator_seconds": agitator_seconds,
                "air_pump_seconds": air_seconds,
            },
        )
    except Exception:
        pass

    return jsonify(
        {
            "ok": True,
            "profile": running_profile,
            "was_paused": was_paused,
            "agitator_seconds": agitator_seconds,
            "air_pump_seconds": air_seconds,
        }
    )





@reservoirs_bp.route("/debug/profiles")
def debug_profiles():
    import os, glob
    ctx = _CTX()
    prof_dir = ctx.get("PROFILE_DIR")
    try:
        files = sorted([os.path.basename(p) for p in glob.glob(os.path.join(prof_dir, "*.json"))], key=str.lower)
        exists = os.path.isdir(prof_dir)
    except Exception as e:
        files, exists = [f"<error: {e}>"], False
    return jsonify({"PROFILE_DIR": prof_dir, "exists": exists, "count": len(files), "files": files})



#
# ───────────────────────────── Dosing & Mixing ─────────────────────────────
#

@reservoirs_bp.route("/api/reservoirs/dose", methods=["POST"])
def api_reservoirs_dose():
    """
    Body: {
      "profile": {
        "nutrients": {
          "A": {"ml_for": <float litres>, "ml": <float ml>},
          "B": {"ml_for": <float litres>, "ml": <float ml>}
        }
      },
      "filled_litres": <float>
    }
    """
    ctx = _CTX()
    logger = ctx["LOGGER"]
    sd = ctx["status_data"]

    payload = request.get_json(force=True) or {}
    profile = payload.get("profile") or {}
    litres  = float(payload.get("filled_litres") or 0.0)

    if not litres:
        litres = float(sd.get("reservoir_water_kg") or 0.0)

    def _ratio(n):
        try:
            nsec = (profile.get("nutrients") or {}).get(n) or {}
            base_l = float(nsec.get("ml_for") or 0.0)
            base_ml = float(nsec.get("ml") or 0.0)
            return (base_ml / base_l) if base_l > 0 else 0.0
        except Exception:
            return 0.0

    a_per_l = _ratio("A")
    b_per_l = _ratio("B")
    need_a_ml = max(0.0, a_per_l * litres)
    need_b_ml = max(0.0, b_per_l * litres)

    try:
        from reservoirs.service import run_dose_ml, plan_seconds_for_ml, clear_dose_cancel_flag
    except Exception:
        run_dose_ml = None
        plan_seconds_for_ml = None
        clear_dose_cancel_flag = None

    # --- compute planned seconds from calibration BEFORE starting
    if plan_seconds_for_ml is not None:
        plan = plan_seconds_for_ml(need_a_ml, need_b_ml)
    else:
        plan = {"A_seconds": 0.0, "B_seconds": 0.0}

    # --- start a brand-new generation; clear cancel + clean slate flags
    try:
        from reservoirs.service import bump_gen, clear_dose_cancel_flag
    except Exception:
        bump_gen = None

    try:
        if bump_gen:
            gen = bump_gen()
            sd["dosing_gen"] = int(gen)
        if clear_dose_cancel_flag:
            try:
                clear_dose_cancel_flag()
            except Exception:
                pass

        sd.pop("dosing_cancelled", None)
        sd.pop("reservoir_dose_cancel", None)
        sd["dosing_running"] = True
        sd["dosing_phase"] = None
        sd["dosing_phase_started_at"] = None
        sd["dosing_started_at"] = time.time()
        sd["nutrient_A_on"] = False
        sd["nutrient_B_on"] = False
        sd["dosing_plan"] = {
            "A_seconds": float(plan.get("A_seconds") or 0.0),
            "B_seconds": float(plan.get("B_seconds") or 0.0),
        }
    except Exception:
        pass
        

    # --- if service missing, fail gracefully but leave UI consistent
    if run_dose_ml is None:
        sd["dosing_running"] = False
        return jsonify({"ok": False, "error": "service.run_dose_ml not available"}), 500

    # --- worker that performs the blocking dose while this endpoint returns 200 OK
    def _dose_worker(_need_a_ml: float, _need_b_ml: float, _litres: float, _gen: int, _app):
        # Open a Flask app context so service.py can use current_app safely
        with _app.app_context():
            res = {}
            try:
                res = run_dose_ml(_need_a_ml, _need_b_ml, logger=logger) or {}
            finally:
                # Only tidy the flags if still the active generation
                try:
                    sd_local = _CTX()["status_data"]  # fetch via app context
                    if int(sd_local.get("dosing_gen") or 0) == int(_gen):
                        if not sd_local.get("nutrient_A_on") and not sd_local.get("nutrient_B_on"):
                            sd_local["dosing_running"] = False
                            sd_local["dosing_phase"] = None
                except Exception:
                    pass

                # persist the actual seconds used (for UI) only if still current gen
                try:
                    sd_local = _CTX()["status_data"]
                    if int(sd_local.get("dosing_gen") or 0) == int(_gen):
                        sd_local["dosing_plan"] = {
                            "A_seconds": float(res.get("A_seconds") or sd_local.get("dosing_plan", {}).get("A_seconds") or 0.0),
                            "B_seconds": float(res.get("B_seconds") or sd_local.get("dosing_plan", {}).get("B_seconds") or 0.0),
                        }
                except Exception:
                    pass

                # log completion (logging is harmless even if gen changed)
                try:
                    pid = _active_profile_id()
                    logger.log_event(
                        "reservoir_dose",
                        "Nutrient dosing complete",
                        profile_id=pid,
                        payload={
                            "litres": _litres,
                            "need_a_ml": round(_need_a_ml, 2),
                            "need_b_ml": round(_need_b_ml, 2),
                            "ran_a_s": res.get("A_seconds"),
                            "ran_b_s": res.get("B_seconds"),
                        }
                    )
                except Exception:
                    pass







    # capture the real Flask app object
    from flask import current_app as _flask_current_app
    _app = _flask_current_app._get_current_object()

    threading.Thread(
        target=_dose_worker,
        args=(need_a_ml, need_b_ml, litres, int(sd.get("dosing_gen") or 0), _app),
        daemon=True
    ).start()







    # --- return immediately so the UI flips to RUNNING and shows STOP
    return jsonify({
        "ok": True,
        "dosed_ml": {"A": need_a_ml, "B": need_b_ml},
        "seconds": {
            "A_seconds": float(sd.get("dosing_plan", {}).get("A_seconds") or plan.get("A_seconds") or 0.0),
            "B_seconds": float(sd.get("dosing_plan", {}).get("B_seconds") or plan.get("B_seconds") or 0.0),
        }
    })









@reservoirs_bp.route("/api/reservoirs/mix", methods=["POST"])
def api_reservoirs_mix():
    """
    Runs the agitator for X seconds from global settings (new field: agitator_mix_seconds).
    """
    ctx = _CTX()
    gs = ctx["load_global_settings"]()
    secs = float(gs.get("agitator_mix_seconds", 30) or 30)

    try:
        from reservoirs.service import run_agitator_seconds
    except Exception:
        run_agitator_seconds = None

    if run_agitator_seconds is None:
        return jsonify({"ok": False, "error": "service.run_agitator_seconds not available"}), 500

    run_agitator_seconds(secs)

    try:
        pid = _active_profile_id()
        ctx["LOGGER"].log_event("reservoir_mix", f"Agitator ran for {secs} s", profile_id=pid)
    except Exception:
        pass




    return jsonify({"ok": True, "seconds": secs})


@reservoirs_bp.route("/api/reservoirs/complete", methods=["POST"])
def api_reservoirs_complete():
    """
    Called at the end of the wizard. Stamps last fill time and logs.
    Body (optional): {"profile_name":"..."} – stored in the log payload.
    """
    from datetime import datetime, timezone
    ctx = _CTX()
    sd = ctx["status_data"]
    logger = ctx["LOGGER"]

    body = request.get_json(silent=True) or {}
    profile_name = (body.get("profile_name") or "").strip() or None
    client_stamp = body.get("completed_at_client")

    now_utc = datetime.now(timezone.utc)
    iso_utc = now_utc.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    iso_local = now_utc.astimezone().isoformat()

    try:
        sd["reservoir_last_fill_iso"] = iso_utc
    except Exception:
        sd["reservoir_last_fill_iso"] = None

    payload = {
        "profile_used": profile_name,
        "completed_at_utc": iso_utc,
        "completed_at_local": iso_local,
    }
    if client_stamp:
        payload["completed_at_client"] = client_stamp

    try:
        pid = _active_profile_id()
        logger.log_event(
            "reservoir_renewal",
            "Reservoir renewal complete",
            reason_code="complete",
            profile_id=pid,
            payload=payload,
            ts_utc=iso_utc,
            ts_local=iso_local,
        )
    except Exception:
        pass

    return jsonify({
        "ok": True,
        "last_fill": sd.get("reservoir_last_fill_iso"),
        "completed_at_utc": iso_utc,
        "completed_at_local": iso_local,
    })

@reservoirs_bp.route("/api/nutrient/prime", methods=["POST"])
def api_nutrient_prime():
    """
    Body: {"pump":"A"|"B", "on": true|false}
    Instant ON/OFF for priming (no timers, no lag).
    """
    d = request.get_json(force=True) or {}
    pump = (d.get("pump") or "").strip().upper()
    on = bool(d.get("on"))

    try:
        from reservoirs.calibration import prime
    except Exception:
        return jsonify({"ok": False, "error": "calibration.prime not available"}), 500

    prime(pump, on)
    try:
        _CTX()["LOGGER"].log_event("nutrient_prime", f"Pump {pump} {'ON' if on else 'OFF'}")
    except Exception:
        pass

    return jsonify({"ok": True})


@reservoirs_bp.route("/api/nutrient/run", methods=["POST"])
def api_nutrient_run_seconds():
    """
    Body: {"pump":"A"|"B", "seconds": <float>}
    Runs pump for exact seconds using monotonic timers.
    """
    d = request.get_json(force=True) or {}
    pump = (d.get("pump") or "").strip().upper()
    seconds = float(d.get("seconds") or 0.0)

    try:
        from reservoirs.calibration import run_for_seconds
    except Exception:
        return jsonify({"ok": False, "error": "calibration.run_for_seconds not available"}), 500

    run_for_seconds(pump, seconds)

    try:
        _CTX()["LOGGER"].log_event("nutrient_cal_run", f"Pump {pump} ran for {seconds:.2f}s")
    except Exception:
        pass

    return jsonify({"ok": True})


@reservoirs_bp.route("/api/nutrient/stop", methods=["POST"])
def api_nutrient_emergency_stop():
    """
    Immediately stop nutrient pumps (A and B) and cancel any in-flight dosing.
    Also invalidates the current dosing generation so stale workers can't write.
    """
    ctx = _CTX()
    sd = ctx["status_data"]

    # Helpers
    try:
        from reservoirs.calibration import prime
    except Exception:
        return jsonify({"ok": False, "error": "calibration.prime not available"}), 500
    try:
        from reservoirs.service import cancel_current_dose_immediately, bump_gen
    except Exception:
        cancel_current_dose_immediately = None
        bump_gen = None

    # HARD STOP both pumps
    try: prime("A", False)
    except Exception: pass
    try: prime("B", False)
    except Exception: pass

    # Cancel and bump generation so any old worker cannot write further state
    if cancel_current_dose_immediately:
        try: cancel_current_dose_immediately()
        except Exception: pass
    if bump_gen:
        try:
            new_gen = bump_gen()
            sd["dosing_gen"] = int(new_gen)
        except Exception:
            pass

    # Full reset of dosing flags (clean slate)
    sd["reservoir_dose_cancel"] = True
    sd["dosing_cancelled"] = True
    sd["dosing_running"] = False
    sd["dosing_phase"] = None
    sd["dosing_phase_started_at"] = None
    sd["dosing_plan"] = None
    sd["nutrient_A_on"] = False
    sd["nutrient_B_on"] = False

    try:
        ctx["LOGGER"].log_event("nutrient_emergency_stop", "Emergency stop invoked")
    except Exception:
        pass

    return jsonify({"ok": True})









@reservoirs_bp.route("/api/nutrient/calc", methods=["POST"])
def api_nutrient_record_measurement():
    """
    Body: {"pump":"A"|"B", "seconds": <float>, "measured_ml": <float>}
    Updates ml/s calibration store and returns the new calibration map.
    """
    d = request.get_json(force=True) or {}
    pump = (d.get("pump") or "").strip().upper()
    seconds = float(d.get("seconds") or 0.0)
    measured_ml = float(d.get("measured_ml") or 0.0)

    try:
        from reservoirs.calibration import record_measurement
    except Exception:
        return jsonify({"ok": False, "error": "calibration.record_measurement not available"}), 500

    cal = record_measurement(pump, seconds, measured_ml)

    try:
        _CTX()["LOGGER"].log_event(
            "nutrient_calibration",
            "Calibration updated",
            payload={
                "pump": pump, "seconds": seconds, "measured_ml": measured_ml,
                "ml_per_s": (cal.get(pump) or {}).get("ml_per_s")
            }
        )
    except Exception:
        pass

    return jsonify({"ok": True, "cal": cal})


