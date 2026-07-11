from __future__ import annotations

import copy
import json
import os
import re
import time
import unicodedata
from datetime import datetime, timezone
from typing import Any

from .engine.reducer import (
    apply_definitive_filter,
    acquirer_type_for_heir,
    build_counterfactuals,
    normalize_heirs,
    reduce_case,
    select_home_acquirer_id_for_type,
)


TOOL_DECLARATIONS: list[dict[str, Any]] = [
    {
        "name": "select_taker_branch",
        "description": "相談文から取得者区分を選び、税務結論ではなく確認分岐を起動する。",
        "parameters": {
            "type": "object",
            "properties": {
                "acquirer_type": {
                    "type": "string",
                    "enum": ["spouse", "co_resident", "house_lost"],
                    "description": (
                        "spouse=配偶者が取得。co_resident=取得者が被相続人と同居・一緒に生活・"
                        "暮らしていた。house_lost=取得者本人が別居・非同居・賃貸・持ち家なし。"
                    ),
                },
                "reason": {"type": "string"},
            },
            "required": ["acquirer_type", "reason"],
        },
    },
    {
        "name": "request_clarification",
        "description": (
            "取得者分岐を安全に選ぶための事実が不足・矛盾しているとき、案件を変更せず追加確認で停止する。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "missing_fact": {
                    "type": "string",
                    "enum": ["home_acquirer", "residence_and_home_ownership"],
                    "description": (
                        "home_acquirer=自宅を取得する人が不明。"
                        "residence_and_home_ownership=取得予定者の同居状況または持ち家状況が不明・矛盾。"
                    ),
                },
                "reason": {
                    "type": "string",
                    "description": "なぜ分岐を確定せず追加確認が必要か。税務結論は書かない。",
                },
            },
            "required": ["missing_fact", "reason"],
        },
    },
    {
        "name": "check_requirements",
        "description": "選択済み取得者区分の要件確認をreducerで再導出する。",
        "parameters": {
            "type": "object",
            "properties": {"acquirer_type": {"type": "string"}},
            "required": ["acquirer_type"],
        },
    },
    {
        "name": "list_missing_documents",
        "description": "要件確認に必要な不足資料を列挙する。",
        "parameters": {
            "type": "object",
            "properties": {"document_ids": {"type": "array", "items": {"type": "string"}}},
            "required": ["document_ids"],
        },
    },
    {
        "name": "flag_title_anomaly",
        "description": "登記名義と被相続人のずれを発見イベントとして起票する。",
        "parameters": {
            "type": "object",
            "properties": {
                "document_id": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["document_id", "reason"],
        },
    },
]

GEMINI_ROUTER_MODEL = "gemini-3.5-flash"
GEMINI_ROUTER_TOOL_NAME = "select_taker_branch"
GEMINI_CLARIFICATION_TOOL_NAME = "request_clarification"
GEMINI_ROUTER_TOOL_NAMES = {GEMINI_ROUTER_TOOL_NAME, GEMINI_CLARIFICATION_TOOL_NAME}
CLARIFICATION_QUESTIONS = {
    "home_acquirer": "自宅を取得する予定の方は誰ですか？",
    "residence_and_home_ownership": (
        "取得予定者は被相続人と同居していましたか。また、本人または配偶者名義の持ち家がありますか？"
    ),
}
# 実APIがハングしても審査デモを固めない。超過したら例外→決定的リプレイへfallbackする。
GEMINI_ROUTER_TIMEOUT_SECONDS = 10


STATUS_RANK = {
    "not_requested": 0,
    "requested": 1,
    "received": 2,
    "verified": 3,
}

NEUTRAL_CASE_TITLE = "小規模宅地 要件確認案件"


def build_agent_run(
    *,
    consultation_text: str,
    state: dict[str, Any],
    rules: dict[str, Any],
    gemini_configured: bool,
    source: str = "consultation",
    decision_history: list[dict[str, Any]] | None = None,
    continuation_of: str = "",
) -> tuple[dict[str, Any], dict[str, Any]]:
    """相談文をACTIONタイムラインへ変換し、reducer実行で案件状態を更新する。"""
    text = _normalize_text(consultation_text)
    next_state = copy.deepcopy(state)
    route = _route_consultation(text, rules)
    route = (
        _route_from_card_state(next_state, route)
        if source == "heir_cards"
        else _resolve_ambiguous_route_from_state(text, next_state, route)
    )
    gemini_trace = _gemini_trace(configured=gemini_configured)
    if gemini_configured:
        gemini_route, gemini_trace = _route_consultation_with_gemini(
            text,
            rules,
            route,
            structured_context=_structured_heir_context(next_state),
            authoritative_context=(
                source == "heir_cards" or bool(route.get("authoritative_structured_facts"))
            ),
        )
        route = gemini_route
        if route.get("decision") == "clarify":
            return next_state, _build_clarification_run(
                text=text,
                route=route,
                gemini_trace=gemini_trace,
                source=source,
                decision_history=decision_history,
                continuation_of=continuation_of,
            )
    if route.get("hard_clarification"):
        gemini_trace["guardrail_applied"] = True
        gemini_trace["effective_route"] = "safe_stop"
        gemini_trace["guardrail_reason"] = "取得者未定または居住事実の矛盾を検出したため分岐を確定しない"
    if route.get("requires_clarification") or route.get("hard_clarification"):
        safe_route = _deterministic_clarification_route(route)
        return next_state, _build_clarification_run(
            text=text,
            route=safe_route,
            gemini_trace=gemini_trace,
            source=source,
            decision_history=decision_history,
            continuation_of=continuation_of,
        )
    heir_events = _heir_events_for_text(text, next_state, route)
    document_events = _document_events(text)

    next_state["case_title"] = _case_title(text)
    next_state["acquirer_type"] = route["acquirer_type"]
    selected_id = heir_events.get("home_acquirer_id") or select_home_acquirer_id_for_type(
        next_state, route["acquirer_type"]
    )
    if selected_id:
        next_state["home_acquirer_id"] = selected_id
        if (
            heir_events.get("generated")
            and gemini_trace.get("used")
            and gemini_trace.get("tool_name") == GEMINI_ROUTER_TOOL_NAME
        ):
            _apply_gemini_route_to_generated_heir(
                next_state,
                selected_id,
                route["acquirer_type"],
                text,
            )
        selected_heir = _heir_by_id(next_state, selected_id)
        if selected_heir and heir_events.get("home_acquirer_id"):
            next_state["acquirer_type"] = acquirer_type_for_heir(selected_heir)
            route = {
                **route,
                "acquirer_type": next_state["acquirer_type"],
                "reason": heir_events.get(
                    "selection_reason",
                    "相談文で指名された自宅取得者カードから分岐を選択",
                ),
            }
    _finalize_gemini_route_trace(gemini_trace, route, next_state)
    next_state["partition_status"] = route["partition_status"]
    documents = dict(next_state.get("documents", {}))
    for doc_id, status in document_events["status_updates"].items():
        documents[doc_id] = _raise_status(documents.get(doc_id, "not_requested"), status)
    next_state["documents"] = documents

    reduced = reduce_case(next_state, rules)
    branches = build_counterfactuals(next_state, rules)
    steps = _build_steps(
        text=text,
        route=route,
        heir_events=heir_events,
        document_events=document_events,
        reduced=reduced,
        branches=branches,
        rules=rules,
        source=source,
    )
    history = list(decision_history or [])
    history.append(_route_decision_event(gemini_trace, route, next_state, len(history) + 1))
    run = {
        "id": _run_id(),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "continuation_of": continuation_of,
        "status": "REVIEW_PENDING",
        "mode": "gemini_function_calling" if gemini_trace.get("used") else "deterministic_replay",
        "gemini_configured": gemini_configured,
        "gemini": gemini_trace,
        "responds_with": "actions_only",
        "assistant_reply": "",
        "approval_status": "PENDING_APPROVAL",
        "state_mutated": True,
        "input_text": text,
        "tool_declarations": TOOL_DECLARATIONS,
        "decision_history": history,
        "steps": steps,
    }
    return next_state, run


def build_card_review_run(
    *,
    state: dict[str, Any],
    rules: dict[str, Any],
    gemini_configured: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """相続人カードだけでReview到達用のACTIONタイムラインを作る。"""
    return build_agent_run(
        consultation_text=_card_review_text(state),
        state=state,
        rules=rules,
        gemini_configured=gemini_configured,
        source="heir_cards",
    )


def _route_consultation_with_gemini(
    text: str,
    rules: dict[str, Any],
    fallback_route: dict[str, Any],
    *,
    structured_context: str = "",
    authoritative_context: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Geminiに「分岐を進める／追加確認で止まる」を選ばせ、失敗時は決定的分岐へ戻す。"""
    started = time.perf_counter()
    trace = _gemini_trace(configured=True)
    api_key = os.getenv("GEMINI_API_KEY", "")
    try:
        client = _create_gemini_client(api_key)
        interaction = client.interactions.create(
            model=GEMINI_ROUTER_MODEL,
            input=_gemini_router_prompt(text, rules, structured_context=structured_context),
            tools=_gemini_router_tools(),
            timeout=GEMINI_ROUTER_TIMEOUT_SECONDS,
        )
        function_call = _first_function_call(interaction)
        if not function_call:
            trace["fallback_reason"] = "gemini_no_function_call"
            trace["latency_ms"] = _elapsed_ms(started)
            return fallback_route, trace

        tool_name = str(function_call.get("name", ""))
        arguments = _normalize_function_arguments(function_call.get("arguments", {}))
        if tool_name == GEMINI_CLARIFICATION_TOOL_NAME:
            missing_fact = str(arguments.get("missing_fact", ""))
            if missing_fact not in CLARIFICATION_QUESTIONS:
                trace.update(
                    {
                        "tool_name": tool_name,
                        "arguments": arguments,
                        "fallback_reason": "gemini_invalid_tool_call",
                        "latency_ms": _elapsed_ms(started),
                    }
                )
                return {**fallback_route, "decision": "route"}, trace

            reason = _safe_prompt_text(
                str(arguments.get("reason") or "取得者分岐に必要な事実が不足"),
                rules,
            )[:240]
            guardrail_applied = False
            if fallback_route.get("hard_clarification"):
                required_fact = str(fallback_route.get("missing_fact") or "home_acquirer")
                if required_fact in CLARIFICATION_QUESTIONS and required_fact != missing_fact:
                    missing_fact = required_fact
                    guardrail_applied = True
            clarification = {
                "missing_fact": missing_fact,
                "question": CLARIFICATION_QUESTIONS[missing_fact],
                "reason": reason,
            }
            if authoritative_context:
                trace.update(
                    {
                        "tool_name": tool_name,
                        "arguments": clarification,
                        "fallback_reason": "clarification_not_needed_structured_facts",
                        "latency_ms": _elapsed_ms(started),
                        "effective_route": str(fallback_route.get("acquirer_type") or ""),
                        "guardrail_applied": True,
                        "guardrail_reason": "登録済みカードまたは明示された事実で分岐を一意に確定できるため",
                    }
                )
                return {**fallback_route, "decision": "route"}, trace
            trace.update(
                {
                    "used": True,
                    "tool_name": tool_name,
                    "arguments": clarification,
                    "latency_ms": _elapsed_ms(started),
                    "guardrail_applied": guardrail_applied,
                    "guardrail_reason": (
                        "決定的ガードが検出した不足事実を質問へ優先"
                        if guardrail_applied
                        else ""
                    ),
                }
            )
            return {
                **fallback_route,
                "decision": "clarify",
                "clarification": clarification,
            }, trace

        acquirer_type = str(arguments.get("acquirer_type", ""))
        if tool_name != GEMINI_ROUTER_TOOL_NAME or acquirer_type not in rules["expert"]["acquirer_types"]:
            trace.update(
                {
                    "tool_name": tool_name,
                    "arguments": arguments,
                    "fallback_reason": "gemini_invalid_tool_call",
                    "latency_ms": _elapsed_ms(started),
                }
            )
            return {**fallback_route, "decision": "route"}, trace

        reason = _safe_prompt_text(
            str(arguments.get("reason") or "Gemini 3.5 Flash function calling が取得者分岐を選択"),
            rules,
        )
        trace.update(
            {
                "used": True,
                "tool_name": tool_name,
                "arguments": {
                    "acquirer_type": acquirer_type,
                    "reason": reason,
                },
                "latency_ms": _elapsed_ms(started),
            }
        )
        return {
            **fallback_route,
            "decision": "route",
            "requires_clarification": False,
            "acquirer_type": acquirer_type,
            "reason": f"Gemini 3.5 Flash function calling: {reason}",
        }, trace
    except Exception as exc:  # pragma: no cover - 実API/SDK障害時もデモを止めないための保険
        trace.update(
            {
                "fallback_reason": exc.__class__.__name__,
                "latency_ms": _elapsed_ms(started),
            }
        )
        return fallback_route, trace


def _gemini_trace(*, configured: bool) -> dict[str, Any]:
    return {
        "configured": configured,
        "used": False,
        "model": GEMINI_ROUTER_MODEL,
        "tool_name": "",
        "arguments": {},
        "latency_ms": 0,
        "fallback_reason": "" if configured else "gemini_api_key_not_set",
        "proposed_route": "",
        "effective_route": "",
        "guardrail_applied": False,
        "guardrail_reason": "",
    }


def _create_gemini_client(api_key: str) -> Any:
    from google import genai

    return genai.Client(api_key=api_key)


def _gemini_router_tools() -> list[dict[str, Any]]:
    return [
        {"type": "function", **declaration}
        for declaration in TOOL_DECLARATIONS
        if declaration["name"] in GEMINI_ROUTER_TOOL_NAMES
    ]


def _gemini_router_prompt(
    text: str,
    rules: dict[str, Any],
    *,
    structured_context: str = "",
) -> str:
    labels = {
        acquirer_type: config["label"]
        for acquirer_type, config in rules["expert"]["acquirer_types"].items()
    }
    return (
        "あなたは相続税申告支援デモのRouterです。"
        "税務結論、書面添付本文、総合所見は書かず、必ず function calling で"
        f"{GEMINI_ROUTER_TOOL_NAME} または {GEMINI_CLARIFICATION_TOOL_NAME} のどちらか一つだけを呼び出してください。"
        "目的は、相談文から自宅取得者の確認分岐を安全に起動することです。"
        f"選択肢: {json.dumps(labels, ensure_ascii=False)}。"
        "判定対象は自宅を相続・取得する本人です。"
        "配偶者が取得するなら spouse。取得者が被相続人と同居、一緒に住んでいた、"
        "被相続人と暮らしていた、故人と生活していた文脈なら co_resident。"
        "取得者本人について別居、非同居、賃貸、借家、持ち家なしが明示された場合だけ"
        " house_lost を選んでください。子・次男という続柄だけで house_lost と推測しないでください。"
        "自宅を取得する人が特定できない場合は request_clarification の home_acquirer。"
        "取得者は特定できても、配偶者取得でも同居取得でもなく、取得者本人の同居状況または持ち家状況が"
        "不足・矛盾して分岐を一意に選べない場合は request_clarification の residence_and_home_ownership。"
        "登録済みカードがある場合は、その続柄と同居・非同居を相談文より信頼できる構造化事実として使ってください。"
        "不足事実を推測して select_taker_branch を呼んではいけません。"
        f"\n登録済みカード: {structured_context or 'なし'}"
        f"\n相談文: {text}"
    )


def _build_clarification_run(
    *,
    text: str,
    route: dict[str, Any],
    gemini_trace: dict[str, Any],
    source: str,
    decision_history: list[dict[str, Any]] | None,
    continuation_of: str,
) -> dict[str, Any]:
    clarification = dict(route["clarification"])
    history = list(decision_history or [])
    decision_tool = (
        GEMINI_CLARIFICATION_TOOL_NAME
        if gemini_trace.get("used")
        and gemini_trace.get("tool_name") == GEMINI_CLARIFICATION_TOOL_NAME
        else "deterministic_safe_stop"
    )
    history.append(
        {
            "sequence": len(history) + 1,
            "tool": decision_tool,
            "decision": "additional_information_required",
            "result": clarification["missing_fact"],
            "state_mutated": False,
            "reason": clarification["reason"],
        }
    )
    return {
        "id": _run_id(),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "continuation_of": continuation_of,
        "status": "AWAITING_CLARIFICATION",
        "mode": (
            "gemini_function_calling"
            if gemini_trace.get("used")
            else "deterministic_safe_stop"
        ),
        "gemini_configured": bool(gemini_trace.get("configured")),
        "gemini": gemini_trace,
        "responds_with": "actions_only",
        "assistant_reply": "",
        "approval_status": "NOT_READY",
        "state_mutated": False,
        "input_text": text,
        "tool_declarations": TOOL_DECLARATIONS,
        "clarification": clarification,
        "decision_history": history,
        "steps": [
            {
                "id": "intake",
                "label": "Intake",
                "status": "DONE",
                "summary": "相談文を受け付け、取得者分岐に必要な事実がそろっているか確認しました。",
                "actions": [
                    {
                        "type": "capture_consultation",
                        "target": "pending_consultation",
                        "value": NEUTRAL_CASE_TITLE,
                        "reason": "追加回答と結合して再開するため、相談文をセッション内に一時保持",
                    }
                ],
            },
            {
                "id": "clarification",
                "label": "Clarify",
                "status": "AWAITING_INPUT",
                "summary": "必要な事実が不足しているため、案件状態を変更せず追加情報待ちで停止しました。",
                "actions": [
                    {
                        "type": GEMINI_CLARIFICATION_TOOL_NAME,
                        "target": clarification["missing_fact"],
                        "value": clarification["question"],
                        "reason": clarification["reason"],
                        "state_mutated": False,
                    }
                ],
            },
        ],
    }


def _run_id() -> str:
    return f"run_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}"


def _finalize_gemini_route_trace(
    trace: dict[str, Any],
    route: dict[str, Any],
    state: dict[str, Any],
) -> None:
    if not trace.get("used") or trace.get("tool_name") != GEMINI_ROUTER_TOOL_NAME:
        return
    proposed = str(trace.get("arguments", {}).get("acquirer_type", ""))
    effective = str(state.get("acquirer_type") or route.get("acquirer_type") or "")
    trace["proposed_route"] = proposed
    trace["effective_route"] = effective
    trace["guardrail_applied"] = bool(proposed and effective and proposed != effective)
    if trace["guardrail_applied"]:
        trace["guardrail_reason"] = (
            "登録済みまたは相談文から起票した自宅取得者カードの構造化事実を優先"
        )


def _route_decision_event(
    trace: dict[str, Any],
    route: dict[str, Any],
    state: dict[str, Any],
    sequence: int,
) -> dict[str, Any]:
    tool = trace.get("tool_name") if trace.get("used") else "deterministic_replay"
    return {
        "sequence": sequence,
        "tool": tool or "deterministic_replay",
        "decision": "route_selected",
        "result": str(state.get("acquirer_type") or route.get("acquirer_type") or ""),
        "state_mutated": True,
        "reason": str(route.get("reason") or ""),
    }


def _first_function_call(interaction: Any) -> dict[str, Any] | None:
    for step in _interaction_steps(interaction):
        step_type = _field(step, "type", "")
        if step_type != "function_call":
            continue
        return {
            "id": _field(step, "id", ""),
            "name": _field(step, "name", ""),
            "arguments": _field(step, "arguments", {}),
        }
    return None


def _interaction_steps(interaction: Any) -> list[Any]:
    steps = _field(interaction, "steps", None)
    if steps is None:
        steps = _field(interaction, "outputs", [])
    return list(steps or [])


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _normalize_function_arguments(arguments: Any) -> dict[str, Any]:
    if isinstance(arguments, dict):
        return arguments
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _elapsed_ms(started: float) -> int:
    return max(0, round((time.perf_counter() - started) * 1000))


def _build_steps(
    *,
    text: str,
    route: dict[str, Any],
    heir_events: dict[str, Any],
    document_events: dict[str, Any],
    reduced: dict[str, Any],
    branches: list[dict[str, Any]],
    rules: dict[str, Any],
    source: str,
) -> list[dict[str, Any]]:
    missing_ids = [item["id"] for item in reduced["missing_documents"]]
    missing_labels = [item["label"] for item in reduced["missing_documents"]]
    card_review = source == "heir_cards"
    steps = [
        {
            "id": "intake",
            "label": "Intake",
            "status": "DONE",
            "summary": (
                "相続人カードを案件条件として受け付け、相談文なしでReview作成を開始しました。"
                if card_review
                else "相談文を案件化し、回答文ではなく実行指示として受け付けました。"
            ),
            "actions": [_intake_capture_action(text=text, reduced=reduced, card_review=card_review)]
            + _heir_card_actions(heir_events),
        },
        {
            "id": "router",
            "label": "Router",
            "status": "DONE",
            "summary": f"取得者区分を「{reduced['acquirer']['label']}」として要件分岐を起動しました。",
            "actions": [
                {
                    "type": "select_taker_branch",
                    "target": "acquirer_type",
                    "value": route["acquirer_type"],
                    "reason": route["reason"],
                },
                {
                    "type": "set_partition_status",
                    "target": "partition_status",
                    "value": route["partition_status"],
                    "reason": route["partition_reason"],
                },
            ],
        },
        {
            "id": "evidence",
            "label": "Evidence",
            "status": "DONE",
            "summary": f"不足資料 {len(missing_ids)} 件を起票し、提示済み資料と照合しました。",
            "actions": [
                {
                    "type": "list_missing_documents",
                    "target": "documents",
                    "value": missing_ids,
                    "labels": missing_labels,
                    "reason": "取得者分岐に必要な資料だけをreducerから列挙",
                }
            ]
            + _title_anomaly_actions(document_events),
        },
        {
            "id": "draft",
            "label": "Draft",
            "status": "DONE",
            "summary": "書面添付ドラフトを「確認中」として更新しました。",
            "actions": [
                {
                    "type": "check_requirements",
                    "target": "draft.section_3_land_review",
                    "value": reduced["draft"]["section_3_land_review"][:4],
                    "reason": "適用可否ではなく税理士確認前の論点として生成",
                },
                {
                    "type": "render_counterfactuals",
                    "target": "counterfactuals",
                    "value": [branch["to"] for branch in branches],
                    "reason": "取得者を切り替えた場合の再導出候補を準備",
                },
            ],
        },
        {
            "id": "review",
            "label": "Review",
            "status": "PENDING_APPROVAL",
            "summary": "税理士レビューで停止中です。アラート・不足資料・総合所見を確認し、レビュー完了（承認）後にWord出力へ進めます。",
            "actions": _review_actions(reduced, rules),
        },
    ]
    return steps


def _intake_capture_action(*, text: str, reduced: dict[str, Any], card_review: bool) -> dict[str, Any]:
    if card_review:
        home = reduced.get("home_acquirer") or {}
        return {
            "type": "capture_heir_cards",
            "target": "heirs",
            "value": f"自宅取得者: {home.get('name', '未選択')} / {reduced['acquirer']['label']}",
            "reason": "登録済みの相続人カードと自宅取得者選択からReview作成を開始",
        }
    return {
        "type": "capture_consultation",
        "target": "case_title",
        "value": _case_title(text),
        "reason": "相続実務の入口である散文相談を案件名へ変換",
    }


def _review_actions(reduced: dict[str, Any], rules: dict[str, Any]) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    denial_impact = rules["expert"]["small_residence_denial_impact"]
    for alert in reduced.get("eligibility_alerts", []):
        actions.append(
            {
                "type": "alert_small_residence_ineligible",
                "target": "review.small_residence",
                "value": alert["title"],
                "message": alert["message"],
                "impact_yen": alert["impact_yen"],
                "impact_label": "課税価格",
                "impact_summary": denial_impact["summary"],
                "reason": "相続人の同居有無と自宅取得者の組み合わせから適用不可リスクを検出",
            }
        )
    prompt = rules["expert"].get("secondary_inheritance_prompt", {})
    if reduced["acquirer"]["id"] == prompt.get("trigger_acquirer_type"):
        actions.append(
            {
                "type": "ask_secondary_inheritance_review",
                "target": "review.secondary_inheritance",
                "value": _safe_prompt_text(str(prompt.get("question", "")), rules),
                "why": _safe_prompt_text(str(prompt.get("why", "")), rules),
                "guardrail": _safe_prompt_text(str(prompt.get("guardrail", "")), rules),
                "provenance": dict(prompt.get("provenance", {})),
                "reason": "配偶者取得時だけ、二次相続の専門家確認論点を承認前に提示",
            }
        )
    actions.append(
        {
            "type": "request_human_approval",
            "target": "word_export",
            "value": "PENDING_APPROVAL",
            "reason": "アラート、不足資料、総合所見を確認した税理士がレビュー完了（承認）するため",
        }
    )
    return actions


def _card_review_text(state: dict[str, Any]) -> str:
    heirs = normalize_heirs(state.get("heirs", []))
    home_acquirer = _heir_by_id({"heirs": heirs}, str(state.get("home_acquirer_id") or "")) if heirs else None
    if home_acquirer is None and heirs:
        home_acquirer = heirs[0]
    heir_summary = "、".join(_heir_card_text(heir) for heir in heirs) or "未登録"
    home_name = str(home_acquirer.get("name", "未選択")) if home_acquirer else "未選択"
    home_status = _heir_card_status_text(home_acquirer) if home_acquirer else "未選択"
    return (
        "相談文なし。相続人カード登録済み。"
        f"自宅取得者は{home_name}（{home_status}）が相続します。"
        f"相続人カード: {heir_summary}。"
        "登録カードの同居・非同居情報を優先してReviewを作成します。"
    )


def _heir_card_text(heir: dict[str, Any]) -> str:
    return f"{heir['name']}（{_heir_card_status_text(heir)}）"


def _heir_card_status_text(heir: dict[str, Any]) -> str:
    if heir.get("relation") == "spouse":
        return "配偶者"
    return "同居" if heir.get("co_resident") else "別居"


def _safe_prompt_text(text: str, rules: dict[str, Any]) -> str:
    safety = rules["expert"]["safety_rules"]
    compact = re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))
    has_forbidden = any(
        re.sub(r"\s+", "", unicodedata.normalize("NFKC", phrase)) in compact
        for phrase in safety["forbidden_definitive_phrases"]
    )
    if has_forbidden:
        return apply_definitive_filter(text, rules)
    return text


def _heir_events_for_text(
    text: str,
    next_state: dict[str, Any],
    route: dict[str, str],
) -> dict[str, Any]:
    existing_heirs = normalize_heirs(next_state.get("heirs", []))
    if existing_heirs:
        home_acquirer_id = _infer_home_acquirer_id(text, existing_heirs)
        if home_acquirer_id:
            next_state["home_acquirer_id"] = home_acquirer_id
            return {
                "generated": False,
                "heirs": [],
                "home_acquirer_id": home_acquirer_id,
                "selection_reason": "相談文で既存の相続人カードが自宅取得者として指名されたため、カードの同居・非同居情報を優先",
            }
        return {"generated": False, "heirs": [], "home_acquirer_id": ""}

    inferred_heirs = _infer_heirs_from_text(text)
    if not inferred_heirs:
        return {"generated": False, "heirs": [], "home_acquirer_id": ""}

    next_state["heirs"] = inferred_heirs
    home_acquirer_id = _infer_home_acquirer_id(text, inferred_heirs)
    if not home_acquirer_id:
        home_acquirer_id = select_home_acquirer_id_for_type(next_state, route["acquirer_type"])
    if home_acquirer_id:
        next_state["home_acquirer_id"] = home_acquirer_id

    return {
        "generated": True,
        "heirs": copy.deepcopy(inferred_heirs),
        "home_acquirer_id": home_acquirer_id,
        "selection_reason": "未登録の相続人カードを相談文から起票し、自宅取得者カードから分岐を選択",
    }


def _infer_heirs_from_text(text: str) -> list[dict[str, Any]]:
    heirs: list[dict[str, Any]] = []
    spouse_name = _spouse_name_from_text(text)
    if spouse_name:
        heirs.append(
            {
                "id": "mother" if spouse_name == "母" else "spouse",
                "name": spouse_name,
                "relation": "spouse",
                "co_resident": True,
            }
        )

    seen_ids = {heir["id"] for heir in heirs}
    for heir_id, label in _child_candidates():
        if heir_id in seen_ids or label not in text:
            continue
        heirs.append(
            {
                "id": heir_id,
                "name": label,
                "relation": "child",
                "co_resident": _co_resident_for_label(text, label),
            }
        )
        seen_ids.add(heir_id)
    return heirs


def _spouse_name_from_text(text: str) -> str:
    if re.search(r"(?<![祖義養継])母", text):
        return "母"
    if re.search(r"(?<![義養継])妻", text):
        return "妻"
    if _has_any(text, ["配偶者", "奥様", "ご主人"]) or re.search(r"(?<!丈)夫", text):
        return "配偶者"
    return ""


def _child_candidates() -> list[tuple[str, str]]:
    return [
        ("eldest_son", "長男"),
        ("second_son", "次男"),
        ("second_son", "二男"),
        ("eldest_daughter", "長女"),
        ("second_daughter", "次女"),
        ("second_daughter", "二女"),
        ("son", "息子"),
        ("daughter", "娘"),
    ]


def _co_resident_for_label(text: str, label: str) -> bool:
    signal = _residence_signal_for_label(text, label)
    return bool(signal) if signal is not None else False


def _residence_signal_for_label(text: str, label: str) -> bool | None:
    positive, negative = _residence_evidence_for_label(text, label)
    if positive and negative:
        return None
    if negative:
        return False
    if positive:
        return True
    return None


def _residence_evidence_for_label(text: str, label: str) -> tuple[bool, bool]:
    negative_terms = [
        "別居",
        "非同居",
        "賃貸",
        "借家",
        "持ち家なし",
        "持家なし",
        "持ち家のない",
        "持家のない",
        "自宅なし",
        "家なき子",
    ]
    positive = False
    negative = False
    for match in re.finditer(re.escape(label), text):
        sentence = _sentence_around(text, match.start(), match.end())
        if _has_any(sentence, negative_terms):
            negative = True
        if _has_co_resident_context(sentence):
            positive = True
    return positive, negative


def _infer_home_acquirer_id(text: str, heirs: list[dict[str, Any]]) -> str:
    candidates: list[tuple[int, int, int, str]] = []
    for heir in heirs:
        for alias in _aliases_for_heir(heir):
            for match in re.finditer(re.escape(alias), text):
                sentence = _sentence_around(text, match.start(), match.end())
                tail = text[match.end() : match.end() + 64]
                if _has_any(sentence, ["相続", "取得", "引き継", "受け継"]) and (
                    _has_any(sentence, ["自宅", "実家", "家", "宅地", "居宅", "土地"])
                    or _has_any(tail, ["相続", "取得", "引き継", "受け継"])
                ):
                    subject = re.match(r"\s*(?:さん|様)?(?:が|は)", tail)
                    possessive = re.match(r"\s*の", tail)
                    transfer = re.search(r"(?:相続|取得|引き継|受け継)", tail)
                    grammar_rank = 0 if subject else 1
                    if possessive:
                        grammar_rank = 2
                    transfer_distance = transfer.start() if transfer else len(sentence)
                    candidates.append(
                        (grammar_rank, transfer_distance, match.start(), str(heir["id"]))
                    )
    if not candidates:
        return ""
    candidates.sort(key=lambda item: item[:3])
    return candidates[0][3]


def _aliases_for_heir(heir: dict[str, Any]) -> list[str]:
    aliases = [str(heir.get("name") or "")]
    if heir.get("relation") == "spouse":
        aliases.extend(["配偶者", "妻", "夫", "奥様", "ご主人"])
    aliases = [alias for alias in aliases if alias]
    return list(dict.fromkeys(aliases))


def _sentence_around(text: str, start: int, end: int) -> str:
    left = max(text.rfind("。", 0, start), text.rfind("！", 0, start), text.rfind("？", 0, start))
    right_candidates = [text.find(mark, end) for mark in ["。", "！", "？"]]
    right_candidates = [index for index in right_candidates if index != -1]
    sentence_start = 0 if left == -1 else left + 1
    sentence_end = len(text) if not right_candidates else min(right_candidates) + 1
    return text[sentence_start:sentence_end]


def _heir_by_id(state: dict[str, Any], heir_id: str) -> dict[str, Any] | None:
    for heir in normalize_heirs(state.get("heirs", [])):
        if heir["id"] == heir_id:
            return heir
    return None


def _apply_gemini_route_to_generated_heir(
    state: dict[str, Any],
    heir_id: str,
    acquirer_type: str,
    text: str,
) -> None:
    """自然文から新規起票したカードだけ、Geminiの構造化結果で居住属性を補う。"""
    for heir in state.get("heirs", []):
        if not isinstance(heir, dict) or str(heir.get("id")) != heir_id:
            continue
        # 続柄と明示的な同居・別居語は決定的な構造化事実としてGeminiより優先する。
        if heir.get("relation") == "spouse" or acquirer_type == "spouse":
            return
        residence_signal = _residence_signal_for_label(text, str(heir.get("name") or ""))
        if residence_signal is not None:
            return
        if acquirer_type == "co_resident":
            heir["co_resident"] = True
        elif acquirer_type == "house_lost":
            heir["co_resident"] = False
        return


def _heir_card_actions(heir_events: dict[str, Any]) -> list[dict[str, Any]]:
    if not heir_events.get("generated"):
        return []
    names = [heir["name"] for heir in heir_events.get("heirs", [])]
    selected = ""
    for heir in heir_events.get("heirs", []):
        if heir["id"] == heir_events.get("home_acquirer_id"):
            selected = heir["name"]
            break
    return [
        {
            "type": "populate_heir_cards",
            "target": "heirs",
            "value": names,
            "selected_home_acquirer": selected,
            "reason": "相続人カード未登録のため、相談文の続柄語からカード候補を起票",
        }
    ]


def _route_consultation(text: str, rules: dict[str, Any]) -> dict[str, Any]:
    branch_matches: list[tuple[str, str]] = []
    acquirer_undecided = _home_acquirer_is_undecided(text)
    residence_conflict = _selected_residence_conflict(text)
    hard_clarification = acquirer_undecided or residence_conflict
    if not hard_clarification and _matches_house_lost(text):
        branch_matches.append(
            (
                "house_lost",
                "家なき子・持ち家なし・取得者本人の別居賃貸を示す語から家なき子分岐を選択",
            )
        )
    if not hard_clarification and _matches_spouse(text):
        branch_matches.append(("spouse", "配偶者取得を示す語から配偶者分岐を選択"))
    if not hard_clarification and _matches_co_resident(text):
        branch_matches.append(("co_resident", "同居・居住継続を示す語から同居親族分岐を選択"))

    if len(branch_matches) == 1:
        acquirer_type, reason = branch_matches[0]
        requires_clarification = False
        missing_fact = ""
    elif len(branch_matches) > 1:
        acquirer_type = rules["expert"]["demo_case"]["default_acquirer_type"]
        reason = "取得者区分の候補が複数あるため、安全側で税理士確認前提の既定分岐に戻しました"
        requires_clarification = True
        missing_fact = "home_acquirer"
    else:
        acquirer_type = rules["expert"]["demo_case"]["default_acquirer_type"]
        reason = "明示語が不足しているため、M1の既定値として同居親族分岐から確認"
        requires_clarification = True
        missing_fact = (
            "home_acquirer"
            if acquirer_undecided
            else "residence_and_home_ownership"
            if residence_conflict
            else _missing_fact_for_text(text)
        )

    if _matches_finalized_partition(text):
        partition_status = "finalized"
        partition_reason = "分割協議書または分割確定を示す語を検出"
    elif _has_any(text, ["見込書", "見込み", "分割予定"]):
        partition_status = "expected"
        partition_reason = "分割見込書または予定を示す語を検出"
    else:
        partition_status = "in_progress"
        partition_reason = "分割協議の確定情報が無いため進行中として起票"

    return {
        "acquirer_type": acquirer_type,
        "reason": reason,
        "partition_status": partition_status,
        "partition_reason": partition_reason,
        "requires_clarification": requires_clarification,
        "missing_fact": missing_fact,
        "authoritative_structured_facts": False,
        "hard_clarification": hard_clarification,
    }


def _home_acquirer_is_undecided(text: str) -> bool:
    compact = re.sub(r"\s+", "", text)
    patterns = [
        r"(?:取得者|自宅を取得する人|自宅を相続する人)(?:が|は)?(?:未定|不明|決まってい(?:ない|ません)|決定していません|決まっておらず)",
        r"誰が.{0,12}(?:相続|取得|引き継|受け継).{0,18}(?:未定|不明|決まってい(?:ない|ません)|決定していません|決まっておらず)",
        r"(?:相続|取得|引き継|受け継).{0,12}(?:人|方)(?:が|は)?(?:未定|不明|決まってい(?:ない|ません)|決定していません)",
    ]
    return any(re.search(pattern, compact) for pattern in patterns)


def _selected_residence_conflict(text: str) -> bool:
    heirs = normalize_heirs(_infer_heirs_from_text(text))
    selected_id = _infer_home_acquirer_id(text, heirs)
    selected = next((heir for heir in heirs if heir["id"] == selected_id), None)
    if selected is None or selected.get("relation") == "spouse":
        return False
    positive, negative = _residence_evidence_for_label(text, str(selected.get("name") or ""))
    return positive and negative


def _missing_fact_for_text(text: str) -> str:
    heirs = normalize_heirs(_infer_heirs_from_text(text))
    if _infer_home_acquirer_id(text, heirs):
        return "residence_and_home_ownership"
    return "home_acquirer"


def _resolve_ambiguous_route_from_state(
    text: str,
    state: dict[str, Any],
    route: dict[str, Any],
) -> dict[str, Any]:
    """相談文が曖昧でも、指名された既存カードに構造化事実があれば安全に分岐できる。"""
    if not route.get("requires_clarification"):
        return route
    if route.get("hard_clarification"):
        return route
    heirs = normalize_heirs(state.get("heirs", []))
    inferred_from_text = False
    if not heirs:
        heirs = normalize_heirs(_infer_heirs_from_text(text))
        inferred_from_text = True
    selected_id = _infer_home_acquirer_id(text, heirs)
    selected = next((heir for heir in heirs if heir["id"] == selected_id), None)
    if selected is None:
        return route
    if inferred_from_text and selected.get("relation") != "spouse":
        residence_signal = _residence_signal_for_label(text, str(selected.get("name") or ""))
        if residence_signal is None:
            return route
    acquirer_type = acquirer_type_for_heir(selected)
    return {
        **route,
        "acquirer_type": acquirer_type,
        "reason": "相談文で指名された登録済み取得者カードの続柄・同居情報から分岐を選択",
        "requires_clarification": False,
        "missing_fact": "",
        "authoritative_structured_facts": not inferred_from_text,
    }


def _route_from_card_state(state: dict[str, Any], route: dict[str, Any]) -> dict[str, Any]:
    """カードReviewでは、選択済みカードをRouterの確定入力として扱う。"""
    heirs = normalize_heirs(state.get("heirs", []))
    selected_id = str(state.get("home_acquirer_id") or "")
    selected = next((heir for heir in heirs if heir["id"] == selected_id), None)
    if selected is None:
        return route
    return {
        **route,
        "acquirer_type": acquirer_type_for_heir(selected),
        "reason": "登録済みの自宅取得者カードの続柄・同居情報から分岐を選択",
        "requires_clarification": False,
        "missing_fact": "",
        "authoritative_structured_facts": True,
        "hard_clarification": False,
    }


def _structured_heir_context(state: dict[str, Any]) -> str:
    heirs = normalize_heirs(state.get("heirs", []))
    if not heirs:
        return ""
    return "、".join(
        f"{heir['name']}（{'配偶者' if heir['relation'] == 'spouse' else '同居' if heir['co_resident'] else '非同居'}）"
        for heir in heirs
    )


def _deterministic_clarification_route(route: dict[str, Any]) -> dict[str, Any]:
    missing_fact = str(route.get("missing_fact") or "home_acquirer")
    if missing_fact not in CLARIFICATION_QUESTIONS:
        missing_fact = "home_acquirer"
    reason = (
        "Geminiの有効なFunction Callingを得られず、取得者分岐に必要な事実も不足しているため安全停止"
    )
    return {
        **route,
        "decision": "clarify",
        "clarification": {
            "missing_fact": missing_fact,
            "question": CLARIFICATION_QUESTIONS[missing_fact],
            "reason": reason,
        },
    }


def _document_events(text: str) -> dict[str, Any]:
    updates: dict[str, str] = {}
    keyword_updates = {
        "family_register": ["戸籍"],
        "resident_record": ["住民票", "戸籍附票"],
        "land_registry": ["登記", "登記事項", "地番"],
        "cadastral_map": ["公図"],
        "survey_map": ["測量図", "地積測量"],
        "fixed_asset_certificate": ["固定資産", "評価証明"],
        "partition_agreement": ["遺産分割", "協議書", "見込書"],
    }
    for doc_id, keywords in keyword_updates.items():
        if _has_any(text, keywords):
            updates[doc_id] = "received"

    title_anomaly = _has_any(
        text,
        [
            "先代名義",
            "祖父名義",
            "祖母名義",
            "父名義",
            "母名義",
            "名義が違",
            "古い名義",
            "未登記",
            "被相続人以外の名義",
        ],
    )
    if title_anomaly:
        updates["prior_generation_title_check"] = "requested"

    return {
        "status_updates": updates,
        "title_anomaly": title_anomaly,
    }


def _title_anomaly_actions(document_events: dict[str, Any]) -> list[dict[str, Any]]:
    if not document_events["title_anomaly"]:
        return []
    return [
        {
            "type": "flag_title_anomaly",
            "target": "prior_generation_title_check",
            "value": "requested",
            "reason": "登記名義と被相続人のずれを示す相談語から先代名義土地の確認を起票",
        }
    ]


def _case_title(text: str) -> str:
    return NEUTRAL_CASE_TITLE


def _normalize_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized


def _has_any(text: str, keywords: list[str]) -> bool:
    return any(keyword in text for keyword in keywords)


def _matches_spouse(text: str) -> bool:
    if _has_any(text, ["配偶者", "奥様", "ご主人"]):
        return True

    spouse_patterns = [
        r"妻",
        r"(?<!丈)夫",
        r"(?<![祖義養継])父",
        r"(?<![祖義養継])母",
    ]
    return any(_matches_person_transfer(text, pattern) for pattern in spouse_patterns)


def _matches_person_transfer(text: str, person_pattern: str) -> bool:
    for match in re.finditer(person_pattern, text):
        tail = text[match.end() : match.end() + 18]
        compact_tail = re.sub(r"\s+", "", tail)
        if compact_tail.startswith(
            (
                "名義",
                "が名義",
                "は名義",
                "の相続",
                "が亡くな",
                "は亡くな",
                "が死亡",
                "は死亡",
                "が死去",
                "は死去",
                "が逝去",
                "は逝去",
                "が他界",
                "は他界",
                "が被相続人",
                "は被相続人",
            )
        ):
            continue

        transfer = re.search(r"(?:取得|相続|引き継|受け継)", tail)
        if not transfer:
            continue

        context = tail[: transfer.start()]
        if _has_any(context, ["名義", "亡くな", "死亡", "死去", "逝去", "他界", "被相続人"]):
            continue
        return True

    return False


def _matches_co_resident(text: str) -> bool:
    return _has_co_resident_context(text)


def _has_co_resident_context(text: str) -> bool:
    if re.search(r"(?<!非)(?<!未)同居", text):
        return True
    if _has_any(text, ["一緒に住", "住み続け", "居住継続", "一緒に暮ら", "共に暮ら"]):
        return True
    return bool(
        re.search(
            r"(?:被相続人|故人)(?:と|と一緒に|と共に).{0,12}(?:暮ら|生活して)",
            text,
        )
    )


def _matches_house_lost(text: str) -> bool:
    lowered = text.lower()
    if _has_any(text, ["家なき子", "持ち家なし", "持家なし", "自宅なし"]):
        return True
    if re.search(r"(?<!guest\s)\bhouse lost\b", lowered) and "warehouse lost" not in lowered:
        return True

    person = r"(?:長男|長女|次男|次女|二男|二女|取得者|相続人|本人|子|息子|娘)"
    lifestyle = r"(?:別居|非同居|賃貸|借家|持ち家なし|持家なし|持ち家のない|持家のない|3年以内|三年以内)"
    transfer = r"(?:相続|取得|引き継|受け継)"
    patterns = [
        rf"{lifestyle}.{{0,14}}{person}.{{0,24}}{transfer}",
        rf"{person}.{{0,14}}{lifestyle}.{{0,24}}{transfer}",
        rf"{person}.{{0,24}}{transfer}.{{0,24}}{lifestyle}",
    ]
    return any(re.search(pattern, text) for pattern in patterns)


def _matches_finalized_partition(text: str) -> bool:
    negative_terms = ["未確定", "確定していない", "確定前", "未作成"]
    if _has_any(text, negative_terms):
        return False
    finalized_patterns = [
        r"分割済",
        r"全員合意",
        r"(?:遺産)?分割(?:が|は)?確定",
        r"協議(?:が|は)?確定",
        r"(?:遺産分割)?協議書(?:が|は|を)?(?:ある|あり|作成済|締結済|提出済|受領|確認)",
    ]
    return any(re.search(pattern, text) for pattern in finalized_patterns)


def _raise_status(current: str, candidate: str) -> str:
    if STATUS_RANK.get(candidate, 0) > STATUS_RANK.get(current, 0):
        return candidate
    return current
