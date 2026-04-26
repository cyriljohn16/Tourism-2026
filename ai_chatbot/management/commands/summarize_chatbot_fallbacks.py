import csv
import csv
from collections import Counter
from datetime import timedelta
from pathlib import Path

from django.core.management.base import BaseCommand
from django.utils import timezone

from ai_chatbot.models import ChatbotLog


class Command(BaseCommand):
    help = (
        "Summarize chatbot fallback and unclear-query logs for internal QA "
        "and continuous intent/routing improvement."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            default=30,
            help="Lookback window in days (default: 30).",
        )
        parser.add_argument(
            "--top",
            type=int,
            default=10,
            help="Top N entries for repeated query/reason lists (default: 10).",
        )
        parser.add_argument(
            "--out-csv",
            default="",
            help="Optional output CSV path for top fallback rows.",
        )

    def handle(self, *args, **options):
        days = max(1, int(options.get("days") or 30))
        top_n = max(1, int(options.get("top") or 10))
        out_csv = str(options.get("out_csv") or "").strip()

        since = timezone.now() - timedelta(days=days)
        qs = ChatbotLog.objects.filter(created_at__gte=since).order_by("-created_at")
        fallback_qs = qs.filter(fallback_used=True)

        total_logs = qs.count()
        fallback_logs = fallback_qs.count()

        role_counter = Counter()
        page_counter = Counter()
        reason_counter = Counter()
        query_counter = Counter()
        intent_counter = Counter()
        recent_rows = []

        for row in fallback_qs[:2000]:
            provenance = row.provenance_json if isinstance(row.provenance_json, dict) else {}
            role = str(provenance.get("chat_role") or "unknown").strip().lower() or "unknown"
            page = str(provenance.get("page_context") or "unknown").strip() or "unknown"
            reason = (
                str(provenance.get("fallback_reason") or "")
                or str(provenance.get("intent_classifier_error") or "")
                or str(row.intent_classifier_source or "")
                or "unspecified"
            ).strip()
            query = " ".join(str(row.user_message or "").strip().lower().split())
            intent = str(row.resolved_intent or "unknown").strip().lower() or "unknown"

            role_counter[role] += 1
            page_counter[page] += 1
            reason_counter[reason] += 1
            if query:
                query_counter[query] += 1
            intent_counter[intent] += 1

            if len(recent_rows) < max(20, top_n):
                recent_rows.append(
                    {
                        "created_at": row.created_at.isoformat(),
                        "role": role,
                        "intent": intent,
                        "fallback_reason": reason,
                        "page_context": page,
                        "query": str(row.user_message or "").strip(),
                    }
                )

        self.stdout.write(self.style.SUCCESS("Chatbot fallback summary"))
        self.stdout.write(f"- Window (days): {days}")
        self.stdout.write(f"- Total chatbot logs: {total_logs}")
        self.stdout.write(f"- Fallback/clarification logs: {fallback_logs}")
        rate = (fallback_logs / total_logs * 100.0) if total_logs else 0.0
        self.stdout.write(f"- Fallback rate: {rate:.2f}%")

        def _print_counter(title, counter):
            self.stdout.write(f"\n{title}")
            if not counter:
                self.stdout.write("  (none)")
                return
            for key, count in counter.most_common(top_n):
                self.stdout.write(f"  - {key}: {count}")

        _print_counter("Top fallback reasons", reason_counter)
        _print_counter("Fallback count by role", role_counter)
        _print_counter("Fallback count by page/context", page_counter)
        _print_counter("Fallback count by resolved intent", intent_counter)
        _print_counter("Most repeated unclear queries", query_counter)

        if out_csv:
            out_path = Path(out_csv).resolve()
            out_path.parent.mkdir(parents=True, exist_ok=True)
            with out_path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=[
                        "created_at",
                        "role",
                        "intent",
                        "fallback_reason",
                        "page_context",
                        "query",
                    ],
                )
                writer.writeheader()
                writer.writerows(recent_rows)
            self.stdout.write(self.style.SUCCESS(f"\nCSV exported: {out_path}"))
