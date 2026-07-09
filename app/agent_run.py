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
                "acquirer_type": {"type": "string", "enum": ["spouse", "co_resident", "house_lost"]},
                "reason": {"type": "string"},
            },
            "required": ["acquirer_type", "reason"],
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
) -> tuple[dict[str, Any], dict[str, Any]]:
    """相談文をACTIONタイムラインへ変換し、reducer実行で案件状態を更新する。"""
    text = _normalize_text(consultation_text)
    next_state = copy.deepcopy(state)
    route = _route_consultation(text, rules)
    gemini_trace = _gemini_trace(configured=gemini_configured)
    if gemini_configured:
        gemini_route, gemini_trace = _route_consultation_with_gemini(text, rules, route)
        route = gemini_route
    heir_events = _heir_events_for_text(text, next_state, route)
    document_events = _document_events(text)

    next_state["case_title"] = _case_title(text)
    next_state["acquirer_type"] = route["acquirer_type"]
    selected_id = heir_events.get("home_acquirer_id") or select_home_acquirer_id_for_type(
        next_state, route["acquirer_type"]
    )
    if selected_id:
        next_state["home_acquirer_id"] = selected_id
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
    run = {
        "id": f"run_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "mode": "gemini_function_calling" if gemini_trace.get("used") else "deterministic_replay",
        "gemini_configured": gemini_configured,
        "gemini": gemini_trace,
        "responds_with": "actions_only",
        "assistant_reply": "",
        "approval_status": "PENDING_APPROVAL",
        "input_text": text,
        "tool_declarations": TOOL_DECLARATIONS,
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
    fallback_route: dict[str, str],
) -> tuple[dict[str, str], dict[str, Any]]:
    """Gemini function callingでRouter分岐だけを選ばせ、失敗時は決定的分岐へ戻す。"""
    started = time.perf_counter()
    trace = _gemini_trace(configured=True)
    api_key = os.getenv("GEMINI_API_KEY", "")
    try:
        client = _create_gemini_client(api_key)
        interaction = client.interactions.create(
            model=GEMINI_ROUTER_MODEL,
            input=_gemini_router_prompt(text, rules),
            tools=_gemini_router_tools(),
        )
        function_call = _first_function_call(interaction)
        if not function_call:
            trace["fallback_reason"] = "gemini_no_function_call"
            trace["latency_ms"] = _elapsed_ms(started)
            return fallback_route, trace

        tool_name = str(function_call.get("name", ""))
        arguments = _normalize_function_arguments(function_call.get("arguments", {}))
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
            return fallback_route, trace

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
    }


def _create_gemini_client(api_key: str) -> Any:
    from google import genai

    return genai.Client(api_key=api_key)


def _gemini_router_tools() -> list[dict[str, Any]]:
    return [
        {"type": "function", **declaration}
        for declaration in TOOL_DECLARATIONS
        if declaration["name"] == GEMINI_ROUTER_TOOL_NAME
    ]


def _gemini_router_prompt(text: str, rules: dict[str, Any]) -> str:
    labels = {
        acquirer_type: config["label"]
        for acquirer_type, config in rules["expert"]["acquirer_types"].items()
    }
    return (
        "あなたは相続税申告支援デモのRouterです。"
        "税務結論、書面添付本文、総合所見は書かず、必ず function calling で"
        f"{GEMINI_ROUTER_TOOL_NAME} だけを呼び出してください。"
        "目的は、相談文から自宅取得者の確認分岐を選ぶことだけです。"
        f"選択肢: {json.dumps(labels, ensure_ascii=False)}。"
        "配偶者が自宅を相続する文脈なら spouse、同居親族が取得する文脈なら co_resident、"
        "別居・賃貸・家なき子候補が取得する文脈なら house_lost を選んでください。"
        f"\n相談文: {text}"
    )


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
    negative_terms = ["別居", "賃貸", "借家", "持ち家なし", "持家なし", "自宅なし", "家なき子"]
    positive_terms = ["同居", "一緒に住", "住み続け", "居住継続"]
    for match in re.finditer(re.escape(label), text):
        sentence = _sentence_around(text, match.start(), match.end())
        if _has_any(sentence, negative_terms):
            return False
        if _has_any(sentence, positive_terms):
            return True
    return False


def _infer_home_acquirer_id(text: str, heirs: list[dict[str, Any]]) -> str:
    candidates: list[tuple[int, str]] = []
    for heir in heirs:
        for alias in _aliases_for_heir(heir):
            for match in re.finditer(re.escape(alias), text):
                sentence = _sentence_around(text, match.start(), match.end())
                tail = text[match.end() : match.end() + 32]
                if _has_any(sentence, ["相続", "取得"]) and (
                    _has_any(sentence, ["自宅", "宅地", "居宅", "土地"]) or _has_any(tail, ["相続", "取得"])
                ):
                    candidates.append((match.start(), str(heir["id"])))
    if not candidates:
        return ""
    candidates.sort(key=lambda item: item[0])
    return candidates[0][1]


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


def _route_consultation(text: str, rules: dict[str, Any]) -> dict[str, str]:
    branch_matches: list[tuple[str, str]] = []
    if _matches_house_lost(text):
        branch_matches.append(
            (
                "house_lost",
                "家なき子・持ち家なし・取得者本人の別居賃貸を示す語から家なき子分岐を選択",
            )
        )
    if _matches_spouse(text):
        branch_matches.append(("spouse", "配偶者取得を示す語から配偶者分岐を選択"))
    if _matches_co_resident(text):
        branch_matches.append(("co_resident", "同居・居住継続を示す語から同居親族分岐を選択"))

    if len(branch_matches) == 1:
        acquirer_type, reason = branch_matches[0]
    elif len(branch_matches) > 1:
        acquirer_type = rules["expert"]["demo_case"]["default_acquirer_type"]
        reason = "取得者区分の候補が複数あるため、安全側で税理士確認前提の既定分岐に戻しました"
    else:
        acquirer_type = rules["expert"]["demo_case"]["default_acquirer_type"]
        reason = "明示語が不足しているため、M1の既定値として同居親族分岐から確認"

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

        transfer = re.search(r"(?:取得|相続)", tail)
        if not transfer:
            continue

        context = tail[: transfer.start()]
        if _has_any(context, ["名義", "亡くな", "死亡", "死去", "逝去", "他界", "被相続人"]):
            continue
        return True

    return False


def _matches_co_resident(text: str) -> bool:
    return _has_any(text, ["同居", "一緒に住", "住み続け", "居住継続"])


def _matches_house_lost(text: str) -> bool:
    lowered = text.lower()
    if _has_any(text, ["家なき子", "持ち家なし", "持家なし", "自宅なし"]):
        return True
    if re.search(r"(?<!guest\s)\bhouse lost\b", lowered) and "warehouse lost" not in lowered:
        return True

    person = r"(?:長男|長女|次男|次女|二男|二女|取得者|相続人|本人|子|息子|娘)"
    lifestyle = r"(?:別居|賃貸|借家|持ち家なし|持家なし|3年以内|三年以内)"
    transfer = r"(?:相続|取得)"
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
