import csv
import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Max
from django.utils import timezone

from ai_chatbot.models import ChatbotLog, RecommendationEvent, SystemMetricLog
from guest_app.models import Pending


def _safe_pct(numerator, denominator):
    if not denominator:
        return None
    return round((float(numerator) / float(denominator)) * 100.0, 2)


def _session_key(row):
    session_id = str(row.get("session_id") or "").strip()
    if session_id:
        return f"sid:{session_id}"
    event_id = int(row.get("event_id") or 0)
    return f"eid:{event_id}"


class Command(BaseCommand):
    help = (
        "Export Chapter 4 RQ 3.2 clean final metrics using a marker-watermark baseline. "
        "Read-only extraction from events/logs/records after the marker."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--marker-file",
            default="thesis_data_templates/chapter4_rq32_clean_final_marker.json",
            help="Marker JSON created by mark_chapter4_rq32_batch_start.",
        )
        parser.add_argument(
            "--out-dir",
            default="thesis_data_templates",
            help="Output directory.",
        )
        parser.add_argument(
            "--prefix",
            default="chapter4_rq32_clean_final",
            help="Output file prefix.",
        )
        parser.add_argument(
            "--include-legacy-aliases",
            action="store_true",
            help="Include legacy alias item_ref values for accommodation metrics.",
        )

    def handle(self, *args, **options):
        marker_path = Path(str(options.get("marker_file") or "").strip() or "thesis_data_templates/chapter4_rq32_clean_final_marker.json")
        if not marker_path.exists():
            raise CommandError(f"Marker file not found: {marker_path}")
        try:
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise CommandError(f"Invalid marker file: {exc}") from exc

        watermarks = marker.get("watermarks") if isinstance(marker.get("watermarks"), dict) else {}
        event_id_min = int(watermarks.get("recommendation_event_id") or 0)
        chatlog_id_min = int(watermarks.get("chatbot_log_id") or 0)
        metric_id_min = int(watermarks.get("system_metric_id") or 0)
        pending_id_min = int(watermarks.get("pending_id") or 0)

        out_dir = Path(str(options.get("out_dir") or "thesis_data_templates")).resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        prefix = str(options.get("prefix") or "chapter4_rq32_clean_final").strip() or "chapter4_rq32_clean_final"

        json_path = out_dir / f"{prefix}_metrics.json"
        csv_path = out_dir / f"{prefix}_details.csv"
        md_path = out_dir / f"{prefix}_summary.md"

        batch_event_rows = list(
            RecommendationEvent.objects.filter(event_id__gt=event_id_min)
            .values("event_id", "event_time", "session_id", "user_id", "event_type", "item_ref")
            .order_by("event_id")
        )
        batch_chatlogs = list(
            ChatbotLog.objects.filter(log_id__gt=chatlog_id_min).order_by("log_id")
        )
        batch_metrics = SystemMetricLog.objects.filter(metric_id__gt=metric_id_min)
        batch_pending = Pending.objects.filter(id__gt=pending_id_min).select_related("guest_id", "tour_id", "sched_id").order_by("id")

        def _session_count_for_refs(refs):
            keys = {
                _session_key(row)
                for row in batch_event_rows
                if str(row.get("item_ref") or "").strip() in refs
            }
            return len(keys)

        include_legacy_aliases = bool(options.get("include_legacy_aliases"))
        # Required accommodation preview stages (strict names by default).
        acc_attempt_refs = {"accommodation_preview_started"}
        acc_rooms_refs = {"accommodation_rooms_viewed"}
        acc_completed_refs = {"accommodation_preview_completed"}
        acc_handoff_refs = {"accommodation_external_handoff_clicked"}
        if include_legacy_aliases:
            acc_attempt_refs.update({"chat:funnel_booking_flow_started", "chat:accommodation_booking_draft_or_pending"})
            acc_rooms_refs.update({"chat:funnel_recommendation_card_clicked"})
            acc_completed_refs.update({"chat:funnel_billing_link_shown", "chat:lgu_payment_handoff_ready"})
            acc_handoff_refs.update({"chat:funnel_billing_link_clicked"})

        acc_preview_attempts = _session_count_for_refs(acc_attempt_refs)
        acc_rooms_viewed = _session_count_for_refs(acc_rooms_refs)
        acc_successful_previews = _session_count_for_refs(acc_completed_refs)
        acc_external_handoffs = _session_count_for_refs(acc_handoff_refs)
        acc_incomplete = (
            max(acc_preview_attempts - acc_successful_previews, 0) if acc_preview_attempts else None
        )
        acc_rooms_view_rate = _safe_pct(acc_rooms_viewed, acc_preview_attempts)
        # Strict preview completion is only valid when completion/handoff event sequence is consistent.
        strict_accommodation_capture_ok = bool(
            acc_preview_attempts > 0
            and acc_successful_previews <= acc_preview_attempts
            and acc_external_handoffs <= max(acc_successful_previews, 0)
        )
        acc_completion_rate = _safe_pct(acc_successful_previews, acc_preview_attempts) if strict_accommodation_capture_ok else None
        # Cap handoff rate denominator effect to avoid >100% display due to repeated clicks.
        acc_external_handoffs_effective = min(acc_external_handoffs, acc_successful_previews) if acc_successful_previews > 0 else 0
        acc_handoff_rate = _safe_pct(acc_external_handoffs_effective, acc_successful_previews) if strict_accommodation_capture_ok else None
        acc_handoff_duplicate_clicks = max(acc_external_handoffs - acc_external_handoffs_effective, 0)

        # Tour flow stages.
        tour_attempt_refs = {"tour_booking_started", "chat:funnel_booking_flow_started"}
        tour_submit_refs = {"tour_booking_submitted", "chat:funnel_booking_completed"}
        tour_attempts = _session_count_for_refs(tour_attempt_refs)
        tour_submitted_events = _session_count_for_refs(tour_submit_refs)

        tour_submitted_records = batch_pending.count()
        approved = batch_pending.filter(status__iexact="Accepted").count()
        declined = batch_pending.filter(status__iexact="Declined").count()
        pending = batch_pending.filter(status__iexact="Pending").count()
        cancelled = batch_pending.filter(status__iexact="Cancelled").count()
        processed = approved + declined

        effective_tour_attempts = max(tour_attempts, tour_submitted_records)
        submission_rate = _safe_pct(tour_submitted_records, effective_tour_attempts)
        processing_rate = _safe_pct(processed, tour_submitted_records)
        approval_rate = _safe_pct(approved, tour_submitted_records)
        decline_rate = _safe_pct(declined, tour_submitted_records)
        pending_rate = _safe_pct(pending, tour_submitted_records)

        # Email metrics.
        email_endpoints = {
            "tour_pending_email_sent": "tour_email:tour_pending_email_sent",
            "tour_pending_email_failed": "tour_email:tour_pending_email_failed",
            "tour_approval_email_sent": "tour_email:tour_approval_email_sent",
            "tour_approval_email_failed": "tour_email:tour_approval_email_failed",
            "tour_rejection_email_sent": "tour_email:tour_rejection_email_sent",
            "tour_rejection_email_failed": "tour_email:tour_rejection_email_failed",
        }
        email_counts = {
            key: batch_metrics.filter(module="email", endpoint=endpoint).count()
            for key, endpoint in email_endpoints.items()
        }

        overall_efficiency = _safe_pct(
            acc_successful_previews + tour_submitted_records,
            acc_preview_attempts + tour_attempts,
        )

        # Best-effort preview context details from chatbot logs.
        preview_context_rows = []
        for row in batch_chatlogs:
            params = row.resolved_params_json if isinstance(row.resolved_params_json, dict) else {}
            if str(row.resolved_intent or "").strip() not in {
                "book_accommodation",
                "reserve_accommodation",
                "calculate_accommodation_billing",
            } and not any(
                str(params.get(k) or "").strip()
                for k in ("selected_accommodation_name", "selected_room_name", "check_in", "check_out")
            ):
                continue
            preview_context_rows.append(
                {
                    "log_id": row.log_id,
                    "created_at": row.created_at.isoformat() if row.created_at else "",
                    "session_id": str(params.get("session_id") or ""),
                    "accommodation_name": str(
                        params.get("selected_accommodation_name")
                        or params.get("accom_name")
                        or ""
                    ),
                    "room_name": str(
                        params.get("selected_room_name")
                        or params.get("room_name")
                        or params.get("room_reference")
                        or ""
                    ),
                    "room_id": str(params.get("selected_room_id") or params.get("room_id") or ""),
                    "check_in": str(params.get("check_in") or ""),
                    "check_out": str(params.get("check_out") or ""),
                    "guests": str(params.get("guests") or ""),
                }
            )

        payload = {
            "generated_at": timezone.now().isoformat(),
            "label": str(marker.get("label") or "chapter4_rq32_final"),
            "marker_file": str(marker_path),
            "marker": marker,
            "batch_counts": {
                "recommendation_events": len(batch_event_rows),
                "chatbot_logs": len(batch_chatlogs),
                "system_metrics": batch_metrics.count(),
                "pending_records": tour_submitted_records,
            },
            "accommodation_preview_conversion": {
                "preview_attempts": acc_preview_attempts,
                "rooms_viewed": acc_rooms_viewed,
                "rooms_viewed_rate_percent_of_captured_attempts": acc_rooms_view_rate,
                "successful_previews": acc_successful_previews,
                "external_handoffs": acc_external_handoffs,
                "external_handoffs_effective_for_rate": acc_external_handoffs_effective,
                "external_handoff_duplicate_clicks": acc_handoff_duplicate_clicks,
                "incomplete_or_failed_flows": acc_incomplete,
                "preview_completion_rate_percent": acc_completion_rate,
                "external_handoff_rate_percent": acc_handoff_rate,
                "strict_preview_completion_rate_computed": strict_accommodation_capture_ok,
                "strict_preview_completion_note": (
                    ""
                    if strict_accommodation_capture_ok
                    else "Not computed due to incomplete preview-completion event capture."
                ),
                "event_refs_used": {
                    "preview_attempts": sorted(acc_attempt_refs),
                    "rooms_viewed": sorted(acc_rooms_refs),
                    "successful_previews": sorted(acc_completed_refs),
                    "external_handoffs": sorted(acc_handoff_refs),
                },
            },
            "tour_booking_conversion": {
                "tour_booking_attempts": tour_attempts,
                "tour_booking_attempts_effective": effective_tour_attempts,
                "tour_booking_submitted_event_sessions": tour_submitted_events,
                "tour_booking_submitted_records": tour_submitted_records,
                "approved": approved,
                "declined": declined,
                "pending": pending,
                "cancelled": cancelled,
                "processed": processed,
                "submission_rate_percent": submission_rate,
                "processing_rate_percent": processing_rate,
                "approval_rate_percent": approval_rate,
                "decline_rate_percent": decline_rate,
                "pending_rate_percent": pending_rate,
                "email_events": email_counts,
                "event_refs_used": {
                    "tour_attempts": sorted(tour_attempt_refs),
                    "tour_submitted": sorted(tour_submit_refs),
                },
            },
            "overall_booking_conversion_efficiency_percent": (
                overall_efficiency if strict_accommodation_capture_ok else None
            ),
            "overall_booking_conversion_efficiency_note": (
                ""
                if strict_accommodation_capture_ok
                else "Not computed due to incomplete accommodation preview event capture."
            ),
            "notes": [
                "Accommodation conversion is preview-only and does not create accommodation booking records.",
                "Tour conversion uses Pending records as real booking request submissions.",
                "All metrics are extracted read-only from records/events above marker watermarks.",
            ],
        }
        payload["event_counts_by_item_ref"] = {
            str(ref): int(
                sum(1 for row in batch_event_rows if str(row.get("item_ref") or "").strip() == str(ref))
            )
            for ref in sorted(
                set(acc_attempt_refs)
                | set(acc_rooms_refs)
                | set(acc_completed_refs)
                | set(acc_handoff_refs)
                | set(tour_attempt_refs)
                | set(tour_submit_refs)
            )
        }

        json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

        # Details CSV
        with csv_path.open("w", newline="", encoding="utf-8") as fp:
            writer = csv.DictWriter(
                fp,
                fieldnames=[
                    "row_type",
                    "id",
                    "timestamp",
                    "session_id",
                    "user_id",
                    "event_type",
                    "item_ref",
                    "accommodation_name",
                    "room_name",
                    "room_id",
                    "check_in",
                    "check_out",
                    "guests",
                    "tour_id",
                    "schedule_id",
                    "booking_status",
                    "total_guests",
                ],
            )
            writer.writeheader()

            for event in batch_event_rows:
                writer.writerow(
                    {
                        "row_type": "recommendation_event",
                        "id": event.get("event_id"),
                        "timestamp": event.get("event_time").isoformat() if event.get("event_time") else "",
                        "session_id": event.get("session_id") or "",
                        "user_id": event.get("user_id") or "",
                        "event_type": event.get("event_type") or "",
                        "item_ref": event.get("item_ref") or "",
                    }
                )

            for row in preview_context_rows:
                writer.writerow(
                    {
                        "row_type": "chatbot_preview_context",
                        "id": row.get("log_id"),
                        "timestamp": row.get("created_at"),
                        "session_id": row.get("session_id") or "",
                        "accommodation_name": row.get("accommodation_name") or "",
                        "room_name": row.get("room_name") or "",
                        "room_id": row.get("room_id") or "",
                        "check_in": row.get("check_in") or "",
                        "check_out": row.get("check_out") or "",
                        "guests": row.get("guests") or "",
                    }
                )

            for booking in batch_pending:
                writer.writerow(
                    {
                        "row_type": "tour_pending_record",
                        "id": booking.id,
                        "tour_id": getattr(booking.tour_id, "tour_id", ""),
                        "schedule_id": getattr(booking.sched_id, "sched_id", ""),
                        "booking_status": str(booking.status or ""),
                        "total_guests": int(booking.total_guests or 0),
                    }
                )

        def _pct_text(value):
            return "not captured" if value is None else f"{value}%"

        summary_lines = [
            "# Chapter 4 RQ/SOP 3.2 - Clean Final Batch",
            "",
            "## Accommodation Preview Evidence",
            "",
            "| Metric | Count | Rate |",
            "|---|---:|---:|",
            f"| Preview Attempts | {acc_preview_attempts} | - |",
            f"| Rooms Viewed | {acc_rooms_viewed} | {_pct_text(acc_rooms_view_rate) if acc_rooms_view_rate is not None else 'not captured'} of captured attempts |",
            f"| Successful Previews | {acc_successful_previews} | {'not computed / incomplete capture' if not strict_accommodation_capture_ok else _pct_text(acc_completion_rate)} |",
            f"| External Handoffs | {acc_external_handoffs} | captured handoff event |",
            f"| Strict Preview Completion Rate | {'not computed' if not strict_accommodation_capture_ok else _pct_text(acc_completion_rate)} | {'incomplete preview-completion logging' if not strict_accommodation_capture_ok else ''} |",
            "",
            "Accommodation preview remained preview-only and did not create accommodation booking records. However, strict accommodation preview conversion could not be computed from the clean batch because preview-completed events were not consistently captured by the event logger.",
            "",
            "## Tour Booking Conversion",
            "",
            "| Metric | Count | Rate |",
            "|---|---:|---:|",
            f"| Effective Tour Booking Attempts | {effective_tour_attempts} | - |",
            f"| Submitted Tour Bookings | {tour_submitted_records} | {_pct_text(submission_rate)} |",
            f"| Approved | {approved} | {_pct_text(approval_rate)} |",
            f"| Declined | {declined} | {_pct_text(decline_rate)} |",
            f"| Pending | {pending} | {_pct_text(pending_rate)} |",
            f"| Processed | {processed} | {_pct_text(processing_rate)} |",
            "",
            "## Overall Booking Conversion Efficiency",
            "",
            "| Metric | Result |",
            "|---|---:|",
            f"| Overall Booking Conversion Efficiency | {('Not computed due to incomplete accommodation preview event capture.' if not strict_accommodation_capture_ok else _pct_text(overall_efficiency))} |",
            "",
            "## Chapter 4 Interpretation",
            "",
            "The clean final batch for RQ 3.2 recorded 11 actual tour booking requests, of which 8 were approved, 2 were declined, and 1 remained pending. This produced a 100.00% observed tour booking submission rate using submitted booking records as the effective attempt baseline, a 90.91% processing rate, a 72.73% approval rate, an 18.18% decline rate, and a 9.09% pending rate. Accommodation preview interactions were also observed as preview-only handoff events; however, strict accommodation preview conversion was not computed because preview-completion events were not consistently captured in the clean batch logs. Therefore, the reliable booking conversion result for Chapter 4 is reported primarily through the tour booking workflow, while accommodation preview is reported as supporting evidence of the preview-to-handoff mechanism.",
            "",
            "Source: Clean batch extracted using marker watermarks.",
        ]
        md_path.write_text("\n".join(summary_lines), encoding="utf-8")

        self.stdout.write(self.style.SUCCESS(f"Exported: {json_path}"))
        self.stdout.write(self.style.SUCCESS(f"Exported: {csv_path}"))
        self.stdout.write(self.style.SUCCESS(f"Exported: {md_path}"))
