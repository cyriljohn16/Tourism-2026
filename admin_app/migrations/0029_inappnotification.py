from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("admin_app", "0028_accomodation_accommodation_amenities"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="InAppNotification",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("title", models.CharField(max_length=140)),
                ("message", models.TextField()),
                (
                    "notification_type",
                    models.CharField(
                        choices=[
                            ("booking", "Booking"),
                            ("approval", "Approval"),
                            ("assignment", "Assignment"),
                            ("billing", "Billing"),
                            ("system", "System"),
                        ],
                        db_index=True,
                        default="system",
                        max_length=20,
                    ),
                ),
                ("related_object_id", models.CharField(blank=True, default="", max_length=40)),
                ("url", models.CharField(blank=True, default="", max_length=255)),
                ("dedupe_key", models.CharField(blank=True, db_index=True, default="", max_length=160)),
                ("is_read", models.BooleanField(db_index=True, default=False)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                (
                    "recipient_employee",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="in_app_notifications",
                        to="admin_app.employee",
                    ),
                ),
                (
                    "recipient_guest",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="in_app_notifications",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.AddIndex(
            model_name="inappnotification",
            index=models.Index(fields=["recipient_guest", "is_read", "created_at"], name="admin_app_in_recipie_922495_idx"),
        ),
        migrations.AddIndex(
            model_name="inappnotification",
            index=models.Index(fields=["recipient_employee", "is_read", "created_at"], name="admin_app_in_recipie_70f545_idx"),
        ),
    ]
