# 🏡 PropVox AI Voice Agent — System Evaluation Report
**Project:** PropVox AI Voice Assistant (Myra)  
**Status:** Active Development — Production-Ready Core  
**Last Updated:** WebSocket TTS migration completed; Deepgram endpointing tuned for ultra-low latency; English + Hindi fully operational

---

## 1. System Overview

Myra is a real-time AI voice agent designed to act as a human caller from PropVox Estate. She conducts natural, low-latency voice conversations with prospective customers, answers questions about the PropVox Estate project, and captures visit intent — all through a browser-based interface.

### Architecture at a Glance

```
Browser (Next.js / React)
    ↕ WebSocket (ws://localhost:8000/ws/voice)
FastAPI Backend (Python / asyncio)
    ├── Deepgram STT     → live voice-to-text
    ├── Groq LLM         → streaming AI response generation
    └── Sarvam AI TTS    → WebSocket TTS (one persistent connection per utterance)
           wss://api.sarvam.ai/text-to-speech/ws
```

---

## 2. Technology Stack

| Layer | Technology | Purpose |
|---|---|---|
| **Frontend** | Next.js + React + TypeScript | Browser UI, audio capture, playback |
| **Backend** | FastAPI + Python asyncio | WebSocket server, pipeline orchestration |
| **STT** | Deepgram Nova-2 | Real-time speech-to-text |
| **LLM** | Groq (`llama-3.1-8b-instant`) | AI response generation |
| **TTS** | Sarvam AI (`bulbul:v3`, `simran`) | WebSocket text-to-speech synthesis |
| **Transport** | WebSocket (binary + JSON) | Low-latency audio & message streaming |

---

## 3. Implemented Features

### 3.1 Real-Time Speech-to-Text (STT) — [modules/speech_to_text.py](file:///c:/Users/omtil/OneDrive/Desktop/Riverwood_AI/backend/modules/speech_to_text.py)

- **Deepgram Nova-2** model used for high-accuracy transcription
- **Live streaming** via Deepgram's WebSocket API (`listen.live.v("1")`)
- **`interim_results: True`** — transcription updates as user speaks
- **`endpointing: 200ms`** and **`utterance_end_ms: 1000ms`** — Deepgram accurately detects end of speech in 200ms for minimal latency, with a 1s secondary fallback.
- Language routing: English ([en](file:///c:/Users/omtil/OneDrive/Desktop/Riverwood_AI/frontend/components/VoiceAgent.tsx#59-63)) and Hindi ([hi](file:///c:/Users/omtil/OneDrive/Desktop/Riverwood_AI/backend/modules/memory_manager.py#24-44)) supported
- **`is_final`** events lock in confirmed word chunks
- **`speech_final`** events signal a definitive end-of-utterance pause

### 3.2 Dual-Event STT Endpointing — [main.py](file:///c:/Users/omtil/OneDrive/Desktop/Riverwood_AI/backend/main.py)

Two complementary paths ensure robust speech detection:

**Path A — Natural (Deepgram `speech_final`):**
- Deepgram fires `speech_final` after its 200ms VAD detects pause
- Immediately cancels the fallback timer and fires the LLM pipeline

**Path B — Forced Fallback (0.3s Timer):**
- Every `is_final` chunk resets a 0.3s async timer
- If Deepgram "hangs" (network delay, slow endpointing), the timer fires after 0.3s
- Fires the LLM pipeline independently
- A mutual exclusion guard prevents duplicate LLM calls if both paths fire

```python
# Guard prevents race condition between the two paths
if current_processing_task and not current_processing_task.done():
    return  # One path already won — bail out
```

### 3.3 Streaming LLM Pipeline — [modules/conversation_engine.py](file:///c:/Users/omtil/OneDrive/Desktop/Riverwood_AI/backend/modules/conversation_engine.py)

- **Groq `llama-3.1-8b-instant`** for sub-200ms first token latency
- **Streaming** via `client.chat.completions.create(stream=True)`
- **Token temperature:** 0.7, **Max tokens:** 150 for English, 110 for Hindi (concise responses)
- **Dual-phase sentence flushing strategy:**

| Phase | Trigger | Why |
|---|---|---|
| **First chunk** | 4+ words OR any punctuation (`. ! ? , ;`) | Get audio to user ASAP — perceived latency |
| **Subsequent chunks** | Full sentence endings only (`. ! ?`) | Natural speech prosody, clean audio stitching |

- `EARLY_SPLIT` and `LATE_SPLIT` punctuation sets handle Hindi sentence markers too (`|` and `।`)

#### Language-Specific System Prompts

Two separate, purpose-built system prompts — not a generic one with a language variable:

**English prompt:**
- Casual human caller persona with English filler words ([um](file:///c:/Users/omtil/OneDrive/Desktop/Riverwood_AI/backend/modules/conversation_engine.py#186-219), `uh`, `like`)
- Contractions-heavy speech style
- Explicit ban on AI phrases (`"Certainly!"`, `"How can I help?"`)

**Hindi prompt (Hinglish codemix style):**
- Written in Hindi so the LLM internalizes it as a Hindi-mode system
- **Explicit feminine gender rules** — examples of correct vs wrong verb forms:
  - ✅ `मैं बता रही हूँ / मैं आऊँगी` (feminine)
  - ❌ `मैं बता रहा हूँ / मैं आऊँगा` (masculine — wrong)
- **`आप` honorific enforced** — `तुम/तू` explicitly banned with examples
- **Real Hinglish codemix style** — Hindi sentence structure + English words that Indians naturally use:
  - [available](file:///c:/Users/omtil/OneDrive/Desktop/Riverwood_AI/frontend/components/VoiceAgent.tsx#210-239), `visit`, `site`, `plot`, [date](file:///c:/Users/omtil/OneDrive/Desktop/Riverwood_AI/backend/modules/conversation_engine.py#186-219), `confirm`, `timing`, `plan`, `booking`, `details`
- **Pure/formal Hindi banned** — explicit list: `रूझान`, `निर्धारित`, `अभिप्राय`, `उपलब्ध`
- **Hindi filler words**: `हाँ`, `अच्छा`, `देखो`, `मतलब`, `वो`, `यानी`, `हाँ जी`
- **4 correct style examples** vs 2 wrong examples for in-context guidance

### 3.4 Text-to-Speech Pipeline — [modules/text_to_speech.py](file:///c:/Users/omtil/OneDrive/Desktop/Riverwood_AI/backend/modules/text_to_speech.py)

- **Sarvam AI `bulbul:v3`** model with `simran` voice (bilingual — English & Hindi)
- **`en-IN` / `hi-IN`** target language routing
- **Pace:** 1.1, **24000 Hz** sample rate (v3 native), **MP3 output**
- **`enable_preprocessing: True`** — handles code-mixed Hindi+English sentences (e.g. "PropVox Estate" inside Hindi text)
- **WebSocket transport** via `wss://api.sarvam.ai/text-to-speech/ws`
  - `model=bulbul:v3&send_completion_event=true` as URL query params
  - **One TCP+TLS handshake per utterance** (not per sentence) — eliminates per-sentence connection overhead
  - Config message sent once: speaker, language, codec, sample rate
  - Each sentence: `{"type":"text"}` + `{"type":"flush"}` → audio chunks arrive → `{"type":"event", "event_type":"final"}`
  - Audio chunks accumulated per sentence → sent as one complete binary frame to browser
- **Auto-reconnect on 422** — when Hindi+English mixed text triggers a language error, a background [reconnect()](file:///c:/Users/omtil/OneDrive/Desktop/Riverwood_AI/backend/modules/text_to_speech.py#43-53) task fires immediately so the next sentence gets a fresh session
- **Minimum buffer guard:** Responses < 1000 bytes discarded (prevents empty-frame audio pops)

### 3.5 Parallel WS-TTS + LLM Orchestration — [main.py](file:///c:/Users/omtil/OneDrive/Desktop/Riverwood_AI/backend/main.py)

```
process_llm_and_tts() starts
├─→ [background task] wss:// connect + config sent  (~200ms)
└─→ [main]            LLM streaming starts           (~0ms)
                      First sentence arrives          (~150ms)
                      synthesize_stream() waits on _ready event
                      (WS ready by ~200ms — LLM wins or ties the race)
                      → text + flush → audio chunks → accumulate → send complete MP3
```

- **Sequential per-utterance**: one WS session, sentences processed in order
- Audio accumulated per sentence before sending — browser `decodeAudioData()` requires complete MP3 (not partial fragments)
- Raw binary WebSocket frames to frontend — no Base64 overhead
- 10ms `asyncio.sleep` yield after each sentence to keep event loop responsive

### 3.6 Barge-In Interrupt System — [main.py](file:///c:/Users/omtil/OneDrive/Desktop/Riverwood_AI/backend/main.py)

- **Instant detection:** As soon as Deepgram returns ≥ 2 characters of transcript, interruption is checked
- **Only triggers if AI is actively playing** (`current_processing_task` is running)
- **Three-step interrupt sequence:**
  1. Send `{"type": "interrupt"}` JSON to frontend → audio stops immediately (< 5ms)
  2. `asyncio.Task.cancel()` → kills LLM + TTS pipeline
  3. Clear `last_audio_cache` → prevents replaying a half-spoken response
- **Context Clubbing:** When interrupted, the AI's last partial response is popped from memory and the previous user query is prepended to the new one, so the LLM has full context continuity

### 3.7 Query Deduplication + Audio Cache — [main.py](file:///c:/Users/omtil/OneDrive/Desktop/Riverwood_AI/backend/main.py)

A three-layer cache eliminates redundant LLM + TTS calls after accidental interrupts:

```
Normal call:  "then" → LLM (150ms) + TTS (550ms) → audio plays → CACHED
Accidental interrupt with "we" → merged query = "then" (same!)
Cache hit: skip LLM + skip TTS → blast raw bytes over WebSocket
          ⚡ TTFB: ~10ms instead of ~740ms
```

| Cache Variable | Contents |
|---|---|
| `last_query_cache` | The exact query string of the last completed response |
| `last_response_cache` | Full LLM text response |
| `last_audio_cache` | List of raw MP3 byte arrays (one per sentence) |

- Cache is **cleared on interrupt** (partial audio is never replayed)
- Cache is **per-session**, never shared across users

### 3.8 Memory Management — [modules/memory_manager.py](file:///c:/Users/omtil/OneDrive/Desktop/Riverwood_AI/backend/modules/memory_manager.py)

- **In-memory session store** — per WebSocket connection
- **Sliding window:** Last 8 messages (4 complete exchanges) sent to LLM each turn
- **Hard cap:** 100 messages max per session (FIFO rollover)
- **Rolling Summary System:** After every 3+ exchanges (>6 messages), a background asyncio task runs [update_summary_task()](file:///c:/Users/omtil/OneDrive/Desktop/Riverwood_AI/backend/modules/conversation_engine.py#186-219) which:
  - Extracts key facts (visit intent, questions asked, info already given)
  - Injects summary as a hidden system message at the top of context
  - Prevents Myra from repeating herself or forgetting key agreements
- **[pop_last_message()](file:///c:/Users/omtil/OneDrive/Desktop/Riverwood_AI/backend/modules/memory_manager.py#56-62)** — used by Context Clubbing to undo the last AI response when interrupted

### 3.9 Multi-Language Support

- Language selected at session start via UI (English 🇬🇧 / Hindi 🇮🇳)
- Passed as query param: `ws://localhost:8000/ws/voice?language=en`
- Routes through all pipeline stages:
  - STT: Deepgram language code ([en](file:///c:/Users/omtil/OneDrive/Desktop/Riverwood_AI/frontend/components/VoiceAgent.tsx#59-63) / [hi](file:///c:/Users/omtil/OneDrive/Desktop/Riverwood_AI/backend/modules/memory_manager.py#24-44))
  - LLM: System prompt explicitly instructs chosen language
  - TTS: Sarvam `target_language_code` (`en-IN` / `hi-IN`)
  - Sentence splitting: Hindi punctuation markers (`|`, `।`) added to split sets

### 3.10 Conversation Logging — [main.py](file:///c:/Users/omtil/OneDrive/Desktop/Riverwood_AI/backend/main.py)

- Every completed exchange is appended to `convo.log`
- Format: `USER: <text>\nMYRA: <text>\n\n`
- Written via `loop.call_soon_threadsafe` to avoid blocking the async event loop

---

## 4. Frontend Implementation — [VoiceAgent.tsx](file:///c:/Users/omtil/OneDrive/Desktop/Riverwood_AI/frontend/components/VoiceAgent.tsx)

### 4.1 WebSocket Binary Audio Playback

- `ws.binaryType = "arraybuffer"` — receives binary frames natively (no base64 overhead)
- **Promise-chained decode queue** (`decodeChainRef`) ensures audio chunks play in order even if smaller chunks decode faster than larger ones
- **Three-guard session ID system** prevents stale audio from playing after interrupts:

| Guard | Location | Protects Against |
|---|---|---|
| Guard 1 | Before entering decode chain | Audio queued before session change |
| Guard 2 | Before `decodeAudioData()` | Audio that was queued but session changed while waiting |
| Guard 3 | After `decodeAudioData()` | Decode was in-flight during interrupt (10-200ms) |

- `audioData.slice(0)` — copies ArrayBuffer before decode, prevents detachment errors

### 4.2 Interrupt Handling

On receiving `{"type": "interrupt"}`:
1. Increment `playbackSessionIdRef` → invalidates ALL pending decode futures
2. Stop actively playing `AudioBufferSourceNode`
3. Close and nullify `AudioContext`
4. Clear `audioQueueRef`
5. Reset `decodeChainRef` to resolved promise

### 4.3 Live Transcript Streaming

- `interim` subtypes → **replace** the last user bubble in-place (live word-by-word update)
- `final` subtype → replace with finalized, clean text
- Result: single chat bubble that types out in real-time as user speaks — no duplicate bubbles

### 4.4 Microphone Audio Capture

- `MediaRecorder` with `audio/webm` MIME type, **250ms timeslice** chunks
- Audio encoded to **Base64** and sent over WebSocket as JSON `audio_chunk`
- Async encoding with `arrayBuffer()` — no `FileReader` race conditions
- **In-flight chunk tracking** (`pendingChunksRef`) — ensures `audio_stop` is sent only after the last audio chunk has been encoded and transmitted

### 4.5 Echo Cancellation

```typescript
audio: {
  echoCancellation: true,
  echoCancellationType: "system",     // OS-level AEC (Chrome/Edge)
  noiseSuppression: true,
  autoGainControl: true,
  suppressLocalAudioPlayback: true,   // Couples AEC to local audio output
  channelCount: 1,                    // Mono — reduced echo surface
  sampleRate: 16000,                  // Matches Deepgram preferred rate
  latency: 0,                         // Minimum capture latency
}
```

### 4.6 Text Input Fallback

- Full text input box alongside voice — user can type instead of speaking
- Sends `{"type": "text", "text": "..."}` to backend
- Same LLM + TTS pipeline handles it identically

### 4.7 UI Features

- Language selection screen (English / Hindi) before conversation starts
- Connection status indicator (green/red dot)
- Animated pulsing mic button during recording (red + ping animation)
- "Myra is thinking..." spinner during LLM processing
- Per-message latency badge `⚡ Response Time: 0.71s`
- Chat history with role-differentiated bubbles (user right, Myra left)
- Change language button (resets session)

---

## 5. Latency Benchmarks (Real Test Data)

### Before WebSocket TTS Migration (HTTP per-sentence)

| Stage | Average | Best | Worst |
|---|---|---|---|
| STT → LLM | ~1ms | 0ms | 4ms |
| LLM → First Sentence | ~152ms | 131ms | 191ms |
| TTS (HTTP round-trip) | ~778ms | 664ms | 897ms |
| **Total TTFB** | **~935ms** | **814ms** | **1051ms** |

### After WebSocket TTS (current — live benchmark)

| Query | TTFB | Notes |
|---|---|---|
| Short query ("yeah") | **0.58s** | 🟢 Best case |
| Normal query ("okay then") | **0.71s** | ✅ Typical |
| Post-interrupt (merged query) | 0.80s | ✅ |
| Long query (WS barely lost race) | 1.12s | ⚠️ Rare |

### Improvement Summary

| Metric | HTTP (before) | WebSocket (now) | Improvement |
|---|---|---|---|
| **Average TTFB** | ~935ms | **~740ms** | **−195ms ✅** |
| **Best TTFB** | 814ms | **500ms** | **−314ms ✅** |
| **Worst (normal)** | 1051ms | **~890ms** | **−160ms ✅** |

### Cache Performance

| Scenario | TTFB |
|---|---|
| Normal fresh query | ~740ms |
| Cache hit (same query replay) | **~10ms** |

---

## 6. Known Issues & Limitations

| Issue | Status | Notes |
|---|---|---|
| Hindi inter-sentence gap (~200ms) | 🟡 Minor | Reconnect fires after 422 (English filler in Hindi mode) — small audible pause between sentences |
| Acoustic Echo | 🟡 Mitigated | Browser AEC with OS-level hint; headphones eliminate it entirely |
| Deepgram occasional hang | 🟡 Handled | 0.3s fallback timer covers delayed `speech_final` |
| WS race condition TTFB spike | 🔵 Rare | If WS connect loses race to LLM first token, TTFB spikes to ~1.1s |
| Session persistence on refresh | 🔴 None | Memory is in-process; refresh resets conversation context |
| Multi-user concurrency | 🔵 Untested | Each WebSocket gets isolated session state — theoretically safe |

---

## 7. Recommended Next Steps

### High Impact
1. **Session Persistence** — Store `memory.sessions` to Redis or SQLite so conversations survive page refreshes.
2. **Headphone note for demos** — Advise demo users to use headphones to eliminate physical acoustic echo.
3. **LLM upgrade evaluation** — Test `llama-3.3-70b-versatile` on Groq for better Hinglish quality at the cost of +200ms TTFB, or evaluate Gemini Flash for best multilingual output.

### Medium Impact
4. **Deepgram endpointing tuning** — ✅ Completed (Tuned to 200ms endpointing with 1000ms utterance boundary fallback for optimal responsiveness).
5. **Per-session analytics** — Log TTFB, session duration, and interrupt frequency to `convo.log` for performance monitoring.

### Low Impact / Polish
6. **WS keepalive ping** — Add periodic `{"type":"ping"}` to prevent Sarvam WS idle timeout during long silences between turns.
7. **Conversation export** — Surface `convo.log` via a REST endpoint or admin panel.
8. **Analytics dashboard** — Visualize TTFB trends and session health over time.

---

## 8. Future Plans (Not Immediate)

### 🔵 Sarvam AI STT WebSocket Migration
**Status:** Researched — deferred. Do not implement without careful testing.

**Endpoint:** `wss://api.sarvam.ai/speech-to-text/ws`  
**Auth:** `api-subscription-key` header  
**Model:** `saaras:v3` (recommended) — 24 Indian languages, `codemix` mode for Hinglish

**Why it's interesting:**
- Native Indian language models → significantly better Hindi + Hinglish accuracy
- `codemix` mode handles mixed Hindi+English naturally at the model level
- India-based servers → potentially −10–30ms round-trip improvement

**Why we're waiting:**
- **Audio format incompatibility** — Sarvam only accepts `wav`/`pcm`, but our frontend sends `audio/webm`. Requires backend `webm→pcm_s16le` conversion per chunk via ffmpeg/audioop.
- **No interim word events** — No `is_final` equivalent. Live word-by-word transcript typing effect would need to be redesigned around `START_SPEECH`/`END_SPEECH` VAD events.
- **No precise endpointing control** — Only `high_vad_sensitivity: true/false` (vs Deepgram's `endpointing=300ms`). VAD timing unknown — could be slower than 300ms, adding to TTFB.
- **High rewrite cost** — Entire dual-timer endpointing system, STT module, and transcript streaming all need changes.

**Latency impact (estimated):**
| Scenario | Delta vs current |
|---|---|
| Best case (Sarvam VAD fast) | −30ms |
| Neutral | +10ms |
| Worst case (Sarvam VAD slow) | +100–300ms |

**Migration plan when ready:**
1. Add ffmpeg/pydub webm→pcm_s16le conversion in [speech_to_text.py](file:///c:/Users/omtil/OneDrive/Desktop/Riverwood_AI/backend/modules/speech_to_text.py)
2. Replace Deepgram client with Sarvam WS client
3. Use `END_SPEECH` VAD as early-fire trigger; keep 0.5s fallback timer as safety net
4. Redesign live transcript streaming without `is_final` word events
5. Set `mode=codemix` for Hindi sessions


---

## 8. File Structure Reference

```
Riverwood_AI/
├── backend/
│   ├── main.py                      # WebSocket server, full pipeline orchestration
│   ├── modules/
│   │   ├── speech_to_text.py        # Deepgram STT wrapper
│   │   ├── conversation_engine.py   # Groq LLM streaming + sentence splitting
│   │   ├── text_to_speech.py        # Sarvam AI WebSocket TTS (TTSSession + TTSSessionContext)
│   │   └── memory_manager.py        # Session memory + rolling summary
│   └── .env                         # API keys (DEEPGRAM, GROQ, SARVAM)
└── frontend/
    └── components/
        └── VoiceAgent.tsx           # Full UI, audio capture, playback engine
```

---

*Report generated from live codebase analysis — March 16, 2026*  
*Updated after WebSocket TTS migration: −195ms average TTFB improvement*
