import json
from django.test import RequestFactory, Client
from django.contrib.auth import get_user_model
from django.contrib.sessions.middleware import SessionMiddleware
from ai_chatbot.views import ai_chat
from guest_app.models import AccommodationBooking

rf = RequestFactory()
User = get_user_model()
user, _ = User.objects.get_or_create(username='validation_guest_bot')


def build_request(message):
    req = rf.post('/api/chat/', data=json.dumps({'message': message}), content_type='application/json')
    middleware = SessionMiddleware(lambda r: None)
    middleware.process_request(req)
    req.session.save()
    req.user = user
    return req


def run_message(message):
    req = build_request(message)
    resp = ai_chat(req)
    payload = json.loads(resp.content.decode('utf-8'))
    return resp.status_code, payload

results = {}

before = AccommodationBooking.objects.count()
s_preview, p_preview = run_message('create booking preview for Sunset View Inn Family Room')
after = AccommodationBooking.objects.count()
results['preview_safety'] = {
    'status': s_preview,
    'booking_count_delta': after - before,
    'fulfillmentText': p_preview.get('fulfillmentText',''),
}

s1,p1 = run_message('show schedules for Bayawan City Highlights Day Tour')
results['tour_name_schedule'] = {
  'status': s1,
  'fulfillmentText': p1.get('fulfillmentText',''),
  'titles': [str((i or {}).get('title','')) for i in (p1.get('recommendation_trace') or [])],
}

s2,p2 = run_message('show tours on May 7 2026')
results['exact_date_schedule'] = {
  'status': s2,
  'fulfillmentText': p2.get('fulfillmentText',''),
  'subtitles': [str((i or {}).get('subtitle','')) for i in (p2.get('recommendation_trace') or [])],
}

s3,p3 = run_message('map')
results['map_short_prompt'] = {
  'status': s3,
  'fulfillmentText': p3.get('fulfillmentText',''),
  'quick_replies': p3.get('quick_replies') or [],
}

# Endpoint validation with safe host (avoid DisallowedHost in test client)
client = Client(HTTP_HOST='localhost')
client.force_login(user)
r = client.post('/api/chat/', data=json.dumps({'message':'help'}), content_type='application/json')
endpoint_payload = None
try:
    endpoint_payload = r.json()
except Exception:
    endpoint_payload = {'raw': r.content.decode('utf-8', errors='ignore')[:300]}
results['endpoint_api_chat'] = {
  'status': r.status_code,
  'payload': endpoint_payload,
}

print(json.dumps(results, indent=2, default=str))
