import json
import os
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import Client, TestCase
from django.utils import timezone

from tour_app.models import Admission_Rates, Tour_Add, Tour_Schedule


class OpenAIChatEndpointTests(TestCase):
    def setUp(self):
        self.client = Client()
        self.url = "/api/chat/"

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
