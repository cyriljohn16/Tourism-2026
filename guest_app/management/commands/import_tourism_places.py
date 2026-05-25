import csv
import json
import re
from pathlib import Path

from django.conf import settings
from django.core.files import File
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from admin_app.models import TourismInformation
from guest_app.models import MapBookmark


CSV_COLUMNS = [
    "name",
    "category",
    "short_description",
    "full_description",
    "location",
    "address",
    "barangay",
    "latitude",
    "longitude",
    "map_x",
    "map_y",
    "image_path",
    "source",
    "is_published",
    "is_featured",
    "official_note",
]


PUBLIC_CATEGORY_MAP = {
    "dining": "restaurant",
    "restaurant": "restaurant",
    "restaurants": "restaurant",
    "cafe": "restaurant",
    "food": "restaurant",
    "food_house": "restaurant",
    "tourist_spot": "landmark",
    "tourist spot": "landmark",
    "tourist_spots": "landmark",
    "attraction": "landmark",
    "landmark": "landmark",
    "nature": "landmark",
    "nature_attraction": "landmark",
    "public_facility": "public",
    "public facility": "public",
    "public": "public",
    "service": "public",
    "government": "public",
    "shopping_local_products": "shopping",
    "shopping/local products": "shopping",
    "shopping": "shopping",
    "market": "shopping",
    "local_products": "shopping",
    "approved_stay": "hotel",
    "approved stay": "hotel",
    "accommodation": "hotel",
    "hotel": "hotel",
    "stay": "hotel",
    "other_tourism_place": "custom",
    "other tourism place": "custom",
    "other": "custom",
    "custom": "custom",
}

TOURISM_INFORMATION_CATEGORIES = {
    "tourist_spot",
    "tourist spot",
    "tourist_spots",
    "attraction",
    "landmark",
    "nature",
    "nature_attraction",
}


def _normalize_text(value):
    return str(value or "").strip()


def _normalize_key(value):
    return re.sub(r"[^a-z0-9]+", "", str(value or "").strip().lower())


def _parse_bool(value, *, default=False):
    text = str(value or "").strip().lower()
    if not text:
        return default
    if text in {"1", "true", "yes", "y", "published", "active"}:
        return True
    if text in {"0", "false", "no", "n", "draft", "inactive"}:
        return False
    return default


def _parse_float(value):
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


class Command(BaseCommand):
    help = (
        "Import Tourism Office-verified tourism places into existing "
        "TourismInformation and public MapBookmark records. Supports dry-run."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--csv",
            required=True,
            help="Path to a CSV file using docs/import_templates/tourism_places_import_template.csv columns.",
        )
        parser.add_argument("--dry-run", action="store_true", help="Validate and report without writing DB changes.")
        parser.add_argument(
            "--report-csv",
            default="artifacts/import_reports/import_tourism_places_report.csv",
            help="Path for row-level CSV import report.",
        )
        parser.add_argument(
            "--report-json",
            default="artifacts/import_reports/import_tourism_places_report.json",
            help="Path for JSON import summary.",
        )

    def handle(self, *args, **options):
        csv_path = Path(str(options["csv"] or "").strip())
        if not csv_path.exists():
            raise CommandError(f"CSV file not found: {csv_path}")

        dry_run = bool(options.get("dry_run"))
        report_csv_path = Path(str(options.get("report_csv") or "").strip())
        report_json_path = Path(str(options.get("report_json") or "").strip())

        rows = self._read_rows(csv_path)
        counters = {
            "rows_read": len(rows),
            "created": 0,
            "updated": 0,
            "skipped": 0,
            "errors": 0,
        }
        report_rows = []

        if dry_run:
            self.stdout.write(self.style.WARNING("Running in dry-run mode. No database records will be changed."))

        with transaction.atomic():
            for index, row in enumerate(rows, start=2):
                result = self._process_row(row, row_number=index, dry_run=dry_run)
                report_rows.append(result)
                status = result.get("status")
                if status in {"created", "would_create"}:
                    counters["created"] += 1
                elif status in {"updated", "would_update"}:
                    counters["updated"] += 1
                elif status == "error":
                    counters["errors"] += 1
                else:
                    counters["skipped"] += 1

            if dry_run:
                transaction.set_rollback(True)

        self._write_reports(report_rows, counters, report_csv_path, report_json_path, dry_run=dry_run)

        self.stdout.write(self.style.SUCCESS(f"Rows read: {counters['rows_read']}"))
        self.stdout.write(self.style.SUCCESS(f"Created/would create: {counters['created']}"))
        self.stdout.write(self.style.SUCCESS(f"Updated/would update: {counters['updated']}"))
        self.stdout.write(self.style.WARNING(f"Skipped: {counters['skipped']}"))
        self.stdout.write(self.style.ERROR(f"Errors: {counters['errors']}"))
        self.stdout.write(f"Report CSV: {report_csv_path}")
        self.stdout.write(f"Report JSON: {report_json_path}")

    def _read_rows(self, path):
        with path.open("r", encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh)
            missing = [column for column in ("name", "category", "is_published") if column not in (reader.fieldnames or [])]
            if missing:
                raise CommandError(f"CSV is missing required column(s): {', '.join(missing)}")
            return [dict(row) for row in reader]

    def _process_row(self, row, *, row_number, dry_run):
        name = _normalize_text(row.get("name"))
        raw_category = _normalize_text(row.get("category")).lower()
        map_category = PUBLIC_CATEGORY_MAP.get(raw_category)
        is_published = _parse_bool(row.get("is_published"), default=False)

        if not name:
            return self._result(row_number, name, raw_category, "error", "missing_name")
        if not map_category:
            return self._result(row_number, name, raw_category, "error", "unsupported_category")
        if not is_published:
            return self._result(row_number, name, raw_category, "skipped", "not_marked_published_or_active")

        latitude = _parse_float(row.get("latitude"))
        longitude = _parse_float(row.get("longitude"))
        description = _normalize_text(row.get("full_description")) or _normalize_text(row.get("short_description"))
        location = self._compose_location(row)
        source_note = _normalize_text(row.get("source"))
        official_note = _normalize_text(row.get("official_note"))
        details = description
        if official_note:
            details = f"{details}\n\nTourism Office note: {official_note}".strip()
        if source_note:
            details = f"{details}\n\nSource: {source_note}".strip()

        actions = []
        errors = []

        if raw_category in TOURISM_INFORMATION_CATEGORIES:
            tourism_status = self._upsert_tourism_information(
                name=name,
                description=description,
                location=location,
                image_path=_normalize_text(row.get("image_path")),
                dry_run=dry_run,
            )
            actions.append(tourism_status)

        if latitude is not None and longitude is not None:
            if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
                errors.append("invalid_latitude_or_longitude_range")
            else:
                marker_status = self._upsert_map_bookmark(
                    name=name,
                    category=map_category,
                    latitude=latitude,
                    longitude=longitude,
                    details=details,
                    image_path=_normalize_text(row.get("image_path")),
                    dry_run=dry_run,
                )
                actions.append(marker_status)
        else:
            actions.append("marker_skipped_missing_coordinates")

        if errors:
            return self._result(row_number, name, raw_category, "error", ";".join(errors), actions=actions)
        if any(action.endswith("created") for action in actions):
            return self._result(row_number, name, raw_category, "would_create" if dry_run else "created", "ok", actions=actions)
        if any(action.endswith("updated") for action in actions):
            return self._result(row_number, name, raw_category, "would_update" if dry_run else "updated", "ok", actions=actions)
        return self._result(row_number, name, raw_category, "skipped", "no_writable_target_for_row", actions=actions)

    def _upsert_tourism_information(self, *, name, description, location, image_path, dry_run):
        existing = TourismInformation.objects.filter(spot_name__iexact=name).first()
        if dry_run:
            return "tourism_information_updated" if existing else "tourism_information_created"

        if existing:
            existing.description = description
            existing.location = location
            existing.publication_status = "published"
            existing.is_active = True
            self._attach_image(existing, "image", image_path)
            existing.save()
            return "tourism_information_updated"

        obj = TourismInformation(
            spot_name=name,
            description=description,
            location=location,
            publication_status="published",
            is_active=True,
        )
        self._attach_image(obj, "image", image_path)
        obj.save()
        return "tourism_information_created"

    def _upsert_map_bookmark(self, *, name, category, latitude, longitude, details, image_path, dry_run):
        existing = MapBookmark.objects.filter(user__isnull=True, category=category, name__iexact=name).first()
        if dry_run:
            return "map_bookmark_updated" if existing else "map_bookmark_created"

        if existing:
            existing.latitude = latitude
            existing.longitude = longitude
            existing.details = details
            self._attach_image(existing, "primary_image", image_path)
            existing.save()
            return "map_bookmark_updated"

        obj = MapBookmark(
            name=name,
            category=category,
            latitude=latitude,
            longitude=longitude,
            details=details,
            user=None,
        )
        self._attach_image(obj, "primary_image", image_path)
        obj.save()
        return "map_bookmark_created"

    def _attach_image(self, obj, field_name, image_path):
        if not image_path:
            return
        path = Path(image_path)
        if not path.is_absolute():
            path = settings.BASE_DIR / path
        try:
            resolved = path.resolve()
        except Exception:
            return
        if not resolved.exists() or not resolved.is_file():
            return
        with resolved.open("rb") as fh:
            getattr(obj, field_name).save(resolved.name, File(fh), save=False)

    def _compose_location(self, row):
        parts = [
            _normalize_text(row.get("location")),
            _normalize_text(row.get("address")),
            _normalize_text(row.get("barangay")),
        ]
        seen = set()
        cleaned = []
        for part in parts:
            key = _normalize_key(part)
            if part and key not in seen:
                cleaned.append(part)
                seen.add(key)
        return ", ".join(cleaned)

    def _result(self, row_number, name, category, status, message, *, actions=None):
        return {
            "row_number": row_number,
            "name": name,
            "category": category,
            "status": status,
            "message": message,
            "actions": "|".join(actions or []),
        }

    def _write_reports(self, rows, counters, csv_path, json_path, *, dry_run):
        if csv_path:
            csv_path.parent.mkdir(parents=True, exist_ok=True)
            with csv_path.open("w", encoding="utf-8", newline="") as fh:
                writer = csv.DictWriter(
                    fh,
                    fieldnames=["row_number", "name", "category", "status", "message", "actions"],
                )
                writer.writeheader()
                writer.writerows(rows)
        if json_path:
            json_path.parent.mkdir(parents=True, exist_ok=True)
            with json_path.open("w", encoding="utf-8") as fh:
                json.dump({"dry_run": dry_run, "summary": counters, "rows": rows}, fh, indent=2)
