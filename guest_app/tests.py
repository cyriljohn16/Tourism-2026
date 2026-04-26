import json
import hashlib
import hmac
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import Group
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from admin_app.models import Accomodation, Room
from guest_app.models import AccommodationBooking, Billing, AccommodationBookingCompanion


class GuestAccommodationApprovalVisibilityTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.user = user_model.objects.create_user(
            username="guest_filter_user",
            email="guest_filter_user@example.com",
            password="secure-pass-123",
            first_name="Guest",
            last_name="Filter",
        )
        self.client.force_login(self.user)
        self.other_user = user_model.objects.create_user(
            username="guest_filter_other_user",
            email="guest_filter_other_user@example.com",
            password="secure-pass-123",
            first_name="Guest",
            last_name="Other",
        )

        self.accepted_accom = Accomodation.objects.create(
            company_name="Accepted Hotel",
            email_address="accepted@example.com",
            location="Bayawan",
            company_type="hotel",
            password="accom-pass-1",
            phone_number="09990000001",
            approval_status="accepted",
            status="accepted",
        )
        self.pending_accom = Accomodation.objects.create(
            company_name="Pending Hotel",
            email_address="pending@example.com",
            location="Bayawan",
            company_type="hotel",
            password="accom-pass-2",
            phone_number="09990000002",
            approval_status="pending",
            status="pending",
        )
        self.declined_accom = Accomodation.objects.create(
            company_name="Declined Inn",
            email_address="declined@example.com",
            location="Bayawan",
            company_type="inn",
            password="accom-pass-3",
            phone_number="09990000003",
            approval_status="declined",
            status="declined",
        )

        self.accepted_room = Room.objects.create(
            accommodation=self.accepted_accom,
            room_name="Accepted Room",
            person_limit=2,
            current_availability=2,
            price_per_night=Decimal("1200.00"),
            status="AVAILABLE",
        )
        self.pending_room = Room.objects.create(
            accommodation=self.pending_accom,
            room_name="Pending Room",
            person_limit=2,
            current_availability=2,
            price_per_night=Decimal("1200.00"),
            status="AVAILABLE",
        )
        self.declined_room = Room.objects.create(
            accommodation=self.declined_accom,
            room_name="Declined Room",
            person_limit=2,
            current_availability=2,
            price_per_night=Decimal("1200.00"),
            status="AVAILABLE",
        )
        self.prototype_accom = Accomodation.objects.create(
            company_name="Prototype Hidden Stay",
            email_address="prototype_hidden_stay@placeholder.local",
            location="Bayawan",
            company_type="hotel",
            password="demo-password",
            phone_number="09990000077",
            approval_status="accepted",
            status="accepted",
            is_active=True,
        )
        self.prototype_room = Room.objects.create(
            accommodation=self.prototype_accom,
            room_name="Prototype Room",
            person_limit=2,
            current_availability=2,
            price_per_night=Decimal("1200.00"),
            status="AVAILABLE",
        )

    def test_pending_and_declined_do_not_appear_on_guest_accommodation_page(self):
        response = self.client.get(reverse("accommodation_page"))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode("utf-8")
        self.assertIn("Accepted Hotel", content)
        self.assertNotIn("Pending Hotel", content)
        self.assertNotIn("Declined Inn", content)
        self.assertNotIn("Prototype Hidden Stay", content)

    @override_settings(TOURISM_APPROVED_ACCOMMODATION_IDS=[])
    def test_guest_accommodation_page_respects_approved_allowlist(self):
        with self.settings(TOURISM_APPROVED_ACCOMMODATION_IDS=[self.accepted_accom.accom_id]):
            response = self.client.get(reverse("accommodation_page"))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode("utf-8")
        self.assertIn("Accepted Hotel", content)
        # Any accommodation not in explicit allowlist must stay hidden.
        self.assertNotIn("Pending Hotel", content)
        self.assertNotIn("Declined Inn", content)

    def test_only_accepted_accommodation_is_bookable(self):
        check_in = timezone.now().date() + timedelta(days=2)
        check_out = check_in + timedelta(days=2)

        pending_response = self.client.post(
            reverse("accommodation_book"),
            data={
                "room_id": self.pending_room.room_id,
                "check_in": check_in.isoformat(),
                "check_out": check_out.isoformat(),
                "num_guests": 1,
            },
        )
        self.assertEqual(pending_response.status_code, 410)

        accepted_response = self.client.post(
            reverse("accommodation_book"),
            data={
                "room_id": self.accepted_room.room_id,
                "check_in": check_in.isoformat(),
                "check_out": check_out.isoformat(),
                "num_guests": 1,
            },
        )
        self.assertEqual(accepted_response.status_code, 410)
        body = accepted_response.json()
        self.assertFalse(body.get("success"))
        self.assertEqual(body.get("code"), "accommodation_transaction_disabled")

    def test_pending_and_declined_rooms_are_not_billable(self):
        pending_response = self.client.post(
            reverse("accommodation_billing"),
            data={
                "room_id": self.pending_room.room_id,
                "nights": 2,
            },
        )
        self.assertEqual(pending_response.status_code, 410)

        accepted_response = self.client.post(
            reverse("accommodation_billing"),
            data={
                "room_id": self.accepted_room.room_id,
                "nights": 2,
            },
        )
        self.assertEqual(accepted_response.status_code, 410)
        self.assertFalse(accepted_response.json().get("success"))

    def test_overlapping_room_booking_is_blocked(self):
        check_in = timezone.now().date() + timedelta(days=5)
        check_out = check_in + timedelta(days=2)
        AccommodationBooking.objects.create(
            guest=self.other_user,
            accommodation=self.accepted_accom,
            room=self.accepted_room,
            check_in=check_in,
            check_out=check_out,
            num_guests=1,
            status="confirmed",
            total_amount=Decimal("2400.00"),
        )

        response = self.client.post(
            reverse("accommodation_book"),
            data={
                "room_id": self.accepted_room.room_id,
                "check_in": (check_in + timedelta(days=1)).isoformat(),
                "check_out": (check_out + timedelta(days=1)).isoformat(),
                "num_guests": 1,
            },
        )
        self.assertEqual(response.status_code, 409)
        body = response.json()
        self.assertFalse(body.get("success"))
        self.assertIn("already booked", str(body.get("message", "")).lower())
        self.assertEqual(
            AccommodationBooking.objects.filter(room=self.accepted_room).count(),
            1,
        )

    def test_booking_with_companions_creates_linked_companion_records(self):
        check_in = timezone.now().date() + timedelta(days=10)
        check_out = check_in + timedelta(days=2)
        payload = [
            {"name": "Juan Dela Cruz", "contact_info": "09171234567"},
            {"name": "Maria Cruz", "contact_info": "maria@example.com"},
        ]

        response = self.client.post(
            reverse("accommodation_book"),
            data={
                "room_id": self.accepted_room.room_id,
                "check_in": check_in.isoformat(),
                "check_out": check_out.isoformat(),
                "num_guests": 2,
                "companions_json": json.dumps(payload),
            },
        )
        self.assertEqual(response.status_code, 200)
        booking = AccommodationBooking.objects.get(
            guest=self.user,
            room=self.accepted_room,
            check_in=check_in,
            check_out=check_out,
        )
        companions = AccommodationBookingCompanion.objects.filter(booking=booking)
        self.assertEqual(companions.count(), 2)
        self.assertTrue(
            companions.filter(
                companion_name="Juan Dela Cruz",
                companion_contact="09171234567",
            ).exists()
        )
        self.assertTrue(
            companions.filter(
                companion_name="Maria Cruz",
                companion_contact="maria@example.com",
            ).exists()
        )

    def test_booking_with_invalid_companion_payload_returns_validation_error(self):
        check_in = timezone.now().date() + timedelta(days=12)
        check_out = check_in + timedelta(days=2)

        response = self.client.post(
            reverse("accommodation_book"),
            data={
                "room_id": self.accepted_room.room_id,
                "check_in": check_in.isoformat(),
                "check_out": check_out.isoformat(),
                "num_guests": 1,
                "companions_json": '{"invalid": "object"}',
            },
        )
        self.assertEqual(response.status_code, 400)
        body = response.json()
        self.assertFalse(body.get("success"))
        self.assertIn("companions", body.get("errors", {}))


class GuestAccommodationRoleEnforcementTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.guest_user = user_model.objects.create_user(
            username="rbac_guest_user",
            email="rbac_guest_user@example.com",
            password="secure-pass-123",
            first_name="Rbac",
            last_name="Guest",
        )
        self.owner_user = user_model.objects.create_user(
            username="rbac_owner_user",
            email="rbac_owner_user@example.com",
            password="secure-pass-123",
            first_name="Rbac",
            last_name="Owner",
        )
        owner_group, _ = Group.objects.get_or_create(name="accommodation_owner")
        self.owner_user.groups.add(owner_group)
        self.declined_owner_user = user_model.objects.create_user(
            username="rbac_declined_owner_user",
            email="rbac_declined_owner_user@example.com",
            password="secure-pass-123",
            first_name="Rbac",
            last_name="Declined",
        )
        declined_group, _ = Group.objects.get_or_create(name="accommodation_owner_declined")
        self.declined_owner_user.groups.add(declined_group)

        self.accepted_accom = Accomodation.objects.create(
            company_name="RBAC Accepted Hotel",
            email_address="rbac-accepted@example.com",
            location="Bayawan",
            company_type="hotel",
            password="accom-pass-rbac",
            phone_number="09990000111",
            approval_status="accepted",
            status="accepted",
        )
        self.accepted_room = Room.objects.create(
            accommodation=self.accepted_accom,
            room_name="RBAC Room",
            person_limit=2,
            current_availability=2,
            price_per_night=Decimal("1500.00"),
            status="AVAILABLE",
        )

    def test_accommodation_owner_cannot_access_guest_accommodation_page(self):
        self.client.force_login(self.owner_user)
        response = self.client.get(reverse("accommodation_page"))
        self.assertEqual(response.status_code, 403)

    def test_accommodation_owner_cannot_access_guest_booking_history(self):
        self.client.force_login(self.owner_user)
        response = self.client.get(reverse("my_accommodation_bookings"))
        self.assertEqual(response.status_code, 403)

    def test_declined_owner_group_user_can_still_access_guest_pages(self):
        self.client.force_login(self.declined_owner_user)
        response = self.client.get(reverse("accommodation_page"))
        self.assertEqual(response.status_code, 200)

    def test_declined_owner_group_user_can_login_via_guest_login(self):
        response = self.client.post(
            reverse("login"),
            data={
                "email": "rbac_declined_owner_user@example.com",
                "password": "secure-pass-123",
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload.get("success"))

    def test_accommodation_owner_cannot_preview_billing_or_create_booking(self):
        self.client.force_login(self.owner_user)
        check_in = timezone.now().date() + timedelta(days=3)
        check_out = check_in + timedelta(days=2)

        billing_response = self.client.post(
            reverse("accommodation_billing"),
            data={
                "room_id": self.accepted_room.room_id,
                "check_in": check_in.isoformat(),
                "check_out": check_out.isoformat(),
            },
        )
        self.assertEqual(billing_response.status_code, 403)

        before_count = AccommodationBooking.objects.count()
        booking_response = self.client.post(
            reverse("accommodation_book"),
            data={
                "room_id": self.accepted_room.room_id,
                "check_in": check_in.isoformat(),
                "check_out": check_out.isoformat(),
                "num_guests": 1,
            },
        )
        self.assertEqual(booking_response.status_code, 403)
        self.assertEqual(AccommodationBooking.objects.count(), before_count)

    def test_accommodation_owner_cannot_request_guest_recommendations_endpoint(self):
        self.client.force_login(self.owner_user)
        response = self.client.post(
            reverse("accommodation_recommend"),
            data={
                "location": "Bayawan",
                "budget": 2000,
                "guests": 1,
            },
        )
        self.assertEqual(response.status_code, 403)

    def test_accommodation_owner_cannot_cancel_guest_booking(self):
        check_in = timezone.now().date() + timedelta(days=6)
        check_out = check_in + timedelta(days=2)
        booking = AccommodationBooking.objects.create(
            guest=self.guest_user,
            accommodation=self.accepted_accom,
            room=self.accepted_room,
            check_in=check_in,
            check_out=check_out,
            num_guests=1,
            status="pending",
            total_amount=Decimal("3000.00"),
        )

        self.client.force_login(self.owner_user)
        response = self.client.post(
            reverse("cancel_my_accommodation_booking", args=[booking.booking_id]),
            data={},
        )
        self.assertEqual(response.status_code, 403)
        booking.refresh_from_db()
        self.assertEqual(booking.status, "pending")


class GuestOwnerRoutingUxTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.owner_without_accommodation = user_model.objects.create_user(
            username="owner_no_accommodation",
            email="owner_no_accommodation@example.com",
            password="secure-pass-123",
            first_name="Owner",
            last_name="NoAccommodation",
        )
        owner_group, _ = Group.objects.get_or_create(name="accommodation_owner")
        self.owner_without_accommodation.groups.add(owner_group)

        self.owner_with_accepted_accommodation = user_model.objects.create_user(
            username="owner_with_accommodation",
            email="owner_with_accommodation@example.com",
            password="secure-pass-123",
            first_name="Owner",
            last_name="WithAccommodation",
        )
        self.owner_with_accepted_accommodation.groups.add(owner_group)
        Accomodation.objects.create(
            owner=self.owner_with_accepted_accommodation,
            company_name="Owner Linked Hotel",
            email_address="owner-linked-hotel@example.com",
            location="Bayawan",
            company_type="hotel",
            password="accom-pass-123",
            phone_number="09991112222",
            approval_status="accepted",
            status="accepted",
        )

    def test_authenticated_owner_get_login_goes_to_admin_login(self):
        self.client.force_login(self.owner_without_accommodation)
        response = self.client.get(reverse("login"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("admin_app:login"))

    def test_owner_without_accepted_accommodation_is_not_auto_redirected_to_owner_hub(self):
        self.client.force_login(self.owner_without_accommodation)
        response = self.client.get(reverse("main-page"))
        self.assertEqual(response.status_code, 200)

    def test_owner_with_accepted_accommodation_is_redirected_to_owner_hub(self):
        self.client.force_login(self.owner_with_accepted_accommodation)
        response = self.client.get(reverse("main-page"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("admin_app:owner_hub"))


class AccommodationRoomAvailabilityLifecycleTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.guest_user = user_model.objects.create_user(
            username="lifecycle_guest_user",
            email="lifecycle_guest_user@example.com",
            password="secure-pass-123",
            first_name="Life",
            last_name="Cycle",
        )
        self.owner_user = user_model.objects.create_user(
            username="lifecycle_owner_user",
            email="lifecycle_owner_user@example.com",
            password="secure-pass-123",
            first_name="Life",
            last_name="Owner",
        )
        owner_group, _ = Group.objects.get_or_create(name="accommodation_owner")
        self.owner_user.groups.add(owner_group)

        self.accepted_accom = Accomodation.objects.create(
            company_name="Lifecycle Hotel",
            email_address="lifecycle-hotel@example.com",
            location="Bayawan",
            company_type="hotel",
            password="accom-pass-lifecycle",
            phone_number="09990000222",
            approval_status="accepted",
            status="accepted",
        )
        self.room = Room.objects.create(
            accommodation=self.accepted_accom,
            room_name="Lifecycle Room",
            person_limit=3,
            current_availability=3,
            price_per_night=Decimal("1800.00"),
            status="AVAILABLE",
        )

    def _set_admin_session(self):
        session = self.client.session
        session["user_type"] = "employee"
        session["is_admin"] = True
        session["employee_id"] = 1
        session.save()

    def test_pending_booking_creation_keeps_room_operationally_available(self):
        self.client.force_login(self.guest_user)
        today = timezone.localdate()
        response = self.client.post(
            reverse("accommodation_book"),
            data={
                "room_id": self.room.room_id,
                "check_in": today.isoformat(),
                "check_out": (today + timedelta(days=1)).isoformat(),
                "num_guests": 1,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.room.refresh_from_db()
        self.assertEqual(self.room.status, "AVAILABLE")
        self.assertEqual(self.room.current_availability, self.room.person_limit)

    def test_confirmed_booking_for_today_marks_current_availability_zero(self):
        today = timezone.localdate()
        booking = AccommodationBooking.objects.create(
            guest=self.guest_user,
            accommodation=self.accepted_accom,
            room=self.room,
            check_in=today,
            check_out=today + timedelta(days=1),
            num_guests=1,
            status="pending",
            total_amount=Decimal("1800.00"),
        )

        self.client.force_login(self.guest_user)
        self._set_admin_session()
        response = self.client.post(
            reverse("admin_app:accommodation_booking_update", args=[booking.booking_id]),
            data={"action": "confirm"},
        )
        self.assertEqual(response.status_code, 302)
        booking.refresh_from_db()
        self.room.refresh_from_db()
        self.assertEqual(booking.status, "confirmed")
        self.assertEqual(self.room.status, "AVAILABLE")
        self.assertEqual(self.room.current_availability, 0)

    def test_declined_booking_restores_room_current_availability(self):
        today = timezone.localdate()
        booking = AccommodationBooking.objects.create(
            guest=self.guest_user,
            accommodation=self.accepted_accom,
            room=self.room,
            check_in=today,
            check_out=today + timedelta(days=1),
            num_guests=1,
            status="confirmed",
            total_amount=Decimal("1800.00"),
        )
        self.room.current_availability = 0
        self.room.save(update_fields=["current_availability", "updated_at"])

        self.client.force_login(self.guest_user)
        self._set_admin_session()
        response = self.client.post(
            reverse("admin_app:accommodation_booking_update", args=[booking.booking_id]),
            data={"action": "decline"},
        )
        self.assertEqual(response.status_code, 302)
        booking.refresh_from_db()
        self.room.refresh_from_db()
        self.assertEqual(booking.status, "declined")
        self.assertEqual(self.room.current_availability, self.room.person_limit)

    def test_guest_cancellation_restores_room_current_availability(self):
        today = timezone.localdate()
        booking = AccommodationBooking.objects.create(
            guest=self.guest_user,
            accommodation=self.accepted_accom,
            room=self.room,
            check_in=today,
            check_out=today + timedelta(days=1),
            num_guests=1,
            status="confirmed",
            total_amount=Decimal("1800.00"),
        )
        self.room.current_availability = 0
        self.room.save(update_fields=["current_availability", "updated_at"])

        self.client.force_login(self.guest_user)
        response = self.client.post(
            reverse("cancel_my_accommodation_booking", args=[booking.booking_id]),
            data={"reason": "Change of plans"},
        )
        self.assertEqual(response.status_code, 302)
        booking.refresh_from_db()
        self.room.refresh_from_db()
        self.assertEqual(booking.status, "cancelled")
        self.assertEqual(self.room.current_availability, self.room.person_limit)


class AccommodationOwnerSignupFromGuestRegisterTests(TestCase):
    def test_ajax_signup_as_owner_assigns_group_and_redirect_url(self):
        image_bytes = (
            b"\x47\x49\x46\x38\x39\x61\x01\x00\x01\x00\x80\x00\x00"
            b"\x00\x00\x00\xff\xff\xff\x21\xf9\x04\x01\x00\x00\x00\x00"
            b"\x2c\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02\x44\x01"
            b"\x00\x3b"
        )
        response = self.client.post(
            reverse("register"),
            data={
                "first_name": "Owner",
                "middle_initial": "A",
                "last_name": "Signup",
                "age": "26",
                "country_of_origin": "Philippines",
                "city": "Bayawan",
                "phone_number": "09991112222",
                "email": "owner-signup-flow@example.com",
                "company_name": "Owner Demo Stay",
                "sex": "F",
                "password": "owner-pass-123",
                "confirm_password": "owner-pass-123",
                "register_as_accommodation_owner": "on",
                "owner_signup_intent": "1",
                "picture": SimpleUploadedFile("owner.gif", image_bytes, content_type="image/gif"),
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload.get("success"))
        self.assertEqual(payload.get("redirect_url"), reverse("admin_app:login"))

        user_model = get_user_model()
        owner_user = user_model.objects.get(email="owner-signup-flow@example.com")
        self.assertTrue(owner_user.groups.filter(name__iexact="accommodation_owner_pending").exists())

    def test_guest_signup_does_not_enter_owner_approval_without_owner_intent(self):
        image_bytes = (
            b"\x47\x49\x46\x38\x39\x61\x01\x00\x01\x00\x80\x00\x00"
            b"\x00\x00\x00\xff\xff\xff\x21\xf9\x04\x01\x00\x00\x00\x00"
            b"\x2c\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02\x44\x01"
            b"\x00\x3b"
        )
        response = self.client.post(
            reverse("register"),
            data={
                "first_name": "Guest",
                "middle_initial": "B",
                "last_name": "Signup",
                "age": "24",
                "country_of_origin": "Philippines",
                "city": "Bayawan",
                "phone_number": "09992223333",
                "email": "guest-signup-flow@example.com",
                "company_name": "",
                "sex": "F",
                "password": "guest-pass-123",
                "confirm_password": "guest-pass-123",
                "register_as_accommodation_owner": "on",
                "owner_signup_intent": "0",
                "picture": SimpleUploadedFile("guest.gif", image_bytes, content_type="image/gif"),
            },
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload.get("success"))
        self.assertEqual(payload.get("redirect_url"), "")

        user_model = get_user_model()
        guest_user = user_model.objects.get(email="guest-signup-flow@example.com")
        self.assertFalse(guest_user.groups.filter(name__iexact="accommodation_owner_pending").exists())
        self.assertFalse(guest_user.groups.filter(name__iexact="accommodation_owner").exists())


class PaymentWebhookCallbackTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.user = user_model.objects.create_user(
            username="payment_webhook_user",
            email="payment_webhook_user@example.com",
            password="secure-pass-123",
        )
        self.accommodation = Accomodation.objects.create(
            company_name="Webhook Hotel",
            email_address="webhook-hotel@example.com",
            location="Bayawan",
            company_type="hotel",
            password="accom-pass-webhook",
            phone_number="09990000999",
            approval_status="accepted",
            status="accepted",
        )
        self.room = Room.objects.create(
            accommodation=self.accommodation,
            room_name="Webhook Room",
            person_limit=2,
            current_availability=2,
            price_per_night=Decimal("1200.00"),
            status="AVAILABLE",
        )
        today = timezone.localdate()
        self.booking = AccommodationBooking.objects.create(
            guest=self.user,
            accommodation=self.accommodation,
            room=self.room,
            check_in=today + timedelta(days=2),
            check_out=today + timedelta(days=4),
            num_guests=2,
            status="pending",
            total_amount=Decimal("2400.00"),
            payment_status="unpaid",
            amount_paid=Decimal("0.00"),
        )
        self.billing = Billing.objects.create(
            booking=self.booking,
            booking_reference=f"AB-{self.booking.booking_id}",
            total_amount=self.booking.total_amount,
            payment_status="unpaid",
            amount_paid=Decimal("0.00"),
        )
        self.url = reverse("payment_webhook_callback")

    @override_settings(PAYMENT_WEBHOOK_SECRET="")
    def test_payment_webhook_returns_503_when_secret_not_configured(self):
        response = self.client.post(
            self.url,
            data=json.dumps({"booking_reference": self.billing.booking_reference, "payment_status": "paid"}),
            content_type="application/json",
            HTTP_X_PAYMENT_SIGNATURE="dummy",
        )
        self.assertEqual(response.status_code, 503)

    @override_settings(PAYMENT_WEBHOOK_SECRET="unit-test-secret")
    def test_payment_webhook_rejects_invalid_signature(self):
        response = self.client.post(
            self.url,
            data=json.dumps({"booking_reference": self.billing.booking_reference, "payment_status": "paid"}),
            content_type="application/json",
            HTTP_X_PAYMENT_SIGNATURE="invalid",
        )
        self.assertEqual(response.status_code, 403)

    @override_settings(PAYMENT_WEBHOOK_SECRET="unit-test-secret")
    def test_payment_webhook_updates_billing_and_booking_status(self):
        payload = {
            "booking_reference": self.billing.booking_reference,
            "payment_status": "paid",
            "amount_paid": "2400.00",
            "payment_method": "gcash",
        }
        raw = json.dumps(payload).encode("utf-8")
        signature = hmac.new(b"unit-test-secret", raw, hashlib.sha256).hexdigest()

        response = self.client.post(
            self.url,
            data=raw,
            content_type="application/json",
            HTTP_X_PAYMENT_SIGNATURE=signature,
        )
        self.assertEqual(response.status_code, 410)
        body = response.json()
        self.assertEqual(body.get("status"), "disabled")
        self.assertEqual(body.get("error"), "accommodation_transaction_disabled")

        self.booking.refresh_from_db()
        self.billing.refresh_from_db()
        self.assertEqual(self.booking.payment_status, "unpaid")
        self.assertEqual(self.billing.payment_status, "unpaid")


class GuestCurrentLocationApiTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.user = user_model.objects.create_user(
            username="guest_geo_user",
            email="guest_geo_user@example.com",
            password="secure-pass-123",
            first_name="Geo",
            last_name="Guest",
        )
        self.client.force_login(self.user)
        self.url = reverse("current_location_api")

    def test_post_and_get_current_location(self):
        post_response = self.client.post(
            self.url,
            data='{"latitude":9.3679,"longitude":122.8071,"accuracy":18.2}',
            content_type="application/json",
        )
        self.assertEqual(post_response.status_code, 200)
        self.assertTrue(post_response.json().get("success"))

        get_response = self.client.get(self.url)
        self.assertEqual(get_response.status_code, 200)
        body = get_response.json()
        self.assertTrue(body.get("success"))
        location = body.get("location") or {}
        self.assertAlmostEqual(float(location.get("latitude")), 9.3679, places=3)
        self.assertAlmostEqual(float(location.get("longitude")), 122.8071, places=3)
