import os
import json
from datetime import datetime

from tzutil import IST

try:
    import redis
except ImportError:
    redis = None

DB_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "customers.json"))
REDIS_URL = os.environ.get("REDIS_URL", "").strip()
_redis_client = None


def _format_call_dt(dt=None):
    """Human-readable IST timestamp for CRM storage."""
    when = dt or datetime.now(IST)
    if when.tzinfo is None:
        when = when.replace(tzinfo=IST)
    else:
        when = when.astimezone(IST)
    return when.strftime("%A, %d %B %Y at %I:%M %p %Z")


def _normalize_record(entry):
    """Legacy plain string -> dict with summary + optional last_call_dt."""
    if entry is None:
        return None
    if isinstance(entry, str):
        return {"summary": entry, "last_call_dt": None}
    if isinstance(entry, dict):
        return {
            "summary": entry.get("summary") or "",
            "last_call_dt": entry.get("last_call_dt"),
        }
    return {"summary": str(entry), "last_call_dt": None}


def _redis_key(phone_number):
    return f"customer:{phone_number}:history"


def _get_redis():
    global _redis_client
    if not REDIS_URL or redis is None:
        return None
    if _redis_client is None:
        try:
            _redis_client = redis.from_url(REDIS_URL, decode_responses=True)
            _redis_client.ping()
        except Exception as e:
            print(f"⚠️ [CRM] Redis unavailable, using file DB: {e}")
            _redis_client = False
    return _redis_client if _redis_client is not False else None


def _load_db():
    if not os.path.exists(DB_PATH):
        return {}
    with open(DB_PATH, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return {}

def _save_db(data):
    with open(DB_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4)

def get_customer_context(phone_number):
    """Returns the most recent CRM record as a dict {summary, last_call_dt}, or None."""
    if not phone_number:
        return None
    r = _get_redis()
    if r:
        try:
            raw = r.lindex(_redis_key(phone_number), -1)
            if not raw:
                return None
            return _normalize_record(json.loads(raw))
        except Exception as e:
            print(f"⚠️ [CRM] Redis read failed, falling back to file DB: {e}")
    db = _load_db()

    customer_notes = db.get(phone_number, [])
    if customer_notes:
        return _normalize_record(customer_notes[-1])
    return None

def save_call_summary(phone_number, summary, call_dt=None):
    """Persists merged profile with call end time in IST (for date-aware next calls)."""
    if not phone_number or not summary:
        return

    db = _load_db()
    if phone_number not in db:
        db[phone_number] = []

    record = {
        "summary": summary,
        "last_call_dt": _format_call_dt(call_dt),
    }
    r = _get_redis()
    if r:
        try:
            r.rpush(_redis_key(phone_number), json.dumps(record, ensure_ascii=False))
            print(f"📁 [CRM] Successfully saved call interaction memory for {phone_number} (Redis).")
            return
        except Exception as e:
            print(f"⚠️ [CRM] Redis write failed, falling back to file DB: {e}")

    db[phone_number].append(record)
    _save_db(db)
    print(f"📁 [CRM] Successfully saved call interaction memory for {phone_number}.")
