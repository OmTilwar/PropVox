"""Browser demo for Myra, on the same server as the Twilio agent.

The page (web/index.html) speaks Twilio's Media Streams protocol from the browser: 8 kHz mu-law audio in
`media` events, plus `start` / `stop`. So the browser call runs through exactly the same pipeline as a phone
call (twilio_handler) — no second code path to keep in sync.

Routes on the one port:
  GET  /            the demo page (only when WEB_DEMO_CODE is set)
  GET  /healthz     200 "ok" (Render health check)
  WS   /browser     a browser call — needs ?code=<WEB_DEMO_CODE>; time-limited, capped
  WS   anything else  a Twilio Media Stream, exactly as before

Guards, because a public page spends real API credit (Deepgram, LLM, Sarvam):
  WEB_DEMO_CODE              access code; the demo is off when empty
  WEB_DEMO_MAX_SECONDS       per-call limit (default 180)
  WEB_DEMO_MAX_CONCURRENT    calls at once (default 2)
  WEB_DEMO_MAX_PER_DAY       calls per IST day (default 40)
  WEB_DEMO_ALLOWED_ORIGINS   page origins allowed to start calls, e.g. https://propvox.vercel.app (empty = any)

The page can also be hosted elsewhere (Vercel): web/config.js points it at this server, and /check sends CORS
headers for the allowed origins.
"""
from __future__ import annotations

import asyncio
import hmac
import os
from datetime import datetime
from http import HTTPStatus
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from tzutil import IST

PAGE = Path(__file__).resolve().parents[1] / "web" / "index.html"
WEB_PREFIX = "web-"          # caller ids from the browser start with this (CRM memory per browser)


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


class DemoGate:
    """Access code, concurrency and daily caps for browser calls."""

    def __init__(self):
        self.active = 0
        self.day = None
        self.count = 0

    @property
    def code(self) -> str:
        return os.environ.get("WEB_DEMO_CODE", "").strip()

    @property
    def enabled(self) -> bool:
        return bool(self.code)

    def _roll_day(self) -> None:
        today = datetime.now(IST).date()
        if today != self.day:
            self.day, self.count = today, 0

    def check(self, code: str | None) -> tuple[HTTPStatus, str] | None:
        """None = allowed; otherwise (status, reason)."""
        if not self.enabled:
            return HTTPStatus.NOT_FOUND, "Web demo is off.\n"
        if not code or not hmac.compare_digest(code.strip(), self.code):
            return HTTPStatus.FORBIDDEN, "Wrong or missing access code.\n"
        self._roll_day()
        if self.active >= _int("WEB_DEMO_MAX_CONCURRENT", 2):
            return HTTPStatus.TOO_MANY_REQUESTS, "Myra is on other calls right now. Try again in a few minutes.\n"
        if self.count >= _int("WEB_DEMO_MAX_PER_DAY", 40):
            return HTTPStatus.TOO_MANY_REQUESTS, "Today's demo calls are used up. Try again tomorrow.\n"
        return None

    def opened(self) -> None:
        self._roll_day()
        self.active += 1
        self.count += 1

    def closed(self) -> None:
        self.active = max(0, self.active - 1)


GATE = DemoGate()


def _is_upgrade(headers) -> bool:
    return "websocket" in (headers.get("Upgrade") or "").lower()


def allowed_origins() -> set[str]:
    """WEB_DEMO_ALLOWED_ORIGINS: comma-separated page origins (e.g. https://propvox.vercel.app) that may start
    browser calls. Empty = any origin (local development)."""
    raw = os.environ.get("WEB_DEMO_ALLOWED_ORIGINS", "")
    return {o.strip().rstrip("/") for o in raw.split(",") if o.strip()}


def origin_ok(request_headers) -> bool:
    allowed = allowed_origins()
    if not allowed:
        return True
    origin = (request_headers.get("Origin") or "").rstrip("/")
    return origin in allowed


def _cors(request_headers) -> list[tuple[str, str]]:
    origin = (request_headers.get("Origin") or "").rstrip("/")
    if origin and (not allowed_origins() or origin in allowed_origins()):
        return [("Access-Control-Allow-Origin", origin), ("Vary", "Origin")]
    return []


def _text(status: HTTPStatus, body: str, extra: list[tuple[str, str]] | None = None):
    headers = [("Content-Type", "text/plain; charset=utf-8"), ("Cache-Control", "no-store"), *(extra or [])]
    return status, headers, body.encode()


async def process_request(path: str, request_headers):
    """websockets' HTTP hook: serve the page / health check; gate browser calls; let Twilio through."""
    url = urlparse(path)
    if not _is_upgrade(request_headers):
        if url.path == "/healthz":     # also the page's wake-up ping (Render's free plan sleeps when idle)
            return _text(HTTPStatus.OK, "ok\n", _cors(request_headers))
        if url.path == "/check":       # lets the page explain a refusal before it opens the call
            cors = _cors(request_headers)
            if not origin_ok(request_headers):
                return _text(HTTPStatus.FORBIDDEN, "This page isn't allowed to start calls.\n", cors)
            refused = GATE.check((parse_qs(url.query).get("code") or [None])[0])
            return _text(*refused, cors) if refused else _text(HTTPStatus.OK, "ok\n", cors)
        if url.path in ("/", "/index.html") and GATE.enabled and PAGE.exists():
            return HTTPStatus.OK, [("Content-Type", "text/html; charset=utf-8"), ("Cache-Control", "no-store")], \
                PAGE.read_bytes()
        if url.path == "/config.js" and GATE.enabled:   # served here = same-origin backend (local runs)
            return HTTPStatus.OK, [("Content-Type", "application/javascript; charset=utf-8"), ("Cache-Control", "no-store")], \
                b'window.PROPVOX_BACKEND = "";\n'
        return _text(HTTPStatus.NOT_FOUND, "Not found.\n")
    if url.path == "/browser":
        if not origin_ok(request_headers):       # only the demo page itself may open calls
            return _text(HTTPStatus.FORBIDDEN, "This page isn't allowed to start calls.\n")
        refused = GATE.check((parse_qs(url.query).get("code") or [None])[0])
        if refused:
            return _text(*refused)
    return None          # a WebSocket we accept: browser (checked above) or Twilio


def is_browser(path: str) -> bool:
    return urlparse(path).path == "/browser"


def router(call_handler):
    """Wrap the Twilio handler: browser calls are counted and closed at the time limit."""
    async def handle(websocket, path):
        if not is_browser(path):
            return await call_handler(websocket, path)
        GATE.opened()
        limit = _int("WEB_DEMO_MAX_SECONDS", 180)
        loop = asyncio.get_running_loop()
        # closing the socket ends the handler's receive loop normally, so its cleanup (CRM summary) still runs
        timer = loop.call_later(limit, lambda: asyncio.ensure_future(websocket.close(4000, "Demo time limit reached")))
        print(f"🌐 [Web demo] Browser call opened ({GATE.active} active, {GATE.count} today, limit {limit}s)")
        try:
            await call_handler(websocket, path)
        finally:
            timer.cancel()
            GATE.closed()
            print(f"🌐 [Web demo] Browser call closed ({GATE.active} active)")
    return handle


def save_logs_for(caller_id: str | None) -> bool:
    """Browser visitors' calls are not written to conversation logs unless WEB_DEMO_SAVE_LOGS=1."""
    if caller_id and caller_id.startswith(WEB_PREFIX):
        return os.environ.get("WEB_DEMO_SAVE_LOGS", "") == "1"
    return True
