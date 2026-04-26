from django.urls import path
from . import views

app_name = 'admin_app'

urlpatterns = [
    # Authentication routes
    path('login/', views.login, name='login'),
    path('forgot-password/', views.forgot_password, name='forgot_password'),
    path('reset-password/', views.reset_password, name='reset_password'),
    path('admin/logout/', views.admin_logout, name='admin_logout'),

    # Dashboard routes
    path('employee_dashboard/', views.employee_dashboard, name='employee_dashboard'),
    path('admin_dashboard/', views.admin_dashboard, name='admin_dashboard'),
    path('accommodation_dashboard/', views.accommodation_dashboard, name='accommodation_dashboard'),
    path('owner/dashboard/', views.owner_dashboard_entry, name='owner_dashboard_entry'),
    path('owner/hub/', views.owner_hub, name='owner_hub'),
    path('owner/accommodation/<int:accom_id>/edit/', views.owner_edit_accommodation, name='owner_edit_accommodation'),
    path('owner/bookings/', views.owner_accommodation_bookings, name='owner_accommodation_bookings'),
    path('owner/bookings/<int:booking_id>/update/', views.owner_accommodation_booking_update, name='owner_accommodation_booking_update'),
    path('owner/reports/submit/', views.owner_report_submit, name='owner_report_submit'),
    path('owner/reports/review/', views.owner_reports_review, name='owner_reports_review'),
    path('owner/room-bookings/<int:room_id>/', views.owner_room_bookings_json, name='owner_room_bookings_json'),
    path('owner/room-bookings/check-in/', views.owner_room_bookings_check_in, name='owner_room_bookings_check_in'),
    path('establishment_dashboard/', views.establishment_summary, name='establishment_dashboard'),
    path('map/', views.map_view, name='map'),
    path('tour_calendar/', views.tour_calendar, name='tour_calendar'),
    path('activity-tracker/', views.activity_tracker, name='activity_tracker'),
    path('survey-results/', views.survey_results_dashboard, name='survey_results_dashboard'),
    path('api/survey-results/', views.survey_results_api, name='survey_results_api'),
    path('tourism-information/', views.tourism_information_manage, name='tourism_information_manage'),
    path('tourism-information/add/', views.tourism_information_create, name='tourism_information_create'),
    path('tourism-information/<int:tourism_info_id>/edit/', views.tourism_information_edit, name='tourism_information_edit'),
    path('tourism-information/<int:tourism_info_id>/publish/', views.tourism_information_publish, name='tourism_information_publish'),
    path('tourism-information/<int:tourism_info_id>/archive/', views.tourism_information_archive, name='tourism_information_archive'),
    path('mainpage-photos/', views.mainpage_photos, name='mainpage_photos'),
    
    # Employee specific routes
    path('employee/assigned-tours/', views.employee_assigned_tours, name='employee_assigned_tours'),
    path('employee/tour-calendar/', views.employee_tour_calendar, name='employee_tour_calendar'),
    path('employee/accommodations/', views.employee_accommodations, name='employee_accommodations'),
    path('employee/map/', views.employee_map_view, name='employee_map'),
    path('employee/notifications/', views.employee_notifications, name='employee_notifications'),
    path('notifications/feed/', views.notifications_feed, name='notifications_feed'),
    path('notifications/open/<int:notification_id>/', views.notification_open, name='notification_open'),
    path('notifications/mark-all-read/', views.notifications_mark_all_read, name='notifications_mark_all_read'),
    path('employee/profile/', views.employee_profile, name='employee_profile'),

    # Employee management routes
    path('register/', views.employee_register, name='employee-register'),
    path('employees/pending/', views.pending_employees, name='pending_employees'),
    path('employees/update/<int:emp_id>/', views.update_employees, name='update_employee'),

    # Accommodation management routes
    path('accommodation/register/', views.accommodation_register, name='accommodation_register'),
    path('accommodation/<int:accom_id>/manage-rooms/', views.owner_manage_rooms, name='owner_manage_rooms'),
    path('accommodation/create/', views.create_accommodation, name='create_accommodation'),
    path('accommodation/update/<int:pk>/', views.accommodation_update, name='accommodation_update'),
    path('accommodation/pending/', views.pending_accommodation, name='pending_accommodation'),
    path('accommodation/owners/pending/', views.pending_accommodation_owners, name='pending_accommodation_owners'),
    path('accommodation/owners/<str:user_id>/update/', views.accommodation_owner_update, name='accommodation_owner_update'),
    path('accommodation/bookings/', views.accommodation_bookings, name='accommodation_bookings'),
    path('accommodation/bookings/<int:booking_id>/update/', views.accommodation_booking_update, name='accommodation_booking_update'),

    # Establishment management route
    path('add_establishment/', views.admin_create_form, name='admin_create_form'),

    # AJAX endpoints for immediate database actions
    # Region endpoints
    path('ajax/add_region/', views.ajax_add_region, name='ajax_add_region'),
    path('ajax/edit_region/', views.ajax_edit_region, name='ajax_edit_region'),
    path('ajax/delete_region/', views.ajax_delete_region, name='ajax_delete_region'),

    # Country endpoints
    path('ajax/add_country/', views.ajax_add_country, name='ajax_add_country'),
    path('ajax/edit_country/', views.ajax_edit_country, name='ajax_edit_country'),
    path('ajax/delete_country/', views.ajax_delete_country, name='ajax_delete_country'),

    # Entry endpoints
    path('ajax/add_entry/', views.ajax_add_entry, name='ajax_add_entry'),
    path('ajax/edit_entry/', views.ajax_edit_entry, name='ajax_edit_entry'),
    path('ajax/delete_entry/', views.ajax_delete_entry, name='ajax_delete_entry'),

    path('ajax_mark_as_hotel/', views.ajax_mark_as_hotel, name='ajax_mark_as_hotel'),
    path('ajax_mark_summary_as_hotel/', views.ajax_mark_summary_as_hotel, name='ajax_mark_summary_as_hotel'),
    
    # Employee tour assignment routes
    # path('admin/assign-employee/', views.assign_employee_to_tour, name='assign_employee_to_tour'),
    path('employee/get-itinerary/<str:tour_id>/', views.get_employee_itinerary, name='get_employee_itinerary'),
    path('employee/update-event-status/', views.update_event_status, name='update_event_status'),
    # path('assign-employee-to-tour/', views.assign_employee_to_tour, name='assign_employee_to_tour'),
    path('assign-employee-direct/', views.assign_employee_direct, name='assign_employee_direct'),
    # path('admin/get-employee-progress/', views.get_employee_progress, name='get_employee_progress'),
]
