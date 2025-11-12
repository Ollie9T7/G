-- events table: append-only
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY,
  ts_utc TEXT NOT NULL,
  ts_local TEXT NOT NULL,
  event_type TEXT NOT NULL,
  reason_code TEXT,
  msg TEXT,
  profile_id TEXT,
  profile_version INTEGER,
  stage TEXT,
  cycle_id TEXT,
  actor TEXT,
  cfg_sha TEXT,
  payload_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_time ON events(ts_utc);
CREATE INDEX IF NOT EXISTS idx_events_type_time ON events(event_type, ts_utc);
CREATE INDEX IF NOT EXISTS idx_events_profile_time ON events(profile_id, ts_utc);

-- optional: minute summaries for charts later (not used yet)
CREATE TABLE IF NOT EXISTS minute_summaries (
  id INTEGER PRIMARY KEY,
  ts_minute_utc TEXT NOT NULL,
  sensor_id TEXT NOT NULL,
  name TEXT,
  unit TEXT,
  min REAL, max REAL, mean REAL, stdev REAL, samples INTEGER
);

PRAGMA journal_mode=WAL;



