import csv
from pathlib import Path

from django.core.management.base import BaseCommand

from guest_app.models import MapBookmark


class Command(BaseCommand):
    help = "Seed official public MapBookmark records (user=None) using verified coordinates only."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Preview changes without writing to DB.")
        parser.add_argument(
            "--csv-template",
            default="thesis_data_templates/mapbookmark_public_seed_template.csv",
            help="Path to write/update coordinate template CSV.",
        )

    def handle(self, *args, **options):
        dry_run = bool(options.get("dry_run"))
        template_path = Path(str(options.get("csv_template") or "").strip())

        seed_rows = [
            {
                "name": "Bayawan City Plaza",
                "category": "landmark",
                "latitude": 9.366320,
                "longitude": 122.804893,
                "details": "Public city plaza and landmark in Bayawan.",
            },
            {
                "name": "Bayawan City Public Terminal",
                "category": "public",
                "latitude": 9.367904,
                "longitude": 122.807103,
                "details": "Main public terminal area for local transport.",
            },
            {
                "name": "Bayawan City Public Market",
                "category": "shopping",
                "latitude": 9.363832,
                "longitude": 122.804670,
                "details": "Public market area with food and local goods.",
            },
            {
                "name": "Catholic Church",
                "category": "landmark",
                "latitude": 9.367582,
                "longitude": 122.804770,
                "details": "Recognized church landmark in the city proper area.",
            },
            {
                "name": "Hayahay Square",
                "category": "landmark",
                "latitude": 9.361274,
                "longitude": 122.798739,
                "details": "Known local square/area in Bayawan.",
            },
            {
                "name": "Eskina Restaurant",
                "category": "restaurant",
                "latitude": 9.363390,
                "longitude": 122.799181,
                "details": "Dining place listed in the current map reference points.",
            },
            {
                "name": "Handurawan Cafe",
                "category": "restaurant",
                "latitude": 9.362982,
                "longitude": 122.802543,
                "details": "Cafe/dining place listed in map reference points.",
            },
            {
                "name": "Canamaque Royal Suites",
                "category": "hotel",
                "latitude": 9.364171,
                "longitude": 122.806765,
                "details": "Accommodation mapped from verified reference points.",
            },
        ]

        needs_verified_coordinates = [
            {
                "name": "Bayawan City Hall",
                "category": "public",
                "details": "Coordinate required before public seeding.",
            },
            {
                "name": "Bayawan Boulevard",
                "category": "landmark",
                "details": "Coordinate required before public seeding.",
            },
        ]

        created = 0
        updated = 0
        skipped = []

        for row in seed_rows:
            if row.get("latitude") is None or row.get("longitude") is None:
                skipped.append(
                    {
                        "name": row.get("name", ""),
                        "reason": "missing_coordinates",
                    }
                )
                continue

            defaults = {
                "category": row["category"],
                "latitude": float(row["latitude"]),
                "longitude": float(row["longitude"]),
                "details": str(row.get("details") or "").strip(),
                "user": None,
            }
            if dry_run:
                exists = MapBookmark.objects.filter(name=row["name"], user__isnull=True).exists()
                if exists:
                    updated += 1
                else:
                    created += 1
                continue

            obj, was_created = MapBookmark.objects.update_or_create(
                name=row["name"],
                user=None,
                defaults=defaults,
            )
            if was_created:
                created += 1
            else:
                updated += 1

        for row in needs_verified_coordinates:
            skipped.append({"name": row["name"], "reason": "needs_verified_coordinates"})

        self._write_template(template_path, needs_verified_coordinates)

        self.stdout.write(self.style.SUCCESS(f"Dry run: {dry_run}"))
        self.stdout.write(self.style.SUCCESS(f"Created: {created}"))
        self.stdout.write(self.style.SUCCESS(f"Updated: {updated}"))
        self.stdout.write(self.style.WARNING(f"Skipped: {len(skipped)}"))
        for item in skipped:
            self.stdout.write(f"- {item['name']}: {item['reason']}")
        self.stdout.write(f"Template: {template_path}")

    def _write_template(self, path, rows):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(
                fh,
                fieldnames=["name", "category", "latitude", "longitude", "details", "status_note"],
            )
            writer.writeheader()
            for row in rows:
                writer.writerow(
                    {
                        "name": row.get("name", ""),
                        "category": row.get("category", ""),
                        "latitude": "",
                        "longitude": "",
                        "details": row.get("details", ""),
                        "status_note": "Needs verified coordinates from Tourism Office/map team",
                    }
                )
