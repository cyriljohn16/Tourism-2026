from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase
from django.urls import reverse

from admin_app.models import Accomodation, Room as AdminRoom, RoomAssignment as AdminRoomAssignment
from accom_app.models import Room as LegacyRoom, RoomsGuestAdd, AuthoritativeRoomDetails
from guest_app.models import AccommodationBooking, Guest


class RoomManagementAuthoritativeModelTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        owner_group, _ = Group.objects.get_or_create(name="accommodation_owner")

        self.owner = user_model.objects.create_user(
            username="room_owner",
            email="room_owner@example.com",
            password="secure-pass-123",
            first_name="Room",
            last_name="Owner",
        )
        self.owner.groups.add(owner_group)

        self.other_owner = user_model.objects.create_user(
            username="other_room_owner",
            email="other_room_owner@example.com",
            password="secure-pass-123",
            first_name="Other",
            last_name="Owner",
        )
        self.other_owner.groups.add(owner_group)

        self.accommodation = Accomodation.objects.create(
            owner=self.owner,
            company_name="Owner Hotel",
            email_address="owner-hotel@example.com",
            location="Bayawan",
            company_type="hotel",
            password="accom-pass-owner",
            phone_number="09990001000",
            approval_status="accepted",
            status="accepted",
        )

    def _set_room_session(self, *, accom_id, user_type="accomodation"):
        session = self.client.session
        session["accom_id"] = accom_id
        session["user_type"] = user_type
        session.save()

    def test_add_room_ajax_writes_to_admin_room_only(self):
        self.client.force_login(self.owner)
        self._set_room_session(accom_id=self.accommodation.accom_id)

        response = self.client.post(
            reverse("accom_app:add_room_ajax"),
            data={
                "room_name": "Deluxe Queen",
                "room_type": "Deluxe Queen",
                "person_limit": 3,
                "price_per_night": "2200.50",
                "availability_status": "available",
                "amenities": '["wifi", "aircon"]',
            },
        )

        self.assertEqual(response.status_code, 200)
        created_room = AdminRoom.objects.get(accommodation=self.accommodation, room_name="Deluxe Queen")
        self.assertEqual(created_room.person_limit, 3)
        self.assertEqual(str(created_room.price_per_night), "2200.50")
        self.assertEqual(created_room.status, "AVAILABLE")
        details = AuthoritativeRoomDetails.objects.get(room=created_room)
        self.assertEqual(details.room_type, "Deluxe Queen")
        self.assertIn("wifi", details.amenities.lower())
        self.assertFalse(
            LegacyRoom.objects.filter(
                accom_id=self.accommodation,
                room_name="Deluxe Queen",
            ).exists()
        )

    def test_add_room_ajax_rejects_non_owner_even_if_session_accom_id_is_spoofed(self):
        self.client.force_login(self.other_owner)
        self._set_room_session(accom_id=self.accommodation.accom_id)

        response = self.client.post(
            reverse("accom_app:add_room_ajax"),
            data={"room_name": "Spoofed Access Room", "person_limit": 2},
        )

        self.assertEqual(response.status_code, 403)
        self.assertFalse(
            AdminRoom.objects.filter(
                accommodation=self.accommodation,
                room_name="Spoofed Access Room",
            ).exists()
        )

    def test_add_room_ajax_rejects_missing_room_type(self):
        self.client.force_login(self.owner)
        self._set_room_session(accom_id=self.accommodation.accom_id)

        response = self.client.post(
            reverse("accom_app:add_room_ajax"),
            data={
                "room_name": "No Type Room",
                "person_limit": 2,
                "price_per_night": "1200.00",
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("room type is required", response.json().get("message", "").lower())

    def test_add_room_ajax_rejects_invalid_capacity_price_or_availability(self):
        self.client.force_login(self.owner)
        self._set_room_session(accom_id=self.accommodation.accom_id)

        response = self.client.post(
            reverse("accom_app:add_room_ajax"),
            data={
                "room_name": "Bad Room",
                "room_type": "Standard",
                "person_limit": 0,
                "price_per_night": "0",
                "current_availability": 2,
            },
        )
        self.assertEqual(response.status_code, 400)
        payload = response.json()
        self.assertEqual(payload.get("status"), "error")
        self.assertIn("capacity", payload.get("message", "").lower())

    def test_get_rooms_json_returns_authoritative_contract_fields(self):
        self.client.force_login(self.owner)
        self._set_room_session(accom_id=self.accommodation.accom_id)

        room = AdminRoom.objects.create(
            accommodation=self.accommodation,
            room_name="Suite 401",
            person_limit=4,
            current_availability=2,
            price_per_night="3500.00",
            status="AVAILABLE",
        )
        AuthoritativeRoomDetails.objects.create(
            room=room,
            room_type="Suite",
            amenities='["wifi","pool"]',
        )

        response = self.client.get(reverse("accom_app:get_rooms_json"))
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body.get("status"), "success")
        self.assertEqual(len(body.get("rooms", [])), 1)
        row = body["rooms"][0]
        self.assertEqual(row["room_type"], "Suite")
        self.assertEqual(row["person_limit"], 4)
        self.assertEqual(row["price_per_night"], "3500.00")
        self.assertEqual(row["availability_status"], "AVAILABLE")
        self.assertIn("wifi", [x.lower() for x in row.get("amenities", [])])

    def test_update_room_ajax_updates_authoritative_room_and_details(self):
        self.client.force_login(self.owner)
        self._set_room_session(accom_id=self.accommodation.accom_id)

        room = AdminRoom.objects.create(
            accommodation=self.accommodation,
            room_name="Classic 101",
            person_limit=2,
            current_availability=2,
            price_per_night="1200.00",
            status="AVAILABLE",
        )

        response = self.client.post(
            reverse("accom_app:update_room_ajax"),
            data={
                "room_id": room.room_id,
                "room_type": "Family",
                "room_name": "Family 101",
                "capacity": 5,
                "price_per_night": "2500.00",
                "availability_status": "UNAVAILABLE",
                "amenities": "wifi,aircon,breakfast",
                "current_availability": 4,
            },
        )
        self.assertEqual(response.status_code, 200)

        room.refresh_from_db()
        self.assertEqual(room.room_name, "Family 101")
        self.assertEqual(room.person_limit, 5)
        self.assertEqual(str(room.price_per_night), "2500.00")
        self.assertEqual(room.status, "UNAVAILABLE")
        self.assertEqual(room.current_availability, 4)
        details = AuthoritativeRoomDetails.objects.get(room=room)
        self.assertEqual(details.room_type, "Family")
        self.assertIn("wifi", details.amenities.lower())

    def test_update_room_ajax_rejects_non_owner(self):
        room = AdminRoom.objects.create(
            accommodation=self.accommodation,
            room_name="Locked 001",
            person_limit=2,
            current_availability=2,
            price_per_night="1500.00",
            status="AVAILABLE",
        )

        self.client.force_login(self.other_owner)
        self._set_room_session(accom_id=self.accommodation.accom_id)
        response = self.client.post(
            reverse("accom_app:update_room_ajax"),
            data={
                "room_id": room.room_id,
                "room_name": "Hacked Name",
                "person_limit": 2,
                "price_per_night": "1500.00",
                "status": "AVAILABLE",
            },
        )
        self.assertEqual(response.status_code, 403)
        room.refresh_from_db()
        self.assertEqual(room.room_name, "Locked 001")

    def test_register_guest_to_room_is_safely_blocked_by_default(self):
        self.client.force_login(self.owner)
        self._set_room_session(accom_id=self.accommodation.accom_id)

        admin_room = AdminRoom.objects.create(
            accommodation=self.accommodation,
            room_name="Assigned Room",
            person_limit=2,
            current_availability=2,
            price_per_night="1500.00",
            status="AVAILABLE",
        )

        check_in = date.today() + timedelta(days=1)
        check_out = check_in + timedelta(days=2)
        response = self.client.post(
            reverse("accom_app:register_room_guest_ajax"),
            data={
                "room_id": admin_room.room_id,
                "guest_first_name": "Walk",
                "guest_last_name": "In",
                "checked_in": check_in.isoformat(),
                "checked_out": check_out.isoformat(),
                "num_guests": 1,
            },
        )

        self.assertEqual(response.status_code, 409)
        payload = response.json()
        self.assertEqual(payload.get("status"), "error")
        self.assertIn("disabled", str(payload.get("message", "")).lower())
        self.assertTrue(
            AdminRoomAssignment.objects.filter(room=admin_room).count() == 0
        )
        self.assertFalse(
            LegacyRoom.objects.filter(
                accom_id=self.accommodation,
                room_name=admin_room.room_name,
            ).exists()
        )
        self.assertEqual(RoomsGuestAdd.objects.count(), 0)

    def test_delete_room_ajax_cleans_legacy_rows_by_name_mapping(self):
        self.client.force_login(self.owner)
        self._set_room_session(accom_id=self.accommodation.accom_id)

        admin_room = AdminRoom.objects.create(
            accommodation=self.accommodation,
            room_name="Delete Me",
            person_limit=2,
            current_availability=2,
            price_per_night="1300.00",
            status="AVAILABLE",
        )
        legacy_room = LegacyRoom.objects.create(
            accom_id=self.accommodation,
            room_name="Delete Me",
            person_limit=2,
            current_availability=2,
            status="AVAILABLE",
        )
        RoomsGuestAdd.objects.create(
            room_id=legacy_room,
            accom_id=self.accommodation,
            checked_in=date.today(),
            checked_out=date.today() + timedelta(days=1),
            no_of_nights=1,
            month="January",
            num_guests=1,
        )

        response = self.client.post(
            reverse("accom_app:delete_room_ajax"),
            data={"room_id": admin_room.room_id},
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(AdminRoom.objects.filter(room_id=admin_room.room_id).exists())
        self.assertFalse(LegacyRoom.objects.filter(pk=legacy_room.pk).exists())
        self.assertFalse(RoomsGuestAdd.objects.filter(room_id=legacy_room).exists())


class OwnerReportsAnalyticsTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        owner_group, _ = Group.objects.get_or_create(name="accommodation_owner")

        self.owner = user_model.objects.create_user(
            username="analytics_owner",
            email="analytics_owner@example.com",
            password="secure-pass-123",
            first_name="Analytics",
            last_name="Owner",
        )
        self.owner.groups.add(owner_group)

        self.accommodation = Accomodation.objects.create(
            owner=self.owner,
            company_name="Analytics Hotel",
            email_address="analytics-hotel@example.com",
            location="Bayawan",
            company_type="hotel",
            password="accom-pass-owner",
            phone_number="09990009999",
            approval_status="accepted",
            status="accepted",
        )
        self.room = AdminRoom.objects.create(
            accommodation=self.accommodation,
            room_name="Executive Twin",
            person_limit=5,
            current_availability=5,
            price_per_night="3400.00",
            status="AVAILABLE",
        )
        self.guest = Guest.objects.create_user(
            username="analytics_guest",
            email="analytics_guest@example.com",
            password="guest-pass-123",
            first_name="Test",
            last_name="Guest",
            country_of_origin="Philippines",
            phone_number="09171234567",
            sex="F",
        )

    def test_reports_default_to_latest_confirmed_booking_month_when_unfiltered(self):
        AccommodationBooking.objects.create(
            guest=self.guest,
            accommodation=self.accommodation,
            room=self.room,
            check_in=date(2026, 5, 10),
            check_out=date(2026, 5, 13),
            num_guests=2,
            status="confirmed",
            total_amount="6800.00",
        )

        self.client.force_login(self.owner)
        response = self.client.get(reverse("accom_app:owner_reports_analytics"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["selected_month"], 5)
        self.assertEqual(response.context["selected_year"], 2026)
        self.assertEqual(response.context["total_checkins"], 1)

    def test_reports_respect_explicit_month_filter(self):
        AccommodationBooking.objects.create(
            guest=self.guest,
            accommodation=self.accommodation,
            room=self.room,
            check_in=date(2026, 5, 25),
            check_out=date(2026, 5, 27),
            num_guests=2,
            status="confirmed",
            total_amount="6800.00",
        )

        self.client.force_login(self.owner)
        response = self.client.get(
            reverse("accom_app:owner_reports_analytics"),
            {"month": "4", "year": "2026"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["selected_month"], 4)
        self.assertEqual(response.context["selected_year"], 2026)
        self.assertEqual(response.context["total_checkins"], 0)

    def test_reports_support_trend_window_and_metric_filters(self):
        AccommodationBooking.objects.create(
            guest=self.guest,
            accommodation=self.accommodation,
            room=self.room,
            check_in=date(2026, 5, 10),
            check_out=date(2026, 5, 13),
            num_guests=2,
            status="confirmed",
            total_amount="6800.00",
        )

        self.client.force_login(self.owner)
        response = self.client.get(
            reverse("accom_app:owner_reports_analytics"),
            {"month": "5", "year": "2026", "trend_window": "12", "trend_metric": "rooms"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["selected_trend_window"], "12")
        self.assertEqual(response.context["selected_trend_metric"], "rooms")
        self.assertEqual(len(response.context["trend_rows"]), 12)

    def test_reports_invalid_trend_filters_fallback_to_defaults(self):
        self.client.force_login(self.owner)
        response = self.client.get(
            reverse("accom_app:owner_reports_analytics"),
            {"trend_window": "99", "trend_metric": "invalid"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["selected_trend_window"], "6")
        self.assertEqual(response.context["selected_trend_metric"], "all")
        self.assertEqual(len(response.context["trend_rows"]), 6)
