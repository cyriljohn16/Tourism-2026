import json
import importlib
import hashlib
import logging
import math
import os
import re
import time
import uuid
import calendar
from collections import Counter
from datetime import datetime, timedelta
from decimal import Decimal
from difflib import get_close_matches
from pathlib import Path
from types import SimpleNamespace

from django.conf import settings
from django.contrib.auth.models import Group
from django.core.mail import send_mail
from django.db import transaction
from django.db.models import Count, DecimalField, ExpressionWrapper, F, Q, Sum
from django.http import JsonResponse
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

try:
    from openai import OpenAI
except ModuleNotFoundError:
    OpenAI = None

try:
    from google import genai  # type: ignore
except Exception:
    try:
        genai = importlib.import_module("google.genai")
    except Exception:
        genai = None

np = None
pd = None
tf = None

from tour_app.models import Admission_Rates, Tour_Add, Tour_Schedule
from admin_app.models import (
    Accomodation,
    Employee,
    Room,
    TourismInformation,
    TourAssignment,
    OwnerMonthlyReport,
    MonthlyReportRoomUsage,
)
from accom_app.models import AuthoritativeRoomDetails
from admin_app.notification_service import (
    create_notification,
    notify_assigned_employees_for_schedule,
)
from guest_app.models import AccommodationBooking, Billing, Guest, TourBooking, Pending
from .recommenders import (
    apply_approved_accommodation_scope,
    recommend_tours,
    recommend_accommodations_with_diagnostics,
    get_decision_tree_runtime_status,
    get_unavailable_tour_matches,
)
from .llm_translation import (
    translate_to_english,
    translate_to_user_language,
    translation_runtime_health,
)
from .chat_services.state import (
    load_chat_state as _load_chat_state_service,
    save_chat_state as _save_chat_state_service,
    clear_chat_state as _clear_chat_state_service,
)
from .chat_services.response_templates import (
    get_accommodation_slot_question,
    format_acknowledged_details,
    build_personalization_offer_text as _template_personalization_offer_text,
    PERSONALIZATION_PROMPT_DEFAULT,
)
from .models import (
    ChatbotLog,
    RecommendationEvent,
    RecommendationResult,
    SystemMetricLog,
    UsabilitySurveyResponse,
)

logger = logging.getLogger(__name__)

_TEXT_CNN_MODEL_CACHE = None
_TEXT_CNN_MODEL_PATH_CACHE = None
_TEXT_CNN_RUNTIME_IMPORT_ATTEMPTED = False
_TEXT_CNN_RUNTIME_IMPORT_ERROR = ""
_TEXT_CNN_DISABLED_LOGGED = False
_ACCOM_LOCATION_CACHE = None
_MAP_REFERENCE_PLACE_CACHE = None
_CHAT_STATE_SESSION_KEY = "ai_chatbot_state"
_CHAT_PREFERENCE_SESSION_KEY = "ai_chatbot_saved_preferences"
_CHAT_SOCIAL_SESSION_KEY = "ai_chatbot_social_profile"
_CHAT_ASSISTANT_MEMORY_SESSION_KEY = "ai_chatbot_assistant_memory"
_CHAT_STATE_TTL_SECONDS = 30 * 60
_PENDING_BOOKING_TTL_SECONDS = 10 * 60
DEMO_SAFE_MODE = bool(getattr(settings, "DEMO_SAFE_MODE", True))
_VALID_DATA_SOURCES = {"unlabeled", "demo_seeded", "pilot_test", "real_world"}
_ALLOWED_INTENTS = {
    "get_recommendation",
    "get_tourism_information",
    "calculate_billing",
    "get_accommodation_recommendation",
    "calculate_accommodation_billing",
    "book_accommodation",
    "plan_bayawan_stay",
    "travel_guidance",
    "reporting_summary",
    "employee_assigned_tours",
    "employee_open_assignment",
    "employee_update_assignment",
}
_INTENT_LABEL_ALIASES = {
    "gettourrecommendation": "get_recommendation",
    "tour_recommendation": "get_recommendation",
    "tour recommendation": "get_recommendation",
    "gettourisminformation": "get_tourism_information",
    "tourism_information": "get_tourism_information",
    "tourism information": "get_tourism_information",
    "tourist_information": "get_tourism_information",
    "tourist information": "get_tourism_information",
    "tourist_spot_info": "get_tourism_information",
    "tourist spot info": "get_tourism_information",
    "attraction_information": "get_tourism_information",
    "attraction information": "get_tourism_information",
    "calculatetourbilling": "calculate_billing",
    "tour_billing": "calculate_billing",
    "tour billing": "calculate_billing",
    "gethotelrecommendation": "get_accommodation_recommendation",
    "hotel_recommendation": "get_accommodation_recommendation",
    "hotel recommendation": "get_accommodation_recommendation",
    "calculatehotelbilling": "calculate_accommodation_billing",
    "hotel_billing": "calculate_accommodation_billing",
    "hotel billing": "calculate_accommodation_billing",
    # Accommodation bookings are external-link only; keep these routed to
    # recommendation/link guidance instead of any internal transaction flow.
    "bookhotel": "get_accommodation_recommendation",
    "book_hotel": "get_accommodation_recommendation",
    "reserve_accommodation": "get_accommodation_recommendation",
    "stay_planning": "plan_bayawan_stay",
    "budget_planning": "plan_bayawan_stay",
    "itinerary_planning": "plan_bayawan_stay",
    "plan_stay": "plan_bayawan_stay",
    "plan_trip": "plan_bayawan_stay",
    "bayawan_stay_planning": "plan_bayawan_stay",
    "directions": "travel_guidance",
    "direction": "travel_guidance",
    "travel_guidance": "travel_guidance",
    "distance_check": "travel_guidance",
    "reporting": "reporting_summary",
    "tourist_influx_report": "reporting_summary",
    "monthly_report": "reporting_summary",
    "employee_assigned": "employee_assigned_tours",
    "employee_assigned_tours": "employee_assigned_tours",
    "employee_open_assignment": "employee_open_assignment",
    "employee_update_assignment": "employee_update_assignment",
}


def _env_flag(name, default=False):
    raw = str(os.getenv(name, str(default))).strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _is_text_cnn_intent_disabled():
    return _env_flag("DISABLE_TEXT_CNN_INTENT", False)


def _ensure_text_cnn_runtime_stack():
    global np, pd, tf, _TEXT_CNN_RUNTIME_IMPORT_ATTEMPTED, _TEXT_CNN_RUNTIME_IMPORT_ERROR

    if _is_text_cnn_intent_disabled():
        return False, "disabled_by_environment"

    if np is not None and pd is not None and tf is not None:
        return True, ""

    if _TEXT_CNN_RUNTIME_IMPORT_ATTEMPTED:
        return False, _TEXT_CNN_RUNTIME_IMPORT_ERROR or "tensorflow_not_installed"

    _TEXT_CNN_RUNTIME_IMPORT_ATTEMPTED = True
    try:
        import numpy as _np
        import pandas as _pd
        import tensorflow as _tf
    except ModuleNotFoundError as exc:
        _TEXT_CNN_RUNTIME_IMPORT_ERROR = f"module_not_found:{exc}"
        return False, "tensorflow_not_installed"
    except Exception as exc:
        _TEXT_CNN_RUNTIME_IMPORT_ERROR = f"runtime_import_error:{exc}"
        return False, "tensorflow_not_installed"

    np = _np
    pd = _pd
    tf = _tf
    _TEXT_CNN_RUNTIME_IMPORT_ERROR = ""
    return True, ""
_MAP_ANCHOR_ALIASES = {
    "Bayawan City Public Terminal": (
        "terminal",
        "public terminal",
        "city terminal",
        "bus terminal",
        "bayawan terminal",
        "bayawan city public terminal",
        "near terminal",
        "near bus terminal",
    ),
    "Bayawan City Plaza": (
        "plaza",
        "city plaza",
        "bayawan plaza",
        "bayawan city plaza",
        "plaza area",
        "near plaza",
    ),
    "Bayawan City Public Market": (
        "market",
        "public market",
        "city market",
        "bayawan market",
        "bayawan city public market",
        "wet market",
        "near market",
    ),
    "Hayahay Square": (
        "hayahay",
        "hayahay square",
        "near hayahay",
        "hayahay area",
    ),
    "Eskina Restaurant": (
        "eskina",
        "eskina restaurant",
        "near eskina",
        "eskina area",
    ),
    "Catholic Church": (
        "church",
        "catholic church",
        "parish church",
        "near church",
        "near catholic church",
    ),
    "Puregold Grocery Section": (
        "puregold",
        "puregold bayawan",
        "near puregold",
        "grocery near puregold",
    ),
}
_TERMINAL_SPECIFIC_MARKERS = (
    "bus terminal",
    "public terminal",
    "city terminal",
    "trike terminal",
    "tricycle terminal",
    "tricyle terminal",
    "pedicab terminal",
    "motorcab terminal",
)
_SUS_CODES = [f"SUS_Q{i}" for i in range(1, 11)]
_TAM_CODES = [f"PU_Q{i}" for i in range(1, 5)] + [f"PEU_Q{i}" for i in range(1, 5)]
_DIFFICULTY_CODES = ["DIFF_DISCOVER", "DIFF_MATCH", "DIFF_PLAN", "DIFF_BOOKPAY"]
_FULL_SURVEY_CODES = set(_SUS_CODES + _TAM_CODES)
_GUEST_FUNNEL_EVENT_MAP = {
    "chatbot_opened": {"item_ref": "chat:funnel_chatbot_opened", "event_type": "view"},
    "quick_start_clicked": {"item_ref": "chat:funnel_quick_start_clicked", "event_type": "click"},
    "recommendation_shown": {"item_ref": "chat:funnel_recommendation_shown", "event_type": "view"},
    "recommendation_card_clicked": {"item_ref": "chat:funnel_recommendation_card_clicked", "event_type": "click"},
    "book_button_clicked": {"item_ref": "chat:funnel_book_button_clicked", "event_type": "click"},
    "booking_flow_started": {"item_ref": "chat:funnel_booking_flow_started", "event_type": "save"},
    "billing_link_shown": {"item_ref": "chat:funnel_billing_link_shown", "event_type": "view"},
    "billing_link_clicked": {"item_ref": "chat:funnel_billing_link_clicked", "event_type": "click"},
    "booking_completed": {"item_ref": "chat:funnel_booking_completed", "event_type": "book"},
    # Clean accommodation preview-only flow events.
    "accommodation_search_started": {"item_ref": "accommodation_search_started", "event_type": "view"},
    "accommodation_recommendations_shown": {"item_ref": "accommodation_recommendations_shown", "event_type": "view"},
    "accommodation_rooms_viewed": {"item_ref": "accommodation_rooms_viewed", "event_type": "click"},
    "accommodation_preview_started": {"item_ref": "accommodation_preview_started", "event_type": "save"},
    "accommodation_preview_completed": {"item_ref": "accommodation_preview_completed", "event_type": "view"},
    "accommodation_external_handoff_clicked": {"item_ref": "accommodation_external_handoff_clicked", "event_type": "click"},
}


def _to_bool_env(value, default=False):
    raw = str(value or "").strip().lower()
    if not raw:
        return bool(default)
    if raw in ("1", "true", "yes", "on", "y"):
        return True
    if raw in ("0", "false", "no", "off", "n"):
        return False
    return bool(default)


def _to_bool(value, default=False):
    return _to_bool_env(value, default=default)


def _known_accommodation_locations(force_reload=False):
    global _ACCOM_LOCATION_CACHE
    if _ACCOM_LOCATION_CACHE is not None and not force_reload:
        return _ACCOM_LOCATION_CACHE
    try:
        rows = (
            apply_approved_accommodation_scope(Accomodation.objects.all(), accommodation_path="")
            .exclude(location__isnull=True)
            .exclude(location__exact="")
            .values_list("location", flat=True)
            .distinct()
        )
        normalized = []
        seen = set()
        for row in rows:
            value = " ".join(str(row or "").strip().lower().split())
            if not value or value in seen:
                continue
            seen.add(value)
            normalized.append(value)
        _ACCOM_LOCATION_CACHE = normalized[:300]
    except Exception:
        _ACCOM_LOCATION_CACHE = []
    return _ACCOM_LOCATION_CACHE


def _normalize_chat_text(value):
    text = " ".join(str(value or "").strip().lower().split())
    if not text:
        return ""
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    return " ".join(text.split())


def _load_map_reference_place_entries(force_reload=False):
    global _MAP_REFERENCE_PLACE_CACHE
    if _MAP_REFERENCE_PLACE_CACHE is not None and not force_reload:
        return _MAP_REFERENCE_PLACE_CACHE

    entries = []
    seen = set()
    try:
        template_path = Path(__file__).resolve().parent.parent / "admin_app" / "templates" / "map.html"
        raw = template_path.read_text(encoding="utf-8", errors="ignore")
        object_pattern = re.compile(
            r'name:\s*"([^"]+)"\s*,\s*gps:\s*\{\s*lat:\s*([-0-9.]+)\s*,\s*lng:\s*([-0-9.]+)\s*\}',
            re.IGNORECASE | re.MULTILINE,
        )
        for match in object_pattern.finditer(raw):
            canonical = " ".join(str(match.group(1) or "").split()).strip()
            normalized = _normalize_chat_text(canonical)
            if not canonical or not normalized or normalized in seen:
                continue
            lat_val = None
            lng_val = None
            try:
                lat_val = float(match.group(2))
                lng_val = float(match.group(3))
            except Exception:
                lat_val = None
                lng_val = None
            seen.add(normalized)
            entries.append(
                {
                    "name": canonical,
                    "normalized": normalized,
                    "lat": lat_val,
                    "lng": lng_val,
                }
            )
        if not entries:
            for name in re.findall(r'name:\s*"([^"]+)"', raw):
                canonical = " ".join(str(name or "").split()).strip()
                normalized = _normalize_chat_text(canonical)
                if not canonical or not normalized or normalized in seen:
                    continue
                seen.add(normalized)
                entries.append(
                    {
                        "name": canonical,
                        "normalized": normalized,
                        "lat": None,
                        "lng": None,
                    }
                )
    except Exception:
        entries = []

    _MAP_REFERENCE_PLACE_CACHE = entries[:500]
    return _MAP_REFERENCE_PLACE_CACHE


def _match_map_reference_place(raw_location):
    matches = _match_map_reference_places(raw_location, limit=1)
    return matches[0] if matches else {}


def _match_map_reference_places(raw_location, *, limit=5):
    candidate = _normalize_chat_text(raw_location)
    if not candidate:
        return []

    ranked = []
    seen = set()

    def _push(entry, rank):
        if not isinstance(entry, dict):
            return
        name = " ".join(str(entry.get("name") or "").split()).strip()
        normalized = _normalize_chat_text(entry.get("normalized") or name)
        lat_val = entry.get("lat")
        lng_val = entry.get("lng")
        if not name or not normalized:
            return
        key = normalized
        if key in seen:
            return
        seen.add(key)
        ranked.append(
            {
                "name": name,
                "normalized": normalized,
                "lat": lat_val,
                "lng": lng_val,
                "_rank": int(rank),
            }
        )

    # Resolve known user wordings first (e.g., "trike terminal").
    for anchor_name, aliases in _MAP_ANCHOR_ALIASES.items():
        for alias in aliases:
            alias_norm = _normalize_chat_text(alias)
            if not alias_norm:
                continue
            if anchor_name == "Bayawan City Public Terminal":
                if any(
                    marker in candidate
                    for marker in (
                        "trike terminal",
                        "tricycle terminal",
                        "tricyle terminal",
                        "pedicab terminal",
                        "motorcab terminal",
                    )
                ):
                    # Keep trike/pedicab terminals from collapsing to the bus/public anchor.
                    continue
            if candidate == alias_norm:
                _push(
                    {
                        "name": anchor_name,
                        "normalized": _normalize_chat_text(anchor_name),
                        "lat": None,
                        "lng": None,
                    },
                    rank=0,
                )
            elif candidate in alias_norm or alias_norm in candidate:
                _push(
                    {
                        "name": anchor_name,
                        "normalized": _normalize_chat_text(anchor_name),
                        "lat": None,
                        "lng": None,
                    },
                    rank=1,
                )

    entries = _load_map_reference_place_entries()
    if not entries:
        trimmed = sorted(ranked, key=lambda row: (row.get("_rank", 99), row.get("name", "")))
        return [{k: v for k, v in row.items() if not str(k).startswith("_")} for row in trimmed[: max(1, int(limit or 1))]]

    for entry in entries:
        if candidate == entry["normalized"]:
            _push(entry, rank=0)

    for entry in entries:
        normalized_name = str(entry.get("normalized") or "")
        if candidate in normalized_name or normalized_name in candidate:
            _push(entry, rank=1)

    pool = [str(entry.get("normalized") or "") for entry in entries]
    close = get_close_matches(candidate, pool, n=max(1, int(limit or 1)), cutoff=0.8)
    for best in close:
        for entry in entries:
            if entry.get("normalized") == best:
                _push(entry, rank=2)
                break

    trimmed = sorted(ranked, key=lambda row: (row.get("_rank", 99), row.get("name", "")))
    return [{k: v for k, v in row.items() if not str(k).startswith("_")} for row in trimmed[: max(1, int(limit or 1))]]


def _map_place_to_location_hint(place_name):
    normalized = _normalize_chat_text(place_name)
    if not normalized:
        return ""

    hint_rules = [
        ("villareal", "villareal"),
        ("villarreal", "villareal"),
        ("tinago", "tinago"),
        ("boyco", "boyco"),
        ("ubos", "ubos"),
        ("suba", "suba"),
        ("poblacion", "poblacion"),
        ("public terminal", "tinago"),
        ("terminal", "tinago"),
        ("public market", "boyco"),
        ("market", "boyco"),
        ("plaza", "poblacion"),
        ("catholic church", "ubos"),
        ("church", "ubos"),
        ("hayahay", "suba"),
        ("eskina", "suba"),
        ("city hall", "bayawan city"),
        ("bayawan", "bayawan city"),
    ]
    for needle, hint in hint_rules:
        if needle in normalized:
            return hint
    return "bayawan city"


def _is_generic_terminal_reference(value):
    normalized = _normalize_chat_text(value)
    if "terminal" not in normalized:
        return False
    return not any(marker in normalized for marker in _TERMINAL_SPECIFIC_MARKERS)


def _approved_accommodation_queryset():
    return apply_approved_accommodation_scope(
        Accomodation.objects.all(),
        accommodation_path="",
    )


def _approved_room_queryset():
    room_qs = Room.objects.select_related("accommodation").all()
    return apply_approved_accommodation_scope(room_qs, accommodation_path="accommodation")


def _safe_float(value, default=None):
    try:
        if value in (None, ""):
            return default
        return float(value)
    except Exception:
        return default


def _resolve_client_location(payload, request):
    location_payload = payload.get("client_location") if isinstance(payload, dict) else {}
    if not isinstance(location_payload, dict):
        location_payload = {}
    lat = _safe_float(location_payload.get("latitude"))
    lng = _safe_float(location_payload.get("longitude"))
    accuracy = _safe_float(
        location_payload.get("accuracy_m")
        if location_payload.get("accuracy_m") not in (None, "")
        else location_payload.get("accuracy")
    )
    status = str(location_payload.get("status") or "").strip().lower()

    if lat is None or lng is None:
        session_payload = {}
        try:
            session_payload = request.session.get("guest_current_location") if hasattr(request, "session") else {}
        except Exception:
            session_payload = {}
        if isinstance(session_payload, dict):
            lat = _safe_float(session_payload.get("latitude"), default=lat)
            lng = _safe_float(session_payload.get("longitude"), default=lng)
            if accuracy is None:
                accuracy = _safe_float(session_payload.get("accuracy_m"))
            if not status:
                status = str(session_payload.get("status") or "available").strip().lower()

    if lat is None or lng is None:
        return {"status": status or "unavailable"}
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lng <= 180.0):
        return {"status": "unavailable"}
    return {
        "status": status or "available",
        "latitude": round(float(lat), 6),
        "longitude": round(float(lng), 6),
        "accuracy_m": round(float(accuracy), 2) if isinstance(accuracy, float) and accuracy >= 0 else None,
    }


def _haversine_km(lat1, lon1, lat2, lon2):
    radius_km = 6371.0
    p1 = math.radians(float(lat1))
    p2 = math.radians(float(lat2))
    dlat = math.radians(float(lat2) - float(lat1))
    dlon = math.radians(float(lon2) - float(lon1))
    a = math.sin(dlat / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlon / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return max(0.0, radius_km * c)


def _estimate_travel_minutes(distance_km):
    km = max(0.0, float(distance_km or 0.0))
    if km <= 2.0:
        speed_kmh = 4.5  # walkable city estimate
    elif km <= 12.0:
        speed_kmh = 22.0  # tricycle/local city traffic
    else:
        speed_kmh = 35.0  # mixed local road estimate
    minutes = (km / speed_kmh) * 60.0
    return int(max(1, round(minutes)))


def _is_travel_guidance_request(message):
    text = _normalize_chat_text(message)
    if not text:
        return False
    markers = (
        "how far",
        "how many minutes",
        "how many hours",
        "how long",
        "how to go",
        "how to go there",
        "how to get to",
        "how to get there",
        "how do i get",
        "how far is that",
        "how far is that from",
        "which way",
        "directions",
        "directions to",
        "directions there",
        "route to",
        "from my location",
        "from manila",
        "from another country",
        "international",
        "where is",
    )
    return any(marker in text for marker in markers)


def _is_contextual_direction_followup(message):
    text = _normalize_chat_text(message)
    if not text:
        return False
    followup_markers = (
        "how to go there",
        "how to get there",
        "directions there",
        "how far is that",
        "how far is that from",
    )
    return any(marker in text for marker in followup_markers)


def _is_guest_vague_query(message):
    text = _normalize_chat_text(message)
    if not text:
        return False
    # Very short generic prompts should be clarified first.
    if text in {
        "help",
        "where",
        "place",
        "something",
        "something nice",
        "hotel",
        "inn",
        "accommodation",
        "stay",
        "recommendation",
        "any recommendation",
    }:
        return True
    # Planning requests should never be treated as vague/open-ended.
    if _is_stay_planning_request(text):
        return False
    direct_scope_terms = (
        "tour",
        "hotel",
        "inn",
        "accommodation",
        "directions",
        "how to go",
        "how to get",
        "map",
        "report",
        "book",
        "booking",
    )
    if any(term in text for term in direct_scope_terms):
        return False
    vague_phrases = (
        "i want to go somewhere nice",
        "help me",
        "any recommendation",
        "where should i go",
        "where do i go",
    )
    if any(phrase in text for phrase in vague_phrases):
        return True
    if text in {"what can i do", "recommend me something", "recommend something"}:
        return True
    return False


def _is_likely_gibberish_query(message):
    text = _normalize_chat_text(message)
    if not text:
        return False
    if len(text) <= 2:
        return False
    # If it already contains domain cues, treat it as intentional.
    if re.search(
        r"\b(tour|hotel|inn|accommodation|stay|room|book|booking|preview|directions?|map|plan|trip|bayawan|report|owner|employee|admin)\b",
        text,
    ):
        return False

    tokens = re.findall(r"[a-z]+", text.lower())
    if not tokens:
        return True
    if len(tokens) > 3:
        return False

    collapsed = "".join(tokens)
    if len(tokens) == 1:
        token = tokens[0]
        if len(token) < 6:
            return False
        if any(pattern in token for pattern in ("qwe", "asd", "zxc", "qaz", "wsx")):
            return True
        common_single_words = {
            "recommendation",
            "recommendations",
            "something",
            "somewhere",
            "planning",
            "direction",
            "directions",
            "accommodation",
            "accommodations",
            "itinerary",
            "tourism",
        }
        if len(token) >= 10 and token not in common_single_words:
            return True
        if re.fullmatch(r"([a-z]{2,4})\1{1,}", token):
            return True
        vowel_count = sum(1 for ch in token if ch in "aeiou")
        vowel_ratio = float(vowel_count) / float(max(len(token), 1))
        if vowel_ratio < 0.20:
            return True
        if not re.search(r"(th|he|in|re|an|on|to|at|er|st|or|ar|ou|el|ho|di|pl|tr)", token):
            return True
    else:
        vowel_count = sum(1 for ch in collapsed if ch in "aeiou")
        if len(collapsed) >= 10 and float(vowel_count) / float(max(len(collapsed), 1)) < 0.22:
            return True
    return False


def _is_vague_accommodation_request(message, params=None):
    text = _normalize_chat_text(message)
    params = params if isinstance(params, dict) else {}
    if not text:
        return False
    if not re.search(r"\b(hotel|inn|accommodation|place to stay|stay)\b", text):
        return False
    budget = _to_int(params.get("budget"), default=0)
    guests = _to_int(params.get("guests"), default=0)
    location = str(params.get("location") or "").strip()
    if budget > 0 or guests > 0 or location:
        return False
    vague_markers = (
        "place to stay",
        "maybe like a place to stay",
        "accommodation",
        "maybe a place to stay",
        "find a place to stay",
    )
    return any(marker in text for marker in vague_markers)


def _recent_accommodation_choices(chat_state, *, limit=3):
    rows = (
        chat_state.get("last_accommodation_recommendations")
        if isinstance(chat_state, dict) and isinstance(chat_state.get("last_accommodation_recommendations"), list)
        else []
    )
    seen = set()
    choices = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = str(
            row.get("accom_name")
            or row.get("company_name")
            or row.get("name")
            or ""
        ).strip()
        location = str(row.get("location") or "").strip()
        if not name:
            continue
        key = _normalize_chat_text(name)
        if key in seen:
            continue
        seen.add(key)
        choices.append({"name": name, "location": location})
        if len(choices) >= max(1, limit):
            break
    return choices


def _has_accommodation_plus_direction_mix(message, params=None):
    text = _normalize_chat_text(message)
    params = params if isinstance(params, dict) else {}
    if not text:
        return False
    has_accommodation = bool(re.search(r"\b(hotel|inn|accommodation|stay|place to stay)\b", text))
    has_direction = _is_travel_guidance_request(text)
    has_constraints = bool(
        re.search(r"\bunder\s*\d+|\bbudget\b|\b\d+\s*(guest|guests|people|person|pax)\b", text)
        or str(params.get("location") or "").strip()
    )
    return has_accommodation and has_direction and has_constraints


def _is_role_vague_query(actor, message):
    role = str((actor or {}).get("role") or "").strip().lower()
    text = _normalize_chat_text(message)
    if not text:
        return False
    if role == "guest":
        return _is_guest_vague_query(text)
    if role == "owner":
        return text in {"what do i do here", "help", "what now"} or (
            "help" in text and not _is_owner_help_command(text)
        )
    if role == "employee":
        return text in {"help", "what now"} or (
            "help" in text and not _is_employee_assigned_tours_command(text)
        )
    if role == "admin":
        return text in {"help", "what now"} or ("help" in text and not _is_reporting_summary_request(text))
    return False


def _is_reporting_summary_request(message):
    text = _normalize_chat_text(message)
    if not text:
        return False
    markers = (
        "summary",
        "reports",
        "tourist influx",
        "monthly report",
        "monthly reports",
        "accommodation report",
        "owner report",
        "owner monthly report",
        "tourism report",
        "report for",
        "guests recorded",
        "room usage by room",
        "check ins per room",
        "check-ins per room",
        "latest monthly report",
        "show latest monthly report",
    )
    return any(marker in text for marker in markers)


def _extract_reporting_period_hint(message):
    text = str(message or "").strip()
    if not text:
        return None

    iso_match = re.search(r"\b(20\d{2})[-/](0?[1-9]|1[0-2])\b", text)
    if iso_match:
        year_val = int(iso_match.group(1))
        month_val = int(iso_match.group(2))
        return datetime(year_val, month_val, 1).date()

    month_names = [m for m in calendar.month_name if m]
    month_pattern = "|".join(month_names)
    month_match = re.search(rf"\b({month_pattern})\s+(20\d{{2}})\b", text, flags=re.IGNORECASE)
    if month_match:
        month_name = str(month_match.group(1) or "").strip().lower()
        year_val = int(month_match.group(2))
        month_val = 0
        for idx, value in enumerate(month_names, start=1):
            if value.lower() == month_name:
                month_val = idx
                break
        if month_val > 0:
            return datetime(year_val, month_val, 1).date()

    return None


def _extract_nationality_counts(raw_text):
    text = str(raw_text or "").strip()
    if not text:
        return Counter()
    counts = Counter()
    for match in re.finditer(r"([A-Za-z][A-Za-z\s\-]{1,40})\s*[:\-]?\s*(\d{1,6})", text):
        label = " ".join(str(match.group(1) or "").split()).strip().title()
        value = _to_int(match.group(2), default=0)
        if label and value > 0:
            counts[label] += value
    return counts


def _resolve_reporting_accommodation_name(message, params, available_names):
    preferred = str((params or {}).get("accom_name") or "").strip()
    if preferred:
        return preferred

    text = str(message or "").strip()
    if not text:
        return ""
    hint_match = re.search(
        r"\b(?:for|of|from)\s+([A-Za-z0-9][A-Za-z0-9\s\-'&]{2,80})\b",
        text,
        flags=re.IGNORECASE,
    )
    if hint_match:
        raw_name = " ".join(str(hint_match.group(1) or "").split()).strip(" .,!?")
        if raw_name:
            if re.fullmatch(
                r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|"
                r"sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\s+20\d{2}",
                raw_name.strip().lower(),
            ):
                raw_name = ""
        if raw_name:
            close = get_close_matches(raw_name, available_names, n=1, cutoff=0.72)
            return close[0] if close else raw_name
    close = get_close_matches(text, available_names, n=1, cutoff=0.78)
    return close[0] if close else ""


def _build_reporting_summary_payload(message, params):
    lower_message = _normalize_chat_text(message)
    requested_latest = _contains_any_phrase(lower_message, ("latest monthly report", "latest report"))
    requested_this_month = _contains_any_phrase(lower_message, ("this month", "monthly summary this month"))

    base_qs = OwnerMonthlyReport.objects.select_related("accommodation").exclude(status="draft")
    if not base_qs.exists():
        return {
            "reply": "No report data is available yet for this period.",
            "quick_replies": ["Show latest monthly report", "Show tourist influx this month"],
        }

    available_names = [str(v or "").strip() for v in base_qs.values_list("accommodation__company_name", flat=True)]
    available_names = [v for v in available_names if v]
    report_period = _extract_reporting_period_hint(message)
    if report_period is None and requested_this_month:
        today = timezone.localdate()
        report_period = datetime(today.year, today.month, 1).date()
    accommodation_name = _resolve_reporting_accommodation_name(message, params, available_names)

    qs = base_qs
    if accommodation_name:
        qs = qs.filter(accommodation__company_name__icontains=accommodation_name)
    if report_period is not None:
        qs = qs.filter(
            reporting_period__year=report_period.year,
            reporting_period__month=report_period.month,
        )
    elif requested_latest:
        latest_period = qs.order_by("-reporting_period").values_list("reporting_period", flat=True).first()
        if latest_period is not None:
            report_period = latest_period
            qs = qs.filter(
                reporting_period__year=latest_period.year,
                reporting_period__month=latest_period.month,
            )

    if not qs.exists():
        period_text = report_period.strftime("%B %Y") if report_period is not None else "the selected period"
        if accommodation_name:
            no_data_text = f"No report data is available yet for {accommodation_name} in {period_text}."
        else:
            no_data_text = f"No report data is available yet for {period_text}."
        return {
            "reply": no_data_text,
            "quick_replies": ["Show latest monthly report", "Show tourist influx this month", "Show accommodation reports"],
        }

    totals = qs.aggregate(
        total_in=Sum("guests_checked_in"),
        total_out=Sum("guests_checked_out"),
        total_rooms=Sum("rooms_used"),
    )
    total_in = _to_int(totals.get("total_in"), default=0)
    total_out = _to_int(totals.get("total_out"), default=0)
    total_rooms = _to_int(totals.get("total_rooms"), default=0)

    room_usage_rows = (
        MonthlyReportRoomUsage.objects.filter(monthly_report__in=qs)
        .values("room_name_snapshot")
        .annotate(total_check_ins=Sum("check_ins"), total_check_outs=Sum("check_outs"))
        .order_by("-total_check_ins", "room_name_snapshot")
    )
    has_room_level = bool(room_usage_rows)

    nationality_counts = Counter()
    for item in qs.values_list("nationality_breakdown", flat=True):
        nationality_counts.update(_extract_nationality_counts(item))

    sample = qs.order_by("-reporting_period", "accommodation__company_name").first()
    period_text = report_period.strftime("%B %Y") if report_period is not None else sample.reporting_period.strftime("%B %Y")
    accommodation_text = ""
    if accommodation_name and sample is not None:
        accommodation_text = f" for {sample.accommodation.company_name}"

    has_finalized = qs.filter(status="reviewed").exists()
    if has_finalized:
        lines = [f"Here's the tourism report summary for {period_text}{accommodation_text}:"]
    else:
        lines = [
            f"No finalized monthly summary document is available yet, but submitted owner reports show for {period_text}{accommodation_text}:"
        ]

    lines.append(f"- Check-ins: {total_in}")
    lines.append(f"- Check-outs: {total_out}")
    if has_room_level:
        lines.append("- Room usage (by room type):")
        for row in list(room_usage_rows)[:8]:
            room_name = str(row.get("room_name_snapshot") or "Unnamed Room").strip()
            check_ins = _to_int(row.get("total_check_ins"), default=0)
            check_outs = _to_int(row.get("total_check_outs"), default=0)
            lines.append(f"  - {room_name}: {check_ins} check-ins, {check_outs} check-outs")
    else:
        lines.append(f"- Room usage (total only): {total_rooms} room(s) used")
        lines.append("Older reports only contain total room usage. New reports provide room-level details.")

    if nationality_counts:
        labels = [f"{label} ({count})" for label, count in nationality_counts.most_common(6)]
        lines.append(f"- Visitor nationalities: {', '.join(labels)}")
    else:
        lines.append("- Visitor nationalities: not reported yet")
    lines.append("This is based on submitted owner reports.")

    return {
        "reply": "\n".join(lines),
        "quick_replies": [
            "Show tourist influx this month",
            "Show monthly report for April 2026",
            "Show accommodation reports",
        ],
    }

def _allow_demo_artifact_fallback():
    override = os.getenv("CHATBOT_ALLOW_DEMO_ARTIFACT_FALLBACK")
    if override not in (None, ""):
        return _to_bool_env(override, default=False)
    return bool(getattr(settings, "DEBUG", False))


def _resolve_model_artifact_path(*, env_var, final_relative_path, demo_relative_path=None):
    configured = str(os.getenv(env_var, "")).strip()
    if configured:
        return Path(configured), "configured_env"

    artifacts_root = Path(__file__).resolve().parent.parent / "artifacts"
    final_path = artifacts_root / final_relative_path
    if final_path.exists():
        return final_path, "final_default"

    if demo_relative_path and _allow_demo_artifact_fallback():
        demo_path = artifacts_root / demo_relative_path
        if demo_path.exists():
            return demo_path, "demo_fallback"

    return final_path, "final_required_missing"


def _resolve_accommodation_text_cnn_model_path():
    return _resolve_model_artifact_path(
        env_var="CHATBOT_ACCOM_CNN_MODEL_PATH",
        final_relative_path="text_cnn_accommodation/text_cnn_accommodation.keras",
        demo_relative_path="text_cnn_demo/text_cnn_demo.keras",
    )


def _default_text_cnn_model_path():
    path, _source = _resolve_accommodation_text_cnn_model_path()
    return path


def _resolve_intent_text_cnn_model_path():
    return _resolve_model_artifact_path(
        env_var="CHATBOT_INTENT_CNN_MODEL_PATH",
        final_relative_path="text_cnn_intent/text_cnn_intent.h5",
        demo_relative_path="text_cnn_demo/text_cnn_demo.keras",
    )


def _default_intent_text_cnn_model_path():
    path, _source = _resolve_intent_text_cnn_model_path()
    return path


def _resolve_text_cnn_model_source(model_path):
    try:
        candidate = Path(model_path).resolve()
    except Exception:
        return "manual_override"

    accom_path, accom_source = _resolve_accommodation_text_cnn_model_path()
    intent_path, intent_source = _resolve_intent_text_cnn_model_path()
    try:
        if candidate == accom_path.resolve():
            return f"accommodation:{accom_source}"
    except Exception:
        pass
    try:
        if candidate == intent_path.resolve():
            return f"intent:{intent_source}"
    except Exception:
        pass
    return "manual_override"


def _default_label_map_path_for_model(model_path):
    path = Path(model_path)
    return path.parent / "label_map.json"


def _default_vocab_path_candidates_for_model(model_path):
    path = Path(model_path)
    return [
        path.parent / "text_cnn_intent_vocab.json",
        path.parent / f"{path.stem}_vocab.json",
        path.parent / "text_cnn_vocab.json",
    ]


def _load_saved_vectorizer_vocab(model_path):
    for vocab_path in _default_vocab_path_candidates_for_model(model_path):
        if not vocab_path.exists():
            continue
        try:
            payload = json.loads(vocab_path.read_text(encoding="utf-8"))
            if isinstance(payload, list) and payload:
                tokens = []
                for token in payload:
                    text = str(token or "").strip()
                    if not text:
                        continue
                    # Reserved placeholders are added internally by
                    # TextVectorization; re-injecting them shifts token ids.
                    lowered = text.lower()
                    if lowered in {"[unk]", "unk"}:
                        continue
                    tokens.append(text)
                if tokens:
                    return tokens, str(vocab_path)
        except Exception:
            continue
    return None, ""


def _default_text_cnn_repair_dataset_path():
    configured = str(os.getenv("CHATBOT_TEXT_CNN_REPAIR_DATASET", "")).strip()
    if configured:
        return Path(configured)
    root = Path(__file__).resolve().parent.parent / "thesis_data_templates"
    preferred = root / "text_cnn_messages_final_expanded_v3_clean.csv"
    if preferred.exists():
        return preferred
    return root / "text_cnn_messages_final_merged.csv"


def _load_repair_corpus(dataset_path):
    runtime_ready, runtime_err = _ensure_text_cnn_runtime_stack()
    if not runtime_ready:
        return [], runtime_err or "pandas_not_installed"
    path = Path(dataset_path)
    if not path.exists():
        return [], f"repair_dataset_not_found:{path}"
    try:
        # Pandas keeps this robust with UTF-8/BOM CSV variants.
        df = pd.read_csv(path, encoding="utf-8-sig")
    except Exception as exc:
        return [], f"repair_dataset_error:{exc}"
    if "message_text" not in df.columns:
        return [], "repair_dataset_missing_message_text"
    corpus = (
        df["message_text"]
        .fillna("")
        .astype(str)
        .str.strip()
        .tolist()
    )
    corpus = [row for row in corpus if row]
    if not corpus:
        return [], "repair_dataset_empty"
    return corpus, ""


def _repair_text_vectorization_table(loaded_model, model_path=None):
    runtime_ready, runtime_err = _ensure_text_cnn_runtime_stack()
    if not runtime_ready:
        return None, runtime_err or "tensorflow_not_installed"

    try:
        old_vectorizer = None
        for layer in loaded_model.layers:
            if isinstance(layer, tf.keras.layers.TextVectorization):
                old_vectorizer = layer
                break
        if old_vectorizer is None:
            return None, "repair_vectorizer_missing"

        embedding_layer = loaded_model.get_layer("embedding")
        conv_layer = loaded_model.get_layer("conv1d")
        dense_layer = loaded_model.get_layer("class_probs")

        emb_weights = embedding_layer.get_weights()
        conv_weights = conv_layer.get_weights()
        dense_weights = dense_layer.get_weights()
        if not emb_weights or not conv_weights or not dense_weights:
            return None, "repair_weights_missing"

        vocab_size, embedding_dim = emb_weights[0].shape
        kernel_size, conv_in_dim, conv_filters = conv_weights[0].shape
        if conv_in_dim != embedding_dim:
            return None, "repair_shape_mismatch"

        dense_in, class_count = dense_weights[0].shape
        if dense_in != conv_filters:
            return None, "repair_dense_shape_mismatch"

        vec_cfg = old_vectorizer.get_config()
        sequence_length = int(vec_cfg.get("output_sequence_length") or 24)
        max_tokens = int(vec_cfg.get("max_tokens") or vocab_size)
        standardize = vec_cfg.get("standardize") or "lower_and_strip_punctuation"

        text_input = tf.keras.Input(shape=(1,), dtype=tf.string, name="text")
        vectorizer = tf.keras.layers.TextVectorization(
            max_tokens=max_tokens,
            output_mode="int",
            output_sequence_length=sequence_length,
            standardize=standardize,
            name=old_vectorizer.name,
        )
        x = vectorizer(text_input)
        x = tf.keras.layers.Embedding(
            input_dim=vocab_size,
            output_dim=embedding_dim,
            name="embedding",
        )(x)
        x = tf.keras.layers.Conv1D(
            filters=conv_filters,
            kernel_size=kernel_size,
            activation="relu",
            name="conv1d",
        )(x)
        x = tf.keras.layers.GlobalMaxPooling1D(name="global_max_pooling1d")(x)
        out = tf.keras.layers.Dense(
            class_count,
            activation="softmax",
            name="class_probs",
        )(x)
        repaired_model = tf.keras.Model(inputs=text_input, outputs=out)
        repaired_model.compile(
            optimizer="adam",
            loss="sparse_categorical_crossentropy",
            metrics=["accuracy"],
        )

        saved_vocab, _saved_vocab_path = _load_saved_vectorizer_vocab(model_path) if model_path else (None, "")
        if saved_vocab:
            vectorizer.set_vocabulary(saved_vocab)
        else:
            corpus, corpus_err = _load_repair_corpus(_default_text_cnn_repair_dataset_path())
            if not corpus:
                return None, corpus_err
            vectorizer.adapt(tf.data.Dataset.from_tensor_slices(corpus).batch(32))
        repaired_model.get_layer("embedding").set_weights(emb_weights)
        repaired_model.get_layer("conv1d").set_weights(conv_weights)
        repaired_model.get_layer("class_probs").set_weights(dense_weights)
        # Smoke test: fail fast if lookup table is still not initialized.
        repaired_model.predict(np.array(["healthcheck"], dtype=object), verbose=0)
        return repaired_model, ""
    except Exception as exc:
        return None, f"repair_failed:{exc}"


def _load_text_cnn_model(model_path=None):
    global _TEXT_CNN_MODEL_CACHE, _TEXT_CNN_MODEL_PATH_CACHE

    runtime_ready, runtime_err = _ensure_text_cnn_runtime_stack()
    if not runtime_ready:
        return None, runtime_err or "tensorflow_not_installed"

    resolved_path = Path(model_path or _default_text_cnn_model_path())
    artifact_source = _resolve_text_cnn_model_source(resolved_path)
    if not resolved_path.exists():
        logger.warning(
            "Text-CNN artifact missing | path=%s | source=%s",
            str(resolved_path),
            artifact_source,
        )
        return None, f"model_not_found:{resolved_path}"

    if _TEXT_CNN_MODEL_CACHE is not None and _TEXT_CNN_MODEL_PATH_CACHE == str(resolved_path):
        logger.info(
            "Text-CNN model cache hit | path=%s | source=%s",
            str(resolved_path),
            artifact_source,
        )
        return _TEXT_CNN_MODEL_CACHE, None

    try:
        loaded = tf.keras.models.load_model(resolved_path, compile=False)
        try:
            loaded.predict(np.array(["healthcheck"], dtype=object), verbose=0)
        except Exception as predict_exc:
            lowered = str(predict_exc).lower()
            if "table not initialized" in lowered:
                repaired, repair_err = _repair_text_vectorization_table(
                    loaded,
                    model_path=resolved_path,
                )
                if repaired is None:
                    return None, f"model_predict_error:{predict_exc}|{repair_err}"
                loaded = repaired
            else:
                return None, f"model_predict_error:{predict_exc}"
        _TEXT_CNN_MODEL_CACHE = loaded
        _TEXT_CNN_MODEL_PATH_CACHE = str(resolved_path)
        logger.info(
            "Text-CNN model loaded | path=%s | source=%s",
            str(resolved_path),
            artifact_source,
        )
        return _TEXT_CNN_MODEL_CACHE, None
    except Exception as exc:
        logger.exception(
            "Text-CNN load failed | path=%s | source=%s | error=%s",
            str(resolved_path),
            artifact_source,
            str(exc),
        )
        return None, f"model_load_error:{exc}"


def _load_text_cnn_labels(label_map_path=None):
    resolved_label_map = Path(label_map_path or _default_label_map_path_for_model(_default_text_cnn_model_path()))
    label_map_path = resolved_label_map
    if not label_map_path.exists():
        return None, f"label_map_not_found:{label_map_path}"
    try:
        payload = json.loads(label_map_path.read_text(encoding="utf-8"))
        classes = payload.get("classes") or []
        if not isinstance(classes, list) or not classes:
            return None, "invalid_label_map"
        return [str(c) for c in classes], None
    except Exception as exc:
        return None, f"label_map_error:{exc}"


def _predict_text_cnn_labels(*, text, model_path, label_map_path):
    message = str(text or "").strip()
    if not message:
        return None, "empty_text"

    runtime_ready, runtime_err = _ensure_text_cnn_runtime_stack()
    if not runtime_ready:
        return None, runtime_err or "tensorflow_not_installed"

    model, model_err = _load_text_cnn_model(model_path=model_path)
    if model is None:
        return None, model_err

    classes, label_err = _load_text_cnn_labels(label_map_path=label_map_path)
    if classes is None:
        return None, label_err

    try:
        probs = model.predict(np.array([message], dtype=object), verbose=0)[0]
        pred_idx = int(np.argmax(probs))
        top_idx = np.argsort(probs)[::-1][:3]
        return {
            "predicted_class": classes[pred_idx],
            "confidence": float(probs[pred_idx]),
            "top_3": [
                {"label": classes[int(i)], "confidence": float(probs[int(i)])}
                for i in top_idx
            ],
            "label_space": [str(c) for c in classes],
        }, None
    except Exception as exc:
        return None, f"predict_error:{exc}"


def _predict_accommodation_class_from_text(text):
    if _is_text_cnn_intent_disabled():
        return None, "disabled_by_environment"
    model_path, artifact_source = _resolve_accommodation_text_cnn_model_path()
    label_map_path = _default_label_map_path_for_model(model_path)
    payload, err = _predict_text_cnn_labels(
        text=text,
        model_path=model_path,
        label_map_path=label_map_path,
    )
    if payload is None:
        return None, err
    payload.pop("label_space", None)
    payload["artifact_source"] = artifact_source
    return payload, None


def _format_cnn_prediction_for_chat(cnn_prediction):
    if not cnn_prediction:
        return ""

    def _to_domain_label(raw_label):
        label = str(raw_label or "").strip().lower()
        remap = {
            "hostel": "hotel",
            "transient_house": "hotel",
            "transient house": "hotel",
        }
        return remap.get(label, label or "unknown")

    predicted = _to_domain_label(cnn_prediction.get("predicted_class", "unknown"))
    confidence = float(cnn_prediction.get("confidence", 0.0))
    top_3 = cnn_prediction.get("top_3") or []

    lines = [
        "No DB match yet for the current filters.",
        f"CNN predicted type: {predicted}",
        f"Confidence: {confidence:.3f}",
    ]

    if top_3:
        lines.append("Top 3 classes:")
        for item in top_3[:3]:
            label = _to_domain_label(item.get("label", "unknown"))
            score = float(item.get("confidence", 0.0))
            lines.append(f"- {label}: {score:.3f}")

    lines.append(
        "Note: Current accommodation DB results depend on available hotel/inn room records and filters."
    )
    return "\n".join(lines)


def _normalize_data_source(value):
    source = str(value or "").strip().lower()
    return source if source in _VALID_DATA_SOURCES else "unlabeled"


def _resolve_data_source(request=None, payload=None):
    if isinstance(payload, dict):
        payload_source = payload.get("data_source")
        if payload_source not in (None, ""):
            return _normalize_data_source(payload_source)

    if request is not None:
        try:
            header_source = request.headers.get("X-Data-Source", "")
        except Exception:
            header_source = ""
        if header_source:
            return _normalize_data_source(header_source)

    default_source = os.getenv("CHATBOT_DATA_SOURCE", "unlabeled")
    return _normalize_data_source(default_source)


def _safe_log_system_metric(
    *,
    endpoint,
    response_time_ms,
    success_flag,
    status_code=None,
    error_message="",
    request=None,
):
    try:
        SystemMetricLog.objects.create(
            module="chat",
            endpoint=endpoint,
            response_time_ms=max(int(response_time_ms), 0),
            success_flag=bool(success_flag),
            status_code=status_code,
            error_message=(error_message or "")[:1000],
            data_source=_resolve_data_source(request=request),
        )
    except Exception:
        # Logging must never break the chatbot response path.
        pass


def _safe_log_recommendation_event(request, intent):
    try:
        user = getattr(request, "user", None)
        if not user or not getattr(user, "is_authenticated", False):
            return

        item_ref = "chat:tour_recommendation_request"
        if intent in ("get_accommodation_recommendation", "gethotelrecommendation"):
            item_ref = "chat:accommodation_recommendation_request"

        session_key = ""
        if hasattr(request, "session"):
            session_key = request.session.session_key or ""

        RecommendationEvent.objects.create(
            user=user,
            event_type="view",
            item_ref=item_ref,
            session_id=session_key,
            data_source=_resolve_data_source(request=request),
        )
        if intent in ("get_accommodation_recommendation", "gethotelrecommendation"):
            RecommendationEvent.objects.create(
                user=user,
                event_type="view",
                item_ref="accommodation_search_started",
                session_id=session_key,
                data_source=_resolve_data_source(request=request),
            )
    except Exception:
        # Event logging is optional and should not affect chatbot behavior.
        pass


def _compose_click_item_ref(payload):
    if not isinstance(payload, dict):
        return "chat:accommodation_recommendation_click"

    room_id = _to_int(payload.get("room_id"), default=0)
    accom_id = _to_int(payload.get("accom_id"), default=0)
    rank = _to_int(payload.get("rank"), default=0)
    mode = str(payload.get("scoring_mode") or "").strip().lower()[:40]

    parts = []
    if room_id > 0:
        parts.append(f"room:{room_id}")
    if accom_id > 0:
        parts.append(f"accom:{accom_id}")
    if rank > 0:
        parts.append(f"rank:{rank}")
    if mode:
        parts.append(f"mode:{mode}")

    return "|".join(parts) if parts else "chat:accommodation_recommendation_click"


def _safe_log_recommendation_click(request, payload):
    try:
        user = getattr(request, "user", None)
        if not user or not getattr(user, "is_authenticated", False):
            return False, ""

        session_key = ""
        if hasattr(request, "session"):
            session_key = request.session.session_key or ""

        rating_score = _to_int(payload.get("rating_score"), default=0) if isinstance(payload, dict) else 0
        dwell_time = _to_int(payload.get("dwell_time_sec"), default=0) if isinstance(payload, dict) else 0
        item_ref = _compose_click_item_ref(payload)

        RecommendationEvent.objects.create(
            user=user,
            event_type="click",
            item_ref=item_ref[:100],
            rating_score=rating_score if 1 <= rating_score <= 5 else None,
            dwell_time_sec=dwell_time if dwell_time >= 0 else None,
            session_id=session_key,
            data_source=_resolve_data_source(request=request, payload=payload),
        )
        RecommendationEvent.objects.create(
            user=user,
            event_type="click",
            item_ref="accommodation_rooms_viewed",
            session_id=session_key,
            data_source=_resolve_data_source(request=request, payload=payload),
        )
        return True, item_ref
    except Exception:
        return False, ""


def _safe_log_chat_step_event(
    request,
    *,
    event_type="view",
    item_ref="",
    rating_score=None,
    dwell_time_sec=None,
):
    try:
        user = getattr(request, "user", None)
        if not user or not getattr(user, "is_authenticated", False):
            return
        normalized_type = str(event_type or "").strip().lower()
        if normalized_type not in {"view", "click", "save", "rate", "book"}:
            normalized_type = "view"
        session_key = ""
        if hasattr(request, "session"):
            session_key = request.session.session_key or ""
        RecommendationEvent.objects.create(
            user=user,
            event_type=normalized_type,
            item_ref=str(item_ref or "chat:step").strip()[:100],
            rating_score=rating_score if isinstance(rating_score, int) and 1 <= rating_score <= 5 else None,
            dwell_time_sec=dwell_time_sec if isinstance(dwell_time_sec, int) and dwell_time_sec >= 0 else None,
            session_id=session_key,
            data_source=_resolve_data_source(request=request),
        )
    except Exception:
        pass


def _safe_log_chat_runtime_event(request, *, event_key, detail=""):
    normalized_key = str(event_key or "").strip().lower().replace(" ", "_")[:80]
    if not normalized_key:
        return
    _safe_log_chat_step_event(
        request,
        event_type="view",
        item_ref=f"chat:{normalized_key}",
    )
    try:
        _safe_log_system_metric(
            endpoint=f"{getattr(request, 'path', '/api/chat/')}#event:{normalized_key}",
            response_time_ms=0,
            success_flag=True,
            status_code=200,
            error_message=str(detail or "")[:200],
            request=request,
        )
    except Exception:
        pass


def _safe_log_step_events_from_response(request, *, intent, response_payload):
    if not isinstance(response_payload, dict):
        return
    actor = _resolve_chat_actor(request)
    is_guest = actor.get("role") == "guest"
    if response_payload.get("recommendation_trace"):
        if intent in ("get_accommodation_recommendation", "gethotelrecommendation"):
            _safe_log_chat_step_event(
                request,
                event_type="view",
                item_ref="chat:accommodation_recommendation_rendered",
            )
            if is_guest:
                funnel = _GUEST_FUNNEL_EVENT_MAP.get("recommendation_shown") or {}
                _safe_log_chat_step_event(
                    request,
                    event_type=str(funnel.get("event_type") or "view"),
                    item_ref=str(funnel.get("item_ref") or "chat:funnel_recommendation_shown"),
                )
                _safe_log_chat_step_event(
                    request,
                    event_type="view",
                    item_ref="accommodation_recommendations_shown",
                )
        elif intent in ("get_recommendation", "gettourrecommendation"):
            _safe_log_chat_step_event(
                request,
                event_type="view",
                item_ref="chat:tour_recommendation_rendered",
            )

    if response_payload.get("booking_id") and response_payload.get("show_feedback_prompt"):
        _safe_log_chat_step_event(
            request,
            event_type="book",
            item_ref="chat:accommodation_booking_confirmed",
        )
    elif response_payload.get("booking_id"):
        _safe_log_chat_step_event(
            request,
            event_type="save",
            item_ref="chat:accommodation_booking_draft_or_pending",
        )
    if is_guest and response_payload.get("room_id") and intent in (
        "book_accommodation",
        "bookhotel",
        "book_hotel",
        "reserve_accommodation",
    ):
        click_ev = _GUEST_FUNNEL_EVENT_MAP.get("book_button_clicked") or {}
        flow_ev = _GUEST_FUNNEL_EVENT_MAP.get("booking_flow_started") or {}
        _safe_log_chat_step_event(
            request,
            event_type=str(click_ev.get("event_type") or "click"),
            item_ref=str(click_ev.get("item_ref") or "chat:funnel_book_button_clicked"),
        )
        _safe_log_chat_step_event(
            request,
            event_type=str(flow_ev.get("event_type") or "save"),
            item_ref=str(flow_ev.get("item_ref") or "chat:funnel_booking_flow_started"),
        )
        _safe_log_chat_step_event(
            request,
            event_type="save",
            item_ref="accommodation_preview_started",
        )

    if response_payload.get("billing_link"):
        _safe_log_chat_step_event(
            request,
            event_type="view",
            item_ref="chat:lgu_payment_handoff_ready",
        )
        if is_guest:
            billing_ev = _GUEST_FUNNEL_EVENT_MAP.get("billing_link_shown") or {}
            _safe_log_chat_step_event(
                request,
                event_type=str(billing_ev.get("event_type") or "view"),
                item_ref=str(billing_ev.get("item_ref") or "chat:funnel_billing_link_shown"),
            )
            _safe_log_chat_step_event(
                request,
                event_type="view",
                item_ref="accommodation_preview_completed",
            )
    if is_guest and response_payload.get("booking_id"):
        completed_ev = _GUEST_FUNNEL_EVENT_MAP.get("booking_completed") or {}
        _safe_log_chat_step_event(
            request,
            event_type=str(completed_ev.get("event_type") or "book"),
            item_ref=str(completed_ev.get("item_ref") or "chat:funnel_booking_completed"),
        )


def _safe_log_recommendation_result(request, intent, reply, params, cnn_prediction=None):
    return _safe_log_recommendation_result_with_metadata(
        request,
        intent,
        reply,
        params,
        cnn_prediction=cnn_prediction,
    )


def _safe_log_recommendation_result_with_metadata(
    request,
    intent,
    reply,
    params,
    cnn_prediction=None,
    *,
    message_text="",
    recommended_items=None,
    booking_linkage=None,
):
    try:
        user = getattr(request, "user", None)
        if not user or not getattr(user, "is_authenticated", False):
            return

        if intent not in (
            "get_recommendation",
            "gettourrecommendation",
            "get_tourism_information",
            "get_accommodation_recommendation",
            "gethotelrecommendation",
            "book_accommodation",
            "bookhotel",
            "book_hotel",
            "reserve_accommodation",
        ):
            return

        context_payload = {
            "intent": intent,
            "params": params if isinstance(params, dict) else {},
        }
        if message_text:
            context_payload["message_text"] = str(message_text)[:2000]
        if hasattr(request, "session"):
            context_payload["session_id"] = request.session.session_key or ""
        if cnn_prediction:
            context_payload["cnn_prediction"] = cnn_prediction
        if booking_linkage and isinstance(booking_linkage, dict):
            context_payload["booking_outcome"] = booking_linkage
        context_payload["data_source"] = _resolve_data_source(request=request)

        items_payload = []
        if isinstance(recommended_items, list):
            for item in recommended_items[:10]:
                if isinstance(item, dict):
                    items_payload.append(item)

        # Fallback to reply text if structured recommendation items are unavailable.
        if not items_payload:
            items_payload = [{"reply_text": str(reply or "")}]

        mode_counts = Counter()
        dt_source_counts = Counter()
        dt_scores = []
        for item in items_payload:
            if not isinstance(item, dict):
                continue
            mode = str(item.get("scoring_mode") or "").strip().lower()
            meta = item.get("meta") if isinstance(item.get("meta"), dict) else {}
            trace = meta.get("trace") if isinstance(meta.get("trace"), dict) else {}
            if not mode:
                mode = str(trace.get("scoring_mode") or "").strip().lower()
            if mode:
                mode_counts[mode] += 1

            dt_score = item.get("decision_tree_score")
            if not isinstance(dt_score, (int, float)):
                dt_score = trace.get("decision_tree_score")
            if isinstance(dt_score, (int, float)):
                dt_scores.append(float(dt_score))
            dt_source = item.get("decision_tree_source")
            if not isinstance(dt_source, str) or not dt_source.strip():
                dt_source = trace.get("decision_tree_source")
            dt_source = str(dt_source or "").strip().lower()
            if dt_source:
                dt_source_counts[dt_source] += 1

        if mode_counts:
            context_payload["hybrid_mode_counts"] = dict(mode_counts)
            context_payload["top_mode"] = mode_counts.most_common(1)[0][0]
        if dt_source_counts:
            context_payload["decision_tree_source_counts"] = dict(dt_source_counts)
            context_payload["decision_tree_top_source"] = dt_source_counts.most_common(1)[0][0]
        if dt_scores:
            context_payload["decision_tree_score_avg"] = round(sum(dt_scores) / len(dt_scores), 6)
            context_payload["decision_tree_score_count"] = len(dt_scores)
        if isinstance(cnn_prediction, dict):
            context_payload["cnn_artifact_source"] = str(cnn_prediction.get("artifact_source") or "")[:80]
            context_payload["cnn_predicted_class"] = str(cnn_prediction.get("predicted_class") or "")[:80]
            context_payload["cnn_confidence"] = float(cnn_prediction.get("confidence", 0.0) or 0.0)

        clicked_item_ref = ""
        if booking_linkage and isinstance(booking_linkage, dict):
            room_id = booking_linkage.get("room_id")
            booking_id = booking_linkage.get("booking_id")
            if room_id:
                clicked_item_ref = f"room:{room_id}"
            if room_id and booking_id:
                clicked_item_ref = f"room:{room_id}|booking:{booking_id}"

        RecommendationResult.objects.create(
            user=user,
            algorithm_version="v1-chat",
            context_json=context_payload,
            recommended_items_json=items_payload,
            top_k=max(len(items_payload), 1),
            clicked_item_ref=clicked_item_ref,
            data_source=_resolve_data_source(request=request),
        )
    except Exception:
        # Result logging should not affect chatbot behavior.
        pass


def _safe_log_chatbot_interaction(
    request,
    *,
    user_message,
    resolved_intent="",
    resolved_params=None,
    bot_response="",
    intent_classifier=None,
    response_nlg_source="",
    fallback_used=False,
    provenance=None,
):
    try:
        user = getattr(request, "user", None)
        if not user or not getattr(user, "is_authenticated", False):
            user = None

        params_payload = resolved_params if isinstance(resolved_params, dict) else {}
        params_payload = {str(k)[:60]: v for k, v in params_payload.items()}

        intent_classifier = intent_classifier if isinstance(intent_classifier, dict) else {}
        provenance_payload = provenance if isinstance(provenance, dict) else {}
        # Keep provenance compact and avoid accidentally logging large/sensitive payloads.
        compact_provenance = {
            "intent_source": str(intent_classifier.get("source") or "")[:80],
            "intent_confidence": float(intent_classifier.get("confidence", 0.0) or 0.0),
            "intent_error": str(intent_classifier.get("error") or "")[:160],
            "intent_artifact_source": str(intent_classifier.get("artifact_source") or "")[:80],
            "extra": provenance_payload,
        }

        ChatbotLog.objects.create(
            user=user,
            user_message=str(user_message or "")[:4000],
            resolved_intent=str(resolved_intent or "")[:80],
            resolved_params_json=params_payload,
            bot_response=str(bot_response or "")[:8000],
            intent_classifier_source=str(intent_classifier.get("source") or "")[:80],
            response_nlg_source=str(response_nlg_source or "")[:80],
            fallback_used=bool(fallback_used),
            provenance_json=compact_provenance,
            data_source=_resolve_data_source(request=request),
        )
    except Exception:
        # Canonical chat logging must never break chatbot responses.
        pass


def _chat_json_response(request, start_time, payload, status=200, error_message=""):
    response_payload = payload if isinstance(payload, dict) else payload
    try:
        if isinstance(response_payload, dict):
            if isinstance(response_payload.get("recommendation_trace"), list):
                response_payload["recommendation_trace"] = _normalize_chat_recommendation_trace(
                    response_payload.get("recommendation_trace")
                )
            if response_payload.get("needs_clarification"):
                existing_qr = response_payload.get("quick_replies")
                if not (isinstance(existing_qr, list) and existing_qr):
                    missing_slot = str(response_payload.get("missing_slot") or "").strip().lower()
                    clarification_qr = _slot_quick_replies(missing_slot)
                    if not clarification_qr:
                        clarification_qr = [{"label": "Help", "value": "help"}]
                    response_payload["quick_replies"] = clarification_qr
            fallback_text = str(response_payload.get("fulfillmentText") or "").strip()
            if not fallback_text:
                if response_payload.get("billing_link"):
                    response_payload["fulfillmentText"] = (
                        "You can continue using the link below."
                    )
                elif isinstance(response_payload.get("recommendation_trace"), list) and response_payload.get("recommendation_trace"):
                    response_payload["fulfillmentText"] = (
                        "Here are the recommendations I found based on your request."
                    )
                elif isinstance(response_payload.get("quick_replies"), list) and response_payload.get("quick_replies"):
                    response_payload["fulfillmentText"] = (
                        "Please choose one of the quick options below, or type your request."
                    )
                else:
                    response_payload["fulfillmentText"] = (
                        "I can help with your request. Please try rephrasing it in one sentence."
                    )

        context = getattr(request, "_chatbot_log_context", None)
        if isinstance(context, dict) and isinstance(response_payload, dict):
            response_payload = _inject_contextual_ux_suggestions(
                response_payload,
                intent=str(context.get("resolved_intent") or "").strip().lower(),
                actor_role=(
                    (context.get("provenance") or {}).get("chat_role")
                    if isinstance(context.get("provenance"), dict)
                    else ""
                ),
            )
            bot_text = str(response_payload.get("fulfillmentText") or "").strip()
            resolved_intent = str(context.get("resolved_intent") or "").strip().lower()
            user_message = str(context.get("user_message") or "").strip().lower()
            context_nlg_source = str(
                response_payload.get("response_nlg_source")
                or context.get("response_nlg_source")
                or ""
            ).strip()
            # Performance-only guard: deterministic accommodation slot-filling filters
            # like "suba under 1500" do not need external NLG rewriting.
            fast_accommodation_filter_turn = bool(
                resolved_intent in {"get_accommodation_recommendation", "gethotelrecommendation"}
                and re.search(r"\b(?:suba|poblacion|bayawan|villareal|tinago|ubos)\b", user_message)
                and (
                    re.search(r"\b(?:under|below|budget)\s*[0-9][0-9,]*(?:\.[0-9]+)?k?\b", user_message)
                    or re.search(r"\b(?:cheap|affordable|budget[-\s]?friendly)\b", user_message)
                )
            )
            fast_clarification_turn = bool(
                resolved_intent == "clarification"
                and (
                    bool(response_payload.get("needs_clarification"))
                    or _contains_any_phrase(
                        user_message,
                        (
                            "i want to go somewhere nice",
                            "where should i go",
                            "any recommendation",
                            "what can i do",
                        ),
                    )
                )
            )
            if (
                bot_text
                and not context_nlg_source
                and (
                    (
                        bool(response_payload.get("needs_clarification"))
                        and resolved_intent in {"get_accommodation_recommendation", "gethotelrecommendation"}
                        and re.search(r"\b(?:under|below|budget)\s*[0-9][0-9,]*(?:\.[0-9]+)?k?\b", user_message)
                        and re.search(r"\b(?:suba|poblacion|bayawan|villareal|tinago|ubos)\b", user_message)
                    )
                    or fast_accommodation_filter_turn
                    or fast_clarification_turn
                )
            ):
                context_nlg_source = "backend_structured_template"
                response_payload["response_nlg_source"] = context_nlg_source
                context["response_nlg_source"] = context_nlg_source
            if (
                bot_text
                and not context_nlg_source
                and status < 400
            ):
                finalized_text, finalized_source = generate_final_ai_response(
                    request=request,
                    intent=resolved_intent,
                    user_message=user_message,
                    backend_reply=bot_text,
                )
                if str(finalized_text or "").strip():
                    response_payload["fulfillmentText"] = str(finalized_text).strip()
                    bot_text = str(finalized_text).strip()
                context_nlg_source = str(finalized_source or "").strip()
                if context_nlg_source:
                    response_payload["response_nlg_source"] = context_nlg_source
                    context["response_nlg_source"] = context_nlg_source
            provenance = context.get("provenance") if isinstance(context.get("provenance"), dict) else {}
            if response_payload.get("needs_clarification"):
                provenance["used_clarification_response"] = True
                provenance["clarification_used"] = True
                missing_slot = str(response_payload.get("missing_slot") or "").strip()
                if missing_slot:
                    provenance["clarification_slot"] = missing_slot[:60]
            fallback_reason = str(response_payload.get("recommendation_fallback") or "").strip()
            if fallback_reason:
                provenance["fallback_reason"] = fallback_reason[:120]
            no_match_reasons = response_payload.get("no_match_reasons")
            if isinstance(no_match_reasons, list) and no_match_reasons:
                provenance["no_match_reasons"] = [str(v)[:60] for v in no_match_reasons[:4]]
                provenance["used_no_match_fallback"] = True
            if response_payload.get("error_code"):
                provenance["error_code"] = str(response_payload.get("error_code"))[:80]
            provenance["fallback_used"] = bool(
                context.get("fallback_used")
                or response_payload.get("needs_clarification")
                or response_payload.get("recommendation_fallback")
                or (
                    isinstance(response_payload.get("no_match_reasons"), list)
                    and bool(response_payload.get("no_match_reasons"))
                )
            )
            context["provenance"] = provenance
            detected_language = str(provenance.get("detected_language") or "").strip().lower()
            already_back_translated = "gemini_back_translate" in context_nlg_source
            if bot_text and detected_language not in ("", "en", "english") and not already_back_translated:
                translated = translate_to_user_language(bot_text, detected_language)
                translated_text = str(translated or "").strip()
                if translated_text:
                    response_payload["fulfillmentText"] = translated_text
                    if context_nlg_source:
                        context_nlg_source = f"{context_nlg_source}|gemini_back_translate"
                    else:
                        context_nlg_source = "gemini_back_translate"
                    response_payload["response_nlg_source"] = context_nlg_source
                    context["response_nlg_source"] = context_nlg_source
                    provenance["response_translated_to_user_language"] = True
                    context["provenance"] = provenance
    except Exception:
        # Translation/normalization should not block response delivery.
        response_payload = payload

    response = JsonResponse(response_payload, status=status)
    elapsed_ms = int((time.perf_counter() - start_time) * 1000)
    _safe_log_system_metric(
        endpoint=request.path,
        response_time_ms=elapsed_ms,
        success_flag=(200 <= status < 400),
        status_code=status,
        error_message=error_message,
        request=request,
    )
    try:
        context = getattr(request, "_chatbot_log_context", None)
        if isinstance(context, dict):
            payload_for_logs = response_payload if isinstance(response_payload, dict) else {}
            if payload_for_logs.get("needs_clarification"):
                _safe_log_chat_runtime_event(
                    request,
                    event_key="clarification_triggered",
                    detail=str(payload_for_logs.get("missing_slot") or ""),
                )
            if isinstance(payload_for_logs.get("no_match_reasons"), list) and payload_for_logs.get("no_match_reasons"):
                _safe_log_chat_runtime_event(
                    request,
                    event_key="no_match_database_result",
                    detail=",".join([str(v) for v in payload_for_logs.get("no_match_reasons", [])][:4]),
                )
            nlg_source_payload = str(payload_for_logs.get("response_nlg_source") or context.get("response_nlg_source") or "")
            if "guardrail_fallback" in nlg_source_payload:
                _safe_log_chat_runtime_event(
                    request,
                    event_key="output_guardrail_triggered",
                    detail=nlg_source_payload[:120],
                )
            provenance_payload = context.get("provenance") if isinstance(context.get("provenance"), dict) else {}
            runtime_models = provenance_payload.get("runtime_models") if isinstance(provenance_payload.get("runtime_models"), dict) else {}
            if "gemini" in nlg_source_payload:
                runtime_models["llm_provider"] = "gemini"
                runtime_models["gemini_model"] = str(os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite") or "").strip() or "gemini-2.5-flash-lite"
            elif "openai" in nlg_source_payload:
                runtime_models["llm_provider"] = "openai"
                runtime_models["openai_model"] = str(os.getenv("OPENAI_MODEL", "gpt-4o-mini") or "").strip() or "gpt-4o-mini"
            if nlg_source_payload:
                runtime_models["nlg_source"] = nlg_source_payload[:120]

            intent_classifier = context.get("intent_classifier") if isinstance(context.get("intent_classifier"), dict) else {}
            artifact_source = str(intent_classifier.get("artifact_source") or "").strip()
            if artifact_source:
                runtime_models["intent_cnn_artifact_source"] = artifact_source[:80]
            try:
                dt_status = get_decision_tree_runtime_status(force_reload=False)
                runtime_models["decision_tree_artifact_source"] = str(dt_status.get("source") or "")[:80]
                runtime_models["decision_tree_fallback_used"] = bool(dt_status.get("fallback_used"))
            except Exception:
                pass

            if runtime_models:
                provenance_payload["runtime_models"] = runtime_models
                context["provenance"] = provenance_payload
            bot_text = str(payload_for_logs.get("fulfillmentText") or "")
            _safe_log_chatbot_interaction(
                request,
                user_message=context.get("user_message", ""),
                resolved_intent=context.get("resolved_intent", ""),
                resolved_params=context.get("resolved_params", {}),
                bot_response=bot_text,
                intent_classifier=context.get("intent_classifier", {}),
                response_nlg_source=(
                    payload_for_logs.get("response_nlg_source")
                    or context.get("response_nlg_source", "")
                ),
                fallback_used=bool(
                    context.get("fallback_used")
                    or payload_for_logs.get("needs_clarification")
                    or payload_for_logs.get("recommendation_fallback")
                    or (
                        isinstance(payload_for_logs.get("no_match_reasons"), list)
                        and bool(payload_for_logs.get("no_match_reasons"))
                    )
                    or _is_nlg_fallback_source(
                        payload_for_logs.get("response_nlg_source")
                        or context.get("response_nlg_source", "")
                    )
                ),
                provenance=context.get("provenance", {}),
            )
    except Exception:
        pass
    return response


def _normalize_survey_response_items(raw_items):
    normalized = []
    if not isinstance(raw_items, list):
        return normalized
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        code = str(item.get("statement_code") or "").strip().upper()[:30]
        score = _to_int(item.get("likert_score"), default=0)
        comment = str(item.get("comment") or "").strip()[:1000]
        if not code or score < 1 or score > 5:
            continue
        normalized.append(
            {
                "statement_code": code,
                "likert_score": score,
                "comment": comment,
            }
        )
    return normalized


def _load_chat_state(request):
    return _load_chat_state_service(
        request,
        session_key=_CHAT_STATE_SESSION_KEY,
        ttl_seconds=_CHAT_STATE_TTL_SECONDS,
    )


def _save_chat_state(request, state):
    _save_chat_state_service(
        request,
        state,
        session_key=_CHAT_STATE_SESSION_KEY,
    )


def _clear_chat_state(request):
    _clear_chat_state_service(
        request,
        session_key=_CHAT_STATE_SESSION_KEY,
    )


def _load_saved_chat_preferences(request):
    raw = request.session.get(_CHAT_PREFERENCE_SESSION_KEY, {})
    return raw if isinstance(raw, dict) else {}


def _save_saved_chat_preferences(request, prefs):
    payload = prefs if isinstance(prefs, dict) else {}
    request.session[_CHAT_PREFERENCE_SESSION_KEY] = payload
    request.session.modified = True


def _clear_saved_chat_preferences(request):
    if _CHAT_PREFERENCE_SESSION_KEY in request.session:
        del request.session[_CHAT_PREFERENCE_SESSION_KEY]
        request.session.modified = True


def _is_reset_command(message):
    text = str(message or "").strip().lower()
    if not text:
        return False
    reset_phrases = {
        "reset",
        "start over",
        "startover",
        "clear",
        "clear chat",
        "clear state",
    }
    return text in reset_phrases or any(phrase in text for phrase in (" reset", "start over", "startover"))


def _normalize_iso_date(value):
    raw = str(value or "").strip()
    if not raw:
        return ""
    raw = re.sub(r"\s+", " ", raw).strip()
    raw = re.sub(r",\s*", ", ", raw)

    match = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", raw)
    if match:
        year = int(match.group(1))
        month = int(match.group(2))
        day = int(match.group(3))
        try:
            return datetime(year, month, day).date().isoformat()
        except Exception:
            return ""

    for fmt in ("%B %d, %Y", "%b %d, %Y", "%B %d %Y", "%b %d %Y"):
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue

    # Allow month/day inputs without year (e.g., "May 10") for conversational flow.
    inferred_year = timezone.localdate().year
    for fmt in ("%B %d", "%b %d"):
        try:
            parsed = datetime.strptime(raw, fmt).date()
            return parsed.replace(year=inferred_year).isoformat()
        except ValueError:
            continue
    return ""


def _has_stay_details(params):
    if not isinstance(params, dict):
        return False
    has_dates = bool(str(params.get("check_in") or "").strip()) and bool(
        str(params.get("check_out") or "").strip()
    )
    nights = _to_int(params.get("nights"), default=0)
    return has_dates or nights > 0


def _has_sufficient_accommodation_details(params):
    if not isinstance(params, dict):
        return False
    company_type = str(params.get("company_type") or "").strip().lower()
    has_type = company_type in {"hotel", "inn", "either"}
    has_location = bool(
        str(params.get("location") or "").strip()
        or str(params.get("location_anchor") or "").strip()
    )
    budget = _to_int(params.get("budget"), default=0)
    guests = _to_int(params.get("guests"), default=0)
    return (has_location or has_type) and (budget > 0 or guests > 0)


def _next_accommodation_clarifying_question(params):
    if not isinstance(params, dict):
        params = {}

    missing_fields = []

    company_type = str(params.get("company_type") or "").strip().lower()

    location = str(params.get("location") or "").strip()
    location_anchor = str(params.get("location_anchor") or "").strip()
    preference_tags = params.get("preference_tags") if isinstance(params.get("preference_tags"), list) else []
    if (not location) and (not location_anchor) and (not preference_tags):
        missing_fields.append(("location", "Preferred area/location in Bayawan"))

    guests = _to_int(params.get("guests"), default=0)
    if guests <= 0:
        missing_fields.append(("guests", "Number of guests"))

    budget = _to_int(params.get("budget"), default=0)
    if budget <= 0:
        missing_fields.append(("budget", "Budget per night in PHP"))

    if len(missing_fields) == 1:
        field = missing_fields[0][0]
        return field, _build_dynamic_accommodation_slot_question(field, params)

    if len(missing_fields) > 1:
        missing_keys = [field for field, _label in missing_fields]
        if "guests" in missing_keys and "budget" in missing_keys:
            return (
                "guests",
                "Sure. To narrow this down quickly, how many guests and what budget per night in PHP?",
            )
        if "location" in missing_keys and "budget" in missing_keys:
            return (
                "location",
                "Got it. Which area in Bayawan do you prefer, and what is your budget per night in PHP?",
            )
        # Ask the next most relevant missing slot instead of repeating a full checklist every turn.
        next_field = missing_fields[0][0]
        return next_field, _build_dynamic_accommodation_slot_question(next_field, params)

    return None, ""


def _stable_choice(options, seed_text):
    if not isinstance(options, list) or not options:
        return ""
    seed = str(seed_text or "")
    if not seed:
        return str(options[0])
    digest = hashlib.md5(seed.encode("utf-8")).hexdigest()
    idx = int(digest[:8], 16) % len(options)
    return str(options[idx])


def _build_dynamic_accommodation_slot_question(field, params):
    slot = str(field or "").strip().lower()
    params = params if isinstance(params, dict) else {}
    company_type = str(params.get("company_type") or "").strip().lower()
    location = str(params.get("location") or "").strip()
    guests = _to_int(params.get("guests"), default=0)
    budget = _to_int(params.get("budget"), default=0)
    prefix_bits = []
    if company_type in ("hotel", "inn"):
        prefix_bits.append(company_type)
    if location:
        prefix_bits.append(location)
    if guests > 0:
        prefix_bits.append(f"{guests} guest(s)")
    if budget > 0:
        prefix_bits.append(f"PHP {budget}")
    prefix = ""
    if prefix_bits:
        prefix = f"So far: {', '.join(prefix_bits)}. "

    if slot == "company_type":
        variants = [
            "Do you prefer a hotel, an inn, or either?",
            "For your stay, should I prioritize hotel options, inn options, or both?",
            "Which accommodation type do you want first: hotel, inn, or either?",
        ]
        return prefix + _stable_choice(variants, f"{slot}|{company_type}|{location}")
    if slot == "location":
        variants = [
            "Which area in Bayawan should I prioritize?",
            "What area in Bayawan would you like me to focus on?",
            "Please share your preferred area in Bayawan so I can narrow results.",
        ]
        return prefix + _stable_choice(variants, f"{slot}|{company_type}|{guests}")
    if slot == "budget":
        variants = [
            "What is your budget per night in PHP?",
            "Please share your preferred nightly budget in PHP.",
            "How much is your target budget per night (PHP)?",
        ]
        return prefix + _stable_choice(variants, f"{slot}|{location}|{guests}")
    if slot == "guests":
        variants = [
            "How many guests will stay in the room?",
            "Please indicate the total number of guests.",
            "How many people should the room accommodate?",
        ]
        return prefix + _stable_choice(variants, f"{slot}|{company_type}|{budget}")
    if slot == "stay_details":
        variants = [
            "Please provide your check-in/check-out dates (YYYY-MM-DD), or the number of nights.",
            "What are your stay dates (check-in/check-out), or how many nights do you plan to stay?",
            "To continue, share your check-in/check-out dates (YYYY-MM-DD) or tell me how many nights.",
        ]
        return prefix + _stable_choice(variants, f"{slot}|{location}|{budget}")
    return prefix + get_accommodation_slot_question(slot)


def _looks_like_slot_update(params):
    if not isinstance(params, dict):
        return False
    slot_keys = {
        "budget",
        "total_budget",
        "spending_style",
        "guests",
        "group_size",
        "party_type",
        "adults",
        "children",
        "location",
        "company_type",
        "check_in",
        "check_out",
        "duration_days",
        "nights",
        "accommodation_needed",
        "experience_style",
        "activity_mix",
        "preference_text",
        "amenities",
        "amenity",
        "preference_tags",
        "prefer_low_price",
        "clear_budget",
        "clear_location_anchor",
        "broaden_location",
        "broaden_company_type",
    }
    return any(key in params for key in slot_keys)


def _looks_like_tour_request(message):
    text = str(message or "").strip().lower()
    if not text:
        return False
    tour_keywords = ("tour", "itinerary", "schedule", "destination", "package")
    if any(keyword in text for keyword in tour_keywords):
        return True
    timeframe_hint = _extract_tour_timeframe_hint(text)
    if timeframe_hint and any(
        marker in text
        for marker in ("event", "events", "activity", "activities", "what can i do", "things to do", "happening")
    ):
        return True
    return False


def _is_stay_planning_request(message):
    text = str(message or "").strip().lower()
    if not text:
        return False
    planning_markers = (
        "plan my stay",
        "plan a stay",
        "plan my trip",
        "trip plan",
        "itinerary",
        "adjust to budget version",
        "make it family-friendly",
        "family version",
        "budget version",
        "make it cheaper",
        "what can i do with",
        "how can i spend",
        "help me plan",
        "budget plan",
        "budget planning",
        "can you plan it",
        "plan it",
        "spend",
        "suggest a hotel and places",
        "suggest a hotel and tourist spots",
    )
    if any(marker in text for marker in planning_markers):
        return True
    # Natural planning phrasing: number appears before budget (e.g., "10k budget").
    if bool(re.search(r"\b[0-9][0-9,]*(?:\.[0-9]+)?k?\s*budget\b", text, flags=re.IGNORECASE)):
        if any(token in text for token in ("plan", "trip", "stay", "bayawan", "spend", "days")):
            return True
    has_budget = bool(
        re.search(
            r"\b([0-9][0-9,]*(?:\.[0-9]+)?k?)\s*(?:php|peso|pesos)\b|\bbudget\b\s*[0-9][0-9,]*(?:\.[0-9]+)?k?\b|\b[0-9][0-9,]*(?:\.[0-9]+)?k?\s*budget\b",
            text,
            flags=re.IGNORECASE,
        )
    )
    tourism_scope = any(
        token in text
        for token in ("bayawan", "stay", "trip", "tour", "tourist", "attraction", "spot", "places to visit")
    )
    return has_budget and tourism_scope


def _next_stay_planning_clarifying_question(params):
    seed = str(params or "")
    budget_total = _to_int((params or {}).get("total_budget"), default=0)
    if budget_total <= 0:
        return (
            "total_budget",
            _pick_response_variant(
                [
                    "I'd love to help. What's your total budget in PHP for this Bayawan stay?",
                    "Sure, I can help with that. What’s your total budget in PHP for this trip?",
                    "Great, let’s plan it properly. What total budget in PHP are you working with?",
                ],
                seed_text=f"{seed}|plan-budget",
            ),
        )

    duration_days = _to_int((params or {}).get("duration_days"), default=0)
    if duration_days <= 0:
        return (
            "duration_days",
            _pick_response_variant(
                [
                    "How many days are you planning to stay in Bayawan?",
                    "How long are you planning to stay in Bayawan?",
                    "How many days should I use for your Bayawan plan?",
                ],
                seed_text=f"{seed}|plan-days",
            ),
        )

    party_type = str((params or {}).get("party_type") or "").strip().lower()
    group_size = _to_int((params or {}).get("group_size"), default=0)
    guests = _to_int((params or {}).get("guests"), default=0)
    if not party_type and group_size <= 0 and guests <= 0:
        return (
            "party_type",
            _pick_response_variant(
                [
                    "Are you traveling solo, as a couple, with family, or as a group?",
                    "Who are you traveling with: solo, couple, family, or group?",
                    "Is this for solo travel, a couple trip, a family trip, or a group?",
                ],
                seed_text=f"{seed}|plan-party",
            ),
        )

    return "", ""


def _estimate_tourism_spot_cost(row, spending_style="mid"):
    raw = " ".join(
        [
            str(getattr(row, "spot_name", "") or ""),
            str(getattr(row, "description", "") or ""),
            str(getattr(row, "location", "") or ""),
        ]
    ).lower()
    if any(token in raw for token in ("museum", "heritage", "landmark", "church", "plaza")):
        base = 120
        duration = "1-2 hours"
    elif any(token in raw for token in ("falls", "waterfall", "beach", "island", "nature", "park", "trail")):
        base = 250
        duration = "2-4 hours"
    else:
        base = 180
        duration = "1-3 hours"

    style = str(spending_style or "mid").strip().lower()
    if style in {"budget", "low"}:
        base = int(base * 0.85)
    elif style in {"premium", "high"}:
        base = int(base * 1.25)
    else:
        base = int(base)

    tags = []
    if base <= 150:
        tags.append("budget-friendly")
    if any(token in raw for token in ("family", "kids", "park", "plaza", "beach")):
        tags.append("family-friendly")
    if any(token in raw for token in ("nature", "falls", "beach", "river", "trail")):
        tags.append("relaxing")
    if any(token in raw for token in ("trail", "hike", "climb", "adventure")):
        tags.append("adventure")
    if not tags:
        tags.append("general-interest")

    return {
        "estimated_cost_per_person": max(base, 50),
        "suggested_duration": duration,
        "tags": tags[:3],
    }


def _build_budget_stay_plan_payload(params, message):
    input_params = params if isinstance(params, dict) else {}
    working = dict(input_params)

    total_budget = _to_int(working.get("total_budget"), default=0)
    if total_budget <= 0:
        total_budget = _to_int(working.get("budget"), default=0)
        if total_budget > 0:
            working["total_budget"] = total_budget

    duration_days = _to_int(working.get("duration_days"), default=0)
    if duration_days <= 0:
        nights = _to_int(working.get("nights"), default=0)
        if nights > 0:
            duration_days = max(1, nights)
            working["duration_days"] = duration_days
    if duration_days <= 0:
        duration_days = 2
        working["duration_days"] = duration_days
        working["_assumed_duration_days"] = True

    party_type = str(working.get("party_type") or "").strip().lower()
    guests = _to_int(working.get("guests"), default=0)
    group_size = _to_int(working.get("group_size"), default=0)
    if guests > 0 and group_size <= 0:
        group_size = guests
    if group_size <= 0:
        inferred_party_sizes = {"solo": 1, "couple": 2, "family": 4, "group": 5}
        group_size = inferred_party_sizes.get(party_type, 2)
        working["_assumed_group_size"] = True
    working["group_size"] = group_size

    if not party_type:
        if group_size <= 1:
            party_type = "solo"
        elif group_size == 2:
            party_type = "couple"
        elif group_size <= 4:
            party_type = "family"
        else:
            party_type = "group"
    working["party_type"] = party_type

    accommodation_needed = working.get("accommodation_needed")
    if accommodation_needed is None:
        accommodation_needed = True
    accommodation_needed = bool(accommodation_needed)
    working["accommodation_needed"] = accommodation_needed

    activity_mix = str(working.get("activity_mix") or "").strip().lower() or "mixed"
    spending_style = str(working.get("spending_style") or "").strip().lower() or "mid"
    location = str(working.get("location") or "Bayawan City").strip() or "Bayawan City"

    need_slot, question = _next_stay_planning_clarifying_question(working)
    if need_slot:
        return {
            "needs_clarification": True,
            "missing_slot": need_slot,
            "question": question,
            "params": working,
        }

    total_budget = _to_int(working.get("total_budget"), default=0)
    if total_budget <= 0:
        return {
            "needs_clarification": True,
            "missing_slot": "total_budget",
            "question": "Could you share your total budget in PHP so I can draft a realistic stay plan?",
            "params": working,
        }
    minimum_practical_budget = max(1200 * max(duration_days, 1), 2000)
    if total_budget < minimum_practical_budget:
        return {
            "needs_clarification": False,
            "params": working,
            "reply": (
                f"PHP {total_budget:,} may be too low for a full {duration_days}-day stay with tours. "
                "I can show a very budget-focused option, or you may increase the budget slightly."
            ),
            "quick_replies": [
                "show a very budget-focused option",
                f"budget {minimum_practical_budget}",
                "show available tours",
            ],
            "plan_signature": f"low-budget-warning|{total_budget}|{duration_days}",
        }

    accom_share = Decimal("0.45") if accommodation_needed else Decimal("0.00")
    activity_share = Decimal("0.40") if accommodation_needed else Decimal("0.75")
    flex_share = Decimal("1.00") - accom_share - activity_share

    if spending_style in {"budget", "low"}:
        accom_share = Decimal("0.35") if accommodation_needed else Decimal("0.00")
        activity_share = Decimal("0.45") if accommodation_needed else Decimal("0.78")
        flex_share = Decimal("1.00") - accom_share - activity_share
    elif spending_style in {"premium", "high"}:
        accom_share = Decimal("0.55") if accommodation_needed else Decimal("0.00")
        activity_share = Decimal("0.32") if accommodation_needed else Decimal("0.68")
        flex_share = Decimal("1.00") - accom_share - activity_share

    total_budget_dec = Decimal(total_budget)
    accommodation_budget_total = (total_budget_dec * accom_share).quantize(Decimal("1"))
    activity_budget_total = (total_budget_dec * activity_share).quantize(Decimal("1"))
    flex_budget_total = max((total_budget_dec * flex_share).quantize(Decimal("1")), Decimal("0"))

    nightly_budget = Decimal("0")
    if accommodation_needed and duration_days > 0:
        nightly_budget = (accommodation_budget_total / Decimal(max(duration_days, 1))).quantize(Decimal("1"))

    accommodation_rows = []
    if accommodation_needed:
        room_qs = (
            _approved_room_queryset()
            .filter(status="AVAILABLE", current_availability__gte=1)
            .filter(person_limit__gte=max(group_size, 1))
            .order_by("price_per_night", "room_id")
        )
        if location:
            room_qs = room_qs.filter(accommodation__location__icontains=location.split(",")[0].strip())
        if nightly_budget > 0:
            filtered = room_qs.filter(price_per_night__lte=nightly_budget)
            room_qs = filtered if filtered.exists() else room_qs.filter(price_per_night__lte=(nightly_budget * Decimal("1.25")))

        for room in list(room_qs[:12]):
            accom_name = str(getattr(room.accommodation, "company_name", "") or "").strip()
            room_name = str(getattr(room, "room_name", "") or "").strip()
            accom_location = str(getattr(room.accommodation, "location", "") or "").strip()
            if not _is_clean_public_record(accom_name, room_name, accom_location):
                continue
            if not accom_name or not room_name:
                continue
            link, label = _build_accommodation_official_link(room=room)
            accommodation_rows.append(
                {
                    "name": accom_name,
                    "type": str(getattr(room.accommodation, "company_type", "") or "").strip(),
                    "location": accom_location,
                    "description": str(getattr(room.accommodation, "description", "") or "").strip(),
                    "room_name": room_name,
                    "price_per_night": Decimal(str(getattr(room, "price_per_night", "0") or "0")),
                    "capacity": _to_int(getattr(room, "person_limit", 0), default=0),
                    "official_link": link,
                    "official_label": label,
                }
            )
            if len(accommodation_rows) >= 3:
                break

    tour_rows = []
    now = timezone.now()
    schedule_qs = (
        Tour_Schedule.objects.select_related("tour")
        .filter(tour__publication_status="published", end_time__gte=now)
        .exclude(status="cancelled")
        .order_by("start_time")
    )
    for sched in list(schedule_qs[:24]):
        tour_name = str(getattr(sched.tour, "tour_name", "") or "").strip()
        sched_id = str(getattr(sched, "sched_id", "") or "").strip()
        tour_desc = str(getattr(sched.tour, "description", "") or "").strip()
        if not _is_clean_public_record(tour_name, sched_id, tour_desc):
            continue
        if not tour_name or not sched_id:
            continue
        admission_sum = (
            Admission_Rates.objects.filter(tour_id=sched.tour).aggregate(total=Sum("price")).get("total")
            or Decimal("0")
        )
        estimated_per_person = Decimal(str(sched.price or 0)) + Decimal(str(admission_sum or 0))
        estimated_total = (estimated_per_person * Decimal(max(group_size, 1))).quantize(Decimal("1"))
        if estimated_total <= activity_budget_total * Decimal("1.15"):
            tour_rows.append(
                {
                    "tour_name": tour_name,
                    "sched_id": sched_id,
                    "description": tour_desc,
                    "duration_days": _to_int(getattr(sched, "duration_days", 1), default=1),
                    "estimated_per_person": estimated_per_person.quantize(Decimal("1")),
                    "estimated_total": estimated_total,
                }
            )
        if len(tour_rows) >= 3:
            break

    lines = [
        _pick_response_variant(
            [
                f"Here's a simple {duration_days}-day Bayawan plan for around PHP {total_budget:,}.",
                f"Here's a quick plan for your Bayawan stay at around PHP {total_budget:,} for {duration_days} day(s).",
                f"This is a practical {duration_days}-day Bayawan setup around PHP {total_budget:,}.",
            ],
            seed_text=f"{message}|plan-intro-v2",
        )
    ]

    assumption_parts = []
    if bool(working.get("_assumed_duration_days")):
        assumption_parts.append(f"{duration_days} days")
    if bool(working.get("_assumed_group_size")):
        assumption_parts.append(f"{group_size} travelers")
    if assumption_parts:
        lines.append(f"Assuming {', '.join(assumption_parts)}.")

    lines.extend(
        [
            "",
            "Budget plan:",
            f"- Stay: {'~PHP ' + format(int(nightly_budget), ',') + '/night' if accommodation_needed else 'not included'}",
            f"- Tours: ~PHP {int(activity_budget_total):,} total",
            f"- Food and transport: ~PHP {int(flex_budget_total):,} buffer",
        ]
    )

    lines.append("")
    lines.append("Accommodations (top matches):")
    if accommodation_rows:
        for idx, item in enumerate(accommodation_rows[:3], 1):
            fit_reason = _planning_fit_reason_for_accommodation(
                item,
                nightly_budget=nightly_budget,
                group_size=group_size,
                party_type=party_type,
                user_location=location,
            )
            lines.append(f"{idx}. {item['name']} - {item['room_name']}")
            lines.append(f"   - PHP {int(item['price_per_night']):,}/night | up to {item['capacity']} guests")
            lines.append(f"   - {item['location'] or 'Bayawan'}")
            lines.append(f"   - Why it fits: {fit_reason}")
    else:
        lines.append("- I don't have exact matches yet, but I can show the closest options.")

    lines.append("")
    lines.append("Tours (top matches):")
    if tour_rows:
        for idx, item in enumerate(tour_rows[:3], 1):
            fit_reason = _planning_fit_reason_for_tour(
                item,
                activity_budget_total=activity_budget_total,
                duration_days=duration_days,
            )
            lines.append(f"{idx}. {item['tour_name']}")
            lines.append(f"   - PHP {int(item['estimated_per_person']):,}/person | {item['duration_days']} day(s)")
            lines.append(f"   - Why it fits: {fit_reason}")
    else:
        lines.append("- I don't have exact matches yet, but here are the closest options.")

    lines.extend(
        [
            "",
            _pick_response_variant(
                [
                    "Want me to adjust this plan or show booking links?",
                    "I can refine this plan further if you want.",
                    "Need me to tweak this or pull official booking links?",
                ],
                seed_text=f"{message}|plan-next-v2",
            ),
        ]
    )

    quick_replies = [
        "adjust to budget version",
        "make it family-friendly",
        "show available tours",
        "show accommodation recommendations",
    ]

    payload = {
        "needs_clarification": False,
        "params": working,
        "reply": "\n".join(lines),
        "quick_replies": quick_replies,
        "plan_signature": _build_stay_plan_signature(working, accommodation_rows, tour_rows),
    }
    if accommodation_rows and accommodation_rows[0].get("official_link"):
        payload["billing_link"] = str(accommodation_rows[0].get("official_link"))
        payload["billing_link_label"] = str(
            accommodation_rows[0].get("official_label") or "Open Official Link"
        )
    return payload


def _extract_tour_timeframe_hint(message):
    text = str(message or "").strip().lower()
    if not text:
        return ""
    if "this weekend" in text or re.search(r"\bweekend\b", text):
        return "this weekend"
    if re.search(r"\btours?\s+today\b", text) or re.search(r"\bevents?\s+today\b", text) or re.search(r"\btoday\b", text):
        return "today"
    if "this week" in text or re.search(r"\bevents?\s+this week\b", text):
        return "this week"
    if "upcoming" in text:
        return "upcoming schedules"
    return ""


def _message_mentions_guest_count(message):
    text = str(message or "").strip().lower()
    if not text:
        return False
    return bool(re.search(r"\b\d+\s*(guest|guests|people|person|pax|bisita|katao)\b", text))


def _is_personalization_decline_message(message):
    text = str(message or "").strip().lower()
    if not text:
        return False
    decline_phrases = {
        "no",
        "nope",
        "nah",
        "different budget",
        "no thanks",
        "not now",
    }
    if text in decline_phrases:
        return True
    if re.search(r"^(no|nope|nah)\b", text):
        return True
    return ("different budget" in text) or ("give list" in text) or ("show list" in text)


def _is_personalization_accept_message(message):
    text = str(message or "").strip().lower()
    if not text:
        return False
    accept_phrases = {
        "yes",
        "y",
        "yes please",
        "sure",
        "ok",
        "okay",
        "same",
        "similar",
        "go ahead",
    }
    return (text in accept_phrases) or bool(re.search(r"^(yes|y)\b", text))


def _is_booking_confirmation_decline(message):
    text = str(message or "").strip().lower()
    if not text:
        return False
    return text in {"no", "nope", "cancel", "stop", "do not book", "don't book"}


def _is_booking_confirmation_accept(message):
    text = str(message or "").strip().lower()
    if not text:
        return False
    return text in {"yes", "y", "yes please", "confirm", "book it", "proceed"}


def _infer_user_accommodation_baseline(user):
    if not user or not getattr(user, "is_authenticated", False):
        return {}

    today = timezone.now().date()
    past_bookings = list(
        AccommodationBooking.objects.select_related("accommodation", "room")
        .filter(guest=user, check_out__lt=today)
    )
    if not past_bookings:
        return {}

    nightly_rates = []
    type_counter = Counter()
    location_counter = Counter()

    for booking in past_bookings:
        room = getattr(booking, "room", None)
        if room is not None and getattr(room, "price_per_night", None) not in (None, ""):
            nightly_rates.append(Decimal(str(room.price_per_night)))
        else:
            nights = max((booking.check_out - booking.check_in).days, 1)
            if booking.total_amount not in (None, "") and nights > 0:
                nightly_rates.append(Decimal(str(booking.total_amount)) / Decimal(nights))

        accom = getattr(booking, "accommodation", None)
        if accom is None:
            continue

        raw_type = str(getattr(accom, "company_type", "") or "").strip().lower()
        normalized_type = ""
        if "hotel" in raw_type and "inn" in raw_type:
            normalized_type = "either"
        elif "hotel" in raw_type:
            normalized_type = "hotel"
        elif "inn" in raw_type:
            normalized_type = "inn"
        if normalized_type:
            type_counter[normalized_type] += 1

        location_value = str(getattr(accom, "location", "") or "").strip().lower()
        if location_value:
            location_counter[location_value] += 1

    if not nightly_rates:
        return {}

    sorted_rates = sorted(nightly_rates)
    mid = len(sorted_rates) // 2
    typical_rate = sorted_rates[mid]

    common_type = type_counter.most_common(1)[0][0] if type_counter else ""
    common_location = location_counter.most_common(1)[0][0] if location_counter else ""

    return {
        "typical_budget": int(typical_rate),
        "budget_min": int(sorted_rates[0]),
        "budget_max": int(sorted_rates[-1]),
        "common_company_type": common_type,
        "common_location": common_location,
        "sample_size": len(past_bookings),
    }


def _build_personalization_defaults(params, baseline):
    if not isinstance(params, dict) or not isinstance(baseline, dict):
        return {}

    defaults = {}

    budget = _to_int(params.get("budget"), default=0)
    prefer_low_price = _to_bool(params.get("prefer_low_price"), default=False)
    if budget <= 0:
        if prefer_low_price:
            defaults["budget"] = 1500
        else:
            baseline_budget = _to_int(baseline.get("typical_budget"), default=0)
            if baseline_budget > 0:
                # Keep fallback realistic for Bayawan hotel/inn prototype ranges.
                defaults["budget"] = max(1200, min(baseline_budget, 2500))

    company_type = str(params.get("company_type") or "").strip().lower()
    baseline_type = str(baseline.get("common_company_type") or "").strip().lower()
    if company_type not in ("hotel", "inn", "either") and baseline_type in ("hotel", "inn", "either"):
        defaults["company_type"] = baseline_type

    location = str(params.get("location") or "").strip().lower()
    baseline_location = str(baseline.get("common_location") or "").strip().lower()
    if not location and baseline_location:
        defaults["location"] = baseline_location

    return defaults


def _build_personalization_offer_text(defaults, baseline):
    parts = []
    budget_value = _to_int(defaults.get("budget"), default=0)
    if budget_value > 0:
        parts.append(f"around PHP {budget_value}")

    company_type = str(defaults.get("company_type") or "").strip().lower()
    if company_type in ("hotel", "inn", "either"):
        if company_type == "either":
            parts.append("hotel or inn")
        else:
            parts.append(f"{company_type} stays")

    location = str(defaults.get("location") or "").strip()
    if location:
        parts.append(f"in {location.title()}")

    if not parts:
        return ""

    basis_text = "If you'd like, I can narrow this down with a quick default setup"
    defaults_text = ", ".join(parts)
    return _template_personalization_offer_text(basis_text, defaults_text)


def _to_int(value, default=0):
    try:
        if value in ("", None):
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _to_decimal(value, default=Decimal("0")):
    try:
        if value in ("", None):
            return default
        return Decimal(str(value))
    except Exception:
        return default


def _safe_file_url(file_field):
    if not file_field:
        return ""
    try:
        return str(file_field.url or "").strip()
    except Exception:
        return ""


def _parse_compact_number(raw_value):
    """
    Parse compact numeric chat inputs (e.g. '8000', '8,000', '8k').
    Returns int or None when invalid.
    """
    value = str(raw_value or "").strip().lower().replace(",", "")
    if not value:
        return None
    multiplier = 1
    if value.endswith("k"):
        multiplier = 1000
        value = value[:-1].strip()
    try:
        parsed = Decimal(value)
    except Exception:
        return None
    if parsed < 0:
        return None
    return int(parsed * multiplier)


def _resolve_guests(params):
    guests = _to_int(params.get("guests"), default=0)
    if guests > 0:
        return guests

    adults = _to_int(params.get("adults"), default=0)
    children = _to_int(params.get("children"), default=0)
    total = adults + children
    return total if total > 0 else 0


def _looks_like_debug_or_test_record(value):
    text = str(value or "").strip().lower()
    if not text:
        return False
    return bool(
        re.search(
            r"\b(debug|prototype|placeholder|dummy|test(?:\s|$)|dbg\d*)\b",
            text,
            flags=re.IGNORECASE,
        )
    )


def _is_clean_public_record(*values):
    for value in values:
        if _looks_like_debug_or_test_record(value):
            return False
    return True


def _planning_fit_reason_for_accommodation(item, nightly_budget, group_size, party_type, user_location):
    price = _to_int(item.get("price_per_night"), default=0)
    capacity = _to_int(item.get("capacity"), default=0)
    location = str(item.get("location") or "").strip().lower()
    location_hint = str(user_location or "").strip().lower()

    if nightly_budget > 0 and price > 0 and price <= nightly_budget:
        return "within budget"
    if party_type == "family" and capacity >= 4:
        return "good for families"
    if group_size > 0 and capacity >= group_size:
        return "matches your group size"
    if location_hint and location and location_hint.split(",")[0].strip() in location:
        return "near your location"
    return "popular choice"


def _planning_fit_reason_for_tour(item, activity_budget_total, duration_days):
    estimated_total = _to_int(item.get("estimated_total"), default=0)
    per_person = _to_int(item.get("estimated_per_person"), default=0)
    tour_days = _to_int(item.get("duration_days"), default=0)

    if activity_budget_total > 0 and estimated_total > 0 and estimated_total <= _to_int(activity_budget_total, default=0):
        return "within budget"
    if duration_days > 0 and tour_days > 0 and tour_days <= duration_days:
        return "short duration and fits your time"
    if per_person > 0 and per_person <= 1200:
        return "budget-friendly"
    return "popular choice"


def _build_stay_plan_signature(params, accommodation_rows, tour_rows):
    parts = [
        str(_to_int((params or {}).get("total_budget"), default=0)),
        str(_to_int((params or {}).get("duration_days"), default=0)),
        str(_to_int((params or {}).get("group_size"), default=0)),
        str((params or {}).get("party_type") or "").strip().lower(),
        str((params or {}).get("spending_style") or "").strip().lower(),
        str((params or {}).get("activity_mix") or "").strip().lower(),
        ",".join(str((row or {}).get("name") or "").strip().lower() for row in (accommodation_rows or [])[:3]),
        ",".join(str((row or {}).get("tour_name") or "").strip().lower() for row in (tour_rows or [])[:3]),
    ]
    return "|".join(parts)


def _compose_accommodation_context_summary(params):
    if not isinstance(params, dict):
        return ""
    parts = []
    company_type = str(params.get("company_type") or "").strip().lower()
    if company_type in ("hotel", "inn"):
        parts.append(company_type)
    elif company_type == "either":
        parts.append("hotel or inn")

    location = str(params.get("location") or "").strip()
    if location:
        parts.append(f"near {location}")

    guests = _to_int(params.get("guests"), default=0)
    if guests > 0:
        parts.append(f"for {guests} guest{'s' if guests != 1 else ''}")

    budget = _to_int(params.get("budget"), default=0)
    if budget > 0:
        parts.append(f"under PHP {budget}")
    elif bool(params.get("prefer_low_price")):
        parts.append("favoring budget-friendly options")

    preference_tags = params.get("preference_tags") if isinstance(params.get("preference_tags"), list) else []
    if preference_tags:
        parts.append(f"with preferences: {', '.join(str(tag) for tag in preference_tags[:3])}")
    room_type = str(params.get("room_type") or "").strip()
    if room_type:
        parts.append(f"room type: {room_type}")

    check_in = str(params.get("check_in") or "").strip()
    check_out = str(params.get("check_out") or "").strip()
    nights = _to_int(params.get("nights"), default=0)
    if check_in and check_out:
        parts.append(f"from {check_in} to {check_out}")
    elif nights > 0:
        parts.append(f"for {nights} night(s)")

    return ", ".join(parts)


def _format_known_accommodation_details(*, company_type="", location="", budget=Decimal("0"), guests=0):
    parts = []
    normalized_type = str(company_type or "").strip().lower()
    normalized_location = str(location or "").strip()
    normalized_budget = _to_decimal(budget, default=Decimal("0"))
    normalized_guests = _to_int(guests, default=0)

    if normalized_type in ("hotel", "inn", "either"):
        parts.append(f"type: {normalized_type}")
    if normalized_location:
        parts.append(f"location: {normalized_location}")
    if normalized_budget > 0:
        parts.append(f"budget: PHP {normalized_budget:.2f}")
    if normalized_guests > 0:
        parts.append(f"guests: {normalized_guests}")
    return "; ".join(parts)


def _inject_recommendation_context(response_payload, params=None, default_summary=""):
    if not isinstance(response_payload, dict):
        return
    summary = _compose_accommodation_context_summary(params if isinstance(params, dict) else {})
    if summary:
        response_payload["recommendation_context_summary"] = summary
        return
    fallback = str(default_summary or "").strip()
    if fallback:
        response_payload["recommendation_context_summary"] = fallback


def _resolve_accommodation_result_limit(params):
    requested = _to_int((params or {}).get("result_limit"), default=5)
    if requested <= 0:
        requested = 5
    return max(1, min(requested, 10))


def _get_recommendations(params):
    try:
        results = recommend_tours(params, limit=3)
    except Exception:
        results = []
    timeframe_hint = str((params or {}).get("tour_timeframe_hint") or "").strip()
    filtered_results = []
    for item in results:
        title = str(getattr(item, "title", "") or "").strip()
        subtitle = str(getattr(item, "subtitle", "") or "").strip()
        if not title or not subtitle:
            continue
        if not _is_clean_public_record(title, subtitle):
            continue
        filtered_results.append(item)
    results = filtered_results[:3]
    if not results:
        now = timezone.now()
        public_schedule_qs = (
            Tour_Schedule.objects.select_related("tour")
            .filter(tour__publication_status="published", end_time__gte=now)
            .exclude(status="cancelled")
            .order_by("start_time")
        )
        fallback_rows = []
        for sched in list(public_schedule_qs[:8]):
            tour_obj = getattr(sched, "tour", None)
            title = str(getattr(tour_obj, "tour_name", "") or "").strip()
            sched_id = str(getattr(sched, "sched_id", "") or "").strip()
            if not title or not sched_id:
                continue
            if not _is_clean_public_record(title, sched_id, str(getattr(tour_obj, "description", "") or "")):
                continue
            price_text = f"PHP {Decimal(str(getattr(sched, 'price', 0) or 0)).quantize(Decimal('1')):,.0f} per guest"
            duration_days = _to_int(getattr(sched, "duration_days", 1), default=1)
            subtitle = f"{price_text} | {duration_days} day(s)"
            fallback_rows.append(
                SimpleNamespace(
                    score=0.0,
                    title=title,
                    subtitle=subtitle,
                    meta={"sched_id": sched_id},
                )
            )
            if len(fallback_rows) >= 3:
                break
        if fallback_rows:
            results = fallback_rows
        else:
            guests = _resolve_guests(params)
            budget = _to_int(params.get("budget"), default=0)
            pref_type = str(params.get("tour_type") or params.get("preferred_type") or "").strip()
            known_bits = []
            if guests > 0:
                known_bits.append(f"guests: {guests}")
            if budget > 0:
                known_bits.append(f"budget: PHP {budget}")
            if pref_type:
                known_bits.append(f"tour type: {pref_type}")
            known_text = (
                f"Thank you. I have recorded the following details: {'; '.join(known_bits)}.\n"
                if known_bits else ""
            )
            no_match_text = (
                "No tours are available right now."
            )
            if timeframe_hint:
                no_match_text = (
                    f"For {timeframe_hint}, no tours are currently available."
                )
            return (f"{known_text}{no_match_text}"), []

    intro = (
        f"For {timeframe_hint}, I found these available tour packages you can check."
        if timeframe_hint
        else "I found these available tour packages you can check."
    )
    items_payload = []
    for idx, item in enumerate(results, 1):
        meta = item.meta if isinstance(item.meta, dict) else {}
        sched_id = str(meta.get("sched_id") or "").strip()
        schedule = None
        if sched_id:
            schedule = (
                Tour_Schedule.objects.select_related("tour")
                .filter(sched_id__iexact=sched_id, tour__publication_status="published")
                .exclude(status="cancelled")
                .first()
            )
        if schedule is None:
            schedule = (
                Tour_Schedule.objects.select_related("tour")
                .filter(tour__tour_name__icontains=str(item.title or "").strip(), tour__publication_status="published")
                .exclude(status="cancelled")
                .order_by("start_time")
                .first()
            )
        if schedule is not None:
            card = _build_tour_card_trace(SimpleNamespace(build_absolute_uri=lambda x: x), schedule)
            if card:
                card["rank"] = idx
                card["score"] = round(float(item.score), 6)
                card["meta"] = meta
                items_payload.append(card)
                continue
        items_payload.append(
            {
                "kind": "tour",
                "rank": idx,
                "title": str(item.title or "").strip(),
                "subtitle": str(item.subtitle or "").strip(),
                "description": "You can view schedules first or start a booking request.",
                "score": round(float(item.score), 6),
                "meta": meta,
            }
        )
    return intro, items_payload


def _get_accommodation_recommendations(params):
    limit = _resolve_accommodation_result_limit(params)
    evaluation_limit = max(limit, 8)
    results, diagnostics = recommend_accommodations_with_diagnostics(params, limit=evaluation_limit)
    filtered_results = []
    for item in results:
        title = str(getattr(item, "title", "") or "").strip()
        subtitle = str(getattr(item, "subtitle", "") or "").strip()
        meta = item.meta if isinstance(item.meta, dict) else {}
        if not title or not subtitle:
            continue
        if not _is_clean_public_record(
            title,
            subtitle,
            meta.get("room_id"),
            meta.get("accom_id"),
            meta.get("location"),
            meta.get("description"),
        ):
            continue
        filtered_results.append(item)
    results = filtered_results
    context_summary = _compose_accommodation_context_summary(params)
    requested_amenities = []
    if isinstance(params, dict):
        amenity_raw = params.get("amenities") or params.get("amenity")
        if isinstance(amenity_raw, list):
            requested_amenities = [str(v).strip().lower() for v in amenity_raw if str(v).strip()]
        elif str(amenity_raw or "").strip():
            requested_amenities = [str(v).strip().lower() for v in re.split(r"[;,]", str(amenity_raw)) if str(v).strip()]
    requested_budget = _to_decimal(params.get("budget"), default=Decimal("0"))
    requested_budget_min = _to_decimal(params.get("budget_min"), default=Decimal("0"))
    requested_guests = _to_int(params.get("guests"), default=0)
    requested_company_type = str(params.get("company_type") or "").strip().lower()
    requested_room_type = str(params.get("room_type") or "").strip().lower()
    requested_location = str(params.get("location") or "").strip()

    def _item_price(candidate):
        meta = candidate.meta if isinstance(candidate.meta, dict) else {}
        return _to_decimal(meta.get("price_per_night"), default=Decimal("0"))

    budget_filtered_count = 0
    if requested_budget > 0 and results:
        in_budget = []
        above_budget = []
        for item in results:
            item_price = _item_price(item)
            if item_price > 0 and item_price <= requested_budget:
                in_budget.append(item)
            else:
                above_budget.append(item)
        if in_budget:
            results = in_budget
            budget_filtered_count = len(in_budget)
        else:
            results = above_budget
            budget_filtered_count = 0

    target_visible = max(3, min(limit, 5))

    def _build_supplemental_results(existing_items, needed):
        if needed <= 0:
            return []
        existing_room_ids = set()
        for existing in existing_items:
            meta = existing.meta if isinstance(getattr(existing, "meta", None), dict) else {}
            room_id = _to_int(meta.get("room_id"), default=0)
            if room_id > 0:
                existing_room_ids.add(room_id)

        location_hint = str(params.get("location") or "").strip()
        company_type = str(params.get("company_type") or "").strip().lower()
        qs = _approved_room_queryset().filter(status="AVAILABLE", current_availability__gte=1)
        if location_hint:
            qs = qs.filter(accommodation__location__icontains=location_hint)
        if requested_guests > 0:
            qs = qs.filter(person_limit__gte=requested_guests)

        in_budget_qs = qs
        if requested_budget > 0:
            in_budget_qs = qs.filter(price_per_night__lte=requested_budget)
            if in_budget_qs.exists():
                qs = in_budget_qs

        supplemental = []
        for room in qs.order_by("price_per_night", "room_id"):
            if len(supplemental) >= needed:
                break
            if room.room_id in existing_room_ids:
                continue
            accom = room.accommodation
            if not _is_clean_public_record(
                getattr(accom, "company_name", ""),
                getattr(room, "room_name", ""),
                getattr(accom, "location", ""),
                getattr(accom, "description", ""),
            ):
                continue
            accom_type = str(getattr(accom, "company_type", "") or "").strip().lower()
            reason_label = "Closest available option"
            if company_type == "hotel" and "inn" in accom_type:
                reason_label = "Inn option in your area"
            elif company_type == "inn" and "hotel" in accom_type:
                reason_label = "Hotel option in your area"

            subtitle = (
                f"{str(getattr(accom, 'location', '') or '').strip()} | "
                f"PHP {str(getattr(room, 'price_per_night', '') or '').strip()}/night | "
                f"up to {_to_int(getattr(room, 'person_limit', 0), default=0)} guests"
            )
            supplemental.append(
                SimpleNamespace(
                    score=0.0,
                    title=f"{str(getattr(accom, 'company_name', '') or '').strip()} - {str(getattr(room, 'room_name', '') or '').strip()}",
                    subtitle=subtitle,
                    meta={
                        "room_id": room.room_id,
                        "accom_id": room.accommodation_id,
                        "location": str(getattr(accom, "location", "") or "").strip(),
                        "price_per_night": str(getattr(room, "price_per_night", "") or "").strip(),
                        "person_limit": _to_int(getattr(room, "person_limit", 0), default=0),
                        "description": str(getattr(accom, "description", "") or "").strip(),
                        "phone_number": str(getattr(accom, "phone_number", "") or "").strip(),
                        "email_address": str(getattr(accom, "email_address", "") or "").strip(),
                        "official_booking_url": str(getattr(accom, "official_booking_url", "") or "").strip(),
                        "official_contact_url": str(getattr(accom, "official_contact_url", "") or "").strip(),
                        "profile_image_url": _safe_file_url(getattr(accom, "profile_picture", None)),
                        "supplemental_visibility": True,
                        "supplemental_reason": reason_label,
                    },
                )
            )
        return supplemental

    if len(results) < target_visible:
        supplement = _build_supplemental_results(results, target_visible - len(results))
        if supplement:
            results = list(results) + supplement

    results = results[:target_visible]
    if not results:
        no_match_reasons = diagnostics.get("no_match_reasons") or []
        suggested_budget_min = diagnostics.get("suggested_budget_min")
        location = str(params.get("location") or "").strip()
        budget = _to_decimal(params.get("budget"), default=Decimal("0"))
        guests = _to_int(params.get("guests"), default=0)
        company_type = str(params.get("company_type") or "").strip().lower()
        location_anchor = str(params.get("location_anchor") or "").strip()
        location_scope_note = str(params.get("location_scope_note") or "").strip()

        lines = []
        requested_type = company_type if company_type in ("hotel", "inn") else "hotel/inn"
        location_label = location.title() if location else "your preferred area"
        budget_label = f"under PHP {int(budget):,}" if budget > 0 else "within your budget"
        guest_label = f"for {guests} guest{'s' if guests != 1 else ''}" if guests > 0 else ""
        lines.append(
            f"I couldn't find an exact {requested_type} match in {location_label} {budget_label}{(' ' + guest_label) if guest_label else ''}."
        )
        quick_replies = []
        if "budget_too_low" in no_match_reasons and suggested_budget_min:
            min_budget_value = Decimal(str(suggested_budget_min))
            quick_replies.append(f"budget {int(min_budget_value)}")
            quick_replies.append("show nearest available options")
            quick_replies.append("include both hotel and inn")
            lines.append("You can try:")
            lines.append(f"- increase your budget to around PHP {int(min_budget_value):,}")
            lines.append("- check the nearest available options")
            lines.append("- include both hotel and inn")
        elif "location_too_narrow" in no_match_reasons:
            lines.append("You can try:")
            lines.append("- broaden to nearby barangays")
            lines.append("- include both hotel and inn")
            quick_replies.append("broaden location")
            quick_replies.append("include both hotel and inn")
        elif "type_too_narrow" in no_match_reasons:
            lines.append("You can try:")
            lines.append("- switch to either hotel or inn")
            lines.append("- view the nearest available stays")
            quick_replies.append("include both hotel and inn")
            quick_replies.append("show nearest available options")
        else:
            lines.append("You can try:")
            lines.append("- increasing your budget slightly")
            lines.append("- switching hotel/inn type")
            lines.append("- checking nearby barangays")
            quick_replies.append("show nearest available options")
            quick_replies.append("include both hotel and inn")

        if not quick_replies:
            quick_replies.append("show default hotel suggestions")
            quick_replies.append("broaden location")

        return "\n\n".join(lines), [], {
            "no_match_reasons": no_match_reasons,
            "suggested_budget_min": suggested_budget_min,
            "fallback_applied": diagnostics.get("fallback_applied", "none"),
            "quick_replies": quick_replies,
            "display_mode": "accommodation_list",
            "parsed_context": {
                "location": requested_location,
                "budget": float(requested_budget or 0),
                "budget_min": float(requested_budget_min or 0),
                "guests": requested_guests,
                "company_type": requested_company_type,
                "room_type": requested_room_type,
            },
        }

    if _should_use_accommodation_first_view(params):
        accommodation_reply, accommodation_items = _build_accommodation_first_payload(
            results,
            params,
            limit=target_visible,
        )
        if accommodation_items:
            return accommodation_reply, accommodation_items, {
                "fallback_applied": diagnostics.get("fallback_applied", "none"),
                "view_mode": "accommodation_first",
                "display_mode": "accommodation_list",
                "parsed_context": {
                    "location": requested_location,
                    "budget": float(requested_budget or 0),
                    "budget_min": float(requested_budget_min or 0),
                    "guests": requested_guests,
                    "company_type": requested_company_type,
                    "room_type": requested_room_type,
                },
            }

    lines = []
    fallback_applied = str(diagnostics.get("fallback_applied") or "none").strip().lower()
    has_only_above_budget = requested_budget > 0 and budget_filtered_count == 0

    if has_only_above_budget:
        requested_type = str(params.get("company_type") or "").strip().lower()
        requested_type_label = requested_type if requested_type in ("hotel", "inn") else "hotel/inn"
        preferred_area = str(params.get("location") or "").strip()
        location_part = f" in {preferred_area.title()}" if preferred_area else ""
        guest_part = (
            f" for {requested_guests} guest{'s' if requested_guests != 1 else ''}"
            if requested_guests > 0
            else ""
        )
        lines.append(
            f"I couldn't find {requested_type_label} options{location_part}{guest_part} under PHP {int(requested_budget):,} per night."
        )
        lines.append("Here are the closest available options above your budget:")
    else:
        lines.append(
            _pick_response_variant(
                [
                    "Here are some approved stays you can explore:",
                    "I found a few approved stays that match your request:",
                    "These approved stays should work well for your trip:",
                ],
                seed_text=f"{params}|accom-intro-v3",
            )
        )
    if context_summary:
        lines.append(f"Filters applied: {context_summary}.")

    items_payload = []
    for idx, item in enumerate(results, 1):
        item_meta = item.meta if isinstance(item.meta, dict) else {}
        room_id = item_meta.get("room_id")
        room_id_label = f" | Room ID: {room_id}" if room_id not in (None, "") else ""
        trace = item_meta.get("trace") if isinstance(item_meta.get("trace"), dict) else {}
        reasons = trace.get("reasons") if isinstance(trace.get("reasons"), list) else []
        match_score = trace.get("match_score")
        match_strength = str(trace.get("match_strength") or "").strip()
        price_per_night = _to_decimal(item_meta.get("price_per_night"), default=Decimal("0"))
        person_limit = _to_int(item_meta.get("person_limit"), default=0)
        preferred_location = str(params.get("location") or "").strip().lower()
        item_location = str(item_meta.get("location") or "").strip().lower()
        is_above_budget = requested_budget > 0 and price_per_night > requested_budget and price_per_night > 0

        concise_reasons = []
        supplemental_reason = str(item_meta.get("supplemental_reason") or "").strip()
        if supplemental_reason:
            concise_reasons.append(supplemental_reason)
        if requested_budget > 0 and price_per_night > 0 and price_per_night <= requested_budget:
            concise_reasons.append("Within your budget")
        elif requested_budget > 0 and is_above_budget:
            concise_reasons.append("One of the closest available options above your budget")
        if requested_guests > 0 and person_limit >= requested_guests:
            concise_reasons.append(f"Good for {requested_guests} guests")
        if preferred_location and item_location and preferred_location in item_location:
            concise_reasons.append(f"In {str(item_meta.get('location') or '').strip()}, which matches your preferred area")
        if not concise_reasons and person_limit > 0:
            concise_reasons.append("Suitable for a short stay")
        concise_reasons = concise_reasons[:3]

        title_text = str(item.title or "").strip()
        subtitle_text = str(item.subtitle or "").strip()
        lines.append(f"{idx}. {title_text}{room_id_label}")
        if subtitle_text and _normalize_chat_text(subtitle_text) != _normalize_chat_text(title_text):
            lines.append(f"   - {subtitle_text}")
        for concise_reason in concise_reasons:
            lines.append(f"   - Why it fits: {concise_reason}")
        if is_above_budget:
            lines.append(
                f"   - Budget note: PHP {int(price_per_night):,}/night (above your PHP {int(requested_budget):,} budget)."
            )
        items_payload.append(
            {
                "rank": idx,
                "title": item.title,
                "subtitle": item.subtitle,
                "score": round(float(item.score), 6),
                "ranking_score": round(float(item.score), 6),
                "room_id": item_meta.get("room_id"),
                "accom_id": item_meta.get("accom_id"),
                "location": str(item_meta.get("location") or ""),
                "price_per_night": str(item_meta.get("price_per_night") or ""),
                "person_limit": _to_int(item_meta.get("person_limit"), default=0),
                "description": str(item_meta.get("description") or ""),
                "phone_number": str(item_meta.get("phone_number") or ""),
                "email_address": str(item_meta.get("email_address") or ""),
                "official_booking_url": str(item_meta.get("official_booking_url") or ""),
                "official_contact_url": str(item_meta.get("official_contact_url") or ""),
                "profile_image_url": str(item_meta.get("profile_image_url") or ""),
                "why_fits": concise_reasons,
                "above_budget": bool(is_above_budget),
                "budget_note": (
                    f"Above budget by PHP {int(max(price_per_night - requested_budget, Decimal('0'))):,}"
                    if is_above_budget
                    else ""
                ),
                "match_score": match_score,
                "match_strength": match_strength,
                "decision_tree_score": trace.get("decision_tree_score"),
                "cnn_alignment": trace.get("cnn_alignment"),
                "scoring_mode": trace.get("scoring_mode"),
                "reasons": [str(r) for r in reasons],
                "meta": item_meta,
            }
        )
    lines.append(
        _pick_response_variant(
            [
                "Want me to refine these options or open official pages?",
                "I can narrow these further if you want.",
                "Need help comparing these or opening official links?",
            ],
            seed_text=f"{params}|accom-next-v3",
        )
    )
    return "\n".join(lines), items_payload, {
        "fallback_applied": diagnostics.get("fallback_applied", "none"),
        "fallback_reason": diagnostics.get("fallback_reason", ""),
        "fallback_reason_codes": diagnostics.get("fallback_reason_codes", []),
        "display_mode": "room_list",
        "parsed_context": {
            "location": requested_location,
            "budget": float(requested_budget or 0),
            "budget_min": float(requested_budget_min or 0),
            "guests": requested_guests,
            "company_type": requested_company_type,
            "room_type": requested_room_type,
        },
    }


def _safe_get_accommodation_recommendations(params):
    try:
        return _get_accommodation_recommendations(params)
    except Exception:
        fallback_text = (
            "I encountered a temporary issue while loading hotel/inn recommendations.\n"
            "Please try again, or refine your request with location, budget, and number of guests."
        )
        return fallback_text, [], {
            "fallback_applied": "runtime_error",
            "quick_replies": ["show default hotel suggestions"],
        }


def _should_use_accommodation_first_view(params):
    if not isinstance(params, dict):
        return True
    if _to_bool(params.get("force_room_cards"), default=False):
        return False
    if _to_bool(params.get("explicit_room_listing"), default=False):
        return False
    # Default policy for guest accommodation search:
    # show accommodation-level cards first unless the user explicitly asks for rooms.
    return True


def _build_accommodation_first_payload(results, params, *, limit=3):
    grouped = {}
    for item in results:
        if item is None:
            continue
        item_meta = item.meta if isinstance(item.meta, dict) else {}
        accom_id = _to_int(item_meta.get("accom_id"), default=0)
        if accom_id <= 0:
            continue
        title_text = str(getattr(item, "title", "") or "").strip()
        accommodation_name = str(title_text.split(" - ")[0] or "").strip() or "Accommodation"
        location = str(item_meta.get("location") or "").strip()
        price = _to_decimal(item_meta.get("price_per_night"), default=Decimal("0"))
        capacity = _to_int(item_meta.get("person_limit"), default=0)
        grouped.setdefault(
            accom_id,
            {
                "accom_id": accom_id,
                "accommodation_name": accommodation_name,
                "location": location,
                "description": str(item_meta.get("description") or "").strip(),
                "profile_image_url": str(item_meta.get("profile_image_url") or "").strip(),
                "official_booking_url": str(item_meta.get("official_booking_url") or "").strip(),
                "official_contact_url": str(item_meta.get("official_contact_url") or "").strip(),
                "min_price": price if price > 0 else Decimal("0"),
                "max_capacity": capacity,
            },
        )
        bucket = grouped[accom_id]
        if price > 0 and (bucket["min_price"] <= 0 or price < bucket["min_price"]):
            bucket["min_price"] = price
        if capacity > bucket["max_capacity"]:
            bucket["max_capacity"] = capacity
        if not bucket["location"] and location:
            bucket["location"] = location
        if not bucket["description"] and str(item_meta.get("description") or "").strip():
            bucket["description"] = str(item_meta.get("description") or "").strip()
        if not bucket["profile_image_url"] and str(item_meta.get("profile_image_url") or "").strip():
            bucket["profile_image_url"] = str(item_meta.get("profile_image_url") or "").strip()

    rows = list(grouped.values())[: max(1, min(int(limit), 5))]
    if not rows:
        return "", []

    lines = ["Here are approved accommodations you can explore:"]
    if str(params.get("location") or "").strip():
        lines.append(f"Area: {str(params.get('location') or '').strip().title()}")

    payload = []
    requested_room_hint = str(params.get("room_type") or "").strip().lower() if isinstance(params, dict) else ""
    for index, row in enumerate(rows, 1):
        price_label = (
            f"From PHP {int(row['min_price']):,}/night" if row["min_price"] > 0 else "Rate to be confirmed"
        )
        capacity_label = (
            f"Up to {row['max_capacity']} guests" if _to_int(row["max_capacity"], default=0) > 0 else "Capacity to be confirmed"
        )
        lines.append(f"{index}. {row['accommodation_name']}")
        lines.append(f"   - {row['location']}")
        lines.append(f"   - {price_label} | {capacity_label}")
        payload.append(
            {
                "rank": index,
                "item_type": "accommodation",
                "kind": "accommodation",
                "title": row["accommodation_name"],
                "accommodation_name": row["accommodation_name"],
                "subtitle": f"{row['location']} | {price_label}",
                "accom_id": row["accom_id"],
                "room_id": None,
                "location": row["location"],
                "price_per_night": str(row["min_price"]) if row["min_price"] > 0 else "",
                "person_limit": _to_int(row["max_capacity"], default=0),
                "description": row["description"],
                "profile_image_url": row["profile_image_url"],
                "official_booking_url": row["official_booking_url"],
                "official_contact_url": row["official_contact_url"],
                "why_fits": ["Approved accommodation option"],
            }
        )
        if requested_room_hint and index <= len(results):
            best_item = results[index - 1]
            best_meta = best_item.meta if isinstance(getattr(best_item, "meta", None), dict) else {}
            best_room_name = str(best_meta.get("room_name") or "").strip()
            if best_room_name:
                payload[-1]["why_fits"] = [
                    f"Has a {best_room_name} matching your room-type request"
                ]
    lines.append("You can open details or select Show Rooms to choose a room.")
    return "\n".join(lines), payload


def _resolve_room_image_url_for_chat(room):
    if room is None:
        return ""
    for field_name in ("image", "room_image", "profile_picture", "photo", "cover_image"):
        candidate = getattr(room, field_name, None)
        if not candidate:
            continue
        file_url = _safe_file_url(candidate)
        if file_url:
            return file_url
        if isinstance(candidate, str):
            candidate_url = str(candidate or "").strip()
            if candidate_url:
                return candidate_url
    accom = getattr(room, "accommodation", None)
    return _safe_file_url(getattr(accom, "profile_picture", None))


def _resolve_accommodation_by_name(accommodation_name):
    raw_name = str(accommodation_name or "").strip()
    if not raw_name:
        return None, []
    qs = _approved_accommodation_queryset()
    exact = qs.filter(company_name__iexact=raw_name).order_by("company_name").first()
    if exact is not None:
        return exact, [str(getattr(exact, "company_name", "") or "").strip()]

    contains_qs = qs.filter(company_name__icontains=raw_name).order_by("company_name")
    contains_rows = list(contains_qs[:6])
    if len(contains_rows) == 1:
        return contains_rows[0], [str(getattr(contains_rows[0], "company_name", "") or "").strip()]
    if len(contains_rows) > 1:
        names = [str(getattr(row, "company_name", "") or "").strip() for row in contains_rows if row is not None]
        return None, [name for name in names if name]

    all_names = [
        str(value or "").strip()
        for value in qs.values_list("company_name", flat=True)
        if str(value or "").strip()
    ]
    if not all_names:
        return None, []
    close = get_close_matches(raw_name, all_names, n=3, cutoff=0.72)
    if len(close) == 1:
        chosen = qs.filter(company_name__iexact=close[0]).order_by("company_name").first()
        return chosen, [str(close[0])]
    return None, close


def _extract_accommodation_name_for_room_listing(message, cached_rows, state_params=None):
    text = str(message or "").strip()
    if not text:
        if isinstance(state_params, dict):
            return str(state_params.get("selected_accommodation_name") or state_params.get("accom_name") or "").strip()
        return ""
    normalized_text = _normalize_chat_text(text)

    # Prefer exact mention of a known approved accommodation name in the message.
    known_names = [
        str(value or "").strip()
        for value in _approved_accommodation_queryset().values_list("company_name", flat=True)
        if str(value or "").strip()
    ]
    if known_names:
        matched = []
        for name in known_names:
            if _normalize_chat_text(name) and _normalize_chat_text(name) in normalized_text:
                matched.append(name)
        if matched:
            matched.sort(key=len, reverse=True)
            return matched[0]

    patterns = [
        r"\brooms?\s+for\s+(.+)$",
        r"\bin\s+(.+)$",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            candidate = str(match.group(1) or "").strip(" .,!?:;")
            if candidate:
                return candidate

    if isinstance(state_params, dict):
        selected_name = str(
            state_params.get("selected_accommodation_name")
            or state_params.get("accom_name")
            or ""
        ).strip()
        if selected_name:
            return selected_name

    unique_names = []
    for row in cached_rows or []:
        if not isinstance(row, dict):
            continue
        name = str(row.get("accommodation_name") or "").strip()
        if not name:
            title = str(row.get("title") or "").strip()
            name = str(title.split(" - ")[0] or "").strip()
        if name and name not in unique_names:
            unique_names.append(name)
    if len(unique_names) == 1:
        return unique_names[0]
    return ""


def _build_room_listing_response_for_accommodation(accommodation_name, *, limit=5):
    accom_name = str(accommodation_name or "").strip()
    if not accom_name:
        return {
            "fulfillmentText": "Which accommodation do you want to check rooms for?",
            "quick_replies": ["show approved accommodations in bayawan"],
            "needs_clarification": True,
            "missing_slot": "accommodation_name",
        }
    selected_accommodation, accommodation_choices = _resolve_accommodation_by_name(accom_name)
    if selected_accommodation is None:
        if len(accommodation_choices) > 1:
            choice_list = [str(v) for v in accommodation_choices[:3] if str(v).strip()]
            return {
                "fulfillmentText": f"Which accommodation do you mean: {', '.join(choice_list)}?",
                "quick_replies": choice_list,
                "needs_clarification": True,
                "missing_slot": "accommodation_name",
            }
        return {
            "fulfillmentText": f"I couldn't find {accom_name} in approved accommodation listings yet.",
            "quick_replies": ["show approved accommodations in bayawan"],
            "needs_clarification": True,
            "missing_slot": "accommodation_name",
        }
    rooms = list(
        _approved_room_queryset()
        .filter(accommodation=selected_accommodation)
        .filter(status="AVAILABLE")
        .order_by("price_per_night", "room_name")[: max(1, min(int(limit), 6))]
    )
    if not rooms:
        return {
            "fulfillmentText": f"I couldn't find available rooms for {selected_accommodation.company_name} right now.",
            "quick_replies": ["show approved accommodations in bayawan"],
        }

    lines = [f"Here are available rooms for {selected_accommodation.company_name}:"]
    trace = []
    for index, room in enumerate(rooms, 1):
        accom = room.accommodation
        lines.append(
            f"{index}. {accom.company_name} - {room.room_name}\n"
            f"   - PHP {room.price_per_night}/night | up to {room.person_limit} guests"
        )
        trace.append(
            {
                "rank": index,
                "item_type": "room",
                "kind": "room",
                "title": f"{accom.company_name} - {room.room_name}",
                "accommodation_name": str(accom.company_name or "").strip(),
                "subtitle": f"{accom.location} | PHP {room.price_per_night}/night | up to {room.person_limit} guests",
                "room_id": room.room_id,
                "accom_id": room.accommodation_id,
                "location": str(accom.location or "").strip(),
                "price_per_night": str(room.price_per_night),
                "person_limit": _to_int(room.person_limit, default=0),
                "description": str(accom.description or "").strip(),
                "profile_image_url": _resolve_room_image_url_for_chat(room),
                "official_booking_url": str(getattr(accom, "official_booking_url", "") or "").strip(),
                "official_contact_url": str(getattr(accom, "official_contact_url", "") or "").strip(),
                "why_fits": ["Matches your selected accommodation"],
            }
        )
    lines.append("Choose a room or ask me to create a booking preview.")
    return {
        "fulfillmentText": "\n".join(lines),
        "recommendation_trace": trace,
        "selected_accommodation_id": _to_int(getattr(selected_accommodation, "accom_id", 0), default=0),
        "selected_accommodation_name": str(getattr(selected_accommodation, "company_name", "") or "").strip(),
        "quick_replies": ["create booking preview"],
    }


def _is_broad_accommodation_discovery_query(message, params=None):
    text = _normalize_chat_text(message)
    params = params if isinstance(params, dict) else {}
    if not text:
        return False
    has_accommodation_scope = bool(
        re.search(r"\b(hotel|hotels|inn|inns|accommodation|accommodations|stay|stays|place to stay)\b", text)
    )
    if not has_accommodation_scope:
        return False
    if _is_accommodation_room_listing_command(text):
        return False
    if _to_int(params.get("guests"), default=0) > 0:
        return False
    if _to_decimal(params.get("budget"), default=Decimal("0")) > 0:
        return False
    if str(params.get("room_name") or params.get("room_reference") or "").strip():
        return False
    if re.search(r"\b(room|rooms?|under|below|budget|cheap|cheaper|affordable|for\s+\d+\s*(guest|guests|adult|adults|pax))\b", text):
        return False
    return True


def _build_broad_accommodation_discovery_response(params=None, *, limit=5):
    params = params if isinstance(params, dict) else {}
    location = str(params.get("location") or "").strip()
    company_type = str(params.get("company_type") or "").strip().lower()

    qs = _approved_accommodation_queryset().order_by("company_name")
    if company_type in {"hotel", "inn"}:
        qs = qs.filter(company_type__icontains=company_type)
    if location:
        qs = qs.filter(location__icontains=location)

    accommodations = list(qs[: max(3, min(int(limit), 8))])
    if not accommodations:
        location_part = f" in {location}" if location else ""
        return {
            "fulfillmentText": f"I couldn't find approved accommodations{location_part} right now.",
            "quick_replies": ["show approved accommodations in bayawan"],
            "needs_clarification": True,
        }

    lines = ["Here are approved accommodations you can check out:"]
    trace = []
    for idx, accom in enumerate(accommodations, 1):
        room_qs = (
            _approved_room_queryset()
            .filter(accommodation=accom, status="AVAILABLE", current_availability__gte=1)
            .order_by("price_per_night", "room_name")
        )
        first_room = room_qs.first()
        starting_rate = _to_decimal(getattr(first_room, "price_per_night", 0), default=Decimal("0")) if first_room else Decimal("0")
        max_capacity = _to_int(getattr(first_room, "person_limit", 0), default=0) if first_room else 0
        subtitle_parts = [str(getattr(accom, "location", "") or "").strip()]
        if starting_rate > 0:
            subtitle_parts.append(f"from PHP {starting_rate}/night")
        if max_capacity > 0:
            subtitle_parts.append(f"up to {max_capacity} guests")
        subtitle = " | ".join(part for part in subtitle_parts if part)
        lines.append(f"{idx}. {accom.company_name}\n   - {subtitle}")
        why = []
        if location:
            why.append("Matches your preferred area")
        if company_type in {"hotel", "inn"}:
            why.append(f"{company_type.title()} option")
        if not why:
            why.append("Approved accommodation option")
        trace.append(
            {
                "rank": idx,
                "item_type": "accommodation",
                "kind": "accommodation",
                "title": str(getattr(accom, "company_name", "") or "").strip(),
                "subtitle": subtitle,
                "accommodation_name": str(getattr(accom, "company_name", "") or "").strip(),
                "accom_id": _to_int(getattr(accom, "accom_id", 0), default=0),
                "location": str(getattr(accom, "location", "") or "").strip(),
                "price_per_night": str(starting_rate) if starting_rate > 0 else "",
                "person_limit": max_capacity,
                "description": str(getattr(accom, "description", "") or "").strip(),
                "profile_image_url": _safe_file_url(getattr(accom, "profile_picture", None)),
                "official_booking_url": str(getattr(accom, "official_booking_url", "") or "").strip(),
                "official_contact_url": str(getattr(accom, "official_contact_url", "") or "").strip(),
                "why_fits": why[:2],
            }
        )
    quick = [f"show rooms for {str(getattr(a, 'company_name', '') or '').strip()}" for a in accommodations[:3]]
    return {
        "fulfillmentText": "\n".join(lines),
        "recommendation_trace": trace,
        "quick_replies": quick,
    }


def _get_default_accommodation_suggestions(limit=3):
    room_qs = (
        _approved_room_queryset()
        .filter(status="AVAILABLE")
        .filter(current_availability__gte=1)
        .filter(
            Q(accommodation__company_type__icontains="hotel") |
            Q(accommodation__company_type__icontains="inn")
        )
    )

    rooms = list(room_qs.order_by("?")[:limit])

    # Fallback to any available accommodation rooms if no hotel/inn tag matches in DB.
    if not rooms:
        rooms = list(
            _approved_room_queryset()
            .filter(status="AVAILABLE")
            .filter(current_availability__gte=1)
            .order_by("?")[:limit]
        )

    if not rooms:
        return (
            "I don't have available hotel/inn room records to suggest yet. "
            "Please try again later or ask for a specific hotel/room."
        )

    grouped = {}
    for room in rooms:
        accom = room.accommodation
        if not _is_clean_public_record(
            getattr(accom, "company_name", ""),
            getattr(room, "room_name", ""),
            getattr(accom, "location", ""),
            getattr(accom, "description", ""),
        ):
            continue
        accom_id = _to_int(room.accommodation_id, default=0)
        if accom_id <= 0:
            continue
        current_price = _to_decimal(getattr(room, "price_per_night", 0), default=Decimal("0"))
        grouped.setdefault(
            accom_id,
            {
                "accom": accom,
                "min_price": current_price if current_price > 0 else Decimal("0"),
                "max_capacity": _to_int(getattr(room, "person_limit", 0), default=0),
            },
        )
        bucket = grouped[accom_id]
        if current_price > 0 and (bucket["min_price"] <= 0 or current_price < bucket["min_price"]):
            bucket["min_price"] = current_price
        if _to_int(getattr(room, "person_limit", 0), default=0) > bucket["max_capacity"]:
            bucket["max_capacity"] = _to_int(getattr(room, "person_limit", 0), default=0)

    accommodation_rows = list(grouped.values())[: max(1, min(int(limit), 5))]
    if not accommodation_rows:
        return (
            "I don't have available hotel/inn room records to suggest yet. "
            "Please try again later or ask for a specific hotel/room."
        )

    lines = ["Accommodation recommendations (suggested stays):"]
    suggestion_items = []
    for idx, row in enumerate(accommodation_rows, 1):
        accom = row["accom"]
        price_text = (
            f"From PHP {int(row['min_price']):,}/night" if row["min_price"] > 0 else "Rate to be confirmed"
        )
        lines.append(f"{idx}. {accom.company_name} | {accom.location} | {price_text}")
        suggestion_items.append(
            {
                "rank": idx,
                "item_type": "accommodation",
                "kind": "accommodation",
                "title": str(accom.company_name or "").strip(),
                "accommodation_name": str(accom.company_name or "").strip(),
                "subtitle": f"{accom.location} | {price_text}",
                "room_id": None,
                "accom_id": accom.accom_id,
                "official_booking_url": str(getattr(accom, "official_booking_url", "") or "").strip(),
                "official_contact_url": str(getattr(accom, "official_contact_url", "") or "").strip(),
                "profile_image_url": _safe_file_url(getattr(accom, "profile_picture", None)),
                "description": str(getattr(accom, "description", "") or "").strip(),
                "match_strength": "Suggested",
                "price_per_night": str(row["min_price"]) if row["min_price"] > 0 else "",
                "person_limit": row["max_capacity"],
                "location": str(accom.location or "").strip(),
                "why_fits": ["Approved accommodation option"],
            }
        )
    lines.append("Select one to view rooms.")
    return "\n".join(lines), suggestion_items


def _calculate_billing(params):
    guests = _resolve_guests(params)
    sched_id = str(params.get("sched_id") or params.get("schedule_id") or "").strip()
    tour_name = str(params.get("tour_name") or "").strip()

    schedule = None
    if sched_id:
        schedule = Tour_Schedule.objects.select_related("tour").filter(sched_id=sched_id).first()

    if schedule is None and tour_name:
        now = timezone.now()
        schedule = (
            Tour_Schedule.objects.select_related("tour")
            .filter(tour__tour_name__icontains=tour_name, end_time__gte=now)
            .exclude(status="cancelled")
            .order_by("start_time")
            .first()
        )

    if schedule is None:
        return (
            "I couldn't find that schedule. Please provide a valid sched_id "
            "or exact tour name."
        )

    base = Decimal(schedule.price) * guests
    admission_per_guest = (
        Admission_Rates.objects.filter(tour_id=schedule.tour).aggregate(total=Sum("price"))["total"]
        or Decimal("0")
    )
    admission_total = Decimal(admission_per_guest) * guests
    grand_total = base + admission_total

    return (
        f"Billing Summary for {schedule.tour.tour_name} ({schedule.sched_id}):\n"
        f"Guests: {guests}\n"
        f"Base fare: PHP {schedule.price} x {guests} = PHP {base:.2f}\n"
        f"Admission fees: PHP {admission_per_guest:.2f} x {guests} = PHP {admission_total:.2f}\n"
        f"Total amount due: PHP {grand_total:.2f}"
    )


def _build_find_another_accommodation_prompt(params):
    if not isinstance(params, dict):
        return "find another hotel"

    guests = _resolve_guests(params)
    budget = _to_int(params.get("budget"), default=0)
    location = str(params.get("location") or "").strip()
    company_type = str(params.get("company_type") or "").strip().lower()

    type_text = "hotel"
    if company_type in ("hotel", "inn"):
        type_text = company_type
    elif company_type == "either":
        type_text = "hotel/inn"

    parts = [f"find another {type_text}"]
    if location:
        parts.append(f"in {location}")
    if guests > 0:
        parts.append(f"for {guests} guests")
    if budget > 0:
        parts.append(f"under {budget}")

    prompt = " ".join(parts).strip()
    return prompt if prompt else "find another hotel"


def _sanitize_quick_replies(items, *, limit=4):
    if not isinstance(items, list):
        return []
    blocked_values = {
        "show available hotels and inns",
        "show default hotel suggestions",
        "show more hotels/inns",
        "why option 1",
        "how do i contact this property",
        "compare top 3",
        "adjust to budget version",
        "family-friendly",
        "show tours",
        "view accommodation links",
        "official booking links",
        "nearby dining too",
        "modify booking preview",
        "call accommodation",
        "email accommodation",
    }
    normalized = []
    for item in items:
        if isinstance(item, dict):
            value = str(item.get("value") or "").strip()
            if not value:
                continue
            label = str(item.get("label") or value).strip() or value
            if value.lower() in blocked_values or label.lower() in {
                "call accommodation",
                "email accommodation",
            }:
                continue
            normalized.append({"label": label[:80], "value": value[:300]})
            continue

        value = str(item or "").strip()
        if value and value.lower() not in blocked_values:
            normalized.append(value[:300])
    return normalized[:limit]


def _normalize_chat_recommendation_trace(items):
    if not isinstance(items, list):
        return []
    normalized_items = []
    for raw in items[:10]:
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        kind = str(item.get("kind") or item.get("item_type") or "").strip().lower()
        title = str(item.get("title") or "").strip()
        accom_name = str(item.get("accommodation_name") or "").strip()
        if not accom_name and title and " - " in title:
            accom_name = str(title.split(" - ")[0] or "").strip()
        if not accom_name and title:
            accom_name = title
        if kind not in {"accommodation", "room", "tour", "tour_schedule"}:
            kind = "room" if _to_int(item.get("room_id"), default=0) > 0 else "accommodation"

        accom_id = _to_int(item.get("accom_id") or item.get("accommodation_id"), default=0)
        detail_url = str(item.get("detail_url") or "").strip()
        if not detail_url and kind in {"accommodation", "room"} and accom_id > 0:
            try:
                detail_url = reverse("accommodation_detail_page", kwargs={"accom_id": accom_id})
            except Exception:
                detail_url = f"/guest_app/accommodation/{accom_id}/"
        if not detail_url and kind in {"tour", "tour_schedule"}:
            tour_id = str(item.get("tour_id") or "").strip()
            sched_id = str(item.get("sched_id") or "").strip()
            if tour_id:
                try:
                    detail_url = reverse("guest_book", kwargs={"tour_id": tour_id})
                except Exception:
                    detail_url = f"/guest_book/{tour_id}/"
                if sched_id:
                    detail_url = f"{detail_url}?sched_id={sched_id}"

        official_booking = str(item.get("official_booking_url") or item.get("official_url") or "").strip()
        official_contact = str(item.get("official_contact_url") or item.get("facebook_url") or "").strip()
        official_url = ""
        facebook_url = ""
        for candidate in (official_booking, official_contact):
            if not candidate:
                continue
            if "facebook.com" in candidate.lower():
                if not facebook_url:
                    facebook_url = candidate
            elif not official_url:
                official_url = candidate

        item["kind"] = kind
        if kind in {"accommodation", "room"}:
            item["accommodation_name"] = accom_name
        if detail_url:
            item["detail_url"] = detail_url
        if official_url and kind in {"accommodation", "room"}:
            item["official_url"] = official_url
        if facebook_url and kind in {"accommodation", "room"}:
            item["facebook_url"] = facebook_url
        description = str(item.get("description") or "").strip()
        if description:
            item["description"] = description[:160]
        normalized_items.append(item)
    return normalized_items


def _find_accommodation_room(params):
    room_id = params.get("room_id")
    selected_room_id = _to_int(params.get("selected_room_id"), default=0)
    selected_accommodation_id = _to_int(params.get("selected_accommodation_id"), default=0)
    guests = _resolve_guests(params)
    budget = _to_decimal(params.get("budget"), default=None)
    location = str(params.get("location") or "").strip()
    accom_name = str(params.get("accom_name") or params.get("hotel_name") or "").strip()

    qs = _approved_room_queryset().filter(status="AVAILABLE")

    if selected_room_id > 0:
        chosen = qs.filter(room_id=selected_room_id).first()
        if chosen is not None:
            return chosen

    if room_id not in ("", None):
        try:
            return qs.filter(room_id=int(room_id)).first()
        except (TypeError, ValueError):
            pass

    if selected_accommodation_id > 0:
        qs = qs.filter(accommodation_id=selected_accommodation_id)

    if guests > 0:
        qs = qs.filter(person_limit__gte=guests)
    if budget is not None:
        qs = qs.filter(price_per_night__lte=budget)
    if location:
        qs = qs.filter(accommodation__location__icontains=location)
    if accom_name:
        qs = qs.filter(accommodation__company_name__icontains=accom_name)

    return qs.order_by("price_per_night", "room_id").first()


def _extract_preview_accommodation_name(message):
    text = _normalize_chat_text(message)
    if not text:
        return ""
    patterns = [
        r"\b(?:create|make|prepare)?\s*(?:a\s+)?(?:booking\s+preview|preview\s+cost|booking\s+draft)\s+for\s+([a-z0-9][a-z0-9\s&\-\']{2,80})\b",
        r"\bhow much if i stay at\s+([a-z0-9][a-z0-9\s&\-\']{2,80})\b",
        r"\bestimate my stay at\s+([a-z0-9][a-z0-9\s&\-\']{2,80})\b",
        r"\bpreview cost for\s+([a-z0-9][a-z0-9\s&\-\']{2,80})\b",
        r"\bhow to book\s+([a-z0-9][a-z0-9\s&\-\']{2,80})\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if not match:
            continue
        candidate = " ".join(str(match.group(1) or "").split()).strip(" .,!?")
        if re.fullmatch(r"room\s+[a-z0-9\-]+", candidate, flags=re.IGNORECASE):
            continue
        if candidate and candidate not in {"this hotel", "this room", "that one", "first one"}:
            return candidate
    return ""


def _resolve_preview_room_selection(*, params=None, message="", cached_rows=None):
    params = params if isinstance(params, dict) else {}
    cached_rows = cached_rows if isinstance(cached_rows, list) else []
    normalized_message = _normalize_chat_text(message)
    selected_room_id = _to_int(params.get("selected_room_id"), default=0)
    selected_accommodation_name_hint = str(params.get("selected_accommodation_name") or "").strip()
    guests = _resolve_guests(params)
    budget = _to_decimal(params.get("budget"), default=Decimal("0"))

    explicit_name = str(
        params.get("accom_name")
        or params.get("hotel_name")
        or ""
    ).strip()
    if explicit_name and re.fullmatch(r"room\s+[a-z0-9\-]+", explicit_name, flags=re.IGNORECASE):
        explicit_name = ""
    if not explicit_name:
        explicit_name = _extract_preview_accommodation_name(message)
    if not explicit_name and selected_accommodation_name_hint:
        explicit_name = selected_accommodation_name_hint

    if explicit_name:
        explicit_room_hint = str(params.get("room_name") or params.get("room_reference") or "").strip()
        chosen_accommodation, candidate_names = _resolve_accommodation_by_name(explicit_name)
        if chosen_accommodation is None:
            def _norm_for_match(value):
                return " ".join(re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).split())

            all_names = [
                str(v or "").strip()
                for v in _approved_accommodation_queryset().values_list("company_name", flat=True)
                if str(v or "").strip()
            ]
            explicit_lower = explicit_name.lower()
            explicit_norm = _norm_for_match(explicit_name)
            for known_name in sorted(all_names, key=len, reverse=True):
                name_lower = known_name.lower()
                name_norm = _norm_for_match(known_name)
                if (
                    explicit_lower == name_lower
                    or explicit_lower.startswith(f"{name_lower} ")
                    or (name_norm and (explicit_norm == name_norm or explicit_norm.startswith(f"{name_norm} ") or f" {name_norm} " in f" {explicit_norm} "))
                ):
                    chosen_accommodation = (
                        _approved_accommodation_queryset()
                        .filter(company_name__iexact=known_name)
                        .order_by("company_name")
                        .first()
                    )
                    if chosen_accommodation is not None:
                        candidate_names = [known_name]
                        if not explicit_room_hint:
                            trailing = re.sub(re.escape(known_name), "", explicit_name, count=1, flags=re.IGNORECASE).strip(" -,:;")
                            if trailing and trailing.lower() not in {"room", "hotel", "inn", "accommodation"}:
                                explicit_room_hint = trailing
                    break
        if chosen_accommodation is None:
            if len(candidate_names) > 1:
                return {
                    "room": None,
                    "source": "explicit_name",
                    "ambiguous_names": candidate_names[:3],
                    "requested_name": explicit_name,
                }
            return {
                "room": None,
                "source": "explicit_name",
                "not_found_name": explicit_name,
            }

        chosen_name = str(getattr(chosen_accommodation, "company_name", "") or "").strip()
        if not explicit_room_hint and chosen_name:
            normalized_full = _normalize_chat_text(explicit_name)
            room_name_candidates = [
                str(v or "").strip()
                for v in (
                    _approved_room_queryset()
                    .filter(accommodation=chosen_accommodation, status="AVAILABLE")
                    .values_list("room_name", flat=True)
                )
                if str(v or "").strip()
            ]
            for room_name_candidate in sorted(room_name_candidates, key=len, reverse=True):
                if _normalize_chat_text(room_name_candidate) and _normalize_chat_text(room_name_candidate) in normalized_full:
                    explicit_room_hint = room_name_candidate
                    break
        # Handle phrases like "create booking preview for Hotel Maefinn Standard Room".
        if not explicit_room_hint and chosen_name:
            lowered_explicit = explicit_name.lower()
            lowered_chosen = chosen_name.lower()
            if lowered_explicit.startswith(lowered_chosen):
                trailing = explicit_name[len(chosen_name):].strip(" -,:;")
                if trailing and trailing.lower() not in {"room", "hotel", "inn", "accommodation"}:
                    explicit_room_hint = trailing
        if not explicit_room_hint and chosen_name:
            raw_message = str(message or "").strip()
            command_match = re.search(
                r"\b(?:create|make|prepare)?\s*(?:a\s+)?(?:booking\s+preview|preview\s+cost|booking\s+draft)\s+for\s+(.+)$",
                raw_message,
                flags=re.IGNORECASE,
            )
            if command_match:
                tail = str(command_match.group(1) or "").strip(" .,!?:;")
                if tail:
                    # Remove the selected accommodation prefix from the command tail,
                    # supporting common dash variants between names.
                    name_pattern = re.escape(chosen_name).replace(r"\-", r"[\-–—]").replace("–", r"[\-–—]").replace("—", r"[\-–—]")
                    reduced = re.sub(rf"^\s*{name_pattern}\s*", "", tail, count=1, flags=re.IGNORECASE).strip(" -,:;")
                    if not reduced:
                        norm_tail = _normalize_chat_text(tail)
                        norm_name = _normalize_chat_text(chosen_name)
                        if norm_tail.startswith(norm_name):
                            reduced = norm_tail[len(norm_name):].strip(" -,:;")
                    if reduced and reduced.lower() not in {"room", "hotel", "inn", "accommodation"}:
                        explicit_room_hint = reduced
        if not explicit_room_hint:
            message_candidate = str(message or "").strip()
            normalized_candidate = _normalize_chat_text(message_candidate)
            is_likely_detail_turn = bool(
                normalized_candidate
                and not re.search(r"\b(yes|no|confirm|proceed|okay|ok)\b", normalized_candidate)
                and not re.search(r"\b\d+\s*(guest|guests|adult|adults|pax|night|nights)\b", normalized_candidate)
                and not re.search(r"\b(jan|january|feb|february|mar|march|apr|april|may|jun|june|jul|july|aug|august|sep|sept|september|oct|october|nov|november|dec|december)\b", normalized_candidate)
            )
            if is_likely_detail_turn:
                cleaned = re.sub(r"^(room|show details for|details of)\s+", "", message_candidate, flags=re.IGNORECASE).strip(" .,!?:;")
                normalized_cleaned = _normalize_chat_text(cleaned)
                normalized_chosen = _normalize_chat_text(chosen_name)
                if cleaned and normalized_cleaned != normalized_chosen and normalized_chosen not in normalized_cleaned:
                    explicit_room_hint = cleaned

        room_qs = _approved_room_queryset().filter(
            accommodation=chosen_accommodation,
            status="AVAILABLE",
        )
        if selected_room_id > 0 and not explicit_room_hint:
            selected_room = (
                _approved_room_queryset()
                .select_related("accommodation")
                .filter(
                    room_id=selected_room_id,
                    accommodation=chosen_accommodation,
                    status="AVAILABLE",
                )
                .first()
            )
            if selected_room is not None:
                return {
                    "room": selected_room,
                    "source": "selected_room",
                    "selected_accommodation_id": _to_int(getattr(chosen_accommodation, "accom_id", 0), default=0),
                    "selected_accommodation_name": chosen_name,
                }
        if explicit_room_hint:
            refined_qs = room_qs.filter(room_name__icontains=explicit_room_hint)
            if not refined_qs.exists():
                hint_tokens = [
                    token for token in re.split(r"[\s,\-_/]+", explicit_room_hint)
                    if len(token) >= 3 and token.lower() not in {"room", "hotel", "inn", "accommodation"}
                ]
                for token in hint_tokens:
                    refined_qs = refined_qs.filter(room_name__icontains=token) if refined_qs.exists() else room_qs.filter(room_name__icontains=token)
            if refined_qs.exists():
                room_qs = refined_qs
            else:
                room_choices = [
                    str(v or "").strip()
                    for v in (
                        _approved_room_queryset()
                        .filter(accommodation=chosen_accommodation, status="AVAILABLE")
                        .order_by("price_per_night", "room_name")
                        .values_list("room_name", flat=True)[:5]
                    )
                    if str(v or "").strip()
                ]
                return {
                    "room": None,
                    "source": "explicit_name",
                    "not_found_room_ref": explicit_room_hint,
                    "selected_accommodation_id": _to_int(getattr(chosen_accommodation, "accom_id", 0), default=0),
                    "selected_accommodation_name": chosen_name,
                    "room_choices": room_choices,
                }

        if selected_room_id <= 0 and not explicit_room_hint:
            room_choices = [
                str(v or "").strip()
                for v in (
                    _approved_room_queryset()
                    .filter(accommodation=chosen_accommodation, status="AVAILABLE")
                    .order_by("price_per_night", "room_name")
                    .values_list("room_name", flat=True)[:5]
                )
                if str(v or "").strip()
            ]
            return {
                "room": None,
                "source": "explicit_name",
                "needs_room_selection": True,
                "selected_accommodation_id": _to_int(getattr(chosen_accommodation, "accom_id", 0), default=0),
                "selected_accommodation_name": chosen_name,
                "room_choices": room_choices,
            }

        if guests > 0:
            room_qs = room_qs.filter(person_limit__gte=guests)
        priced_qs = room_qs
        if budget > 0:
            priced_qs = room_qs.filter(price_per_night__lte=budget)
            if priced_qs.exists():
                room_qs = priced_qs
        chosen_room = room_qs.order_by("price_per_night", "room_id").first()
        if chosen_room is None:
            chosen_room = (
                _approved_room_queryset()
                .filter(accommodation=chosen_accommodation, status="AVAILABLE")
                .order_by("price_per_night", "room_id")
                .first()
            )
        return {
            "room": chosen_room,
            "source": "explicit_name",
            "selected_accommodation_id": _to_int(getattr(chosen_accommodation, "accom_id", 0), default=0),
            "selected_accommodation_name": chosen_name,
        }

    selected_accommodation_id = _to_int(params.get("selected_accommodation_id"), default=0)
    selected_accommodation_name = selected_accommodation_name_hint
    selected_accommodation = None
    if selected_accommodation_id > 0:
        selected_accommodation = (
            _approved_accommodation_queryset()
            .filter(accom_id=selected_accommodation_id)
            .order_by("company_name")
            .first()
        )
    if selected_accommodation is None and selected_accommodation_name:
        selected_accommodation, _ = _resolve_accommodation_by_name(selected_accommodation_name)

    if selected_accommodation is not None:
        room_qs = _approved_room_queryset().filter(
            accommodation=selected_accommodation,
            status="AVAILABLE",
        )
        explicit_room_hint = str(params.get("room_name") or params.get("room_reference") or "").strip()
        if not explicit_room_hint:
            message_candidate = str(message or "").strip()
            normalized_candidate = _normalize_chat_text(message_candidate)
            is_likely_detail_turn = bool(
                normalized_candidate
                and not re.search(r"\b(yes|no|confirm|proceed|okay|ok)\b", normalized_candidate)
                and not re.search(r"\b\d+\s*(guest|guests|adult|adults|pax|night|nights)\b", normalized_candidate)
                and not re.search(r"\b(jan|january|feb|february|mar|march|apr|april|may|jun|june|jul|july|aug|august|sep|sept|september|oct|october|nov|november|dec|december)\b", normalized_candidate)
            )
            if is_likely_detail_turn:
                cleaned = re.sub(r"^(room|show details for|details of)\s+", "", message_candidate, flags=re.IGNORECASE).strip(" .,!?:;")
                if cleaned:
                    explicit_room_hint = cleaned

        if explicit_room_hint:
            refined_qs = room_qs.filter(room_name__icontains=explicit_room_hint)
            if not refined_qs.exists():
                hint_tokens = [
                    token for token in re.split(r"[\s,\-_/]+", explicit_room_hint)
                    if len(token) >= 3 and token.lower() not in {"room", "hotel", "inn", "accommodation"}
                ]
                token_qs = room_qs
                for token in hint_tokens:
                    token_qs = token_qs.filter(room_name__icontains=token)
                if token_qs.exists():
                    refined_qs = token_qs
            if refined_qs.exists():
                chosen_room = refined_qs.order_by("price_per_night", "room_name", "room_id").first()
                return {
                    "room": chosen_room,
                    "source": "selected_accommodation",
                    "selected_accommodation_id": _to_int(getattr(selected_accommodation, "accom_id", 0), default=0),
                    "selected_accommodation_name": str(getattr(selected_accommodation, "company_name", "") or "").strip(),
                }
            room_choices = [
                str(v or "").strip()
                for v in room_qs.order_by("price_per_night", "room_name").values_list("room_name", flat=True)[:5]
                if str(v or "").strip()
            ]
            return {
                "room": None,
                "source": "selected_accommodation",
                "not_found_room_ref": explicit_room_hint,
                "selected_accommodation_id": _to_int(getattr(selected_accommodation, "accom_id", 0), default=0),
                "selected_accommodation_name": str(getattr(selected_accommodation, "company_name", "") or "").strip(),
                "room_choices": room_choices,
            }
        if selected_room_id > 0:
            selected_room = (
                _approved_room_queryset()
                .select_related("accommodation")
                .filter(room_id=selected_room_id, accommodation=selected_accommodation)
                .first()
            )
            if selected_room is not None:
                return {
                    "room": selected_room,
                    "source": "selected_room",
                    "selected_accommodation_id": _to_int(getattr(selected_accommodation, "accom_id", 0), default=0),
                    "selected_accommodation_name": str(getattr(selected_accommodation, "company_name", "") or "").strip(),
                }

    raw_room_ref = str(params.get("room_id") or "").strip()
    if raw_room_ref and selected_room_id <= 0:
        # UI/UX rule: keep room IDs internal; ask users to choose by room type/accommodation.
        return {
            "room": None,
            "source": "explicit_room_id",
            "not_found_room_ref": raw_room_ref,
        }

    selection_index = _extract_numeric_option_index(message)
    if selection_index <= 0:
        if re.search(r"\bfirst( one| option)?\b", normalized_message):
            selection_index = 1
        elif re.search(r"\bsecond( one| option)?\b", normalized_message):
            selection_index = 2
        elif re.search(r"\bthird( one| option)?\b", normalized_message):
            selection_index = 3

    use_last_card_context = bool(
        re.search(r"\b(this|that)\s+(hotel|room|accommodation)\b", normalized_message)
        or selection_index > 0
        or _contains_any_phrase(normalized_message, ("that one", "this one", "i want this room", "i want this hotel"))
    )
    if use_last_card_context and cached_rows:
        resolved_room_id = _resolve_accommodation_room_from_selection(cached_rows, selection_index or 1)
        if resolved_room_id > 0:
            chosen_room = (
                _approved_room_queryset()
                .select_related("accommodation")
                .filter(room_id=resolved_room_id)
                .first()
            )
            if chosen_room is not None:
                return {
                    "room": chosen_room,
                    "source": "last_card",
                    "selected_accommodation_name": str(getattr(chosen_room.accommodation, "company_name", "") or "").strip(),
                }

    fallback_room = _find_accommodation_room(params)
    if fallback_room is not None:
        return {
            "room": fallback_room,
            "source": "fallback",
            "selected_accommodation_name": str(getattr(fallback_room.accommodation, "company_name", "") or "").strip(),
        }
    return {"room": None, "source": "none"}


def _clear_invalid_preview_context(chat_state):
    state = dict(chat_state) if isinstance(chat_state, dict) else {}
    state.pop("pending_booking", None)
    params = state.get("params") if isinstance(state.get("params"), dict) else {}
    if isinstance(params, dict):
        cleaned = dict(params)
        for key in (
            "selected_room_id",
            "selected_room_name",
            "selected_accommodation_id",
            "selected_accommodation_name",
            "room_id",
            "accom_name",
            "hotel_name",
            "nightly_rate",
            "selection_source",
        ):
            cleaned.pop(key, None)
        state["params"] = cleaned
    return state


def _build_accommodation_link_actions(room=None, row_meta=None, max_actions=3):
    accom = getattr(room, "accommodation", None) if room is not None else None

    booking_url = str(getattr(accom, "official_booking_url", "") or "").strip()
    contact_url = str(getattr(accom, "official_contact_url", "") or "").strip()
    email_value = str(getattr(accom, "email_address", "") or "").strip()
    phone_value = str(getattr(accom, "phone_number", "") or "").strip()

    if isinstance(row_meta, dict):
        booking_url = booking_url or str(row_meta.get("official_booking_url") or "").strip()
        contact_url = contact_url or str(row_meta.get("official_contact_url") or "").strip()
        email_value = email_value or str(row_meta.get("email_address") or "").strip()
        phone_value = phone_value or str(row_meta.get("phone_number") or "").strip()

    accom_name = ""
    if accom is not None:
        accom_name = str(getattr(accom, "company_name", "") or "").strip()
    if not accom_name and isinstance(row_meta, dict):
        title_text = str(row_meta.get("title") or row_meta.get("company_name") or "").strip()
        if title_text:
            accom_name = title_text.split(" - ")[0].strip()
    short_name = accom_name[:40] if accom_name else "Accommodation"

    actions = []
    seen_urls = set()

    def _push(url, label):
        cleaned = str(url or "").strip()
        if not cleaned:
            return
        dedupe_key = cleaned.lower()
        if dedupe_key in seen_urls:
            return
        seen_urls.add(dedupe_key)
        actions.append({"url": cleaned, "label": str(label or "Visit Official Page").strip()})

    if booking_url:
        if "facebook.com" in booking_url.lower():
            _push(booking_url, "View Facebook")
        else:
            _push(booking_url, "Open Official Page")
    if contact_url:
        if "facebook.com" in contact_url.lower():
            _push(contact_url, "View Facebook")
        else:
            _push(contact_url, "Open Official Page")

    return actions[:max_actions]


def _build_accommodation_official_link(room=None, row_meta=None):
    actions = _build_accommodation_link_actions(room=room, row_meta=row_meta, max_actions=1)
    if actions:
        first = actions[0] if isinstance(actions[0], dict) else {}
        return str(first.get("url") or ""), str(first.get("label") or "Visit Official Page")
    return "", ""


def _is_accommodation_preview_command(message):
    text = _normalize_chat_text(message)
    if not text:
        return False
    if _contains_any_phrase(
        text,
        (
            "create booking preview",
            "booking preview",
            "booking draft",
            "estimated booking summary",
            "preview cost",
            "estimate my stay",
            "calculate my hotel stay",
            "how much if i stay here",
            "how much for 2 nights",
            "how much for may",
            "i want this room",
            "can i book this accommodation",
            "modify booking preview",
        ),
    ):
        return True
    return bool(
        re.search(
            r"\b(book|reserve|proceed with)\b.*\b(hotel|inn|accommodation|room)\b",
            text,
            flags=re.IGNORECASE,
        )
    )


def _is_open_official_page_request(message):
    text = _normalize_chat_text(message)
    if not text:
        return False
    return _contains_any_phrase(
        text,
        (
            "open official page",
            "official page",
            "open booking page",
            "continue to official",
            "continue to official booking page",
            "continue booking outside",
            "book directly with hotel",
            "open facebook page",
            "call accommodation",
            "email accommodation",
            "contact accommodation",
            "call hotel",
            "email hotel",
        ),
    )


def _is_preview_confirmation_message(message):
    text = _normalize_chat_text(message)
    if not text:
        return False
    return text in {"yes", "confirm", "proceed", "okay", "ok", "go ahead"}


def _is_accommodation_how_to_book_request(message):
    text = _normalize_chat_text(message)
    if not text:
        return False
    return bool(
        re.search(
            r"\bhow\s+to\s+book\b.*\b(hotel|inn|accommodation|room)\b",
            text,
            flags=re.IGNORECASE,
        )
    )


def _format_preview_date(date_value):
    raw = str(date_value or "").strip()
    if not raw:
        return ""
    normalized = _normalize_iso_date(raw)
    if not normalized:
        return raw
    try:
        return datetime.strptime(normalized, "%Y-%m-%d").strftime("%b %d, %Y")
    except Exception:
        return normalized


def _build_accommodation_preview_response(*, room=None, params=None):
    params = params if isinstance(params, dict) else {}
    guests = _to_int(params.get("guests"), default=0)
    check_in = str(params.get("check_in") or "").strip()
    check_out = str(params.get("check_out") or "").strip()
    nights = _to_int(params.get("nights"), default=0)
    if check_in and check_out:
        try:
            in_date = datetime.strptime(_normalize_iso_date(check_in), "%Y-%m-%d").date()
            out_date = datetime.strptime(_normalize_iso_date(check_out), "%Y-%m-%d").date()
            delta_nights = (out_date - in_date).days
            if delta_nights > 0:
                nights = delta_nights
        except Exception:
            pass

    missing_fields = []
    if guests <= 0:
        missing_fields.append("guests")
    if nights <= 0:
        missing_fields.append("stay_details")

    if room is None:
        missing_fields.append("room")

    if missing_fields:
        if "room" in missing_fields:
            selected_accom_name = str(
                params.get("selected_accommodation_name")
                or params.get("accom_name")
                or params.get("hotel_name")
                or ""
            ).strip()
            selected_accom_id = _to_int(params.get("selected_accommodation_id"), default=0)
            room_choices = []
            if selected_accom_id > 0 or selected_accom_name:
                room_qs = _approved_room_queryset().filter(status="AVAILABLE", current_availability__gte=1)
                if selected_accom_id > 0:
                    room_qs = room_qs.filter(accommodation_id=selected_accom_id)
                elif selected_accom_name:
                    room_qs = room_qs.filter(accommodation__company_name__icontains=selected_accom_name)
                room_choices = [
                    str(v or "").strip()
                    for v in room_qs.order_by("price_per_night", "room_name").values_list("room_name", flat=True)[:5]
                    if str(v or "").strip()
                ]
            if selected_accom_name:
                question = f"Which room would you like to preview at {selected_accom_name}?"
            else:
                question = "Which room would you like to preview?"
            replies = room_choices[:4] if room_choices else ["show rooms"]
        elif "guests" in missing_fields and "stay_details" in missing_fields:
            question = "I can prepare a booking preview. Please share your check-in/check-out (or nights) and number of guests."
            replies = []
        elif "guests" in missing_fields:
            question = "How many guests should I use for this booking preview?"
            replies = []
        else:
            question = "Please share your check-in/check-out dates or number of nights for the booking preview."
            replies = []
        return {
            "ready": False,
            "question": question,
            "quick_replies": replies,
            "missing_fields": missing_fields,
        }

    rate = _to_decimal(getattr(room, "price_per_night", 0), default=Decimal("0"))
    capacity = _to_int(getattr(room, "person_limit", 0), default=0)
    if capacity > 0 and guests > capacity:
        same_accom_alternatives = [
            str(v or "").strip()
            for v in (
                _approved_room_queryset()
                .filter(
                    accommodation_id=_to_int(getattr(room, "accommodation_id", 0), default=0),
                    status="AVAILABLE",
                    current_availability__gte=1,
                    person_limit__gte=guests,
                )
                .exclude(room_id=getattr(room, "room_id", 0))
                .order_by("price_per_night", "room_name")
                .values_list("room_name", flat=True)[:4]
            )
            if str(v or "").strip()
        ]
        return {
            "ready": False,
            "question": (
                f"This room fits up to {capacity} guest{'s' if capacity != 1 else ''}. "
                + (
                    f"Other rooms in {getattr(getattr(room, 'accommodation', None), 'company_name', 'this accommodation')} may fit {guests} guests."
                    if same_accom_alternatives
                    else "Please reduce guest count or choose another room."
                )
            ),
            "quick_replies": same_accom_alternatives,
            "missing_fields": ["guests"],
        }

    if rate <= 0:
        estimated_total = Decimal("0")
    else:
        estimated_total = (rate * Decimal(max(nights, 1))).quantize(Decimal("1"))

    accommodation_name = str(getattr(getattr(room, "accommodation", None), "company_name", "") or "").strip()
    room_name = str(getattr(room, "room_name", "") or "").strip()
    stay_label = (
        f"{_format_preview_date(check_in)} to {_format_preview_date(check_out)}"
        if check_in and check_out
        else f"{max(nights, 1)} night(s)"
    )
    summary_lines = [
        "Here is your estimated accommodation booking preview:",
        "",
        f"Accommodation: {accommodation_name}",
        f"Room: {room_name}",
        f"Guests: {guests}",
        f"Stay: {stay_label}",
        f"Nights: {max(nights, 1)}",
        f"Rate: PHP {int(rate):,}/night" if rate > 0 else "Rate: To be confirmed",
        f"Estimated Total: PHP {int(estimated_total):,}" if estimated_total > 0 else "Estimated Total: To be confirmed",
        "",
        (
            "This is only a booking preview. Your accommodation is not reserved yet. "
            "To complete the actual booking, continue through the accommodation's official page or contact channels."
        ),
    ]

    link_actions = _build_accommodation_link_actions(room=room, max_actions=4)
    return {
        "ready": True,
        "text": "\n".join(summary_lines),
        "quick_replies": [],
        "link_actions": link_actions,
        "billing_link": str(link_actions[0].get("url") or "") if link_actions else "",
        "billing_link_label": str(link_actions[0].get("label") or "Continue to Official Booking Page") if link_actions else "",
    }


def _extract_params_with_confidence(message):
    text = (message or "").strip().lower()
    params = {}
    confidence = 1.0
    needs_clarification = False
    clarification_question = ""
    clarification_field = ""
    clarification_options = []
    numeric_only = re.fullmatch(
        r"\s*([0-9][0-9,]*(?:\.[0-9]+)?k?)\s*",
        text,
        flags=re.IGNORECASE,
    )
    known_location_map = {
        "terminal": "terminal area",
        "terminal area": "terminal area",
        "public terminal": "tinago",
        "bus terminal": "tinago",
        "trike terminal": "tinago",
        "tricycle terminal": "tinago",
        "pedicab terminal": "tinago",
        "motorcab terminal": "tinago",
        "tinago": "tinago",
        "boyco": "boyco",
        "ubos": "ubos",
        "poblacion": "poblacion",
        "bayawan": "bayawan",
        "bayawan city": "bayawan city",
        "bayawan city proper": "poblacion",
        "city proper": "poblacion",
        "villareal": "villareal",
        "villarreal": "villareal",
        "suba": "suba",
        "suba barangay": "suba",
        "barangay suba": "suba",
        "brgy suba": "suba",
        "poblacion barangay": "poblacion",
        "barangay poblacion": "poblacion",
        "brgy poblacion": "poblacion",
    }
    map_location_anchor = ""

    def _normalize_location_alias_text(raw_location):
        candidate = " ".join(str(raw_location or "").split()).strip().lower()
        candidate = re.sub(r"[^a-z0-9\s]", " ", candidate)
        candidate = " ".join(candidate.split())
        candidate = re.sub(r"^the\s+", "", candidate).strip()
        if candidate.startswith("barangay "):
            candidate = candidate.replace("barangay ", "", 1).strip()
        elif candidate.startswith("brgy "):
            candidate = candidate.replace("brgy ", "", 1).strip()
        if candidate.endswith(" barangay"):
            candidate = candidate[: -len(" barangay")].strip()
        elif candidate.endswith(" brgy"):
            candidate = candidate[: -len(" brgy")].strip()
        if candidate in ("city proper", "bayawan city proper"):
            return "poblacion"
        return candidate

    def _resolve_location_value(raw_location):
        candidate = _normalize_location_alias_text(raw_location)
        if not candidate:
            return ""
        if candidate in known_location_map:
            return known_location_map.get(candidate, candidate)
        # Prefer specific location aliases embedded in longer phrases
        # (e.g., "suba bayawan", "mabini street in suba").
        embedded_aliases = [
            alias
            for alias in known_location_map.keys()
            if alias
            and alias in candidate
            and alias not in {"bayawan", "bayawan city", "city proper", "terminal", "terminal area"}
        ]
        if embedded_aliases:
            embedded_aliases.sort(key=len, reverse=True)
            return known_location_map.get(embedded_aliases[0], embedded_aliases[0])
        alias_place_match = _match_map_reference_place(candidate)
        if alias_place_match:
            return _map_place_to_location_hint(alias_place_match.get("name"))
        if "trike terminal" in candidate or "tricycle terminal" in candidate or "tricyle terminal" in candidate:
            return "tinago"
        if "pedicab terminal" in candidate or "motorcab terminal" in candidate:
            return "tinago"
        if "bus terminal" in candidate or "public terminal" in candidate or "city terminal" in candidate:
            return "tinago"
        if "terminal" in candidate:
            return "terminal area"
        if "public market" in candidate or candidate == "market":
            return "boyco"
        if "plaza" in candidate:
            return "poblacion"
        db_locations = _known_accommodation_locations()
        close = get_close_matches(candidate, list(known_location_map.keys()), n=1, cutoff=0.82)
        if close:
            return known_location_map.get(close[0], close[0])
        close_db = get_close_matches(candidate, db_locations, n=1, cutoff=0.78)
        if close_db:
            return close_db[0]
        return candidate

    def _set_clarification(field, question, penalty=0.3):
        nonlocal confidence, needs_clarification, clarification_question, clarification_field
        needs_clarification = True
        confidence = max(0.0, confidence - float(penalty))
        if not clarification_question:
            clarification_question = question
            clarification_field = field

    def _set_location_ambiguity_clarification(raw_value, place_matches):
        nonlocal clarification_options
        candidates = []
        for row in place_matches or []:
            name = " ".join(str((row or {}).get("name") or "").split()).strip()
            if name and name not in candidates:
                candidates.append(name)
        if not candidates:
            return
        pick_list = ", ".join(candidates[:4])
        _set_clarification(
            "location",
            (
                f"I found multiple places matching '{raw_value}'. "
                f"Which one do you mean: {pick_list}?"
            ),
            penalty=0.2,
        )
        clarification_options = [
            {"label": name[:50], "value": name[:80]}
            for name in candidates[:4]
        ]

    def _set_terminal_clarification(raw_value):
        nonlocal clarification_options
        _set_clarification(
            "location",
            (
                f"I found multiple terminal matches for '{raw_value}'. "
                "Do you mean Bayawan City Public Terminal (bus/public) or the Trike Terminal?"
            ),
            penalty=0.2,
        )
        clarification_options = [
            {"label": "Bus/Public Terminal", "value": "near Bayawan City Public Terminal"},
            {"label": "Trike Terminal", "value": "near trike terminal"},
        ]

    def _parse_compact_number(raw_value):
        value = str(raw_value or "").strip().lower().replace(",", "")
        if not value:
            return None
        multiplier = 1
        if value.endswith("k"):
            multiplier = 1000
            value = value[:-1].strip()
        try:
            parsed = Decimal(value)
        except Exception:
            return None
        if parsed < 0:
            return None
        return int(parsed * multiplier)

    def _infer_budget_range_from_keywords(raw_text):
        """
        Map qualitative budget words to thesis-safe numeric ranges.
        Returns dict keys compatible with recommender filters:
        - cheap/affordable => budget <= 1500
        - mid/moderate => 1500 <= budget <= 2500
        - expensive => budget >= 2501
        """
        normalized = str(raw_text or "").strip().lower()
        if not normalized:
            return {}

        cheap_markers = (
            "cheap",
            "affordable",
            "budget-friendly",
            "budget friendly",
            "low budget",
            "economical",
            "barato",
            "mas mura",
            "mura",
        )
        mid_markers = (
            "mid",
            "mid range",
            "mid-range",
            "moderate",
            "moderately priced",
            "sakto lang",
            "katamtaman",
        )
        expensive_markers = (
            "expensive",
            "high-end",
            "high end",
            "premium",
            "luxury",
            "mahal",
        )

        if any(token in normalized for token in cheap_markers):
            return {"budget": 1500}
        if any(token in normalized for token in mid_markers):
            return {"budget_min": 1500, "budget": 2500}
        if any(token in normalized for token in expensive_markers):
            return {"budget_min": 2501}
        return {}

    # Extract schedule ID like Sched00001.
    sched_match = re.search(r"(sched\d+)", text, flags=re.IGNORECASE)
    if sched_match:
        params["sched_id"] = sched_match.group(1)

    # Extract guest count from "<n> guest(s)/people/person/pax/bisita/katao".
    guest_match = re.search(r"(\d+)\s*(guest|guests|people|person|pax|bisita|katao|ka\s*bisita)", text)
    if guest_match:
        params["guests"] = int(guest_match.group(1))
        params["group_size"] = int(guest_match.group(1))

    if re.search(r"\bsolo\b|\balone\b", text):
        params["party_type"] = "solo"
        params.setdefault("group_size", 1)
    elif re.search(r"\bcouple\b|\bpartner\b|\bfor two\b", text):
        params["party_type"] = "couple"
        params.setdefault("group_size", 2)
    elif re.search(r"\bfamily\b|\bkids\b|\bchildren\b", text):
        params["party_type"] = "family"
    elif re.search(r"\bgroup\b|\bteam\b|\bfriends\b", text):
        params["party_type"] = "group"

    # Extract requested recommendation list size (e.g., "give 10 inns available", "top 5 hotels").
    list_size_match = re.search(
        r"\b(?:give|show|list|recommend|top)\s+(\d{1,2})\b.*\b(?:hotel|hotels|inn|inns|accommodation|accommodations)\b",
        text,
        flags=re.IGNORECASE,
    )
    if list_size_match:
        params["result_limit"] = max(1, min(int(list_size_match.group(1)), 10))

    # Extract budget from forms like:
    # - budget 1500
    # - budget 1,500
    # - budget 1.5k
    # - under/below/less than 2000
    budget_match = re.search(
        r"(?:budget(?:\s+(?:around|about|approx(?:imately)?))?|under|below|less than|max|up to|not more than|no more than)\s*[:\-]?\s*(?:php|peso|pesos)?\s*([0-9][0-9,]*(?:\.[0-9]+)?k?)",
        text,
        flags=re.IGNORECASE,
    )
    around_budget_match = re.search(
        r"\b(?:around|about|approx(?:imately)?)\s*(?:php|peso|pesos)?\s*([0-9][0-9,]*(?:\.[0-9]+)?k?)\b",
        text,
        flags=re.IGNORECASE,
    )
    budget_range_match = re.search(
        r"\b(?:around|about|between)?\s*([0-9][0-9,]*(?:\.[0-9]+)?k?)\s*(?:to|-)\s*([0-9][0-9,]*(?:\.[0-9]+)?k?)\b",
        text,
        flags=re.IGNORECASE,
    )
    budget_tail_match = re.search(
        r"([0-9][0-9,]*(?:\.[0-9]+)?k?)\s*(?:php|peso|pesos)?\s*(?:ang\s+)?budget\b",
        text,
        flags=re.IGNORECASE,
    )
    if budget_range_match:
        low_value = _parse_compact_number(budget_range_match.group(1))
        high_value = _parse_compact_number(budget_range_match.group(2))
        if low_value is not None and high_value is not None and high_value > 0:
            lower = min(low_value, high_value)
            upper = max(low_value, high_value)
            params["budget_min"] = lower
            params["budget"] = upper
    elif budget_match:
        budget_value = _parse_compact_number(budget_match.group(1))
        if budget_value is not None:
            if "total" in text and not re.search(r"(per\s*night|nightly|/night)", text):
                _set_clarification(
                    "budget",
                    "Is that amount your budget per night in PHP?",
                    penalty=0.35,
                )
            else:
                params["budget"] = budget_value
    elif budget_tail_match:
        budget_value = _parse_compact_number(budget_tail_match.group(1))
        if budget_value is not None:
            params["budget"] = budget_value
    elif around_budget_match:
        budget_value = _parse_compact_number(around_budget_match.group(1))
        if budget_value is not None:
            params["budget"] = budget_value

    total_budget_match = re.search(
        r"\b(?:what can i do with|i have|with|around|about|total budget(?: is| of)?|budget for (?:the )?(?:trip|stay))\s*"
        r"(?:php|peso|pesos)?\s*([0-9][0-9,]*(?:\.[0-9]+)?k?)\b",
        text,
        flags=re.IGNORECASE,
    )
    if total_budget_match:
        total_budget_value = _parse_compact_number(total_budget_match.group(1))
        if total_budget_value is not None:
            params["total_budget"] = total_budget_value

    if (
        _is_stay_planning_request(text)
        and _to_int(params.get("total_budget"), default=0) <= 0
        and _to_int(params.get("budget"), default=0) > 0
        and not re.search(r"(per\s*night|nightly|/night)", text)
    ):
        params["total_budget"] = _to_int(params.get("budget"), default=0)
    # Numeric-only fallback is handled contextually in the main chat flow
    # to avoid misreading guest-count replies as budget.

    # Budget-clearing commands ("remove budget", "without budget", etc.)
    if re.search(
        r"\b(remove|clear|reset|ignore|without|no)\s+(the\s+)?budget\b",
        text,
        flags=re.IGNORECASE,
    ) or re.search(
        r"\bwithout\s+minding\s+the\s+budget\b",
        text,
        flags=re.IGNORECASE,
    ):
        params["clear_budget"] = True
        params["budget"] = 0
        params.pop("budget_min", None)

    # Keyword-only budget inference (only when explicit numeric budget is not present).
    if (
        _to_int(params.get("budget"), default=0) <= 0
        and _to_int(params.get("budget_min"), default=0) <= 0
        and not _to_bool(params.get("clear_budget"), default=False)
    ):
        inferred = _infer_budget_range_from_keywords(text)
        if inferred:
            if _to_int(inferred.get("budget_min"), default=0) > 0:
                params["budget_min"] = _to_int(inferred.get("budget_min"), default=0)
            if _to_int(inferred.get("budget"), default=0) > 0:
                params["budget"] = _to_int(inferred.get("budget"), default=0)

    # Keep backward-compatible behavior for plain numeric messages
    # (e.g., "1500" => budget=1500).
    if (
        numeric_only
        and _to_int(params.get("budget"), default=0) <= 0
        and not _to_bool(params.get("clear_budget"), default=False)
    ):
        compact = _parse_compact_number(numeric_only.group(1))
        if compact is not None and compact > 0:
            params["budget"] = compact

    # Extract duration from "<n> day(s)".
    duration_match = re.search(r"(\d+)\s*day", text)
    if duration_match:
        params["duration_days"] = int(duration_match.group(1))

    # Extract nights from "<n> night(s)".
    nights_match = re.search(r"(\d+)\s*night", text)
    if nights_match:
        params["nights"] = int(nights_match.group(1))

    if re.search(r"\b(no accommodation|without accommodation|day trip only|no hotel needed)\b", text):
        params["accommodation_needed"] = False
    elif re.search(r"\b(need accommodation|with accommodation|need hotel|need inn|need a room)\b", text):
        params["accommodation_needed"] = True

    if re.search(r"\b(tour only|tours only)\b", text):
        params["activity_mix"] = "tour"
    elif re.search(r"\b(spots only|attractions only|tourist spots only)\b", text):
        params["activity_mix"] = "spot"
    elif re.search(r"\b(mixed|combination|both tours and spots|hotel and places)\b", text):
        params["activity_mix"] = "mixed"

    if re.search(r"\b(adventure|adventurous|hike|trail|outdoor)\b", text):
        params["experience_style"] = "adventure"
    elif re.search(r"\b(relax|relaxing|chill|peaceful|calm)\b", text):
        params["experience_style"] = "relaxing"
    elif re.search(r"\b(culture|cultural|heritage|history|museum)\b", text):
        params["experience_style"] = "culture"
    elif re.search(r"\b(family-friendly|family|kids)\b", text):
        params["experience_style"] = "family"

    if re.search(r"\b(cheap|cheaper|affordable|budget|tipid|barato|lower budget|less expensive)\b", text):
        params["spending_style"] = "budget"
    elif re.search(r"\b(premium|luxury|high-end|high end)\b", text):
        params["spending_style"] = "premium"
    elif re.search(r"\b(mid-range|mid range|moderate)\b", text):
        params["spending_style"] = "mid"

    # Extract location from phrases like "in bayawan", "near terminal", "around poblacion".
    # Stop before common trailing constraint phrases so we don't swallow guests/budget text.
    loc_match = re.search(
        r"\b(in|near|around|sa|at)\s+([a-z\s]+?)(?=\s+(?:for|under|below|budget|with|from)\b|$)",
        text,
    )
    if loc_match:
        loc_prefix = str(loc_match.group(1) or "").strip()
        raw_location = " ".join(loc_match.group(2).split()).strip()
        if raw_location:
            if loc_prefix in ("near", "around", "at") and _is_generic_terminal_reference(raw_location):
                _set_terminal_clarification(raw_location)
                raw_location = ""
        if raw_location:
            normalized_location = _resolve_location_value(raw_location)
            if normalized_location in known_location_map.values() or raw_location.lower() == normalized_location.lower():
                params["location"] = normalized_location
                # Prevent stale anchor carry-over when location changed without a map anchor.
                params["clear_location_anchor"] = True
                if loc_prefix in ("near", "around", "at"):
                    place_matches = _match_map_reference_places(raw_location, limit=4)
                    if not place_matches:
                        place_matches = _match_map_reference_places(normalized_location, limit=4)
                    if (not place_matches) and normalized_location == "terminal area":
                        place_matches = [{"name": "Bayawan City Public Terminal"}]
                    if len(place_matches) > 1:
                        _set_location_ambiguity_clarification(raw_location, place_matches)
                    elif place_matches:
                        place_match = place_matches[0]
                        map_location_anchor = str(place_match.get("name") or "").strip()
                        params["location_anchor"] = map_location_anchor
                        params["location_anchor_source"] = "map_reference"
                        params["location"] = _map_place_to_location_hint(map_location_anchor)
                        params["broaden_location"] = True
                        params["location_scope_note"] = (
                            "Here are accommodations in/near "
                            f"{params.get('location')} based on available records (matched by city-proper map anchor)."
                        )
                        params.pop("clear_location_anchor", None)
            elif loc_prefix in ("near", "around"):
                place_matches = _match_map_reference_places(raw_location, limit=4)
                if len(place_matches) > 1:
                    _set_location_ambiguity_clarification(raw_location, place_matches)
                elif place_matches:
                    place_match = place_matches[0]
                    map_location_anchor = str(place_match.get("name") or "").strip()
                    params["location_anchor"] = map_location_anchor
                    params["location_anchor_source"] = "map_reference"
                    params["location"] = _map_place_to_location_hint(map_location_anchor)
                    params["broaden_location"] = True
                    params["location_scope_note"] = (
                        "Here are accommodations in/near "
                        f"{params.get('location')} based on available records (matched by city-proper map anchor)."
                    )
                else:
                    _set_clarification(
                        "location",
                        "I couldn't map that place yet. Please specify a barangay/area in Bayawan, or use a known city-proper landmark.",
                        penalty=0.35,
                    )
            else:
                params["location"] = raw_location
            if params.get("location") and not params.get("location_scope_note"):
                params["location_scope_note"] = (
                    f"Here are accommodations in/near {params.get('location')} based on available records."
                )

    # Capture common location mentions even without "in/near/around".
    if "location" not in params:
        for known_location in known_location_map:
            if known_location in text:
                params["location"] = known_location_map.get(known_location, known_location)
                params["clear_location_anchor"] = True
                break

    # Allow map-place anchoring even when user did not use explicit "in/near/around".
    if (not params.get("location_anchor")) and (
        any(token in text for token in ("near", "nearest", "close to", "walking distance"))
        or any(token in text for token in ("terminal", "trike", "pedicab", "market", "plaza", "mall", "church", "hayahay", "eskina", "puregold"))
    ):
        if _is_generic_terminal_reference(text):
            _set_terminal_clarification(text)
        place_matches = _match_map_reference_places(text, limit=4)
        if len(place_matches) > 1 and not needs_clarification:
            _set_location_ambiguity_clarification(text, place_matches)
        elif place_matches and not needs_clarification:
            place_match = place_matches[0]
            map_location_anchor = str(place_match.get("name") or "").strip()
            params["location_anchor"] = map_location_anchor
            params["location_anchor_source"] = "map_reference"
            params["location"] = _map_place_to_location_hint(map_location_anchor)
            params.setdefault("broaden_location", True)
            params.setdefault(
                "location_scope_note",
                "Matched by city-proper map anchor and accommodation records.",
            )

    # Extract room reference:
    # - valid numeric room id: "room 12"
    # - invalid/tampered token: "room xyz" (captured for safe validation messaging)
    room_token_match = re.search(r"\broom\s+([a-z0-9\-]+)\b", text, flags=re.IGNORECASE)
    if room_token_match:
        room_token = str(room_token_match.group(1) or "").strip()
        if room_token:
            params["room_id"] = room_token

    # Capture room-type intent without forcing room-card display immediately.
    requested_room_tokens = []
    for token in (
        "standard", "deluxe", "family", "double", "twin", "suite",
        "queen", "king", "villa", "matrimonial", "single",
    ):
        if re.search(rf"\b{re.escape(token)}\b", text, flags=re.IGNORECASE):
            requested_room_tokens.append(token)
    if requested_room_tokens:
        params["room_type"] = " ".join(requested_room_tokens[:2])

    # Extract check-in/check-out dates:
    # - YYYY-M-D / YYYY-MM-DD
    # - Month DD, YYYY (e.g., March 27, 2026)
    # - Mon DD, YYYY (e.g., Mar 27, 2026)
    month_regex = (
        r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
        r"jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
    )
    raw_date_matches = re.findall(
        rf"(\d{{4}}-\d{{1,2}}-\d{{1,2}}|{month_regex}\s+\d{{1,2}}(?:,\s*\d{{4}}|\s+\d{{4}})?)",
        text,
        flags=re.IGNORECASE,
    )
    normalized_dates = [_normalize_iso_date(v) for v in raw_date_matches]
    date_matches = [v for v in normalized_dates if v]
    if len(date_matches) >= 2:
        params["check_in"] = date_matches[0]
        params["check_out"] = date_matches[1]
    elif len(date_matches) == 1:
        params["check_in"] = date_matches[0]

    # Parse conversational month-day ranges without repeating month/year:
    # - "May 10 to 12"
    # - "May 10 to May 12"
    # - "May 10 - May 12"
    short_month_range = re.search(
        r"\b"
        r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|"
        r"sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\s+(\d{1,2})"
        r"\s*(?:-|to)\s*"
        r"(?:(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|"
        r"sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\s+)?(\d{1,2})"
        r"(?:,\s*(20\d{2}))?\b",
        text,
        flags=re.IGNORECASE,
    )
    if short_month_range:
        month_a = str(short_month_range.group(1) or "").strip()
        day_a = str(short_month_range.group(2) or "").strip()
        month_b = str(short_month_range.group(3) or "").strip() or month_a
        day_b = str(short_month_range.group(4) or "").strip()
        year_token = str(short_month_range.group(5) or "").strip()
        if not year_token:
            year_token = str(timezone.localdate().year)
        start_raw = f"{month_a} {day_a}, {year_token}"
        end_raw = f"{month_b} {day_b}, {year_token}"
        start_iso = _normalize_iso_date(start_raw)
        end_iso = _normalize_iso_date(end_raw)
        if start_iso and end_iso:
            params["check_in"] = start_iso
            params["check_out"] = end_iso

    if (
        params.get("check_in")
        and not params.get("check_out")
        and _to_int(params.get("nights"), default=0) <= 0
        and len(date_matches) == 1
    ):
        _set_clarification(
            "date_range",
            "Please provide check-out date or number of nights for your stay preview.",
            penalty=0.25,
        )

    # Detect month-day ranges without year and ask for clarification instead of guessing.
    month_day_range_no_year = re.search(
        r"\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|"
        r"aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
        r"\s+\d{1,2}\s*(?:-|to)\s*\d{1,2}\b",
        text,
        flags=re.IGNORECASE,
    )
    if month_day_range_no_year and not re.search(r"\b\d{4}\b", text):
        _set_clarification(
            "date_range",
            "Please include the year for your check-in/check-out dates (e.g., 2026-03-27 or March 27, 2026).",
            penalty=0.4,
        )

    # Relative date resolution for conversational prompts:
    # today, tomorrow, this weekend, next week, and "for N nights starting tomorrow".
    if not params.get("check_in") and not params.get("check_out"):
        rel_check_in, rel_check_out, _ = _resolve_relative_stay_window(text)
        if rel_check_in and rel_check_out:
            params["check_in"] = rel_check_in.isoformat()
            params["check_out"] = rel_check_out.isoformat()

    # Extract accommodation/hotel name from "at <name>" or "hotel <name>".
    at_match = re.search(r"\bat\s+([a-z0-9\s\-&]+)", text)
    if at_match and "check-in" not in at_match.group(1):
        at_candidate = " ".join(str(at_match.group(1) or "").split()).strip()
        if at_candidate and not re.search(
            r"\b(near|around|in|for|budget|guest|guests|from|to)\b",
            at_candidate,
            flags=re.IGNORECASE,
        ):
            params.setdefault("accom_name", at_candidate)

    hotel_name_match = re.search(r"\b(?:hotel|inn)\s+([a-z0-9\s\-&]+)", text)
    if hotel_name_match:
        name_candidate = " ".join(str(hotel_name_match.group(1) or "").split()).strip()
        if name_candidate and not re.search(
            r"\b(near|around|in|for|budget|guest|guests|from|to)\b",
            name_candidate,
            flags=re.IGNORECASE,
        ):
            params.setdefault("accom_name", name_candidate)

    preview_name_match = re.search(
        r"\b(?:create|make|prepare)?\s*(?:a\s+)?(?:booking\s+preview|preview\s+cost|booking\s+draft)\s+for\s+([a-z0-9][a-z0-9\s\-&']{2,80})\b",
        text,
        flags=re.IGNORECASE,
    )
    if not preview_name_match:
        preview_name_match = re.search(
            r"\b(?:estimate my stay|how much if i stay at)\s+([a-z0-9][a-z0-9\s\-&']{2,80})\b",
            text,
            flags=re.IGNORECASE,
        )
    if preview_name_match:
        preview_candidate = " ".join(str(preview_name_match.group(1) or "").split()).strip(" .,!?")
        if preview_candidate and preview_candidate not in {"this hotel", "this room", "that one", "first one"}:
            params["accom_name"] = preview_candidate

    # Respect explicit accommodation type words in the user's prompt.
    if "inn" in text and "hotel" not in text:
        params.setdefault("company_type", "inn")
    elif "hotel" in text and "inn" not in text:
        params.setdefault("company_type", "hotel")
    elif "hotel" in text and "inn" in text:
        params.setdefault("company_type", "either")

    # Tour-type/preference extraction for follow-up prompts such as:
    # "i prefer nature tours", "culture tour", "falls package".
    tour_preference_aliases = {
        "sea": ("sea", "beach", "coastal", "ocean", "island", "shore", "seaside"),
        "nature": ("nature", "natural", "falls", "waterfall", "eco", "scenic"),
        "culture": ("culture", "cultural", "heritage", "food", "culinary"),
        "city": ("city", "urban", "downtown", "proper", "plaza"),
        "highlights": ("highlight", "highlights", "must-see"),
        "river": ("river", "riverside"),
        "adventure": ("adventure", "hike", "trek", "outdoor"),
        "family": ("family", "kid", "kids", "child-friendly"),
    }
    matched_tour_type = ""
    for canonical_tour_type, markers in tour_preference_aliases.items():
        if any(re.search(rf"\b{re.escape(marker)}\b", text) for marker in markers):
            matched_tour_type = canonical_tour_type
            break
    prefer_phrase_match = re.search(
        r"\b(?:i\s+prefer|prefer|i\s+want|want|how\s+about|what\s+about)\s+([a-z0-9\s\-]+)",
        text,
        flags=re.IGNORECASE,
    )
    if prefer_phrase_match:
        preference_phrase = str(prefer_phrase_match.group(1) or "").strip().lower()
        # Trim generic suffix words while keeping meaningful terms like "city highlights".
        preference_phrase = re.sub(
            r"\b(tour|tours|tour package|tour packages|package|packages|trip|trips)\b",
            "",
            preference_phrase,
            flags=re.IGNORECASE,
        ).strip()
        if preference_phrase:
            params["preference_text"] = preference_phrase
    if matched_tour_type:
        params["tour_type"] = matched_tour_type
        params["preference"] = matched_tour_type
    else:
        # Backward-compatible fallback for short legacy keywords.
        for keyword in ["river", "mountain", "sea", "sunset", "forest"]:
            if keyword in text:
                params["preference"] = keyword
                break
    timeframe_hint = _extract_tour_timeframe_hint(text)
    if timeframe_hint:
        params["tour_timeframe_hint"] = timeframe_hint

    # Tourism information query hint extraction.
    tourism_query_matchers = [
        r"(?:tell me about|information about|info about|details about)\s+([a-z0-9\s\-'&]+)",
        r"(?:operating hours for|opening hours for|contact for)\s+([a-z0-9\s\-'&]+)",
        r"(?:where is)\s+([a-z0-9\s\-'&]+)",
        r"(?:how far is|how long to|get to|go to)\s+([a-z0-9\s\-'&]+?)(?:\s+from\b|$)",
    ]
    for matcher in tourism_query_matchers:
        m = re.search(matcher, text, flags=re.IGNORECASE)
        if not m:
            continue
        candidate = " ".join(str(m.group(1) or "").split()).strip(" .,!?:;")
        if candidate:
            params["tourism_query"] = candidate
            break

    origin_match = re.search(r"\bfrom\s+([a-z0-9\s\-'&]+)", text, flags=re.IGNORECASE)
    if origin_match:
        origin_value = " ".join(str(origin_match.group(1) or "").split()).strip(" .,!?:;")
        if origin_value:
            params["origin_hint"] = origin_value

    # Amenity keyword extraction for accommodation refinement prompts like "with pool".
    known_amenities = (
        "pool",
        "wifi",
        "parking",
        "breakfast",
        "aircon",
        "ac",
        "kitchen",
        "balcony",
        "gym",
    )
    matched_amenities = [token for token in known_amenities if re.search(rf"\b{re.escape(token)}\b", text)]
    if matched_amenities:
        # Keep as list; recommender normalizes both list and string inputs.
        params["amenities"] = matched_amenities

    preference_profile = _extract_preference_profile(text)
    preference_tags = preference_profile.get("preference_tags") or []
    prefer_low_price = bool(preference_profile.get("prefer_low_price"))

    if (
        not preference_tags
        and not prefer_low_price
        and any(token in text for token in ("hotel", "inn", "accommodation", "stay", "place"))
    ):
        gemini_profile = _extract_preference_profile_with_gemini(text)
        gemini_tags = gemini_profile.get("preference_tags") if isinstance(gemini_profile.get("preference_tags"), list) else []
        merged_tags = []
        for tag in (preference_tags + gemini_tags):
            tag_val = str(tag or "").strip().lower()
            if tag_val and tag_val not in merged_tags:
                merged_tags.append(tag_val)
        preference_tags = merged_tags
        prefer_low_price = prefer_low_price or bool(gemini_profile.get("prefer_low_price"))

    if preference_tags:
        params["preference_tags"] = preference_tags
    if prefer_low_price:
        params["prefer_low_price"] = True

    # Explicit scope broadening controls (strict by default).
    if re.search(r"\b(broaden|expand|widen)\s+(the\s+)?location\b", text):
        params["broaden_location"] = True
    if re.search(r"\b(include|allow|show)\s+(both\s+)?(hotel\s+and\s+inn|inn\s+and\s+hotel)\b", text):
        params["broaden_company_type"] = True
    if re.search(r"\b(broaden|expand)\s+(filters|search|scope)\b", text):
        params["broaden_location"] = True
        params["broaden_company_type"] = True

    # Keep specific barangay/street intent when generic "bayawan" is also present.
    normalized_loc = str(params.get("location") or "").strip().lower()
    if normalized_loc in {"bayawan", "bayawan city", "terminal area", ""}:
        specificity_hints = (
            ("peping gamo", "tinago"),
            ("j p rizal", "suba"),
            ("jp rizal", "suba"),
            ("mabini", "suba"),
            ("suba", "suba"),
            ("tinago", "tinago"),
            ("poblacion", "poblacion"),
            ("villareal", "villareal"),
            ("villarreal", "villareal"),
        )
        for token, target in specificity_hints:
            if token in text:
                params["location"] = target
                break

    return {
        "params": params,
        "confidence": round(confidence, 3),
        "needs_clarification": needs_clarification,
        "clarification_question": clarification_question,
        "clarification_field": clarification_field,
        "clarification_options": clarification_options,
    }


def _extract_params_from_message(message):
    parsed = _extract_params_with_confidence(message)
    return parsed.get("params", {})


def _extract_preference_profile(text):
    raw = str(text or "").strip().lower()
    if not raw:
        return {"preference_tags": [], "prefer_low_price": False}

    preference_map = {
        "quiet": [
            "quiet", "peaceful", "calm", "relaxing", "serene", "less noise", "not noisy",
            "chill", "tahimik", "walang ingay", "hindi maingay", "mingaw",
        ],
        "nature": [
            "nature", "green", "garden", "fresh air", "good environment", "cool place",
            "cool environment", "scenic", "view", "mountain", "river", "presko", "luntian",
        ],
        "family": [
            "family", "family-friendly", "kids", "children", "group", "spacious",
            "pang pamilya", "for family",
        ],
        "clean": [
            "clean", "sanitary", "hygienic", "well-maintained", "tidy",
            "malinis", "limpyo",
        ],
        "accessible": [
            "near terminal", "accessible", "easy transport", "commute", "near transport",
            "near downtown", "city proper", "walking distance", "near highway",
            "malapit", "duol",
        ],
        "romantic": [
            "romantic", "honeymoon", "couple", "date place", "for couples",
        ],
    }
    low_price_markers = (
        "cheap", "affordable", "budget-friendly", "budget friendly", "low price", "economical",
        "value for money", "sulit", "barato", "murag barato",
    )

    matched_tags = []
    for tag, markers in preference_map.items():
        if any(marker in raw for marker in markers):
            matched_tags.append(tag)

    return {
        "preference_tags": matched_tags,
        "prefer_low_price": any(marker in raw for marker in low_price_markers),
    }


def _extract_json_payload(raw_text):
    text = str(raw_text or "").strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except Exception:
        pass
    match = re.search(r"\{[\s\S]*\}", text)
    if not match:
        return {}
    try:
        return json.loads(match.group(0))
    except Exception:
        return {}


def _gemini_preference_parsing_enabled():
    raw = str(os.getenv("CHATBOT_GEMINI_PREFERENCE_PARSING_ENABLED", "1") or "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def _extract_preference_profile_with_gemini(text):
    raw = str(text or "").strip()
    if not raw:
        return {"preference_tags": [], "prefer_low_price": False}
    if not _gemini_preference_parsing_enabled():
        return {"preference_tags": [], "prefer_low_price": False}
    if genai is None:
        return {"preference_tags": [], "prefer_low_price": False}
    api_key = str(os.getenv("GEMINI_API_KEY", "") or "").strip()
    if not api_key:
        return {"preference_tags": [], "prefer_low_price": False}

    model = str(os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite") or "").strip() or "gemini-2.5-flash-lite"
    prompt = (
        "Extract accommodation preference intent from user text.\n"
        "Return JSON only with keys:\n"
        "- preference_tags: array from this closed list only: "
        "[quiet, nature, family, clean, accessible, romantic]\n"
        "- prefer_low_price: boolean\n"
        "Rules:\n"
        "- Do not include other keys.\n"
        "- If uncertain, return empty array and false.\n"
        f"User text:\n{raw}"
    )
    try:
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model=model,
            contents=prompt,
        )
        payload = _extract_json_payload(getattr(response, "text", ""))
        tags = payload.get("preference_tags") if isinstance(payload.get("preference_tags"), list) else []
        normalized_tags = []
        allowed = {"quiet", "nature", "family", "clean", "accessible", "romantic"}
        for tag in tags:
            tag_str = str(tag or "").strip().lower()
            if tag_str in allowed and tag_str not in normalized_tags:
                normalized_tags.append(tag_str)
        return {
            "preference_tags": normalized_tags,
            "prefer_low_price": bool(payload.get("prefer_low_price")),
        }
    except Exception:
        return {"preference_tags": [], "prefer_low_price": False}


def _intent_from_message(message):
    text = (message or "").lower()
    if _is_stay_planning_request(text):
        return "plan_bayawan_stay"
    if _is_travel_guidance_request(text):
        return "travel_guidance"
    if _is_reporting_summary_request(text):
        return "reporting_summary"
    if _is_dining_query(text):
        return "get_tourism_information"
    recommendation_keywords = [
        "recommend", "suggest", "show", "find", "looking for", "search",
        "best place", "where should i go", "saan magandang", "saan maganda", "gumala",
        "affordable", "cheap", "budget-friendly", "relaxation", "family trip", "solo traveler",
        "upcoming tours", "upcoming tour", "tours today", "events this week", "this weekend",
    ]
    billing_keywords = [
        "bill",
        "billing",
        "total",
        "price",
        "cost",
        "how much",
        "amount due",
        "magkano",
        "bayad",
    ]
    booking_keywords = [
        "book",
        "booking",
        "reserve",
        "reservation",
        "mag-book",
        "book online",
        "availability",
        "available pa",
        "check availability",
        "pay later",
    ]
    accommodation_keywords = [
        "hotel", "inn", "room", "accommodation", "place to stay", "stay", "matutuluyan", "tulugan",
        "persons", "pax",
    ]
    tourism_info_keywords = [
        "tourism information",
        "tourism info",
        "tourist spot",
        "tourist spots",
        "attraction",
        "attractions",
        "landmark",
        "landmarks",
        "operating hours",
        "opening hours",
        "pasyalan",
        "puntahan",
        "gala",
        "galaan",
        "beach",
        "nature spot",
        "city proper",
    ]
    if ("available" in text or "availability" in text) and ("room" in text or "hotel" in text or "inn" in text):
        return "get_accommodation_recommendation"
    if any(keyword in text for keyword in recommendation_keywords) and any(
        keyword in text for keyword in accommodation_keywords
    ):
        return "get_accommodation_recommendation"
    if any(keyword in text for keyword in booking_keywords) and any(keyword in text for keyword in accommodation_keywords):
        return "get_accommodation_recommendation"
    if any(keyword in text for keyword in billing_keywords):
        if any(keyword in text for keyword in accommodation_keywords):
            return "calculate_accommodation_billing"
        return "calculate_billing"
    if any(keyword in text for keyword in accommodation_keywords):
        return "get_accommodation_recommendation"
    if any(keyword in text for keyword in tourism_info_keywords):
        return "get_tourism_information"
    if _looks_like_tour_request(text):
        return "get_recommendation"
    return "get_recommendation"


def _is_dining_query(message):
    text = _normalize_chat_text(message)
    if not text:
        return False
    markers = (
        "where can i eat",
        "where to eat",
        "food spots",
        "dining",
        "restaurant",
        "restaurants",
        "kainan",
        "pagkaon",
    )
    return any(marker in text for marker in markers)


def _extract_tourism_search_tokens(message):
    raw = str(message or "").strip().lower()
    if not raw:
        return []
    cleaned = re.sub(r"[^a-z0-9\s\-]", " ", raw)
    stop_words = {
        "the", "and", "for", "with", "about", "please", "show", "tell", "me", "what",
        "where", "when", "how", "is", "are", "in", "on", "at", "of", "to", "a", "an",
        "tourism", "information", "info", "tourist", "spot", "spots", "attraction",
        "attractions", "landmark", "landmarks", "operating", "opening", "hours", "contact",
    }
    tokens = []
    for token in cleaned.split():
        token = token.strip("-")
        if len(token) < 3:
            continue
        if token in stop_words:
            continue
        tokens.append(token)
    deduped = []
    seen = set()
    for token in tokens:
        if token in seen:
            continue
        seen.add(token)
        deduped.append(token)
    return deduped[:8]


def _get_tourism_information(params, user_message):
    qs = TourismInformation.objects.published()
    if not qs.exists():
        return (
            "I don't have published tourism information yet. "
            "Please check back later once the Tourism Office publishes records."
        )

    location = str(params.get("location") or "").strip()
    if location:
        qs = qs.filter(location__icontains=location)

    tourism_query = str(params.get("tourism_query") or "").strip()
    if tourism_query:
        direct_qs = qs.filter(
            Q(spot_name__icontains=tourism_query)
            | Q(description__icontains=tourism_query)
            | Q(location__icontains=tourism_query)
        )
        if direct_qs.exists():
            qs = direct_qs

    if not tourism_query and _is_dining_query(user_message):
        dining_qs = qs.filter(
            Q(spot_name__icontains="restaurant")
            | Q(description__icontains="restaurant")
            | Q(description__icontains="food")
            | Q(description__icontains="dining")
            | Q(description__icontains="eat")
        )
        if dining_qs.exists():
            qs = dining_qs

    if not tourism_query:
        tokens = _extract_tourism_search_tokens(user_message)
        if tokens:
            token_query = Q()
            for token in tokens:
                token_query |= (
                    Q(spot_name__icontains=token)
                    | Q(description__icontains=token)
                    | Q(location__icontains=token)
                )
            token_qs = qs.filter(token_query).distinct()
            if token_qs.exists():
                qs = token_qs

    rows = list(qs.order_by("spot_name")[:3])
    if not rows:
        return (
            "I couldn't find a published tourism information match for that query. "
            "Please try another spot name or location."
        )

    lines = ["Here are the tourism information records I found:"]
    for idx, row in enumerate(rows, 1):
        lines.append(f"{idx}. {row.spot_name}")
        lines.append(f"   Location: {row.location or 'Not specified'}")
        if row.operating_hours:
            lines.append(f"   Operating hours: {row.operating_hours}")
        if row.contact_information:
            lines.append(f"   Contact: {row.contact_information}")
        if row.description:
            lines.append(f"   Description: {row.description}")
    return "\n".join(lines)


def _extract_origin_hint(message):
    text = _normalize_chat_text(message)
    if not text:
        return {"type": "unknown", "value": ""}
    if any(token in text for token in ("from another country", "international", "from abroad", "outside the philippines")):
        return {"type": "international", "value": "outside the Philippines"}
    from_match = re.search(r"\bfrom\s+([a-z][a-z\s]{2,50})", text)
    if from_match:
        origin_value = " ".join(str(from_match.group(1) or "").split()).strip()
        if origin_value:
            if (
                "bayawan" in origin_value
                or "city center" in origin_value
                or "downtown" in origin_value
                or "poblacion" in origin_value
                or "suba" in origin_value
            ):
                return {"type": "local", "value": origin_value}
            if "manila" in origin_value:
                return {"type": "domestic_far", "value": "Manila"}
            if any(city in origin_value for city in ("cebu", "davao", "iloilo", "bacolod", "dumaguete", "cagayan de oro")):
                return {"type": "domestic_far", "value": origin_value.title()}
            return {"type": "domestic_far", "value": origin_value.title()}
    return {"type": "unknown", "value": ""}


def _coords_from_map_match(raw_value):
    match = _match_map_reference_place(raw_value)
    if not isinstance(match, dict):
        return {"lat": None, "lng": None, "anchor": ""}
    lat_val = _safe_float(match.get("lat"))
    lng_val = _safe_float(match.get("lng"))
    anchor_name = str(match.get("name") or "").strip()
    if (lat_val is None or lng_val is None) and anchor_name:
        anchor_norm = _normalize_chat_text(anchor_name)
        for entry in _load_map_reference_place_entries():
            if _normalize_chat_text(entry.get("name")) == anchor_norm:
                lat_val = _safe_float(entry.get("lat"), default=lat_val)
                lng_val = _safe_float(entry.get("lng"), default=lng_val)
                break
    return {
        "lat": lat_val,
        "lng": lng_val,
        "anchor": anchor_name,
    }


def _resolve_destination_for_travel(message, params):
    params = params if isinstance(params, dict) else {}
    normalized_message = _normalize_chat_text(message)
    accom_name = str(params.get("accom_name") or params.get("hotel_name") or "").strip()
    tourism_query = str(params.get("tourism_query") or "").strip()
    location = str(params.get("location") or "").strip()

    if accom_name:
        qs = _approved_accommodation_queryset().filter(company_name__icontains=accom_name).order_by("company_name")
        accom = qs.first()
        if accom is not None:
            coord = _coords_from_map_match(str(getattr(accom, "company_name", "") or "") or str(getattr(accom, "location", "") or ""))
            link, label = _build_accommodation_official_link(row_meta={
                "official_booking_url": str(getattr(accom, "official_booking_url", "") or "").strip(),
                "official_contact_url": str(getattr(accom, "official_contact_url", "") or "").strip(),
            })
            return {
                "kind": "accommodation",
                "name": str(accom.company_name or "").strip(),
                "location": str(accom.location or "").strip(),
                "lat": coord.get("lat"),
                "lng": coord.get("lng"),
                "anchor": coord.get("anchor"),
                "link": link,
                "link_label": label or "Visit Official Page",
            }

    for accom in _approved_accommodation_queryset().order_by("company_name")[:40]:
        accom_name_norm = _normalize_chat_text(getattr(accom, "company_name", ""))
        if accom_name_norm and (accom_name_norm in normalized_message or normalized_message in accom_name_norm):
            coord = _coords_from_map_match(str(accom.company_name or "") or str(accom.location or ""))
            link, label = _build_accommodation_official_link(row_meta={
                "official_booking_url": str(getattr(accom, "official_booking_url", "") or "").strip(),
                "official_contact_url": str(getattr(accom, "official_contact_url", "") or "").strip(),
            })
            return {
                "kind": "accommodation",
                "name": str(accom.company_name or "").strip(),
                "location": str(accom.location or "").strip(),
                "lat": coord.get("lat"),
                "lng": coord.get("lng"),
                "anchor": coord.get("anchor"),
                "link": link,
                "link_label": label or "Visit Official Page",
            }

    if tourism_query:
        tourism_row = TourismInformation.objects.published().filter(
            Q(spot_name__icontains=tourism_query) | Q(location__icontains=tourism_query)
        ).order_by("spot_name").first()
    else:
        tourism_row = TourismInformation.objects.published().filter(
            Q(spot_name__icontains=normalized_message) | Q(location__icontains=normalized_message)
        ).order_by("spot_name").first()
    if tourism_row is not None:
        coord = _coords_from_map_match(str(getattr(tourism_row, "spot_name", "") or "") or str(getattr(tourism_row, "location", "") or ""))
        return {
            "kind": "tourist_spot",
            "name": str(tourism_row.spot_name or "").strip(),
            "location": str(tourism_row.location or "").strip(),
            "lat": coord.get("lat"),
            "lng": coord.get("lng"),
            "anchor": coord.get("anchor"),
            "link": "",
            "link_label": "",
        }

    location_candidate = tourism_query or message or location
    coord = _coords_from_map_match(location_candidate)
    if coord.get("anchor"):
        return {
            "kind": "place",
            "name": coord.get("anchor"),
            "location": str(location or "Bayawan City").strip(),
            "lat": coord.get("lat"),
            "lng": coord.get("lng"),
            "anchor": coord.get("anchor"),
            "link": "",
            "link_label": "",
        }
    return {"kind": "", "name": "", "location": location, "lat": None, "lng": None, "anchor": "", "link": "", "link_label": ""}


def _build_travel_guidance_payload(message, params, client_location):
    destination = _resolve_destination_for_travel(message, params)
    origin_hint = _extract_origin_hint(message)
    lines = []
    quick_replies = [
        "How far is this from me?",
        "I'm from Manila, how do I get there?",
        "Open map",
    ]
    action_link = ""
    action_label = ""

    destination_name = str(destination.get("name") or "that destination").strip() or "that destination"
    destination_location = str(destination.get("location") or "Bayawan").strip() or "Bayawan"
    lines.append(
        _pick_response_variant(
            [
                f"Sure, here’s a simple travel guide to {destination_name}.",
                f"Happy to help. Here’s the best practical guidance to reach {destination_name}.",
                f"Got it. Here’s a clear step-by-step guide to get to {destination_name}.",
            ],
            seed_text=str(message or ""),
        )
    )

    if origin_hint.get("type") == "international":
        lines.append(
            "If you're coming from outside the Philippines, the practical route is: fly into a major Philippine gateway, then connect to Negros Oriental (usually via Dumaguete or nearby hubs), and continue by land to Bayawan."
        )
        lines.append(
            "After arriving in Bayawan, local transport (tricycle, multicab, or hired vehicle) can take you to your destination."
        )
        lines.append("Typical travel flow: International origin -> Manila/Cebu -> Dumaguete -> Bus/van to Bayawan.")
    elif origin_hint.get("type") == "domestic_far":
        origin_name = str(origin_hint.get("value") or "your city").strip()
        if "manila" in origin_name.lower():
            lines.append("From Manila, a practical route is: Manila -> Dumaguete (flight) -> Bayawan (bus/van).")
            lines.append("Approximate overall travel time is often around 6 to 10 hours, depending on transfers.")
        else:
            lines.append(
                f"From {origin_name}, a typical route is to travel to Negros Oriental (often via Dumaguete or nearby hubs), then continue by land to Bayawan."
            )
        lines.append("Once you're in Bayawan, local transport can take you to the exact destination.")

    origin_lat = _safe_float(client_location.get("latitude")) if isinstance(client_location, dict) else None
    origin_lng = _safe_float(client_location.get("longitude")) if isinstance(client_location, dict) else None
    dest_lat = _safe_float(destination.get("lat"))
    dest_lng = _safe_float(destination.get("lng"))
    distance_km = None
    eta_minutes = None

    if origin_lat is not None and origin_lng is not None and dest_lat is not None and dest_lng is not None:
        distance_km = _haversine_km(origin_lat, origin_lng, dest_lat, dest_lng)
        eta_minutes = _estimate_travel_minutes(distance_km)
        lines.append(
            f"From your current location to {destination_name}, the estimated distance is about {distance_km:.1f} km."
        )
        if eta_minutes >= 60:
            eta_hours = eta_minutes / 60.0
            lines.append(f"Estimated travel time is around {eta_hours:.1f} hour(s), depending on traffic and vehicle.")
        else:
            lines.append(f"Estimated travel time is around {eta_minutes} minute(s), depending on traffic and vehicle.")
        action_link = (
            "https://www.google.com/maps/dir/?api=1"
            f"&origin={origin_lat},{origin_lng}"
            f"&destination={dest_lat},{dest_lng}"
            "&travelmode=driving"
        )
        action_label = f"Open Directions to {destination_name}"
    else:
        location_status = str(client_location.get("status") or "").strip().lower() if isinstance(client_location, dict) else ""
        if location_status == "denied":
            lines.append(
                "Location access looks disabled on your side, so I can't compute exact distance from your device yet."
            )
        if destination_name:
            lines.append(
                f"{destination_name} is in/near {destination_location}. I can give a better distance/time estimate if you allow current-location access or share your starting point."
            )
        else:
            lines.append(
                "I can guide you better if you tell me your destination (hotel, tourist spot, or tour meeting point) and your origin."
            )

    if destination.get("kind") == "accommodation" and destination.get("link"):
        lines.append("For accommodation booking or inquiry, please use the property's official page/contact link.")
        action_link = action_link or str(destination.get("link"))
        action_label = action_label or str(destination.get("link_label") or "Visit Official Page")

        lines.append("Travel times are approximate guides and can vary with traffic, weather, and transport availability.")

    return {
        "reply": "\n".join(lines),
        "quick_replies": quick_replies,
        "link": action_link,
        "link_label": action_label or "Open Map",
    }


def _normalize_intent_label(raw_label):
    normalized = str(raw_label or "").strip().lower()
    if not normalized:
        return ""
    normalized = _INTENT_LABEL_ALIASES.get(normalized, normalized)
    return normalized if normalized in _ALLOWED_INTENTS else ""


def _intent_confidence_threshold():
    raw = str(os.getenv("CHATBOT_INTENT_CNN_CONFIDENCE_THRESHOLD", "")).strip()
    if not raw:
        return 0.60
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 0.60
    return max(0.0, min(1.0, value))


def _classify_intent_with_text_cnn(message):
    global _TEXT_CNN_DISABLED_LOGGED
    if _is_text_cnn_intent_disabled():
        if not _TEXT_CNN_DISABLED_LOGGED:
            logger.info(
                "Text CNN intent classifier disabled by environment; using fallback intent routing."
            )
            _TEXT_CNN_DISABLED_LOGGED = True
        return {
            "intent": "",
            "source": "text_cnn_unavailable",
            "confidence": 0.0,
            "top_3": [],
            "error": "disabled_by_environment",
            "artifact_source": "env_disabled",
        }

    model_path, artifact_source = _resolve_intent_text_cnn_model_path()
    label_map_path = _default_label_map_path_for_model(model_path)
    prediction, err = _predict_text_cnn_labels(
        text=message,
        model_path=model_path,
        label_map_path=label_map_path,
    )
    if prediction is None:
        return {
            "intent": "",
            "source": "text_cnn_unavailable",
            "confidence": 0.0,
            "top_3": [],
            "error": err or "unknown_error",
            "artifact_source": artifact_source,
        }

    normalized_intent = _normalize_intent_label(prediction.get("predicted_class"))
    normalized_top3 = []
    raw_top3 = prediction.get("top_3") if isinstance(prediction.get("top_3"), list) else []
    for item in raw_top3:
        if not isinstance(item, dict):
            continue
        raw_label = item.get("label")
        mapped = _normalize_intent_label(raw_label)
        normalized_top3.append(
            {
                "raw_label": str(raw_label or ""),
                "intent": mapped,
                "confidence": float(item.get("confidence", 0.0) or 0.0),
            }
        )

    if normalized_intent:
        confidence = float(prediction.get("confidence", 0.0) or 0.0)
        threshold = _intent_confidence_threshold()
        if confidence < threshold:
            return {
                "intent": "",
                "source": "text_cnn_low_confidence",
                "confidence": confidence,
                "top_3": normalized_top3,
                "error": f"low_confidence:{confidence:.4f}<threshold:{threshold:.4f}",
                "artifact_source": artifact_source,
            }
        return {
            "intent": normalized_intent,
            "source": "text_cnn_intent",
            "confidence": confidence,
            "top_3": normalized_top3,
            "error": "",
            "artifact_source": artifact_source,
        }

    # Controlled compatibility fallback:
    # Existing deployments may still have accommodation-type labels (hotel/inn/etc.).
    return {
        "intent": "",
        "source": "text_cnn_incompatible_label_space",
        "confidence": 0.0,
        "top_3": normalized_top3,
        "error": "incompatible_label_space",
        "artifact_source": artifact_source,
    }


def _deterministic_intent_route(*, actor=None, message=""):
    role = str((actor or {}).get("role") or "").strip().lower()
    text = _normalize_chat_text(message)
    if not text:
        return ""

    if role in {"", "guest"}:
        if _contains_any_phrase(
            text,
            (
                "i want to go somewhere nice",
                "where should i go",
                "any recommendation",
                "what can i do",
            ),
        ):
            return "clarification"
        if _is_travel_guidance_request(text):
            return "travel_guidance"
        if _is_accommodation_how_to_book_request(text):
            return "book_accommodation"
        if _is_accommodation_preview_command(text):
            return "book_accommodation"
        if _is_guest_tour_booking_command(text):
            return "get_recommendation"
        if re.search(r"\b(show|available|list|display|view)\b.*\b(tour|tours|tour package|tour packages)\b", text):
            return "get_recommendation"
        # Deterministic shorthand for accommodation filters like
        # "suba under 1500" during conversational slot-filling.
        if (
            re.search(r"\b(?:under|below|budget)\s*[0-9][0-9,]*(?:\.[0-9]+)?k?\b", text)
            and re.search(r"\b(?:suba|poblacion|bayawan|villareal|tinago|ubos)\b", text)
            and not _looks_like_tour_request(text)
        ):
            return "get_accommodation_recommendation"
        if re.search(r"\b(hotel|inn|accommodation|stay|place to stay)\b", text):
            return "get_accommodation_recommendation"
        if _is_stay_planning_request(text):
            return "plan_bayawan_stay"
        if re.search(r"\bbudget\b\s*[0-9][0-9,]*(?:\.[0-9]+)?k?\b", text):
            return "plan_bayawan_stay"

    if role == "owner":
        if _contains_any_phrase(
            text,
            (
                "submit monthly report",
                "monthly report",
                "owner report",
                "owner monthly report",
            ),
        ):
            return "reporting_summary"

    if role == "employee":
        if _is_employee_assigned_tours_command(text):
            return "employee_assigned_tours"
        if _is_employee_open_assignment_command(text):
            return "employee_open_assignment"
        if _is_employee_assignment_update_command(text):
            return "employee_update_assignment"

    if role in {"", "admin"}:
        if _is_reporting_summary_request(text):
            return "reporting_summary"

    return ""


def _classify_intent_and_extract_params(message, actor=None):
    deterministic_intent = _deterministic_intent_route(actor=actor, message=message)
    normalized_text = _normalize_chat_text(message)
    actor_role = str((actor or {}).get("role") or "guest").strip().lower()

    if (
        actor_role in {"", "guest"}
        and deterministic_intent == "clarification"
        and _contains_any_phrase(
            normalized_text,
            (
                "i want to go somewhere nice",
                "where should i go",
                "any recommendation",
                "what can i do",
            ),
        )
    ):
        return {
            "intent": deterministic_intent,
            "params": {},
            "source": "deterministic_pre_route",
            "confidence": 1.0,
            "needs_clarification": False,
            "clarification_question": "",
            "clarification_field": "",
            "clarification_options": [],
            "intent_classifier": {
                "intent": deterministic_intent,
                "source": "deterministic_pre_route",
                "confidence": 1.0,
                "top_3": [],
                "error": "",
                "artifact_source": "deterministic_router",
            },
        }

    def _fast_extract_accommodation_params(text):
        fast_params = {}
        if re.search(r"\bhotel\b", text):
            fast_params["company_type"] = "hotel"
        elif re.search(r"\binn\b", text):
            fast_params["company_type"] = "inn"
        elif re.search(r"\b(?:accommodation|stay|place to stay)\b", text):
            fast_params["company_type"] = "either"

        guests_match = re.search(r"\b(\d+)\s*(?:people|person|guest|guests|pax|adult|adults)\b", text)
        if not guests_match:
            guests_match = re.search(r"\bfor\s+(\d+)\b", text)
        if guests_match:
            guests_val = _to_int(guests_match.group(1), default=0)
            if guests_val > 0:
                fast_params["guests"] = guests_val
                fast_params["group_size"] = guests_val

        budget_match = re.search(
            r"\b(?:under|below|budget(?:\s+is)?)\s*([0-9][0-9,]*(?:\.[0-9]+)?k?)\b",
            text,
            flags=re.IGNORECASE,
        )
        if budget_match:
            parse_compact = globals().get("_parse_compact_number")
            parsed_budget = (
                parse_compact(budget_match.group(1))
                if callable(parse_compact)
                else _to_int(budget_match.group(1), default=0)
            )
            if parsed_budget and parsed_budget > 0:
                fast_params["budget"] = int(parsed_budget)

        if re.search(r"\b(?:cheap|affordable|budget friendly|budget-friendly)\b", text):
            fast_params["prefer_low_price"] = True

        location_match = re.search(
            r"\b(suba|poblacion|bayawan|villareal|tinago|ubos)\b",
            text,
            flags=re.IGNORECASE,
        )
        if location_match:
            fast_params["location"] = str(location_match.group(1)).strip().lower()
        return fast_params

    if (
        actor_role in {"", "guest"}
        and deterministic_intent == "get_accommodation_recommendation"
    ):
        has_budgetish = bool(
            re.search(r"\b(?:under|below|budget)\s*[0-9][0-9,]*(?:\.[0-9]+)?k?\b", normalized_text)
            or re.search(r"\b(?:cheap|affordable)\b", normalized_text)
        )
        has_accom_or_loc = bool(
            re.search(r"\b(hotel|inn|accommodation|stay|place to stay)\b", normalized_text)
            or re.search(r"\b(suba|poblacion|bayawan|villareal|tinago|ubos)\b", normalized_text)
        )
        has_guests = bool(re.search(r"\b\d+\s*(?:people|person|guest|guests|pax|adult|adults)\b", normalized_text))
        if has_accom_or_loc and (has_budgetish or has_guests):
            fast_params = _fast_extract_accommodation_params(normalized_text)
            return {
                "intent": deterministic_intent,
                "params": fast_params,
                "source": "deterministic_pre_route_fast_accommodation",
                "confidence": 1.0,
                "needs_clarification": False,
                "clarification_question": "",
                "clarification_field": "",
                "clarification_options": [],
                "intent_classifier": {
                    "intent": deterministic_intent,
                    "source": "deterministic_pre_route_fast_accommodation",
                    "confidence": 1.0,
                    "top_3": [],
                    "error": "",
                    "artifact_source": "deterministic_router_fast",
                },
            }

    extracted = _extract_params_with_confidence(message)
    params = extracted.get("params", {}) if isinstance(extracted.get("params"), dict) else {}
    if deterministic_intent:
        return {
            "intent": deterministic_intent,
            "params": params,
            "source": "deterministic_pre_route",
            "confidence": 1.0,
            "needs_clarification": bool(extracted.get("needs_clarification")),
            "clarification_question": extracted.get("clarification_question", ""),
            "clarification_field": extracted.get("clarification_field", ""),
            "clarification_options": extracted.get("clarification_options", []),
            "intent_classifier": {
                "intent": deterministic_intent,
                "source": "deterministic_pre_route",
                "confidence": 1.0,
                "top_3": [],
                "error": "",
                "artifact_source": "deterministic_router",
            },
        }

    cnn_result = _classify_intent_with_text_cnn(message)

    if cnn_result.get("intent"):
        return {
            "intent": cnn_result.get("intent"),
            "params": params,
            "source": cnn_result.get("source", "text_cnn_intent"),
            "confidence": float(cnn_result.get("confidence", 0.0) or 0.0),
            "needs_clarification": bool(extracted.get("needs_clarification")),
            "clarification_question": extracted.get("clarification_question", ""),
            "clarification_field": extracted.get("clarification_field", ""),
            "clarification_options": extracted.get("clarification_options", []),
            "intent_classifier": cnn_result,
        }

    heuristic_intent = _intent_from_message(message)
    return {
        "intent": heuristic_intent,
        "params": params,
        "source": "heuristic_intent_fallback",
        "confidence": float(extracted.get("confidence", 1.0) or 1.0),
        "needs_clarification": bool(extracted.get("needs_clarification")),
        "clarification_question": extracted.get("clarification_question", ""),
        "clarification_field": extracted.get("clarification_field", ""),
        "clarification_options": extracted.get("clarification_options", []),
        "intent_classifier": cnn_result,
    }


def _is_my_accommodation_booking_status_command(message):
    text = (message or "").strip().lower()
    my_accommodation_booking_phrases = [
        "show my bookings",
        "view my bookings",
        "check my bookings",
        "my bookings",
        "show bookings",
        "booking status",
        "my booking status",
        "show my hotel bookings",
        "show my inn bookings",
        "show my accommodation bookings",
        "show accommodation links",
        "show my room bookings",
        "view my hotel bookings",
        "View accommodation links",
        "view accommodation bookings",
        "check my hotel bookings",
        "check accommodation bookings",
        "check my booking status for hotel",
        "my hotel bookings",
        "my accommodation bookings",
        "accommodation bookings",
        "hotel booking status",
        "inn booking status",
        "accommodation booking status",
        "reservation already confirmed",
        "is my reservation already confirmed",
        "is my booking confirmed",
        "check if my reservation is confirmed",
    ]
    return any(phrase in text for phrase in my_accommodation_booking_phrases)


def _is_guest_booking_requirements_command(message):
    text = str(message or "").strip().lower()
    if not text:
        return False
    return any(
        phrase in text
        for phrase in (
            "what details do i need to provide for booking",
            "what details do i need for booking",
            "what details are needed for booking",
            "what do i need to provide for booking",
            "requirements for booking",
            "booking requirements",
            "ano kailangan para mag book",
            "unsa kinahanglan para mag book",
        )
    )


def _is_guest_search_help_command(message):
    text = str(message or "").strip().lower()
    if not text:
        return False
    return any(
        phrase in text
        for phrase in (
            "how to search hotels",
            "how to search inns",
            "how to search hotel",
            "how to find hotels",
            "how to find inns",
            "how to find accommodation",
            "search hotels and inns",
            "paano maghanap ng hotel",
            "unsaon pagpangita og hotel",
        )
    )


def _is_guest_billing_details_help_command(message):
    text = str(message or "").strip().lower()
    if not text:
        return False
    return any(
        phrase in text
        for phrase in (
            "billing details",
            "show billing details",
            "how to view billing details",
            "view billing breakdown",
            "payment breakdown",
            "detalye sa billing",
            "detalye ng billing",
        )
    )


def _is_guest_booking_review_help_command(message):
    text = str(message or "").strip().lower()
    if not text:
        return False
    return any(
        phrase in text
        for phrase in (
            "how to review my booking",
            "how do i review my booking",
            "review my booking",
            "check my booking details",
            "view my booking details",
            "saan makikita booking ko",
            "asa makita akong booking",
        )
    )


def _is_guest_password_help_command(message):
    text = str(message or "").strip().lower()
    if not text:
        return False
    return any(
        phrase in text
        for phrase in (
            "forgot my password",
            "forgot password",
            "reset my password",
            "reset password",
            "can't log in",
            "cannot log in",
            "cant log in",
            "di maka login",
            "hindi makalogin",
            "nakalimutan ko password",
            "nalimot nako password",
        )
    )


def _is_guest_booking_cancel_support_command(message):
    text = str(message or "").strip().lower()
    if not text:
        return False
    return any(
        phrase in text
        for phrase in (
            "cancel my booking",
            "cancel booking",
            "cancel reservation",
            "cancel my reservation",
            "i want to cancel my booking",
            "i want to cancel my reservation",
            "pwede i-cancel",
            "icancel booking",
            "kansela booking",
        )
    )


def _is_guest_booking_change_date_command(message):
    text = str(message or "").strip().lower()
    if not text:
        return False
    return any(
        phrase in text
        for phrase in (
            "change my check-in date",
            "change check-in date",
            "change check in date",
            "change booking date",
            "reschedule my booking",
            "reschedule booking",
            "move my check-in",
            "move check-in",
            "baguhin check-in",
            "usab check-in",
        )
    )


def _is_guest_payment_methods_command(message):
    text = str(message or "").strip().lower()
    if not text:
        return False
    return any(
        phrase in text
        for phrase in (
            "payment method",
            "payment methods",
            "how can i pay",
            "how do i pay",
            "ways to pay",
            "mode of payment",
            "modes of payment",
            "paano magbayad",
            "unsaon pagbayad",
        )
    )


def _is_guest_down_payment_command(message):
    text = str(message or "").strip().lower()
    if not text:
        return False
    return any(
        phrase in text
        for phrase in (
            "down payment",
            "deposit required",
            "is down payment required",
            "need a deposit",
            "kailangan ba ng down payment",
            "need ba og downpayment",
        )
    )


def _is_guest_room_availability_command(message):
    text = str(message or "").strip().lower()
    if not text:
        return False
    if "available" in text and ("room" in text or "rooms" in text):
        return True
    return any(
        phrase in text
        for phrase in (
            "available pa tomorrow",
            "may available room",
            "available room ba",
            "are there available rooms today",
            "available rooms today",
        )
    )


def _is_accommodation_room_listing_command(message):
    text = str(message or "").strip().lower()
    if not text:
        return False
    direct_phrases = (
        "show rooms for",
        "rooms in ",
        "what rooms do they have",
        "show available rooms",
        "show me rooms",
        "list rooms",
        "available rooms in",
        "show rooms",
        "room options",
        "select room",
        "book this room",
        "book room",
    )
    if any(phrase in text for phrase in direct_phrases):
        return True
    return bool(
        re.search(r"\brooms?\b", text)
        and re.search(r"\b(show|list|view|available|what)\b", text)
    )


def _resolve_relative_stay_window(message):
    text = str(message or "").strip().lower()
    today = timezone.localdate()
    nights_hint_match = re.search(r"\b(\d+)\s*night", text)
    nights_hint = _to_int(nights_hint_match.group(1), default=0) if nights_hint_match else 0

    starting_tomorrow_match = re.search(r"\bfor\s+(\d+)\s*nights?\s+starting\s+tomorrow\b", text)
    if starting_tomorrow_match:
        nights = max(_to_int(starting_tomorrow_match.group(1), default=1), 1)
        check_in = today + timedelta(days=1)
        return check_in, check_in + timedelta(days=nights), (
            f"starting tomorrow for {nights} night(s) "
            f"({check_in.isoformat()} to {(check_in + timedelta(days=nights)).isoformat()})"
        )

    if "today" in text:
        return today, today + timedelta(days=1), f"today ({today.isoformat()})"
    if "tomorrow" in text:
        check_in = today + timedelta(days=1)
        nights = max(nights_hint, 1)
        return check_in, check_in + timedelta(days=nights), (
            f"tomorrow ({check_in.isoformat()} to {(check_in + timedelta(days=nights)).isoformat()})"
        )
    if "weekend" in text:
        days_until_saturday = (5 - today.weekday()) % 7
        saturday = today + timedelta(days=days_until_saturday)
        monday = saturday + timedelta(days=2)
        return saturday, monday, f"this weekend ({saturday.isoformat()} to {monday.isoformat()})"
    if "next week" in text:
        days_until_next_monday = (7 - today.weekday()) % 7
        if days_until_next_monday == 0:
            days_until_next_monday = 7
        next_monday = today + timedelta(days=days_until_next_monday)
        nights = max(nights_hint, 2)
        return next_monday, next_monday + timedelta(days=nights), (
            f"next week ({next_monday.isoformat()} to {(next_monday + timedelta(days=nights)).isoformat()})"
        )
    return None, None, ""


def _build_guest_room_availability_summary(message, params):
    check_in, check_out, period_label = _resolve_relative_stay_window(message)
    guests = _to_int((params or {}).get("guests"), default=0)
    budget = _to_decimal((params or {}).get("budget"), default=Decimal("0"))
    location = str((params or {}).get("location") or "").strip()

    qs = (
        _approved_room_queryset()
        .filter(status="AVAILABLE", current_availability__gte=1)
        .filter(
            Q(accommodation__company_type__icontains="hotel")
            | Q(accommodation__company_type__icontains="inn")
        )
    )
    if guests > 0:
        qs = qs.filter(person_limit__gte=guests)
    if budget > 0:
        qs = qs.filter(price_per_night__lte=budget)
    if location:
        qs = qs.filter(accommodation__location__icontains=location)
    if check_in and check_out:
        qs = qs.exclude(
            guest_bookings__status__in=["pending", "confirmed"],
            guest_bookings__check_in__lt=check_out,
            guest_bookings__check_out__gt=check_in,
        )

    rows = list(qs.order_by("price_per_night", "room_id")[:3])
    total = qs.count()
    filters = []
    if guests > 0:
        filters.append(f"for {guests} guest(s)")
    if budget > 0:
        filters.append(f"under PHP {int(budget)}")
    if location:
        filters.append(f"in/near {location}")
    filter_text = f" ({', '.join(filters)})" if filters else ""
    period_text = period_label or "the requested period"

    if total <= 0:
        return (
            f"I checked available hotel/inn rooms for {period_text}{filter_text}, but none matched.\n"
            "Try adjusting budget, guest count, or location so I can suggest alternatives."
        )

    lines = [f"I found {total} available room option(s) for {period_text}{filter_text}."]
    lines.append("Top options right now:")
    for idx, room in enumerate(rows, 1):
        lines.append(
            (
                f"{idx}. {room.accommodation.company_name} - {room.room_name} "
                f"| PHP {room.price_per_night}/night | "
                f"up to {room.person_limit} guests"
            )
        )
    lines.append("Reply with the option number (e.g., 1) or say: create booking preview for this room.")
    return "\n".join(lines)


def _is_accommodation_bookings_page_command(message):
    text = str(message or "").strip().lower()
    if not text:
        return False
    strong_phrases = (
        "accommodation bookings",
        "hotel bookings",
        "inn bookings",
        "room bookings",
        "booking status for hotel",
        "pending accommodation bookings",
    )
    if any(phrase in text for phrase in strong_phrases):
        return True
    return bool(
        re.search(r"\b(accommodation|hotel|inn|room)\b.*\bbooking", text)
        or re.search(r"\bbooking.*\b(accommodation|hotel|inn|room)\b", text)
    )


def _is_accommodation_owner_user(user, request=None):
    if not user or not getattr(user, "is_authenticated", False):
        return False
    try:
        if user.groups.filter(name__iexact="accommodation_owner").exists():
            return True
    except Exception:
        pass

    role_value = str(getattr(user, "role", "") or "").strip().lower()
    if role_value in {"accommodation_owner", "accommodation owner", "owner"}:
        return True

    if request is not None:
        try:
            session_user_type = str(request.session.get("user_type") or "").strip().lower()
            if session_user_type in {"accomodation", "accommodation", "establishment"}:
                return True
        except Exception:
            pass

    try:
        if hasattr(user, "owned_accommodations") and user.owned_accommodations.exists():
            return True
    except Exception:
        pass

    return False


def _resolve_chat_actor(request):
    user = getattr(request, "user", None)
    if user and getattr(user, "is_authenticated", False):
        # Prioritize elevated/staff roles before falling back to guest mode.
        try:
            if bool(getattr(user, "is_superuser", False)) or bool(getattr(user, "is_staff", False)):
                return {
                    "is_allowed": True,
                    "role": "admin",
                    "user": user,
                    "employee": None,
                    "display_name": str(getattr(user, "first_name", "") or getattr(user, "username", "") or "Admin").strip(),
                }
        except Exception:
            pass

        try:
            if user.groups.filter(name__iexact="admin").exists() or user.groups.filter(name__iexact="administrator").exists():
                return {
                    "is_allowed": True,
                    "role": "admin",
                    "user": user,
                    "employee": None,
                    "display_name": str(getattr(user, "first_name", "") or getattr(user, "username", "") or "Admin").strip(),
                }
            if user.groups.filter(name__iexact="employee").exists():
                return {
                    "is_allowed": True,
                    "role": "employee",
                    "user": user,
                    "employee": None,
                    "display_name": str(getattr(user, "first_name", "") or getattr(user, "username", "") or "Staff").strip(),
                }
        except Exception:
            pass

        try:
            session_user_type = str(request.session.get("user_type") or "").strip().lower()
            if session_user_type == "employee":
                return {
                    "is_allowed": True,
                    "role": "admin" if bool(request.session.get("is_admin")) else "employee",
                    "user": user,
                    "employee": None,
                    "display_name": str(getattr(user, "first_name", "") or getattr(user, "username", "") or "Staff").strip(),
                }
        except Exception:
            pass

        if _is_accommodation_owner_user(user, request=request):
            return {
                "is_allowed": True,
                "role": "owner",
                "user": user,
                "employee": None,
                "display_name": str(getattr(user, "first_name", "") or getattr(user, "username", "") or "Owner").strip(),
            }
        return {
            "is_allowed": True,
            "role": "guest",
            "user": user,
            "employee": None,
            "display_name": str(getattr(user, "first_name", "") or getattr(user, "username", "") or "Guest").strip(),
        }

    try:
        session_user_type = str(request.session.get("user_type") or "").strip().lower()
        employee_id = request.session.get("employee_id")
    except Exception:
        session_user_type = ""
        employee_id = None

    if session_user_type == "employee" and employee_id:
        employee = Employee.objects.filter(emp_id=employee_id).first()
        if employee and str(getattr(employee, "status", "") or "").strip().lower() == "accepted":
            is_admin = bool(request.session.get("is_admin")) or str(getattr(employee, "role", "")).strip().lower() == "admin"
            return {
                "is_allowed": True,
                "role": "admin" if is_admin else "employee",
                "user": None,
                "employee": employee,
                "display_name": str(getattr(employee, "first_name", "") or "Staff").strip(),
            }

    return {
        "is_allowed": False,
        "role": "anonymous",
        "user": None,
        "employee": None,
        "display_name": "User",
    }


def _is_help_or_greeting_command(message):
    text = str(message or "").strip().lower()
    if not text:
        return False
    quick = {
        "help",
        "menu",
        "commands",
        "start",
        "hello",
        "hi",
        "hey",
    }
    if text in quick:
        return True
    return any(
        phrase in text
        for phrase in (
            "what can you do",
            "how can you help",
            "assist me",
            "show commands",
            "show menu",
        )
    )


def _is_owner_help_command(message):
    return _contains_any_phrase(
        message,
        (
            "what can i do here as an owner",
            "what do i do here",
            "what do i do here as an owner",
            "owner help",
            "help owner",
            "what can i manage",
            "what can i do as owner",
        ),
    )


def _is_owner_manage_links_command(message):
    return _contains_any_phrase(
        message,
        (
            "update my accommodation links",
            "update links",
            "update official page",
            "update facebook link",
            "update accommodation images",
            "update listing links",
            "update official link",
            "update booking page",
            "update external link",
            "how do i update links",
            "where do i add photos",
        ),
    )


def _contains_any_phrase(message, phrases):
    raw_text = str(message or "").strip().lower()
    if not raw_text:
        return False
    text = re.sub(r"[^a-z0-9\s]", " ", raw_text)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return False

    # Keep direct substring matching first for backwards compatibility.
    for phrase in phrases:
        phrase_text = str(phrase or "").strip().lower()
        if not phrase_text:
            continue
        if phrase_text in raw_text:
            return True

    text_tokens = set(text.split())
    stopwords = {
        "a", "an", "the", "to", "for", "in", "on", "at", "of", "is", "are", "am",
        "my", "your", "our", "ako", "akong", "nako", "ko", "ang", "sa", "si", "ng",
        "yung", "ito", "yan", "kini", "kani", "ug", "and", "or",
    }
    for phrase in phrases:
        phrase_text = re.sub(r"[^a-z0-9\s]", " ", str(phrase or "").strip().lower())
        phrase_text = re.sub(r"\s+", " ", phrase_text).strip()
        if not phrase_text:
            continue
        if phrase_text in text:
            return True
        phrase_tokens = [tok for tok in phrase_text.split() if tok not in stopwords]
        # Only use unordered-token fallback when phrase still has useful signal.
        if len(phrase_tokens) >= 3 and all(token in text_tokens for token in phrase_tokens):
            return True
    return False


def _pick_response_variant(options, seed_text=""):
    choices = [str(opt).strip() for opt in (options or []) if str(opt).strip()]
    if not choices:
        return ""
    seed = str(seed_text or "")
    digest = hashlib.sha256(seed.encode("utf-8", errors="ignore")).hexdigest()
    idx = int(digest[:8], 16) % len(choices)
    return choices[idx]


def _normalize_common_chat_typos(message):
    text = str(message or "").strip()
    if not text:
        return text
    normalized = text
    typo_map = {
        r"\breccomend\b": "recommend",
        r"\brecomend\b": "recommend",
        r"\baccomodation\b": "accommodation",
        r"\baccomodations\b": "accommodations",
        r"\bbookigns\b": "bookings",
        r"\bwher\b": "where",
        r"\brestauarant\b": "restaurant",
        r"\brestarant\b": "restaurant",
        r"\bhow muchh\b": "how much",
        r"\btour pakage\b": "tour package",
    }
    for pattern, replacement in typo_map.items():
        normalized = re.sub(pattern, replacement, normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\s{2,}", " ", normalized).strip()
    return normalized


def _normalize_button_parity_message(message):
    text = str(message or "").strip()
    if not text:
        return text
    # Keep explicit room-listing queries intact so they do not get remapped
    # into broad accommodation discovery commands.
    if _is_accommodation_room_listing_command(text):
        return text
    normalized = _normalize_chat_text(text)
    if not normalized:
        return text

    # Preserve constrained accommodation discovery phrasing (location/budget/guests/room cues)
    # so deterministic recommendation parsing can use the original query details.
    if re.search(
        r"\b(barangay|brgy|suba|villareal|poblacion|tinago|ubos|in\s+[a-z]|near|under|below|budget|for\s+\d+\s*(guest|guests|adult|adults|pax)|room|rooms?)\b",
        normalized,
        flags=re.IGNORECASE,
    ):
        return text

    # Keep button-click and typed-input behavior aligned for common guest phrases.
    parity_rules = (
        (
            r"\b(show|view|check|list)\b.*\b(my\s+)?(tour\s+)?book(?:ing|ings|igns)\b"
            r"|\bmy tour bookings\b|\bshow my tour bookigns\b",
            "show my tour bookings",
        ),
        (
            r"\b(show|available|list|display|view)\b.*\b(tours?|tour packages?|packages?)\b"
            r"|\bwhat tours do you have\b|\btour packages?\b",
            "show available tours",
        ),
        (
            r"\b(show|recommend|find|suggest)\b.*\b(hotels?|inns?|accommodations?|stays?)\b"
            r"|\bwhere should i stay\b|\bplaces to stay\b|\bshow approved stays\b",
            "show accommodation recommendations",
        ),
        (
            r"\b(make|adjust|change|convert)\b.*\bbudget\b"
            r"|\bmake it cheaper\b|\blower (the )?budget\b|\bsomething cheaper\b|\bcheaper option\b",
            "adjust to budget version",
        ),
        (
            r"\bfamily version\b|\bfamily[- ]friendly\b|\bgood for kids\b"
            r"|\btraveling with family\b|\bfor kids\b|\bwith kids\b",
            "make it family-friendly",
        ),
        (
            r"\bhow far\b.*\bfrom me\b|\bdistance from me\b|\bnear me\b",
            "how far is this from me?",
        ),
        (
            r"\bwhere can i eat\b|\bwhere to eat\b|\bfood nearby\b|\bnearby food\b|\brestaurants? nearby\b",
            "where can i eat nearby",
        ),
    )
    for pattern, replacement in parity_rules:
        if re.search(pattern, normalized, flags=re.IGNORECASE):
            return replacement
    return text


def _extract_intro_first_name(message):
    text = str(message or "").strip()
    if not text:
        return ""
    lowered = text.lower()
    match = re.search(
        r"\b(?:i am|i'm|im|my name is|this is)\s+([a-z][a-z'\-]{1,30})\b",
        lowered,
    )
    if not match:
        return ""
    candidate = str(match.group(1) or "").strip().lower()
    blocked_tokens = {
        "from",
        "in",
        "near",
        "bayawan",
        "manila",
        "cebu",
        "dumaguete",
        "philippines",
        "today",
        "here",
    }
    if candidate in blocked_tokens:
        return ""
    return candidate[:1].upper() + candidate[1:]


def _load_social_profile(request):
    if not hasattr(request, "session"):
        return {}
    payload = request.session.get(_CHAT_SOCIAL_SESSION_KEY)
    return payload if isinstance(payload, dict) else {}


def _save_social_profile(request, payload):
    if not hasattr(request, "session"):
        return
    request.session[_CHAT_SOCIAL_SESSION_KEY] = payload if isinstance(payload, dict) else {}
    request.session.modified = True


def _load_assistant_memory(request):
    if not hasattr(request, "session"):
        return {}
    payload = request.session.get(_CHAT_ASSISTANT_MEMORY_SESSION_KEY)
    return payload if isinstance(payload, dict) else {}


def _save_assistant_memory(request, payload):
    if not hasattr(request, "session"):
        return
    request.session[_CHAT_ASSISTANT_MEMORY_SESSION_KEY] = payload if isinstance(payload, dict) else {}
    request.session.modified = True


def _assistant_memory_to_params(memory):
    source = memory if isinstance(memory, dict) else {}
    allowed_keys = (
        "total_budget",
        "budget",
        "duration_days",
        "group_size",
        "guests",
        "party_type",
        "origin_hint",
        "location",
        "accommodation_needed",
        "experience_style",
        "activity_mix",
        "spending_style",
    )
    params = {}
    for key in allowed_keys:
        value = source.get(key)
        if value in ("", None, []):
            continue
        params[key] = value
    return params


def _save_assistant_memory_from_context(request, *, actor, intent, params):
    if str(actor.get("role") or "").strip().lower() != "guest":
        return
    normalized_intent = str(intent or "").strip().lower()
    if normalized_intent not in _ALLOWED_INTENTS:
        return
    payload = _load_assistant_memory(request)
    if not isinstance(payload, dict):
        payload = {}

    topic_map = {
        "plan_bayawan_stay": "planning",
        "get_recommendation": "tours",
        "get_accommodation_recommendation": "accommodations",
        "travel_guidance": "directions",
        "reporting_summary": "reports",
        "get_tourism_information": "tourism_info",
    }
    active_topic = topic_map.get(normalized_intent)
    if active_topic:
        payload["active_topic"] = active_topic

    source_params = params if isinstance(params, dict) else {}
    remember_keys = (
        "total_budget",
        "budget",
        "duration_days",
        "group_size",
        "guests",
        "party_type",
        "origin_hint",
        "location",
        "accommodation_needed",
        "experience_style",
        "activity_mix",
        "spending_style",
    )
    for key in remember_keys:
        value = source_params.get(key)
        if value in ("", None, []):
            continue
        payload[key] = value
    payload["last_updated_epoch"] = int(time.time())
    _save_assistant_memory(request, payload)


def _looks_like_assistant_followup(message):
    text = _normalize_chat_text(message)
    if not text:
        return False
    if re.fullmatch(r"\d+\s*(days?|people|person|pax|guests?)?", text):
        return True
    markers = (
        "make it cheaper",
        "lower the budget",
        "budget version",
        "family version",
        "for 2 people",
        "for 3 people",
        "2 days only",
        "3 days only",
        "from manila",
        "just nearby",
        "nearby only",
    )
    return any(marker in text for marker in markers)


def _apply_strict_intent_overrides(*, actor, message, current_intent):
    role = str((actor or {}).get("role") or "").strip().lower()
    text = _normalize_chat_text(message)
    intent = str(current_intent or "").strip().lower()

    if role == "guest":
        if _is_accommodation_preview_command(text) or _is_accommodation_how_to_book_request(text):
            return "book_accommodation"
        if _is_guest_tour_booking_command(text):
            return "get_recommendation"
        if _is_stay_planning_request(text):
            return "plan_bayawan_stay"
        if _is_travel_guidance_request(text):
            return "travel_guidance"
        if _is_reporting_summary_request(text):
            return "reporting_summary"
        if _looks_like_tour_request(text) and re.search(
            r"\b(show|available|list|display|view|tour|tours|tour package|tour packages)\b",
            text,
        ):
            return "get_recommendation"
        if _contains_any_phrase(text, ("what can i do there", "things to do there", "what can i do in bayawan", "things to do in bayawan")):
            return "get_recommendation"
        if re.search(r"\b(hotel|inn|accommodation|stay|place to stay)\b", text):
            return "get_accommodation_recommendation"
        return intent

    if role == "employee":
        if _is_employee_assigned_tours_command(text):
            return "employee_assigned_tours"
        if _is_employee_open_assignment_command(text):
            return "employee_open_assignment"
        if _is_employee_assignment_update_command(text):
            return "employee_update_assignment"
        return intent

    if role == "owner":
        if _contains_any_phrase(
            text,
            (
                "submit monthly report",
                "monthly report",
                "owner monthly report",
                "owner report",
            ),
        ):
            return "reporting_summary"
        if _is_reporting_summary_request(text):
            return "reporting_summary"
        return intent

    if role == "admin":
        if _is_reporting_summary_request(text):
            return "reporting_summary"
        return intent

    return intent


def _has_strict_intent_signal(*, actor, message):
    role = str((actor or {}).get("role") or "").strip().lower()
    text = _normalize_chat_text(message)
    if not text:
        return False
    if role == "guest":
        if _is_stay_planning_request(text) or _is_travel_guidance_request(text):
            return True
        if re.search(r"\b(hotel|inn|accommodation|place to stay|stay)\b", text):
            return True
        if re.search(r"\b(show|available|list|view)\b.*\b(tour|tours|tour package|tour packages)\b", text):
            return True
        if _is_guest_tour_booking_command(text):
            return True
        return False
    if role == "employee":
        return (
            _is_employee_assigned_tours_command(text)
            or _is_employee_open_assignment_command(text)
            or _is_employee_assignment_update_command(text)
        )
    if role == "owner":
        return bool(
            _contains_any_phrase(
                text,
                (
                    "update links",
                    "update my accommodation links",
                    "update facebook link",
                    "update official link",
                    "submit monthly report",
                    "owner report",
                    "owner monthly report",
                ),
            )
        )
    if role == "admin":
        return _is_reporting_summary_request(text)
    return False


def _is_explicit_tour_command(message):
    text = str(message or "").strip().lower()
    if not text:
        return False
    explicit_phrases = (
        "show available tours",
        "show tours",
        "tour packages",
        "book tour",
        "book a tour",
        "show tour schedules",
        "tour schedules",
        "available tours",
        "list tours",
    )
    return any(phrase in text for phrase in explicit_phrases)


def _looks_like_room_selection_reply(message):
    text = _normalize_chat_text(message)
    if not text:
        return False
    if _is_explicit_tour_command(text):
        return False
    if re.search(r"\b(yes|no|confirm|proceed|okay|ok|cancel)\b", text):
        return False
    if re.search(r"\b\d+\s*(guest|guests|adult|adults|pax|night|nights)\b", text):
        return False
    if re.search(r"\b(jan|january|feb|february|mar|march|apr|april|may|jun|june|jul|july|aug|august|sep|sept|september|oct|october|nov|november|dec|december)\b", text):
        return False
    tokens = [token for token in re.split(r"[\s,._\-]+", text) if token]
    if not tokens or len(tokens) > 8:
        return False
    room_markers = {
        "room", "suite", "deluxe", "standard", "family", "single", "double",
        "twin", "queen", "king", "matrimonial", "budget", "triple", "quad",
    }
    return any(token in room_markers for token in tokens)


def _assistant_display_first_name(actor):
    if not isinstance(actor, dict):
        return ""
    raw = str(actor.get("display_name") or "").strip()
    if not raw:
        return ""
    token = raw.split()[0].strip(" ,.!?")
    if not token or not re.fullmatch(r"[A-Za-z][A-Za-z\-']{0,24}", token):
        return ""
    return token


def _assistant_ack_prefix(intent, message, actor=None):
    normalized_intent = str(intent or "").strip().lower()
    first_name = _assistant_display_first_name(actor)
    name_line = ""
    if first_name and str((actor or {}).get("role") or "").strip().lower() == "guest":
        name_line = _pick_response_variant(
            [
                f"Got it, {first_name}.",
                f"Sure, {first_name}.",
                f"Alright, {first_name}.",
            ],
            seed_text=f"{message}|ack-name",
        )
    if name_line and (len(str(message or "").strip()) % 3 == 0):
        return name_line
    if normalized_intent == "plan_bayawan_stay":
        return _pick_response_variant(
            [
                "Got it.",
                "Sure.",
                "Alright.",
            ],
            seed_text=f"{message}|ack-planning",
        )
    if normalized_intent in ("travel_guidance",):
        return _pick_response_variant(
            [
                "Sure, I can help with that.",
                "Got it, let me guide you.",
                "No problem, here's a simple guide.",
            ],
            seed_text=f"{message}|ack-travel",
        )
    if normalized_intent in ("get_accommodation_recommendation", "gethotelrecommendation"):
        return _pick_response_variant(
            [
                "Sure.",
                "Got it.",
                "No problem.",
            ],
            seed_text=f"{message}|ack-accom",
        )
    return _pick_response_variant(
        [
            "Sure.",
            "Got it.",
            "Alright.",
        ],
        seed_text=f"{message}|ack-generic",
    )


def _assistant_next_step_prompt(*, intent, params, memory):
    normalized_intent = str(intent or "").strip().lower()
    params = params if isinstance(params, dict) else {}
    memory = memory if isinstance(memory, dict) else {}
    if normalized_intent == "plan_bayawan_stay":
        if _to_int(params.get("total_budget"), default=0) > 0:
            return _pick_response_variant(
                [
                    "Want me to refine this plan or show booking links?",
                    "I can adjust this further if you want.",
                    "Need me to tune this for budget, family, or tours?",
                ],
                seed_text=f"{params}|next-planning",
            )
        return _pick_response_variant(
            [
                "Want me to continue this with your budget and trip details?",
                "I can keep refining this once you share a few more details.",
                "Need help filling in the missing trip details?",
            ],
            seed_text=f"{params}|next-planning-missing",
        )
    if normalized_intent == "get_recommendation":
        return _pick_response_variant(
            [
                "Want me to show schedules or guide you to booking?",
                "Want me to filter these tours by date?",
                "I can help you choose one if you want.",
            ],
            seed_text=f"{params}|next-tours",
        )
    if normalized_intent in ("get_accommodation_recommendation", "gethotelrecommendation"):
        return _pick_response_variant(
            [
                "Want me to open the official link for one of these?",
                "I can refine these options if you want.",
                "Need official booking or contact links for these?",
            ],
            seed_text=f"{params}|next-accom",
        )
    if normalized_intent == "travel_guidance":
        if str(params.get("origin_hint") or memory.get("origin_hint") or "").strip():
            return _pick_response_variant(
                [
                    "Share your exact starting point and I can tighten this route estimate.",
                    "If you give your exact origin, I can make this guidance more precise.",
                    "Want me to narrow this using your exact start location?",
                ],
                seed_text=f"{params}|next-travel-origin",
            )
        return _pick_response_variant(
            [
                "Want me to map this from your starting point?",
                "Share where you're coming from and I'll guide you step by step.",
                "I can make this more precise once I know your origin.",
            ],
            seed_text=f"{params}|next-travel",
        )
    if normalized_intent == "get_tourism_information":
        return _pick_response_variant(
            [
                "Want directions from your location too?",
                "I can also suggest nearby spots if you want.",
                "Need me to show nearby options next?",
            ],
            seed_text=f"{params}|next-tourism-info",
        )
    if normalized_intent == "reporting_summary":
        return _pick_response_variant(
            [
                "Want me to summarize a specific month or accommodation?",
                "I can break this down by period if you want.",
                "Need a quick comparison across recent months?",
            ],
            seed_text=f"{params}|next-reporting",
        )
    return ""


def _build_contextual_ux_suggestions(*, intent, response_payload, actor_role):
    normalized_intent = str(intent or "").strip().lower()
    role = str(actor_role or "").strip().lower()
    payload = response_payload if isinstance(response_payload, dict) else {}
    if payload.get("needs_clarification"):
        return []

    if normalized_intent in ("get_accommodation_recommendation", "gethotelrecommendation"):
        return []

    if normalized_intent == "plan_bayawan_stay":
        return [
            {"label": "Make it cheaper", "value": "adjust to budget version"},
            {"label": "Family-friendly", "value": "make it family-friendly"},
            {"label": "Show matching tours", "value": "show available tours"},
        ]

    if normalized_intent == "travel_guidance":
        return [
            {"label": "Estimate travel time", "value": "how many minutes will it take"},
            {"label": "From my location", "value": "how far is this from me"},
            {"label": "How do I get there?", "value": "how do i get there"},
        ]

    if normalized_intent in ("get_tourism_information",):
        return [
            {"label": "Nearby places to eat", "value": "where can i eat nearby"},
            {"label": "Add to trip plan", "value": "add this to my trip plan"},
            {"label": "Show directions", "value": "how do i get there"},
        ]

    if normalized_intent in ("get_recommendation", "gettourrecommendation"):
        return [
            {"label": "View tour schedules", "value": "show tour schedules"},
            {"label": "Book a tour", "value": "book tour"},
            {"label": "Plan my stay", "value": "plan my stay"},
        ]

    if normalized_intent in ("reporting_summary",) and role in {"admin", "employee", "owner"}:
        return [
            {"label": "Latest monthly summary", "value": "show latest monthly report"},
            {"label": "Per accommodation summary", "value": "show per accommodation report"},
            {"label": "Tourist influx summary", "value": "show tourist influx summary"},
        ]

    return []


def _inject_contextual_ux_suggestions(response_payload, *, intent, actor_role):
    if not isinstance(response_payload, dict):
        return response_payload
    existing = response_payload.get("quick_replies")
    if isinstance(existing, list) and len(existing) >= 3:
        return response_payload
    contextual = _build_contextual_ux_suggestions(
        intent=intent,
        response_payload=response_payload,
        actor_role=actor_role,
    )
    if not contextual:
        return response_payload
    response_payload["quick_replies"] = _merge_quick_replies(
        existing if isinstance(existing, list) else [],
        contextual,
        limit=3,
    )
    return response_payload


def _assistant_followup_bridge(message, memory, intent):
    text = _normalize_chat_text(message)
    if not text:
        return ""
    active_topic = str((memory or {}).get("active_topic") or "").strip().lower()
    normalized_intent = str(intent or "").strip().lower()
    if re.search(r"\b(cheap|cheaper|lower budget|budget version|mas mura|barato)\b", text):
        return _pick_response_variant(
            [
                "Got it. I'll adjust this to a lower-budget version.",
                "Sure. I'll focus this on more budget-friendly options.",
                "Alright. I'll make this more affordable.",
            ],
            seed_text=f"{message}|followup-cheaper",
        )
    if re.search(r"\b(family|family[- ]friendly|kids|children)\b", text):
        return _pick_response_variant(
            [
                "Okay. I'll adjust this for a family setup.",
                "Got it. I'll make this more family-friendly.",
                "Sure. I'll prioritize family-friendly options.",
            ],
            seed_text=f"{message}|followup-family",
        )
    if re.search(r"\b(how far|distance|how many minutes|travel time)\b", text):
        return _pick_response_variant(
            [
                "Got it. Let me check the distance for you.",
                "Sure. I'll estimate the travel distance and time.",
                "Alright. I'll map out the distance from your location.",
            ],
            seed_text=f"{message}|followup-distance",
        )
    if re.search(r"\b(show more|more options|more)\b", text):
        return _pick_response_variant(
            [
                "Sure. Here are more options you can consider.",
                "Got it. I'll pull more options.",
                "No problem. Let me show additional options.",
            ],
            seed_text=f"{message}|followup-more",
        )
    if re.search(r"\b(show tours?|tour packages?)\b", text) and (
        active_topic in {"planning", "tours"} or normalized_intent in {"plan_bayawan_stay", "get_recommendation"}
    ):
        return _pick_response_variant(
            [
                "Sure. Here are tours that match your current plan.",
                "Got it. I'll show tours that fit your setup.",
                "Alright. Here are tour options for your plan.",
            ],
            seed_text=f"{message}|followup-tours",
        )
    return ""


def _apply_assistant_response_style(*, actor, intent, message, text, params, memory, needs_clarification=False, nlg_source=""):
    if needs_clarification:
        clarification = str(text or "").strip()
        if not clarification:
            return clarification
        lowered = clarification.lower()
        if any(token in lowered for token in ("clarify", "which one", "what", "could you")):
            return clarification
        return _pick_response_variant(
            [
                "I want to make sure I understood correctly. " + clarification,
                "Could you clarify what you need help with? " + clarification,
                "Just to confirm, what would you like help with? " + clarification,
            ],
            seed_text=f"{message}|clarification-style",
        )
    body = str(text or "").strip()
    if not body:
        return body
    replacements = {
        "Here are some places you might like based on your preferences:": "I found a few approved accommodations that match your request:",
        "Please select an option to view room details or to create a booking preview.": "You can view details, check rooms, or create a cost preview.",
        "I can guide you using the official accommodation pages below.": "You can continue through the accommodation's official page.",
        "based on currently available hotels/inns (no preference filter yet)": "based on currently available approved accommodations",
    }
    for src, dst in replacements.items():
        body = body.replace(src, dst)
    # Preserve highly structured backend templates as-is.
    lowered = body.lower()
    if any(
        marker in lowered
        for marker in (
            "great. here are the details i have so far:",
            "booking receipt / summary",
            "booking draft (not yet saved)",
        )
    ):
        return body
    normalized_nlg_source = str(nlg_source or "").strip().lower()
    if normalized_nlg_source in {"openai_nlg", "gemini_nlg", "gemini_nlg_retry"} and "\n" not in body and len(body) <= 220:
        return body

    followup_line = _assistant_followup_bridge(message, memory, intent)
    if followup_line and followup_line.lower() not in lowered:
        body = f"{followup_line}\n{body}"
        lowered = body.lower()

    has_ack = bool(re.match(r"^(got it|sure|great|thanks|happy to help|sounds good|absolutely)\b", lowered))
    if not has_ack:
        ack = _assistant_ack_prefix(intent, message, actor=actor)
        if "\n" in body:
            body = f"{ack}\n{body}"
        else:
            body = f"{ack} {body}"
        lowered = body.lower()

    actor_role = str((actor or {}).get("role") or "").strip().lower()
    next_step = _assistant_next_step_prompt(intent=intent, params=params, memory=memory)
    if actor_role != "guest" and str(intent or "").strip().lower() in {
        "get_recommendation",
        "get_accommodation_recommendation",
        "gethotelrecommendation",
        "plan_bayawan_stay",
    }:
        next_step = ""
    if next_step and next_step.lower() not in body.lower() and len(body) < 2200:
        body = f"{body}\n\n{next_step}"
    return body


def _limit_assistant_quick_replies_for_guest(response_payload, *, limit=3):
    if not isinstance(response_payload, dict):
        return response_payload
    qr = response_payload.get("quick_replies")
    if not isinstance(qr, list) or not qr:
        return response_payload
    response_payload["quick_replies"] = _sanitize_quick_replies(qr, limit=max(1, min(int(limit), 3)))
    return response_payload


def _build_small_talk_payload(*, request, actor, message):
    text = str(message or "").strip()
    lowered = text.lower()
    if not lowered:
        return None

    profile = _load_social_profile(request)
    remembered_name = str(profile.get("first_name") or "").strip()
    introduced_name = _extract_intro_first_name(text)
    if introduced_name:
        profile["first_name"] = introduced_name
        _save_social_profile(request, profile)
        remembered_name = introduced_name

    has_greeting = bool(
        re.search(
            r"\b(hi|hello|hey|good morning|good afternoon|good evening|good day|yo)\b",
            lowered,
        )
    )
    asks_help = _contains_any_phrase(
        lowered,
        (
            "can you help me",
            "can u help me",
            "are you available",
            "can you assist",
            "need help",
            "what can you do",
            "how can you help",
            "assist me",
            "show menu",
            "show commands",
        ),
    )
    says_thanks = bool(re.search(r"\b(thanks|thank you|salamat)\b", lowered))
    asks_how_are_you = bool(re.search(r"\b(how are you|kumusta)\b", lowered))
    says_sorry = bool(re.search(r"\b(sorry|apologies|pasensya)\b", lowered))

    # Do not swallow actionable tourism queries as small-talk.
    # Example: "can you help me plan my stay in bayawan?"
    actionable_query = (
        _is_stay_planning_request(lowered)
        or _is_guest_tour_booking_command(lowered)
        or _looks_like_tour_request(lowered)
        or _is_dining_query(lowered)
        or _is_travel_guidance_request(lowered)
        or _is_reporting_summary_request(lowered)
        or any(token in lowered for token in ("hotel", "inn", "accommodation", "where should i stay"))
    )
    if actionable_query:
        return None

    if not (has_greeting or asks_help or says_thanks or asks_how_are_you or says_sorry or introduced_name):
        return None

    role = str(actor.get("role") or "").strip().lower()
    guest_quick_replies = [
        "Plan my 10k stay",
        "Show available tours",
        "How far is this from me?",
        "Show my tour bookings",
    ]

    if says_thanks and not (has_greeting or asks_help):
        return {
            "fulfillmentText": _pick_response_variant(
                [
                    "You're welcome. I’m here whenever you need help planning your Bayawan trip.",
                    "Anytime. If you want, I can help with tours, directions, or stay planning next.",
                    "Glad to help. I can continue with tours, places to stay, or nearby dining anytime.",
                ],
                seed_text=lowered,
            ),
            "quick_replies": guest_quick_replies if role == "guest" else ["Help", "Open dashboard"],
        }

    if asks_how_are_you:
        base = _pick_response_variant(
            [
                "I’m doing well, thanks for asking.",
                "I’m good and ready to help.",
                "I’m doing great, and I’m here to help with your Bayawan plans.",
            ],
            seed_text=lowered,
        )
        next_line = (
            "I can help you with tours, places to stay, directions, dining spots, or trip planning. What would you like first?"
            if role == "guest"
            else "How can I help you with your current tasks?"
        )
        return {
            "fulfillmentText": f"{base} {next_line}",
            "quick_replies": guest_quick_replies if role == "guest" else ["Help", "Open dashboard"],
        }

    if asks_help and role == "guest":
        return {
            "fulfillmentText": (
                "I can help you find approved accommodations, show rooms, create accommodation cost previews, "
                "explore tour packages, submit tour booking requests, check directions, and plan your Bayawan trip."
            ),
            "quick_replies": guest_quick_replies,
        }

    if has_greeting or asks_help or says_sorry or introduced_name:
        name_part = f", {remembered_name}" if remembered_name else ""
        if role == "guest":
            greeting_line = _pick_response_variant(
                [
                    f"Hi{name_part}. Welcome to Ibayaw Tour.",
                    f"Hello{name_part}. Great to have you here at Ibayaw Tour.",
                    f"Hey{name_part}. Happy to help with your Bayawan plans.",
                ],
                seed_text=f"{lowered}|social-greeting-line",
            )
            capability_line = _pick_response_variant(
                [
                    "I can help you explore Bayawan tours, approved accommodations, directions, and trip planning.",
                    "I can help with tour packages, approved stays, travel directions, and practical Bayawan trip planning.",
                    "I can guide you through tours, approved accommodations, directions, and plan options for your trip.",
                ],
                seed_text=f"{lowered}|social-capability-line",
            )
            follow = _pick_response_variant(
                [
                    "What would you like to do first?",
                    "How can I help with your trip today?",
                    "What can I help you with right now?",
                ],
                seed_text=lowered,
            )
            return {
                "fulfillmentText": f"{greeting_line} {capability_line} {follow}",
                "quick_replies": guest_quick_replies,
            }
        role_help = _build_role_help_payload(actor)
        return {
            "fulfillmentText": _pick_response_variant(
                [
                    f"Hi{name_part}. I’m here and ready to help.",
                    f"Hello{name_part}. I can help you with your current workspace tasks.",
                ],
                seed_text=lowered,
            )
            + " "
            + str(role_help.get("fulfillmentText") or ""),
            "quick_replies": role_help.get("quick_replies") if isinstance(role_help.get("quick_replies"), list) else ["Help"],
        }

    return None


def _is_remember_preferences_command(message):
    return _contains_any_phrase(
        message,
        (
            "remember my preferences",
            "save my preferences",
            "remember this preference",
            "remember these preferences",
            "save this preference",
            "save these preferences",
        ),
    )


def _is_forget_preferences_command(message):
    return _contains_any_phrase(
        message,
        (
            "forget my preferences",
            "clear my preferences",
            "remove my preferences",
            "reset my preferences",
            "forget preferences",
        ),
    )


def _extract_memory_preference_payload(params):
    if not isinstance(params, dict):
        return {}
    payload = {}
    for key in (
        "company_type",
        "location",
        "budget",
        "guests",
        "preference_tags",
        "prefer_low_price",
        "amenities",
    ):
        value = params.get(key)
        if value in (None, "", [], {}):
            continue
        payload[key] = value
    return payload


def _apply_saved_preferences_to_params(params, saved_prefs):
    if not isinstance(params, dict):
        params = {}
    if not isinstance(saved_prefs, dict):
        return params
    merged = dict(params)
    for key, value in saved_prefs.items():
        if key not in (
            "company_type",
            "location",
            "budget",
            "guests",
            "preference_tags",
            "prefer_low_price",
            "amenities",
        ):
            continue
        if merged.get(key) in (None, "", [], {}):
            merged[key] = value
    return merged


def _build_role_operational_snapshot(actor):
    role = str(actor.get("role") or "").strip().lower()
    user = actor.get("user")
    employee = actor.get("employee")

    try:
        if role == "owner" and user is not None:
            owner_accom_qs = Accomodation.objects.filter(owner=user, is_active=True)
            accepted_accom_qs = owner_accom_qs.filter(approval_status="accepted")
            room_qs = Room.objects.filter(accommodation__in=accepted_accom_qs)
            reports_qs = OwnerMonthlyReport.objects.filter(accommodation__in=accepted_accom_qs).exclude(status="draft")
            return (
                "Current snapshot: "
                f"{owner_accom_qs.count()} accommodation(s), "
                f"{room_qs.count()} room(s), "
                f"{reports_qs.count()} submitted monthly report(s)."
            )

        if role == "admin":
            pending_accom = Accomodation.objects.filter(is_active=True, approval_status="pending").count()
            pending_owner_group, _ = Group.objects.get_or_create(name="accommodation_owner_pending")
            pending_owner = pending_owner_group.user_set.count()
            today = timezone.localdate()
            today_bookings = AccommodationBooking.objects.filter(booking_date__date=today).count()
            return (
                "Current snapshot: "
                f"{pending_accom} pending accommodation approval(s), "
                f"{pending_owner} pending owner account(s), "
                f"{today_bookings} booking event(s) today."
            )

        if role == "employee":
            active_sched = Tour_Schedule.objects.filter(status="active").count()
            upcoming_sched = Tour_Schedule.objects.filter(start_time__date__gte=timezone.localdate()).count()
            name = str(getattr(employee, "first_name", "") or "").strip() if employee else ""
            prefix = f"{name}, " if name else ""
            return (
                f"Current snapshot: {prefix}"
                f"{active_sched} active schedule(s), "
                f"{upcoming_sched} upcoming schedule(s)."
            )
    except Exception:
        return ""

    return ""


def _build_role_help_payload(actor):
    role = str(actor.get("role") or "").strip().lower()
    snapshot = _build_role_operational_snapshot(actor)
    if role == "owner":
        snapshot_block = f"{snapshot}\n" if snapshot else ""
        return {
            "fulfillmentText": (
                "Owner assistant mode is active. I can help you check your business side.\n"
                f"{snapshot_block}"
                "- Show my accommodations\n"
                "- Show my rooms\n"
                "- Update official links/images\n"
                "- Submit monthly report\n"
                "- Open reports and analytics\n"
                "- Open Owner Hub\n"
                "- Show tourism information about <place>\n\n"
                "For registration/edits, use Owner Hub.\n"
                "For Tourism Office compliance, submit check-ins per room type in monthly reports."
            ),
            "quick_replies": [
                "Show my accommodations",
                "Show my rooms",
                "Update my accommodation links",
                "Submit monthly report",
                "How many available rooms today?",
                "Open reports and analytics",
                "Open Owner Hub",
            ],
        }
    if role == "admin":
        snapshot_block = f"{snapshot}\n" if snapshot else ""
        return {
            "fulfillmentText": (
                "Admin assistant mode is active.\n"
                f"{snapshot_block}"
                "I can answer tourism information, show moderation summaries, and help with quick navigation.\n"
                "Use the Admin Dashboard for approvals, encoding, and reports."
            ),
            "quick_replies": [
                "Open dashboard",
                "Open map",
                "Open discounts",
                "Open traveler surveys",
                "Show pending accommodations",
                "Show pending owner accounts",
                "How to activate or deactivate listings?",
                "Open accommodation links",
            ],
        }
    if role == "employee":
        snapshot_block = f"{snapshot}\n" if snapshot else ""
        return {
            "fulfillmentText": (
                "Employee assistant mode is active.\n"
                f"{snapshot_block}"
                "I can answer tourism information and guide role-based navigation.\n"
                "Use the Employee Dashboard for operational tasks."
            ),
            "quick_replies": [
                "Open dashboard",
                "Open tour list",
                "Open assigned tours",
                "How to manage tourist records?",
                "Open tour calendar",
                "Open map",
                "Open profile",
            ],
        }
    return {
        "fulfillmentText": (
            "You're in guest assistant mode. I can help you discover approved accommodations, plan a Bayawan stay, and guide directions with travel estimates.\n"
            "Try this: suggest a hotel in Bayawan for 2 guests under 2000, or ask how far a place is from your location."
        ),
        "quick_replies": [
            "Recommend a hotel in Bayawan for 2 guests under 2000",
            "How far is Bayawan City Plaza from me?",
            "I'm from Manila, how do I get to Bayawan?",
            "How to search hotels and inns?",
            "Open map",
            "Show tourism information in Bayawan",
            "Show my tour bookings",
            "Remember my preferences",
            "Forget my preferences",
        ],
    }


def _build_out_of_scope_payload(actor, message=""):
    role = str(actor.get("role") or "").strip().lower()
    seed = str(message or "")
    if role == "owner":
        return {
            "fulfillmentText": _pick_response_variant(
                [
                    "I might have missed what you need there.\nI can still help with owner tasks like managing accommodations, updating links/images, monthly reports, and Owner Hub navigation.",
                    "I want to guide you correctly.\nI can help with owner workflows such as accommodation updates, room management, monthly reports, and Owner Hub actions.",
                    "I’m not fully sure which owner task you mean yet.\nI can assist with accommodation management, link/image updates, reports, and Owner Hub navigation.",
                ],
                seed_text=f"{seed}|owner-oos",
            ),
            "quick_replies": ["Help", "Show my rooms", "Submit monthly report", "Open Owner Hub"],
        }
    if role == "admin":
        return {
            "fulfillmentText": _pick_response_variant(
                [
                    "I want to make sure I guide you to the right admin task.\nI can help with approvals, visibility checks, surveys, and admin navigation.",
                    "I may have missed your exact admin intent.\nI can help with approvals, records visibility, survey monitoring, and dashboard navigation.",
                    "Let’s narrow that down so I can help quickly.\nI can assist with approvals, moderation checks, surveys, and admin pages.",
                ],
                seed_text=f"{seed}|admin-oos",
            ),
            "quick_replies": ["Help", "Show pending accommodations", "Show pending owner accounts", "Open dashboard"],
        }
    if role == "employee":
        return {
            "fulfillmentText": _pick_response_variant(
                [
                    "I’m not fully sure which employee task you meant.\nI can help with assigned tours, tour calendar, accommodations, profile, and dashboard navigation.",
                    "I want to make sure I route you to the right employee task.\nI can help with assigned tours, calendar, accommodations, profile, and dashboard actions.",
                    "I may have missed your exact request.\nI can assist with assigned tours, tour calendar, accommodations, profile, and employee dashboard navigation.",
                ],
                seed_text=f"{seed}|employee-oos",
            ),
            "quick_replies": ["Help", "Open assigned tours", "Open tour calendar", "Open dashboard"],
        }
    return {
        "fulfillmentText": _pick_response_variant(
            [
                "I'm mainly designed to help with Bayawan tourism services, such as tours, approved accommodations, directions, trip planning, and booking previews.",
                "I can best help with Bayawan tourism tasks: tours, approved accommodations, directions, trip planning, and booking previews.",
                "I can guide Bayawan tourism requests like tours, approved stays, directions, trip planning, and booking previews.",
            ],
            seed_text=f"{seed}|guest-oos",
        ),
        "quick_replies": [
            "Plan my Bayawan trip",
            "Show available tours",
            "Find approved stays",
            "Get directions",
        ],
    }


def _clarification_fallback(message):
    prompt = str(message or "").strip()
    if not prompt:
        prompt = "Could you clarify what you need help with?"
    return f"I want to make sure I understood correctly. {prompt}"


def _role_aware_clarification_payload(actor):
    role = str((actor or {}).get("role") or "").strip().lower()
    if role == "owner":
        return {
            "text": _clarification_fallback(
                "As an owner, do you need help with accommodation details, links/images, rooms, or monthly reports?"
            ),
            "quick_replies": [
                {"label": "Accommodation Details", "value": "show my accommodations"},
                {"label": "Links/Images", "value": "update my accommodation links"},
                {"label": "Rooms", "value": "show my rooms"},
                {"label": "Monthly Reports", "value": "submit monthly report"},
            ],
        }
    if role == "employee":
        return {
            "text": _clarification_fallback(
                "As staff, do you want assigned tours, assignment details, or tourist records support?"
            ),
            "quick_replies": [
                {"label": "Assigned Tours", "value": "show my assigned tours"},
                {"label": "Open Assignment", "value": "open may 5 assignment"},
                {"label": "Tour Calendar", "value": "open tour calendar"},
                {"label": "Tourist Records", "value": "open dashboard"},
            ],
        }
    if role == "admin":
        return {
            "text": _clarification_fallback(
                "For admin monitoring, do you need summaries, reports, or tourism monitoring data?"
            ),
            "quick_replies": [
                {"label": "Summary", "value": "show this month's summary"},
                {"label": "Reports", "value": "show accommodation reports"},
                {"label": "Tourist Influx", "value": "show tourist influx"},
                {"label": "Dashboard", "value": "open dashboard"},
            ],
        }
    return {
        "text": _clarification_fallback(
            "Do you want help with tours, accommodations, directions, or full trip planning?"
        ),
        "quick_replies": [
            {"label": "Tours", "value": "show available tours"},
            {"label": "Accommodations", "value": "show accommodation recommendations"},
            {"label": "Directions", "value": "how to get to bayawan"},
            {"label": "Plan Stay", "value": "plan my bayawan trip"},
        ],
    }


def _no_data_fallback():
    return "No results found for that request. Try adjusting your filters."


def _system_fallback():
    return "Something went wrong. Let me try that again."


def _is_open_dashboard_command(message):
    text = str(message or "").strip().lower()
    if not text:
        return False
    return any(
        phrase in text
        for phrase in ("open dashboard", "go to dashboard", "dashboard")
    )


def _is_out_of_scope_message(message):
    text = str(message or "").strip().lower()
    if not text:
        return False
    if re.fullmatch(r"\d{1,2}", text):
        return False

    domain_keywords = (
        "bayawan",
        "tour",
        "itinerary",
        "schedule",
        "tourism",
        "hotel",
        "inn",
        "accommodation",
        "room",
        "booking",
        "bookings",
        "billing",
        "payment",
        "dashboard",
        "owner",
        "employee",
        "admin",
        "reports",
        "analytics",
        "survey",
        "map",
        "spot",
        "attraction",
    )
    if any(keyword in text for keyword in domain_keywords):
        return False

    out_scope_markers = (
        "poem",
        "joke",
        "lyrics",
        "translate",
        "who is",
        "what is",
        "where is",
        "when did",
        "why is",
        "history of",
        "solve",
        "math",
        "code this",
        "president",
        "capital of",
        "medical advice",
        "legal advice",
        "financial advice",
        "java programming",
        "homework",
        "essay",
    )
    if any(marker in text for marker in out_scope_markers):
        return True

    return False


def _is_owner_room_overview_command(message):
    text = str(message or "").strip().lower()
    if not text:
        return False
    phrases = (
        "show my rooms",
        "view my rooms",
        "list my rooms",
        "check my rooms",
        "my rooms",
        "show rooms",
        "room list",
        "room status",
    )
    if any(phrase in text for phrase in phrases):
        return True
    return bool(
        re.search(r"\b(show|view|list|check)\b.*\b(my\s+)?rooms?\b", text)
    )


def _is_owner_accommodation_overview_command(message):
    text = str(message or "").strip().lower()
    if not text:
        return False
    phrases = (
        "show my accommodations",
        "view my accommodations",
        "list my accommodations",
        "my accommodations",
        "my accommodation",
        "show my inns",
        "show my hotels",
        "show my businesses",
    )
    if any(phrase in text for phrase in phrases):
        return True
    return bool(
        re.search(r"\b(show|view|list|check)\b.*\b(my\s+)?(accommodation|accommodations|hotel|inn|business)\b", text)
    )


def _is_owner_hub_command(message):
    return _contains_any_phrase(
        message,
        (
            "open owner hub",
            "go to owner hub",
            "owner hub",
            "owner dashboard",
            "open accommodation owner hub",
            "adto owner hub",
            "ablihi owner hub",
            "owner panel",
        ),
    )


def _is_owner_register_accommodation_command(message):
    return _contains_any_phrase(
        message,
        (
            "register accommodation",
            "add accommodation",
            "create accommodation",
            "open accommodation registration",
            "register hotel",
            "register inn",
            "add hotel",
            "add inn",
            "parehistro ug accommodation",
            "parehistro ug hotel",
            "parehistro ug inn",
            "mag register ng accommodation",
        ),
    )


def _is_owner_performance_summary_command(message):
    return _contains_any_phrase(
        message,
        (
            "owner performance",
            "my performance",
            "show my summary",
            "show my business summary",
            "show occupancy",
            "occupancy summary",
            "room occupancy",
            "show revenue",
            "revenue summary",
            "income summary",
            "booking performance",
            "performance snapshot",
            "pakita occupancy",
            "pakita revenue",
            "pakita summary",
        ),
    )


def _is_owner_reports_analytics_command(message):
    return _contains_any_phrase(
        message,
        (
            "open reports and analytics",
            "open reports",
            "owner reports",
            "hotel reports",
            "reports analytics",
            "show reports and analytics",
            "show reports",
            "open analytics",
            "owner analytics",
            "pakita reports",
            "pakita analytics",
        ),
    )


def _detect_owner_support_topic(message):
    text = str(message or "").strip().lower()
    if not text:
        return ""
    compact_text = re.sub(r"[^a-z0-9\s]", " ", text)
    compact_text = re.sub(r"\s+", " ", compact_text).strip()

    if _contains_any_phrase(text, ("forgot my password", "forgot password", "reset password", "can't log in", "cannot log in", "cant log in", "nakalimutan ko password", "di makalogin", "dili ko ka log in")):
        return "owner_password_help"
    if _contains_any_phrase(
        text,
        (
            "is my accommodation approved",
            "is my listing approved",
            "listing status",
            "approval status of my accommodation",
            "status of my listing",
            "approved na ba ang listing ko",
            "na approve na ba accommodation ko",
        ),
    ):
        return "owner_listing_status"
    if _contains_any_phrase(text, ("listing not showing", "listing is missing", "my listing is missing", "not showing on the website", "hindi lumalabas ang listing", "listing missing", "wala nagpakita akong listing", "di makita listing")):
        return "owner_listing_visibility"
    if _contains_any_phrase(text, ("cannot update room", "can't update room", "cant update room", "cannot edit room", "i cannot update my room details", "i-edit ang room details", "edit room details after posting", "di ko ma update room", "dili ma edit room")):
        return "owner_room_update_issue"

    if _contains_any_phrase(text, ("register my hotel", "register my inn", "register accommodation", "add my inn", "add my hotel", "i-register ang hotel", "i register ang hotel", "parehistro sa akong hotel", "irehistro ko ang inn")):
        return "owner_register_listing"
    if _contains_any_phrase(text, ("requirements", "need to submit", "what requirements", "list my accommodation", "unsa requirements", "ano requirements")):
        return "owner_listing_requirements"
    if _contains_any_phrase(text, ("update my accommodation information", "edit my hotel profile", "edit my accommodation", "update accommodation information", "update listing", "edit listing profile", "i update ang listing profile", "usba akong accommodation info")):
        return "owner_listing_update"

    if _contains_any_phrase(text, ("add a new room", "add room", "register room", "mag add room", "dugang kwarto")):
        return "owner_add_room"
    if _contains_any_phrase(text, ("update room price", "update my room rates", "update room rates", "update price", "room price", "room rates", "usba presyo sa kwarto", "iupdate ko ang room rate")):
        return "owner_update_room_price"
    if _contains_any_phrase(text, ("change the room capacity", "room capacity", "change capacity", "capacity of room", "update room capacity", "usba capacity sa kwarto", "ilan capacity ng room")):
        return "owner_update_room_capacity"
    if _contains_any_phrase(text, ("mark a room as unavailable", "room unavailable", "mark unavailable", "close a room for maintenance", "maintenance", "i-mark unavailable ang room", "isarado ang room for maintenance")):
        return "owner_mark_room_unavailable"
    if _contains_any_phrase(text, ("edit the amenities of a room", "edit amenities", "room amenities", "amenities sa kwarto", "amenities ng room")):
        return "owner_edit_room_amenities"

    if _contains_any_phrase(
        text,
        (
            "monthly report",
            "submit monthly report",
            "owner monthly report",
            "tourism office report",
            "tourism monitoring report",
            "report submission",
            "submit report",
            "where do i input room check ins",
            "where do i input room check-ins",
            "how do i report room usage",
            "add check-ins for",
            "room check-ins",
        ),
    ):
        return "owner_submit_monthly_report"

    if _contains_any_phrase(text, ("update room availability", "availability update", "reopen a room", "re-open a room", "reopen room", "iupdate availability sa room", "ablihi balik ang kwarto")):
        return "owner_update_availability"
    if _contains_any_phrase(
        text,
        (
            "how many available rooms today",
            "available rooms today",
            "rooms available today",
            "how many rooms are available today",
            "available ba rooms today",
            "pila ka available rooms karon",
        ),
    ):
        return "owner_available_rooms_today"
    if _contains_any_phrase(text, ("still showing as available", "why is my room still showing as available", "still available", "nganong available gihapon", "bakit available pa rin")):
        return "owner_room_still_available_issue"
    if _contains_any_phrase(text, ("block dates", "block date", "close dates", "unavailable dates", "i block ang dates", "isarado ang petsa")):
        return "owner_block_dates"

    if _contains_any_phrase(
        text,
        (
            "official page link",
            "facebook link",
            "update listing links",
            "update contact links",
            "external booking link",
        ),
    ):
        return "owner_listing_update"

    if compact_text in {"add room", "update price", "listing not showing", "room unavailable how"}:
        mapping = {
            "add room": "owner_add_room",
            "update price": "owner_update_room_price",
            "listing not showing": "owner_listing_visibility",
            "room unavailable how": "owner_mark_room_unavailable",
        }
        return mapping.get(compact_text, "")

    return ""


def _build_owner_manage_rooms_link_payload(request, *, text, label="Open Manage Rooms"):
    user = getattr(request, "user", None)
    accepted = (
        Accomodation.objects.filter(owner=user, approval_status="accepted", is_active=True)
        .order_by("company_name", "accom_id")
        .first()
    )
    if accepted is None:
        return _build_link_payload(
            request,
            text=f"{text}\nI could not find an accepted accommodation yet. Open Owner Hub first.",
            route_name="admin_app:owner_hub",
            label="Open Owner Hub",
        )
    manage_link = reverse("admin_app:owner_manage_rooms", kwargs={"accom_id": accepted.accom_id})
    if hasattr(request, "build_absolute_uri"):
        manage_link = request.build_absolute_uri(manage_link)
    return {
        "fulfillmentText": text,
        "billing_link": manage_link,
        "billing_link_label": label,
        "open_in_new_tab": True,
    }


def _build_owner_booking_payment_summary(user, *, max_rows=5):
    owner_bookings = (
        AccommodationBooking.objects.select_related("accommodation", "room")
        .filter(accommodation__owner=user, accommodation__is_active=True)
        .order_by("-booking_date", "-booking_id")
    )
    total = owner_bookings.count()
    if total <= 0:
        return (
            "No booking records found for your accommodations yet.\n"
            "Once guests create bookings, booking and payment status will appear here."
        )

    status_counts = Counter(
        str(status or "").strip().lower()
        for status in owner_bookings.values_list("status", flat=True)
    )
    payment_counts = Counter(
        str(status or "").strip().lower()
        for status in owner_bookings.values_list("payment_status", flat=True)
    )
    lines = [
        (
            f"Owner booking summary: Total {total}, "
            f"Pending {status_counts.get('pending', 0)}, "
            f"Confirmed {status_counts.get('confirmed', 0)}, "
            f"Declined {status_counts.get('declined', 0)}, "
            f"Cancelled {status_counts.get('cancelled', 0)}."
        ),
        (
            f"Payment summary: Unpaid {payment_counts.get('unpaid', 0)}, "
            f"Partial {payment_counts.get('partial', 0)}, "
            f"Paid {payment_counts.get('paid', 0)}."
        ),
        "Recent bookings:",
    ]
    shown = 0
    for booking in owner_bookings[: max(max_rows, 1)]:
        if shown >= max_rows:
            break
        accom_name = str(getattr(booking.accommodation, "company_name", "") or "Accommodation").strip()
        room = getattr(booking, "room", None)
        room_label = f"Room {getattr(room, 'room_id', '')}" if room is not None else "No room"
        lines.append(
            (
                f"- Booking ID {booking.booking_id} | {accom_name} | {room_label} | "
                f"{booking.check_in} to {booking.check_out} | "
                f"Status: {str(booking.status).title()} | Payment: {str(booking.payment_status).title()} | "
                f"Paid PHP {Decimal(booking.amount_paid):.2f} / PHP {Decimal(booking.total_amount):.2f}"
            )
        )
        shown += 1
    return "\n".join(lines)


def _build_owner_booking_count_today_summary(user):
    today = timezone.localdate()
    active_today_qs = AccommodationBooking.objects.filter(
        accommodation__owner=user,
        accommodation__is_active=True,
        check_in__lte=today,
        check_out__gt=today,
    ).exclude(status__in=["declined", "cancelled"])

    room_ids_today = {
        int(room_id)
        for room_id in active_today_qs.values_list("room_id", flat=True)
        if room_id
    }
    booking_count_today = active_today_qs.count()
    pending_today = active_today_qs.filter(status="pending").count()
    confirmed_today = active_today_qs.filter(status="confirmed").count()

    lines = [
        f"Today ({today.isoformat()}), {len(room_ids_today)} room(s) under your listing(s) have active bookings.",
        (
            f"Booking rows affecting today: total {booking_count_today}, "
            f"confirmed {confirmed_today}, pending {pending_today}."
        ),
        "Use Monthly Reports to submit Tourism Office monitoring updates.",
    ]
    return "\n".join(lines)


def _build_owner_available_rooms_today_summary(user):
    today = timezone.localdate()
    accepted_qs = Accomodation.objects.filter(
        owner=user,
        is_active=True,
        approval_status="accepted",
    )
    if not accepted_qs.exists():
        return (
            "I could not find an accepted accommodation under your account yet.\n"
            "Complete listing approval first, then room availability can be monitored from your dashboard."
        )

    rooms_qs = Room.objects.filter(accommodation__in=accepted_qs)
    available_rooms_qs = rooms_qs.filter(status="AVAILABLE")
    unavailable_rooms_qs = rooms_qs.exclude(status="AVAILABLE")
    active_today_bookings_qs = AccommodationBooking.objects.filter(
        accommodation__in=accepted_qs,
        check_in__lte=today,
        check_out__gt=today,
    ).exclude(status__in=["declined", "cancelled"])
    booked_room_ids = {
        int(room_id)
        for room_id in active_today_bookings_qs.values_list("room_id", flat=True)
        if room_id
    }
    physically_available_now = max(available_rooms_qs.count() - len(booked_room_ids), 0)
    lines = [
        f"Room availability snapshot for today ({today.isoformat()}):",
        (
            f"- Total rooms: {rooms_qs.count()} | Marked AVAILABLE: {available_rooms_qs.count()} | "
            f"Unavailable/Maintenance: {unavailable_rooms_qs.count()}"
        ),
        (
            f"- Rooms currently occupied by active bookings: {len(booked_room_ids)} | "
            f"Rooms likely available now: {physically_available_now}"
        ),
        "Use Manage Rooms to adjust status and Owner Bookings for booking-level checks.",
    ]
    return "\n".join(lines)


def _build_owner_listing_status_summary(user):
    qs = Accomodation.objects.filter(owner=user, is_active=True).order_by("-submitted_at", "company_name")
    if not qs.exists():
        return (
            "No active accommodation listing is linked to your owner account yet.\n"
            "Use Accommodation Registration to submit your first listing."
        )
    counts = Counter(str(v or "").strip().lower() for v in qs.values_list("approval_status", flat=True))
    lines = [
        (
            "Listing approval status snapshot: "
            f"Accepted {counts.get('accepted', 0)}, "
            f"Pending {counts.get('pending', 0)}, "
            f"Declined {counts.get('declined', 0)}."
        ),
        "Your latest listings:",
    ]
    for accom in qs[:5]:
        lines.append(
            f"- {accom.company_name} | {str(accom.approval_status or '').title()} | {accom.location}"
        )
    return "\n".join(lines)


def _build_owner_billing_details_summary(user, *, max_rows=5):
    billing_qs = (
        Billing.objects.select_related("booking", "booking__accommodation", "booking__room")
        .filter(booking__accommodation__owner=user, booking__accommodation__is_active=True)
        .order_by("-billing_date", "-billing_id")
    )
    total = billing_qs.count()
    if total <= 0:
        return "No billing records found yet for your accommodations."
    lines = [f"Billing records found: {total}. Recent billing entries:"]
    shown = 0
    for billing in billing_qs[: max(max_rows, 1)]:
        if shown >= max_rows:
            break
        booking = getattr(billing, "booking", None)
        accom = getattr(booking, "accommodation", None) if booking is not None else None
        accom_name = str(getattr(accom, "company_name", "") or "Accommodation").strip()
        room = getattr(booking, "room", None) if booking is not None else None
        room_id = getattr(room, "room_id", "")
        lines.append(
            (
                f"- Billing ID {billing.billing_id} | Booking ID {getattr(booking, 'booking_id', '')} | "
                f"{accom_name} | Room {room_id} | "
                f"Status: {str(billing.payment_status).title()} | "
                f"Method: {str(billing.payment_method or 'Not Set').replace('_', ' ').title()} | "
                f"Paid PHP {Decimal(billing.amount_paid):.2f} / PHP {Decimal(billing.total_amount):.2f}"
            )
        )
        shown += 1
    return "\n".join(lines)


def _build_owner_listing_visibility_diagnostic(user):
    accom_qs = Accomodation.objects.filter(owner=user, is_active=True)
    total = accom_qs.count()
    if total <= 0:
        return (
            "I could not find any active accommodation listing under your account.\n"
            "Please register your accommodation first from Owner Hub."
        )
    accepted = accom_qs.filter(approval_status="accepted")
    pending = accom_qs.filter(approval_status="pending")
    declined = accom_qs.filter(approval_status="declined")

    lines = [
        (
            f"Listing visibility check: Total {total}, "
            f"Accepted {accepted.count()}, Pending {pending.count()}, Declined {declined.count()}."
        )
    ]
    if pending.exists():
        lines.append("- Some listings are still pending admin approval; they may not appear publicly yet.")
    if declined.exists():
        lines.append("- Some listings were declined. Please open Owner Hub and update required details before re-submission.")
    if accepted.exists():
        accepted_rooms = Room.objects.filter(accommodation__in=accepted).count()
        lines.append(f"- Accepted listings currently have {accepted_rooms} room record(s).")
        if accepted_rooms <= 0:
            lines.append("- Add rooms to your accepted listing so it can appear in stay recommendations.")
    lines.append("Use Owner Hub and Manage Rooms to refresh listing details.")
    return "\n".join(lines)


def _build_owner_accommodations_summary(user, *, max_rows=8):
    owner_accommodations = list(
        Accomodation.objects.filter(owner=user, is_active=True).order_by("-submitted_at", "company_name")
    )
    if not owner_accommodations:
        return (
            "You don't have any accommodation record yet. "
            "Open Owner Hub and click Register New Accommodation."
        )

    status_counts = Counter(
        str(status or "").strip().lower()
        for status in Accomodation.objects.filter(owner=user, is_active=True).values_list("approval_status", flat=True)
    )
    lines = [
        (
            f"You currently have {len(owner_accommodations)} accommodation(s): "
            f"Accepted {status_counts.get('accepted', 0)}, "
            f"Pending {status_counts.get('pending', 0)}, "
            f"Declined {status_counts.get('declined', 0)}."
        ),
        "Your accommodations:",
    ]

    shown = 0
    for accom in owner_accommodations:
        if shown >= max_rows:
            break
        room_count = Room.objects.filter(accommodation=accom).count()
        status = str(getattr(accom, "approval_status", "") or "").strip().title() or "Unknown"
        company_type = str(getattr(accom, "company_type", "") or "Accommodation").strip().title()
        lines.append(
            f"- {accom.company_name} ({company_type}) | Status: {status} | Rooms: {room_count}"
        )
        shown += 1

    if len(owner_accommodations) > shown:
        lines.append(f"...and {len(owner_accommodations) - shown} more accommodation(s).")

    lines.append("Tip: Use Owner Hub to manage rooms and submit additional accommodations.")
    return "\n".join(lines)


def _build_owner_rooms_summary(user, *, max_rows=12):
    accepted_accommodations = list(
        Accomodation.objects.filter(owner=user, approval_status="accepted", is_active=True).order_by("company_name")
    )
    if not accepted_accommodations:
        return (
            "You don't have any accepted accommodation yet. "
            "Please wait for admin approval, then add rooms in Owner Hub."
        )

    rooms_qs = (
        Room.objects.select_related("accommodation")
        .filter(accommodation__in=accepted_accommodations)
        .order_by("accommodation__company_name", "room_name", "room_id")
    )
    total_rooms = rooms_qs.count()
    if total_rooms <= 0:
        return (
            "Your accepted accommodation is ready, but no rooms are registered yet. "
            "Open Owner Hub and click Manage Rooms to add your first room."
        )

    available_slots = rooms_qs.aggregate(total=Sum("current_availability")).get("total") or 0
    lines = [
        (
            f"You currently have {total_rooms} room(s) across "
            f"{len(accepted_accommodations)} accepted accommodation(s). "
            f"Total available slots: {available_slots}."
        ),
        "Your rooms:",
    ]

    shown = 0
    for room in rooms_qs:
        if shown >= max_rows:
            break
        accom = getattr(room, "accommodation", None)
        accom_name = str(getattr(accom, "company_name", "") or "Accommodation").strip()
        room_name = str(getattr(room, "room_name", "") or "Room").strip()
        price = _to_decimal(getattr(room, "price_per_night", 0), default=Decimal("0"))
        capacity = _to_int(getattr(room, "person_limit", 0), default=0)
        current = _to_int(getattr(room, "current_availability", 0), default=0)
        status = str(getattr(room, "status", "") or "").strip().title() or "Unknown"
        lines.append(
            (
                f"- {accom_name} | {room_name} | "
                f"PHP {price:.2f}/night | Capacity: {capacity} pax | "
                f"Available: {current} | Status: {status}"
            )
        )
        shown += 1

    if total_rooms > shown:
        lines.append(f"...and {total_rooms - shown} more room(s).")

    lines.append("Tip: Open Owner Hub > Manage Rooms to edit prices, pax, and availability.")
    return "\n".join(lines)


def _build_owner_performance_summary(user):
    accepted_accommodations = list(
        Accomodation.objects.filter(owner=user, approval_status="accepted", is_active=True)
    )
    if not accepted_accommodations:
        return (
            "You don't have any accepted accommodation yet, so performance metrics are not available.\n"
            "Once approved, add rooms and receive bookings to populate this summary."
        )

    rooms_qs = Room.objects.filter(accommodation__in=accepted_accommodations)
    total_rooms = rooms_qs.count()
    total_capacity = rooms_qs.aggregate(total=Sum("person_limit")).get("total") or 0
    total_available = rooms_qs.aggregate(total=Sum("current_availability")).get("total") or 0
    total_occupied = max(int(total_capacity) - int(total_available), 0)
    occupancy_pct = (float(total_occupied) / float(total_capacity) * 100.0) if total_capacity else 0.0

    booking_qs = AccommodationBooking.objects.filter(accommodation__in=accepted_accommodations)
    total_bookings = booking_qs.count()
    pending_count = booking_qs.filter(status="pending").count()
    confirmed_count = booking_qs.filter(status="confirmed").count()
    declined_count = booking_qs.filter(status="declined").count()
    cancelled_count = booking_qs.filter(status="cancelled").count()
    total_confirmed_revenue = booking_qs.filter(status="confirmed").aggregate(total=Sum("total_amount")).get("total") or Decimal("0")
    total_paid = booking_qs.aggregate(total=Sum("amount_paid")).get("total") or Decimal("0")

    lines = [
        "Owner Performance Snapshot",
        f"- Accepted accommodations: {len(accepted_accommodations)}",
        f"- Total rooms: {total_rooms}",
        f"- Estimated occupancy now: {occupancy_pct:.1f}% ({total_occupied} occupied capacity out of {total_capacity})",
        (
            f"- Booking counts: total {total_bookings}, pending {pending_count}, "
            f"confirmed {confirmed_count}, declined {declined_count}, cancelled {cancelled_count}"
        ),
        f"- Confirmed booking revenue (gross): PHP {Decimal(total_confirmed_revenue):.2f}",
        f"- Total amount paid (collected): PHP {Decimal(total_paid):.2f}",
        "Note: Occupancy is estimated from room capacity vs current availability.",
    ]
    return "\n".join(lines)


def _build_owner_direct_booking_flow_summary(user):
    active_qs = Accomodation.objects.filter(owner=user, is_active=True)
    if not active_qs.exists():
        return (
            "I couldn't find an active accommodation listing under your owner account yet.\n"
            "Register your accommodation first in Owner Hub."
        )

    accepted_qs = active_qs.filter(approval_status="accepted")
    pending_qs = active_qs.filter(approval_status="pending")
    declined_qs = active_qs.filter(approval_status="declined")
    accepted_rooms = Room.objects.filter(accommodation__in=accepted_qs)
    publicly_bookable_rooms = accepted_rooms.filter(
        status="AVAILABLE",
        current_availability__gte=1,
    )

    lines = [
        "Owner direct-booking workflow:",
        (
            f"- Listings: Accepted {accepted_qs.count()}, Pending {pending_qs.count()}, "
            f"Declined {declined_qs.count()}."
        ),
        (
            f"- Rooms under accepted listings: {accepted_rooms.count()} "
            f"(currently available: {publicly_bookable_rooms.count()})."
        ),
    ]
    if accepted_qs.exists() and publicly_bookable_rooms.exists():
        lines.append(
            "- Yes. Guests can book from your listing via the guest accommodation page and chatbot flow "
            "when your listing is accepted and room availability is set."
        )
    elif accepted_qs.exists() and not accepted_rooms.exists():
        lines.append(
            "- Guests cannot book yet because no rooms are registered under your accepted listing."
        )
    elif accepted_qs.exists():
        lines.append(
            "- Guests cannot book those rooms yet because they are not currently marked available."
        )
    else:
        lines.append(
            "- Guests cannot book your listing yet because there is no accepted listing currently visible."
        )
    lines.append(
        "Use Owner Hub and Manage Rooms to update listing status, room availability, and booking visibility."
    )
    return "\n".join(lines)


def _is_booking_count_command(message):
    text = str(message or "").strip().lower()
    if "booking" not in text:
        return False
    if any(token in text for token in ("all time", "all-time", "overall", "lifetime")):
        return True
    if any(token in text for token in ("how many", "count", "total", "number of", "pila", "ilan")):
        return True
    return bool(re.search(r"\bbookings?\b.*\b(today|this month|monthly|daily|now|all time|all-time|overall|lifetime)\b", text))


def _is_admin_pending_accommodations_command(message):
    return _contains_any_phrase(
        message,
        (
            "pending accommodations",
            "pending accommodation",
            "show pending hotels",
            "show pending inns",
            "accommodation approvals",
            "pending hotel registrations",
            "pending inns",
            "pending accom",
            "pakita pending accommodations",
        ),
    )


def _is_admin_pending_owner_accounts_command(message):
    return _contains_any_phrase(
        message,
        (
            "pending owner accounts",
            "pending owners",
            "owner account approvals",
            "pending accommodation owners",
            "accommodation owner approvals",
            "owner approvals",
            "pending owner",
            "pakita pending owners",
        ),
    )


def _is_admin_accommodation_bookings_command(message):
    return _contains_any_phrase(
        message,
        (
            "open accommodation links",
            "show accommodation links",
            "hotel bookings",
            "inn bookings",
            "room bookings",
            "accommodation booking list",
            "accommodation reservations",
            "hotel reservations",
            "open reservations",
            "pakita accommodation links",
        ),
    )


def _is_admin_tourism_manage_command(message):
    return _contains_any_phrase(
        message,
        (
            "open tourism information",
            "tourism information manage",
            "manage tourism information",
            "tourism management",
            "manage tourism spots",
            "open tourism spots",
            "pakita tourism information",
        ),
    )


def _is_admin_survey_results_command(message):
    return _contains_any_phrase(
        message,
        (
            "survey results",
            "open survey results",
            "show survey results",
            "sus results",
            "tam results",
            "rq4 results",
            "usability results",
            "acceptance results",
            "pakita survey",
        ),
    )


def _is_admin_map_command(message):
    return _contains_any_phrase(
        message,
        (
            "open map",
            "show map",
            "city map",
            "tourist map",
            "open city map",
            "open tourist map",
            "pakita map",
        ),
    )


def _is_admin_discounts_command(message):
    return _contains_any_phrase(
        message,
        (
            "open discounts",
            "show discounts",
            "manage discounts",
            "discount page",
            "discount management",
            "pakita discounts",
        ),
    )


def _is_admin_tour_list_command(message):
    return _contains_any_phrase(
        message,
        (
            "open tour list",
            "show tour list",
            "tour list",
            "manage tours",
            "tours page",
            "pakita tours",
        ),
    )


def _is_admin_activity_logs_command(message):
    return _contains_any_phrase(
        message,
        (
            "open activity logs",
            "show activity logs",
            "activity tracker",
            "user activity tracker",
            "open activity tracker",
            "pakita activity logs",
        ),
    )


def _is_admin_traveler_surveys_command(message):
    return _contains_any_phrase(
        message,
        (
            "open traveler surveys",
            "show traveler surveys",
            "traveler surveys",
            "tourism reports dashboard",
            "survey dashboard",
            "pakita traveler surveys",
        ),
    )


def _is_admin_tour_calendar_command(message):
    return _contains_any_phrase(
        message,
        (
            "open tour calendar",
            "show tour calendar",
            "tour calendar",
            "calendar page",
            "pakita calendar",
        ),
    )


def _is_employee_assigned_tours_command(message):
    return _contains_any_phrase(
        message,
        (
            "open assigned tours",
            "my assigned tours",
            "assigned tours",
            "show my assigned tours",
            "what tour am i assigned",
            "which tour am i assigned",
            "what tour package am i assigned",
            "tour package am i assigned",
            "assigned tour package",
            "assigned in",
            "assigned to",
            "ano ang assigned tour ko",
            "ano ang tour na assigned sa akin",
            "ano ang tour package na assigned sa akin",
            "ano yung tour package na assign sakin",
            "ano po yung tour package na assign sakin",
            "tour package na assign sakin",
            "unsa akong assigned tour",
            "unsa nga tour ang assigned nako",
            "unsa akong assigned tour package",
            "tasks for tours",
            "what are my tasks today",
            "my tasks today",
            "tasks today",
            "pakita assigned tours",
        ),
    )


def _is_employee_open_assignment_command(message):
    text = _normalize_chat_text(message)
    if not text:
        return False
    if re.search(r"\bopen\b.*\bassignment\b", text):
        return True
    if re.search(r"\bassignment\b.*\b(open|details?)\b", text):
        return True
    if re.search(r"\b(open|view)\s+\w+\s+\d{1,2}\s+assignment\b", text):
        return True
    if re.search(r"\bopen\b.*\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{1,2}\b.*\bone\b", text):
        return True
    return False


def _is_employee_assignment_update_command(message):
    text = _normalize_chat_text(message)
    if not text:
        return False
    if bool(re.fullmatch(r"\s*(?:ok(?:ay)?\s+)?(accept|decline)\s*(?:that|this|it)?\s*", text)):
        return True
    if not bool(re.search(r"\b(accept|decline)\b", text)):
        return False
    if "assignment" in text:
        return True
    return bool(re.search(r"\b(that|this|it)\b", text))


def _resolve_employee_record(request, actor):
    employee = actor.get("employee")
    if employee is None:
        employee_id = request.session.get("employee_id") if hasattr(request, "session") else None
        if employee_id:
            employee = Employee.objects.filter(emp_id=employee_id).first()
    return employee


def _resolve_employee_assignment_row(request, actor, sched_id_hint=""):
    assignment, _ = _resolve_employee_assignment_row_with_meta(
        request,
        actor,
        sched_id_hint=sched_id_hint,
        target_date=None,
    )
    return assignment


def _resolve_employee_assignment_row_with_meta(request, actor, sched_id_hint="", target_date=None):
    employee = _resolve_employee_record(request, actor)
    if employee is None:
        return None, "none"
    qs = (
        TourAssignment.objects.select_related("schedule", "schedule__tour")
        .filter(employee=employee)
        .order_by("-assigned_date", "-id")
    )
    if not qs.exists():
        return None, "none"
    sched_id = str(sched_id_hint or "").strip()
    if sched_id:
        picked = qs.filter(schedule__sched_id__iexact=sched_id).first()
        if picked is not None:
            return picked, "sched_id_exact"

    if target_date is not None:
        exact_date_pick = qs.filter(schedule__start_time__date=target_date).order_by("schedule__start_time", "-id").first()
        if exact_date_pick is not None:
            return exact_date_pick, "date_exact"

        assignment_rows = [row for row in qs[:50] if getattr(row, "schedule", None) is not None]
        nearest_row = None
        nearest_diff = None
        for row in assignment_rows:
            schedule = getattr(row, "schedule", None)
            schedule_start = getattr(schedule, "start_time", None) if schedule is not None else None
            if schedule_start is None:
                continue
            schedule_date = timezone.localtime(schedule_start).date()
            diff = abs((schedule_date - target_date).days)
            if nearest_diff is None or diff < nearest_diff:
                nearest_row = row
                nearest_diff = diff
        if nearest_row is not None:
            return nearest_row, "date_nearest"

    return qs.first(), "latest"

def _build_employee_assigned_tours_summary(request, actor, *, max_rows=5):
    employee = actor.get("employee")
    if employee is None:
        employee_id = request.session.get("employee_id") if hasattr(request, "session") else None
        if employee_id:
            employee = Employee.objects.filter(emp_id=employee_id).first()
    if employee is None:
        return (
            "I can open your assigned tours page, but I could not resolve your employee profile in this session.\n"
            "Please open Assigned Tours from the dashboard."
        )

    assignments = list(
        TourAssignment.objects.select_related("schedule", "schedule__tour")
        .filter(employee=employee)
        .order_by("-assigned_date", "-id")
    )
    if not assignments:
        return (
            "You currently have no assigned tours.\n"
            "Once an admin assigns a tour schedule to your account, it will appear here."
        )

    now = timezone.now()
    active_count = 0
    upcoming_count = 0
    lines = [f"Assigned tours for {getattr(employee, 'first_name', 'Employee')}:"]
    for assignment in assignments:
        schedule = getattr(assignment, "schedule", None)
        if schedule is None:
            continue
        start_time = getattr(schedule, "start_time", None)
        end_time = getattr(schedule, "end_time", None)
        if start_time and end_time and start_time <= now <= end_time:
            active_count += 1
        elif start_time and start_time > now:
            upcoming_count += 1

    lines.append(f"- Total assigned: {len(assignments)} | Active now: {active_count} | Upcoming: {upcoming_count}")
    lines.append("Latest assignments:")

    shown = 0
    for assignment in assignments:
        if shown >= max_rows:
            break
        schedule = getattr(assignment, "schedule", None)
        if schedule is None:
            continue
        tour = getattr(schedule, "tour", None)
        tour_name = str(getattr(tour, "tour_name", "") or "Tour").strip()
        sched_id = str(getattr(schedule, "sched_id", "") or "").strip()
        start_time = getattr(schedule, "start_time", None)
        start_text = timezone.localtime(start_time).strftime("%b %d, %Y %I:%M %p") if start_time else "No start time"
        status = str(getattr(schedule, "status", "") or "").strip().lower() or "unknown"
        lines.append(f"- {tour_name} ({sched_id}) | {start_text} | Status: {status.title()}")
        shown += 1

    return "\n".join(lines)


def _is_employee_tour_calendar_command(message):
    return _contains_any_phrase(
        message,
        (
            "open tour calendar",
            "my tour calendar",
            "tour calendar",
            "show schedule",
            "show my schedule",
            "pakita calendar",
        ),
    )


def _is_employee_accommodations_command(message):
    return _contains_any_phrase(
        message,
        (
            "open accommodations",
            "employee accommodations",
            "accommodation list",
            "show accommodations",
            "list accommodations",
            "pakita accommodations",
        ),
    )


def _is_employee_profile_command(message):
    return _contains_any_phrase(
        message,
        (
            "open profile",
            "my profile",
            "employee profile",
            "show profile",
            "account profile",
            "pakita profile",
        ),
    )


def _is_employee_tour_list_command(message):
    return _contains_any_phrase(
        message,
        (
            "open tour list",
            "show tour list",
            "tour list",
            "list tours",
            "pakita tours",
        ),
    )


def _is_employee_create_tour_command(message):
    return _contains_any_phrase(
        message,
        (
            "open create tour",
            "create tour",
            "add new tour",
            "new tour",
            "open add tour",
            "pakita create tour",
        ),
    )


def _is_employee_map_command(message):
    return _contains_any_phrase(
        message,
        (
            "open map",
            "show map",
            "city map",
            "tourist map",
            "pakita map",
        ),
    )


def _detect_employee_support_topic(message):
    text = str(message or "").strip().lower()
    if not text:
        return ""
    compact_text = re.sub(r"[^a-z0-9\s]", " ", text)
    compact_text = re.sub(r"\s+", " ", compact_text).strip()

    if _contains_any_phrase(text, ("forgot my password", "forgot password", "reset password", "cannot access the employee dashboard", "dashboard not opening", "dashboard not opening", "nakalimutan ko password", "dili ma open dashboard")):
        return "employee_account_access_help"
    if _contains_any_phrase(text, ("record not showing", "not showing in the system", "hindi lumalabas ang tourist record", "wala nagpakita ang tourist record")):
        return "employee_record_visibility_issue"
    if _contains_any_phrase(text, ("monitoring dashboard", "go to monitoring dashboard", "monitoring panel", "monitoring dashboard sa employee")):
        return "employee_monitoring_dashboard_help"
    if _contains_any_phrase(text, ("can i update records from my account", "update records from my account", "pwede ba ako mag update ng records", "pwede ba ko mag update og records")):
        return "employee_record_update_help"
    if _contains_any_phrase(
        text,
        (
            "how to manage tourist records",
            "how do i manage tourist records",
            "process tourist records",
            "workflow for tourist records",
            "unsaon pag manage sa tourist records",
            "paano i-manage ang tourist records",
        ),
    ):
        return "employee_tourist_records_workflow_help"

    if _contains_any_phrase(text, ("tourist records", "current tourists", "tourist arrivals", "tourist information records", "tourist by name", "tourist record", "records ng tourist", "listahan sa turista")):
        return "employee_tourist_monitoring"
    if _contains_any_phrase(text, ("all bookings in the system", "pending reservations", "bookings are confirmed", "cancelled reservations", "monitor accommodation bookings", "pending bookings", "monitor bookings", "bantayan ang bookings")):
        return "employee_booking_monitoring"
    if _contains_any_phrase(text, ("registered accommodations", "hotels or inns are active", "accommodation details", "accommodation records", "active hotels", "active inns", "mga active na hotel", "aktibong inns")):
        return "employee_accommodation_records"
    if _contains_any_phrase(text, ("update tourism destination details", "manage published attractions", "feedback or tourism concerns", "tourism concerns", "manage tourism info", "feedback sa turismo")):
        return "employee_workflow_destination_feedback"
    if _contains_any_phrase(text, ("tourism destination records", "destination is published", "published attractions", "tourism destination", "mga destination records", "published destinations")):
        return "employee_destination_records"
    if _contains_any_phrase(text, ("generate tourism reports", "booking summaries", "tourist statistics", "monitoring reports", "accommodation-related reports", "generate report", "accommodation reports", "gumawa ng report", "himo ug report")):
        return "employee_reports_support"
    if _contains_any_phrase(text, ("approve accommodation registrations", "review submitted accommodation listings", "review ang accommodation listing", "listing approval", "suriin ang listing approval", "review sa listing")):
        return "employee_workflow_listing_review"

    short_map = {
        "tourist records": "employee_tourist_monitoring",
        "pending bookings": "employee_booking_monitoring",
        "generate report": "employee_reports_support",
        "listing approval": "employee_workflow_listing_review",
        "dashboard not opening": "employee_account_access_help",
    }
    return short_map.get(compact_text, "")


def _build_employee_tourist_monitoring_summary(message):
    text = str(message or "").strip().lower()
    tourist_qs = Guest.objects.filter(is_active=True)
    total_tourists = tourist_qs.count()
    today = timezone.localdate()
    arrival_today = Pending.objects.count()
    active_bookings_today = TourBooking.objects.filter(booking_date__date=today).count()

    name_match = re.search(r"(?:name|named)\s+([a-zA-Z][a-zA-Z\s\-]{1,60})", text)
    if name_match:
        name_query = str(name_match.group(1) or "").strip()
        matches = tourist_qs.filter(
            Q(first_name__icontains=name_query)
            | Q(last_name__icontains=name_query)
            | Q(username__icontains=name_query)
        )[:5]
        if not matches:
            return (
                f"No tourist record matched '{name_query}'.\n"
                "Try searching by first name, last name, or username."
            )
        lines = [f"I found {len(matches)} tourist record(s) matching '{name_query}':"]
        for guest in matches:
            lines.append(f"- {guest.first_name} {guest.last_name} ({guest.username})")
        return "\n".join(lines)

    return (
        "Tourist monitoring snapshot:\n"
        f"- Active tourist records: {total_tourists}\n"
        f"- Tourist arrival entries today: {arrival_today}\n"
        f"- Tour booking records today: {active_bookings_today}\n"
        "For name search, ask: search tourist by name <name>."
    )


def _build_employee_booking_monitoring_summary():
    today = timezone.localdate()
    accom_qs = AccommodationBooking.objects.all()
    tour_rollup = _build_tour_booking_rollup(period="all_time", today=today)
    accom_counts = Counter(str(v or "").strip().lower() for v in accom_qs.values_list("status", flat=True))
    lines = [
        "Booking monitoring snapshot:",
        (
            f"- Accommodation bookings: total {accom_qs.count()}, "
            f"pending {accom_counts.get('pending', 0)}, confirmed {accom_counts.get('confirmed', 0)}, "
            f"declined {accom_counts.get('declined', 0)}, cancelled {accom_counts.get('cancelled', 0)}."
        ),
        (
            f"- Tour bookings: total {tour_rollup.get('total', 0)}, "
            f"pending {tour_rollup.get('pending', 0)}, active {tour_rollup.get('active', 0)}, "
            f"completed {tour_rollup.get('completed', 0)}, cancelled {tour_rollup.get('cancelled', 0)}."
        ),
        (
            f"- New accommodation bookings today ({today.isoformat()}): "
            f"{accom_qs.filter(booking_date__date=today).count()}"
        ),
    ]
    if tour_rollup.get("includes_pending_legacy") and int(tour_rollup.get("pending_legacy_count") or 0) > 0:
        lines.append(
            "Note: Tour totals include legacy pending-booking records to align with the booking report page."
        )
    return "\n".join(lines)


def _build_employee_accommodation_records_summary():
    accom_qs = Accomodation.objects.filter(is_active=True)
    total = accom_qs.count()
    accepted = accom_qs.filter(approval_status="accepted")
    pending = accom_qs.filter(approval_status="pending")
    declined = accom_qs.filter(approval_status="declined")
    rooms_total = Room.objects.filter(accommodation__in=accepted).count()
    return (
        "Accommodation records snapshot:\n"
        f"- Total active listings: {total}\n"
        f"- Accepted: {accepted.count()} | Pending: {pending.count()} | Declined: {declined.count()}\n"
        f"- Rooms under accepted listings: {rooms_total}"
    )


def _build_employee_destination_records_summary():
    all_dest = TourismInformation.objects.filter(is_active=True)
    published = all_dest.filter(publication_status="published")
    draft = all_dest.filter(publication_status="draft")
    archived = all_dest.filter(publication_status="archived")
    return (
        "Tourism destination records snapshot:\n"
        f"- Total active destination records: {all_dest.count()}\n"
        f"- Published: {published.count()} | Draft: {draft.count()} | Archived: {archived.count()}"
    )


def _build_employee_reports_support_summary():
    today = timezone.localdate()
    accom_bookings = AccommodationBooking.objects.all()
    tourism_records = TourismInformation.objects.published().count()
    tour_rollup = _build_tour_booking_rollup(period="all_time", today=today)
    lines = [
        "Monitoring and reports snapshot:",
        f"- Tour bookings total: {tour_rollup.get('total', 0)}",
        f"- Accommodation bookings total: {accom_bookings.count()}",
        f"- Published tourism destinations: {tourism_records}",
        (
            f"- Records updated today ({today.isoformat()}): "
            f"{TourismInformation.objects.filter(updated_at__date=today).count()}"
        ),
    ]
    if tour_rollup.get("includes_pending_legacy") and int(tour_rollup.get("pending_legacy_count") or 0) > 0:
        lines.append(
            "Note: Tour totals include legacy pending-booking records to align with the booking report page."
        )
    return "\n".join(lines)


def _is_guest_map_command(message):
    return _contains_any_phrase(
        message,
        (
            "open map",
            "show map",
            "city map",
            "tourist map",
            "bayawan map",
            "open city map",
            "open tourist map",
            "show bayawan map",
            "pakita map",
        ),
    )


def _extract_sched_id_from_message(message):
    text = str(message or "").strip()
    if not text:
        return ""
    match = re.search(r"(sched\d+)", text, flags=re.IGNORECASE)
    if not match:
        return ""
    return str(match.group(1) or "").strip()


def _extract_tour_selection_index(message):
    text = str(message or "").strip().lower()
    if not text:
        return 0

    ordinal_map = {
        "first": 1,
        "1st": 1,
        "second": 2,
        "2nd": 2,
        "third": 3,
        "3rd": 3,
        "fourth": 4,
        "4th": 4,
        "fifth": 5,
        "5th": 5,
    }
    for token, idx in ordinal_map.items():
        if re.search(rf"\b{re.escape(token)}\b", text):
            return idx
    if re.search(r"\bthat one\b", text) or re.search(r"\bthat tour\b", text):
        return 1

    # Accept "book #1", "book number 2", "book option 3", or plain "book 1".
    idx_match = re.search(
        r"\b(?:book|reserve|reservation)\b(?:\s+(?:#|number|no\.?|option|tour))?\s*(\d{1,2})\b",
        text,
        flags=re.IGNORECASE,
    )
    if idx_match:
        return _to_int(idx_match.group(1), default=0)

    return 0


def _is_guest_tour_interest_followup(message):
    text = _normalize_chat_text(message)
    if not text:
        return False
    if not any(token in text for token in ("tour", "trail", "highlights", "falls", "package")):
        return False
    interest_markers = (
        "looks nice",
        "looks good",
        "i like",
        "id like",
        "i want",
        "that one",
        "the first one",
        "book that",
    )
    return any(marker in text for marker in interest_markers)


def _extract_tour_interest_hint(message):
    text = str(message or "").strip()
    if not text:
        return ""
    normalized = re.sub(r"\s+", " ", text).strip()
    match = re.search(
        r"\b(?:that|the)?\s*([a-z0-9][a-z0-9\s\-]{2,80})\s+(?:tour|trail|package)\b",
        normalized,
        flags=re.IGNORECASE,
    )
    if match:
        return str(match.group(1) or "").strip(" .,!?:;")
    # Fallback to tour-name matcher using the full phrase.
    for tour in Tour_Add.objects.filter(publication_status="published").order_by("tour_name")[:30]:
        tour_name = str(getattr(tour, "tour_name", "") or "").strip()
        if not tour_name:
            continue
        if _normalize_chat_text(tour_name) in _normalize_chat_text(text):
            return tour_name
    return ""


def _extract_numeric_option_index(message):
    text = str(message or "").strip()
    if not re.fullmatch(r"\d{1,2}", text):
        return 0
    value = _to_int(text, default=0)
    return value if value > 0 else 0


def _extract_why_option_index(message):
    text = str(message or "").strip().lower()
    if not text:
        return 0
    if not ("why" in text or "explain" in text or "reason" in text):
        return 0
    match = re.search(r"\b(?:option|room|hotel|inn|#)\s*(\d{1,2})\b", text)
    if match:
        return _to_int(match.group(1), default=0)
    plain = re.search(r"\bwhy\s+(\d{1,2})\b", text)
    if plain:
        return _to_int(plain.group(1), default=0)
    return 0


def _extract_compare_top_n(message, default=3):
    text = str(message or "").strip().lower()
    if not text:
        return 0
    if not any(token in text for token in ("compare", "comparison")):
        return 0
    match = re.search(r"\btop\s*(\d{1,2})\b", text)
    if match:
        return max(2, min(_to_int(match.group(1), default=default), 5))
    num_match = re.search(r"\b(\d{1,2})\b", text)
    if num_match:
        return max(2, min(_to_int(num_match.group(1), default=default), 5))
    return default


def _merge_quick_replies(*reply_lists, limit=4):
    combined = []
    seen_values = set()
    for reply_list in reply_lists:
        if not isinstance(reply_list, list):
            continue
        for item in reply_list:
            if isinstance(item, dict):
                value = str(item.get("value") or "").strip()
                label = str(item.get("label") or value).strip()
                if not value:
                    continue
                dedupe_key = value.lower()
                if dedupe_key in seen_values:
                    continue
                seen_values.add(dedupe_key)
                combined.append({"label": label, "value": value})
            else:
                value = str(item or "").strip()
                if not value:
                    continue
                dedupe_key = value.lower()
                if dedupe_key in seen_values:
                    continue
                seen_values.add(dedupe_key)
                combined.append(value)
    return _sanitize_quick_replies(combined, limit=limit)


def _slot_quick_replies(slot_name):
    slot = str(slot_name or "").strip().lower()
    if slot == "total_budget":
        return [
            {"label": "PHP 5000", "value": "budget 5000"},
            {"label": "PHP 8000", "value": "budget 8000"},
            {"label": "PHP 10000", "value": "budget 10000"},
        ]
    if slot == "duration_days":
        return [
            {"label": "2 days", "value": "2 days"},
            {"label": "3 days", "value": "3 days"},
            {"label": "4 days", "value": "4 days"},
        ]
    if slot == "party_type":
        return [
            {"label": "Solo", "value": "solo"},
            {"label": "Couple", "value": "couple"},
            {"label": "Family", "value": "family"},
            {"label": "Group", "value": "group"},
        ]
    if slot == "accommodation_needed":
        return [
            {"label": "Include Accommodation", "value": "with accommodation"},
            {"label": "Activities Only", "value": "no accommodation needed"},
        ]
    if slot == "experience_style":
        return [
            {"label": "Relaxing", "value": "relaxing"},
            {"label": "Adventure", "value": "adventure"},
            {"label": "Cultural", "value": "cultural"},
            {"label": "Mixed", "value": "mixed"},
        ]
    if slot == "company_type":
        return [
            {"label": "Hotel", "value": "hotel"},
            {"label": "Inn", "value": "inn"},
            {"label": "Either", "value": "either"},
        ]
    if slot == "location":
        return [
            {"label": "Bayawan", "value": "bayawan"},
            {"label": "Poblacion", "value": "poblacion"},
            {"label": "Villareal", "value": "villareal"},
            {"label": "Suba", "value": "suba"},
        ]
    if slot == "guests":
        return [
            {"label": "1 Guest", "value": "1 guest"},
            {"label": "2 Guests", "value": "2 guests"},
            {"label": "4 Guests", "value": "4 guests"},
        ]
    if slot == "budget":
        return [
            {"label": "PHP 1000", "value": "budget 1000"},
            {"label": "PHP 1500", "value": "budget 1500"},
            {"label": "PHP 2000", "value": "budget 2000"},
        ]
    if slot == "stay_details":
        return [
            {"label": "2 Nights", "value": "2 nights"},
            {"label": "3 Nights", "value": "3 nights"},
        ]
    return []


def _build_recommendation_assist_quick_replies(cached_rows):
    return []


def _build_post_compare_quick_replies(cached_rows, top_n=3):
    return []


def _build_why_option_text(cached_rows, option_index):
    if not isinstance(cached_rows, list) or option_index <= 0:
        return ""
    selected = None
    for row in cached_rows:
        if not isinstance(row, dict):
            continue
        if _to_int(row.get("rank"), default=0) == option_index:
            selected = row
            break
    if not selected:
        return ""
    title = str(selected.get("title") or f"Option {option_index}").strip()
    subtitle = str(selected.get("subtitle") or "").strip()
    match_strength = str(selected.get("match_strength") or "").strip()
    reasons = selected.get("reasons") if isinstance(selected.get("reasons"), list) else []
    lines = [f"Why Option {option_index}: {title}"]
    if subtitle:
        lines.append(subtitle)
    if match_strength:
        lines.append(f"Match strength: {match_strength}")
    if reasons:
        lines.append("Primary matching reasons:")
        for reason in reasons[:4]:
            lines.append(f"- {str(reason)}")
    else:
        lines.append("Share your priority (budget, location, amenities, or guest count) for a more specific explanation.")
    return "\n".join(lines)


def _build_compare_options_text(cached_rows, top_n):
    if not isinstance(cached_rows, list) or len(cached_rows) < 2:
        return ""
    n = max(2, min(_to_int(top_n, default=3), 5))
    rows = [row for row in cached_rows if isinstance(row, dict)]
    if len(rows) < 2:
        return ""
    rows = rows[:n]
    lines = [f"Comparison of Top {len(rows)} options:"]
    for row in rows:
        rank = _to_int(row.get("rank"), default=0)
        title = str(row.get("title") or "").strip()
        subtitle = str(row.get("subtitle") or "").strip()
        match_strength = str(row.get("match_strength") or "").strip()
        reasons = row.get("reasons") if isinstance(row.get("reasons"), list) else []
        lines.append(f"{rank}. {title}")
        if subtitle:
            lines.append(f"   Details: {subtitle}")
        if match_strength:
            lines.append(f"   Match strength: {match_strength}")
        if reasons:
            lines.append(f"   Key reason: {str(reasons[0])}")
    lines.append("Next step: reply with 'why option <number>' for a detailed explanation.")
    return "\n".join(lines)


def _build_accommodation_selection_cache(items):
    if not isinstance(items, list):
        return []
    rows = []
    for idx, item in enumerate(items, 1):
        if not isinstance(item, dict):
            continue
        room_id = _to_int(item.get("room_id"), default=0)
        accom_id = _to_int(item.get("accom_id"), default=0)
        item_type = str(item.get("item_type") or item.get("kind") or ("room" if room_id > 0 else "accommodation")).strip().lower()
        if room_id <= 0 and accom_id <= 0:
            continue
        rank = _to_int(item.get("rank"), default=idx)
        rows.append(
            {
                "rank": rank if rank > 0 else idx,
                "room_id": room_id,
                "accom_id": accom_id,
                "item_type": item_type,
                "accommodation_name": str(item.get("accommodation_name") or "").strip()[:120],
                "title": str(item.get("title") or "").strip()[:120],
                "subtitle": str(item.get("subtitle") or "").strip()[:240],
                "match_strength": str(item.get("match_strength") or "").strip()[:24],
                "reasons": (
                    [str(reason).strip()[:160] for reason in item.get("reasons", []) if str(reason).strip()][:5]
                    if isinstance(item.get("reasons"), list)
                    else []
                ),
            }
        )
    return rows[:10]


def _resolve_accommodation_room_from_selection(selection_rows, selection_index):
    if not isinstance(selection_rows, list) or selection_index <= 0:
        return 0
    for row in selection_rows:
        if not isinstance(row, dict):
            continue
        if _to_int(row.get("rank"), default=0) == selection_index:
            return _to_int(row.get("room_id"), default=0)
    return 0


def _is_accommodation_detail_query(message):
    text = str(message or "").strip().lower()
    if not text:
        return False
    if any(token in text for token in ("why option", "compare top", "book option", "preview option")):
        return False
    if any(token in text for token in ("why", "explain", "reason")):
        return False
    detail_markers = (
        "amenity",
        "amenities",
        "facility",
        "facilities",
        "details",
        "tell me more",
        "more about",
        "what does",
        "show details",
    )
    subject_markers = (
        "option",
        "room",
        "hotel",
        "inn",
        "this hotel",
        "this room",
    )
    return any(marker in text for marker in detail_markers) and any(
        marker in text for marker in subject_markers
    )


def _extract_detail_option_index(message):
    text = str(message or "").strip().lower()
    if not text:
        return 0
    option_match = re.search(r"\boption\s*(\d{1,2})\b", text)
    if option_match:
        return _to_int(option_match.group(1), default=0)
    ordinal_match = re.search(r"\b(first|second|third|fourth|fifth)\s+option\b", text)
    if ordinal_match:
        ordinal_map = {
            "first": 1,
            "second": 2,
            "third": 3,
            "fourth": 4,
            "fifth": 5,
        }
        return _to_int(ordinal_map.get(str(ordinal_match.group(1) or "").lower()), default=0)
    return 0


def _extract_detail_room_id(message):
    text = str(message or "").strip().lower()
    if not text:
        return 0
    room_match = re.search(r"\broom(?:\s*id)?\s*[:#-]?\s*(\d{1,6})\b", text)
    if room_match:
        return _to_int(room_match.group(1), default=0)
    return 0


def _extract_detail_room_type_hint(message):
    text = str(message or "").strip().lower()
    if not text:
        return ""
    for token in ("standard", "deluxe", "matrimonial", "family", "suite", "single", "double", "twin"):
        if re.search(rf"\b{re.escape(token)}\b", text):
            return token
    return ""


def _extract_detail_accommodation_name_hint(message):
    text = _normalize_chat_text(message)
    if not text:
        return ""
    patterns = [
        r"\b(?:room|details?|show details|more about)\s+(?:in|for|at)\s+([a-z0-9][a-z0-9\s&\-\']{2,80})\b",
        r"\b(?:at|in)\s+([a-z0-9][a-z0-9\s&\-\']{2,80})\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if not match:
            continue
        candidate = " ".join(str(match.group(1) or "").split()).strip(" .,!?")
        if candidate:
            return candidate
    return ""


def _resolve_detail_room_from_hints(*, message="", cached_rows=None):
    text = str(message or "").strip().lower()
    cached_rows = cached_rows if isinstance(cached_rows, list) else []
    room_type_hint = _extract_detail_room_type_hint(text)
    accommodation_hint = _extract_detail_accommodation_name_hint(message)

    if cached_rows:
        for row in cached_rows:
            if not isinstance(row, dict):
                continue
            title = str(row.get("title") or "").strip().lower()
            room_name = title.split(" - ", 1)[1].strip() if " - " in title else ""
            if room_type_hint and room_type_hint not in room_name:
                continue
            if accommodation_hint and accommodation_hint.lower() not in title:
                continue
            room_id = _to_int(row.get("room_id"), default=0)
            if room_id > 0:
                return room_id

    qs = Room.objects.select_related("accommodation").filter(
        accommodation__approval_status="accepted",
        accommodation__is_active=True,
    )
    if accommodation_hint:
        qs = qs.filter(accommodation__company_name__icontains=accommodation_hint)
    if room_type_hint:
        qs = qs.filter(room_name__icontains=room_type_hint)
    if not room_type_hint and not accommodation_hint:
        return 0
    room = qs.order_by("price_per_night", "room_id").first()
    return _to_int(getattr(room, "room_id", 0), default=0)


def _normalize_amenities_for_display(raw_value):
    text = str(raw_value or "").strip()
    if not text:
        return []
    tokens = [
        str(item).strip()
        for item in re.split(r"[,\n;/|]+", text)
        if str(item).strip()
    ]
    deduped = []
    seen = set()
    for token in tokens:
        lowered = token.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        deduped.append(token)
    return deduped[:12]


def _extract_room_level_amenities_for_display(room):
    if room is None:
        return []
    details = getattr(room, "owner_details", None)
    if details is None:
        try:
            details = AuthoritativeRoomDetails.objects.filter(room=room).first()
        except Exception:
            details = None
    raw_value = getattr(details, "amenities", "") if details is not None else ""
    raw_text = str(raw_value or "").strip()
    if not raw_text:
        return []
    try:
        parsed = json.loads(raw_text)
    except Exception:
        parsed = None
    if isinstance(parsed, list):
        return _normalize_amenities_for_display(", ".join(str(item) for item in parsed))
    if isinstance(parsed, str):
        return _normalize_amenities_for_display(parsed)
    return _normalize_amenities_for_display(raw_text)


def _build_guest_room_detail_payload(message, cached_rows):
    text = str(message or "").strip().lower()
    option_index = _extract_detail_option_index(text)
    requested_room_id = _extract_detail_room_id(text)
    resolved_room_id = requested_room_id
    if resolved_room_id <= 0 and option_index > 0:
        resolved_room_id = _resolve_accommodation_room_from_selection(cached_rows, option_index)
    if resolved_room_id <= 0 and ("this hotel" in text or "this room" in text):
        first_row = cached_rows[0] if isinstance(cached_rows, list) and cached_rows else {}
        if isinstance(first_row, dict):
            resolved_room_id = _to_int(first_row.get("room_id"), default=0)
    if resolved_room_id <= 0:
        resolved_room_id = _resolve_detail_room_from_hints(message=message, cached_rows=cached_rows)

    if resolved_room_id <= 0:
        return {
            "fulfillmentText": (
                "I can show room details once you provide a room reference.\n"
                "Please share a room type (for example: Standard Twin Room or Deluxe Room), "
                "an accommodation name, or an option number from your latest recommendation list."
            ),
            "quick_replies": [
                "show details for standard room",
                "show details for deluxe room",
            ],
            "needs_clarification": True,
            "missing_slot": "room_reference",
        }

    room = (
        Room.objects.select_related("accommodation", "owner_details")
        .filter(
            room_id=resolved_room_id,
            accommodation__approval_status="accepted",
            accommodation__is_active=True,
        )
        .first()
    )
    if room is None:
        return {
            "fulfillmentText": (
                "I couldn't find that room in the currently accepted hotel/inn listings.\n"
                "Please try a room type (Standard/Deluxe/Matrimonial/Family) or specify the accommodation name."
            ),
            "quick_replies": [
                "show details for standard room",
                "show details for family room",
            ],
        }

    accom = getattr(room, "accommodation", None)
    room_level_amenities = _extract_room_level_amenities_for_display(room)
    accommodation_level_amenities = _normalize_amenities_for_display(
        getattr(accom, "accommodation_amenities", "") if accom is not None else ""
    )
    lines = [
        (
            f"Room details: {getattr(accom, 'company_name', 'Accommodation')} - "
            f"{room.room_name}"
        ),
        f"Location: {getattr(accom, 'location', '') or 'Not specified'}",
        f"Type: {str(getattr(accom, 'company_type', '') or 'Accommodation').title()}",
        f"Rate: PHP {Decimal(room.price_per_night):.2f} per night",
        f"Capacity: up to {_to_int(room.person_limit, default=0)} guest(s)",
        f"Current availability slots: {_to_int(room.current_availability, default=0)}",
        f"Room status: {str(room.status or 'unknown').title()}",
    ]
    if room_level_amenities:
        lines.append(f"Amenities listed: {', '.join(room_level_amenities)}")
        if accommodation_level_amenities:
            lines.append("Additional property amenities may also be available at the accommodation level.")
    elif accommodation_level_amenities:
        lines.append(
            "Amenities listed (accommodation-level): "
            + ", ".join(accommodation_level_amenities)
        )
    else:
        lines.append(
            "Amenities information is currently limited for this room/property in the current records."
        )
    description = str(getattr(accom, "description", "") or "").strip()
    if description:
        lines.append(f"Description: {description}")

    link_actions = _build_accommodation_link_actions(room=room, max_actions=5)
    link = ""
    link_label = ""
    if link_actions:
        first_action = link_actions[0] if isinstance(link_actions[0], dict) else {}
        link = str(first_action.get("url") or "")
        link_label = str(first_action.get("label") or "")
    quick_replies = [
        {"label": "View Details", "value": f"show details for {room.room_name}"},
        {"label": "Create Booking Preview", "value": f"create booking preview for {getattr(accom, 'company_name', 'this accommodation')}"},
        {"label": "Open Official Page", "value": "open official page"},
        {"label": "View Facebook Page", "value": "open facebook page"},
    ]
    payload = {
        "fulfillmentText": "\n".join(lines),
        "room_id": room.room_id,
        "quick_replies": quick_replies,
    }
    if link:
        payload["billing_link"] = link
        payload["billing_link_label"] = link_label or "Open Official Link"
    if link_actions:
        payload["link_actions"] = link_actions
    return payload


def _is_guest_tour_booking_command(message):
    text = str(message or "").strip().lower()
    if not text:
        return False
    # Guard: viewing/listing booking history should not trigger booking execution flow.
    if re.search(
        r"\b(show|view|check|list)\b.*\b(my\s+)?(tour\s+)?book(?:ing|ings|igns)\b",
        text,
    ):
        return False
    if not any(term in text for term in ("book", "reserve", "reservation")):
        return False
    # Keep accommodation flow isolated from tour booking.
    if re.search(r"\b(hotel|inn|accommodation|room|stay)\b", text):
        return False
    tour_terms = ("tour", "package", "schedule", "sched", "trail", "highlights", "nature")
    if any(term in text for term in tour_terms):
        return True
    # Support direct phrasing like "book bayawan food and culture trail".
    return bool(re.search(r"\b(book|reserve)\b\s+[a-z0-9][a-z0-9\s\-]{3,80}$", text))


def _extract_tour_name_booking_hint(message):
    text = str(message or "").strip()
    if not text:
        return ""
    normalized = re.sub(r"\s+", " ", text).strip()
    match = re.search(
        r"\b(?:book|reserve|reservation(?: for)?)\b\s+(.+)$",
        normalized,
        flags=re.IGNORECASE,
    )
    if not match:
        return ""
    raw_hint = str(match.group(1) or "").strip(" .,!?:;")
    # Remove trailing date/guest fragments when present.
    raw_hint = re.split(
        r"\b(?:for\s+\d+\s*(?:adult|adults|guest|guests|people|person|pax)|on\s+[a-z]+\s+\d{1,2}|on\s+\d{4}-\d{2}-\d{2}|for\s+[a-z]+\s+\d{1,2})\b",
        raw_hint,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0].strip(" .,!?:;")
    raw_hint = re.sub(
        r"\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\s+\d{1,2}(?:,\s*\d{4})?\b",
        "",
        raw_hint,
        flags=re.IGNORECASE,
    ).strip(" .,!?:;")
    if len(raw_hint) < 3:
        return ""
    return raw_hint


def _is_guest_view_tour_bookings_command(message):
    text = str(message or "").strip().lower()
    if not text:
        return False
    phrases = (
        "show my tour bookings",
        "view my tour bookings",
        "check my tour bookings",
        "my tour bookings",
        "show tour bookings",
        "view tour bookings",
        "show my tour bookigns",
        "show tour bookigns",
        "my tour bookigns",
        "show my bookings",
        "view my bookings",
        "check my bookings",
        "my bookings",
    )
    if any(phrase in text for phrase in phrases):
        return True
    return bool(
        re.search(
            r"\b(show|view|check|list)\b.*\b(my\s+)?(tour\s+)?book(?:ing|ings|igns)\b",
            text,
        )
    )


def _build_guest_tour_bookings_payload(request, user):
    upcoming_count = 0
    current_count = 0
    past_count = 0
    now = timezone.now()

    tour_bookings = TourBooking.objects.filter(guest=user).select_related("schedule")
    for booking in tour_bookings:
        schedule = getattr(booking, "schedule", None)
        if schedule is None:
            past_count += 1
            continue
        start_time = schedule.start_time
        end_time = schedule.end_time
        if not timezone.is_aware(start_time):
            start_time = timezone.make_aware(start_time)
        if not timezone.is_aware(end_time):
            end_time = timezone.make_aware(end_time)
        status = str(getattr(booking, "status", "") or "").strip().lower()
        if status == "cancelled":
            past_count += 1
        elif start_time > now:
            upcoming_count += 1
        elif start_time <= now <= end_time:
            current_count += 1
        else:
            past_count += 1

    pending_qs = Pending.objects.filter(guest_id=user).select_related("sched_id")
    for pending in pending_qs:
        schedule = getattr(pending, "sched_id", None)
        if schedule is None:
            past_count += 1
            continue
        start_time = schedule.start_time
        end_time = schedule.end_time
        if not timezone.is_aware(start_time):
            start_time = timezone.make_aware(start_time)
        if not timezone.is_aware(end_time):
            end_time = timezone.make_aware(end_time)
        status = str(getattr(pending, "status", "") or "").strip().lower()
        if status == "cancelled":
            past_count += 1
        elif start_time > now:
            upcoming_count += 1
        elif start_time <= now <= end_time:
            current_count += 1
        else:
            past_count += 1

    total_count = upcoming_count + current_count + past_count
    bookings_url = reverse("main-page") + "#myBookings"
    if hasattr(request, "build_absolute_uri"):
        bookings_url = request.build_absolute_uri(bookings_url)

    if total_count == 0:
        summary = _pick_response_variant(
            [
                "I couldn’t find any tour bookings linked to your account yet. You can browse schedules and book when ready.",
                "I don’t see any tour bookings in your account yet. You can check available schedules when you’re ready.",
                "No tour bookings are showing on your account yet. I can help you find available tours next.",
            ],
            seed_text=f"{user.pk}|guest-tour-bookings-empty",
        )
    else:
        summary = _pick_response_variant(
            [
                f"You currently have {total_count} tour booking(s): {upcoming_count} upcoming, {current_count} current, and {past_count} past.",
                f"Here’s your booking snapshot: {total_count} total tour booking(s), with {upcoming_count} upcoming, {current_count} current, and {past_count} past.",
                f"I found {total_count} tour booking(s) in your account: {upcoming_count} upcoming, {current_count} current, and {past_count} past.",
            ],
            seed_text=f"{user.pk}|guest-tour-bookings-summary",
        )
    return {
        "fulfillmentText": summary + "\nOpen your My Bookings section below.",
        "billing_link": bookings_url,
        "billing_link_label": "Open My Tour Bookings",
        "open_in_new_tab": True,
        "quick_replies": [
            "Show available tours",
            "How do I cancel a tour booking?",
            "Plan my 10k stay",
        ],
    }


def _extract_tour_date_hint_from_message(message):
    text = str(message or "").strip()
    if not text:
        return None

    iso_match = re.search(r"\b(20\d{2})-(0?[1-9]|1[0-2])-(0?[1-9]|[12]\d|3[01])\b", text)
    if iso_match:
        try:
            return datetime(
                int(iso_match.group(1)),
                int(iso_match.group(2)),
                int(iso_match.group(3)),
            ).date()
        except Exception:
            return None

    month_names = [m for m in calendar.month_name if m]
    month_pattern = "|".join(month_names)
    month_day = re.search(
        rf"\b({month_pattern})\s+([0-2]?\d|3[01])(?:,\s*(20\d{{2}}))?\b",
        text,
        flags=re.IGNORECASE,
    )
    if month_day:
        month_name = str(month_day.group(1) or "").strip().lower()
        day_val = _to_int(month_day.group(2), default=0)
        year_val = _to_int(month_day.group(3), default=timezone.localdate().year)
        month_val = 0
        for idx, value in enumerate(month_names, start=1):
            if value.lower() == month_name:
                month_val = idx
                break
        if month_val > 0 and day_val > 0:
            try:
                parsed = datetime(year_val, month_val, day_val).date()
            except Exception:
                return None
            today = timezone.localdate()
            if parsed < today and month_day.group(3) is None:
                try:
                    parsed = datetime(year_val + 1, month_val, day_val).date()
                except Exception:
                    pass
            return parsed
    return None


def _is_tour_schedule_request(message):
    text = _normalize_chat_text(message)
    if not text:
        return False
    markers = (
        "show tour schedules",
        "tour schedules",
        "show schedules",
        "view schedules",
        "available schedules",
        "when is",
        "available dates",
        "tours on ",
        "schedule for",
    )
    if any(marker in text for marker in markers):
        return True
    if "schedule" in text and any(token in text for token in ("tour", "trail", "highlights", "package", "date", "available")):
        return True
    return False


def _extract_tour_name_from_schedule_request(message):
    text = str(message or "").strip()
    if not text:
        return ""
    normalized = re.sub(r"\s+", " ", text).strip()
    patterns = (
        r"\b(?:schedule|schedules)\s+for\s+(.+)$",
        r"\bwhen\s+is\s+(.+?)\s+(?:available|open)\b",
        r"\bavailable\s+dates?\s+for\s+(.+)$",
    )
    for pattern in patterns:
        match = re.search(pattern, normalized, flags=re.IGNORECASE)
        if match:
            value = str(match.group(1) or "").strip(" .,!?:;")
            if value:
                return value
    for tour in Tour_Add.objects.filter(publication_status="published").order_by("tour_name")[:60]:
        name = str(getattr(tour, "tour_name", "") or "").strip()
        if name and _normalize_chat_text(name) in _normalize_chat_text(normalized):
            return name
    return ""


def _tour_primary_image_url(tour_obj):
    if tour_obj is None:
        return ""
    image_field = getattr(tour_obj, "image", None)
    if image_field:
        try:
            return str(image_field.url or "").strip()
        except Exception:
            return ""
    return ""


def _tour_detail_url(request, tour_obj, schedule=None):
    if tour_obj is None:
        return ""
    try:
        link = reverse("guest_book", kwargs={"tour_id": str(getattr(tour_obj, "tour_id", "") or "").strip()})
        sched_id = str(getattr(schedule, "sched_id", "") or "").strip() if schedule is not None else ""
        if sched_id:
            link = f"{link}?sched_id={sched_id}"
        if hasattr(request, "build_absolute_uri"):
            link = request.build_absolute_uri(link)
        return link
    except Exception:
        return ""


def _build_tour_card_trace(request, schedule):
    tour_obj = getattr(schedule, "tour", None)
    tour_name = str(getattr(tour_obj, "tour_name", "") or "").strip()
    if not tour_name:
        return {}
    sched_date = ""
    try:
        sched_date = timezone.localtime(schedule.start_time).strftime("%B %d, %Y")
    except Exception:
        sched_date = ""
    price_text = f"PHP {_to_decimal(getattr(schedule, 'price', 0), default=Decimal('0')):,.0f} per guest"
    duration_days = max(1, _to_int(getattr(schedule, "duration_days", 1), default=1))
    slots = max(0, _to_int(getattr(schedule, "slots_available", 0), default=0))
    description = str(getattr(tour_obj, "description", "") or "").strip()
    if len(description) > 170:
        description = description[:167].rstrip() + "..."
    return {
        "kind": "tour",
        "tour_id": str(getattr(tour_obj, "tour_id", "") or "").strip(),
        "sched_id": str(getattr(schedule, "sched_id", "") or "").strip(),
        "title": tour_name,
        "subtitle": f"{price_text} | {duration_days} day(s)",
        "description": (
            f"{description}\nNext available: {sched_date if sched_date else 'TBA'} | Slots: {slots}"
            if description
            else f"Next available: {sched_date if sched_date else 'TBA'} | Slots: {slots}"
        ),
        "image_url": _tour_primary_image_url(tour_obj),
        "detail_url": _tour_detail_url(request, tour_obj),
        "meta": {
            "sched_id": str(getattr(schedule, "sched_id", "") or "").strip(),
            "tour_id": str(getattr(tour_obj, "tour_id", "") or "").strip(),
        },
    }


def _build_schedule_card_trace(request, schedule):
    tour_obj = getattr(schedule, "tour", None)
    tour_name = str(getattr(tour_obj, "tour_name", "") or "").strip()
    if not tour_name:
        return {}
    try:
        start_local = timezone.localtime(schedule.start_time)
        end_local = timezone.localtime(schedule.end_time)
        date_text = start_local.strftime("%B %d, %Y")
        time_text = f"{start_local.strftime('%I:%M %p')} - {end_local.strftime('%I:%M %p')}"
    except Exception:
        date_text = ""
        time_text = ""
    slots = max(0, _to_int(getattr(schedule, "slots_available", 0), default=0))
    price_text = f"PHP {_to_decimal(getattr(schedule, 'price', 0), default=Decimal('0')):,.0f} per guest"
    subtitle_parts = [part for part in [date_text, time_text, price_text] if part]
    return {
        "kind": "tour_schedule",
        "tour_id": str(getattr(tour_obj, "tour_id", "") or "").strip(),
        "sched_id": str(getattr(schedule, "sched_id", "") or "").strip(),
        "title": tour_name,
        "subtitle": " | ".join(subtitle_parts),
        "description": f"Available slots: {slots}",
        "image_url": _tour_primary_image_url(tour_obj),
        "detail_url": _tour_detail_url(request, tour_obj, schedule=schedule),
        "meta": {
            "sched_id": str(getattr(schedule, "sched_id", "") or "").strip(),
            "tour_id": str(getattr(tour_obj, "tour_id", "") or "").strip(),
        },
    }


def _build_tour_schedule_listing_payload(request, message, params):
    params = params if isinstance(params, dict) else {}
    tour_name_hint = _extract_tour_name_from_schedule_request(message)
    date_hint = _extract_tour_date_hint_from_message(message)
    now = timezone.now()
    qs = (
        Tour_Schedule.objects.select_related("tour")
        .filter(tour__publication_status="published")
        .exclude(status="cancelled")
        .order_by("start_time")
    )
    if date_hint is not None:
        local_tz = timezone.get_current_timezone()
        day_start = timezone.make_aware(datetime(date_hint.year, date_hint.month, date_hint.day, 0, 0, 0), local_tz)
        day_end = day_start + timedelta(days=1)
        qs = qs.filter(start_time__gte=day_start, start_time__lt=day_end)
    else:
        qs = qs.filter(end_time__gte=now)
    if tour_name_hint:
        qs = qs.filter(tour__tour_name__icontains=tour_name_hint)
    rows = list(qs[:8])
    if not rows:
        if tour_name_hint:
            return {
                "reply": "There are no available schedules for this tour right now. You may choose another tour package.",
                "items": [],
                "quick_replies": ["show available tours", "recommend a tour"],
            }
        return {
            "reply": "There are no available tour schedules right now. You may choose another tour package.",
            "items": [],
            "quick_replies": ["show available tours", "recommend a tour"],
        }
    items = []
    sched_ids = []
    for sched in rows:
        card = _build_schedule_card_trace(request, sched)
        if card:
            items.append(card)
            sched_ids.append(str(getattr(sched, "sched_id", "") or "").strip())
    if not items:
        return {
            "reply": "There are no available tour schedules right now.",
            "items": [],
            "quick_replies": ["show available tours"],
        }
    if tour_name_hint:
        intro = f"Here are available schedules for {tour_name_hint}."
    elif date_hint is not None:
        intro = f"Here are available schedules on {date_hint.strftime('%B %d, %Y')}."
    else:
        intro = "Here are available tour schedules right now."
    return {
        "reply": intro,
        "items": items,
        "sched_ids": sched_ids,
        "quick_replies": ["Book this schedule", "show available tours"],
    }


def _resolve_schedule_for_tour_booking(*, tour_name_hint="", sched_id="", date_hint=None):
    normalized_sched_id = str(sched_id or "").strip()
    if normalized_sched_id:
        schedule = (
            Tour_Schedule.objects.select_related("tour")
            .filter(
                sched_id__iexact=normalized_sched_id,
                tour__publication_status="published",
            )
            .exclude(status="cancelled")
            .first()
        )
        if schedule is not None:
            return schedule, []

    qs = (
        Tour_Schedule.objects.select_related("tour")
        .filter(tour__publication_status="published")
        .exclude(status="cancelled")
        .annotate(assigned_count=Count("employee_assignments", distinct=True))
    )
    if str(tour_name_hint or "").strip():
        qs = qs.filter(tour__tour_name__icontains=str(tour_name_hint).strip())
    if date_hint is not None:
        local_tz = timezone.get_current_timezone()
        day_start = timezone.make_aware(datetime(date_hint.year, date_hint.month, date_hint.day, 0, 0, 0), local_tz)
        day_end = day_start + timedelta(days=1)
        day_rows = list(
            qs.filter(start_time__gte=day_start, start_time__lt=day_end)
            .order_by("-assigned_count", "start_time")[:6]
        )
        if len(day_rows) == 1:
            return day_rows[0], []
        if len(day_rows) > 1:
            return None, day_rows
        nearby = list(
            qs.filter(start_time__gte=day_start - timedelta(days=3), start_time__lt=day_end + timedelta(days=3))
            .order_by("-assigned_count", "start_time")[:6]
        )
        return None, nearby
    schedule = (
        qs.filter(end_time__gte=timezone.now(), assigned_count__gt=0)
        .order_by("start_time")
        .first()
    )
    if schedule is None:
        schedule = qs.filter(end_time__gte=timezone.now()).order_by("-assigned_count", "start_time").first()
    if schedule is None:
        schedule = qs.order_by("-assigned_count", "start_time").first()
    return schedule, []


def _build_tour_booking_summary_text(*, schedule, guests):
    tour_name = str(getattr(getattr(schedule, "tour", None), "tour_name", "") or "").strip() or "Selected Tour"
    try:
        date_text = timezone.localtime(schedule.start_time).strftime("%B %d, %Y")
    except Exception:
        date_text = "Selected date"
    rate = _to_decimal(getattr(schedule, "price", 0), default=Decimal("0"))
    total = rate * Decimal(max(1, guests))
    return (
        "Here is your tour booking summary:\n\n"
        f"Tour: {tour_name}\n"
        f"Schedule: {date_text}\n"
        f"Guests: {guests}\n"
        f"Rate: PHP {rate:,.0f} per guest\n"
        f"Estimated Total: PHP {total:,.0f}\n\n"
        "Reply YES to submit this booking request."
    )


def _submit_guest_tour_booking_request(request, user, *, schedule, guests):
    if schedule is None:
        return {"ok": False, "error": "Selected schedule is no longer available."}
    if guests <= 0:
        return {"ok": False, "error": "Please provide at least 1 guest to continue."}
    available_slots = _to_int(getattr(schedule, "slots_available", 0), default=0)
    if available_slots < guests:
        return {"ok": False, "error": "Not enough available slots for that schedule right now."}
    tour_obj = getattr(schedule, "tour", None)
    if tour_obj is None:
        return {"ok": False, "error": "Selected tour package is unavailable right now."}

    guest_name = f"{str(getattr(user, 'first_name', '') or '').strip()} {str(getattr(user, 'last_name', '') or '').strip()}".strip()
    if not guest_name:
        guest_name = str(getattr(user, "username", "") or "Guest")
    guest_email = str(getattr(user, "email", "") or "").strip()
    guest_phone = str(getattr(user, "phone_number", "") or "").strip() or "000-000-0000"

    assignment_count = 0
    with transaction.atomic():
        sched_locked = Tour_Schedule.objects.select_for_update().select_related("tour").filter(
            sched_id=getattr(schedule, "sched_id", ""),
            tour__publication_status="published",
        ).first()
        if sched_locked is None or str(getattr(sched_locked, "status", "") or "").lower() == "cancelled":
            return {"ok": False, "error": "That tour schedule is unavailable right now."}
        if _to_int(getattr(sched_locked, "slots_available", 0), default=0) < guests:
            return {"ok": False, "error": "Not enough available slots for that schedule right now."}
        pending = Pending.objects.create(
            guest_id=user,
            sched_id=sched_locked,
            tour_id=sched_locked.tour,
            status="Pending",
            total_guests=guests,
            your_name=guest_name,
            your_email=guest_email or "default@example.com",
            your_phone=guest_phone,
            num_adults=guests,
            num_children=0,
        )
        sched_locked.slots_booked = _to_int(getattr(sched_locked, "slots_booked", 0), default=0) + guests
        sched_locked.slots_available = _to_int(getattr(sched_locked, "slots_available", 0), default=0) - guests
        sched_locked.save(update_fields=["slots_booked", "slots_available"])
        assignment_count = TourAssignment.objects.filter(schedule=sched_locked).count()

    try:
        create_notification(
            recipient_guest=user,
            title="Tour booking submitted",
            message=f"Your booking request for {sched_locked.tour.tour_name} is pending staff review.",
            notification_type="booking",
            url=reverse("main-page") + "#user-bookings",
            dedupe_key=f"tour-pending-{pending.id}",
            related_object_id=str(pending.id),
        )
        notify_assigned_employees_for_schedule(
            schedule=sched_locked,
            title="New tour booking request",
            message=f"{sched_locked.tour.tour_name} received a new pending booking from {guest_name}.",
            notification_type="booking",
            url=reverse("tour_app:pending_view"),
            dedupe_key_prefix=f"tour-pending-{pending.id}",
        )
    except Exception:
        pass

    try:
        if guest_email:
            start_local = timezone.localtime(sched_locked.start_time)
            end_local = timezone.localtime(sched_locked.end_time)
            total_amount = _to_decimal(getattr(sched_locked, "price", 0), default=Decimal("0")) * Decimal(max(1, guests))
            send_mail(
                subject=f"Booking Request Received for {sched_locked.tour.tour_name}",
                message=(
                    f"Dear {guest_name},\n\n"
                    f"Your booking request is now pending review.\n\n"
                    f"Tour: {sched_locked.tour.tour_name}\n"
                    f"Schedule: {start_local.strftime('%B %d, %Y %I:%M %p')} to {end_local.strftime('%B %d, %Y %I:%M %p')}\n"
                    f"Guests: {guests}\n"
                    f"Estimated Total: PHP {total_amount:.2f}\n"
                    f"Status: Pending\n\n"
                    "We will notify you once tourism staff reviews your request.\n"
                ),
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[guest_email],
                fail_silently=False,
            )
    except Exception:
        pass
    return {
        "ok": True,
        "pending_id": getattr(pending, "id", None),
        "assignment_count": assignment_count,
        "sched_id": str(getattr(sched_locked, "sched_id", "") or ""),
    }


def _build_guest_tour_booking_link_payload(
    request,
    *,
    sched_id="",
    fallback_sched_ids=None,
    selection_index=0,
    user_message="",
):
    resolved_sched_id = str(sched_id or "").strip()
    tour_name_hint = _extract_tour_name_booking_hint(user_message)
    if not resolved_sched_id and isinstance(fallback_sched_ids, list):
        cleaned = [str(item).strip() for item in fallback_sched_ids if str(item).strip()]
        if selection_index > 0:
            pick = selection_index - 1
            if 0 <= pick < len(cleaned):
                resolved_sched_id = cleaned[pick]
            else:
                return {
                    "fulfillmentText": (
                        f"I couldn't find option #{selection_index} in your recent recommendations.\n"
                    f"Available options: {', '.join(cleaned[:5])}"
                    )
                }
        elif len(cleaned) == 1:
            resolved_sched_id = cleaned[0]
        elif len(cleaned) > 1:
            return {
                "fulfillmentText": (
                    "I can help you book right away. Please share the schedule ID you want.\n"
                    f"Available recent options: {', '.join(cleaned[:5])}\n"
                    "Example: book tour sched00001"
                )
            }

    if not resolved_sched_id and tour_name_hint:
        upcoming_named_qs = (
            Tour_Schedule.objects.select_related("tour")
            .filter(
                tour__publication_status="published",
                tour__tour_name__icontains=tour_name_hint,
                end_time__gte=timezone.now(),
            )
            .exclude(status="cancelled")
            .order_by("start_time")
        )
        if not upcoming_named_qs.exists():
            upcoming_named_qs = (
                Tour_Schedule.objects.select_related("tour")
                .filter(
                    tour__publication_status="published",
                    tour__tour_name__icontains=tour_name_hint,
                )
                .exclude(status="cancelled")
                .order_by("start_time")
            )
        if upcoming_named_qs.exists():
            resolved_sched_id = str(upcoming_named_qs.first().sched_id)

    if not resolved_sched_id:
        date_hint = _extract_tour_date_hint_from_message(user_message)
        if date_hint is not None:
            local_tz = timezone.get_current_timezone()
            day_start = timezone.make_aware(
                datetime(date_hint.year, date_hint.month, date_hint.day, 0, 0, 0),
                local_tz,
            )
            day_end = day_start + timedelta(days=1)
            date_qs = (
                Tour_Schedule.objects.select_related("tour")
                .filter(
                    start_time__gte=day_start,
                    start_time__lt=day_end,
                    tour__publication_status="published",
                )
                .order_by("start_time")
            )
            if date_qs.count() == 1:
                resolved_sched_id = str(date_qs.first().sched_id)
            elif date_qs.exists():
                options = [
                    f"{row.tour.tour_name} ({row.sched_id})"
                    for row in date_qs[:4]
                ]
                return {
                    "fulfillmentText": (
                        f"I found multiple tour schedules on {date_hint.strftime('%B %d, %Y')}:\n"
                        + "\n".join(f"- {item}" for item in options)
                        + "\nPlease pick one schedule ID so I can continue (example: book tour SCHED00001)."
                    )
                }
            else:
                nearby_qs = (
                    Tour_Schedule.objects.select_related("tour")
                    .filter(
                        start_time__gte=day_start - timedelta(days=3),
                        start_time__lt=day_end + timedelta(days=3),
                        tour__publication_status="published",
                    )
                    .order_by("start_time")
                )
                if nearby_qs.exists():
                    preview = [
                        f"{row.start_time.strftime('%b %d')} - {row.tour.tour_name} ({row.sched_id})"
                        for row in nearby_qs[:3]
                    ]
                    return {
                        "fulfillmentText": (
                            f"I couldn't find an exact schedule on {date_hint.strftime('%B %d, %Y')}, "
                            "but here are nearby options:\n"
                            + "\n".join(f"- {item}" for item in preview)
                            + "\nSend the schedule ID you prefer and I'll open the booking page."
                        )
                    }

    if not resolved_sched_id:
        if tour_name_hint:
            return {
                "fulfillmentText": (
                    "I couldn't find that tour right now. "
                    "You can try viewing current schedules or share a target date."
                ),
                "quick_replies": ["show available tours", "book tour for May 5", "help me choose a tour"],
            }
        return {
            "fulfillmentText": (
                "I can guide you to tour booking right away. Share a schedule ID or a target date.\n"
                "Example: book tour sched00001\n"
                "or: I want to book a tour package for April 25"
            )
        }

    schedule = (
        Tour_Schedule.objects.select_related("tour")
        .filter(
            sched_id__iexact=resolved_sched_id,
            tour__publication_status="published",
        )
        .first()
    )
    if schedule is None:
        return {
            "fulfillmentText": (
                f"I couldn't find published schedule {resolved_sched_id}. "
                "Please send a valid sched_id."
            )
        }

    booking_url = reverse("guest_book", kwargs={"tour_id": schedule.tour.tour_id})
    booking_url = f"{booking_url}?sched_id={schedule.sched_id}"
    if hasattr(request, "build_absolute_uri"):
        booking_url = request.build_absolute_uri(booking_url)

    link_actions = [
        {
            "label": "Open Tour Booking",
            "url": booking_url,
        }
    ]

    return {
        "fulfillmentText": (
            f"I found {schedule.tour.tour_name}. "
            "You can continue your booking using the button below."
        ),
        "billing_link": booking_url,
        "billing_link_label": "Open Tour Booking",
        "link_actions": link_actions,
        "open_in_new_tab": True,
    }


def _build_link_payload(request, *, text, route_name, label):
    link = reverse(route_name)
    if hasattr(request, "build_absolute_uri"):
        link = request.build_absolute_uri(link)
    return {
        "fulfillmentText": text,
        "billing_link": link,
        "billing_link_label": label,
        "open_in_new_tab": True,
    }


def _build_admin_pending_accommodations_summary():
    qs = Accomodation.objects.filter(is_active=True)
    pending = qs.filter(approval_status="pending")
    accepted = qs.filter(approval_status="accepted").count()
    declined = qs.filter(approval_status="declined").count()
    lines = [
        (
            f"Accommodation moderation summary: Pending {pending.count()}, "
            f"Accepted {accepted}, Declined {declined}."
        )
    ]
    sample = list(pending.order_by("-submitted_at", "company_name")[:6])
    if sample:
        lines.append("Latest pending accommodations:")
        for accom in sample:
            lines.append(f"- {accom.company_name} | {accom.company_type} | {accom.location}")
    return "\n".join(lines)


def _build_admin_pending_owner_accounts_summary():
    pending_group, _ = Group.objects.get_or_create(name="accommodation_owner_pending")
    approved_group, _ = Group.objects.get_or_create(name="accommodation_owner")
    declined_group, _ = Group.objects.get_or_create(name="accommodation_owner_declined")
    pending_count = pending_group.user_set.count()
    approved_count = approved_group.user_set.count()
    declined_count = declined_group.user_set.count()
    lines = [
        (
            "Accommodation owner account review summary: "
            f"Pending {pending_count}, Approved {approved_count}, Declined {declined_count}."
        )
    ]
    sample_pending = list(
        pending_group.user_set.order_by("date_joined", "username").values_list("username", flat=True)[:6]
    )
    if sample_pending:
        lines.append("Pending owner accounts:")
        for username in sample_pending:
            lines.append(f"- {username}")
    return "\n".join(lines)


def _detect_admin_support_topic(message):
    text = str(message or "").strip().lower()
    if not text:
        return ""
    compact_text = re.sub(r"[^a-z0-9\s]", " ", text)
    compact_text = re.sub(r"\s+", " ", compact_text).strip()

    if _contains_any_phrase(text, ("record not showing in the admin panel", "record not showing", "hindi lumalabas ang record sa admin panel", "dili makita sa admin panel ang record")):
        return "admin_record_visibility_issue"
    if _contains_any_phrase(text, ("reset a user account", "reset password", "reset a user password", "manage user accounts", "ireset ang user account", "manage users")):
        return "admin_user_account_management"
    if _contains_any_phrase(text, ("admin dashboard", "access the admin dashboard", "overall system summaries", "dashboard ng admin")):
        return "admin_reports_dashboard_support"
    if _contains_any_phrase(
        text,
        (
            "activate accommodation",
            "deactivate accommodation",
            "activate or deactivate listings",
            "activate/deactivate listings",
            "set listing active",
            "set listing inactive",
            "activation and deactivation",
            "how to activate listing",
            "how to deactivate listing",
            "i-activate ang listing",
            "i-deactivate ang listing",
        ),
    ):
        return "admin_activation_deactivation_help"

    if _contains_any_phrase(text, ("approve accommodation registrations", "review pending accommodation listings", "reject a submitted accommodation listing", "listings waiting for approval", "pending review", "pending listings", "i-approve ang accommodation listing", "approve listing", "aprubahan ang accommodation listing")):
        return "admin_approval_workflow"
    if _contains_any_phrase(text, ("publish a tourism destination", "unpublish a destination", "update destination details", "manage tourism attractions", "edit destination information after publishing", "manage tourism information")):
        return "admin_destination_management"
    if _contains_any_phrase(text, ("view all registered accommodations", "accommodation details in the system", "active or inactive", "manage accommodation records", "specific hotel or inn", "manage all accommodations")):
        return "admin_accommodation_records_management"
    if _contains_any_phrase(
        text,
        (
            "view all bookings in the system",
            "monitor confirmed and pending reservations",
            "check cancelled bookings",
            "booking summaries",
            "overall reservation activity",
            "check all bookings",
            "tour bookings",
            "tour booking",
            "in tour bookings",
            "tour package bookings",
            "tour reservations",
            "i mean tour packages",
        ),
    ):
        return "admin_booking_system_monitoring"
    if _contains_any_phrase(text, ("check employee and owner accounts", "manage system users", "update account roles or access", "i-manage ang users", "manage users", "user account management")):
        return "admin_user_account_management"
    if _contains_any_phrase(text, ("generate system reports", "tourism statistics and booking reports", "system summaries", "booking summary", "analytics and reports", "mga ulat ng system")):
        return "admin_reports_dashboard_support"
    if _contains_any_phrase(
        text,
        (
            "monitor chatbot activity",
            "chatbot activity",
            "show chatbot logs",
            "conversation logs",
            "chat metrics",
            "monitor chatbot usage",
            "chatbot usage",
            "chat logs",
            "pwede ba i monitor ang chatbot activity",
            "ma monitor ba ang chatbot activity",
            "ipakita ang chatbot logs",
        ),
    ):
        return "admin_chatbot_activity_monitoring"

    short = {
        "approve listing": "admin_approval_workflow",
        "pending accommodations": "admin_approval_workflow",
        "manage users": "admin_user_account_management",
        "admin dashboard": "admin_reports_dashboard_support",
        "booking summary": "admin_booking_system_monitoring",
        "chatbot activity": "admin_chatbot_activity_monitoring",
        "chat logs": "admin_chatbot_activity_monitoring",
    }
    return short.get(compact_text, "")


def _build_admin_approval_workflow_summary():
    accom_qs = Accomodation.objects.filter(is_active=True)
    pending_accom = accom_qs.filter(approval_status="pending")
    accepted_count = accom_qs.filter(approval_status="accepted").count()
    declined_count = accom_qs.filter(approval_status="declined").count()
    pending_owner_group, _ = Group.objects.get_or_create(name="accommodation_owner_pending")
    owner_pending_count = pending_owner_group.user_set.count()
    return (
        "Approval workflow snapshot:\n"
        f"- Accommodation listings: Pending {pending_accom.count()}, Accepted {accepted_count}, Declined {declined_count}\n"
        f"- Pending owner accounts: {owner_pending_count}\n"
        "Use Pending Accommodations and Pending Owner Accounts pages for review actions."
    )


def _build_admin_destination_management_summary():
    records = TourismInformation.objects.filter(is_active=True)
    return (
        "Destination/content management snapshot:\n"
        f"- Total active destination records: {records.count()}\n"
        f"- Published: {records.filter(publication_status='published').count()} | "
        f"Draft: {records.filter(publication_status='draft').count()} | "
        f"Archived: {records.filter(publication_status='archived').count()}\n"
        "Use Tourism Information management to publish/archive/update records."
    )


def _build_admin_accommodation_records_summary(message):
    text = str(message or "").strip().lower()
    qs = Accomodation.objects.filter(is_active=True)
    lines = [
        "Accommodation records snapshot:",
        f"- Total active listings: {qs.count()}",
        (
            f"- Accepted: {qs.filter(approval_status='accepted').count()} | "
            f"Pending: {qs.filter(approval_status='pending').count()} | "
            f"Declined: {qs.filter(approval_status='declined').count()}"
        ),
    ]
    search_match = re.search(r"(?:search|specific)\s+(?:for\s+)?([a-zA-Z][a-zA-Z\s\-]{2,60})", text)
    if search_match:
        needle = str(search_match.group(1) or "").strip()
        filtered = qs.filter(company_name__icontains=needle)[:5]
        if filtered:
            lines.append(f"Search results for '{needle}':")
            for accom in filtered:
                lines.append(
                    f"- {accom.company_name} | {accom.company_type} | {accom.location} | {str(accom.approval_status).title()}"
                )
        else:
            lines.append(f"No accommodation found for '{needle}'.")
    return "\n".join(lines)


def _build_admin_booking_system_summary():
    today = timezone.localdate()
    accom_qs = AccommodationBooking.objects.all()
    tour_qs = TourBooking.objects.all()
    accom_counts = Counter(str(v or "").strip().lower() for v in accom_qs.values_list("status", flat=True))
    tour_counts = Counter(str(v or "").strip().lower() for v in tour_qs.values_list("status", flat=True))
    return (
        "System-wide booking monitoring snapshot:\n"
        f"- Accommodation bookings: Total {accom_qs.count()}, Pending {accom_counts.get('pending', 0)}, "
        f"Confirmed {accom_counts.get('confirmed', 0)}, Declined {accom_counts.get('declined', 0)}, "
        f"Cancelled {accom_counts.get('cancelled', 0)}\n"
        f"- Tour bookings: Total {tour_qs.count()}, Pending {tour_counts.get('pending', 0)}, "
        f"Active {tour_counts.get('active', 0)}, Completed {tour_counts.get('completed', 0)}, "
        f"Cancelled {tour_counts.get('cancelled', 0)}\n"
        f"- Accommodation bookings today ({today.isoformat()}): {accom_qs.filter(booking_date__date=today).count()}"
    )


def _build_tour_booking_rollup(*, period="all_time", today=None):
    today = today or timezone.localdate()
    period = str(period or "all_time").strip().lower()
    include_pending_legacy = period == "all_time"

    tour_qs = TourBooking.objects.all()
    if period == "today":
        tour_qs = tour_qs.filter(booking_date__date=today)
    elif period == "month":
        tour_qs = tour_qs.filter(booking_date__year=today.year, booking_date__month=today.month)

    pending_qs = Pending.objects.all() if include_pending_legacy else Pending.objects.none()
    tour_counts = Counter(str(v or "").strip().lower() for v in tour_qs.values_list("status", flat=True))
    pending_counts = Counter(str(v or "").strip().lower() for v in pending_qs.values_list("status", flat=True))

    pending_count = tour_counts.get("pending", 0) + pending_counts.get("pending", 0)
    active_count = tour_counts.get("active", 0) + pending_counts.get("accepted", 0)
    completed_count = tour_counts.get("completed", 0) + pending_counts.get("completed", 0)
    cancelled_count = tour_counts.get("cancelled", 0) + pending_counts.get("cancelled", 0)
    declined_count = pending_counts.get("declined", 0)
    total_count = tour_qs.count() + pending_qs.count()

    tour_revenue_gross = (
        tour_qs.exclude(status="cancelled").aggregate(total=Sum("total_amount")).get("total") or Decimal("0")
    )
    tour_revenue_paid = (
        tour_qs.filter(payment_status="paid").aggregate(total=Sum("amount_paid")).get("total") or Decimal("0")
    )
    pending_accepted_gross = (
        pending_qs.filter(status__iexact="accepted")
        .aggregate(
            total=Sum(
                ExpressionWrapper(
                    F("total_guests") * F("sched_id__price"),
                    output_field=DecimalField(max_digits=12, decimal_places=2),
                )
            )
        )
        .get("total")
        or Decimal("0")
    )

    return {
        "total": total_count,
        "pending": pending_count,
        "active": active_count,
        "completed": completed_count,
        "cancelled": cancelled_count,
        "declined": declined_count,
        "revenue_gross": Decimal(tour_revenue_gross) + Decimal(pending_accepted_gross),
        "revenue_paid": Decimal(tour_revenue_paid),
        "includes_pending_legacy": bool(include_pending_legacy),
        "pending_legacy_count": pending_qs.count(),
    }


def _build_admin_user_account_summary():
    pending_owner_group, _ = Group.objects.get_or_create(name="accommodation_owner_pending")
    approved_owner_group, _ = Group.objects.get_or_create(name="accommodation_owner")
    return (
        "User/account management snapshot:\n"
        f"- Employee accounts: {Employee.objects.count()} (Accepted: {Employee.objects.filter(status='accepted').count()})\n"
        f"- Owner accounts: Pending {pending_owner_group.user_set.count()}, Approved {approved_owner_group.user_set.count()}\n"
        f"- Guest accounts: {Guest.objects.filter(is_active=True).count()}"
    )


def _build_admin_reports_dashboard_summary():
    today = timezone.localdate()
    accom_confirmed_total = (
        AccommodationBooking.objects.filter(status="confirmed").aggregate(total=Sum("total_amount")).get("total")
        or Decimal("0")
    )
    tour_paid_total = (
        TourBooking.objects.filter(payment_status="paid").aggregate(total=Sum("amount_paid")).get("total")
        or Decimal("0")
    )
    return (
        "Admin dashboard/report snapshot:\n"
        f"- Published tourism records: {TourismInformation.objects.published().count()}\n"
        f"- Accommodation bookings (total): {AccommodationBooking.objects.count()}\n"
        f"- Tour bookings (total): {TourBooking.objects.count()}\n"
        f"- Confirmed accommodation revenue (gross): PHP {Decimal(accom_confirmed_total):.2f}\n"
        f"- Paid tour revenue (collected): PHP {Decimal(tour_paid_total):.2f}\n"
        f"- Records updated today ({today.isoformat()}): {TourismInformation.objects.filter(updated_at__date=today).count()}"
    )


def _build_admin_chatbot_activity_summary():
    now = timezone.now()
    window_start = now - timedelta(days=7)
    total_chat_logs = ChatbotLog.objects.count()
    recent_chat_logs = ChatbotLog.objects.filter(created_at__gte=window_start).count()
    unique_chat_users = (
        ChatbotLog.objects.exclude(user__isnull=True)
        .values("user_id")
        .distinct()
        .count()
    )
    recent_reco_events = RecommendationEvent.objects.filter(event_time__gte=window_start).count()
    recent_metric_logs = SystemMetricLog.objects.filter(
        module="chat",
        logged_at__gte=window_start,
    ).count()
    recent_survey = UsabilitySurveyResponse.objects.filter(submitted_at__gte=window_start).count()

    return (
        "Chatbot activity snapshot (last 7 days):\n"
        f"- Chat messages logged: {recent_chat_logs} (all-time: {total_chat_logs})\n"
        f"- Unique authenticated chat users: {unique_chat_users}\n"
        f"- Recommendation/chat step events: {recent_reco_events}\n"
        f"- Chat runtime metric logs: {recent_metric_logs}\n"
        f"- Chat usability feedback entries: {recent_survey}\n"
        "Use Activity Logs for timeline monitoring and admin reports for broader system context."
    )


def _build_admin_record_visibility_diagnostic():
    inactive_accom = Accomodation.objects.filter(is_active=False).count()
    pending_accom = Accomodation.objects.filter(is_active=True, approval_status="pending").count()
    draft_dest = TourismInformation.objects.filter(is_active=True, publication_status="draft").count()
    archived_dest = TourismInformation.objects.filter(is_active=True, publication_status="archived").count()
    return (
        "Admin visibility diagnostic:\n"
        f"- Inactive accommodations: {inactive_accom}\n"
        f"- Pending accommodations: {pending_accom}\n"
        f"- Draft destinations: {draft_dest}\n"
        f"- Archived destinations: {archived_dest}\n"
        "If records are missing, verify status filters, approval status, and publication state."
    )


def _build_admin_activation_deactivation_summary():
    active_qs = Accomodation.objects.filter(is_active=True)
    inactive_qs = Accomodation.objects.filter(is_active=False)
    return (
        "Activation/deactivation guidance:\n"
        f"- Active listings: {active_qs.count()} | Inactive listings: {inactive_qs.count()}\n"
        "- Approval status and active flag both affect listing visibility and booking eligibility.\n"
        "- Use accommodation management pages to review records before applying status changes.\n"
        "- If a listing should disappear from operations, set it inactive and verify associated room availability."
    )


def _build_booking_count_summary(actor, *, user=None, message=""):
    role = str(actor.get("role") or "").strip().lower()
    text = str(message or "").strip().lower()
    today = timezone.localdate()
    period_label = "all time"

    if "today" in text:
        qs = qs.filter(booking_date__date=today)
        period_label = f"today ({today.isoformat()})"
    elif "this month" in text or "monthly" in text:
        qs = qs.filter(booking_date__year=today.year, booking_date__month=today.month)
        period_label = f"this month ({today.year}-{today.month:02d})"

    if role in {"admin", "employee"}:
        tour_scope_markers = (
            "tour booking",
            "tour bookings",
            "tour package",
            "tour packages",
            "tour reservation",
            "tour reservations",
            " tour ",
            " tours",
            "packages",
        )
        accommodation_scope_markers = (
            "accommodation",
            "hotel",
            "inn",
            "room",
            "rooms",
        )
        has_tour_scope = any(marker in text for marker in tour_scope_markers)
        has_accommodation_scope = any(marker in text for marker in accommodation_scope_markers)

        if has_tour_scope and not has_accommodation_scope:
            period_key = "today" if "today" in text else ("month" if ("this month" in text or "monthly" in text) else "all_time")
            rollup = _build_tour_booking_rollup(period=period_key, today=today)
            lines = [
                (
                    f"Tour booking summary for {period_label}: Total {rollup['total']}, "
                    f"Pending {rollup['pending']}, Active {rollup['active']}, "
                    f"Completed {rollup['completed']}, Cancelled {rollup['cancelled']}."
                )
            ]
            if rollup["declined"]:
                lines.append(f"Declined: {rollup['declined']}.")
            if "revenue" in text or "income" in text:
                lines.append(
                    f"Tour revenue for {period_label}: Gross PHP {Decimal(rollup['revenue_gross']):.2f}, "
                    f"Paid PHP {Decimal(rollup['revenue_paid']):.2f}."
                )
            if rollup["includes_pending_legacy"] and rollup["pending_legacy_count"] > 0:
                lines.append(
                    "Note: Includes legacy tour booking records from the pending-booking module to match booking report totals."
                )
            return "\n".join(lines)

        if has_tour_scope and has_accommodation_scope:
            accom_qs = AccommodationBooking.objects.all()
            period_key = "today" if "today" in text else ("month" if ("this month" in text or "monthly" in text) else "all_time")
            rollup = _build_tour_booking_rollup(period=period_key, today=today)
            if "today" in text:
                accom_qs = accom_qs.filter(booking_date__date=today)
            elif "this month" in text or "monthly" in text:
                accom_qs = accom_qs.filter(booking_date__year=today.year, booking_date__month=today.month)
            accom_status_counts = Counter(str(s or "").strip().lower() for s in accom_qs.values_list("status", flat=True))
            lines = [
                (
                f"Booking summary for {period_label}:\n"
                f"- Accommodation: Total {accom_qs.count()}, Pending {accom_status_counts.get('pending', 0)}, "
                f"Confirmed {accom_status_counts.get('confirmed', 0)}, Declined {accom_status_counts.get('declined', 0)}, "
                f"Cancelled {accom_status_counts.get('cancelled', 0)}\n"
                f"- Tour: Total {rollup['total']}, Pending {rollup['pending']}, Active {rollup['active']}, "
                f"Completed {rollup['completed']}, Cancelled {rollup['cancelled']}."
                )
            ]
            if "revenue" in text or "income" in text:
                accom_revenue = accom_qs.filter(status="confirmed").aggregate(total=Sum("total_amount")).get("total") or Decimal("0")
                lines.append(
                    f"Revenue for {period_label}: Accommodation gross PHP {Decimal(accom_revenue):.2f}, "
                    f"Tour gross PHP {Decimal(rollup['revenue_gross']):.2f}, Tour paid PHP {Decimal(rollup['revenue_paid']):.2f}."
                )
            if rollup["includes_pending_legacy"] and rollup["pending_legacy_count"] > 0:
                lines.append(
                    "Note: Tour totals include legacy pending-booking records to align with the tour booking report page."
                )
            return "\n".join(lines)

        if not has_tour_scope and not has_accommodation_scope:
            accom_qs = AccommodationBooking.objects.all()
            period_key = "today" if "today" in text else ("month" if ("this month" in text or "monthly" in text) else "all_time")
            rollup = _build_tour_booking_rollup(period=period_key, today=today)
            if "today" in text:
                accom_qs = accom_qs.filter(booking_date__date=today)
            elif "this month" in text or "monthly" in text:
                accom_qs = accom_qs.filter(booking_date__year=today.year, booking_date__month=today.month)
            accom_status_counts = Counter(str(s or "").strip().lower() for s in accom_qs.values_list("status", flat=True))
            lines = [
                (
                f"Booking summary for {period_label}:\n"
                f"- Accommodation: Total {accom_qs.count()}, Pending {accom_status_counts.get('pending', 0)}, "
                f"Confirmed {accom_status_counts.get('confirmed', 0)}, Declined {accom_status_counts.get('declined', 0)}, "
                f"Cancelled {accom_status_counts.get('cancelled', 0)}\n"
                f"- Tour: Total {rollup['total']}, Pending {rollup['pending']}, Active {rollup['active']}, "
                f"Completed {rollup['completed']}, Cancelled {rollup['cancelled']}."
                )
            ]
            if "revenue" in text or "income" in text:
                accom_revenue = accom_qs.filter(status="confirmed").aggregate(total=Sum("total_amount")).get("total") or Decimal("0")
                lines.append(
                    f"Revenue for {period_label}: Accommodation gross PHP {Decimal(accom_revenue):.2f}, "
                    f"Tour gross PHP {Decimal(rollup['revenue_gross']):.2f}, Tour paid PHP {Decimal(rollup['revenue_paid']):.2f}."
                )
            if rollup["includes_pending_legacy"] and rollup["pending_legacy_count"] > 0:
                lines.append(
                    "Note: Tour totals include legacy pending-booking records to align with the tour booking report page."
                )
            return "\n".join(lines)

    qs = AccommodationBooking.objects.all()
    if role == "guest" and user is not None:
        qs = qs.filter(guest=user)
    elif role == "owner" and user is not None:
        qs = qs.filter(accommodation__owner=user)

    total = qs.count()
    status_counts = Counter(str(s or "").strip().lower() for s in qs.values_list("status", flat=True))
    return (
        f"Booking summary for {period_label}: Total {total}, "
        f"Pending {status_counts.get('pending', 0)}, "
        f"Confirmed {status_counts.get('confirmed', 0)}, "
        f"Declined {status_counts.get('declined', 0)}, "
        f"Cancelled {status_counts.get('cancelled', 0)}."
    )


def _is_default_accommodation_suggestions_command(message):
    text = (message or "").strip().lower()
    if not text:
        return False
    # Let location/budget/guest-constrained discovery pass through the main
    # recommendation parser so results stay dynamic per query context.
    if re.search(
        r"\b(in|near|under|below|budget|for\s+\d+|barangay|brgy|villareal|suba|poblacion|tinago|ubos|bayawan city)\b",
        text,
    ):
        return False
    phrases = [
        "show default hotel suggestions",
        "show available hotels",
        "show available inns",
        "show available accommodations",
        "show available hotels and inns",
        "show available hotels/inns",
        "show hotels and inns",
        "show hotels near me",
        "default hotel suggestions",
        "show hotel suggestions",
        "show inn suggestions",
        "show accommodation suggestions",
        "show accommodation recommendations",
        "show accommodations",
        "show hotels",
        "show inns",
        "places to stay",
        "where should i stay",
        "show approved stays",
        "suggest hotels",
        "suggest inns",
    ]
    return any(phrase in text for phrase in phrases)


def _openai_extract_intent_and_params(message):
    # Legacy compatibility wrapper kept to avoid breaking imports/tests.
    # OpenAI is no longer used for intent parsing.
    parsed = _classify_intent_and_extract_params(message, actor=actor)
    parsed["source"] = "legacy_wrapper_no_openai_parse"
    return parsed


def _fallback_nlg_paraphrase(reply):
    text = str(reply or "").strip()
    if not text:
        return text
    lines = [line.strip() for line in text.splitlines() if str(line).strip()]
    if not lines:
        return text
    if len(lines) == 1:
        return lines[0]
    normalized = []
    for line in lines:
        cleaned = re.sub(r"\s{2,}", " ", line)
        # Normalize common encoding artifacts seen in legacy strings.
        cleaned = (
            cleaned.replace("â€™", "'")
            .replace("â€¢", "-")
            .replace("â‚±", "₱")
        )
        normalized.append(cleaned)
    merged = "\n".join(normalized)
    replacements = {
        "Invalid JSON payload.": "I had trouble reading that request format. Please try again.",
        "Please send a message in this format: {\"message\": \"...\"}.": "Please type a short message and I’ll help from there.",
        "Please log in first to use the chatbot.": "Please log in first so I can assist with your account-based requests.",
        "I can help with your request. Please try rephrasing it in one sentence.": "I can help with that. Please rephrase it in one clear sentence so I can guide you better.",
    }
    for src, dst in replacements.items():
        if src in merged:
            merged = merged.replace(src, dst)
    if merged.lower().startswith("please specify which schedule to book"):
        merged = merged.replace(
            "Please specify which schedule to book.",
            "Happy to help. Please share the schedule ID you want to book.",
            1,
        )
    if len(lines) == 1:
        return merged
    # Keep facts unchanged but improve readability for template-heavy text.
    if not merged.endswith(".") and not merged.endswith("?"):
        merged = f"{merged}."
    return merged


def _extract_critical_facts_for_nlg_guardrails(text):
    raw = str(text or "")
    lowered = raw.lower()

    urls = re.findall(r"https?://[^\s)>\]}]+", raw, flags=re.IGNORECASE)
    iso_dates = re.findall(r"\b\d{4}-\d{2}-\d{2}\b", raw)
    money_tokens = re.findall(r"(?:php|₱)\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)", raw, flags=re.IGNORECASE)
    booking_ids = re.findall(r"\bbooking\s*id\s*[:#-]?\s*([A-Za-z0-9\-]+)\b", raw, flags=re.IGNORECASE)
    room_ids = re.findall(r"\broom\s*id\s*[:#-]?\s*(\d+)\b", raw, flags=re.IGNORECASE)
    quantity_pairs = re.findall(
        r"\b(\d+)\s*(guest|guests|night|nights|room|rooms|booking|bookings|pax)\b",
        lowered,
        flags=re.IGNORECASE,
    )

    normalized_money = []
    for token in money_tokens:
        cleaned = str(token or "").replace(",", "").strip()
        if not cleaned:
            continue
        try:
            normalized_money.append(f"{float(cleaned):.2f}")
        except Exception:
            continue

    normalized_urls = sorted({str(url).strip() for url in urls if str(url).strip()})
    normalized_dates = sorted({str(val).strip() for val in iso_dates if str(val).strip()})
    normalized_booking_ids = sorted({str(val).strip().lower() for val in booking_ids if str(val).strip()})
    normalized_room_ids = sorted({str(val).strip() for val in room_ids if str(val).strip()})
    normalized_quantities = sorted(
        {
            f"{str(num).strip()}:{str(unit).strip().lower()}"
            for num, unit in quantity_pairs
            if str(num).strip() and str(unit).strip()
        }
    )
    normalized_money = sorted(set(normalized_money))

    return {
        "urls": normalized_urls,
        "iso_dates": normalized_dates,
        "amounts": normalized_money,
        "booking_ids": normalized_booking_ids,
        "room_ids": normalized_room_ids,
        "quantities": normalized_quantities,
    }


def _guardrails_validate_nlg_output(backend_reply, candidate_reply):
    backend_facts = _extract_critical_facts_for_nlg_guardrails(backend_reply)
    candidate_facts = _extract_critical_facts_for_nlg_guardrails(candidate_reply)
    # Allow clean plain-text rewrites that avoid introducing conflicting
    # structured facts. This prevents overly aggressive guardrail rejection when
    # the NLG output intentionally stays concise and non-transactional.
    backend_fact_count = sum(len(backend_facts.get(key) or []) for key in ("urls", "iso_dates", "amounts", "booking_ids", "room_ids", "quantities"))
    candidate_fact_count = sum(len(candidate_facts.get(key) or []) for key in ("urls", "iso_dates", "amounts", "booking_ids", "room_ids", "quantities"))
    if backend_fact_count > 0 and candidate_fact_count == 0 and str(candidate_reply or "").strip():
        return True, []
    reasons = []
    for key in ("urls", "iso_dates", "amounts", "booking_ids", "room_ids", "quantities"):
        expected_values = backend_facts.get(key) or []
        actual_values = set(candidate_facts.get(key) or [])
        for expected in expected_values:
            if expected not in actual_values:
                reasons.append(f"missing_{key}:{expected}")
        # Block injected factual values in structured replies.
        if key in ("amounts", "booking_ids", "room_ids", "quantities") and expected_values:
            expected_set = set(expected_values)
            for actual in actual_values:
                if actual not in expected_set:
                    reasons.append(f"unexpected_{key}:{actual}")
    return (len(reasons) == 0), reasons


def _apply_nlg_output_guardrails(*, request, provider_source, backend_reply, candidate_reply):
    candidate_text = str(candidate_reply or "").strip()
    backend_text = str(backend_reply or "").strip()
    if not candidate_text or not backend_text:
        return candidate_text, provider_source

    is_valid, reasons = _guardrails_validate_nlg_output(backend_text, candidate_text)
    if is_valid:
        return candidate_text, provider_source

    _safe_log_chat_runtime_event(
        request,
        event_key="output_guardrail_triggered",
        detail=";".join(reasons[:6]),
    )
    fallback_source = f"{provider_source}_guardrail_fallback"
    return backend_text, fallback_source


def _classify_gemini_error(exc):
    text = str(exc or "").strip()
    lowered = text.lower()
    reason = "provider_error"
    timeout_flag = False
    retryable = False
    http_status = None

    status_match = re.search(r"\bstatus(?:\s*code)?\s*[:=]?\s*(\d{3})\b", lowered)
    if status_match:
        try:
            http_status = int(status_match.group(1))
        except Exception:
            http_status = None
    elif "429" in lowered:
        http_status = 429
    elif "503" in lowered:
        http_status = 503
    elif "500" in lowered:
        http_status = 500

    if any(token in lowered for token in ("timeout", "timed out", "deadline", "read timed out")):
        reason = "timeout"
        timeout_flag = True
        retryable = True
    elif any(token in lowered for token in ("rate limit", "quota", "resource_exhausted", "too many requests")) or http_status == 429:
        reason = "rate_limited"
        retryable = True
    elif any(token in lowered for token in ("unavailable", "temporarily", "internal", "server error", "overloaded")) or http_status in {500, 502, 503, 504}:
        reason = "provider_unavailable"
        retryable = True
    elif any(token in lowered for token in ("blocked", "safety", "content policy", "recitation")):
        reason = "blocked"
        retryable = True
    elif any(token in lowered for token in ("invalid argument", "malformed", "bad request", "invalid_request")) or http_status == 400:
        reason = "invalid_request"
        retryable = False
    elif any(token in lowered for token in ("permission", "unauthorized", "forbidden", "api key", "authentication")) or http_status in {401, 403}:
        reason = "auth_error"
        retryable = False

    return {
        "reason": reason,
        "http_status": http_status,
        "timeout": timeout_flag,
        "retryable": retryable,
        "message": text[:300],
    }


def _extract_gemini_response_text(gemini_response):
    direct_text = str(getattr(gemini_response, "text", "") or "").strip()
    if direct_text:
        return direct_text

    candidates = getattr(gemini_response, "candidates", None)
    if not isinstance(candidates, list):
        return ""
    parts = []
    for candidate in candidates[:3]:
        content = getattr(candidate, "content", None)
        part_rows = getattr(content, "parts", None)
        if not isinstance(part_rows, list):
            continue
        for part in part_rows[:8]:
            part_text = str(getattr(part, "text", "") or "").strip()
            if part_text:
                parts.append(part_text)
    return "\n".join(parts).strip()


def _is_nlg_fallback_source(source):
    normalized = str(source or "").strip().lower()
    if not normalized:
        return False
    fallback_markers = (
        "fallback",
        "_error",
        "_unavailable",
        "_empty",
        "_disabled",
    )
    return any(marker in normalized for marker in fallback_markers)


def _record_nlg_meta(request, meta):
    try:
        context = getattr(request, "_chatbot_log_context", None)
        if not isinstance(context, dict):
            return
        provenance = context.get("provenance") if isinstance(context.get("provenance"), dict) else {}
        payload = meta if isinstance(meta, dict) else {}
        compact_payload = {
            "nlg_error_reason": str(payload.get("nlg_error_reason") or "")[:120],
            "nlg_http_status": payload.get("nlg_http_status"),
            "nlg_timeout": bool(payload.get("nlg_timeout")),
            "nlg_retry_count": _to_int(payload.get("nlg_retry_count"), default=0),
            "nlg_guardrail_triggered": bool(payload.get("nlg_guardrail_triggered")),
            "nlg_empty_response": bool(payload.get("nlg_empty_response")),
            "nlg_request_skipped": bool(payload.get("nlg_request_skipped")),
            "nlg_source": str(payload.get("nlg_source") or "")[:120],
            "nlg_provider": str(payload.get("nlg_provider") or "")[:40],
        }
        provenance["nlg"] = compact_payload
        context["provenance"] = provenance
        # Preserve existing parse-fallback signals while accurately including
        # NLG fallbacks only when they really happened.
        context["fallback_used"] = bool(context.get("fallback_used")) or bool(payload.get("nlg_fallback_used"))
        if payload.get("nlg_source"):
            context["response_nlg_source"] = str(payload.get("nlg_source"))
    except Exception:
        pass


def _resolve_supported_gemini_model():
    raw = str(os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite") or "").strip() or "gemini-2.5-flash-lite"
    lowered = raw.lower()
    if lowered.startswith("gemini-1.") or "gemini-pro" in lowered:
        return "gemini-2.5-flash-lite", True
    return raw, False


def generate_final_ai_response(*, request, intent, user_message, backend_reply):
    reply = str(backend_reply or "").strip()
    nlg_meta = {
        "nlg_provider": "",
        "nlg_error_reason": "",
        "nlg_http_status": None,
        "nlg_timeout": False,
        "nlg_retry_count": 0,
        "nlg_guardrail_triggered": False,
        "nlg_empty_response": False,
        "nlg_request_skipped": False,
        "nlg_source": "",
        "nlg_fallback_used": False,
    }

    def _finish(text, source, **updates):
        nlg_meta.update(updates or {})
        nlg_meta["nlg_source"] = str(source or "")
        if "nlg_fallback_used" not in updates:
            nlg_meta["nlg_fallback_used"] = _is_nlg_fallback_source(source)
        _record_nlg_meta(request, nlg_meta)
        return text, source

    if not reply:
        return _finish(reply, "empty_backend_reply", nlg_error_reason="empty_backend_reply", nlg_request_skipped=True)

    # Preserve structured booking/slot templates exactly to avoid key-value drift.
    if any(
        marker in reply.lower()
        for marker in (
            "great. here are the details i have so far:",
            "recorded details:",
            "thank you. i have recorded the following details:",
            "details received:",
            "booking receipt / summary",
            "booking summary (draft",
            "booking draft (not yet saved)",
        )
    ):
        return _finish(reply, "backend_structured_template", nlg_request_skipped=True)

    openai_api_key = str(os.getenv("OPENAI_API_KEY", "")).strip()
    gemini_api_key = str(os.getenv("GEMINI_API_KEY", "")).strip()
    if getattr(settings, "TESTING", False) and not openai_api_key:
        # Keep tests deterministic by skipping live Gemini rewrites unless
        # OpenAI is explicitly enabled by a test case.
        gemini_api_key = ""
    nlg_enabled = str(
        os.getenv("CHATBOT_LLM_NLG_ENABLED", os.getenv("CHATBOT_OPENAI_NLG_ENABLED", "1"))
    ).strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    if not nlg_enabled:
        return _finish(
            _fallback_nlg_paraphrase(reply),
            "llm_nlg_disabled_paraphrase",
            nlg_error_reason="llm_nlg_disabled",
            nlg_request_skipped=True,
            nlg_fallback_used=True,
        )
    if DEMO_SAFE_MODE:
        safe_demo_intents = {
            "plan_bayawan_stay",
            "get_accommodation_recommendation",
            "gethotelrecommendation",
            "travel_guidance",
            "get_recommendation",
            "gettourrecommendation",
            "calculate_billing",
            "employee_assigned_tours",
            "employee_open_assignment",
            "employee_update_assignment",
            "reporting_summary",
        }
        allow_demo_nlg = str(os.getenv("CHATBOT_DEMO_ALLOW_NLG", "0")).strip().lower() in {"1", "true", "yes", "on"}
        openai_override = bool(openai_api_key and OpenAI is not None)
        if str(intent or "").strip().lower() in safe_demo_intents and not allow_demo_nlg and not openai_override:
            return _finish(
                reply,
                "demo_safe_mode_deterministic",
                nlg_request_skipped=True,
                nlg_error_reason="demo_safe_mode",
                nlg_fallback_used=False,
            )

    user_id = ""
    user = getattr(request, "user", None)
    if user and getattr(user, "is_authenticated", False):
        user_id = str(getattr(user, "pk", "") or "")

    # LLM receives sanitized backend context only.
    nlg_payload = {
        "intent": str(intent or ""),
        "user_message": str(user_message or "")[:500],
        "backend_reply": reply[:3500],
        "user_id": user_id[:40],
    }
    if not str(nlg_payload.get("backend_reply") or "").strip():
        return _finish(reply, "nlg_skipped_empty_backend_reply", nlg_error_reason="empty_backend_reply", nlg_request_skipped=True)

    payload_json = json.dumps(nlg_payload, ensure_ascii=True)
    max_prompt_chars = max(1200, min(_to_int(os.getenv("CHATBOT_NLG_MAX_PROMPT_CHARS", "6000"), default=6000), 12000))
    if len(payload_json) > max_prompt_chars:
        return _finish(
            reply,
            "nlg_skipped_prompt_too_long",
            nlg_error_reason="prompt_too_long",
            nlg_request_skipped=True,
        )

    system_prompt = (
        "You are a tourism reservation assistant NLG layer.\n"
        "Rewrite the backend reply into clear, professional, and formal language.\n"
        "Use polite and concise phrasing suitable for customer support.\n"
        "Do not add new facts, prices, dates, IDs, links, or policy claims.\n"
        "Keep all booking/payment constraints exactly as provided.\n"
        "Return plain text only."
    )

    if openai_api_key and OpenAI is not None:
        model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
        try:
            client = OpenAI(api_key=openai_api_key)
            completion = client.chat.completions.create(
                model=model,
                temperature=0.2,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": json.dumps(nlg_payload, ensure_ascii=True)},
                ],
            )
            phrased = str(completion.choices[0].message.content or "").strip()
            if phrased:
                out_text, out_source = _apply_nlg_output_guardrails(
                    request=request,
                    provider_source="openai_nlg",
                    backend_reply=reply,
                    candidate_reply=phrased,
                )
                return _finish(
                    out_text,
                    out_source,
                    nlg_provider="openai",
                    nlg_guardrail_triggered=bool("guardrail_fallback" in str(out_source or "")),
                )
        except Exception:
            pass

    if gemini_api_key and genai is not None:
        gemini_model, model_was_upgraded = _resolve_supported_gemini_model()
        nlg_meta["nlg_provider"] = "gemini"
        if model_was_upgraded:
            nlg_meta["nlg_error_reason"] = "deprecated_model_replaced"
        gemini_client = None
        try:
            gemini_client = genai.Client(api_key=gemini_api_key)
        except Exception:
            gemini_client = None
        if gemini_client is None:
            _safe_log_chat_runtime_event(
                request,
                event_key="gemini_failure_fallback",
                detail="gemini_nlg_unavailable",
            )
            return _finish(
                reply,
                "gemini_nlg_unavailable",
                nlg_error_reason="client_unavailable",
                nlg_fallback_used=True,
            )

        last_error = {}
        max_attempts = max(1, min(_to_int(os.getenv("CHATBOT_GEMINI_NLG_RETRY", "2"), default=2), 3))
        prompt = (
            f"{system_prompt}\n\n"
            "Input JSON:\n"
            f"{payload_json}"
        )
        prompt = prompt[: max_prompt_chars + 500]
        retry_count = 0
        for attempt in range(max_attempts):
            try:
                gemini_response = gemini_client.models.generate_content(
                    model=gemini_model,
                    contents=prompt,
                )
                phrased = _extract_gemini_response_text(gemini_response)
                if phrased:
                    source = "gemini_nlg"
                    if attempt > 0:
                        source = "gemini_nlg_retry"
                    out_text, out_source = _apply_nlg_output_guardrails(
                        request=request,
                        provider_source=source,
                        backend_reply=reply,
                        candidate_reply=phrased,
                    )
                    return _finish(
                        out_text,
                        out_source,
                        nlg_retry_count=retry_count,
                        nlg_guardrail_triggered=bool("guardrail_fallback" in str(out_source or "")),
                    )
                last_error = {
                    "reason": "empty_response",
                    "http_status": None,
                    "timeout": False,
                    "retryable": False,
                    "message": "",
                }
                nlg_meta["nlg_empty_response"] = True
            except Exception as exc:
                last_error = _classify_gemini_error(exc)
                nlg_meta["nlg_http_status"] = last_error.get("http_status")
                nlg_meta["nlg_timeout"] = bool(last_error.get("timeout"))
                nlg_meta["nlg_error_reason"] = str(last_error.get("reason") or "")
                if bool(last_error.get("retryable")) and attempt + 1 < max_attempts:
                    retry_count += 1
                    continue
                continue
            # No response text: retry once only for retryable empty output cases.
            if attempt + 1 < max_attempts:
                retry_count += 1
                continue
            break

        reason_after_attempts = str(last_error.get("reason") or "")
        if reason_after_attempts in {"timeout", "blocked", "empty_response"} or nlg_meta.get("nlg_empty_response"):
            try:
                simplified_prompt = (
                    "Rewrite this reply in concise, natural English.\n"
                    "Do not add or change facts.\n"
                    f"Reply:\n{reply[:2500]}"
                )
                gemini_response = gemini_client.models.generate_content(
                    model=gemini_model,
                    contents=simplified_prompt,
                )
                tier_b_text = _extract_gemini_response_text(gemini_response)
                if tier_b_text:
                    out_text, out_source = _apply_nlg_output_guardrails(
                        request=request,
                        provider_source="gemini_nlg_tier_b",
                        backend_reply=reply,
                        candidate_reply=tier_b_text,
                    )
                    return _finish(
                        out_text,
                        out_source,
                        nlg_retry_count=retry_count + 1,
                        nlg_guardrail_triggered=bool("guardrail_fallback" in str(out_source or "")),
                    )
            except Exception as exc:
                last_error = _classify_gemini_error(exc)
                reason_after_attempts = str(last_error.get("reason") or reason_after_attempts)

        fallback_text = _fallback_nlg_paraphrase(reply)
        if not str(fallback_text or "").strip():
            fallback_text = "I can still help with that. Please try your request again in one short sentence."
        if fallback_text and fallback_text != reply:
            reason = str(last_error.get("reason") or reason_after_attempts)
            if not reason and nlg_meta.get("nlg_empty_response"):
                reason = "empty_response"
            _safe_log_chat_runtime_event(
                request,
                event_key="gemini_failure_fallback",
                detail=f"gemini_nlg_fallback_paraphrase:{reason or 'unknown'}",
            )
            return _finish(
                fallback_text,
                "gemini_nlg_fallback_paraphrase",
                nlg_error_reason=reason or "fallback_paraphrase",
                nlg_retry_count=retry_count,
                nlg_fallback_used=True,
            )
        if last_error:
            _safe_log_chat_runtime_event(
                request,
                event_key="gemini_failure_fallback",
                detail=f"gemini_nlg_error:{str(last_error.get('reason') or 'provider_error')}",
            )
            return _finish(
                reply,
                "gemini_nlg_error",
                nlg_error_reason=str(last_error.get("reason") or "provider_error"),
                nlg_http_status=last_error.get("http_status"),
                nlg_timeout=bool(last_error.get("timeout")),
                nlg_retry_count=retry_count,
                nlg_fallback_used=True,
            )
        _safe_log_chat_runtime_event(
            request,
            event_key="gemini_failure_fallback",
            detail="gemini_nlg_empty",
        )
        return _finish(
            reply,
            "gemini_nlg_empty",
            nlg_error_reason="empty_response",
            nlg_empty_response=True,
            nlg_retry_count=retry_count,
            nlg_fallback_used=True,
        )

    if openai_api_key and OpenAI is not None:
        return _finish(
            _fallback_nlg_paraphrase(reply),
            "openai_nlg_error_paraphrase",
            nlg_provider="openai",
            nlg_error_reason="provider_error",
            nlg_fallback_used=True,
        )
    if gemini_api_key and genai is None:
        _safe_log_chat_runtime_event(
            request,
            event_key="gemini_failure_fallback",
            detail="gemini_nlg_unavailable_paraphrase",
        )
        return _finish(
            _fallback_nlg_paraphrase(reply),
            "gemini_nlg_unavailable_paraphrase",
            nlg_provider="gemini",
            nlg_error_reason="client_unavailable",
            nlg_fallback_used=True,
        )
    if gemini_api_key:
        _safe_log_chat_runtime_event(
            request,
            event_key="gemini_failure_fallback",
            detail="gemini_nlg_error_paraphrase",
        )
        return _finish(
            _fallback_nlg_paraphrase(reply),
            "gemini_nlg_error_paraphrase",
            nlg_provider="gemini",
            nlg_error_reason="provider_error",
            nlg_fallback_used=True,
        )
    if openai_api_key:
        return _finish(
            _fallback_nlg_paraphrase(reply),
            "openai_nlg_error_paraphrase",
            nlg_provider="openai",
            nlg_error_reason="provider_error",
            nlg_fallback_used=True,
        )
    return _finish(
        _fallback_nlg_paraphrase(reply),
        "llm_nlg_unavailable_paraphrase",
        nlg_error_reason="provider_unavailable",
        nlg_fallback_used=True,
    )

def _openai_generate_final_response(*, request, intent, user_message, backend_reply):
    """
    Backward-compatible alias.
    Deprecated naming retained to avoid breaking older imports/tests.
    """
    return generate_final_ai_response(
        request=request,
        intent=intent,
        user_message=user_message,
        backend_reply=backend_reply,
    )


@csrf_exempt
def ai_chat(request):
    start_time = time.perf_counter()

    if request.method != "POST":
        return _chat_json_response(request, start_time, {"status": "ok"})

    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return _chat_json_response(
            request,
            start_time,
            {"fulfillmentText": "Invalid JSON payload."},
            status=400,
            error_message="invalid_json_payload",
        )
    client_location = _resolve_client_location(payload, request)

    actor = _resolve_chat_actor(request)
    user = actor.get("user")
    if not actor.get("is_allowed"):
        return _chat_json_response(
            request,
            start_time,
            {
                "fulfillmentText": "Please log in first to use the chatbot.",
                "error_code": "chat_requires_login",
            },
            status=401,
            error_message="chat_requires_login",
        )

    raw_message = str(payload.get("message", "")).strip()
    page_context = str(
        payload.get("page_context")
        or payload.get("current_page")
        or ""
    ).strip()[:240]
    translated_message, detected_language = translate_to_english(raw_message)
    message = str(translated_message or raw_message).strip()
    pre_normalized_message = str(message or "").strip()
    normalized_message = _normalize_common_chat_typos(pre_normalized_message)
    if normalized_message:
        message = normalized_message
    if actor.get("role") == "guest":
        parity_normalized_message = _normalize_button_parity_message(message)
        if parity_normalized_message:
            message = parity_normalized_message
    init_suggestions = bool(payload.get("init_suggestions"))
    if not message:
        return _chat_json_response(
            request,
            start_time,
            {"fulfillmentText": "Please send a message in this format: {\"message\": \"...\"}."},
            status=400,
            error_message="missing_message",
        )
    request._chatbot_log_context = {
        "user_message": raw_message,
        "resolved_intent": "",
        "resolved_params": {},
        "intent_classifier": {},
        "response_nlg_source": "",
        "fallback_used": False,
        "provenance": {
            "chat_role": actor.get("role", ""),
            "session_id": (request.session.session_key or "") if hasattr(request, "session") else "",
            "page_context": page_context,
            "detected_language": detected_language,
            "input_translated_to_english": bool(
                str(raw_message or "").strip()
                and str(message or "").strip()
                and str(raw_message).strip() != str(message).strip()
            ),
            "typo_normalization_applied": bool(
                str(pre_normalized_message or "").strip()
                and str(message or "").strip()
                and str(pre_normalized_message).strip() != str(message).strip()
            ),
            "response_translated_to_user_language": False,
            "active_flow": "",
            "slots_filled": [],
            "slots_missing": "",
            "last_results_type": "",
            "last_selected_entity": "",
            "clarification_used": False,
            "context_reply_used": False,
        },
    }
    if (
        actor.get("role") == "guest"
        and _contains_any_phrase(
            message,
            (
                "i want to go somewhere nice",
                "where should i go",
                "any recommendation",
                "what can i do",
            ),
        )
        and not _is_stay_planning_request(message)
    ):
        request._chatbot_log_context["resolved_intent"] = "clarification"
        return _chat_json_response(
            request,
            start_time,
            {
                "fulfillmentText": (
                    "I want to make sure I understood correctly. "
                    "Do you want help with tours, accommodations, directions, or full trip planning?"
                ),
                "quick_replies": [
                    "Show tours",
                    "Find accommodations",
                    "Plan my trip",
                    "Get directions",
                ],
                "needs_clarification": True,
                "missing_slot": "clarification",
            },
        )
    admin_topic_hint = _detect_admin_support_topic(message) if actor.get("role") == "admin" else ""
    employee_topic_hint = _detect_employee_support_topic(message) if actor.get("role") == "employee" else ""

    social_payload = _build_small_talk_payload(
        request=request,
        actor=actor,
        message=message,
    )
    if social_payload:
        request._chatbot_log_context["resolved_intent"] = "small_talk"
        return _chat_json_response(request, start_time, social_payload)

    if _is_help_or_greeting_command(message):
        request._chatbot_log_context["resolved_intent"] = "role_help"
        help_payload = _build_role_help_payload(actor)
        return _chat_json_response(request, start_time, help_payload)

    if actor.get("role") == "owner" and _is_owner_help_command(message):
        request._chatbot_log_context["resolved_intent"] = "role_help"
        return _chat_json_response(request, start_time, _build_role_help_payload(actor))

    if actor.get("role") == "owner" and _is_owner_manage_links_command(message):
        request._chatbot_log_context["resolved_intent"] = "owner_listing_visibility"
        return _chat_json_response(
            request,
            start_time,
            _build_link_payload(
                request,
                text=(
                    "You can update your accommodation links and images from Owner Hub.\n"
                    "Please update these fields there: official website, Facebook page, external booking page, and accommodation images."
                ),
                route_name="admin_app:owner_hub",
                label="Open Owner Hub",
            ),
        )

    if actor.get("role") == "owner":
        owner_booking_today_text = re.sub(
            r"\s+",
            " ",
            re.sub(r"[^a-z0-9\s]", " ", str(message or "").lower()),
        ).strip()
        owner_booking_today_hit = bool(
            re.search(
                r"\b(rooms?|kwarto)\b.*\b(bookings?|booked|reserved|reservations?)\b.*\b(today|karon|ngayon)\b",
                owner_booking_today_text,
            )
            or re.search(
                r"\b(today|karon|ngayon)\b.*\b(rooms?|kwarto)\b.*\b(bookings?|booked|reserved|reservations?)\b",
                owner_booking_today_text,
            )
        )
        if owner_booking_today_hit:
            request._chatbot_log_context["resolved_intent"] = "owner_submit_monthly_report"
            return _chat_json_response(
                request,
                start_time,
                _build_link_payload(
                    request,
                    text=(
                        "Owner-side booking transactions are out of scope in chat.\n"
                        "Please submit or review your monthly tourism report instead."
                    ),
                    route_name="admin_app:owner_report_submit",
                    label="Open Monthly Reports",
                ),
            )

    if actor.get("role") == "guest":
        current_state = _load_chat_state(request)
        fallback_sched_ids = (
            current_state.get("last_tour_recommendation_sched_ids")
            if isinstance(current_state.get("last_tour_recommendation_sched_ids"), list)
            else []
        )
        if _is_guest_view_tour_bookings_command(message):
            request._chatbot_log_context["resolved_intent"] = "guest_view_tour_bookings"
            return _chat_json_response(
                request,
                start_time,
                _build_guest_tour_bookings_payload(request, user),
            )
        selection_index = _extract_tour_selection_index(message)
        is_tour_booking_shortcut = (
            selection_index > 0
            and bool(re.search(r"\b(book|reserve|reservation)\b", str(message or "").lower()))
        )
        if (
            _is_guest_tour_interest_followup(message)
            and not _is_guest_tour_booking_command(message)
            and not is_tour_booking_shortcut
        ):
            interest_hint = _extract_tour_interest_hint(message)
            if not interest_hint and selection_index > 0 and fallback_sched_ids:
                pick = selection_index - 1
                if 0 <= pick < len(fallback_sched_ids):
                    sched = (
                        Tour_Schedule.objects.select_related("tour")
                        .filter(sched_id__iexact=str(fallback_sched_ids[pick]))
                        .first()
                    )
                    if sched is not None:
                        interest_hint = str(getattr(getattr(sched, "tour", None), "tour_name", "") or "").strip()
            if interest_hint:
                request._chatbot_log_context["resolved_intent"] = "book_tour_via_link"
                current_state["pending_tour_booking"] = {
                    "stage": "awaiting_details",
                    "active_flow": "tour_booking",
                    "tour_name_hint": interest_hint,
                    "expected_slots": ["date", "guests"],
                }
                _save_chat_state(request, current_state)
                return _chat_json_response(
                    request,
                    start_time,
                    {
                        "fulfillmentText": (
                            f"Great choice. I can help book {interest_hint}. "
                            "What date and how many adults?"
                        ),
                        "quick_replies": ["May 10 for 2 adults", "May 12 for 2 adults", "show available tours"],
                    },
                )
        if _is_guest_tour_booking_command(message) or is_tour_booking_shortcut:
            request._chatbot_log_context["resolved_intent"] = "book_tour_via_link"
            direct_tour_name_hint = _extract_tour_name_booking_hint(message)
            direct_date_hint = _extract_tour_date_hint_from_message(message)
            direct_sched_id = _extract_sched_id_from_message(message)
            parsed_booking = _extract_params_with_confidence(message)
            parsed_booking_params = parsed_booking.get("params") if isinstance(parsed_booking.get("params"), dict) else {}
            direct_guests = _to_int(parsed_booking_params.get("guests"), default=0)
            if direct_guests <= 0:
                direct_guests = _to_int(parsed_booking_params.get("group_size"), default=0)
            if direct_guests <= 0:
                guest_match = re.search(r"\b(\d+)\s*(adult|adults|guest|guests|people|person|pax)\b", _normalize_chat_text(message))
                if guest_match:
                    direct_guests = _to_int(guest_match.group(1), default=0)

            if direct_tour_name_hint and not direct_date_hint and not direct_sched_id and not is_tour_booking_shortcut:
                current_state["pending_tour_booking"] = {
                    "stage": "awaiting_details",
                    "active_flow": "tour_booking",
                    "tour_name_hint": direct_tour_name_hint,
                    "expected_slots": ["date", "guests"],
                }
                _save_chat_state(request, current_state)
                return _chat_json_response(
                    request,
                    start_time,
                    {
                        "fulfillmentText": (
                            f"Sure. I found {direct_tour_name_hint}. "
                            "Please share your preferred date and number of adults to continue."
                        ),
                        "quick_replies": ["May 5 for 2 adults", "show available tours"],
                    },
                )
            inferred_sched_id = direct_sched_id
            if not inferred_sched_id and selection_index > 0 and fallback_sched_ids:
                pick = selection_index - 1
                if 0 <= pick < len(fallback_sched_ids):
                    inferred_sched_id = str(fallback_sched_ids[pick] or "").strip()
            if not direct_tour_name_hint and not inferred_sched_id and selection_index <= 0 and direct_date_hint is None:
                return _chat_json_response(
                    request,
                    start_time,
                    {
                        "fulfillmentText": "Which tour would you like to book?",
                        "quick_replies": ["show available tours", "show tour schedules"],
                    },
                )

            schedule, alt_options = _resolve_schedule_for_tour_booking(
                tour_name_hint=direct_tour_name_hint,
                sched_id=inferred_sched_id,
                date_hint=direct_date_hint,
            )
            if schedule is None and alt_options:
                schedule_cards = []
                for sched in alt_options[:5]:
                    card = _build_schedule_card_trace(request, sched)
                    if card:
                        schedule_cards.append(card)
                current_state["pending_tour_booking"] = {
                    "stage": "awaiting_details",
                    "active_flow": "tour_booking",
                    "tour_name_hint": direct_tour_name_hint,
                    "expected_slots": ["schedule", "guests"],
                }
                _save_chat_state(request, current_state)
                response_payload = {
                    "fulfillmentText": "I found multiple schedule options. Please choose one schedule to continue booking.",
                    "quick_replies": ["show available tours"],
                }
                if schedule_cards:
                    response_payload["recommendation_trace"] = schedule_cards
                return _chat_json_response(request, start_time, response_payload)

            if schedule is None:
                return _chat_json_response(
                    request,
                    start_time,
                    {
                        "fulfillmentText": "I couldn't find that tour right now. You can try viewing current schedules or share a target date.",
                        "quick_replies": ["show available tours", "show tour schedules"],
                    },
                )

            if direct_guests <= 0:
                current_state["pending_tour_booking"] = {
                    "stage": "awaiting_details",
                    "active_flow": "tour_booking",
                    "tour_name_hint": str(getattr(getattr(schedule, "tour", None), "tour_name", "") or "").strip(),
                    "sched_id": str(getattr(schedule, "sched_id", "") or "").strip(),
                    "expected_slots": ["guests"],
                }
                _save_chat_state(request, current_state)
                return _chat_json_response(
                    request,
                    start_time,
                    {
                        "fulfillmentText": "Great choice. How many guests are joining?",
                        "quick_replies": ["2 adults", "3 adults", "4 adults"],
                    },
                )

            summary_state = dict(current_state)
            summary_state["pending_tour_booking"] = {
                "stage": "awaiting_confirmation",
                "active_flow": "tour_booking_confirmation",
                "tour_name_hint": str(getattr(getattr(schedule, "tour", None), "tour_name", "") or "").strip(),
                "sched_id": str(getattr(schedule, "sched_id", "") or "").strip(),
                "date_text": timezone.localtime(schedule.start_time).strftime("%B %d, %Y"),
                "guests": direct_guests,
                "expected_slots": ["confirmation"],
            }
            _save_chat_state(request, summary_state)
            return _chat_json_response(
                request,
                start_time,
                {
                    "fulfillmentText": _build_tour_booking_summary_text(schedule=schedule, guests=direct_guests),
                    "quick_replies": ["Yes", "No", "change date"],
                },
            )

    if (
        actor.get("role") in {"admin", "employee"}
        and _is_open_dashboard_command(message)
        and not (actor.get("role") == "admin" and admin_topic_hint)
        and not (actor.get("role") == "employee" and employee_topic_hint)
    ):
        request._chatbot_log_context["resolved_intent"] = "open_dashboard"
        target_name = "admin_app:admin_dashboard" if actor.get("role") == "admin" else "admin_app:employee_dashboard"
        target_label = "Open Admin Dashboard" if actor.get("role") == "admin" else "Open Employee Dashboard"
        target_reply = (
            "I found your admin dashboard. Click the button below to open it in a new tab."
            if actor.get("role") == "admin"
            else "I found your employee dashboard. Click the button below to open it in a new tab."
        )
        return _chat_json_response(
            request,
            start_time,
            _build_link_payload(
                request,
                text=target_reply,
                route_name=target_name,
                label=target_label,
            ),
        )
    if actor.get("role") == "owner" and _is_open_dashboard_command(message):
        request._chatbot_log_context["resolved_intent"] = "open_owner_dashboard"
        return _chat_json_response(
            request,
            start_time,
            _build_link_payload(
                request,
                text="I found your accommodation dashboard. Click the button below to open it in a new tab.",
                route_name="admin_app:accommodation_dashboard",
                label="Open Accommodation Dashboard",
            ),
        )

    if _is_booking_count_command(message):
        request._chatbot_log_context["resolved_intent"] = "booking_count_summary"
        summary = _build_booking_count_summary(actor, user=user, message=message)
        payload = {"fulfillmentText": summary}
        role = str(actor.get("role") or "").strip().lower()
        if role == "owner":
            payload["fulfillmentText"] = (
                "Owner booking transactions are not handled in chat.\n"
                "For Tourism Office operations, please submit or review your monthly report."
            )
            payload.update(
                _build_link_payload(
                    request,
                    text=payload["fulfillmentText"],
                    route_name="admin_app:owner_report_submit",
                    label="Open Monthly Reports",
                )
            )
            payload["quick_replies"] = [
                "Submit monthly report",
                "Open reports and analytics",
                "Open Owner Hub",
            ]
        elif role == "guest":
            payload.update(
                _build_link_payload(
                    request,
                    text=summary,
                    route_name="my_accommodation_bookings",
                    label="Open Accommodation Links",
                )
            )
        elif role == "admin":
            admin_message_text = str(message or "").strip().lower()
            admin_tour_scope = any(
                marker in admin_message_text
                for marker in ("tour booking", "tour bookings", "tour package", "tour packages", "tour reservation", "tour reservations", " tour ", " tours")
            )
            admin_accommodation_scope = any(
                marker in admin_message_text
                for marker in ("accommodation", "hotel", "inn", "room", "rooms")
            )
            admin_generic_scope = not admin_tour_scope and not admin_accommodation_scope
            payload.update(
                _build_link_payload(
                    request,
                    text=summary,
                    route_name=(
                        "tour_app:pending_view"
                        if admin_tour_scope
                        else ("admin_app:admin_dashboard" if admin_generic_scope else "admin_app:owner_reports_review")
                    ),
                    label=(
                        "Open Tour Bookings"
                        if admin_tour_scope
                        else ("Open Booking Monitoring" if admin_generic_scope else "open accommodation links")
                    ),
                )
            )
        return _chat_json_response(request, start_time, payload)

    if actor.get("role") == "owner" and _is_owner_hub_command(message):
        request._chatbot_log_context["resolved_intent"] = "open_owner_hub"
        return _chat_json_response(
            request,
            start_time,
            _build_link_payload(
                request,
                text="I found your Owner Hub. Click the button below to open it in a new tab.",
                route_name="admin_app:owner_hub",
                label="Open Owner Hub",
            ),
        )

    if actor.get("role") == "owner" and _is_owner_reports_analytics_command(message):
        request._chatbot_log_context["resolved_intent"] = "open_owner_reports_analytics"
        return _chat_json_response(
            request,
            start_time,
            _build_link_payload(
                request,
                text="I found your Reports & Analytics page. Click the button below to open it in a new tab.",
                route_name="accom_app:owner_reports_analytics",
                label="Open Reports & Analytics",
            ),
        )

    if actor.get("role") == "owner" and _is_owner_register_accommodation_command(message):
        request._chatbot_log_context["resolved_intent"] = "open_owner_accommodation_register"
        return _chat_json_response(
            request,
            start_time,
            _build_link_payload(
                request,
                text="I found the accommodation registration page. Click the button below to open it in a new tab.",
                route_name="admin_app:accommodation_register",
                label="Register New Accommodation",
            ),
        )

    if (
        actor.get("role") == "owner"
        and _is_owner_accommodation_overview_command(message)
        and not _detect_owner_support_topic(message)
    ):
        request._chatbot_log_context["resolved_intent"] = "owner_accommodation_overview"
        owner_accom_reply = _build_owner_accommodations_summary(user)
        return _chat_json_response(
            request,
            start_time,
            {
                "fulfillmentText": owner_accom_reply,
                "quick_replies": ["Show my rooms", "Submit monthly report", "Open Owner Hub"],
            },
        )

    if actor.get("role") == "owner" and _is_owner_performance_summary_command(message):
        request._chatbot_log_context["resolved_intent"] = "owner_performance_summary"
        owner_summary_reply = _build_owner_performance_summary(user)
        return _chat_json_response(
            request,
            start_time,
            {
                "fulfillmentText": owner_summary_reply,
                "quick_replies": ["Show my rooms", "Submit monthly report", "Open Owner Hub"],
            },
        )

    if actor.get("role") == "admin" and admin_topic_hint:
        request._chatbot_log_context["resolved_intent"] = admin_topic_hint
        short_admin = {"approve listing", "pending accommodations", "manage users", "admin dashboard", "booking summary"}

        if admin_topic_hint == "admin_approval_workflow":
            payload = _build_link_payload(
                request,
                text=_build_admin_approval_workflow_summary(),
                route_name="admin_app:pending_accommodation",
                label="Open Pending Accommodations",
            )
            if str(message or "").strip().lower() in short_admin:
                payload["needs_clarification"] = True
                payload["missing_slot"] = "approval_scope"
                payload["quick_replies"] = [
                    "Show pending accommodations",
                    "Show pending owner accounts",
                    "Open admin dashboard",
                ]
            return _chat_json_response(request, start_time, payload)

        if admin_topic_hint == "admin_destination_management":
            return _chat_json_response(
                request,
                start_time,
                _build_link_payload(
                    request,
                    text=_build_admin_destination_management_summary(),
                    route_name="admin_app:tourism_information_manage",
                    label="Open Tourism Information",
                ),
            )

        if admin_topic_hint == "admin_accommodation_records_management":
            return _chat_json_response(
                request,
                start_time,
                _build_link_payload(
                    request,
                    text=_build_admin_accommodation_records_summary(message),
                    route_name="admin_app:owner_reports_review",
                    label="Open Accommodation Records",
                ),
            )

        if admin_topic_hint == "admin_booking_system_monitoring":
            payload = _build_link_payload(
                request,
                text=_build_admin_booking_system_summary(),
                route_name="admin_app:owner_reports_review",
                label="Open Booking Monitoring",
            )
            if str(message or "").strip().lower() in short_admin:
                payload["needs_clarification"] = True
                payload["missing_slot"] = "booking_summary_scope"
                payload["quick_replies"] = [
                    "Show pending reservations",
                    "Show cancelled bookings",
                    "Open admin dashboard",
                ]
            return _chat_json_response(request, start_time, payload)

        if admin_topic_hint == "admin_user_account_management":
            payload = _build_link_payload(
                request,
                text=_build_admin_user_account_summary(),
                route_name="admin_app:pending_accommodation_owners",
                label="Open Owner/Account Review",
            )
            if str(message or "").strip().lower() in short_admin:
                payload["needs_clarification"] = True
                payload["missing_slot"] = "account_scope"
                payload["quick_replies"] = [
                    "Show pending owner accounts",
                    "Show employee accounts",
                    "Reset password help",
                ]
            return _chat_json_response(request, start_time, payload)

        if admin_topic_hint == "admin_reports_dashboard_support":
            payload = _build_link_payload(
                request,
                text=_build_admin_reports_dashboard_summary(),
                route_name="admin_app:admin_dashboard",
                label="Open Admin Dashboard",
            )
            if str(message or "").strip().lower() in short_admin:
                payload["needs_clarification"] = True
                payload["missing_slot"] = "report_scope"
                payload["quick_replies"] = [
                    "Show booking summary",
                    "Show tourism statistics",
                    "Open admin dashboard",
                ]
            return _chat_json_response(request, start_time, payload)

        if admin_topic_hint == "admin_activation_deactivation_help":
            return _chat_json_response(
                request,
                start_time,
                _build_link_payload(
                    request,
                    text=_build_admin_activation_deactivation_summary(),
                    route_name="admin_app:owner_reports_review",
                    label="Open Accommodation Management",
                ),
            )

        if admin_topic_hint == "admin_chatbot_activity_monitoring":
            return _chat_json_response(
                request,
                start_time,
                _build_link_payload(
                    request,
                    text=_build_admin_chatbot_activity_summary(),
                    route_name="admin_app:activity_tracker",
                    label="Open Activity Logs",
                ),
            )

        if admin_topic_hint == "admin_record_visibility_issue":
            return _chat_json_response(
                request,
                start_time,
                _build_link_payload(
                    request,
                    text=_build_admin_record_visibility_diagnostic(),
                    route_name="admin_app:admin_dashboard",
                    label="Open Admin Dashboard",
                ),
            )

    if actor.get("role") == "admin" and _is_admin_pending_accommodations_command(message):
        request._chatbot_log_context["resolved_intent"] = "admin_pending_accommodations"
        summary = _build_admin_pending_accommodations_summary()
        return _chat_json_response(
            request,
            start_time,
            _build_link_payload(
                request,
                text=summary,
                route_name="admin_app:pending_accommodation",
                label="Open Pending Accommodations",
            ),
        )

    if actor.get("role") == "admin" and _is_admin_pending_owner_accounts_command(message):
        request._chatbot_log_context["resolved_intent"] = "admin_pending_owner_accounts"
        summary = _build_admin_pending_owner_accounts_summary()
        return _chat_json_response(
            request,
            start_time,
            _build_link_payload(
                request,
                text=summary,
                route_name="admin_app:pending_accommodation_owners",
                label="Open Pending Owner Accounts",
            ),
        )

    if actor.get("role") == "admin" and _is_admin_accommodation_bookings_command(message):
        request._chatbot_log_context["resolved_intent"] = "admin_accommodation_bookings"
        return _chat_json_response(
            request,
            start_time,
            _build_link_payload(
                request,
                text="I found the Accommodation Bookings page. Click the button below to open it in a new tab.",
                route_name="admin_app:owner_reports_review",
                label="open accommodation links",
            ),
        )

    if actor.get("role") == "admin" and _is_admin_tourism_manage_command(message):
        request._chatbot_log_context["resolved_intent"] = "admin_tourism_information_manage"
        return _chat_json_response(
            request,
            start_time,
            _build_link_payload(
                request,
                text="I found the Tourism Information management page. Click the button below to open it in a new tab.",
                route_name="admin_app:tourism_information_manage",
                label="Open Tourism Information",
            ),
        )

    if actor.get("role") == "admin" and _is_admin_survey_results_command(message):
        request._chatbot_log_context["resolved_intent"] = "admin_survey_results"
        return _chat_json_response(
            request,
            start_time,
            _build_link_payload(
                request,
                text="I found the survey results dashboard. Click the button below to open it in a new tab.",
                route_name="admin_app:survey_results_dashboard",
                label="Open Survey Results",
            ),
        )

    if actor.get("role") == "admin" and _is_admin_traveler_surveys_command(message):
        request._chatbot_log_context["resolved_intent"] = "admin_traveler_surveys"
        return _chat_json_response(
            request,
            start_time,
            _build_link_payload(
                request,
                text="I found the Traveler Surveys page. Click the button below to open it in a new tab.",
                route_name="admin_app:survey_results_dashboard",
                label="Open Traveler Surveys",
            ),
        )

    if actor.get("role") == "admin" and _is_admin_tour_calendar_command(message):
        request._chatbot_log_context["resolved_intent"] = "admin_tour_calendar"
        return _chat_json_response(
            request,
            start_time,
            _build_link_payload(
                request,
                text="I found the Tour Calendar page. Click the button below to open it in a new tab.",
                route_name="admin_app:tour_calendar",
                label="Open Tour Calendar",
            ),
        )

    if actor.get("role") == "admin" and _is_admin_tour_list_command(message):
        request._chatbot_log_context["resolved_intent"] = "admin_tour_list"
        return _chat_json_response(
            request,
            start_time,
            _build_link_payload(
                request,
                text="I found the Tour List page. Click the button below to open it in a new tab.",
                route_name="tour_app:home",
                label="Open Tour List",
            ),
        )

    if actor.get("role") == "admin" and _is_admin_activity_logs_command(message):
        request._chatbot_log_context["resolved_intent"] = "admin_activity_logs"
        return _chat_json_response(
            request,
            start_time,
            _build_link_payload(
                request,
                text="I found the Activity Logs page. Click the button below to open it in a new tab.",
                route_name="admin_app:activity_tracker",
                label="Open Activity Logs",
            ),
        )

    if actor.get("role") == "admin" and _is_admin_map_command(message):
        request._chatbot_log_context["resolved_intent"] = "admin_map"
        return _chat_json_response(
            request,
            start_time,
            _build_link_payload(
                request,
                text="I found the Map page. Click the button below to open it in a new tab.",
                route_name="admin_app:map",
                label="Open Map",
            ),
        )

    if actor.get("role") == "admin" and _is_admin_discounts_command(message):
        request._chatbot_log_context["resolved_intent"] = "admin_discounts"
        return _chat_json_response(
            request,
            start_time,
            _build_link_payload(
                request,
                text="I found the Discounts page. Click the button below to open it in a new tab.",
                route_name="tour_app:admission_rate",
                label="Open Discounts",
            ),
        )

    if actor.get("role") == "employee" and _is_employee_assigned_tours_command(message) and not employee_topic_hint:
        request._chatbot_log_context["resolved_intent"] = "employee_assigned_tours"
        return _chat_json_response(
            request,
            start_time,
            _build_link_payload(
                request,
                text=_build_employee_assigned_tours_summary(request, actor),
                route_name="admin_app:employee_assigned_tours",
                label="Open Assigned Tours",
            ),
        )

    if actor.get("role") == "employee" and _is_employee_open_assignment_command(message):
        request._chatbot_log_context["resolved_intent"] = "employee_open_assignment"
        sched_id_hint = _extract_sched_id_from_message(message)
        date_hint = _extract_tour_date_hint_from_message(message)
        assignment, assignment_match_mode = _resolve_employee_assignment_row_with_meta(
            request,
            actor,
            sched_id_hint=sched_id_hint,
            target_date=date_hint,
        )
        if assignment is None:
            return _chat_json_response(
                request,
                start_time,
                {
                    "fulfillmentText": "No assigned tours found yet. Open Assigned Tours to check current assignments.",
                    "quick_replies": ["Open assigned tours", "Open dashboard"],
                },
            )
        schedule = getattr(assignment, "schedule", None)
        tour = getattr(schedule, "tour", None) if schedule is not None else None
        sched_id = str(getattr(schedule, "sched_id", "") or "").strip()
        tour_name = str(getattr(tour, "tour_name", "") or "Assigned Tour").strip()
        start_time_text = ""
        schedule_start = getattr(schedule, "start_time", None)
        if schedule_start is not None:
            start_time_text = timezone.localtime(schedule_start).strftime("%b %d, %Y %I:%M %p")
        date_hint_note = ""
        if date_hint is not None and assignment_match_mode == "date_nearest":
            opened_date = timezone.localtime(schedule_start).strftime("%B %d, %Y") if schedule_start is not None else "a nearby date"
            date_hint_note = (
                f"I didn't find an exact assignment on {date_hint.strftime('%B %d, %Y')}, "
                f"so I opened the nearest one on {opened_date}.\n"
            )
        reply = (
            f"{date_hint_note}Assignment opened: {tour_name} ({sched_id}).\n"
            + (f"Start time: {start_time_text}.\n" if start_time_text else "")
            + "You can continue this from the Assigned Tours page."
        )
        _save_chat_state(
            request,
            {
                "pending_intent": "employee_open_assignment",
                "params": {"assignment_sched_id": sched_id},
            },
        )
        if hasattr(request, "session"):
            request.session["current_assignment_id"] = str(getattr(assignment, "id", "") or "")
            request.session["current_assignment_sched_id"] = sched_id
            request.session.modified = True
        return _chat_json_response(
            request,
            start_time,
            _build_link_payload(
                request,
                text=reply,
                route_name="admin_app:employee_assigned_tours",
                label="Open Assigned Tours",
            ),
        )

    if actor.get("role") == "employee" and _is_employee_assignment_update_command(message):
        request._chatbot_log_context["resolved_intent"] = "employee_update_assignment"
        decision = "accept" if "accept" in _normalize_chat_text(message) else "decline"
        sched_id_hint = _extract_sched_id_from_message(message)
        state_for_assignment = _load_chat_state(request)
        state_params = state_for_assignment.get("params") if isinstance(state_for_assignment.get("params"), dict) else {}
        if not sched_id_hint:
            sched_id_hint = str(state_params.get("assignment_sched_id") or "").strip()
        assignment_id_hint = ""
        if hasattr(request, "session"):
            assignment_id_hint = str(request.session.get("current_assignment_id") or "").strip()
        assignment = _resolve_employee_assignment_row(request, actor, sched_id_hint=sched_id_hint)
        if assignment is None and assignment_id_hint:
            assignment = (
                TourAssignment.objects.select_related("schedule", "schedule__tour")
                .filter(id=assignment_id_hint)
                .first()
            )
        if assignment is None:
            return _chat_json_response(
                request,
                start_time,
                {
                    "fulfillmentText": "I couldn't find an active assignment yet. Open an assignment first, then send accept or decline.",
                    "quick_replies": ["Open assigned tours"],
                },
            )
        schedule = getattr(assignment, "schedule", None)
        sched_id = str(getattr(schedule, "sched_id", "") or "").strip()
        reply = (
            f"Understood. You selected {decision} for assignment {sched_id}.\n"
            "Please finalize this update in Assigned Tours so it is reflected in your operational workflow."
        )
        return _chat_json_response(
            request,
            start_time,
            _build_link_payload(
                request,
                text=reply,
                route_name="admin_app:employee_assigned_tours",
                label="Open Assigned Tours",
            ),
        )

    if actor.get("role") == "employee" and _is_employee_tour_calendar_command(message) and not employee_topic_hint:
        request._chatbot_log_context["resolved_intent"] = "employee_tour_calendar"
        return _chat_json_response(
            request,
            start_time,
            _build_link_payload(
                request,
                text="I found your tour calendar. Click the button below to open it in a new tab.",
                route_name="admin_app:employee_tour_calendar",
                label="Open Tour Calendar",
            ),
        )

    if actor.get("role") == "employee" and _is_employee_accommodations_command(message) and not employee_topic_hint:
        request._chatbot_log_context["resolved_intent"] = "employee_accommodations"
        return _chat_json_response(
            request,
            start_time,
            _build_link_payload(
                request,
                text="I found the accommodations page for employee view. Click the button below to open it in a new tab.",
                route_name="admin_app:employee_accommodations",
                label="Open Accommodations",
            ),
        )

    if actor.get("role") == "employee" and _is_employee_profile_command(message) and not employee_topic_hint:
        request._chatbot_log_context["resolved_intent"] = "employee_profile"
        return _chat_json_response(
            request,
            start_time,
            _build_link_payload(
                request,
                text="I found your profile page. Click the button below to open it in a new tab.",
                route_name="admin_app:employee_profile",
                label="Open Profile",
            ),
        )

    if actor.get("role") == "employee" and _is_employee_tour_list_command(message) and not employee_topic_hint:
        request._chatbot_log_context["resolved_intent"] = "employee_tour_list"
        return _chat_json_response(
            request,
            start_time,
            _build_link_payload(
                request,
                text="I found the Tour List page. Click the button below to open it in a new tab.",
                route_name="tour_app:home",
                label="Open Tour List",
            ),
        )

    if actor.get("role") == "employee" and _is_employee_create_tour_command(message) and not employee_topic_hint:
        request._chatbot_log_context["resolved_intent"] = "employee_create_tour"
        return _chat_json_response(
            request,
            start_time,
            _build_link_payload(
                request,
                text="I found the Create Tour page. Click the button below to open it in a new tab.",
                route_name="tour_app:add_tour",
                label="Open Create Tour",
            ),
        )

    if actor.get("role") == "employee" and _is_employee_map_command(message) and not employee_topic_hint:
        request._chatbot_log_context["resolved_intent"] = "employee_map"
        return _chat_json_response(
            request,
            start_time,
            _build_link_payload(
                request,
                text="I found the Map page. Click the button below to open it in a new tab.",
                route_name="admin_app:map",
                label="Open Map",
            ),
        )

    if actor.get("role") == "employee":
        employee_topic = employee_topic_hint
        if employee_topic:
            request._chatbot_log_context["resolved_intent"] = employee_topic
            short_inputs = {"tourist records", "pending bookings", "generate report", "listing approval", "dashboard not opening"}

            if employee_topic == "employee_account_access_help":
                payload = _build_link_payload(
                    request,
                    text=(
                        "If you forgot your password or cannot open the employee dashboard, use the password recovery page first.\n"
                        "After reset, sign in again and open the Employee Dashboard."
                    ),
                    route_name="admin_app:forgot_password",
                    label="Open Password Recovery",
                )
                if str(message or "").strip().lower() in short_inputs:
                    payload["needs_clarification"] = True
                    payload["missing_slot"] = "employee_access_issue"
                    payload["quick_replies"] = [
                        "Open employee dashboard",
                        "Reset password",
                        "Record not showing in system",
                    ]
                return _chat_json_response(request, start_time, payload)

            if employee_topic == "employee_tourist_monitoring":
                payload = _build_link_payload(
                    request,
                    text=_build_employee_tourist_monitoring_summary(message),
                    route_name="admin_app:employee_dashboard",
                    label="Open Employee Dashboard",
                )
                if str(message or "").strip().lower() in short_inputs:
                    payload["needs_clarification"] = True
                    payload["missing_slot"] = "tourist_monitoring_scope"
                    payload["quick_replies"] = [
                        "Search tourist by name",
                        "Show booking summaries",
                        "Open monitoring dashboard",
                    ]
                return _chat_json_response(request, start_time, payload)

            if employee_topic == "employee_booking_monitoring":
                payload = _build_link_payload(
                    request,
                    text=_build_employee_booking_monitoring_summary(),
                    route_name="admin_app:employee_dashboard",
                    label="Open Monitoring Dashboard",
                )
                if str(message or "").strip().lower() in short_inputs:
                    payload["needs_clarification"] = True
                    payload["missing_slot"] = "booking_status_scope"
                    payload["quick_replies"] = [
                        "Show pending reservations",
                        "Show confirmed bookings",
                        "Show cancelled reservations",
                    ]
                return _chat_json_response(request, start_time, payload)

            if employee_topic == "employee_accommodation_records":
                return _chat_json_response(
                    request,
                    start_time,
                    _build_link_payload(
                        request,
                        text=_build_employee_accommodation_records_summary(),
                        route_name="admin_app:employee_accommodations",
                        label="Open Employee Accommodations",
                    ),
                )

            if employee_topic == "employee_destination_records":
                return _chat_json_response(
                    request,
                    start_time,
                    _build_link_payload(
                        request,
                        text=_build_employee_destination_records_summary(),
                        route_name="admin_app:employee_dashboard",
                        label="Open Monitoring Dashboard",
                    ),
                )

            if employee_topic == "employee_reports_support":
                payload = _build_link_payload(
                    request,
                    text=_build_employee_reports_support_summary(),
                    route_name="admin_app:employee_dashboard",
                    label="Open Monitoring Dashboard",
                )
                if str(message or "").strip().lower() in short_inputs:
                    payload["needs_clarification"] = True
                    payload["missing_slot"] = "report_type"
                    payload["quick_replies"] = [
                        "Show booking summaries",
                        "Show tourist statistics",
                        "Show accommodation reports",
                    ]
                return _chat_json_response(request, start_time, payload)

            if employee_topic == "employee_tourist_records_workflow_help":
                return _chat_json_response(
                    request,
                    start_time,
                    _build_link_payload(
                        request,
                        text=(
                            "Tourist record workflow:\n"
                            "1) Open Employee Dashboard monitoring view.\n"
                            "2) Check active tourist records and booking signals.\n"
                            "3) Search by tourist name when needed.\n"
                            "4) Escalate approval or account-level actions to admin."
                        ),
                        route_name="admin_app:employee_dashboard",
                        label="Open Monitoring Dashboard",
                    ),
                )

            if employee_topic == "employee_workflow_listing_review":
                payload = _build_link_payload(
                    request,
                    text=(
                        "Accommodation approval decisions are admin-controlled.\n"
                        "As employee, you can review listing records and flag issues for admin action."
                    ),
                    route_name="admin_app:employee_accommodations",
                    label="Open Employee Accommodations",
                )
                if str(message or "").strip().lower() in short_inputs:
                    payload["needs_clarification"] = True
                    payload["missing_slot"] = "listing_review_scope"
                    payload["quick_replies"] = [
                        "Review submitted listings",
                        "Open monitoring dashboard",
                        "How to flag listing issue",
                    ]
                return _chat_json_response(request, start_time, payload)

            if employee_topic == "employee_workflow_destination_feedback":
                return _chat_json_response(
                    request,
                    start_time,
                    _build_link_payload(
                        request,
                        text=(
                            "You can manage destination record updates from staff workflows and escalate publishing changes for approval when needed.\n"
                            "For tourism concerns/feedback, use your notifications and monitoring workflow."
                        ),
                        route_name="admin_app:employee_notifications",
                        label="Open Employee Notifications",
                    ),
                )

            if employee_topic == "employee_record_visibility_issue":
                return _chat_json_response(
                    request,
                    start_time,
                    _build_link_payload(
                        request,
                        text=(
                            "Record visibility check:\n"
                            f"{_build_employee_tourist_monitoring_summary(message)}\n"
                            f"{_build_employee_accommodation_records_summary()}\n"
                            "If records are still missing, verify status filters, publication state, and approval status."
                        ),
                        route_name="admin_app:employee_dashboard",
                        label="Open Monitoring Dashboard",
                    ),
                )

            if employee_topic == "employee_monitoring_dashboard_help":
                return _chat_json_response(
                    request,
                    start_time,
                    _build_link_payload(
                        request,
                        text="I found the monitoring dashboard for employee operations.",
                        route_name="admin_app:employee_dashboard",
                        label="Open Monitoring Dashboard",
                    ),
                )

            if employee_topic == "employee_record_update_help":
                return _chat_json_response(
                    request,
                    start_time,
                    _build_link_payload(
                        request,
                        text=(
                            "You can update records that are allowed by your employee role in staff workflows.\n"
                            "Some approval/publish actions remain admin-controlled."
                        ),
                        route_name="admin_app:employee_dashboard",
                        label="Open Employee Dashboard",
                    ),
                )

    if actor.get("role") == "guest" and _is_guest_map_command(message):
        request._chatbot_log_context["resolved_intent"] = "guest_map"
        return _chat_json_response(
            request,
            start_time,
            _build_link_payload(
                request,
                text="I found the Bayawan City Map page. Click the button below to open it in a new tab.",
                route_name="map",
                label="Open Map",
            ),
        )

    if actor.get("role") == "guest" and _is_guest_search_help_command(message):
        request._chatbot_log_context["resolved_intent"] = "guest_search_help"
        return _chat_json_response(
            request,
            start_time,
            {
                "fulfillmentText": (
                    "To search hotels/inns in this system, send a preference sentence with location, guests, and budget.\n"
                    "Example: hotel in Suba barangay for 2 guests under 2000.\n"
                    "Then choose an option and I can share its official page."
                ),
                "quick_replies": [
                    "show approved accommodations in bayawan",
                    "hotel in suba for 2 guests under 1500",
                    "create booking preview",
                ],
            },
        )

    if actor.get("role") == "guest" and _is_guest_billing_details_help_command(message):
        request._chatbot_log_context["resolved_intent"] = "guest_billing_details_help"
        return _chat_json_response(
            request,
            start_time,
            {
                "fulfillmentText": (
                    "I can share estimated cost details after you pick a room option and guest count.\n"
                    "You'll then continue on the accommodation's official page/contact channel."
                ),
                "quick_replies": [
                    "show approved accommodations in bayawan",
                    "create booking preview",
                    "open official page",
                ],
            },
        )

    if actor.get("role") == "guest" and _is_guest_booking_review_help_command(message):
        request._chatbot_log_context["resolved_intent"] = "guest_booking_review_help"
        return _chat_json_response(
            request,
            start_time,
            _build_link_payload(
                request,
                text=(
                    "Open Accommodation Links to continue to each establishment's official page/contact channel.\n"
                    "I can also help you compare options before you open a link."
                ),
                route_name="my_accommodation_bookings",
                label="Open Accommodation Links",
            ),
        )

    if actor.get("role") == "guest" and _is_guest_password_help_command(message):
        request._chatbot_log_context["resolved_intent"] = "guest_password_help"
        return _chat_json_response(
            request,
            start_time,
            _build_link_payload(
                request,
                text=(
                    "If you forgot your password, open the Guest Login page and use the password reset option.\n"
                    "If reset is unavailable, please contact the Tourism Office for manual account recovery."
                ),
                route_name="login",
                label="Open Guest Login",
            ),
        )

    if actor.get("role") == "guest" and _is_guest_booking_cancel_support_command(message):
        request._chatbot_log_context["resolved_intent"] = "guest_booking_cancel_support"
        return _chat_json_response(
            request,
            start_time,
            _build_link_payload(
                request,
                text=(
                    "Accommodation transactions are now completed on each establishment's official page.\n"
                    "Please contact the accommodation directly for cancellation or date-change requests."
                ),
                route_name="my_accommodation_bookings",
                label="Open Accommodation Links",
            ),
        )

    if actor.get("role") == "guest" and _is_guest_booking_change_date_command(message):
        request._chatbot_log_context["resolved_intent"] = "guest_booking_change_date_support"
        return _chat_json_response(
            request,
            start_time,
            _build_link_payload(
                request,
                text=(
                    "Date changes are handled by the accommodation's official page/contact channel.\n"
                    "Open the official link and request a schedule update directly with the property."
                ),
                route_name="my_accommodation_bookings",
                label="Open Accommodation Links",
            ),
        )

    if actor.get("role") == "guest" and _is_guest_payment_methods_command(message):
        request._chatbot_log_context["resolved_intent"] = "guest_payment_methods"
        methods = [
            str(label or "").strip()
            for value, label in Billing.PAYMENT_METHOD_CHOICES
            if str(value or "").strip()
        ]
        methods_text = ", ".join(methods) if methods else "Cash, GCash, Bank Transfer, Card"
        return _chat_json_response(
            request,
            start_time,
            {
                "fulfillmentText": (
                    f"Common payment methods listed by accommodations include: {methods_text}.\n"
                    "Final payment and confirmation are handled on each accommodation's official channel."
                )
            },
        )

    if actor.get("role") == "guest" and _is_guest_down_payment_command(message):
        request._chatbot_log_context["resolved_intent"] = "guest_down_payment_policy"
        return _chat_json_response(
            request,
            start_time,
            {
                "fulfillmentText": (
                    "Down payment policy may vary per accommodation.\n"
                    "Please confirm payment terms directly on the accommodation's official page/contact channel."
                ),
            },
        )

    if actor.get("role") == "guest" and _is_guest_room_availability_command(message):
        request._chatbot_log_context["resolved_intent"] = "guest_room_availability_check"
        parsed_avail = _extract_params_with_confidence(message)
        avail_params = (
            parsed_avail.get("params") if isinstance(parsed_avail.get("params"), dict) else {}
        )
        avail_text = _build_guest_room_availability_summary(message, avail_params)
        return _chat_json_response(
            request,
            start_time,
            {
                "fulfillmentText": avail_text,
                "quick_replies": [
                    "Recommend a hotel in Bayawan for 2 guests",
                    "Show default hotel suggestions",
                    "View accommodation links",
                ],
            },
        )

    if actor.get("role") == "guest" and _is_guest_booking_requirements_command(message):
        request._chatbot_log_context["resolved_intent"] = "guest_booking_requirements_help"
        response = {
            "fulfillmentText": (
                "Great question. Booking has 2 simple steps:\n"
                "1) Choose a room first from available hotels/inns.\n"
                "2) Open the property's official page/contact channel.\n\n"
                "Required details:\n"
                "- Room reference (room type or selected option)\n"
                "- Your preferred check-in date\n"
                "- Your preferred check-out date\n"
                "- Number of guests\n\n"
                "Optional details for better matching:\n"
                "- Budget\n"
                "- Preferred location\n"
                "- Amenities (Wi-Fi, aircon, etc.)\n\n"
                "I can then open the property's official page so you can complete booking directly with them."
            ),
            "quick_replies": [
                "show approved accommodations in bayawan",
                "show rooms",
                "create booking preview",
            ],
        }
        return _chat_json_response(
            request,
            start_time,
            response,
        )

    if _is_reset_command(message):
        _clear_chat_state(request)
        request._chatbot_log_context["resolved_intent"] = "reset_command"
        return _chat_json_response(
            request,
            start_time,
            {
                "fulfillmentText": (
                    "Conversation context cleared. You can start a new hotel/inn request anytime."
                )
            },
        )

    if _is_default_accommodation_suggestions_command(message):
        request._chatbot_log_context["resolved_intent"] = "get_accommodation_recommendation_init"
        if actor.get("role") != "guest":
            help_payload = _build_role_help_payload(actor)
            return _chat_json_response(request, start_time, help_payload)
        suggestions_payload = _get_default_accommodation_suggestions(limit=3)
        default_reply = None
        recommendation_trace = []
        if isinstance(suggestions_payload, tuple):
            default_reply, recommendation_trace = suggestions_payload
        else:
            default_reply = suggestions_payload

        response = {"fulfillmentText": default_reply}
        if recommendation_trace:
            response["recommendation_trace"] = recommendation_trace
            response["quick_replies"] = _merge_quick_replies(
                response.get("quick_replies") if isinstance(response.get("quick_replies"), list) else [],
                _build_recommendation_assist_quick_replies(
                    _build_accommodation_selection_cache(recommendation_trace)
                ),
                limit=4,
            )
            _inject_recommendation_context(
                response,
                params={},
                default_summary="based on currently available hotels/inns (no preference filter yet)",
            )

        _save_chat_state(
            request,
            {
                "pending_intent": "get_accommodation_recommendation",
                "params": {},
                "missing_slot": "",
                "last_accommodation_recommendations": _build_accommodation_selection_cache(recommendation_trace),
            },
        )
        return _chat_json_response(request, start_time, response)

    if _is_my_accommodation_booking_status_command(message):
        request._chatbot_log_context["resolved_intent"] = "view_my_accommodation_bookings"
        response_payload = {}
        if actor.get("role") == "owner":
            my_bookings_url = reverse("admin_app:owner_report_submit")
            booking_label = "Open Monthly Reports (Owner)"
            booking_reply = "I found your owner reporting page. Click the button below to open it in a new tab."
        elif actor.get("role") == "admin":
            if _is_accommodation_bookings_page_command(message):
                my_bookings_url = reverse("admin_app:owner_reports_review")
                booking_label = "Open Owner Reports Review"
                booking_reply = "I found the owner reports review page. Click the button below to open it in a new tab."
            else:
                my_bookings_url = reverse("admin_app:admin_dashboard")
                booking_label = "Open Admin Dashboard"
                booking_reply = "I found your admin dashboard. Click the button below to open it in a new tab."
        elif actor.get("role") == "employee":
            my_bookings_url = reverse("admin_app:employee_dashboard")
            booking_label = "Open Employee Dashboard"
            booking_reply = "I found your employee dashboard. Click the button below to open it in a new tab."
        else:
            my_bookings_url = reverse("my_accommodation_bookings")
            booking_label = "Open Accommodation Links"
            booking_reply = "I found the accommodation links page. Click the button below to open official page/contact options."
        if hasattr(request, "build_absolute_uri"):
            my_bookings_url = request.build_absolute_uri(my_bookings_url)
        response_payload = {
            "fulfillmentText": booking_reply,
            "billing_link": my_bookings_url,
            "billing_link_label": booking_label,
            "open_in_new_tab": True,
        }
        return _chat_json_response(
            request,
            start_time,
            response_payload,
        )

    if (
        actor.get("role") == "owner"
        and _is_owner_room_overview_command(message)
        and not _detect_owner_support_topic(message)
    ):
        request._chatbot_log_context["resolved_intent"] = "owner_room_overview"
        owner_room_reply = _build_owner_rooms_summary(user)
        return _chat_json_response(
            request,
            start_time,
            {"fulfillmentText": owner_room_reply},
        )

    if actor.get("role") == "owner":
        owner_topic = _detect_owner_support_topic(message)
        if owner_topic:
            request._chatbot_log_context["resolved_intent"] = owner_topic
            if owner_topic == "owner_password_help":
                return _chat_json_response(
                    request,
                    start_time,
                    _build_link_payload(
                        request,
                        text=(
                            "If you forgot your owner password, open the admin/owner password recovery page.\n"
                            "After reset, log in again and continue in Owner Hub."
                        ),
                        route_name="admin_app:forgot_password",
                        label="Open Password Recovery",
                    ),
                )
            if owner_topic in {"owner_register_listing", "owner_listing_requirements"}:
                return _chat_json_response(
                    request,
                    start_time,
                    _build_link_payload(
                        request,
                        text=(
                            "To register your accommodation, open the registration form and submit complete business/profile details.\n"
                            "Required fields are validated directly in the form before submission."
                        ),
                        route_name="admin_app:accommodation_register",
                        label="Open Accommodation Registration",
                    ),
                )
            if owner_topic == "owner_listing_update":
                request._chatbot_log_context["resolved_intent"] = "owner_listing_visibility"
                return _chat_json_response(
                    request,
                    start_time,
                    _build_link_payload(
                        request,
                        text=(
                            "Use Owner Hub to update your accommodation links and images.\n"
                            "You can edit: official website, Facebook page, external booking page, and accommodation photos."
                        ),
                        route_name="admin_app:owner_hub",
                        label="Open Owner Hub",
                    ),
                )
            if owner_topic in {
                "owner_add_room",
                "owner_update_room_price",
                "owner_update_room_capacity",
                "owner_mark_room_unavailable",
                "owner_edit_room_amenities",
                "owner_update_availability",
                "owner_block_dates",
                "owner_room_still_available_issue",
                "owner_room_update_issue",
            }:
                detail_map = {
                    "owner_add_room": "To add a new room, open Manage Rooms and click Add Room.",
                    "owner_update_room_price": "To update room price, open Manage Rooms then edit the room pricing fields.",
                    "owner_update_room_capacity": "To update room capacity, open Manage Rooms then edit the room guest capacity.",
                    "owner_mark_room_unavailable": "To mark a room unavailable or under maintenance, open Manage Rooms and set room status to UNAVAILABLE.",
                    "owner_edit_room_amenities": "Room amenities are managed through your room/listing details in Owner Hub and Manage Rooms.",
                    "owner_update_availability": "To update room availability, edit room status/availability values in Manage Rooms.",
                    "owner_block_dates": (
                        "Date-blocking per room is handled via room availability/status workflow in this version.\n"
                        "Set room as UNAVAILABLE for maintenance windows and reopen after."
                    ),
                    "owner_room_still_available_issue": (
                        "If a room still appears available, verify room status, current availability, and accepted listing status.\n"
                        "Then refresh Manage Rooms and save updates."
                    ),
                    "owner_room_update_issue": (
                        "If room update is failing, verify your listing is accepted and the room belongs to your account.\n"
                        "Then retry from Manage Rooms."
                    ),
                }
                short_owner_texts = {"add room", "update price", "listing not showing", "room unavailable how"}
                payload = _build_owner_manage_rooms_link_payload(
                    request,
                    text=str(detail_map.get(owner_topic) or "Open Manage Rooms to continue."),
                    label="Open Manage Rooms",
                )
                if str(message or "").strip().lower() in short_owner_texts:
                    payload["needs_clarification"] = True
                    payload["missing_slot"] = "owner_room_action"
                    payload["quick_replies"] = [
                        "Update room price",
                        "Change room capacity",
                        "Mark room unavailable",
                        "Submit monthly report",
                    ]
                return _chat_json_response(request, start_time, payload)
            if owner_topic == "owner_submit_monthly_report":
                return _chat_json_response(
                    request,
                    start_time,
                    _build_link_payload(
                        request,
                        text=(
                            "Owner reporting is handled through monthly tourism reports.\n"
                            "Open the report page to submit or update your monthly data.\n"
                            "Please enter check-ins per room so the Tourism Office can monitor usage per room type."
                        ),
                        route_name="admin_app:owner_report_submit",
                        label="Open Monthly Reports",
                    ),
                )
            if owner_topic == "owner_available_rooms_today":
                return _chat_json_response(
                    request,
                    start_time,
                    _build_owner_manage_rooms_link_payload(
                        request,
                        text=_build_owner_available_rooms_today_summary(user),
                        label="Open Manage Rooms",
                    ),
                )
            if owner_topic == "owner_listing_status":
                return _chat_json_response(
                    request,
                    start_time,
                    _build_link_payload(
                        request,
                        text=_build_owner_listing_status_summary(user),
                        route_name="admin_app:owner_hub",
                        label="Open Owner Hub",
                    ),
                )
            if owner_topic == "owner_listing_visibility":
                payload = _build_link_payload(
                    request,
                    text=_build_owner_listing_visibility_diagnostic(user),
                    route_name="admin_app:owner_hub",
                    label="Open Owner Hub",
                )
                if str(message or "").strip().lower() == "listing not showing":
                    payload["needs_clarification"] = True
                    payload["missing_slot"] = "listing_status_context"
                    payload["quick_replies"] = [
                        "Show my accommodations",
                        "Show my rooms",
                        "Open Owner Hub",
                    ]
                return _chat_json_response(
                    request,
                    start_time,
                    payload,
                )

    chat_state = _load_chat_state(request)
    chat_state_params = chat_state.get("params") if isinstance(chat_state.get("params"), dict) else {}
    chat_state_pending = str(chat_state.get("pending_intent") or "").strip().lower()
    chat_state_missing = str(chat_state.get("missing_slot") or "").strip().lower()
    chat_state_provenance = (
        request._chatbot_log_context.get("provenance")
        if isinstance(request._chatbot_log_context.get("provenance"), dict)
        else {}
    )
    if isinstance(chat_state_provenance, dict):
        active_flow = ""
        pending_tour_state = (
            chat_state.get("pending_tour_booking")
            if isinstance(chat_state.get("pending_tour_booking"), dict)
            else {}
        )

    if actor.get("role") == "guest" and (
        _is_tour_schedule_request(message) or _is_tour_schedule_request(raw_message)
    ):
        request._chatbot_log_context["resolved_intent"] = "get_recommendation"
        schedule_payload = _build_tour_schedule_listing_payload(request, message, chat_state_params)
        response_payload = {
            "fulfillmentText": str(schedule_payload.get("reply") or "Here are available tour schedules right now.").strip(),
        }
        if isinstance(schedule_payload.get("items"), list) and schedule_payload.get("items"):
            response_payload["recommendation_trace"] = schedule_payload.get("items")
        if isinstance(schedule_payload.get("quick_replies"), list):
            response_payload["quick_replies"] = _sanitize_quick_replies(schedule_payload.get("quick_replies"), limit=4)
        sched_ids = schedule_payload.get("sched_ids") if isinstance(schedule_payload.get("sched_ids"), list) else []
        next_state = dict(chat_state)
        next_state.pop("pending_tour_booking", None)
        if sched_ids:
            next_state["last_tour_recommendation_sched_ids"] = sched_ids[:8]
        next_state["pending_intent"] = "get_recommendation"
        next_state["params"] = chat_state_params if isinstance(chat_state_params, dict) else {}
        _save_chat_state(request, next_state)
        return _chat_json_response(request, start_time, response_payload)

    if (
        actor.get("role") == "guest"
        and _is_likely_gibberish_query(message)
        and not isinstance(chat_state.get("pending_tour_booking"), dict)
    ):
        role_clarifier = _role_aware_clarification_payload(actor)
        request._chatbot_log_context["resolved_intent"] = "clarification"
        request._chatbot_log_context["fallback_used"] = False
        _save_chat_state(
            request,
            {
                "pending_intent": "clarification",
                "params": chat_state_params if isinstance(chat_state_params, dict) else {},
                "missing_slot": "clarification",
            },
        )
        return _chat_json_response(
            request,
            start_time,
            {
                "fulfillmentText": (
                    "I'm not sure I understood your request. You can ask me about tours, approved accommodations, directions, trip planning, or booking previews."
                ),
                "quick_replies": _sanitize_quick_replies(role_clarifier.get("quick_replies"), limit=4),
                "needs_clarification": True,
                "missing_slot": "clarification",
            },
        )

    if (
        actor.get("role") == "guest"
        and _is_out_of_scope_message(message)
        and not _has_strict_intent_signal(actor=actor, message=message)
    ):
        request._chatbot_log_context["resolved_intent"] = "out_of_scope"
        request._chatbot_log_context["fallback_used"] = False
        return _chat_json_response(
            request,
            start_time,
            _build_out_of_scope_payload(actor, message=message),
        )

    if actor.get("role") == "guest" and _contains_any_phrase(
        message,
        ("what can i do there", "things to do there", "what can i do in bayawan", "things to do in bayawan"),
    ):
        request._chatbot_log_context["resolved_intent"] = "get_recommendation"
        _safe_log_recommendation_event(request, "get_recommendation")
        try:
            reply, logged_items = _get_recommendations({})
        except Exception:
            reply, logged_items = ("No tours available right now.", [])
        response = {"fulfillmentText": str(reply or "No tours available right now.")}
        if logged_items:
            response["recommendation_trace"] = logged_items
        rec_sched_ids = []
        for item in logged_items:
            if not isinstance(item, dict):
                continue
            meta = item.get("meta") if isinstance(item.get("meta"), dict) else {}
            sched_id = str(item.get("sched_id") or meta.get("sched_id") or "").strip()
            if sched_id:
                rec_sched_ids.append(sched_id)
        if rec_sched_ids:
            chat_state["last_tour_recommendation_sched_ids"] = rec_sched_ids[:5]
            _save_chat_state(request, chat_state)
        return _chat_json_response(request, start_time, response)

    # Phase A: hard directions override before any continuation/slot-filling branches.
    if actor.get("role") == "guest" and _is_travel_guidance_request(message):
        combo_parse = _extract_params_with_confidence(message)
        combo_params = combo_parse.get("params") if isinstance(combo_parse.get("params"), dict) else {}
        if _has_accommodation_plus_direction_mix(message, combo_params):
            missing_slot, question = _next_accommodation_clarifying_question(combo_params)
            next_state = {
                "pending_intent": "get_accommodation_recommendation",
                "params": combo_params,
                "missing_slot": missing_slot or "",
            }
            _save_chat_state(request, next_state)
            if missing_slot:
                return _chat_json_response(
                    request,
                    start_time,
                    {
                        "fulfillmentText": (
                            f"{question}\nAfter that, I can also guide you with distance or directions."
                        ),
                        "quick_replies": _slot_quick_replies(missing_slot),
                        "needs_clarification": True,
                        "missing_slot": missing_slot,
                    },
                )

        request._chatbot_log_context["resolved_intent"] = "travel_guidance"
        travel_params = {}
        if _is_contextual_direction_followup(message):
            explicit_destination = _resolve_destination_for_travel(message, {})
            explicit_name = str(explicit_destination.get("name") or "").strip()
            explicit_kind = str(explicit_destination.get("kind") or "").strip().lower()
            if explicit_name and explicit_kind in {"accommodation", "tourist_spot", "place"}:
                travel_params["accom_name"] = explicit_name
                request._chatbot_log_context["provenance"]["context_reply_used"] = True
                request._chatbot_log_context["provenance"]["last_selected_entity"] = explicit_name[:120]
            else:
                recent_choices = _recent_accommodation_choices(chat_state, limit=3)
                if len(recent_choices) == 1:
                    choice = recent_choices[0]
                    travel_params["accom_name"] = str(choice.get("name") or "").strip()
                    if str(choice.get("location") or "").strip():
                        travel_params["location"] = str(choice.get("location") or "").strip()
                    request._chatbot_log_context["provenance"]["context_reply_used"] = True
                    request._chatbot_log_context["provenance"]["last_selected_entity"] = str(choice.get("name") or "")[:120]
                elif len(recent_choices) > 1:
                    a = recent_choices[0]
                    b = recent_choices[1]
                    return _chat_json_response(
                        request,
                        start_time,
                        {
                            "fulfillmentText": (
                                f"Which place do you mean: {a.get('name')} or {b.get('name')}?"
                            ),
                            "quick_replies": [
                                str(a.get("name") or ""),
                                str(b.get("name") or ""),
                            ],
                            "needs_clarification": True,
                            "missing_slot": "direction_target",
                        },
                    )
                elif isinstance(chat_state_params, dict):
                    if str(chat_state_params.get("location") or "").strip():
                        travel_params["location"] = str(chat_state_params.get("location") or "").strip()
                        request._chatbot_log_context["provenance"]["context_reply_used"] = True
            if not travel_params and isinstance(chat_state_params, dict):
                if str(chat_state_params.get("location") or "").strip():
                    travel_params["location"] = str(chat_state_params.get("location") or "").strip()
                    request._chatbot_log_context["provenance"]["context_reply_used"] = True
        guidance = _build_travel_guidance_payload(message, travel_params, client_location)
        guidance_reply = str(guidance.get("reply") or "").strip() or "I can guide you with directions. Please share your destination."
        guidance_response = {"fulfillmentText": guidance_reply}
        if isinstance(guidance.get("quick_replies"), list):
            guidance_response["quick_replies"] = _sanitize_quick_replies(guidance.get("quick_replies"), limit=4)
        if guidance.get("link"):
            guidance_response["billing_link"] = str(guidance.get("link"))
            guidance_response["billing_link_label"] = str(guidance.get("link_label") or "Open Map")
        return _chat_json_response(request, start_time, guidance_response)

    pending_tour_booking = (
        chat_state.get("pending_tour_booking") if isinstance(chat_state.get("pending_tour_booking"), dict) else {}
    )
    if actor.get("role") == "guest" and pending_tour_booking:
        if isinstance(request._chatbot_log_context.get("provenance"), dict):
            request._chatbot_log_context["provenance"]["context_reply_used"] = True
        booking_stage = str(pending_tour_booking.get("stage") or "").strip().lower()
        pending_tour_name = str(pending_tour_booking.get("tour_name_hint") or "").strip()
        pending_date_text = str(pending_tour_booking.get("date_text") or "").strip()
        pending_guests = _to_int(pending_tour_booking.get("guests"), default=0)
        pending_sched_id = str(pending_tour_booking.get("sched_id") or "").strip()
        normalized_message = _normalize_chat_text(message)
        if _contains_any_phrase(normalized_message, ("cancel", "stop booking", "never mind")):
            _clear_chat_state(request)
            return _chat_json_response(
                request,
                start_time,
                {
                    "fulfillmentText": "Tour booking flow cancelled. You can ask for available tours anytime.",
                    "quick_replies": ["show available tours", "show tour schedules"],
                },
            )
        if _contains_any_phrase(
            normalized_message,
            ("show available tours", "show tours", "tour packages", "show tour schedules", "view tour schedules"),
        ):
            _clear_chat_state(request)
            if _is_tour_schedule_request(normalized_message):
                schedule_payload = _build_tour_schedule_listing_payload(request, message, {})
                response_payload = {
                    "fulfillmentText": str(schedule_payload.get("reply") or "Here are available tour schedules right now."),
                }
                if isinstance(schedule_payload.get("items"), list) and schedule_payload.get("items"):
                    response_payload["recommendation_trace"] = schedule_payload.get("items")
                if isinstance(schedule_payload.get("quick_replies"), list):
                    response_payload["quick_replies"] = _sanitize_quick_replies(schedule_payload.get("quick_replies"), limit=4)
                return _chat_json_response(request, start_time, response_payload)
            rec_reply, rec_items = _get_recommendations({})
            response_payload = {"fulfillmentText": str(rec_reply or "No tours available right now.")}
            if rec_items:
                response_payload["recommendation_trace"] = rec_items
            return _chat_json_response(request, start_time, response_payload)
        if booking_stage == "awaiting_confirmation" and _contains_any_phrase(
            normalized_message,
            ("change guests", "change guest", "update guests", "change date", "update date"),
        ):
            keep_date = pending_date_text
            keep_guests = pending_guests
            expect_slots = ["date", "guests"]
            prompt = "Sure. Please share your updated date and number of adults."
            if _contains_any_phrase(normalized_message, ("change guests", "change guest", "update guests")):
                expect_slots = ["guests"]
                prompt = "Sure. How many guests are joining now?"
            elif _contains_any_phrase(normalized_message, ("change date", "update date")):
                expect_slots = ["date"]
                prompt = "Sure. What date or schedule would you prefer?"
            refreshed_state = dict(chat_state)
            refreshed_state["pending_tour_booking"] = {
                "stage": "awaiting_details",
                "active_flow": "tour_booking",
                "tour_name_hint": pending_tour_name,
                "sched_id": pending_sched_id,
                "date_text": keep_date,
                "guests": keep_guests,
                "expected_slots": expect_slots,
            }
            _save_chat_state(request, refreshed_state)
            return _chat_json_response(
                request,
                start_time,
                {
                    "fulfillmentText": prompt,
                    "quick_replies": ["2 adults", "3 adults", "May 10 for 2 adults"],
                },
            )
        if booking_stage == "awaiting_confirmation" and _is_personalization_accept_message(normalized_message):
            request._chatbot_log_context["resolved_intent"] = "book_tour_via_link"
            schedule = None
            if pending_sched_id:
                schedule = (
                    Tour_Schedule.objects.select_related("tour")
                    .filter(
                        sched_id__iexact=pending_sched_id,
                        tour__publication_status="published",
                    )
                    .exclude(status="cancelled")
                    .first()
                )
            if schedule is None and pending_tour_name and pending_date_text:
                parsed_date = _extract_tour_date_hint_from_message(pending_date_text)
                schedule, _alt = _resolve_schedule_for_tour_booking(
                    tour_name_hint=pending_tour_name,
                    date_hint=parsed_date,
                )
            if schedule is None:
                _clear_chat_state(request)
                return _chat_json_response(
                    request,
                    start_time,
                    {
                        "fulfillmentText": "I couldn't finalize that schedule right now. Please choose a schedule and try again.",
                        "quick_replies": ["show tour schedules", "show available tours"],
                    },
                )
            submit_result = _submit_guest_tour_booking_request(
                request,
                user,
                schedule=schedule,
                guests=max(1, pending_guests),
            )
            _clear_chat_state(request)
            if not submit_result.get("ok"):
                return _chat_json_response(
                    request,
                    start_time,
                    {
                        "fulfillmentText": str(submit_result.get("error") or "I couldn't submit the booking request right now."),
                        "quick_replies": ["show tour schedules", "show available tours"],
                    },
                )
            bookings_url = reverse("main-page") + "#user-bookings"
            if hasattr(request, "build_absolute_uri"):
                bookings_url = request.build_absolute_uri(bookings_url)
            has_assignment = _to_int(submit_result.get("assignment_count"), default=0) > 0
            status_tail = (
                "Please wait for approval before completing payment."
                if has_assignment
                else "Your request is saved. Staff assignment may still be in progress before review."
            )
            payload = {
                "fulfillmentText": (
                    "Your tour booking request has been submitted and is now pending review. "
                    "A confirmation email has been sent to your registered email address.\n"
                    + status_tail
                ),
                "billing_link": bookings_url,
                "billing_link_label": "View My Tour Bookings",
                "link_actions": [
                    {"label": "View My Tour Bookings", "url": bookings_url},
                ],
                "quick_replies": ["show available tours", "show tour schedules"],
                "response_nlg_source": "backend_structured_template",
            }
            return _chat_json_response(request, start_time, payload)
        if booking_stage == "awaiting_confirmation" and _is_personalization_decline_message(normalized_message):
            request._chatbot_log_context["resolved_intent"] = "book_tour_via_link"
            _clear_chat_state(request)
            return _chat_json_response(
                request,
                start_time,
                {
                    "fulfillmentText": "No problem. Tell me your preferred date and number of adults, and I'll prepare the booking flow again.",
                    "quick_replies": ["show available tours", "book tour for May 5"],
                },
            )

        parsed_booking = _extract_params_with_confidence(message)
        parsed_booking_params = parsed_booking.get("params") if isinstance(parsed_booking.get("params"), dict) else {}
        date_hint = _extract_tour_date_hint_from_message(message)
        guest_hint = _to_int(parsed_booking_params.get("guests"), default=0)
        expected_slots = (
            pending_tour_booking.get("expected_slots")
            if isinstance(pending_tour_booking.get("expected_slots"), list)
            else ["date", "guests"]
        )
        if guest_hint <= 0:
            guest_hint = _to_int(parsed_booking_params.get("group_size"), default=0)
        if guest_hint <= 0:
            guest_match = re.search(r"\b(\d+)\s*(adult|adults|guest|guests|people|person|pax)\b", normalized_message)
            if guest_match:
                guest_hint = _to_int(guest_match.group(1), default=0)
        if guest_hint <= 0:
            # Handle compact phrasing like "May 10 for 2".
            guest_for_match = re.search(r"\bfor\s+(\d+)\b", normalized_message)
            if guest_for_match:
                guest_hint = _to_int(guest_for_match.group(1), default=0)

        if date_hint is None and str(pending_date_text or "").strip():
            date_hint = _extract_tour_date_hint_from_message(pending_date_text)
        if guest_hint <= 0 and pending_guests > 0:
            guest_hint = pending_guests

        need_date = "date" in expected_slots
        need_guests = "guests" in expected_slots
        missing_date = need_date and date_hint is None
        missing_guests = need_guests and guest_hint <= 0
        if missing_date or missing_guests:
            request._chatbot_log_context["resolved_intent"] = "book_tour_via_link"
            if missing_date and missing_guests:
                missing_text = "please share your preferred date/schedule and number of adults."
                missing_replies = ["May 5 for 2 adults", "May 12 for 2 adults", "show available tours"]
            elif missing_date:
                missing_text = "please share your preferred tour date or schedule."
                missing_replies = ["May 5", "show tour schedules", "show available tours"]
            else:
                missing_text = "please share the number of adults."
                missing_replies = ["2 adults", "3 adults", "show available tours"]
            refreshed_state = dict(chat_state)
            refreshed_state["pending_tour_booking"] = {
                "stage": "awaiting_details",
                "active_flow": "tour_booking",
                "tour_name_hint": pending_tour_name,
                "sched_id": pending_sched_id,
                "expected_slots": ["date", "guests"],
            }
            _save_chat_state(request, refreshed_state)
            return _chat_json_response(
                request,
                start_time,
                {
                    "fulfillmentText": f"To continue booking {pending_tour_name}, {missing_text}",
                    "quick_replies": missing_replies,
                },
            )

        schedule, alt_options = _resolve_schedule_for_tour_booking(
            tour_name_hint=pending_tour_name,
            sched_id=pending_sched_id,
            date_hint=date_hint,
        )
        if schedule is None:
            schedule_items = []
            for sched in alt_options[:5]:
                card = _build_schedule_card_trace(request, sched)
                if card:
                    schedule_items.append(card)
            response_payload = {
                "fulfillmentText": (
                    f"I found multiple schedules for {pending_tour_name or 'that tour'}. "
                    "Please choose one schedule to continue."
                ),
                "quick_replies": ["show available tours"],
            }
            if schedule_items:
                response_payload["recommendation_trace"] = schedule_items
            refreshed_state = dict(chat_state)
            refreshed_state["pending_tour_booking"] = {
                "stage": "awaiting_details",
                "active_flow": "tour_booking",
                "tour_name_hint": pending_tour_name,
                "expected_slots": ["schedule", "guests"],
            }
            _save_chat_state(request, refreshed_state)
            return _chat_json_response(request, start_time, response_payload)

        date_text = timezone.localtime(schedule.start_time).strftime("%B %d, %Y")
        request._chatbot_log_context["resolved_intent"] = "book_tour_via_link"
        confirm_state = dict(chat_state)
        confirm_state["pending_tour_booking"] = {
            "stage": "awaiting_confirmation",
            "active_flow": "tour_booking_confirmation",
            "tour_name_hint": str(getattr(getattr(schedule, "tour", None), "tour_name", "") or pending_tour_name),
            "sched_id": str(getattr(schedule, "sched_id", "") or "").strip(),
            "date_text": date_text,
            "guests": guest_hint,
            "expected_slots": ["confirmation"],
        }
        _save_chat_state(request, confirm_state)
        return _chat_json_response(
            request,
            start_time,
            {
                "fulfillmentText": _build_tour_booking_summary_text(
                    schedule=schedule,
                    guests=guest_hint,
                ),
                "quick_replies": ["Yes", "No", "change date"],
            },
        )

    pending_booking = (
        chat_state.get("pending_booking") if isinstance(chat_state.get("pending_booking"), dict) else {}
    )
    pending_booking_params = (
        pending_booking.get("params") if isinstance(pending_booking.get("params"), dict) else {}
    )
    pending_booking_created_at = _to_int(pending_booking.get("created_at"), default=0)
    cached_accommodation_rows_early = (
        chat_state.get("last_accommodation_recommendations")
        if isinstance(chat_state.get("last_accommodation_recommendations"), list)
        else []
    )
    state_intent_hint = str(chat_state.get("pending_intent") or "").strip().lower()
    state_params_hint = chat_state.get("params") if isinstance(chat_state.get("params"), dict) else {}
    assistant_memory = _load_assistant_memory(request)
    if (
        actor.get("role") == "guest"
        and not state_intent_hint
        and _looks_like_assistant_followup(message)
        and isinstance(assistant_memory, dict)
    ):
        topic = str(assistant_memory.get("active_topic") or "").strip().lower()
        topic_to_intent = {
            "planning": "plan_bayawan_stay",
            "accommodations": "get_accommodation_recommendation",
            "directions": "travel_guidance",
            "tours": "get_recommendation",
        }
        resumed_intent = topic_to_intent.get(topic, "")
        if resumed_intent:
            resumed_params = _assistant_memory_to_params(assistant_memory)
            chat_state = dict(chat_state)
            chat_state["pending_intent"] = resumed_intent
            chat_state["params"] = resumed_params
            chat_state["missing_slot"] = str(chat_state.get("missing_slot") or "")
            state_intent_hint = resumed_intent
            state_params_hint = resumed_params
    cached_accommodation_rows = (
        chat_state.get("last_accommodation_recommendations")
        if isinstance(chat_state.get("last_accommodation_recommendations"), list)
        else []
    )

    if actor.get("role") == "guest" and _is_accommodation_room_listing_command(message):
        request._chatbot_log_context["resolved_intent"] = "get_accommodation_room_listing"
        target_name = _extract_accommodation_name_for_room_listing(
            message,
            cached_accommodation_rows,
            state_params=state_params_hint,
        )
        room_listing_payload = _build_room_listing_response_for_accommodation(target_name, limit=5)
        if isinstance(room_listing_payload.get("recommendation_trace"), list):
            next_state = dict(chat_state)
            next_state["last_accommodation_recommendations"] = _build_accommodation_selection_cache(
                room_listing_payload.get("recommendation_trace")
            )
            next_params = dict(next_state.get("params") if isinstance(next_state.get("params"), dict) else {})
            selected_accom_id = _to_int(room_listing_payload.get("selected_accommodation_id"), default=0)
            selected_accom_name = str(room_listing_payload.get("selected_accommodation_name") or "").strip()
            if selected_accom_id > 0:
                next_params["selected_accommodation_id"] = selected_accom_id
            if selected_accom_name:
                next_params["selected_accommodation_name"] = selected_accom_name
                next_params["accom_name"] = selected_accom_name
            next_state["params"] = next_params
            # Switching to explicit room browsing should discard stale preview locks.
            next_state.pop("pending_booking", None)
            _save_chat_state(request, next_state)
        return _chat_json_response(request, start_time, room_listing_payload)

    if actor.get("role") == "guest" and _is_accommodation_detail_query(message):
        request._chatbot_log_context["resolved_intent"] = "get_accommodation_details"
        detail_payload = _build_guest_room_detail_payload(message, cached_accommodation_rows)
        return _chat_json_response(request, start_time, detail_payload)

    if actor.get("role") == "guest" and _is_forget_preferences_command(message):
        _clear_saved_chat_preferences(request)
        return _chat_json_response(
            request,
            start_time,
            {
                "fulfillmentText": (
                    "Done. I cleared your saved accommodation preferences for this account."
                ),
                "quick_replies": [
                    "Show default hotel suggestions",
                    "Recommend a hotel in Bayawan",
                ],
            },
        )

    if actor.get("role") == "guest" and _is_remember_preferences_command(message):
        parsed_pref = _extract_params_with_confidence(message)
        parsed_pref_params = (
            parsed_pref.get("params") if isinstance(parsed_pref.get("params"), dict) else {}
        )
        merged_pref_params = dict(state_params_hint)
        merged_pref_params.update(parsed_pref_params)
        payload = _extract_memory_preference_payload(merged_pref_params)
        if not payload:
            return _chat_json_response(
                request,
                start_time,
                {
                    "fulfillmentText": (
                        "I can remember your preferences, but I need at least one detail first "
                        "(type, location, budget, guests, or preference tags)."
                    )
                },
            )
        _save_saved_chat_preferences(request, payload)
        summary_parts = []
        if str(payload.get("company_type") or "").strip():
            summary_parts.append(f"type: {payload.get('company_type')}")
        if str(payload.get("location") or "").strip():
            summary_parts.append(f"location: {payload.get('location')}")
        if _to_int(payload.get("budget"), default=0) > 0:
            summary_parts.append(f"budget: PHP {_to_int(payload.get('budget'), default=0)}")
        if _to_int(payload.get("guests"), default=0) > 0:
            summary_parts.append(f"guests: {_to_int(payload.get('guests'), default=0)}")
        if isinstance(payload.get("preference_tags"), list) and payload.get("preference_tags"):
            summary_parts.append(f"preferences: {', '.join(str(v) for v in payload.get('preference_tags')[:4])}")
        summary_text = "; ".join(summary_parts) if summary_parts else "basic preferences saved"
        return _chat_json_response(
            request,
            start_time,
            {
                "fulfillmentText": f"Saved. I will reuse these defaults in your next requests: {summary_text}.",
                "quick_replies": [
                    "Show default hotel suggestions",
                    "Recommend a hotel",
                    "Forget my preferences",
                ],
            },
        )

    if pending_booking_params:
        if pending_booking_created_at > 0 and (int(time.time()) - pending_booking_created_at) > _PENDING_BOOKING_TTL_SECONDS:
            expired_state = dict(chat_state)
            expired_state.pop("pending_booking", None)
            _save_chat_state(request, expired_state)
        else:
            request._chatbot_log_context["resolved_intent"] = "book_accommodation_preview"
            normalized_followup = _normalize_chat_text(message)
            parsed_followup = _extract_params_with_confidence(message)
            followup_params = (
                parsed_followup.get("params") if isinstance(parsed_followup.get("params"), dict) else {}
            )
            followup_intent_hint = _intent_from_message(message)
            followup_check_in = str(followup_params.get("check_in") or "").strip()
            followup_check_out = str(followup_params.get("check_out") or "").strip()
            followup_nights = _to_int(followup_params.get("nights"), default=0)
            followup_guests = _to_int(followup_params.get("guests"), default=0)
            followup_location = str(followup_params.get("location") or "").strip()
            followup_room_ref = str(followup_params.get("room_reference") or followup_params.get("room_name") or "").strip()
            preview_stage = str(pending_booking.get("stage") or "").strip().lower()
            explicit_tour_switch_requested = _is_explicit_tour_command(message)
            likely_room_selection_reply = _looks_like_room_selection_reply(message)

            is_preview_followup = (
                _is_accommodation_preview_command(message)
                or _is_preview_confirmation_message(normalized_followup)
                or _is_open_official_page_request(normalized_followup)
                or followup_guests > 0
                or followup_nights > 0
                or bool(followup_check_in and followup_check_out)
                or bool(followup_room_ref)
                or likely_room_selection_reply
            )
            has_explicit_new_accommodation_query = (
                _contains_any_phrase(
                    normalized_followup,
                    (
                        "show approved accommodations",
                        "show available hotels",
                        "show hotels",
                        "show inns",
                        "where can i stay",
                        "what about",
                        "hotel in ",
                        "inn in ",
                        "accommodation in ",
                        "place to stay",
                        "stay in ",
                    ),
                )
                and not _is_accommodation_preview_command(message)
            )
            is_new_context_pivot = (
                followup_intent_hint in {"get_accommodation_recommendation", "travel_guidance", "plan_bayawan_stay"}
                or _is_accommodation_room_listing_command(message)
                or explicit_tour_switch_requested
            )
            # If preview handoff was already shown and the user asks a new discovery/pivot query,
            # release preview lock so normal routing can continue naturally.
            if has_explicit_new_accommodation_query or (
                not is_preview_followup
                and (
                    (
                        is_new_context_pivot
                        and not (
                            preview_stage in {"collecting_details", "awaiting_handoff"}
                            and likely_room_selection_reply
                            and not explicit_tour_switch_requested
                        )
                    )
                    or (
                        preview_stage in {"awaiting_handoff", "ready"}
                        and (
                            bool(followup_location)
                            or bool(followup_room_ref)
                            or _contains_any_phrase(normalized_followup, ("hotel", "inn", "accommodation", "stay", "villareal", "suba", "poblacion"))
                        )
                    )
                )
            ):
                next_state = dict(chat_state)
                next_state.pop("pending_booking", None)
                _save_chat_state(request, next_state)
                pending_booking_params = {}
                pending_booking_created_at = 0
            if not pending_booking_params:
                pass
            else:
                merged_preview_params = dict(pending_booking_params)
                merged_preview_params.update(followup_params)
                explicit_accommodation_name = _extract_preview_accommodation_name(message)
                if explicit_accommodation_name:
                    current_selected_name = str(merged_preview_params.get("selected_accommodation_name") or "").strip()
                    if current_selected_name and current_selected_name.lower() != explicit_accommodation_name.lower():
                        # Switching accommodation mid-flow: clear prior preview context.
                        merged_preview_params = {}
                        for safe_key in ("guests", "budget"):
                            if safe_key in followup_params and followup_params.get(safe_key) not in ("", None):
                                merged_preview_params[safe_key] = followup_params.get(safe_key)
                    merged_preview_params["accom_name"] = explicit_accommodation_name

                preview_resolution = _resolve_preview_room_selection(
                    params=merged_preview_params,
                    message=message,
                    cached_rows=cached_accommodation_rows_early,
                )
                if isinstance(preview_resolution.get("ambiguous_names"), list) and preview_resolution.get("ambiguous_names"):
                    choices = [str(v) for v in preview_resolution.get("ambiguous_names")[:3] if str(v).strip()]
                    return _chat_json_response(
                        request,
                        start_time,
                        {
                            "fulfillmentText": f"Which accommodation do you mean: {', '.join(choices)}?",
                            "quick_replies": choices,
                            "needs_clarification": True,
                            "missing_slot": "accommodation_name",
                        },
                    )
                if preview_resolution.get("needs_room_selection"):
                    selected_accom_name = str(preview_resolution.get("selected_accommodation_name") or "").strip()
                    selected_accom_id = _to_int(preview_resolution.get("selected_accommodation_id"), default=0)
                    room_choices = [
                        str(v) for v in (preview_resolution.get("room_choices") or []) if str(v).strip()
                    ][:4]
                    if selected_accom_id > 0:
                        merged_preview_params["selected_accommodation_id"] = selected_accom_id
                    if selected_accom_name:
                        merged_preview_params["selected_accommodation_name"] = selected_accom_name
                        merged_preview_params["accom_name"] = selected_accom_name
                    refreshed_state = dict(chat_state)
                    refreshed_state["pending_booking"] = {
                        "params": merged_preview_params,
                        "created_at": pending_booking_created_at or int(time.time()),
                        "stage": "collecting_details",
                    }
                    _save_chat_state(request, refreshed_state)
                    question_text = (
                        f"Which room would you like to preview at {selected_accom_name}?"
                        if selected_accom_name
                        else "Which room would you like to preview?"
                    )
                    return _chat_json_response(
                        request,
                        start_time,
                        {
                            "fulfillmentText": question_text,
                            "quick_replies": room_choices or ["show rooms"],
                            "needs_clarification": True,
                            "missing_slot": "room_reference",
                        },
                    )
                if preview_resolution.get("not_found_name"):
                    missing_name = str(preview_resolution.get("not_found_name") or "").strip()
                    reset_state = _clear_invalid_preview_context(chat_state)
                    _save_chat_state(request, reset_state)
                    return _chat_json_response(
                        request,
                        start_time,
                        {
                            "fulfillmentText": (
                                f"I couldn't find {missing_name} in approved accommodation listings yet. "
                                "Please choose from available rooms or specify the accommodation."
                            ),
                            "quick_replies": ["show approved accommodations in bayawan", "create booking preview"],
                            "needs_clarification": True,
                            "missing_slot": "accommodation_name",
                        },
                    )
                if preview_resolution.get("not_found_room_ref"):
                    selected_accom_name = str(preview_resolution.get("selected_accommodation_name") or "").strip()
                    selected_accom_id = _to_int(preview_resolution.get("selected_accommodation_id"), default=0)
                    room_choices = [
                        str(v) for v in (preview_resolution.get("room_choices") or []) if str(v).strip()
                    ][:4]
                    reset_state = dict(chat_state)
                    reset_params = dict(merged_preview_params)
                    if selected_accom_id > 0:
                        reset_params["selected_accommodation_id"] = selected_accom_id
                    if selected_accom_name:
                        reset_params["selected_accommodation_name"] = selected_accom_name
                        reset_params["accom_name"] = selected_accom_name
                    reset_params.pop("room_id", None)
                    reset_params.pop("selected_room_id", None)
                    reset_params.pop("selected_room_name", None)
                    reset_params.pop("room_name", None)
                    reset_params.pop("room_reference", None)
                    reset_state["params"] = reset_params
                    reset_state["pending_booking"] = {
                        "params": reset_params,
                        "created_at": pending_booking_created_at or int(time.time()),
                        "stage": "collecting_details",
                    }
                    _save_chat_state(request, reset_state)
                    return _chat_json_response(
                        request,
                        start_time,
                        {
                            "fulfillmentText": (
                                (
                                    f"I couldn't find that room in {selected_accom_name}. "
                                    "Please choose from available rooms in this accommodation."
                                )
                                if selected_accom_name
                                else "I couldn't find that room. Please choose from available rooms or specify the accommodation."
                            ),
                            "quick_replies": room_choices or ["show approved accommodations in bayawan", "create booking preview"],
                            "needs_clarification": True,
                            "missing_slot": "room_reference",
                        },
                    )

                preview_room = preview_resolution.get("room")
                if preview_room is not None:
                    merged_preview_params["room_id"] = _to_int(getattr(preview_room, "room_id", 0), default=0)
                    merged_preview_params["selected_room_id"] = merged_preview_params["room_id"]
                    merged_preview_params["selected_room_name"] = str(getattr(preview_room, "room_name", "") or "").strip()
                    merged_preview_params["selected_accommodation_id"] = _to_int(
                        getattr(preview_room, "accommodation_id", 0),
                        default=0,
                    )
                    merged_preview_params["selected_accommodation_name"] = str(
                        getattr(getattr(preview_room, "accommodation", None), "company_name", "") or ""
                    ).strip()
                    merged_preview_params["accom_name"] = merged_preview_params["selected_accommodation_name"]
                    merged_preview_params["nightly_rate"] = str(getattr(preview_room, "price_per_night", "") or "").strip()
                    merged_preview_params["selection_source"] = str(preview_resolution.get("source") or "fallback")

                preview_payload = _build_accommodation_preview_response(
                    room=preview_room,
                    params=merged_preview_params,
                )

                refreshed_state = dict(chat_state)
                if preview_payload.get("ready"):
                    refreshed_state["pending_booking"] = {
                        "params": merged_preview_params,
                        "created_at": pending_booking_created_at or int(time.time()),
                        "stage": "awaiting_handoff",
                    }
                    _save_chat_state(request, refreshed_state)
                    if _is_preview_confirmation_message(normalized_followup):
                        return _chat_json_response(
                            request,
                            start_time,
                            {
                                "fulfillmentText": (
                                    "This preview is not a confirmed booking. Please use the official link or contact the accommodation to complete your reservation."
                                ),
                                "quick_replies": ["open official page"],
                                "link_actions": preview_payload.get("link_actions") or [],
                                "billing_link": str(preview_payload.get("billing_link") or ""),
                                "billing_link_label": str(
                                    preview_payload.get("billing_link_label") or "Continue to Official Booking Page"
                                ),
                            },
                        )
                    if _is_open_official_page_request(normalized_followup):
                        return _chat_json_response(
                            request,
                            start_time,
                            {
                                "fulfillmentText": "Opening official accommodation channels for your booking handoff.",
                                "quick_replies": ["open official page"],
                                "link_actions": preview_payload.get("link_actions") or [],
                                "billing_link": str(preview_payload.get("billing_link") or ""),
                                "billing_link_label": str(
                                    preview_payload.get("billing_link_label") or "Continue to Official Booking Page"
                                ),
                            },
                        )
                    return _chat_json_response(
                        request,
                        start_time,
                        {
                            "fulfillmentText": str(preview_payload.get("text") or "").strip(),
                            "quick_replies": _sanitize_quick_replies(
                                preview_payload.get("quick_replies"),
                                limit=4,
                            ),
                            "link_actions": preview_payload.get("link_actions") or [],
                            "billing_link": str(preview_payload.get("billing_link") or ""),
                            "billing_link_label": str(
                                preview_payload.get("billing_link_label") or "Continue to Official Booking Page"
                            ),
                        },
                    )

                refreshed_state["pending_booking"] = {
                    "params": merged_preview_params,
                    "created_at": pending_booking_created_at or int(time.time()),
                    "stage": "collecting_details",
                }
                _save_chat_state(request, refreshed_state)
                return _chat_json_response(
                    request,
                    start_time,
                    {
                        "fulfillmentText": str(preview_payload.get("question") or "").strip(),
                        "quick_replies": _sanitize_quick_replies(
                            preview_payload.get("quick_replies"),
                            limit=4,
                        ),
                        "needs_clarification": True,
                        "missing_slot": (
                            str((preview_payload.get("missing_fields") or ["accommodation_details"])[0])
                            if isinstance(preview_payload.get("missing_fields"), list)
                            else "accommodation_details"
                        ),
                    },
                )

    # Compact numeric selection support for accommodation recommendations:
    # if the user replies with just "1", "2", "3", map it to the last shown room.
    if actor.get("role") == "guest" and state_intent_hint in ("get_accommodation_recommendation", "gethotelrecommendation"):
        cached_rows = (
            chat_state.get("last_accommodation_recommendations")
            if isinstance(chat_state.get("last_accommodation_recommendations"), list)
            else []
        )
        why_option_index = _extract_why_option_index(message)
        if why_option_index > 0 and cached_rows:
            why_text = _build_why_option_text(cached_rows, why_option_index)
            if why_text:
                _safe_log_chat_step_event(
                    request,
                    event_type="click",
                    item_ref=f"chat:why_option:{why_option_index}",
                )
                return _chat_json_response(
                    request,
                    start_time,
                    {
                        "fulfillmentText": why_text,
                        "quick_replies": _merge_quick_replies(
                            _build_recommendation_assist_quick_replies(cached_rows),
                            [{"label": f"View Option {why_option_index}", "value": str(why_option_index)}],
                            limit=4,
                        ),
                    },
                )

        compare_top_n = _extract_compare_top_n(message, default=3)
        if compare_top_n > 0 and cached_rows:
            compare_text = _build_compare_options_text(cached_rows, compare_top_n)
            if compare_text:
                _safe_log_chat_step_event(
                    request,
                    event_type="click",
                    item_ref=f"chat:compare_top:{compare_top_n}",
                )
                return _chat_json_response(
                    request,
                    start_time,
                    {
                        "fulfillmentText": compare_text,
                        "quick_replies": _sanitize_quick_replies(
                            _build_post_compare_quick_replies(cached_rows, compare_top_n),
                            limit=4,
                        ),
                    },
                )

        option_index = _extract_numeric_option_index(message)
        if option_index > 0 and cached_rows:
            selected_room_id = _resolve_accommodation_room_from_selection(cached_rows, option_index)
            if selected_room_id > 0:
                selected_params = dict(state_params_hint)
                selected_params["room_id"] = selected_room_id
                selected_params["_personalization_opt_out"] = bool(
                    selected_params.get("_personalization_opt_out", False)
                )
                next_state = dict(chat_state)
                next_state["pending_intent"] = "book_accommodation"
                next_state["params"] = selected_params
                next_state["missing_slot"] = ""
                next_state["pending_booking"] = {
                    "params": selected_params,
                    "created_at": int(time.time()),
                    "stage": "collecting_details",
                }
                _save_chat_state(request, next_state)

                selected_room = Room.objects.select_related("accommodation").filter(room_id=selected_room_id).first()
                preview_payload = _build_accommodation_preview_response(room=selected_room, params=selected_params)
                response = {
                    "fulfillmentText": (
                        "Great choice. I can prepare an accommodation booking preview for you. "
                        "Final booking is completed directly with the accommodation."
                    ),
                    "quick_replies": [
                        "May 10 to May 12 for 2 guests",
                        "2 nights for 2 guests",
                        "open official page",
                    ],
                }
                if preview_payload.get("ready"):
                    response["fulfillmentText"] = str(preview_payload.get("text") or "").strip()
                    response["quick_replies"] = _sanitize_quick_replies(
                        preview_payload.get("quick_replies"),
                        limit=4,
                    )
                    if preview_payload.get("billing_link"):
                        response["billing_link"] = str(preview_payload.get("billing_link"))
                        response["billing_link_label"] = str(
                            preview_payload.get("billing_link_label") or "Continue to Official Booking Page"
                        )
                    if isinstance(preview_payload.get("link_actions"), list):
                        response["link_actions"] = preview_payload.get("link_actions")[:4]
                if selected_room is not None:
                    selected_params["selected_room_id"] = _to_int(getattr(selected_room, "room_id", 0), default=0)
                    selected_params["selected_room_name"] = str(getattr(selected_room, "room_name", "") or "").strip()
                    selected_params["selected_accommodation_id"] = _to_int(getattr(selected_room, "accommodation_id", 0), default=0)
                    selected_params["selected_accommodation_name"] = str(
                        getattr(getattr(selected_room, "accommodation", None), "company_name", "") or ""
                    ).strip()
                    selected_params["nightly_rate"] = str(getattr(selected_room, "price_per_night", "") or "").strip()
                    selected_params["selection_source"] = "last_card"
                    next_state["params"] = selected_params
                    next_state["pending_booking"] = {
                        "params": selected_params,
                        "created_at": int(time.time()),
                        "stage": "collecting_details",
                    }
                    _save_chat_state(request, next_state)
                return _chat_json_response(request, start_time, response)

    pending_budget_offer = (
        chat_state.get("pending_budget_offer")
        if isinstance(chat_state.get("pending_budget_offer"), dict)
        else {}
    )
    offered_budget = _to_int(pending_budget_offer.get("suggested_budget"), default=0)
    if (
        pending_budget_offer
        and state_intent_hint in ("get_accommodation_recommendation", "gethotelrecommendation")
        and _is_accommodation_how_to_book_request(message)
    ):
        next_state = dict(chat_state)
        next_state.pop("pending_budget_offer", None)
        _save_chat_state(request, next_state)
        chat_state = next_state
        pending_budget_offer = {}
        offered_budget = 0
    if pending_budget_offer and state_intent_hint in ("get_accommodation_recommendation", "gethotelrecommendation"):
        request._chatbot_log_context["resolved_intent"] = "get_accommodation_recommendation"
        parsed_offer_reply = _extract_params_with_confidence(message)
        reply_params = parsed_offer_reply.get("params") if isinstance(parsed_offer_reply.get("params"), dict) else {}
        custom_budget = _to_int(reply_params.get("budget"), default=0)

        if _is_personalization_accept_message(message):
            if offered_budget > 0:
                accepted_params = dict(state_params_hint)
                accepted_params["budget"] = offered_budget
                reply, logged_recommended_items, accommodation_meta = _safe_get_accommodation_recommendations(accepted_params)
                next_state = dict(chat_state)
                next_state["pending_intent"] = "get_accommodation_recommendation"
                next_state["params"] = accepted_params
                next_state["missing_slot"] = ""
                next_state.pop("pending_budget_offer", None)
                next_state["last_accommodation_recommendations"] = _build_accommodation_selection_cache(
                    logged_recommended_items
                )
                if (
                    isinstance(accommodation_meta, dict)
                    and "budget_too_low" in (accommodation_meta.get("no_match_reasons") or [])
                    and _to_int(accommodation_meta.get("suggested_budget_min"), default=0) > 0
                ):
                    next_state["pending_budget_offer"] = {
                        "suggested_budget": _to_int(accommodation_meta.get("suggested_budget_min"), default=0)
                    }
                _save_chat_state(request, next_state)

                response = {"fulfillmentText": reply}
                if logged_recommended_items:
                    response["recommendation_trace"] = logged_recommended_items
                if isinstance(accommodation_meta, dict):
                    if isinstance(accommodation_meta.get("no_match_reasons"), list):
                        response["no_match_reasons"] = [str(v) for v in accommodation_meta.get("no_match_reasons")]
                    if accommodation_meta.get("suggested_budget_min") not in (None, ""):
                        response["suggested_budget_min"] = accommodation_meta.get("suggested_budget_min")
                    if accommodation_meta.get("fallback_applied"):
                        response["recommendation_fallback"] = str(accommodation_meta.get("fallback_applied"))
                    if isinstance(accommodation_meta.get("quick_replies"), list):
                        response["quick_replies"] = _sanitize_quick_replies(
                            accommodation_meta.get("quick_replies"),
                            limit=4,
                        )
                if logged_recommended_items:
                    response["quick_replies"] = _merge_quick_replies(
                        response.get("quick_replies") if isinstance(response.get("quick_replies"), list) else [],
                        _build_recommendation_assist_quick_replies(
                            _build_accommodation_selection_cache(logged_recommended_items)
                        ),
                        limit=4,
                    )
                _inject_recommendation_context(response, accepted_params)
                return _chat_json_response(request, start_time, response)

        if _is_personalization_decline_message(message):
            next_state = dict(chat_state)
            next_state.pop("pending_budget_offer", None)
            _save_chat_state(request, next_state)
            return _chat_json_response(
                request,
                start_time,
                {"fulfillmentText": "Okay, no problem. Please share your preferred budget per night in PHP."},
            )

        if custom_budget > 0:
            accepted_params = dict(state_params_hint)
            accepted_params["budget"] = custom_budget
            reply, logged_recommended_items, accommodation_meta = _safe_get_accommodation_recommendations(accepted_params)
            next_state = dict(chat_state)
            next_state["pending_intent"] = "get_accommodation_recommendation"
            next_state["params"] = accepted_params
            next_state["missing_slot"] = ""
            next_state.pop("pending_budget_offer", None)
            next_state["last_accommodation_recommendations"] = _build_accommodation_selection_cache(
                logged_recommended_items
            )
            if (
                isinstance(accommodation_meta, dict)
                and "budget_too_low" in (accommodation_meta.get("no_match_reasons") or [])
                and _to_int(accommodation_meta.get("suggested_budget_min"), default=0) > 0
            ):
                next_state["pending_budget_offer"] = {
                    "suggested_budget": _to_int(accommodation_meta.get("suggested_budget_min"), default=0)
                }
            _save_chat_state(request, next_state)

            response = {"fulfillmentText": reply}
            if logged_recommended_items:
                response["recommendation_trace"] = logged_recommended_items
            if isinstance(accommodation_meta, dict):
                if isinstance(accommodation_meta.get("no_match_reasons"), list):
                    response["no_match_reasons"] = [str(v) for v in accommodation_meta.get("no_match_reasons")]
                if accommodation_meta.get("suggested_budget_min") not in (None, ""):
                    response["suggested_budget_min"] = accommodation_meta.get("suggested_budget_min")
                if accommodation_meta.get("fallback_applied"):
                    response["recommendation_fallback"] = str(accommodation_meta.get("fallback_applied"))
                if isinstance(accommodation_meta.get("quick_replies"), list):
                    response["quick_replies"] = _sanitize_quick_replies(
                        accommodation_meta.get("quick_replies"),
                        limit=4,
                    )
            if logged_recommended_items:
                response["quick_replies"] = _merge_quick_replies(
                    response.get("quick_replies") if isinstance(response.get("quick_replies"), list) else [],
                    _build_recommendation_assist_quick_replies(
                        _build_accommodation_selection_cache(logged_recommended_items)
                    ),
                    limit=4,
                )
            _inject_recommendation_context(response, accepted_params)
            return _chat_json_response(request, start_time, response)

        # Preserve non-budget slot updates (like new dates/guests) while waiting for
        # explicit budget confirmation so the user doesn't need to retype them.
        non_budget_updates = {}
        for key in (
            "check_in",
            "check_out",
            "nights",
            "guests",
            "adults",
            "children",
            "location",
            "company_type",
            "preference_tags",
            "prefer_low_price",
            "amenities",
            "broaden_location",
            "broaden_company_type",
        ):
            if key in reply_params and reply_params.get(key) not in ("", None):
                non_budget_updates[key] = reply_params.get(key)
        if non_budget_updates:
            updated_state_params = dict(state_params_hint)
            updated_state_params.update(non_budget_updates)
            next_state = dict(chat_state)
            next_state["params"] = updated_state_params
            _save_chat_state(request, next_state)

        return _chat_json_response(
            request,
            start_time,
            {
                "fulfillmentText": (
                    f"I saved your dates/filters. I still need budget confirmation first.\n"
                    f"Reply YES to use PHP {offered_budget} budget, "
                    "reply NO to skip, or send a different budget amount."
                )
            },
        )

    if actor.get("role") == "guest" and _is_accommodation_room_listing_command(message):
        request._chatbot_log_context["resolved_intent"] = "get_accommodation_room_listing"
        early_state = _load_chat_state(request)
        early_cached_rows = (
            early_state.get("last_accommodation_recommendations")
            if isinstance(early_state.get("last_accommodation_recommendations"), list)
            else []
        )
        early_state_params = early_state.get("params") if isinstance(early_state.get("params"), dict) else {}
        target_name = _extract_accommodation_name_for_room_listing(
            message,
            early_cached_rows,
            state_params=early_state_params,
        )
        room_listing_payload = _build_room_listing_response_for_accommodation(target_name, limit=5)
        if isinstance(room_listing_payload.get("recommendation_trace"), list):
            next_state = dict(early_state)
            next_state["last_accommodation_recommendations"] = _build_accommodation_selection_cache(
                room_listing_payload.get("recommendation_trace")
            )
            next_params = dict(early_state_params)
            selected_accom_id = _to_int(room_listing_payload.get("selected_accommodation_id"), default=0)
            selected_accom_name = str(room_listing_payload.get("selected_accommodation_name") or "").strip()
            if selected_accom_id > 0:
                next_params["selected_accommodation_id"] = selected_accom_id
            if selected_accom_name:
                next_params["selected_accommodation_name"] = selected_accom_name
                next_params["accom_name"] = selected_accom_name
            next_state["params"] = next_params
            next_state["pending_intent"] = "get_accommodation_recommendation"
            next_state["missing_slot"] = ""
            next_state.pop("pending_booking", None)
            _save_chat_state(request, next_state)
        return _chat_json_response(request, start_time, room_listing_payload)

    if _is_role_vague_query(actor, message):
        role_clarifier = _role_aware_clarification_payload(actor)
        request._chatbot_log_context["resolved_intent"] = "clarification"
        request._chatbot_log_context["fallback_used"] = False
        if isinstance(request._chatbot_log_context.get("provenance"), dict):
            request._chatbot_log_context["provenance"]["clarification_used"] = True
        _save_chat_state(
            request,
            {
                "pending_intent": "clarification",
                "params": chat_state_params if isinstance(chat_state_params, dict) else {},
                "missing_slot": "clarification",
            },
        )
        return _chat_json_response(
            request,
            start_time,
            {
                "fulfillmentText": str(role_clarifier.get("text") or _clarification_fallback("Could you clarify what you need help with?")),
                "quick_replies": _sanitize_quick_replies(role_clarifier.get("quick_replies"), limit=4),
                "needs_clarification": True,
                "missing_slot": "clarification",
            },
        )

    parsed = _classify_intent_and_extract_params(message)
    intent = str(parsed["intent"]).strip().lower()
    params = parsed["params"] if isinstance(parsed["params"], dict) else {}
    parse_confidence = float(parsed.get("confidence", 1.0) or 1.0)
    parse_source = str(parsed.get("source") or "").strip().lower()
    needs_parser_clarification = bool(parsed.get("needs_clarification"))
    parser_clarification_question = str(parsed.get("clarification_question") or "").strip()
    parser_clarification_field = str(parsed.get("clarification_field") or "").strip()
    parser_clarification_options = (
        parsed.get("clarification_options")
        if isinstance(parsed.get("clarification_options"), list)
        else []
    )
    intent_classifier = parsed.get("intent_classifier") if isinstance(parsed.get("intent_classifier"), dict) else {}
    if actor.get("role") == "guest" and _is_stay_planning_request(message):
        intent = "plan_bayawan_stay"
    if actor.get("role") == "guest" and _is_travel_guidance_request(message):
        intent = "travel_guidance"
    if _is_reporting_summary_request(message):
        intent = "reporting_summary"
    strict_intent_locked = _has_strict_intent_signal(actor=actor, message=message)
    intent = _apply_strict_intent_overrides(
        actor=actor,
        message=message,
        current_intent=intent,
    )
    request._chatbot_log_context["resolved_intent"] = intent
    request._chatbot_log_context["resolved_params"] = params
    request._chatbot_log_context["intent_classifier"] = intent_classifier
    parse_fallback_used = str(parsed.get("source") or "") in (
        "heuristic_intent_fallback",
        "text_cnn_unavailable",
        "text_cnn_low_confidence",
        "text_cnn_incompatible_label_space",
    )
    request._chatbot_log_context["parse_fallback_used"] = parse_fallback_used
    try:
        provenance_log = (
            request._chatbot_log_context.get("provenance")
            if isinstance(request._chatbot_log_context.get("provenance"), dict)
            else {}
        )
        provenance_log["intent_parse_source"] = parse_source[:80]
        if parse_fallback_used:
            provenance_log["fallback_reason"] = parse_source[:120]
            provenance_log["intent_parse_fallback_used"] = True
        intent_error = str(intent_classifier.get("error") or "").strip()
        if intent_error:
            provenance_log["intent_classifier_error"] = intent_error[:180]
        if needs_parser_clarification:
            provenance_log["clarification_requested_by_parser"] = True
        classifier_source = str(intent_classifier.get("source") or "").strip().lower()
        classifier_conf = float(intent_classifier.get("confidence", 0.0) or 0.0)
        is_low_conf_case = classifier_source == "text_cnn_low_confidence"
        top3 = intent_classifier.get("top_3") if isinstance(intent_classifier.get("top_3"), list) else []
        predicted_intent = ""
        if top3 and isinstance(top3[0], dict):
            predicted_intent = str(top3[0].get("intent") or top3[0].get("raw_label") or "").strip().lower()
        deterministic_fixed = bool(
            is_low_conf_case
            and parse_source == "deterministic_pre_route"
            and str(intent or "").strip().lower() != predicted_intent
        )
        provenance_log["low_confidence_case"] = bool(is_low_conf_case)
        if is_low_conf_case:
            provenance_log["low_confidence_raw_query"] = str(message or "")[:240]
            provenance_log["low_confidence_predicted_intent"] = predicted_intent[:80]
            provenance_log["low_confidence_confidence"] = round(classifier_conf, 6)
            provenance_log["low_confidence_final_resolved_intent"] = str(intent or "")[:80]
            provenance_log["deterministic_routing_applied"] = parse_source == "deterministic_pre_route"
            provenance_log["deterministic_routing_fixed_low_confidence"] = deterministic_fixed
        request._chatbot_log_context["provenance"] = provenance_log
    except Exception:
        pass
    state_intent = str(chat_state.get("pending_intent") or "").strip().lower()
    state_params = chat_state.get("params") if isinstance(chat_state.get("params"), dict) else {}
    state_missing_slot = str(chat_state.get("missing_slot") or "").strip().lower()
    state_default_offer = (
        chat_state.get("default_offer") if isinstance(chat_state.get("default_offer"), dict) else {}
    )
    state_offer_defaults = (
        state_default_offer.get("defaults")
        if isinstance(state_default_offer.get("defaults"), dict)
        else {}
    )
    state_offer_prompt = str(state_default_offer.get("prompt") or "").strip()
    state_skip_default_offer = bool(chat_state.get("skip_default_offer")) or bool(
        state_params.get("_personalization_opt_out", False)
    )

    accommodation_intents = ("get_accommodation_recommendation", "gethotelrecommendation")
    booking_intents = ("book_accommodation", "bookhotel", "book_hotel", "reserve_accommodation")
    planning_intents = ("plan_bayawan_stay",)
    continuation_intents = accommodation_intents + booking_intents + planning_intents + ("get_recommendation",)
    continuing_accommodation_flow = (
        (not strict_intent_locked)
        and
        state_intent in accommodation_intents
        and (
            intent in accommodation_intents
            or (
                intent == "get_recommendation"
                and not _looks_like_tour_request(message)
                and (
                    _looks_like_slot_update(params)
                    or state_missing_slot in ("company_type", "location", "budget", "guests", "stay_details", "accommodation_details")
                )
            )
        )
    )
    if continuing_accommodation_flow:
        intent = state_intent

    continuing_booking_flow = (
        (not strict_intent_locked)
        and
        state_intent in booking_intents
        and (
            intent in booking_intents
            or _looks_like_slot_update(params)
            or state_missing_slot in ("guests", "stay_details", "accommodation_details", "date_range")
        )
    )
    if continuing_booking_flow:
        intent = state_intent

    continuing_planning_flow = (
        (not strict_intent_locked)
        and
        state_intent in planning_intents
        and (
            intent in planning_intents
            or _looks_like_slot_update(params)
            or state_missing_slot in ("total_budget", "duration_days", "party_type")
            or bool(re.fullmatch(r"\s*(\d+)\s*(day|days)?\s*", str(message or "").strip().lower()))
        )
    )
    if continuing_planning_flow:
        intent = state_intent

    if actor.get("role") == "guest" and (
        _is_accommodation_preview_command(message) or _is_accommodation_how_to_book_request(message)
    ):
        intent = "book_accommodation"

    if (
        intent == "get_recommendation"
        and not continuing_accommodation_flow
        and not _looks_like_tour_request(message)
        and _is_out_of_scope_message(message)
    ):
        intent = "out_of_scope"

    compact_num_match = re.fullmatch(r"\s*(\d+)\s*", message)
    if compact_num_match and state_missing_slot in ("guests", "stay_details", "accommodation_details"):
        # Avoid treating compact numeric replies as budget when we are collecting
        # guests or stay details in a running accommodation flow.
        existing_budget = _to_int(state_params.get("budget"), default=0)
        parsed_budget = _to_int(params.get("budget"), default=0)
        if existing_budget > 0 and parsed_budget > 0 and parsed_budget != existing_budget:
            params.pop("budget", None)
    if compact_num_match and state_missing_slot == "total_budget":
        compact_value = _to_int(compact_num_match.group(1), default=0)
        if compact_value > 0:
            params["total_budget"] = compact_value
    if compact_num_match and state_missing_slot == "duration_days":
        compact_value = _to_int(compact_num_match.group(1), default=0)
        if compact_value > 0:
            params["duration_days"] = compact_value

    merged_params = dict(state_params)
    merged_params.update(params)
    params = merged_params
    if actor.get("role") == "guest" and isinstance(assistant_memory, dict):
        memory_defaults = _assistant_memory_to_params(assistant_memory)
        for key, value in memory_defaults.items():
            if params.get(key) in ("", None, []):
                params[key] = value
    compact_lower = str(message or "").strip().lower()
    if state_intent in planning_intents:
        people_match = re.search(r"\bfor\s+(\d+)\s*(people|person|pax|guests?)\b", compact_lower)
        if people_match:
            people_count = _to_int(people_match.group(1), default=0)
            if people_count > 0:
                params["group_size"] = people_count
                params["guests"] = people_count
        implied_budget_match = re.search(
            r"\b(?:only\s+have|i\s+have|have|budget(?:\s+is)?|what\s+if\s+i\s+have)\s*([0-9][0-9,]*(?:\.[0-9]+)?k?)\b",
            compact_lower,
        )
        if implied_budget_match and _to_int(params.get("total_budget"), default=0) <= 0:
            parse_compact = globals().get("_parse_compact_number")
            implied_budget = (
                parse_compact(implied_budget_match.group(1))
                if callable(parse_compact)
                else _to_int(implied_budget_match.group(1), default=0)
            )
            if implied_budget is not None and implied_budget > 0:
                params["total_budget"] = implied_budget
                params.setdefault("spending_style", "budget")
        days_match = re.search(r"\b(\d+)\s*days?\b", compact_lower)
        if days_match:
            day_count = _to_int(days_match.group(1), default=0)
            if day_count > 0:
                params["duration_days"] = day_count
        if _contains_any_phrase(compact_lower, ("no accommodation needed", "without accommodation", "no hotel needed")):
            params["accommodation_needed"] = False
        elif _contains_any_phrase(compact_lower, ("with accommodation", "need accommodation", "hotel needed")):
            params["accommodation_needed"] = True
        if _contains_any_phrase(compact_lower, ("budget only", "just budget")):
            params["spending_style"] = "budget"
        if _contains_any_phrase(compact_lower, ("just nearby", "nearby only", "nearby", "close by")):
            params["location"] = str(params.get("location") or "Bayawan City Proper")
        if _contains_any_phrase(compact_lower, ("relaxing", "relax", "chill")):
            params["experience_style"] = "relaxing"
        elif _contains_any_phrase(compact_lower, ("adventure", "adventurous", "trail", "hike")):
            params["experience_style"] = "adventure"
        elif _contains_any_phrase(compact_lower, ("culture", "cultural", "heritage")):
            params["experience_style"] = "culture"
        elif _contains_any_phrase(compact_lower, ("mixed", "combination", "both")):
            params["experience_style"] = "mixed"
        if re.fullmatch(r"\s*(solo|couple|family|group)\s*", compact_lower):
            params["party_type"] = compact_lower.strip()
        origin_match = re.search(r"\bfrom\s+([a-z][a-z\s\-]{2,35})\b", compact_lower)
        if origin_match and not str(params.get("origin_hint") or "").strip():
            params["origin_hint"] = " ".join(str(origin_match.group(1) or "").split()).strip().title()

    if isinstance(client_location, dict):
        params["client_location_status"] = str(client_location.get("status") or "").strip().lower()
        if client_location.get("latitude") is not None and client_location.get("longitude") is not None:
            params["origin_latitude"] = client_location.get("latitude")
            params["origin_longitude"] = client_location.get("longitude")
        if client_location.get("accuracy_m") is not None:
            params["origin_accuracy_m"] = client_location.get("accuracy_m")
    guest_count_update_note = ""
    previous_guests = _to_int(state_params.get("guests"), default=0)
    current_guests = _to_int(params.get("guests"), default=0)
    if (
        state_intent in accommodation_intents + booking_intents
        and current_guests > 0
        and _message_mentions_guest_count(message)
    ):
        if previous_guests <= 0:
            guest_count_update_note = f"Noted. I updated your guest count to {current_guests}."
        elif previous_guests != current_guests:
            guest_count_update_note = (
                f"Noted. I updated your guest count from {previous_guests} to {current_guests}."
            )

    # User explicitly removed budget constraint; keep accommodation flow and clear budget state.
    if _to_bool(params.get("clear_budget"), default=False):
        params["budget"] = 0
        params.pop("pending_budget_offer", None)

    # If a new plain location was provided without a resolved map anchor, clear prior anchor context.
    if _to_bool(params.get("clear_location_anchor"), default=False) and not str(params.get("location_anchor") or "").strip():
        params.pop("location_anchor", None)
        params.pop("location_anchor_source", None)
        params.pop("location_scope_note", None)
    if actor.get("role") == "guest":
        saved_prefs = _load_saved_chat_preferences(request)
        params = _apply_saved_preferences_to_params(params, saved_prefs)
        if (
            intent == "get_recommendation"
            and isinstance(saved_prefs, dict)
            and bool(saved_prefs)
            and not _looks_like_tour_request(message)
            and not _is_out_of_scope_message(message)
            and not _contains_any_phrase(
                message,
                (
                    "tourism information",
                    "tourist spot",
                    "attraction",
                    "landmark",
                    "history",
                ),
            )
        ):
            intent = "get_accommodation_recommendation"
    lowered_message = str(message or "").strip().lower()
    cheaper_followup = bool(
        re.search(r"\b(make it cheaper|cheaper|lower budget|budget version|something cheaper|adjust to budget version)\b", lowered_message)
    )
    if (
        state_intent in accommodation_intents
        and any(token in lowered_message for token in ("cheaper", "lower price", "less expensive", "mas mura", "barato"))
    ):
        existing_budget = _to_int(state_params.get("budget"), default=0)
        if existing_budget > 0:
            params["budget"] = max(500, int(existing_budget * 0.8))
        params["prefer_low_price"] = True
    if state_intent in planning_intents and cheaper_followup:
        existing_total_budget = _to_int(params.get("total_budget"), default=0)
        if existing_total_budget <= 0:
            existing_total_budget = _to_int(state_params.get("total_budget"), default=0)
        if existing_total_budget > 0:
            if existing_total_budget <= 3500:
                params["_already_lowest_budget"] = True
            else:
                params["total_budget"] = max(3000, int(existing_total_budget * 0.8))
                params["_cheaper_adjustment_applied"] = True
                params["spending_style"] = "budget"
    if (
        state_intent in accommodation_intents + booking_intents
        and _to_int(state_params.get("guests"), default=0) > 0
        and _to_int(params.get("guests"), default=0) == 1
        and not _message_mentions_guest_count(message)
    ):
        # Keep previously provided guest count; don't overwrite with parser fallback defaults.
        params["guests"] = _to_int(state_params.get("guests"), default=0)
    request._chatbot_log_context["resolved_intent"] = intent
    request._chatbot_log_context["resolved_params"] = params

    # Performance-only fast path for compact accommodation budget/location follow-ups
    # (e.g., "suba under 1500") while a stay-search flow is already active.
    if (
        state_intent in accommodation_intents
        and intent in accommodation_intents
        and re.search(r"\b(?:under|below|budget)\s*[0-9][0-9,]*(?:\.[0-9]+)?k?\b", compact_lower)
        and re.search(r"\b(?:suba|poblacion|bayawan|villareal|tinago|ubos)\b", compact_lower)
        and _to_int(params.get("guests"), default=0) <= 0
    ):
        location_hint = str(params.get("location") or "").strip()
        budget_hint = _to_int(params.get("budget"), default=0)
        if location_hint and budget_hint > 0:
            next_state = dict(chat_state)
            next_state["pending_intent"] = "get_accommodation_recommendation"
            next_state["params"] = dict(params)
            next_state["missing_slot"] = "guests"
            _save_chat_state(request, next_state)
            return _chat_json_response(
                request,
                start_time,
                {
                    "fulfillmentText": (
                        f"So far: {location_hint.lower()}, PHP {budget_hint}. "
                        "How many people should the room accommodate?"
                    ),
                    "quick_replies": _slot_quick_replies("guests"),
                    "needs_clarification": True,
                    "missing_slot": "guests",
                },
            )

    # Context-aware slot repair for short follow-up replies while collecting
    # accommodation details (e.g., user replies with just "suba" or "inn").
    compact_text = str(message or "").strip()
    compact_lower = compact_text.lower()
    if state_intent in accommodation_intents:
        if state_missing_slot == "company_type" and "company_type" not in params:
            if compact_lower in ("hotel", "inn", "either", "hotel or inn"):
                params["company_type"] = "either" if compact_lower == "hotel or inn" else compact_lower
                intent = state_intent
        if state_missing_slot == "location" and not str(params.get("location") or "").strip():
            simple_location = re.fullmatch(r"[a-zA-Z][a-zA-Z\\s\\-]{1,60}", compact_text)
            if simple_location and compact_lower not in ("yes", "no", "hotel", "inn", "either"):
                params["location"] = compact_text
                intent = state_intent
        if state_missing_slot == "guests" and _to_int(params.get("guests"), default=0) <= 0:
            guest_inline_match = re.search(r"\b(\d+)\b(?:\s*(?:under|below|less than)\s*\d+)?", compact_lower)
            if guest_inline_match:
                inline_guests = _to_int(guest_inline_match.group(1), default=0)
                if inline_guests > 0:
                    params["guests"] = inline_guests
                    intent = state_intent
        if state_missing_slot == "budget":
            if compact_lower in {"no", "none", "skip", "no budget", "without budget"}:
                # User chose to proceed without a strict budget cap.
                params["budget"] = 0
                params.pop("budget_min", None)
                params["clear_budget"] = True
                intent = state_intent

    # Context-aware compact answers:
    # - If we are explicitly waiting for guests, allow plain integer replies like "2".
    # - If we are waiting for stay details, allow plain integer replies as nights.
    if compact_num_match:
        compact_num = _to_int(compact_num_match.group(1), default=0)
        if state_missing_slot == "guests" and _to_int(params.get("guests"), default=0) <= 0 and compact_num > 0:
            params["guests"] = compact_num
            params.pop("budget", None)
        elif state_missing_slot == "budget" and _to_int(params.get("budget"), default=0) <= 0 and compact_num > 0:
            params["budget"] = compact_num
        elif (
            state_missing_slot == "stay_details"
            and not _has_stay_details(params)
            and compact_num > 0
        ):
            params["nights"] = compact_num
        elif (
            state_missing_slot == "accommodation_details"
            and compact_num > 0
            and _to_int(params.get("guests"), default=0) <= 0
            and _to_int(params.get("budget"), default=0) <= 0
        ):
            # If other constraints already exist, treat numeric-only follow-up as budget.
            has_existing_constraints = bool(
                str(params.get("location") or "").strip()
                or str(params.get("company_type") or "").strip()
                or str(params.get("room_type") or "").strip()
                or _to_int(state_params.get("guests"), default=0) > 0
            )
            if has_existing_constraints and compact_num >= 500:
                params["budget"] = compact_num
            else:
                params["guests"] = compact_num

    if (
        compact_num_match
        and state_intent in accommodation_intents
        and state_missing_slot in {"budget", "accommodation_details"}
        and _to_int(params.get("budget"), default=0) > 0
    ):
        intent = state_intent

    if state_intent in accommodation_intents and (
        _is_accommodation_how_to_book_request(message) or _is_accommodation_preview_command(message)
    ):
        intent = "book_accommodation"
        state_offer_defaults = {}
        state_skip_default_offer = True
        override_state = dict(chat_state)
        override_state.pop("default_offer", None)
        override_state.pop("pending_budget_offer", None)
        override_state["pending_intent"] = "book_accommodation"
        override_state["params"] = params
        _save_chat_state(request, override_state)

    if (
        state_intent in accommodation_intents
        and state_offer_defaults
        and not state_skip_default_offer
        and not _has_sufficient_accommodation_details(params)
        and not (_is_accommodation_how_to_book_request(message) or _is_accommodation_preview_command(message))
    ):
        if _is_personalization_decline_message(message):
            declined_params = dict(params)
            declined_params["_personalization_opt_out"] = True
            missing_slot, question = _next_accommodation_clarifying_question(declined_params)
            next_state = {
                "pending_intent": state_intent,
                "params": declined_params,
                "skip_default_offer": True,
            }
            if missing_slot:
                next_state["missing_slot"] = missing_slot
            company_type = str(declined_params.get("company_type") or "").strip().lower()
            location = str(declined_params.get("location") or "").strip()
            preference_tags = (
                declined_params.get("preference_tags")
                if isinstance(declined_params.get("preference_tags"), list)
                else []
            )
            preview_key = f"{company_type}|{location.lower()}|{','.join(sorted(str(tag) for tag in preference_tags))}"
            if (
                missing_slot in ("stay_details", "budget", "accommodation_details")
                and state_missing_slot != "location"
                and company_type in ("hotel", "inn", "either")
                and (location or preference_tags)
                and str(chat_state.get("location_preview_for") or "") != preview_key
            ):
                preview_params = dict(declined_params)
                preview_reply, preview_items, preview_meta = _safe_get_accommodation_recommendations(preview_params)
                if (
                    "top hotel/inn recommendations" not in str(preview_reply or "").lower()
                    and company_type in ("hotel", "inn")
                ):
                    preview_any_type_params = dict(preview_params)
                    preview_any_type_params["company_type"] = "either"
                    preview_reply, preview_items, preview_meta = _safe_get_accommodation_recommendations(
                        preview_any_type_params
                    )
                if str(preview_reply or "").strip():
                    next_state["location_preview_for"] = preview_key
                    next_state["last_accommodation_recommendations"] = _build_accommodation_selection_cache(preview_items)
                    _save_chat_state(request, next_state)
                    prompt_suffix = (
                        f"To continue booking, {question}"
                        if question and "top hotel/inn recommendations" in str(preview_reply or "").lower()
                        else ""
                    )
                    response_text = str(preview_reply or "").strip()
                    if prompt_suffix:
                        response_text = f"{response_text}\n\n{prompt_suffix}"
                    response = {"fulfillmentText": response_text}
                    if preview_items:
                        response["recommendation_trace"] = preview_items
                    if isinstance(preview_meta, dict) and preview_meta.get("fallback_applied"):
                        response["recommendation_fallback"] = str(preview_meta.get("fallback_applied"))
                    if isinstance(preview_meta, dict) and isinstance(preview_meta.get("quick_replies"), list):
                        response["quick_replies"] = _sanitize_quick_replies(
                            preview_meta.get("quick_replies"),
                            limit=4,
                        )
                    if preview_items:
                        response["quick_replies"] = _merge_quick_replies(
                            response.get("quick_replies") if isinstance(response.get("quick_replies"), list) else [],
                            _build_recommendation_assist_quick_replies(
                                _build_accommodation_selection_cache(preview_items)
                            ),
                            limit=4,
                        )
                    return _chat_json_response(request, start_time, response)
            _save_chat_state(request, next_state)
            if question:
                return _chat_json_response(
                    request,
                    start_time,
                    {
                        "fulfillmentText": question,
                        "quick_replies": _slot_quick_replies(missing_slot),
                        "needs_clarification": True,
                        "missing_slot": missing_slot,
                    },
                )
        elif _is_personalization_accept_message(message):
            accepted_params = dict(params)
            accepted_params.pop("_personalization_opt_out", None)
            for key, value in state_offer_defaults.items():
                if accepted_params.get(key) in ("", None):
                    accepted_params[key] = value
            params = accepted_params
            intent = state_intent
            _save_chat_state(
                request,
                {
                    "pending_intent": state_intent,
                    "params": params,
                    "skip_default_offer": False,
                },
            )
        elif not _looks_like_slot_update(params):
            return _chat_json_response(
                request,
                start_time,
                {"fulfillmentText": state_offer_prompt or PERSONALIZATION_PROMPT_DEFAULT},
            )

    if (
        not needs_parser_clarification
        and parse_source == "heuristic_intent_fallback"
        and parse_confidence < 0.45
        and intent == "get_recommendation"
        and not _looks_like_slot_update(params)
        and not _looks_like_tour_request(message)
    ):
        role_clarifier = _role_aware_clarification_payload(actor)
        if actor.get("role") == "guest":
            clarifier_text = _pick_response_variant(
                [
                    str(role_clarifier.get("text") or _clarification_fallback("Do you want help with places to stay, tour packages, directions, dining, or full trip planning?")),
                    "I can help with several things in one place.\nAre you asking about accommodations, tour packages, directions, dining, or planning your stay?",
                    "Let me guide you to the right part.\nWould you like help with approved stays, tours, directions, dining, or full trip planning?",
                ],
                seed_text=f"{message}|guest-low-confidence-clarifier",
            )
            clarifier_replies = _sanitize_quick_replies(role_clarifier.get("quick_replies"), limit=4)
        else:
            clarifier_text = str(
                role_clarifier.get("text")
                or _clarification_fallback("Are you asking about dashboard monitoring, reports, or navigation to a module?")
            )
            clarifier_replies = _sanitize_quick_replies(role_clarifier.get("quick_replies"), limit=4)
        _save_chat_state(
            request,
            {
                "pending_intent": "clarification",
                "params": params,
                "missing_slot": "clarification",
            },
        )
        return _chat_json_response(
            request,
            start_time,
            {
                "fulfillmentText": clarifier_text,
                "quick_replies": clarifier_replies,
                "needs_clarification": True,
                "confidence": parse_confidence,
            },
        )

    if needs_parser_clarification:
        if not parser_clarification_question:
            role_clarifier = _role_aware_clarification_payload(actor)
            parser_clarification_question = _pick_response_variant(
                [
                    str(role_clarifier.get("text") or _clarification_fallback("Could you share a bit more so I can guide you correctly?")),
                    "I can help with that. Can you clarify what you need most right now?",
                    "To help you better, could you clarify your request a little more?",
                ],
                seed_text=message,
            )
        clarification_quick_replies = _merge_quick_replies(
            _sanitize_quick_replies(parser_clarification_options, limit=4),
            _sanitize_quick_replies(_role_aware_clarification_payload(actor).get("quick_replies"), limit=4),
            _slot_quick_replies(parser_clarification_field),
            limit=4,
        )
        _save_chat_state(
            request,
            {
                "pending_intent": state_intent or intent or "get_accommodation_recommendation",
                "params": params,
                "missing_slot": parser_clarification_field or "clarification",
            },
        )
        return _chat_json_response(
            request,
            start_time,
            {
                "fulfillmentText": parser_clarification_question,
                "quick_replies": clarification_quick_replies,
                "needs_clarification": True,
                "confidence": parse_confidence,
            },
        )
    cnn_prediction = None
    cnn_error = None
    logged_recommended_items = []
    accommodation_meta = None
    billing_actions = {}
    out_of_scope_quick_replies = []

    if init_suggestions:
        if actor.get("role") != "guest":
            request._chatbot_log_context["resolved_intent"] = "role_help_init"
            help_payload = _build_role_help_payload(actor)
            return _chat_json_response(request, start_time, help_payload)
        suggestions_payload = _get_default_accommodation_suggestions(limit=3)
        default_reply = None
        recommendation_trace = []
        if isinstance(suggestions_payload, tuple):
            default_reply, recommendation_trace = suggestions_payload
        else:
            default_reply = suggestions_payload
        response = {"fulfillmentText": default_reply}
        if recommendation_trace:
            response["recommendation_trace"] = recommendation_trace
        _save_chat_state(
            request,
            {
                "pending_intent": "get_accommodation_recommendation",
                "params": {},
                "missing_slot": "",
                "last_accommodation_recommendations": _build_accommodation_selection_cache(recommendation_trace),
            },
        )
        return _chat_json_response(request, start_time, response)

    if intent in ("get_recommendation", "gettourrecommendation"):
        if actor.get("role") in {"owner", "admin", "employee"}:
            _clear_chat_state(request)
            role_help = _build_role_help_payload(actor)
            role = str(actor.get("role") or "").strip().lower()
            if role == "admin":
                role_clarifier = (
                    "I can help with reports, approvals, system activity, and monitoring. "
                    "Could you clarify what you want to check?"
                )
            elif role == "owner":
                role_clarifier = (
                    "I can help you manage your accommodations, rooms, links/images, and reports. "
                    "What would you like to do?"
                )
            else:
                role_clarifier = (
                    "I can assist with tourism records, assignments, and monitoring. "
                    "Please specify your request."
                )
            if _looks_like_tour_request(message):
                reply = (
                    role_clarifier
                    + "\nTour recommendation cards are guest-focused in chat. "
                    "Please use your dashboard workflows for staff operations."
                )
            else:
                reply = (
                    role_clarifier + "\n" + str(role_help.get("fulfillmentText") or "")
                )
            out_of_scope_quick_replies = _sanitize_quick_replies(
                role_help.get("quick_replies") if isinstance(role_help.get("quick_replies"), list) else [],
                limit=4,
            )
            logged_recommended_items = []
        else:
            if _is_tour_schedule_request(message) or _is_tour_schedule_request(raw_message):
                _clear_chat_state(request)
                schedule_payload = _build_tour_schedule_listing_payload(request, message, params)
                reply = str(schedule_payload.get("reply") or "Here are available tour schedules right now.").strip()
                logged_recommended_items = (
                    schedule_payload.get("items")
                    if isinstance(schedule_payload.get("items"), list)
                    else []
                )
                sched_ids = (
                    schedule_payload.get("sched_ids")
                    if isinstance(schedule_payload.get("sched_ids"), list)
                    else []
                )
                if sched_ids:
                    _save_chat_state(
                        request,
                        {
                            "last_tour_recommendation_sched_ids": sched_ids[:8],
                            "pending_intent": "get_recommendation",
                            "params": params,
                        },
                    )
                if isinstance(schedule_payload.get("quick_replies"), list):
                    billing_actions["quick_replies"] = _sanitize_quick_replies(
                        schedule_payload.get("quick_replies"),
                        limit=4,
                    )
            else:
                accommodation_like_request = bool(
                    re.search(r"\b(hotel|inn|room|accommodation|stay)\b", str(message or "").lower())
                ) and not _looks_like_tour_request(message)
                if accommodation_like_request and _to_int(params.get("guests"), default=0) <= 0:
                    missing_slot = "guests"
                    question = _build_dynamic_accommodation_slot_question(missing_slot, params)
                    _save_chat_state(
                        request,
                        {
                            "pending_intent": "get_accommodation_recommendation",
                            "params": params,
                            "missing_slot": missing_slot,
                        },
                    )
                    return _chat_json_response(
                        request,
                        start_time,
                        {
                            "fulfillmentText": question,
                            "quick_replies": _slot_quick_replies(missing_slot),
                            "needs_clarification": True,
                            "missing_slot": missing_slot,
                        },
                    )
                _clear_chat_state(request)
                _safe_log_recommendation_event(request, intent)
                try:
                    reply, logged_recommended_items = _get_recommendations(params)
                except Exception:
                    _safe_log_chat_runtime_event(
                        request,
                        event_key="tour_listing_fallback",
                        detail="tour_listing_error_clean_fallback",
                    )
                    reply = "No tours available right now."
                    logged_recommended_items = []
                if (
                    not logged_recommended_items
                    and _contains_any_phrase(str(reply or "").lower(), ("something went wrong", "error", "trouble loading"))
                ):
                    reply = "No tours available right now."
                if not logged_recommended_items and not str(reply or "").strip():
                    reply = "No tours available right now."
                rec_sched_ids = []
                for item in logged_recommended_items:
                    if not isinstance(item, dict):
                        continue
                    meta = item.get("meta") if isinstance(item.get("meta"), dict) else {}
                    sched_id = str(item.get("sched_id") or meta.get("sched_id") or "").strip()
                    if sched_id:
                        rec_sched_ids.append(sched_id)
                if rec_sched_ids:
                    _save_chat_state(
                        request,
                        {
                            "last_tour_recommendation_sched_ids": rec_sched_ids[:8],
                            "pending_intent": "get_recommendation",
                            "params": params,
                        },
                    )
    elif intent in ("get_tourism_information",):
        _clear_chat_state(request)
        reply = _get_tourism_information(params, message)
        if actor.get("role") == "guest" and _is_dining_query(message):
            try:
                map_url = reverse("map")
                if hasattr(request, "build_absolute_uri"):
                    map_url = request.build_absolute_uri(map_url)
                billing_actions["billing_link"] = map_url
                billing_actions["billing_link_label"] = "Open Map"
            except Exception:
                pass
            billing_actions["quick_replies"] = _sanitize_quick_replies(
                [
                    "How far is this from me?",
                    "Show nearby landmarks",
                    "Plan my Bayawan stay with 5000 budget",
                ],
                limit=4,
            )
    elif intent in ("reporting_summary",):
        _clear_chat_state(request)
        reporting_payload = _build_reporting_summary_payload(message, params)
        reply = str(reporting_payload.get("reply") or "").strip()
        if isinstance(reporting_payload.get("quick_replies"), list):
            billing_actions["quick_replies"] = _sanitize_quick_replies(
                reporting_payload.get("quick_replies"),
                limit=4,
            )
    elif intent in ("travel_guidance",):
        _clear_chat_state(request)
        guidance = _build_travel_guidance_payload(message, params, client_location)
        reply = str(guidance.get("reply") or "").strip() or "I can guide you with directions. Please share your destination."
        if isinstance(guidance.get("quick_replies"), list):
            billing_actions["quick_replies"] = _sanitize_quick_replies(guidance.get("quick_replies"), limit=4)
        if guidance.get("link"):
            billing_actions["billing_link"] = str(guidance.get("link"))
            billing_actions["billing_link_label"] = str(guidance.get("link_label") or "Open Map")
    elif intent in ("plan_bayawan_stay",):
        if actor.get("role") in {"owner", "admin", "employee"}:
            _clear_chat_state(request)
            role_help = _build_role_help_payload(actor)
            reply = (
                "Budget-based stay planning is available in guest tourism-assistant mode. "
                + str(role_help.get("fulfillmentText") or "")
            )
            out_of_scope_quick_replies = _sanitize_quick_replies(
                role_help.get("quick_replies") if isinstance(role_help.get("quick_replies"), list) else [],
                limit=4,
            )
        else:
            planning_payload = _build_budget_stay_plan_payload(params, message)
            if planning_payload.get("needs_clarification"):
                next_params = (
                    planning_payload.get("params")
                    if isinstance(planning_payload.get("params"), dict)
                    else dict(params)
                )
                missing_slot = str(planning_payload.get("missing_slot") or "").strip() or "total_budget"
                _save_chat_state(
                    request,
                    {
                        "pending_intent": "plan_bayawan_stay",
                        "params": next_params,
                        "missing_slot": missing_slot,
                    },
                )
                return _chat_json_response(
                    request,
                    start_time,
                    {
                        "fulfillmentText": str(planning_payload.get("question") or "").strip(),
                        "quick_replies": _slot_quick_replies(missing_slot),
                        "needs_clarification": True,
                        "missing_slot": missing_slot,
                    },
                )

            current_plan_signature = str(planning_payload.get("plan_signature") or "").strip()
            previous_plan_signature = str(chat_state.get("last_plan_signature") or "").strip()
            if current_plan_signature and previous_plan_signature and current_plan_signature == previous_plan_signature:
                current_total_budget = _to_int(
                    (planning_payload.get("params") if isinstance(planning_payload.get("params"), dict) else {}).get("total_budget"),
                    default=0,
                )
                if bool(re.search(r"\b(make it cheaper|cheaper|lower budget|budget version|something cheaper)\b", lowered_message)):
                    short_update = (
                        "These are already the lowest-cost options I found for your current filters. "
                        "If you want cheaper results, try a different date range, fewer days, or a broader location."
                    )
                    if current_total_budget > 0:
                        short_update = f"{short_update} Current budget target: PHP {current_total_budget:,}."
                else:
                    short_update = (
                        "These are still your best matches with the same details. "
                        "If you want a different result, share a new budget, number of days, or group size."
                    )
                _save_chat_state(
                    request,
                    {
                        "pending_intent": "plan_bayawan_stay",
                        "params": (
                            planning_payload.get("params")
                            if isinstance(planning_payload.get("params"), dict)
                            else dict(params)
                        ),
                        "missing_slot": "",
                        "last_plan_signature": current_plan_signature,
                    },
                )
                return _chat_json_response(
                    request,
                    start_time,
                    {
                        "fulfillmentText": short_update,
                        "quick_replies": _sanitize_quick_replies(
                            planning_payload.get("quick_replies")
                            if isinstance(planning_payload.get("quick_replies"), list)
                            else [],
                            limit=4,
                        ),
                    },
                )

            _save_chat_state(
                request,
                {
                    "pending_intent": "plan_bayawan_stay",
                    "params": (
                        planning_payload.get("params")
                        if isinstance(planning_payload.get("params"), dict)
                        else dict(params)
                    ),
                    "missing_slot": "",
                    "last_plan_signature": current_plan_signature,
                },
            )
            reply = str(planning_payload.get("reply") or "").strip()
            billing_actions["quick_replies"] = _sanitize_quick_replies(
                planning_payload.get("quick_replies") if isinstance(planning_payload.get("quick_replies"), list) else [],
                limit=4,
            )
            if planning_payload.get("billing_link"):
                billing_actions["billing_link"] = str(planning_payload.get("billing_link"))
                billing_actions["billing_link_label"] = str(
                    planning_payload.get("billing_link_label") or "Open Official Link"
                )
    elif intent in ("calculate_billing", "calculatetourbilling"):
        if actor.get("role") in {"owner", "admin", "employee"}:
            _clear_chat_state(request)
            reply = "Tour billing computation in chat is for guest bookings. Please use dashboard records for staff workflows."
        else:
            _clear_chat_state(request)
            reply = _calculate_billing(params)
            treasurer_url = str(
                getattr(settings, "TOURISM_TREASURER_BILLING_URL", "")
                or getattr(settings, "TOURISM_OFFICE_BILLING_URL", "")
                or os.getenv("TOURISM_TREASURER_BILLING_URL", "")
                or os.getenv("TOURISM_OFFICE_BILLING_URL", "")
                or "https://bayawancity.gov.ph/"
            ).strip()
            if treasurer_url:
                billing_actions["billing_link"] = treasurer_url
                billing_actions["billing_link_label"] = "Proceed to Treasurer Billing"
                billing_actions["quick_replies"] = [
                    "show my tour bookings",
                    "show available tours",
                ]
    elif intent in ("get_accommodation_recommendation", "gethotelrecommendation"):
        if actor.get("role") in {"owner", "admin", "employee"}:
            _clear_chat_state(request)
            role_help = _build_role_help_payload(actor)
            reply = (
                "Accommodation recommendation cards are shown for guest discovery flow only. "
                + str(role_help.get("fulfillmentText") or "")
            )
            logged_recommended_items = []
            accommodation_meta = None
        else:
            if _is_accommodation_room_listing_command(message):
                target_name = _extract_accommodation_name_for_room_listing(
                    message,
                    cached_accommodation_rows,
                    state_params=params if isinstance(params, dict) else {},
                )
                room_listing_payload = _build_room_listing_response_for_accommodation(target_name, limit=5)
                if isinstance(room_listing_payload.get("recommendation_trace"), list):
                    next_state = dict(chat_state)
                    next_state["last_accommodation_recommendations"] = _build_accommodation_selection_cache(
                        room_listing_payload.get("recommendation_trace")
                    )
                    listing_params = dict(params) if isinstance(params, dict) else {}
                    selected_id = _to_int(room_listing_payload.get("selected_accommodation_id"), default=0)
                    selected_name = str(room_listing_payload.get("selected_accommodation_name") or "").strip()
                    if selected_id > 0:
                        listing_params["selected_accommodation_id"] = selected_id
                    if selected_name:
                        listing_params["selected_accommodation_name"] = selected_name
                        listing_params["accom_name"] = selected_name
                    next_state["params"] = listing_params
                    next_state["pending_intent"] = "get_accommodation_recommendation"
                    next_state["missing_slot"] = ""
                    next_state.pop("pending_booking", None)
                    _save_chat_state(request, next_state)
                return _chat_json_response(request, start_time, room_listing_payload)

            if _is_broad_accommodation_discovery_query(message, params):
                broad_payload = _build_broad_accommodation_discovery_response(params=params, limit=5)
                if isinstance(broad_payload.get("recommendation_trace"), list):
                    next_state = dict(chat_state)
                    next_state["last_accommodation_recommendations"] = _build_accommodation_selection_cache(
                        broad_payload.get("recommendation_trace")
                    )
                    broad_params = dict(params) if isinstance(params, dict) else {}
                    next_state["pending_intent"] = "get_accommodation_recommendation"
                    next_state["params"] = broad_params
                    next_state["missing_slot"] = ""
                    next_state.pop("pending_booking", None)
                    _save_chat_state(request, next_state)
                return _chat_json_response(request, start_time, broad_payload)

            missing_slot, question = _next_accommodation_clarifying_question(params)
            if missing_slot and _has_sufficient_accommodation_details(params):
                # Enough constraints are already present for recommendation;
                # avoid re-asking optional slots like budget.
                missing_slot = ""
                question = ""
            if missing_slot:
                baseline = _infer_user_accommodation_baseline(user)
                defaults = _build_personalization_defaults(params, baseline)
                personalization_prompt = _build_personalization_offer_text(defaults, baseline)
                suppress_default_offer = _is_vague_accommodation_request(message, params)
                if (
                    (not suppress_default_offer)
                    and (not state_skip_default_offer)
                    and (not _has_sufficient_accommodation_details(params))
                    and defaults
                    and personalization_prompt
                ):
                    _save_chat_state(
                        request,
                        {
                            "pending_intent": "get_accommodation_recommendation",
                            "params": params,
                            "missing_slot": missing_slot,
                            "skip_default_offer": False,
                            "default_offer": {
                                "defaults": defaults,
                                "prompt": personalization_prompt,
                            },
                        },
                    )
                    return _chat_json_response(
                        request,
                        start_time,
                        {
                            "fulfillmentText": personalization_prompt,
                        },
                    )

                company_type = str(params.get("company_type") or "").strip().lower()
                location = str(params.get("location") or "").strip()
                preference_tags = (
                    params.get("preference_tags")
                    if isinstance(params.get("preference_tags"), list)
                    else []
                )
                preview_key = f"{company_type}|{location.lower()}|{','.join(sorted(str(tag) for tag in preference_tags))}"
                if (
                    missing_slot in ("stay_details", "budget", "accommodation_details")
                    and state_missing_slot != "location"
                    and company_type in ("hotel", "inn", "either")
                    and (location or preference_tags)
                    and str(chat_state.get("location_preview_for") or "") != preview_key
                ):
                    preview_params = dict(params)
                    preview_reply, preview_items, preview_meta = _safe_get_accommodation_recommendations(preview_params)
                    if (
                        "top hotel/inn recommendations" not in str(preview_reply or "").lower()
                        and company_type in ("hotel", "inn")
                    ):
                        preview_any_type_params = dict(preview_params)
                        preview_any_type_params["company_type"] = "either"
                        preview_reply, preview_items, preview_meta = _safe_get_accommodation_recommendations(preview_any_type_params)
                    if "top hotel/inn recommendations" in str(preview_reply or "").lower():
                        next_missing_slot, next_question = _next_accommodation_clarifying_question(params)
                        next_state = {
                            "pending_intent": "get_accommodation_recommendation",
                            "params": params,
                            "missing_slot": next_missing_slot or "budget",
                            "skip_default_offer": state_skip_default_offer,
                            "location_preview_for": preview_key,
                            "last_accommodation_recommendations": _build_accommodation_selection_cache(preview_items),
                        }
                        _save_chat_state(request, next_state)
                        prompt_suffix = (
                            f"To refine this recommendation, {next_question}"
                            if next_question else
                            "Tell me which option you want to explore and I'll share the official page."
                        )
                        response = {
                            "fulfillmentText": f"{preview_reply}\n\n{prompt_suffix}"
                        }
                        if preview_items:
                            response["recommendation_trace"] = preview_items
                        if isinstance(preview_meta, dict) and preview_meta.get("fallback_applied"):
                            response["recommendation_fallback"] = str(preview_meta.get("fallback_applied"))
                        if isinstance(preview_meta, dict) and isinstance(preview_meta.get("quick_replies"), list):
                            response["quick_replies"] = _sanitize_quick_replies(
                                preview_meta.get("quick_replies"),
                                limit=4,
                            )
                        if preview_items:
                            response["quick_replies"] = _merge_quick_replies(
                                response.get("quick_replies") if isinstance(response.get("quick_replies"), list) else [],
                                _build_recommendation_assist_quick_replies(
                                    _build_accommodation_selection_cache(preview_items)
                                ),
                                limit=4,
                            )
                        _inject_recommendation_context(response, preview_params)
                        return _chat_json_response(request, start_time, response)

                _save_chat_state(
                    request,
                    {
                        "pending_intent": "get_accommodation_recommendation",
                        "params": params,
                        "missing_slot": missing_slot,
                        "skip_default_offer": state_skip_default_offer,
                        "location_preview_for": chat_state.get("location_preview_for", ""),
                    },
                )
                if (
                    suppress_default_offer
                    and _to_int(params.get("guests"), default=0) <= 0
                    and _to_int(params.get("budget"), default=0) <= 0
                ):
                    question = "Sure. I can help you find a place to stay. How many guests and what budget per night?"
                return _chat_json_response(
                    request,
                    start_time,
                    {
                        "fulfillmentText": question,
                        "quick_replies": _slot_quick_replies(missing_slot),
                        "needs_clarification": True,
                        "missing_slot": missing_slot,
                    },
                )
            _safe_log_recommendation_event(request, intent)
            cnn_prediction, cnn_error = _predict_accommodation_class_from_text(message)
            if cnn_prediction and isinstance(params, dict):
                # Allow the recommender (or future logic) to use the predicted class.
                params.setdefault("predicted_accommodation_type", cnn_prediction["predicted_class"])
                params.setdefault("predicted_accommodation_confidence", float(cnn_prediction.get("confidence", 0.0)))
                try:
                    log_ctx = getattr(request, "_chatbot_log_context", None)
                    if isinstance(log_ctx, dict):
                        prov = log_ctx.get("provenance") if isinstance(log_ctx.get("provenance"), dict) else {}
                        runtime_models = prov.get("runtime_models") if isinstance(prov.get("runtime_models"), dict) else {}
                        runtime_models["accommodation_cnn_artifact_source"] = str(cnn_prediction.get("artifact_source") or "")[:80]
                        runtime_models["accommodation_cnn_predicted_class"] = str(cnn_prediction.get("predicted_class") or "")[:80]
                        runtime_models["accommodation_cnn_confidence"] = float(cnn_prediction.get("confidence", 0.0) or 0.0)
                        prov["runtime_models"] = runtime_models
                        log_ctx["provenance"] = prov
                except Exception:
                    pass
            reply, logged_recommended_items, accommodation_meta = _safe_get_accommodation_recommendations(params)
            next_state = {
                "pending_intent": "get_accommodation_recommendation",
                "params": params,
                "missing_slot": "",
                "skip_default_offer": False,
                "last_accommodation_recommendations": _build_accommodation_selection_cache(logged_recommended_items),
            }
            if (
                isinstance(accommodation_meta, dict)
                and "budget_too_low" in (accommodation_meta.get("no_match_reasons") or [])
                and _to_int(accommodation_meta.get("suggested_budget_min"), default=0) > 0
            ):
                next_state["pending_budget_offer"] = {
                    "suggested_budget": _to_int(accommodation_meta.get("suggested_budget_min"), default=0)
                }
            _save_chat_state(request, next_state)
            show_cnn_debug_in_chat = str(os.getenv("CHATBOT_SHOW_CNN_DEBUG", "0")).strip().lower() in (
                "1",
                "true",
                "yes",
                "on",
            )
            if (
                show_cnn_debug_in_chat
                and cnn_prediction
                and reply.startswith("I couldn't find a matching hotel or inn right now.")
            ):
                reply = f"{reply}\n\n{_format_cnn_prediction_for_chat(cnn_prediction)}"
    elif intent in ("calculate_accommodation_billing", "calculatehotelbilling"):
        _clear_chat_state(request)
        resolution = _resolve_preview_room_selection(
            params=params,
            message=message,
            cached_rows=cached_accommodation_rows,
        )
        if isinstance(resolution.get("ambiguous_names"), list) and resolution.get("ambiguous_names"):
            options = [str(v) for v in resolution.get("ambiguous_names")[:3] if str(v).strip()]
            return _chat_json_response(
                request,
                start_time,
                {
                    "fulfillmentText": f"Which accommodation do you mean: {', '.join(options)}?",
                    "quick_replies": options,
                    "needs_clarification": True,
                    "missing_slot": "accommodation_name",
                },
            )
        room = resolution.get("room")
        if resolution.get("needs_room_selection"):
            room_choices = [str(v) for v in (resolution.get("room_choices") or []) if str(v).strip()][:4]
            selected_accom_name = str(resolution.get("selected_accommodation_name") or "").strip()
            prompt = (
                f"Which room would you like to preview at {selected_accom_name}?"
                if selected_accom_name
                else "Which room would you like to preview?"
            )
            next_state = dict(chat_state)
            preview_params = dict(params)
            if _to_int(resolution.get("selected_accommodation_id"), default=0) > 0:
                preview_params["selected_accommodation_id"] = _to_int(resolution.get("selected_accommodation_id"), default=0)
            if selected_accom_name:
                preview_params["selected_accommodation_name"] = selected_accom_name
                preview_params["accom_name"] = selected_accom_name
            next_state["pending_intent"] = "book_accommodation"
            next_state["params"] = preview_params
            next_state["missing_slot"] = "room_reference"
            next_state["pending_booking"] = {
                "params": preview_params,
                "created_at": int(time.time()),
                "stage": "collecting_details",
            }
            _save_chat_state(request, next_state)
            return _chat_json_response(
                request,
                start_time,
                {
                    "fulfillmentText": prompt,
                    "quick_replies": room_choices or ["show rooms"],
                    "needs_clarification": True,
                    "missing_slot": "room_reference",
                },
            )
        if resolution.get("not_found_name"):
            not_found = str(resolution.get("not_found_name") or "").strip()
            _save_chat_state(request, _clear_invalid_preview_context(chat_state))
            return _chat_json_response(
                request,
                start_time,
                {
                    "fulfillmentText": (
                        f"I couldn't find {not_found} in approved accommodations yet. "
                        "Please choose from available rooms or specify the accommodation."
                    ),
                    "quick_replies": ["show approved accommodations in bayawan"],
                    "needs_clarification": True,
                    "missing_slot": "accommodation_name",
                },
            )
        if resolution.get("not_found_room_ref"):
            room_choices = [str(v) for v in (resolution.get("room_choices") or []) if str(v).strip()][:4]
            selected_accom_name = str(resolution.get("selected_accommodation_name") or "").strip()
            selected_accom_id = _to_int(resolution.get("selected_accommodation_id"), default=0)
            next_state = dict(chat_state)
            next_params = dict(params)
            if selected_accom_id > 0:
                next_params["selected_accommodation_id"] = selected_accom_id
            if selected_accom_name:
                next_params["selected_accommodation_name"] = selected_accom_name
                next_params["accom_name"] = selected_accom_name
            next_state["pending_intent"] = "book_accommodation"
            next_state["params"] = next_params
            next_state["missing_slot"] = "room_reference"
            next_state["pending_booking"] = {
                "params": next_params,
                "created_at": int(time.time()),
                "stage": "collecting_details",
            }
            _save_chat_state(request, next_state)
            return _chat_json_response(
                request,
                start_time,
                {
                    "fulfillmentText": (
                        (
                            f"I couldn't find that room in {selected_accom_name}. Please choose a room from this accommodation."
                            if selected_accom_name
                            else "I couldn't find that room. Please choose from available rooms or specify the accommodation."
                        )
                    ),
                    "quick_replies": room_choices or ["show rooms"],
                    "needs_clarification": True,
                    "missing_slot": "room_reference",
                },
            )
        preview_payload = _build_accommodation_preview_response(room=room, params=params)
        if preview_payload.get("ready"):
            reply = str(preview_payload.get("text") or "").strip()
            billing_actions["quick_replies"] = _sanitize_quick_replies(
                preview_payload.get("quick_replies"),
                limit=4,
            )
            if preview_payload.get("billing_link"):
                billing_actions["billing_link"] = str(preview_payload.get("billing_link"))
                billing_actions["billing_link_label"] = str(
                    preview_payload.get("billing_link_label") or "Continue to Official Booking Page"
                )
            if isinstance(preview_payload.get("link_actions"), list):
                billing_actions["link_actions"] = preview_payload.get("link_actions")[:4]
        else:
            reply = (
                "I can prepare an accommodation booking preview for you. "
                "Please share missing details so I can estimate total cost."
            )
            billing_actions["quick_replies"] = _sanitize_quick_replies(
                preview_payload.get("quick_replies"),
                limit=4,
            )
        next_state = dict(chat_state)
        next_state["pending_intent"] = "book_accommodation"
        preview_params = dict(params)
        if room is not None:
            preview_params["selected_room_id"] = _to_int(getattr(room, "room_id", 0), default=0)
            preview_params["selected_room_name"] = str(getattr(room, "room_name", "") or "").strip()
            preview_params["selected_accommodation_id"] = _to_int(getattr(room, "accommodation_id", 0), default=0)
            preview_params["selected_accommodation_name"] = str(
                getattr(getattr(room, "accommodation", None), "company_name", "") or ""
            ).strip()
            preview_params["accom_name"] = preview_params["selected_accommodation_name"]
            preview_params["nightly_rate"] = str(getattr(room, "price_per_night", "") or "").strip()
            preview_params["selection_source"] = str(resolution.get("source") or "fallback")
        next_state["params"] = preview_params
        next_state["missing_slot"] = ""
        next_state["pending_booking"] = {
            "params": preview_params,
            "created_at": int(time.time()),
            "stage": "collecting_details",
        }
        _save_chat_state(request, next_state)
    elif intent in ("book_accommodation", "bookhotel", "book_hotel", "reserve_accommodation"):
        resolution = _resolve_preview_room_selection(
            params=params,
            message=message,
            cached_rows=cached_accommodation_rows,
        )
        if isinstance(resolution.get("ambiguous_names"), list) and resolution.get("ambiguous_names"):
            options = [str(v) for v in resolution.get("ambiguous_names")[:3] if str(v).strip()]
            return _chat_json_response(
                request,
                start_time,
                {
                    "fulfillmentText": f"Which accommodation do you mean: {', '.join(options)}?",
                    "quick_replies": options,
                    "needs_clarification": True,
                    "missing_slot": "accommodation_name",
                },
            )
        room = resolution.get("room")
        if resolution.get("needs_room_selection"):
            room_choices = [str(v) for v in (resolution.get("room_choices") or []) if str(v).strip()][:4]
            selected_accom_name = str(resolution.get("selected_accommodation_name") or "").strip()
            prompt = (
                f"Which room would you like to preview at {selected_accom_name}?"
                if selected_accom_name
                else "Which room would you like to preview?"
            )
            next_state = dict(chat_state)
            preview_params = dict(params)
            if _to_int(resolution.get("selected_accommodation_id"), default=0) > 0:
                preview_params["selected_accommodation_id"] = _to_int(resolution.get("selected_accommodation_id"), default=0)
            if selected_accom_name:
                preview_params["selected_accommodation_name"] = selected_accom_name
                preview_params["accom_name"] = selected_accom_name
            next_state["pending_intent"] = "book_accommodation"
            next_state["params"] = preview_params
            next_state["missing_slot"] = "room_reference"
            next_state["pending_booking"] = {
                "params": preview_params,
                "created_at": int(time.time()),
                "stage": "collecting_details",
            }
            _save_chat_state(request, next_state)
            return _chat_json_response(
                request,
                start_time,
                {
                    "fulfillmentText": prompt,
                    "quick_replies": room_choices or ["show rooms"],
                    "needs_clarification": True,
                    "missing_slot": "room_reference",
                },
            )
        if resolution.get("not_found_name"):
            missing_name = str(resolution.get("not_found_name") or "").strip()
            _save_chat_state(request, _clear_invalid_preview_context(chat_state))
            return _chat_json_response(
                request,
                start_time,
                {
                    "fulfillmentText": (
                        f"I couldn't find {missing_name} in approved accommodation listings yet. "
                        "Please choose from available rooms or specify the accommodation."
                    ),
                    "quick_replies": ["show approved accommodations in bayawan"],
                    "needs_clarification": True,
                    "missing_slot": "accommodation_name",
                },
            )
        if resolution.get("not_found_room_ref"):
            room_choices = [str(v) for v in (resolution.get("room_choices") or []) if str(v).strip()][:4]
            selected_accom_name = str(resolution.get("selected_accommodation_name") or "").strip()
            selected_accom_id = _to_int(resolution.get("selected_accommodation_id"), default=0)
            next_state = dict(chat_state)
            next_params = dict(params)
            if selected_accom_id > 0:
                next_params["selected_accommodation_id"] = selected_accom_id
            if selected_accom_name:
                next_params["selected_accommodation_name"] = selected_accom_name
                next_params["accom_name"] = selected_accom_name
            next_state["pending_intent"] = "book_accommodation"
            next_state["params"] = next_params
            next_state["missing_slot"] = "room_reference"
            next_state["pending_booking"] = {
                "params": next_params,
                "created_at": int(time.time()),
                "stage": "collecting_details",
            }
            _save_chat_state(request, next_state)
            return _chat_json_response(
                request,
                start_time,
                {
                    "fulfillmentText": (
                        (
                            f"I couldn't find that room in {selected_accom_name}. Please choose a room from this accommodation."
                            if selected_accom_name
                            else "I couldn't find that room. Please choose from available rooms or specify the accommodation."
                        )
                    ),
                    "quick_replies": room_choices or ["show rooms"],
                    "needs_clarification": True,
                    "missing_slot": "room_reference",
                },
            )
        if _is_accommodation_how_to_book_request(message):
            request._chatbot_log_context["resolved_intent"] = "book_accommodation"
            guidance_text = (
                "You can complete your booking directly with the accommodation using their official page "
                "or contact details. I can also prepare a booking preview if you'd like to estimate your stay cost."
            )
            link_actions = _build_accommodation_link_actions(room=room, max_actions=4) if room is not None else []
            return _chat_json_response(
                request,
                start_time,
                {
                    "fulfillmentText": guidance_text,
                    "quick_replies": [
                        "create booking preview",
                    ],
                    "link_actions": link_actions,
                },
            )
        preview_payload = _build_accommodation_preview_response(room=room, params=params)
        if preview_payload.get("ready"):
            reply = str(preview_payload.get("text") or "").strip()
            billing_actions["quick_replies"] = _sanitize_quick_replies(
                preview_payload.get("quick_replies"),
                limit=4,
            )
            if preview_payload.get("billing_link"):
                billing_actions["billing_link"] = str(preview_payload.get("billing_link"))
                billing_actions["billing_link_label"] = str(
                    preview_payload.get("billing_link_label") or "Continue to Official Booking Page"
                )
            if isinstance(preview_payload.get("link_actions"), list):
                billing_actions["link_actions"] = preview_payload.get("link_actions")[:4]
            next_stage = "awaiting_handoff"
        else:
            reply = (
                "I can prepare an accommodation booking preview for you. "
                "Final booking is completed directly with the accommodation.\n"
                f"{str(preview_payload.get('question') or '').strip()}"
            ).strip()
            billing_actions["quick_replies"] = _sanitize_quick_replies(
                preview_payload.get("quick_replies"),
                limit=4,
            )
            next_stage = "collecting_details"

        next_state = dict(chat_state)
        next_state["pending_intent"] = "book_accommodation"
        preview_params = dict(params)
        if room is not None:
            preview_params["selected_room_id"] = _to_int(getattr(room, "room_id", 0), default=0)
            preview_params["selected_room_name"] = str(getattr(room, "room_name", "") or "").strip()
            preview_params["selected_accommodation_id"] = _to_int(getattr(room, "accommodation_id", 0), default=0)
            preview_params["selected_accommodation_name"] = str(
                getattr(getattr(room, "accommodation", None), "company_name", "") or ""
            ).strip()
            preview_params["accom_name"] = preview_params["selected_accommodation_name"]
            preview_params["nightly_rate"] = str(getattr(room, "price_per_night", "") or "").strip()
            preview_params["selection_source"] = str(resolution.get("source") or "fallback")
        next_state["params"] = preview_params
        next_state["missing_slot"] = ""
        next_state["pending_booking"] = {
            "params": preview_params,
            "created_at": int(time.time()),
            "stage": next_stage,
        }
        _save_chat_state(request, next_state)
    else:
        if intent not in continuation_intents:
            _clear_chat_state(request)
        fallback_payload = _build_out_of_scope_payload(actor, message=message)
        reply = str(fallback_payload.get("fulfillmentText") or "").strip()
        if isinstance(fallback_payload.get("quick_replies"), list):
            out_of_scope_quick_replies = _sanitize_quick_replies(
                fallback_payload.get("quick_replies"),
                limit=4,
            )

    normalized_reply = str(reply or "").strip().lower()
    if intent in (
        "get_accommodation_recommendation",
        "gethotelrecommendation",
        "get_recommendation",
        "reporting_summary",
    ):
        if normalized_reply.startswith("i couldn't find") or normalized_reply.startswith("no data available yet"):
            if intent == "reporting_summary":
                reply = str(reply or "No reporting data is available yet.").strip()
            else:
                reply = f"{reply}\n{_no_data_fallback()}"

    final_reply, nlg_source = generate_final_ai_response(
        request=request,
        intent=intent,
        user_message=raw_message,
        backend_reply=reply,
    )
    # Phase 2 UX consistency: keep outbound responses in English to avoid
    # unexpected language switching within the same conversation.
    translated_back = False

    request._chatbot_log_context["response_nlg_source"] = nlg_source
    response = {"fulfillmentText": final_reply}
    if 'booking_result' in locals():
        if booking_result.get("billing_link"):
            response["billing_link"] = booking_result["billing_link"]
            if booking_result.get("billing_link_label"):
                response["billing_link_label"] = booking_result["billing_link_label"]
        if booking_result.get("booking_id") is not None:
            response["booking_id"] = booking_result["booking_id"]
        if booking_result.get("receipt_text"):
            response["receipt_text"] = booking_result["receipt_text"]
            response["receipt_filename"] = booking_result.get("receipt_filename") or "ibayaw_booking_receipt.png"
        if isinstance(booking_result.get("quick_replies"), list):
            response["quick_replies"] = _sanitize_quick_replies(
                booking_result.get("quick_replies"),
                limit=4,
            )
        if booking_result.get("booking_id") and not booking_result.get("requires_confirmation"):
            response["show_feedback_prompt"] = True
    if isinstance(billing_actions, dict) and billing_actions:
        if isinstance(billing_actions.get("quick_replies"), list):
            response["quick_replies"] = _sanitize_quick_replies(
                billing_actions.get("quick_replies"),
                limit=4,
            )
        if isinstance(billing_actions.get("link_actions"), list):
            response["link_actions"] = billing_actions.get("link_actions")[:3]
        if billing_actions.get("billing_link"):
            response["billing_link"] = str(billing_actions.get("billing_link"))
            response["billing_link_label"] = str(
                billing_actions.get("billing_link_label") or "Open Official Link"
            )
    if out_of_scope_quick_replies:
        response["quick_replies"] = out_of_scope_quick_replies
    if cnn_prediction:
        response["cnn_prediction"] = cnn_prediction
    elif cnn_error and intent in ("get_accommodation_recommendation", "gethotelrecommendation"):
        response["cnn_prediction_error"] = cnn_error
    if isinstance(intent_classifier, dict):
        response["intent_classifier"] = {
            "source": str(intent_classifier.get("source") or ""),
            "confidence": float(intent_classifier.get("confidence", 0.0) or 0.0),
            "error": str(intent_classifier.get("error") or ""),
            "artifact_source": str(intent_classifier.get("artifact_source") or ""),
            "top_3": intent_classifier.get("top_3", []),
        }
    response["response_nlg_source"] = nlg_source
    if logged_recommended_items and intent in ("get_accommodation_recommendation", "gethotelrecommendation"):
        response["recommendation_trace"] = logged_recommended_items
        first_item = logged_recommended_items[0] if isinstance(logged_recommended_items, list) and logged_recommended_items else {}
        first_meta = first_item.get("meta") if isinstance(first_item, dict) and isinstance(first_item.get("meta"), dict) else {}
        first_link_actions = _build_accommodation_link_actions(row_meta=first_meta, max_actions=3)
        if first_link_actions:
            response["link_actions"] = first_link_actions
        official_link, official_label = _build_accommodation_official_link(row_meta=first_meta)
        if official_link:
            response["billing_link"] = official_link
            response["billing_link_label"] = official_label or "Open Official Link"
        assist_qr = _build_recommendation_assist_quick_replies(
            _build_accommodation_selection_cache(logged_recommended_items)
        )
        response["quick_replies"] = _merge_quick_replies(
            response.get("quick_replies") if isinstance(response.get("quick_replies"), list) else [],
            assist_qr,
            limit=4,
        )
        _inject_recommendation_context(response, params if isinstance(params, dict) else {})
    if logged_recommended_items and intent in ("get_recommendation", "gettourrecommendation"):
        response["recommendation_trace"] = logged_recommended_items
        rec_sched_ids = []
        for row in logged_recommended_items:
            if not isinstance(row, dict):
                continue
            sched_id = str(row.get("sched_id") or "").strip()
            if not sched_id:
                meta = row.get("meta") if isinstance(row.get("meta"), dict) else {}
                sched_id = str(meta.get("sched_id") or "").strip()
            if sched_id:
                rec_sched_ids.append(sched_id)
        if rec_sched_ids:
            merged_state = _load_chat_state(request)
            if not isinstance(merged_state, dict):
                merged_state = {}
            merged_state["last_tour_recommendation_sched_ids"] = rec_sched_ids[:8]
            _save_chat_state(request, merged_state)
    if accommodation_meta and intent in ("get_accommodation_recommendation", "gethotelrecommendation"):
        if isinstance(accommodation_meta, dict):
            no_match_reasons = accommodation_meta.get("no_match_reasons")
            suggested_budget_min = accommodation_meta.get("suggested_budget_min")
            fallback_applied = accommodation_meta.get("fallback_applied")
            quick_replies = accommodation_meta.get("quick_replies")
            if isinstance(no_match_reasons, list):
                response["no_match_reasons"] = [str(item) for item in no_match_reasons]
            if suggested_budget_min not in (None, ""):
                response["suggested_budget_min"] = suggested_budget_min
            if fallback_applied:
                response["recommendation_fallback"] = str(fallback_applied)
            if isinstance(quick_replies, list):
                response["quick_replies"] = _sanitize_quick_replies(quick_replies, limit=4)
            parsed_context = (
                accommodation_meta.get("parsed_context")
                if isinstance(accommodation_meta.get("parsed_context"), dict)
                else {}
            )
            response["recommendation_diagnostics"] = {
                "display_mode": str(
                    accommodation_meta.get("display_mode")
                    or accommodation_meta.get("view_mode")
                    or "accommodation_list"
                ),
                "fallback_stage": str(accommodation_meta.get("fallback_applied") or "none"),
                "fallback_reason_codes": [
                    str(code) for code in (accommodation_meta.get("fallback_reason_codes") or [])
                ],
                "extracted_location": str(parsed_context.get("location") or ""),
                "extracted_budget": _to_int(parsed_context.get("budget"), default=0),
                "extracted_budget_min": _to_int(parsed_context.get("budget_min"), default=0),
                "extracted_guests": _to_int(parsed_context.get("guests"), default=0),
                "extracted_type": str(parsed_context.get("company_type") or ""),
                "extracted_room_type": str(parsed_context.get("room_type") or ""),
            }
    if actor.get("role") == "guest" and intent in ("get_recommendation", "gettourrecommendation"):
        response["fulfillmentText"] = re.sub(
            r"\b(?:sched(?:ule)?\s*id[:\s-]*|sched)\s*[a-z]*\d+\b",
            "",
            str(response.get("fulfillmentText") or ""),
            flags=re.IGNORECASE,
        )
        if isinstance(response.get("recommendation_trace"), list):
            cleaned_trace = []
            for row in response.get("recommendation_trace"):
                if not isinstance(row, dict):
                    cleaned_trace.append(row)
                    continue
                cleaned_row = dict(row)
                cleaned_row["subtitle"] = re.sub(
                    r"\b(?:sched(?:ule)?\s*id[:\s-]*|sched)\s*[a-z]*\d+\s*\|?\s*",
                    "",
                    str(cleaned_row.get("subtitle") or ""),
                    flags=re.IGNORECASE,
                ).strip(" |")
                cleaned_trace.append(cleaned_row)
            response["recommendation_trace"] = cleaned_trace
    response["fulfillmentText"] = _apply_assistant_response_style(
        actor=actor,
        intent=intent,
        message=message,
        text=response.get("fulfillmentText") or "",
        params=params,
        memory=assistant_memory if isinstance(assistant_memory, dict) else {},
        needs_clarification=bool(response.get("needs_clarification")),
        nlg_source=str(response.get("response_nlg_source") or ""),
    )
    if actor.get("role") == "guest":
        response = _limit_assistant_quick_replies_for_guest(response, limit=3)
        _save_assistant_memory_from_context(
            request,
            actor=actor,
            intent=intent,
            params=params,
        )
    _safe_log_step_events_from_response(request, intent=intent, response_payload=response)
    _safe_log_recommendation_result_with_metadata(
        request,
        intent,
        final_reply,
        params,
        cnn_prediction=cnn_prediction,
        message_text=message,
        recommended_items=logged_recommended_items,
    )
    request._chatbot_log_context["provenance"] = {
        "recommendation_fallback": response.get("recommendation_fallback", ""),
        "has_recommendation_trace": bool(response.get("recommendation_trace")),
        "status_code": 200,
        "detected_language": detected_language,
        "input_translated_to_english": bool(
            str(raw_message or "").strip() and str(message or "").strip() and str(raw_message).strip() != str(message).strip()
        ),
        "response_translated_to_user_language": translated_back,
    }
    return _chat_json_response(request, start_time, response)


@csrf_exempt
def openai_chat(request):
    """
    Backward-compatible alias.
    Deprecated naming retained to avoid breaking existing routes/imports.
    """
    return ai_chat(request)


@csrf_exempt
def chat_runtime_health(request):
    if request.method != "GET":
        return JsonResponse({"error": "Method not allowed."}, status=405)

    actor = _resolve_chat_actor(request)
    user = actor.get("user")
    if not user or not getattr(user, "is_authenticated", False):
        return JsonResponse(
            {
                "fulfillmentText": "Please log in first to use this endpoint.",
                "error_code": "chat_requires_login",
            },
            status=401,
        )

    openai_key_present = bool(str(os.getenv("OPENAI_API_KEY", "") or "").strip())
    gemini_key_present = bool(str(os.getenv("GEMINI_API_KEY", "") or "").strip())
    nlg_enabled = str(
        os.getenv("CHATBOT_LLM_NLG_ENABLED", os.getenv("CHATBOT_OPENAI_NLG_ENABLED", "1"))
    ).strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    provider = "none"
    if openai_key_present and OpenAI is not None:
        provider = "openai"
    elif gemini_key_present and genai is not None:
        provider = "gemini"
    intent_cnn_path, intent_cnn_source = _resolve_intent_text_cnn_model_path()
    accom_cnn_path, accom_cnn_source = _resolve_accommodation_text_cnn_model_path()
    dt_status = get_decision_tree_runtime_status(force_reload=False)
    return JsonResponse(
        {
            "status": "ok",
            "chatbot": {
                "translation": translation_runtime_health(),
                "nlg": {
                    "enabled": nlg_enabled,
                    "api_key_present": openai_key_present,
                    "gemini_api_key_present": gemini_key_present,
                    "client_available": OpenAI is not None,
                    "gemini_client_available": genai is not None,
                    "provider": provider,
                    "model": str(os.getenv("OPENAI_MODEL", "gpt-4o-mini") or "").strip() or "gpt-4o-mini",
                    "gemini_model": str(os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite") or "").strip() or "gemini-2.5-flash-lite",
                },
                "models": {
                    "intent_cnn_path": str(intent_cnn_path),
                    "intent_cnn_source": intent_cnn_source,
                    "intent_cnn_exists": bool(intent_cnn_path.exists()),
                    "accommodation_cnn_path": str(accom_cnn_path),
                    "accommodation_cnn_source": accom_cnn_source,
                    "accommodation_cnn_exists": bool(accom_cnn_path.exists()),
                    "decision_tree_path": str(dt_status.get("path") or ""),
                    "decision_tree_source": str(dt_status.get("source") or ""),
                    "decision_tree_fallback_used": bool(dt_status.get("fallback_used")),
                    "decision_tree_exists": bool(dt_status.get("file_exists")),
                },
            },
        }
    )


@csrf_exempt
def decision_tree_runtime_status(request):
    if request.method != "GET":
        return JsonResponse({"error": "Method not allowed."}, status=405)

    actor = _resolve_chat_actor(request)
    user = actor.get("user")
    if not user or not getattr(user, "is_authenticated", False):
        return JsonResponse(
            {
                "fulfillmentText": "Please log in first to use this endpoint.",
                "error_code": "chat_requires_login",
            },
            status=401,
        )

    if not (getattr(user, "is_staff", False) or getattr(user, "is_superuser", False)):
        return JsonResponse(
            {
                "error": "Forbidden.",
                "error_code": "chat_admin_required",
            },
            status=403,
        )

    status_payload = get_decision_tree_runtime_status(force_reload=True)
    return JsonResponse({"status": "ok", "decision_tree_runtime": status_payload})


@csrf_exempt
def log_recommendation_click(request):
    if request.method != "POST":
        return JsonResponse({"error": "Method not allowed."}, status=405)

    user = getattr(request, "user", None)
    if not user or not getattr(user, "is_authenticated", False):
        return JsonResponse(
            {
                "fulfillmentText": "Please log in first to use this tracking endpoint.",
                "error_code": "chat_requires_login",
            },
            status=401,
        )

    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON payload."}, status=400)

    logged, item_ref = _safe_log_recommendation_click(request, payload)
    return JsonResponse({"status": "ok" if logged else "skipped", "item_ref": item_ref})


@csrf_exempt
def log_guest_funnel_event(request):
    if request.method != "POST":
        return JsonResponse({"error": "Method not allowed."}, status=405)

    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        payload = {}

    user = getattr(request, "user", None)
    if not user or not getattr(user, "is_authenticated", False):
        return JsonResponse({"status": "skipped", "reason": "not_authenticated"}, status=200)

    event_key = str((payload or {}).get("event_key") or "").strip().lower()
    config = _GUEST_FUNNEL_EVENT_MAP.get(event_key)
    if not config:
        return JsonResponse({"status": "skipped", "reason": "unknown_event_key"}, status=200)

    item_ref = str(config.get("item_ref") or "").strip()
    event_type = str(config.get("event_type") or "view").strip().lower()
    detail = str((payload or {}).get("detail") or "").strip()[:180]
    _safe_log_chat_step_event(
        request,
        event_type=event_type,
        item_ref=item_ref,
    )
    _safe_log_system_metric(
        endpoint=f"{getattr(request, 'path', '/api/chat/funnel-event/')}#event:{event_key}",
        response_time_ms=0,
        success_flag=True,
        status_code=200,
        error_message=detail,
        request=request,
    )
    if event_key == "billing_link_clicked":
        _safe_log_chat_step_event(
            request,
            event_type="click",
            item_ref="accommodation_external_handoff_clicked",
        )
    return JsonResponse({"status": "ok", "event_key": event_key, "item_ref": item_ref, "event_type": event_type})


@csrf_exempt
def accommodation_booking_notifications(request):
    if request.method != "GET":
        return JsonResponse({"error": "Method not allowed."}, status=405)

    try:
        links_url = reverse("my_accommodation_bookings")
    except NoReverseMatch:
        links_url = "/accommodations/my-bookings/"

    return JsonResponse(
        {
            "status": "disabled",
            "bookings": [],
            "reason": "accommodation_booking_transactions_decommissioned",
            "message": "Accommodation transactions are handled through each property's official page.",
            "redirect_url": links_url,
            "redirect_label": "Visit Official Accommodation Pages",
        },
        status=200,
    )


@csrf_exempt
def submit_usability_feedback(request):
    if request.method != "POST":
        return JsonResponse({"error": "Method not allowed."}, status=405)

    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON payload."}, status=400)

    user = getattr(request, "user", None)
    if not user or not getattr(user, "is_authenticated", False):
        user = None

    data_source = _resolve_data_source(request=request, payload=payload)
    instrument = str(payload.get("instrument") or "").strip().lower()
    comment = str(payload.get("comment") or "").strip()[:1000]
    batch_id = str(payload.get("survey_batch_id") or "").strip()[:64]
    if not batch_id:
        batch_id = f"survey-{uuid.uuid4().hex}"

    response_items = _normalize_survey_response_items(payload.get("responses"))
    if response_items:
        if instrument == "sus_tam_full":
            submitted_codes = {item["statement_code"] for item in response_items}
            if not _FULL_SURVEY_CODES.issubset(submitted_codes):
                missing = sorted(list(_FULL_SURVEY_CODES.difference(submitted_codes)))
                return JsonResponse(
                    {
                        "error": "Missing required survey items for sus_tam_full.",
                        "missing_codes": missing,
                    },
                    status=400,
                )
        elif instrument == "difficulty_full":
            submitted_codes = {item["statement_code"] for item in response_items}
            required = set(_DIFFICULTY_CODES)
            if not required.issubset(submitted_codes):
                missing = sorted(list(required.difference(submitted_codes)))
                return JsonResponse(
                    {
                        "error": "Missing required survey items for difficulty_full.",
                        "missing_codes": missing,
                    },
                    status=400,
                )
        elif instrument == "sus_tam_difficulty_full":
            submitted_codes = {item["statement_code"] for item in response_items}
            required = set(_FULL_SURVEY_CODES).union(_DIFFICULTY_CODES)
            if not required.issubset(submitted_codes):
                missing = sorted(list(required.difference(submitted_codes)))
                return JsonResponse(
                    {
                        "error": "Missing required survey items for sus_tam_difficulty_full.",
                        "missing_codes": missing,
                    },
                    status=400,
                )

        try:
            with transaction.atomic():
                for idx, item in enumerate(response_items):
                    row_comment = item.get("comment") or (comment if idx == 0 else "")
                    UsabilitySurveyResponse.objects.create(
                        user=user,
                        statement_code=item["statement_code"],
                        likert_score=item["likert_score"],
                        comment=row_comment,
                        survey_batch_id=batch_id,
                        data_source=data_source,
                    )
        except Exception:
            return JsonResponse({"status": "error"}, status=500)

        return JsonResponse(
            {
                "status": "ok",
                "saved_count": len(response_items),
                "survey_batch_id": batch_id,
            }
        )

    statement_code = str(payload.get("statement_code") or "CHAT_UX_QUICK").strip().upper()[:30]
    likert_score = _to_int(payload.get("likert_score"), default=0)
    if likert_score < 1 or likert_score > 5:
        return JsonResponse({"error": "likert_score must be between 1 and 5."}, status=400)

    try:
        UsabilitySurveyResponse.objects.create(
            user=user,
            statement_code=statement_code or "CHAT_UX_QUICK",
            likert_score=likert_score,
            comment=comment,
            survey_batch_id=batch_id,
            data_source=data_source,
        )
    except Exception:
        return JsonResponse({"status": "error"}, status=500)

    return JsonResponse({"status": "ok", "saved_count": 1, "survey_batch_id": batch_id})


@csrf_exempt
def text_cnn_predict(request):
    if request.method != "POST":
        return JsonResponse(
            {
                "status": "ok",
                "usage": "POST JSON: {\"message\": \"need a cheap inn near terminal\"}",
            }
        )

    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "Invalid JSON payload."}, status=400)

    message = str(payload.get("message", "")).strip()
    if not message:
        return JsonResponse({"error": "Missing 'message'."}, status=400)

    prediction, err = _predict_accommodation_class_from_text(message)
    if err:
        return JsonResponse({"error": err}, status=500)
    return JsonResponse(prediction)






