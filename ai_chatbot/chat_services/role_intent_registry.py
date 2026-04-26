"""
Role-based chatbot intent registry for system-side coverage tracking.

This module is documentation-first and intentionally decoupled from runtime routing.
It provides a maintainable reference for:
- supported intents by role
- example user queries
- expected behavior in current implementation
- known unsupported/system-limited requests
"""

ROLE_INTENT_REGISTRY = {
    "guest": {
        "supported": [
            "guest_map",
            "guest_search_help",
            "guest_booking_requirements_help",
            "guest_room_availability_check",
            "guest_payment_methods",
            "guest_down_payment_policy",
            "guest_billing_details_help",
            "guest_booking_review_help",
            "guest_booking_cancel_support",
            "guest_booking_change_date_support",
            "view_my_accommodation_bookings",
            "get_accommodation_recommendation",
            "accommodation_outbound_handoff",
            "plan_bayawan_stay",
            "travel_guidance",
        ],
        "examples": [
            "How to search hotels and inns?",
            "Help me plan my stay in Bayawan with a 10,000 peso budget.",
            "How far is Bayawan City Plaza from my location?",
            "I'm from Manila, how do I get to this place?",
            "hotel in Suba barangay for 2 guests under 2000",
            "What details do I need to provide for booking?",
            "Show accommodation contact links",
            "How do I continue booking outside the system?",
        ],
    },
    "employee": {
        "supported": [
            "open_dashboard",
            "employee_assigned_tours",
            "employee_tour_calendar",
            "employee_accommodations",
            "employee_profile",
            "employee_tour_list",
            "employee_create_tour",
            "employee_map",
            "employee_tourist_monitoring",
            "employee_booking_monitoring",
            "employee_accommodation_records",
            "employee_destination_records",
            "employee_reports_support",
            "employee_tourist_records_workflow_help",
            "employee_workflow_listing_review",
            "employee_workflow_destination_feedback",
            "employee_account_access_help",
            "employee_record_visibility_issue",
            "employee_monitoring_dashboard_help",
            "employee_record_update_help",
        ],
        "examples": [
            "How to manage tourist records?",
            "Show booking summaries",
            "Open assigned tours",
            "Can I update records from my account?",
        ],
    },
    "owner": {
        "supported": [
            "open_owner_dashboard",
            "open_owner_hub",
            "open_owner_reports_analytics",
            "open_owner_accommodation_register",
            "owner_accommodation_overview",
            "owner_room_overview",
            "owner_performance_summary",
            "owner_add_room",
            "owner_update_room_price",
            "owner_update_room_capacity",
            "owner_update_availability",
            "owner_mark_room_unavailable",
            "owner_available_rooms_today",
            "owner_report_submission",
            "owner_report_status",
            "owner_listing_status",
            "owner_listing_visibility",
            "owner_direct_booking_flow",
        ],
        "examples": [
            "How many available rooms today?",
            "Is my listing approved?",
            "Open my monthly reports",
            "How do I submit this month report?",
        ],
    },
    "admin": {
        "supported": [
            "open_dashboard",
            "admin_pending_accommodations",
            "admin_pending_owner_accounts",
            "admin_owner_reports_review",
            "admin_tourism_information_manage",
            "admin_survey_results",
            "admin_traveler_surveys",
            "admin_tour_calendar",
            "admin_tour_list",
            "admin_activity_logs",
            "admin_map",
            "admin_discounts",
            "admin_approval_workflow",
            "admin_destination_management",
            "admin_accommodation_records_management",
            "admin_booking_system_monitoring",
            "admin_user_account_management",
            "admin_reports_dashboard_support",
            "admin_chatbot_activity_monitoring",
            "admin_record_visibility_issue",
            "admin_activation_deactivation_help",
        ],
        "examples": [
            "Show pending accommodations",
            "How to activate or deactivate listings?",
            "Can I monitor chatbot activity?",
            "Show owner report summary",
        ],
    },
}


ROLE_UNSUPPORTED_QUERIES = {
    "guest": [
        "Edit an existing booking date directly (prototype supports cancel + rebook flow).",
    ],
    "employee": [
        "Approve/reject accommodation listings directly (admin-controlled).",
    ],
    "owner": [
        "Approve own listing status directly (admin-controlled).",
    ],
    "admin": [
        "Automated external API inventory sync (not implemented in current thesis scope).",
    ],
}
