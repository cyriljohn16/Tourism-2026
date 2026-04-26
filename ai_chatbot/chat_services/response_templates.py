ACCOMMODATION_SLOT_QUESTIONS = {
    "company_type": "Do you prefer a hotel, an inn, or either?",
    "location": "Please specify the area in Bayawan you would like me to prioritize.",
    "budget": "Please provide your preferred budget per night in PHP.",
    "guests": "Please indicate how many guests will stay in the room.",
    "stay_details": "Please provide your check-in/check-out dates (YYYY-MM-DD), or indicate how many nights you plan to stay.",
}

PERSONALIZATION_PROMPT_DEFAULT = (
    "If you'd like, I can narrow this down with a quick default setup. Reply Yes or No."
)


def get_accommodation_slot_question(field):
    key = str(field or "").strip().lower()
    return ACCOMMODATION_SLOT_QUESTIONS.get(
        key,
        "Could you share the next detail for your hotel or room inquiry?",
    )


def format_acknowledged_details(acknowledged_parts, next_question):
    parts = acknowledged_parts if isinstance(acknowledged_parts, list) else []
    if not parts:
        return str(next_question or "").strip()
    return (
        "Got it. I noted: "
        + "; ".join(str(part) for part in parts if str(part).strip())
        + ".\nNext step: "
        + str(next_question or "").strip().rstrip("?")
        + "?"
    )


def build_personalization_offer_text(basis_text, defaults_text):
    basis = str(basis_text or "").strip()
    defaults = str(defaults_text or "").strip()
    if not basis or not defaults:
        return ""
    return (
        f"{basis}: {defaults}. "
        "Want me to use this quick setup? Please reply Yes or No."
    )
