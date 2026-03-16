# Riverwood AI — Voice Agent

A real-time AI voice agent for **Riverwood Estate** — a bilingual (English + Hindi/Hinglish) voice assistant named **Myra** that handles site-visit scheduling via natural phone-call-style conversations.

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| **Frontend** | Next.js 14, TypeScript, Web Audio API |
| **Backend** | FastAPI, Python, asyncio |
| **STT** | Deepgram (real-time streaming) |
| **LLM** | Groq (Llama 3.1 8B Instant) |
| **TTS** | Sarvam AI `bulbul:v3` (WebSocket streaming) |
| **Transport** | WebSocket (binary audio frames) |

---

## Features

- 🎙️ Real-time barge-in / interruption support
- 🔄 Bilingual — English and Hindi/Hinglish
- ⚡ Streaming LLM + TTS pipeline (low TTFB)
- 🧠 Rolling conversation memory with summarization
- 🔇 Echo cancellation & noise suppression
- 📋 Construction site daily updates

---

## Project Structure

```
Riverwood_AI/
├── backend/
│   ├── main.py                    # FastAPI WebSocket server
│   ├── modules/
│   │   ├── conversation_engine.py # LLM streaming + memory
│   │   ├── speech_to_text.py      # Deepgram live STT
│   │   ├── text_to_speech.py      # Sarvam TTS WebSocket session
│   │   └── memory_manager.py      # Session memory + summarization
│   ├── construction_updates.json  # Daily site update feed
│   └── requirements.txt
└── frontend/
    ├── app/
    │   └── page.tsx
    ├── components/
    │   └── VoiceAgent.tsx         # Main voice agent UI
    └── package.json
```

---

## Setup

### 1. Clone the repo

```bash
git clone https://github.com/YOUR_USERNAME/Riverwood_AI.git
cd Riverwood_AI
```

### 2. Backend

```bash
cd backend
python -m venv venv
venv\Scripts\activate          # Windows
# source venv/bin/activate    # macOS/Linux

pip install -r requirements.txt
```

Create `backend/.env`:
```env
GROQ_API_KEY=your_groq_api_key
SARVAM_API_KEY=your_sarvam_api_key
DEEPGRAM_API_KEY=your_deepgram_api_key
```

Start the backend:
```bash
python -m uvicorn main:app --reload --port 8000
```

### 3. Frontend

```bash
cd frontend
npm install
npm run dev
```

Open [http://localhost:3000](http://localhost:3000).

---

## Environment Variables

| Variable | Where | Description |
|----------|-------|-------------|
| `GROQ_API_KEY` | `backend/.env` | Groq LLM API key |
| `SARVAM_API_KEY` | `backend/.env` | Sarvam AI TTS key |
| `DEEPGRAM_API_KEY` | `backend/.env` | Deepgram STT key |

---

## Usage

1. Select language (English / Hindi)
2. Tap the **green mic button** to start a call
3. Speak naturally — Myra responds in real time
4. Tap the **amber button** (shown while Myra is speaking) to **interrupt**
5. Tap the **red button** to end the call
