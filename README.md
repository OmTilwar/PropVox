# PropVox AI Voice Agent

Real-time, bilingual (English / Hinglish) AI phone agent for real-estate outreach.
**Myra** calls leads over Twilio, talks naturally with sub-second perceived latency,
handles interruptions, and remembers each caller across calls.

```
Phone (customer)
   ⇅  Twilio Media Streams — 8 kHz μ-law over WebSocket
backend/main.py  (Python asyncio WebSocket server)
   ├── Deepgram Nova-3 (multi)  → streaming STT, English + Hindi code-switching
   ├── Groq (Llama 4 Scout)     → streaming reply, starts with a [filler] token
   ├── Pre-decoded filler audio → played the instant the [filler] token arrives
   ├── Sarvam bulbul:v3 TTS     → sentence-by-sentence synthesis, streamed back to Twilio
   └── CRM memory (Redis or JSON) → merged post-call summary, injected into the next call
```

## Latency pipeline

| Technique | Effect |
|---|---|
| **Instant filler audio** — the LLM's first token is a tag like `[neutral_hmm]`; the matching pre-decoded μ-law clip is sent to Twilio immediately | Caller hears a human reaction ~300 ms after they stop talking |
| **TTS socket pre-warm** — a Sarvam WebSocket is opened as soon as the caller's turn ends, overlapping the LLM's thinking time | Removes the TLS/WS handshake from time-to-first-audio |
| **Parallel sentence synthesis** — every complete sentence starts TTS immediately; playback stays in order | No gap between sentences waiting for the next synthesis to start |
| **Early first clause** — the first chunk of a reply may be cut at a comma once it is long enough | First spoken words arrive sooner on long opening sentences |
| **Hindi-aware sentence splitting** — splits on `।` as well as `. ? !`, never inside numbers like `2.5` | Hinglish replies stream instead of waiting for the full reply |
| **Shared Groq client** — one HTTP connection pool per process | Reuses warm TLS connections across turns and calls |
| **Background in-call memory folding** — older turns are summarised off the critical path | Turn tasks finish as soon as the reply is out |
| **Parallel filler decoding at startup** | Server boot ~5× faster |

## Conversation features

- **English-first, mirrors Hindi** — switches to Hinglish (Hindi in Devanagari, feminine verb forms) when the caller speaks Hindi, and back.
- **Barge-in** — interim transcripts while Myra is speaking cancel the LLM + TTS for that turn and send Twilio a `clear` to flush buffered audio.
- **Full utterances** — Deepgram `is_final` segments are accumulated until `speech_final`, so long sentences are not truncated.
- **Cross-call memory** — after each call the LLM merges the previous CRM profile with the new transcript (absolute IST dates for visits); the next call opens with that context, including a wellbeing check if the caller was unwell.
- **Rolling in-call memory** — the last 6 exchanges are sent verbatim, older ones are compressed.
- **Call logs** — one JSON per call in `backend/conversation_logs/` (git-ignored).

## Quick start

Prerequisites: Python 3.11+, `ffmpeg` on PATH, accounts for Deepgram, Groq, Sarvam and Twilio.

```bash
cd backend
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt twilio
cp .env.example .env            # fill in your keys
python main.py                  # ws://0.0.0.0:5050
```

Expose the server publicly (e.g. `ngrok http 5050`), set `VOICE_AGENT_WSS_URL=wss://<your-host>` in `.env`, then place a call:

```bash
python make_outbound_call.py
```

The customer speaks first; Myra replies after their first words.

### Customising the project

The facts Myra pitches come from env vars, so the same agent works for any project:

```
PROPVOX_COMPANY, PROPVOX_PROJECT, PROPVOX_LOCATION, PROPVOX_SIZE, PROPVOX_PRICE
```

Other knobs: `MYRA_LANGUAGE` (`auto` / `english` / `hinglish`), `MYRA_LLM_MODEL`, `DEEPGRAM_MODEL`, `DEEPGRAM_LANGUAGE`, `SARVAM_SPEAKER`, `TTS_SAMPLE_RATE`, `REDIS_URL`.

## Deploy (Render / Docker)

```bash
docker build -t propvox-voice-agent .
docker run -p 5050:5050 --env-file backend/.env propvox-voice-agent
```

`render.yaml` deploys the same image on Render; the server logs the `wss://` URL to point Twilio at.

## Repository layout

```
backend/
  main.py                 Twilio WebSocket handler: STT → LLM → filler → TTS pipeline
  llm.py                  Groq streaming layer, prompt, in-call memory, CRM summary
  stt.py                  Deepgram streaming client
  tts.py                  Sarvam streaming client with pre-warmed sockets
  crm.py                  Caller memory (Redis, falls back to customers.json)
  conversation_log.py     Per-call JSON logs
  make_outbound_call.py   Dial a number and connect it to the agent
  simple_twilio_call.py   Minimal Twilio <Say> test call
  test_layers.py          Latency smoke test for each provider
  filler_audio/           Pre-recorded English / Hindi filler clips
benchmark/                STT / LLM / TTS latency benchmarks and filler generation
docs/                     System evaluation report
local_mic_agent.py        Early laptop-microphone prototype
```
