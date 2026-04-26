from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase
from django.urls import reverse

from admin_app.models import Accomodation, TourismInformation, Room, Employee, InAppNotification
from guest_app.models import AccommodationBooking


class AccommodationRegistrationRBACTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.user = user_model.objects.create_user(
            username="normal_guest",
            email="normal_guest@example.com",
            password="secure-pass-123",
            first_name="Normal",
            last_name="Guest",
        )
        self.owner_user = user_model.objects.create_user(
            username="accom_owner_user",
            email="accom_owner@example.com",
            password="secure-pass-456",
            first_name="Accom",
            last_name="Owner",
        )
        owner_group, _ = Group.objects.get_or_create(name="accommodation_owner")
        self.owner_user.groups.add(owner_group)
        self.url = reverse("admin_app:accommodation_register")

    def _payload(self, *, suffix):
        return {
            "company_name": f"Bayawan Test Stay {suffix}",
            "company_type": "Hotel",
            "location": "Bayawan City",
            "phone_number": "09990000000",
            "email_address": f"accom-{suffix}@example.com",
            "description": "A database-driven registration test record.",
            "accommodation_amenities": "WiFi, Parking",
            "password": "accom-pass-123",
            "password_confirm": "accom-pass-123",
        }

    def test_non_owner_cannot_access_registration_endpoint(self):
        self.client.force_login(self.user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("admin_app:login"), response.url)

    def test_non_owner_cannot_submit_registration(self):
        self.client.force_login(self.user)
        response = self.client.post(self.url, data=self._payload(suffix="non-owner"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("admin_app:login"), response.url)
        self.assertFalse(Accomodation.objects.filter(email_address="accom-non-owner@example.com").exists())

    def test_accommodation_owner_can_submit_registration_and_is_pending(self):
        self.client.force_login(self.owner_user)
        response = self.client.post(self.url, data=self._payload(suffix="owner"))
        self.assertEqual(response.status_code, 302)

        accom = Accomodation.objects.get(email_address="accom-owner@example.com")
        self.assertEqual(accom.owner_id, self.owner_user.pk)
        self.assertEqual(accom.approval_status, "pending")

    def test_registration_rejects_out_of_scope_company_type(self):
        self.client.force_login(self.owner_user)
        payload = self._payload(suffix="resort")
        payload["company_type"] = "Resort"
        response = self.client.post(self.url, data=payload)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Company type must be Hotel or Inn.")
        self.assertFalse(Accomodation.objects.filter(email_address=payload["email_address"]).exists())

    def test_registration_normalizes_canonical_location_alias(self):
        self.client.force_login(self.owner_user)
        payload = self._payload(suffix="locnorm")
        payload["location"] = "poblacion"
        response = self.client.post(self.url, data=payload)
        self.assertEqual(response.status_code, 302)
        accom = Accomodation.objects.get(email_address=payload["email_address"])
        self.assertEqual(accom.location, "Poblacion, Bayawan City")

    def test_registration_rejects_non_bayawan_location(self):
        self.client.force_login(self.owner_user)
        payload = self._payload(suffix="outside")
        payload["location"] = "Dumaguete City"
        response = self.client.post(self.url, data=payload)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Location must be within Bayawan City scope.")
        self.assertFalse(Accomodation.objects.filter(email_address=payload["email_address"]).exists())


class AccommodationDashboardTemplateRouteTests(TestCase):
    def test_dashboard_renders_without_stale_url_reverse_errors(self):
        user_model = get_user_model()
        owner_user = user_model.objects.create_user(
            username="owner_dashboard_user",
            email="owner_dashboard@example.com",
            password="secure-pass-789",
            first_name="Owner",
            last_name="Dashboard",
        )
        owner_group, _ = Group.objects.get_or_create(name="accommodation_owner")
        owner_user.groups.add(owner_group)
        accom = Accomodation.objects.create(
            owner=owner_user,
            company_name="Dashboard Stay",
            email_address="dashboard-stay@example.com",
            location="Bayawan",
            company_type="hotel",
            description="Template route regression test",
            password="accom-pass-123",
            phone_number="09995550123",
            approval_status="accepted",
            status="accepted",
        )
        self.client.force_login(owner_user)

        response = self.client.get(reverse("admin_app:accommodation_dashboard"))
        self.assertEqual(response.status_code, 200)


class OwnerRoomBookingsJsonTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        owner_group, _ = Group.objects.get_or_create(name="accommodation_owner")

        self.owner_user = user_model.objects.create_user(
            username="owner_room_json_user",
            email="owner_room_json@example.com",
            password="secure-pass-789",
            first_name="Owner",
            last_name="Json",
        )
        self.owner_user.groups.add(owner_group)

        self.accom = Accomodation.objects.create(
            owner=self.owner_user,
            company_name="JSON Stay",
            email_address="json-stay@example.com",
            location="Bayawan",
            company_type="hotel",
            description="Owner room booking JSON test",
            password="accom-pass-123",
            phone_number="09995550199",
            approval_status="accepted",
            status="accepted",
        )
        self.room = Room.objects.create(
            accommodation=self.accom,
            room_name="Executive Twin",
            person_limit=5,
            current_availability=5,
            price_per_night="3400.00",
            status="AVAILABLE",
        )
        self.guest_user = user_model.objects.create_user(
            username="owner_room_guest",
            email="owner_room_guest@example.com",
            password="secure-pass-111",
            first_name="Jade",
            last_name="Guest",
        )

    def test_owner_room_bookings_json_returns_room_scoped_non_cancelled_rows(self):
        AccommodationBooking.objects.create(
            guest=self.guest_user,
            accommodation=self.accom,
            room=self.room,
            check_in=date(2026, 5, 10),
            check_out=date(2026, 5, 13),
            num_guests=2,
            status="confirmed",
            total_amount="6800.00",
        )
        AccommodationBooking.objects.create(
            guest=self.guest_user,
            accommodation=self.accom,
            room=self.room,
            check_in=date(2026, 5, 20),
            check_out=date(2026, 5, 22),
            num_guests=1,
            status="cancelled",
            total_amount="3400.00",
        )

        self.client.force_login(self.owner_user)
        response = self.client.get(
            reverse("admin_app:owner_room_bookings_json", kwargs={"room_id": self.room.room_id})
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload.get("status"), "success")
        self.assertEqual(payload.get("count"), 1)
        self.assertEqual(payload["guests"][0]["first_name"], "Jade")
        self.assertEqual(payload["guests"][0]["status_raw"], "confirmed")

    def test_owner_room_bookings_json_rejects_room_from_other_owner(self):
        user_model = get_user_model()
        owner_group = Group.objects.get(name="accommodation_owner")
        other_owner = user_model.objects.create_user(
            username="other_owner_room_json_user",
            email="other_owner_room_json@example.com",
            password="secure-pass-222",
            first_name="Other",
            last_name="Owner",
        )
        other_owner.groups.add(owner_group)

        other_accom = Accomodation.objects.create(
            owner=other_owner,
            company_name="Other Stay",
            email_address="other-stay@example.com",
            location="Bayawan",
            company_type="hotel",
            description="Other room scope",
            password="accom-pass-222",
            phone_number="09995550200",
            approval_status="accepted",
            status="accepted",
        )
        other_room = Room.objects.create(
            accommodation=other_accom,
            room_name="Other Room",
            person_limit=2,
            current_availability=2,
            price_per_night="1200.00",
            status="AVAILABLE",
        )

        self.client.force_login(self.owner_user)
        response = self.client.get(
            reverse("admin_app:owner_room_bookings_json", kwargs={"room_id": other_room.room_id})
        )
        self.assertEqual(response.status_code, 404)


class OwnerRoomBookingsCheckInTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        owner_group, _ = Group.objects.get_or_create(name="accommodation_owner")

        self.owner_user = user_model.objects.create_user(
            username="owner_room_checkin_user",
            email="owner_room_checkin@example.com",
            password="secure-pass-789",
            first_name="Owner",
            last_name="CheckIn",
        )
        self.owner_user.groups.add(owner_group)

        self.accom = Accomodation.objects.create(
            owner=self.owner_user,
            company_name="Checkin Stay",
            email_address="checkin-stay@example.com",
            location="Bayawan",
            company_type="hotel",
            description="Owner room booking check-in test",
            password="accom-pass-123",
            phone_number="09995550333",
            approval_status="accepted",
            status="accepted",
        )
        self.room = Room.objects.create(
            accommodation=self.accom,
            room_name="Business Single",
            person_limit=2,
            current_availability=2,
            price_per_night="1200.00",
            status="AVAILABLE",
        )
        self.guest_user = user_model.objects.create_user(
            username="owner_room_checkin_guest",
            email="owner_room_checkin_guest@example.com",
            password="secure-pass-111",
            first_name="Cyril",
            last_name="Guest",
        )

    def test_owner_room_check_in_accepts_confirmed_booking_for_today(self):
        today = date.today()
        booking = AccommodationBooking.objects.create(
            guest=self.guest_user,
            accommodation=self.accom,
            room=self.room,
            check_in=today,
            check_out=today + timedelta(days=1),
            num_guests=1,
            status="confirmed",
            total_amount="1200.00",
        )

        self.client.force_login(self.owner_user)
        response = self.client.post(
            reverse("admin_app:owner_room_bookings_check_in"),
            data={"room_id": self.room.room_id, "booking_ids": [booking.booking_id]},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload.get("status"), "success")
        self.assertEqual(payload.get("checked_in_count"), 1)
        self.assertIn(booking.booking_id, payload.get("checked_in_ids", []))

    def test_owner_room_check_in_rejects_future_checkin_date(self):
        today = date.today()
        booking = AccommodationBooking.objects.create(
            guest=self.guest_user,
            accommodation=self.accom,
            room=self.room,
            check_in=today + timedelta(days=10),
            check_out=today + timedelta(days=12),
            num_guests=1,
            status="confirmed",
            total_amount="2400.00",
        )

        self.client.force_login(self.owner_user)
        response = self.client.post(
            reverse("admin_app:owner_room_bookings_check_in"),
            data={"room_id": self.room.room_id, "booking_ids": [booking.booking_id]},
        )
        self.assertEqual(response.status_code, 400)
        payload = response.json()
        self.assertEqual(payload.get("status"), "error")
        self.assertEqual(payload.get("checked_in_count"), 0)


class OwnerAccommodationBookingEditTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        owner_group, _ = Group.objects.get_or_create(name="accommodation_owner")

        self.owner_user = user_model.objects.create_user(
            username="owner_booking_edit_user",
            email="owner_booking_edit@example.com",
            password="secure-pass-123",
            first_name="Owner",
            last_name="Edit",
        )
        self.owner_user.groups.add(owner_group)

        self.accom = Accomodation.objects.create(
            owner=self.owner_user,
            company_name="Edit Stay",
            email_address="edit-stay@example.com",
            location="Bayawan",
            company_type="hotel",
            description="Owner booking edit test",
            password="accom-pass-123",
            phone_number="09995550777",
            approval_status="accepted",
            status="accepted",
        )
        self.room = Room.objects.create(
            accommodation=self.accom,
            room_name="Executive Twin",
            person_limit=5,
            current_availability=5,
            price_per_night="3400.00",
            status="AVAILABLE",
        )
        self.guest_user = user_model.objects.create_user(
            username="owner_booking_edit_guest",
            email="owner_booking_edit_guest@example.com",
            password="secure-pass-111",
            first_name="Jade",
            last_name="Guest",
        )
        self.booking = AccommodationBooking.objects.create(
            guest=self.guest_user,
            accommodation=self.accom,
            room=self.room,
            check_in=date(2026, 5, 10),
            check_out=date(2026, 5, 13),
            num_guests=1,
            status="pending",
            total_amount="3400.00",
        )

    def test_edit_action_updates_details_without_auto_confirm(self):
        self.client.force_login(self.owner_user)
        response = self.client.post(
            reverse("admin_app:owner_accommodation_booking_update", kwargs={"booking_id": self.booking.booking_id}),
            data={
                "action": "edit",
                "check_in": "2026-05-11",
                "check_out": "2026-05-14",
                "num_guests": "2",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.booking.refresh_from_db()
        self.assertEqual(str(self.booking.check_in), "2026-05-11")
        self.assertEqual(str(self.booking.check_out), "2026-05-14")
        self.assertEqual(self.booking.num_guests, 2)
        self.assertEqual(self.booking.status, "pending")


class AccommodationRegisterOwnerApprovalRequiredTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.user = user_model.objects.create_user(
            username="upgrade_guest_user",
            email="upgrade_guest_user@example.com",
            password="secure-pass-123",
            first_name="Upgrade",
            last_name="Guest",
        )
        self.url = reverse("admin_app:accommodation_register")

    def test_authenticated_guest_is_redirected_to_admin_login_for_owner_flow(self):
        self.client.force_login(self.user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("admin_app:login"), response.url)
        self.user.refresh_from_db()
        self.assertFalse(self.user.groups.filter(name__iexact="accommodation_owner").exists())


class AccommodationOwnerApprovalDashboardTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.owner_candidate = user_model.objects.create_user(
            username="pending_owner_candidate",
            email="pending_owner_candidate@example.com",
            password="secure-pass-123",
            first_name="Pending",
            last_name="Owner",
        )
        pending_group, _ = Group.objects.get_or_create(name="accommodation_owner_pending")
        self.owner_candidate.groups.add(pending_group)

        session = self.client.session
        session["user_type"] = "employee"
        session["is_admin"] = True
        session["employee_id"] = 1
        session.save()

    def test_pending_owner_page_lists_owner_candidates(self):
        response = self.client.get(reverse("admin_app:pending_accommodation_owners"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "pending_owner_candidate@example.com")

    def test_admin_can_accept_pending_owner(self):
        response = self.client.post(
            reverse("admin_app:accommodation_owner_update", kwargs={"user_id": self.owner_candidate.pk}),
            data={"action": "accept"},
        )
        self.assertEqual(response.status_code, 302)
        self.owner_candidate.refresh_from_db()
        self.assertTrue(self.owner_candidate.groups.filter(name__iexact="accommodation_owner").exists())
        self.assertFalse(self.owner_candidate.groups.filter(name__iexact="accommodation_owner_pending").exists())


class AccommodationOwnerLoginFlowTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.pending_owner = user_model.objects.create_user(
            username="owner_pending_login",
            email="owner_pending_login@example.com",
            password="secure-pass-123",
            first_name="Pending",
            last_name="Owner",
        )
        self.approved_owner = user_model.objects.create_user(
            username="owner_approved_login",
            email="owner_approved_login@example.com",
            password="secure-pass-456",
            first_name="Approved",
            last_name="Owner",
        )
        self.approved_owner_no_accommodation = user_model.objects.create_user(
            username="owner_approved_no_accom",
            email="owner_approved_no_accom@example.com",
            password="secure-pass-789",
            first_name="Approved",
            last_name="NoAccom",
        )

        pending_group, _ = Group.objects.get_or_create(name="accommodation_owner_pending")
        approved_group, _ = Group.objects.get_or_create(name="accommodation_owner")

        self.pending_owner.groups.add(pending_group)
        self.approved_owner.groups.add(approved_group)
        self.approved_owner_no_accommodation.groups.add(approved_group)

        Accomodation.objects.create(
            owner=self.approved_owner,
            company_name="Approved Owner Stay",
            email_address="approved-owner-stay@example.com",
            location="Bayawan",
            company_type="Hotel",
            description="Owner login regression test",
            password="accom-pass-123",
            phone_number="09995550111",
            approval_status="accepted",
            status="accepted",
        )

    def test_pending_owner_username_login_shows_pending_message(self):
        response = self.client.post(
            reverse("admin_app:login"),
            data={"username": self.pending_owner.username, "password": "secure-pass-123"},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "pending admin approval")

    def test_approved_owner_can_login_using_username(self):
        response = self.client.post(
            reverse("admin_app:login"),
            data={"username": self.approved_owner.username, "password": "secure-pass-456"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("admin_app:accommodation_dashboard"))

    def test_approved_owner_without_accommodation_can_login_and_land_on_owner_hub(self):
        response = self.client.post(
            reverse("admin_app:login"),
            data={"username": self.approved_owner_no_accommodation.username, "password": "secure-pass-789"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("admin_app:owner_hub"))


class OwnerDashboardEntryRoutingTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        approved_group, _ = Group.objects.get_or_create(name="accommodation_owner")

        self.owner_without_accommodation = user_model.objects.create_user(
            username="owner_dashboard_no_accom",
            email="owner_dashboard_no_accom@example.com",
            password="secure-pass-123",
            first_name="Owner",
            last_name="NoAccom",
        )
        self.owner_without_accommodation.groups.add(approved_group)

        self.owner_with_accommodation = user_model.objects.create_user(
            username="owner_dashboard_with_accom",
            email="owner_dashboard_with_accom@example.com",
            password="secure-pass-456",
            first_name="Owner",
            last_name="WithAccom",
        )
        self.owner_with_accommodation.groups.add(approved_group)

        Accomodation.objects.create(
            owner=self.owner_with_accommodation,
            company_name="Dashboard Entry Hotel",
            email_address="dashboard-entry-hotel@example.com",
            location="Bayawan",
            company_type="Hotel",
            description="Owner dashboard entry route test",
            password="accom-pass-123",
            phone_number="09995550999",
            approval_status="accepted",
            status="accepted",
        )

    def test_owner_dashboard_entry_redirects_to_owner_hub_when_no_accepted_accommodation(self):
        self.client.force_login(self.owner_without_accommodation)
        response = self.client.get(reverse("admin_app:owner_dashboard_entry"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("admin_app:owner_hub"))

    def test_owner_dashboard_entry_redirects_to_accommodation_dashboard_when_accepted_accommodation_exists(self):
        self.client.force_login(self.owner_with_accommodation)
        response = self.client.get(reverse("admin_app:owner_dashboard_entry"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("admin_app:accommodation_dashboard"))


class AccommodationDashboardGuardTests(TestCase):
    def test_owner_without_accepted_accommodation_redirects_to_owner_hub(self):
        user_model = get_user_model()
        owner_user = user_model.objects.create_user(
            username="owner_dashboard_guard_user",
            email="owner_dashboard_guard@example.com",
            password="secure-pass-123",
            first_name="Owner",
            last_name="Guard",
        )
        owner_group, _ = Group.objects.get_or_create(name="accommodation_owner")
        owner_user.groups.add(owner_group)

        self.client.force_login(owner_user)
        response = self.client.get(reverse("admin_app:accommodation_dashboard"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("admin_app:owner_hub"))


class TourismInformationModelTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.user = user_model.objects.create_user(
            username="tourism_admin_seed",
            email="tourism_admin_seed@example.com",
            password="secure-tourism-pass-123",
            first_name="Tourism",
            last_name="Admin",
        )

    def test_defaults_to_draft_and_active(self):
        row = TourismInformation.objects.create(
            spot_name="Danjugan Falls",
            description="Scenic falls in Bayawan area.",
            location="Bayawan City",
            created_by=self.user,
            updated_by=self.user,
        )

        self.assertEqual(row.publication_status, "draft")
        self.assertTrue(row.is_active)
        self.assertFalse(row.is_published)

    def test_published_queryset_only_returns_active_published(self):
        TourismInformation.objects.create(
            spot_name="Published Spot",
            publication_status="published",
            is_active=True,
            created_by=self.user,
            updated_by=self.user,
        )
        TourismInformation.objects.create(
            spot_name="Archived Spot",
            publication_status="archived",
            is_active=False,
            created_by=self.user,
            updated_by=self.user,
        )
        TourismInformation.objects.create(
            spot_name="Unpublished Spot",
            publication_status="draft",
            is_active=True,
            created_by=self.user,
            updated_by=self.user,
        )

        rows = TourismInformation.objects.published()
        self.assertEqual(rows.count(), 1)
        self.assertEqual(rows.first().spot_name, "Published Spot")


class TourismInformationAdminAccessTests(TestCase):
    def _set_admin_session(self):
        session = self.client.session
        session["user_type"] = "employee"
        session["is_admin"] = True
        session["employee_id"] = 1
        session.save()

    def _set_non_admin_session(self):
        session = self.client.session
        session["user_type"] = "employee"
        session["is_admin"] = False
        session["employee_id"] = 2
        session.save()

    def test_non_admin_cannot_access_tourism_information_manage(self):
        self._set_non_admin_session()
        response = self.client.get(reverse("admin_app:tourism_information_manage"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("admin_app:login"), response.url)

    def test_admin_can_create_publish_archive_tourism_information(self):
        self._set_admin_session()

        create_response = self.client.post(
            reverse("admin_app:tourism_information_create"),
            data={
                "spot_name": "Bayawan Heritage Park",
                "description": "A local cultural and historical attraction.",
                "location": "Bayawan City Proper",
                "contact_information": "09171234567",
                "operating_hours": "08:00 AM - 05:00 PM",
                "publication_status": "draft",
                "is_active": "on",
            },
        )
        self.assertEqual(create_response.status_code, 302)

        row = TourismInformation.objects.get(spot_name="Bayawan Heritage Park")
        self.assertEqual(row.publication_status, "draft")
        self.assertTrue(row.is_active)

        publish_response = self.client.post(
            reverse("admin_app:tourism_information_publish", kwargs={"tourism_info_id": row.tourism_info_id})
        )
        self.assertEqual(publish_response.status_code, 302)
        row.refresh_from_db()
        self.assertEqual(row.publication_status, "published")
        self.assertTrue(row.is_active)

        archive_response = self.client.post(
            reverse("admin_app:tourism_information_archive", kwargs={"tourism_info_id": row.tourism_info_id})
        )
        self.assertEqual(archive_response.status_code, 302)
        row.refresh_from_db()
        self.assertEqual(row.publication_status, "archived")
        self.assertFalse(row.is_active)


class AdminAccommodationBookingApprovalModeTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.owner_user = user_model.objects.create_user(
            username="admin_mode_owner",
            email="admin_mode_owner@example.com",
            password="secure-pass-123",
            first_name="AdminMode",
            last_name="Owner",
        )
        self.guest_user = user_model.objects.create_user(
            username="admin_mode_guest",
            email="admin_mode_guest@example.com",
            password="secure-pass-456",
            first_name="AdminMode",
            last_name="Guest",
        )
        self.accom = Accomodation.objects.create(
            owner=self.owner_user,
            company_name="Admin Monitoring Stay",
            email_address="admin-monitoring-stay@example.com",
            location="Bayawan",
            company_type="Hotel",
            description="Admin booking mode regression test",
            password="accom-pass-123",
            phone_number="09995551001",
            approval_status="accepted",
            status="accepted",
            is_active=True,
        )
        self.room = Room.objects.create(
            accommodation=self.accom,
            room_name="Admin Mode Room",
            person_limit=2,
            current_availability=2,
            price_per_night="1500.00",
            status="AVAILABLE",
        )

        self.booking_confirm = AccommodationBooking.objects.create(
            guest=self.guest_user,
            accommodation=self.accom,
            room=self.room,
            check_in=date(2026, 5, 10),
            check_out=date(2026, 5, 12),
            num_guests=2,
            status="pending",
            total_amount="3000.00",
        )
        self.booking_decline = AccommodationBooking.objects.create(
            guest=self.guest_user,
            accommodation=self.accom,
            room=self.room,
            check_in=date(2026, 5, 14),
            check_out=date(2026, 5, 16),
            num_guests=1,
            status="pending",
            total_amount="1500.00",
        )

        session = self.client.session
        session["user_type"] = "employee"
        session["is_admin"] = True
        session["employee_id"] = 1
        session.save()

    def test_admin_cannot_confirm_or_decline_in_monitoring_mode(self):
        confirm_response = self.client.post(
            reverse("admin_app:accommodation_booking_update", kwargs={"booking_id": self.booking_confirm.booking_id}),
            data={"action": "confirm"},
        )
        self.assertEqual(confirm_response.status_code, 302)
        self.booking_confirm.refresh_from_db()
        self.assertEqual(self.booking_confirm.status, "pending")

        decline_response = self.client.post(
            reverse("admin_app:accommodation_booking_update", kwargs={"booking_id": self.booking_decline.booking_id}),
            data={"action": "decline"},
        )
        self.assertEqual(decline_response.status_code, 302)
        self.booking_decline.refresh_from_db()
        self.assertEqual(self.booking_decline.status, "pending")

    def test_admin_can_confirm_or_decline_only_in_override_mode(self):
        confirm_response = self.client.post(
            reverse("admin_app:accommodation_booking_update", kwargs={"booking_id": self.booking_confirm.booking_id}),
            data={"action": "confirm", "allow_override": "1"},
        )
        self.assertEqual(confirm_response.status_code, 302)
        self.booking_confirm.refresh_from_db()
        self.assertEqual(self.booking_confirm.status, "confirmed")

        decline_response = self.client.post(
            reverse("admin_app:accommodation_booking_update", kwargs={"booking_id": self.booking_decline.booking_id}),
            data={"action": "decline", "allow_override": "1"},
        )
        self.assertEqual(decline_response.status_code, 302)
        self.booking_decline.refresh_from_db()
        self.assertEqual(self.booking_decline.status, "declined")


class OwnerAccommodationOverlapApprovalTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        owner_group, _ = Group.objects.get_or_create(name="accommodation_owner")
        self.owner_user = user_model.objects.create_user(
            username="owner_overlap_user",
            email="owner_overlap_user@example.com",
            password="secure-pass-123",
            first_name="Owner",
            last_name="Overlap",
        )
        self.owner_user.groups.add(owner_group)

        self.guest_user = user_model.objects.create_user(
            username="owner_overlap_guest",
            email="owner_overlap_guest@example.com",
            password="secure-pass-456",
            first_name="Overlap",
            last_name="Guest",
        )

        self.accom = Accomodation.objects.create(
            owner=self.owner_user,
            company_name="Overlap Stay",
            email_address="overlap-stay@example.com",
            location="Bayawan",
            company_type="Hotel",
            description="Overlap acceptance regression test",
            password="accom-pass-123",
            phone_number="09995551002",
            approval_status="accepted",
            status="accepted",
            is_active=True,
        )
        self.room = Room.objects.create(
            accommodation=self.accom,
            room_name="Overlap Room",
            person_limit=2,
            current_availability=2,
            price_per_night="1600.00",
            status="AVAILABLE",
        )

        AccommodationBooking.objects.create(
            guest=self.guest_user,
            accommodation=self.accom,
            room=self.room,
            check_in=date(2026, 5, 10),
            check_out=date(2026, 5, 12),
            num_guests=2,
            status="confirmed",
            total_amount="3200.00",
        )
        self.pending_overlap = AccommodationBooking.objects.create(
            guest=self.guest_user,
            accommodation=self.accom,
            room=self.room,
            check_in=date(2026, 5, 11),
            check_out=date(2026, 5, 13),
            num_guests=2,
            status="pending",
            total_amount="3200.00",
        )
        self.pending_valid = AccommodationBooking.objects.create(
            guest=self.guest_user,
            accommodation=self.accom,
            room=self.room,
            check_in=date(2026, 5, 13),
            check_out=date(2026, 5, 15),
            num_guests=2,
            status="pending",
            total_amount="3200.00",
        )

    def test_owner_cannot_accept_pending_booking_with_overlapping_confirmed_booking(self):
        self.client.force_login(self.owner_user)
        response = self.client.post(
            reverse("admin_app:owner_accommodation_booking_update", kwargs={"booking_id": self.pending_overlap.booking_id}),
            data={"action": "confirm"},
        )
        self.assertEqual(response.status_code, 302)
        self.pending_overlap.refresh_from_db()
        self.assertEqual(self.pending_overlap.status, "pending")

    def test_owner_can_accept_valid_non_overlapping_pending_booking(self):
        self.client.force_login(self.owner_user)
        response = self.client.post(
            reverse("admin_app:owner_accommodation_booking_update", kwargs={"booking_id": self.pending_valid.booking_id}),
            data={"action": "confirm"},
        )
        self.assertEqual(response.status_code, 302)
        self.pending_valid.refresh_from_db()
        self.assertEqual(self.pending_valid.status, "confirmed")


class InAppNotificationVisibilityTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.guest_one = user_model.objects.create_user(
            username="notif_guest_one",
            email="notif_guest_one@example.com",
            password="secure-pass-123",
            first_name="Notif",
            last_name="One",
        )
        self.guest_two = user_model.objects.create_user(
            username="notif_guest_two",
            email="notif_guest_two@example.com",
            password="secure-pass-123",
            first_name="Notif",
            last_name="Two",
        )
        self.employee = Employee.objects.create(
            first_name="Notif",
            last_name="Employee",
            username="notif_employee_user",
            age=30,
            phone_number="09995551020",
            email="notif_employee@example.com",
            sex="F",
            status="accepted",
            role="Employee",
        )

        self.guest_notification = InAppNotification.objects.create(
            recipient_guest=self.guest_one,
            title="Guest Notice",
            message="Guest-only message",
            notification_type="system",
            url="/guest_app/main-page/",
        )
        InAppNotification.objects.create(
            recipient_guest=self.guest_two,
            title="Other Guest Notice",
            message="Should not be visible",
            notification_type="system",
        )
        self.employee_notification = InAppNotification.objects.create(
            recipient_employee=self.employee,
            title="Employee Notice",
            message="Employee-only message",
            notification_type="assignment",
            url="/tour_app/pending/",
        )

    def test_guest_feed_shows_only_guest_notifications(self):
        self.client.force_login(self.guest_one)
        response = self.client.get(reverse("admin_app:notifications_feed"))
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload.get("success"))
        ids = [row.get("id") for row in payload.get("notifications", [])]
        self.assertIn(self.guest_notification.id, ids)
        self.assertNotIn(self.employee_notification.id, ids)

    def test_employee_feed_shows_only_employee_notifications(self):
        session = self.client.session
        session["user_type"] = "employee"
        session["employee_id"] = self.employee.emp_id
        session["is_admin"] = False
        session.save()
        response = self.client.get(reverse("admin_app:notifications_feed"))
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        ids = [row.get("id") for row in payload.get("notifications", [])]
        self.assertIn(self.employee_notification.id, ids)
        self.assertNotIn(self.guest_notification.id, ids)

    def test_notification_open_marks_item_as_read(self):
        self.client.force_login(self.guest_one)
        open_url = reverse("admin_app:notification_open", kwargs={"notification_id": self.guest_notification.id})
        response = self.client.get(open_url)
        self.assertEqual(response.status_code, 302)
        self.guest_notification.refresh_from_db()
        self.assertTrue(self.guest_notification.is_read)


class OwnerAccommodationEditTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        owner_group, _ = Group.objects.get_or_create(name="accommodation_owner")

        self.owner_user = user_model.objects.create_user(
            username="owner_edit_user",
            email="owner_edit@example.com",
            password="secure-pass-123",
            first_name="Owner",
            last_name="Edit",
        )
        self.owner_user.groups.add(owner_group)

        self.other_owner = user_model.objects.create_user(
            username="owner_edit_other_user",
            email="owner_edit_other@example.com",
            password="secure-pass-123",
            first_name="Other",
            last_name="Owner",
        )
        self.other_owner.groups.add(owner_group)

        self.accom = Accomodation.objects.create(
            owner=self.owner_user,
            company_name="Baywalk Breeze Resort",
            email_address="baywalk-breeze@example.com",
            location="Bayawan City",
            company_type="Hotel",
            description="Original description",
            accommodation_amenities="WiFi, Parking",
            password="accom-pass-123",
            phone_number="09995551234",
            approval_status="accepted",
            status="accepted",
        )

    def test_owner_can_edit_own_accommodation_company_name(self):
        self.client.force_login(self.owner_user)
        edit_url = reverse("admin_app:owner_edit_accommodation", kwargs={"accom_id": self.accom.accom_id})
        response = self.client.post(
            edit_url,
            data={
                "company_name": "Baywalk Breeze Resort Updated",
                "company_type": "Hotel",
                "location": "Bayawan City",
                "phone_number": "09995551234",
                "email_address": "baywalk-breeze@example.com",
                "description": "Updated description",
                "accommodation_amenities": "WiFi, Parking, Breakfast",
                "official_booking_url": "",
                "official_contact_url": "",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("admin_app:owner_hub"))
        self.accom.refresh_from_db()
        self.assertEqual(self.accom.company_name, "Baywalk Breeze Resort Updated")

    def test_owner_cannot_edit_other_owners_accommodation(self):
        other_accom = Accomodation.objects.create(
            owner=self.other_owner,
            company_name="Other Owner Stay",
            email_address="other-owner-stay@example.com",
            location="Bayawan City",
            company_type="Inn",
            description="Other owner's accommodation",
            accommodation_amenities="WiFi",
            password="accom-pass-321",
            phone_number="09995550999",
            approval_status="accepted",
            status="accepted",
        )
        self.client.force_login(self.owner_user)
        edit_url = reverse("admin_app:owner_edit_accommodation", kwargs={"accom_id": other_accom.accom_id})
        response = self.client.get(edit_url)
        self.assertEqual(response.status_code, 404)
