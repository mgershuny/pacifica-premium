"""
Pacifica Premium — AI Voice Agent
LLM-powered natural conversation for booking, FAQ, and transfers.
Uses DeepSeek API for intelligent extraction and response generation.
"""

import os, json, re, uuid, hashlib, urllib.request, urllib.error
from datetime import datetime, date

# ─── Config ───
TWILIO_PHONE = os.getenv('TWILIO_PHONE', '+143****8523')
DEEPSEEK_API_KEY = os.getenv('DEEPSEEK_API_KEY', '')
DEEPSEEK_MODEL = 'deepseek-chat'
DEEPSEEK_URL = 'https://api.deepseek.com/v1/chat/completions'
ELEVENLABS_API_KEY = os.getenv('ELEVENLABS_API_KEY', '')
ELEVENLABS_VOICE = 'nPczCjzI2devNBz1zQrb'  # Brian - Deep, Resonant and Comforting
AUDIO_DIR = os.path.join(os.path.dirname(__file__), 'audio')

# ─── ElevenLabs TTS ───

def synthesize_speech(text, voice_id=None):
    """Generate ElevenLabs Brian audio. Returns absolute path to MP3 file."""
    if not ELEVENLABS_API_KEY:
        return None
    
    voice_id = voice_id or ELEVENLABS_VOICE
    text_hash = hashlib.md5(text.encode()).hexdigest()[:12]
    filename = f"brian_{text_hash}.mp3"
    filepath = os.path.join(AUDIO_DIR, filename)
    
    # Return cached version if exists
    if os.path.exists(filepath):
        return filepath
    
    # Ensure audio directory exists
    os.makedirs(AUDIO_DIR, exist_ok=True)
    
    try:
        payload = json.dumps({
            "text": text,
            "model_id": "eleven_monolingual_v1",
            "voice_settings": {
                "stability": 0.35,
                "similarity_boost": 0.75,
            }
        }).encode('utf-8')
        
        req = urllib.request.Request(
            f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "xi-api-key": ELEVENLABS_API_KEY,
            },
            method="POST"
        )
        
        with urllib.request.urlopen(req, timeout=20) as resp:
            with open(filepath, 'wb') as f:
                f.write(resp.read())
        
        return filepath
    except Exception as e:
        print(f"ElevenLabs TTS error: {e}")
        return None

# ─── System Prompt ───

BOOKING_FIELDS = {
    "date": "Date of the ride (e.g. July 10th, 2026-07-10)",
    "time": "Time of pickup (e.g. 3 PM, 15:00)",
    "pickup": "Pickup address or location",
    "dropoff": "Dropoff destination",
    "passengers": "Number of passengers (a number)",
    "trip_type": "Type: airport run, long distance, or event/night out",
    "name": "Caller's full name for the booking",
    "phone": "Caller's phone number",
    "payment": "Payment method: credit card, PayPal, or cash",
}

SYSTEM_PROMPT = f"""You are the Pacifica Premium booking assistant — a real, warm customer service agent handling calls for a luxury chauffeur service in Toronto/GTA. Never mention you're AI.

ABOUT THE COMPANY:
- Chrysler Pacifica PHEV minivan, 6 passengers + driver + luggage
- USB-A and USB-C in all rows (no 120V outlets)
- Serving YYZ Pearson ($45 flat), YTZ Billy Bishop ($40 flat), YHM Hamilton, and all GTA
- Long distance trips from $75, events/night outs $55
- Accepts: credit card (via Stripe), PayPal, cash (collected at ride)
- Owned and operated by Musa — book online or by phone

BOOKING FIELDS TO COLLECT:
{json.dumps(BOOKING_FIELDS, indent=2)}

YOUR JOB:
Have a natural, flowing conversation. The caller can give info in ANY order — extract whatever they provide from each sentence. For example:
- "I need a ride to the airport on Friday at 3pm for 3 people" → extracts date, time, dropoff, passengers, trip_type
- "Pick me up at 123 Main Street" → extracts pickup
- "Actually make it 4 people" → updates passengers

Rules:
1. Be CONCISE — 1-2 short sentences per response. Sound like a real person.
2. Extract ANY booking fields the caller mentions, even if mixed with other conversation.
3. If a field value changes ("actually", "correction", "make it"), update it.
4. Only ask for fields that are still missing. Ask naturally.
5. If they ask a question about the company (rates, vehicle, areas, etc.), answer from the company info above.
6. If they ask for Musa directly ("talk to Musa", "let me speak to Musa"), set transfer_to_musa=true.
7. CONFIRMATION STEP — CRITICAL: When ALL fields have been collected for the FIRST time, DO NOT set all_collected=true yet. Instead, REPEAT BACK EVERYTHING clearly and ask for confirmation. For example: "Let me confirm everything: pickup at [address], going to [destination], on [date] at [time], [passengers] passengers, paid by [payment]. Is that all correct?"
8. After presenting the confirmation, if the caller says "yes", "correct", "that's right", "looks good", or confirms — THEN set all_collected=true.
9. If the caller says "no", "change", or corrects something — update that field and present the updated confirmation again.
10. If they're done or say goodbye, set farewell=true.
11. If you can't understand them, ask a clarifying question.
12. Keep your responses BRIEF — this is a phone call, not a chat.

RESPOND WITH VALID JSON ONLY:
{{
  "say": "What you say to the caller (natural, 1-2 sentences)",
  "extracted": {{
    "date": "value or omit if not provided",
    "time": "value or omit",
    "pickup": "value or omit",
    "dropoff": "value or omit",
    "passengers": "value or omit",
    "trip_type": "value or omit",
    "name": "value or omit",
    "phone": "value or omit",
    "payment": "value or omit"
  }},
  "all_collected": false,
  "farewell": false,
  "transfer_to_musa": false,
  "needs_clarification": null
}}

IMPORTANT RULES:
- Only include fields in "extracted" that the caller ACTUALLY provided in this turn. Omit fields they didn't mention.
- If they provided info that contradicts what was previously given, the new value wins.
- ALL_COLLECTED=true can ONLY be set AFTER confirmation — meaning the caller has explicitly confirmed all the details are correct. Do not shortcut this step.
- After confirmation (all_collected=true), the booking is final. The caller can still say goodbye or ask questions but the booking is saved.
"""


# ─── LLM API Call ───

def call_llm(messages, retries=2):
    """Call DeepSeek API with message history. Returns parsed JSON or None."""
    if not DEEPSEEK_API_KEY:
        return None

    payload = json.dumps({
        "model": DEEPSEEK_MODEL,
        "messages": messages,
        "temperature": 0.3,
        "max_tokens": 500,
    }).encode('utf-8')

    req = urllib.request.Request(
        DEEPSEEK_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        },
        method="POST"
    )

    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                result = json.loads(resp.read().decode('utf-8'))
                content = result['choices'][0]['message']['content']

                # Extract JSON from response (handle markdown-wrapped JSON)
                json_match = re.search(r'\{.*\}', content, re.DOTALL)
                if json_match:
                    return json.loads(json_match.group())
                return json.loads(content)
        except (urllib.error.URLError, json.JSONDecodeError, KeyError) as e:
            if attempt < retries - 1:
                continue
            return None


# ─── Booking Session ───

class BookingSession:
    """LLM-powered booking session. Collects fields in any order via conversation."""
    
    REQUIRED_FIELDS = ["date", "time", "pickup", "dropoff", "passengers", "name", "phone", "payment"]
    
    def __init__(self, call_sid):
        self.call_sid = call_sid
        self.state = "greeting"  # greeting, collecting, done
        self.data = {}
        self.history = []  # list of {"role": "assistant"/"user", "content": "..."}
    
    @property
    def missing_fields(self):
        return [f for f in self.REQUIRED_FIELDS if f not in self.data]
    
    @property
    def is_complete(self):
        return len(self.missing_fields) == 0
    
    def to_booking_data(self):
        pmt = self.data.get("payment", "").lower()
        return {
            "date": self.data.get("date", ""),
            "time": self.data.get("time", ""),
            "pickup": self.data.get("pickup", ""),
            "dropoff": self.data.get("dropoff", ""),
            "passengers": self.data.get("passengers", "1"),
            "trip": self._map_trip_type(self.data.get("trip_type", "")),
            "name": self.data.get("name", "Phone Booking"),
            "phone": self.data.get("phone", ""),
            "email": "phone@booking.com",
            "payment_method": "cash" if "cash" in pmt else "credit_card",
            "notes": "Booked via phone",
        }
    
    def _map_trip_type(self, raw):
        r = raw.lower()
        if any(w in r for w in ["airport", "yyz", "ytz", "pearson", "billy"]):
            return "Airport Transfer"
        if any(w in r for w in ["long", "distance", "far"]):
            return "Long Distance"
        if any(w in r for w in ["event", "night", "party", "concert", "dinner"]):
            return "Event / Night Out"
        return "Airport Transfer"
    
    def build_messages(self):
        """Build message list for LLM context."""
        msgs = [{"role": "system", "content": SYSTEM_PROMPT}]
        
        # Add conversation history
        msgs.extend(self.history)
        
        # Add current known state as a user message
        known = {k: v for k, v in self.data.items()}
        missing = self.missing_fields
        state_msg = f"[STATE] Known fields: {json.dumps(known)}\nMissing fields: {missing}"
        msgs.append({"role": "user", "content": state_msg})
        
        return msgs


# ─── Main Conversation Handler ───

def handle_conversation(session, user_speech):
    """
    Process user speech through LLM, update session, return response dict.
    
    Returns:
        dict with keys: say, is_complete, needs_transfer, booking_data, farewell
    """
    # Add user message to history
    session.history.append({"role": "user", "content": user_speech})
    
    # Trim history if too long (keep system + last 20 turns)
    while len(session.history) > 40:
        session.history.pop(0)
    
    # Call LLM
    messages = session.build_messages()
    result = call_llm(messages)
    
    # Fallback if LLM fails
    if not result:
        fallback = _fallback_response(session, user_speech)
        session.history.append({"role": "assistant", "content": fallback["say"]})
        return fallback
    
    # Extract fields from LLM response
    extracted = result.get("extracted", {})
    if extracted:
        for k, v in extracted.items():
            if v and k in BOOKING_FIELDS:
                session.data[k] = v
    
    # Determine response
    say = result.get("say", "")
    is_complete = result.get("all_collected", False) or session.is_complete
    needs_transfer = result.get("transfer_to_musa", False)
    farewell = result.get("farewell", False)
    
    # Add assistant response to history
    session.history.append({"role": "assistant", "content": say})
    
    response = {
        "say": say,
        "is_complete": is_complete,
        "needs_transfer": needs_transfer and not is_complete,
        "farewell": farewell,
        "booking_data": session.to_booking_data() if is_complete else None,
    }
    
    return response


def _fallback_response(session, user_speech):
    """Keyword-based fallback if LLM fails. Context-aware — stays in booking flow if fields already collected."""
    text = user_speech.lower()
    
    # If we're already in a booking flow (have some fields), ask about the next missing field
    if session.data:
        missing = session.missing_fields
        if missing:
            next_field = missing[0]
            prompts = {
                "date": "What date do you need the ride?",
                "time": "What time works for you?",
                "pickup": "Where should we pick you up?",
                "dropoff": "And where are you headed?",
                "passengers": "How many passengers?",
                "name": "What name should I put the booking under?",
                "phone": "And a phone number?",
                "payment": "Will that be credit card, PayPal, or cash?",
            }
            return {"say": f"I didn't quite catch that. {prompts.get(next_field, 'Can you tell me more?')}",
                    "is_complete": False, "needs_transfer": False, "farewell": False, "booking_data": None}
        # All fields collected but LLM failed on confirmation
        return {"say": "I'm sorry, I didn't catch that. Is everything correct?",
                "is_complete": False, "needs_transfer": False, "farewell": False, "booking_data": None}
    
    # Transfer
    if any(w in text for w in ["musa", "owner", "manager", "talk to", "speak to", "transfer", "human"]):
        return {"say": "One moment please, I'll transfer you to Musa.", "is_complete": False,
                "needs_transfer": True, "farewell": False, "booking_data": None}
    
    # Farewell
    if any(w in text for w in ["bye", "goodbye", "thank you", "thanks", "that's all", "that is all"]):
        return {"say": "You're welcome! Have a great day!", "is_complete": False,
                "needs_transfer": False, "farewell": True, "booking_data": None}
    
    # FAQ
    if any(w in text for w in ["rate", "price", "cost", "how much", "$"]):
        return {"say": "Our airport rate is $45 to YYZ, $40 to YTZ. Long distance from $75, events $55. All in CAD.", 
                "is_complete": False, "needs_transfer": False, "farewell": False, "booking_data": None}
    
    # Booking intent
    if any(w in text for w in ["book", "ride", "airport", "pick me", "need a ride", "schedule"]):
        if session.missing_fields:
            next_field = session.missing_fields[0]
            prompts = {
                "date": "What date do you need the ride?",
                "time": "What time works for you?",
                "pickup": "Where should we pick you up?",
                "dropoff": "And where are you headed?",
                "passengers": "How many passengers?",
                "name": "What name should I put the booking under?",
                "phone": "And a phone number?",
                "payment": "Will that be credit card, PayPal, or cash?",
            }
            return {"say": prompts.get(next_field, "Can you tell me more?"),
                    "is_complete": False, "needs_transfer": False, "farewell": False, "booking_data": None}
    
    # Generic fallback
    return {"say": "I'm sorry, I didn't quite catch that. Are you looking to book a ride or check our rates?",
            "is_complete": False, "needs_transfer": False, "farewell": False, "booking_data": None}


# ─── Session Store ───

BOOKING_SESSIONS = {}

def get_or_create_session(call_sid):
    if call_sid not in BOOKING_SESSIONS:
        BOOKING_SESSIONS[call_sid] = BookingSession(call_sid)
    return BOOKING_SESSIONS[call_sid]
