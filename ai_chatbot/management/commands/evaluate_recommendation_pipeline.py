import csv
import json
import re
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q

from admin_app.models import Accomodation, Room
from ai_chatbot.models import RecommendationEvent, RecommendationResult
from ai_chatbot.recommenders import (
    _location_matches_requested,
    _owner_exclusion_keywords,
    recommend_accommodations_with_diagnostics,
)
from guest_app.models import AccommodationBooking

try:
    from sklearn.metrics import (
        accuracy_score,
        confusion_matrix,
        precision_recall_fscore_support,
    )
except Exception as exc:
    raise RuntimeError(
        "scikit-learn is required for evaluation metrics. "
        "Install dependencies from requirements.txt."
    ) from exc


BOOKING_INTENTS = {
    "book_accommodation",
    "bookhotel",
    "book_hotel",
    "reserve_accommodation",
}
RECOMMENDATION_INTENTS = {
    "get_accommodation_recommendation",
    "gethotelrecommendation",
}

LOCATION_NORMALIZATION_RULES = {
    "city proper": "poblacion",
    "poblacion": "poblacion",
    "bayawan": "bayawan city",
}
ITEM_REF_ROOM_RE = re.compile(r"(?:^|\|)room:(\d+)(?:\||$)", re.IGNORECASE)
ITEM_REF_ACCOM_RE = re.compile(r"(?:^|\|)accom:(\d+)(?:\||$)", re.IGNORECASE)


def _safe_int(value, default=0):
    try:
        if value in ("", None):
            return default
        return int(float(value))
    except Exception:
        return default


def _normalize_text(value: str) -> str:
    return " ".join(str(value or "").strip().split())


def _normalize_company_type(raw_value: str) -> str:
    value = str(raw_value or "").strip().lower()
    if "hotel" in value and "inn" in value:
        return "either"
    if "hotel" in value:
        return "hotel"
    if "inn" in value:
        return "inn"
    return "either"


def _raw_company_type_scope(raw_value: str) -> str:
    value = str(raw_value or "").strip().lower()
    has_hotel = "hotel" in value
    has_inn = "inn" in value
    if has_hotel and has_inn:
        return "either"
    if has_hotel:
        return "hotel"
    if has_inn:
        return "inn"
    return ""


def _normalize_location_for_query(location: str) -> Tuple[str, str]:
    raw = _normalize_text(location)
    key = raw.lower()
    mapped = LOCATION_NORMALIZATION_RULES.get(key)
    if mapped:
        return mapped, f"exact:{key}->{mapped}"
    return raw, ""


def _extract_amenity_list(params: dict, room_obj: Optional[Room], accom_obj: Optional[Accomodation]) -> List[str]:
    items: List[str] = []

    raw_amenities = params.get("amenities")
    if isinstance(raw_amenities, list):
        items.extend([str(v).strip() for v in raw_amenities if str(v).strip()])
    elif raw_amenities:
        items.extend([s.strip() for s in str(raw_amenities).replace(";", ",").split(",") if s.strip()])

    raw_pref_tags = params.get("preference_tags")
    if isinstance(raw_pref_tags, list):
        items.extend([str(v).strip() for v in raw_pref_tags if str(v).strip()])

    # Fallback from authoritative room details JSON field.
    details = getattr(room_obj, "owner_details", None)
    if details is not None:
        try:
            parsed = json.loads(str(getattr(details, "amenities", "") or "[]"))
            if isinstance(parsed, list):
                items.extend([str(v).strip() for v in parsed if str(v).strip()])
        except Exception:
            pass

    # Fallback from accommodation-level amenities text.
    if accom_obj is not None:
        accom_amenities = str(getattr(accom_obj, "accommodation_amenities", "") or "").strip()
        if accom_amenities:
            items.extend([s.strip() for s in accom_amenities.replace(";", ",").split(",") if s.strip()])

    deduped: List[str] = []
    seen = set()
    for item in items:
        key = item.lower()
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped[:8]


def _extract_ids_from_item_ref(item_ref: str) -> Tuple[int, int]:
    raw = str(item_ref or "").strip()
    if not raw:
        return 0, 0
    room_match = ITEM_REF_ROOM_RE.search(raw)
    accom_match = ITEM_REF_ACCOM_RE.search(raw)
    room_id = _safe_int(room_match.group(1), default=0) if room_match else 0
    accom_id = _safe_int(accom_match.group(1), default=0) if accom_match else 0
    return room_id, accom_id


@dataclass
class EvalSample:
    sample_id: str
    sample_source: str
    source_result_id: int
    budget: int
    location: str
    amenities: str
    guests: int
    company_type: str
    expected_accom_id: int
    expected_accommodation: str
    expected_room_id: int
    expected_room_name: str


class Command(BaseCommand):
    help = (
        "Build a realistic labeled accommodation recommendation dataset from real booking-linked "
        "chatbot records, run recommender evaluation, and export JSON/CSV metrics."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--target-samples",
            type=int,
            default=50,
            help="Target dataset size upper bound (default: 50).",
        )
        parser.add_argument(
            "--out-dir",
            default="thesis_data_templates",
            help="Output directory for evaluation artifacts. Default: thesis_data_templates",
        )
        parser.add_argument(
            "--prefix",
            default="recommendation_eval",
            help="Output filename prefix. Default: recommendation_eval",
        )
        parser.add_argument(
            "--real-only",
            action="store_true",
            help=(
                "Use only booking-linked chatbot labels. "
                "Skips catalog bootstrap backfill and allows sample count below 30."
            ),
        )

    def handle(self, *args, **options):
        target_samples = int(options.get("target_samples") or 50)
        real_only = bool(options.get("real_only"))
        if real_only:
            if target_samples < 1 or target_samples > 100:
                raise CommandError("--target-samples must be between 1 and 100 when --real-only is enabled.")
        else:
            if target_samples < 30 or target_samples > 100:
                raise CommandError("--target-samples must be between 30 and 100.")

        out_dir = Path(str(options.get("out_dir") or "thesis_data_templates")).resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        prefix = str(options.get("prefix") or "recommendation_eval").strip() or "recommendation_eval"

        samples, build_meta = self._build_labeled_samples(target_samples=target_samples, real_only=real_only)
        if not real_only and len(samples) < 30:
            raise CommandError(
                f"Only {len(samples)} real labeled samples were found. "
                "Collect more booking-linked chatbot records before evaluating."
            )
        if not samples:
            raise CommandError("No labeled samples were found for evaluation.")

        predictions, metrics_payload, confusion_rows, error_rows, diagnostics_report = self._run_evaluation(
            samples,
            real_only=real_only,
            build_meta=build_meta,
        )

        dataset_csv = out_dir / f"{prefix}_dataset.csv"
        predictions_csv = out_dir / f"{prefix}_predictions.csv"
        metrics_json = out_dir / f"{prefix}_metrics.json"
        metrics_csv = out_dir / f"{prefix}_metrics.csv"
        confusion_csv = out_dir / f"{prefix}_confusion_matrix.csv"
        error_csv = out_dir / f"{prefix}_error_analysis.csv"
        error_json = out_dir / f"{prefix}_error_analysis.json"
        diagnostics_json = out_dir / f"{prefix}_diagnostics_report.json"

        self._write_dataset_csv(dataset_csv, samples)
        self._write_predictions_csv(predictions_csv, predictions)
        metrics_json.write_text(json.dumps(metrics_payload, indent=2, ensure_ascii=False), encoding="utf-8")
        self._write_metrics_csv(metrics_csv, metrics_payload)
        self._write_confusion_csv(confusion_csv, confusion_rows)
        self._write_error_csv(error_csv, error_rows)
        error_json.write_text(json.dumps(error_rows, indent=2, ensure_ascii=False), encoding="utf-8")
        diagnostics_json.write_text(json.dumps(diagnostics_report, indent=2, ensure_ascii=False), encoding="utf-8")

        self.stdout.write(
            json.dumps(
                {
                    "status": "ok",
                    "samples_used": len(samples),
                    "outputs": {
                        "dataset_csv": str(dataset_csv),
                        "predictions_csv": str(predictions_csv),
                        "metrics_json": str(metrics_json),
                        "metrics_csv": str(metrics_csv),
                        "confusion_matrix_csv": str(confusion_csv),
                        "error_analysis_csv": str(error_csv),
                        "error_analysis_json": str(error_json),
                        "diagnostics_report_json": str(diagnostics_json),
                    },
                    "summary": {
                        "accuracy": metrics_payload["overall"]["accuracy"],
                        "precision_macro": metrics_payload["overall"]["precision_macro"],
                        "recall_macro": metrics_payload["overall"]["recall_macro"],
                        "f1_macro": metrics_payload["overall"]["f1_macro"],
                    },
                    "filters": metrics_payload["dataset"]["filters"],
                    "location_normalization": metrics_payload["dataset"]["location_normalization"],
                    "mode": "real_only" if real_only else "mixed",
                },
                indent=2,
            )
        )

    def _servable_accommodation_ids(self) -> set:
        room_qs = (
            Room.objects.select_related("accommodation")
            .filter(status="AVAILABLE")
            .filter(current_availability__gte=1)
            .filter(accommodation__approval_status="accepted")
            .filter(accommodation__is_active=True)
            .filter(accommodation__owner__isnull=False)
            .filter(accommodation__owner__is_active=True)
            .filter(accommodation__owner__groups__name__iexact="accommodation_owner")
            .exclude(accommodation__owner__groups__name__iexact="accommodation_owner_pending")
            .exclude(accommodation__owner__groups__name__iexact="accommodation_owner_declined")
            .filter(
                Q(accommodation__company_type__icontains="hotel")
                | Q(accommodation__company_type__icontains="inn")
            )
        )
        for keyword in _owner_exclusion_keywords():
            room_qs = room_qs.exclude(accommodation__owner__email__icontains=keyword)
            room_qs = room_qs.exclude(accommodation__owner__username__icontains=keyword)
            room_qs = room_qs.exclude(accommodation__owner__first_name__icontains=keyword)
            room_qs = room_qs.exclude(accommodation__owner__last_name__icontains=keyword)
            room_qs = room_qs.exclude(accommodation__company_name__icontains=keyword)
        return set(room_qs.values_list("accommodation_id", flat=True))

    def _non_servable_reason(self, accom_obj: Optional[Accomodation], servable_accom_ids: set) -> str:
        if accom_obj is None:
            return "missing_accommodation"
        if int(accom_obj.accom_id) in servable_accom_ids:
            return ""
        if str(getattr(accom_obj, "approval_status", "")).strip().lower() != "accepted":
            return "declined_or_unapproved"
        if not bool(getattr(accom_obj, "is_active", False)):
            return "accommodation_inactive"
        owner = getattr(accom_obj, "owner", None)
        if owner is None:
            return "missing_owner"
        if not bool(getattr(owner, "is_active", False)):
            return "owner_inactive"
        if not _raw_company_type_scope(getattr(accom_obj, "company_type", "")):
            return "out_of_scope_company_type"
        has_available = Room.objects.filter(
            accommodation_id=accom_obj.accom_id,
            status="AVAILABLE",
            current_availability__gte=1,
        ).exists()
        if not has_available:
            return "no_available_room"
        return "not_retrievable_by_current_recommender"

    def _label_sanity_reason(
        self,
        *,
        accom_obj: Optional[Accomodation],
        guests: int,
        requested_company_type: str,
        requested_budget: int = 0,
        requested_location: str = "",
    ) -> str:
        """
        Dataset-only label validation to remove inconsistent ground-truth rows.
        This does not alter runtime recommendation behavior.
        """
        if accom_obj is None:
            return "missing_accommodation"

        req_type = _normalize_company_type(requested_company_type)
        accom_type = _normalize_company_type(getattr(accom_obj, "company_type", ""))
        if req_type in {"hotel", "inn"} and accom_type in {"hotel", "inn"} and req_type != accom_type:
            return "label_type_conflict"

        guest_count = max(1, _safe_int(guests, default=1))
        has_capacity = Room.objects.filter(
            accommodation_id=accom_obj.accom_id,
            status="AVAILABLE",
            current_availability__gte=1,
            person_limit__gte=guest_count,
        ).exists()
        if not has_capacity:
            return "label_capacity_conflict"

        budget_cap = max(0, _safe_int(requested_budget, default=0))
        if budget_cap > 0:
            has_budget_fit = Room.objects.filter(
                accommodation_id=accom_obj.accom_id,
                status="AVAILABLE",
                current_availability__gte=1,
                person_limit__gte=guest_count,
                price_per_night__lte=budget_cap,
            ).exists()
            if not has_budget_fit:
                return "label_budget_conflict"

        normalized_location = _normalize_text(requested_location)
        if normalized_location:
            if not _location_matches_requested(
                normalized_location,
                str(getattr(accom_obj, "location", "") or ""),
            ):
                return "label_location_conflict"

        return ""

    def _build_labeled_samples(self, *, target_samples: int, real_only: bool = False) -> Tuple[List[EvalSample], dict]:
        qs = RecommendationResult.objects.filter(
            context_json__intent__in=(BOOKING_INTENTS | RECOMMENDATION_INTENTS)
        ).order_by("-generated_at")

        bookings_by_id = {
            int(b.booking_id): b
            for b in AccommodationBooking.objects.select_related("accommodation", "room").all()
        }
        rooms_by_id = {
            int(r.room_id): r
            for r in Room.objects.select_related("accommodation", "owner_details").all()
        }
        accom_by_id = {
            int(a.accom_id): a
            for a in Accomodation.objects.all()
        }

        rows = list(qs)
        events = list(
            RecommendationEvent.objects.exclude(session_id="").order_by("-event_time")
        )
        results_by_session: Dict[str, List[RecommendationResult]] = defaultdict(list)
        for row in rows:
            ctx = row.context_json if isinstance(row.context_json, dict) else {}
            session_id = str(ctx.get("session_id") or "").strip()
            intent = str(ctx.get("intent") or "").strip().lower()
            if session_id and intent in (BOOKING_INTENTS | RECOMMENDATION_INTENTS):
                results_by_session[session_id].append(row)
        for key in results_by_session:
            results_by_session[key].sort(key=lambda item: item.generated_at)

        # Build booking labels indexed by session so recommendation queries can be
        # labeled by the actual later booking outcome in the same user session.
        booking_labels_by_session: Dict[str, List[dict]] = defaultdict(list)
        for row in rows:
            ctx = row.context_json if isinstance(row.context_json, dict) else {}
            intent = str(ctx.get("intent") or "").strip().lower()
            if intent not in BOOKING_INTENTS:
                continue
            outcome = ctx.get("booking_outcome") if isinstance(ctx.get("booking_outcome"), dict) else {}
            session_id = str(ctx.get("session_id") or "").strip()
            room_id = _safe_int(outcome.get("room_id"), default=0)
            accom_id = _safe_int(outcome.get("accom_id"), default=0)
            booking_id = _safe_int(outcome.get("booking_id"), default=0)
            if not session_id or (room_id <= 0 and accom_id <= 0 and booking_id <= 0):
                continue
            booking_labels_by_session[session_id].append(
                {
                    "generated_at": row.generated_at,
                    "room_id": room_id,
                    "accom_id": accom_id,
                    "booking_id": booking_id,
                }
            )
        for key in booking_labels_by_session:
            booking_labels_by_session[key].sort(key=lambda item: item["generated_at"])

        # Build interaction labels indexed by session from recommendation clicks.
        # These are used when booking-linked labels are unavailable.
        interaction_labels_by_session: Dict[str, List[dict]] = defaultdict(list)
        for event in events:
            if str(event.event_type or "").strip().lower() not in {"click", "book", "save"}:
                continue
            session_id = str(event.session_id or "").strip()
            if not session_id:
                continue
            room_id, accom_id = _extract_ids_from_item_ref(getattr(event, "item_ref", ""))
            if room_id <= 0 and accom_id <= 0:
                continue
            interaction_labels_by_session[session_id].append(
                {
                    "generated_at": event.event_time,
                    "room_id": room_id,
                    "accom_id": accom_id,
                    "booking_id": 0,
                    "event_id": int(getattr(event, "event_id", 0) or 0),
                    "label_source": "interaction_click_label",
                }
            )
        for key in interaction_labels_by_session:
            interaction_labels_by_session[key].sort(key=lambda item: item["generated_at"])

        grouped: Dict[int, List[EvalSample]] = defaultdict(list)
        servable_accom_ids = self._servable_accommodation_ids()
        build_meta = {
            "real_candidate_rows": 0,
            "real_samples_kept": 0,
            "interaction_labels_total": int(sum(len(v) for v in interaction_labels_by_session.values())),
            "interaction_labels_used": 0,
            "interaction_labels_skipped": 0,
            "removed_non_servable_labels_total": 0,
            "removed_non_servable_by_reason": defaultdict(int),
            "removed_low_confidence_labels_total": 0,
            "removed_low_confidence_by_reason": defaultdict(int),
            "bootstrap_samples_added": 0,
            "bootstrap_removed_out_of_scope_company_type": 0,
            "bootstrap_removed_non_servable": 0,
            "location_normalization_rules": dict(LOCATION_NORMALIZATION_RULES),
        }
        sample_seq = 1

        def _resolve_label_entities(label: dict) -> Tuple[int, int, Optional[AccommodationBooking], Optional[Room], Optional[Accomodation]]:
            room_id = _safe_int(label.get("room_id"), default=0)
            accom_id = _safe_int(label.get("accom_id"), default=0)
            booking_id = _safe_int(label.get("booking_id"), default=0)
            booking_obj = bookings_by_id.get(booking_id)
            room_obj = rooms_by_id.get(room_id)

            if room_obj is None and booking_obj is not None:
                room_obj = booking_obj.room
            if accom_id <= 0 and room_obj is not None and room_obj.accommodation_id:
                accom_id = int(room_obj.accommodation_id)
            if accom_id <= 0 and booking_obj is not None:
                accom_id = int(booking_obj.accommodation_id)

            accom_obj = accom_by_id.get(accom_id)
            return accom_id, room_id, booking_obj, room_obj, accom_obj

        for row in rows:
            build_meta["real_candidate_rows"] += 1
            ctx = row.context_json if isinstance(row.context_json, dict) else {}
            intent = str(ctx.get("intent") or "").strip().lower()
            params = ctx.get("params") if isinstance(ctx.get("params"), dict) else {}

            label_data = None
            if intent in BOOKING_INTENTS:
                outcome = ctx.get("booking_outcome") if isinstance(ctx.get("booking_outcome"), dict) else {}
                label_data = {
                    "generated_at": row.generated_at,
                    "room_id": _safe_int(outcome.get("room_id"), default=0),
                    "accom_id": _safe_int(outcome.get("accom_id"), default=0),
                    "booking_id": _safe_int(outcome.get("booking_id"), default=0),
                }
            elif intent in RECOMMENDATION_INTENTS:
                session_id = str(ctx.get("session_id") or "").strip()
                if session_id and booking_labels_by_session.get(session_id):
                    future_labels = [
                        item
                        for item in booking_labels_by_session[session_id]
                        if item["generated_at"] >= row.generated_at
                    ]
                    if future_labels:
                        label_data = future_labels[0]
                    else:
                        # Fallback: use nearest real booking label in the same session.
                        candidates = booking_labels_by_session[session_id]
                        if candidates:
                            label_data = min(
                                candidates,
                                key=lambda item: abs((item["generated_at"] - row.generated_at).total_seconds()),
                            )
                if (not label_data) and session_id and interaction_labels_by_session.get(session_id):
                    # Use interaction-linked labels only when close in time to avoid
                    # accidental cross-conversation session collisions.
                    max_window_seconds = 2 * 60 * 60
                    future_clicks = [
                        item
                        for item in interaction_labels_by_session[session_id]
                        if item["generated_at"] >= row.generated_at
                        and (item["generated_at"] - row.generated_at).total_seconds() <= max_window_seconds
                    ]
                    if future_clicks:
                        label_data = future_clicks[0]
                    else:
                        candidates = [
                            item
                            for item in interaction_labels_by_session[session_id]
                            if abs((item["generated_at"] - row.generated_at).total_seconds()) <= max_window_seconds
                        ]
                        if candidates:
                            label_data = min(
                                candidates,
                                key=lambda item: abs((item["generated_at"] - row.generated_at).total_seconds()),
                            )

            if not label_data:
                continue

            accom_id, room_id, booking_obj, room_obj, accom_obj = _resolve_label_entities(label_data)
            if accom_obj is None:
                build_meta["removed_non_servable_labels_total"] += 1
                build_meta["removed_non_servable_by_reason"]["missing_accommodation"] += 1
                continue
            removal_reason = self._non_servable_reason(accom_obj, servable_accom_ids)
            if removal_reason:
                build_meta["removed_non_servable_labels_total"] += 1
                build_meta["removed_non_servable_by_reason"][removal_reason] += 1
                continue

            budget = _safe_int(params.get("budget"), default=0)
            if budget <= 0 and room_obj is not None:
                budget = _safe_int(getattr(room_obj, "price_per_night", 0), default=0)
            if budget <= 0 and accom_obj is not None:
                first_room = (
                    Room.objects.filter(accommodation_id=accom_obj.accom_id)
                    .order_by("price_per_night")
                    .first()
                )
                if first_room is not None:
                    budget = _safe_int(getattr(first_room, "price_per_night", 0), default=0)
            if budget <= 0:
                continue

            location = _normalize_text(params.get("location") or "")
            if not location:
                location = _normalize_text(getattr(accom_obj, "location", ""))
            if not location:
                continue

            amenities_list = _extract_amenity_list(params, room_obj, accom_obj)
            if not amenities_list:
                amenities_list = ["general_stay"]

            guests = _safe_int(params.get("guests"), default=0)
            if guests <= 0 and booking_obj is not None:
                guests = _safe_int(getattr(booking_obj, "num_guests", 0), default=0)
            if guests <= 0:
                guests = 1

            company_type = _normalize_company_type(params.get("company_type") or getattr(accom_obj, "company_type", ""))
            sanity_reason = self._label_sanity_reason(
                accom_obj=accom_obj,
                guests=guests,
                requested_company_type=company_type,
                requested_budget=budget,
                requested_location=location,
            )
            if sanity_reason:
                build_meta["removed_low_confidence_labels_total"] += 1
                build_meta["removed_low_confidence_by_reason"][sanity_reason] += 1
                continue

            sample = EvalSample(
                sample_id=f"S{sample_seq:04d}",
                sample_source=str(label_data.get("label_source") or "chat_session_label"),
                source_result_id=int(label_data.get("event_id") or row.result_id),
                budget=budget,
                location=location,
                amenities=", ".join(amenities_list),
                guests=guests,
                company_type=company_type,
                expected_accom_id=int(accom_obj.accom_id),
                expected_accommodation=str(accom_obj.company_name or f"Accommodation {accom_obj.accom_id}"),
                expected_room_id=int(getattr(room_obj, "room_id", 0) or room_id or 0),
                expected_room_name=str(getattr(room_obj, "room_name", "") or ""),
            )
            grouped[sample.expected_accom_id].append(sample)
            build_meta["real_samples_kept"] += 1
            if sample.sample_source == "interaction_click_label":
                build_meta["interaction_labels_used"] += 1
            sample_seq += 1

        seen_event_ids = set()
        for session_id, label_rows in interaction_labels_by_session.items():
            anchors = results_by_session.get(session_id) or []
            if not anchors:
                build_meta["interaction_labels_skipped"] += len(label_rows)
                continue
            for label in label_rows:
                event_id = _safe_int(label.get("event_id"), default=0)
                if event_id <= 0 or event_id in seen_event_ids:
                    continue
                seen_event_ids.add(event_id)

                # Prefer recommendation records prior to click time, else nearest record in session.
                anchor = None
                prior_rows = [row for row in anchors if row.generated_at <= label["generated_at"]]
                if prior_rows:
                    anchor = prior_rows[-1]
                else:
                    anchor = min(
                        anchors,
                        key=lambda row: abs((row.generated_at - label["generated_at"]).total_seconds()),
                    )
                if anchor is None:
                    build_meta["interaction_labels_skipped"] += 1
                    continue

                ctx = anchor.context_json if isinstance(anchor.context_json, dict) else {}
                params = ctx.get("params") if isinstance(ctx.get("params"), dict) else {}
                accom_id, room_id, booking_obj, room_obj, accom_obj = _resolve_label_entities(label)
                if accom_obj is None:
                    build_meta["removed_non_servable_labels_total"] += 1
                    build_meta["removed_non_servable_by_reason"]["missing_accommodation"] += 1
                    continue
                removal_reason = self._non_servable_reason(accom_obj, servable_accom_ids)
                if removal_reason:
                    build_meta["removed_non_servable_labels_total"] += 1
                    build_meta["removed_non_servable_by_reason"][removal_reason] += 1
                    continue

                budget = _safe_int(params.get("budget"), default=0)
                if budget <= 0 and room_obj is not None:
                    budget = _safe_int(getattr(room_obj, "price_per_night", 0), default=0)
                if budget <= 0 and accom_obj is not None:
                    first_room = (
                        Room.objects.filter(accommodation_id=accom_obj.accom_id)
                        .order_by("price_per_night")
                        .first()
                    )
                    if first_room is not None:
                        budget = _safe_int(getattr(first_room, "price_per_night", 0), default=0)
                if budget <= 0:
                    build_meta["interaction_labels_skipped"] += 1
                    continue

                location = _normalize_text(params.get("location") or "")
                if not location:
                    location = _normalize_text(getattr(accom_obj, "location", ""))
                if not location:
                    build_meta["interaction_labels_skipped"] += 1
                    continue

                amenities_list = _extract_amenity_list(params, room_obj, accom_obj)
                if not amenities_list:
                    amenities_list = ["general_stay"]

                guests = _safe_int(params.get("guests"), default=0)
                if guests <= 0 and booking_obj is not None:
                    guests = _safe_int(getattr(booking_obj, "num_guests", 0), default=0)
                if guests <= 0:
                    guests = 1

                company_type = _normalize_company_type(
                    params.get("company_type") or getattr(accom_obj, "company_type", "")
                )
                sanity_reason = self._label_sanity_reason(
                    accom_obj=accom_obj,
                    guests=guests,
                    requested_company_type=company_type,
                    requested_budget=budget,
                    requested_location=location,
                )
                if sanity_reason:
                    build_meta["removed_low_confidence_labels_total"] += 1
                    build_meta["removed_low_confidence_by_reason"][sanity_reason] += 1
                    continue

                sample = EvalSample(
                    sample_id=f"S{sample_seq:04d}",
                    sample_source="interaction_click_label",
                    source_result_id=int(event_id),
                    budget=budget,
                    location=location,
                    amenities=", ".join(amenities_list),
                    guests=guests,
                    company_type=company_type,
                    expected_accom_id=int(accom_obj.accom_id),
                    expected_accommodation=str(accom_obj.company_name or f"Accommodation {accom_obj.accom_id}"),
                    expected_room_id=int(getattr(room_obj, "room_id", 0) or room_id or 0),
                    expected_room_name=str(getattr(room_obj, "room_name", "") or ""),
                )
                grouped[sample.expected_accom_id].append(sample)
                build_meta["real_samples_kept"] += 1
                build_meta["interaction_labels_used"] += 1
                sample_seq += 1

        current_total = sum(len(rows) for rows in grouped.values())
        if (not real_only) and current_total < target_samples:
            needed = target_samples - current_total
            bootstrap_samples = self._build_catalog_bootstrap_samples(
                needed=needed,
                start_seq=sample_seq,
                existing_samples=[item for rows in grouped.values() for item in rows],
                servable_accom_ids=servable_accom_ids,
                build_meta=build_meta,
            )
            for sample in bootstrap_samples:
                grouped[sample.expected_accom_id].append(sample)
            build_meta["bootstrap_samples_added"] = len(bootstrap_samples)
            sample_seq += len(bootstrap_samples)

        # Round-robin selection across accommodations to avoid one-class dominance.
        buckets = {k: list(v) for k, v in grouped.items() if v}
        selected: List[EvalSample] = []
        limit = min(target_samples, current_total) if real_only else target_samples
        while len(selected) < limit and buckets:
            for accom_id in list(buckets.keys()):
                rows = buckets[accom_id]
                if not rows:
                    del buckets[accom_id]
                    continue
                selected.append(rows.pop(0))
                if len(selected) >= limit:
                    break

        build_meta["removed_non_servable_by_reason"] = dict(build_meta["removed_non_servable_by_reason"])
        build_meta["removed_low_confidence_by_reason"] = dict(build_meta["removed_low_confidence_by_reason"])
        return selected, build_meta

    def _build_catalog_bootstrap_samples(
        self,
        *,
        needed: int,
        start_seq: int,
        existing_samples: List[EvalSample],
        servable_accom_ids: set,
        build_meta: dict,
    ) -> List[EvalSample]:
        if needed <= 0:
            return []

        existing_keys = {
            (
                s.expected_accom_id,
                s.budget,
                s.location.strip().lower(),
                s.guests,
                s.company_type.strip().lower(),
                s.amenities.strip().lower(),
            )
            for s in existing_samples
        }

        candidate_rooms = (
            Room.objects.select_related("accommodation", "owner_details")
            .filter(accommodation__approval_status="accepted", accommodation__is_active=True)
            .order_by("accommodation__company_name", "price_per_night", "room_name")
        )

        generated: List[EvalSample] = []
        seq = start_seq
        for room in candidate_rooms:
            accom = getattr(room, "accommodation", None)
            if accom is None:
                continue

            company_type = _raw_company_type_scope(getattr(accom, "company_type", ""))
            if company_type not in {"hotel", "inn", "either"}:
                build_meta["bootstrap_removed_out_of_scope_company_type"] += 1
                continue
            if company_type == "either":
                company_type = "hotel"
            if int(accom.accom_id) not in servable_accom_ids:
                build_meta["bootstrap_removed_non_servable"] += 1
                continue

            location = _normalize_text(getattr(accom, "location", ""))
            if not location:
                continue

            amenities_list = _extract_amenity_list({}, room, accom)
            if not amenities_list:
                amenities_list = ["general_stay"]
            amenities_text = ", ".join(amenities_list)

            base_price = _safe_int(getattr(room, "price_per_night", 0), default=0)
            if base_price <= 0:
                continue
            guests_base = max(1, min(_safe_int(getattr(room, "person_limit", 1), default=1), 4))

            scenario_values = [
                (base_price, guests_base),
                (int(round(base_price * 1.15)), max(1, min(guests_base + 1, _safe_int(getattr(room, "person_limit", guests_base), default=guests_base)))),
            ]

            for budget, guests in scenario_values:
                key = (
                    int(accom.accom_id),
                    int(budget),
                    location.lower(),
                    int(guests),
                    company_type.lower(),
                    amenities_text.lower(),
                )
                if key in existing_keys:
                    continue
                existing_keys.add(key)

                generated.append(
                    EvalSample(
                        sample_id=f"S{seq:04d}",
                        sample_source="catalog_bootstrap",
                        source_result_id=0,
                        budget=int(budget),
                        location=location,
                        amenities=amenities_text,
                        guests=int(guests),
                        company_type=company_type,
                        expected_accom_id=int(accom.accom_id),
                        expected_accommodation=str(accom.company_name or f"Accommodation {accom.accom_id}"),
                        expected_room_id=int(getattr(room, "room_id", 0) or 0),
                        expected_room_name=str(getattr(room, "room_name", "") or ""),
                    )
                )
                seq += 1
                if len(generated) >= needed:
                    return generated

        return generated

    def _run_evaluation(
        self,
        samples: List[EvalSample],
        *,
        real_only: bool = False,
        build_meta: Optional[dict] = None,
    ) -> Tuple[List[dict], dict, List[dict], List[dict], dict]:
        y_true: List[str] = []
        y_pred: List[str] = []
        prediction_rows: List[dict] = []
        error_rows: List[dict] = []
        source_counts = defaultdict(int)
        location_normalized_count = 0

        for sample in samples:
            source_counts[sample.sample_source] += 1
            normalized_location, normalization_rule = _normalize_location_for_query(sample.location)
            if normalization_rule:
                location_normalized_count += 1
            params = {
                "guests": sample.guests,
                "budget": sample.budget,
                "location": normalized_location,
                "company_type": sample.company_type,
                "amenities": [token.strip() for token in sample.amenities.split(",") if token.strip()],
            }
            results, diagnostics = recommend_accommodations_with_diagnostics(params, limit=8)

            top_prediction = results[0] if results else None
            pred_accom_id = None
            pred_name = ""
            pred_score = ""
            if top_prediction is not None:
                meta = getattr(top_prediction, "meta", {}) if hasattr(top_prediction, "meta") else {}
                pred_accom_id = _safe_int(meta.get("accom_id"), default=0)
                pred_name = str(top_prediction.title or "")
                pred_score = float(getattr(top_prediction, "score", 0.0))

            expected_label = f"accom_{sample.expected_accom_id}"
            predicted_label = f"accom_{pred_accom_id}" if pred_accom_id else "__NO_RESULT__"
            y_true.append(expected_label)
            y_pred.append(predicted_label)

            prediction_rows.append(
                {
                    "sample_id": sample.sample_id,
                    "sample_source": sample.sample_source,
                    "source_result_id": sample.source_result_id,
                    "budget": sample.budget,
                    "location": sample.location,
                    "normalized_location": normalized_location,
                    "location_normalization_rule": normalization_rule,
                    "amenities": sample.amenities,
                    "guests": sample.guests,
                    "company_type": sample.company_type,
                    "expected_accom_id": sample.expected_accom_id,
                    "expected_accommodation": sample.expected_accommodation,
                    "predicted_accom_id": pred_accom_id or "",
                    "predicted_top1": pred_name,
                    "predicted_top1_score": pred_score if pred_score != "" else "",
                    "match_top1": int(predicted_label == expected_label),
                    "fallback_applied": str(diagnostics.get("fallback_applied") or ""),
                    "fallback_reason": str(diagnostics.get("fallback_reason") or ""),
                    "fallback_reason_codes": ",".join(
                        [str(v) for v in (diagnostics.get("fallback_reason_codes") or [])]
                    ),
                    "no_match_reasons": ",".join(
                        [str(v) for v in (diagnostics.get("no_match_reasons") or [])]
                    ),
                }
            )
            if predicted_label != expected_label:
                error_rows.append(
                    self._build_error_analysis_row(
                        sample=sample,
                        params=params,
                        results=results,
                        diagnostics=diagnostics,
                        expected_label=expected_label,
                        predicted_label=predicted_label,
                    )
                )

        labels = sorted(set(y_true) | set(y_pred))
        cm = confusion_matrix(y_true, y_pred, labels=labels)
        accuracy = accuracy_score(y_true, y_pred)
        precision_macro, recall_macro, f1_macro, _ = precision_recall_fscore_support(
            y_true,
            y_pred,
            labels=labels,
            average="macro",
            zero_division=0,
        )
        precision_weighted, recall_weighted, f1_weighted, _ = precision_recall_fscore_support(
            y_true,
            y_pred,
            labels=labels,
            average="weighted",
            zero_division=0,
        )

        per_class_precision, per_class_recall, per_class_f1, per_class_support = precision_recall_fscore_support(
            y_true,
            y_pred,
            labels=labels,
            average=None,
            zero_division=0,
        )
        per_class = []
        for idx, label in enumerate(labels):
            per_class.append(
                {
                    "label": label,
                    "precision": float(per_class_precision[idx]),
                    "recall": float(per_class_recall[idx]),
                    "f1_score": float(per_class_f1[idx]),
                    "support": int(per_class_support[idx]),
                }
            )

        confusion_rows = []
        for i, true_label in enumerate(labels):
            row_payload = {"true_label": true_label}
            for j, pred_label in enumerate(labels):
                row_payload[pred_label] = int(cm[i][j])
            confusion_rows.append(row_payload)

        metrics_payload = {
            "dataset": {
                "sample_count": len(samples),
                "label_source": (
                    "booking-linked and interaction-linked chatbot records only"
                    if real_only
                    else "booking-linked chatbot records with deterministic room-catalog backfill"
                ),
                "sample_sources": dict(source_counts),
                "scope": "Bayawan-style hotel/inn accommodation flow",
                "mode": "real_only" if real_only else "mixed",
                "location_normalization": {
                    "rules": dict(LOCATION_NORMALIZATION_RULES),
                    "applied_samples": int(location_normalized_count),
                },
                "filters": {
                    "interaction_labels_total": int((build_meta or {}).get("interaction_labels_total", 0)),
                    "interaction_labels_used": int((build_meta or {}).get("interaction_labels_used", 0)),
                    "removed_non_servable_labels_total": int((build_meta or {}).get("removed_non_servable_labels_total", 0)),
                    "removed_non_servable_by_reason": dict((build_meta or {}).get("removed_non_servable_by_reason", {})),
                    "removed_low_confidence_labels_total": int((build_meta or {}).get("removed_low_confidence_labels_total", 0)),
                    "removed_low_confidence_by_reason": dict((build_meta or {}).get("removed_low_confidence_by_reason", {})),
                    "bootstrap_removed_out_of_scope_company_type": int((build_meta or {}).get("bootstrap_removed_out_of_scope_company_type", 0)),
                    "bootstrap_removed_non_servable": int((build_meta or {}).get("bootstrap_removed_non_servable", 0)),
                },
            },
            "overall": {
                "accuracy": float(accuracy),
                "precision_macro": float(precision_macro),
                "recall_macro": float(recall_macro),
                "f1_macro": float(f1_macro),
                "precision_weighted": float(precision_weighted),
                "recall_weighted": float(recall_weighted),
                "f1_weighted": float(f1_weighted),
            },
            "labels": labels,
            "per_class": per_class,
            "confusion_matrix": {
                "labels": labels,
                "matrix": [[int(v) for v in row] for row in cm.tolist()],
            },
        }

        diagnostics_report = self._build_diagnostics_report(
            error_rows=error_rows,
            metrics_payload=metrics_payload,
            build_meta=(build_meta or {}),
        )
        return prediction_rows, metrics_payload, confusion_rows, error_rows, diagnostics_report

    def _build_error_analysis_row(
        self,
        *,
        sample: EvalSample,
        params: dict,
        results: List,
        diagnostics: dict,
        expected_label: str,
        predicted_label: str,
    ) -> dict:
        expected_accom = Accomodation.objects.filter(accom_id=sample.expected_accom_id).first()
        expected_rooms_qs = Room.objects.filter(
            accommodation_id=sample.expected_accom_id,
            status="AVAILABLE",
            current_availability__gte=1,
        )
        gt_capacity_fit = expected_rooms_qs.filter(person_limit__gte=max(1, int(sample.guests or 1))).exists()
        gt_budget_fit = True
        if int(sample.budget or 0) > 0:
            gt_budget_fit = expected_rooms_qs.filter(
                person_limit__gte=max(1, int(sample.guests or 1)),
                price_per_night__lte=int(sample.budget or 0),
            ).exists()
        gt_type_fit = True
        if sample.company_type in {"hotel", "inn"}:
            expected_type = _normalize_company_type(getattr(expected_accom, "company_type", "") if expected_accom else "")
            gt_type_fit = expected_type == sample.company_type
        gt_location_fit = True
        if str(sample.location or "").strip() and expected_accom is not None:
            gt_location_fit = _location_matches_requested(
                str(sample.location or "").strip(),
                str(getattr(expected_accom, "location", "") or ""),
            )
        ground_truth_valid = bool(expected_accom and gt_capacity_fit and gt_budget_fit and gt_type_fit and gt_location_fit)

        predicted_top = results[0] if results else None
        predicted_meta = predicted_top.meta if (predicted_top is not None and isinstance(predicted_top.meta, dict)) else {}
        predicted_trace = predicted_meta.get("trace") if isinstance(predicted_meta.get("trace"), dict) else {}
        expected_result = None
        for item in results:
            meta = item.meta if isinstance(getattr(item, "meta", None), dict) else {}
            if _safe_int(meta.get("accom_id"), default=0) == int(sample.expected_accom_id):
                expected_result = item
                break
        expected_meta = expected_result.meta if (expected_result is not None and isinstance(expected_result.meta, dict)) else {}
        expected_trace = expected_meta.get("trace") if isinstance(expected_meta.get("trace"), dict) else {}

        candidates = []
        for rank, item in enumerate(results[:5], start=1):
            meta = item.meta if isinstance(getattr(item, "meta", None), dict) else {}
            trace = meta.get("trace") if isinstance(meta.get("trace"), dict) else {}
            candidates.append(
                {
                    "rank": rank,
                    "accom_id": _safe_int(meta.get("accom_id"), default=0),
                    "company_name": str(meta.get("company_name") or ""),
                    "room_name": str(meta.get("room_name") or ""),
                    "score": round(float(getattr(item, "score", 0.0) or 0.0), 6),
                    "location_match": bool(trace.get("location_match")),
                    "type_match": bool(trace.get("type_match")),
                    "guest_fit": bool(trace.get("guest_fit")),
                    "amenity_match_ratio": float(trace.get("amenity_match_ratio") or 0.0),
                    "room_type_match_ratio": float(trace.get("room_type_match_ratio") or 0.0),
                    "location_specificity": float(trace.get("location_specificity") or 0.0),
                    "reasons": trace.get("reasons") if isinstance(trace.get("reasons"), list) else [],
                }
            )

        cause_category = "ranking/tie-break issue"
        if not ground_truth_valid:
            cause_category = "bad or non-servable label"
        elif not gt_location_fit:
            cause_category = "location mismatch"
        elif not gt_budget_fit:
            cause_category = "budget scoring issue"
        elif not gt_capacity_fit:
            cause_category = "capacity issue"
        elif not gt_type_fit:
            cause_category = "accommodation type mismatch"
        elif str(diagnostics.get("fallback_applied") or "").startswith("soft_"):
            reason_codes = diagnostics.get("fallback_reason_codes") or []
            if any("location_relaxed" in str(v) for v in reason_codes):
                cause_category = "fallback too broad"
            elif any("budget_relaxed" in str(v) for v in reason_codes):
                cause_category = "over-aggressive soft recovery"
            else:
                cause_category = "fallback too broad"
        elif expected_result is None:
            if str(sample.location or "").strip().lower() in {"bayawan", "bayawan city", "city proper", "poblacion"}:
                cause_category = "class ambiguity / near duplicate accommodations"
            else:
                cause_category = "location mismatch"
        else:
            expected_score = float(getattr(expected_result, "score", 0.0) or 0.0)
            predicted_score = float(getattr(predicted_top, "score", 0.0) or 0.0) if predicted_top is not None else 0.0
            score_gap = predicted_score - expected_score
            if score_gap <= 0.03:
                cause_category = "class ambiguity / near duplicate accommodations"
            elif float(expected_trace.get("amenity_match_ratio") or 0.0) < float(predicted_trace.get("amenity_match_ratio") or 0.0):
                cause_category = "missing/weak amenities feature"
            elif float(expected_trace.get("room_type_match_ratio") or 0.0) < float(predicted_trace.get("room_type_match_ratio") or 0.0):
                cause_category = "room type mismatch"
            else:
                cause_category = "ranking/tie-break issue"

        expected_rank = ""
        if expected_result is not None:
            for idx, item in enumerate(results, start=1):
                meta = item.meta if isinstance(getattr(item, "meta", None), dict) else {}
                if _safe_int(meta.get("accom_id"), default=0) == int(sample.expected_accom_id):
                    expected_rank = idx
                    break

        expected_score = float(getattr(expected_result, "score", 0.0) or 0.0) if expected_result is not None else None
        predicted_score = float(getattr(predicted_top, "score", 0.0) or 0.0) if predicted_top is not None else None
        why_expected_lost = ""
        if not ground_truth_valid:
            reasons = []
            if not expected_accom:
                reasons.append("missing accommodation record")
            if not gt_type_fit:
                reasons.append("type conflict")
            if not gt_location_fit:
                reasons.append("location conflict")
            if not gt_capacity_fit:
                reasons.append("capacity conflict")
            if not gt_budget_fit:
                reasons.append("budget conflict")
            why_expected_lost = "Ground truth label cannot be fully defended: " + ", ".join(reasons)
        elif expected_result is None:
            why_expected_lost = "Expected accommodation did not survive strict ranked candidates under current constraints."
        else:
            why_expected_lost = (
                f"Predicted score {predicted_score:.4f} outranked expected score {expected_score:.4f}."
            )

        return {
            "sample_id": sample.sample_id,
            "expected_label": expected_label,
            "predicted_label": predicted_label,
            "expected_accom_id": int(sample.expected_accom_id),
            "expected_accommodation": str(sample.expected_accommodation or ""),
            "predicted_accom_id": _safe_int(predicted_meta.get("accom_id"), default=0),
            "predicted_accommodation": str(predicted_meta.get("company_name") or str(getattr(predicted_top, "title", "") or "")),
            "query_location": str(params.get("location") or ""),
            "query_budget": _safe_int(params.get("budget"), default=0),
            "query_company_type": str(params.get("company_type") or ""),
            "query_guests": _safe_int(params.get("guests"), default=0),
            "query_amenities": ", ".join([str(v) for v in (params.get("amenities") or [])]),
            "query_room_type": str(params.get("room_type") or ""),
            "query_check_in": str(params.get("check_in") or ""),
            "query_check_out": str(params.get("check_out") or ""),
            "fallback_applied": str(diagnostics.get("fallback_applied") or ""),
            "fallback_reason_codes": ",".join([str(v) for v in (diagnostics.get("fallback_reason_codes") or [])]),
            "expected_rank_in_candidates": expected_rank,
            "expected_score": expected_score if expected_score is not None else "",
            "predicted_score": predicted_score if predicted_score is not None else "",
            "ground_truth_valid_and_servable": bool(ground_truth_valid),
            "ground_truth_validity_notes": why_expected_lost,
            "prediction_reasonable_but_ambiguous": bool(cause_category == "class ambiguity / near duplicate accommodations"),
            "cause_category": cause_category,
            "why_expected_lost": why_expected_lost,
            "top_candidates_json": json.dumps(candidates, ensure_ascii=False),
            "excluded_candidate_hints": ",".join([str(v) for v in (diagnostics.get("no_match_reasons") or [])]),
        }

    def _build_diagnostics_report(self, *, error_rows: List[dict], metrics_payload: dict, build_meta: dict) -> dict:
        cause_counts = defaultdict(int)
        ambiguous_count = 0
        invalid_gt_count = 0
        for row in error_rows:
            cause_counts[str(row.get("cause_category") or "unknown")] += 1
            if bool(row.get("prediction_reasonable_but_ambiguous")):
                ambiguous_count += 1
            if not bool(row.get("ground_truth_valid_and_servable", True)):
                invalid_gt_count += 1
        return {
            "generated_summary": {
                "total_errors": len(error_rows),
                "cause_counts": dict(cause_counts),
                "ambiguous_prediction_count": ambiguous_count,
                "invalid_ground_truth_count": invalid_gt_count,
            },
            "dataset_filters": metrics_payload.get("dataset", {}).get("filters", {}),
            "location_normalization": metrics_payload.get("dataset", {}).get("location_normalization", {}),
            "build_meta": build_meta,
        }

    def _write_dataset_csv(self, path: Path, samples: Iterable[EvalSample]) -> None:
        headers = [
            "sample_id",
            "sample_source",
            "source_result_id",
            "budget",
            "location",
            "amenities",
            "guests",
            "company_type",
            "expected_accom_id",
            "expected_accommodation",
            "expected_room_id",
            "expected_room_name",
        ]
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=headers)
            writer.writeheader()
            for sample in samples:
                writer.writerow(asdict(sample))

    def _write_predictions_csv(self, path: Path, rows: Iterable[dict]) -> None:
        rows = list(rows)
        if not rows:
            return
        headers = list(rows[0].keys())
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=headers)
            writer.writeheader()
            writer.writerows(rows)

    def _write_metrics_csv(self, path: Path, metrics_payload: dict) -> None:
        rows = [
            {"metric_name": "accuracy", "metric_value": metrics_payload["overall"]["accuracy"]},
            {"metric_name": "precision_macro", "metric_value": metrics_payload["overall"]["precision_macro"]},
            {"metric_name": "recall_macro", "metric_value": metrics_payload["overall"]["recall_macro"]},
            {"metric_name": "f1_macro", "metric_value": metrics_payload["overall"]["f1_macro"]},
            {"metric_name": "precision_weighted", "metric_value": metrics_payload["overall"]["precision_weighted"]},
            {"metric_name": "recall_weighted", "metric_value": metrics_payload["overall"]["recall_weighted"]},
            {"metric_name": "f1_weighted", "metric_value": metrics_payload["overall"]["f1_weighted"]},
            {"metric_name": "sample_count", "metric_value": metrics_payload["dataset"]["sample_count"]},
            {"metric_name": "location_normalized_samples", "metric_value": metrics_payload["dataset"]["location_normalization"]["applied_samples"]},
            {"metric_name": "removed_non_servable_labels_total", "metric_value": metrics_payload["dataset"]["filters"]["removed_non_servable_labels_total"]},
            {"metric_name": "removed_low_confidence_labels_total", "metric_value": metrics_payload["dataset"]["filters"]["removed_low_confidence_labels_total"]},
            {"metric_name": "bootstrap_removed_out_of_scope_company_type", "metric_value": metrics_payload["dataset"]["filters"]["bootstrap_removed_out_of_scope_company_type"]},
            {"metric_name": "bootstrap_removed_non_servable", "metric_value": metrics_payload["dataset"]["filters"]["bootstrap_removed_non_servable"]},
        ]
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=["metric_name", "metric_value"])
            writer.writeheader()
            writer.writerows(rows)

    def _write_confusion_csv(self, path: Path, rows: Iterable[dict]) -> None:
        rows = list(rows)
        if not rows:
            return
        headers = ["true_label"] + [h for h in rows[0].keys() if h != "true_label"]
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=headers)
            writer.writeheader()
            writer.writerows(rows)

    def _write_error_csv(self, path: Path, rows: Iterable[dict]) -> None:
        rows = list(rows)
        if not rows:
            return
        headers = list(rows[0].keys())
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=headers)
            writer.writeheader()
            writer.writerows(rows)
