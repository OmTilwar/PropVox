"""
Write one JSON file per call under backend/conversation_logs/ for analysis and tuning.

Contains: caller, timestamps, CRM snapshot at connect, full turns, merged summary, rolling memory.
"""
import json
from datetime import datetime
from pathlib import Path

from tzutil import IST

LOG_DIR = Path(__file__).resolve().parent / "conversation_logs"


def save_call_log(
    llm,
    caller_phone,
    *,
    stream_sid=None,
    crm_at_connect=None,
    merged_summary=None,
    call_started_at=None,
):
    """
    Safe to call with partial data. Prints path on success.
    """
    if llm is None:
        return None

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    safe_phone = (caller_phone or "unknown").replace("+", "").replace("/", "_")[:20]
    ts = datetime.now(IST).strftime("%Y%m%d_%H%M%S")
    path = LOG_DIR / f"{ts}_{safe_phone}.json"

    payload = {
        "saved_at_ist": datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S %Z"),
        "caller_phone": caller_phone,
        "stream_sid": stream_sid,
        "call_started_at_ist": call_started_at,
        "crm_at_connect": crm_at_connect,
        "merged_crm_summary_after_call": merged_summary,
        **llm.export_conversation_log(),
    }

    abs_path = path.resolve()
    with open(abs_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
    print(f"📎 Conversation log saved: {abs_path}")
    return abs_path
