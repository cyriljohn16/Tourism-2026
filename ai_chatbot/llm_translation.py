import importlib
import json
import os
import re

try:
    from google import genai  # type: ignore
except Exception:
    try:
        genai = importlib.import_module("google.genai")
    except Exception:
        genai = None


def _is_translation_enabled():
    raw = str(os.getenv("CHATBOT_GEMINI_TRANSLATION_ENABLED", "1") or "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def _gemini_client():
    if not _is_translation_enabled() or genai is None:
        return None
    api_key = str(os.getenv("GEMINI_API_KEY", "") or "").strip()
    if not api_key:
        return None
    try:
        return genai.Client(api_key=api_key)
    except Exception:
        return None


def _gemini_model():
    return str(os.getenv("GEMINI_MODEL", "gemini-2.5-flash-lite") or "").strip() or "gemini-2.5-flash-lite"


def _extract_json_payload(text):
    raw = str(text or "").strip()
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        pass
    match = re.search(r"\{[\s\S]*\}", raw)
    if not match:
        return {}
    try:
        return json.loads(match.group(0))
    except Exception:
        return {}


def _normalize_language_code(value):
    lang = str(value or "").strip().lower()
    if not lang:
        return "en"
    if lang in ("english", "eng", "en-us", "en-gb"):
        return "en"
    aliases = {
        "tagalog": "tl",
        "fil": "tl",
        "filipino": "tl",
        "cebuano": "ceb",
        "bisaya": "ceb",
        "spanish": "es",
        "espanol": "es",
    }
    normalized = aliases.get(lang, lang)
    # Keep translations within the app-supported UI languages to avoid
    # accidental back-translation to unrelated languages (e.g., Dutch).
    return normalized if normalized in {"en", "tl", "ceb", "es"} else "en"


def _looks_like_english_chat_command(text):
    normalized = re.sub(r"\s+", " ", str(text or "").strip().lower())
    if not normalized:
        return False
    if not normalized.isascii():
        return False
    # Performance fast-path: common English command vocabulary used by the app.
    command_markers = (
        "help",
        "plan",
        "trip",
        "budget",
        "hotel",
        "inn",
        "accommodation",
        "stay",
        "tour",
        "book",
        "directions",
        "how to",
        "how far",
        "where",
        "show",
        "summary",
        "report",
        "yes",
        "no",
    )
    return any(marker in normalized for marker in command_markers)


def translate_to_english(user_input):
    text = str(user_input or "").strip()
    if not text:
        return "", "en"
    # Numeric/ID-only replies (e.g., "2", "1500", "room 12") should not trigger
    # language detection because they can be misclassified by external models.
    if not re.search(r"[A-Za-z]", text):
        return text, "en"
    if _looks_like_english_chat_command(text):
        return text, "en"

    client = _gemini_client()
    if client is None:
        return text, "en"

    prompt = (
        "Detect the language of the user text and translate it into English.\n"
        "Rules:\n"
        "- Translation only.\n"
        "- Preserve meaning exactly.\n"
        "- Do not answer the user.\n"
        "- Do not add or remove facts.\n"
        "- Output JSON only with keys detected_language and translated_text.\n\n"
        f"User text:\n{text}"
    )

    try:
        response = client.models.generate_content(
            model=_gemini_model(),
            contents=prompt,
        )
        parsed = _extract_json_payload(getattr(response, "text", ""))
        translated_text = str(parsed.get("translated_text") or "").strip()
        detected_language = _normalize_language_code(parsed.get("detected_language"))
        if not translated_text:
            translated_text = text
        if not detected_language:
            detected_language = "en"
        return translated_text, detected_language
    except Exception:
        return text, "en"


def translate_to_user_language(response_text, target_language):
    reply = str(response_text or "").strip()
    language = _normalize_language_code(target_language)
    if not reply:
        return reply
    if language in ("en", "english"):
        return reply

    client = _gemini_client()
    if client is None:
        return reply

    prompt = (
        "Translate the following assistant response to the target language.\n"
        "Rules:\n"
        "- Translation only.\n"
        "- Preserve all facts, values, dates, IDs, links, and constraints exactly.\n"
        "- Do not add explanations.\n"
        "- Output plain translated text only.\n\n"
        f"Target language: {language}\n"
        f"Assistant response:\n{reply}"
    )

    try:
        response = client.models.generate_content(
            model=_gemini_model(),
            contents=prompt,
        )
        translated = str(getattr(response, "text", "") or "").strip()
        return translated or reply
    except Exception:
        return reply


def translation_runtime_health():
    key_present = bool(str(os.getenv("GEMINI_API_KEY", "") or "").strip())
    module_loaded = genai is not None
    enabled = _is_translation_enabled()
    client_ready = bool(_gemini_client())
    return {
        "enabled": enabled,
        "module_loaded": module_loaded,
        "api_key_present": key_present,
        "client_ready": client_ready,
        "model": _gemini_model(),
    }
