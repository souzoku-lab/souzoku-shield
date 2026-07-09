from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles

from .agent_run import build_agent_run, build_card_review_run
from .docx_export import build_shomen_docx
from .engine.harness import evaluate_bad_demo_fixture, evaluate_suite
from .engine.reducer import (
    acquirer_type_for_heir,
    build_counterfactuals,
    normalize_heirs,
    reduce_case,
    select_home_acquirer_id_for_type,
)
from .models import (
    CasePatch,
    ConsultationRunRequest,
    DocumentPatch,
    HeirCreateRequest,
    HeirPatch,
    HealthResponse,
    IntakeRequest,
    ManualOpinionPatch,
)
from .rules_loader import default_case_state, load_rules


APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"

app = FastAPI(title="Souzoku Attachment Agent M1", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

_STATE: dict[str, Any] = default_case_state()
_LAST_RUN: dict[str, Any] | None = None
_WORD_EXPORT_APPROVED = False

HEIR_RELATIONSHIP_PROFILES = {
    "spouse": {"id": "spouse", "name": "配偶者", "relation": "spouse"},
    "eldest_son": {"id": "eldest_son", "name": "長男", "relation": "child"},
    "eldest_daughter": {"id": "eldest_daughter", "name": "長女", "relation": "child"},
    "second_son": {"id": "second_son", "name": "次男", "relation": "child"},
    "second_daughter": {"id": "second_daughter", "name": "次女", "relation": "child"},
    "third_son": {"id": "third_son", "name": "三男", "relation": "child"},
    "third_daughter": {"id": "third_daughter", "name": "三女", "relation": "child"},
}


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health", response_model=HealthResponse)
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "service": "souzoku-attachment-agent",
        "storage": "memory",
        "llm_required": False,
        "gemini_configured": bool(os.getenv("GEMINI_API_KEY")),
    }


@app.post("/api/demo/seed")
def seed_demo() -> dict[str, Any]:
    global _STATE, _LAST_RUN
    _STATE = default_case_state()
    _LAST_RUN = None
    _reset_approval()
    return {"ok": True, "state": copy.deepcopy(_STATE), "case": get_case()}


@app.post("/api/demo/clear-heirs")
def clear_demo_heirs() -> dict[str, Any]:
    """自然文から相続人カードを起票するデモ用の未登録状態に戻す。"""
    global _LAST_RUN
    _STATE["heirs"] = []
    _STATE["home_acquirer_id"] = ""
    _STATE["acquirer_type"] = load_rules()["expert"]["demo_case"]["default_acquirer_type"]
    _LAST_RUN = None
    _reset_approval()
    return {"ok": True, "case": get_case()}


@app.get("/api/case")
def get_case() -> dict[str, Any]:
    rules = load_rules()
    state = copy.deepcopy(_STATE)
    reduced = reduce_case(state, rules)
    return {
        "state": state,
        "manual_inputs": _manual_inputs_payload(state),
        "rules_summary": _rules_summary(rules),
        "analysis": reduced,
        "counterfactuals": build_counterfactuals(state, rules),
        "harness": evaluate_suite(rules, state),
        "bad_demo_fixture": evaluate_bad_demo_fixture(rules, state),
        "last_run": copy.deepcopy(_LAST_RUN),
        "approval": _approval_payload(),
    }


@app.patch("/api/case")
def patch_case(payload: CasePatch) -> dict[str, Any]:
    if payload.acquirer_type is not None:
        _STATE["acquirer_type"] = payload.acquirer_type
        selected_id = select_home_acquirer_id_for_type(_STATE, payload.acquirer_type)
        if selected_id:
            _STATE["home_acquirer_id"] = selected_id
    if payload.home_acquirer_id is not None:
        _set_home_acquirer(payload.home_acquirer_id)
    if payload.partition_status is not None:
        _STATE["partition_status"] = payload.partition_status
    _reset_review_state(clear_run=True)
    return get_case()


@app.post("/api/intake")
def create_intake(payload: IntakeRequest) -> dict[str, Any]:
    """案件1枚投入のM1導線。保存はdemo memoryのみ。"""
    _STATE["case_title"] = payload.title
    _STATE["acquirer_type"] = payload.acquirer_type
    selected_id = select_home_acquirer_id_for_type(_STATE, payload.acquirer_type)
    if selected_id:
        _STATE["home_acquirer_id"] = selected_id
    _STATE["partition_status"] = payload.partition_status
    _reset_review_state(clear_run=True)
    return get_case()


@app.post("/api/run")
def run_agent(payload: ConsultationRunRequest) -> dict[str, Any]:
    """相談文からACTIONタイムラインを起動する。応答文は返さず状態を更新する。"""
    global _STATE, _LAST_RUN
    rules = load_rules()
    next_state, run = build_agent_run(
        consultation_text=payload.text,
        state=copy.deepcopy(_STATE),
        rules=rules,
        gemini_configured=bool(os.getenv("GEMINI_API_KEY")),
    )
    _STATE = next_state
    _STATE["manual_inputs"] = _default_manual_inputs()
    _LAST_RUN = run
    _reset_approval()
    return {"ok": True, "run": copy.deepcopy(run), "case": get_case()}


@app.post("/api/review/from-cards")
def run_review_from_cards() -> dict[str, Any]:
    """相談文なしで、登録済み相続人カードからReview到達状態を作る。"""
    global _STATE, _LAST_RUN
    _ensure_card_review_inputs()
    rules = load_rules()
    next_state, run = build_card_review_run(
        state=copy.deepcopy(_STATE),
        rules=rules,
        gemini_configured=bool(os.getenv("GEMINI_API_KEY")),
    )
    _STATE = next_state
    _STATE["manual_inputs"] = _default_manual_inputs()
    _LAST_RUN = run
    _reset_approval()
    return {"ok": True, "run": copy.deepcopy(run), "case": get_case()}


@app.patch("/api/documents/{document_id}")
def patch_document(document_id: str, payload: DocumentPatch) -> dict[str, Any]:
    if document_id not in _STATE["documents"]:
        raise HTTPException(status_code=404, detail="document_not_found")
    _STATE["documents"][document_id] = payload.status
    _reset_review_state(clear_run=True)
    return get_case()


@app.patch("/api/heirs/{heir_id}")
def patch_heir(heir_id: str, payload: HeirPatch) -> dict[str, Any]:
    heirs = _ensure_heirs(_STATE)
    heir = next((item for item in heirs if item["id"] == heir_id), None)
    if heir is None:
        raise HTTPException(status_code=404, detail="heir_not_found")
    if payload.name is not None:
        heir["name"] = payload.name
    if payload.relation is not None:
        heir["relation"] = payload.relation
    if payload.co_resident is not None:
        heir["co_resident"] = payload.co_resident
    if _STATE.get("home_acquirer_id") == heir_id:
        _STATE["acquirer_type"] = acquirer_type_for_heir(heir)
    _reset_review_state(clear_run=True)
    return get_case()


@app.post("/api/heirs")
def create_heir(payload: HeirCreateRequest) -> dict[str, Any]:
    heirs = _ensure_heirs(_STATE)
    profile = HEIR_RELATIONSHIP_PROFILES[payload.relationship]
    new_heir = {
        "id": _next_heir_id(profile["id"], heirs),
        "name": profile["name"],
        "relation": profile["relation"],
        "co_resident": payload.co_resident,
    }
    heirs.append(new_heir)
    _STATE["heirs"] = heirs
    if not _STATE.get("home_acquirer_id"):
        _STATE["home_acquirer_id"] = new_heir["id"]
        _STATE["acquirer_type"] = acquirer_type_for_heir(new_heir)
    _reset_review_state(clear_run=True)
    return get_case()


@app.patch("/api/manual/overall-opinion")
def patch_overall_opinion(payload: ManualOpinionPatch) -> dict[str, Any]:
    """税理士が画面上で手入力した総合所見を保存する。AIはこの欄を生成しない。"""
    manual_inputs = _ensure_manual_inputs(_STATE)
    manual_inputs["overall_opinion"] = payload.overall_opinion
    _reset_approval()
    return get_case()


@app.get("/api/counterfactuals")
def counterfactuals() -> dict[str, Any]:
    rules = load_rules()
    return {"branches": build_counterfactuals(copy.deepcopy(_STATE), rules)}


@app.get("/api/harness")
def harness() -> dict[str, Any]:
    rules = load_rules()
    state = copy.deepcopy(_STATE)
    return {
        "current": evaluate_suite(rules, state),
        "bad_demo_fixture": evaluate_bad_demo_fixture(rules, state),
    }


@app.post("/api/approve")
def approve_word_export() -> dict[str, Any]:
    """Review終端のHITL承認。承認後だけWord出力を許可する。"""
    global _WORD_EXPORT_APPROVED
    if not _review_ready():
        raise HTTPException(status_code=409, detail="review_not_ready")
    _WORD_EXPORT_APPROVED = True
    return {"ok": True, "approval": _approval_payload(), "export_url": "/api/export/word"}


@app.get("/api/export/word")
def export_word() -> StreamingResponse:
    if not (_WORD_EXPORT_APPROVED and _review_ready()):
        raise HTTPException(status_code=409, detail="approval_required")
    rules = load_rules()
    state = copy.deepcopy(_STATE)
    analysis = reduce_case(state, rules)
    analysis["manual_inputs"] = _manual_inputs_payload(state)
    harness_result = evaluate_suite(rules, state)
    docx = build_shomen_docx(analysis, harness_result)
    filename = f"shomen_attachment_{analysis['case']['id']}.docx"
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    return StreamingResponse(
        iter([docx]),
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers=headers,
    )


def _rules_summary(rules: dict[str, Any]) -> dict[str, Any]:
    expert = rules["expert"]
    return {
        "acquirer_types": [
            {"id": key, "label": value["label"], "summary": value["summary"]}
            for key, value in expert["acquirer_types"].items()
        ],
        "partition_statuses": [
            {"id": key, "label": value["label"]}
            for key, value in expert["partition_statuses"].items()
        ],
        "document_statuses": expert["document_statuses"],
    }


def _reset_approval() -> None:
    global _WORD_EXPORT_APPROVED
    _WORD_EXPORT_APPROVED = False


def _reset_review_state(*, clear_run: bool = False) -> None:
    global _LAST_RUN
    _reset_approval()
    if clear_run:
        _LAST_RUN = None


def _default_manual_inputs() -> dict[str, str]:
    return {"overall_opinion": ""}


def _ensure_heirs(state: dict[str, Any]) -> list[dict[str, Any]]:
    heirs = normalize_heirs(state.get("heirs", []))
    state["heirs"] = heirs
    if not any(heir["id"] == state.get("home_acquirer_id") for heir in heirs) and heirs:
        state["home_acquirer_id"] = heirs[0]["id"]
    return heirs


def _ensure_card_review_inputs() -> None:
    heirs = _ensure_heirs(_STATE)
    if not heirs:
        raise HTTPException(status_code=409, detail="heirs_required_for_review")
    if not _STATE.get("home_acquirer_id"):
        raise HTTPException(status_code=409, detail="home_acquirer_required_for_review")


def _set_home_acquirer(heir_id: str) -> None:
    heirs = _ensure_heirs(_STATE)
    heir = next((item for item in heirs if item["id"] == heir_id), None)
    if heir is None:
        raise HTTPException(status_code=404, detail="heir_not_found")
    _STATE["home_acquirer_id"] = heir_id
    _STATE["acquirer_type"] = acquirer_type_for_heir(heir)


def _next_heir_id(base_id: str, heirs: list[dict[str, Any]]) -> str:
    existing = {str(heir.get("id")) for heir in heirs}
    if base_id not in existing:
        return base_id
    suffix = 2
    while f"{base_id}_{suffix}" in existing:
        suffix += 1
    return f"{base_id}_{suffix}"


def _ensure_manual_inputs(state: dict[str, Any]) -> dict[str, str]:
    raw = state.get("manual_inputs")
    if not isinstance(raw, dict):
        raw = {}
        state["manual_inputs"] = raw
    raw.setdefault("overall_opinion", "")
    return raw


def _manual_inputs_payload(state: dict[str, Any]) -> dict[str, str]:
    manual = _ensure_manual_inputs(state)
    return {"overall_opinion": str(manual.get("overall_opinion", ""))}


def _review_ready() -> bool:
    if not _LAST_RUN:
        return False
    return any(
        step.get("id") == "review" and step.get("status") == "PENDING_APPROVAL"
        for step in _LAST_RUN.get("steps", [])
    )


def _approval_payload() -> dict[str, Any]:
    ready = _review_ready()
    return {
        "status": "APPROVED" if _WORD_EXPORT_APPROVED else ("PENDING_APPROVAL" if ready else "NEEDS_REVIEW"),
        "review_ready": ready,
        "word_export_enabled": _WORD_EXPORT_APPROVED and ready,
    }
