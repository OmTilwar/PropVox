import os
import re
import asyncio

from openai import AsyncOpenAI
from dotenv import load_dotenv

from tzutil import IST, now_str_ist

load_dotenv()

# Project facts injected into the prompt; override per deployment via env vars.
PROJECT = {
    "company": os.environ.get("PROPVOX_COMPANY", "PropVox Realty"),
    "project": os.environ.get("PROPVOX_PROJECT", "PropVox Estate"),
    "location": os.environ.get("PROPVOX_LOCATION", "Sector 12, NH-44 corridor"),
    "size": os.environ.get("PROPVOX_SIZE", "20-acre plotted township"),
    "price": os.environ.get(
        "PROPVOX_PRICE", "Plots start from ₹30 lakh. Sizes range from 100 to 300 sqyd."
    ),
}

# OpenAI-compatible LLM providers. OpenRouter is preferred when its key is present.
LLM_PROVIDERS = {
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "key_envs": ("OPENROUTER_API_KEY", "OPEN_ROUTER_API"),
        "default_model": "meta-llama/llama-3.3-70b-instruct",
    },
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "key_envs": ("GROQ_API_KEY",),
        "default_model": "openai/gpt-oss-120b",
    },
}


def _provider_key(name):
    for env in LLM_PROVIDERS[name]["key_envs"]:
        value = (os.environ.get(env) or "").strip()
        if value:
            return value
    return None


def resolve_llm_provider():
    """LLM_PROVIDER env wins; otherwise OpenRouter if keyed, else Groq."""
    name = (os.environ.get("LLM_PROVIDER") or "").strip().lower()
    if name not in LLM_PROVIDERS:
        name = "openrouter" if _provider_key("openrouter") else "groq"
    return name


def _extra_body(provider, model):
    """Provider-specific request options tuned for low first-token latency."""
    if provider == "openrouter":
        body = {"provider": {"sort": "latency"}}
        fallbacks = [
            m.strip()
            for m in (os.environ.get("MYRA_LLM_FALLBACK_MODELS") or "openai/gpt-4o-mini").split(",")
            if m.strip() and m.strip() != model
        ]
        if fallbacks:
            body["models"] = [model] + fallbacks  # tried in order if the primary errors / is rate-limited
        return body
    if "gpt-oss" in model:
        return {"reasoning_effort": "low"}
    return None


# One shared client per process: reuses the HTTP/TLS connection pool across turns and calls.
_SHARED_CLIENTS = {}


def _get_client(provider: str, api_key: str) -> AsyncOpenAI:
    if provider not in _SHARED_CLIENTS:
        _SHARED_CLIENTS[provider] = AsyncOpenAI(
            base_url=LLM_PROVIDERS[provider]["base_url"], api_key=api_key
        )
    return _SHARED_CLIENTS[provider]


# Last N user+assistant pairs kept verbatim in the API; older turns fold into rolling_incall_summary
WINDOW_PAIRS = 6
WINDOW_MSGS = WINDOW_PAIRS * 2


# Roman-script Hindi/Hinglish cues (STT often transcribes Hindi this way).
_LATIN_HINDI_RE = re.compile(
    r"\b("
    r"haan|haanji|hanji|nahi|nahin|naa|nhi|"
    r"theek|thik|accha|acha|achha|"
    r"aap|kya|kab|kahan|kaha|kyun|kaise|kaisi|"
    r"hai|ho|hoon|hain|hum|mein|maine|"
    r"aaj|kal|bas|matlab|suno|suniye|batao|bataiye|"
    r"meri|mera|mere|samajh|"
    r"shukriya|dhanyavaad|dhanyawad|alvida|"
    r"koi|kabhi|ji"
    r")\b",
    re.I,
)


def _infer_language_hint_from_user_text(text: str) -> str:
    """
    English-first: default English unless the user's last message clearly
    indicates Hindi (Devanagari or common Latin-script Hindi).
    Used when MYRA_LANGUAGE=auto.
    """
    if not text or not str(text).strip():
        return "english"
    s = str(text)
    if re.search(r"[\u0900-\u097F]", s):
        return "hinglish"
    if _LATIN_HINDI_RE.search(s):
        return "hinglish"
    return "english"


def _last_user_content_for_turn(dialogue: list) -> str:
    """Last real user utterance (CRM dialogue; bootstrap stripped)."""
    for m in reversed(dialogue):
        if m.get("role") == "user":
            return (m.get("content") or "").strip()
    return ""


class GroqLLMLayer:
    """
    LLM layer over an OpenAI-compatible API (OpenRouter by default, Groq optional).
    Exposes an async streaming generator for seamless TTS chunking.
    (Class name kept for backward compatibility.)
    """
    def __init__(
        self,
        customer_context=None,
        filler_keys=None,
        filler_keys_english=None,
        filler_keys_hinglish=None,
    ):
        self.provider = resolve_llm_provider()
        self.api_key = _provider_key(self.provider)
        if not self.api_key:
            raise ValueError(
                f"No API key for LLM provider '{self.provider}' "
                f"(set one of {', '.join(LLM_PROVIDERS[self.provider]['key_envs'])})."
            )

        # Async client used for real-time streaming
        self.client = _get_client(self.provider, self.api_key)
        self.model = (
            os.environ.get("MYRA_LLM_MODEL")
            or LLM_PROVIDERS[self.provider]["default_model"]
        ).strip()
        self._extra_body = _extra_body(self.provider, self.model)
        self.rolling_incall_summary = ""
        self._dialogue_folded_until = 0
        self._fold_task = None
        # Filler buckets: we select the correct one per turn language.
        if filler_keys_english is None and filler_keys_hinglish is None:
            # Backward-compat: single list applies to both.
            keys = tuple(filler_keys) if filler_keys else ()
            self._filler_keys_english = keys
            self._filler_keys_hinglish = keys
        else:
            self._filler_keys_english = tuple(filler_keys_english or ())
            self._filler_keys_hinglish = tuple(filler_keys_hinglish or ())

            # If one bucket is empty, fall back to whatever we have.
            if not self._filler_keys_english:
                self._filler_keys_english = tuple(filler_keys) if filler_keys else self._filler_keys_hinglish
            if not self._filler_keys_hinglish:
                self._filler_keys_hinglish = tuple(filler_keys) if filler_keys else self._filler_keys_english

        self._filler_keys_all = tuple(
            sorted(set(self._filler_keys_english) | set(self._filler_keys_hinglish))
        )
        self._past_first_assistant_reply = False  # after first spoken turn, forbid repeat intro
        raw = (os.environ.get("MYRA_LANGUAGE") or "auto").strip().lower()
        if raw in ("english", "en", "all_english"):
            self._language_mode = "force_english"
        elif raw in ("hinglish", "hi", "hindi"):
            self._language_mode = "force_hinglish"
        else:
            self._language_mode = "auto"  # real-time mirror (default)

        base_prompt = self._base_prompt_body()
        base_prompt += self._crm_context_block(customer_context)

        self._static_system_base = base_prompt
        self.conversation_history = [{"role": "system", "content": self._static_system_base}]
        print(
            f"[LLM] provider={self.provider} model={self.model} | MYRA_LANGUAGE={self._language_mode} "
            f"| fillers: EN={len(self._filler_keys_english)} HI={len(self._filler_keys_hinglish)} "
            "(auto=English first, switch to Hinglish when user speaks Hindi; hinglish|english=lock)"
        )

    def _base_prompt_body(self):
        body = """You are Myra, a friendly, casual human female caller from {project}. 
You are NOT an AI assistant. You are a real woman making a phone call.

─── WHO SPEAKS FIRST (IMPORTANT) ─────────────────────────────────────────
- On this call the CUSTOMER speaks first (e.g. hello / हाँ). You do not talk until their first message appears in the chat.
- Your very first reply must respond to what they actually said: acknowledge it briefly, then introduce who you are if needed,
  and continue with CRM (if any) in 1–2 short sentences. Do not monologue before they have spoken.

─── REAL-TIME LANGUAGE (ENGLISH FIRST — SWITCH ON HINDI) ───────────────
- **Default:** your **entire** reply after the [filler] is **English** until the user clearly speaks/writes **Hindi**.
- **Switch to Hinglish** when their **latest** message has Devanagari **or** clear Hindi in Roman script (the LIVE line below will say **hinglish**).
- **Switch back to English** when their latest message is English-only again (LIVE line **english**).
- **Mixed** in one turn (English + Hindi): use **Hinglish** for that reply; keep **English** for plot/site/EMI terms; Hindi only in Devanagari.
- You may get a **LIVE** line below that says FORCE or HINT for this turn — follow it when present.

─── LANGUAGE & SCRIPT (WHEN YOU OUTPUT HINGLISH THIS TURN) ────────────────
When this turn is Hinglish (per above):
You must seamlessly understand and reply in Hinglish.
CRITICAL: ALL Hindi words MUST be written exclusively in the Devanagari (Hindi) script. NEVER write Hindi words using generic English/Latin letters!
- ALWAYS use feminine verb forms in Hindi:
  ✅ "मैं बता रही हूँ" / "मैं समझती हूँ" / "मैं share करूँगी"
  ❌ "मैं बता रहा हूँ" / "मैं भेजूँगा" (Male forms are strictly forbidden)
- Address the customer with "आप". Use "जी" sparingly (at most once per reply when natural); do not end every phrase with जी or stack "जी जी".
- Never use "तुम/तुझे/भाई".
- Use English ONLY for real estate and daily terminology naturally:
  ✅ plot, site, booking, EMI, plan, visit, available, details.
- EXTREMELY short and punchy sentences. 1-2 sentences maximum.

─── BEHAVIOR & TONE ──────────────────────────────────────────────────────
- NEVER say "How can I help you?", "Certainly", "I am an AI", or use bullet points.
- Stay STRICTLY on topic: {project}, plots, construction, and site visits.
- If asked personal questions, politely redirect.
- NEVER invent personal details or facts. If unsure in English: say details can be covered on the site visit; in Hinglish you may say "Site visit पे details मिल जाएँगी।"

─── TIMES & DATES — DO NOT HALLUCINATE ────────────────────────────────────
- Never invent a specific clock time (e.g. 3 बजे, 10:30, "at 5 PM") unless it appears in the CRM profile below OR the user said it clearly in this same call.
- If you need a time but none is known: ask a short open question (e.g. कितने बजे convenient है?) without naming an hour yourself.
- You may still say relative words tied to CRM/IST (e.g. कल / आज) when grounded by the profile or live clock.

─── PROJECT FACTS ────────────────────────────────────────────────────────
- Company: {company}
- Project: {project}
- Location: {location}
- Size: {size}
- Cost / Price: {price}

─── CRM IS YOUR MEMORY — DO NOT IGNORE IT ─────────────────────────────────
If a PREVIOUS CALL (CRM) block appears below, you are calling someone you already spoke with.
- The CRM profile is authoritative: it is what YOU (Myra) last knew about them. Treat it as fact until they correct you.
- You MUST NOT sound like a cold first-time telecaller who forgot everything. Never open with only a generic
  "site visit का क्या plan है? / कब आओगे?" when CRM already mentions a committed visit, time, or clear next step.
- First substantive content after your opening greeting MUST tie to CRM: confirm the scheduled visit, ask if they are
  still coming as discussed, or follow up on their last objection/interest. Use CURRENT TIME (IST) in the live section
  below so "tomorrow / कल / today" matches the real calendar (e.g. if they said "कल visit" last call and today IS that
  day, ask if they are coming today as planned — not "tomorrow" again).

─── RETURNING CALLER (CRM profile present) ───────────────────────────────
- Open warmly, then immediately anchor on CRM: e.g. visit they fixed, time, or what they asked last time.
- If CRM states a future visit date/time: ask ONE natural confirmation — e.g. whether they are still coming as scheduled,
  or if timing still works — NOT a fresh generic pitch.
- If that visit date is already TODAY (per IST): ask if they are coming today / on track for the agreed time.
- If that visit date is already PAST (per IST): gently ask if they visited or want to reschedule — do not pretend it is still "upcoming tomorrow".
- If CRM shows interest but no fixed visit: you may propose a visit or timing, but reference what they cared about (size, budget) from CRM.

─── HEALTH / WELLBEING — NEXT CALL AFTER THEY WERE UNWELL (MANDATORY) ───
- Read the PREVIOUS CALL (CRM) profile: if it says (or clearly implies) the user was **unwell**, **not feeling well**,
  **sick**, **under the weather**, needed **rest**, asked to **call later because of health**, **tabiyat** / **तबीयत**
  not okay, or similar: on **this call's first reply after they speak** (e.g. after "hello"), you **must** include **one short,
  human line** checking if they are **feeling better now**, before you push site visit or sales detail.
- English examples (tone: light, not medical): "Hope you're feeling a bit better today — okay if we chat for a minute?",
  "Just wanted to check — are you feeling better since we last spoke?"
- Hinglish examples (when this turn is hinglish, Hindi in Devanagari only): "आज थोड़ा बेहतर feel कर रहे हैं?",
  "बस पूछ रही थी — अब तबीयत ठीक है?"
- If they say they are **still** unwell: be brief, wish them well, offer to call another time — do not push the visit hard.
- Do **not** skip this wellbeing line when CRM clearly mentions illness/unwellness, even if a visit is also scheduled.

─── NEW LEAD (no CRM block below) ───────────────────────────────────────
- Greeting: casual **English** first, e.g. "Hi, this is Myra from {project} — is this a good time?"
- If they reply in Hindi, switch to Hinglish from that turn onward until they go English-only again.
- Then one light hook and ONE soft question about site visit interest or when they can come — not pushy.

─── DURING THE CALL (both cases) ─────────────────────────────────────────
- IF they show interest (day/time): narrow to time, then confirm clearly.
- IF NOT INTERESTED or NO PLAN: accept it politely (e.g. कोई बात नहीं…). Offer WhatsApp details; do NOT keep forcing visit.

─── ENDING THE CALL ─────────────────────────────────────────────────────
ONLY close when they clearly say goodbye (bye, thanks, alvida, shukriya). Then warm close; do not reopen the pitch.
"""
        # str.replace (not .format) so any literal braces in the prompt stay safe.
        for key, value in PROJECT.items():
            body = body.replace("{" + key + "}", value)
        return body

    def _filler_instruction_block(self, filler_keys):
        """Tokens must match filler mp3 stem names, e.g. neutral_hmm -> [neutral_hmm]."""
        if not filler_keys:
            return ""
        listed = "\n".join(f"  - {tok}" for tok in filler_keys)
        return (
            "\n\n─── INSTANT FILLER AUDIO (MANDATORY EVERY REPLY) ───\n"
            "Pre-recorded audio exists ONLY for these exact bracket tokens (nothing else is valid):\n"
            f"{listed}\n\n"
            "RULES:\n"
            "- EVERY assistant message MUST start with EXACTLY ONE token from the list above — copy/paste spelling.\n"
            "- Put NOTHING before that token: no space, no punctuation, no Hindi/English before `[`.\n"
            "- Right after the closing `]`, add a space if needed, then your spoken line for **this turn** "
            "(1–2 short sentences) in the language given under **THIS TURN — LANGUAGE (LIVE)** in the live section.\n"
            "- Pick a token that fits the moment (thinking → neutral_hmm / neutral_i_see; agreement → ack_*; pause → wait_*).\n"
            "- Do NOT invent new [bracket] sounds. Do NOT output more than one filler token at the start.\n"
        )

    def _crm_context_block(self, customer_context):
        if not customer_context:
            return ""
        if isinstance(customer_context, str):
            summary, last_dt = customer_context, None
        else:
            summary = (customer_context or {}).get("summary") or ""
            last_dt = (customer_context or {}).get("last_call_dt")
        last_line = f"When that call ended (IST): {last_dt}\n" if last_dt else ""
        now_ist = now_str_ist()
        return (
            f"\n\n─── PREVIOUS CALL (CRM) — USE THIS, DO NOT RESET TO COLD SCRIPT ───\n"
            f"{last_line}"
            f"Profile: {summary}\n\n"
            f"─── TIME ANCHOR (for interpreting visits vs 'tomorrow') ───\n"
            f"Right now (IST): {now_ist}\n\n"
            "MANDATORY BEHAVIOUR:\n"
            "- Your opening (after filler) must show you remember this profile — e.g. confirm their scheduled visit, "
            "or the last thing they said — not a generic site-visit pitch alone.\n"
            "- If the profile mentions illness, unwell, not feeling well, or health-related reason to defer the call: "
            "your **first reply after they speak** must include a short wellbeing check (see HEALTH / WELLBEING in instructions) — "
            "before leaning on site visit or pricing.\n"
            "- If the profile mentions a visit on a specific calendar day/time: your job is to confirm or gently "
            "check that plan against TODAY's date above (IST).\n"
            "- Do NOT ask 'when will you visit?' / 'कब site visit करेंगे?' as if nothing was agreed if the profile "
            "already states a visit commitment.\n"
            "- Resolve tomorrow / today / कल / Monday using TODAY (IST) vs the dates implied in the profile — "
            "not as a stale phrase from the old call.\n"
        )

    def _build_system_for_request(self):
        """Fresh IST clock + optional rolling in-call memory (last 6 pairs sent separately)."""
        parts = [self._static_system_base]
        parts.append("\n\n─── LIVE CALL — CURRENT TIME (IST) ───\n")
        parts.append(now_str_ist())
        parts.append(
            "\nUse this as the only source of truth for today vs tomorrow vs कल vs scheduled dates. "
            f"The last {WINDOW_PAIRS} back-and-forth turns are in the messages below; older turns are summarized only.\n"
        )
        dialogue = self._dialogue_for_crm()
        last_user = _last_user_content_for_turn(dialogue)
        mode = self._language_mode
        turn_filler_lang = None
        if mode == "force_english":
            turn_filler_lang = "english"
            parts.append(
                "\n─── THIS TURN — LANGUAGE (LIVE) ───\n"
                "FORCE: Your entire reply after [filler] must be **English only** (no Devanagari, no Hindi words).\n"
            )
        elif mode == "force_hinglish":
            turn_filler_lang = "hinglish"
            parts.append(
                "\n─── THIS TURN — LANGUAGE (LIVE) ───\n"
                "FORCE: Your entire reply after [filler] must be **Hinglish** (Hindi in Devanagari; English for plot/site/EMI terms).\n"
            )
        else:
            hint = _infer_language_hint_from_user_text(last_user)
            turn_filler_lang = hint
            parts.append(
                "\n─── THIS TURN — LANGUAGE (LIVE) ───\n"
                f"HINT (English-first): for your text after [filler], use **{hint}** — "
                "**english** unless their last message clearly indicates Hindi; then **hinglish**.\n"
            )

        # Select the filler audio bucket to match this turn's language.
        if turn_filler_lang == "english":
            filler_keys_for_turn = self._filler_keys_english
        else:
            filler_keys_for_turn = self._filler_keys_hinglish
        parts.append(self._filler_instruction_block(filler_keys_for_turn))

        if self.rolling_incall_summary.strip():
            parts.append("\n─── EARLIER IN THIS CALL (compressed) ───\n")
            parts.append(self.rolling_incall_summary.strip())
        if not self._past_first_assistant_reply:
            parts.append(
                "\n\n─── FIRST MYRA REPLY (user already spoke — e.g. hello) ───\n"
                "- Acknowledge in one short beat (e.g. hi / thanks / हाँ जी) matching their tone, then continue in "
                "the language for this turn (see LIVE LANGUAGE above).\n"
                "- ONE tight opening after [filler]; do not chain two greetings.\n"
                "- If CRM exists and the profile mentions they were unwell / not feeling well: **include the wellbeing "
                "check line in this first reply** (required — see HEALTH / WELLBEING), then optionally one line on visit."
                " You may use 2 short sentences total for this case (still no long monologue).\n"
                "- If CRM exists (no health issue in profile): you may skip a long self-intro and go straight to "
                "confirming the visit — or one short identity + one question (max 1–2 short sentences).\n"
                "- If no CRM: brief who-you-are + one question.\n"
                "- Never stack long intro + second greeting + main ask in the same reply.\n"
            )
        if self._past_first_assistant_reply:
            parts.append(
                "\n\n─── MID-CALL (second reply onward — NO second greeting) ───\n"
                "- Do NOT re-introduce yourself or open like a new cold call.\n"
                "- FORBIDDEN after [filler]: full cold-open lines like \"this is Myra from " + PROJECT["project"] + "\", "
                "\"" + PROJECT["project"] + " से Myra बोल रही हूँ\", or repeating the pitch as a fresh opener.\n"
                "- If they only said Hello / Hi / हाँ / Ok: do not lead with the project name; move the topic forward.\n"
                "- Continue the thread: confirm, answer, or one follow-up — no duplicate opening energy.\n"
                "- Re-identify only if they ask who is calling (कौन / who's this).\n"
                "- Start with exactly one [filler] token, then your text in the language for this turn.\n"
            )
        return "".join(parts)

    def _messages_for_api(self):
        """System + rolling memory + last WINDOW_PAIRS user/assistant pairs (no full history)."""
        dialogue = self._dialogue_for_crm()
        window = dialogue[-WINDOW_MSGS:] if len(dialogue) > WINDOW_MSGS else dialogue
        return [{"role": "system", "content": self._build_system_for_request()}] + window

    async def generate_response_stream(self, user_text: str):
        """
        Sends the user text to Groq and yields tokens back as they arrive.
        """
        self.conversation_history.append({"role": "user", "content": user_text})
        messages = self._messages_for_api()
        full_response = ""

        try:
            stream = await self.client.chat.completions.create(
                model=self.model,
                extra_body=self._extra_body,
                messages=messages,
                stream=True,
                max_tokens=280,
                temperature=0.7
            )

            async for chunk in stream:
                if chunk.choices:
                    delta = chunk.choices[0].delta.content
                    if delta:
                        full_response += delta
                        yield delta

            if full_response.strip():
                self.conversation_history.append({"role": "assistant", "content": full_response.strip()})
                self._past_first_assistant_reply = True
            # Fold old turns in the background so the turn task finishes as soon as the reply is out
            # (a barge-in would otherwise cancel the fold mid-request).
            if self._fold_task is None or self._fold_task.done():
                self._fold_task = asyncio.create_task(self._maybe_roll_incall_summary())

        except Exception as e:
            print(f"[LLM Error] ({self.provider}): {e}")

    async def _maybe_roll_incall_summary(self):
        dialogue = self._dialogue_for_crm()
        while len(dialogue) - self._dialogue_folded_until > WINDOW_MSGS:
            end = len(dialogue) - WINDOW_MSGS
            chunk = dialogue[self._dialogue_folded_until:end]
            if chunk:
                await self._fold_incall_chunk(chunk)
            self._dialogue_folded_until = end

    async def _fold_incall_chunk(self, chunk):
        chunk_text = "\n".join(
            f'{m["role"].upper()}: {m.get("content", "")}' for m in chunk
        )
        prev = self.rolling_incall_summary.strip()
        system = (
            "Compress older live-call lines into a short memory (2-8 lines, can use dashes). "
            "Keep: visit dates/times, interest, objections, commitments, names. Drop filler."
        )
        user = (
            f"EXISTING COMPRESSED MEMORY:\n{prev if prev else '(none)'}\n\n"
            f"ADD THIS DIALOGUE:\n{chunk_text}"
        )
        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                extra_body=self._extra_body,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=0.2,
                max_tokens=350,
            )
            self.rolling_incall_summary = (response.choices[0].message.content or "").strip()
        except Exception as e:
            print(f"[In-call fold Error]: {e}")

    def export_conversation_log(self):
        """Serializable snapshot for offline analysis (JSON). Omits bulky system prompt."""
        turns_all = [
            {"role": m["role"], "content": m.get("content", "")}
            for m in self.conversation_history
            if m["role"] != "system"
        ]
        turns_for_analysis = [
            {"role": m["role"], "content": m.get("content", "")}
            for m in self._dialogue_for_crm()
        ]
        return {
            "turns_full": turns_all,
            "turns_without_greeting_bootstrap": turns_for_analysis,
            "rolling_incall_summary": self.rolling_incall_summary or "",
        }

    def _dialogue_for_crm(self):
        """User/assistant turns only, with the synthetic greeting bootstrap removed."""
        dialogue = [msg for msg in self.conversation_history if msg["role"] != "system"]
        if (
            len(dialogue) >= 2
            and dialogue[0]["role"] == "user"
            and "Please greet the user warmly" in dialogue[0].get("content", "")
        ):
            dialogue = dialogue[2:]
        return dialogue

    async def generate_summary(self, previous_crm_summary=None, previous_last_call_dt=None):
        """
        Build one updated CRM profile by merging prior CRM text with this call's transcript.
        Includes absolute dates (IST) so the next call can reason about calendar vs 'tomorrow'.
        """
        dialogue = self._dialogue_for_crm()
        if not dialogue:
            return None

        has_real_exchange = any(
            m["role"] == "user" and "Please greet the user warmly" not in m.get("content", "")
            for m in dialogue
        )
        if not has_real_exchange and not (previous_crm_summary or "").strip():
            return None

        transcript_text = "\n".join(
            f'{m["role"].upper()}: {m.get("content", "")}' for m in dialogue
        )
        prev = (previous_crm_summary or "").strip()
        call_ended_ist = now_str_ist()

        system = (
            f"You maintain a single compact CRM profile for {PROJECT['project']} outbound calls.\n"
            "You will receive: (A) optional PREVIOUS CRM PROFILE and when that was recorded, "
            f"(B) when THIS CALL ended (IST): {call_ended_ist}, and "
            "(C) THIS CALL TRANSCRIPT.\n\n"
            "Write ONE updated profile (4-6 short sentences, plain text, no bullets) that:\n"
            "- Merges still-relevant facts from the previous profile with new facts from this call.\n"
            "- For any site visit or callback timing, state ABSOLUTE calendar detail (e.g. "
            "'Site visit agreed: Tuesday 25 March 2026, 10:30 AM IST') so a future call does not confuse 'tomorrow'.\n"
            "- When the new call contradicts old info, prefer the latest call.\n"
            "- States interest level, objections, budget/sqyd if mentioned, and what to reference on the next call.\n"
            "- If the user said they are unwell, not feeling well, sick, or need to talk later due to health: state it "
            "explicitly (e.g. 'User was unwell / not feeling well last call; next call should ask how they are feeling.') "
            "so the agent opens with a wellbeing check.\n"
            "- If the transcript is thin but previous profile exists, keep and lightly refresh the previous profile.\n"
            "- If there is no previous profile and almost no substance, output a minimal factual line.\n"
            "- Ignore leading bracket tokens like [neutral_hmm] in assistant lines when extracting customer facts.\n"
            "- Do not record a specific visit clock time unless the user or assistant clearly agreed on it in the transcript."
        )

        user_payload = (
            f"PREVIOUS CRM PROFILE (from a call that ended: {previous_last_call_dt or 'unknown'}):\n"
            f"{prev if prev else '(none — new lead)'}\n\n"
            f"THIS CALL ENDED (IST): {call_ended_ist}\n\n"
            f"THIS CALL TRANSCRIPT:\n{transcript_text}"
        )

        try:
            response = await self.client.chat.completions.create(
                model=self.model,
                extra_body=self._extra_body,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_payload},
                ],
                temperature=0.25,
                max_tokens=400,
            )
            out = response.choices[0].message.content.strip()
            return out or None
        except Exception as e:
            print(f"[LLM Summary Error]: {e}")
            return None
