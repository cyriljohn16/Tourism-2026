from django.urls import path
from .views import (
    ai_chat,
    openai_chat,
    chat_runtime_health,
    decision_tree_runtime_status,
    text_cnn_predict,
    log_recommendation_click,
    log_guest_funnel_event,
    accommodation_booking_notifications,
    submit_usability_feedback,
    chat_image_predict,
)

urlpatterns = [
    path("chat/", ai_chat, name="ai_chat"),
    path("chat/image/", chat_image_predict, name="chat_image_predict"),
    # Backward-compatible route name retained for older reverse() calls.
    path("chat/", openai_chat, name="openai_chat"),
    path("chat/health/", chat_runtime_health, name="chat_runtime_health"),
    path("chat/decision-tree-runtime/", decision_tree_runtime_status, name="chat_decision_tree_runtime_status"),
    path("chat/recommendation-click/", log_recommendation_click, name="chat_recommendation_click"),
    path("chat/funnel-event/", log_guest_funnel_event, name="chat_funnel_event"),
    # Deprecated compatibility endpoint: accommodation transactions are out of scope.
    path("chat/accommodation-booking-notifications/", accommodation_booking_notifications, name="chat_accommodation_booking_notifications"),
    path("chat/deprecated/accommodation-booking-notifications/", accommodation_booking_notifications, name="chat_accommodation_booking_notifications_deprecated"),
    path("chat/usability-feedback/", submit_usability_feedback, name="chat_usability_feedback"),
    path("text-cnn/predict/", text_cnn_predict, name="text_cnn_predict"),
]
