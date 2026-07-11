from __future__ import annotations

import copy
import os
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, Response
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
    ClarificationAnswerRequest,
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

# 公開デモは同一Originのフロントエンドからしか呼ばれないため、CORSは開放しない。
# 状態は訪問者ごとにCookieセッションで完全分離する（レポート §9 のフル分離）。
app = FastAPI(title="Souzoku Shield — 相続の盾 (M1)", version="0.1.0")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

SESSION_COOKIE = "souzoku_sid"
SESSION_TTL_SECONDS = 30 * 60  # 30分。審査員が離席してもデモが混ざらないよう自然失効させる。
RUN_COOLDOWN_SECONDS = 2.0
RUN_LIMIT_PER_SESSION = 20


@dataclass
class DemoSession:
    """訪問者1人ぶんの案件状態。プロセス共有をやめ、審査員ごとに隔離する。"""

    state: dict[str, Any] = field(default_factory=default_case_state)
    last_run: dict[str, Any] | None = None
    pending_clarification: dict[str, Any] | None = None
    word_export_approved: bool = False
    touched_at: float = field(default_factory=time.time)
    run_count: int = 0
    last_run_started_at: float = 0.0
    run_in_progress: bool = False
    run_guard: Lock = field(default_factory=Lock, repr=False, compare=False)


class SessionStore:
    """Cookie(session id)でDemoSessionを引くメモリストア。永続化はしない。"""

    # 公開URLはCookie無しリクエスト（bot/クローラ）でもセッションを生む。TTL到達前に
    # 無制限に積み上がってOOMしないよう、上限を設けて最古から追い出す（可用性ガード）。
    MAX_SESSIONS = 5000

    def __init__(self) -> None:
        self._sessions: dict[str, DemoSession] = {}
        self._lock = Lock()

    def _sweep(self, now: float) -> None:
        expired = [sid for sid, s in self._sessions.items() if now - s.touched_at > SESSION_TTL_SECONDS]
        for sid in expired:
            self._sessions.pop(sid, None)

    def _evict_oldest_if_full(self) -> None:
        overflow = len(self._sessions) - self.MAX_SESSIONS + 1
        if overflow <= 0:
            return
        oldest = sorted(self._sessions.items(), key=lambda item: item[1].touched_at)
        for sid, _ in oldest[:overflow]:
            self._sessions.pop(sid, None)

    def get(self, sid: str | None) -> DemoSession | None:
        if not sid:
            return None
        now = time.time()
        with self._lock:
            session = self._sessions.get(sid)
            if session is None:
                return None
            if now - session.touched_at > SESSION_TTL_SECONDS:
                self._sessions.pop(sid, None)
                return None
            session.touched_at = now
            return session

    def create(self) -> tuple[str, DemoSession]:
        now = time.time()
        sid = secrets.token_urlsafe(24)
        with self._lock:
            self._sweep(now)
            self._evict_oldest_if_full()
            session = DemoSession(touched_at=now)
            self._sessions[sid] = session
        return sid, session


_STORE = SessionStore()


def _request_is_https(request: Request) -> bool:
    """Cloud Run等のリバースプロキシ越しでも元スキームを判定する。"""
    forwarded = request.headers.get("x-forwarded-proto", "")
    if forwarded:
        return forwarded.split(",")[0].strip().lower() == "https"
    return request.url.scheme == "https"


def get_session(request: Request, response: Response) -> DemoSession:
    """訪問者のCookieからセッションを引く。無ければ新規作成しCookieを発行する。"""
    sid = request.cookies.get(SESSION_COOKIE)
    session = _STORE.get(sid)
    if session is None:
        sid, session = _STORE.create()
        response.set_cookie(
            key=SESSION_COOKIE,
            value=sid,
            max_age=SESSION_TTL_SECONDS,
            httponly=True,
            samesite="lax",
            # 公開デモ(HTTPS)ではsecure付与、localhost(HTTP)開発では無効。
            # Cloud Runはproxy越しにHTTPで届くため、元スキームは X-Forwarded-Proto を見る。
            secure=_request_is_https(request),
        )
    return session


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
        "version": os.getenv("APP_VERSION", "local"),
        "revision": os.getenv("K_REVISION", "local"),
    }


@app.post("/api/demo/seed")
def seed_demo(session: DemoSession = Depends(get_session)) -> dict[str, Any]:
    _reject_if_run_in_progress(session)
    session.state = default_case_state()
    session.last_run = None
    session.pending_clarification = None
    _reset_approval(session)
    return {"ok": True, "state": copy.deepcopy(session.state), "case": _case_payload(session)}


@app.post("/api/demo/clear-heirs")
def clear_demo_heirs(session: DemoSession = Depends(get_session)) -> dict[str, Any]:
    """自然文から相続人カードを起票するデモ用の未登録状態に戻す。"""
    _reject_if_run_in_progress(session)
    session.state["heirs"] = []
    session.state["home_acquirer_id"] = ""
    session.state["acquirer_type"] = load_rules()["expert"]["demo_case"]["default_acquirer_type"]
    session.last_run = None
    session.pending_clarification = None
    _reset_approval(session)
    return {"ok": True, "case": _case_payload(session)}


@app.get("/api/case")
def get_case(session: DemoSession = Depends(get_session)) -> dict[str, Any]:
    return _case_payload(session)


@app.patch("/api/case")
def patch_case(payload: CasePatch, session: DemoSession = Depends(get_session)) -> dict[str, Any]:
    _reject_if_run_in_progress(session)
    _reject_if_clarification_pending(session)
    if payload.acquirer_type is not None:
        session.state["acquirer_type"] = payload.acquirer_type
        selected_id = select_home_acquirer_id_for_type(session.state, payload.acquirer_type)
        if selected_id:
            session.state["home_acquirer_id"] = selected_id
    if payload.home_acquirer_id is not None:
        _set_home_acquirer(session, payload.home_acquirer_id)
    if payload.partition_status is not None:
        session.state["partition_status"] = payload.partition_status
    _reset_review_state(session, clear_run=True)
    return _case_payload(session)


@app.post("/api/intake")
def create_intake(payload: IntakeRequest, session: DemoSession = Depends(get_session)) -> dict[str, Any]:
    """案件1枚投入のM1導線。保存はdemo memoryのみ。"""
    _reject_if_run_in_progress(session)
    _reject_if_clarification_pending(session)
    session.state["case_title"] = payload.title
    session.state["acquirer_type"] = payload.acquirer_type
    selected_id = select_home_acquirer_id_for_type(session.state, payload.acquirer_type)
    if selected_id:
        session.state["home_acquirer_id"] = selected_id
    session.state["partition_status"] = payload.partition_status
    _reset_review_state(session, clear_run=True)
    return _case_payload(session)


@app.post("/api/run")
def run_agent(payload: ConsultationRunRequest, session: DemoSession = Depends(get_session)) -> dict[str, Any]:
    """相談文からACTIONタイムラインを起動する。応答文は返さず状態を更新する。"""
    _reject_if_clarification_pending(session)
    _reserve_agent_run(session)
    try:
        rules = load_rules()
        next_state, run = build_agent_run(
            consultation_text=payload.text,
            state=copy.deepcopy(session.state),
            rules=rules,
            gemini_configured=bool(os.getenv("GEMINI_API_KEY")),
        )
        _store_agent_result(session, next_state, run)
        return {"ok": True, "run": copy.deepcopy(run), "case": _case_payload(session)}
    finally:
        _finish_agent_run(session)


@app.post("/api/run/continue")
def continue_agent(
    payload: ClarificationAnswerRequest,
    session: DemoSession = Depends(get_session),
) -> dict[str, Any]:
    """追加回答を元相談へ結合し、停止中のRouter判断を再開する。"""
    pending = copy.deepcopy(session.pending_clarification)
    if pending is None:
        raise HTTPException(status_code=409, detail="clarification_not_pending")

    _reserve_agent_run(session, bypass_cooldown=True)
    try:
        rules = load_rules()
        combined_text = _continuation_text(pending, payload.answer)
        next_state, run = build_agent_run(
            consultation_text=combined_text,
            state=copy.deepcopy(session.state),
            rules=rules,
            gemini_configured=bool(os.getenv("GEMINI_API_KEY")),
            source="clarification_resume",
            decision_history=copy.deepcopy(pending["decision_history"]),
            continuation_of=str(pending["run_id"]),
        )
        _store_agent_result(session, next_state, run)
        return {"ok": True, "run": copy.deepcopy(run), "case": _case_payload(session)}
    finally:
        _finish_agent_run(session)


@app.post("/api/review/from-cards")
def run_review_from_cards(session: DemoSession = Depends(get_session)) -> dict[str, Any]:
    """相談文なしで、登録済み相続人カードからReview到達状態を作る。"""
    _reject_if_run_in_progress(session)
    _reject_if_clarification_pending(session)
    _ensure_card_review_inputs(session)
    _reserve_agent_run(session)
    try:
        rules = load_rules()
        next_state, run = build_card_review_run(
            state=copy.deepcopy(session.state),
            rules=rules,
            gemini_configured=bool(os.getenv("GEMINI_API_KEY")),
        )
        _store_agent_result(session, next_state, run)
        return {"ok": True, "run": copy.deepcopy(run), "case": _case_payload(session)}
    finally:
        _finish_agent_run(session)


@app.patch("/api/documents/{document_id}")
def patch_document(
    document_id: str, payload: DocumentPatch, session: DemoSession = Depends(get_session)
) -> dict[str, Any]:
    _reject_if_run_in_progress(session)
    _reject_if_clarification_pending(session)
    if document_id not in session.state["documents"]:
        raise HTTPException(status_code=404, detail="document_not_found")
    session.state["documents"][document_id] = payload.status
    _reset_review_state(session, clear_run=True)
    return _case_payload(session)


@app.patch("/api/heirs/{heir_id}")
def patch_heir(heir_id: str, payload: HeirPatch, session: DemoSession = Depends(get_session)) -> dict[str, Any]:
    _reject_if_run_in_progress(session)
    _reject_if_clarification_pending(session)
    heirs = _ensure_heirs(session.state)
    heir = next((item for item in heirs if item["id"] == heir_id), None)
    if heir is None:
        raise HTTPException(status_code=404, detail="heir_not_found")
    if payload.relationship is not None:
        profile = HEIR_RELATIONSHIP_PROFILES[payload.relationship]
        heir["name"] = profile["name"]
        heir["relation"] = profile["relation"]
    if payload.name is not None:
        heir["name"] = payload.name
    if payload.relation is not None:
        heir["relation"] = payload.relation
    if payload.co_resident is not None:
        heir["co_resident"] = payload.co_resident
    if session.state.get("home_acquirer_id") == heir_id:
        session.state["acquirer_type"] = acquirer_type_for_heir(heir)
    _reset_review_state(session, clear_run=True)
    return _case_payload(session)


@app.delete("/api/heirs/{heir_id}")
def delete_heir(heir_id: str, session: DemoSession = Depends(get_session)) -> dict[str, Any]:
    _reject_if_run_in_progress(session)
    _reject_if_clarification_pending(session)
    heirs = _ensure_heirs(session.state)
    if not any(item["id"] == heir_id for item in heirs):
        raise HTTPException(status_code=404, detail="heir_not_found")

    remaining = [item for item in heirs if item["id"] != heir_id]
    session.state["heirs"] = remaining
    if session.state.get("home_acquirer_id") == heir_id:
        if remaining:
            next_acquirer = remaining[0]
            session.state["home_acquirer_id"] = next_acquirer["id"]
            session.state["acquirer_type"] = acquirer_type_for_heir(next_acquirer)
        else:
            session.state["home_acquirer_id"] = ""
            session.state["acquirer_type"] = load_rules()["expert"]["demo_case"]["default_acquirer_type"]
    _reset_review_state(session, clear_run=True)
    return _case_payload(session)


@app.post("/api/heirs")
def create_heir(payload: HeirCreateRequest, session: DemoSession = Depends(get_session)) -> dict[str, Any]:
    _reject_if_run_in_progress(session)
    _reject_if_clarification_pending(session)
    heirs = _ensure_heirs(session.state)
    profile = HEIR_RELATIONSHIP_PROFILES[payload.relationship]
    new_heir = {
        "id": _next_heir_id(profile["id"], heirs),
        "name": profile["name"],
        "relation": profile["relation"],
        "co_resident": payload.co_resident,
    }
    heirs.append(new_heir)
    session.state["heirs"] = heirs
    if not session.state.get("home_acquirer_id"):
        session.state["home_acquirer_id"] = new_heir["id"]
        session.state["acquirer_type"] = acquirer_type_for_heir(new_heir)
    _reset_review_state(session, clear_run=True)
    return _case_payload(session)


@app.patch("/api/manual/overall-opinion")
def patch_overall_opinion(
    payload: ManualOpinionPatch, session: DemoSession = Depends(get_session)
) -> dict[str, Any]:
    """税理士が画面上で手入力した総合所見を保存する。AIはこの欄を生成しない。"""
    _reject_if_run_in_progress(session)
    _reject_if_clarification_pending(session)
    manual_inputs = _ensure_manual_inputs(session.state)
    manual_inputs["overall_opinion"] = payload.overall_opinion
    _reset_approval(session)
    return _case_payload(session)


@app.get("/api/counterfactuals")
def counterfactuals(session: DemoSession = Depends(get_session)) -> dict[str, Any]:
    rules = load_rules()
    return {"branches": build_counterfactuals(copy.deepcopy(session.state), rules)}


@app.get("/api/harness")
def harness(session: DemoSession = Depends(get_session)) -> dict[str, Any]:
    rules = load_rules()
    state = copy.deepcopy(session.state)
    return {
        "current": evaluate_suite(rules, state),
        "bad_demo_fixture": evaluate_bad_demo_fixture(rules, state),
    }


@app.post("/api/approve")
def approve_word_export(session: DemoSession = Depends(get_session)) -> dict[str, Any]:
    """Review終端のHITL承認。承認後だけWord出力を許可する。"""
    _reject_if_run_in_progress(session)
    if not _review_ready(session):
        raise HTTPException(status_code=409, detail="review_not_ready")
    session.word_export_approved = True
    return {"ok": True, "approval": _approval_payload(session), "export_url": "/api/export/word"}


@app.get("/api/export/word")
def export_word(session: DemoSession = Depends(get_session)) -> StreamingResponse:
    _reject_if_run_in_progress(session)
    if not (session.word_export_approved and _review_ready(session)):
        raise HTTPException(status_code=409, detail="approval_required")
    rules = load_rules()
    state = copy.deepcopy(session.state)
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


def _case_payload(session: DemoSession) -> dict[str, Any]:
    rules = load_rules()
    state = copy.deepcopy(session.state)
    reduced = reduce_case(state, rules)
    return {
        "state": state,
        "manual_inputs": _manual_inputs_payload(state),
        "rules_summary": _rules_summary(rules),
        "analysis": reduced,
        "counterfactuals": build_counterfactuals(state, rules),
        "harness": evaluate_suite(rules, state),
        "bad_demo_fixture": evaluate_bad_demo_fixture(rules, state),
        "last_run": copy.deepcopy(session.last_run),
        "pending_clarification": _pending_clarification_payload(session),
        "approval": _approval_payload(session),
    }


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


def _reset_approval(session: DemoSession) -> None:
    session.word_export_approved = False


def _reserve_agent_run(session: DemoSession, *, bypass_cooldown: bool = False) -> None:
    now = time.monotonic()
    with session.run_guard:
        if session.run_in_progress:
            raise HTTPException(status_code=409, detail="run_in_progress")
        if session.run_count >= RUN_LIMIT_PER_SESSION:
            raise HTTPException(
                status_code=429,
                detail="run_limit_exceeded",
                headers={"Retry-After": str(SESSION_TTL_SECONDS)},
            )
        elapsed = now - session.last_run_started_at
        if not bypass_cooldown and session.last_run_started_at and elapsed < RUN_COOLDOWN_SECONDS:
            retry_after = max(1, int(RUN_COOLDOWN_SECONDS - elapsed + 0.999))
            raise HTTPException(
                status_code=429,
                detail="run_cooldown",
                headers={"Retry-After": str(retry_after)},
            )
        session.run_in_progress = True
        session.run_count += 1
        session.last_run_started_at = now


def _finish_agent_run(session: DemoSession) -> None:
    with session.run_guard:
        session.run_in_progress = False


def _reset_review_state(session: DemoSession, *, clear_run: bool = False) -> None:
    _reset_approval(session)
    session.pending_clarification = None
    if clear_run:
        session.last_run = None


def _default_manual_inputs() -> dict[str, str]:
    return {"overall_opinion": ""}


def _ensure_heirs(state: dict[str, Any]) -> list[dict[str, Any]]:
    heirs = normalize_heirs(state.get("heirs", []))
    state["heirs"] = heirs
    if not any(heir["id"] == state.get("home_acquirer_id") for heir in heirs) and heirs:
        state["home_acquirer_id"] = heirs[0]["id"]
    return heirs


def _ensure_card_review_inputs(session: DemoSession) -> None:
    heirs = _ensure_heirs(session.state)
    if not heirs:
        raise HTTPException(status_code=409, detail="heirs_required_for_review")
    if not session.state.get("home_acquirer_id"):
        raise HTTPException(status_code=409, detail="home_acquirer_required_for_review")


def _set_home_acquirer(session: DemoSession, heir_id: str) -> None:
    heirs = _ensure_heirs(session.state)
    heir = next((item for item in heirs if item["id"] == heir_id), None)
    if heir is None:
        raise HTTPException(status_code=404, detail="heir_not_found")
    session.state["home_acquirer_id"] = heir_id
    session.state["acquirer_type"] = acquirer_type_for_heir(heir)


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


def _review_ready(session: DemoSession) -> bool:
    if not session.last_run:
        return False
    return any(
        step.get("id") == "review" and step.get("status") == "PENDING_APPROVAL"
        for step in session.last_run.get("steps", [])
    )


def _approval_payload(session: DemoSession) -> dict[str, Any]:
    ready = _review_ready(session)
    approved = session.word_export_approved
    awaiting = session.pending_clarification is not None
    return {
        "status": (
            "AWAITING_CLARIFICATION"
            if awaiting
            else "APPROVED"
            if approved
            else "PENDING_APPROVAL"
            if ready
            else "NEEDS_REVIEW"
        ),
        "review_ready": ready,
        "word_export_enabled": approved and ready,
    }


def _store_agent_result(
    session: DemoSession,
    next_state: dict[str, Any],
    run: dict[str, Any],
) -> None:
    """追加確認なら状態不変で停止し、それ以外は案件状態を確定する。"""
    if run.get("status") == "AWAITING_CLARIFICATION":
        session.pending_clarification = {
            "original_text": str(run["input_text"]),
            "run_id": str(run["id"]),
            "missing_fact": str(run["clarification"]["missing_fact"]),
            "question": str(run["clarification"]["question"]),
            "decision_history": copy.deepcopy(run.get("decision_history", [])),
        }
    else:
        session.state = next_state
        session.state["manual_inputs"] = _default_manual_inputs()
        session.pending_clarification = None
    session.last_run = run
    _reset_approval(session)


def _pending_clarification_payload(session: DemoSession) -> dict[str, Any] | None:
    pending = session.pending_clarification
    if pending is None:
        return None
    return {
        "run_id": str(pending["run_id"]),
        "missing_fact": str(pending["missing_fact"]),
        "question": str(pending["question"]),
    }


def _reject_if_clarification_pending(session: DemoSession) -> None:
    if session.pending_clarification is not None:
        raise HTTPException(status_code=409, detail="clarification_pending")


def _reject_if_run_in_progress(session: DemoSession) -> None:
    if session.run_in_progress:
        raise HTTPException(status_code=409, detail="run_in_progress")


def _continuation_text(pending: dict[str, Any], answer: str) -> str:
    original = str(pending["original_text"])
    missing_fact = str(pending.get("missing_fact") or "")
    if missing_fact == "home_acquirer":
        supplement = f"追加回答（自宅取得者）: {answer} 自宅を取得する予定なのはこの方です。"
    else:
        supplement = (
            f"追加回答（自宅取得者本人の居住・持ち家状況）: 取得者本人は{answer} "
            "この取得者本人が自宅を取得します。"
        )
    return f"{original} {supplement}"
