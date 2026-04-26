from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from accom_app.models import Other_Estab, Room as AccomAppRoom, RoomsGuestAdd, Summary as AccomSummary, mies_table
from admin_app.models import AccommodationCertification, Accomodation, OwnerMonthlyReport, Room
from admin_app.prototype_scope import prototype_accommodation_q, prototype_marker_reasons
from guest_app.models import AccommodationBooking, Billing


class Command(BaseCommand):
    help = (
        "Audit prototype/demo accommodations, classify deletion safety, and optionally "
        "apply safe deactivation (is_active=False) without hard deletion."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply-deactivation",
            action="store_true",
            help="Apply safe non-destructive deactivation for detected prototype accommodations.",
        )
        parser.add_argument(
            "--report-json",
            default="thesis_data_templates/prototype_accommodation_audit.json",
            help="Output JSON path for audit report.",
        )
        parser.add_argument(
            "--report-csv",
            default="thesis_data_templates/prototype_accommodation_audit.csv",
            help="Output CSV path for audit report.",
        )

    def handle(self, *args, **options):
        apply_deactivation = bool(options.get("apply_deactivation"))
        report_json = Path(str(options.get("report_json") or "").strip())
        report_csv = Path(str(options.get("report_csv") or "").strip())

        prototype_qs = (
            Accomodation.objects.filter(prototype_accommodation_q(accommodation_path=""))
            .select_related("owner")
            .order_by("company_name", "accom_id")
        )
        rows = []
        deactivated_count = 0

        for accom in prototype_qs:
            deps = self._dependency_counts(accom)
            safety = self._classify_safety(accom=accom, deps=deps)
            action_taken = "none"

            if apply_deactivation and accom.is_active:
                with transaction.atomic():
                    accom.is_active = False
                    deactivation_note = (
                        f"[prototype-deactivated:{timezone.now().date().isoformat()}] "
                        "Automatically hidden from guest/chatbot recommendation scope pending cleanup."
                    )
                    existing_reason = str(accom.rejection_reason or "").strip()
                    if deactivation_note not in existing_reason:
                        accom.rejection_reason = (
                            f"{existing_reason}\n{deactivation_note}".strip()
                            if existing_reason
                            else deactivation_note
                        )
                    accom.save(update_fields=["is_active", "rejection_reason"])
                action_taken = "deactivated"
                deactivated_count += 1

            owner = getattr(accom, "owner", None)
            owner_groups = []
            if owner is not None:
                try:
                    owner_groups = list(owner.groups.values_list("name", flat=True))
                except Exception:
                    owner_groups = []

            rows.append(
                {
                    "accom_id": int(accom.accom_id),
                    "company_name": str(accom.company_name or ""),
                    "email_address": str(accom.email_address or ""),
                    "approval_status": str(accom.approval_status or ""),
                    "is_active": bool(accom.is_active),
                    "owner_pk": str(getattr(owner, "pk", "") or ""),
                    "owner_email": str(getattr(owner, "email", "") or ""),
                    "owner_groups": owner_groups,
                    "prototype_reasons": prototype_marker_reasons(accom),
                    "dependencies": deps,
                    "safety_classification": safety,
                    "action_taken": action_taken,
                }
            )

        summary = {
            "generated_at": datetime.utcnow().isoformat() + "Z",
            "apply_deactivation": apply_deactivation,
            "prototype_count": prototype_qs.count(),
            "deactivated_count": deactivated_count,
            "settings_scope": {
                "approved_ids": getattr(settings, "TOURISM_APPROVED_ACCOMMODATION_IDS", ""),
                "approved_names": getattr(settings, "TOURISM_APPROVED_ACCOMMODATION_NAMES", ""),
            },
        }
        payload = {"summary": summary, "rows": rows}
        self._write_reports(report_json, report_csv, payload)

        self.stdout.write(
            self.style.SUCCESS(
                f"Prototype audit complete. Found {summary['prototype_count']} records; "
                f"deactivated {deactivated_count}."
            )
        )
        self.stdout.write(f"JSON report: {report_json}")
        self.stdout.write(f"CSV report: {report_csv}")

    def _dependency_counts(self, accom):
        admin_room_ids = list(Room.objects.filter(accommodation=accom).values_list("room_id", flat=True))
        return {
            "admin_rooms": len(admin_room_ids),
            "admin_room_owner_details": Room.objects.filter(
                accommodation=accom,
                owner_details__isnull=False,
            ).count(),
            "accommodation_certifications": AccommodationCertification.objects.filter(accommodation=accom).count(),
            "owner_monthly_reports": OwnerMonthlyReport.objects.filter(accommodation=accom).count(),
            "guest_accommodation_bookings": AccommodationBooking.objects.filter(accommodation=accom).count(),
            "billing_records": Billing.objects.filter(booking__accommodation=accom).count(),
            "accom_app_other_estab": Other_Estab.objects.filter(accom_id=accom).count(),
            "accom_app_summary": AccomSummary.objects.filter(accom_id=accom).count(),
            "accom_app_rooms": AccomAppRoom.objects.filter(accom_id=accom).count(),
            "accom_app_rooms_guest_add": RoomsGuestAdd.objects.filter(accom_id=accom).count(),
            "accom_app_mies_events": mies_table.objects.filter(accom_id=accom).count(),
        }

    def _classify_safety(self, *, accom, deps):
        hard_dependency_total = (
            int(deps.get("owner_monthly_reports", 0))
            + int(deps.get("guest_accommodation_bookings", 0))
            + int(deps.get("billing_records", 0))
            + int(deps.get("accom_app_other_estab", 0))
            + int(deps.get("accom_app_summary", 0))
            + int(deps.get("accom_app_rooms_guest_add", 0))
            + int(deps.get("accom_app_mies_events", 0))
        )
        child_dependency_total = (
            int(deps.get("admin_rooms", 0))
            + int(deps.get("admin_room_owner_details", 0))
            + int(deps.get("accommodation_certifications", 0))
            + int(deps.get("accom_app_rooms", 0))
        )
        has_owner = bool(getattr(accom, "owner_id", None))

        if hard_dependency_total > 0:
            return "NOT SAFE TO DELETE YET"
        if child_dependency_total > 0 or has_owner:
            return "SAFE TO DELETE AFTER DEACTIVATION"
        return "SAFE TO HIDE/DEACTIVATE"

    def _write_reports(self, json_path: Path, csv_path: Path, payload: dict):
        json_path.parent.mkdir(parents=True, exist_ok=True)
        csv_path.parent.mkdir(parents=True, exist_ok=True)

        json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

        rows = payload.get("rows") if isinstance(payload.get("rows"), list) else []
        fieldnames = [
            "accom_id",
            "company_name",
            "email_address",
            "approval_status",
            "is_active",
            "owner_pk",
            "owner_email",
            "owner_groups",
            "prototype_reasons",
            "safety_classification",
            "action_taken",
            "dependencies",
        ]
        with csv_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            for row in rows:
                writer.writerow(
                    {
                        **row,
                        "owner_groups": json.dumps(row.get("owner_groups") or []),
                        "prototype_reasons": json.dumps(row.get("prototype_reasons") or []),
                        "dependencies": json.dumps(row.get("dependencies") or {}),
                    }
                )
