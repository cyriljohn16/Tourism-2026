from __future__ import annotations

from typing import Iterable

from django.db import transaction
from django.urls import reverse

from admin_app.models import Employee, InAppNotification, TourAssignment


def create_notification(
    *,
    recipient_guest=None,
    recipient_employee=None,
    title: str,
    message: str,
    notification_type: str = "system",
    related_object_id: str = "",
    url: str = "",
    dedupe_key: str = "",
):
    if bool(recipient_guest) == bool(recipient_employee):
        return None

    payload = {
        "recipient_guest": recipient_guest,
        "recipient_employee": recipient_employee,
        "title": str(title or "").strip()[:140] or "Notification",
        "message": str(message or "").strip()[:1200] or "You have an update.",
        "notification_type": str(notification_type or "system").strip().lower()[:20] or "system",
        "related_object_id": str(related_object_id or "").strip()[:40],
        "url": str(url or "").strip()[:255],
        "dedupe_key": str(dedupe_key or "").strip()[:160],
    }

    with transaction.atomic():
        if payload["dedupe_key"]:
            existing = InAppNotification.objects.filter(
                recipient_guest=payload["recipient_guest"],
                recipient_employee=payload["recipient_employee"],
                dedupe_key=payload["dedupe_key"],
            ).first()
            if existing:
                return existing
        return InAppNotification.objects.create(**payload)


def notify_admins(*, title: str, message: str, notification_type: str = "system", url: str = "", dedupe_key: str = ""):
    admins = Employee.objects.filter(status__iexact="accepted", role__iexact="admin")
    for admin_user in admins:
        create_notification(
            recipient_employee=admin_user,
            title=title,
            message=message,
            notification_type=notification_type,
            url=url,
            dedupe_key=f"{dedupe_key}:admin:{admin_user.emp_id}" if dedupe_key else "",
        )


def notify_assigned_employees_for_schedule(*, schedule, title: str, message: str, notification_type: str = "assignment", url: str = "", dedupe_key_prefix: str = ""):
    assignments = TourAssignment.objects.select_related("employee").filter(schedule=schedule)
    for row in assignments:
        employee = row.employee
        if not employee or str(employee.status or "").lower() != "accepted":
            continue
        dedupe_key = f"{dedupe_key_prefix}:emp:{employee.emp_id}" if dedupe_key_prefix else ""
        create_notification(
            recipient_employee=employee,
            title=title,
            message=message,
            notification_type=notification_type,
            url=url,
            dedupe_key=dedupe_key,
            related_object_id=getattr(schedule, "sched_id", ""),
        )


def notify_accommodation_owner(*, accommodation, title: str, message: str, notification_type: str = "booking", url: str = "", dedupe_key: str = "", related_object_id: str = ""):
    owner = getattr(accommodation, "owner", None)
    if owner is None:
        return None
    return create_notification(
        recipient_guest=owner,
        title=title,
        message=message,
        notification_type=notification_type,
        url=url,
        dedupe_key=dedupe_key,
        related_object_id=related_object_id or str(getattr(accommodation, "accom_id", "") or ""),
    )


def serialize_notification_rows(rows: Iterable[InAppNotification]):
    out = []
    for row in rows:
        out.append(
            {
                "id": row.id,
                "title": row.title,
                "message": row.message,
                "type": row.notification_type,
                "url": row.url or "",
                "is_read": bool(row.is_read),
                "display_date": row.created_at.strftime("%b %d, %Y %I:%M %p") if row.created_at else "",
            }
        )
    return out


def default_notifications_url_for_role(role: str):
    role_key = str(role or "").strip().lower()
    if role_key == "owner":
        return reverse("admin_app:owner_report_submit")
    if role_key == "employee":
        return reverse("admin_app:employee_notifications")
    if role_key == "admin":
        return reverse("admin_app:admin_dashboard")
    return reverse("main-page")
