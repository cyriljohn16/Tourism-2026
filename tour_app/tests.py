from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from admin_app.models import Employee, TourAssignment
from admin_app.models import InAppNotification
from guest_app.models import Pending
from tour_app.models import Tour_Add, Tour_Schedule


class TourBookingAssignmentApprovalTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.guest = user_model.objects.create_user(
            username="tour_assign_guest",
            email="tour_assign_guest@example.com",
            password="secure-pass-123",
            first_name="Tour",
            last_name="Guest",
        )
        self.assigned_employee = Employee.objects.create(
            first_name="Assigned",
            last_name="Employee",
            username="assigned_emp_user",
            age=28,
            phone_number="09995551011",
            email="assigned_emp@example.com",
            sex="F",
            status="accepted",
        )
        self.unassigned_employee = Employee.objects.create(
            first_name="Unassigned",
            last_name="Employee",
            username="unassigned_emp_user",
            age=30,
            phone_number="09995551012",
            email="unassigned_emp@example.com",
            sex="M",
            status="accepted",
        )

        self.tour = Tour_Add.objects.create(
            tour_name="Bayawan City Highlights",
            description="Tour booking assignment test tour",
            publication_status="published",
        )
        now = timezone.now()
        self.schedule = Tour_Schedule.objects.create(
            tour=self.tour,
            start_time=now,
            end_time=now + timezone.timedelta(hours=4),
            price="500.00",
            slots_available=10,
            slots_booked=0,
            duration_days=1,
            status="active",
        )
        TourAssignment.objects.create(employee=self.assigned_employee, schedule=self.schedule)

        self.pending_booking = Pending.objects.create(
            guest_id=self.guest,
            sched_id=self.schedule,
            tour_id=self.tour,
            status="Pending",
            total_guests=2,
            your_name="Tour Guest",
            your_email=self.guest.email,
            your_phone="09170000000",
            num_adults=2,
            num_children=0,
        )

    def _set_employee_session(self, employee, *, is_admin=False):
        session = self.client.session
        session["user_type"] = "employee"
        session["employee_id"] = employee.emp_id
        session["is_admin"] = bool(is_admin)
        session.save()

    def test_assigned_employee_can_update_pending_booking_for_assigned_schedule(self):
        self._set_employee_session(self.assigned_employee, is_admin=False)
        response = self.client.post(
            reverse("tour_app:status_update", kwargs={"pk": self.pending_booking.pk}),
            data={
                "status": "Accepted",
                "guest_email": self.pending_booking.your_email,
                "guest_name": self.pending_booking.your_name,
            },
        )
        self.assertEqual(response.status_code, 302)
        self.pending_booking.refresh_from_db()
        self.assertEqual(self.pending_booking.status, "Accepted")
        self.assertTrue(
            InAppNotification.objects.filter(
                recipient_guest=self.guest,
                dedupe_key=f"tour-status-{self.pending_booking.id}-accepted",
            ).exists()
        )

    def test_unassigned_employee_cannot_update_pending_booking_for_unassigned_schedule(self):
        self._set_employee_session(self.unassigned_employee, is_admin=False)
        response = self.client.post(
            reverse("tour_app:status_update", kwargs={"pk": self.pending_booking.pk}),
            data={
                "status": "Accepted",
                "guest_email": self.pending_booking.your_email,
                "guest_name": self.pending_booking.your_name,
            },
        )
        self.assertEqual(response.status_code, 302)
        self.pending_booking.refresh_from_db()
        self.assertEqual(self.pending_booking.status, "Pending")
