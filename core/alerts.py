# core/alerts.py
import os
import threading
import queue
import requests

DISCORD_WEBHOOK = os.getenv(
    "DISCORD_WEBHOOK",
    "https://discord.com/api/webhooks/1410644857579503740/WX8bUFFQ7Y4QJ9c957-8k58d2aPFprYwKSsClvcLEdu9gh3sb6-jpmtVupajI84O7gEU"
)

_alert_q: "queue.Queue[str]" = queue.Queue(maxsize=256)
_worker_thread: threading.Thread | None = None
_stop_evt = threading.Event()

def _worker():
    while not _stop_evt.is_set():
        try:
            msg = _alert_q.get(timeout=0.2)
        except queue.Empty:
            continue
        try:
            if DISCORD_WEBHOOK:
                requests.post(DISCORD_WEBHOOK, json={"content": msg}, timeout=5)
        except Exception:
            pass
        finally:
            _alert_q.task_done()

def start_alert_worker():
    global _worker_thread
    if _worker_thread and _worker_thread.is_alive():
        return
    _stop_evt.clear()
    _worker_thread = threading.Thread(target=_worker, name="discord-alerts", daemon=True)
    _worker_thread.start()

def stop_alert_worker():
    _stop_evt.set()
    try:
        _alert_q.put_nowait("")
    except Exception:
        pass
    if _worker_thread and _worker_thread.is_alive():
        _worker_thread.join(timeout=2.0)

def send_discord(text: str):
    if not text or not DISCORD_WEBHOOK:
        return
    try:
        _alert_q.put_nowait(str(text))
    except queue.Full:
        pass

start_alert_worker()



