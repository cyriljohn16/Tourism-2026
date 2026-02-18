import json
import os
import re
from decimal import Decimal

from django.db.models import F, Sum
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

try:
    from openai import OpenAI
except ModuleNotFoundError:
    OpenAI = None

from tour_app.models import Admission_Rates, Tour_Schedule
from admin_app.models import Room
from .recommenders import (
    recommend_tours,
    recommend_accommodations,
    calculate_accommodation_billing,
)


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


def _resolve_guests(params):
    guests = _to_int(params.get("guests"), default=0)
    if guests > 0:
        return guests

    adults = _to_int(params.get("adults"), default=0)
    children = _to_int(params.get("children"), default=0)
    total = adults + children
    return total if total > 0 else 1


def _get_recommendations(params):
    results = recommend_tours(params, limit=3)
    if not results:
        return (
            "I couldn't find a matching tour right now. "
            "Try increasing budget or changing preferred tour type."
        )

    lines = ["Top recommendations for you (CNN + Decision Tree):"]
    for idx, item in enumerate(results, 1):
        lines.append(f"{idx}. {item.title} | {item.subtitle}")
    return "\n".join(lines)


def _get_accommodation_recommendations(params):
    results = recommend_accommodations(params, limit=3)
    if not results:
        return (
            "I couldn't find a matching hotel or inn right now. "
            "Try adjusting budget, guests, or location."
        )

    lines = ["Top hotel/inn recommendations for you (CNN + Decision Tree):"]
    for idx, item in enumerate(results, 1):
        lines.append(f"{idx}. {item.title} | {item.subtitle}")
    return "\n".join(lines)


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


def _calculate_accommodation_billing(params):
    room_id = params.get("room_id")
    check_in = params.get("check_in")
    check_out = params.get("check_out")
    nights = _to_int(params.get("nights"), default=0)

    room = None
    if room_id:
        room = Room.objects.filter(room_id=room_id).select_related("accommodation").first()
    if room is None and params.get("accom_name"):
        room = (
            Room.objects.select_related("accommodation")
            .filter(accommodation__company_name__icontains=params.get("accom_name"))
            .first()
        )

    if room is None:
        return "I couldn't find that room. Please provide a valid room ID or accommodation name."

    if check_in and check_out:
        try:
            from datetime import datetime
            check_in_dt = datetime.strptime(check_in, "%Y-%m-%d").date()
            check_out_dt = datetime.strptime(check_out, "%Y-%m-%d").date()
            total = calculate_accommodation_billing(room, check_in_dt, check_out_dt)
            nights = max((check_out_dt - check_in_dt).days, 1)
        except Exception:
            return "Please provide dates in YYYY-MM-DD format for check-in and check-out."
    else:
        nights = max(nights, 1)
        total = Decimal(room.price_per_night) * Decimal(nights)

    return (
        f"Billing Summary for {room.accommodation.company_name} - {room.room_name}:\n"
        f"Nights: {nights}\n"
        f"Rate: PHP {room.price_per_night} per night\n"
        f"Total amount due: PHP {total:.2f}"
    )


def _extract_params_from_message(message):
    text = (message or "").strip().lower()
    params = {}

    # Extract schedule ID like Sched00001.
    sched_match = re.search(r"(sched\d+)", text, flags=re.IGNORECASE)
    if sched_match:
        params["sched_id"] = sched_match.group(1)

    # Extract guest count from "<n> guest(s)/people/person/pax".
    guest_match = re.search(r"(\d+)\s*(guest|guests|people|person|pax)", text)
    if guest_match:
        params["guests"] = int(guest_match.group(1))
    else:
        # Fallback: first number in message can be treated as guests.
        first_num = re.search(r"\b(\d+)\b", text)
        if first_num:
            params["guests"] = int(first_num.group(1))

    # Extract budget from "budget 1500", "under 1500", "below 1500".
    budget_match = re.search(r"(budget|under|below|less than)\s*[:\-]?\s*(\d+)", text)
    if budget_match:
        params["budget"] = int(budget_match.group(2))

    # Extract duration from "<n> day(s)".
    duration_match = re.search(r"(\d+)\s*day", text)
    if duration_match:
        params["duration_days"] = int(duration_match.group(1))

    # Extract nights from "<n> night(s)".
    nights_match = re.search(r"(\d+)\s*night", text)
    if nights_match:
        params["nights"] = int(nights_match.group(1))

    # Extract location from "in <location>"
    loc_match = re.search(r"\bin\s+([a-z\s]+)$", text)
    if loc_match:
        params["location"] = loc_match.group(1).strip()

    # Extract room_id like Room0001 or just room 12
    room_match = re.search(r"(room\\s*(\\d+))", text)
    if room_match:
        params["room_id"] = room_match.group(2)

    # Extract check-in/check-out dates in YYYY-MM-DD
    date_matches = re.findall(r"(\\d{4}-\\d{2}-\\d{2})", text)
    if len(date_matches) >= 2:
        params["check_in"] = date_matches[0]
        params["check_out"] = date_matches[1]

    if "hotel" in text or "inn" in text or "accommodation" in text:
        params.setdefault("company_type", "hotel")

    # Basic keyword preference extraction.
    for keyword in ["river", "mountain", "sea", "sunset", "forest"]:
        if keyword in text:
            params["preference"] = keyword
            break

    return params


def _intent_from_message(message):
    text = (message or "").lower()
    billing_keywords = [
        "bill",
        "billing",
        "total",
        "price",
        "cost",
        "how much",
        "amount due",
    ]
    accommodation_keywords = ["hotel", "inn", "room", "accommodation"]
    if any(keyword in text for keyword in billing_keywords):
        if any(keyword in text for keyword in accommodation_keywords):
            return "calculate_accommodation_billing"
        return "calculate_billing"
    if any(keyword in text for keyword in accommodation_keywords):
        return "get_accommodation_recommendation"
    return "get_recommendation"


def _normalize_openai_output(raw_payload):
    default = {"intent": "get_recommendation", "params": {}}
    if not raw_payload:
        return default

    try:
        parsed = json.loads(raw_payload)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw_payload, flags=re.DOTALL)
        if not match:
            return default
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return default

    intent = (
        parsed.get("intent")
        or parsed.get("action")
        or parsed.get("operation")
        or "get_recommendation"
    )
    params = parsed.get("params") or parsed.get("parameters") or {}
    if not isinstance(params, dict):
        params = {}
    return {"intent": intent, "params": params}


def _openai_extract_intent_and_params(message):
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key or OpenAI is None:
        return {
            "intent": _intent_from_message(message),
            "params": _extract_params_from_message(message),
            "source": "heuristic_no_api_key_or_sdk",
        }

    model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    system_prompt = (
        "You classify tourism chatbot messages into intent and parameters.\n"
        "Return JSON only with keys: intent, params.\n"
        "Allowed intent values: get_recommendation, calculate_billing, "
        "get_accommodation_recommendation, calculate_accommodation_billing.\n"
        "For params, extract if present: guests, adults, children, budget, "
        "duration_days, preference, sched_id, tour_name, location, "
        "company_type, room_id, accom_name, check_in, check_out, nights.\n"
        "Do not include any text outside JSON."
    )

    try:
        client = OpenAI(api_key=api_key)
        completion = client.chat.completions.create(
            model=model,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": message},
            ],
        )
        content = (completion.choices[0].message.content or "").strip()
        parsed = _normalize_openai_output(content)
        return {
            "intent": parsed["intent"],
            "params": parsed["params"],
            "source": "openai",
        }
    except Exception:
        return {
            "intent": _intent_from_message(message),
            "params": _extract_params_from_message(message),
            "source": "heuristic_fallback",
        }


@csrf_exempt
def openai_chat(request):
    if request.method != "POST":
        return JsonResponse({"status": "ok"})

    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"fulfillmentText": "Invalid JSON payload."}, status=400)

    message = str(payload.get("message", "")).strip()
    if not message:
        return JsonResponse(
            {"fulfillmentText": "Please send a message in this format: {\"message\": \"...\"}."},
            status=400,
        )

    parsed = _openai_extract_intent_and_params(message)
    intent = str(parsed["intent"]).strip().lower()
    params = parsed["params"]

    if intent in ("get_recommendation", "gettourrecommendation"):
        reply = _get_recommendations(params)
    elif intent in ("calculate_billing", "calculatetourbilling"):
        reply = _calculate_billing(params)
    elif intent in ("get_accommodation_recommendation", "gethotelrecommendation"):
        reply = _get_accommodation_recommendations(params)
    elif intent in ("calculate_accommodation_billing", "calculatehotelbilling"):
        reply = _calculate_accommodation_billing(params)
    else:
        reply = (
            "I can help with tour and accommodation recommendations and billing. "
            "Try: 'recommend a tour for 2 guests under 1500', "
            "'recommend a hotel in Bayawan for 2 guests under 2000', "
            "or 'calculate hotel bill for room 12 for 2 nights'."
        )

    return JsonResponse({"fulfillmentText": reply})
