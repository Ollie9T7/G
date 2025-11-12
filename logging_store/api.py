from flask import Blueprint, request, Response, jsonify, current_app, stream_with_context
import sqlite3, csv, io, json

logs_bp = Blueprint("logs_bp", __name__)

# ───────────────────────────── helpers ─────────────────────────────
def _get_db():
    path = current_app.config.get("EVENTS_DB_PATH", "data/logs/events.db")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


# ───────────────────────────── routes ──────────────────────────────
@logs_bp.route("/events")
def list_events():
    """
    Simple UI log view: recent notable events.
    Query:
      limit (int) default 200
      types=comma,separated,list  (actuator_change,irrigation_cycle,alert,profile_lifecycle)
    """
    limit = int(request.args.get("limit", 200))
    types = request.args.get(
        "types",
        "actuator_change,irrigation_cycle,alert,profile_lifecycle"
    ).split(",")

    qmarks = ",".join("?" for _ in types)

    sql = f"""
        SELECT ts_local, event_type, reason_code, msg,
               profile_id, profile_version, stage, cycle_id, actor, cfg_sha
        FROM events
        WHERE event_type IN ({qmarks})
        ORDER BY ts_utc DESC
        LIMIT ?
    """
    with _get_db() as conn:
        rows = conn.execute(sql, [*types, limit]).fetchall()
    return jsonify([dict(r) for r in rows])


@logs_bp.route("/export.csv")
def export_csv():
    """
    CSV export of base event columns.

    Query params:
      from=YYYY-MM-DD, to=YYYY-MM-DD  (inclusive day range; 'to' handled as < next day)
      type=<event_type>               (optional exact match)
      profile_id=<id>                 (optional)
      current=1                       (optional; if set and profile_id empty, auto-detect latest non-empty profile_id)
    """
    import datetime as _dt

    params = []
    where = []

    frm = request.args.get("from")
    to = request.args.get("to")
    ev_type = request.args.get("type")
    profile_id = request.args.get("profile_id")
    want_current = request.args.get("current") == "1"

    db_path = current_app.config.get("EVENTS_DB_PATH", "data/logs/events.db")

    # Time window
    if frm:
        where.append("ts_utc >= ?")
        params.append(f"{frm}T00:00:00.000Z")
    if to:
        try:
            to_dt = _dt.datetime.strptime(to, "%Y-%m-%d").date()
            to_plus1 = (to_dt + _dt.timedelta(days=1)).strftime("%Y-%m-%d")
            where.append("ts_utc < ?")
            params.append(f"{to_plus1}T00:00:00.000Z")
        except ValueError:
            # If 'to' wasn't a strict YYYY-MM-DD, keep same-day exclusive upper bound
            where.append("ts_utc < ?")
            params.append(f"{to}T00:00:00.000Z")

    # Filter by event_type (optional)
    if ev_type:
        where.append("event_type = ?")
        params.append(ev_type)

    # Discover current profile if asked
    if not profile_id and want_current:
        with sqlite3.connect(db_path) as _conn:
            _conn.row_factory = sqlite3.Row
            row = _conn.execute(
                """
                SELECT profile_id
                FROM events
                WHERE profile_id IS NOT NULL AND TRIM(profile_id) <> ''
                ORDER BY ts_utc DESC
                LIMIT 1
                """
            ).fetchone()
            if row and row["profile_id"]:
                profile_id = row["profile_id"]

    if profile_id:
        where.append("profile_id = ?")
        params.append(profile_id)

    base_cols = [
    "ts_utc", "ts_local", "event_type", "reason_code", "msg",
    "profile_id", "actor", "payload_json"
    ]


    sql = f"SELECT {', '.join(base_cols)} FROM events"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY ts_utc ASC"

    def generate():
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(sql, params)

            header = base_cols

            buf = io.StringIO()
            writer = csv.writer(buf)
            writer.writerow(header)
            yield buf.getvalue()
            buf.seek(0)
            buf.truncate(0)

            for row in cur:
                writer.writerow([row[c] for c in base_cols])
                yield buf.getvalue()
                buf.seek(0)
                buf.truncate(0)

    filename_bits = [frm or "start", to or "now"]
    if profile_id:
        filename_bits.append(profile_id)
    filename = "events_" + "_".join(filename_bits) + ".csv"

    return Response(
        stream_with_context(generate()),
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename=\"%s\"' % filename}
    )



