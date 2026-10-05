"""
STT Latency Benchmark: Sarvam AI vs Deepgram vs ElevenLabs
=============================================================
Records audio from your microphone and streams it to all three STT
services simultaneously, then measures Time-To-First-Transcript (TTFT).

Usage:
    python stt_benchmark.py
"""

import asyncio
import base64
import audioop
import io
import json
import os
import sys
import time
import threading
from urllib.parse import urlencode
import wave
from dataclasses import dataclass, field
from typing import List, Optional

import pyaudio
import websockets
from dotenv import load_dotenv

try:
    from deepgram import DeepgramClient, LiveOptions, LiveTranscriptionEvents
    DEEPGRAM_SDK_AVAILABLE = True
except ImportError:
    DEEPGRAM_SDK_AVAILABLE = False

load_dotenv()

# Ensure emoji/box drawing output works in Windows terminals.
if os.name == "nt":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# ─── Config ────────────────────────────────────────────────────────────────────
SARVAM_API_KEY     = os.environ.get("SARVAM_API_KEY", "")
DEEPGRAM_API_KEY   = os.environ.get("DEEPGRAM_API_KEY", "")
ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY", "")

SAMPLE_RATE  = 16000
CHANNELS     = 1
CHUNK_SIZE   = 1024
FORMAT       = pyaudio.paInt16
RECORD_SECS  = 4
NUM_ROUNDS   = 3
AUDIO_CHUNK  = 4096   # bytes per WebSocket send
MAX_SILENCE_AFTER_SPEECH_MS = 350
PRE_SPEECH_TIMEOUT_MS = 1800
MIN_SPEECH_MS = 180
EOS_TAIL_SEND_MS = 220

# ─── Colors ────────────────────────────────────────────────────────────────────
def clr(t, c): return f"\033[{c}m{t}\033[0m"
GREEN  = lambda t: clr(t, "92");  RED    = lambda t: clr(t, "91")
YELLOW = lambda t: clr(t, "93");  CYAN   = lambda t: clr(t, "96")
BOLD   = lambda t: clr(t, "1");   DIM    = lambda t: clr(t, "2")

# ─── Data ──────────────────────────────────────────────────────────────────────
@dataclass
class STTResult:
    service: str
    round_num: int
    ttft_ms: float
    vad_detect_ms: float
    vad_to_final_ms: float
    total_ms: float
    transcript: str
    error: Optional[str] = None

@dataclass
class Summary:
    service: str
    results: List[STTResult] = field(default_factory=list)
    @property
    def valid(self): return [r for r in self.results if r.error is None]
    def avg(self, a): v=[getattr(r,a) for r in self.valid]; return sum(v)/len(v) if v else float("inf")
    def best(self, a): v=[getattr(r,a) for r in self.valid]; return min(v) if v else float("inf")


def is_retryable_error(err: Optional[str]) -> bool:
    if not err:
        return False
    e = err.lower()
    return any(token in e for token in (
        "timeout",
        "no transcript received",
        "insufficient_audio_activity",
        "commit_throttled",
        "connection closed",
    ))


async def run_with_retry(fn, pcm: bytes, round_num: int, retries: int = 1) -> STTResult:
    eos_ms = detect_end_of_speech_ms(pcm)
    pcm_for_send = trim_pcm_after_eos(pcm, eos_ms, EOS_TAIL_SEND_MS)
    eos_ms_for_send = min(eos_ms, len(pcm_for_send) / (SAMPLE_RATE * CHANNELS * 2) * 1000.0)
    result = await fn(pcm_for_send, round_num, eos_ms_for_send)
    attempt = 0
    while attempt < retries and (result.error and is_retryable_error(result.error)):
        await asyncio.sleep(0.35)
        retry_result = await fn(pcm_for_send, round_num, eos_ms_for_send)
        # Keep the first success immediately, otherwise keep the latest error.
        if not retry_result.error:
            return retry_result
        result = retry_result
        attempt += 1
    return result

# ─── Helpers ───────────────────────────────────────────────────────────────────
def record_audio(seconds: int) -> bytes:
    pa = pyaudio.PyAudio()
    stream = pa.open(format=FORMAT, channels=CHANNELS,
                     rate=SAMPLE_RATE, input=True, frames_per_buffer=CHUNK_SIZE)
    print(CYAN(f"\n  🎤 Recording for {seconds}s... speak now!"), flush=True)
    frames = [stream.read(CHUNK_SIZE, exception_on_overflow=False)
              for _ in range(int(SAMPLE_RATE / CHUNK_SIZE * seconds))]
    print(DIM("  ⏹  Done recording."), flush=True)
    stream.stop_stream(); stream.close(); pa.terminate()
    return b"".join(frames)


def record_audio_until_vad(max_seconds: int) -> bytes:
    """Record mic audio and stop soon after local end-of-speech is detected."""
    pa = pyaudio.PyAudio()
    stream = pa.open(format=FORMAT, channels=CHANNELS,
                     rate=SAMPLE_RATE, input=True, frames_per_buffer=CHUNK_SIZE)
    print(CYAN(f"\n  🎤 Recording (VAD) up to {max_seconds}s... speak now!"), flush=True)

    max_frames = int(SAMPLE_RATE / CHUNK_SIZE * max_seconds)
    chunk_ms = int((CHUNK_SIZE / SAMPLE_RATE) * 1000)
    frames: List[bytes] = []

    noise_window: List[int] = []
    in_speech = False
    speech_ms = 0
    silence_ms = 0
    pre_speech_ms = 0

    for _ in range(max_frames):
        chunk = stream.read(CHUNK_SIZE, exception_on_overflow=False)
        frames.append(chunk)

        rms = audioop.rms(chunk, 2)
        noise_window.append(rms)
        if len(noise_window) > max(4, int(300 / max(1, chunk_ms))):
            noise_window.pop(0)
        noise_floor = max(80, int(sum(noise_window) / len(noise_window)))
        speech_threshold = max(220, int(noise_floor * 2.5))

        if rms >= speech_threshold:
            in_speech = True
            speech_ms += chunk_ms
            silence_ms = 0
        else:
            if in_speech:
                silence_ms += chunk_ms
            else:
                pre_speech_ms += chunk_ms

        if not in_speech and pre_speech_ms >= PRE_SPEECH_TIMEOUT_MS:
            # No voice detected yet; keep recording until max or user starts speech.
            pre_speech_ms = 0
        if in_speech and speech_ms >= MIN_SPEECH_MS and silence_ms >= MAX_SILENCE_AFTER_SPEECH_MS:
            break

    print(DIM("  ⏹  VAD stop."), flush=True)
    stream.stop_stream(); stream.close(); pa.terminate()
    return b"".join(frames)

def pcm_to_wav(pcm: bytes) -> bytes:
    """Wrap raw PCM into a WAV container."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(2)          # 16-bit = 2 bytes
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(pcm)
    return buf.getvalue()


def chunk_audio_ms(byte_len: int) -> float:
    bytes_per_sample = 2  # 16-bit PCM
    return (byte_len / (SAMPLE_RATE * CHANNELS * bytes_per_sample)) * 1000.0


def detect_end_of_speech_ms(pcm: bytes, frame_ms: int = 20) -> float:
    """Estimate end-of-speech inside recorded PCM using RMS VAD."""
    if not pcm:
        return 0.0
    bytes_per_sample = 2
    frame_bytes = max(1, int(SAMPLE_RATE * CHANNELS * bytes_per_sample * frame_ms / 1000))
    frames = [pcm[i: i + frame_bytes] for i in range(0, len(pcm), frame_bytes) if pcm[i: i + frame_bytes]]
    if not frames:
        return 0.0

    rms_values = [audioop.rms(frame, 2) for frame in frames]
    noise_probe = max(1, min(len(rms_values), int(300 / frame_ms)))
    noise_floor = max(80, int(sum(rms_values[:noise_probe]) / noise_probe))
    speech_threshold = max(220, int(noise_floor * 2.5))

    silence_hold_ms = 320
    min_speech_ms = 160
    silence_ms = 0
    speech_ms = 0
    last_speech_end_ms = 0.0
    in_speech = False

    for idx, rms in enumerate(rms_values):
        t_end_ms = (idx + 1) * frame_ms
        if rms >= speech_threshold:
            in_speech = True
            speech_ms += frame_ms
            silence_ms = 0
            last_speech_end_ms = float(t_end_ms)
        elif in_speech:
            silence_ms += frame_ms
            if speech_ms >= min_speech_ms and silence_ms >= silence_hold_ms:
                break

    if last_speech_end_ms > 0:
        return last_speech_end_ms
    return float(len(pcm) / (SAMPLE_RATE * CHANNELS * bytes_per_sample) * 1000.0)


def trim_pcm_after_eos(pcm: bytes, eos_ms: float, tail_ms: int = EOS_TAIL_SEND_MS) -> bytes:
    if not pcm:
        return pcm
    total_ms = len(pcm) / (SAMPLE_RATE * CHANNELS * 2) * 1000.0
    keep_ms = min(total_ms, max(eos_ms + tail_ms, 120.0))
    keep_bytes = int((keep_ms / 1000.0) * SAMPLE_RATE * CHANNELS * 2)
    keep_bytes = max(2, min(len(pcm), keep_bytes))
    return pcm[:keep_bytes]


async def stream_mic_to_queues(queues, seconds: int):
    """Capture mic audio once and broadcast chunks live to all provider queues."""
    loop = asyncio.get_running_loop()
    done = asyncio.Event()

    def _worker():
        pa = pyaudio.PyAudio()
        stream = pa.open(format=FORMAT, channels=CHANNELS,
                         rate=SAMPLE_RATE, input=True, frames_per_buffer=CHUNK_SIZE)
        try:
            print(CYAN(f"\n  🎤 Live streaming mic for up to {seconds}s... speak now!"), flush=True)
            total = int(SAMPLE_RATE / CHUNK_SIZE * seconds)
            for _ in range(total):
                chunk = stream.read(CHUNK_SIZE, exception_on_overflow=False)
                for q in queues:
                    loop.call_soon_threadsafe(q.put_nowait, chunk)
            print(DIM("  ⏹  Live capture done."), flush=True)
        finally:
            stream.stop_stream()
            stream.close()
            pa.terminate()
            for q in queues:
                loop.call_soon_threadsafe(q.put_nowait, None)
            loop.call_soon_threadsafe(done.set)

    threading.Thread(target=_worker, daemon=True).start()
    await done.wait()


# ══════════════════════════════════════════════════════════════════════════════
# DEEPGRAM — SDK first, with raw WebSocket fallback when SDK is missing
# ══════════════════════════════════════════════════════════════════════════════
async def bench_deepgram_ws(pcm: bytes, round_num: int, eos_ms: float) -> STTResult:
    params = {
        "model": "nova-2",
        "language": "hi",
        "encoding": "linear16",
        "sample_rate": SAMPLE_RATE,
        "channels": CHANNELS,
        "interim_results": "true",
        "smart_format": "true",
        "punctuate": "true",
    }
    url = f"wss://api.deepgram.com/v1/listen?{urlencode(params)}"

    transcript = ""
    ttft_ms = -1.0
    final_after_eos_ms = -1.0
    error = None
    t_start = time.perf_counter()
    t_last_text_ms = -1.0
    t_final_ms = -1.0

    try:
        async with websockets.connect(
            url,
            extra_headers={"Authorization": f"Token {DEEPGRAM_API_KEY}"},
            open_timeout=10,
        ) as ws:
            for i in range(0, len(pcm), AUDIO_CHUNK):
                chunk = pcm[i: i + AUDIO_CHUNK]
                await ws.send(chunk)
                await asyncio.sleep(chunk_audio_ms(len(chunk)) / 1000.0)

            # Ask Deepgram to finalize buffered audio.
            await ws.send(json.dumps({"type": "CloseStream"}))

            while True:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=12.0)
                except asyncio.TimeoutError:
                    if transcript:
                        break
                    error = "Receive timeout"
                    break
                except websockets.exceptions.ConnectionClosed:
                    break

                if isinstance(raw, bytes):
                    continue

                try:
                    payload = json.loads(raw)
                except Exception:
                    continue

                msg_type = payload.get("type", "")
                channel = payload.get("channel", {})
                alts = channel.get("alternatives", []) if isinstance(channel, dict) else []
                text = ""
                if alts:
                    text = (alts[0].get("transcript") or "").strip()

                if text and ttft_ms < 0:
                    ttft_ms = (time.perf_counter() - t_start) * 1000
                if text:
                    transcript = text
                    t_last_text_ms = (time.perf_counter() - t_start) * 1000

                if msg_type == "Results" and payload.get("speech_final"):
                    t_final_ms = (time.perf_counter() - t_start) * 1000
                    break
                if msg_type == "Metadata":
                    if transcript:
                        t_final_ms = (time.perf_counter() - t_start) * 1000
                    break
                if msg_type in ("Error", "error"):
                    error = payload.get("description") or payload.get("error") or str(payload)
                    break
    except Exception as e:
        error = str(e)

    total_ms = (time.perf_counter() - t_start) * 1000
    if t_final_ms < 0 and t_last_text_ms >= 0:
        t_final_ms = t_last_text_ms
    if t_final_ms >= 0 and eos_ms >= 0:
        final_after_eos_ms = max(0.0, t_final_ms - eos_ms)
    if not transcript and not error:
        error = "No transcript received"
    if error or not transcript:
        final_after_eos_ms = -1.0
    vad_detect_ms = eos_ms if transcript and not error else -1.0
    return STTResult("Deepgram", round_num, ttft_ms, vad_detect_ms, final_after_eos_ms, total_ms, transcript, error)


async def bench_deepgram(pcm: bytes, round_num: int, eos_ms: float) -> STTResult:
    if not DEEPGRAM_SDK_AVAILABLE:
        return await bench_deepgram_ws(pcm, round_num, eos_ms)
    transcript = ""
    ttft_ms = -1.0
    final_after_eos_ms = -1.0
    error = None
    t_start = time.perf_counter()
    done_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    t_last_text_ms = -1.0
    t_final_ms = -1.0

    try:
        client = DeepgramClient(DEEPGRAM_API_KEY)
        dg_conn = client.listen.live.v("1")

        def on_message(self_inner, result, **kwargs):
            nonlocal transcript, ttft_ms, t_last_text_ms, t_final_ms
            if not result.channel or not result.channel.alternatives:
                return
            text = result.channel.alternatives[0].transcript.strip()
            speech_final = getattr(result, "speech_final", False)
            if text and ttft_ms < 0:
                ttft_ms = (time.perf_counter() - t_start) * 1000
            if text:
                transcript = text
                t_last_text_ms = (time.perf_counter() - t_start) * 1000
            if speech_final:
                t_final_ms = (time.perf_counter() - t_start) * 1000
                loop.call_soon_threadsafe(done_event.set)

        def on_error(self_inner, err, **kwargs):
            nonlocal error
            error = str(err)
            loop.call_soon_threadsafe(done_event.set)

        def on_close(self_inner, close, **kwargs):
            loop.call_soon_threadsafe(done_event.set)

        dg_conn.on(LiveTranscriptionEvents.Transcript, on_message)
        dg_conn.on(LiveTranscriptionEvents.Error, on_error)
        dg_conn.on(LiveTranscriptionEvents.Close, on_close)

        options = LiveOptions(
            model="nova-2",
            language="hi",
            encoding="linear16",
            sample_rate=SAMPLE_RATE,
            channels=CHANNELS,
            interim_results=True,
            endpointing="50",
            utterance_end_ms="500",
            vad_events=True,
        )

        if dg_conn.start(options) is False:
            return STTResult("Deepgram", round_num, -1, -1, -1, 0, "", "Failed to start connection")

        # Stream PCM in a background thread (SDK is sync)
        def _send_audio():
            for i in range(0, len(pcm), AUDIO_CHUNK):
                chunk = pcm[i: i + AUDIO_CHUNK]
                dg_conn.send(chunk)
                time.sleep(chunk_audio_ms(len(chunk)) / 1000.0)
            dg_conn.finish()

        threading.Thread(target=_send_audio, daemon=True).start()

        # Wait for transcript or timeout
        try:
            await asyncio.wait_for(done_event.wait(), timeout=15.0)
        except asyncio.TimeoutError:
            error = "Receive timeout"
            dg_conn.finish()

    except ImportError:
        error = "deepgram-sdk not installed — run: pip install deepgram-sdk"
    except Exception as e:
        error = str(e)

    total_ms = (time.perf_counter() - t_start) * 1000
    if t_final_ms < 0 and t_last_text_ms >= 0:
        t_final_ms = t_last_text_ms
    if t_final_ms >= 0 and eos_ms >= 0:
        final_after_eos_ms = max(0.0, t_final_ms - eos_ms)
    vad_detect_ms = eos_ms if transcript and not error else -1.0
    return STTResult("Deepgram", round_num, ttft_ms, vad_detect_ms, final_after_eos_ms, total_ms, transcript, error)


async def bench_deepgram_live(round_num: int, audio_q) -> STTResult:
    params = {
        "model": "nova-2",
        "language": "hi",
        "encoding": "linear16",
        "sample_rate": SAMPLE_RATE,
        "channels": CHANNELS,
        "interim_results": "true",
        "utterance_end_ms": "1000",
        "endpointing": "50",
        "smart_format": "true",
        "punctuate": "true",
    }
    url = f"wss://api.deepgram.com/v1/listen?{urlencode(params)}"
    transcript = ""
    ttft_ms = -1.0
    vad_detect_ms = -1.0
    vad_to_final_ms = -1.0
    error = None
    t_start = time.perf_counter()
    final_ms = -1.0

    try:
        async with websockets.connect(url, extra_headers={"Authorization": f"Token {DEEPGRAM_API_KEY}"}, open_timeout=10) as ws:
            async def sender():
                while True:
                    chunk = await audio_q.get()
                    if chunk is None:
                        await ws.send(json.dumps({"type": "CloseStream"}))
                        break
                    await ws.send(chunk)

            send_task = asyncio.create_task(sender())
            while True:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=15.0)
                except asyncio.TimeoutError:
                    error = "Receive timeout"
                    break
                except websockets.exceptions.ConnectionClosed:
                    break
                if isinstance(raw, bytes):
                    continue
                payload = json.loads(raw)
                msg_type = payload.get("type", "")
                alts = payload.get("channel", {}).get("alternatives", [])
                text = (alts[0].get("transcript") if alts else "") or ""
                text = text.strip()
                if text and ttft_ms < 0:
                    ttft_ms = (time.perf_counter() - t_start) * 1000
                if text:
                    transcript = text
                if msg_type == "UtteranceEnd" and vad_detect_ms < 0:
                    # Provider VAD-like end-of-utterance marker (audio timeline).
                    last_word_end = payload.get("last_word_end")
                    if isinstance(last_word_end, (int, float)):
                        vad_detect_ms = max(0.0, float(last_word_end) * 1000.0)
                    else:
                        vad_detect_ms = (time.perf_counter() - t_start) * 1000
                if msg_type == "Results" and payload.get("speech_final"):
                    final_ms = (time.perf_counter() - t_start) * 1000
                    if vad_detect_ms < 0:
                        vad_detect_ms = final_ms
                    break
                if msg_type == "Metadata":
                    if transcript:
                        final_ms = (time.perf_counter() - t_start) * 1000
                        if vad_detect_ms < 0:
                            vad_detect_ms = final_ms
                    break
                if msg_type in ("Error", "error"):
                    error = payload.get("description") or payload.get("error") or str(payload)
                    break
            await send_task
    except Exception as e:
        error = str(e)

    total_ms = (time.perf_counter() - t_start) * 1000
    if not transcript and not error:
        error = "No transcript received"
    if vad_detect_ms >= 0 and final_ms >= 0:
        vad_to_final_ms = max(0.0, final_ms - vad_detect_ms)
    if error or not transcript:
        vad_detect_ms = -1.0
        vad_to_final_ms = -1.0
    return STTResult("Deepgram", round_num, ttft_ms, vad_detect_ms, vad_to_final_ms, total_ms, transcript, error)


# ─── Reporter ──────────────────────────────────────────────────────────────────
def print_result(r: STTResult):
    err_str = str(r.error) if r.error else ""
    status  = GREEN("✅ OK") if not r.error else RED(f"❌ {err_str[:70]}")
    ttft    = f"{r.ttft_ms:.0f}ms" if r.ttft_ms >= 0 else "N/A "
    vad_ms = f"{r.vad_detect_ms:.0f}ms" if r.vad_detect_ms >= 0 else "N/A "
    post_eos = f"{r.vad_to_final_ms:.0f}ms" if r.vad_to_final_ms >= 0 else "N/A "
    snippet = (r.transcript[:50] + "...") if len(r.transcript) > 50 else r.transcript or "-"
    print(f"  [{r.service:<12}] Round {r.round_num} | TTFT {ttft:>7} | VAD {vad_ms:>7} | VAD→Final {post_eos:>7} | "
          f"Total {r.total_ms:>7.0f}ms | \"{snippet}\" | {status}")

def print_summary(s: Summary):
    if not s.valid: print(f"  {RED('No valid results!')}"); return
    for label, val in [
        ("Best TTFT",  f"{s.best('ttft_ms'):.0f}ms"),
        ("Avg  TTFT",  f"{s.avg('ttft_ms'):.0f}ms"),
        ("Avg VAD detect", f"{s.avg('vad_detect_ms'):.0f}ms"),
        ("Avg VAD→Final", f"{s.avg('vad_to_final_ms'):.0f}ms"),
        ("Avg  Total", f"{s.avg('total_ms'):.0f}ms"),
        ("Success",    f"{len(s.valid)}/{len(s.results)}"),
    ]:
        print(f"  {label:<14} {val}")

# ─── Main ──────────────────────────────────────────────────────────────────────
SERVICES = [
        ("deepgram",   DEEPGRAM_API_KEY,   "Deepgram",   bench_deepgram_live),
    ]

async def run():
    print(BOLD("\n=== STT Latency Benchmark: Sarvam vs Deepgram vs ElevenLabs ===\n"))

    active = [(k, key, name, fn) for k, key, name, fn in SERVICES if key]
    missing = [name for k, key, name, fn in SERVICES if not key]
    if missing: print(YELLOW(f"⚠️  No API key: {', '.join(missing)}"))
    if not active: print(RED("❌ No API keys found!")); return

    summaries = {k: Summary(name) for k, _, name, _ in active}

    for round_num in range(1, NUM_ROUNDS + 1):
        print(BOLD(f"\n━━━ Round {round_num}/{NUM_ROUNDS} ━━━"))
        print(CYAN("  ⚡ Live streaming to all providers simultaneously..."))
        queues = [asyncio.Queue(maxsize=128) for _ in active]
        recv_tasks = [asyncio.create_task(fn(round_num, q)) for (_, _, _, fn), q in zip(active, queues)]
        await stream_mic_to_queues(queues, RECORD_SECS)
        results = await asyncio.gather(*recv_tasks, return_exceptions=True)
        for (k, _, name, _), result in zip(active, results):
            if isinstance(result, Exception):
                result = STTResult(name, round_num, -1, -1, -1, 0, "", str(result))
            summaries[k].results.append(result)
            print_result(result)

        if round_num < NUM_ROUNDS:
            print(DIM("  Waiting 2s...")); await asyncio.sleep(2)

    print(BOLD("\n" + "="*60))
    print(BOLD("  📊 OVERALL STT LATENCY SUMMARY"))
    print(BOLD("="*60))

    icons = {"sarvam": "🔵", "deepgram": "🟢", "elevenlabs": "🟡"}
    valid_summaries = []
    for k, _, _, _ in active:
        s = summaries[k]
        print(BOLD(f"\n  {icons.get(k,'▪')} {s.service}"))
        print_summary(s)
        if s.valid: valid_summaries.append(s)

    if len(valid_summaries) >= 2:
        winner = min(valid_summaries, key=lambda s: s.avg("vad_to_final_ms"))
        print(BOLD(f"\n  🏆 FASTEST FINAL AFTER VAD: {GREEN(winner.service)} (avg VAD→Final {winner.avg('vad_to_final_ms'):.0f}ms)"))
        print(BOLD("\n  🎯 Budget Check (target: <250ms after provider VAD)"))
        for s in valid_summaries:
            avg = s.avg("vad_to_final_ms")
            flag = GREEN("✅ EXCELLENT") if avg<250 else YELLOW("⚠️ PASSABLE") if avg<500 else RED("❌ SLOW")
            print(f"     {s.service:<14} avg VAD→Final {avg:>6.0f}ms  {flag}")
    print()

if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("\nInterrupted.")
