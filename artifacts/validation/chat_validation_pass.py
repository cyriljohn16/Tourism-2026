import json
from django.test import Client
from django.contrib.auth import get_user_model
from guest_app.models import AccommodationBooking
from admin_app.models import Room, Accomodation

results = {}
User = get_user_model()
user, created = User.objects.get_or_create(username='validation_guest_bot')
if created:
    try:
        user.set_password('pass1234')
    except Exception:
        pass
    if hasattr(user, 'email') and not getattr(user, 'email', None):
        user.email = 'validation_guest_bot@example.com'
    for attr, val in [('first_name','Validation'),('last_name','Guest')]:
        if hasattr(user, attr):
            setattr(user, attr, val)
    try:
        user.save()
    except Exception:
        pass

client = Client()
client.force_login(user)
before_accom_bookings = AccommodationBooking.objects.count()

def chat(msg):
    r = client.post('/api/chat/', data=json.dumps({'message': msg}), content_type='application/json')
    try:
        payload = r.json()
    except Exception:
        payload = {'_raw': r.content.decode('utf-8', errors='ignore')[:1000]}
    return r.status_code, payload

status1, payload1 = chat('show rooms for Sunset View Inn')
after1 = AccommodationBooking.objects.count()
results['case1'] = {
    'status': status1,
    'fulfillmentText': payload1.get('fulfillmentText',''),
    'trace_kinds': [str((i or {}).get('kind','')) for i in (payload1.get('recommendation_trace') or [])],
    'trace_titles': [str((i or {}).get('title','')) for i in (payload1.get('recommendation_trace') or [])],
    'has_preview_markers': bool(payload1.get('accommodation_preview_completed')),
    'booking_count_delta': after1 - before_accom_bookings,
}

status2, payload2 = chat('create booking preview for Sunset View Inn Family Room')
after2 = AccommodationBooking.objects.count()
results['case2'] = {
    'status': status2,
    'fulfillmentText': payload2.get('fulfillmentText',''),
    'has_reco_trace': bool(payload2.get('recommendation_trace')),
    'trace_kinds': [str((i or {}).get('kind','')) for i in (payload2.get('recommendation_trace') or [])],
    'quick_replies': payload2.get('quick_replies') or [],
    'has_link_actions': bool(payload2.get('link_actions')),
    'billing_link': payload2.get('billing_link',''),
    'billing_link_label': payload2.get('billing_link_label',''),
    'missing_slot': payload2.get('missing_slot',''),
    'needs_clarification': bool(payload2.get('needs_clarification')),
    'booking_count_delta': after2 - after1,
}

status3, payload3 = chat('show schedules for Bayawan City Highlights Day Tour')
results['case3'] = {
    'status': status3,
    'fulfillmentText': payload3.get('fulfillmentText',''),
    'trace_kinds': [str((i or {}).get('kind','')) for i in (payload3.get('recommendation_trace') or [])],
    'trace_titles': [str((i or {}).get('title','')) for i in (payload3.get('recommendation_trace') or [])],
    'quick_replies': payload3.get('quick_replies') or [],
}

status4, payload4 = chat('show tours on May 7 2026')
results['case4'] = {
    'status': status4,
    'fulfillmentText': payload4.get('fulfillmentText',''),
    'trace_kinds': [str((i or {}).get('kind','')) for i in (payload4.get('recommendation_trace') or [])],
    'trace_subtitles': [str((i or {}).get('subtitle','')) for i in (payload4.get('recommendation_trace') or [])],
    'quick_replies': payload4.get('quick_replies') or [],
}

short_prompts = ['hotel','accommodation','rooms','tours','book','schedule','map','directions','reports','help']
short_results = {}
for prompt in short_prompts:
    st, pl = chat(prompt)
    short_results[prompt] = {
        'status': st,
        'fulfillmentText': pl.get('fulfillmentText',''),
        'quick_replies': pl.get('quick_replies') or [],
        'has_reco_trace': bool(pl.get('recommendation_trace')),
    }
results['case5'] = short_results

with open('guest_app/templates/components/guest_chat_widget.html', 'r', encoding='utf-8') as f:
    txt = f.read().lower()
results['case6'] = {
    'contains_view_my_tour_bookings_redirect_literal': 'view my tour bookings' in txt,
    'contains_open_my_tour_bookings_redirect_literal': 'open my tour bookings' in txt,
    'contains_target_path_literal': '/guest_app/tour-bookings/' in txt,
}

results['data_context'] = {
    'sunset_view_inn_exists': Accomodation.objects.filter(company_name__iexact='Sunset View Inn').exists(),
    'sunset_view_inn_room_matches': list(Room.objects.filter(accommodation__company_name__iexact='Sunset View Inn').values_list('room_name', flat=True)[:10]),
    'total_accommodation_bookings_after': AccommodationBooking.objects.count(),
}

print(json.dumps(results, indent=2, default=str))
