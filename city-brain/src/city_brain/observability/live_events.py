import time
from collections import deque

_live_events = deque(maxlen=50)

def log_live_event(event_type: str, details: str, css_class: str = ""):
    _live_events.append({
        "ts": time.time(), 
        "type": event_type, 
        "details": details, 
        "class": css_class
    })

def get_live_events():
    return list(_live_events)