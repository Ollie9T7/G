# growcontroller/logging_helpers.py
from typing import Optional, Dict, Any
from logging_store.store import EventLogger

LOGGER: Optional[EventLogger] = None

def bind_logger(logger: EventLogger):
    global LOGGER
    LOGGER = logger

def _log(event_type: str, msg: str, **kw):
    if LOGGER is None:
        return
    try:
        LOGGER.log_event(event_type, msg, **kw)
    except Exception:
        pass

def log_global_settings_snapshot(profile_id: str, globals_dict: Dict[str, Any], *, reason: str = "globals"):
    _log(
        "profile_lifecycle",
        "Global settings",
        reason_code=reason,            # "globals" or "globals_edit"
        profile_id=(profile_id or ""),
        actor="ui",
        payload={"global_parameters": globals_dict or {}},
    )


def log_profile_resume(profile_name: str, previous_run_id: Optional[str] = None):
    _log(
        "profile_lifecycle",
        "Profile resumed",
        reason_code="resume",
        profile_id=profile_name,
        actor="system",
        payload={"previous_run_id": previous_run_id},
    )



