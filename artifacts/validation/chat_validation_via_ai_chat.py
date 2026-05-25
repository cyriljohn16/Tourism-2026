import json
from django.test import RequestFactory
from django.contrib.auth import get_user_model
from django.contrib.sessions.middleware import SessionMiddleware
from ai_chatbot.views import ai_chat
from guest_app.models import AccommodationBooking
from admin_app.models import Room, Accomodation

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

s1,p1 = run_message('show rooms for Sunset View Inn')
a1 = AccommodationBooking.objects.count()
results['case1'] = {
  'status': s1,
  'fulfillmentText': p1.get('fulfillmentText',''),
  'trace_kinds': [str((i or {}).get('kind','')) for i in (p1.get('recommendation_trace') or [])],
  'trace_titles': [str((i or {}).get('title','')) for i in (p1.get('recommendation_trace') or [])],
  'has_preview_marker': bool(p1.get('accommodation_preview_completed')),
  'booking_count_delta': a1-before,
}

s2,p2 = run_message('create booking preview for Sunset View Inn Family Room')
a2 = AccommodationBooking.objects.count()
results['case2'] = {
  'status': s2,
  'fulfillmentText': p2.get('fulfillmentText',''),
  'trace_kinds': [str((i or {}).get('kind','')) for i in (p2.get('recommendation_trace') or [])],
  'quick_replies': p2.get('quick_replies') or [],
  'link_actions': p2.get('link_actions') or [],
  'billing_link': p2.get('billing_link',''),
  'billing_link_label': p2.get('billing_link_label',''),
  'missing_slot': p2.get('missing_slot',''),
  'needs_clarification': bool(p2.get('needs_clarification')),
  'booking_count_delta': a2-a1,
}

s3,p3 = run_message('show schedules for Bayawan City Highlights Day Tour')
results['case3'] = {
  'status': s3,
  'fulfillmentText': p3.get('fulfillmentText',''),
  'trace_kinds': [str((i or {}).get('kind','')) for i in (p3.get('recommendation_trace') or [])],
  'trace_titles': [str((i or {}).get('title','')) for i in (p3.get('recommendation_trace') or [])],
}

s4,p4 = run_message('show tours on May 7 2026')
results['case4'] = {
  'status': s4,
  'fulfillmentText': p4.get('fulfillmentText',''),
  'trace_kinds': [str((i or {}).get('kind','')) for i in (p4.get('recommendation_trace') or [])],
  'trace_subtitles': [str((i or {}).get('subtitle','')) for i in (p4.get('recommendation_trace') or [])],
}

short_prompts = ['hotel','accommodation','rooms','tours','book','schedule','map','directions','reports','help']
short = {}
for m in short_prompts:
  st,pl = run_message(m)
  short[m] = {
    'status': st,
    'fulfillmentText': pl.get('fulfillmentText',''),
    'quick_replies': pl.get('quick_replies') or [],
    'has_trace': bool(pl.get('recommendation_trace')),
  }
results['case5'] = short

with open('guest_app/templates/components/guest_chat_widget.html','r',encoding='utf-8') as f:
  txt=f.read().lower()
results['case6']={
  'view_literal': 'view my tour bookings' in txt,
  'open_literal': 'open my tour bookings' in txt,
  'redirect_path_literal': '/guest_app/tour-bookings/' in txt,
}

results['context']={
  'sunset_exists': Accomodation.objects.filter(company_name__iexact='Sunset View Inn').exists(),
  'sunset_rooms': list(Room.objects.filter(accommodation__company_name__iexact='Sunset View Inn').values_list('room_name', flat=True)[:10]),
  'accommodation_booking_count_after': AccommodationBooking.objects.count(),
}

print(json.dumps(results, indent=2, default=str))
