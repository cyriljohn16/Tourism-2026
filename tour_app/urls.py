from django.urls import path
from django.conf import settings
from django.conf.urls.static import static
from . import views

app_name = 'tour_app'


urlpatterns = [
    path('create/', views.add_tour, name='add_tour'),
    path('', views.home, name='home'),
    # path('itinerary/<str:sched_id>/', views.itinerary, name='itinerary'),  # sched_id is required!
    path('tour/<str:tour_id>/create_schedule/', views.create_schedule, name='create_schedule'),
    path('tour/<str:tour_id>/update_schedules/', views.update_schedules, name='update_schedules'),
    path('tour/<str:tour_id>/update_itinerary/', views.update_itinerary, name='update_itinerary'),
    path('itinerary/<str:sched_id>/', views.itinerary, name='itinerary'),  # Change tour_id to sched_id
    path('pending/', views.pending_view, name='pending_view'),
    path('pending/<int:pk>/edit/', views.StatusUpdateView.as_view(), name='status_update'),
    path('rates', views.add_admission_rate, name='admission_rate'),
    path('cancel/', views.cancel_tour_view, name='cancel_tour'),
    
    # API endpoints for AJAX functionality
    path('tour/<str:tour_id>/schedules/', views.get_tour_schedules_api, name='get_tour_schedules_api'),
    path('tour/<str:tour_id>/details/', views.get_tour_details, name='get_tour_details'),
    path('tour/<str:tour_id>/update/', views.update_tour, name='update_tour'),
    path('tour/<str:tour_id>/delete/', views.delete_tour, name='delete_tour'),
    path('schedule/<str:schedule_id>/', views.get_schedule_details, name='get_schedule_details'),
    path('schedule/<str:schedule_id>/update/', views.update_schedule, name='update_schedule'),
    path('schedule/<str:schedule_id>/events/', views.get_schedule_events, name='get_schedule_events'),
    path('schedule/<str:schedule_id>/events/add/', views.add_event, name='add_event'),
    path('schedule/<str:schedule_id>/payables/', views.get_schedule_payables, name='get_schedule_payables'),
    path('event/<str:event_id>/', views.get_event_details, name='get_event_details'),
    path('event/<str:event_id>/update/', views.update_event, name='update_event'),
    path('event/<str:event_id>/delete/', views.delete_event, name='delete_event'),
    path('rates/json/', views.get_admission_rates_json, name='admission_rates_json'),
    
    # Add translation management URLs
    path('tour/<str:tour_id>/translations/', views.update_tour_translation, name='tour_translations'),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
