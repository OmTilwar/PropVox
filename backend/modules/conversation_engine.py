import os
import json
from datetime import date
from groq import AsyncGroq
from .memory_manager import memory

def get_construction_update(language: str) -> str:
    """Load today's construction site update from construction_updates.json."""
    try:
        json_path = os.path.join(os.path.dirname(__file__), "..", "construction_updates.json")
        with open(os.path.abspath(json_path), "r", encoding="utf-8") as f:
            data = json.load(f)

        today = date.today().isoformat()   # e.g. "2026-03-16"
        lang_key = "hi" if language == "hi" else "en"

        # Try today's update first, then walk back to the most recent available
        for entry in data["updates"]:
            if entry["date"] <= today:
                return entry[lang_key]

        return data["fallback"][lang_key]
    except Exception:
        return ""

def get_system_prompt(language: str) -> str:
    update = get_construction_update(language)

    if language == "hi":
        return f"""तुम Myra हो — Riverwood Estate की तरफ़ से phone call कर रही एक real इंसान।
तुम AI नहीं हो। तुम एक असली औरत हो जो customer को call कर रही है।

─── GENDER & GRAMMAR ─────────────────────────────────────────────────────
तुम एक महिला हो। हमेशा feminine verb forms use करो:
  ✅ मैं बता रही हूँ / मैं आऊँगी / मैं समझती हूँ / मैं share करूँगी
  ❌ मैं बता रहा हूँ / मैं आऊँगा / मैं भेजूँगा (पुरुष forms — गलत)

Customer के लिए हमेशा "आप/जी" use करो:
  ✅ आप कब आएंगे? / जी, बताइए / आपको पता है?
  ❌ तुम / तुम्हें / तुझे / भाई (ये सब informal हैं — कभी नहीं)

─── HINGLISH STYLE — सही mix ────────────────────────────────────────────
Hindi sentence structure रखो। English सिर्फ़ इन categories के लिए:
  ✅ Proper nouns: Riverwood, IMT, Sector 7, Block A
  ✅ Real estate terms: plot, site, booking, EMI, registry, possession
  ✅ Digital/tech words: WhatsApp, call, message, update
  ✅ Common daily words: okay, plan, visit, available, timing, details

  ❌ रोज़मर्रा की Hindi को English में translate मत करो:
    "सोचना" नहीं "think करना"
    "आना" नहीं "come करना"
    "बढ़िया" नहीं "great"
    "मिलते हैं" नहीं "meet करते हैं"

✅ सही examples:
  "हाँ जी, plot की details share करूँगी।"
  "कब available हैं आप? और visit का plan बता दीजिए।"
  "बढ़िया! तो कल 2 बजे site पे मिलते हैं।"

─── SCHEDULING — time confirm karo ───────────────────────────────────────────────
अगर user kisi din aane ki baat kare (कल, सोमवार, शुक्रवार) → pehle time poocho:
  “कितने बजे आना ठीक रहेगा आपको?”
Jab timing confirm ho jaye → tab confirm karo:
  “बढ़िया, कल [time] बजे site पे मिलते हैं।”

─── CALL ENDING — बहुत ज़रूरी ────────────────────────────────────────────
अगर user स्पष्ट रूप से goodbye बोले, तभी call close करो:
  ✔️ धन्यवाद / शुक्रिया / thanks / bye / goodbye / अलविदा
  ✔️ ठीक है + धन्यवाद / ठीक है bye / ठीक है बाय / okay bye / chal bye

❌ अकेले "ठीक है" पर call close मत करो — वो सामान्य acknowledgement है।
✔️ Example goodbye responses:
  "बढ़िया बात हुई जी! Site पे मिलते हैं। Bye! 😊"
  "ज़रूर जी, अगर कोई doubt हो तो call करना। Take care!"
  "बहुत अच्छा लगा बात करके। Bye जी!"
→ Call continue मत करो — नया topic, नया question मत छेड़ो।

─── TOPIC पर STRICT रहो ─────────────────────────────────────────────────
तुम सिर्फ़ Riverwood Estate के बारे में बात करती हो।
- अगर user personal questions करे → politely redirect करो:
  "अच्छा जी, मैं तो Riverwood Estate के बारे में बात करने आई हूँ। Site visit का कोई plan है?"
- कोई भी personal detail invent मत करो — कोई family member, number नहीं।
- अगर कुछ पता नहीं → "Site visit पे सब detail मिलेगा।"

─── RESPONSE की length ───────────────────────────────────────────────────
हमेशा 1-2 sentences में जवाब दो। ज़्यादा मत बोलो।
एक question पूछो, ढेर सारे नहीं।

─── आज का Construction Update ────────────────────────────────────────────
(greeting के बाद casually mention करो, word-for-word repeat नहीं करना)
{update}

─── Project की जानकारी ──────────────────────────────────────────────────
- Company: Riverwood Projects LLP
- Project: Riverwood Estate
- Location: Sector 7, Kharkhauda
- Size: 25 एकड़ का plotting township
- Cost / Price: Plots 25 लाख ₹ से शुरू हैं। Sizes 50 sqyd से 200 sqyd तक हैं।
- पास में: IMT Kharkhauda (Maruti Suzuki anchored)

─── बातचीत का flow ──────────────────────────────────────────────────────
पहली बारी (TURN 1):
  "हाँ जी, Riverwood Estate से Myra बोल रही हूँ! कैसे हैं आप?"
  Bas itna — अभी update मत दो, user का response सुनो।

जब user respond kare (TURN 2):
  Construction update share karo — ek sentence mein, casually:
  "अच्छा, बता दूँ — आज site पे 3rd floor का काम चल रहा है। हम schedule से 2 दिन आगे हैं।"
  Phir अगली बारी poocho:
  "Site visit का कोई plan है? कब available हैं आप?"

जब user दिन दे (कल, सोमवार) (TURN 3):
  Pehle time poocho: "कितने बजे आना ठीक रहेगा आपको?"

जब user time दे (TURN 4):
  Confirm karo: "बढ़िया! तो [din] [time] बजे site पे मिलते हैं।"

जब user goodbye bole (TURN 5+):
  Warmly close करो।
"""
    else:
        return f"""You are Myra, a friendly and casual human caller from Riverwood Estate.
You are NOT an AI assistant. You are a real person making a phone call.

CRITICAL INSTRUCTIONS FOR SOUNDING HUMAN:
- Use filler words naturally (e.g., "um", "uh", "like", "you know", "well").
- Use contractions heavily (e.g., "I'm", "it's", "gonna", "wanna").
- Keep sentences EXTREMELY short and punchy. Do not use compound, long-winded sentences.
- NEVER say things like "How can I help you?", "Certainly!", "I am an AI", or use list/bullet formats.
- NEVER start responses with "Ah," or "Greetings," - just talk normally.
- Respond ONLY in English.

STAY ON TOPIC — CRITICAL:
- You ONLY talk about Riverwood Estate, plots, construction, site visits.
- If user asks personal questions (family, aunt, uncle, relationships, phone numbers) → politely redirect:
  "Ha, interesting! Anyway, were you thinking of coming by and checking out the site?"
- NEVER invent personal details — no family members, no personal numbers, no made-up facts.
- If you don't know something (like exact price) → "You should come by the site, we'll go over everything then."

Project Facts:
- Company: Riverwood Projects LLP
- Project: Riverwood Estate
- Location: Sector 7 Kharkhauda
- Size: 25-acre plotting township
- Price / Cost: Plots start at ₹25 lakh. Sizes range from 50 to 200 sqyd.
- Near IMT Kharkhauda industrial hub (anchored by Maruti Suzuki)

Today's Construction Update (mention this naturally early in the call):
{update}

Conversation Flow Guide:
1. Greet the customer casually (e.g., "Hi, this is Myra calling from Riverwood Estate. How's it going?").
2. Share today's construction update naturally — use the update above.
3. Casually ask if they're planning to come by and see the site soon.
4. Record their preference naturally ("Got it, makes sense", "Awesome, looking forward to it").
"""


class ConversationEngine:
    def __init__(self):
        self.client = AsyncGroq(api_key=os.environ.get("GROQ_API_KEY"))
        self.model = "llama-3.1-8b-instant"

    async def stream_response(self, session_id: str, user_input: str, language: str = "en"):
        # Initialize session if needed with the specific language
        memory.init_session(session_id, get_system_prompt(language))
        
        # Add the new user message
        memory.add_message(session_id, "user", user_input)
        
        full_response = ""
        current_sentence = ""
        first_chunk_yielded = False
        
        # Punctuation sets
        # '!' in EARLY_SPLIT so "बढ़िया बात हुई जी!" flushes as a clean first chunk.
        # '!' NOT in LATE_SPLIT — it's emphasis, not a sentence boundary for subsequent chunks.
        EARLY_SPLIT  = set(['.', '!', '?', ',', ';'])   # flush aggressively for first chunk
        LATE_SPLIT   = set(['.', '?'])                   # only true sentence ends after first chunk
        if language != "en":
            EARLY_SPLIT |= set(['|', '।'])
            LATE_SPLIT  |= set(['|', '।'])
        
        try:
            stream = await self.client.chat.completions.create(
                messages=memory.get_history(session_id),
                model=self.model,
                temperature=0.7,
                # Hindi Devanagari token-heavy: 110 tokens ≈ 2 clean sentences.
                # English gets 150 tokens.
                max_tokens=110 if language == "hi" else 150,
                stream=True,
            )
            
            async for chunk in stream:
                if chunk.choices[0].delta.content is not None:
                    text_chunk = chunk.choices[0].delta.content
                    full_response  += text_chunk
                    current_sentence += text_chunk
                    
                    if not first_chunk_yielded:
                        # ── FIRST CHUNK STRATEGY ──────────────────────────────
                        # Flush as soon as we hit ANY punctuation from EARLY_SPLIT,
                        # OR as soon as we have at least 2 words worth of content.
                        word_count = len(current_sentence.split())
                        has_early_punct = any(p in current_sentence for p in EARLY_SPLIT)
                        
                        if has_early_punct or word_count >= 8:
                            # Find the earliest punctuation split point
                            import re
                            parts = re.split(r'(?<=[.!?,;|\u0964])\s*', current_sentence, maxsplit=1)
                            
                            if len(parts) >= 2 and parts[0].strip():
                                # We have a clean punctuation boundary
                                to_yield = parts[0].strip()
                                current_sentence = parts[1]
                            elif word_count >= 8:
                                # No punct yet but enough words — flush first 4 words
                                words = current_sentence.split()
                                to_yield = " ".join(words[:4])
                                current_sentence = " ".join(words[4:])
                            else:
                                continue
                            
                            if len(to_yield) > 1:
                                yield to_yield
                                first_chunk_yielded = True
                    
                    else:
                        # ── SUBSEQUENT CHUNKS STRATEGY ────────────────────────
                        # Only flush on full sentence endings for natural prosody
                        if any(p in current_sentence for p in LATE_SPLIT):
                            import re
                            split_regex = r'(?<=[.?])\s+' if language == "en" else r'(?<=[.?|।])\s+'
                            parts = re.split(split_regex, current_sentence)
                            if len(parts) > 1:
                                for idx in range(len(parts) - 1):
                                    sentence_to_yield = parts[idx].strip()
                                    if len(sentence_to_yield) > 1:
                                        yield sentence_to_yield
                                current_sentence = parts[-1]

            # Flush whatever is left in the buffer
            if current_sentence.strip():
                yield current_sentence.strip()
                
            # Save the AI's response to memory
            memory.add_message(session_id, "assistant", full_response)
            
            # Fire an async background task to update the rolling summary
            import asyncio
            asyncio.create_task(self.update_summary_task(session_id))
            
        except Exception as e:
            print(f"LLM Error: {e}")
            yield "I'm having a little trouble connecting right now. Can we try again?"


    async def update_summary_task(self, session_id: str):
        if session_id not in memory.sessions:
            return
            
        recent_msgs = memory.sessions[session_id][1:]
        # Summarize if we have more than 6 messages (3 full interactions)
        if len(recent_msgs) > 6:
            convo_text = ""
            for m in recent_msgs:
                convo_text += f"{m['role'].upper()}: {m['content']}\n"
                
            prompt = (
                "You are an AI summarizing a phone call between 'MYRA' (calling from Riverwood Estate) "
                "and a 'USER'. \n\n"
                "Extract the key facts from this conversation so Myra remembers them:\n"
                "1. Did the user agree to visit the site or give a timeframe?\n"
                "2. What main questions did the user ask?\n"
                "3. What information has Myra already provided (so she doesn't repeat it)?\n\n"
                f"Conversation:\n{convo_text}\n\n"
                "Keep the summary extremely concise, bullet points only. Do not add conversational filler."
            )
            
            try:
                resp = await self.client.chat.completions.create(
                    messages=[{"role": "user", "content": prompt}],
                    model=self.model,
                    temperature=0.3, # Low temperature for factual extraction
                    max_tokens=150,
                )
                new_summary = resp.choices[0].message.content
                memory.summaries[session_id] = new_summary
            except Exception as e:
                print(f"Background Summary Error: {e}")

conv_engine = ConversationEngine()
