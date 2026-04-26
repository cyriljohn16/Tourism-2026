from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("admin_app", "0029_inappnotification"),
    ]

    operations = [
        migrations.AddField(
            model_name="accomodation",
            name="official_booking_url",
            field=models.URLField(
                blank=True,
                default="",
                help_text="Official external booking page URL for promotional handoff.",
                max_length=500,
            ),
        ),
        migrations.AddField(
            model_name="accomodation",
            name="official_contact_url",
            field=models.URLField(
                blank=True,
                default="",
                help_text="Official contact/inquiry page URL for this accommodation.",
                max_length=500,
            ),
        ),
        migrations.CreateModel(
            name="OwnerMonthlyReport",
            fields=[
                ("report_id", models.BigAutoField(primary_key=True, serialize=False)),
                (
                    "reporting_period",
                    models.DateField(
                        help_text="Use first day of month to represent reporting period (e.g., 2026-02-01)."
                    ),
                ),
                ("guests_checked_in", models.PositiveIntegerField(default=0)),
                ("guests_checked_out", models.PositiveIntegerField(default=0)),
                ("rooms_used", models.PositiveIntegerField(default=0)),
                ("room_usage_notes", models.TextField(blank=True, default="")),
                (
                    "nationality_breakdown",
                    models.TextField(
                        blank=True,
                        default="",
                        help_text="Owner-submitted nationality summary (e.g., Filipino: 34, Foreign: 12).",
                    ),
                ),
                ("additional_remarks", models.TextField(blank=True, default="")),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("draft", "Draft"),
                            ("submitted", "Submitted"),
                            ("returned", "Returned for Revision"),
                            ("reviewed", "Reviewed"),
                        ],
                        default="submitted",
                        max_length=20,
                    ),
                ),
                ("review_notes", models.TextField(blank=True, default="")),
                ("submitted_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "accommodation",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="monthly_reports",
                        to="admin_app.accomodation",
                    ),
                ),
                (
                    "owner",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="owner_monthly_reports",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "reviewed_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="reviewed_owner_monthly_reports",
                        to="admin_app.employee",
                    ),
                ),
            ],
            options={
                "ordering": ["-reporting_period", "-submitted_at"],
                "unique_together": {("accommodation", "reporting_period")},
            },
        ),
    ]

