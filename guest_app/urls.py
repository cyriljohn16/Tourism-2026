from django.urls import path
from . import views
from django.conf.urls.static import static
from django.conf import settings

urlpatterns = [
    path('guest_book/<str:tour_id>/', views.guest_book, name='guest_book'),
    path('main-page/', views.main_page, name='main-page'),
    path('service-worker.js', views.guest_service_worker, name='guest_service_worker'),
    path('manifest.webmanifest', views.guest_manifest, name='guest_manifest'),
    path('offline/', views.guest_offline, name='guest_offline'),
    path('register/', views.register, name='register'),
    path('login/', views.login_view, name='login'),
    path('logout/', views.logout_view, name='logout'),
    path('get_tour_schedules/<str:tour_id>/', views.get_tour_schedules, name='get-tour-schedules'),
    # UPDATED booking URL with trailing slash
    path('book_tour/', views.book_tour, name='book_tour'),
    path('tour_schedule/<str:sched_id>/', views.tour_schedule_detail, name='tour_schedule_detail'),
    path('map/', views.map_view, name='map'),
    
    # Companion management URLs
    path('companion/', views.companion_view, name='companion'),
    path('companion/edit/<str:companion_id>/', views.edit_companion, name='edit_companion'),
    path('companion/delete/<str:companion_id>/', views.delete_companion, name='delete_companion'),
    path('companion/groups/', views.manage_companion_groups, name='manage_companion_groups'),
    path('companion/groups/debug/', views.companion_group_debug, name='companion_group_debug'),
    path('get_companions/', views.get_companions, name='get_companions'),
    
    # Tour details endpoints
    # path('get_tour_payables/', views.get_tour_payables, name='get_tour_payables'),
    path('get_tour_itinerary/', views.get_tour_itinerary, name='get_tour_itinerary'),
    
    # Language settings endpoint
    path('set-language/<str:lang_code>/', views.set_language_view, name='set-language'),
    path('get-translations/<str:lang_code>/', views.get_translations_view, name='get-translations'),
    
    # Profile update endpoints
    path('profile/', views.my_profile, name='my_profile'),
    path('profile/data/', views.get_profile_data, name='get_profile_data'),
    path('profile/update/', views.update_profile, name='update_profile'),
    path('notifications/', views.guest_notifications, name='guest_notifications'),
    
    # Friendship management
    path('friendship_debug/', views.friendship_debug, name='friendship_debug'),
    
    # Map bookmark API endpoints
    path('api/bookmarks/', views.bookmark_list, name='bookmark_list'),
    path('api/bookmarks/create/', views.bookmark_create, name='bookmark_create'),
    path('api/bookmarks/<int:bookmark_id>/update/', views.bookmark_update, name='bookmark_update'),
    path('api/bookmarks/<int:bookmark_id>/delete/', views.bookmark_delete, name='bookmark_delete'),
    path('api/bookmarks/debug/', views.bookmark_debug, name='bookmark_debug'),
    path('api/location/current/', views.current_location_api, name='current_location_api'),
    
    # Bookmark image API endpoints
    path('api/bookmarks/<int:bookmark_id>/images/', views.bookmark_get_images, name='bookmark_get_images'),
    path('api/bookmarks/<int:bookmark_id>/images/add/', views.bookmark_add_image, name='bookmark_add_image'),
    path('api/bookmarks/images/<int:image_id>/delete/', views.bookmark_delete_image, name='bookmark_delete_image'),
    
    # Booking cancellation endpoint
    path('cancel_booking/', views.cancel_booking, name='cancel_booking'),

    # Accommodation discovery (transactions are disabled; official outbound links only)
    path('accommodations/', views.accommodation_page, name='accommodation_page'),
    path('accommodation/<int:accom_id>/', views.accommodation_detail_page, name='accommodation_detail_page'),
    path('accommodation/<int:accom_id>/reviews/submit/', views.submit_accommodation_review, name='submit_accommodation_review'),
    path('accommodations/official-links/', views.my_accommodation_bookings, name='accommodation_official_links'),
    path('accommodations/my-bookings/', views.my_accommodation_bookings, name='my_accommodation_bookings'),
    path('tour-bookings/', views.my_tour_bookings, name='my_tour_bookings'),
    path('accommodations/my-bookings/<int:booking_id>/cancel/', views.cancel_my_accommodation_booking, name='cancel_my_accommodation_booking'),
    path('accommodations/recommend/', views.accommodation_recommend, name='accommodation_recommend'),
    # Deprecated accommodation transaction endpoints kept for safe compatibility.
    path('accommodations/deprecated/billing/', views.accommodation_billing, name='accommodation_billing_deprecated'),
    path('accommodations/billing/', views.accommodation_billing, name='accommodation_billing'),
    path('accommodations/deprecated/book/', views.accommodation_book, name='accommodation_book_deprecated'),
    path('accommodations/book/', views.accommodation_book, name='accommodation_book'),
    path('accommodations/deprecated/payment/webhook/', views.payment_webhook_callback, name='payment_webhook_callback_deprecated'),
    path('accommodations/payment/webhook/', views.payment_webhook_callback, name='payment_webhook_callback'),

    # Companion request endpoints
    path('companion/search/', views.search_users, name='search_users'),
    path('companion/requests/', views.list_companion_requests, name='list_companion_requests'),
    path('companion/requests/count/', views.companion_request_count, name='companion_request_count'),
    path('companion/request/send/', views.send_companion_request, name='send_companion_request'),
    # UPDATED URL patterns to match JavaScript requests
    path('companion/requests/accept/<int:request_id>/', views.accept_companion_request, name='accept_companion_request'),
    path('companion/requests/decline/<int:request_id>/', views.decline_companion_request, name='decline_companion_request'),
    path('companion/requests/debug/', views.debug_companion_requests, name='debug_companion_requests'),
    path('companion/requests/fix/', views.fix_companion_request, name='fix_companion_request'),
    path('companion/qrcode/', views.send_companion_qr_code, name='companion_qr_code'),
    path('debug/guest_model/', views.debug_guest_model, name='debug_guest_model'),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
