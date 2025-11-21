import json
import os
import tempfile
from typing import Optional

CONFIG_DIR = os.path.join(os.path.dirname(__file__), "..", "config")
STATE_PATH = os.path.join(CONFIG_DIR, "reservoir_state.json")
os.makedirs(CONFIG_DIR, exist_ok=True)

def _read_state() -> dict:
    try:
        with open(STATE_PATH, "r") as f:
            return json.load(f) or {}
    except Exception:
        return {}

def load_last_fill_iso() -> Optional[str]:
    """Return the last reservoir fill ISO timestamp persisted to disk, if any."""
    data = _read_state()
    val = data.get("last_fill_iso")
    return val if isinstance(val, str) and val.strip() else None

def load_humid_last_fill_iso() -> Optional[str]:
    """Return the last humidifier reservoir fill ISO timestamp, if any."""
    data = _read_state()
    val = data.get("humid_last_fill_iso")
    return val if isinstance(val, str) and val.strip() else None


def save_last_fill_iso(iso_str: str) -> None:
    """Persist the provided ISO timestamp so it survives restarts/crashes."""
    tmp_path = None
    try:
        payload = _read_state()
        payload["last_fill_iso"] = iso_str
        fd, tmp_path = tempfile.mkstemp(dir=CONFIG_DIR, prefix=".resstate_", suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, STATE_PATH)
        # fsync the directory entry to be extra safe
        dir_fd = os.open(CONFIG_DIR, os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except Exception:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass

def save_humid_last_fill_iso(iso_str: str) -> None:
    """Persist the humidifier reservoir last fill time alongside the main value."""
    tmp_path = None
    try:
        payload = _read_state()
        payload["humid_last_fill_iso"] = iso_str
        fd, tmp_path = tempfile.mkstemp(dir=CONFIG_DIR, prefix=".resstate_", suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, STATE_PATH)
        dir_fd = os.open(CONFIG_DIR, os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except Exception:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass
