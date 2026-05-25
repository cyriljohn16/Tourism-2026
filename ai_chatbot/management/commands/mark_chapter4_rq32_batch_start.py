import json
from pathlib import Path

from django.core.management.base import BaseCommand
from django.utils import timezone

from ai_chatbot.models import ChatbotLog, RecommendationEvent, SystemMetricLog
from guest_app.models import Pending


class Command(BaseCommand):
    help = (
        "Create a read-only baseline marker for Chapter 4 RQ 3.2 clean batch extraction. "
        "The marker stores current max IDs and timestamp; no data rows are modified."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--label",
            default="chapter4_rq32_final",
            help="Label to include in the marker file.",
        )
        parser.add_argument(
            "--out-file",
            default="thesis_data_templates/chapter4_rq32_clean_final_marker.json",
            help="Marker JSON file path.",
        )

    def handle(self, *args, **options):
        out_file = Path(str(options.get("out_file") or "").strip() or "thesis_data_templates/chapter4_rq32_clean_final_marker.json")
        out_file.parent.mkdir(parents=True, exist_ok=True)

        payload = {
            "label": str(options.get("label") or "chapter4_rq32_final").strip() or "chapter4_rq32_final",
            "created_at": timezone.now().isoformat(),
            "watermarks": {
                "recommendation_event_id": int(
                    RecommendationEvent.objects.order_by("-event_id").values_list("event_id", flat=True).first() or 0
                ),
                "chatbot_log_id": int(
                    ChatbotLog.objects.order_by("-log_id").values_list("log_id", flat=True).first() or 0
                ),
                "system_metric_id": int(
                    SystemMetricLog.objects.order_by("-metric_id").values_list("metric_id", flat=True).first() or 0
                ),
                "pending_id": int(
                    Pending.objects.order_by("-id").values_list("id", flat=True).first() or 0
                ),
            },
        }
        out_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")

        self.stdout.write(self.style.SUCCESS(f"Created RQ 3.2 marker: {out_file}"))
        self.stdout.write(json.dumps(payload, indent=2))
