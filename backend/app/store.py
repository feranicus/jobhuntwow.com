"""Tiny JSON store for connection settings (KISS). Secrets stay server-side."""
import os, json, threading
from .settings import DATA_DIR

_LOCK = threading.Lock()
_PATH = os.path.join(DATA_DIR, "connections.json")

DEFAULT = {
    "qwen":     {"connected": False, "model": ""},
    "hermes":   {"connected": False},
    "telegram": {"connected": False, "bot_token": ""},
    "whatsapp": {"connected": False, "phone": ""},
    "linkedin": {"connected": False, "has_cookies": False},
}

def _load():
    if not os.path.exists(_PATH):
        return json.loads(json.dumps(DEFAULT))
    try:
        with open(_PATH, encoding="utf-8") as f:
            data = json.load(f)
        for k, v in DEFAULT.items():
            data.setdefault(k, v)
        return data
    except Exception:
        return json.loads(json.dumps(DEFAULT))

def get_public():
    """Return config WITHOUT secret values (safe for the browser)."""
    d = _load()
    return {
        "qwen":     {"connected": d["qwen"]["connected"], "model": d["qwen"].get("model", "")},
        "hermes":   {"connected": d["hermes"]["connected"]},
        "telegram": {"connected": d["telegram"]["connected"]},
        "whatsapp": {"connected": d["whatsapp"]["connected"], "phone": d["whatsapp"].get("phone", "")},
        "linkedin": {"connected": d["linkedin"]["connected"], "has_cookies": d["linkedin"].get("has_cookies", False)},
    }

def update(section: str, patch: dict):
    with _LOCK:
        os.makedirs(DATA_DIR, exist_ok=True)
        d = _load()
        d.setdefault(section, {}).update(patch)
        with open(_PATH, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
    return get_public()

def raw(section: str):
    return _load().get(section, {})
