from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable, List, Optional

from django.utils import timezone
from django.db.models import F, Q

from admin_app.models import Accomodation, Room
from tour_app.models import Tour_Schedule


@dataclass
class RecommendationResult:
    title: str
    subtitle: str
    score: float
    meta: dict


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


def _normalize(value: float, min_value: float, max_value: float) -> float:
    if max_value <= min_value:
        return 0.0
    return max(0.0, min(1.0, (value - min_value) / (max_value - min_value)))


def _cnn_score(features: List[float]) -> float:
    """
    Lightweight 1D CNN-style scorer with a fixed kernel.
    This is intentionally simple and dependency-free.
    """
    if not features:
        return 0.0
    if len(features) < 3:
        return sum(features) / len(features)

    kernel = [0.25, 0.5, 0.25]
    conv = []
    for i in range(len(features) - 2):
        window = features[i:i + 3]
        conv.append(sum(w * k for w, k in zip(window, kernel)))
    return sum(conv) / len(conv)


def _decision_tree_penalty(conditions: Iterable[bool]) -> float:
    """
    Simple Decision Tree proxy: penalize failing hard constraints.
    """
    if any(conditions):
        return -10.0
    return 0.0


def recommend_tours(params: dict, limit: int = 3) -> List[RecommendationResult]:
    now = timezone.now()
    guests = _to_int(params.get("guests"), default=1)
    budget = _to_decimal(params.get("budget"), default=Decimal("0"))
    duration = _to_int(params.get("duration_days"), default=0)
    preference = str(
        params.get("preference")
        or params.get("tour_type")
        or params.get("interest")
        or ""
    ).strip().lower()

    schedules = (
        Tour_Schedule.objects.select_related("tour")
        .filter(end_time__gte=now)
        .exclude(status="cancelled")
        .annotate(slots_left=F("slots_available") - F("slots_booked"))
    )

    prices = [float(s.price) for s in schedules] or [0.0]
    min_price, max_price = min(prices), max(prices)

    results = []
    for schedule in schedules:
        slots_left = max(schedule.slots_left, 0)
        if slots_left < guests:
            continue

        price = float(schedule.price)
        price_fit = 1.0 if budget <= 0 else float(min(budget / Decimal(price), 1))
        duration_fit = 1.0 if duration and schedule.duration_days == duration else 0.5 if duration and abs(schedule.duration_days - duration) == 1 else 0.0
        if not duration:
            duration_fit = 0.5

        name = (schedule.tour.tour_name or "").lower()
        desc = (schedule.tour.description or "").lower()
        preference_fit = 1.0 if preference and (preference in name or preference in desc) else 0.3 if not preference else 0.0
        availability_fit = min(slots_left, 10) / 10.0
        price_norm = 1.0 - _normalize(price, min_price, max_price)

        features = [price_fit, duration_fit, preference_fit, availability_fit, price_norm]
        cnn_score = _cnn_score(features)

        penalty = _decision_tree_penalty([
            budget > 0 and price > float(budget),
            duration > 0 and schedule.duration_days not in (duration, duration - 1, duration + 1),
        ])

        score = cnn_score + penalty
        results.append(
            RecommendationResult(
                title=schedule.tour.tour_name,
                subtitle=f"{schedule.sched_id} | PHP {schedule.price} per guest | {schedule.duration_days} day(s)",
                score=score,
                meta={"sched_id": schedule.sched_id},
            )
        )

    results.sort(key=lambda item: item.score, reverse=True)
    return results[:limit]


def recommend_accommodations(params: dict, limit: int = 3) -> List[RecommendationResult]:
    guests = _to_int(params.get("guests"), default=1)
    budget = _to_decimal(params.get("budget"), default=Decimal("0"))
    location = str(params.get("location") or "").strip().lower()
    company_type = str(params.get("company_type") or "").strip().lower()

    room_qs = (
        Room.objects.select_related("accommodation")
        .filter(status="AVAILABLE")
        .filter(current_availability__gte=1)
    )

    if company_type:
        room_qs = room_qs.filter(accommodation__company_type__icontains=company_type)
    else:
        room_qs = room_qs.filter(
            Q(accommodation__company_type__icontains="hotel") |
            Q(accommodation__company_type__icontains="inn")
        )

    prices = [float(r.price_per_night) for r in room_qs] or [0.0]
    min_price, max_price = min(prices), max(prices)

    results = []
    for room in room_qs:
        accom = room.accommodation
        if room.person_limit and guests > room.person_limit:
            continue

        price = float(room.price_per_night)
        price_fit = 1.0 if budget <= 0 else float(min(budget / Decimal(price or 1), 1))
        capacity_fit = 1.0 if room.person_limit and guests <= room.person_limit else 0.5
        location_fit = 1.0 if location and location in (accom.location or "").lower() else 0.4 if not location else 0.0
        company_fit = 1.0 if company_type and company_type in (accom.company_type or "").lower() else 0.5
        price_norm = 1.0 - _normalize(price, min_price, max_price)

        features = [price_fit, capacity_fit, location_fit, company_fit, price_norm]
        cnn_score = _cnn_score(features)

        penalty = _decision_tree_penalty([
            budget > 0 and price > float(budget),
            room.status != "AVAILABLE",
            room.current_availability is not None and room.current_availability <= 0,
        ])

        score = cnn_score + penalty
        results.append(
            RecommendationResult(
                title=f"{accom.company_name} - {room.room_name}",
                subtitle=f"{accom.location} | PHP {room.price_per_night} per night | {room.person_limit} pax",
                score=score,
                meta={"room_id": room.room_id, "accom_id": accom.accom_id},
            )
        )

    results.sort(key=lambda item: item.score, reverse=True)
    return results[:limit]


def calculate_accommodation_billing(room: Room, check_in, check_out) -> Decimal:
    nights = max((check_out - check_in).days, 1)
    return Decimal(room.price_per_night) * Decimal(nights)
