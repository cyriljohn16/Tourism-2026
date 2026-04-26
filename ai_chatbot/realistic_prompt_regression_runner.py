import json
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from django.contrib.auth.models import Group
from django.test import Client

from ai_chatbot.demo_prompt_regression_runner import (
    _ensure_employee,
    _ensure_guest,
    _ensure_tour_and_assignments,
    _new_client_for_role,
)
from ai_chatbot.models import ChatbotLog


DEFAULT_PROGRESS_PATH = Path("thesis_data_templates/realistic_regression_progress.jsonl")
DEFAULT_TIMEOUT_AUDIT_PATH = Path("thesis_data_templates/realistic_timeout_audit.jsonl")


@dataclass
class PromptCase:
    key: str
    group: str
    prompt: str
    mode: str = "single"  # single | continuous
    turn_index: int = -1


def _extract_detected_intent(response_payload: dict[str, Any]) -> str:
    intent_classifier = response_payload.get("intent_classifier") if isinstance(response_payload, dict) else {}
    if not isinstance(intent_classifier, dict):
        return ""
    top3 = intent_classifier.get("top_3") if isinstance(intent_classifier.get("top_3"), list) else []
    if top3 and isinstance(top3[0], dict):
        return str(top3[0].get("intent") or top3[0].get("raw_label") or "").strip().lower()
    source = str(intent_classifier.get("source") or "").strip().lower()
    if source == "deterministic_pre_route":
        return "deterministic_pre_route"
    return ""


def _build_cases() -> list[PromptCase]:
    groups: dict[str, list[str]] = {
        "trip_planning": [
            "help me plan my stay in bayawan",
            "can you help me plan my trip",
            "plan my bayawan trip with 5000 budget",
            "i have 10k budget for bayawan, can you plan it",
            "how can i spend 8000 in bayawan for 2 days",
        ],
        "accommodation": [
            "hotel in bayawan",
            "where can i stay in bayawan",
            "i need a place to stay",
            "hotel in suba for 2 people",
            "cheap inn near city center",
            "hotel under 1500 per night",
            "affordable stay in bayawan for family",
            "2 people hotel suba cheap",
            "budget stay bayawan under 1k",
            "inn near poblacion for couple",
        ],
        "tour_activity": [
            "what can i do in bayawan",
            "what are the tourist spots in bayawan",
            "show me tours",
            "any activities in bayawan",
            "something fun to do in bayawan",
            "day tours in bayawan",
            "food tours",
            "nature tours",
        ],
        "mixed_realistic": [
            "i want to go somewhere nice maybe a hotel",
            "hotel for 2 people and what can we do there",
            "cheap stay and some activities",
            "place to stay and tour suggestions",
            "recommend a hotel and things to do",
        ],
        "flow5_context": [
            "i want to go somewhere nice",
            "maybe like a place to stay",
            "2 people",
            "suba under 1500",
            "how far is that from city center",
            "what can we do there",
            "that food tour looks nice",
            "may 10 for 2",
            "yes",
        ],
    }
    cases: list[PromptCase] = []
    for group, prompts in groups.items():
        for idx, prompt in enumerate(prompts):
            mode = "continuous" if group == "flow5_context" else "single"
            key = f"{group}:{idx}"
            turn_index = idx if mode == "continuous" else -1
            cases.append(PromptCase(key=key, group=group, prompt=prompt, mode=mode, turn_index=turn_index))
    return cases


def _contains_forbidden_accommodation_booking_language(text: str) -> bool:
    t = str(text or "").strip().lower()
    if not t:
        return False
    forbidden = (
        "book room",
        "confirm accommodation booking",
        "reservation approved",
        "complete accommodation booking",
    )
    return any(token in t for token in forbidden)


def _evaluate_row(row: dict[str, Any], *, flow5_index: int = -1) -> tuple[bool, str]:
    group = str(row.get("group") or "")
    prompt = str(row.get("prompt") or "").strip().lower()
    resolved = str(row.get("resolved_intent") or "").strip().lower()
    response = str(row.get("response_summary") or "").strip().lower()
    fallback_used = bool(row.get("fallback_used"))
    timed_out = bool(row.get("timed_out"))

    if timed_out:
        return False, "prompt_timeout"

    if group == "trip_planning":
        if resolved != "plan_bayawan_stay":
            return False, "planning_intent_not_resolved"
        return True, ""

    if group == "accommodation":
        if resolved == "plan_bayawan_stay":
            return False, "accommodation_routed_to_planning"
        if resolved not in {"get_accommodation_recommendation", "gethotelrecommendation", "clarification"}:
            return False, "accommodation_wrong_intent"
        if _contains_forbidden_accommodation_booking_language(response):
            return False, "forbidden_internal_accommodation_booking_language"
        return True, ""

    if group == "tour_activity":
        if resolved not in {"get_recommendation", "get_tourism_information", "clarification"}:
            return False, "tour_query_wrong_intent"
        if "something went wrong" in response and "no tours available right now" not in response:
            return False, "unclean_tour_no_data_response"
        return True, ""

    if group == "mixed_realistic":
        if resolved in {"out_of_scope", ""}:
            return False, "mixed_query_unresolved"
        if resolved == "plan_bayawan_stay" and "plan" not in prompt and "budget" not in prompt:
            return False, "mixed_query_unexpected_planning"
        return True, ""

    if group == "flow5_context":
        expected_by_turn = {
            0: {"clarification"},
            1: {"get_accommodation_recommendation", "clarification"},
            2: {"get_accommodation_recommendation"},
            3: {"get_accommodation_recommendation"},
            4: {"travel_guidance", "clarification"},
            5: {"get_recommendation", "get_tourism_information"},
            6: {"book_tour_via_link", "get_recommendation"},
            7: {"book_tour_via_link"},
            8: {"book_tour_via_link"},
        }
        expected = expected_by_turn.get(flow5_index, set())
        if expected and resolved not in expected:
            return False, f"flow5_turn_{flow5_index + 1}_unexpected_intent"
        if flow5_index in {4, 8} and fallback_used:
            return False, f"flow5_turn_{flow5_index + 1}_unexpected_fallback"
        return True, ""

    return False, "unknown_group"


def _load_progress(progress_path: Path) -> dict[str, dict[str, Any]]:
    if not progress_path.exists():
        return {}
    rows: dict[str, dict[str, Any]] = {}
    for line in progress_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except Exception:
            continue
        key = str(row.get("case_key") or "").strip()
        if key:
            rows[key] = row
    return rows


def _append_progress(progress_path: Path, row: dict[str, Any]) -> None:
    progress_path.parent.mkdir(parents=True, exist_ok=True)
    with progress_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _find_timeout_cases(progress_path: Path) -> list[PromptCase]:
    rows = _load_progress(progress_path)
    case_map = {case.key: case for case in _build_cases()}
    timeout_cases: list[PromptCase] = []
    for key, row in rows.items():
        if str(row.get("failure_reason") or "") != "prompt_timeout":
            continue
        case = case_map.get(key)
        if case is not None:
            timeout_cases.append(case)
    timeout_cases.sort(key=lambda c: (c.group, c.turn_index))
    return timeout_cases


def _warmup_runtime(
    client: Client,
    *,
    timeout_seconds: float = 12.0,
    executor: ThreadPoolExecutor | None = None,
) -> dict[str, Any]:
    from ai_chatbot.views import _classify_intent_with_text_cnn, _classify_intent_and_extract_params

    warmup: dict[str, Any] = {"steps": [], "ok": True}

    start = time.perf_counter()
    try:
        _classify_intent_with_text_cnn("warmup intent model")
        warmup["steps"].append(
            {"step": "cnn_model_load", "elapsed_s": round(time.perf_counter() - start, 3), "ok": True}
        )
    except Exception as exc:
        warmup["ok"] = False
        warmup["steps"].append(
            {
                "step": "cnn_model_load",
                "elapsed_s": round(time.perf_counter() - start, 3),
                "ok": False,
                "error": str(exc)[:160],
            }
        )

    start = time.perf_counter()
    try:
        _classify_intent_and_extract_params("warmup plan my bayawan stay", actor={"role": "guest"})
        warmup["steps"].append(
            {"step": "vectorization_init", "elapsed_s": round(time.perf_counter() - start, 3), "ok": True}
        )
    except Exception as exc:
        warmup["ok"] = False
        warmup["steps"].append(
            {
                "step": "vectorization_init",
                "elapsed_s": round(time.perf_counter() - start, 3),
                "ok": False,
                "error": str(exc)[:160],
            }
        )

    timed_out, elapsed_s, status_code, _ = _post_with_timeout(
        client,
        "hello",
        timeout_seconds,
        executor=executor,
    )
    warmup["steps"].append(
        {
            "step": "dummy_chat_call",
            "elapsed_s": elapsed_s,
            "status_code": status_code,
            "timed_out": bool(timed_out),
            "ok": bool((not timed_out) and status_code == 200),
        }
    )
    if timed_out or status_code != 200:
        warmup["ok"] = False
    return warmup


def _profile_prompt_components(prompt: str) -> dict[str, Any]:
    from ai_chatbot.views import (
        _classify_intent_and_extract_params,
        _deterministic_intent_route,
        _extract_params_with_confidence,
        _get_recommendations,
        _looks_like_tour_request,
        _safe_get_accommodation_recommendations,
    )

    actor = {"role": "guest"}
    lower_prompt = str(prompt or "").strip().lower()
    profile: dict[str, Any] = {
        "deterministic_routing_s": None,
        "cnn_extract_s": None,
        "recommender_query_s": None,
        "tour_query_s": None,
        "template_render_s": None,
        "notes": [],
    }

    start = time.perf_counter()
    deterministic_intent = _deterministic_intent_route(actor=actor, message=prompt)
    profile["deterministic_routing_s"] = round(time.perf_counter() - start, 3)
    profile["deterministic_intent"] = str(deterministic_intent or "")

    start = time.perf_counter()
    parsed = _classify_intent_and_extract_params(prompt, actor=actor)
    profile["cnn_extract_s"] = round(time.perf_counter() - start, 3)
    params = parsed.get("params") if isinstance(parsed.get("params"), dict) else {}
    parsed_intent = str(parsed.get("intent") or "").strip().lower()
    profile["parsed_intent"] = parsed_intent

    is_accommodation = any(token in lower_prompt for token in ("hotel", "inn", "accommodation", "stay", "place to stay"))
    if is_accommodation:
        start = time.perf_counter()
        try:
            _safe_get_accommodation_recommendations(params)
            profile["recommender_query_s"] = round(time.perf_counter() - start, 3)
        except Exception as exc:
            profile["recommender_query_s"] = round(time.perf_counter() - start, 3)
            profile["notes"].append(f"accommodation_reco_error:{str(exc)[:120]}")

    if _looks_like_tour_request(prompt) or "tour" in lower_prompt or parsed_intent == "get_recommendation":
        start = time.perf_counter()
        try:
            _get_recommendations(params if isinstance(params, dict) else {})
            profile["tour_query_s"] = round(time.perf_counter() - start, 3)
        except Exception as exc:
            profile["tour_query_s"] = round(time.perf_counter() - start, 3)
            profile["notes"].append(f"tour_query_error:{str(exc)[:120]}")

    # Kept for requested breakdown; chat API here returns JSON (no HTML template render path).
    profile["template_render_s"] = 0.0
    return profile


def _ensure_test_users():
    guest_user = _ensure_guest(
        username="realistic_guest_regression",
        email="realistic.guest.regression@ibayaw.local",
        first_name="Realistic",
        last_name="Guest",
    )
    owner_user = _ensure_guest(
        username="realistic_owner_regression",
        email="realistic.owner.regression@ibayaw.local",
        first_name="Realistic",
        last_name="Owner",
    )
    owner_group, _ = Group.objects.get_or_create(name="accommodation_owner")
    owner_user.groups.add(owner_group)
    admin_user = _ensure_guest(
        username="realistic_admin_regression",
        email="realistic.admin.regression@ibayaw.local",
        first_name="Realistic",
        last_name="Admin",
    )
    admin_user.is_staff = True
    admin_user.save(update_fields=["is_staff"])
    employee = _ensure_employee()
    _ensure_tour_and_assignments(employee)
    return guest_user, owner_user, admin_user, employee


def _post_with_timeout(
    client: Client,
    prompt: str,
    timeout_seconds: float,
    *,
    executor: ThreadPoolExecutor | None = None,
) -> tuple[bool, float, int, dict[str, Any]]:
    start = time.perf_counter()
    own_executor = executor is None
    exec_ref = executor or ThreadPoolExecutor(max_workers=1)
    fut = exec_ref.submit(
        client.post,
        "/api/chat/",
        data=json.dumps({"message": prompt}),
        content_type="application/json",
    )
    try:
        http_resp = fut.result(timeout=max(0.5, float(timeout_seconds)))
        elapsed = round(time.perf_counter() - start, 3)
        payload: dict[str, Any]
        try:
            payload = json.loads(http_resp.content.decode("utf-8"))
        except Exception:
            payload = {}
        return False, elapsed, int(http_resp.status_code), payload
    except FutureTimeout:
        elapsed = round(time.perf_counter() - start, 3)
        fut.cancel()
        if own_executor:
            exec_ref.shutdown(wait=False, cancel_futures=True)
        return True, elapsed, 0, {}
    finally:
        if own_executor:
            try:
                exec_ref.shutdown(wait=False, cancel_futures=False)
            except Exception:
                pass


def _build_row_from_response(
    *,
    case: PromptCase,
    timed_out: bool,
    elapsed_s: float,
    status_code: int,
    response_json: dict[str, Any],
    last_log_id: int,
) -> dict[str, Any]:
    new_log = ChatbotLog.objects.filter(log_id__gt=last_log_id).order_by("-log_id").first()
    resolved_intent = str(getattr(new_log, "resolved_intent", "") or "").strip().lower() if new_log else ""
    fallback_used = bool(getattr(new_log, "fallback_used", False)) if new_log else False
    intent_source = str(getattr(new_log, "intent_classifier_source", "") or "").strip().lower() if new_log else ""
    provenance = new_log.provenance_json if (new_log and isinstance(new_log.provenance_json, dict)) else {}
    extra = provenance.get("extra") if isinstance(provenance.get("extra"), dict) else {}
    deterministic_used = bool(
        intent_source == "deterministic_pre_route"
        or bool(extra.get("deterministic_routing_applied"))
        or str(provenance.get("intent_source") or "").strip().lower() == "deterministic_pre_route"
    )

    row = {
        "case_key": case.key,
        "group": case.group,
        "prompt": case.prompt,
        "mode": case.mode,
        "turn_index": case.turn_index,
        "timed_out": bool(timed_out),
        "elapsed_s": float(elapsed_s),
        "status_code": int(status_code),
        "response_summary": str(response_json.get("fulfillmentText") or "").strip()[:260],
        "detected_intent": _extract_detected_intent(response_json),
        "resolved_intent": resolved_intent,
        "deterministic_routing_used": deterministic_used,
        "fallback_used": fallback_used,
        "active_flow": str(extra.get("active_flow") or ""),
        "slots_filled": extra.get("slots_filled") if isinstance(extra.get("slots_filled"), list) else [],
        "slots_missing": str(extra.get("slots_missing") or ""),
        "needs_clarification": bool(response_json.get("needs_clarification")),
    }
    passed, reason = _evaluate_row(row, flow5_index=case.turn_index)
    row["pass"] = bool(passed)
    row["failure_reason"] = "" if passed else reason
    return row


def _summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(rows)
    passed_count = len([r for r in rows if r.get("pass")])
    failed_rows = [r for r in rows if not r.get("pass")]
    by_group: dict[str, dict[str, int]] = {}
    for row in rows:
        grp = str(row.get("group") or "")
        if grp not in by_group:
            by_group[grp] = {"total": 0, "passed": 0, "failed": 0}
        by_group[grp]["total"] += 1
        if row.get("pass"):
            by_group[grp]["passed"] += 1
        else:
            by_group[grp]["failed"] += 1
    root_causes: dict[str, int] = {}
    for row in failed_rows:
        key = str(row.get("failure_reason") or "unknown_failure")
        root_causes[key] = root_causes.get(key, 0) + 1
    return {
        "total": total,
        "passed": passed_count,
        "failed": total - passed_count,
        "pass_rate": round((passed_count / total) if total else 0.0, 4),
        "by_group": by_group,
        "root_causes": root_causes,
        "rows": rows,
    }


def run_batch(
    *,
    include_groups: list[str] | None = None,
    batch_size: int = 8,
    timeout_seconds: float = 8.0,
    slow_prompt_seconds: float = 5.0,
    max_turns_per_conversation: int = 12,
    progress_path: str = str(DEFAULT_PROGRESS_PATH),
    resume: bool = True,
) -> dict[str, Any]:
    progress_file = Path(progress_path)
    progress_rows = _load_progress(progress_file) if resume else {}

    cases = _build_cases()
    if isinstance(include_groups, list) and include_groups:
        allowed = {str(v).strip().lower() for v in include_groups if str(v).strip()}
        cases = [c for c in cases if str(c.group).strip().lower() in allowed]

    remaining = [c for c in cases if c.key not in progress_rows]
    to_run = remaining[: max(1, int(batch_size or 1))]

    if not to_run:
        completed_rows = [progress_rows[c.key] for c in cases if c.key in progress_rows]
        return {
            "message": "No remaining prompts for the selected scope.",
            "completed_summary": _summarize(completed_rows),
            "processed_in_batch": [],
            "remaining_count": 0,
        }

    guest_user, owner_user, admin_user, employee = _ensure_test_users()
    flow_client = _new_client_for_role("guest", guest_user, owner_user, admin_user, employee)
    flow_replayed_until = -1
    shared_executor = ThreadPoolExecutor(max_workers=1)

    processed_rows: list[dict[str, Any]] = []
    try:
        for case in to_run:
            if case.mode == "single":
                client = _new_client_for_role("guest", guest_user, owner_user, admin_user, employee)
            else:
                if case.turn_index >= max_turns_per_conversation:
                    row = {
                        "case_key": case.key,
                        "group": case.group,
                        "prompt": case.prompt,
                        "mode": case.mode,
                        "turn_index": case.turn_index,
                        "timed_out": False,
                        "elapsed_s": 0.0,
                        "status_code": 0,
                        "response_summary": "",
                        "detected_intent": "",
                        "resolved_intent": "",
                        "deterministic_routing_used": False,
                        "fallback_used": False,
                        "active_flow": "",
                        "slots_filled": [],
                        "slots_missing": "",
                        "needs_clarification": False,
                        "slow_or_stalled": False,
                        "slow_or_stalled_reason": "max_turn_limit_reached",
                        "pass": False,
                        "failure_reason": "max_turn_limit_reached",
                    }
                    _append_progress(progress_file, row)
                    processed_rows.append(row)
                    print(json.dumps({"stream": "row", **row}, ensure_ascii=False), flush=True)
                    continue
                # Replay prior flow prompts to rebuild session state when resuming.
                for replay_turn in range(flow_replayed_until + 1, case.turn_index):
                    replay_key = f"flow5_context:{replay_turn}"
                    replay_case = next((c for c in cases if c.key == replay_key), None)
                    if replay_case is None:
                        continue
                    _post_with_timeout(
                        flow_client,
                        replay_case.prompt,
                        timeout_seconds=max(2.0, timeout_seconds),
                        executor=shared_executor,
                    )
                    flow_replayed_until = replay_turn
                client = flow_client

            last_log_id = ChatbotLog.objects.order_by("-log_id").values_list("log_id", flat=True).first() or 0
            timed_out, elapsed_s, status_code, response_json = _post_with_timeout(
                client,
                case.prompt,
                timeout_seconds,
                executor=shared_executor,
            )
            row = _build_row_from_response(
                case=case,
                timed_out=timed_out,
                elapsed_s=elapsed_s,
                status_code=status_code,
                response_json=response_json,
                last_log_id=last_log_id,
            )
            if timed_out:
                row["failure_reason"] = "prompt_timeout"
                row["pass"] = False
            row["slow_or_stalled"] = bool(timed_out or float(elapsed_s) >= max(0.1, float(slow_prompt_seconds)))
            if timed_out:
                row["slow_or_stalled_reason"] = "timeout"
            elif float(elapsed_s) >= max(0.1, float(slow_prompt_seconds)):
                row["slow_or_stalled_reason"] = "slow_prompt"
            else:
                row["slow_or_stalled_reason"] = ""
            _append_progress(progress_file, row)
            processed_rows.append(row)
            if case.mode == "continuous" and not timed_out:
                flow_replayed_until = max(flow_replayed_until, case.turn_index)
            print(json.dumps({"stream": "row", **row}, ensure_ascii=False), flush=True)
    finally:
        shared_executor.shutdown(wait=False, cancel_futures=True)

    all_rows = _load_progress(progress_file)
    completed_rows = [all_rows[c.key] for c in cases if c.key in all_rows]
    remaining_count = len([c for c in cases if c.key not in all_rows])
    return {
        "processed_in_batch": processed_rows,
        "batch_summary": _summarize(processed_rows),
        "completed_summary": _summarize(completed_rows),
        "remaining_count": remaining_count,
        "progress_path": str(progress_file),
    }


def run(
    *,
    include_groups: list[str] | None = None,
    start_index: int = 0,
    max_cases: int | None = None,
) -> dict[str, Any]:
    # Backward-compatible full in-memory run without timeout/persistence.
    result = run_batch(
        include_groups=include_groups,
        batch_size=max_cases or 1000,
        timeout_seconds=30.0,
        max_turns_per_conversation=20,
        resume=False,
        progress_path="thesis_data_templates/realistic_regression_tmp.jsonl",
    )
    rows = result.get("processed_in_batch") if isinstance(result.get("processed_in_batch"), list) else []
    if start_index > 0:
        rows = rows[start_index:]
    return _summarize(rows)


def audit_timeout_cases(
    *,
    progress_path: str = str(DEFAULT_PROGRESS_PATH),
    out_path: str = str(DEFAULT_TIMEOUT_AUDIT_PATH),
    rerun_timeout_seconds: float = 12.0,
    slow_prompt_seconds: float = 5.0,
) -> dict[str, Any]:
    progress_file = Path(progress_path)
    out_file = Path(out_path)
    timeout_cases = _find_timeout_cases(progress_file)
    if not timeout_cases:
        return {
            "message": "No timeout cases found.",
            "timeout_prompts": [],
            "rows": [],
            "out_path": str(out_file),
        }

    progress_rows = _load_progress(progress_file)
    guest_user, owner_user, admin_user, employee = _ensure_test_users()
    warmup_client = _new_client_for_role("guest", guest_user, owner_user, admin_user, employee)
    shared_executor = ThreadPoolExecutor(max_workers=1)
    warmup = _warmup_runtime(
        warmup_client,
        timeout_seconds=max(8.0, rerun_timeout_seconds),
        executor=shared_executor,
    )

    rows: list[dict[str, Any]] = []
    case_map = {case.key: case for case in _build_cases()}
    flow_client = _new_client_for_role("guest", guest_user, owner_user, admin_user, employee)
    flow_replayed_until = -1
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text("", encoding="utf-8")

    try:
        for case in timeout_cases:
            if case.mode == "continuous":
                for replay_turn in range(flow_replayed_until + 1, case.turn_index):
                    replay_key = f"{case.group}:{replay_turn}"
                    replay_case = case_map.get(replay_key)
                    if replay_case is None:
                        continue
                    _post_with_timeout(
                        flow_client,
                        replay_case.prompt,
                        timeout_seconds=max(2.0, rerun_timeout_seconds),
                        executor=shared_executor,
                    )
                    flow_replayed_until = replay_turn
                client = flow_client
            else:
                client = _new_client_for_role("guest", guest_user, owner_user, admin_user, employee)
            profile = _profile_prompt_components(case.prompt)

            last_log_id = ChatbotLog.objects.order_by("-log_id").values_list("log_id", flat=True).first() or 0
            timed_out, elapsed_s, status_code, response_json = _post_with_timeout(
                client,
                case.prompt,
                rerun_timeout_seconds,
                executor=shared_executor,
            )
            row = _build_row_from_response(
                case=case,
                timed_out=timed_out,
                elapsed_s=elapsed_s,
                status_code=status_code,
                response_json=response_json,
                last_log_id=last_log_id,
            )
            if timed_out:
                row["pass"] = False
                row["failure_reason"] = "prompt_timeout"

            row["slow_or_stalled"] = bool(timed_out or float(elapsed_s) >= max(0.1, float(slow_prompt_seconds)))
            if timed_out:
                row["slow_or_stalled_reason"] = "timeout"
            elif float(elapsed_s) >= max(0.1, float(slow_prompt_seconds)):
                row["slow_or_stalled_reason"] = "slow_prompt"
            else:
                row["slow_or_stalled_reason"] = ""

            prev_row = progress_rows.get(case.key, {})
            row["before_elapsed_s"] = float(prev_row.get("elapsed_s", 0.0) or 0.0)
            row["before_timed_out"] = bool(prev_row.get("timed_out"))
            row["component_profile"] = profile
            row["api_request_response_s"] = float(elapsed_s)

            with out_file.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            rows.append(row)
            if case.mode == "continuous" and not timed_out:
                flow_replayed_until = max(flow_replayed_until, case.turn_index)
            print(json.dumps({"stream": "timeout_audit_row", **row}, ensure_ascii=False), flush=True)
    finally:
        shared_executor.shutdown(wait=False, cancel_futures=True)

    summary = _summarize(rows)
    root_causes: dict[str, int] = {}
    for row in rows:
        reason = str(row.get("failure_reason") or "")
        root_causes[reason] = root_causes.get(reason, 0) + 1
    return {
        "timeout_prompts": [{"case_key": c.key, "prompt": c.prompt, "group": c.group} for c in timeout_cases],
        "warmup": warmup,
        "rows": rows,
        "summary": summary,
        "root_causes": root_causes,
        "out_path": str(out_file),
    }
