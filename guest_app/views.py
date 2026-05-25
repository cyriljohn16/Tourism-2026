from django.contrib.auth import logout, authenticate, login as auth_login
from django.contrib.auth.models import Group
from django.db import IntegrityError
from tour_app.models import Tour_Event
from .forms import GuestRegistrationForm
import calendar
from datetime import datetime, timedelta
from django.contrib import messages
from tour_app.models import Tour_Schedule, Tour_Add, Tour_Admission, Admission_Rates, Tour_Event
from django.http import JsonResponse
from django.contrib.auth.decorators import login_required
from .models import Pending, Guest, GuestCredential, DisabilityDocument, BookingCompanion  # Add BookingCompanion here
from .forms import BookingForm  # Assuming this is your form for booking
from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from guest_app.models import Pending, Guest
from tour_app.models import Tour_Schedule, Tour_Add
from .models import MapBookmark, BookmarkImage
import json
from django.http import HttpResponse
from django.views.decorators.csrf import csrf_exempt, ensure_csrf_cookie
from django.utils.decorators import method_decorator
from django.utils.http import url_has_allowed_host_and_scheme
import base64
import hashlib
import hmac
from django.core.files.base import ContentFile
from django.core.mail import send_mail, EmailMultiAlternatives
from django.template.loader import render_to_string
from django.utils.html import strip_tags
from django.conf import settings
from django.views.decorators.http import require_http_methods, require_POST
from django.views.decorators.clickjacking import xframe_options_exempt
from django.urls import reverse
from .utils import translate, get_translations_json, set_language, get_current_language, LANGUAGE_SESSION_KEY
from django.shortcuts import render, get_object_or_404, redirect
from .models import TourBooking
from .forms import ProfileUpdateForm
from django.utils import timezone
import requests  # Add this import
from django.db import models
from .models import FriendGroup, Friendship
import pytz  # Add this import
import qrcode
from io import BytesIO
import sys
from django.core.files.uploadedfile import InMemoryUploadedFile
from PIL import Image
from admin_app.models import (
    Accomodation,
    Room as AdminRoom,
    InAppNotification,
    AccommodationCertification,
    TourismInformation,
)
from admin_app.notification_service import (
    create_notification,
    notify_accommodation_owner,
    notify_admins,
    notify_assigned_employees_for_schedule,
    serialize_notification_rows,
)
from .models import AccommodationBooking, AccommodationReview
from .models import Billing
from .booking_integrity import (
    create_accommodation_booking_with_integrity,
    sync_room_current_availability,
)
from ai_chatbot.recommenders import recommend_accommodations, calculate_accommodation_billing
from ai_chatbot.recommenders import apply_approved_accommodation_scope
from ai_chatbot.models import RecommendationEvent, SystemMetricLog
from admin_app.mainpage_media import get_public_assets as get_mainpage_public_assets
from functools import wraps
from decimal import Decimal, InvalidOperation


def _safe_log_tour_event(*, request, user, event_type, item_ref):
    try:
        if not user or not getattr(user, "is_authenticated", False):
            return
        session_id = ""
        if hasattr(request, "session"):
            session_id = request.session.session_key or ""
        RecommendationEvent.objects.create(
            user=user,
            event_type=str(event_type or "view").strip().lower(),
            item_ref=str(item_ref or "").strip()[:100],
            session_id=session_id,
            data_source="real_world",
        )
    except Exception:
        pass


def _safe_log_tour_email_dispatch(*, email_type, success, error_message=""):
    try:
        SystemMetricLog.objects.create(
            module="email",
            endpoint=f"tour_email:{str(email_type or '').strip().lower()}",
            response_time_ms=0,
            success_flag=bool(success),
            status_code=200 if success else 500,
            error_message=str(error_message or "")[:300],
            data_source="real_world",
        )
    except Exception:
        pass

def _verify_recaptcha_response(request):
    """
    Verify Google reCAPTCHA token only when both site key and secret are configured.
    Returns (is_valid, error_message).
    """
    recaptcha_site_key = str(getattr(settings, "RECAPTCHA_SITE_KEY", "") or "").strip()
    recaptcha_secret = str(getattr(settings, "RECAPTCHA_SECRET_KEY", "") or "").strip()
    recaptcha_enforce_on_debug = bool(getattr(settings, "RECAPTCHA_ENFORCE_ON_DEBUG", False))
    if getattr(settings, "TESTING", False) or "test" in sys.argv:
        return True, ""
    if bool(getattr(settings, "DEBUG", False)) and not recaptcha_enforce_on_debug:
        # Local development default: do not block auth flows on reCAPTCHA.
        return True, ""
    if not recaptcha_site_key or not recaptcha_secret:
        # Safe fallback for local/dev or partial config.
        # Enforcing verification with only one key causes impossible login/signup flows.
        return True, ""

    recaptcha_response = (
        request.POST.get("g-recaptcha-response")
        or request.POST.get("g_recaptcha_response")
        or ""
    ).strip()
    if not recaptcha_response:
        return False, "Please complete the reCAPTCHA verification."

    try:
        recaptcha_result = requests.post(
            "https://www.google.com/recaptcha/api/siteverify",
            data={"secret": recaptcha_secret, "response": recaptcha_response},
            timeout=15,
        ).json()
    except Exception:
        return False, "reCAPTCHA verification is temporarily unavailable. Please try again."

    if not recaptcha_result.get("success", False):
        return False, "reCAPTCHA verification failed. Please try again."
    return True, ""


def _is_recaptcha_required():
    """
    Determine if UI should require CAPTCHA completion.
    Keep this aligned with _verify_recaptcha_response behavior.
    """
    recaptcha_site_key = str(getattr(settings, "RECAPTCHA_SITE_KEY", "") or "").strip()
    recaptcha_secret = str(getattr(settings, "RECAPTCHA_SECRET_KEY", "") or "").strip()
    recaptcha_enforce_on_debug = bool(getattr(settings, "RECAPTCHA_ENFORCE_ON_DEBUG", False))

    if bool(getattr(settings, "TESTING", False)) or "test" in sys.argv:
        return False
    if bool(getattr(settings, "DEBUG", False)) and not recaptcha_enforce_on_debug:
        return False
    if not recaptcha_site_key or not recaptcha_secret:
        return False
    return True


def _resolve_treasurer_billing_url():
    return str(
        getattr(settings, "TOURISM_TREASURER_BILLING_URL", "")
        or getattr(settings, "TOURISM_OFFICE_BILLING_URL", "")
        or "https://bayawancity.gov.ph/"
        or ""
    ).strip()


def _resolve_accommodation_outbound_link(accommodation):
    if accommodation is None:
        return ""
    links = _resolve_accommodation_links(accommodation)
    for key in ("facebook_url", "provider_url", "email_link", "phone_link"):
        value = str(links.get(key) or "").strip()
        if value:
            return value
    return ""


def _is_accommodation_facebook_url(url):
    text = str(url or "").strip().lower()
    return "facebook.com" in text or "fb.com" in text


def _is_third_party_accommodation_booking_url(url):
    text = str(url or "").strip().lower()
    if not text:
        return False
    third_party_hosts = (
        "booking.com",
        "agoda.",
        "traveloka.",
        "expedia.",
        "hotels.com",
        "tripadvisor.",
        "airbnb.",
        "trivago.",
        "kayak.",
    )
    return any(host in text for host in third_party_hosts)


def _is_verified_accommodation_provider_url(url):
    text = str(url or "").strip()
    if not text:
        return False
    if _is_third_party_accommodation_booking_url(text):
        return False
    return text.lower().startswith(("http://", "https://"))


def _resolve_accommodation_links(accommodation):
    booking_url = str(getattr(accommodation, "official_booking_url", "") or "").strip() if accommodation else ""
    contact_url = str(getattr(accommodation, "official_contact_url", "") or "").strip() if accommodation else ""
    email_value = str(getattr(accommodation, "email_address", "") or "").strip() if accommodation else ""
    phone_value = str(getattr(accommodation, "phone_number", "") or "").strip() if accommodation else ""

    facebook_url = ""
    for candidate in (booking_url, contact_url):
        if candidate and _is_accommodation_facebook_url(candidate):
            facebook_url = candidate
            break

    provider_url = ""
    # Prefer contact/provider channels over booking URLs. Third-party booking
    # platforms are intentionally suppressed from guest-facing handoff.
    for candidate in (contact_url, booking_url):
        if candidate and not _is_accommodation_facebook_url(candidate) and _is_verified_accommodation_provider_url(candidate):
            provider_url = candidate
            break

    has_third_party_booking_url = _is_third_party_accommodation_booking_url(booking_url) or _is_third_party_accommodation_booking_url(contact_url)

    return {
        "official_url": provider_url,
        "provider_url": provider_url,
        "facebook_url": facebook_url,
        "phone_link": f"tel:{phone_value}" if phone_value else "",
        "email_link": f"mailto:{email_value}" if email_value else "",
        "has_verified_provider_link": bool(facebook_url or provider_url or email_value or phone_value),
        "suppressed_third_party_booking_url": has_third_party_booking_url,
    }


def _resolve_accommodation_image_url(accommodation):
    if accommodation is None:
        return ""
    profile_picture = getattr(accommodation, "profile_picture", None)
    if not profile_picture:
        return ""
    try:
        return str(profile_picture.url or "").strip()
    except Exception:
        return ""


def _resolve_room_image_url(room, *, fallback_url=""):
    if room is None:
        return str(fallback_url or "").strip()
    for field_name in ("image", "room_image", "profile_picture", "photo", "cover_image"):
        candidate = getattr(room, field_name, None)
        if not candidate:
            continue
        try:
            url = str(getattr(candidate, "url", "") or "").strip()
        except Exception:
            url = ""
        if url:
            return url
    return str(fallback_url or "").strip()


def _build_homepage_accommodation_cards(limit=6):
    cards = []
    max_items = max(1, int(limit))
    accommodations = list(_approved_accommodation_queryset().order_by("company_name")[:max_items])
    if len(accommodations) > 2:
        # Rotate the featured stays daily so the mobile top-two cards are not permanently static.
        rotation = timezone.localdate().toordinal() % len(accommodations)
        accommodations = accommodations[rotation:] + accommodations[:rotation]
    review_summaries = _get_accommodation_review_summaries(
        [getattr(accom, "accom_id", None) for accom in accommodations]
    )
    for accom in accommodations:
        links = _resolve_accommodation_links(accom)
        room = (
            AdminRoom.objects.filter(accommodation=accom, status="AVAILABLE")
            .order_by("price_per_night", "room_id")
            .first()
        )
        price_cue = ""
        if room is not None and getattr(room, "price_per_night", None) not in (None, ""):
            try:
                price_cue = f"From PHP {Decimal(str(room.price_per_night)):.0f} / night"
            except Exception:
                price_cue = ""
        rating_label = _format_accommodation_rating_label(
            review_summaries.get(getattr(accom, "accom_id", None))
        )
        cards.append(
            {
                "accommodation": accom,
                "image_url": _resolve_accommodation_image_url(accom),
                "official_link": links.get("facebook_url") or links.get("provider_url") or links.get("email_link") or links.get("phone_link") or "",
                "official_page_url": links.get("provider_url") or "",
                "provider_url": links.get("provider_url") or "",
                "contact_channel_url": links.get("provider_url") or links.get("email_link") or links.get("phone_link") or "",
                "facebook_url": links.get("facebook_url") or "",
                "has_verified_provider_link": links.get("has_verified_provider_link", False),
                "suppressed_third_party_booking_url": links.get("suppressed_third_party_booking_url", False),
                "price_cue": price_cue,
                "capacity_cue": (
                    f"Up to {int(getattr(room, 'person_limit', 0))} guests"
                    if room is not None and getattr(room, "person_limit", None) not in (None, "")
                    else ""
                ),
                "rating_label": rating_label,
                "room_name": str(getattr(room, "room_name", "") or "").strip() if room else "",
            }
        )
    return cards


def _accommodation_transaction_disabled_payload():
    return {
        "success": False,
        "message": (
            "Accommodation transactions are no longer processed in this system. "
            "Please use the official accommodation pages shown in listings."
        ),
        "code": "accommodation_transaction_disabled",
    }


def _approved_accommodation_queryset():
    base_qs = Accomodation.objects.all()
    return apply_approved_accommodation_scope(base_qs, accommodation_path="")


def _get_accommodation_review_summaries(accommodation_ids):
    ids = [value for value in accommodation_ids if value not in (None, "")]
    if not ids:
        return {}
    rows = (
        AccommodationReview.objects.filter(
            accommodation_id__in=ids,
            status="approved",
        )
        .values("accommodation_id")
        .annotate(average=models.Avg("rating"), count=models.Count("review_id"))
    )
    return {
        row["accommodation_id"]: {
            "average": row.get("average"),
            "count": int(row.get("count") or 0),
        }
        for row in rows
    }


def _format_accommodation_rating_label(summary):
    count = int((summary or {}).get("count") or 0)
    if count <= 0:
        return "Not yet rated"
    average = summary.get("average") or 0
    try:
        average_label = f"{float(average):.1f}"
    except Exception:
        average_label = "0.0"
    suffix = "review" if count == 1 else "reviews"
    return f"★ {average_label} / 5.0 ({count} {suffix})"


def _homepage_tourist_map_url():
    images_dir = settings.BASE_DIR / "static" / "images"
    candidates = (
        ("bayawan_tourist_map_official.png", "/static/images/bayawan_tourist_map_official.png"),
        ("bayawan_map.jpg", "/static/images/bayawan_map.jpg"),
        ("bayawan_map.png", "/static/images/bayawan_map.png"),
    )
    for filename, url in candidates:
        try:
            if (images_dir / filename).exists():
                return url
        except Exception:
            continue
    return "/static/images/bayawan_map.jpg"


def _tourism_map_category_label(category):
    key = str(category or "").strip().lower()
    return {
        "restaurant": "Dining Places",
        "hotel": "Approved Stays",
        "landmark": "Tourist Spots / Landmarks",
        "public": "Public Tourism Facilities",
        "shopping": "Shopping / Local Products",
        "custom": "Other Tourism Places",
    }.get(key, "Other Tourism Places")


def _normalize_tourism_place_name(value):
    text = str(value or "").strip().lower()
    return "".join(ch for ch in text if ch.isalnum())


def _tourism_category_metadata():
    return [
        {"key": "landmark", "label": "Tourist Spots / Landmarks", "description": "Published destinations, landmarks, and attraction markers.", "map_query": "tourist spots"},
        {"key": "restaurant", "label": "Dining Places", "description": "Mapped restaurants, cafes, and food-related stops.", "map_query": "dining places"},
        {"key": "public", "label": "Public Facilities", "description": "Tourism-related public places and visitor services.", "map_query": "public facilities"},
        {"key": "shopping", "label": "Shopping / Local Products", "description": "Markets, shops, souvenirs, and local product stops.", "map_query": "shopping local products"},
        {"key": "hotel", "label": "Approved Stays", "description": "Tourism Office-approved accommodations promoted for visitor planning.", "map_query": "approved stays"},
        {"key": "custom", "label": "Other Tourism Places", "description": "Additional mapped places approved for public viewing.", "map_query": "tourism places"},
    ]


def _tourism_map_category_summary():
    try:
        rows = (
            MapBookmark.objects.filter(user__isnull=True)
            .values("category")
            .annotate(total=models.Count("id"))
        )
        counts = {str(row.get("category") or "custom").strip().lower(): int(row.get("total") or 0) for row in rows}
    except Exception:
        counts = {}

    summary = []
    for meta in _tourism_category_metadata():
        key = meta["key"]
        total = counts.get(key, 0)
        if key == "landmark":
            try:
                total += TourismInformation.objects.published().count()
            except Exception:
                pass
        if total:
            summary.append(
                {
                    "label": meta["label"],
                    "count": total,
                    "description": meta["description"],
                    "map_query": meta["label"],
                }
            )

    if not summary:
        summary = [
            {"label": "Tourist Spots / Landmarks", "count": 0, "description": "Tourism place records will appear here soon.", "map_query": "tourist spots"},
            {"label": "Dining Places", "count": 0, "description": "Dining records will appear here soon.", "map_query": "dining places"},
            {"label": "Approved Stays", "count": 0, "description": "Approved stay markers will appear here soon.", "map_query": "approved stays"},
            {"label": "Public Facilities", "count": 0, "description": "Public facility records will appear here soon.", "map_query": "public facilities"},
        ]
    return summary[:6]


def _external_direction_url(lat, lng):
    try:
        lat_value = float(lat)
        lng_value = float(lng)
    except (TypeError, ValueError):
        return ""
    return f"https://www.google.com/maps/search/?api=1&query={lat_value},{lng_value}"


def _build_homepage_tourism_place_cards(limit=8):
    cards = []

    try:
        tourism_rows = TourismInformation.objects.published().order_by("spot_name")[:limit]
    except Exception:
        tourism_rows = []

    for info in tourism_rows:
        image_url = ""
        image_field = getattr(info, "image", None)
        if image_field:
            try:
                image_url = image_field.url
            except Exception:
                image_url = ""
        name = str(getattr(info, "spot_name", "") or "").strip()
        cards.append(
            {
                "name": name,
                "category": "Tourism Information",
                "badge": "Tourism Office Published",
                "description": str(getattr(info, "description", "") or "").strip(),
                "location": str(getattr(info, "location", "") or "").strip(),
                "image_url": image_url,
                "map_query": name,
                "directions_url": "",
            }
        )
        if len(cards) >= limit:
            return cards

    remaining = max(0, int(limit or 8) - len(cards))
    if remaining <= 0:
        return cards

    try:
        marker_rows = MapBookmark.objects.filter(user__isnull=True).order_by("category", "name")[: remaining + 8]
    except Exception:
        marker_rows = []

    seen_names = {str(card.get("name") or "").strip().lower() for card in cards if card.get("name")}
    for marker in marker_rows:
        name = str(getattr(marker, "name", "") or "").strip()
        if not name or name.lower() in seen_names:
            continue
        image_url = ""
        image_field = getattr(marker, "primary_image", None)
        if image_field:
            try:
                image_url = image_field.url
            except Exception:
                image_url = ""
        cards.append(
            {
                "name": name,
                "category": _tourism_map_category_label(getattr(marker, "category", "")),
                "badge": "Tourism Office Mapped",
                "description": str(getattr(marker, "details", "") or "").strip(),
                "location": "",
                "image_url": image_url,
                "map_query": name,
                "directions_url": _external_direction_url(
                    getattr(marker, "latitude", None),
                    getattr(marker, "longitude", None),
                ),
            }
        )
        seen_names.add(name.lower())
        if len(cards) >= limit:
            break
    return cards


def _build_homepage_tourism_place_groups(limit_per_group=6):
    groups = []
    group_lookup = {}
    for meta in _tourism_category_metadata():
        group = {
            "key": meta["key"],
            "label": meta["label"],
            "description": meta["description"],
            "map_query": meta["map_query"],
            "places": [],
            "count": 0,
        }
        groups.append(group)
        group_lookup[meta["key"]] = group

    seen_names_by_group = {group["key"]: set() for group in groups}

    try:
        tourism_rows = TourismInformation.objects.published().order_by("spot_name")
    except Exception:
        tourism_rows = []

    for info in tourism_rows:
        name = str(getattr(info, "spot_name", "") or "").strip()
        normalized_name = _normalize_tourism_place_name(name)
        if not name or normalized_name in seen_names_by_group["landmark"]:
            continue
        image_url = ""
        image_field = getattr(info, "image", None)
        if image_field:
            try:
                image_url = image_field.url
            except Exception:
                image_url = ""
        group_lookup["landmark"]["places"].append(
            {
                "name": name,
                "category": group_lookup["landmark"]["label"],
                "badge": "Tourism Office Published",
                "description": str(getattr(info, "description", "") or "").strip(),
                "location": str(getattr(info, "location", "") or "").strip(),
                "image_url": image_url,
                "map_query": name,
                "directions_url": "",
                "has_map_marker": False,
            }
        )
        seen_names_by_group["landmark"].add(normalized_name)

    try:
        marker_rows = list(MapBookmark.objects.filter(user__isnull=True).order_by("category", "name"))
    except Exception:
        marker_rows = []

    marker_lookup = {}
    for marker in marker_rows:
        marker_name = str(getattr(marker, "name", "") or "").strip()
        if not marker_name:
            continue
        normalized_marker_name = _normalize_tourism_place_name(marker_name)
        if normalized_marker_name and normalized_marker_name not in marker_lookup:
            marker_lookup[normalized_marker_name] = marker

    try:
        approved_stays = list(_approved_accommodation_queryset().order_by("company_name"))
    except Exception:
        approved_stays = []

    for accom in approved_stays:
        name = str(getattr(accom, "company_name", "") or "").strip()
        normalized_name = _normalize_tourism_place_name(name)
        if not name or normalized_name in seen_names_by_group["hotel"]:
            continue
        matched_marker = marker_lookup.get(normalized_name)
        links = _resolve_accommodation_links(accom)
        facebook_url = str(links.get("facebook_url") or "").strip()
        provider_url = str(links.get("provider_url") or "").strip()
        contact_url = facebook_url or provider_url or str(links.get("email_link") or "").strip() or str(links.get("phone_link") or "").strip()
        contact_label = ""
        if facebook_url:
            contact_label = "Open Official Facebook Page"
        elif contact_url:
            contact_label = "Contact Accommodation Provider"
        group_lookup["hotel"]["places"].append(
            {
                "name": name,
                "category": group_lookup["hotel"]["label"],
                "badge": "Tourism Office Approved",
                "description": (
                    str(getattr(accom, "description", "") or "").strip()
                    or "Preview-only accommodation information. Final arrangements continue through the establishment's verified provider channel."
                ),
                "location": str(getattr(accom, "location", "") or "").strip(),
                "image_url": _resolve_accommodation_image_url(accom),
                "map_query": name,
                "directions_url": (
                    _external_direction_url(
                        getattr(matched_marker, "latitude", None),
                        getattr(matched_marker, "longitude", None),
                    )
                    if matched_marker is not None
                    else ""
                ),
                "has_map_marker": matched_marker is not None,
                "detail_url": reverse("accommodation_detail_page", kwargs={"accom_id": getattr(accom, "accom_id", None)}),
                "contact_url": contact_url,
                "contact_label": contact_label,
            }
        )
        seen_names_by_group["hotel"].add(normalized_name)

    for marker in marker_rows:
        name = str(getattr(marker, "name", "") or "").strip()
        if not name:
            continue
        raw_category = str(getattr(marker, "category", "") or "custom").strip().lower() or "custom"
        key = raw_category if raw_category in group_lookup else "custom"
        normalized_name = _normalize_tourism_place_name(name)
        if key == "hotel":
            # Approved Stays are sourced from accepted accommodations; hotel
            # markers only enrich matching stays with map/direction data.
            continue
        if normalized_name in seen_names_by_group[key]:
            continue
        image_url = ""
        image_field = getattr(marker, "primary_image", None)
        if image_field:
            try:
                image_url = image_field.url
            except Exception:
                image_url = ""
        group_lookup[key]["places"].append(
            {
                "name": name,
                "category": _tourism_map_category_label(raw_category),
                "badge": "Tourism Office Mapped",
                "description": str(getattr(marker, "details", "") or "").strip(),
                "location": "Bayawan City",
                "image_url": image_url,
                "map_query": name,
                "directions_url": _external_direction_url(
                    getattr(marker, "latitude", None),
                    getattr(marker, "longitude", None),
                ),
                "has_map_marker": True,
            }
        )
        seen_names_by_group[key].add(normalized_name)

    safe_limit = max(1, int(limit_per_group or 6))
    for group in groups:
        group["count"] = len(group["places"])
        if group["key"] != "hotel":
            group["places"] = group["places"][:safe_limit]
    return groups


@ensure_csrf_cookie
def main_page(request):
    """Main page view with language support"""
    # Keep accommodation owners in admin-side owner workflow.
    if request.user.is_authenticated:
        role_value = str(getattr(request.user, "role", "") or "").strip().lower()
        owner_group_names = {
            "accommodation_owner",
            "accommodation_owner_pending",
        }
        owner_like_account = (
            role_value in {"accommodation_owner", "accommodation owner", "owner"}
            or request.user.groups.filter(name__in=owner_group_names).exists()
        )
        owner_side_session = (
            str(request.session.get("user_type") or "").strip().lower()
            in {"accomodation", "accommodation", "establishment"}
        )

        if owner_side_session:
            return redirect("admin_app:owner_hub")

        if owner_like_account:
            has_accepted_linked_accommodation = Accomodation.objects.filter(
                owner=request.user,
                approval_status="accepted",
            ).exists()
            if has_accepted_linked_accommodation:
                return redirect("admin_app:owner_hub")

            messages.info(
                request,
                "Owner account detected, but no accepted accommodation is linked yet. "
                "Please use Admin Panel login and complete/confirm your accommodation approval.",
            )

    # Helper function to ensure datetime objects are properly converted
    def ensure_timezone_aware(dt):
        if dt is None:
            return None
        if isinstance(dt, str):
            try:
                dt = datetime.fromisoformat(dt.replace('Z', '+00:00'))
            except ValueError:
                try:
                    dt = datetime.strptime(dt, "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    return None
        if not timezone.is_aware(dt):
            dt = timezone.make_aware(dt)
        return dt
    
    # Get the current language preference
    current_language = get_current_language(request)
    
    # Get only published tours with non-expired schedules for guest-facing discovery.
    now = timezone.now()
    active_tour_ids = Tour_Schedule.objects.filter(end_time__gte=now).exclude(
        status='cancelled'
    ).values_list('tour_id', flat=True).distinct()
    tours = Tour_Add.objects.filter(
        publication_status="published",
        tour_id__in=active_tour_ids,
    )
    
    # For each tour, translate translatable fields and calculate min/max duration
    translated_tours = []
    for tour in tours:
        # Calculate min and max duration days for each tour's schedules
        schedules = Tour_Schedule.objects.filter(
            tour_id=tour.tour_id,
            end_time__gte=now,
        ).exclude(status='cancelled')
        min_duration = None
        max_duration = None
        
        if schedules.exists():
            durations = [s.duration_days for s in schedules if s.duration_days]
            if durations:
                min_duration = min(durations)
                max_duration = max(durations)
        
        # Attach duration info to the tour object for easy access in template
        tour.min_duration = min_duration
        tour.max_duration = max_duration
        tour.has_duration_range = schedules.count() > 1 and min_duration != max_duration
        
        # Append tour and its translatable fields as context 
        # (assuming Tour_Add has translatable fields like name_tl, description_tl, etc.)
        tour_data = {
            'tour': tour,
            'translatable': {
                'tour_name': getattr(tour, f'tour_name_{current_language}', tour.tour_name),
                'description': getattr(tour, f'description_{current_language}', tour.description),
                # Add other translatable fields as needed
            }
        }
        translated_tours.append(tour_data)
    
    # Get user bookings if authenticated
    upcoming_tours = []
    current_tours = []
    past_tours = []
    
    if request.user.is_authenticated:
        # Get current time for comparison - remove any time zone issues by using UTC
        now = timezone.now()
        print(f"Current server time (UTC): {now}")
        
        # Reset the lists to ensure they're empty
        upcoming_tours = []
        current_tours = []
        past_tours = []
        
        # First, try to get TourBooking records
        tour_bookings = TourBooking.objects.filter(
            guest=request.user
        ).select_related('tour', 'schedule').order_by('schedule__start_time')
        
        print(f"Found {len(tour_bookings)} TourBooking records")
        
        # Also check for Pending bookings
        pending_bookings = Pending.objects.filter(
            guest_id=request.user
        ).select_related('tour_id', 'sched_id').order_by('sched_id__start_time')
        
        print(f"Found {len(pending_bookings)} Pending records")
        
        print(f"Total bookings to process: {len(tour_bookings) + len(pending_bookings)}")
        
        # Process all bookings and categorize them based ONLY on time, not status
        
        # Process TourBooking records first
        for booking in tour_bookings:
            try:
                # Only skip if explicitly cancelled
                if booking.status == 'cancelled':
                    print(f"TourBooking {booking.booking_id} is cancelled, adding to past_tours")
                    past_tours.append(booking)
                    continue
                
                # Get schedule times directly
                start_time = booking.schedule.start_time
                end_time = booking.schedule.end_time
                
                # Ensure timezone awareness for proper comparison
                if not timezone.is_aware(start_time):
                    start_time = timezone.make_aware(start_time)
                if not timezone.is_aware(end_time):
                    end_time = timezone.make_aware(end_time)
                
                # Debug output
                print(f"TourBooking {booking.booking_id}: {start_time} to {end_time}, now is {now}")
                
                # Simple date comparison
                if start_time > now:
                    print(f"TourBooking {booking.booking_id} is UPCOMING")
                    # Update status to 'pending' if it's not already set
                    if booking.status not in ['pending', 'cancelled', 'active', 'completed']:
                        booking.status = 'pending'
                        booking.save()
                    upcoming_tours.append(booking)
                elif start_time <= now and end_time >= now:
                    print(f"TourBooking {booking.booking_id} is CURRENT")
                    # Update status to 'active' if it's not already set
                    if booking.status not in ['active', 'cancelled', 'completed']:
                        booking.status = 'active'
                        booking.save()
                    current_tours.append(booking)
                else:
                    print(f"TourBooking {booking.booking_id} is PAST")
                    # Update status to 'completed' if it's not already set
                    if booking.status not in ['completed', 'cancelled']:
                        booking.status = 'completed'
                        booking.save()
                    past_tours.append(booking)
            except Exception as e:
                print(f"Error categorizing TourBooking {booking.booking_id}: {str(e)}")
                past_tours.append(booking)  # Default to past if error occurs
        
        # Now process Pending bookings
        for booking in pending_bookings:
            try:
                # Only skip if explicitly cancelled
                if booking.status.lower() == 'cancelled':
                    print(f"Pending {booking.id} has status Cancelled, adding to past_tours")
                    past_tours.append(booking)
                    continue
                
                # Get schedule times directly
                start_time = booking.sched_id.start_time
                end_time = booking.sched_id.end_time
                
                # Ensure timezone awareness for proper comparison
                if not timezone.is_aware(start_time):
                    start_time = timezone.make_aware(start_time)
                if not timezone.is_aware(end_time):
                    end_time = timezone.make_aware(end_time)
                
                # Debug output
                print(f"Pending {booking.id}: {start_time} to {end_time}, now is {now}")
                
                # Simple date comparison - ignore the 'Pending' status and use only timing
                if start_time > now:
                    print(f"Pending {booking.id} is UPCOMING")
                    # Keep as Pending (no status change needed)
                    upcoming_tours.append(booking)
                elif start_time <= now and end_time >= now:
                    print(f"Pending {booking.id} is CURRENT")
                    # Update to Active if it's not already
                    if booking.status.lower() == 'pending':
                        booking.status = 'Active'
                        booking.save()
                    current_tours.append(booking)
                else:
                    print(f"Pending {booking.id} is PAST")
                    # Update to Completed if not cancelled
                    if booking.status.lower() == 'pending':
                        booking.status = 'Completed'
                        booking.save()
                    past_tours.append(booking)
            except Exception as e:
                print(f"Error categorizing Pending {booking.id}: {str(e)}")
                past_tours.append(booking)  # Default to past if error occurs
        
        print(f"Final categorization: {len(upcoming_tours)} upcoming, {len(current_tours)} current, {len(past_tours)} past")
    
    is_accommodation_owner_user = False
    if request.user.is_authenticated:
        try:
            if request.user.groups.filter(name__iexact="accommodation_owner").exists():
                is_accommodation_owner_user = True
            else:
                role_value = str(getattr(request.user, "role", "") or "").strip().lower()
                if role_value in {"accommodation_owner", "accommodation owner", "owner"}:
                    is_accommodation_owner_user = True
                else:
                    session_user_type = str(request.session.get("user_type") or "").strip().lower()
                    if session_user_type in {"accomodation", "accommodation", "establishment"}:
                        is_accommodation_owner_user = True
                    elif hasattr(request.user, "owned_accommodations"):
                        is_accommodation_owner_user = request.user.owned_accommodations.exists()
        except Exception:
            is_accommodation_owner_user = False

    mainpage_assets = get_mainpage_public_assets()
    approved_accommodation_cards = _build_homepage_accommodation_cards(limit=6)
    official_tourist_map_url = _homepage_tourist_map_url()
    tourism_place_cards = _build_homepage_tourism_place_cards(limit=8)
    tourism_place_groups = _build_homepage_tourism_place_groups(limit_per_group=6)
    tourism_category_summary = [
        {
            "label": group["label"],
            "count": group["count"],
            "description": group["description"],
            "map_query": group["map_query"],
        }
        for group in tourism_place_groups
    ]

    context = {
        'tours': tours,  # Keep the original queryset for Django template usage
        'translated_tours': translated_tours,  # Add translated data
        'user': request.user,
        'upcoming_tours': upcoming_tours,
        'current_tours': current_tours,
        'past_tours': past_tours,
        'current_language': current_language,
        'translations_json': get_translations_json(current_language),  # Add translations for JavaScript
        'is_accommodation_owner_user': is_accommodation_owner_user,
        'recaptcha_site_key': str(getattr(settings, 'RECAPTCHA_SITE_KEY', '') or '').strip(),
        'recaptcha_configured': bool(
            str(getattr(settings, 'RECAPTCHA_SITE_KEY', '') or '').strip()
            and str(getattr(settings, 'RECAPTCHA_SECRET_KEY', '') or '').strip()
        ),
        'active_logo_url': mainpage_assets.get('active_logo_url') or '',
        'hero_backgrounds': mainpage_assets.get('hero_urls') or [],
        'approved_accommodation_cards': approved_accommodation_cards,
        'official_tourist_map_url': official_tourist_map_url,
        'tourism_place_cards': tourism_place_cards,
        'tourism_place_groups': tourism_place_groups,
        'tourism_category_summary': tourism_category_summary,
    }
    
    return render(request, 'mainpage.html', context)


def user_is_allowed(user):
    # Implement your custom logic to check if the user is allowed
    # Example: Check if the user is authenticated or has specific permissions
    return user.is_authenticated  # or any other condition you need


def is_guest_tourist_user(user, request=None):
    """
    Compatibility-safe guest/tourist role check.
    This project uses Guest as AUTH_USER_MODEL, while owner/admin roles may be
    represented via groups, role-like attributes, or session flags.
    """
    if not user or not getattr(user, "is_authenticated", False):
        return False

    if getattr(user, "is_superuser", False):
        return False

    role_value = str(getattr(user, "role", "") or "").strip().lower()
    disallowed_role_values = {
        "admin",
        "employee",
        "accommodation_owner",
        "accommodation owner",
        "owner",
        "establishment",
    }
    if role_value in disallowed_role_values:
        return False

    try:
        blocked_group_names = {
            "accommodation_owner",
            "accommodation_owner_pending",
        }
        user_groups = {str(name).strip().lower() for name in user.groups.values_list("name", flat=True)}
        if user_groups.intersection(blocked_group_names):
            return False
    except Exception:
        pass

    return True


def guest_tourist_required(view_func):
    @wraps(view_func)
    def wrapped_view(request, *args, **kwargs):
        if is_guest_tourist_user(request.user, request=request):
            return view_func(request, *args, **kwargs)

        message = "Only guest/tourist accounts can use this booking endpoint."
        expects_json = (
            request.headers.get("X-Requested-With") == "XMLHttpRequest"
            or "application/json" in str(request.headers.get("Accept", "")).lower()
            or "application/json" in str(getattr(request, "content_type", "")).lower()
            or request.path.endswith(("/recommend/", "/billing/", "/book/"))
        )
        if expects_json:
            return JsonResponse({"success": False, "message": message}, status=403)
        return HttpResponse(message, status=403)

    return wrapped_view


@login_required
@require_http_methods(["GET", "POST"])
def guest_notifications(request):
    """
    Lightweight guest notification feed for navbar dropdown.
    Uses existing booking/accommodation/tour records (no schema changes).
    """
    if not is_guest_tourist_user(request.user, request=request):
        return JsonResponse({"success": False, "message": "Guest access required."}, status=403)

    now = timezone.now()
    cookie_key = "guest_notifications_state_v1"
    seen_key = "guest_notifications_seen_at"
    seen_ids_key = "guest_notifications_seen_ids"
    first_seen_key = "guest_notifications_first_seen_map"

    def _parse_seen_at(raw_value):
        raw = str(raw_value or "").strip()
        if not raw:
            return None
        try:
            parsed = datetime.fromisoformat(raw)
            return timezone.make_aware(parsed) if timezone.is_naive(parsed) else parsed
        except Exception:
            return None

    def _read_cookie_state():
        raw = str(request.COOKIES.get(cookie_key) or "").strip()
        if not raw:
            return {"seen_at": "", "seen_ids": []}
        try:
            payload = json.loads(raw)
        except Exception:
            return {"seen_at": "", "seen_ids": []}
        if not isinstance(payload, dict):
            return {"seen_at": "", "seen_ids": []}
        if str(payload.get("uid") or "") != str(getattr(request.user, "pk", "") or ""):
            return {"seen_at": "", "seen_ids": []}
        seen_at_raw = str(payload.get("seen_at") or "").strip()
        seen_ids_raw = payload.get("seen_ids") if isinstance(payload.get("seen_ids"), list) else []
        seen_ids_clean = [str(v).strip() for v in seen_ids_raw if str(v).strip()]
        return {"seen_at": seen_at_raw, "seen_ids": seen_ids_clean}

    def _write_cookie_state(response, *, seen_at_raw, seen_ids):
        safe_ids = [str(v).strip() for v in (seen_ids or []) if str(v).strip()][:120]
        payload = {
            "uid": str(getattr(request.user, "pk", "") or ""),
            "seen_at": str(seen_at_raw or "").strip(),
            "seen_ids": safe_ids,
        }
        response.set_cookie(
            cookie_key,
            json.dumps(payload, separators=(",", ":")),
            max_age=60 * 60 * 24 * 180,
            httponly=True,
            samesite="Lax",
        )
        return response

    seen_ids = request.session.get(seen_ids_key) or []
    if not isinstance(seen_ids, list):
        seen_ids = []
    cookie_state = _read_cookie_state()
    seen_ids_set = {str(v) for v in seen_ids if str(v).strip()}
    seen_ids_set.update({str(v) for v in cookie_state.get("seen_ids", []) if str(v).strip()})
    first_seen_raw = request.session.get(first_seen_key) or {}
    if not isinstance(first_seen_raw, dict):
        first_seen_raw = {}
    first_seen_map = {
        str(k).strip(): str(v).strip()
        for k, v in first_seen_raw.items()
        if str(k).strip() and str(v).strip()
    }

    if request.method == "POST":
        action = ""
        notif_id = ""
        notif_ids = []
        try:
            payload = json.loads(request.body.decode("utf-8") or "{}")
            action = str(payload.get("action") or "").strip().lower()
            notif_id = str(payload.get("notification_id") or "").strip()
            if isinstance(payload.get("notification_ids"), list):
                notif_ids = [
                    str(v).strip()
                    for v in payload.get("notification_ids")
                    if str(v).strip()
                ]
        except Exception:
            action = ""
            notif_id = ""
            notif_ids = []

        def _to_db_notif_id(raw_value):
            raw = str(raw_value or "").strip()
            if not raw:
                return None
            if raw.lower().startswith("db-"):
                raw = raw[3:]
            try:
                return int(raw)
            except Exception:
                return None

        db_single_id = _to_db_notif_id(notif_id)
        db_bulk_ids = [_to_db_notif_id(v) for v in notif_ids]
        db_bulk_ids = [v for v in db_bulk_ids if v is not None]

        if action == "mark_all_read":
            # Mark persisted in-app notifications as read.
            ids_to_mark = list(db_bulk_ids)
            if db_single_id is not None:
                ids_to_mark.append(db_single_id)
            if ids_to_mark:
                InAppNotification.objects.filter(
                    recipient_guest=request.user,
                    id__in=list(set(ids_to_mark)),
                ).update(is_read=True)

            # Also mark generated (non-db) notifications as seen in guest session/cookie state.
            if notif_ids:
                seen_ids_set.update(notif_ids)
            request.session[seen_key] = now.isoformat()
            request.session[seen_ids_key] = list(seen_ids_set)[-400:]
            request.session.modified = True
            response = JsonResponse(
                {
                    "success": True,
                    "message": "Notifications marked as read.",
                    "unread_count": 0,
                }
            )
            return _write_cookie_state(
                response,
                seen_at_raw=now.isoformat(),
                seen_ids=list(seen_ids_set),
            )

        if db_single_id is not None:
            updated = InAppNotification.objects.filter(
                id=db_single_id,
                recipient_guest=request.user,
            ).update(is_read=True)
            if updated:
                unread_count = InAppNotification.objects.filter(
                    recipient_guest=request.user,
                    is_read=False,
                ).count()
                return JsonResponse({"success": True, "message": "Notification marked as read.", "unread_count": unread_count})

        if db_bulk_ids:
            InAppNotification.objects.filter(
                recipient_guest=request.user,
                id__in=db_bulk_ids,
            ).update(is_read=True)
            unread_count = InAppNotification.objects.filter(
                recipient_guest=request.user,
                is_read=False,
            ).count()
            return JsonResponse({"success": True, "message": "Notifications marked as read.", "unread_count": unread_count})

        if notif_id:
            seen_ids_set.add(notif_id)
            request.session[seen_ids_key] = list(seen_ids_set)[-400:]
            request.session.modified = True
            seen_raw = str(request.session.get(seen_key) or "").strip()
            response = JsonResponse({"success": True, "message": "Notification marked as read."})
            return _write_cookie_state(response, seen_at_raw=seen_raw, seen_ids=list(seen_ids_set))

        request.session[seen_key] = now.isoformat()
        if notif_ids:
            seen_ids_set.update(notif_ids)
        request.session[seen_ids_key] = list(seen_ids_set)[-400:]
        request.session.modified = True
        response = JsonResponse({"success": True, "message": "Notifications marked as read.", "unread_count": 0})
        return _write_cookie_state(response, seen_at_raw=now.isoformat(), seen_ids=list(seen_ids_set))

    seen_at = None
    seen_raw = str(request.session.get(seen_key) or "").strip()
    if seen_raw:
        seen_at = _parse_seen_at(seen_raw)
    cookie_seen_at = _parse_seen_at(cookie_state.get("seen_at"))
    if cookie_seen_at is not None and (seen_at is None or cookie_seen_at > seen_at):
        seen_at = cookie_seen_at

    notifications = []
    guest_user = request.user
    recent_cutoff = now - timedelta(days=30)

    # Accommodation booking updates
    accommodation_updates = (
        AccommodationBooking.objects.filter(guest=guest_user)
        .select_related("accommodation", "room")
        .order_by("-last_updated")[:20]
    )
    status_title = {
        "pending": "Accommodation booking pending",
        "confirmed": "Accommodation booking confirmed",
        "declined": "Accommodation booking declined",
        "cancelled": "Accommodation booking cancelled",
    }
    for booking in accommodation_updates:
        updated_at = booking.last_updated or booking.booking_date
        notif_status = str(booking.status or "").strip().lower()
        title = status_title.get(notif_status, "Accommodation booking update")
        room_name = booking.room.room_name if booking.room else "Selected room"
        message = (
            f"{booking.accommodation.company_name} - {room_name} | "
            f"{booking.check_in} to {booking.check_out} | Status: {str(booking.status).title()}."
        )
        notifications.append(
            {
                "id": f"accom-{booking.booking_id}-{notif_status}",
                "title": title,
                "message": message,
                "type": "booking",
                "status": notif_status,
                "created_at": updated_at,
                "link": reverse("my_accommodation_bookings"),
            }
        )

    # Tour booking updates (new flow)
    tour_updates = (
        TourBooking.objects.filter(guest=guest_user)
        .select_related("tour", "schedule")
        .order_by("-last_updated")[:20]
    )
    tour_status_title = {
        "pending": "Tour booking pending",
        "active": "Tour booking active",
        "completed": "Tour booking completed",
        "cancelled": "Tour booking cancelled",
    }
    for booking in tour_updates:
        updated_at = booking.last_updated or booking.booking_date
        notif_status = str(booking.status or "").strip().lower()
        title = tour_status_title.get(notif_status, "Tour booking update")
        message = (
            f"{booking.tour.tour_name} ({booking.schedule.sched_id}) | "
            f"Guests: {booking.total_guests} | Status: {str(booking.status).title()}."
        )
        notifications.append(
            {
                "id": f"tour-{booking.booking_id}-{notif_status}",
                "title": title,
                "message": message,
                "type": "tour",
                "status": notif_status,
                "created_at": updated_at,
                "link": reverse("main-page") + "#myBookings",
            }
        )

    # NOTE:
    # Legacy Pending status notifications are persisted via InAppNotification
    # at status-update time (tour_app StatusUpdateView). We intentionally avoid
    # regenerating synthetic accepted/declined/cancelled notices here because
    # they can incorrectly use schedule start_time as "notification time" and
    # produce misleading dates in the guest dropdown.

    # New published tours with upcoming schedules
    new_tour_schedules = (
        Tour_Schedule.objects.filter(
            status="active",
            start_time__gte=now,
            start_time__lte=now + timedelta(days=30),
            tour__publication_status="published",
        )
        .select_related("tour")
        .order_by("start_time")[:10]
    )
    seen_tour_ids = set()
    for sched in new_tour_schedules:
        if sched.tour_id in seen_tour_ids:
            continue
        seen_tour_ids.add(sched.tour_id)
        notifications.append(
            {
                "id": f"new-tour-{sched.tour_id}",
                "title": "New or upcoming tour available",
                "message": f"{sched.tour.tour_name} is available. Next schedule: {timezone.localtime(sched.start_time).strftime('%b %d, %Y %I:%M %p')}.",
                "type": "announcement",
                "status": "info",
                "created_at": sched.start_time,
                "link": reverse("main-page") + "#tour-packages",
            }
        )

    # New accepted accommodations and new rooms
    new_accommodations = (
        Accomodation.objects.filter(approval_status="accepted", submitted_at__gte=recent_cutoff, is_active=True)
        .order_by("-submitted_at")[:10]
    )
    for accom in new_accommodations:
        notifications.append(
            {
                "id": f"new-accom-{accom.accom_id}",
                "title": "New accommodation available",
                "message": f"{accom.company_name} ({str(accom.company_type).title()}) is now available in {accom.location}.",
                "type": "announcement",
                "status": "info",
                "created_at": accom.submitted_at,
                "link": reverse("accommodation_page"),
            }
        )

    new_rooms = (
        AdminRoom.objects.filter(created_at__gte=recent_cutoff, accommodation__approval_status="accepted", accommodation__is_active=True)
        .select_related("accommodation")
        .order_by("-created_at")[:10]
    )
    for room in new_rooms:
        notifications.append(
            {
                "id": f"new-room-{room.room_id}",
                "title": "New room option added",
                "message": f"{room.accommodation.company_name} added {room.room_name} (up to {room.person_limit} guests).",
                "type": "announcement",
                "status": "info",
                "created_at": room.created_at,
                "link": reverse("accommodation_page"),
            }
        )

    # Stable latest-first order:
    # - transaction updates keep backend created_at
    # - generated announcement items keep their first-seen timestamp,
    #   so newer notifications naturally push older ones down.
    current_ids = set()
    map_updated = False
    for row in notifications:
        notif_id = str(row.get("id") or "").strip()
        if not notif_id:
            continue
        current_ids.add(notif_id)
        if notif_id not in first_seen_map:
            first_seen_map[notif_id] = now.isoformat()
            map_updated = True

    # Trim stale entries and keep map bounded.
    stale_ids = [k for k in first_seen_map.keys() if k not in current_ids]
    if stale_ids:
        for stale_id in stale_ids:
            first_seen_map.pop(stale_id, None)
        map_updated = True
    if len(first_seen_map) > 400:
        ordered_first_seen = sorted(
            first_seen_map.items(),
            key=lambda item: item[1],
            reverse=True,
        )
        first_seen_map = dict(ordered_first_seen[:400])
        map_updated = True

    if map_updated:
        request.session[first_seen_key] = first_seen_map
        request.session.modified = True

    def _parse_iso_dt(value):
        raw = str(value or "").strip()
        if not raw:
            return None
        try:
            parsed = datetime.fromisoformat(raw)
            return timezone.make_aware(parsed) if timezone.is_naive(parsed) else parsed
        except Exception:
            return None

    for row in notifications:
        created_at = row.get("created_at") or now
        if timezone.is_naive(created_at):
            created_at = timezone.make_aware(created_at)

        notif_id = str(row.get("id") or "").strip()
        notif_type = str(row.get("type") or "").strip().lower()
        sort_at = created_at
        if notif_type == "announcement" and notif_id:
            first_seen_dt = _parse_iso_dt(first_seen_map.get(notif_id))
            if first_seen_dt is not None:
                sort_at = first_seen_dt

        row["_created_at_dt"] = created_at
        row["_sort_at"] = sort_at

    notifications.sort(key=lambda row: row.get("_sort_at") or now, reverse=True)
    notifications = notifications[:25]

    serialized = []
    unread_count = 0
    for row in notifications:
        created_at = row.get("_created_at_dt") or row.get("created_at") or now
        if timezone.is_naive(created_at):
            created_at = timezone.make_aware(created_at)
        is_unread = bool((seen_at is None or created_at > seen_at) and (str(row.get("id")) not in seen_ids_set))
        if is_unread:
            unread_count += 1
        serialized.append(
            {
                "id": row.get("id"),
                "title": row.get("title"),
                "message": row.get("message"),
                "type": row.get("type"),
                "status": row.get("status"),
                "link": row.get("link") or "",
                "created_at": created_at.isoformat(),
                "display_date": timezone.localtime(created_at).strftime("%b %d"),
                "is_unread": is_unread,
            }
        )

    db_rows = list(
        InAppNotification.objects.filter(recipient_guest=request.user)
        .order_by("-created_at")[:20]
    )
    db_serialized = []
    for item in db_rows:
        created_at = item.created_at or now
        db_serialized.append(
            {
                "id": f"db-{item.id}",
                "title": item.title,
                "message": item.message,
                "type": item.notification_type,
                "status": item.notification_type,
                "link": item.url or "",
                "created_at": created_at.isoformat(),
                "display_date": timezone.localtime(created_at).strftime("%b %d"),
                "is_unread": not bool(item.is_read),
            }
        )

    merged = db_serialized + serialized
    merged.sort(key=lambda row: str(row.get("created_at") or ""), reverse=True)
    serialized = merged[:25]
    unread_count = sum(1 for row in serialized if row.get("is_unread"))

    response = JsonResponse(
        {
            "success": True,
            "unread_count": unread_count,
            "notifications": serialized,
            "generated_at": now.isoformat(),
        }
    )
    seen_at_out = seen_at.isoformat() if seen_at is not None else str(seen_raw or "").strip()
    return _write_cookie_state(response, seen_at_raw=seen_at_out, seen_ids=list(seen_ids_set))

def register(request):
    next_url = str(request.GET.get("next") or request.POST.get("next") or "").strip()
    owner_signup_intent = str(request.GET.get("owner_signup") or "").strip() == "1"

    if request.method == 'POST':
        recaptcha_ok, recaptcha_error = _verify_recaptcha_response(request)
        if not recaptcha_ok:
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return JsonResponse({'success': False, 'message': recaptcha_error}, status=400)
            messages.error(request, recaptcha_error)
            return redirect('main-page')

        print("Files in request:", request.FILES)
        print("POST data:", request.POST)
        form = GuestRegistrationForm(request.POST, request.FILES)
        if form.is_valid():
            try:
                guest = form.save(commit=False)

                # Handle optional company_name field
                company_name = form.cleaned_data.get('company_name')
                if company_name:
                    guest.company_name = company_name
                else:
                    guest.company_name = None

                # Save the guest to create the instance with an ID
                guest.save()

                register_as_owner_requested = bool(form.cleaned_data.get("register_as_accommodation_owner"))
                owner_signup_intent_post = str(request.POST.get("owner_signup_intent") or "").strip() == "1"
                # Safety guard: owner registration requires BOTH
                # 1) explicit owner checkbox/flag and
                # 2) explicit owner-signup intent from the owner flow.
                # This prevents normal guest registrations from being accidentally
                # routed into accommodation-owner approval.
                register_as_owner = register_as_owner_requested and owner_signup_intent_post
                requested_next = str(request.POST.get("next") or request.GET.get("next") or "").strip()
                redirect_url = ""
                if register_as_owner:
                    pending_group, _ = Group.objects.get_or_create(name="accommodation_owner_pending")
                    approved_group, _ = Group.objects.get_or_create(name="accommodation_owner")
                    declined_group, _ = Group.objects.get_or_create(name="accommodation_owner_declined")
                    guest.groups.remove(approved_group, declined_group)
                    guest.groups.add(pending_group)
                    notify_admins(
                        title="New owner registration pending",
                        message=f"{guest.first_name} {guest.last_name} requested accommodation-owner access.",
                        notification_type="approval",
                        url=reverse("admin_app:pending_accommodation_owners"),
                        dedupe_key=f"owner-registration-{guest.pk}",
                    )
                    create_notification(
                        recipient_guest=guest,
                        title="Owner registration submitted",
                        message="Your accommodation-owner account request is pending admin approval.",
                        notification_type="approval",
                        url=reverse("admin_app:login"),
                        dedupe_key=f"owner-registration-submitted-{guest.pk}",
                        related_object_id=str(guest.pk),
                    )
                    redirect_url = reverse("admin_app:login")
                    if requested_next and url_has_allowed_host_and_scheme(
                        requested_next,
                        allowed_hosts={request.get_host()},
                    ):
                        # Keep next flow safe for callers, but owner approval is still required.
                        redirect_url = requested_next
                else:
                    # Defensive cleanup: keep standard guest signups out of owner groups.
                    pending_group, _ = Group.objects.get_or_create(name="accommodation_owner_pending")
                    approved_group, _ = Group.objects.get_or_create(name="accommodation_owner")
                    declined_group, _ = Group.objects.get_or_create(name="accommodation_owner_declined")
                    guest.groups.remove(pending_group, approved_group, declined_group)

                messages.success(request, 'Registration successful! You can now log in.')

                # For AJAX requests, return JSON response
                if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                    return JsonResponse({
                        'success': True,
                        'message': 'Registration successful! Logging you in...',
                        'redirect_url': redirect_url,
                    })
                if redirect_url:
                    return redirect(redirect_url)
                return redirect('login')
            except IntegrityError as e:
                error_msg = str(e).lower()
                if "email" in error_msg:
                    form.add_error('email', 'This email is already registered. Please use another or login.')
                    messages.error(request, 'This email is already registered. Please use another or login.')
                else:
                    messages.error(request, 'An error occurred during registration.')
        else:
            # Form is invalid, so we don't change the generic message
            messages.error(request, 'Please correct the errors below.')
        
        # For AJAX requests with errors, return JSON response
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({
                'success': False,
                'message': 'Please correct the errors below.',
                'errors': {field: errors[0] for field, errors in form.errors.items()}
            })
    else:
        form = GuestRegistrationForm()
    return render(
        request,
        'register.html',
        {
            'form': form,
            'next_url': next_url,
            'owner_signup_intent': owner_signup_intent,
        },
    )


def login_view(request):
    if request.user.is_authenticated:
        if is_guest_tourist_user(request.user, request=request):
            return redirect('main-page')
        messages.info(
            request,
            "This account is configured for accommodation-owner access. "
            "Please log in via the Admin Panel.",
        )
        return redirect("admin_app:login")

    if request.method == 'POST':
        is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
        recaptcha_ok, recaptcha_error = _verify_recaptcha_response(request)
        if not recaptcha_ok:
            if is_ajax:
                return JsonResponse({'success': False, 'message': recaptcha_error}, status=400)
            messages.error(request, recaptcha_error)
            return redirect('main-page')

        email = (request.POST.get('email') or '').strip()
        password = request.POST.get('password') or ''
        
        # Try to find a user with the given email
        try:
            user = Guest.objects.get(email__iexact=email)
            # Authenticate using the actual username stored for the guest.
            user = authenticate(request, username=user.username, password=password)
            
            if user is not None:
                # Enforce split login entry points:
                # - guest_app/login is for guest/tourist accounts only
                # - accommodation owners should use admin_app/login
                if not is_guest_tourist_user(user, request=request):
                    owner_login_url = reverse("admin_app:login")
                    owner_only_message = (
                        "Accommodation owners must log in via the Admin Panel login page."
                    )
                    if is_ajax:
                        return JsonResponse({
                            'success': False,
                            'message': owner_only_message,
                            'owner_login_only': True,
                            'redirect_url': owner_login_url,
                        })
                    messages.error(request, owner_only_message)
                    return redirect(owner_login_url)

                auth_login(request, user)
                requested_next = str(request.POST.get("next") or request.GET.get("next") or "").strip()
                redirect_url = ""
                if requested_next and url_has_allowed_host_and_scheme(
                    requested_next,
                    allowed_hosts={request.get_host()},
                ):
                    redirect_url = requested_next
                # For AJAX requests, return JSON response
                if is_ajax:
                    return JsonResponse({
                        'success': True,
                        'first_name': user.first_name,
                        'message': 'Login successful',
                        'redirect_url': redirect_url,
                    })
                return redirect(request.GET.get('next', 'main-page'))
            else:
                # For AJAX requests, return JSON response
                if is_ajax:
                    return JsonResponse({
                        'success': False, 
                        'message': 'Invalid email or password'
                    })
                messages.error(request, "Invalid email or password")
        except Guest.DoesNotExist:
            # For AJAX requests, return JSON response
            if is_ajax:
                return JsonResponse({
                    'success': False, 
                    'message': 'Invalid email or password'
                })
            messages.error(request, "Invalid email or password")
        
        return redirect('main-page')

    # Keep this URL for login POST handling, but use main-page as the UI entry point.
    return redirect('main-page')


def logout_view(request):
    if request.user.is_authenticated:
        logout(request)  # Logs out the user
        request.session.flush()  # Completely removes session data
        request.session.clear()  # Ensures session dictionary is emptied (optional)

    messages.success(request, "You have been logged out successfully.")
    
    # For AJAX requests, return JSON response
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({
            'success': True,
            'message': 'You have been logged out successfully.'
        })
        
    return redirect('main-page')  # Redirect to main page instead of login


def tour_schedule_detail(request, sched_id):
    # Fetch the schedule details
    schedule = get_object_or_404(
        Tour_Schedule,
        sched_id=sched_id,
        tour__publication_status="published",
        end_time__gte=timezone.now(),
    )
    if str(getattr(schedule, "status", "")).strip().lower() == "cancelled":
        return redirect('main-page')

    context = {
        'schedule': schedule,
    }

    return render(request, 'guest_book.html', context)


# API to fetch tour schedules dynamically
def get_tour_schedules(request, tour_id):
    tour_schedules = Tour_Schedule.objects.filter(
        tour_id=tour_id,
        tour__publication_status="published",
        end_time__gte=timezone.now(),
    ).exclude(status__iexact='cancelled')
    schedules = []

    for schedule in tour_schedules:
        schedules.append({
            'start': schedule.start_time.strftime('%Y-%m-%dT%H:%M:%S'),
            'end': schedule.end_time.strftime('%Y-%m-%dT%H:%M:%S'),
        })

    return JsonResponse({'schedules': schedules})




@login_required
@guest_tourist_required
@require_POST
def book_tour(request):
    try:
        recaptcha_ok, recaptcha_error = _verify_recaptcha_response(request)
        if not recaptcha_ok:
            return JsonResponse({'error': recaptcha_error}, status=400)

        guest = request.user
        sched_id = request.POST.get('sched_id')
        price = float(request.POST.get('price', 0))
        total_guests = int(request.POST.get('total_guests', 1))
        if total_guests <= 0:
            return JsonResponse({'error': 'Total guests must be at least 1.'}, status=400)

        selected_companions_json = request.POST.get('selected_companions', '[]')
        selected_companions = json.loads(selected_companions_json)

        schedule = get_object_or_404(
            Tour_Schedule,
            sched_id=sched_id,
            tour__publication_status='published',
        )
        tour = schedule.tour
        _safe_log_tour_event(
            request=request,
            user=guest,
            event_type="save",
            item_ref="tour_booking_started",
        )

        if schedule.slots_available < total_guests:
            return JsonResponse({'error': 'Not enough available slots.'}, status=400)

        companion_names = []
        if selected_companions:
            companions = Guest.objects.filter(guest_id__in=selected_companions, made_by=guest)
        else:
            companions = Guest.objects.none()

        pending_booking = Pending.objects.create(
            guest_id=guest,
            sched_id=schedule,
            tour_id=tour,
            status='Pending',
            total_guests=total_guests,
            your_name=f'{guest.first_name} {guest.last_name}',
            your_email=guest.email,
            your_phone=guest.phone_number,
            num_adults=total_guests,
            num_children=0,
        )
        _safe_log_tour_event(
            request=request,
            user=guest,
            event_type="book",
            item_ref="tour_booking_submitted",
        )
        create_notification(
            recipient_guest=guest,
            title="Tour booking submitted",
            message=f"Your booking request for {tour.tour_name} is pending staff review.",
            notification_type="booking",
            url=reverse("main-page") + "#myBookings",
            dedupe_key=f"tour-pending-{pending_booking.id}",
            related_object_id=str(pending_booking.id),
        )
        notify_assigned_employees_for_schedule(
            schedule=schedule,
            title="New tour booking request",
            message=f"{tour.tour_name} received a new pending booking from {guest.first_name} {guest.last_name}.",
            notification_type="booking",
            url=reverse("tour_app:pending_view"),
            dedupe_key_prefix=f"tour-pending-{pending_booking.id}",
        )

        if selected_companions:
            for companion in companions:
                BookingCompanion.objects.create(
                    booking=pending_booking,
                    companion=companion,
                )
                companion_names.append(f'{companion.first_name} {companion.last_name}')

        schedule.slots_booked += total_guests
        schedule.slots_available -= total_guests
        schedule.save()

        total_amount = total_guests * price

        try:
            subject = f'Booking Request Received for {tour.tour_name}'
            start_time = timezone.localtime(schedule.start_time)
            end_time = timezone.localtime(schedule.end_time)
            start_formatted = start_time.strftime('%A %d %B %Y at %I:%M %p')
            end_formatted = end_time.strftime('%A %d %B %Y at %I:%M %p')
            current_ph_time = timezone.now() + timedelta(hours=8)
            ph_time_formatted = current_ph_time.strftime('%A %d %B %Y at %I:%M %p')
            guest_country_formatted = f'(Please check local time in {guest.country_of_origin})'
            companions_list = '\n'.join([f'- {name}' for name in companion_names]) or 'None'

            message = f'''Dear {guest.first_name},

Your booking request for {tour.tour_name} has been received and is pending approval.

Booking Details:
- Tour: {tour.tour_name}
- Schedule: {start_formatted} to {end_formatted}
- Total Guests: {total_guests}
- Total Amount: PHP {total_amount:.2f}

Companions included:
{companions_list}

Time Information:
- Current Philippine Time: {ph_time_formatted}
- Your Country ({guest.country_of_origin}): {guest_country_formatted}

We will notify you once your booking is confirmed or if we need additional information.

Thank you for choosing our tours!

Best regards,
The Tour Team'''

            send_mail(
                subject=subject,
                message=message,
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[guest.email],
                fail_silently=False,
            )
            _safe_log_tour_email_dispatch(
                email_type="tour_pending_email_sent",
                success=True,
            )
        except Exception as email_error:
            print(f'Email sending failed: {str(email_error)}')
            _safe_log_tour_email_dispatch(
                email_type="tour_pending_email_failed",
                success=False,
                error_message=str(email_error),
            )

        treasurer_link = _resolve_treasurer_billing_url()
        response_payload = {
            'success': (
                'Booking request submitted. You will receive a confirmation update from tourism staff.'
            ),
            'total_payment': total_amount,
        }
        if treasurer_link:
            response_payload['success'] += " Continue to the Treasurer's Office billing page to complete payment."
            response_payload["billing_link"] = treasurer_link
            response_payload["billing_link_label"] = "Proceed to Treasurer Billing"
        else:
            response_payload['success'] += (
                ' Treasurer billing link is not configured yet. '
                'Please wait for payment instructions from the Tourism Office.'
            )
        return JsonResponse(response_payload)
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=400)

def guest_book(request, tour_id):
    """View for displaying tour booking page with language support"""
    # Get current language
    current_language = get_current_language(request)
    
    # Retrieve the tour based on the provided tour_id from the URL.
    tour = get_object_or_404(Tour_Add, tour_id=tour_id, publication_status="published")

    # Retrieve all schedules associated with this tour.
    # Assuming your Tour_Schedule model's foreign key to Tour_Add is named "tour_id".
    schedules = Tour_Schedule.objects.filter(
        tour_id=tour,
        end_time__gte=timezone.now(),
    ).exclude(status__iexact='cancelled')
    
    # Prepare translated tour data
    tour_data = {
        'id': tour.tour_id,
        'name': getattr(tour, f'tour_name_{current_language}', tour.tour_name),
        'description': getattr(tour, f'description_{current_language}', tour.description),
        # Add other translatable fields
    }
    
    # Add translations for this specific tour to the translations dictionary
    tour_translations = {
        f'tour_{tour.tour_id}_name': tour_data['name'],
        f'tour_{tour.tour_id}_description': tour_data['description'],
    }
    
    # Get generic translations and add tour-specific ones
    translations = json.loads(get_translations_json(current_language))
    translations.update(tour_translations)
    
    # Add translations for booking-related terms
    booking_translations = {
        'schedule_id': translate('schedule_id', current_language),
        'start_time': translate('start_time', current_language),
        'end_time': translate('end_time', current_language),
        'price': translate('price', current_language),
        'available_slots': translate('available_slots', current_language),
        'booked_slots': translate('booked_slots', current_language),
        'book_this_schedule': translate('book_this_schedule', current_language),
        'no_more_slots': translate('no_more_slots', current_language),
        'no_schedules': translate('no_schedules', current_language),
        'back_to_main': translate('back_to_main', current_language),
    }
    translations.update(booking_translations)
    
    context = {
        'tour': tour,
        'schedules': schedules,
        'current_language': current_language,
        'translations_json': json.dumps(translations),
        'tour_data': tour_data,
        'booking_requires_login': not bool(getattr(request.user, "is_authenticated", False)),
        'recaptcha_site_key': str(getattr(settings, 'RECAPTCHA_SITE_KEY', '') or '').strip(),
        'recaptcha_required': _is_recaptcha_required(),
    }
    
    return render(request, 'guest_book.html', context)

@xframe_options_exempt
def map_view(request):
    """View for displaying the interactive Bayawan City map with language support"""
    # Get current language
    current_language = get_current_language(request)

    # Guest visibility policy:
    # - Anonymous users: public/system markers only (user=None)
    # - Authenticated users: public/system markers + their own markers
    if request.user.is_authenticated:
        bookmarks = MapBookmark.objects.filter(
            models.Q(user__isnull=True) | models.Q(user=request.user)
        )
    else:
        bookmarks = MapBookmark.objects.filter(user__isnull=True)
    
    # Translate bookmarks
    translated_bookmarks = []
    for bookmark in bookmarks:
        bookmark_data = {
            'id': bookmark.id,
            'name': bookmark.get_name(current_language),
            'category': bookmark.category,
            'lat': bookmark.latitude,
            'lng': bookmark.longitude,
            'details': bookmark.get_details(current_language) or '',
            'images': []
        }
        
        # Get translated images
        for image in bookmark.images.all():
            image_data = {
                'id': image.id,
                'title': image.get_title(current_language),
                'description': image.get_description(current_language),
                'url': request.build_absolute_uri(image.image.url) if image.image else None,
            }
            bookmark_data['images'].append(image_data)
        
        translated_bookmarks.append(bookmark_data)
    
    return render(request, 'map.html', {
        'bookmarks': bookmarks,  # Original queryset for Django templates
        'translated_bookmarks': translated_bookmarks,  # Translated data
        'current_language': current_language,
        'translations_json': get_translations_json(current_language),
        'map_mode': 'guest',
        'can_edit_bookmarks': False,
        'guest_hidden_place_names': (
            list(getattr(settings, "TOURISM_MAP_GUEST_HIDDEN_PLACES", ["Bayawan City Hall"]))
            if isinstance(getattr(settings, "TOURISM_MAP_GUEST_HIDDEN_PLACES", ["Bayawan City Hall"]), (list, tuple))
            else ["Bayawan City Hall"]
        ),
    })

# API endpoints for map bookmarks
def bookmark_list(request):
    """API endpoint to list all bookmarks with language support"""
    print("Bookmark list requested")
    
    # Get current language
    current_language = get_current_language(request)
    
    if request.user.is_authenticated:
        bookmarks = MapBookmark.objects.filter(
            models.Q(user__isnull=True) | models.Q(user=request.user)
        )
    else:
        # For anonymous users, get public/system bookmarks only
        bookmarks = MapBookmark.objects.filter(user__isnull=True)
    
    data = []
    for bookmark in bookmarks:
        # Get translated name and details
        name = bookmark.get_name(current_language)
        details = bookmark.get_details(current_language)
        
        # Get images for this bookmark with translations
        images = []
        for image in bookmark.images.all():
            image_data = {
                'id': image.id,
                'title': image.get_title(current_language),
                'description': image.get_description(current_language),
                'url': request.build_absolute_uri(image.image.url) if image.image else None,
            }
            images.append(image_data)
        
        # Add bookmark data with images
        data.append({
            'id': bookmark.id,
            'name': name,
            'category': bookmark.category,
            'lat': bookmark.latitude,
            'lng': bookmark.longitude,
            'details': details or '',
            'images': images
        })
    
    return JsonResponse({'bookmarks': data})


@require_http_methods(["GET", "POST"])
def current_location_api(request):
    """
    Lightweight session-backed location endpoint for guest map/chat assistance.
    """
    session_key = "guest_current_location"
    if request.method == "GET":
        payload = request.session.get(session_key) if hasattr(request, "session") else None
        if isinstance(payload, dict) and payload.get("latitude") is not None and payload.get("longitude") is not None:
            return JsonResponse({"success": True, "location": payload})
        return JsonResponse({"success": False, "message": "Current location not set."}, status=404)

    try:
        body = json.loads(request.body or "{}")
    except Exception:
        body = {}
    lat_raw = body.get("latitude")
    lng_raw = body.get("longitude")
    accuracy_raw = body.get("accuracy")
    source_raw = str(body.get("source") or "browser_geolocation").strip() or "browser_geolocation"

    try:
        lat = float(lat_raw)
        lng = float(lng_raw)
    except Exception:
        return JsonResponse({"success": False, "message": "Invalid latitude/longitude payload."}, status=400)

    if not (-90.0 <= lat <= 90.0 and -180.0 <= lng <= 180.0):
        return JsonResponse({"success": False, "message": "Latitude/longitude out of range."}, status=400)

    accuracy = None
    try:
        if accuracy_raw not in (None, ""):
            accuracy = float(accuracy_raw)
    except Exception:
        accuracy = None

    location_payload = {
        "latitude": round(lat, 6),
        "longitude": round(lng, 6),
        "accuracy_m": round(accuracy, 2) if isinstance(accuracy, float) and accuracy >= 0 else None,
        "source": source_raw[:40],
        "captured_at": timezone.now().isoformat(),
    }
    if hasattr(request, "session"):
        request.session[session_key] = location_payload
        request.session.modified = True
    return JsonResponse({"success": True, "location": location_payload})

@csrf_exempt
def bookmark_create(request):
    """API endpoint to create a new bookmark"""
    print("Bookmark create requested")
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
            print("Received bookmark data:", data)
            
            bookmark = MapBookmark(
                name=data.get('name'),
                category=data.get('category', 'custom'),
                latitude=data.get('lat'),
                longitude=data.get('lng'),
                details=data.get('details', '')
            )
            
            if request.user.is_authenticated:
                bookmark.user = request.user
                
            bookmark.save()
            print("Bookmark created with ID:", bookmark.id)
            
            return JsonResponse({
                'success': True,
                'id': bookmark.id,
                'message': 'Bookmark created successfully'
            })
        except Exception as e:
            print("Error creating bookmark:", str(e))
            return JsonResponse({
                'success': False,
                'message': str(e)
            }, status=400)
    
    return JsonResponse({'message': 'Invalid request method'}, status=405)

@csrf_exempt
def bookmark_update(request, bookmark_id):
    """API endpoint to update a bookmark"""
    print(f"Bookmark update requested for ID: {bookmark_id}")
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
            print("Update data:", data)
            
            # Get the bookmark, checking for ownership
            if request.user.is_authenticated:
                bookmark = MapBookmark.objects.get(id=bookmark_id, user=request.user)
            else:
                bookmark = MapBookmark.objects.get(id=bookmark_id, user=None)
            
            # Update fields
            if 'name' in data:
                bookmark.name = data['name']
            if 'category' in data:
                bookmark.category = data['category']
            if 'lat' in data:
                bookmark.latitude = data['lat']
            if 'lng' in data:
                bookmark.longitude = data['lng']
            if 'details' in data:
                bookmark.details = data['details']
            
            bookmark.save()
            print("Bookmark updated successfully")
            
            return JsonResponse({
                'success': True,
                'message': 'Bookmark updated successfully'
            })
        except MapBookmark.DoesNotExist:
            print("Bookmark not found")
            return JsonResponse({
                'success': False,
                'message': 'Bookmark not found or access denied'
            }, status=404)
        except Exception as e:
            print("Error updating bookmark:", str(e))
            return JsonResponse({
                'success': False,
                'message': str(e)
            }, status=400)
    
    return JsonResponse({'message': 'Invalid request method'}, status=405)

@csrf_exempt
def bookmark_delete(request, bookmark_id):
    """API endpoint to delete a bookmark"""
    print(f"Bookmark delete requested for ID: {bookmark_id}")
    if request.method == 'POST':
        try:
            # Get the bookmark, checking for ownership
            if request.user.is_authenticated:
                bookmark = MapBookmark.objects.get(id=bookmark_id, user=request.user)
            else:
                bookmark = MapBookmark.objects.get(id=bookmark_id, user=None)
            
            bookmark.delete()
            print("Bookmark deleted successfully")
            
            return JsonResponse({
                'success': True,
                'message': 'Bookmark deleted successfully'
            })
        except MapBookmark.DoesNotExist:
            print("Bookmark not found")
            return JsonResponse({
                'success': False,
                'message': 'Bookmark not found or access denied'
            }, status=404)
        except Exception as e:
            print("Error deleting bookmark:", str(e))
            return JsonResponse({
                'success': False,
                'message': str(e)
            }, status=400)
    
    return JsonResponse({'message': 'Invalid request method'}, status=405)

def bookmark_debug(request):
    """Debug view for bookmark API"""
    # Get all bookmarks
    all_bookmarks = MapBookmark.objects.all()
    
    # Prepare response data
    debug_info = {
        'total_bookmarks': all_bookmarks.count(),
        'bookmarks': [{
            'id': b.id,
            'name': b.name,
            'category': b.category,
            'latitude': b.latitude,
            'longitude': b.longitude,
            'user': b.user.username if b.user else None,
            'created_at': b.created_at.isoformat() if b.created_at else None
        } for b in all_bookmarks],
        'user': {
            'is_authenticated': request.user.is_authenticated,
            'username': request.user.username if request.user.is_authenticated else None
        },
        'csrf_token': request.META.get('CSRF_COOKIE', 'Not set')
    }
    
    # Return formatted JSON response
    response = HttpResponse(
        json.dumps(debug_info, indent=2),
        content_type='application/json'
    )
    return response

@csrf_exempt
def bookmark_add_image(request, bookmark_id):
    """API endpoint to add an image to a bookmark"""
    if request.method == 'POST':
        try:
            # Get the bookmark
            bookmark = get_object_or_404(MapBookmark, id=bookmark_id)
            
            # Check if user owns the bookmark or it's a public bookmark
            if request.user.is_authenticated:
                if bookmark.user and bookmark.user != request.user:
                    return JsonResponse({
                        'success': False,
                        'message': 'You do not have permission to add images to this bookmark'
                    }, status=403)
            elif bookmark.user is not None:
                return JsonResponse({
                    'success': False,
                    'message': 'You must be logged in to add images to this bookmark'
                }, status=401)
            
            # Process the image data
            data = json.loads(request.body)
            image_data = data.get('image')  # Base64 encoded image
            title = data.get('title', '')
            description = data.get('description', '')
            
            print(f"Received image upload request for bookmark {bookmark_id}")
            print(f"Image data length: {len(image_data) if image_data else 'None'}")
            
            # Convert base64 to image file
            if image_data:
                try:
                    # Handle data URI format (data:image/jpeg;base64,...)
                    if ',' in image_data:
                        format_info, image_data = image_data.split(',', 1)
                        print(f"Image format: {format_info}")
                    
                    # Decode the base64 data
                    image_content = ContentFile(base64.b64decode(image_data))
                    
                    # Create the bookmark image
                    bookmark_image = BookmarkImage(
                        bookmark=bookmark,
                        title=title,
                        description=description
                    )
                    
                    # Save the image file with a unique name
                    import uuid
                    file_name = f"bookmark_{bookmark.id}_{uuid.uuid4().hex}.jpg"
                    bookmark_image.image.save(file_name, image_content, save=True)
                    
                    # Make sure image URL is absolute
                    image_url = request.build_absolute_uri(bookmark_image.image.url)
                    
                    return JsonResponse({
                        'success': True,
                        'id': bookmark_image.id,
                        'message': 'Image added successfully',
                        'image_url': image_url
                    })
                except Exception as e:
                    print(f"Error processing image: {str(e)}")
                    return JsonResponse({
                        'success': False,
                        'message': f'Error processing image: {str(e)}'
                    }, status=400)
            else:
                return JsonResponse({
                    'success': False,
                    'message': 'No image data provided'
                }, status=400)
                
        except Exception as e:
            import traceback
            print(f"Error adding image: {str(e)}")
            print(traceback.format_exc())
            return JsonResponse({
                'success': False,
                'message': str(e)
            }, status=400)
    
    return JsonResponse({'message': 'Invalid request method'}, status=405)

@csrf_exempt
def bookmark_delete_image(request, image_id):
    """API endpoint to delete a bookmark image"""
    if request.method == 'POST':
        try:
            # Get the image
            image = get_object_or_404(BookmarkImage, id=image_id)
            bookmark = image.bookmark
            
            # Check if user owns the bookmark or it's a public bookmark
            if request.user.is_authenticated:
                if bookmark.user and bookmark.user != request.user:
                    return JsonResponse({
                        'success': False,
                        'message': 'You do not have permission to delete this image'
                    }, status=403)
            elif bookmark.user is not None:
                return JsonResponse({
                    'success': False,
                    'message': 'You must be logged in to delete this image'
                }, status=401)
            
            # Delete the image file and record
            image.image.delete()
            image.delete()
            
            return JsonResponse({
                'success': True,
                'message': 'Image deleted successfully'
            })
        except Exception as e:
            return JsonResponse({
                'success': False,
                'message': str(e)
            }, status=400)
    
    return JsonResponse({'message': 'Invalid request method'}, status=405)

@csrf_exempt
def bookmark_get_images(request, bookmark_id):
    """API endpoint to get all images for a bookmark"""
    if request.method == 'GET':
        try:
            # Get the bookmark
            bookmark = get_object_or_404(MapBookmark, id=bookmark_id)
            
            # Get all images for the bookmark
            images = bookmark.images.all()
            
            # Prepare the response data
            data = [{
                'id': image.id,
                'title': image.title,
                'description': image.description,
                'url': request.build_absolute_uri(image.image.url),
                'upload_date': image.upload_date.isoformat()
            } for image in images]
            
            return JsonResponse({
                'success': True,
                'images': data
            })
        except Exception as e:
            return JsonResponse({
                'success': False,
                'message': str(e)
            }, status=400)
    
    return JsonResponse({'message': 'Invalid request method'}, status=405)

# Profile update functions
@login_required
@guest_tourist_required
def my_profile(request):
    return render(request, "my_profile.html", {"user": request.user})


@require_http_methods(["GET"])
def get_profile_data(request):
    if request.user.is_authenticated:
        user = request.user
        return JsonResponse({
            'success': True,
            'user': {
                'first_name': user.first_name,
                'middle_initial': user.middle_initial,
                'last_name': user.last_name,
                'email': user.email,
                'birthday': user.birthday.isoformat() if user.birthday else '',
                'country_of_origin': user.country_of_origin,
                'city': user.city,
                'phone_number': user.phone_number,
                'age': user.age,
                'age_label': user.age_label,
                'company_name': user.company_name,
                'sex': user.sex,
                'has_disability': user.has_disability,
                'disability_type': user.disability_type,
            }
        })
    return JsonResponse({'success': False, 'message': 'User not authenticated'})

@require_http_methods(["POST"])
def update_profile(request):
    if request.user.is_authenticated:
        user = request.user
        errors = {}
        
        # Basic validation
        if not request.POST.get('first_name'):
            errors['first_name'] = 'First name is required'
        
        if not request.POST.get('last_name'):
            errors['last_name'] = 'Last name is required'
        
        if not request.POST.get('email'):
            errors['email'] = 'Email is required'

        if not request.POST.get('country_of_origin'):
            errors['country_of_origin'] = 'Country of origin is required'
        
        if not request.POST.get('city'):
            errors['city'] = 'City is required'
        
        if not request.POST.get('phone_number'):
            errors['phone_number'] = 'Phone number is required'
        
        if not request.POST.get('sex'):
            errors['sex'] = 'Please select your sex'
        
        # If we have errors, return them
        if errors:
            return JsonResponse({
                'success': False,
                'errors': errors
            })
        
        # If validation passes, update the user
        try:
            user.first_name = request.POST.get('first_name')
            user.middle_initial = request.POST.get('middle_initial')
            user.last_name = request.POST.get('last_name')
            requested_email = (request.POST.get('email') or '').strip()
            if requested_email and Guest.objects.exclude(pk=user.pk).filter(email__iexact=requested_email).exists():
                return JsonResponse({
                    'success': False,
                    'errors': {'email': 'This email is already in use'}
                })
            if requested_email:
                user.email = requested_email
            user.country_of_origin = request.POST.get('country_of_origin')
            user.city = request.POST.get('city')
            user.phone_number = request.POST.get('phone_number')

            birthday_raw = (request.POST.get('birthday') or '').strip()
            if birthday_raw:
                try:
                    user.birthday = datetime.strptime(birthday_raw, '%Y-%m-%d').date()
                except ValueError:
                    return JsonResponse({
                        'success': False,
                        'errors': {'birthday': 'Invalid birthday format'}
                    })
            
            # Handle optional fields
            age = request.POST.get('age')
            if age:
                user.age = int(age)
            
            # Handle company_name as optional
            company_name = request.POST.get('company_name')
            if company_name:
                user.company_name = company_name
            else:
                user.company_name = None
                
            user.sex = request.POST.get('sex')
            user.has_disability = str(request.POST.get('has_disability', '')).lower() in {'true', '1', 'on', 'yes'}
            user.disability_type = (request.POST.get('disability_type') or '').strip() if user.has_disability else None
            
            if 'picture' in request.FILES:
                user.picture = request.FILES['picture']
                
            user.save()
            return JsonResponse({'success': True})
        except Exception as e:
            return JsonResponse({
                'success': False,
                'message': str(e)
            })
    
    return JsonResponse({'success': False, 'message': 'User not authenticated'})

# Language-related views
def set_language_view(request, lang_code):
    """View to set language preference"""
    if lang_code not in ['en', 'tl', 'ceb', 'es']:
        lang_code = 'en'
        
    # Set language in session
    set_language(request, lang_code)
    
    # Return JSON response for AJAX calls
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({'success': True, 'language': lang_code})
        
    # Otherwise redirect to referer or home
    referer = request.META.get('HTTP_REFERER', '/')
    return redirect(referer)

@require_http_methods(["GET"])
def get_translations_view(request, lang_code):
    """API endpoint to get all translations for a language as JSON"""
    if lang_code not in ['en', 'tl', 'ceb', 'es']:
        lang_code = 'en'
    
    # Return all translations as JSON
    return JsonResponse({
        'success': True,
        'language': lang_code,
        'translations': json.loads(get_translations_json(lang_code))
    })

@login_required
def cancel_booking(request):
    """Handle booking cancellation via AJAX"""
    if request.method == 'POST':
        booking_id = request.POST.get('booking_id')
        booking_type = request.POST.get('booking_type', 'tour')
        cancellation_reason = request.POST.get('cancellation_reason', '')
        
        # Validate input
        if not booking_id:
            return JsonResponse({'success': False, 'message': 'Booking ID is required'})
        
        try:
            # Handle different booking types
            if booking_type == 'pending':
                # Handle Pending model bookings
                booking = get_object_or_404(Pending, id=booking_id, guest_id=request.user)
                booking.status = 'Cancelled'
                booking.cancellation_reason = cancellation_reason
                booking.save()
                create_notification(
                    recipient_guest=request.user,
                    title="Tour booking cancelled",
                    message=f"Your booking for {booking.tour_id.tour_name} was cancelled.",
                    notification_type="booking",
                    url=reverse("main-page") + "#myBookings",
                    dedupe_key=f"tour-cancelled-{booking.id}",
                    related_object_id=str(booking.id),
                )
                notify_assigned_employees_for_schedule(
                    schedule=booking.sched_id,
                    title="Tour booking cancelled",
                    message=f"A booking for {booking.tour_id.tour_name} was cancelled by the guest.",
                    notification_type="booking",
                    url=reverse("tour_app:pending_view"),
                    dedupe_key_prefix=f"tour-cancelled-{booking.id}",
                )
            else:
                # Handle TourBooking model bookings
                booking = get_object_or_404(TourBooking, booking_id=booking_id, guest=request.user)
                booking.status = 'cancelled'
                booking.cancellation_reason = cancellation_reason
                booking.save()
                create_notification(
                    recipient_guest=request.user,
                    title="Tour booking cancelled",
                    message=f"Your booking for {booking.tour.tour_name} was cancelled.",
                    notification_type="booking",
                    url=reverse("main-page") + "#myBookings",
                    dedupe_key=f"tourbooking-cancelled-{booking.booking_id}",
                    related_object_id=str(booking.booking_id),
                )
            
            # You could also add email notification to staff here
            
            return JsonResponse({
                'success': True, 
                'message': 'Booking cancelled successfully'
            })
            
        except Exception as e:
            return JsonResponse({
                'success': False, 
                'message': f'Error cancelling booking: {str(e)}'
            })
    
    return JsonResponse({'success': False, 'message': 'Invalid request method'})

@login_required
@require_http_methods(["GET"])
def get_tour_itinerary(request):
    """Get detailed itinerary information for a tour schedule."""
    tour_id = str(request.GET.get('tour_id') or '').strip()
    sched_id = str(request.GET.get('sched_id') or '').strip()

    if not tour_id:
        return JsonResponse({'success': False, 'message': 'Missing tour_id parameter'}, status=400)

    tour = get_object_or_404(Tour_Add, tour_id=tour_id, publication_status="published")

    schedules_qs = Tour_Schedule.objects.filter(tour=tour)
    if sched_id:
        schedules_qs = schedules_qs.filter(sched_id=sched_id)
    schedule = schedules_qs.order_by('start_time').first()
    if schedule is None:
        return JsonResponse({'success': False, 'message': 'Schedule not found'}, status=404)

    events = (
        Tour_Event.objects.filter(sched_id=schedule)
        .order_by('day_number', 'event_time')
    )

    if not events.exists():
        return JsonResponse({
            'success': True,
            'tour_name': tour.tour_name,
            'itinerary_html': '<p>No detailed itinerary available for this schedule.</p>',
        })

    days = {}
    for event in events:
        days.setdefault(event.day_number, []).append(event)

    itinerary_parts = []
    for day_number in sorted(days.keys()):
        itinerary_parts.append(f'<div class="itinerary-day"><h4>Day {day_number}</h4><ul>')
        for event in days[day_number]:
            event_time_display = event.event_time.strftime('%I:%M %p') if event.event_time else ''
            name = event.event_name or 'Activity'
            description = event.event_description or ''
            location = event.event_location or ''
            description_html = f'<br><span class="event-description">{description}</span>' if description else ''
            location_html = f'<br><small><strong>Location:</strong> {location}</small>' if location else ''
            itinerary_parts.append(
                f'<li>'
                f'<strong>{event_time_display}</strong> - {name}'
                f'{description_html}'
                f'{location_html}'
                f'</li>'
            )
        itinerary_parts.append('</ul></div>')

    return JsonResponse({
        'success': True,
        'tour_name': tour.tour_name,
        'itinerary_html': ''.join(itinerary_parts),
    })

# @login_required
# def get_tour_payables(request):
#     """Get detailed payable information for a tour"""
#     tour_id = request.GET.get('tour_id')
#     sched_id = request.GET.get('sched_id')
#     
#     if not tour_id or not sched_id:
#         return JsonResponse({
#             'success': False, 
#             'message': 'Missing tour_id or sched_id parameter'
#         })
#     
#     try:
#         # Get the tour and schedule
#         tour = get_object_or_404(Tour_Add, tour_id=tour_id)
#         schedule = get_object_or_404(Tour_Schedule, sched_id=sched_id)
#         
#         # Get admission rates for this tour
#         admission_rates = Admission_Rates.objects.filter(
#             tour_id=tour
#         ).order_by('age_group')
#         
#         # Format the admission rates for display
#         rates = []
#         for rate in admission_rates:
#             rates.append({
#                 'age_group': rate.age_group,
#                 'rate': rate.rate,
#                 'description': rate.description
#             })
#         
#         # Get any other payable items for this tour
#         # Add tour add-ons or additional costs here
#         
#         return JsonResponse({
#             'success': True,
#             'tour_name': tour.tour_name,
#             'base_price': schedule.price,
#             'admission_rates': rates,
#             # Add other payables here
#         })
#         
#     except Exception as e:
#         return JsonResponse({
#             'success': False,
#             'message': str(e)
#         })
# 
# @login_required
# def get_tour_itinerary(request):
#     """Get detailed itinerary information for a tour"""
#     tour_id = request.GET.get('tour_id')
#     sched_id = request.GET.get('sched_id')
#     
#     if not tour_id or not sched_id:
#         return JsonResponse({
#             'success': False, 
#             'message': 'Missing tour_id or sched_id parameter'
#         })
#     
#     try:
#         # Get the tour and schedule
#         tour = get_object_or_404(Tour_Add, tour_id=tour_id)
#         schedule = get_object_or_404(Tour_Schedule, sched_id=sched_id)
#         
#         # Get tour events for this schedule
#         tour_events = Tour_Event.objects.filter(
#             tour_id=tour,
#             sched_id=schedule
#         ).order_by('day_number', 'start_time')
#         
#         # Format the itinerary for display
#         days = {}
#         for event in tour_events:
#             day_number = event.day_number
#             if day_number not in days:
#                 days[day_number] = []
#             
#             days[day_number].append({
#                 'title': event.title,
#                 'description': event.description,
#                 'start_time': event.start_time.strftime('%I:%M %p') if event.start_time else None,
#                 'end_time': event.end_time.strftime('%I:%M %p') if event.end_time else None,
#                 'location': event.location,
#                 'notes': event.notes
#             })
#         
#         # Build HTML for the itinerary
#         itinerary_html = ''
#         for day_number in sorted(days.keys()):
#             itinerary_html += f'<div class="itinerary-day"><h4>Day {day_number}</h4><ul>'
#             for event in days[day_number]:
#                 time_display = ''
#                 if event['start_time']:
#                     time_display = event['start_time']
#                     if event['end_time']:
#                         time_display += f' - {event["end_time"]}'
#                         
#                 itinerary_html += f'<li><strong>{time_display}</strong> - {event["title"]}'
#                 if event['description']:
#                     itinerary_html += f'<br><span class="event-description">{event["description"]}</span>'
#                 itinerary_html += '</li>'
#             itinerary_html += '</ul></div>'
#         
#         if not itinerary_html:
#             itinerary_html = '<p>No detailed itinerary available for this tour.</p>'
#         
#         return JsonResponse({
#             'success': True,
#             'tour_name': tour.tour_name,
#             'itinerary_html': itinerary_html
#         })
#         
#     except Exception as e:
#         return JsonResponse({
#             'success': False,
#             'message': str(e)
#         })

@login_required
def companion_view(request):
    """View for managing companions"""
    # Get current language
    current_language = get_current_language(request)
    from .forms import CompanionForm
    from .models import Guest, GuestCredential, DisabilityDocument, CompanionGroup, CompanionRequest
    
    # Get existing companions for this user
    companions = Guest.objects.filter(made_by=request.user).select_related('group')
    
    # Get user's companion groups
    groups = CompanionGroup.objects.filter(owner=request.user)
    
    # Organize companions by group for better display
    organized_companions = {
        'no_group': [],
        'by_group': {}
    }
    
    # Initialize groups in the organized structure
    for group in groups:
        organized_companions['by_group'][group.id] = {
            'group': group,
            'companions': []
        }
    
    # Organize companions into their groups
    for companion in companions:
        if companion.group:
            # Add to appropriate group
            group_id = companion.group.id
            if group_id in organized_companions['by_group']:
                organized_companions['by_group'][group_id]['companions'].append(companion)
        else:
            # Add to "no group" list
            organized_companions['no_group'].append(companion)
    
    # Get all group members counts for display in a format that can be directly used in templates
    group_counts = {}
    for group in groups:
        # Use string keys for the dictionary to ensure it works in the template
        group_counts[str(group.id)] = companions.filter(group=group).count()
    
    # Get friend connections (users with accepted companion requests)
    sent_friend_requests = CompanionRequest.objects.filter(
        sender=request.user, 
        status='accepted'
    ).select_related('recipient', 'group')
    
    received_friend_requests = CompanionRequest.objects.filter(
        recipient=request.user, 
        status='accepted'
    ).select_related('sender', 'group')
    
    # Create a list of friend connections
    friends = []
    
    # Add recipients of accepted sent requests
    for req in sent_friend_requests:
        friends.append({
            'user': req.recipient,
            'request_id': req.id,
            'created_at': req.created_at,
            'direction': 'sent',
            'group': req.group
        })
    
    # Add senders of accepted received requests
    for req in received_friend_requests:
        friends.append({
            'user': req.sender,
            'request_id': req.id,
            'created_at': req.created_at,
            'direction': 'received',
            'group': req.group
        })
    
    # Sort friends by name
    friends.sort(key=lambda x: f"{x['user'].first_name} {x['user'].last_name}")
    
    # Create dictionary to track all groups, including those from connections
    all_groups = {}
    for group in groups:
        all_groups[group.id] = group
    
    # Collect any groups from friend connections that aren't user's own groups
    for friend in friends:
        if friend['group'] and friend['group'].id not in all_groups:
            all_groups[friend['group'].id] = friend['group']
            print(f"Added external group from connections: {friend['group'].name} (ID: {friend['group'].id})")
    
    # Debug the groups
    print(f"Found {len(all_groups)} total groups for organizing friends")
    for group_id, group in all_groups.items():
        print(f"Group: {group.name} (ID: {group_id})")
    
    # Organize friends by group for better display
    organized_friends = {
        'no_group': [],
        'by_group': {}
    }
    
    # Initialize all groups in the organized structure for friends
    for group_id, group in all_groups.items():
        organized_friends['by_group'][group_id] = {
            'group': group,
            'friends': []
        }
    
    # Organize friends into their groups
    for friend in friends:
        print(f"Processing friend: {friend['user'].first_name} with group: {friend['group'].name if friend['group'] else 'None'}")
        if friend['group']:
            # Add to appropriate group
            group_id = friend['group'].id
            if group_id in organized_friends['by_group']:
                organized_friends['by_group'][group_id]['friends'].append(friend)
                print(f"Added to group: {friend['group'].name}")
            else:
                # Create entry for this group if it doesn't exist
                organized_friends['by_group'][group_id] = {
                    'group': friend['group'],
                    'friends': [friend]
                }
                print(f"Created new group entry for: {friend['group'].name}")
        else:
            # Add to "no group" list
            organized_friends['no_group'].append(friend)
            print(f"Added to no_group list")
    
    # Handle group creation
    if request.method == 'POST' and 'create_group' in request.POST:
        group_name = request.POST.get('group_name')
        group_description = request.POST.get('group_description')
        
        if group_name:
            new_group = CompanionGroup.objects.create(
                name=group_name,
                description=group_description,
                owner=request.user
            )
            messages.success(request, f'Group "{group_name}" created successfully!')
            return redirect('companion')
    
    # Handle companion form submission
    elif request.method == 'POST':
        form = CompanionForm(request.POST, request.FILES)
        if form.is_valid():
            try:
                # Save the companion with the current user as made_by
                companion = form.save(commit=False)
                
                # Convert MM/DD/YY format to a proper date object
                birthday_str = form.cleaned_data.get('birthday')
                if birthday_str and isinstance(birthday_str, str) and '/' in birthday_str:
                    try:
                        # Parse MM/DD/YY format
                        from datetime import datetime
                        month, day, year = birthday_str.split('/')
                        # Assuming YY format, convert to 4-digit year (assuming 20xx for years less than 50)
                        if len(year) == 2:
                            year = f"20{year}" if int(year) < 50 else f"19{year}"
                        companion.birthday = datetime.strptime(f"{month}/{day}/{year}", "%m/%d/%Y").date()
                    except (ValueError, IndexError) as e:
                        # If parsing fails, try using the original value
                        print(f"Error parsing birthday: {e}")
                        companion.birthday = form.cleaned_data.get('birthday')
                else:
                    # Use the original value if not in MM/DD/YY format
                    companion.birthday = form.cleaned_data.get('birthday')
                
                # Handle disability fields
                companion.has_disability = form.cleaned_data.get('has_disability', False)
                if companion.has_disability:
                    companion.disability_type = form.cleaned_data.get('disability_type', '')
                
                # Set made_by field to current user
                companion.made_by = request.user
                
                # Assign to group if specified
                group_id = request.POST.get('companion_group')
                if group_id and group_id != 'none':
                    try:
                        group = CompanionGroup.objects.get(id=group_id, owner=request.user)
                        companion.group = group
                    except CompanionGroup.DoesNotExist:
                        pass  # Ignore if group doesn't exist or doesn't belong to user
                else:
                    companion.group = None
                
                # Save the companion to create the instance with an ID
                companion.save()
                
                # Process and save credentials (multiple files)
                credentials = request.FILES.getlist('credentials')
                for credential_file in credentials:
                    GuestCredential.objects.create(
                        guest=companion,
                        document=credential_file
                    )
                
                # Process and save disability documents if has_disability is checked
                if companion.has_disability:
                    disability_documents = form.cleaned_data.get('disability_documents')
                    if disability_documents:
                        # Handle both single file and list of files
                        if not isinstance(disability_documents, list):
                            disability_documents = [disability_documents]
                        
                        for doc_file in disability_documents:
                            DisabilityDocument.objects.create(
                                guest=companion,
                                document=doc_file
                            )
                
                messages.success(request, 'Companion added successfully!')
                
                # For AJAX requests, return JSON response
                if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                    return JsonResponse({
                        'success': True,
                        'message': 'Companion added successfully!'
                    })
                return redirect('companion')
            except Exception as e:
                messages.error(request, f'Error adding companion: {str(e)}')
        else:
            # Form is invalid
            messages.error(request, 'Please correct the errors below.')
    else:
        form = CompanionForm()
    
    # Get pending request count
    pending_request_count = CompanionRequest.objects.filter(
        recipient=request.user, status='pending'
    ).count()
    
    context = {
        'user': request.user,
        'companions': companions,  # Keep the original queryset for backward compatibility
        'organized_companions': organized_companions,  # New organized structure
        'groups': groups,
        'group_counts': group_counts,
        'friends': friends,
        'organized_friends': organized_friends,  # New organized friends structure
        'form': form,
        'pending_request_count': pending_request_count,
        'current_language': current_language,
        'translations_json': get_translations_json(current_language)
    }
    
    return render(request, 'companion.html', context)

@login_required
def edit_companion(request, companion_id):
    """View for editing a companion's information"""
    from .forms import CompanionForm
    from .models import Guest, GuestCredential, DisabilityDocument, CompanionGroup
    
    # Get the companion
    try:
        companion = Guest.objects.get(guest_id=companion_id)
        
        # Check if the user is the owner of this companion
        if companion.made_by != request.user:
            messages.error(request, "You don't have permission to edit this companion.")
            return redirect('companion')
        
        # Get user's companion groups
        groups = CompanionGroup.objects.filter(owner=request.user)
            
        # Handle form submission
        if request.method == 'POST':
            form = CompanionForm(request.POST, request.FILES, instance=companion)
            if form.is_valid():
                try:
                    # Save the updated companion
                    updated_companion = form.save(commit=False)
                    
                    # Ensure made_by field remains the same
                    updated_companion.made_by = request.user
                    
                    # Convert MM/DD/YY format to a proper date object
                    birthday_str = form.cleaned_data.get('birthday')
                    if birthday_str and isinstance(birthday_str, str) and '/' in birthday_str:
                        try:
                            # Parse MM/DD/YY format
                            from datetime import datetime
                            month, day, year = birthday_str.split('/')
                            # Assuming YY format, convert to 4-digit year (assuming 20xx for years less than 50)
                            if len(year) == 2:
                                year = f"20{year}" if int(year) < 50 else f"19{year}"
                            updated_companion.birthday = datetime.strptime(f"{month}/{day}/{year}", "%m/%d/%Y").date()
                        except (ValueError, IndexError) as e:
                            # If parsing fails, try using the original value
                            print(f"Error parsing birthday: {e}")
                            updated_companion.birthday = form.cleaned_data.get('birthday')
                    else:
                        # Use the original value if not in MM/DD/YY format
                        updated_companion.birthday = form.cleaned_data.get('birthday')
                    
                    updated_companion.has_disability = form.cleaned_data.get('has_disability', False)
                    if updated_companion.has_disability:
                        updated_companion.disability_type = form.cleaned_data.get('disability_type', '')
                    
                    # Update group assignment if specified
                    group_id = request.POST.get('companion_group')
                    if group_id == 'none':
                        updated_companion.group = None
                    elif group_id:
                        try:
                            group = CompanionGroup.objects.get(id=group_id, owner=request.user)
                            updated_companion.group = group
                        except CompanionGroup.DoesNotExist:
                            pass  # Ignore if group doesn't exist or doesn't belong to user
                    
                    # Save the updated companion
                    updated_companion.save()
                    
                    # Process new credentials if provided
                    credentials = request.FILES.getlist('credentials')
                    if credentials:
                        for credential_file in credentials:
                            GuestCredential.objects.create(
                                guest=updated_companion,
                                document=credential_file
                            )
                    
                    # Process new disability documents if provided
                    if updated_companion.has_disability:
                        disability_documents = form.cleaned_data.get('disability_documents')
                        if disability_documents:
                            if not isinstance(disability_documents, list):
                                disability_documents = [disability_documents]
                            
                            for doc_file in disability_documents:
                                DisabilityDocument.objects.create(
                                    guest=updated_companion,
                                    document=doc_file
                                )
                    
                    messages.success(request, 'Companion updated successfully!')
                    return redirect('companion')
                except Exception as e:
                    messages.error(request, f'Error updating companion: {str(e)}')
            else:
                messages.error(request, 'Please correct the errors below.')
        else:
            # Pre-fill the form with companion data
            form = CompanionForm(instance=companion)
        
        context = {
            'form': form,
            'companion': companion,
            'groups': groups,
            'editing': True,
            'current_language': get_current_language(request),
            'translations_json': get_translations_json(get_current_language(request))
        }
        
        return render(request, 'companion_edit.html', context)
        
    except Guest.DoesNotExist:
        messages.error(request, "Companion not found.")
        return redirect('companion')

@login_required
def manage_companion_groups(request):
    """View for managing companion groups"""
    from .models import CompanionGroup, Guest
    
    # Get user's groups
    groups = CompanionGroup.objects.filter(owner=request.user)
    
    if request.method == 'POST':
        # Handle group creation
        if 'create_group' in request.POST:
            group_name = request.POST.get('group_name')
            group_description = request.POST.get('group_description')
            
            if group_name:
                new_group = CompanionGroup.objects.create(
                    name=group_name,
                    description=group_description,
                    owner=request.user
                )
                messages.success(request, f'Group "{group_name}" created successfully!')
                
        # Handle group deletion
        elif 'delete_group' in request.POST:
            group_id = request.POST.get('group_id')
            try:
                group = CompanionGroup.objects.get(id=group_id, owner=request.user)
                group_name = group.name
                group.delete()
                messages.success(request, f'Group "{group_name}" deleted successfully!')
            except CompanionGroup.DoesNotExist:
                messages.error(request, "Group not found or you don't have permission to delete it.")
        
        # Handle group editing
        elif 'edit_group' in request.POST:
            group_id = request.POST.get('group_id')
            group_name = request.POST.get('group_name')
            group_description = request.POST.get('group_description')
            
            try:
                group = CompanionGroup.objects.get(id=group_id, owner=request.user)
                if group_name:
                    group.name = group_name
                if group_description is not None:  # Allow empty description
                    group.description = group_description
                group.save()
                messages.success(request, f'Group "{group_name}" updated successfully!')
            except CompanionGroup.DoesNotExist:
                messages.error(request, "Group not found or you don't have permission to edit it.")
        
        return redirect('manage_companion_groups')
    
    context = {
        'groups': groups,
        'companions_count': {
            group.id: Guest.objects.filter(group=group).count() 
            for group in groups
        },
        'current_language': get_current_language(request),
        'translations_json': get_translations_json(get_current_language(request))
    }
    
    return render(request, 'manage_companion_groups.html', context)

@login_required
@require_http_methods(["POST"])
def delete_companion(request, companion_id):
    """Handle companion deletion"""
    try:
        # Get the companion
        companion = get_object_or_404(Guest, guest_id=companion_id)
        
        # Check if the user is the owner of this companion
        if companion.made_by != request.user:
            return JsonResponse({
                'success': False,
                'message': "You don't have permission to delete this companion."
            }, status=403)
        
        # Delete the companion
        companion_name = f"{companion.first_name} {companion.last_name}"
        companion.delete()
        
        # Return success response
        return JsonResponse({
            'success': True,
            'message': f'Companion {companion_name} has been deleted successfully.'
        })
        
    except Guest.DoesNotExist:
        return JsonResponse({
            'success': False,
            'message': 'Companion not found.'
        }, status=404)
    except Exception as e:
        return JsonResponse({
            'success': False,
            'message': f'Error deleting companion: {str(e)}'
        }, status=400)

# Companion Request Views
@login_required
def search_users(request):
    """Search for users by email to send companion requests"""
    email = request.GET.get('email', '').strip()
    
    if not email:
        return JsonResponse({
            'success': False,
            'message': 'Please enter an email to search.'
        })
    
    try:
        # Find user by exact email match (for security reasons)
        # Ensure we only find regular users (not companions)
        user = Guest.objects.filter(
            email=email, 
            made_by__isnull=True  # This ensures we only get regular users, not companions
        ).first()
        
        if not user:
            return JsonResponse({
                'success': False,
                'message': 'No registered user found with this email address.'
            })
        
        # Don't allow searching for yourself
        if user == request.user:
            return JsonResponse({
                'success': False,
                'message': 'You cannot send a companion request to yourself.'
            })
            
        # Check if there's already a request between these users
        from .models import CompanionRequest
        
        # Check more specifically for the relationship direction
        # Only check if there's a sent request from current user to found user
        existing_sent_request = CompanionRequest.objects.filter(
            sender=request.user, recipient=user
        ).first()
        
        # Check if there's a received request from found user to current user
        existing_received_request = CompanionRequest.objects.filter(
            sender=user, recipient=request.user
        ).first()
        
        # Handle case of existing sent request
        if existing_sent_request:
            if existing_sent_request.status == 'pending':
                return JsonResponse({
                    'success': False,
                    'message': 'You have already sent a request to this user. Please wait for their response.'
                })
            elif existing_sent_request.status == 'accepted':
                return JsonResponse({
                    'success': False,
                    'message': 'You are already connected with this user.'
                })
            # If declined, we'll allow them to send a new request
        
        # Handle case of existing received request
        if existing_received_request:
            if existing_received_request.status == 'pending':
                return JsonResponse({
                    'success': False,
                    'message': 'This user has already sent you a request. Please check your companion requests.'
                })
            elif existing_received_request.status == 'accepted':
                return JsonResponse({
                    'success': False,
                    'message': 'You are already connected with this user.'
                })
            # If declined, we'll allow them to receive a new request
            
        # Check if the user is already a companion of the current user
        # More explicitly check for companion relationship with matching first/last name (not just email)
        is_companion = Guest.objects.filter(
            made_by=request.user, 
            email=email,
            first_name=user.first_name,
            last_name=user.last_name
        ).exists()
        
        if is_companion:
            return JsonResponse({
                'success': False,
                'message': 'This user is already in your companions list.'
            })
            
        # Return user info for confirmation
        picture_url = user.picture.url if user.picture else None
        return JsonResponse({
            'success': True,
            'user': {
                'guest_id': user.guest_id,
                'name': f"{user.first_name} {user.last_name}",
                'first_name': user.first_name,
                'last_name': user.last_name,
                'email': user.email,
                'picture': picture_url
            }
        })
        
    except Exception as e:
        return JsonResponse({
            'success': False,
            'message': f'Error searching for user: {str(e)}'
        })

@login_required
def debug_companion_requests(request):
    """Debug view to help troubleshoot companion request issues"""
    if not request.user.is_staff:
        messages.error(request, "You don't have permission to access this page.")
        return redirect('companion')
    
    email = request.GET.get('email', '').strip()
    results = {}
    
    if email:
        from .models import CompanionRequest
        try:
            # Find the user
            user = Guest.objects.filter(email=email).first()
            if user:
                results['user_found'] = {
                    'guest_id': user.guest_id,
                    'name': f"{user.first_name} {user.last_name}",
                    'email': user.email,
                    'is_companion': user.made_by is not None
                }
                
                # Check for sent requests
                sent_requests = CompanionRequest.objects.filter(
                    sender=request.user, recipient=user
                )
                results['sent_requests'] = [{
                    'id': req.id,
                    'status': req.status,
                    'created_at': req.created_at.strftime('%Y-%m-%d %H:%M:%S'),
                    'updated_at': req.updated_at.strftime('%Y-%m-%d %H:%M:%S')
                } for req in sent_requests]
                
                # Check for received requests
                received_requests = CompanionRequest.objects.filter(
                    sender=user, recipient=request.user
                )
                results['received_requests'] = [{
                    'id': req.id,
                    'status': req.status,
                    'created_at': req.created_at.strftime('%Y-%m-%d %H:%M:%S'),
                    'updated_at': req.updated_at.strftime('%Y-%m-%d %H:%M:%S')
                } for req in received_requests]
                
                # Check if the user is a companion of current user
                companion = Guest.objects.filter(
                    made_by=request.user,
                    email=email
                ).first()
                if companion:
                    results['is_companion'] = {
                        'guest_id': companion.guest_id,
                        'name': f"{companion.first_name} {companion.last_name}",
                        'email': companion.email
                    }
                else:
                    results['is_companion'] = False
            else:
                results['user_found'] = False
        except Exception as e:
            results['error'] = str(e)
    
    return JsonResponse(results)

@login_required
def send_companion_request(request):
    """Send a companion request to another user"""
    if request.method != 'POST':
        return JsonResponse({'success': False, 'message': 'Invalid request method.'})
    
    recipient_id = request.POST.get('recipient_id')
    message = request.POST.get('message', '')
    group_id = request.POST.get('group_id', '')
    
    print(f"Received companion request - recipient: {recipient_id}, group_id: {group_id}, message length: {len(message)}")
    
    if not recipient_id:
        return JsonResponse({'success': False, 'message': 'Recipient ID is required.'})
    
    try:
        from .models import CompanionRequest, CompanionGroup
        
        # Get the recipient
        recipient = get_object_or_404(Guest, guest_id=recipient_id)
        
        # Get the group if provided
        group = None
        if group_id:
            try:
                group = CompanionGroup.objects.get(id=group_id, owner=request.user)
                print(f"Found group for request: {group.name} (ID: {group.id})")
            except CompanionGroup.DoesNotExist:
                print(f"Group not found with ID: {group_id}")
                # Continue without group rather than failing
        
        # Don't allow sending requests to yourself
        if recipient == request.user:
            return JsonResponse({
                'success': False,
                'message': 'You cannot send a companion request to yourself.'
            })
        
        # Check for existing requests
        existing_request = CompanionRequest.objects.filter(
            sender=request.user, recipient=recipient
        ).first()
        
        if existing_request:
            if existing_request.status == 'pending':
                return JsonResponse({
                    'success': False,
                    'message': 'You have already sent a request to this user. Please wait for their response.'
                })
            elif existing_request.status == 'accepted':
                return JsonResponse({
                    'success': False,
                    'message': 'You are already connected with this user.'
                })
            else:  # declined
                # Allow sending a new request if the previous one was declined
                existing_request.status = 'pending'
                existing_request.message = message
                existing_request.group = group
                existing_request.save()
                
                group_msg = f" (will be added to group '{group.name}')" if group else ""
                return JsonResponse({
                    'success': True,
                    'message': f'Your companion request to {recipient.first_name} has been sent{group_msg}.'
                })
        
        # Create new request
        new_request = CompanionRequest.objects.create(
            sender=request.user,
            recipient=recipient,
            message=message,
            group=group
        )
        
        print(f"Created new companion request: ID {new_request.id} with group: {group.name if group else 'None'}")
        
        group_msg = f" (will be added to group '{group.name}')" if group else ""
        return JsonResponse({
            'success': True,
            'message': f'Your companion request to {recipient.first_name} has been sent{group_msg}.'
        })
        
    except Exception as e:
        import traceback
        print(f"Error sending companion request: {str(e)}")
        print(traceback.format_exc())
        return JsonResponse({
            'success': False,
            'message': f'Error sending companion request: {str(e)}'
        })

@login_required
def list_companion_requests(request):
    """List all pending companion requests for the current user"""
    from .models import CompanionRequest
    
    # Get received requests
    received_requests = CompanionRequest.objects.filter(
        recipient=request.user, status='pending'
    ).select_related('sender')
    
    # Get sent requests
    sent_requests = CompanionRequest.objects.filter(
        sender=request.user, status='pending'
    ).select_related('recipient')
    
    context = {
        'received_requests': received_requests,
        'sent_requests': sent_requests,
        'current_language': get_current_language(request),
        'translations_json': get_translations_json(get_current_language(request))
    }
    
    return render(request, 'companion_requests.html', context)

@login_required
def companion_request_count(request):
    """Get count of pending companion requests for the current user"""
    from .models import CompanionRequest
    
    count = CompanionRequest.objects.filter(
        recipient=request.user, status='pending'
    ).count()
    
    return JsonResponse({
        'success': True,
        'count': count
    })

@login_required
def accept_companion_request(request, request_id):
    """Accept a companion request"""
    try:
        from .models import CompanionRequest, Guest, CompanionGroup
        
        # Get the request
        companion_request = get_object_or_404(CompanionRequest, id=request_id, recipient=request.user)
        
        # Check if request is pending
        if companion_request.status != 'pending':
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return JsonResponse({
                    'success': False,
                    'message': "This request has already been processed."
                })
            messages.error(request, "This request has already been processed.")
            return redirect('list_companion_requests')
        
        # Get the group if specified in the request
        group = None
        print(f"Request {request_id} has group: {companion_request.group}")
        if companion_request.group:
            try:
                # First try to get a group with the same name owned by the recipient
                matching_groups = CompanionGroup.objects.filter(
                    owner=request.user, 
                    name=companion_request.group.name
                )
                
                if matching_groups.exists():
                    # Use existing group with same name if found
                    group = matching_groups.first()
                    print(f"Using existing group with matching name: {group.name} (ID: {group.id})")
                else:
                    # If no matching group found, look up by ID
                    group = CompanionGroup.objects.get(id=companion_request.group.id, owner=request.user)
                    print(f"Found group by ID: {group.name} (ID: {group.id})")
            except CompanionGroup.DoesNotExist:
                print(f"Group not found with ID: {companion_request.group.id}")
                
                # Create a new group with the same name if it doesn't exist
                sender_group = companion_request.group
                if sender_group:
                    group = CompanionGroup.objects.create(
                        name=f"{sender_group.name} (from {companion_request.sender.first_name})",
                        description=f"Group created from connection with {companion_request.sender.first_name} {companion_request.sender.last_name}",
                        owner=request.user
                    )
                    print(f"Created new group: {group.name} (ID: {group.id})")
        
        # Accept the request
        companion_request.accept()
        print(f"Accepted companion request {request_id}")
        
        # Create companion relationship
        sender = companion_request.sender
        recipient = request.user
        
        # Check if companion already exists with this email
        existing_companion = Guest.objects.filter(
            made_by=request.user,
            email=sender.email
        ).first()
        
        if not existing_companion:
            # Create a new companion record only if one doesn't exist
            # Generate a unique email for the companion to avoid duplicate entry errors
            import uuid
            unique_suffix = uuid.uuid4().hex[:8]
            companion_email = f"{sender.email.split('@')[0]}+companion{unique_suffix}@{sender.email.split('@')[1]}"
            
            new_companion = Guest(
                first_name=sender.first_name,
                last_name=sender.last_name,
                email=companion_email,  # Use the unique email
                phone_number=sender.phone_number if hasattr(sender, 'phone_number') else '',
                made_by=request.user,
                group=group  # Assign the group directly
            )
            new_companion.save()
            
            # Double-check that the group was assigned
            if group:
                print(f"Created new companion with group: {group.name}")
                # Explicitly update the group relation in case it wasn't set properly
                new_companion.group = group
                new_companion.save(update_fields=['group'])
                print(f"Verified companion group assignment: {new_companion.group and new_companion.group.name}")
            
            companion = new_companion
        elif group:
            # If companion already exists but a group was specified in the request, update their group
            existing_companion.group = group
            existing_companion.save(update_fields=['group'])
            print(f"Updated existing companion with group: {group.name}")
            companion = existing_companion
        else:
            companion = existing_companion
            
        # *** NEW CODE - IMPORTANT: Update the sender's side to show the recipient in the correct group ***
        # Check if the recipient already exists as a companion in the sender's list
        sender_companion = Guest.objects.filter(
            made_by=sender,
            email=recipient.email
        ).first()
        
        # Get the original group that was specified in the request (from sender's side)
        original_group = companion_request.group
        
        if not sender_companion:
            # Create a new companion record for the recipient in the sender's account
            # This ensures the sender sees the recipient as a companion
            unique_suffix = uuid.uuid4().hex[:8]
            recipient_email = f"{recipient.email.split('@')[0]}+companion{unique_suffix}@{recipient.email.split('@')[1]}"
            
            new_sender_companion = Guest(
                first_name=recipient.first_name,
                last_name=recipient.last_name,
                email=recipient_email,
                phone_number=recipient.phone_number if hasattr(recipient, 'phone_number') else '',
                made_by=sender,
                group=original_group  # Use the ORIGINAL group from the request
            )
            new_sender_companion.save()
            print(f"Created recipient companion in sender's list with group: {original_group.name if original_group else 'None'}")
        elif original_group:
            # If recipient already exists in sender's companions but the group needs updating
            sender_companion.group = original_group
            sender_companion.save(update_fields=['group'])
            print(f"Updated recipient in sender's companions with original group: {original_group.name}")
        
        # Update the CompanionRequest to keep the group association for both sides
        if group and companion_request.group != group:
            # Keep the original group in the request
            print(f"Keeping original group in the request: {companion_request.group.name if companion_request.group else 'None'}")
        
        # If this is an AJAX request
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            group_message = f" and added to group '{group.name}'" if group else ""
            return JsonResponse({
                'success': True,
                'message': f"You are now connected with {sender.first_name} {sender.last_name}{group_message}.",
                'companion_id': companion.guest_id,
                'group_id': group.id if group else None,
                'group_name': group.name if group else None
            })
            
        group_message = f" and added to group '{group.name}'" if group else ""
        messages.success(request, f"You are now connected with {sender.first_name} {sender.last_name}{group_message}.")
        return redirect('list_companion_requests')
        
    except Exception as e:
        import traceback
        print(f"Error accepting companion request: {str(e)}")
        print(traceback.format_exc())
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({
                'success': False,
                'message': f"Error accepting companion request: {str(e)}"
            })
        messages.error(request, f"Error accepting companion request: {str(e)}")
        return redirect('list_companion_requests')

@login_required
def decline_companion_request(request, request_id):
    """Decline a companion request"""
    from .models import CompanionRequest
    
    try:
        companion_request = get_object_or_404(
            CompanionRequest, id=request_id, recipient=request.user, status='pending'
        )
        
        sender_name = companion_request.sender.first_name
        companion_request.decline()
        
        # If this is an AJAX request
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({
                'success': True,
                'message': f'You have declined the companion request from {sender_name}.'
            })
        
        messages.success(request, f'You have declined the companion request from {sender_name}.')
        return redirect('list_companion_requests')
    
    except Exception as e:
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({
                'success': False,
                'message': f'Error declining companion request: {str(e)}'
            })
        
        messages.error(request, f'Error declining companion request: {str(e)}')
        return redirect('list_companion_requests')

@login_required
def fix_companion_request(request):
    """Admin function to fix problematic companion requests or remove friend connections"""
    if request.method != 'POST':
        return JsonResponse({
            'success': False,
            'message': 'Invalid request method.'
        }, status=405)
    
    # Get parameters
    email = request.POST.get('email', '').strip()
    action = request.POST.get('action', '')
    request_id = request.POST.get('request_id', None)
    delete_type = request.POST.get('delete_type', 'all')
    
    try:
        from .models import CompanionRequest, Guest
        
        # Allow normal users to delete specific requests (their own connections)
        if action == 'delete' and request_id and delete_type == 'specific':
            try:
                # Find the specific request
                request_obj = CompanionRequest.objects.get(id=request_id)
                
                # Only allow if user is a participant in this request
                if request.user == request_obj.sender or request.user == request_obj.recipient:
                    request_obj.delete()
                    return JsonResponse({
                        'success': True,
                        'message': 'Connection has been removed successfully.'
                    })
                else:
                    return JsonResponse({
                        'success': False,
                        'message': "You don't have permission to delete this connection."
                    }, status=403)
            except CompanionRequest.DoesNotExist:
                return JsonResponse({
                    'success': False,
                    'message': 'Connection not found.'
                }, status=404)
        
        # All other actions require staff permissions
        if not request.user.is_staff:
            return JsonResponse({
                'success': False,
                'message': "You don't have permission to access this feature."
            }, status=403)
        
        if not email or not action:
            return JsonResponse({
                'success': False,
                'message': 'Missing required parameters.'
            }, status=400)
        
        # Find the user
        user = Guest.objects.filter(email=email, made_by__isnull=True).first()
        if not user:
            return JsonResponse({
                'success': False,
                'message': 'User not found or is already a companion account.'
            }, status=404)
        
        # Handle different actions
        if action == 'delete':
            # Delete companion requests between these users
            if request_id:
                # Delete specific request
                request_obj = get_object_or_404(CompanionRequest, id=request_id)
                request_obj.delete()
                message = f"Companion request #{request_id} deleted."
            else:
                # Delete all requests between the users
                sent_count = CompanionRequest.objects.filter(
                    sender=request.user, recipient=user
                ).delete()[0]
                
                received_count = CompanionRequest.objects.filter(
                    sender=user, recipient=request.user
                ).delete()[0]
                
                message = f"Deleted {sent_count + received_count} companion requests."
            
        elif action == 'reset':
            # Reset companion request status to 'pending'
            if request_id:
                # Reset specific request
                request_obj = get_object_or_404(CompanionRequest, id=request_id)
                request_obj.status = 'pending'
                request_obj.save()
                message = f"Companion request #{request_id} reset to 'pending'."
            else:
                # Reset all requests between the users to pending
                sent_updated = 0
                for req in CompanionRequest.objects.filter(sender=request.user, recipient=user):
                    req.status = 'pending'
                    req.save()
                    sent_updated += 1
                
                received_updated = 0
                for req in CompanionRequest.objects.filter(sender=user, recipient=request.user):
                    req.status = 'pending'
                    req.save()
                    received_updated += 1
                
                message = f"Reset {sent_updated + received_updated} companion requests to 'pending'."
            
        elif action == 'create-companion':
            # Create a companion relationship directly
            # Check if companion already exists
            existing_companion = Guest.objects.filter(
                made_by=request.user,
                email=user.email
            ).first()
            
            if existing_companion:
                message = f"Companion already exists for {user.first_name} {user.last_name}."
            else:
                # Create new companion
                companion = Guest.objects.create(
                    first_name=user.first_name,
                    middle_initial=user.middle_initial,
                    last_name=user.last_name,
                    email=user.email,
                    phone_number=user.phone_number,
                    country_of_origin=user.country_of_origin,
                    city=user.city,
                    company_name=user.company_name,
                    sex=user.sex,
                    has_disability=user.has_disability,
                    disability_type=user.disability_type,
                    picture=user.picture,
                    made_by=request.user,
                    birthday=user.birthday
                )
                
                message = f"Created companion for {user.first_name} {user.last_name}."
        else:
            return JsonResponse({
                'success': False,
                'message': f"Unknown action: {action}"
            }, status=400)
        
        return JsonResponse({
            'success': True,
            'message': message
        })
    
    except Exception as e:
        return JsonResponse({
            'success': False,
            'message': f"Error: {str(e)}"
        }, status=500)

@login_required
def companion_group_debug(request):
    """Debug view for companion group relationships"""
    from .models import Guest, CompanionGroup, CompanionRequest, FriendGroup
    
    # Get data to debug
    user = request.user
    guest = Guest.objects.get(guest_id=user.guest_id)
    
    # Owned groups
    owned_groups = CompanionGroup.objects.filter(owner=guest)
    
    # Member of groups
    member_groups = CompanionGroup.objects.filter(members=guest)
    
    # Friend groups
    friend_groups = FriendGroup.objects.filter(members=guest)
    
    # Companion requests
    sent_requests = CompanionRequest.objects.filter(sender=guest)
    received_requests = CompanionRequest.objects.filter(recipient=guest)
    
    # Direct companions
    direct_companions = Guest.objects.filter(made_by=guest)
    
    context = {
        'user': user,
        'guest': guest,
        'owned_groups': owned_groups,
        'member_groups': member_groups,
        'friend_groups': friend_groups,
        'sent_requests': sent_requests,
        'received_requests': received_requests,
        'direct_companions': direct_companions,
    }
    
    return render(request, 'companion_group_debug.html', context)


@login_required
def friendship_debug(request):
    """Debug view to see friendship connections for the current user"""
    user = request.user
    
    # Get friendships for the current user
    try:
        friendships = Friendship.objects.filter(user=user).select_related('friend')
        friendship_data = []
        
        for friendship in friendships:
            friend = friendship.friend
            friendship_data.append({
                'friend_id': friend.guest_id,
                'friend_name': f"{friend.first_name} {friend.last_name}",
                'group': friendship.group_name,
                'created': friendship.created_at.strftime('%Y-%m-%d'),
            })
        
        # Get friendships data
        friend_count = len(friendship_data)
        group_counts = {}
        for item in friendship_data:
            group = item['group']
            if group not in group_counts:
                group_counts[group] = 0
            group_counts[group] += 1
        
        # Get data from legacy methods for comparison
        legacy_companions = get_companions_legacy(user)
        legacy_count = len(legacy_companions)
        
        # Create diagnostic result
        result = {
            'success': True,
            'user': f"{user.first_name} {user.last_name}",
            'friendship_count': friend_count,
            'groups': group_counts,
            'friendships': friendship_data,
            'legacy_count': legacy_count,
        }
        
        # Option to repopulate friendships
        if request.GET.get('repopulate') == 'true':
            from .utils import populate_friendships
            new_count = populate_friendships()
            result['repopulated'] = True
            result['new_friendship_count'] = new_count
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        result = {
            'success': False,
            'error': str(e)
        }
    
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse(result)
    else:
        # Render a debug template
        return render(request, 'friendship_debug.html', {
            'result': result,
            'result_json': json.dumps(result, indent=2),
        })

@login_required
def get_companions(request):
    """API endpoint to get companions for the current user"""
    try:
        user = request.user
        if not hasattr(user, 'guest_id'):
            return JsonResponse({'success': False, 'message': 'User is not associated with a guest profile'}, status=400)

        guest = Guest.objects.get(guest_id=user.guest_id)
        all_companions = []

        # Simple direct query using the new Friendship model
        try:
            # Get all friendships for this user
            friendships = Friendship.objects.filter(user=guest).select_related('friend')

            # Group by relationship type
            friendship_groups = {}
            for friendship in friendships:
                group_name = friendship.group_name
                if group_name not in friendship_groups:
                    friendship_groups[group_name] = []

                friend = friendship.friend
                friendship_groups[group_name].append({
                    'guest_id': friend.guest_id,
                    'first_name': friend.first_name,
                    'last_name': friend.last_name,
                    'age': friend.age,
                    'age_label': friend.age_label,
                    'group_name': group_name,
                    'picture_url': friend.picture.url if friend.picture else None
                })

            # Combine all groups into a single list
            for group_name, companions in friendship_groups.items():
                all_companions.extend(companions)

            print(f"Found {len(all_companions)} companions using Friendship model for guest {guest.guest_id}")

            # If no companions found in Friendship model, fall back to legacy methods
            if not all_companions:
                print("No companions found in Friendship model, attempting to populate...")
                from .utils import populate_friendships
                populate_friendships()
                # Try once more directly (no recursive request wrapper call)
                friendships = Friendship.objects.filter(user=guest).select_related('friend')
                for friendship in friendships:
                    friend = friendship.friend
                    all_companions.append({
                        'guest_id': friend.guest_id,
                        'first_name': friend.first_name,
                        'last_name': friend.last_name,
                        'age': friend.age,
                        'age_label': friend.age_label,
                        'group_name': friendship.group_name,
                        'picture_url': friend.picture.url if friend.picture else None
                    })

        except Exception as e:
            import traceback
            print(f"Error using Friendship model: {e}")
            traceback.print_exc()
            # If Friendship approach fails, fall back to legacy method
            all_companions = get_companions_legacy(guest)

        return JsonResponse({
            'success': True,
            'companions': all_companions
        })
    except Exception as e:
        import traceback
        print(f"Error in get_companions: {e}")
        traceback.print_exc()
        return JsonResponse({'success': False, 'message': str(e)}, status=500)

def get_companions_legacy(guest):
    """Legacy method to get companions from various relationship sources"""
    print(f"Using legacy companion fetching for guest {guest.guest_id}")
    all_companions = []
    
    # Get companions added directly by the user
    try:
        direct_companions = Guest.objects.filter(made_by=guest).select_related('group')
        for companion in direct_companions:
            all_companions.append({
                'guest_id': companion.guest_id,
                'first_name': companion.first_name,
                'last_name': companion.last_name,
                'age': companion.age,
                'age_label': companion.age_label,
                'group_name': 'Personal Companions',
                'picture_url': companion.picture.url if companion.picture else None
            })
    except Exception as e:
        print(f"Error fetching direct companions: {e}")
    
    # Get companions from family group
    try:
        if hasattr(guest, 'family') and guest.family:
            family_members = Guest.objects.filter(family=guest.family).exclude(guest_id=guest.guest_id)
            for member in family_members:
                all_companions.append({
                    'guest_id': member.guest_id,
                    'first_name': member.first_name,
                    'last_name': member.last_name,
                    'age': member.age,
                    'age_label': member.age_label,
                    'group_name': 'Family',
                    'picture_url': member.picture.url if member.picture else None
                })
    except Exception as e:
        print(f"Error fetching family companions: {e}")
    
    # Get companions from friend groups
    try:
        friend_groups = FriendGroup.objects.filter(members=guest)
        for group in friend_groups:
            members = group.members.all().exclude(guest_id=guest.guest_id)
            for member in members:
                all_companions.append({
                    'guest_id': member.guest_id,
                    'first_name': member.first_name,
                    'last_name': member.last_name,
                    'age': member.age,
                    'age_label': member.age_label,
                    'group_name': group.name,
                    'picture_url': member.picture.url if member.picture else None
                })
    except Exception as e:
        print(f"Error fetching friend group companions: {e}")
    
    print(f"Found {len(all_companions)} companions using legacy method")
    return all_companions

# Add a new URL mapping in urls.py:
# path('get_companions/', views.get_companions, name='get_companions'),

@login_required
@require_http_methods(["POST"])
def send_companion_qr_code(request):
    """
    Generate a QR code with user and companion information, and send it to the user's email.
    """
    try:
        # Parse request data
        try:
            data = json.loads(request.body)
            include_companions = data.get('include_companions', True)
            debug_mode = data.get('debug_mode', False)
            refresh_data = data.get('refresh_data', False)
        except json.JSONDecodeError as e:
            return JsonResponse({
                'success': False,
                'error': f"Invalid JSON in request: {str(e)}"
            }, status=400)
        
        # Get current user data
        user = request.user
        
        # Safely extract user data based on what kind of object it is
        if hasattr(user, 'guest_id'):
            # User is a Guest object
            user_data = {
                'id': user.guest_id,
                'email': user.email if hasattr(user, 'email') else '',
                'first_name': user.first_name if hasattr(user, 'first_name') else '',
                'last_name': user.last_name if hasattr(user, 'last_name') else '',
                'phone_number': user.phone_number if hasattr(user, 'phone_number') else '',
            }
            if hasattr(user, 'username'):
                user_data['username'] = user.username
        else:
            # User is a standard Django User object
            user_data = {
                'id': user.id,
                'username': user.username,
                'email': user.email,
                'first_name': user.first_name,
                'last_name': user.last_name,
                'phone_number': getattr(user, 'phone_number', ''),
            }
        
        # Include companion data if requested
        companion_data = []
        if include_companions:
            try:
                # Get all companions for this user
                companions = Guest.objects.filter(made_by=user)
                
                if debug_mode:
                    print(f"Found {companions.count()} companions for user {user.username}")
                
                for companion in companions:
                    try:
                        # Safely extract basic companion information
                        companion_info = {}
                        
                        # Check each attribute exists before accessing
                        if hasattr(companion, 'guest_id'):
                            companion_info['id'] = companion.guest_id
                        else:
                            # Fall back to primary key if guest_id doesn't exist
                            companion_info['id'] = companion.pk
                            
                        # Extract other basic fields
                        for field in ['first_name', 'last_name', 'email', 'phone_number']:
                            if hasattr(companion, field):
                                companion_info[field] = getattr(companion, field)
                            else:
                                companion_info[field] = f"No {field}"
                        
                        # Add group information - carefully handle the relationship
                        try:
                            if hasattr(companion, 'group'):
                                group = getattr(companion, 'group')
                                if group is not None and hasattr(group, 'name'):
                                    companion_info['group'] = group.name
                                else:
                                    companion_info['group'] = 'No Group'
                            else:
                                companion_info['group'] = 'No Group'
                        except Exception as ge:
                            companion_info['group'] = 'No Group'
                            if debug_mode:
                                print(f"Error getting group: {str(ge)}")
                        
                        companion_data.append(companion_info)
                        
                        if debug_mode:
                            print(f"Processed companion: {companion_info}")
                            
                    except Exception as ce:
                        if debug_mode:
                            print(f"Error processing individual companion: {str(ce)}")
                            print(f"Companion object: {companion}")
                            print(f"Available attributes: {dir(companion)}")
            except Exception as ce:
                return JsonResponse({
                    'success': False,
                    'error': f"Error processing companions: {str(ce)}"
                }, status=500)
                
        user_data['companions'] = companion_data
        
        # Convert the data to JSON string
        try:
            json_data = json.dumps(user_data, indent=2)
        except Exception as je:
            return JsonResponse({
                'success': False,
                'error': f"Error converting data to JSON: {str(je)}"
            }, status=500)
        
        # Generate QR code
        try:
            qr = qrcode.QRCode(
                version=2,  # Lower version for simpler code
                error_correction=qrcode.constants.ERROR_CORRECT_L,  # Low error correction for simplicity
                box_size=10,  # Smaller box size for a more compact code
                border=4,   # Standard border
            )
            qr.add_data(json_data)
            qr.make(fit=True)
            
            # Create simple black and white QR code
            img = qr.make_image(fill_color="black", back_color="white")
            
            # Save the QR code to a BytesIO object
            buffer = BytesIO()
            img.save(buffer, format="PNG")
            buffer.seek(0)
            
        except Exception as qe:
            return JsonResponse({
                'success': False,
                'error': f"Error generating QR code: {str(qe)}"
            }, status=500)
        
        # Create and send email
        try:
            from django.core.mail import EmailMessage
            from django.template.loader import render_to_string
            
            # Email subject and message
            subject = "Your Companion Management QR Code"
            html_message = render_to_string('email/qr_code_email.html', {
                'user': user,
                'companion_count': len(companion_data),
                'date': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            })
            
            # Create and send email
            email = EmailMessage(
                subject=subject,
                body=html_message,
                from_email=settings.DEFAULT_FROM_EMAIL,
                to=[user.email],
            )
            email.content_subtype = "html"  # Set the email to be HTML
            
            # Attach the QR code as a file
            email.attach('companion_qr_code.png', buffer.getvalue(), 'image/png')
            
            email.send()
        except Exception as ee:
            return JsonResponse({
                'success': False,
                'error': f"Error sending email: {str(ee)}"
            }, status=500)
        
        return JsonResponse({
            'success': True,
            'message': 'QR code has been sent to your email address.'
        })
        
    except Exception as e:
        import traceback
        error_traceback = traceback.format_exc()
        print(f"Error in send_companion_qr_code: {str(e)}")
        print(error_traceback)
        return JsonResponse({
            'success': False,
            'error': f"Unexpected error: {str(e)}"
        }, status=500)

# Add this to urls.py: path('companion/qrcode/', views.send_companion_qr_code, name='companion_qr_code'),

@login_required
def debug_guest_model(request):
    """
    Debug view to inspect the Guest model structure.
    """
    try:
        user = request.user
        companions = Guest.objects.filter(made_by=user)
        
        debug_info = {
            'guest_model_fields': [],
            'guest_instances': []
        }
        
        # Get model fields
        if companions.exists():
            first_companion = companions.first()
            debug_info['guest_model_fields'] = [field.name for field in first_companion._meta.fields]
            
            # Get instance data for some companions
            for companion in companions[:5]:  # Limit to 5 to avoid overwhelming output
                companion_data = {
                    'repr': str(companion),
                    'attributes': {}
                }
                
                # Get all available attributes
                for field in first_companion._meta.fields:
                    field_name = field.name
                    try:
                        value = getattr(companion, field_name)
                        companion_data['attributes'][field_name] = str(value)
                    except Exception as e:
                        companion_data['attributes'][field_name] = f"Error: {str(e)}"
                
                debug_info['guest_instances'].append(companion_data)
        
        return JsonResponse({
            'success': True,
            'debug_info': debug_info
        })
    except Exception as e:
        import traceback
        error_traceback = traceback.format_exc()
        print(f"Error in debug_guest_model: {str(e)}")
        print(error_traceback)
    return JsonResponse({
        'success': False,
        'error': str(e),
        'traceback': error_traceback
    }, status=500)

# Add to urls.py: path('debug/guest_model/', views.debug_guest_model, name='debug_guest_model'),


def accommodation_page(request):
    accommodations = list(_approved_accommodation_queryset().order_by("company_name"))
    review_summaries = _get_accommodation_review_summaries(
        [getattr(accom, "accom_id", None) for accom in accommodations]
    )
    listing_rows = []
    for accom in accommodations:
        links = _resolve_accommodation_links(accom)
        room = (
            AdminRoom.objects.filter(accommodation=accom, status="AVAILABLE")
            .order_by("price_per_night", "room_id")
            .first()
        )
        rating_label = _format_accommodation_rating_label(
            review_summaries.get(getattr(accom, "accom_id", None))
        )
        listing_rows.append(
            {
                "accommodation": accom,
                "room_name": str(getattr(room, "room_name", "") or "").strip() if room else "",
                "price_per_night": getattr(room, "price_per_night", None) if room else None,
                "person_limit": getattr(room, "person_limit", None) if room else None,
                "rating_label": rating_label,
                "official_link": links.get("facebook_url") or links.get("provider_url") or links.get("email_link") or links.get("phone_link") or "",
                "official_page_url": links.get("provider_url") or "",
                "provider_url": links.get("provider_url") or "",
                "contact_channel_url": links.get("provider_url") or links.get("email_link") or links.get("phone_link") or "",
                "facebook_url": links.get("facebook_url") or "",
                "has_verified_provider_link": links.get("has_verified_provider_link", False),
                "suppressed_third_party_booking_url": links.get("suppressed_third_party_booking_url", False),
                "image_url": _resolve_accommodation_image_url(accom),
                "phone_link": links.get("phone_link") or "",
                "email_link": links.get("email_link") or "",
            }
        )
    return render(request, "accommodation_book.html", {
        "listing_rows": listing_rows,
        "accommodation_transactions_disabled": True,
    })


def accommodation_detail_page(request, accom_id):
    accommodation = get_object_or_404(_approved_accommodation_queryset(), accom_id=accom_id)
    links = _resolve_accommodation_links(accommodation)
    review_summary = _get_accommodation_review_summaries([accommodation.accom_id]).get(accommodation.accom_id)
    approved_reviews = (
        AccommodationReview.objects.select_related("guest")
        .filter(accommodation=accommodation, status="approved")
        .order_by("-created_at")[:20]
    )
    review_rows = []
    for review in approved_reviews:
        guest = getattr(review, "guest", None)
        display_name = str(getattr(guest, "first_name", "") or "").strip() or "Verified Guest"
        review_rows.append(
            {
                "rating": review.rating,
                "comment": str(review.comment or "").strip(),
                "display_name": display_name,
                "created_at": review.created_at,
            }
        )
    user_review = None
    if request.user.is_authenticated:
        user_review = AccommodationReview.objects.filter(
            accommodation=accommodation,
            guest=request.user,
        ).first()

    image_url = _resolve_accommodation_image_url(accommodation)
    gallery_images = []
    seen_gallery_urls = set()
    if image_url:
        gallery_images.append(
            {
                "url": image_url,
                "label": str(accommodation.company_name or "Accommodation"),
            }
        )
        seen_gallery_urls.add(str(image_url).strip().lower())

    for cert in AccommodationCertification.objects.filter(accommodation=accommodation).order_by("-uploaded_at", "-id")[:8]:
        cert_image = ""
        try:
            cert_image = str(getattr(cert.image, "url", "") or "").strip()
        except Exception:
            cert_image = ""
        if not cert_image:
            continue
        normalized_url = cert_image.lower()
        if normalized_url in seen_gallery_urls:
            continue
        seen_gallery_urls.add(normalized_url)
        gallery_images.append(
            {
                "url": cert_image,
                "label": f"{str(accommodation.company_name or 'Accommodation').strip()} image",
            }
        )

    rooms_qs = (
        AdminRoom.objects.select_related("accommodation")
        .filter(accommodation=accommodation)
        .order_by("price_per_night", "room_name", "room_id")
    )
    room_rows = []
    lowest_rate = None
    for room in rooms_qs:
        room_rate = getattr(room, "price_per_night", None)
        if room_rate is not None:
            try:
                numeric_rate = Decimal(str(room_rate))
                if lowest_rate is None or numeric_rate < lowest_rate:
                    lowest_rate = numeric_rate
            except Exception:
                pass
        room_rows.append(
            {
                "room_name": str(getattr(room, "room_name", "") or "").strip() or "Room",
                "price_per_night": room_rate,
                "person_limit": getattr(room, "person_limit", None),
                "status": str(getattr(room, "status", "") or "").strip().title() or "Available",
                "description": str(getattr(room, "description", "") or "").strip(),
                "image_url": _resolve_room_image_url(room, fallback_url=image_url),
            }
        )

    amenities = [
        part.strip()
        for part in str(getattr(accommodation, "accommodation_amenities", "") or "").replace(";", ",").split(",")
        if part.strip()
    ]

    context = {
        "accommodation": accommodation,
        "gallery_images": gallery_images,
        "official_page_url": links.get("provider_url") or "",
        "provider_url": links.get("provider_url") or "",
        "contact_channel_url": links.get("provider_url") or links.get("email_link") or links.get("phone_link") or "",
        "facebook_url": links.get("facebook_url") or "",
        "has_verified_provider_link": links.get("has_verified_provider_link", False),
        "suppressed_third_party_booking_url": links.get("suppressed_third_party_booking_url", False),
        "room_rows": room_rows,
        "amenities": amenities[:20],
        "lowest_rate": lowest_rate,
        "rating_label": _format_accommodation_rating_label(review_summary),
        "review_rows": review_rows,
        "user_review": user_review,
        "recaptcha_site_key": str(getattr(settings, 'RECAPTCHA_SITE_KEY', '') or '').strip(),
        "recaptcha_required": _is_recaptcha_required(),
    }
    return render(request, "accommodation_detail.html", context)


@login_required
@require_POST
def submit_accommodation_review(request, accom_id):
    accommodation = get_object_or_404(_approved_accommodation_queryset(), accom_id=accom_id)
    try:
        rating = int(str(request.POST.get("rating") or "").strip())
    except (TypeError, ValueError):
        rating = 0
    comment = str(request.POST.get("comment") or "").strip()[:1500]

    if rating < 1 or rating > 5:
        messages.error(request, "Please choose a rating from 1 to 5 stars.")
        return redirect("accommodation_detail_page", accom_id=accommodation.accom_id)

    defaults = {
        "rating": rating,
        "comment": comment,
        "status": "pending",
        "moderation_notes": "",
        "reviewed_at": None,
        "reviewed_by": None,
    }
    try:
        AccommodationReview.objects.update_or_create(
            accommodation=accommodation,
            guest=request.user,
            defaults=defaults,
        )
        messages.success(request, "Thank you. Your review has been submitted for review.")
    except IntegrityError:
        messages.info(request, "You already submitted a review for this accommodation.")
    return redirect("accommodation_detail_page", accom_id=accommodation.accom_id)


@login_required
@guest_tourist_required
def my_accommodation_bookings(request):
    accommodations = (
        _approved_accommodation_queryset()
        .order_by("company_name")
    )
    listing_rows = [
        {
            "accommodation": accom,
            "official_link": _resolve_accommodation_outbound_link(accom),
            "image_url": _resolve_accommodation_image_url(accom),
        }
        for accom in accommodations
    ]
    return render(
        request,
        "my_accommodation_bookings.html",
        {
            "listing_rows": listing_rows,
            "accommodation_transactions_disabled": True,
        },
    )


@login_required
@guest_tourist_required
def my_tour_bookings(request):
    # Guest tour bookings are surfaced in the main page booking section.
    return redirect(f"{reverse('main-page')}#myBookings")


@login_required
@guest_tourist_required
@require_POST
def cancel_my_accommodation_booking(request, booking_id):
    messages.info(
        request,
        "Accommodation transactions are disabled. Please use official accommodation pages.",
    )
    return redirect("my_accommodation_bookings")


@login_required
@guest_tourist_required
@require_http_methods(["POST"])
def accommodation_recommend(request):
    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        payload = request.POST

    params = {
        "guests": payload.get("guests"),
        "budget": payload.get("budget"),
        "location": payload.get("location"),
        "company_type": payload.get("company_type"),
    }

    results = recommend_accommodations(params, limit=5)
    data = [
        {
            "title": item.title,
            "subtitle": item.subtitle,
            "score": item.score,
            "meta": item.meta,
            "official_link": str(
                (item.meta or {}).get("official_booking_url")
                or (item.meta or {}).get("official_contact_url")
                or ""
            ).strip(),
        }
        for item in results
    ]
    return JsonResponse({"success": True, "results": data})


@login_required
@guest_tourist_required
@require_http_methods(["POST"])
def accommodation_billing(request):
    return JsonResponse(_accommodation_transaction_disabled_payload(), status=410)


@login_required
@guest_tourist_required
@require_http_methods(["POST"])
def accommodation_book(request):
    return JsonResponse(_accommodation_transaction_disabled_payload(), status=410)


def _parse_decimal_amount(raw_value, *, default=None):
    if raw_value in (None, ""):
        return default
    try:
        return Decimal(str(raw_value))
    except (InvalidOperation, TypeError, ValueError):
        return default


def _normalize_payment_method(raw_value):
    value = str(raw_value or "").strip().lower()
    allowed = {choice[0] for choice in Billing.PAYMENT_METHOD_CHOICES}
    if value in allowed:
        return value
    if value:
        return "other"
    return ""


def _derive_payment_status(*, total_amount, amount_paid, explicit_status):
    valid_statuses = {"unpaid", "partial", "paid"}
    if isinstance(amount_paid, Decimal):
        if amount_paid <= Decimal("0"):
            return "unpaid"
        if amount_paid >= total_amount:
            return "paid"
        return "partial"

    explicit = str(explicit_status or "").strip().lower()
    if explicit in valid_statuses:
        return explicit
    return "unpaid"


@csrf_exempt
@require_POST
def payment_webhook_callback(request):
    """
    Receives payment callbacks from external LGU payment system.

    Expected auth header:
      X-Payment-Signature: hex(HMAC_SHA256(raw_body, PAYMENT_WEBHOOK_SECRET))

    Payload (JSON preferred, form-encoded also accepted):
      booking_reference: AB-<booking_id> (preferred) OR booking_id: <int>
      payment_status: unpaid|partial|paid (optional if amount_paid is provided)
      amount_paid: decimal (optional)
      payment_method: cash|gcash|bank_transfer|card|other (optional)
    """
    return JsonResponse(
        {
            "status": "disabled",
            "error": "accommodation_transaction_disabled",
            "message": (
                "Accommodation webhook endpoint is inactive because accommodation "
                "transactions were decommissioned."
            ),
        },
        status=410,
    )


@require_http_methods(["GET"])
def guest_service_worker(request):
    """
    Serve the guest PWA service worker from /guest_app/ so its scope stays on
    public guest pages and avoids broad caching of admin or private areas.
    """
    sw_path = settings.BASE_DIR / "static" / "pwa" / "guest-service-worker.js"
    try:
        content = sw_path.read_text(encoding="utf-8")
    except OSError:
        content = (
            'self.addEventListener("fetch", function () {'
            '  /* service worker file unavailable */'
            '});'
        )
    response = HttpResponse(content, content_type="application/javascript")
    response["Service-Worker-Allowed"] = "/guest_app/"
    response["Cache-Control"] = "no-cache"
    return response


@require_http_methods(["GET"])
def guest_manifest(request):
    manifest_path = settings.BASE_DIR / "static" / "pwa" / "manifest.webmanifest"
    try:
        content = manifest_path.read_text(encoding="utf-8")
    except OSError:
        content = json.dumps(
            {
                "name": "Ibayaw Tour",
                "short_name": "Ibayaw",
                "start_url": "/guest_app/main-page/",
                "scope": "/guest_app/",
                "display": "standalone",
                "theme_color": "#12335e",
                "background_color": "#ffffff",
            }
        )
    return HttpResponse(content, content_type="application/manifest+json")


@require_http_methods(["GET"])
def guest_offline(request):
    offline_path = settings.BASE_DIR / "static" / "pwa" / "offline.html"
    try:
        content = offline_path.read_text(encoding="utf-8")
    except OSError:
        content = "<h1>You are offline</h1><p>Please reconnect to use Ibayaw Tour.</p>"
    return HttpResponse(content, content_type="text/html")
