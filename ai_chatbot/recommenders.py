from __future__ import annotations

import os
import pickle
import logging
import re
from datetime import date, datetime
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Iterable, List, Optional

from django.conf import settings
from django.utils import timezone
from django.db.models import F, Q

try:
    import pandas as pd
except ModuleNotFoundError:
    pd = None

from admin_app.models import Accomodation, Room
from admin_app.prototype_scope import allow_prototype_accommodations, prototype_accommodation_q
from tour_app.models import Tour_Schedule

_DECISION_TREE_MODEL_CACHE = None
_DECISION_TREE_MODEL_PATH_CACHE = None
_DECISION_TREE_MODEL_SOURCE_CACHE = "unknown"
_DECISION_TREE_EXPECTED_PARAMS = {
    "criterion": "entropy",
    "max_depth": 8,
    "min_samples_leaf": 5,
    "min_samples_split": 30,
    "max_features": None,
}
logger = logging.getLogger(__name__)

_TOUR_PREFERENCE_ALIASES = {
    "sea": ("sea", "beach", "coastal", "ocean", "island", "shore", "seaside"),
    "nature": ("nature", "natural", "falls", "waterfall", "forest", "mountain", "river", "scenic"),
    "culture": ("culture", "cultural", "heritage", "food", "culinary", "tradition", "museum"),
    "city": ("city", "urban", "proper", "downtown", "plaza"),
    "highlights": ("highlight", "highlights", "day tour", "must-see"),
    "adventure": ("adventure", "hike", "trek", "trail", "outdoor", "camp"),
    "family": ("family", "kids", "child", "children", "friendly"),
    "river": ("river", "riverside"),
}

_LOCATION_ALIAS_CANONICAL = {
    "suba": "suba",
    "barangay suba": "suba",
    "suba barangay": "suba",
    "brgy suba": "suba",
    "poblacion": "poblacion",
    "barangay poblacion": "poblacion",
    "poblacion barangay": "poblacion",
    "city proper": "poblacion",
    "bayawan city proper": "poblacion",
    "tinago": "tinago",
    "ubos": "ubos",
    "boyco": "boyco",
    "villareal": "villareal",
    "villarreal": "villareal",
    "bayawan": "bayawan city",
    "bayawan city": "bayawan city",
}

_LOCATION_GENERIC_TOKENS = {
    "barangay", "brgy", "city", "bayawan", "negros", "oriental",
    "street", "st", "road", "rd", "avenue", "ave", "highway",
    "near", "around", "in", "sa", "proper", "downtown",
}

_ROOM_TYPE_KEYWORDS = (
    "standard", "deluxe", "family", "double", "twin",
    "suite", "queen", "king", "single", "matrimonial",
    "villa", "executive", "business", "economy", "budget",
    "premium",
)


def _parse_approved_accommodation_ids() -> list[int]:
    configured = os.getenv(
        "TOURISM_APPROVED_ACCOMMODATION_IDS",
        getattr(settings, "TOURISM_APPROVED_ACCOMMODATION_IDS", ""),
    )
    if isinstance(configured, (list, tuple, set)):
        parsed = []
        for value in configured:
            try:
                parsed.append(int(value))
            except Exception:
                continue
        return sorted({v for v in parsed if v > 0})
    raw = str(configured or "").strip()
    if not raw:
        return []
    parsed = []
    for token in raw.split(","):
        compact = str(token or "").strip()
        if not compact:
            continue
        try:
            parsed.append(int(compact))
        except Exception:
            continue
    return sorted({v for v in parsed if v > 0})


def _parse_approved_accommodation_names() -> list[str]:
    configured = os.getenv(
        "TOURISM_APPROVED_ACCOMMODATION_NAMES",
        getattr(settings, "TOURISM_APPROVED_ACCOMMODATION_NAMES", ""),
    )
    if isinstance(configured, (list, tuple, set)):
        names = []
        for value in configured:
            compact = " ".join(str(value or "").strip().lower().split())
            if compact:
                names.append(compact)
        return sorted(set(names))
    raw = str(configured or "").strip()
    if not raw:
        return []
    names = []
    for token in raw.split(","):
        compact = " ".join(str(token or "").strip().lower().split())
        if compact:
            names.append(compact)
    return sorted(set(names))


def apply_approved_accommodation_scope(qs, *, accommodation_path: str = "accommodation"):
    """
    Enforce Tourism Office-approved accommodation visibility for guest-facing flows.
    Base scope always requires accepted + active accommodations.
    Optional strict allowlist can be configured through:
    - TOURISM_APPROVED_ACCOMMODATION_IDS (comma-separated integer IDs)
    - TOURISM_APPROVED_ACCOMMODATION_NAMES (comma-separated company names)
    """
    if qs is None:
        return qs
    if accommodation_path is None:
        prefix = "accommodation"
    else:
        prefix = str(accommodation_path).strip()
    if prefix:
        prefix = f"{prefix}__"
    scoped = (
        qs.filter(**{f"{prefix}approval_status": "accepted"})
        .filter(**{f"{prefix}is_active": True})
    )
    if not allow_prototype_accommodations():
        scoped = scoped.exclude(prototype_accommodation_q(accommodation_path=accommodation_path))
    approved_ids = _parse_approved_accommodation_ids()
    approved_names = _parse_approved_accommodation_names()
    if approved_ids:
        scoped = scoped.filter(**{f"{prefix}accom_id__in": approved_ids})
    if approved_names:
        name_q = Q()
        for company_name in approved_names:
            name_q |= Q(**{f"{prefix}company_name__iexact": company_name})
        scoped = scoped.filter(name_q)
    return scoped


@dataclass
class RecommendationResult:
    title: str
    subtitle: str
    score: float
    meta: dict


def _to_int(value, default=0):
    try:
        if value in ("", None):
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _to_decimal(value, default=Decimal("0")):
    try:
        if value in ("", None):
            return default
        return Decimal(str(value))
    except Exception:
        return default


def _safe_media_url(file_field):
    if not file_field:
        return ""
    try:
        return str(file_field.url or "").strip()
    except Exception:
        return ""


def _normalize_location_phrase(value: str) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    text = " ".join(text.split())
    if not text:
        return ""
    if text.startswith("barangay "):
        text = text.replace("barangay ", "", 1).strip()
    elif text.startswith("brgy "):
        text = text.replace("brgy ", "", 1).strip()
    if text.endswith(" barangay"):
        text = text[: -len(" barangay")].strip()
    elif text.endswith(" brgy"):
        text = text[: -len(" brgy")].strip()
    return _LOCATION_ALIAS_CANONICAL.get(text, text)


def _expand_location_aliases(raw_location: str) -> list[str]:
    normalized = _normalize_location_phrase(raw_location)
    if not normalized:
        return []
    aliases = {normalized}
    for alias, canonical in _LOCATION_ALIAS_CANONICAL.items():
        if canonical == normalized:
            aliases.add(alias)
        if alias and alias in normalized:
            aliases.add(alias)
            aliases.add(canonical)
    if normalized == "bayawan city":
        aliases.update({"bayawan", "city proper"})
    # Address-aware token expansion:
    # convert long user-entered addresses into stable searchable terms
    # (e.g., "J.P. Rizal St., Barangay Suba, Bayawan City").
    stop_tokens = {
        "barangay", "brgy", "city", "bayawan", "negros", "oriental",
        "st", "street", "rd", "road", "avenue", "ave", "near",
    }
    tokens = [token for token in normalized.split() if token]
    for token in tokens:
        if len(token) >= 4 and token not in stop_tokens:
            aliases.add(token)
    if "j p rizal" in normalized:
        aliases.update({"j p rizal", "rizal"})
    if "r t diao" in normalized:
        aliases.update({"r t diao", "diao"})
    if "mabini" in normalized:
        aliases.add("mabini")
    if "national highway" in normalized:
        aliases.update({"national highway", "highway"})
    if "villareal" in normalized or "villarreal" in normalized:
        aliases.update({"villareal", "villarreal"})
    return sorted({a for a in aliases if a})


def _location_specific_tokens(value: str) -> set[str]:
    normalized = _normalize_location_phrase(value)
    if not normalized:
        return set()
    tokens = set()
    for token in normalized.split():
        cleaned = "".join(ch for ch in token if ch.isalnum())
        if not cleaned:
            continue
        if cleaned in _LOCATION_GENERIC_TOKENS:
            continue
        if len(cleaned) < 3:
            continue
        tokens.add(cleaned)
    return tokens


def _location_specificity_score(requested_location: str, accom_location: str) -> float:
    req_tokens = _location_specific_tokens(requested_location)
    if not req_tokens:
        return 0.0
    accom_tokens = _location_specific_tokens(accom_location)
    if not accom_tokens:
        return 0.0
    overlap = req_tokens.intersection(accom_tokens)
    return max(0.0, min(1.0, float(len(overlap) / max(1, len(req_tokens)))))


def _has_exact_location_landmark_match(requested_location: str, accom_location: str) -> bool:
    """
    Detect stronger street/landmark agreement (e.g. "Peping Gamo", "J.P. Rizal", "Mabini").
    This is intentionally stricter than generic location matching to keep
    exact-place intent ahead of cheaper-but-wrong-location options.
    """
    req_tokens = _location_specific_tokens(requested_location)
    accom_tokens = _location_specific_tokens(accom_location)
    if not req_tokens or not accom_tokens:
        return False
    overlap = req_tokens.intersection(accom_tokens)
    # Require at least one specific token and good overlap ratio.
    return bool(overlap) and (len(overlap) / max(1, len(req_tokens))) >= 0.66


def _extract_requested_room_type_tokens(params: dict) -> list[str]:
    if not isinstance(params, dict):
        return []
    pieces = [
        str(params.get("room_type") or "").strip().lower(),
        str(params.get("room_name") or "").strip().lower(),
        str(params.get("room_reference") or "").strip().lower(),
        str(params.get("selected_room_name") or "").strip().lower(),
    ]
    merged = " ".join(piece for piece in pieces if piece)
    if not merged:
        return []
    hits = []
    for keyword in _ROOM_TYPE_KEYWORDS:
        if re.search(rf"\b{re.escape(keyword)}\b", merged):
            hits.append(keyword)
    deduped = []
    seen = set()
    for token in hits:
        if token in seen:
            continue
        seen.add(token)
        deduped.append(token)
    return deduped


def _room_type_match_ratio(room_name: str, requested_tokens: list[str]) -> float:
    if not requested_tokens:
        return 1.0
    normalized_room_name = str(room_name or "").strip().lower()
    if not normalized_room_name:
        return 0.0
    matched = [token for token in requested_tokens if token in normalized_room_name]
    return max(0.0, min(1.0, float(len(matched) / max(1, len(requested_tokens)))))


def _location_matches_requested(requested_location: str, accom_location: str) -> bool:
    accom_norm = _normalize_location_phrase(accom_location)
    if not accom_norm:
        return False
    aliases = _expand_location_aliases(requested_location)
    generic_aliases = {"bayawan", "bayawan city", "city proper"}
    specific_aliases = [alias for alias in aliases if alias not in generic_aliases]
    probe_aliases = specific_aliases if specific_aliases else aliases
    for alias in probe_aliases:
        if alias and (alias in accom_norm or accom_norm in alias):
            return True
    return False


def _normalize(value: float, min_value: float, max_value: float) -> float:
    if max_value <= min_value:
        return 0.0
    return max(0.0, min(1.0, (value - min_value) / (max_value - min_value)))


def _cnn_score(features: List[float]) -> float:
    """
    Lightweight 1D convolution-style heuristic scorer with a fixed kernel.
    This is intentionally simple and dependency-free, and is not a trained CNN model.
    """
    if not features:
        return 0.0
    if len(features) < 3:
        return sum(features) / len(features)

    kernel = [0.25, 0.5, 0.25]
    conv = []
    for i in range(len(features) - 2):
        window = features[i:i + 3]
        conv.append(sum(w * k for w, k in zip(window, kernel)))
    return sum(conv) / len(conv)


def _decision_tree_penalty(conditions: Iterable[bool]) -> float:
    """
    Simple Decision Tree proxy: penalize failing hard constraints.
    """
    if any(conditions):
        return -10.0
    return 0.0


def _extract_tour_preference_tags_from_text(text: str) -> set[str]:
    lowered = str(text or "").strip().lower()
    if not lowered:
        return set()
    detected = set()
    for canonical, markers in _TOUR_PREFERENCE_ALIASES.items():
        if any(marker in lowered for marker in markers):
            detected.add(canonical)
    return detected


def _collect_requested_tour_preferences(params: dict) -> set[str]:
    requested = set()
    for key in ("preference", "tour_type", "preferred_type", "interest"):
        value = str(params.get(key) or "").strip().lower()
        if value:
            requested.update(_extract_tour_preference_tags_from_text(value))

    tags_value = params.get("preference_tags")
    if isinstance(tags_value, list):
        for item in tags_value:
            tag = str(item or "").strip().lower()
            if tag:
                requested.update(_extract_tour_preference_tags_from_text(tag))

    # Keep only canonical tags + known explicit values derived above.
    return {tag for tag in requested if tag}


def _tokenize_preference_phrase(text: str) -> set[str]:
    raw = str(text or "").strip().lower()
    if not raw:
        return set()
    stopwords = {
        "i", "we", "prefer", "want", "like", "tour", "tours", "package", "packages",
        "day", "trip", "please", "show", "me", "a", "an", "the", "in", "near",
        "and", "or", "how", "about", "what", "right", "now", "there", "any", "are",
    }
    tokens = set()
    for token in raw.replace("/", " ").replace("-", " ").split():
        cleaned = "".join(ch for ch in token if ch.isalnum())
        if len(cleaned) >= 3 and cleaned not in stopwords:
            tokens.add(cleaned)
    return tokens


def get_unavailable_tour_matches(params: dict, limit: int = 3) -> list[str]:
    """
    Return matching published tour titles that currently have no upcoming schedules.
    This supports UX messaging when a preference appears valid but no future run exists.
    """
    now = timezone.now()
    requested_preferences = _collect_requested_tour_preferences(params if isinstance(params, dict) else {})
    requested_preference_text = str(
        (params or {}).get("preference_text")
        or (params or {}).get("preference")
        or (params or {}).get("tour_type")
        or (params or {}).get("preferred_type")
        or (params or {}).get("interest")
        or ""
    ).strip().lower()
    requested_preference_tokens = _tokenize_preference_phrase(requested_preference_text)
    if not requested_preferences and not requested_preference_tokens:
        return []

    schedules = (
        Tour_Schedule.objects.select_related("tour")
        .filter(tour__publication_status="published")
        .exclude(status="cancelled")
    )
    tour_availability: dict[str, bool] = {}
    tour_objects: dict[str, object] = {}
    for schedule in schedules:
        tour_key = str(schedule.tour_id)
        tour_objects[tour_key] = schedule.tour
        has_future = bool(schedule.end_time and schedule.end_time >= now)
        tour_availability[tour_key] = bool(tour_availability.get(tour_key)) or has_future

    scored = []
    for tour_key, tour_obj in tour_objects.items():
        if tour_availability.get(tour_key):
            continue
        name = str(getattr(tour_obj, "tour_name", "") or "").lower()
        desc = str(getattr(tour_obj, "description", "") or "").lower()
        detected_tour_tags = _extract_tour_preference_tags_from_text(f"{name} {desc}")
        tour_tokens = _tokenize_preference_phrase(f"{name} {desc}")

        tag_match_ratio = 0.0
        token_match_ratio = 0.0
        if requested_preferences:
            overlap = requested_preferences.intersection(detected_tour_tags)
            tag_match_ratio = len(overlap) / max(1, len(requested_preferences))
        if requested_preference_tokens:
            token_overlap = requested_preference_tokens.intersection(tour_tokens)
            token_match_ratio = len(token_overlap) / max(1, len(requested_preference_tokens))

        match_score = max(tag_match_ratio, token_match_ratio)
        if match_score > 0:
            scored.append((match_score, str(getattr(tour_obj, "tour_name", "") or "").strip()))

    scored.sort(key=lambda row: (-float(row[0]), row[1].lower()))
    names = []
    for _score, title in scored:
        if title and title not in names:
            names.append(title)
        if len(names) >= max(1, int(limit or 3)):
            break
    return names


def _to_bool_env(value, default=False):
    raw = str(value or "").strip().lower()
    if not raw:
        return bool(default)
    if raw in ("1", "true", "yes", "on", "y"):
        return True
    if raw in ("0", "false", "no", "off", "n"):
        return False
    return bool(default)


def _owner_exclusion_keywords() -> list[str]:
    raw = str(
        os.getenv(
            "CHATBOT_OWNER_EXCLUDE_KEYWORDS",
            getattr(settings, "CHATBOT_OWNER_EXCLUDE_KEYWORDS", "smoke"),
        )
        or ""
    ).strip()
    if not raw:
        return []
    tokens = [str(v).strip().lower() for v in raw.split(",")]
    return [token for token in tokens if token]


def _allow_demo_artifact_fallback():
    override = os.getenv("CHATBOT_ALLOW_DEMO_ARTIFACT_FALLBACK")
    if override not in (None, ""):
        return _to_bool_env(override, default=False)
    settings_override = getattr(settings, "CHATBOT_ALLOW_DEMO_ARTIFACT_FALLBACK", None)
    if settings_override not in (None, ""):
        return _to_bool_env(settings_override, default=False)
    return False


def _resolve_decision_tree_model_path() -> tuple[Path, str]:
    configured = str(
        os.getenv(
            "CHATBOT_DECISION_TREE_MODEL_PATH",
            getattr(settings, "CHATBOT_DECISION_TREE_MODEL_PATH", ""),
        )
        or ""
    ).strip()
    if configured:
        configured_path = Path(configured)
        if not configured_path.is_absolute():
            configured_path = Path(getattr(settings, "BASE_DIR", Path.cwd())) / configured_path
        return configured_path, "configured_env"

    artifacts_root = Path(__file__).resolve().parent.parent / "artifacts"
    if _allow_demo_artifact_fallback():
        demo_path = artifacts_root / "decision_tree_demo" / "decision_tree_demo.pkl"
        if demo_path.exists():
            return demo_path, "demo_fallback"

    final_path = artifacts_root / "decision_tree_final" / "decision_tree_final.pkl"
    if final_path.exists():
        return final_path, "final_default"

    return final_path, "final_required_missing"


def _default_decision_tree_model_path() -> Path:
    path, _source = _resolve_decision_tree_model_path()
    return path


def _load_decision_tree_model(model_path: Optional[Path] = None):
    global _DECISION_TREE_MODEL_CACHE, _DECISION_TREE_MODEL_PATH_CACHE, _DECISION_TREE_MODEL_SOURCE_CACHE

    if model_path is None:
        resolved_path, resolved_source = _resolve_decision_tree_model_path()
    else:
        resolved_path = Path(model_path)
        resolved_source = "manual_override"
    exists = resolved_path.exists()
    demo_allowed = _allow_demo_artifact_fallback()
    fallback_used = resolved_source.startswith("demo") or resolved_source.startswith("surrogate")
    logger.info(
        "DecisionTree load attempt | path=%s | source=%s | file_exists=%s | demo_fallback_allowed=%s | fallback_used=%s",
        str(resolved_path),
        resolved_source,
        exists,
        demo_allowed,
        fallback_used,
    )
    if resolved_source.startswith("demo") or resolved_source.startswith("final_required_missing"):
        logger.warning(
            "DecisionTree fallback/non-final source active | source=%s | path=%s",
            resolved_source,
            str(resolved_path),
        )
    if not resolved_path.exists():
        _DECISION_TREE_MODEL_SOURCE_CACHE = resolved_source
        return None, f"model_not_found:{resolved_path}"

    resolved_str = str(resolved_path)
    if _DECISION_TREE_MODEL_CACHE is not None and _DECISION_TREE_MODEL_PATH_CACHE == resolved_str:
        _DECISION_TREE_MODEL_SOURCE_CACHE = resolved_source
        return _DECISION_TREE_MODEL_CACHE, None

    try:
        with resolved_path.open("rb") as f:
            model = pickle.load(f)
        _DECISION_TREE_MODEL_CACHE = model
        _DECISION_TREE_MODEL_PATH_CACHE = resolved_str
        _DECISION_TREE_MODEL_SOURCE_CACHE = resolved_source
        params = _extract_decision_tree_params(model)
        logger.info(
            "DecisionTree loaded | source=%s | params=%s",
            resolved_source,
            params,
        )
        return model, None
    except Exception as exc:
        _DECISION_TREE_MODEL_SOURCE_CACHE = resolved_source
        return None, f"model_load_error:{exc}"


def _extract_decision_tree_params(model) -> dict:
    classifier = model
    if hasattr(model, "named_steps"):
        classifier = model.named_steps.get("model", model)
    params = {}
    for key in ("criterion", "max_depth", "min_samples_leaf", "min_samples_split", "max_features"):
        params[key] = getattr(classifier, key, None)
    return params


def get_decision_tree_runtime_status(*, force_reload=False) -> dict:
    resolved_path, resolved_source = _resolve_decision_tree_model_path()
    if force_reload:
        model, load_error = _load_decision_tree_model(model_path=resolved_path)
    else:
        model, load_error = _load_decision_tree_model()
    file_exists = resolved_path.exists()
    mtime = ""
    if file_exists:
        try:
            mtime = datetime.fromtimestamp(resolved_path.stat().st_mtime).isoformat()
        except Exception:
            mtime = ""
    params = _extract_decision_tree_params(model) if model is not None else {}
    fallback_used = bool(
        resolved_source.startswith("demo")
        or resolved_source.startswith("final_required_missing")
        or str(load_error or "").startswith("model_not_found")
        or str(load_error or "").startswith("model_load_error")
    )
    expected_match = True
    if params:
        expected_match = all(params.get(k) == v for k, v in _DECISION_TREE_EXPECTED_PARAMS.items())

    status = {
        "path": str(resolved_path),
        "source": resolved_source,
        "file_exists": bool(file_exists),
        "fallback_used": fallback_used,
        "demo_fallback_allowed": bool(_allow_demo_artifact_fallback()),
        "loaded_model_params": params,
        "expected_pruned_params": dict(_DECISION_TREE_EXPECTED_PARAMS),
        "expected_params_match": bool(expected_match) if params else False,
        "file_last_modified": mtime,
        "load_error": str(load_error or ""),
    }
    if fallback_used:
        logger.warning("DecisionTree runtime status indicates fallback usage | status=%s", status)
    return status


def _parse_date(value):
    if isinstance(value, date):
        return value
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        return None


def _resolve_nights_requested(params: dict) -> int:
    nights = _to_int(params.get("nights"), default=0)
    if nights > 0:
        return nights
    check_in = _parse_date(params.get("check_in"))
    check_out = _parse_date(params.get("check_out"))
    if check_in and check_out:
        delta = (check_out - check_in).days
        if delta > 0:
            return delta
    return 1


def _surrogate_decision_tree_score(
    room: Room,
    params: dict,
    *,
    shown_rank: int,
    cnn_confidence: float,
) -> float:
    requested_budget = _to_decimal(params.get("budget"), default=Decimal("0"))
    requested_budget_min = _to_decimal(params.get("budget_min"), default=Decimal("0"))
    requested_guests = _to_int(params.get("guests"), default=1)
    requested_location = str(params.get("location") or "").strip().lower()
    requested_type = str(
        params.get("company_type")
        or params.get("predicted_accommodation_type")
        or ""
    ).strip().lower()
    room_price = _to_decimal(getattr(room, "price_per_night", 0), default=Decimal("0"))
    room_capacity = _to_int(getattr(room, "person_limit", 0), default=0)
    room_location = str(getattr(room.accommodation, "location", "") or "").strip().lower()
    company_type = str(getattr(room.accommodation, "company_type", "") or "").strip().lower()
    prefer_low_price = bool(params.get("prefer_low_price"))
    room_type_tokens = _extract_requested_room_type_tokens(params)

    score = 0.08

    # 1) Exact accommodation type match (highest priority)
    if requested_type and requested_type != "either":
        if requested_type == company_type:
            score += 0.42
        elif requested_type in company_type:
            score += 0.30
        else:
            score -= 0.28
    else:
        score += 0.05

    # 2) Budget closeness (second priority)
    if requested_budget > 0 or requested_budget_min > 0:
        budget_floor = requested_budget_min if requested_budget_min > 0 else Decimal("0")
        budget_ceiling = requested_budget if requested_budget > 0 else Decimal("0")
        in_range = True
        if budget_floor > 0 and room_price < budget_floor:
            in_range = False
        if budget_ceiling > 0 and room_price > budget_ceiling:
            in_range = False

        if in_range:
            # Cap-only intent:
            # - prioritize lower within-cap prices when explicitly budget-conscious
            # - otherwise still prefer lower prices, but with softer spread
            if budget_floor <= 0 and budget_ceiling > 0 and prefer_low_price:
                closeness = max(
                    0.0,
                    min(1.0, float((budget_ceiling - room_price) / budget_ceiling)),
                )
            elif budget_floor <= 0 and budget_ceiling > 0:
                low_price_advantage = max(
                    0.0,
                    min(1.0, float((budget_ceiling - room_price) / budget_ceiling)),
                )
                closeness = 0.5 + (0.5 * low_price_advantage)
            else:
                target = (
                    (budget_floor + budget_ceiling) / Decimal("2")
                    if budget_floor > 0 and budget_ceiling > 0
                    else (budget_ceiling if budget_ceiling > 0 else budget_floor)
                )
                if target > 0:
                    ratio = float(abs(room_price - target) / target)
                    closeness = max(0.0, min(1.0, 1.0 - ratio))
                else:
                    closeness = 1.0
            score += 0.30 * closeness
        else:
            score -= 0.20
    else:
        score += 0.06

    # 3) Location match (third priority)
    if requested_location:
        if requested_location in room_location:
            score += 0.16
        else:
            score -= 0.14
        score += 0.12 * _location_specificity_score(requested_location, room_location)
    else:
        score += 0.05

    # 4) Capacity fit (fourth priority)
    if requested_guests > 0 and room_capacity > 0:
        if requested_guests <= room_capacity:
            fit_ratio = min(1.0, float(requested_guests / max(1, room_capacity)))
            score += 0.10 * fit_ratio
        else:
            score -= 0.30

    if room_type_tokens:
        type_ratio = _room_type_match_ratio(getattr(room, "room_name", ""), room_type_tokens)
        score += 0.12 * type_ratio
        if type_ratio <= 0:
            score -= 0.12

    if shown_rank <= 3:
        score += 0.06
    elif shown_rank <= 6:
        score += 0.03

    score += min(0.08, max(0.0, float(cnn_confidence)) * 0.08)
    return max(0.0, min(1.0, round(float(score), 6)))


def _decision_tree_relevance_score(room: Room, params: dict, *, shown_rank: int, cnn_confidence: float):
    model, _error = _load_decision_tree_model()
    if model is None:
        return _surrogate_decision_tree_score(
            room,
            params,
            shown_rank=shown_rank,
            cnn_confidence=cnn_confidence,
        ), f"surrogate:{_DECISION_TREE_MODEL_SOURCE_CACHE}"

    requested_type = str(
        params.get("company_type")
        or params.get("predicted_accommodation_type")
        or "either"
    ).strip().lower()

    feature_row = {
        "requested_guests": _to_int(params.get("guests"), default=1),
        "requested_budget": float(_to_decimal(params.get("budget"), default=Decimal("0"))),
        "requested_location": str(params.get("location") or "").strip().lower(),
        "requested_accommodation_type": requested_type,
        "room_price_per_night": float(room.price_per_night or 0),
        "room_capacity": _to_int(room.person_limit, default=0),
        "room_available": _to_int(room.current_availability, default=0),
        "accom_location": str(getattr(room.accommodation, "location", "") or "").strip().lower(),
        "company_type": str(getattr(room.accommodation, "company_type", "") or "").strip().lower(),
        "nights_requested": _resolve_nights_requested(params),
        "cnn_confidence": float(max(0.0, min(1.0, cnn_confidence))),
        "shown_rank": int(max(1, shown_rank)),
    }

    try:
        model_input = [feature_row]
        if pd is not None and hasattr(model, "named_steps"):
            # sklearn Pipeline + ColumnTransformer expects named columns.
            model_input = pd.DataFrame([feature_row])

        if hasattr(model, "predict_proba"):
            classes = [str(c).strip().lower() for c in getattr(model, "classes_", [])]
            if not classes and hasattr(model, "named_steps"):
                clf = model.named_steps.get("model")
                classes = [str(c).strip().lower() for c in getattr(clf, "classes_", [])]
            probabilities = model.predict_proba(model_input)[0]
            if "relevant" in classes:
                idx = classes.index("relevant")
                return float(probabilities[idx]), f"model:{_DECISION_TREE_MODEL_SOURCE_CACHE}"
            if probabilities is not None and len(probabilities):
                return float(max(probabilities)), f"model:{_DECISION_TREE_MODEL_SOURCE_CACHE}"

        predicted = model.predict(model_input)[0]
        predicted_label = str(predicted).strip().lower()
        if predicted_label in ("relevant", "1", "true", "yes"):
            return 1.0, f"model:{_DECISION_TREE_MODEL_SOURCE_CACHE}"
        return 0.0, f"model:{_DECISION_TREE_MODEL_SOURCE_CACHE}"
    except Exception:
        return _surrogate_decision_tree_score(
            room,
            params,
            shown_rank=shown_rank,
            cnn_confidence=cnn_confidence,
        ), f"surrogate:{_DECISION_TREE_MODEL_SOURCE_CACHE}"


def predict_accommodation_relevance_from_features(feature_row: dict) -> dict:
    """
    Runtime helper for direct Decision Tree inference from a feature payload.
    Used by internal validation utilities and backend integration checks.
    """
    normalized = {
        "requested_guests": _to_int(feature_row.get("requested_guests"), default=1),
        "requested_budget": float(_to_decimal(feature_row.get("requested_budget"), default=Decimal("0"))),
        "requested_location": str(feature_row.get("requested_location") or "").strip().lower(),
        "requested_accommodation_type": str(feature_row.get("requested_accommodation_type") or "either").strip().lower(),
        "room_price_per_night": float(_to_decimal(feature_row.get("room_price_per_night"), default=Decimal("0"))),
        "room_capacity": _to_int(feature_row.get("room_capacity"), default=1),
        "room_available": _to_int(feature_row.get("room_available"), default=0),
        "accom_location": str(feature_row.get("accom_location") or "").strip().lower(),
        "company_type": str(feature_row.get("company_type") or "").strip().lower(),
        "nights_requested": max(1, _to_int(feature_row.get("nights_requested"), default=1)),
        "cnn_confidence": float(max(0.0, min(1.0, float(feature_row.get("cnn_confidence") or 0.0)))),
        "shown_rank": max(1, _to_int(feature_row.get("shown_rank"), default=1)),
    }

    model, error = _load_decision_tree_model()
    if model is None:
        return {
            "score": 0.0,
            "predicted_label": "unknown",
            "source": f"unavailable:{_DECISION_TREE_MODEL_SOURCE_CACHE}",
            "error": error or "model_unavailable",
            "normalized_features": normalized,
        }

    try:
        model_input = [normalized]
        if pd is not None and hasattr(model, "named_steps"):
            model_input = pd.DataFrame([normalized])

        if hasattr(model, "predict_proba"):
            classes = [str(c).strip().lower() for c in getattr(model, "classes_", [])]
            if not classes and hasattr(model, "named_steps"):
                clf = model.named_steps.get("model")
                classes = [str(c).strip().lower() for c in getattr(clf, "classes_", [])]
            proba = model.predict_proba(model_input)[0]
            if "relevant" in classes:
                idx = classes.index("relevant")
                score = float(proba[idx])
            else:
                score = float(max(proba)) if len(proba) else 0.0
            label = "relevant" if score >= 0.5 else "not_relevant"
            return {
                "score": score,
                "predicted_label": label,
                "source": f"model:{_DECISION_TREE_MODEL_SOURCE_CACHE}",
                "error": "",
                "normalized_features": normalized,
            }

        pred = str(model.predict(model_input)[0]).strip().lower()
        score = 1.0 if pred in ("relevant", "1", "true", "yes") else 0.0
        label = "relevant" if score >= 0.5 else "not_relevant"
        return {
            "score": score,
            "predicted_label": label,
            "source": f"model:{_DECISION_TREE_MODEL_SOURCE_CACHE}",
            "error": "",
            "normalized_features": normalized,
        }
    except Exception as exc:
        return {
            "score": 0.0,
            "predicted_label": "unknown",
            "source": f"error:{_DECISION_TREE_MODEL_SOURCE_CACHE}",
            "error": str(exc),
            "normalized_features": normalized,
        }


def _normalize_amenity_tokens(value) -> List[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        tokens = []
        for item in value:
            for token in str(item).replace(";", ",").split(","):
                cleaned = token.strip().lower()
                if cleaned:
                    tokens.append(cleaned)
        return tokens
    else:
        raw = str(value)
    return [token.strip().lower() for token in raw.replace(";", ",").split(",") if token.strip()]


def _normalize_amenity_alias(token: str) -> str:
    alias_map = {
        "ac": "aircon",
        "air conditioning": "aircon",
        "air-conditioned": "aircon",
        "airconditioned": "aircon",
    }
    lowered = str(token or "").strip().lower()
    return alias_map.get(lowered, lowered)


def _to_bool(value, default=False):
    if isinstance(value, bool):
        return value
    if value in (None, ""):
        return default
    lowered = str(value).strip().lower()
    if lowered in ("1", "true", "yes", "y", "on"):
        return True
    if lowered in ("0", "false", "no", "n", "off"):
        return False
    return default


def build_accommodation_recommendation_trace(room: Room, params: dict) -> dict:
    """
    Build explainable recommendation reasons from existing accommodation logic.
    """
    guests = _to_int(params.get("guests"), default=1)
    budget = _to_decimal(params.get("budget"), default=Decimal("0"))
    budget_min = _to_decimal(params.get("budget_min"), default=Decimal("0"))
    location = str(params.get("location") or "").strip()
    location_anchor = str(params.get("location_anchor") or "").strip()
    company_type = str(params.get("company_type") or "").strip().lower()
    room_type_tokens = _extract_requested_room_type_tokens(params)

    accom = room.accommodation
    price = Decimal(str(room.price_per_night or 0))
    reasons: List[str] = []
    score = 0.0

    # 1) Exact accommodation type match (highest ranking priority)
    type_match = True
    if company_type and company_type != "either":
        accom_type = str(accom.company_type or "").strip().lower()
        if company_type == accom_type:
            reasons.append(f"Exact {company_type.title()} type match")
            score += 0.40
        elif company_type in accom_type:
            reasons.append(f"{company_type.title()} type partial match")
            score += 0.28
        else:
            type_match = False
            reasons.append(f"Different type: {accom.company_type}")
            score -= 0.26
    else:
        reasons.append(f"Type: {accom.company_type}")
        score += 0.05

    # 2) Budget closeness (second ranking priority)
    if budget > 0 or budget_min > 0:
        budget_floor = budget_min if budget_min > 0 else Decimal("0")
        budget_ceiling = budget if budget > 0 else Decimal("0")
        in_budget_range = True
        if budget_floor > 0 and price < budget_floor:
            in_budget_range = False
        if budget_ceiling > 0 and price > budget_ceiling:
            in_budget_range = False

        if in_budget_range:
            if budget_floor <= 0 and budget_ceiling > 0 and bool(params.get("prefer_low_price")):
                # Explicit budget-conscious intent: lower within-cap prices are preferred.
                closeness = max(
                    0.0,
                    min(1.0, float((budget_ceiling - price) / budget_ceiling)),
                )
            elif budget_floor <= 0 and budget_ceiling > 0:
                # General cap intent: still prefer lower valid prices, but avoid extreme dominance.
                low_price_advantage = max(
                    0.0,
                    min(1.0, float((budget_ceiling - price) / budget_ceiling)),
                )
                closeness = 0.5 + (0.5 * low_price_advantage)
            else:
                target = (
                    (budget_floor + budget_ceiling) / Decimal("2")
                    if budget_floor > 0 and budget_ceiling > 0
                    else (budget_ceiling if budget_ceiling > 0 else budget_floor)
                )
                closeness = 1.0
                if target > 0:
                    closeness = max(0.0, min(1.0, 1.0 - float(abs(price - target) / target)))
            if budget_floor > 0 and budget_ceiling > 0:
                reasons.append(
                    f"Budget range fit (PHP {budget_floor:.2f}-{budget_ceiling:.2f}); closeness {closeness:.2f}"
                )
            elif budget_ceiling > 0:
                reasons.append(f"Within budget cap (PHP {price:.2f} <= PHP {budget_ceiling:.2f}); closeness {closeness:.2f}")
            else:
                reasons.append(f"Meets minimum budget floor (PHP {price:.2f} >= PHP {budget_floor:.2f}); closeness {closeness:.2f}")
            score += 0.30 * closeness
        else:
            if budget_floor > 0 and price < budget_floor:
                reasons.append(f"Below preferred minimum budget (PHP {price:.2f} < PHP {budget_floor:.2f})")
            elif budget_ceiling > 0 and price > budget_ceiling:
                reasons.append(f"Above budget cap (PHP {price:.2f} > PHP {budget_ceiling:.2f})")
            score -= 0.20
    else:
        reasons.append("No budget limit provided")
        score += 0.06

    location_match = True
    # 3) Location match (third ranking priority)
    if location_anchor:
        reasons.append(f"Map anchor considered: {location_anchor} (city-proper coverage)")
        score += 0.05
    if location:
        specificity = _location_specificity_score(location, str(accom.location or ""))
        exact_landmark_match = _has_exact_location_landmark_match(location, str(accom.location or ""))
        if _location_matches_requested(location, str(accom.location or "")):
            reasons.append(f"Location match: {accom.location}")
            score += 0.18
            if exact_landmark_match:
                reasons.append("Exact street/landmark alignment")
                score += 0.16
            if specificity > 0:
                reasons.append(f"Specific location overlap {specificity:.2f}")
                score += 0.16 * specificity
            elif not exact_landmark_match:
                score -= 0.04
        else:
            location_match = False
            reasons.append(f"Outside preferred location: {accom.location}")
            score -= 0.15
        if specificity < 0.34 and not exact_landmark_match:
            score -= 0.08
    else:
        reasons.append(f"Located in: {accom.location}")
        score += 0.05

    # 4) Capacity fit (fourth ranking priority)
    guest_fit = True
    if room.person_limit and guests > 0:
        if guests <= room.person_limit:
            fit_ratio = min(1.0, float(guests / max(1, room.person_limit)))
            reasons.append(
                f"Capacity fit ({guests}/{room.person_limit} pax); closeness {fit_ratio:.2f}"
            )
            score += 0.10 * fit_ratio
        else:
            reasons.append(f"Capacity limit ({room.person_limit} pax)")
            guest_fit = False
            score -= 0.40

    room_type_match_ratio = _room_type_match_ratio(room.room_name, room_type_tokens)
    if room_type_tokens:
        if room_type_match_ratio >= 1.0:
            reasons.append(f"Room type match: {', '.join(room_type_tokens)}")
            score += 0.22
        elif room_type_match_ratio > 0:
            reasons.append(
                f"Partial room type match ({room_type_match_ratio:.2f}) for {', '.join(room_type_tokens)}"
            )
            score += 0.10
        else:
            reasons.append(f"Room type mismatch for requested: {', '.join(room_type_tokens)}")
            score -= 0.18

    requested_amenities = _normalize_amenity_tokens(
        params.get("amenities") or params.get("amenity")
    )
    amenity_match_ratio = 1.0
    if requested_amenities:
        normalized_requested = sorted(
            { _normalize_amenity_alias(token) for token in requested_amenities if str(token).strip() }
        )
        searchable_text = " ".join(
            [
                str(room.room_name or ""),
                str(accom.company_name or ""),
                str(accom.location or ""),
                str(accom.company_type or ""),
                str(getattr(accom, "description", "") or ""),
                str(getattr(accom, "accommodation_amenities", "") or ""),
            ]
        ).lower()
        matched = [token for token in normalized_requested if token in searchable_text]
        amenity_match_ratio = (len(matched) / len(normalized_requested)) if normalized_requested else 0.0
        if amenity_match_ratio >= 1.0:
            reasons.append(f"Amenity match: {', '.join(matched)}")
            score += 0.35
        elif amenity_match_ratio > 0:
            reasons.append(
                f"Partial amenity match ({len(matched)}/{len(normalized_requested)}): {', '.join(matched)}"
            )
            score += 0.18
        else:
            reasons.append("No amenity keyword match found in available details")
            score -= 0.15

    preference_tags = (
        params.get("preference_tags")
        if isinstance(params.get("preference_tags"), list)
        else []
    )
    normalized_preference_tags = sorted(
        {
            str(tag or "").strip().lower()
            for tag in preference_tags
            if str(tag or "").strip()
        }
    )
    preference_match_ratio = 1.0
    if normalized_preference_tags:
        searchable_text = " ".join(
            [
                str(room.room_name or ""),
                str(accom.company_name or ""),
                str(accom.location or ""),
                str(accom.company_type or ""),
                str(getattr(accom, "description", "") or ""),
                str(getattr(accom, "accommodation_amenities", "") or ""),
            ]
        ).lower()
        preference_aliases = {
            "quiet": ("quiet", "peaceful", "calm", "serene", "relax"),
            "nature": ("nature", "green", "garden", "fresh air", "view", "scenic", "river", "mountain"),
            "family": ("family", "kids", "children", "group", "suite", "spacious"),
            "clean": ("clean", "hygienic", "sanitary", "well-maintained", "tidy"),
            "accessible": ("terminal", "transport", "commute", "downtown", "highway"),
        }
        matched_preferences = []
        for tag in normalized_preference_tags:
            markers = preference_aliases.get(tag, (tag,))
            if any(marker in searchable_text for marker in markers):
                matched_preferences.append(tag)
        preference_match_ratio = (
            len(matched_preferences) / len(normalized_preference_tags)
            if normalized_preference_tags
            else 0.0
        )
        if preference_match_ratio >= 1.0:
            reasons.append(f"Preference match: {', '.join(matched_preferences)}")
            score += 0.18
        elif preference_match_ratio > 0:
            reasons.append(
                f"Partial preference match ({len(matched_preferences)}/{len(normalized_preference_tags)}): "
                f"{', '.join(matched_preferences)}"
            )
            score += 0.08
        else:
            reasons.append("No strong text match for your descriptive preferences yet")
            score -= 0.10

    if bool(params.get("prefer_low_price")) and budget <= 0:
        if price <= Decimal("1500"):
            reasons.append("Budget-friendly option")
            score += 0.12
        elif price <= Decimal("2500"):
            reasons.append("Mid-range price option")
            score += 0.05
        else:
            reasons.append("Premium-priced option")
            score -= 0.08

    normalized_score = round(max(0.0, min(score, 1.0)), 4)
    if normalized_score >= 0.80:
        match_strength = "High"
    elif normalized_score >= 0.55:
        match_strength = "Medium"
    else:
        match_strength = "Low"

    return {
        "match_score": normalized_score,
        "match_strength": match_strength,
        "reasons": reasons,
        "location_match": location_match,
        "type_match": type_match,
        "guest_fit": guest_fit,
        "exact_location_landmark_match": bool(
            _has_exact_location_landmark_match(location, str(accom.location or ""))
        ) if location else False,
        "location_specificity": round(float(_location_specificity_score(location, str(accom.location or ""))), 3)
        if location
        else 0.0,
        "room_type_match_ratio": round(float(room_type_match_ratio), 3),
        "amenity_match_ratio": round(float(amenity_match_ratio), 3),
        "preference_match_ratio": round(float(preference_match_ratio), 3),
    }


def recommend_tours(params: dict, limit: int = 3) -> List[RecommendationResult]:
    now = timezone.now()
    guests = _to_int(params.get("guests"), default=1)
    budget = _to_decimal(params.get("budget"), default=Decimal("0"))
    duration = _to_int(params.get("duration_days"), default=0)
    requested_preferences = _collect_requested_tour_preferences(params)
    requested_preference_text = str(
        params.get("preference_text")
        or params.get("preference")
        or params.get("tour_type")
        or params.get("preferred_type")
        or params.get("interest")
        or ""
    ).strip().lower()
    requested_preference_tokens = _tokenize_preference_phrase(requested_preference_text)

    schedules = (
        Tour_Schedule.objects.select_related("tour")
        .filter(end_time__gte=now)
        .exclude(status="cancelled")
        .annotate(slots_left=F("slots_available") - F("slots_booked"))
    )

    prices = [float(s.price) for s in schedules] or [0.0]
    min_price, max_price = min(prices), max(prices)

    results = []
    for schedule in schedules:
        slots_left = max(schedule.slots_left, 0)
        if slots_left < guests:
            continue

        price = float(schedule.price)
        price_fit = 1.0 if budget <= 0 else float(min(budget / Decimal(price), 1))
        duration_fit = 1.0 if duration and schedule.duration_days == duration else 0.5 if duration and abs(schedule.duration_days - duration) == 1 else 0.0
        if not duration:
            duration_fit = 0.5

        name = (schedule.tour.tour_name or "").lower()
        desc = (schedule.tour.description or "").lower()
        tour_tokens = _tokenize_preference_phrase(f"{name} {desc}")
        detected_tour_tags = _extract_tour_preference_tags_from_text(f"{name} {desc}")
        tag_match_ratio = 0.0
        token_match_ratio = 0.0
        if requested_preferences:
            overlap = requested_preferences.intersection(detected_tour_tags)
            tag_match_ratio = len(overlap) / max(1, len(requested_preferences))
        if requested_preference_tokens:
            token_overlap = requested_preference_tokens.intersection(tour_tokens)
            token_match_ratio = len(token_overlap) / max(1, len(requested_preference_tokens))
        if requested_preferences or requested_preference_tokens:
            preference_fit = max(tag_match_ratio, token_match_ratio)
        else:
            preference_fit = 0.3
        availability_fit = min(slots_left, 10) / 10.0
        price_norm = 1.0 - _normalize(price, min_price, max_price)

        features = [price_fit, duration_fit, preference_fit, availability_fit, price_norm]
        cnn_score = _cnn_score(features)

        penalty = _decision_tree_penalty([
            budget > 0 and price > float(budget),
            duration > 0 and schedule.duration_days not in (duration, duration - 1, duration + 1),
        ])

        score = cnn_score + penalty
        results.append(
            RecommendationResult(
                title=schedule.tour.tour_name,
                subtitle=f"{schedule.sched_id} | PHP {schedule.price} per guest | {schedule.duration_days} day(s)",
                score=score,
                meta={
                    "sched_id": schedule.sched_id,
                    "detected_tour_tags": sorted(detected_tour_tags),
                    "requested_tour_preferences": sorted(requested_preferences),
                    "requested_preference_tokens": sorted(requested_preference_tokens),
                    "matched_preference_tokens": sorted(
                        requested_preference_tokens.intersection(tour_tokens)
                    ),
                    "tag_match_ratio": round(float(tag_match_ratio), 4),
                    "token_match_ratio": round(float(token_match_ratio), 4),
                },
            )
        )

    results.sort(key=lambda item: item.score, reverse=True)
    return results[:limit]


def recommend_accommodations(params: dict, limit: int = 3) -> List[RecommendationResult]:
    results, _diagnostics = recommend_accommodations_with_diagnostics(params, limit=limit)
    return results


def _build_accommodation_room_queryset(
    params: dict,
    *,
    apply_location: bool,
    apply_company_type: bool,
    apply_budget: bool,
):
    guests = _to_int(params.get("guests"), default=1)
    budget = _to_decimal(params.get("budget"), default=Decimal("0"))
    budget_min = _to_decimal(params.get("budget_min"), default=Decimal("0"))
    location = str(params.get("location") or "").strip().lower()
    company_type = str(params.get("company_type") or "").strip().lower()

    room_qs = (
        Room.objects.select_related("accommodation")
        .filter(status="AVAILABLE")
        .filter(current_availability__gte=1)
        .filter(accommodation__owner__isnull=False)
        .filter(accommodation__owner__is_active=True)
        .filter(accommodation__owner__groups__name__iexact="accommodation_owner")
        .exclude(accommodation__owner__groups__name__iexact="accommodation_owner_pending")
        .exclude(accommodation__owner__groups__name__iexact="accommodation_owner_declined")
    )
    room_qs = apply_approved_accommodation_scope(room_qs, accommodation_path="accommodation")

    for keyword in _owner_exclusion_keywords():
        room_qs = room_qs.exclude(accommodation__owner__email__icontains=keyword)
        room_qs = room_qs.exclude(accommodation__owner__username__icontains=keyword)
        room_qs = room_qs.exclude(accommodation__owner__first_name__icontains=keyword)
        room_qs = room_qs.exclude(accommodation__owner__last_name__icontains=keyword)
        room_qs = room_qs.exclude(accommodation__company_name__icontains=keyword)

    if company_type:
        if apply_company_type:
            if company_type == "either":
                room_qs = room_qs.filter(
                    Q(accommodation__company_type__iexact="hotel") |
                    Q(accommodation__company_type__iexact="inn")
                )
            elif company_type in {"hotel", "inn"}:
                # Strict type filter: only exact Hotel/Inn matches are allowed.
                room_qs = room_qs.filter(accommodation__company_type__iexact=company_type)
            else:
                room_qs = room_qs.filter(accommodation__company_type__icontains=company_type)
    else:
        room_qs = room_qs.filter(
            Q(accommodation__company_type__iexact="hotel") |
            Q(accommodation__company_type__iexact="inn")
        )

    if apply_location and location:
        aliases = _expand_location_aliases(location)
        generic_aliases = {"bayawan", "bayawan city", "city proper"}
        specific_aliases = [alias for alias in aliases if alias not in generic_aliases]
        probe_aliases = specific_aliases if specific_aliases else aliases
        if probe_aliases:
            location_filter = Q()
            for alias in probe_aliases:
                location_filter |= Q(accommodation__location__icontains=alias)
            room_qs = room_qs.filter(location_filter)
        else:
            room_qs = room_qs.filter(accommodation__location__icontains=location)

    # Budget guard:
    # strict passes enforce a hard budget cap; controlled soft recovery handles
    # no-match scenarios separately in the fallback phase.
    if apply_budget and budget > 0:
        room_qs = room_qs.filter(price_per_night__lte=Decimal(budget))
    if apply_budget and budget_min > 0:
        room_qs = room_qs.filter(price_per_night__gte=budget_min)

    return room_qs.distinct(), guests


def _build_accommodation_results(room_qs, *, guests: int, params: dict) -> List[RecommendationResult]:
    best_by_accom: dict[int, RecommendationResult] = {}
    predicted_type = str(params.get("predicted_accommodation_type") or "").strip().lower()
    cnn_confidence = float(max(0.0, min(1.0, float(params.get("predicted_accommodation_confidence") or 0.0))))

    for shown_rank, room in enumerate(room_qs, start=1):
        accom = room.accommodation
        if room.person_limit and guests > room.person_limit:
            continue

        trace = build_accommodation_recommendation_trace(room, params)
        base_score = float(trace.get("match_score") or 0.0)
        room_type = str(getattr(accom, "company_type", "") or "").strip().lower()
        cnn_type_match = bool(predicted_type and predicted_type in room_type)
        cnn_alignment = 0.0
        if predicted_type and cnn_confidence > 0:
            weight = min(0.20, 0.30 * cnn_confidence)
            cnn_alignment = weight if cnn_type_match else (-0.5 * weight)

        dt_score, dt_source = _decision_tree_relevance_score(
            room,
            params,
            shown_rank=shown_rank,
            cnn_confidence=cnn_confidence,
        )
        if dt_score is None:
            score = max(0.0, min(1.0, base_score + cnn_alignment))
            scoring_mode = "hybrid_fallback_heuristic"
        else:
            score = max(0.0, min(1.0, (0.35 * float(dt_score)) + (0.60 * base_score) + (0.05 * cnn_alignment)))
            scoring_mode = (
                "hybrid_textcnn_decisiontree"
                if str(dt_source).startswith("model")
                else "hybrid_textcnn_surrogate_tree"
            )

        trace["decision_tree_score"] = None if dt_score is None else round(float(dt_score), 4)
        trace["decision_tree_source"] = dt_source
        trace["cnn_alignment"] = round(float(cnn_alignment), 4)
        trace["cnn_predicted_type"] = predicted_type or ""
        trace["cnn_confidence"] = round(float(cnn_confidence), 4)
        trace["cnn_type_match"] = bool(cnn_type_match)
        trace["scoring_mode"] = scoring_mode
        candidate = RecommendationResult(
            title=f"{accom.company_name} - {room.room_name}",
            subtitle=f"{accom.location} | PHP {room.price_per_night} per night | {room.person_limit} pax",
            score=score,
            meta={
                "room_id": room.room_id,
                "accom_id": accom.accom_id,
                "company_name": str(getattr(accom, "company_name", "") or "").strip(),
                "room_name": str(getattr(room, "room_name", "") or "").strip(),
                "location": str(getattr(accom, "location", "") or "").strip(),
                "description": str(getattr(accom, "description", "") or "").strip(),
                "price_per_night": str(getattr(room, "price_per_night", "") or "").strip(),
                "person_limit": _to_int(getattr(room, "person_limit", 0), default=0),
                "phone_number": str(getattr(accom, "phone_number", "") or "").strip(),
                "email_address": str(getattr(accom, "email_address", "") or "").strip(),
                "official_booking_url": str(getattr(accom, "official_booking_url", "") or "").strip(),
                "official_contact_url": str(getattr(accom, "official_contact_url", "") or "").strip(),
                "profile_image_url": _safe_media_url(getattr(accom, "profile_picture", None)),
                "trace": trace,
                "decision_tree_score": None if dt_score is None else round(float(dt_score), 6),
                "decision_tree_source": dt_source,
                "cnn_alignment": round(float(cnn_alignment), 6),
                "scoring_mode": scoring_mode,
            },
        )
        accom_id = _to_int(getattr(accom, "accom_id", 0), default=0)
        existing = best_by_accom.get(accom_id)
        if existing is None:
            best_by_accom[accom_id] = candidate
            continue
        candidate_price = _to_decimal(candidate.meta.get("price_per_night"), default=Decimal("0"))
        existing_price = _to_decimal(existing.meta.get("price_per_night"), default=Decimal("0"))
        if candidate.score > existing.score + 1e-9:
            best_by_accom[accom_id] = candidate
        elif abs(candidate.score - existing.score) <= 1e-9 and candidate_price < existing_price:
            best_by_accom[accom_id] = candidate

    results = list(best_by_accom.values())
    results.sort(
        key=lambda item: (
            -item.score,
            -int(bool(((item.meta or {}).get("trace") or {}).get("exact_location_landmark_match"))),
            -float(((item.meta or {}).get("trace") or {}).get("location_specificity") or 0.0),
            -float(((item.meta or {}).get("trace") or {}).get("room_type_match_ratio") or 0.0),
            -float(((item.meta or {}).get("trace") or {}).get("amenity_match_ratio") or 0.0),
            -int(bool(((item.meta or {}).get("trace") or {}).get("location_match"))),
            -int(bool(((item.meta or {}).get("trace") or {}).get("guest_fit"))),
            _to_decimal((item.meta or {}).get("price_per_night"), default=Decimal("0")),
            str((item.meta or {}).get("company_name") or "").lower(),
        )
    )
    return results


def recommend_accommodations_with_diagnostics(params: dict, limit: int = 3):
    diagnostics = {
        "fallback_applied": "none",
        "fallback_reason": "",
        "fallback_reason_codes": [],
        "no_match_reasons": [],
        "suggested_budget_min": None,
    }

    broaden_location = _to_bool(params.get("broaden_location"), default=False)
    broaden_type = _to_bool(params.get("broaden_company_type"), default=False)
    enable_soft_recovery = _to_bool(params.get("enable_soft_recovery"), default=True)

    # Pass tuple: (name, apply_location, apply_company_type, apply_budget)
    passes = [("strict", True, True, True)]
    if broaden_location:
        passes.append(("relaxed_location", False, True, True))
    if broaden_type:
        passes.append(("relaxed_type", True, False, True))
    if broaden_location and broaden_type:
        passes.append(("relaxed_location_and_type", False, False, True))

    seen_passes = set()
    final_results: List[RecommendationResult] = []
    min_score_by_pass = {
        "strict": 0.10,
        "relaxed_location": 0.15,
        "relaxed_type": 0.15,
        "relaxed_location_and_type": 0.20,
    }

    for pass_name, apply_location, apply_company_type, apply_budget in passes:
        room_qs, guests = _build_accommodation_room_queryset(
            params,
            apply_location=apply_location,
            apply_company_type=apply_company_type,
            apply_budget=apply_budget,
        )
        results = _build_accommodation_results(room_qs, guests=guests, params=params)
        if results:
            top_score = float(results[0].score) if results else 0.0
            min_required = float(min_score_by_pass.get(pass_name, 0.0))
            if top_score >= min_required:
                diagnostics["fallback_applied"] = pass_name
                final_results = results[:limit]
                break
        seen_passes.add((apply_location, apply_company_type, apply_budget))

    # Strict-then-soft recovery:
    # If strict (and optional explicit broaden passes) fails, run controlled relaxations
    # to recover servable recommendations without changing metric formulas.
    if not final_results and enable_soft_recovery:
        soft_recovery_passes = [
            (
                "soft_budget_recovery",
                True,
                True,
                False,
                ["budget_relaxed_after_strict_no_match"],
            ),
            (
                "soft_type_recovery",
                True,
                False,
                True,
                ["type_relaxed_after_strict_no_match"],
            ),
            (
                "soft_location_recovery",
                False,
                True,
                True,
                ["location_relaxed_after_strict_no_match"],
            ),
            (
                "soft_budget_type_recovery",
                True,
                False,
                False,
                ["budget_relaxed_after_strict_no_match", "type_relaxed_after_strict_no_match"],
            ),
            (
                "soft_budget_location_recovery",
                False,
                True,
                False,
                ["budget_relaxed_after_strict_no_match", "location_relaxed_after_strict_no_match"],
            ),
        ]
        min_score_by_soft_pass = {
            "soft_budget_recovery": 0.14,
            "soft_type_recovery": 0.14,
            "soft_location_recovery": 0.14,
            "soft_budget_type_recovery": 0.16,
            "soft_budget_location_recovery": 0.16,
        }

        for (
            pass_name,
            apply_location,
            apply_company_type,
            apply_budget,
            reason_codes,
        ) in soft_recovery_passes:
            pass_key = (apply_location, apply_company_type, apply_budget)
            if pass_key in seen_passes:
                continue

            room_qs, guests = _build_accommodation_room_queryset(
                params,
                apply_location=apply_location,
                apply_company_type=apply_company_type,
                apply_budget=apply_budget,
            )
            results = _build_accommodation_results(room_qs, guests=guests, params=params)
            seen_passes.add(pass_key)
            if not results:
                continue

            top_score = float(results[0].score) if results else 0.0
            min_required = float(min_score_by_soft_pass.get(pass_name, 0.0))
            if top_score < min_required:
                continue

            diagnostics["fallback_applied"] = pass_name
            diagnostics["fallback_reason_codes"] = list(reason_codes)
            diagnostics["fallback_reason"] = ", ".join(list(reason_codes))
            final_results = results[:limit]
            break

    if final_results:
        return final_results, diagnostics

    budget = _to_decimal(params.get("budget"), default=Decimal("0"))
    location = str(params.get("location") or "").strip()
    company_type = str(params.get("company_type") or "").strip().lower()

    # Analyze constraints without budget to provide actionable guidance.
    analysis_qs, guests = _build_accommodation_room_queryset(
        params,
        apply_location=True,
        apply_company_type=True,
        apply_budget=False,
    )
    analysis_candidates = list(analysis_qs)
    analysis_candidates = [
        room for room in analysis_candidates
        if not room.person_limit or guests <= room.person_limit
    ]

    if budget > 0 and analysis_candidates:
        min_price = min(Decimal(str(room.price_per_night or 0)) for room in analysis_candidates)
        if min_price > budget:
            diagnostics["no_match_reasons"].append("budget_too_low")
            diagnostics["suggested_budget_min"] = float(min_price)

    if location:
        location_relaxed_qs, location_guests = _build_accommodation_room_queryset(
            params,
            apply_location=False,
            apply_company_type=True,
            apply_budget=True,
        )
        location_relaxed_candidates = [
            room for room in location_relaxed_qs
            if not room.person_limit or location_guests <= room.person_limit
        ]
        if location_relaxed_candidates:
            diagnostics["no_match_reasons"].append("location_too_narrow")

    if company_type:
        type_relaxed_qs, type_guests = _build_accommodation_room_queryset(
            params,
            apply_location=True,
            apply_company_type=False,
            apply_budget=True,
        )
        type_relaxed_candidates = [
            room for room in type_relaxed_qs
            if not room.person_limit or type_guests <= room.person_limit
        ]
        if type_relaxed_candidates:
            diagnostics["no_match_reasons"].append("type_too_narrow")

    if not diagnostics["no_match_reasons"]:
        diagnostics["no_match_reasons"].append("no_available_match")

    return [], diagnostics


def calculate_accommodation_billing(room: Room, check_in, check_out) -> Decimal:
    nights = max((check_out - check_in).days, 1)
    return Decimal(room.price_per_night) * Decimal(nights)
