# state_manager.py
import json, os, tempfile

STATE_DIR = "/tmp/growcontroller"  # outside your project folder
os.makedirs(STATE_DIR, exist_ok=True)
STATE_FILE = os.path.join(STATE_DIR, "state.json")

def save_state(data):
    # atomic write
    d = os.path.dirname(STATE_FILE)
    fd, tmppath = tempfile.mkstemp(dir=d, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmppath, STATE_FILE)
        # fsync the directory entry
        dirfd = os.open(d, os.O_DIRECTORY)
        try:
            os.fsync(dirfd)
        finally:
            os.close(dirfd)
    except Exception:
        try: os.remove(tmppath)
        except Exception: pass
        raise

def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return None

def clear_state():
    try:
        os.remove(STATE_FILE)
    except Exception:
        pass



