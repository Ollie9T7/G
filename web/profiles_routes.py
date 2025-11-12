#web/profiles_routes.py
import os, json, re
from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app

bp = Blueprint("profiles", __name__)

def ctx():
    return current_app.config["CTX"]

# convenience shims
def _to_int(v, default=None):  return ctx()["_to_int"](v, default)
def _to_float(v, default=None):return ctx()["_to_float"](v, default)
def _slugify(s):               return ctx()["_slugify"](s)
def upgrade_profile_schema(p): return ctx()["upgrade_profile_schema"](p)
def LOGGER():                  return ctx()["LOGGER"]

def PROFILE_DIR():             return ctx()["PROFILE_DIR"]
def ARCHIVE_DIR():             return ctx()["ARCHIVE_DIR"]

def running_profile():         return ctx()["get_running_profile"]()
def status_data():             return ctx()["status_data"]

def load_state():              return ctx()["load_state"]()
def save_state(d):             return ctx()["save_state"](d)
def clear_state():             return ctx()["clear_state"]()


# --- Nutrient ratio helper (X ml per Y litres) ---
def parse_nutrient_ratios(form):
    """
    Reads two fields per pump:
      nutrient_A_ml, nutrient_A_per_l    (and same for B, C if used)
    Stores both raw inputs and a derived ml_per_l for convenience.
    """
    out = {}
    for key in ("A", "B"):  # extend to ("A","B","C") if you have a third pump
        ml_raw   = form.get(f"nutrient_{key}_ml")
        per_lraw = form.get(f"nutrient_{key}_per_l")
        try:
            ml = float(ml_raw) if ml_raw is not None and ml_raw != "" else 0.0
        except ValueError:
            ml = 0.0
        try:
            per_l = float(per_lraw) if per_lraw is not None and per_lraw != "" else 0.0
        except ValueError:
            per_l = 0.0

        if ml > 0 and per_l > 0:
            out[key] = {
                "ml": ml,                  # X ml …
                "per_litres": per_l,       # … per Y litres
                "ml_per_l": ml / per_l     # derived convenience
            }
    return out or None





@bp.route('/profiles')
def list_profiles():
    files = [f for f in os.listdir(PROFILE_DIR()) if f.endswith('.json')]
    rc = load_state()
    return render_template('profiles.html',
                           profiles=files,
                           running_profile=running_profile(),
                           paused=bool(status_data().get("paused", False)),
                           resume_candidate=rc)

@bp.route('/archive')
def view_archive():
    archived = [f for f in os.listdir(ARCHIVE_DIR()) if f.endswith('.json')]
    return render_template('archive.html', archived_profiles=archived)

@bp.route("/new", methods=["GET", "POST"])
def new_profile():
    if request.method == "GET":
        return render_template("new_profile.html")

    # POST: collect form data
    name = (request.form.get("name") or "").strip()

    light_on  = _to_int(request.form.get("light_on"))
    light_off = _to_int(request.form.get("light_off"))
    light_int = _to_int(request.form.get("light_intensity"))

    pump_on_time  = _to_int(request.form.get("pump_on_time"))
    pump_off_time = _to_int(request.form.get("pump_off_time"))
    dur_on_min    = _to_float(request.form.get("pump_duration_on"), 0.0)
    dur_off_min   = _to_float(request.form.get("pump_duration_off"), 0.0)
    dur_on_sec    = int(round((dur_on_min or 0.0) * 60.0))
    dur_off_sec   = int(round((dur_off_min or 0.0) * 60.0))

    t_min = _to_float(request.form.get("temp_min"))
    t_max = _to_float(request.form.get("temp_max"))
    t_tgt = _to_float(request.form.get("temp_target"))

    h_min = _to_float(request.form.get("hum_min"))
    h_max = _to_float(request.form.get("hum_max"))
    h_tgt = _to_float(request.form.get("hum_target"))

    ph_min = _to_float(request.form.get("ph_min"))
    ph_max = _to_float(request.form.get("ph_max"))

    notes = request.form.get("notes") or ""

    profile = {
        "name": name,
        "lighting": {"on": light_on, "off": light_off, "intensity": light_int},
        "pump": {
            "on_time": pump_on_time, "off_time": pump_off_time,
            "duration_on": dur_on_min, "duration_off": dur_off_min,
            "on_duration_sec": dur_on_sec, "off_duration_sec": dur_off_sec
        },
        "temperature": {"min": t_min, "max": t_max, "target": t_tgt},
        "humidity":    {"min": h_min, "max": h_max, "target": h_tgt},
        "ph":          {"min": ph_min, "max": ph_max},
        "notes": notes
    }

    base = _slugify(name)
    
    nutrients = parse_nutrient_ratios(request.form)
    if nutrients:
        profile["nutrients"] = nutrients
    
    filename = f"{base}.json"
    path = os.path.join(PROFILE_DIR(), filename)
    i = 1
    while os.path.exists(path):
        filename = f"{base}_{i}.json"
        path = os.path.join(PROFILE_DIR(), filename)
        i += 1

    with open(path, "w") as f:
        json.dump(profile, f, indent=2)

    flash(f"Profile '{name}' created.")
    return redirect(url_for("profiles.edit_profile", profile_name=filename))

@bp.route("/profiles/<path:profile_name>/edit", methods=["GET", "POST"])
@bp.route("/edit/<path:profile_name>", methods=["GET", "POST"])
def edit_profile(profile_name):
    src_path = os.path.join(PROFILE_DIR(), profile_name)
    if not os.path.isfile(src_path):
        flash("Profile not found.")
        return redirect(url_for("profiles.list_profiles"))

    with open(src_path, "r") as f:
        data = json.load(f)

    if request.method == "POST":
        new_name = (request.form.get("name") or "").strip()
        light_on  = _to_int(request.form.get("light_on"))
        light_off = _to_int(request.form.get("light_off"))
        light_int = _to_int(request.form.get("light_intensity"))

        pump_on_time   = _to_int(request.form.get("pump_on_time"))
        pump_off_time  = _to_int(request.form.get("pump_off_time"))
        dur_on_min     = _to_float(request.form.get("pump_duration_on"), 0.0)
        dur_off_min    = _to_float(request.form.get("pump_duration_off"), 0.0)
        dur_on_sec  = int(round((dur_on_min or 0.0) * 60.0))
        dur_off_sec = int(round((dur_off_min or 0.0) * 60.0))

        t_min = _to_float(request.form.get("temp_min"))
        t_max = _to_float(request.form.get("temp_max"))
        t_tgt = _to_float(request.form.get("temp_target"))

        h_min = _to_float(request.form.get("hum_min"))
        h_max = _to_float(request.form.get("hum_max"))
        h_tgt = _to_float(request.form.get("hum_target"))

        ph_min = _to_float(request.form.get("ph_min"))
        ph_max = _to_float(request.form.get("ph_max"))

        notes = request.form.get("notes") or ""

        data.setdefault("lighting", {})
        data.setdefault("pump", {})
        data.setdefault("temperature", {})
        data.setdefault("humidity", {})
        data.setdefault("ph", {})

        data["name"] = new_name
        data["lighting"].update({"on": light_on, "off": light_off, "intensity": light_int})
        data["pump"].update({
            "on_time": pump_on_time, "off_time": pump_off_time,
            "duration_on": dur_on_min, "duration_off": dur_off_min,
            "on_duration_sec": dur_on_sec, "off_duration_sec": dur_off_sec
        })
        data["temperature"].update({"min": t_min, "max": t_max, "target": t_tgt})
        data["humidity"].update({"min": h_min, "max": h_max, "target": h_tgt})
        data["ph"].update({"min": ph_min, "max": ph_max})
        data["notes"] = notes
        
        nutrients = parse_nutrient_ratios(request.form)
        if nutrients:
            data["nutrients"] = nutrients


        with open(src_path, "w") as f:
            json.dump(data, f, indent=2)

        try:
            LOGGER().log_event(
                "profile_lifecycle", msg="Profile edited", reason_code="edit",
                profile_id=profile_name, actor="ui", payload={"parameters": data}
            )
        except Exception:
            pass

        message = "Profile saved."
        new_filename = profile_name
        base, ext = os.path.splitext(profile_name)
        if not ext: ext = ".json"
        if new_name:
            new_slug = _slugify(new_name)
            candidate = f"{new_slug}{ext}"
            if candidate != profile_name:
                if running_profile() == profile_name:
                    message = "Profile saved, but name change deferred: stop the profile before renaming the file."
                else:
                    dst_path = os.path.join(PROFILE_DIR(), candidate)
                    i = 1
                    while os.path.exists(dst_path) and os.path.normpath(dst_path) != os.path.normpath(src_path):
                        candidate = f"{new_slug}_{i}{ext}"
                        dst_path = os.path.join(PROFILE_DIR(), candidate); i += 1
                    try:
                        os.replace(src_path, dst_path)
                        new_filename = candidate
                        if status_data().get("profile") == profile_name:
                            status_data()["profile"] = new_filename
                        message = "Profile saved and renamed."
                    except Exception:
                        message = "Profile saved, but renaming the file failed."

        flash(message)
        return redirect(url_for("profiles.edit_profile", profile_name=new_filename))

    upgraded = data
    return render_template("edit_profile.html", profile=upgraded, filename=profile_name)

@bp.route("/archive/edit/<path:profile_name>", methods=["GET", "POST"])
def edit_archived_profile(profile_name):
    src_path = os.path.join(ARCHIVE_DIR(), profile_name)
    if not os.path.isfile(src_path):
        flash("Archived profile not found.")
        return redirect(url_for("profiles.view_archive"))

    with open(src_path, "r") as f:
        data = json.load(f)

    if request.method == "POST":
        new_name = (request.form.get("name") or "").strip()

        light_on  = _to_int(request.form.get("light_on"))
        light_off = _to_int(request.form.get("light_off"))
        light_int = _to_int(request.form.get("light_intensity"))

        pump_on_time   = _to_int(request.form.get("pump_on_time"))
        pump_off_time  = _to_int(request.form.get("pump_off_time"))
        dur_on_min     = _to_float(request.form.get("pump_duration_on"), 0.0)
        dur_off_min    = _to_float(request.form.get("pump_duration_off"), 0.0)
        dur_on_sec  = int(round((dur_on_min or 0.0) * 60.0))
        dur_off_sec = int(round((dur_off_min or 0.0) * 60.0))

        t_min = _to_float(request.form.get("temp_min"))
        t_max = _to_float(request.form.get("temp_max"))
        t_tgt = _to_float(request.form.get("temp_target"))

        h_min = _to_float(request.form.get("hum_min"))
        h_max = _to_float(request.form.get("hum_max"))
        h_tgt = _to_float(request.form.get("hum_target"))

        ph_min = _to_float(request.form.get("ph_min"))
        ph_max = _to_float(request.form.get("ph_max"))

        notes = request.form.get("notes") or ""

        data.setdefault("lighting", {})
        data.setdefault("pump", {})
        data.setdefault("temperature", {})
        data.setdefault("humidity", {})
        data.setdefault("ph", {})

        data["name"] = new_name
        data["lighting"].update({ "on": light_on, "off": light_off, "intensity": light_int })
        data["pump"].update({
            "on_time": pump_on_time, "off_time": pump_off_time,
            "duration_on": dur_on_min, "duration_off": dur_off_min,
            "on_duration_sec": dur_on_sec, "off_duration_sec": dur_off_sec
        })
        data["temperature"].update({ "min": t_min, "max": t_max, "target": t_tgt })
        data["humidity"].update({ "min": h_min, "max": h_max, "target": h_tgt })
        data["ph"].update({ "min": ph_min, "max": ph_max })
        data["notes"] = notes

        with open(src_path, "w") as f:
            json.dump(data, f, indent=2)

        old_base, old_ext = os.path.splitext(profile_name)
        if new_name:
            new_slug = _slugify(new_name)
            new_filename = f"{new_slug}{old_ext or '.json'}"
            dst_path = os.path.join(ARCHIVE_DIR(), new_filename)
            i = 1
            while os.path.exists(dst_path) and os.path.normpath(dst_path) != os.path.normpath(src_path):
                dst_path = os.path.join(ARCHIVE_DIR(), f"{new_slug}_{i}{old_ext or '.json'}")
                new_filename = os.path.basename(dst_path); i += 1

            if os.path.normpath(dst_path) != os.path.normpath(src_path):
                try:
                    os.replace(src_path, dst_path)
                    profile_name = new_filename
                    flash("Archived profile saved and renamed.")
                except Exception:
                    flash("Archived profile saved, but rename failed.")
            else:
                flash("Archived profile saved.")
        else:
            flash("Archived profile saved.")

        return redirect(url_for("profiles.edit_archived_profile", profile_name=profile_name))

    upgraded = data
    return render_template("edit_profile.html", profile=upgraded, filename=profile_name)

@bp.route('/delete/<path:profile_name>', methods=['POST'])
def delete_profile(profile_name):
    src = os.path.join(PROFILE_DIR(), profile_name)
    if not os.path.isfile(src):
        flash("Profile not found.")
        return redirect(url_for('profiles.list_profiles'))

    if running_profile() == profile_name:
        flash("Cannot delete the currently running profile. Stop it first.")
        return redirect(url_for('profiles.list_profiles'))

    base, ext = os.path.splitext(profile_name)
    dst = os.path.join(ARCHIVE_DIR(), profile_name)
    i = 1
    while os.path.exists(dst):
        dst = os.path.join(ARCHIVE_DIR(), f"{base}_arch{i}{ext}")
        i += 1

    try:
        os.replace(src, dst)
        flash(f"Profile moved to archive: {profile_name}")
    except Exception:
        flash("Failed to archive profile.")

    return redirect(url_for('profiles.list_profiles'))

@bp.route('/restore/<path:profile_name>', methods=['POST'])
def restore_profile(profile_name):
    src = os.path.join(ARCHIVE_DIR(), profile_name)
    if not os.path.isfile(src):
        flash("Archived profile not found.")
        return redirect(url_for('profiles.view_archive'))

    base, ext = os.path.splitext(profile_name)
    if not ext: ext = '.json'
    dst = os.path.join(PROFILE_DIR(), base + ext)
    i = 1
    while os.path.exists(dst):
        dst = os.path.join(PROFILE_DIR(), f"{base}_restored{i}{ext}")
        i += 1

    try:
        os.replace(src, dst)
        restored_name = os.path.basename(dst)
        flash(f"Profile restored: {restored_name}")
        return redirect(url_for('profiles.list_profiles', restored=restored_name))
    except Exception:
        flash("Failed to restore profile.")
        return redirect(url_for('profiles.view_archive'))

@bp.route('/duplicate/<path:profile_name>', methods=['POST'])
def duplicate_profile(profile_name):
    src = os.path.join(PROFILE_DIR(), profile_name)
    if not os.path.isfile(src):
        flash("Profile not found.")
        return redirect(url_for('profiles.list_profiles'))

    try:
        with open(src, "r") as f:
            data = json.load(f)
    except Exception:
        flash("Failed to read profile file.")
        return redirect(url_for('profiles.list_profiles'))

    base_name = data.get("name") or os.path.splitext(os.path.basename(profile_name))[0]
    def next_display_name(n): return f"{base_name} (copy{'' if n == 1 else f' {n}'})"

    n = 1
    while True:
        display_name = next_display_name(n)
        slug = _slugify(display_name)
        candidate = f"{slug}.json"
        dst = os.path.join(PROFILE_DIR(), candidate)
        if not os.path.exists(dst):
            break
        n += 1

    data["name"] = display_name
    try:
        with open(dst, "w") as f:
            json.dump(data, f, indent=2)
    except Exception:
        flash("Failed to write duplicate profile.")
        return redirect(url_for('profiles.list_profiles'))

    flash(f"Duplicated profile → {display_name}")
    return redirect(url_for('profiles.edit_profile', profile_name=os.path.basename(dst)))



