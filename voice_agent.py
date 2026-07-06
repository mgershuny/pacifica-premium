"""
Pacifica Premium — AI Voice Agent
LLM-powered natural conversation for booking, FAQ, and transfers.
Uses DeepSeek API with function calling for intelligent data retrieval + response generation.
"""

import os, json, re, uuid, hashlib, urllib.request, urllib.error
import requests
from datetime import datetime, date

# ─── Config ───
TWILIO_PHONE = os.getenv('TWILIO_PHONE', '+143****8523')
DEEPSEEK_API_KEY = os.getenv('DEEPSEEK_API_KEY', '')
DEEPSEEK_MODEL = 'deepseek-chat'
DEEPSEEK_URL = 'https://api.deepseek.com/v1/chat/completions'
ELEVENLABS_API_KEY = os.getenv('ELEVENLABS_API_KEY', '')
ELEVENLABS_VOICE = 'nPczCjzI2devNBz1zQrb'  # Brian - Deep, Resonant and Comforting
AUDIO_DIR = os.path.join(os.path.dirname(__file__), 'audio')

# ─── Today's date for LLM context ───
TODAY_DATE = datetime.now().strftime("%A, %B %d, %Y")

# ─── Travel Time Calculator (free, no API key) ───

def geocode(address):
    """Geocode an address to (lat, lng) using Nominatim."""
    try:
        url = "https://nominatim.openstreetmap.org/search"
        params = {"q": address, "format": "json", "limit": 1}
        resp = requests.get(url, params=params,
                            headers={"User-Agent": "PacificaPremium/1.0"},
                            timeout=10)
        data = resp.json()
        if data:
            return float(data[0]["lat"]), float(data[0]["lon"])
    except Exception as e:
        print(f"[geocode] Error: {e}")
    return None

def calculate_drive_time(pickup, dropoff):
    """Get driving time in minutes between two addresses using OSRM. Returns int or None."""
    try:
        origin = geocode(pickup)
        dest = geocode(dropoff)
        if not origin or not dest:
            return None
        url = (f"https://router.project-osrm.org/route/v1/driving/"
               f"{origin[1]},{origin[0]};{dest[1]},{dest[0]}?overview=false")
        resp = requests.get(url, timeout=10)
        data = resp.json()
        if data.get("code") == "Ok":
            seconds = data["routes"][0]["duration"]
            return int(seconds / 60)
    except Exception as e:
        print(f"[drive_time] Error: {e}")
    return None

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

# ─── Tool Definitions ───

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "resolve_location",
            "description": "Resolve a vague location ('home', 'my house', 'my place', 'the office', 'downtown') to a real street address. Looks up saved addresses for returning callers. Returns the resolved address or 'needs_address' if unclear.",
            "parameters": {
                "type": "object",
                "properties": {
                    "description": {
                        "type": "string",
                        "description": "What the caller said about the location (e.g. 'my home', '123 Main St', 'the airport', 'work')"
                    },
                    "caller_phone": {
                        "type": "string",
                        "description": "The caller's phone number to check for saved addresses. Pass empty string if unknown."
                    }
                },
                "required": ["description"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "calculate_price",
            "description": "Calculate the estimated price for a ride based on pickup and dropoff locations. Handles flat airport rates, distance-based pricing, and event rates.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pickup": {
                        "type": "string",
                        "description": "Pickup address or location name"
                    },
                    "dropoff": {
                        "type": "string",
                        "description": "Dropoff address or destination"
                    },
                    "trip_type": {
                        "type": "string",
                        "enum": ["airport", "long_distance", "event", "unknown"],
                        "description": "Type of trip. 'airport' = YYZ/YTZ flat rates, 'long_distance' = $75+, 'event' = $55 flat"
                    }
                },
                "required": ["pickup", "dropoff"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "lookup_caller",
            "description": "Look up a caller by phone number. Returns their name and past addresses from previous bookings. Use this to personalize greetings and recognize returning customers.",
            "parameters": {
                "type": "object",
                "properties": {
                    "phone": {
                        "type": "string",
                        "description": "Caller's phone number"
                    }
                },
                "required": ["phone"]
            }
        }
    }
]

# ─── Tool Implementations ───

def _resolve_location(description, caller_phone=""):
    """Resolve a vague location description to a real address."""
    desc_lower = description.lower().strip()
    
    # Check if it's already an address-like string (contains numbers, street types, etc.)
    has_address_pattern = bool(re.search(r'\d+\s+\w+\s+(street|st|avenue|ave|road|rd|drive|dr|blvd|boulevard|crescent|cres|way|lane|ln|circle|cres|court|crt|gate|terrace|terr)', desc_lower))
    
    if has_address_pattern:
        return {
            "resolved": description,
            "source": "provided",
            "needs_confirmation": True,
            "needs_full_address": False
        }
    
    # Check saved addresses for returning callers
    if caller_phone and any(w in desc_lower for w in ["home", "house", "place", "my place", "same"]):
        saved = get_caller_addresses(caller_phone)
        if saved:
            return {
                "resolved": saved[0],
                "source": "saved",
                "needs_confirmation": True,
                "needs_full_address": False,
                "alternatives": saved[1:] if len(saved) > 1 else []
            }
    
    # Generic location words — needs full address
    vague_words = ["home", "my house", "my place", "the house", "house", "work", "office", "my office", "downtown", "uptown", "my work", "the office", "the shop", "my shop"]
    if any(w == desc_lower or desc_lower.startswith(w) or desc_lower.endswith(w) for w in vague_words):
        return {
            "resolved": None,
            "source": "vague",
            "needs_full_address": True,
            "hint": f"Caller said '{description}'. Ask for their street address."
        }
    
    # Airport/common destinations
    airport_names = ["yyz", "pearson", "toronto pearson", "ytz", "billy bishop", "toronto airport", "airport"]
    if any(a in desc_lower for a in airport_names):
        return {
            "resolved": "Toronto Pearson International Airport (YYZ)" if any(a in desc_lower for a in ["yyz", "pearson"]) else "Billy Bishop Toronto City Airport (YTZ)",
            "source": "airport",
            "needs_confirmation": True,
            "needs_full_address": False
        }
    
    # It's a specific-sounding location but not a full address
    return {
        "resolved": description,
        "source": "provided",
        "needs_confirmation": True,
        "needs_full_address": False
    }


def _calculate_price(pickup, dropoff, trip_type="unknown"):
    """Calculate estimated price for a ride."""
    p_lower = (pickup + " " + dropoff).lower()
    
    # Airport flat rates
    to_yyz = any(w in p_lower for w in ["yyz", "pearson"])
    to_ytz = any(w in p_lower for w in ["ytz", "billy bishop"])
    
    if to_yyz:
        return {
            "price": 45,
            "currency": "CAD",
            "type": "Airport Transfer - YYZ",
            "note": "$45 flat rate to/from Toronto Pearson"
        }
    if to_ytz:
        return {
            "price": 40,
            "currency": "CAD",
            "type": "Airport Transfer - YTZ",
            "note": "$40 flat rate to/from Billy Bishop"
        }
    
    if trip_type == "event":
        return {
            "price": 55,
            "currency": "CAD",
            "type": "Event / Night Out",
            "note": "$55 flat rate for events and nights out in the GTA"
        }
    
    if trip_type == "long_distance":
        return {
            "price": 75,
            "currency": "CAD",
            "type": "Long Distance",
            "note": "Starting from $75 for long distance trips, varies by distance"
        }
    
    return {
        "price": None,
        "currency": "CAD",
        "type": "unknown",
        "note": "Airport rates: $45 YYZ, $40 YTZ. Long distance from $75. Events $55. All CAD.",
        "needs_clarification": True
    }


def _lookup_caller(phone):
    """Look up caller info by phone number."""
    try:
        name = get_caller_name(phone)
        addresses = get_caller_addresses(phone) if name else []
        return {
            "name": name,
            "past_addresses": addresses,
            "is_returning": name is not None
        }
    except Exception as e:
        print(f"[lookup_caller] Error: {e}")
        return {"name": None, "past_addresses": [], "is_returning": False}


# ─── Tool Dispatcher ───

def execute_tool(name, args):
    """Execute a tool by name with parsed arguments. Returns dict result."""
    try:
        if name == "resolve_location":
            return _resolve_location(
                args.get("description", ""),
                args.get("caller_phone", "")
            )
        elif name == "calculate_price":
            return _calculate_price(
                args.get("pickup", ""),
                args.get("dropoff", ""),
                args.get("trip_type", "unknown")
            )
        elif name == "lookup_caller":
            return _lookup_caller(args.get("phone", ""))
        else:
            return {"error": f"Unknown tool: {name}"}
    except Exception as e:
        return {"error": f"Tool {name} failed: {str(e)}"}


# ─── System Prompt ───

BOOKING_FIELDS = {
    "date": "Date of the ride (e.g. July 10th, 2026-07-10)",
    "time": "Time of pickup (e.g. 3 PM, 15:00)",
    "pickup": "Pickup address or location",
    "dropoff": "Dropoff destination",
    "passengers": "Number of passengers (a number 1-6)",
    "trip_type": "Type: airport run, long distance, or event/night out",
    "flight_time": "ONLY if caller mentions a flight departure time (e.g. 'my flight is at 5pm'). Extract this SEPARATELY from the pickup time.",
    "name": "Caller's full name for the booking",
    "phone": "Caller's phone number",
    "email": "Caller's email address",
    "payment": "Payment method: credit card, PayPal, or cash",
}

SYSTEM_PROMPT = f"""You are the Pacifica Premium booking assistant — a real, warm customer service agent handling calls for a luxury chauffeur service in Toronto/GTA. Never mention you're AI.

TODAY IS: {TODAY_DATE}. Always resolve relative dates (like "Friday", "tomorrow", "next Monday") against today's date. Output dates as "Month Day, Year" (e.g. "July 10, 2026").

ABOUT THE COMPANY:
- Chrysler Pacifica PHEV minivan, 6 passengers + driver + luggage
- USB-A and USB-C in all rows (no 120V outlets)
- Serving YYZ Pearson ($45 flat), YTZ Billy Bishop ($40 flat), YHM Hamilton, and all GTA
- Long distance trips from $75, events/night outs $55
- Accepts: credit card (via Stripe), PayPal, cash (collected at ride)
- Owned and operated by Musa — book online or by phone

BOOKING FIELDS TO COLLECT:
{json.dumps(BOOKING_FIELDS, indent=2)}

CRITICAL — YOU HAVE TOOLS AVAILABLE. USE THEM.
- resolve_location: When caller says "home", "my house", "my place", "work", "the office", or any vague location. Call this tool BEFORE accepting it as a pickup address. It will return the real address or tell you to ask for the full address.
- calculate_price: When caller asks about rates, pricing, or before confirming a booking. Don't guess prices — use the tool.
- lookup_caller: Use this at the start of the call if you have the caller's phone number. It returns their name and past addresses for a personalized greeting.

YOUR JOB:
Have a natural, flowing conversation. The caller can give info in ANY order — extract whatever they provide from each sentence. For example:
- "I need a ride to the airport on Friday at 3pm for 3 people" → extracts date, time, dropoff, passengers, trip_type
- "Pick me up at 123 Main Street" → extracts pickup
- "Actually make it 4 people" → updates passengers

PASSENGER COUNT RULES (CRITICAL — DO NOT GET THIS WRONG):
- "just me", "just myself", "just 1", "by myself" → passengers = 1
- "me and my wife", "my wife and I", "me and 1 other" → passengers = 2
- "me and 2 others", "myself and 2 friends", "my wife and I and our son" → passengers = 3
- "me and 3 others" → passengers = 4
- Any number given literally: "3 people", "2 passengers" → that exact number
- MAXIMUM 6 passengers (the vehicle holds 6 + the driver). If they say more than 6, say "I can take up to 6 passengers. Would you like to split into two trips or adjust?"

FLIGHT TIME LOGIC (CRITICAL — SMART PICKUP TIMES):
- When a caller mentions a flight departure time (e.g. "my flight leaves at 5pm", "I need to catch a 3pm flight"), extract it into the "flight_time" field.
- Do NOT set the "time" field to the flight time — the "time" field is for PICKUP time, not flight time.
- After you have flight_time AND pickup AND dropoff ALL three, set needs_travel_calc=true in your response. This triggers the system to calculate drive time and suggest an optimal pickup time.
- The system will calculate: arrival_time = flight_time - airport_buffer (2h domestic, 3h international), then pickup_time = arrival_time - drive_time.
- After the system calculates this, you'll receive the result in the next turn as a state update: "Travel calculation: pickup by [time], arrives [arrival_time], drive is [X] min."
- Then suggest this to the caller: "Your flight is at 5pm, so you'd need to be at the airport by 3pm. With traffic, the drive takes about 45 minutes. I'll pick you up at 2:15pm. How does that sound?"
- If they agree, set "time" to the calculated pickup time. If they want a different time, update it.

Rules:
1. Be CONCISE — 1-2 short sentences per response. Sound like a real person.
2. Extract ANY booking fields the caller mentions, even if mixed with other conversation.
3. If a field value changes ("actually", "correction", "make it"), update it.
4. Only ask for fields that are still missing. Ask naturally. If they give you a date, follow up with what time — if they give you a time, mention the date if it's still missing. Always connect the two.
5. RETURNING CALLERS — If the [STATE] shows that name is already filled in or that this is a returning caller, DO NOT ask for their name. Name is already known. Skip directly to any other missing fields.
6. CONFIRM EACH FIELD — When the caller provides a piece of information, repeat it back to confirm before moving on. For example:
   - They say "Friday" → "Friday July 10th, got it. And what time?"
   - They say "3pm" → "3 PM, noted. And where should I pick you up?"
   - They say "cash" → "Cash, got it."
   - They say "john@gmail.com" → "john@gmail.com, is that correct?"
7. CONFIRM EMAIL — When they provide their email, repeat it back and ask them to confirm it's correct before saving it.
8. CONFIRM PAYMENT — When they say cash, credit card, or PayPal, repeat it back for confirmation.
9. If they ask a question about the company (rates, vehicle, areas, trip types, payment methods, etc.), answer from the company info above — those are booking-related and fine to answer.
10. CONVERSATION GUARDRAILS — You are a booking assistant ONLY. Topics you can discuss: booking a ride, rates, vehicle info, service areas, payment, airport info. Topics you CANNOT discuss and must redirect on: weather, sports, news, politics, personal advice, your own capabilities or identity (never say you're AI), the caller's personal life, jokes or casual chat unrelated to booking, complaints about anything outside the service, or any topic that has nothing to do with the caller's transportation needs. If the caller tries to go off-topic, say: "I'm here to help with your booking. If there's something else you need, I'll need to transfer you to Musa." If they persist or you can't keep them on track, set transfer_to_musa=true.
11. If they ask for Musa directly ("talk to Musa", "let me speak to Musa"), set transfer_to_musa=true.
12. CONFIRMATION STEP — CRITICAL: When ALL fields have been collected for the FIRST time, DO NOT set all_collected=true yet. Instead, REPEAT BACK EVERYTHING clearly and ask for confirmation. For example: "Let me confirm everything: pickup at [address], going to [destination], on [date] at [time], [passengers] passengers, paid by [payment], confirmation to [email]. Is that all correct?"
13. After presenting the confirmation, if the caller says "yes", "correct", "that's right", "looks good", or confirms — THEN set all_collected=true.
14. If the caller says "no", "change", or corrects something — update that field and present the updated confirmation again.
15. BOOKING COMPLETE — After all_collected=true, tell them they're all booked. Give them a booking reference (generate a short 6-character code like "PAC-ABC123"). Say they'll get a confirmation email at their email address. Ask if there's anything else they need.
16. If they're done or say goodbye, set farewell=true.
17. If you can't understand them, ask a clarifying question.
18. Keep your responses BRIEF — this is a phone call, not a chat.
19. PICKUP ADDRESS HANDLING — When the caller says a generic location instead of a full address:
    - "home", "my house", "my place", "the house", "my home", "pick me up at home" → use resolve_location tool to get their saved address or ask for street address
    - "work", "my office", "my job", "from work" → use resolve_location tool
    - "same as before", "same place", "the usual", "like last time" → use resolve_location tool
    - DO NOT just accept "my home", "home", "my place" as the pickup value — it's not a drivable address. Always use resolve_location to get or ask for the real address.

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
    "flight_time": "value or omit",
    "name": "value or omit",
    "phone": "value or omit",
    "email": "value or omit",
    "payment": "value or omit"
  }},
  "all_collected": false,
  "farewell": false,
  "transfer_to_musa": false,
  "needs_travel_calc": false,
  "needs_clarification": null
}}

IMPORTANT RULES:
|- Only include fields in "extracted" that the caller ACTUALLY provided in this turn. Omit fields they didn't mention.
|- If they provided info that contradicts what was previously given, the new value wins.
|- ALL_COLLECTED=true can ONLY be set AFTER confirmation — meaning the caller has explicitly confirmed all the details are correct. Do not shortcut this step.
|- After confirmation (all_collected=true), the booking is final. Generate a booking reference like "PAC-" followed by 3 random uppercase letters and 3 random digits (e.g. "PAC-XRT742"). Tell the caller their booking reference and that they'll receive a confirmation email. The caller can still say goodbye or ask questions but the booking is saved.
"""


# ─── LLM API Call with Tool Support ───

def _raw_llm_call(messages, tools=None, retries=2):
    """Raw DeepSeek API call. Returns the full response dict or None."""
    if not DEEPSEEK_API_KEY:
        return None

    payload_dict = {
        "model": DEEPSEEK_MODEL,
        "messages": messages,
        "temperature": 0.3,
        "max_tokens": 500,
    }
    if tools:
        payload_dict["tools"] = tools

    payload_str = json.dumps(payload_dict)
    payload = payload_str.encode('utf-8')

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
            with urllib.request.urlopen(req, timeout=20) as resp:
                body = resp.read().decode('utf-8')
                if not body or not body.strip():
                    print(f"[_raw_llm_call] Empty response body (attempt {attempt+1})")
                    continue
                return json.loads(body)
        except urllib.error.HTTPError as e:
            err_body = e.read().decode('utf-8', errors='replace')
            print(f"[_raw_llm_call] HTTP {e.code}: {err_body[:500]}")
            if attempt < retries - 1:
                continue
            return None
        except (urllib.error.URLError, json.JSONDecodeError, KeyError) as e:
            print(f"[_raw_llm_call] Error (attempt {attempt+1}): {e}")
            if attempt < retries - 1:
                continue
            return None
    return None


def call_llm_with_tools(messages, max_tool_rounds=6):
    """Call LLM with tool support. Keeps calling tools until LLM returns text JSON.
    
    Each round: LLM responds → if tool_calls → execute → append result → repeat
    When LLM returns text → extract JSON and return it.
    
    Returns: parsed JSON dict with say/extracted etc., or None on failure.
    """
    # Start with system prompt + history + state
    working_messages = list(messages)
    
    for round_num in range(max_tool_rounds):
        response = _raw_llm_call(working_messages, tools=TOOLS)
        if not response:
            return None
        
        try:
            message = response['choices'][0]['message']
        except (KeyError, IndexError):
            return None
        
        # Check if LLM wants to call tools
        tool_calls = message.get('tool_calls')
        if tool_calls:
            # Add assistant message with tool calls to history
            assistant_msg = {
                "role": "assistant",
                "content": message.get('content') or None
            }
            # Add any content it said before calling the tool
            if message.get('content'):
                assistant_msg["content"] = message['content']
            assistant_msg["tool_calls"] = tool_calls
            working_messages.append(assistant_msg)
            
            # Execute each tool and append results
            for tc in tool_calls:
                func_name = tc['function']['name']
                try:
                    func_args = json.loads(tc['function']['arguments'])
                except json.JSONDecodeError:
                    func_args = {}
                
                result = execute_tool(func_name, func_args)
                working_messages.append({
                    "role": "tool",
                    "tool_call_id": tc['id'],
                    "content": json.dumps(result)
                })
            
            # Trim working_messages if too long (keep system + last 30)
            while len(working_messages) > 30:
                # Remove oldest non-system messages
                for i, msg in enumerate(working_messages):
                    if msg['role'] != 'system' and msg['role'] != 'tool':
                        working_messages.pop(i)
                        break
            
            continue  # Go again with tool results fed back
        
        # No tool calls — extract JSON content
        content = message.get('content', '')
        if not content:
            print(f"[call_llm] Empty content (round {round_num+1})")
            return None
        
        # Extract JSON from response (handle markdown-wrapped JSON)
        try:
            json_match = re.search(r'\{.*\}', content, re.DOTALL)
            if json_match:
                return json.loads(json_match.group())
            return json.loads(content)
        except (json.JSONDecodeError, ValueError) as e:
            print(f"[call_llm] JSON parse error (round {round_num+1}): {e}")
            print(f"[call_llm] Raw content: {content[:500]}")
            return None
    
    return None  # Hit max tool rounds


def call_llm(messages, retries=2):
    """Legacy: Call DeepSeek API without tools. Returns parsed JSON or None."""
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
    
    REQUIRED_FIELDS = ["date", "time", "pickup", "dropoff", "passengers", "name", "phone", "email", "payment"]
    
    def __init__(self, call_sid):
        self.call_sid = call_sid
        self.state = "greeting"  # greeting, collecting, done
        self.data = {}
        self.travel_calc = {}  # {drive_minutes, arrival_buffer, suggested_pickup, arrival_by}
        self.returning_name = None  # Set by app.py when returning caller detected
        self.saved_addresses = []  # Past pickup addresses for returning callers
        self.silence_count = 0  # Consecutive silent/empty responses
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
            "email": self.data.get("email", "phone@booking.com"),
            "payment_method": "cash" if "cash" in pmt else "credit_card",
            "flight_time": self.data.get("flight_time", ""),
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
        state_msg = f"[STATE] Known fields: {json.dumps(known)}\nMissing fields: {missing}\nCaller phone: {self.data.get('phone', 'unknown')}"
        
        # Include identity confirmation context
        if self.returning_name and not self.data.get("name"):
            first = self.returning_name.split()[0]
            state_msg += (
                f"\n[Caller context] This caller was greeted as '{first}' based on their phone number. "
                f"They were asked if they're {first} calling back. "
                f"If they confirm, set name=\"{self.returning_name}\". "
                f"If they deny or give a different name, use that instead."
            )
        
        # Include travel calc if available
        if self.travel_calc:
            tc = self.travel_calc
            state_msg += (
                f"\n[Travel calculation] Drive time: {tc.get('drive_minutes','?')} min. "
                f"Airport buffer: {tc.get('arrival_buffer','?')} min. "
                f"Suggested pickup: {tc.get('suggested_pickup','?')}. "
                f"Arrive by: {tc.get('arrival_by','?')}."
            )
        
        # Include saved addresses if pickup is still needed and caller has history
        if not self.data.get("pickup") and self.saved_addresses:
            addr_list = "\n".join(f"  {i+1}. \"{a}\"" for i, a in enumerate(self.saved_addresses))
            state_msg += (
                f"\n[Caller's saved addresses from past bookings]"
                f"\n{addr_list}"
                f"\nCall resolve_location to confirm which address they want."
            )
        
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
    
    # Call LLM with tool support
    messages = session.build_messages()
    result = call_llm_with_tools(messages)
    
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
    needs_travel_calc = result.get("needs_travel_calc", False)
    
    # Add assistant response to history
    session.history.append({"role": "assistant", "content": say})
    
    response = {
        "say": say,
        "is_complete": is_complete,
        "needs_transfer": needs_transfer and not is_complete,
        "farewell": farewell,
        "needs_travel_calc": needs_travel_calc and not is_complete,
        "booking_data": session.to_booking_data() if is_complete else None,
    }
    
    return response


def _fallback_response(session, user_speech):
    """Fallback extraction + response if LLM fails. Uses regex to grab fields directly."""
    text = user_speech.lower().strip()

    # ─── Try to extract fields with regex ───
    extracted = {}

    # Time patterns: 3pm, 3:00, 3 o'clock, noon, midnight, 2:30pm, 3 in the afternoon
    time_pats = [
        r'(\d{1,2}):(\d{2})\s*(pm|am|p\.m\.|a\.m\.)',  # 3:00pm, 2:30 AM
        r'(\d{1,2})\s*(pm|am|p\.m\.|a\.m\.|:00)',       # 3pm, 3 am, 3:00
        r'(\d{1,2})\s*o\'?clock',                         # 3 o'clock
        r'(noon|midnight|midday)',                         # noon, midnight
        r'(\d{1,2})\s*in\s*the\s*(morning|afternoon|evening)',  # 3 in the afternoon
    ]
    for pat in time_pats:
        m = re.search(pat, text)
        if m:
            grps = m.groups()
            if grps[0] in ('noon', 'midnight', 'midday'):
                extracted['time'] = grps[0].title()
            elif ':' in m.group(0):
                h, mm, suf = grps
                extracted['time'] = f"{h}:{mm}{suf}".upper().replace(' ', '')
            elif grps[-1] in ('morning', 'afternoon', 'evening'):
                extracted['time'] = f"{grps[0]}{' AM' if grps[-1]=='morning' else ' PM'}"
            else:
                h, suf = grps[0], (grps[1] if len(grps) > 1 else 'PM').upper()
                extracted['time'] = f"{h} {suf}"
            break

    # Date patterns: monday, friday, july 10th, tomorrow, next week
    days = ['monday','tuesday','wednesday','thursday','friday','saturday','sunday']
    months = ['january','february','march','april','may','june','july','august','september','october','november','december']
    if 'tomorrow' in text or 'tomorow' in text:
        extracted['date'] = 'tomorrow'
    elif 'today' in text:
        extracted['date'] = 'today'
    else:
        for d in days:
            if d in text:
                extracted['date'] = d.title()
                break
        for m in months:
            if m in text:
                # Try to extract "July 10th" or "July 10"
                dm = re.search(r'(%s)\s+(\d{1,2})(?:st|nd|rd|th)?' % m, text)
                if dm:
                    extracted['date'] = f"{dm.group(1).title()} {dm.group(2)}"
                else:
                    extracted['date'] = m.title()
                break

    # Passengers: "3 people", "2 passengers", "for 4", "just me", "me and 2 others"
    pm = re.search(r'(\d+)\s*(?:people|passengers|pax|adults?|guests?)', text)
    if pm:
        extracted['passengers'] = pm.group(1)
    elif re.search(r'\bjust\s*(?:me|myself)\b', text) or re.search(r'\bby\s*myself\b', text):
        extracted['passengers'] = '1'
    else:
        # "me and X others", "myself and X", "my wife and I and..."
        ma = re.search(r'(?:me|myself|my\s+\w+)\s+and\s+(\d+)\s+(?:others?|friends?|people|guests?)', text)
        if ma:
            extracted['passengers'] = str(int(ma.group(1)) + 1)
        else:
            ma2 = re.search(r'(?:me|myself)\s+and\s+(\d+)', text)
            if ma2:
                extracted['passengers'] = str(int(ma2.group(1)) + 1)

    # Flight time: "flight at 5pm", "flight leaves at 3", "5pm flight"
    ft = re.search(r'(?:flight|plane)\s+(?:at|leaves?|is|departs?|for)\s+(\d{1,2})(?::(\d{2}))?\s*(pm|am|p\.m\.|a\.m\.)?', text)
    if not ft:
        ft = re.search(r'(\d{1,2})(?::(\d{2}))?\s*(pm|am|p\.m\.|a\.m\.)?\s+(?:flight|plane)', text)
    if ft:
        h, m, suf = ft.groups()
        suf = (suf or 'PM').upper().replace('.', '').replace(' ', '')
        if m:
            extracted['flight_time'] = f"{h}:{m} {suf}"
        else:
            extracted['flight_time'] = f"{h} {suf}"

    # Phone: basic North American pattern
    ph = re.search(r'(\+?1?\s*\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4})', text)
    if ph:
        extracted['phone'] = ph.group(1).strip()

    # Email: basic email pattern
    em = re.search(r'([\w.+-]+@[\w-]+\.[\w.]+)', user_speech)
    if em:
        extracted['email'] = em.group(1).strip()

    # Name: anything after "name is" or "it's" or "this is"
    nm = re.search(r'(?:name\'?s|name is|this is|it\'?s)\s+([A-Za-z]+(?:\s+[A-Za-z]+)?)', user_speech)
    if nm:
        extracted['name'] = nm.group(1).strip().title()

    # Pickup: "at [location]", "from [location]", "pick me up at [location]"
    pk = re.search(r'(?:at|from|pick\s+(?:me\s+)?up\s+(?:at|from)?)\s+(.+?)(?:\s+(?:to|and|going|for|at)\s+|$)', user_speech)
    if pk and len(pk.group(1)) > 3:
        extracted['pickup'] = pk.group(1).strip().rstrip('.,')

    # Dropoff: "to [destination]", "going to [destination]", "headed to"
    dr = re.search(r'(?:(?:going|headed|need|get)\s+)?(?:to)\s+(.+?)(?:\.|$)', user_speech)
    if dr and len(dr.group(1)) > 3:
        extracted['dropoff'] = dr.group(1).strip().rstrip('.,')

    # Payment method
    if any(w in text for w in ['cash']):
        extracted['payment'] = 'cash'
    elif any(w in text for w in ['credit', 'visa', 'mastercard', 'card', 'debit']):
        extracted['payment'] = 'credit card'
    elif any(w in text for w in ['paypal']):
        extracted['payment'] = 'PayPal'

    # Trip type
    if any(w in text for w in ['airport', 'pearson', 'yyz', 'ytz', 'billy', 'billy bishop']):
        extracted['trip_type'] = 'airport'
    elif any(w in text for w in ['long', 'distance', 'far']):
        extracted['trip_type'] = 'long distance'
    elif any(w in text for w in ['event', 'night', 'party', 'concert', 'dinner']):
        extracted['trip_type'] = 'event'

    # ─── Apply extracted fields ───
    for k, v in extracted.items():
        if v and k in BOOKING_FIELDS:
            session.data[k] = v

    # ─── Now respond based on current state ───
    if session.data:
        missing = session.missing_fields
        if missing:
            next_field = missing[0]
            prompts = {
                "date": "What date do you need the ride?",
                "time": "What time works for you?",
                "pickup": "Where should I pick you up?",
                "dropoff": "And where are you headed?",
                "passengers": "How many passengers?",
                "name": "What name should I put the booking under?",
                "phone": "And a phone number?",
                "email": "And an email for your confirmation?",
                "payment": "Will that be credit card, PayPal, or cash?",
            }
            return {"say": prompts.get(next_field, "Can you tell me more?"),
                    "is_complete": False, "needs_transfer": False, "farewell": False, "booking_data": None}
        return {"say": "Let me confirm: is everything correct?",
                "is_complete": False, "needs_transfer": False, "farewell": False, "booking_data": None}

    # ─── Transfer ───
    if any(w in text for w in ["musa", "owner", "manager", "talk to", "speak to", "transfer", "human"]):
        return {"say": "One moment please, I'll transfer you to Musa.", "is_complete": False,
                "needs_transfer": True, "farewell": False, "booking_data": None}

    # ─── Farewell ───
    if any(w in text for w in ["bye", "goodbye", "thank you", "thanks", "that's all", "that is all"]):
        return {"say": "You're welcome! Have a great day!", "is_complete": False,
                "needs_transfer": False, "farewell": True, "booking_data": None}

    # ─── FAQ ───
    if any(w in text for w in ["rate", "price", "cost", "how much", "$"]):
        return {"say": "Our airport rate is $45 to YYZ, $40 to YTZ. Long distance from $75, events $55. All in CAD.",
                "is_complete": False, "needs_transfer": False, "farewell": False, "booking_data": None}

    # ─── Booking intent ───
    if any(w in text for w in ["book", "ride", "airport", "pick me", "need a ride", "schedule"]):
        if session.missing_fields:
            next_field = session.missing_fields[0]
            prompts = {
                "date": "What date do you need the ride?",
                "time": "What time works for you?",
                "pickup": "Where should I pick you up?",
                "dropoff": "And where are you headed?",
                "passengers": "How many passengers?",
                "name": "What name should I put the booking under?",
                "phone": "And a phone number?",
                "email": "And an email for your confirmation?",
                "payment": "Will that be credit card, PayPal, or cash?",
            }
            return {"say": prompts.get(next_field, "Can you tell me more?"),
                    "is_complete": False, "needs_transfer": False, "farewell": False, "booking_data": None}

    # ─── Generic ───
    return {"say": "I'm sorry, I didn't quite catch that. Are you looking to book a ride or check our rates?",
            "is_complete": False, "needs_transfer": False, "farewell": False, "booking_data": None}


# ─── Session Store ───

BOOKING_SESSIONS = {}

def get_or_create_session(call_sid):
    if call_sid not in BOOKING_SESSIONS:
        BOOKING_SESSIONS[call_sid] = BookingSession(call_sid)
    return BOOKING_SESSIONS[call_sid]


def get_caller_name(phone):
    """Look up a caller's phone number in past bookings to greet them by name."""
    import json, os
    bookings_file = os.path.join(os.path.dirname(__file__), 'bookings.json')
    if not os.path.exists(bookings_file):
        return None
    # Normalize phone: strip +1, spaces, dashes, parens
    norm = phone.replace('+1', '').replace('+', '').replace(' ', '').replace('-', '').replace('(', '').replace(')', '')
    try:
        with open(bookings_file) as f:
            bookings = json.load(f)
        names_seen = {}
        for b in bookings:
            bp = (b.get('phone', '') or '').replace('+1', '').replace('+', '').replace(' ', '').replace('-', '').replace('(', '').replace(')', '')
            if norm in bp or bp in norm or (len(norm) > 6 and len(bp) > 6 and norm[-6:] == bp[-6:]):
                name = b.get('name', '').strip()
                if name and len(name) > 1:
                    names_seen[name] = names_seen.get(name, 0) + 1
        if names_seen:
            # Return the most common name associated with this number
            return max(names_seen, key=names_seen.get)
    except:
        pass
    return None


def get_caller_addresses(phone):
    """Look up past pickup addresses for a caller by phone number.
    
    Returns a list of unique pickup addresses from past bookings, most recent first.
    """
    import json, os
    bookings_file = os.path.join(os.path.dirname(__file__), 'bookings.json')
    if not os.path.exists(bookings_file):
        return []
    norm = phone.replace('+1', '').replace('+', '').replace(' ', '').replace('-', '').replace('(', '').replace(')', '')
    try:
        with open(bookings_file) as f:
            bookings = json.load(f)
        addresses = []
        seen = set()
        for b in reversed(bookings):
            bp = (b.get('phone', '') or '').replace('+1', '').replace('+', '').replace(' ', '').replace('-', '').replace('(', '').replace(')', '')
            if norm in bp or bp in norm or (len(norm) > 6 and len(bp) > 6 and norm[-6:] == bp[-6:]):
                addr = (b.get('pickup', '') or '').strip()
                if addr and len(addr) > 3 and addr not in seen:
                    addresses.append(addr)
                    seen.add(addr)
        return addresses
    except:
        return []
