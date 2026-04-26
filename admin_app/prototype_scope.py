from __future__ import annotations

import os
from typing import Dict, List

from django.conf import settings
from django.db.models import Q


PROTOTYPE_DESCRIPTION_MARKERS = (
    "prototype accommodation record for thesis demonstration",
    "not a verified business listing",
)


def allow_prototype_accommodations() -> bool:
    raw = str(
        os.getenv(
            "TOURISM_INCLUDE_PROTOTYPE_ACCOMMODATIONS",
            getattr(settings, "TOURISM_INCLUDE_PROTOTYPE_ACCOMMODATIONS", ""),
        )
        or ""
    ).strip().lower()
    return raw in {"1", "true", "yes", "on"}


def prototype_accommodation_q(*, accommodation_path: str = "") -> Q:
    prefix = str(accommodation_path or "").strip()
    if prefix:
        prefix = f"{prefix}__"
    return (
        Q(**{f"{prefix}email_address__iendswith": "@placeholder.local"})
        | Q(**{f"{prefix}company_name__icontains": "prototype"})
        | Q(**{f"{prefix}description__icontains": PROTOTYPE_DESCRIPTION_MARKERS[0]})
        | Q(**{f"{prefix}description__icontains": PROTOTYPE_DESCRIPTION_MARKERS[1]})
    )


def prototype_marker_reasons(accommodation) -> List[str]:
    reasons: List[str] = []
    email_address = str(getattr(accommodation, "email_address", "") or "").strip().lower()
    company_name = str(getattr(accommodation, "company_name", "") or "").strip().lower()
    description = str(getattr(accommodation, "description", "") or "").strip().lower()

    if email_address.endswith("@placeholder.local"):
        reasons.append("email_domain_placeholder_local")
    if "prototype" in company_name:
        reasons.append("company_name_contains_prototype")
    if PROTOTYPE_DESCRIPTION_MARKERS[0] in description:
        reasons.append("description_thesis_prototype_marker")
    if PROTOTYPE_DESCRIPTION_MARKERS[1] in description:
        reasons.append("description_not_verified_marker")
    return reasons


def is_likely_prototype(accommodation) -> bool:
    return bool(prototype_marker_reasons(accommodation))


def prototype_flags(accommodation) -> Dict[str, bool]:
    reasons = prototype_marker_reasons(accommodation)
    return {
        "is_likely_prototype": bool(reasons),
        "has_placeholder_email": "email_domain_placeholder_local" in reasons,
        "has_prototype_name": "company_name_contains_prototype" in reasons,
        "has_prototype_description": (
            "description_thesis_prototype_marker" in reasons
            or "description_not_verified_marker" in reasons
        ),
    }
