from django.core.management.base import BaseCommand

from admin_app.models import Employee, TourAssignment
from guest_app.models import Pending


class Command(BaseCommand):
    help = "Audit visibility of a pending tour booking for assigned employees."

    def add_arguments(self, parser):
        parser.add_argument("--booking-id", type=int, required=True, help="Pending booking primary key.")

    def handle(self, *args, **options):
        booking_id = int(options["booking_id"])
        booking = (
            Pending.objects.select_related("guest_id", "tour_id", "sched_id")
            .filter(pk=booking_id)
            .first()
        )
        if booking is None:
            self.stdout.write(self.style.ERROR(f"Booking #{booking_id} not found."))
            return

        status = str(getattr(booking, "status", "") or "").strip()
        sched = getattr(booking, "sched_id", None)
        tour = getattr(booking, "tour_id", None)
        guest = getattr(booking, "guest_id", None)

        self.stdout.write("=== Booking Visibility Audit ===")
        self.stdout.write(f"Booking ID: {booking.pk}")
        self.stdout.write(f"Status: {status or 'N/A'}")
        self.stdout.write(f"Tour: {getattr(tour, 'tour_name', 'N/A')}")
        self.stdout.write(f"Schedule ID: {getattr(sched, 'sched_id', 'N/A')}")
        self.stdout.write(f"Schedule Date: {getattr(sched, 'start_time', 'N/A')}")
        self.stdout.write(f"Guest: {getattr(guest, 'username', 'N/A')} ({getattr(guest, 'email', 'N/A')})")
        self.stdout.write(f"Total Guests: {getattr(booking, 'total_guests', 'N/A')}")

        assignment_rows = list(
            TourAssignment.objects.select_related("employee")
            .filter(schedule=sched)
        ) if sched is not None else []
        self.stdout.write(f"Assigned Employee Count: {len(assignment_rows)}")
        for row in assignment_rows:
            emp = row.employee
            self.stdout.write(
                f"- {emp.emp_id} | {emp.first_name} {emp.last_name} | status={getattr(emp, 'status', '')}"
            )

        if not assignment_rows:
            self.stdout.write(
                self.style.WARNING(
                    "No employee assignment exists for this schedule. Non-admin employees will not see this booking in pending queue."
                )
            )

        visible_employee_ids = {
            str(row.employee.emp_id)
            for row in assignment_rows
            if str(getattr(row.employee, "status", "") or "").strip().lower() == "accepted"
        }
        self.stdout.write(
            f"Visible to accepted assigned employees: {', '.join(sorted(visible_employee_ids)) or 'None'}"
        )

        expected_visible = status.lower() == "pending" and bool(visible_employee_ids)
        reason = "OK"
        if status.lower() != "pending":
            reason = "Booking is not pending."
        elif not visible_employee_ids:
            reason = "No accepted assigned employee for the booking schedule."
        self.stdout.write(f"Should appear in employee pending page: {expected_visible}")
        self.stdout.write(f"Reason: {reason}")
