import csv
from collections import Counter
from datetime import timedelta
from pathlib import Path

from django.core.management.base import BaseCommand
from django.utils import timezone

from ai_chatbot.models import ChatbotLog


class Command(BaseCommand):
    help = "Summarize chatbot NLG reliability (Gemini/OpenAI success, errors, retries, and fallback usage)."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=30, help="Lookback window in days (default: 30).")
        parser.add_argument("--top", type=int, default=10, help="Top N grouped rows (default: 10).")
        parser.add_argument("--out-csv", default="", help="Optional CSV output for recent NLG rows.")

    def handle(self, *args, **options):
        days = max(1, int(options.get("days") or 30))
        top_n = max(1, int(options.get("top") or 10))
        out_csv = str(options.get("out_csv") or "").strip()

        since = timezone.now() - timedelta(days=days)
        qs = ChatbotLog.objects.filter(created_at__gte=since).order_by("-created_at")
        total = qs.count()

        nlg_qs = qs.exclude(response_nlg_source__exact="")
        nlg_total = nlg_qs.count()

        provider_counter = Counter()
        source_counter = Counter()
        reason_counter = Counter()
        status_counter = Counter()
        fallback_counter = Counter()
        retry_counter = Counter()
        guardrail_counter = Counter()
        empty_counter = Counter()
        rows = []

        for log in nlg_qs[:5000]:
            source = str(log.response_nlg_source or "").strip().lower()
            provenance = log.provenance_json if isinstance(log.provenance_json, dict) else {}
            nlg_meta = provenance.get("extra", {}).get("nlg", {}) if isinstance(provenance.get("extra"), dict) else {}
            if not isinstance(nlg_meta, dict):
                nlg_meta = {}

            provider = str(nlg_meta.get("nlg_provider") or ("gemini" if "gemini" in source else ("openai" if "openai" in source else "other"))).strip().lower()
            reason = str(nlg_meta.get("nlg_error_reason") or "").strip().lower() or "none"
            http_status = nlg_meta.get("nlg_http_status")
            retry_count = int(nlg_meta.get("nlg_retry_count") or 0)
            timeout_flag = bool(nlg_meta.get("nlg_timeout"))
            guardrail_flag = bool(nlg_meta.get("nlg_guardrail_triggered"))
            empty_flag = bool(nlg_meta.get("nlg_empty_response"))
            fallback_flag = bool(log.fallback_used)

            provider_counter[provider] += 1
            source_counter[source or "unknown"] += 1
            reason_counter[reason] += 1
            status_counter[str(http_status) if http_status not in (None, "") else "none"] += 1
            retry_counter[str(retry_count)] += 1
            guardrail_counter["true" if guardrail_flag else "false"] += 1
            empty_counter["true" if empty_flag else "false"] += 1
            fallback_counter["true" if fallback_flag else "false"] += 1
            if timeout_flag:
                reason_counter["timeout"] += 0  # keep explicit key present when timeout happens

            if len(rows) < max(50, top_n):
                rows.append(
                    {
                        "created_at": log.created_at.isoformat(),
                        "source": source,
                        "provider": provider,
                        "fallback_used": str(fallback_flag),
                        "nlg_error_reason": reason,
                        "nlg_http_status": str(http_status or ""),
                        "nlg_retry_count": str(retry_count),
                        "nlg_timeout": str(timeout_flag),
                        "nlg_guardrail_triggered": str(guardrail_flag),
                        "nlg_empty_response": str(empty_flag),
                    }
                )

        success_like = 0
        for source, count in source_counter.items():
            if source in {"gemini_nlg", "gemini_nlg_retry", "openai_nlg"}:
                success_like += count

        fallback_count = fallback_counter.get("true", 0)
        retries_attempted = sum(int(k) * v for k, v in retry_counter.items() if str(k).isdigit())
        retry_success = source_counter.get("gemini_nlg_retry", 0)

        self.stdout.write(self.style.SUCCESS("Chatbot NLG reliability summary"))
        self.stdout.write(f"- Window (days): {days}")
        self.stdout.write(f"- Total chat logs: {total}")
        self.stdout.write(f"- Logs with NLG source: {nlg_total}")
        self.stdout.write(f"- NLG success-like count: {success_like}")
        self.stdout.write(f"- NLG fallback-used count: {fallback_count}")
        self.stdout.write(f"- Retry attempts observed: {retries_attempted}")
        self.stdout.write(f"- Retry success count (gemini_nlg_retry): {retry_success}")

        nlg_success_rate = (success_like / nlg_total * 100.0) if nlg_total else 0.0
        nlg_fallback_rate = (fallback_count / nlg_total * 100.0) if nlg_total else 0.0
        self.stdout.write(f"- NLG success rate: {nlg_success_rate:.2f}%")
        self.stdout.write(f"- NLG fallback rate: {nlg_fallback_rate:.2f}%")

        def _print_counter(title, counter):
            self.stdout.write(f"\n{title}")
            if not counter:
                self.stdout.write("  (none)")
                return
            for key, count in counter.most_common(top_n):
                self.stdout.write(f"  - {key}: {count}")

        _print_counter("Top NLG sources", source_counter)
        _print_counter("Provider distribution", provider_counter)
        _print_counter("Top NLG error reasons", reason_counter)
        _print_counter("HTTP status distribution", status_counter)
        _print_counter("Retry count distribution", retry_counter)
        _print_counter("Guardrail-triggered distribution", guardrail_counter)
        _print_counter("Empty-response distribution", empty_counter)
        _print_counter("Fallback-used distribution", fallback_counter)

        if out_csv:
            out_path = Path(out_csv).resolve()
            out_path.parent.mkdir(parents=True, exist_ok=True)
            with out_path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=[
                        "created_at",
                        "source",
                        "provider",
                        "fallback_used",
                        "nlg_error_reason",
                        "nlg_http_status",
                        "nlg_retry_count",
                        "nlg_timeout",
                        "nlg_guardrail_triggered",
                        "nlg_empty_response",
                    ],
                )
                writer.writeheader()
                writer.writerows(rows)
            self.stdout.write(self.style.SUCCESS(f"\nCSV exported: {out_path}"))
