import os
import json
import base64
import asyncio
import websockets
from dotenv import load_dotenv

load_dotenv()

class SarvamTTSLayer:
    """
    Modular Layer for Sarvam AI Text-to-Speech over WebSockets.

    Keeps one pre-warmed socket ready so the TLS/WebSocket handshake (~100-300 ms) overlaps
    LLM thinking time instead of adding to time-to-first-audio on every sentence.
    """
    def __init__(self, api_key=None):
        self.api_key = api_key or os.environ.get("SARVAM_API_KEY")
        if not self.api_key:
            raise ValueError("SARVAM_API_KEY is not set.")

        self.ws_url = "wss://api.sarvam.ai/text-to-speech/ws?model=bulbul:v3&send_completion_event=true"
        self.speaker = os.environ.get("SARVAM_SPEAKER", "simran")
        self._spare = None  # asyncio.Task resolving to an open socket (or None on failure)

    def _contains_devanagari(self, s: str) -> bool:
        # If Hindi words are in Devanagari, Sarvam should use hi-IN.
        # Otherwise we default to English-first output.
        for ch in s or "":
            if "ऀ" <= ch <= "ॿ":
                return True
        return False

    async def _open(self):
        try:
            return await websockets.connect(
                self.ws_url,
                extra_headers={"api-subscription-key": self.api_key},
                open_timeout=5,
            )
        except Exception as e:
            print(f"[Sarvam TTS Connect Error]: {type(e).__name__}: {e!r}")
            return None

    def prewarm(self):
        """Start opening a socket in the background (no-op if one is already pending/ready)."""
        if self._spare is None:
            self._spare = asyncio.create_task(self._open())

    async def _take_connection(self):
        """Use the pre-warmed socket if it is healthy, otherwise connect now. Re-arms the spare."""
        ws = None
        if self._spare is not None:
            spare, self._spare = self._spare, None
            ws = await spare
            if ws is not None and not ws.open:
                ws = None
        if ws is None:
            ws = await self._open()
        self.prewarm()  # next sentence gets a warm socket too
        return ws

    async def close(self):
        if self._spare is not None:
            spare, self._spare = self._spare, None
            spare.cancel()
            try:
                ws = await spare
                if ws is not None:
                    await ws.close()
            except (asyncio.CancelledError, Exception):
                pass

    async def speak(
        self,
        phrase: str,
        sample_rate=16000,
        output_codec="mp3",
        target_language_code: str | None = None,
    ):
        """
        Sends the text phrase to Sarvam and yields raw audio byte chunks.
        A stale pre-warmed socket (closed by the server while idle) is retried once on a fresh one.
        """
        config = {
            "type": "config",
            "data": {
                "target_language_code": (
                    target_language_code
                    if target_language_code
                    else ("hi-IN" if self._contains_devanagari(phrase) else "en-IN")
                ),
                "speaker": self.speaker,
                "speech_sample_rate": sample_rate,
                "pace": 1.05,
                "enable_preprocessing": True,
                "output_audio_codec": output_codec,
            }
        }

        for attempt in range(2):
            ws = await self._take_connection() if attempt == 0 else await self._open()
            if ws is None:
                continue
            yielded = False
            try:
                # 1. Send Config, Text and Flush
                await ws.send(json.dumps(config))
                await ws.send(json.dumps({"type": "text", "data": {"text": phrase}}))
                await ws.send(json.dumps({"type": "flush"}))

                # 2. Receive Audio
                while True:
                    raw = await asyncio.wait_for(ws.recv(), timeout=20.0)
                    if isinstance(raw, bytes):
                        yielded = True
                        yield raw
                        continue
                    payload = json.loads(raw)
                    msg_type = payload.get("type", "")

                    if msg_type == "audio":
                        b64 = payload.get("data", {}).get("audio", "")
                        if b64:
                            yielded = True
                            yield base64.b64decode(b64)
                    elif msg_type == "event" and payload.get("data", {}).get("event_type", "") == "final":
                        return  # End of audio stream for this phrase
                    elif msg_type == "error":
                        print(f"[Sarvam TTS Error]: {payload.get('data', {}).get('message', '')}")
                        return
            except asyncio.TimeoutError:
                print("[Sarvam TTS] Receive timeout.")
                return
            except websockets.exceptions.ConnectionClosed as e:
                if yielded:
                    print(f"[Sarvam TTS] Connection closed mid-stream: {e}")
                    return
                # Stale socket before any audio: retry once on a fresh connection.
            except Exception as e:
                print(f"[Sarvam TTS Exception]: {e}")
                return
            finally:
                asyncio.create_task(ws.close())
