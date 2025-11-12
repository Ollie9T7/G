import os, json, sqlite3, threading, queue, time, datetime, atexit
from typing import Optional, Dict, Any, Iterable, Tuple

ISO_UTC = "%Y-%m-%dT%H:%M:%S.%fZ"

def _utc_now() -> str:
    return datetime.datetime.utcnow().strftime(ISO_UTC)

def _local_now() -> str:
    return datetime.datetime.now().isoformat(timespec="milliseconds")

class EventLogger:
    """
    Single-writer event logger.
    Use: LOGGER.log_event(...); a background thread batches inserts into SQLite.
    """

    def __init__(self, db_path: str, schema_path: str, batch_size: int = 50, flush_ms: int = 250):
        self.db_path = db_path
        self.schema_path = schema_path
        self.batch_size = batch_size
        self.flush_ms = flush_ms
        self.q: "queue.Queue[Tuple[str, Tuple]]" = queue.Queue(maxsize=5000)
        self._stop = threading.Event()
        self._thr: Optional[threading.Thread] = None

        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._init_db()

        atexit.register(self.stop)

    def _init_db(self):
        conn = sqlite3.connect(self.db_path)
        try:
            with open(self.schema_path, "r", encoding="utf-8") as f:
                conn.executescript(f.read())
            conn.commit()
        finally:
            conn.close()

    def start(self):
        if self._thr and self._thr.is_alive():
            return self
        self._stop.clear()
        self._thr = threading.Thread(target=self._run, name="EventLogger", daemon=True)
        self._thr.start()
        return self

    def stop(self):
        self._stop.set()
        try:
            self.q.put_nowait(("__STOP__", ()))
        except queue.Full:
            pass
        if self._thr and self._thr.is_alive():
            self._thr.join(timeout=2.0)
        self._thr = None

    def _run(self):
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        cur = conn.cursor()
        insert_sql = """
            INSERT INTO events (
              ts_utc, ts_local, event_type, reason_code, msg,
              profile_id, profile_version, stage, cycle_id, actor, cfg_sha, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        pending = []
        deadline = time.time() + (self.flush_ms / 1000.0)

        while not self._stop.is_set():
            try:
                item = self.q.get(timeout=self.flush_ms / 1000.0)
            except queue.Empty:
                item = None

            if item is not None:
                tag, values = item
                if tag == "__STOP__":
                    if pending:
                        cur.executemany(insert_sql, pending)
                        conn.commit()
                        pending.clear()
                    break
                pending.append(values)

            if pending and (len(pending) >= self.batch_size or time.time() >= deadline):
                try:
                    cur.executemany(insert_sql, pending)
                    conn.commit()
                except Exception as e:
                    self._fallback_dump(pending, e)
                finally:
                    pending.clear()
                    deadline = time.time() + (self.flush_ms / 1000.0)

        conn.close()

    def _fallback_dump(self, rows: Iterable[Tuple], err: Exception):
        dump_path = self.db_path + ".fallback.ndjson"
        now = _utc_now()
        with open(dump_path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts_utc": now, "error": str(err), "rows": len(list(rows))}) + "\n")

    # ---------- Public API ----------
    def log_event(
        self,
        event_type: str,
        msg: str,
        *,
        reason_code: Optional[str] = None,
        profile_id: Optional[str] = None,
        profile_version: Optional[int] = None,
        stage: Optional[str] = None,
        cycle_id: Optional[str] = None,
        actor: Optional[str] = None,
        cfg_sha: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
        ts_utc: Optional[str] = None,
        ts_local: Optional[str] = None,
    ):
        tsu = ts_utc or _utc_now()
        tsl = ts_local or _local_now()
        values = (
            tsu,
            tsl,
            event_type,
            reason_code,
            msg,
            profile_id,
            profile_version,
            stage,
            cycle_id,
            actor,
            cfg_sha,
            json.dumps(payload or {}, ensure_ascii=False),
        )
        try:
            self.q.put_nowait(("insert", values))
        except queue.Full:
            try:
                self.q.get_nowait()
                self.q.put_nowait(("insert", values))
            except Exception:
                pass

    def flush(self):
        time.sleep(self.flush_ms / 1000.0 + 0.05)



