from collections import Counter
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from ai_chatbot.models import ChatbotLog


class Command(BaseCommand):
    help = "Summarize low-confidence CNN intent cases and deterministic-routing recovery."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=30, help="Lookback window in days (default: 30).")
        parser.add_argument("--top", type=int, default=10, help="Top N groups to print (default: 10).")

    def handle(self, *args, **options):
        days = max(1, int(options.get("days") or 30))
        top_n = max(1, int(options.get("top") or 10))
        since = timezone.now() - timedelta(days=days)

        qs = ChatbotLog.objects.filter(created_at__gte=since).order_by("-created_at")
        total = qs.count()

        low_conf_rows = []
        for row in qs[:5000]:
            provenance = row.provenance_json if isinstance(row.provenance_json, dict) else {}
            extra = provenance.get("extra") if isinstance(provenance.get("extra"), dict) else {}
            is_low = bool(extra.get("low_confidence_case"))
            intent_source = str(provenance.get("intent_source") or "").strip().lower()
            if not is_low and intent_source != "text_cnn_low_confidence":
                continue
            low_conf_rows.append((row, extra, provenance))

        low_count = len(low_conf_rows)
        fixed_count = 0

        predicted_counter = Counter()
        final_counter = Counter()
        role_counter = Counter()
        source_counter = Counter()
        query_counter = Counter()

        for row, extra, provenance in low_conf_rows:
            predicted = str(extra.get("low_confidence_predicted_intent") or "").strip().lower() or "unknown"
            final_intent = (
                str(extra.get("low_confidence_final_resolved_intent") or "").strip().lower()
                or str(row.resolved_intent or "").strip().lower()
                or "unknown"
            )
            role = str(provenance.get("chat_role") or "unknown").strip().lower() or "unknown"
            source = str(provenance.get("intent_source") or "unknown").strip().lower() or "unknown"
            query = " ".join(str(row.user_message or "").strip().lower().split())
            fixed = bool(extra.get("deterministic_routing_fixed_low_confidence"))

            predicted_counter[predicted] += 1
            final_counter[final_intent] += 1
            role_counter[role] += 1
            source_counter[source] += 1
            if query:
                query_counter[query] += 1
            if fixed:
                fixed_count += 1

        self.stdout.write(self.style.SUCCESS("Chatbot low-confidence summary"))
        self.stdout.write(f"- Window (days): {days}")
        self.stdout.write(f"- Total chatbot logs: {total}")
        self.stdout.write(f"- Low-confidence cases: {low_count}")
        fix_rate = (fixed_count / low_count * 100.0) if low_count else 0.0
        self.stdout.write(f"- Deterministic-routing fixed cases: {fixed_count} ({fix_rate:.2f}%)")

        def _print_counter(title, counter):
            self.stdout.write(f"\n{title}")
            if not counter:
                self.stdout.write("  (none)")
                return
            for key, count in counter.most_common(top_n):
                self.stdout.write(f"  - {key}: {count}")

        _print_counter("Top predicted intents (low confidence)", predicted_counter)
        _print_counter("Top final resolved intents", final_counter)
        _print_counter("Low-confidence count by role", role_counter)
        _print_counter("Low-confidence source distribution", source_counter)
        _print_counter("Most repeated low-confidence queries", query_counter)
