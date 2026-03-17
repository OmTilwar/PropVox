import websockets
import json
import base64
import os
import asyncio
from datetime import datetime
from typing import AsyncIterator


class TTSSession:
    def __init__(self, api_key: str, language: str):
        self.api_key = api_key
        self.language = language
        self.target_code = "hi-IN" if language == "hi" else "en-IN"
        self.ws = None
        self._ready = asyncio.Event()

    async def connect(self):
        """Open a new WS connection and send config. Sets _ready when done."""
        url = "wss://api.sarvam.ai/text-to-speech/ws?model=bulbul:v3&send_completion_event=true"
        try:
            self.ws = await asyncio.wait_for(
                websockets.connect(url, additional_headers={"api-subscription-key": self.api_key}),
                timeout=8.0
            )
            await self.ws.send(json.dumps({
                "type": "config",
                "data": {
                    "target_language_code": self.target_code,
                    "speaker": "simran",         # Bilingual — works for both en-IN and hi-IN
                    "speech_sample_rate": 24000,  # v3 native rate
                    "pace": 1.1,
                    "enable_preprocessing": True, # Handle code-mixed Hindi+English text
                    "output_audio_codec": "mp3",
                }
            }))
            ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
            print(f"[{ts}] [TTS-WS] ✅ Connected & config sent.")
        except Exception as e:
            ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
            print(f"[{ts}] [TTS-WS] ❌ Connection failed: {e}")
            self.ws = None
        finally:
            self._ready.set()   # Always unblock waiters, even on failure

    async def reconnect(self):
        """Close existing WS and open a fresh connection."""
        if self.ws:
            try:
                await self.ws.close()
            except Exception:
                pass
            self.ws = None
        self._ready = asyncio.Event()   # Reset so callers wait for the new connection
        await self.connect()

    async def close(self):
        if self.ws:
            try:
                await self.ws.close()
            except Exception:
                pass

    async def synthesize_stream(self, text: str) -> AsyncIterator[bytes]:
        # Wait for the background connect() to finish
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=10.0)
        except asyncio.TimeoutError:
            print(f"[TTS-WS] ⏱ Timed out waiting for connection.")
            return

        # If WS is not open (connection failure or closed after 422), skip
        if not self.ws or self.ws.state.name != "OPEN":
            print(f"[TTS-WS] ⚠️ WS not open — skipping sentence.")
            return

        text = text.strip()
        if not text:
            return

        try:
            await self.ws.send(json.dumps({"type": "text", "data": {"text": text}}))
            await self.ws.send(json.dumps({"type": "flush"}))
        except Exception as e:
            print(f"[TTS-WS] ❌ Send failed: {e}")
            asyncio.create_task(self.reconnect())
            return

        chunk_count = 0
        try:
            while True:
                try:
                    raw = await asyncio.wait_for(self.ws.recv(), timeout=8.0)
                except asyncio.TimeoutError:
                    print(f"[TTS-WS] ⏱ Timeout waiting for audio (got {chunk_count} chunks).")
                    break

                if isinstance(raw, bytes):
                    yield raw
                    continue

                try:
                    payload = json.loads(raw)
                except Exception:
                    continue

                msg_type = payload.get("type")

                if msg_type == "audio":
                    b64 = payload.get("data", {}).get("audio", "")
                    if b64:
                        chunk = base64.b64decode(b64)
                        chunk_count += 1
                        yield chunk

                elif msg_type == "event":
                    if payload.get("data", {}).get("event_type") == "final":
                        break   # Sentence done — WS still open for next sentence

                elif msg_type == "error":
                    code = payload.get("data", {}).get("code", 0)
                    msg  = payload.get("data", {}).get("message", "")
                    print(f"[TTS-WS] ❌ Server error {code}: {msg[:80]}")
                    # 422 = language mismatch — reconnect so next sentence gets a fresh session
                    asyncio.create_task(self.reconnect())
                    break

        except websockets.exceptions.ConnectionClosedOK:
            # Server closed cleanly — reconnect for next sentence
            asyncio.create_task(self.reconnect())
        except Exception as e:
            print(f"[TTS-WS] ❌ Exception in receive loop: {e}")
            asyncio.create_task(self.reconnect())


class TTSSessionContext:
    """Async context manager: fires WS connect as background task immediately on enter."""

    def __init__(self, api_key: str, language: str):
        self.session = TTSSession(api_key, language)
        self._connect_task: asyncio.Task | None = None

    async def __aenter__(self) -> TTSSession:
        # Start WS connect in parallel with LLM streaming
        self._connect_task = asyncio.create_task(self.session.connect())
        return self.session

    async def __aexit__(self, *args):
        if self._connect_task:
            try:
                await self._connect_task
            except Exception:
                pass
        await self.session.close()


class TTSEngine:
    def __init__(self):
        self.sarvam_api_key = os.environ.get("SARVAM_API_KEY")
        if not self.sarvam_api_key:
            print("WARNING: SARVAM_API_KEY is missing in .env")

    def create_session(self, language: str) -> TTSSessionContext:
        return TTSSessionContext(self.sarvam_api_key, language)


tts_engine = TTSEngine()
