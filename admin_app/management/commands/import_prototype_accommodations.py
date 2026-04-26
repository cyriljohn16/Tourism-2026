import csv
import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from accom_app.models import AuthoritativeRoomDetails
from admin_app.models import Accomodation, Room


ALLOWED_COMPANY_TYPES = {"hotel", "inn"}
ALLOWED_ROOM_STATUSES = {"AVAILABLE", "OCCUPIED", "UNAVAILABLE"}


@dataclass
class OwnerResolution:
    user: object | None
    source: str
    created: bool
    error: str


class Command(BaseCommand):
    help = (
        "Import thesis-safe prototype accommodations into live tables with "
        "owner/group linkage, room upsert, and eligibility validation report."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dataset",
            default="thesis_data_templates/prototype_accommodations_thesis_safe.json",
            help="Path to prototype accommodation dataset (JSON preferred).",
        )
        parser.add_argument(
            "--owner-map-csv",
            default="",
            help=(
                "Optional CSV mapping with columns: record_id,owner_email,"
                "owner_username,first_name,last_name,password."
            ),
        )
        parser.add_argument(
            "--owner-email",
            default="",
            help="Optional shared owner email to link all imported accommodations.",
        )
        parser.add_argument(
            "--create-owners",
            action="store_true",
            help="Create owner accounts when no mapped/shared owner exists.",
        )
        parser.add_argument(
            "--allow-unlinked-owners",
            action="store_true",
            help=(
                "Allow importing accommodations without owner linkage. "
                "These records are not recommendation-eligible."
            ),
        )
        parser.add_argument(
            "--owner-default-password",
            default="OwnerPass123!",
            help="Default password used when creating owner accounts.",
        )
        parser.add_argument(
            "--accommodation-password",
            default="AccomPass123!",
            help="Default password set for newly created accommodation records.",
        )
        parser.add_argument(
            "--report-json",
            default="thesis_data_templates/prototype_import_report.json",
            help="Output path for JSON import report.",
        )
        parser.add_argument(
            "--report-csv",
            default="thesis_data_templates/prototype_import_report.csv",
            help="Output path for CSV import report.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Validate and print actions without writing database changes.",
        )

    def handle(self, *args, **options):
        dataset_path = Path(str(options["dataset"]).strip())
        owner_map_path = Path(str(options["owner_map_csv"]).strip()) if options["owner_map_csv"] else None
        report_json_path = Path(str(options["report_json"]).strip())
        report_csv_path = Path(str(options["report_csv"]).strip())
        dry_run = bool(options["dry_run"])
        shared_owner_email = str(options["owner_email"] or "").strip().lower()
        create_owners = bool(options["create_owners"])
        allow_unlinked = bool(options["allow_unlinked_owners"])
        owner_default_password = str(options["owner_default_password"] or "OwnerPass123!").strip()
        accommodation_password = str(options["accommodation_password"] or "AccomPass123!").strip()

        if not dataset_path.exists():
            raise CommandError(f"Dataset file not found: {dataset_path}")

        owner_map = self._load_owner_map(owner_map_path) if owner_map_path else {}
        if not (shared_owner_email or owner_map or create_owners or allow_unlinked):
            raise CommandError(
                "Owner linkage is required. Provide --owner-email, --owner-map-csv, "
                "--create-owners, or explicitly pass --allow-unlinked-owners."
            )

        records = self._load_dataset(dataset_path)
        if not records:
            raise CommandError("Dataset has no entries to import.")

        if dry_run:
            self.stdout.write(self.style.WARNING("Running in dry-run mode (no DB writes)."))

        rows = []
        counters = {
            "processed": 0,
            "imported": 0,
            "updated": 0,
            "skipped_out_of_scope": 0,
            "skipped_errors": 0,
            "owner_created": 0,
            "owner_linked": 0,
            "rooms_created": 0,
            "rooms_updated": 0,
        }

        for raw_record in records:
            counters["processed"] += 1
            result = self._process_record(
                raw_record=raw_record,
                owner_map=owner_map,
                shared_owner_email=shared_owner_email,
                create_owners=create_owners,
                allow_unlinked=allow_unlinked,
                owner_default_password=owner_default_password,
                accommodation_password=accommodation_password,
                dry_run=dry_run,
            )
            rows.append(result)

            status = result.get("status")
            if status == "imported":
                counters["imported"] += 1
            elif status == "updated":
                counters["updated"] += 1
            elif status == "skipped_out_of_scope":
                counters["skipped_out_of_scope"] += 1
            else:
                counters["skipped_errors"] += 1

            if result.get("owner_created"):
                counters["owner_created"] += 1
            if result.get("owner_linked"):
                counters["owner_linked"] += 1
            counters["rooms_created"] += int(result.get("rooms_created") or 0)
            counters["rooms_updated"] += int(result.get("rooms_updated") or 0)

        report = {
            "dataset": str(dataset_path),
            "dry_run": dry_run,
            "summary": counters,
            "rows": rows,
        }
        self._write_reports(report_json_path, report_csv_path, report)

        self.stdout.write(
            self.style.SUCCESS(
                "Prototype import finished: "
                f"{counters['imported']} imported, {counters['updated']} updated, "
                f"{counters['skipped_out_of_scope']} skipped(out-of-scope), "
                f"{counters['skipped_errors']} skipped(errors)."
            )
        )
        self.stdout.write(f"JSON report: {report_json_path}")
        self.stdout.write(f"CSV report: {report_csv_path}")

    def _load_dataset(self, dataset_path: Path) -> list[dict]:
        suffix = dataset_path.suffix.lower()
        if suffix == ".json":
            payload = json.loads(dataset_path.read_text(encoding="utf-8-sig"))
            if isinstance(payload, dict):
                entries = payload.get("entries")
                if isinstance(entries, list):
                    return entries
                raise CommandError("JSON dataset must contain an 'entries' array.")
            if isinstance(payload, list):
                return payload
            raise CommandError("Unsupported JSON dataset structure.")

        if suffix == ".csv":
            # CSV is accepted for compatibility, but does not include room-level details.
            with dataset_path.open("r", encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            for row in rows:
                row.setdefault("rooms", [])
            return rows

        raise CommandError(f"Unsupported dataset format: {dataset_path.suffix}")

    def _load_owner_map(self, csv_path: Path) -> dict[str, dict]:
        if not csv_path.exists():
            raise CommandError(f"Owner map CSV not found: {csv_path}")
        with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            rows = {}
            for row in reader:
                record_id = str(row.get("record_id") or "").strip()
                if not record_id:
                    continue
                rows[record_id] = {
                    "owner_email": str(row.get("owner_email") or "").strip().lower(),
                    "owner_username": str(row.get("owner_username") or "").strip(),
                    "first_name": str(row.get("first_name") or "").strip(),
                    "last_name": str(row.get("last_name") or "").strip(),
                    "password": str(row.get("password") or "").strip(),
                }
            return rows

    def _process_record(
        self,
        *,
        raw_record: dict,
        owner_map: dict[str, dict],
        shared_owner_email: str,
        create_owners: bool,
        allow_unlinked: bool,
        owner_default_password: str,
        accommodation_password: str,
        dry_run: bool,
    ) -> dict:
        record_id = str(raw_record.get("record_id") or "").strip()
        company_name = str(raw_record.get("company_name") or "").strip()
        company_type_raw = str(raw_record.get("company_type") or "").strip()
        company_type_norm = company_type_raw.lower()

        base_row = {
            "record_id": record_id,
            "company_name": company_name,
            "company_type": company_type_raw,
            "status": "",
            "message": "",
            "owner_email": "",
            "owner_created": False,
            "owner_linked": False,
            "rooms_created": 0,
            "rooms_updated": 0,
            "catalog_visible": False,
            "recommendation_eligible": False,
            "booking_eligible": False,
        }

        if company_type_norm not in ALLOWED_COMPANY_TYPES:
            base_row["status"] = "skipped_out_of_scope"
            base_row["message"] = "company_type is outside thesis scope (Hotel/Inn only)"
            return base_row

        owner_resolution = self._resolve_owner(
            raw_record=raw_record,
            owner_map=owner_map,
            shared_owner_email=shared_owner_email,
            create_owners=create_owners,
            owner_default_password=owner_default_password,
            dry_run=dry_run,
        )
        if owner_resolution.error:
            if not allow_unlinked:
                base_row["status"] = "skipped_error"
                base_row["message"] = owner_resolution.error
                return base_row
            owner = None
            owner_source = "unlinked_allowed"
            owner_created = False
        else:
            owner = owner_resolution.user
            owner_source = owner_resolution.source
            owner_created = owner_resolution.created

        location = str(raw_record.get("location") or "").strip()
        email_address = str(raw_record.get("email_address") or "").strip().lower()
        phone_number = str(raw_record.get("phone_number") or "").strip()
        description = str(raw_record.get("description") or "").strip()
        amenities_text = str(raw_record.get("accommodation_amenities") or "").strip()
        approval_status = self._normalize_approval_status(raw_record.get("approval_status"))
        is_active = self._to_bool(raw_record.get("is_active"), default=True)
        rooms = self._extract_rooms(raw_record)

        if not company_name or not email_address or not location:
            base_row["status"] = "skipped_error"
            base_row["message"] = "Missing required accommodation fields (company_name/email/location)."
            return base_row

        if not rooms:
            base_row["status"] = "skipped_error"
            base_row["message"] = "No room entries found. Booking/recommendation path requires rooms."
            return base_row

        if dry_run:
            dry_flags = self._simulate_eligibility_flags(
                owner_present=owner is not None,
                owner_linked=owner is not None,
                owner_active=True,
                owner_group_ok=owner is not None,
                approval_status=approval_status,
                is_active=is_active,
                rooms=rooms,
            )
            base_row.update(
                {
                    "status": "imported",
                    "message": f"dry-run via {owner_source}",
                    "owner_email": str(getattr(owner, "email", "") if owner is not None else ""),
                    "owner_created": owner_created,
                    "owner_linked": owner is not None,
                    "rooms_created": len(rooms),
                    "rooms_updated": 0,
                    "catalog_visible": dry_flags["catalog_visible"],
                    "recommendation_eligible": dry_flags["recommendation_eligible"],
                    "booking_eligible": dry_flags["booking_eligible"],
                }
            )
            return base_row

        with transaction.atomic():
            accommodation, created = Accomodation.objects.get_or_create(
                email_address=email_address,
                defaults={
                    "company_name": company_name,
                    "company_type": company_type_raw.title(),
                    "location": location,
                    "phone_number": phone_number or "00000000000",
                    "description": description,
                    "accommodation_amenities": amenities_text,
                    "approval_status": approval_status,
                    "status": approval_status,
                    "is_active": is_active,
                    "owner": owner,
                    "password": accommodation_password,
                },
            )

            if not created:
                accommodation.company_name = company_name
                accommodation.company_type = company_type_raw.title()
                accommodation.location = location
                accommodation.phone_number = phone_number or accommodation.phone_number or "00000000000"
                accommodation.description = description
                accommodation.accommodation_amenities = amenities_text
                accommodation.approval_status = approval_status
                accommodation.status = approval_status
                accommodation.is_active = is_active
                accommodation.owner = owner
                if not accommodation.password:
                    accommodation.password = accommodation_password
                accommodation.save()

            owner_linked = owner is not None and accommodation.owner_id == getattr(owner, "pk", None)
            owner_email_out = str(getattr(owner, "email", "") or "")

            rooms_created = 0
            rooms_updated = 0
            for room_payload in rooms:
                room_name = str(room_payload["room_name"]).strip()
                defaults = {
                    "person_limit": int(room_payload["person_limit"]),
                    "current_availability": int(room_payload["current_availability"]),
                    "price_per_night": Decimal(str(room_payload["price_per_night"])),
                    "status": str(room_payload["status"]).upper(),
                }
                room_obj, room_created = Room.objects.update_or_create(
                    accommodation=accommodation,
                    room_name=room_name,
                    defaults=defaults,
                )
                if room_created:
                    rooms_created += 1
                else:
                    rooms_updated += 1

                details, _ = AuthoritativeRoomDetails.objects.get_or_create(room=room_obj)
                details.room_type = str(room_payload.get("room_type") or room_name)[:100]
                details.amenities = json.dumps(room_payload.get("amenities") or [], ensure_ascii=True)
                details.save(update_fields=["room_type", "amenities", "updated_at"])

            flags = self._compute_runtime_flags(accommodation)

            base_row.update(
                {
                    "status": "imported" if created else "updated",
                    "message": f"owner_source={owner_source}",
                    "owner_email": owner_email_out,
                    "owner_created": owner_created,
                    "owner_linked": owner_linked,
                    "rooms_created": rooms_created,
                    "rooms_updated": rooms_updated,
                    "catalog_visible": flags["catalog_visible"],
                    "recommendation_eligible": flags["recommendation_eligible"],
                    "booking_eligible": flags["booking_eligible"],
                }
            )
            return base_row

    def _resolve_owner(
        self,
        *,
        raw_record: dict,
        owner_map: dict[str, dict],
        shared_owner_email: str,
        create_owners: bool,
        owner_default_password: str,
        dry_run: bool,
    ) -> OwnerResolution:
        user_model = get_user_model()
        record_id = str(raw_record.get("record_id") or "").strip()
        map_row = owner_map.get(record_id) or {}

        owner_email = str(map_row.get("owner_email") or "").strip().lower()
        owner_username = str(map_row.get("owner_username") or "").strip()
        first_name = str(map_row.get("first_name") or "").strip() or "Prototype"
        last_name = str(map_row.get("last_name") or "").strip() or "Owner"
        password = str(map_row.get("password") or "").strip() or owner_default_password

        source = "owner_map"
        if not owner_email and shared_owner_email:
            owner_email = shared_owner_email
            source = "shared_owner"
        if not owner_email and create_owners:
            rid = (record_id or "record").lower()
            owner_email = f"{rid}_owner@placeholder.local"
            source = "auto_per_record"
        if not owner_email:
            return OwnerResolution(user=None, source="none", created=False, error="Owner mapping is missing.")

        if not owner_username:
            owner_username = owner_email.split("@")[0][:100]
        if not owner_username:
            owner_username = f"owner_{(record_id or 'record').lower()}"[:100]

        user = user_model.objects.filter(email__iexact=owner_email).first()
        created = False

        if user is None and source == "shared_owner":
            return OwnerResolution(
                user=None,
                source=source,
                created=False,
                error=f"Shared owner not found: {owner_email}",
            )

        if user is None and not create_owners and source != "owner_map":
            return OwnerResolution(
                user=None,
                source=source,
                created=False,
                error=f"Owner not found and auto-create is disabled: {owner_email}",
            )

        if user is None and dry_run:
            return OwnerResolution(
                user=self._dry_user_stub(owner_email),
                source=source,
                created=True,
                error="",
            )

        if user is None:
            user = self._create_owner_user(
                user_model=user_model,
                username=owner_username,
                email=owner_email,
                first_name=first_name,
                last_name=last_name,
                password=password,
            )
            created = True

        if not dry_run:
            self._ensure_owner_group_membership(user)

        return OwnerResolution(user=user, source=source, created=created, error="")

    def _create_owner_user(self, *, user_model, username, email, first_name, last_name, password):
        # Guest model has required profile fields that generic create_user does not set.
        base_username = username[:80] or "owner"
        candidate = base_username
        counter = 1
        while user_model.objects.filter(username__iexact=candidate).exists():
            counter += 1
            candidate = f"{base_username}{counter}"

        user = user_model.objects.create_user(
            username=candidate,
            email=email,
            password=password,
            first_name=first_name or "Prototype",
            last_name=last_name or "Owner",
            country_of_origin="Philippines",
            city="Bayawan City",
            phone_number="09000000000",
            sex="M",
        )
        user.is_active = True
        user.save(update_fields=["is_active"])
        return user

    def _ensure_owner_group_membership(self, user):
        pending_group, _ = Group.objects.get_or_create(name="accommodation_owner_pending")
        approved_group, _ = Group.objects.get_or_create(name="accommodation_owner")
        declined_group, _ = Group.objects.get_or_create(name="accommodation_owner_declined")

        user.groups.remove(pending_group, declined_group)
        user.groups.add(approved_group)
        if not bool(getattr(user, "is_active", True)):
            user.is_active = True
            user.save(update_fields=["is_active"])

    def _extract_rooms(self, record: dict) -> list[dict]:
        rooms_value = record.get("rooms")
        if isinstance(rooms_value, str):
            try:
                rooms_value = json.loads(rooms_value)
            except Exception:
                rooms_value = []
        if not isinstance(rooms_value, list):
            return []

        normalized_rooms = []
        for idx, room in enumerate(rooms_value, start=1):
            if not isinstance(room, dict):
                continue

            room_name = str(room.get("room_name") or room.get("name") or "").strip()
            room_type = str(room.get("room_type") or room_name or f"Room {idx}").strip()
            if not room_name:
                room_name = room_type or f"Room {idx}"

            person_limit = self._safe_int(room.get("person_limit"), default=1)
            if person_limit < 1:
                person_limit = 1

            current_availability = self._safe_int(
                room.get("current_availability"),
                default=person_limit,
            )
            if current_availability < 0:
                current_availability = 0
            if current_availability > person_limit:
                current_availability = person_limit

            price = self._safe_decimal(room.get("price_per_night"), default=Decimal("0"))
            if price <= 0:
                price = Decimal("1.00")

            status = str(room.get("status") or "AVAILABLE").strip().upper()
            if status not in ALLOWED_ROOM_STATUSES:
                status = "AVAILABLE"

            amenities = room.get("amenities")
            if isinstance(amenities, str):
                amenities = [part.strip() for part in amenities.split(",") if part.strip()]
            if not isinstance(amenities, list):
                amenities = []
            amenities = [str(item).strip()[:60] for item in amenities if str(item).strip()]

            normalized_rooms.append(
                {
                    "room_name": room_name[:100],
                    "room_type": room_type[:100],
                    "person_limit": person_limit,
                    "current_availability": current_availability,
                    "price_per_night": price,
                    "status": status,
                    "amenities": amenities,
                }
            )
        return normalized_rooms

    def _compute_runtime_flags(self, accommodation: Accomodation) -> dict:
        room_qs = Room.objects.filter(accommodation=accommodation)
        available_rooms_qs = room_qs.filter(status="AVAILABLE", current_availability__gte=1)
        owner = accommodation.owner

        owner_ok = bool(
            owner
            and getattr(owner, "is_active", False)
            and owner.groups.filter(name__iexact="accommodation_owner").exists()
            and not owner.groups.filter(name__iexact="accommodation_owner_pending").exists()
            and not owner.groups.filter(name__iexact="accommodation_owner_declined").exists()
        )
        accommodation_ok = (
            str(accommodation.approval_status or "").lower() == "accepted"
            and bool(accommodation.is_active)
            and str(accommodation.company_type or "").strip().lower() in ALLOWED_COMPANY_TYPES
        )

        return {
            "catalog_visible": bool(
                available_rooms_qs.exists()
                and str(accommodation.approval_status or "").lower() == "accepted"
            ),
            "recommendation_eligible": bool(available_rooms_qs.exists() and accommodation_ok and owner_ok),
            "booking_eligible": bool(
                available_rooms_qs.exists()
                and str(accommodation.approval_status or "").lower() == "accepted"
            ),
        }

    def _simulate_eligibility_flags(
        self,
        *,
        owner_present: bool,
        owner_linked: bool,
        owner_active: bool,
        owner_group_ok: bool,
        approval_status: str,
        is_active: bool,
        rooms: list[dict],
    ) -> dict:
        available_rooms = [
            room
            for room in rooms
            if str(room.get("status") or "").upper() == "AVAILABLE"
            and int(room.get("current_availability") or 0) >= 1
        ]
        catalog_visible = bool(available_rooms and approval_status == "accepted")
        recommendation_eligible = bool(
            available_rooms
            and approval_status == "accepted"
            and is_active
            and owner_present
            and owner_linked
            and owner_active
            and owner_group_ok
        )
        booking_eligible = bool(available_rooms and approval_status == "accepted")
        return {
            "catalog_visible": catalog_visible,
            "recommendation_eligible": recommendation_eligible,
            "booking_eligible": booking_eligible,
        }

    def _normalize_approval_status(self, value) -> str:
        normalized = str(value or "").strip().lower()
        if normalized in {"accepted", "pending", "declined"}:
            return normalized
        return "accepted"

    def _safe_int(self, value, *, default=0):
        try:
            return int(str(value).strip())
        except Exception:
            return int(default)

    def _safe_decimal(self, value, *, default=Decimal("0")) -> Decimal:
        try:
            return Decimal(str(value).strip())
        except (InvalidOperation, ValueError, TypeError):
            return Decimal(default)

    def _to_bool(self, value, *, default=False) -> bool:
        if isinstance(value, bool):
            return value
        raw = str(value or "").strip().lower()
        if not raw:
            return bool(default)
        if raw in {"1", "true", "yes", "y", "on"}:
            return True
        if raw in {"0", "false", "no", "n", "off"}:
            return False
        return bool(default)

    def _dry_user_stub(self, email: str):
        class DryUser:
            def __init__(self, raw_email):
                self.email = raw_email
                self.pk = -1

        return DryUser(email)

    def _write_reports(self, json_path: Path, csv_path: Path, report: dict):
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

        csv_path.parent.mkdir(parents=True, exist_ok=True)
        rows = report.get("rows") or []
        headers = [
            "record_id",
            "company_name",
            "company_type",
            "status",
            "message",
            "owner_email",
            "owner_created",
            "owner_linked",
            "rooms_created",
            "rooms_updated",
            "catalog_visible",
            "recommendation_eligible",
            "booking_eligible",
        ]
        with csv_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=headers)
            writer.writeheader()
            for row in rows:
                writer.writerow({key: row.get(key, "") for key in headers})
