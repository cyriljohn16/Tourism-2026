import csv
import json
from datetime import datetime, time
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q
from django.utils import timezone

from ai_chatbot.models import RecommendationEvent, RecommendationResult, SystemMetricLog
from guest_app.models import Pending, TourBooking


ACCOM_RECO_INTENTS = {"get_accommodation_recommendation", "gethotelrecommendation"}

STAGE_EVENT_REFS = {
    "accommodation_search_started": {
        "new": {"accommodation_search_started"},
        "legacy": {"chat:accommodation_recommendation_request"},
    },
    "accommodation_recommendations_shown": {
        "new": {"accommodation_recommendations_shown"},
        "legacy": {"chat:funnel_recommendation_shown", "chat:accommodation_recommendation_rendered"},
    },
    "accommodation_rooms_viewed": {
        "new": {"accommodation_rooms_viewed"},
        "legacy": {"chat:funnel_recommendation_card_clicked"},
    },
    "accommodation_preview_started": {
        "new": {"accommodation_preview_started"},
        "legacy": {"chat:funnel_booking_flow_started", "chat:accommodation_booking_draft_or_pending"},
    },
    "accommodation_preview_completed": {
        "new": {"accommodation_preview_completed"},
        "legacy": {"chat:funnel_billing_link_shown", "chat:lgu_payment_handoff_ready"},
    },
    "accommodation_external_handoff_clicked": {
        "new": {"accommodation_external_handoff_clicked"},
        "legacy": {"chat:funnel_billing_link_clicked", "chat:billing_link_click"},
    },
}

EMAIL_METRIC_ENDPOINTS = {
    "tour_pending_email": {
        "sent": "tour_email:tour_pending_email_sent",
        "failed": "tour_email:tour_pending_email_failed",
    },
    "tour_approval_email": {
        "sent": "tour_email:tour_approval_email_sent",
        "failed": "tour_email:tour_approval_email_failed",
    },
    "tour_rejection_email": {
        "sent": "tour_email:tour_rejection_email_sent",
        "failed": "tour_email:tour_rejection_email_failed",
    },
}


def _safe_pct(numerator, denominator):
    if not denominator:
        return None
    return round((float(numerator) / float(denominator)) * 100.0, 4)


def _iso(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        if timezone.is_naive(value):
            value = timezone.make_aware(value, timezone.get_current_timezone())
        return value.isoformat()
    return str(value)


def _parse_date(value: str, *, end=False):
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError as exc:
        raise CommandError(f"Invalid date format '{raw}'. Use YYYY-MM-DD.") from exc
    dt = datetime.combine(parsed, time.max if end else time.min)
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, timezone.get_current_timezone())
    return dt


def _session_set_for_refs(base_qs, refs):
    if not refs:
        return set()
    return set(
        base_qs.filter(item_ref__in=list(refs))
        .exclude(session_id="")
        .values_list("session_id", flat=True)
    )


class Command(BaseCommand):
    help = (
        "Export Chapter 4 conversion metrics from runtime tables (read-only). "
        "Reports session-deduplicated accommodation preview funnel and tour/email evidence."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--out-dir",
            default="thesis_data_templates",
            help="Output directory for Chapter 4 conversion artifacts.",
        )
        parser.add_argument(
            "--prefix",
            default="chapter4_conversion",
            help="Output filename prefix.",
        )
        parser.add_argument(
            "--start-date",
            default="",
            help="Optional start date filter (YYYY-MM-DD) for event-based tables.",
        )
        parser.add_argument(
            "--end-date",
            default="",
            help="Optional end date filter (YYYY-MM-DD) for event-based tables.",
        )

    def handle(self, *args, **options):
        out_dir = Path(str(options.get("out_dir") or "thesis_data_templates")).resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        prefix = str(options.get("prefix") or "chapter4_conversion").strip() or "chapter4_conversion"

        start_date = _parse_date(str(options.get("start_date") or "").strip(), end=False)
        end_date = _parse_date(str(options.get("end_date") or "").strip(), end=True)
        if start_date and end_date and start_date > end_date:
            raise CommandError("--start-date cannot be later than --end-date.")

        event_qs = RecommendationEvent.objects.all()
        metric_qs = SystemMetricLog.objects.filter(module="email")
        if start_date:
            event_qs = event_qs.filter(event_time__gte=start_date)
            metric_qs = metric_qs.filter(logged_at__gte=start_date)
        if end_date:
            event_qs = event_qs.filter(event_time__lte=end_date)
            metric_qs = metric_qs.filter(logged_at__lte=end_date)

        # Stage session sets (prefer new semantics when available).
        stage_session_sets = {}
        stage_sources = {}
        for stage, refs in STAGE_EVENT_REFS.items():
            new_set = _session_set_for_refs(event_qs, refs.get("new") or set())
            legacy_set = _session_set_for_refs(event_qs, refs.get("legacy") or set())
            if new_set:
                stage_session_sets[stage] = new_set
                stage_sources[stage] = "new_event_names"
            else:
                stage_session_sets[stage] = legacy_set
                stage_sources[stage] = "legacy_event_names"

        # Also keep recommendation sessions from result records as supporting evidence.
        result_qs = RecommendationResult.objects.filter(context_json__intent__in=ACCOM_RECO_INTENTS)
        if start_date:
            result_qs = result_qs.filter(generated_at__gte=start_date)
        if end_date:
            result_qs = result_qs.filter(generated_at__lte=end_date)
        recommendation_sessions = set()
        for row in result_qs:
            ctx = row.context_json if isinstance(row.context_json, dict) else {}
            sid = str(ctx.get("session_id") or "").strip()
            if sid:
                recommendation_sessions.add(sid)

        ordered_stages = [
            "accommodation_search_started",
            "accommodation_recommendations_shown",
            "accommodation_rooms_viewed",
            "accommodation_preview_started",
            "accommodation_preview_completed",
            "accommodation_external_handoff_clicked",
        ]
        stage_counts = {stage: len(stage_session_sets.get(stage, set())) for stage in ordered_stages}

        def _subset_rate(numerator_stage, denominator_stage):
            numerator = stage_session_sets.get(numerator_stage, set())
            denominator = stage_session_sets.get(denominator_stage, set())
            if not denominator:
                return "", "Not computed (denominator has zero sessions)."
            if not numerator.issubset(denominator):
                return "Not computed due to inconsistent historical event semantics.", (
                    "Not computed due to inconsistent historical event semantics."
                )
            pct = _safe_pct(len(numerator), len(denominator))
            return f"{pct}%", ""

        rates = [
            ("search-to-recommendation-shown rate", "accommodation_recommendations_shown", "accommodation_search_started"),
            ("recommendation-to-room-view rate", "accommodation_rooms_viewed", "accommodation_recommendations_shown"),
            ("room-view-to-preview-start rate", "accommodation_preview_started", "accommodation_rooms_viewed"),
            ("preview-start-to-preview-completed rate", "accommodation_preview_completed", "accommodation_preview_started"),
            ("preview-completed-to-handoff rate", "accommodation_external_handoff_clicked", "accommodation_preview_completed"),
            ("search-to-handoff rate", "accommodation_external_handoff_clicked", "accommodation_search_started"),
        ]
        rate_rows = []
        for rate_name, num_stage, den_stage in rates:
            rate_value, note = _subset_rate(num_stage, den_stage)
            rate_rows.append(
                {
                    "flow_step": rate_name,
                    "count": "",
                    "rate": rate_value,
                    "notes": note,
                }
            )

        accommodation_rows = [
            {
                "flow_step": "Accommodation search sessions",
                "count": stage_counts["accommodation_search_started"],
                "rate": "",
                "notes": f"Source: {stage_sources['accommodation_search_started']}",
            },
            {
                "flow_step": "Recommendation shown sessions",
                "count": stage_counts["accommodation_recommendations_shown"],
                "rate": "",
                "notes": f"Source: {stage_sources['accommodation_recommendations_shown']}",
            },
            {
                "flow_step": "Room-view sessions",
                "count": stage_counts["accommodation_rooms_viewed"],
                "rate": "",
                "notes": f"Source: {stage_sources['accommodation_rooms_viewed']}",
            },
            {
                "flow_step": "Preview-start sessions",
                "count": stage_counts["accommodation_preview_started"],
                "rate": "",
                "notes": f"Source: {stage_sources['accommodation_preview_started']}",
            },
            {
                "flow_step": "Preview-completed sessions",
                "count": stage_counts["accommodation_preview_completed"],
                "rate": "",
                "notes": f"Source: {stage_sources['accommodation_preview_completed']}",
            },
            {
                "flow_step": "External-handoff sessions",
                "count": stage_counts["accommodation_external_handoff_clicked"],
                "rate": "",
                "notes": f"Source: {stage_sources['accommodation_external_handoff_clicked']}",
            },
            {
                "flow_step": "Recommendation sessions from RecommendationResult (supporting evidence)",
                "count": len(recommendation_sessions),
                "rate": "",
                "notes": "Distinct RecommendationResult.context_json.session_id for accommodation intents.",
            },
        ]
        accommodation_rows.extend(rate_rows)

        # Tour conversion (status-based; Pending has no created_at field).
        pending_qs = Pending.objects.all()
        submitted_count = pending_qs.count()
        pending_count = pending_qs.filter(status__iexact="pending").count()
        accepted_count = pending_qs.filter(status__iexact="accepted").count()
        declined_count = pending_qs.filter(status__iexact="declined").count()

        # Optional event-based starts/submitted for future clean windows.
        started_event_sessions = _session_set_for_refs(event_qs, {"tour_booking_started"})
        submitted_event_sessions = _session_set_for_refs(event_qs, {"tour_booking_submitted"})
        approved_event_sessions = _session_set_for_refs(event_qs, {"tour_booking_approved"})
        declined_event_sessions = _session_set_for_refs(event_qs, {"tour_booking_declined"})

        tour_rows = [
            {
                "flow_step": "Tour booking starts (event sessions)",
                "count": len(started_event_sessions),
                "rate": "",
                "notes": "Event name: tour_booking_started.",
            },
            {
                "flow_step": "Tour bookings submitted (event sessions)",
                "count": len(submitted_event_sessions),
                "rate": (
                    f"{_safe_pct(len(submitted_event_sessions), len(started_event_sessions))}%"
                    if started_event_sessions and submitted_event_sessions.issubset(started_event_sessions)
                    else "Not computed due to inconsistent historical event semantics."
                    if started_event_sessions
                    else "Not currently logged"
                ),
                "notes": "Event name: tour_booking_submitted.",
            },
            {
                "flow_step": "Tour bookings submitted (Pending records)",
                "count": submitted_count,
                "rate": "",
                "notes": "Pending is the authoritative internal submission record.",
            },
            {
                "flow_step": "Pending tour bookings (Pending records)",
                "count": pending_count,
                "rate": f"{_safe_pct(pending_count, submitted_count)}%" if submitted_count else "",
                "notes": "Pending.status = Pending.",
            },
            {
                "flow_step": "Accepted/approved tour bookings (Pending records)",
                "count": accepted_count,
                "rate": f"{_safe_pct(accepted_count, submitted_count)}%" if submitted_count else "",
                "notes": "Pending.status = Accepted.",
            },
            {
                "flow_step": "Declined tour bookings (Pending records)",
                "count": declined_count,
                "rate": f"{_safe_pct(declined_count, submitted_count)}%" if submitted_count else "",
                "notes": "Pending.status = Declined.",
            },
            {
                "flow_step": "Approved status events (session count)",
                "count": len(approved_event_sessions),
                "rate": "",
                "notes": "Event name: tour_booking_approved.",
            },
            {
                "flow_step": "Declined status events (session count)",
                "count": len(declined_event_sessions),
                "rate": "",
                "notes": "Event name: tour_booking_declined.",
            },
        ]

        email_rows = []
        for label, endpoints in EMAIL_METRIC_ENDPOINTS.items():
            sent_count = metric_qs.filter(endpoint=endpoints["sent"], success_flag=True).count()
            failed_count = metric_qs.filter(
                Q(endpoint=endpoints["failed"]) | Q(endpoint=endpoints["sent"], success_flag=False)
            ).count()
            total_attempts = sent_count + failed_count
            success_rate = _safe_pct(sent_count, total_attempts) if total_attempts else None
            email_rows.append(
                {
                    "email_type": label,
                    "sent_count": sent_count if total_attempts else "Not currently logged",
                    "failed_count": failed_count if total_attempts else "Not currently logged",
                    "evidence_notes": (
                        f"Metric endpoints: {endpoints['sent']} / {endpoints['failed']}."
                        if total_attempts
                        else "Email flow implemented but sent-count logging is not available for this window."
                    ),
                    "success_rate": f"{success_rate}%" if success_rate is not None else "Not currently logged",
                }
            )

        missing_data_rows = [
            {
                "gap": "Pending.created_at not available",
                "impact": "Date-window filtering for authoritative tour submissions is limited.",
                "recommended_field_or_event": "Add created_at + updated_at in Pending (with safe migration plan).",
            },
            {
                "gap": "Historical accommodation events use mixed legacy semantics",
                "impact": "Some cross-stage rates are not computable as strict subsets.",
                "recommended_field_or_event": "Use new stage names only after a clean cutover date.",
            },
            {
                "gap": "Email dispatch logs may be absent in historical window",
                "impact": "Sent/failed counts may show 'Not currently logged'.",
                "recommended_field_or_event": "Keep SystemMetricLog email events enabled and verify in production.",
            },
        ]

        payload = {
            "generated_at": timezone.now().isoformat(),
            "window": {
                "start_date": _iso(start_date),
                "end_date": _iso(end_date),
            },
            "notes": {
                "safety": "Read-only aggregation from runtime tables; no business data modified.",
                "accommodation_scope": "Accommodation is preview-only: recommendation -> room viewing -> cost preview -> external handoff.",
                "tour_scope": "Tour booking is full internal workflow with pending -> accepted/declined.",
            },
            "data_sources": {
                "recommendation_events_table": "ai_chatbot.RecommendationEvent",
                "recommendation_results_table": "ai_chatbot.RecommendationResult",
                "tour_submissions_table": "guest_app.Pending",
                "tour_bookings_table": "guest_app.TourBooking",
                "email_metric_table": "ai_chatbot.SystemMetricLog (module=email)",
            },
            "accommodation_conversion": accommodation_rows,
            "tour_booking_conversion": tour_rows,
            "email_notification": email_rows,
            "missing_data_report": missing_data_rows,
        }

        json_path = out_dir / f"{prefix}.json"
        accom_csv = out_dir / f"{prefix}_accommodation_conversion.csv"
        tour_csv = out_dir / f"{prefix}_tour_booking_conversion.csv"
        email_csv = out_dir / f"{prefix}_email_notification.csv"
        missing_csv = out_dir / f"{prefix}_missing_data_report.csv"

        json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

        with accom_csv.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=["flow_step", "count", "rate", "notes"])
            writer.writeheader()
            writer.writerows(accommodation_rows)

        with tour_csv.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=["flow_step", "count", "rate", "notes"])
            writer.writeheader()
            writer.writerows(tour_rows)

        with email_csv.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=["email_type", "sent_count", "failed_count", "success_rate", "evidence_notes"],
            )
            writer.writeheader()
            writer.writerows(email_rows)

        with missing_csv.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=["gap", "impact", "recommended_field_or_event"])
            writer.writeheader()
            writer.writerows(missing_data_rows)

        self.stdout.write(
            json.dumps(
                {
                    "status": "ok",
                    "outputs": {
                        "json": str(json_path),
                        "accommodation_conversion_csv": str(accom_csv),
                        "tour_booking_conversion_csv": str(tour_csv),
                        "email_notification_csv": str(email_csv),
                        "missing_data_report_csv": str(missing_csv),
                    },
                    "headline": {
                        "accommodation_search_sessions": stage_counts["accommodation_search_started"],
                        "accommodation_handoff_sessions": stage_counts["accommodation_external_handoff_clicked"],
                        "tour_submitted_records": submitted_count,
                        "tour_accepted_records": accepted_count,
                        "tour_declined_records": declined_count,
                    },
                },
                indent=2,
            )
        )
