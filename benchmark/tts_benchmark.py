"""
TTS Latency Benchmark: Sarvam AI vs ElevenLabs
================================================
Measures Time-To-First-Byte (TTFB) for both TTS services over WebSockets.
Runs multiple rounds and prints a detailed latency report.

Usage:
    pip install websockets python-dotenv
    Copy .env.example to .env and fill in your API keys
    python tts_benchmark.py
"""

import asyncio
import base64
import json
import os
import time
from dataclasses import dataclass, field
from typing import List, Optional

import websockets
from dotenv import load_dotenv

load_dotenv()

# ─── Config ────────────────────────────────────────────────────────────────────

SARVAM_API_KEY   = os.environ.get("SARVAM_API_KEY", "")
ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY", "")
ELEVENLABS_VOICE_ID = os.environ.get("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM")

# Texts to test (vary in length to see buffering effects)
TEST_TEXTS = [
    "Hello, how are you doing today?",
    "Your loan application has been received and is currently under review.",
    "We wanted to follow up on your recent inquiry and provide you with an update.",
]

NUM_ROUNDS = 3  # How many times to test each text (average for reliability)

# ─── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class BenchmarkResult:
    service: str
    text: str
    round_num: int
    connect_ms: float       # Time to establish WS + send config
    ttfb_ms: float          # Time from sending text → first audio byte received
    total_ms: float         # Time from sending text → completion event
    first_chunk_bytes: int  # Size of first audio chunk
    total_bytes: int        # Total audio bytes received
    error: Optional[str] = None

@dataclass
class ServiceSummary:
    service: str
    results: List[BenchmarkResult] = field(default_factory=list)

    @property
    def valid(self):
        return [r for r in self.results if r.error is None]

    def avg(self, attr):
        vals = [getattr(r, attr) for r in self.valid]
        return sum(vals) / len(vals) if vals else float("inf")

    def p50(self, attr):
        vals = sorted(getattr(r, attr) for r in self.valid)
        if not vals:
            return float("inf")
        return vals[len(vals) // 2]

    def p95(self, attr):
        vals = sorted(getattr(r, attr) for r in self.valid)
        if not vals:
            return float("inf")
        idx = int(len(vals) * 0.95)
        return vals[min(idx, len(vals) - 1)]

    def min_val(self, attr):
        vals = [getattr(r, attr) for r in self.valid]
        return min(vals) if vals else float("inf")


# ─── Sarvam AI Benchmark ───────────────────────────────────────────────────────

async def benchmark_sarvam(text: str, round_num: int) -> BenchmarkResult:
    url = "wss://api.sarvam.ai/text-to-speech/ws?model=bulbul:v3&send_completion_event=true"
    t_start = time.perf_counter()
    ttfb_ms = None
    total_bytes = 0
    first_chunk_bytes = 0
    error = None

    try:
        async with websockets.connect(
            url,
            extra_headers={"api-subscription-key": SARVAM_API_KEY},
            open_timeout=10,
        ) as ws:
            # Send config
            await ws.send(json.dumps({
                "type": "config",
                "data": {
                    "target_language_code": "en-IN",
                    "speaker": "simran",
                    "speech_sample_rate": 24000,
                    "pace": 1.1,
                    "enable_preprocessing": True,
                    "output_audio_codec": "mp3",
                }
            }))
            connect_ms = (time.perf_counter() - t_start) * 1000

            # Send text and flush — record the moment we send
            t_send = time.perf_counter()
            await ws.send(json.dumps({"type": "text", "data": {"text": text}}))
            await ws.send(json.dumps({"type": "flush"}))

            # Receive loop
            while True:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=10.0)
                except asyncio.TimeoutError:
                    error = "Receive timeout"
                    break

                if isinstance(raw, bytes):
                    chunk = raw
                else:
                    try:
                        payload = json.loads(raw)
                    except Exception:
                        continue

                    msg_type = payload.get("type", "")

                    if msg_type == "audio":
                        b64 = payload.get("data", {}).get("audio", "")
                        chunk = base64.b64decode(b64) if b64 else b""
                    elif msg_type == "event":
                        evt = payload.get("data", {}).get("event_type", "")
                        if evt == "final":
                            break
                        continue
                    elif msg_type == "error":
                        error = payload.get("data", {}).get("message", "unknown error")
                        break
                    else:
                        continue

                if chunk:
                    if ttfb_ms is None:
                        ttfb_ms = (time.perf_counter() - t_send) * 1000
                        first_chunk_bytes = len(chunk)
                    total_bytes += len(chunk)

            total_ms = (time.perf_counter() - t_send) * 1000

    except Exception as e:
        connect_ms = (time.perf_counter() - t_start) * 1000
        total_ms = connect_ms
        error = str(e)

    return BenchmarkResult(
        service="Sarvam AI",
        text=text,
        round_num=round_num,
        connect_ms=connect_ms if 'connect_ms' in dir() else 0,
        ttfb_ms=ttfb_ms if ttfb_ms is not None else -1,
        total_ms=total_ms if 'total_ms' in dir() else 0,
        first_chunk_bytes=first_chunk_bytes,
        total_bytes=total_bytes,
        error=error,
    )


# ─── ElevenLabs Benchmark ──────────────────────────────────────────────────────

async def benchmark_elevenlabs(text: str, round_num: int) -> BenchmarkResult:
    # Using flash model (eleven_flash_v2_5) for minimum latency — ~75ms model inference
    model_id = "eleven_flash_v2_5"
    url = (
        f"wss://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}/stream-input"
        f"?model_id={model_id}&optimize_streaming_latency=4&output_format=mp3_22050_32"
    )
    t_start = time.perf_counter()
    ttfb_ms = None
    total_bytes = 0
    first_chunk_bytes = 0
    error = None
    connect_ms = 0

    try:
        async with websockets.connect(
            url,
            extra_headers={"xi-api-key": ELEVENLABS_API_KEY},
            open_timeout=10,
        ) as ws:
            connect_ms = (time.perf_counter() - t_start) * 1000

            # Send BOS (Beginning of Stream) with voice settings
            await ws.send(json.dumps({
                "text": " ",
                "voice_settings": {
                    "stability": 0.5,
                    "similarity_boost": 0.8,
                    "use_speaker_boost": False,
                },
                "generation_config": {
                    # Smallest possible chunk_length_schedule = fastest TTFB
                    "chunk_length_schedule": [50, 100, 150, 200],
                },
                "xi_api_key": ELEVENLABS_API_KEY,
            }))

            # Send the actual text
            t_send = time.perf_counter()
            await ws.send(json.dumps({"text": text}))

            # Send EOS (End of Stream) to flush
            await ws.send(json.dumps({"text": ""}))

            # Receive loop
            while True:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=10.0)
                except asyncio.TimeoutError:
                    error = "Receive timeout"
                    break

                try:
                    payload = json.loads(raw)
                except Exception:
                    # raw binary
                    if isinstance(raw, bytes) and raw:
                        if ttfb_ms is None:
                            ttfb_ms = (time.perf_counter() - t_send) * 1000
                            first_chunk_bytes = len(raw)
                        total_bytes += len(raw)
                    continue

                audio_b64 = payload.get("audio", "")
                is_final  = payload.get("isFinal", False)
                err_msg   = payload.get("message", "")

                if err_msg and not audio_b64:
                    error = err_msg
                    break

                if audio_b64:
                    chunk = base64.b64decode(audio_b64)
                    if chunk:
                        if ttfb_ms is None:
                            ttfb_ms = (time.perf_counter() - t_send) * 1000
                            first_chunk_bytes = len(chunk)
                        total_bytes += len(chunk)

                if is_final:
                    break

            total_ms = (time.perf_counter() - t_send) * 1000

    except Exception as e:
        error = str(e)
        total_ms = (time.perf_counter() - t_start) * 1000

    return BenchmarkResult(
        service="ElevenLabs",
        text=text,
        round_num=round_num,
        connect_ms=connect_ms,
        ttfb_ms=ttfb_ms if ttfb_ms is not None else -1,
        total_ms=total_ms,
        first_chunk_bytes=first_chunk_bytes,
        total_bytes=total_bytes,
        error=error,
    )


# ─── Reporter ──────────────────────────────────────────────────────────────────

def color(text, code): return f"\033[{code}m{text}\033[0m"
GREEN  = lambda t: color(t, "92")
RED    = lambda t: color(t, "91")
YELLOW = lambda t: color(t, "93")
CYAN   = lambda t: color(t, "96")
BOLD   = lambda t: color(t, "1")

def print_result(r: BenchmarkResult):
    status = GREEN("✅ OK") if r.error is None else RED(f"❌ {r.error}")
    ttfb   = f"{r.ttfb_ms:.1f}ms" if r.ttfb_ms >= 0 else "N/A"
    print(
        f"  [{r.service:<12}] Round {r.round_num} | {ttfb:>8} TTFB | "
        f"{r.total_ms:>8.1f}ms total | {r.total_bytes:>6} bytes | {status}"
    )

def print_summary(summary: ServiceSummary):
    v = summary.valid
    if not v:
        print(f"  {RED('No valid results!')}")
        return

    print(f"  {'Metric':<20} {'Value':>10}")
    print(f"  {'-'*32}")
    print(f"  {'TTFB Min':<20} {summary.min_val('ttfb_ms'):>9.1f}ms")
    print(f"  {'TTFB Avg':<20} {summary.avg('ttfb_ms'):>9.1f}ms")
    print(f"  {'TTFB p50 (median)':<20} {summary.p50('ttfb_ms'):>9.1f}ms")
    print(f"  {'TTFB p95':<20} {summary.p95('ttfb_ms'):>9.1f}ms")
    print(f"  {'Total Avg':<20} {summary.avg('total_ms'):>9.1f}ms")
    print(f"  {'Connect Avg':<20} {summary.avg('connect_ms'):>9.1f}ms")
    print(f"  {'Success Rate':<20} {len(v):>8}/{len(summary.results)}")


# ─── Main ──────────────────────────────────────────────────────────────────────

async def run_benchmark():
    print(BOLD("\n=== TTS Latency Benchmark: Sarvam AI vs ElevenLabs ===\n"))

    if not SARVAM_API_KEY:
        print(RED("❌ SARVAM_API_KEY is not set in .env — skipping Sarvam tests."))
    if not ELEVENLABS_API_KEY:
        print(RED("❌ ELEVENLABS_API_KEY is not set in .env — skipping ElevenLabs tests."))
    if not SARVAM_API_KEY and not ELEVENLABS_API_KEY:
        return

    sarvam_summary   = ServiceSummary("Sarvam AI")
    elevenlabs_summary = ServiceSummary("ElevenLabs")

    for i, text in enumerate(TEST_TEXTS, 1):
        short_text = text[:50] + ("..." if len(text) > 50 else "")
        print(CYAN(f"\n📝 Text {i}: \"{short_text}\""))
        print(f"   Length: {len(text)} chars\n")

        for round_num in range(1, NUM_ROUNDS + 1):
            tasks = []
            if SARVAM_API_KEY:
                tasks.append(("sarvam", benchmark_sarvam(text, round_num)))
            if ELEVENLABS_API_KEY:
                tasks.append(("elevenlabs", benchmark_elevenlabs(text, round_num)))

            # Run both in parallel for each round
            results = await asyncio.gather(*(t[1] for t in tasks), return_exceptions=True)

            for (key, _), result in zip(tasks, results):
                if isinstance(result, Exception):
                    result = BenchmarkResult(
                        service=key, text=text, round_num=round_num,
                        connect_ms=0, ttfb_ms=-1, total_ms=0,
                        first_chunk_bytes=0, total_bytes=0,
                        error=str(result)
                    )
                if key == "sarvam":
                    sarvam_summary.results.append(result)
                else:
                    elevenlabs_summary.results.append(result)
                print_result(result)

            # Small delay between rounds to avoid rate limiting
            if round_num < NUM_ROUNDS:
                await asyncio.sleep(0.5)

    # ── Overall Summary ────────────────────────────────────────────────────────
    print(BOLD("\n" + "="*54))
    print(BOLD("   📊 OVERALL SUMMARY"))
    print(BOLD("="*54))

    summaries = []
    if SARVAM_API_KEY:
        print(BOLD(f"\n  🔵 Sarvam AI (bulbul:v3)"))
        print_summary(sarvam_summary)
        summaries.append(sarvam_summary)
    if ELEVENLABS_API_KEY:
        print(BOLD(f"\n  🟡 ElevenLabs (eleven_flash_v2_5)"))
        print_summary(elevenlabs_summary)
        summaries.append(elevenlabs_summary)

    # Winner
    if len(summaries) == 2:
        s_ttfb = sarvam_summary.avg("ttfb_ms")
        e_ttfb = elevenlabs_summary.avg("ttfb_ms")
        diff = abs(s_ttfb - e_ttfb)
        winner = "Sarvam AI" if s_ttfb < e_ttfb else "ElevenLabs"
        loser_ms = max(s_ttfb, e_ttfb)
        print(BOLD(f"\n  🏆 WINNER: {GREEN(winner)} is faster by {diff:.1f}ms avg TTFB"))
        print(f"     → Recommend using {GREEN(winner)} for your voice agent pipeline.\n")

        # Suitability check for 500-800ms pipeline budget
        print(BOLD("  🎯 Pipeline Budget Check (target: 500-800ms total)"))
        for s in summaries:
            ttfb = s.avg("ttfb_ms")
            flag = GREEN("✅ FITS") if ttfb < 400 else YELLOW("⚠️ TIGHT") if ttfb < 600 else RED("❌ OVER BUDGET")
            print(f"     {s.service:<15}: avg TTFB {ttfb:.1f}ms  {flag}")

    print()


if __name__ == "__main__":
    asyncio.run(run_benchmark())
