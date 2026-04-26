from django.contrib import admin

from .models import ChatbotLog


@admin.register(ChatbotLog)
class ChatbotLogAdmin(admin.ModelAdmin):
    list_display = (
        "log_id",
        "created_at",
        "user",
        "resolved_intent",
        "fallback_used",
        "intent_classifier_source",
        "response_nlg_source",
    )
    list_filter = (
        "fallback_used",
        "resolved_intent",
        "intent_classifier_source",
        "response_nlg_source",
        "created_at",
    )
    search_fields = (
        "user_message",
        "resolved_intent",
        "bot_response",
    )
    readonly_fields = (
        "created_at",
        "provenance_json",
        "resolved_params_json",
        "user_message",
        "bot_response",
        "intent_classifier_source",
        "response_nlg_source",
        "fallback_used",
        "resolved_intent",
        "data_source",
    )
    ordering = ("-created_at",)
