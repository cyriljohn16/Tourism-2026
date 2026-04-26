from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("admin_app", "0030_accomodation_official_links_and_owner_monthly_report"),
    ]

    operations = [
        migrations.CreateModel(
            name="MonthlyReportRoomUsage",
            fields=[
                ("usage_id", models.BigAutoField(primary_key=True, serialize=False)),
                ("room_name_snapshot", models.CharField(default="", max_length=120)),
                ("check_ins", models.PositiveIntegerField(default=0)),
                ("check_outs", models.PositiveIntegerField(default=0)),
                ("guests_count", models.PositiveIntegerField(default=0)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "monthly_report",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="room_usage_rows",
                        to="admin_app.ownermonthlyreport",
                    ),
                ),
                (
                    "room",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="monthly_report_usage_rows",
                        to="admin_app.room",
                    ),
                ),
            ],
            options={
                "ordering": ["room_name_snapshot", "usage_id"],
                "unique_together": {("monthly_report", "room")},
            },
        ),
    ]
