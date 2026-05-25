import json
from dataclasses import dataclass
from typing import Any

from django.test import Client

from ai_chatbot.demo_prompt_regression_runner import _ensure_employee, _ensure_guest, _ensure_tour_and_assignments
from ai_chatbot.models import ChatbotLog
from guest_app.models import Pending
from tour_app.models import Tour_Add, Tour_Schedule


@dataclass
class Case:
    group: str
    prompt: str
    expected_intents: tuple[str, ...]
    expect_cards: bool | None = None
    expect_preview_only: bool = False


def _extract_detected_intent(response_payload: dict[str, Any]) -> str:
    intent_classifier = response_payload.get("intent_classifier") if isinstance(response_payload, dict) else {}
    if not isinstance(intent_classifier, dict):
        return ""
    top3 = intent_classifier.get("top_3") if isinstance(intent_classifier.get("top_3"), list) else []
    if top3 and isinstance(top3[0], dict):
        return str(top3[0].get("intent") or top3[0].get("raw_label") or "").strip().lower()
    source = str(intent_classifier.get("source") or "").strip().lower()
    if source == "deterministic_pre_route":
        return "deterministic_pre_route"
    return ""


def _post_chat(client: Client, prompt: str, *, actor_username: str) -> dict[str, Any]:
    last_log_id = ChatbotLog.objects.order_by("-log_id").values_list("log_id", flat=True).first() or 0
    http_resp = client.post(
        "/api/chat/",
        data=json.dumps({"message": prompt}),
        content_type="application/json",
    )
    payload: dict[str, Any] = {}
    try:
        payload = json.loads(http_resp.content.decode("utf-8"))
    except Exception:
        payload = {}

    log_row = (
        ChatbotLog.objects.filter(log_id__gt=last_log_id, user_message=prompt)
        .order_by("-log_id")
        .first()
    )

    resolved_intent = str(getattr(log_row, "resolved_intent", "") or payload.get("resolved_intent") or "").strip().lower()
    fallback_used = bool(getattr(log_row, "fallback_used", False) or payload.get("needs_clarification"))
    response_text = str(payload.get("fulfillmentText") or "").strip()
    has_cards = bool(isinstance(payload.get("recommendation_trace"), list) and payload.get("recommendation_trace"))
    return {
        "prompt": prompt,
        "http_status": int(http_resp.status_code),
        "detected_intent": _extract_detected_intent(payload),
        "resolved_intent": resolved_intent,
        "fallback_used": fallback_used,
        "has_cards": has_cards,
        "response_summary": response_text[:260],
        "payload": payload,
        "actor": actor_username,
    }


def _pass_case(row: dict[str, Any], case: Case) -> tuple[bool, str]:
    resolved = str(row.get("resolved_intent") or "")
    text = str(row.get("response_summary") or "").lower()
    has_cards = bool(row.get("has_cards"))
    payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}

    if int(row.get("http_status") or 0) != 200:
        return False, "http_not_200"
    if case.expected_intents and resolved not in case.expected_intents:
        return False, f"unexpected_intent:{resolved}"
    if case.expect_cards is True and not has_cards:
        return False, "cards_expected"
    if case.expect_cards is False and has_cards:
        return False, "cards_not_expected"

    if case.group == "out_of_scope":
        if not (
            "bayawan tourism" in text
            or ("approved accommodations" in text and "tours" in text and "directions" in text)
        ):
            return False, "missing_scope_redirect"
    if case.group == "gibberish":
        if "not sure i understood your request" not in text:
            return False, "missing_gibberish_clarification"
    if case.group == "small_talk":
        if any(token in text for token in ("here are approved accommodations", "here are available tour schedules")):
            return False, "small_talk_triggered_recommendations"
    if case.group == "tour_flow" and case.prompt.strip().lower() == "yes":
        if "pending review" not in text and "booking request has been submitted" not in text:
            return False, "tour_yes_not_submitted"
    if case.expect_preview_only:
        if "booking preview" not in text and "estimated accommodation booking preview" not in text:
            return False, "missing_preview_wording"
        if "pending review" in text or "submitted" in text:
            return False, "preview_entered_real_booking"
        billing_label = str(payload.get("billing_link_label") or "").strip().lower()
        billing_url = str(payload.get("billing_link") or "").strip().lower()
        if billing_url and ("treasurer" in billing_url or "payment" in billing_url) and "official" not in billing_label:
            return False, "preview_has_payment_link"
    return True, ""


def run() -> dict[str, Any]:
    guest = _ensure_guest(
        username="demo_guest_smalltalk_scope",
        email="demo.smalltalk.scope@ibayaw.local",
        first_name="Demo",
        last_name="Guest",
    )
    employee = _ensure_employee()
    _ensure_tour_and_assignments(employee)
    tour = Tour_Add.objects.filter(tour_name__iexact="Bayawan Food and Culture Trail").first()
    if tour is not None:
        for sched in Tour_Schedule.objects.filter(tour=tour).exclude(status__iexact="cancelled"):
            if int(getattr(sched, "slots_available", 0) or 0) < 10:
                sched.slots_available = 30
                sched.slots_booked = max(0, int(getattr(sched, "slots_booked", 0) or 0))
                sched.save(update_fields=["slots_available", "slots_booked"])

    def _guest_client() -> Client:
        c = Client(HTTP_HOST="127.0.0.1")
        c.force_login(guest)
        return c

    rows: list[dict[str, Any]] = []
    one_turn_cases: list[Case] = [
        Case("small_talk", "hi", ("small_talk",), expect_cards=False),
        Case("small_talk", "hello", ("small_talk",), expect_cards=False),
        Case("small_talk", "good morning", ("small_talk",), expect_cards=False),
        Case("small_talk", "hi, I'm Renold", ("small_talk",), expect_cards=False),
        Case("small_talk", "my name is Renold", ("small_talk",), expect_cards=False),
        Case("small_talk", "thank you", ("small_talk",), expect_cards=False),
        Case("small_talk", "what can you do?", ("small_talk", "role_help"), expect_cards=False),
        Case("out_of_scope", "who is the president?", ("out_of_scope",), expect_cards=False),
        Case("out_of_scope", "solve my math homework", ("out_of_scope",), expect_cards=False),
        Case("out_of_scope", "write me an essay", ("out_of_scope",), expect_cards=False),
        Case("out_of_scope", "explain Java programming", ("out_of_scope",), expect_cards=False),
        Case("gibberish", "asdfqwe", ("clarification",), expect_cards=False),
        Case("gibberish", "qweqweqwe", ("clarification",), expect_cards=False),
        Case("gibberish", "adsceawarwaeve", ("clarification",), expect_cards=False),
        Case("combined", "hi, show available tours", ("get_recommendation",), expect_cards=True),
        Case(
            "combined",
            "hello, hotel in Suba for 2 guests under 1500",
            ("get_accommodation_recommendation", "book_accommodation"),
            expect_cards=True,
        ),
        Case("combined", "thanks, show rooms for Hotel Maefinn", ("get_accommodation_room_listing",), expect_cards=True),
        Case("combined", "I'm Renold, book Bayawan Food and Culture Trail", ("book_tour_via_link",), expect_cards=False),
    ]

    pending_before = Pending.objects.filter(guest_id=guest).count()
    for case in one_turn_cases:
        client = _guest_client()
        row = _post_chat(client, case.prompt, actor_username=guest.username)
        passed, reason = _pass_case(row, case)
        row["group"] = case.group
        row["pass"] = bool(passed)
        row["failure_reason"] = reason
        rows.append(row)

    boundary_preview_flow = [
        Case(
            "boundary_preview",
            "create booking preview for Hotel Maefinn Family Suite",
            ("book_accommodation", "book_accommodation_preview"),
            expect_preview_only=True,
        ),
        Case(
            "boundary_preview",
            "May 10 to May 12 for 3 guests",
            ("book_accommodation", "book_accommodation_preview"),
            expect_preview_only=True,
        ),
    ]
    client = _guest_client()
    for case in boundary_preview_flow:
        row = _post_chat(client, case.prompt, actor_username=guest.username)
        passed, reason = _pass_case(row, case)
        row["group"] = case.group
        row["pass"] = bool(passed)
        row["failure_reason"] = reason
        rows.append(row)

    tour_flow = [
        Case("tour_flow", "book Bayawan Food and Culture Trail", ("book_tour_via_link",), expect_cards=False),
        Case("tour_flow", "May 5 for 2 adults", ("book_tour_via_link",), expect_cards=False),
        Case("tour_flow", "YES", ("book_tour_via_link",), expect_cards=False),
    ]
    client = _guest_client()
    for case in tour_flow:
        row = _post_chat(client, case.prompt, actor_username=guest.username)
        passed, reason = _pass_case(row, case)
        row["group"] = case.group
        row["pass"] = bool(passed)
        row["failure_reason"] = reason
        rows.append(row)

    pending_after = Pending.objects.filter(guest_id=guest).count()
    pending_delta = max(0, pending_after - pending_before)

    if pending_delta <= 0:
        rows.append(
            {
                "group": "tour_flow",
                "prompt": "YES",
                "resolved_intent": "",
                "detected_intent": "",
                "fallback_used": False,
                "has_cards": False,
                "http_status": 200,
                "response_summary": "No new Pending row detected for tour flow.",
                "pass": False,
                "failure_reason": "tour_pending_not_created",
                "actor": guest.username,
                "payload": {},
            }
        )

    summary = {
        "total": len(rows),
        "passed": sum(1 for r in rows if r.get("pass")),
        "failed": [r for r in rows if not r.get("pass")],
        "pending_delta": pending_delta,
        "rows": rows,
    }
    return summary


if __name__ == "__main__":
    print(json.dumps(run(), indent=2, ensure_ascii=False))
