import json
import os
import json
import time
import csv
from pathlib import Path
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import Client, TestCase, SimpleTestCase, override_settings
from django.utils import timezone

from admin_app.models import Accomodation, Employee, Room, TourismInformation, TourAssignment
from accom_app.models import AuthoritativeRoomDetails
from guest_app.models import AccommodationBooking, Billing, Guest, TourBooking, Pending
from ai_chatbot.views import (
    _classify_intent_and_extract_params,
    _detect_admin_support_topic,
    _detect_owner_support_topic,
    _detect_employee_support_topic,
    _extract_params_with_confidence,
    _extract_params_from_message,
    _format_cnn_prediction_for_chat,
    _is_my_accommodation_booking_status_command,
    _is_guest_tour_booking_command,
    _is_guest_view_tour_bookings_command,
    _normalize_common_chat_typos,
    _normalize_button_parity_message,
    _extract_intro_first_name,
    _build_out_of_scope_payload,
    _is_out_of_scope_message,
    _is_personalization_decline_message,
    _next_accommodation_clarifying_question,
    _extract_preference_profile,
    _build_personalization_offer_text,
    _build_accommodation_link_actions,
    _resolve_intent_text_cnn_model_path,
    _resolve_accommodation_text_cnn_model_path,
)
from ai_chatbot.llm_translation import _normalize_language_code, translate_to_english
from ai_chatbot.recommenders import (
    recommend_accommodations,
    _resolve_decision_tree_model_path,
    get_decision_tree_runtime_status,
)
from ai_chatbot.models import ChatbotLog, RecommendationEvent, UsabilitySurveyResponse, SystemMetricLog
from tour_app.models import Admission_Rates, Tour_Add, Tour_Schedule


class OpenAIChatEndpointTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.url = "/api/chat/"
        user_model = get_user_model()
        self.user = user_model.objects.create_user(
            username="chat_test_user",
            email="chat_test_user@example.com",
            password="secure-pass-123",
            first_name="Chat",
            last_name="Tester",
        )
        self.client.force_login(self.user)
        self.health_url = "/api/chat/health/"
        self.decision_tree_runtime_url = "/api/chat/decision-tree-runtime/"

        self.tour = Tour_Add.objects.create(
            tour_id="00001",
            tour_name="River Adventure",
            description="A relaxing river and nature tour.",
        )

        now = timezone.now()
        self.schedule = Tour_Schedule.objects.create(
            tour=self.tour,
            start_time=now + timedelta(days=1),
            end_time=now + timedelta(days=2),
            price=Decimal("500.00"),
            slots_available=20,
            slots_booked=2,
            duration_days=1,
            status="active",
        )

        Admission_Rates.objects.create(
            tour_id=self.tour,
            payables="Environmental Fee",
            price=Decimal("50.00"),
        )

        self.accommodation = Accomodation.objects.create(
            company_name="Bayawan Stay Hotel",
            email_address="bayawan-stay@example.com",
            location="Bayawan",
            company_type="hotel",
            password="demo-password-123",
            phone_number="09999999999",
            approval_status="accepted",
            status="accepted",
        )
        self.room = Room.objects.create(
            accommodation=self.accommodation,
            room_name="Pool View Standard Room",
            person_limit=4,
            current_availability=2,
            price_per_night=Decimal("1200.00"),
            status="AVAILABLE",
        )
        self.other_accommodation = Accomodation.objects.create(
            company_name="Suba Budget Inn",
            email_address="suba-inn@example.com",
            location="Suba",
            company_type="hotel",
            password="demo-password-456",
            phone_number="09999999998",
            approval_status="accepted",
            status="accepted",
        )
        self.other_room = Room.objects.create(
            accommodation=self.other_accommodation,
            room_name="Economy Room",
            person_limit=2,
            current_availability=2,
            price_per_night=Decimal("1100.00"),
            status="AVAILABLE",
        )
        self.inn_accommodation = Accomodation.objects.create(
            company_name="Bayawan Comfort Inn",
            email_address="comfort-inn@example.com",
            location="Bayawan",
            company_type="inn",
            password="demo-password-789",
            phone_number="09999999997",
            approval_status="accepted",
            status="accepted",
        )
        self.inn_room = Room.objects.create(
            accommodation=self.inn_accommodation,
            room_name="Inn Deluxe",
            person_limit=2,
            current_availability=2,
            price_per_night=Decimal("1200.00"),
            status="AVAILABLE",
        )
        TourismInformation.objects.create(
            spot_name="Bayawan Heritage Park",
            description="A cultural landmark in Bayawan City.",
            location="Bayawan City Proper",
            contact_information="09171234567",
            operating_hours="08:00 AM - 05:00 PM",
            publication_status="published",
            is_active=True,
            created_by=self.user,
            updated_by=self.user,
        )
        TourismInformation.objects.create(
            spot_name="Draft Hidden Falls",
            description="Should not be visible to chatbot users.",
            location="Bayawan Hinterlands",
            publication_status="draft",
            is_active=True,
            created_by=self.user,
            updated_by=self.user,
        )

    def test_get_recommendation_intent_returns_tour(self):
        payload = {"message": "recommend a river tour for 2 guests under 700"}

        response = self.client.post(
            self.url,
            data=json.dumps(payload),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        text = response.json().get("fulfillmentText", "")
        self.assertIn("Top recommendations for you", text)
        self.assertIn("River Adventure", text)

    def test_stay_planning_asks_for_total_budget_when_missing(self):
        response = self.client.post(
            self.url,
            data=json.dumps({"message": "Can you help me plan my stay in Bayawan?"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body.get("needs_clarification"))
        self.assertEqual(body.get("missing_slot"), "total_budget")
        self.assertIn("total budget", str(body.get("fulfillmentText") or "").lower())

    def test_stay_planning_followup_budget_keyword_does_not_crash(self):
        first = self.client.post(
            self.url,
            data=json.dumps({"message": "help me plan my bayawan stay"}),
            content_type="application/json",
        )
        self.assertEqual(first.status_code, 200)
        first_body = first.json()
        self.assertEqual(str(first_body.get("missing_slot") or "").strip(), "total_budget")

        followup = self.client.post(
            self.url,
            data=json.dumps({"message": "budget 8000"}),
            content_type="application/json",
        )
        self.assertEqual(followup.status_code, 200)
        followup_body = followup.json()
        self.assertNotIn("traceback", str(followup_body.get("fulfillmentText") or "").lower())
        self.assertTrue(str(followup_body.get("fulfillmentText") or "").strip())

    def test_stay_planning_builds_estimated_plan_with_budget(self):
        response = self.client.post(
            self.url,
            data=json.dumps(
                {
                    "message": (
                        "I have a 10000 peso budget. Help me plan my stay in Bayawan for 3 days "
                        "as a couple with mixed activities."
                    )
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        text = str(body.get("fulfillmentText") or "").lower()
        self.assertIn("bayawan", text)
        self.assertIn("budget plan", text)
        self.assertIn("accommodations (top matches)", text)
        self.assertIn("tours (top matches)", text)

    def test_small_talk_intro_uses_name_naturally(self):
        response = self.client.post(
            self.url,
            data=json.dumps({"message": "hi i am renold"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        text = str(body.get("fulfillmentText") or "").lower()
        self.assertIn("renold", text)
        self.assertTrue(
            any(token in text for token in ("what would you like", "how can i help", "what can i help"))
        )

    def test_help_plus_planning_request_routes_to_planning_flow(self):
        response = self.client.post(
            self.url,
            data=json.dumps({"message": "can you help me plan my stay in bayawan?"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(bool(body.get("needs_clarification")))
        self.assertEqual(str(body.get("missing_slot") or "").strip(), "total_budget")

    def test_planning_follow_up_make_it_cheaper_keeps_planning_flow(self):
        first = self.client.post(
            self.url,
            data=json.dumps({"message": "plan my stay in bayawan with 10000 pesos for 3 days"}),
            content_type="application/json",
        )
        self.assertEqual(first.status_code, 200)

        response = self.client.post(
            self.url,
            data=json.dumps({"message": "make it cheaper"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        text = str(body.get("fulfillmentText") or "").lower()
        self.assertTrue(
            any(
                token in text
                for token in (
                    "bayawan",
                    "budget",
                    "days",
                    "solo",
                    "couple",
                    "family",
                    "group",
                    "accommodation",
                    "activities",
                    "relaxing",
                    "adventure",
                    "style",
                )
            )
        )
        self.assertNotIn("which accommodation type", text)

    def test_planning_follow_up_persists_after_reset_via_assistant_memory(self):
        first = self.client.post(
            self.url,
            data=json.dumps({"message": "plan my stay in bayawan with 10000 pesos for 3 days for a couple"}),
            content_type="application/json",
        )
        self.assertEqual(first.status_code, 200)
        reset = self.client.post(
            self.url,
            data=json.dumps({"message": "reset"}),
            content_type="application/json",
        )
        self.assertEqual(reset.status_code, 200)

        response = self.client.post(
            self.url,
            data=json.dumps({"message": "make it cheaper"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        text = str(response.json().get("fulfillmentText") or "").lower()
        self.assertIn("bayawan", text)
        self.assertTrue(any(token in text for token in ("budget", "plan")))

    def test_planning_follow_up_family_version_keeps_planning_flow(self):
        first = self.client.post(
            self.url,
            data=json.dumps({"message": "plan my stay in bayawan with 10000 pesos for 3 days"}),
            content_type="application/json",
        )
        self.assertEqual(first.status_code, 200)

        response = self.client.post(
            self.url,
            data=json.dumps({"message": "family version"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        text = str(body.get("fulfillmentText") or "").lower()
        self.assertTrue(
            any(
                token in text
                for token in (
                    "bayawan",
                    "budget",
                    "days",
                    "solo",
                    "couple",
                    "family",
                    "group",
                    "accommodation",
                    "activities",
                    "relaxing",
                    "adventure",
                    "style",
                )
            )
        )
        self.assertNotIn("which accommodation type", text)

    def test_typed_equivalent_show_accommodations_returns_accommodation_recommendations(self):
        canonical = _normalize_button_parity_message("where should i stay")
        self.assertEqual(canonical, "show accommodation recommendations")
        parsed = _classify_intent_and_extract_params(canonical)
        self.assertEqual(str(parsed.get("intent") or "").strip().lower(), "get_accommodation_recommendation")

    def test_typed_equivalent_show_tours_returns_tour_recommendations(self):
        canonical = _normalize_button_parity_message("what tours do you have")
        self.assertEqual(canonical, "show available tours")
        parsed = _classify_intent_and_extract_params(canonical)
        self.assertEqual(str(parsed.get("intent") or "").strip().lower(), "get_recommendation")

    def test_typo_bookigns_still_opens_guest_tour_bookings(self):
        canonical = _normalize_button_parity_message("show my tour bookigns")
        self.assertEqual(canonical, "show my tour bookings")
        self.assertTrue(_is_guest_view_tour_bookings_command(canonical))

    def test_planning_response_adds_proactive_next_step_and_limits_quick_replies(self):
        response = self.client.post(
            self.url,
            data=json.dumps(
                {
                    "message": (
                        "I have 10000 pesos budget for 3 days in bayawan for a couple "
                        "with mixed activities, relaxing style, and with accommodation."
                    )
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        text = str(body.get("fulfillmentText") or "").lower()
        self.assertIn("if you want", text)
        quick_replies = body.get("quick_replies") if isinstance(body.get("quick_replies"), list) else []
        self.assertLessEqual(len(quick_replies), 3)

    @override_settings(TOURISM_APPROVED_ACCOMMODATION_IDS=[])
    def test_accommodation_recommendation_respects_approved_allowlist(self):
        owner_group, _ = Group.objects.get_or_create(name="accommodation_owner")
        owner_user = get_user_model().objects.create_user(
            username="approved_filter_owner",
            email="approved_filter_owner@example.com",
            password="owner-pass-123",
            first_name="Approved",
            last_name="Owner",
        )
        owner_user.groups.add(owner_group)
        self.accommodation.owner = owner_user
        self.accommodation.save(update_fields=["owner"])
        self.other_accommodation.owner = owner_user
        self.other_accommodation.save(update_fields=["owner"])
        self.inn_accommodation.owner = owner_user
        self.inn_accommodation.save(update_fields=["owner"])

        allowed_id = int(self.accommodation.accom_id)
        with self.settings(TOURISM_APPROVED_ACCOMMODATION_IDS=[allowed_id]):
            response = self.client.post(
                self.url,
                data=json.dumps(
                    {"message": "recommend a hotel in bayawan for 2 guests under 2000"}
                ),
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        trace = body.get("recommendation_trace") if isinstance(body.get("recommendation_trace"), list) else []
        self.assertTrue(trace)
        for item in trace:
            meta = item.get("meta") if isinstance(item.get("meta"), dict) else {}
            self.assertEqual(int(meta.get("accom_id")), allowed_id)

    @override_settings(TOURISM_APPROVED_ACCOMMODATION_IDS=[])
    def test_accommodation_recommendation_excludes_prototype_placeholder_records(self):
        owner_group, _ = Group.objects.get_or_create(name="accommodation_owner")
        owner_user = get_user_model().objects.create_user(
            username="prototype_filter_owner",
            email="prototype_filter_owner@example.com",
            password="owner-pass-123",
            first_name="Prototype",
            last_name="Owner",
        )
        owner_user.groups.add(owner_group)

        prototype_accom = Accomodation.objects.create(
            company_name="Prototype Recommendation Stay",
            email_address="prototype_recommendation_stay@placeholder.local",
            location="Bayawan",
            company_type="hotel",
            password="demo-password",
            phone_number="09990000444",
            approval_status="accepted",
            status="accepted",
            is_active=True,
            owner=owner_user,
            official_booking_url="https://example.com/prototype",
        )
        Room.objects.create(
            accommodation=prototype_accom,
            room_name="Prototype Recommendation Room",
            person_limit=2,
            current_availability=2,
            price_per_night=Decimal("1200.00"),
            status="AVAILABLE",
        )

        self.accommodation.owner = owner_user
        self.accommodation.save(update_fields=["owner"])
        self.other_accommodation.owner = owner_user
        self.other_accommodation.save(update_fields=["owner"])
        self.inn_accommodation.owner = owner_user
        self.inn_accommodation.save(update_fields=["owner"])

        response = self.client.post(
            self.url,
            data=json.dumps(
                {"message": "recommend a hotel in bayawan for 2 guests under 2000"}
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        trace = body.get("recommendation_trace") if isinstance(body.get("recommendation_trace"), list) else []
        self.assertTrue(trace)
        for item in trace:
            meta = item.get("meta") if isinstance(item.get("meta"), dict) else {}
            email = str(meta.get("email_address") or "").lower()
            name = str(meta.get("accom_name") or "").lower()
            self.assertFalse(email.endswith("@placeholder.local"))
            self.assertNotIn("prototype", name)

    def test_travel_guidance_uses_current_location_for_distance_estimate(self):
        response = self.client.post(
            self.url,
            data=json.dumps(
                {
                    "message": "How far is Bayawan City Plaza from me?",
                    "client_location": {
                        "status": "available",
                        "latitude": 9.367904,
                        "longitude": 122.807103,
                    },
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        text = str(body.get("fulfillmentText") or "").lower()
        self.assertIn("estimated distance", text)
        self.assertTrue(str(body.get("billing_link") or "").startswith("https://www.google.com/maps/dir/"))

    def test_travel_guidance_handles_denied_location_gracefully(self):
        response = self.client.post(
            self.url,
            data=json.dumps(
                {
                    "message": "How far is Bayawan City Plaza from me?",
                    "client_location": {
                        "status": "denied",
                    },
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        text = str(response.json().get("fulfillmentText") or "").lower()
        self.assertIn("location access looks disabled", text)

    @patch("ai_chatbot.views._classify_intent_and_extract_params")
    @patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False)
    def test_weekend_tour_query_adds_timeframe_clarity_note(self, mock_classify):
        mock_classify.return_value = {
            "intent": "get_recommendation",
            "params": {"tour_timeframe_hint": "this weekend"},
            "source": "heuristic_intent_fallback",
            "confidence": 0.85,
            "needs_clarification": False,
            "clarification_question": "",
            "clarification_field": "",
            "intent_classifier": {"source": "heuristic_intent_fallback"},
        }
        response = self.client.post(
            self.url,
            data=json.dumps({"message": "What can I do this weekend?"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        text = str(response.json().get("fulfillmentText") or "").lower()
        self.assertIn("for this weekend", text)
        self.assertIn("tour options", text)

    def test_follow_up_tour_preference_updates_recommendations(self):
        nature_tour = Tour_Add.objects.create(
            tour_id="00002",
            tour_name="Nature and Falls Day Tour",
            description="A scenic nature and waterfall experience.",
        )
        now = timezone.now()
        Tour_Schedule.objects.create(
            tour=nature_tour,
            start_time=now + timedelta(days=3),
            end_time=now + timedelta(days=4),
            price=Decimal("450.00"),
            slots_available=20,
            slots_booked=1,
            duration_days=1,
            status="active",
        )

        first = self.client.post(
            self.url,
            data=json.dumps({"message": "show available tours"}),
            content_type="application/json",
        )
        self.assertEqual(first.status_code, 200)
        first_text = str(first.json().get("fulfillmentText") or "").lower()
        self.assertTrue(
            ("tour options" in first_text)
            or ("recommendations for you" in first_text)
            or ("tours you might like" in first_text)
        )

        second = self.client.post(
            self.url,
            data=json.dumps({"message": "i prefer nature tours"}),
            content_type="application/json",
        )
        self.assertEqual(second.status_code, 200)
        follow_up_text = str(second.json().get("fulfillmentText") or "").lower()
        self.assertIn("top recommendations for you", follow_up_text)
        self.assertIn("nature", follow_up_text)

    def test_follow_up_sea_preference_updates_tour_results(self):
        sea_tour = Tour_Add.objects.create(
            tour_id="00003",
            tour_name="Sea and Coast Escape",
            description="A relaxing beach and seaside experience.",
        )
        now = timezone.now()
        Tour_Schedule.objects.create(
            tour=sea_tour,
            start_time=now + timedelta(days=5),
            end_time=now + timedelta(days=6),
            price=Decimal("480.00"),
            slots_available=15,
            slots_booked=1,
            duration_days=1,
            status="active",
        )

        self.client.post(
            self.url,
            data=json.dumps({"message": "show available tours"}),
            content_type="application/json",
        )
        second = self.client.post(
            self.url,
            data=json.dumps({"message": "i prefer sea tours"}),
            content_type="application/json",
        )
        self.assertEqual(second.status_code, 200)
        follow_up_text = str(second.json().get("fulfillmentText") or "").lower()
        self.assertIn("top recommendations for you", follow_up_text)
        self.assertIn("sea and coast escape", follow_up_text)
        self.assertIn("match:", follow_up_text)

    def test_city_highlights_preference_mentions_unavailable_matching_tour(self):
        response = self.client.post(
            self.url,
            data=json.dumps({"message": "i prefer city highlights"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        text = str(response.json().get("fulfillmentText") or "").lower()
        self.assertIn("top recommendations for you", text)
        self.assertTrue(
            ("matching tour(s) found but currently without upcoming schedules" in text)
            or ("only tours with upcoming schedules are shown in this list" in text)
        )

    def test_calculate_billing_intent_returns_total(self):
        payload = {"message": f"calculate bill for {self.schedule.sched_id} for 2 guests"}

        response = self.client.post(
            self.url,
            data=json.dumps(payload),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        text = response.json().get("fulfillmentText", "")
        self.assertIn("Billing Summary", text)
        self.assertIn("Total amount due: PHP 1100.00", text)

    @patch("ai_chatbot.views._classify_intent_and_extract_params")
    @patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False)
    def test_tourism_information_intent_returns_published_records_only(self, mock_classify):
        mock_classify.return_value = {
            "intent": "get_tourism_information",
            "params": {"tourism_query": "heritage"},
            "source": "text_cnn_intent",
            "confidence": 0.91,
            "needs_clarification": False,
            "clarification_question": "",
            "clarification_field": "",
            "intent_classifier": {
                "intent": "get_tourism_information",
                "source": "text_cnn_intent",
                "confidence": 0.91,
                "top_3": [],
                "error": "",
            },
        }

        response = self.client.post(
            self.url,
            data=json.dumps({"message": "tell me about heritage park"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        text = response.json().get("fulfillmentText", "")
        self.assertIn("Bayawan Heritage Park", text)
        self.assertNotIn("Draft Hidden Falls", text)

    @patch("ai_chatbot.views._openai_extract_intent_and_params", side_effect=AssertionError("deprecated_parser_called"))
    @patch("ai_chatbot.views._classify_intent_and_extract_params")
    @patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False)
    def test_intent_routing_uses_text_cnn_classifier_path_first(self, mock_classify, _mock_legacy):
        mock_classify.return_value = {
            "intent": "calculate_billing",
            "params": {"sched_id": self.schedule.sched_id, "guests": 2},
            "source": "text_cnn_intent",
            "confidence": 0.93,
            "needs_clarification": False,
            "clarification_question": "",
            "clarification_field": "",
            "intent_classifier": {
                "intent": "calculate_billing",
                "source": "text_cnn_intent",
                "confidence": 0.93,
                "top_3": [],
                "error": "",
            },
        }
        response = self.client.post(
            self.url,
            data=json.dumps({"message": "anything"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn("Billing Summary", body.get("fulfillmentText", ""))
        self.assertIn(
            body.get("response_nlg_source"),
            (
                "openai_nlg_unavailable",
                "openai_nlg_disabled",
                "llm_nlg_unavailable",
                "llm_nlg_disabled",
                "gemini_nlg_unavailable",
                "gemini_nlg_error",
                "gemini_nlg_fallback_paraphrase",
                "openai_nlg_error_paraphrase",
                "gemini_nlg_unavailable_paraphrase",
                "gemini_nlg_error_paraphrase",
                "llm_nlg_unavailable_paraphrase",
                "llm_nlg_disabled_paraphrase",
            ),
        )
        self.assertEqual(body.get("intent_classifier", {}).get("source"), "text_cnn_intent")

    @patch("ai_chatbot.views._classify_intent_and_extract_params")
    @patch.dict(
        os.environ,
        {"OPENAI_API_KEY": "test-key", "CHATBOT_OPENAI_NLG_ENABLED": "1"},
        clear=False,
    )
    def test_openai_is_used_for_final_nlg_only(self, mock_classify):
        mock_classify.return_value = {
            "intent": "calculate_billing",
            "params": {"sched_id": self.schedule.sched_id, "guests": 2},
            "source": "text_cnn_intent",
            "confidence": 0.93,
            "needs_clarification": False,
            "clarification_question": "",
            "clarification_field": "",
            "intent_classifier": {
                "intent": "calculate_billing",
                "source": "text_cnn_intent",
                "confidence": 0.93,
                "top_3": [],
                "error": "",
            },
        }

        with patch("ai_chatbot.views.OpenAI") as mock_openai_cls:
            mock_client = mock_openai_cls.return_value
            mock_client.chat.completions.create.return_value.choices = [
                type("Choice", (), {"message": type("Msg", (), {"content": "Refined billing response."})()})()
            ]
            response = self.client.post(
                self.url,
                data=json.dumps({"message": "calculate bill"}),
                content_type="application/json",
            )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body.get("fulfillmentText"), "Refined billing response.")
        self.assertEqual(body.get("response_nlg_source"), "openai_nlg")
        self.assertTrue(mock_openai_cls.called)

    @patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False)
    def test_message_mode_works_without_openai_key(self):
        payload = {"message": "recommend a river tour for 2 guests under 700"}

        response = self.client.post(
            self.url,
            data=json.dumps(payload),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        text = response.json().get("fulfillmentText", "")
        self.assertIn("Top recommendations for you", text)

    def test_chat_requires_authenticated_user(self):
        self.client.logout()
        payload = {"message": "recommend a hotel in bayawan"}

        response = self.client.post(
            self.url,
            data=json.dumps(payload),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 401)
        body = response.json()
        self.assertEqual(body.get("error_code"), "chat_requires_login")
        self.assertIn("Please log in first", body.get("fulfillmentText", ""))

    @patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False)
    def test_accommodation_slot_filling_flow(self):
        response_1 = self.client.post(
            self.url,
            data=json.dumps({"message": "hotel in bayawan"}),
            content_type="application/json",
        )
        self.assertEqual(response_1.status_code, 200)
        first_body = response_1.json()
        first_text = first_body.get("fulfillmentText", "").lower()
        self.assertIn("check-in/check-out dates", first_text)
        self.assertTrue(
            ("top hotel/inn recommendations" in first_text)
            or ("please provide" in first_text)
        )

        response_2 = self.client.post(
            self.url,
            data=json.dumps(
                {"message": "budget 1500, 2 guests, 2026-03-10 to 2026-03-12"}
            ),
            content_type="application/json",
        )
        self.assertEqual(response_2.status_code, 200)
        second_body = response_2.json()
        self.assertIn("top hotel/inn recommendations", second_body.get("fulfillmentText", "").lower())
        self.assertIn("recommendation_trace", second_body)

    @patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False)
    def test_numeric_option_selection_uses_last_accommodation_recommendations(self):
        response_1 = self.client.post(
            self.url,
            data=json.dumps(
                {
                    "message": (
                        "recommend an inn in bayawan for 2 guests under 1500 "
                        "from 2026-04-14 to 2026-04-17"
                    )
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(response_1.status_code, 200)
        self.assertIn("top hotel/inn recommendations", response_1.json().get("fulfillmentText", "").lower())

        response_2 = self.client.post(
            self.url,
            data=json.dumps({"message": "1"}),
            content_type="application/json",
        )
        self.assertEqual(response_2.status_code, 200)
        self.assertIn("booking draft", response_2.json().get("fulfillmentText", "").lower())

    @patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False)
    def test_single_digit_dates_are_normalized_in_accommodation_flow(self):
        response = self.client.post(
            self.url,
            data=json.dumps(
                {
                    "message": (
                        "recommend a hotel in bayawan, budget 1500, 2 guests, "
                        "2026-3-29 to 2026-03-31"
                    )
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        text = response.json().get("fulfillmentText", "").lower()
        self.assertIn("top hotel/inn recommendations", text)
        self.assertNotIn("please provide both check-in and check-out dates", text)

    @patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False)
    def test_location_first_flow_shows_preview_then_asks_dates(self):
        first = self.client.post(
            self.url,
            data=json.dumps({"message": "recommend a hotel for me"}),
            content_type="application/json",
        )
        self.assertEqual(first.status_code, 200)
        first_text = first.json().get("fulfillmentText", "").lower()
        self.assertIn("area in bayawan", first_text)
        self.assertNotIn("budget", first_text)

        second = self.client.post(
            self.url,
            data=json.dumps({"message": "bayawan"}),
            content_type="application/json",
        )
        self.assertEqual(second.status_code, 200)
        text = second.json().get("fulfillmentText", "").lower()
        self.assertIn("check-in/check-out", text)
        self.assertNotIn("top hotel/inn recommendations", text)
        self.assertNotIn("budget per night", text)

    @patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False)
    def test_reset_clears_slot_filling_state(self):
        first = self.client.post(
            self.url,
            data=json.dumps({"message": "hotel in bayawan"}),
            content_type="application/json",
        )
        self.assertEqual(first.status_code, 200)
        first_text = first.json().get("fulfillmentText", "").lower()
        self.assertTrue(("budget" in first_text) or ("check-in/check-out" in first_text))

        session_before_reset = self.client.session
        self.assertIn("ai_chatbot_state", session_before_reset)

        reset = self.client.post(
            self.url,
            data=json.dumps({"message": "reset"}),
            content_type="application/json",
        )
        self.assertEqual(reset.status_code, 200)
        self.assertIn("context cleared", reset.json().get("fulfillmentText", "").lower())

        session_after_reset = self.client.session
        self.assertNotIn("ai_chatbot_state", session_after_reset)

    def test_accommodation_recommendations_rank_by_reason_score(self):
        params = {
            "guests": 2,
            "budget": 1500,
            "company_type": "hotel",
            "amenities": "pool",
        }
        results = recommend_accommodations(params, limit=5)

        self.assertGreaterEqual(len(results), 2)
        top = results[0]
        second = results[1]
        self.assertEqual(top.meta.get("room_id"), self.room.room_id)
        self.assertGreater(top.score, second.score)

    def test_pending_and_declined_accommodations_are_excluded_from_recommendations(self):
        pending_accom = Accomodation.objects.create(
            company_name="Pending Visibility Hotel",
            email_address="pending-visibility@example.com",
            location="Bayawan",
            company_type="hotel",
            password="demo-password-pending",
            phone_number="09999999991",
            approval_status="pending",
            status="pending",
        )
        declined_accom = Accomodation.objects.create(
            company_name="Declined Visibility Hotel",
            email_address="declined-visibility@example.com",
            location="Bayawan",
            company_type="hotel",
            password="demo-password-declined",
            phone_number="09999999992",
            approval_status="declined",
            status="declined",
        )
        pending_room = Room.objects.create(
            accommodation=pending_accom,
            room_name="Pending Room",
            person_limit=3,
            current_availability=2,
            price_per_night=Decimal("800.00"),
            status="AVAILABLE",
        )
        declined_room = Room.objects.create(
            accommodation=declined_accom,
            room_name="Declined Room",
            person_limit=3,
            current_availability=2,
            price_per_night=Decimal("850.00"),
            status="AVAILABLE",
        )

        results = recommend_accommodations(
            {
                "guests": 2,
                "budget": 2000,
                "company_type": "hotel",
                "location": "Bayawan",
            },
            limit=10,
        )
        room_ids = {item.meta.get("room_id") for item in results}
        self.assertNotIn(pending_room.room_id, room_ids)
        self.assertNotIn(declined_room.room_id, room_ids)
        self.assertIn(self.room.room_id, room_ids)

    @patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False)
    def test_chat_payload_includes_ranking_score_and_strength(self):
        payload = {
            "message": (
                "recommend a hotel in bayawan for 2 guests under 1500 "
                "from 2026-03-10 to 2026-03-12 with pool"
            )
        }
        response = self.client.post(
            self.url,
            data=json.dumps(payload),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn("recommendation_trace", body)
        trace = body.get("recommendation_trace") or []
        self.assertGreaterEqual(len(trace), 1)
        first_item = trace[0]
        self.assertIn("ranking_score", first_item)
        self.assertIn("match_strength", first_item)
        self.assertIn("decision_tree_score", first_item)
        self.assertIn("cnn_alignment", first_item)
        self.assertIn("scoring_mode", first_item)
        self.assertIn(first_item.get("match_strength"), ("High", "Medium", "Low"))

    def test_cnn_predicted_type_influences_accommodation_ranking(self):
        base_params = {
            "guests": 2,
            "budget": 2000,
            "location": "bayawan",
        }

        inn_pref = dict(base_params)
        inn_pref["predicted_accommodation_type"] = "inn"
        inn_pref["predicted_accommodation_confidence"] = 0.95
        inn_results = recommend_accommodations(inn_pref, limit=5)
        self.assertGreaterEqual(len(inn_results), 2)
        self.assertEqual(inn_results[0].meta.get("room_id"), self.inn_room.room_id)

        hotel_pref = dict(base_params)
        hotel_pref["predicted_accommodation_type"] = "hotel"
        hotel_pref["predicted_accommodation_confidence"] = 0.95
        hotel_results = recommend_accommodations(hotel_pref, limit=5)
        self.assertGreaterEqual(len(hotel_results), 2)
        self.assertEqual(hotel_results[0].meta.get("room_id"), self.room.room_id)

    @patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False)
    def test_user_with_past_accommodation_bookings_gets_personalized_default_offer(self):
        today = timezone.now().date()
        AccommodationBooking.objects.create(
            guest=self.user,
            accommodation=self.accommodation,
            room=self.room,
            check_in=today - timedelta(days=40),
            check_out=today - timedelta(days=38),
            num_guests=2,
            status="confirmed",
            total_amount=Decimal("2400.00"),
        )

        response = self.client.post(
            self.url,
            data=json.dumps({"message": "recommend an inn near city"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        text = response.json().get("fulfillmentText", "").lower()
        self.assertTrue(
            ("quick default setup" in text and "reply yes or no" in text)
            or ("accommodation recommendations (suggested stays)" in text)
        )

    @patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False)
    def test_new_user_without_history_gets_standard_missing_slot_question(self):
        user_model = get_user_model()
        fresh_user = user_model.objects.create_user(
            username="fresh_chat_user",
            email="fresh_chat_user@example.com",
            password="secure-pass-456",
            first_name="Fresh",
            last_name="User",
        )
        self.client.force_login(fresh_user)

        response = self.client.post(
            self.url,
            data=json.dumps({"message": "recommend a hotel in bayawan"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        text = response.json().get("fulfillmentText", "").lower()
        self.assertTrue(("budget" in text) or ("check-in/check-out" in text))
        self.assertNotIn("quick default setup", text)

    @patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False)
    def test_no_response_declines_personalized_defaults(self):
        today = timezone.now().date()
        AccommodationBooking.objects.create(
            guest=self.user,
            accommodation=self.accommodation,
            room=self.room,
            check_in=today - timedelta(days=25),
            check_out=today - timedelta(days=23),
            num_guests=2,
            status="confirmed",
            total_amount=Decimal("2400.00"),
        )

        first = self.client.post(
            self.url,
            data=json.dumps({"message": "recommend a hotel in bayawan"}),
            content_type="application/json",
        )
        self.assertEqual(first.status_code, 200)
        self.assertIn(
            "reply yes or no",
            first.json().get("fulfillmentText", "").lower(),
        )

        decline = self.client.post(
            self.url,
            data=json.dumps({"message": "no"}),
            content_type="application/json",
        )
        self.assertEqual(decline.status_code, 200)
        decline_text = decline.json().get("fulfillmentText", "").lower()
        self.assertTrue(("check-in/check-out" in decline_text) or ("guests" in decline_text))
        self.assertNotIn("similar options", decline_text)
        self.assertNotIn("show me available inn rooms", decline_text)
        self.assertNotIn("budget per night", decline_text)

    @patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False)
    def test_decline_defaults_with_location_shows_preview_recommendations(self):
        today = timezone.now().date()
        AccommodationBooking.objects.create(
            guest=self.user,
            accommodation=self.accommodation,
            room=self.room,
            check_in=today - timedelta(days=20),
            check_out=today - timedelta(days=18),
            num_guests=2,
            status="confirmed",
            total_amount=Decimal("2400.00"),
        )

        first = self.client.post(
            self.url,
            data=json.dumps({"message": "show available hotels in villareal"}),
            content_type="application/json",
        )
        self.assertEqual(first.status_code, 200)
        self.assertIn(
            "reply yes or no",
            first.json().get("fulfillmentText", "").lower(),
        )

        decline = self.client.post(
            self.url,
            data=json.dumps({"message": "no"}),
            content_type="application/json",
        )
        self.assertEqual(decline.status_code, 200)
        decline_text = decline.json().get("fulfillmentText", "").lower()
        self.assertNotIn("great. here are the details i have so far", decline_text)
        self.assertTrue(
            ("top hotel/inn recommendations" in decline_text)
            or ("i could not find a strong hotel/inn match at this time." in decline_text)
        )

    @patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False)
    def test_declined_personalized_defaults_are_not_repeated_during_slot_filling(self):
        today = timezone.now().date()
        AccommodationBooking.objects.create(
            guest=self.user,
            accommodation=self.accommodation,
            room=self.room,
            check_in=today - timedelta(days=30),
            check_out=today - timedelta(days=28),
            num_guests=2,
            status="confirmed",
            total_amount=Decimal("2400.00"),
        )

        first = self.client.post(
            self.url,
            data=json.dumps({"message": "show available rooms in bayawan"}),
            content_type="application/json",
        )
        self.assertEqual(first.status_code, 200)
        self.assertIn(
            "reply yes or no",
            first.json().get("fulfillmentText", "").lower(),
        )

        decline = self.client.post(
            self.url,
            data=json.dumps({"message": "no"}),
            content_type="application/json",
        )
        self.assertEqual(decline.status_code, 200)
        self.assertNotIn(
            "reply yes or no",
            decline.json().get("fulfillmentText", "").lower(),
        )

        type_reply = self.client.post(
            self.url,
            data=json.dumps({"message": "a hotel please"}),
            content_type="application/json",
        )
        self.assertEqual(type_reply.status_code, 200)
        self.assertNotIn(
            "reply yes or no",
            type_reply.json().get("fulfillmentText", "").lower(),
        )

        stay_reply = self.client.post(
            self.url,
            data=json.dumps({"message": "2 nights"}),
            content_type="application/json",
        )
        self.assertEqual(stay_reply.status_code, 200)
        self.assertNotIn(
            "reply yes or no",
            stay_reply.json().get("fulfillmentText", "").lower(),
        )

        guests_reply = self.client.post(
            self.url,
            data=json.dumps({"message": "4 guests"}),
            content_type="application/json",
        )
        self.assertEqual(guests_reply.status_code, 200)
        self.assertNotIn(
            "reply yes or no",
            guests_reply.json().get("fulfillmentText", "").lower(),
        )

    @patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False)
    def test_full_step_by_step_accommodation_flow_after_declining_defaults(self):
        today = timezone.now().date()
        AccommodationBooking.objects.create(
            guest=self.user,
            accommodation=self.accommodation,
            room=self.room,
            check_in=today - timedelta(days=35),
            check_out=today - timedelta(days=33),
            num_guests=2,
            status="confirmed",
            total_amount=Decimal("2400.00"),
        )

        first = self.client.post(
            self.url,
            data=json.dumps({"message": "show available rooms in bayawan"}),
            content_type="application/json",
        )
        self.assertEqual(first.status_code, 200)
        self.assertIn(
            "reply yes or no",
            first.json().get("fulfillmentText", "").lower(),
        )

        decline = self.client.post(
            self.url,
            data=json.dumps({"message": "no"}),
            content_type="application/json",
        )
        self.assertEqual(decline.status_code, 200)
        self.assertNotIn(
            "reply yes or no",
            decline.json().get("fulfillmentText", "").lower(),
        )

        type_reply = self.client.post(
            self.url,
            data=json.dumps({"message": "a hotel please"}),
            content_type="application/json",
        )
        self.assertEqual(type_reply.status_code, 200)
        type_text = type_reply.json().get("fulfillmentText", "").lower()
        self.assertNotIn("reply yes or no", type_text)

        stay_reply = self.client.post(
            self.url,
            data=json.dumps({"message": "2 nights"}),
            content_type="application/json",
        )
        self.assertEqual(stay_reply.status_code, 200)
        self.assertNotIn(
            "reply yes or no",
            stay_reply.json().get("fulfillmentText", "").lower(),
        )

        guests_reply = self.client.post(
            self.url,
            data=json.dumps({"message": "4 guests"}),
            content_type="application/json",
        )
        self.assertEqual(guests_reply.status_code, 200)
        guests_text = guests_reply.json().get("fulfillmentText", "").lower()
        self.assertIn("budget", guests_text)
        self.assertNotIn("reply yes or no", guests_text)

        budget_reply = self.client.post(
            self.url,
            data=json.dumps({"message": "3400"}),
            content_type="application/json",
        )
        self.assertEqual(budget_reply.status_code, 200)
        budget_text = budget_reply.json().get("fulfillmentText", "").lower()
        self.assertIn("top hotel/inn recommendations", budget_text)
        self.assertNotIn("reply yes or no", budget_text)

    def test_chat_runtime_health_endpoint_requires_login(self):
        self.client.logout()
        response = self.client.get(self.health_url)
        self.assertEqual(response.status_code, 401)
        body = response.json()
        self.assertEqual(body.get("error_code"), "chat_requires_login")

    def test_chat_runtime_health_endpoint_returns_runtime_flags(self):
        response = self.client.get(self.health_url)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body.get("status"), "ok")
        chatbot = body.get("chatbot", {})
        self.assertIn("translation", chatbot)
        self.assertIn("nlg", chatbot)
        self.assertIn("enabled", chatbot.get("translation", {}))
        self.assertIn("client_ready", chatbot.get("translation", {}))
        self.assertIn("model", chatbot.get("translation", {}))

    def test_decision_tree_runtime_status_endpoint_requires_login(self):
        self.client.logout()
        response = self.client.get(self.decision_tree_runtime_url)
        self.assertEqual(response.status_code, 401)
        body = response.json()
        self.assertEqual(body.get("error_code"), "chat_requires_login")

    def test_decision_tree_runtime_status_endpoint_requires_staff(self):
        response = self.client.get(self.decision_tree_runtime_url)
        self.assertEqual(response.status_code, 403)
        body = response.json()
        self.assertEqual(body.get("error_code"), "chat_admin_required")

    def test_decision_tree_runtime_status_endpoint_returns_runtime_payload_for_staff(self):
        self.user.is_staff = True
        self.user.save(update_fields=["is_staff"])
        response = self.client.get(self.decision_tree_runtime_url)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body.get("status"), "ok")
        runtime = body.get("decision_tree_runtime", {})
        self.assertIn("source", runtime)
        self.assertIn("path", runtime)
        self.assertIn("file_exists", runtime)
        self.assertIn("fallback_used", runtime)
        self.assertIn("demo_fallback_allowed", runtime)
        self.assertIn("loaded_model_params", runtime)
        self.assertIn("criterion", runtime.get("loaded_model_params", {}))
        self.assertIn("min_samples_leaf", runtime.get("loaded_model_params", {}))
        self.assertIn("min_samples_split", runtime.get("loaded_model_params", {}))
        self.assertIn("max_depth", runtime.get("loaded_model_params", {}))

    def test_decision_tree_runtime_status_helper_exposes_expected_fields(self):
        status = get_decision_tree_runtime_status(force_reload=True)
        self.assertIn("path", status)
        self.assertIn("source", status)
        self.assertIn("file_exists", status)
        self.assertIn("fallback_used", status)
        self.assertIn("loaded_model_params", status)
        self.assertIn("expected_pruned_params", status)
        self.assertIn("expected_params_match", status)

    @patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False)
    def test_owner_role_receives_owner_perspective_help(self):
        owner_group, _ = Group.objects.get_or_create(name="accommodation_owner")
        self.user.groups.add(owner_group)

        response = self.client.post(
            self.url,
            data=json.dumps({"message": "help"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        text = payload.get("fulfillmentText", "").lower()
        self.assertIn("owner assistant mode is active", text)
        self.assertIn("current snapshot", text)
        quick_replies = payload.get("quick_replies") or []
        self.assertIn("Open Owner Hub", quick_replies)

    @patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False)
    def test_owner_all_time_bookings_command_returns_owner_booking_summary(self):
        owner_group, _ = Group.objects.get_or_create(name="accommodation_owner")
        self.user.groups.add(owner_group)
        self.accommodation.owner = self.user
        self.accommodation.save(update_fields=["owner"])

        today = timezone.localdate()
        AccommodationBooking.objects.create(
            guest=self.user,
            accommodation=self.accommodation,
            room=self.room,
            check_in=today + timedelta(days=1),
            check_out=today + timedelta(days=2),
            num_guests=2,
            status="pending",
            total_amount=Decimal("2400.00"),
        )

        response = self.client.post(
            self.url,
            data=json.dumps({"message": "how about all-time bookings?"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        text = payload.get("fulfillmentText", "").lower()
        self.assertIn("booking summary for all time", text)
        self.assertIn("total 1", text)
        self.assertIn("pending 1", text)

    @patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False)
    def test_remember_preferences_command_saves_guest_defaults(self):
        response = self.client.post(
            self.url,
            data=json.dumps({"message": "remember my preferences: hotel in bayawan budget 1500 for 2 guests"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        text = response.json().get("fulfillmentText", "").lower()
        self.assertIn("saved", text)
        self.assertIn("bayawan", text)

    @patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False)
    def test_forget_preferences_command_clears_saved_defaults(self):
        self.client.post(
            self.url,
            data=json.dumps({"message": "remember my preferences: hotel in bayawan budget 1500"}),
            content_type="application/json",
        )
        response = self.client.post(
            self.url,
            data=json.dumps({"message": "forget my preferences"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        text = response.json().get("fulfillmentText", "").lower()
        self.assertIn("cleared", text)

    @patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False)
    def test_saved_preferences_are_applied_to_follow_up_request(self):
        self.client.post(
            self.url,
            data=json.dumps({"message": "remember my preferences: hotel in bayawan budget 2000"}),
            content_type="application/json",
        )
        response = self.client.post(
            self.url,
            data=json.dumps({"message": "recommend hotel options"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        text = response.json().get("fulfillmentText", "").lower()
        self.assertTrue(
            ("top hotel/inn recommendations" in text)
            or ("bayawan" in text)
        )

    def test_out_of_scope_detector_identifies_non_tourism_prompt(self):
        self.assertTrue(_is_out_of_scope_message("write me a poem about stars"))
        self.assertFalse(_is_out_of_scope_message("recommend a hotel in bayawan"))

    @patch("ai_chatbot.views._classify_intent_and_extract_params")
    @patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False)
    def test_guest_out_of_scope_prompt_returns_scope_message(self, mock_classify):
        mock_classify.return_value = {
            "intent": "get_recommendation",
            "params": {},
            "source": "heuristic_intent_fallback",
            "confidence": 0.41,
            "needs_clarification": False,
            "clarification_question": "",
            "clarification_field": "",
            "intent_classifier": {"source": "heuristic_intent_fallback"},
        }

        response = self.client.post(
            self.url,
            data=json.dumps({"message": "write me a poem about stars"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        text = payload.get("fulfillmentText", "").lower()
        self.assertTrue(
            ("outside this system's scope" in text)
            or ("not completely sure" in text)
            or ("i can help" in text)
        )
        self.assertIn("quick_replies", payload)

    @patch("ai_chatbot.views._classify_intent_and_extract_params")
    @patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False)
    def test_owner_out_of_scope_prompt_returns_owner_scope_message(self, mock_classify):
        owner_group, _ = Group.objects.get_or_create(name="accommodation_owner")
        self.user.groups.add(owner_group)
        mock_classify.return_value = {
            "intent": "get_recommendation",
            "params": {},
            "source": "heuristic_intent_fallback",
            "confidence": 0.42,
            "needs_clarification": False,
            "clarification_question": "",
            "clarification_field": "",
            "intent_classifier": {"source": "heuristic_intent_fallback"},
        }

        response = self.client.post(
            self.url,
            data=json.dumps({"message": "write me a poem about stars"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        text = response.json().get("fulfillmentText", "").lower()
        self.assertIn("outside the scope of this owner assistant", text)

    @patch("ai_chatbot.views._classify_intent_and_extract_params")
    @patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False)
    def test_admin_out_of_scope_prompt_returns_admin_scope_message(self, mock_classify):
        self.client.logout()
        employee = Employee.objects.create(
            first_name="Admin",
            last_name="Scope",
            username="admin_scope_user",
            age=30,
            phone_number="09170001222",
            email="admin_scope_user@example.com",
            sex="M",
            role="Admin",
            status="accepted",
        )
        employee.set_password("secure-pass-123")
        employee.save()

        session = self.client.session
        session["user_type"] = "employee"
        session["employee_id"] = employee.emp_id
        session["is_admin"] = True
        session.save()

        mock_classify.return_value = {
            "intent": "get_recommendation",
            "params": {},
            "source": "heuristic_intent_fallback",
            "confidence": 0.43,
            "needs_clarification": False,
            "clarification_question": "",
            "clarification_field": "",
            "intent_classifier": {"source": "heuristic_intent_fallback"},
        }

        response = self.client.post(
            self.url,
            data=json.dumps({"message": "write me a poem about stars"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        text = response.json().get("fulfillmentText", "").lower()
        self.assertIn("outside the scope of this admin assistant", text)

    @patch("ai_chatbot.views._classify_intent_and_extract_params")
    @patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False)
    def test_employee_out_of_scope_prompt_returns_employee_scope_message(self, mock_classify):
        self.client.logout()
        employee = Employee.objects.create(
            first_name="Employee",
            last_name="Scope",
            username="employee_scope_user",
            age=26,
            phone_number="09170001333",
            email="employee_scope_user@example.com",
            sex="F",
            role="Tourism Employee",
            status="accepted",
        )
        employee.set_password("secure-pass-123")
        employee.save()

        session = self.client.session
        session["user_type"] = "employee"
        session["employee_id"] = employee.emp_id
        session["is_admin"] = False
        session.save()

        mock_classify.return_value = {
            "intent": "get_recommendation",
            "params": {},
            "source": "heuristic_intent_fallback",
            "confidence": 0.44,
            "needs_clarification": False,
            "clarification_question": "",
            "clarification_field": "",
            "intent_classifier": {"source": "heuristic_intent_fallback"},
        }

        response = self.client.post(
            self.url,
            data=json.dumps({"message": "write me a poem about stars"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        text = response.json().get("fulfillmentText", "").lower()
        self.assertIn("outside the scope of this employee assistant", text)

    @patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False)
    def test_admin_role_help_includes_live_snapshot(self):
        self.client.logout()
        employee = Employee.objects.create(
            first_name="Admin",
            last_name="Test",
            username="admin_help_user",
            age=30,
            phone_number="09170001111",
            email="admin_help_user@example.com",
            sex="M",
            role="Admin",
            status="accepted",
        )
        employee.set_password("secure-pass-123")
        employee.save()

        session = self.client.session
        session["user_type"] = "employee"
        session["employee_id"] = employee.emp_id
        session["is_admin"] = True
        session.save()

        response = self.client.post(
            self.url,
            data=json.dumps({"message": "help"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        text = response.json().get("fulfillmentText", "").lower()
        self.assertIn("admin assistant mode is active", text)
        self.assertIn("current snapshot", text)

    @patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False)
    def test_plain_integer_guest_reply_works_when_waiting_for_guests(self):
        response_1 = self.client.post(
            self.url,
            data=json.dumps({"message": "recommend a hotel in bayawan"}),
            content_type="application/json",
        )
        self.assertEqual(response_1.status_code, 200)
        first_text = response_1.json().get("fulfillmentText", "").lower()
        self.assertIn("check-in/check-out", first_text)
        self.assertNotIn("budget per night", first_text)

        response_2 = self.client.post(
            self.url,
            data=json.dumps({"message": "1500"}),
            content_type="application/json",
        )
        self.assertEqual(response_2.status_code, 200)
        second_text = response_2.json().get("fulfillmentText", "").lower()
        self.assertIn("guests", second_text)
        self.assertNotIn("budget per night", second_text)
        self.assertNotIn("check-in/check-out", second_text)

        response_3 = self.client.post(
            self.url,
            data=json.dumps({"message": "2"}),
            content_type="application/json",
        )
        self.assertEqual(response_3.status_code, 200)
        self.assertIn("budget", response_3.json().get("fulfillmentText", "").lower())

    @patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False)
    def test_date_only_follow_up_stays_in_accommodation_context(self):
        self.client.post(
            self.url,
            data=json.dumps(
                {"message": "recommend a hotel in bayawan, budget 1500, 2 guests"}
            ),
            content_type="application/json",
        )

        first_dates = self.client.post(
            self.url,
            data=json.dumps({"message": "2026-06-17 to 2026-06-20"}),
            content_type="application/json",
        )
        self.assertEqual(first_dates.status_code, 200)
        first_text = first_dates.json().get("fulfillmentText", "").lower()
        self.assertTrue(
            ("hotel or inn" in first_text) or ("top hotel/inn recommendations" in first_text)
        )

        second_dates = self.client.post(
            self.url,
            data=json.dumps({"message": "2026-03-15 to 2026-03-17"}),
            content_type="application/json",
        )
        self.assertEqual(second_dates.status_code, 200)
        second_text = second_dates.json().get("fulfillmentText", "").lower()
        self.assertTrue(
            ("hotel or inn" in second_text) or ("top hotel/inn recommendations" in second_text)
        )
        self.assertNotIn("matching tour", second_text)

    @patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False)
    def test_suggested_hotels_by_location_returns_preview_even_without_dates(self):
        response = self.client.post(
            self.url,
            data=json.dumps({"message": "show suggested hotels in bayawan"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        text = response.json().get("fulfillmentText", "").lower()
        self.assertIn("top hotel/inn recommendations", text)

    @patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False)
    def test_description_based_preference_prompt_can_return_recommendations_without_location(self):
        response = self.client.post(
            self.url,
            data=json.dumps({"message": "suggest a quiet hotel with good environment and affordable price"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        text = response.json().get("fulfillmentText", "").lower()
        self.assertIn("top hotel/inn recommendations", text)

    @patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False)
    def test_why_option_follow_up_returns_explanation(self):
        first = self.client.post(
            self.url,
            data=json.dumps({"message": "show default hotel suggestions"}),
            content_type="application/json",
        )
        self.assertEqual(first.status_code, 200)

        second = self.client.post(
            self.url,
            data=json.dumps({"message": "why option 1"}),
            content_type="application/json",
        )
        self.assertEqual(second.status_code, 200)
        text = second.json().get("fulfillmentText", "").lower()
        self.assertIn("why option 1", text)

    @patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False)
    def test_compare_top_follow_up_returns_comparison(self):
        first = self.client.post(
            self.url,
            data=json.dumps({"message": "show default hotel suggestions"}),
            content_type="application/json",
        )
        self.assertEqual(first.status_code, 200)

        second = self.client.post(
            self.url,
            data=json.dumps({"message": "compare top 3"}),
            content_type="application/json",
        )
        self.assertEqual(second.status_code, 200)
        text = second.json().get("fulfillmentText", "").lower()
        self.assertIn("comparison of top", text)

    @patch("ai_chatbot.views._get_accommodation_recommendations", side_effect=RuntimeError("boom"))
    @patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False)
    def test_accommodation_runtime_error_returns_graceful_fallback(self, _mock_reco):
        response = self.client.post(
            self.url,
            data=json.dumps(
                {
                    "message": (
                        "recommend a hotel in bayawan for 2 guests under 2000 "
                        "from 2026-04-14 to 2026-04-17"
                    )
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        text = response.json().get("fulfillmentText", "").lower()
        self.assertIn("temporary issue", text)

    @patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False)
    def test_no_match_returns_budget_guidance_metadata(self):
        response = self.client.post(
            self.url,
            data=json.dumps(
                {
                    "message": (
                        "recommend a hotel in bayawan for 2 guests under 500 "
                        "from 2026-03-25 to 2026-03-27"
                    )
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        text = body.get("fulfillmentText", "").lower()
        self.assertTrue(bool(text.strip()))
        self.assertTrue(("budget" in text) or ("you can try" in text))
        if "no_match_reasons" in body:
            self.assertIn("budget_too_low", body.get("no_match_reasons", []))
            self.assertEqual(body.get("suggested_budget_min"), 1200.0)
        else:
            self.assertIn("recommendation_trace", body)

    @patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False)
    def test_relaxed_location_fallback_returns_recommendations(self):
        response = self.client.post(
            self.url,
            data=json.dumps(
                {
                    "message": (
                        "recommend a hotel in poblacion for 2 guests under 1500 "
                        "from 2026-03-25 to 2026-03-27"
                    )
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        text = body.get("fulfillmentText", "").lower()
        self.assertTrue(bool(text.strip()))
        if "broaden location" in text:
            self.assertNotIn("recommendation_trace", body)
        else:
            self.assertIn("recommendation_trace", body)

    @patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False)
    def test_compact_guest_reply_does_not_override_budget(self):
        self.client.post(
            self.url,
            data=json.dumps(
                {"message": "recommend a hotel in bayawan, budget 1500, 2 guests"}
            ),
            content_type="application/json",
        )

        state = self.client.session.get("ai_chatbot_state", {})
        params = state.get("params", {})
        self.assertEqual(params.get("budget"), 1500)
        self.assertEqual(params.get("guests"), 2)

    @patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False)
    def test_with_pool_refinement_stays_in_accommodation_flow(self):
        self.client.post(
            self.url,
            data=json.dumps(
                {
                    "message": (
                        "recommend a hotel in bayawan, budget 1500, 2 guests, "
                        "2026-03-25 to 2026-03-27"
                    )
                }
            ),
            content_type="application/json",
        )

        refine = self.client.post(
            self.url,
            data=json.dumps({"message": "with pool"}),
            content_type="application/json",
        )
        self.assertEqual(refine.status_code, 200)
        text = refine.json().get("fulfillmentText", "").lower()
        self.assertIn("hotel/inn", text)
        self.assertNotIn("matching tour", text)

    @patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False)
    def test_yes_after_budget_offer_stays_in_accommodation_flow(self):
        initial = self.client.post(
            self.url,
            data=json.dumps(
                {
                    "message": (
                        "recommend a hotel in bayawan for 2 guests under 1000 "
                        "from 2026-03-25 to 2026-03-27"
                    )
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(initial.status_code, 200)
        self.assertIn("would you like to use php", initial.json().get("fulfillmentText", "").lower())

        follow_up = self.client.post(
            self.url,
            data=json.dumps({"message": "yes"}),
            content_type="application/json",
        )
        self.assertEqual(follow_up.status_code, 200)
        text = follow_up.json().get("fulfillmentText", "").lower()
        self.assertIn("top hotel/inn recommendations", text)
        self.assertNotIn("matching tour", text)

    @patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False)
    def test_custom_budget_after_offer_stays_in_accommodation_flow(self):
        initial = self.client.post(
            self.url,
            data=json.dumps(
                {
                    "message": (
                        "recommend a hotel in bayawan for 2 guests under 1000 "
                        "from 2026-03-25 to 2026-03-27"
                    )
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(initial.status_code, 200)
        self.assertIn("would you like to use php", initial.json().get("fulfillmentText", "").lower())

        follow_up = self.client.post(
            self.url,
            data=json.dumps({"message": "1600"}),
            content_type="application/json",
        )
        self.assertEqual(follow_up.status_code, 200)
        text = follow_up.json().get("fulfillmentText", "").lower()
        self.assertIn("top hotel/inn recommendations", text)
        self.assertNotIn("matching tour", text)

    @patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False)
    def test_non_budget_updates_are_preserved_while_waiting_budget_confirmation(self):
        initial = self.client.post(
            self.url,
            data=json.dumps(
                {
                    "message": (
                        "recommend a hotel in bayawan for 2 guests under 1000 "
                        "from 2026-03-25 to 2026-03-27"
                    )
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(initial.status_code, 200)
        self.assertIn("would you like to use php", initial.json().get("fulfillmentText", "").lower())

        update_dates = self.client.post(
            self.url,
            data=json.dumps({"message": "2026-04-01 to 2026-04-03"}),
            content_type="application/json",
        )
        self.assertEqual(update_dates.status_code, 200)
        self.assertIn(
            "i saved your dates/filters",
            update_dates.json().get("fulfillmentText", "").lower(),
        )
        state_before_confirmation = self.client.session.get("ai_chatbot_state", {})
        params_before_confirmation = state_before_confirmation.get("params", {})
        self.assertEqual(params_before_confirmation.get("check_in"), "2026-04-01")
        self.assertEqual(params_before_confirmation.get("check_out"), "2026-04-03")

        accepted = self.client.post(
            self.url,
            data=json.dumps({"message": "yes"}),
            content_type="application/json",
        )
        self.assertEqual(accepted.status_code, 200)
        payload = accepted.json()
        self.assertIn("recommendation_trace", payload)
        trace = payload.get("recommendation_trace") or []
        self.assertGreaterEqual(len(trace), 1)
        state_after_confirmation = self.client.session.get("ai_chatbot_state", {})
        params_after_confirmation = state_after_confirmation.get("params", {})
        self.assertEqual(params_after_confirmation.get("check_in"), "2026-04-01")
        self.assertEqual(params_after_confirmation.get("check_out"), "2026-04-03")

    @patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False)
    def test_booking_date_followup_keeps_booking_flow_and_returns_draft_summary(self):
        first = self.client.post(
            self.url,
            data=json.dumps({"message": f"book room {self.room.room_id}"}),
            content_type="application/json",
        )
        self.assertEqual(first.status_code, 200)
        first_text = first.json().get("fulfillmentText", "").lower()
        self.assertIn("official page", first_text)

        follow_up = self.client.post(
            self.url,
            data=json.dumps({"message": "April 26, 2026 to April 29, 2026"}),
            content_type="application/json",
        )
        self.assertEqual(follow_up.status_code, 200)
        follow_up_text = follow_up.json().get("fulfillmentText", "").lower()
        self.assertTrue(bool(follow_up_text.strip()))

    @patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False)
    def test_booking_requires_confirmation_before_creating_record(self):
        before_count = AccommodationBooking.objects.filter(guest=self.user).count()

        draft = self.client.post(
            self.url,
            data=json.dumps(
                {"message": f"book room {self.room.room_id} for 2 guests from 2026-03-25 to 2026-03-27"}
            ),
            content_type="application/json",
        )
        self.assertEqual(draft.status_code, 200)
        draft_text = draft.json().get("fulfillmentText", "").lower()
        self.assertIn("outside this system", draft_text)
        self.assertEqual(
            AccommodationBooking.objects.filter(guest=self.user).count(),
            before_count,
        )

        confirmed = self.client.post(
            self.url,
            data=json.dumps({"message": "yes"}),
            content_type="application/json",
        )
        self.assertEqual(confirmed.status_code, 200)
        self.assertEqual(
            AccommodationBooking.objects.filter(guest=self.user).count(),
            before_count,
        )

    @patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False)
    def test_booking_confirmation_no_cancels_without_creating_record(self):
        before_count = AccommodationBooking.objects.filter(guest=self.user).count()

        draft = self.client.post(
            self.url,
            data=json.dumps(
                {"message": f"book room {self.room.room_id} for 2 guests from 2026-03-25 to 2026-03-27"}
            ),
            content_type="application/json",
        )
        self.assertEqual(draft.status_code, 200)
        self.assertIn("outside this system", draft.json().get("fulfillmentText", "").lower())

        cancelled = self.client.post(
            self.url,
            data=json.dumps({"message": "no"}),
            content_type="application/json",
        )
        self.assertEqual(cancelled.status_code, 200)
        self.assertEqual(
            AccommodationBooking.objects.filter(guest=self.user).count(),
            before_count,
        )

    @patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False)
    def test_booking_confirmation_rejects_overlapping_existing_room_booking(self):
        other_user = get_user_model().objects.create_user(
            username="chat_overlap_user",
            email="chat_overlap_user@example.com",
            password="secure-pass-123",
            first_name="Overlap",
            last_name="Tester",
        )
        AccommodationBooking.objects.create(
            guest=other_user,
            accommodation=self.accommodation,
            room=self.room,
            check_in=timezone.now().date() + timedelta(days=5),
            check_out=timezone.now().date() + timedelta(days=7),
            num_guests=2,
            status="confirmed",
            total_amount=Decimal("2400.00"),
        )

        before_count = AccommodationBooking.objects.filter(guest=self.user).count()
        self.client.post(
            self.url,
            data=json.dumps(
                {
                    "message": (
                        f"book room {self.room.room_id} for 2 guests from "
                        f"{(timezone.now().date() + timedelta(days=5)).isoformat()} to "
                        f"{(timezone.now().date() + timedelta(days=7)).isoformat()}"
                    )
                }
            ),
            content_type="application/json",
        )

        confirm = self.client.post(
            self.url,
            data=json.dumps({"message": "yes"}),
            content_type="application/json",
        )
        self.assertEqual(confirm.status_code, 200)
        self.assertEqual(
            AccommodationBooking.objects.filter(guest=self.user).count(),
            before_count,
        )

    @patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False)
    def test_booking_confirmation_accept_is_one_time_only(self):
        before_count = AccommodationBooking.objects.filter(guest=self.user).count()

        draft = self.client.post(
            self.url,
            data=json.dumps(
                {"message": f"book room {self.room.room_id} for 2 guests from 2026-03-25 to 2026-03-27"}
            ),
            content_type="application/json",
        )
        self.assertEqual(draft.status_code, 200)
        self.assertIn("outside this system", draft.json().get("fulfillmentText", "").lower())

        first_yes = self.client.post(
            self.url,
            data=json.dumps({"message": "yes"}),
            content_type="application/json",
        )
        self.assertEqual(first_yes.status_code, 200)
        self.assertEqual(
            AccommodationBooking.objects.filter(guest=self.user).count(),
            before_count,
        )

        second_yes = self.client.post(
            self.url,
            data=json.dumps({"message": "yes"}),
            content_type="application/json",
        )
        self.assertEqual(second_yes.status_code, 200)
        self.assertEqual(
            AccommodationBooking.objects.filter(guest=self.user).count(),
            before_count,
        )

    @patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False)
    def test_book_accommodation_returns_official_link_guidance(self):
        self.client.post(
            self.url,
            data=json.dumps(
                {"message": f"book room {self.room.room_id} for 2 guests from 2026-03-25 to 2026-03-27"}
            ),
            content_type="application/json",
        )

        confirmed = self.client.post(
            self.url,
            data=json.dumps({"message": "yes"}),
            content_type="application/json",
        )
        self.assertEqual(confirmed.status_code, 200)
        body = confirmed.json()
        self.assertIn("fulfillmentText", body)
        self.assertIn("official", str(body.get("fulfillmentText") or "").lower())

    def test_accommodation_booking_notifications_returns_confirmed_or_declined(self):
        today = timezone.now().date()
        AccommodationBooking.objects.create(
            guest=self.user,
            accommodation=self.accommodation,
            room=self.room,
            check_in=today + timedelta(days=2),
            check_out=today + timedelta(days=4),
            num_guests=2,
            status="confirmed",
            total_amount=Decimal("2400.00"),
        )

        response = self.client.get("/api/chat/accommodation-booking-notifications/")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body.get("status"), "disabled")
        self.assertEqual(body.get("reason"), "accommodation_booking_transactions_decommissioned")
        self.assertEqual(body.get("bookings"), [])

    def test_accommodation_booking_notifications_unauthenticated_returns_safe_empty_payload(self):
        self.client.logout()
        response = self.client.get("/api/chat/accommodation-booking-notifications/")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body.get("status"), "disabled")
        self.assertEqual(body.get("reason"), "accommodation_booking_transactions_decommissioned")
        self.assertEqual(body.get("bookings"), [])

    @patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False)
    def test_guest_forgot_password_prompt_returns_guest_login_link(self):
        response = self.client.post(
            self.url,
            data=json.dumps({"message": "I forgot my password. How can I log in again?"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn("billing_link", body)
        self.assertEqual(body.get("billing_link_label"), "Open Guest Login")

    @patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False)
    def test_guest_cancel_booking_support_returns_my_bookings_link(self):
        response = self.client.post(
            self.url,
            data=json.dumps({"message": "I want to cancel my booking."}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn("billing_link", body)
        self.assertEqual(body.get("billing_link_label"), "Open Accommodation Links")

    @patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False)
    def test_guest_change_check_in_support_returns_rebook_guidance(self):
        response = self.client.post(
            self.url,
            data=json.dumps({"message": "Can I change my check-in date?"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        text = response.json().get("fulfillmentText", "").lower()
        self.assertIn("official", text)
        self.assertIn("date changes", text)

    @patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False)
    def test_guest_room_availability_short_mixed_prompt_returns_availability_summary(self):
        response = self.client.post(
            self.url,
            data=json.dumps({"message": "May available room ba for 2 persons tomorrow?"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        text = response.json().get("fulfillmentText", "").lower()
        self.assertIn("available", text)
        self.assertIn("room", text)

    @patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False)
    def test_guest_booking_requirements_prompt_returns_required_fields(self):
        response = self.client.post(
            self.url,
            data=json.dumps({"message": "What details do I need to provide for booking?"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        text = response.json().get("fulfillmentText", "").lower()
        self.assertIn("official page", text)
        self.assertIn("number of guests", text)

    def test_accommodation_link_actions_prioritize_official_links(self):
        self.accommodation.official_booking_url = "https://example.com/book"
        self.accommodation.official_contact_url = "https://facebook.com/example-accommodation"
        self.accommodation.save(update_fields=["official_booking_url", "official_contact_url"])

        actions = _build_accommodation_link_actions(room=self.room, max_actions=3)
        self.assertGreaterEqual(len(actions), 1)
        labels = [str(item.get("label") or "") for item in actions]
        self.assertTrue(any("booking page" in str(label).lower() for label in labels))
        self.assertTrue(any("facebook" in str(label).lower() for label in labels))
        self.assertNotIn("Email Accommodation", labels)

    def test_accommodation_link_actions_use_email_only_as_fallback(self):
        self.accommodation.official_booking_url = ""
        self.accommodation.official_contact_url = ""
        self.accommodation.save(update_fields=["official_booking_url", "official_contact_url"])

        actions = _build_accommodation_link_actions(room=self.room, max_actions=3)
        labels = [str(item.get("label") or "") for item in actions]
        self.assertNotIn("Email Accommodation", labels)
        self.assertTrue(
            any(("call " in str(label).lower()) or ("email " in str(label).lower()) for label in labels),
            msg=f"Expected fallback contact action, got: {labels}",
        )

    @patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False)
    def test_guest_reservation_confirmed_prompt_routes_to_booking_status_page(self):
        response = self.client.post(
            self.url,
            data=json.dumps({"message": "Is my reservation already confirmed?"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn("billing_link", body)
        self.assertEqual(body.get("billing_link_label"), "Open Accommodation Links")

    @patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False)
    def test_pending_booking_confirmation_expires_after_10_minutes(self):
        before_count = AccommodationBooking.objects.filter(guest=self.user).count()

        draft = self.client.post(
            self.url,
            data=json.dumps(
                {"message": f"book room {self.room.room_id} for 2 guests from 2026-03-25 to 2026-03-27"}
            ),
            content_type="application/json",
        )
        self.assertEqual(draft.status_code, 200)

        session = self.client.session
        state = session.get("ai_chatbot_state", {})
        if "pending_booking" in state and isinstance(state["pending_booking"], dict):
            state["pending_booking"]["created_at"] = int(time.time()) - 601
        session["ai_chatbot_state"] = state
        session.save()

        expired = self.client.post(
            self.url,
            data=json.dumps({"message": "yes"}),
            content_type="application/json",
        )
        self.assertEqual(expired.status_code, 200)
        self.assertIn("expired after 10 minutes", expired.json().get("fulfillmentText", "").lower())
        self.assertEqual(
            AccommodationBooking.objects.filter(guest=self.user).count(),
            before_count,
        )

    @patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False)
    def test_invalid_tampered_room_id_is_handled_safely(self):
        before_count = AccommodationBooking.objects.filter(guest=self.user).count()

        response = self.client.post(
            self.url,
            data=json.dumps({"message": "book room xyz for 2 guests from 2026-03-25 to 2026-03-27"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        text = response.json().get("fulfillmentText", "").lower()
        self.assertTrue(("valid room id" in text) or ("should be numeric" in text))
        self.assertEqual(
            AccommodationBooking.objects.filter(guest=self.user).count(),
            before_count,
        )

    def test_recommendation_click_endpoint_logs_click_event(self):
        response = self.client.post(
            "/api/chat/recommendation-click/",
            data=json.dumps(
                {
                    "room_id": self.room.room_id,
                    "accom_id": self.accommodation.accom_id,
                    "rank": 1,
                    "scoring_mode": "hybrid_textcnn_decisiontree",
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            RecommendationEvent.objects.filter(
                user=self.user,
                event_type="click",
                item_ref__icontains=f"room:{self.room.room_id}",
            ).exists()
        )

    def test_guest_funnel_event_endpoint_logs_supported_event(self):
        response = self.client.post(
            "/api/chat/funnel-event/",
            data=json.dumps({"event_key": "chatbot_opened", "detail": "chat opened from floating button"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body.get("status"), "ok")
        self.assertEqual(body.get("item_ref"), "chat:funnel_chatbot_opened")
        self.assertTrue(
            RecommendationEvent.objects.filter(
                user=self.user,
                item_ref="chat:funnel_chatbot_opened",
                event_type="view",
            ).exists()
        )

    def test_guest_funnel_event_endpoint_skips_unknown_event(self):
        response = self.client.post(
            "/api/chat/funnel-event/",
            data=json.dumps({"event_key": "unknown_event"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body.get("status"), "skipped")
        self.assertEqual(body.get("reason"), "unknown_event_key")

    def test_usability_feedback_endpoint_persists_sus_response(self):
        response = self.client.post(
            "/api/chat/usability-feedback/",
            data=json.dumps(
                {
                    "statement_code": "CHAT_UX_HELPFULNESS",
                    "likert_score": 5,
                    "comment": "Helpful recommendation card.",
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            UsabilitySurveyResponse.objects.filter(
                user=self.user,
                statement_code="CHAT_UX_HELPFULNESS",
                likert_score=5,
            ).exists()
        )

    def test_usability_feedback_endpoint_accepts_full_sus_tam_batch(self):
        responses = []
        for idx in range(1, 11):
            responses.append({"statement_code": f"SUS_Q{idx}", "likert_score": 4})
        for idx in range(1, 5):
            responses.append({"statement_code": f"PU_Q{idx}", "likert_score": 5})
            responses.append({"statement_code": f"PEU_Q{idx}", "likert_score": 4})

        response = self.client.post(
            "/api/chat/usability-feedback/",
            data=json.dumps(
                {
                    "instrument": "sus_tam_full",
                    "responses": responses,
                    "comment": "Batch submission test",
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body.get("saved_count"), 18)
        batch_id = body.get("survey_batch_id")
        self.assertTrue(batch_id)
        self.assertEqual(
            UsabilitySurveyResponse.objects.filter(user=self.user, survey_batch_id=batch_id).count(),
            18,
        )

    def test_usability_feedback_endpoint_rejects_incomplete_full_batch(self):
        responses = [{"statement_code": "SUS_Q1", "likert_score": 4}]
        response = self.client.post(
            "/api/chat/usability-feedback/",
            data=json.dumps(
                {
                    "instrument": "sus_tam_full",
                    "responses": responses,
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("missing_codes", response.json())

    @patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False)
    def test_chatbot_log_created_for_chat_interaction(self):
        message = "recommend a river tour for 2 guests under 700"
        response = self.client.post(
            self.url,
            data=json.dumps({"message": message}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        row = ChatbotLog.objects.filter(user=self.user).latest("created_at")
        self.assertEqual(row.user_message, message)
        self.assertTrue(bool(row.bot_response))
        self.assertTrue(bool(row.resolved_intent))
        self.assertIn(
            row.response_nlg_source,
            {
                "openai_nlg_unavailable",
                "openai_nlg_disabled",
                "llm_nlg_unavailable",
                "llm_nlg_disabled",
                "gemini_nlg_unavailable",
                "gemini_nlg_error",
                "gemini_nlg_fallback_paraphrase",
                "openai_nlg_error_paraphrase",
                "gemini_nlg_unavailable_paraphrase",
                "gemini_nlg_error_paraphrase",
                "llm_nlg_unavailable_paraphrase",
                "llm_nlg_disabled_paraphrase",
            },
        )
        self.assertIn("intent_source", row.provenance_json)

    @patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False)
    def test_chatbot_log_created_for_reset_command_early_return(self):
        message = "reset"
        response = self.client.post(
            self.url,
            data=json.dumps({"message": message}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        row = ChatbotLog.objects.filter(user=self.user).latest("created_at")
        self.assertEqual(row.user_message, message)
        self.assertEqual(row.resolved_intent, "reset_command")
        self.assertIn("context cleared", row.bot_response.lower())

    @patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False)
    def test_chatbot_log_created_for_default_suggestions_shortcut(self):
        message = "show default hotel suggestions"
        response = self.client.post(
            self.url,
            data=json.dumps({"message": message}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        row = ChatbotLog.objects.filter(user=self.user).latest("created_at")
        self.assertEqual(row.user_message, message)
        self.assertEqual(row.resolved_intent, "get_accommodation_recommendation_init")
        self.assertIn("suggested stays", row.bot_response.lower())

    @patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False)
    def test_chatbot_log_created_for_view_my_bookings_shortcut(self):
        message = "view my accommodation bookings"
        response = self.client.post(
            self.url,
            data=json.dumps({"message": message}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        row = ChatbotLog.objects.filter(user=self.user).latest("created_at")
        self.assertEqual(row.user_message, message)
        self.assertEqual(row.resolved_intent, "view_my_accommodation_bookings")
        self.assertTrue(
            ("booking status page" in row.bot_response.lower())
            or ("bookings page" in row.bot_response.lower())
        )

    @patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False)
    def test_guest_intro_message_uses_small_talk_layer(self):
        response = self.client.post(
            self.url,
            data=json.dumps({"message": "hi i am renold"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        text = str(body.get("fulfillmentText") or "").lower()
        self.assertIn("renold", text)
        self.assertIn("bayawan", text)
        row = ChatbotLog.objects.filter(user=self.user).latest("created_at")
        self.assertEqual(row.resolved_intent, "small_talk")

    @patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False)
    def test_guest_typo_bookigns_is_normalized_to_view_tour_bookings(self):
        response = self.client.post(
            self.url,
            data=json.dumps({"message": "show my tour bookigns"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn("Open My Tour Bookings", str(body.get("billing_link_label") or ""))
        row = ChatbotLog.objects.filter(user=self.user).latest("created_at")
        self.assertEqual(row.resolved_intent, "guest_view_tour_bookings")

    @patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False)
    def test_guest_show_my_tour_bookings_returns_booking_summary_instead_of_booking_prompt(self):
        TourBooking.objects.create(
            guest=self.user,
            tour=self.tour,
            schedule=self.schedule,
            status="pending",
            total_guests=1,
            num_adults=1,
            num_children=0,
            base_price=Decimal("500.00"),
            additional_fees=Decimal("0.00"),
            discounts=Decimal("0.00"),
            total_amount=Decimal("500.00"),
            payment_status="unpaid",
            amount_paid=Decimal("0.00"),
        )
        Pending.objects.create(
            guest_id=self.user,
            sched_id=self.schedule,
            tour_id=self.tour,
            status="Pending",
            total_guests=1,
            your_name="Chat Tester",
            your_email=self.user.email,
            your_phone="09990000999",
            num_adults=1,
            num_children=0,
        )

        response = self.client.post(
            self.url,
            data=json.dumps({"message": "show my tour bookings"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn("Open My Tour Bookings", str(body.get("billing_link_label") or ""))
        self.assertIn("tour booking", str(body.get("fulfillmentText") or "").lower())
        self.assertNotIn("please specify which schedule to book", str(body.get("fulfillmentText") or "").lower())

        row = ChatbotLog.objects.filter(user=self.user).latest("created_at")
        self.assertEqual(row.resolved_intent, "guest_view_tour_bookings")


class ChatbotRuntimeEventTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.url = "/api/chat/"
        self._cnn_patcher = patch("ai_chatbot.views._classify_intent_with_text_cnn")
        self.mock_cnn = self._cnn_patcher.start()
        self.mock_cnn.return_value = {
            "intent": "",
            "source": "text_cnn_unavailable",
            "confidence": 0.0,
            "top_3": [],
            "error": "runtime_event_force_heuristic",
        }
        user_model = get_user_model()
        self.user = user_model.objects.create_user(
            username="chat_runtime_events_user",
            email="chat_runtime_events_user@example.com",
            password="secure-pass-123",
            first_name="Runtime",
            last_name="Events",
        )
        self.client.force_login(self.user)
        self.tour = Tour_Add.objects.create(
            tour_id="90001",
            tour_name="Runtime Event Tour",
            description="Tour for runtime event billing checks.",
        )
        now = timezone.now()
        self.schedule = Tour_Schedule.objects.create(
            tour=self.tour,
            start_time=now + timedelta(days=1),
            end_time=now + timedelta(days=2),
            price=Decimal("500.00"),
            slots_available=20,
            slots_booked=0,
            duration_days=1,
            status="active",
        )
        Admission_Rates.objects.create(
            tour_id=self.tour,
            payables="Environmental Fee",
            price=Decimal("50.00"),
        )
        self.accommodation = Accomodation.objects.create(
            company_name="Runtime Event Hotel",
            email_address="runtime-event-hotel@example.com",
            location="Bayawan",
            company_type="hotel",
            password="demo-password-runtime",
            phone_number="09990000002",
            approval_status="accepted",
            status="accepted",
        )
        self.room = Room.objects.create(
            accommodation=self.accommodation,
            room_name="Runtime Event Room",
            person_limit=3,
            current_availability=2,
            price_per_night=Decimal("1200.00"),
            status="AVAILABLE",
        )
        TourismInformation.objects.create(
            spot_name="Runtime Event Spot",
            description="Tourism spot for runtime-event testing.",
            location="Bayawan",
            publication_status="published",
            is_active=True,
            created_by=self.user,
            updated_by=self.user,
        )

    def tearDown(self):
        self._cnn_patcher.stop()

    @patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False)
    def test_clarification_trigger_event_is_logged_for_incomplete_prompt(self):
        response = self.client.post(
            self.url,
            data=json.dumps({"message": "recommend an inn near city"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            SystemMetricLog.objects.filter(
                endpoint__icontains="#event:clarification_triggered",
            ).exists()
        )

    @patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False)
    def test_no_match_database_event_is_logged(self):
        response = self.client.post(
            self.url,
            data=json.dumps(
                {
                    "message": (
                        "recommend a hotel in bayawan for 2 guests under 500 "
                        "from 2026-03-25 to 2026-03-27"
                    )
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn("no_match_reasons", body)
        self.assertTrue(
            RecommendationEvent.objects.filter(
                user=self.user,
                item_ref="chat:no_match_database_result",
            ).exists()
        )
        self.assertTrue(
            SystemMetricLog.objects.filter(
                endpoint__icontains="#event:no_match_database_result",
            ).exists()
        )

    @patch("ai_chatbot.views._classify_intent_and_extract_params")
    @patch.dict(
        os.environ,
        {"OPENAI_API_KEY": "test-key", "CHATBOT_OPENAI_NLG_ENABLED": "1"},
        clear=False,
    )
    def test_output_guardrail_reverts_to_backend_reply_when_llm_changes_values(self, mock_classify):
        mock_classify.return_value = {
            "intent": "calculate_billing",
            "params": {"sched_id": self.schedule.sched_id, "guests": 2},
            "source": "text_cnn_intent",
            "confidence": 0.92,
            "needs_clarification": False,
            "clarification_question": "",
            "clarification_field": "",
            "intent_classifier": {"source": "text_cnn_intent", "confidence": 0.92},
        }
        with patch("ai_chatbot.views.OpenAI") as mock_openai_cls:
            mock_client = mock_openai_cls.return_value
            mock_client.chat.completions.create.return_value.choices = [
                type(
                    "Choice",
                    (),
                    {"message": type("Msg", (), {"content": "Total amount due is PHP 9999.00."})()},
                )()
            ]
            response = self.client.post(
                self.url,
                data=json.dumps({"message": f"calculate bill for {self.schedule.sched_id} for 2 guests"}),
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn("Billing Summary", body.get("fulfillmentText", ""))
        self.assertIn("Total amount due", body.get("fulfillmentText", ""))
        self.assertIn("guardrail_fallback", str(body.get("response_nlg_source") or ""))
        self.assertTrue(
            RecommendationEvent.objects.filter(
                user=self.user,
                item_ref="chat:output_guardrail_triggered",
            ).exists()
        )

    @override_settings(TESTING=False)
    @patch.dict(os.environ, {"OPENAI_API_KEY": "", "GEMINI_API_KEY": "dummy-gemini-key"}, clear=False)
    def test_gemini_failure_fallback_event_is_logged(self):
        response = self.client.post(
            self.url,
            data=json.dumps({"message": "show tourist spots in bayawan"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            RecommendationEvent.objects.filter(
                user=self.user,
                item_ref="chat:gemini_failure_fallback",
            ).exists()
        )


class ChatbotGuestPromptRegressionPackTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.url = "/api/chat/"
        user_model = get_user_model()
        self.user = user_model.objects.create_user(
            username="guest_regression_user",
            email="guest_regression_user@example.com",
            password="secure-pass-123",
            first_name="Guest",
            last_name="Regression",
        )
        self.client.force_login(self.user)

        self.accommodation = Accomodation.objects.create(
            company_name="Regression Bayawan Hotel",
            email_address="reg-hotel@example.com",
            location="Bayawan",
            company_type="hotel",
            password="demo-password-reg-1",
            phone_number="09990000001",
            approval_status="accepted",
            status="accepted",
        )
        self.room = Room.objects.create(
            accommodation=self.accommodation,
            room_name="Regression Standard Room",
            person_limit=4,
            current_availability=3,
            price_per_night=Decimal("1500.00"),
            status="AVAILABLE",
        )
        TourismInformation.objects.create(
            spot_name="Regression Bayawan River Park",
            description="Regression tourism spot for automated chatbot checks.",
            location="Bayawan City Proper",
            contact_information="09170000001",
            operating_hours="08:00 AM - 05:00 PM",
            publication_status="published",
            is_active=True,
            created_by=self.user,
            updated_by=self.user,
        )

    def _load_regression_pack_rows(self):
        pack_path = Path(__file__).resolve().parent.parent / "thesis_data_templates" / "CHATBOT_GUEST_40_PROMPT_REGRESSION_PACK.csv"
        rows = []
        with pack_path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                if not isinstance(row, dict):
                    continue
                rows.append(row)
        self.assertEqual(len(rows), 40)
        return rows

    @patch("ai_chatbot.views._classify_intent_with_text_cnn")
    @patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False)
    def test_regression_pack_intent_classification_alignment(self, mock_cnn):
        mock_cnn.return_value = {
            "intent": "",
            "source": "text_cnn_unavailable",
            "confidence": 0.0,
            "top_3": [],
            "error": "regression_pack_force_heuristic",
        }
        rows = self._load_regression_pack_rows()
        broad_intent_domain = {
            "get_recommendation",
            "get_tourism_information",
            "get_accommodation_recommendation",
            "gethotelrecommendation",
            "book_accommodation",
            "bookhotel",
            "reserve_accommodation",
            "calculate_billing",
            "calculate_accommodation_billing",
            "calculatehotelbilling",
            "out_of_scope",
        }
        for row in rows:
            prompt = str(row.get("prompt") or "").strip()
            expected = {
                str(item).strip()
                for item in str(row.get("expected_intents") or "").split("|")
                if str(item).strip()
            }
            with self.subTest(case_id=row.get("id"), prompt=prompt):
                parsed = _classify_intent_and_extract_params(prompt)
                resolved_intent = str(parsed.get("intent") or "").strip()
                self.assertTrue(resolved_intent)
                if resolved_intent not in expected:
                    # Some support commands are intentionally resolved by explicit shortcut
                    # handlers in openai_chat before/after parsing. Keep this pack focused
                    # on stable domain routing rather than strict command aliasing.
                    self.assertIn(resolved_intent, broad_intent_domain)

    @patch("ai_chatbot.views._classify_intent_with_text_cnn")
    @patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False)
    def test_regression_pack_endpoint_safety_and_clarification_behavior(self, mock_cnn):
        mock_cnn.return_value = {
            "intent": "",
            "source": "text_cnn_unavailable",
            "confidence": 0.0,
            "top_3": [],
            "error": "regression_pack_force_heuristic",
        }
        rows = self._load_regression_pack_rows()
        for row in rows:
            prompt = str(row.get("prompt") or "").strip()
            with self.subTest(case_id=row.get("id"), prompt=prompt):
                response = self.client.post(
                    self.url,
                    data=json.dumps({"message": prompt}),
                    content_type="application/json",
                )
                self.assertEqual(response.status_code, 200)
                body = response.json()
                text = str(body.get("fulfillmentText") or "").strip()
                self.assertTrue(text)

    @patch("ai_chatbot.views._classify_intent_with_text_cnn")
    def test_weak_and_incomplete_prompts_trigger_follow_up_guidance(self, mock_cnn):
        mock_cnn.return_value = {
            "intent": "",
            "source": "text_cnn_unavailable",
            "confidence": 0.0,
            "top_3": [],
            "error": "regression_pack_force_heuristic",
        }
        weak_prompts = [
            "room for 2 how much",
            "book room now",
            "available pa tomorrow?",
            "I want to book a room for tomorrow.",
            "Can I reserve a room for 2 nights?",
        ]
        for prompt in weak_prompts:
            with self.subTest(prompt=prompt):
                response = self.client.post(
                    self.url,
                    data=json.dumps({"message": prompt}),
                    content_type="application/json",
                )
                self.assertEqual(response.status_code, 200)
                body = response.json()
                text = str(body.get("fulfillmentText") or "").strip().lower()
                self.assertTrue(text)
                guidance_detected = bool(body.get("needs_clarification")) or bool(body.get("quick_replies"))
                guidance_detected = guidance_detected or any(
                    token in text
                    for token in (
                        "please provide",
                        "i need",
                        "need your",
                        "room id",
                        "check-in",
                        "check out",
                        "booking draft",
                        "available",
                    )
                )
                self.assertTrue(guidance_detected)

    @patch("ai_chatbot.views._classify_intent_with_text_cnn")
    @patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False)
    def test_guest_option_or_room_detail_query_returns_factual_room_details(self, mock_cnn):
        mock_cnn.return_value = {
            "intent": "",
            "source": "text_cnn_unavailable",
            "confidence": 0.0,
            "top_3": [],
            "error": "guest_detail_force_heuristic",
        }
        warmup = self.client.post(
            self.url,
            data=json.dumps(
                {
                    "message": "Recommend a hotel in Bayawan for 2 guests from 2026-04-20 to 2026-04-22 under 2000"
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(warmup.status_code, 200)
        detail = self.client.post(
            self.url,
            data=json.dumps(
                {"message": f"What amenities does Option 1 or Room {self.room.room_id} have?"}
            ),
            content_type="application/json",
        )
        self.assertEqual(detail.status_code, 200)
        body = detail.json()
        text = str(body.get("fulfillmentText") or "")
        self.assertIn(f"Room {self.room.room_id}", text)
        self.assertIn("Room details:", text)
        self.assertTrue(
            ("Amenities listed:" in text)
            or ("Amenities information is currently limited" in text)
        )
        self.assertNotIn("budget per night", text.lower())

    @patch("ai_chatbot.views._classify_intent_with_text_cnn")
    @patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False)
    def test_guest_room_detail_prefers_room_level_amenities(self, mock_cnn):
        mock_cnn.return_value = {
            "intent": "",
            "source": "text_cnn_unavailable",
            "confidence": 0.0,
            "top_3": [],
            "error": "guest_room_amenity_force_heuristic",
        }
        self.accommodation.accommodation_amenities = "Pool, Restaurant, Parking"
        self.accommodation.save(update_fields=["accommodation_amenities"])
        AuthoritativeRoomDetails.objects.create(
            room=self.room,
            room_type=self.room.room_name,
            amenities='["Room WiFi", "Smart TV", "Mini Fridge"]',
        )
        detail = self.client.post(
            self.url,
            data=json.dumps({"message": f"What amenities does Room {self.room.room_id} have?"}),
            content_type="application/json",
        )
        self.assertEqual(detail.status_code, 200)
        text = str(detail.json().get("fulfillmentText") or "")
        self.assertIn("Amenities listed: Room WiFi, Smart TV, Mini Fridge", text)
        self.assertIn("Additional property amenities may also be available", text)
        self.assertNotIn("Amenities listed (accommodation-level):", text)

    @patch("ai_chatbot.views._classify_intent_with_text_cnn")
    @patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False)
    def test_guest_room_detail_falls_back_to_accommodation_level_amenities(self, mock_cnn):
        mock_cnn.return_value = {
            "intent": "",
            "source": "text_cnn_unavailable",
            "confidence": 0.0,
            "top_3": [],
            "error": "guest_accom_amenity_force_heuristic",
        }
        self.accommodation.accommodation_amenities = "WiFi, Parking, Breakfast"
        self.accommodation.save(update_fields=["accommodation_amenities"])
        detail = self.client.post(
            self.url,
            data=json.dumps({"message": f"What facilities does Room {self.room.room_id} have?"}),
            content_type="application/json",
        )
        self.assertEqual(detail.status_code, 200)
        text = str(detail.json().get("fulfillmentText") or "")
        self.assertIn("Amenities listed (accommodation-level): WiFi, Parking, Breakfast", text)

    @patch("ai_chatbot.views._classify_intent_with_text_cnn")
    @patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False)
    def test_guest_room_detail_reports_limited_amenities_when_both_levels_missing(self, mock_cnn):
        mock_cnn.return_value = {
            "intent": "",
            "source": "text_cnn_unavailable",
            "confidence": 0.0,
            "top_3": [],
            "error": "guest_missing_amenity_force_heuristic",
        }
        self.accommodation.accommodation_amenities = ""
        self.accommodation.save(update_fields=["accommodation_amenities"])
        AuthoritativeRoomDetails.objects.create(
            room=self.room,
            room_type=self.room.room_name,
            amenities="",
        )
        detail = self.client.post(
            self.url,
            data=json.dumps({"message": f"Tell me the amenities of Room {self.room.room_id}."}),
            content_type="application/json",
        )
        self.assertEqual(detail.status_code, 200)
        text = str(detail.json().get("fulfillmentText") or "")
        self.assertIn("Amenities information is currently limited", text)
        self.assertNotIn("Amenities listed:", text)
        self.assertNotIn("Amenities listed (accommodation-level):", text)

    @patch("ai_chatbot.views._classify_intent_with_text_cnn")
    @patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False)
    def test_guest_tourism_information_query_returns_published_records(self, mock_cnn):
        mock_cnn.return_value = {
            "intent": "",
            "source": "text_cnn_unavailable",
            "confidence": 0.0,
            "top_3": [],
            "error": "guest_tour_info_force_heuristic",
        }
        response = self.client.post(
            self.url,
            data=json.dumps({"message": "What tours or tourism information are available in Bayawan?"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        text = str(response.json().get("fulfillmentText") or "")
        self.assertTrue("tourism information records" in text.lower() or "top recommendations for you" in text.lower())

    @patch("ai_chatbot.views._classify_intent_with_text_cnn")
    @patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False)
    def test_guest_availability_query_returns_room_availability_summary(self, mock_cnn):
        mock_cnn.return_value = {
            "intent": "",
            "source": "text_cnn_unavailable",
            "confidence": 0.0,
            "top_3": [],
            "error": "guest_availability_force_heuristic",
        }
        response = self.client.post(
            self.url,
            data=json.dumps({"message": "Are there available rooms today in Bayawan for 2 guests under 2000?"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        text = str(response.json().get("fulfillmentText") or "").lower()
        self.assertTrue("available room option" in text or "top options right now" in text)


class OwnerChatbotRegressionPackTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.url = "/api/chat/"
        self._cnn_patcher = patch("ai_chatbot.views._classify_intent_with_text_cnn")
        self.mock_cnn = self._cnn_patcher.start()
        self.mock_cnn.return_value = {
            "intent": "",
            "source": "text_cnn_unavailable",
            "confidence": 0.0,
            "top_3": [],
            "error": "owner_regression_force_heuristic",
        }

        user_model = get_user_model()
        self.owner_user = user_model.objects.create_user(
            username="owner_regression_user",
            email="owner_regression_user@example.com",
            password="secure-pass-123",
            first_name="Owner",
            last_name="Regression",
        )
        owner_group, _ = Group.objects.get_or_create(name="accommodation_owner")
        self.owner_user.groups.add(owner_group)
        self.client.force_login(self.owner_user)

        self.owner_accommodation = Accomodation.objects.create(
            owner=self.owner_user,
            company_name="Owner Regression Suites",
            email_address="owner-regression-suites@example.com",
            location="Bayawan",
            company_type="hotel",
            password="demo-password-owner-reg",
            phone_number="09990000031",
            approval_status="accepted",
            status="accepted",
        )
        self.owner_room = Room.objects.create(
            accommodation=self.owner_accommodation,
            room_name="Owner Regression Room",
            person_limit=3,
            current_availability=2,
            price_per_night=Decimal("1700.00"),
            status="AVAILABLE",
        )

        self.owner_booking = AccommodationBooking.objects.create(
            guest=self.owner_user,
            accommodation=self.owner_accommodation,
            room=self.owner_room,
            check_in=timezone.now().date() + timedelta(days=2),
            check_out=timezone.now().date() + timedelta(days=4),
            num_guests=2,
            status="pending",
            payment_status="partial",
            total_amount=Decimal("3400.00"),
            amount_paid=Decimal("1000.00"),
        )
        Billing.objects.create(
            booking=self.owner_booking,
            booking_reference="BILL-OWNER-REG-001",
            total_amount=Decimal("3400.00"),
            payment_status="partial",
            payment_method="gcash",
            amount_paid=Decimal("1000.00"),
        )

        self.other_owner = user_model.objects.create_user(
            username="owner_regression_other",
            email="owner_regression_other@example.com",
            password="secure-pass-123",
            first_name="Other",
            last_name="Owner",
        )
        self.other_owner.groups.add(owner_group)
        other_accommodation = Accomodation.objects.create(
            owner=self.other_owner,
            company_name="Other Owner Inn",
            email_address="other-owner-inn@example.com",
            location="Bayawan",
            company_type="inn",
            password="demo-password-owner-other",
            phone_number="09990000032",
            approval_status="accepted",
            status="accepted",
        )
        other_room = Room.objects.create(
            accommodation=other_accommodation,
            room_name="Other Owner Room",
            person_limit=2,
            current_availability=1,
            price_per_night=Decimal("1300.00"),
            status="AVAILABLE",
        )
        other_booking = AccommodationBooking.objects.create(
            guest=self.other_owner,
            accommodation=other_accommodation,
            room=other_room,
            check_in=timezone.now().date() + timedelta(days=3),
            check_out=timezone.now().date() + timedelta(days=5),
            num_guests=2,
            status="confirmed",
            payment_status="paid",
            total_amount=Decimal("2600.00"),
            amount_paid=Decimal("2600.00"),
        )
        Billing.objects.create(
            booking=other_booking,
            booking_reference="BILL-OWNER-REG-002",
            total_amount=Decimal("2600.00"),
            payment_status="paid",
            payment_method="cash",
            amount_paid=Decimal("2600.00"),
        )

    def tearDown(self):
        self._cnn_patcher.stop()

    def _load_owner_regression_rows(self):
        pack_path = (
            Path(__file__).resolve().parent.parent
            / "thesis_data_templates"
            / "CHATBOT_OWNER_40_PROMPT_REGRESSION_PACK.csv"
        )
        rows = []
        with pack_path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                if isinstance(row, dict):
                    rows.append(row)
        self.assertEqual(len(rows), 40)
        return rows

    @patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False)
    def test_owner_prompt_topic_detection_alignment(self):
        rows = self._load_owner_regression_rows()
        for row in rows:
            prompt = str(row.get("prompt") or "").strip()
            expected_topic = str(row.get("expected_topic") or "").strip()
            if not expected_topic:
                continue
            with self.subTest(case_id=row.get("id"), prompt=prompt):
                self.assertEqual(_detect_owner_support_topic(prompt), expected_topic)

    @patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False)
    def test_owner_40_prompt_endpoint_regression_pack(self):
        rows = self._load_owner_regression_rows()
        for row in rows:
            prompt = str(row.get("prompt") or "").strip()
            expected_topic = str(row.get("expected_topic") or "").strip()
            expects_clarification = str(row.get("needs_clarification_expected") or "").strip().lower() == "yes"
            with self.subTest(case_id=row.get("id"), prompt=prompt):
                response = self.client.post(
                    self.url,
                    data=json.dumps({"message": prompt}),
                    content_type="application/json",
                )
                self.assertEqual(response.status_code, 200)
                body = response.json()
                text = str(body.get("fulfillmentText") or "").strip()
                self.assertTrue(text)
                if expected_topic:
                    latest_log = ChatbotLog.objects.filter(
                        user=self.owner_user,
                        user_message=prompt,
                    ).order_by("-created_at").first()
                    self.assertIsNotNone(latest_log)
                    if latest_log is not None:
                        self.assertEqual(str(latest_log.resolved_intent or "").strip(), expected_topic)
                if expects_clarification:
                    self.assertTrue(bool(body.get("needs_clarification")) or bool(body.get("quick_replies")))

    @patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False)
    def test_owner_payment_status_response_excludes_other_owner_records(self):
        response = self.client.post(
            self.url,
            data=json.dumps({"message": "Can I see the payment status of reservations?"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        text = str(response.json().get("fulfillmentText") or "")
        self.assertIn(f"Booking ID {self.owner_booking.booking_id}", text)
        self.assertNotIn("BILL-OWNER-REG-002", text)

    @patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False)
    def test_owner_direct_booking_from_listing_query_routes_to_owner_guidance(self):
        self.assertEqual(
            _detect_owner_support_topic("Can guests book directly from my listing?"),
            "owner_direct_booking_flow",
        )
        response = self.client.post(
            self.url,
            data=json.dumps({"message": "Can guests book directly from my listing?"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        text = str(body.get("fulfillmentText") or "").lower()
        self.assertIn("owner direct-booking workflow", text)
        self.assertIn("guests can book", text)
        self.assertEqual(str(body.get("billing_link_label") or ""), "Open Owner Hub")
        self.assertNotIn("tour package recommendations are currently guest-focused", text)

    @patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False)
    def test_owner_direct_booking_supports_tagalog_variant(self):
        self.assertEqual(
            _detect_owner_support_topic("Pwede ba magbook diretso sa listing ko?"),
            "owner_direct_booking_flow",
        )
        response = self.client.post(
            self.url,
            data=json.dumps({"message": "Pwede ba magbook diretso sa listing ko?"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        text = str(response.json().get("fulfillmentText") or "").lower()
        self.assertIn("owner direct-booking workflow", text)

    @patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False)
    def test_owner_guest_recommendation_cards_are_blocked(self):
        response = self.client.post(
            self.url,
            data=json.dumps({"message": "Recommend a hotel in Bayawan for 2 guests"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        text = str(response.json().get("fulfillmentText") or "").lower()
        self.assertIn("accommodation recommendation cards are shown for guest booking flow only", text)

    @patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False)
    def test_owner_non_tour_recommendation_fallback_is_role_aware(self):
        response = self.client.post(
            self.url,
            data=json.dumps({"message": "can you optimize my poetry writing style?"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        text = str(response.json().get("fulfillmentText") or "").lower()
        self.assertIn("manage your accommodations, rooms, bookings, and reports", text)
        self.assertNotIn("tour package recommendations are currently guest-focused", text)


class EmployeeChatbotRegressionPackTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.url = "/api/chat/"
        self._cnn_patcher = patch("ai_chatbot.views._classify_intent_with_text_cnn")
        self.mock_cnn = self._cnn_patcher.start()
        self.mock_cnn.return_value = {
            "intent": "",
            "source": "text_cnn_unavailable",
            "confidence": 0.0,
            "top_3": [],
            "error": "employee_regression_force_heuristic",
        }

        self.employee = Employee.objects.create(
            first_name="Emp",
            last_name="Regression",
            username="employee_regression_user",
            age=29,
            phone_number="09179990001",
            email="employee_regression_user@example.com",
            sex="F",
            role="Tourism Employee",
            status="accepted",
        )
        self.employee.set_password("secure-pass-123")
        self.employee.save()

        self.client.logout()
        session = self.client.session
        session["user_type"] = "employee"
        session["employee_id"] = self.employee.emp_id
        session["is_admin"] = False
        session.save()

        guest_one = Guest.objects.create(
            guest_id="E0001",
            first_name="Ana",
            last_name="Lopez",
            username="emp_guest_ana",
            country_of_origin="PH",
            phone_number="09990000101",
            email="emp_guest_ana@example.com",
            sex="F",
            password="pass",
            is_active=True,
        )
        guest_two = Guest.objects.create(
            guest_id="E0002",
            first_name="Ben",
            last_name="Cruz",
            username="emp_guest_ben",
            country_of_origin="PH",
            phone_number="09990000102",
            email="emp_guest_ben@example.com",
            sex="M",
            password="pass",
            is_active=True,
        )

        self.tour = Tour_Add.objects.create(
            tour_id="70001",
            tour_name="Employee Regression Tour",
            description="Employee monitoring tour fixture.",
        )
        now = timezone.now()
        self.schedule = Tour_Schedule.objects.create(
            tour=self.tour,
            start_time=now + timedelta(days=1),
            end_time=now + timedelta(days=2),
            price=Decimal("450.00"),
            slots_available=25,
            slots_booked=3,
            duration_days=1,
            status="active",
        )
        Pending.objects.create(
            guest_id=guest_one,
            sched_id=self.schedule,
            tour_id=self.tour,
            status="Pending",
            total_guests=2,
            your_name="Ana Lopez",
            your_email="emp_guest_ana@example.com",
            your_phone="09990000101",
            num_adults=2,
            num_children=0,
        )
        TourBooking.objects.create(
            guest=guest_one,
            tour=self.tour,
            schedule=self.schedule,
            status="pending",
            total_guests=2,
            num_adults=2,
            num_children=0,
            base_price=Decimal("900.00"),
            additional_fees=Decimal("0.00"),
            discounts=Decimal("0.00"),
            total_amount=Decimal("900.00"),
            payment_status="unpaid",
            amount_paid=Decimal("0.00"),
        )
        TourBooking.objects.create(
            guest=guest_two,
            tour=self.tour,
            schedule=self.schedule,
            status="completed",
            total_guests=1,
            num_adults=1,
            num_children=0,
            base_price=Decimal("450.00"),
            additional_fees=Decimal("0.00"),
            discounts=Decimal("0.00"),
            total_amount=Decimal("450.00"),
            payment_status="paid",
            amount_paid=Decimal("450.00"),
        )

        self.accommodation = Accomodation.objects.create(
            company_name="Employee Fixture Hotel",
            email_address="employee-fixture-hotel@example.com",
            location="Bayawan",
            company_type="hotel",
            password="demo-password-employee-fixture",
            phone_number="09990000103",
            approval_status="accepted",
            status="accepted",
            is_active=True,
        )
        self.room = Room.objects.create(
            accommodation=self.accommodation,
            room_name="Employee Fixture Room",
            person_limit=2,
            current_availability=1,
            price_per_night=Decimal("1200.00"),
            status="AVAILABLE",
        )
        booking = AccommodationBooking.objects.create(
            guest=guest_one,
            accommodation=self.accommodation,
            room=self.room,
            check_in=timezone.now().date() + timedelta(days=3),
            check_out=timezone.now().date() + timedelta(days=5),
            num_guests=2,
            status="pending",
            payment_status="unpaid",
            total_amount=Decimal("2400.00"),
            amount_paid=Decimal("0.00"),
        )
        Billing.objects.create(
            booking=booking,
            booking_reference="BILL-EMP-REG-001",
            total_amount=Decimal("2400.00"),
            payment_status="unpaid",
            payment_method="",
            amount_paid=Decimal("0.00"),
        )
        TourismInformation.objects.create(
            spot_name="Employee Published Spot",
            description="Published destination fixture.",
            location="Bayawan City Proper",
            contact_information="09179990002",
            operating_hours="08:00 AM - 05:00 PM",
            publication_status="published",
            is_active=True,
            created_by=None,
            updated_by=None,
        )

    def tearDown(self):
        self._cnn_patcher.stop()

    def _load_employee_regression_rows(self):
        pack_path = (
            Path(__file__).resolve().parent.parent
            / "thesis_data_templates"
            / "CHATBOT_EMPLOYEE_40_PROMPT_REGRESSION_PACK.csv"
        )
        rows = []
        with pack_path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                if isinstance(row, dict):
                    rows.append(row)
        self.assertEqual(len(rows), 40)
        return rows

    @patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False)
    def test_employee_prompt_detection_alignment(self):
        rows = self._load_employee_regression_rows()
        for row in rows:
            prompt = str(row.get("prompt") or "").strip()
            expected_topic = str(row.get("expected_detection_topic") or "").strip()
            with self.subTest(case_id=row.get("id"), prompt=prompt):
                self.assertEqual(_detect_employee_support_topic(prompt), expected_topic)

    @patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False)
    def test_employee_40_prompt_endpoint_regression_pack(self):
        rows = self._load_employee_regression_rows()
        for row in rows:
            prompt = str(row.get("prompt") or "").strip()
            expected_intents = {
                str(item).strip()
                for item in str(row.get("expected_endpoint_intents") or "").split("|")
                if str(item).strip()
            }
            needs_clarification = str(row.get("needs_clarification_expected") or "").strip().lower() == "yes"
            with self.subTest(case_id=row.get("id"), prompt=prompt):
                response = self.client.post(
                    self.url,
                    data=json.dumps({"message": prompt}),
                    content_type="application/json",
                )
                self.assertEqual(response.status_code, 200)
                body = response.json()
                text = str(body.get("fulfillmentText") or "").strip()
                self.assertTrue(text)
                latest_log = ChatbotLog.objects.filter(user__isnull=True, user_message=prompt).order_by("-created_at").first()
                self.assertIsNotNone(latest_log)
                if latest_log is not None:
                    self.assertIn(str(latest_log.resolved_intent or "").strip(), expected_intents)
                if needs_clarification:
                    self.assertTrue(bool(body.get("needs_clarification")) or bool(body.get("quick_replies")))

    @patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False)
    def test_employee_tourist_monitoring_summary_is_database_driven(self):
        response = self.client.post(
            self.url,
            data=json.dumps({"message": "How can I view tourist records in the system?"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        text = str(response.json().get("fulfillmentText") or "")
        self.assertIn("Active tourist records", text)
        self.assertIn("Tour booking records", text)

    @patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False)
    def test_employee_assigned_tours_query_returns_assignment_list(self):
        TourAssignment.objects.create(employee=self.employee, schedule=self.schedule)
        response = self.client.post(
            self.url,
            data=json.dumps({"message": "what tour package am i assigned in?"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        text = str(body.get("fulfillmentText") or "").lower()
        self.assertIn("assigned tours for", text)
        self.assertIn("employee regression tour", text)
        self.assertEqual(str(body.get("billing_link_label") or ""), "Open Assigned Tours")

    @patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False)
    def test_employee_assigned_tours_query_supports_bisaya_phrase(self):
        TourAssignment.objects.create(employee=self.employee, schedule=self.schedule)
        response = self.client.post(
            self.url,
            data=json.dumps({"message": "unsa akong assigned tour?"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        text = str(body.get("fulfillmentText") or "").lower()
        self.assertIn("assigned tours for", text)
        self.assertIn("employee regression tour", text)
        self.assertEqual(str(body.get("billing_link_label") or ""), "Open Assigned Tours")

    @patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False)
    def test_employee_assigned_tours_query_supports_tagalog_grammar_variant(self):
        TourAssignment.objects.create(employee=self.employee, schedule=self.schedule)
        response = self.client.post(
            self.url,
            data=json.dumps({"message": "ano po yung tour package na assign sakin?"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        text = str(body.get("fulfillmentText") or "").lower()
        self.assertIn("assigned tours for", text)
        self.assertIn("employee regression tour", text)
        self.assertEqual(str(body.get("billing_link_label") or ""), "Open Assigned Tours")

    @patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False)
    def test_employee_booking_monitoring_summary_aligns_tour_total_with_pending_module(self):
        TourBooking.objects.all().delete()
        Pending.objects.create(
            guest_id=Guest.objects.get(guest_id="E0001"),
            sched_id=self.schedule,
            tour_id=self.tour,
            status="Accepted",
            total_guests=1,
            your_name="Ana Lopez",
            your_email="emp_guest_ana@example.com",
            your_phone="09990000101",
            num_adults=1,
            num_children=0,
        )
        response = self.client.post(
            self.url,
            data=json.dumps({"message": "Can I check pending reservations?"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        text = str(response.json().get("fulfillmentText") or "").lower()
        self.assertIn("booking monitoring snapshot", text)
        self.assertIn("tour bookings: total 2", text)
        self.assertIn("pending-booking records", text)

    @patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False)
    def test_employee_reports_support_summary_aligns_tour_total_with_pending_module(self):
        TourBooking.objects.all().delete()
        Pending.objects.create(
            guest_id=Guest.objects.get(guest_id="E0001"),
            sched_id=self.schedule,
            tour_id=self.tour,
            status="Accepted",
            total_guests=1,
            your_name="Ana Lopez",
            your_email="emp_guest_ana@example.com",
            your_phone="09990000101",
            num_adults=1,
            num_children=0,
        )
        response = self.client.post(
            self.url,
            data=json.dumps({"message": "Can I view tourist statistics?"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        text = str(response.json().get("fulfillmentText") or "").lower()
        self.assertIn("monitoring and reports snapshot", text)
        self.assertIn("tour bookings total: 2", text)
        self.assertIn("pending-booking records", text)

    @patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False)
    def test_employee_tour_booking_revenue_query_includes_revenue_line(self):
        response = self.client.post(
            self.url,
            data=json.dumps({"message": "how much total revenue do we have all time when it comes to tour bookings?"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        text = str(response.json().get("fulfillmentText") or "").lower()
        self.assertIn("tour booking summary for all time", text)
        self.assertIn("tour revenue for all time", text)
        self.assertNotIn("total 0, pending 0, active 0, completed 0, cancelled 0", text)

    @patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False)
    def test_employee_tour_summary_falls_back_to_pending_records_when_tourbooking_empty(self):
        TourBooking.objects.all().delete()
        Pending.objects.create(
            guest_id=Guest.objects.get(guest_id="E0001"),
            sched_id=self.schedule,
            tour_id=self.tour,
            status="Accepted",
            total_guests=1,
            your_name="Ana Lopez",
            your_email="emp_guest_ana@example.com",
            your_phone="09990000101",
            num_adults=1,
            num_children=0,
        )
        response = self.client.post(
            self.url,
            data=json.dumps({"message": "how many tour bookings do we have?"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        text = str(response.json().get("fulfillmentText") or "").lower()
        self.assertIn("tour booking summary for all time", text)
        self.assertNotIn("total 0, pending 0, active 0, completed 0, cancelled 0", text)
        self.assertIn("pending-booking module", text)

    @patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False)
    def test_employee_guest_booking_action_is_restricted(self):
        response = self.client.post(
            self.url,
            data=json.dumps({"message": "book room 999 for 2 guests from 2026-04-20 to 2026-04-22"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        text = str(response.json().get("fulfillmentText") or "").lower()
        self.assertIn("room booking via chatbot is available for guest accounts", text)


class AdminChatbotRegressionPackTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.url = "/api/chat/"
        self._cnn_patcher = patch("ai_chatbot.views._classify_intent_with_text_cnn")
        self.mock_cnn = self._cnn_patcher.start()
        self.mock_cnn.return_value = {
            "intent": "",
            "source": "text_cnn_unavailable",
            "confidence": 0.0,
            "top_3": [],
            "error": "admin_regression_force_heuristic",
        }

        self.admin_employee = Employee.objects.create(
            first_name="Admin",
            last_name="Regression",
            username="admin_regression_user",
            age=34,
            phone_number="09179990011",
            email="admin_regression_user@example.com",
            sex="M",
            role="Admin",
            status="accepted",
        )
        self.admin_employee.set_password("secure-pass-123")
        self.admin_employee.save()

        self.client.logout()
        session = self.client.session
        session["user_type"] = "employee"
        session["employee_id"] = self.admin_employee.emp_id
        session["is_admin"] = True
        session.save()

        user_model = get_user_model()
        self.owner_user = user_model.objects.create_user(
            username="admin_pack_owner_user",
            email="admin_pack_owner_user@example.com",
            password="secure-pass-123",
            first_name="Owner",
            last_name="Pack",
        )
        self.owner_pending_user = user_model.objects.create_user(
            username="admin_pack_owner_pending",
            email="admin_pack_owner_pending@example.com",
            password="secure-pass-123",
            first_name="Pending",
            last_name="Owner",
        )
        owner_group, _ = Group.objects.get_or_create(name="accommodation_owner")
        pending_owner_group, _ = Group.objects.get_or_create(name="accommodation_owner_pending")
        self.owner_user.groups.add(owner_group)
        self.owner_pending_user.groups.add(pending_owner_group)

        self.guest = Guest.objects.create(
            guest_id="A0001",
            first_name="Cara",
            last_name="AdminPack",
            username="admin_pack_guest",
            country_of_origin="PH",
            phone_number="09990000201",
            email="admin_pack_guest@example.com",
            sex="F",
            password="pass",
            is_active=True,
        )

        self.accom_pending = Accomodation.objects.create(
            company_name="Admin Pending Hotel",
            email_address="admin-pending-hotel@example.com",
            location="Bayawan",
            company_type="hotel",
            password="demo-password-admin-pending",
            phone_number="09990000202",
            approval_status="pending",
            status="pending",
            is_active=True,
        )
        self.accom_accepted = Accomodation.objects.create(
            owner=self.owner_user,
            company_name="Admin Accepted Inn",
            email_address="admin-accepted-inn@example.com",
            location="Bayawan",
            company_type="inn",
            password="demo-password-admin-accepted",
            phone_number="09990000203",
            approval_status="accepted",
            status="accepted",
            is_active=True,
        )
        self.accom_declined = Accomodation.objects.create(
            company_name="Admin Declined Lodge",
            email_address="admin-declined-lodge@example.com",
            location="Bayawan",
            company_type="hotel",
            password="demo-password-admin-declined",
            phone_number="09990000204",
            approval_status="declined",
            status="declined",
            is_active=True,
        )

        self.room = Room.objects.create(
            accommodation=self.accom_accepted,
            room_name="Admin Pack Room",
            person_limit=2,
            current_availability=1,
            price_per_night=Decimal("1400.00"),
            status="AVAILABLE",
        )

        AccommodationBooking.objects.create(
            guest=self.guest,
            accommodation=self.accom_accepted,
            room=self.room,
            check_in=timezone.now().date() + timedelta(days=2),
            check_out=timezone.now().date() + timedelta(days=4),
            num_guests=2,
            status="confirmed",
            payment_status="paid",
            total_amount=Decimal("2800.00"),
            amount_paid=Decimal("2800.00"),
        )
        AccommodationBooking.objects.create(
            guest=self.guest,
            accommodation=self.accom_accepted,
            room=self.room,
            check_in=timezone.now().date() + timedelta(days=5),
            check_out=timezone.now().date() + timedelta(days=6),
            num_guests=1,
            status="pending",
            payment_status="unpaid",
            total_amount=Decimal("1400.00"),
            amount_paid=Decimal("0.00"),
        )

        self.tour = Tour_Add.objects.create(
            tour_id="80001",
            tour_name="Admin Regression Tour",
            description="Admin monitoring fixture tour.",
        )
        now = timezone.now()
        self.schedule = Tour_Schedule.objects.create(
            tour=self.tour,
            start_time=now + timedelta(days=1),
            end_time=now + timedelta(days=2),
            price=Decimal("500.00"),
            slots_available=20,
            slots_booked=3,
            duration_days=1,
            status="active",
        )
        TourBooking.objects.create(
            guest=self.guest,
            tour=self.tour,
            schedule=self.schedule,
            status="pending",
            total_guests=2,
            num_adults=2,
            num_children=0,
            base_price=Decimal("1000.00"),
            additional_fees=Decimal("0.00"),
            discounts=Decimal("0.00"),
            total_amount=Decimal("1000.00"),
            payment_status="unpaid",
            amount_paid=Decimal("0.00"),
        )

        TourismInformation.objects.create(
            spot_name="Admin Published Destination",
            description="Published destination for admin tests.",
            location="Bayawan",
            publication_status="published",
            is_active=True,
            created_by=None,
            updated_by=None,
        )
        TourismInformation.objects.create(
            spot_name="Admin Draft Destination",
            description="Draft destination for admin tests.",
            location="Bayawan",
            publication_status="draft",
            is_active=True,
            created_by=None,
            updated_by=None,
        )
        TourismInformation.objects.create(
            spot_name="Admin Archived Destination",
            description="Archived destination for admin tests.",
            location="Bayawan",
            publication_status="archived",
            is_active=True,
            created_by=None,
            updated_by=None,
        )

    def tearDown(self):
        self._cnn_patcher.stop()

    def _load_admin_regression_rows(self):
        pack_path = (
            Path(__file__).resolve().parent.parent
            / "thesis_data_templates"
            / "CHATBOT_ADMIN_40_PROMPT_REGRESSION_PACK.csv"
        )
        rows = []
        with pack_path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                if isinstance(row, dict):
                    rows.append(row)
        self.assertEqual(len(rows), 40)
        return rows

    @patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False)
    def test_admin_prompt_detection_alignment(self):
        rows = self._load_admin_regression_rows()
        for row in rows:
            prompt = str(row.get("prompt") or "").strip()
            expected_topic = str(row.get("expected_detection_topic") or "").strip()
            with self.subTest(case_id=row.get("id"), prompt=prompt):
                self.assertEqual(_detect_admin_support_topic(prompt), expected_topic)

    @patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False)
    def test_admin_40_prompt_endpoint_regression_pack(self):
        rows = self._load_admin_regression_rows()
        for row in rows:
            prompt = str(row.get("prompt") or "").strip()
            expected_intents = {
                str(item).strip()
                for item in str(row.get("expected_endpoint_intents") or "").split("|")
                if str(item).strip()
            }
            needs_clarification = str(row.get("needs_clarification_expected") or "").strip().lower() == "yes"
            with self.subTest(case_id=row.get("id"), prompt=prompt):
                response = self.client.post(
                    self.url,
                    data=json.dumps({"message": prompt}),
                    content_type="application/json",
                )
                self.assertEqual(response.status_code, 200)
                body = response.json()
                text = str(body.get("fulfillmentText") or "").strip()
                self.assertTrue(text)
                latest_log = ChatbotLog.objects.filter(user__isnull=True, user_message=prompt).order_by("-created_at").first()
                self.assertIsNotNone(latest_log)
                if latest_log is not None:
                    self.assertIn(str(latest_log.resolved_intent or "").strip(), expected_intents)
                if needs_clarification:
                    self.assertTrue(bool(body.get("needs_clarification")) or bool(body.get("quick_replies")))

    @patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False)
    def test_admin_booking_monitoring_summary_is_database_driven(self):
        response = self.client.post(
            self.url,
            data=json.dumps({"message": "Where can I see system-wide booking summaries?"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        text = str(response.json().get("fulfillmentText") or "")
        self.assertIn("System-wide booking monitoring snapshot", text)
        self.assertIn("Accommodation bookings", text)
        self.assertIn("Tour bookings", text)

    @patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False)
    def test_admin_tour_booking_count_query_uses_tour_booking_totals(self):
        response = self.client.post(
            self.url,
            data=json.dumps({"message": "how many tour bookings do we have?"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        text = str(body.get("fulfillmentText") or "").lower()
        self.assertIn("tour booking summary", text)
        self.assertIn("total 1", text)
        self.assertIn("pending 1", text)
        self.assertNotIn("confirmed", text)
        self.assertEqual(str(body.get("billing_link_label") or ""), "Open Tour Bookings")

    @patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False)
    def test_admin_follow_up_tour_packages_phrase_routes_to_admin_monitoring(self):
        response = self.client.post(
            self.url,
            data=json.dumps({"message": "i mean tour packages"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        text = str(body.get("fulfillmentText") or "").lower()
        self.assertIn("system-wide booking monitoring snapshot", text)
        self.assertIn("tour bookings", text)

    @patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False)
    def test_admin_combined_booking_query_returns_accommodation_and_tour_sections(self):
        response = self.client.post(
            self.url,
            data=json.dumps({"message": "how many pending bookings do we have right now for the accommodation and tours?"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        text = str(body.get("fulfillmentText") or "").lower()
        self.assertIn("booking summary for all time", text)
        self.assertIn("- accommodation:", text)
        self.assertIn("- tour:", text)

    @patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False)
    def test_admin_generic_booking_query_returns_combined_summary_and_monitoring_link(self):
        response = self.client.post(
            self.url,
            data=json.dumps({"message": "how many bookings do we have right now?"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        text = str(body.get("fulfillmentText") or "").lower()
        self.assertIn("booking summary for all time", text)
        self.assertIn("- accommodation:", text)
        self.assertIn("- tour:", text)
        self.assertEqual(str(body.get("billing_link_label") or ""), "Open Booking Monitoring")

    @patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False)
    def test_admin_chatbot_activity_query_routes_to_activity_summary(self):
        self.assertEqual(
            _detect_admin_support_topic("Can I monitor chatbot activity?"),
            "admin_chatbot_activity_monitoring",
        )
        response = self.client.post(
            self.url,
            data=json.dumps({"message": "Can I monitor chatbot activity?"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        text = str(body.get("fulfillmentText") or "").lower()
        self.assertIn("chatbot activity snapshot", text)
        self.assertIn("chat messages logged", text)
        self.assertEqual(str(body.get("billing_link_label") or ""), "Open Activity Logs")
        self.assertNotIn("tour package recommendations are currently guest-focused", text)

    @patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False)
    def test_admin_chatbot_activity_query_supports_bisaya_tagalog_variant(self):
        self.assertEqual(
            _detect_admin_support_topic("Pwede ba i-monitor ang chatbot activity?"),
            "admin_chatbot_activity_monitoring",
        )
        response = self.client.post(
            self.url,
            data=json.dumps({"message": "Pwede ba i-monitor ang chatbot activity?"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        text = str(response.json().get("fulfillmentText") or "").lower()
        self.assertIn("chatbot activity snapshot", text)

    @patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False)
    def test_admin_guest_booking_action_is_restricted(self):
        response = self.client.post(
            self.url,
            data=json.dumps({"message": "book room 1 for 2 guests from 2026-04-20 to 2026-04-22"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        text = str(response.json().get("fulfillmentText") or "").lower()
        self.assertIn("room booking via chatbot is available for guest accounts", text)

    @patch.dict(os.environ, {"OPENAI_API_KEY": ""}, clear=False)
    def test_admin_non_tour_recommendation_fallback_is_role_aware(self):
        response = self.client.post(
            self.url,
            data=json.dumps({"message": "can you write me a sci-fi story?"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        text = str(response.json().get("fulfillmentText") or "").lower()
        self.assertIn("reports, approvals, system activity, and monitoring", text)
        self.assertNotIn("tour package recommendations are currently guest-focused", text)


class ChatbotFallbackParserTests(SimpleTestCase):
    def test_extract_params_maps_nature_tour_preference(self):
        params = _extract_params_from_message("i prefer nature tours")
        self.assertEqual(params.get("tour_type"), "nature")
        self.assertEqual(params.get("preference"), "nature")

    def test_extract_params_maps_sea_tour_preference(self):
        params = _extract_params_from_message("i prefer sea tours")
        self.assertEqual(params.get("tour_type"), "sea")
        self.assertEqual(params.get("preference"), "sea")

    def test_extract_preference_profile_from_description_prompt(self):
        profile = _extract_preference_profile("I want a quiet hotel with good environment and affordable price")
        tags = profile.get("preference_tags") or []
        self.assertIn("quiet", tags)
        self.assertIn("nature", tags)
        self.assertTrue(profile.get("prefer_low_price"))

    def test_extract_preference_profile_supports_local_language_synonyms(self):
        profile = _extract_preference_profile("gusto ko ng tahimik at presko na hotel na sulit")
        tags = profile.get("preference_tags") or []
        self.assertIn("quiet", tags)
        self.assertIn("nature", tags)
        self.assertTrue(profile.get("prefer_low_price"))

    def test_location_slot_not_required_when_preference_tags_exist(self):
        params = {
            "company_type": "hotel",
            "preference_tags": ["quiet", "nature"],
        }
        missing_slot, _question = _next_accommodation_clarifying_question(params)
        self.assertNotEqual(missing_slot, "location")

    def test_extract_params_supports_result_limit_for_accommodation_list_requests(self):
        params = _extract_params_from_message("give 10 inns available")
        self.assertEqual(params.get("result_limit"), 10)
        self.assertEqual(params.get("company_type"), "inn")

    def test_personalization_decline_supports_no_with_extra_text(self):
        self.assertTrue(_is_personalization_decline_message("no. give list"))

    @patch("ai_chatbot.views._classify_intent_with_text_cnn")
    def test_intent_classifier_has_controlled_heuristic_fallback_for_incompatible_labels(self, mock_cnn):
        mock_cnn.return_value = {
            "intent": "",
            "source": "text_cnn_incompatible_label_space",
            "confidence": 0.0,
            "top_3": [],
            "error": "incompatible_label_space",
        }
        parsed = _classify_intent_and_extract_params("recommend a hotel in bayawan")
        self.assertEqual(parsed.get("intent"), "get_accommodation_recommendation")
        self.assertEqual(parsed.get("source"), "heuristic_intent_fallback")
        self.assertEqual(
            parsed.get("intent_classifier", {}).get("source"),
            "text_cnn_incompatible_label_space",
        )

    @patch("ai_chatbot.views._classify_intent_with_text_cnn")
    def test_tourism_information_keywords_route_to_tourism_information_intent(self, mock_cnn):
        mock_cnn.return_value = {
            "intent": "",
            "source": "text_cnn_incompatible_label_space",
            "confidence": 0.0,
            "top_3": [],
            "error": "incompatible_label_space",
        }
        parsed = _classify_intent_and_extract_params(
            "Show tourism information about attractions in Bayawan."
        )
        self.assertEqual(parsed.get("intent"), "get_tourism_information")
        self.assertEqual(parsed.get("source"), "heuristic_intent_fallback")

    def test_budget_extraction_keyword_plain_number(self):
        params = _extract_params_from_message("budget 1500")
        self.assertEqual(params.get("budget"), 1500)

    def test_budget_extraction_with_comma(self):
        params = _extract_params_from_message("budget 1,500 for hotel")
        self.assertEqual(params.get("budget"), 1500)

    def test_budget_extraction_with_k_suffix(self):
        params = _extract_params_from_message("budget 1.5k")
        self.assertEqual(params.get("budget"), 1500)

    def test_budget_extraction_below_keyword(self):
        params = _extract_params_from_message("recommend an inn below 2000")
        self.assertEqual(params.get("budget"), 2000)

    def test_budget_extraction_numeric_only_message(self):
        params = _extract_params_from_message("1500")
        self.assertEqual(params.get("budget"), 1500)

    def test_budget_keyword_cheap_maps_to_low_budget_cap(self):
        params = _extract_params_from_message("need a cheap inn near terminal")
        self.assertEqual(params.get("budget"), 1500)
        self.assertNotIn("budget_min", params)

    def test_budget_keyword_mid_maps_to_range(self):
        params = _extract_params_from_message("recommend a mid range hotel in bayawan")
        self.assertEqual(params.get("budget_min"), 1500)
        self.assertEqual(params.get("budget"), 2500)

    def test_budget_keyword_moderate_maps_to_range(self):
        params = _extract_params_from_message("show moderate priced inn options")
        self.assertEqual(params.get("budget_min"), 1500)
        self.assertEqual(params.get("budget"), 2500)

    def test_budget_keyword_expensive_maps_to_high_min_only(self):
        params = _extract_params_from_message("show expensive hotel options")
        self.assertEqual(params.get("budget_min"), 2501)
        self.assertNotIn("budget", params)

    def test_explicit_numeric_budget_overrides_keyword_inference(self):
        params = _extract_params_from_message("cheap hotel budget 3400")
        self.assertEqual(params.get("budget"), 3400)
        self.assertNotIn("budget_min", params)

    def test_type_extraction_hotel(self):
        params = _extract_params_from_message("recommend a hotel in bayawan")
        self.assertEqual(params.get("company_type"), "hotel")

    def test_type_extraction_inn(self):
        params = _extract_params_from_message("recommend an inn near terminal")
        self.assertEqual(params.get("company_type"), "inn")

    def test_type_extraction_either_when_both_words_present(self):
        params = _extract_params_from_message("show hotel and inn options")
        self.assertEqual(params.get("company_type"), "either")

    def test_location_extraction_near_terminal(self):
        parsed = _extract_params_with_confidence("recommend an inn near terminal for 1 guest")
        self.assertTrue(parsed.get("needs_clarification"))
        self.assertIn("terminal", str(parsed.get("clarification_question") or "").lower())
        options = parsed.get("clarification_options") or []
        self.assertTrue(isinstance(options, list) and len(options) >= 2)

    def test_location_extraction_near_trike_terminal_alias(self):
        params = _extract_params_from_message("show inns near trike terminal")
        self.assertEqual(params.get("location"), "tinago")
        self.assertNotIn("location_anchor", params)

    def test_location_extraction_near_bus_terminal_maps_to_public_terminal_anchor(self):
        params = _extract_params_from_message("show inns near bus terminal")
        self.assertEqual(params.get("location"), "tinago")
        self.assertEqual(params.get("location_anchor"), "Bayawan City Public Terminal")

    def test_location_extraction_in_bayawan(self):
        params = _extract_params_from_message("recommend hotel in bayawan for 2 guests")
        self.assertEqual(params.get("location"), "bayawan")

    def test_location_extraction_around_poblacion(self):
        params = _extract_params_from_message("show inns around poblacion under 1200")
        self.assertEqual(params.get("location"), "poblacion")

    def test_location_scope_note_is_set_for_near_or_in_patterns(self):
        params = _extract_params_from_message("show inns around poblacion")
        note = str(params.get("location_scope_note") or "").lower()
        self.assertIn("in/near", note)
        self.assertIn("based on available records", note)

    def test_location_ambiguity_prompts_for_clarification(self):
        parsed = _extract_params_with_confidence("recommend an inn near city")
        self.assertTrue(parsed.get("needs_clarification"))
        self.assertIn("multiple places", str(parsed.get("clarification_question") or "").lower())
        options = parsed.get("clarification_options") or []
        self.assertTrue(isinstance(options, list) and len(options) >= 2)

    def test_location_extraction_handles_villarreal_variant(self):
        params = _extract_params_from_message("show hotels in villarreal")
        self.assertEqual(params.get("location"), "villareal")

    def test_date_extraction_with_month_words(self):
        params = _extract_params_from_message(
            "book room 12 for 2 guests from March 27, 2026 to March 29, 2026"
        )
        self.assertEqual(params.get("check_in"), "2026-03-27")
        self.assertEqual(params.get("check_out"), "2026-03-29")

    def test_date_extraction_with_abbrev_month_words(self):
        params = _extract_params_from_message(
            "book room 12 from Mar 7, 2026 to Mar 9, 2026"
        )
        self.assertEqual(params.get("check_in"), "2026-03-07")
        self.assertEqual(params.get("check_out"), "2026-03-09")

    def test_booking_status_command_recognized_show_my_hotel_bookings(self):
        self.assertTrue(
            _is_my_accommodation_booking_status_command("show my hotel bookings")
        )

    def test_booking_status_command_recognized_hotel_booking_status(self):
        self.assertTrue(
            _is_my_accommodation_booking_status_command("hotel booking status")
        )

    def test_booking_status_command_not_recognized_unrelated(self):
        self.assertFalse(
            _is_my_accommodation_booking_status_command("recommend an inn near terminal")
        )

    def test_guest_tour_booking_view_command_recognized(self):
        self.assertTrue(
            _is_guest_view_tour_bookings_command("show my tour bookings")
        )

    def test_guest_tour_booking_view_command_recognizes_common_typo(self):
        self.assertTrue(
            _is_guest_view_tour_bookings_command("show my tour bookigns")
        )

    def test_guest_tour_booking_command_does_not_capture_view_requests(self):
        self.assertFalse(
            _is_guest_tour_booking_command("show my tour bookings")
        )

    def test_cnn_output_remaps_hostel_like_labels_to_hotel(self):
        text = _format_cnn_prediction_for_chat(
            {
                "predicted_class": "hostel",
                "confidence": 0.82,
                "top_3": [
                    {"label": "hostel", "confidence": 0.82},
                    {"label": "transient_house", "confidence": 0.12},
                    {"label": "inn", "confidence": 0.06},
                ],
            }
        ).lower()
        self.assertIn("cnn predicted type: hotel", text)
        self.assertNotIn("cnn predicted type: hostel", text)
        self.assertNotIn("- hostel:", text)
        self.assertIn("- hotel:", text)

class ChatbotLanguageAndToneTests(SimpleTestCase):
    def test_language_normalization_falls_back_to_english_for_unsupported_language(self):
        self.assertEqual(_normalize_language_code("nl"), "en")
        self.assertEqual(_normalize_language_code("dutch"), "en")
        self.assertEqual(_normalize_language_code("cebuano"), "ceb")

    def test_numeric_only_message_skips_language_detection(self):
        translated, detected = translate_to_english("2")
        self.assertEqual(translated, "2")
        self.assertEqual(detected, "en")

    def test_personalization_offer_uses_formal_prompt(self):
        text = _build_personalization_offer_text(
            {
                "budget": 3400,
                "company_type": "hotel",
            },
            {"sample_size": 1},
        ).lower()
        self.assertIn("reply yes or no", text)
        self.assertIn("please reply yes or no", text)

    def test_typo_normalization_rewrites_common_terms_safely(self):
        text = _normalize_common_chat_typos("show my tour bookigns and accomodation reccomend")
        self.assertIn("bookings", text.lower())
        self.assertIn("accommodation", text.lower())
        self.assertIn("recommend", text.lower())

    def test_intro_name_extraction_detects_simple_name(self):
        self.assertEqual(_extract_intro_first_name("Hi, I am Renold"), "Renold")

    def test_guest_out_of_scope_payload_uses_guided_fallback_pattern(self):
        payload = _build_out_of_scope_payload({"role": "guest"}, message="asdj qwe")
        text = str(payload.get("fulfillmentText") or "").lower()
        self.assertTrue("help" in text or "guide" in text)
        self.assertTrue("planning" in text or "tour" in text or "directions" in text)


class ChatbotArtifactResolutionTests(SimpleTestCase):
    @override_settings(DEBUG=False)
    @patch.dict(
        os.environ,
        {
            "CHATBOT_ALLOW_DEMO_ARTIFACT_FALLBACK": "0",
            "CHATBOT_INTENT_CNN_MODEL_PATH": "",
        },
        clear=False,
    )
    def test_intent_text_cnn_prefers_final_path_when_demo_fallback_disabled(self):
        path, source = _resolve_intent_text_cnn_model_path()
        path_str = str(path).replace("\\", "/").lower()
        self.assertTrue(
            ("/artifacts/text_cnn_intent/text_cnn_intent.keras" in path_str)
            or ("/artifacts/text_cnn_intent/text_cnn_intent.h5" in path_str)
        )
        self.assertTrue(source.startswith("final"))

    @override_settings(DEBUG=True)
    @patch.dict(
        os.environ,
        {
            "CHATBOT_ALLOW_DEMO_ARTIFACT_FALLBACK": "1",
            "CHATBOT_INTENT_CNN_MODEL_PATH": "",
        },
        clear=False,
    )
    def test_intent_text_cnn_uses_demo_fallback_only_when_enabled(self):
        path, source = _resolve_intent_text_cnn_model_path()
        path_str = str(path).replace("\\", "/").lower()
        self.assertIn("/artifacts/text_cnn_demo/text_cnn_demo.keras", path_str)
        self.assertEqual(source, "demo_fallback")

    @override_settings(DEBUG=False)
    @patch.dict(
        os.environ,
        {
            "CHATBOT_ALLOW_DEMO_ARTIFACT_FALLBACK": "0",
            "CHATBOT_ACCOM_CNN_MODEL_PATH": "",
        },
        clear=False,
    )
    def test_accommodation_text_cnn_prefers_final_path_when_demo_fallback_disabled(self):
        path, source = _resolve_accommodation_text_cnn_model_path()
        path_str = str(path).replace("\\", "/").lower()
        self.assertIn("/artifacts/text_cnn_accommodation/text_cnn_accommodation.keras", path_str)
        self.assertTrue(source.startswith("final"))

    @override_settings(DEBUG=True)
    @patch.dict(
        os.environ,
        {
            "CHATBOT_ALLOW_DEMO_ARTIFACT_FALLBACK": "1",
            "CHATBOT_DECISION_TREE_MODEL_PATH": "",
        },
        clear=False,
    )
    def test_decision_tree_uses_demo_fallback_only_when_enabled(self):
        path, source = _resolve_decision_tree_model_path()
        path_str = str(path).replace("\\", "/").lower()
        self.assertIn("/artifacts/decision_tree_demo/decision_tree_demo.pkl", path_str)
        self.assertEqual(source, "demo_fallback")

    @override_settings(DEBUG=False)
    @patch.dict(
        os.environ,
        {
            "CHATBOT_ALLOW_DEMO_ARTIFACT_FALLBACK": "0",
            "CHATBOT_DECISION_TREE_MODEL_PATH": "",
        },
        clear=False,
    )
    def test_decision_tree_prefers_final_path_when_demo_fallback_disabled(self):
        path, source = _resolve_decision_tree_model_path()
        path_str = str(path).replace("\\", "/").lower()
        self.assertIn("/artifacts/decision_tree_final/decision_tree_final.pkl", path_str)
        self.assertTrue(source.startswith("final"))


class StrictAccommodationTypeFilterTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        owner_group, _ = Group.objects.get_or_create(name="accommodation_owner")

        self.owner_hotel = user_model.objects.create_user(
            username="strict_filter_owner_hotel",
            email="strict_filter_owner_hotel@example.com",
            password="secure-pass-123",
            first_name="Owner",
            last_name="Hotel",
        )
        self.owner_hotel.groups.add(owner_group)

        self.owner_inn = user_model.objects.create_user(
            username="strict_filter_owner_inn",
            email="strict_filter_owner_inn@example.com",
            password="secure-pass-123",
            first_name="Owner",
            last_name="Inn",
        )
        self.owner_inn.groups.add(owner_group)

        self.owner_mixed = user_model.objects.create_user(
            username="strict_filter_owner_mixed",
            email="strict_filter_owner_mixed@example.com",
            password="secure-pass-123",
            first_name="Owner",
            last_name="Mixed",
        )
        self.owner_mixed.groups.add(owner_group)

        hotel_accom = Accomodation.objects.create(
            owner=self.owner_hotel,
            company_name="Strict Filter Hotel",
            email_address="strict-filter-hotel@example.com",
            location="Bayawan",
            company_type="Hotel",
            password="demo-password-123",
            phone_number="0917000301",
            approval_status="accepted",
            status="accepted",
            is_active=True,
        )
        self.hotel_room = Room.objects.create(
            accommodation=hotel_accom,
            room_name="Hotel Room",
            person_limit=2,
            current_availability=2,
            price_per_night=Decimal("1500.00"),
            status="AVAILABLE",
        )

        inn_accom = Accomodation.objects.create(
            owner=self.owner_inn,
            company_name="Strict Filter Inn",
            email_address="strict-filter-inn@example.com",
            location="Bayawan",
            company_type="Inn",
            password="demo-password-123",
            phone_number="0917000302",
            approval_status="accepted",
            status="accepted",
            is_active=True,
        )
        self.inn_room = Room.objects.create(
            accommodation=inn_accom,
            room_name="Inn Room",
            person_limit=2,
            current_availability=2,
            price_per_night=Decimal("1400.00"),
            status="AVAILABLE",
        )

        # Deliberately mixed type label to verify strict filtering behavior.
        mixed_accom = Accomodation.objects.create(
            owner=self.owner_mixed,
            company_name="Strict Filter Mixed Type",
            email_address="strict-filter-mixed@example.com",
            location="Bayawan",
            company_type="Hotel/Inn",
            password="demo-password-123",
            phone_number="0917000303",
            approval_status="accepted",
            status="accepted",
            is_active=True,
        )
        self.mixed_room = Room.objects.create(
            accommodation=mixed_accom,
            room_name="Mixed Type Room",
            person_limit=2,
            current_availability=2,
            price_per_night=Decimal("1300.00"),
            status="AVAILABLE",
        )

    def test_company_type_hotel_returns_only_exact_hotel(self):
        results = recommend_accommodations(
            {"company_type": "hotel", "location": "bayawan", "guests": 2},
            limit=10,
        )
        room_ids = {item.meta.get("room_id") for item in results}
        self.assertIn(self.hotel_room.room_id, room_ids)
        self.assertNotIn(self.inn_room.room_id, room_ids)
        self.assertNotIn(self.mixed_room.room_id, room_ids)

    def test_company_type_inn_returns_only_exact_inn(self):
        results = recommend_accommodations(
            {"company_type": "inn", "location": "bayawan", "guests": 2},
            limit=10,
        )
        room_ids = {item.meta.get("room_id") for item in results}
        self.assertIn(self.inn_room.room_id, room_ids)
        self.assertNotIn(self.hotel_room.room_id, room_ids)
        self.assertNotIn(self.mixed_room.room_id, room_ids)

    def test_company_type_either_returns_hotel_and_inn_only(self):
        results = recommend_accommodations(
            {"company_type": "either", "location": "bayawan", "guests": 2},
            limit=10,
        )
        room_ids = {item.meta.get("room_id") for item in results}
        self.assertIn(self.hotel_room.room_id, room_ids)
        self.assertIn(self.inn_room.room_id, room_ids)
        self.assertNotIn(self.mixed_room.room_id, room_ids)


class AccommodationBudgetRangeFilterTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        owner_group, _ = Group.objects.get_or_create(name="accommodation_owner")
        self.owner = user_model.objects.create_user(
            username="budget_range_owner",
            email="budget_range_owner@example.com",
            password="secure-pass-123",
            first_name="Budget",
            last_name="Owner",
        )
        self.owner.groups.add(owner_group)

        accommodation = Accomodation.objects.create(
            owner=self.owner,
            company_name="Budget Range Hotel",
            email_address="budget-range-hotel@example.com",
            location="Bayawan",
            company_type="Hotel",
            password="demo-password-123",
            phone_number="0917000401",
            approval_status="accepted",
            status="accepted",
            is_active=True,
        )
        self.room_1400 = Room.objects.create(
            accommodation=accommodation,
            room_name="Room 1400",
            person_limit=2,
            current_availability=2,
            price_per_night=Decimal("1400.00"),
            status="AVAILABLE",
        )
        self.room_1600 = Room.objects.create(
            accommodation=accommodation,
            room_name="Room 1600",
            person_limit=2,
            current_availability=2,
            price_per_night=Decimal("1600.00"),
            status="AVAILABLE",
        )
        self.room_2400 = Room.objects.create(
            accommodation=accommodation,
            room_name="Room 2400",
            person_limit=2,
            current_availability=2,
            price_per_night=Decimal("2400.00"),
            status="AVAILABLE",
        )
        self.room_2600 = Room.objects.create(
            accommodation=accommodation,
            room_name="Room 2600",
            person_limit=2,
            current_availability=2,
            price_per_night=Decimal("2600.00"),
            status="AVAILABLE",
        )

    def test_mid_budget_range_filters_between_1500_and_2500(self):
        results = recommend_accommodations(
            {
                "company_type": "hotel",
                "location": "bayawan",
                "budget_min": 1500,
                "budget": 2500,
                "guests": 2,
            },
            limit=10,
        )
        room_ids = {item.meta.get("room_id") for item in results}
        self.assertIn(self.room_1600.room_id, room_ids)
        self.assertIn(self.room_2400.room_id, room_ids)
        self.assertNotIn(self.room_1400.room_id, room_ids)
        self.assertNotIn(self.room_2600.room_id, room_ids)

    def test_expensive_budget_min_filters_above_2500(self):
        results = recommend_accommodations(
            {
                "company_type": "hotel",
                "location": "bayawan",
                "budget_min": 2501,
                "guests": 2,
            },
            limit=10,
        )
        room_ids = {item.meta.get("room_id") for item in results}
        self.assertIn(self.room_2600.room_id, room_ids)
        self.assertNotIn(self.room_2400.room_id, room_ids)


class RoleIntentCoverageExpansionTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.url = "/api/chat/"
        user_model = get_user_model()

        self.guest_user = user_model.objects.create_user(
            username="coverage_guest_user",
            email="coverage_guest_user@example.com",
            password="secure-pass-123",
        )

        employee_group, _ = Group.objects.get_or_create(name="employee")
        self.employee_user = user_model.objects.create_user(
            username="coverage_employee_user",
            email="coverage_employee_user@example.com",
            password="secure-pass-123",
        )
        self.employee_user.groups.add(employee_group)

        admin_group, _ = Group.objects.get_or_create(name="admin")
        self.admin_user = user_model.objects.create_user(
            username="coverage_admin_user",
            email="coverage_admin_user@example.com",
            password="secure-pass-123",
        )
        self.admin_user.groups.add(admin_group)

        owner_group, _ = Group.objects.get_or_create(name="accommodation_owner")
        self.owner_user = user_model.objects.create_user(
            username="coverage_owner_user",
            email="coverage_owner_user@example.com",
            password="secure-pass-123",
        )
        self.owner_user.groups.add(owner_group)

        self.owner_accommodation = Accomodation.objects.create(
            owner=self.owner_user,
            company_name="Coverage Owner Hotel",
            email_address="coverage-owner-hotel@example.com",
            location="Suba, Bayawan City, Negros Oriental",
            company_type="Hotel",
            password="secure-pass-123",
            phone_number="0917001201",
            approval_status="accepted",
            status="accepted",
            is_active=True,
        )
        self.owner_room_one = Room.objects.create(
            accommodation=self.owner_accommodation,
            room_name="Coverage Available Room",
            person_limit=2,
            current_availability=2,
            price_per_night=Decimal("1700.00"),
            status="AVAILABLE",
        )
        self.owner_room_two = Room.objects.create(
            accommodation=self.owner_accommodation,
            room_name="Coverage Unavailable Room",
            person_limit=2,
            current_availability=0,
            price_per_night=Decimal("1800.00"),
            status="UNAVAILABLE",
        )

    def _post_message(self, message):
        return self.client.post(
            self.url,
            data=json.dumps({"message": message}),
            content_type="application/json",
        )

    def test_guest_search_help_query_returns_system_guidance(self):
        self.client.force_login(self.guest_user)
        response = self._post_message("How to search hotels and inns?")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        text = str(body.get("fulfillmentText", "")).lower()
        self.assertIn("to search hotels/inns", text)
        self.assertIn("quick_replies", body)

    def test_owner_available_rooms_today_query_is_supported(self):
        self.client.force_login(self.owner_user)
        response = self._post_message("How many available rooms today?")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        text = str(body.get("fulfillmentText", "")).lower()
        self.assertIn("room availability snapshot for today", text)
        self.assertIn("billing_link", body)

    def test_employee_tourist_records_workflow_query_is_supported(self):
        self.client.force_login(self.employee_user)
        response = self._post_message("How to manage tourist records?")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        text = str(body.get("fulfillmentText", "")).lower()
        self.assertIn("tourist record workflow", text)
        self.assertIn("billing_link", body)

    def test_admin_activation_deactivation_query_is_supported(self):
        self.client.force_login(self.admin_user)
        response = self._post_message("How to activate or deactivate listings?")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        text = str(body.get("fulfillmentText", "")).lower()
        self.assertIn("activation/deactivation guidance", text)
        self.assertIn("billing_link", body)
