import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import Client
from django.utils import timezone

from admin_app.models import Employee, TourAssignment
from ai_chatbot.models import ChatbotLog
from tour_app.models import Tour_Add, Tour_Schedule


@dataclass
class PromptCase:
    role: str
    prompt: str
    expected: list[str]


def _ensure_guest(username: str, email: str, first_name: str, last_name: str):
    User = get_user_model()
    user = User.objects.filter(username=username).first()
    if user is None:
        user = User.objects.create_user(
            username=username,
            email=email,
            password="demo-pass-123",
        )
    user.first_name = first_name
    user.last_name = last_name
    user.country_of_origin = getattr(user, "country_of_origin", "") or "Philippines"
    user.phone_number = getattr(user, "phone_number", "") or f"09{timezone.now().strftime('%f')[:9]}"
    user.sex = getattr(user, "sex", "") or "M"
    user.is_active = True
    user.save()
    return user


def _ensure_employee():
    employee = Employee.objects.filter(email="demo.employee.regression@ibayaw.local").first()
    if employee is None:
        employee = Employee.objects.create(
            first_name="Demo",
            last_name="Employee",
            middle_name="R",
            username="demo_employee_regression",
            age=28,
            phone_number="09991234567",
            email="demo.employee.regression@ibayaw.local",
            sex="M",
            role="employee",
            status="accepted",
            is_active=True,
            is_staff=False,
            is_superuser=False,
        )
        employee.set_password("demo-pass-123")
    else:
        employee.status = "accepted"
    employee.save()
    return employee


def _ensure_tour_and_assignments(employee):
    tour = (
        Tour_Add.objects.filter(tour_name__iexact="Bayawan Food and Culture Trail")
        .order_by("tour_id")
        .first()
    )
    if tour is None:
        tour = Tour_Add.objects.create(
            tour_name="Bayawan Food and Culture Trail",
            description="Cultural immersion trail in Bayawan.",
            publication_status="published",
        )
    else:
        if tour.publication_status != "published":
            tour.publication_status = "published"
            tour.save(update_fields=["publication_status"])

    may5_start = timezone.make_aware(datetime(2026, 5, 5, 8, 0, 0))
    may5_end = timezone.make_aware(datetime(2026, 5, 5, 17, 0, 0))
    may7_start = timezone.make_aware(datetime(2026, 5, 7, 8, 0, 0))
    may7_end = timezone.make_aware(datetime(2026, 5, 7, 17, 0, 0))

    sched_may5 = Tour_Schedule.objects.filter(tour=tour, start_time=may5_start).first()
    if sched_may5 is None:
        sched_may5 = Tour_Schedule.objects.create(
            tour=tour,
            start_time=may5_start,
            end_time=may5_end,
            price=Decimal("370.00"),
            slots_available=30,
            slots_booked=3,
            duration_days=1,
            status="active",
        )

    sched_may7 = Tour_Schedule.objects.filter(tour=tour, start_time=may7_start).first()
    if sched_may7 is None:
        sched_may7 = Tour_Schedule.objects.create(
            tour=tour,
            start_time=may7_start,
            end_time=may7_end,
            price=Decimal("370.00"),
            slots_available=30,
            slots_booked=2,
            duration_days=1,
            status="active",
        )

    TourAssignment.objects.get_or_create(employee=employee, schedule=sched_may5)
    TourAssignment.objects.get_or_create(employee=employee, schedule=sched_may7)


def _expected_cases():
    return [
        PromptCase("guest", "help me plan my bayawan trip", ["plan_bayawan_stay", "role_help", "small_talk"]),
        PromptCase("guest", "budget 8000", ["plan_bayawan_stay"]),
        PromptCase("guest", "make it cheaper", ["plan_bayawan_stay"]),
        PromptCase("guest", "make it family-friendly", ["plan_bayawan_stay", "get_accommodation_recommendation"]),
        PromptCase("guest", "hotel in suba for 2 guests under 1500", ["get_accommodation_recommendation"]),
        PromptCase("guest", "how to get to hotel maefinn", ["travel_guidance"]),
        PromptCase("guest", "reset", ["reset_command"]),
        PromptCase("guest", "show available tours", ["get_recommendation"]),
        PromptCase("guest", "book bayawan food and culture trail", ["book_tour_via_link", "get_recommendation"]),
        PromptCase("guest", "may 5 for 2 adults", ["book_tour_via_link", "get_recommendation", "small_talk", "clarification"]),
        PromptCase("guest", "yes", ["book_tour_via_link", "get_recommendation", "small_talk", "clarification"]),
        PromptCase("employee", "show my assigned tours", ["employee_assigned_tours"]),
        PromptCase("employee", "open may 5 assignment", ["employee_open_assignment"]),
        PromptCase("employee", "accept", ["employee_update_assignment"]),
        PromptCase("owner", "what can I do here as an owner?", ["role_help"]),
        PromptCase("owner", "update my accommodation links", ["owner_listing_visibility", "owner_listing_status", "role_help"]),
        PromptCase("owner", "submit monthly report", ["owner_submit_monthly_report", "reporting_summary"]),
        PromptCase("admin", "show this month's summary", ["reporting_summary"]),
        PromptCase("admin", "show accommodation reports", ["reporting_summary"]),
        PromptCase("admin", "show tourist influx for Hotel Maefinn", ["reporting_summary"]),
    ]


def _new_client_for_role(role, guest_user, owner_user, admin_user, employee):
    client = Client(HTTP_HOST="127.0.0.1")
    if role == "guest":
        client.force_login(guest_user)
    elif role == "owner":
        client.force_login(owner_user)
    elif role == "admin":
        client.force_login(admin_user)
    elif role == "employee":
        session = client.session
        session["user_type"] = "employee"
        session["employee_id"] = employee.emp_id
        session["is_admin"] = False
        session["first_name"] = employee.first_name
        session.save()
    return client


def _extract_detected_intent(response_payload):
    intent_classifier = response_payload.get("intent_classifier") if isinstance(response_payload, dict) else {}
    if not isinstance(intent_classifier, dict):
        return ""
    source = str(intent_classifier.get("source") or "").strip().lower()
    top3 = intent_classifier.get("top_3") if isinstance(intent_classifier.get("top_3"), list) else []
    if top3 and isinstance(top3[0], dict):
        return str(top3[0].get("intent") or top3[0].get("raw_label") or "").strip().lower()
    if source == "deterministic_pre_route":
        return "deterministic_pre_route"
    return ""


def _is_rule_based_shortcut_intent(intent_value: str):
    normalized = str(intent_value or "").strip().lower()
    return normalized in {
        "travel_guidance",
        "reset_command",
        "book_tour_via_link",
        "employee_assigned_tours",
        "employee_open_assignment",
        "employee_update_assignment",
        "owner_submit_monthly_report",
        "owner_accommodation_overview",
        "owner_listing_visibility",
        "owner_listing_status",
        "open_dashboard",
        "open_owner_dashboard",
    }


def run():
    guest_user = _ensure_guest(
        username="demo_guest_regression",
        email="demo.guest.regression@ibayaw.local",
        first_name="Demo",
        last_name="Guest",
    )
    owner_user = _ensure_guest(
        username="demo_owner_regression",
        email="demo.owner.regression@ibayaw.local",
        first_name="Demo",
        last_name="Owner",
    )
    owner_group, _ = Group.objects.get_or_create(name="accommodation_owner")
    owner_user.groups.add(owner_group)

    admin_user = _ensure_guest(
        username="demo_admin_regression",
        email="demo.admin.regression@ibayaw.local",
        first_name="Demo",
        last_name="Admin",
    )
    admin_user.is_staff = True
    admin_user.save(update_fields=["is_staff"])

    employee = _ensure_employee()
    _ensure_tour_and_assignments(employee)

    role_clients = {
        "guest": _new_client_for_role("guest", guest_user, owner_user, admin_user, employee),
        "owner": _new_client_for_role("owner", guest_user, owner_user, admin_user, employee),
        "admin": _new_client_for_role("admin", guest_user, owner_user, admin_user, employee),
        "employee": _new_client_for_role("employee", guest_user, owner_user, admin_user, employee),
    }

    cases = _expected_cases()
    rows = []

    for idx, case in enumerate(cases, start=1):
        client = role_clients[case.role]
        last_log_id = ChatbotLog.objects.order_by("-log_id").values_list("log_id", flat=True).first() or 0
        http_resp = client.post(
            "/api/chat/",
            data=json.dumps({"message": case.prompt}),
            content_type="application/json",
        )
        response_json = {}
        try:
            response_json = json.loads(http_resp.content.decode("utf-8"))
        except Exception:
            response_json = {}

        new_log = (
            ChatbotLog.objects.filter(log_id__gt=last_log_id)
            .order_by("-log_id")
            .first()
        )
        resolved_intent = str(getattr(new_log, "resolved_intent", "") or "").strip().lower() if new_log else ""
        fallback_used = bool(getattr(new_log, "fallback_used", False)) if new_log else False
        intent_source = str(getattr(new_log, "intent_classifier_source", "") or "").strip().lower() if new_log else ""
        provenance = new_log.provenance_json if (new_log and isinstance(new_log.provenance_json, dict)) else {}
        extra = provenance.get("extra") if isinstance(provenance.get("extra"), dict) else {}
        deterministic_used = bool(
            intent_source == "deterministic_pre_route"
            or str(provenance.get("intent_source") or "").strip().lower() == "deterministic_pre_route"
            or bool(extra.get("deterministic_routing_applied"))
            or _is_rule_based_shortcut_intent(resolved_intent)
        )
        detected_intent = _extract_detected_intent(response_json)
        if (not detected_intent or detected_intent == "n/a") and isinstance(extra, dict):
            detected_intent = str(extra.get("low_confidence_predicted_intent") or "").strip().lower()
        if not detected_intent and intent_source == "deterministic_pre_route":
            detected_intent = "deterministic_pre_route"
        if not detected_intent:
            detected_intent = str(provenance.get("intent_source") or "").strip().lower()

        passed = resolved_intent in case.expected
        rows.append(
            {
                "id": idx,
                "role": case.role,
                "prompt": case.prompt,
                "expected": case.expected,
                "http_status": int(http_resp.status_code),
                "detected_intent": detected_intent or "n/a",
                "final_resolved_intent": resolved_intent or "n/a",
                "intent_source": intent_source or "n/a",
                "deterministic_routing_used": deterministic_used,
                "fallback_used": fallback_used,
                "pass": passed and int(http_resp.status_code) == 200,
            }
        )

    return rows


if __name__ == "__main__":
    results = run()
    print(json.dumps(results, indent=2))
