from __future__ import annotations

from typing import Any

from scripts import production_evidence


class FakeResponse:
    def __init__(self, payload: dict[str, Any], status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise AssertionError(f"unexpected status: {self.status_code}")

    def json(self) -> dict[str, Any]:
        return self._payload


class FakeClient:
    def __init__(self, gemini_route: str, calls: list[str]) -> None:
        self.gemini_route = gemini_route
        self.calls = calls

    def __enter__(self) -> FakeClient:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def post(self, path: str, json: dict[str, Any] | None = None) -> FakeResponse:
        self.calls.append(path)
        if path != "/api/run":
            return FakeResponse({"ok": True})
        return FakeResponse(
            {
                "run": {
                    "mode": "gemini_function_calling",
                    "gemini": {
                        "used": True,
                        "tool_name": "select_taker_branch",
                        "arguments": {"acquirer_type": self.gemini_route},
                        "fallback_reason": "",
                        "latency_ms": 12,
                    },
                },
                "case": {
                    "analysis": {"acquirer": {"id": "co_resident"}},
                    "state": {
                        "heirs": [{"id": "second_son"}],
                        "home_acquirer_id": "second_son",
                    },
                },
            }
        )


def test_run_case_clears_seed_cards_and_requires_matching_gemini_route(monkeypatch) -> None:
    calls: list[str] = []
    current_route = {"value": "co_resident"}

    monkeypatch.setattr(
        production_evidence,
        "_client",
        lambda base_url: FakeClient(current_route["value"], calls),
    )

    passed = production_evidence._run_case("https://example.test", "case", "相談文", "co_resident")
    assert calls == ["/api/demo/seed", "/api/demo/clear-heirs", "/api/run"]
    assert passed["gemini_route"] == "co_resident"
    assert passed["cards_inferred"] is True
    assert passed["passed"] is True

    calls.clear()
    current_route["value"] = "house_lost"
    failed = production_evidence._run_case("https://example.test", "case", "相談文", "co_resident")
    assert failed["actual_route"] == "co_resident"
    assert failed["gemini_route"] == "house_lost"
    assert failed["passed"] is False


class FakeClarificationClient:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def __enter__(self) -> FakeClarificationClient:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def post(self, path: str, json: dict[str, Any] | None = None) -> FakeResponse:
        self.calls.append(path)
        empty_state = {"heirs": [], "home_acquirer_id": "", "acquirer_type": "co_resident"}
        if path == "/api/demo/seed":
            return FakeResponse({"ok": True})
        if path == "/api/demo/clear-heirs":
            return FakeResponse({"case": {"state": empty_state}})
        if path == "/api/run":
            return FakeResponse(
                {
                    "run": {
                        "id": "run-clarify-1",
                        "status": "AWAITING_CLARIFICATION",
                        "state_mutated": False,
                        "gemini": {
                            "used": True,
                            "tool_name": "request_clarification",
                            "latency_ms": 10,
                        },
                        "clarification": {
                            "missing_fact": "residence_and_home_ownership",
                        },
                    },
                    "case": {
                        "state": empty_state,
                        "approval": {"review_ready": False},
                    },
                }
            )
        if path == "/api/approve":
            return FakeResponse({"detail": "review_not_ready"}, status_code=409)
        if path == "/api/run/continue":
            return FakeResponse(
                {
                    "run": {
                        "continuation_of": "run-clarify-1",
                        "status": "REVIEW_PENDING",
                        "gemini": {
                            "used": True,
                            "tool_name": "select_taker_branch",
                            "latency_ms": 12,
                        },
                        "decision_history": [
                            {"tool": "request_clarification"},
                            {"tool": "select_taker_branch"},
                        ],
                    },
                    "case": {
                        "analysis": {"acquirer": {"id": "house_lost"}},
                        "approval": {"review_ready": True},
                    },
                }
            )
        raise AssertionError(path)

    def get(self, path: str) -> FakeResponse:
        self.calls.append(path)
        if path == "/api/export/word":
            return FakeResponse({"detail": "approval_required"}, status_code=409)
        raise AssertionError(path)


def test_run_clarification_case_requires_stop_resume_and_history(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        production_evidence,
        "_client",
        lambda base_url: FakeClarificationClient(calls),
    )
    case = {
        "id": "clarify_residence",
        "input": "曖昧な相談文です。",
        "missing_fact": "residence_and_home_ownership",
        "answer": "別居で賃貸、持ち家はありません。",
        "expected_route": "house_lost",
    }

    result = production_evidence._run_clarification_case("https://example.test", case)

    assert calls == [
        "/api/demo/seed",
        "/api/demo/clear-heirs",
        "/api/run",
        "/api/approve",
        "/api/export/word",
        "/api/run/continue",
    ]
    assert result["first_state_unchanged"] is True
    assert result["decision_history"] == ["request_clarification", "select_taker_branch"]
    assert result["actual_route"] == "house_lost"
    assert result["fallback"] is False
    assert result["passed"] is True
