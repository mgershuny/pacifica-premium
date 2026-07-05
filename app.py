import os, json, uuid, hmac, hashlib, base64, smtplib, email, threading, re
from datetime import datetime
from dotenv import load_dotenv
load_dotenv()
from flask import Flask, request, jsonify, send_from_directory, redirect, url_for, Response
import stripe
from twilio.twiml.voice_response import VoiceResponse, Gather
from voice_agent import (
    get_or_create_session, handle_conversation, synthesize_speech
)

app = Flask(__name__, static_folder='.', static_url_path='')
app.secret_key = os.urandom(32).hex()

stripe.api_key = os.getenv('STRIPE_SECRET_KEY')
MG_EMAIL = os.getenv('MG_EMAIL', 'mgershuny@gmail.com')
APP_URL = os.getenv('APP_URL', 'http://127.0.0.1:5000')
CALENDAR_ID = os.getenv('PACIFICA_CALENDAR_ID')
TWILIO_PHONE = os.getenv('TWILIO_PHONE', '+143****8523')
MG_PHONE = os.getenv('MG_PHONE', '')

BOOKINGS_FILE = os.path.join(os.path.dirname(__file__), 'bookings.json')
TOKEN_FILE = os.path.join(os.path.dirname(__file__), 'mg_token.txt')
CONFIRM_TOKEN = os.getenv('MG_CONFIRM_TOKEN') or 'pacifica-confirm-2026'

# ─── Load/save bookings ───
def load_bookings():
    if os.path.exists(BOOKINGS_FILE):
        with open(BOOKINGS_FILE) as f:
            return json.load(f)
    return []

def save_booking(booking):
    bookings = load_bookings()
    booking['id'] = str(uuid.uuid4())[:8]
    booking['created_at'] = datetime.utcnow().isoformat()
    booking['status'] = 'pending'
    bookings.append(booking)
    with open(BOOKINGS_FILE, 'w') as f:
        json.dump(bookings, f, indent=2)
    return booking

def update_booking(booking_id, updates):
    bookings = load_bookings()
    for b in bookings:
        if b['id'] == booking_id:
            b.update(updates)
            break
    with open(BOOKINGS_FILE, 'w') as f:
        json.dump(bookings, f, indent=2)

def get_booking(booking_id):
    for b in load_bookings():
        if b['id'] == booking_id:
            return b
    return None

# ─── Email MG ───
def send_email(to, subject, body_html):
    sender = os.getenv('MG_EMAIL', 'mgershuny@gmail.com')
    app_pass = os.getenv('GMAIL_APP_PASS')
    if not app_pass:
        print(f"\n=== EMAIL TO {to} ===\nSubject: {subject}\n{body_html}\n===================\n")
        return True
    try:
        msg = email.message.EmailMessage()
        msg['From'] = sender
        msg['To'] = to
        msg['Subject'] = subject
        msg.set_content(body_html, 'html')
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as smtp:
            smtp.login(sender, app_pass)
            smtp.send_message(msg)
        return True
    except Exception as e:
        print(f"Email error: {e}")
        return False

# ─── Calendar IDs for availability ───
ALL_CALENDARS = {
    "primary": "mgershuny@gmail.com",
    "family": "family00535914979446525255@group.calendar.google.com",
    "pacifica": os.getenv('PACIFICA_CALENDAR_ID', ''),
    "celine": "oat9mdj24ateup8s3gqbsn01cs@group.calendar.google.com",
    "holidays": "en.canadian#holiday@group.v.calendar.google.com",
}

def _get_calendar_service():
    """Get authenticated Google Calendar service. Uses GOOGLE_TOKEN_B64 env var or local token file."""
    import base64
    token_b64 = os.getenv('GOOGLE_TOKEN_B64')
    if token_b64:
        token_json = base64.b64decode(token_b64).decode('utf-8')
        cd = json.loads(token_json)
    else:
        token_path = os.path.expanduser('~/.hermes/google_token.json')
        with open(token_path) as f:
            cd = json.load(f)
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    creds = Credentials(
        token=cd['token'], refresh_token=cd['refresh_token'],
        token_uri='https://oauth2.googleapis.com/token',
        client_id=cd['client_id'], client_secret=cd['client_secret']
    )
    return build('calendar', 'v3', credentials=creds)

@app.route('/api/availability')
def availability():
    """Return available time slots for a date. Checks all 5 calendars for busy periods."""
    date_str = request.args.get('date', '')
    if not date_str:
        return jsonify({'error': 'date required'}), 400

    try:
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        import datetime as dtmod
        date = dtmod.date.fromisoformat(date_str)
        tz = dtmod.timezone(dtmod.timedelta(hours=-4))  # EDT

        time_min = dtmod.datetime(date.year, date.month, date.day, 6, 0, tzinfo=tz)
        time_max = dtmod.datetime(date.year, date.month, date.day, 23, 0, tzinfo=tz)
        now = dtmod.datetime.now(tz)

        service = _get_calendar_service()

        items = [{"id": cid} for cid in ALL_CALENDARS.values() if cid]
        body = {"timeMin": time_min.isoformat(), "timeMax": time_max.isoformat(), "items": items}
        result = service.freebusy().query(body=body).execute()

        busy_periods = []
        for cal_id, data in result.get('calendars', {}).items():
            for period in data.get('busy', []):
                busy_periods.append({'start': period['start'], 'end': period['end'], 'calendar': cal_id})
        busy_periods.sort(key=lambda x: x['start'])

        slots = []
        current = time_min
        while current < time_max:
            slot_end = current + dtmod.timedelta(minutes=30)
            s_iso, e_iso = current.isoformat(), slot_end.isoformat()

            is_busy = False
            for bp in busy_periods:
                if s_iso < bp['end'] and e_iso > bp['start']:
                    is_busy = True
                    break
            time_str = current.strftime('%H:%M')
            is_past = time_str <= now.strftime('%H:%M') and date_str == now.strftime('%Y-%m-%d')

            slots.append({
                'time': time_str,
                'available': not is_busy and not is_past,
                'past': is_past,
            })
            current = slot_end

        return jsonify({'date': date_str, 'slots': slots})
    except Exception as e:
        print(f"Availability error: {e}")
        import traceback; traceback.print_exc()
        return jsonify({'error': str(e)}), 500

# ─── Google Calendar event ───
def create_calendar_event(booking):
    try:
        service = _get_calendar_service()

        start_dt = f"{booking['date']}T{booking['time']}:00"
        import datetime as dtmod
        start_parsed = dtmod.datetime.fromisoformat(start_dt)
        end_parsed = start_parsed + dtmod.timedelta(hours=2)

        pmt = booking.get('payment_method', 'unknown')
        pmt_status = '✅ Paid' if booking.get('paid') else ('💵 Cash' if pmt == 'cash' else '⏳ Pending')
        notes = booking.get('notes', '')
        pickup = booking.get('pickup', '')
        dropoff = booking.get('dropoff', '')
        pax = booking.get('passengers', '')
        trip = booking.get('trip', '')

        event = {
            'summary': f"🚗 {booking['name']} — {trip}",
            'description': (
                f"From: {booking.get('name')} ({booking.get('phone')}, {booking.get('email')})\n"
                f"Pickup: {pickup}\n"
                f"Dropoff: {dropoff}\n"
                f"Passengers: {pax}\n"
                f"Trip: {trip}\n"
                f"Payment: {pmt_status} ({pmt})\n"
                f"Notes: {notes}\n"
                f"Booking ID: {booking['id']}"
            ),
            'location': pickup,
            'start': {'dateTime': start_parsed.isoformat(), 'timeZone': 'America/Toronto'},
            'end': {'dateTime': end_parsed.isoformat(), 'timeZone': 'America/Toronto'},
            'colorId': '7',
        }
        created = service.events().insert(calendarId=CALENDAR_ID, body=event).execute()
        return created.get('htmlLink', '')
    except Exception as e:
        print(f"Calendar error: {e}")
        return None

# ─── Routes ───

@app.route('/')
def index():
    return send_from_directory('.', 'index.html')

@app.route('/images/<path:filename>')
def images(filename):
    return send_from_directory('images', filename)

@app.route('/audio/<path:filename>')
def audio(filename):
    return send_from_directory('audio', filename)

@app.route('/api/config')
def config():
    return jsonify({'stripe_key': os.getenv('STRIPE_PUBLISHABLE_KEY')})

@app.route('/api/book', methods=['POST'])
def book():
    data = request.json
    required = ['date', 'time', 'name', 'phone', 'pickup', 'dropoff', 'passengers', 'trip', 'payment_method']
    for field in required:
        if not data.get(field):
            return jsonify({'error': f'{field} is required'}), 400

    pmt = data['payment_method']
    data['paid'] = False
    booking = save_booking(data)
    booking_id = booking['id']

    if pmt == 'cash':
        booking['status'] = 'pending'
        update_booking(booking_id, {'status': 'pending'})
        _notify_mg(booking)
        return jsonify({
            'status': 'pending',
            'booking_id': booking_id,
            'message': 'Booking submitted! Musa will confirm shortly.'
        })

    try:
        amount_map = {
            'To Airport (YYZ)': 4500,
            'From Airport (YYZ)': 4500,
            'To Airport (YTZ)': 4000,
            'From Airport (YTZ)': 4000,
            'Long Distance': 7500,
            'Event / Night Out': 5500,
        }
        amount = amount_map.get(data['trip'], 4500)

        session = stripe.checkout.Session.create(
            mode='payment',
            payment_method_types=['card'] if pmt == 'credit_card' else ['card', 'paypal'],
            line_items=[{
                'price_data': {
                    'currency': 'cad',
                    'product_data': {
                        'name': f"Pacifica Premium — {data['trip']}",
                        'description': f"{data['date']} at {data['time']} — {data['pickup']} → {data['dropoff']}"
                    },
                    'unit_amount': amount,
                },
                'quantity': 1,
            }],
            metadata={'booking_id': booking_id},
            success_url=f"{APP_URL}/api/booking/success?booking_id={booking_id}&session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{APP_URL}/?cancelled=true",
        )

        update_booking(booking_id, {
            'stripe_session_id': session.id,
            'payment_amount': amount,
            'status': 'awaiting_payment'
        })

        return jsonify({
            'status': 'redirect',
            'booking_id': booking_id,
            'checkout_url': session.url,
        })

    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/booking/success')
def booking_success():
    booking_id = request.args.get('booking_id')
    session_id = request.args.get('session_id')
    if booking_id:
        update_booking(booking_id, {'paid': True, 'status': 'paid'})
        booking = get_booking(booking_id)
        if booking:
            _notify_mg(booking)
    return redirect(f"/?booking={booking_id}&status=paid")

@app.route('/api/stripe/webhook', methods=['POST'])
def stripe_webhook():
    payload = request.get_data()
    sig_header = request.headers.get('Stripe-Signature')
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, os.getenv('STRIPE_WEBHOOK_SECRET', ''))
    except (ValueError, stripe.error.SignatureVerificationError):
        return '', 400

    if event['type'] == 'checkout.session.completed':
        session = event['data']['object']
        booking_id = session.get('metadata', {}).get('booking_id')
        if booking_id:
            update_booking(booking_id, {'paid': True, 'status': 'paid'})
            booking = get_booking(booking_id)
            if booking:
                _notify_mg(booking)
    return '', 200

@app.route('/api/confirm/<token>')
def confirm_booking(token):
    if token != CONFIRM_TOKEN:
        return 'Invalid confirmation link', 403
    booking_id = request.args.get('id')
    if not booking_id:
        return 'Missing booking ID', 400
    booking = get_booking(booking_id)
    if not booking:
        return 'Booking not found', 404
    if booking['status'] in ('confirmed', 'cancelled'):
        return f'Booking already {booking["status"]}', 400

    cal_link = create_calendar_event(booking)
    update_booking(booking_id, {'status': 'confirmed', 'cal_link': cal_link})

    pmt = booking.get('payment_method', '')
    pmt_info = '✅ Paid' if booking.get('paid') else ('💵 Cash (collect at ride)' if pmt == 'cash' else '⏳')

    msg = f"""<!DOCTYPE html><html><body style="font-family:sans-serif;padding:2rem;background:#0a0a0a;color:#f0f0f0">
<h1 style="color:#c8a46e">✅ Booking Confirmed!</h1>
<p><strong>{booking['name']}</strong> — {booking['date']} at {booking['time']}</p>
<p>{booking['pickup']} → {booking['dropoff']}</p>
<p>Payment: {pmt_info}</p>
<p>Calendar event created ✓</p>
<hr style="border-color:#333">
<p style="color:#888;font-size:0.85rem">Booking ID: {booking_id}</p>
<a href="/" style="color:#c8a46e">← Back to page</a>
</body></html>"""
    return msg

@app.route('/api/bookings/pending')
def pending_bookings():
    pw = request.args.get('pw')
    if pw != CONFIRM_TOKEN[:8]:
        return jsonify({'error': 'unauthorized'}), 403
    bookings = [b for b in load_bookings() if b['status'] in ('pending', 'paid')]
    return jsonify(bookings)

# ─── Helpers ───
def _notify_mg(booking):
    bid = booking['id']
    pmt = booking.get('payment_method', '')
    paid = booking.get('paid', False)
    amount = booking.get('payment_amount', 0)

    pmt_str = f"${amount/100:.2f} via {'Card' if pmt == 'credit_card' else 'PayPal'} ✓ PAID" if paid else (
        '💵 Cash (collect at ride)' if pmt == 'cash' else '⏳ Not yet paid'
    )

    confirm_link = f"{APP_URL}/api/confirm/{CONFIRM_TOKEN}?id={bid}"
    view_link = f"{APP_URL}/?booking={bid}"

    html = f"""<div style="font-family:sans-serif;max-width:600px;margin:auto">
<h2 style="color:#c8a46e">🚗 New Pacifica Booking</h2>
<table style="width:100%;border-collapse:collapse;margin:1rem 0">
<tr><td style="padding:8px;color:#888">Name</td><td style="padding:8px"><strong>{booking['name']}</strong></td></tr>
<tr style="background:#111"><td style="padding:8px;color:#888">Date</td><td style="padding:8px">{booking['date']}</td></tr>
<tr><td style="padding:8px;color:#888">Time</td><td style="padding:8px">{booking['time']}</td></tr>
<tr style="background:#111"><td style="padding:8px;color:#888">Pickup</td><td style="padding:8px">{booking.get('pickup','')}</td></tr>
<tr><td style="padding:8px;color:#888">Dropoff</td><td style="padding:8px">{booking.get('dropoff','')}</td></tr>
<tr style="background:#111"><td style="padding:8px;color:#888">Passengers</td><td style="padding:8px">{booking.get('passengers','')}</td></tr>
<tr><td style="padding:8px;color:#888">Trip</td><td style="padding:8px">{booking.get('trip','')}</td></tr>
<tr style="background:#111"><td style="padding:8px;color:#888">Payment</td><td style="padding:8px">{pmt_str}</td></tr>
<tr><td style="padding:8px;color:#888">Phone</td><td style="padding:8px">{booking.get('phone','')}</td></tr>
<tr style="background:#111"><td style="padding:8px;color:#888">Email</td><td style="padding:8px">{booking.get('email','')}</td></tr>
<tr><td style="padding:8px;color:#888">Notes</td><td style="padding:8px">{booking.get('notes','—')}</td></tr>
</table>
<p style="margin:1.5rem 0">
<a href="{confirm_link}" style="display:inline-block;padding:1rem 2rem;background:#c8a46e;color:#000;text-decoration:none;border-radius:50px;font-weight:600">
✅ Confirm & Add to Calendar
</a></p>
<p style="color:#888;font-size:0.85rem;margin-top:1rem">
Booking ID: {bid}<br>
<a href="{view_link}" style="color:#c8a46e">View on site</a>
</p></div>"""
    send_email(MG_EMAIL, f"🚗 New Booking — {booking['name']} {booking['date']}", html)


# ─── Voice Agent Endpoints (Twilio) ───

def _play_or_say(text, gather=None):
    """Try ElevenLabs Brian first, fall back to Polly Matthew."""
    audio_path = synthesize_speech(text)
    if audio_path:
        # Convert absolute path to URL
        filename = os.path.basename(audio_path)
        audio_url = f"{APP_URL}/audio/{filename}"
        if gather:
            gather.play(audio_url)
        else:
            return audio_url
    else:
        if gather:
            gather.say(text, voice='Polly.Matthew', language='en-US')
        return None

def _make_gather(text):
    """Create a Gather with Brian voice (or Polly fallback)."""
    gather = Gather(input='speech', action='/voice/response', method='POST',
                    speechTimeout='auto', timeout='5')
    _play_or_say(text, gather)
    return gather


@app.route('/voice/incoming', methods=['GET', 'POST'])
def voice_incoming():
    """Entry point for incoming calls."""
    try:
        call_sid = request.values.get('CallSid', 'unknown')
        session = get_or_create_session(call_sid)
        session.state = "greeting"

        resp = VoiceResponse()
        greeting = (
            "Hello! You've reached Pacifica Premium. This is your chauffeur service for "
            "the Toronto area. I can help you book a ride or answer questions about our service. "
            "How can I help you today?"
        )
        gather = _make_gather(greeting)
        resp.append(gather)
        return Response(str(resp), mimetype='text/xml')
    except Exception as e:
        print(f"Error in /voice/incoming: {e}")
        resp = VoiceResponse()
        resp.say("Sorry, we're having a technical issue. Please try again later.")
        resp.hangup()
        return Response(str(resp), mimetype='text/xml')


@app.route('/voice/response', methods=['POST'])
def voice_response():
    """Handle speech input from caller — LLM-powered conversation with Brian voice."""
    try:
        call_sid = request.values.get('CallSid', 'unknown')
        speech = request.values.get('SpeechResult', '')

        session = get_or_create_session(call_sid)
        resp = VoiceResponse()

        if not speech.strip():
            gather = _make_gather("I didn't catch that. Could you repeat it?")
            resp.append(gather)
            return Response(str(resp), mimetype='text/xml')

        # LLM handles everything — booking in any order, FAQ, transfers, goodbye
        result = handle_conversation(session, speech)

        if result.get("needs_transfer"):
            if MG_PHONE and MG_PHONE != 'PLACEHOLDER':
                # Short transfer message doesn't need Brian
                resp.say("One moment please, I'll transfer you to Musa.", voice='Polly.Matthew')
                resp.dial(MG_PHONE, callerId=TWILIO_PHONE, action='/voice/end')
            else:
                # Continue the conversation instead of restarting
                say = "I'm sorry, Musa isn't available to take calls right now, but I can definitely help you with a booking or answer any questions. What can I do for you?"
                audio_url = _play_or_say(say)
                gather = Gather(input='speech', action='/voice/response', method='POST',
                                speechTimeout='auto', timeout='5')
                if audio_url:
                    gather.play(audio_url)
                else:
                    gather.say(say, voice='Polly.Matthew')
                resp.append(gather)
            return Response(str(resp), mimetype='text/xml')

        if result.get("is_complete"):
            booking_data = result["booking_data"]
            booking = save_booking(booking_data)
            _notify_mg(booking)
            session.state = "done"
            msg = (
                f"Perfect, your booking is confirmed! Let me recap: "
                f"pickup at {booking_data['pickup']} going to {booking_data['dropoff']}, "
                f"on {booking_data['date']} at {booking_data['time']}, "
                f"for {booking_data['passengers']} passengers, "
                f"paid by {booking_data.get('payment_method', 'card')}. "
                f"Your reference is {booking['id']}. "
                f"Musa will review this shortly. "
                f"If you need anything else, just ask. Otherwise, have a great day!"
            )
            gather = _make_gather(msg)
            resp.append(gather)
            return Response(str(resp), mimetype='text/xml')

        if result.get("farewell"):
            # For farewell use Brian if possible, else Polly
            audio_url = _play_or_say(result["say"])
            if audio_url:
                resp.play(audio_url)
            else:
                resp.say(result["say"], voice='Polly.Matthew')
            resp.hangup()
            return Response(str(resp), mimetype='text/xml')

        # Normal conversation turn
        say = result.get("say", "")
        audio_url = _play_or_say(say)
        gather = Gather(input='speech', action='/voice/response', method='POST',
                        speechTimeout='auto', timeout='5')
        if audio_url:
            gather.play(audio_url)
        else:
            gather.say(say, voice='Polly.Matthew')
        resp.append(gather)
        return Response(str(resp), mimetype='text/xml')

    except Exception as e:
        print(f"Error in /voice/response: {e}")
        import traceback
        traceback.print_exc()
        resp = VoiceResponse()
        resp.say("Sorry about that! Let me connect you with Musa.")
        if MG_PHONE and MG_PHONE != 'PLACEHOLDER':
            resp.dial(MG_PHONE, callerId=TWILIO_PHONE, action='/voice/end')
        else:
            resp.say("Please try again later.", voice='Polly.Matthew')
            resp.hangup()
        return Response(str(resp), mimetype='text/xml')


@app.route('/voice/end', methods=['POST'])
def voice_end():
    """Called after a transfer or when call ends."""
    resp = VoiceResponse()
    resp.hangup()
    return Response(str(resp), mimetype='text/xml')


# ─── Start ───
if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    print(f"Pacifica Premium server on :{port}")
    app.run(host='0.0.0.0', port=port, debug=True, use_reloader=False)
